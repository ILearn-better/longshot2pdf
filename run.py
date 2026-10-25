#!/usr/bin/env python3
"""LongShot2PDF 一键启动器（只用标准库，不依赖任何第三方包）。

为什么需要它
------------
直接 `python gui.py` 会在四种情况下失败，而且报错都指向错误的方向：

1. 机器上装了好几个 Python，PATH 上那个不是"能用"的那个
   → `ModuleNotFoundError: No module named 'mss'`
   用户第一反应是「项目坏了」，其实是**解释器选错了**。
2. 用了 Microsoft Store 版 / 精简发行版 / 部分 conda 环境，没有 tkinter
   → `ModuleNotFoundError: No module named '_tkinter'`
   看起来像「少装了个包」，其实这个 Python 没带 GUI 支持，pip 装不回来。
3. Python 版本太老（<3.9）
   → 报错发生在 `Region = tuple[int, int, int, int]` 这种类型别名上，
   信息是 `TypeError: 'type' object is not subscriptable`，跟业务毫无关系。
4. 界面能开，但一张图都截不到
   → 只在真正截图时才炸，前面一切正常，最容易被当成「程序有 bug」。

所以启动器做两件事：
- **自动找到能用的解释器**（本机常见情况：PATH 上是 3.13 但没装依赖，
  真正有 tkinter 的 3.11 躺在 conda 目录里、py launcher 之外）；
- **把环境问题一次性说清**，给出可复制的修复命令。

环境健康时它是个透明的转发器，不改变 CLI/GUI 的任何行为。

用法
----
    python run.py                   # 打开图形界面（推荐，双击 start.bat 等价）
    python run.py --doctor          # 环境体检：逐个解释器 + 依赖 + 截图 + DPI
    python run.py --region 100,200,1200,900 --out 长图.pdf
    python run.py --select          # 框选后命令行采集
    python run.py --manual --select # 手动滚：你自己滚，它只负责抓帧拼接
    python run.py --venv            # 在 .venv 里重建隔离环境后启动
    python run.py --no-install      # 只诊断，不尝试装依赖
    python run.py --no-switch       # 不要自动换解释器

其余参数原样转发给 cli.py，两者是同一套参数表。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PY = (3, 9)
DEPS = (
    ("mss", "mss"),
    ("numpy", "numpy"),
    ("PIL", "pillow"),
    ("img2pdf", "img2pdf"),
)

EXIT_OK = 0
EXIT_ENV = 2       # 环境不满足，需要用户处理
EXIT_DEPS = 3      # 依赖缺失且装不上

# 防止「换了再换」的无限循环：切过一次就不再切
_SWITCH_FLAG = "LS2PDF_SWITCHED"

# 一次性把目标解释器的关键状态取回来。
# 用 subprocess 而不是在当前进程里探测：我们要能被别的解释器探测，
# 而它们既没装依赖、也可能不兼容当前语法。
_PROBE = r"""
import json, sys
info = {
    "exe": sys.executable,
    "ver": "%d.%d" % sys.version_info[:2],
    "tk": False,
    "missing": [],
}
try:
    import tkinter
    info["tk"] = True
except Exception:
    pass
for mod in ("mss", "numpy", "PIL", "img2pdf"):
    try:
        __import__(mod)
    except Exception:
        info["missing"].append(mod)
