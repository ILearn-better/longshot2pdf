"""一组图片 → 一份 .docx（Word / WPS 都能打开）。

为什么手写 OOXML 而不用 python-docx
-----------------------------------
python-docx 依赖 lxml（C 扩展，约 4.5MB）。本项目其余部分只需要 numpy + Pillow，
为了「导出 Word」这一个动作给所有用户加一个编译扩展依赖，代价不相称；
而且一旦某个 Python 版本没有对应的 lxml 轮子，pip 会去编译 C 代码，
在没有编译器的 Windows 上直接失败 —— 那正是启动器辛苦避开的那类问题。

我们要生成的东西其实很窄：一串图片段落 + 对齐方式 + 分页 + 页面尺寸。
这正好是 OOXML 里最稳定、十几年没变过的一小块，手写完全可控。
生成结果可以用 python-docx **反过来读**来做独立校验（见 tests/test_docx.py）。

docx 本质
---------
一个 ZIP 包，里面是若干 XML + 媒体文件。最小可用集合：
    [Content_Types].xml        声明每种扩展名/部件的类型
    _rels/.rels                包级关系 → 指向 word/document.xml
    docProps/core.xml          标题/作者等元数据
    docProps/app.xml           应用名
    word/document.xml          正文
    word/_rels/document.xml.rels  正文里引用的图片/样式
    word/styles.xml            默认段落样式（Word 会按它决定默认字体行距）
    word/media/imageN.jpeg     图片本体

单位
----
- 长度：EMU（1 英寸 = 914400 EMU）。像素按 96 DPI 换算：1px = 9525 EMU。
- 页面/边距：twip（1/20 pt）。A4 = 11906×16838 twip，2cm 边距 = 1134 twip。
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

from . import paths

EMU_PER_PX = 9525          # 914400 / 96
A4_W_TWIP = 11906          # 210mm
A4_H_TWIP = 16838          # 297mm
MARGIN_TWIP = 1134         # 20mm

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)

_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_CONTENT_TYPES = (
    _HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
    'package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.extended-properties+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    _HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
    'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/extended-properties" Target="docProps/app.xml"/>'
    "</Relationships>"
)

_STYLES = (
    _HEAD
    + f"<w:styles {_NS}>"
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/>'
    '<w:sz w:val="21"/><w:szCs w:val="21"/>'
    "</w:rPr></w:rPrDefault>"
    '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    "</w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/>'
    "</w:style>"
    "</w:styles>"
)

_APP = (
    _HEAD
    + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
    'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
    'docPropsVTypes"><Application>LongShot2PDF</Application></Properties>'
)


def _core_xml(title: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        _HEAD
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_esc(title)}</dc:title><dc:creator>LongShot2PDF</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_ALIGN = {"left": "left", "center": "center", "right": "right"}


def _picture_paragraph(idx: int, cx: int, cy: int, align: str, gap_twip: int) -> str:
    """一段「只放一张图」的段落。

    docPr 的 id 在文档内必须唯一，否则 Word 会报「内容有问题」；
    用序号而不是随机值，出问题时人能直接对上号。
    """
    jc = _ALIGN.get(align, "center")
    return (
        "<w:p><w:pPr>"
        f'<w:spacing w:before="0" w:after="{gap_twip}" w:line="240" w:lineRule="auto"/>'
        f'<w:jc w:val="{jc}"/>'
        "</w:pPr><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{idx}" name="Picture {idx}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/>'
        "</wp:cNvGraphicFramePr>"
        "<a:graphic>"
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:pic>"
        f'<pic:nvPicPr><pic:cNvPr id="{idx}" name="image{idx}.jpeg"/><pic:cNvPicPr/>'
        "</pic:nvPicPr>"
        f'<pic:blipFill><a:blip r:embed="rIdImg{idx}"/><a:stretch><a:fillRect/></a:stretch>'
        "</pic:blipFill>"
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


_PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _encode_jpeg(image: np.ndarray, quality: int) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    # subsampling=0 (4:4:4)：截图里全是细笔画文字，色度下采样会让黑字边缘泛彩边，
    # 与 pdfwriter 用同一套参数，保证「同一张图在 PDF 和 Word 里长得一样」。
    Image.fromarray(image).save(buf, "JPEG", quality=int(quality), subsampling=0)
    return buf.getvalue()


def write_docx(
    images: Sequence[np.ndarray],
    out_path: str | Path,
    *,
    align: str = "center",
    page_break_between: bool = True,
    quality: int = 90,
    gap_pt: float = 6.0,
    title: str = "连续截图整合",
    sizes_px: Sequence[tuple[int, int]] | None = None,
    unique: bool = True,
) -> Path:
    """把一组 RGB 图片写成 docx，返回输出路径。

    align : left / center / right，控制每张图在页面里的水平位置。
    page_break_between : True = 每张图一页；False = 首尾相接连续排布。
    gap_pt : 连续排布时图与图之间的空隙（pt），每图一页时是页内底部留白。
    quality : JPEG 质量，与 PDF 侧保持一致。
    sizes_px : 每张图的**排版尺寸**（96dpi 口径）。给了它就按这个尺寸算 EMU，
        图片本身的像素可以更大 —— 页面上占的宽度不变，但每英寸的像素更多，
        也就是「同样的版面、更高的清晰度」。不传时退回按图片自身像素算。

    尺寸策略：所有图**乘同一个缩放系数**，让最宽的那张正好放进版心。
    不用「每张各自缩到版心宽」——那样宽度 800 和 1600 的图会变成一样大，
    用户选的「统一宽度/原始尺寸」就白设了。
    """
    if len(images) == 0:
        raise ValueError("没有可导出的图片")

    actual = [(int(im.shape[1]), int(im.shape[0])) for im in images]
    if any(w <= 0 or h <= 0 for w, h in actual):
        raise ValueError("存在空图片")

    if sizes_px is None:
        logical = actual
    else:
        logical = [(int(w), int(h)) for w, h in sizes_px]
        if len(logical) != len(actual):
            raise ValueError(
                f"sizes_px 数量（{len(logical)}）与图片数量（{len(actual)}）不一致"
            )
        if any(w <= 0 or h <= 0 for w, h in logical):
            raise ValueError("sizes_px 里有非正的尺寸")

    usable_twip = A4_W_TWIP - 2 * MARGIN_TWIP
    usable_emu = int(usable_twip * 635)          # 1 twip = 635 EMU
    # 缩放系数按**排版尺寸**算，与嵌入像素无关 —— 这正是「版式不变、像素变多」的关键
    widest_emu = max(w for w, _ in logical) * EMU_PER_PX
    scale = min(1.0, usable_emu / max(1, widest_emu))

    gap_twip = max(0, int(round(gap_pt * 20)))

    body: list[str] = []
    for i, (w, h) in enumerate(logical, start=1):
        if i > 1 and page_break_between:
            body.append(_PAGE_BREAK)
        cx = max(1, int(round(w * EMU_PER_PX * scale)))
        cy = max(1, int(round(h * EMU_PER_PX * scale)))
        body.append(_picture_paragraph(i, cx, cy, align, gap_twip))

    sect = (
        "<w:sectPr>"
        f'<w:pgSz w:w="{A4_W_TWIP}" w:h="{A4_H_TWIP}"/>'
        f'<w:pgMar w:top="{MARGIN_TWIP}" w:right="{MARGIN_TWIP}" '
        f'w:bottom="{MARGIN_TWIP}" w:left="{MARGIN_TWIP}" '
        'w:header="851" w:footer="992" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document = _HEAD + f"<w:document {_NS}><w:body>" + "".join(body) + sect + "</w:body></w:document>"

    rels = [_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    rels.append(
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    for i in range(1, len(images) + 1):
        rels.append(
            f'<Relationship Id="rIdImg{i}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/image" Target="media/image{i}.jpeg"/>'
        )
    rels.append("</Relationships>")

    out_path = Path(out_path)
    why = paths.check_target(out_path)
    if why:
        raise ValueError(why)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
    if unique:
        # 与 PDF 侧同一条规矩：同名不覆盖，改成 名字(1).docx
        out_path = paths.unique_path(out_path)

    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # [Content_Types].xml 放第一个：OPC 规范没强制顺序，
            # 但有些第三方解析器（含部分 WPS 版本）按顺序找，放第一个最稳。
            zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
            zf.writestr("_rels/.rels", _ROOT_RELS)
            zf.writestr("docProps/core.xml", _core_xml(title))
            zf.writestr("docProps/app.xml", _APP)
            zf.writestr("word/document.xml", document)
            zf.writestr("word/_rels/document.xml.rels", "".join(rels))
            zf.writestr("word/styles.xml", _STYLES)
            for i, image in enumerate(images, start=1):
                zf.writestr(f"word/media/image{i}.jpeg", _encode_jpeg(image, quality))
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
    return out_path


def docx_image_count(path: str | Path) -> int:
    """数一下包里有几张图，供自测断言用（不依赖 python-docx）。"""
    with zipfile.ZipFile(Path(path)) as zf:
        return sum(1 for n in zf.namelist() if n.startswith("word/media/"))
