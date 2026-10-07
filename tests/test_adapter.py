"""适配层单元测试。

通过注入 FakeBridge，无需安装 wxauto / wechatauto，也无需真实微信环境，
即可验证适配层的行为：状态、发送语义、目标消歧、读取缓冲。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mcp.adapters.deepseekgirl import DeepSeekGirlAdapter  # noqa: E402
from wechat_mcp.config import AdapterConfig  # noqa: E402
from wechat_mcp.schemas import ConnectionState, SendStatus  # noqa: E402


class FakeContactDb:
    """模拟 wechatauto 的本地联系人库。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.fail = False

    def search_contact(self, keyword: str) -> list[dict]:
        if self.fail:
            raise RuntimeError("联系人库不可用")
        return [
            row
            for row in self.rows
            if keyword in str(row.get("nick_name", ""))
            or keyword in str(row.get("remark", ""))
            or keyword in str(row.get("username", ""))
        ]


class FakeBridge:
    """模拟 deepseekgirl 的 WeChatBridge，仅实现适配层用到的最小接口。"""

    SEND_TIMEOUT_SECONDS = 5.0
    # 桥接层用类常量控制「附件发送失败后的重试」，适配层会按配置覆盖。
    FILE_SEND_ATTEMPTS = 2
    FILE_SEND_ENTER_NUDGES = 2

    def __init__(self) -> None:
        self.on_message = None
        self.listen_private = True
        self._backend = "fake"
        self._connected = False
        self._status = "disconnected"
        self._running = False
        self._listen_all_active = False
        self.listening = False
        self.sent: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, str]] = []
        self.fail_send = False
        self.raise_on_send = False
        self.fail_send_file = False
        # 供 search_contact 使用的本地联系人库句柄。
        self._wx = SimpleNamespace(_db=FakeContactDb())

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status,
            "target_groups": [],
            "listen_private": self.listen_private,
        }

    async def connect(self) -> bool:
        self._connected = True
        self._status = "connected"
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._status = "disconnected"

    async def send_text(self, room_name: str, content: str) -> bool:
        if self.raise_on_send:
            raise RuntimeError("底层异常")
        if self.fail_send:
            return False
        self.sent.append((room_name, content))
        return True

    async def send_file(self, room_name: str, file_path: str) -> bool:
        if self.fail_send_file:
            return False
        self.sent_files.append((room_name, file_path))
        return True

    def start_listening(self, loop) -> None:
        self.listening = True
        self._running = True
        self._listen_all_active = True

    def has_usable_uia(self) -> bool:
        return True

    def has_usable_gui(self) -> bool:
        return True


def make_message(chat: str, content: str, *, sender: str = "张三", is_group: bool = True,
                 message_id: str = "m1") -> SimpleNamespace:
    return SimpleNamespace(
        room_name=chat,
        room_id=chat,
        sender=sender,
        sender_name=sender,
        content=content,
        message_type="text",
        is_group=is_group,
        timestamp=time.time(),
        id=message_id,
    )


class AdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bridge = FakeBridge()
        self.adapter = DeepSeekGirlAdapter(
            config=AdapterConfig(listen_on_connect=True),
            bridge_factory=lambda: self.bridge,
        )

    async def _feed(self, *messages: SimpleNamespace) -> None:
        for message in messages:
            await self.bridge.on_message(message)

    # ---------------------------------------------------------------- 状态

    async def test_status_before_connect(self):
        status = self.adapter.get_status()
        self.assertFalse(status.ok)
        self.assertEqual(status.state, ConnectionState.DISCONNECTED)

    async def test_backend_unavailable_error_is_surfaced(self):
        from wechat_mcp.errors import BackendUnavailableError

        def boom():
            raise BackendUnavailableError("未找到桥接模块")

        adapter = DeepSeekGirlAdapter(bridge_factory=boom)
        status = await adapter.connect()
        self.assertFalse(status.ok)
        self.assertEqual(status.error["code"], "backend_unavailable")
        # 失败原因需在后续状态查询中保留，避免错误码丢失
        self.assertEqual(
            adapter.get_status().error["code"], "backend_unavailable"
        )

    async def test_connect_reports_connected_and_listening(self):
        status = await self.adapter.connect()
        self.assertTrue(status.ok)
        self.assertEqual(status.state, ConnectionState.CONNECTED)
        self.assertTrue(status.listening)
        self.assertTrue(status.window_visible)
        self.assertEqual(status.backend, "fake")
        self.assertTrue(self.bridge.listening)

    async def test_disconnect(self):
        await self.adapter.connect()
        status = await self.adapter.disconnect()
        self.assertFalse(status.ok)
        self.assertEqual(status.state, ConnectionState.DISCONNECTED)

    # ---------------------------------------------------------------- 发送

    async def test_send_requires_connection(self):
        result = await self.adapter.send_message("测试群", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "not_connected")
        self.assertEqual(self.bridge.sent, [])

    async def test_send_rejects_empty_recipient(self):
        await self.adapter.connect()
        result = await self.adapter.send_message("  ", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "target_invalid")
        self.assertEqual(self.bridge.sent, [])

    async def test_send_success(self):
        await self.adapter.connect()
        result = await self.adapter.send_message("测试群", "你好")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, SendStatus.SENT)
        self.assertEqual(self.bridge.sent, [("测试群", "你好")])

    async def test_send_failure_is_not_reported_as_success(self):
        await self.adapter.connect()
        self.bridge.fail_send = True
        result = await self.adapter.send_message("测试群", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, SendStatus.FAILED)
        self.assertEqual(result.error["code"], "send_failed")

    async def test_send_exception_is_mapped(self):
        await self.adapter.connect()
        self.bridge.raise_on_send = True
        result = await self.adapter.send_message("测试群", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "send_failed")

    async def test_send_timeout_is_widened(self):
        await self.adapter.connect()
        self.assertEqual(self.bridge.SEND_TIMEOUT_SECONDS, 20.0)

    async def test_send_attempts_are_applied_when_bridge_is_built(self):
        """连接前设好的重试次数，建桥时要套用（重连后也不会丢）。

        换算：重发次数 = 1 + 重试次数；补按回车次数 = 重试次数。
        """
        self.adapter.apply_send_attempts(4)
        await self.adapter.connect()
        self.assertEqual(self.bridge.FILE_SEND_ATTEMPTS, 5)
        self.assertEqual(self.bridge.FILE_SEND_ENTER_NUDGES, 4)

    async def test_send_attempts_are_applied_to_a_live_bridge(self):
        """已连接时改配置要立刻生效，0 表示不重试。"""
        await self.adapter.connect()
        self.adapter.apply_send_attempts(0)
        self.assertEqual(self.bridge.FILE_SEND_ATTEMPTS, 1)
        self.assertEqual(self.bridge.FILE_SEND_ENTER_NUDGES, 0)

    async def test_send_attempts_are_clamped(self):
        """非法值与超范围值都归一到 0..10。"""
        self.adapter.apply_send_attempts(-5)
        await self.adapter.connect()
        self.assertEqual(self.bridge.FILE_SEND_ATTEMPTS, 1)
        self.adapter.apply_send_attempts(99)
        self.assertEqual(self.bridge.FILE_SEND_ENTER_NUDGES, 10)
        self.adapter.apply_send_attempts("坏值")
        self.assertEqual(self.bridge.FILE_SEND_ENTER_NUDGES, 10, "非法值应被忽略")

    async def test_dry_run_does_not_send(self):
        await self.adapter.connect()
        result = await self.adapter.send_message("测试群", "你好", dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, SendStatus.DRY_RUN)
        self.assertTrue(result.dry_run)
        self.assertEqual(self.bridge.sent, [])

    async def test_ambiguous_target_refused(self):
        await self.adapter.connect()
        await self._feed(
            make_message("项目A组", "hi", message_id="1"),
            make_message("项目B组", "hi", message_id="2"),
        )
        result = await self.adapter.send_message("项目", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "target_ambiguous")
        self.assertEqual(result.error["detail"]["candidates"], ["项目A组", "项目B组"])
        self.assertEqual(self.bridge.sent, [])

    # ---------------------------------------------------------------- 读取

    async def test_chat_history_from_buffer(self):
        await self.adapter.connect()
        await self._feed(
            make_message("测试群", "第一条", message_id="1"),
            make_message("测试群", "第二条", message_id="2"),
            make_message("另一个群", "无关", message_id="3"),
        )
        history = self.adapter.get_chat_history("测试群", limit=1)
        self.assertTrue(history.ok)
        self.assertEqual(history.chat, "测试群")
        self.assertEqual([m.content for m in history.messages], ["第二条"])

    async def test_chat_history_rejects_empty_name(self):
        history = self.adapter.get_chat_history("   ")
        self.assertFalse(history.ok)

    async def test_chat_list_summarizes_observed_chats(self):
        await self.adapter.connect()
        await self._feed(
            make_message("测试群", "a", message_id="1"),
            make_message("测试群", "b", message_id="2"),
            make_message("私聊对象", "c", is_group=False, message_id="3"),
        )
        listing = self.adapter.get_chat_list()
        self.assertTrue(listing.ok)
        self.assertEqual(listing.total, 2)
        counts = {chat.name: chat.message_count for chat in listing.chats}
        self.assertEqual(counts, {"测试群": 2, "私聊对象": 1})

    async def test_chat_list_keyword_filter(self):
        await self.adapter.connect()
        await self._feed(
            make_message("测试群", "a", message_id="1"),
            make_message("工作群", "b", message_id="2"),
        )
        listing = self.adapter.get_chat_list(keyword="工作")
        self.assertEqual([chat.name for chat in listing.chats], ["工作群"])


