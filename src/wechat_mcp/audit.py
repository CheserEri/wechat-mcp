"""操作审计。

只记录必要的操作元数据（工具名、目标、时间、执行结果与错误码），
默认不记录完整聊天正文。采用 JSONL 追加写入，便于后续检索。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """线程安全的 JSONL 审计记录器。"""

    def __init__(self, path: Path | str, enabled: bool = True):
        self._path = Path(path)
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        if self._enabled:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # 目录不可写则关闭审计，不影响主流程
                logger.warning("无法创建审计日志目录，已停用审计: %s", exc)
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        tool: str,
        *,
        action: str = "",
        target: str = "",
        ok: bool | None = None,
        status: str = "",
        error_code: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """追加一条审计记录。写入失败只告警，不抛出。"""
        if not self._enabled:
            return
        entry: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "tool": str(tool),
            "action": str(action),
            "target": str(target),
            "ok": ok,
            "status": str(status),
            "error_code": str(error_code),
        }
        if extra:
            entry["extra"] = extra
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                logger.warning("写入审计日志失败: %s", exc)