sys.stdout.write(json.dumps(info))
"""


# ---------------------------------------------------------------- 解释器探测

# 一次启动里同一个解释器会被问很多遍（列表、排序、决策各一次），
# 每次都是一个新进程（Windows 上约 100~300ms），不缓存会让启动明显发钝。
_PROBE_CACHE: dict[str, dict | None] = {}


def probe(py: str | Path, *, fresh: bool = False) -> dict | None:
    """问一个解释器：版本多少、有没有 tkinter、缺哪些依赖。"""
    key = str(py)
    if not fresh and key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    info = _probe_uncached(py)
    _PROBE_CACHE[key] = info
    return info


def _probe_uncached(py: str | Path) -> dict | None:
    try:
        proc = subprocess.run(
            [str(py), "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:  # noqa: BLE001 - 超时/无权限/坏解释器都算探测失败
        return None
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return None
    try:
        info["ver_tuple"] = tuple(int(x) for x in info["ver"].split("."))
    except Exception:  # noqa: BLE001
        info["ver_tuple"] = (0, 0)
    return info


def venv_python() -> Path | None:
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        p = ROOT / ".venv" / rel
        if p.exists():
            return p
    return None


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def candidates() -> list[Path]:
    """猜出这台机器上所有可能可用的解释器。

    来源按可信度排列：当前解释器 → 项目 venv → py launcher 注册表 →
    PATH 上的 python3/python。故意**不扫全盘**：扫盘慢且容易被用户排斥，
    而这几处已经覆盖了 Windows 上 99% 的情况。
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def add(p: str | Path | None) -> None:
        if not p:
            return
        try:
            rp = Path(p).resolve()
        except Exception:  # noqa: BLE001
            return
        if rp in seen or not rp.exists():
            return
        seen.add(rp)
        out.append(rp)

    add(sys.executable)
    add(venv_python())

    # py launcher 认识页面上看不到的安装（conda、多版本并存）
    if os.name == "nt" and shutil.which("py"):
        try:
            r = subprocess.run(
                ["py", "-0p"], capture_output=True, text=True, timeout=15
            )
            for line in (r.stdout or "").splitlines():
                m = re.search(r"([A-Za-z]:\\[^\r\n]*?python\.exe)", line, re.I)
                if m:
                    add(m.group(1))
        except Exception:  # noqa: BLE001
            pass

    for name in ("python3", "python"):
        add(shutil.which(name))

    return out


def rank(info: dict, need_tk: bool) -> tuple:
    """给候选解释器打分，越小越好。

    排序意图：版本达标 > 不缺点东西 > 依赖齐全。
    tkinter 只在需要图形界面时才算硬条件 —— 命令行模式不需要它，
    因为它的缺失去否决一个本来能用的解释器是没道理的。
    """
    ver_ok = info["ver_tuple"] >= MIN_PY
    tk_ok = info["tk"] or not need_tk
    return (not ver_ok, not tk_ok, len(info["missing"]))


def pick(*, need_tk: bool) -> dict | None:
    """挑一个能用的解释器；没有就返回 None。"""
    best: dict | None = None
    best_key: tuple | None = None
    for py in candidates():
        info = probe(py)
        if info is None:
            continue
        key = rank(info, need_tk)
        if best_key is None or key < best_key:
            best, best_key = info, key
    return best


def usable(info: dict | None, *, need_tk: bool, allow_missing_deps: bool) -> bool:
    if info is None:
        return False
    if info["ver_tuple"] < MIN_PY:
        return False
    if need_tk and not info["tk"]:
        return False
    if not allow_missing_deps and info["missing"]:
        return False
    return True


# ---------------------------------------------------------------- 体检报告


def tk_can_open() -> tuple[bool, str]:
    """tkinter 能不能真的建出窗口。

    与 probe 里的 import 检查分开：import 成功但 Tk() 失败通常意味着
    无图形会话（远程/服务进程），换解释器没用，得回到有桌面的会话。
    """
    try:
        import tkinter
    except Exception as exc:  # noqa: BLE001
        return False, f"未编译 tkinter（{type(exc).__name__}）"
    try:
        root = tkinter.Tk()
        root.withdraw()
        ver = f"Tk {tkinter.TkVersion}"
        root.destroy()
        return True, ver
    except Exception as exc:  # noqa: BLE001
        return False, f"有 tkinter 但无法创建窗口（{type(exc).__name__}）"


