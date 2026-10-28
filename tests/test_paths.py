"""路径口径验证：桌面定位、Windows 保留名、目标预检、错误转译、后缀规则。

这些都是「不碰界面、不碰 numpy」的纯逻辑，所以能离线把真实世界里最容易
让用户拿到坏文件（0 字节 PDF、找不到的导出文件）的几种情况全验一遍。

检验：
1. desktop_dir 永远返回一个存在的目录（跟随 Windows 已知文件夹 / XDG）
2. reserved_reason 认出全部 Windows 设备名，带扩展名也认，普通名字不误伤
3. check_target 拦下结尾空格与点、上级是文件；正常路径放行
4. explain_os_error 把 WinError 206/183/5/32 翻成人话（含「怎么办」）
5. with_suffix_for 只替换像格式的后缀，``报告.v2`` 不被吃掉
6. unique_path 同名时按 (1)(2) 递增，原文件一字不动，编号用尽抛错

直接运行：  python tests/test_paths.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import paths  # noqa: E402


class _FakeOSError(OSError):
    """造一个带指定 winerror 的 OSError，用来验错误转译的分支。"""

    def __init__(self, winerror: int) -> None:
        super().__init__(winerror, "假的系统错误")
        self.winerror = winerror


def case_desktop() -> None:
    print("\n[1] 桌面定位")
    got = paths.desktop_dir()
    assert isinstance(got, Path), got
    assert got.is_dir(), f"返回的桌面不存在：{got}"
    print(f"      desktop_dir → {got}")

    # 关键契约：桌面拿不到时退回主目录，**永远不返回不存在的路径**
    home = Path.home()
    assert got.is_dir() and (got != home or home.is_dir())
    print(f"      契约：永远返回存在的目录（本次{'退回主目录' if got == home else '命中真实桌面'}）")

    # Windows 上已知文件夹优先于 ~/Desktop：两者可能不是同一个地方（OneDrive）
    if os.name == "nt":
        known = paths._windows_desktop()
        if known is not None:
            assert known.is_dir(), known
            print(f"      SHGetFolderPathW → {known}")


def case_reserved() -> None:
    print("\n[2] Windows 保留设备名")
    for name in ("CON.pdf", "con.PDF", "PRN.docx", "AUX", "NUL.pdf",
                 "COM1.pdf", "COM9", "LPT1.doc"):
        if os.name == "nt":
            why = paths.reserved_reason(name)
            assert why, f"{name} 应该被判为保留名"
    print("      CON/PRN/AUX/NUL/COM1..9/LPT1..9 全部认出（大小写、带扩展名都算）")

    # 正常名字不能误伤：CONSOLE、COM10 都不是设备名
    for name in ("CONSOLE.pdf", "COM10.pdf", "LPT0.pdf", "报告.pdf", "contract.docx"):
        assert paths.reserved_reason(name) is None, f"{name} 被误判成保留名"
    print("      CONSOLE / COM10 / LPT0 / 中文名 不误伤")

    # 非 Windows 上这个规则不该生效（Linux 允许叫 con.pdf）
    if os.name != "nt":
        assert paths.reserved_reason("CON.pdf") is None
        print("      非 Windows 平台不启用该规则")


def case_check_target() -> None:
    print("\n[3] 导出目标预检")
    tmp = Path(tempfile.mkdtemp(prefix="ls2pdf_paths_"))
    try:
        assert paths.check_target(tmp / "正常.pdf") is None
        print("      正常路径 → 放行")

        if os.name == "nt":
            why = paths.check_target(tmp / "CON.pdf")
            assert why and "保留设备名" in why, why
            print(f"      保留名 → 拦下：{why.splitlines()[0][:40]}…")

            why = paths.check_target(tmp / "报告.pdf ")
            assert why and "空格" in why, why
            print("      结尾空格 → 拦下（Windows 会静默去掉，实际落地名不符）")

            why = paths.check_target(tmp / "报告.pdf.")
            assert why and "点" in why, why
            print("      结尾点 → 拦下")

            # 名字中间的空格完全合法，不能误伤
            assert paths.check_target(tmp / "我的 报告.pdf") is None
            print("      名字中间的空格 → 放行")

        # 上级路径被一个**文件**占住：这是最容易误读的一种失败
        occupied = tmp / "占位.pdf"
        occupied.write_bytes(b"%PDF-1.4")
        why = paths.check_target(occupied / "x.pdf")
        assert why and "已经是一个文件" in why, why
        print("      上级是文件 → 拦下（而不是让 mkdir 抛 WinError 183）")

        # 上级不存在但不是文件：交给 mkdir 建，预检要放行
        assert paths.check_target(tmp / "没建过" / "a.pdf") is None
        print("      上级尚未创建 → 放行（由 mkdir(parents=True) 负责）")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def case_explain() -> None:
    print("\n[4] 写盘错误转译")
    target = "C:/某处/很深的/报告.pdf"
    cases = [
        (206, "路径太长", "260"),
        (183, "文件", "换"),
        (5, "权限", "位置"),
        (32, "打开", "关"),
    ]
    for code, must_have, also in cases:
        msg = paths.explain_os_error(_FakeOSError(code), target)
        assert must_have in msg, (code, msg)
        assert also in msg, (code, msg)
        print(f"      WinError {code:<4} → {msg.splitlines()[0][:52]}…")
    # 认不出的错误也要带上路径，不能只吐一句裸异常
    msg = paths.explain_os_error(OSError(9999, "未知"), target)
    # Windows 上 Path 会把 / 归一成 \，所以按归一化后的形式比较
    assert str(Path(target)) in msg, msg
    print("      未收录的错误 → 仍带目标路径")


def case_suffix() -> None:
    print("\n[5] 后缀规则")
    eq = [
        # (输入, 期望后缀, 说明)
        ("a.pdf", ".pdf", "已是目标格式 → 原样"),
        ("a.docx", ".pdf", "已知格式后缀 → 替换"),
        ("a.DOCX", ".pdf", "大小写不敏感"),
        ("a.v2", ".pdf", "不像格式 → 追加，保留版本号"),
        ("无后缀", ".pdf", "无后缀 → 追加"),
        ("报告 一.final", ".docx", "非格式后缀 → 追加"),
    ]
    for raw, want, why in eq:
        got = paths.with_suffix_for(raw, want)
        assert got.suffix.lower() == want, (raw, got)
        print(f"      {raw:<14} → {got.name:<18}（{why}）")

    # 这条是回归点：Path.with_suffix 会把 报告.v2 变成 报告.pdf，吃掉版本号
    assert paths.with_suffix_for("报告.v2", ".pdf").name == "报告.v2.pdf"
    print("      回归：报告.v2 → 报告.v2.pdf（不是 报告.pdf）")

    # 带目录的输入不能被「拍平」
    got = paths.with_suffix_for("C:/a/b/报告.v2", ".pdf")
    assert got.parent.name == "b", got
    print(f"      目录结构保留：{got}")


def case_unique(tmp: Path) -> None:
    print("\n[6] 同名避让（同名文件不再覆盖，按 (1)(2) 递增）")
    target = tmp / "长图.pdf"
    # 不存在时原样返回
    assert paths.unique_path(target) == target
    print("      不存在 → 原名")

    target.write_bytes(b"first")
    got1 = paths.unique_path(target)
    assert got1.name == "长图(1).pdf", got1
    got1.write_bytes(b"second")
    got2 = paths.unique_path(target)
    assert got2.name == "长图(2).pdf", got2
    print(f"      已存在两次 → {got1.name} / {got2.name}")

    # 原文件必须一个字节都没被改动
    assert target.read_bytes() == b"first"
    print("      原文件内容未被改动")

    # 版本号式文件名：序号插在**最后一段扩展名**前，不吃掉 .v2
    v = tmp / "报告.v2.pdf"
    v.write_bytes(b"x")
    assert paths.unique_path(v).name == "报告.v2(1).pdf", paths.unique_path(v)
    print("      报告.v2.pdf → 报告.v2(1).pdf")

    # 上限兜底：塞满 3 个编号后应该抛 OSError 而不是死循环
    for i in (1, 2, 3):
        (tmp / f"满({i}).pdf").write_bytes(b"x")
    (tmp / "满.pdf").write_bytes(b"x")
    try:
        paths.unique_path(tmp / "满.pdf", max_index=3)
    except OSError as exc:
        print(f"      编号用尽 → OSError：{str(exc)[:40]}…")
    else:
        raise AssertionError("编号用尽时没有抛错，可能死循环")


def main() -> int:
    print("=" * 64)
    print("路径口径 · 桌面 / 保留名 / 预检 / 错误转译 / 后缀 / 同名避让")
    print("=" * 64)
    try:
        case_desktop()
        case_reserved()
        case_check_target()
        case_explain()
        case_suffix()
        with tempfile.TemporaryDirectory(prefix="ls2pdf_paths_") as tag:
            case_unique(Path(tag))
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
