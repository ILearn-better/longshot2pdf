"""拼接内核离线验证。

不依赖真实屏幕，用合成帧模拟「滚动 → 截屏」过程，检验：

1. 拼接结果与原图**逐像素差 = 0**（不是「接近」）——零接缝、零重复、零丢失
2. 不同步长（含抖动）都能对齐
3. 周期结构 / 大片空白这两种最容易骗过匹配算法的样本
4. 滚过头能检测出来，而不是写入错位的脏数据
5. 到底（画面静止）能识别成 delta == 0
6. 流式分页页数/尺寸正确，且缓冲区内存与总长度解耦
7. PDF 页数与页 JPEG 数量一致
8. locate() 单帧耗时
9. abort 能抢救半成品

直接运行：  python tests/test_stitch.py
"""

from __future__ import annotations

import io
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.stitch import (  # noqa: E402
    REFINE_MAX_ROWS,
    StreamStitcher,
    locate,
    match_offset,
    page_height_for,
    prep_gray,
)
from tests.mock_frames import (  # noqa: E402
    ScrollTrace,
    VirtualScreen,
    frame_pair,
    make_document,
    make_plain_document,
    scroll_trace,
)

TMP_PAGES = ROOT / "tests" / "_tmp_pages"
TMP_PDF = ROOT / "tests" / "_tmp_out.pdf"


# ---------------------------------------------------------------- 工具


def stitch_trace(trace: ScrollTrace) -> tuple[np.ndarray, list[int], list[float]]:
    """用与 pipeline 相同的逻辑把一帧序列拼成整图（不分页）。

    同时返回每步实测位移与残差，供各用例核对「位移完全正确」。
    """
    frames = trace.frames
    parts = [frames[0]]
    deltas: list[int] = []
    errs: list[float] = []
    prev = frames[0]
    for i, cur in enumerate(frames[1:], 1):
        loc = locate(prev, cur)
        if loc is None:
            raise AssertionError(f"第 {i} 帧匹配失败，未能定位重叠位置")
        new_start, delta, err = loc
        if new_start + delta != cur.shape[0]:
            raise AssertionError(
                f"第 {i} 帧违反契约 new_start({new_start}) + delta({delta}) != h({cur.shape[0]})"
            )
        deltas.append(delta)
        errs.append(err)
        parts.append(cur[new_start:])
        prev = cur
    return np.vstack(parts), deltas, errs


def assert_pixel_perfect(name: str, trace: ScrollTrace) -> None:
    """整条链路唯一可接受的判据：拼接结果与 ground truth 逐像素相等。"""
    out, deltas, errs = stitch_trace(trace)
    gt = ground_truth(trace)
    if out.shape[0] != gt.shape[0]:
        raise AssertionError(f"{name}: 拼接高度 {out.shape[0]} != 原图高度 {gt.shape[0]}")
    if deltas != trace.deltas:
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(deltas, trace.deltas)) if a != b]
        raise AssertionError(f"{name}: 位移应完全正确，但有 {len(bad)} 处不符 {bad[:5]}")
    if not np.array_equal(out, gt):
        diff = np.abs(out.astype(np.int16) - gt.astype(np.int16))
        raise AssertionError(
            f"{name}: 存在像素差异，最大差 {diff.max()}，不同像素 {int((diff != 0).sum())}"
        )
    print(
        f"  [OK] {name}：{len(trace.frames)} 帧 · 位移 {min(trace.deltas)}~{max(trace.deltas)}px "
        f"全部正确 · {out.shape[0]} 行逐像素差 = 0 · 残差中位 {np.median(errs):.2f}"
    )


def ground_truth(trace: ScrollTrace) -> np.ndarray:
    """从帧序列按真实 offset 写回一张长图，作为逐像素判据的真值。

    注意不能拿「拼接结果」自己当参照物，否则零接缝只是同义反复。
    这里用 ScrollTrace 记录的 offsets（mock 生成时已知的真实滚动位置）重建。
    """
    H = trace.doc_h
    w = trace.frames[0].shape[1]
    gt = np.zeros((H, w, 3), dtype=np.uint8)
    filled = np.zeros(H, dtype=bool)
    for frame, off in zip(trace.frames, trace.offsets):
        gt[off : off + frame.shape[0]] = frame
        filled[off : off + frame.shape[0]] = True
    if not filled.all():
        raise AssertionError(
            f"ground truth 有 {int((~filled).sum())} 行未被任何帧覆盖，用例本身不成立"
        )
    return gt


# ---------------------------------------------------------------- 用例


