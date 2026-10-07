# =============================================================================
# 来源声明 / Source attribution
#
# 本文件是从上游项目 `deepseekgirl`（https://github.com/linxisama/deepseekgirl）
# 原样复制的第三方源码：
#   上游本地目录：F:\Code\deepseekgirl-main
#   上游文件路径：src/wechat_bridge.py
#   引入用途：wechat-mcp 复用其 WeChatBridge（微信 UI 自动化桥接），
#             供源码模式与 PyInstaller 冻结打包内置。
#
# 重要：上游项目**未声明任何开源许可证**（其仓库根目录不存在 LICENSE 文件）。
# 本仓库（wechat-mcp，MPL-2.0）以「如实标注来源」的方式收录该文件，
# 但标注来源并不等同于获得授权；如进行再分发，请自行确认上游授权情况。
# 详见仓库根目录 THIRD_PARTY_NOTICES.md。
#
# 除本声明注释块，以及为支持「引用/回复消息」检测而做的最小增量
# （WeChatMessage.reply_to_name 字段、WeChatBridge._extract_reply_target 解析及
# 构造处的一次调用，均为新增、不改变任何原有行为）外，本文件未做其他修改。
# =============================================================================

"""微信桥接模块：通过 wxauto (UI Automation) 连接微信桌面客户端"""

import asyncio
import ctypes
import html
import hashlib
import importlib.util
import multiprocessing
import os
import queue
import random
import re
import subprocess
import tempfile
import time
import threading
from ctypes import wintypes
from typing import Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

from loguru import logger


def _running_process_names() -> set[str]:
    """Return lower-case executable names without relying on tasklist.

    Some Windows sessions deny access to tasklist even though the current
    user can inspect processes through the Win32 API.  That made a healthy
    WeChat client look like it was not running after a restart.
    """
    names: set[str] = set()
    if os.name == "nt":
        try:
            TH32CS_SNAPPROCESS = 0x00000002
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot != INVALID_HANDLE_VALUE:
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                    has_entry = bool(
                        kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                    )
                    while has_entry:
                        names.add(entry.szExeFile.lower())
                        has_entry = bool(
                            kernel32.Process32NextW(
                                snapshot,
                                ctypes.byref(entry),
                            )
                        )
                finally:
                    kernel32.CloseHandle(snapshot)
        except Exception as e:
            logger.debug(f"通过 Win32 API 读取进程失败: {e}")

    if names:
        return names

    # Last-resort fallback for environments where the snapshot API is blocked.
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=5,
        )
        output = result.stdout.decode("utf-8", errors="ignore").lower()
        for line in output.splitlines():
            first = line.split(",", 1)[0].strip().strip('"')
            if first:
                names.add(first)
    except Exception as e:
        logger.debug(f"回退读取微信进程失败: {e}")
    return names


def _visible_window_process_ids(executable_names: set[str]) -> set[int]:
    """Find visible top-level window owners for the requested processes."""
    pids: set[int] = set()
    if os.name != "nt":
        return pids

    wanted = {name.lower() for name in executable_names}
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True

            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(process_id),
            )
            if not process_id.value:
                return True

            handle = kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                process_id.value,
            )
            if not handle:
                return True
            try:
                size = wintypes.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle,
                    0,
                    buffer,
                    ctypes.byref(size),
                ):
                    executable = os.path.basename(buffer.value).lower()
                    if executable in wanted:
                        pids.add(process_id.value)
            finally:
                kernel32.CloseHandle(handle)
            return True

        user32.EnumWindows(enum_proc(callback), 0)
    except Exception as e:
        logger.debug(f"读取微信窗口进程失败: {e}")
    return pids


def _capture_window_region(
    hwnd: int,
    box: tuple[int, int, int, int] | None = None,
):
    """Capture a window with PrintWindow and crop absolute screen coordinates.

    PIL's ImageGrab depends on an attached interactive desktop. Remote,
    locked, or service-backed Windows sessions can deny that API even though
    the target window is alive. PrintWindow works through the window's device
    context in those sessions and keeps OCR available.
    """
    from PIL import Image
    import win32gui
    import win32ui

    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError("微信窗口句柄无效")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("微信窗口尺寸无效")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError("无法获取微信窗口设备上下文")

    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)

        flags = getattr(win32gui, "PW_RENDERFULLCONTENT", 0x00000002)
        captured = bool(
            ctypes.windll.user32.PrintWindow(
                hwnd,
                memory_dc.GetSafeHdc(),
                flags,
            )
        )
        if not captured:
            captured = bool(
                ctypes.windll.user32.PrintWindow(
                    hwnd,
                    memory_dc.GetSafeHdc(),
                    0,
                )
            )
        if not captured:
            raise RuntimeError("PrintWindow 截图失败")

        info = bitmap.GetInfo()
        data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            data,
            "raw",
            "BGRX",
            0,
            1,
        )

        if box is not None:
            crop_left = max(left, int(box[0]))
            crop_top = max(top, int(box[1]))
            crop_right = min(right, int(box[2]))
            crop_bottom = min(bottom, int(box[3]))
            if crop_right <= crop_left or crop_bottom <= crop_top:
                raise RuntimeError("截图区域不在微信窗口内")
            image = image.crop(
                (
                    crop_left - left,
                    crop_top - top,
                    crop_right - left,
                    crop_bottom - top,
                )
            )

        # Some remote sessions return an all-black DC without reporting an
        # error. Treat that as a failed capture so the next fallback runs.
        if max(image.convert("L").getextrema()) == 0:
            raise RuntimeError("PrintWindow 返回空白图像")
        return image
    finally:
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if memory_dc is not None:
            try:
                memory_dc.DeleteDC()
            except Exception:
                pass
        if source_dc is not None:
            try:
                source_dc.DeleteDC()
            except Exception:
                pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def _install_wechatauto_screenshot_compat() -> None:
    """Make wechatauto OCR survive RDP and restricted desktop sessions."""
    try:
        from wechatauto.guia import WeChatGUI
    except Exception as e:
        logger.debug(f"加载 wechatauto 截图兼容层失败: {e}")
        return

    if getattr(WeChatGUI, "_wechatbot_safe_screenshot", False):
        return

    def safe_grab_screen(self, box=None):
        from PIL import ImageGrab

        errors: list[str] = []
        last_error: Exception | None = None

        for delay in (0.0, 0.12):
            if delay:
                time.sleep(delay)
            try:
                return (
                    ImageGrab.grab(bbox=box, all_screens=True)
                    if box
                    else ImageGrab.grab(all_screens=True)
                )
            except Exception as exc:
                last_error = exc
                errors.append(f"桌面截图: {exc}")

        hwnd = int(getattr(self, "main_hwnd", 0) or 0)
        try:
            return _capture_window_region(hwnd, box)
        except Exception as exc:
            last_error = exc
            errors.append(f"窗口截图: {exc}")

        # A disconnected RDP session can leave the window iconized. Restore
        # it and retry both capture paths once before surfacing the error.
        try:
            if hwnd:
                user32 = ctypes.windll.user32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                else:
                    user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.2)
        except Exception as exc:
            errors.append(f"唤醒窗口: {exc}")

        try:
            return _capture_window_region(hwnd, box)
        except Exception as exc:
            last_error = exc
            errors.append(f"窗口重试: {exc}")

        try:
            return (
                ImageGrab.grab(bbox=box, all_screens=True)
                if box
                else ImageGrab.grab(all_screens=True)
            )
        except Exception as exc:
            last_error = exc
            errors.append(f"桌面重试: {exc}")

        logger.debug("截图兼容层尝试失败: " + "；".join(errors))
        raise RuntimeError(f"屏幕截图失败：{last_error}")

    WeChatGUI._grab_screen = safe_grab_screen
    WeChatGUI._wechatbot_safe_screenshot = True


def _install_wechatauto_input_box_guard() -> None:
    """让 ``guia.get_input_box()`` 在探测失败时**真的**返回 ``None``。

    wechatauto 的 ``get_input_box()`` 连续探测失败后会兜底返回一个按 ``render_h``
    比例算出来的**猜测矩形**，**永不返回 None**；而它自己的 docstring 写的是
    「探测失败返回 None（不再回退到默认布局，避免在错误坐标上误点）」。

    库里的安全网**全部**建立在「失败返回 None」这个假设上，于是被这一个返回值
    同时废掉：

    * ``focus_input()`` 的 ``if not box: return False`` —— 死代码；
    * 文本发送的 ``if not box: 重试``（6 次）—— 死代码；
    * ``_open_chat_and_settle()`` 的 5 次重试 —— 永远「成功」；
    * ``wx.open_chat()`` 的成败判断 —— 永远成功；
    * ``send_file()`` 的「会话复用」捷径 —— 输入框没就绪也照走。

    后果：附件被粘到猜测坐标上、根本没进输入框，而
    ``_paste_attachment_and_send`` 对**非图片**附件是**无条件 return True** 的，
    于是整条链路记成「发送成功」。0.8.7 实测：群聊 101.6 MB 视频下载成功、
    发送落空、日志记成功，聊天里什么都没多出来。

    这里把兜底换成 ``None``，并在放弃前**强制重新校准一次**（忽略
    ``_auto_recalibrated`` 的「每会话一次」限制，30 秒内不重复校准），
    让库原本的重试逻辑真正生效；定位不到就明确失败，不再往错误坐标上盲粘。
    """
    try:
        from wechatauto.guia import WeChatGUI
    except Exception as exc:  # pragma: no cover - 缺依赖时安静跳过
        logger.debug(f"加载 wechatauto 输入框兼容层失败: {exc}")
        return

    if getattr(WeChatGUI, "_wechatbot_strict_input_box", False):
        return

    WeChatGUI.get_input_box = _strict_input_box
    WeChatGUI._wechatbot_strict_input_box = True
    logger.info("已启用输入框严格定位：探测失败不再回退到猜测坐标")


#: 输入框定位的探测轮数 / 间隔，以及「强制重新校准」的最小间隔（秒）。
#: 抽成模块常量便于测试直接调小，不必真等 3 秒。
_INPUT_BOX_PROBE_ATTEMPTS = 6
_INPUT_BOX_PROBE_INTERVAL = 0.5
_INPUT_BOX_RECALIBRATE_INTERVAL = 30.0


def _strict_input_box(gui) -> tuple | None:
    """严格版输入框定位：UIA 优先 → 像素探测 → 重校准，全失败返回 ``None``。

    与 wechatauto 原实现的两点区别：

    * **不再兜底返回猜测矩形**——正是那个兜底让库里所有 ``if not box:`` 的
      安全网失效（细节见 :func:`_install_wechatauto_input_box_guard`）。
    * **像素探测换成从右半侧起扫**（:func:`_probe_input_box_rightside`），
      不再依赖可能是默认值的 ``sidebar_ratio``。
    """
    # 1) UIA 优先：矩形精确、不需要截屏，窗口缩在托盘里也照样有值。
    box = _uia_input_box(gui)
    if box:
        return box

    probe = _probe_input_box_rightside
    for _ in range(_INPUT_BOX_PROBE_ATTEMPTS):
        box = probe(gui)
        if box:
            return box
        time.sleep(_INPUT_BOX_PROBE_INTERVAL)
        gui._update_render_rect()

    # 探测失败：强制重新校准一次再试。原实现被 ``_auto_recalibrated`` 限制成
    # 「每会话只触发一次」，第二次失败就再也救不回来了。校准开销大，
    # 这个间隔内不重复做。
    now = time.time()
    if now - getattr(gui, "_wechatbot_last_recalibrate", 0.0) > _INPUT_BOX_RECALIBRATE_INTERVAL:
        gui._wechatbot_last_recalibrate = now
        gui._auto_recalibrated = False
        try:
            gui.calibrate_layout()
        except Exception as exc:
            logger.debug(f"重新校准微信布局失败: {exc}")
        box = _uia_input_box(gui)
        if box:
            logger.info("重新校准布局后（UIA）成功定位输入框")
            return box
        for _ in range(3):
            box = probe(gui)
            if box:
                logger.info("重新校准布局后成功定位输入框")
                return box
            time.sleep(_INPUT_BOX_PROBE_INTERVAL)
            gui._update_render_rect()

    logger.warning(
        "输入框定位失败（UIA + 像素探测 + 重新校准都没成功），本次放弃粘贴——"
        "宁可报失败，也不把附件粘到猜测出来的错误位置上"
    )
    return None


#: UIA 取输入框失败后的冷却时间（秒）。UIA 首次初始化或控件树异常时会抛错，
#: 而 ``get_input_box`` 是热路径，不能每次都白等一次超时。
_UIA_INPUT_BOX_RETRY_INTERVAL = 30.0
_uia_input_box_disabled_until = 0.0


def _uia_input_box(gui) -> tuple | None:
    """用 UIA 拿聊天输入框矩形，换算成渲染相对坐标（``(x0,y0,x1,y1)``）。

    UIA 给的 ``mmui::ChatInputField`` 矩形是**精确**的，而且不需要截屏——比
    像素探测可靠得多：像素探测依赖 ``right_pane_left``，而它来自
    ``sidebar_ratio``，那个值可能压根没测出来（见 :func:`_probe_input_box_rightside`）。

    UIA 层本身不可用时按 ``_UIA_INPUT_BOX_RETRY_INTERVAL`` 冷却，避免在
    ``get_input_box`` 这种热路径上反复付超时代价。
    """
    global _uia_input_box_disabled_until
    if time.time() < _uia_input_box_disabled_until:
        return None
    try:
        uia = gui._get_uia()
        if uia is None:
            _uia_input_box_disabled_until = time.time() + _UIA_INPUT_BOX_RETRY_INTERVAL
            return None
        ctrl = uia._chat_input(getattr(uia, "_win", None))
        if ctrl is None:
            return None
        rect = ctrl.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if right - left < 50 or bottom - top < 30:
            return None
        ox = int(getattr(gui, "origin_x", 0) or 0)
        oy = int(getattr(gui, "origin_y", 0) or 0)
        return (left - ox, top - oy, right - ox, bottom - oy)
    except Exception as exc:  # noqa: BLE001 - UIA 不可用就走像素兜底
        logger.debug(f"UIA 取输入框矩形失败: {exc}")
        _uia_input_box_disabled_until = time.time() + _UIA_INPUT_BOX_RETRY_INTERVAL
        return None


