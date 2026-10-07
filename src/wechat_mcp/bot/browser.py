"""极简浏览器桥：用本机 Chrome / Edge + CDP 拿 JS 渲染后的数据。

为什么需要它
------------
抖音的 Web 接口要求 ``a_bogus`` 签名，而签名算法藏在 JSVMP 混淆的
``webmssdk`` 里，且会随版本轮换（见 ``douyin.py`` 的说明）。与其反复逆向，
不如直接借本机浏览器的 JS 环境跑一遍页面——页面自己会把请求签好名。

实现上只依赖标准库：自己实现 RFC 6455 的 WebSocket 客户端（CDP 只用到
文本帧、分片、ping/pong 这几件事），**不引入 websocket-client 等新依赖**，
打包时不用额外收集。

安全性：浏览器用**独立的临时用户目录**启动，不碰用户既有的浏览器配置与
登录态；退出时关进程并删除临时目录。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_LOGGER = Callable[[str, str], None]

# --------------------------------------------------------------------------- #
# 浏览器定位
# --------------------------------------------------------------------------- #

# 优先 Edge：Windows 10/11 自带，用户不必额外装。
_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_WINDOWS_LOCAL_CANDIDATES = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str:
    """返回可用的 Chromium 系浏览器可执行文件路径；找不到返回空串。"""
    if sys.platform != "win32":
        return ""
    for candidate in _WINDOWS_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        for relative in _WINDOWS_LOCAL_CANDIDATES:
            candidate = Path(local) / relative
            if candidate.is_file():
                return str(candidate)
    return ""


def browser_available() -> bool:
    return bool(find_browser())


# --------------------------------------------------------------------------- #
# 极简 WebSocket（RFC 6455，客户端）
# --------------------------------------------------------------------------- #


class _WebSocket:
    """只实现 CDP 需要的部分：文本帧、分片重组、ping/pong、关闭。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        if not url.startswith("ws://"):
            raise ValueError(f"不支持的 WebSocket 地址：{url}")
        rest = url[5:]
        host_port, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_text = host_port.partition(":")
        port = int(port_text or 80)
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise OSError(f"WebSocket 握手失败：{status_line}")

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("连接被关闭")
            data += chunk
        return data

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise OSError("连接被关闭")
            data += chunk
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        """读取一条完整文本消息（自动处理分片与 ping）。"""
        buffer = b""
        while True:
            first, second = self._read_exact(2)
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b""
            if opcode == 0x9:  # ping → 回 pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise OSError("WebSocket 被对端关闭")
            buffer += payload
            if fin:
                return buffer.decode("utf-8", "replace")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        header.append(0x80 | len(payload))
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    def settimeout(self, seconds: float) -> None:
        try:
            self._sock.settimeout(max(0.05, seconds))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 浏览器会话
# --------------------------------------------------------------------------- #

_LAUNCH_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
    "--window-size=1280,900",
    "--remote-allow-origins=*",
)

#: 启动失败后最多尝试几次（含首次）。Chromium 系偶发「秒退」——比如 Edge 后台
#: 正在做版本切换，新起的进程会把命令行交接给新版本然后自己退出（实测 2.86 秒就退、
#: 系统事件日志里连崩溃记录都没有）。这类失败重试一次基本就好，不必让整条链接
#: 解析白跑。
_START_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.0
#: 退出诊断里回显 stderr 的最大字符数。
_STDERR_TAIL_CHARS = 600
#: 浏览器 stderr 的落盘文件名。放在临时 profile 目录里，随目录一起被清掉。
_STDERR_NAME = "_wechat-mcp-stderr.log"


class BrowserExitedError(RuntimeError):
    """浏览器进程在调试端口就绪前就退出了。

    单列一种异常，是为了和「端口迟迟不就绪」区分开：前者**可重试**（见
    :data:`_START_ATTEMPTS`），后者重试只是白等——所以 :meth:`BrowserSession.start`
    只对这一个异常做重试。
    """


