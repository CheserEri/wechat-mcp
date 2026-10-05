"""PyInstaller 打包入口。

- 无参数（默认）：启动 stdio MCP 服务（供 DSH 等 MCP 客户端调用）。
- ``--gui``：启动桌面窗口（实时自动回复助手）。
- ``--selfcheck``：打印运行环境自检信息（版本、内置 yt-dlp、ffmpeg 等）。
- ``--resolve <url> [--download] [--outdir <目录>]``：诊断用，直接解析（可选下载）
  一个链接并打印结果，用于验证链接解析链路。
- ``--tweet <推文链接> [--outdir <目录>]``：诊断用，抓取 X 推文并渲染成卡片图。
"""

import sys

from wechat_mcp.desktop import run as run_desktop
from wechat_mcp.selfcheck import main as run_selfcheck
from wechat_mcp.selfcheck import resolve as run_resolve
from wechat_mcp.selfcheck import tweet_card as run_tweet_card
from wechat_mcp.server import main as run_server


def _flag_value(args: list[str], flag: str) -> str | None:
    """取 ``--flag value`` 形式的参数值；缺值时返回 ``None``。"""
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selfcheck" in args:
        raise SystemExit(run_selfcheck())
    if "--resolve" in args:
        raise SystemExit(
            run_resolve(
                _flag_value(args, "--resolve") or "",
                download="--download" in args,
                outdir=_flag_value(args, "--outdir"),
            )
        )
    if "--tweet" in args:
        raise SystemExit(
            run_tweet_card(
                _flag_value(args, "--tweet") or "",
                outdir=_flag_value(args, "--outdir"),
            )
        )
    if "--gui" in args:
        run_desktop()
    else:
        run_server()
