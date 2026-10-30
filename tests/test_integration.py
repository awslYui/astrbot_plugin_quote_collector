"""Exercise command workflows with a simulated AstrBot / OneBot boundary.

No live AstrBot instance or QQ account is used by these tests.
"""

from __future__ import annotations

import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_plugin_with_test_api():
    names = ["astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star"]
    modules = {name: types.ModuleType(name) for name in names}
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("quote-tests")
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL="all"),
        CustomFilter=type("CustomFilter", (), {}),
        custom_filter=lambda *args, **kwargs: lambda method: method)

    class Star:
        def __init__(self, context):
            self.context = context

    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=None)
    # Restore only the AstrBot boundary mocks. Rolling back all of sys.modules
    # also removes plugin dependencies imported here and creates duplicate
    # exception classes when another test imports them later.
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        return importlib.import_module("astrbot_plugin_quote_collector.main")
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


plugin_module = load_plugin_with_test_api()
from astrbot_plugin_quote_collector.catalog import parse_catalog
from astrbot_plugin_quote_collector.store import Quote


def text(value):
    return {"type": "text", "data": {"text": value}}


def forward(resource):
    return {"type": "forward", "data": {"id": resource}}


class Bot:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.forwards = {}
        self.fail_send = False

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_forward_msg":
            return self.forwards[params["id"]]
        if action == "get_msg":
            return self.messages[params["message_id"]]
        if self.fail_send:
            raise RuntimeError("simulated forward send failure")
        return {"message_id": 1000}


