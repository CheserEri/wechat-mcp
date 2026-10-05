"""链接解析：检测消息中的 URL，用内置 yt-dlp 提取元数据、按需下载音视频。

设计要点
--------
- **媒体站点**（YouTube / Bilibili / 抖音 …）走 yt-dlp 的 ``extract_info``
  （``skip_download=True``，只取元数据）。
- **普通网页**回退到直接抓取 ``<title>`` 与 ``meta description``。
- 结果按 URL **缓存**（成功永久、失败带 TTL 允许重试），重复出现不再请求。
- yt-dlp 是阻塞调用，统一放到线程池执行，避免卡住事件循环。
- yt-dlp 源码**内置在仓库** ``vendor/yt_dlp``（见 THIRD_PARTY_NOTICES.md），
  源码模式与 PyInstaller 冻结模式都能导入。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import tweet

# --------------------------------------------------------------------------- #
# URL 检测
# --------------------------------------------------------------------------- #

# URL 允许的字符（RFC 3986）。刻意不含中文与全角标点，避免把紧跟其后的
# 中文句子一起吞进 URL。
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
# 单条交替正则：http(s):// 开头，或裸 www. 域名（其前不能是字母/数字/@/./-/）。
# 用一条正则保证匹配按出现顺序返回。
_LINK_RE = re.compile(
    rf"(?:https?://|(?<![\w@./-])www\.)[{_URL_CHARS}]+",
    re.IGNORECASE,
)
# 末尾常见标点（中英文），解析前剥掉。
_TRAILING = "，。、；：！？）】》」』”’\"'.,;:!?*｜|"


def _clean_url(raw: str) -> str:
    """剥掉 URL 末尾粘上的标点；成对的括号保留（如 wiki 的 ``(disambiguation)``）。"""
    url = (raw or "").strip()
    while url:
        last = url[-1]
        if last == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
            continue
        if last == "]" and url.count("[") < url.count("]"):
            url = url[:-1]
            continue
        if last in _TRAILING:
            url = url[:-1]
            continue
        break
    return url


def extract_urls(text: str) -> list[str]:
    """从文本中抽取 URL（含裸 ``www.`` 域名），去重并保持出现顺序。"""
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for match in _LINK_RE.finditer(text):
        raw = match.group(0)
        url = _clean_url(raw if raw[:4].lower() == "http" else "http://" + raw)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def contains_url(text: str) -> bool:
    return bool(_LINK_RE.search(text or ""))


# --------------------------------------------------------------------------- #
# 内置 yt-dlp 的导入引导
# --------------------------------------------------------------------------- #

_YTDLP_LOCK = threading.Lock()
_YTDLP_MODULE: Any = None
_YTDLP_TRIED = False
_YTDLP_ERROR = ""


def _vendor_dir() -> Path | None:
    """返回内置 ``yt_dlp`` 所在目录（该目录需加入 ``sys.path``）。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for candidate in (base, base / "wechat_mcp" / "_vendor"):
            if (candidate / "yt_dlp" / "__init__.py").is_file():
                return candidate
        return None
    # src/wechat_mcp/bot/links.py -> 仓库根
    root = Path(__file__).resolve().parents[3]
    vendor = root / "vendor"
    return vendor if (vendor / "yt_dlp" / "__init__.py").is_file() else None


def load_yt_dlp() -> Any:
    """导入内置 yt-dlp；不可用时返回 ``None``（只尝试一次）。"""
    global _YTDLP_MODULE, _YTDLP_TRIED, _YTDLP_ERROR
    with _YTDLP_LOCK:
        if _YTDLP_TRIED:
            return _YTDLP_MODULE
        _YTDLP_TRIED = True
        try:
            import yt_dlp  # type: ignore

            _YTDLP_MODULE = yt_dlp
            return _YTDLP_MODULE
        except ImportError as exc:
            _YTDLP_ERROR = f"直接导入失败（{type(exc).__name__}: {exc}）"

        vendor = _vendor_dir()
        if vendor is None:
            _YTDLP_ERROR += "；未找到内置 yt_dlp 目录"
            return None
        sys.path.insert(0, str(vendor))
        # sys.path 变化后清掉查找器缓存，避免冻结环境里首次失败被缓存住。
        importlib.invalidate_caches()
        try:
            import yt_dlp  # type: ignore

            _YTDLP_MODULE = yt_dlp
            _YTDLP_ERROR = ""
        except Exception as exc:  # 记下真实原因，便于 --selfcheck 排查
            _YTDLP_ERROR = (
                f"从 {vendor} 导入失败（{type(exc).__name__}: {exc}）"
            )
        return _YTDLP_MODULE


