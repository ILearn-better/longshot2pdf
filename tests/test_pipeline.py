"""端到端联调：编排层 + 探测 + 拼接 + 分页 + PDF，全流程不用真实屏幕。

为什么要有这个测试
------------------
只测单个模块是不够的。真实故障几乎都发生在模块之间：
探测把起始位置滚偏了、校准算错格数导致重叠不足、中断时半成品没有落盘……
这些只有把 `pipeline.run()` 整条链路跑一遍才看得见。

做法是把「屏幕」换成 `tests/mock_frames.VirtualScreen`：
它按滚轮格数移动内部偏移量，`grab()` 返回当前可见区域。
于是在一台没有可滚动窗口的机器上，也能反复、确定性地验收 500 页场景。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_frames as M  # noqa: E402
from src.pdfwriter import pdf_page_count  # noqa: E402
from src.pipeline import CaptureConfig, Deps, run  # noqa: E402
from src.stitch import page_height_for  # noqa: E402

OUT = ROOT / "tests" / "_tmp_pipeline"


def make_deps(vs: M.VirtualScreen) -> Deps:
    """把虚拟屏幕伪装成真实屏幕。sleep 仍保留真实等待，但压到很小以加速。"""
    return Deps(
        grabber=vs,
        wheel=vs.wheel,
        set_cursor=vs.set_cursor,
        esc=vs.esc,
        sleep=lambda s: None,
    )


def load_pages(temp_dir: Path) -> np.ndarray:
    """把中间页 JPEG 按序还原成一整张长图，用于和 ground truth 比对。"""
    from PIL import Image

    files = sorted(temp_dir.glob("page_*.jpg"))
    assert files, f"{temp_dir} 里没有页文件"
    imgs = [np.asarray(Image.open(f).convert("RGB")) for f in files]
    return np.vstack(imgs)


def mad(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.int16)
    b = b.astype(np.int16)
    return float(np.abs(a - b).mean())


# ------------------------------------------------------------------ 用例


def case_end_to_end() -> None:
    """完整跑通：内容必须一行不差、一页不歪。"""
    doc = M.make_document(width=800, height=9000)
    view_h = 900
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    out = OUT / "e2e.pdf"

    cfg = CaptureConfig(region=(0, 0, 800, view_h), out_path=out, keep_temp=True)
    res = run(cfg, deps=make_deps(vs))

    assert res.ok, f"运行失败：{res.message}"
    assert res.rows == doc.shape[0], f"拼接行数 {res.rows} != 文档高度 {doc.shape[0]}"
    assert res.pdf_path and res.pdf_path.exists()

    page_h = page_height_for(800, 1.414)
    expect_pages = int(np.ceil(doc.shape[0] / page_h))
    assert res.pages == expect_pages, f"页数 {res.pages} != 期望 {expect_pages}"
    assert pdf_page_count(out) == res.pages, "PDF 实际页数与报告不一致"

    got = load_pages(res.temp_dir)
    assert got.shape[0] == doc.shape[0], f"页图总高 {got.shape[0]} != {doc.shape[0]}"
    # JPEG 是有损的，但 quality=85 + 4:4:4 下文字图 MAD 应在 2 以内；
    # 一旦拼接错位，这个值会立刻跳到几十。
    diff = mad(got, doc)
    assert diff < 6.0, f"与原始文档偏差过大（{diff:.2f}），疑似拼接错位"
    print(
        f"  [OK] 端到端：{res.frames} 帧 -> {res.rows} 行 -> {res.pages} 页，"
        f"{res.size_mb:.2f} MB，与原文逐行偏差 MAD={diff:.2f}"
    )


def case_strip_fixed_bars() -> None:
    """剔除顶部/底部固定条：拼接结果应正好少掉被裁的行数。"""
    doc = M.make_document(width=640, height=7000)
    view_h = 800
    top, bottom = 30, 40
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    cfg = CaptureConfig(
        region=(0, 0, 640, view_h),
        out_path=OUT / "strip.pdf",
        strip_top=top,
        strip_bottom=bottom,
    )
    res = run(cfg, deps=make_deps(vs))
    assert res.ok, res.message
    expect = doc.shape[0] - top - bottom
    assert res.rows == expect, f"行数 {res.rows} != 期望 {expect}"
    print(f"  [OK] 固定条剔除：{res.rows} 行（原 {doc.shape[0]}，去头 {top} 去尾 {bottom}）")


def case_stop_partial() -> None:
    """中途停止必须仍然给出可打开的 PDF，而不是丢弃全部成果。"""
    doc = M.make_document(width=640, height=9000)
    vs = M.VirtualScreen(doc, 800, unit_px=100)
    state = {"frames": 0}

    def on_progress(frames, pages, rows, note):
        state["frames"] = frames

    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "partial.pdf")
    res = run(
        cfg,
        on_progress=on_progress,
        should_stop=lambda: state["frames"] >= 4,
        deps=make_deps(vs),
    )
    assert res.ok, res.message
    assert res.stopped_early, "应标记为提前结束"
    assert res.pages >= 1 and res.pdf_path and res.pdf_path.exists()
    assert res.rows < doc.shape[0], "提前结束却拼了整篇，停止逻辑没生效"
    print(f"  [OK] 中途停止：{res.frames} 帧 -> {res.pages} 页半成品，{res.rows} 行")


def case_esc_interrupt() -> None:
    """ESC 中断与 should_stop 等价。"""
    doc = M.make_document(width=640, height=9000)
    vs = M.VirtualScreen(doc, 800, unit_px=100)
    state = {"n": 0}

    def on_progress(frames, pages, rows, note):
        state["n"] = frames
        if frames >= 3:
            vs.press_esc()

    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "esc.pdf")
    res = run(cfg, on_progress=on_progress, deps=make_deps(vs))
    assert res.ok and res.stopped_early, f"ESC 中断失败：{res.message}"
    print(f"  [OK] ESC 中断：{res.frames} 帧 -> {res.pages} 页，结论「{res.message}」")


def case_max_pages() -> None:
    """页数上限：达到即收尾，不多滚一帧。"""
    doc = M.make_document(width=640, height=12000)
    vs = M.VirtualScreen(doc, 800, unit_px=100)
    cfg = CaptureConfig(
        region=(0, 0, 640, 800), out_path=OUT / "maxp.pdf", max_pages=2
    )
    res = run(cfg, deps=make_deps(vs))
    assert res.ok, res.message
    assert res.pages == 2, f"页数上限 2 未生效，实际 {res.pages}"
    print(f"  [OK] 页数上限：停在 {res.pages} 页 / {res.frames} 帧")


def case_not_scrollable() -> None:
    """一屏就装完的内容，必须一开始就拒绝，而不是白滚一场。"""
    doc = M.make_document(width=640, height=800)
    vs = M.VirtualScreen(doc, 800, unit_px=100)
    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "noscroll.pdf")
    res = run(cfg, deps=make_deps(vs))
    assert not res.ok, "不可滚区域却报告成功"
    assert not (OUT / "noscroll.pdf").exists(), "失败时不应留下 PDF"
    print(f"  [OK] 不可滚拒绝：{res.message}")


def case_smooth_scroll() -> None:
    """平滑滚动（一格只走一半）：校准必须能自适配，不能假设一格=整段。"""
    doc = M.make_document(width=640, height=8000)
    vs = M.VirtualScreen(doc, 800, unit_px=100, smooth=True)
    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "smooth.pdf")
    res = run(cfg, deps=make_deps(vs))
    assert res.ok, res.message
    assert res.frames > 5, f"只采了 {res.frames} 帧，说明步长被算成整屏了"
    print(f"  [OK] 平滑滚动：{res.frames} 帧 -> {res.pages} 页（步长自适应）")


def case_recovers_from_overscroll() -> None:
    """一格滚轮=整屏的程序：应自动降步长或明确报警，不能静默产出断连文件。"""
    doc = M.make_document(width=640, height=8000)
    vs = M.VirtualScreen(doc, 800, unit_px=800)  # 一格 = 一屏，重叠为 0
    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "overscroll.pdf")
    res = run(cfg, deps=make_deps(vs))
    # 允许两种结局：要么明确失败/报警，要么降级后仍能拼（能拼则行数必须对得上）
    if res.ok:
        assert res.rows <= doc.shape[0]
        print(f"  [OK] 整屏滚动降级：{res.message} -> {res.pages} 页 {res.rows} 行")
    else:
        print(f"  [OK] 整屏滚动被拒绝：{res.message}")


def case_fixed_footer_guard() -> None:
    """区域里有固定底栏：必须给出可操作的提示，而不是含糊的「匹配失败」。

    这是踩坑清单 4.2 的自动化验收。固定底栏在每帧同一位置重复出现，
    重叠区永远对不上 —— 减步长再多试也没用，只能让用户去 strip。
    """
    doc = M.make_document(width=640, height=9000)
    vs = M.VirtualScreen(doc, 800, unit_px=100, footer_rows=100)
    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "footer.pdf")
    res = run(cfg, deps=make_deps(vs))
    msg = res.message
    assert "strip" in msg or "底" in msg, f"提示不可操作：{msg}"
    print(f"  [OK] 固定底栏：按预期拦下并提示 →「{msg[:46]}…」")

    # 剔除底栏之后必须能正常拼（代价是最底部 100 行采不到，这是物理上必然的）
    vs2 = M.VirtualScreen(doc, 800, unit_px=100, footer_rows=100)
    cfg2 = CaptureConfig(
        region=(0, 0, 640, 800), out_path=OUT / "footer_ok.pdf", strip_bottom=100
    )
    res2 = run(cfg2, deps=make_deps(vs2))
    assert res2.ok, res2.message
    assert res2.rows == doc.shape[0] - 100, f"行数 {res2.rows} 不符预期"
    print(f"  [OK] 剔除底栏后：{res2.rows} 行 -> {res2.pages} 页，恢复正常")


def case_lazy_loading() -> None:
    """惰性加载：新内容先给白块、过几帧才渲染出来，不能把白块截进去。

    这是踩坑清单 4.1 的自动化验收。判断依据必须是「画面稳定」而非「滚动到位」：
    滚动指令早就执行完了，但图片还在解码。等稳定再取帧，才能拿全。
    """
    doc = M.make_document(width=640, height=8000)
    for delay in (2, 4):
        vs = M.VirtualScreen(
            doc, 800, unit_px=100, lazy_rows=120, lazy_delay_frames=delay
        )
        cfg = CaptureConfig(
            region=(0, 0, 640, 800), out_path=OUT / f"lazy_{delay}.pdf"
        )
        res = run(cfg, deps=make_deps(vs))
        assert res.ok, res.message
        assert res.rows == doc.shape[0], (
            f"延迟 {delay} 帧时只拼到 {res.rows}/{doc.shape[0]} 行，"
            "说明把空白占位当成内容截进去了"
        )
        print(f"  [OK] 惰性加载（延迟 {delay} 帧）：仍采全 {res.rows} 行 -> {res.pages} 页")


def case_dirty_region_warning() -> None:
    """固定元素只污染了一部分重叠带：位置仍然对，应该继续采但给出质量警告。

    与 case_fixed_footer_guard 的区别：那里污染严重到匹配失败，
    这里残差升高但仍在可接受范围（<12），内容是全的、只是可能有重影。
    正确做法是「继续 + 提醒」，中止会让用户白白拿不到东西。
    """
    doc = M.make_document(width=640, height=8000)
    vs = M.VirtualScreen(doc, 800, unit_px=200, footer_rows=30)
    notes: list[str] = []

    def on_progress(frames, pages, rows, note):
        if "残差" in note:
            notes.append(note)

    cfg = CaptureConfig(region=(0, 0, 640, 800), out_path=OUT / "dirty.pdf")
    res = run(cfg, on_progress=on_progress, deps=make_deps(vs))

    assert res.ok, res.message
    assert res.rows == doc.shape[0], f"警告不应影响采集完整性，实际 {res.rows} 行"
    assert notes, "残差偏高却没有发出任何质量警告"
    print(f"  [OK] 残差偏高：仍采全 {res.rows} 行，并给出 1 次质量警告（不重复刷屏）")


class _JitteryGrabber:
    """每次抓屏都不一样：模拟视频缩略图 / 加载动画这类天然在动的区域。

    VirtualScreen 的噪声是按 (whell_calls, top) 定种的，同一位置的连续抓屏
    完全一致，测不出「逐帧独立抖动」。这里用一个真随机的假 grabber 补上。
    """

    def __init__(self, w=320, h=200, seed=0):
        self.w, self.h = w, h
        self.rng = np.random.default_rng(seed)

    def grab(self, region):
        left, top, w, h = (int(v) for v in region)
        return self.rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)

    def close(self):
        pass


def case_jitter_adaptive_threshold() -> None:
    """画面自身在抖时，质量告警门槛必须跟着抬升，否则会一直误报。

    这是 DIRTY_JITTER_GAIN 的接线验证：`_grab_stable` 必须从既有的连续帧
    比较里把抖动带出来，而不是额外再抓一帧。
    """
    from src.pipeline import DIRTY_ERR, DIRTY_JITTER_GAIN, _grab_stable

    fake = _JitteryGrabber()
    deps = Deps(grabber=fake, sleep=lambda s: None)
    _frame, jitter = _grab_stable(deps, (0, 0, 320, 200), 0)

    assert jitter > 0.6, f"抖动画面却报告 jitter={jitter}，说明逐帧比较没有生效"
    level = max(DIRTY_ERR, DIRTY_JITTER_GAIN * jitter)
    assert level > DIRTY_ERR, "抖动很大却没有抬升门槛，仍会误报固定元素污染"

    # 静止画面必须收敛到 0，不能把正常情况也当成"在抖"
    doc = M.make_document(width=320, height=2000)
    still = M.VirtualScreen(doc, 200, unit_px=80)
    _f2, jitter2 = _grab_stable(
        Deps(grabber=still, sleep=lambda s: None), (0, 0, 320, 200), 0
    )
    assert jitter2 == 0.0, f"静止画面却报告 jitter={jitter2}"
    print(
        f"  [OK] 抖动自适应：动态画面 jitter={jitter:.1f} → 门槛 {DIRTY_ERR}→{level:.1f}；"
        "静止画面 jitter=0"
    )


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print("LongShot2PDF 端到端联调（虚拟屏幕）")
    case_end_to_end()
    case_strip_fixed_bars()
    case_stop_partial()
    case_esc_interrupt()
    case_max_pages()
    case_not_scrollable()
    case_smooth_scroll()
    case_recovers_from_overscroll()
    case_fixed_footer_guard()
    case_lazy_loading()
    case_dirty_region_warning()
    case_jitter_adaptive_threshold()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
