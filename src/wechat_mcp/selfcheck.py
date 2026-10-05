"""运行环境自检与链接解析诊断。

用法::

    wechat-mcp --selfcheck
    wechat-mcp --resolve <url> [--download] [--outdir <目录>]

``--selfcheck`` 会检查：版本、是否冻结运行、内置 yt-dlp 能否导入、ffmpeg 是否可用、
内置桥接层与 webui 是否存在。任何一项显示 ``(不可用)`` / ``(缺失)`` 都
意味着对应功能会失效。

``--resolve`` 则在不依赖微信环境的前提下，直接调用链接解析链路解析（必要时下载）
一个链接，用于验证「检索到链接→解析→返回内容」这一路径在冻结包里是否可用。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _utf8_console() -> None:
    """让控制台输出用 UTF-8。

    Windows 控制台默认是 GBK（cp936），而链接标题 / 推文正文里常有 emoji 与
    其他非 GBK 字符，直接 ``print`` 会抛 ``UnicodeEncodeError`` 把整条诊断命令
    打断。这里强制 UTF-8 并允许无法编码的字符降级替换。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _webui_index() -> Path | None:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "webui" / "index.html"
    else:
        candidate = Path(__file__).resolve().parent / "webui" / "index.html"
    return candidate if candidate.is_file() else None


def _bridge_path() -> Path | None:
    """桥接层来源：冻结时取内置副本，源码模式取仓库内的 packaging/wechat_bridge.py。"""
    from .adapters.deepseekgirl import _bundled_bridge_file

    bundled = _bundled_bridge_file()
    if bundled:
        return bundled
    candidate = (
        Path(__file__).resolve().parents[2] / "packaging" / "wechat_bridge.py"
    )
    return candidate if candidate.is_file() else None


def collect() -> dict[str, object]:
    """收集自检信息（顺序即打印顺序）。"""
    from .bot import links
    from .server import SERVER_VERSION

    bridge = _bridge_path()
    webui = _webui_index()
    yt_dlp_version = links.yt_dlp_version()
    info: dict[str, object] = {
        "version": SERVER_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "yt_dlp_version": yt_dlp_version or "(不可用)",
        "ffmpeg": links._ffmpeg_exe() or "(不可用)",
        "bridge": str(bridge) if bridge else "(缺失)",
        "webui": str(webui) if webui else "(缺失)",
    }
    if not yt_dlp_version:
        info["yt_dlp_error"] = links.yt_dlp_error() or "(未知原因)"
    return info


def main() -> int:
    _utf8_console()
    for key, value in collect().items():
        print(f"{key}: {value}")
    return 0


def resolve(url: str, *, download: bool = False, outdir: str | None = None) -> int:
    """诊断用：解析（可选下载）单个链接并打印结果，不依赖微信环境。

    返回 0 表示解析成功，1 表示解析失败，2 表示用法错误。
    """
    import asyncio
    import tempfile

    from .bot.links import LinkResolver

    _utf8_console()
    if not url:
        print("用法：wechat-mcp --resolve <url> [--download] [--outdir <目录>]")
        return 2

    resolver = LinkResolver(timeout=30.0)
    info = asyncio.run(resolver.resolve(url))

    print(f"url: {info.url}")
    print(f"ok: {info.ok}")
    print(f"title: {info.title or '(无)'}")
    print(f"uploader: {info.uploader or '(无)'}")
    print(f"duration: {info.duration if info.duration is not None else '(无)'}")
    print(f"extractor: {info.extractor or '(无)'}")
    print(f"is_media: {info.is_media}")
    print(f"webpage_url: {info.webpage_url or '(无)'}")
    if info.error:
        print(f"error: {info.error}")
    print(f"summary: {info.summary()}")

    if download:
        target = outdir or tempfile.mkdtemp(prefix="wechat-mcp-resolve-")
        path = asyncio.run(resolver.download(url, target, max_mb=0))
        print(f"download: {path or '(失败)'}")
        if path:
            print(f"download_dir: {target}")

    return 0 if info.ok else 1


def tweet_card(url: str, outdir: str | None = None) -> int:
    """诊断用：抓取一条 X 推文并渲染成卡片图，打印产物路径。

    返回 0 表示成功，1 表示抓取/渲染失败，2 表示用法错误。
    """
    import tempfile

    from .bot import tweet as tweet_mod

    _utf8_console()
    if not url:
        print("用法：wechat-mcp --tweet <推文链接> [--outdir <目录>]")
        return 2
    if not tweet_mod.is_tweet_url(url):
        print(f"不是 X/Twitter 推文链接：{url}")
        return 2

    info = tweet_mod.fetch_tweet(
        url, timeout=30.0, logger=lambda level, message: print(f"[{level}] {message}")
    )
    if info is None:
        print("推文抓取失败（可能已删除、受保护，或网络不通）")
        return 1

    print(f"author: {info.author_name} (@{info.author_handle})")
    print(f"time: {info.created_at or '(未知)'}")
    print(f"verified: {info.verified or '(无)'}")
    print(f"has_video: {info.has_video}")
    print(f"photos: {len(info.photos)}")
    print(f"likes: {info.likes} | replies: {info.replies}")
    print(f"text: {info.text}")

    target = outdir or tempfile.mkdtemp(prefix="wechat-mcp-tweet-")
    path = tweet_mod.render_card(info, target, timeout=30.0)
    print(f"card: {path or '(渲染失败)'}")
    return 0 if path else 1