def yt_dlp_error() -> str:
    """最近一次导入 yt-dlp 失败的原因（成功时为空串）。"""
    load_yt_dlp()
    return _YTDLP_ERROR


def yt_dlp_version() -> str:
    module = load_yt_dlp()
    if module is None:
        return ""
    try:
        return str(module.version.__version__)
    except Exception:  # pragma: no cover - 版本属性缺失时忽略
        return ""


# --------------------------------------------------------------------------- #
# 解析结果
# --------------------------------------------------------------------------- #


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _short(text: Any, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _clean_display_url(url: str) -> str:
    """展示用地址：裁掉一堆分享/跟踪参数（``share_*`` / ``buvid`` / ``unique_k`` …）。

    只在参数明显是噪声（多于 3 个）时裁，像 ``?v=xxx`` 这种「参数即标识」的
    地址保持原样。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query or len(parts.query.split("&")) <= 3:
        return url
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _newest_new_file(directory: str, before: set[str]) -> str | None:
    """兜底：返回目录里本次新增的最新文件（没有新增则 None）。"""
    try:
        entries = [
            path
            for path in Path(directory).iterdir()
            if path.is_file() and path.name not in before
        ]
    except OSError:
        return None
    if not entries:
        return None
    try:
        return str(max(entries, key=lambda path: path.stat().st_mtime))
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 短链展开
# --------------------------------------------------------------------------- #

# yt-dlp **不认识**这些短链主机：会落到 Generic 提取器，把短码当成标题
# （例如 ``https://b23.tv/P7kJlgt`` → 标题 "P7kJlgt"），既拿不到元数据也
# 没法下载。故解析/下载前先跟随重定向拿到真实地址。
_SHORT_LINK_HOSTS = {
    "b23.tv",         # 哔哩哔哩分享短链
    "v.douyin.com",   # 抖音
    "xhslink.com",    # 小红书
    "t.cn",           # 微博
    "sourl.cn",
    "dwz.cn",
    "url.cn",
}

# b23.tv 等有时是 JS/meta 跳转而非 302，从正文里兜底捞真实地址。
_SHORT_LINK_TARGET_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"bilibili\.com/video/[A-Za-z0-9]+"
    r"|douyin\.com/video/\d+"
    r"|xiaohongshu\.com/[A-Za-z0-9/_\-]+"
    r"|weibo\.com/[A-Za-z0-9/_\-]+"
    r")",
    re.IGNORECASE,
)


def _is_short_link(url: str) -> bool:
    """是否属于已知短链主机（据此决定要不要先展开）。"""
    host = urllib.parse.urlsplit(url).netloc.lower()
    if "@" in host:  # 去掉 userinfo
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host in _SHORT_LINK_HOSTS


@dataclass
class LinkInfo:
    """单个链接的解析结果。"""

    url: str
    ok: bool = False
    title: str = ""
    uploader: str = ""
    duration: int | None = None
    description: str = ""
    extractor: str = ""
    is_media: bool = False
    webpage_url: str = ""
    error: str = ""
    ts: float = 0.0  # 解析时间，用于失败条目的重试判定
    label: str = "链接"  # 注入上下文时的前缀，例如推文用「推文」
    meta: str = ""  # 额外元信息（如推文的时间/点赞），原样并入摘要

    def summary(self, max_desc: int = 200) -> str:
        """压成一行注入模型上下文的短文本。"""
        tag = self.label or "链接"
        if not self.ok:
            reason = f"：{self.error}" if self.error else ""
            return f"[{tag}] {self.url}（解析失败{reason}）"
        parts: list[str] = []
        if self.title:
            parts.append(f"标题：{self.title}")
        if self.uploader:
            parts.append(f"作者：{self.uploader}")
        duration = _fmt_duration(self.duration)
        if duration:
            parts.append(f"时长：{duration}")
        if self.extractor:
            parts.append(f"来源：{self.extractor}")
        if self.meta:
            parts.append(self.meta)
        desc = _short(self.description, max_desc)
        if desc:
            parts.append(f"简介：{desc}")
        head = f"[{tag}] {_clean_display_url(self.webpage_url or self.url)}"
        return head + ("｜" + "；".join(parts) if parts else "")


def format_link_block(infos: list[LinkInfo], max_desc: int = 200) -> str:
    return "\n".join(info.summary(max_desc) for info in infos if info)


def _rekey(info: LinkInfo, url: str, lookup: str) -> LinkInfo:
    """把解析结果重新挂回**原始 URL**。

    缓存与 ``_link_block`` 都按消息里出现的原始 URL 查找，而短链展开后实际
    解析的是另一个地址，故这里必须换回原始 URL（真实地址留在 ``webpage_url``，
    模型仍能看到它）。
    """
    if lookup != url:
        info.url = url
        if not info.webpage_url or info.webpage_url == lookup:
            info.webpage_url = lookup
    return info


def _tweet_link_info(
    url: str, timeout: float, logger: Callable[[str, str], None]
) -> LinkInfo | None:
    """把 X 推文转成 :class:`LinkInfo`，让「[推文] …」也能注入上下文。"""
    info = tweet.fetch_tweet_cached(url, timeout=timeout, logger=logger)
    if info is None:
        return None
    body = re.sub(r"\s+", " ", info.text).strip()
    author = " ".join(
        part
        for part in (
            info.author_name,
            f"@{info.author_handle}" if info.author_handle else "",
        )
        if part
    )
    meta_parts: list[str] = []
    if info.created_at:
        meta_parts.append(info.created_at)
    if info.has_video:
        meta_parts.append("含视频")
    elif info.photos:
        meta_parts.append(f"含 {len(info.photos)} 张图")
    if info.likes:
        meta_parts.append(f"{info.likes} 喜欢")
    return LinkInfo(
        url=url,
        ok=True,
        # 推文正文放进 description（summary 里按「简介」展示，上限 200 字），
        # title 留空避免同一段文字被重复注入两次。
        title="",
        uploader=author,
        description=body,
        extractor="X",
        is_media=info.has_video,
        webpage_url=url,
        label="推文",
        meta=" · ".join(meta_parts),
    )


# --------------------------------------------------------------------------- #
# 网页兜底抓取
# --------------------------------------------------------------------------- #

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_HTML_BYTES = 512 * 1024
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"(name|property|content)\s*=\s*[\"'](.*?)[\"']", re.IGNORECASE | re.DOTALL
)
_DESC_KEYS = ("description", "og:description", "twitter:description")


def _html_unescape(text: str) -> str:
    import html as _html

    return _html.unescape(text or "")


def _html_title(markup: str) -> str:
    match = _TITLE_RE.search(markup or "")
    if not match:
        return ""
    return _short(_html_unescape(match.group(1)), 300)


def _html_description(markup: str) -> str:
    for tag in _META_TAG_RE.finditer(markup or ""):
        attrs: dict[str, str] = {}
        for key, value in _ATTR_RE.findall(tag.group(0)):
            attrs[key.lower()] = value
        name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        if name in _DESC_KEYS and attrs.get("content"):
            return _short(_html_unescape(attrs["content"]), 300)
    return ""


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


class _QuietLogger:
    """吞掉 yt-dlp 的内部日志，避免污染助手日志。"""

    def debug(self, msg: str) -> None:  # noqa: D102
        pass

    def info(self, msg: str) -> None:  # noqa: D102
        pass

    def warning(self, msg: str) -> None:  # noqa: D102
        pass

    def error(self, msg: str) -> None:  # noqa: D102
        pass


def _ffmpeg_exe() -> str | None:
    """定位可用的 ffmpeg：系统 PATH → imageio_ffmpeg 自带二进制。

    DASH 分离流（Bilibili / YouTube 等）必须靠 ffmpeg 合流，否则只能拿到
    无声视频或无画面的音频。打包时 imageio_ffmpeg 会随包内置。
    """
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_available() -> bool:
    """是否有可用的 ffmpeg（决定能否合流高清音视频）。"""
    return _ffmpeg_exe() is not None


class LinkResolver:
    """URL → 元数据 / 本地文件。线程池执行，结果带缓存。"""

    def __init__(
        self,
        cache_size: int = 128,
        timeout: float = 20.0,
        failed_ttl: float = 600.0,
        logger: Callable[[str, str], None] | None = None,
    ) -> None:
        self._cache: "OrderedDict[str, LinkInfo]" = OrderedDict()
        self._cache_size = max(1, int(cache_size))
        # 公开属性：引擎在配置热更新时会直接改写它。
        self.timeout = max(1.0, float(timeout))
        self._failed_ttl = max(0.0, float(failed_ttl))
        self._log = logger or (lambda level, message: None)
        self._lock = threading.Lock()

    # -- 缓存 ------------------------------------------------------------- #

    def cached(self, url: str) -> LinkInfo | None:
        """取缓存；失败的条目超过 TTL 视为过期，允许重试。"""
        with self._lock:
            info = self._cache.get(url)
            if info is None:
                return None
            if not info.ok and (time.time() - info.ts) > self._failed_ttl:
                return None
            self._cache.move_to_end(url)
            return info

    def _store(self, info: LinkInfo) -> None:
        info.ts = time.time()
        with self._lock:
            self._cache[info.url] = info
            self._cache.move_to_end(info.url)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    # -- 解析 ------------------------------------------------------------- #

    async def resolve(self, url: str) -> LinkInfo:
        hit = self.cached(url)
        if hit is not None:
            return hit
        loop = asyncio.get_running_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, self._resolve_sync, url),
                timeout=self.timeout + 15.0,
            )
        except asyncio.TimeoutError:
            info = LinkInfo(url=url, ok=False, error="解析超时")
        self._store(info)
        return info

    async def resolve_many(self, urls: list[str], limit: int = 3) -> dict[str, LinkInfo]:
        """解析一批 URL（最多 ``limit`` 个，含已缓存的）；返回 url → LinkInfo。"""
        result: dict[str, LinkInfo] = {}
        fresh: list[str] = []
        for url in urls:
            hit = self.cached(url)
            if hit is not None:
                result[url] = hit
            elif len(fresh) < max(0, limit):
                fresh.append(url)
        if fresh:
            infos = await asyncio.gather(*(self.resolve(url) for url in fresh))
            for url, info in zip(fresh, infos):
                result[url] = info
        return result

    def _expand_short_link(self, url: str) -> tuple[str, str]:
        """展开短链。返回 ``(真实地址, 错误说明)``；无法展开时地址为空串。"""
        if not _is_short_link(url):
            return "", ""
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                final = resp.geturl()
                # 只有跳到**非短链主机**才算展开成功；http→https 这种同主机
                # 跳转（b23.tv 常见）不算，得继续看正文。
                if final and final != url and not _is_short_link(final):
                    return final, ""
                raw = resp.read(_MAX_HTML_BYTES)
        except Exception:  # 展开失败就退回原地址继续尝试
            return "", ""

        text = raw.decode("utf-8", "replace")
        if '"code":-404' in text.replace(" ", ""):
            return "", "短链已失效"
        match = _SHORT_LINK_TARGET_RE.search(text)
        if match:
            return match.group(0), ""
        return "", ""

    def _resolve_sync(self, url: str) -> LinkInfo:
        # X/Twitter 推文单独处理：yt-dlp 只认「带视频的推文」，普通网页兜底
        # 也拿不到内容（X 前端是 JS 渲染的）。走推文接口，顺带让「[推文] …」
        # 也能注入模型上下文。
        if tweet.is_tweet_url(url):
            info = _tweet_link_info(url, self.timeout, self._log)
            if info is not None:
                return info
        module = load_yt_dlp()
        target, expand_error = self._expand_short_link(url)
        if expand_error:
            return LinkInfo(url=url, ok=False, error=expand_error)
        lookup = target or url
        if module is not None:
            info = self._extract_with_ytdlp(module, lookup)
            if info.ok:
                return _rekey(info, url, lookup)
        return _rekey(self._fetch_page(lookup), url, lookup)

    def _extract_with_ytdlp(self, module: Any, url: str) -> LinkInfo:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": self.timeout,
            "cachedir": False,
            "logger": _QuietLogger(),
        }
        try:
            with module.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp 抛出的异常类型很多，统一兜住
            return LinkInfo(url=url, ok=False, error=_short(exc))
        if not isinstance(data, dict):
            return LinkInfo(url=url, ok=False, error="无法解析该链接")
        if data.get("_type") == "playlist":
            entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
            if not entries:
                return LinkInfo(url=url, ok=False, error="播放列表为空")
            data = entries[0]
        title = str(data.get("title") or "").strip()
        if not title:
            return LinkInfo(url=url, ok=False, error="未取到标题")
        return LinkInfo(
            url=url,
            ok=True,
            title=title,
            uploader=str(data.get("uploader") or data.get("channel") or "").strip(),
            duration=data.get("duration"),
            description=str(data.get("description") or "").strip(),
            extractor=str(
                data.get("extractor_key") or data.get("extractor") or ""
            ).strip(),
            is_media=True,
            webpage_url=str(data.get("webpage_url") or url),
        )

    def _fetch_page(self, url: str) -> LinkInfo:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                content_type = resp.headers.get_content_type()
                charset = resp.headers.get_content_charset()
                raw = resp.read(_MAX_HTML_BYTES)
        except urllib.error.HTTPError as exc:
            return LinkInfo(url=url, ok=False, error=f"HTTP {exc.code}")
        except Exception as exc:
            return LinkInfo(url=url, ok=False, error=_short(exc))

        if content_type and not (
            content_type.startswith("text/") or "html" in content_type
        ):
            return LinkInfo(url=url, ok=False, error=f"非网页内容（{content_type}）")
        try:
            markup = raw.decode(charset or "utf-8", "replace")
        except (LookupError, TypeError):
            markup = raw.decode("utf-8", "replace")

        title = _html_title(markup)
        description = _html_description(markup)
        if not title and not description:
            return LinkInfo(url=url, ok=False, error="未能提取标题")
        return LinkInfo(
            url=url,
            ok=True,
            title=title,
            description=description,
            is_media=False,
            webpage_url=url,
        )

    # -- 下载 ------------------------------------------------------------- #

    async def download(
        self, url: str, outdir: str, max_mb: int = 0
    ) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._download_sync, url, str(outdir), int(max_mb or 0)
        )

    def _download_sync(self, url: str, outdir: str, max_mb: int) -> str | None:
        module = load_yt_dlp()
        if module is None:
            self._log("error", "内置 yt-dlp 不可用，无法下载链接内容。")
            return None
        # 短链（如 b23.tv）yt-dlp 无法直接下载，先展开成真实地址。
        target, expand_error = self._expand_short_link(url)
        if expand_error:
            self._log("warning", f"下载链接内容失败 {url}：{expand_error}")
            return None
        url = target or url
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log("error", f"创建下载目录失败：{exc}")
            return None

        try:
            before = {p.name for p in Path(outdir).iterdir() if p.is_file()}
        except OSError:
            before = set()

        state: dict[str, str] = {}

        def _hook(payload: dict) -> None:
            if payload.get("status") == "finished" and payload.get("filename"):
                state["path"] = str(payload["filename"])

        # 有 ffmpeg 才请求「最佳视频+最佳音频」合流；否则退化为单文件，
        # 避免因缺少 ffmpeg 直接失败。
        ffmpeg = _ffmpeg_exe()
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": self.timeout,
            "cachedir": False,
            "windowsfilenames": True,
            "outtmpl": os.path.join(outdir, "%(title).80s [%(id)s].%(ext)s"),
            "format": "bv*+ba/b" if ffmpeg else "b",
            "merge_output_format": "mp4",
            "progress_hooks": [_hook],
            "logger": _QuietLogger(),
        }
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg
        if max_mb > 0:
            options["max_filesize"] = max_mb * 1024 * 1024

        try:
            with module.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=True)
        except Exception as exc:
            detail = _short(exc)
            if not ffmpeg and "format is not available" in detail.lower():
                detail += "（该站点只提供分离音视频流，需要 ffmpeg 合流）"
            self._log("warning", f"下载链接内容失败 {url}：{detail}")
            return None

        # 优先用 yt-dlp 给出的最终路径：多流合流时 progress hook 的 filename
        # 指向的是「分片」文件（合流后已被删除），不能直接拿来用。
        candidates: list[str] = []
        if isinstance(data, dict):
            downloads = data.get("requested_downloads") or []
            if downloads and isinstance(downloads[0], dict):
                for key in ("filepath", "_filename"):
                    if downloads[0].get(key):
                        candidates.append(str(downloads[0][key]))
            if data.get("_filename"):
                candidates.append(str(data["_filename"]))
        if state.get("path"):
            candidates.append(state["path"])
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        return _newest_new_file(outdir, before)
