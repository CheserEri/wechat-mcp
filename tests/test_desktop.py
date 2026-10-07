"""桌面端：运行日志落盘。"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
