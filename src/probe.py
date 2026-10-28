"""滚动有效性探测。

核心问题：不能假设界面上任何一个位置都能滚。微信聊天区能滚，图片预览不能；
PDF 阅读器正文能滚，工具栏不能；资源管理器列表能滚，空白处不能。

做法是「一发一收 + 双向验证 + 实测位移 + 精确归位」：

    ① 截整区域作基准 before（同时切出一块探测块 before_box）
    ② 向下滚 1 格，等画面稳定，截 after_down
    ③ 探测块像素差 ≥ 阈值 → 可滚动（向下）
       没差 → 再向上滚 1 格（可能已经滚到底部），有差 → 可滚动（向上）
    ④ 用 before / after 两帧整区域喂 locate()，量出「一格滚轮 = 多少像素」
       量得可疑（< 3px）时用 3 格重量一次，避免整数像素误差被放大 3 倍
    ⑤ 反方向滚回原格数，再截一帧与 before 比对，确认**真的回到原位**
    ⑥ 全部候选点都无位移 → 判定「不可滚动」

为什么必须实测 unit_px：不同程序一格滚轮差别极大（Chrome ~100px、Excel 按行、
PDF 阅读器可能整屏）。编排层若自己再滚一格去校准，等于多一次滚动、多一次误差，
而且探测阶段已经拿到了这对帧，白扔了。

阈值为什么自适应：``threshold = max(1.5, 3 × 噪声估计)``。
- 下界 1.5：低于它的差异肉眼不可见，只可能是采集噪声；
- 3 倍噪声：静态但"抖动"的画面（视频缩略图、渐变背景、远程桌面的重绘抖动）
  每帧都有 1~3 的随机差，用固定 1.5 会把这类画面误判成"可滚动"，
  进而让采集流程空转到底（这是要修掉的误判之一）；取 3 倍才能压住误报。
- 噪声估计**只统计平坦区**：文字笔画、表格线是信号不是噪声，
  把它们算进去会把阈值抬到天上，弱对比界面（浅灰字、细表格）就被误判成"不可滚"。
  做法是用 x/y 两个方向的梯度分位圈出平坦掩码（两个方向都很平才算平坦），
  只在这个掩码里统计梯度均值，再乘 √2（单帧只能看到一次抖动，
  帧间差是两次独立抖动之差，期望幅度大 √2 倍）得到"帧间噪声"的期望值。
  纯色且无抖动的画面噪声=0，阈值退回 1.5，任何真实位移都能被抓住。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import winapi
from .capture import Grabber, Region

WheelFn = Callable[[int], None]
CursorFn = Callable[[int, int], None]

# 像素平均绝对差阈值（自适应时的下界）
DEFAULT_THRESHOLD = 1.5
# 阈值 = max(DEFAULT_THRESHOLD, NOISE_GAIN × 噪声估计)，再夹到 MAX_THRESHOLD
NOISE_GAIN = 3.0
MAX_THRESHOLD = 60.0
# 单帧平坦区梯度 → 帧间噪声的换算系数（两次独立抖动之差的期望是单帧的 √2 倍）
SINGLE_FRAME_TO_PAIR = 1.4142
# 单帧噪声估计最多考虑 80% 分位以下的梯度（更高的都属于文字/线条等真实结构）
FLAT_PERCENTILE = 80.0
# 与起始帧的 MAD 小于此值即认为「回到了原位」（另允许 ±1px 对齐误差）
RESTORE_TOL = 0.5
# 噪声画面做不到 0.5 的严格判据，放宽到「残差与噪声同量级」
RESTORE_NOISE_GAIN = 2.5
# 光标移到滚动点后等 hover 生效（部分控件要 hover 才接受滚轮）
HOVER_S = 0.02
# 等画面稳定的采样间隔 / 「已稳定」判据
POLL_S = 0.025
STABLE_MAD = 0.35
# 一格滚轮位移小于这个值就用 3 格重测（整数像素误差会放大）
TINY_UNIT_PX = 3.0
RETRY_STEPS = 3


@dataclass
class ProbeResult:
    scrollable: bool
    direction: str = "none"          # "down" | "up" | "none"
    unit_px: float = 0.0             # 实测「一格滚轮」的像素位移；0 表示没测准
    hot_point: tuple[int, int] = (0, 0)
    note: str = ""
    tried: list[str] = field(default_factory=list)
    restored: bool = False           # 是否确认把滚动位置还原到探测前
    unit_steps: int = 0              # 测 unit_px 时一共滚了几格

    def __bool__(self) -> bool:
        return self.scrollable


# ------------------------------------------------------------------ 基础度量


def _mad(a: np.ndarray, b: np.ndarray) -> float:
    """两张同尺寸图的平均绝对差（0~255）。"""
    if a.shape != b.shape or a.size == 0:
        return 255.0
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _noise_floor(frame: np.ndarray) -> float:
    """画面噪声估计（只统计平坦区，不被文字边缘污染）。

    1) 分别求 |∂x| 和 |∂y|，各取 80% 分位当门限；
    2) 只有**两个方向都低于各自门限**的像素才算平坦区 —— 文字笔画、表格线
       至少在某个方向上梯度很大，会被排除，所以弱对比内容的阈值不会被抬高；
    3) 平坦区里取 (|∂x|+|∂y|)/2 的均值，即单帧抖动的幅度；
    4) 乘 √2 换算成「帧间差」的期望幅度（两次独立抖动之差）。

    实测标定（tests/mock_frames 的静态抖动画面，noise=1/2/3/5）：
    估计得 0.62 / 0.95 / 1.39 / 1.94，真实帧间差 0.89 / 1.60 / 2.29 / 3.33；
    经 3 倍后阈值 1.9 / 2.8 / 4.2 / 5.8，全部压住噪声，全部远小于
    真实滚动 100px 的帧间差（60~90）—— 既不会把抖动误判成可滚，也不会漏判。
    干净画面（无抖动）平坦区梯度恒为 0，估计值 0，阈值退回下界 1.5。
    """
    g = frame.astype(np.float32)
    if g.ndim == 3:
        g = g.mean(axis=2)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    gx = np.abs(np.diff(g, axis=1))[:-1, :]
    gy = np.abs(np.diff(g, axis=0))[:, :-1]
    tx = np.percentile(gx, FLAT_PERCENTILE)
    ty = np.percentile(gy, FLAT_PERCENTILE)
    flat = (gx <= tx) & (gy <= ty)
    if not flat.any():
        return 0.0
    amp = float(((gx + gy) * 0.5)[flat].mean())
    return max(0.0, amp * SINGLE_FRAME_TO_PAIR)


def _auto_threshold(noise: float, threshold: float | None = None) -> float:
    """自适应阈值 = max(1.5, 3 × 噪声估计)，上限 60。传 threshold 时用固定值。"""
    if threshold is not None:
        return float(threshold)
    return float(min(MAX_THRESHOLD, max(DEFAULT_THRESHOLD, NOISE_GAIN * noise)))


# ---------------------------------------------------------------- 空白画面
#
# 「这一帧是不是一片空白」是滚动检测的**必需品**，不是锦上添花：
# 滚轮明明生效了，但新露出来的是一片白纸 / 页边距 / 图表下方的留白，
# 前后两帧字面上一模一样 —— 只比像素的话，「到底了」和「正在穿过留白」
# 这两个完全不同的事实会给出同一个观测值（零位移）。
# 编排层据此把留白误判成结尾，用户就会拿到一份只有前半段的 PDF。
#
# 判据用**灰度标准差**：纯白纸、纯色底、深色主题的空页都接近 0~1；
# 哪怕只有一行小字也会把它顶到 3 以上。这道分界线比「平均梯度」稳，
# 因为 1px 的细横线（表格线）梯度贡献不大，但标准差会明显抬头。

# 灰度标准差低于此值即视为「没有可比对的内容」
BLANK_STD = 2.5
# 平均绝对梯度低于此值（配合标准差）才算空白：防止大片均匀平涂的彩色背景被误判
BLANK_EDGE = 1.2
# 判据抽样步长：整帧 1000x1400 全算要 4M 次浮点，抽一半足够且快 4 倍
BLANK_SAMPLE = 2


def frame_flatness(frame: np.ndarray) -> tuple[float, float]:
    """返回 (灰度标准差, 平均绝对梯度)，都按 ``BLANK_SAMPLE`` 降采样后统计。

    返回值是「画面有多少可比对的结构」的度量：
    纯色 ≈ (0, 0)；有文字的正文 ≈ (20~60, 3~20)。
    """
    if frame is None or frame.size == 0:
        return 0.0, 0.0
    g = frame.astype(np.float32)
    if g.ndim == 3:
        g = g.mean(axis=2)
    g = g[::BLANK_SAMPLE, ::BLANK_SAMPLE]
    if g.shape[0] < 2 or g.shape[1] < 2:
        return 0.0, 0.0
    std = float(g.std())
    edge = float(
        (np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=0)).mean()) * 0.5
    )
    return std, edge


def is_blank_frame(
    frame: np.ndarray, *, std: float = BLANK_STD, edge: float = BLANK_EDGE
) -> bool:
    """这一帧是不是「一片空白」（没有可用于比对的内容）。

    两个条件同时成立才算空白：结构少（标准差低）**且** 边缘少（梯度低）。
    只看标准差的话，一块均匀但很亮的彩色底（比如浅蓝表格底色铺满）会被
    误判；只看梯度的话，压缩噪声大的空白页会被误判成有内容。
    """
    s, e = frame_flatness(frame)
    return s < std and e < edge


def content_top(frame: np.ndarray, *, std: float = BLANK_STD, min_rows: int = 3) -> int:
    """从上往下找第一行「有内容」的行号；整帧都空白时返回帧高。

    用途：跨越大片留白之后，重新看到内容时往往**上面是空白、下面是正文**。
    这时候不能整帧塞进缓冲区（上半截会把留白重复一遍），也不能只看
    locate（留白区没有重叠，它无从下手）。逐行看看哪一行开始有结构，
    就是新内容的真实起点 —— 纯算术，不依赖任何匹配。

    min_rows : 连续多少行有结构才算「开始有内容」。取 3 是为了过滤
        单行噪点、JPEG 块边界这种孤立亮线。
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return 0
    g = frame.astype(np.float32)
    if g.ndim == 3:
        g = g.mean(axis=2)
    g = g[::BLANK_SAMPLE, ::BLANK_SAMPLE]
    h = int(g.shape[0])
    if h < 1:
        return 0
    row_std = g.std(axis=1)
    live = row_std >= std
    if not live.any():
        return int(frame.shape[0])
    span = max(1, int(min_rows))
    # 找第一段「连续 span 行都有结构」的起点
    run = 0
    for i, flag in enumerate(live):
        if flag:
            run += 1
            if run >= span:
                return int((i - span + 1) * BLANK_SAMPLE)
        else:
            run = 0
    idx = int(np.argmax(live))
    return int(idx * BLANK_SAMPLE)


