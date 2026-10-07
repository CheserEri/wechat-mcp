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
import contextvars
import io
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

import webview

from .adapters.deepseekgirl import DeepSeekGirlAdapter
from .bot import BotConfig, BotEngine, default_config_path
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


def window_icon() -> Path | None:
    """定位窗口图标：冻结包用 _MEIPASS/wechat-mcp.ico，源码用 packaging/wechat-mcp.ico。

    必须显式传给 ``webview.start(icon=...)``：不传时 pywebview 会从
    ``sys.executable`` 提取图标，而源码方式运行时那里是 ``python.exe``，
    窗口标题栏与任务栏就会显示 Python 图标。
    """
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / "wechat-mcp.ico")
    candidates.append(
        Path(__file__).resolve().parents[2] / "packaging" / "wechat-mcp.ico"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


#: 标记「当前这条日志正从界面 sink 回流进引擎」。
#:
#: 为什么需要：loguru 的界面 sink 会把输出送进 ``UI_LOG.emit`` → ``engine.log``
#: → ``on_log`` → ``forward_engine_log`` → 又交回 loguru。界面 sink 自己能用
#: record 上的 ``ENGINE_LOG_MARK`` 过滤掉回流，但**文件 sink 不认这个标记**，
#: 于是同一条桥接层日志会落盘两次（一次原样、一次带 ``[引擎]`` 前缀）。
#:
#: 这里在调用 ``engine.log`` 期间置位，``forward_engine_log`` 见到置位就直接返回。
#: 界面 sink 是同步调用（没有 ``enqueue=True``），所以 contextvar 能可靠地跨越
#: 这条调用链——即便日志来自别的线程，置位与检查也在同一调用栈里完成。
_ENGINE_FORWARDING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wechat_mcp_engine_forwarding", default=False
)


