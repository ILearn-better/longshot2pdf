"""编排层：探测 → 首帧 → 校准 → 循环采集 → 分页落盘 → 生成 PDF。

这里不实现具体算法，只负责把它们按正确顺序串起来，并处理所有"意外"：
滚不动、滚过头、匹配失败、中途中断、磁盘/内存异常。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import winapi
from .capture import Grabber, Region
from .pdfwriter import A4_WIDTH_PT, jpegs_to_pdf
from .probe import probe
from .stitch import StreamStitcher, locate, page_height_for

ProgressFn = Callable[[int, int, int, str], None]
StopFn = Callable[[], bool]


@dataclass
class CaptureConfig:
    region: Region
    out_path: Path
    overlap: float = 0.15          # 相邻帧重叠比例
    settle_ms: int = 180           # 滚动后等待渲染稳定的时间
    quality: int = 85              # JPEG 质量
    page_ratio: float = 1.414      # 页高 / 页宽
    strip_top: int = 0             # 裁掉顶部固定条（表头）
    strip_bottom: int = 0          # 裁掉底部固定条
    max_frames: int = 20000
    keep_temp: bool = False        # 保留中间页 JPEG（调试用）


@dataclass
class RunResult:
    ok: bool
    pdf_path: Path | None = None
    pages: int = 0
    frames: int = 0
    rows: int = 0
    size_mb: float = 0.0
    message: str = ""


def _mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _small_box(region: Region) -> Region:
    """取区域中部一小块，用于快速判断「画面是否还在动」。"""
    x, y, w, h = region
    bw = max(32, int(w * 0.5))
    bh = max(32, int(h * 0.3))
    return (x + (w - bw) // 2, y + (h - bh) // 2, bw, bh)


def _grab_stable(
    grabber: Grabber,
    region: Region,
    settle_ms: int,
    tol: float = 0.6,
    max_tries: int = 5,
) -> np.ndarray:
    """等画面稳定后再取全帧。

    判断依据是「画面不再变化」而不是「等够了时间」——
    因为不同程序的滚动有惯性、有延迟渲染、有图片异步加载，
    固定 sleep 一定会截到中间态。
    """
    time.sleep(settle_ms / 1000.0)
    box = _small_box(region)
    ref = grabber.grab(box)
    for _ in range(max_tries):
        time.sleep(0.045)
        cur = grabber.grab(box)
        if _mad(cur, ref) < tol:
            break
        ref = cur
    return grabber.grab(region)


def run(
    cfg: CaptureConfig,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
) -> RunResult:
    def report(frames: int, pages: int, rows: int, note: str) -> None:
        if on_progress:
            try:
                on_progress(frames, pages, rows, note)
            except Exception:
                pass

    winapi.enable_dpi_awareness()

    region: Region = tuple(int(v) for v in cfg.region)  # type: ignore[assignment]
    if cfg.strip_top or cfg.strip_bottom:
        left, top, w, h = region
        region = (
            left,
            top + cfg.strip_top,
            w,
            h - cfg.strip_top - cfg.strip_bottom,
        )
    if region[2] <= 0 or region[3] <= 0:
        return RunResult(False, message="截取区域（扣除固定条后）无效")

    out_path = Path(cfg.out_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="longshot_"))
    grabber = Grabber()
    page_h = page_height_for(region[2], cfg.page_ratio)
    stitcher = StreamStitcher(
        width=region[2], page_h=page_h, out_dir=tmp_dir, quality=cfg.quality
    )

    total_frames = 0
    last_note = ""
    try:
        # ---------- 1. 探测能否滚动 ----------
        report(0, 0, 0, "正在检测该位置是否响应滚轮…")
        pr = probe(grabber, region, cfg.settle_ms)
        if not pr.scrollable:
            return RunResult(False, message=pr.note)
        hot = pr.hot_point
        winapi.set_cursor_pos(*hot)
        time.sleep(0.05)

        # ---------- 2. 首帧 ----------
        report(0, 0, 0, "探测通过，正在采集首帧…")
        head = _grab_stable(grabber, region, cfg.settle_ms)
        stitcher.append(head)
        prev = head
        total_frames = 1

        # ---------- 3. 校准：量出「一格滚轮 = 多少像素」 ----------
        target_step = region[3] * (1.0 - cfg.overlap)
        steps = 1
        unit_px = 0
        winapi.wheel(-1)
        nxt = _grab_stable(grabber, region, cfg.settle_ms)
        loc = locate(prev, nxt)
        if loc is not None:
            new_start, delta, _err = loc
            if delta > 0:
                unit_px = delta
                stitcher.append(nxt[new_start:])
                prev = nxt
                total_frames += 1
                steps = max(1, min(50, int(round(target_step / unit_px))))

        if not unit_px:
            # 一格都没有位移：要么已到底，要么粒度极细。用 1 格继续试。
            steps = 1
            report(total_frames, stitcher.page_count, stitcher.total_rows,
                   "未能测出一格滚轮的位移，改用最小步长继续")

        last_note = (
            f"校准完成：一格滚轮 ≈ {unit_px}px，"
            f"每帧滚动 {steps} 格（目标 {int(target_step)}px）"
            if unit_px
            else "校准未取得位移"
        )
        report(total_frames, stitcher.page_count, stitcher.total_rows, last_note)

        # ---------- 4. 主循环 ----------
        stalls = 0
        retries = 0
        last_delta = unit_px
        while total_frames < cfg.max_frames:
            if should_stop and should_stop():
                last_note = "已手动停止"
                break
            if winapi.esc_pressed():
                last_note = "检测到 ESC，已停止"
                break

            winapi.wheel(-steps)
            cur = _grab_stable(grabber, region, cfg.settle_ms)
            loc = locate(prev, cur)

            if loc is None:
                # 多半是滚过头了（一帧内位移超过一屏），退回并减小步长重试
                retries += 1
                if retries > 3:
                    last_note = "连续匹配失败（可能是滚动粒度过大或界面重绘），已停止"
                    break
                winapi.wheel(steps)
                time.sleep(0.15)
                steps = max(1, steps // 2)
                report(total_frames, stitcher.page_count, stitcher.total_rows,
                       f"匹配失败，步长降为 {steps} 格后重试")
                continue

            retries = 0
            new_start, delta, _err = loc

            if delta <= 0:
                stalls += 1
                if stalls >= 2:
                    last_note = "已滚动到底部，采集结束"
                    break
                continue

            stalls = 0
            stitcher.append(cur[new_start:])
            prev = cur
            total_frames += 1
            last_delta = delta

            # 步长自适应微调
            if delta > target_step * 1.35 and steps > 1:
                steps -= 1
            elif delta < target_step * 0.65 and steps < 50:
                steps += 1

            report(
                total_frames,
                stitcher.page_count,
                stitcher.total_rows,
                f"已采集 {total_frames} 帧 · {stitcher.total_rows} 行 "
                f"· {stitcher.page_count} 页 · 本帧位移 {delta}px",
            )

        # ---------- 5. 收尾 ----------
        report(total_frames, stitcher.page_count, stitcher.total_rows, "正在生成 PDF…")
        pages = stitcher.finish()
        if not pages:
            return RunResult(False, message=(last_note or "") + " 未采集到任何内容")

        pdf = jpegs_to_pdf(pages, out_path, A4_WIDTH_PT, cfg.page_ratio)
        size_mb = pdf.stat().st_size / (1024 * 1024)

        return RunResult(
            ok=True,
            pdf_path=pdf,
            pages=len(pages),
            frames=total_frames,
            rows=stitcher.total_rows,
            size_mb=size_mb,
            message=last_note or "完成",
        )
    except Exception as exc:  # noqa: BLE001
        return RunResult(False, message=f"采集异常：{type(exc).__name__}: {exc}")
    finally:
        grabber.close()
        if not cfg.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            report(total_frames, stitcher.page_count, stitcher.total_rows,
                   f"中间页已保留在 {tmp_dir}")