def _probe_input_box_rightside(gui) -> tuple | None:
    """像素探测兜底：探测行**从右半侧起扫**，不依赖 ``right_pane_left``。

    wechatauto 原版从 ``right_pane_left``（= ``sidebar_ratio * render_w``）开始
    扫整行，要求该行 ≥80% 是白（>240）。但 ``sidebar_ratio`` 可能是**没测出来
    的默认值** 0.22——``calibrate_layout()`` 的「搜索」OCR 锚点失败后会静默回落
    到默认比例，而同一份布局文件里 ``send_button_ratio`` 却是实测值，很容易
    让人以为整份配置都是可信的。

    本机实测真值 ≈0.384：多扫进去的 274px 会话列表底色是 ``(238,238,240)``，
    不满足 >240，把白度从 **0.988 压到 0.782**，正好卡在 0.80 阈值之下，
    探测**必然失败**——日志里刷屏的「输入框定位失败」就是这么来的。

    输入框永远在聊天区（右侧），所以直接从右半侧起扫即可，彻底不依赖侧栏校准。
    """
    render_w = int(getattr(gui, "render_w", 0) or 0)
    render_h = int(getattr(gui, "render_h", 0) or 0)
    if render_w <= 0 or render_h <= 0:
        return None
    origin_x = int(getattr(gui, "origin_x", 0) or 0)
    origin_y = int(getattr(gui, "origin_y", 0) or 0)
    # 探测行起点：至少半窗宽，绝不落进会话列表。
    pane_left = max(int(getattr(gui, "right_pane_left", 0) or 0),
                    int(render_w * 0.5))
    sx = origin_x + (pane_left + render_w) // 2

    for probe_off in (150, 120, 200, 250, 350, 100, 450):
        probe_y = render_h - probe_off
        if probe_y <= int(render_h * 0.50):
            continue
        sy = origin_y + probe_y
        row = gui._grab_screen((origin_x + pane_left, sy,
                                origin_x + render_w, sy + 1))
        px = row.load()
        width = row.size[0]
        if width <= 0:
            continue
        white = sum(1 for x in range(0, width, 2) if sum(px[x, 0]) / 3 > 240)
        if white / max(1, (width + 1) // 2) < 0.8:
            continue
        scan_top = origin_y + int(render_h * 0.50)
        col = gui._grab_screen((sx, scan_top, sx + 1, sy + 1))
        pc = col.load()
        dy = sy - scan_top
        y0 = dy
        while y0 > 0 and sum(pc[0, y0]) / 3 > 240:
            y0 -= 1
        y0 += 1
        y1 = dy
        while y1 < col.size[1] - 1 and sum(pc[0, y1]) / 3 > 240:
            y1 += 1
        g = y0 - 1
        while g >= 0 and sum(pc[0, g]) / 3 <= 240:
            g -= 1
        divider_h = (y0 - 1) - g
        y0_abs = scan_top + y0 - origin_y
        y1_abs = scan_top + y1 - origin_y
        if (1 <= divider_h <= 4
                and y1_abs - y0_abs >= 150
                and y0_abs >= int(render_h * 0.45)
                and y1_abs >= int(render_h * 0.85)):
            return (pane_left, y0_abs, render_w, y1_abs)
    return None


# ---------------------------------------------------------------------------
# 原生「选择文件」对话框路线：UIA 点按钮 → Win32 填路径
# ---------------------------------------------------------------------------
#
# 为什么改走这条路：剪贴板路线要「探输入框（像素）→ 写 CF_HDROP → 等微信把
# 原始文件拷进自己的目录 → 按回车」，任何一步时序错位都会静默失败；而且它
# 依赖像素探测，窗口一被缩到托盘就全盘失效。这条路线**全程不碰像素**：
#
#   * 「发送文件」按钮在 UIA 树里是个**有名字**的 ButtonControl（mmui::XButton）；
#   * 它弹出的是标准 Vista+ 原生对话框（class ``#32770``，标题「选择文件」），
#     文件名输入框是 ctrlID 1148（``edt1``）的 Edit，确认按钮是 IDOK(1)。
#
# 实测：找按钮 0.076s、点击 0.39s、对话框出现 0.39s、WM_SETTEXT 即时，
# 全程约 1 秒、**0 次截屏、0 次 OCR**。

#: UIA 树里「发送文件」按钮的名字（按顺序尝试，兼容不同版本）。
FILE_BUTTON_NAMES = ("发送文件", "发送文件(Alt+F)")

#: 原生文件对话框：窗口类 / 标题 / 控件 ID（通用对话框的固定常量）。
FILE_DIALOG_CLASS = "#32770"
FILE_DIALOG_TITLES = ("选择文件", "打开")
FILE_DIALOG_EDIT_ID = 1148       # edt1：文件名输入框
FILE_DIALOG_OK_ID = 1            # IDOK：打开
FILE_DIALOG_CANCEL_ID = 2        # IDCANCEL：取消

#: 等对话框出现的轮询参数。
FILE_DIALOG_TIMEOUT_SECONDS = 4.0
FILE_DIALOG_POLL_INTERVAL = 0.12

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
BM_CLICK = 0x00F5
WM_CLOSE = 0x0010
_SW_SHOW = 5
_SW_RESTORE = 9

#: 窗口枚举回调原型（``WINFUNCTYPE``，调用约定必须是 ``stdcall``）。
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

#: 私有 user32 句柄。``ctypes.windll.user32`` 是**进程内共享单例**，给它设
#: ``argtypes``/``restype`` 会连带改掉 wechatauto 自己的调用签名（它用的是同一个
#: 对象，而且它显式设过 ``SendMessageW.argtypes``）。这里单独 ``WinDLL("user32")``
#: 拿一份独立句柄，就能放心声明签名、也让 64 位的 ``LRESULT`` 不被截成 32 位。
_USER32 = None


def _user32():
    """惰性创建带显式签名的私有 user32 句柄（与 wechatauto 互不影响）。"""
    global _USER32
    if _USER32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, ctypes.c_void_p]
        lib.SendMessageW.restype = ctypes.c_ssize_t
        lib.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR]
        lib.FindWindowExW.restype = wintypes.HWND
        lib.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        lib.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC,
                                         wintypes.LPARAM]
        lib.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        lib.GetClassNameW.restype = ctypes.c_int
        lib.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        lib.GetWindowTextW.restype = ctypes.c_int
        lib.GetDlgCtrlID.argtypes = [wintypes.HWND]
        lib.GetDlgCtrlID.restype = ctypes.c_int
        lib.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
        lib.GetWindowThreadProcessId.restype = wintypes.DWORD
        lib.IsWindow.argtypes = [wintypes.HWND]
        lib.IsWindow.restype = wintypes.BOOL
        lib.IsWindowVisible.argtypes = [wintypes.HWND]
        lib.IsWindowVisible.restype = wintypes.BOOL
        lib.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        lib.ShowWindow.restype = wintypes.BOOL
        lib.SetForegroundWindow.argtypes = [wintypes.HWND]
        lib.SetForegroundWindow.restype = wintypes.BOOL
        _USER32 = lib
    return _USER32


def _hwnd_int(value) -> int:
    """把回调里拿到的句柄参数统一成 ``int``（ctypes 可能给 int 或 ``c_void_p``）。"""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(getattr(value, "value", 0) or 0)


def _send_msg(hwnd: int, msg: int, wparam: int = 0, lparam=None):
    """``SendMessageW`` 的薄封装：走私有句柄，参数显式包成 ctypes 类型。"""
    if lparam is None:
        lparam = ctypes.c_void_p(0)
    return _user32().SendMessageW(
        wintypes.HWND(hwnd), wintypes.UINT(msg),
        wintypes.WPARAM(wparam), lparam,
    )


def _find_file_dialog(allowed_pids=None) -> int:
    """找微信弹出的原生「选择文件」对话框；没有则返回 0。

    ``allowed_pids`` 给定时只认这些进程拥有的窗口。**必须给**：``#32770`` +
    标题「打开」/「选择文件」是 Windows 通用对话框的固定长相，任何程序
    （记事本、浏览器、别的工具）都会弹一模一样的窗口。认错窗口就会把文件路径
    填到**别人**的对话框里——比发不出去糟得多。本机实测就撞到过：微信自己的
    「选择文件」对话框还开着（上一次手工测试留下的），说明这种窗口确实会残留。
    """
    found: list[int] = []
    u32 = _user32()
    allowed = set(allowed_pids) if allowed_pids else None

    def _cb(hwnd, _lparam):
        try:
            h = _hwnd_int(hwnd)
            cls = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(wintypes.HWND(h), cls, 64)
            if cls.value != FILE_DIALOG_CLASS:
                return True
            title = ctypes.create_unicode_buffer(128)
            u32.GetWindowTextW(wintypes.HWND(h), title, 128)
            if title.value not in FILE_DIALOG_TITLES:
                return True
            if allowed is not None:
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(pid))
                if pid.value not in allowed:
                    return True
            found.append(h)
            return False
        except Exception:  # noqa: BLE001 - 枚举里单点失败不该中断整轮
            pass
        return True

    try:
        u32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception as exc:
        logger.debug(f"枚举文件对话框失败: {exc}")
    return found[0] if found else 0


def _dialog_child_by_id(parent: int, ctrl_id: int, cls: str = "") -> int:
    """按控件 ID 在对话框里找子控件（可再用类名收窄）。"""
    found: list[int] = []
    u32 = _user32()

    def _cb(hwnd, _lparam):
        try:
            h = _hwnd_int(hwnd)
            if u32.GetDlgCtrlID(wintypes.HWND(h)) != ctrl_id:
                return True
            if cls:
                name = ctypes.create_unicode_buffer(64)
                u32.GetClassNameW(wintypes.HWND(h), name, 64)
                if name.value != cls:
                    return True
            found.append(h)
            return False
        except Exception:  # noqa: BLE001
            return True

    try:
        u32.EnumChildWindows(wintypes.HWND(parent), _WNDENUMPROC(_cb), 0)
    except Exception as exc:
        logger.debug(f"枚举对话框子控件失败: {exc}")
    return found[0] if found else 0


def _dialog_edit(dlg: int) -> int:
    """定位文件名输入框：``ComboBoxEx32 → ComboBox → Edit``（通用对话框固定结构）。"""
    u32 = _user32()

    def _child(parent: int, cls: str) -> int:
        return _hwnd_int(u32.FindWindowExW(wintypes.HWND(parent), None, cls, None))

    combo_ex = _child(dlg, "ComboBoxEx32")
    if combo_ex:
        combo = _child(combo_ex, "ComboBox")
        if combo:
            edit = _child(combo, "Edit")
            if edit:
                return edit
    return _dialog_child_by_id(dlg, FILE_DIALOG_EDIT_ID, "Edit")


def _close_file_dialog(dlg: int) -> None:
    """关掉原生文件对话框（收尾用，避免残留窗口挡住下一轮）。"""
    try:
        _send_msg(dlg, WM_CLOSE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"关闭文件对话框失败: {exc}")


def _set_dialog_text(hwnd: int, text: str) -> bool:
    """用 ``WM_SETTEXT`` 直接写控件文本并读回校验。

    比 ``SendInput`` 逐字打路径快两个数量级（每字符 2 次系统调用），
    也不受输入法干扰。
    """
    try:
        _send_msg(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text))
        length = int(_send_msg(hwnd, WM_GETTEXTLENGTH))
        buf = ctypes.create_unicode_buffer(length + 2)
        _send_msg(hwnd, WM_GETTEXT, length + 1,
                  ctypes.cast(buf, ctypes.c_void_p))
        return buf.value == text
    except Exception as exc:
        logger.debug(f"写入文件对话框失败: {exc}")
        return False


def _click_dialog_control(hwnd: int) -> bool:
    try:
        _send_msg(hwnd, BM_CLICK)
        return True
    except Exception as exc:
        logger.debug(f"点击对话框控件失败: {exc}")
        return False


def _ensure_wechat_visible(gui) -> bool:
    """确认微信主窗**真的可见**；不可见就恢复显示并置前。

    为什么必须先做这一步：窗口被缩到托盘时（``WS_VISIBLE`` 被清掉）
    ``GetWindowRect`` **仍然返回完全正确的矩形**，但 ``ImageGrab.grab`` 抓到的
    是**别的程序的画面**——所有基于像素的判断（输入框探测、会话定位）会一起
    失效，表现为日志里刷屏的「输入框定位失败」+「无法打开会话」。

    实测过：按微信窗口矩形抓屏，抓到的是 WorkBuddy 自己的窗口。

    返回值是**「可以继续发送吗」**而不是「窗口可见吗」：拿不到句柄（判断不了）
    时返回 ``True``——判断不了就不阻断，别把没问题的发送拦下来；只有**确认
    不可见且恢复失败**才返回 ``False``。
    """
    try:
        hwnd = int(getattr(gui, "main_hwnd", 0) or 0)
        if not hwnd:
            return True
        u32 = _user32()
        if u32.IsWindowVisible(wintypes.HWND(hwnd)):
            return True
        logger.warning("微信主窗当前不可见（可能在托盘里），正在恢复显示…")
        u32.ShowWindow(wintypes.HWND(hwnd), _SW_SHOW)
        u32.ShowWindow(wintypes.HWND(hwnd), _SW_RESTORE)
        u32.SetForegroundWindow(wintypes.HWND(hwnd))
        time.sleep(0.5)
        if u32.IsWindowVisible(wintypes.HWND(hwnd)):
            logger.info("微信主窗已恢复显示")
            return True
        logger.error("微信主窗仍不可见，无法进行依赖画面/点击的发送")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"恢复微信主窗显示失败（忽略，按可见继续）: {exc}")
        return True


def _uia_find_button_center(uia, names) -> tuple[int, int] | None:
    """在 UIA 树里按名字找按钮，返回其屏幕中心坐标；找不到返回 ``None``。"""
    if uia is None:
        return None
    win = getattr(uia, "_win", None)
    if win is None:
        return None
    try:
        from wechatauto.uia_driver import _find_by
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"加载 UIA 遍历工具失败: {exc}")
        return None
    for name in names:
        try:
            btn = _find_by(
                win,
                lambda c, _n=name: (c.ControlTypeName == "ButtonControl"
                                    and (c.Name or "") == _n),
            )
        except Exception:  # noqa: BLE001
            btn = None
        if btn is None:
            continue
        try:
            rect = btn.BoundingRectangle
            return ((int(rect.left) + int(rect.right)) // 2,
                    (int(rect.top) + int(rect.bottom)) // 2)
        except Exception:  # noqa: BLE001
            continue
    return None


def _install_wechatauto_uia_compat() -> None:
    """Keep UIA usable when Windows blocks third-party module enumeration.

    The packaged wechatauto driver filters visible windows by inspecting
    ``Weixin.dll``.  Some desktop sessions deny that module scan even though
    UI Automation itself works, which makes a logged-in WeChat look absent.
    The fallback below only enumerates visible windows owned by Weixin.exe and
    never clicks, scans, or otherwise handles a login prompt.
    """
    _install_wechatauto_screenshot_compat()
    _install_wechatauto_input_box_guard()

    try:
        from wechatauto.uia_driver import WeChatUIA
    except Exception as e:
        logger.debug(f"加载 wechatauto UIA 兼容层失败: {e}")
        return

    if getattr(WeChatUIA, "_wechatbot_safe_hwnds", False):
        return

    original = WeChatUIA._wechat_hwnds

    def safe_process_modules(pid: int):
        """Yield module tuples through PSAPI when Toolhelp is restricted."""
        try:
            import win32api
            import win32con
            import win32process

            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION
                | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
        except Exception:
            return

        if not handle:
            return

        try:
            modules = win32process.EnumProcessModulesEx(handle, 0x03)
            for module in modules:
                try:
                    path = win32process.GetModuleFileNameEx(handle, module)
                    yield (
                        int(module),
                        0,
                        os.path.basename(path),
                        path,
                    )
                except Exception:
                    continue
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass

    def safe_wechat_hwnds(self):
        try:
            handles = original(self)
        except Exception:
            handles = []
        if handles:
            return handles

        try:
            import psutil
            import win32gui
            import win32process

            weixin_pids = {
                proc.pid
                for proc in psutil.process_iter(["name"])
                if (proc.info.get("name") or "").lower() == "weixin.exe"
            }
            if not weixin_pids:
                return []

            candidates = []

            def collect(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd) or ""
                    if not any(hint in title for hint in ("微信", "Weixin")):
                        return True
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid not in weixin_pids:
                        return True
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    area = max(0, right - left) * max(0, bottom - top)
                    candidates.append((area, hwnd))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(collect, None)
            candidates.sort(reverse=True)
            return [hwnd for _, hwnd in candidates]
        except Exception as e:
            logger.debug(f"兼容层枚举微信窗口失败: {e}")
            return []

    WeChatUIA._wechat_hwnds = safe_wechat_hwnds
    WeChatUIA._process_modules = staticmethod(safe_process_modules)
    WeChatUIA._wechatbot_safe_hwnds = True
    # The bot never performs login.  This also protects older library paths
    # that would otherwise click a login button when a login window appears.
    WeChatUIA._auto_login = lambda self: False


class ConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class WeChatMessage:
    """微信消息"""
    id: str                     # 消息ID
    sender: str                 # 发送者显示名
    sender_name: str            # 发送者昵称
    room_id: str                # 群聊名称（用作唯一标识）
    room_name: str              # 群聊名称
    content: str                # 消息内容
    type: int                   # 消息类型（1=文本）
    timestamp: float            # 时间戳
    is_group: bool              # 是否群消息
    is_at_me: bool              # 是否@我
    message_type: str = "text"
    is_private: bool = False    # 是否私聊消息
    is_target_group: bool = True  # 群消息是否属于配置里要回应的群
    is_poke_me: bool = False    # 是否有人拍一拍/戳一戳机器人
    is_self: bool = False       # 是否机器人自己发出的消息
    poke_sender: str = ""       # 拍一拍发起人的显示名
    is_image_request: bool = False  # 是否要求机器人联网找图片
    voice_path: str = ""        # 已提取到本地的语音文件（供 ASR 使用）
    # wechat-mcp 新增：已提取到本地的图片文件（供视觉模型识别）。
    image_path: str = ""
    voice_url: str = ""         # 远端语音附件地址（QQ）
    voice_mime: str = ""        # 语音附件 MIME
    voice_filename: str = ""    # 远端语音附件文件名
    # wechat-mcp 为「引用/回复消息」检测新增：被引用者显示名，非引用消息为空。
    reply_to_name: str = ""


def _control_bounds(control) -> tuple[int, int, int, int] | None:
    """返回可见控件的屏幕矩形；控件不可见或尺寸异常时返回 None。"""
    try:
        if bool(getattr(control, "IsOffscreen", False)):
            return None
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
    except Exception:
        return None

    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return left, top, right, bottom


def _iter_uia_controls(root, max_depth: int = 40, max_nodes: int = 5000):
    """遍历 UIA 控件树，带深度和节点数限制。"""
    if root is None:
        return

    stack = [(root, 0)]
    visited = 0
    while stack and visited < max_nodes:
        control, depth = stack.pop()
        visited += 1
        yield control
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))


