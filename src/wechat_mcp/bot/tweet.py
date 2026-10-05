"""X（Twitter）推文卡片：抓取推文数据，并用 PIL 本地渲染成一张图片。

数据源
------
X 的 **syndication** 接口（公开、无需登录、无需 API Key）::

    https://cdn.syndication.twimg.com/tweet-result?id=<推文ID>&token=<token>

``token`` 由推文 ID 算出（算法同 react-tweet，见 ``_token``）。实测该接口对
token 只要求**非空**（给错值也返回数据），但仍按正确算法生成，避免哪天收紧。

渲染
----
完全用 PIL 本地绘制（**不依赖浏览器**，离线可用），产出接近 X 推文卡片的图：
头像、昵称、认证徽标、@handle、正文、配图、底部时间与互动数。

字体：中文/西文用「微软雅黑」，emoji 用「Segoe UI Emoji」——**按字符选字体**，
因为雅黑没有 emoji 字形、Segoe UI Emoji 没有中文字形，单用一种必然出豆腐块。

视频：``parse_tweet`` 会顺带取回**最佳 mp4 直链**（``Tweet.video_url``），
由 ``download_video`` 直接流式下载——不依赖 yt-dlp 的 X 提取器（其默认走
GraphQL，未登录时容易 403）。yt-dlp 仅作为拿不到直链时的兜底。
"""

from __future__ import annotations

import io
import json
import math
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# 推文 URL
# --------------------------------------------------------------------------- #

# 形如 https://x.com/<user>/status/<id>（也兼容 twitter.com / mobile.x.com 等）。
_TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/"
    r"(?P<user>[A-Za-z0-9_]{1,20})/status(?:es)?/(?P<id>\d+)",
    re.IGNORECASE,
)

_API = "https://cdn.syndication.twimg.com/tweet-result"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_tweet_url(url: str) -> bool:
    """是否 X/Twitter 推文链接。"""
    return _TWEET_RE.match(url or "") is not None


def tweet_id(url: str) -> str | None:
    """取推文数字 ID；不是推文链接时返回 ``None``。"""
    match = _TWEET_RE.match(url or "")
    return match.group("id") if match else None


def _base36(value: float) -> str:
    """浮点转 36 进制（模拟 JS 的 ``Number.prototype.toString(36)``）。"""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole = int(value)
    frac = value - whole
    out = "0" if whole == 0 else ""
    while whole:
        out = digits[whole % 36] + out
        whole //= 36
    if frac:
        out += "."
        for _ in range(25):
            frac *= 36
            digit = int(frac)
            out += digits[digit]
            frac -= digit
            if frac == 0:
                break
    return out


def _token(tid: str) -> str:
    """syndication 接口的 token：``((id / 1e15) * PI).toString(36)`` 去掉 0 与点。"""
    return _base36((int(tid) / 1e15) * math.pi).replace("0", "").replace(".", "")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class Tweet:
    """一条推文（卡片渲染所需的全部字段）。"""

    id: str
    url: str
    text: str = ""
    author_name: str = ""
    author_handle: str = ""
    avatar_url: str = ""
    # 认证："" / "blue" / "business" / "government"
    verified: str = ""
    created_at: str = ""
    photos: list[str] = field(default_factory=list)
    has_video: bool = False
    video_url: str = ""  # 最佳 mp4 直链（有视频时才有）
    video_poster: str = ""  # 视频/动图的封面帧（卡片上用它 + 播放角标）
    likes: int = 0
    replies: int = 0

    def summary(self) -> str:
        """压成一行注入模型上下文。"""
        parts: list[str] = []
        if self.author_name:
            handle = f"（@{self.author_handle}）" if self.author_handle else ""
            parts.append(f"作者：{self.author_name}{handle}")
        if self.created_at:
            parts.append(f"时间：{self.created_at}")
        if self.has_video:
            parts.append("含视频")
        elif self.photos:
            parts.append(f"含 {len(self.photos)} 张图")
        if self.likes:
            parts.append(f"点赞：{self.likes}")
        body = re.sub(r"\s+", " ", self.text).strip()
        head = f"[推文] {self.url}"
        if body:
            head += f"｜正文：{body[:200]}"
        return head + ("｜" + "；".join(parts) if parts else "")


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def _fmt_time(raw: str) -> str:
    """``2014-09-03T15:18:45.000Z`` → ``2014-09-03 15:18``（转本地时区）。"""
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _best_video_url(media: list[dict]) -> str:
    """从 ``mediaDetails`` 里挑最高码率的 mp4 直链。

    ``animated_gif``（动图）本质也是 mp4，同样可用；只要没有 mp4 变体就返回空串。
    """
    best_url = ""
    best_bitrate = -1
    for item in media:
        if not isinstance(item, dict):
            continue
        variants = ((item.get("video_info") or {}).get("variants")) or []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            url = str(variant.get("url") or "")
            if not url or variant.get("content_type") != "video/mp4":
                continue
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            if bitrate > best_bitrate:
                best_bitrate, best_url = bitrate, url
    return best_url


