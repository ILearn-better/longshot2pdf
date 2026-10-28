"""页 JPEG 序列 → 多页 PDF。

关键点：**不重新编码**。
img2pdf 把 JPEG 的原始码流直接塞进 PDF 作为图像流（DCTDecode 过滤器），
像素零损失 —— 这正好呼应需求里的「保证超长截图像素清晰、不被缩放」。

为什么不能用 Pillow(Image.save) / reportlab 重画：
它们会把 JPEG 解码成像素再按自己的参数重新编码一遍，等于对同一块文字做
**二次有损压缩**，字边缘会二次发虚、体积还会变大。img2pdf 没有任何解码
步骤，输入码流 == 输出码流，这也是「不缩放、不重采样」的最后一道保障。

页序约定：`pages` 的传入顺序就是 PDF 的页序，本模块**绝不自己排序**——
排序会打乱 "0001.jpg, 0002.jpg, ..., 0010.jpg" 与 "1.jpg, 10.jpg, 2.jpg"
这类命名差异下的正确性，顺序的解释权归调用方（StreamStitcher）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

import img2pdf

from . import paths

A4_WIDTH_PT = 595.0  # A4 纵向宽度（pt）
DEFAULT_PAGE_RATIO = 1.414  # 页高 / 页宽（A4 纵向）
# PDF 规范（PDF 1.7 附录 C）里 Acrobat 支持的单页最大边长为 14400pt（= 200 英寸）。
# 超过它很多阅读器会直接拒绝打开或渲染成空白，所以必须主动收口。
PDF_MAX_PAGE_PT = 14400.0

# 数页数用的兜底正则：`/Type /Page`，但不能是 `/Type /Pages`（那是页树根）。
# 见 pdf_page_count 的说明。
_PAGE_OBJ_RE = re.compile(rb"/Type\s*/Page[^s]")


def _validate_pages(pages: Sequence[Path]) -> list[Path]:
    """校验每一页都在、且非空，并把报错定位到具体是第几页。

    不做这一步的后果：img2pdf 只会抛一句笼统的 "cannot read"，几百页里
    到底是哪一页坏了完全看不出来，排查成本极高。
    """
    if not pages:
        raise ValueError("没有可输出的页，无法生成 PDF")

    checked: list[Path] = []
    for idx, raw in enumerate(pages, start=1):
        page = Path(raw)
        if not page.exists():
            raise FileNotFoundError(f"第 {idx} 页 JPEG 不存在：{page}")
        if not page.is_file():
            raise ValueError(f"第 {idx} 页不是文件：{page}")
        if page.stat().st_size == 0:
            raise ValueError(f"第 {idx} 页 JPEG 是空文件（编码中断？）：{page}")
        checked.append(page)
    return checked


def _fit_page_size(page_w_pt: float, page_ratio: float) -> tuple[float, float]:
    """把 (页宽, 页高) 收进 PDF 规范允许的范围，返回 (w, h)。

    两步收口，顺序不能反：
    1) 先按 page_ratio 算出页高，页高超 14400pt 就等比缩小两者 ——
       等比缩放保证页内图片**不变形**（比例仍是 page_ratio）。
    2) 再兜底检查页宽本身（page_ratio < 1 的横向页可能出现页宽超限）。
    """
    if page_w_pt <= 0:
        raise ValueError(f"page_w_pt 必须为正数，收到 {page_w_pt}")
    if page_ratio <= 0:
        raise ValueError(f"page_ratio 必须为正数，收到 {page_ratio}")

    w = float(page_w_pt)
    h = w * float(page_ratio)

    if h > PDF_MAX_PAGE_PT:
        scale = PDF_MAX_PAGE_PT / h
        w *= scale
        h = PDF_MAX_PAGE_PT
    if w > PDF_MAX_PAGE_PT:  # 防御 page_ratio < 1 的极端横向配置
        scale = PDF_MAX_PAGE_PT / w
        w = PDF_MAX_PAGE_PT
        h *= scale
    return w, h


def jpegs_to_pdf(
    pages: list[Path],
    out_path: Path,
    page_w_pt: float = A4_WIDTH_PT,
    page_ratio: float = DEFAULT_PAGE_RATIO,
    *,
    unique: bool = True,
) -> Path:
    """把一串页 JPEG 合成一个多页 PDF。

    参数
    ----
    pages : 页 JPEG 路径列表，**顺序即页序**，本函数不排序。
    out_path : 输出 PDF 路径，父目录不存在会自动创建。
    page_w_pt : 页的物理宽度（pt）。固定为 A4 宽，让阅读器里显示大小稳定，
        不随截图区域的像素宽度而变。
    page_ratio : 页高 / 页宽。与切页时的像素比例保持一致，图片才不会被拉伸。
    unique : 目标已存在时自动改名为 ``名字(1).pdf`` 而不是覆盖（默认开）。
        滚动截图最贵的成本是用户时间，绝不能因为同名把上一份悄悄吃掉 ——
        见 ``paths.unique_path``。

    尺寸防爆
    --------
    PDF 单页最大边约 14400pt。若 `page_w_pt * page_ratio > 14400`，
    自动**等比**缩小 page_w_pt 直到页高合规（见 `_fit_page_size`），
    这样既不会生成打不开的 PDF，也不会让页内图片变形。

    异常
    ----
    ValueError : pages 为空 / 某页为空文件 / 尺寸参数非正。
    FileNotFoundError : 某页文件不存在（报错里带页号与路径）。
    """
    checked = _validate_pages(pages)

    out_path = Path(out_path)
    why = paths.check_target(out_path)
    if why:
        raise ValueError(why)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
    if unique:
        out_path = paths.unique_path(out_path)
    page_w, page_h = _fit_page_size(page_w_pt, page_ratio)

    # layout_fun 固定成 (宽, 高) 的定点布局：img2pdf 默认会按图片 DPI 算尺寸，
    # 那样同一次采集里不同宽度的页会物理大小不一，阅读器翻页时忽大忽小。
    layout = img2pdf.get_layout_fun((page_w, page_h))
    try:
        with open(out_path, "wb") as fh:
            img2pdf.convert(
                [str(p) for p in checked],
                layout_fun=layout,
                outputstream=fh,
            )
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
    except Exception as exc:  # noqa: BLE001 - 统一转成带页数的可读错误
        raise RuntimeError(
            f"合并 PDF 失败（{len(checked)} 页 → {out_path}）：{type(exc).__name__}: {exc}"
        ) from exc
    return out_path


def pdf_page_count(path: Path) -> int:
    """读回 PDF 的页数，供自测断言用。

    两条路，按「准 → 快」的顺序退化：

    1. 有 pypdf 就让它解析（最权威）；
    2. 没有 pypdf（打包成 exe 时它被排除掉了，省 1MB 体积）就直接数
       ``/Type /Page``。img2pdf 写的是**未压缩的对象**，页字典明文可见，
       数出来的 3 就是 3。

    第 2 条对「别家生成的 PDF」不一定成立：不少工具会把对象压进
    object stream，那时一个 ``/Type /Page`` 都扫不到。所以它只用于
    自测（本模块自己写出来的文件），不要拿去验第三方 PDF。

    返回 -1 表示「读不到」—— 文件损坏/不存在，或者两条路都没成。
    返回 -1 而不是抛异常，是为了让自测里 `pdf_page_count(p) == 3`
    这种写法能直接表达失败。
    """
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - 没装 pypdf 不算错误
        pass
    else:
        try:
            return len(PdfReader(str(path)).pages)
        except Exception:  # noqa: BLE001 - 文件损坏/加密/不存在
            return -1

    try:
        data = Path(path).read_bytes()
    except OSError:
        return -1
    # [^s] 把 /Type /Pages 挡在外面：那是页树根，不是页
    return len(_PAGE_OBJ_RE.findall(data)) or -1


def pages_total_size(pages: Iterable[Path]) -> int:
    """页 JPEG 的总字节数。

    用途：出 PDF 之前就能预估体积、检查临时目录剩余空间（踩坑清单 4.8），
    以及给 GUI 显示「已编码 XX MB」。取不到的文件按 0 计，不抛异常。
    """
    total = 0
    for page in pages:
        try:
            total += Path(page).stat().st_size
        except OSError:
            continue
    return total


def images_to_pdf(
    images,
    out_path: Path,
    *,
    page_w_pt: float = A4_WIDTH_PT,
    quality: int = 85,
    unique: bool = True,
) -> Path:
    """一组**尺寸各不相同**的图片 → 多页 PDF，每页按自己的比例出。

    与 `jpegs_to_pdf` 的区别：那个是给长截图用的，所有页共用一个 page_ratio
    （因为拼出来本来就是等宽的连续页）；连续截图整合里的每张图长宽比都不一样，
    共用比例会让 img2pdf 把图拉伸或套黑边。这里给 img2pdf 一个**按页算**的
    layout_fun，页宽固定、页高按本页像素比例算，于是：
        · 所有页物理宽度一致（阅读器里翻页不会忽大忽小）；
        · 每页高度跟着图走（不裁、不拉伸、不套边）。
    """
    import tempfile

    from PIL import Image

    images = list(images)
    if not images:
        raise ValueError("没有可输出的页，无法生成 PDF")

    out_path = Path(out_path)
    why = paths.check_target(out_path)
    if why:
        raise ValueError(why)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
    if unique:
        out_path = paths.unique_path(out_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="longshot_pages_"))
    try:
        # 局部变量不能叫 paths：会和上面 `from . import paths` 撞名，
        # 一旦遮蔽，下面 except 里的 explain_os_error 就变成「list 没有这个属性」。
        page_files = []
        for i, img in enumerate(images, start=1):
            p = tmp_dir / f"{i:04d}.jpg"
            Image.fromarray(img).save(p, "JPEG", quality=int(quality), subsampling=0)
            page_files.append(p)

        def layout(imgwidthpx, imgheightpx, ndpi):
            # img2pdf 的 layout_fun 收 (宽px, 高px, (xdpi, ydpi))，必须返回四个值：
            # (页宽, 页高, 图片绘制宽, 图片绘制高)，单位都是 pt。
            # 只用像素比例算，不用 ndpi —— 截图的 DPI 元数据不可信（各工具写得不一），
            # 拿它折算会算出离谱的页高。
            w = float(page_w_pt)
            h = w * (float(imgheightpx) / float(imgwidthpx))
            if h > PDF_MAX_PAGE_PT:      # 极长图：等比缩小，避免阅读器打不开
                scale = PDF_MAX_PAGE_PT / h
                w *= scale
                h = PDF_MAX_PAGE_PT
            if w > PDF_MAX_PAGE_PT:
                scale = PDF_MAX_PAGE_PT / w
                w = PDF_MAX_PAGE_PT
                h *= scale
            # 绘制尺寸 == 页尺寸：页比例本来就是按这张图算的，铺满即不变形
            return (w, h, w, h)

        try:
            with open(out_path, "wb") as fh:
                img2pdf.convert(
                    [str(p) for p in page_files],
                    layout_fun=layout,
                    outputstream=fh,
                )
        except OSError as exc:
            raise RuntimeError(paths.explain_os_error(exc, out_path)) from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"合并 PDF 失败（{len(page_files)} 页 → {out_path}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out_path