class AdapterP1Tests(unittest.IsolatedAsyncioTestCase):
    """P1 能力：联系人搜索、聊天信息、增量读取、关注列表、文件发送。"""

    def setUp(self) -> None:
        self.bridge = FakeBridge()
        self.adapter = DeepSeekGirlAdapter(
            config=AdapterConfig(listen_on_connect=True),
            bridge_factory=lambda: self.bridge,
        )

    async def _feed(self, *messages: SimpleNamespace) -> None:
        for message in messages:
            await self.bridge.on_message(message)

    # ---------------------------------------------------------------- 联系人搜索

    async def test_search_contact_returns_candidates(self):
        await self.adapter.connect()
        self.bridge._wx._db.rows = [
            {"username": "wxid_a", "nick_name": "老王", "remark": ""},
            {"username": "wxid_b", "nick_name": "老王", "remark": "同事"},
            {"username": "12345@chatroom", "nick_name": "老王项目组", "remark": ""},
        ]
        result = self.adapter.search_contact("老王")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "wechat_db")
        self.assertEqual(result.total, 3)
        names = [c.name for c in result.candidates]
        self.assertEqual(names, ["老王", "同事", "老王项目组"])
        groups = [c.is_group for c in result.candidates]
        self.assertEqual(groups, [False, False, True])

    async def test_search_contact_requires_connection(self):
        result = self.adapter.search_contact("老王")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "not_connected")

    async def test_search_contact_rejects_empty_keyword(self):
        await self.adapter.connect()
        result = self.adapter.search_contact("   ")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "target_invalid")

    async def test_search_contact_falls_back_to_buffer(self):
        await self.adapter.connect()
        self.bridge._wx = SimpleNamespace(_db=None)
        await self._feed(make_message("项目讨论群", "hi", message_id="1"))
        result = self.adapter.search_contact("项目")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "buffer")
        self.assertEqual([c.name for c in result.candidates], ["项目讨论群"])

    # ---------------------------------------------------------------- 聊天信息

    async def test_get_chat_info_from_buffer(self):
        await self.adapter.connect()
        await self._feed(
            make_message("测试群", "a", message_id="1"),
            make_message("测试群", "b", message_id="2"),
        )
        info = self.adapter.get_chat_info("测试群")
        self.assertTrue(info.ok)
        self.assertTrue(info.is_group)
        self.assertEqual(info.message_count, 2)
        self.assertTrue(info.monitored)

    async def test_get_chat_info_rejects_empty(self):
        info = self.adapter.get_chat_info("  ")
        self.assertFalse(info.ok)
        self.assertEqual(info.error["code"], "target_invalid")

    # ---------------------------------------------------------------- 增量读取

    async def test_recent_messages_incremental_cursor(self):
        await self.adapter.connect()
        await self._feed(
            make_message("测试群", "第一条", message_id="1"),
            make_message("测试群", "第二条", message_id="2"),
        )
        first = self.adapter.get_recent_messages(after_seq=0)
        self.assertTrue(first.ok)
        self.assertEqual([m.content for m in first.messages], ["第一条", "第二条"])
        self.assertEqual(first.next_seq, 2)

        empty = self.adapter.get_recent_messages(after_seq=first.next_seq)
        self.assertEqual(empty.messages, [])
        self.assertEqual(empty.next_seq, 2)

        await self._feed(make_message("测试群", "第三条", message_id="3"))
        second = self.adapter.get_recent_messages(after_seq=first.next_seq)
        self.assertEqual([m.content for m in second.messages], ["第三条"])
        self.assertEqual(second.next_seq, 3)

    async def test_recent_messages_limit_and_chat_filter(self):
        await self.adapter.connect()
        await self._feed(
            make_message("群A", "a1", message_id="1"),
            make_message("群B", "b1", message_id="2"),
            make_message("群A", "a2", message_id="3"),
        )
        page = self.adapter.get_recent_messages(after_seq=0, limit=1)
        self.assertEqual([m.content for m in page.messages], ["a1"])
        self.assertFalse(page.complete)
        filtered = self.adapter.get_recent_messages(chat_name="群B")
        self.assertEqual([m.content for m in filtered.messages], ["b1"])

    # ---------------------------------------------------------------- 关注列表

    async def test_block_list_filters_reads_and_has_priority(self):
        await self.adapter.connect()
        await self._feed(
            make_message("工作群", "a", message_id="1"),
            make_message("闲聊群", "b", message_id="2"),
        )
        # 白名单只保留工作群，同时把工作群加入黑名单：黑名单优先
        self.adapter.set_monitored_chats(add=["工作群"], mode="allow")
        listing = self.adapter.get_chat_list()
        self.assertEqual([c.name for c in listing.chats], ["工作群"])

        self.adapter.set_monitored_chats(add=["工作群"], mode="block")
        listing = self.adapter.get_chat_list()
        # block 模式下白名单失效，仅工作群被忽略，闲聊群可见
        self.assertEqual([c.name for c in listing.chats], ["闲聊群"])

        history = self.adapter.get_chat_history("工作群")
        self.assertEqual(history.messages, [])
        self.assertIn("关注列表", history.note)

    async def test_empty_allow_list_means_all(self):
        await self.adapter.connect()
        await self._feed(make_message("任意群", "a", message_id="1"))
        listing = self.adapter.get_chat_list()
        self.assertEqual([c.name for c in listing.chats], ["任意群"])

    async def test_remove_and_invalid_mode(self):
        await self.adapter.connect()
        result = self.adapter.set_monitored_chats(add=["群A", "群B"])
        self.assertEqual(result.allow, ["群A", "群B"])
        result = self.adapter.set_monitored_chats(remove=["群A"])
        self.assertEqual(result.allow, ["群B"])
        bad = self.adapter.set_monitored_chats(mode="unknown")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error["code"], "target_invalid")

    # ---------------------------------------------------------------- 文件发送

    def _adapter_with_dir(self, directory: str, **kwargs) -> DeepSeekGirlAdapter:
        return DeepSeekGirlAdapter(
            config=AdapterConfig(
                listen_on_connect=True,
                allow_send_dirs=(Path(directory),),
                **kwargs,
            ),
            bridge_factory=lambda: self.bridge,
        )

    async def test_send_file_rejected_without_whitelist(self):
        await self.adapter.connect()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            result = await self.adapter.send_file("测试群", str(target))
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "path_not_allowed")
        self.assertEqual(self.bridge.sent_files, [])

    async def test_send_file_outside_whitelist_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter_with_dir(tmp)
            await adapter.connect()
            outside = Path(tmp).parent / "outside.txt"
            outside.write_text("hi", encoding="utf-8")
            try:
                result = await adapter.send_file("测试群", str(outside))
            finally:
                outside.unlink(missing_ok=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "path_not_allowed")

    async def test_send_file_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter_with_dir(tmp)
            await adapter.connect()
            result = await adapter.send_file("测试群", str(Path(tmp) / "no.txt"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "file_invalid")

    async def test_send_file_dry_run_does_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter_with_dir(tmp)
            await adapter.connect()
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            result = await adapter.send_file("测试群", str(target), dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, SendStatus.DRY_RUN)
        self.assertEqual(result.file_name, "a.txt")
        self.assertEqual(self.bridge.sent_files, [])

    async def test_send_file_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter_with_dir(tmp)
            await adapter.connect()
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            result = await adapter.send_file("测试群", str(target))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, SendStatus.SENT)
        self.assertEqual(len(self.bridge.sent_files), 1)
        self.assertEqual(self.bridge.sent_files[0][0], "测试群")

    async def test_send_file_failure_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter_with_dir(tmp)
            await adapter.connect()
            self.bridge.fail_send_file = True
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            result = await adapter.send_file("测试群", str(target))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, SendStatus.FAILED)

    # ---------------------------------------------------------------- 发送确认

    async def test_confirmation_required_blocks_send(self):
        adapter = DeepSeekGirlAdapter(
            config=AdapterConfig(
                listen_on_connect=True, require_confirmation=True
            ),
            bridge_factory=lambda: self.bridge,
        )
        await adapter.connect()
        result = await adapter.send_message("测试群", "你好")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, SendStatus.CONFIRMATION_REQUIRED)
        self.assertEqual(self.bridge.sent, [])

        confirmed = await adapter.send_message("测试群", "你好", confirm=True)
        self.assertTrue(confirmed.ok)
        self.assertEqual(self.bridge.sent, [("测试群", "你好")])


if __name__ == "__main__":
    unittest.main()