def case_error_metric() -> None:
    """附加（评审意见 #1）：返回的 err 必须是全量精修级残差，不是抽样值。

    抽样级和全量级的抽样集合不同，数值不可比。如果实现里把抽样值当基线
    去和全量值比大小，返回给编排层的 err 就会是偏乐观的抽样值。
    这里用一段能整段复算的重叠区，独立算出「整段逐像素 MAD」并逐位比对。
    """
    doc = make_document(1000, 6000, seed=61)
    view_h, delta = 900, 300
    prev, cur = frame_pair(doc, view_h, delta)
    # 让残差非零：把当前帧过一遍 JPEG q=85（等价于真实截图链路里的压缩抖动），
    # 这样「全量 MAD」和「160 行抽样 MAD」才会是不同的实数，断言才有鉴别力。
    buf = io.BytesIO()
    Image.fromarray(cur).save(buf, format="JPEG", quality=85, subsampling=0)
    buf.seek(0)
    cur = np.asarray(Image.open(buf).convert("RGB"))

    loc = locate(prev, cur)
    assert loc is not None, "应能定位"
    new_start, got_delta, err = loc
    assert got_delta == delta, f"位移应为 {delta}，实际 {got_delta}"

    overlap = view_h - delta
    assert overlap <= REFINE_MAX_ROWS, "本用例要求重叠区不触发精修截断，便于整段复算"

    def full_mad(trim: int) -> float:
        """整段重叠区（除去末尾 trim 行）的逐像素 MAD。"""
        n = overlap - trim
        return float(
            np.abs(prev[delta : delta + n].astype(np.int16) - cur[:n].astype(np.int16)).mean()
        )

    # err 必须落在「整段重叠区 MAD」这一族里（±1 邻位参与比较时会少 1~2 行）
    cands = [full_mad(t) for t in (0, 1, 2)]
    hit = [v for v in cands if abs(err - v) < 1e-9]
    assert hit, f"err={err!r} 不是任何整段重叠区 MAD（候选 {cands}）——疑似返回了抽样值"

    # 抽样级残差必然与之不同，证明确实没拿抽样值充数
    rows = np.unique(np.linspace(0, overlap - 1, 160).astype(np.intp))
    sampled = float(
        np.abs(prev[delta + rows].astype(np.int16) - cur[rows].astype(np.int16)).mean()
    )
    assert abs(sampled - err) > 1e-3, (
        f"抽样值与全量值过于接近（{sampled:.6f} vs {err:.6f}），本用例失去鉴别力"
    )
    print(
        f"  [OK] 误差度量：返回 err={err:.6f} ≡ 整段 {overlap} 行逐像素 MAD "
        f"（160 行抽样值 {sampled:.6f} 未被采用）"
    )


def case_idle_noise() -> None:
    """附加（评审意见 #2）：带像素抖动时「到底」仍要判对，且不能吞掉真实滚动。

    抖动来自 VirtualScreen(noise=...)，它是按帧重新播种的随机噪声，
    等价于真实屏幕的异步重绘抖动。像素域 MAD 在这种噪声下的噪声底会盖过
    「真的滚了 1~2px」的信号，所以判据放在签名域。
    """
    doc = make_document(1000, 6000, seed=31)
    region = (0, 0, 1000, 900)
    for noise in (0, 1, 2, 3, 5):
        vs = VirtualScreen(doc, 900, noise=noise)
        for _ in range(3):
            a = vs.grab(region)
            vs.wheel_calls += 1  # 换一帧噪声种子，模拟两帧之间重新渲染
            b = vs.grab(region)
            loc = locate(a, b)
            assert loc is not None, f"noise={noise} 静止画面不应返回 None"
            assert loc[1] == 0, f"noise={noise} 静止画面应判 delta=0，实际 {loc}"
            assert loc[0] == 900, f"noise={noise} new_start 应为 h，实际 {loc[0]}"

        # 同样的噪声下，真实 1~3px 滚动不能被当成「没动」
        for d in (1, 2, 3):
            vs.offset = 0
            a = vs.grab(region)
            vs.offset = d
            b = vs.grab(region)
            loc = locate(a, b)
            assert loc is not None and loc[1] == d, f"noise={noise} 滚动 {d}px 定位错误：{loc}"
    print(
        "  [OK] 噪声下到底判定：noise=0/1/2/3/5 静止帧均判 delta=0，"
        "同噪声下 1/2/3px 真实滚动仍全部识别"
    )


