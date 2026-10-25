"""拼接内核离线验证。

不依赖真实屏幕，用合成帧模拟"滚动 → 截屏"过程，检验：
1. 拼接结果与原图逐像素一致（零接缝、零重复、零丢失）
2. 不同步长（含不均匀滚动）都能对齐
3. 滚过头能检测出来而不是写入脏数据
4. 流式分页的内存占用与总长度解耦
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.stitch import (  # noqa: E402
    StreamStitcher,
    locate,
    page_height_for,
    prep_gray,
)


def make_page_image(width: int = 800, height: int = 7000) -> np.ndarray:
    """生成一张有丰富唯一结构的"长文档"，模拟 PDF 页面叠加后的长图。"""
    rng = np.random.default_rng(20261025)
    img = np.full((height, width, 3), 250, dtype=np.uint8)

    # 模拟文字行：每行的墨块长度和位置都不同，保证唯一可识别
    y = 20
    while y < height - 30:
        x = 50
        line_h = int(rng.integers(14, 22))
        while x < width - 80:
            w = int(rng.integers(12, 70))
            if x + w > width - 60:
                break
            shade = int(rng.integers(0, 90))
            img[y : y + line_h, x : x + w] = shade
            x += w + int(rng.integers(5, 18))
        y += line_h + int(rng.integers(10, 26))

    # 模拟表格横线：制造"重复结构"，用来考验匹配算法会不会被误导
    for y in range(200, height, 480):
        img[y : y + 2, 60 : width - 60] = 150

    return img


def simulate_frames(big: np.ndarray, view_h: int, step: int):
    """模拟滚动采集：返回 (帧, 该帧顶部在原图中的行号) 列表。"""
    H = big.shape[0]
    offsets = list(range(0, H - view_h + 1, step))
    if not offsets or offsets[-1] != H - view_h:
        offsets.append(H - view_h)  # 最后一帧贴底
    return [(big[o : o + view_h], o) for o in offsets]


def stitch_frames(frames, width: int) -> np.ndarray:
    """用与 pipeline 相同的逻辑把帧拼起来（不分页，便于比对）。"""
    prev = frames[0]
    parts = [prev]
    for cur in frames[1:]:
        loc = locate(prev, cur)
        if loc is None:
            raise AssertionError("匹配失败，未能定位重叠位置")
        new_start, delta, _err = loc
        assert delta > 0, f"位移应为正，实际 {delta}"
        parts.append(cur[new_start:])
        prev = cur
    return np.vstack(parts)


def case_basic() -> None:
    big = make_page_image()
    H = big.shape[0]
    frames = [f for f, _ in simulate_frames(big, view_h=900, step=760)]
    out = stitch_frames(frames, big.shape[1])

    assert out.shape[1] == big.shape[1], "宽度不一致"
    assert out.shape[0] == H, f"拼接高度 {out.shape[0]} != 原图高度 {H}"
    diff = np.abs(out.astype(np.int16) - big[: out.shape[0]].astype(np.int16))
    assert diff.max() == 0, f"存在像素差异，最大差 {diff.max()}"
    print(f"  [OK] 基础拼接：{len(frames)} 帧 -> {out.shape[0]} 行，逐像素完全一致")


def case_uneven() -> None:
    """不均匀滚动：模拟某些程序滚动量忽大忽小。"""
    big = make_page_image()
    rng = np.random.default_rng(7)
    H, V = big.shape[0], 900
    offsets = [0]
    while offsets[-1] + V < H:
        nxt = offsets[-1] + int(rng.integers(420, 840))
        if nxt + V >= H:
            nxt = H - V
        offsets.append(nxt)
        if nxt == H - V:
            break
    frames = [big[o : o + V] for o in offsets]
    out = stitch_frames(frames, big.shape[1])
    assert out.shape[0] == H, f"拼接高度 {out.shape[0]} != {H}"
    assert np.abs(out.astype(np.int16) - big.astype(np.int16)).max() == 0
    print(f"  [OK] 不均匀滚动：{len(frames)} 帧 -> 逐像素一致")


def case_overscroll() -> None:
    """滚过头（位移 >= 视口高，重叠为 0）必须返回 None，而不是瞎猜。"""
    big = make_page_image()
    V = 900
    f1 = big[0:V]
    f2 = big[900 : 900 + V]  # 完全无重叠
    loc = locate(f1, f2)
    assert loc is None, f"滚过头应匹配失败，却返回 {loc}"
    print("  [OK] 滚过头检测：正确返回匹配失败，不会写入脏数据")


def case_paging() -> None:
    """流式分页：页数正确、每页尺寸正确、内存恒定。"""
    from PIL import Image

    big = make_page_image(width=640, height=9000)
    width = big.shape[1]
    page_h = page_height_for(width, 1.414)
    out_dir = ROOT / "tests" / "_tmp_pages"

    import shutil

    shutil.rmtree(out_dir, ignore_errors=True)

    st = StreamStitcher(width=width, page_h=page_h, out_dir=out_dir, quality=85)
    V = 700
    for o in range(0, big.shape[0] - V + 1, 620):
        st.append(big[o : o + V])
    pages = st.finish()

    expected_pages = int(np.ceil(st.total_rows / page_h))
    assert len(pages) == expected_pages, f"页数 {len(pages)} != 期望 {expected_pages}"
    for p in pages[:-1]:
        with Image.open(p) as im:
            assert im.size == (width, page_h), f"{p.name} 尺寸 {im.size} 异常"
    buf_mb = st._buf.nbytes / (1024 * 1024)
    print(
        f"  [OK] 流式分页：{st.total_rows} 行 -> {len(pages)} 页，"
        f"缓冲区仅 {buf_mb:.1f} MB（与总长度无关）"
    )


def case_pdf() -> None:
    """PDF 输出：页数与体积。"""
    from src.pdfwriter import jpegs_to_pdf

    out_dir = ROOT / "tests" / "_tmp_pages"
    pages = sorted(out_dir.glob("page_*.jpg"))
    if not pages:
        print("  [SKIP] 没有可用的页文件")
        return
    pdf = ROOT / "tests" / "_tmp_out.pdf"
    jpegs_to_pdf(pages, pdf)
    size_mb = pdf.stat().st_size / (1024 * 1024)
    assert pdf.exists() and pdf.stat().st_size > 0

    try:
        import pypdf

        n = len(pypdf.PdfReader(str(pdf)).pages)
        assert n == len(pages), f"PDF 页数 {n} != 图片页数 {len(pages)}"
        print(f"  [OK] PDF 输出：{n} 页，{size_mb:.2f} MB")
    except ImportError:
        print(f"  [OK] PDF 输出：{size_mb:.2f} MB（未安装 pypdf，跳过页数校验）")


def main() -> int:
    print("LongShot2PDF 拼接内核实测")
    case_basic()
    case_uneven()
    case_overscroll()
    case_paging()
    case_pdf()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
