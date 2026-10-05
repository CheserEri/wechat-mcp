"""清除自带程序集上的「Internet 区域」标记（Mark-of-the-Web）。

背景：从 zip 解压出来的文件会被 Windows 打上 ``Zone.Identifier`` 数据流
（``ZoneId=3``）。.NET Framework 出于安全默认**拒绝加载**带该标记的程序集，
于是 pythonnet 初始化失败、pywebview 起不来，界面表现就是**黑屏**：

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
    from ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

本模块让冻结包在启动时自己把标记清掉，用户「解压即用」，无需手动解除锁定。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ZONE_STREAM = ":Zone.Identifier"

# 真正需要 .NET 加载的程序集：pythonnet 的运行时 DLL，以及 pywebview 的
# WebView2 托管 DLL（含 runtimes/ 下的原生 loader）。只清这几处即可，
# 不必遍历上千个文件。
_TARGET_GLOBS = (
    "pythonnet/runtime/*.dll",
    "webview/lib/**/*.dll",
)


def _delete_zone_stream(path: Path) -> bool:
    """删除 ``path`` 上的 Zone.Identifier 数据流；确实删掉了才返回 True。"""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    delete_file = ctypes.windll.kernel32.DeleteFileW
    delete_file.argtypes = [wintypes.LPCWSTR]
    delete_file.restype = wintypes.BOOL
    return bool(delete_file(str(path) + _ZONE_STREAM))


def has_zone_identifier(path: Path) -> bool:
    """判断文件是否带 Internet 区域标记（无标记或非 NTFS 返回 False）。"""
    try:
        with open(str(path) + _ZONE_STREAM, "r", errors="replace"):
            return True
    except OSError:
        return False


def bundled_root() -> Path | None:
    """冻结包的内置资源根目录（``_internal``）；源码模式返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def unblock_bundled_assemblies(root: Path | None = None) -> int:
    """清除自带程序集上的 Internet 区域标记，返回实际处理的数量。

    ``root`` 缺省取 :func:`bundled_root`；源码模式（无 ``_MEIPASS``）直接跳过。
    """
    base = root if root is not None else bundled_root()
    if base is None or not base.is_dir():
        return 0
    count = 0
    for pattern in _TARGET_GLOBS:
        for path in base.glob(pattern):
            if has_zone_identifier(path) and _delete_zone_stream(path):
                count += 1
    return count
