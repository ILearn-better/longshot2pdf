"""截取区域的纯算术验证：框选、收口、九宫格对齐、按比例改尺寸、输出尺寸。

这一层没有任何界面和屏幕操作，所以能离线把「拖了多少、贴到哪、改完多大」
全部验一遍 —— 界面上那几十行按钮回调只是把这些函数接起来而已。

检验：
1. 拖拽的两个方向都得到同一个区域；误触（小于下限）返回 None；副屏负坐标可用
2. clamp_region 把跑到屏幕外/过大的区域拉回来，且不改变「贴边」的意图
3. snap_region 九个锚点的落点，且**不改尺寸**（各张才能严丝合缝）
4. fit_region 的按比例联动、单边修改、结果始终在桌面内
5. output_size 的原样 / 统一宽度两种口径

直接运行：  python tests/test_region.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.regionpick import (  # noqa: E402
    MIN_SELECTION,
    MIN_SIDE,
    clamp_region,
    fit_region,
    output_size,
    rect_from_points,
    snap_region,
)

SCREEN = (0, 0, 1920, 1080)


def case_drag() -> None:
    print("\n[1] 拖拽 → 区域")
    # 往右下拖 / 往左上拖，结果必须一致（人是随手拖的，方向不固定）
    a = rect_from_points(100, 200, 700, 800)
    b = rect_from_points(700, 800, 100, 200)
    assert a == b == (100, 200, 600, 600), (a, b)
    print(f"      正反两个方向一致：{a}")

    # 误触：宽或高小于下限
    assert rect_from_points(10, 10, 10 + MIN_SELECTION - 1, 500) is None
    assert rect_from_points(10, 10, 500, 10 + MIN_SELECTION - 1) is None
    assert rect_from_points(10, 10, 10 + MIN_SELECTION, 10 + MIN_SELECTION) is not None
    print(f"      小于 {MIN_SELECTION}px 判为误触")

    # 副屏在主屏左侧：x 为负
    neg = rect_from_points(-1500, 100, -900, 700)
    assert neg == (-1500, 100, 600, 600), neg
    print(f"      负坐标（左侧副屏）：{neg}")


def case_clamp() -> None:
    print("\n[2] 收进桌面")
    # 位置超右下：贴到边缘而不是被裁掉
    reg = clamp_region((1800, 1000, 400, 300), SCREEN)
    assert reg == (1520, 780, 400, 300), reg
    print(f"      超右下 → {reg}（尺寸不变，贴边）")

    # 位置超左上
    reg = clamp_region((-500, -300, 400, 300), SCREEN)
    assert reg == (0, 0, 400, 300), reg
    print(f"      超左上 → {reg}")

    # 尺寸比桌面还大：收成桌面那么大（再大也只能截到桌面）
    reg = clamp_region((100, 100, 9999, 9999), SCREEN)
    assert reg == (0, 0, 1920, 1080), reg
    print(f"      超尺寸 → {reg}")

    # 最小边：拖到 1px 时收成 MIN_SIDE，而不是留一个 0px 的非法区域
    reg = clamp_region((10, 10, 1, 1), SCREEN)
    assert reg[2] == MIN_SIDE and reg[3] == MIN_SIDE, reg
    print(f"      极小 → {reg}")


def case_snap() -> None:
    print("\n[3] 九宫格对齐（只挪位置）")
    region = (400, 300, 800, 600)
    expect = {
        ("left", "top"): (0, 0),
        ("center", "top"): (560, 0),
        ("right", "top"): (1120, 0),
        ("left", "middle"): (0, 240),
        ("center", "middle"): (560, 240),
        ("right", "middle"): (1120, 240),
        ("left", "bottom"): (0, 480),
        ("center", "bottom"): (560, 480),
        ("right", "bottom"): (1120, 480),
    }
    seen = []
    for (hx, vy), (ex, ey) in expect.items():
        got = snap_region(region, SCREEN, hx, vy)
        assert (got[0], got[1]) == (ex, ey), f"{hx}/{vy}: {got} != {(ex, ey)}"
        assert (got[2], got[3]) == (800, 600), f"{hx}/{vy} 改了尺寸: {got}"
        seen.append(f"{hx[0]}{vy[0]}={got[0]},{got[1]}")
    print("      " + " ".join(seen))

    # 区域本身比桌面大时，先收成桌面大小再对齐，落点仍然正确
    got = snap_region((0, 0, 5000, 5000), SCREEN, "right", "bottom")
    assert got == (0, 0, 1920, 1080), got
    print(f"      超大区域 → {got}")


def case_fit() -> None:
    print("\n[4] 改尺寸（含按比例联动）")
    region = (100, 100, 800, 400)

    # 只改宽：高不动
    got = fit_region(region, SCREEN, width=1600)
    assert got == (100, 100, 1600, 400), got
    print(f"      只改宽 → {got}")

    # 锁定比例改宽：高度按 800:400 跟着变
    got = fit_region(region, SCREEN, width=1600, keep_ratio=True)
    assert got == (100, 100, 1600, 800), got
    print(f"      锁比例改宽 → {got}")

    # 锁定比例改高：宽度按比例回算
    got = fit_region(region, SCREEN, height=200, keep_ratio=True)
    assert got == (100, 100, 400, 200), got
    print(f"      锁比例改高 → {got}")

    # 两个都给时以宽为准（拖控件的中间态必须有确定行为）
    got = fit_region(region, SCREEN, width=1000, height=1000, keep_ratio=True)
    assert got[2] == 1000 and got[3] == 500, got
    print(f"      宽高同时给 → {got}（以宽为准）")

    # 改完一定要在桌面内：贴着右下角放大时，位置会被拉回来
    got = fit_region((1500, 900, 400, 300), SCREEN, width=1200, keep_ratio=True)
    assert got[0] + got[2] <= 1920 and got[1] + got[3] <= 1080, got
    print(f"      贴边放大 → {got}（仍在桌面内）")


def case_output() -> None:
    print("\n[5] 输出尺寸")
    region = (300, 200, 900, 600)
    assert output_size(region, 0) == (900, 600)
    print(f"      原样 → {output_size(region, 0)}")
    assert output_size(region, 1800) == (1800, 1200)
    print(f"      统一宽 1800 → {output_size(region, 1800)}（比例不变）")
    assert output_size(region, 300) == (300, 200)
    print(f"      统一宽 300 → {output_size(region, 300)}")


def main() -> int:
    print("=" * 64)
    print("截取区域 · 纯算术")
    print("=" * 64)
    try:
        case_drag()
        case_clamp()
        case_snap()
        case_fit()
        case_output()
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print("-" * 64)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
