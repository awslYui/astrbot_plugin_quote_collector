"""Durable, group-isolated quote storage using Python's SQLite library."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .core import UserError, clean_text


class QuoteConflict(UserError):
    """A duplicate or a quote changed in another window."""


class QuoteNotFound(UserError):
    """A quote was already removed."""


@dataclass(frozen=True)
class Quote:
    body: str
    author_id: str = ""
    author_name: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class StoredQuote:
    id: int
    keyword: str
    body: str


class QuoteStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                keyword TEXT NOT NULL,
                body TEXT NOT NULL,
                author_id TEXT NOT NULL DEFAULT '',
                author_name TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                collector_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scope, keyword, body)
            )""")
            conn.execute("CREATE TABLE IF NOT EXISTS quote_sessions (scope TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE IF NOT EXISTS quote_aliases (alias TEXT PRIMARY KEY, keyword TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO quote_sessions SELECT DISTINCT scope FROM quotes")
            has_source_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quote_sources'").fetchone()
            conn.execute("""CREATE TABLE IF NOT EXISTS quote_sources (
                scope TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL, keyword TEXT NOT NULL,
                PRIMARY KEY(scope, kind, source_id, keyword)
            )""")
            if not has_source_table:
                conn.execute("""INSERT OR IGNORE INTO quote_sources
                    SELECT DISTINCT scope, 'message', source_id, keyword FROM quotes WHERE source_id<>''""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, scope: str, keyword: str, quotes: list[Quote], collector_id: str,
            source_keys: list[tuple[str, str]] | None = None) -> tuple[int, int]:
        return self.add_many(scope, {keyword: quotes}, collector_id, source_keys)[keyword]

    @staticmethod
    def _canonical(conn, keyword: str) -> str:
        row = conn.execute("SELECT keyword FROM quote_aliases WHERE alias=?", (keyword,)).fetchone()
        return row[0] if row else keyword

    def configure_aliases(self, aliases: dict[str, str]) -> dict[str, int]:
        """Apply validated groups and merge historical rows atomically by scope."""
        moved = duplicates = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = dict(conn.execute("SELECT alias, keyword FROM quote_aliases").fetchall())
            if current == aliases:
                return {"moved": 0, "duplicates": 0}
            for alias, main in aliases.items():
                if alias == main:
                    continue
                duplicates += conn.execute("""DELETE FROM quotes WHERE keyword=? AND EXISTS (
                    SELECT 1 FROM quotes AS target WHERE target.scope=quotes.scope
                    AND target.keyword=? AND target.body=quotes.body)""", (alias, main)).rowcount
                moved += conn.execute("UPDATE quotes SET keyword=? WHERE keyword=?", (main, alias)).rowcount
                conn.execute("""INSERT OR IGNORE INTO quote_sources
                    SELECT scope, kind, source_id, ? FROM quote_sources WHERE keyword=?""", (main, alias))
                conn.execute("DELETE FROM quote_sources WHERE keyword=?", (alias,))
            conn.execute("DELETE FROM quote_aliases")
            conn.executemany("INSERT INTO quote_aliases VALUES (?, ?)", aliases.items())
        return {"moved": moved, "duplicates": duplicates}

    @staticmethod
    def _insert_quotes(conn, scope: str, keyword: str, quotes: list[Quote], collector_id: str) -> tuple[int, int]:
        added = duplicates = 0
        for quote in quotes:
            body = clean_text(quote.body)
            if not body:
                continue
            cursor = conn.execute("""INSERT OR IGNORE INTO quotes
                (scope, keyword, body, author_id, author_name, source_id, collector_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (scope, keyword, body, quote.author_id, quote.author_name, quote.source_id, collector_id))
            if cursor.rowcount:
                added += 1
            else:
                duplicates += 1
        return added, duplicates

    def add_many(self, scope: str, groups: dict[str, list[Quote]], collector_id: str,
                 source_keys: list[tuple[str, str]] | None = None,
                 source_libraries: dict[str, set[str]] | None = None,
                 source_triggers: dict[str, set[str]] | None = None) -> dict[str, tuple[int, int]]:
        counts = {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO quote_sessions(scope) VALUES (?)", (scope,))
            for original_keyword, quotes in groups.items():
                keyword = self._canonical(conn, original_keyword)
                counts[original_keyword] = self._insert_quotes(conn, scope, keyword, quotes, collector_id)
                for kind, source_id in source_keys or []:
                    conn.execute("INSERT OR IGNORE INTO quote_sources VALUES (?, ?, ?, ?)",
                                 (scope, kind, source_id, keyword))
                for library in (source_libraries or {}).get(original_keyword, set()):
                    conn.execute("INSERT OR IGNORE INTO quote_sources VALUES (?, 'library', ?, ?)",
                                 (scope, library, keyword))
                for trigger in (source_triggers or {}).get(original_keyword, set()):
                    conn.execute("INSERT OR IGNORE INTO quote_sources VALUES (?, 'trigger', ?, ?)",
                                 (scope, trigger, keyword))
        return counts

    def remember_source(self, scope: str, keyword: str, source_keys: list[tuple[str, str]]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            keyword = self._canonical(conn, keyword)
            conn.executemany("INSERT OR IGNORE INTO quote_sources VALUES (?, ?, ?, ?)",
                             [(scope, kind, source_id, keyword) for kind, source_id in source_keys])

    def source_keywords(self, scope: str, source_keys: list[tuple[str, str]]) -> set[str]:
        keywords = set()
        with self._connect() as conn:
            for kind, source_id in source_keys:
                rows = conn.execute("SELECT keyword FROM quote_sources WHERE scope=? AND kind=? AND source_id=?",
                                    (scope, kind, source_id))
                keywords.update(row[0] for row in rows)
        return keywords

    def search(self, scope: str, keyword: str, page: int, page_size: int, *,
               exact_only: bool = False, triggers: tuple[str, ...] = ()) -> tuple[list[StoredQuote], int]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            keyword = self._canonical(conn, keyword)
            # A main entry takes priority over a trigger alias. Aliases point
            # to the same rows, so multiple triggers never copy the quote data.
            exact = conn.execute("SELECT 1 FROM quotes WHERE scope=? AND keyword=? LIMIT 1", (scope, keyword)).fetchone()
            alias_where = "scope=? AND keyword IN (SELECT keyword FROM quote_sources WHERE scope=? AND kind='trigger' AND source_id=?)"
            if exact:
                where, args = "scope=? AND keyword=?", (scope, keyword)
            else:
                where, args = "scope=? AND keyword=?", (scope, keyword)
                for alias in dict.fromkeys((*triggers, keyword)):
                    # A full original trigger can differ from the main ID and
                    # still point to that person's existing canonical rows.
                    canonical = self._canonical(conn, alias)
                    if canonical != alias and conn.execute(
                        "SELECT 1 FROM quotes WHERE scope=? AND keyword=? LIMIT 1", (scope, canonical)
                    ).fetchone():
                        where, args = "scope=? AND keyword=?", (scope, canonical)
                        break
                    alias_args = (scope, scope, alias)
                    if conn.execute(f"SELECT 1 FROM quotes WHERE {alias_where} LIMIT 1", alias_args).fetchone():
                        where, args = alias_where, alias_args
                        break
                else:
                    if not exact_only:
                        # Literal substring fallback; '%' and '_' are not wildcards.
                        where, args = "scope=? AND (instr(keyword, ?)>0 OR instr(body, ?)>0)", (scope, keyword, keyword)
            count = conn.execute(f"SELECT COUNT(*) FROM quotes WHERE {where}", args).fetchone()[0]
            rows = conn.execute(f"SELECT id, keyword, body FROM quotes WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                                (*args, page_size, (page - 1) * page_size)).fetchall()
        return [StoredQuote(**dict(row)) for row in rows], count

    def delete(self, scope: str, keyword: str, body: str) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            keyword = self._canonical(conn, keyword)
            return conn.execute("DELETE FROM quotes WHERE scope=? AND keyword=? AND body=?",
                                (scope, keyword, clean_text(body))).rowcount

    @staticmethod
    def _web_row(row) -> dict:
        result = dict(row)
        version_data = [result[key] for key in ("id", "scope", "keyword", "body")]
        result["version"] = hashlib.sha256(json.dumps(version_data, ensure_ascii=False).encode()).hexdigest()
        return result

    def web_overview(self) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN")
            counts = dict(conn.execute("""SELECT COUNT(*) AS quotes,
                COUNT(DISTINCT keyword) AS keywords FROM quotes""").fetchone())
            scopes = [dict(row) for row in conn.execute("""SELECT s.scope, COUNT(q.id) AS count
                FROM quote_sessions s LEFT JOIN quotes q ON q.scope=s.scope
                GROUP BY s.scope ORDER BY count DESC, s.scope""")]
            keywords = [dict(row) for row in conn.execute("""SELECT keyword, COUNT(*) AS count
                FROM quotes GROUP BY keyword ORDER BY keyword""")]
        counts["scopes"] = len(scopes)
        return {"stats": counts, "scopes": scopes, "keywords": keywords}

    def web_list(self, *, scope: str = "", keyword: str = "", query: str = "",
                 page: int = 1, page_size: int = 20) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN")
            clauses, args = [], []
            if scope:
                clauses.append("scope=?")
                args.append(scope)
            if keyword:
                clauses.append("keyword=?")
                args.append(self._canonical(conn, keyword))
            if query:
                clauses.append("(instr(body, ?)>0 OR instr(keyword, ?)>0 OR keyword IN (SELECT keyword FROM quote_aliases WHERE alias=?))")
                args.extend([query, query, query])
            where = " AND ".join(clauses) or "1=1"
            total = conn.execute(f"SELECT COUNT(*) FROM quotes WHERE {where}", args).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM quotes WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                                (*args, page_size, (page - 1) * page_size)).fetchall()
            items = [self._web_row(row) for row in rows]
            for item in items:
                item["source_libraries"] = [row[0] for row in conn.execute(
                    "SELECT source_id FROM quote_sources WHERE scope=? AND kind='library' AND keyword=? ORDER BY source_id",
                    (item["scope"], item["keyword"]))]
                item["source_triggers"] = [row[0] for row in conn.execute(
                    "SELECT source_id FROM quote_sources WHERE scope=? AND kind='trigger' AND keyword=? ORDER BY source_id",
                    (item["scope"], item["keyword"]))]
        return {"items": items, "total": total,
                "page": page, "page_size": page_size}

    def web_save(self, *, scope: str, keyword: str, body: str, collector_id: str,
                 quote_id: int | None = None, version: str = "") -> dict:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                keyword = self._canonical(conn, keyword)
                if quote_id is None:
                    known = conn.execute("SELECT 1 FROM quote_sessions WHERE scope=?", (scope,)).fetchone()
                    if not known:
                        raise UserError("请选择已有会话。首次使用时，请先在 QQ 中添加一条语录。")
                    cursor = conn.execute("""INSERT INTO quotes(scope, keyword, body, collector_id)
                        VALUES (?, ?, ?, ?)""", (scope, keyword, body, collector_id))
                    quote_id = cursor.lastrowid
                else:
                    row = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
                    if row is None:
                        raise QuoteNotFound("这条语录已被删除，请刷新列表。")
                    if self._web_row(row)["version"] != version:
                        raise QuoteConflict("这条语录已在其他位置修改，请刷新后重新编辑。")
                    if row["scope"] != scope:
                        raise UserError("编辑时不能改变语录的所属会话。")
                    conn.execute("UPDATE quotes SET keyword=?, body=? WHERE id=?", (keyword, body, quote_id))
                return self._web_row(conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise QuoteConflict("这个会话的该关键词下，已经存在相同语录。") from exc

    def web_delete(self, quote_id: int, version: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
            if row is None:
                raise QuoteNotFound("这条语录已被删除，请刷新列表。")
            if self._web_row(row)["version"] != version:
                raise QuoteConflict("这条语录已在其他位置修改，请刷新后重新确认删除。")
            conn.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
