"""打包脚本（``packaging/build.py``）辅助函数的单元测试。

只测不依赖 PyInstaller / Inno Setup 的纯逻辑部分：版本号解析与 ISCC 定位。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import build  # noqa: E402 - 需先把 packaging/ 放进 sys.path


class ReadVersionTests(unittest.TestCase):
    def test_reads_version_from_pyproject(self):
        version = build.read_version()
        # 形如 0.8.0 / 1.2.3-rc1，至少要是 major.minor。
        self.assertRegex(version, r"^\d+\.\d+")
        self.assertNotEqual(version, "0.0.0")

    def test_version_matches_pyproject_toml(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{build.read_version()}"', text)


class FindIsccTests(unittest.TestCase):
    def test_returns_existing_path_or_none(self):
        iscc = build.find_iscc()
        if iscc is not None:
            self.assertTrue(iscc.is_file())
            self.assertEqual(iscc.name.lower(), "iscc.exe")

    def test_env_override_wins(self):
        """环境变量 ISCC 指向真实文件时应被优先采用。"""
        import os

        existing = build.find_iscc()
        if existing is None:
            self.skipTest("本机未安装 Inno Setup")
        old = os.environ.get("ISCC")
        os.environ["ISCC"] = str(existing)
        try:
            self.assertEqual(build.find_iscc(), existing)
        finally:
            if old is None:
                os.environ.pop("ISCC", None)
            else:
                os.environ["ISCC"] = old

    def test_installer_script_exists(self):
        self.assertTrue(build.ISS.is_file(), f"缺少安装脚本: {build.ISS}")


if __name__ == "__main__":
    unittest.main()
