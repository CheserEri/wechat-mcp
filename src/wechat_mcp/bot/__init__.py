"""常驻自动回复 bot：实时监听 → 上下文 → 大模型 → 回复。"""

from .config import BotConfig, default_config_path
from .engine import BotEngine
from .llm import LLMClient, LLMError
from .persona import DEFAULT_PERSONA, get_persona, reset_persona

__all__ = [
    "BotConfig",
    "BotEngine",
    "LLMClient",
    "LLMError",
    "DEFAULT_PERSONA",
    "default_config_path",
    "get_persona",
    "reset_persona",
]
