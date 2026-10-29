"""QQ quote collector for AstrBot's OneBot v11 (aiocqhttp) adapter."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping
from dataclasses import replace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core import Command, QueryTracker, UserError, can_delete, parse_alias_groups, parse_command
from .catalog import parse_catalog
from .forwarding import Extraction, ForwardReader, contains_forward, reply_id, segments
from .replica import Reference, envelope_hints, keyword_hint, plan_replica, source_keys
from .store import Quote, QuoteStore


HELP = """语录插件（指令前可加 /）
来点关键词：查询该人物的语录，例如 来点示例人物
来点关键词#2：查看第 2 页
查 关键词：以编号列表汇总展示语录库，长内容自动分块
查 关键词#2：查看第 2 页
添加 关键词#语录：添加一条语录
引用消息后发送 添加 关键词：收录引用的内容
引用聊天记录后发送 偷 关键词：批量收录
引用原语录聊天记录后发送 复刻：沿用原关键词导入已有语录
删除 关键词#完整语录：管理员精确删除
语录帮助：查看本说明

自动收录：在插件配置填入来源机器人的 QQ；在同群发送 查 关键词，插件会收录随后返回的合并聊天记录。每群的语录独立保存。
也支持 来点关键词 发起查询和收录；单独发送人物名字或别名不会触发语录。
原记录含“主库”时，以该人物 ID 作为主词条；“触发词”作为查询别名。
可在 WebUI 的“人物别名”中配置 主词条#别名#别名，查询、添加和删除共用同一语录库。
本插件收录文本；表情转为 [表情:ID]、@ 转为文字。图片、语音和视频本体不保存。"""


class QuoteEventFilter(filter.CustomFilter):
    """Avoid activating AstrBot on unrelated messages or other platforms."""

    plugin = None

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return bool(self.plugin is not None and self.plugin.accepts(event))


class QuoteCollectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.web_ui = None
        self.refresh_config()
        self.store = QuoteStore(StarTools.get_data_dir("astrbot_plugin_quote_collector") / "quotes.db")
        self.store.configure_aliases(self.aliases)
        QuoteEventFilter.plugin = self

    def refresh_config(self) -> None:
        """Apply persisted settings and reset query matching to the new sources."""
        self.aliases = parse_alias_groups(self.config.get("keyword_aliases", []))
        self.sources = self._id_set("source_bot_qqs")
        self.admins = self._id_set("admin_qqs")
        self.groups = self._id_set("enabled_groups")
        self.page_size = self._number("page_size", 40, 1, 100)
        self.import_limit = self._number("max_import_quotes", 2000, 1, 10000)
        self.tracker = QueryTracker(self._number("query_timeout", 60, 5, 300))
        self.ignore_patterns = []
        for pattern in self.config.get("ignore_node_patterns", []):
            try:
                self.ignore_patterns.append(re.compile(pattern))
            except (TypeError, re.error):
                logger.warning("[语录收录] 已忽略无效的节点过滤正则。")

    def canonical_keyword(self, keyword: str) -> str:
        return self.aliases.get(keyword, keyword)

    def _canonical_plan(self, plan):
        groups, libraries, triggers = {}, {}, {}
        for keyword, quotes in plan.groups.items():
            main = self.canonical_keyword(keyword)
            groups.setdefault(main, []).extend(quotes)
            if keyword in plan.libraries:
                libraries.setdefault(main, set()).update(plan.libraries[keyword])
            if keyword in plan.triggers:
                triggers.setdefault(main, set()).update(plan.triggers[keyword])
        plan.groups, plan.libraries, plan.triggers = groups, libraries, triggers
        return plan

    async def initialize(self) -> None:
        from .web_api import register_web_ui
        if self.web_ui is not None:
            self.web_ui.close()
        self.web_ui = register_web_ui(self, logger)

    def _id_set(self, key: str) -> set[str]:
        values = self.config.get(key, [])
        if isinstance(values, str):
            values = re.split(r"[,，\s]+", values)
        return {str(value).strip() for value in values if str(value).strip()}

    def _number(self, key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(self.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _raw(event: AstrMessageEvent) -> Mapping:
        raw = event.message_obj.raw_message
        return raw if isinstance(raw, Mapping) else {}

    def _message(self, event: AstrMessageEvent):
        return self._raw(event).get("message", event.message_obj.message)

    def _text(self, event: AstrMessageEvent) -> str:
        return "".join(str(item.get("data", {}).get("text", ""))
                       for item in segments(self._message(event)) if item.get("type") == "text")

    def accepts(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
        sender_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if sender_id == str(event.get_self_id()) or (group_id and self.groups and group_id not in self.groups):
            return False
        if sender_id in self.sources:
            message = self._message(event)
            return bool(group_id and contains_forward(message)
                        and self.tracker.match(self._scope(event), reply_id(message)))
        return bool(re.match(r"^[／/]?(?:语录帮助$|复刻(?:\s|$)|来点|(?:查|添加|删除|偷)(?:\s|$))", self._text(event).strip()))

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        # unified_msg_origin can contain the speaker even in group chat.
        # A storage scope must be shared by all speakers in the same QQ group.
        group = str(event.get_group_id() or "")
        return json.dumps([str(event.get_platform_id()), str(event.get_self_id()),
                           "group" if group else "private", group or str(event.get_sender_id())],
                          ensure_ascii=False, separators=(",", ":"))

    async def _api(self, event: AstrMessageEvent, action: str, **params):
        self_id = str(event.get_self_id())
        if self_id.isdigit():
            params["self_id"] = int(self_id)
        result = await asyncio.wait_for(event.bot.call_action(action, **params), timeout=20)
        if isinstance(result, Mapping) and ("status" in result or "retcode" in result):
            if result.get("status") == "failed" or result.get("retcode", 0) not in (0, None):
                raise UserError("QQ 协议端返回失败，请稍后重试。")
            result = result.get("data", result)
        return result

    async def _say(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    def _reader(self, event: AstrMessageEvent) -> ForwardReader:
        async def call(action: str, **params):
            return await self._api(event, action, **params)
        return ForwardReader(call, self.import_limit, self.ignore_patterns)

    async def _reference(self, event: AstrMessageEvent) -> Reference:
        message_id = reply_id(self._message(event))
        fallback = None
        for component in event.message_obj.message:
            kind = getattr(component, "type", "")
            if str(getattr(kind, "value", kind)).lower() == "reply":
                fallback = component
                if message_id is None:
                    message_id = str(component.id)
                break
        if message_id is None:
            raise UserError("请先用 QQ 的“回复/引用”功能引用一条消息或合并聊天记录，再发送此指令。")
        if not message_id.lstrip("-").isdigit():
            raise UserError("引用消息的 ID 无效，请重新引用原消息。")
        try:
            original = await self._api(event, "get_msg", message_id=int(message_id))
        except Exception:
            original = None
        if isinstance(original, Mapping) and "message" in original:
            original_group = str(original.get("group_id") or "")
            if original_group and original_group != str(event.get_group_id() or ""):
                raise UserError("请引用当前会话里的消息。")
            sender = original.get("sender") or {}
            return Reference(original["message"], message_id, str(sender.get("user_id") or ""),
                             str(sender.get("card") or sender.get("nickname") or ""))
        if fallback is not None:
            content = getattr(fallback, "chain", None)
            if not content:
                content = getattr(fallback, "message_str", "") or getattr(fallback, "text", "")
            if content:
                return Reference(content, message_id, str(getattr(fallback, "sender_id", "") or ""),
                                 str(getattr(fallback, "sender_nickname", "") or ""))
        raise UserError("未能读取引用内容。请确认记录仍可打开，并检查协议端的 get_msg 接口。")

    @staticmethod
    def _summary(keyword: str, report: Extraction, added: int, duplicates: int) -> str:
        text = f"【{keyword}】收录完成：新增 {added} 条，重复 {duplicates} 条。"
        if report.skipped:
            text += f"跳过空白、纯媒体、说明或过滤节点 {report.skipped} 条。"
        if report.media:
            text += f"另有 {report.media} 个非文本消息段未保存。"
        if report.errors:
            text += f"有 {report.errors} 处记录读取失败，可重新引用导入以补全。"
        return text

    async def _add(self, event: AstrMessageEvent, command: Command) -> None:
        provenance = []
        if command.body is not None:
            report = Extraction([Quote(command.body, str(event.get_sender_id()),
                                        event.get_sender_name(), str(event.message_obj.message_id))])
        else:
            reference = await self._reference(event)
            content = reference.message
            source_id, author_id, author_name = reference.message_id, reference.author_id, reference.author_name
            if command.action == "偷" and not contains_forward(content):
                raise UserError("“偷 关键词”需要引用合并聊天记录；收录普通消息请用“添加 关键词”。")
            report = await self._reader(event).read(content, allow_plain=not contains_forward(content),
                                                   source_id=source_id, author_id=author_id,
                                                   author_name=author_name)
            if contains_forward(content):
                provenance = source_keys(content, source_id)
        if not report.quotes:
            detail = "记录读取失败，请稍后重试。" if report.errors else "没有找到可收录的文本语录。图片、语音和视频本体不保存。"
            raise UserError(detail)
        catalog = parse_catalog(report.quotes, limit=self.import_limit) if provenance else None
        if catalog is not None:
            quotes = [quote for values in catalog.groups.values() for quote in values]
            libraries = {command.keyword: set().union(*catalog.libraries.values())} if catalog.libraries else {}
            triggers = {command.keyword: set().union(*catalog.triggers.values())} if catalog.triggers else {}
            counts = await asyncio.to_thread(self.store.add_many, self._scope(event), {command.keyword: quotes},
                                            str(event.get_sender_id()), provenance, libraries, triggers)
            added, duplicates = counts[command.keyword]
            report.skipped += catalog.headers
        else:
            added, duplicates = await asyncio.to_thread(self.store.add, self._scope(event), command.keyword,
                                                       report.quotes, str(event.get_sender_id()), provenance)
        await self._say(event, self._summary(command.keyword, report, added, duplicates))

    async def _replica_query_hint(self, event: AstrMessageEvent, message) -> str | None:
        query_id = reply_id(message)
        if query_id is None or not query_id.lstrip("-").isdigit():
            return None
        try:
            original = await self._api(event, "get_msg", message_id=int(query_id))
        except Exception:
            return None
        if not isinstance(original, Mapping):
            return None
        original_group = str(original.get("group_id") or "")
        if original_group and original_group != str(event.get_group_id() or ""):
            raise UserError("原查询不属于当前会话，无法确认关键词，本次未写入。")
        text = "".join(str(item.get("data", {}).get("text", "")) for item in segments(original.get("message"))
                       if item.get("type") == "text")
        try:
            query = parse_command(text)
        except UserError:
            return None
        return self.canonical_keyword(query.keyword) if query and query.action in {"查", "来点"} and query.page == 1 else None

    async def _replicate(self, event: AstrMessageEvent) -> None:
        reference = await self._reference(event)
        if not contains_forward(reference.message):
            raise UserError("“复刻”只接受已有的合并聊天记录。请引用原语录记录后直接回复“复刻”；普通消息可用“添加 关键词”。")
        provenance = source_keys(reference.message, reference.message_id)
        remembered = await asyncio.to_thread(self.store.source_keywords, self._scope(event), provenance)
        hints = envelope_hints(reference.message)
        author_hint = keyword_hint(reference.author_name)
        if author_hint:
            hints.add(author_hint)
        query_hint = await self._replica_query_hint(event, reference.message)
        if query_hint:
            hints.add(query_hint)
        report = await self._reader(event).read(reference.message, source_id=reference.message_id)
        if report.errors:
            raise UserError("这份原记录有部分内容读取失败，本次未复刻。请引用可正常打开的完整记录后重试。")
        if not report.quotes:
            raise UserError("没有读到可复刻的现成文本语录。图片、语音和视频本体不保存。")
        plan = self._canonical_plan(plan_replica(report, hints=hints, remembered=remembered,
                                               limit=self.import_limit, aliases=self.aliases))
        counts = await asyncio.to_thread(self.store.add_many, self._scope(event), plan.groups,
                                        str(event.get_sender_id()), provenance, plan.libraries, plan.triggers)
        report.skipped += plan.headers
        added = sum(count[0] for count in counts.values())
        duplicates = sum(count[1] for count in counts.values())
        names = list(plan.groups)
        label = "、".join(names[:5]) + ("等" if len(names) > 5 else "")
        summary = self._summary(label, report, added, duplicates).replace("收录完成", "复刻完成", 1)
        if plan.libraries:
            libraries = sorted(set().union(*plan.libraries.values()))
            summary += "原主库：" + "、".join(libraries[:5]) + ("等" if len(libraries) > 5 else "") + "。"
        if plan.triggers:
            triggers = sorted(set().union(*plan.triggers.values()))
            summary += "原触发词：" + "、".join(triggers[:5]) + ("等" if len(triggers) > 5 else "") + "。"
        await self._say(event, summary)

    async def _delete(self, event: AstrMessageEvent, command: Command) -> None:
        sender = self._raw(event).get("sender") or {}
        role = str(sender.get("role") or "") if isinstance(sender, Mapping) else ""
        if not can_delete(event.is_admin(), str(event.get_sender_id()), role,
                          bool(event.get_group_id()), self.admins):
            raise UserError("删除失败：只有 AstrBot 管理员、配置的管理员、当前群的群主/管理员可以删除。")
        deleted = await asyncio.to_thread(self.store.delete, self._scope(event), command.keyword, command.body)
        await self._say(event, f"已删除【{command.keyword}】的这条语录。" if deleted else
                        "没有找到这条语录。请确认关键词和 # 后的完整内容（包括换行）一致。")

    async def _query(self, event: AstrMessageEvent, command: Command, waiting: bool) -> None:
        if not self.config.get("answer_queries", True):
            return
        natural = command.action == "来点"
        if natural:
            triggers = (command.trigger, "来点 " + command.trigger[2:])
            quotes, total = await asyncio.to_thread(self.store.search, self._scope(event), command.keyword,
                                                    command.page, self.page_size, exact_only=True, triggers=triggers)
            pages = math.ceil(total / self.page_size)
            shown = len(quotes)
        else:
            catalog = await asyncio.to_thread(self.store.catalog_page, self._scope(event), command.keyword, command.page)
            total, pages, shown = catalog.total, catalog.pages, catalog.shown
        if not total:
            if not waiting:
                await self._say(event, f"还没有【{command.keyword}】的语录。可用 添加 {command.keyword}#语录，或引用聊天记录后发送 偷 {command.keyword}。")
            return
        query_text = command.trigger if natural else f"查 {command.keyword}"
        if command.page > pages:
            raise UserError(f"【{command.keyword}】共 {pages} 页，请使用 {query_text}#1 至 #{pages}。")
        if natural:
            contents = [(f"{quote.keyword} · #{quote.id}", quote.body) for quote in quotes]
        else:
            contents = [(f"语录库 · {index}/{len(catalog.fragments)}", fragment)
                        for index, fragment in enumerate(catalog.fragments, 1)]
        nodes = [{"type": "node", "data": {
            "user_id": str(event.get_self_id()), "nickname": nickname,
            "content": [{"type": "text", "data": {"text": body}}],
        }} for nickname, body in contents]
        is_group = bool(event.get_group_id())
        action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
        target = {"group_id": int(event.get_group_id())} if is_group else {"user_id": int(event.get_sender_id())}
        try:
            await self._api(event, action, messages=nodes, **target)
        except Exception:
            logger.warning("[语录收录] 合并转发发送失败，改为文本输出。")
            text = "\n\n".join(body for _, body in contents)
            for start in range(0, len(text), 3500):
                await self._say(event, text[start:start + 3500])
        if pages > 1:
            following = f"下一页：{query_text}#{command.page + 1}" if command.page < pages else "已到最后一页。"
            detail = f"本页展示 {shown} 条。" if not natural else ""
            await self._say(event, f"【{command.keyword}】共 {total} 条，第 {command.page}/{pages} 页。{detail}{following}")

    async def _collect(self, event: AstrMessageEvent) -> None:
        message = self._message(event)
        if not event.get_group_id() or not contains_forward(message):
            return
        query = self.tracker.claim(self._scope(event), reply_id(message))
        if query is None:
            return
        event.stop_event()
        success = False
        try:
            provenance = source_keys(message, str(event.message_obj.message_id))
            if not self.config.get("auto_collect", True):
                # Keep only the exact query -> record association. Content is
                # fetched/imported later only when a user explicitly replicates.
                await asyncio.to_thread(self.store.remember_source, self._scope(event), query.keyword, provenance)
                success = True
                return
            report = await self._reader(event).read(message, source_id=str(event.message_obj.message_id))
            if not report.quotes:
                success = not report.errors
                if self.config.get("notify_import", True):
                    await self._say(event, "未能收录：" + ("聊天记录读取失败，请稍后引用重试。" if report.errors else "这份聊天记录中没有可收录的文本。"))
                return
            catalog = parse_catalog(report.quotes, limit=self.import_limit)
            label = query.keyword
            if catalog is not None:
                catalog = self._canonical_plan(catalog)
                counts = await asyncio.to_thread(self.store.add_many, self._scope(event), catalog.groups,
                                                str(event.get_sender_id()), provenance, catalog.libraries, catalog.triggers)
                added = sum(count[0] for count in counts.values())
                duplicates = sum(count[1] for count in counts.values())
                report.skipped += catalog.headers
                names = list(catalog.groups)
                label = "、".join(names[:5]) + ("等" if len(names) > 5 else "")
            else:
                added, duplicates = await asyncio.to_thread(self.store.add, self._scope(event), query.keyword,
                                                           report.quotes, str(event.get_sender_id()), provenance)
            success = True
            if self.config.get("notify_import", True):
                await self._say(event, self._summary(label, report, added, duplicates))
        finally:
            self.tracker.finish(query, success)

    @filter.custom_filter(QuoteEventFilter, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """Listen without requiring a slash or @ mention, including other bots' forwards."""
        if event.get_platform_name() != "aiocqhttp":
            return
        sender_id = str(event.get_sender_id())
        if sender_id == str(event.get_self_id()):
            return
        group_id = str(event.get_group_id() or "")
        if group_id and self.groups and group_id not in self.groups:
            return
        try:
            if sender_id in self.sources:
                await self._collect(event)
                return
            # Read only this message's text, never text nested inside Reply.
            command = parse_command(self._text(event))
            if command is None:
                return
            command = replace(command, keyword=self.canonical_keyword(command.keyword))
            event.stop_event()
            event.should_call_llm(False)
            if command.action in {"查", "来点"}:
                waiting = bool(group_id and self.sources and self.config.get("auto_collect", True))
                if group_id and self.sources and command.page == 1:
                    self.tracker.add(self._scope(event), str(event.message_obj.message_id), command.keyword)
                await self._query(event, command, waiting and command.page == 1)
            else:
                if command.action in {"添加", "偷"}:
                    await self._add(event, command)
                elif command.action == "复刻":
                    await self._replicate(event)
                elif command.action == "删除":
                    await self._delete(event, command)
                else:
                    await self._say(event, HELP)
        except UserError as exc:
            event.stop_event()
            event.should_call_llm(False)
            await self._say(event, str(exc))
        except Exception:
            logger.exception("[语录收录] 处理消息失败。")
            event.stop_event()
            event.should_call_llm(False)
            await self._say(event, "语录处理失败，已有语录会保留。请检查 AstrBot 日志后重试。")

    async def terminate(self) -> None:
        if self.web_ui is not None:
            self.web_ui.close()
            self.web_ui = None
        if QuoteEventFilter.plugin is self:
            QuoteEventFilter.plugin = None