def _find_uia_controls(root, predicate, max_depth: int = 40) -> list:
    """返回控件树中所有满足条件的控件。"""
    return [
        control
        for control in _iter_uia_controls(root, max_depth=max_depth)
        if predicate(control)
    ]


class WeChatBridge:
    """微信桥接器：通过 UI Automation 监听群消息、发送回复"""

    SEND_TIMEOUT_SECONDS = 5.0           # 单次发送最多等待 5 秒（避免频繁超时）
    SEND_MIN_INTERVAL_SECONDS = 0.5      # 两条外发消息之间的最小间隔（0.5秒足够快）
    SEND_BACKOFF_BASE_SECONDS = 0.3      # 发送异常后的短冷却（更快恢复）
    SEND_BACKOFF_MAX_SECONDS = 2.0       # 冷却最长 2 秒（减少等待）
    SEND_ITEM_TTL_SECONDS = 60.0         # 排队太久没发出去的回复直接丢弃
    # 附件发送后回查数据库确认的等待上限。微信落库是异步的，大附件更慢，
    # 但也不能无限等——超时就判定为「微信没收下」，让上层能看见失败。
    FILE_CONFIRM_TIMEOUT_SECONDS = 10.0
    #: 附件发送的最大尝试次数。**只对「微信明确拒绝」重试**——那种情况说明
    #: 附件压根没粘上去（典型是输入框定位失败），重发不会重复。若只是数据库
    #: 没确认（可能落库慢），一律不重试，避免同一附件发两遍。
    FILE_SEND_ATTEMPTS = 2
    #: 附件「已粘进输入框、但回车没被微信接受」时，补按回车的次数与间隔（秒）。
    #: 微信粘贴大文件后要先把**原始文件**拷进自己的视频目录并生成卡片，这期间
    #: 按下的回车会被丢掉。实测那条 101.6 MB 的视频：原始文件 10:26:45 就已经
    #: 进了微信的 `msg/video/2026-10/<hash>_raw.mp4`（说明**粘贴是成功的**），
    #: 但数据库里直到 12:33 人工点「发送」才出现消息行——中间那两个小时里，
    #: 附件就静静躺在输入框里（用户截图可见）。
    #: **补按回车是安全的**：草稿还在就把它发出去；输入框已空则回车什么也不做，
    #: 不会重复发送。
    FILE_SEND_ENTER_NUDGES = 2
    FILE_SEND_ENTER_NUDGE_INTERVAL = 3.0
    #: 附件体积的**告警**阈值（MB），**只写日志、不拦截发送**。
    #: 微信的单文件上限**随会话类型变化**：自聊 / 文件传输助手最宽松（高速传输
    #: 可达 10 GB），普通聊天较严（旧版 100 MB）。所以这里只提醒一句，不替微信
    #: 做决定——拦掉一个本来发得出去的文件，比白试一轮更糟。
    #: 设 0 表示不打这条告警。
    FILE_SEND_SIZE_WARN_MB = 100
    #: 附件发送路线：
    #:
    #: * ``auto``（默认）——先试原生「选择文件」对话框路线，失败再回退剪贴板粘贴；
    #: * ``dialog``——只用对话框路线（失败即失败，不回退）；
    #: * ``clipboard``——只用剪贴板粘贴路线（0.8.9 及以前的行为）。
    #:
    #: 对话框路线**全程不截屏、不 OCR**：按钮坐标来自 UIA，路径用 ``WM_SETTEXT``
    #: 直接写进原生对话框，实测约 1 秒完成。
    FILE_SEND_MODE = "auto"
    FILE_SEND_MODES = ("auto", "dialog", "clipboard")
    #: 视为「附件已落地」的消息类型（对应 wechatauto 的 MSG_TYPE_NAMES）。
    #: 图片=3、视频=43、文件/链接/卡片=49。视频消息正文里没有文件名，
    #: 只能像图片一样靠「类型 + 时序」判断。
    ATTACHMENT_MSG_TYPES = ("图片", "视频", "文件/链接/卡片")
    MESSAGE_DEDUPE_WINDOW_SECONDS = 60.0 # 去重窗口60秒（避免重复但不过长）
    # 机器人昵称缓存的有效期：微信里改了昵称后不必重启程序——一旦遇到「有 @ 但
    # 没匹配上」的消息，就按这个间隔重读一次昵称（避免每条群消息都去查库）。
    IDENTITY_REFRESH_SECONDS = 60.0
    # 无名群兜底名里最多拼几个成员昵称（微信界面上也是截断显示的，拼太多反而对不上）。
    ROOM_NAME_MEMBER_LIMIT = 3

    # 这些账号的消息不参与私聊自动回复（公众号/系统号/文件传输助手等）
    IGNORED_PRIVATE_ACCOUNTS = {
        "filehelper",
        "newsapp",
        "fmessage",
        "floatbottle",
        "medianote",
        "notifymessage",
        "qqmail",
        "tmessage",
        "weixin",
        "mphelper",
        "weixinnotify",
    }

    def __init__(
        self,
        target_groups: list[str],
        on_message: Callable[[WeChatMessage], Awaitable[None]] | None = None,
        reply_delay: float = 0.0,
        backend: str = "auto",
        listen_private: bool = True,
    ):
        self.target_groups = target_groups
        self.on_message = on_message
        self.reply_delay = reply_delay
        self.backend = backend
        self.listen_private = bool(listen_private)
        self._wx = None
        self._backend = ""
        self._status = ConnectionStatus.DISCONNECTED
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._room_names_by_id: dict[str, str] = {}
        self._listen_keys_by_group: dict[str, str] = {}
        self._registered_listen_keys_by_group: dict[str, str] = {}
        self._bot_names: set[str] = set()
        self._self_wxid = ""
        # 上次读取机器人昵称的时间（monotonic），用于「改昵称免重启」的刷新节流。
        self._identity_loaded_at = 0.0
        self._chat_names: dict[str, str] = {}
        # 已经就「这个群没有群名称」提醒过的群，避免每次消息都刷同一条警告。
        self._nameless_rooms_warned: set[str] = set()
        self._target_group_ids: set[str] = set()
        self._listen_all_active = False
        self._uia_available = False
        # 发送隔离：所有外发消息由单独线程串行处理，
        # 微信界面卡住时只会丢弃本次发送，不会拖垮整个机器人
        self._send_queue: queue.Queue = queue.Queue(maxsize=8)
        self._sender_thread: threading.Thread | None = None
        self._sender_busy = threading.Event()
        self._sender_busy_since = 0.0
        self._accepting_sends = True
        self._send_gate: asyncio.Lock | None = None
        self._last_send_at = 0.0
        self._send_backoff_until = 0.0
        self._send_failures = 0
        self._recent_sent: dict[tuple[str, str], float] = {}
        self._send_inflight: dict[tuple[str, str], float] = {}
        self._send_inflight_lock = threading.Lock()
        self._seen_message_ids: dict[str, float] = {}
        self._window_process_ids: set[int] = set()

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    async def connect(self) -> bool:
        """连接用户已经手动登录的微信窗口。"""
        try:
            self._status = ConnectionStatus.CONNECTING
            logger.info("正在通过 UI Automation 连接微信...")

            try:
                self._backend = self._resolve_backend()
            except RuntimeError as e:
                logger.error(str(e))
                self._status = ConnectionStatus.ERROR
                return False

            # WeChat() 是同步阻塞调用，在线程池中执行避免阻塞 asyncio
            loop = asyncio.get_event_loop()
            if self._backend == "wechatauto":
                _install_wechatauto_uia_compat()
                from wechatauto import WeChat

                self._wx = await loop.run_in_executor(
                    None,
                    lambda: WeChat(start_listener=False),
                )
            elif self._backend == "wxauto4":
                from wxauto4 import WeChat, WxParam

                WxParam.TELEMETRY_ENABLED = False
                self._wx = await loop.run_in_executor(
                    None,
                    lambda: WeChat(
                        ads=False,
                        resize=False,
                        start_listener=False,
                    ),
                )
            else:
                from wxauto import WeChat

                self._wx = await loop.run_in_executor(None, WeChat)

            self._load_bot_identity()
            if self._backend == "wechatauto":
                try:
                    uia_ready = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            self.prepare_uia,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    uia_ready = False
                    logger.warning(
                        "微信 UIA 初始化超过 10 秒，切换到 OCR 兼容模式"
                    )
                if not uia_ready and not self.has_usable_gui():
                    logger.error(
                        "微信聊天主窗口不可用，"
                        "请确认已手动登录微信且窗口可见"
                    )
                    self._status = ConnectionStatus.ERROR
                    return False
                if not uia_ready:
                    logger.warning(
                        "微信 UIA 树不可用，已启用 OCR 兼容模式；"
                        "仍会监听并回复消息"
                    )
            self._window_process_ids = _visible_window_process_ids(
                self._window_executable_names()
            )
            self._accepting_sends = True
            self._status = ConnectionStatus.CONNECTED
            logger.info(f"微信连接成功（{self._backend}）")
            return True

        except Exception as e:
            error_msg = str(e)
            if "SetWindowPos" in error_msg or "窗口" in error_msg or "1400" in error_msg:
                logger.error(
                    "微信连接失败: 找不到可用聊天窗口。"
                    "请由用户手动登录微信并保持窗口可见"
                )
            else:
                logger.error(f"微信连接失败: {e}")
            self._status = ConnectionStatus.ERROR
            return False

    def _window_executable_names(self) -> set[str]:
        if self._backend in {"wechatauto", "wxauto4"}:
            return {"weixin.exe"}
        return {"wechat.exe"}

    def has_usable_uia(self) -> bool:
        """Return True when the chat window exposes a usable UIA tree."""
        if not self._uia_available:
            return False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            uia = gui._get_uia()
            return bool(uia is not None and uia.is_materialized())
        except Exception as e:
            logger.debug(f"检测微信 UIA 可用性失败: {e}")
            return False

    def has_usable_gui(self) -> bool:
        """Return True when the GUI bridge has a live, visible window."""
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            hwnd = int(
                getattr(gui, "main_hwnd", 0)
                or getattr(gui, "_hwnd", 0)
                or getattr(gui, "render_hwnd", 0)
                or 0
            )
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.4)
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                time.sleep(0.2)
            return bool(
                user32.IsWindowVisible(hwnd)
                and not user32.IsIconic(hwnd)
            )
        except Exception as e:
            logger.debug(f"检测微信 GUI 可用性失败: {e}")
            return False

    def prepare_uia(self) -> bool:
        """Initialize the UIA tree of a window that the user already logged in."""
        self._uia_available = False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            uia = gui._get_uia(refresh=True)
            if uia is None:
                return False
            if not uia._wechat_hwnds():
                return False
            if not uia.is_materialized():
                uia.ensure_materialized(
                    timeout=8,
                    force=True,
                )
            main = uia._find_main()
            if main is None:
                return False
            uia._win = main
            uia._activate(main)
            self._uia_available = True
            return True
        except Exception as e:
            logger.debug(f"预初始化微信 UIA 失败: {e}")
            return False

    def window_process_changed(self) -> bool:
        """Detect a WeChat client restart while the bot still holds old UIA objects."""
        if self._status != ConnectionStatus.CONNECTED:
            return False

        current = _visible_window_process_ids(self._window_executable_names())
        if not current:
            return False

        if self._window_process_ids and not (
            current & self._window_process_ids
        ):
            logger.warning(
                "检测到微信主窗口进程已变化: "
                f"{sorted(self._window_process_ids)} -> {sorted(current)}"
            )
            return True

        if not self._window_process_ids:
            self._window_process_ids = current
        return False

    def is_sender_stuck(self, timeout: float = 20.0) -> bool:
        """Return True when a UIA send has not returned for too long."""
        if not self._sender_busy.is_set() or self._sender_busy_since <= 0:
            return False
        return time.monotonic() - self._sender_busy_since >= timeout

    def abandon_stuck_sender(self) -> bool:
        """Detach the current sender thread and reset its global UIA lock.

        A blocked UIA call cannot be cancelled in-process.  Giving the new
        connection a fresh queue/event/lock lets the bot recover instead of
        waiting forever behind the old WeChat window.
        """
        if not self._sender_busy.is_set():
            return False

        old_sender = self._sender_thread
        self._send_queue = queue.Queue(maxsize=8)
        self._sender_thread = None
        self._sender_busy = threading.Event()
        self._sender_busy_since = 0.0

        try:
            from wechatauto.utils.lock import LockManager

            # The old sender may still hold this object forever.  New UIA
            # work must not wait behind that stale lock.
            LockManager.process_lock = multiprocessing.Lock()
            LockManager.thread_lock = threading.RLock()
        except Exception as e:
            logger.debug(f"重置微信自动化锁失败: {e}")

        logger.warning(
            "已隔离卡死的微信发送线程"
            + (f" ({old_sender.name})" if old_sender else "")
        )
        return True

    async def recover_connection(self) -> bool:
        """Reconnect after WeChat restarts or a UIA send thread gets stuck."""
        self.abandon_stuck_sender()

        try:
            await asyncio.wait_for(self.disconnect(), timeout=18)
        except asyncio.TimeoutError:
            logger.warning("自动恢复时断开旧微信连接超时，继续尝试重连")
        except Exception as e:
            logger.warning(f"自动恢复时断开旧微信连接失败: {e}")

        if not await self.connect():
            return False

        self._send_failures = 0
        self._send_backoff_until = 0.0
        self.start_listening(asyncio.get_running_loop())
        return (
            self._status == ConnectionStatus.CONNECTED
            and self._running
        )

    def _resolve_backend(self) -> str:
        """根据微信进程和显式配置选择自动化后端。"""
        requested = self.backend.lower().strip()
        aliases = {
            "wechatauto": "wechatauto",
            "wechatauto-replica": "wechatauto",
            "weixin4": "wechatauto",
            "wxauto4": "wxauto4",
            "weixin": "wechatauto",
            "4": "wechatauto",
            "wxauto": "wxauto",
            "wechat": "wxauto",
            "3.9": "wxauto",
        }

        processes = _running_process_names()

        if requested != "auto":
            backend = aliases.get(requested, requested)
            if backend not in {"wxauto", "wxauto4", "wechatauto"}:
                raise RuntimeError(f"不支持的微信后端: {self.backend}")
        elif "weixin.exe" in processes:
            backend = (
                "wechatauto"
                if importlib.util.find_spec("wechatauto") is not None
                else "wxauto4"
            )
        elif "wechat.exe" in processes:
            backend = "wxauto"
        else:
            raise RuntimeError(
                "未检测到微信进程，请由用户先打开并登录微信桌面客户端"
            )

        module_name = backend
        if importlib.util.find_spec(module_name) is None:
            packages = {
                "wechatauto": "wechatauto-replica",
                "wxauto4": "wxauto4",
                "wxauto": "wxauto",
            }
            package = packages[backend]
            raise RuntimeError(f"{package} 未安装，请重新运行本地部署安装命令")

        logger.info(f"自动选择微信自动化后端: {backend}")
        return backend

    def _load_bot_identity(self) -> None:
        """记录当前微信昵称，用于识别群消息中的 @机器人。

        读不到任何名字时**保留上一次的结果**：宁可继续用旧昵称，也好过把 @ 识别
        整个清空（清空后群里 @ 机器人就永远不会触发了）。
        """
        names: set[str] = set()
        wx = getattr(self, "_wx", None)
        nickname = getattr(wx, "nickname", None)
        if nickname:
            names.add(str(nickname).strip())

        try:
            info = wx._db.get_self_info()
            self_wxid = str(info.get("username") or "").strip()
            if self_wxid:
                self._self_wxid = self_wxid
            for key in ("nick_name", "remark", "username"):
                value = info.get(key)
                if value:
                    names.add(str(value).strip())
        except Exception:
            pass

        self._identity_loaded_at = time.monotonic()
        collected = {name for name in names if name}
        if not collected:
            return
        if collected != self._bot_names:
            logger.info(f"已识别机器人微信昵称: {', '.join(sorted(collected))}")
        self._bot_names = collected

    def _refresh_bot_identity(self) -> bool:
        """按冷却时间重读机器人昵称；昵称发生变化时返回 ``True``。

        微信昵称只在连接时读过一次，用户改了昵称就得重启程序才能生效——这里让它在
        需要时自动跟上：``_is_at_me`` 遇到「消息里有 @ 但没匹配上任何已知昵称」时
        调用一次，改名后最慢在下一条 @ 消息上生效。
        """
        now = time.monotonic()
        if now - self._identity_loaded_at < self.IDENTITY_REFRESH_SECONDS:
            return False
        before = set(self._bot_names)
        try:
            self._load_bot_identity()
        except Exception as exc:  # 刷新失败不应影响原本的判定结果
            self._identity_loaded_at = now
            logger.debug(f"刷新机器人昵称失败: {exc}")
            return False
        return self._bot_names != before

    def start_listening(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动消息监听（轮询模式）"""
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法启动监听")
            return

        self._running = True
        self._loop = loop
        self._room_names_by_id.clear()
        self._listen_keys_by_group.clear()
        self._registered_listen_keys_by_group.clear()
        self._chat_names.clear()
        self._target_group_ids.clear()
        self._listen_all_active = False

        # 先把配置里的群名解析成群 wxid，用于后续过滤非目标群消息
        resolved_groups: list[tuple[str, str]] = []
        for group in self.target_groups:
            group_id = group
            if self._backend == "wechatauto":
                group_id = self._resolve_wechatauto_group_id(group) or ""
                if not group_id:
                    logger.error(
                        f"未在微信数据库中群聊 [{group}]，已跳过注册，"
                        "避免误监听同名联系人"
                    )
                    continue
            self._room_names_by_id[group_id] = group
            self._listen_keys_by_group[group] = group_id
            self._target_group_ids.add(group_id)
            resolved_groups.append((group, group_id))

        self._registered_listen_keys_by_group.update(
            self._listen_keys_by_group
        )

        # wechatauto 支持全局监听：一次监听所有会话（含私聊与新群），
        # 因此私聊消息也能进入处理管线，不再只监听配置里的群。
        if self._backend == "wechatauto" and self.listen_private:
            try:
                self._wx.AddListenAll(
                    callback=self._handle_callback_message,
                )
                self._listen_all_active = True
                self._wx.StartListening()
                logger.info(
                    "消息监听已启动（全局监听：私聊 + 全部会话，"
                    f"仅对配置群聊回应 @/关键词）: "
                    f"{', '.join(group for group, _ in resolved_groups) or '无'}"
                )
                return
            except Exception as e:
                logger.error(f"启动全局消息监听失败，回退为按群监听: {e}")
                self._listen_all_active = False

        # 按群注册监听（非 wechatauto 后端，或全局监听不可用时）
        for group, listen_key in resolved_groups:
            try:
                if self._backend in {"wxauto4", "wechatauto"}:
                    self._wx.AddListenChat(
                        listen_key,
                        callback=self._handle_callback_message,
                    )
                else:
                    self._wx.AddListenChat(who=group)
                if listen_key == group:
                    logger.info(f"已注册监听群聊: {group}")
                else:
                    logger.info(f"已注册监听群聊: {group} ({listen_key})")
            except Exception as e:
                logger.warning(f"注册监听群聊失败 [{group}]: {e}")

        if self._backend in {"wxauto4", "wechatauto"}:
            try:
                self._wx.StartListening()
            except Exception as e:
                logger.error(f"启动 {self._backend} 消息监听失败: {e}")
                self._running = False
                return
            logger.info(f"消息监听已启动（{self._backend} 回调模式）")
            return

        # 启动轮询线程
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="wechat-poller",
        )
        self._poll_thread.start()
        logger.info("消息监听已启动（轮询模式）")

    def _resolve_wechatauto_group_id(self, group: str) -> str | None:
        """将群名解析为群聊 wxid，避免同名联系人优先匹配。"""
        if group.endswith("@chatroom"):
            return group

        try:
            group_id = self._wx._db.group_name_to_id(group)
            if group_id:
                return group_id
        except Exception:
            pass

        try:
            for contact in self._wx._db.search_contact(group):
                username = contact.get("username", "")
                if username.endswith("@chatroom"):
                    return username
        except Exception:
            pass
        return None

    def _normalize_target_groups(self, target_groups: list[str]) -> list[str]:
        """去重并整理群聊名称，保持用户配置顺序。"""
        result: list[str] = []
        for group in target_groups or []:
            name = str(group or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    async def set_target_groups(self, target_groups: list[str]) -> dict:
        """热更新需要监控的群聊列表，不重启微信连接。"""
        groups = self._normalize_target_groups(target_groups)
        self.target_groups = groups

        if (
            not self._wx
            or not self._running
            or self._status != ConnectionStatus.CONNECTED
        ):
            self._target_group_ids.clear()
            self._room_names_by_id.clear()
            self._listen_keys_by_group.clear()
            logger.info(
                f"监控群聊配置已更新（等待启动）: "
                f"{', '.join(groups) or '无'}"
            )
            return {
                "success": True,
                "live": False,
                "groups": groups,
                "registered": [],
                "unresolved": [],
                "message": "配置已保存，机器人启动后生效",
            }

        try:
            return await asyncio.to_thread(
                self._refresh_target_group_registration,
                groups,
            )
        except Exception as exc:
            logger.error(f"热更新监控群聊失败: {exc}")
            return {
                "success": False,
                "live": False,
                "groups": groups,
                "registered": [],
                "unresolved": [],
                "message": f"运行时更新失败，重启后生效: {exc}",
            }

    def _refresh_target_group_registration(
        self,
        groups: list[str],
    ) -> dict:
        """同步刷新群 wxid 映射，并在按群监听模式下补注册新群。"""
        previous_keys = set(self._listen_keys_by_group.values())
        resolved: list[tuple[str, str]] = []
        unresolved: list[str] = []

        for group in groups:
            group_id = group
            if self._backend == "wechatauto":
                group_id = self._resolve_wechatauto_group_id(group) or ""
                if not group_id:
                    unresolved.append(group)
                    logger.warning(
                        f"未在微信数据库中群聊 [{group}]，"
                        "本次暂未注册，启动或重连后会继续尝试"
                    )
                    continue
            resolved.append((group, group_id))

        self._room_names_by_id = {
            group_id: group for group, group_id in resolved
        }
        self._listen_keys_by_group = {
            group: group_id for group, group_id in resolved
        }
        self._target_group_ids = {
            group_id for _, group_id in resolved
        }

        if not self._listen_all_active:
            for group, listen_key in resolved:
                if listen_key in previous_keys:
                    continue
                try:
                    if self._backend in {"wxauto4", "wechatauto"}:
                        self._wx.AddListenChat(
                            listen_key,
                            callback=self._handle_callback_message,
                        )
                    else:
                        self._wx.AddListenChat(who=group)
                    self._registered_listen_keys_by_group[
                        group
                    ] = listen_key
                    logger.info(f"已新增监听群聊: {group} ({listen_key})")
                except Exception as exc:
                    logger.warning(f"新增监听群聊失败 [{group}]: {exc}")

        registered = [group for group, _ in resolved]
        logger.info(
            "监控群聊已热更新: "
            f"{', '.join(registered) or '无'}"
            + (
                f"；未找到: {', '.join(unresolved)}"
                if unresolved
                else ""
            )
        )
        return {
            "success": True,
            "live": True,
            "groups": groups,
            "registered": registered,
            "unresolved": unresolved,
            "message": (
                f"已实时更新 {len(registered)} 个群聊"
                + (
                    f"，{len(unresolved)} 个群名暂未找到"
                    if unresolved
                    else ""
                )
            ),
        }

    def _handle_callback_message(self, msg, chat) -> None:
        """接收 4.x 自动化库的回调消息。"""
        chat_id = (
            getattr(chat, "_wxid", None)
            or getattr(chat, "who", None)
            or str(chat)
        )
        chat_name = self._room_names_by_id.get(str(chat_id))
        if not chat_name:
            chat_name = self._chat_display_name(str(chat_id))
        self._handle_message(str(chat_id), msg, room_name=chat_name)

    def _poll_loop(self) -> None:
        """消息轮询循环（运行在独立线程）"""
        while self._running and self._wx:
            try:
                msgs = self._wx.GetListenMessage()
                if msgs:
                    for chat, messages in msgs.items():
                        chat_name = getattr(chat, "name", str(chat))
                        for msg in messages:
                            if msg.type == "friend":
                                self._handle_message(chat_name, msg)
            except Exception as e:
                if self._running:
                    logger.error(f"消息轮询异常: {e}")
                    time.sleep(2)
            time.sleep(1)

    def _handle_message(
        self,
        chat_id: str,
        msg,
        room_name: str | None = None,
    ) -> None:
        """处理单条消息"""
        try:
            chat_id = str(chat_id)
            is_group = chat_id.endswith("@chatroom")
            lower_id = chat_id.lower()
            if not is_group and (
                lower_id in self.IGNORED_PRIVATE_ACCOUNTS
                or lower_id.startswith("gh_")
            ):
                logger.debug(f"已忽略公众号/系统账号消息: {chat_id}")
                return

            room_name = room_name or self._chat_display_name(chat_id)
            raw_content = getattr(msg, "content", "") or ""
            if not isinstance(raw_content, str):
                raw_content = str(raw_content)
            sender = str(getattr(msg, "sender", "") or "").strip()
            if sender.startswith("wxid_"):
                # 把群成员/好友 wxid 换成昵称，日志和上下文更好读
                sender = self._chat_display_name(sender)
            elif not sender or (not is_group and sender == chat_id):
                sender = room_name
            content = self.normalize_message_content(
                raw_content,
                sender=sender,
            )
            is_self = self._is_self_message(
                msg,
                chat_id=chat_id,
                room_name=room_name,
                sender=sender,
                content=content,
            )
            if is_self:
                logger.debug(
                    f"已忽略机器人自己发出的消息 [{room_name}]: "
                    f"{content[:50]}"
                )
                return

            is_poke_me = False
            poke_sender = ""
            is_poke_me, poke_sender = self._detect_poke(raw_content)
            if is_poke_me:
                if not content:
                    content = f"{poke_sender or '有人'} 拍了拍本鲸鱼娘"
                # 拍一拍消息的原始发送者是群号/会话号，改成发起人，
                # 否则会被误判成机器人自己发的消息而丢弃
                if poke_sender:
                    sender = poke_sender
            local_id = getattr(msg, "local_id", None)
            sort_seq = getattr(msg, "sort_seq", None)
            raw_id = getattr(msg, "id", None)
            message_id = ":".join(
                str(part)
                for part in (chat_id, local_id or raw_id, sort_seq or "")
                if part
            )
            message_type = str(getattr(msg, "type", "text") or "text")
            voice_path = ""
            if message_type == "voice":
                voice_path = self._extract_voice_file(msg, chat_id)
            # wechat-mcp 新增：图片消息提取到本地，供视觉模型识别。
            image_path = ""
            if message_type == "image":
                image_path = self._extract_image_file(msg, chat_id)

            # wechat-mcp 新增：解析引用/回复的目标显示名。
            reply_to_name = self._extract_reply_target(raw_content)

            # 同一实例/多实例重复投递的消息只处理一次
            if message_id and self._is_duplicate_delivery(message_id):
                logger.debug(f"重复投递的消息已忽略: {message_id}")
                return

            message = WeChatMessage(
                id=message_id or str(time.time()),
                sender=sender,
                sender_name=sender,
                room_id=chat_id,
                room_name=room_name,
                content=content,
                type=3 if message_type == "image" else 1,
                timestamp=time.time(),
                is_group=is_group,
                is_at_me=self._is_at_me(msg, content),
                message_type=message_type,
                is_private=not is_group,
                is_target_group=(chat_id in self._target_group_ids)
                if is_group
                else True,
                is_poke_me=is_poke_me,
                is_self=is_self,
                poke_sender=poke_sender,
                voice_path=voice_path,
                image_path=image_path,
                reply_to_name=reply_to_name,
            )

            scope = "群聊" if is_group else "私聊"
            flag = "（拍一拍）" if is_poke_me else ""
            logger.info(
                f"[{scope}][{room_name}] {message.sender}: "
                f"{content[:50]}...{flag}"
            )

            if self.on_message and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self.on_message(message), self._loop
                )

        except Exception as e:
            logger.error(f"消息处理异常: {e}")

    def _resolve_media_context(self, msg, chat_id: str):
        """解析媒体提取所需的 ``(local_id, db, user)``；上下文不全返回 None。

        ``user`` 必须是**会话** wxid：media 库按会话（``chat_name_id`` /
        ``msg/attach/<md5(会话)>``）分组。关键点：全局监听（``AddListenAll``）下
        ``msg.parent.root`` 是只有 ``.who``/``._wxid``、**没有 ``_db``** 的
        ``_AllMessageChat`` 占位，所以 db 必须回退到桥接层自身的句柄
        ``self._wx._db``，否则媒体提取会静默失败。
        """
        local_id = getattr(msg, "local_id", None)
        parent = getattr(msg, "parent", None)
        chat = getattr(parent, "root", None) or parent
        db = (
            getattr(chat, "_db", None)
            or getattr(parent, "_db", None)
            or getattr(self._wx, "_db", None)
        )
        if local_id is None or db is None:
            return None
        user = str(
            getattr(chat, "_wxid", "")
            or getattr(parent, "_wxid", "")
            or chat_id
        ).strip()
        if not user:
            return None
        try:
            return int(local_id), db, user
        except (TypeError, ValueError):
            return None

    def _extract_voice_file(self, msg, chat_id: str) -> str:
        """Extract a wechatauto voice message from the local media database."""
        if self._backend != "wechatauto":
            logger.debug(f"[微信] {self._backend} 后端暂不提取语音文件")
            return ""

        ctx = self._resolve_media_context(msg, chat_id)
        if ctx is None:
            logger.debug(f"[微信] 语音消息缺少媒体上下文: {chat_id}")
            return ""
        local_id, db, user = ctx

        # 写到系统临时目录：冻结打包后包内路径不可靠，临时目录一定可写。
        save_dir = os.path.join(tempfile.gettempdir(), "wechat-mcp", "voice")
        try:
            from wechatauto.media import MediaDownloader

            path = MediaDownloader(db, save_dir=save_dir).download_voice(
                user,
                local_id,
                save_dir=save_dir,
            )
            if path:
                logger.info(f"[微信] 已提取语音文件: {path}")
                return str(path)
            logger.warning(
                f"[微信] 本地媒体库未找到语音文件: {chat_id}/{local_id}"
            )
        except Exception as exc:
            logger.warning(f"[微信] 提取语音文件失败: {exc}")
        return ""

    def _extract_image_file(self, msg, chat_id: str) -> str:
        """Extract a wechatauto image message from the local media database.

        wechat-mcp 新增：镜像 ``_extract_voice_file``，把图片解密落盘，
        供上层交给视觉模型识别。群聊图片默认只有缩略图，原图需在微信中
        点开过才会下载，故这里通常拿到的是缩略图。
        """
        if self._backend != "wechatauto":
            logger.debug(f"[微信] {self._backend} 后端暂不提取图片文件")
            return ""

        ctx = self._resolve_media_context(msg, chat_id)
        if ctx is None:
            logger.debug(f"[微信] 图片消息缺少媒体上下文: {chat_id}")
            return ""
        local_id, db, user = ctx

        # 写到系统临时目录：冻结打包后包内路径不可靠，临时目录一定可写。
        save_dir = os.path.join(tempfile.gettempdir(), "wechat-mcp", "images")
        try:
            from wechatauto.media import MediaDownloader

            path = MediaDownloader(db, save_dir=save_dir).download_image(
                user,
                local_id,
                save_dir=save_dir,
            )
            if path:
                logger.info(f"[微信] 已提取图片文件: {path}")
                return str(path)
            logger.debug(f"[微信] 本地媒体库未找到图片: {chat_id}/{local_id}")
        except Exception as exc:
            logger.warning(f"[微信] 提取图片文件失败: {exc}")
        return ""

    @staticmethod
    def _identity_key(value: str) -> str:
        """归一化微信显示名，便于稳定比较机器人身份。"""
        text = html.unescape(str(value or "")).strip().casefold()
        return re.sub(r"[\s\u2000-\u200f\u2028-\u202f\u205f\u3000]+", "", text)

    def _self_identity_keys(self) -> set[str]:
        values = set(self._bot_names)
        if self._self_wxid:
            values.add(self._self_wxid)
        return {
            key
            for value in values
            if (key := self._identity_key(value))
        }

    def _is_self_message(
        self,
        msg,
        *,
        chat_id: str = "",
        room_name: str = "",
        sender: str = "",
        content: str = "",
    ) -> bool:
        """多路判断机器人自己的消息，避免回复回显形成自娱自乐循环。"""
        if bool(getattr(msg, "is_self", False)):
            return True
        try:
            if int(getattr(msg, "wxid", 0) or 0) == 2:
                return True
        except (TypeError, ValueError):
            pass
        raw_wxid = str(getattr(msg, "wxid", "") or "").strip()
        if (
            raw_wxid
            and self._self_wxid
            and raw_wxid == str(self._self_wxid).strip()
        ):
            return True
        if str(getattr(msg, "attr", "") or "").casefold() == "self":
            return True

        sender_key = self._identity_key(sender)
        if sender_key and sender_key in self._self_identity_keys():
            return True

        self_wxid = self._identity_key(self._self_wxid)
        if self_wxid and self._identity_key(chat_id) == self_wxid:
            return True

        # 微信 4.x 的监听回调偶尔丢失 attr/is_self，机器人刚发出的内容
        # 很快又会作为新消息回调回来。短时间内容指纹是最可靠兜底。
        return bool(
            content
            and room_name
            and self._was_recently_sent(
                room_name,
                content,
                max_age=15.0,
            )
        )

    def _is_duplicate_delivery(self, message_id: str) -> bool:
        """同一消息被重复投递（多监听/多实例）时只处理一次。"""
        now = time.monotonic()
        if len(self._seen_message_ids) > 2000:
            self._seen_message_ids = {
                key: stamp
                for key, stamp in self._seen_message_ids.items()
                if now - stamp < self.MESSAGE_DEDUPE_WINDOW_SECONDS
            }
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = now
        return False

    def _chat_display_name(self, chat_id: str) -> str:
        """把会话 wxid 解析成可搜索的显示名（群名/联系人昵称）。

        群聊如果**没有设置群名称**，contact.db 里的 nick_name / remark 都是空的，
        而微信搜索框只认名字、不认 ``xxx@chatroom`` 这种 ID——发送时就定位不到会话，
        表现为「群里 @ 了机器人却收不到回复」。这里按微信自己的显示习惯兜底：
        无名群在界面上就是「成员昵称、成员昵称…」，用它当可搜索名。
        拼不出两个以上名字时保持 ID 不变，并提醒用户去设置群名称。
        """
        cached = self._room_names_by_id.get(chat_id) or self._chat_names.get(chat_id)
        if cached:
            return cached

        name = chat_id
        try:
            nickname = self._wx._db.get_nickname(chat_id)
            if nickname:
                name = str(nickname).strip() or chat_id
        except Exception:
            pass
        if name == chat_id and chat_id.endswith("@chatroom"):
            fallback = self._room_name_from_members(chat_id)
            if fallback:
                name = fallback
                logger.info(
                    f"群 {chat_id} 没有群名称，已按成员昵称拼出可搜索名「{fallback}」"
                )
            elif chat_id not in self._nameless_rooms_warned:
                self._nameless_rooms_warned.add(chat_id)
                logger.warning(
                    f"群 {chat_id} 没有群名称，微信搜索框无法定位它，"
                    "该群里的回复发不出去；请在微信里给这个群设置一个群名称。"
                )
        if chat_id == "filehelper":
            name = "文件传输助手"
        self._chat_names[chat_id] = name
        return name

    def _room_name_from_members(self, chat_id: str) -> str:
        """无名群兜底：用成员昵称拼出微信界面上显示的那个名字。

        **至少要有两个非空昵称才返回**：只有一个名字时，微信搜索会先命中同名的
        联系人，可能把群消息发进私聊，宁可不猜（返回空串，交给上层给出可操作的
        错误提示）。成员顺序按 username 排序，与微信的展示顺序不保证一致，
        所以这属于尽力而为——拼出来的名字能命中会话就发，命不中仍然会拒绝发送。
        """
        try:
            members = self._wx._db.get_group_members(chat_id) or []
        except Exception:
            return ""
        names: list[str] = []
        for member in members:
            try:
                label = str(
                    member.get("remark") or member.get("nick_name") or ""
                ).strip()
            except AttributeError:
                continue
            if label and label not in names:
                names.append(label)
            if len(names) >= self.ROOM_NAME_MEMBER_LIMIT:
                break
        if len(names) < 2:
            return ""
        return "、".join(names)

    def _detect_poke(self, raw_content: str) -> tuple[bool, str]:
        """识别拍一拍消息，返回 (是否拍了机器人, 发起人显示名)。

        微信 4.x 的拍一拍是 appmsg(type=62)，字段在 ``<patinfo>`` 里：
        ``pattedusername`` 是被拍的人，``fromusername`` 是发起人。
        """
        text = raw_content or ""
        if "<patinfo>" not in text:
            return False, ""

        patinfo = text[text.index("<patinfo>"):]
        patted = re.search(
            r"<pattedusername>(.*?)</pattedusername>",
            patinfo,
            flags=re.DOTALL,
        )
        patted_username = patted.group(1).strip() if patted else ""
        if self._self_wxid and patted_username:
            if patted_username != self._self_wxid:
                return False, ""
        elif not patted_username:
            title = re.search(r"<title>(.*?)</title>", text, flags=re.DOTALL)
            title_text = title.group(1) if title else ""
            if not self._poke_targets_me(title_text):
                return False, ""

        poker = ""
        source = re.search(
            r"<fromusername>(.*?)</fromusername>",
            patinfo,
            flags=re.DOTALL,
        )
        if source:
            poker_wxid = source.group(1).strip()
            if poker_wxid:
                poker = self._chat_display_name(poker_wxid)
        if not poker:
            title = re.search(r"<title>(.*?)</title>", text, flags=re.DOTALL)
            if title:
                matched = re.match(
                    r'\s*"?([^"<>]{1,40}?)"?\s*(?:拍了拍|戳了戳|碰了碰|敲了敲)',
                    title.group(1),
                )
                if matched:
                    poker = matched.group(1).strip()
        return True, poker

    def _poke_targets_me(self, title_text: str) -> bool:
        """在缺少 pattedusername 时，用标题里的被拍名称兜底判断。"""
        if not title_text:
            return False
        names = set(self._bot_names)
        names.add("鲸鱼娘")
        for name in names:
            if not name:
                continue
            pattern = (
                rf"(?:拍了拍|戳了戳|碰了碰|敲了敲)\s*[\"“]?{re.escape(name)}"
            )
            if re.search(pattern, title_text):
                return True
        return False

    @staticmethod
    def normalize_message_content(content: str, sender: str = "") -> str:
        """把微信卡片、引用和表情压缩成适合上下文使用的短文本。"""
        text = html.unescape(content or "").strip()
        if not text:
            return ""

        if sender:
            prefix = f"{sender.strip()}:"
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        text = re.sub(r"^wxid_[A-Za-z0-9_-]{4,}\s*:\s*", "", text)

        # 微信图片/表情/卡片消息常带 XML 声明（<?xml version="1.0"?>）。
        # 不先剥掉声明，"<msg" 判断会失配，导致整段原始 XML 漏进上下文。
        if text.startswith("<?xml"):
            decl_end = text.find("?>")
            if decl_end != -1:
                text = text[decl_end + 2:].lstrip()

        if text.startswith("<msg"):
            if "<emoji" in text:
                return "[表情]"
            if "<img" in text:
                return "[图片]"

            # 链接/分享卡片：原实现只保留标题、丢掉 <url>，下游拿不到链接，
            # 无法做链接解析。wechat-mcp 修改：保留标题的同时附上链接 URL。
            link_url = ""
            url_match = re.search(
                r"<url\b[^>]*>(.*?)</url>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if url_match:
                link_url = re.sub(
                    r"<!\[CDATA\[(.*?)\]\]>",
                    r"\1",
                    url_match.group(1),
                    flags=re.DOTALL,
                )
                link_url = re.sub(r"\s+", "", link_url).strip()

            title_match = re.search(
                r"<title\b[^>]*>(.*?)</title>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if title_match:
                title = re.sub(
                    r"<!\[CDATA\[(.*?)\]\]>",
                    r"\1",
                    title_match.group(1),
                    flags=re.DOTALL,
                ).strip()
                title = re.sub(r"\s+", " ", title)
                if title:
                    return (
                        f"{title[:300]}\n{link_url}" if link_url else title[:300]
                    )

            desc_match = re.search(
                r"<des\b[^>]*>(.*?)</des>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if desc_match:
                desc = re.sub(r"\s+", " ", desc_match.group(1)).strip()
                if desc:
                    return (
                        f"{desc[:300]}\n{link_url}" if link_url else desc[:300]
                    )
            return link_url or "[卡片消息]"

        return text.strip()

    @staticmethod
    def _extract_reply_target(raw_content: str) -> str:
        """引用/回复消息：返回被引用者的显示名；非引用或解析不到时返回空串。

        wechat-mcp 新增。微信引用消息（appmsg）原始内容里含 ``<refermsg>``，
        其 ``<displayname>`` 为被引用者显示名，用于判断是否在回复本机器人。
        本方法只读取，不改变任何原有逻辑。
        """
        if not raw_content or "<refermsg>" not in raw_content:
            return ""
        match = re.search(
            r"<refermsg>.*?<displayname>\s*(.*?)\s*</displayname>",
            raw_content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return ""
        return html.unescape(match.group(1)).strip()[:100]

    def _matches_bot_name(self, content: str) -> bool:
        """消息文本里是否出现了 ``@<机器人昵称>``。"""
        if not content:
            return False
        for name in self._bot_names:
            pattern = (
                rf"@{re.escape(name)}"
                r"(?=$|[\s\u2000-\u200f\u2028-\u202f\u205f\u3000，,。.!！？:：])"
            )
            if re.search(pattern, content):
                return True
        return False

    def _is_at_me(self, msg, content: str) -> bool:
        """判断消息是否明确 @ 当前机器人。"""
        if bool(getattr(msg, "is_at", False)):
            return True
        if bool(getattr(msg, "is_at_me", False)):
            return True
        if self._matches_bot_name(content):
            return True
        # 文本里有 @ 却没匹配上：可能是用户刚在微信里改了昵称（昵称只在连接时读过
        # 一次）。这里按冷却时间重读一次再判，省得「改个昵称必须重启程序」。
        if "@" in (content or "") and self._refresh_bot_identity():
            return self._matches_bot_name(content)
        return False

    async def send_text(self, room_name: str, content: str) -> bool:
        """发送文本消息（带节流、冷却与单线程隔离，避免微信界面卡死拖垮机器人）"""
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送消息")
            return False

        if self._was_recently_sent(room_name, content):
            logger.info(f"消息已在超时重试前成功发出，跳过重复发送: [{room_name}]")
            return True

        if self._send_is_inflight(room_name, content):
            logger.warning(
                "相同内容仍有发送任务在处理，等待结果以避免重复: "
                f"[{room_name}]"
            )
            deadline = time.monotonic() + self.SEND_BACKOFF_MAX_SECONDS
            while time.monotonic() < deadline:
                if self._was_recently_sent(room_name, content):
                    return True
                if not self._send_is_inflight(room_name, content):
                    break
                await asyncio.sleep(0.2)
            if self._was_recently_sent(room_name, content):
                return True
            if self._send_is_inflight(room_name, content):
                logger.warning(
                    "发送结果尚未确认，为避免重复已跳过同内容再次发送: "
                    f"[{room_name}]"
                )
                return True

        now = time.monotonic()
        if now < self._send_backoff_until:
            wait = min(
                self.SEND_BACKOFF_MAX_SECONDS,
                max(0.0, self._send_backoff_until - now),
            )
            logger.warning(
                f"发送短冷却中（还剩约 {wait:.1f} 秒），"
                f"等待恢复后重试: [{room_name}]"
            )
            await asyncio.sleep(wait)

        if self._sender_busy.is_set():
            logger.warning(
                "上一次发送仍未返回，先等待发送线程恢复，再尝试本次发送: "
                f"[{room_name}]"
            )
            idle = await self._wait_sender_idle(
                timeout=self.SEND_BACKOFF_MAX_SECONDS,
            )
            if not idle:
                self._note_send_failure("发送线程持续未返回")
                return False

        try:
            async with self._get_send_gate():
                wait = (
                    self._last_send_at
                    + self.SEND_MIN_INTERVAL_SECONDS
                    - time.monotonic()
                )
                if wait > 0:
                    await asyncio.sleep(wait + random.uniform(0.0, 0.4))
                if self.reply_delay > 0:
                    await asyncio.sleep(self.reply_delay)

                success = await asyncio.wait_for(
                    self._enqueue_send(room_name, content),
                    timeout=self.SEND_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.error(
                f"发送消息超时（超过 {self.SEND_TIMEOUT_SECONDS:.1f} 秒），"
                f"已跳过本次卡住的发送: [{room_name}]"
            )
            self._note_send_failure("发送超时")
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            self._note_send_failure(str(e))
            return False

        if not success:
            logger.error(f"发送消息失败: [{room_name}]")
            self._note_send_failure("发送接口返回失败")
            return False

        self._last_send_at = time.monotonic()
        self._send_failures = 0
        self._send_backoff_until = 0.0
        self._remember_sent(room_name, content)
        logger.info(f"消息已发送到 [{room_name}]: {content[:50]}...")
        return True

    async def _wait_sender_idle(self, timeout: float) -> bool:
        """短暂等待串行发送线程恢复，避免一次超时永久挡住后续消息。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._sender_busy.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return not self._sender_busy.is_set()

    def _sent_key(self, room_name: str, content: str) -> tuple[str, str]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return room_name, digest

    def _remember_sent(self, room_name: str, content: str) -> None:
        now = time.monotonic()
        self._recent_sent[self._sent_key(room_name, content)] = now
        if len(self._recent_sent) > 500:
            cutoff = now - 60.0
            self._recent_sent = {
                key: stamp
                for key, stamp in self._recent_sent.items()
                if stamp >= cutoff
            }

    def _mark_send_inflight(self, room_name: str, content: str) -> bool:
        """标记内容正在发送，阻止超时后的同内容重复投递。"""
        key = self._sent_key(room_name, content)
        now = time.monotonic()
        with self._send_inflight_lock:
            if len(self._send_inflight) > 200:
                cutoff = now - self.SEND_ITEM_TTL_SECONDS
                self._send_inflight = {
                    item_key: stamp
                    for item_key, stamp in self._send_inflight.items()
                    if stamp >= cutoff
                }
            if key in self._send_inflight:
                return False
            self._send_inflight[key] = now
            return True

    def _clear_send_inflight(self, room_name: str, content: str) -> None:
        with self._send_inflight_lock:
            self._send_inflight.pop(
                self._sent_key(room_name, content),
                None,
            )

    def _send_is_inflight(self, room_name: str, content: str) -> bool:
        key = self._sent_key(room_name, content)
        with self._send_inflight_lock:
            stamp = self._send_inflight.get(key, 0.0)
            if not stamp:
                return False
            return (
                time.monotonic() - stamp
                <= self.SEND_ITEM_TTL_SECONDS
            )

    def _was_recently_sent(
        self,
        room_name: str,
        content: str,
        max_age: float = 30.0,
    ) -> bool:
        stamp = self._recent_sent.get(self._sent_key(room_name, content), 0.0)
        return bool(
            stamp
            and time.monotonic() - stamp <= max(0.0, float(max_age))
        )

    def _get_send_gate(self) -> asyncio.Lock:
        """惰性创建发送串行锁（避免在无事件循环时构造）。"""
        if self._send_gate is None:
            self._send_gate = asyncio.Lock()
        return self._send_gate

    async def _enqueue_send(self, room_name: str, content: str) -> bool:
        """把发送任务交给专用发送线程，并等待结果。"""
        if not self._accepting_sends:
            logger.debug(f"机器人正在停止，忽略发送任务: [{room_name}]")
            return False
        if not self._mark_send_inflight(room_name, content):
            logger.debug(f"同内容发送已在处理中，忽略重复入队: [{room_name}]")
            return True
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = (
            room_name,
            content,
            time.monotonic() + self.SEND_ITEM_TTL_SECONDS,
            loop,
            future,
        )
        try:
            self._send_queue.put_nowait(item)
        except queue.Full:
            self._clear_send_inflight(room_name, content)
            logger.warning(f"发送队列已满，丢弃本次发送: [{room_name}]")
            return False
        self._ensure_sender_thread()
        return await future

    def _ensure_sender_thread(self) -> None:
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            args=(self._send_queue, self._sender_busy),
            daemon=True,
            name="wechat-sender",
        )
        self._sender_thread.start()

    def _sender_loop(
        self,
        send_queue: queue.Queue,
        busy_event: threading.Event,
    ) -> None:
        """专用发送线程：串行执行 UIA 发送，卡住时只影响后续发送。"""
        while True:
            item = send_queue.get()
            if item is None:
                return
            room_name, content, deadline, loop, future = item
            if time.monotonic() > deadline:
                self._clear_send_inflight(room_name, content)
                logger.warning(f"发送任务已过期，丢弃: [{room_name}]")
                self._set_send_result(loop, future, False)
                continue

            busy_event.set()
            if busy_event is self._sender_busy:
                self._sender_busy_since = time.monotonic()
            try:
                ok = bool(self._send_text_sync(room_name, content))
            except Exception as e:
                if busy_event is self._sender_busy:
                    logger.error(f"发送消息失败: {e}")
                else:
                    logger.debug(f"已隔离的旧发送线程结束: {e}")
                ok = False
            finally:
                busy_event.clear()
                if busy_event is self._sender_busy:
                    self._sender_busy_since = 0.0
            if ok:
                self._remember_sent(room_name, content)
            self._clear_send_inflight(room_name, content)
            self._set_send_result(loop, future, ok)

    @staticmethod
    def _set_send_result(loop, future, ok: bool) -> None:
        def _finish() -> None:
            if future.done():
                return
            future.set_result(ok)

        try:
            loop.call_soon_threadsafe(_finish)
        except Exception:
            pass

    def _note_send_failure(self, reason: str) -> None:
        """发送异常后进入短冷却，下一次回复会重新尝试。"""
        self._send_failures += 1
        backoff = min(
            self.SEND_BACKOFF_BASE_SECONDS * (2 ** (self._send_failures - 1)),
            self.SEND_BACKOFF_MAX_SECONDS,
        )
        self._send_backoff_until = time.monotonic() + backoff
        logger.warning(
            f"微信发送异常（{reason}），{backoff:.0f} 秒后恢复重试"
        )

    def _send_text_sync(self, room_name: str, content: str) -> bool:
        """在线程中执行同步发送，避免阻塞 asyncio 事件循环。"""
        if self._backend == "wechatauto":
            try:
                from wechatauto.utils.lock import LockManager

                with LockManager.acquire():
                    if self._send_text_with_uia(room_name, content):
                        return True
                    if not self.has_usable_uia():
                        logger.debug(
                            "微信 UIA 不可用，使用 OCR 兼容发送路径"
                        )
                    result = self._wx.SendMsg(
                        content,
                        who=room_name,
                        exact=True,
                    )
                    if not result:
                        logger.error(f"发送消息失败: {result}")
                        return False
                    return True
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
                return False

        if self._backend == "wxauto4":
            result = self._wx.SendMsg(content, who=room_name, exact=True)
            if not result:
                logger.error(f"发送消息失败: {result}")
                return False
            return True

        self._wx.SendMsg(content, who=room_name)
        return True

    def _send_text_with_uia(self, room_name: str, content: str) -> bool:
        """优先走纯 UIA 输入发送，绕开截图检测导致的偶发卡死。"""
        if not self._uia_available:
            return False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False

        uia = gui._get_uia()
        if uia is None:
            return False

        try:
            current_chat = uia.current_chat() or ""
            # Only use the fast path when the target is already open. Calling
            # send_text_to here performs a second full search/open flow when
            # the regular SendMsg path will do the same work and is faster.
            if not self._uia_chat_is_current(current_chat, room_name):
                return False
            if hasattr(uia, "ensure_window") and not uia.ensure_window():
                return False
            sent = bool(uia.send_text(content))
            if sent:
                logger.debug(f"已通过 UIA 快路径发送到 [{room_name}]")
            return sent
        except Exception as e:
            logger.debug(f"UIA 快路径发送失败，将回退完整发送流程: {e}")
            return False

    @staticmethod
    def _uia_chat_is_current(current_chat: str, room_name: str) -> bool:
        """识别带语音输入提示后缀的当前会话名。"""
        current_chat = current_chat or ""
        return (
            current_chat == room_name
            or current_chat.startswith(f"{room_name} ")
            or current_chat.startswith(f"{room_name}\u3000")
        )

    async def send_image(
        self,
        room_name: str,
        image_path: str | os.PathLike,
        timeout: float = 15.0,
    ) -> bool | None:
        """发送本地图片，复用发送串行锁，避免与文本发送抢占微信窗口。"""
        path = os.path.abspath(os.fspath(image_path))
        if not os.path.isfile(path):
            logger.error(f"图片文件不存在，无法发送: {path}")
            return False
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送图片")
            return False

        send_gate = self._get_send_gate()
        gate_held = False
        send_task: asyncio.Task | None = None
        try:
            await send_gate.acquire()
            gate_held = True
            wait = (
                self._last_send_at
                + self.SEND_MIN_INTERVAL_SECONDS
                - time.monotonic()
            )
            if wait > 0:
                await asyncio.sleep(wait)
            send_task = asyncio.create_task(
                asyncio.to_thread(
                    self._send_image_sync,
                    room_name,
                    path,
                )
            )
            success = await asyncio.wait_for(
                asyncio.shield(send_task),
                timeout=max(5.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            if send_task is not None:
                asyncio.create_task(
                    self._observe_image_send(
                        send_task,
                        room_name,
                        path,
                        send_gate,
                    )
                )
                # The observer owns the gate until the underlying sync send
                # finishes, so later text cannot race the image paste/send.
                gate_held = False
            logger.warning(
                "发送图片结果暂未确认，已跳过失败兜底，"
                f"后台继续等待: [{room_name}] {path}"
            )
            return None
        except Exception as exc:
            logger.error(f"发送图片失败: [{room_name}] {exc}")
            return False
        finally:
            if gate_held:
                send_gate.release()

        if not success:
            logger.error(f"发送图片失败: [{room_name}] {path}")
            return False
        self._last_send_at = time.monotonic()
        logger.info(f"图片已发送到 [{room_name}]: {path}")
        return True

    async def _observe_image_send(
        self,
        task: asyncio.Task,
        room_name: str,
        image_path: str,
        send_gate: asyncio.Lock | None = None,
    ) -> None:
        """确认超时图片的最终结果，避免后台成功后又补发失败文案。"""
        success = False
        try:
            success = bool(await task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"后台图片发送确认失败: [{room_name}] {image_path}: {exc}"
            )
            return
        finally:
            if send_gate is not None and send_gate.locked():
                send_gate.release()
            if success:
                self._last_send_at = time.monotonic()
                logger.info(
                    "图片已在后台确认发送成功，"
                    f"跳过失败文案: [{room_name}] {image_path}"
                )
            else:
                logger.error(
                    f"图片后台确认发送失败: [{room_name}] {image_path}"
                )

    def _send_image_sync(self, room_name: str, image_path: str) -> bool:
        """Send through clipboard paste first, then fall back to SendFiles()."""
        if self._backend == "wechatauto":
            try:
                from wechatauto.utils.lock import LockManager

                with LockManager.acquire():
                    gui = getattr(self._wx, "_gui", None)
                    if gui is not None and not _ensure_wechat_visible(gui):
                        logger.error(
                            "微信主窗不可见且无法恢复显示，本次图片发送放弃: "
                            f"[{room_name}] {image_path}"
                        )
                        return False
                    send_image = getattr(gui, "send_image", None)
                    if callable(send_image):
                        try:
                            result = send_image(
                                image_path,
                                who=room_name,
                                verify=False,
                            )
                            if result is None or bool(result):
                                logger.info(
                                    "图片已通过剪贴板粘贴发送: "
                                    f"[{room_name}] {image_path}"
                                )
                                return True
                            logger.warning(
                                "剪贴板粘贴发送图片失败，回退 SendFiles: "
                                f"[{room_name}] {result}"
                            )
                        except Exception as exc:
                            logger.warning(
                                "剪贴板粘贴发送图片异常，回退 SendFiles: "
                                f"[{room_name}] {exc}"
                            )

                    result = self._wx.SendFiles(
                        image_path,
                        who=room_name,
                        exact=True,
                    )
                    return result is None or bool(result)
            except Exception as exc:
                logger.error(f"发送图片失败: {exc}")
                return False

        if self._backend == "wxauto4":
            result = self._wx.SendFiles(image_path, who=room_name)
            return result is None or bool(result)

        result = self._wx.SendFiles(image_path, who=room_name)
        return result is None or bool(result)

    async def send_file(
        self,
        room_name: str,
        file_path: str | os.PathLike,
        timeout: float = 45.0,
    ) -> bool:
        """发送本地文件（语音 / 歌曲等），复用发送串行锁避免与文本抢窗口。

        与 ``send_image`` 一样用 ``asyncio.shield`` 托住底层同步发送：超时后
        那个线程并不会被取消（``asyncio.to_thread`` 不可取消），此时若立刻放开
        发送闸门，后面的文本/附件会和它还留在输入框里的粘贴动作抢同一个窗口。
        所以超时后把收尾交给后台观察者，闸门等底层真正结束再放。
        """
        path = os.path.abspath(os.fspath(file_path))
        if not os.path.isfile(path):
            logger.error(f"文件不存在，无法发送: {path}")
            return False
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送文件")
            return False

        try:
            size_mb = os.path.getsize(path) / 1024 / 1024
        except OSError:
            size_mb = 0.0
        # 大附件在微信侧要落盘/转码，固定 45 秒会误判为超时；
        # 按体积放宽，但设上限避免长期占着发送闸门。
        effective_timeout = min(max(float(timeout), 30.0 + size_mb), 180.0)
        logger.info(
            f"开始发送文件: [{room_name}] {os.path.basename(path)} "
            f"({size_mb:.1f} MB，超时 {effective_timeout:.0f}s)"
        )

        send_gate = self._get_send_gate()
        gate_held = False
        send_task: asyncio.Task | None = None
        try:
            await send_gate.acquire()
            gate_held = True
            wait = (
                self._last_send_at
                + self.SEND_MIN_INTERVAL_SECONDS
                - time.monotonic()
            )
            if wait > 0:
                await asyncio.sleep(wait)
            send_task = asyncio.create_task(
                asyncio.to_thread(self._send_file_sync, room_name, path)
            )
            success = await asyncio.wait_for(
                asyncio.shield(send_task),
                timeout=max(5.0, effective_timeout),
            )
        except asyncio.TimeoutError:
            if send_task is not None:
                asyncio.create_task(
                    self._observe_file_send(send_task, room_name, path, send_gate)
                )
                # 观察者接管闸门，直到底层同步发送真正结束。
                gate_held = False
            logger.warning(
                f"发送文件超时（{effective_timeout:.0f}s），后台继续等待结果: "
                f"[{room_name}] {path}"
            )
            return False
        except Exception as exc:
            logger.error(f"发送文件失败: [{room_name}] {exc}")
            return False
        finally:
            if gate_held:
                send_gate.release()

        if not success:
            logger.error(f"发送文件未成功: [{room_name}] {path}")
            return False
        self._last_send_at = time.monotonic()
        logger.info(f"文件已发送到 [{room_name}]: {path}")
        return True

    async def _observe_file_send(
        self,
        task: asyncio.Task,
        room_name: str,
        file_path: str,
        send_gate: asyncio.Lock | None = None,
    ) -> None:
        """收尾超时的文件发送：等底层同步发送真正结束，再放开发送闸门。"""
        success = False
        try:
            success = bool(await task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"后台文件发送确认失败: [{room_name}] {file_path}: {exc}"
            )
            return
        finally:
            if send_gate is not None and send_gate.locked():
                send_gate.release()
            if success:
                self._last_send_at = time.monotonic()
                logger.info(
                    f"文件已在后台确认发送成功: [{room_name}] {file_path}"
                )
            else:
                logger.error(f"文件后台确认发送失败: [{room_name}] {file_path}")

    def _oversize_hint(self, file_path: str) -> str:
        """附件较大时返回一句提示（**只用于告警，绝不拦截发送**），否则返回空串。

        微信的单文件上限**随会话类型变化**：自聊 / 文件传输助手最宽松（高速传输
        可达 10 GB），普通聊天较严（旧版 100 MB）。所以这里只给提示、不替微信
        做决定——拦掉一个本来发得出去的文件，比白试一轮更糟；而发送失败本身
        已经由 :meth:`_confirm_file_sent` 明确报出来了。

        ``FILE_SEND_SIZE_WARN_MB`` <= 0 表示不打这条告警。
        """
        limit = int(getattr(self, "FILE_SEND_SIZE_WARN_MB", 0) or 0)
        if limit <= 0:
            return ""
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return ""
        if size <= limit * 1024 * 1024:
            return ""
        return (
            f"附件 {size / 1048576:.1f} MB 较大（超过常见的 {limit} MB 参考值）；"
            "微信的单文件上限随会话类型变化（自聊/文件传输助手最宽松），"
            "若发送失败多半是微信拒收了大文件"
        )

    def _send_file_sync(self, room_name: str, file_path: str) -> bool:
        """同步发送文件：走 wxauto 的 SendFiles，并在发送后回查数据库确认。

        为什么必须回查：剪贴板粘贴路线（``guia._paste_attachment_and_send``）
        对**非图片**附件是无条件返回成功的——它不检测草稿是否出现，按完回车就
        ``return True``。微信若因体积超限、输入框未就绪等原因没有真的把附件放
        上去，这里既不会抛异常也不会返回失败，只表现为「聊天里什么都没多出来」。
        0.8.7 实测就踩到了：群里 101.6 MB 的视频被静默吞掉，日志却记成功。

        **重试策略**：只有「微信明确拒绝」时才重试——那种情况下附件根本没粘上
        去（典型是输入框定位失败，见 :func:`_install_wechatauto_input_box_guard`），
        重发是安全的。反过来，如果操作执行了、只是**数据库没确认**，那可能只是
        落库慢，重试有重复发送的风险，所以直接判失败、不再重试。

        **体积提示**：附件较大（超过 ``FILE_SEND_SIZE_WARN_MB``）时**只写一条
        告警**，不拦截——微信的上限随会话类型变化，自聊/文件传输助手能收很大的
        文件，拦掉反而会误伤。
        """
        try:
            hint = self._oversize_hint(file_path)
            if hint:
                logger.warning(f"{hint}: [{room_name}] {os.path.basename(file_path)}")
            if self._backend == "wechatauto":
                from wechatauto.utils.lock import LockManager

                gui = getattr(self._wx, "_gui", None)
                if gui is not None and not _ensure_wechat_visible(gui):
                    logger.error(
                        "微信主窗不可见且无法恢复显示，本次附件发送放弃: "
                        f"[{room_name}] {os.path.basename(file_path)}"
                    )
                    return False

                with LockManager.acquire():
                    mode = self._file_send_mode()
                    if mode in ("auto", "dialog"):
                        if self._send_file_via_dialog(room_name, file_path):
                            return True
                        if mode == "dialog":
                            logger.error(
                                "文件对话框路线失败，且配置已禁用剪贴板兜底: "
                                f"[{room_name}] {os.path.basename(file_path)}"
                            )
                            return False
                        logger.warning(
                            "文件对话框路线失败，回退剪贴板粘贴路线: "
                            f"[{room_name}] {os.path.basename(file_path)}"
                        )
                    return self._send_file_via_clipboard(room_name, file_path)
            result = self._wx.SendFiles(file_path, who=room_name)
            return result is None or bool(result)
        except Exception as exc:
            logger.error(f"发送文件异常: {exc}")
            return False

    def _file_send_mode(self) -> str:
        """当前附件发送路线；取值非法时按 ``auto`` 处理。"""
        mode = str(getattr(self, "FILE_SEND_MODE", "auto") or "auto").strip().lower()
        return mode if mode in self.FILE_SEND_MODES else "auto"

    def apply_file_send_mode(self, mode: str) -> None:
        """供适配层按配置热更新附件发送路线。"""
        text = str(mode or "auto").strip().lower()
        if text not in self.FILE_SEND_MODES:
            logger.warning(f"未知的附件发送路线 {mode!r}，按 auto 处理")
            text = "auto"
        if text != getattr(self, "FILE_SEND_MODE", ""):
            logger.info(f"附件发送路线已切换为 {text}")
        self.FILE_SEND_MODE = text

    @staticmethod
    def _click_file_button(uia, gui, center: tuple[int, int]) -> bool:
        """点「发送文件」按钮：优先走 UIA 的点击（会临时摘掉渲染层的
        ``WS_EX_TRANSPARENT``，否则点击会穿透过去），拿不到就退回 ``wx_click``。
        """
        x, y = int(center[0]), int(center[1])
        clicker = getattr(uia, "_click_at", None) if uia is not None else None
        try:
            if callable(clicker):
                clicker(x, y)
            else:
                gui.wx_click(x, y)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"点击「发送文件」按钮失败: {exc}")
            return False

    @staticmethod
    def _wait_file_dialog(allowed_pids=None) -> int:
        """轮询等原生文件对话框出现；超时返回 0。"""
        deadline = time.time() + FILE_DIALOG_TIMEOUT_SECONDS
        while time.time() < deadline:
            dlg = _find_file_dialog(allowed_pids)
            if dlg:
                return dlg
            time.sleep(FILE_DIALOG_POLL_INTERVAL)
        return 0

    def _wechat_pids(self, gui) -> set[int]:
        """已知属于微信的进程号，用于确认弹出的对话框确实是微信的。"""
        pids = {int(p) for p in (getattr(self, "_window_process_ids", None) or ()) if p}
        pid = int(getattr(gui, "pid", 0) or 0)
        if pid:
            pids.add(pid)
        return pids

    @staticmethod
    def _ensure_chat_open(gui, room_name: str) -> bool:
        """确保**目标会话就是当前打开的会话**。

        这一步不能省：剪贴板路线（``guia.send_file``）自带切会话——它先
        ``_open_chat_and_settle(who)`` 再粘贴。而「发送文件」按钮只会把附件塞进
        **当前**会话，不认「目标是谁」。少了这一步，附件就会被发到发消息时恰好
        打开的那个聊天里，比发不出去更糟。

        ``gui.open_chat`` 内部已经优先走 UIA，只有 UIA 不可用时才回落 OCR 侧栏。
        """
        get_input_box = getattr(gui, "get_input_box", None)
        current = getattr(gui, "_current_chat", None)
        if current == room_name and callable(get_input_box) and get_input_box():
            return True
        opener = getattr(gui, "_open_chat_and_settle", None)
        if not callable(opener):
            opener = getattr(gui, "open_chat", None)
        if not callable(opener):
            logger.warning("拿不到切会话的入口，无法保证附件发到目标聊天")
            return False
        try:
            return bool(opener(room_name))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"打开目标会话失败: [{room_name}] {exc}")
            return False

    def _send_file_via_dialog(self, room_name: str, file_path: str) -> bool:
        """原生对话框路线：UIA 点「发送文件」→ ``WM_SETTEXT`` 填路径 → 点「打开」。

        为什么值得单独做一条路线：剪贴板路线要「探输入框（像素）→ 写 CF_HDROP
        → 等微信把原始文件拷进自己的目录 → 按回车」，任何一步时序错位都会静默
        失败，而且它依赖像素探测，窗口一被缩到托盘就全盘失效。这条路**全程
        不碰像素**：按钮坐标由 UIA 直接给出，对话框是系统标准控件，路径用
        ``WM_SETTEXT`` 一次写入。实测约 1 秒完成、0 次截屏、0 次 OCR。

        返回 ``False`` 表示「这条路线没走通」，调用方（``auto`` 模式）会回退到
        剪贴板路线——所以这里的失败日志一律用 ``warning`` 而不是 ``error``。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            logger.warning("拿不到 WeChatGUI，无法走文件对话框路线")
            return False
        # 窗口缩在托盘里时点击会落到别的程序上，必须先确保它真的可见。
        if not _ensure_wechat_visible(gui):
            logger.warning("微信主窗不可见，放弃文件对话框路线")
            return False
        # 「发送文件」按钮只作用于当前会话，必须先切到目标会话。
        if not self._ensure_chat_open(gui, room_name):
            logger.warning(f"无法打开目标会话，放弃文件对话框路线: [{room_name}]")
            return False

        uia = None
        try:
            uia = gui._get_uia()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"获取 UIA 失败: {exc}")
        center = _uia_find_button_center(uia, FILE_BUTTON_NAMES)
        if center is None:
            logger.warning("在微信界面上找不到「发送文件」按钮，本路线不可用")
            return False

        # 清掉可能残留的旧对话框（上一轮超时留下的），避免把路径填进错的窗口。
        # 只认微信进程的窗口——别的程序也会弹同名的「打开」对话框。
        pids = self._wechat_pids(gui) or None
        stale = _find_file_dialog(pids)
        if stale:
            logger.warning("发现残留的文件对话框，先关掉再继续")
            _close_file_dialog(stale)
            time.sleep(0.3)

        before_seq = self._target_seq(room_name)
        if not self._click_file_button(uia, gui, center):
            return False

        dlg = self._wait_file_dialog(pids)
        if not dlg:
            logger.warning("点了「发送文件」但原生对话框没有出现（点击可能没命中）")
            return False

        edit = _dialog_edit(dlg)
        if not edit:
            logger.warning("文件对话框里找不到文件名输入框")
            _close_file_dialog(dlg)
            return False
        if not _set_dialog_text(edit, file_path):
            logger.warning("往文件对话框写入路径失败（读回校验不一致）")
            _close_file_dialog(dlg)
            return False

        ok = (_dialog_child_by_id(dlg, FILE_DIALOG_OK_ID, "Button")
              or _dialog_child_by_id(dlg, FILE_DIALOG_OK_ID))
        if not ok or not _click_dialog_control(ok):
            logger.warning("文件对话框里找不到「打开」按钮")
            _close_file_dialog(dlg)
            return False
        logger.info(
            f"已通过文件对话框提交附件: [{room_name}] "
            f"{os.path.basename(file_path)}"
        )

        if self._confirm_file_sent(room_name, before_seq):
            return True
        # 微信把附件放进了输入框但没自动发出去 → 补按回车（草稿还在就发出，
        # 输入框已空则回车无副作用，不会重复发送）。
        if self._nudge_enter_until_confirmed(room_name, before_seq):
            logger.info(
                f"补按回车后发送成功（对话框路线）: [{room_name}] "
                f"{os.path.basename(file_path)}"
            )
            return True
        logger.warning(
            "文件对话框路线未在数据库中得到确认: "
            f"[{room_name}] {os.path.basename(file_path)}"
        )
        return False

    def _send_file_via_clipboard(self, room_name: str, file_path: str) -> bool:
        """剪贴板粘贴路线（0.8.9 及以前的行为）：``SendFiles`` + 数据库回查确认。

        **调用方必须已经持有** ``LockManager`` 与发送闸门。

        重试策略：只有「微信明确拒绝」时才重试——那种情况下附件根本没粘上去
        （典型是输入框定位失败，见 :func:`_install_wechatauto_input_box_guard`），
        重发是安全的。反过来，如果操作执行了、只是**数据库没确认**，那可能只是
        落库慢，重试有重复发送的风险，所以直接判失败、不再重试。
        """
        reason = ""
        for attempt in range(1, self.FILE_SEND_ATTEMPTS + 1):
            self._reset_layout_recalibration()
            before_seq = self._target_seq(room_name)
            result = self._wx.SendFiles(
                file_path,
                who=room_name,
                exact=True,
            )
            if not (result is None or bool(result)):
                # 明确被拒：没有粘贴成功，重试安全。
                reason = f"微信拒绝了本次发送：{result}"
                logger.warning(
                    f"发送文件被拒绝（第 {attempt}/"
                    f"{self.FILE_SEND_ATTEMPTS} 次）: [{room_name}] "
                    f"{os.path.basename(file_path)}: {result}"
                )
                continue
            if self._confirm_file_sent(room_name, before_seq):
                if attempt > 1:
                    logger.info(
                        f"第 {attempt} 次尝试后发送成功: "
                        f"[{room_name}] {os.path.basename(file_path)}"
                    )
                return True
            # 没确认：最可能是「附件已经粘进输入框，但回车被微信吞了」
            # （大文件粘贴后微信要先拷原始文件、生成卡片，这期间的回车
            # 会被丢掉，草稿会一直留在输入框里）。
            # 补按回车是安全的：草稿还在就发出去，输入框已空则回车
            # 什么也不做，所以**不会重复发送**——这也是这里不像
            # 「明确被拒」那样整条重来的原因。
            if self._nudge_enter_until_confirmed(room_name, before_seq):
                logger.info(
                    f"补按回车后发送成功: [{room_name}] "
                    f"{os.path.basename(file_path)}"
                )
                return True
            logger.error(
                "发送文件未在数据库中得到确认，微信很可能没有真正收下"
                f"这个附件（输入框未就绪 / 微信拒收）: [{room_name}] "
                f"{os.path.basename(file_path)}"
            )
            return False
        logger.error(
            f"发送文件失败（已尝试 {self.FILE_SEND_ATTEMPTS} 次）"
            f"[{room_name}] {os.path.basename(file_path)}：{reason}"
        )
        return False

    def _nudge_enter_until_confirmed(self, room_name: str, before_seq: int) -> bool:
        """补按回车，直到数据库确认附件落地（最多 ``FILE_SEND_ENTER_NUDGES`` 次）。

        为什么要补：微信粘贴大文件后要先把**原始文件**拷进自己的视频目录并生成
        卡片，这期间按下的回车会被丢掉——附件会一直留在输入框里（实测那条
        101.6 MB 的视频在输入框里躺了两个小时，直到人工点「发送」才发出去）。
        粘贴本身是成功的，缺的只是「再按一次回车」。

        安全性：只按回车、**不重新粘贴**。草稿还在就把附件发出去；输入框已经
        空了（说明上一次其实已经发出去）回车什么也不做，所以不存在重复发送。
        定位不到输入框时**不按**——回车会落到当前拥有焦点的窗口上，那更危险。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        for nudge in range(1, self.FILE_SEND_ENTER_NUDGES + 1):
            time.sleep(self.FILE_SEND_ENTER_NUDGE_INTERVAL)
            if not self._press_enter(gui):
                return False
            logger.warning(
                f"发送未确认，已补按回车（第 {nudge}/"
                f"{self.FILE_SEND_ENTER_NUDGES} 次）: [{room_name}]"
            )
            if self._confirm_file_sent(room_name, before_seq):
                return True
        return False

    @staticmethod
    def _press_enter(gui) -> bool:
        """把焦点放回输入框再按一次回车；定位不到输入框就不按。"""
        try:
            from wechatauto.guia import VK_RETURN

            box = gui.get_input_box()
            if not box:
                logger.warning("补按回车前定位不到输入框，跳过（不盲按回车）")
                return False
            if not gui.focus_input(box):
                logger.warning("补按回车前无法聚焦输入框，跳过")
                return False
            gui._input.key(VK_RETURN)
            return True
        except Exception as exc:  # noqa: BLE001 - 尽力而为，不因此判失败
            logger.debug(f"补按回车失败（忽略）: {exc}")
            return False

    def _reset_layout_recalibration(self) -> None:
        """让 wechatauto 在输入框探测再次失败时重新校准布局。

        ``guia.get_input_box()`` 探测连续失败时会自动 ``calibrate_layout()``，
        但该动作被 ``_auto_recalibrated`` 限制成「**每会话只触发一次**」。实测
        0.8.7：08:23 第一次失败靠它救回来了（校准后粘贴成功），08:27 第二次失败
        时标志已置位，于是直接回退到按 ``render_h`` 比例算出来的**猜测坐标**，
        粘贴就可能落空——而那条路不报错，只表现为「聊天里什么都没多出来」。

        发送附件前把这个标志复位，等于每次附件发送都重新保留
        「探测失败 → 重新校准」这条兜底路径。探测正常时不会触发校准，
        所以不增加额外开销。第三方内部属性，取不到就安静跳过。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None or not getattr(gui, "_auto_recalibrated", False):
            return
        try:
            gui._auto_recalibrated = False
        except Exception as exc:  # pragma: no cover - 属性只读时忽略
            logger.debug(f"复位布局重校准标志失败: {exc}")

    def _resolve_db_user(self, room_name: str) -> str:
        """显示名 → 数据库里的会话 username；查不到就按原名试。"""
        db = getattr(self._wx, "_db", None)
        if db is None:
            return room_name
        try:
            hits = db.search_contact(room_name)
        except Exception as exc:
            logger.debug(f"按显示名反查会话失败: [{room_name}] {exc}")
            return room_name
        if hits:
            return hits[0].get("username") or room_name
        return room_name

    def _target_seq(self, room_name: str) -> int:
        """取目标会话当前最大 sort_seq，作为发送后确认的基线。

        取不到时返回 ``-1``（区别于「会话为空」的 0），调用方据此跳过确认，
        避免在拿不到水位时把历史附件误判成本次发送的结果。
        """
        db = getattr(self._wx, "_db", None)
        if db is None:
            return -1
        try:
            rows = db.get_messages(self._resolve_db_user(room_name), limit=1)
        except Exception as exc:
            logger.debug(f"读取会话水位失败: [{room_name}] {exc}")
            return -1
        return rows[0].get("sort_seq", 0) if rows else 0

    def _confirm_file_sent(
        self,
        room_name: str,
        before_seq: int,
        timeout: float | None = None,
    ) -> bool:
        """轮询数据库，确认这次发送真的产生了一条附件消息。

        视频消息的正文里**没有文件名**（只有 ``<videomsg>`` 的 aeskey/md5），
        无法像文件那样按名字匹配，所以只能像图片一样靠「新出现的、自己发出的
        附件类消息」判断。发送闸门保证了同一时刻只有一条附件在发，不会误判。

        返回 False 表示「明确没发出去」；数据库不可用或水位拿不到时返回 True
        （无法确认 ≠ 发送失败，不能因此阻断发送）。
        """
        db = getattr(self._wx, "_db", None)
        if db is None or before_seq < 0:
            return True
        user = self._resolve_db_user(room_name)
        if timeout is None:
            timeout = self.FILE_CONFIRM_TIMEOUT_SECONDS
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                rows = db.get_new_messages(user, since_seq=before_seq, limit=8)
            except Exception as exc:
                logger.debug(f"确认附件落库失败: [{room_name}] {exc}")
                return True
            for row in rows:
                if row.get("sender_id") != 2:
                    continue
                if row.get("type") in self.ATTACHMENT_MSG_TYPES:
                    logger.debug(
                        f"已确认附件落库: [{room_name}] {row.get('type')}"
                    )
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    async def send_random_favorite_sticker(
        self,
        room_name: str,
        preferred_only: bool = False,
        limit: int = 3,
        is_group: bool = False,
    ) -> bool:
        """从微信收藏表情中随机点击一个表情，发到「当前打开的会话」。

        不做跨窗口切换：目标不是当前聊天窗口时直接返回 False。
        is_group 仅为 API 兼容保留，不影响发送逻辑。
        """
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送收藏表情")
            return False
        if self._backend != "wechatauto":
            logger.warning("当前后端不支持收藏表情 UIA 发送，仅 wechatauto 可用")
            return False
        if not self._uia_available:
            logger.debug("当前微信窗口为 OCR 兼容模式，暂无 UIA 收藏表情入口")
            return False
        if not getattr(self._wx, "_gui", None):
            logger.warning("微信 GUI 对象不可用，无法发送收藏表情")
            return False

        return await asyncio.to_thread(
            self._send_random_favorite_sticker_sync,
            room_name,
            preferred_only,
            limit,
            is_group,
        )

    def _send_random_favorite_sticker_sync(
        self,
        room_name: str,
        preferred_only: bool = False,
        limit: int = 3,
        is_group: bool = False,
    ) -> bool:
        """在线程中执行 UIA 点击，调用方负责等待。

        is_group 仅为 API 兼容保留；收藏表情只在当前打开的会话发送，
        目标不是当前窗口时跳过、不切换窗口。
        """
        try:
            from wechatauto.utils.lock import LockManager
        except Exception as e:
            logger.error(f"加载微信自动化锁失败: {e}")
            return False

        with LockManager.acquire():
            gui = self._wx._gui
            try:
                if not gui.ensure_visible():
                    logger.warning("微信窗口不可见，无法发送收藏表情")
                    return False

                uia = gui._get_uia()
                if uia is None:
                    logger.warning("微信 UIA 不可用，无法定位收藏表情")
                    return False
                if hasattr(uia, "ensure_window") and not uia.ensure_window():
                    logger.warning("微信 UIA 窗口未就绪，无法发送收藏表情")
                    return False
                # 收藏表情只在「当前已打开的聊天窗口」发送，不通过 open_chat
                # 跳转到其他窗口（微信风控会把跨窗口跳转识别为风险行为而拒绝）。
                # 目标不是当前会话就直接跳过，绝不切窗口。
                current_chat = uia.current_chat() or ""
                if not self._uia_chat_is_current(current_chat, room_name):
                    logger.debug(
                        f"目标会话 [{room_name}] 不是当前打开的窗口，"
                        f"跳过发送收藏表情（不切换窗口）"
                    )
                    return False

                window = getattr(uia, "_win", None)
                if window is None:
                    logger.warning("UIA 窗口句柄不存在，无法发送收藏表情")
                    return False

                launcher = self._find_sticker_launcher(window)
                if launcher is None:
                    logger.warning("未找到微信输入框的表情按钮")
                    return False
                panel = self._find_sticker_panel(window)
                if panel is None:
                    if not self._click_uia_control(uia, launcher):
                        logger.warning("点击微信表情按钮失败")
                        return False

                    time.sleep(0.8)
                    panel = self._find_sticker_panel(window)
                    if panel is None:
                        logger.warning("表情面板未打开或未被 UIA 暴露")
                        return False

                items = self._find_favorite_items(
                    panel,
                    include_generic=False,
                )
                clicked_favorite_tab = False
                if not items:
                    favorite_tab = self._find_favorite_tab(panel)
                    if favorite_tab is not None:
                        clicked_favorite_tab = self._click_uia_control(
                            uia,
                            favorite_tab,
                        )
                        if clicked_favorite_tab:
                            time.sleep(0.8)
                            panel = self._find_sticker_panel(window) or panel
                            items = self._find_favorite_items(
                                panel,
                                include_generic=True,
                            )
                if not items:
                    logger.warning("收藏表情面板中没有可点击的可见表情")
                    return False

                items.sort(key=_control_bounds)
                if preferred_only:
                    items = items[:max(1, int(limit))]
                chosen = random.choice(items)
                if not self._click_uia_control(uia, chosen):
                    logger.warning("点击收藏表情失败")
                    return False

                time.sleep(0.5)
                logger.info(f"已从收藏表情中随机发送一个表情到 [{room_name}]")
                return True
            except Exception as e:
                logger.error(f"发送收藏表情失败: {e}")
                return False

    @staticmethod
    def _find_sticker_launcher(window):
        """在聊天输入工具栏中定位表情入口按钮。"""
        blocked_classes = (
            "emoticonpanel",
            "emoticongridview",
            "emoticoncontentview",
            "favemoticonitemview",
            "expressioncollectitem",
            "emoticontoolbaritem",
        )
        toolbar_classes = (
            "chatinputtoolbarleftview",
            "chatinputtoolbarrightview",
            "chatinputview",
        )

        def is_launcher(control, allow_class_match: bool = True) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            automation_id = (getattr(control, "AutomationId", "") or "").lower()
            name = (getattr(control, "Name", "") or "").strip()
            if any(token in control_class for token in blocked_classes):
                return False
            if _control_bounds(control) is None:
                return False

            haystack = f"{control_class} {automation_id} {name}".lower()
            if "发送表情" in name or name in {"表情", "表情包"}:
                return True
            if "chat_text_emoticon_view" in haystack:
                return True
            return allow_class_match and control_class == "mmui::emoticonview"

        roots = _find_uia_controls(
            window,
            lambda control: any(
                token in (getattr(control, "ClassName", "") or "").lower()
                for token in toolbar_classes
            ),
            max_depth=30,
        )
        roots.append(window)

        for root in roots:
            matches = _find_uia_controls(root, is_launcher, max_depth=16)
            if matches:
                matches.sort(key=lambda control: _control_bounds(control))
                return matches[0]

        exact_matches = _find_uia_controls(
            window,
            lambda control: is_launcher(control, allow_class_match=False),
            max_depth=30,
        )
        if exact_matches:
            exact_matches.sort(key=lambda control: _control_bounds(control))
            return exact_matches[0]
        return None

    @staticmethod
    def _find_sticker_panel(window):
        """定位当前展开的微信表情面板或弹窗。"""
        matches = _find_uia_controls(
            window,
            lambda control: (
                (getattr(control, "ClassName", "") or "").lower()
                in {"mmui::emoticonpanel", "mmui::emoticonpopover"}
                or "emoticon_panel" in (
                    getattr(control, "AutomationId", "") or ""
                ).lower()
            )
            and _control_bounds(control) is not None,
            max_depth=30,
        )
        if matches:
            return matches[0]

        try:
            import uiautomation as auto

            for popup in auto.GetRootControl().GetChildren():
                class_name = (getattr(popup, "ClassName", "") or "").lower()
                if class_name in {
                    "mmui::emoticonpopover",
                    "mmui::emoticonpanel",
                }:
                    return popup
        except Exception:
            pass
        return None

    @staticmethod
    def _find_favorite_tab(panel):
        """定位表情面板中的收藏标签。"""
        def is_favorite_tab(control) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            if control_class != "mmui::emoticontoolbaritem":
                return False
            if _control_bounds(control) is None:
                return False
            haystack = (
                f"{getattr(control, 'Name', '') or ''} "
                f"{getattr(control, 'AutomationId', '') or ''}"
            ).lower()
            return (
                "收藏" in haystack
                or "自定义表情" in haystack
                or "fav" in haystack
                or "favorite" in haystack
            )

        matches = _find_uia_controls(panel, is_favorite_tab, max_depth=12)
        return matches[0] if matches else None

    @staticmethod
    def _find_favorite_items(panel, include_generic: bool = False) -> list:
        """收集收藏表情面板内可见、尺寸正常的表情项。"""
        def is_favorite_item(control) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            automation_id = (getattr(control, "AutomationId", "") or "").lower()
            if not (
                control_class == "mmui::favemoticonitemview"
                or "favemoticon" in automation_id
                or (
                    include_generic
                    and control_class in {"mmui::emoticonitemview", "mmui::commonemoticonview"}
                )
            ):
                return False

            bounds = _control_bounds(control)
            if bounds is None:
                return False
            _, _, right, bottom = bounds
            left, top, _, _ = bounds
            width, height = right - left, bottom - top
            return 18 <= width <= 220 and 18 <= height <= 220

        return _find_uia_controls(panel, is_favorite_item, max_depth=24)

    @staticmethod
    def _click_uia_control(uia, control) -> bool:
        """优先使用 wechatauto 的真实鼠标点击，失败才调用控件默认点击。"""
        try:
            if hasattr(uia, "_click_ctrl"):
                return bool(uia._click_ctrl(control))
        except Exception:
            pass
        try:
            control.Click()
            return True
        except Exception:
            return False

    async def disconnect(self) -> None:
        """断开微信连接（带超时，避免停止机器人时卡死）"""
        self._running = False
        self._accepting_sends = False
        self._drain_send_queue()
        if self._sender_busy.is_set():
            if not await self._wait_sender_idle(timeout=4.0):
                self.abandon_stuck_sender()
        wx = self._wx
        if wx:
            def _teardown() -> None:
                if self._listen_all_active:
                    try:
                        wx.RemoveListenAll()
                    except Exception:
                        pass
                if self._backend in {"wxauto4", "wechatauto"}:
                    try:
                        wx.StopListening(remove=False)
                    except Exception:
                        pass
                registered_groups = (
                    self._registered_listen_keys_by_group
                    or self._listen_keys_by_group
                )
                for group, listen_key in registered_groups.items():
                    try:
                        if self._backend in {"wxauto4", "wechatauto"}:
                            wx.RemoveListenChat(
                                listen_key,
                                close_window=False,
                            )
                        else:
                            wx.RemoveListenChat(who=group)
                    except Exception:
                        pass
                self._registered_listen_keys_by_group.clear()

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_teardown),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                logger.warning("断开微信监听超时，已放弃等待（不影响重新启动）")
            except Exception as e:
                logger.warning(f"断开微信监听异常: {e}")
        self._listen_all_active = False
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("微信连接已断开")

    def _drain_send_queue(self) -> None:
        """Resolve queued sends as cancelled while the bot is stopping."""
        while True:
            try:
                item = self._send_queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            try:
                _, room_name, _, loop, future = item
                self._set_send_result(loop, future, False)
                logger.debug(f"停止时取消排队消息: [{room_name}]")
            except Exception:
                continue

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status.value,
            "target_groups": self.target_groups,
            "listen_private": self.listen_private,
        }
