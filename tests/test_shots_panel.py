"""「连续截图整合」面板的离线集成验证：不抓真实屏幕，但走完整界面逻辑。

做法：真的建一个 Tk 窗口和面板，只把两处外部依赖换掉 ——
    · shots_gui.grab_screen() → 返回合成图像（否则要真屏幕，且每张都一样）
    · filedialog / messagebox → 直接给路径、直接点「是」
然后用 root.update() 手动推进事件循环（不用 mainloop，否则测试就挂住了）。
这正好能验到「按钮状态、列表编号、页数估算、导出结果」这些只在界面层
才存在的东西 —— 纯逻辑测试碰不到它们。

检验：
1. 初始状态：列表空、导出置灰、提示「按 F9」
2. 点「截取」→ 追加到列表末尾，编号 01/02 递增（顺序即可见）
3. 参数联动：改目标边长 → 摘要里的页数/尺寸跟着变；改「保持原始」→ 目标框置灰
4. 删除选中 → 剩下的自动重编号，后面的顺序不变
5. 一键导出 Word → 文件真的生成，状态区显示结果，「打开文件」可用
6. 清空 → 回到初始状态
7. shutdown() 能停掉 F9 轮询（不会留下 root.after 定时器）
8. 取消保存对话框 → 不导出、不进忙碌态
9. 切到「自定义区域」→ 区域控件出现、默认给一个居中框
10. 九宫格对齐只挪位置；锁定比例改宽时高跟着变
11. 抓取框选区域 → 传下去的 rect 与界面上的数字一致，列表标出「框选」
12. 统一输出宽度 → 抓到的图按这个宽度等比缩放
13. 「框选区域…」→ 覆盖层返回的矩形直接成为新区域
14. 切回「整屏」→ 区域控件整块消失，抓的还是整屏

直接运行：  python tests/test_shots_panel.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

import shots_gui  # noqa: E402
from src import regionpick  # noqa: E402
from src.docxwriter import docx_image_count  # noqa: E402


class FakeDialog:
    """假的 filedialog / messagebox：固定返回，不弹窗也不阻塞。"""

    def __init__(self, save_to: Path | None = None, confirm: bool = True):
        self.save_to = save_to
        self.confirm = confirm
        self.asked: list[str] = []

    # filedialog 接口
    def asksaveasfilename(self, **_kw):
        self.asked.append("saveas")
        return str(self.save_to) if self.save_to else ""

    # messagebox 接口
    def askyesno(self, title="", message="", **_kw):
        self.asked.append(f"yesno:{title}")
        return self.confirm

    def showerror(self, title="", message="", **_kw):
        self.asked.append(f"error:{title}")
        raise AssertionError(f"不该出现错误弹窗：{title} / {message}")

    def showinfo(self, *_a, **_kw):
        pass


def pump(root, until, timeout: float = 8.0) -> bool:
    """手动推进事件循环直到条件成立。超时返回 False，绝不无限等。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        root.update()
        if until():
            return True
        time.sleep(0.02)
    root.update()
    return bool(until())


def make_root() -> tk.Tk:
    root = tk.Tk()
    root.withdraw()  # 不闪窗口；withdraw 状态下 update() 照样跑事件循环
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Title.TLabel", font=("Microsoft YaHei", 13, "bold"))
    style.configure("Start.TButton", font=("Microsoft YaHei", 12, "bold"))
    return root


def build(root, dialog: FakeDialog) -> dict:
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    return shots_gui.build_panel(
        frame,
        tk=tk,
        ttk=ttk,
        filedialog=dialog,
        messagebox=dialog,
        root=root,
    )


