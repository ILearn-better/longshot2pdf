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
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 框选相关的实现搬到了 src/regionpick.py：两个面板都要用它，而
# `python gui.py` 下本模块的名字是 __main__，从别处 import gui 会把界面再跑一遍。
# 这里保留同名对象，既有的调用与测试（gui.select_region = ...）都不用改。
from src import paths  # noqa: E402
from src.regionpick import (  # noqa: E402
    MIN_SELECTION,
    Region,
    _coerce_region,
    _geometry,
    rect_from_points,
    select_region,
    virtual_screen_rect,
)


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


# --------------------------------------------------------------------- 工具函数


def default_out_path() -> Path:
    """默认输出到桌面；跟随 OneDrive / XDG 的桌面重定向，没有就退回主目录。

    不能直接写 ``Path.home() / "Desktop"``：OneDrive 备份会把桌面挪到
    ``~/OneDrive/Desktop``，Linux 中文环境又叫 ``~/桌面`` —— 那样写会悄悄
    存到主目录，用户按「存桌面」的预期找不到文件。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return paths.desktop_dir() / f"longshot_{stamp}.pdf"


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


# --------------------------------------------------------------------- 合并面板


def build_merge_tab(parent, *, tk, ttk, filedialog, messagebox, root) -> dict:
    """「PDF 合并」标签页：每张 PDF 一行，右边一个填页码范围的输入框。

    为什么单独一个标签页而不是塞进采集页
    ------------------------------------
    采集页在采集期间要「整块消失」（面板本身会被截进画面），而合并是采集
    **之后**的事，还得让用户一边看结果一边改页码。两件事的界面生命周期完全
    相反，挤在一起只会互相干扰。

    为什么是「每行一个输入框」而不是「选中一行再在下面填」
    ------------------------------------------------------
    需求原话就是「右侧提供输入框用于填写该 PDF 需要参与合并的页码范围」。
    一行一框的好处是一屏就能看全所有范围，不用来回点选；坏处是占地方，
    所以清单区做成可滚动的。

    返回句柄：``add_file(path)`` 给采集完成时自动登记结果用。
    """
    from src import pdfmerge

    outer = ttk.Frame(parent)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="PDF 合并（按页码范围）", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        outer,
        text=(
            "从上到下就是拼接顺序。右边填这个 PDF 要哪些页：留空 = 整本，"
            "1-19 = 前 19 页，1-20,22-40 = 跳过第 21 页。"
            "同一份文件想要多段，写在一个框里就行。"
        ),
        foreground="#666666",
        wraplength=600,
        justify="left",
    ).pack(anchor="w", pady=(2, 8))

    box = ttk.LabelFrame(outer, text=" 合并清单 ", padding=(8, 6))
    box.pack(fill="both", expand=True)

    # 表头：说明每一列是什么，也顺便固定列宽，免得输入框随文件名长短跳动
    head = ttk.Frame(box)
    head.pack(fill="x")
    ttk.Label(head, text="#", width=3, anchor="e", foreground="#888888").grid(row=0, column=0)
    ttk.Label(head, text="文件", anchor="w", foreground="#888888").grid(
        row=0, column=1, sticky="ew", padx=(6, 0)
    )
    ttk.Label(head, text="页数", width=8, anchor="e", foreground="#888888").grid(
        row=0, column=2, padx=(6, 0)
    )
    ttk.Label(head, text="页码范围", width=18, anchor="w", foreground="#888888").grid(
        row=0, column=3, padx=(8, 0)
    )
    ttk.Label(head, text="", width=11).grid(row=0, column=4)
    head.columnconfigure(1, weight=1)

    # ---- 可滚动清单 ----
    wrap = ttk.Frame(box)
    wrap.pack(fill="both", expand=True, pady=(4, 0))
    canvas = tk.Canvas(wrap, highlightthickness=0, height=210)
    scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def _sync_scroll(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_width(event) -> None:
        canvas.itemconfigure(window, width=event.width)

    inner.bind("<Configure>", _sync_scroll)
    canvas.bind("<Configure>", _sync_width)
    # 滚轮只在鼠标位于清单上方时接管，离开就解绑 —— 全局绑死会抢走
    # 其它输入框/下拉框的滚轮行为（tk 的经典坑）。
    def _on_wheel(event) -> None:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    empty_var = tk.StringVar(value="清单还是空的：截完一张会自动出现在这里，也可以点「添加 PDF…」")
    empty_lbl = ttk.Label(inner, textvariable=empty_var, foreground="#999999")

    rows: list[dict] = []
    busy = {"run": False}
    last_result: dict = {"path": None}

    # ---- 行操作 ----
    def _reindex() -> None:
        for i, row in enumerate(rows, start=1):
            row["idx"].configure(text=str(i))

    def _relayout() -> None:
        for row in rows:
            row["frame"].pack_forget()
        for row in rows:
            row["frame"].pack(fill="x", pady=1)
        if rows:
            empty_lbl.pack_forget()
        else:
            empty_lbl.pack(anchor="w", pady=8)
        _reindex()
        _validate_and_show()

    def _move(row: dict, delta: int) -> None:
        if busy["run"]:
            return
        try:
            at = rows.index(row)
        except ValueError:
            return
        to = at + delta
        if not (0 <= to < len(rows)):
            return
        rows[at], rows[to] = rows[to], rows[at]
        _relayout()

    def _remove(row: dict) -> None:
        if busy["run"]:
            return
        if row in rows:
            rows.remove(row)
        row["frame"].destroy()
        _relayout()

    def _first_error() -> str | None:
        """校验所有行，返回第一条问题（None = 全部可用）。"""
        for i, row in enumerate(rows, start=1):
            if row["count"] <= 0:
                return f"第 {i} 行「{row['path'].name}」读不出页数（文件损坏或加密）"
            text = row["ranges"].get().strip()
            try:
                pdfmerge.parse_pages(text, row["count"])
            except pdfmerge.MergeError as exc:
                return f"第 {i} 行「{row['path'].name}」：{exc}"
        return None

    def _validate_and_show() -> None:
        if busy["run"]:
            return
        err = _first_error()
        if err:
            status_var.set("✗ " + err)
            btn_merge.state(["disabled"])
        else:
            status_var.set(
                f"待合并 {len(rows)} 个来源 · 合计 "
                f"{sum(len(pdfmerge.parse_pages(r['ranges'].get(), r['count'])) for r in rows)} 页"
                if rows
                else "清单为空"
            )
            btn_merge.state(["!disabled"] if rows else ["disabled"])

    def add_file(path, ranges: str = "") -> None:
        """把一份 PDF 加进清单（采集完成后自动调用）。"""
        p = Path(path)
        count = pdfmerge.read_page_count(p)
        frame = ttk.Frame(inner)
        idx = ttk.Label(frame, width=3, anchor="e")
        shown = p.name if len(p.name) <= 34 else p.name[:31] + "…"
        name = ttk.Label(frame, text=shown, anchor="w")
        pages = ttk.Label(
            frame, text=(f"{count} 页" if count > 0 else "? 页"), width=8, anchor="e"
        )
        var = tk.StringVar(value=str(ranges or ""))
        entry = ttk.Entry(frame, textvariable=var, width=18)
        up = ttk.Button(frame, text="↑", width=3)
        down = ttk.Button(frame, text="↓", width=3)
        kill = ttk.Button(frame, text="✕", width=3)

        idx.grid(row=0, column=0)
        name.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        pages.grid(row=0, column=2, padx=(6, 0))
        entry.grid(row=0, column=3, padx=(8, 0))
        up.grid(row=0, column=4, padx=(6, 0))
        down.grid(row=0, column=5)
        kill.grid(row=0, column=6)
        frame.columnconfigure(1, weight=1)

        row = {
            "path": p, "count": count, "frame": frame, "ranges": var,
            "idx": idx, "entry": entry, "pages": pages,
        }
        up.configure(command=lambda r=row: _move(r, -1))
        down.configure(command=lambda r=row: _move(r, 1))
        kill.configure(command=lambda r=row: _remove(r))
        # 鼠标悬停时在状态行显示完整路径：文件名被截断过，得能核对是哪一个
        name.bind("<Enter>", lambda _e, q=p: hint_var.set(str(q)))
        name.bind("<Leave>", lambda _e: hint_var.set(""))
        for widget in (entry, pages):
            widget.bind("<Enter>", lambda _e, q=p: hint_var.set(str(q)))
            widget.bind("<Leave>", lambda _e: hint_var.set(""))
        var.trace_add("write", lambda *_a: _validate_and_show())

        rows.append(row)
        _relayout()
        canvas.after_idle(_sync_scroll)

    # ---- 按钮 ----
    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(10, 0))
    btn_add = ttk.Button(bar, text="添加 PDF…")
    btn_add.pack(side="left")
    btn_clear = ttk.Button(bar, text="清空")
    btn_clear.pack(side="left", padx=(8, 0))
    btn_open = ttk.Button(bar, text="打开结果", state="disabled")
    btn_open.pack(side="right")
    btn_merge = ttk.Button(bar, text="合并并保存…", style="Start.TButton")
    btn_merge.pack(side="right", padx=(0, 8))

    status_var = tk.StringVar(value="清单为空")
    hint_var = tk.StringVar(value="")
    ttk.Label(outer, textvariable=status_var, wraplength=600, justify="left").pack(
        anchor="w", pady=(10, 0)
    )
    ttk.Label(outer, textvariable=hint_var, foreground="#888888").pack(anchor="w")

    def on_add() -> None:
        if busy["run"]:
            return
        picked = filedialog.askopenfilenames(
            title="选择要合并的 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not picked:
            return
        for item in picked:
            add_file(item)

    def on_clear() -> None:
        if busy["run"]:
            return
        for row in list(rows):
            row["frame"].destroy()
        rows.clear()
        _relayout()

    def on_merge() -> None:
        if busy["run"]:
            return
        if not rows:
            messagebox.showinfo("清单是空的", "先截几张图，或点「添加 PDF…」选几份。")
            return
        err = _first_error()
        if err:
            messagebox.showerror("页码范围有问题", err)
            return

        first = rows[0]["path"]
        out = filedialog.asksaveasfilename(
            title="合并结果保存到…",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialdir=str(first.parent),
            initialfile=f"合并_{datetime.now():%Y%m%d_%H%M%S}.pdf",
        )
        if not out:
            return

        items = [(row["path"], row["ranges"].get().strip()) for row in rows]
        busy["run"] = True
        btn_merge.state(["disabled"])
        btn_add.state(["disabled"])
        btn_clear.state(["disabled"])
        status_var.set("正在合并…")

        q: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                q.put(("progress", f"准备合并 {len(items)} 个来源…"))

                def prog(done: int, total: int, note: str) -> None:
                    q.put(("progress", f"[{done}/{total}] {note}"))

                res = pdfmerge.merge(items, out, on_progress=prog)
                q.put(("done", res))
            except BaseException as exc:  # noqa: BLE001 - 连 ImportError 一起回传
                q.put(("error", exc))

        threading.Thread(target=work, daemon=True, name="longshot-merge").start()

        def poll() -> None:
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == "progress":
                        status_var.set(str(payload))
                    elif kind == "done":
                        busy["run"] = False
                        btn_add.state(["!disabled"])
                        btn_clear.state(["!disabled"])
                        btn_merge.state(["!disabled"])
                        last_result["path"] = payload.out_path
                        btn_open.state(["!disabled"])
                        status_var.set(payload.summary().replace("\n", "  ·  "))
                        messagebox.showinfo(
                            "合并完成",
                            f"{payload.pages} 页 → \n{payload.out_path}"
                            + (
                                "\n\n（目标同名，已自动加 (1) 保存，原文件没动）"
                                if payload.out_path.name != Path(out).name
                                else ""
                            ),
                        )
                        return
                    else:
                        busy["run"] = False
                        btn_add.state(["!disabled"])
                        btn_clear.state(["!disabled"])
                        btn_merge.state(["!disabled"])
                        status_var.set("合并失败")
                        messagebox.showerror(
                            "合并失败", f"{type(payload).__name__}: {payload}"
                        )
                        return
            except queue.Empty:
                pass
            root.after(120, poll)

        root.after(120, poll)

    def on_open() -> None:
        target = last_result["path"]
        if not target:
            return
        try:
            paths.open_in_default_app(target)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开文件", f"{target}\n{exc}")

    btn_add.configure(command=on_add)
    btn_clear.configure(command=on_clear)
    btn_merge.configure(command=on_merge)
    btn_open.configure(command=on_open)
    _relayout()

    return {
        "add_file": add_file,
        "rows": rows,
        "status_var": status_var,
        "validate": _first_error,
        "move": _move,
        "remove": _remove,
        "btn_add": btn_add,
        "btn_merge": btn_merge,
        "btn_open": btn_open,
        "outer": outer,
    }


# --------------------------------------------------------------------- 控制面板


def main() -> int:
    tk, ttk, filedialog, messagebox = _load_tk()
    from src import winapi

    # 与 select_region 同理：早于 tk.Tk() 才能真正生效
    winapi.enable_dpi_awareness()

    root = tk.Tk()
    base_title = "LongShot2PDF · 滚动截长图 / 连续截图整合"
    root.title(base_title)
    root.geometry("720x900")
    root.minsize(680, 720)

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
    tab_merge = ttk.Frame(nav, padding=(10, 8))
    nav.add(tab_merge, text="  PDF 合并  ")

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
    merge_panel = None  # 同上：采集完成后要把结果登记进合并清单

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
        out_path = paths.with_suffix_for(out_path, ".pdf")
        # 先做不改文件系统的预检（保留设备名 / 结尾空格 / 上级是文件），
        # 这些情况直接 open() 会静默写出 0 字节或抛出看不懂的 WinError
        why = paths.check_target(out_path)
        if why:
            messagebox.showerror("输出路径不可用", why)
            return
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "输出路径不可用", paths.explain_os_error(exc, out_path)
            )
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
            # 结果同时登记到「PDF 合并」清单：断点续存时用户手里是
            # 长图.pdf / 长图(1).pdf / 长图(2).pdf 这么几段，正好按顺序待合并。
            # 同名自动加 (1) 由 paths.unique_path 保证，这里只负责登记。
            if merge_panel is not None:
                try:
                    merge_panel["add_file"](state["pdf"])
                except Exception:  # noqa: BLE001 - 登记失败绝不能影响「已保存」这个事实
                    pass
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
            paths.open_in_default_app(pdf)
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
    # 挂到 root 上：关窗时要调它的 shutdown()（停掉 F9 轮询），
    # 版式审计脚本也能直接拿到面板句柄。
    root._shots_panel = shots_panel

    merge_panel = build_merge_tab(
        tab_merge,
        tk=tk,
        ttk=ttk,
        filedialog=filedialog,
        messagebox=messagebox,
        root=root,
    )
    root._merge_panel = merge_panel

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_region_text()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
