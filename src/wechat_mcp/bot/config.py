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
        return config

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
