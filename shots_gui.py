"""「连续截图整合」面板：截取 → 追加 → 尺寸对齐 → 位置对齐 → 扫描效果 → 导出。

为什么单独一个模块
------------------
`gui.py` 已经有 800 多行，而两个面板的生命周期几乎不重叠：滚动截长图是
「一次长任务」（线程 + 最小化 + 进度队列），连续截图是「一串短动作」
（按一次抓一屏、调参数、导出）。混在一个函数里，两边状态机迟早互相踩。
拆开之后 gui.py 只负责把两个标签页挂上去。

交互约定（与需求的六条一一对应）
------------------------------
1) **两种触发**：面板上的「截取当前屏」按钮 + 全局 F9。
   「全局」是关键：抓屏前面板会把自己最小化，那时 tkinter 的 bind 收不到
   任何按键，所以用 GetAsyncKeyState 轮询物理键状态（与项目里 ESC 中断
   同一套机制）。焦点停在浏览器上照样生效，这才是连续截图的真实用法。
2) **自动追加**：每张 append 到列表末尾，序号 01/02/03 就是文档顺序。
   列表里能看见顺序，删除只删选中的那张，其余顺序不变。
3) **尺寸对齐**：统一宽度 / 统一高度 / 保持原始 + 目标边长（ps）。
4) **位置对齐**：水平 左/中/右 + 排布 每图一页/连续排列 + 页边距/图间距。
5) **扫描效果**：开关 + 强度滑杆；随机种子固定，所以预览与导出完全一致。
6) **一键导出**：Word 或 PDF 各一个按钮，完成后可直接打开。

状态展示（不是只打日志，按钮本身要变）
------------------------------------
    空闲（0 张）  截取可用；删除/清空/导出 置灰；提示「按 F9 开始」
    就绪（N 张）  导出可用；摘要「共 N 张 · 预计 M 页 · 统一后 1200×675」
    抓取中        截取按钮变「抓取中…」并禁用（防连点把同一屏抓两次）
    导出中        两个导出按钮同时禁用，进度条跑起来
    失败          弹出原因 + 红色状态，已截内容一张不丢
另外两个「参数联动」也在界面上表达出来：选「保持原始」时目标边长置灰，
选「每图一页」时图间距置灰 —— 参数不参与排版时就该看起来不参与。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from src.shots import (
    DEFAULT_PAGE_RATIO,
    Layout,
    Shot,
    estimate_pages,
    estimate_size,
)
from src.shots_export import default_name, export_document, normalize_format

HOTKEY_LABEL = "F9"          # 与 winapi.VK_F9 对应
POLL_MS = 60                 # 轮询 F9 的间隔；60ms 人感觉不到延迟，开销可忽略
ICONIFY_SETTLE_MS = 320      # 最小化动画约 200ms，等它画完再抓
COOLDOWN_S = 0.35            # 两次抓屏的最小间隔，挡住按键自动重复
TARGET_MIN, TARGET_MAX = 200, 8000


def _desktop_dir() -> Path:
    """默认导出目录：桌面，没有就退回用户主目录。"""
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def screen_rect() -> tuple[int, int, int, int]:
    """虚拟桌面范围（物理像素，多屏合并，允许负坐标）。

    这里没有复用 `gui.virtual_screen_rect`：`python gui.py` 运行时 gui 的
    模块名是 `__main__`，`import gui` 会把整个界面再执行一遍（多一个 Tk
    实例、多一个主循环）。为省十几行而埋这个坑不值得，所以就地实现。
    """
    from src import winapi

    try:
        left, top, width, height = winapi.virtual_screen()
        if int(width) > 0 and int(height) > 0:
            return (int(left), int(top), int(width), int(height))
    except Exception:  # noqa: BLE001 - 拿不到就用 mss 兜底
        pass

    import mss

    factory = getattr(mss, "MSS", None) or getattr(mss, "mss")
    with factory() as sct:
        mon = sct.monitors[0]
        return (
            int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])
        )


def grab_screen():
    """抓整个屏幕，返回 (H, W, 3) uint8 RGB。"""
    from src.capture import Grabber

    rect = screen_rect()
    with Grabber() as grabber:
        return grabber.grab(rect)


def build_panel(
    parent,
    *,
    tk,
    ttk,
    filedialog,
    messagebox,
    root,
    can_capture=None,
) -> dict:
    """在 parent 里建好整个面板，返回控制句柄。

    can_capture : 可选的无参回调，返回 False 时全局快捷键不响应。
        用来避免「滚动截长图正在跑的时候按 F9 又去抓屏」——
        两个采集同时抢屏幕只会互相污染。
    """
    ok_to_capture = can_capture or (lambda: True)

    outer = ttk.Frame(parent)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="连续截图整合 → Word / PDF", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        outer,
        text=(
            f"翻一屏按一次 {HOTKEY_LABEL}（焦点不用切回来），每张按顺序追加；"
            "全部截完再统一调版式、一键导出。"
        ),
        foreground="#666666",
        wraplength=600,
        justify="left",
    ).pack(anchor="w", pady=(2, 10))

    st: dict = {
        "shots": [],
        "busy": False,
        "queue": queue.Queue(),
        "f9_down": False,
        "hidden": False,
        "last_capture": 0.0,
        "last": None,
        "closed": False,
        "tick_id": None,
        "refresh_id": None,
    }

    def _safe_int(var, default: int) -> int:
        """读整数控件。Spinbox/Entry 里可能是一段非法中间态（如空串、'-'），
        直接 int(var.get()) 会抛 TclError 把整个回调打断 —— 退回默认值即可。"""
        try:
            return int(round(float(var.get())))
        except Exception:  # noqa: BLE001
            return default

    # ------------------------------------------------------------ 截取
    box_grab = ttk.LabelFrame(outer, text=" 截取 ", padding=10)
    box_grab.pack(fill="x")

    row_grab = ttk.Frame(box_grab)
    row_grab.pack(fill="x")
    btn_capture = ttk.Button(
        row_grab, text=f"截取当前屏（{HOTKEY_LABEL}）", style="Start.TButton"
    )
    btn_capture.pack(side="left", ipadx=6, ipady=2)
    btn_del = ttk.Button(row_grab, text="删除选中")
    btn_del.pack(side="left", padx=(10, 0))
    btn_clear = ttk.Button(row_grab, text="全部清空")
    btn_clear.pack(side="left", padx=(6, 0))

    hide_var = tk.BooleanVar(value=True)
    chk_hide = ttk.Checkbutton(
        box_grab,
        text="抓屏前自动收起面板（否则面板会被截进每一张图里）",
        variable=hide_var,
    )
    chk_hide.pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------ 列表
    box_list = ttk.LabelFrame(outer, text=" 已截取（顺序即文档顺序） ", padding=10)
    box_list.pack(fill="both", expand=True, pady=(10, 0))

    list_wrap = ttk.Frame(box_list)
    list_wrap.pack(fill="both", expand=True)
    lb = tk.Listbox(
        list_wrap,
        height=4,
        selectmode="extended",
        font=("Consolas", 10),
        activestyle="none",
        borderwidth=1,
        relief="solid",
        highlightthickness=0,
        bg="#ffffff",
        fg="#1b1b1b",
        selectbackground="#0078d7",
        selectforeground="#ffffff",
    )
    lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=lb_sb.set)
    lb.pack(side="left", fill="both", expand=True)
    lb_sb.pack(side="right", fill="y")

    summary_var = tk.StringVar(value="还没有截图")
    ttk.Label(box_list, textvariable=summary_var, foreground="#555555").pack(
        anchor="w", pady=(6, 0)
    )

    # ------------------------------------------------------------ 尺寸对齐
    box_size = ttk.LabelFrame(outer, text=" 尺寸对齐（把不同长宽统一起来） ", padding=10)
    box_size.pack(fill="x", pady=(10, 0))

    row_size = ttk.Frame(box_size)
    row_size.pack(fill="x")
    size_var = tk.StringVar(value="width")
    size_radios = []
    for text, value in (("统一宽度", "width"), ("统一高度", "height"), ("保持原始", "original")):
        rb = ttk.Radiobutton(row_size, text=text, value=value, variable=size_var)
        rb.pack(side="left")
        size_radios.append(rb)

    ttk.Label(row_size, text="目标").pack(side="left", padx=(16, 4))
    target_var = tk.StringVar(value="1200")
    spin_target = ttk.Spinbox(
        row_size, from_=TARGET_MIN, to=TARGET_MAX, textvariable=target_var, width=7
    )
    spin_target.pack(side="left")
    ttk.Label(row_size, text="px").pack(side="left", padx=(4, 0))

    size_hint = tk.StringVar(value="")
    ttk.Label(box_size, textvariable=size_hint, foreground="#888888").pack(
        anchor="w", pady=(6, 0)
    )

    # ------------------------------------------------------------ 位置对齐
    box_pos = ttk.LabelFrame(outer, text=" 位置对齐（各图在页面里的落点） ", padding=10)
    box_pos.pack(fill="x", pady=(10, 0))

    row_align = ttk.Frame(box_pos)
    row_align.pack(fill="x")
    ttk.Label(row_align, text="水平").pack(side="left")
    align_var = tk.StringVar(value="center")
    align_radios = []
    for text, value in (("居左", "left"), ("居中", "center"), ("居右", "right")):
        rb = ttk.Radiobutton(row_align, text=text, value=value, variable=align_var)
        rb.pack(side="left", padx=(8, 0))
        align_radios.append(rb)

    row_paging = ttk.Frame(box_pos)
    row_paging.pack(fill="x", pady=(6, 0))
    ttk.Label(row_paging, text="排布").pack(side="left")
    paging_var = tk.StringVar(value="each")
    paging_radios = []
    for text, value in (("每图一页", "each"), ("连续排列", "flow")):
        rb = ttk.Radiobutton(row_paging, text=text, value=value, variable=paging_var)
        rb.pack(side="left", padx=(8, 0))
        paging_radios.append(rb)

    ttk.Label(row_paging, text="页边距").pack(side="left", padx=(16, 4))
    margin_var = tk.StringVar(value="24")
    spin_margin = ttk.Spinbox(
        row_paging, from_=0, to=400, textvariable=margin_var, width=5
    )
    spin_margin.pack(side="left")
    ttk.Label(row_paging, text="px").pack(side="left", padx=(4, 0))

    ttk.Label(row_paging, text="图间距").pack(side="left", padx=(14, 4))
    gap_var = tk.StringVar(value="16")
    spin_gap = ttk.Spinbox(row_paging, from_=0, to=400, textvariable=gap_var, width=5)
    spin_gap.pack(side="left")
    ttk.Label(row_paging, text="px").pack(side="left", padx=(4, 0))

    # ------------------------------------------------------------ 扫描效果
    box_scan = ttk.LabelFrame(outer, text=" 扫描版效果 ", padding=10)
    box_scan.pack(fill="x", pady=(10, 0))

    row_scan = ttk.Frame(box_scan)
    row_scan.pack(fill="x")
    scan_var = tk.BooleanVar(value=False)
    chk_scan = ttk.Checkbutton(
        row_scan, text="转为扫描件观感（灰度 + 轻微模糊 + 纸张底色）", variable=scan_var
    )
    chk_scan.pack(side="left")

    ttk.Label(row_scan, text="强度").pack(side="left", padx=(14, 6))
    strength_var = tk.DoubleVar(value=50)
    scale_strength = ttk.Scale(
        row_scan, from_=0, to=100, variable=strength_var, orient="horizontal", length=150
    )
    scale_strength.pack(side="left")
    strength_lbl = ttk.Label(row_scan, width=4, anchor="e", text="50")
    strength_lbl.pack(side="left", padx=(6, 0))

    # ------------------------------------------------------------ 导出
    box_exp = ttk.LabelFrame(outer, text=" 导出 ", padding=10)
    box_exp.pack(fill="x", pady=(10, 0))

    row_exp = ttk.Frame(box_exp)
    row_exp.pack(fill="x")
    btn_docx = ttk.Button(row_exp, text="导出 Word", style="Start.TButton")
    btn_docx.pack(side="left", ipadx=8, ipady=2)
    btn_pdf = ttk.Button(row_exp, text="导出 PDF", style="Start.TButton")
    btn_pdf.pack(side="left", padx=(10, 0), ipadx=8, ipady=2)
    btn_open = ttk.Button(row_exp, text="打开文件", state="disabled")
    btn_open.pack(side="right")

    bar = ttk.Progressbar(box_exp, mode="indeterminate")
    bar.pack(fill="x", pady=(10, 0))

    status_var = tk.StringVar(value=f"就绪：翻到要截的那一屏，按 {HOTKEY_LABEL} 或点「截取当前屏」")
    ttk.Label(box_exp, textvariable=status_var, wraplength=580, justify="left").pack(
        anchor="w", pady=(8, 0)
    )
    result_var = tk.StringVar(value="")
    ttk.Label(box_exp, textvariable=result_var, foreground="#0a7a2f").pack(anchor="w")

    # ------------------------------------------------------------ 状态同步
    def current_layout() -> Layout:
        return Layout(
            size_mode=str(size_var.get()),
            target_px=_safe_int(target_var, 1200),
            align=str(align_var.get()),
            paging=str(paging_var.get()),
            margin_px=_safe_int(margin_var, 24),
            gap_px=_safe_int(gap_var, 16),
            page_ratio=DEFAULT_PAGE_RATIO,
            scan=bool(scan_var.get()),
            scan_strength=_safe_int(strength_var, 50),
        )

    def _sync_layout_widgets(lay: Layout, busy: bool) -> None:
        """把「参数是否参与排版」直接体现在控件可用性上。

        目标边长在「保持原始」下不参与任何计算，图间距在「每图一页」下也不参与
        —— 让它们保持可编辑只会让用户以为改了不起作用。置灰 + 一句说明，
        比文档里写一段话有效得多。
        """
        spin_target.state(
            ["disabled"] if (busy or lay.size_mode == "original") else ["!disabled"]
        )
        spin_gap.state(["disabled"] if (busy or lay.paging != "flow") else ["!disabled"])
        scale_strength.state(["disabled"] if (busy or not lay.scan) else ["!disabled"])

    def refresh(*_args) -> None:
        st["refresh_id"] = None
        shots = st["shots"]
        busy = bool(st["busy"])

        lb.delete(0, "end")
        for i, shot in enumerate(shots):
            lb.insert("end", shot.describe(i))

        lay = current_layout()
        n = len(shots)
        if n:
            summary_var.set(f"共 {n} 张 · 预计 {estimate_pages(shots, lay)} 页")
        else:
            summary_var.set("还没有截图")

        size = estimate_size(shots, lay)
        if not n:
            size_hint.set("截图后这里会显示统一后的尺寸")
        elif lay.size_mode == "original":
            size_hint.set("保持原始：各图尺寸不变，页宽按最宽的那张算")
        elif size is not None:
            size_hint.set(f"统一后：{size[0]} × {size[1]} px（按第一张换算）")
        else:
            size_hint.set("")

        can_use = n > 0 and not busy
        for btn in (btn_docx, btn_pdf):
            btn.state(["!disabled"] if can_use else ["disabled"])
        for btn in (btn_del, btn_clear):
            btn.state(["!disabled"] if can_use else ["disabled"])
        btn_capture.state(["disabled"] if busy else ["!disabled"])
        btn_capture.configure(text="抓取中…" if busy else f"截取当前屏（{HOTKEY_LABEL}）")

        _sync_layout_widgets(lay, busy)

    def schedule_refresh(*_args) -> None:
        """参数变动时合并刷新（120ms）。

        滑杆拖动一次会产生几十个 write 事件，每次都重建整个 Listbox 纯属浪费；
        合并之后体感仍是实时的，因为估算本身只是几十次整数乘除。
        """
        if st.get("refresh_id"):
            return
        st["refresh_id"] = root.after(120, refresh)

    for var in (
        size_var, target_var, align_var, paging_var, margin_var, gap_var, scan_var
    ):
        var.trace_add("write", schedule_refresh)

    def on_strength_changed(*_args) -> None:
        """强度滑杆：数字跟着走，顺带触发重算。

        用 trace 而不是 Scale 的 command —— command 只在鼠标拖拽时触发，
        键盘调值 / 代码设值都不会回调，trace 覆盖得全。
        """
        try:
            strength_lbl.configure(text=str(_safe_int(strength_var, 50)))
        except Exception:  # noqa: BLE001
            pass
        schedule_refresh()

    strength_var.trace_add("write", on_strength_changed)

    # ------------------------------------------------------------ 抓屏
    def _restore_panel() -> None:
        """把面板从最小化还原回来。

        抓完立刻还原，用户才能看到「第几张、多少像素」的反馈 ——
        否则连按 F9 时完全不知道程序有没有在动。
        短暂置顶是为了把面板从目标窗口后面露出来，400ms 后取消，
        免得它长期压住正在阅读的页面。
        """
        if not st.get("hidden"):
            return
        st["hidden"] = False
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(400, _clear_topmost)
        except tk.TclError:
            pass

    def _clear_topmost() -> None:
        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _finish_capture() -> None:
        st["busy"] = False
        refresh()

    def _grab(seq: int) -> None:
        """真正抓屏。放在 after 里跑，是为了让「抓取中…」先画到屏幕上。"""
        try:
            img = grab_screen()
        except Exception as exc:  # noqa: BLE001 - 抓屏失败不能让界面卡在忙碌态
            _restore_panel()
            _finish_capture()
            status_var.set(f"抓取失败：{type(exc).__name__}: {exc}")
            return

        # 标签用抓取时刻而不是「屏N」：删掉中间一张之后，「屏N」就和前面的
        # 序号对不上了（01 后面跟着「屏2」），而时间戳永远是真的。
        stamp = time.strftime("%H:%M:%S")
        st["shots"].append(Shot(image=img, label=stamp))
        _restore_panel()
        _finish_capture()
        h, w = int(img.shape[0]), int(img.shape[1])
        status_var.set(
            f"已追加第 {len(st['shots'])} 张（{w}×{h}）"
            f"；滚动到下一屏，再按 {HOTKEY_LABEL}"
        )

    def on_capture(*_args) -> None:
        if st["busy"]:
            return
        now = time.monotonic()
        # 冷却窗口：F9 按住时 Windows 会连续发重复键，轮询和 bind_all 也可能
        # 同时对同一次按下作出反应 —— 两者都会让同一屏被抓两次。
        if now - st["last_capture"] < COOLDOWN_S:
            return
        st["last_capture"] = now
        st["busy"] = True

        seq = len(st["shots"]) + 1
        btn_capture.state(["disabled"])
        btn_capture.configure(text="抓取中…")
        status_var.set(f"正在抓取第 {seq} 张…")

        if not bool(hide_var.get()):
            # 面板留在画面上是用户的选择，但状态文字得先画出来再抓
            root.update_idletasks()
            root.after(60, lambda: _grab(seq))
            return

        st["hidden"] = True
        try:
            root.update_idletasks()
            root.iconify()
        except tk.TclError:
            pass
        # 最小化有约 200ms 的动画。动画没结束就抓，会把「正在缩小的面板」
        # 一起拍进图里 —— 连续截图里每一张都会带同一块残影。
        root.after(ICONIFY_SETTLE_MS, lambda: _grab(seq))

    # ------------------------------------------------------------ 删除
    def on_delete() -> None:
        if st["busy"]:
            return
        picked = list(lb.curselection())
        if not picked:
            status_var.set("先在上面的列表里选中要删除的行")
            return
        # 从后往前删，否则前面删掉一个之后后面的索引全部偏移
        for index in sorted(picked, reverse=True):
            if 0 <= index < len(st["shots"]):
                del st["shots"][index]
        refresh()
        status_var.set(f"已删除 {len(picked)} 张，还剩 {len(st['shots'])} 张")

    def on_clear() -> None:
        if st["busy"] or not st["shots"]:
            return
        if not messagebox.askyesno(
            "清空列表", f"确定要清空这 {len(st['shots'])} 张截图吗？清空后无法恢复。"
        ):
            return
        st["shots"].clear()
        st["last"] = None
        btn_open.state(["disabled"])
        result_var.set("")
        refresh()
        status_var.set("已清空，可以从第一屏重新开始")

    # ------------------------------------------------------------ 导出
    def _export_worker(snap, lay, path, fmt) -> None:
        """导出线程：只往队列塞结果，绝不碰控件（tkinter 不是线程安全的）。"""
        try:
            res = export_document(snap, lay, path, fmt)
            st["queue"].put(("done", res))
        except BaseException as exc:  # noqa: BLE001 - 连 MemoryError 一起回传
            st["queue"].put(("error", exc))

    def _poll_export() -> None:
        try:
            kind, payload = st["queue"].get_nowait()
        except queue.Empty:
            root.after(100, _poll_export)
            return

        bar.stop()
        st["busy"] = False
        refresh()
        if kind == "error":
            status_var.set("导出失败")
            messagebox.showerror("导出失败", f"{type(payload).__name__}: {payload}")
            return

        st["last"] = payload.path
        btn_open.state(["!disabled"])
        result_var.set(payload.describe())
        status_var.set(f"完成：{payload.path}")

    def on_export(fmt: str) -> None:
        if st["busy"] or not st["shots"]:
            return
        fmt = normalize_format(fmt)
        label = "Word" if fmt == "docx" else "PDF"

        path = filedialog.asksaveasfilename(
            title=f"导出为 {label}",
            defaultextension="." + fmt,
            filetypes=[(f"{label} 文件", "*." + fmt)],
            initialfile=default_name(fmt),
            initialdir=str(_desktop_dir()),
        )
        if not path:
            status_var.set("已取消导出")
            return

        snap = list(st["shots"])
        lay = current_layout()
        st["busy"] = True
        result_var.set("")
        status_var.set(f"正在生成 {label}（{len(snap)} 张，{estimate_pages(snap, lay)} 页）…")
        refresh()
        bar.start(12)
        threading.Thread(
            target=_export_worker,
            args=(snap, lay, path, fmt),
            daemon=True,
            name="longshot-export",
        ).start()
        root.after(100, _poll_export)

    def on_open() -> None:
        path = st["last"]
        if not path:
            return
        try:
            os.startfile(str(path))  # noqa: S606 - 用系统默认程序打开本地文件
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开文件", f"{path}\n{exc}")

    # ------------------------------------------------------------ 快捷键
    def tick() -> None:
        st["tick_id"] = None
        if st["closed"]:
            return
        down = False
        try:
            from src import winapi

            down = winapi.key_down(winapi.VK_F9)
        except Exception:  # noqa: BLE001 - 非 Windows / 受限环境：只剩按钮可用
            down = False

        if down and not st["f9_down"]:
            st["f9_down"] = True
            if not st["busy"] and ok_to_capture():
                on_capture()
        elif not down:
            st["f9_down"] = False
        st["tick_id"] = root.after(POLL_MS, tick)

    def shutdown() -> None:
        """窗口关闭前停掉轮询，并释放常驻资源。"""
        st["closed"] = True
        for key in ("tick_id", "refresh_id"):
            pending = st.get(key)
            if pending:
                try:
                    root.after_cancel(pending)
                except Exception:  # noqa: BLE001
                    pass
                st[key] = None

    btn_capture.configure(command=on_capture)
    btn_del.configure(command=on_delete)
    btn_clear.configure(command=on_clear)
    btn_docx.configure(command=lambda: on_export("docx"))
    btn_pdf.configure(command=lambda: on_export("pdf"))
    btn_open.configure(command=on_open)

    def on_hotkey(_event=None) -> None:
        """bind_all 版本的 F9：窗口有焦点时由 Tk 派发。

        与轮询互为备份（轮询在面板最小化时仍然有效，这里不行），
        但**必须**和轮询一样先过 can_capture —— 否则长截图正在跑的时候
        按一下 F9，两个采集会同时抢屏幕。
        """
        if not ok_to_capture():
            return
        on_capture()

    root.bind_all(f"<{HOTKEY_LABEL}>", on_hotkey)

    refresh()
    tick()
    return {
        "state": st,
        "shutdown": shutdown,
        "capture": on_capture,
        "export": on_export,
        # 变量与控件都交出去：测试可以像用户一样改值、点按钮，
        # 而不用去猜控件在 pack 树里的位置（那是最脆的断言方式）。
        "vars": {
            "size_mode": size_var,
            "target": target_var,
            "align": align_var,
            "paging": paging_var,
            "margin": margin_var,
            "gap": gap_var,
            "scan": scan_var,
            "strength": strength_var,
            "hide": hide_var,
        },
        "widgets": {
            "listbox": lb,
            "capture": btn_capture,
            "delete": btn_del,
            "clear": btn_clear,
            "docx": btn_docx,
            "pdf": btn_pdf,
            "open": btn_open,
            "summary": summary_var,
            "size_hint": size_hint,
            "status": status_var,
            "result": result_var,
        },
    }