def screen_status() -> tuple[bool, str]:
    """mss 能不能真的截到一帧。

    这是「前面全绿、一开始采集就崩」的最后一道检查。抓 1x1 像素即可。
    """
    try:
        import mss
    except Exception:  # noqa: BLE001
        return False, "mss 不可用（依赖未安装）"
    try:
        # 与 src/capture.py 一致：mss 10.2 起 mss.mss() 已弃用
        factory = getattr(mss, "MSS", None) or getattr(mss, "mss")
        with factory() as sct:
            mon = sct.monitors[0]
            sct.grab({"left": mon["left"], "top": mon["top"], "width": 1, "height": 1})
        return True, f"虚拟桌面 {mon['width']}x{mon['height']}"
    except Exception as exc:  # noqa: BLE001
        return False, f"抓屏失败（{type(exc).__name__}），通常是没有交互式桌面"


def doctor() -> int:
    me = probe(sys.executable) or {
        "exe": sys.executable,
        "ver": "%d.%d" % sys.version_info[:2],
        "ver_tuple": sys.version_info[:2],
        "missing": [m for m, _ in DEPS if not _has(m)],
        "tk": _has("tkinter"),
    }

    print("LongShot2PDF 环境体检")
    print("=" * 60)
    print("当前解释器")
    print(f"  路径 {sys.executable}")
    print(f"  版本 {sys.version.split()[0]}（需要 >= {MIN_PY[0]}.{MIN_PY[1]}）")
    print(f"  虚拟环境 {'是' if in_venv() else '否'}")
    print(f"  依赖 {'齐全' if not me['missing'] else '缺 ' + '、'.join(me['missing'])}")

    # 把机器上看得到的解释器全列出来 —— 这是排障时最有价值的一张表
    print()
    print("机器上的解释器")
    for py in candidates():
        info = probe(py)
        if info is None:
            print(f"  [ 无法探测 ] {py}")
            continue
        ver_ok = info["ver_tuple"] >= MIN_PY
        marks = []
        marks.append(f"{info['ver']:<6}")
        marks.append("tk" if info["tk"] else "--")
        marks.append("依赖齐" if not info["missing"] else "缺" + str(len(info["missing"])))
        best = ""
        try:
            if Path(py).resolve() == Path(sys.executable).resolve():
                best = "  <- 当前"
        except Exception:  # noqa: BLE001
            pass
        flag = "OK" if (ver_ok and info["tk"] and not info["missing"]) else "  "
        print(f"  [{flag}] {' '.join(m for m in marks):<22}{py}{best}")

    print()
    tk_ok, tk_msg = tk_can_open()
    print(f"{'[OK]' if tk_ok else '[!!]'} 图形界面  {tk_msg}")

    sc_ok, sc_msg = screen_status()
    print(f"{'[OK]' if sc_ok else '[!!]'} 截图能力  {sc_msg}")

    files_ok = all((ROOT / n).exists() for n in ("cli.py", "gui.py", "src/pipeline.py"))
    print(f"{'[OK]' if files_ok else '[!!]'} 项目文件  {'完整' if files_ok else '缺失'}")

    best = pick(need_tk=True)
    print("=" * 60)
    if files_ok and tk_ok and sc_ok and not me["missing"]:
        print("一切正常：双击 start.bat，或运行 python run.py。")
        return EXIT_OK

    print("需要处理：")
    if me["missing"]:
        print("  · 依赖缺失：" + "、".join(me["missing"]))
        print("    python run.py           会自动安装（加 --no-install 只看不装）")
    if not tk_ok:
        tips = [f"    {p}" for p in candidates() if (probe(p) or {}).get("tk")]
        print("  · 图形界面不可用；装了 tkinter 的解释器有：")
        print("\n".join(tips) if tips else "    （没找到任何带 tkinter 的解释器）")
        print("    换用它重建环境：python run.py --venv")
        print("    或继续用命令行：python run.py --select --out 长图.pdf")
    if not sc_ok:
        print("  · 截屏不可用：确认当前会话有可见桌面（远程桌面/服务进程里跑不了）")
    if best and not usable(best, need_tk=True, allow_missing_deps=False):
        print(f"  · 最佳候选 {best['exe']} 还缺：{'、'.join(best['missing'])}")
        print("    python run.py           交给启动器自动处理（推荐）")
    return EXIT_ENV


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 装依赖