class _UiLogSink:
    """把控制台输出并入界面「运行日志」。

    exe 是控制台程序（MCP stdio 模式要靠 stdout 通信），打包后的桌面端启动时会
    附带一个黑色命令行窗口。启动时把它隐藏掉，原本打在里面的内容（loguru 日志、
    ``print``、异常回溯）改由这里转投到引擎的日志缓冲，界面照常轮询显示。

    引擎在后台线程里延迟创建，此前的输出先暂存，引擎就绪后补发。
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        self._engine: BotEngine | None = None
        self._pending: deque[tuple[str, str]] = deque(maxlen=maxlen)

    def attach(self, engine: BotEngine) -> None:
        with self._lock:
            self._engine = engine
            pending, self._pending = self._pending, deque(maxlen=200)
        for level, line in pending:
            self._feed(engine, level, line)

    def emit(self, level: str, message: str) -> None:
        line = str(message).strip()
        if not line:
            return
        with self._lock:
            engine = self._engine
            if engine is None:
                self._pending.append((level, line))
                return
        self._feed(engine, level, line)

    @staticmethod
    def _feed(engine: BotEngine, level: str, line: str) -> None:
        """把一条日志交给引擎，并在此期间标记「正在回流」。

        置位是为了让 ``forward_engine_log`` 认出这条日志本来就来自 loguru，
        不要再转发一次（否则桥接层日志会在文件里出现两遍）。
        """
        token = _ENGINE_FORWARDING.set(True)
        try:
            engine.log(level, line)
        finally:
            _ENGINE_FORWARDING.reset(token)


# 全局唯一：loguru sink、stdout/stderr 重定向与桌面 API 共用同一个收集器。
UI_LOG = _UiLogSink()

# loguru 级别名 → 界面日志级别（对应 style.css 的 .log-* 配色）。
_LOGURU_LEVELS = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "SUCCESS": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "error",
}

# 引擎日志级别 → loguru 级别名（方向与上面相反）。
_ENGINE_LEVELS = {
    "debug": "DEBUG",
    "info": "INFO",
    "success": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
}

#: 转发到 loguru 的引擎日志会带上这个标记，界面 sink 据此跳过（避免重复 + 递归）。
ENGINE_LOG_MARK = "_engine_forwarded"


def forward_engine_log(line: Any) -> None:
    """把引擎自己的运行日志转发给 loguru，让它们也能**落盘**。

    为什么需要：``UI_LOG.attach(engine)`` 只把引擎日志塞进**内存**里的界面日志
    （界面靠轮询 ``get_logs()`` 显示），而文件 sink 挂在 loguru 上。于是日志文件
    里只有桥接层/适配层的话——引擎自己的判断（「已发送链接提示」「开始下载链接
    内容」「下载失败或超出体积上限，已跳过」…）**一条都没有**。现象就是
    「链接发了、提示回了、然后什么都没有」时，翻日志文件**查不出任何原因**。

    必须打标记：``UI_LOG.emit`` 最终会调 ``engine._log``，若不过滤就会
    「引擎日志 → loguru → 界面 sink → engine._log → …」无限递归。

    另外要拦住**回流**：桥接层用 loguru 打的日志会经界面 sink 回到引擎，此时
    ``_ENGINE_FORWARDING`` 已置位——那条日志本来就在 loguru 手里（文件 sink 已经
    写过一次），再转发就会在文件里出现两遍（一遍原样、一遍带 ``[引擎]`` 前缀）。
    """
    if _ENGINE_FORWARDING.get():
        return
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时静默跳过
        return
    level = _ENGINE_LEVELS.get(str(getattr(line, "level", "info")).lower(), "INFO")
    try:
        logger.bind(**{ENGINE_LOG_MARK: True}).log(
            level, f"[引擎] {getattr(line, 'message', line)}"
        )
    except Exception:  # noqa: BLE001 - 写日志失败不该影响运行
        pass


def _guess_level(text: str) -> str:
    """从裸文本（print / 回溯）里猜一个日志级别。"""
    upper = text.upper()
    if "| ERROR" in upper or "ERROR:" in upper or "TRACEBACK" in upper:
        return "error"
    if "| WARNING" in upper or "WARN" in upper:
        return "warning"
    if "| DEBUG" in upper:
        return "debug"
    return "info"


class _TeeStream(io.TextIOBase):
    """写入时同时转给原控制台与界面运行日志的流。"""

    def __init__(self, original: Any) -> None:
        self._original = original
        self._buffer = ""

    def write(self, text: str) -> int:
        if self._original is not None:
            try:
                written = self._original.write(text)
            except Exception:  # noqa: BLE001 - 原控制台不可写时以本次写入为准
                written = len(text)
        else:
            written = len(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            text_line = line.rstrip("\r")
            if text_line.strip():
                UI_LOG.emit(_guess_level(text_line), text_line)
        return written

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:  # noqa: BLE001 - 冲刷失败无需上报
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._original is None:
            raise OSError("没有可用的文件描述符")
        return self._original.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", None) or "utf-8"

    def __getattr__(self, name: str) -> Any:
        # 未覆盖的属性（如 .buffer）回落到原控制台流；下划线名字直接拒绝，
        # 避免 _original 尚未赋值时无限递归。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._original, name)


def log_dir() -> Path:
    """运行日志目录：``%APPDATA%\\wechat-mcp\\logs``。"""
    return default_config_path().parent / "logs"


def install_file_logging(level: str = "INFO", retention_days: int = 7) -> Path | None:
    """给 loguru 再挂一个**文件** sink，让运行日志能落盘回溯。

    界面上的「运行日志」只在内存里，进程一退出就没了——出问题（比如「链接发了、
    提示也回了、然后什么都没有」）时完全没有线索，只能靠猜。这里把同样的日志
    按天写进 ``logs/app-YYYY-MM-DD.log``，保留最近若干天。

    返回日志文件路径；挂载失败返回 ``None``（不能因为日志影响程序启动）。
    """
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时静默跳过
        return None
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(directory / "app-{time:YYYY-MM-DD}.log"),
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
            level=level,
            encoding="utf-8",
            enqueue=True,  # 跨线程写日志，串行化到队列，避免多线程交叉
            rotation="00:00",
            retention=f"{max(1, int(retention_days))} days",
            backtrace=False,
            diagnose=False,
        )
    except Exception:  # noqa: BLE001 - 落盘失败不影响界面运行
        return None
    return directory


def install_console_capture() -> None:
    """接管控制台输出，让桌面端不再需要那个黑色命令行窗口。

    loguru 的默认 sink 在它自己被导入时就绑定了当时的 ``sys.stderr``，之后再替换
    ``sys.stderr`` 也收不到它的输出，因此这里另外挂一个 sink。
    """
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时只接管 print/回溯
        logger = None
    if logger is not None:
        try:
            logger.add(
                lambda message: UI_LOG.emit(
                    _LOGURU_LEVELS.get(message.record["level"].name, "info"),
                    message,
                ),
                format="{message}",
                colorize=False,
                level="INFO",
                # 引擎日志已经由 UI_LOG.attach 进过界面了，转发到 loguru 只是为了让
                # 它们落盘；这里跳过，既避免界面重复显示，也避免
                # 「引擎日志 → loguru → 界面 sink → engine._log」无限递归。
                filter=lambda record: not record["extra"].get(ENGINE_LOG_MARK),
            )
        except Exception:  # noqa: BLE001 - 挂载失败不影响界面运行
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and not isinstance(stream, _TeeStream):
            setattr(sys, name, _TeeStream(stream))


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
        # 引擎就绪后接管控制台输出，连接微信期间的日志即可直接显示到界面。
        UI_LOG.attach(self.engine)
        # 界面那份只在内存里（进程一退就没了），再转发一份给 loguru 才能落盘。
        self.engine.on_log = forward_engine_log
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
    # 先把控制台输出接管到界面运行日志，再启动窗口；这样启动期的日志也不会丢。
    install_console_capture()
    # 同一份日志再落盘一份：界面日志进程一退就没了，出问题时无从回溯。
    install_file_logging()

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

    icon = window_icon()
    try:
        webview.start(icon=str(icon) if icon else None)
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
