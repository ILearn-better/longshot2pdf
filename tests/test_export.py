"""连续截图整合的导出验证：.docx 与 .pdf 都能真的生成、且内容对得上。

不依赖 python-docx / 不依赖真实屏幕：
docx 是 ZIP + XML，直接读包里的 document.xml 用正则数段落、图片、分页符；
PDF 用 pypdf 读回页数与页尺寸（pypdf 只在自测里用，运行时缺了不影响出文件）。

检验：
1. docx 是合法 OPC 包（必需部件齐全），图片数与截图数一致
2. 三张图 → 三个 <w:drawing>，每图一页 → 恰好 N-1 个分页符；连续排列 → 0 个
3. 图片按「同一缩放系数」缩，相对大小与原图一致（不是每张各自撑满版心）
4. 对齐方式写进了段落属性（left/center/right 各不相同）
5. PDF 页数 == 界面显示的「预计页数」，页宽一致、页高按各图比例
6. 格式名归一化（Word/.docx/DOCX 都行），非法格式给出明确报错
7. 空截图列表拒绝导出，而不是生成一个 0 字节文件

直接运行：  python tests/test_export.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.docxwriter import docx_image_count  # noqa: E402
from src.shots import Layout, Shot, estimate_pages, render_pages  # noqa: E402
from src.shots_export import (  # noqa: E402
    default_name,
    export_document,
    normalize_format,
)

REQUIRED_PARTS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/core.xml",
    "docProps/app.xml",
    "word/document.xml",
    "word/_rels/document.xml.rels",
    "word/styles.xml",
]


def shot(width: int, height: int, value: int = 200) -> Shot:
    img = np.full((height, width, 3), value, dtype=np.uint8)
    # 加一点结构，避免整图纯色导致 JPEG 编码后尺寸极端（也能看清压缩没压坏）
    img[: max(1, height // 8)] = 90
    return Shot(image=img)


def sample_shots() -> list[Shot]:
    return [shot(1600, 900, 230), shot(1000, 1000, 180), shot(1000, 400, 210)]


def _doc_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def case_docx_structure(tmp: Path) -> None:
    print("\n[1] docx 包结构与分页")
    shots = sample_shots()
    lay = Layout(size_mode="width", target_px=1200, paging="each", align="center")
    out = export_document(shots, lay, tmp / "each.docx", "docx")

    assert out.path.suffix == ".docx"
    assert out.images == 3, out.images
    assert out.size_mb > 0

    with zipfile.ZipFile(out.path) as zf:
        names = set(zf.namelist())
        missing = [p for p in REQUIRED_PARTS if p not in names]
        assert not missing, f"缺部件: {missing}"
        media = sorted(n for n in names if n.startswith("word/media/"))
        assert len(media) == 3, media
        # 每张图必须真的是 JPEG（扩展名和对内容都得对得上）
        for name in media:
            blob = zf.read(name)
            assert blob[:2] == b"\xff\xd8", f"{name} 不是 JPEG"

    assert docx_image_count(out.path) == 3
    xml = _doc_xml(out.path)
    assert xml.count("<w:drawing>") == 3, "图片段落数不对"
    # 首图前面没有分页符 → 3 张图正好 2 个分页符
    assert xml.count('w:type="page"') == 2, "每图一页应有 N-1 个分页符"
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in xml, "页面不是 A4"
    print(f"      {out.path.name}: 3 图 / 2 分页符 / {out.size_mb:.2f} MB / A4")

    # 连续排列：不插分页符，让 Word 自己分页
    lay = Layout(size_mode="width", target_px=1200, paging="flow")
    flow = export_document(shots, lay, tmp / "flow.docx", "word")
    assert flow.fmt == "docx", "别名 'word' 未归一化"
    xml = _doc_xml(flow.path)
    assert xml.count('w:type="page"') == 0, "连续排列不该有分页符"
    assert xml.count("<w:drawing>") == 3
    print(f"      flow: 0 分页符，同 3 张图")


def case_docx_relative_size(tmp: Path) -> None:
    print("\n[2] docx 尺寸策略：共享缩放系数")
    shots = sample_shots()
    lay = Layout(size_mode="original", paging="each")
    out = export_document(shots, lay, tmp / "sizes.docx", "docx")
    xml = _doc_xml(out.path)
    extents = [(int(cx), int(cy)) for cx, cy in
               re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', xml)]
    assert len(extents) == 3, extents

    ratios = [round(cx / cy, 3) for cx, cy in extents]
    for got, want in zip(ratios, (1600 / 900, 1.0, 2.5)):
        assert abs(got - want) < 0.01, f"比例失真: {got} != {want}"

    # 关键：宽度 1600 的图和宽度 1000 的图，在文档里宽度的比例还得是 1.6
    assert extents[0][0] > extents[1][0], "最宽的图没有更宽"
    ratio = extents[0][0] / extents[1][0]
    assert abs(ratio - 1.6) < 0.01, f"相对大小被压平了: {ratio}"
    # 最宽的那张要正好铺满 A4 版心（6.69in = 6123400 EMU 左右）
    usable_emu = (11906 - 2 * 1134) * 635
    assert abs(extents[0][0] - usable_emu) <= 2, (extents[0][0], usable_emu)
    print(f"      宽比 1600:1000 = {ratio:.3f}（应 1.600），"
          f"最宽图占版心 {extents[0][0]/914400:.2f}in")


def case_docx_align(tmp: Path) -> None:
    print("\n[3] docx 对齐：段落属性的三种取值")
    found = {}
    for align in ("left", "center", "right"):
        lay = Layout(size_mode="width", target_px=800, align=align)
        out = export_document(sample_shots(), lay, tmp / f"align_{align}.docx", "docx")
        xml = _doc_xml(out.path)
        values = set(re.findall(r'<w:jc w:val="(\w+)"/>', xml))
        assert align in values, f"{align} 没写进段落属性: {values}"
        found[align] = sorted(values)
    print(f"      {found}")


def case_pdf(tmp: Path) -> None:
    print("\n[4] PDF 页数与页尺寸")
    shots = sample_shots()
    lay = Layout(size_mode="width", target_px=1200, paging="each", margin_px=20)
    out = export_document(shots, lay, tmp / "pages.pdf", "pdf")

    assert out.path.suffix == ".pdf"
    assert out.path.stat().st_size > 1024, "PDF 太小，八成是空文件"
    assert out.pages == estimate_pages(shots, lay), (
        f"导出页数 {out.pages} != 预计 {estimate_pages(shots, lay)}"
    )

    canvases = render_pages(shots, lay)
    assert out.pages == len(canvases)

    try:
        from pypdf import PdfReader
    except ImportError:  # pypdf 只是自测依赖，缺了就只验文件头
        blob = out.path.read_bytes()
        assert blob.startswith(b"%PDF"), "不是 PDF"
        print(f"      无 pypdf，只验文件头 · {out.size_mb:.2f} MB")
        return

    reader = PdfReader(str(out.path))
    assert len(reader.pages) == len(canvases), (len(reader.pages), len(canvases))
    widths, heights = [], []
    for page, canvas in zip(reader.pages, canvases):
        box = page.mediabox
        w, h = float(box.width), float(box.height)
        widths.append(round(w, 2))
        heights.append(round(h, 2))
        want_h = 595.0 * canvas.shape[0] / canvas.shape[1]
        assert abs(h - want_h) < 1.0, f"页高 {h} 与该图比例不符（应 {want_h:.1f}）"
    assert len(set(widths)) == 1, f"各页宽度不一致: {widths}"
    print(f"      {len(reader.pages)} 页，页宽 {widths[0]}pt，页高 {heights}")

    # 连续排列：页数同样要等于预计值
    lay = Layout(size_mode="width", target_px=1200, paging="flow", margin_px=20)
    flow = export_document(shots, lay, tmp / "flow.pdf", "pdf")
    reader = PdfReader(str(flow.path))
    assert len(reader.pages) == flow.pages == estimate_pages(shots, lay)
    print(f"      flow: {len(reader.pages)} 页（预计 {estimate_pages(shots, lay)}）")


def texty_shot(width: int = 1200, height: int = 900) -> Shot:
    """造一张「有文字的图」：浅底 + 一叠深色横条。

    纯色块不能用来测体积 —— JPEG 编码一大片纯色几乎不花字节，而扫描滤镜会
    在上面加满颗粒，体积看起来就会暴涨几倍，那是合成样本的假象，不是真实代价。
    """
    img = np.full((height, width, 3), 250, dtype=np.uint8)
    for y in range(40, height - 20, 26):
        img[y : y + 11, 40 : width - 40] = 40
    return Shot(image=img)


def case_scan_export(tmp: Path) -> None:
    print("\n[5] 扫描效果一路传到文件")
    shots = [texty_shot()]
    # 固定 1× 清晰度作为对照：这里验的是「颗粒让 JPEG 涨多少」，
    # 混进像素倍数就分不清是滤镜还是密度在影响体积了。
    plain = export_document(
        shots,
        Layout(size_mode="width", target_px=1000, hi_res=1.0),
        tmp / "plain.pdf",
        "pdf",
    )
    scanned = export_document(
        shots,
        Layout(size_mode="width", target_px=1000, hi_res=1.0, scan=True, scan_strength=50),
        tmp / "scan.pdf",
        "pdf",
    )
    assert scanned.path.exists() and scanned.size_mb > 0

    # 颗粒是全图高频噪声，JPEG 压不动，体积必然涨一点；但涨到 1.5 倍以上
    # 就说明颗粒太重（人眼看着是「糊」而不是「扫描件」），属于回归。
    ratio = scanned.size_mb / plain.size_mb
    assert 1.0 < ratio < 1.5, f"扫描版体积 {ratio:.2f}× 彩色版，超出合理区间"
    print(f"      扫描 {scanned.size_mb:.2f} MB / 彩色 {plain.size_mb:.2f} MB "
          f"= {ratio:.2f}×")


def case_guards(tmp: Path) -> None:
    print("\n[6] 格式归一化与拒绝空导出")
    assert normalize_format("Word") == "docx"
    assert normalize_format(".DOCX") == "docx"
    assert normalize_format("pdf") == "pdf"
    assert normalize_format(".PDF") == "pdf"
    for bad in ("excel", "", "pptx"):
        try:
            normalize_format(bad)
        except ValueError as exc:
            pass
        else:
            raise AssertionError(f"{bad!r} 未被拒绝")

    name = default_name("docx")
    assert name.startswith("连续截图_") and name.endswith(".docx"), name
    assert len(name) == len("连续截图_YYYYMMDD_HHMMSS.docx")
    print(f"      默认文件名 {name}")

    try:
        export_document([], Layout(), tmp / "empty.pdf", "pdf")
    except ValueError as exc:
        print(f"      空列表被拒：{exc}")
    else:
        raise AssertionError("空列表竟然导出了")
    assert not (tmp / "empty.pdf").exists(), "失败时不该留下半成品"

    # 后缀写错也要纠正到实际格式，而不是生成一个 docx 名字的 PDF
    res = export_document(sample_shots(), Layout(size_mode="width", target_px=600),
                          tmp / "wrongname.pdf", "docx")
    assert res.path.suffix == ".docx", res.path


def main() -> int:
    print("=" * 64)
    print("连续截图整合 · 导出 Word / PDF")
    print("=" * 64)
    tmp = Path(tempfile.mkdtemp(prefix="longshot_export_"))
    try:
        case_docx_structure(tmp)
        case_docx_relative_size(tmp)
        case_docx_align(tmp)
        case_pdf(tmp)
        case_scan_export(tmp)
        case_guards(tmp)
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