class Event:
    def __init__(self, bot, message, *, group="100", sender="1", role="member", message_id="10", admin=False):
        self.bot, self.group, self.sender, self.admin = bot, group, sender, admin
        self.message_obj = types.SimpleNamespace(
            message_id=message_id, message=[], raw_message={
                "message": message, "sender": {"user_id": sender, "role": role}})
        self.sent = []
        self.llm = True
        self.stopped = False

    def get_platform_name(self): return "aiocqhttp"
    def get_platform_id(self): return "qq-main"
    def get_self_id(self): return "999"
    def get_sender_id(self): return self.sender
    def get_sender_name(self): return "测试用户"
    def get_group_id(self): return self.group
    def is_admin(self): return self.admin
    def should_call_llm(self, value): self.llm = value
    def stop_event(self): self.stopped = True
    def plain_result(self, value): return value
    async def send(self, value): self.sent.append(value)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name))
        self.data_patch.start()
        self.bot = Bot()

    def tearDown(self):
        self.data_patch.stop()
        self.temp.cleanup()

    def plugin(self, **config):
        return plugin_module.QuoteCollectorPlugin(object(), config)

    async def dispatch(self, plugin, value, **kwargs):
        event = Event(self.bot, [text(value)] if isinstance(value, str) else value, **kwargs)
        await plugin.on_message(event)
        return event

    async def test_add_query_delete_workflow_and_group_member_permission(self):
        plugin = self.plugin()
        await self.dispatch(plugin, "添加 嘉然#今天好开心\n第二行")
        result = await self.dispatch(plugin, "查 嘉然")
        action, params = self.bot.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        content = params["messages"][0]["data"]["content"][0]["data"]["text"]
        self.assertEqual(parse_catalog([Quote(content)]).groups["嘉然"][0].body, "今天好开心\n第二行")
        self.assertFalse(result.llm)
        denied = await self.dispatch(plugin, "删除 嘉然#今天好开心\n第二行")
        self.assertIn("只有", denied.sent[-1])
        deleted = await self.dispatch(plugin, "删除 嘉然#今天好开心\n第二行", role="admin")
        self.assertIn("已删除", deleted.sent[-1])

    async def test_quote_reply_addition_preserves_text(self):
        plugin = self.plugin()
        self.bot.messages[50] = {"group_id": 100, "sender": {"user_id": 22}, "message": [text("引用的 原话\n另一行")]}
        result = await self.dispatch(plugin, [{"type": "reply", "data": {"id": "50"}}, text("添加 a")])
        self.assertIn("新增 1", result.sent[-1])
        rows, _ = plugin.store.search(plugin._scope(result), "a", 1, 40)
        self.assertEqual(rows[0].body, "引用的 原话\n另一行")

    async def test_manual_forward_import_deduplicates(self):
        plugin = self.plugin()
        self.bot.messages[50] = {"message": [forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text("第一条")]}, {"message": [text("第二条")]}, {"message": [text("第一条")]}]}
        request = [{"type": "reply", "data": {"id": 50}}, text("偷 a")]
        first = await self.dispatch(plugin, request)
        second = await self.dispatch(plugin, request)
        self.assertIn("新增 2 条，重复 1 条", first.sent[-1])
        self.assertIn("新增 0 条，重复 3 条", second.sent[-1])

    async def test_automatic_collection_requires_trusted_source_and_same_group(self):
        plugin = self.plugin(source_bot_qqs=["888"], answer_queries=False)
        self.bot.forwards["r"] = {"messages": [{"message": [text("自动拿到的语录")]}]}
        query = await self.dispatch(plugin, "查 a", message_id="1")
        await self.dispatch(plugin, [forward("r")], sender="777")
        await self.dispatch(plugin, [forward("r")], sender="888", group="200")
        self.assertEqual(plugin.store.search(plugin._scope(query), "a", 1, 40)[1], 0)
        imported = await self.dispatch(plugin, [forward("r")], sender="888")
        self.assertEqual(imported.sent, [])
        self.assertEqual(plugin.store.search(plugin._scope(query), "a", 1, 40)[1], 1)

    async def test_ambiguous_queries_are_not_assigned_to_wrong_keyword(self):
        plugin = self.plugin(source_bot_qqs=["888"], answer_queries=False)
        self.bot.forwards["r"] = {"messages": [{"message": [text("语录")]}]}
        query = await self.dispatch(plugin, "查 a", message_id="1")
        await self.dispatch(plugin, "查 b", message_id="2")
        await self.dispatch(plugin, [forward("r")], sender="888")
        self.assertEqual(plugin.store.search(plugin._scope(query), "a", 1, 40)[1], 0)
        await self.dispatch(plugin, [{"type": "reply", "data": {"id": 1}}, forward("r")], sender="888")
        self.assertEqual(plugin.store.search(plugin._scope(query), "a", 1, 40)[1], 1)
        self.assertEqual(plugin.store.search(plugin._scope(query), "b", 1, 40)[1], 0)

    async def test_import_over_limit_writes_nothing(self):
        plugin = self.plugin(max_import_quotes=1)
        self.bot.messages[50] = {"message": [forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text("1")]}, {"message": [text("2")]}]}
        result = await self.dispatch(plugin, [{"type": "reply", "data": {"id": 50}}, text("偷 a")])
        self.assertIn("本次未写入", result.sent[-1])
        self.assertEqual(plugin.store.search(plugin._scope(result), "a", 1, 40)[1], 0)

    async def test_cross_group_quote_cannot_import(self):
        plugin = self.plugin()
        self.bot.messages[50] = {"group_id": 200, "message": [text("另一个群的内容")]}
        result = await self.dispatch(plugin, [{"type": "reply", "data": {"id": 50}}, text("添加 a")])
        self.assertIn("当前会话", result.sent[-1])

    async def test_private_query_and_admin_deletion(self):
        plugin = self.plugin(admin_qqs=["1"])
        await self.dispatch(plugin, "添加 a#b", group="")
        await self.dispatch(plugin, "查 a", group="")
        self.assertEqual(self.bot.calls[-1][0], "send_private_forward_msg")
        result = await self.dispatch(plugin, "删除 a#b", group="")
        self.assertIn("已删除", result.sent[-1])

    async def test_group_allowlist_and_own_bot_messages(self):
        plugin = self.plugin(enabled_groups=["200"])
        ignored = await self.dispatch(plugin, "添加 a#b")
        own = await self.dispatch(plugin, "添加 a#b", group="200", sender="999")
        self.assertFalse(ignored.sent)
        self.assertFalse(own.sent)

    async def test_disabled_auto_collection(self):
        plugin = self.plugin(source_bot_qqs=["888"], auto_collect=False)
        query = await self.dispatch(plugin, "查 a")
        await self.dispatch(plugin, [forward("r")], sender="888")
        self.assertEqual(query.sent, [])
        self.assertFalse(self.bot.calls)

    async def test_forward_send_falls_back_to_literal_text(self):
        plugin = self.plugin()
        await self.dispatch(plugin, "添加 a#[CQ:at,qq=all]")
        self.bot.fail_send = True
        result = await self.dispatch(plugin, "查 a")
        self.assertIn("1. ‘[CQ:at,qq=all]’", result.sent[-1])

    async def test_quoted_author_role_cannot_grant_delete_permission(self):
        plugin = self.plugin()
        await self.dispatch(plugin, "添加 a#b")
        self.bot.messages[50] = {"sender": {"user_id": 2, "role": "owner"}, "message": [text("b")]}
        result = await self.dispatch(plugin, [{"type": "reply", "data": {"id": 50}}, text("删除 a#b")])
        self.assertIn("只有", result.sent[-1])
        self.assertEqual(plugin.store.search(plugin._scope(result), "a", 1, 40)[1], 1)

    async def test_filter_ignores_unrelated_chat_and_unmatched_forward(self):
        plugin = self.plugin(source_bot_qqs=["888"])
        handler_filter = plugin_module.QuoteEventFilter()
        for message, sender in (([text("今天好开心")], "1"), ([text("查找好友")], "1"),
                                ([forward("r")], "1"), ([forward("r")], "888")):
            self.assertFalse(handler_filter.filter(Event(self.bot, message, sender=sender), {}))
        self.assertTrue(handler_filter.filter(Event(self.bot, [text("添加 a#b")]), {}))
        await self.dispatch(plugin, "查 a")
        self.assertTrue(handler_filter.filter(Event(self.bot, [forward("r")], sender="888"), {}))

    async def test_filter_does_not_claim_before_handler_runs(self):
        plugin = self.plugin(source_bot_qqs=["888"])
        await self.dispatch(plugin, "查 a")
        event = Event(self.bot, [forward("r")], sender="888")
        handler_filter = plugin_module.QuoteEventFilter()
        self.assertTrue(handler_filter.filter(event, {}))
        self.assertTrue(handler_filter.filter(event, {}))
        self.assertIsNotNone(plugin.tracker.claim(plugin._scope(event)))

    async def test_silent_collecting_query_is_marked_handled(self):
        plugin = self.plugin(source_bot_qqs=["888"], answer_queries=False)
        event = await self.dispatch(plugin, "/查 a")
        self.assertFalse(event.sent)
        self.assertTrue(event.stopped)

    async def test_unload_clears_filter_reference(self):
        plugin = self.plugin()
        await plugin.terminate()
        self.assertFalse(plugin_module.QuoteEventFilter().filter(Event(self.bot, [text("查 a")]), {}))
