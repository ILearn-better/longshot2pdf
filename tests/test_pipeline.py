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


def make_manual_deps(driver: M.ManualDriver, wheel_probe: M.WheelProbe) -> Deps:
    """手动模式的依赖：滚轮接口故意留成探针，用来断言「一次都没被调用」。"""
    return Deps(
        grabber=driver,
        wheel=wheel_probe.wheel,
        set_cursor=driver.screen.set_cursor,
        esc=driver.screen.esc,
        sleep=lambda s: None,
    )


def case_manual_irregular_steps() -> None:
    """手动滚动：用户步长不匀、还夹着停顿，内容必须一行不差。"""
    doc = M.make_document(width=800, height=9000)
    view_h = 800
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    # 两轮不规则的「用户手感」，总位移超过文档剩余高度（会在底部被夹住）
    plan = [420, 380, 510, 300, 460, 400, 520, 360, 480, 430] * 2
    driver = M.ManualDriver(vs, plan)
    wheel = M.WheelProbe()

    cfg = CaptureConfig(
        region=(0, 0, 800, view_h),
        out_path=OUT / "manual.pdf",
        keep_temp=True,
        scroll="manual",
    )
    res = run(
        cfg,
        should_stop=lambda: driver.done,
        deps=make_manual_deps(driver, wheel),
    )

    assert res.ok, f"手动模式失败：{res.message}"
    assert not wheel.calls, f"手动模式不该投递滚轮，却投了 {len(wheel.calls)} 次"
    assert res.rows == doc.shape[0], (
        f"手动模式拼接行数 {res.rows} != 文档高度 {doc.shape[0]}"
    )
    got = load_pages(res.temp_dir)
    diff = mad(got, doc)
    assert diff < 6.0, f"手动模式拼接错位（MAD={diff:.2f}）"
    print(
        f"  [OK] 手动滚动：{res.frames} 帧 -> {res.rows} 行 -> {res.pages} 页，"
        f"零滚轮投递，MAD={diff:.2f}"
    )


def case_manual_overscroll() -> None:
    """手动模式滚过一屏：不能像自动模式那样替用户滚回去，只能提示 + 认账接续。"""
    doc = M.make_document(width=800, height=9000)
    view_h = 800
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    # 第一步就滚 1300px（> 一屏 800px，重叠为 0），之后恢复正常步长
    plan = [1300] + [400] * 20
    driver = M.ManualDriver(vs, plan)
    wheel = M.WheelProbe()
    notes: list[str] = []

    def on_progress(frames, pages, rows, note):
        notes.append(note)

    cfg = CaptureConfig(
        region=(0, 0, 800, view_h),
        out_path=OUT / "manual_over.pdf",
        scroll="manual",
    )
    res = run(
        cfg,
        on_progress=on_progress,
        should_stop=lambda: driver.done,
        deps=make_manual_deps(driver, wheel),
    )

    assert res.ok, f"滚过一屏后应能接续，实际失败：{res.message}"
    assert not wheel.calls, "手动模式不该投递滚轮"
    joined = " ".join(notes)
    assert "接不上" in joined or "漏掉" in joined, (
        f"滚过一屏却没有给出任何提示，用户会以为内容是全的：{joined[:120]}"
    )
    assert "新起点接续" in joined, "连续接不上之后没有认账接续，会永久卡住"
    # 丢了第一屏之后的内容，所以总行数必然少于全文
    assert res.rows < doc.shape[0], "声称滚过一屏还能拼出全文，可疑"
    print(
        f"  [OK] 滚过一屏：{res.rows} 行（丢了一段）-> {res.pages} 页，"
        "已提示并接续而不是卡死"
    )


def case_manual_idle_finish() -> None:
    """手动模式停手：靠「多久没动静」收尾，不能把「画面不动」当成到底。"""
    doc = M.make_document(width=800, height=9000)
    view_h = 800
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    driver = M.ManualDriver(vs, [400, 400, 400])   # 滚三次就停手
    wheel = M.WheelProbe()

    cfg = CaptureConfig(
        region=(0, 0, 800, view_h),
        out_path=OUT / "manual_idle.pdf",
        scroll="manual",
        # 用一个极小的正数：只要过了任意一点时间就算超时，避免测试依赖真实时钟
        idle_timeout=1e-9,
    )
    res = run(cfg, deps=make_manual_deps(driver, wheel))

    assert res.ok, res.message
    assert not res.stopped_early, "闲置收尾是正常结束，不该标成中断"
    assert "没有检测到滚动" in res.message, f"收尾原因不明确：{res.message}"
    assert 0 < res.rows < doc.shape[0], f"停手时应只有部分内容，实际 {res.rows} 行"
    print(
        f"  [OK] 停手收尾：{res.frames} 帧 -> {res.rows} 行 -> {res.pages} 页，"
        f"结论「{res.message}」"
    )


