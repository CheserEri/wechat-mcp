# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把 wechat-mcp 打成 Windows onedir 独立 exe。

请通过 ``packaging/build.py`` 调用。它会把本仓库内的
``packaging/wechat_bridge.py``（从上游 deepseekgirl 收录，来源见文件头注释与
``THIRD_PARTY_NOTICES.md``）作为数据文件内置进包，使分发的 exe 无需外部
deepseekgirl 项目即可运行。

体积说明：``wechatauto`` / ``wxauto4`` / ``uiautomation`` 会带进较多依赖；
``cv2`` / ``numpy`` 等仅在朋友圈等本适配层未启用的路径中被惰性导入，故剔除。
``imageio_ffmpeg``（自带 ffmpeg 二进制，约 84 MB）为「链接下载」合流 DASH
分离流所必需，``winsdk``（约 43 MB）为 wechatauto 的 UIA 发送路径所必需，
二者均保留内置，会使产物体积明显增大。

内置 yt-dlp（``vendor/yt_dlp``，链接解析用）通过 ``pathex`` + ``hiddenimports``
交给 PyInstaller 做**静态分析**，并启用其自带的官方 hook（``vendor/yt_dlp/
__pyinstaller``）。这样 yt-dlp 用到的标准库子模块与全部提取器都会被自动收集；
早期把它仅当作数据目录分发时，必须手工枚举标准库，极易遗漏（实测先缺
``optparse``、补上后又缺 ``html.parser``）。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
BRIDGE = SPEC_DIR / "wechat_bridge.py"
WEBUI = ROOT / "src" / "wechat_mcp" / "webui"
VENDOR = ROOT / "vendor"
# 应用图标（由 packaging/make_icon.py 生成），同时供 exe 与安装包使用。
ICON = SPEC_DIR / "wechat-mcp.ico"

if not BRIDGE.is_file():
    raise SystemExit(
        "缺少 packaging/wechat_bridge.py；该文件应随仓库一同提供。"
    )
if not WEBUI.is_dir():
    raise SystemExit("缺少 src/wechat_mcp/webui 前端目录。")
if not (VENDOR / "yt_dlp" / "__init__.py").is_file():
    raise SystemExit(
        "缺少 vendor/yt_dlp；内置的 yt-dlp 源码应随仓库一同提供。"
    )

datas = [
    (str(BRIDGE), "."),
    (str(WEBUI), "webui"),
    # 窗口图标：桌面端启动时显式传给 pywebview（与 exe 内嵌图标同一文件）。
    (str(ICON), "."),
]
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
    # 桌面 GUI（--gui 模式）
    "bottle",
    "webview",
    "webview.platforms.edgechromium",
    # 链接下载的 ffmpeg 定位（links.py 内部惰性导入，静态分析扫不到）
    "imageio_ffmpeg",
    # 内置 yt-dlp（链接解析）。显式声明后 PyInstaller 会真正分析 yt_dlp，
    # 自动收集它用到的全部标准库子模块（html.parser / urllib.request …）与
    # 940 个提取器；其官方 hook 另在下方 hookspath 中启用。
    "yt_dlp",
    # 抖音解析的浏览器桥：douyin.py 里是函数内惰性导入，静态分析未必扫到，
    # 漏掉会在冻结包里报 ModuleNotFoundError（只依赖标准库，不增加体积）。
    "wechat_mcp.bot.browser",
]

for _pkg in (
    "wechatauto",
    "wxauto4",
    "uiautomation",
    "comtypes",
    # pywebview（含其 js 资源）与 pythonnet（clr，含 Python.Runtime 二进制）
    "webview",
    "clr",
    # 链接下载：DASH 分离流（Bilibili/YouTube）必须靠 ffmpeg 合流，
    # imageio_ffmpeg 自带 ffmpeg 二进制（约 84 MB），故一并内置。
    "imageio_ffmpeg",
    # wechatauto 的 UIA 发送/OCR 路径依赖 winsdk（约 43 MB）。**不能排除**：
    # 缺它时 `SendMsg` 会抛 ModuleNotFoundError，首次发送必然失败、退化成
    # OCR 兼容路径并重试（日志 `发送消息失败: No module named 'winsdk'`）。
    "winsdk",
):
    _datas, _binaries, _hidden = collect_all(_pkg)
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

a = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT / "src"), str(VENDOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # yt-dlp 自带的官方 PyInstaller hook（hook-yt_dlp.py），补齐可选依赖与兼容模块。
    hookspath=[str(VENDOR / "yt_dlp" / "__pyinstaller")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "cv2",
        "numpy",
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
    icon=str(ICON) if ICON.is_file() else None,
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