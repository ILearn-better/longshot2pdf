"""GUI 集成验证（离线）：实际点一遍「框选 → 开始 → 采集 → 结束」。

验的是「开始时面板要让开画面」这条链路，具体四点：
1. 点开始后先显示提示，**再**最小化（否则窗口一闪就没了，分不清是开始还是崩了）；
2. 真正采集时窗口处于 `iconic` —— 最小化的窗口不参与屏幕合成，不会被截进画面；
3. 最小化动画结束后才启动采集（否则首帧会拍到「正在缩下去的面板」，
   而首帧是全程基准，那块污点会污染整页）；
4. 采集结束后自动还原，否则「采集成功」在用户眼里等于「窗口没了、卡死了」。

不碰真实桌面的技巧
------------------
- `mainloop` 换成手动泵（只跑 update），否则 after 回调永远不执行；
- `select_region` 打桩返回固定区域，省掉全屏框选；
- `tkinter.messagebox` 的弹窗函数全部打桩 —— 注意它的引用是 main() 的
  **局部变量**（`_load_tk()` 返回），在 gui 模块上打桩无效，必须打到模块上；
- `src.pipeline.run` 换成 0.6 秒就返回的桩：真实 run 会抓屏、投递滚轮，
  在没有交互式桌面的机器上会直接卡住，那也不是本文件要验的东西。

需要带 tkinter 的解释器；没有时 run_all.py 会自动跳过。
"""

from __future__ import annotations

import sys
import time
import tkinter
import tkinter.messagebox as _mb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui  # noqa: E402
from src import pipeline as P  # noqa: E402

EVENTS: list[tuple[float, str]] = []
MARKS: dict[str, float] = {}
T0 = time.monotonic()
PUMP_SECONDS = 6.0


def log(msg: str) -> None:
    EVENTS.append((time.monotonic() - T0, msg))


# ---------------------------------------------------------------- 打桩

class FakeResult:
    """假采集结果：0 页会走「未生成 PDF」分支，正好顺带验一次错误弹窗不阻塞。"""

    ok = True
    pdf_path = None
    pages = 0
    frames = 0
    rows = 0
    size_mb = 0.0
    message = "桩：模拟采集结束"
    stopped_early = False
    temp_dir = None
    stage = "done"


def fake_run(cfg, on_progress=None, should_stop=None, deps=None):
    log("run 启动")
    if on_progress:
        for i in range(3):
            time.sleep(0.12)
            on_progress(i + 1, 1, 600 * (i + 1), f"桩进度 {i + 1}")
    log("run 返回")
    return FakeResult()


P.run = fake_run
gui.select_region = lambda: (0, 0, 600, 400)


def _dialog(name):
    def _f(*a, **k):
        log(f"dialog:{name}")
        return True

    return _f


for _n in ("showerror", "showinfo", "showwarning", "askyesno", "askokcancel"):
    setattr(_mb, _n, _dialog(_n))


# 记录窗口状态变化
_orig_iconify = tkinter.Wm.iconify
_orig_deiconify = tkinter.Wm.deiconify


def iconify(self, *a):
    log("iconify")
    return _orig_iconify(self, *a)


def deiconify(self, *a):
    log("deiconify")
    return _orig_deiconify(self, *a)


tkinter.Wm.iconify = iconify
tkinter.Wm.deiconify = deiconify


# ---------------------------------------------------------------- 事件泵

def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def find_button(root, prefix):
    for w in walk(root):
        if isinstance(w, tkinter.ttk.Button):
            try:
                if str(w.cget("text")).startswith(prefix):
                    return w
            except tkinter.TclError:
                pass
    return None


_created: dict = {}
_clicked = {"v": False}
_orig_update = tkinter.Misc.update
_orig_tk = tkinter.Tk

# 「停止条件」那组控件的现场观测结果，交给 main() 参与判定
UI: dict = {}


