"""常驻自动回复 bot 的配置与本地持久化。

配置以 JSON 存在用户私有目录（默认 ``%APPDATA%\\wechat-mcp\\bot.json``），
可用环境变量 ``WECHAT_BOT_CONFIG`` 指定其他路径。``api_key`` 仅落盘在该
本地私有文件中，不进入仓库、不随包分发。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_config_path() -> Path:
    """返回 bot 配置文件路径：WECHAT_BOT_CONFIG 优先，否则 %APPDATA%\\wechat-mcp。"""
    raw = os.environ.get("WECHAT_BOT_CONFIG", "").strip()
    if raw:
        return Path(raw)
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "wechat-mcp" / "bot.json"


@dataclass
class BotConfig:
    """自动回复运行参数（均可在桌面界面修改）。"""

    # 自动回复总开关。
    enabled: bool = False

    # ---- 触发条件 ----
    reply_private: bool = True   # 私聊：每条入站消息都回复
    trigger_at: bool = True     # 群聊：@我 时回复
    trigger_reply: bool = True  # 群聊：引用/回复我的消息时回复
    # 会话延续：刚被回复过的发送者在该窗口内再次发言也回复（0=关闭）。
    # 用于「回复 A 之后 A 继续补充，机器人接着回」的连续对话。
    continuation_seconds: float = 120.0

    # ---- 作用范围 ----
    # True=所有群；False=仅 groups 白名单内的群。
    all_groups: bool = True
    groups: list[str] = field(default_factory=list)

    # ---- 大模型（OpenAI 兼容 /chat/completions）----
    api_base: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.9
    max_tokens: int = 300
    timeout_seconds: float = 30.0

    # ---- 上下文与节流 ----
    context_messages: int = 20       # 送给模型的最近消息条数
    cooldown_seconds: float = 3.0    # 同一会话同一发送者的冷却秒数

    # ---- 人设 ----
    # 用户自定义人设；空串（默认）表示不注入任何人设。
    persona_custom: str = ""

    # ---- 拟人化：让自动回复更像真人 ----
    # 总开关；关闭后恢复「收到即整段回复」的旧行为。
    humanize: bool = True
    # 触发后实际回复的概率（0~1）。1 = 每条都回；越小越像真人（会漏回）。
    reply_probability: float = 0.85
    # 把较长回复拆成多条短消息发出，模拟真人分句连发。
    split_replies: bool = True
    # 单次最多拆成几条。
    split_max_parts: int = 3
    # 仅当回复长度超过该字数才分条（避免把一句短话也拆开）。
    split_min_length: int = 40
    # 分条之间的间隔区间（秒）。
    split_delay_min: float = 0.4
    split_delay_max: float = 1.2

    # ---- 静默：降低存在感，避免打扰 ----
    # 静默时段内不回复任何人（含主动参与）。支持跨零点，如 23:00-08:00。
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    # 群级节流：每个群在窗口内最多回复多少条，超出则静默（0 = 不限制）。
    group_rate_enabled: bool = True
    group_rate_window_minutes: float = 30.0
    group_rate_max_replies: int = 5

    # ---- 偶发参与（主动水群）----
    # 默认关闭：开启后引擎会周期性检查群聊，偶发主动接一句。
    proactive_enabled: bool = False
    # 检查间隔区间（秒）：每隔这个随机时间检查一次。
    proactive_interval_min: float = 300.0
    proactive_interval_max: float = 900.0
    # 单次检查中实际主动发言的概率。
    proactive_probability: float = 0.1
    # 仅当群里最近这段时间（秒）内有过新消息才考虑主动发言。
    proactive_recent_seconds: float = 600.0
    # 同一群两次主动发言之间的最小间隔（秒）。
    proactive_per_group_cooldown: float = 1800.0

    # ---- 图像识别（需模型支持视觉）----
    # 关闭时图片只以「[图片]」文本形式进入上下文，不发送图片数据。
    vision_enabled: bool = False
    # 单次请求最多附带几张上下文图片（0 = 不带图）。
    vision_max_images: int = 3

    # ---- 链接解析（检测到链接时自动解析并喂给模型）----
    # 开启后，上下文里出现的链接会被解析（标题/作者/时长/简介），
    # 结果作为「[链接] …」注入模型上下文，让机器人能就链接内容回应。
    link_parse_enabled: bool = True
    # 单次请求最多解析多少个链接（含已缓存的）。
    link_parse_max: int = 3
    # 单个链接的解析超时（秒）。
    link_parse_timeout: float = 20.0
    # 下载音视频并发回当前聊天。默认**关闭**：会向真实聊天发送文件。
    link_download_enabled: bool = False
    # 单次触发最多下载几个链接。
    link_download_max: int = 1
    # 下载目录；空串表示使用默认目录（%APPDATA%\wechat-mcp\downloads）。
    link_download_dir: str = ""
    # 单文件体积上限（MB），超过则跳过下载（0 = 不限制）。
    link_download_max_mb: int = 100
    # 下载目录**总占用**上限（MB）：超出后自动删除最旧的文件，直到降到上限以内。
    # 0 = 不限制。默认 1 GB——长期运行不至于把磁盘吃满，正常使用也很少触发。
    link_download_quota_mb: int = 1024
    # X（Twitter）推文额外发一张本地渲染的卡片图（头像/昵称/正文/配图）。
    # 仅对 X 推文生效；推文含视频时还会下载视频一并回发。
    link_tweet_card_enabled: bool = True
    # 识别到链接时先回一句**固定**提示（不经过大模型），让用户知道正在处理。
    # 私聊与群聊都生效（群聊里不必 @ 机器人）。
    link_ack_enabled: bool = True
    # 固定提示的文案；留空则不发送提示。
    link_ack_text: str = "正在解析链接"
    # 发完固定提示后，是否仍让大模型就这条消息接话。
    # 默认 True：私聊等原本会回复的场景保持原样（多一条提示）。
    # 设为 False 则「带链接的消息只回固定提示 + 回发解析结果」，不烧 token。
    link_llm_followup: bool = True

    def normalized_api_base(self) -> str:
        """去掉末尾斜杠，便于拼接 /chat/completions。"""
        return self.api_base.strip().rstrip("/")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        """从 dict 构造；忽略未知键、补齐缺失键，容忍旧版本配置。"""
        if not isinstance(data, dict):
            return cls()
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {key: value for key, value in data.items() if key in valid}
        config = cls(**clean)
        # 类型纠正，避免坏配置导致后续崩溃。
        config.groups = [str(item) for item in config.groups if str(item).strip()]
        config.context_messages = max(1, int(config.context_messages))
        config.cooldown_seconds = max(0.0, float(config.cooldown_seconds))
        config.continuation_seconds = max(
            0.0, float(config.continuation_seconds)
        )
        config.temperature = float(config.temperature)
        config.max_tokens = max(1, int(config.max_tokens))
        config.timeout_seconds = max(1.0, float(config.timeout_seconds))
        # 拟人化参数：概率钳制到 [0,1]，条数与间隔取合法范围。
        config.humanize = bool(config.humanize)
        config.reply_probability = min(
            1.0, max(0.0, float(config.reply_probability))
        )
        config.split_replies = bool(config.split_replies)
        config.split_max_parts = max(1, int(config.split_max_parts))
        config.split_min_length = max(0, int(config.split_min_length))
        config.split_delay_min = max(0.0, float(config.split_delay_min))
        config.split_delay_max = max(
            config.split_delay_min, float(config.split_delay_max)
        )
        # 静默 / 节流 / 主动参与参数。
        config.quiet_hours_enabled = bool(config.quiet_hours_enabled)
        config.quiet_hours_start = str(config.quiet_hours_start or "").strip()
        config.quiet_hours_end = str(config.quiet_hours_end or "").strip()
        config.group_rate_enabled = bool(config.group_rate_enabled)
        config.group_rate_window_minutes = max(
            0.0, float(config.group_rate_window_minutes)
        )
        config.group_rate_max_replies = max(0, int(config.group_rate_max_replies))
        config.proactive_enabled = bool(config.proactive_enabled)
        config.proactive_interval_min = max(1.0, float(config.proactive_interval_min))
        config.proactive_interval_max = max(
            config.proactive_interval_min, float(config.proactive_interval_max)
        )
        config.proactive_probability = min(
            1.0, max(0.0, float(config.proactive_probability))
        )
        config.proactive_recent_seconds = max(
            0.0, float(config.proactive_recent_seconds)
        )
        config.proactive_per_group_cooldown = max(
            0.0, float(config.proactive_per_group_cooldown)
        )
        config.vision_enabled = bool(config.vision_enabled)
        config.vision_max_images = max(0, int(config.vision_max_images))
        # 链接解析参数。
        config.link_parse_enabled = bool(config.link_parse_enabled)
        config.link_parse_max = max(0, int(config.link_parse_max))
        config.link_parse_timeout = max(1.0, float(config.link_parse_timeout))
        config.link_download_enabled = bool(config.link_download_enabled)
        config.link_download_max = max(0, int(config.link_download_max))
        config.link_download_dir = str(config.link_download_dir or "").strip()
        config.link_download_max_mb = max(0, int(config.link_download_max_mb))
        config.link_download_quota_mb = max(0, int(config.link_download_quota_mb))
        config.link_tweet_card_enabled = bool(config.link_tweet_card_enabled)
        config.link_ack_enabled = bool(config.link_ack_enabled)
        # 文案留空即视为「不发提示」，故不做默认值回填（只去空白与换行）。
        config.link_ack_text = " ".join(str(config.link_ack_text or "").split())
        config.link_llm_followup = bool(config.link_llm_followup)
        return config

    def download_dir(self) -> Path:
        """链接下载目录：显式配置优先，否则用 %APPDATA%\\wechat-mcp\\downloads。"""
        raw = self.link_download_dir.strip()
        if raw:
            return Path(raw).expanduser()
        return default_config_path().parent / "downloads"

    def download_quota_bytes(self) -> int:
        """下载目录总占用上限（字节）；0 表示不限制。"""
        return max(0, int(self.link_download_quota_mb)) * 1024 * 1024

    def save(self, path: Path | None = None) -> Path:
        target = path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "BotConfig":
        target = path or default_config_path()
        if not target.is_file():
            return cls()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()
        return cls.from_dict(data)
