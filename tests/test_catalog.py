import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import Bot, Event, forward, plugin_module, text
from astrbot_plugin_quote_collector.catalog import parse_catalog
from astrbot_plugin_quote_collector.core import UserError
from astrbot_plugin_quote_collector.forwarding import Extraction
from astrbot_plugin_quote_collector.replica import plan_replica
from astrbot_plugin_quote_collector.store import Quote, QuoteStore


SAMPLE = """{
主库：占位符
触发词：来点占位符
全部文本：5 条
1. ‘这么一想嘉然打击感很强啊’
2. ‘看嘉然不顺眼了’
3. ‘滚吧嘉然不回复我的’
4. ‘好想成为野狗’
5. ‘嘉然再不认知我就变成东北人’
}"""
EXPECTED = ["这么一想嘉然打击感很强啊", "看嘉然不顺眼了", "滚吧嘉然不回复我的", "好想成为野狗", "嘉然再不认知我就变成东北人"]


def catalog(body="1. ‘原文’", *, trigger="原触发词", total=1, library="原主库"):
    return f"主库：{library}\n触发词：{trigger}\n全部文本：{total} 条\n{body}"


class CatalogTests(unittest.TestCase):
    def test_exact_user_sample_extracts_five_individual_original_quotes(self):
        plan = parse_catalog([Quote(SAMPLE)])
        self.assertEqual(set(plan.groups), {"占位符"})
        self.assertEqual([quote.body for quote in plan.groups["占位符"]], EXPECTED)
        self.assertEqual(plan.libraries, {"占位符": {"占位符"}})
        self.assertEqual(plan.triggers, {"占位符": {"来点占位符"}})

    def test_replica_uses_main_library_as_keyword_instead_of_trigger_or_previous_query(self):
        plan = plan_replica(Extraction([Quote(SAMPLE)]), hints={"占位符"}, remembered={"其他查询"})
        self.assertEqual(set(plan.groups), {"占位符"})
        self.assertEqual(plan.triggers, {"占位符": {"来点占位符"}})

    def test_main_entry_is_read_dynamically_without_inference_from_quote_text(self):
        for person_id in ("123456789", "人物甲", "ID_A-01"):
            with self.subTest(person_id=person_id):
                source = catalog("1. ‘语录里提到的其他人’", library=person_id, trigger="完全无关的查询口令")
                plan = plan_replica(Extraction([Quote(source)]), hints={"另一个人"})
                self.assertEqual(set(plan.groups), {person_id})
                self.assertEqual(plan.triggers, {person_id: {"完全无关的查询口令"}})

    def test_headers_and_items_can_be_separate_qq_nodes(self):
        nodes = [Quote(line) for line in SAMPLE.splitlines() if line not in {"{", "}"}]
        plan = parse_catalog(nodes)
        self.assertEqual([quote.body for quote in plan.groups["占位符"]], EXPECTED)
        self.assertEqual(plan.headers, 3)

    def test_only_one_outer_quote_pair_is_removed(self):
        pairs = [("‘", "’"), ("“", "”"), ("'", "'"), ('"', '"'), ("「", "」"), ("『", "』")]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                plan = parse_catalog([Quote(catalog(f"1. {left}内含‘引号’和#号{right}"))])
                self.assertEqual(plan.groups["原主库"][0].body, "内含‘引号’和#号")

    def test_quoted_multiline_numbered_and_metadata_lines_remain_original_text(self):
        original = "第一行\n2. 这是正文中的编号\n触发词：这也是正文\n全部文本：99 条\n末行"
        plan = parse_catalog([Quote(catalog(f"1. ‘{original}’"))])
        self.assertEqual(plan.groups["原主库"][0].body, original)

    def test_unquoted_multiline_content_is_preserved(self):
        plan = parse_catalog([Quote(catalog("1. 第一行\n第二行  有空格"))])
        self.assertEqual(plan.groups["原主库"][0].body, "第一行\n第二行  有空格")

    def test_source_author_and_message_provenance_are_retained(self):
        original = Quote(catalog(), "123", "原作者", "original-msg")
        copied = parse_catalog([original]).groups["原主库"][0]
        self.assertEqual((copied.author_id, copied.author_name, copied.source_id), ("123", "原作者", "original-msg"))

    def test_ascii_field_separators_and_optional_outer_braces(self):
        source = catalog().replace("：", ":")
        self.assertEqual(parse_catalog([Quote(source)]).groups["原主库"][0].body, "原文")

    def test_declared_count_must_match_actual_items(self):
        for total in (0, 2):
            with self.subTest(total=total), self.assertRaisesRegex(UserError, "实际读到"):
                parse_catalog([Quote(catalog(total=total))])

    def test_numbering_must_start_at_one_and_be_contiguous(self):
        for body in ("2. ‘原文’", "0. ‘原文’", "1. ‘a’\n3. ‘b’", "1. ‘a’\n1. ‘b’"):
            with self.subTest(body=body), self.assertRaisesRegex(UserError, "编号"):
                parse_catalog([Quote(catalog(body, total=body.count("\n") + 1))])

    def test_malformed_wrapping_and_empty_items_are_rejected(self):
        for body in ("1. ‘缺少右引号", "1. ‘错误引号”", "1. ‘’"):
            with self.subTest(body=body), self.assertRaises(UserError):
                parse_catalog([Quote(catalog(body))])

    def test_missing_or_invalid_catalog_metadata_is_not_imported_as_one_quote(self):
        for source in ("主库：a\n触发词：b\n1. ‘原文’", "触发词：b\n全部文本：1 条\n1. ‘原文’",
                       catalog().replace("全部文本：1 条", "全部文本：很多条")):
            with self.subTest(source=source), self.assertRaises(UserError):
                parse_catalog([Quote(source)])

    def test_split_items_enforce_the_configured_import_limit(self):
        with self.assertRaisesRegex(UserError, "导入上限"):
            parse_catalog([Quote(SAMPLE)], limit=4)

    def test_multiple_catalog_blocks_keep_their_individual_triggers_and_libraries(self):
        source = catalog(trigger="来点甲", library="甲") + "\n" + catalog(trigger="来点乙", library="乙")
        plan = parse_catalog([Quote(source)])
        self.assertEqual(set(plan.groups), {"甲", "乙"})
        self.assertEqual(plan.libraries, {"甲": {"甲"}, "乙": {"乙"}})
        self.assertEqual(plan.triggers, {"甲": {"来点甲"}, "乙": {"来点乙"}})

    def test_limit_is_shared_across_multiple_blocks(self):
        source = catalog(trigger="来点甲") + "\n" + catalog(trigger="来点乙")
        with self.assertRaisesRegex(UserError, "导入上限"):
            parse_catalog([Quote(source)], limit=1)

    def test_unrelated_content_after_a_complete_catalog_is_rejected(self):
        with self.assertRaises(UserError):
            parse_catalog([Quote(SAMPLE), Quote("无法对应编号的说明文字")])

    def test_other_formats_keep_the_existing_replica_path(self):
        self.assertIsNone(parse_catalog([Quote("关键词：嘉然"), Quote("原文")]))

    def test_main_keyword_does_not_require_a_trigger(self):
        source = "主库：原主库\n全部文本：1 条\n1. ‘原文’"
        plan = parse_catalog([Quote(source)])
        self.assertEqual(set(plan.groups), {"原主库"})
        self.assertEqual(plan.triggers, {})

    def test_trigger_is_metadata_and_may_contain_hash(self):
        plan = parse_catalog([Quote(catalog(trigger="来点#原主库"))])
        self.assertEqual(set(plan.groups), {"原主库"})
        self.assertEqual(plan.triggers, {"原主库": {"来点#原主库"}})

    def test_two_triggers_of_one_main_keyword_do_not_create_separate_keywords(self):
        source = catalog(trigger="来点甲") + "\n" + catalog(trigger="换点甲")
        plan = parse_catalog([Quote(source)])
        self.assertEqual(set(plan.groups), {"原主库"})
        self.assertEqual(plan.triggers["原主库"], {"来点甲", "换点甲"})


class CatalogWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name))
        self.path_patch.start()
        self.plugin = plugin_module.QuoteCollectorPlugin(object(), {})
        self.bot = Bot()
        self.bot.messages[50] = {"group_id": 100, "sender": {"user_id": 888}, "message": [forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text(SAMPLE)]}]}

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    async def dispatch(self, message, **kwargs):
        event = Event(self.bot, [text(message)] if isinstance(message, str) else message, **kwargs)
        await self.plugin.on_message(event)
        return event

    async def quote_command(self, command):
        return await self.dispatch([{"type": "reply", "data": {"id": 50}}, text(command)])

    async def test_exact_sample_replication_and_second_replication(self):
        first = await self.quote_command("复刻")
        second = await self.quote_command("复刻")
        self.assertIn("【占位符】复刻完成：新增 5 条，重复 0 条", first.sent[-1])
        self.assertIn("原主库：占位符", first.sent[-1])
        self.assertIn("原触发词：来点占位符", first.sent[-1])
        self.assertIn("新增 0 条，重复 5 条", second.sent[-1])
        rows, count = self.plugin.store.search(self.plugin._scope(first), "占位符", 1, 40)
        self.assertEqual((count, [row.body for row in rows]), (5, EXPECTED))

    async def test_original_library_metadata_is_persisted_and_in_management_rows(self):
        await self.quote_command("复刻")
        store = QuoteStore(self.plugin.store.path)
        rows = store.web_list()["items"]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["source_libraries"] == ["占位符"] for row in rows))
        self.assertTrue(all(row["source_triggers"] == ["来点占位符"] for row in rows))
        self.assertEqual({row["keyword"] for row in rows}, {"占位符"})

    async def test_qq_query_returns_five_clean_nodes(self):
        await self.quote_command("复刻")
        await self.dispatch("查 占位符")
        action, params = self.bot.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual([node["data"]["content"][0]["data"]["text"] for node in params["messages"]], EXPECTED)
        self.assertTrue(all(node["data"]["nickname"].startswith("占位符 · #") for node in params["messages"]))

    async def test_querying_original_trigger_returns_the_same_main_entry(self):
        await self.quote_command("复刻")
        await self.dispatch("查 来点占位符")
        action, params = self.bot.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual([node["data"]["content"][0]["data"]["text"] for node in params["messages"]], EXPECTED)
        self.assertTrue(all(node["data"]["nickname"].startswith("占位符 · #") for node in params["messages"]))

    async def test_arbitrary_person_id_collects_different_triggers_into_one_main_entry(self):
        first = catalog("1. ‘原文’", library="123456789", trigger="口令甲")
        second = catalog("1. ‘原文’\n2. ‘第二条’", total=2, library="123456789", trigger="口令乙")
        for source in (first, second):
            self.bot.forwards["r"] = {"messages": [{"message": [text(source)]}]}
            await self.quote_command("复刻")
        overview = self.plugin.store.web_overview()
        self.assertEqual(overview["stats"], {"quotes": 2, "keywords": 1, "scopes": 1})
        self.assertEqual(overview["keywords"], [{"keyword": "123456789", "count": 2}])
        for trigger in ("口令甲", "口令乙"):
            rows, count = self.plugin.store.search(self.plugin.store.web_list()["items"][0]["scope"], trigger, 1, 40)
            self.assertEqual((count, {row.keyword for row in rows}), (2, {"123456789"}))

    async def test_count_mismatch_aborts_without_writing_any_content(self):
        self.bot.forwards["r"] = {"messages": [{"message": [text(SAMPLE.replace("全部文本：5 条", "全部文本：6 条"))]}]}
        result = await self.quote_command("复刻")
        self.assertIn("实际读到 5 条，本次未写入", result.sent[-1])
        self.assertEqual(self.plugin.store.web_list()["total"], 0)

    async def test_split_quote_count_limit_is_enforced_before_database_write(self):
        self.plugin.config["max_import_quotes"] = 4
        self.plugin.refresh_config()
        result = await self.quote_command("复刻")
        self.assertIn("导入上限 4 条，本次未写入", result.sent[-1])
        self.assertEqual(self.plugin.store.web_list()["total"], 0)

    async def test_auto_collection_uses_main_keyword_and_deduplicates_with_replication(self):
        self.plugin.config["source_bot_qqs"] = ["888"]
        self.plugin.refresh_config()
        await self.dispatch("查 占位符", message_id="1")
        source = await self.dispatch([forward("r")], sender="888", message_id="50")
        self.assertIn("【占位符】收录完成：新增 5 条", source.sent[-1])
        second = await self.quote_command("复刻")
        self.assertIn("新增 0 条，重复 5 条", second.sent[-1])
        self.assertEqual({row["keyword"] for row in self.plugin.store.web_list()["items"]}, {"占位符"})

    async def test_explicit_manual_import_can_keep_its_chosen_keyword(self):
        result = await self.quote_command("偷 自定义关键词")
        self.assertIn("【自定义关键词】收录完成：新增 5 条", result.sent[-1])
        rows = self.plugin.store.web_list()["items"]
        self.assertEqual({row["keyword"] for row in rows}, {"自定义关键词"})
        self.assertTrue(all(row["source_libraries"] == ["占位符"] for row in rows))

    async def test_inline_add_is_still_a_single_user_supplied_quote(self):
        await self.dispatch("添加 原样保存#" + SAMPLE)
        rows = self.plugin.store.web_list()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], SAMPLE)
