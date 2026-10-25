"""全屏框选：在虚拟桌面上拖出一个矩形，外加一组「改这块矩形」的纯算术工具。

为什么从 gui.py 搬出来
----------------------
两个面板都要框选：「滚动截长图」框的是要滚动的窗口，「连续截图整合」框的是
要反复抓的那一块。而 `python gui.py` 运行时 gui 的模块名是 `__main__`，
从别处 `import gui` 会把整个界面再执行一遍（多一个 Tk 实例、多一个 mainloop，
在 Windows 上表现是闪一下窗口或者直接卡住）。搬进 `src/` 就没有这个坑。

坐标系
------
一律是 Windows 虚拟桌面的**物理像素**，副屏在主屏左边时 x 为负。
Tk 的 geometry 语法里负 x 表示「距右边缘」，表达不了副屏的负坐标，
所以覆盖层落到哪里只影响观感；选框的**结果**始终用 `x_root/y_root` 算，
不受覆盖层实际位置影响。
"""

from __future__ import annotations

from typing import Tuple

# 用 typing.Tuple：别名在运行期求值，内置 tuple[...] 需要 Python 3.9+
Region = Tuple[int, int, int, int]  # (left, top, width, height) 物理像素

MIN_SELECTION = 40  # 松手时宽或高小于此值视为误触，忽略这次拖拽
MIN_SIDE = 16       # 手工输入 / 对齐吸附后允许的最小边（比误触阈值小，便于微调）


# ------------------------------------------------------------------ 屏幕范围


def _coerce_region(value) -> Region | None:
    """把 winapi.virtual_screen() 的返回值统一成 (l, t, w, h)（防御式兼容）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        if all(k in value for k in ("left", "top", "width", "height")):
            return tuple(
                int(value[k]) for k in ("left", "top", "width", "height")
            )  # type: ignore[return-value]
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

    首选 `winapi.virtual_screen()`（能拿到左侧/上方副屏的负坐标）；
    拿不到时退回 `mss.monitors[0]` —— 注意不能退回 `GetSystemMetrics(0/1)`，
    那两个值只是主屏尺寸，副屏会被漏掉。
    """
    try:
        from . import winapi

        fn = getattr(winapi, "virtual_screen", None)
        if callable(fn):
            got = _coerce_region(fn())
            if got and got[2] > 0 and got[3] > 0:
                return got
    except Exception:  # noqa: BLE001
        pass

    try:
        import mss

        factory = getattr(mss, "MSS", None) or getattr(mss, "mss")
        with factory() as sct:
            mon = sct.monitors[0]
            return (
                int(mon["left"]),
                int(mon["top"]),
                int(mon["width"]),
                int(mon["height"]),
            )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"拿不到虚拟桌面尺寸：{type(exc).__name__}: {exc}") from exc


def _geometry(size: Region) -> str:
    """把 (l, t, w, h) 拼成 Tk 的 geometry 串。

    负坐标时退化为覆盖主屏起点 (0,0) —— 结果坐标仍取 x_root/y_root，
    不影响正确性，只是副屏区域可能在覆盖层之外。真机上遇到多屏偏移时，
    用命令行 `--region` 直接给坐标更稳。
    """
    left, top, width, height = size
    if left < 0 or top < 0:
        return f"{width}x{height}+0+0"
    return f"{width}x{height}+{left}+{top}"


# ------------------------------------------------------------------ 纯算术


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


def clamp_region(region: Region, screen: Region) -> Region:
    """把区域的**位置**收进桌面、**尺寸**收进桌面尺寸，不改变优先级。

    与 `capture.normalize_region` 的区别：那个是「超出部分裁掉」，
    这里是「先挪进来再裁」，所以一块跑到屏幕外的区域仍然能完整保留 ——
    界面上拖 Spinbox 把 x 调成很大的值时，用户想要的是「贴到右边缘」，
    而不是「图像被裁成一条」。
    """
    left, top, width, height = (int(v) for v in region)
    sx, sy, sw, sh = (int(v) for v in screen)
    width = max(MIN_SIDE, min(width, sw))
    height = max(MIN_SIDE, min(height, sh))
    left = max(sx, min(left, sx + sw - width))
    top = max(sy, min(top, sy + sh - height))
    return (left, top, width, height)