class BrowserSession:
    """启动一个临时浏览器实例，并提供一个页面用于取数据。

    用法::

        with BrowserSession() as page:
            page.navigate("https://www.douyin.com/video/123")
            data = page.wait_for_json("aweme/detail", timeout=20)
    """

    def __init__(
        self,
        logger: _LOGGER | None = None,
        timeout: float = 30.0,
        port: int = 0,
    ) -> None:
        self._log = logger or (lambda level, message: None)
        self._timeout = float(timeout)
        self._fixed_port = bool(port)
        self._port = port or _free_port()
        self._process: subprocess.Popen | None = None
        self._profile = ""
        self._stderr_path = ""
        self._ws: _WebSocket | None = None
        self._message_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, Any]] = {}

    # -- 生命周期 --------------------------------------------------------- #

    def start(self) -> "BrowserSession":
        exe = find_browser()
        if not exe:
            raise RuntimeError(
                "未找到 Chrome / Edge，无法用浏览器方式解析（可安装 Edge 后重试）"
            )
        attempts = max(1, _START_ATTEMPTS)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._start_once(exe)
            except BrowserExitedError as exc:
                # 秒退是可重试的：收干净再起一次。
                last_error = exc
                self.close()
                if attempt >= attempts:
                    break
                self._log(
                    "warning",
                    f"浏览器启动后立刻退出（第 {attempt}/{attempts} 次）：{exc}；"
                    f"{_RETRY_DELAY_SECONDS:.0f} 秒后重试",
                )
                time.sleep(_RETRY_DELAY_SECONDS)
            except Exception:
                # 其它失败（找不到浏览器、端口迟迟不就绪）重试只是白等。
                self.close()
                raise
        if last_error is None:  # pragma: no cover - attempts >= 1 时走不到
            last_error = RuntimeError("浏览器启动失败")
        raise last_error

    def _start_once(self, exe: str) -> "BrowserSession":
        """起一次浏览器；是否重试由 :meth:`start` 决定。"""
        _sweep_once()
        if not self._fixed_port:
            # 上一次尝试可能留下还没完全释放的调试端口，重试时换一个。
            self._port = _free_port()
        # 目录名带上自己的 PID，方便下次启动识别「哪些是残留」。
        self._profile = tempfile.mkdtemp(
            prefix=f"{_PROFILE_PREFIX}{os.getpid()}-"
        )
        args = [
            exe,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile}",
            *_LAUNCH_FLAGS,
            "about:blank",
        ]
        creation = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        sink = self._open_stderr_sink()
        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=sink,
                creationflags=creation,
            )
        finally:
            if sink is not None:
                # 子进程已经继承了这份句柄，自己这份可以关掉：既不留多余句柄，
                # 也让 profile 目录能被正常删除。
                try:
                    sink.close()
                except OSError:  # pragma: no cover - 关不掉也不影响主流程
                    pass
        self._ws = self._connect_page()
        self._call("Network.enable")
        self._call("Page.enable")
        return self

    # -- 启动失败的诊断 --------------------------------------------------- #

    def _open_stderr_sink(self):
        """打开浏览器 stderr 的落盘文件，返回可交给 ``Popen`` 的句柄。

        原来 stderr 直接丢进 ``DEVNULL``，浏览器秒退时**退出码和原因全部丢弃**，
        只能靠时间差和系统事件日志反推（见 2026-10-07 那次排查）。改成落文件，
        失败时把尾部回显进日志。文件放在临时 profile 目录里，随目录一起被清掉。
        """
        self._stderr_path = ""
        if not self._profile:
            return None
        path = os.path.join(self._profile, _STDERR_NAME)
        try:
            handle = open(path, "wb")
        except OSError:  # pragma: no cover - 极少数权限/占用情况
            return None
        self._stderr_path = path
        return handle

    def _read_stderr_tail(self) -> str:
        """读回 stderr 落盘文件的尾部；读不到返回空串。"""
        if not self._stderr_path:
            return ""
        try:
            with open(self._stderr_path, "rb") as handle:
                blob = handle.read()
        except OSError:
            return ""
        text = " ".join(blob.decode("utf-8", "replace").split())
        if len(text) > _STDERR_TAIL_CHARS:
            text = "…" + text[-_STDERR_TAIL_CHARS:]
        return text

    def _exit_diagnosis(self) -> str:
        """浏览器提前退出时，拼一条能直接定位原因的说明。"""
        code = self._process.poll() if self._process is not None else None
        text = f"浏览器进程已退出（退出码 {code}）"
        tail = self._read_stderr_tail()
        if tail:
            return f"{text}；stderr：{tail}"
        return (
            f"{text}；stderr 为空。Chromium 系在版本切换/更新待生效时会静默交接给"
            "新进程后自己退出，重试通常可恢复"
        )

    def _debugger_url(self, kind: str) -> str:
        url = f"http://127.0.0.1:{self._port}/json/{kind}"
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.read().decode("utf-8", "replace")

    def _connect_page(self) -> _WebSocket:
        deadline = time.time() + self._timeout
        last_error = ""
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                raise BrowserExitedError(self._exit_diagnosis())
            try:
                targets = json.loads(self._debugger_url("list"))
                chosen = _pick_page_target(targets)
                if chosen is not None:
                    return _WebSocket(
                        chosen["webSocketDebuggerUrl"], timeout=self._timeout
                    )
            except Exception as exc:  # 端口还没起来
                last_error = str(exc)
            time.sleep(0.3)
        raise RuntimeError(f"浏览器调试端口未就绪：{last_error}")

    def close(self, blocking: bool = True) -> None:
        """关闭会话。

        ``blocking=False`` 时把「杀进程 + 删临时目录」丢到后台线程——实测这步
        要十几秒（Chromium 进程树退出 + 删几百 MB 的 profile），不该挡住解析
        结果的返回。
        """
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        process, profile = self._process, self._profile
        self._process, self._profile = None, ""
        # stderr 落盘文件在 profile 目录里，交给 _reap 一起删；这里只断开引用，
        # 免得下一次启动的 _exit_diagnosis 读到上一个会话的旧文件。
        self._stderr_path = ""
        if blocking:
            _reap(process, profile)
        else:
            threading.Thread(
                target=_reap, args=(process, profile), daemon=True
            ).start()

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- CDP ------------------------------------------------------------- #

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("浏览器会话未启动")
        self._message_id += 1
        message_id = self._message_id
        self._ws.send_text(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        # 页面会持续抛出 CDP 事件（网络、日志…），若只靠 socket 超时，
        # 只要事件不断就不会触发——这里用绝对 deadline 兜住。
        deadline = time.time() + (timeout if timeout else self._timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError(f"CDP {method} 超时（{deadline:.0f}）")
            self._ws.settimeout(remaining)
            message = json.loads(self._ws.recv_text())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} 失败：{message['error']}")
                return message.get("result") or {}
            self._observe(message)

    def _observe(self, message: dict[str, Any]) -> None:
        """记录网络事件，供 :meth:`wait_for_json` 使用。"""
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Network.responseReceived":
            request_id = params.get("requestId")
            if request_id:
                self._responses[request_id] = {
                    "url": (params.get("response") or {}).get("url", ""),
                    "status": (params.get("response") or {}).get("status"),
                }

    def navigate(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})

    def wait_for(
        self, condition: str, timeout: float = 20.0, interval: float = 0.2
    ) -> bool:
        """轮询一个返回真值的 JS 表达式，直到为真或超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(f"!!({condition})"):
                    return True
            except Exception:
                pass  # 导航中会报「Inspected target navigated or closed」
            time.sleep(interval)
        return False

    def wait_ready(self, timeout: float = 20.0) -> bool:
        """等页面加载完（``document.readyState === 'complete'``）。

        注意：``complete`` 要等全部资源（含视频预加载），在视频页上可能很久，
        能接受「页面已落到目标域」时优先用 :meth:`wait_for`。
        """
        return self.wait_for("document.readyState === 'complete'", timeout)

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        """在页面里执行 JS 并返回结果值。"""
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "timeout": int(self._timeout * 1000),
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(
                f"页面脚本出错：{result['exceptionDetails'].get('text')}"
            )
        return (result.get("result") or {}).get("value")

    # 页面内 XHR：**必须用 XMLHttpRequest 而不是 fetch**。站点的安全 SDK 会
    # hook XHR 的 open/send 自动补签名，fetch 绕过它（实测 fetch 直接被风控拒）。
    _XHR_TEMPLATE = (
        "new Promise((resolve) => {"
        "  const x = new XMLHttpRequest();"
        "  x.open('GET', %s, true);"
        "  x.timeout = %d;"
        "  x.onreadystatechange = () => {"
        "    if (x.readyState === 4) {"
        "      resolve(x.status + '\\u0001' + (x.responseText || ''));"
        "    }"
        "  };"
        "  x.onerror = () => resolve('-1\\u0001');"
        "  x.ontimeout = () => resolve('-2\\u0001');"
        "  x.send();"
        "})"
    )

    def xhr_get(self, path: str, timeout_ms: int = 15000) -> tuple[int, str]:
        """在页面里发一个同源 GET，返回 ``(状态码, 响应文本)``。

        ``path`` 用站内相对路径（如 ``/aweme/v1/web/aweme/detail/?…``），
        这样请求与页面同源，安全 SDK 才会给它签名。
        """
        expression = self._XHR_TEMPLATE % (json.dumps(path), int(timeout_ms))
        raw = self.evaluate(expression, await_promise=True)
        status_text, _, body = str(raw or "").partition("\u0001")
        try:
            return int(status_text), body
        except ValueError:
            return 0, body

    def wait_for_json(
        self, url_keyword: str, timeout: float = 20.0
    ) -> dict[str, Any] | None:
        """等一个 URL 含 ``url_keyword`` 的响应，返回其 JSON 正文。"""
        if self._ws is None:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = json.loads(self._ws.recv_text())
            except Exception:
                break
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Network.responseReceived":
                self._observe(message)
                url = (params.get("response") or {}).get("url", "")
                if url_keyword in url and (params.get("response") or {}).get("status") == 200:
                    body = self._response_body(params.get("requestId"))
                    if body:
                        try:
                            return json.loads(body)
                        except json.JSONDecodeError:
                            return None
            elif method == "Network.loadingFailed":
                continue
        return None

    def _response_body(self, request_id: str | None) -> str:
        if not request_id:
            return ""
        try:
            result = self._call("Network.getResponseBody", {"requestId": request_id})
        except Exception:
            return ""
        body = result.get("body") or ""
        if result.get("base64Encoded"):
            try:
                return base64.b64decode(body).decode("utf-8", "replace")
            except Exception:
                return ""
        return body


_PROFILE_PREFIX = "wechat-mcp-browser-"
# 目录名里带创建者的 PID（``wechat-mcp-browser-<pid>-xxxx``）。正常关闭时会自己
# 删掉临时目录，只有进程被强杀 / 短命脚本来不及跑完异步清理时才会残留，
# 这里兜底清扫。
_PROFILE_STALE_SECONDS = 1800.0
# 本进程自己留下的临时目录不能用「PID 还活着」来判断——那个 PID 就是自己，
# 永远活着，于是每次解析漏下的浏览器越堆越多（实测 78 分钟还挂着 4 个）。
# 对自家目录改用 mtime：解析最长也就几十秒，超过这个时间还没被动过就是残留。
_OWN_PROFILE_STALE_SECONDS = 600.0
# 清扫是「周期性」而非「每进程一次」：进程活得久时残留会一直累积。
_SWEEP_INTERVAL_SECONDS = 600.0
_swept_at = 0.0

#: CDP 内部页前缀——这些页面不能作为导航目标。
_INTERNAL_PAGE_PREFIXES = ("edge://", "chrome://", "devtools://", "about:blank#")


def _pick_page_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从 CDP ``/json/list`` 里挑一个真正能导航的页面目标。

    Edge/Chrome 用全新用户目录启动时会**额外**开一个内部页
    （``edge://sync-confirmation-dialog/`` 之类），它在列表里排在命令行给的
    ``about:blank`` **前面**。原来直接取第一个 ``type == "page"`` 会连上这个
    内部页：导航落在它身上，而 ``about:blank`` 一直是空白——现象就是「浏览器
    起来了，但页面没进抖音」，解析白等一场（实测 09:35 那次就是这么卡的）。

    优先 ``about:blank``（我们显式指定的那个），其次任何非内部页。
    """
    pages = [
        target
        for target in targets
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None
    for page in pages:
        if str(page.get("url", "")).startswith("about:blank"):
            return page
    for page in pages:
        url = str(page.get("url", "")).lower()
        if url.startswith(_INTERNAL_PAGE_PREFIXES):
            continue
        return page
    return pages[0]


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（用来判断临时目录是残留还是仍在使用）。"""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 查不到就保守当成还活着，宁可不删
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _sweep_stale_profiles(max_age: float = _PROFILE_STALE_SECONDS) -> int:
    """删掉此前异常退出遗留的临时用户目录，返回删掉的数量。

    优先按目录名里的 PID 判断：PID 没了就是残留；PID 还在（同机另一个实例正在用）
    就跳过。解析不出 PID 的旧目录退回按 mtime 判断。
    """
    root = tempfile.gettempdir()
    now = time.time()
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not name.startswith(_PROFILE_PREFIX):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        owner = name[len(_PROFILE_PREFIX):].partition("-")[0]
        try:
            pid = int(owner)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            # 别的实例正在用，跳过。
            continue
        if pid == os.getpid():
            # 本进程自己留下的：PID 永远「活着」，只能靠 mtime 判断。
            try:
                if now - entry.stat().st_mtime < _OWN_PROFILE_STALE_SECONDS:
                    continue
            except OSError:
                continue
        elif not pid:
            # 解析不出 PID 的旧目录：退回按 mtime 判断。
            try:
                if now - entry.stat().st_mtime < max_age:
                    continue
            except OSError:
                continue
        # 其余情况（PID 已消失）就是异常退出的残留，直接收掉。
        # 先按 profile 把浏览器进程树收干净，再删目录：否则目录被占着删不掉，
        # 而且孤儿进程会一直吃内存（taskkill /T 只对还活着的根进程有效）。
        _kill_by_profile(entry.path)
        shutil.rmtree(entry.path, ignore_errors=True)
        if not Path(entry.path).exists():
            removed += 1
    return removed


def _sweep_once() -> None:
    """周期性清扫临时目录（默认最多每 10 分钟一次）。

    原来写的是「每个进程只扫一次」，但进程一开就是几小时，期间每次解析漏下的
    浏览器会一直累积（实测 78 分钟堆了 4 个无头 Edge）。改成按时间间隔节流。
    """
    global _swept_at
    now = time.time()
    if now - _swept_at < _SWEEP_INTERVAL_SECONDS:
        return
    _swept_at = now
    try:
        _sweep_stale_profiles()
    except Exception:
        pass


def _kill_by_profile(profile: str) -> int:
    """按 ``--user-data-dir`` 杀掉该临时目录下的**所有**浏览器进程。

    ``taskkill /T`` 只对**还活着的根进程**有效：Chromium 的根进程一旦先退出，
    剩下的 renderer/gpu/utility 就成孤儿，按 PID 再也找不到它们，会一直占着
    临时目录吃内存。按命令行里的 ``--user-data-dir`` 匹配才能收干净。
    返回杀掉的进程数；psutil 不可用时静默返回 0。
    """
    if not profile:
        return 0
    try:
        import psutil
    except Exception:
        return 0
    marker = f"--user-data-dir={profile}".lower()
    killed = 0
    try:
        processes = list(psutil.process_iter(["pid", "name"]))
    except Exception:
        return 0
    for proc in processes:
        try:
            name = str(proc.info.get("name") or "").lower()
            if "msedge" not in name and "chrome" not in name:
                continue
            cmdline = proc.cmdline()
        except Exception:
            continue
        if not any(marker in str(part).lower() for part in cmdline):
            continue
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    return killed


def _kill_tree(process: "subprocess.Popen | None") -> None:
    """连子进程一起杀掉。

    Chromium 会派生 renderer/gpu/utility 等一堆子进程，只 ``terminate`` 主进程
    会让它们变孤儿并继续占着临时用户目录，导致后面删目录反复失败。
    """
    if process is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except Exception:
            pass
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3)
        except Exception:
            pass


def _reap(process: "subprocess.Popen | None", profile: str) -> None:
    """杀进程并删掉临时用户目录（可放到后台线程执行）。"""
    _kill_tree(process)
    if not profile:
        return
    # 兜底：根进程可能已经先退出，剩下的 renderer/gpu 成孤儿，按 PID 杀不到，
    # 只能按 --user-data-dir 认领（否则目录被占着删不掉，进程也一直挂着）。
    _kill_by_profile(profile)
    for _ in range(6):
        shutil.rmtree(profile, ignore_errors=True)
        if not Path(profile).exists():
            return
        time.sleep(0.5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
