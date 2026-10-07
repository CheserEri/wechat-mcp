"""桌面端：运行日志落盘。"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wechat_mcp import desktop


class FileLoggingTests(unittest.TestCase):
    """`install_file_logging`：界面日志进程一退就没了，必须另存一份到磁盘。"""

    def setUp(self):
        from loguru import logger

        self.tmp = Path(tempfile.mkdtemp(prefix="wechat-mcp-logtest-"))
        self._before = set(logger._core.handlers)  # noqa: SLF001

    def tearDown(self):
        from loguru import logger

        for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        # sink 撤掉之后文件句柄才释放，这时才能删临时目录。
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self) -> Path | None:
        with mock.patch.object(desktop, "log_dir", lambda: self.tmp):
            return desktop.install_file_logging()

    def test_writes_log_file_with_content(self):
        from loguru import logger

        self.assertIsNotNone(self._install(), "应返回日志目录")

        logger.info("单元测试：链接解析开始")
        deadline = time.time() + 5.0
        files: list[Path] = []
        while time.time() < deadline:
            files = list(self.tmp.glob("*.log"))
            if files and "链接解析开始" in files[0].read_text(encoding="utf-8"):
                break
            time.sleep(0.05)

        self.assertTrue(files, "应生成日志文件")
        self.assertEqual(files[0].name, f"app-{time.strftime('%Y-%m-%d')}.log")
        self.assertIn("链接解析开始", files[0].read_text(encoding="utf-8"))

    def test_missing_loguru_returns_none(self):
        with mock.patch.dict("sys.modules", {"loguru": None}):
            self.assertIsNone(desktop.install_file_logging())

    def test_log_dir_is_under_appdata(self):
        path = desktop.log_dir()
        self.assertEqual(path.name, "logs")
        self.assertEqual(path.parent, desktop.default_config_path().parent)


class EngineLogForwardingTests(unittest.TestCase):
    """引擎日志必须也落盘。

    回归点（0.8.10 实测）：``on_log`` 在全仓**没有任何地方赋值**，于是引擎自己的
    日志只进内存里的界面缓冲（``UI_LOG.attach``），文件 sink 挂在 loguru 上、收不到
    ——日志文件里只有桥接层/适配层的话。现象是「链接发了、提示回了、然后什么都没有」
    时，翻日志文件**查不出任何原因**（引擎的「开始下载链接内容」「下载失败或超出
    体积上限，已跳过」全都不在文件里）。
    """

    def setUp(self):
        from loguru import logger

        self.tmp = Path(tempfile.mkdtemp(prefix="wechat-mcp-englog-"))
        self._before = set(logger._core.handlers)  # noqa: SLF001

    def tearDown(self):
        from loguru import logger

        for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, needle: str, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            files = list(self.tmp.glob("*.log"))
            if files:
                text = files[0].read_text(encoding="utf-8")
                if needle in text:
                    return text
            time.sleep(0.05)
        return text

    def _install(self) -> None:
        with mock.patch.object(desktop, "log_dir", lambda: self.tmp):
            desktop.install_file_logging()

    def test_engine_line_reaches_the_file(self):
        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="info", message="开始下载链接内容：https://x")
        )
        self.assertIn("开始下载链接内容", self._read("开始下载链接内容"))
        self.assertIn("[引擎]", self._read("[引擎]"))

    def test_engine_levels_are_mapped(self):
        from loguru import logger

        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="warning", message="下载失败或超出体积上限，已跳过")
        )
        text = self._read("下载失败或超出体积上限")
        self.assertIn("WARNING", text, "warning 级别不该被降级成 INFO")

    def test_debug_lines_stay_out_of_the_file(self):
        """文件 sink 是 INFO+，引擎的 debug 行（未 @ 机器人等）不该刷进来。"""
        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="debug", message="单元测试：这条不该落盘")
        )
        time.sleep(0.4)
        files = list(self.tmp.glob("*.log"))
        text = files[0].read_text(encoding="utf-8") if files else ""
        self.assertNotIn("这条不该落盘", text)

    def test_ui_sink_skips_forwarded_records(self):
        """转发到 loguru 的引擎日志不能再回到 UI_LOG，否则会无限递归。

        ``UI_LOG.emit`` 最终调 ``engine._log``；不拦的话链路是
        「引擎日志 → loguru → 界面 sink → engine._log → …」。
        """
        from loguru import logger

        seen: list[tuple[str, str]] = []
        with mock.patch.object(
            desktop.UI_LOG, "emit", lambda level, message: seen.append((level, message))
        ):
            desktop.install_console_capture()
            try:
                desktop.forward_engine_log(
                    SimpleNamespace(level="info", message="引擎转发的这条")
                )
                logger.info("桥接层直接打的这条")
            finally:
                for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
                    try:
                        logger.remove(sink_id)
                    except ValueError:
                        pass
        messages = [str(m).strip() for _level, m in seen]
        self.assertIn("桥接层直接打的这条", messages, "普通 loguru 日志仍要进界面")
        self.assertNotIn(
            "引擎转发的这条", messages, "引擎转发的不该再回界面（会递归）"
        )

    def test_bridge_log_line_lands_in_the_file_once(self):
        """桥接层经 loguru 打的日志不能被「回流」写成两遍。

        回归点（0.8.10 实机）：``UI_LOG.emit`` 会把 loguru 的输出交给引擎，引擎再经
        ``on_log`` 转发回 loguru——界面 sink 的 filter 挡住了回流界面（防递归），但
        **文件 sink 不认那个标记**，于是同一条日志落盘两次（一遍原样、一遍带
        ``[引擎]`` 前缀）。修法是回流期间置位 ``_ENGINE_FORWARDING``。
        """
        from loguru import logger

        class _Engine:
            @staticmethod
            def log(level, message):
                # 模拟 BotEngine._log → on_log 这一段
                desktop.forward_engine_log(
                    SimpleNamespace(level=level, message=message)
                )

        self._install()
        with mock.patch.object(desktop.UI_LOG, "_engine", _Engine()):
            desktop.install_console_capture()
            try:
                logger.info("桥接层直接打的这条")
            finally:
                for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
                    try:
                        logger.remove(sink_id)
                    except ValueError:
                        pass
        # 文件 sink 是 enqueue=True（异步），多等一会儿让它彻底冲刷。
        time.sleep(0.6)
        text = self._read("桥接层直接打的这条")
        self.assertEqual(text.count("桥接层直接打的这条"), 1, text)
        self.assertNotIn("[引擎] 桥接层直接打的这条", text)

    def test_bootstrap_wires_on_log(self):
        """桌面壳启动时必须真的把 ``on_log`` 接上（这就是当初漏掉的那一步）。"""
        import asyncio

        from wechat_mcp.bot import BotConfig

        fake_engine = SimpleNamespace(start=mock.AsyncMock())
        fake_adapter = SimpleNamespace(connect=mock.AsyncMock())
        api = desktop.DesktopApi.__new__(desktop.DesktopApi)

        with mock.patch.object(
            desktop, "DeepSeekGirlAdapter", lambda *a, **k: fake_adapter
        ), mock.patch.object(
            desktop, "BotEngine", lambda *a, **k: fake_engine
        ), mock.patch.object(desktop, "BotConfig") as cfg, mock.patch.object(
            desktop, "UI_LOG"
        ) as ui_log:
            cfg.load.return_value = BotConfig()
            asyncio.run(desktop.DesktopApi._bootstrap_async(api))

        self.assertIs(fake_engine.on_log, desktop.forward_engine_log)
        ui_log.attach.assert_called_once_with(fake_engine)


if __name__ == "__main__":
    unittest.main()
