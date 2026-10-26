"""一键跑完所有离线测试。

用 subprocess 逐个跑而不是 import 进来跑，理由有两个：
1) 每个测试文件都是「直接执行」的自测脚本风格，互相之间的 sys.modules 状态
   （比如 mss 在无显示环境下初始化失败）不该互相污染。
2) 谁崩了能立刻定位到文件，而不是拿到一个被后一个用例覆盖的堆栈。

用法：
    python tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = [
    "test_paths.py",        # 路径口径：桌面定位、Windows 保留名、目标预检、后缀规则
    "test_stitch.py",       # 拼接内核（合成帧，零接缝）
    "test_probe.py",        # 滚轮探测
    "test_region.py",       # 截取区域：框选、收口、九宫格对齐、按比例改尺寸
    "test_shots.py",        # 连续截图整合：尺寸/位置对齐、页数估算、清晰度、扫描滤镜
    "test_export.py",       # 连续截图整合：docx / PDF 真的写得出来
    "test_pipeline.py",     # 编排（自动 / 手动滚动）
    "test_gui_hide.py",     # 界面：最小化时间线
    "test_shots_panel.py",  # 界面：连续截图面板（整屏/框选、顺序、联动、导出）
]

# 界面那两条需要 tkinter：嵌入版 / 精简版 Python 没有它，
# 这种环境里跳过而不是判失败 —— 采集内核本身跟 tkinter 无关。
NEEDS_TK = {"test_gui_hide.py", "test_shots_panel.py"}


def _has_tk() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    py = sys.executable
    failed: list[str] = []
    total = 0.0
    has_tk = _has_tk()

    for name in SUITES:
        path = ROOT / "tests" / name
        if not path.exists():
            print(f"\n[SKIP] {name} 不存在")
            continue
        if name in NEEDS_TK and not has_tk:
            print(f"\n[SKIP] {name}（当前解释器没有 tkinter）")
            continue
        print(f"\n===== {name} =====")
        t0 = time.perf_counter()
        proc = subprocess.run(
            [py, str(path)], cwd=str(ROOT), capture_output=True, text=True
        )
        dt = time.perf_counter() - t0
        total += dt
        out = (proc.stdout or "") + (proc.stderr or "")
        print(out.rstrip())
        if proc.returncode != 0:
            failed.append(f"{name} (exit={proc.returncode}, {dt:.1f}s)")
        else:
            print(f"  -- {dt:.1f}s")

    print("\n" + "=" * 46)
    if failed:
        print("失败：" + "；".join(failed))
        return 1
    print(f"全部通过，总耗时 {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