def parse_tweet(data: dict, url: str) -> Tweet | None:
    """把 syndication 的 JSON 转成 :class:`Tweet`。"""
    if not isinstance(data, dict) or not data.get("id_str"):
        return None
    user = data.get("user") or {}
    photos = [
        str(item["url"])
        for item in (data.get("photos") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    media = [item for item in (data.get("mediaDetails") or []) if isinstance(item, dict)]
    verified = ""
    if user.get("is_blue_verified"):
        verified = str(user.get("verified_type") or "blue").lower()
    elif user.get("verified"):
        verified = "blue"
    has_video = any(m.get("type") in ("video", "animated_gif") for m in media)
    # 视频/动图没有 photos 条目，但 syndication 给了封面帧（media_url_https）；
    # 把它补进配图列表，卡片才有缩略图可画。
    poster = ""
    for item in media:
        if item.get("type") in ("video", "animated_gif"):
            poster = str(item.get("media_url_https") or "")
            if poster:
                break
    if poster and poster not in photos:
        photos.append(poster)
    return Tweet(
        id=str(data["id_str"]),
        url=url,
        text=str(data.get("text") or ""),
        author_name=str(user.get("name") or ""),
        author_handle=str(user.get("screen_name") or ""),
        avatar_url=str(user.get("profile_image_url_https") or ""),
        verified=verified,
        created_at=_fmt_time(str(data.get("created_at") or "")),
        photos=photos,
        has_video=has_video,
        video_url=_best_video_url(media) if has_video else "",
        video_poster=poster if has_video else "",
        likes=int(data.get("favorite_count") or 0),
        replies=int(data.get("conversation_count") or 0),
    )


def fetch_tweet(
    url: str,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Tweet | None:
    """抓取推文；失败返回 ``None``。"""
    log = logger or (lambda level, message: None)
    tid = tweet_id(url)
    if not tid:
        return None
    api = f"{_API}?id={tid}&token={_token(tid)}&lang=en"
    try:
        payload = json.loads(_get(api, timeout))
    except urllib.error.HTTPError as exc:
        log("warning", f"推文抓取失败（HTTP {exc.code}）：{url}")
        return None
    except Exception as exc:  # 网络/解析异常统一兜住
        log("warning", f"推文抓取失败：{exc}")
        return None
    tweet = parse_tweet(payload, url)
    if tweet is None:
        log("warning", f"推文不存在或已删除：{url}")
    return tweet


_CACHE: dict[str, Tweet] = {}
_CACHE_LOCK = threading.Lock()


def fetch_tweet_cached(
    url: str,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Tweet | None:
    """带缓存的抓取：同一条推文在一次运行里只请求一次（成功才缓存）。

    解析上下文（``links.LinkResolver``）与发送卡片都会用到推文数据，
    缓存一下避免同一条推文被抓两遍。
    """
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
    if hit is not None:
        return hit
    info = fetch_tweet(url, timeout, logger)
    if info is not None:
        with _CACHE_LOCK:
            _CACHE[url] = info
    return info


def download_video(
    info: Tweet,
    outdir: str | Path,
    timeout: float = 60.0,
    logger: Callable[[str, str], None] | None = None,
    max_mb: int = 0,
) -> Path | None:
    """把推文里的视频下载到 ``outdir``，返回文件路径；失败返回 ``None``。

    直接用 ``Tweet.video_url``（syndication 给的 mp4 直链）流式下载，
    因此不依赖 yt-dlp 的 X 提取器。``max_mb`` > 0 时超限即中断并删除残文件。
    """
    log = logger or (lambda level, message: None)
    url = info.video_url
    if not url:
        log("warning", f"推文未提供视频直链：{info.url}")
        return None
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"tweet-{info.id}.mp4"
    limit = max_mb * 1024 * 1024 if max_mb and max_mb > 0 else 0
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://x.com/"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            declared = resp.headers.get("Content-Length")
            if limit and declared and int(declared) > limit:
                log("warning", f"推文视频超过体积上限（{declared} 字节），已跳过")
                return None
            written = 0
            with target.open("wb") as handle:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if limit and written > limit:
                        raise ValueError("视频超过体积上限")
                    handle.write(chunk)
    except Exception as exc:
        log("warning", f"推文视频下载失败：{exc}")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        log("warning", "推文视频下载为空文件")
        return None
    return target


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

CARD_WIDTH = 760
_PAD = 28
_AVATAR = 76
_NAME_SIZE = 27
_HANDLE_SIZE = 21
_BODY_SIZE = 25
_FOOT_SIZE = 20
_LINE_GAP = 10
_MEDIA_RADIUS = 14
_MAX_MEDIA_HEIGHT = 460

# 中英混排用雅黑，emoji 用 Segoe UI Emoji（单色线条，但不会变豆腐块）。
_TEXT_FONTS = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf")
_BOLD_FONTS = (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\segoeuib.ttf")
_EMOJI_FONTS = (r"C:\Windows\Fonts\seguiemj.ttf",)


@lru_cache(maxsize=32)
def _font(paths: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


@lru_cache(maxsize=8)
def _fonts(size_key: str) -> dict[str, ImageFont.FreeTypeFont]:
    sizes = {
        "name": _NAME_SIZE,
        "handle": _HANDLE_SIZE,
        "body": _BODY_SIZE,
        "foot": _FOOT_SIZE,
    }
    size = sizes[size_key]
    return {
        "text": _font(_TEXT_FONTS, size),
        "bold": _font(_BOLD_FONTS, size),
        "emoji": _font(_EMOJI_FONTS, size),
    }


# emoji / 符号区段（这些用雅黑会变豆腐块）。
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # 各类象形/表情/交通/补充符号
    (0x1F1E6, 0x1F1FF),  # 区域指示符（国旗）
    (0x2600, 0x27BF),    # 杂项符号、装饰符号（含 ✓ ✨ 等）
    (0x2B00, 0x2BFF),    # 杂项符号与箭头
    (0xFE00, 0xFE0F),    # 变体选择符
    (0x1F900, 0x1F9FF),
)
_ZERO_WIDTH = {"\u200d", "\ufe0e", "\ufe0f", "\u200b"}


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def _glyphs(text: str) -> list[tuple[str, str]]:
    """拆成 ``(字符, 字体键)``；零宽字符直接丢掉。"""
    out: list[tuple[str, str]] = []
    for ch in text:
        if ch in _ZERO_WIDTH:
            continue
        out.append((ch, "emoji" if _is_emoji(ch) else "text"))
    return out


def _width(glyphs: list[tuple[str, str]], fonts: dict) -> float:
    return sum(fonts[key].getlength(ch) for ch, key in glyphs)


def _wrap(text: str, fonts: dict, max_width: float) -> list[list[tuple[str, str]]]:
    """按宽度折行：西文尽量在空格处断，中日韩逐字断。"""
    lines: list[list[tuple[str, str]]] = []
    for raw_line in text.split("\n"):
        glyphs = _glyphs(raw_line)
        current: list[tuple[str, str]] = []
        width = 0.0
        for ch, key in glyphs:
            char_width = fonts[key].getlength(ch)
            if current and width + char_width > max_width:
                # 行内最后一个空格处断开（避免把英文单词劈开）
                cut = -1
                for index in range(len(current) - 1, -1, -1):
                    if current[index][0] == " ":
                        cut = index
                        break
                if cut > 0:
                    lines.append(current[:cut])
                    current = current[cut + 1 :]
                else:
                    lines.append(current)
                    current = []
                width = _width(current, fonts)
            current.append((ch, key))
            width += char_width
        lines.append(current)
    return lines


def _draw_glyphs(draw: ImageDraw.ImageDraw, x: float, y: float, glyphs, fonts, fill):
    for ch, key in glyphs:
        draw.text((x, y), ch, font=fonts[key], fill=fill)
        x += fonts[key].getlength(ch)
    return x


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _load_image(url: str, timeout: float) -> Image.Image | None:
    try:
        raw = _get(url, timeout)
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _draw_badge(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, kind: str) -> None:
    """认证徽标：蓝底白勾（政府/企业也统一画蓝，避免配色歧义）。"""
    color = (29, 155, 240) if kind in ("", "blue", "business") else (120, 86, 255)
    draw.ellipse((x, y, x + size, y + size), fill=color)
    cx, cy = x + size / 2, y + size / 2
    draw.line(
        [
            (cx - size * 0.22, cy + size * 0.02),
            (cx - size * 0.06, cy + size * 0.18),
            (cx + size * 0.24, cy - size * 0.18),
        ],
        fill="white",
        width=max(2, int(size * 0.13)),
        joint="curve",
    )


def _draw_play_badge(card: Image.Image, left: int, top: int, width: int, height: int) -> None:
    """在视频封面正中画一个半透明播放角标（卡片本身是 RGB，故叠一层 RGBA 再贴回）。"""
    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    cx, cy = left + width / 2, top + height / 2
    radius = max(22, min(width, height) * 0.14)
    odraw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=(0, 0, 0, 140)
    )
    tri = radius * 0.46
    odraw.polygon(
        [
            (cx - tri * 0.55, cy - tri),
            (cx - tri * 0.55, cy + tri),
            (cx + tri * 0.85, cy),
        ],
        fill=(255, 255, 255, 235),
    )
    merged = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    card.paste(merged, (0, 0))


def render_card(
    tweet: Tweet,
    outdir: str | Path,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Path | None:
    """把推文渲染成 PNG，返回文件路径；失败返回 ``None``。"""
    log = logger or (lambda level, message: None)
    outdir = Path(outdir)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("warning", f"创建卡片目录失败：{exc}")
        return None

    body_fonts = _fonts("body")
    inner = CARD_WIDTH - _PAD * 2
    text_x = _PAD + _AVATAR + 20
    text_width = CARD_WIDTH - _PAD - text_x

    lines = _wrap(tweet.text, body_fonts, text_width) if tweet.text else []
    body_height = sum(
        max(body_fonts[key].getbbox(ch)[3] for ch, key in line) + _LINE_GAP
        for line in lines
        if line
    )
    line_h = _BODY_SIZE + _LINE_GAP

    # 媒体：单图按原比例（限高），多图走两列网格。视频封面额外画播放角标。
    media: list[tuple[Image.Image, bool]] = []
    for url in tweet.photos[:4]:
        img = _load_image(url, timeout)
        if img is not None:
            media.append((img, bool(tweet.video_poster) and url == tweet.video_poster))
    media_boxes: list[tuple[Image.Image, tuple[int, int, int, int], bool]] = []
    if media:
        gap = 8
        if len(media) == 1:
            img, play = media[0]
            w = inner
            h = min(int(w * img.height / max(1, img.width)), _MAX_MEDIA_HEIGHT)
            media_boxes.append((img, (_PAD, 0, w, h), play))
        else:
            cols = 2
            rows = (len(media) + cols - 1) // cols
            cell_w = (inner - gap) // 2
            cell_h = int(cell_w * 0.62)
            for index, (img, play) in enumerate(media):
                row, col = divmod(index, cols)
                media_boxes.append(
                    (
                        img,
                        (
                            _PAD + col * (cell_w + gap),
                            row * (cell_h + gap),
                            cell_w,
                            cell_h,
                        ),
                        play,
                    )
                )
    media_height = 0
    if media_boxes:
        media_height = max(top + height for _, (_, top, _, height), _ in media_boxes)

    name_fonts = _fonts("name")
    handle_fonts = _fonts("handle")
    foot_fonts = _fonts("foot")

    head_bottom = _PAD + _AVATAR
    body_top = head_bottom + 20
    media_top = body_top + body_height + (16 if media_boxes else 0)
    foot_top = media_top + media_height + (18 if media_boxes else 0)
    footer = " · ".join(
        part
        for part in (
            tweet.created_at,
            f"{tweet.likes} 喜欢" if tweet.likes else "",
            f"{tweet.replies} 回复" if tweet.replies else "",
        )
        if part
    )
    height = int(foot_top + _FOOT_SIZE + _PAD + 6)

    card = Image.new("RGB", (CARD_WIDTH, height), (255, 255, 255))
    draw = ImageDraw.Draw(card)

    # 头像
    if tweet.avatar_url:
        avatar = _load_image(tweet.avatar_url, timeout)
        if avatar is not None:
            card.paste(_circle(avatar, _AVATAR), (_PAD, _PAD), _circle(avatar, _AVATAR))

    # 昵称 + 认证
    name_x = _draw_glyphs(
        draw, text_x, _PAD + 2, _glyphs(tweet.author_name or "未知用户"), name_fonts, (15, 20, 25)
    )
    if tweet.verified:
        _draw_badge(draw, name_x + 8, _PAD + 8, _NAME_SIZE - 4, tweet.verified)
    # @handle
    _draw_glyphs(
        draw,
        text_x,
        _PAD + 2 + _NAME_SIZE + 8,
        _glyphs(f"@{tweet.author_handle}" if tweet.author_handle else ""),
        handle_fonts,
        (83, 100, 113),
    )

    # 正文
    y = body_top
    for line in lines:
        if not line:
            y += line_h
            continue
        _draw_glyphs(draw, text_x, y, line, body_fonts, (15, 20, 25))
        y += max(body_fonts[key].getbbox(ch)[3] for ch, key in line) + _LINE_GAP

    # 配图（圆角；视频封面加播放角标）
    for img, (left, top, width, height_), play in media_boxes:
        box = img.resize((width, height_), Image.LANCZOS)
        mask = _rounded_mask((width, height_), _MEDIA_RADIUS)
        card.paste(box, (left, int(media_top) + top), mask)
        if play:
            _draw_play_badge(card, left, int(media_top) + top, width, height_)

    # 底部时间 / 互动
    if footer:
        _draw_glyphs(
            draw, text_x, foot_top, _glyphs(footer), foot_fonts, (83, 100, 113)
        )

    path = outdir / f"tweet-{tweet.id}.png"
    try:
        card.save(path, "PNG")
    except OSError as exc:
        log("warning", f"保存推文卡片失败：{exc}")
        return None
    return path
