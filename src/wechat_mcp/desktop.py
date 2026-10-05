"""桌面应用：pywebview 原生窗口承载 DSH 风格 Web UI。

用法：

    .venv\\Scripts\\python.exe -m wechat_mcp.desktop

pywebview 6.x 不支持把 ``async def`` 暴露给 JS（其桥接无 asyncio 集成），
因此这里采用经典模式：一个后台线程跑专用 asyncio loop，承载适配层与 bot
引擎；暴露给 JS 的方法都是同步方法，内部通过
``run_coroutine_threadsafe(...).result()`` 等待结果。每次 JS 调用在 pywebview
的工作线程中执行，阻塞等待不影响界面。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import webview

from .adapters.deepseekgirl import DeepSeekGirlAdapter
from .bot import BotConfig, BotEngine
from .bot.llm import LLMClient, LLMError
from .config import AdapterConfig
from .unblock import unblock_bundled_assemblies


def webui_dir() -> Path:
    """定位前端静态目录：冻结包用 _MEIPASS/webui，源码用包内 webui。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "webui"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "webui"


class _AsyncHub:
    """后台线程 + 专用事件循环。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Any, timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


class DesktopApi:
    """暴露给前端的同步 API；内部转发到后台事件循环。"""

    # 各操作默认最长等待秒数（模型测试可能较慢）。
    DEFAULT_TIMEOUT = 120.0

    def __init__(self, hub: _AsyncHub) -> None:
        self._hub = hub
        self.adapter: DeepSeekGirlAdapter | None = None
        self.engine: BotEngine | None = None
        self.config: BotConfig | None = None
        self._ready = threading.Event()

    def _call(self, coro: Any, timeout: float = DEFAULT_TIMEOUT) -> Any:
        self._ready.wait(timeout=timeout)
        return self._hub.call(coro, timeout=timeout)

    # ------------------------------------------------------------------ 初始化

    async def _bootstrap_async(self) -> None:
        self.adapter = DeepSeekGirlAdapter(AdapterConfig.from_env())
        self.config = BotConfig.load()
        self.engine = BotEngine(self.adapter, self.config)
        # 连接失败不阻塞界面，用户可在状态页重连。
        await self.adapter.connect()
        if self.config.enabled:
            await self.engine.start()

    def bootstrap(self) -> None:
        try:
            self._hub.call(self._bootstrap_async(), timeout=90.0)
        finally:
            self._ready.set()

    # ------------------------------------------------------------------ 状态

    async def _get_state_async(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "engine": self.engine.status_snapshot(),
        }

    def get_state(self) -> dict:
        return self._call(self._get_state_async())

    # ------------------------------------------------------------------ 保存

    async def _save_config_async(self, new: BotConfig) -> dict:
        if new.enabled and not self.engine.running:
            await self.engine.start()
        elif not new.enabled and self.engine.running:
            await self.engine.stop()
        # 就地更新共享对象：引擎与本 API 持有同一引用，立即生效。
        self.config.__dict__.update(new.to_dict())
        self.config.save()
        return await self._get_state_async()

    def save_config(self, data: dict) -> dict:
        return self._call(self._save_config_async(BotConfig.from_dict(data)))

    # ------------------------------------------------------------------ 连接

    async def _connect_async(self) -> dict:
        result = await self.adapter.connect()
        return result.to_dict()

    def connect_wechat(self) -> dict:
        return self._call(self._connect_async(), timeout=60.0)

    # ------------------------------------------------------------------ 日志

    def get_logs(self, after_seq: int = 0) -> list[dict]:
        return self._call(self._get_logs_async(int(after_seq or 0)))

    async def _get_logs_async(self, after_seq: int) -> list[dict]:
        return self.engine.get_logs(after_seq)

    # ------------------------------------------------------------------ 模型测试

    async def _test_connection_async(self, data: dict) -> dict:
        cfg = BotConfig.from_dict(data)
        try:
            reply = await LLMClient(cfg).chat(
                [{"role": "user", "content": "用一句简短的话打个招呼"}]
            )
            return {"ok": True, "reply": reply}
        except LLMError as exc:
            return {"ok": False, "error": exc.to_dict()}

    def test_connection(self, data: dict) -> dict:
        return self._call(self._test_connection_async(data), timeout=90.0)

    # ------------------------------------------------------------------ 人设

    async def _reset_persona_async(self) -> str:
        self.config.persona_custom = ""
        self.config.save()
        return ""

    def reset_persona(self) -> str:
        return self._call(self._reset_persona_async())


def _show_fatal(message: str) -> None:
    """界面起不来时弹一个错误框。

    双击启动没有控制台，一旦 ``webview.start()`` 抛异常，用户只会看到
    黑屏/闪退，毫无线索。这里把原因显示出来。
    """
    print(message, file=sys.stderr)
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, "灵语 启动失败", 0x10
        )
    except Exception:  # noqa: BLE001 - 弹框失败不能再掩盖原始异常
        pass


def run() -> None:
    index = webui_dir() / "index.html"
    if not index.is_file():
        raise SystemExit(f"缺少前端入口: {index}")

    # 从 zip 解压的冻结包会被打上「Internet 区域」标记，.NET 拒绝加载
    # 带标记的程序集 → pythonnet 起不来 → 黑屏。这里先自行清除。
    unblocked = unblock_bundled_assemblies()

    hub = _AsyncHub()
    hub.start()
    api = DesktopApi(hub)

    webview.create_window(
        "灵语",
        url=index.as_uri(),
        js_api=api,
        width=1180,
        height=780,
        min_size=(960, 640),
        background_color="#ffffff",
    )

    # 后台初始化（连接微信可能耗时），完成后放行其它 API。
    threading.Thread(target=api.bootstrap, daemon=True).start()

    if unblocked:
        print(f"已清除 {unblocked} 个内置程序集的 Internet 区域标记。")

    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001 - 兜底成可读提示，避免黑屏
        _show_fatal(
            "界面启动失败："
            f"{exc}\n\n"
            "若提示无法加载 Python.Runtime.dll，通常是解压时文件被标记为"
            "「来自 Internet」。请右键压缩包 → 属性 → 勾选「解除锁定」，"
            "然后重新解压。"
        )
        raise


if __name__ == "__main__":
    run()