def _restore_error(a: np.ndarray, b: np.ndarray) -> float:
    """两帧「同一位置」的最小 MAD，允许 ±1px 的整行偏移。

    质量门允许归位偏差 ≤1px，而 1px 偏移在文字画面上 MAD 会到 10 以上，
    所以必须按行对齐后再比，否则会把「差 1 像素」误报成「没归位」。
    """
    if a.shape != b.shape or a.size == 0:
        return 255.0
    best = _mad(a, b)
    if a.shape[0] < 4:
        return best
    best = min(best, _mad(a[:-1], b[1:]))   # 内容整体偏下 1px
    best = min(best, _mad(a[1:], b[:-1]))   # 内容整体偏上 1px
    return best


def _crop(full: np.ndarray, region: Region, box: Region) -> np.ndarray:
    """从整区域帧里切出探测块。"""
    rx, ry = region[0], region[1]
    bx, by, bw, bh = box
    x0, y0 = bx - rx, by - ry
    patch = full[y0 : y0 + bh, x0 : x0 + bw]
    if patch.shape[:2] != (bh, bw):
        return full  # 理论上不会发生；退化成整区域，宁可慢也不能尺寸不齐
    return patch


# ------------------------------------------------------------------ 候选点


def _probe_box(region: Region, hot: tuple[int, int]) -> Region:
    """以滚动点为中心，取一块足够大的探测区。

    不能取太小：落在纯色空白区会被误判为「不可滚动」。
    宽度取区域的 80%，高度取区域高度的 40%（上限 320px），基本必然包含正文内容。
    """
    x, y, w, h = region
    bw = int(min(w, max(48, w * 0.8)))
    bh = int(min(h, max(64, min(320, h * 0.4))))
    cx, cy = hot
    left = int(min(max(x, cx - bw // 2), x + w - bw))
    top = int(min(max(y, cy - bh // 2), y + h - bh))
    return (left, top, bw, bh)


def _candidates(region: Region) -> list[tuple[int, int]]:
    """候选滚动点，按优先级排列。

    中心点最可能有效，且离内容热点之外的悬浮控件（右下角按钮、右上角工具条）
    最远 —— 滚动点本身会被截进画面，靠中心能最大限度减少光标污染。
    """
    x, y, w, h = region
    pts = [
        (x + w * 0.50, y + h * 0.50),
        (x + w * 0.50, y + h * 0.35),
        (x + w * 0.50, y + h * 0.65),
        (x + w * 0.30, y + h * 0.50),
        (x + w * 0.70, y + h * 0.50),
    ]
    out = []
    for px, py in pts:
        cx = int(min(max(x, px), x + w - 1))
        cy = int(min(max(y, py), y + h - 1))
        if (cx, cy) not in out:
            out.append((cx, cy))
    return out


# ------------------------------------------------------------------ 等待稳定


def _settle(grab_box: Callable[[], np.ndarray], settle_ms: int) -> np.ndarray:
    """滚动后等画面稳定，返回稳定后的帧（只截探测块，省带宽）。

    ``grab_box`` 是零参数闭包，重复调用只截探测块，不截整个区域。

    判据和 pipeline 的「画面稳定」一致：连续两次采样几乎相同就认为渲染完成。
    但加了**下限** ``max(60ms, settle_ms/2)``：
    有些程序在收到滚轮后 20~50ms 才开始动画，早退会把动画前的旧画面当成结果，
    从而把「会滚」误判成「不会滚」。

    返回的帧不保证与滚动前不同 —— 判定交给调用方做 MAD 比较，
    这样同一个函数既能等"动了"，也能等"确实没动"。
    """
    t0 = time.perf_counter()
    floor_s = max(0.06, settle_ms / 2000.0)
    deadline = t0 + settle_ms / 1000.0
    last = grab_box()
    while True:
        now = time.perf_counter()
        remain = deadline - now
        if remain <= 0:
            return last
        time.sleep(min(POLL_S, remain))
        cur = grab_box()
        if time.perf_counter() - t0 >= floor_s and _mad(cur, last) <= STABLE_MAD:
            return cur
        last = cur


# ------------------------------------------------------------------ 位移测量


def _locate(prev: np.ndarray, cur: np.ndarray):
    """调用拼接模块的 locate，任何异常都退化成 None（绝不拖垮探测）。

    stitch.py 与 probe.py 由不同 Agent 维护，联调期 locate 可能正在被重写、
    甚至暂时 import 失败。探测的唯一职责是回答「能不能滚」，
    测不出位移就返回 0，不能因此整体失败。
    """
    try:
        from .stitch import locate
    except Exception:
        return None
    try:
        return locate(prev, cur)
    except Exception:
        return None


def _profile_shift(prev_box: np.ndarray, cur_box: np.ndarray) -> float:
    """降级方案：用行亮度剖面 + _mad 估算位移（±1px，只给量级）。

    locate 走的是二维块匹配；这里把两帧压成「每行平均亮度」一维序列，
    枚举位移 d 用 _mad 找最相似的位置。对文字/表格类画面足够，
    对横向条纹或大面积纯色会失效 —— 失效时返回 0，由调用方标成「未测准」。
    """
    h = prev_box.shape[0]
    if h < 16:
        return 0.0
    pa = prev_box.astype(np.float32).mean(axis=(1, 2))
    pb = cur_box.astype(np.float32).mean(axis=(1, 2))
    if float(pa.std()) < 1e-6:
        return 0.0

    err0 = float(np.abs(pa - pb).mean())
    max_d = h - 8
    best_d, best_err = 0, float("inf")
    for d in range(1, max_d + 1):
        # 内容向上移了 d 行（向下滚）：pa[d:] 对应 pb[:h-d]
        err = float(np.abs(pa[d:] - pb[: h - d]).mean())
        if err < best_err:
            best_err, best_d = err, d
        # 内容向下移了 d 行（向上滚）：pa[:h-d] 对应 pb[d:]
        err = float(np.abs(pa[: h - d] - pb[d:]).mean())
        if err < best_err:
            best_err, best_d = err, d
    # 最佳位移下的残差必须明显小于零位移残差，否则视为不可信
    if best_err > err0 * 0.5:
        return 0.0
    # 位移大到几乎整块都换掉了：多半是匹配到周期结构上，不能当位移用
    if best_d >= max_d:
        return 0.0
    return float(best_d)


def _measure_unit(
    prev_full: np.ndarray,
    cur_full: np.ndarray,
    steps: int,
    prev_box: np.ndarray,
    cur_box: np.ndarray,
) -> tuple[float, str]:
    """量出「一格滚轮 = 多少像素」。返回 (unit_px, 来源)。"""
    if steps <= 0:
        return 0.0, "none"
    # locate 的语义是「上一帧的内容在当前帧里上移了多少」，天生只认向下滚。
    # 向上滚（探测"已在底部"的场景）时把两帧对调再量一次，位移量取绝对值。
    for a, b in ((prev_full, cur_full), (cur_full, prev_full)):
        loc = _locate(a, b)
        if loc is None:
            continue
        try:
            delta = abs(int(loc[1]))
        except (TypeError, ValueError, IndexError):
            delta = 0
        # 位移超过一屏不可信（可能是匹配到别处），交给降级方案
        if 0 < delta < prev_full.shape[0]:
            return delta / steps, "locate"
    shift = _profile_shift(prev_box, cur_box)
    if shift > 0:
        return shift / steps, "profile"
    return 0.0, "none"


# ------------------------------------------------------------------ 单点尝试


@dataclass
class _Attempt:
    moved: bool = False
    direction: str = "none"
    unit_px: float = 0.0
    unit_steps: int = 0
    threshold: float = DEFAULT_THRESHOLD
    restored: bool = True
    restore_err: float = 0.0
    source: str = "none"
    log: str = ""


def _restore(
    grab_box: Callable[[], np.ndarray],
    before: np.ndarray,
    settle_ms: int,
    wheel_fn: WheelFn,
    back_steps: int,
    tol: float = RESTORE_TOL,
) -> tuple[bool, float, str]:
    """反向滚回原格数并验证。

    第一次没回到位（平滑滚动/惯性只走了半程）时**同方向补滚一次**，
    再失败就认输，由调用方在 note 里提示「起始位置可能有偏移」。
    """
    wheel_fn(back_steps)
    cur = _settle(grab_box, settle_ms)
    err = _restore_error(before, cur)
    if err < tol:
        return True, err, "已归位"
    wheel_fn(back_steps)
    cur = _settle(grab_box, settle_ms)
    err = _restore_error(before, cur)
    if err < tol:
        return True, err, "补滚一次已归位"
    return False, err, f"两次未能归位（残差 {err:.2f} > {tol:.2f}）"


def _try_point(
    grabber: Grabber,
    region: Region,
    hot: tuple[int, int],
    settle_ms: int,
    threshold: float | None,
    wheel_fn: WheelFn,
    set_cursor_fn: CursorFn,
    measure_unit: bool,
) -> _Attempt:
    box = _probe_box(region, hot)
    set_cursor_fn(*hot)
    time.sleep(HOVER_S)  # 部分控件要 hover 才接受滚轮

    before_full = grabber.grab(region)
    before = _crop(before_full, region, box)
    noise = _noise_floor(before)
    thr = _auto_threshold(noise, threshold)
    # 有抖动的画面上不可能做到「残差 0.5」的严格归位判据，放宽到与噪声同量级
    tol = max(RESTORE_TOL, RESTORE_NOISE_GAIN * noise)

    def grab_box() -> np.ndarray:
        return grabber.grab(box)

    def confirmed(direction: str, sign: int, after_box: np.ndarray, mad: float) -> _Attempt:
        """某个方向确认有位移：量位移 → 归位 → 复检。"""
        scrolled = 1
        unit, source = 0.0, "none"
        if measure_unit:
            after_full = grabber.grab(region)
            unit, source = _measure_unit(before_full, after_full, 1, before, after_box)
            if 0.0 < unit < TINY_UNIT_PX:
                # 一格只走了 1~3px 时，整数误差占比太大，用 3 格重测
                wheel_fn(sign * (RETRY_STEPS - 1))
                box3 = _settle(grab_box, settle_ms)
                full3 = grabber.grab(region)
                unit3, source3 = _measure_unit(
                    before_full, full3, RETRY_STEPS, before, box3
                )
                scrolled = RETRY_STEPS
                if unit3 > 0:
                    unit, source = unit3, source3
        restored, err, how = _restore(
            grab_box, before, settle_ms, wheel_fn, -sign * scrolled, tol
        )
        parts = [
            "下滚" if direction == "down" else "上滚",
            f"Δ={mad:.2f} ≥ 阈 {thr:.2f} → 可滚",
        ]
        if measure_unit:
            parts.append(
                f"unit={unit:.1f}px/{scrolled}格({source})" if unit > 0
                else f"unit 未测准({source})"
            )
        parts.append(how)
        return _Attempt(
            moved=True,
            direction=direction,
            unit_px=unit,
            unit_steps=scrolled if unit > 0 else 0,
            threshold=thr,
            restored=restored,
            restore_err=err,
            source=source,
            log="，".join(parts),
        )

    # ① 先试向下
    wheel_fn(-1)
    after_down = _settle(grab_box, settle_ms)
    mad_down = _mad(before, after_down)
    if mad_down >= thr:
        return confirmed("down", -1, after_down, mad_down)

    # ② 向下无位移，试向上（可能已经滚到底部）
    wheel_fn(1)
    after_up = _settle(grab_box, settle_ms)
    mad_up = _mad(before, after_up)
    if mad_up >= thr:
        return confirmed("up", 1, after_up, mad_up)

    # ③ 两个方向都没动：位置天然没变（-1 +1 相互抵消），无需再补滚
    err = _restore_error(before, after_up)
    return _Attempt(
        moved=False,
        threshold=thr,
        restored=True,
        restore_err=err,
        log=(
            f"下滚 Δ={mad_down:.2f} / 上滚 Δ={mad_up:.2f} < 阈 {thr:.2f} → 无位移"
        ),
    )


# ------------------------------------------------------------------ 对外入口


def _note_moved(at: _Attempt) -> str:
    if at.unit_px > 0:
        unit = f"每格滚轮 ≈ {at.unit_px:.0f}px（{at.source} 实测）"
    else:
        unit = "一格滚轮的位移量未测准（可用最小步长继续）"
    if at.direction == "down":
        head = f"可滚动：该位置响应滚轮，{unit}，可以开始向下采集。"
    else:
        head = (
            f"已在底部：该位置可滚动但向下无位移，{unit}。"
            "请先手动向上滚动一段再开始。"
        )
    tail = (
        "滚动位置已归位。"
        if at.restored
        else f"⚠ 起始位置可能有偏移（与探测前相差 MAD={at.restore_err:.2f}）。"
    )
    return head + tail


def probe(
    grabber: Grabber,
    region: Region,
    settle_ms: int = 180,
    *,
    threshold: float | None = None,
    wheel_fn: WheelFn | None = None,
    set_cursor_fn: CursorFn | None = None,
    measure_unit: bool = True,
) -> ProbeResult:
    """检测区域是否可滚动，并实测「一格滚轮 = 多少像素」。

    参数
    ----
    grabber : 任何提供 ``grab(region) -> (H, W, 3)`` 的对象（真实 Grabber 或测试用虚拟屏幕）
    region : (left, top, w, h)，物理像素，允许负坐标
    settle_ms : 每次滚动后最多等多久画面稳定（实际会提前早退，见 :func:`_settle`）
    threshold : 固定阈值；None 表示按画面噪声自适应（推荐）
    wheel_fn / set_cursor_fn : 注入点，默认 ``winapi.wheel`` / ``winapi.set_cursor_pos``；
        离线测试传虚拟屏幕的绑定方法即可，不需要真实鼠标
    measure_unit : False 表示只判能否滚动，不测位移（省一轮截图）

    耗时上界：确认可滚的点**立刻返回**；最坏情况（5 个点全无位移）约
    ``5 × 2 × settle_ms``，这是"告诉用户滚不了"必须付的代价，
    但每个点的等待都会在画面稳定后早退，实测远小于这个上界。
    """
    wheel = winapi.wheel if wheel_fn is None else wheel_fn
    set_cursor = winapi.set_cursor_pos if set_cursor_fn is None else set_cursor_fn

    try:
        region = tuple(int(v) for v in region)  # type: ignore[assignment]
    except (TypeError, ValueError):
        raise ValueError(f"区域必须是 4 个整数: {region!r}") from None
    if len(region) != 4:
        raise ValueError(f"区域必须是 4 个整数: {region!r}")
    if region[2] <= 0 or region[3] <= 0:
        raise ValueError(f"非法探测区域（宽高必须为正）: {region!r}")
    settle_ms = max(0, int(settle_ms))

    tried: list[str] = []
    last: _Attempt | None = None
    for hot in _candidates(region):
        at = _try_point(
            grabber, region, hot, settle_ms, threshold, wheel, set_cursor, measure_unit
        )
        tried.append(f"({hot[0]},{hot[1]}) {at.log}")
        last = at
        if at.moved:
            return ProbeResult(
                scrollable=True,
                direction=at.direction,
                unit_px=float(at.unit_px),
                hot_point=hot,
                note=_note_moved(at),
                tried=tried,
                restored=bool(at.restored),
                unit_steps=int(at.unit_steps),
            )

    cx = region[0] + region[2] // 2
    cy = region[1] + region[3] // 2
    return ProbeResult(
        scrollable=False,
        direction="none",
        unit_px=0.0,
        hot_point=(cx, cy),
        note=(
            f"不可滚动：该区域对滚轮无响应（静态画面 / 工具栏 / 图片预览），"
            f"{len(tried)} 个候选点均无位移。"
        ),
        tried=tried,
        restored=bool(last.restored) if last else True,
        unit_steps=0,
    )