def case_thin_overlap() -> None:
    """附加（评审意见 #3）：重叠薄到 5% 视口以下时一律拒判，绝不猜。

    min_overlap 默认 max(32, h//20)。视口 800 -> 40 行（5%）。
    重叠 >= 40 行必须正确；< 40 行（即一帧滚了 >95% 视口）必须返回 None，
    把「不可靠」显式交回编排层缩步长，而不是给一个看起来成功的位置。
    """
    view_h = 800
    docs = [("普通", make_document(1000, 10000, seed=41)), ("周期", make_plain_document(800, 8000))]

    for name, doc in docs:
        for ov in (46, 45, 44, 40):
            d = view_h - ov
            loc = locate(doc[0:view_h], doc[d : d + view_h])
            assert loc is not None and loc[1] == d, f"{name} 重叠 {ov} 行应能定位，实际 {loc}"
        for ov in (35, 30, 20):
            d = view_h - ov
            hits = 0
            total = 0
            for start in range(0, doc.shape[0] - (view_h + d) - 1, 211):
                loc = locate(doc[start : start + view_h], doc[start + d : start + d + view_h])
                total += 1
                if loc is not None and loc[1] != d:
                    raise AssertionError(f"{name} 重叠 {ov} 行给出了错误位移 {loc}（应拒判）")
                hits += loc is not None and loc[1] == d
            assert hits == 0, f"{name} 重叠 {ov} 行不应判成功"
    print(
        "  [OK] 薄重叠边界：重叠 40~46 行 100% 正确；"
        "重叠 20/30/35 行（>95% 视口位移）全部返回 None，零错误位移"
    )


def case_basic() -> None:
    """1. 基础拼接：等步长滚动，逐像素必须完全相同。"""
    doc = make_document(1000, 14000)
    tr = scroll_trace(doc, 900, 760)
    assert_pixel_perfect("基础拼接 1000×900 / 步长 760", tr)


def case_uneven() -> None:
    """2. 不均匀滚动：每步 420~840 随机，模拟平滑滚动/滚轮解释不一致。"""
    doc = make_document(1000, 14000, seed=99)
    tr = scroll_trace(doc, 900, 630, jitter=210, seed=7)
    steps = tr.deltas
    print(
        f"       实测步长范围 {min(steps)}~{max(steps)}px（含 {len(set(steps))} 种不同位移）"
    )
    assert_pixel_perfect("不均匀滚动 1000×900 / 步长 420~840", tr)


def case_periodic() -> None:
    """3. 周期结构：等距横线 + 等宽文本，最容易让匹配「看哪儿都像」。"""
    doc = make_plain_document(800, 9000)
    tr = scroll_trace(doc, 900, 630)
    assert_pixel_perfect("周期结构 800×9000", tr)


def case_blank_margin() -> None:
    """4. 大片空白边距：首尾各 400px 纯白，考验空白区不给出错误位移。"""
    doc = make_document(1000, 14000, blank_margins=400, seed=5)
    tr = scroll_trace(doc, 900, 700)
    assert_pixel_perfect("空白边距 400px 1000×900", tr)


def case_overscroll() -> None:
    """5. 滚过头：位移 >= 视口高，重叠为 0，必须返回 None。"""
    doc = make_document(1000, 14000)
    view_h = 900
    for delta in (view_h, view_h + 1, view_h * 2):
        prev = doc[0:view_h]
        cur = doc[delta : delta + view_h]
        if cur.shape[0] < view_h:
            continue
        loc = locate(prev, cur)
        assert loc is None, f"位移 {delta}px 无重叠，应返回 None，实际 {loc}"
    print(f"  [OK] 滚过头检测：位移 ≥ {view_h}px（无重叠）均正确返回 None")


def case_bottom() -> None:
    """6. 到底判定：两帧完全相同 → delta == 0，new_start == h。"""
    doc = make_document(1000, 3000)
    view_h = 900
    prev = doc[0:view_h].copy()

    # 6a 完全相同的两帧
    loc = locate(prev, prev)
    assert loc is not None, "静止画面应返回结果而不是 None"
    new_start, delta, err = loc
    assert delta == 0, f"静止画面 delta 应为 0，实际 {delta}"
    assert new_start == view_h, f"静止画面 new_start 应为 h={view_h}，实际 {new_start}"
    assert err <= 0.8, f"静止画面残差应接近 0，实际 {err}"
    print(f"  [OK] 到底判定：相同帧 -> new_start={new_start}, delta={delta}, err={err:.3f}")

    # 6b 贴底后程序再滚一次，画面确实没变（同一张图对象也行，但这里用独立拷贝）
    tail = doc[doc.shape[0] - view_h :].copy()
    again = tail.copy()
    loc2 = locate(tail, again)
    assert loc2 is not None and loc2[1] == 0, f"贴底后再滚应判 delta=0，实际 {loc2}"
    print(f"  [OK] 到底判定：贴底重复帧 -> delta={loc2[1]}, err={loc2[2]:.3f}")


