"""编排层：探测 → 首帧 → 校准 → 循环采集 → 分页落盘 → 生成 PDF。

这里不实现具体算法，只负责把它们按正确顺序串起来，并处理所有"意外"：
滚不动、滚过头、匹配失败、中途中断、磁盘/内存异常。

三个设计决定
------------
1) **依赖注入**。真实运行时用 mss + SendInput，测试时换成 VirtualScreen。
   编排逻辑本身不该和"屏幕"绑死，否则端到端测试只能靠人肉点鼠标。
2) **优先级：先出半成品，再谈完美**。用户按 ESC 或点停止，已经采到的内容
   必须马上变成可打开的 PDF。500 页滚到第 400 页崩了却什么都不给，等于全废。
3) **探测的实测值要复用**。probe 已经测出「一格滚轮 = 多少像素」，
   编排层如果还要再滚一格重新校准，等于白滚一次、多一次起始位置漂移的机会。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import winapi
from .capture import Grabber, Region
from .pdfwriter import A4_WIDTH_PT, jpegs_to_pdf
from .probe import probe
from .stitch import StreamStitcher, locate, page_height_for

# on_progress(帧数, 页数, 已拼行数, 人类可读状态)
ProgressFn = Callable[[int, int, int, str], None]
StopFn = Callable[[], bool]


# ---------------------------------------------------------------- 配置与结果


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
    max_frames: int = 20000        # 安全上限，防止死循环
    max_pages: int = 0             # 0 = 不限；>0 时达到页数即收尾
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
    stopped_early: bool = False    # 中途被打断，但已导出半成品 PDF
    temp_dir: Path | None = None   # keep_temp=True 时指向中间页目录（便于事后核对）
    stage: str = "done"            # 失败发生在哪一步，供 GUI/CLI 定位问题
    #   "config"   参数/区域不合法，还没开始采集
    #   "probe"    探测判定不可滚动（或已在底部），一次都没滚
    #   "capture"  采集过程中断/异常
    #   "done"     正常结束（含提前停止）

    def summary(self) -> str:
        if not self.ok:
            return f"失败：{self.message}"
        head = "已停止（半成品）" if self.stopped_early else "完成"
        return (
            f"{head}：{self.pages} 页 / {self.frames} 帧 / "
            f"{self.rows} 行 / {self.size_mb:.2f} MB → {self.pdf_path}"
        )


@dataclass
class Deps:
    """依赖注入点。全部留空则使用真实 Windows 屏幕与全局滚轮投递。

    测试用法：
        vs = VirtualScreen(doc, view_h=900)
        deps = Deps(grabber=vs, wheel=vs.wheel, set_cursor=vs.set_cursor,
                    esc=vs.esc, sleep=lambda s: None)
    """

    grabber: Any | None = None
    wheel: Callable[[int], None] | None = None
    set_cursor: Callable[[int, int], None] | None = None
    esc: Callable[[], bool] | None = None
    sleep: Callable[[float], None] | None = None
    enable_dpi: Callable[[], None] | None = None
    # 光标归位（真实运行时用，虚拟屏幕传 None 即跳过）
    save_cursor: Callable[[], tuple[int, int] | None] | None = None
    restore_cursor: Callable[[tuple[int, int] | None], None] | None = None


def _resolve(deps: Deps | None) -> Deps:
    d = deps or Deps()
    return Deps(
        grabber=d.grabber,
        wheel=d.wheel or winapi.wheel,
        set_cursor=d.set_cursor or winapi.set_cursor_pos,
        esc=d.esc or winapi.esc_pressed,
        sleep=d.sleep or time.sleep,
        enable_dpi=d.enable_dpi or winapi.enable_dpi_awareness,
        save_cursor=d.save_cursor or _safe_save_cursor,
        restore_cursor=d.restore_cursor or _safe_restore_cursor,
    )


def _safe_save_cursor() -> tuple[int, int] | None:
    """优先用 winapi.save_cursor（它会顺带判断是否真的能取到光标）。"""
    try:
        saver = getattr(winapi, "save_cursor", None)
        if saver is not None:
            return saver()
    except Exception:  # noqa: BLE001
        pass
    try:
        return winapi.get_cursor_pos()
    except Exception:  # noqa: BLE001
        return None


def _safe_restore_cursor(pos: tuple[int, int] | None) -> None:
    if pos is None:
        return
    try:
        restorer = getattr(winapi, "restore_cursor", None)
        if restorer is not None:
            restorer(pos)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        winapi.set_cursor_pos(*pos)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 小工具


def _mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


# 残差「偏高但未失败」的判定线。
# 正常情况下匹配残差 < 1；有固定表头/悬浮按钮压在重叠区上时会抬到 5~10，
# 但位置依然是对的（只是重叠区被污染了一部分）。这种情况不该中止采集，
# 而应该**边采边警告**：让用户知道成品可能有重影，下次记得用 strip 剔除。
# 上界交给 stitch 的 max_error(12)，超过它就直接判失败，不会走到这里。
DIRTY_ERR = 3.0


def _stable_box(region: Region) -> Region:
    """取区域下部一条，用于判断「画面是否还在动」。

    为什么是下部：滚动后新内容是从底部进来的，惰性加载、图片解码、
    列表补页这些延迟现象也都先出现在下部。盯住这里比盯中心更早发现问题。
    """
    x, y, w, h = region
    bh = max(24, int(h * 0.4))
    return (x, y + h - bh, w, bh)


def _grab_stable(
    deps: Deps,
    region: Region,
    settle_ms: int,
    tol: float = 0.6,
    max_tries: int = 6,
) -> np.ndarray:
    """等画面稳定后再取全帧。

    判断依据是「画面不再变化」而不是「等够了时间」——
    因为不同程序的滚动有惯性、有延迟渲染、有图片异步加载，
    固定 sleep 一定会截到中间态。
    """
    deps.sleep(settle_ms / 1000.0)
    box = _stable_box(region)
    ref = deps.grabber.grab(box)
    for _ in range(max_tries):
        deps.sleep(0.045)
        cur = deps.grabber.grab(box)
        if _mad(cur, ref) < tol:
            break
        ref = cur
    return deps.grabber.grab(region)


# ---------------------------------------------------------------- 主流程


def run(
    cfg: CaptureConfig,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    deps: Deps | None = None,
) -> RunResult:
    d = _resolve(deps)

    def report(frames: int, pages: int, rows: int, note: str) -> None:
        if on_progress:
            try:
                on_progress(frames, pages, rows, note)
            except Exception:  # noqa: BLE001
                pass

    d.enable_dpi()

    # ---------- 0. 区域与输出 ----------
    region: Region = tuple(int(v) for v in cfg.region)  # type: ignore[assignment]
    if cfg.strip_top or cfg.strip_bottom:
        left, top, w, h = region
        region = (
            left,
            top + cfg.strip_top,
            w,
            h - cfg.strip_top - cfg.strip_bottom,
        )
    if region[2] < 40 or region[3] < 40:
        return RunResult(
            False,
            stage="config",
            message="截取区域（扣除固定条后）过小，宽度和高度都需 ≥ 40px",
        )

    out_path = Path(cfg.out_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="longshot_"))
    page_h = page_height_for(region[2], cfg.page_ratio)
    stitcher = StreamStitcher(
        width=region[2], page_h=page_h, out_dir=tmp_dir, quality=cfg.quality
    )

    grabber = d.grabber
    owned_grabber = False
    if grabber is None:
        grabber = Grabber()
        d.grabber = grabber
        owned_grabber = True

    total_frames = 0
    last_note = ""
    stopped = False
    finished = False
    cursor_before = d.save_cursor()

    try:
        # ---------- 1. 探测能否滚动 ----------
        report(0, 0, 0, "正在检测该位置是否响应滚轮…")
        pr = probe(
            grabber,
            region,
            cfg.settle_ms,
            wheel_fn=d.wheel,
            set_cursor_fn=d.set_cursor,
        )
        if not pr.scrollable:
            return RunResult(False, stage="probe", message=pr.note)
        if pr.direction == "up":
            # 探测确认能滚，但只有在底部时才向上有反应：直接开始会立刻到底
            return RunResult(False, stage="probe", message=pr.note)

        hot = pr.hot_point
        d.set_cursor(*hot)
        d.sleep(0.05)

        # 探测靠「滚下去再滚回来」判断能否滚动，归位不准的后果不是失败而是
        # **悄悄从偏移后的位置开始截**，用户拿到的 PDF 开头会缺一小段。
        # 这里不能静默，必须在进度里说出来。
        if not getattr(pr, "restored", True):
            report(
                0, 0, 0,
                "警告：探测未能确认滚动位置已完全归位，起始位置可能有轻微偏移",
            )

        # ---------- 2. 首帧 ----------
        report(0, 0, 0, "探测通过，正在采集首帧…")
        prev = _grab_stable(d, region, cfg.settle_ms)
        stitcher.append(prev)
        total_frames = 1

        # ---------- 3. 校准：一格滚轮 = 多少像素 ----------
        target_step = region[3] * (1.0 - cfg.overlap)
        unit_px = float(getattr(pr, "unit_px", 0.0) or 0.0)
        steps = 1
        if unit_px > 0:
            # probe 已经实测过，直接换算，省掉一次多余的滚动
            steps = max(1, min(50, int(round(target_step / unit_px))))
            last_note = (
                f"校准完成（复用探测实测）：一格滚轮 ≈ {unit_px:.0f}px，"
                f"每帧滚动 {steps} 格（目标 {int(target_step)}px）"
            )
            report(total_frames, stitcher.page_count, stitcher.total_rows, last_note)
        else:
            d.wheel(-1)
            nxt = _grab_stable(d, region, cfg.settle_ms)
            loc = locate(prev, nxt)
            if loc is not None:
                new_start, delta, _err = loc
                if delta > 0:
                    unit_px = float(delta)
                    stitcher.append(nxt[new_start:])
                    prev = nxt
                    total_frames += 1
                    steps = max(1, min(50, int(round(target_step / unit_px))))
            last_note = (
                f"校准完成：一格滚轮 ≈ {unit_px:.0f}px，"
                f"每帧滚动 {steps} 格（目标 {int(target_step)}px）"
                if unit_px
                else "未能测出一格滚轮的位移，改用最小步长继续"
            )
            report(total_frames, stitcher.page_count, stitcher.total_rows, last_note)

        # 一格滚轮就等于一整屏的程序（某些 PDF 阅读器的「翻页式」滚动）：
        # 相邻帧重叠为 0，拼接算法在数学上无解 —— 不是参数没调好。
        # 与其滚 3 次、失败 3 次、最后甩一句含糊的「匹配失败」，
        # 不如立刻说清原因和可选做法，并把首帧出一页。
        grain_too_coarse = unit_px >= region[3] * 0.98
        if grain_too_coarse:
            stopped = True
            last_note = (
                f"该界面一格滚轮 ≈ {unit_px:.0f}px，几乎等于整个视口（{region[3]}px），"
                "相邻两帧没有重叠区，无法无缝拼接。"
                "建议把框选高度调到半屏左右，让每次滚动都留出重叠区。"
            )
            report(total_frames, stitcher.page_count, stitcher.total_rows, last_note)

        # ---------- 4. 主循环 ----------
        stalls = 0          # 连续「滚了但画面没动」的次数
        retries = 0         # 连续匹配失败次数
        coarse_warned = False
        hit_max_pages = False
        dirty_run = 0       # 连续「残差偏高」的帧数
        dirty_warned = False

        while not stopped and total_frames < cfg.max_frames:
            if should_stop and should_stop():
                last_note = "已手动停止"
                stopped = True
                break
            if d.esc():
                last_note = "检测到 ESC，已停止"
                stopped = True
                break
            if cfg.max_pages and stitcher.page_count >= cfg.max_pages:
                last_note = f"已达页数上限 {cfg.max_pages}，正常收尾"
                hit_max_pages = True
                break

            d.wheel(-steps)
            cur = _grab_stable(d, region, cfg.settle_ms)
            loc = locate(prev, cur)

            if loc is None:
                # 多半滚过头了（一帧内位移超过一屏，重叠为 0）：
                # 先滚回去，再减半步长重试。宁可重滚也不要写入错位内容。
                retries += 1
                if retries > 3:
                    last_note = (
                        "连续匹配失败（滚动粒度过大或界面重绘），已停止；"
                        "已采集部分会正常导出"
                    )
                    # 最常见的真实原因不是「滚过头」，而是框选区域里混进了
                    # 固定表头 / 悬浮按钮 / 状态栏：它们在每帧的同一位置重复出现，
                    # 会让重叠区永远对不上。这时减步长再怎么试都没用，
                    # 必须让用户去剔除固定条，否则他会以为是程序坏了。
                    if not (cfg.strip_top or cfg.strip_bottom):
                        last_note += (
                            "。若截图区域内含固定表头、底栏、悬浮按钮或滚动条，"
                            "请用 --strip-top / --strip-bottom 剔除后重试"
                        )
                    stopped = True
                    break
                d.wheel(steps)          # 滚回
                d.sleep(0.15)
                steps = max(1, steps // 2)
                report(
                    total_frames, stitcher.page_count, stitcher.total_rows,
                    f"匹配失败，步长降为 {steps} 格后重试",
                )
                continue

            retries = 0
            new_start, delta, _err = loc

            # 残差偏高 = 位置对但重叠区被污染（固定表头压在重叠带上等）。
            # 不中止，只提醒：中止会让用户拿不到东西，而重影是"能修"的。
            if _err > DIRTY_ERR:
                dirty_run += 1
                if (
                    dirty_run >= 3
                    and not dirty_warned
                    and not (cfg.strip_top or cfg.strip_bottom)
                ):
                    dirty_warned = True
                    report(
                        total_frames, stitcher.page_count, stitcher.total_rows,
                        f"警告：连续 {dirty_run} 帧匹配残差偏高（≈{_err:.1f}），"
                        "区域内可能有固定元素压在重叠带，成品可能有轻微重影；"
                        "建议用 --strip-top / --strip-bottom 剔除后重采",
                    )
            else:
                dirty_run = 0

            if delta <= 0:
                # 画面完全没变，连续几次才认定到底（一次可能只是加载中）
                stalls += 1
                if stalls >= 2:
                    last_note = "已滚动到底部，采集结束"
                    break
                d.sleep(0.12)
                continue

            stalls = 0
            stitcher.append(cur[new_start:])
            prev = cur
            total_frames += 1

            # 重叠不足会直接导致拼接风险，必须让用户知道
            if not coarse_warned and delta >= region[3] * 0.95:
                coarse_warned = True
                report(
                    total_frames, stitcher.page_count, stitcher.total_rows,
                    "警告：单帧位移接近整屏，重叠不足 5%，拼接可能断连",
                )

            # 步长自适应微调：目标是一帧走 target_step
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
        finished = True
        report(total_frames, stitcher.page_count, stitcher.total_rows, "正在生成 PDF…")
        pages = stitcher.finish()
        if not pages:
            return RunResult(
                False, stage="capture", message=(last_note or "") + " 未采集到任何内容"
            )
        # 页数上限是硬约束：finish() 会把不满一页的残页也刷出去，
        # 于是「限制 2 页」会实际输出 3 页。多出来的这页不算内容丢失
        # （它的行在下一页里不会出现，但用户要的就是这个上限），直接截掉。
        if hit_max_pages and len(pages) > cfg.max_pages:
            pages = pages[: cfg.max_pages]

        pdf = jpegs_to_pdf(pages, out_path, A4_WIDTH_PT, cfg.page_ratio)
        return RunResult(
            ok=True,
            pdf_path=pdf,
            pages=len(pages),
            frames=total_frames,
            rows=stitcher.total_rows,
            size_mb=pdf.stat().st_size / (1024 * 1024),
            message=last_note or "完成",
            stopped_early=stopped,
            temp_dir=tmp_dir if cfg.keep_temp else None,
        )

    except Exception as exc:  # noqa: BLE001
        # 异常也要尽量把已采内容变成可打开的 PDF：500 页采到第 400 页崩掉，
        # 直接什么都不给等于全部白费。
        salvaged: RunResult | None = None
        if not finished and stitcher.total_rows > 0:
            try:
                pages = stitcher.finish()
                if pages:
                    pdf = jpegs_to_pdf(pages, out_path, A4_WIDTH_PT, cfg.page_ratio)
                    salvaged = RunResult(
                        ok=True,
                        pdf_path=pdf,
                        pages=len(pages),
                        frames=total_frames,
                        rows=stitcher.total_rows,
                        size_mb=pdf.stat().st_size / (1024 * 1024),
                        message=f"采集异常后已抢救导出：{type(exc).__name__}: {exc}",
                        stopped_early=True,
                        temp_dir=tmp_dir if cfg.keep_temp else None,
                    )
            except Exception:  # noqa: BLE001
                salvaged = None
        if salvaged is not None:
            return salvaged
        return RunResult(
            False, stage="capture", message=f"采集异常：{type(exc).__name__}: {exc}"
        )
    finally:
        d.restore_cursor(cursor_before)
        if owned_grabber:
            try:
                grabber.close()
            except Exception:  # noqa: BLE001
                pass
        if not cfg.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            report(
                total_frames, stitcher.page_count, stitcher.total_rows,
                f"中间页已保留在 {tmp_dir}",
            )
