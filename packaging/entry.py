"""PyInstaller 打包入口。

- 无参数（默认）：启动 stdio MCP 服务（供 DSH 等 MCP 客户端调用）。
- ``--gui``：启动桌面窗口（实时自动回复助手）。
- ``--selfcheck``：打印运行环境自检信息（版本、内置 yt-dlp、ffmpeg 等）。
- ``--resolve <url> [--download] [--outdir <目录>]``：诊断用，直接解析（可选下载）
  一个链接并打印结果，用于验证链接解析链路。
- ``--tweet <推文链接> [--outdir <目录>]``：诊断用，抓取 X 推文并渲染成卡片图。
"""

import sys

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


def _hide_gui_console() -> None:
    """隐藏随 exe 自动分配的控制台窗口（桌面端）。

    打包出的 exe 是控制台程序（MCP stdio 模式要靠 stdout 通信），双击运行时
    系统会给它分配一个黑色命令行窗口。桌面端用不到它：这里直接隐藏，原本打在
    控制台的内容由 ``wechat_mcp.desktop`` 转投到界面「运行日志」。

    只在打包产物中隐藏，且在已有终端里运行时（控制台与其它进程共享，例如用户
    在 cmd 里手动执行）不动它，免得把用户的终端窗口一起藏掉。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        processes = (ctypes.c_uint32 * 2)()
        if kernel32.GetConsoleProcessList(processes, 2) > 1:
            return
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:  # noqa: BLE001 - 隐藏失败不影响正常启动
        pass


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
        # 先隐藏控制台再导入桌面端：desktop 会拉起 pywebview/pythonnet，
        # 导入耗时较长，不先隐藏的话那个黑窗口会在屏幕上多停留一会儿。
        _hide_gui_console()
        from wechat_mcp.desktop import run as run_desktop

        run_desktop()
    else:
        run_server()
