"""MCP Server 工具注册与调用测试。

通过注入 FakeBridge，无需真实微信即可验证：工具可被发现、参数校验与
错误语义正确、发送结果如实表达。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_adapter import FakeBridge, make_message  # noqa: E402
from wechat_mcp.adapters.deepseekgirl import DeepSeekGirlAdapter  # noqa: E402
from wechat_mcp.audit import AuditLogger  # noqa: E402
from wechat_mcp.config import AdapterConfig  # noqa: E402
from wechat_mcp.server import build_server  # noqa: E402
from wechat_mcp.service import WeChatService  # noqa: E402

EXPECTED_TOOLS = {
    "get_wechat_status",
    "get_chat_list",
    "get_chat_history",
    "send_message",
    "search_contact",
    "get_chat_info",
    "send_file",
    "get_recent_messages",
    "set_monitored_chats",
}


def extract(result) -> dict:
    """从 CallToolResult 中取出结构化结果。"""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


class ServerToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bridge = FakeBridge()
        adapter = DeepSeekGirlAdapter(
            config=AdapterConfig(listen_on_connect=True, audit_enabled=False),
            bridge_factory=lambda: self.bridge,
        )
        self.server = build_server(service=WeChatService(adapter=adapter))

    async def test_discovers_all_tools(self):
        names = {tool.name for tool in await self.server.list_tools()}
        self.assertEqual(names, EXPECTED_TOOLS)

    async def test_status_tool_connects_and_reports(self):
        result = extract(await self.server.call_tool("get_wechat_status", {}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "connected")

    async def test_chat_list_and_history_tools(self):
        await self.server.call_tool("get_wechat_status", {})  # 建立连接并接入回调
        await self.bridge.on_message(make_message("主账号", "你好", message_id="1"))
        listing = extract(await self.server.call_tool("get_chat_list", {}))
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["total"], 1)
        history = extract(
            await self.server.call_tool(
                "get_chat_history", {"chat_name": "主账号", "limit": 5}
            )
        )
        self.assertTrue(history["ok"])
        self.assertEqual(history["messages"][0]["content"], "你好")

    async def test_send_message_tool_success(self):
        result = extract(
            await self.server.call_tool(
                "send_message", {"recipient": "主账号", "message": "测试"}
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.bridge.sent, [("主账号", "测试")])

    async def test_send_message_tool_dry_run(self):
        result = extract(
            await self.server.call_tool(
                "send_message",
                {"recipient": "主账号", "message": "测试", "dry_run": True},
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.bridge.sent, [])

    async def test_send_message_tool_rejects_empty_recipient(self):
        result = extract(
            await self.server.call_tool(
                "send_message", {"recipient": " ", "message": "测试"}
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "target_invalid")

    async def test_send_failure_not_reported_as_success(self):
        self.bridge.fail_send = True
        result = extract(
            await self.server.call_tool(
                "send_message", {"recipient": "主账号", "message": "测试"}
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")

    # ---------------------------------------------------------------- P1 工具

    async def test_search_contact_tool(self):
        self.bridge._wx._db.rows = [
            {"username": "wxid_a", "nick_name": "老王", "remark": ""}
        ]
        result = extract(
            await self.server.call_tool("search_contact", {"keyword": "老王"})
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["candidates"][0]["name"], "老王")

    async def test_get_chat_info_tool(self):
        await self.server.call_tool("get_wechat_status", {})
        await self.bridge.on_message(make_message("主账号", "你好", message_id="1"))
        result = extract(
            await self.server.call_tool("get_chat_info", {"chat_name": "主账号"})
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_count"], 1)
        self.assertTrue(result["monitored"])

    async def test_get_recent_messages_tool(self):
        await self.server.call_tool("get_wechat_status", {})
        await self.bridge.on_message(make_message("主账号", "你好", message_id="1"))
        result = extract(
            await self.server.call_tool("get_recent_messages", {"after_seq": 0})
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["messages"][0]["content"], "你好")
        self.assertEqual(result["next_seq"], 1)

    async def test_set_monitored_chats_tool_filters_reads(self):
        await self.server.call_tool("get_wechat_status", {})
        await self.bridge.on_message(make_message("工作群", "a", message_id="1"))
        await self.bridge.on_message(make_message("闲聊群", "b", message_id="2"))
        updated = extract(
            await self.server.call_tool(
                "set_monitored_chats", {"add": ["工作群"], "mode": "allow"}
            )
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["allow"], ["工作群"])
        listing = extract(await self.server.call_tool("get_chat_list", {}))
        self.assertEqual([c["name"] for c in listing["chats"]], ["工作群"])

    async def test_send_file_tool_rejects_without_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            result = extract(
                await self.server.call_tool(
                    "send_file", {"recipient": "主账号", "file_path": str(target)}
                )
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "path_not_allowed")
        self.assertEqual(self.bridge.sent_files, [])


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_records_metadata_without_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            bridge = FakeBridge()
            adapter = DeepSeekGirlAdapter(
                config=AdapterConfig(listen_on_connect=True, audit_enabled=False),
                bridge_factory=lambda: bridge,
            )
            service = WeChatService(
                adapter=adapter,
                audit=AuditLogger(log_path, enabled=True),
            )
            server = build_server(service=service)
            await server.call_tool(
                "send_message", {"recipient": "主账号", "message": "机密正文"}
            )
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()

        entry = json.loads(lines[-1])
        self.assertEqual(entry["tool"], "send_message")
        self.assertEqual(entry["target"], "主账号")
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["status"], "sent")
        # 审计不应写入消息正文，只记录长度等元数据
        self.assertNotIn("机密正文", lines[-1])
        self.assertEqual(entry["extra"]["message_length"], 4)


if __name__ == "__main__":
    unittest.main()