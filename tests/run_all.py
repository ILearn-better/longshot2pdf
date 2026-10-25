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
SUITES = ["test_stitch.py", "test_probe.py", "test_pipeline.py"]


def main() -> int:
    py = sys.executable
    failed: list[str] = []
    total = 0.0

    for name in SUITES:
        path = ROOT / "tests" / name
        if not path.exists():
            print(f"\n[SKIP] {name} 不存在")
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
