"""按页码范围把多个 PDF 合并成一个。

为什么单独一个模块
------------------
「合并 PDF」听起来是一件事，实际有四个彼此独立的坑，任何一个都会让用户
拿到一份页数不对但看起来正常的东西：

1. **页码范围要能看懂人话**。用户写的是 ``1-20,22-40``，还可能是全角
   ``１－２０，２２－４０``、``1~20``、``1－20``、``全部``、留空。写错一个
   分隔符就少要点页，比报错更糟 —— 因为成品是"看起来对"的。
2. **越界必须挡住**。``22-40`` 落在一个只有 19 页的文件上，pypdf 会抛
   IndexError（或者说"第 40 页不存在"这种要翻源码才懂的话）。这里提前
   换成人话：「a.pdf 只有 19 页，你写了 22-40」。
3. **同一文件多段范围**要按用户写的顺序拼，不能自动排序、不能自动去重 ——
   ``1-20,22-40`` 的意图就是"跳过第 21 页"，一排序就全乱了。
4. **输出不能覆盖已有文件**。接在断点续存后面用的时候，用户往往连点两次，
   第二次把第一次的成果吃掉是最难受的失败。统一走 ``paths.unique_path``。

本模块只依赖 pypdf（纯 Python）。没有 pypdf 时给出「装一下」的明确提示，
而不是 ImportError 堆栈。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import paths

# on_progress(已完成来源数, 来源总数, 说明)
ProgressFn = Callable[[int, int, str], None]

# 页码范围里的分隔符：半角/全角逗号、分号、顿号、空格都当同一个意思
_SEP = re.compile(r"[,，;；、\s]+")
# 区间符号：- ~ ～ — – －（用户从 Word / 中文输入法里粘过来的都算）
_DASH = re.compile(r"[-~～—–－]+")
# 「全部」的几种写法
_ALL_WORDS = {"", "*", "全部", "所有", "整本", "all", "ALL", "All"}

MISSING_PYPDF_HINT = (
    "缺少 pypdf，无法读取/合并 PDF。装一下即可：\n"
    "    pip install pypdf\n"
    "（打包好的 exe 里已经内置，不需要单独安装）"
)


def _import_pypdf():
    """导入 pypdf，并把「没装」和「装了但导入链坏了」说成两句不同的话。

    为什么要分成两句：v0.10 打包时把标准库的 ``xml`` 排掉了，而
    ``pypdf/xmp.py`` 顶层有 ``from xml.dom.expatbuilder import ExpatBuilderNS``，
    于是在 exe 里 ``import pypdf`` 抛的是
    ``ModuleNotFoundError: No module named 'xml.dom.expatbuilder'``。
    原来的写法把所有异常一律翻成「缺少 pypdf，pip install 一下」——
    照着这句话装一百遍也没用，真因被自己的错误信息藏住了，只能靠猜。

    现在：真的是 pypdf 不在 → 给安装提示；其他情况 → 原样带出真实异常，
    让人一眼看到缺的是哪个模块。
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "pypdf":
            raise RuntimeError(MISSING_PYPDF_HINT) from None
        raise RuntimeError(
            f"pypdf 已经装上了，但它自己导入失败：{type(exc).__name__}: {exc}\n"
            "多半是依赖不全或环境损坏，重装一次：pip install --force-reinstall pypdf"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"pypdf 导入失败：{type(exc).__name__}: {exc}\n"
            "重装一次：pip install --force-reinstall pypdf"
        ) from exc
    return PdfReader, PdfWriter


class MergeError(ValueError):
    """输入层面的问题：页码越界、格式看不懂、文件不存在。界面直接显示这句话。"""


@dataclass(frozen=True)
class MergeItem:
    """一个待合并的来源：文件 + 页码范围（空 = 全部）。"""

    path: Path
    ranges: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "ranges", str(self.ranges or ""))


@dataclass
class MergeSource:
    """合并结果里，某一个来源实际取了哪些页（1-based，按取出顺序）。"""

    path: Path
    ranges: str
    total: int
    picked: list[int] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.path.name}（{self.ranges or '全部'}）→ {len(self.picked)} 页"


