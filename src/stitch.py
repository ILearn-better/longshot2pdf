"""重叠检测 + 流式拼接 + 自动分页。

两个设计决定，直接决定这个工具能不能截 500 页：

1) 重叠检测不用固定位移，用画面内容自己对齐。
   取上一帧底部 K 行做「指纹」，在当前帧里滑动搜索最相似的位置，
   相似度最高的位置就是重叠起点，其后才是全新内容。
   这样即使网页平滑滚动、列表加载导致位移不均匀，也不会断连。

2) 内存里永远只保留不到两页。
   500 页 × 1400px ≈ 70 万像素高，整图 RGB 需要 2.1GB，必然 OOM。
   所以缓冲区一满一页就立刻编码成 JPEG 落盘，剩余的 memmove 回开头。
   峰值内存与总长度无关。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

SMALL_W = 64          # 匹配用缩略宽度
DEFAULT_K = 12        # 指纹行数
MAX_MATCH_ERROR = 8.0  # 最小 MAD 超过此值视为匹配失败（滚过头/界面重绘）


def page_height_for(width: int, ratio: float = 1.414) -> int:
    """按 A4 纵向比例推算单页像素高度。"""
    return max(1, int(round(width * ratio)))


def prep_gray(frame: np.ndarray, small_w: int = SMALL_W) -> np.ndarray:
    """转灰度并横向降采样到 small_w 列，用于快速匹配。"""
    img = Image.fromarray(frame).convert("L")
    h = max(1, int(round(img.height * small_w / img.width)))
    if (img.width, img.height) != (small_w, h):
        img = img.resize((small_w, h), Image.BOX)
    return np.asarray(img)


def match_offset(
    prev_small: np.ndarray,
    cur_small: np.ndarray,
    k: int = DEFAULT_K,
    max_error: float = MAX_MATCH_ERROR,
) -> tuple[int, float] | None:
    """在当前帧中定位上一帧底部 K 行的位置。

    返回 (y, error)：
        cur_small[y : y+k] 匹配 prev 的最后 k 行
        全新内容从 cur 的 y+k 行开始
    匹配失败（error 过大或尺寸异常）返回 None，调用方应停止采集，
    宁可少截也不要写入错位的脏数据。
    """
    if prev_small.ndim != 2 or cur_small.ndim != 2:
        return None
    hp, w = prev_small.shape
    hc, wc = cur_small.shape
    if w != wc or hp < k or hc < k + 2:
        return None

    tail = prev_small[-k:].astype(np.int16)
    windows = sliding_window_view(cur_small, (k, w))[:, 0].astype(np.int16)
    mad = np.abs(windows - tail).mean(axis=(1, 2))
    y = int(mad.argmin())
    err = float(mad[y])
    if err > max_error:
        return None
    return y, err


REFINE_K = 32       # 精修用的全分辨率指纹行数
REFINE_RADIUS = 18  # 精修搜索半径（像素）


def _refine(
    prev_full: np.ndarray,
    cur_full: np.ndarray,
    y_guess: int,
    k: int = REFINE_K,
    radius: int = REFINE_RADIUS,
) -> tuple[int, float] | None:
    """在缩略图粗定位的 ±radius 像素内，用全分辨率 RGB 做像素级精修。

    必须做这一步：缩略图 64 列宽意味着 1 行的定位误差 = 十几像素，
    直接乘回去会在拼接处留下一条错位缝。粗定位只用来压缩搜索范围，
    最终偏移必须由原始像素决定。
    """
    hp, w = prev_full.shape[0], prev_full.shape[1]
    hc = cur_full.shape[0]
    k = min(k, hp, hc)
    if k < 2:
        return None
    tail = prev_full[-k:].astype(np.int16)
    lo = max(0, y_guess - radius)
    hi = min(hc - k, y_guess + radius)
    if hi < lo:
        return None
    band = cur_full[lo : hi + k].astype(np.int16)
    windows = sliding_window_view(band, (k, w, 3))[:, 0]
    mad = np.abs(windows - tail).mean(axis=(1, 2, 3))
    j = int(mad.argmin())
    return lo + j, float(mad[j])


def locate(
    prev_full: np.ndarray,
    cur_full: np.ndarray,
    k_small: int = DEFAULT_K,
    small_w: int = SMALL_W,
    max_error: float = MAX_MATCH_ERROR,
) -> tuple[int, int, float] | None:
    """定位当前帧中「上一帧内容」的结束位置。

    返回 (new_start, delta_px, error)：
        new_start : cur_full 中全新内容的起始行号，新内容 = cur_full[new_start:]
        delta_px  : 本帧相对上一帧的滚动位移（像素）
    失败返回 None。
    """
    ps = prep_gray(prev_full, small_w)
    cs = prep_gray(cur_full, small_w)
    coarse = match_offset(ps, cs, k=k_small, max_error=max_error)
    if coarse is None:
        return None
    y_small, _ = coarse

    scale = cur_full.shape[0] / max(1, cs.shape[0])
    y_guess = int(round(y_small * scale))

    fine = _refine(prev_full, cur_full, y_guess)
    if fine is None:
        return None
    y_full, err = fine
    if err > max_error * 3:  # 全分辨率下误差容忍度略放宽
        return None

    new_start = y_full + REFINE_K
    delta_px = (prev_full.shape[0] - REFINE_K) - y_full
    return new_start, int(delta_px), err


class StreamStitcher:
    """流式拼接器：追加新内容行，满一页立即落盘。"""

    def __init__(
        self,
        width: int,
        page_h: int,
        out_dir: Path,
        quality: int = 85,
        max_frame_h: int = 4096,
    ) -> None:
        self.width = int(width)
        self.page_h = int(page_h)
        self.quality = int(quality)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._cap = self.page_h + int(max_frame_h)
        self._buf = np.empty((self._cap, self.width, 3), dtype=np.uint8)
        self._fill = 0
        self._pages: list[Path] = []
        self.total_rows = 0

    # ---------- 内部 ----------
    def _grow(self, need: int) -> None:
        new_cap = max(need, self._cap * 2)
        new_buf = np.empty((new_cap, self.width, 3), dtype=np.uint8)
        new_buf[: self._fill] = self._buf[: self._fill]
        self._buf = new_buf
        self._cap = new_cap

    def _flush(self, arr: np.ndarray) -> None:
        idx = len(self._pages)
        path = self.out_dir / f"page_{idx:05d}.jpg"
        Image.fromarray(arr).save(
            path,
            format="JPEG",
            quality=self.quality,
            subsampling=0,   # 4:4:4，关掉色度下采样，否则文字边缘发虚
            optimize=False,  # 500 页场景下 optimize 会让编码慢 2~3 倍
        )
        self._pages.append(path)

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        """宽度不一致时缩放到目标宽度（正常流程不应触发）。"""
        img = Image.fromarray(frame)
        h = max(1, int(round(img.height * self.width / img.width)))
        return np.asarray(img.resize((self.width, h), Image.LANCZOS))

    # ---------- 对外 ----------
    def append(self, frame: np.ndarray) -> None:
        """追加一段全新内容（调用方负责裁掉重叠部分）。"""
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"帧格式应为 (H, W, 3)，实际 {frame.shape}")
        if frame.shape[1] != self.width:
            frame = self._fit(frame)
        h = frame.shape[0]
        if h <= 0:
            return
        if self._fill + h > self._cap:
            self._grow(self._fill + h)

        self._buf[self._fill : self._fill + h] = frame
        self._fill += h
        self.total_rows += h

        while self._fill >= self.page_h:
            self._flush(self._buf[: self.page_h])
            remain = self._fill - self.page_h
            if remain > 0:
                self._buf[:remain] = self._buf[self.page_h : self._fill].copy()
            self._fill = remain

    def finish(self) -> list[Path]:
        """把不满一页的残留内容也出一页，返回全部页 JPEG 路径。"""
        if self._fill > 0:
            self._flush(self._buf[: self._fill].copy())
            self._fill = 0
        return list(self._pages)

    @property
    def pages(self) -> list[Path]:
        return list(self._pages)

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def pending_rows(self) -> int:
        return self._fill
