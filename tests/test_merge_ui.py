"""「PDF 合并」面板的离线集成验证：真窗口、假文件对话框，不弹真弹窗。

为什么值得测界面层
------------------
这一页的核心是**输入校验**：用户在右边框里打字，写错一个数字（``1-99``）
或写错一个分隔符，最坏结果不是报错，而是悄悄少合并几十页。所以这里验的是
界面层才存在的东西：

    打字 → 立即校验 → 出错就置灰「合并并保存」并把错在第几行说出来
    改对 → 立刻恢复可点
    行序 / 删除 / 页码越界 → 清单状态跟着变
    点合并 → 真的调 pdfmerge.merge，落地的文件页数对得上

检验：
1. 初始状态：清单为空、合并置灰
2. add_file 两卷 PDF → 清单两行、状态显示合计页数、合并可点
3. 页码写错 → 状态行首是 ✗ 且指出第几行，合并置灰；改对 → 恢复
4. ↑↓ 调整顺序 → 合并结果的页序跟着变
5. ✕ 删除一行 → 剩下的重新编号
6. 点「合并并保存…」→ 文件真的生成，页数 = 两卷之和，状态行显示结果
7. 输出同名 → 自动加 (1)，原文件不动

直接运行：  python tests/test_merge_ui.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from PIL import Image  # noqa: E402

import gui  # noqa: E402
from src import pdfwriter  # noqa: E402


class FakeDialog:
    """假的 filedialog / messagebox：固定返回，绝不阻塞。"""

    def __init__(self, save_to: Path | None = None) -> None:
        self.save_to = save_to
        self.info: list[str] = []
        self.errors: list[str] = []
        self.asked: list[str] = []

    def asksaveasfilename(self, **_kw):
        self.asked.append("saveas")
        return str(self.save_to) if self.save_to else ""

    def askopenfilenames(self, **_kw):
        return ()

    def showinfo(self, title, message, **_kw):
        self.info.append(f"{title}: {message}")

    def showerror(self, title, message, **_kw):
        self.errors.append(f"{title}: {message}")


def make_pdf(dir_path: Path, name: str, pages: int, color) -> Path:
    jpegs = []
    for i in range(pages):
        arr = np.zeros((60, 48, 3), dtype=np.uint8)
        arr[:, :] = color
        arr[: 4 + i % 6, : 5] = 240
        p = dir_path / f"{name}_{i + 1:03d}.jpg"
        Image.fromarray(arr).save(p, quality=88, subsampling=0)
        jpegs.append(p)
    return pdfwriter.jpegs_to_pdf(jpegs, dir_path / f"{name}.pdf")


def pump(root, until, timeout: float = 10.0) -> bool:
    """推进事件循环直到 until() 为真（不用 mainloop，否则测试会挂住）。"""
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return True
        time.sleep(0.01)
    return False


def enabled(widget) -> bool:
    return "disabled" not in widget.state()


def case_flow(tmp: Path) -> None:
    dialog = FakeDialog(save_to=tmp / "合并结果.pdf")
    root = tk.Tk()
    root.geometry("720x860")
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue
    style.configure("Title.TLabel", font=("Microsoft YaHei", 13, "bold"))
    style.configure("Start.TButton", font=("Microsoft YaHei", 12, "bold"))

    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    panel = gui.build_merge_tab(
        frame, tk=tk, ttk=ttk, filedialog=dialog, messagebox=dialog, root=root
    )
    root.update()

    print("\n[1] 初始状态")
    assert panel["rows"] == [], panel["rows"]
    assert not enabled(panel["btn_merge"]), "空清单时「合并并保存」应该置灰"
    assert panel["status_var"].get() == "清单为空", panel["status_var"].get()
    print(f"      清单空、合并置灰、状态「{panel['status_var'].get()}」")

    a = make_pdf(tmp, "甲", 3, (200, 50, 50))
    b = make_pdf(tmp, "乙", 4, (50, 60, 200))

    print("\n[2] 登记两卷 PDF")
    panel["add_file"](a)
    panel["add_file"](b)
    root.update()
    assert len(panel["rows"]) == 2, len(panel["rows"])
    assert enabled(panel["btn_merge"])
    status = panel["status_var"].get()
    assert "2 个来源" in status and "7 页" in status, status
    print(f"      {status}")
    assert panel["rows"][0]["count"] == 3 and panel["rows"][1]["count"] == 4

    print("\n[3] 页码写错 → 立刻置灰并指出第几行")
    panel["rows"][1]["ranges"].set("1-99")
    root.update()
    status = panel["status_var"].get()
    assert status.startswith("✗"), status
    assert "第 2 行" in status and "超出范围" in status, status
    assert not enabled(panel["btn_merge"]), "有错时不该还能点合并"
    print(f"      {status}")

    panel["rows"][1]["ranges"].set("2-4")
    root.update()
    assert enabled(panel["btn_merge"]), "改对之后应该恢复可点"
    assert panel["status_var"].get().startswith("待合并")
    print(f"      {panel['status_var'].get()}（恢复可点）")

    print("\n[4] ↓ 调整顺序：乙 排到 甲 前面")
    panel["move"](panel["rows"][1], -1)
    root.update()
    assert [r["path"].name for r in panel["rows"]] == ["乙.pdf", "甲.pdf"]
    assert [r["idx"].cget("text") for r in panel["rows"]] == ["1", "2"]
    print(f"      顺序 → {[r['path'].name for r in panel['rows']]}，编号已重排")

    print("\n[5] 点「合并并保存…」")
    panel["btn_merge"].invoke()
    ok = pump(root, lambda: bool(dialog.info) or bool(dialog.errors))
    assert ok, "合并没有在 10s 内结束"
    assert not dialog.errors, dialog.errors
    out = tmp / "合并结果.pdf"
    assert out.exists(), f"没有生成 {out}"
    assert pdfwriter.pdf_page_count(out) == 3 + 3, pdfwriter.pdf_page_count(out)
    print(f"      生成 {out.name}：{pdfwriter.pdf_page_count(out)} 页"
          f"（乙 2-4 三页 + 甲 全部三页）")
    print(f"      状态行：{panel['status_var'].get()}")
    assert enabled(panel["btn_open"]), "合并完成后「打开结果」应该可用"

    print("\n[6] 再点一次 → 自动落到 合并结果(1).pdf")
    dialog.info.clear()
    panel["btn_merge"].invoke()
    ok = pump(root, lambda: bool(dialog.info) or bool(dialog.errors))
    assert ok, "第二次合并没有结束"
    assert (tmp / "合并结果(1).pdf").exists(), "同名时没有自动加 (1)"
    print("      合并结果.pdf 未动，第二份落到 合并结果(1).pdf")

    print("\n[7] ✕ 删除第一行 → 重新编号")
    panel["remove"](panel["rows"][0])
    root.update()
    assert len(panel["rows"]) == 1
    assert panel["rows"][0]["idx"].cget("text") == "1"
    print(f"      剩 {len(panel['rows'])} 行，编号 {panel['rows'][0]['idx'].cget('text')}")

    print("\n[8] 版式：面板请求尺寸必须装得下（不能把底部按钮挤出窗口）")
    root.update_idletasks()
    need_w, need_h = frame.winfo_reqwidth(), frame.winfo_reqheight()
    assert need_w <= 700, f"请求宽度 {need_w} > 700，窗口会被撑宽"
    assert need_h <= 820, f"请求高度 {need_h} > 820，底部按钮会被切掉"
    print(f"      请求 {need_w}×{need_h}，窗口内容区约 700×820")

    root.destroy()


def main() -> int:
    print("=" * 64)
    print("PDF 合并面板 · 清单 / 校验 / 排序 / 真合并 / 同名避让")
    print("=" * 64)
    try:
        with tempfile.TemporaryDirectory(prefix="ls2pdf_mergeui_") as tag:
            case_flow(Path(tag))
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