@dataclass
class MergeResult:
    out_path: Path
    pages: int
    sources: list[MergeSource]
    size_mb: float = 0.0

    def summary(self) -> str:
        head = f"合并完成：{len(self.sources)} 个来源 → {self.pages} 页"
        detail = "；".join(s.describe() for s in self.sources)
        return f"{head} · {self.size_mb:.2f} MB\n{detail}"


# ---------------------------------------------------------------- 页码范围


def _normalize(text: str) -> str:
    """全角转半角（１-２０ → 1-20），首尾空白去掉。

    NFKC 会把全角数字、全角逗号、全角波浪号统统折成半角，比手写映射表可靠。
    只对**范围表达式**做，不动文件路径（路径里的全角字符是合法的）。
    """
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def parse_pages(text: str, total: int) -> list[int]:
    """把 ``"1-20,22-40"`` 解析成 1-based 页码列表，顺序即拼接顺序。

    参数
    ----
    text : 页码范围。空字符串 / ``*`` / ``全部`` 都表示「整本」。
    total : 该 PDF 的总页数（用来判越界）。

    返回
    ----
    ``[1, 2, ..., 20, 22, ..., 40]`` 这样的列表。**重复页会被保留**
    （用户写 ``1-3,2-4`` 就是想要 1,2,3,2,3,4），不排序、不去重。

    异常
    ----
    MergeError : 格式看不懂 / 页码越界 / 起始页大于结束页。
    """
    total = int(total)
    if total <= 0:
        raise MergeError("这个 PDF 一页都没有，没法合并")

    raw = _normalize(text)
    if raw in _ALL_WORDS:
        return list(range(1, total + 1))

    picked: list[int] = []
    for token in _SEP.split(raw):
        if not token:
            continue
        if token in _ALL_WORDS - {""}:
            picked.extend(range(1, total + 1))
            continue

        if _DASH.search(token):
            parts = _DASH.split(token, maxsplit=1)
            if len(parts) != 2:
                raise MergeError(_bad_token(token))
            head, tail = parts[0].strip(), parts[1].strip()
            # 允许省略一端：5- 表示 5 到末页，-10 表示第 1 到 10 页
            start = _to_int(head, token) if head else 1
            end = _to_int(tail, token) if tail else total
            if start > end:
                raise MergeError(
                    f"「{token}」的起始页 {start} 大于结束页 {end}，"
                    f"想反着取请写成 {end}-{start}"
                )
            _check_range(start, end, total, token)
            picked.extend(range(start, end + 1))
            continue

        page = _to_int(token, token)
        _check_range(page, page, total, token)
        picked.append(page)

    if not picked:
        raise MergeError(f"页码范围「{text}」里没有解析出任何页")
    return picked


def _bad_token(token: str) -> str:
    return (
        f"看不懂页码范围里的「{token}」。"
        "写法示例：1-19 · 1-20,22-40 · 5- （第 5 页到末页）· 留空表示全部"
    )


