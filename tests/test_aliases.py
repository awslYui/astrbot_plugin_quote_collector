"""Verify that configured names share data across QQ and the embedded manager."""

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_integration import Bot, Event, forward, plugin_module, text
from test_web_api import Config, Context, Request
from astrbot_plugin_quote_collector.core import UserError, parse_alias_groups
from astrbot_plugin_quote_collector.forwarding import Extraction
from astrbot_plugin_quote_collector.replica import plan_replica
from astrbot_plugin_quote_collector.store import Quote, QuoteConflict, QuoteStore
from astrbot_plugin_quote_collector.web_api import QuoteWebApi, validate_settings

import logging
import types


GROUPS = ["楚宇晗#cyh#醋与寒"]
ALIASES = parse_alias_groups(GROUPS)


class AliasValidationTests(unittest.TestCase):
    def test_multiple_people_and_whitespace(self):
        aliases = parse_alias_groups([" 楚宇晗 # cyh # 醋与寒 ", "另一个人#另一人", ""])
        self.assertEqual({aliases[name] for name in ("楚宇晗", "cyh", "醋与寒")}, {"楚宇晗"})
        self.assertEqual(aliases["另一人"], "另一个人")

    def test_no_names_are_preconfigured_and_names_are_case_sensitive(self):
        self.assertEqual(parse_alias_groups([]), {})
        self.assertNotIn("CYH", ALIASES)

    def test_duplicate_names_within_the_same_group_are_deduplicated(self):
        self.assertEqual(parse_alias_groups(["楚宇晗#cyh#cyh", "楚宇晗#醋与寒"]), ALIASES)

    def test_invalid_groups_and_conflicts_are_rejected(self):
        cases = ["楚宇晗#cyh", [1], ["只有主词条"], ["a#"], ["#b"], ["a#a"],
                 ["a#b", "c#b"], ["a#b", "b#c"], ["a#b", "b#a"],
                 ["a#" + "b" * 101], ["a#b\nc"], ["a#b"] * 201,
                 ["#".join(str(i) for i in range(51))]]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(UserError):
                parse_alias_groups(value)

    def test_native_and_web_configuration_share_validation(self):
        self.assertEqual(validate_settings({"keyword_aliases": GROUPS}), {"keyword_aliases": GROUPS})
        with self.assertRaises(UserError):
            validate_settings({"keyword_aliases": ["人物甲#共同别名", "人物乙#共同别名"]})


class AliasStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = QuoteStore(Path(self.temp.name) / "quotes.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_merge_existing_alias_rows_without_merging_scopes(self):
        self.store.add("g1", "楚宇晗", [Quote("相同内容"), Quote("主词条的语录")], "1")
        self.store.add("g1", "cyh", [Quote("相同内容"), Quote("缩写的语录")], "1")
        self.store.add("g1", "醋与寒", [Quote("谐音的语录")], "1")
        self.store.add("g2", "cyh", [Quote("另一个群的语录")], "1")
        result = self.store.configure_aliases(ALIASES)
        self.assertEqual(result, {"moved": 3, "duplicates": 1})
        for name in ALIASES:
            rows, count = self.store.search("g1", name, 1, 40)
            self.assertEqual(count, 4)
            self.assertEqual({row.keyword for row in rows}, {"楚宇晗"})
            self.assertNotIn("另一个群的语录", {row.body for row in rows})
        self.assertEqual(self.store.search("g2", "醋与寒", 1, 40)[1], 1)

    def test_new_additions_and_exact_delete_share_the_canonical_entry(self):
        self.store.configure_aliases(ALIASES)
        self.assertEqual(self.store.add("g", "cyh", [Quote("原文")], "1"), (1, 0))
        self.assertEqual(self.store.add("g", "醋与寒", [Quote("原文")], "1"), (0, 1))
        self.assertEqual(self.store.delete("g", "楚宇晗", "原"), 0)
        self.assertEqual(self.store.delete("g", "醋与寒", "原文"), 1)

    def test_alias_mapping_and_source_relationships_survive_restart(self):
        keys = [("message", "50"), ("forward", "r")]
        self.store.add_many("g", {"cyh": [Quote("原文", "123", "原作者", "50")]}, "1", keys,
                            {"cyh": {"cyh"}}, {"cyh": {"来点原语录"}})
        self.store.configure_aliases(ALIASES)
        store = QuoteStore(self.store.path)
        self.assertEqual(store.source_keywords("g", keys), {"楚宇晗"})
        rows, count = store.search("g", "来点原语录", 1, 40)
        self.assertEqual((count, rows[0].keyword), (1, "楚宇晗"))
        row = store.web_list(query="cyh")["items"][0]
        self.assertEqual((row["source_libraries"], row["source_triggers"]), (["cyh"], ["来点原语录"]))
        self.assertEqual((row["author_id"], row["author_name"], row["source_id"]), ("123", "原作者", "50"))

    def test_duplicate_merges_keep_both_source_relationships(self):
        self.store.add("g", "楚宇晗", [Quote("原文")], "1", [("forward", "main")])
        self.store.add("g", "cyh", [Quote("原文")], "1", [("forward", "alias")])
        self.store.configure_aliases(ALIASES)
        self.assertEqual(self.store.source_keywords("g", [("forward", "main")]), {"楚宇晗"})
        self.assertEqual(self.store.source_keywords("g", [("forward", "alias")]), {"楚宇晗"})
        self.assertEqual(self.store.web_list()["total"], 1)

    def test_removing_aliases_does_not_split_already_merged_quotes(self):
        self.store.configure_aliases(ALIASES)
        self.store.add("g", "cyh", [Quote("此前合并的语录")], "1")
        self.store.configure_aliases({})
        self.assertEqual(self.store.search("g", "楚宇晗", 1, 40)[1], 1)
        self.assertEqual(self.store.search("g", "cyh", 1, 40)[1], 0)

    def test_changing_the_canonical_name_merges_to_the_new_main(self):
        self.store.configure_aliases(ALIASES)
        self.store.add("g", "cyh", [Quote("原文")], "1")
        self.store.configure_aliases(parse_alias_groups(["cyh#楚宇晗#醋与寒"]))
        self.assertEqual(self.store.search("g", "楚宇晗", 1, 40)[0][0].keyword, "cyh")

    def test_web_writes_and_filters_accept_aliases_and_protect_stale_edits(self):
        self.store.add("g", "cyh", [Quote("原文")], "1")
        before = self.store.web_list()["items"][0]
        self.store.configure_aliases(ALIASES)
        with self.assertRaises(QuoteConflict):
            self.store.web_save(scope="g", keyword="cyh", body="覆盖", collector_id="web:admin",
                                quote_id=before["id"], version=before["version"])
        created = self.store.web_save(scope="g", keyword="醋与寒", body="网页添加", collector_id="web:admin")
        self.assertEqual(created["keyword"], "楚宇晗")
        self.assertEqual(self.store.web_list(keyword="cyh")["total"], 2)
        self.assertEqual(self.store.web_list(query="醋与寒")["total"], 2)

    def test_merge_failure_rolls_back_quotes_provenance_and_aliases(self):
        self.store.add("g", "cyh", [Quote("原文")], "1", [("forward", "r")])
        with self.store._connect() as conn:
            conn.execute("""CREATE TRIGGER fail_aliases BEFORE INSERT ON quote_aliases
                BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.configure_aliases(ALIASES)
        self.assertEqual(self.store.search("g", "cyh", 1, 40)[0][0].keyword, "cyh")
        self.assertEqual(self.store.source_keywords("g", [("forward", "r")]), {"cyh"})
        self.assertEqual(self.store.search("g", "醋与寒", 1, 40)[1], 0)

    def test_concurrent_alias_additions_do_not_create_multiple_copies(self):
        self.store.configure_aliases(ALIASES)
        def add(index):
            return self.store.add("g", list(ALIASES)[index % 3], [Quote("同一语录")], "1")[0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(add, range(18))), 1)
        self.assertEqual(self.store.web_list()["total"], 1)


class AliasWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(plugin_module.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = plugin_module.QuoteCollectorPlugin(Context(), Config(Path(self.temp.name) / "settings.json",
                                                                                keyword_aliases=GROUPS))
        self.bot = Bot()

    def tearDown(self):
        self.temp.cleanup()

    async def dispatch(self, message, **kwargs):
        event = Event(self.bot, [text(message)] if isinstance(message, str) else message, **kwargs)
        await self.plugin.on_message(event)
        return event

    def web_api(self):
        web = types.SimpleNamespace(request=Request(), json_response=lambda value: {"code": 200, "data": value},
                                    error_response=lambda text, status_code=400: {"code": status_code, "message": text})
        return QuoteWebApi(self.plugin, web, logging.getLogger("alias-web-tests")), web

    async def test_add_through_either_alias_query_every_name_and_admin_delete(self):
        await self.dispatch("添加 cyh#第一条")
        await self.dispatch("添加 醋与寒#第二条")
        for name in ALIASES:
            await self.dispatch("查 " + name)
            self.assertEqual([node["data"]["content"][0]["data"]["text"] for node in self.bot.calls[-1][1]["messages"]],
                             ["第一条", "第二条"])
        denied = await self.dispatch("删除 醋与寒#第一条")
        self.assertIn("只有", denied.sent[-1])
        deleted = await self.dispatch("删除 醋与寒#第一条", role="admin")
        self.assertIn("已删除【楚宇晗】", deleted.sent[-1])
        self.assertEqual(self.plugin.store.web_list()["total"], 1)

    async def test_quoted_add_and_manual_steal_resolve_aliases(self):
        self.bot.messages[50] = {"group_id": 100, "message": [text("引用的原文")]}
        await self.dispatch([{"type": "reply", "data": {"id": 50}}, text("添加 cyh")])
        self.bot.messages[51] = {"group_id": 100, "message": [forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text("另一个原文")]}]}
        await self.dispatch([{"type": "reply", "data": {"id": 51}}, text("偷 醋与寒")])
        self.assertEqual({row["keyword"] for row in self.plugin.store.web_list()["items"]}, {"楚宇晗"})

    async def test_catalog_replica_resolves_aliased_main_ids_and_preserves_original_sources(self):
        source = "主库：cyh\n触发词：来点cyh\n全部文本：1 条\n1. ‘原文’\n主库：醋与寒\n触发词：来点醋与寒\n全部文本：1 条\n1. ‘原文’"
        self.bot.messages[50] = {"group_id": 100, "message": [forward("r")]}
        self.bot.forwards["r"] = {"messages": [{"message": [text(source)]}]}
        result = await self.dispatch([{"type": "reply", "data": {"id": 50}}, text("复刻")])
        self.assertIn("【楚宇晗】复刻完成：新增 1 条，重复 1 条", result.sent[-1])
        row = self.plugin.store.web_list()["items"][0]
        self.assertEqual(row["source_libraries"], ["cyh", "醋与寒"])
        self.assertEqual(row["source_triggers"], ["来点cyh", "来点醋与寒"])

    async def test_automatic_replies_to_two_names_are_one_unambiguous_pending_entry(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False)
        self.plugin.refresh_config()
        await self.dispatch("查 cyh", message_id="1")
        event = await self.dispatch("查 醋与寒", message_id="2")
        self.assertEqual(self.plugin.tracker.match(self.plugin._scope(event)).keyword, "楚宇晗")
        self.bot.forwards["r"] = {"messages": [{"message": [text("自动收录的原文")]}]}
        result = await self.dispatch([forward("r")], sender="888")
        self.assertIn("【楚宇晗】收录完成", result.sent[-1])
        self.assertEqual(self.plugin.store.web_list()["items"][0]["keyword"], "楚宇晗")

    async def test_automatic_catalog_collection_uses_the_configured_main_entry(self):
        self.plugin.config.update(source_bot_qqs=["888"], answer_queries=False)
        self.plugin.refresh_config()
        await self.dispatch("查 cyh", message_id="1")
        source = "主库：醋与寒\n触发词：原查询口令\n全部文本：1 条\n1. ‘原来的语录’"
        self.bot.forwards["r"] = {"messages": [{"message": [text(source)]}]}
        result = await self.dispatch([forward("r")], sender="888")
        self.assertIn("【楚宇晗】收录完成：新增 1 条", result.sent[-1])
        row = self.plugin.store.web_list()["items"][0]
        self.assertEqual((row["keyword"], row["body"], row["source_libraries"]), ("楚宇晗", "原来的语录", ["醋与寒"]))

    async def test_web_setting_save_merges_existing_data_and_refreshes_live_commands(self):
        await self.dispatch("添加 未关联别名#之前的内容")
        api, web = self.web_api()
        web.request = Request({"keyword_aliases": ["楚宇晗#cyh#醋与寒#未关联别名"]})
        result = await api.settings()
        self.assertEqual(result["code"], 200)
        self.assertEqual(result["data"]["alias_merge"], {"moved": 1, "duplicates": 0})
        self.assertEqual(json.loads(self.plugin.config.path.read_text())["keyword_aliases"], ["楚宇晗#cyh#醋与寒#未关联别名"])
        await self.dispatch("查 醋与寒")
        self.assertEqual(self.bot.calls[-1][1]["messages"][0]["data"]["content"][0]["data"]["text"], "之前的内容")

    async def test_invalid_alias_setting_does_not_merge_or_change_live_configuration(self):
        await self.dispatch("添加 未关联别名#原文")
        api, web = self.web_api()
        web.request = Request({"keyword_aliases": ["楚宇晗#未关联别名", "另一人#未关联别名"]})
        self.assertEqual((await api.settings())["code"], 400)
        self.assertEqual(self.plugin.config["keyword_aliases"], GROUPS)
        self.assertEqual(self.plugin.store.web_list()["items"][0]["keyword"], "未关联别名")

    async def test_failed_persistence_does_not_merge_existing_data(self):
        await self.dispatch("添加 未关联别名#原文")
        api, web = self.web_api()
        self.plugin.config.fail = True
        web.request = Request({"keyword_aliases": ["楚宇晗#未关联别名"]})
        with self.assertLogs("alias-web-tests", level="ERROR"):
            self.assertEqual((await api.settings())["code"], 500)
        self.assertEqual(self.plugin.aliases, ALIASES)
        self.assertEqual(self.plugin.store.web_list()["items"][0]["keyword"], "未关联别名")

    async def test_failed_merge_restores_the_persisted_config_and_live_aliases(self):
        api, web = self.web_api()
        web.request = Request({"keyword_aliases": ["楚宇晗#新别名"]})
        with patch.object(self.plugin.store, "configure_aliases", side_effect=OSError("simulated merge failure")):
            with self.assertLogs("alias-web-tests", level="ERROR"):
                self.assertEqual((await api.settings())["code"], 500)
        self.assertEqual(self.plugin.aliases, ALIASES)
        self.assertEqual(json.loads(self.plugin.config.path.read_text())["keyword_aliases"], GROUPS)

    def test_generic_replica_treats_same_persons_alias_labels_as_consistent(self):
        plan = plan_replica(Extraction([Quote("关键词：cyh\n原文", author_name="楚宇晗 · #1")]), aliases=ALIASES)
        self.assertEqual(set(plan.groups), {"楚宇晗"})
        self.assertEqual(plan.groups["楚宇晗"][0].body, "原文")

    def test_generic_replica_collapses_aliased_query_hints_before_ambiguity_check(self):
        plan = plan_replica(Extraction([Quote("原文")]), hints={"cyh", "醋与寒"}, aliases=ALIASES)
        self.assertEqual(set(plan.groups), {"楚宇晗"})
