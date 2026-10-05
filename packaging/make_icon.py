"""生成安装包与快捷方式用的应用图标（``packaging/wechat-mcp.ico``）。

图标采用项目在界面（侧栏品牌）与人设中使用的鲸鱼标识（🐋）：直接用系统
彩色 emoji 字体渲染，再按 16~256 px 重新取样，小尺寸下仍然可辨。用法：

    .venv\\Scripts\\python.exe packaging\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "wechat-mcp.ico"

# 先按大尺寸绘制再降采样，等效于抗锯齿。
S = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256]

EMOJI = "\U0001F40B"  # 🐋 鲸鱼
# Windows 自带的彩色 emoji 字体；缺失时退回按字体名查找。
EMOJI_FONTS = (r"C:\Windows\Fonts\seguiemj.ttf", "seguiemj.ttf")
WHALE_RATIO = 0.80  # 鲸鱼占画布的比例

TOP = (234, 241, 255)     # #EAF1FF
BOTTOM = (211, 226, 255)  # #D3E2FF


def _load_emoji_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in EMOJI_FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    raise SystemExit("未找到彩色 emoji 字体（seguiemj.ttf），无法生成图标")


def _background() -> Image.Image:
    """圆角方形底 + 自上而下的浅蓝渐变。"""
    grad = Image.new("RGB", (1, S))
    pixels = grad.load()
    for y in range(S):
        t = y / (S - 1)
        pixels[0, y] = tuple(
            round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)
        )
    grad = grad.resize((S, S), Image.NEAREST)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=255
    )
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _whale() -> Image.Image:
    """按 ARGB 裁剪后的彩色鲸鱼 emoji，等比缩放到画布的 ``WHALE_RATIO``。"""
    font = _load_emoji_font(S)
    canvas = Image.new("RGBA", (S * 2, S * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text(
        (S, S), EMOJI, font=font, embedded_color=True, anchor="mm"
    )
    bbox = canvas.split()[3].getbbox()  # 以 alpha 通道求实际内容边界
    if bbox is None:
        raise SystemExit("emoji 字体未渲染出鲸鱼字形")
    whale = canvas.crop(bbox)

    target = int(S * WHALE_RATIO)
    width, height = whale.size
    scale = target / max(width, height)
    return whale.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.LANCZOS,
    )


def main() -> None:
    icon = _background()
    whale = _whale()
    icon.alpha_composite(
        whale, ((S - whale.width) // 2, (S - whale.height) // 2)
    )
    icon = icon.resize((256, 256), Image.LANCZOS)
    icon.save(OUT, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"已生成 {OUT}（{', '.join(str(s) for s in SIZES)} px）")


if __name__ == "__main__":
    main()
