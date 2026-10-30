"""Exercise compact catalog output, capacity paging and lossless replication."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import Bot, Event, forward, plugin_module, text
from astrbot_plugin_quote_collector.catalog import parse_catalog
from astrbot_plugin_quote_collector.core import UserError
from astrbot_plugin_quote_collector.exporting import MAX_NODES, NODE_CHARS, build_catalog_page, restore_fragments
from astrbot_plugin_quote_collector.store import Quote, QuoteStore, StoredQuote


def contents(page):
    return [Quote(fragment.strip()) for fragment in page.fragments]


def original_bodies(page, limit=10000):
    return {keyword: [quote.body for quote in rows]
            for keyword, rows in parse_catalog(contents(page), limit=limit).groups.items()}


class ExportTests(unittest.TestCase):
    def page(self, bodies, **budgets):
        rows = [StoredQuote(index, "示例人物", body) for index, body in enumerate(bodies, 1)]
        return build_catalog_page(rows, lambda keyword: len(rows), 1, **budgets)

    def test_short_library_uses_the_requested_header_and_one_numbered_node(self):
        page = self.page(["第一条", "第二条", "第三条", "第四条", "第五条"])
        self.assertEqual(len(page.fragments), 1)
        self.assertEqual(page.fragments[0], "{\n主库：示例人物\n触发词：来点示例人物\n全部文本：5 条\n\n1. ‘第一条’\n2. ‘第二条’\n3. ‘第三条’\n4. ‘第四条’\n5. ‘第五条’\n\n}")

    def test_more_than_40_quotes_fit_in_one_node(self):
        bodies = [f"示例原文{index}" for index in range(300)]
        page = self.page(bodies)
        self.assertEqual((len(page.fragments), page.total, page.shown, page.pages), (1, 300, 300, 1))
        self.assertEqual(original_bodies(page)["示例人物"], bodies)

    def test_large_library_uses_few_bounded_nodes_and_retains_every_quote(self):
        bodies = [f"{index}：" + "完整文本" * 20 for index in range(1000)]
        page = self.page(bodies)
        self.assertTrue(1 < len(page.fragments) < MAX_NODES)
        self.assertTrue(all(len(fragment) <= NODE_CHARS for fragment in page.fragments))
        self.assertEqual(original_bodies(page)["示例人物"], bodies)

    def test_long_single_line_and_boundary_whitespace_round_trip(self):
        body = "正文" * 3000 + "  \n  内部换行\n" + "🙂" * 9000
        page = self.page([body])
        self.assertGreater(len(page.fragments), 1)
        self.assertEqual(original_bodies(page, limit=1)["示例人物"], [body])

    def test_multiline_headers_braces_and_numbered_text_remain_quote_contents(self):
        body = "第一行\n2. 正文里的编号\n主库：这是正文\n触发词：仍是正文\n全部文本：99 条\n}\n最后一行"
        self.assertEqual(original_bodies(self.page([body]))["示例人物"], [body])

    def test_multiline_closing_quote_cannot_end_an_item_early(self):
        body = "第一行带‘引号’\n2. 第二行仍是正文"
        self.assertEqual(original_bodies(self.page([body]))["示例人物"], [body])

    def test_ambiguous_multiline_quotes_use_a_lossless_marked_fallback(self):
        body = "一’\n二”\n三」\n四』\n五'\n末行\\n不是换行"
        page = self.page([body])
        self.assertTrue(page.fragments[0].startswith("【语录库片段 1/1】"))
        self.assertEqual(original_bodies(page)["示例人物"], [body])

    def test_original_plugins_literal_backslashes_are_not_json_decoded(self):
        source = '主库：示例人物\n触发词：来点示例人物\n全部文本：1 条\n1. "原文\\n仍是反斜杠"'
        self.assertEqual(parse_catalog([Quote(source)]).groups["示例人物"][0].body, "原文\\n仍是反斜杠")

    def test_partial_pages_keep_library_total_and_enforce_displayed_count(self):
        rows = [StoredQuote(index, "示例人物", f"原文{index}") for index in range(12)]
        all_bodies = []
        for number in range(1, 4):
            page = build_catalog_page(rows, lambda key: 12, number, max_quotes=5)
            self.assertEqual(page.pages, 3)
            self.assertIn("全部文本：12 条", page.fragments[0])
            self.assertIn(f"本页文本：{page.shown} 条", page.fragments[0])
            all_bodies.extend(original_bodies(page, limit=5)["示例人物"])
        self.assertEqual(all_bodies, [row.body for row in rows])

    def test_node_capacity_pagination_has_no_missing_or_repeated_items(self):
        rows = [StoredQuote(index, "示例人物", f"{index}:" + "文本" * 100) for index in range(30)]
        first = build_catalog_page(rows, lambda key: 30, 1, node_chars=1024, max_nodes=2)
        self.assertGreater(first.pages, 1)
        result = []
        for number in range(1, first.pages + 1):
            page = build_catalog_page(rows, lambda key: 30, number, node_chars=1024, max_nodes=2)
            self.assertLessEqual(len(page.fragments), 2)
            self.assertTrue(all(len(fragment) <= 1024 for fragment in page.fragments))
            result.extend(original_bodies(page)["示例人物"])
        self.assertEqual(result, [row.body for row in rows])

    def test_multiple_libraries_keep_separate_headers_and_numbering(self):
        rows = [StoredQuote(1, "人物甲", "甲的原文"), StoredQuote(2, "人物乙", "乙的原文")]
        page = build_catalog_page(rows, lambda key: 1, 1)
        self.assertEqual(len(page.fragments), 1)
        self.assertEqual(original_bodies(page), {"人物甲": ["甲的原文"], "人物乙": ["乙的原文"]})

    def test_mixed_group_sizes_and_long_quotes_round_trip_across_bounded_pages(self):
        rows = []
        for group in range(5):
            for index, size in enumerate((10, 400, 1500, 6000, 10000)):
                rows.append(StoredQuote(len(rows) + 1, f"人物{group}", f"原文{index}:" + "字" * size))
        first = build_catalog_page(rows, lambda key: 5, 1, max_nodes=3)
        collected = {f"人物{group}": [] for group in range(5)}
        for number in range(1, first.pages + 1):
            page = build_catalog_page(rows, lambda key: 5, number, max_nodes=3)
            self.assertLessEqual(len(page.fragments), 3)
            for keyword, bodies in original_bodies(page).items():
                collected[keyword].extend(bodies)
        self.assertEqual(collected, {keyword: [row.body for row in rows if row.keyword == keyword] for keyword in collected})

    def test_missing_reordered_or_corrupt_fragments_are_rejected(self):
        quotes = contents(self.page(["正文" * 10000]))
        self.assertGreater(len(quotes), 2)
        cases = [quotes[:-1], quotes[1:], [quotes[1], quotes[0], *quotes[2:]],
                 [Quote(quotes[0].body.replace("1/", "2/", 1)), *quotes[1:]],
                 [Quote("【语录库片段 1/1】\n坏片段")]]
        for values in cases:
            with self.subTest(parts=len(values)), self.assertRaises(UserError):
                parse_catalog(values)

    def test_complete_separate_exports_can_be_restored_together(self):
        page = self.page(["正文" * 4000])
        restored, own = restore_fragments(contents(page) + contents(page))
        self.assertEqual(len(restored), 2)
        self.assertEqual(own, {0, 1})

    def test_partial_count_mismatch_is_rejected_and_total_is_not_import_limit(self):
        source = "主库：示例人物\n全部文本：9999 条\n本页文本：1 条\n1. ‘原文’"
        self.assertEqual(parse_catalog([Quote(source)], limit=1).groups["示例人物"][0].body, "原文")
        for broken in (source.replace("本页文本：1", "本页文本：2"), source.replace("全部文本：9999", "全部文本：0")):
            with self.assertRaises(UserError):
                parse_catalog([Quote(broken)])


class ExportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = QuoteStore(Path(self.temp.name) / "quotes.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_alias_lookup_uses_canonical_library_and_scope(self):
        self.store.configure_aliases({"示例人物": "示例人物", "demo": "示例人物"})
        self.store.add("g1", "demo", [Quote("当前群的语录")], "1")
        self.store.add("g2", "示例人物", [Quote("另一个群的语录")], "1")
        page = self.store.catalog_page("g1", "demo", 1)
        self.assertEqual(original_bodies(page), {"示例人物": ["当前群的语录"]})

    def test_fuzzy_results_show_actual_library_total_and_only_matching_text(self):
        self.store.add("g", "人物甲", [Quote("命中甲"), Quote("其余的甲")], "1")
        self.store.add("g", "人物乙", [Quote("命中乙")], "1")
        page = self.store.catalog_page("g", "命中", 1)
        self.assertEqual((page.total, page.shown), (2, 2))
        self.assertIn("全部文本：2 条\n本页文本：1 条", page.fragments[0])
        self.assertEqual(original_bodies(page), {"人物甲": ["命中甲"], "人物乙": ["命中乙"]})

    def test_empty_results_and_out_of_range_pages_do_not_invent_content(self):
        self.assertEqual(self.store.catalog_page("g", "无内容", 1).fragments, [])
        self.store.add("g", "示例人物", [Quote("原文")], "1")
        page = self.store.catalog_page("g", "示例人物", 2)
        self.assertEqual((page.total, page.shown, page.pages, page.fragments), (1, 0, 1, []))


class ExportWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = plugin_module.QuoteCollectorPlugin(object(), {"page_size": 1})
        self.bot = Bot()

    def tearDown(self):
        self.temp.cleanup()

    async def dispatch(self, value, **kwargs):
        event = Event(self.bot, [text(value)] if isinstance(value, str) else value, **kwargs)
        await self.plugin.on_message(event)
        return event

    def query_nodes(self):
        return [params["messages"] for action, params in self.bot.calls if action.startswith("send_")][-1]

    async def test_catalog_shows_all_rows_while_prefix_returns_one_random_text(self):
        event = await self.dispatch("添加 示例人物#第一条")
        self.plugin.store.add(self.plugin._scope(event), "示例人物", [Quote("第二条")], "1")
        await self.dispatch("查 示例人物")
        nodes = self.query_nodes()
        page = parse_catalog([Quote(node["data"]["content"][0]["data"]["text"]) for node in nodes])
        self.assertEqual([quote.body for quote in page.groups["示例人物"]], ["第一条", "第二条"])
        self.bot.calls.clear()
        with patch("astrbot_plugin_quote_collector.store.random.randrange", return_value=1):
            result = await self.dispatch("来点示例人物")
        self.assertEqual(result.sent, ["第二条"])
        self.assertEqual(self.bot.calls, [])

    async def test_exported_long_quote_can_be_replicated_with_one_item_import_limit(self):
        body = "很长的原文" * 3000
        event = await self.dispatch("添加 示例人物#" + body)
        await self.dispatch("查 示例人物")
        nodes = self.query_nodes()
        self.assertGreater(len(nodes), 1)
        self.plugin.config["max_import_quotes"] = 1
        self.plugin.refresh_config()
        self.bot.messages[50] = {"group_id": 200, "message": [forward("own")]}
        self.bot.forwards["own"] = {"messages": nodes}
        result = await self.dispatch([{"type": "reply", "data": {"id": 50}}, text("复刻")], group="200")
        self.assertIn("新增 1 条", result.sent[-1])
        self.assertEqual(self.plugin.store.search(self.plugin._scope(result), "示例人物", 1, 40)[0][0].body, body)

    async def test_incomplete_exported_record_is_not_partially_written(self):
        await self.dispatch("添加 示例人物#" + "长文本" * 4000)
        await self.dispatch("查 示例人物")
        nodes = self.query_nodes()
        self.bot.messages[50] = {"group_id": 200, "message": [forward("broken")]}
        self.bot.forwards["broken"] = {"messages": nodes[:-1]}
        result = await self.dispatch([{"type": "reply", "data": {"id": 50}}, text("复刻")], group="200")
        self.assertIn("本次未写入", result.sent[-1])
        self.assertEqual(self.plugin.store.search(self.plugin._scope(result), "示例人物", 1, 40)[1], 0)

    async def test_compact_query_text_fallback_still_displays_all_contents(self):
        await self.dispatch("添加 示例人物#第一条")
        await self.dispatch("添加 示例人物#第二条")
        self.bot.fail_send = True
        result = await self.dispatch("查 示例人物")
        self.assertIn("全部文本：2 条", "".join(result.sent))
        self.assertIn("1. ‘第一条’", "".join(result.sent))
        self.assertIn("2. ‘第二条’", "".join(result.sent))

    async def test_compact_query_pagination_instruction_and_invalid_page(self):
        event = await self.dispatch("添加 示例人物#原文0")
        self.plugin.store.add(self.plugin._scope(event), "示例人物", [Quote(f"原文{i}") for i in range(1, 5)], "1")
        def small_page(scope, keyword, page):
            rows, count = self.plugin.store.search(scope, keyword, 1, 100)
            return build_catalog_page(rows, lambda name: count, page, max_quotes=2)
        with patch.object(self.plugin.store, "catalog_page", side_effect=small_page):
            first = await self.dispatch("查 示例人物")
            self.assertIn("下一页：查 示例人物#2", first.sent[-1])
            second = await self.dispatch("查 示例人物#2")
            page = parse_catalog([Quote(node["data"]["content"][0]["data"]["text"]) for node in self.query_nodes()])
            self.assertEqual([quote.body for quote in page.groups["示例人物"]], ["原文2", "原文3"])
            self.assertIn("本页展示 2 条", second.sent[-1])
            invalid = await self.dispatch("查 示例人物#4")
            self.assertIn("共 3 页", invalid.sent[-1])
            final = await self.dispatch("查 示例人物#3")
            self.assertEqual(final.sent, [])
            page = parse_catalog([Quote(node["data"]["content"][0]["data"]["text"]) for node in self.query_nodes()])
            self.assertEqual([quote.body for quote in page.groups["示例人物"]], ["原文4"])
