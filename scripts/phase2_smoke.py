"""阶段 2 冒烟测试：以真实 stdio 传输启动 MCP Server，发现工具并调用状态工具。

用法（项目根目录，需微信已登录）::

    .venv\\Scripts\\python.exe scripts\\phase2_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wechat_mcp.server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print("TOOLS=" + json.dumps(names, ensure_ascii=False), flush=True)

            result = await session.call_tool("get_wechat_status", {})
            payload = result.structured_content or result.content
            print(
                "STATUS=" + json.dumps(payload, ensure_ascii=False, default=str),
                flush=True,
            )

            result = await session.call_tool("get_chat_list", {"limit": 5})
            payload = result.structured_content or result.content
            print(
                "CHAT_LIST=" + json.dumps(payload, ensure_ascii=False, default=str),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))