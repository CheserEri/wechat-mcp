"""对 deepseekgirl 微信自动化能力的适配封装。

设计原则：

- 只复用其 ``WeChatBridge`` 的连接、状态、发送与消息监听回调能力；
  不导入、不启动原项目的 ``ChatBot`` / ``MessageHandler`` / 人设 / 自动回复逻辑。
- 所有调用返回结构化结果；底层失败不伪装成成功。
- 发送操作做目标校验、同名消歧与串行化，不依赖当前焦点窗口。

已知限制（阶段 0 结论）：底层库不提供「按需拉取会话列表 / 历史消息」接口，
读取能力只能基于监听回调收集到的消息，故 ``get_chat_list`` / ``get_chat_history``
返回的是「适配层运行期间被动接收到的消息」，不是微信完整历史。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import re
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..config import AdapterConfig
from ..errors import (
    BackendUnavailableError,
    ConfirmationRequiredError,
    NotConnectedError,
    TargetAmbiguousError,
    WeChatMCPError,
)
from ..monitoring import MonitoredChats
from ..schemas import (
    ChatHistoryResult,
    ChatInfoResult,
    ChatListResult,
    ChatSummary,
    ConnectionState,
    ContactCandidate,
    ContactSearchResult,
    FileSendResult,
    MessageRecord,
    MonitoredChatsResult,
    RecentMessagesResult,
    SendResult,
    SendStatus,
    StatusResult,
)
from ..security import (
    resolve_target,
    validate_message,
    validate_recipient,
    validate_send_path,
)

_STATUS_MAP: dict[str, ConnectionState] = {
    "disconnected": ConnectionState.DISCONNECTED,
    "connecting": ConnectionState.CONNECTING,
    "connected": ConnectionState.CONNECTED,
    "error": ConnectionState.ERROR,
}

_listener_logger = logging.getLogger(__name__)

# 微信搜索框不认会话 ID（wxid / 群 room id），把它们当关键词会打开错误会话。
_CHAT_ID_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_-]+|gh_[A-Za-z0-9_-]+|.+@chatroom)$")


def _looks_like_chat_id(value: str) -> bool:
    """判断字符串是否为不可直接搜索的会话 ID。"""
    return bool(_CHAT_ID_RE.match(str(value or "").strip()))


def _unresolved_target_error(recipient: str) -> dict[str, Any]:
    """会话 ID 无法解析成显示名时的结构化错误。"""
    return {
        "code": "chat_id_unresolved",
        "message": (
            f"无法把会话 ID「{recipient}」解析成可搜索的会话名，已拒绝发送，"
            "避免误搜/误发到其它会话"
        ),
        "detail": {"recipient": recipient},
    }


def _bundled_bridge_file() -> Path | None:
    """返回随包内置的 ``wechat_bridge.py`` 路径。

    冻结打包（PyInstaller）时该文件被作为数据文件内置到解包目录，
    使分发的 exe 无需外部 deepseekgirl 项目即可运行；源码模式下返回 ``None``。
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = Path(base) / "wechat_bridge.py"
    return candidate if candidate.is_file() else None


