"""适配层配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEEPSEEKGIRL_PATH = Path(r"F:\Code\deepseekgirl-main")
# 审计日志默认写到项目根目录下的 logs/audit.jsonl（可用 WECHAT_AUDIT_LOG 覆盖）。
DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "audit.jsonl"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _split_list(raw: str) -> tuple[str, ...]:
    """把逗号分隔的环境变量拆成去重、去空的元组，保持输入顺序。"""
    result: list[str] = []
    for item in str(raw or "").split(","):
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


@dataclass
class AdapterConfig:
    """微信适配层运行参数。"""

    # 原项目位置：适配层从这里复用 WeChatBridge。
    deepseekgirl_path: Path = DEFAULT_DEEPSEEKGIRL_PATH
    # auto / wechatauto / wxauto4 / wxauto
    backend: str = "auto"
    # 读取能力依赖监听回调，故连接后默认启动监听。
    listen_on_connect: bool = True
    listen_private: bool = True
    # 适配层不做机器人式拟人延迟。
    reply_delay: float = 0.0
    # 单次发送的最长等待秒数。原项目为“秒回”硬编码为 5 秒，但向尚未打开的
    # 会话发送需要搜索、切换、输入，首次往往超过 5 秒，故在适配层放宽。
    send_timeout_seconds: float = 20.0
    # 被动消息缓冲区容量，决定 get_chat_history 最多可回溯的条数。
    history_buffer_size: int = 2000

    # ---- P1：文件发送白名单 ----
    # 允许发送文件的目录。为空表示默认拒绝发送任何文件，需显式配置放宽。
    allow_send_dirs: tuple[Path, ...] = ()
    # 发送前是否需要显式确认（confirm=True）。开启后未确认的发送仅返回预览。
    require_confirmation: bool = False

    # ---- P1：操作审计 ----
    audit_enabled: bool = True
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH

    # ---- P1：会话关注列表预设 ----
    # allow（白名单，空=关注全部）/ block（黑名单，优先级更高）
    monitor_mode: str = "allow"
    monitor_allow: tuple[str, ...] = ()
    monitor_block: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        raw_path = os.environ.get("DEEPSEEKGIRL_PATH", "").strip()
        path = Path(raw_path) if raw_path else DEFAULT_DEEPSEEKGIRL_PATH
        backend = os.environ.get("WECHAT_BACKEND", "auto").strip() or "auto"
        listen = (
            os.environ.get("WECHAT_LISTEN_ON_CONNECT", "1").strip().lower()
            in _TRUE_VALUES
        )
        raw_timeout = os.environ.get("WECHAT_SEND_TIMEOUT", "").strip()
        try:
            send_timeout = float(raw_timeout)
        except ValueError:
            send_timeout = cls.send_timeout_seconds

        send_dirs = tuple(
            Path(item)
            for item in _split_list(os.environ.get("WECHAT_SEND_DIRS", ""))
        )
        require_confirm = (
            os.environ.get("WECHAT_REQUIRE_CONFIRM", "0").strip().lower()
            in _TRUE_VALUES
        )
        audit_enabled = (
            os.environ.get("WECHAT_AUDIT_ENABLED", "1").strip().lower()
            in _TRUE_VALUES
        )
        raw_audit_log = os.environ.get("WECHAT_AUDIT_LOG", "").strip()
        audit_log_path = (
            Path(raw_audit_log) if raw_audit_log else DEFAULT_AUDIT_LOG_PATH
        )
        monitor_mode = (
            os.environ.get("WECHAT_MONITOR_MODE", "allow").strip().lower() or "allow"
        )
        if monitor_mode not in ("allow", "block"):
            monitor_mode = "allow"

        return cls(
            deepseekgirl_path=path,
            backend=backend,
            listen_on_connect=listen,
            send_timeout_seconds=send_timeout,
            allow_send_dirs=send_dirs,
            require_confirmation=require_confirm,
            audit_enabled=audit_enabled,
            audit_log_path=audit_log_path,
            monitor_mode=monitor_mode,
            monitor_allow=_split_list(os.environ.get("WECHAT_MONITOR_ALLOW", "")),
            monitor_block=_split_list(os.environ.get("WECHAT_MONITOR_BLOCK", "")),
        )
