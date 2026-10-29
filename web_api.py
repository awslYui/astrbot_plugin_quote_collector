"""Authenticated AstrBot Plugin View endpoints; no standalone HTTP server."""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import re
from functools import wraps
from pathlib import Path

from .core import UserError, check_keyword, clean_text, parse_alias_groups
from .store import QuoteConflict, QuoteNotFound

PLUGIN_NAME = "astrbot_plugin_quote_collector"
SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))
RANGES = {"query_timeout": (5, 300), "page_size": (1, 100), "max_import_quotes": (1, 10000)}


def scope_details(scope: str) -> dict:
    try:
        platform, bot, kind, target = json.loads(scope)
        label = f"QQ群 {target}" if kind == "group" else f"私聊 {target}"
        return {"label": label, "detail": f"机器人 {bot} · {platform}"}
    except (ValueError, TypeError):
        return {"label": "历史会话", "detail": "从旧版本保留的会话"}


def validate_settings(payload: dict) -> dict:
    if set(payload) - set(SCHEMA):
        raise UserError("包含未知的配置项，请刷新页面重试。")
    result = {}
    for key, value in payload.items():
        spec = SCHEMA[key]
        kind = spec["type"]
        if kind == "bool":
            if type(value) is not bool:
                raise UserError(f"{spec['description']}必须是开关值。")
        elif kind == "int":
            low, high = RANGES[key]
            if type(value) is not int or not low <= value <= high:
                raise UserError(f"{spec['description']}须为 {low}～{high} 的整数。")
        elif kind == "list":
            if not isinstance(value, list) or len(value) > 200 or any(not isinstance(item, str) for item in value):
                raise UserError(f"{spec['description']}须为文本列表，最多 200 项。")
            value = list(dict.fromkeys(item.strip() for item in value if item.strip()))
            if key == "keyword_aliases":
                parse_alias_groups(value)
            elif key == "ignore_node_patterns":
                for pattern in value:
                    if len(pattern) > 300:
                        raise UserError("单条过滤正则最多 300 字。")
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise UserError("过滤正则表达式无效，请检查后重试。") from exc
            elif any(not re.fullmatch(r"[0-9]{1,20}", item) for item in value):
                raise UserError(f"{spec['description']}只能包含数字账号或群号。")
        result[key] = value
    return result


def endpoint(handler):
    @wraps(handler)
    async def wrapped(self, *args, **kwargs):
        if not self.active:
            return self.web.error_response("插件已停止，请重载插件后刷新页面。", status_code=503)
        if not self.is_admin():
            return self.web.error_response("只有 AstrBot WebUI 管理员可以管理语录。", status_code=403)
        try:
            return self.web.json_response(await handler(self, *args, **kwargs))
        except QuoteNotFound as exc:
            return self.web.error_response(str(exc), status_code=404)
        except QuoteConflict as exc:
            return self.web.error_response(str(exc), status_code=409)
        except UserError as exc:
            return self.web.error_response(str(exc), status_code=400)
        except Exception:
            self.logger.exception("[语录收录] WebUI 请求失败。")
            return self.web.error_response("操作失败，请查看 AstrBot 日志后重试。", status_code=500)
    return wrapped


