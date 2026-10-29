"""Exercise the actual WebUI service with isolated data and request adapters."""

import copy
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import plugin_module
from astrbot_plugin_quote_collector.store import Quote, QuoteStore
from astrbot_plugin_quote_collector.web_api import QuoteWebApi, register_web_ui, validate_settings


class Config(dict):
    def __init__(self, path, **values):
        super().__init__(values)
        self.path = path
        self.fail = False

    def save_config(self, values):
        self.clear()
        self.update(copy.deepcopy(values))
        if self.fail:
            raise OSError("simulated persistence failure")
        self.path.write_text(json.dumps(values), encoding="utf-8")


class Context:
    def __init__(self):
        self.registered_web_apis = []

    def get_config(self):
        return {"dashboard": {"username": "admin"}}

    def register_web_api(self, route, handler, methods, description):
        self.registered_web_apis.append((route, handler, methods, description))


class Request:
    def __init__(self, payload=None, *, query=None, username="admin", raw=None):
        self.username = username
        self.query = query or {}
        self.raw = raw if raw is not None else json.dumps(payload).encode()

    async def body(self):
        return self.raw

    async def json(self, default=None):
        try:
            return json.loads(self.raw)
        except (ValueError, TypeError):
            return default


class WebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with patch.object(plugin_module.StarTools, "get_data_dir", return_value=self.root):
            self.plugin = plugin_module.QuoteCollectorPlugin(Context(), Config(self.root / "settings.json"))
        self.web = types.SimpleNamespace(
            request=Request(), json_response=lambda value: {"code": 200, "data": value},
            error_response=lambda text, status_code=400: {"code": status_code, "message": text})
        self.api = QuoteWebApi(self.plugin, self.web, logging.getLogger("quote-web-tests"))
        self.scope = json.dumps(["qq-main", "999", "group", "100"], separators=(",", ":"))
        self.other_scope = json.dumps(["qq-main", "999", "group", "200"], separators=(",", ":"))
        self.plugin.store.add(self.scope, "嘉然", [Quote("原来的语录\n第二行")], "1")

    def tearDown(self):
        self.temp.cleanup()

    def row(self):
        return self.plugin.store.web_list()["items"][0]

    async def call(self, method, payload=None, **request_options):
        self.web.request = Request(payload, **request_options)
        return await getattr(self.api, method)()

    async def test_all_endpoints_require_the_dashboard_admin(self):
        for username in (None, "", "member", "ADMIN"):
            for method in ("state", "quotes", "save", "delete", "delete_many", "settings"):
                with self.subTest(username=username, method=method):
                    self.assertEqual((await self.call(method, username=username))["code"], 403)

    async def test_missing_dashboard_configuration_fails_closed(self):
        self.plugin.context.get_config = lambda: {}
        self.assertEqual((await self.call("state"))["code"], 403)

    async def test_overview_and_human_readable_scopes(self):
        response = await self.call("state")
        data = response["data"]
        self.assertEqual(data["stats"], {"quotes": 1, "keywords": 1, "scopes": 1})
        self.assertEqual(data["scopes"][0]["label"], "QQ群 100")
        self.assertTrue(data["can_save_settings"])
        self.assertTrue(data["settings"]["auto_collect"])

    async def test_list_filter_and_pagination(self):
        self.plugin.store.add(self.other_scope, "其他", [Quote("别的群")], "1")
        result = await self.call("quotes", query={"scope": self.scope, "keyword": "嘉然", "query": "第二行", "page_size": "1"})
        self.assertEqual(result["data"]["total"], 1)
        self.assertEqual(result["data"]["items"][0]["label"], "QQ群 100")
        self.assertEqual(len(result["data"]["items"][0]["version"]), 64)

    async def test_list_input_validation(self):
        for query in ({"page": "abc"}, {"page": "0"}, {"page_size": "101"}, {"query": "a" * 201}):
            with self.subTest(query=query):
                self.assertEqual((await self.call("quotes", query=query))["code"], 400)

    async def test_multiple_libraries_filter_matches_any_selected_name_with_scope_and_search(self):
        self.plugin.store.add(self.scope, "示例库甲", [Quote("共同文本甲"), Quote("其他文本")], "1")
        self.plugin.store.add(self.scope, "示例库乙", [Quote("共同文本乙")], "1")
        self.plugin.store.add(self.scope, "示例库丙", [Quote("共同文本丙")], "1")
        self.plugin.store.add(self.other_scope, "示例库甲", [Quote("共同文本其他群")], "1")
        names = json.dumps(["示例库甲", "示例库乙"], ensure_ascii=False)
        result = await self.call("quotes", query={"scope": self.scope, "keywords": names, "query": "共同", "page_size": "1"})
        self.assertEqual(result["data"]["total"], 2)
        first = result["data"]["items"][0]
        result = await self.call("quotes", query={"scope": self.scope, "keywords": names, "query": "共同", "page_size": "1", "page": "2"})
        second = result["data"]["items"][0]
        self.assertEqual({first["keyword"], second["keyword"]}, {"示例库甲", "示例库乙"})
        self.assertEqual({first["scope"], second["scope"]}, {self.scope})

    async def test_multiple_library_filter_accepts_aliases_without_duplicate_rows(self):
        self.plugin.store.configure_aliases({"嘉然": "嘉然", "示例别名": "嘉然"})
        result = await self.call("quotes", query={"keywords": json.dumps(["嘉然", "示例别名", "嘉然"])})
        self.assertEqual(result["data"]["total"], 1)
        self.assertEqual(result["data"]["items"][0]["keyword"], "嘉然")

    async def test_multiple_library_filter_is_literal_and_empty_selection_means_all(self):
        self.plugin.store.add(self.scope, "库%'_", [Quote("文字")], "1")
        selected = await self.call("quotes", query={"keywords": json.dumps(["库%'_"])})
        self.assertEqual(selected["data"]["total"], 1)
        self.assertEqual(selected["data"]["items"][0]["keyword"], "库%'_")
        self.assertEqual((await self.call("quotes", query={"keywords": "[]"}))["data"]["total"], 2)
        self.assertEqual((await self.call("quotes", query={"keywords": '["不存在"]'}))["data"]["total"], 0)

    async def test_multiple_library_filter_input_validation(self):
        for encoded in ("broken", '"库名"', "null", "{}", "[true]", "[null]", '[""]', '["a#b"]',
                        json.dumps(["a\nb"]), json.dumps(["x" * 101]), json.dumps(["x"] * 101), "x" * 64001):
            with self.subTest(encoded=encoded[:40]):
                self.assertEqual((await self.call("quotes", query={"keywords": encoded}))["code"], 400)
        self.assertEqual((await self.call("quotes", query={"keyword": "甲", "keywords": '["乙"]'}))["code"], 400)

    def batch_payload(self, rows=None):
        rows = self.plugin.store.web_list()["items"] if rows is None else rows
        return {"items": [{"id": row["id"], "version": row["version"]} for row in rows]}

    async def test_batch_delete_removes_only_selected_quotes_across_libraries_and_sessions(self):
        self.plugin.store.add(self.other_scope, "示例库", [Quote("其他会话的选中项"), Quote("保留的内容")], "1")
        rows = self.plugin.store.web_list()["items"]
        selected = [row for row in rows if row["body"] != "保留的内容"]
        result = await self.call("delete_many", self.batch_payload(selected))
        self.assertEqual(result, {"code": 200, "data": {"deleted": 2}})
        self.assertEqual([row["body"] for row in self.plugin.store.web_list()["items"]], ["保留的内容"])
        scopes = {row["scope"] for row in self.plugin.store.web_overview()["scopes"]}
        self.assertEqual(scopes, {self.scope, self.other_scope})

    async def test_batch_delete_rejects_everything_if_one_quote_was_edited(self):
        self.plugin.store.add(self.scope, "示例库", [Quote("第二条")], "1")
        payload = self.batch_payload()
        row = self.row()
        self.plugin.store.web_save(scope=row["scope"], keyword=row["keyword"], body="已经修改",
                                   collector_id="web:admin", quote_id=row["id"], version=row["version"])
        result = await self.call("delete_many", payload)
        self.assertEqual(result["code"], 409)
        self.assertEqual(self.plugin.store.web_list()["total"], 2)
        self.assertIn("未删除任何", result["message"])

    async def test_batch_delete_rejects_everything_if_one_quote_was_deleted(self):
        self.plugin.store.add(self.scope, "示例库", [Quote("第二条")], "1")
        payload = self.batch_payload()
        row = self.row()
        self.plugin.store.web_delete(row["id"], row["version"])
        result = await self.call("delete_many", payload)
        self.assertEqual(result["code"], 404)
        self.assertEqual(self.plugin.store.web_list()["total"], 1)

    async def test_batch_delete_rejects_malformed_empty_oversized_and_duplicate_selections(self):
        item = self.batch_payload()["items"][0]
        for payload in ({}, {"items": []}, {"items": {}}, {"items": [None]}, {"items": [True]},
                        {"items": [item, item]}, {"items": [item] * 501},
                        {"items": [{"id": True, "version": item["version"]}]},
                        {"items": [{"id": item["id"], "version": "bad"}]},
                        {"items": [item, {"id": -1, "version": item["version"]}]}):
            with self.subTest(keys=list(payload)):
                self.assertEqual((await self.call("delete_many", payload))["code"], 400)
                self.assertEqual(self.plugin.store.web_list()["total"], 1)

    async def test_batch_delete_rolls_back_if_the_database_write_fails_partway(self):
        self.plugin.store.add(self.scope, "示例库", [Quote("第二条")], "1")
        payload = self.batch_payload()
        failure_id = payload["items"][1]["id"]
        with self.plugin.store._connect() as conn:
            conn.execute(f"CREATE TRIGGER fail_batch BEFORE DELETE ON quotes WHEN OLD.id={failure_id} BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with self.assertLogs("quote-web-tests", level="ERROR"):
            result = await self.call("delete_many", payload)
        self.assertEqual(result["code"], 500)
        self.assertEqual(self.plugin.store.web_list()["total"], 2)

    async def test_batch_delete_accepts_the_documented_selection_limit(self):
        self.plugin.store.add(self.scope, "示例库", [Quote(f"原文 {i}") for i in range(499)], "1")
        rows = self.plugin.store.web_list(page_size=500)["items"]
        result = await self.call("delete_many", self.batch_payload(rows))
        self.assertEqual(result, {"code": 200, "data": {"deleted": 500}})
        self.assertEqual(self.plugin.store.web_list()["total"], 0)

    async def test_web_create_is_visible_to_qq_query(self):
        response = await self.call("save", {"scope": self.scope, "keyword": "新词", "body": " WebUI 添加\n第二行 "})
        self.assertEqual(response["code"], 200)
        rows, count = self.plugin.store.search(self.scope, "新词", 1, 40)
        self.assertEqual((count, rows[0].body), (1, "WebUI 添加\n第二行"))

    async def test_unknown_scope_cannot_create_an_unreachable_quote(self):
        result = await self.call("save", {"scope": self.other_scope, "keyword": "a", "body": "b"})
        self.assertEqual(result["code"], 400)
        self.assertEqual(self.plugin.store.web_list()["total"], 1)

    async def test_duplicate_create_is_explicit(self):
        result = await self.call("save", {"scope": self.scope, "keyword": "嘉然", "body": self.row()["body"]})
        self.assertEqual(result["code"], 409)

    async def test_edit_then_stale_edit_is_rejected(self):
        row = self.row()
        payload = {"id": row["id"], "version": row["version"], "scope": self.scope, "keyword": "新关键词", "body": "修改后的内容"}
        first = await self.call("save", payload)
        self.assertEqual(first["code"], 200)
        self.assertNotEqual(first["data"]["version"], row["version"])
        self.assertEqual((await self.call("save", {**payload, "body": "过期覆盖"}))["code"], 409)
        self.assertEqual(self.row()["body"], "修改后的内容")

    async def test_edit_cannot_move_a_quote_between_groups(self):
        row = self.row()
        result = await self.call("save", {"id": row["id"], "version": row["version"], "scope": self.other_scope, "keyword": "a", "body": "b"})
        self.assertEqual(result["code"], 400)
        self.assertEqual(self.row()["scope"], self.scope)

    async def test_deletion_keeps_empty_session_available_for_new_quotes(self):
        row = self.row()
        result = await self.call("delete", {"id": row["id"], "version": row["version"]})
        self.assertEqual(result["code"], 200)
        self.assertEqual((await self.call("state"))["data"]["scopes"][0]["count"], 0)
        result = await self.call("save", {"scope": self.scope, "keyword": "重新添加", "body": "b"})
        self.assertEqual(result["code"], 200)

    async def test_stale_delete_does_not_remove_a_changed_quote(self):
        row = self.row()
        self.plugin.store.web_save(scope=self.scope, keyword="嘉然", body="已更改", collector_id="web:admin",
                                   quote_id=row["id"], version=row["version"])
        result = await self.call("delete", {"id": row["id"], "version": row["version"]})
        self.assertEqual(result["code"], 409)
        self.assertEqual(self.plugin.store.web_list()["total"], 1)

    async def test_missing_quote_returns_not_found(self):
        result = await self.call("delete", {"id": 999, "version": "a" * 64})
        self.assertEqual(result["code"], 404)

    async def test_malformed_requests_and_quote_validation(self):
        for payload in ([], None, {"scope": self.scope, "keyword": "a#b", "body": "b"},
                        {"scope": self.scope, "keyword": "a", "body": " "},
                        {"scope": self.scope, "keyword": "a", "body": "b" * 30001}):
            with self.subTest(kind=type(payload).__name__):
                self.assertEqual((await self.call("save", payload))["code"], 400)
        self.assertEqual((await self.call("save", raw=b"not JSON"))["code"], 400)
        self.assertEqual((await self.call("save", raw=b" " * (128 * 1024 + 1)))["code"], 400)
        self.assertEqual((await self.call("delete", {"id": True, "version": "a" * 64}))["code"], 400)

    async def test_settings_are_persisted_and_applied_without_reload(self):
        result = await self.call("settings", {"source_bot_qqs": ["888", "888"], "query_timeout": 80,
                                               "answer_queries": False, "max_import_quotes": 10})
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.plugin.sources, {"888"})
        self.assertEqual(self.plugin.tracker.ttl, 80)
        self.assertEqual(self.plugin.import_limit, 10)
        self.assertEqual(json.loads(self.plugin.config.path.read_text())["source_bot_qqs"], ["888"])
        self.assertFalse(self.plugin.config["answer_queries"])

    async def test_invalid_settings_do_not_partially_apply(self):
        result = await self.call("settings", {"source_bot_qqs": ["888"], "query_timeout": 0})
        self.assertEqual(result["code"], 400)
        self.assertEqual(self.plugin.sources, set())
        self.assertFalse(self.plugin.config.path.exists())

    async def test_settings_persistence_failure_rolls_back_memory(self):
        self.plugin.config.fail = True
        with self.assertLogs("quote-web-tests", level="ERROR"):
            result = await self.call("settings", {"source_bot_qqs": ["888"]})
        self.assertEqual(result["code"], 500)
        self.assertEqual(dict(self.plugin.config), {})
        self.assertEqual(self.plugin.sources, set())

    async def test_settings_validation_rules(self):
        for payload in ({"unknown": True}, {"auto_collect": "false"}, {"max_import_quotes": True},
                        {"source_bot_qqs": ["not-a-qq"]}, {"ignore_node_patterns": ["["]}):
            with self.subTest(payload=payload):
                self.assertEqual((await self.call("settings", payload))["code"], 400)

    async def test_legacy_query_page_size_is_ignored_and_not_exposed_as_a_setting(self):
        self.plugin.config["page_size"] = 1
        self.plugin.refresh_config()
        self.assertNotIn("page_size", self.api.current_settings())
        self.assertFalse(hasattr(self.plugin, "page_size"))
        result = await self.call("settings", {"max_import_quotes": 100})
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.plugin.config["page_size"], 1)

    async def test_registration_and_unload_remove_only_owned_routes(self):
        self.plugin.context.registered_web_apis.append(("/other/ping", object(), ["GET"], "other"))
        self.api.register()
        self.assertEqual(len(self.plugin.context.registered_web_apis), 7)
        self.api.close()
        self.assertEqual(len(self.plugin.context.registered_web_apis), 1)
        self.assertEqual((await self.call("state"))["code"], 503)

    async def test_native_initialization_and_termination(self):
        api_module = types.ModuleType("astrbot.api")
        api_module.web = self.web
        with patch.dict(sys.modules, {"astrbot.api": api_module}):
            await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.web_ui)
        self.assertEqual(len(self.plugin.context.registered_web_apis), 6)
        await self.plugin.terminate()
        self.assertEqual(self.plugin.context.registered_web_apis, [])

    async def test_legacy_context_keeps_qq_plugin_usable(self):
        self.plugin.context = object()
        self.assertIsNone(register_web_ui(self.plugin, logging.getLogger("quote-web-tests")))

    async def test_existing_database_is_migrated_without_changing_quotes(self):
        with self.plugin.store._connect() as conn:
            conn.execute("DROP TABLE quote_sessions")
        migrated = QuoteStore(self.plugin.store.path)
        self.assertEqual(migrated.web_overview()["stats"]["quotes"], 1)
        self.assertEqual(migrated.web_overview()["scopes"][0]["scope"], self.scope)

    async def test_quote_markup_stays_literal_data(self):
        body = '<img src=x onerror="alert(1)">[CQ:at,qq=all]'
        result = await self.call("save", {"scope": self.scope, "keyword": "HTML", "body": body})
        self.assertEqual(result["data"]["body"], body)
        self.assertEqual((await self.call("quotes", query={"keyword": "HTML"}))["data"]["items"][0]["body"], body)
