# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把 wechat-mcp 打成 Windows onedir 独立 exe。

请通过 ``packaging/build.py`` 调用。它会把本仓库内的
``packaging/wechat_bridge.py``（从上游 deepseekgirl 收录，来源见文件头注释与
``THIRD_PARTY_NOTICES.md``）作为数据文件内置进包，使分发的 exe 无需外部
deepseekgirl 项目即可运行。

体积说明：``wechatauto`` / ``wxauto4`` / ``uiautomation`` 会带进较多依赖；
``cv2`` / ``numpy`` / ``imageio_ffmpeg`` / ``winsdk`` 等仅在朋友圈、媒体下载
等本适配层未启用的路径中被惰性导入，故剔除，可将体积从约 341 MB 降到约 71 MB。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
BRIDGE = SPEC_DIR / "wechat_bridge.py"

if not BRIDGE.is_file():
    raise SystemExit(
        "缺少 packaging/wechat_bridge.py；该文件应随仓库一同提供。"
    )

datas = [(str(BRIDGE), ".")]
binaries = []
hiddenimports = [
    # wechat_bridge 在运行期才被加载，需显式声明其依赖，否则冻结后报 ModuleNotFoundError。
    "loguru",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
    "win32gui",
    "win32ui",
    "win32api",
    "win32con",
    "win32process",
    "psutil",
]

for _pkg in ("wechatauto", "wxauto4", "uiautomation", "comtypes"):
    _datas, _binaries, _hidden = collect_all(_pkg)
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

a = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "cv2",
        "numpy",
        "imageio_ffmpeg",
        "winsdk",
        "pyautogui",
        "pypinyin",
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wechat-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wechat-mcp",
)