class QuoteWebApi:
    def __init__(self, plugin, web, logger):
        self.plugin, self.web, self.logger = plugin, web, logger
        self.active = True
        self._settings_lock = asyncio.Lock()
        self.routes = {
            "state": (self.state, "GET"), "quotes": (self.quotes, "GET"),
            "save": (self.save, "POST"), "delete": (self.delete, "POST"),
            "settings": (self.settings, "POST"),
        }

    def is_admin(self) -> bool:
        # The bridge/core authenticates requests. Additionally restrict this
        # management surface to the configured Dashboard administrator account.
        try:
            username = self.web.request.username
            expected = self.plugin.context.get_config().get("dashboard", {}).get("username")
            return bool(isinstance(username, str) and username and isinstance(expected, str)
                        and expected and hmac.compare_digest(username.encode(), expected.encode()))
        except (AttributeError, RuntimeError, TypeError):
            return False

    async def payload(self) -> dict:
        if len(await self.web.request.body()) > 128 * 1024:
            raise UserError("请求内容过长，请减少语录或配置的长度。")
        payload = await self.web.request.json(default=None)
        if not isinstance(payload, dict):
            raise UserError("请求须为有效的 JSON 对象。")
        return payload

    def current_settings(self) -> dict:
        return {key: copy.deepcopy(self.plugin.config.get(key, spec["default"]))
                for key, spec in SCHEMA.items()}

    @endpoint
    async def state(self) -> dict:
        result = await asyncio.to_thread(self.plugin.store.web_overview)
        for item in result["scopes"]:
            item.update(scope_details(item["scope"]))
        result["settings"] = self.current_settings()
        result["can_save_settings"] = callable(getattr(self.plugin.config, "save_config", None))
        return result

    @endpoint
    async def quotes(self) -> dict:
        query = self.web.request.query
        try:
            page = int(query.get("page", 1))
            page_size = int(query.get("page_size", 20))
        except (TypeError, ValueError) as exc:
            raise UserError("页码与每页条数须为整数。") from exc
        if not 1 <= page <= 1000000 or not 1 <= page_size <= 100:
            raise UserError("页码或每页条数超出范围。")
        scope, keyword, text = (str(query.get(key, "") or "") for key in ("scope", "keyword", "query"))
        if len(scope) > 500 or len(keyword) > 100 or len(text) > 200:
            raise UserError("筛选条件过长。")
        result = await asyncio.to_thread(self.plugin.store.web_list, scope=scope, keyword=keyword,
                                         query=text.strip(), page=page, page_size=page_size)
        for item in result["items"]:
            item.update(scope_details(item["scope"]))
        return result

    @staticmethod
    def _id(payload: dict) -> int:
        value = payload.get("id")
        if type(value) is not int or not 1 <= value <= 9223372036854775807:
            raise UserError("语录编号无效。")
        return value

    @staticmethod
    def _version(payload: dict) -> str:
        version = payload.get("version", "")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise UserError("缺少语录版本信息，请刷新后重新操作。")
        return version

    @endpoint
    async def save(self) -> dict:
        payload = await self.payload()
        for key in ("scope", "keyword", "body"):
            if not isinstance(payload.get(key), str):
                raise UserError("请选择所属会话，并填写关键词和语录内容。")
        scope = payload["scope"]
        if not scope or len(scope) > 500:
            raise UserError("请选择有效的所属会话。")
        keyword, body = check_keyword(payload["keyword"]), clean_text(payload["body"])
        if not body or len(body) > 30000:
            raise UserError("语录须为 1～30000 字。")
        quote_id = self._id(payload) if "id" in payload else None
        version = self._version(payload) if quote_id is not None else ""
        result = await asyncio.to_thread(self.plugin.store.web_save, scope=scope, keyword=keyword,
                                         body=body, quote_id=quote_id, version=version,
                                         collector_id=f"web:{self.web.request.username}")
        result.update(scope_details(result["scope"]))
        return result

    @endpoint
    async def delete(self) -> dict:
        payload = await self.payload()
        await asyncio.to_thread(self.plugin.store.web_delete, self._id(payload), self._version(payload))
        return {"deleted": True}

    @endpoint
    async def settings(self) -> dict:
        patch = validate_settings(await self.payload())
        if not callable(getattr(self.plugin.config, "save_config", None)):
            raise UserError("当前环境不支持保存配置，请在 AstrBot 插件配置中设置。")
        async with self._settings_lock:
            old = copy.deepcopy(dict(self.plugin.config))
            updated = {**old, **patch}
            aliases = parse_alias_groups(updated.get("keyword_aliases", []))
            persisted = False
            try:
                await asyncio.to_thread(self.plugin.config.save_config, updated)
                persisted = True
                merged = await asyncio.to_thread(self.plugin.store.configure_aliases, aliases)
            except Exception:
                if persisted:
                    try:
                        await asyncio.to_thread(self.plugin.config.save_config, old)
                    except Exception:
                        self.logger.exception("[语录收录] 别名合并失败后，恢复配置文件失败。")
                self.plugin.config.clear()
                self.plugin.config.update(old)
                raise
            self.plugin.config.clear()
            self.plugin.config.update(updated)
            self.plugin.refresh_config()
        return {"saved": True, "settings": self.current_settings(), "alias_merge": merged}

    def register(self) -> None:
        for name, (handler, method) in self.routes.items():
            self.plugin.context.register_web_api(f"/{PLUGIN_NAME}/ui/{name}", handler, [method],
                                                f"QQ quote manager: {name}")

    def close(self) -> None:
        self.active = False
        registry = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registry, list):
            registry[:] = [entry for entry in registry if len(entry) < 2 or getattr(entry[1], "__self__", None) is not self]


def register_web_ui(plugin, logger):
    if not callable(getattr(plugin.context, "register_web_api", None)):
        return None
    try:
        from astrbot.api import web
    except ImportError:
        logger.warning("[语录收录] 当前 AstrBot 缺少插件视图 API；QQ 指令可用，WebUI 需升级 AstrBot。")
        return None
    api = QuoteWebApi(plugin, web, logger)
    try:
        api.register()
    except Exception:
        api.close()
        logger.exception("[语录收录] WebUI 注册失败，QQ 指令仍可用。")
        return None
    return api
