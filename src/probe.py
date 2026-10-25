"""滚动有效性探测。

核心问题：不能假设界面上任何一个位置都能滚。微信聊天区能滚，图片预览不能；
PDF 阅读器正文能滚，工具栏不能；资源管理器列表能滚，空白处不能。

做法是「一发一收 + 双向验证」：

    ① 截一小块作基准 before
    ② 向下滚 1 格，等待渲染稳定，截 after_down
    ③ 有明显像素差 → 可滚动
       无像素差     → 再向上滚 1 格试一次（可能已经滚到底部了）
    ④ 无论结果如何，把滚动位置**滚回原点**，保证采集起点不偏移

只判「能不能滚」，不估位移量 —— 位移量由采集阶段的匹配算法实测（更准）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import winapi
from .capture import Grabber, Region

# 像素平均绝对差阈值。滚动 100px 时通常 MAD 在 10~50，空白区可能 <1。
DEFAULT_THRESHOLD = 1.5


@dataclass
class ProbeResult:
    scrollable: bool
    direction: str = "none"          # "down" | "up" | "none"
    unit_px: float = 0.0             # 「一格滚轮」的像素位移，由采集阶段校准填入
    hot_point: tuple[int, int] = (0, 0)
    note: str = ""
    tried: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.scrollable


def _mad(a: np.ndarray, b: np.ndarray) -> float:
    """两张同尺寸图的平均绝对差（0~255）。"""
    if a.shape != b.shape:
        return 255.0
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _probe_box(region: Region, hot: tuple[int, int]) -> Region:
    """以滚动点为中心，取一块足够大的探测区。

    不能取太小：落在纯色空白区会被误判为「不可滚动」。
    宽度取区域的 80%，高度取区域高度的 40%（上限 320px），基本必然包含正文内容。
    """
    x, y, w, h = region
    bw = max(48, int(w * 0.8))
    bh = int(min(320, max(64, h * 0.4)))
    cx, cy = hot
    left = int(min(max(x, cx - bw // 2), x + w - bw))
    top = int(min(max(y, cy - bh // 2), y + h - bh))
    return (left, top, bw, bh)


def _candidates(region: Region) -> list[tuple[int, int]]:
    """候选滚动点，按优先级排列。中心点最可能有效。"""
    x, y, w, h = region
    pts = [
        (x + w * 0.50, y + h * 0.50),
        (x + w * 0.50, y + h * 0.35),
        (x + w * 0.50, y + h * 0.65),
        (x + w * 0.30, y + h * 0.50),
        (x + w * 0.70, y + h * 0.50),
    ]
    return [(int(px), int(py)) for px, py in pts]


def _try_at(
    grabber: Grabber,
    region: Region,
    hot: tuple[int, int],
    settle_ms: int,
    threshold: float,
) -> tuple[bool, str]:
    """在单个点上测试是否可滚。返回 (是否可滚, 方向)。"""
    box = _probe_box(region, hot)
    winapi.set_cursor_pos(*hot)
    time.sleep(0.02)  # 等 hover 生效，某些控件要 hover 才接受滚动

    before = grabber.grab(box)

    # ① 先试向下
    winapi.wheel(-1)
    time.sleep(settle_ms / 1000.0)
    after_down = grabber.grab(box)
    if _mad(before, after_down) >= threshold:
        winapi.wheel(1)  # 滚回原点
        time.sleep(settle_ms / 1000.0)
        return True, "down"

    # ② 向下没反应，试向上（可能已经在底部）
    winapi.wheel(1)
    time.sleep(settle_ms / 1000.0)
    after_up = grabber.grab(box)
    if _mad(before, after_up) >= threshold:
        winapi.wheel(-1)  # 还原
        time.sleep(settle_ms / 1000.0)
        return True, "up"

    return False, "none"


def probe(
    grabber: Grabber,
    region: Region,
    settle_ms: int = 180,
    threshold: float = DEFAULT_THRESHOLD,
) -> ProbeResult:
    """检测区域是否可滚动。会逐个尝试候选点，全部失败才判定不可滚。"""
    tried: list[str] = []
    for hot in _candidates(region):
        ok, direction = _try_at(grabber, region, hot, settle_ms, threshold)
        tried.append(f"({hot[0]},{hot[1]}) -> {'可滚/' + direction if ok else '无反应'}")
        if ok:
            if direction == "up":
                return ProbeResult(
                    scrollable=True,
                    direction="up",
                    hot_point=hot,
                    note="可滚动，但当前位置已在底部，无法向下采集。建议先向上滚动一段再开始。",
                    tried=tried,
                )
            return ProbeResult(
                scrollable=True,
                direction="down",
                hot_point=hot,
                note="检测通过：该位置响应滚轮，可以向下滚动截图。",
                tried=tried,
            )

    x, y, w, h = region
    return ProbeResult(
        scrollable=False,
        direction="none",
        hot_point=(x + w // 2, y + h // 2),
        note="该区域对滚轮无响应（可能是图片预览区、工具栏或已是纯静态画面）。",
        tried=tried,
    )
