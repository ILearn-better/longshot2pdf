#!/usr/bin/env python
"""LongShot2PDF 图形界面：全屏半透明框选 + 控制面板，全程零命令行。

为什么 tkinter 只在函数内部 import
----------------------------------
`cli.py --select` 需要 `import gui`，而 `--region` / `--fullscreen` 模式根本
不需要图形栈。如果模块顶层就 `import tkinter`，那些没编译 tkinter 的精简
Python（部分便携版、某些 conda 环境、服务器镜像）一执行 CLI 就直接崩在
import 阶段。延迟导入 + 明确报错，能把「没有图形环境」和「参数写错」两件事
分开，用户看到的是人话而不是 traceback。

坐标系统一（这条最容易踩坑）
---------------------------
tkinter 给的是**逻辑像素**，mss 给的是**物理像素**。系统缩放 125%/150% 时
两者差 1.25/1.5 倍，框选出来的区域会整体漂移、尺寸偏小。
所以必须在创建任何窗口之前调用 `winapi.enable_dpi_awareness()`（进程级声明，
声明之后 tk 的坐标就等于物理像素了）；迟于 Tk 创建再调，Win32 会直接拒绝
（E_ACCESSDENIED），漂移照样存在。
另外结果坐标一律取 `event.x_root / event.y_root`（屏幕绝对坐标），而不是
窗口内相对坐标 —— 这样即使覆盖层因为负坐标定位失败而没铺满全桌面，
返回的区域依然是对的。
"""

from __future__ import annotations

import dataclasses
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 用 typing.Tuple：别名在运行期求值，内置 tuple[...] 需要 Python 3.9+
Region = Tuple[int, int, int, int]  # (left, top, width, height) 物理像素

MIN_SELECTION = 40  # 松手时宽或高小于此值视为误触，忽略这次拖拽


# --------------------------------------------------------------------- 环境准备


def _load_tk():
    """延迟导入 tkinter，缺了给一句能看懂的错。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:  # 精简发行版可能没编译 _tkinter
        raise RuntimeError(
            "当前 Python 没有可用的 tkinter，图形界面无法启动；"
            "请改用命令行：python cli.py --region x,y,w,h --out 输出.pdf"
        ) from exc
    return tk, ttk, filedialog, messagebox


def _coerce_region(value) -> Region | None:
    """把 winapi.virtual_screen() 的返回值统一成 (l, t, w, h)（防御式兼容）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        if all(k in value for k in ("left", "top", "width", "height")):
            return tuple(int(value[k]) for k in ("left", "top", "width", "height"))  # type: ignore[return-value]
        return None
    if hasattr(value, "left") and hasattr(value, "width"):
        try:
            return (int(value.left), int(value.top), int(value.width), int(value.height))
        except Exception:  # noqa: BLE001
            return None
    try:
        parts = tuple(int(v) for v in value)
    except Exception:  # noqa: BLE001
        return None
    return parts if len(parts) == 4 else None  # type: ignore[return-value]


