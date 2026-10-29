"""Copy existing forward contents under explicit original keyword labels."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .core import UserError, check_keyword, clean_text, parse_command
from .catalog import parse_catalog
from .forwarding import Extraction, forward_id, segments
from .store import Quote


@dataclass(frozen=True)
class Reference:
    message: Any
    message_id: str
    author_id: str
    author_name: str


def source_keys(message: Any, message_id: str) -> list[tuple[str, str]]:
    keys = {("message", message_id)} if message_id else set()
    for item in segments(message):
        resource = forward_id(item)
        if resource:
            keys.add(("forward", resource))
    return sorted(keys)


def keyword_hint(value: str, *, text_header: bool = False) -> str | None:
    """Only recognize explicit metadata, never arbitrary nicknames/prose."""
    value = clean_text(value)
    if not value or len(value) > 180 or "\n" in value:
        return None
    patterns = (
        r"(?:关键词|关键字)\s*[:：]\s*(.+)",
        r"【(.+?)】(?:的)?(?:语录(?:合集|列表|查询结果)?|(?:共|共有)?\s*\d+\s*条语录)\s*[:：]?",
        r"(.+?)的语录(?:合集|列表|查询结果)?(?:[（(](?:共|共有)?\s*\d+\s*条[）)])?\s*[:：]?",
        r"(.+?)语录(?:合集|列表|查询结果)\s*[:：]?",
        r"(.+?)\s*·\s*#\d+",
    )
    # Ordinary quote text such as “这是他的语录” must remain a quote. The
    # broader title/name formats apply only to metadata, not message prose.
    for pattern in patterns[:2] if text_header else patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return check_keyword(match.group(1))
    # An actual query copied into the existing record is explicit provenance.
    if re.match(r"^[／/]?查\s", value) or (not text_header and re.match(r"^[／/]?来点", value)):
        try:
            command = parse_command(value)
        except UserError:
            return None
        if command is not None and command.action in {"查", "来点"} and command.page == 1:
            return command.keyword
    return None


def envelope_hints(message: Any) -> set[str]:
    result = set()
    for item in segments(message):
        kind, data = item.get("type"), item.get("data", {})
        if not isinstance(data, Mapping):
            continue
        labels = []
        if kind == "text":
            labels.append(str(data.get("text", "")))
        elif kind == "json":
            try:
                card = data.get("data", {})
                card = json.loads(card) if isinstance(card, str) else card
                if card.get("app") == "com.tencent.multimsg":
                    detail = card.get("meta", {}).get("detail", {})
                    labels.extend(detail.get(key, "") for key in ("title", "source"))
            except (TypeError, ValueError, AttributeError):
                pass
        elif kind == "xml":
            try:
                root = ET.fromstring(data.get("data", ""))
                labels.extend(root.get(key, "") for key in ("brief", "summary"))
            except (TypeError, ValueError, ET.ParseError):
                pass
        for label in labels:
            if isinstance(label, str):
                hint = keyword_hint(label)
                if hint:
                    result.add(hint)
    return result


@dataclass
class ReplicaPlan:
    groups: dict[str, list[Quote]]
    headers: int = 0
    libraries: dict[str, set[str]] = field(default_factory=dict)
    triggers: dict[str, set[str]] = field(default_factory=dict)


def plan_replica(report: Extraction, *, hints: set[str] | None = None,
                 remembered: set[str] | None = None, limit: int = 2000,
                 aliases: dict[str, str] | None = None) -> ReplicaPlan:
    catalog = parse_catalog(report.quotes, limit=limit)
    if catalog is not None:
        return ReplicaPlan(catalog.groups, catalog.headers, catalog.libraries, catalog.triggers)
    aliases = aliases or {}
    entries = []
    explicit = {aliases.get(keyword, keyword) for keyword in hints or []}
    remembered = {aliases.get(keyword, keyword) for keyword in remembered or []}
    for quote in report.quotes:
        label = keyword_hint(quote.author_name)
        label = aliases.get(label, label)
        first, separator, rest = quote.body.partition("\n")
        header = keyword_hint(first, text_header=True)
        header = aliases.get(header, header)
        if header and label and header != label:
            raise UserError("原记录的关键词标注不一致，本次未写入。请按关键词分别引用记录。")
        if header:
            explicit.add(header)
            if not separator or not rest.strip():
                entries.append((header, None))
                continue
            # Remove metadata only; every remaining character comes from the
            # quoted record. No completions, paraphrasing or synthetic quotes.
            rest = re.sub(r"^语录\s*[:：]\s*", "", rest, count=1)
            entries.append((header, replace(quote, body=clean_text(rest))))
        else:
            if label:
                explicit.add(label)
            entries.append((label, quote))
    fallback = next(iter(explicit)) if len(explicit) == 1 else None
    if not explicit and len(remembered or set()) == 1:
        fallback = next(iter(remembered))
    groups, current, headers = {}, None, 0
    for label, quote in entries:
        if quote is None:
            current = label
            headers += 1
            continue
        keyword = label or current or fallback
        if not keyword:
            raise UserError("无法从这份记录确认原关键词，本次未写入。可引用记录发送“偷 原关键词”；或先让原机器人返回“查 原关键词”的结果，再引用该结果回复“复刻”。")
        if quote.body:
            groups.setdefault(check_keyword(keyword), []).append(quote)
    if not groups:
        raise UserError("这份聊天记录只有说明信息，没有可复刻的现成文本语录。")
    return ReplicaPlan(groups, headers)