def case_paging() -> tuple[list[Path], int]:
    """7. 流式分页：页数、尺寸正确，缓冲区内存与总长度解耦。"""
    from PIL import Image

    width = 640
    page_h = page_height_for(width, 1.414)
    rows = 5000
    view_h = 700
    doc = make_document(width, rows, seed=3)

    if TMP_PAGES.exists():  # 沙箱对不存在的路径 rmtree 会打 stderr 噪音
        shutil.rmtree(TMP_PAGES, ignore_errors=True)
    st = StreamStitcher(width=width, page_h=page_h, out_dir=TMP_PAGES, quality=85)

    peak_buf = 0
    appended = 0
    for o in range(0, rows - view_h + 1, 620):
        seg = doc[o : o + view_h]
        st.append(seg)
        appended += seg.shape[0]
        peak_buf = max(peak_buf, st.buffer_bytes)

    # 用 short run 验证「缓冲区大小与总长度无关」
    short_dir = TMP_PAGES / "_short"
    short_dir.mkdir(parents=True, exist_ok=True)
    st_short = StreamStitcher(width=width, page_h=page_h, out_dir=short_dir, quality=85)
    for o in range(0, 1200 - view_h + 1, 620):
        st_short.append(doc[o : o + view_h])
    st_short.finish()

    pages = st.finish()
    expected = int(np.ceil(st.total_rows / page_h))
    assert len(pages) == expected, f"页数 {len(pages)} != 期望 {expected}"
    assert st.total_rows == appended, f"total_rows {st.total_rows} != 累计追加 {appended}"

    for p in pages[:-1]:
        with Image.open(p) as im:
            assert im.size == (width, page_h), f"{p.name} 尺寸 {im.size} != {(width, page_h)}"
    with Image.open(pages[-1]) as im:
        last_h = im.size[1]
        assert im.size[0] == width, f"末页宽度 {im.size[0]} != {width}"
    assert last_h == st.total_rows - page_h * (len(pages) - 1), "末页高度应等于残留行数"
    assert st.pending_rows == 0 and st.page_count == len(pages)

    assert st.buffer_bytes == st_short.buffer_bytes, (
        f"缓冲区大小应恒定：长图 {st.buffer_bytes} B vs 短图 {st_short.buffer_bytes} B"
    )
    peak_mb = peak_buf / (1024 * 1024)
    assert peak_mb < 50, f"峰值缓冲 {peak_mb:.1f} MB 超出 50 MB 上限"

    print(
        f"  [OK] 流式分页：{st.total_rows} 行 -> {len(pages)} 页（末页 {last_h} 行）"
        f"，除末页外均 {width}×{page_h}"
    )
    print(
        f"       峰值缓冲 {peak_mb:.2f} MB（1200 行短图同样是 "
        f"{st_short.buffer_bytes / (1024 * 1024):.2f} MB，与总长度无关）"
    )
    # 清掉 short run，避免影响 PDF 用例的页文件扫描
    if short_dir.exists():
        shutil.rmtree(short_dir, ignore_errors=True)
    return pages, page_h


def case_pdf(pages: list[Path], page_h: int) -> None:
    """8. PDF 输出：页数与 pypdf 读到的页数一致，且不重新编码。"""
    from src.pdfwriter import jpegs_to_pdf

    jpegs_to_pdf(pages, TMP_PDF)
    assert TMP_PDF.exists() and TMP_PDF.stat().st_size > 0, "PDF 未生成"
    size_mb = TMP_PDF.stat().st_size / (1024 * 1024)
    jpeg_mb = sum(p.stat().st_size for p in pages) / (1024 * 1024)

    import pypdf

    reader = pypdf.PdfReader(str(TMP_PDF))
    n = len(reader.pages)
    assert n == len(pages), f"PDF 页数 {n} != 页 JPEG 数量 {len(pages)}"

    box = reader.pages[0].mediabox
    print(
        f"  [OK] PDF 输出：{n} 页 · {size_mb:.2f} MB "
        f"（源 JPEG 合计 {jpeg_mb:.2f} MB，体积比 {size_mb / max(jpeg_mb, 1e-9):.2f} ≈ 1 说明未重编码）"
        f" · 首屏 {float(box.width):.0f}×{float(box.height):.0f} pt"
    )
    expected_h = 595.0 * 1.414
    assert abs(float(box.height) - expected_h) < 1.5, f"页高 {box.height} pt 不符 A4 比例"


