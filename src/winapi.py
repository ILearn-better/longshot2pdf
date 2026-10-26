"""Windows 底层操作：全局滚轮投递、光标定位、虚拟桌面范围、DPI 缩放、ESC 中断检测。

全部通过 ctypes 调用 user32 / gdi32，不依赖 pywin32。

设计要点
--------
1) 滚轮事件是**全局投递**到「光标下方窗口」的，不是投给某个句柄。
   这正好满足本项目「任意界面」的要求：不关心目标程序是浏览器、网盘客户端
   还是 PDF 阅读器，只要光标停在它上面，滚轮就作用在它身上。

2) 坐标一律是**物理像素**，坐标系是 Windows 的「虚拟桌面」。
   多显示器时左侧副屏的 x 可能是负数，右侧副屏可能超出主屏宽，
   所以任何边界判断都必须用 ``virtual_screen()``，不能假设 ``left/top >= 0``。

3) 除 ``wheel()`` / ``get_cursor_pos()`` 这两个「拿不到就是致命错误」的函数外，
   本模块所有函数都不抛异常：跨平台、受限环境、远程会话里拿不到系统信息时
   返回保守默认值，调用方按正常流程继续即可（宁可退化成单屏假设，也不要崩）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

try:  # 非 Windows 平台或受限环境：模块仍可 import，动作类函数退化为空操作
    user32 = ctypes.WinDLL("user32", use_last_error=True)
except Exception:  # pragma: no cover - 只在非 Windows 触发
    user32 = None  # type: ignore[assignment]

try:
    gdi32 = ctypes.WinDLL("gdi32")
except Exception:  # pragma: no cover
    gdi32 = None  # type: ignore[assignment]

WHEEL_DELTA = 120
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE = 0
VK_ESCAPE = 0x1B
VK_F9 = 0x78  # 「连续截图整合」的抓屏快捷键（F9 在浏览器/Office 里都没有默认动作）

# GetSystemMetrics 索引
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# GetDeviceCaps 索引
LOGPIXELSX = 88

# 连 GetSystemMetrics 都拿不到时的兜底屏幕（宁可给大一点，误裁比越界安全）
_FALLBACK_SCREEN = (0, 0, 1920, 1080)


# ------------------------------------------------------------------ 结构体
#
# 64 位对齐说明（这是 SendInput 最容易踩的坑）：
#
#   Win32 头文件里 INPUT 是
#       typedef struct tagINPUT {
#           DWORD type;                       // 4 字节
#           union { MOUSEINPUT mi; KEYBDINPUT ki; HARDWAREINPUT hi; } DUMMYUNIONNAME;
#       } INPUT;
#
#   64 位下 MOUSEINPUT = { LONG dx; LONG dy; DWORD mouseData; DWORD dwFlags;
#                          DWORD time; ULONG_PTR dwExtraInfo; }
#   -> dx 0, dy 4, mouseData 8, dwFlags 12, time 16, 之后有 4 字节填充，
#      dwExtraInfo 位于偏移 24，占 8 字节 => sizeof(MOUSEINPUT) = 32。
#   union 大小取最大成员，最大成员正是 MOUSEINPUT(32)，所以
#      sizeof(INPUT) = 4(type) + 4(padding) + 32(union) = 40。
#
#   本文件把 dwExtraInfo 声明为 ``ctypes.c_void_p``（指针，64 位下 8 字节、
#   8 字节对齐），ctypes 自己算出来的 sizeof(MOUSEINPUT)/sizeof(INPUT)
#   就与官方头文件完全一致，不需要（也不允许）再手写 padding。
#   反过来，如果把 dwExtraInfo 写成 DWORD，ctypes 会算出 28 字节的 INPUT，
#   而内核按 40 字节读取，会越界读到栈上的脏数据 —— 滚轮就会"时灵时不灵"。
#
#   这里只声明了 union 的 mi 成员（本项目只投递滚轮），这在 ABI 上是安全的：
#   union 的尺寸由最大成员决定，缺少 ki/hi 不改变 sizeof(INPUT)。
#   keystat（KEYBDINPUT）留给以后需要投键盘事件时再补，补的时候不要动
#   dwExtraInfo 的 c_void_p 声明。
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),  # ULONG_PTR，必须是指针宽度
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]  # 见上方注释：省略 ki/hi 不改变 ABI


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if user32 is not None:
    try:
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
    except Exception:  # pragma: no cover
        pass


# ------------------------------------------------------------------ DPI


def enable_dpi_awareness() -> None:
    """声明进程为 DPI 感知（尽量拿到最高的那一档）。

    不声明的话，在 125%/150% 缩放的系统上，mss 取到的是缩放后的位图，
    而 tkinter 框选拿到的是逻辑坐标，两者不在同一坐标系，框出来的区域会漂移。

    三档依次尝试，越靠前越好：

    ==============================================  ==================
    ``SetProcessDpiAwarenessContext(-4)``           Per-Monitor V2（Win10 1703+）
    ``SetProcessDpiAwareness(2)``                   Per-Monitor V1（Win8.1+）
    ``SetProcessDPIAware()``                        System DPI Aware（Vista+）
    ==============================================  ==================

    为什么非要争 V2：只有 V2 才能保证「每个显示器都按它自己的物理像素出图」。
    停在 V1 时，跨不同缩放的副屏上抓到的图会被系统位图拉伸一次，
    表现就是**像素偏低、文字发虚** —— 这不是缩放算法的问题，源头就没拿到足够的像素。
    """
    ctx = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        if user32 is not None:
            fn = user32.SetProcessDpiAwarenessContext
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = wintypes.BOOL
            if fn(ctypes.c_void_p(ctx)):
                return
    except Exception:
        pass  # 老系统没这个符号，或用清单已固定过，都往下退
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        if user32 is not None:
            user32.SetProcessDPIAware()
    except Exception:
        pass


def _device_dpi() -> int:
    """用 GDI 取主显示器 DPI（GetDpiForSystem 不可用时的回退）。"""
    hdc = None
    try:
        if user32 is not None and gdi32 is not None:
            hdc = user32.GetDC(None)
            if hdc:
                return int(gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
    except Exception:
        pass
    finally:
        try:
            if hdc and user32 is not None:
                user32.ReleaseDC(None, hdc)
        except Exception:
            pass
    return 0


def get_dpi_scale() -> float:
    """主显示器的缩放比例（1.0 = 100%，1.5 = 150%）。

    优先 ``GetDpiForSystem``（Win10 1607+），失败回退 ``GetDeviceCaps(LOGPIXELSX)/96``。
    两个都拿不到时返回 1.0（当作 100%，调用方按未缩放处理）。

    为什么要它：mss 拿到的是物理像素，tkinter 拿的是逻辑像素，
    两者换算需要这个比例；另外日志里报「系统缩放 150%」能省掉一半的排障时间。
    """
    dpi = 0
    fn = getattr(user32, "GetDpiForSystem", None) if user32 is not None else None
    if fn is not None:
        try:
            fn.restype = wintypes.UINT
            fn.argtypes = ()
            dpi = int(fn())
        except Exception:
            dpi = 0
    if dpi <= 0:
        dpi = _device_dpi()
    if dpi <= 0:
        return 1.0
    # 兜底夹紧：远程桌面/虚拟显示偶尔返回 0 或离谱值
    return max(0.5, min(4.0, dpi / 96.0))


# ------------------------------------------------------------------ 屏幕范围


def available() -> bool:
    """本模块的动作能不能真的执行（Windows 上是 True，其它平台 False）。

    调用方用它来选「问 Win32」还是「问 mss」：非 Windows 上
    ``virtual_screen()`` 会返回 (0,0,0,0)，此时应当改用 mss.monitors[0]，
    而不是把 0 当成屏幕尺寸。
    """
    return user32 is not None


def _metric(index: int, default: int = 0) -> int:
    if user32 is None:
        return default
    try:
        return int(user32.GetSystemMetrics(index))
    except Exception:
        return default


def virtual_screen() -> tuple[int, int, int, int]:
    """多显示器合并后的虚拟桌面范围 (x, y, w, h)，单位物理像素。

    副屏在主屏左边时 x 为负（例如 (-1920, 0, 3840, 1080)），
    副屏在上边时 y 为负。调用方**不能**假设返回值的 x/y 是 0。

    非 Windows 平台返回 ``(0, 0, 0, 0)`` —— 表示「问不到」，不是「屏幕是 0×0」。
    这里**不能**退回 1920×1080：那会把 macOS/Linux 上的宽屏钉死在左上角一块，
    2560 宽的屏幕右半边永远截不到。正确的做法是让调用方改问 mss.monitors[0]
    （见 :func:`src.capture.normalize_region`）。
    """
    x = _metric(SM_XVIRTUALSCREEN, 0)
    y = _metric(SM_YVIRTUALSCREEN, 0)
    w = _metric(SM_CXVIRTUALSCREEN, 0)
    h = _metric(SM_CYVIRTUALSCREEN, 0)
    if w > 0 and h > 0:
        return x, y, w, h

    # 回退 1：老系统只给主屏尺寸（退回单屏假设，但至少不是 0）
    pw = _metric(SM_CXSCREEN, 0)
    ph = _metric(SM_CYSCREEN, 0)
    if pw > 0 and ph > 0:
        return 0, 0, pw, ph

    if user32 is None:
        return 0, 0, 0, 0  # 非 Windows：交给调用方去问 mss

    # 回退 2：Windows 但完全拿不到（受限会话 / 服务进程）
    return _FALLBACK_SCREEN


def screen_size() -> tuple[int, int]:
    """屏幕可用范围 (w, h)。

    历史上返回主显示器尺寸；现在基于 :func:`virtual_screen` 实现，
    多显示器时返回**合并后**的范围 —— 因为这是「区域不能超出哪里」的正确边界。
    单显示器下与旧行为完全等价。
    """
    _, _, w, h = virtual_screen()
    return w, h


# ------------------------------------------------------------------ 光标


def get_cursor_pos() -> tuple[int, int]:
    """当前光标位置（物理像素）。

    唯一会抛异常的函数：拿不到光标位置说明 Win32 会话异常，
    静默返回 (0, 0) 会让后续采集滚到屏幕左上角，比直接失败更糟。
    需要「不抛异常」的语义请用 :func:`save_cursor`。
    """
    pt = wintypes.POINT()
    if user32 is None or not user32.GetCursorPos(ctypes.byref(pt)):
        raise OSError("GetCursorPos 失败")
    return int(pt.x), int(pt.y)


def set_cursor_pos(x: int, y: int) -> bool:
    """把光标移到 (x, y)，支持负坐标（副屏）。失败返回 False，不抛异常。"""
    if user32 is None:
        return False
    try:
        return bool(user32.SetCursorPos(int(x), int(y)))
    except Exception:
        return False


_saved_cursor: tuple[int, int] | None = None


def save_cursor() -> tuple[int, int] | None:
    """记住当前光标位置，供采集结束后还原。

    不抛异常：拿不到就返回 None，``restore_cursor()`` 会自动变成空操作。
    """
    global _saved_cursor
    try:
        _saved_cursor = get_cursor_pos()
    except Exception:
        _saved_cursor = None
    return _saved_cursor


def restore_cursor(pos: tuple[int, int] | None = None) -> bool:
    """把光标还原到上次 :func:`save_cursor` 的位置（或显式传入的位置）。

    采集过程必须把光标钉在滚动点上，否则滚轮会投递到别的窗口；
    采集结束若不还原，用户的鼠标会莫名其妙留在画面中间。
    返回是否还原成功。
    """
    global _saved_cursor
    target = pos if pos is not None else _saved_cursor
    if target is None:
        return False
    ok = set_cursor_pos(*target)
    _saved_cursor = None
    return ok


# ------------------------------------------------------------------ 输入


def wheel(steps: int) -> None:
    """投递一次滚轮事件。

    steps > 0 向上滚，steps < 0 向下滚，绝对值 = 格数。
    """
    if steps == 0:
        return
    if user32 is None:
        raise OSError("当前环境无 user32，无法投递滚轮")
    data = ctypes.c_uint32((int(steps) * WHEEL_DELTA) & 0xFFFFFFFF).value
    inp = INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, None)),
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise OSError(f"滚轮投递失败 (SendInput={sent}, err={ctypes.get_last_error()})")


def key_down(vk: int) -> bool:
    """轮询某个虚拟键当前是否被按住（全局，与窗口焦点无关）。

    为什么用轮询而不是 RegisterHotKey：「连续截图整合」面板在抓屏前会把自己
    最小化，那时 tkinter 的 bind 收不到任何键盘事件；而 GetAsyncKeyState 是
    查全局键状态，焦点在浏览器上时照样能问到。项目里的 ESC 中断本来就是这个
    机制（见 esc_pressed），复用同一套比再引入一个消息循环更稳。
    """
    if user32 is None:
        return False
    try:
        return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def esc_pressed() -> bool:
    """轮询 ESC 是否被按下（用于中途中断采集）。拿不到键盘状态时返回 False。"""
    return key_down(VK_ESCAPE)
