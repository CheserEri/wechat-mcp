"""工具逻辑层：封装适配层生命周期，输出可直接返回给 MCP 客户端的结构化结果。

设计要点：

- 首次调用时按需连接微信，后续复用同一连接。
- 所有失败都返回结构化错误，不抛出给协议层，便于 Agent 读取与决策。
- 微信未连接等前置条件不满足时，返回明确错误而非虚报成功。
- 对外部有影响的操作（发送）记录审计元数据，默认不记录完整正文。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .adapters.deepseekgirl import DeepSeekGirlAdapter
from .audit import AuditLogger
from .config import AdapterConfig
from .errors import WeChatMCPError

logger = logging.getLogger(__name__)


class WeChatService:
    """面向 MCP 工具的微信服务封装。"""

    def __init__(
        self,
        adapter: DeepSeekGirlAdapter | None = None,
        audit: AuditLogger | None = None,
    ):
        self._adapter = adapter or DeepSeekGirlAdapter(AdapterConfig.from_env())
        config = self._adapter.config
        self._audit = audit or AuditLogger(config.audit_log_path, config.audit_enabled)
        self._connect_lock = asyncio.Lock()

    @property
    def adapter(self) -> DeepSeekGirlAdapter:
        return self._adapter

    @property
    def audit(self) -> AuditLogger:
        return self._audit

    async def _ensure_connected(self) -> dict[str, Any] | None:
        """确保已连接；成功返回 None，失败返回结构化错误。"""
        if self._adapter.is_connected:
            return None
        async with self._connect_lock:
            if self._adapter.is_connected:
                return None
            try:
                status = await self._adapter.connect()
            except WeChatMCPError as exc:
                return exc.to_dict()
            except Exception as exc:  # 兜底，避免异常穿透到协议层
                logger.exception("连接微信失败")
                return {"ok": False, "code": "connect_failed", "message": str(exc)}
        if not status.ok:
            return status.error or {
                "ok": False,
                "code": "connect_failed",
                "message": "微信连接失败，请确认微信已登录且主窗口可见",
            }
        return None

    def _audit_result(
        self,
        tool: str,
        result: dict[str, Any],
        *,
        action: str = "read",
        **extra: Any,
    ) -> None:
        error = result.get("error") or {}
        self._audit.record(
            tool,
            action=action,
            target=str(extra.pop("target", "") or ""),
            ok=bool(result.get("ok")),
            status=str(result.get("status", "") or ""),
            error_code=str(error.get("code", "") or ""),
            extra=extra or None,
        )

    async def get_status(self) -> dict[str, Any]:
        error = await self._ensure_connected()
        status = self._adapter.get_status().to_dict()
        if error and not status.get("ok"):
            # 连接失败时把底层错误并入状态结果，供 Agent 诊断
            status.setdefault("error", error)
        self._audit_result("get_wechat_status", status)
        return status

    async def get_chat_list(
        self, limit: int | None = None, keyword: str = ""
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("get_chat_list", error, target=keyword)
            return error
        result = self._adapter.get_chat_list(limit=limit, keyword=keyword).to_dict()
        self._audit_result(
            "get_chat_list", result, target=keyword, total=result.get("total")
        )
        return result

    async def get_chat_history(
        self, chat_name: str, limit: int = 20
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("get_chat_history", error, target=chat_name)
            return error
        result = self._adapter.get_chat_history(chat_name, limit=limit).to_dict()
        self._audit_result(
            "get_chat_history",
            result,
            target=chat_name,
            count=len(result.get("messages") or []),
        )
        return result

    async def get_chat_info(self, chat_name: str) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("get_chat_info", error, target=chat_name)
            return error
        result = self._adapter.get_chat_info(chat_name).to_dict()
        self._audit_result(
            "get_chat_info",
            result,
            target=chat_name,
            monitored=result.get("monitored"),
        )
        return result

    async def get_recent_messages(
        self,
        after_seq: int = 0,
        limit: int = 20,
        chat_name: str = "",
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("get_recent_messages", error, target=chat_name)
            return error
        result = self._adapter.get_recent_messages(
            after_seq=after_seq, limit=limit, chat_name=chat_name
        ).to_dict()
        self._audit_result(
            "get_recent_messages",
            result,
            target=chat_name,
            count=len(result.get("messages") or []),
            next_seq=result.get("next_seq"),
        )
        return result

    async def search_contact(
        self, keyword: str, limit: int = 20
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("search_contact", error, target=keyword)
            return error
        result = self._adapter.search_contact(keyword, limit=limit).to_dict()
        self._audit_result(
            "search_contact", result, target=keyword, total=result.get("total")
        )
        return result

    async def set_monitored_chats(
        self,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        # 仅调整本地过滤规则，无需连接微信。
        result = self._adapter.set_monitored_chats(
            add=add, remove=remove, mode=mode
        ).to_dict()
        self._audit_result(
            "set_monitored_chats", result, action="update", mode=result.get("mode")
        )
        return result

    async def send_message(
        self,
        recipient: str,
        message: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("send_message", error, target=recipient)
            return error
        result = await self._adapter.send_message(
            recipient, message, dry_run=dry_run, confirm=confirm
        )
        payload = result.to_dict()
        self._audit_result(
            "send_message",
            payload,
            action="send",
            target=recipient,
            resolved=payload.get("resolved_recipient"),
            dry_run=dry_run,
            message_length=len(str(message)),
        )
        return payload

    async def send_file(
        self,
        recipient: str,
        file_path: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        error = await self._ensure_connected()
        if error:
            self._audit_result("send_file", error, target=recipient)
            return error
        result = await self._adapter.send_file(
            recipient, file_path, dry_run=dry_run, confirm=confirm
        )
        payload = result.to_dict()
        self._audit_result(
            "send_file",
            payload,
            action="send",
            target=recipient,
            resolved=payload.get("resolved_recipient"),
            file_name=payload.get("file_name"),
            file_size=payload.get("file_size"),
            dry_run=dry_run,
        )
        return payload