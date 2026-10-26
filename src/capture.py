"""区域截图：把屏幕的一块矩形变成 numpy 数组。

用 mss 的 BitBlt 后端，单帧 1000x1400 约 5~15ms，比 PIL.ImageGrab 快 5 倍以上。
注意 mss 实例必须在**使用它的线程**里创建，所以 Grabber 不做跨线程共享。

坐标系是 Windows 虚拟桌面（物理像素）：副屏在主屏左边时 left 是负数，
所以这里**不允许**假设坐标非负；越界裁剪交给 :func:`normalize_region`。
"""

from __future__ import annotations

import numpy as np
import mss

from . import winapi

Region = tuple[int, int, int, int]  # (left, top, width, height) 物理像素

MIN_SIDE = 2  # 小于 2px 的区域没有采集/匹配意义


def _screen_from_mss() -> Region:
    """问 mss 要虚拟桌面范围（非 Windows 平台拿不到 Win32 信息时用它）。

    mss 的 ``monitors[0]`` 就是「所有显示器合并后的矩形」，语义与
    Win32 的 ``SM_XVIRTUALSCREEN`` 一致，负坐标也照给。
    """
    with new_session() as sct:
        mon = sct.monitors[0]
    return (
        int(mon["left"]),
        int(mon["top"]),
        int(mon["width"]),
        int(mon["height"]),
    )


def normalize_region(region: Region, screen: Region | None = None) -> Region:
    """把区域裁剪进虚拟桌面范围，返回合法区域；完全落在屏幕外抛 ValueError。

    为什么需要它：用户框选时可能拖到屏幕外（尤其是贴着副屏边缘、或缩放后
    tkinter 给出的逻辑坐标换算回来超界），mss 遇到这种区域不会报错，
    而是返回一块全黑图，直接喂给拼接算法会得到"整帧全黑"的脏数据。
    先裁剪再截，至少保证拿到的是真实像素。

    参数
    ----
    region : (left, top, w, h)，left/top 可为负
    screen : 覆盖用的桌面范围；默认先问 winapi，问不到（非 Windows）再问 mss
    """
    try:
        left, top, width, height = (int(v) for v in region)
    except (TypeError, ValueError):
        raise ValueError(f"区域必须是 4 个整数: {region!r}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"非法截取区域（宽高必须为正）: {region!r}")

    if screen is None:
        screen = winapi.virtual_screen()
        if screen[2] <= 0 or screen[3] <= 0:
            # 非 Windows 上 winapi 返回 (0,0,0,0)，含义是「问不到」而不是「屏幕为 0」
            screen = _screen_from_mss()
    try:
        sx, sy, sw, sh = (int(v) for v in screen)
    except (TypeError, ValueError):
        raise ValueError(f"屏幕范围必须是 4 个整数: {screen!r}") from None
    if sw <= 0 or sh <= 0:
        raise ValueError(f"屏幕范围无效: {screen!r}")

    x0 = max(left, sx)
    y0 = max(top, sy)
    x1 = min(left + width, sx + sw)
    y1 = min(top + height, sy + sh)
    cw, ch = x1 - x0, y1 - y0
    if cw < MIN_SIDE or ch < MIN_SIDE:
        raise ValueError(f"区域完全落在屏幕外: {region!r}（桌面 {screen!r}）")
    return (x0, y0, cw, ch)


def new_session():
    """创建一个 mss 截屏会话。

    mss 10.2 起 `mss.mss()` 已弃用、`MSS` 类取而代之，直接调用会在每次创建时
    往 stderr 打一行 DeprecationWarning —— 而截图是每帧都做的事，这行警告会
    把真正的错误挤出终端。所以优先用新 API，旧版本回退。

    单独暴露成公共函数是为了让 cli 的 ``--fullscreen`` 也走同一条路径：
    两处各写一份 `mss.mss()` 最容易出现「一处升级了另一处忘了」。
    """
    factory = getattr(mss, "MSS", None) or getattr(mss, "mss")
    return factory()


class Grabber:
    def __init__(self) -> None:
        self._sct: mss.base.MSSBase | None = new_session()

    def grab(self, region: Region) -> np.ndarray:
        """截取区域，返回 (H, W, 3) uint8 RGB 数组。

        left/top 允许为负（副屏），但宽高必须是正数；非法尺寸直接抛 ValueError，
        而不是把异常留给 mss 的 C 层 —— 那里只会给出难以定位的乱码报错。
        """
        if self._sct is None:
            raise RuntimeError("Grabber 已关闭")
        try:
            left, top, width, height = (int(v) for v in region)
        except (TypeError, ValueError):
            raise ValueError(f"区域必须是 4 个整数: {region!r}") from None
        if width <= 0 or height <= 0:
            raise ValueError(f"非法截取区域: {region!r}")

        shot = self._sct.grab(
            {"left": left, "top": top, "width": width, "height": height}
        )
        arr = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
            shot.height, shot.width, 3
        )
        # shot.rgb 是 bytes（只读），复制一份让下游可以自由切片/改写
        return arr.copy()

    def close(self) -> None:
        sct, self._sct = self._sct, None
        if sct is None:
            return
        try:
            sct.close()
        except Exception:
            pass

    def __enter__(self) -> "Grabber":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        # 兜底：调用方忘了 close() 时不留下 DC 句柄（析构期异常一律吞掉）
        try:
            self.close()
        except Exception:
            pass