def case_perf() -> None:
    """9. 性能：1000×1400 帧的 locate() 平均耗时。"""
    doc = make_document(1000, 20000, seed=11)
    view_h = 1400
    deltas = [60, 200, 400, 700, 1000, 1190, 1300]
    times: list[float] = []
    for d in deltas:
        prev = doc[0:view_h]
        cur = doc[d : d + view_h]
        for _ in range(3):  # 预热，避开首次调用的 import/缓存开销
            locate(prev, cur)
        for _ in range(4):
            t0 = time.perf_counter()
            loc = locate(prev, cur)
            times.append((time.perf_counter() - t0) * 1000.0)
            assert loc is not None and loc[1] == d, f"d={d} 定位错误：{loc}"

    avg = float(np.mean(times))
    p_max = float(np.max(times))
    print(
        f"  [OK] 性能：1000×1400 帧 locate() 平均 {avg:.1f} ms"
        f"（最慢 {p_max:.1f} ms，位移范围 {min(deltas)}~{max(deltas)}px，共 {len(times)} 次）"
    )
    assert avg < 60.0, f"平均耗时 {avg:.1f} ms 超出 60 ms 目标"


def case_abort() -> None:
    """10. abort 语义：中途中断能拿到已生成页 + 残留页，且不重复刷页。"""
    width = 480
    page_h = page_height_for(width, 1.414)
    out_dir = TMP_PAGES / "_abort"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    st = StreamStitcher(width=width, page_h=page_h, out_dir=out_dir, quality=85)
    rows = page_h * 2 + 300
    doc = make_document(width, rows, seed=17)
    st.append(doc[:rows])
    assert st.page_count == 2, f"应已落盘 2 页，实际 {st.page_count}"
    assert st.pending_rows == 300, f"残留应为 300 行，实际 {st.pending_rows}"

    early = st.pages
    pages = st.abort()
    assert len(pages) == 3, f"abort 应返回 3 页（2 满页 + 1 残留页），实际 {len(pages)}"
    assert pages[:2] == early, "abort 不应改动已落盘的页"
    assert pages[-1].name == "page_00002.jpg", f"残留页命名异常 {pages[-1].name}"
    assert st.pending_rows == 0

    from PIL import Image

    with Image.open(pages[-1]) as im:
        assert im.size == (width, 300), f"残留页尺寸 {im.size} != {(width, 300)}"

    # abort 之后再 finish 不能重复出页
    again = st.finish()
    assert len(again) == 3, f"abort 后 finish 不应新增页，实际 {len(again)}"

    # 空行段安全返回
    st2 = StreamStitcher(width=width, page_h=page_h, out_dir=out_dir, quality=85)
    empty = np.zeros((0, width, 3), dtype=np.uint8)
    st2.append(empty)
    st2.append(doc[:10])
    assert st2.total_rows == 10 and st2.pending_rows == 10
    st2.abort()

    print(
        f"  [OK] abort 语义：中断时拿到 2 满页 + 1 残页（300 行），"
        f"abort 后 finish 不再重复出页，空行段安全返回"
    )
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)


def case_legacy_api() -> None:
    """附加：旧接口 prep_gray / match_offset / page_height_for 仍在契约内。"""
    doc = make_document(900, 4000, seed=23)
    prev, _ = frame_pair(doc, 800, 800)
    small = prep_gray(prev)
    assert small.ndim == 2 and small.shape[1] == 64, f"prep_gray 输出异常 {small.shape}"
    assert max(1, int(round(1000 * 1.414)) - 1) <= page_height_for(1000) <= 1415

    ps = prep_gray(prev)
    cs = prep_gray(doc[800 : 800 + 800])
    hit = match_offset(ps, cs, k=12)
    print(
        f"  [OK] 旧接口保留：prep_gray -> {small.shape}，"
        f"match_offset 可调用（本对帧位移=视口高、无重叠，返回 {hit}）"
    )
    print(f"  [OK] page_height_for(1000) = {page_height_for(1000)}（A4 1.414）")


def main() -> int:
    print("LongShot2PDF 拼接内核实测（合成帧，逐像素判据）")
    print("-" * 64)
    try:
        case_basic()
        case_uneven()
        case_periodic()
        case_blank_margin()
        case_overscroll()
        case_bottom()
        case_error_metric()
        case_idle_noise()
        case_thin_overlap()
        pages, page_h = case_paging()
        case_pdf(pages, page_h)
        case_perf()
        case_abort()
        case_legacy_api()
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
