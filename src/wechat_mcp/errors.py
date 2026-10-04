"""统一错误类型。

将底层微信自动化库的各种失败映射为稳定的错误语义，便于上层 MCP 工具
返回结构化错误，而不是把底层异常原样抛出或伪装成成功。
"""

from __future__ import annotations

from typing import Any


class WeChatMCPError(Exception):
    """所有适配层错误的基类。"""

    code = "wechat_error"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = str(message)
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


class BackendUnavailableError(WeChatMCPError):
    """微信自动化后端或依赖不可用（未安装、模块缺失等）。"""

    code = "backend_unavailable"


class NotConnectedError(WeChatMCPError):
    """尚未连接微信，或连接已失效。"""

    code = "not_connected"


class TargetInvalidError(WeChatMCPError):
    """发送目标不合法（为空、含非法字符、超长等）。"""

    code = "target_invalid"


class TargetAmbiguousError(WeChatMCPError):
    """存在多个同名/相似候选，拒绝执行以避免误发。"""

    code = "target_ambiguous"

    def __init__(
        self,
        message: str,
        candidates: list[str],
        *,
        detail: dict[str, Any] | None = None,
    ):
        merged = dict(detail or {})
        merged["candidates"] = list(candidates)
        super().__init__(message, detail=merged)
        self.candidates = list(candidates)


class UnsupportedOperationError(WeChatMCPError):
    """当前后端或运行环境不支持该操作。"""

    code = "unsupported_operation"


class FilePathInvalidError(WeChatMCPError):
    """待发送文件路径不合法（为空、不存在、不是文件或不可读）。"""

    code = "file_invalid"


class FilePathNotAllowedError(WeChatMCPError):
    """文件路径不在允许发送的目录范围内。"""

    code = "path_not_allowed"


class ConfirmationRequiredError(WeChatMCPError):
    """配置要求发送前确认，但本次调用未显式确认。"""

    code = "confirmation_required"
