"""下载目录的占用统计与自动清理。

「下载并回发」会不断往下载目录里写文件（推文卡片图、视频、音视频），长期运行
会无限膨胀，把用户磁盘吃满。这里提供一组不依赖引擎的纯函数：

- :func:`dir_usage`：递归统计目录内文件的总字节数。
- :func:`total_size`：统计一组文件的字节数。
- :func:`enforce_quota`：目录总占用超过配额时，**按修改时间从旧到新**删除文件，
  直到降到配额以内，返回被删除的文件路径列表。

安全约束（刻意保守）：只删 ``directory`` 之内的**普通文件**，跳过符号链接与目录，
``protect`` 里给出的路径（例如正在发送的文件）永不删除。删除失败只记日志、不抛异常
——清理是「尽力而为」的收尾动作，不该影响正常的下载回发流程。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

Logger = Callable[[str, str], None]


def _iter_files(directory: Path) -> list[Path]:
    """列出目录内的普通文件（递归，跳过符号链接与目录本身）。"""
    found: list[Path] = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            path = Path(root) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            found.append(path)
    return found


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def dir_usage(directory: str | Path) -> int:
    """目录内所有普通文件的总字节数；目录不存在时返回 0。"""
    path = Path(directory)
    if not path.is_dir():
        return 0
    return sum(_file_size(item) for item in _iter_files(path))


def total_size(paths: Iterable[str | Path]) -> int:
    """一组文件的总字节数（取不到大小的按 0 计）。"""
    return sum(_file_size(Path(item)) for item in paths)


def enforce_quota(
    directory: str | Path,
    quota_bytes: int,
    *,
    protect: Iterable[str | Path] = (),
    logger: Logger | None = None,
) -> list[Path]:
    """目录占用超过 ``quota_bytes`` 时按「最旧优先」删除，返回已删文件列表。

    ``quota_bytes`` <= 0 表示不限制，直接返回空列表。``protect`` 中的路径
    （无论以何种形式给出，均按 ``resolve()`` 后的绝对路径比对）不会被删除。
    """
    log = logger or (lambda level, message: None)
    root = Path(directory)
    if quota_bytes <= 0 or not root.is_dir():
        return []

    protected: set[Path] = set()
    for item in protect:
        try:
            protected.add(Path(item).resolve())
        except OSError:
            continue

    entries: list[tuple[float, Path, int]] = []
    total = 0
    for path in _iter_files(root):
        try:
            if path.resolve() in protected:
                continue
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, path, stat.st_size))
        total += stat.st_size

    if total <= quota_bytes:
        return []

    entries.sort(key=lambda item: item[0])  # 最旧在前
    removed: list[Path] = []
    for _mtime, path, size in entries:
        if total <= quota_bytes:
            break
        try:
            path.unlink()
        except OSError as exc:
            log("warning", f"清理下载文件失败（{path.name}）：{exc}")
            continue
        total -= size
        removed.append(path)
    return removed