def virtual_screen_rect() -> Region:
    """整个虚拟桌面（多屏合并，含负坐标）。

    首选 `winapi.virtual_screen()`（AG-A 提供，能拿到左侧/上方副屏的负坐标）；
    拿不到时退回 `mss.monitors[0]` —— 注意不能退回 `GetSystemMetrics(0/1)`，
    那两个值只是主屏尺寸，副屏会被漏掉。
    """
    try:
        from src import winapi

        fn = getattr(winapi, "virtual_screen", None)
        if callable(fn):
            got = _coerce_region(fn())
            if got and got[2] > 0 and got[3] > 0:
                return got
    except Exception:  # noqa: BLE001
        pass

    try:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[0]
            return (int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"]))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"拿不到虚拟桌面尺寸：{type(exc).__name__}: {exc}") from exc


def _geometry(size: Region) -> str:
    """把 (l, t, w, h) 拼成 Tk 的 geometry 串。

    Tk 的 geometry 语法里负 x/y 表示「距屏幕右/下边缘」，不能表达副屏的负坐标。
    所以负坐标时改用无符号绝对定位 `+abs(x)+abs(y)` 再让 Win32 兜底不可行，
    这里退化为覆盖主屏起点 (0,0) —— 结果坐标仍取 x_root/y_root，不影响正确性，
    只是副屏区域可能在覆盖层之外。真机上遇到多屏偏移时，用 --region 直接给坐标更稳。
    """
    left, top, width, height = size
    if left < 0 or top < 0:
        return f"{width}x{height}+0+0"
    return f"{width}x{height}+{left}+{top}"


# --------------------------------------------------------------------- 全屏框选


def rect_from_points(x0: int, y0: int, x1: int, y1: int) -> Region | None:
    """把按下/松开两个屏幕点变成合法区域；宽或高小于下限视为误触，返回 None。

    单独抽成函数是为了能脱离鼠标事件测试这段判断（框选交互本身没法自动化）。
    坐标允许为负（副屏在主屏左侧时，x 就是负的）。
    """
    left, top = min(x0, x1), min(y0, y1)
    width, height = abs(x1 - x0), abs(y1 - y0)
    if width < MIN_SELECTION or height < MIN_SELECTION:
        return None
    return (int(left), int(top), int(width), int(height))


def select_region() -> Region | None:
    """全屏半透明覆盖层上拖拽框选，返回物理像素 (x, y, w, h)；取消返回 None。

    交互：按住左键拖 → 红框 + 亮区 + 实时尺寸；松手确认；ESC / 右键取消。
    """
    tk, _, _, _ = _load_tk()
    from src import winapi

    # 必须早于 tk.Tk()：声明 DPI 感知后 tk 的坐标才和 mss 的物理像素对齐
    winapi.enable_dpi_awareness()
    screen = virtual_screen_rect()

    root = tk.Tk()
    root.withdraw()  # 主窗口只用来承载 Toplevel，本身不显示
    overlay = None
    try:
        overlay = tk.Toplevel(root)
        overlay.overrideredirect(True)  # 无边框，铺满桌面
        overlay.geometry(_geometry(screen))
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.28)  # 半透明：既压暗桌面又能看清下面的内容
        overlay.configure(bg="#0b0b0f")

        canvas = tk.Canvas(
            overlay, bg="#0b0b0f", highlightthickness=0, bd=0, cursor="crosshair"
        )
        canvas.pack(fill="both", expand=True)

        canvas.create_text(
            24, 20,
            text="拖拽框选要滚动的区域 · 松开确认 · ESC 或右键取消",
            fill="#f0f0f0", anchor="nw", font=("Microsoft YaHei", 12, "bold"),
        )

        state = {"x0": 0, "y0": 0, "dragging": False}
        result: dict[str, Region | None] = {"region": None}

        def redraw(x0: int, y0: int, x1: int, y1: int) -> None:
            """按 canvas 局部坐标重画选框。

            用 event.x/event.y（窗口内局部坐标）而不是 x_root/y_root 来画，
            这样覆盖层实际落在屏幕哪个位置都不影响画面正确性。
            """
            canvas.delete("sel")
            left, top = min(x0, x1), min(y0, y1)
            right, bottom = max(x0, x1), max(y0, y1)
            if right - left <= 0 or bottom - top <= 0:
                return
            # 亮区：整体半透明的前提下叠一块白，视觉上就是「这块比周围亮」
            canvas.create_rectangle(
                left, top, right, bottom, outline="", fill="#ffffff", tags="sel"
            )
            canvas.create_rectangle(
                left, top, right, bottom,
                outline="#ff2d2d", width=1, tags="sel",
            )  # 1px 红框，不遮内容
            # 尺寸标签贴在选框左上角外侧；用一块深色底衬保证任何背景上都读得清
            label = f"{right - left} x {bottom - top}"
            tx, ty = left, max(2, top - 20)
            tid = canvas.create_text(
                tx + 4, ty + 2, text=label, fill="#ffd7d7", anchor="nw",
                font=("Consolas", 11, "bold"), tags="sel",
            )
            box = canvas.bbox(tid)
            if box:
                bg = canvas.create_rectangle(
                    box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1,
                    outline="", fill="#1a1a20", tags="sel",
                )
                canvas.tag_lower(bg, tid)
            canvas.tag_raise("sel")

        def on_press(event) -> None:
            state["dragging"] = True
            state["x0"], state["y0"] = event.x_root, event.y_root
            canvas.delete("sel")
            redraw(event.x, event.y, event.x, event.y)

        def on_motion(event) -> None:
            if not state["dragging"]:
                return
            x0 = state["x0"] - overlay.winfo_rootx()
            y0 = state["y0"] - overlay.winfo_rooty()
            redraw(x0, y0, event.x, event.y)

        def on_release(event) -> None:
            if not state["dragging"]:
                return
            state["dragging"] = False
            region = rect_from_points(state["x0"], state["y0"], event.x_root, event.y_root)
            if region is None:
                # 误触保护：手抖点一下就会产生 1x1 的区域，直接忽略这次拖拽，
                # 留在框选模式让用户重来（不退出、不报错）
                canvas.delete("sel")
                return
            result["region"] = region
            canvas.quit()  # 结束 mainloop

        def on_cancel(_event=None) -> None:
            result["region"] = None
            canvas.quit()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<ButtonPress-3>", on_cancel)  # 右键取消
        for widget in (overlay, canvas):
            widget.bind("<Escape>", on_cancel)

        overlay.lift()
        overlay.update_idletasks()
        # overrideredirect 窗口不会被系统自动激活，而 ESC/右键的关键是能收到键盘事件；
        # grab_set 把整个桌面的输入都收进这块覆盖层，避免用户误点到下层窗口。
        for attempt in (overlay.grab_set, overlay.focus_force, canvas.focus_set):
            try:
                attempt()
            except Exception:  # noqa: BLE001 - 个别 WM 上 grab 会失败，降级为仅置顶
                pass
        canvas.mainloop()
        return result["region"]
    finally:
        # 一定要销毁：留一个隐形全屏 topmost 窗口会挡住整个桌面，
        # 用户看到的现象是「电脑卡死了但其实鼠标能动」。
        for widget in (overlay, root):
            try:
                if widget is not None:
                    widget.destroy()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------- 工具函数