def install_deps() -> bool:
    """在当前解释器里装缺失依赖，返回是否装成功。"""
    req = ROOT / "requirements.txt"
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    print(f"缺少依赖，正在为以下解释器安装（只需一次）：")
    print(f"  {sys.executable}")
    print("  " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd)
    except Exception as exc:  # noqa: BLE001
        print(f"无法启动 pip：{exc}")
        return False

    left = [m for m, _ in DEPS if not _has(m)]
    if proc.returncode == 0 and not left:
        print("依赖安装完成。")
        return True

    print()
    print("自动安装失败。常见原因和对应做法：")
    print("  1) 网络访问 PyPI 受限 —— 换国内镜像重试：")
    print(f"     \"{sys.executable}\" -m pip install -r requirements.txt "
          "-i https://pypi.tuna.tsinghua.edu.cn/simple")
    print("  2) 这个 Python 不允许装包（系统级/受管环境）—— 建隔离环境：")
    print("     python run.py --venv")
    if left:
        print(f"  仍缺少：{'、'.join(left)}")
    return False


# ---------------------------------------------------------------- venv


def ensure_venv(target: Path, *, force_recreate: bool = False) -> Path | None:
    """在 target 建（或修复）隔离环境并装好依赖，返回其解释器路径。"""
    py = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    reuse = py.exists() and not force_recreate
    if reuse:
        info = probe(py, fresh=True)
        # venv 是从某个 base 解释器克隆出来的：base 没有 tkinter，venv 就永远没有。
        # 这种环境 pip 修不好，只能重建 —— 用 venv --clear 而不是删目录。
        if info is None or info["ver_tuple"] < MIN_PY or not info["tk"]:
            reuse = False

    if not reuse:
        verb = "重建" if py.exists() else "创建"
        print(f"正在{verb}虚拟环境：{target}")
        cmd = [sys.executable, "-m", "venv"]
        if py.exists():
            cmd.append("--clear")
        proc = subprocess.run([*cmd, str(target)])
        if proc.returncode != 0 or not py.exists():
            print("创建虚拟环境失败。")
            print("  Debian/Ubuntu 需要先装：sudo apt install python3-venv")
            return None

    print("正在为虚拟环境安装依赖（只需一次，之后启动很快）…")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
    )
    if proc.returncode != 0:
        print()
        print("依赖安装失败。若网络受限可加镜像：")
        print(f"  \"{py}\" -m pip install -r requirements.txt "
              "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return None
    return py


def relaunch(py: str | Path) -> int:
    """用指定解释器重新执行本脚本（参数不变），返回其退出码。"""
    # 先把本进程的说明文字刷出去：stdout 接管道/重定向时是块缓冲的，
    # 不刷的话子进程的输出会先落地，用户看到的顺序是反的（先看到错误，后看到原因）。
    sys.stdout.flush()
    sys.stderr.flush()
    env = dict(os.environ)
    env[_SWITCH_FLAG] = "1"
    return subprocess.run(
        [str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env
    ).returncode


# ---------------------------------------------------------------- 启动


def launch_gui() -> int:
    tk_ok, tk_msg = tk_can_open()
    if not tk_ok:
        print("图形界面无法启动：" + tk_msg)
        print()
        print("命令行照样能干活：")
        print("  python run.py --select --out 长图.pdf      # 全屏框选后采集")
        print("  python run.py --region 100,200,1200,900 --out 长图.pdf")
        print("  python run.py --help                       # 全部参数")
        print()
        print("完整环境报告：python run.py --doctor")
        return EXIT_ENV
    try:
        import gui
    except Exception as exc:  # noqa: BLE001
        print(f"加载界面失败：{type(exc).__name__}: {exc}")
        return EXIT_ENV
    return gui.main()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    switched = bool(os.environ.get(_SWITCH_FLAG))

    # ---- 显式建 venv ----
    if "--venv" in argv:
        i = argv.index("--venv")
        target = ROOT / ".venv"
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            target = Path(argv[i + 1]).expanduser().resolve()
            del argv[i : i + 2]
        else:
            del argv[i]
        py = ensure_venv(target, force_recreate=True)
        if py is None:
            return EXIT_DEPS
        return relaunch(py)

    if argv[:1] in (["--doctor"], ["-d"]):
        return doctor()

    no_install = "--no-install" in argv
    no_switch = "--no-switch" in argv
    argv = [a for a in argv if a not in ("--no-install", "--no-switch")]

    want_gui = not argv or argv[:1] == ["--gui"]
    if argv[:1] == ["--gui"]:
        argv = argv[1:]

    # ---- 1. 项目里已有 .venv，只要它真能用就直接进去 ----
    # 放在最前面是有意的：双击行为的可复现性来自「每次都进同一个环境」，
    # 而不是每次重新猜解释器。坏掉的 .venv 不进去，留给第 4 步重建。
    vpy = venv_python()
    if vpy is not None and not in_venv() and not switched:
        if usable(probe(vpy), need_tk=want_gui, allow_missing_deps=True):
            return relaunch(vpy)

    # ---- 2. 当前解释器连硬条件都不满足（版本低 / 要界面却没 tkinter）→ 换一个 ----
    me = probe(sys.executable)
    if not switched and not no_switch:
        if not usable(me, need_tk=want_gui, allow_missing_deps=True):
            alt = pick(need_tk=want_gui)
            try:
                same = alt is None or (
                    Path(alt["exe"]).resolve() == Path(sys.executable).resolve()
                )
            except Exception:  # noqa: BLE001
                same = True
            if alt is not None and not same:
                why = []
                if me is None or me["ver_tuple"] < MIN_PY <= alt["ver_tuple"]:
                    why.append("版本符合")
                if want_gui and alt["tk"]:
                    why.append("有 tkinter")
                print(f"当前解释器不适用（{sys.version.split()[0]}"
                      f"{'，缺 ' + '、'.join(me['missing']) if me and me['missing'] else ''}"
                      f"{'，无 tkinter' if me and not me['tk'] else ''}）")
                print(f"改用 {alt['exe']}（{'、'.join(why) or '更适合'}）")
                return relaunch(alt["exe"])

    # ---- 3. 版本兜底 ----
    if sys.version_info[:2] < MIN_PY:
        print(f"Python 版本过低：{sys.version.split()[0]}，需要 >= {MIN_PY[0]}.{MIN_PY[1]}")
        print("请安装新版 Python：https://www.python.org/downloads/")
        return EXIT_ENV

    # ---- 4. 依赖 ----
    miss = [m for m, _ in DEPS if not _has(m)]
    if miss:
        print(f"缺少依赖：{'、'.join(miss)}")
        if no_install:
            print()
            print("先解决依赖再启动。查看环境：python run.py --doctor")
            return EXIT_DEPS
        if in_venv():
            ok = install_deps()          # 隔离环境里装包是天经地义
        else:
            # 在用户的 base 环境里装包会污染 conda/系统 Python，
            # 先建项目自己的 .venv；建不出来才退回直接安装。
            built = ensure_venv(ROOT / ".venv")
            if built is not None:
                return relaunch(built)
            print("改用当前解释器直接安装。")
            ok = install_deps()
        if not ok:
            print()
            print("先解决依赖再启动。查看环境：python run.py --doctor")
            return EXIT_DEPS

    if not argv:
        return launch_gui()

    # 其余参数原样交给 cli，启动器不重复实现一套解析
    sys.path.insert(0, str(ROOT))
    import cli

    return cli.main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130)
