"""实时自动回复引擎。

与「MCP 被动工具 + Agent 轮询」不同，引擎直接订阅适配层的实时消息：
每条入站消息到达瞬间入会话历史；当消息满足触发条件（群里 @我 / 引用回复我、
刚回复过的人继续发言 / 私聊）且过了冷却，就取最近 N 条上下文 + 人设送给大模型，
并把回复发回同一会话。触发消息会被标记为「[待回复]」，避免模型答错对象。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..adapters.deepseekgirl import DeepSeekGirlAdapter
from ..schemas import MessageRecord
from .config import BotConfig
from .llm import LLMClient, LLMError
from .persona import get_persona

# 每个会话在引擎内保留的历史条数上限（上下文窗口之外的环形缓冲）。
PER_CHAT_HISTORY = 200

# 固定行为约束（不是角色人设）：明确本次要回应哪条消息。
# 群里历史消息里往往有更抢眼的话题，不加约束时模型容易答错对象。
BEHAVIOR_HINT = (
    "你是自动回复助手。只针对最后一条标记「[待回复]」的消息作答，"
    "它是刚刚 @ 你或引用你的那条消息；更早的消息仅用于理解指代和语境，"
    "不要改变本次要回答的话题。"
    "回复必须是微信可直接发送的纯文本：禁止使用任何 Markdown 标记，"
    "例如 **加粗**、*斜体*、# 标题、- 列表符号、``` 代码块或表格；"
    "需要分点时用「1. 2. 3.」或分号自然叙述，不要出现这些符号。"
)


@dataclass
class HistoryItem:
    timestamp: float
    role: str  # "user" / "assistant"
    sender: str
    content: str
    is_group: bool = False


@dataclass
class LogLine:
    seq: int
    level: str
    message: str
    timestamp: float


class BotEngine:
    """订阅实时消息并按需自动回复。"""

    def __init__(
        self,
        adapter: DeepSeekGirlAdapter,
        config: BotConfig | None = None,
        llm_factory: Callable[[BotConfig], Any] | None = None,
    ):
        self._adapter = adapter
        self._config = config or BotConfig.load()
        self._llm_factory = llm_factory or (lambda cfg: LLMClient(cfg))

        self._queue: asyncio.Queue[MessageRecord] = asyncio.Queue()
        self._history: dict[str, deque[HistoryItem]] = {}
        self._log_lines: deque[LogLine] = deque(maxlen=500)
        self._log_seq = 0
        self._cooldown_until: dict[str, float] = {}
        self._processed_ids: deque[str] = deque(maxlen=1000)
        # 会话键 → (最近被回复的发送者, 回复时间)，用于会话延续触发。
        self._last_reply_to: dict[str, tuple[str, float]] = {}

        self._consume_task: asyncio.Task | None = None
        self._running = False

        self._date_key = time.strftime("%Y-%m-%d")
        self.reply_count_today = 0
        self.last_trigger_at: float | None = None

        # 桌面壳可注入：新日志产生时回调（用于推送到前端）。
        self.on_log: Callable[[LogLine], Any] | None = None

    # ------------------------------------------------------------------ 配置

    @property
    def config(self) -> BotConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._running

    def update_config(self, config: BotConfig) -> None:
        """界面保存配置后热替换（下次处理即生效）。"""
        self._config = config

    # ------------------------------------------------------------------ 启停

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._adapter.add_message_listener(self._enqueue)
        self._consume_task = asyncio.create_task(self._consume())
        self._log("info", "自动回复已开启。")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._adapter.remove_message_listener(self._enqueue)
        if self._consume_task is not None:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
            self._consume_task = None
        self._log("info", "自动回复已停止。")

    def _enqueue(self, record: MessageRecord) -> None:
        # 适配层回调在事件循环线程中同步调用，put_nowait 安全。
        self._queue.put_nowait(record)

    async def _consume(self) -> None:
        while self._running:
            record = await self._queue.get()
            self._record_inbound(record)
            try:
                await self._handle(record)
            except Exception as exc:  # 兜底：单条异常不影响消费循环
                self._log("error", f"处理消息异常：{exc}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 历史

    def _bucket(self, chat: str) -> deque[HistoryItem]:
        return self._history.setdefault(
            chat, deque(maxlen=PER_CHAT_HISTORY)
        )

    @staticmethod
    def _chat_key(record: MessageRecord) -> str:
        """会话稳定标识：优先用会话 ID，显示名可能是解析失败的退化值。"""
        return record.chat_id or record.chat

    def _record_inbound(self, record: MessageRecord) -> None:
        self._bucket(self._chat_key(record)).append(
            HistoryItem(
                timestamp=record.timestamp or time.time(),
                role="user",
                sender=record.sender,
                content=record.content,
                is_group=record.is_group,
            )
        )

    def _record_assistant(self, chat: str, content: str) -> None:
        self._bucket(chat).append(
            HistoryItem(
                timestamp=time.time(),
                role="assistant",
                sender="",
                content=content,
            )
        )

    # ------------------------------------------------------------------ 触发

    def _should_reply(self, record: MessageRecord) -> bool:
        if not record.content:
            return False
        if not record.is_group:
            return self._config.reply_private
        # 群聊作用范围（配置可写群名或会话 ID，两者都认）
        if not self._config.all_groups and not (
            record.chat in self._config.groups
            or (record.chat_id and record.chat_id in self._config.groups)
        ):
            return False
        if self._config.trigger_at and record.is_at_me:
            return True
        if self._config.trigger_reply and self._is_reply_to_me(record):
            return True
        return self._is_continuation(record)

    def _is_continuation(self, record: MessageRecord) -> bool:
        """会话延续：刚被回复过的发送者在窗口内继续发言，接着回复他。"""
        window = self._config.continuation_seconds
        if window <= 0:
            return False
        last = self._last_reply_to.get(self._chat_key(record))
        if not last:
            return False
        sender, ts = last
        return sender == record.sender and (time.time() - ts) <= window

    def _is_reply_to_me(self, record: MessageRecord) -> bool:
        target = record.reply_to_name.strip()
        if not target:
            return False
        names = self._adapter.self_names()
        if not names:
            # 上游尚未记录到自身昵称时，非空引用目标按「回复本人」处理。
            return True
        return target in names

    @classmethod
    def _cooldown_key(cls, record: MessageRecord) -> str:
        key = cls._chat_key(record)
        return key if not record.is_group else f"{key}::{record.sender}"

    # ------------------------------------------------------------------ 处理

    async def _handle(self, record: MessageRecord) -> None:
        self._roll_day()

        if record.message_id and record.message_id in self._processed_ids:
            return
        if not self._should_reply(record):
            return

        now = time.time()
        key = self._cooldown_key(record)
        if now < self._cooldown_until.get(key, 0.0):
            self._log("debug", f"冷却中，跳过 [{record.sender}]。")
            return

        if record.message_id:
            self._processed_ids.append(record.message_id)
        self._cooldown_until[key] = now + self._config.cooldown_seconds
        self.last_trigger_at = now
        self._log(
            "info",
            f"触发 [{record.chat}] {record.sender}：{record.content[:40]}",
        )

        key = self._chat_key(record)
        messages = self._build_messages(key)
        try:
            client = self._llm_factory(self._config)
            reply = await client.chat(messages)
        except LLMError as exc:
            # 不回滚冷却：Key 缺失/无效时避免每条消息都重试报错。
            self._log("error", f"模型调用失败：{exc.message}")
            return

        # 发送目标用显示名；适配层会把「退化成的会话 ID」反查成可搜索名。
        result = await self._adapter.send_message(record.chat, reply)
        if not result.ok:
            message = (result.error or {}).get("message", "结果未确认")
            self._log("error", f"回复发送失败：{message}")
            return

        self._record_assistant(key, reply)
        self._last_reply_to[key] = (record.sender, time.time())
        self.reply_count_today += 1
        self._log("info", f"已回复 [{record.chat}]：{reply[:40]}")

    def _build_messages(self, chat: str) -> list[dict[str, str]]:
        items = self._history.get(chat)
        if not items:
            return []
        recent = list(items)[-self._config.context_messages :]
        # 触发消息是本会话最后一条入站消息，标记出来避免被更早的话题带偏。
        mark_index = len(recent) - 1 if recent[-1].role == "user" else -1

        parts = [BEHAVIOR_HINT]
        persona = get_persona(self._config).strip()
        if persona:
            parts.append(persona)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": "\n\n".join(parts)}
        ]
        for idx, item in enumerate(recent):
            prefix = "[待回复] " if idx == mark_index else ""
            if item.role == "assistant":
                messages.append({"role": "assistant", "content": item.content})
            elif item.is_group and item.sender:
                messages.append(
                    {
                        "role": "user",
                        "content": f"{prefix}{item.sender}: {item.content}",
                    }
                )
            else:
                messages.append(
                    {"role": "user", "content": f"{prefix}{item.content}"}
                )
        return messages

    # ------------------------------------------------------------------ 观测

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "reply_count_today": self.reply_count_today,
            "last_trigger_at": self.last_trigger_at,
            "wechat": self._adapter.get_status().to_dict(),
        }

    def get_logs(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [
            asdict(line)
            for line in self._log_lines
            if line.seq > int(after_seq or 0)
        ]

    def _log(self, level: str, message: str) -> None:
        self._log_seq += 1
        line = LogLine(
            seq=self._log_seq,
            level=level,
            message=message,
            timestamp=time.time(),
        )
        self._log_lines.append(line)
        if self.on_log is not None:
            try:
                self.on_log(line)
            except Exception:
                pass

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._date_key:
            self._date_key = today
            self.reply_count_today = 0
