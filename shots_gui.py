"""「连续截图整合」面板：截取 → 追加 → 尺寸对齐 → 位置对齐 → 扫描效果 → 导出。

为什么单独一个模块
------------------
`gui.py` 已经有 800 多行，而两个面板的生命周期几乎不重叠：滚动截长图是
「一次长任务」（线程 + 最小化 + 进度队列），连续截图是「一串短动作」
（按一次抓一屏、调参数、导出）。混在一个函数里，两边状态机迟早互相踩。
拆开之后 gui.py 只负责把两个标签页挂上去。

交互约定（与需求的六条一一对应）
------------------------------
1) **两种触发**：面板上的「截取」按钮 + 全局 F9。
   「全局」是关键：抓屏前面板会把自己最小化，那时 tkinter 的 bind 收不到
   任何按键，所以用 GetAsyncKeyState 轮询物理键状态（与项目里 ESC 中断
   同一套机制）。焦点停在浏览器上照样生效，这才是连续截图的真实用法。
2) **自动追加**：每张 append 到列表末尾，序号 01/02/03 就是文档顺序。
   列表里能看见顺序，删除只删选中的那张，其余顺序不变。
3) **尺寸对齐**：统一宽度 / 统一高度 / 保持原始 + 目标边长（ps）。
4) **位置对齐**：水平 左/中/右 + 排布 每图一页/连续排列 + 页边距/图间距。
5) **扫描效果**：开关 + 强度滑杆；随机种子固定，所以预览与导出完全一致。
6) **一键导出**：Word 或 PDF 各一个按钮，完成后可直接打开。

截取范围（v0.7 起）
------------------
    整屏        抓整个虚拟桌面（多屏合并）
    自定义区域  抓事先框好的一块，**每张都是同一块** —— 这才是连拍的用法：
                翻页时正文位置不动，框住正文就只抓正文，不必事后再裁。
    区域可改位置（X/Y + 九宫格对齐）、改大小（宽/高 + 锁定比例）、
    还能把输出统一到固定宽度（区域大小不一，交付的图却一边宽）。
    选「整屏」时这一整块控件**消失**（不是置灰）——
    它不参与截取，留着只会让人以为改了有用。

状态展示（不是只打日志，按钮本身要变）
------------------------------------
    空闲（0 张）  截取可用；删除/清空/导出 置灰；提示「按 F9 开始」
    就绪（N 张）  导出可用；摘要「共 N 张 · 预计 M 页 · 统一后 1200×675」
    框选中        面板先让开画面，框完自动回来并报「已框选 900×600」
    抓取中        截取按钮变「抓取中…」并禁用（防连点把同一屏抓两次）
    导出中        两个导出按钮同时禁用，进度条跑起来
    失败          弹出原因 + 红色状态，已截内容一张不丢
另外几个「参数联动」也在界面上表达出来：选「保持原始」时目标边长置灰，
选「每图一页」时图间距置灰，选「整屏」时整个区域控件消失 ——
参数不参与排版时就该看起来不参与。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from src import paths, regionpick
from src.regionpick import Region, clamp_region, output_size, snap_region
from src.shots import (
    DEFAULT_PAGE_RATIO,
    Layout,
    Shot,
    estimate_dpi,
    estimate_pages,
    estimate_size,
    resize_to_width,
)
from src.shots_export import default_name, export_document, normalize_format

HOTKEY_LABEL = "F9"          # 与 winapi.VK_F9 对应
POLL_MS = 60                 # 轮询 F9 的间隔；60ms 人感觉不到延迟，开销可忽略
ICONIFY_SETTLE_MS = 320      # 最小化动画约 200ms，等它画完再抓
COOLDOWN_S = 0.35            # 两次抓屏的最小间隔，挡住按键自动重复
TARGET_MIN, TARGET_MAX = 200, 8000
OUT_W_MIN, OUT_W_MAX = 200, 10000

# 清晰度档位：给的是「嵌入像素 = 排版尺寸 × 倍数」的上限，
# 实际不会超过原图像素（放大不增加细节，只会让文件变大）。
HI_RES_LEVELS = (("标准", "1"), ("高清", "2"), ("超清", "3"))
DEFAULT_HI_RES = "2"

CAPTURE_MODES = (("整屏", "screen"), ("自定义区域", "region"))

# 九宫格的标签与锚点。顺序就是按钮从左到右、从上到下
ANCHORS = (
    ("↖", "left", "top"), ("↑", "center", "top"), ("↗", "right", "top"),
    ("←", "left", "middle"), ("◎", "center", "middle"), ("→", "right", "middle"),
    ("↙", "left", "bottom"), ("↓", "center", "bottom"), ("↘", "right", "bottom"),
)


def _desktop_dir() -> Path:
    """默认导出目录：桌面（跟随 OneDrive / XDG 重定向），没有就退回主目录。"""
    return paths.desktop_dir()


def screen_rect() -> Region:
    """虚拟桌面范围（物理像素，多屏合并，允许负坐标）。"""
    try:
        return regionpick.virtual_screen_rect()
    except Exception:  # noqa: BLE001 - 拿不到桌面也不能让面板起不来
        return (0, 0, 1920, 1080)


def grab_screen():
    """抓整个屏幕，返回 (H, W, 3) uint8 RGB。

    保持无参：测试直接把它替换成造好的合成图（见 tests/test_shots_panel.py），
    换成带参签名会让那些断言全都要跟着改，而它们验的不是抓屏。
    """
    from src.capture import Grabber

    with Grabber() as grabber:
        return grabber.grab(screen_rect())


def grab_region(rect: Region):
    """抓指定区域，返回 (H, W, 3) uint8 RGB。

    先过一遍 `normalize_region`：区域可能被拖到屏幕外（尤其贴副屏边缘时），
    mss 遇到这种区域不报错而是返回一块全黑图，那种图混进文档最难查。
    """
    from src.capture import Grabber, normalize_region

    safe = normalize_region(tuple(int(v) for v in rect), screen_rect())
    with Grabber() as grabber:
        return grabber.grab(safe)


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
        "region": None,       # (l, t, w, h)，仅自定义模式有效
        "screen": None,       # 缓存的虚拟桌面，切模式时刷新
        "mode": "screen",
        "syncing": False,     # 防止「写控件变量」再触发回调形成回环
    }

    def _safe_int(var, default: int) -> int:
        """读整数控件。Spinbox/Entry 里可能是一段非法中间态（如空串、'-'），
        直接 int(var.get()) 会抛 TclError 把整个回调打断 —— 退回默认值即可。"""
        try:
            return int(round(float(var.get())))
        except Exception:  # noqa: BLE001
            return default

    # ------------------------------------------------------------ 截取范围
    box_grab = ttk.LabelFrame(outer, text=" 截取范围 ", padding=10)
    box_grab.pack(fill="x")

    row_mode = ttk.Frame(box_grab)
    row_mode.pack(fill="x")
    mode_var = tk.StringVar(value="screen")
    mode_radios = []
    for text, value in CAPTURE_MODES:
        rb = ttk.Radiobutton(row_mode, text=text, value=value, variable=mode_var)
        rb.pack(side="left", padx=(0 if not mode_radios else 14, 0))
        mode_radios.append(rb)

    screen_var = tk.StringVar(value="")
    ttk.Label(row_mode, textvariable=screen_var, foreground="#888888").pack(
        side="left", padx=(18, 0)
    )

    # ---- 以下整块只在「自定义区域」时出现 ----
    # 三行排完：位置/大小一格、对齐一格、输出一格。
    # 长说明一律不挂在这一块上 —— 它们是「窄屏下把窗口撑宽」的元凶，
    # 说明放状态行和 README 里，界面只留控件本身。
    region_box = ttk.Frame(box_grab)

    row_rect = ttk.Frame(region_box)
    row_rect.pack(fill="x", pady=(8, 0))
    btn_pick = ttk.Button(row_rect, text="框选区域…")
    btn_pick.pack(side="left")
    btn_undo = ttk.Button(row_rect, text="还原为整屏")
    btn_undo.pack(side="left", padx=(6, 0))

    ttk.Label(row_rect, text="位置").pack(side="left", padx=(14, 4))
    x_var = tk.StringVar(value="0")
    y_var = tk.StringVar(value="0")
    spin_x = ttk.Spinbox(row_rect, from_=-20000, to=20000, textvariable=x_var, width=5)
    spin_x.pack(side="left")
    ttk.Label(row_rect, text=",").pack(side="left", padx=1)
    spin_y = ttk.Spinbox(row_rect, from_=-20000, to=20000, textvariable=y_var, width=5)
    spin_y.pack(side="left")

    ttk.Label(row_rect, text="大小").pack(side="left", padx=(12, 4))
    w_var = tk.StringVar(value="0")
    h_var = tk.StringVar(value="0")
    spin_w = ttk.Spinbox(row_rect, from_=16, to=20000, textvariable=w_var, width=5)
    spin_w.pack(side="left")
    ttk.Label(row_rect, text="×").pack(side="left", padx=1)
    spin_h = ttk.Spinbox(row_rect, from_=16, to=20000, textvariable=h_var, width=5)
    spin_h.pack(side="left")
    ttk.Label(row_rect, text="px").pack(side="left", padx=(3, 0))

    lock_var = tk.BooleanVar(value=False)
    chk_lock = ttk.Checkbutton(row_rect, text="锁定比例", variable=lock_var)
    chk_lock.pack(side="left", padx=(10, 0))

    row_anchor = ttk.Frame(region_box)
    row_anchor.pack(fill="x", pady=(6, 0))
    ttk.Label(row_anchor, text="对齐").pack(side="left")
    anchor_btns = []
    for label, hx, vy in ANCHORS:
        btn = ttk.Button(
            row_anchor, text=label, width=2,
            command=lambda h=hx, v=vy: on_snap(h, v),
        )
        btn.pack(side="left", padx=(2, 0))
        anchor_btns.append(btn)

    # 第三行：输出尺寸 + 它的即时读数（读数就在控件旁边，不用来回看）
    row_out = ttk.Frame(region_box)
    row_out.pack(fill="x", pady=(6, 0))
    ttk.Label(row_out, text="输出").pack(side="left")
    out_mode_var = tk.StringVar(value="same")
    radio_out_same = ttk.Radiobutton(
        row_out, text="按区域原样", value="same", variable=out_mode_var
    )
    radio_out_same.pack(side="left", padx=(6, 0))
    radio_out_fix = ttk.Radiobutton(
        row_out, text="统一宽度", value="fixed", variable=out_mode_var
    )
    radio_out_fix.pack(side="left", padx=(4, 0))
    out_w_var = tk.StringVar(value="1600")
    spin_out_w = ttk.Spinbox(
        row_out, from_=OUT_W_MIN, to=OUT_W_MAX, textvariable=out_w_var, width=6
    )
    spin_out_w.pack(side="left", padx=(4, 0))
    ttk.Label(row_out, text="px").pack(side="left", padx=(3, 0))

    out_hint = tk.StringVar(value="")
    ttk.Label(row_out, textvariable=out_hint, foreground="#888888").pack(
        side="left", padx=(10, 0)
    )

    # ------------------------------------------------------------ 截取
    row_grab = ttk.Frame(box_grab)
    row_grab.pack(fill="x", pady=(8, 0))
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
        row_grab,
        text="抓屏前自动收起面板",
        variable=hide_var,
    )
    chk_hide.pack(side="left", padx=(14, 0))

    # ------------------------------------------------------------ 列表
    box_list = ttk.LabelFrame(outer, text=" 已截取（顺序即文档顺序） ", padding=10)
    box_list.pack(fill="both", expand=True, pady=(6, 0))

    list_wrap = ttk.Frame(box_list)
    list_wrap.pack(fill="both", expand=True)
    lb = tk.Listbox(
        list_wrap,
        height=3,
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
    box_size.pack(fill="x", pady=(6, 0))

    row_size = ttk.Frame(box_size)
    row_size.pack(fill="x")
    size_var = tk.StringVar(value="width")
    size_radios = []
    for text, value in (("统一宽度", "width"), ("统一高度", "height"), ("保持原始", "original")):
        rb = ttk.Radiobutton(row_size, text=text, value=value, variable=size_var)
        rb.pack(side="left")
        size_radios.append(rb)

    ttk.Label(row_size, text="目标").pack(side="left", padx=(16, 4))
    target_var = tk.StringVar(value="1600")
    spin_target = ttk.Spinbox(
        row_size, from_=TARGET_MIN, to=TARGET_MAX, textvariable=target_var, width=7
    )
    spin_target.pack(side="left")
    ttk.Label(row_size, text="px").pack(side="left", padx=(4, 0))

    row_hi = ttk.Frame(box_size)
    row_hi.pack(fill="x", pady=(6, 0))
    ttk.Label(row_hi, text="输出清晰度").pack(side="left")
    hi_res_var = tk.StringVar(value=DEFAULT_HI_RES)
    hi_radios = []
    for text, value in HI_RES_LEVELS:
        rb = ttk.Radiobutton(row_hi, text=text, value=value, variable=hi_res_var)
        rb.pack(side="left", padx=(8, 0))
        hi_radios.append(rb)
    hi_hint = tk.StringVar(value="")
    ttk.Label(row_hi, textvariable=hi_hint, foreground="#888888").pack(
        side="left", padx=(12, 0)
    )

    size_hint = tk.StringVar(value="")
    ttk.Label(box_size, textvariable=size_hint, foreground="#888888").pack(
        anchor="w", pady=(6, 0)
    )

    # ------------------------------------------------------------ 位置对齐
    box_pos = ttk.LabelFrame(outer, text=" 位置对齐（各图在页面里的落点） ", padding=10)
    box_pos.pack(fill="x", pady=(6, 0))

    row_align = ttk.Frame(box_pos)
    row_align.pack(fill="x")
    ttk.Label(row_align, text="水平").pack(side="left")
    align_var = tk.StringVar(value="center")
    align_radios = []
    for text, value in (("居左", "left"), ("居中", "center"), ("居右", "right")):
        rb = ttk.Radiobutton(row_align, text=text, value=value, variable=align_var)
        rb.pack(side="left", padx=(6, 0))
        align_radios.append(rb)

    ttk.Label(row_align, text="排布").pack(side="left", padx=(12, 0))
    paging_var = tk.StringVar(value="each")
    paging_radios = []
    for text, value in (("每图一页", "each"), ("连续排列", "flow")):
        rb = ttk.Radiobutton(row_align, text=text, value=value, variable=paging_var)
        rb.pack(side="left", padx=(6, 0))
        paging_radios.append(rb)

    ttk.Label(row_align, text="页边距").pack(side="left", padx=(12, 4))
    margin_var = tk.StringVar(value="24")
    spin_margin = ttk.Spinbox(
        row_align, from_=0, to=400, textvariable=margin_var, width=4
    )
    spin_margin.pack(side="left")
    ttk.Label(row_align, text="px").pack(side="left", padx=(3, 0))

    ttk.Label(row_align, text="图间距").pack(side="left", padx=(10, 4))
    gap_var = tk.StringVar(value="16")
    spin_gap = ttk.Spinbox(row_align, from_=0, to=400, textvariable=gap_var, width=4)
    spin_gap.pack(side="left")
    ttk.Label(row_align, text="px").pack(side="left", padx=(3, 0))

    # ------------------------------------------------------------ 扫描效果
    box_scan = ttk.LabelFrame(outer, text=" 扫描版效果 ", padding=10)
    box_scan.pack(fill="x", pady=(6, 0))

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
    box_exp.pack(fill="x", pady=(6, 0))

    row_exp = ttk.Frame(box_exp)
    row_exp.pack(fill="x")
    btn_docx = ttk.Button(row_exp, text="导出 Word", style="Start.TButton")
    btn_docx.pack(side="left", ipadx=8, ipady=2)
    btn_pdf = ttk.Button(row_exp, text="导出 PDF", style="Start.TButton")
    btn_pdf.pack(side="left", padx=(10, 0), ipadx=8, ipady=2)
    btn_open = ttk.Button(row_exp, text="打开文件", state="disabled")
    btn_open.pack(side="right")

    bar = ttk.Progressbar(box_exp, mode="indeterminate")
    bar.pack(fill="x", pady=(6, 0))

    status_var = tk.StringVar(value=f"就绪：翻到要截的那一屏，按 {HOTKEY_LABEL} 或点「截取当前屏」")
    ttk.Label(box_exp, textvariable=status_var, wraplength=580, justify="left").pack(
        anchor="w", pady=(8, 0)
    )
    result_var = tk.StringVar(value="")
    ttk.Label(box_exp, textvariable=result_var, foreground="#0a7a2f").pack(anchor="w")

    # ------------------------------------------------------------ 区域状态
    def current_screen() -> Region:
        if st["screen"] is None:
            st["screen"] = screen_rect()
        return st["screen"]

    def region_from_vars() -> Region:
        """把四个 Spinbox 读成区域。

        中间态（空串、只剩减号）用上一个有效值兜底，不让界面卡在半路；
        真正收口（最小边、比例、贴回桌面）发生在 normalize_region_vars()，
        那是失焦/回车/箭头时才跑的 —— 否则用户打「1920」打到一半就被改写。
        """
        old = st["region"] or (0, 0, 1, 1)
        x = _safe_int(x_var, old[0])
        y = _safe_int(y_var, old[1])
        w = _safe_int(w_var, old[2])
        h = _safe_int(h_var, old[3])
        return (x, y, max(1, w), max(1, h))

    def write_region(reg: Region, *, sync_vars: bool = True) -> None:
        st["region"] = reg
        # 记住「上一次归一化后的区域」：锁定比例时要以它为基准算比例，
        # 否则用户把宽改成 800 之后，基准也跟着变成 800，比例就永远是 1:1。
        st["commit"] = reg
        if not sync_vars:
            return
        st["syncing"] = True
        try:
            x_var.set(str(reg[0]))
            y_var.set(str(reg[1]))
            w_var.set(str(reg[2]))
            h_var.set(str(reg[3]))
        finally:
            st["syncing"] = False

    def normalize_region_vars(*_args) -> None:
        """编辑结束：按锁定比例联动，再收进桌面，最后写回控件。

        基准取「上次归一化后的区域」而不是当前输入框里的值 —— 输入框里那个
        已经被用户改过了，拿它算比例等于拿结果算原因。
        """
        if st["mode"] != "region":
            return
        raw = region_from_vars()
        base = st.get("commit") or raw
        scr = current_screen()

        if bool(lock_var.get()):
            w_changed = raw[2] != base[2]
            h_changed = raw[3] != base[3]
            if w_changed and not h_changed:
                sized = regionpick.fit_region(
                    (raw[0], raw[1], base[2], base[3]), scr,
                    width=raw[2], keep_ratio=True,
                )
                raw = (raw[0], raw[1], sized[2], sized[3])
            elif h_changed and not w_changed:
                sized = regionpick.fit_region(
                    (raw[0], raw[1], base[2], base[3]), scr,
                    height=raw[3], keep_ratio=True,
                )
                raw = (raw[0], raw[1], sized[2], sized[3])

        write_region(clamp_region(raw, scr))
        refresh()

    def on_region_var_changed(*_args) -> None:
        """编辑中：只更新内部状态并刷新提示，不回写控件（回写会打断输入）。"""
        if st["syncing"] or st["mode"] != "region":
            return
        st["region"] = region_from_vars()
        schedule_refresh()

    def out_width_value() -> int:
        """返回「统一输出宽度」，0 表示按区域原样。

        整屏模式下返回 0：那个控件在界面上已经整块消失了，
        它的值就不该再悄悄影响结果 —— 看不见的参数不许生效。
        """
        if st["mode"] != "region" or str(out_mode_var.get()) != "fixed":
            return 0
        return max(1, _safe_int(out_w_var, 1600))

    def on_snap(hx: str, vy: str) -> None:
        """九宫格对齐：只挪位置，尺寸保持不变（各张才能严丝合缝）。"""
        if st["busy"]:
            return
        scr = current_screen()
        reg = snap_region(region_from_vars(), scr, hx, vy)
        write_region(reg)
        refresh()
        status_var.set(
            f"区域已贴到 {hx}/{vy}：{reg[2]}×{reg[3]} @ ({reg[0]}, {reg[1]})"
        )

    def on_reset_region() -> None:
        """还原为整屏那么大的一块（等价于「整屏」但保留在自定义模式里）。"""
        scr = current_screen()
        write_region(scr)
        refresh()
        status_var.set(f"区域已还原为整屏 {scr[2]}×{scr[3]}")

    def on_mode_changed(*_args) -> None:
        mode = str(mode_var.get())
        st["mode"] = "region" if mode == "region" else "screen"
        st["screen"] = None  # 重新问一次桌面，可能用户刚插了显示器
        scr = current_screen()
        if st["mode"] == "region":
            if st["region"] is None:
                # 给一个合理起点：居中 70% 的屏幕。总比 0×0 让用户自己去猜好
                w = max(regionpick.MIN_SIDE, int(scr[2] * 0.7))
                h = max(regionpick.MIN_SIDE, int(scr[3] * 0.7))
                write_region(snap_region((scr[0], scr[1], w, h), scr, "center", "middle"))
            else:
                write_region(clamp_region(st["region"], scr))
            region_box.pack(fill="x", after=row_mode)
            status_var.set(
                "自定义区域：点「框选区域…」拖一块，之后每按一次"
                f"{HOTKEY_LABEL} 都抓这一块"
            )
        else:
            region_box.pack_forget()
            status_var.set(f"整屏模式：按 {HOTKEY_LABEL} 抓下整个屏幕")
        refresh()

    # ------------------------------------------------------------ 状态同步
    def current_layout() -> Layout:
        return Layout(
            size_mode=str(size_var.get()),
            target_px=_safe_int(target_var, 1600),
            align=str(align_var.get()),
            paging=str(paging_var.get()),
            margin_px=_safe_int(margin_var, 24),
            gap_px=_safe_int(gap_var, 16),
            page_ratio=DEFAULT_PAGE_RATIO,
            scan=bool(scan_var.get()),
            scan_strength=_safe_int(strength_var, 50),
            hi_res=float(_safe_int(hi_res_var, 2)),
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
        spin_out_w.state(
            ["disabled"] if (busy or str(out_mode_var.get()) != "fixed") else ["!disabled"]
        )
        for btn in [btn_pick, btn_undo, *anchor_btns]:
            btn.state(["disabled"] if busy else ["!disabled"])
        for spin in (spin_x, spin_y, spin_w, spin_h):
            spin.state(["disabled"] if busy else ["!disabled"])

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

        # 清晰度：说清「嵌入多少像素」，而不是给一个没有参照的 dpi 数字
        if not n:
            hi_hint.set("原图有更多像素就用更多，绝不放大")
        else:
            dpi = estimate_dpi(shots, lay)
            limited = min(s.width for s in shots) < lay.target_px * lay.hi_res
            hi_hint.set(
                f"约 {dpi:.0f} dpi"
                + ("（已用满原图像素，再调高不会更清晰）" if limited else "")
            )

        # 区域读数的两种口径：区域本身、以及最终输出的图
        scr = current_screen()
        screen_var.set(f"屏幕 {scr[2]}×{scr[3]}")
        if st["mode"] == "region":
            reg = st["region"] or region_from_vars()
            ow, oh = output_size(reg, out_width_value())
            extra = "" if (ow, oh) == (reg[2], reg[3]) else f"（缩放到 {ow}×{oh}）"
            out_hint.set(f"→ 输出 {ow} × {oh} px{extra}")

        can_use = n > 0 and not busy
        for btn in (btn_docx, btn_pdf):
            btn.state(["!disabled"] if can_use else ["disabled"])
        for btn in (btn_del, btn_clear):
            btn.state(["!disabled"] if can_use else ["disabled"])
        btn_capture.state(["disabled"] if busy else ["!disabled"])
        if busy:
            btn_capture.configure(text="抓取中…")
        else:
            btn_capture.configure(
                text=(
                    f"截取框选区域（{HOTKEY_LABEL}）"
                    if st["mode"] == "region"
                    else f"截取当前屏（{HOTKEY_LABEL}）"
                )
            )

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
        size_var, target_var, align_var, paging_var, margin_var, gap_var, scan_var,
        hi_res_var, out_mode_var,
    ):
        var.trace_add("write", schedule_refresh)

    for var in (x_var, y_var, w_var, h_var):
        var.trace_add("write", on_region_var_changed)

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

    # ------------------------------------------------------------ 框选
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

    def _hide_panel(then) -> None:
        """收起面板再执行 then（抓屏/框选）。

        不收起的话，整屏截图会把面板自己拍进去，而且每一张都在同一个位置。
        最小化有约 200ms 的动画，动画没结束就抓会把「正在缩小的面板」一起
        拍进图里，所以统一等这个时长。
        """
        if not bool(hide_var.get()):
            root.update_idletasks()
            root.after(60, then)
            return
        st["hidden"] = True
        try:
            root.update_idletasks()
            root.iconify()
        except tk.TclError:
            pass
        root.after(ICONIFY_SETTLE_MS, then)

    def on_pick() -> None:
        if st["busy"]:
            return
        st["busy"] = True
        refresh()
        status_var.set("正在框选：拖一块要反复截取的区域，松开确认（ESC 取消）")
        _hide_panel(_pick_now)

    def _pick_now() -> None:
        try:
            reg = regionpick.select_region(
                initial=st["region"],
                hint=(
                    f"拖拽框选要反复截取的区域 · 松开确认 · ESC 取消"
                    f"（之后按 {HOTKEY_LABEL} 抓的都是这一块）"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 框选失败不能让面板卡在忙碌态
            _restore_panel()
            st["busy"] = False
            refresh()
            status_var.set(f"框选失败：{type(exc).__name__}: {exc}")
            return

        _restore_panel()
        st["busy"] = False
        if reg:
            reg = clamp_region(reg, current_screen())
            write_region(reg)
            status_var.set(
                f"已框选 {reg[2]}×{reg[3]}（左上 {reg[0]}, {reg[1]}）；"
                f"之后每按一次 {HOTKEY_LABEL} 都抓这一块"
            )
        else:
            status_var.set("已取消框选，区域保持不变")
        refresh()

    # ------------------------------------------------------------ 抓屏
    def _finish_capture() -> None:
        st["busy"] = False
        refresh()

    def _grab(seq: int) -> None:
        """真正抓屏。放在 after 里跑，是为了让「抓取中…」先画到屏幕上。"""
        block: Region | None = None
        if st["mode"] == "region":
            block = st["region"] or region_from_vars()
        try:
            img = grab_region(block) if block else grab_screen()
        except Exception as exc:  # noqa: BLE001 - 抓屏失败不能让界面卡在忙碌态
            _restore_panel()
            _finish_capture()
            status_var.set(f"抓取失败：{type(exc).__name__}: {exc}")
            return

        # 输出尺寸：区域大小不一时按统一宽度收口，交付出去的图才一边宽
        ow = out_width_value()
        if ow:
            try:
                img = resize_to_width(img, ow)
            except Exception as exc:  # noqa: BLE001
                _restore_panel()
                _finish_capture()
                status_var.set(f"统一输出宽度失败：{type(exc).__name__}: {exc}")
                return

        # 标签用抓取时刻而不是「屏N」：删掉中间一张之后，「屏N」就和前面的
        # 序号对不上了（01 后面跟着「屏2」），而时间戳永远是真的。
        stamp = time.strftime("%H:%M:%S")
        tag = f"{stamp} · 框选" if block else stamp
        st["shots"].append(Shot(image=img, label=tag))
        _restore_panel()
        _finish_capture()
        h, w = int(img.shape[0]), int(img.shape[1])
        where = f"，框选 {block[2]}×{block[3]}" if block else ""
        status_var.set(
            f"已追加第 {len(st['shots'])} 张（{w}×{h}{where}）"
            f"；翻到下一屏，再按 {HOTKEY_LABEL}"
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
        if st["mode"] == "region":
            reg = st["region"] or region_from_vars()
            status_var.set(f"正在抓取第 {seq} 张（框选 {reg[2]}×{reg[3]}）…")
        else:
            status_var.set(f"正在抓取第 {seq} 张…")
        refresh()
        _hide_panel(lambda: _grab(seq))

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
            paths.open_in_default_app(path)
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
    btn_pick.configure(command=on_pick)
    btn_undo.configure(command=on_reset_region)

    for spin in (spin_x, spin_y, spin_w, spin_h):
        spin.bind("<FocusOut>", normalize_region_vars)
        spin.bind("<Return>", normalize_region_vars)
        spin.configure(command=normalize_region_vars)
    chk_lock.configure(command=normalize_region_vars)

    mode_var.trace_add("write", on_mode_changed)
    root.bind_all(f"<{HOTKEY_LABEL}>", lambda _e: ok_to_capture() and on_capture())

    refresh()
    tick()
    return {
        "state": st,
        "shutdown": shutdown,
        "capture": on_capture,
        "export": on_export,
        "pick": on_pick,
        # 变量与控件都交出去：测试可以像用户一样改值、点按钮，
        # 而不用去猜控件在 pack 树里的位置（那是最脆的断言方式）。
        "vars": {
            "capture_mode": mode_var,
            "region_x": x_var,
            "region_y": y_var,
            "region_w": w_var,
            "region_h": h_var,
            "lock_ratio": lock_var,
            "out_mode": out_mode_var,
            "out_width": out_w_var,
            "size_mode": size_var,
            "target": target_var,
            "align": align_var,
            "paging": paging_var,
            "margin": margin_var,
            "gap": gap_var,
            "scan": scan_var,
            "strength": strength_var,
            "hi_res": hi_res_var,
            "hide": hide_var,
        },
        "widgets": {
            "listbox": lb,
            "capture": btn_capture,
            "delete": btn_del,
            "clear": btn_clear,
            "pick": btn_pick,
            "reset_region": btn_undo,
            "docx": btn_docx,
            "pdf": btn_pdf,
            "open": btn_open,
            "summary": summary_var,
            "size_hint": size_hint,
            "hi_hint": hi_hint,
            "out_hint": out_hint,
            "screen": screen_var,
            "status": status_var,
            "result": result_var,
        },
        "callbacks": {
            "snap": on_snap,
            "normalize_region": normalize_region_vars,
        },
    }