def default_out_path() -> Path:
    """默认输出到桌面；没有桌面目录（某些精简系统）就退回用户主目录。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.is_dir() else Path.home()
    return base / f"longshot_{stamp}.pdf"


def build_config(region: Region, out_path: Path, values: dict):
    """构造 CaptureConfig。

    编排层（pipeline.py）正在并行重写，字段可能增减；这里按 dataclass 的实际
    字段过滤，多出来的丢弃并提示，少了的用默认值 —— 不能因为多一个 max_pages
    就让整个界面点不动。
    """
    from src.pipeline import CaptureConfig

    desired = {"region": region, "out_path": Path(out_path), **values}
    fields = {f.name for f in dataclasses.fields(CaptureConfig)}
    ignored = sorted(set(desired) - fields)
    cfg = CaptureConfig(**{k: v for k, v in desired.items() if k in fields})
    return cfg, ignored


def _pipeline_has(param: str) -> bool:
    import inspect

    from src.pipeline import run

    try:
        return param in inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------- 控制面板


def main() -> int:
    tk, ttk, filedialog, messagebox = _load_tk()
    from src import winapi

    # 与 select_region 同理：早于 tk.Tk() 才能真正生效
    winapi.enable_dpi_awareness()

    root = tk.Tk()
    base_title = "LongShot2PDF · 滚动截长图 / 连续截图整合"
    root.title(base_title)
    root.geometry("700x820")
    root.minsize(660, 760)

    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue
    style.configure("Title.TLabel", font=("Microsoft YaHei", 13, "bold"))
    style.configure("Start.TButton", font=("Microsoft YaHei", 12, "bold"))

    # ---------------- 顶部导航 ----------------
    # 两个功能同级挂在 Notebook 上：一个负责「一屏长图 → 多页 PDF」，
    # 一个负责「多屏无关截图 → 一份 Word/PDF」。用 Notebook 而不是自绘
    # 按钮条，是因为 ttk.Notebook 天然带键盘切换（Ctrl+Tab）和焦点管理，
    # 自绘的「导航」在无障碍和键盘操作上立刻露馅。
    nav = ttk.Notebook(root)
    nav.pack(fill="both", expand=True, padx=10, pady=(10, 0))

    tab_long = ttk.Frame(nav, padding=(10, 8))
    nav.add(tab_long, text="  滚动截长图  ")
    tab_shots = ttk.Frame(nav, padding=(10, 8))
    nav.add(tab_shots, text="  连续截图整合  ")  # 与上一个同级并列

    state: dict = {
        "region": None,
        "running": False,
        "stop": threading.Event(),
        "queue": queue.Queue(),
        "started_at": 0.0,
        "pdf": None,
        "close_when_done": False,
        "hidden": False,      # 面板当前是否被最小化（采集期间）
    }

    outer = ttk.Frame(tab_long)
    outer.pack(fill="both", expand=True)
    shots_panel = None  # 由 main() 末尾填充；on_close 里要在关闭前停掉它的轮询

    ttk.Label(outer, text="滚动截长图 → 多页 PDF", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        outer,
        text="框选一块能滚动的区域，点开始后程序自动下滚、逐屏拼接、分页导出。",
        foreground="#666666",
    ).pack(anchor="w", pady=(2, 10))

    # ---------------- 区域 ----------------
    box_region = ttk.LabelFrame(outer, text=" 截取区域 ", padding=10)
    box_region.pack(fill="x")
    region_var = tk.StringVar(value="尚未框选")
    entry_region = ttk.Entry(box_region, textvariable=region_var, state="readonly")
    entry_region.pack(side="left", fill="x", expand=True)
    btn_select = ttk.Button(box_region, text="框选区域")
    btn_select.pack(side="left", padx=(8, 0))
    ttk.Label(
        box_region,
        text="提示：框选时避开右侧滚动条与固定悬浮按钮",
        foreground="#888888",
    ).pack(anchor="w", pady=(6, 0))

    # ---------------- 输出 ----------------
    box_out = ttk.LabelFrame(outer, text=" 输出文件 ", padding=10)
    box_out.pack(fill="x", pady=(10, 0))
    out_var = tk.StringVar(value=str(default_out_path()))
    entry_out = ttk.Entry(box_out, textvariable=out_var)
    entry_out.pack(side="left", fill="x", expand=True)
    btn_browse = ttk.Button(box_out, text="浏览…")
    btn_browse.pack(side="left", padx=(8, 0))

    # ---------------- 参数 ----------------
    box_param = ttk.LabelFrame(outer, text=" 采集参数（与命令行一一对应） ", padding=10)
    box_param.pack(fill="x", pady=(10, 0))
    box_param.columnconfigure(1, weight=1)

    overlap_var = tk.DoubleVar(value=0.15)
    settle_var = tk.IntVar(value=180)
    quality_var = tk.IntVar(value=85)
    ratio_var = tk.DoubleVar(value=1.414)
    strip_top_var = tk.IntVar(value=0)
    strip_bottom_var = tk.IntVar(value=0)
    max_pages_var = tk.IntVar(value=0)

    param_widgets: list = []

    def add_scale(row: int, label: str, var, lo: float, hi: float, hint: str) -> None:
        ttk.Label(box_param, text=label).grid(row=row, column=0, sticky="w", pady=3)
        value_lbl = ttk.Label(box_param, width=8, anchor="e")
        value_lbl.grid(row=row, column=2, sticky="e")
        scale = ttk.Scale(
            box_param, from_=lo, to=hi, variable=var, orient="horizontal",
            command=lambda _v, v=var, l=value_lbl: _refresh_scale_label(v, l),
        )
        scale.grid(row=row, column=1, sticky="ew", padx=8)
        param_widgets.extend([scale, value_lbl])
        ttk.Label(box_param, text=hint, foreground="#888888").grid(
            row=row, column=3, sticky="w", padx=(6, 0)
        )
        _refresh_scale_label(var, value_lbl)

    def _refresh_scale_label(var, label_widget) -> None:
        try:
            value = var.get()
        except tk.TclError:
            return
        if isinstance(var, tk.DoubleVar):
            label_widget.configure(text=f"{value:.3f}".rstrip("0").rstrip("."))
        else:
            label_widget.configure(text=str(int(round(value))))

    add_scale(0, "滚动重叠", overlap_var, 0.02, 0.60, "0.02~0.6，越小越快但重叠越少")
    add_scale(1, "稳定等待", settle_var, 0, 1000, "毫秒，滚动后等渲染完成")
    add_scale(2, "JPEG 质量", quality_var, 1, 100, "85 已肉眼无损，体积约为 95 的 40%")
    add_scale(3, "页比例", ratio_var, 0.3, 3.0, "页高/页宽，1.414 = A4 纵向")

    def add_spin(row: int, label: str, var, lo: int, hi: int, hint: str):
        ttk.Label(box_param, text=label).grid(row=row, column=0, sticky="w", pady=3)
        spin = ttk.Spinbox(box_param, from_=lo, to=hi, textvariable=var, width=8)
        spin.grid(row=row, column=1, sticky="w", padx=8)
        param_widgets.append(spin)
        ttk.Label(box_param, text=hint, foreground="#888888").grid(
            row=row, column=3, sticky="w", padx=(6, 0)
        )
        return spin

    add_spin(4, "顶部剔除", strip_top_var, 0, 10000, "像素，裁掉表头等固定条")
    add_spin(5, "底部剔除", strip_bottom_var, 0, 10000, "像素，裁掉状态栏/悬浮按钮")
    add_spin(6, "最大页数", max_pages_var, 0, 100000, "0 = 不限；>0 达到即收尾")

    # ---- 滚动方式 ----
    # 拼接是纯内容比对的，不关心位移是谁造成的，所以两种模式只差「谁滚」。
    scroll_var = tk.StringVar(value="auto")
    ttk.Label(box_param, text="滚动方式").grid(row=7, column=0, sticky="w", pady=3)
    radio_row = ttk.Frame(box_param)
    radio_row.grid(row=7, column=1, columnspan=3, sticky="w", padx=8)
    rb_auto = ttk.Radiobutton(radio_row, text="自动", value="auto", variable=scroll_var)
    rb_auto.pack(side="left")
    rb_manual = ttk.Radiobutton(radio_row, text="手动（我自己滚）", value="manual",
                                variable=scroll_var)
    rb_manual.pack(side="left", padx=(12, 0))
    param_widgets.extend([rb_auto, rb_manual])

    idle_var = tk.IntVar(value=60)
    add_spin(8, "无滚动收尾", idle_var, 0, 3600, "秒，仅手动模式；0 = 一直等（按 ESC 结束）")

    # ---- 开始时是否让开画面 ----
    # 面板只要还摆在屏幕上，就必然出现在每一帧里（尤其框选覆盖整个窗口时）。
    # 默认最小化：屏幕上干干净净，但它仍在任务栏里，点一下就能还原 ——
    # 不用 withdraw 是因为那样连任务栏入口都没了，万一 ESC 不好使就彻底没退路。
    hide_var = tk.BooleanVar(value=True)
    chk_hide = ttk.Checkbutton(
        box_param, text="开始后最小化面板", variable=hide_var,
    )
    chk_hide.grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))
    param_widgets.append(chk_hide)
    ttk.Label(
        box_param, text="避免面板被截进画面；点任务栏图标可随时还原", foreground="#888888"
    ).grid(row=9, column=3, sticky="w", padx=(6, 0))

    # ---------------- 操作 ----------------
    box_action = ttk.Frame(outer)
    box_action.pack(fill="x", pady=(12, 0))
    btn_start = ttk.Button(box_action, text="开始滚动截图", style="Start.TButton")
    btn_start.pack(side="left", ipadx=10, ipady=4)
    btn_stop = ttk.Button(box_action, text="停止（保留已采集内容）", state="disabled")
    btn_stop.pack(side="left", padx=(10, 0))

    # ---------------- 进度 ----------------
    box_prog = ttk.LabelFrame(outer, text=" 进度 ", padding=10)
    box_prog.pack(fill="both", expand=True, pady=(12, 0))
    bar = ttk.Progressbar(box_prog, mode="indeterminate")
    bar.pack(fill="x")
    status_var = tk.StringVar(value="就绪")
    # 进度 note 可能是长句（探测失败说明、固定元素告警等），必须设 wraplength，
    # 否则 Label 会按文本宽度撑开整个窗口，用户会看到界面「越跑越宽」。
    ttk.Label(box_prog, textvariable=status_var, wraplength=580, justify="left").pack(
        anchor="w", pady=(8, 0)
    )
    count_var = tk.StringVar(value="帧 0 · 页 0 · 行 0")
    ttk.Label(box_prog, textvariable=count_var, foreground="#555555").pack(anchor="w")

    def on_scroll_mode_changed(*_args) -> None:
        """切换滚动方式时改按钮文案和提示。

        文案要改是因为两种模式下「开始」之后发生的事完全不同：
        自动模式点完就不用管了，手动模式点完才是用户开始干活的时候。
        """
        if state["running"]:
            return
        if str(scroll_var.get()) == "manual":
            btn_start.configure(text="开始采集（之后你自己滚）")
            status_var.set("手动模式：开始后由你滚动页面，程序只负责抓帧拼接")
        else:
            btn_start.configure(text="开始滚动截图")
            status_var.set("就绪")

    scroll_var.trace_add("write", on_scroll_mode_changed)

    box_done = ttk.Frame(box_prog)
    box_done.pack(fill="x", pady=(10, 0))
    result_var = tk.StringVar(value="")
    ttk.Label(box_done, textvariable=result_var, foreground="#0a7a2f").pack(
        side="left", anchor="w"
    )
    btn_open = ttk.Button(box_done, text="打开 PDF", state="disabled")
    btn_open.pack(side="right")

    # ---------------- 逻辑 ----------------
    def refresh_region_text() -> None:
        region = state["region"]
        if region is None:
            region_var.set("尚未框选")
            return
        left, top, width, height = region
        region_var.set(f"left={left}  top={top}  width={width}  height={height}  （物理像素）")

    def on_select_clicked() -> None:
        if state["running"]:
            return
        try:
            region = select_region()
        except Exception as exc:  # noqa: BLE001 - 框选失败要能回到界面继续用
            messagebox.showerror("框选失败", f"{type(exc).__name__}: {exc}")
            return
        if region is None:
            status_var.set("已取消框选")
            return
        state["region"] = region
        refresh_region_text()
        status_var.set("区域已就绪，可以开始采集")

    btn_select.configure(command=on_select_clicked)

    def on_browse() -> None:
        path = filedialog.asksaveasfilename(
            title="保存 PDF 到…",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile=Path(out_var.get()).name or "longshot.pdf",
        )
        if path:
            out_var.set(path)

    btn_browse.configure(command=on_browse)

    def set_param_state(enabled: bool) -> None:
        """采集期间禁用参数区，避免运行中改参数导致「参数与实际行为不符」。"""
        widgets = list(param_widgets) + [btn_select, btn_browse, entry_out, btn_start]
        for widget in widgets:
            try:
                if isinstance(
                    widget,
                    (
                        ttk.Scale,
                        ttk.Spinbox,
                        ttk.Entry,
                        ttk.Button,
                        ttk.Radiobutton,
                        ttk.Checkbutton,
                    ),
                ):
                    widget.state(["!disabled"] if enabled else ["disabled"])
            except Exception:  # noqa: BLE001
                pass
        # 区域输入框任何时候都只读：它只用来展示框选结果，手改坐标没有意义
        entry_region.state(["!disabled", "readonly"])

    def collect_params() -> dict:
        return {
            "overlap": float(overlap_var.get()),
            "settle_ms": int(round(settle_var.get())),
            "quality": int(round(quality_var.get())),
            "page_ratio": float(ratio_var.get()),
            "strip_top": int(strip_top_var.get()),
            "strip_bottom": int(strip_bottom_var.get()),
            "max_pages": int(max_pages_var.get()),
            "max_frames": 20000,
            "keep_temp": False,
            "scroll": str(scroll_var.get() or "auto"),
            "idle_timeout": float(idle_var.get() or 0),
        }

    def worker(cfg, q: queue.Queue, stop_evt: threading.Event) -> None:
        """采集线程：**只**往队列里塞消息，绝不碰任何 tk 控件。

        tkinter 不是线程安全的，子线程里改控件轻则界面错乱、重则整个进程
        卡死在 Tcl 的 GIL 之外。所有刷新都交给主线程的 poll() 做。
        """
        try:
            from src.pipeline import run

            kwargs: dict = {
                "on_progress": lambda f, p, r, n: q.put(("progress", (f, p, r, n))),
                "should_stop": stop_evt.is_set,
            }
            if _pipeline_has("deps"):
                kwargs["deps"] = None
            res = run(cfg, **kwargs)
            q.put(("done", res))
        except BaseException as exc:  # noqa: BLE001 - 连 KeyboardInterrupt 一起回传
            q.put(("error", exc))

    def on_start() -> None:
        if state["running"]:
            return
        region = state["region"]
        if region is None:
            messagebox.showerror("还没框选", "请先点「框选区域」，在屏幕上拖出要滚动的范围。")
            return

        out_text = out_var.get().strip()
        out_path = Path(out_text) if out_text else default_out_path()
        if out_path.suffix.lower() != ".pdf":
            out_path = out_path.with_suffix(".pdf")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出路径不可用", f"{out_path.parent}\n{exc}")
            return
        out_var.set(str(out_path))

        values = collect_params()
        manual = values.get("scroll") == "manual"
        # 重叠只在自动模式下决定步长；手动模式的位移是用户给的，校验它没有意义
        if not manual and not (0.02 <= values["overlap"] <= 0.6):
            messagebox.showerror("参数不合法", "滚动重叠必须在 0.02~0.6 之间。")
            return

        try:
            cfg, ignored = build_config(region, out_path, values)
        except TypeError as exc:
            messagebox.showerror("参数不兼容", f"编排层接口与界面不一致：{exc}")
            return

        state["stop"] = threading.Event()
        state["running"] = True
        state["queue"] = queue.Queue()
        state["started_at"] = time.monotonic()
        state["pdf"] = None
        state["close_when_done"] = False

        set_param_state(False)
        btn_stop.state(["!disabled"])
        btn_open.state(["disabled"])
        result_var.set("")
        status_var.set(
            "手动模式：现在请自己滚动页面，采完按 ESC 或点「停止」"
            if manual
            else "正在检测该位置是否响应滚轮…"
        )
        count_var.set("帧 0 · 页 0 · 行 0")
        bar.start(12)

        if ignored:
            status_var.set(f"提示：pipeline 暂不支持 {', '.join(ignored)}，已忽略")

        def start_worker() -> None:
            thread = threading.Thread(
                target=worker, args=(cfg, state["queue"], state["stop"]),
                daemon=True, name="longshot-gui-run",
            )
            state["thread"] = thread
            thread.start()
            root.after(80, poll)

        def minimize_then_run() -> None:
            try:
                root.iconify()
                root.update_idletasks()
            except tk.TclError:
                pass
            # 最小化有个约 200ms 的窗口动画。动画没结束就抓首帧，
            # 会把「正在缩下去的面板」拍进第一帧 —— 后面所有帧都以首帧为基准，
            # 那一小块污点会在整页里留下一条不该有的痕迹。
            root.after(320, start_worker)

        if not bool(hide_var.get()):
            start_worker()
            return

        # 先把「要最小化了」画出来，再动手。
        # 否则用户只看到窗口一闪就没了，分不清是开始采集还是程序崩了。
        # 顺带把「接下来该干什么」写在同一句里 —— 最小化之后这句话就看不见了。
        state["hidden"] = True
        status_var.set(
            "面板即将最小化，现在请自己滚动页面；按 ESC 停止，点任务栏图标可还原"
            if manual
            else "面板即将最小化，正在自动滚动；按 ESC 停止，点任务栏图标可还原"
        )
        root.update_idletasks()
        root.after(220, minimize_then_run)

    def finish_ui(result, exc: BaseException | None = None) -> None:
        state["running"] = False
        bar.stop()

        # 面板可能还处于最小化状态：先还原，否则用户根本看不到结果，
        # 也点不到「打开 PDF」—— 采集成功却像是卡住了。
        if state.get("hidden"):
            state["hidden"] = False
            try:
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)
                # 短暂置顶把它顶到目标程序前面，再取消，避免长期压住别的窗口
                root.after(600, lambda: root.attributes("-topmost", False))
            except tk.TclError:
                pass
        try:
            root.title(base_title)
        except tk.TclError:
            pass

        set_param_state(True)
        btn_stop.state(["disabled"])
        elapsed = time.monotonic() - state["started_at"]

        if exc is not None:
            status_var.set("运行失败")
            messagebox.showerror("采集失败", f"{type(exc).__name__}: {exc}")
            return

        ok = bool(getattr(result, "ok", False))
        message = str(getattr(result, "message", "") or "")
        pages = int(getattr(result, "pages", 0) or 0)
        frames = int(getattr(result, "frames", 0) or 0)
        rows = int(getattr(result, "rows", 0) or 0)
        size_mb = float(getattr(result, "size_mb", 0.0) or 0.0)
        pdf = getattr(result, "pdf_path", None)
        stopped = bool(getattr(result, "stopped_early", False))

        if pdf is not None and pages > 0:
            state["pdf"] = Path(pdf)
            btn_open.state(["!disabled"])
            note = "已停止，" if stopped else ""
            result_var.set(
                f"{note}已保存 {pages} 页 · {frames} 帧 · {rows} 行 · {size_mb:.2f} MB · 用时 {elapsed:.1f}s"
            )
            status_var.set(("已停止，半成品已导出：" if stopped else "完成：") + str(pdf))
            count_var.set(f"帧 {frames} · 页 {pages} · 行 {rows}")
        else:
            status_var.set("未生成 PDF")
            messagebox.showerror("采集失败", message or "未知原因")

        if state["close_when_done"]:
            root.destroy()

    def poll() -> None:
        """主线程唯一的刷新入口：80ms 拉一次队列。

        为什么用队列 + after 而不是让子线程直接改控件：见 worker() 的说明。
        80ms 是视觉上「跟手」与 CPU 开销的折中（每帧刷新对人眼已足够流畅）。
        """
        try:
            while True:
                kind, payload = state["queue"].get_nowait()
                if kind == "progress":
                    frames, pages, rows, note = payload
                    count_var.set(f"帧 {frames} · 页 {pages} · 行 {rows}")
                    status_var.set(str(note))
                    # 面板最小化时看不到进度，把它写进标题：任务栏按钮上能直接读到
                    if state.get("hidden"):
                        try:
                            root.title(f"采集中 · 帧 {frames} · {pages} 页 · 按 ESC 停止")
                        except tk.TclError:
                            pass
                elif kind == "done":
                    finish_ui(payload)
                elif kind == "error":
                    finish_ui(None, payload)
        except queue.Empty:
            pass

        if state["running"]:
            root.after(80, poll)

    def on_stop() -> None:
        if not state["running"]:
            return
        state["stop"].set()
        status_var.set("正在停止…已采集的内容会导出为半成品 PDF")
        btn_stop.state(["disabled"])

    btn_start.configure(command=on_start)
    btn_stop.configure(command=on_stop)

    def on_open() -> None:
        pdf = state["pdf"]
        if not pdf:
            return
        try:
            os.startfile(str(pdf))  # noqa: S606 - 只用系统默认阅读器打开本地文件
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开 PDF", f"{pdf}\n{exc}")

    btn_open.configure(command=on_open)

    def on_close() -> None:
        if state["running"]:
            # 面板可能还最小化着：弹窗会躲在后面看不见，先还原再问
            was_hidden = bool(state.get("hidden"))
            if was_hidden:
                state["hidden"] = False
                try:
                    root.deiconify()
                    root.lift()
                except tk.TclError:
                    pass
            if not messagebox.askyesno(
                "正在采集",
                "采集尚未结束，确定要停止并退出吗？\n已采集的内容仍会尝试导出为半成品 PDF。",
            ):
                # 用户决定继续采 → 面板得重新让开，否则接下来每一帧都会拍到它
                if was_hidden:
                    try:
                        root.iconify()
                    except tk.TclError:
                        pass
                    state["hidden"] = True
                return
            state["close_when_done"] = True
            state["stop"].set()
            status_var.set("正在停止并导出半成品…")
            return
        if shots_panel is not None:
            try:
                shots_panel["shutdown"]()  # 停掉 F9 轮询，别让它继续 root.after
            except Exception:  # noqa: BLE001
                pass
        root.destroy()

    # ---------------- 第二个功能面板 ----------------
    def shots_tab_active() -> bool:
        """F9 只在「连续截图整合」标签页激活时响应。

        否则在滚动截长图跑着的时候按一下 F9，两个采集会同时抢屏幕，
        结果两边都是脏的。
        """
        try:
            return nav.index(nav.select()) == nav.index(str(tab_shots))
        except tk.TclError:
            return False

    import shots_gui  # 与 gui.py 同级；延迟导入，只有真要开界面时才需要它

    shots_panel = shots_gui.build_panel(
        tab_shots,
        tk=tk,
        ttk=ttk,
        filedialog=filedialog,
        messagebox=messagebox,
        root=root,
        can_capture=lambda: shots_tab_active() and not state["running"],
    )

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_region_text()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
