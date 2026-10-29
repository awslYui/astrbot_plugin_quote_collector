import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import Bot, Event, forward, plugin_module, text
from astrbot_plugin_quote_collector.core import UserError, parse_command
from astrbot_plugin_quote_collector.forwarding import Extraction
from astrbot_plugin_quote_collector.replica import envelope_hints, keyword_hint, plan_replica, source_keys
from astrbot_plugin_quote_collector.store import Quote


class ReplicaPlanTests(unittest.TestCase):
    def test_exact_command_and_slash(self):
        self.assertEqual(parse_command("复刻").action, "复刻")
        self.assertEqual(parse_command("/复刻").action, "复刻")
        self.assertIsNone(parse_command("复刻一下"))
        with self.assertRaises(UserError):
            parse_command("复刻 新关键词#新内容")

    def test_explicit_metadata_formats(self):
        for label in ("关键词：嘉然", "【嘉然】语录", "【嘉然】共12条语录", "嘉然的语录", "嘉然语录合集", "嘉然 · #12"):
            with self.subTest(label=label):
                self.assertEqual(keyword_hint(label), "嘉然")
        self.assertIsNone(keyword_hint("原语录插件"))
        self.assertIsNone(keyword_hint("普通人的昵称"))

    def test_header_is_removed_and_content_is_preserved(self):
        report = Extraction([Quote("关键词：嘉然"), Quote("原有  空格\n第二行#字符")])
        plan = plan_replica(report)
        self.assertEqual(plan.headers, 1)
        self.assertEqual([quote.body for quote in plan.groups["嘉然"]], ["原有  空格\n第二行#字符"])

    def test_labeled_node_names_preserve_multiple_original_keywords(self):
        report = Extraction([Quote("嘉然原文", author_name="嘉然 · #1"),
                             Quote("贝拉原文", author_name="贝拉 · #2")])
        plan = plan_replica(report)
        self.assertEqual(set(plan.groups), {"嘉然", "贝拉"})

    def test_metadata_sections_keep_their_own_keyword(self):
        report = Extraction([Quote("关键词：嘉然"), Quote("嘉然原话"),
                             Quote("关键词：贝拉"), Quote("贝拉原话")])
        plan = plan_replica(report)
        self.assertEqual(plan.groups["嘉然"][0].body, "嘉然原话")
        self.assertEqual(plan.groups["贝拉"][0].body, "贝拉原话")

    def test_structured_keyword_and_body_node(self):
        plan = plan_replica(Extraction([Quote("关键词：嘉然\n语录：今天好开心\n另一行")]))
        self.assertEqual(plan.groups["嘉然"][0].body, "今天好开心\n另一行")

    def test_ordinary_prose_is_not_stripped_as_metadata(self):
        plan = plan_replica(Extraction([Quote("这是他的语录")]), remembered={"原关键词"})
        self.assertEqual(plan.groups["原关键词"][0].body, "这是他的语录")

    def test_plain_sender_nickname_is_not_guessed_as_keyword(self):
        with self.assertRaises(UserError):
            plan_replica(Extraction([Quote("现成语录", author_name="普通人的昵称")]))

    def test_ambiguous_remembered_keywords_are_rejected(self):
        with self.assertRaises(UserError):
            plan_replica(Extraction([Quote("原文")]), remembered={"a", "b"})

    def test_conflicting_node_annotations_are_rejected(self):
        with self.assertRaises(UserError):
            plan_replica(Extraction([Quote("关键词：a\n原文", author_name="b · #1")]))

    def test_unassigned_nodes_in_mixed_record_reject_whole_plan(self):
        with self.assertRaises(UserError):
            plan_replica(Extraction([Quote("没标签"), Quote("a的内容", author_name="a · #1"),
                                    Quote("b的内容", author_name="b · #2")]))

    def test_header_only_record_cannot_create_a_quote(self):
        with self.assertRaises(UserError):
            plan_replica(Extraction([Quote("关键词：a")]))

    def test_forward_card_title_and_resource_identifier(self):
        card = {"type": "json", "data": {"data": json.dumps({"app": "com.tencent.multimsg",
            "meta": {"detail": {"resid": "r", "title": "嘉然的语录"}}}, ensure_ascii=False)}}
        self.assertEqual(envelope_hints([card]), {"嘉然"})
        self.assertEqual(set(source_keys([card], "50")), {("message", "50"), ("forward", "r")})

    def test_query_inside_record_supplies_provenance_without_becoming_quote(self):
        plan = plan_replica(Extraction([Quote("查 嘉然"), Quote("原有语录")]))
        self.assertEqual([quote.body for quote in plan.groups["嘉然"]], ["原有语录"])


class ReplicaWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name))
        self.path_patch.start()
        self.bot = Bot()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def plugin(self, **settings):
        return plugin_module.QuoteCollectorPlugin(object(), settings)

    async def dispatch(self, plugin, message, **kwargs):
        event = Event(self.bot, [text(message)] if isinstance(message, str) else message, **kwargs)
        await plugin.on_message(event)
        return event

    async def replicate(self, plugin, message_id=50, **kwargs):
        return await self.dispatch(plugin, [{"type": "reply", "data": {"id": message_id}}, text("复刻")], **kwargs)

    def record(self, resource="r", message_id=50, messages=None, **extra):
        self.bot.messages[message_id] = {"group_id": 100, "sender": {"user_id": 888},
                                          "message": [forward(resource)], **extra}
        self.bot.forwards[resource] = {"messages": messages or [{"message": [text("现成的原文")]}]}

    async def test_requires_quote_and_accepts_only_forwarded_record(self):
        plugin = self.plugin()
        no_quote = await self.dispatch(plugin, "复刻")
        self.assertIn("引用", no_quote.sent[-1])
        self.bot.messages[50] = {"message": [text("普通消息")], "group_id": 100}
        not_forward = await self.replicate(plugin)
        self.assertIn("只接受已有的合并聊天记录", not_forward.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_original_header_import_and_deduplication(self):
        plugin = self.plugin()
        self.record(messages=[{"message": [text("关键词：嘉然")]},
                              {"message": [text("原文  内容\n第二行#保持")]}])
        first = await self.replicate(plugin)
        second = await self.replicate(plugin)
        self.assertIn("复刻完成：新增 1 条", first.sent[-1])
        self.assertIn("新增 0 条，重复 1 条", second.sent[-1])
        rows, count = plugin.store.search(plugin._scope(first), "嘉然", 1, 40)
        self.assertEqual((count, rows[0].body), (1, "原文  内容\n第二行#保持"))
        self.assertFalse(first.llm)
        self.assertTrue(first.stopped)

    async def test_multiple_node_keywords_are_retained(self):
        plugin = self.plugin()
        self.record(messages=[{"sender": {"nickname": "嘉然 · #12"}, "message": [text("原文一")]},
                              {"sender": {"nickname": "贝拉 · #13"}, "message": [text("原文二")]}])
        result = await self.replicate(plugin)
        self.assertIn("新增 2 条", result.sent[-1])
        self.assertEqual({row["keyword"] for row in plugin.store.web_list()["items"]}, {"嘉然", "贝拉"})

    async def test_disabled_auto_collect_remembers_only_provenance_until_replication(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        await self.dispatch(plugin, "查 嘉然", message_id="1")
        source = await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        self.assertEqual(plugin.store.web_list()["total"], 0)
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(source.sent, [])
        self.record()
        result = await self.replicate(plugin)
        self.assertIn("【嘉然】复刻完成", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 1)

    async def test_record_keyword_association_survives_restart_and_new_message_id(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        await self.dispatch(plugin, "查 嘉然", message_id="1")
        await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        restarted = self.plugin()
        self.record(message_id=60)
        result = await self.replicate(restarted, 60)
        self.assertIn("【嘉然】复刻完成", result.sent[-1])

    async def test_existing_automatic_import_can_be_replicated_as_duplicate(self):
        plugin = self.plugin(source_bot_qqs=["888"])
        self.record()
        await self.dispatch(plugin, "查 嘉然", message_id="1")
        await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        result = await self.replicate(plugin)
        self.assertIn("新增 0 条，重复 1 条", result.sent[-1])

    async def test_local_web_edit_does_not_rewrite_original_provenance_after_restart(self):
        plugin = self.plugin(source_bot_qqs=["888"])
        self.record()
        await self.dispatch(plugin, "查 嘉然", message_id="1")
        await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        row = plugin.store.web_list()["items"][0]
        plugin.store.web_save(scope=row["scope"], keyword="本地修改", body=row["body"], collector_id="web:admin",
                              quote_id=row["id"], version=row["version"])
        restarted = self.plugin()
        result = await self.replicate(restarted)
        self.assertIn("【嘉然】复刻完成", result.sent[-1])
        self.assertEqual({row["keyword"] for row in restarted.store.web_list()["items"]}, {"本地修改", "嘉然"})

    async def test_source_query_reply_provides_exact_keyword_without_configuration(self):
        plugin = self.plugin()
        self.record(message=[{"type": "reply", "data": {"id": 101}}, forward("r")])
        self.bot.messages[101] = {"group_id": 100, "message": [text("查 嘉然")]}
        result = await self.replicate(plugin)
        self.assertIn("【嘉然】复刻完成", result.sent[-1])

    async def test_reply_to_query_in_other_group_is_rejected(self):
        plugin = self.plugin()
        self.record(message=[{"type": "reply", "data": {"id": 101}}, forward("r")])
        self.bot.messages[101] = {"group_id": 200, "message": [text("查 嘉然")]}
        result = await self.replicate(plugin)
        self.assertIn("原查询不属于当前会话", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_unrelated_recent_query_is_not_used_for_an_old_record(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        await self.dispatch(plugin, "查 贝拉", message_id="1")
        self.record()
        result = await self.replicate(plugin)
        self.assertIn("无法从这份记录确认原关键词", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_provenance_cannot_cross_group_boundaries(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        await self.dispatch(plugin, "查 嘉然", message_id="1")
        await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        self.record(group_id=200)
        result = await self.replicate(plugin, group="200")
        self.assertIn("无法从这份记录确认原关键词", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_new_content_arguments_cannot_be_used_with_replication(self):
        plugin = self.plugin()
        result = await self.dispatch(plugin, "复刻 新词#编造内容")
        self.assertIn("不填写新关键词或语录", result.sent[-1])
        self.assertFalse(self.bot.calls)
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_unreadable_nested_record_aborts_without_partial_copy(self):
        plugin = self.plugin()
        self.record(messages=[{"message": [text("关键词：嘉然")]},
                              {"message": [text("可读取原文")]}, {"message": [forward("expired")]}])
        result = await self.replicate(plugin)
        self.assertIn("部分内容读取失败，本次未复刻", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)

    async def test_filter_matches_only_the_exact_replication_verb(self):
        plugin = self.plugin()
        self.assertTrue(plugin.accepts(Event(self.bot, [text("复刻")])))
        self.assertTrue(plugin.accepts(Event(self.bot, [text("/复刻")])))
        self.assertFalse(plugin.accepts(Event(self.bot, [text("这个复刻挺好的")])))
        self.assertFalse(plugin.accepts(Event(self.bot, [text("复刻一下")])))

    async def test_ambiguous_query_record_bindings_fail_without_guessing(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        for number, keyword in enumerate(["嘉然", "贝拉"]):
            await self.dispatch(plugin, f"查 {keyword}", message_id=str(number + 1))
            await self.dispatch(plugin, [forward("r")], sender="888", message_id="50")
        self.record()
        result = await self.replicate(plugin)
        self.assertIn("无法从这份记录确认原关键词", result.sent[-1])
        self.assertEqual(plugin.store.web_list()["total"], 0)
