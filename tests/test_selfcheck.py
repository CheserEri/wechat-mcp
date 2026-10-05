"""运行环境自检的单元测试。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from wechat_mcp.selfcheck import collect, resolve, tweet_card


class SelfCheckTests(unittest.TestCase):
    def test_collect_returns_expected_keys(self):
        info = collect()
        for key in (
            "version",
            "frozen",
            "python",
            "yt_dlp_version",
            "ffmpeg",
            "bridge",
            "webui",
        ):
            self.assertIn(key, info)

    def test_bundled_yt_dlp_is_importable_in_source_mode(self):
        """仓库内置的 vendor/yt_dlp 应可导入（否则链接解析整条链路失效）。"""
        info = collect()
        self.assertNotEqual(info["yt_dlp_version"], "(不可用)")

    def test_webui_present_in_source_mode(self):
        self.assertNotEqual(collect()["webui"], "(缺失)")


class ResolveCliTests(unittest.TestCase):
    """``--resolve`` 诊断命令（不触网，靠替换 LinkResolver.resolve）。"""

    def _run_with(self, fake) -> tuple[int, str]:
        from wechat_mcp.bot import links

        original = links.LinkResolver.resolve
        links.LinkResolver.resolve = fake
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = resolve("https://example.com/x")
        finally:
            links.LinkResolver.resolve = original
        return code, buffer.getvalue()

    def test_empty_url_is_usage_error(self):
        self.assertEqual(resolve(""), 2)

    def test_success_prints_summary_and_returns_zero(self):
        from wechat_mcp.bot import links

        async def _fake(self, url):
            return links.LinkInfo(url=url, ok=True, title="示例标题", extractor="Example")

        code, out = self._run_with(_fake)
        self.assertEqual(code, 0)
        self.assertIn("ok: True", out)
        self.assertIn("示例标题", out)
        self.assertIn("Example", out)

    def test_failure_returns_one_and_prints_error(self):
        from wechat_mcp.bot import links

        async def _fake(self, url):
            return links.LinkInfo(url=url, ok=False, error="网络不可达")

        code, out = self._run_with(_fake)
        self.assertEqual(code, 1)
        self.assertIn("网络不可达", out)


class TweetCardCliTests(unittest.TestCase):
    """``--tweet`` 诊断命令（不触网，靠替换 tweet 模块的函数）。"""

    def test_empty_url_is_usage_error(self):
        self.assertEqual(tweet_card(""), 2)

    def test_non_tweet_url_is_usage_error(self):
        self.assertEqual(tweet_card("https://example.com/x"), 2)

    def test_render_failure_returns_one(self):
        from wechat_mcp.bot import tweet as tweet_mod

        original = (tweet_mod.fetch_tweet, tweet_mod.render_card)
        tweet_mod.fetch_tweet = lambda *a, **k: tweet_mod.Tweet(
            id="1", url="https://x.com/a/status/1", text="hi"
        )
        tweet_mod.render_card = lambda *a, **k: None
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = tweet_card("https://x.com/a/status/1")
        finally:
            tweet_mod.fetch_tweet, tweet_mod.render_card = original
        self.assertEqual(code, 1)
        self.assertIn("渲染失败", buffer.getvalue())

    def test_success_returns_zero_and_prints_path(self):
        from wechat_mcp.bot import tweet as tweet_mod

        original = (tweet_mod.fetch_tweet, tweet_mod.render_card)
        tweet_mod.fetch_tweet = lambda *a, **k: tweet_mod.Tweet(
            id="1",
            url="https://x.com/a/status/1",
            text="正文",
            author_name="作者",
            author_handle="demo",
            has_video=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.png"
            card.write_bytes(b"png")
            tweet_mod.render_card = lambda *a, **k: card
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    code = tweet_card("https://x.com/a/status/1", outdir=tmp)
            finally:
                tweet_mod.fetch_tweet, tweet_mod.render_card = original
        self.assertEqual(code, 0)
        out = buffer.getvalue()
        self.assertIn("has_video: True", out)
        self.assertIn("card.png", out)


if __name__ == "__main__":
    unittest.main()
