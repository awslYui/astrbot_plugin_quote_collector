"""Read OneBot v11 / NapCat merged forwards without relying on an SDK shape."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .core import UserError, clean_text
from .store import Quote


class ReadLimitError(UserError):
    """Abort the complete import when a traversal budget is exceeded."""


def _unescape(value: str, parameter: bool = False) -> str:
    if parameter:
        value = value.replace("&#44;", ",")
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def segments(value: Any) -> list[dict]:
    """Accept array messages, CQ strings, and AstrBot components as a fallback."""
    if value is None:
        return []
    if isinstance(value, str):
        result, offset = [], 0
        for match in re.finditer(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]", value):
            if match.start() > offset:
                result.append({"type": "text", "data": {"text": _unescape(value[offset:match.start()])}})
            data = {}
            for item in match.group(2).lstrip(",").split(","):
                key, separator, content = item.partition("=")
                if separator:
                    data[key] = _unescape(content, parameter=True)
            result.append({"type": match.group(1).lower(), "data": data})
            offset = match.end()
        if offset < len(value):
            result.append({"type": "text", "data": {"text": _unescape(value[offset:])}})
        return result
    if isinstance(value, Mapping):
        if "type" in value:
            return [dict(value)]
        return segments(value.get("message", value.get("content")))
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(segments(item))
        return result
    kind = getattr(value, "type", "")
    kind = str(getattr(kind, "value", kind)).lower()
    if kind == "nodes":
        return [{"type": "node", "data": {
            "content": segments(getattr(node, "content", [])),
            "user_id": getattr(node, "uin", ""), "nickname": getattr(node, "name", ""),
        }} for node in getattr(value, "nodes", [])]
    if kind == "node":
        return [{"type": "node", "data": {
            "content": segments(getattr(value, "content", [])),
            "user_id": getattr(value, "uin", ""), "nickname": getattr(value, "name", ""),
        }}]
    if callable(getattr(value, "toDict", None)):
        return segments(value.toDict())
    return []


def forward_id(segment: dict) -> str | None:
    kind, data = segment.get("type"), segment.get("data", {})
    if not isinstance(data, Mapping):
        return None
    if kind == "forward":
        value = data.get("id")
        return str(value) if value else None
    if kind == "json":
        try:
            card = data.get("data", {})
            card = json.loads(card) if isinstance(card, str) else card
            if card.get("app") == "com.tencent.multimsg":
                detail = card.get("meta", {}).get("detail", {})
                value = detail.get("resid") or detail.get("resId")
                return str(value) if value else None
        except (TypeError, ValueError, AttributeError):
            return None
    if kind == "xml":
        try:
            root = ET.fromstring(data.get("data", ""))
            value = root.get("m_resid") or root.get("resid")
            return str(value) if value else None
        except (TypeError, ValueError, ET.ParseError):
            return None
    return None


def contains_forward(message: Any) -> bool:
    return any(item.get("type") in {"forward", "node", "nodes"} or forward_id(item)
               for item in segments(message))


def reply_id(message: Any) -> str | None:
    for item in segments(message):
        if item.get("type") == "reply":
            value = item.get("data", {}).get("id")
            if value is not None:
                return str(value)
    return None


@dataclass
class Extraction:
    quotes: list[Quote] = field(default_factory=list)
    skipped: int = 0
    media: int = 0
    errors: int = 0


class ForwardReader:
    def __init__(self, call_api: Callable[..., Awaitable[Any]], limit: int = 2000,
                 ignore_patterns: list[re.Pattern] | None = None):
        self.call_api = call_api
        self.limit = limit
        self.ignore_patterns = ignore_patterns or []
        self.report = Extraction()
        self._seen: set[tuple[str, str]] = set()
        self._api_calls = 0
        self._visited_nodes = 0
        self._ordinary_quotes = 0

    async def api(self, action: str, **params) -> Any:
        self._api_calls += 1
        if self._api_calls > 128:
            raise ReadLimitError("嵌套聊天记录读取次数超过 128 次，本次未写入。请分批引用导入。")
        result = await self.call_api(action, **params)
        if isinstance(result, Mapping) and ("retcode" in result or "status" in result):
            if result.get("status") == "failed" or result.get("retcode", 0) not in (0, None):
                raise UserError("QQ 协议端未能读取这条消息，请确认记录仍可打开后重试。")
            result = result.get("data", result)
        return result

    async def read(self, message: Any, *, allow_plain: bool = False,
                   source_id: str = "", author_id: str = "", author_name: str = "") -> Extraction:
        await self._walk_message(message, 0, allow_plain, source_id, author_id, author_name)
        return self.report

    async def _walk_message(self, message: Any, depth: int, allow_plain: bool,
                            source_id: str, author_id: str, author_name: str) -> None:
        if depth > 8:
            raise UserError("聊天记录嵌套超过 8 层，本次未写入。请引用内层记录导入。")
        items = segments(message)
        text = []
        media = 0
        has_forward = False
        for item in items:
            kind, data = item.get("type"), item.get("data", {})
            if not isinstance(data, Mapping):
                self.report.skipped += 1
                continue
            resource = forward_id(item)
            if resource:
                has_forward = True
                await self._read_forward(resource, depth + 1, source_id)
            elif kind == "node":
                has_forward = True
                await self._read_node(item, depth + 1, source_id)
            elif kind == "nodes":
                has_forward = True
                for node in data.get("messages", data.get("nodes", [])):
                    await self._read_node(node, depth + 1, source_id)
            elif kind == "text":
                text.append(str(data.get("text", "")))
            elif kind == "face":
                text.append(f"[表情:{data.get('id', '')}]")
            elif kind == "at":
                qq = str(data.get("qq", ""))
                text.append("@全体成员" if qq == "all" else f"@{qq}")
            elif kind not in {"reply"}:
                media += 1
        if allow_plain:
            self.report.media += media
            body = clean_text("".join(text))
            if body and not any(pattern.search(body) for pattern in self.ignore_patterns):
                if len(body) > 30000:
                    raise UserError("有语录超过 30000 字，本次未写入。请拆分后添加。")
                # A compact export can use multiple continuation nodes for
                # one quote. The catalog parser validates actual item counts.
                from .exporting import FRAGMENT
                fragment = FRAGMENT.fullmatch(body)
                if not fragment and self._ordinary_quotes >= self.limit:
                    raise UserError(f"单次导入超过 {self.limit} 条，本次未写入。请分批引用导入，或调高导入上限。")
                if not fragment:
                    self._ordinary_quotes += 1
                self.report.quotes.append(Quote(body, author_id, author_name, source_id))
            elif not has_forward:
                self.report.skipped += 1

    async def _read_forward(self, resource: str, depth: int, source_id: str) -> None:
        key = ("forward", resource)
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            result = await self.api("get_forward_msg", id=resource)
        except ReadLimitError:
            raise
        except Exception:
            self.report.errors += 1
            return
        if isinstance(result, Mapping):
            nodes = result.get("messages", result.get("message", []))
        else:
            nodes = result
        if not isinstance(nodes, list):
            self.report.errors += 1
            return
        for node in nodes:
            await self._read_node(node, depth, source_id or resource)

    async def _read_node(self, node: Any, depth: int, source_id: str) -> None:
        self._visited_nodes += 1
        if self._visited_nodes > self.limit * 5 + 100:
            raise UserError("聊天记录节点过多，本次未写入。请分批引用导入。")
        if not isinstance(node, Mapping):
            self.report.skipped += 1
            return
        data = node.get("data", {}) if node.get("type") == "node" else node
        if not isinstance(data, Mapping):
            self.report.skipped += 1
            return
        content = data.get("content", data.get("message"))
        if content is None and data.get("id") is not None:
            message_id = str(data["id"])
            key = ("message", message_id)
            if key in self._seen:
                return
            self._seen.add(key)
            if not message_id.lstrip("-").isdigit():
                self.report.errors += 1
                return
            try:
                data = await self.api("get_msg", message_id=int(message_id))
                if not isinstance(data, Mapping):
                    self.report.errors += 1
                    return
                content = data.get("message", data.get("content"))
            except ReadLimitError:
                raise
            except Exception:
                self.report.errors += 1
                return
        sender = data.get("sender") or {}
        if not isinstance(sender, Mapping):
            sender = {}
        await self._walk_message(content, depth, True,
                                 str(data.get("message_id") or source_id),
                                 str(sender.get("user_id") or data.get("user_id") or data.get("uin") or ""),
                                 str(sender.get("card") or sender.get("nickname") or data.get("nickname") or data.get("name") or ""))
