"""Parse the original plugin's 主库 / 触发词 / 全部文本 numbered export."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .core import UserError, check_keyword, clean_text
from .store import Quote

FIELD = re.compile(r"(主库|触发词|全部文本)\s*[:：]\s*(.*)")
NUMBER = re.compile(r"\s*([0-9]+)[.．、)]\s*(.*)")
WRAPPERS = {"‘": "’", "“": "”", "'": "'", '"': '"', "「": "」", "『": "』"}


@dataclass
class CatalogPlan:
    groups: dict[str, list[Quote]]
    libraries: dict[str, set[str]]
    headers: int = 0
    triggers: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class _Block:
    library: str = ""
    trigger: str | None = None
    total: int | None = None
    items: list[Quote] = field(default_factory=list)


@dataclass
class _Item:
    owner: Quote
    lines: list[str]
    owners: set[int]

    def text(self) -> str:
        return clean_text("\n".join(self.lines))

    def closing(self) -> str | None:
        text = self.text()
        return WRAPPERS.get(text[:1])

    def closed(self) -> bool:
        text = self.text()
        closing = self.closing()
        return bool(closing and len(text) >= 2 and text.endswith(closing))


def parse_catalog(quotes: list[Quote], *, limit: int = 2000) -> CatalogPlan | None:
    """Return None for other formats; reject incomplete known-format exports.

    主库 is the person's ID / canonical entry; 触发词 is an optional alias.
    A catalog can occupy one QQ node or multiple adjacent nodes. Line ownership
    is retained so each extracted item keeps its original message provenance.
    """
    lines = [(line, quote, index) for index, quote in enumerate(quotes) for line in quote.body.split("\n")]
    fields = {match.group(1) for line, _, _ in lines if (match := FIELD.fullmatch(line.strip()))}
    if len(fields) < 2:
        return None

    blocks: list[_Block] = []
    block: _Block | None = None
    item: _Item | None = None
    used_owners: set[int] = set()
    parsed_count = 0

    def finish_item() -> None:
        nonlocal item, parsed_count
        if item is None:
            return
        body = item.text()
        if item.closing():
            if not item.closed():
                raise UserError("原语录的外围引号未闭合，本次未写入。请引用完整的原记录。")
            body = clean_text(body[1:-1])
        if not body or len(body) > 30000:
            raise UserError("原记录存在空语录或超过 30000 字的语录，本次未写入。")
        parsed_count += 1
        if parsed_count > limit:
            raise UserError(f"编号列表超过单次导入上限 {limit} 条，本次未写入。")
        assert block is not None
        block.items.append(replace(item.owner, body=body))
        used_owners.update(item.owners)
        item = None

    def finish_block() -> None:
        nonlocal block
        if block is None:
            return
        if not block.library or block.total is None:
            raise UserError("原记录缺少作为关键词的“主库”或“全部文本”信息，本次未写入。请引用完整原记录。")
        if len(block.items) != block.total:
            raise UserError(f"原记录标注 {block.total} 条文本，实际读到 {len(block.items)} 条，本次未写入。请引用完整原记录。")
        blocks.append(block)
        block = None

    for line, owner, owner_index in lines:
        stripped = line.strip()
        if item is not None and item.closing() and not item.closed():
            # Numbered lines and apparent metadata inside a quoted multiline
            # item are literal quote contents, not export structure.
            item.lines.append(line)
            item.owners.add(owner_index)
            continue
        if not stripped:
            if item is not None and not item.closing():
                item.lines.append(line)
                item.owners.add(owner_index)
            continue
        if stripped in {"{", "}", "｛", "｝"}:
            finish_item()
            if stripped in {"}", "｝"}:
                finish_block()
            continue
        match = FIELD.fullmatch(stripped)
        if match:
            name, value = match.groups()
            if item is not None or (block is not None and block.items):
                finish_item()
                previous_library = block.library if block else ""
                if name == "全部文本":
                    raise UserError("“全部文本”出现在编号列表之后，本次未写入。请确认原记录格式完整。")
                finish_block()
                block = _Block(library=previous_library if name == "触发词" else "")
            if block is None:
                block = _Block()
            if name == "主库":
                library = check_keyword(value)
                if block.library and block.library != library:
                    raise UserError("原记录的“主库”信息为空、过长或不一致，本次未写入。")
                block.library = library
            elif name == "触发词":
                trigger = clean_text(value)
                if len(trigger) > 200:
                    raise UserError("原记录的触发词过长，本次未写入。")
                if block.trigger is not None and block.trigger != trigger:
                    raise UserError("原记录出现不一致的触发词，本次未写入。")
                block.trigger = trigger
            else:
                total = re.fullmatch(r"([0-9]{1,6})\s*条", value.strip())
                if total is None:
                    raise UserError("“全部文本”须为“数字 条”，本次未写入。")
                count = int(total.group(1))
                if count > limit:
                    raise UserError(f"原记录标注 {count} 条，超过单次导入上限 {limit} 条，本次未写入。")
                if block.total is not None and block.total != count:
                    raise UserError("原记录的文本条数标注不一致，本次未写入。")
                block.total = count
            continue
        numbered = NUMBER.fullmatch(line)
        if numbered:
            finish_item()
            if block is None or not block.library or block.total is None:
                raise UserError("编号列表前缺少作为关键词的主库或文本条数，本次未写入。")
            if len(numbered.group(1)) > 6 or int(numbered.group(1)) != len(block.items) + 1:
                raise UserError("原记录编号缺失、重复或不连续，本次未写入。请引用完整原记录。")
            item = _Item(owner, [numbered.group(2)], {owner_index})
        elif item is not None and not item.closing():
            item.lines.append(line)
            item.owners.add(owner_index)
        elif block is None and (stripped == "语录查询结果" or re.fullmatch(r"[／/]?(?:查\s+.+|来点\s*.+)", stripped)):
            continue
        else:
            raise UserError("原语录列表中含有无法对应编号的内容，本次未写入。请单独引用完整的原语录记录。")
    finish_item()
    finish_block()

    groups: dict[str, list[Quote]] = {}
    libraries: dict[str, set[str]] = {}
    triggers: dict[str, set[str]] = {}
    for parsed in blocks:
        if parsed.items:
            keyword = parsed.library
            groups.setdefault(keyword, []).extend(parsed.items)
            libraries.setdefault(keyword, set()).add(parsed.library)
            if parsed.trigger:
                triggers.setdefault(keyword, set()).add(parsed.trigger)
    if not groups:
        raise UserError("原记录没有可复刻的文本语录，本次未写入。")
    return CatalogPlan(groups, libraries, len(quotes) - len(used_owners), triggers)
