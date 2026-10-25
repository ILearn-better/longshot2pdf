"""连续截图整合的几何与滤镜离线验证（不碰屏幕、不写文件）。

检验：
1. 三种尺寸模式下的目标尺寸（纯算术）与真实缩放结果完全一致
2. 尺寸对齐后同一模式下所有图宽度（或高度）严格相等
3. 位置对齐的左/中/右偏移等于「内容宽 - 图宽」的比例位置
4. 每图一页 / 连续排列两种排布的页数与页尺寸
5. estimate_pages 与 render_pages 的页数**永远相等**（界面显示的页数可信）
6. estimate_pages 不解码像素（大图估算也在毫秒级，滑杆才拖得动）
7. 扫描效果的形状/类型/确定性/强度单调性
8. 越界参数被 normalized 收口，超限页面给出明确报错
9. 输出清晰度：嵌入像素只多不少，且绝不放大超过原图

直接运行：  python tests/test_shots.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.shots import (  # noqa: E402
    ALIGN_MODES,
    MAX_SIDE_PX,
    Layout,
    Shot,
    apply_scan,
    embed_scale,
    estimate_dpi,
    estimate_pages,
    estimate_size,
    prepare,
    render_pages,
    resize_to,
    resize_to_width,
    target_size,
)


def shot(width: int, height: int, value: int = 200, label: str = "") -> Shot:
    """造一张纯色截图（内容不重要，这里只验几何）。"""
    return Shot(image=np.full((height, width, 3), value, dtype=np.uint8), label=label)


def sample_shots() -> list[Shot]:
    """刻意做成长宽各异：1600×900、1000×1000、1000×400。"""
    return [shot(1600, 900, 220), shot(1000, 1000, 180), shot(1000, 400, 240)]


def case_target_size() -> None:
    print("\n[1] 尺寸对齐：算术尺寸 == 真实缩放尺寸")
    lay = Layout(size_mode="width", target_px=1000)
    assert target_size(1600, 800, lay) == (1000, 500)
    assert target_size(1000, 800, lay) == (1000, 800)

    lay = Layout(size_mode="height", target_px=400)
    assert target_size(1600, 800, lay) == (800, 400)

    lay = Layout(size_mode="original", target_px=400)
    assert target_size(1600, 800, lay) == (1600, 800)

    # 真实缩放必须与算术结果逐像素一致，否则「预计页数」会与实际排版错位
    for mode, target in (("width", 1000), ("height", 400), ("original", 999)):
        lay = Layout(size_mode=mode, target_px=target)
        for w, h in ((1600, 900), (1000, 1000), (777, 1500)):
            got = resize_to(np.zeros((h, w, 3), np.uint8), lay)
            assert (got.shape[1], got.shape[0]) == target_size(w, h, lay), (
                f"{mode} {w}x{h}: 实际 {got.shape[1]}x{got.shape[0]} "
                f"!= 算术 {target_size(w, h, lay)}"
            )
    print("      width/height/original 三模式一致")

    # 极长图必须被 MAX_SIDE_PX 收住，否则拼出来的页任何阅读器都打不开
    lay = Layout(size_mode="width", target_px=8000)
    w, h = target_size(100, 10000, lay)
    assert max(w, h) == MAX_SIDE_PX, f"未收口: {w}x{h}"
    print(f"      极长图收口到 {w}×{h}")


def case_uniform_after_resize() -> None:
    print("\n[2] 尺寸对齐后长宽一致")
    shots = sample_shots()
    for mode, target, attr in (("width", 1200, "width"), ("height", 700, "height")):
        lay = Layout(size_mode=mode, target_px=target, hi_res=1.0)
        placed = prepare(shots, lay)
        values = {getattr(p, attr) for p in placed}
        assert values == {target}, f"{mode} 模式未统一: {sorted(values)}"
        print(f"      {mode} 模式：{attr} 全部 = {target}px"
              f"，高度/宽度依次为 {[p.height if attr=='width' else p.width for p in placed]}")

    # 保持原始：尺寸一张都不许动
    placed = prepare(shots, Layout(size_mode="original", hi_res=1.0))
    assert [(p.width, p.height) for p in placed] == [(1600, 900), (1000, 1000), (1000, 400)]
    print("      original 模式：尺寸原样保留")


def case_align() -> None:
    print("\n[3] 位置对齐：左 / 中 / 右")
    shots = sample_shots()
    # 用 original 模式才能看出对齐差异（统一宽度后所有图一样宽，偏移恒为 0）
    base = Layout(size_mode="original", align="left", hi_res=1.0)
    left = prepare(shots, base)
    middle = prepare(shots, Layout(size_mode="original", align="center", hi_res=1.0))
    right = prepare(shots, Layout(size_mode="original", align="right", hi_res=1.0))

    content_w = max(s.width for s in shots)  # 1600
    assert [p.x for p in left] == [0, 0, 0], left
    assert [p.x for p in middle] == [0, 300, 300], [p.x for p in middle]
    assert [p.x for p in right] == [0, 600, 600], [p.x for p in right]
    for mode in ALIGN_MODES:
        placed = prepare(shots, Layout(size_mode="original", align=mode, hi_res=1.0))
        for p in placed:
            assert 0 <= p.x <= content_w - p.width, f"{mode}: x={p.x} 越界"
    print(f"      内容宽 {content_w}，左 {[p.x for p in left]} "
          f"中 {[p.x for p in middle]} 右 {[p.x for p in right]}")


def case_paging() -> None:
    print("\n[4] 排布：每图一页 / 连续排列")
    shots = sample_shots()
    lay = Layout(size_mode="width", target_px=1000, margin_px=20, hi_res=1.0)
    pages = render_pages(shots, lay)
    assert len(pages) == len(shots), f"每图一页应为 {len(shots)} 页，得到 {len(pages)}"
    widths = {p.shape[1] for p in pages}
    assert len(widths) == 1, f"页宽不一致: {widths}"
    page_w = widths.pop()
    assert page_w == 1000 + 2 * 20
    for page, s in zip(pages, prepare(shots, lay)):
        assert page.shape[0] == s.height + 2 * 20, (page.shape, s.height)
    print(f"      each：{len(pages)} 页，页宽 {page_w}，页高 "
          f"{[p.shape[0] for p in pages]}")

    lay = Layout(size_mode="width", target_px=1000, margin_px=20, gap_px=10,
                 paging="flow", page_ratio=1.414, hi_res=1.0)
    flow = render_pages(shots, lay)
    assert len(flow) >= 1
    sizes = {(p.shape[1], p.shape[0]) for p in flow}
    assert len(sizes) == 1, f"flow 各页尺寸必须一致: {sizes}"
    fw, fh = sizes.pop()
    assert fh == round(fw * 1.414), (fh, fw * 1.414)
    print(f"      flow：{len(flow)} 页，每页 {fw}×{fh}")


def case_estimate_matches_render() -> None:
    print("\n[5] 预计页数 == 实际页数")
    shots = sample_shots()
    compared = 0
    for mode in ("width", "height", "original"):
        for paging in ("each", "flow"):
            for target in (600, 1200):
                lay = Layout(size_mode=mode, target_px=target, paging=paging,
                             margin_px=18, gap_px=12)
                est = estimate_pages(shots, lay)
                real = len(render_pages(shots, lay))
                assert est == real, f"{mode}/{paging}/{target}: 预计 {est} 实际 {real}"
                compared += 1
    print(f"      {compared} 组参数全部一致")

    # 边界：空列表不许崩，也不许说「1 页」
    assert estimate_pages([], Layout()) == 0
    assert render_pages([], Layout()) == []
    assert estimate_size([], Layout()) is None
    print("      空列表 → 0 页")


def case_estimate_is_cheap() -> None:
    print("\n[6] 预计页数不解码像素")
    # 20 张 4000×3000 的图，真缩放要几百毫秒以上；纯算术必须是毫秒级
    shots = [shot(4000, 3000) for _ in range(20)]
    lay = Layout(size_mode="width", target_px=1600, paging="flow")
    t0 = time.perf_counter()
    for _ in range(20):
        estimate_pages(shots, lay)
    elapsed = (time.perf_counter() - t0) / 20
    assert elapsed < 0.05, f"单次估算 {elapsed*1000:.1f}ms，界面滑杆会卡"
    print(f"      20 张 4K 图估算一次 {elapsed*1000:.2f}ms")

    size = estimate_size(shots, lay)
    assert size == (1600, 1200), size
    assert prepare(shots, lay)[0].logical_width == size[0]
    print(f"      统一后尺寸 {size} 与实际渲染一致")


def case_scan() -> None:
    print("\n[7] 扫描版效果")
    # 造一张有内容的图：左侧深色块，其余浅色（纯色图看不出对比度变化）
    img = np.full((600, 800, 3), 250, dtype=np.uint8)
    img[:, :400] = 30
    img[100:200, 500:700] = (200, 60, 60)

    out = apply_scan(img, 60)
    assert out.shape == img.shape and out.dtype == np.uint8
    assert out.ndim == 3 and out.shape[2] == 3
    # 纸色偏暖：R 略高于 B（固定比例，肉眼看着像纸而不是屏幕）
    assert np.all(out[..., 0] >= out[..., 2]), "纸色应为暖色（R>=B）"
    # 逐像素算差值：拿全图 max(R) 减全图 min(B) 比的是两个不同像素，没有意义
    warm = int((out[..., 0].astype(np.int16) - out[..., 2]).max())
    assert warm <= 8, f"暖色偏移过大: {warm}"
    print(f"      形状 {out.shape}，逐像素暖色偏移 ≤ {warm}")

    # 确定性：预览和导出必须逐像素一致，否则同一个强度每次结果都不同
    assert np.array_equal(out, apply_scan(img, 60)), "同一强度两次结果不同"
    print("      同强度两次调用逐像素一致")

    # 强度越大越「脏」：抬黑 + 噪点让均值上升，灰度层次变窄
    means = [float(apply_scan(img, s).mean()) for s in (0, 50, 100)]
    assert means[0] <= means[1] <= means[2], f"强度不单调: {means}"
    spreads = [float(apply_scan(img, s).std()) for s in (0, 50, 100)]
    assert spreads[0] >= spreads[2], f"强噪点应压缩灰度层次: {spreads}"
    print(f"      均值 {[round(m,1) for m in means]} 标准差 {[round(s,1) for s in spreads]}")

    # 关掉扫描时排版结果必须与原图逐像素相同
    shots = [shot(300, 200, 123)]
    on = prepare(shots, Layout(size_mode="original", scan=True, scan_strength=50))
    off = prepare(shots, Layout(size_mode="original", scan=False))
    assert not np.array_equal(on[0].image, off[0].image)
    assert np.array_equal(off[0].image, shots[0].image)
    print("      开关生效，关闭时零改动")


def case_bad_params() -> None:
    print("\n[8] 越界参数与超限页")
    lay = Layout(
        size_mode="diagonal", target_px=99999, align="middle",
        paging="grid", margin_px=-50, gap_px=99999, page_ratio=99,
        scan_strength=800,
    ).normalized()
    assert lay.size_mode == "width", lay.size_mode
    assert lay.align == "center", lay.align
    assert lay.paging == "each", lay.paging
    assert 0 <= lay.margin_px <= 400
    assert 0 <= lay.gap_px <= 400
    assert 0.3 <= lay.page_ratio <= 5.0
    assert lay.scan_strength == 100
    assert 200 <= lay.target_px <= 8000
    print(f"      normalized → {lay.size_mode}/{lay.align}/{lay.paging} "
          f"margin={lay.margin_px} gap={lay.gap_px} ratio={lay.page_ratio:.2f} "
          f"strength={lay.scan_strength}")

    # 单页宽度超限要明确报错，而不是让用户拿到一个打不开的 PDF
    huge = [Shot(image=np.zeros((10, MAX_SIDE_PX + 100, 3), np.uint8))]
    try:
        render_pages(huge, Layout(size_mode="original", margin_px=10))
    except ValueError as exc:
        print(f"      超限报错：{exc}")
    else:
        raise AssertionError("超宽页未报错")


def case_resolution() -> None:
    """清晰度：嵌入像素只多不少，但**绝不凭空放大**。"""
    print("\n[9] 输出清晰度：只少缩、不虚放")
    lay = Layout(size_mode="width", target_px=1200)

    # 1080p 截图排版到 1200：1× 会缩到 1200，2× 用满 1920 原生像素
    hd = [shot(1920, 1080)]
    got = {}
    for hi in (1.0, 2.0, 3.0):
        p = prepare(hd, Layout(size_mode="width", target_px=1200, hi_res=hi))[0]
        got[hi] = p.width
        assert p.logical_width == 1200, f"hi={hi} 排版宽度被改了: {p.logical_width}"
    assert got == {1.0: 1200, 2.0: 1920, 3.0: 1920}, got
    print(f"      1080p 源图 → 嵌入宽度 {got}（2×/3× 都停在原生 1920）")

    # 4K 源图：被 hi_res 挡住，不会白白塞进 3840
    uhd = [shot(3840, 2160)]
    for hi, expect in ((1.0, 1200), (2.0, 2400), (3.0, 3600)):
        p = prepare(uhd, Layout(size_mode="width", target_px=1200, hi_res=hi))[0]
        assert p.width == expect, (hi, p.width, expect)
    print("      4K 源图 → 1200 / 2400 / 3600（随档位，最高 3×）")

    # 源图比排版尺寸还小：清晰度倍数不会在「排版放大」之上再叠一层
    # （把 600 撑到 1200 是用户选的「统一宽度」，不是清晰度擅自决定的）
    small = [shot(600, 400)]
    for hi in (1.0, 2.0, 3.0):
        p = prepare(small, Layout(size_mode="width", target_px=1200, hi_res=hi))[0]
        assert p.width == 1200, (hi, p.width)
    assert embed_scale(small[0], Layout(size_mode="width", target_px=1200)) == 1.0
    keep = prepare(small, Layout(size_mode="original", hi_res=3.0))[0]
    assert (keep.width, keep.height) == (600, 400), (keep.width, keep.height)
    print("      600px 源图：统一宽度撑到 1200 后不再叠加放大；original 模式 600 不动")

    # 版式与页数不随清晰度变化 —— 这是「提高分辨率」而不是「把图放大」
    shots = sample_shots()
    base = None
    for hi in (1.0, 2.0, 3.0):
        lay = Layout(size_mode="width", target_px=1000, hi_res=hi)
        shape = [
            (p.logical_width, p.logical_height) for p in prepare(shots, lay)
        ]
        pages = len(render_pages(shots, lay))
        if base is None:
            base = (shape, pages)
        assert (shape, pages) == base, f"hi={hi} 改动了版式/页数: {(shape, pages)}"
    print(f"      1×/2×/3× 的排版尺寸与页数完全相同（{base[1]} 页）")

    # PDF 页面的**物理**比例不变：像素多了，纸上的大小一样
    for hi in (1.0, 2.0):
        page = render_pages(shots, Layout(size_mode="width", target_px=1000, hi_res=hi))[0]
        ratio = page.shape[1] / page.shape[0]
        if hi == 1.0:
            first = ratio
        else:
            assert abs(ratio - first) < 0.01, f"页比例变了: {ratio} vs {first}"
    print("      PDF 页面的宽高比不随清晰度变化")

    # 等效分辨率提示：取最差的那张
    # 最窄的那张（1000px）排版到 500 时能到 2×，所以整批按 192dpi 报
    dpi = estimate_dpi(shots, Layout(size_mode="width", target_px=500))
    assert abs(dpi - 192.0) < 1.0, dpi
    print(f"      estimate_dpi = {dpi:.0f} dpi（取最差一张）")

    # 「输出宽度」：等比缩放，宽度已一致时零开销
    img = np.zeros((400, 800, 3), np.uint8)
    assert resize_to_width(img, 800) is img
    assert resize_to_width(img, 1600).shape == (800, 1600, 3)
    assert resize_to_width(img, 400).shape == (200, 400, 3)
    print("      resize_to_width：800→1600/400 等比，等宽时零拷贝")


def main() -> int:
    print("=" * 64)
    print("连续截图整合 · 几何与滤镜")
    print("=" * 64)
    try:
        case_target_size()
        case_uniform_after_resize()
        case_align()
        case_paging()
        case_estimate_matches_render()
        case_estimate_is_cheap()
        case_scan()
        case_bad_params()
        case_resolution()
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
