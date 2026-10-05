"""实时自动回复引擎。

与「MCP 被动工具 + Agent 轮询」不同，引擎直接订阅适配层的实时消息：
每条入站消息到达瞬间入会话历史；当消息满足触发条件（群里 @我 / 引用回复我、
刚回复过的人继续发言 / 私聊）且过了冷却，就取最近 N 条上下文 + 人设送给大模型，
并把回复发回同一会话。触发消息会被标记为「[待回复]」，避免模型答错对象。

在此之上还有「降低存在感」与「偶发参与」两层：
- 静默时段与群级节流：命中时保持安静，不回复任何人。
- 主动参与：周期性检查群聊，小概率主动接一句；模型可回 ``[SILENT]`` 选择不发言。
"""

from __future__ import annotations

import asyncio
import base64
import math
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ..adapters.deepseekgirl import DeepSeekGirlAdapter
from ..schemas import MessageRecord
from .config import BotConfig
from . import tweet
from .links import LinkResolver, extract_urls, format_link_block
from .llm import LLMClient, LLMError
from .persona import get_persona

# 每个会话在引擎内保留的历史条数上限（上下文窗口之外的环形缓冲）。
PER_CHAT_HISTORY = 200

# 分条发送时的句子边界：中英文句末标点之后，或换行处（保留标点，不丢内容）。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])|\n+")

# 固定行为约束（不是角色人设）：明确本次要回应哪条消息，并要求口语化。
# 群里历史消息里往往有更抢眼的话题，不加约束时模型容易答错对象。
BEHAVIOR_HINT = (
    "你是自动回复助手。只针对最后一条标记「[待回复]」的消息作答，"
    "它是刚刚 @ 你或引用你的那条消息；更早的消息仅用于理解指代和语境，"
    "不要改变本次要回答的话题。"
    "像真人用微信聊天那样回复：口语化、简短自然，多用短句，"
    "可以自然地用「嗯」「哈哈」「好呀」这类语气词，"
    "不要像客服或 AI 那样一本正经地长篇大论；"
    "一般两三句话以内，对方明确要求详细说明时才展开。"
    "回复必须是微信可直接发送的纯文本：禁止使用任何 Markdown 标记，"
    "例如 **加粗**、*斜体*、# 标题、- 列表符号、``` 代码块或表格；"
    "需要分点时用「1. 2. 3.」或分号自然叙述，不要出现这些符号。"
)

# 主动参与（水群）时的行为约束：允许模型自己判断「这次不发言」。
SILENT_SENTINEL = "[SILENT]"
PROACTIVE_HINT = (
    "你正在一个微信群里潜水。以下是最近的群聊记录，"
    "你可以像普通群成员一样自然地接一句，也可以选择不发言。"
    "只有在确实有话可说、能自然融入当前话题时才发言；"
    "如果话题与你无关、或没有合适的话，必须只回复 "
    f"{SILENT_SENTINEL} 这一串字符，不要输出任何其它内容。"
    "发言时口语化、随意，一般一句话以内，像真人水群那样，不要长篇大论。"
    "回复必须是微信可直接发送的纯文本：禁止使用任何 Markdown 标记"
    "（如 **加粗**、# 标题、- 列表、``` 代码块或表格）；"
    "需要分点时用「1. 2. 3.」或分号自然叙述。"
)

# 可交给视觉模型的图片类型与单张体积上限（超过则跳过，避免请求体过大）。
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _image_data_uri(path: str) -> str | None:
    """把本地图片读成 data URI；类型不支持、文件缺失或过大时返回 None。"""
    text = str(path or "").strip()
    if not text:
        return None
    file = Path(text)
    mime = _IMAGE_MIME.get(file.suffix.lower())
    if mime is None or not file.is_file():
        return None
    try:
        if file.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        encoded = base64.b64encode(file.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{mime};base64,{encoded}"


def _send_error(result: Any) -> str:
    """从发送结果里取一句可读的错误说明。"""
    error = getattr(result, "error", None)
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "未知原因")
    return str(error or "未知原因")