def probe_stop_mode(root) -> None:
    """点「开始」之前，先验一遍「停止条件」这个开关。

    要验三件事，任何一件错了用户就控制不了行为：
      1. 两个选项都在（滚不动就停 / 自动判断完成）；
      2. 默认是「滚不动就停」—— 用户要的是「自动滚动 = 替我按滚轮」；
      3. 切到手动滚动时置灰（手动模式的收尾靠 idle_timeout，这个开关无意义）。
    """
    radios: dict[str, object] = {}
    for w in walk(root):
        if isinstance(w, tkinter.ttk.Radiobutton):
            try:
                radios[str(w.cget("text"))] = w
            except tkinter.TclError:
                pass
    UI["labels"] = sorted(radios)
    if "滚不动就停" not in radios or "自动判断完成" not in radios:
        UI["error"] = f"没找到两个选项，只有 {sorted(radios)}"
        return

    scroll_rb = radios["滚不动就停"]
    manual_rb = next(
        (w for t, w in radios.items() if t.startswith("手动")), None
    )
    var_name = str(scroll_rb.cget("variable"))
    UI["default"] = str(root.getvar(var_name))

    if manual_rb is not None:
        manual_rb.invoke()
        UI["disabled_in_manual"] = bool(scroll_rb.instate(["disabled"]))
        # 切回自动，后面还要正常走一遍采集流程
        next(w for t, w in radios.items() if t == "自动").invoke()
        UI["enabled_back"] = not bool(scroll_rb.instate(["disabled"]))
        UI["value_after_switch"] = str(root.getvar(var_name))


def make_root(*a, **k):
    r = _orig_tk(*a, **k)
    _created["root"] = r
    return r


def pump_update(self):
    """第一次 update 时界面已建好 —— 在这里验开关、点「框选」再点「开始」。"""
    if "root" in _created and not _clicked["v"]:
        _clicked["v"] = True
        root = _created["root"]
        probe_stop_mode(root)
        sel = find_button(root, "框选区域")
        start = find_button(root, "开始")
        if sel is None or start is None:
            log("[FAIL] 找不到按钮")
        else:
            sel.invoke()
            log("点击开始")
            start.invoke()
            root.after(60, lambda: log(f"state@60ms={root.state()}"))
            root.after(700, lambda: log(f"state@700ms={root.state()}"))
    return _orig_update(self)


def pump_mainloop(self, n=0):
    end = time.monotonic() + PUMP_SECONDS
    while time.monotonic() < end:
        try:
            self.update()
        except tkinter.TclError:
            return
        time.sleep(0.02)


tkinter.Tk = make_root
tkinter.Misc.update = pump_update
tkinter.Misc.mainloop = pump_mainloop


# ---------------------------------------------------------------- 执行

def main() -> int:
    print("LongShot2PDF 界面：开始后让开画面（离线集成）")
    rc = gui.main()

    root = _created.get("root")
    try:
        final_state = root.state() if root is not None else "?"
    except tkinter.TclError:
        final_state = "destroyed"

    print("  时间轴：")
    for t, e in EVENTS:
        print(f"    {t:5.2f}s  {e}")

    def at(name: str) -> float | None:
        for t, e in EVENTS:
            if e == name:
                return t
        return None

    t_click, t_icon = at("点击开始"), at("iconify")
    t_run, t_run_end, t_deicon = at("run 启动"), at("run 返回"), at("deiconify")
    state_700 = next((e for _, e in EVENTS if e.startswith("state@700ms=")), "")

    def fmt(t: float | None) -> str:
        return "-" if t is None else f"{t:.2f}s"

    checks: list[tuple[str, bool, str]] = [
        ("停止条件：两个选项都在",
         "滚不动就停" in UI.get("labels", []) and "自动判断完成" in UI.get("labels", []),
         UI.get("error") or "、".join(UI.get("labels", []))),
        ("停止条件：默认「滚不动就停」", UI.get("default") == "scroll",
         f"默认值 {UI.get('default')}"),
        ("停止条件：手动模式下置灰", bool(UI.get("disabled_in_manual")), ""),
        # 切回自动滚动后，用户上次选的「停止条件」不该被重置
        ("停止条件：切回自动后恢复可选且保留选择",
         bool(UI.get("enabled_back")) and UI.get("value_after_switch") == "scroll",
         f"值 {UI.get('value_after_switch')}"),
        ("点了开始", t_click is not None, ""),
        ("最小化发生在点击之后", bool(t_icon and t_click and t_icon > t_click),
         f"{fmt(t_click)} → {fmt(t_icon)}"),
        ("采集时窗口已最小化", state_700.endswith("iconic"), state_700),
        ("最小化后才启动采集（≥300ms）",
         bool(t_icon and t_run and t_run - t_icon >= 0.30),
         f"间隔 {0 if not (t_icon and t_run) else t_run - t_icon:.2f}s"),
        ("采集结束后自动还原", bool(t_run_end and t_deicon and t_deicon >= t_run_end),
         f"{fmt(t_run_end)} → {fmt(t_deicon)}"),
        ("最终窗口可见", final_state == "normal", final_state),
        ("main() 正常返回", rc == 0, str(rc)),
    ]

    bad = 0
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail else ""))
        bad += 0 if ok else 1

    if bad:
        print(f"  {bad} 项未通过")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
