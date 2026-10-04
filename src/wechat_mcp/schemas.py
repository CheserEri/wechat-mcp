"""适配层输入/输出模型。

统一使用 dataclass，保持与第三方库解耦，并可无损序列化为 dict 供 MCP 工具返回。
所有结构都显式区分「操作是否成功」与「业务状态」，避免把失败表达为成功。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _serialize(value: Any) -> Any:
    """把 Enum / dataclass / 容器递归转换为 JSON 友好的结构。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class SendStatus(str, Enum):
    SENT = "sent"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    # 配置要求发送前确认，但本次调用未显式确认：仅返回预览，不执行发送。
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass
class StatusResult:
    """微信连接与自动化后端状态。"""

    ok: bool
    state: ConnectionState
    backend: str = ""
    listening: bool = False
    window_visible: bool = False
    uia_available: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class MessageRecord:
    """单条消息记录。"""

    chat: str
    sender: str
    content: str
    message_type: str = "text"
    is_group: bool = False
    timestamp: float | None = None
    message_id: str = ""
    # 适配层内单调递增序号，用于 get_recent_messages 的增量游标。
    seq: int = 0
    # 是否 @ 当前登录账号（透传上游 WeChatMessage.is_at_me）。
    is_at_me: bool = False
    # 引用/回复消息中被引用者的显示名；非引用消息为空串。
    reply_to_name: str = ""
    # 会话原始 ID（群聊形如 ``xxxx@chatroom``，私聊为 wxid）。
    # ``chat`` 是显示名，可能因解析失败退化成 ID；本字段始终是稳定标识，
    # 供引擎做历史分桶 / 冷却 / 群范围匹配。
    chat_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ChatSummary:
    """会话摘要（基于适配层运行期间观察到的消息）。"""

    name: str
    is_group: bool = False
    message_count: int = 0
    last_message_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ChatListResult:
    ok: bool
    chats: list[ChatSummary] = field(default_factory=list)
    total: int = 0
    complete: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ChatHistoryResult:
    ok: bool
    chat: str = ""
    messages: list[MessageRecord] = field(default_factory=list)
    complete: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class SendResult:
    """发送结果。ok 为 True 仅当能够确认发送成功。"""

    ok: bool
    status: SendStatus
    recipient: str = ""
    resolved_recipient: str = ""
    message_preview: str = ""
    dry_run: bool = False
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class FileSendResult:
    """文件发送结果。ok 为 True 仅当能够确认发送成功。"""

    ok: bool
    status: SendStatus
    recipient: str = ""
    resolved_recipient: str = ""
    file_path: str = ""
    file_name: str = ""
    file_size: int = 0
    dry_run: bool = False
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ContactCandidate:
    """联系人/群聊搜索结果项。"""

    name: str
    username: str = ""
    remark: str = ""
    is_group: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ContactSearchResult:
    """联系人/群聊搜索结果。不自行猜测同名项，原样返回候选列表。"""

    ok: bool
    keyword: str = ""
    candidates: list[ContactCandidate] = field(default_factory=list)
    total: int = 0
    source: str = ""
    note: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ChatInfoResult:
    """会话摘要信息（基于可确认获取的字段）。"""

    ok: bool
    name: str = ""
    is_group: bool = False
    message_count: int = 0
    last_message_at: float | None = None
    monitored: bool = True
    note: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class RecentMessagesResult:
    """增量读取结果。``next_seq`` 作为下一次调用的游标。"""

    ok: bool
    messages: list[MessageRecord] = field(default_factory=list)
    next_seq: int = 0
    complete: bool = False
    note: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class MonitoredChatsResult:
    """会话关注列表变更结果。"""

    ok: bool = True
    mode: str = "allow"
    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)
    note: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))
