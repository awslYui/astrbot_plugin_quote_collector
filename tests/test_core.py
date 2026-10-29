from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_quote_collector.core import QueryTracker, UserError, can_delete, parse_command
from astrbot_plugin_quote_collector.store import Quote, QuoteStore


class CommandTests(unittest.TestCase):
    def test_inline_body_preserves_spaces_newlines_and_hash(self):
        command = parse_command("/添加 嘉然#今天 好开心\n第二行#仍是语录")
        self.assertEqual((command.keyword, command.body), ("嘉然", "今天 好开心\n第二行#仍是语录"))

    def test_reply_add_and_import(self):
        self.assertIsNone(parse_command("添加 嘉然").body)
        self.assertEqual(parse_command("偷 嘉然").action, "偷")

    def test_pagination_and_fullwidth_slash(self):
        command = parse_command("／查 两个 字#2")
        self.assertEqual((command.keyword, command.page), ("两个 字", 2))

    def test_ordinary_messages_are_ignored(self):
        for text in ("查找一下", "添加好友", "他说 查 嘉然", "删除文件", ""):
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

    def test_empty_or_invalid_commands(self):
        for text in ("查", "添加", "删除", "偷", "添加 a#", "添加 a\nb", "查 a#0", "查 a#x", "删除 a", "偷 a#b"):
            with self.subTest(text=text), self.assertRaises(UserError):
                parse_command(text)

    def test_delete_is_exact_and_keeps_extra_hash(self):
        command = parse_command("删除 a#b#c")
        self.assertEqual((command.keyword, command.body), ("a", "b#c"))

    def test_admin_sources(self):
        self.assertTrue(can_delete(True, "1", "member", False, set()))
        self.assertTrue(can_delete(False, "1", "member", False, {"1"}))
        self.assertTrue(can_delete(False, "1", "admin", True, set()))
        self.assertTrue(can_delete(False, "1", "owner", True, set()))
        self.assertFalse(can_delete(False, "1", "member", True, set()))
        self.assertFalse(can_delete(False, "1", "owner", False, set()))


