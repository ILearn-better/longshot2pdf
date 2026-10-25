"""区域截图：把屏幕的一块矩形变成 numpy 数组。

用 mss 的 BitBlt 后端，单帧 1000x1400 约 5~15ms，比 PIL.ImageGrab 快 5 倍以上。
注意 mss 实例必须在**使用它的线程**里创建，所以 Grabber 不做跨线程共享。
"""

from __future__ import annotations

import numpy as np
import mss

Region = tuple[int, int, int, int]  # (left, top, width, height) 物理像素


class Grabber:
    def __init__(self) -> None:
        self._sct = mss.mss()

    def grab(self, region: Region) -> np.ndarray:
        """截取区域，返回 (H, W, 3) uint8 RGB 数组。"""
        left, top, width, height = (int(v) for v in region)
        if width <= 0 or height <= 0:
            raise ValueError(f"非法截取区域: {region}")
        shot = self._sct.grab(
            {"left": left, "top": top, "width": width, "height": height}
        )
        arr = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
            shot.height, shot.width, 3
        )
        # shot.rgb 是 bytes（只读），复制一份让下游可以自由切片/改写
        return arr.copy()

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    def __enter__(self) -> "Grabber":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
