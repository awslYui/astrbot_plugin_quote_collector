"""Compact, bounded QQ catalog pages with lossless continuation fragments."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from .core import UserError
from .store import Quote, StoredQuote

# These are conservative plugin budgets, not claimed QQ protocol maxima.
NODE_CHARS = 6000
MAX_NODES = 40
MAX_PAGE_QUOTES = 10000
FRAME_RESERVE = 64
HEADER_RESERVE = 512
FRAGMENT = re.compile(r"【语录库片段 ([0-9]{1,3})/([0-9]{1,3})】\n\{\n([\s\S]*)\n\}")
FRAGMENT_PREFIX = "【语录库片段 "
PRETTY_PAIRS = (("‘", "’"), ("“", "”"), ("「", "」"), ("『", "』"), ("'", "'"))


@dataclass
class CatalogPage:
    fragments: list[str]
    total: int
    shown: int
    page: int
    pages: int


@dataclass
class _Group:
    keyword: str
    total: int
    entries: list[str] = field(default_factory=list)


def _quote_text(body: str) -> tuple[str, bool]:
    # Avoid an apparent closing wrapper in the middle of multiline contents.
    # Only our marked exports decode JSON; other plugins' literal backslashes
    # retain their existing meaning.
    if not any(ord(char) < 32 and char not in "\n\t" for char in body):
        for left, right in PRETTY_PAIRS:
            if not any(line.rstrip().endswith(right) for line in body.split("\n")[:-1]):
                return left + body + right, False
    return json.dumps(body, ensure_ascii=False), True


def _append_budget(nodes: int, used: int, lengths: list[int], capacity: int) -> tuple[int, int]:
    for length in lengths:
        if length <= capacity:
            if used + length > capacity:
                nodes, used = nodes + 1, 0
            used += length
        else:
            # Long individual quotes may span nodes; framing restores them
            # without adding newlines or trimming boundary whitespace.
            extra, remainder = divmod(used + length, capacity)
            nodes += extra - (1 if remainder == 0 else 0)
            used = remainder or capacity
    return nodes, used


def _pack(atoms: list[str], capacity: int) -> list[str]:
    chunks, current = [], ""
    for atom in atoms:
        if len(atom) <= capacity:
            if len(current) + len(atom) > capacity:
                chunks.append(current)
                current = ""
            current += atom
            continue
        start = 0
        while start < len(atom):
            room = capacity - len(current)
            current += atom[start:start + room]
            start += room
            if len(current) == capacity:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return chunks


def build_catalog_page(rows: Iterable[StoredQuote], total_for: Callable[[str], int], page: int, *,
                       node_chars: int = NODE_CHARS, max_nodes: int = MAX_NODES,
                       max_quotes: int = MAX_PAGE_QUOTES) -> CatalogPage:
    """Scan a snapshot once and retain only the requested bounded page."""
    capacity = node_chars - FRAME_RESERVE
    if capacity < HEADER_RESERVE or max_nodes < 1 or max_quotes < 1:
        raise ValueError("Invalid catalog page budgets")
    current_page, nodes, used, count, block_count = 1, 1, 0, 0, 0
    current_keyword = None
    library_keyword = None
    library_total = 0
    groups: list[_Group] = []
    selected_count = total = 0
    encoded = False
    for row in rows:
        if row.keyword != library_keyword:
            library_keyword, library_total = row.keyword, total_for(row.keyword)
        new_group = row.keyword != current_keyword
        number = 1 if new_group else block_count + 1
        body, json_encoded = _quote_text(row.body)
        entry = f"{number}. {body}\n"
        candidate = _append_budget(nodes, used, ([HEADER_RESERVE] if new_group else []) + [len(entry)], capacity)
        if count and (candidate[0] > max_nodes or count >= max_quotes):
            current_page += 1
            nodes, used, count, block_count, current_keyword = 1, 0, 0, 0, None
            new_group = True
            entry = f"1. {body}\n"
            candidate = _append_budget(nodes, used, [HEADER_RESERVE, len(entry)], capacity)
        if candidate[0] > max_nodes:
            raise UserError("有单条语录过长，无法在一次聊天记录中完整展示，请先在 WebUI 中检查该语录。")
        if new_group:
            current_keyword, block_count = row.keyword, 0
            if current_page == page:
                groups.append(_Group(row.keyword, library_total))
        nodes, used = candidate
        block_count, count, total = block_count + 1, count + 1, total + 1
        if current_page == page:
            groups[-1].entries.append(entry)
            selected_count += 1
            encoded = encoded or json_encoded

    atoms: list[str] = []
    for group in groups:
        header = (f"主库：{group.keyword}\n触发词：来点{group.keyword}\n全部文本：{group.total} 条\n")
        if len(group.entries) != group.total:
            header += f"本页文本：{len(group.entries)} 条\n"
        header += "\n"
        if len(header) + 1 > HEADER_RESERVE:
            raise UserError("主词条说明过长，无法完整展示，请在 WebUI 中检查关键词。")
        if atoms:
            atoms.append("\n")
        atoms.extend([header, *group.entries])
    chunks = _pack(atoms, capacity)
    if len(chunks) > max_nodes:
        raise UserError("聊天记录分块超出本次展示上限，请缩小查询范围。")
    fragments = []
    for index, chunk in enumerate(chunks, 1):
        marker = f"【语录库片段 {index}/{len(chunks)}】\n" if len(chunks) > 1 or encoded else ""
        fragments.append(marker + "{\n" + chunk + "\n}")
    assert all(len(fragment) <= node_chars for fragment in fragments)
    return CatalogPage(fragments, total, selected_count, page, current_page if total else 0)


def restore_fragments(quotes: list[Quote]) -> tuple[list[Quote], set[int]]:
    """Restore explicitly framed exports before the numbered catalog parser."""
    restored: list[Quote] = []
    own_exports: set[int] = set()
    index = 0
    while index < len(quotes):
        quote = quotes[index]
        if not quote.body.startswith(FRAGMENT_PREFIX):
            restored.append(quote)
            index += 1
            continue
        match = FRAGMENT.fullmatch(quote.body)
        if not match or int(match[1]) != 1 or not 1 <= int(match[2]) <= MAX_NODES:
            raise UserError("语录库片段格式无效或缺少开头，本次未写入。请引用完整记录。")
        count = int(match[2])
        parts = []
        for position in range(1, count + 1):
            current = quotes[index + position - 1] if index + position - 1 < len(quotes) else None
            part = FRAGMENT.fullmatch(current.body) if current else None
            if not part or int(part[1]) != position or int(part[2]) != count:
                raise UserError("语录库片段缺失、乱序或不一致，本次未写入。请引用完整记录。")
            parts.append(part[3])
        own_exports.add(len(restored))
        restored.append(replace(quote, body="".join(parts)))
        index += count
    return restored, own_exports
