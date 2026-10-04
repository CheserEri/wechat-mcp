"""安全校验与目标消歧的单元测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mcp.errors import (  # noqa: E402
    FilePathInvalidError,
    FilePathNotAllowedError,
    TargetInvalidError,
)
from wechat_mcp.security import (  # noqa: E402
    resolve_target,
    validate_message,
    validate_recipient,
    validate_send_path,
)


class ValidateRecipientTests(unittest.TestCase):
    def test_rejects_empty_and_whitespace(self):
        for value in (None, "", "   "):
            with self.assertRaises(TargetInvalidError):
                validate_recipient(value)

    def test_rejects_control_characters(self):
        with self.assertRaises(TargetInvalidError):
            validate_recipient("张三\n李四")

    def test_rejects_overlong_name(self):
        with self.assertRaises(TargetInvalidError):
            validate_recipient("a" * 200)

    def test_strips_valid_name(self):
        self.assertEqual(validate_recipient("  测试群  "), "测试群")


class ValidateMessageTests(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(TargetInvalidError):
            validate_message("   ")

    def test_accepts_normal_text(self):
        self.assertEqual(validate_message("你好"), "你好")


class ResolveTargetTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(resolve_target("测试群", ["测试群", "其它"]), ("测试群", []))

    def test_unique_partial_match(self):
        self.assertEqual(resolve_target("测试", ["测试群", "其它"]), ("测试群", []))

    def test_ambiguous_partial_match_returns_candidates(self):
        resolved, candidates = resolve_target("项目", ["项目A组", "项目B组"])
        self.assertEqual(resolved, "")
        self.assertEqual(candidates, ["项目A组", "项目B组"])

    def test_duplicate_exact_name_is_ambiguous(self):
        resolved, candidates = resolve_target("老王", ["老王", "老王"])
        self.assertEqual(resolved, "")
        self.assertEqual(candidates, ["老王"])

    def test_unknown_target_passes_through(self):
        self.assertEqual(resolve_target("陌生联系人", ["测试群"]), ("陌生联系人", []))


class ValidateSendPathTests(unittest.TestCase):
    def test_rejects_empty_path(self):
        for value in (None, "", "   "):
            with self.assertRaises(FilePathInvalidError):
                validate_send_path(value, [])

    def test_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FilePathInvalidError):
                validate_send_path(Path(tmp) / "no.txt", [tmp])

    def test_rejects_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FilePathInvalidError):
                validate_send_path(tmp, [tmp])

    def test_rejects_when_no_whitelist_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            with self.assertRaises(FilePathNotAllowedError):
                validate_send_path(target, [])

    def test_rejects_outside_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("hi", encoding="utf-8")
            with self.assertRaises(FilePathNotAllowedError):
                validate_send_path(outside, [allowed])

    def test_allows_file_inside_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            target = allowed / "a.txt"
            target.write_text("hi", encoding="utf-8")
            resolved = validate_send_path(target, [allowed])
            self.assertEqual(resolved, target.resolve())

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            secret = Path(tmp) / "secret.txt"
            secret.write_text("hi", encoding="utf-8")
            # 用 .. 试图穿越出白名单目录
            traversal = allowed / ".." / "secret.txt"
            with self.assertRaises(FilePathNotAllowedError):
                validate_send_path(traversal, [allowed])


if __name__ == "__main__":
    unittest.main()
