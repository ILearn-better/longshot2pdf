"""页 JPEG 序列 → 多页 PDF。

关键点：**不重新编码**。img2pdf 直接把 JPEG 码流塞进 PDF 作为图像流，
像素零损失（这也呼应了需求里的「像素清晰或不变」）。
如果改用 Pillow/reportlab 生成 PDF，图片会被重新压缩一次，文字就糊了。
"""

from __future__ import annotations

from pathlib import Path

import img2pdf

A4_WIDTH_PT = 595.0  # A4 纵向宽度（pt）


def jpegs_to_pdf(
    pages: list[Path],
    out_path: Path,
    page_w_pt: float = A4_WIDTH_PT,
    page_ratio: float = 1.414,
) -> Path:
    """把一串页 JPEG 合成一个多页 PDF。

    page_w_pt 固定为 A4 宽，页高按 page_ratio 推算，保证：
    - 所有页物理宽度一致（阅读器里不会忽大忽小）
    - 页比例与图片像素比例一致（不失真，不会被拉长）
    """
    if not pages:
        raise ValueError("没有可输出的页，无法生成 PDF")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page_h_pt = page_w_pt * page_ratio

    layout = img2pdf.get_layout_fun((page_w_pt, page_h_pt))
    with open(out_path, "wb") as fh:
        img2pdf.convert(
            [str(p) for p in pages],
            layout_fun=layout,
            outputstream=fh,
        )
    return out_path
