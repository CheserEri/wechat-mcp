"""发送目标校验、同名消歧与文件路径白名单。

原则：不依赖当前焦点窗口，不允许隐式目标；目标不唯一时拒绝执行；
发送文件前校验文件存在、可读且位于显式允许的目录内，防止越权发送任意本地文件。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .errors import FilePathInvalidError, FilePathNotAllowedError, TargetInvalidError

MAX_RECIPIENT_LENGTH = 128
MAX_MESSAGE_LENGTH = 20000

# 控制字符：会破坏 UI 输入或造成歧义
_FORBIDDEN_CHARS = frozenset("\r\n\t\x00")


def validate_recipient(recipient: object) -> str:
    """校验并规范化接收方名称。"""
    if recipient is None:
        raise TargetInvalidError("发送目标不能为空")
    value = str(recipient).strip()
    if not value:
        raise TargetInvalidError("发送目标不能为空")
    if len(value) > MAX_RECIPIENT_LENGTH:
        raise TargetInvalidError(
            f"发送目标过长（上限 {MAX_RECIPIENT_LENGTH} 字符）",
            detail={"length": len(value)},
        )
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        raise TargetInvalidError("发送目标包含非法控制字符")
    return value


def validate_message(message: object) -> str:
    """校验消息正文。"""
    if message is None or not str(message).strip():
        raise TargetInvalidError("消息内容不能为空")
    value = str(message)
    if len(value) > MAX_MESSAGE_LENGTH:
        raise TargetInvalidError(
            f"消息内容过长（上限 {MAX_MESSAGE_LENGTH} 字符）",
            detail={"length": len(value)},
        )
    return value


def resolve_target(
    recipient: object,
    known_names: Iterable[str],
) -> tuple[str, list[str]]:
    """在已知会话名中解析发送目标。

    返回 ``(resolved, candidates)``：

    - ``resolved`` 非空、``candidates`` 为空：可以执行发送。
    - ``candidates`` 非空：存在同名/多个部分匹配，调用方必须拒绝发送。
    - 两者组合为 ``(recipient, [])``：未在已知会话中命中，交由底层按精确名搜索。
    """
    normalized = validate_recipient(recipient)
    names = [str(name).strip() for name in (known_names or []) if str(name).strip()]

    exact = [name for name in names if name == normalized]
    if len(exact) > 1:
        return "", sorted(set(exact))
    if exact:
        return normalized, []

    partial = sorted({name for name in names if normalized in name})
    if len(partial) == 1:
        return partial[0], []
    if len(partial) > 1:
        return "", partial

    return normalized, []


def _is_within(path: Path, base: Path) -> bool:
    """判断 ``path`` 是否位于 ``base`` 目录内（含 Windows 大小写不敏感）。"""
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def validate_send_path(
    raw_path: object,
    allowed_dirs: Iterable[object] = (),
) -> Path:
    """校验待发送文件路径并返回规范化后的绝对路径。

    校验顺序：非空 → 存在 → 是文件 → 可读 → 位于允许目录内。
    未配置任何允许目录时一律拒绝，需要显式配置 ``WECHAT_SEND_DIRS`` 放宽。
    """
    if raw_path is None or not str(raw_path).strip():
        raise FilePathInvalidError("文件路径不能为空")
    text = str(raw_path).strip()
    if any(ch in _FORBIDDEN_CHARS for ch in text):
        raise FilePathInvalidError("文件路径包含非法控制字符")

    try:
        resolved = Path(text).expanduser().resolve()
    except OSError as exc:
        raise FilePathInvalidError(
            f"无法解析文件路径: {exc}", detail={"path": text}
        ) from exc

    if not resolved.exists():
        raise FilePathInvalidError("文件不存在", detail={"path": str(resolved)})
    if not resolved.is_file():
        raise FilePathInvalidError("路径不是文件", detail={"path": str(resolved)})
    if not os.access(resolved, os.R_OK):
        raise FilePathInvalidError("文件不可读", detail={"path": str(resolved)})

    bases: list[Path] = []
    for item in allowed_dirs or ():
        if item is None or not str(item).strip():
            continue
        try:
            bases.append(Path(str(item)).expanduser().resolve())
        except OSError:
            continue

    if not bases:
        raise FilePathNotAllowedError(
            "未配置可发送目录（WECHAT_SEND_DIRS），已拒绝发送文件",
            detail={"path": str(resolved)},
        )
    if not any(_is_within(resolved, base) for base in bases):
        raise FilePathNotAllowedError(
            "文件不在允许发送的目录内",
            detail={
                "path": str(resolved),
                "allowed_dirs": [str(base) for base in bases],
            },
        )
    return resolved
