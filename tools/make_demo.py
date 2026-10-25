"""生成 demo/ 下的示例文档（Word / PDF / 扫描版 PDF）。

为什么要有这个脚本
------------------
`demo/` 在 `.gitignore` 里（它只是运行产物），所以刚 clone 下来的仓库里没有样例。
README 提到「不用开界面也能看效果」，那就得有一条命令能在本地把样例造出来，
否则那句话对新用户就是空头支票。

    python tools/make_demo.py

造出来的是四张**尺寸各不相同**的合成截图（1 张整屏 + 3 块框选区域），
正好演示「区域大小不一也能排成一份整齐的文档」这件事，顺带打印出
标准档 / 高清档的有效 dpi —— 清晰度提升是能算出来的，不是形容词。

脚本只依赖项目本身已有的 numpy + Pillow，不需要开界面、不抓屏、不联网。
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.shots import Layout, Shot  # noqa: E402
from src.shots_export import export_document  # noqa: E402

OUT_DIR = ROOT / "demo"

# 演示文本：写清楚这个功能在解决什么问题，比放 lorem ipsum 有用
PARAS = [
    "第一章  为什么要框选",
    "整屏连拍会把侧边栏、工具栏、滚动条一起收进每一张图，读的人得自己忽略它们。",
    "框一块正文区域，等于在采集的时候就做了一次裁剪，而且每张的宽高完全一致。",
    "",
    "第二章  位置与大小",
    "九宫格把这块贴到角、边或正中；锁定比例后改宽，高会自动跟着走。",
    "区域可以超出屏幕边缘，导出前会被拉回来，而不是被裁掉一半。",
    "",
    "第三章  清晰度",
    "导出的图按排版尺寸定位，但嵌入的是原图像素，所以版面不变、字更实。",
    "如果原图本来就不够大，程序不会假装放大 —— 放大只增加体积，不增加细节。",
]


def font(size: int, bold: bool = False):
    """找一个能显示中文的字体；找不到就退回默认字体（英文数字仍可读）。"""
    for path in (
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def screen(w: int, h: int, title: str, seed: int) -> np.ndarray:
    """画一张「长得像文档截屏」的图：顶栏 + 若干段文字 + 底栏。"""
    img = Image.new("RGB", (w, h), (252, 252, 254))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 46], fill=(32, 40, 58))
    d.text((24, 11), title, font=font(22, True), fill=(255, 255, 255))
    d.text((w - 150, 16), "LongShot2PDF", font=font(14), fill=(150, 165, 190))

    y = 84
    for line in PARAS:
        if not line:
            y += 14
            continue
        big = line.startswith("第")
        d.text(
            (44, y),
            line,
            font=font(24 if big else 19, big),
            fill=(20, 24, 33) if big else (58, 62, 72),
        )
        y += 42 if big else 32
        if y > h - 60:
            break

    d.rectangle([0, h - 30, w, h], fill=(238, 240, 245))
    d.text((44, h - 25), f"第 {seed} 张 · 演示用合成截图", font=font(14), fill=(120, 126, 138))
    return np.asarray(img)


def sample_shots() -> list[Shot]:
    """四张不同尺寸的图：整屏 + 三块框选，尺寸各不相同是有意的。"""
    return [
        Shot(image=screen(1440, 900, "整屏连拍（1/4）", 1)),
        Shot(image=screen(900, 680, "框选正文（2/4）", 2), label="框选"),
        Shot(image=screen(1200, 760, "框选表格（3/4）", 3), label="框选"),
        Shot(image=screen(1100, 520, "框选说明（4/4）", 4), label="框选"),
    ]


def docx_effective_dpi(path: Path) -> tuple[int, float, float]:
    """读回 docx：图片真实像素宽 / 版面英寸 → 有效 dpi。

    不引入 python-docx：docx 就是 ZIP + XML，这里只需要两个数。
    """
    with zipfile.ZipFile(path) as z:
        media = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        if not media:
            raise RuntimeError(f"{path} 里没有图片")
        px = Image.open(io.BytesIO(z.read(media[0]))).size[0]
        doc = z.read("word/document.xml").decode("utf-8")
    cx = int(re.search(r'<wp:extent cx="(\d+)"', doc).group(1))
    inches = cx / 914400
    return px, inches, px / inches


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    shots = sample_shots()

    base = Layout(
        size_mode="width", target_px=1000, align="center", paging="each",
        margin_px=24, hi_res=2.0,
    )
    scan = Layout(
        size_mode="width", target_px=1000, align="center", paging="flow",
        margin_px=24, gap_px=18, scan=True, scan_strength=55, hi_res=2.0,
    )

    print("四张源图：" + "、".join(f"{s.width}×{s.height}" for s in shots))
    print()

    word = export_document(shots, base, OUT_DIR / "shots_demo.docx", "docx")
    print("Word（每图一页）  " + word.describe())
    px, inch, dpi = docx_effective_dpi(word.path)
    print(f"                   首张图嵌入 {px}px / 版面 {inch:.2f}in → {dpi:.0f} dpi")

    pdf = export_document(shots, base, OUT_DIR / "shots_demo.pdf", "pdf")
    print("PDF（每图一页）   " + pdf.describe())

    scan_pdf = export_document(shots, scan, OUT_DIR / "shots_demo_scan.pdf", "pdf")
    print("PDF（连续+扫描）  " + scan_pdf.describe())

    # 对照组：同一份版面在「标准」档下的有效 dpi —— 清晰度提升得能算出来
    low = export_document(
        shots,
        Layout(size_mode="width", target_px=1000, align="center", hi_res=1.0),
        OUT_DIR / "_standard.docx",
        "docx",
    )
    px0, inch0, dpi0 = docx_effective_dpi(low.path)
    low.path.unlink()
    ratio = (dpi / dpi0) if dpi0 else 1.0
    print()
    print(
        f"清晰度对照（同一份版面 {inch0:.2f}in 宽）："
        f"标准档 {px0}px → {dpi0:.0f} dpi，高清档 {px}px → {dpi:.0f} dpi（{ratio:.2f}×）"
    )
    print(f"输出目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