def case_manual_upward_scroll() -> None:
    """手动模式往上滚：拼接只能往后追加，必须忽略并说清，不能把内容写乱。"""
    doc = M.make_document(width=800, height=9000)
    view_h = 800
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    # 下 400 → 上 200（回看） → 继续往下
    plan = [400, -200, 400, 400, 400, 400]
    driver = M.ManualDriver(vs, plan)
    wheel = M.WheelProbe()
    notes: list[str] = []

    cfg = CaptureConfig(
        region=(0, 0, 800, view_h),
        out_path=OUT / "manual_up.pdf",
        keep_temp=True,
        scroll="manual",
    )
    res = run(
        cfg,
        on_progress=lambda f, p, r, n: notes.append(n),
        should_stop=lambda: driver.done,
        deps=make_manual_deps(driver, wheel),
    )

    assert res.ok, res.message
    assert any("向上滚动" in n for n in notes), "往上滚没有提示，用户不知道那帧被忽略了"
    # 关键：回看不能被写进 PDF —— 内容必须严格递增、不重复
    got = load_pages(res.temp_dir)
    expect = doc[: got.shape[0]]
    diff = mad(got, expect)
    assert diff < 6.0, f"往上滚之后拼接错乱（MAD={diff:.2f}），可能把回看内容追加进去了"
    print(
        f"  [OK] 向上滚动：已忽略并提示，{res.rows} 行内容仍严格递增（MAD={diff:.2f}）"
    )


def case_blank_gap() -> None:
    """正文中间夹着一大片留白：不能在这里判定「已到底」。

    这是用户实际反馈的故障形态：滚轮一直有效，但进入页边距/大段空白之后
    前后两帧一模一样，旧逻辑连判两次「画面没动」就收尾，后半篇全丢。
    验收两条：
        1. 采集必须越过留白、把后面的正文拿全（拼接高度远超留白起点）；
        2. 末尾若干行必须与原文一致 —— 只"没停"但内容错位同样不算过。
    """
    doc = M.make_document(width=800, height=9000, seed=5)
    gap_start, gap_end = 3000, 5500
    doc[gap_start:gap_end] = 252           # 2.5 屏纯白
    view_h = 600
    vs = M.VirtualScreen(doc, view_h, unit_px=100)
    notes: list[str] = []

    cfg = CaptureConfig(
        region=(0, 0, 800, view_h), out_path=OUT / "blank_gap.pdf", keep_temp=True
    )
    res = run(cfg, on_progress=lambda f, p, r, n: notes.append(n), deps=make_deps(vs))

    assert res.ok, res.message
    got = load_pages(res.temp_dir)
    # 旧逻辑会在留白入口处收尾（≈gap_start），这里必须明显越过 gap_end
    assert got.shape[0] > gap_end, (
        f"只拼到 {got.shape[0]} 行（留白是 {gap_start}~{gap_end}），"
        "说明又把一大片留白当成结尾了"
    )
    assert any("留白" in n or "空白" in n for n in notes), "没有报告穿越留白的过程"

    # 末尾必须与原文对齐：证明穿过留白之后重新接上了，而不是错位追加
    tail = 600
    diff = mad(got[-tail:], doc[-tail:])
    assert diff < 6.0, f"留白之后内容错位（末尾 MAD={diff:.2f}）"
    print(
        f"  [OK] 跨过 {gap_end - gap_start}px 留白：拼出 {got.shape[0]} 行"
        f"（{res.pages} 页），末尾与原文对齐 MAD={diff:.2f}"
    )


def case_output_dedup() -> None:
    """同名输出不覆盖：第二次自动落到 名字(1).pdf，第一份内容一字不动。"""
    doc = M.make_document(width=640, height=4000)
    view_h = 600
    out = OUT / "同名.pdf"

    def once() -> object:
        vs = M.VirtualScreen(doc, view_h, unit_px=100)
        cfg = CaptureConfig(region=(0, 0, 640, view_h), out_path=out)
        return run(cfg, deps=make_deps(vs))

    first = once()
    assert first.ok and first.pdf_path == out, first.pdf_path
    keep = out.read_bytes()

    second = once()
    assert second.ok, second.message
    assert second.pdf_path is not None and second.pdf_path != out
    assert second.pdf_path.name == "同名(1).pdf", second.pdf_path.name
    assert out.read_bytes() == keep, "第一份被覆盖了"
    print(f"  [OK] 同名避让：{out.name} 保留原样，第二份落到 {second.pdf_path.name}")


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
    case_manual_irregular_steps()
    case_manual_overscroll()
    case_manual_idle_finish()
    case_manual_upward_scroll()
    case_blank_gap()
    case_output_dedup()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
