"""合成帧数据 + 虚拟屏幕：所有模块离线自测的公共地基。

设计原则
--------
1) **不依赖真实屏幕**。拼接、探测、编排的全部逻辑都能在这套 mock 上跑通，
   这样并行开发时没人需要等别人的真机环境。
2) **高保真**。合成图不是随机噪声，而是模拟真实文档：
   - 文字行：字形块宽度/灰度随机，行高与行距有小幅抖动
   - 表格横线/竖线：制造周期性重复结构（拼接算法最容易被误导的地方）
   - 页边距：成片纯白区域（探测算法最容易误判「不可滚动」的地方）
3) **唯一可判据**。每份文档都返回逐像素的 ground truth，
   拼接结果必须与 ground truth **完全相等**，不接受「看起来对齐」。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------- 文档生成


def make_document(
    width: int = 1000,
    height: int = 14000,
    *,
    seed: int = 20261025,
    margin: int = 60,
    line_gap: tuple[int, int] = (10, 26),
    table_every: int = 0,
    rules: bool = True,
    blank_margins: int = 0,
) -> np.ndarray:
    """生成一张模拟「长文档」的 RGB 长图。

    参数
    ----
    table_every : 每隔多少像素加一条贯穿横线，>0 时制造周期性重复结构。
    blank_margins : 顶部/底部各留多少像素纯白（考验探测算法是否误判）。
    """
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 252, dtype=np.uint8)
    if blank_margins:
        blank_margins = min(blank_margins, height // 3)

    top = margin + blank_margins
    bottom = height - margin - blank_margins

    y = top
    while y < bottom - 30:
        x = margin
        line_h = int(rng.integers(13, 21))
        if y + line_h >= bottom:
            break
        while x < width - margin - 20:
            w = int(rng.integers(8, 64))
            if x + w > width - margin:
                break
            shade = int(rng.integers(0, 110))
            img[y : y + line_h, x : x + w] = shade
            # 1px 竖笔画：给缩略图之外的精细结构，保证 1px 位移可被判据出来
            if w > 6 and rng.random() < 0.7:
                gx = x + int(rng.integers(1, w - 1))
                img[y : y + line_h, gx : gx + 1] = max(0, shade - 40)
            x += w + int(rng.integers(5, 16))
        y += line_h + int(rng.integers(*line_gap))

    if rules:
        # 段落分隔线（不等距）
        for _ in range(max(1, height // 1200)):
            ry = int(rng.integers(top + 40, max(top + 41, bottom - 40)))
            img[ry : ry + 2, margin : width - margin] = 170

    if table_every > 0:
        # 等距横线：周期结构，制造「看哪儿都像」的歧义
        for ry in range(top, bottom, table_every):
            img[ry : ry + 2, margin : width - margin] = 140
            if ry + 2 < bottom:
                img[ry + 2 : ry + 3, margin : width - margin] = 210

    return img


def make_plain_document(width: int = 800, height: int = 9000) -> np.ndarray:
    """周期性最强的困难样本：等距横线 + 等宽文本块。"""
    return make_document(
        width=width,
        height=height,
        table_every=120,
        line_gap=(12, 14),
        rules=False,
        seed=7,
    )


# ---------------------------------------------------------------- 帧序列


@dataclass
class ScrollTrace:
    """一次滚动采集的 ground truth。"""

    frames: list[np.ndarray]     # 每帧画面
    offsets: list[int]           # 每帧顶部在文档中的行号
    deltas: list[int]            # deltas[i] = offsets[i+1] - offsets[i]（长度为 n-1）
    view_h: int
    doc_h: int


def scroll_trace(
    doc: np.ndarray,
    view_h: int,
    step: int,
    *,
    jitter: int = 0,
    seed: int = 1,
    max_frames: int | None = None,
) -> ScrollTrace:
    """模拟「向下滚动 → 截图」，返回帧序列与每步真实位移。

    jitter > 0 时每步位移在 [step-jitter, step+jitter] 之间随机抖动，
    模拟滚动动画、程序对滚轮格的解释不一致等真实情况。
    """
    rng = np.random.default_rng(seed)
    H = doc.shape[0]
    offsets = [0]
    while offsets[-1] + view_h < H:
        d = int(rng.integers(step - jitter, step + jitter + 1)) if jitter else step
        d = max(1, d)
        nxt = offsets[-1] + d
        if nxt + view_h > H:
            nxt = H - view_h
        offsets.append(nxt)
        if nxt + view_h >= H:
            break
        if max_frames and len(offsets) >= max_frames:
            break
    frames = [doc[o : o + view_h].copy() for o in offsets]
    deltas = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]
    return ScrollTrace(frames, offsets, deltas, view_h, H)


def frame_pair(doc: np.ndarray, view_h: int, delta: int) -> tuple[np.ndarray, np.ndarray]:
    """构造一对相邻帧，真实位移恰好为 delta。"""
    prev = doc[0:view_h].copy()
    cur = doc[delta : delta + view_h].copy()
    return prev, cur


# ---------------------------------------------------------------- 虚拟屏幕


class VirtualScreen:
    """把「长文档 + 滚动位置」伪装成一块屏幕，供 pipeline 端到端测试。

    实现 pipeline 依赖的最小接口：
        grab(region) -> (h, w, 3) uint8 rgb
        close()
        wheel(steps)     steps < 0 向下
        set_cursor(x, y)
        esc() -> bool
    """

    def __init__(
        self,
        doc: np.ndarray,
        view_h: int,
        *,
        unit_px: int = 100,
        smooth: bool = False,
        noise: int = 0,
        footer_rows: int = 0,
        lazy_rows: int = 0,
        lazy_delay_frames: int = 2,
    ) -> None:
        self.doc = doc
        self.view_h = int(view_h)
        self.view_w = int(doc.shape[1])
        self.offset = 0
        self.unit_px = int(unit_px)
        self.smooth = smooth          # True: 每格滚一半（模拟平滑滚动动画）
        self.noise = int(noise)       # >0: 每帧叠加 ±noise 的抖动（模拟重绘抖动）
        self.footer_rows = int(footer_rows)  # >0: 底部固定条（模拟悬浮按钮/状态栏）
        self.lazy_rows = int(lazy_rows)      # >0: 顶部若干行延迟渲染
        self.lazy_delay_frames = int(lazy_delay_frames)

        self.max_offset = max(0, doc.shape[0] - self.view_h)
        self.wheel_calls = 0
        self.cursor = (0, 0)
        self._esc = False
        self._pending = 0               # smooth 模式下的残余位移
        self._grabs = 0                 # grab 次数，用于模拟惰性加载的延迟帧
        self._lazy_ready = 0            # 到这个 grab 序号后新内容才补齐

        self._footer = doc[-self.footer_rows :].copy() if self.footer_rows else None

    # ---- 屏幕内容 ----
    def _viewport(self) -> np.ndarray:
        top = self.offset
        patch = np.full((self.view_h, self.view_w, 3), 255, dtype=np.uint8)
        avail = min(self.view_h, self.doc.shape[0] - top)
        if avail > 0:
            patch[:avail] = self.doc[top : top + avail]
        # 固定底栏必须画在**屏幕视口**的底部，而不是「某次 grab 请求区域」的底部：
        # 它是界面自己贴在窗口下沿的状态栏 / 悬浮按钮，位置与截取范围无关。
        # 画错位置的话，strip_bottom 永远剔除不掉它，测试会给出假失败。
        if self._footer is not None:
            fr = min(self._footer.shape[0], self.view_h)
            patch[self.view_h - fr :] = self._footer[self._footer.shape[0] - fr :]
        return patch

    def grab(self, region: tuple[int, int, int, int]) -> np.ndarray:
        self._grabs += 1
        left, top, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            raise ValueError(f"非法截取区域: {region}")
        shot = self._viewport()
        out = shot[top : top + h, left : left + w].copy()
        if out.shape[0] != h or out.shape[1] != w:
            pad = np.full((h, w, 3), 255, dtype=np.uint8)
            pad[: out.shape[0], : out.shape[1]] = out
            out = pad

        # 惰性加载：向下滚动时，新内容是从**底部**进来的，网页/客户端常常先给
        # 一块空白占位，等图片解码或列表取数完成后才补上。
        # 这正是踩坑清单 4.1 要防的「截到空白占位」，用 grab 次数模拟延迟帧数。
        if self.lazy_rows and top > 0 and self._grabs < self._lazy_ready:
            band = min(self.lazy_rows, h)
            out[h - band :] = 255

        if self.noise:
            rng = np.random.default_rng(self.wheel_calls * 7919 + top)
            delta = rng.integers(-self.noise, self.noise + 1, size=out.shape)
            out = np.clip(out.astype(np.int16) + delta, 0, 255).astype(np.uint8)
        return out

    def close(self) -> None:
        pass

    # ---- 滚轮 ----
    def wheel(self, steps: int) -> None:
        self.wheel_calls += 1
        if self.lazy_rows:
            # 滚动之后需要再过 lazy_delay_frames 次抓屏，新内容才渲染出来
            self._lazy_ready = self._grabs + self.lazy_delay_frames
        px = int(round(steps * self.unit_px * (0.5 if self.smooth else 1.0)))
        if self.smooth:
            self._pending += px
            px = int(self._pending)
            self._pending -= px
        # steps < 0 向下 → offset 增加
        self.offset = int(min(max(self.offset - px, 0), self.max_offset))

    def set_cursor(self, x: int, y: int) -> None:
        self.cursor = (int(x), int(y))

    def esc(self) -> bool:
        return self._esc

    def press_esc(self) -> None:
        self._esc = True
