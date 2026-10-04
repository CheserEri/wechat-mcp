"""PyInstaller 打包入口：启动 wechat-mcp 的 stdio MCP 服务。"""

from wechat_mcp.server import main

if __name__ == "__main__":
    main()