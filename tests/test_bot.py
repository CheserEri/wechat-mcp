"""常驻自动回复 bot 的单元测试。"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from wechat_mcp.adapters.deepseekgirl import (
    DeepSeekGirlAdapter,
    _looks_like_chat_id,
)
from wechat_mcp.bot import BotConfig, BotEngine, get_persona, reset_persona
from wechat_mcp.bot.llm import LLMClient, LLMError
from wechat_mcp.schemas import (
    ConnectionState,
    MessageRecord,
    SendResult,
    SendStatus,
    StatusResult,
)


def make_record(
    chat="测试群",
    sender="张三",
    content="你好",
    is_group=True,
    is_at_me=False,
    reply_to_name="",
    message_id="",
    chat_id="",
) -> MessageRecord:
    return MessageRecord(
        chat=chat,
        sender=sender,
        content=content,
        is_group=is_group,
        is_at_me=is_at_me,
        reply_to_name=reply_to_name,
        message_id=message_id,
        chat_id=chat_id,
        timestamp=1000.0,
    )


class FakeAdapter:
    """仅实现 BotEngine 依赖的适配层接口。"""

    def __init__(self, names=None):
        self.listeners = []
        self.names = set(names or [])
        self.sent = []

    def add_message_listener(self, listener):
        self.listeners.append(listener)

    def remove_message_listener(self, listener):
        self.listeners.remove(listener)

    def self_names(self):
        return set(self.names)

    def get_status(self):
        return StatusResult(
            ok=True, state=ConnectionState.CONNECTED, backend="fake", listening=True
        )

    async def send_message(self, chat, text):
        self.sent.append((chat, text))
        return SendResult(ok=True, status=SendStatus.SENT, recipient=chat)

    async def dispatch(self, record):
        for listener in list(self.listeners):
            result = listener(record)
            if asyncio.iscoroutine(result):
                await result

    async def pump(self):
        await asyncio.sleep(0.05)


class FakeLLM:
    def __init__(self, config, reply="本鲸鱼娘收到啦。"):
        self.config = config
        self.reply = reply
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        self.last_messages = messages
        return self.reply


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.json"
            cfg = BotConfig(enabled=True, model="x", groups=["群A", "群B"])
            cfg.save(path)
            loaded = BotConfig.load(path)
            self.assertTrue(loaded.enabled)
            self.assertEqual(loaded.model, "x")
            self.assertEqual(loaded.groups, ["群A", "群B"])

    def test_missing_file(self):
        cfg = BotConfig.load(Path("X:/no-such-file.json"))
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.model, "deepseek-chat")

    def test_from_dict_tolerant(self):
        cfg = BotConfig.from_dict(
            {"unknown": 1, "context_messages": -5, "groups": ["群", "   "]}
        )
        self.assertEqual(cfg.context_messages, 1)  # 最小值纠正
        self.assertEqual(cfg.groups, ["群"])       # 空白项被过滤

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsInstance(BotConfig.load(path), BotConfig)


class PersonaTests(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(get_persona(BotConfig()), "")

    def test_custom(self):
        cfg = BotConfig(persona_custom="你是一只猫。")
        self.assertEqual(get_persona(cfg), "你是一只猫。")

    def test_blank_custom_falls_back_to_empty(self):
        self.assertEqual(get_persona(BotConfig(persona_custom="   ")), "")

    def test_reset(self):
        cfg = BotConfig(persona_custom="X")
        reset_persona(cfg)
        self.assertEqual(cfg.persona_custom, "")
        self.assertEqual(get_persona(cfg), "")


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter(names={"小深"})
        self.engine = BotEngine(self.adapter, BotConfig())

    def test_at_me(self):
        self.assertTrue(self.engine._should_reply(make_record(is_at_me=True)))

    def test_reply_to_me(self):
        self.assertTrue(
            self.engine._should_reply(make_record(reply_to_name="小深"))
        )

    def test_reply_to_someone_else(self):
        self.assertFalse(
            self.engine._should_reply(make_record(reply_to_name="李四"))
        )

    def test_plain_group_message(self):
        self.assertFalse(self.engine._should_reply(make_record()))

    def test_private_follows_switch(self):
        rec = make_record(is_group=False, sender="朋友", chat="朋友")
        self.assertTrue(self.engine._should_reply(rec))
        self.engine.config.reply_private = False
        self.assertFalse(self.engine._should_reply(rec))

    def test_group_scope(self):
        cfg = BotConfig(all_groups=False, groups=["其他群"])
        engine = BotEngine(self.adapter, cfg)
        self.assertFalse(engine._should_reply(make_record(is_at_me=True)))
        cfg.groups = ["测试群"]
        self.assertTrue(engine._should_reply(make_record(is_at_me=True)))

    def test_trigger_switches(self):
        cfg = BotConfig(trigger_at=False, trigger_reply=False)
        engine = BotEngine(self.adapter, cfg)
        self.assertFalse(engine._should_reply(make_record(is_at_me=True)))
        self.assertFalse(
            engine._should_reply(make_record(reply_to_name="小深"))
        )

    def test_reply_target_unknown_self_name(self):
        adapter = FakeAdapter(names=set())
        engine = BotEngine(adapter, BotConfig())
        self.assertTrue(
            engine._should_reply(make_record(reply_to_name="任意昵称"))
        )

    def test_continuation_after_reply(self):
        engine = BotEngine(self.adapter, BotConfig(continuation_seconds=120.0))
        engine._last_reply_to["测试群"] = ("张三", time.time())
        # 刚被回复过的张三继续发言 → 接着回
        self.assertTrue(
            engine._should_reply(make_record(sender="张三", content="补充一句"))
        )
        # 没被回复过的李四发言 → 不回
        self.assertFalse(
            engine._should_reply(make_record(sender="李四", content="我也说"))
        )

    def test_continuation_disabled(self):
        engine = BotEngine(self.adapter, BotConfig(continuation_seconds=0.0))
        engine._last_reply_to["测试群"] = ("张三", time.time())
        self.assertFalse(engine._should_reply(make_record(sender="张三")))

    def test_continuation_expired(self):
        engine = BotEngine(self.adapter, BotConfig(continuation_seconds=30.0))
        engine._last_reply_to["测试群"] = ("张三", time.time() - 60)
        self.assertFalse(engine._should_reply(make_record(sender="张三")))


class MessageBuildTests(unittest.TestCase):
    def test_group_prefix_and_assistant(self):
        adapter = FakeAdapter()
        engine = BotEngine(
            adapter, BotConfig(context_messages=10, persona_custom="人设")
        )
        r1 = make_record(content="第一句")
        engine._record_inbound(r1)
        engine._record_assistant("测试群", "回复一")
        r2 = make_record(content="第二句")
        engine._record_inbound(r2)
        messages = engine._build_messages("测试群")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("张三: 第一句", messages[1]["content"])
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "回复一")
        # 最后一条（触发消息）带「待回复」标记
        self.assertEqual(messages[3]["content"], "[待回复] 张三: 第二句")

    def test_private_no_prefix(self):
        adapter = FakeAdapter()
        engine = BotEngine(adapter, BotConfig(persona_custom="人设"))
        engine._record_inbound(
            make_record(is_group=False, chat="朋友", sender="朋友")
        )
        messages = engine._build_messages("朋友")
        self.assertEqual(messages[1]["content"], "[待回复] 你好")

    def test_context_limit(self):
        adapter = FakeAdapter()
        engine = BotEngine(
            adapter, BotConfig(context_messages=2, persona_custom="人设")
        )
        for i in range(5):
            engine._record_inbound(make_record(content=f"m{i}"))
        messages = engine._build_messages("测试群")
        # system + 仅 2 条历史
        self.assertEqual(len(messages), 3)
        self.assertIn("m3", messages[1]["content"])
        self.assertIn("m4", messages[2]["content"])

    def test_default_persona_not_injected(self):
        engine = BotEngine(FakeAdapter(), BotConfig())
        engine._record_inbound(make_record())
        messages = engine._build_messages("测试群")
        # 仅有「行为约束」这一条 system，不含任何角色人设
        self.assertEqual(len(messages), 2)
        self.assertNotIn("鲸鱼娘", messages[0]["content"])
        self.assertIn("待回复", messages[0]["content"])
        self.assertIn("纯文本", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "[待回复] 张三: 你好")

    def test_custom_persona_appended_to_system(self):
        engine = BotEngine(FakeAdapter(), BotConfig(persona_custom="你是一只猫。"))
        engine._record_inbound(make_record())
        messages = engine._build_messages("测试群")
        self.assertIn("待回复", messages[0]["content"])
        self.assertIn("你是一只猫。", messages[0]["content"])


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key(self):
        with self.assertRaises(LLMError) as ctx:
            await LLMClient(BotConfig(api_key="")).chat(
                [{"role": "user", "content": "hi"}]
            )
        self.assertEqual(ctx.exception.code, "api_key_missing")

    async def test_success(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "好的主人"}}]},
            )
        )
        client = LLMClient(BotConfig(api_key="sk-x"), transport=transport)
        self.assertEqual(
            await client.chat([{"role": "user", "content": "hi"}]), "好的主人"
        )

    async def test_unauthorized(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
        client = LLMClient(BotConfig(api_key="bad"), transport=transport)
        with self.assertRaises(LLMError) as ctx:
            await client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, "unauthorized")

    async def test_empty_reply(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": ""}}]}
            )
        )
        client = LLMClient(BotConfig(api_key="sk-x"), transport=transport)
        with self.assertRaises(LLMError) as ctx:
            await client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, "empty_response")


class EngineFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end(self):
        adapter = FakeAdapter()
        cfg = BotConfig(
            enabled=True, cooldown_seconds=0.0, persona_custom="人设"
        )
        fake_llm = FakeLLM(cfg)
        engine = BotEngine(
            adapter, cfg, llm_factory=lambda c: fake_llm
        )
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(adapter.sent, [("测试群", "本鲸鱼娘收到啦。")])
        self.assertEqual(engine.reply_count_today, 1)
        self.assertIsNotNone(engine.last_trigger_at)
        # 发给模型的消息含 system（人设）
        self.assertEqual(fake_llm.last_messages[0]["role"], "system")
        await engine.stop()

    async def test_continuation_replies_without_at(self):
        adapter = FakeAdapter()
        cfg = BotConfig(enabled=True, cooldown_seconds=0.0, persona_custom="人设")
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: FakeLLM(c))
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(engine._last_reply_to.get("测试群", ("", 0))[0], "张三")
        # 张三随后没有 @ 的补充发言也应被接着回复
        adapter.sent.clear()
        await adapter.dispatch(
            make_record(sender="张三", content="再补一句", message_id="m2")
        )
        await adapter.pump()
        self.assertEqual(len(adapter.sent), 1)
        await engine.stop()

    async def test_cooldown_blocks_second(self):
        adapter = FakeAdapter()
        cfg = BotConfig(enabled=True, cooldown_seconds=60.0)
        engine = BotEngine(
            adapter, cfg, llm_factory=lambda c: FakeLLM(c)
        )
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m2"))
        await adapter.pump()
        self.assertEqual(len(adapter.sent), 1)
        await engine.stop()

    async def test_untriggered_no_reply(self):
        adapter = FakeAdapter()
        cfg = BotConfig(enabled=True, persona_custom="人设")
        engine = BotEngine(
            adapter, cfg, llm_factory=lambda c: FakeLLM(c)
        )
        await engine.start()
        await adapter.dispatch(make_record())  # 普通群消息
        await adapter.pump()
        self.assertEqual(adapter.sent, [])
        # 但上下文已记录，触发时可见
        messages = engine._build_messages("测试群")
        self.assertEqual(len(messages), 2)  # system + 1 条
        await engine.stop()

    async def test_llm_failure_no_send(self):
        class BrokenLLM:
            def __init__(self, config): pass

            async def chat(self, messages):
                raise LLMError("unauthorized", "bad key")

        adapter = FakeAdapter()
        engine = BotEngine(
            adapter, BotConfig(enabled=True), llm_factory=BrokenLLM
        )
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(engine.reply_count_today, 0)
        self.assertTrue(
            any("bad key" in line["message"] for line in engine.get_logs())
        )
        await engine.stop()


class _FakeDB:
    """按 username 精确返回 contact.db 行的最小替身。"""

    def __init__(self, rows=None):
        self.rows = rows or []

    def search_contact(self, keyword):
        return [r for r in self.rows if keyword in (r.get("username") or "")]


class _FakeBridge:
    def __init__(self, db=None, connected=True):
        self.is_connected = connected
        self._wx = SimpleNamespace(_db=db)
        self.sent = []

    async def send_text(self, room_name, content):
        self.sent.append((room_name, content))
        return True


ROOM_ID = "43081897466@chatroom"


class ChatIdentityTests(unittest.TestCase):
    """会话识别改为「ID 为稳定标识、发送前反查显示名」。"""

    def test_looks_like_chat_id(self):
        self.assertTrue(_looks_like_chat_id(ROOM_ID))
        self.assertTrue(_looks_like_chat_id("wxid_abc123"))
        self.assertFalse(_looks_like_chat_id("玩什么游戏？！都给劳资打米！！"))

    def test_non_id_passthrough(self):
        adapter = DeepSeekGirlAdapter()
        self.assertEqual(
            adapter.resolve_chat_identifier("玩什么游戏？"), "玩什么游戏？"
        )

    def test_id_without_db_unresolved(self):
        adapter = DeepSeekGirlAdapter()
        self.assertEqual(adapter.resolve_chat_identifier(ROOM_ID), "")

    def test_id_resolved_from_contact_db(self):
        adapter = DeepSeekGirlAdapter()
        adapter._bridge = _FakeBridge(
            db=_FakeDB(
                [
                    {
                        "username": ROOM_ID,
                        "nick_name": "玩什么游戏？！都给劳资打米！！",
                        "remark": "",
                    }
                ]
            )
        )
        self.assertEqual(
            adapter.resolve_chat_identifier(ROOM_ID),
            "玩什么游戏？！都给劳资打米！！",
        )

    def test_id_not_found_unresolved(self):
        adapter = DeepSeekGirlAdapter()
        # 命中的是别的会话（模糊匹配），不是该 ID 本身 → 拒绝
        adapter._bridge = _FakeBridge(
            db=_FakeDB([{"username": "other@chatroom", "nick_name": "别的群"}])
        )
        self.assertEqual(adapter.resolve_chat_identifier(ROOM_ID), "")

    def test_cached_name_used_when_db_unavailable(self):
        # DB 快照偶发不可用（锁/WAL），用曾成功解析过的名字兜底
        adapter = DeepSeekGirlAdapter()
        adapter._chat_id_names[ROOM_ID] = "玩什么游戏？"
        self.assertEqual(adapter.resolve_chat_identifier(ROOM_ID), "玩什么游戏？")

    def test_to_record_carries_chat_id(self):
        msg = SimpleNamespace(
            room_id=ROOM_ID,
            room_name="玩什么游戏？！都给劳资打米！！",
            sender_name="张三",
            content="你好",
        )
        record = DeepSeekGirlAdapter._to_record(msg)
        self.assertEqual(record.chat, "玩什么游戏？！都给劳资打米！！")
        self.assertEqual(record.chat_id, ROOM_ID)

    def test_to_record_degrades_to_id(self):
        # 上游解析失败：显示名缺失时 chat 退化为 ID，但 chat_id 仍准确
        msg = SimpleNamespace(
            room_id=ROOM_ID,
            room_name="",
            sender_name="张三",
            content="你好",
        )
        record = DeepSeekGirlAdapter._to_record(msg)
        self.assertEqual(record.chat, ROOM_ID)
        self.assertEqual(record.chat_id, ROOM_ID)


class SendTargetTests(unittest.IsolatedAsyncioTestCase):
    """发送目标为会话 ID 时：能反查就发显示名，查不到就明确失败。"""

    async def test_unresolved_id_rejected(self):
        adapter = DeepSeekGirlAdapter()
        adapter._bridge = _FakeBridge(db=_FakeDB([]))
        result = await adapter.send_message(ROOM_ID, "hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "chat_id_unresolved")
        self.assertEqual(adapter._bridge.sent, [])

    async def test_id_resolved_before_send(self):
        adapter = DeepSeekGirlAdapter()
        adapter._bridge = _FakeBridge(
            db=_FakeDB(
                [{"username": ROOM_ID, "nick_name": "玩什么游戏？", "remark": ""}]
            )
        )
        result = await adapter.send_message(ROOM_ID, "hi")
        self.assertTrue(result.ok)
        self.assertEqual(adapter._bridge.sent, [("玩什么游戏？", "hi")])

    async def test_on_message_caches_resolved_name(self):
        adapter = DeepSeekGirlAdapter()
        msg = SimpleNamespace(
            room_id=ROOM_ID,
            room_name="玩什么游戏？",
            sender_name="张三",
            content="你好",
        )
        await adapter._on_message(msg)
        self.assertEqual(adapter.resolve_chat_identifier(ROOM_ID), "玩什么游戏？")


class ChatIdScopeTests(unittest.TestCase):
    """群范围过滤同时接受群名与会话 ID；历史分桶以 ID 为准。"""

    def test_scope_accepts_chat_id(self):
        engine = BotEngine(
            FakeAdapter(), BotConfig(all_groups=False, groups=[ROOM_ID])
        )
        record = make_record(is_at_me=True, chat=ROOM_ID, chat_id=ROOM_ID)
        self.assertTrue(engine._should_reply(record))

    def test_history_bucket_keyed_by_chat_id(self):
        engine = BotEngine(FakeAdapter(), BotConfig(persona_custom="人设"))
        engine._record_inbound(make_record(chat=ROOM_ID, chat_id=ROOM_ID))
        self.assertEqual(len(engine._build_messages(ROOM_ID)), 2)  # system + 1


class QuoteParserTests(unittest.TestCase):
    """验证入库 wechat_bridge.py 的引用回复目标解析。"""

    @staticmethod
    def _parser():
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        module_file = root / "packaging" / "wechat_bridge.py"
        spec = importlib.util.spec_from_file_location(
            "packaged_wechat_bridge", module_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.WeChatBridge._extract_reply_target

    def test_parse_displayname(self):
        raw = (
            "<msg><appmsg><refermsg><type>1</type>"
            "<displayname>小深</displayname><content>在吗</content>"
            "</refermsg></appmsg></msg>"
        )
        self.assertEqual(self._parser()(raw), "小深")

    def test_no_refermsg(self):
        self.assertEqual(self._parser()("普通文本"), "")
        self.assertEqual(self._parser()(""), "")


if __name__ == "__main__":
    unittest.main()
