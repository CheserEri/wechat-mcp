"""PyInstaller 打包入口。

- 无参数（默认）：启动 stdio MCP 服务（供 DSH 等 MCP 客户端调用）。
- ``--gui``：启动桌面窗口（实时自动回复助手）。
"""

import sys

from wechat_mcp.desktop import run as run_desktop
from wechat_mcp.server import main as run_server

if __name__ == "__main__":
    if "--gui" in sys.argv[1:]:
        run_desktop()
    else:
        run_server()
