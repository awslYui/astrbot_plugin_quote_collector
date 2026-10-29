"""Command parsing and query/reply correlation, independent of AstrBot."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


class UserError(ValueError):
    """An actionable error that can be shown in QQ."""


def clean_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def check_keyword(value: str) -> str:
    value = clean_text(value)
    if not value or len(value) > 100 or "#" in value or "\n" in value:
        raise UserError("关键词须为 1～100 字，不能包含 # 或换行。")
    return value


def parse_alias_groups(value: list[str]) -> dict[str, str]:
    """Parse main#alias#alias groups; reject ambiguous or chained ownership."""
    if not isinstance(value, list) or len(value) > 200 or any(not isinstance(line, str) for line in value):
        raise UserError("人物别名须为文本列表，最多 200 组。")
    aliases: dict[str, str] = {}
    for line in value:
        if not line.strip():
            continue
        parts = line.split("#")
        if not 2 <= len(parts) <= 50:
            raise UserError("每组人物别名须为 主词条#别名#别名，包含 2～50 个名字。")
        names = list(dict.fromkeys(check_keyword(part) for part in parts))
        if len(names) < 2:
            raise UserError("每组人物别名至少需要一个与主词条不同的别名。")
        main = names[0]
        for name in names:
            if name in aliases and aliases[name] != main:
                raise UserError(f"“{name}”属于多个主词条，请将同一人物的名字放在同一组。")
            aliases[name] = main
    return aliases


@dataclass(frozen=True)
class Command:
    action: str
    keyword: str = ""
    body: str | None = None
    page: int = 1
    trigger: str = ""


def parse_command(text: str) -> Command | None:
    """Recognize explicit commands, including 来点name; ignore bare names."""
    text = text.strip()
    if text.startswith(("/", "／")):
        text = text[1:].lstrip()
    if text == "语录帮助":
        return Command("help")
    if text == "复刻":
        return Command("复刻")
    if re.match(r"^复刻\s", text):
        raise UserError("复刻只接受现有的合并聊天记录：请引用原记录后直接发送“复刻”，不填写新关键词或语录。")
    if text.startswith("来点"):
        action, payload = "来点", text[2:].strip()
    else:
        match = re.fullmatch(r"(查|添加|删除|偷)(?:\s+([\s\S]*))?", text)
        if not match:
            return None
        action, payload = match.group(1), (match.group(2) or "").strip()
    if not payload:
        if action == "来点":
            raise UserError("用法：来点关键词，例如 来点示例人物；单独发送人物名字不会触发语录。")
        raise UserError("用法：查 关键词；添加 关键词#语录；引用消息后 添加 关键词；删除 关键词#语录；引用聊天记录后 偷 关键词。")
    if action == "来点":
        if "#" in payload:
            raise UserError("来点关键词 每次随机返回一条语录，不支持翻页；查看语录库请用 查 关键词，翻页用 查 关键词#2。")
        keyword = check_keyword(payload)
        return Command(action, keyword, trigger="来点" + keyword)
    if action == "查":
        keyword, separator, page = payload.partition("#")
        if separator and (not page.isdecimal() or len(page) > 6 or int(page) < 1):
            raise UserError("翻页用法：查 示例人物#2，# 后填写大于 0 的页码。")
        keyword = check_keyword(keyword)
        return Command(action, keyword, page=int(page) if separator else 1)
    if action in {"添加", "删除"}:
        keyword, separator, body = payload.partition("#")
        keyword = check_keyword(keyword)
        if action == "删除" and not separator:
            raise UserError("删除用法：删除 关键词#完整语录。此操作需要管理员权限。")
        if separator:
            body = clean_text(body)
            if not body:
                raise UserError("# 后的语录不能为空。")
            if len(body) > 30000:
                raise UserError("单条语录不能超过 30000 字。")
            return Command(action, keyword, body)
        return Command(action, keyword)
    return Command(action, check_keyword(payload))


@dataclass(frozen=True)
class PendingQuery:
    scope: str
    message_id: str
    keyword: str
    expires_at: float


class QueryTracker:
    """Use reply IDs when possible; never guess between concurrent keywords."""

    def __init__(self, ttl: float = 60, max_pending: int = 1000):
        self.ttl = ttl
        self.max_pending = max_pending
        self._queries: dict[tuple[str, str], PendingQuery] = {}
        self._busy: set[tuple[str, str]] = set()

    def prune(self) -> None:
        now = time.monotonic()
        for key, query in list(self._queries.items()):
            if query.expires_at <= now:
                del self._queries[key]
                self._busy.discard(key)

    def add(self, scope: str, message_id: str, keyword: str) -> None:
        self.prune()
        if len(self._queries) >= self.max_pending:
            oldest = min(self._queries, key=lambda key: self._queries[key].expires_at)
            del self._queries[oldest]
            self._busy.discard(oldest)
        self._queries[(scope, message_id)] = PendingQuery(
            scope, message_id, keyword, time.monotonic() + self.ttl
        )

    def match(self, scope: str, reply_id: str | None = None) -> PendingQuery | None:
        self.prune()
        candidates = [query for query in self._queries.values() if query.scope == scope]
        if reply_id is not None:
            candidates = [query for query in candidates if query.message_id == reply_id]
        if not candidates or len({query.keyword for query in candidates}) != 1:
            return None
        # A second unquoted response must not be assigned to another request
        # merely because the first request is currently being downloaded.
        if any((query.scope, query.message_id) in self._busy for query in candidates):
            return None
        return max(candidates, key=lambda item: item.expires_at)

    def claim(self, scope: str, reply_id: str | None = None) -> PendingQuery | None:
        query = self.match(scope, reply_id)
        if query is None:
            return None
        self._busy.add((scope, query.message_id))
        return query

    def finish(self, query: PendingQuery, success: bool) -> None:
        self._busy.discard((query.scope, query.message_id))
        if success:
            # Repeated searches for the same keyword share one response.
            for key, candidate in list(self._queries.items()):
                if candidate.scope == query.scope and candidate.keyword == query.keyword and candidate.expires_at <= query.expires_at:
                    del self._queries[key]


def can_delete(astrbot_admin: bool, sender_id: str, sender_role: str,
               in_group: bool, extra_admins: set[str]) -> bool:
    return bool(astrbot_admin or sender_id in extra_admins or
                (in_group and sender_role in {"admin", "owner"}))
