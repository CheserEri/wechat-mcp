"""人设管理：用户自定义人设（默认留空，不注入 system 提示词）。

与上游「人设锁定」不同，本程序允许用户在界面中修改；默认值为空，
即默认不向大模型注入任何人设，用户按需填写。
"""

from __future__ import annotations

from .config import BotConfig

# 默认人设为空：不注入 system 提示词，模型按自身默认行为回复。
DEFAULT_PERSONA = ""


def get_persona(config: BotConfig) -> str:
    """返回当前生效人设：自定义非空用自定义，否则为空（不注入）。"""
    custom = config.persona_custom.strip()
    return custom if custom else DEFAULT_PERSONA


def reset_persona(config: BotConfig) -> BotConfig:
    """恢复默认人设（清空自定义内容，默认即为空）。"""
    config.persona_custom = ""
    return config