def fake_grab(size=(800, 600), value=200):
    """替换 shots_gui.grab_screen：返回指定尺寸的合成图像。"""
    w, h = size

    def _grab():
        img = np.full((h, w, 3), value, dtype=np.uint8)
        img[: h // 10] = 60
        return img

    return _grab


def enabled(widget) -> bool:
    return "disabled" not in widget.state()


def grab_once(panel, root, expect: int) -> None:
    """点一次「截取」并等到列表变成 expect 行。

    先睡过冷却窗口：面板对 0.35s 内的重复触发是**故意**忽略的（F9 按住时
    系统会连续发重复键），真实使用时人翻页本来就比这慢，测试得照同一个节奏走。
    """
    time.sleep(shots_gui.COOLDOWN_S + 0.05)
    panel["capture"]()
    assert pump(root, lambda: panel["widgets"]["listbox"].size() == expect), (
        f"期望 {expect} 行，实际 {panel['widgets']['listbox'].size()} 行"
    )


def case_flow(tmp: Path) -> None:
    original_grab = shots_gui.grab_screen
    shots_gui.grab_screen = fake_grab()
    root = make_root()
    dialog = FakeDialog(save_to=tmp / "out.docx")
    try:
        panel = build(root, dialog)
        lb = panel["widgets"]["listbox"]
        summary = panel["widgets"]["summary"]
        hint = panel["widgets"]["size_hint"]
        status = panel["widgets"]["status"]
        panel["vars"]["hide"].set(False)  # 不最小化，抓屏走 60ms 延迟分支

        print("\n[1] 初始状态")
        assert lb.size() == 0
        assert summary.get() == "还没有截图", summary.get()
        assert not enabled(panel["widgets"]["docx"]), "空列表时导出应置灰"
        assert not enabled(panel["widgets"]["pdf"])
        assert not enabled(panel["widgets"]["delete"])
        assert enabled(panel["widgets"]["capture"])
        assert shots_gui.HOTKEY_LABEL in status.get(), status.get()
        print(f"      列表空 · 导出置灰 · {status.get()}")

        print("\n[2] 连按两次「截取」→ 顺序追加")
        grab_once(panel, root, 1)
        assert lb.get(0).startswith("01."), lb.get(0)
        assert "800×600" in lb.get(0), lb.get(0)
        assert "共 1 张" in summary.get() and "预计 1 页" in summary.get(), summary.get()
        assert enabled(panel["widgets"]["docx"]), "有图之后导出应可用"
        print(f"      {lb.get(0)!r} · {summary.get()}")

        grab_once(panel, root, 2)
        assert lb.get(1).startswith("02."), lb.get(1)
        assert "共 2 张 · 预计 2 页" == summary.get(), summary.get()
        print(f"      {lb.get(1)!r} · {summary.get()}")

        print("\n[3] 参数联动")
        assert "统一后" in hint.get() and "1200" in hint.get(), hint.get()
        print(f"      默认 {hint.get()}")
        panel["vars"]["size_mode"].set("original")
        assert pump(root, lambda: "保持原始" in hint.get()), hint.get()
        spin_target = panel["vars"]["target"]
        # 「保持原始」时目标边长不参与排版 → 必须置灰
        target_widget = [
            w for w in _walk(root)
            if isinstance(w, ttk.Spinbox) and str(w.cget("textvariable")) == str(spin_target)
        ]
        assert target_widget and not enabled(target_widget[0]), "目标边长未置灰"
        print(f"      original → {hint.get()}，目标边长已置灰")

        panel["vars"]["size_mode"].set("height")
        panel["vars"]["target"].set("300")
        assert pump(root, lambda: "300" in hint.get()), hint.get()
        print(f"      height/300 → {hint.get()}")

        print("\n[4] 删除选中 → 自动重编号")
        panel["vars"]["size_mode"].set("width")
        panel["vars"]["target"].set("400")
        pump(root, lambda: True)
        lb.selection_set(0)
        panel["widgets"]["delete"].invoke()
        assert pump(root, lambda: lb.size() == 1), "删除后应剩 1 张"
        assert lb.get(0).startswith("01."), f"未重编号: {lb.get(0)}"
        assert "共 1 张" in summary.get(), summary.get()
        grab_once(panel, root, 2)
        assert lb.get(1).startswith("02."), lb.get(1)
        print(f"      删除后 {lb.get(0)!r}，再截一张 → {lb.get(1)!r}")

        print("\n[5] 一键导出 Word")
        panel["widgets"]["docx"].invoke()
        assert panel["state"]["busy"], "导出期间应处于忙碌态"
        assert not enabled(panel["widgets"]["docx"]), "导出期间按钮应禁用"
        assert pump(
            root,
            lambda: not panel["state"]["busy"] and bool(panel["widgets"]["result"].get()),
        ), "导出没有结束"
        out = Path(panel["state"]["last"])
        assert out.exists(), f"文件没生成: {out}"
        assert docx_image_count(out) == 2, docx_image_count(out)
        assert enabled(panel["widgets"]["open"]), "导出完成后「打开文件」应可用"
        print(f"      {panel['widgets']['result'].get()}")
        print(f"      {out.name} · 2 张图")

        print("\n[6] 清空")
        dialog.confirm = True
        panel["widgets"]["clear"].invoke()
        assert pump(root, lambda: lb.size() == 0), "清空没生效"
        assert summary.get() == "还没有截图"
        assert not enabled(panel["widgets"]["docx"])
        print(f"      {status.get()}")

        print("\n[7] shutdown 停掉轮询")
        before = panel["state"]["tick_id"]
        assert before, "F9 轮询没有启动"
        panel["shutdown"]()
        assert panel["state"]["closed"] is True
        assert panel["state"]["tick_id"] is None
        print("      轮询已注销")
    finally:
        shots_gui.grab_screen = original_grab
        try:
            root.destroy()
        except tk.TclError:
            pass


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def case_cancel_export(tmp: Path) -> None:
    print("\n[8] 取消保存对话框 → 不导出、不进忙碌态")
    original_grab = shots_gui.grab_screen
    shots_gui.grab_screen = fake_grab((640, 480))
    root = make_root()
    dialog = FakeDialog(save_to=None)  # 用户点了取消
    try:
        panel = build(root, dialog)
        panel["vars"]["hide"].set(False)
        grab_once(panel, root, 1)
        panel["export"]("pdf")
        assert not panel["state"]["busy"], "取消后不该留在忙碌态"
        assert panel["state"]["last"] is None
        assert "取消" in panel["widgets"]["status"].get()
        print(f"      {panel['widgets']['status'].get()}")
    finally:
        shots_gui.grab_screen = original_grab
        try:
            root.destroy()
        except tk.TclError:
            pass


def case_region(tmp: Path) -> None:
    """自定义区域：框选、对齐、按比例改尺寸、统一输出宽度。"""
    print("\n[9] 自定义区域模式")
    original = (shots_gui.screen_rect, shots_gui.grab_screen,
                shots_gui.grab_region, regionpick.select_region)
    shots_gui.screen_rect = lambda: (0, 0, 1920, 1080)
    shots_gui.grab_screen = fake_grab((1920, 1080))
    got_rects: list[tuple] = []

    def fake_region_grab(rect):
        got_rects.append(tuple(int(v) for v in rect))
        w, h = int(rect[2]), int(rect[3])
        img = np.full((h, w, 3), 210, dtype=np.uint8)
        img[: max(1, h // 10)] = 60
        return img

    shots_gui.grab_region = fake_region_grab
    root = make_root()
    dialog = FakeDialog(save_to=tmp / "region.docx")
    try:
        panel = build(root, dialog)
        panel["vars"]["hide"].set(False)
        mode = panel["vars"]["capture_mode"]
        lb = panel["widgets"]["listbox"]

        print("\n[9a] 切到自定义区域 → 默认给一个居中框")
        assert panel["widgets"]["screen"].get() == "屏幕 1920×1080", (
            panel["widgets"]["screen"].get()
        )
        mode.set("region")
        assert pump(root, lambda: panel["vars"]["region_w"].get() == "1344"), (
            panel["vars"]["region_w"].get()
        )
        assert panel["vars"]["region_x"].get() == "288", panel["vars"]["region_x"].get()
        assert panel["vars"]["region_y"].get() == "162"
        assert "框选区域" in panel["widgets"]["capture"].cget("text"), (
            panel["widgets"]["capture"].cget("text")
        )
        print(f"      默认 {panel['vars']['region_w'].get()}×"
              f"{panel['vars']['region_h'].get()} @ "
              f"({panel['vars']['region_x'].get()}, {panel['vars']['region_y'].get()})")

        print("\n[9b] 九宫格对齐：只挪位置")
        panel["callbacks"]["snap"]("left", "top")
        assert (panel["vars"]["region_x"].get(), panel["vars"]["region_y"].get()) == ("0", "0")
        panel["callbacks"]["snap"]("right", "bottom")
        rx, ry = int(panel["vars"]["region_x"].get()), int(panel["vars"]["region_y"].get())
        assert (rx, ry) == (1920 - 1344, 1080 - 756), (rx, ry)
        assert panel["vars"]["region_w"].get() == "1344", "对齐不该改尺寸"
        print(f"      贴右下 → ({rx}, {ry})，尺寸仍是 1344×756")

        print("\n[9c] 改尺寸：锁定比例联动")
        panel["callbacks"]["snap"]("left", "top")
        panel["vars"]["region_w"].set("1600")
        panel["callbacks"]["normalize_region"]()
        assert panel["vars"]["region_w"].get() == "1600"
        assert panel["vars"]["region_h"].get() == "756", panel["vars"]["region_h"].get()
        panel["vars"]["lock_ratio"].set(True)
        panel["vars"]["region_w"].set("800")
        panel["callbacks"]["normalize_region"]()
        assert panel["vars"]["region_w"].get() == "800", panel["vars"]["region_w"].get()
        # 基准是「当前这块」的 1600:756，宽减半 → 高也减半
        assert panel["vars"]["region_h"].get() == "378", panel["vars"]["region_h"].get()
        panel["vars"]["region_h"].set("189")
        panel["callbacks"]["normalize_region"]()
        assert panel["vars"]["region_w"].get() == "400", panel["vars"]["region_w"].get()
        print("      未锁比例 1600×756；锁比例后改宽 800 → 高 378，改高 189 → 宽 400")

        print("\n[9d] 抓取框选区域")
        # 先把区域定成一块好认的尺寸，再验「界面上的数字 == 传给抓屏的 rect」
        panel["vars"]["lock_ratio"].set(False)
        panel["vars"]["region_x"].set("120")
        panel["vars"]["region_y"].set("80")
        panel["vars"]["region_w"].set("800")
        panel["vars"]["region_h"].set("450")
        panel["callbacks"]["normalize_region"]()
        expect = tuple(
            int(panel["vars"][k].get()) for k in ("region_x", "region_y", "region_w", "region_h")
        )
        grab_once(panel, root, 1)
        assert got_rects and got_rects[-1] == expect, (got_rects, expect)
        assert "800×450" in lb.get(0), lb.get(0)
        assert "框选" in lb.get(0), lb.get(0)
        assert "框选" in panel["widgets"]["status"].get()
        print(f"      {lb.get(0)!r} → 传给抓屏的 rect {got_rects[-1]}")

        print("\n[9e] 统一输出宽度")
        panel["vars"]["out_mode"].set("fixed")
        panel["vars"]["out_width"].set("1200")
        # 800×450 统一到宽 1200 → 高 675
        assert pump(root, lambda: "1200 × 675" in panel["widgets"]["out_hint"].get()), (
            panel["widgets"]["out_hint"].get()
        )
        grab_once(panel, root, 2)
        assert "1200×675" in lb.get(1), lb.get(1)
        assert got_rects[-1] == expect, "统一输出宽度不该改变截取区域"
        print(f"      {lb.get(1)!r} · {panel['widgets']['out_hint'].get()}")

        print("\n[9f] 「框选区域…」→ 覆盖层返回值即新区域")
        regionpick.select_region = lambda initial=None, hint="": (100, 120, 640, 480)
        panel["pick"]()
        assert pump(root, lambda: panel["vars"]["region_w"].get() == "640"), (
            panel["vars"]["region_w"].get()
        )
        assert panel["vars"]["region_x"].get() == "100"
        assert "已框选 640×480" in panel["widgets"]["status"].get(), (
            panel["widgets"]["status"].get()
        )
        print(f"      {panel['widgets']['status'].get()}")

        print("\n[9g] 切回整屏 → 区域控件消失，抓的仍是整屏")
        mode.set("screen")
        assert pump(root, lambda: "整屏" in panel["widgets"]["status"].get())
        assert "截取当前屏" in panel["widgets"]["capture"].cget("text")
        before = len(got_rects)
        grab_once(panel, root, 3)
        assert len(got_rects) == before, "整屏模式不该调用 grab_region"
        assert "1920×1080" in lb.get(2), lb.get(2)
        print(f"      {lb.get(2)!r} · {panel['widgets']['status'].get()}")
    finally:
        (shots_gui.screen_rect, shots_gui.grab_screen,
         shots_gui.grab_region, regionpick.select_region) = original
        try:
            root.destroy()
        except tk.TclError:
            pass


def main() -> int:
    print("=" * 64)
    print("连续截图整合 · 面板集成")
    print("=" * 64)
    tmp = Path(tempfile.mkdtemp(prefix="longshot_panel_"))
    try:
        case_flow(tmp)
        case_cancel_export(tmp)
        case_region(tmp)
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