H_ANCHORS = ("left", "center", "right")
V_ANCHORS = ("top", "middle", "bottom")


def snap_region(region: Region, screen: Region, hx: str, vy: str) -> Region:
    """把区域按它自己的尺寸贴到桌面的 左/中/右 × 上/中/下。

    这是需求里「对截取区域的位置进行对齐」的核心：只改位置、不改尺寸，
    所以对齐前后的图大小完全一致（连续截图的各张才能严丝合缝）。
    """
    _, _, width, height = (int(v) for v in region)
    sx, sy, sw, sh = (int(v) for v in screen)
    width = max(MIN_SIDE, min(width, sw))
    height = max(MIN_SIDE, min(height, sh))

    left = {"left": sx, "right": sx + sw - width}.get(hx, sx + (sw - width) // 2)
    top = {"top": sy, "bottom": sy + sh - height}.get(vy, sy + (sh - height) // 2)
    return (int(left), int(top), int(width), int(height))


def fit_region(
    region: Region,
    screen: Region,
    *,
    width: int | None = None,
    height: int | None = None,
    keep_ratio: bool = False,
) -> Region:
    """改尺寸的入口：可单独改宽或高，可按原比例联动，改完自动收回桌面内。

    keep_ratio=True 时，给了 width 就按原比例算 height（反之亦然）。
    优先级规则：两个都给时以 width 为准 —— 界面上同时编辑宽高是拖控件
    的中间态，必须有个确定行为，否则宽高会互相推着跑。
    """
    left, top, old_w, old_h = (int(v) for v in region)
    old_w, old_h = max(MIN_SIDE, old_w), max(MIN_SIDE, old_h)

    w = int(width) if width is not None else old_w
    h = int(height) if height is not None else old_h
    if keep_ratio:
        if width is not None:
            h = int(round(old_h * (w / old_w)))
        elif height is not None:
            w = int(round(old_w * (h / old_h)))

    w = max(MIN_SIDE, w)
    h = max(MIN_SIDE, h)
    return clamp_region((left, top, w, h), screen)


def output_size(region: Region, out_width: int = 0) -> tuple[int, int]:
    """截取结果的像素尺寸。

    out_width > 0 时把图统一到指定宽度（高度按区域比例算），
    这就是需求里「对整体图片尺寸进行对齐和调整」：
    框出来的区域可以大小不一，但交付出去的图可以一边宽。
    注意**放大只是插值，不会增加细节** —— 想要更清晰只能框得更大。
    """
    _, _, width, height = (int(v) for v in region)
    width, height = max(1, width), max(1, height)
    if out_width and int(out_width) > 0:
        w = int(out_width)
        return (w, max(1, int(round(height * w / width))))
    return (width, height)


# ------------------------------------------------------------------ 全屏框选


def select_region(initial: Region | None = None, hint: str = "") -> Region | None:
    """全屏半透明覆盖层上拖拽框选，返回物理像素 (x, y, w, h)；取消返回 None。

    交互：按住左键拖 → 红框 + 亮区 + 实时尺寸；松手确认；ESC / 右键取消。
    initial 会在覆盖层上画一个虚线框作为「当前区域」的起点，方便微调而不是重框。
    """
    import tkinter as tk

    from . import winapi

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
            text=hint or "拖拽框选要截取的区域 · 松开确认 · ESC 或右键取消",
            fill="#f0f0f0", anchor="nw", font=("Microsoft YaHei", 12, "bold"),
        )

        # 当前区域用虚线标出来：用户往往是「在已有区域上微调」而不是从零框
        if initial:
            ix = int(initial[0]) - int(screen[0])
            iy = int(initial[1]) - int(screen[1])
            canvas.create_rectangle(
                ix, iy, ix + int(initial[2]), iy + int(initial[3]),
                outline="#7fd1ff", width=1, dash=(4, 3),
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
