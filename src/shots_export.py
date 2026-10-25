"""连续截图整合的导出编排：排版结果 → .docx / .pdf。

为什么单独一层
--------------
界面只关心三件事：成功了没有、多少页、多大。它不该知道 docx 是个 ZIP 包、
PDF 要过 img2pdf、页数是怎么算的。把这些知识关在这里，收益是：
  · 导出流程可以脱离 tkinter 测试（tests/test_export.py 直接调它）；
  · 以后加命令行入口 / 拖拽导出，只是换个调用方，不用碰界面。

两种格式的口径差异（这是有意为之，不是不一致）
-----------------------------------------------
**PDF**：喂给 img2pdf 的是 `render_pages()` 的**整页画布**。
    于是 PDF 的页数 == 界面上「预计 N 页」，边距、对齐、切页位置全在画布上，
    所见即所得。

**Word**：喂给 docx 的是 `prepare()` 的**单张图**，由 Word 自己排。
    原因有两条：
    1) Word 里图片保持为可选中、可拖动、可另存的对象。若把整页画布塞进去，
       用户拿到的是一张「截图里套着截图」的图片，再也没法编辑；
    2) docx 页面本身有 20mm 页边距，画布上再烘一层白边就是双重留白。
    所以 Word 侧用「所有图乘同一缩放系数 + 段落对齐」复现同样的观感，
    而 `flow`（连续排布）直接靠不分页实现 —— Word 的自然分页比我们先切好更合理。
    代价是 `flow` 模式下 Word 的页数由 Word 决定，不一定等于「预计 N 页」，
    界面上的文案因此写成「预计」而不是「共」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .shots import Layout, Shot, estimate_pages, prepare, render_pages

# 截图像素 → pt（1px@96dpi = 0.75pt）。docxwriter 里 gap 用的是 pt，
# Layout 里的间距是 px，换算只在这一处发生。
_PX_TO_PT = 0.75

FORMATS = ("docx", "pdf")
_ALIASES = {
    "docx": "docx",
    "word": "docx",
    "doc": "docx",
    "w": "docx",
    "pdf": "pdf",
    "p": "pdf",
}


@dataclass
class ExportResult:
    """导出结果。字段全部是界面直接要显示的，不做二次加工。"""

    path: Path
    fmt: str
    images: int
    pages: int
    size_mb: float

    def describe(self) -> str:
        label = "Word" if self.fmt == "docx" else "PDF"
        return (
            f"已导出 {label} · {self.images} 张 · 预计 {self.pages} 页 · "
            f"{self.size_mb:.2f} MB"
        )


def normalize_format(name: str) -> str:
    """把 'Word' / 'DOCX' / '.pdf' 之类统一成 'docx' / 'pdf'。"""
    key = str(name or "").strip().lower().lstrip(".")
    try:
        return _ALIASES[key]
    except KeyError:
        raise ValueError(f"不支持的格式：{name!r}（只支持 Word/docx 与 PDF）") from None


def default_name(fmt: str, when: datetime | None = None) -> str:
    """默认文件名。与长截图那边的 longshot_*.pdf 区分开，避免混在同一个目录里。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"连续截图_{stamp}.{normalize_format(fmt)}"


def export_document(
    shots: Sequence[Shot],
    layout: Layout,
    out_path,
    fmt: str = "pdf",
    *,
    quality: int = 85,
    title: str = "连续截图整合",
) -> ExportResult:
    """把截图按 layout 排版后写成 Word 或 PDF，返回 ExportResult。

    参数
    ----
    shots : 按截图顺序排列的 Shot 列表（顺序就是文档顺序，这里不再排序）
    layout : 排版参数；内部会 normalized()，界面的越界输入不会炸到这一层
    fmt : 'docx' / 'pdf'，也接受 'word' / 'Word' / '.pdf'
    """
    shots = list(shots)
    if not shots:
        raise ValueError("还没有截图，无法导出")

    fmt = normalize_format(fmt)
    out_path = Path(out_path)
    if out_path.suffix.lower().lstrip(".") != fmt:
        out_path = out_path.with_suffix("." + fmt)

    lay = layout.normalized()
    pages_est = estimate_pages(shots, lay)

    if fmt == "docx":
        from .docxwriter import write_docx

        placed = prepare(shots, lay)
        write_docx(
            [p.image for p in placed],
            out_path,
            # 段落对齐 = 需求里的「位置对齐」；单图一页时也就是水平居中/左/右
            align=lay.align,
            # 只有「每图一页」才插分页符；「连续排列」交给 Word 自然分页
            page_break_between=(lay.paging == "each"),
            quality=int(quality),
            gap_pt=max(0.0, lay.gap_px * _PX_TO_PT),
            title=title,
        )
        images = len(placed)
    else:
        from .pdfwriter import images_to_pdf

        canvases = render_pages(shots, lay)
        images_to_pdf(canvases, out_path, quality=int(quality))
        images = len(shots)
        pages_est = len(canvases)  # PDF 的页数就是画布数，不用估

    size_mb = out_path.stat().st_size / (1024.0 * 1024.0)
    return ExportResult(
        path=out_path, fmt=fmt, images=images, pages=pages_est, size_mb=size_mb
    )
