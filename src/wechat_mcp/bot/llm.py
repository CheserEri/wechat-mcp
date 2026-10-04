"""OpenAI 兼容的大模型客户端（``/chat/completions``）。

默认对接 DeepSeek，但任何兼容 OpenAI Chat Completions 格式的端点都可通过
配置 ``api_base`` / ``model`` 使用。失败抛出结构化的 :class:`LLMError`，
不伪装成成功。
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import BotConfig


class LLMError(Exception):
    """大模型调用失败。"""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "status": self.status,
        }


class LLMClient:
    """OpenAI 兼容 chat completions 客户端。"""

    def __init__(self, config: BotConfig, transport: Any = None):
        self._config = config
        # 可选 httpx 传输层（生产为 None；测试注入 MockTransport）。
        self._transport = transport

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """发送消息列表，返回模型文本回复。"""
        config = self._config
        if not config.api_key.strip():
            raise LLMError("api_key_missing", "未配置 API Key，请在「模型设置」中填写。")
        if not messages:
            raise LLMError("empty_messages", "没有可供模型处理的上下文。")

        url = config.normalized_api_base() + "/chat/completions"
        payload: dict[str, Any] = {
            "model": config.model.strip(),
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {config.api_key.strip()}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=config.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(
                "timeout", f"模型请求超时（{config.timeout_seconds:.0f} 秒）。"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError("network_error", f"无法连接模型服务：{exc}") from exc

        if response.status_code in (401, 403):
            raise LLMError(
                "unauthorized",
                "API Key 无效或无权限，请检查 Key 与模型名称。",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise LLMError(
                "api_error",
                f"模型服务返回错误（HTTP {response.status_code}）："
                f"{response.text[:300]}",
                status=response.status_code,
            )

        try:
            data = response.json()
            reply = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(
                "bad_response", f"模型返回格式无法解析：{response.text[:300]}"
            ) from exc

        reply = str(reply or "").strip()
        if not reply:
            raise LLMError("empty_response", "模型返回了空回复。")
        return reply
