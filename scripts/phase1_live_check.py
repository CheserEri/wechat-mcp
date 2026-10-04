"""阶段 1 联调脚本：连接微信、可选发送、可选监听并输出读取结果。

用法（在项目根目录执行，需微信已登录且主窗口可见）::

    .venv\\Scripts\\python.exe scripts\\phase1_live_check.py --listen 30
    .venv\\Scripts\\python.exe scripts\\phase1_live_check.py --send 文件传输助手 --text "联调测试"
    .venv\\Scripts\\python.exe scripts\\phase1_live_check.py --history 测试群 --limit 5

说明：读取能力基于监听回调的被动缓冲，`--listen` 期间收到消息才会出现在结果中。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mcp.adapters.deepseekgirl import DeepSeekGirlAdapter  # noqa: E402
from wechat_mcp.config import AdapterConfig  # noqa: E402


def _dump(label: str, payload: dict) -> None:
    print(f"{label}={json.dumps(payload, ensure_ascii=False)}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-mcp 阶段1 联调检查")
    parser.add_argument("--listen", type=float, default=0.0, help="连接后保持监听的秒数")
    parser.add_argument("--send", default="", help="要发送的目标名称（留空则不发送）")
    parser.add_argument("--text", default="[wechat-mcp 联调测试] 这是一条自动化测试消息，可忽略。")
    parser.add_argument("--history", default="", help="要读取历史的会话名")
    parser.add_argument("--limit", type=int, default=10, help="历史消息条数上限")
    args = parser.parse_args()

    adapter = DeepSeekGirlAdapter(AdapterConfig.from_env())
    status = await adapter.connect()
    _dump("CONNECT", status.to_dict())
    if not status.ok:
        print("连接失败，请确认微信已登录且主窗口可见。")
        return 1

    try:
        if args.send:
            result = await adapter.send_message(args.send, args.text)
            _dump("SEND", result.to_dict())

        if args.listen > 0:
            print(
                f"LISTENING up to {args.listen}s ...（请在此期间向任意聊天发送一条新消息，收到即返回）",
                flush=True,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + args.listen
            while loop.time() < deadline:
                await asyncio.sleep(1.0)
                if adapter.get_chat_list().chats:
                    print("MESSAGE_RECEIVED", flush=True)
                    break

        listing = adapter.get_chat_list()
        _dump("CHAT_LIST", listing.to_dict())
        if args.history == "*":
            for chat in listing.chats:
                _dump(
                    "CHAT_HISTORY",
                    adapter.get_chat_history(chat.name, limit=args.limit).to_dict(),
                )
        elif args.history:
            _dump(
                "CHAT_HISTORY",
                adapter.get_chat_history(args.history, limit=args.limit).to_dict(),
            )
    finally:
        await adapter.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
