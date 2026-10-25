"""探测内核离线验证（不依赖真实屏幕、不依赖真实鼠标）。

用 tests/mock_frames 的 VirtualScreen 把「长文档 + 滚动位置」伪装成一块屏幕，
把 vs.wheel / vs.set_cursor 作为 wheel_fn / set_cursor_fn 注入 probe()，
于是整条探测逻辑（滚轮 → 等稳定 → 比较 → 测位移 → 归位复检）都是真跑的，
只是像素和滚动状态来自内存而不是屏幕。

覆盖：
1. 可滚（在顶部）       → scrollable=True / direction="down" / unit_px 误差 ≤10%
2. 已在底部             → direction="up"（不能报成「不可滚」）
3. 不可滚（一屏装完）   → scrollable=False
4. 归位精度             → restored=True 且 offset 回到 0
5. 弱对比 / 大段空白    → 不误判成「不可滚」；静态抖动画面不误判成「可滚」
6. 反向（向上滚）也能测出 unit_px
7. 耗时                 → 单次 probe < 2 秒

运行： python tests/test_probe.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_frames import VirtualScreen, make_document  # noqa: E402

from src import probe as P  # noqa: E402
from src import winapi  # noqa: E402
from src.capture import normalize_region  # noqa: E402

SETTLE = 180
TIMINGS: list[tuple[str, int]] = []


def run(vs: VirtualScreen, region, label: str, **kw):
    """跑一次 probe，记录耗时。"""
    t0 = time.perf_counter()
    pr = P.probe(
        vs,
        region,
        SETTLE,
        wheel_fn=vs.wheel,
        set_cursor_fn=vs.set_cursor,
        **kw,
    )
    dt = int((time.perf_counter() - t0) * 1000)
    TIMINGS.append((label, dt))
    return pr, dt


def region_of(vs: VirtualScreen) -> tuple[int, int, int, int]:
    return (0, 0, vs.view_w, vs.view_h)


def unit_error(measured: float, truth: float) -> float:
    return abs(measured - truth) / max(1e-9, float(truth))


# ---------------------------------------------------------------- 用例


def case_scrollable_top() -> None:
    """① 顶部可滚：方向和位移都要测对。"""
    doc = make_document(width=1000, height=14000)
    vs = VirtualScreen(doc, view_h=900, unit_px=100)
    pr, dt = run(vs, region_of(vs), "可滚(顶部)")

    assert pr.scrollable, f"应判可滚，实际 note={pr.note}"
    assert pr.direction == "down", f"方向应为 down，实际 {pr.direction}"
    assert pr.unit_px > 0, "unit_px 必须实测出来，不能是 0"
    assert unit_error(pr.unit_px, 100) <= 0.10, (
        f"unit_px={pr.unit_px} 与真实 100px 误差 {unit_error(pr.unit_px, 100):.1%} > 10%"
    )
    assert pr.unit_steps >= 1, f"unit_steps 应记录格数，实际 {pr.unit_steps}"
    assert vs.cursor == pr.hot_point, f"光标应停在生效的滚动点 {pr.hot_point}，实际 {vs.cursor}"
    assert len(pr.tried) >= 1
    print(
        f"  [OK] 顶部可滚：scrollable={pr.scrollable} direction={pr.direction} "
        f"unit_px={pr.unit_px:.1f}（真实 100，误差 {unit_error(pr.unit_px, 100):.1%}）"
        f" unit_steps={pr.unit_steps} restored={pr.restored} {dt}ms"
    )
    print(f"       hot_point={pr.hot_point} note={pr.note}")
    print(f"       tried={pr.tried[0]}")


def case_already_bottom() -> None:
    """② 已在底部：向下没反应，向上有位移 → "up"，不是"不可滚"。"""
    doc = make_document(width=1000, height=12000)
    vs = VirtualScreen(doc, view_h=900, unit_px=100)
    vs.offset = vs.max_offset
    pr, dt = run(vs, region_of(vs), "已在底部")

    assert pr.scrollable, f"底部应判「可滚但方向向上」，实际 note={pr.note}"
    assert pr.direction == "up", f"方向应为 up，实际 {pr.direction}"
    assert pr.unit_px > 0, "反向也应测出 unit_px"
    assert unit_error(pr.unit_px, 100) <= 0.10, f"unit_px={pr.unit_px} 误差过大"
    assert vs.offset == vs.max_offset, f"归位后应仍在底部，实际 offset={vs.offset}"
    assert pr.restored, f"底部的滚动位置也应还原，实际 restored={pr.restored}"
    print(
        f"  [OK] 已在底部：scrollable={pr.scrollable} direction={pr.direction} "
        f"unit_px={pr.unit_px:.1f} offset 保持 {vs.offset}/{vs.max_offset} "
        f"restored={pr.restored} {dt}ms"
    )
    print(f"       note={pr.note}")


def case_not_scrollable() -> None:
    """③ 内容一屏装完（max_offset=0）：必须判不可滚，且 5 个候选点都试过。"""
    doc = make_document(width=800, height=600)
    vs = VirtualScreen(doc, view_h=doc.shape[0])  # view_h == 文档高 → 无处可滚
    assert vs.max_offset == 0
    pr, dt = run(vs, region_of(vs), "不可滚")

    assert not pr.scrollable, f"应判不可滚，实际 direction={pr.direction} note={pr.note}"
    assert pr.direction == "none"
    assert pr.unit_px == 0.0, f"不可滚时 unit_px 应为 0，实际 {pr.unit_px}"
    assert len(pr.tried) == 5, f"应记录全部 5 个候选点，实际 {len(pr.tried)}"
    assert bool(pr) is False
    print(
        f"  [OK] 不可滚：scrollable={pr.scrollable} direction={pr.direction} "
        f"候选点={len(pr.tried)} 个全部无位移 {dt}ms"
    )
    print(f"       note={pr.note}")
    for line in pr.tried:
        print(f"       · {line}")


def case_restore() -> None:
    """④ 归位精度：探测结束滚动位置必须回到探测前（≤1px）。"""
    doc = make_document(width=900, height=10000)
    vs = VirtualScreen(doc, view_h=800, unit_px=120)
    start = 0
    assert vs.offset == start
    pr, dt = run(vs, region_of(vs), "归位精度")

    drift = abs(vs.offset - start)
    assert pr.restored, f"restored 应为 True，实际 False（note={pr.note}）"
    assert drift <= 1, f"归位偏差 {drift}px > 1px"
    print(
        f"  [OK] 归位精度：restored={pr.restored} offset 回到 {vs.offset}（起始 {start}，"
        f"偏差 {drift}px）{dt}ms"
    )


def case_weak_contrast_and_noise() -> None:
    """⑤ 弱对比 / 大段空白不能误判"不可滚"；静态抖动不能误判"可滚"。"""
    # (a) 大段空白：顶部 400 行纯白，探测块 80% 是空白
    doc = make_document(width=800, height=9000, blank_margins=400)
    vs = VirtualScreen(doc, view_h=900, unit_px=100)
    region = region_of(vs)
    box = P._probe_box(region, P._candidates(region)[0])
    patch = vs.grab(box)
    blank_ratio = float((patch.reshape(-1, 3).min(axis=1) >= 250).mean())
    pr, dt = run(vs, region, "大段空白")
    assert pr.scrollable and pr.direction == "down", f"空白多的区域被误判: {pr.note}"
    assert pr.restored
    print(
        f"  [OK] 大段空白：探测块空白占比 {blank_ratio:.0%} 仍判可滚，"
        f"unit_px={pr.unit_px:.1f} restored={pr.restored} {dt}ms"
    )

    # (b) 弱对比：把墨色压到 10% 对比度，帧间差只有 ~9
    weak = np.clip(
        252 - (252 - doc.astype(np.float32)) * 0.10, 0, 255
    ).astype(np.uint8)
    vs = VirtualScreen(weak, view_h=700, unit_px=100)
    pr, dt = run(vs, region_of(vs), "弱对比")
    assert pr.scrollable and pr.direction == "down", f"弱对比被误判为不可滚: {pr.note}"
    print(
        f"  [OK] 弱对比(10%)：仍判可滚，unit_px={pr.unit_px:.1f} "
        f"阈值随画面自适应 {dt}ms"
    )

    # (c) 静态 + 逐帧抖动：阈值必须被抬高，不能被噪声顶成"可滚"
    flat_doc = make_document(width=700, height=600)
    for noise in (2, 3):
        vs = VirtualScreen(flat_doc, view_h=600, noise=noise)
        pr, dt = run(vs, region_of(vs), f"静态抖动{noise}")
        first = pr.tried[0]
        thr = float(first.split("阈 ")[1].split(" ")[0])
        assert not pr.scrollable, (
            f"noise={noise} 的静态画面被误判为可滚（阈 {thr:.2f}，{first}）"
        )
        print(
            f"  [OK] 静态+抖动(noise={noise})：未误判，阈值自动抬到 {thr:.2f} "
            f"（固定 1.5 会误判）{dt}ms"
        )


def case_reverse_direction() -> None:
    """⑥ 在底部向上滚：位移量也要测得准（换个 unit_px 证明不是写死的）。"""
    doc = make_document(width=900, height=14000)
    truth = 137
    vs = VirtualScreen(doc, view_h=900, unit_px=truth)
    vs.offset = vs.max_offset
    pr, dt = run(vs, region_of(vs), "反向测位移")

    assert pr.direction == "up", f"方向应为 up，实际 {pr.direction}"
    assert pr.unit_px > 0
    assert unit_error(pr.unit_px, truth) <= 0.10, (
        f"向上滚 unit_px={pr.unit_px} 与真实 {truth} 误差 "
        f"{unit_error(pr.unit_px, truth):.1%} > 10%"
    )
    print(
        f"  [OK] 反向(向上)测位移：unit_px={pr.unit_px:.1f}（真实 {truth}，"
        f"误差 {unit_error(pr.unit_px, truth):.1%}）restored={pr.restored} {dt}ms"
    )


def case_small_step_retry() -> None:
    """附加：一格只走 1~2px 时，用 3 格重测避免整数误差被放大。"""
    doc = make_document(width=800, height=9000)
    for truth in (1, 2):
        vs = VirtualScreen(doc, view_h=600, unit_px=truth)
        pr, dt = run(vs, region_of(vs), f"小粒度{truth}")
        assert pr.scrollable and pr.unit_px > 0
        assert pr.unit_steps == P.RETRY_STEPS, f"应触发 {P.RETRY_STEPS} 格重测，实际 {pr.unit_steps}"
        assert unit_error(pr.unit_px, truth) <= 0.10, (
            f"unit_px={pr.unit_px} 与真实 {truth} 误差过大"
        )
        print(
            f"  [OK] 小粒度 unit_px={truth}px：自动改用 {pr.unit_steps} 格重测，"
            f"测得 {pr.unit_px:.2f}px recovered_offset={vs.offset} {dt}ms"
        )


def case_locate_unavailable() -> None:
    """附加：locate 不可用（正在被重写/匹配失败）时，探测不能整体失败。"""
    doc = make_document(width=800, height=9000)

    import src.stitch as stitch_mod  # noqa: PLC0415

    original = stitch_mod.locate
    try:
        # (a) locate 返回 None → 降级用行剖面 _mad 估位移
        stitch_mod.locate = lambda prev, cur: None
        vs = VirtualScreen(doc, view_h=700, unit_px=100)
        pr, dt = run(vs, region_of(vs), "locate失效")
        assert pr.scrollable and pr.direction == "down", f"降级后仍应判可滚: {pr.note}"
        assert "profile" in pr.tried[0], f"应走 profile 降级路径: {pr.tried[0]}"
        assert unit_error(pr.unit_px, 100) <= 0.10, f"降级估计 {pr.unit_px}px 误差过大"
        assert pr.restored and vs.offset == 0

        # (b) locate 直接抛异常 → 同样不能影响结论
        def boom(prev, cur):
            raise RuntimeError("stitch 正在重写")

        stitch_mod.locate = boom
        vs = VirtualScreen(doc, view_h=700, unit_px=100)
        pr, dt = run(vs, region_of(vs), "locate抛异常")
        assert pr.scrollable, f"locate 抛异常不应导致探测失败: {pr.note}"
        print(
            f"  [OK] locate 不可用：返回 None / 抛异常两种情况下都能判可滚，"
            f"unit_px 降级为 {pr.unit_px:.1f}px（profile 行剖面估计）{dt}ms"
        )
    finally:
        stitch_mod.locate = original


def case_helpers() -> None:
    """附加：winapi / capture 的新增接口离线自检。"""
    import ctypes

    sx, sy, sw, sh = winapi.virtual_screen()
    assert sw > 0 and sh > 0, f"virtual_screen 返回非法尺寸 {(sx, sy, sw, sh)}"
    assert winapi.screen_size() == (sw, sh), "screen_size 应基于 virtual_screen"
    scale = winapi.get_dpi_scale()
    assert 0.5 <= scale <= 4.0, f"DPI 缩放异常 {scale}"
    # 64 位下 INPUT 必须是 40 字节（type 4 + padding 4 + union 32）
    assert ctypes.sizeof(winapi.INPUT) == 40, f"sizeof(INPUT)={ctypes.sizeof(winapi.INPUT)}"
    assert ctypes.sizeof(winapi.MOUSEINPUT) == 32
    assert winapi.MOUSEINPUT.dwExtraInfo.offset == 24, "dwExtraInfo 偏移应为 24"

    # 负坐标（副屏）裁剪：左侧超出桌面 100px → 裁掉
    assert normalize_region((-100, 0, 300, 200), (-1920, 0, 3840, 1080)) == (-100, 0, 300, 200)
    assert normalize_region((-2000, 0, 300, 200), (-1920, 0, 3840, 1080)) == (-1920, 0, 220, 200)
    try:
        normalize_region((5000, 5000, 100, 100), (0, 0, 1920, 1080))
    except ValueError:
        pass
    else:
        raise AssertionError("完全在屏幕外的区域应抛 ValueError")

    # 光标存取在无交互桌面的环境下也不能抛异常
    winapi.save_cursor()
    winapi.restore_cursor()
    print(
        f"  [OK] 辅助接口：virtual_screen={(sx, sy, sw, sh)} dpi={scale:.2f} "
        f"sizeof(INPUT)=40 normalize_region(负坐标) 正常"
    )


def case_timing() -> None:
    """⑦ 耗时：单次 probe 必须 < 2 秒（离线环境，无真实渲染等待）。"""
    worst = max(TIMINGS, key=lambda kv: kv[1])
    print("  [--] 各次 probe 耗时：")
    for label, dt in TIMINGS:
        print(f"       {label:14s} {dt:5d} ms")
    assert worst[1] < 2000, f"{worst[0]} 耗时 {worst[1]}ms ≥ 2000ms"
    total = sum(dt for _, dt in TIMINGS)
    print(
        f"  [OK] 耗时：最慢一次 probe = {worst[1]}ms（{worst[0]}），"
        f"共 {len(TIMINGS)} 次累计 {total}ms，全部 < 2000ms"
    )


def main() -> int:
    print("LongShot2PDF 探测内核离线实测")
    print(f"settle_ms={SETTLE}，滚动/光标全部由 VirtualScreen 注入，不碰真实桌面")
    print()
    print("① 可滚（顶部）")
    case_scrollable_top()
    print("② 已在底部")
    case_already_bottom()
    print("③ 不可滚（一屏装完）")
    case_not_scrollable()
    print("④ 归位精度")
    case_restore()
    print("⑤ 弱对比 / 空白 / 抖动不误判")
    case_weak_contrast_and_noise()
    print("⑥ 反向（向上滚）测位移")
    case_reverse_direction()
    print("⑦ 小粒度重测 + locate 降级 + 辅助接口")
    case_small_step_retry()
    case_locate_unavailable()
    case_helpers()
    print("⑧ 耗时")
    case_timing()
    print()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
