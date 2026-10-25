"""Windows 底层操作：全局滚轮投递、光标定位、DPI 感知、ESC 中断检测。

全部通过 ctypes 调用 user32，不依赖 pywin32。

设计要点：
滚轮事件是**全局投递**到「光标下方窗口」的，不是投给某个句柄。
这正好满足本项目「任意界面」的要求：不关心目标程序是浏览器、网盘客户端
还是 PDF 阅读器，只要光标停在它上面，滚轮就作用在它身上。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

WHEEL_DELTA = 120
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE = 0
VK_ESCAPE = 0x1B


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def enable_dpi_awareness() -> None:
    """声明进程为 DPI 感知。

    不声明的话，在 125%/150% 缩放的系统上，mss 取到的是缩放后的位图，
    而 tkinter 框选拿到的是逻辑坐标，两者不在同一坐标系，框出来的区域会漂移。
    """
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def wheel(steps: int) -> None:
    """投递一次滚轮事件。

    steps > 0 向上滚，steps < 0 向下滚，绝对值 = 格数。
    """
    if steps == 0:
        return
    data = ctypes.c_uint32((int(steps) * WHEEL_DELTA) & 0xFFFFFFFF).value
    inp = INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, None)),
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise OSError(f"滚轮投递失败 (SendInput={sent}, err={ctypes.get_last_error()})")


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise OSError("GetCursorPos 失败")
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def esc_pressed() -> bool:
    """轮询 ESC 是否被按下（用于中途中断采集）。"""
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
