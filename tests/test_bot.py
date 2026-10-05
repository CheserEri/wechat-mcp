"""常驻自动回复 bot 的单元测试。"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from wechat_mcp.adapters.deepseekgirl import (
    DeepSeekGirlAdapter,
    _looks_like_chat_id,
)
from wechat_mcp.bot import BotConfig, BotEngine, get_persona, reset_persona
from wechat_mcp.bot import links as links_mod
from wechat_mcp.bot.links import LinkInfo, extract_urls, format_link_block
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
    image_path="",
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
        image_path=image_path,
        timestamp=1000.0,
    )


class FakeAdapter:
    """仅实现 BotEngine 依赖的适配层接口。"""

    def __init__(self, names=None):
        self.listeners = []
        self.names = set(names or [])
        self.sent = []
        self.files = []
        # 适配层配置（None 表示未提供，链接下载的白名单注入会跳过）。
        self.config = None

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

    async def send_file(self, recipient, file_path, dry_run=False, confirm=False):
        self.files.append((recipient, file_path))
        return SimpleNamespace(ok=True, status=SendStatus.SENT, error=None)

    async def dispatch(self, record):
        for listener in list(self.listeners):
            result = listener(record)
            if asyncio.iscoroutine(result):
                await result

    async def pump(self):
        # 等待消费循环处理完队列；留足余量避免调度抖动导致的偶发失败。
        await asyncio.sleep(0.1)


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
            enabled=True,
            cooldown_seconds=0.0,
            persona_custom="人设",
            reply_probability=1.0,
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
        cfg = BotConfig(
            enabled=True,
            cooldown_seconds=0.0,
            persona_custom="人设",
            reply_probability=1.0,
        )
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
        cfg = BotConfig(enabled=True, cooldown_seconds=60.0, reply_probability=1.0)
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
        cfg = BotConfig(enabled=True, persona_custom="人设", reply_probability=1.0)
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
            adapter, BotConfig(enabled=True, reply_probability=1.0), llm_factory=BrokenLLM
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


class HumanizeConfigTests(unittest.TestCase):
    """拟人化配置的默认值与范围纠正。"""

    def test_defaults_on(self):
        cfg = BotConfig()
        self.assertTrue(cfg.humanize)
        self.assertTrue(cfg.split_replies)
        self.assertEqual(cfg.split_max_parts, 3)

    def test_probability_clamped(self):
        self.assertEqual(
            BotConfig.from_dict({"reply_probability": 5}).reply_probability, 1.0
        )
        self.assertEqual(
            BotConfig.from_dict({"reply_probability": -1}).reply_probability, 0.0
        )

    def test_split_bounds(self):
        cfg = BotConfig.from_dict(
            {"split_max_parts": 0, "split_delay_min": 2, "split_delay_max": 0}
        )
        self.assertEqual(cfg.split_max_parts, 1)
        self.assertEqual(cfg.split_delay_min, 2.0)
        self.assertEqual(cfg.split_delay_max, 2.0)  # 上限不低于下限

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.json"
            BotConfig(humanize=False, reply_probability=0.5).save(path)
            loaded = BotConfig.load(path)
            self.assertFalse(loaded.humanize)
            self.assertEqual(loaded.reply_probability, 0.5)


class SplitReplyTests(unittest.TestCase):
    """分条发送：只切分不丢内容，且受条数上限约束。"""

    @staticmethod
    def _engine(**kwargs) -> BotEngine:
        return BotEngine(FakeAdapter(), BotConfig(**kwargs))

    def test_empty(self):
        self.assertEqual(self._engine()._split_reply(""), [])

    def test_short_reply_single(self):
        self.assertEqual(self._engine()._split_reply("好的"), ["好的"])

    def test_disabled_returns_single(self):
        engine = self._engine(humanize=False)
        long = "第一句。第二句。第三句。第四句。第五句。"
        self.assertEqual(engine._split_reply(long), [long])

    def test_split_long_reply_preserves_content(self):
        engine = self._engine(split_min_length=10, split_max_parts=3)
        reply = "你好呀。今天天气不错！要不要一起出去走走？我这边正好有空。"
        parts = engine._split_reply(reply)
        self.assertGreater(len(parts), 1)
        self.assertLessEqual(len(parts), 3)
        self.assertEqual("".join(parts), reply)
        self.assertTrue(all(p.strip() for p in parts))

    def test_split_respects_max_parts(self):
        engine = self._engine(split_min_length=5, split_max_parts=2)
        reply = "一。二。三。四。五。六。"
        parts = engine._split_reply(reply)
        self.assertEqual(len(parts), 2)
        self.assertEqual("".join(parts), reply)

    def test_no_sentence_boundary_kept_whole(self):
        engine = self._engine(split_min_length=5, split_max_parts=3)
        reply = "a" * 80  # 无句末标点，无法自然拆分
        self.assertEqual(engine._split_reply(reply), [reply])


class HumanizeFlowTests(unittest.IsolatedAsyncioTestCase):
    """拟人化在完整链路中的行为：概率、分条、总开关。"""

    LONG_REPLY = "你好呀。今天天气不错！要不要一起出去走走？我这边正好有空。"

    async def test_probability_zero_never_replies(self):
        adapter = FakeAdapter()
        cfg = BotConfig(enabled=True, cooldown_seconds=0.0, reply_probability=0.0)
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: FakeLLM(c))
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(engine.reply_count_today, 0)
        await engine.stop()

    async def test_split_sends_multiple_messages(self):
        adapter = FakeAdapter()
        cfg = BotConfig(
            enabled=True,
            cooldown_seconds=0.0,
            reply_probability=1.0,
            split_min_length=10,
            split_max_parts=3,
            split_delay_min=0.0,
            split_delay_max=0.0,
        )
        engine = BotEngine(
            adapter, cfg, llm_factory=lambda c: FakeLLM(c, reply=self.LONG_REPLY)
        )
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertGreater(len(adapter.sent), 1)
        self.assertEqual(
            "".join(text for _, text in adapter.sent), self.LONG_REPLY
        )
        self.assertEqual(engine.reply_count_today, 1)
        await engine.stop()

    async def test_humanize_off_single_message(self):
        adapter = FakeAdapter()
        cfg = BotConfig(enabled=True, cooldown_seconds=0.0, humanize=False)
        engine = BotEngine(
            adapter, cfg, llm_factory=lambda c: FakeLLM(c, reply=self.LONG_REPLY)
        )
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(adapter.sent, [("测试群", self.LONG_REPLY)])
        await engine.stop()


class QuietHoursTests(unittest.TestCase):
    """静默时段判定（含跨零点）。"""

    @staticmethod
    def _engine(**kwargs) -> BotEngine:
        return BotEngine(FakeAdapter(), BotConfig(**kwargs))

    @staticmethod
    def _at(hour: int, minute: int) -> float:
        return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))

    def test_disabled(self):
        self.assertFalse(self._engine()._in_quiet_hours(self._at(3, 0)))

    def test_same_start_end_disabled(self):
        engine = self._engine(
            quiet_hours_enabled=True,
            quiet_hours_start="08:00",
            quiet_hours_end="08:00",
        )
        self.assertFalse(engine._in_quiet_hours(self._at(8, 0)))

    def test_invalid_format_ignored(self):
        engine = self._engine(
            quiet_hours_enabled=True,
            quiet_hours_start="25:00",
            quiet_hours_end="08:00",
        )
        self.assertFalse(engine._in_quiet_hours(self._at(3, 0)))

    def test_normal_range(self):
        engine = self._engine(
            quiet_hours_enabled=True,
            quiet_hours_start="09:00",
            quiet_hours_end="17:00",
        )
        self.assertTrue(engine._in_quiet_hours(self._at(10, 0)))
        self.assertFalse(engine._in_quiet_hours(self._at(18, 0)))
        self.assertFalse(engine._in_quiet_hours(self._at(8, 59)))

    def test_wraps_midnight(self):
        engine = self._engine(
            quiet_hours_enabled=True,
            quiet_hours_start="23:00",
            quiet_hours_end="08:00",
        )
        self.assertTrue(engine._in_quiet_hours(self._at(23, 30)))
        self.assertTrue(engine._in_quiet_hours(self._at(2, 0)))
        self.assertFalse(engine._in_quiet_hours(self._at(12, 0)))


class GroupRateLimitTests(unittest.TestCase):
    """群级节流：窗口内超过上限即静默。"""

    @staticmethod
    def _engine(**kwargs) -> BotEngine:
        return BotEngine(FakeAdapter(), BotConfig(**kwargs))

    def test_disabled(self):
        engine = self._engine(group_rate_enabled=False)
        engine._note_reply("群", now=1000.0)
        self.assertFalse(engine._rate_limited("群", 1001.0))

    def test_under_then_at_limit(self):
        engine = self._engine(
            group_rate_enabled=True,
            group_rate_window_minutes=30,
            group_rate_max_replies=2,
        )
        engine._note_reply("群", now=1000.0)
        self.assertFalse(engine._rate_limited("群", 1001.0))
        engine._note_reply("群", now=1002.0)
        self.assertTrue(engine._rate_limited("群", 1003.0))

    def test_window_expiry(self):
        engine = self._engine(
            group_rate_enabled=True,
            group_rate_window_minutes=1,
            group_rate_max_replies=1,
        )
        engine._note_reply("群", now=1000.0)
        self.assertTrue(engine._rate_limited("群", 1030.0))
        self.assertFalse(engine._rate_limited("群", 1061.0))

    def test_zero_limit_unlimited(self):
        engine = self._engine(
            group_rate_enabled=True,
            group_rate_window_minutes=30,
            group_rate_max_replies=0,
        )
        engine._note_reply("群", now=1000.0)
        self.assertFalse(engine._rate_limited("群", 1001.0))


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    """偶发参与：候选筛选、概率、[SILENT] 哨兵与冷却。"""

    @staticmethod
    def _recent_group(**kwargs):
        record = make_record(
            is_group=True, chat="水群", sender="张三", content="今天好热", **kwargs
        )
        record.timestamp = time.time()
        return record

    def _make(self, adapter, llm, **cfg_kwargs) -> BotEngine:
        options = dict(
            enabled=True,
            humanize=False,
            continuation_seconds=0.0,
            group_rate_enabled=False,
            proactive_enabled=True,
            proactive_probability=1.0,
            proactive_recent_seconds=600.0,
            proactive_per_group_cooldown=0.0,
        )
        options.update(cfg_kwargs)
        return BotEngine(adapter, BotConfig(**options), llm_factory=lambda c: llm)

    def test_default_off(self):
        self.assertFalse(BotConfig().proactive_enabled)

    async def test_no_candidates_no_send(self):
        adapter = FakeAdapter()
        engine = self._make(adapter, FakeLLM(BotConfig()))
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [])

    async def test_speaks_when_eligible(self):
        adapter = FakeAdapter()
        engine = self._make(adapter, FakeLLM(BotConfig(), reply="哈哈确实"))
        engine._record_inbound(self._recent_group())
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [("水群", "哈哈确实")])
        self.assertEqual(engine.reply_count_today, 1)

    async def test_silent_sentinel_skips_send(self):
        adapter = FakeAdapter()
        engine = self._make(adapter, FakeLLM(BotConfig(), reply="[SILENT]"))
        engine._record_inbound(self._recent_group())
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [])
        self.assertEqual(engine.reply_count_today, 0)

    async def test_probability_zero_never_speaks(self):
        adapter = FakeAdapter()
        engine = self._make(
            adapter, FakeLLM(BotConfig(), reply="在的"), proactive_probability=0.0
        )
        engine._record_inbound(self._recent_group())
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [])

    async def test_bot_spoke_last_skips(self):
        adapter = FakeAdapter()
        engine = self._make(adapter, FakeLLM(BotConfig(), reply="在的"))
        engine._record_inbound(self._recent_group())
        engine._record_assistant("水群", "我先说一句")
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [])

    async def test_per_group_cooldown_blocks(self):
        adapter = FakeAdapter()
        engine = self._make(
            adapter,
            FakeLLM(BotConfig(), reply="在的"),
            proactive_per_group_cooldown=3600.0,
        )
        engine._record_inbound(self._recent_group())
        await engine._proactive_tick()
        self.assertEqual(len(adapter.sent), 1)
        await engine._proactive_tick()
        self.assertEqual(len(adapter.sent), 1)

    async def test_stale_group_skipped(self):
        adapter = FakeAdapter()
        engine = self._make(
            adapter, FakeLLM(BotConfig(), reply="在的"), proactive_recent_seconds=10.0
        )
        record = self._recent_group()
        record.timestamp = time.time() - 3600
        engine._record_inbound(record)
        await engine._proactive_tick()
        self.assertEqual(adapter.sent, [])


class SilenceFlowTests(unittest.IsolatedAsyncioTestCase):
    """静默时段 / 群级节流在完整链路中阻断回复。"""

    async def test_quiet_hours_blocks_reply(self):
        adapter = FakeAdapter()
        now = time.time()
        start = time.strftime("%H:%M", time.localtime(now - 3600))
        end = time.strftime("%H:%M", time.localtime(now + 3600))
        cfg = BotConfig(
            enabled=True,
            cooldown_seconds=0.0,
            reply_probability=1.0,
            quiet_hours_enabled=True,
            quiet_hours_start=start,
            quiet_hours_end=end,
        )
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: FakeLLM(c))
        await engine.start()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1"))
        await adapter.pump()
        self.assertEqual(adapter.sent, [])
        await engine.stop()

    async def test_group_rate_limit_blocks_after_max(self):
        adapter = FakeAdapter()
        cfg = BotConfig(
            enabled=True,
            cooldown_seconds=0.0,
            reply_probability=1.0,
            continuation_seconds=0.0,
            group_rate_enabled=True,
            group_rate_window_minutes=30.0,
            group_rate_max_replies=1,
        )
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: FakeLLM(c))
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, sender="张三", message_id="m1")
        )
        await adapter.pump()
        await adapter.dispatch(
            make_record(is_at_me=True, sender="李四", message_id="m2")
        )
        await adapter.pump()
        self.assertEqual(len(adapter.sent), 1)
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


class VisionTests(unittest.TestCase):
    """图像识别管道：开关、多模态上下文与图片数量上限。"""

    @staticmethod
    def _png(tmp: str, name: str = "a.png") -> str:
        path = Path(tmp) / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        return str(path)

    def test_disabled_keeps_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BotEngine(FakeAdapter(), BotConfig(vision_enabled=False))
            engine._record_inbound(
                make_record(content="看这个", image_path=self._png(tmp))
            )
            messages = engine._build_messages("测试群")
            self.assertIsInstance(messages[1]["content"], str)

    def test_enabled_attaches_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BotEngine(
                FakeAdapter(), BotConfig(vision_enabled=True, vision_max_images=3)
            )
            engine._record_inbound(
                make_record(content="看这个", image_path=self._png(tmp))
            )
            content = engine._build_messages("测试群")[1]["content"]
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]["type"], "text")
            self.assertTrue(
                content[1]["image_url"]["url"].startswith("data:image/png;base64,")
            )

    def test_max_images_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BotEngine(
                FakeAdapter(), BotConfig(vision_enabled=True, vision_max_images=1)
            )
            for index in range(3):
                engine._record_inbound(
                    make_record(
                        content=f"图{index}",
                        image_path=self._png(tmp, f"{index}.png"),
                    )
                )
            messages = engine._build_messages("测试群")
            attached = [m for m in messages if isinstance(m["content"], list)]
            self.assertEqual(len(attached), 1)

    def test_missing_file_falls_back_to_text(self):
        engine = BotEngine(FakeAdapter(), BotConfig(vision_enabled=True))
        engine._record_inbound(make_record(content="图", image_path="X:/no.png"))
        self.assertIsInstance(engine._build_messages("测试群")[1]["content"], str)

    def test_unsupported_extension_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.tiff"
            path.write_bytes(b"x")
            engine = BotEngine(FakeAdapter(), BotConfig(vision_enabled=True))
            engine._record_inbound(make_record(content="图", image_path=str(path)))
            self.assertIsInstance(engine._build_messages("测试群")[1]["content"], str)

    def test_proactive_context_also_carries_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BotEngine(
                FakeAdapter(), BotConfig(vision_enabled=True, vision_max_images=1)
            )
            engine._record_inbound(
                make_record(content="看这个", image_path=self._png(tmp))
            )
            messages = engine._build_proactive_messages("测试群")
            self.assertIsInstance(messages[1]["content"], list)


class BridgeNormalizeTests(unittest.TestCase):
    """入库 wechat_bridge.py 的消息归一化（含 XML 声明前缀）。"""

    @staticmethod
    def _module():
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        module_file = root / "packaging" / "wechat_bridge.py"
        spec = importlib.util.spec_from_file_location(
            "packaged_wechat_bridge_norm", module_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_xml_declaration_image_compacted(self):
        normalize = self._module().WeChatBridge.normalize_message_content
        raw = '<?xml version="1.0"?>\n<msg>\n\t<img aeskey="x"/>\n</msg>'
        self.assertEqual(normalize(raw, sender="张三"), "[图片]")

    def test_xml_declaration_emoji_compacted(self):
        normalize = self._module().WeChatBridge.normalize_message_content
        raw = '<?xml version="1.0"?><msg><emoji md5="x"/></msg>'
        self.assertEqual(normalize(raw), "[表情]")

    def test_plain_text_unchanged(self):
        normalize = self._module().WeChatBridge.normalize_message_content
        self.assertEqual(normalize("你好"), "你好")

    def test_message_carries_image_path(self):
        module = self._module()
        self.assertIn("image_path", module.WeChatMessage.__dataclass_fields__)

    def test_link_card_keeps_url(self):
        """链接卡片原先只留标题、丢掉 <url>，下游无法解析链接。"""
        normalize = self._module().WeChatBridge.normalize_message_content
        raw = (
            '<?xml version="1.0"?>\n<msg><appmsg><title>某某视频</title>'
            "<url>https://example.com/v</url></appmsg></msg>"
        )
        out = normalize(raw)
        self.assertIn("某某视频", out)
        self.assertIn("https://example.com/v", out)

    def test_link_card_without_url_still_title_only(self):
        normalize = self._module().WeChatBridge.normalize_message_content
        raw = "<msg><appmsg><title>只有标题</title></appmsg></msg>"
        self.assertEqual(normalize(raw), "只有标题")


class BridgeMediaContextTests(unittest.TestCase):
    """媒体提取上下文解析。

    回归点：桥接层走全局监听（``AddListenAll``），其 chat 占位
    ``_AllMessageChat`` 只有 ``who``/``_wxid``、没有 ``_db``，早期实现因此
    永远拿不到 db，图片/语音提取静默失败。现在必须回退到 ``self._wx._db``。
    """

    @staticmethod
    def _module():
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        module_file = root / "packaging" / "wechat_bridge.py"
        spec = importlib.util.spec_from_file_location(
            "packaged_wechat_bridge_media", module_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _bridge(module, wx):
        bridge = object.__new__(module.WeChatBridge)
        bridge._backend = "wechatauto"
        bridge._wx = wx
        return bridge

    def test_falls_back_to_bridge_db_when_chat_has_none(self):
        import types

        module = self._module()
        db = object()
        bridge = self._bridge(module, types.SimpleNamespace(_db=db))
        # 模拟 _AllMessageChat：只有 _wxid，没有 _db
        root = types.SimpleNamespace(_wxid="123@chatroom")
        msg = types.SimpleNamespace(
            local_id=42, parent=types.SimpleNamespace(root=root)
        )
        self.assertEqual(
            bridge._resolve_media_context(msg, "123@chatroom"),
            (42, db, "123@chatroom"),
        )

    def test_prefers_chat_db_when_available(self):
        import types

        module = self._module()
        chat_db = object()
        bridge = self._bridge(module, types.SimpleNamespace(_db=object()))
        root = types.SimpleNamespace(_wxid="123@chatroom", _db=chat_db)
        msg = types.SimpleNamespace(
            local_id=7, parent=types.SimpleNamespace(root=root)
        )
        self.assertEqual(
            bridge._resolve_media_context(msg, "123@chatroom"),
            (7, chat_db, "123@chatroom"),
        )

    def test_missing_local_id_returns_none(self):
        import types

        module = self._module()
        bridge = self._bridge(module, types.SimpleNamespace(_db=object()))
        root = types.SimpleNamespace(_wxid="x@chatroom")
        msg = types.SimpleNamespace(
            local_id=None, parent=types.SimpleNamespace(root=root)
        )
        self.assertIsNone(bridge._resolve_media_context(msg, "x@chatroom"))

    def test_non_numeric_local_id_returns_none(self):
        import types

        module = self._module()
        bridge = self._bridge(module, types.SimpleNamespace(_db=object()))
        root = types.SimpleNamespace(_wxid="x@chatroom")
        msg = types.SimpleNamespace(
            local_id="abc", parent=types.SimpleNamespace(root=root)
        )
        self.assertIsNone(bridge._resolve_media_context(msg, "x@chatroom"))


class StubResolver:
    """替代 LinkResolver 的测试替身：完全不触网。"""

    def __init__(self, infos=None, download_path=""):
        self.timeout = 5.0
        self._infos = dict(infos or {})
        self.download_path = download_path
        self.batches = []
        self.downloads = []

    def cached(self, url):
        return self._infos.get(url)

    async def resolve_many(self, urls, limit=3):
        self.batches.append(list(urls))
        return {url: self._infos[url] for url in urls if url in self._infos}

    async def download(self, url, outdir, max_mb=0):
        self.downloads.append((url, outdir, max_mb))
        return self.download_path


class LinkExtractTests(unittest.TestCase):
    """从消息文本里抽取 URL。"""

    def test_extracts_http_and_bare_www(self):
        self.assertEqual(
            extract_urls("看 https://a.com/x 和 www.b.com/y，谢谢"),
            ["https://a.com/x", "http://www.b.com/y"],
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(
            extract_urls("链接：https://youtu.be/abc。"), ["https://youtu.be/abc"]
        )
        self.assertEqual(extract_urls("见（https://a.com/b）"), ["https://a.com/b"])

    def test_keeps_balanced_parentheses(self):
        url = "https://en.wikipedia.org/wiki/Mercury_(planet)"
        self.assertEqual(extract_urls(url), [url])

    def test_does_not_swallow_following_chinese(self):
        self.assertEqual(extract_urls("http://a.com/b，还有别的"), ["http://a.com/b"])

    def test_dedupes_and_preserves_order(self):
        self.assertEqual(
            extract_urls("https://a.com https://b.com https://a.com"),
            ["https://a.com", "https://b.com"],
        )

    def test_no_url(self):
        self.assertEqual(extract_urls("没有链接"), [])
        self.assertEqual(extract_urls(""), [])


class LinkInfoTests(unittest.TestCase):
    """解析结果压成上下文文本。"""

    def test_summary_contains_fields(self):
        info = LinkInfo(
            url="https://x",
            ok=True,
            title="标题",
            uploader="作者",
            duration=213,
            description="简介",
            extractor="YouTube",
            is_media=True,
            webpage_url="https://x",
        )
        text = info.summary()
        for token in ("[链接]", "标题：标题", "作者：作者", "时长：3:33", "简介：简介"):
            self.assertIn(token, text)

    def test_failure_summary(self):
        text = LinkInfo(url="https://x", ok=False, error="超时").summary()
        self.assertIn("解析失败", text)
        self.assertIn("超时", text)

    def test_custom_label_is_used_as_prefix(self):
        """推文等特殊来源可改写前缀（默认仍是「链接」）。"""
        self.assertTrue(LinkInfo(url="https://x").summary().startswith("[链接]"))
        info = LinkInfo(url="https://x", ok=True, label="推文", description="正文")
        self.assertTrue(info.summary().startswith("[推文]"))
        self.assertTrue(
            LinkInfo(url="https://x", ok=False, label="推文").summary().startswith(
                "[推文]"
            )
        )

    def test_meta_is_merged_into_summary(self):
        """额外元信息（推文时间/点赞）原样并入摘要。"""
        info = LinkInfo(
            url="https://x", ok=True, extractor="X", meta="2014-09-03 23:18 · 458 喜欢"
        )
        text = info.summary()
        self.assertIn("2014-09-03 23:18", text)
        self.assertIn("458 喜欢", text)
        # 空 meta 不应产生多余的分隔符
        self.assertNotIn("｜｜", LinkInfo(url="https://x", ok=True).summary())

    def test_format_block_joins_lines(self):
        block = format_link_block(
            [
                LinkInfo(url="https://a", ok=True, title="A"),
                LinkInfo(url="https://b", ok=True, title="B"),
            ]
        )
        self.assertEqual(len(block.splitlines()), 2)


class _FakeResponse:
    """urllib 响应的最小替身（仅短链展开用到的两个方法）。"""

    def __init__(self, final_url: str, body: bytes = b"") -> None:
        self._final = final_url
        self._body = body

    def geturl(self) -> str:
        return self._final

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ShortLinkTests(unittest.TestCase):
    """短链展开：yt-dlp 不认识 b23.tv 等主机，须先拿到真实地址。"""

    def _patch_urlopen(self, response):
        return mock.patch.object(
            links_mod.urllib.request, "urlopen", lambda *a, **k: response
        )

    def test_is_short_link(self):
        self.assertTrue(links_mod._is_short_link("https://b23.tv/P7kJlgt"))
        self.assertTrue(links_mod._is_short_link("https://www.b23.tv/abc"))
        self.assertFalse(links_mod._is_short_link("https://www.bilibili.com/video/BV1"))
        self.assertFalse(links_mod._is_short_link("https://example.com/b23.tv"))

    def test_normal_url_is_not_expanded(self):
        """非短链主机不应产生任何网络请求。"""
        with mock.patch.object(
            links_mod.urllib.request,
            "urlopen",
            side_effect=AssertionError("不应发起请求"),
        ):
            self.assertEqual(
                links_mod.LinkResolver()._expand_short_link("https://example.com/x"),
                ("", ""),
            )

    def test_follows_redirect_to_real_page(self):
        resp = _FakeResponse("https://www.bilibili.com/video/BV1xx411c7mD")
        with self._patch_urlopen(resp):
            target, err = links_mod.LinkResolver()._expand_short_link("https://b23.tv/abc")
        self.assertEqual(err, "")
        self.assertEqual(target, "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_dead_short_link_reports_error(self):
        body = b'{"code":-404,"message":"\xe5\x95\xa5\xe9\x83\xbd\xe6\x9c\xa8\xe6\x9c\x89","ttl":1}'
        resp = _FakeResponse("https://b23.tv/abc", body)
        with self._patch_urlopen(resp):
            target, err = links_mod.LinkResolver()._expand_short_link("https://b23.tv/abc")
        self.assertEqual(target, "")
        self.assertEqual(err, "短链已失效")

    def test_extracts_target_from_body(self):
        """JS/meta 跳转（无 302）时从正文里捞真实地址。"""
        body = b'<script>location.href="https://www.bilibili.com/video/BV1yy411c7mE"</script>'
        resp = _FakeResponse("https://b23.tv/abc", body)
        with self._patch_urlopen(resp):
            target, _ = links_mod.LinkResolver()._expand_short_link("https://b23.tv/abc")
        self.assertEqual(target, "https://www.bilibili.com/video/BV1yy411c7mE")

    def test_rekey_keeps_original_url(self):
        info = LinkInfo(url="https://www.bilibili.com/video/BV1", ok=True, title="T")
        links_mod._rekey(info, "https://b23.tv/abc", "https://www.bilibili.com/video/BV1")
        self.assertEqual(info.url, "https://b23.tv/abc")
        self.assertEqual(info.webpage_url, "https://www.bilibili.com/video/BV1")

    def test_resolve_sync_rekeys_result_to_original_url(self):
        """展开后缓存键必须是原始短链（_link_block 按原文查找）。"""
        resolver = links_mod.LinkResolver()
        resolver._extract_with_ytdlp = lambda module, url: LinkInfo(
            url=url, ok=True, title="真实标题", webpage_url=url
        )
        resp = _FakeResponse("https://www.bilibili.com/video/BV1xx411c7mD")
        with self._patch_urlopen(resp), mock.patch.object(
            links_mod, "load_yt_dlp", lambda: object()
        ):
            info = resolver._resolve_sync("https://b23.tv/abc")
        self.assertEqual(info.url, "https://b23.tv/abc")
        self.assertEqual(info.title, "真实标题")
        self.assertEqual(info.webpage_url, "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_dead_short_link_short_circuits_resolve(self):
        body = b'{"code":-404}'
        resp = _FakeResponse("https://b23.tv/abc", body)
        with self._patch_urlopen(resp):
            info = links_mod.LinkResolver()._resolve_sync("https://b23.tv/abc")
        self.assertFalse(info.ok)
        self.assertEqual(info.error, "短链已失效")

    def test_clean_display_url_strips_tracking_params(self):
        noisy = (
            "https://www.bilibili.com/video/BV19pHi6UEmz/?buvid=XU8&mid=GVK"
            "&p=1&share_source=COPY&unique_k=hOgf9CN&up_id=354"
        )
        self.assertEqual(
            links_mod._clean_display_url(noisy),
            "https://www.bilibili.com/video/BV19pHi6UEmz/",
        )

    def test_clean_display_url_keeps_meaningful_query(self):
        self.assertEqual(
            links_mod._clean_display_url("https://example.com/watch?v=abc123"),
            "https://example.com/watch?v=abc123",
        )
        self.assertEqual(
            links_mod._clean_display_url("https://example.com/plain"),
            "https://example.com/plain",
        )

    def test_download_expands_short_link(self):
        """下载前也必须展开短链，否则 yt-dlp 拿不到真实地址。"""
        resolver = links_mod.LinkResolver()
        seen = {}

        class _FakeYDL:
            def __init__(self, options):
                seen["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                seen["url"] = url
                return {"requested_downloads": [{"filepath": ""}]}

        resp = _FakeResponse("https://www.bilibili.com/video/BV1xx411c7mD")
        with tempfile.TemporaryDirectory() as tmp:
            with self._patch_urlopen(resp), mock.patch.object(
                links_mod, "load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=_FakeYDL)
            ):
                resolver._download_sync("https://b23.tv/abc", tmp, 0)
        self.assertEqual(seen["url"], "https://www.bilibili.com/video/BV1xx411c7mD")


class TweetTests(unittest.TestCase):
    """X 推文：URL 识别、token、JSON 解析、卡片渲染。"""

    def test_is_tweet_url(self):
        from wechat_mcp.bot import tweet as tweet_mod

        for url in (
            "https://x.com/Interior/status/507185938620219395",
            "https://twitter.com/Interior/status/507185938620219395",
            "https://mobile.x.com/Interior/statuses/507185938620219395",
        ):
            self.assertTrue(tweet_mod.is_tweet_url(url), url)
        for url in (
            "https://x.com/Interior",
            "https://example.com/status/1",
            "https://b23.tv/abc",
        ):
            self.assertFalse(tweet_mod.is_tweet_url(url), url)

    def test_tweet_id(self):
        from wechat_mcp.bot import tweet as tweet_mod

        self.assertEqual(tweet_mod.tweet_id("https://x.com/a/status/123"), "123")
        self.assertIsNone(tweet_mod.tweet_id("https://x.com/a"))

    def test_token_matches_react_tweet_algorithm(self):
        from wechat_mcp.bot import tweet as tweet_mod

        self.assertEqual(
            tweet_mod._token("507185938620219395"), "189ddm8u5hzych3nyqhvvx6r"
        )

    def test_parse_tweet(self):
        from wechat_mcp.bot import tweet as tweet_mod

        payload = {
            "id_str": "123",
            "text": "你好 world 🎉",
            "created_at": "2026-10-05T01:30:00.000Z",
            "favorite_count": 12,
            "conversation_count": 3,
            "user": {
                "name": "测试",
                "screen_name": "demo",
                "is_blue_verified": True,
                "profile_image_url_https": "https://img/a.jpg",
            },
            "photos": [{"url": "https://img/1.jpg"}],
            "mediaDetails": [{"type": "video"}],
        }
        info = tweet_mod.parse_tweet(payload, "https://x.com/demo/status/123")
        self.assertIsNotNone(info)
        self.assertEqual(info.author_handle, "demo")
        self.assertEqual(info.author_name, "测试")
        self.assertTrue(info.has_video)
        self.assertEqual(info.photos, ["https://img/1.jpg"])
        self.assertEqual(info.likes, 12)
        self.assertIn("demo", info.summary())
        self.assertIn("含视频", info.summary())

    def test_parse_tweet_without_media_is_not_video(self):
        from wechat_mcp.bot import tweet as tweet_mod

        info = tweet_mod.parse_tweet(
            {"id_str": "1", "text": "纯文字", "user": {"screen_name": "a"}},
            "https://x.com/a/status/1",
        )
        self.assertFalse(info.has_video)
        self.assertEqual(info.photos, [])

    def test_parse_tweet_rejects_payload_without_id(self):
        from wechat_mcp.bot import tweet as tweet_mod

        self.assertIsNone(tweet_mod.parse_tweet({}, "https://x.com/a/status/1"))

    def test_parse_tweet_picks_best_video_variant(self):
        """有多个 mp4 变体时取码率最高的那条；非 mp4 一律忽略。"""
        from wechat_mcp.bot import tweet as tweet_mod

        payload = {
            "id_str": "9",
            "text": "带视频",
            "user": {"screen_name": "a"},
            "mediaDetails": [
                {
                    "type": "video",
                    "video_info": {
                        "variants": [
                            {"content_type": "application/x-mpegURL", "url": "https://m3u8"},
                            {"content_type": "video/mp4", "bitrate": 256000, "url": "https://low.mp4"},
                            {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://high.mp4"},
                        ]
                    },
                }
            ],
        }
        info = tweet_mod.parse_tweet(payload, "https://x.com/a/status/9")
        self.assertTrue(info.has_video)
        self.assertEqual(info.video_url, "https://high.mp4")

    def test_parse_tweet_without_variants_has_no_video_url(self):
        from wechat_mcp.bot import tweet as tweet_mod

        info = tweet_mod.parse_tweet(
            {"id_str": "9", "mediaDetails": [{"type": "video"}]}, "https://x.com/a/status/9"
        )
        self.assertTrue(info.has_video)
        self.assertEqual(info.video_url, "")

    def test_parse_tweet_uses_video_poster_as_thumbnail(self):
        """视频/动图没有 photos 条目，须用封面帧当卡片缩略图。"""
        from wechat_mcp.bot import tweet as tweet_mod

        payload = {
            "id_str": "9",
            "text": "带视频",
            "user": {"screen_name": "a"},
            "mediaDetails": [
                {
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/thumb.jpg",
                    "video_info": {
                        "variants": [
                            {"content_type": "video/mp4", "bitrate": 1, "url": "https://v.mp4"}
                        ]
                    },
                }
            ],
        }
        info = tweet_mod.parse_tweet(payload, "https://x.com/a/status/9")
        self.assertEqual(info.video_poster, "https://pbs.twimg.com/thumb.jpg")
        self.assertEqual(info.photos, ["https://pbs.twimg.com/thumb.jpg"])
        # 有视频时摘要说「含视频」，不该降级成「含 N 张图」
        self.assertIn("含视频", info.summary())
        self.assertNotIn("张图", info.summary())

    def test_parse_tweet_without_poster_has_no_video_poster(self):
        from wechat_mcp.bot import tweet as tweet_mod

        info = tweet_mod.parse_tweet(
            {"id_str": "9", "mediaDetails": [{"type": "video"}]}, "https://x.com/a/status/9"
        )
        self.assertEqual(info.video_poster, "")
        self.assertEqual(info.photos, [])

    def test_download_video_streams_to_file(self):
        from wechat_mcp.bot import tweet as tweet_mod

        class _Resp:
            headers = {"Content-Length": "6"}

            def __init__(self):
                self._chunks = [b"abc", b"def", b""]

            def read(self, _n=-1):
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        info = tweet_mod.Tweet(
            id="77", url="https://x.com/a/status/77", has_video=True, video_url="https://v.mp4"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                tweet_mod.urllib.request, "urlopen", lambda *a, **k: _Resp()
            ):
                path = tweet_mod.download_video(info, tmp, timeout=5)
            self.assertIsNotNone(path)
            self.assertEqual(Path(path).read_bytes(), b"abcdef")

    def test_download_video_respects_size_limit(self):
        from wechat_mcp.bot import tweet as tweet_mod

        class _Resp:
            headers = {"Content-Length": str(10 * 1024 * 1024)}

            def read(self, _n=-1):
                return b"x" * 1024

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        info = tweet_mod.Tweet(
            id="78", url="https://x.com/a/status/78", has_video=True, video_url="https://v.mp4"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                tweet_mod.urllib.request, "urlopen", lambda *a, **k: _Resp()
            ):
                path = tweet_mod.download_video(info, tmp, timeout=5, max_mb=1)
            self.assertIsNone(path)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_render_card_writes_png(self):
        """不触网渲染（无头像/无配图）也要能出图，且中英 emoji 混排不报错。"""
        from wechat_mcp.bot import tweet as tweet_mod

        info = tweet_mod.Tweet(
            id="1",
            url="https://x.com/a/status/1",
            text="中文 English 混排 🎉 折行测试，这句话要足够长才能触发换行逻辑。" * 4,
            author_name="某人 Somebody",
            author_handle="someone",
            verified="blue",
            created_at="2026-10-05 09:30",
            likes=7,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = tweet_mod.render_card(info, tmp, timeout=5)
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).is_file())
            self.assertGreater(Path(path).stat().st_size, 1000)


class TweetEngineTests(unittest.IsolatedAsyncioTestCase):
    """引擎：推文链接发卡片图；含视频时再下载视频回发。"""

    def _engine(self, adapter, **cfg_kwargs):
        options = dict(enabled=True, cooldown_seconds=0.0, reply_probability=1.0)
        options.update(cfg_kwargs)
        cfg = BotConfig(**options)
        llm = FakeLLM(cfg)
        return BotEngine(adapter, cfg, llm_factory=lambda c: llm)

    async def _run(self, has_video: bool):
        from wechat_mcp.bot import tweet as tweet_mod

        adapter = FakeAdapter()
        engine = self._engine(adapter, link_download_enabled=True)
        url = "https://x.com/demo/status/123"
        info = tweet_mod.Tweet(
            id="123",
            url=url,
            text="推文正文",
            author_name="作者",
            author_handle="demo",
            has_video=has_video,
        )
        original = (tweet_mod.fetch_tweet_cached, tweet_mod.render_card)
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.png"
            card.write_bytes(b"card")
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            tweet_mod.fetch_tweet_cached = lambda *a, **k: info
            tweet_mod.render_card = lambda *a, **k: card

            async def _fake_download(u, outdir, max_mb=0):
                return str(video)

            engine._links.download = _fake_download
            try:
                record = make_record(
                    is_at_me=True, message_id="m1", content=f"看看这个 {url}"
                )
                await engine._download_and_send(record)
            finally:
                tweet_mod.fetch_tweet_cached, tweet_mod.render_card = original
        return [Path(path).name for _, path in adapter.files]

    async def test_tweet_with_video_sends_card_then_video(self):
        sent = await self._run(has_video=True)
        self.assertEqual(sent, ["card.png", "video.mp4"])

    async def test_tweet_without_video_sends_only_card(self):
        sent = await self._run(has_video=False)
        self.assertEqual(sent, ["card.png"])

    async def test_tweet_card_can_be_disabled(self):
        """关掉卡片开关后，含视频的推文只发视频。"""
        from wechat_mcp.bot import tweet as tweet_mod

        adapter = FakeAdapter()
        engine = self._engine(
            adapter, link_download_enabled=True, link_tweet_card_enabled=False
        )
        url = "https://x.com/demo/status/123"
        info = tweet_mod.Tweet(id="123", url=url, text="t", has_video=True)
        original = tweet_mod.fetch_tweet_cached
        tweet_mod.fetch_tweet_cached = lambda *a, **k: info
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"v")

            async def _fake_download(u, outdir, max_mb=0):
                return str(video)

            engine._links.download = _fake_download
            try:
                await engine._download_and_send(
                    make_record(is_at_me=True, message_id="m1", content=f"看 {url}")
                )
            finally:
                tweet_mod.fetch_tweet_cached = original
        self.assertEqual([Path(p).name for _, p in adapter.files], ["video.mp4"])

    async def test_tweet_video_prefers_direct_link(self):
        """有 mp4 直链时直接下载，不该再走 yt-dlp。"""
        from wechat_mcp.bot import tweet as tweet_mod

        adapter = FakeAdapter()
        # 本用例只关心视频下载路径，关掉卡片开关以免多出一张卡片图。
        engine = self._engine(
            adapter, link_download_enabled=True, link_tweet_card_enabled=False
        )
        url = "https://x.com/demo/status/123"
        info = tweet_mod.Tweet(
            id="123", url=url, text="t", has_video=True, video_url="https://v.mp4"
        )
        original = (tweet_mod.fetch_tweet_cached, tweet_mod.download_video)
        called: list[str] = []

        async def _fake_ytdlp(u, outdir, max_mb=0):
            called.append("ytdlp")
            return None

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "tweet-123.mp4"
            video.write_bytes(b"v")
            tweet_mod.fetch_tweet_cached = lambda *a, **k: info
            tweet_mod.download_video = lambda *a, **k: video
            engine._links.download = _fake_ytdlp
            try:
                await engine._download_and_send(
                    make_record(is_at_me=True, message_id="m1", content=f"看 {url}")
                )
            finally:
                tweet_mod.fetch_tweet_cached, tweet_mod.download_video = original
        self.assertEqual(called, [])
        self.assertEqual([Path(p).name for _, p in adapter.files], ["tweet-123.mp4"])

    async def test_tweet_video_falls_back_to_ytdlp(self):
        """直链下载失败时退回 yt-dlp。"""
        from wechat_mcp.bot import tweet as tweet_mod

        adapter = FakeAdapter()
        engine = self._engine(
            adapter, link_download_enabled=True, link_tweet_card_enabled=False
        )
        url = "https://x.com/demo/status/123"
        info = tweet_mod.Tweet(
            id="123", url=url, text="t", has_video=True, video_url="https://v.mp4"
        )
        original = (tweet_mod.fetch_tweet_cached, tweet_mod.download_video)
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"v")

            async def _fake_ytdlp(u, outdir, max_mb=0):
                return str(video)

            tweet_mod.fetch_tweet_cached = lambda *a, **k: info
            tweet_mod.download_video = lambda *a, **k: None
            engine._links.download = _fake_ytdlp
            try:
                await engine._download_and_send(
                    make_record(is_at_me=True, message_id="m1", content=f"看 {url}")
                )
            finally:
                tweet_mod.fetch_tweet_cached, tweet_mod.download_video = original
        self.assertEqual([Path(p).name for _, p in adapter.files], ["video.mp4"])


class LinkAckTests(unittest.IsolatedAsyncioTestCase):
    """链接服务：识别到链接就回**固定**提示（不经过大模型），私聊 / 群聊都生效。"""

    def _engine(self, adapter, **cfg_kwargs):
        options = dict(enabled=True, cooldown_seconds=0.0, reply_probability=1.0)
        options.update(cfg_kwargs)
        cfg = BotConfig(**options)
        llm = FakeLLM(cfg)
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: llm)
        engine._llm = llm  # 供断言模型是否被调用
        return engine

    async def _run(self, record, **cfg_kwargs):
        adapter = FakeAdapter()
        engine = self._engine(adapter, **cfg_kwargs)
        await engine._handle(record)
        return adapter, engine

    @staticmethod
    async def _drain(engine, rounds: int = 100) -> None:
        """等后台的下载任务跑完。"""
        for _ in range(rounds):
            if not engine._download_tasks:
                return
            await asyncio.sleep(0.01)

    async def test_group_link_gets_fixed_ack_without_at(self):
        """群里没 @ 机器人，只要有链接也要回固定提示。"""
        adapter, _ = await self._run(
            make_record(is_group=True, content="看这个 https://x.com/a/status/1")
        )
        self.assertEqual(adapter.sent, [("测试群", "正在解析链接")])

    async def test_private_link_gets_fixed_ack(self):
        adapter, _ = await self._run(
            make_record(is_group=False, content="https://example.com/a")
        )
        self.assertEqual(adapter.sent[0], ("测试群", "正在解析链接"))

    async def test_message_without_link_has_no_ack(self):
        adapter, _ = await self._run(make_record(content="今天天气不错"))
        self.assertEqual(adapter.sent, [])

    async def test_ack_text_is_configurable(self):
        adapter, _ = await self._run(
            make_record(content="https://example.com/a"), link_ack_text="稍等，我看看"
        )
        self.assertEqual(adapter.sent, [("测试群", "稍等，我看看")])

    async def test_ack_can_be_disabled(self):
        adapter, _ = await self._run(
            make_record(content="https://example.com/a"), link_ack_enabled=False
        )
        self.assertEqual(adapter.sent, [])

    async def test_blank_ack_text_sends_nothing(self):
        adapter, _ = await self._run(
            make_record(content="https://example.com/a"), link_ack_text="   "
        )
        self.assertEqual(adapter.sent, [])

    async def test_group_out_of_scope_has_no_ack(self):
        adapter, _ = await self._run(
            make_record(is_group=True, chat="别的群", content="https://example.com/a"),
            all_groups=False,
            groups=["测试群"],
        )
        self.assertEqual(adapter.sent, [])

    async def test_quiet_hours_skip_ack(self):
        adapter = FakeAdapter()
        engine = self._engine(adapter)
        engine._in_quiet_hours = lambda now=None: True
        await engine._handle(make_record(content="https://example.com/a"))
        self.assertEqual(adapter.sent, [])

    async def test_same_message_id_is_acked_once(self):
        adapter = FakeAdapter()
        engine = self._engine(adapter)
        record = make_record(message_id="m1", content="https://example.com/a")
        await engine._handle(record)
        await engine._handle(record)
        self.assertEqual(len(adapter.sent), 1)

    async def test_llm_still_replies_by_default(self):
        """默认 link_llm_followup=True：原本会回复的场景保持原样（提示 + 模型回复）。"""
        adapter, engine = await self._run(
            make_record(is_group=False, content="https://example.com/a")
        )
        self.assertEqual(
            [text for _, text in adapter.sent], ["正在解析链接", "本鲸鱼娘收到啦。"]
        )
        self.assertEqual(engine._llm.calls, 1)

    async def test_llm_followup_can_be_disabled(self):
        """关掉后带链接的消息只回固定提示，不再调用模型。"""
        adapter, engine = await self._run(
            make_record(is_group=False, content="https://example.com/a"),
            link_llm_followup=False,
        )
        self.assertEqual([text for _, text in adapter.sent], ["正在解析链接"])
        self.assertEqual(engine._llm.calls, 0)

    async def test_group_link_downloads_without_at(self):
        """群里没 @ 机器人也要把链接内容下载回发。"""
        adapter = FakeAdapter()
        engine = self._engine(adapter, link_download_enabled=True)
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"v")

            async def _fake_download(u, outdir, max_mb=0):
                return str(video)

            engine._links.download = _fake_download
            await engine._handle(
                make_record(is_group=True, content="https://example.com/v")
            )
            await self._drain(engine)
        self.assertEqual([Path(p).name for _, p in adapter.files], ["video.mp4"])

    async def test_link_download_is_scheduled_only_once(self):
        """链接服务统一调度下载：不会再被模型回复路径重复调度。"""
        adapter = FakeAdapter()
        engine = self._engine(adapter, link_download_enabled=True)
        started: list[str] = []

        async def _fake_download(u, outdir, max_mb=0):
            started.append(u)
            return None

        engine._links.download = _fake_download
        await engine._handle(
            make_record(is_group=False, content="https://example.com/v")
        )
        await self._drain(engine)
        self.assertEqual(started, ["https://example.com/v"])


class LinkConfigTests(unittest.TestCase):
    """链接解析配置的默认值与范围纠正。"""

    def test_defaults(self):
        cfg = BotConfig()
        self.assertTrue(cfg.link_parse_enabled)
        self.assertEqual(cfg.link_parse_max, 3)
        self.assertFalse(cfg.link_download_enabled)
        self.assertEqual(cfg.link_download_max, 1)
        self.assertEqual(cfg.link_download_max_mb, 100)
        self.assertTrue(cfg.link_tweet_card_enabled)
        # 识别到链接就回固定提示；默认不改变原有的模型回复行为。
        self.assertTrue(cfg.link_ack_enabled)
        self.assertEqual(cfg.link_ack_text, "正在解析链接")
        self.assertTrue(cfg.link_llm_followup)

    def test_ack_text_is_trimmed_and_newlines_collapsed(self):
        cfg = BotConfig.from_dict({"link_ack_text": "  正在\n解析  链接  "})
        self.assertEqual(cfg.link_ack_text, "正在 解析 链接")
        self.assertEqual(BotConfig.from_dict({"link_ack_text": "   "}).link_ack_text, "")

    def test_clamps(self):
        cfg = BotConfig.from_dict(
            {
                "link_parse_max": -3,
                "link_parse_timeout": 0,
                "link_download_max": -1,
                "link_download_max_mb": -5,
                "link_download_dir": "   ",
            }
        )
        self.assertEqual(cfg.link_parse_max, 0)
        self.assertEqual(cfg.link_parse_timeout, 1.0)
        self.assertEqual(cfg.link_download_max, 0)
        self.assertEqual(cfg.link_download_max_mb, 0)
        self.assertEqual(cfg.link_download_dir, "")

    def test_download_dir_default_and_override(self):
        self.assertTrue(str(BotConfig().download_dir()).endswith("downloads"))
        cfg = BotConfig.from_dict({"link_download_dir": tempfile.gettempdir()})
        self.assertEqual(cfg.download_dir(), Path(tempfile.gettempdir()))


class LinkEngineTests(unittest.IsolatedAsyncioTestCase):
    """引擎：链接信息注入上下文 + 下载回发。"""

    def _engine(self, adapter, **cfg_kwargs):
        options = dict(enabled=True, cooldown_seconds=0.0, reply_probability=1.0)
        options.update(cfg_kwargs)
        cfg = BotConfig(**options)
        llm = FakeLLM(cfg)
        engine = BotEngine(adapter, cfg, llm_factory=lambda c: llm)
        return engine, llm

    @staticmethod
    def _user_text(llm):
        return "\n".join(
            str(m["content"]) for m in llm.last_messages if m["role"] == "user"
        )

    async def test_link_info_injected_into_context(self):
        adapter = FakeAdapter()
        engine, llm = self._engine(adapter)
        url = "https://example.com/v"
        engine._links = StubResolver(
            {
                url: LinkInfo(
                    url=url,
                    ok=True,
                    title="示例视频",
                    uploader="某人",
                    duration=213,
                    description="简介",
                    extractor="Test",
                    is_media=True,
                    webpage_url=url,
                )
            }
        )
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, message_id="m1", content=f"看看这个 {url}")
        )
        await adapter.pump()
        text = self._user_text(llm)
        self.assertIn("[链接]", text)
        self.assertIn("示例视频", text)
        self.assertIn("3:33", text)
        await engine.stop()

    async def test_parse_disabled_no_link_block(self):
        adapter = FakeAdapter()
        engine, llm = self._engine(adapter, link_parse_enabled=False)
        url = "https://example.com/v"
        engine._links = StubResolver({url: LinkInfo(url=url, ok=True, title="X")})
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, message_id="m1", content=f"看看 {url}")
        )
        await adapter.pump()
        self.assertNotIn("[链接]", self._user_text(llm))
        await engine.stop()

    async def test_prefetch_covers_whole_context(self):
        """触发消息之外、上下文里更早出现的链接也要被解析。"""
        adapter = FakeAdapter()
        engine, _llm = self._engine(adapter)
        engine._links = StubResolver()
        await engine.start()
        await adapter.dispatch(
            make_record(
                sender="李四", message_id="m0", content="之前的 https://old.example/x"
            )
        )
        await adapter.pump()
        await adapter.dispatch(make_record(is_at_me=True, message_id="m1", content="在吗"))
        await adapter.pump()
        self.assertTrue(engine._links.batches)
        self.assertIn("https://old.example/x", engine._links.batches[-1])
        await engine.stop()

    async def test_download_and_send_back(self):
        adapter = FakeAdapter()
        engine, _llm = self._engine(adapter, link_download_enabled=True)
        url = "https://example.com/v"
        engine._links = StubResolver(
            {url: LinkInfo(url=url, ok=True, title="X")},
            download_path="C:/tmp/v.mp4",
        )
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, message_id="m1", content=f"下载 {url}")
        )
        await adapter.pump()
        self.assertEqual(adapter.files, [("测试群", "C:/tmp/v.mp4")])
        await engine.stop()

    async def test_download_disabled_sends_no_file(self):
        adapter = FakeAdapter()
        engine, _llm = self._engine(adapter)
        url = "https://example.com/v"
        engine._links = StubResolver(
            {url: LinkInfo(url=url, ok=True, title="X")},
            download_path="C:/tmp/v.mp4",
        )
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, message_id="m1", content=f"下载 {url}")
        )
        await adapter.pump()
        self.assertEqual(adapter.files, [])
        await engine.stop()

    async def test_download_dir_added_to_send_whitelist(self):
        adapter = FakeAdapter()
        adapter.config = SimpleNamespace(allow_send_dirs=())
        engine, _llm = self._engine(adapter, link_download_enabled=True)
        url = "https://example.com/v"
        engine._links = StubResolver(
            {url: LinkInfo(url=url, ok=True, title="X")},
            download_path="C:/tmp/v.mp4",
        )
        await engine.start()
        await adapter.dispatch(
            make_record(is_at_me=True, message_id="m1", content=f"下载 {url}")
        )
        await adapter.pump()
        self.assertTrue(adapter.config.allow_send_dirs)
        await engine.stop()


    async def test_download_is_scheduled_in_background_and_deduped(self):
        """下载必须丢到后台（不阻塞消费循环），且同一链接不并发重复下载。"""
        adapter = FakeAdapter()
        engine, _ = self._engine(adapter, link_download_enabled=True)
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def _fake_download(record):
            calls.append(record.content)
            started.set()
            await release.wait()

        engine._download_and_send = _fake_download
        record = make_record(
            is_at_me=True, message_id="m1", content="看这个 https://b23.tv/abc"
        )
        engine._schedule_download(record)
        engine._schedule_download(record)  # 同一链接：不应再起第二个任务

        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(len(engine._download_tasks), 1)
        self.assertIn("https://b23.tv/abc", engine._downloading_urls)

        release.set()
        await asyncio.gather(*list(engine._download_tasks))
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine._download_tasks, set())
        self.assertEqual(engine._downloading_urls, set())


class LinkDownloadTests(unittest.TestCase):
    """下载路径抓取：合流后必须返回最终文件，而不是已被删除的分片。"""

    class _FakeYDL:
        def __init__(self, options, result, hook_filename=""):
            self._options = options
            self._result = result
            self._hook_filename = hook_filename

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if self._hook_filename:
                for hook in self._options.get("progress_hooks", []):
                    hook({"status": "finished", "filename": self._hook_filename})
            return self._result

    def _run(self, tmp, result, hook_filename="", before_extra=None):
        class FakeModule:
            YoutubeDL = staticmethod(
                lambda options: LinkDownloadTests._FakeYDL(
                    options, result, hook_filename
                )
            )

        original = links_mod.load_yt_dlp
        links_mod.load_yt_dlp = lambda: FakeModule
        try:
            return links_mod.LinkResolver()._download_sync("https://x", tmp, 0)
        finally:
            links_mod.load_yt_dlp = original

    def test_prefers_final_file_over_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "video.mp4"
            final.write_bytes(b"x")
            part = Path(tmp) / "video.f30280.m4a"  # 合流后已被删除
            path = self._run(
                tmp,
                {"requested_downloads": [{"filepath": str(final)}]},
                hook_filename=str(part),
            )
            self.assertEqual(path, str(final))

    def test_falls_back_to_newly_created_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "old.mp4").write_bytes(b"old")
            path = self._run(tmp, {})  # 没有 requested_downloads 时
            # 兜底只在有「新增文件」时返回；此处无新增 → None
            self.assertIsNone(path)

    def test_returns_none_when_yt_dlp_unavailable(self):
        original = links_mod.load_yt_dlp
        links_mod.load_yt_dlp = lambda: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(
                    links_mod.LinkResolver()._download_sync("https://x", tmp, 0)
                )
        finally:
            links_mod.load_yt_dlp = original


if __name__ == "__main__":
    unittest.main()
