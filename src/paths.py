"""路径口径：默认导出位置、Windows 保留名、长路径/权限错误的可读转译。

为什么值得单独一层
------------------
「文件存哪」看起来是一行 `Path.home() / "Desktop"`，实际有四个坑：

1. **中文 Windows 的桌面不一定是 `~/Desktop`**。物理目录名通常是 Desktop，
   但启用 OneDrive 备份后会被重定向到 `~/OneDrive/Desktop`，
   企业策略还能指到网络盘。这时 `~/Desktop` 不存在，代码悄悄退回用户主目录 ——
   程序没崩，但用户按「存到桌面」的预期去桌面找，找不到文件。
   正解是问系统要：Windows 用 ``SHGetFolderPathW``，Linux 用 XDG 的
   ``XDG_DESKTOP_DIR``，macOS 没有 XDG，`~/Desktop` 就是对的。
2. **Windows 保留设备名会让写入「成功」但文件是 0 字节**。``open("CON.pdf","wb")``
   不报错，字节全进了控制台设备，磁盘上留下一个 0 字节的空壳。
   这种失败必须提前拦住，否则用户拿到一份打不开的 PDF 却毫无提示。
3. **超过 260 字符的路径**在没开长路径支持的 Windows 上抛 WinError 206，
   原始消息只说「文件名或扩展名太长」，不说「怎么办」。
4. **上级路径被同名文件占住**时抛 WinError 183（当文件已存在时无法创建），
   指向的却是那个**文件**而不是「你想建的目录」，极易误判。

本模块只做判断和转译，不碰界面、不碰 numpy，可以独立单测。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Windows 保留设备名（不分大小写，带任何扩展名都算）。CON.pdf 就是 CON。
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 导出时可以被「替换」的已有后缀。不在这个名单里的后缀一律**追加**而不是替换，
# 因为 `报告.v2` 里的 `.v2` 是版本号不是格式，替换成 .pdf 会把信息吃掉。
_KNOWN_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".word", ".w",
    ".png", ".jpg", ".jpeg", ".txt", ".zip",
}


# ---------------------------------------------------------------- 桌面目录


def _windows_desktop() -> Path | None:
    """问 Windows 要桌面真实路径（能跟住 OneDrive 重定向与企业策略）。

    ``SHGetFolderPathW`` 是 CSIDL 时代的 API，从 Win2000 一直在，且不需要
    构造 GUID（``SHGetKnownFolderPath`` 要传 16 字节的 FOLDERID，ctypes 里得
    手写结构体，为一行路径不值当）。CSIDL_DESKTOPDIRECTORY = 0x0010。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        # hwnd=None, csidl=0x10(桌面), hToken=None, dwFlags=0
        rc = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
        if rc == 0:
            got = buf.value.strip()
            if got:
                return Path(got)
    except Exception:  # noqa: BLE001 - 拿不到就走下面的兜底，不值得打扰用户
        pass
    return None


def _xdg_desktop() -> Path | None:
    """读 XDG 的桌面目录（Linux 中文环境常是 ~/桌面，不是 ~/Desktop）。"""
    if os.name == "nt":
        return None
    home = Path.home()
    cfg_home = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    cfg = Path(cfg_home) / "user-dirs.dirs"
    try:
        # 显式 utf-8：这个文件里常有非 ASCII 路径，靠 locale 猜会炸
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_DESKTOP_DIR"):
            continue
        _, _, raw = line.partition("=")
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            continue
        # 文件里写的是 $HOME/桌面 这种形式，要自己展开
        raw = raw.replace("$HOME", str(home)).replace("${HOME}", str(home))
        return Path(os.path.expanduser(raw))
    return None


def desktop_dir() -> Path:
    """用户桌面目录；实在没有（精简系统 / 无家目录）就退回主目录。

    这个函数**永远返回一个存在且可写的方向**，调用方不用再判空 ——
    退回主目录比抛异常合理：用户要的是「存到顺手的地方」，不是一条报错。
    """
    for finder in (_windows_desktop, _xdg_desktop):
        try:
            got = finder()
        except Exception:  # noqa: BLE001
            got = None
        if got is not None and got.is_dir():
            return got

    home = Path.home()
    for name in ("Desktop", "桌面"):
        cand = home / name
        if cand.is_dir():
            return cand
    return home


# ---------------------------------------------------------------- 目标校验


def reserved_reason(path: str | Path) -> str | None:
    """是不是 Windows 保留设备名；是就返回人话原因，否则 None。

    判断依据是「第一个点之前的部分」：``CON`` / ``CON.pdf`` / ``CON.tar.gz``
    在 Win32 眼里都是同一个设备，全部会被静默写进 null 设备。
    """
    if os.name != "nt":
        return None
    name = Path(path).name
    if not name:
        return None
    head = name.split(".")[0].strip().upper()
    if head in _WIN_RESERVED:
        return (
            f"「{Path(path).name}」用了 Windows 保留设备名 {head}，"
            f"系统会把它当设备而不是文件（写出来是 0 字节），请换个文件名"
        )
    return None


