"""日志配置。

MCP stdio 传输使用 stdout 传输协议消息，因此所有日志必须写入 stderr，
避免污染协议流。
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """将日志输出到 stderr（stdio 传输下 stdout 仅用于协议消息）。"""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)