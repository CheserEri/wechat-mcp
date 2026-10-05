"""Internet 区域标记（Mark-of-the-Web）清除逻辑的单元测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from wechat_mcp.unblock import (
    bundled_root,
    has_zone_identifier,
    unblock_bundled_assemblies,
)

_ZONE_BODY = "[ZoneTransfer]\nZoneId=3\n"


def _mark(path: Path) -> None:
    """给文件打上 Internet 区域标记（写 Zone.Identifier 数据流）。"""
    with open(str(path) + ":Zone.Identifier", "w", encoding="utf-8") as handle:
        handle.write(_ZONE_BODY)


@unittest.skipUnless(sys.platform == "win32", "仅 Windows 有 Zone.Identifier")
class ZoneIdentifierTests(unittest.TestCase):
    def test_plain_file_has_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.dll"
            path.write_bytes(b"x")
            self.assertFalse(has_zone_identifier(path))

    def test_marker_detected_after_marking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marked.dll"
            path.write_bytes(b"x")
            _mark(path)
            self.assertTrue(has_zone_identifier(path))

    def test_missing_file_is_not_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(has_zone_identifier(Path(tmp) / "nope.dll"))


@unittest.skipUnless(sys.platform == "win32", "仅 Windows 有 Zone.Identifier")
class UnblockBundledAssembliesTests(unittest.TestCase):
    def _make_bundle(self, root: Path) -> tuple[Path, Path, Path]:
        """搭一个最小冻结包结构，返回 (pythonnet dll, webview dll, 无关文件)。"""
        py_dir = root / "pythonnet" / "runtime"
        wv_dir = root / "webview" / "lib" / "runtimes" / "win-x64" / "native"
        py_dir.mkdir(parents=True)
        wv_dir.mkdir(parents=True)
        py_dll = py_dir / "Python.Runtime.dll"
        wv_dll = wv_dir / "WebView2Loader.dll"
        other = root / "webui" / "index.html"
        other.parent.mkdir(parents=True)
        for path in (py_dll, wv_dll, other):
            path.write_bytes(b"x")
            _mark(path)
        return py_dll, wv_dll, other

    def test_strips_target_assemblies_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py_dll, wv_dll, other = self._make_bundle(root)

            count = unblock_bundled_assemblies(root)

            self.assertEqual(count, 2)
            self.assertFalse(has_zone_identifier(py_dll))
            self.assertFalse(has_zone_identifier(wv_dll))
            # 不在目标范围内的文件不动它，避免无谓地遍历/改写。
            self.assertTrue(has_zone_identifier(other))

    def test_second_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_bundle(root)
            self.assertEqual(unblock_bundled_assemblies(root), 2)
            self.assertEqual(unblock_bundled_assemblies(root), 0)

    def test_missing_root_returns_zero(self):
        self.assertEqual(unblock_bundled_assemblies(None), 0)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                unblock_bundled_assemblies(Path(tmp) / "not-here"), 0
            )

    def test_source_mode_has_no_bundled_root(self):
        """源码模式没有 _MEIPASS，应直接跳过而不是报错。"""
        if getattr(sys, "_MEIPASS", None) is None:
            self.assertIsNone(bundled_root())


if __name__ == "__main__":
    unittest.main()
