#!/usr/bin/env python3
"""把 LongShot2PDF 打成单文件 exe，并尽量把它压小。

体积是被什么撑起来的（实测，未压缩）
------------------------------------
    numpy.libs/libscipy_openblas  ~36.6 MB   ← 绝对大头
    numpy/_core/_multiarray_umath   3.7 MB
    PIL/_imaging                    2.2 MB
    numpy/_core/_simd               2.1 MB
    python311.dll + tcl/tk          约 12 MB
   标准库若干                       约 6 MB

openblas 能不能删？**不能**。`_multiarray_umath.pyd` 的导入表里直接链着它，
删掉 numpy 连 import 都过不去（本项目也确实用 np.fft 做互相关）。所以
「只有一张牌」——把这堆二进制压一遍。三招叠加：

1. `--optimize 2`：剥掉所有 docstring / assert 再编译字节码。这个项目的注释
   和文档字符串写得极多，纯 py 部分能瘦一圈。
2. `--exclude-module`：把用不到的整类模块排除（pypdf、测试框架、网络栈、
   numpy 里没用到的子模块）。少一个模块就少它的依赖链。
3. UPX：对二进制做一次再压缩，openblas 这种大 DLL 收益最大。代价是杀软
   偶发误报、冷启动多几十毫秒（解压到临时目录）。

用法
----
    python tools/build_exe.py                 # 默认：不带 UPX（最稳）
    python tools/build_exe.py --upx <upx目录>  # 带 UPX（更小）
    python tools/build_exe.py --onedir        # 出目录版（启动更快，总体更大）
    python tools/build_exe.py --no-test       # 打完不跑自测
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "LongShot2PDF"

# 排除清单：每一类都写了「为什么可以排」。
# 判定标准只有一条 —— 运行期（GUI + CLI + 导出）真的用不到。
EXCLUDES = [
    # 1) 只在自测里读回 PDF 页序用，缺了会打印「跳过」
    "pypdf",
    "PyPDF2",
    # 2) 打包/安装工具链，运行时不存在也不需要
    "pip",
    "setuptools",
    "pkg_resources",
    "wheel",
    "distutils",
    "setuptools._distutils",
    "_distutils_hack",
    # 3) 测试与开发框架
    "unittest",
    "doctest",
    "pydoc",
    "pdb",
    "bdb",
    "lib2to3",
    "idlelib",
    "test",
    "tkinter.test",
    # 4) 网络栈：这个程序一个字节都不往外发
    #    注意 urllib 不能排 —— pathlib 里 `from urllib.parse import quote_from_bytes`
    #    是硬依赖，排掉以后连启动引导都过不去（报 No module named 'urllib'）。
    "ssl",
    "_ssl",
    "socket",
    "select",
    "http",
    "email",
    "urllib3",
    "requests",
    "smtplib",
    "ftplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "nntplib",
    "xmlrpc",
    "xml",
    "html",
    # 5) 并发/数据库/压缩存档：单线程跑，不写库
    "asyncio",
    "multiprocessing",
    "concurrent",
    "sqlite3",
    "dbm",
    "bz2",
    "lzma",
    "curses",
    "readline",
    # 6) numpy 里确定用不到的：只排「谁都不 import」的那几个。
    #    教训：`numpy.lib._index_tricks_impl` 里有一句 `from numpy.matrixlib import
    #    matrix`，把 matrixlib 排掉以后 import numpy 直接崩 —— 所以
    #    matrixlib / ma / linalg 这些虽然我们没直接用，也一个都不能动。
    "numpy.f2py",
    "numpy.distutils",
    "numpy.tests",
    "numpy.testing",
    "numpy.array_api",
    "numpy.random",
    # 7) Pillow 里没用到的：界面自己画，不用 Tk 桥和看图器。
    #    同样有坑：img2pdf 模块级就 `from PIL import ... ImageCms`，
    #    排掉 ImageCms 会让整个 img2pdf 导入失败（PDF 导出全废）。
    "PIL.ImageQt",
    "PIL.ImageTk",
    "PIL.ImageShow",
    "PIL.ImageGrab",
    # 只写 JPEG，_webp 编解码器用不上；PIL 的插件发现逻辑会吞掉 ImportError
    "PIL._webp",
    "PIL.WebPImagePlugin",
    # 8) _hashlib：conda 的 _hashlib.pyd 链着 libcrypto-3-x64.dll（1.6 MB），
    #    而 hashlib 只是 img2pdf 里顺手用一下。排掉以后 hashlib 自动退回
    #    内置的 _md5/_sha256 实现，功能一样，体积省一大截。
    #    （验证方式：--doctor 里 img2pdf 仍能 import 成功，说明 hashlib 没坏。）
    "_hashlib",
]


def project_version() -> str:
    """从 src/__init__.py 里读出 __version__，避免两处各写一份版本号。"""
    try:
        text = (ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0.0.0"


# ---------------------------------------------------------------- 图标


def make_icon(path: Path) -> Path:
    """画一个图标：白页 + 折叠角 + 几行灰字 + 底部橙色滚动箭头。

    为什么用代码画而不是塞一个 .ico 进仓库：二进制文件在 diff 里看不见、
    改不动，而这一张图只用 30 行代码就能说清「长截图」这个意思。
    """
    from PIL import Image, ImageDraw

    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    page = (36, 20, 220, 236)
    d.rounded_rectangle(page, radius=18, fill=(252, 252, 254, 255),
                        outline=(70, 96, 140, 255), width=7)

    # 折叠角
    d.polygon([(178, 20), (220, 62), (178, 62)], fill=(214, 224, 240, 255))
    d.line([(178, 20), (178, 62), (220, 62)], fill=(70, 96, 140, 255), width=5)

    # 正文行
    for i, y in enumerate(range(92, 168, 22)):
        w = 150 if i % 3 != 2 else 96
        d.rounded_rectangle((58, y, 58 + w, y + 9), radius=4, fill=(150, 164, 186, 255))

    # 底部滚动箭头（三根竖线 + 向下箭头），点题「继续往下滚」
    for x in (66, 128, 190):
        d.rounded_rectangle((x - 4, 180, x + 4, 208), radius=3,
                            fill=(236, 146, 42, 255))
    d.polygon([(128, 232), (100, 204), (156, 204)], fill=(236, 146, 42, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])
    return path


# ---------------------------------------------------------------- 版本资源


def make_version_file(path: Path, version: str) -> Path:
    """生成 PyInstaller 的版本资源，右键属性里才有版本号/说明。"""
    nums = [int(x) for x in re.split(r"[.\-+]", version) if x.isdigit()][:4]
    nums += [0] * (4 - len(nums))
    quad = ", ".join(str(n) for n in nums)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}),
    prodvers=({quad}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'zhoulikun'),
        StringStruct('FileDescription', 'LongShot2PDF - 滚动截长图 / 连续截图整合'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{NAME}'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', '{NAME}.exe'),
        StringStruct('ProductName', 'LongShot2PDF'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- 构建


def human(n: int) -> str:
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def conda_tk_extras() -> list[list[str]]:
    """conda 版 Python 的 tcl/tk 和 libffi 不在标准位置，得手动塞进包里。

    PyInstaller 只会顺着 `_tkinter.pyd` 的导入表去它旁边的目录找 DLL，
    而 conda 把 tcl86t.dll / tk86t.dll / ffi.dll 全放在 `Library/bin`、
    把 tcl 脚本库放在 `Library/lib/tcl8.6`。找不到的后果是：
    exe 能启动，一点「打开界面」就 `ImportError: DLL load failed`，
    而 ctypes 没了 ffi.dll 更是直接连 DPI 感知都做不了。

    顺便做一次「瘦身拷贝」：把 tcl/tk 数据目录复制到 build/tcltk 并删掉
    确定用不到的子目录（演示程序、时区库、tdbc 数据库绑定……）。单文件 exe
    每次启动都要把这堆文件解压到临时目录，删掉它们既省体积又省启动时间。

    返回 [[参数, 值], ...]，非 conda 环境返回空列表。
    """
    root = Path(sys.base_prefix)
    bin_dir = root / "Library" / "bin"
    lib_dir = root / "Library" / "lib"
    if not (bin_dir / "tcl86t.dll").exists():
        return []

    out: list[list[str]] = []
    for dll in ("tcl86t.dll", "tk86t.dll", "ffi.dll"):
        p = bin_dir / dll
        if p.exists():
            out.append(["--add-binary", f"{p}{os.pathsep}."])

    # 目标目录名必须是 _tcl_data / _tk_data：PyInstaller 的 tkinter 运行时钩子
    # 就是按这两个名字去设 TCL_LIBRARY / TK_LIBRARY 的。
    for src, dest in ((lib_dir / "tcl8.6", "_tcl_data"),
                      (lib_dir / "tk8.6", "_tk_data")):
        if not src.is_dir():
            continue
        staged = _stage_tcl_dir(src, BUILD / "tcltk" / src.name)
        out.append(["--add-data", f"{staged}{os.pathsep}{dest}"])
    return out


# 这些子目录只有「用户主动 require 某个包」时才会被读，Tk 启动与 ttk 控件用不到。
_TCL_PRUNE = (
    "demos",        # 演示程序，其中 tk8.6/demos/images 里还塞了一堆示例图
    "tzdata",       # 只给 `clock format -timezone` 用，本项目不碰时区
    "tdbc",         # Tcl 数据库绑定
    "http1.0",
    "http2.0",
    "tcltest",      # 测试包
    "opt0.4",
)


def _stage_tcl_dir(src: Path, dest: Path) -> Path:
    """把 tcl/tk 数据目录**按需复制**一份（不合格的子目录干脆不复制）。

    这里刻意不写「先整目录复制、再删掉不要的」：那是一次几百个文件的批量删除，
    既慢又容易被各种删除防护拦下来。用 ignore 从一开始就不复制，
    只写不删，天然没有这个问题。

    目标目录名带上修剪规则的指纹：规则一改就换个新目录，避免旧文件残留
    混进包里（也不去删旧目录，删除本身就是刚才想避开的动作）。
    """
    import hashlib

    tag = hashlib.md5("|".join(_TCL_PRUNE).encode()).hexdigest()[:6]
    target = dest.with_name(f"{dest.name}-{tag}")
    try:
        shutil.copytree(
            src, target,
            ignore=shutil.ignore_patterns(*_TCL_PRUNE),
            dirs_exist_ok=True,
        )
    except OSError as exc:
        print(f"  [warn] tcl/tk 瘦身拷贝失败（{exc}），改用原目录")
        return src
    print(f"  tcl/tk 瘦身：{src.name} 跳过 {len(_TCL_PRUNE)} 类子目录")
    return target


def build(*, upx_dir: Path | None, onedir: bool) -> Path:
    version = project_version()
    icon = make_icon(PKG / "app.ico")
    vfile = make_version_file(PKG / "version_info.txt", version)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        # 不加 --clean：它会去删 PyInstaller 的全局 bincache（动辄几十个文件），
        # 一是慢，二是本地构建缓存没有任何需要清的理由（缓存按文件内容取 key）。
        "--noconfirm", "--log-level", "WARN",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyi"),
        "--specpath", str(BUILD),
        "--icon", str(icon),
        "--version-file", str(vfile),
        # 关键：剥掉 docstring/assert。本项目注释极多，收益明显。
        "--optimize", "2",
        # console 而不是 windowed：GUI 模式由 run.py 自己把控制台藏起来，
        # CLI 模式则必须留着，否则脚本里调用拿不到任何输出。
        "--console",
    ]
    cmd += ["--onedir"] if onedir else ["--onefile"]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for pair in conda_tk_extras():
        cmd += pair

    # img2pdf / gui / cli 都是在函数里 import 的，PyInstaller 能扫到，
    # 但 shots_gui 是 gui.py 里延迟导入的二级模块，显式声明一次更保险。
    for mod in ("gui", "cli", "shots_gui"):
        cmd += ["--hidden-import", mod]

    if upx_dir:
        cmd += ["--upx-dir", str(upx_dir)]
    else:
        cmd += ["--noupx"]

    cmd.append(str(ROOT / "run.py"))

    print(f"构建 {NAME} v{version}（{'onedir' if onedir else 'onefile'}"
          f"{'，UPX' if upx_dir else '，无 UPX'}）…")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {proc.returncode}")
    print(f"构建完成，耗时 {time.time() - t0:.0f}s")

    exe = DIST / (f"{NAME}/{NAME}.exe" if onedir else f"{NAME}.exe")
    if not exe.exists():
        raise SystemExit(f"没找到产物：{exe}")
    return exe


def total_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _run_exe(exe: Path, arg: str, timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run([str(exe), arg], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def smoke_test(exe: Path) -> bool:
    """两道冒烟：

    1. `--selftest` —— 验证 numpy 运算、PIL 编码、img2pdf 都在包里活着；
    2. `--doctor`   —— 重点看「图形界面」那一行。conda 的 tcl/tk 在非标准位置，
       打包最容易漏的正是它：selftest 全绿但一点开界面就 DLL load failed。
       截图那一行允许 [!!]（无桌面会话里本来就抓不到），所以不参与判定。
    """
    ok = True

    print("\n冒烟 1/2：--selftest")
    code, out = _run_exe(exe, "--selftest")
    tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
    print("\n".join("    " + ln for ln in tail))
    print(f"    退出码 {code}")
    ok = ok and code == 0

    print("\n冒烟 2/2：--doctor（只看界面与依赖）")
    code, out = _run_exe(exe, "--doctor")
    for line in out.splitlines():
        if any(k in line for k in ("图形界面", "内置依赖", "mss", "numpy", "pillow", "img2pdf")):
            print(f"    {line.strip()}")
            if "图形界面" in line and "[!!]" in line:
                ok = False
    print(f"    退出码 {code}（截图不可用时非 0 属正常）")

    if not ok:
        print("\n[!!] 冒烟没过 —— 先别发这个包。")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 LongShot2PDF 为 exe")
    ap.add_argument("--upx", metavar="DIR", help="UPX 所在目录（含 upx.exe）")
    ap.add_argument("--onedir", action="store_true", help="出目录版而不是单文件")
    ap.add_argument("--no-test", action="store_true", help="打完不跑自测")
    args = ap.parse_args()

    upx_dir = Path(args.upx).resolve() if args.upx else None
    if upx_dir and not (upx_dir / "upx.exe").exists():
        raise SystemExit(f"{upx_dir} 下没有 upx.exe")

    exe = build(upx_dir=upx_dir, onedir=args.onedir)

    ok = True
    if not args.no_test:
        ok = smoke_test(exe)

    print("\n产物")
    print(f"  {exe}   {human(total_size(exe))}")
    if args.onedir:
        print(f"  目录合计 {human(total_size(exe.parent))}")

    if upx_dir:
        # 同一份代码不带 UPX 的体积，用来量化这一招到底值不值
        print("\n提示：UPX 会引入杀软误报风险，发版前在一台干净的机器上双击试一次。")

    # 中间产物（build/pyi、build/tcltk）留给用户自己处置：
    # 这里不写 rmtree —— 一次删几百个文件既慢，又容易被各种删除防护拦下来。
    print("中间产物在 build/ 下，占空间的话可以整个删掉。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