@dataclass
class HistoryItem:
    timestamp: float
    role: str  # "user" / "assistant"
    sender: str
    content: str
    is_group: bool = False
    # 该条消息对应的本地图片路径（非图片为空串），供视觉模型识别。
    image_path: str = ""


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
        # 已做过「链接服务」的消息 ID。与 _processed_ids 分开：链接服务独立于
        # 是否触发大模型回复，同一条消息可能既发固定提示、又触发一次模型回复。
        self._link_handled_ids: deque[str] = deque(maxlen=1000)
        # 会话键 → (最近被回复的发送者, 回复时间)，用于会话延续触发。
        self._last_reply_to: dict[str, tuple[str, float]] = {}
        # 会话键 → 最近一条入站消息，供主动参与挑选目标。
        self._last_inbound: dict[str, MessageRecord] = {}
        # 会话键 → 最近的回复时间戳，用于群级节流。
        self._reply_times: dict[str, deque[float]] = {}
        # 会话键 → 该群下次允许主动发言的时间，避免连续冒泡。
        self._proactive_cooldown_until: dict[str, float] = {}

        self._consume_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._running = False

        self._date_key = time.strftime("%Y-%m-%d")
        self.reply_count_today = 0
        self.last_trigger_at: float | None = None

        # 链接解析（内置 yt-dlp）：结果带缓存，跨消息复用。
        self._links = LinkResolver(
            cache_size=256,
            timeout=self._config.link_parse_timeout,
            logger=self._log,
        )
        # 「下载并回发」在后台执行：大视频要下几分钟，而 _consume 串行消费队列，
        # 若在 _handle 里 await 会把后续消息全卡住。这里跟踪在跑的任务与 URL，
        # 同一个链接不会并发下载多次。
        self._download_tasks: set[asyncio.Task] = set()
        self._downloading_urls: set[str] = set()

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
        # 解析超时随配置热更新；缓存保留，避免重复解析。
        self._links.timeout = config.link_parse_timeout

    # ------------------------------------------------------------------ 启停

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._adapter.add_message_listener(self._enqueue)
        self._consume_task = asyncio.create_task(self._consume())
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        self._log("info", "自动回复已开启。")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._adapter.remove_message_listener(self._enqueue)
        for attr in ("_consume_task", "_proactive_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        for task in list(self._download_tasks):
            task.cancel()
        self._download_tasks.clear()
        self._downloading_urls.clear()
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
        key = self._chat_key(record)
        self._bucket(key).append(
            HistoryItem(
                timestamp=record.timestamp or time.time(),
                role="user",
                sender=record.sender,
                content=record.content,
                is_group=record.is_group,
                image_path=record.image_path,
            )
        )
        # 记住最近一条入站消息，供主动参与挑选目标会话。
        self._last_inbound[key] = record

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

    def _in_group_scope(self, record: MessageRecord) -> bool:
        """会话是否落在配置的群聊作用范围内（群名或会话 ID 都认）。"""
        config = self._config
        if config.all_groups:
            return True
        return bool(
            record.chat in config.groups
            or (record.chat_id and record.chat_id in config.groups)
        )

    def _should_reply(self, record: MessageRecord) -> bool:
        if not record.content:
            return False
        if not record.is_group:
            return self._config.reply_private
        if not self._in_group_scope(record):
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

        # 链接服务：与「这条消息要不要回」无关。群里没 @ 机器人也该解析，
        # 因为这是确定性动作，不是「接话」。放在 _should_reply 之前。
        acked = await self._handle_links(record)

        if not self._should_reply(record):
            return

        if acked and not self._config.link_llm_followup:
            # 已经用固定文案应过声了，按配置不再让模型就这条消息接话。
            self._log(
                "info",
                f"已回固定链接提示，按配置不再让模型接话 [{record.chat}]。",
            )
            return

        now = time.time()
        key = self._cooldown_key(record)
        if now < self._cooldown_until.get(key, 0.0):
            self._log("debug", f"冷却中，跳过 [{record.sender}]。")
            return

        # 静默时段：任何人（含 @我）都不回复。
        if self._in_quiet_hours(now):
            self._log("info", f"静默时段，跳过 [{record.chat}] {record.sender}。")
            return

        # 群级节流：同一群在窗口内回复过多时保持静默。
        if record.is_group and self._rate_limited(self._chat_key(record), now):
            self._log(
                "info",
                f"群级节流中（{self._config.group_rate_window_minutes:.0f} 分钟内"
                f"已达 {self._config.group_rate_max_replies} 条），跳过 "
                f"[{record.chat}]。",
            )
            return

        if record.message_id:
            self._processed_ids.append(record.message_id)
        self._cooldown_until[key] = now + self._config.cooldown_seconds
        self.last_trigger_at = now

        # 拟人化：按概率决定这次到底回不回，避免「有求必应」的机械感。
        if self._config.humanize and random.random() > self._config.reply_probability:
            self._log(
                "info",
                f"拟人化：本次不回复 [{record.chat}] {record.sender}。",
            )
            return

        self._log(
            "info",
            f"触发 [{record.chat}] {record.sender}：{record.content[:40]}",
        )

        chat_key = self._chat_key(record)
        # 先把上下文里的链接解析好，模型才能在下面看到「[链接] …」信息。
        if self._config.link_parse_enabled:
            await self._prefetch_links(chat_key)
        messages = self._build_messages(chat_key)
        try:
            client = self._llm_factory(self._config)
            reply = await client.chat(messages)
        except LLMError as exc:
            # 不回滚冷却：Key 缺失/无效时避免每条消息都重试报错。
            self._log("error", f"模型调用失败：{exc.message}")
            return

        if not await self._send_reply(record.chat, reply):
            return

        self._record_assistant(chat_key, reply)
        self._last_reply_to[chat_key] = (record.sender, time.time())
        self._note_reply(chat_key)
        self.reply_count_today += 1
        parts = self._split_reply(reply)
        if len(parts) > 1:
            self._log(
                "info",
                f"已回复 [{record.chat}]（{len(parts)} 条）：{reply[:40]}",
            )
        else:
            self._log("info", f"已回复 [{record.chat}]：{reply[:40]}")

        # 链接内容的「下载并回发」已由 _handle_links 统一调度（无论这条消息
        # 是否触发模型回复），这里不再重复调度，避免同一链接被下载两次。

    async def _send_reply(self, chat_display: str, reply: str) -> bool:
        """按拟人化规则分条发送回复；任一条失败即停止并返回 False。"""
        parts = self._split_reply(reply)
        for index, part in enumerate(parts):
            # 发送目标用显示名；适配层会把「退化成的会话 ID」反查成可搜索名。
            result = await self._adapter.send_message(chat_display, part)
            if not result.ok:
                message = (result.error or {}).get("message", "结果未确认")
                self._log("error", f"回复发送失败：{message}")
                return False
            if index < len(parts) - 1:
                await asyncio.sleep(
                    random.uniform(
                        self._config.split_delay_min,
                        self._config.split_delay_max,
                    )
                )
        return True

    # ------------------------------------------------------------------ 静默 / 节流

    @staticmethod
    def _parse_hm(value: str) -> int | None:
        """把 ``HH:MM`` 解析成当天分钟数；非法返回 None。"""
        text = str(value or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    def _in_quiet_hours(self, now: float | None = None) -> bool:
        """当前是否处于静默时段（支持跨零点，如 23:00-08:00）。"""
        config = self._config
        if not config.quiet_hours_enabled:
            return False
        start = self._parse_hm(config.quiet_hours_start)
        end = self._parse_hm(config.quiet_hours_end)
        if start is None or end is None or start == end:
            return False
        local = time.localtime(now if now is not None else time.time())
        current = local.tm_hour * 60 + local.tm_min
        if start < end:
            return start <= current < end
        # 跨零点：如 23:00-08:00，命中条件是「已过 start」或「未到 end」。
        return current >= start or current < end

    def _rate_limited(self, chat_key: str, now: float) -> bool:
        """该会话在节流窗口内是否已达回复上限。"""
        config = self._config
        if not config.group_rate_enabled:
            return False
        window = max(0.0, config.group_rate_window_minutes) * 60.0
        limit = max(0, config.group_rate_max_replies)
        if window <= 0 or limit <= 0:
            return False
        times = self._reply_times.get(chat_key)
        if not times:
            return False
        while times and now - times[0] > window:
            times.popleft()
        return len(times) >= limit

    def _note_reply(self, chat_key: str, now: float | None = None) -> None:
        """记录一次成功回复，供群级节流统计。"""
        limit = max(1, int(self._config.group_rate_max_replies))
        bucket = self._reply_times.setdefault(chat_key, deque(maxlen=limit))
        bucket.append(now if now is not None else time.time())

    def _split_reply(self, reply: str) -> list[str]:
        """把较长回复拆成多条短消息，模拟真人分句连发。

        关闭拟人化 / 分条、回复不够长、或拆不出多句时，原样返回单条。
        拆分只切分不丢内容：拼接各条应能还原原文（空白除外）。
        """
        text = str(reply or "").strip()
        if not text:
            return []
        config = self._config
        if not (config.humanize and config.split_replies):
            return [text]
        if len(text) < config.split_min_length:
            return [text]

        sentences = [
            segment.strip()
            for segment in _SENTENCE_SPLIT_RE.split(text)
            if segment.strip()
        ]
        if len(sentences) <= 1:
            return [text]

        max_parts = max(1, config.split_max_parts)
        if len(sentences) <= max_parts:
            return sentences

        # 句子数超过上限：按长度均衡地合并成 max_parts 条。
        target = math.ceil(len(text) / max_parts)
        parts: list[str] = []
        buffer = ""
        for sentence in sentences:
            if (
                buffer
                and len(buffer) + len(sentence) > target
                and len(parts) < max_parts - 1
            ):
                parts.append(buffer)
                buffer = sentence
            else:
                buffer += sentence
        if buffer:
            parts.append(buffer)
        return parts

    def _build_messages(self, chat: str) -> list[dict[str, Any]]:
        recent = self._context_items(chat)
        if not recent:
            return []
        # 触发消息是本会话最后一条入站消息，标记出来避免被更早的话题带偏。
        mark_index = len(recent) - 1 if recent[-1].role == "user" else -1
        return self._render_context(recent, mark_index, BEHAVIOR_HINT)

    def _render_context(
        self, recent: list[HistoryItem], mark_index: int, hint: str
    ) -> list[dict[str, Any]]:
        """把最近上下文渲染成模型消息；开启视觉时把图片附到对应 user 消息。"""
        parts = [hint]
        persona = get_persona(self._config).strip()
        if persona:
            parts.append(persona)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "\n\n".join(parts)}
        ]
        image_slots = self._image_slots(recent)
        for idx, item in enumerate(recent):
            prefix = "[待回复] " if idx == mark_index else ""
            if item.role == "assistant":
                messages.append({"role": "assistant", "content": item.content})
                continue
            if item.is_group and item.sender:
                text = f"{prefix}{item.sender}: {item.content}"
            else:
                text = f"{prefix}{item.content}"
            # 已解析的链接信息附在该条消息后，模型据此回应链接内容。
            link_block = self._link_block(item)
            if link_block:
                text = f"{text}\n{link_block}"
            uri = _image_data_uri(item.image_path) if idx in image_slots else None
            if uri:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": uri}},
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": text})
        return messages

    def _image_slots(self, recent: list[HistoryItem]) -> set[int]:
        """挑出允许带图的位置：从最新往前取，最多 vision_max_images 张。"""
        config = self._config
        if not config.vision_enabled or config.vision_max_images <= 0:
            return set()
        slots: set[int] = set()
        for idx in range(len(recent) - 1, -1, -1):
            item = recent[idx]
            if item.role == "user" and item.image_path:
                slots.add(idx)
                if len(slots) >= config.vision_max_images:
                    break
        return slots

    # ------------------------------------------------------------------ 链接解析

    def _context_items(self, chat: str) -> list[HistoryItem]:
        """当前会话送给模型的最近上下文（受 context_messages 限制）。"""
        items = self._history.get(chat)
        if not items:
            return []
        return list(items)[-self._config.context_messages :]

    async def _prefetch_links(self, chat: str) -> None:
        """解析上下文里出现过的链接（带缓存，已解析过的不会重复请求）。"""
        urls: list[str] = []
        for item in self._context_items(chat):
            if item.role == "user" and item.content:
                urls.extend(extract_urls(item.content))
        if not urls:
            return
        try:
            await self._links.resolve_many(urls, limit=self._config.link_parse_max)
        except Exception as exc:  # 解析失败不应影响正常回复
            self._log("warning", f"链接解析异常：{exc}")

    def _link_block(self, item: HistoryItem) -> str:
        """该条消息里「已解析」链接的上下文块；无则返回空串。"""
        if not self._config.link_parse_enabled or item.role != "user":
            return ""
        if not item.content:
            return ""
        infos = [
            info
            for info in (
                self._links.cached(url) for url in extract_urls(item.content)
            )
            if info is not None
        ]
        return format_link_block(infos)

    async def _handle_links(self, record: MessageRecord) -> bool:
        """链接服务：识别到链接就回一句**固定**提示，并把链接内容处理掉。

        刻意**独立于** ``_should_reply``：群聊里没 @ 机器人时也要解析链接，
        因为这是确定性的服务动作，不是「接话」。提示语由配置写死，
        **不经过大模型**（``link_ack_text``），避免为一句「正在解析链接」烧 token。

        返回是否真的发出了固定提示（供 ``link_llm_followup`` 判断要不要再让模型接话）。
        """
        config = self._config
        if not config.link_parse_enabled:
            return False
        if not extract_urls(record.content):
            return False
        # 群聊作用范围：只在配置允许的群里动手，避免在无关群里刷屏。
        if record.is_group and not self._in_group_scope(record):
            return False
        if record.message_id:
            if record.message_id in self._link_handled_ids:
                return False
            self._link_handled_ids.append(record.message_id)

        # 静默时段：不往聊天里发任何东西（解析本身照旧，供上下文使用）。
        if self._in_quiet_hours():
            self._log("info", f"静默时段，跳过链接提示 [{record.chat}]。")
            return False

        ack = config.link_ack_text.strip()
        if not (config.link_ack_enabled and ack):
            # 提示关掉时仍要把链接内容回发，否则这个功能就完全没动作了。
            if config.link_download_enabled:
                self._schedule_download(record)
            return False

        if await self._send_reply(record.chat, ack):
            self._log("info", f"已发送链接提示 [{record.chat}]：{ack}")
            sent = True
        else:
            self._log("warning", f"链接提示发送失败 [{record.chat}]。")
            sent = False

        # 下载并回发（含推文卡片）在后台跑，不阻塞消息消费循环。
        if config.link_download_enabled:
            self._schedule_download(record)
        return sent

    def _schedule_download(self, record: MessageRecord) -> None:
        """把「下载并回发」放到后台执行，避免拖住消息消费循环。

        同一个链接在下载完成前不会重复起任务（用户可能连着发几次同一条链接）。
        """
        urls = extract_urls(record.content)[: self._config.link_download_max]
        fresh = [url for url in urls if url not in self._downloading_urls]
        if not fresh:
            return
        self._downloading_urls.update(fresh)
        task = asyncio.create_task(self._download_and_send(record))
        self._download_tasks.add(task)

        def _done(_task: "asyncio.Task[None]") -> None:
            self._download_tasks.discard(task)
            self._downloading_urls.difference_update(fresh)

        task.add_done_callback(_done)

    async def _download_and_send(self, record: MessageRecord) -> None:
        """把触发消息里链接的内容发回当前聊天。

        X（Twitter）推文走特殊流程：先本地渲染一张推文卡片图发出去，
        推文里**含视频**时再下载视频一并回发；其它链接沿用「下载后回发」。
        """
        urls = extract_urls(record.content)[: self._config.link_download_max]
        if not urls:
            return
        outdir = self._config.download_dir()
        self._ensure_download_dir_allowed(outdir)
        for url in urls:
            if tweet.is_tweet_url(url):
                await self._send_tweet(record, url, outdir)
            else:
                await self._download_and_send_one(record, url, outdir)

    async def _send_tweet(self, record: MessageRecord, url: str, outdir: Path) -> None:
        """X 推文：发卡片图（可选）；含视频则再下载视频回发。"""
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None,
            lambda: tweet.fetch_tweet_cached(
                url, timeout=self._config.link_parse_timeout, logger=self._log
            ),
        )
        if info is None:
            # 抓不到推文（已删除/网络不通）时退回通用下载，尽量给出点东西。
            self._log("warning", f"推文抓取失败，改为直接下载：{url}")
            await self._download_and_send_one(record, url, outdir)
            return

        if self._config.link_tweet_card_enabled:
            card = await loop.run_in_executor(
                None,
                lambda: tweet.render_card(
                    info, outdir, timeout=self._config.link_parse_timeout, logger=self._log
                ),
            )
            if card:
                await self._send_file(record.chat, card, "推文卡片")
            else:
                self._log("warning", f"推文卡片渲染失败：{url}")

        if info.has_video:
            await self._send_tweet_video(record, info, url, outdir)
        else:
            self._log("info", f"推文无视频，仅发卡片：{url}")

    async def _send_tweet_video(
        self, record: MessageRecord, info: "tweet.Tweet", url: str, outdir: Path
    ) -> None:
        """推文含视频时下载并回发。

        优先用 syndication 给的 mp4 直链（``Tweet.video_url``）——比走 yt-dlp 的
        X 提取器可靠（后者默认 GraphQL，未登录易 403）；拿不到直链或直链下载失败
        时再退回 yt-dlp 通用下载。
        """
        loop = asyncio.get_running_loop()
        if info.video_url:
            path = await loop.run_in_executor(
                None,
                lambda: tweet.download_video(
                    info,
                    outdir,
                    timeout=max(self._config.link_parse_timeout, 60.0),
                    logger=self._log,
                    max_mb=self._config.link_download_max_mb,
                ),
            )
            if path and await self._send_file(record.chat, path, "推文视频"):
                return
            self._log("warning", f"推文视频直链下载失败，改用 yt-dlp 兜底：{url}")
        await self._download_and_send_one(record, url, outdir)

    async def _download_and_send_one(
        self, record: MessageRecord, url: str, outdir: Path
    ) -> None:
        self._log("info", f"开始下载链接内容：{url}")
        path = await self._links.download(
            url, str(outdir), max_mb=self._config.link_download_max_mb
        )
        if not path:
            self._log("warning", f"下载失败或超出体积上限，已跳过：{url}")
            return
        await self._send_file(record.chat, path, "链接内容")

    async def _send_file(self, chat: str, path: str | Path, label: str) -> bool:
        """把文件发回聊天；成功返回 True。"""
        try:
            result = await self._adapter.send_file(chat, str(path))
        except Exception as exc:
            self._log("warning", f"{label} 发送异常：{exc}")
            return False
        if getattr(result, "ok", False):
            self._log("info", f"已发回{label}：{Path(path).name}")
            return True
        self._log("warning", f"{label} 发送未成功：{_send_error(result)}")
        return False

    def _ensure_download_dir_allowed(self, outdir: Path) -> None:
        """把下载目录纳入发送白名单。

        该目录由本程序创建、内容可控，故可安全放行；不这样做的话，适配层的
        「未配置允许目录即拒发」会让自动回发永远失败。
        """
        config = getattr(self._adapter, "config", None)
        if config is None:
            return
        try:
            resolved = Path(outdir).expanduser().resolve()
        except OSError:
            return
        existing = tuple(getattr(config, "allow_send_dirs", ()) or ())
        for item in existing:
            try:
                if Path(str(item)).expanduser().resolve() == resolved:
                    return
            except OSError:
                continue
        try:
            config.allow_send_dirs = existing + (resolved,)
        except Exception:  # 配置不可写时静默：发送时会给出明确错误
            pass

    # ------------------------------------------------------------------ 主动参与

    async def _proactive_loop(self) -> None:
        """周期性检查群聊，偶发主动接一句（水群）。

        循环常驻：即使当前未开启也会轮询配置，便于界面热开热关。
        """
        while self._running:
            if not self._config.proactive_enabled:
                await asyncio.sleep(5)
                continue
            delay = random.uniform(
                self._config.proactive_interval_min,
                self._config.proactive_interval_max,
            )
            await asyncio.sleep(delay)
            if not self._running:
                break
            if not self._config.proactive_enabled:
                continue
            try:
                await self._proactive_tick()
            except Exception as exc:  # 兜底：单次异常不影响循环
                self._log("error", f"主动参与检查异常：{exc}")

    def _proactive_candidates(self, now: float) -> list[tuple[str, MessageRecord]]:
        """筛选出当前可以考虑主动发言的群。"""
        config = self._config
        candidates: list[tuple[str, MessageRecord]] = []
        for key, record in list(self._last_inbound.items()):
            if not record.is_group or not self._in_group_scope(record):
                continue
            items = self._history.get(key)
            if not items:
                continue
            last = items[-1]
            # 只有「别人刚说过话」时才考虑接话，自己刚说过就安静。
            if last.role != "user":
                continue
            if now - (last.timestamp or 0.0) > config.proactive_recent_seconds:
                continue
            if now < self._proactive_cooldown_until.get(key, 0.0):
                continue
            if self._rate_limited(key, now):
                continue
            candidates.append((key, record))
        return candidates

    async def _proactive_tick(self) -> None:
        """一次主动参与检查：至多让一个群发言。"""
        now = time.time()
        if self._in_quiet_hours(now):
            return
        candidates = self._proactive_candidates(now)
        if not candidates:
            return
        # 先掷一次骰子，再随机挑一个群，避免「群越多越爱说话」。
        if random.random() > self._config.proactive_probability:
            return
        key, record = random.choice(candidates)
        await self._speak_proactively(key, record, now)

    async def _speak_proactively(
        self, key: str, record: MessageRecord, now: float
    ) -> None:
        if self._config.link_parse_enabled:
            await self._prefetch_links(key)
        messages = self._build_proactive_messages(key)
        if not messages:
            return
        try:
            client = self._llm_factory(self._config)
            reply = await client.chat(messages)
        except LLMError as exc:
            self._log("error", f"主动参与模型调用失败：{exc.message}")
            return

        # 模型判断此刻没有合适的话要说：不发言，并进入该群的主动冷却。
        if not reply or reply.strip() == SILENT_SENTINEL:
            self._log("debug", f"主动参与：本次不发言 [{record.chat}]。")
            self._proactive_cooldown_until[key] = (
                now + self._config.proactive_per_group_cooldown
            )
            return

        if not await self._send_reply(record.chat, reply):
            return
        self._record_assistant(key, reply)
        self._note_reply(key)
        self._proactive_cooldown_until[key] = (
            time.time() + self._config.proactive_per_group_cooldown
        )
        self.reply_count_today += 1
        self._log("info", f"主动参与 [{record.chat}]：{reply[:40]}")

    def _build_proactive_messages(self, chat: str) -> list[dict[str, Any]]:
        """为主动参与构造上下文：不加「[待回复]」标记，改用 PROACTIVE_HINT。"""
        recent = self._context_items(chat)
        if not recent:
            return []
        return self._render_context(recent, -1, PROACTIVE_HINT)

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