def _to_int(text: str, token: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise MergeError(_bad_token(token)) from None


def _check_range(start: int, end: int, total: int, token: str) -> None:
    if start < 1 or end > total:
        raise MergeError(
            f"「{token}」超出范围：这个文件共 {total} 页，"
            f"页码只能是 1~{total}（你写的是 {start}~{end}）"
        )


def describe_pages(pages: Sequence[int]) -> str:
    """把页码列表压成人看的区间写法：``[1,2,3,7,9,10]`` → ``1-3,7,9-10``。"""
    nums = [int(p) for p in pages]
    if not nums:
        return "（空）"
    spans: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(spans)


# ---------------------------------------------------------------- 读页数


def read_page_count(path: str | Path) -> int:
    """读回页数；读不到返回 -1（界面显示「?」，不弹错误）。

    优先 pypdf（对任意来源的 PDF 都准）；没有它时退回 pdfwriter 的正则兜底
    —— 那条只对「对象未压缩」的 PDF 有效，也就是本工具自己写出来的那些。
    """
    p = Path(path)
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - 没装/导入链坏时走正则兜底
        from .pdfwriter import pdf_page_count

        return pdf_page_count(p)
    try:
        reader = PdfReader(str(p), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return -1
            except Exception:  # noqa: BLE001
                return -1
        return len(reader.pages)
    except Exception:  # noqa: BLE001 - 损坏 / 加密 / 不存在
        return -1


# ---------------------------------------------------------------- 合并


def merge(
    items: Sequence[tuple[Path | str, str] | MergeItem],
    out_path: str | Path,
    *,
    unique: bool = True,
    on_progress: ProgressFn | None = None,
) -> MergeResult:
    """按顺序把每个来源的指定页拼成一个新 PDF。

    参数
    ----
    items : ``[(路径, 页码范围), ...]`` 或 ``MergeItem`` 列表。顺序即页序。
    out_path : 输出路径；已存在时自动改名 ``名字(1).pdf``（``unique=False`` 可关掉）。
    on_progress : ``(已完成, 总数, 说明)``，每处理完一个来源调一次，供界面显示。

    异常
    ----
    MergeError : 输入问题（文件不存在、页码越界、加密、格式看不懂）
    RuntimeError : 写盘失败（空间不足、只读目录等），消息已翻成人话
    """
    norm: list[MergeItem] = [
        it if isinstance(it, MergeItem) else MergeItem(Path(it[0]), it[1] if len(it) > 1 else "")
        for it in items
    ]
    if not norm:
        raise MergeError("没有选择任何 PDF")

    PdfReader, PdfWriter = _import_pypdf()

    target = paths.with_suffix_for(Path(out_path), ".pdf")
    why = paths.check_target(target)
    if why:
        raise MergeError(why)

    writer = PdfWriter()
    sources: list[MergeSource] = []
    total_items = len(norm)

    for idx, item in enumerate(norm, start=1):
        path = Path(item.path)
        if not path.exists():
            raise MergeError(f"找不到文件：{path}")
        if not path.is_file():
            raise MergeError(f"这不是一个文件：{path}")
        if path.stat().st_size == 0:
            raise MergeError(f"这是个 0 字节的空文件，读不出页：{path}")

        try:
            reader = PdfReader(str(path), strict=False)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(
                f"读不了这个 PDF：{path.name}\n{type(exc).__name__}: {exc}"
            ) from exc

        if reader.is_encrypted:
            ok = False
            try:
                ok = bool(reader.decrypt(""))
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                raise MergeError(
                    f"「{path.name}」是加密 PDF，需要先用阅读器解密（去掉打开密码）再合并"
                )

        page_total = len(reader.pages)
        # 页码范围在这里解析，越界错误能直接指名道姓说是哪个文件
        try:
            picked = parse_pages(item.ranges, page_total)
        except MergeError as exc:
            raise MergeError(f"{path.name}：{exc}") from None

        source = MergeSource(
            path=path, ranges=str(item.ranges or ""), total=page_total, picked=picked
        )
        for page_no in picked:
            writer.add_page(reader.pages[page_no - 1])
        sources.append(source)

        if on_progress:
            try:
                on_progress(idx, total_items, source.describe())
            except Exception:  # noqa: BLE001 - 进度回调不许影响合并
                pass

    out = paths.with_suffix_for(Path(out_path), ".pdf")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out)) from exc
    if unique:
        out = paths.unique_path(out)

    try:
        with open(out, "wb") as fh:
            writer.write(fh)
    except OSError as exc:
        raise RuntimeError(paths.explain_os_error(exc, out)) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"写出合并结果失败：{type(exc).__name__}: {exc}") from exc

    size = out.stat().st_size
    if size == 0:
        raise RuntimeError(
            f"文件写出来是 0 字节：{out}\n"
            "通常是文件名撞上了 Windows 保留设备名（CON / PRN / AUX / NUL…），换个名字再试"
        )

    return MergeResult(
        out_path=out,
        pages=sum(len(s.picked) for s in sources),
        sources=sources,
        size_mb=size / (1024.0 * 1024.0),
    )


def parse_spec(spec: str) -> MergeItem:
    """把命令行上的 ``"a.pdf=1-19"`` 解析成 MergeItem。

    分隔符用 ``=`` 而不是 ``:`` —— Windows 路径里的 ``C:\\...`` 含冒号，
    用冒号分割会把盘符切成路径、把范围判定成文件名，很难查。
    """
    text = str(spec or "").strip()
    if not text:
        raise MergeError("空的合并项")
    if "=" in text:
        head, _, tail = text.partition("=")
        return MergeItem(Path(head.strip()), tail.strip())
    return MergeItem(Path(text), "")
