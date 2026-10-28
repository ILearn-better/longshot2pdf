"""PDF 按页码合并：范围解析、页序、零重编码、同名避让、错误话术。

为什么这些必须离线验
--------------------
合并最危险的失败不是「报错」，而是**静默少页**：用户写 ``1-20,22-40``
想要跳过第 21 页，程序如果少解析一段、或者越界时悄悄截断，成品依旧能打开、
页数也"看起来正常"，等发现少了几十页时原件可能已经不在手边了。
所以这里对新文件的每一页都做**字节级**比对：合并后的第 k 页内嵌 JPEG
必须与源文件对应的那一页完全相同（img2pdf / pypdf 都不重编码）。

检验：
1. parse_pages：常规、全角、多段、省略一端、全部 → 页码列表正确
2. parse_pages：越界 / 反序 / 乱写 → 都有能看懂的错误话术
3. describe_pages：连续页码压成区间写法
4. merge：19 页 + 40 页取 1-20,22-40 → 58 页，且每页字节与源一致（页序对）
5. 同一文件多段范围（1-20,22-40）与同一文件出现两次，都按写的顺序拼
6. 输出同名 → 自动 名字(1).pdf，原文件不动
7. 错误输入：文件不存在 / 0 字节 / 加密 PDF / 页码越界
8. pypdf「装了但导入链坏了」→ 报真因，而不是「缺少 pypdf」
9. parse_spec：命令行 a.pdf=1-19 的解析（Windows 路径含盘符也不能被切开）

直接运行：  python tests/test_pdfmerge.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src import pdfmerge, pdfwriter  # noqa: E402


def make_pdf(dir_path: Path, name: str, pages: int, color) -> tuple[Path, list[bytes]]:
    """造一份 pages 页的 PDF，返回 (pdf 路径, 每页源 JPEG 的字节)。

    每页左上角写一个不同的色块，既保证页与页不同（页序错了一定能发现），
    又让 JPEG 码流不一样（字节比对才有意义）。
    """
    jpegs: list[Path] = []
    for i in range(pages):
        arr = np.zeros((90, 70, 3), dtype=np.uint8)
        arr[:, :] = color
        arr[: 6 + i % 5, : 6 + i % 7] = 255 - i % 200
        p = dir_path / f"{name}_{i + 1:03d}.jpg"
        Image.fromarray(arr).save(p, quality=88, subsampling=0)
        jpegs.append(p)
    out = pdfwriter.jpegs_to_pdf(jpegs, dir_path / f"{name}.pdf")
    return out, [p.read_bytes() for p in jpegs]


def case_parse() -> None:
    print("\n[1] 页码范围解析")
    ok = [
        # (表达式, 总页数, 期望页数, 说明)
        ("1-19", 19, 19, "单个区间"),
        ("1-20,22-40", 40, 39, "跳过第 21 页"),
        ("1-3,5", 10, 4, "区间 + 单页"),
        ("", 7, 7, "留空 = 全部"),
        ("全部", 7, 7, "「全部」"),
        ("*", 7, 7, "星号 = 全部"),
        ("１－３，５", 5, 4, "全角数字与标点"),
        ("1~3", 5, 3, "波浪号当区间符"),
        ("5-", 9, 5, "省略右端 = 到末页"),
        ("-3", 9, 3, "省略左端 = 从第 1 页"),
        ("2、4、6", 6, 3, "顿号分隔"),
        ("3-3", 5, 1, "起止相同"),
        ("1-3,2-4", 5, 6, "重复页保留（用户就要 1,2,3,2,3,4）"),
    ]
    for text, total, want, why in ok:
        got = pdfmerge.parse_pages(text, total)
        assert len(got) == want, f"{text!r} → {len(got)} 页，期望 {want}"
        print(f"      {text!r:<14} 共{total:>2}页 → {len(got):>2} 页（{why}）")

    assert pdfmerge.parse_pages("1-20,22-40", 40)[20] == 22
    print("      页序：第 21 个元素是第 22 页（确实跳过了 21）")

    bad = [
        ("22-40", 19, "超出范围"),
        ("0-3", 19, "0 不是合法页码"),
        ("5-2", 19, "起始页大于结束页"),
        ("1-x", 19, "看不懂"),
        ("abc", 19, "看不懂"),
    ]
    print("\n[2] 错误话术（必须能直接显示给用户）")
    for text, total, why in bad:
        try:
            pdfmerge.parse_pages(text, total)
        except pdfmerge.MergeError as exc:
            print(f"      {text!r:<8} → {exc}")
        else:
            raise AssertionError(f"{text!r} 应该报错（{why}）")

    # 总页数为 0 的坏文件要单独有话说，不能算成「全部 = 0 页」
    try:
        pdfmerge.parse_pages("", 0)
    except pdfmerge.MergeError as exc:
        print(f"      空文件     → {exc}")


def case_describe() -> None:
    print("\n[3] 区间写法还原")
    cases = [
        ([1, 2, 3, 7, 9, 10], "1-3,7,9-10"),
        ([5], "5"),
        ([1, 2], "1-2"),
        ([], "（空）"),
    ]
    for pages, want in cases:
        got = pdfmerge.describe_pages(pages)
        assert got == want, (pages, got, want)
        print(f"      {str(pages):<26} → {got}")


def case_merge(tmp: Path) -> None:
    print("\n[4] 合并：19 页 + 40 页取 1-20,22-40 → 58 页")
    a, a_bytes = make_pdf(tmp, "a", 19, (210, 40, 40))
    b, b_bytes = make_pdf(tmp, "b", 40, (40, 60, 200))
    assert pdfwriter.pdf_page_count(a) == 19
    assert pdfwriter.pdf_page_count(b) == 40

    out = tmp / "合并.pdf"
    result = pdfmerge.merge([(a, "1-19"), (b, "1-20,22-40")], out)
    print(f"      {result.summary().replace(chr(10), '  ·  ')}")
    assert result.pages == 58, result.pages
    assert result.out_path.name == "合并.pdf"

    from pypdf import PdfReader

    reader = PdfReader(str(result.out_path))
    assert len(reader.pages) == 58, len(reader.pages)
    got_bytes = []
    for page in reader.pages:
        xobjects = page["/Resources"]["/XObject"]
        got_bytes.append([xobjects[k].get_data() for k in xobjects][0])
    expect = a_bytes + [b_bytes[i - 1] for i in list(range(1, 21)) + list(range(22, 41))]
    assert len(got_bytes) == len(expect), (len(got_bytes), len(expect))
    mismatch = [i + 1 for i, (x, y) in enumerate(zip(got_bytes, expect)) if x != y]
    assert not mismatch, f"第 {mismatch[:5]} 页内容与源不一致"
    print("      58 页逐页字节比对：与源完全一致（零重编码 + 页序正确）")

    print("\n[5] 同一文件多段 / 同一文件出现两次")
    again = pdfmerge.merge([(b, "30-40"), (b, "1-2"), (a, "")], tmp / "多段.pdf")
    assert again.pages == 11 + 2 + 19, again.pages
    print(f"      b(30-40) + b(1-2) + a(全部) → {again.pages} 页")
    assert [s.path.name for s in again.sources] == ["b.pdf", "b.pdf", "a.pdf"]

    print("\n[6] 输出同名 → 自动加 (1)，原文件不动")
    before = result.out_path.read_bytes()
    second = pdfmerge.merge([(a, "1-3")], out)
    assert second.out_path.name == "合并(1).pdf", second.out_path.name
    assert result.out_path.read_bytes() == before
    print(f"      {second.out_path.name}（{second.pages} 页），合并.pdf 未被改动")


def case_errors(tmp: Path) -> None:
    print("\n[7] 坏输入")
    a, _ = make_pdf(tmp, "c", 5, (60, 160, 90))

    def expect_error(items, out: Path, needle: str, why: str) -> None:
        try:
            pdfmerge.merge(items, out)
        except pdfmerge.MergeError as exc:
            assert needle in str(exc), (needle, str(exc))
            print(f"      {why} → {str(exc).splitlines()[0]}")
        else:
            raise AssertionError(f"{why} 应该报错")

    expect_error([(tmp / "没有这个.pdf", "")], tmp / "x.pdf", "找不到文件", "文件不存在")

    empty = tmp / "空.pdf"
    empty.write_bytes(b"")
    expect_error([(empty, "")], tmp / "x.pdf", "0 字节", "0 字节文件")

    expect_error([(a, "3-99")], tmp / "x.pdf", "超出范围", "页码越界")

    # 加密 PDF：能读出来但打不开页，必须说「先解密」而不是抛 IndexError
    from pypdf import PdfWriter

    enc = tmp / "加密.pdf"
    writer = PdfWriter()
    writer.append(str(a))
    writer.encrypt("secret")
    with open(enc, "wb") as fh:
        writer.write(fh)
    expect_error([(enc, "")], tmp / "x.pdf", "加密", "加密 PDF")

    # 报错里必须带上是**哪个**文件，否则十个文件一起合并时无从下手
    missing = tmp / "缺.pdf"
    try:
        pdfmerge.merge([(a, ""), (missing, "")], tmp / "x.pdf")
    except pdfmerge.MergeError as exc:
        assert "缺.pdf" in str(exc), str(exc)
        print(f"      多文件时指名道姓 → {exc}")

    try:
        pdfmerge.merge([], tmp / "x.pdf")
    except pdfmerge.MergeError as exc:
        print(f"      空列表 → {exc}")
    else:
        raise AssertionError("空列表应该报错")


def case_spec() -> None:
    print("\n[9] 命令行写法 a.pdf=1-19（Windows 盘符不能被切开）")
    item = pdfmerge.parse_spec("C:/tmp/长图.pdf=1-19")
    assert item.path.name == "长图.pdf" and item.ranges == "1-19", item
    print(f"      {item.path.name}  范围 {item.ranges}")
    item = pdfmerge.parse_spec("C:/tmp/长图.pdf")
    assert item.ranges == "", item
    print("      省略 =页码 → 整本")
    try:
        pdfmerge.parse_spec("   ")
    except pdfmerge.MergeError as exc:
        print(f"      空串 → {exc}")
    else:
        raise AssertionError("空串应该报错")


def case_import_chain(tmp: Path) -> None:
    """pypdf 装了、但导入链坏了 —— 不能再说成「没装」。

    这是 v0.10 打包时真实踩到的坑：exe 里排掉了标准库的 ``xml``，
    而 pypdf/xmp.py 顶层要 ``from xml.dom.expatbuilder import ExpatBuilderNS``，
    于是 ``import pypdf`` 抛的是 ``No module named 'xml.dom.expatbuilder'``。
    原来的写法把任何异常都翻成「缺少 pypdf，pip install 一下」——
    照着这句话装一百遍也没用。

    这里**照原样复现**：把 pypdf 从 sys.modules 里摘干净，再把
    ``xml.dom.expatbuilder`` 置 None（等同于「这个模块不在包里」），
    于是 pypdf 重新导入时正好在同一个位置炸掉。

    注意不能用 ``sys.modules['pypdf'] = None`` 来模拟 —— CPython 3.11 对它
    抛的是 ``ModuleNotFoundError(name='pypdf')``，与「真没装」逐字相同，
    测了也证明不了什么。
    """
    print("\n[8] pypdf 导入链坏了：要说真因，不能说「没装」")
    src, _ = make_pdf(tmp, "链", 2, (12, 34, 56))

    stashed = {k: sys.modules.pop(k) for k in list(sys.modules) if k.split(".")[0] == "pypdf"}
    had_expat = sys.modules.get("xml.dom.expatbuilder", "__absent__")
    sys.modules["xml.dom.expatbuilder"] = None
    try:
        pdfmerge.merge([(src, "")], tmp / "链合并.pdf")
    except RuntimeError as exc:
        text = str(exc)
        assert "导入失败" in text, text
        assert "xml.dom.expatbuilder" in text, (
            "真因（缺哪个模块）必须带出来，现在只说了：\n" + text
        )
        assert "pip install pypdf\n" not in text, (
            "pypdf 明明装着，不该给「装一下」那句：\n" + text
        )
        print(f"      真因带出来了 → {text.splitlines()[0]}")
    else:
        raise AssertionError("pypdf 导入不了却没有报错")
    finally:
        if had_expat == "__absent__":
            sys.modules.pop("xml.dom.expatbuilder", None)
        else:
            sys.modules["xml.dom.expatbuilder"] = had_expat
        sys.modules.update(stashed)

    # 恢复后必须还能正常合并：确认刚才只是「那一次」抛错，没污染模块状态
    res = pdfmerge.merge([(src, "")], tmp / "链恢复.pdf")
    assert res.pages == 2, res.pages
    print(f"      恢复后正常合并 → {res.pages} 页，{res.size_mb:.2f} MB")


def main() -> int:
    print("=" * 64)
    print("PDF 合并 · 页码范围 / 页序 / 零重编码 / 同名避让 / 错误话术")
    print("=" * 64)
    try:
        case_parse()
        case_describe()
        with tempfile.TemporaryDirectory(prefix="ls2pdf_merge_") as tag:
            tmp = Path(tag)
            case_merge(tmp)
            case_errors(tmp)
            case_import_chain(tmp)
        case_spec()
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
