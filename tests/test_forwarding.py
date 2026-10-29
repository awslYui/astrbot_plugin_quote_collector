from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_quote_collector.core import UserError
from astrbot_plugin_quote_collector.forwarding import ForwardReader, contains_forward, reply_id, segments


def text(value):
    return {"type": "text", "data": {"text": value}}


def forward(resource):
    return {"type": "forward", "data": {"id": resource}}


def node(*message):
    return {"type": "node", "data": {"user_id": "123", "nickname": "原作者", "content": list(message)}}


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_napcat_response(self):
        api = AsyncMock(return_value={"messages": [
            {"sender": {"user_id": 123, "nickname": "原作者"}, "message": [text("第一条\n第二行")]},
            {"sender": {"user_id": 124}, "content": "第二条"},
        ]})
        report = await ForwardReader(api).read([forward("resource")])
        self.assertEqual([quote.body for quote in report.quotes], ["第一条\n第二行", "第二条"])
        self.assertEqual(report.quotes[0].author_name, "原作者")
        api.assert_awaited_once_with("get_forward_msg", id="resource")

    async def test_onebot_standard_enveloped_response(self):
        api = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {
            "message": [node(text("现成的语录"))]}})
        report = await ForwardReader(api).read([forward("r")])
        self.assertEqual(report.quotes[0].body, "现成的语录")

    async def test_recursive_forward_and_cycle(self):
        async def api(action, **params):
            return {"message": [node(text("一条")), node(forward("r")), node(forward("s"))]} if params["id"] == "r" else {"messages": [node(text("嵌套语录"))]}
        report = await ForwardReader(api).read([forward("r")])
        self.assertEqual([quote.body for quote in report.quotes], ["一条", "嵌套语录"])

    async def test_referenced_node_uses_get_msg(self):
        api = AsyncMock(return_value={"sender": {"user_id": 123}, "message": [text("引用节点")]})
        report = await ForwardReader(api).read([{"type": "node", "data": {"id": "-123"}}])
        self.assertEqual(report.quotes[0].body, "引用节点")
        api.assert_awaited_once_with("get_msg", message_id=-123)

    async def test_plain_reply_is_one_quote_and_skips_reply_metadata(self):
        report = await ForwardReader(AsyncMock()).read([
            {"type": "reply", "data": {"id": "1"}}, text("这才是被引用的内容")], allow_plain=True)
        self.assertEqual([quote.body for quote in report.quotes], ["这才是被引用的内容"])

    async def test_forward_envelope_text_is_not_a_quote(self):
        api = AsyncMock(return_value={"messages": [node(text("真正的语录"))]})
        report = await ForwardReader(api).read([text("聊天记录"), forward("r")])
        self.assertEqual([quote.body for quote in report.quotes], ["真正的语录"])

    async def test_media_and_empty_nodes_are_reported(self):
        image = {"type": "image", "data": {"file": "temp.jpg"}}
        report = await ForwardReader(AsyncMock()).read([node(image), node(text("文字"), image), node(text(" "))])
        self.assertEqual([quote.body for quote in report.quotes], ["文字"])
        self.assertEqual((report.skipped, report.media), (2, 2))

    async def test_face_and_at_are_text(self):
        report = await ForwardReader(AsyncMock()).read([node(
            text("你好"), {"type": "face", "data": {"id": "14"}}, {"type": "at", "data": {"qq": "all"}})])
        self.assertEqual(report.quotes[0].body, "你好[表情:14]@全体成员")

    async def test_optional_header_filter(self):
        report = await ForwardReader(AsyncMock(), ignore_patterns=[re.compile(r"^共找到\d+条")]).read([
            node(text("共找到2条")), node(text("语录"))])
        self.assertEqual([quote.body for quote in report.quotes], ["语录"])

    async def test_limit_aborts_instead_of_silently_truncating(self):
        with self.assertRaises(UserError):
            await ForwardReader(AsyncMock(), limit=1).read([node(text("1")), node(text("2"))])

    async def test_api_failure_does_not_discard_other_nodes(self):
        api = AsyncMock(side_effect=TimeoutError)
        report = await ForwardReader(api).read([node(text("仍能读到")), forward("expired")])
        self.assertEqual((report.quotes[0].body, report.errors), ("仍能读到", 1))

    async def test_failed_api_envelope_still_keeps_readable_nodes(self):
        api = AsyncMock(return_value={"status": "failed", "retcode": 1200, "data": None})
        report = await ForwardReader(api).read([node(text("仍能读到")), forward("expired")])
        self.assertEqual((report.quotes[0].body, report.errors), ("仍能读到", 1))

    async def test_api_budget_failure_is_not_treated_as_partial_import(self):
        api = AsyncMock(return_value={"messages": []})
        with self.assertRaises(UserError):
            await ForwardReader(api).read([forward(str(number)) for number in range(129)])

    async def test_malformed_nodes_and_responses(self):
        api = AsyncMock(return_value={"message": "bad"})
        report = await ForwardReader(api).read([forward("r"), {"type": "node", "data": None}])
        self.assertEqual((report.errors, report.skipped), (1, 1))

    async def test_json_and_xml_forward_cards(self):
        for card in (
            {"type": "json", "data": {"data": '{"app":"com.tencent.multimsg","meta":{"detail":{"resid":"r"}}}'}},
            {"type": "xml", "data": {"data": '<msg m_resid="r" serviceID="35"/>'}},
        ):
            with self.subTest(card=card):
                self.assertTrue(contains_forward([card]))
                api = AsyncMock(return_value={"message": [node(text("卡片语录"))]})
                report = await ForwardReader(api).read([card])
                self.assertEqual(report.quotes[0].body, "卡片语录")

    async def test_cq_escaping_does_not_interpret_literal_markup(self):
        parsed = segments("&#91;CQ:forward,id=fake&#93;[CQ:forward,id=real&#44;id]&amp;")
        self.assertEqual(parsed[0], text("[CQ:forward,id=fake]"))
        self.assertEqual(parsed[1], forward("real,id"))
        self.assertEqual(parsed[2], text("&"))

    async def test_reply_id_extraction(self):
        self.assertEqual(reply_id("[CQ:reply,id=-42]添加 a"), "-42")
        self.assertIsNone(reply_id([text("普通文本")]))