class TrackerTests(unittest.TestCase):
    def test_single_query_and_consumption(self):
        tracker = QueryTracker()
        tracker.add("g", "1", "嘉然")
        query = tracker.claim("g")
        self.assertEqual(query.keyword, "嘉然")
        self.assertIsNone(tracker.claim("g"))
        tracker.finish(query, True)
        self.assertIsNone(tracker.claim("g"))

    def test_concurrent_keywords_require_explicit_reply(self):
        tracker = QueryTracker()
        tracker.add("g", "1", "嘉然")
        tracker.add("g", "2", "贝拉")
        self.assertIsNone(tracker.claim("g"))
        self.assertEqual(tracker.claim("g", "1").keyword, "嘉然")
        self.assertIsNone(tracker.claim("g", "unknown"))

    def test_groups_and_timeout_are_isolated(self):
        with patch("astrbot_plugin_quote_collector.core.time.monotonic", return_value=100):
            tracker = QueryTracker(ttl=5)
            tracker.add("g1", "1", "嘉然")
            self.assertIsNone(tracker.claim("g2"))
        with patch("astrbot_plugin_quote_collector.core.time.monotonic", return_value=106):
            self.assertIsNone(tracker.claim("g1"))

    def test_failure_releases_query_for_retry(self):
        tracker = QueryTracker()
        tracker.add("g", "1", "a")
        query = tracker.claim("g")
        tracker.finish(query, False)
        self.assertIsNotNone(tracker.claim("g"))

    def test_repeated_keyword_does_not_create_ambiguity(self):
        tracker = QueryTracker()
        tracker.add("g", "1", "a")
        tracker.add("g", "2", "a")
        query = tracker.claim("g")
        self.assertIsNone(tracker.claim("g"))
        tracker.finish(query, True)
        self.assertIsNone(tracker.claim("g"))

    def test_tracker_is_bounded(self):
        tracker = QueryTracker(max_pending=2)
        for number in range(3):
            tracker.add("g", str(number), str(number))
        self.assertIsNone(tracker.claim("g", "0"))
        self.assertIsNotNone(tracker.claim("g", "2"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quotes.db"
        self.store = QuoteStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_deduplication_and_persistence(self):
        result = self.store.add("g", "a", [Quote("b\r\nc"), Quote("b\nc"), Quote("d")], "1")
        self.assertEqual(result, (2, 1))
        quotes, count = QuoteStore(self.path).search("g", "a", 1, 40)
        self.assertEqual(count, 2)
        self.assertEqual([quote.body for quote in quotes], ["b\nc", "d"])

    def test_scope_isolation_for_read_and_delete(self):
        self.store.add("g1", "a", [Quote("b")], "1")
        self.assertEqual(self.store.search("g2", "a", 1, 40)[1], 0)
        self.assertEqual(self.store.delete("g2", "a", "b"), 0)
        self.assertEqual(self.store.search("g1", "a", 1, 40)[1], 1)

    def test_exact_keyword_has_priority_over_body_search(self):
        self.store.add("g", "a", [Quote("b")], "1")
        self.store.add("g", "other", [Quote("a quote")], "1")
        rows, count = self.store.search("g", "a", 1, 40)
        self.assertEqual((count, rows[0].body), (1, "b"))
        self.assertEqual(self.store.search("g", "quote", 1, 40)[1], 1)

    def test_sql_literals_and_exact_delete(self):
        self.store.add("g", "a", [Quote("100% okay"), Quote("1000 okay"), Quote("x_y")], "1")
        self.assertEqual(self.store.search("g", "%", 1, 40)[1], 1)
        self.assertEqual(self.store.search("g", "_", 1, 40)[1], 1)
        self.assertEqual(self.store.search("g", "' OR 1=1 --", 1, 40)[1], 0)
        self.assertEqual(self.store.delete("g", "a", "100%"), 0)
        self.assertEqual(self.store.delete("g", "a", "100% okay"), 1)

    def test_trigger_aliases_share_the_canonical_rows_after_restart(self):
        self.store.add_many("g", {"123456789": [Quote("第一条"), Quote("第二条")]}, "1",
                            source_triggers={"123456789": {"来点某人", "换个触发词"}})
        store = QuoteStore(self.path)
        canonical, total = store.search("g", "123456789", 1, 40)
        for trigger in ("来点某人", "换个触发词"):
            with self.subTest(trigger=trigger):
                aliases, count = store.search("g", trigger, 1, 40)
                self.assertEqual((aliases, count), (canonical, total))
        self.assertEqual(store.web_overview()["stats"]["keywords"], 1)

    def test_exact_main_entry_has_priority_over_another_entrys_trigger(self):
        self.store.add_many("g", {"人物甲": [Quote("甲的语录")]}, "1",
                            source_triggers={"人物甲": {"人物乙"}})
        self.store.add("g", "人物乙", [Quote("乙的语录")], "1")
        rows, total = self.store.search("g", "人物乙", 1, 40)
        self.assertEqual((total, [row.keyword for row in rows]), (1, ["人物乙"]))

    def test_trigger_aliases_respect_scope_and_do_not_duplicate_shared_targets(self):
        self.store.add_many("g1", {"甲": [Quote("甲一"), Quote("甲二")], "乙": [Quote("乙一")]}, "1",
                            source_triggers={"甲": {"共同触发词"}, "乙": {"共同触发词"}})
        self.store.add_many("g2", {"丙": [Quote("丙的语录")]}, "1",
                            source_triggers={"丙": {"共同触发词"}})
        rows, total = self.store.search("g1", "共同触发词", 2, 2)
        self.assertEqual((total, [row.keyword for row in rows]), (3, ["乙"]))
        self.assertEqual(self.store.search("g3", "共同触发词", 1, 40)[1], 0)

    def test_deleted_alias_targets_do_not_hide_normal_substring_results(self):
        self.store.add_many("g", {"甲": [Quote("甲的语录")]}, "1",
                            source_triggers={"甲": {"来点甲"}})
        self.store.delete("g", "甲", "甲的语录")
        self.store.add("g", "其他", [Quote("这段正文包含来点甲")], "1")
        rows, total = self.store.search("g", "来点甲", 1, 40)
        self.assertEqual((total, rows[0].keyword), (1, "其他"))

    def test_pagination(self):
        self.store.add("g", "a", [Quote(str(i)) for i in range(5)], "1")
        rows, total = self.store.search("g", "a", 2, 2)
        self.assertEqual((total, [row.body for row in rows]), (5, ["2", "3"]))

    def test_concurrent_imports_remain_unique(self):
        def add(_):
            return self.store.add("g", "a", [Quote("b")], "1")[0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            counts = list(pool.map(add, range(12)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(self.store.search("g", "a", 1, 40)[1], 1)