def _load_bridge_from_file(module_file: Path) -> Any:
    """按文件路径加载 ``wechat_bridge`` 模块，避免污染 ``sys.path``。"""
    spec = importlib.util.spec_from_file_location("wechat_bridge", module_file)
    if spec is None or spec.loader is None:
        raise BackendUnavailableError(
            f"无法加载内置微信桥接模块: {module_file}",
            detail={"module_file": str(module_file)},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules["wechat_bridge"] = module
    spec.loader.exec_module(module)
    return module


def load_wechat_bridge(project_path: Path) -> type:
    """加载 ``WeChatBridge`` 类。

    优先使用随包内置的副本（冻结打包时存在），使分发的 exe 无需外部项目；
    否则回退到 ``DEEPSEEKGIRL_PATH`` 指定的源码项目。

    仅加载 ``wechat_bridge`` 模块本身（它只依赖标准库与 loguru），
    不会触发原项目的机器人业务模块导入。
    """
    bundled = _bundled_bridge_file()
    if bundled is not None:
        return _load_bridge_from_file(bundled).WeChatBridge

    src_dir = Path(project_path) / "src"
    module_file = src_dir / "wechat_bridge.py"
    if not module_file.is_file():
        raise BackendUnavailableError(
            f"未找到 deepseekgirl 微信桥接模块: {module_file}",
            detail={"expected_file": str(module_file)},
        )

    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    try:
        module = importlib.import_module("wechat_bridge")
    except ImportError as exc:  # 依赖未安装
        raise BackendUnavailableError(
            f"加载 wechat_bridge 失败，请确认依赖已安装: {exc}",
            detail={"project_path": str(project_path)},
        ) from exc
    return module.WeChatBridge


class DeepSeekGirlAdapter:
    """独立的微信适配层，可脱离原机器人直接调用。"""

    def __init__(
        self,
        config: AdapterConfig | None = None,
        bridge_factory: Callable[[], Any] | None = None,
    ):
        self._config = config or AdapterConfig.from_env()
        self._bridge_factory = bridge_factory
        self._bridge: Any | None = None
        self._send_lock = asyncio.Lock()
        self._buffer: deque[MessageRecord] = deque(
            maxlen=self._config.history_buffer_size
        )
        self._buffer_lock = threading.Lock()
        self._listening = False
        self._last_error: dict[str, Any] | None = None
        # P1：会话关注列表（仅过滤读取结果，不改变底层监听）。
        self._monitor = MonitoredChats.from_config(self._config)
        # P1：消息序号，用于 get_recent_messages 的增量游标。
        self._seq = 0
        # 实时消息订阅者：消息到达瞬间被通知（用于常驻 bot，无需轮询）。
        # 支持同步或异步回调，入参为 MessageRecord。
        self._message_listeners: list[
            Callable[[MessageRecord], Any]
        ] = []
        # 会话 ID → 曾成功解析出的显示名。DB 快照偶发不可用时用它兜底，
        # 避免发送时只剩「退化成 ID」的显示名可用。
        self._chat_id_names: dict[str, str] = {}

    # ------------------------------------------------------------------ 生命周期

    @property
    def config(self) -> AdapterConfig:
        return self._config

    @property
    def is_connected(self) -> bool:
        return bool(
            self._bridge is not None
            and getattr(self._bridge, "is_connected", False)
        )

    def _create_bridge(self) -> Any:
        if self._bridge_factory is not None:
            bridge = self._bridge_factory()
        else:
            bridge_cls = load_wechat_bridge(self._config.deepseekgirl_path)
            bridge = bridge_cls(
                target_groups=[],
                on_message=self._on_message,
                reply_delay=self._config.reply_delay,
                backend=self._config.backend,
                listen_private=self._config.listen_private,
            )
        self._apply_send_timeout(bridge)
        return bridge

    def _apply_send_timeout(self, bridge: Any) -> None:
        """放宽底层硬编码的 5 秒发送超时。

        原项目为“秒回”场景调优；向尚未打开的会话发送需要搜索、切换与输入，
        首次通常超过 5 秒。这里只放宽、不收紧。
        """
        try:
            current = float(getattr(bridge, "SEND_TIMEOUT_SECONDS"))
        except (AttributeError, TypeError, ValueError):
            return
        bridge.SEND_TIMEOUT_SECONDS = max(current, self._config.send_timeout_seconds)

    async def connect(self) -> StatusResult:
        """连接已登录的微信客户端，并按配置启动消息监听。"""
        if self.is_connected:
            return self.get_status()

        try:
            self._bridge = self._create_bridge()
        except WeChatMCPError as exc:
            self._last_error = exc.to_dict()
            return self._status_result(ConnectionState.ERROR, error=exc.to_dict())

        # 注入回调，使监听到的消息进入本地缓冲区。
        self._bridge.on_message = self._on_message

        try:
            connected = bool(await self._bridge.connect())
        except Exception as exc:
            self._last_error = {"code": "connect_failed", "message": str(exc)}
            return self._status_result(
                ConnectionState.ERROR, error=self._last_error
            )

        if not connected:
            self._last_error = {
                "code": "connect_failed",
                "message": "微信连接失败，请确认已登录且主窗口可见",
            }
            return self._status_result(
                ConnectionState.ERROR, error=self._last_error
            )

        self._last_error = None
        if self._config.listen_on_connect:
            self._start_listening()
        return self.get_status()

    def _start_listening(self) -> None:
        if self._bridge is None or self._listening:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            self._bridge.start_listening(loop)
            self._listening = True
        except Exception as exc:
            self._last_error = {"code": "listen_failed", "message": str(exc)}

    async def disconnect(self) -> StatusResult:
        bridge, self._bridge = self._bridge, None
        self._listening = False
        if bridge is not None:
            try:
                await bridge.disconnect()
            except Exception:
                pass
        return self.get_status()

    # ------------------------------------------------------------------ 状态

    def get_status(self) -> StatusResult:
        bridge = self._bridge
        if bridge is None:
            # 保留上一次失败原因，避免后端不可用时错误码为空
            return self._status_result(
                ConnectionState.DISCONNECTED, error=self._last_error
            )

        raw: dict[str, Any] = {}
        try:
            info = bridge.status_info
            if isinstance(info, dict):
                raw = info
        except Exception:
            raw = {}

        state = _STATUS_MAP.get(
            str(raw.get("status", "")).lower(), ConnectionState.DISCONNECTED
        )
        # 以桥接的真实监听状态为准，避免把「已请求监听」误报为「监听中」。
        listen_all_active = bool(getattr(bridge, "_listen_all_active", False))
        running = bool(getattr(bridge, "_running", False))
        listening = bool(self._listening and (listen_all_active or running))
        return self._status_result(
            state,
            backend=str(getattr(bridge, "_backend", "") or self._config.backend),
            listening=listening,
            window_visible=bool(self._safe_call(bridge, "has_usable_gui")),
            uia_available=bool(self._safe_call(bridge, "has_usable_uia")),
            detail={
                "raw_status": raw.get("status"),
                "listen_private": raw.get("listen_private"),
                "listen_all_active": listen_all_active,
            },
            error=self._last_error,
        )

    # ------------------------------------------------------------------ 发送

    async def send_message(
        self,
        recipient: str,
        message: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> SendResult:
        """向明确指定的联系人或群聊发送文本。

        仅在能够确认发送成功时返回 ``ok=True``；目标不唯一时拒绝发送。
        """
        try:
            recipient = validate_recipient(recipient)
            message = validate_message(message)
        except WeChatMCPError as exc:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=str(recipient or ""),
                error=exc.to_dict(),
            )

        if not self.is_connected:
            error = NotConnectedError("尚未连接微信，无法发送消息").to_dict()
            return SendResult(
                ok=False, status=SendStatus.FAILED, recipient=recipient, error=error
            )

        # 会话身份优先用显示名；若上游解析失败退化成了会话 ID，
        # 必须先反查成可搜索的显示名，绝不能把 ID 当关键词送进微信搜索框。
        target = self.resolve_chat_identifier(recipient)
        if not target:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error=_unresolved_target_error(recipient),
            )

        resolved, candidates = resolve_target(target, self._known_chat_names())
        if candidates:
            error = TargetAmbiguousError(
                "目标不唯一，请指定更完整的名称：" + "、".join(candidates),
                candidates,
            ).to_dict()
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error=error,
            )

        preview = message[:50]
        if dry_run:
            return SendResult(
                ok=True,
                status=SendStatus.DRY_RUN,
                recipient=recipient,
                resolved_recipient=resolved,
                message_preview=preview,
                dry_run=True,
            )

        if self._config.require_confirmation and not confirm:
            error = ConfirmationRequiredError(
                "当前配置要求发送前确认，请以 confirm=true 复核后再发送",
                detail={"recipient": resolved, "message_preview": preview},
            ).to_dict()
            return SendResult(
                ok=False,
                status=SendStatus.CONFIRMATION_REQUIRED,
                recipient=recipient,
                resolved_recipient=resolved,
                message_preview=preview,
                error=error,
            )

        async with self._send_lock:
            try:
                sent = bool(await self._bridge.send_text(resolved, message))
            except Exception as exc:
                return SendResult(
                    ok=False,
                    status=SendStatus.FAILED,
                    recipient=recipient,
                    resolved_recipient=resolved,
                    message_preview=preview,
                    error={"code": "send_failed", "message": str(exc)},
                )

        if sent:
            return SendResult(
                ok=True,
                status=SendStatus.SENT,
                recipient=recipient,
                resolved_recipient=resolved,
                message_preview=preview,
            )
        return SendResult(
            ok=False,
            status=SendStatus.FAILED,
            recipient=recipient,
            resolved_recipient=resolved,
            message_preview=preview,
            error={"code": "send_failed", "message": "底层发送未成功，结果未确认"},
        )

    # ------------------------------------------------------------------ 读取

    def get_chat_list(self, limit: int | None = None, keyword: str = "") -> ChatListResult:
        """返回适配层运行期间观察到的会话列表（非微信完整会话列表）。

        结果按会话关注列表过滤；被忽略的会话不会出现在返回中。
        """
        records = [
            record
            for record in self._snapshot()
            if self._monitor.is_allowed(record.chat)
        ]
        keyword = str(keyword or "").strip()

        summaries: dict[str, ChatSummary] = {}
        for record in records:
            if keyword and keyword not in record.chat:
                continue
            item = summaries.get(record.chat)
            if item is None:
                summaries[record.chat] = ChatSummary(
                    name=record.chat,
                    is_group=record.is_group,
                    message_count=1,
                    last_message_at=record.timestamp,
                )
                continue
            item.message_count += 1
            if record.timestamp and (
                item.last_message_at is None or record.timestamp > item.last_message_at
            ):
                item.last_message_at = record.timestamp

        chats = sorted(
            summaries.values(),
            key=lambda chat: chat.last_message_at or 0.0,
            reverse=True,
        )
        total = len(chats)
        if limit is not None and int(limit) > 0:
            chats = chats[: int(limit)]

        return ChatListResult(
            ok=True,
            chats=chats,
            total=total,
            complete=not self._buffer_full(),
            note=self._read_note(),
        )

    def get_chat_history(self, chat_name: str, limit: int = 20) -> ChatHistoryResult:
        """返回指定会话在缓冲区内的最近消息（非微信完整历史）。"""
        try:
            chat_name = validate_recipient(chat_name)
        except WeChatMCPError as exc:
            return ChatHistoryResult(
                ok=False,
                chat=str(chat_name or ""),
                complete=False,
                note=exc.message,
            )

        if not self._monitor.is_allowed(chat_name):
            return ChatHistoryResult(
                ok=True,
                chat=chat_name,
                messages=[],
                complete=not self._buffer_full(),
                note="该会话当前不在关注列表中，读取结果已被过滤。",
            )

        records = [
            record for record in self._snapshot() if record.chat == chat_name
        ]
        if limit is not None and int(limit) > 0:
            records = records[-int(limit) :]

        return ChatHistoryResult(
            ok=True,
            chat=chat_name,
            messages=records,
            complete=not self._buffer_full(),
            note=self._read_note(),
        )

    def get_recent_messages(
        self,
        after_seq: int = 0,
        limit: int = 20,
        chat_name: str = "",
    ) -> RecentMessagesResult:
        """增量读取缓冲区中序号大于 ``after_seq`` 的消息。

        返回的 ``next_seq`` 应作为下一次调用的 ``after_seq``；被关注列表过滤掉的
        消息会被跳过（游标仍向前推进），避免无意义重复扫描。
        """
        cursor = max(0, int(after_seq or 0))
        chat_name = str(chat_name or "").strip()
        if chat_name:
            try:
                chat_name = validate_recipient(chat_name)
            except WeChatMCPError as exc:
                return RecentMessagesResult(
                    ok=False, next_seq=cursor, error=exc.to_dict()
                )

        records = [
            record
            for record in self._snapshot()
            if record.seq > cursor
            and self._monitor.is_allowed(record.chat)
            and (not chat_name or record.chat == chat_name)
        ]
        records.sort(key=lambda record: record.seq)

        truncated = False
        if limit is not None and int(limit) > 0 and len(records) > int(limit):
            records = records[: int(limit)]
            truncated = True

        if records:
            next_seq = records[-1].seq
        else:
            with self._buffer_lock:
                next_seq = max(cursor, self._seq)

        return RecentMessagesResult(
            ok=True,
            messages=records,
            next_seq=next_seq,
            complete=not truncated and not self._buffer_full(),
            note=self._read_note(),
        )

    def get_chat_info(self, chat_name: str) -> ChatInfoResult:
        """返回已确认可获取的会话信息（名称、类型、观察到的消息数等）。"""
        try:
            chat_name = validate_recipient(chat_name)
        except WeChatMCPError as exc:
            return ChatInfoResult(
                ok=False, name=str(chat_name or ""), error=exc.to_dict()
            )

        records = [
            record for record in self._snapshot() if record.chat == chat_name
        ]
        is_group = (
            records[0].is_group
            if records
            else chat_name.endswith("@chatroom")
        )
        last_message_at: float | None = None
        for record in records:
            if record.timestamp and (
                last_message_at is None or record.timestamp > last_message_at
            ):
                last_message_at = record.timestamp

        return ChatInfoResult(
            ok=True,
            name=chat_name,
            is_group=is_group,
            message_count=len(records),
            last_message_at=last_message_at,
            monitored=self._monitor.is_allowed(chat_name),
            note=self._read_note(),
        )

    # ------------------------------------------------------------------ 联系人搜索

    def search_contact(
        self, keyword: str, limit: int = 20
    ) -> ContactSearchResult:
        """按名称搜索联系人/群聊，返回候选列表，不自行猜测同名项。

        优先查询微信本地联系人库；后端不支持本地库时回退为「缓冲区观察到的
        会话名称」，并在 ``source`` 中如实标注来源。
        """
        if keyword is None or not str(keyword).strip():
            return ContactSearchResult(
                ok=False,
                error={"code": "target_invalid", "message": "搜索关键词不能为空"},
            )
        keyword = str(keyword).strip()

        if not self.is_connected:
            return ContactSearchResult(
                ok=False,
                keyword=keyword,
                error=NotConnectedError("尚未连接微信，无法搜索联系人").to_dict(),
            )

        db = self._contact_db()
        candidates: list[ContactCandidate] = []
        source = "wechat_db"
        note = "来自微信本地联系人库。"

        if db is not None:
            try:
                rows = db.search_contact(keyword) or []
                candidates = self._build_candidates(rows)
            except Exception as exc:
                logger.debug("查询联系人库失败，回退缓冲区: %s", exc)
                db = None

        if db is None:
            source = "buffer"
            note = (
                "当前后端无本地联系人库，结果基于适配层运行期间观察到的会话名称。"
            )
            for name in self._known_chat_names():
                if keyword in name:
                    candidates.append(
                        ContactCandidate(
                            name=name,
                            is_group=name.endswith("@chatroom"),
                        )
                    )

        total = len(candidates)
        if limit is not None and int(limit) > 0:
            candidates = candidates[: int(limit)]

        return ContactSearchResult(
            ok=True,
            keyword=keyword,
            candidates=candidates,
            total=total,
            source=source,
            note=note,
        )

    @staticmethod
    def _build_candidates(rows: list[Any]) -> list[ContactCandidate]:
        candidates: list[ContactCandidate] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if isinstance(row, dict):
                username = str(row.get("username", "") or "")
                nick = str(row.get("nick_name", "") or "")
                remark = str(row.get("remark", "") or "")
            else:  # 兼容元组返回
                username = str(row[0] or "")
                nick = str(row[1] or "")
                remark = str(row[2] or "")
            name = remark or nick or username
            key = (name, username)
            if not name or key in seen:
                continue
            seen.add(key)
            candidates.append(
                ContactCandidate(
                    name=name,
                    username=username,
                    remark=remark,
                    is_group=username.endswith("@chatroom"),
                )
            )
        return candidates

    # ------------------------------------------------------------------ 发送文件

    async def send_file(
        self,
        recipient: str,
        file_path: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> FileSendResult:
        """向明确指定的联系人或群聊发送本地文件。

        发送前校验文件存在、可读且位于允许目录内；结果无法确认时不宣称成功。
        """
        try:
            recipient = validate_recipient(recipient)
            resolved_path = validate_send_path(
                file_path, self._config.allow_send_dirs
            )
        except WeChatMCPError as exc:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=str(recipient or ""),
                file_path=str(file_path or ""),
                error=exc.to_dict(),
            )

        file_name = resolved_path.name
        try:
            file_size = resolved_path.stat().st_size
        except OSError:
            file_size = 0

        if not self.is_connected:
            error = NotConnectedError("尚未连接微信，无法发送文件").to_dict()
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                file_path=str(resolved_path),
                file_name=file_name,
                file_size=file_size,
                error=error,
            )

        resolved, candidates = resolve_target(recipient, self._known_chat_names())
        if candidates:
            error = TargetAmbiguousError(
                "目标不唯一，请指定更完整的名称：" + "、".join(candidates),
                candidates,
            ).to_dict()
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                file_path=str(resolved_path),
                file_name=file_name,
                file_size=file_size,
                error=error,
            )

        if dry_run:
            return FileSendResult(
                ok=True,
                status=SendStatus.DRY_RUN,
                recipient=recipient,
                resolved_recipient=resolved,
                file_path=str(resolved_path),
                file_name=file_name,
                file_size=file_size,
                dry_run=True,
            )

        if self._config.require_confirmation and not confirm:
            error = ConfirmationRequiredError(
                "当前配置要求发送前确认，请以 confirm=true 复核后再发送",
                detail={
                    "recipient": resolved,
                    "file_name": file_name,
                    "file_size": file_size,
                },
            ).to_dict()
            return FileSendResult(
                ok=False,
                status=SendStatus.CONFIRMATION_REQUIRED,
                recipient=recipient,
                resolved_recipient=resolved,
                file_path=str(resolved_path),
                file_name=file_name,
                file_size=file_size,
                error=error,
            )

        async with self._send_lock:
            try:
                sent = bool(await self._bridge.send_file(resolved, str(resolved_path)))
            except Exception as exc:
                return FileSendResult(
                    ok=False,
                    status=SendStatus.FAILED,
                    recipient=recipient,
                    resolved_recipient=resolved,
                    file_path=str(resolved_path),
                    file_name=file_name,
                    file_size=file_size,
                    error={"code": "send_failed", "message": str(exc)},
                )

        return FileSendResult(
            ok=sent,
            status=SendStatus.SENT if sent else SendStatus.FAILED,
            recipient=recipient,
            resolved_recipient=resolved,
            file_path=str(resolved_path),
            file_name=file_name,
            file_size=file_size,
            error=None
            if sent
            else {"code": "send_failed", "message": "底层发送未成功，结果未确认"},
        )

    # ------------------------------------------------------------------ 关注列表

    def set_monitored_chats(
        self,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        mode: str | None = None,
    ) -> MonitoredChatsResult:
        """运行时增删关注/忽略会话，或切换 allow/block 模式。"""
        try:
            self._monitor.apply(add=add, remove=remove, mode=mode)
        except WeChatMCPError as exc:
            snapshot = self._monitor.snapshot()
            return MonitoredChatsResult(
                ok=False,
                mode=snapshot["mode"],
                allow=snapshot["allow"],
                block=snapshot["block"],
                error=exc.to_dict(),
            )
        snapshot = self._monitor.snapshot()
        return MonitoredChatsResult(
            ok=True,
            mode=snapshot["mode"],
            allow=snapshot["allow"],
            block=snapshot["block"],
            note="该过滤仅作用于 MCP 读取结果，不改变底层监听行为。",
        )

    def _contact_db(self) -> Any:
        """获取微信本地联系人库句柄；后端不支持时返回 None。"""
        bridge = self._bridge
        if bridge is None:
            return None
        wx = getattr(bridge, "_wx", None)
        return getattr(wx, "_db", None)

    # ------------------------------------------------------------------ 内部

    async def _on_message(self, message: Any) -> None:
        record = self._to_record(message)
        if record is None:
            return
        if record.chat_id and record.chat and record.chat != record.chat_id:
            # 这条消息的显示名解析成功过，记下来供发送时兜底。
            self._chat_id_names[record.chat_id] = record.chat
        with self._buffer_lock:
            self._seq += 1
            record.seq = self._seq
            self._buffer.append(record)
        # 在锁外通知订阅者，避免慢回调拖住缓冲写入。
        for listener in tuple(self._message_listeners):
            try:
                result = listener(record)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _listener_logger.exception("消息订阅回调执行失败")

    def add_message_listener(
        self, listener: Callable[[MessageRecord], Any]
    ) -> None:
        """订阅实时消息；每条入站消息到达时被调用一次（同步或异步回调）。"""
        if listener not in self._message_listeners:
            self._message_listeners.append(listener)

    def remove_message_listener(
        self, listener: Callable[[MessageRecord], Any]
    ) -> None:
        """退订实时消息。"""
        if listener in self._message_listeners:
            self._message_listeners.remove(listener)

    def self_names(self) -> set[str]:
        """当前登录账号用于识别 @/引用的昵称集合（上游记录的 bot 昵称）。"""
        bridge = self._bridge
        names = getattr(bridge, "_bot_names", None) if bridge else None
        return {str(name) for name in names} if names else set()

    def resolve_chat_identifier(self, identifier: str) -> str:
        """把会话 ID（``xxx@chatroom`` / ``wxid_xxx``）解析成可搜索的显示名。

        微信搜索框不认会话 ID，直接把 ID 当关键词会打开错误会话；因此发送前
        必须先经 contact.db 按 username 精确反查 ``remark`` / ``nick_name``。
        解析不到时返回空串，调用方应拒绝发送而不是拿 ID 去搜索。
        非 ID 形态（普通显示名）原样返回。
        """
        value = str(identifier or "").strip()
        if not value or not _looks_like_chat_id(value):
            return value
        cached = self._chat_id_names.get(value)
        if cached:
            return cached
        db = self._contact_db()
        if db is None:
            return ""
        try:
            rows = db.search_contact(value) or []
        except Exception:
            _listener_logger.debug("会话 ID 反查失败: %s", value, exc_info=True)
            return ""
        for row in rows:
            if str(row.get("username") or "").strip() != value:
                continue
            name = str(row.get("remark") or row.get("nick_name") or "").strip()
            if name and name != value:
                return name
        return ""

    @staticmethod
    def _to_record(message: Any) -> MessageRecord | None:
        chat_id = str(getattr(message, "room_id", "") or "").strip()
        chat = str(
            getattr(message, "room_name", "") or chat_id or ""
        ).strip()
        if not chat:
            return None
        return MessageRecord(
            chat=chat,
            chat_id=chat_id,
            sender=str(
                getattr(message, "sender_name", "")
                or getattr(message, "sender", "")
                or ""
            ),
            content=str(getattr(message, "content", "") or ""),
            message_type=str(getattr(message, "message_type", "text") or "text"),
            is_group=bool(getattr(message, "is_group", False)),
            timestamp=float(getattr(message, "timestamp", 0.0) or 0.0),
            message_id=str(getattr(message, "id", "") or ""),
            is_at_me=bool(getattr(message, "is_at_me", False)),
            reply_to_name=str(getattr(message, "reply_to_name", "") or ""),
            image_path=str(getattr(message, "image_path", "") or ""),
        )

    def _snapshot(self) -> list[MessageRecord]:
        with self._buffer_lock:
            return list(self._buffer)

    def _known_chat_names(self) -> list[str]:
        names: list[str] = []
        for record in self._snapshot():
            if record.chat and record.chat not in names:
                names.append(record.chat)
        return names

    def _buffer_full(self) -> bool:
        maxlen = self._buffer.maxlen
        return bool(maxlen is not None and len(self._buffer) >= maxlen)

    def _read_note(self) -> str:
        if not self._listening:
            return (
                "消息监听未启动：结果仅包含适配层启动后收到的消息，可能为空。"
            )
        return "结果基于适配层运行期间被动接收的消息，不是微信完整历史记录。"

    @staticmethod
    def _safe_call(obj: Any, name: str) -> Any:
        func = getattr(obj, name, None)
        if not callable(func):
            return None
        try:
            return func()
        except Exception:
            return None

    def _status_result(
        self,
        state: ConnectionState,
        *,
        backend: str | None = None,
        listening: bool = False,
        window_visible: bool = False,
        uia_available: bool = False,
        detail: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> StatusResult:
        return StatusResult(
            ok=state == ConnectionState.CONNECTED,
            state=state,
            backend=str(backend if backend is not None else self._config.backend),
            listening=listening,
            window_visible=window_visible,
            uia_available=uia_available,
            detail=detail or {},
            error=error,
        )