def check_target(path: str | Path) -> str | None:
    """导出目标路径的可行性预检。返回人话原因，没问题返回 None。

    只做**不改动文件系统**的检查：名字合法、上级可建、父目录不是文件。
    真正的写入失败仍由 OSError 兜底（见 :func:`explain_os_error`）。
    """
    p = Path(path)
    reason = reserved_reason(p)
    if reason:
        return reason
    if not p.name:
        return f"「{p}」不是有效的文件名"
    # Windows 会静默吃掉结尾的点和空格，导致实际落地路径与预期不符
    if os.name == "nt" and p.name != p.name.rstrip(" ."):
        return f"「{p.name}」结尾不能是空格或点（Windows 会自动去掉）"
    parent = p.parent
    if parent.exists() and not parent.is_dir():
        return f"上级路径「{parent}」已经是一个文件了，不能再往里存东西"
    return None


def explain_os_error(exc: BaseException, path: str | Path) -> str:
    """把写文件时最常见的几个 OSError 翻成「原因 + 怎么办」。

    原始消息多是 `[WinError 206] 文件名或扩展名太长。: 'C:\\...'` 这种，
    用户看不出该改什么。这里按错误码给出可执行的下一步。
    """
    target = Path(path)
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)

    if winerror == 206 or errno == getattr(os, "ENAMETOOLONG", None):
        return (
            f"路径太长（{len(str(target))} 字符，Windows 默认上限 260）：{target}\n"
            f"改短一点，或存到更靠外的目录（例如直接存桌面），"
            f"也可以在系统里开启「长路径支持」"
        )
    if winerror == 183:
        return (
            f"「{target.parent}」这一段路径已经被一个**文件**占住了，"
            f"没法再建同名目录：{target}\n换个目录名，或先把那个文件挪走"
        )
    if winerror == 5 or errno == getattr(os, "EACCES", None):
        return f"没有写入权限（目录只读或被策略限制）：{target}\n换个位置，或检查该目录的安全设置"
    if winerror in (32, 33) or errno == getattr(os, "EBUSY", None):
        return f"文件正被别的程序打开，写不进去：{target}\n先关掉 Word / 阅读器里打开的那一份再试"
    if winerror == 3 or errno == getattr(os, "ENOENT", None):
        return f"目录不存在且无法自动创建：{target.parent}"
    if errno == getattr(os, "ENOSPC", None):
        return f"磁盘空间不足，写不下：{target}"
    return f"{type(exc).__name__}: {exc}（目标 {target}）"


def with_suffix_for(path: str | Path, suffix: str) -> Path:
    """把路径的后缀换成 ``suffix``，但**不吃掉不像格式的后缀**。

    ``Path.with_suffix`` 的语义是「替换最后一个点之后的部分」，于是
    ``报告.v2`` → ``报告.pdf``，版本号被当成后缀吞了。
    这里的规则是：只有已知文档后缀才替换，其他一律追加 ——
    ``报告.v2`` → ``报告.v2.pdf``，用户输入的信息一点不丢。
    """
    p = Path(path)
    want = suffix if suffix.startswith(".") else "." + suffix
    if p.suffix.lower() == want.lower():
        return p
    if p.suffix.lower() in _KNOWN_SUFFIXES:
        return p.with_suffix(want)
    # 没有后缀，或者后缀不像格式（.v2 / .final）：追加而不是替换
    return p.with_name(p.name + want)


def unique_path(path: str | Path, *, max_index: int = 9999) -> Path:
    """同名文件已存在时，按 ``名字(1).pdf`` / ``名字(2).pdf`` 往后找空位。

    为什么值得单独一个函数
    ----------------------
    截长图这件事最贵的成本是用户的时间：500 页滚了半小时，最后因为
    「文件已存在」而保存失败，等于白干。而 Windows 的默认行为有两种都很糟：
    ``open(path, "wb")`` 直接覆盖（上一份被无声吃掉），
    或者某些工具弹一个「是否覆盖」把流程卡住（无人值守时直接挂死）。

    这里统一成「永不覆盖、永不失败」：存到第一个空闲编号为止。

    编号规则
    --------
    - 后缀保留：``a.pdf`` → ``a(1).pdf``，扩展名不变（否则双击打不开）；
    - 只在**最后一段扩展名**前插入序号，``报告.v2.pdf`` → ``报告.v2(1).pdf``；
    - 从 (1) 开始数，因为这是用户看得懂的写法；已存在的名字不跳号，逐个探。

    参数
    ----
    path : 期望路径
    max_index : 最多试到第几号，超了抛 OSError（防止目录里有变态数量的同名文件时死循环）

    返回
    ----
    一个**当前不存在**的路径。注意：它只保证「调用这一刻为空」，
    真正的排他创建仍由调用方 open() 完成（这里不做占位）。
    """
    p = Path(path)
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    for index in range(1, max_index + 1):
        cand = p.with_name(f"{stem}({index}){suffix}")
        if not cand.exists():
            return cand
    raise OSError(
        f"「{p.name}」已经存在 {max_index} 个同名副本，找不到可用编号：{p.parent}"
    )


# ---------------------------------------------------------------- 打开文件


def open_in_default_app(path: str | Path) -> None:
    """用系统默认程序打开文件。失败抛异常，由调用方弹提示。

    三个平台三套命令：Windows ``os.startfile``、macOS ``open``、
    Linux ``xdg-open``。直接写 `os.startfile` 在 mac/Linux 上是
    AttributeError —— 界面按钮点了没反应，比报错更难查。
    """
    target = str(Path(path))
    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(  # noqa: S603
            [opener, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"系统里找不到 {opener} 命令，没法自动打开：{target}"
        ) from None
