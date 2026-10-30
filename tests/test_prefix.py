"""Verify prefixed requests and that bare names never activate quote output."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import Bot, Event, forward, plugin_module, text
from astrbot_plugin_quote_collector.catalog import parse_catalog
from astrbot_plugin_quote_collector.core import UserError, parse_command
from astrbot_plugin_quote_collector.forwarding import Extraction
from astrbot_plugin_quote_collector.replica import keyword_hint, plan_replica
from astrbot_plugin_quote_collector.store import Quote


class PrefixCommandTests(unittest.TestCase):
    def test_prefix_accepts_adjacent_names_spaces_and_slashes(self):
        for value, keyword, page in (("来点示例人物", "示例人物", 1), ("来点 demo", "demo", 1),
                                     ("/来点示例昵称", "示例昵称", 1), ("／来点 两个 字", "两个 字", 1)):
            with self.subTest(value=value):
                command = parse_command(value)
                self.assertEqual((command.action, command.keyword, command.page), ("来点", keyword, page))
                self.assertEqual(command.trigger, "来点" + keyword)

    def test_prefix_is_removed_exactly_once(self):
        command = parse_command("来点来点示例人物")
        self.assertEqual(command.keyword, "来点示例人物")

    def test_bare_names_aliases_and_embedded_requests_are_not_commands(self):
        for value in ("示例人物", "demo", "示例昵称", "/示例人物", "有人说来点示例人物", "查找示例人物"):
            with self.subTest(value=value):
                self.assertIsNone(parse_command(value))

    def test_missing_keyword_or_invalid_page_is_rejected(self):
        for value in ("来点", "来点 ", "来点#2", "来点示例人物#0", "来点示例人物#x", "来点示例人物##2",
                      "来点示例人物#1", "来点示例人物#2", "来点示例人物#1000000", "来点示例人物\n第二行"):
            with self.subTest(value=value), self.assertRaises(UserError):
                parse_command(value)

    def test_source_query_hint_understands_prefix_but_not_bare_names(self):
        self.assertEqual(keyword_hint("来点示例人物"), "示例人物")
        self.assertEqual(keyword_hint("/来点示例人物"), "示例人物")
        self.assertIsNone(keyword_hint("来点示例人物#2"))
        self.assertIsNone(keyword_hint("示例人物"))

    def test_quote_prose_starting_with_prefix_is_not_removed_as_metadata(self):
        plan = plan_replica(Extraction([Quote("来点勇气吧")]), remembered={"示例人物"})
        self.assertEqual(plan.groups["示例人物"][0].body, "来点勇气吧")

    def test_catalog_can_follow_a_prefixed_query_header(self):
        source = "来点示例昵称\n主库：示例人物\n触发词：来点示例昵称\n全部文本：1 条\n1. ‘原文’"
        plan = parse_catalog([Quote(source)])
        self.assertEqual([quote.body for quote in plan.groups["示例人物"]], ["原文"])


class PrefixWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = plugin_module.QuoteCollectorPlugin(object(), {"keyword_aliases": ["示例人物#demo#示例昵称"]})
        self.bot = Bot()

    def tearDown(self):
        self.temp.cleanup()

    async def dispatch(self, value, **kwargs):
        event = Event(self.bot, [text(value)] if isinstance(value, str) else value, **kwargs)
        await self.plugin.on_message(event)
        return event

    async def test_prefixed_main_and_aliases_return_the_same_quotes(self):
        await self.dispatch("添加 示例人物#属于这个人的原文")
        for name in ("示例人物", "demo", "示例昵称"):
            result = await self.dispatch("来点" + name)
            self.assertEqual(result.sent, ["属于这个人的原文"])
            self.assertTrue(result.stopped)
            self.assertFalse(result.llm)
            self.assertEqual(self.bot.calls, [])

    async def test_bare_names_do_not_activate_filter_reply_or_pending_collection(self):
        self.plugin.config["source_bot_qqs"] = ["888"]
        self.plugin.refresh_config()
        await self.dispatch("添加 示例人物#已有语录")
        self.bot.calls.clear()
        handler_filter = plugin_module.QuoteEventFilter()
        for name in ("示例人物", "demo", "示例昵称"):
            event = Event(self.bot, [text(name)])
            self.assertFalse(handler_filter.filter(event, {}))
            await self.plugin.on_message(event)
            self.assertFalse(event.sent)
            self.assertFalse(event.stopped)
            self.assertTrue(event.llm)
            self.assertIsNone(self.plugin.tracker.match(self.plugin._scope(event)))
        self.assertEqual(self.bot.calls, [])

    async def test_prefix_inside_quoted_message_does_not_activate_bare_reply(self):
        event = await self.dispatch([{"type": "reply", "data": {"id": 50, "message": [text("来点示例人物")]}}, text("demo")])
        self.assertFalse(event.sent)
        self.assertFalse(event.stopped)
        self.assertEqual(self.bot.calls, [])

    async def test_prefix_activates_filter_but_embedded_prose_does_not(self):
        handler_filter = plugin_module.QuoteEventFilter()
        for value in ("来点示例人物", "/来点demo", "来点 示例昵称"):
            self.assertTrue(handler_filter.filter(Event(self.bot, [text(value)]), {}))
        self.assertFalse(handler_filter.filter(Event(self.bot, [text("他说来点示例人物")]), {}))

    async def test_prefixed_request_does_not_return_other_people_from_fuzzy_text(self):
        await self.dispatch("添加 其他人物#正文里提到了未收录人物")
        await self.dispatch("添加 未收录人物甲#只是名字包含相同文字")
        result = await self.dispatch("来点未收录人物")
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(result.sent, [])
        self.assertTrue(result.stopped)
        self.assertFalse(result.llm)

    async def test_exact_main_entry_has_priority_over_another_original_trigger(self):
        event = await self.dispatch("添加 示例人物#该人物的语录")
        self.plugin.store.add_many(self.plugin._scope(event), {"另一人物": [Quote("别人的语录")]}, "1",
                                   source_triggers={"另一人物": {"来点示例人物"}})
        result = await self.dispatch("来点示例人物")
        self.assertEqual(result.sent, ["该人物的语录"])

    async def test_original_full_trigger_is_resolved_without_creating_new_keyword(self):
        event = Event(self.bot, [])
        self.plugin.store.add_many(self.plugin._scope(event), {"原人物ID": [Quote("原库的语录")]}, "1",
                                   source_triggers={"原人物ID": {"来点原别名"}})
        result = await self.dispatch("来点原别名")
        self.assertEqual(result.sent, ["原库的语录"])
        self.assertEqual(self.plugin.store.web_overview()["stats"]["keywords"], 1)

    async def test_prefixed_alias_supports_space_in_source_trigger(self):
        event = Event(self.bot, [])
        self.plugin.store.add_many(self.plugin._scope(event), {"原人物ID": [Quote("原文")]}, "1",
                                   source_triggers={"原人物ID": {"来点 原别名"}})
        result = await self.dispatch("来点 原别名")
        self.assertEqual(result.sent, ["原文"])

    async def test_custom_alias_containing_the_prefix_remains_an_explicit_command(self):
        aliases = {"示例人物": "示例人物", "来点特殊口令": "示例人物"}
        self.plugin.store.configure_aliases(aliases)
        event = await self.dispatch("添加 示例人物#原文")
        result = await self.dispatch("来点特殊口令")
        self.assertEqual(result.sent, ["原文"])
        self.assertEqual(self.plugin.store.search(self.plugin._scope(event), "示例人物", 1, 40)[1], 1)

    async def test_source_trigger_and_prefix_are_scoped_to_the_current_group(self):
        event = await self.dispatch("添加 示例人物#当前群的语录")
        self.plugin.store.add_many(self.plugin._scope(event), {"原人物ID": [Quote("当前群的来源语录")]}, "1",
                                   source_triggers={"原人物ID": {"来点原别名"}})
        result = await self.dispatch("来点原别名", group="200")
        self.assertEqual(result.sent, [])
        self.assertEqual(self.bot.calls, [])

    async def test_random_reply_samples_whole_library_and_ignores_old_page_size(self):
        self.plugin.config["page_size"] = 1
        self.plugin.refresh_config()
        event = Event(self.bot, [])
        self.plugin.store.add(self.plugin._scope(event), "示例人物", [Quote(f"原文 {i}") for i in range(75)], "1")
        with patch("astrbot_plugin_quote_collector.store.random.randrange", side_effect=[74, 0, 42]) as choose:
            for name, expected in (("demo", 74), ("示例人物", 0), ("示例昵称", 42)):
                result = await self.dispatch("来点" + name)
                self.assertEqual(result.sent, [f"原文 {expected}"])
            self.assertEqual([call.args for call in choose.call_args_list], [(75,), (75,), (75,)])
        self.assertEqual(self.bot.calls, [])

    async def test_prefixed_query_works_in_private_chat(self):
        await self.dispatch("添加 demo#私聊语录", group="")
        result = await self.dispatch("来点示例昵称", group="")
        self.assertEqual(result.sent, ["私聊语录"])
        self.assertEqual(self.bot.calls, [])

    async def test_local_query_switch_also_controls_prefixed_requests(self):
        await self.dispatch("添加 demo#原文")
        self.plugin.config["answer_queries"] = False
        result = await self.dispatch("来点示例人物")
        self.assertFalse(result.sent)
        self.assertTrue(result.stopped)
        self.assertFalse(result.llm)
        self.assertEqual(self.bot.calls, [])

    async def test_prefixed_query_starts_collection_under_the_main_entry(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False, notify_import=True)
        self.plugin.refresh_config()
        query = await self.dispatch("来点demo", message_id="1")
        self.assertEqual(self.plugin.tracker.match(self.plugin._scope(query)).keyword, "示例人物")
        self.bot.forwards["r"] = {"messages": [{"message": [text("原来的语录")]}]}
        result = await self.dispatch([forward("r")], sender="888")
        self.assertIn("【示例人物】收录完成：新增 1 条", result.sent[-1])

    async def test_obsolete_prefixed_page_is_silent_without_starting_collection(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False)
        self.plugin.refresh_config()
        event = await self.dispatch("来点demo#2")
        self.assertIsNone(self.plugin.tracker.match(self.plugin._scope(event)))
        self.assertEqual(event.sent, [])
        self.assertEqual(self.bot.calls, [])

    async def test_random_reply_preserves_multiline_original_without_catalog_labels(self):
        body = "第一行  保留空格\n第二行#原话‘引号’"
        await self.dispatch("添加 示例人物#" + body)
        result = await self.dispatch("来点demo")
        self.assertEqual(result.sent, [body])
        self.assertEqual(self.bot.calls, [])

    async def test_missing_queries_are_silent_in_groups_private_chats_and_aliases(self):
        for group in ("100", "200", ""):
            for command in ("来点未收录人物", "/来点demo", "查 未收录人物", "查 示例昵称"):
                with self.subTest(group=group, command=command):
                    result = await self.dispatch(command, group=group)
                    self.assertEqual(result.sent, [])
                    self.assertTrue(result.stopped)
                    self.assertFalse(result.llm)
        self.assertEqual(self.bot.calls, [])

    async def test_invalid_natural_requests_do_not_emit_usage_or_fall_back_to_llm(self):
        for command in ("来点", "/ 来点 ", "／来点demo#2", "来点#x", "来点" + "字" * 101):
            with self.subTest(command=command):
                result = await self.dispatch(command)
                self.assertEqual(result.sent, [])
                self.assertTrue(result.stopped)
                self.assertFalse(result.llm)

    async def test_natural_query_failures_are_logged_without_chat_error_messages(self):
        with patch.object(self.plugin.store, "random_quote", side_effect=RuntimeError("simulated failure")):
            with self.assertLogs("quote-tests", level="ERROR"):
                result = await self.dispatch("来点demo")
        self.assertEqual(result.sent, [])
        self.assertTrue(result.stopped)
        self.assertFalse(result.llm)

    async def test_auto_collection_is_silent_by_default_but_still_saves_quotes(self):
        self.plugin.config.update(source_bot_qqs=["888"])
        self.plugin.refresh_config()
        query = await self.dispatch("来点demo", message_id="1")
        self.bot.forwards["r"] = {"messages": [{"message": [text("原文")]}]}
        result = await self.dispatch([forward("r")], sender="888")
        self.assertEqual(query.sent, [])
        self.assertEqual(result.sent, [])
        self.assertFalse(result.llm)
        self.assertEqual(self.plugin.store.search(self.plugin._scope(query), "示例人物", 1, 40)[1], 1)

    async def test_enabled_auto_notifications_skip_duplicate_only_imports(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False, notify_import=True)
        self.plugin.refresh_config()
        self.bot.forwards["r"] = {"messages": [{"message": [text("原文")]}]}
        await self.dispatch("查 demo", message_id="1")
        first = await self.dispatch([forward("r")], sender="888", message_id="50")
        self.assertEqual(len(first.sent), 1)
        self.assertIn("新增 1 条", first.sent[0])
        await self.dispatch("查 demo", message_id="2")
        repeated = await self.dispatch([forward("r")], sender="888", message_id="51")
        self.assertEqual(repeated.sent, [])
        self.assertFalse(repeated.llm)

    async def test_auto_empty_or_unreadable_records_remain_silent_even_with_notifications(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False, notify_import=True)
        self.plugin.refresh_config()
        await self.dispatch("查 demo", message_id="1")
        self.bot.forwards["r"] = {"messages": [{"message": []}]}
        empty = await self.dispatch([forward("r")], sender="888")
        self.assertEqual(empty.sent, [])
        await self.dispatch("查 demo", message_id="2")
        with self.assertLogs("quote-tests", level="WARNING"):
            unreadable = await self.dispatch([forward("missing")], sender="888")
        self.assertEqual(unreadable.sent, [])
        self.assertFalse(unreadable.llm)
        self.assertEqual(self.plugin.store.web_list()["total"], 0)

    async def test_auto_catalog_errors_are_silent_and_write_nothing(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False, notify_import=True)
        self.plugin.refresh_config()
        await self.dispatch("查 demo", message_id="1")
        self.bot.forwards["r"] = {"messages": [{"message": [text("主库：示例人物\n全部文本：2 条\n1. ‘只有一条’")]}]}
        result = await self.dispatch([forward("r")], sender="888")
        self.assertEqual(result.sent, [])
        self.assertFalse(result.llm)
        self.assertEqual(self.plugin.store.web_list()["total"], 0)

    async def test_manual_writes_and_explicit_help_keep_their_acknowledgements(self):
        for command in ("添加 demo#原文", "添加 demo#原文", "语录帮助"):
            result = await self.dispatch(command)
            self.assertEqual(len(result.sent), 1)
        deleted = await self.dispatch("删除 demo#原文", role="admin")
        self.assertEqual(len(deleted.sent), 1)
        self.assertIn("已删除", deleted.sent[0])

    async def test_replica_can_use_original_reply_to_a_prefixed_request(self):
        self.bot.messages[101] = {"group_id": 100, "message": [text("来点demo")]}
        self.bot.messages[50] = {"group_id": 100, "message": [{"type": "reply", "data": {"id": 101}}, forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text("原来的语录")]}]}
        result = await self.dispatch([{"type": "reply", "data": {"id": 50}}, text("复刻")])
        self.assertIn("【示例人物】复刻完成：新增 1 条", result.sent[-1])
