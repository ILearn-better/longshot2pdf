"""连续截图整合：会话模型 + 尺寸/位置对齐 + 扫描版效果 + 页面拼版。

与 `stitch.py` 的区别（两件事不要混）
------------------------------------
`stitch.py` 解决的是「一屏内容滚动后如何**无缝接上**」——它靠内容比对反推位移，
输出是一张无限长的连续图。本模块解决的是「N 张**互不相关**的截图如何排成一份
像样的文档」——图与图之间没有重叠、没有接缝，只有版式问题。

所以这里一个像素都不需要比对，全是几何计算：缩放、对齐、拼版、切页。

三个设计决定
------------
1) **先缩放再拼版**，且缩放用一个统一的目标边长。
   直接拼原图会出现「宽度 1280 和 800 的两张图并排」，页宽只能取最大值，
   窄图旁边挂一条白边——这就是需求里说的「长宽不一致」。统一宽度之后，
   页宽是确定的，对齐才有意义。
2) **页面画布用 numpy 直接铺白底再贴图**，不用 PIL 的 paste 循环。
   一张 A4 页 1200×1700 用 numpy 是 6MB 的连续内存，几十页也可以流式写出，
   而 PIL 的 paste 每页都要多一次整图拷贝。
3) **扫描效果做成「可选滤镜」而不是另存一份图**。
   预览和导出都从原图即时算，改强度立刻能看到，磁盘上永远只有一份原图。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image

# 目标边长的合法范围。上限 8000 是按「4K 屏再缩放」留的余量：
# 再大就只是把 JPEG 体积和内存一起推高，肉眼在 PDF/Word 里分辨不出来。
MIN_TARGET_PX = 200
MAX_TARGET_PX = 8000
MAX_SIDE_PX = 20000  # 拼版后单页任一边的上限，超了任何阅读器都会很卡
MAX_STRIP_H = 200_000  # 连续排列时整条竖图的高度上限（约 100 页 A4）
MAX_STRIP_BYTES = 900 * 1024 * 1024  # 拼一整条时的内存上限，超了先劝退

# 输出清晰度：嵌入像素 = 排版尺寸 × hi_res，但**不超过原始像素**。
# 上限 4 是按「4K 截图 + 缩小排版」留的余量；再往上只是把 JPEG 体积推高。
MIN_HI_RES = 1.0
MAX_HI_RES = 4.0

SIZE_MODES = ("width", "height", "original")
ALIGN_MODES = ("left", "center", "right")
PAGING_MODES = ("each", "flow")

DEFAULT_PAGE_RATIO = 1.414  # 与长截图那边一致，A4 纵向


@dataclass
class Shot:
    """一张截图。image 是 (H, W, 3) uint8 的 RGB。"""

    image: np.ndarray
    label: str = ""

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    def describe(self, index: int) -> str:
        """列表里显示的一行。带序号的目的是让「顺序」这件事在界面上可见。"""
        mark = f"{index + 1:02d}"
        text = f"{mark}.  {self.width}×{self.height}"
        return f"{text}   {self.label}" if self.label else text


@dataclass
class Layout:
    """排版参数。全部是「用户能看懂的量」，没有隐藏魔法值。"""

    size_mode: str = "width"      # width=统一宽度 / height=统一高度 / original=原始
    target_px: int = 1200         # 统一后的目标边长（像素）
    align: str = "center"         # 水平对齐 left / center / right
    paging: str = "each"          # each=每图一页 / flow=连续排列
    margin_px: int = 24           # 页边距
    gap_px: int = 16              # flow 模式的图间距
    page_ratio: float = DEFAULT_PAGE_RATIO

    scan: bool = False            # 是否套扫描版效果
    scan_strength: int = 50       # 0~100

    # 输出清晰度：嵌入像素 = 排版尺寸 × hi_res，且不超过原始像素。
    # 默认 2.0，意思是「只要原图有那么多像素，就按两倍排版尺寸嵌入」——
    # 再高只是把文件推大，再低就会把 1080p 截图糟蹋成 1200px。
    hi_res: float = 2.0

    def normalized(self) -> "Layout":
        """把越界参数收进合法区间，避免界面上的手输值直接炸到渲染层。"""
        mode = self.size_mode if self.size_mode in SIZE_MODES else "width"
        align = self.align if self.align in ALIGN_MODES else "center"
        paging = self.paging if self.paging in PAGING_MODES else "each"
        return Layout(
            size_mode=mode,
            target_px=int(np.clip(self.target_px, MIN_TARGET_PX, MAX_TARGET_PX)),
            align=align,
            paging=paging,
            margin_px=int(max(0, min(400, self.margin_px))),
            gap_px=int(max(0, min(400, self.gap_px))),
            page_ratio=float(np.clip(self.page_ratio, 0.3, 5.0)),
            scan=bool(self.scan),
            scan_strength=int(np.clip(self.scan_strength, 0, 100)),
            hi_res=float(np.clip(self.hi_res, MIN_HI_RES, MAX_HI_RES)),
        )


@dataclass
class Placed:
    """排版后的一张图：实际像素 + 在画布里的水平偏移 + 排版口径的尺寸。

    三个尺寸要分清，混用一定会出错：
      · ``width`` / ``height``  实际嵌入的像素（已乘清晰度倍数）
      · ``logical_*``           96dpi 口径的排版尺寸（决定它在纸上多大）
      · ``scale``               width / logical_width
    PDF 只看前两个的**比例**，Word 用 logical_* 算 EMU —— 两个格式都只认
    「排版尺寸」，像素多了只是更清晰，不会把图撑大。
    """

    image: np.ndarray
    width: int
    height: int
    x: int = 0
    logical_width: int = 0
    logical_height: int = 0
    scale: float = 1.0


# ---------------------------------------------------------------- 尺寸对齐


def target_size(width: int, height: int, layout: Layout) -> tuple[int, int]:
    """按 size_mode 算出缩放后的像素尺寸 —— **只做算术，不碰像素**。

    单独抽出来是为了让界面上的「预计 N 页」「统一后 1200×675」能即时刷新：
    滑杆每动一下都会重新估算，如果估算里也跑一遍真实缩放，十几张 4K 截图
    就会把界面卡住。而估算和渲染共用这一个函数，两处口径就不可能对不上。
    """
    w, h = max(1, int(width)), max(1, int(height))
    if layout.size_mode == "original":
        return w, h

    target = int(layout.target_px)
    if layout.size_mode == "width":
        if w == target:
            return w, h
        new_w, new_h = target, max(1, int(round(h * target / w)))
    else:  # height
        if h == target:
            return w, h
        new_h, new_w = target, max(1, int(round(w * target / h)))

    # 拼版后单边不设上限，一张图就能把页面撑到阅读器打不开
    scale = min(1.0, MAX_SIDE_PX / max(new_w, new_h))
    if scale < 1.0:
        new_w, new_h = max(1, int(new_w * scale)), max(1, int(new_h * scale))
    return int(new_w), int(new_h)


def resize_exact(img: np.ndarray, width: int, height: int) -> np.ndarray:
    """精确缩放到给定像素，返回数组（尺寸相同则原样返回，不复制）。

    一律用 LANCZOS：截图里大量是文字，BILINEAR/NEAREST 会把小字号的笔画弄断，
    而 LANCZOS 是 Pillow 里对缩小最友好的一种（带抗锯齿的低通，没有摩尔纹）。
    """
    h, w = int(img.shape[0]), int(img.shape[1])
    width, height = max(1, int(width)), max(1, int(height))
    if (width, height) == (w, h):
        return img
    return np.asarray(Image.fromarray(img).resize((width, height), Image.LANCZOS))


def resize_to(img: np.ndarray, layout: Layout) -> np.ndarray:
    """按 size_mode 把单张图缩放到**排版尺寸**（96dpi 口径），返回数组。"""
    h, w = int(img.shape[0]), int(img.shape[1])
    new_w, new_h = target_size(w, h, layout)
    return resize_exact(img, new_w, new_h)


def resize_to_width(img: np.ndarray, width: int) -> np.ndarray:
    """按宽度等比缩放，宽度已一致时原样返回。

    界面上的「输出宽度」用它：框出来的区域大小各异，但交付出去的图可以一边宽。
    注意放大只是插值，**不会增加细节** —— 想要更清晰只能把框框得更大。
    """
    w, h = int(img.shape[1]), int(img.shape[0])
    width = max(1, int(width))
    if width == w:
        return img
    return resize_exact(img, width, max(1, int(round(h * width / w))))


def embed_scale(shot: "Shot", layout: Layout) -> float:
    """这张图按几倍于排版尺寸嵌入 —— 不超过 hi_res，也**不超过原始像素**。

    「不超过原始像素」是硬规则：光源不够时放大只是插值，看着更糊、文件更大，
    一个有用像素都不会多出来。所以导出的策略是**只许少缩、绝不虚放**：
    1080p 截图排版到 1200px 宽时，嵌入用满 1920 原生像素（1.6×）；
    4K 截图则被 hi_res 挡在 2×，免得文件平白翻倍。
    """
    lay = layout.normalized()
    logical_w, _ = target_size(shot.width, shot.height, lay)
    if logical_w <= 0:
        return MIN_HI_RES
    return float(max(MIN_HI_RES, min(lay.hi_res, shot.width / logical_w)))


def estimate_dpi(shots: Sequence["Shot"], layout: Layout) -> float:
    """这批图里**最差**那张的等效分辨率（96dpi 口径 × 嵌入倍数）。

    取最小值而不是平均值：一份文档里只要有一张虚的，整体观感就下来了，
    界面上的「清晰度」提示应当反映最坏情况。
    """
    lay = layout.normalized()
    if not shots:
        return 96.0 * lay.hi_res
    return 96.0 * min(embed_scale(s, lay) for s in shots)


# ---------------------------------------------------------------- 扫描版效果


def apply_scan(img: np.ndarray, strength: int = 50) -> np.ndarray:
    """把截图压成「扫描件」观感，返回 RGB 数组。

    简版实现，四步（都用现成的 PIL/numpy，没有额外依赖）：
      1. 转灰度 —— 彩色界面先去掉颜色，这是扫描件最直观的特征；
      2. 自动对比度 —— 把最常见的最亮/最暗各切掉一小段，压平不均匀曝光；
      3. 轻微高斯模糊 —— 扫描件经过镜头和纸张反射，边缘本来就不锐；
      4. 抬黑 + 纸色偏移 + 颗粒噪点 —— 纯白背景是「电子文档」的标志，
         真正的扫描件底色发灰发暖。

    strength 0~100 线性控制模糊半径、噪点强度和黑白抬升量。
    strength=0 时只做灰度，仍然有扫描件的底色特征但几乎无损。
    """
    s = float(np.clip(strength, 0, 100)) / 100.0
    gray = Image.fromarray(img).convert("L")
    # 切掉 0.5% 的两端：界面里的纯白工具栏会压住整幅的对比度拉伸
    gray = _autocontrast(gray, cutoff=0.5)

    radius = 0.25 + 1.1 * s          # 0.25~1.35，超过 1.5 文字就开始糊
    if radius > 0.05:
        gray = gray.filter(_gaussian_blur(radius))

    arr = np.asarray(gray).astype(np.float32)

    # 抬黑：纸面不是纯黑，最深处也应该在 20~45 之间
    lift = 8.0 + 26.0 * s
    arr = arr * (1.0 - lift / 255.0) + lift

    # 颗粒：按固定种子，保证预览和导出完全一致（否则同一个强度每次结果都不一样）
    if s > 0:
        rng = np.random.default_rng(20261026)
        arr = arr + rng.normal(0.0, 5.0 * s, size=arr.shape).astype(np.float32)

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    rgb = np.repeat(arr[:, :, None], 3, axis=2)

    # 纸色：冷灰偏一点暖黄，让它在 Word/PDF 里看起来像纸而不是屏幕
    warm = np.array([1.0, 0.995, 0.975], dtype=np.float32)
    rgb = np.clip(rgb.astype(np.float32) * warm, 0, 255).astype(np.uint8)
    return rgb


def _autocontrast(im, cutoff: float = 0.5):
    """Pillow 的 autocontrast 允许 float cutoff（部分版本只吃 int）。

    单独包一层是为了「版本兼容」这件事只出现在一个地方 —— 上层不用关心。
    """
    from PIL import ImageOps

    try:
        return ImageOps.autocontrast(im, cutoff=cutoff)
    except TypeError:
        return ImageOps.autocontrast(im, cutoff=int(round(cutoff)))


def _gaussian_blur(radius: float):
    from PIL import ImageFilter

    return ImageFilter.GaussianBlur(radius)


# ---------------------------------------------------------------- 位置对齐 + 拼版


def _logical(shots: Sequence[Shot], lay: Layout) -> list[tuple[int, int]]:
    """每张图的排版尺寸（96dpi 口径）。页数估算与真实渲染共用它。"""
    return [target_size(s.width, s.height, lay) for s in shots]


def _compose(shots: Sequence[Shot], lay: Layout, scales: Sequence[float]) -> list[Placed]:
    """按给定的嵌入倍数把每张图缩好、套滤镜、算出水平偏移。

    偏移用缩放**前**的排版尺寸算（content_w - logical_w），再乘这一张自己的
    倍数 —— 各图的密度可以不同，但对齐关系必须一致，否则「居中」会歪。
    """
    logical = _logical(shots, lay)
    if not logical:
        return []
    content_w = max(w for w, _ in logical)
    placed: list[Placed] = []
    for shot, (lw, lh), s in zip(shots, logical, scales):
        img = resize_exact(shot.image, int(round(lw * s)), int(round(lh * s)))
        if lay.scan:
            img = apply_scan(img, lay.scan_strength)
        # 宽度以 resize 后的实际值为准：PIL 内部按浮点取整，
        # 理论上可能与算术值差 1px，以实际像素为准才不会贴歪。
        w, h = int(img.shape[1]), int(img.shape[0])
        if lay.align == "left":
            x = 0
        elif lay.align == "right":
            x = int(round((content_w - lw) * s))
        else:
            x = int(round((content_w - lw) * s / 2))
        placed.append(
            Placed(
                image=img, width=w, height=h, x=max(0, x),
                logical_width=lw, logical_height=lh, scale=float(s),
            )
        )
    return placed


def prepare(shots: Sequence[Shot], layout: Layout) -> list[Placed]:
    """把所有图缩放到统一尺寸，并按 align 算出水平偏移（真实像素）。

    每张图用自己能达到的最大嵌入倍数（见 embed_scale），所以同一份文档里
    可能一张 1.6× 一张 2× —— 这在「纸上」完全看不出来（排版尺寸相同），
    只是清晰的那张多带了点像素。
    """
    lay = layout.normalized()
    return _compose(shots, lay, [embed_scale(s, lay) for s in shots])


def render_pages(shots: Sequence[Shot], layout: Layout) -> list[np.ndarray]:
    """把截图排成页面画布序列（每页都是白底 RGB）。

    each : 一张图一页。页宽 = 内容宽 + 两侧边距，页高 = 图高 + 上下边距。
           页高跟着图片走，所以任何长宽比的图都不会被拉伸、也不会被裁。
           各页的像素密度可以不同 —— 它们的**物理**尺寸由写文件层按比例决定，
           所以 PDF 里翻页不会忽大忽小。
    flow : 所有图竖排成一整条，再按 page_ratio 切成若干页。
           切页允许从一张图中间断开 —— 这正是「连续排列」在纸上的样子，
           比为了不切断而把图缩小（从而破坏尺寸统一）更合理。
           整条只能用**一个**密度，否则并排的两张图大小不一致。
    """
    lay = layout.normalized()
    placed = prepare(shots, lay)
    if not placed:
        return []

    margin = lay.margin_px

    if lay.paging == "each":
        content_w = max(p.logical_width for p in placed)
        pages = []
        for p in placed:
            s = p.scale
            page_w = int(round((content_w + 2 * margin) * s))
            page_h = int(round((p.logical_height + 2 * margin) * s))
            if page_w > MAX_SIDE_PX or page_h > MAX_SIDE_PX:
                raise ValueError(
                    f"单页 {page_w}×{page_h}px 超过上限 {MAX_SIDE_PX}px，"
                    "请降低「统一宽度/高度」或边距"
                )
            canvas = np.full((page_h, page_w, 3), 255, dtype=np.uint8)
            y0 = min(max(0, int(round(margin * s))), max(0, page_h - p.height))
            x0 = min(max(0, int(round(margin * s)) + p.x), max(0, page_w - p.width))
            canvas[y0 : y0 + p.height, x0 : x0 + p.width] = p.image
            pages.append(canvas)
        return pages

    # ---- flow：整条用一个密度（各图密度不同时降级到最小的那个） ----
    s = min(p.scale for p in placed)
    if any(abs(p.scale - s) > 1e-6 for p in placed):
        placed = _compose(shots, lay, [s] * len(placed))

    content_w = max(p.logical_width for p in placed)
    page_w = int(round((content_w + 2 * margin) * s))
    if page_w > MAX_SIDE_PX:
        raise ValueError(
            f"页宽 {page_w}px 超过上限 {MAX_SIDE_PX}px，请降低「统一宽度」或边距"
        )
    gap = int(round(lay.gap_px * s))
    margin_px = int(round(margin * s))
    total_h = sum(p.height for p in placed) + gap * (len(placed) - 1)
    strip_h = total_h + 2 * margin_px
    if strip_h > MAX_STRIP_H or strip_h * page_w * 3 > MAX_STRIP_BYTES:
        raise ValueError(
            f"连续排列总高 {strip_h}px 太大（{strip_h * page_w * 3 // 1048576}MB），"
            "请改用「每图一页」或降低输出清晰度"
        )
    strip = np.full((strip_h, page_w, 3), 255, dtype=np.uint8)
    y = margin_px
    for p in placed:
        x0 = min(max(0, margin_px + p.x), max(0, page_w - p.width))
        strip[y : y + p.height, x0 : x0 + p.width] = p.image
        y += p.height + gap
    return _split_strip(strip, page_w, lay.page_ratio)


def _split_strip(strip: np.ndarray, page_w: int, page_ratio: float) -> list[np.ndarray]:
    """把一整条竖图按页比例切页，最后不足一页的补白。"""
    page_h = max(1, int(round(page_w * page_ratio)))
    pages = []
    for top in range(0, strip.shape[0], page_h):
        chunk = strip[top : top + page_h]
        if chunk.shape[0] == page_h:
            pages.append(chunk.copy())
        else:
            pad = np.full((page_h - chunk.shape[0], strip.shape[1], 3), 255, dtype=np.uint8)
            pages.append(np.vstack([chunk, pad]))
    return pages


def estimate_pages(shots: Sequence[Shot], layout: Layout) -> int:
    """不渲染就估出页数，供界面上的「预计 N 页」用。

    与 render_pages 的口径必须一致，所以这里复用了同一套算术
    （_logical + embed_scale），而不是另写一个粗略公式 —— 两套口径迟早对不上。
    注意它**不解码像素**：滑杆每动一下都会调用，真缩放会让界面卡顿。
    """
    lay = layout.normalized()
    if not shots:
        return 0
    logical = _logical(shots, lay)
    if lay.paging == "each":
        return len(logical)

    content_w = max(w for w, _ in logical)
    s = min(embed_scale(shot, lay) for shot in shots)
    page_w = int(round((content_w + 2 * lay.margin_px) * s))
    page_h = max(1, int(round(page_w * lay.page_ratio)))
    total_h = sum(int(round(h * s)) for _, h in logical) + int(round(lay.gap_px * s)) * (
        len(logical) - 1
    )
    strip_h = total_h + 2 * int(round(lay.margin_px * s))
    return max(1, -(-strip_h // page_h))


def estimate_size(shots: Sequence[Shot], layout: Layout) -> tuple[int, int] | None:
    """统一后单张图的尺寸，给界面显示「统一后 1200 × 675 px」。

    取第一张而不是最宽的那张：宽度模式下所有宽都一样，取谁都对；
    高度模式下宽度各异，此时显示「1200 高」比显示第一张的宽度更贴近
    用户的心理预期 —— 所以返回的是 target_size 的结果，与缩放后看到的完全一致。
    """
    if not shots:
        return None
    lay = layout.normalized()
    return target_size(shots[0].width, shots[0].height, lay)
