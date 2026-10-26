#!/usr/bin/env python
"""LongShot2PDF 命令行入口：不开图形界面也能跑完整条流水线。

为什么要有 CLI（项目已有 GUI 的前提下）：
1) 批处理 / 计划任务里无人值守地跑；
2) 调试时参数改动只有一行命令，比在 GUI 里点控件快得多；
3) 本机没有可用的目标滚动窗口时，`--dry-run` / `--selftest` 仍能验证
   参数解析、路径处理和 PDF 合成是否正确 —— 这是集成前唯一能做的事。

退出码约定（脚本可依赖）：
    0    成功
    2    输入问题：区域非法 / 探测判定为不可滚动 / 参数互斥冲突 / 用户取消框选
    3    运行异常：截图、拼接、写 PDF 过程中的意外错误
    130  被 Ctrl+C 中断，但**已经导出半成品 PDF**（和 shell 的 SIGINT 约定一致）
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Tuple

# 直接 `python cli.py` 时 sys.path[0] 是本文件所在目录，
# 但 `python -m` 或从别的 cwd 调用的场景下 src 包可能找不到，显式兜一下。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 纯标准库，不碰 numpy/mss，所以放在顶层不影响 `--help` 的可用性
from src import paths  # noqa: E402

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_RUNTIME_ERROR = 3
EXIT_INTERRUPTED = 130

# 参数边界。写在一处，argparse 校验和 --dry-run 的说明共用同一份。
MIN_OVERLAP, MAX_OVERLAP = 0.02, 0.6
MAX_SETTLE_MS = 5000
MIN_REGION_SIDE = 40  # 与 GUI 框选的最小尺寸保护保持一致
MIN_RATIO, MAX_RATIO = 0.3, 5.0

DEFAULTS = {
    "overlap": 0.15,
    "settle_ms": 180,
    "quality": 85,
    "page_ratio": 1.414,
    "max_frames": 20000,
    "scroll": "auto",
    "idle_timeout": 60.0,
}

# 用 typing.Tuple 而不是内置 tuple[...]：别名是**运行期求值**的，写 tuple[...]
# 会让整个模块在 Python 3.8 上 import 失败（3.9 才支持内置泛型下标）。
Region = Tuple[int, int, int, int]  # (left, top, width, height) 物理像素


# --------------------------------------------------------------------- 进度显示


class ProgressLine:
    """把进度压在**同一行**覆写，避免几百帧刷屏把终端滚走。

    实现细节：用 `\\r` 回到行首而不是清屏；上一次输出比这次长时，尾部会残留
    旧字符，所以用空格补齐到历史最大宽度（不能只补到本次长度）。

    输出被重定向到文件/管道时（`isatty()` 为假）不做 `\\r` 覆写，改为每 25 帧
    或状态变化时输出一行 —— 否则日志里会是一整坨用回车拼起来的天书，
    而且 20000 帧的场景会写出巨量字符。
    """

    def __init__(self, enabled: bool = True, stream=None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stdout
        self.overwrite = bool(getattr(self.stream, "isatty", lambda: False)())
        self._width = 0
        self._last = ""
        self._last_note = ""
        self._dirty = False

    def __call__(self, frames: int, pages: int, rows: int, note: str) -> None:
        if not self.enabled:
            return
        note = " ".join(str(note).split())  # 去掉换行，否则单行覆写会失效
        text = f"帧 {frames} | 页 {pages} | 行 {rows} | {note}"

        if not self.overwrite:
            if frames % 25 == 0 or note != self._last_note:
                self.stream.write(text + "\n")
                self.stream.flush()
            self._last_note = note
            self._last = text
            self._dirty = False
            return

        pad = max(0, self._width - len(text))
        self.stream.write("\r" + text + " " * pad)
        self.stream.flush()
        self._width = max(self._width, len(text))
        self._last = text
        self._last_note = note
        self._dirty = True

    def close(self, final: str | None = None) -> None:
        """收尾换行。失败或中断时也要调用，否则后续报错会接在进度条后面。"""
        if not self.enabled:
            return
        note = " ".join(str(final if final is not None else self._last).split())
        if not self.overwrite:
            # 非终端场景没有「同一行」的概念，但收尾那句提示不能丢（用户要看到
            # 「正在导出半成品」这类关键信息），只在内容确实变化时补一行。
            if final is not None and note and note != self._last_note:
                self.stream.write(note + "\n")
                self.stream.flush()
                self._last_note = note
            return
        if self._dirty:
            self.stream.write("\r" + note + " " * max(0, self._width - len(note)) + "\n")
            self.stream.flush()
            self._dirty = False


# --------------------------------------------------------------------- 参数解析


def _type_overlap(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"需要一个 {MIN_OVERLAP}~{MAX_OVERLAP} 之间的小数，收到 '{text}'"
        ) from None
    if not (MIN_OVERLAP <= value <= MAX_OVERLAP):
        raise argparse.ArgumentTypeError(
            f"重叠比例必须在 {MIN_OVERLAP}~{MAX_OVERLAP} 之间（默认 0.15），收到 {value}。"
            "重叠是拼接的必要条件：为 0 会断连，过大则白滚很多次"
        )
    return value


def _type_int_range(lo: int, hi: int, name: str, hint: str = ""):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} 需要整数，收到 '{text}'") from None
        if not (lo <= value <= hi):
            raise argparse.ArgumentTypeError(
                f"{name} 必须在 {lo}~{hi} 之间，收到 {value}" + (f"。{hint}" if hint else "")
            )
        return value

    return parse


def _type_ratio(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"页比例需要小数（默认 1.414 = A4 纵向），收到 '{text}'") from None
    if not (MIN_RATIO <= value <= MAX_RATIO):
        raise argparse.ArgumentTypeError(
            f"页比例（页高/页宽）必须在 {MIN_RATIO}~{MAX_RATIO} 之间，收到 {value}"
        )
    return value


def _type_region(text: str) -> Region:
    """解析 `x,y,w,h`，坐标单位是**物理像素**（和 mss 一致）。"""
    parts = [p.strip() for p in text.replace("，", ",").replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"区域要写成 x,y,w,h 四个整数（例如 100,200,1200,900），收到 '{text}'"
        )
    try:
        left, top, width, height = (int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"区域里的四个值都必须是整数，收到 '{text}'"
        ) from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(f"区域宽高必须为正数，收到宽 {width} × 高 {height}")
    if width < MIN_REGION_SIDE or height < MIN_REGION_SIDE:
        raise argparse.ArgumentTypeError(
            f"区域太小（{width}×{height}），宽高至少 {MIN_REGION_SIDE}px，"
            "否则既没有重叠余量也容易只截到空白"
        )
    return (left, top, width, height)


# --------------------------------------------------------------------- 区域来源


def _coerce_region(value) -> Region | None:
    """把 winapi.virtual_screen() 的各种可能返回值统一成 (l, t, w, h)。

    该函数由 AG-A 新增，尚在演进，可能返回 tuple、dict 或带属性的对象，
    这里做防御式兼容 —— 拿不到就返回 None，让调用方退回 mss。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        keys = ("left", "top", "width", "height")
        if all(k in value for k in keys):
            return tuple(int(value[k]) for k in keys)  # type: ignore[return-value]
        return None
    if hasattr(value, "left") and hasattr(value, "width"):
        try:
            return (
                int(value.left),
                int(value.top),
                int(value.width),
                int(value.height),
            )
        except Exception:  # noqa: BLE001
            return None
    try:
        parts = tuple(int(v) for v in value)
    except Exception:  # noqa: BLE001
        return None
    if len(parts) != 4:
        return None
    return parts  # type: ignore[return-value]


def virtual_screen_region() -> Region:
    """取整个虚拟桌面（含左侧/上方副屏的负坐标）。

    首选 AG-A 的 `winapi.virtual_screen()`；它还没落地或行为变动时，
    退回 `mss.monitors[0]`（mss 的第 0 个 monitor 就是全桌面合并矩形）。
    """
    try:
        from src import winapi

        fn = getattr(winapi, "virtual_screen", None)
        if callable(fn):
            got = _coerce_region(fn())
            if got and got[2] > 0 and got[3] > 0:
                return got
    except Exception:  # noqa: BLE001 - 兜底优先，任何异常都不该阻止回退
        pass

    try:
        # 与 src/capture.py 共用同一个会话工厂：mss 10.2 起 mss.mss() 已弃用，
        # 两处各写一份最容易出现「一处理了另一处又退回去」。
        from src.capture import new_session

        with new_session() as sct:
            mon = sct.monitors[0]
            return (
                int(mon["left"]),
                int(mon["top"]),
                int(mon["width"]),
                int(mon["height"]),
            )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"拿不到虚拟桌面尺寸（winapi 与 mss 都失败）：{type(exc).__name__}: {exc}"
        ) from exc


def resolve_region(args) -> Region:
    """按 `--region` / `--select` / `--fullscreen` 得到最终区域。"""
    if args.region is not None:
        return tuple(args.region)  # type: ignore[return-value]

    if args.fullscreen:
        return virtual_screen_region()

    if args.select:
        # gui 模块在无图形环境下 import 也不炸（tkinter 是延迟导入的），
        # 所以这里可以放心 import；真正的报错会发生在调用时。
        import gui

        region = gui.select_region()
        if region is None:
            raise _InputError("已取消框选，未开始采集")
        return tuple(int(v) for v in region)  # type: ignore[return-value]

    raise _InputError(
        "必须指定截取区域：--region x,y,w,h（直接给坐标）"
        " 或 --select（全屏框选） 或 --fullscreen（整个桌面），三选一"
    )


class _InputError(Exception):
    """输入类问题 → 退出码 2。用异常而不是直接 sys.exit，方便上层统一收尾。"""


class _SkipMock(Exception):
    """自测里「要用假屏幕但仓库里没有 tests/」的情形，跳过而不是判失败。

    打包成 exe 之后 tests/ 不再随包走，B/C 两段注定跑不起来 ——
    但那不是故障：A 段已经把「JPEG → PDF 零重编码」这条链路验过了。
    记成失败会让打包版的 --selftest 永远红着，反而没人看。
    """


# --------------------------------------------------------------------- 配置构造


def make_config(region: Region, out_path: Path, args) -> tuple[object, list[str]]:
    """构造 CaptureConfig。

    返回值第二项是「当前 pipeline 不认识的参数名」——编排层正在并行重写，
    字段可能比我这边少（例如 max_pages），这时不该直接 TypeError 崩掉，
    而是过滤掉并在 stderr 提示一句。
    """
    from src.pipeline import CaptureConfig

    desired = {
        "region": region,
        "out_path": Path(out_path),
        "overlap": args.overlap,
        "settle_ms": args.settle_ms,
        "quality": args.quality,
        "page_ratio": args.page_ratio,
        "strip_top": args.strip_top,
        "strip_bottom": args.strip_bottom,
        "max_frames": args.max_frames,
        "max_pages": args.max_pages,
        "keep_temp": args.keep_temp,
        "scroll": "manual" if getattr(args, "manual", False) else "auto",
        "idle_timeout": float(getattr(args, "idle_timeout", 60.0) or 0.0),
    }
    fields = {f.name for f in dataclasses.fields(CaptureConfig)}
    ignored = sorted(set(desired) - fields)
    cfg = CaptureConfig(**{k: v for k, v in desired.items() if k in fields})
    return cfg, ignored


def _pipeline_accepts(path: str) -> bool:
    from src.pipeline import run

    try:
        return path in inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------- 运行与中断


def run_with_interrupt(cfg, progress: ProgressLine) -> tuple[object, bool]:
    """在后台线程跑 `run()`，主线程只负责「等」。

    为什么不让主线程直接调用 run()：
    Ctrl+C 抛出的 KeyboardInterrupt 是 BaseException，会从 run() 内部一路
    穿出去；pipeline 的 `except Exception` 抓不到它，于是 `finish()` 那段
    「残留页落盘 + 合并 PDF」根本走不到，半成品直接丢光。
    改成：子线程跑采集，主线程捕获 KeyboardInterrupt 后只置 stop 事件，
    run() 就能正常走到收尾分支，把已采到的内容导出成半成品 PDF。
    """
    from src.pipeline import run

    box: dict[str, object] = {}
    stop = threading.Event()

    def worker() -> None:
        try:
            kwargs: dict[str, object] = {
                "on_progress": progress,
                "should_stop": stop.is_set,
            }
            if _pipeline_accepts("deps"):
                kwargs["deps"] = None  # 真机运行：用默认依赖
            box["result"] = run(cfg, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 原样回传给主线程
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True, name="longshot-run")
    thread.start()

    interrupted = False
    while thread.is_alive():
        try:
            thread.join(0.2)
        except KeyboardInterrupt:
            if interrupted:
                print("\n再次收到 Ctrl+C，强制退出（临时页可能残留）", file=sys.stderr)
                raise SystemExit(EXIT_INTERRUPTED) from None
            interrupted = True
            stop.set()
            progress.close("收到 Ctrl+C，正在导出半成品 PDF…（再按一次 Ctrl+C 强制退出）")

    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("result"), interrupted


def report(result, interrupted: bool) -> int:
    """把 RunResult 变成人话 + 退出码。"""
    ok = bool(getattr(result, "ok", False))
    pdf = getattr(result, "pdf_path", None)
    pages = int(getattr(result, "pages", 0) or 0)
    frames = int(getattr(result, "frames", 0) or 0)
    rows = int(getattr(result, "rows", 0) or 0)
    size_mb = float(getattr(result, "size_mb", 0.0) or 0.0)
    message = str(getattr(result, "message", "") or "")
    stopped_early = bool(getattr(result, "stopped_early", False))

    if ok and pdf is not None:
        print(f"完成：{pages} 页 / {frames} 帧 / {rows} 行 / {size_mb:.2f} MB")
        print(f"输出：{pdf}")
        if message:
            print(f"说明：{message}")
        if interrupted or stopped_early:
            print(f"已保存半成品 {pages} 页 → {pdf}")
            return EXIT_INTERRUPTED
        return EXIT_OK

    # ---- 失败分支 ----
    detail = message or "未知原因"
    if pdf is not None and pages > 0:
        print(f"已保存半成品 {pages} 页 → {pdf}")
        if interrupted:
            return EXIT_INTERRUPTED
    print(f"失败：{detail}", file=sys.stderr)
    if _looks_like_input_problem(result, detail):
        return EXIT_BAD_INPUT
    return EXIT_RUNTIME_ERROR


def _looks_like_input_problem(result, message: str) -> bool:
    """判断失败是「输入/环境不满足」还是「程序异常」。

    优先看编排层给的 `RunResult.stage`（pipeline 目前会填 config/probe/capture）：
    - config / probe → 用户可修正的输入问题，退出码 2
    - capture        → 采集过程中的意外，退出码 3
    没有 stage 时（旧接口）退回关键词匹配；认不出来一律算运行异常（3），
    避免把真 bug 报成 2 而让脚本误以为「改个参数就能好」。
    """
    stage = getattr(result, "stage", None)
    if stage in ("config", "probe", "validate", "region", "input"):
        return True
    # "done" 是 RunResult 的默认值，等价于「编排层没说是哪一步」，继续看关键词
    if stage and stage != "done":
        return False
    keywords = ("不可滚", "不能滚", "滚轮", "探测", "区域", "非法", "无效", "取消")
    return any(k in message for k in keywords)


# --------------------------------------------------------------------- 干跑


def print_dry_run(region: Region, out_path: Path, args, ignored: list[str]) -> None:
    left, top, width, height = region
    page_h = int(round(width * args.page_ratio))
    step = max(1, int(round(height * (1.0 - args.overlap))))
    frames_per_page = max(1, -(-page_h // step))  # 向上取整
    manual = bool(getattr(args, "manual", False))

    print("== 干跑（不截图、不滚轮、不写文件） ==")
    print(f"区域      : left={left} top={top} width={width} height={height}（物理像素）")
    print(f"输出       : {out_path}")
    print(f"单页像素   : {width} × {page_h}（页比例 {args.page_ratio}）")
    if manual:
        print("滚动方式   : 手动 —— 程序不投滚轮，你自己滚，它只抓帧拼接")
        print(
            f"收尾条件   : 按 ESC / 点停止；或 "
            + (f"{args.idle_timeout:.0f}s 无滚动自动收尾" if args.idle_timeout else "一直等你停")
        )
        print(
            f"提醒       : 每次滚动别超过 {height}px（一屏），超过了那一帧接不上，"
            "中间会漏一段"
        )
    else:
        print(
            f"滚动步长   : 视口 {height}px × (1 - 重叠 {args.overlap}) ≈ {step}px"
            f" → 约 {frames_per_page} 帧出 1 页"
        )
    print(
        "估算页数   : 取决于目标文档总高度（无法预知），"
        f"每页 {page_h}px；若文档高 H px，页数 ≈ ceil(H / {page_h})"
    )
    if args.max_pages:
        print(f"页数上限   : {args.max_pages} 页，达到后自动收尾")
    print(f"帧数上限   : {args.max_frames}")
    print(f"稳定等待   : {args.settle_ms} ms    JPEG 质量: {args.quality}")
    if args.strip_top or args.strip_bottom:
        print(f"剔除固定条 : 顶部 {args.strip_top}px / 底部 {args.strip_bottom}px")
    print(f"保留临时页 : {'是（调试）' if args.keep_temp else '否'}")
    for name in ignored:
        print(f"注意       : 当前 pipeline 尚未支持 --{name.replace('_', '-')}，本次忽略")
    print(
        "将执行     : "
        + (
            "采集首帧 → 循环抓帧并比对拼接（等你滚） → 分页 → 合成 PDF"
            if manual
            else "探测能否滚动 → 采集首帧 → 校准滚轮步长 → 循环采集并分页 → 合成 PDF"
        )
    )


# --------------------------------------------------------------------- 端到端自测


def _load_mock_frames():
    import importlib.util

    path = PROJECT_ROOT / "tests" / "mock_frames.py"
    if not path.exists():
        raise _SkipMock(f"没有 mock 数据模块（{path}），B/C 两段跳过")
    name = "_ls2pdf_mock_frames"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise _SkipMock(f"无法加载 mock 数据模块（{path}），B/C 两段跳过")
    module = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules：模块里的 @dataclass 需要在自己模块的命名空间里
    # 找到 sys.modules[cls.__module__]，否则会抛 'NoneType' object has no attribute '__dict__'
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _ScreenDeps:
    """把 VirtualScreen 伪装成 pipeline 需要的依赖集合（旧版 pipeline 用）。"""

    def __init__(self, screen) -> None:
        self.screen = screen

    def grab(self, region):
        return self.screen.grab(region)

    def close(self) -> None:
        self.screen.close()

    def wheel(self, steps: int) -> None:
        self.screen.wheel(steps)

    def set_cursor(self, x: int, y: int) -> None:
        self.screen.set_cursor(x, y)

    def esc(self) -> bool:
        return self.screen.esc()


def _build_pipeline_deps(screen):
    """按 pipeline.Deps 的字段名注入 mock 依赖；字段不认识就返回 None。

    字段名以编排层为准（grabber/wheel/set_cursor/esc/sleep/enable_dpi/
    save_cursor/restore_cursor）。一旦出现没见过的字段，宁可退回 monkeypatch
    也不要瞎猜语义 —— 猜错会让自测结论完全失去意义。
    """
    from src import pipeline

    deps_cls = getattr(pipeline, "Deps", None)
    if deps_cls is None or not dataclasses.is_dataclass(deps_cls):
        return None
    aliases = {
        "grabber": screen,
        "wheel": screen.wheel,
        "set_cursor": screen.set_cursor,
        "set_cursor_pos": screen.set_cursor,
        "esc": screen.esc,
        "esc_pressed": screen.esc,
        "sleep": lambda _seconds: None,  # 虚拟屏幕没有渲染延迟，跳过等待加快自测
        "enable_dpi": lambda: None,
        "enable_dpi_awareness": lambda: None,
        "save_cursor": lambda: None,
        "restore_cursor": lambda _pos: None,
    }
    values = {}
    for field in dataclasses.fields(deps_cls):
        if field.name not in aliases:
            return None
        values[field.name] = aliases[field.name]
    return deps_cls(**values)


def _selftest_args():
    """构造一份与 argparse 结果字段同形的对象，供 make_config 复用。"""

    class Args:
        pass

    args = Args()
    for key, value in DEFAULTS.items():
        setattr(args, key, value)
    args.strip_top = 0
    args.strip_bottom = 0
    args.max_pages = 0
    args.keep_temp = False
    return args


def _run_pipeline(screen, region: Region, out_path: Path, *, should_stop=None, progress=None):
    """用 mock 屏幕跑一次 pipeline.run，返回 (result, ignored, 注入方式)。

    注入方式优先走 pipeline.Deps；老版本没有 Deps 时退回 monkeypatch
    （把 pipeline.Grabber 和 winapi 的几个函数临时换掉，跑完立刻还原）。
    """
    from src import pipeline, winapi

    cfg, ignored = make_config(region, out_path, _selftest_args())
    kwargs: dict[str, object] = {
        "on_progress": progress,
        "should_stop": should_stop or (lambda: False),
    }

    deps = _build_pipeline_deps(screen)
    patched: list[tuple[object, str, object]] = []
    if deps is not None:
        kwargs["deps"] = deps
        mode = "pipeline.Deps"
    else:
        pairs = [
            (pipeline, "Grabber", lambda *a, **k: _ScreenDeps(screen)),
            (winapi, "wheel", screen.wheel),
            (winapi, "set_cursor_pos", screen.set_cursor),
            (winapi, "esc_pressed", screen.esc),
            (winapi, "enable_dpi_awareness", lambda: None),
        ]
        for obj, name, value in pairs:
            if hasattr(obj, name):
                patched.append((obj, name, getattr(obj, name)))
                setattr(obj, name, value)
        mode = "monkeypatch（旧版 pipeline，无 Deps）"

    try:
        result = pipeline.run(cfg, **kwargs)
    finally:
        for obj, name, value in patched:
            setattr(obj, name, value)
    return result, ignored, mode


def selftest() -> int:
    """离线端到端自测：没有真实滚动窗口也能验证整条链路。

    两段：
    A. pdfwriter —— 3 张合成 JPEG → PDF，页数、页序、零重编码三项断言；
    B. pipeline —— VirtualScreen 假装成屏幕，跑 probe→stitch→pdf 全流程。
    """
    import numpy as np
    from PIL import Image

    from src import pdfwriter

    failures: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="ls2pdf_selftest_"))
    print(f"== 自测 · 工作目录 {workdir} ==")

    # ---------- A. pdfwriter：页数 / 页序 / 零重编码 ----------
    print("\n[A] pdfwriter：3 张合成 JPEG → PDF")
    colors = [(220, 30, 30), (30, 180, 60), (40, 70, 220)]
    jpegs: list[Path] = []
    for idx, color in enumerate(colors, start=1):
        arr = np.zeros((900, 640, 3), dtype=np.uint8)
        arr[:, :] = color
        arr[420:, :] = 250  # 下半段纯白，便于人工核对页序
        path = workdir / f"page_{idx:03d}.jpg"
        Image.fromarray(arr).save(path, quality=88, subsampling=0)
        jpegs.append(path)

    out_pdf = workdir / "selftest_3pages.pdf"
    try:
        pdfwriter.jpegs_to_pdf(jpegs, out_pdf)
        count = pdfwriter.pdf_page_count(out_pdf)
        print(f"    生成: {out_pdf.name}  体积 {out_pdf.stat().st_size} B")
        print(f"    pdf_page_count = {count}（期望 3）")
        if count != 3:
            failures.append(f"pdf_page_count={count}，期望 3")

        # 页序 + 零重编码：img2pdf 直塞 JPEG 码流，PDF 里的图像字节应与源文件完全一致。
        # 注意必须走 `/XObject` 的 get_data() 取**原始码流**；pypdf 的 page.images
        # 会做一层图像对象包装，个别字节会被它改写，用它断言会误报「被重编码」。
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(out_pdf))
            order_ok = True
            for i, page in enumerate(reader.pages):
                xobjects = page["/Resources"]["/XObject"]
                datas = [xobjects[key].get_data() for key in xobjects]
                if len(datas) != 1:
                    failures.append(f"第 {i + 1} 页内嵌图像数 {len(datas)}，期望 1")
                    order_ok = False
                    break
                same = datas[0] == jpegs[i].read_bytes()
                print(
                    f"    第 {i + 1} 页内嵌图像 {len(datas[0])} B，"
                    f"与源 {jpegs[i].name} {'字节一致（零重编码）' if same else '不一致'}"
                )
                if not same:
                    order_ok = False
                    failures.append(f"第 {i + 1} 页图像字节与源文件不一致（可能被重编码或页序错）")
            if order_ok:
                print("    页序 & 零重编码：通过")
        except ImportError:
            print("    跳过页序断言：未安装 pypdf")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"pdfwriter 异常：{type(exc).__name__}: {exc}")

    # ---------- B. 端到端 ----------
    print("\n[B] pipeline 端到端（VirtualScreen 模拟滚动）")
    try:
        mock = _load_mock_frames()
        doc = mock.make_document(width=800, height=9000, seed=20261025)
        view_h = 600
        screen = mock.VirtualScreen(doc, view_h, unit_px=100)
        region: Region = (0, 0, doc.shape[1], view_h)
        e2e_pdf = workdir / "selftest_e2e.pdf"
        progress = ProgressLine(enabled=True)

        result, ignored, mode = _run_pipeline(screen, region, e2e_pdf, progress=progress)
        progress.close()

        pages = int(getattr(result, "pages", 0) or 0)
        rows = int(getattr(result, "rows", 0) or 0)
        frames = int(getattr(result, "frames", 0) or 0)
        print(f"    注入方式：{mode}")
        print(f"    ok={result.ok} pages={pages} frames={frames} rows={rows}")
        print(f"    message={result.message}")
        if ignored:
            print(f"    未支持的参数（已忽略）：{ignored}")
        if not result.ok or not result.pdf_path:
            failures.append(f"端到端失败：{result.message}")
        else:
            count = pdfwriter.pdf_page_count(result.pdf_path)
            print(f"    PDF 页数 = {count}，对应 rows={rows}，体积 {result.pdf_path.stat().st_size} B")
            if count != pages:
                failures.append(f"端到端 PDF 页数 {count} != RunResult.pages {pages}")
            # 覆盖率：拼接总行数应等于文档高度，说明首行到末行一行没丢
            if rows < doc.shape[0] - view_h:
                failures.append(f"拼接覆盖不足：rows={rows}，文档高 {doc.shape[0]}")
    except _SkipMock as exc:
        print(f"    跳过：{exc}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"端到端异常：{type(exc).__name__}: {exc}")

    # ---------- C. 中断语义（对应退出码 130 那条路径） ----------
    print("\n[C] 中断语义：采集到第 5 帧时置 stop，期望导出半成品且退出码 130")
    try:
        mock = _load_mock_frames()
        doc = mock.make_document(width=600, height=20000, seed=99)
        view_h = 500
        screen = mock.VirtualScreen(doc, view_h, unit_px=100)
        region = (0, 0, doc.shape[1], view_h)
        stop_pdf = workdir / "selftest_stopped.pdf"

        seen = {"frames": 0}
        progress = ProgressLine(enabled=False)

        def on_progress(frames: int, pages: int, rows: int, note: str) -> None:
            seen["frames"] = frames
            progress(frames, pages, rows, note)

        result, _ignored, _mode = _run_pipeline(
            screen, region, stop_pdf,
            should_stop=lambda: seen["frames"] >= 5,
            progress=on_progress,
        )
        stopped_early = bool(getattr(result, "stopped_early", False))
        pages = int(getattr(result, "pages", 0) or 0)
        print(f"    ok={result.ok} stopped_early={stopped_early} pages={pages} message={result.message}")
        if not (result.ok and stopped_early and pages > 0 and result.pdf_path):
            failures.append("中断后未导出半成品（ok/stopped_early/pages 至少一项不符）")
        else:
            code = report(result, interrupted=True)
            print(f"    report() 退出码 = {code}（期望 {EXIT_INTERRUPTED}）")
            if code != EXIT_INTERRUPTED:
                failures.append(f"中断退出码 {code}，期望 {EXIT_INTERRUPTED}")
    except _SkipMock as exc:
        print(f"    跳过：{exc}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"中断语义异常：{type(exc).__name__}: {exc}")

    # ---------- 结论 ----------
    print("\n== 自测结论 ==")
    if failures:
        for item in failures:
            print(f"  [FAIL] {item}")
        print(f"  共 {len(failures)} 项失败，工作目录保留：{workdir}")
        return EXIT_RUNTIME_ERROR
    print("  全部通过")
    shutil.rmtree(workdir, ignore_errors=True)
    return EXIT_OK


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="把任意可滚动界面自动滚动截取、无缝拼接，输出为多页 PDF。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python cli.py --region 100,200,1200,900 --out 文档.pdf\n"
            "  python cli.py --select --overlap 0.2\n"
            "  python cli.py --select --manual              # 你自己滚，它只负责拼\n"
            "  python cli.py --fullscreen --max-pages 300 --quiet\n"
            "  python cli.py --dry-run --region 0,0,800,600 --out /tmp/x.pdf\n"
            "  python cli.py --selftest\n"
            "\n退出码：0 成功 / 2 输入问题或不可滚动 / 3 运行异常 / 130 中断但已出半成品"
        ),
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--region", type=_type_region, metavar="X,Y,W,H",
                     help="截取区域，物理像素（left,top,width,height）")
    src.add_argument("--select", action="store_true",
                     help="打开全屏半透明层，鼠标拖拽框选区域")
    src.add_argument("--fullscreen", action="store_true",
                     help="截取整个虚拟桌面（含副屏）")

    parser.add_argument("--out", type=Path, metavar="PDF",
                        help="输出 PDF 路径（默认放到桌面 longshot_时间戳.pdf）")
    parser.add_argument("--overlap", type=_type_overlap, default=DEFAULTS["overlap"],
                        metavar="R", help="相邻帧重叠比例，%.2f~%.2f（默认 0.15）" % (MIN_OVERLAP, MAX_OVERLAP))
    parser.add_argument("--settle-ms", dest="settle_ms",
                        type=_type_int_range(0, MAX_SETTLE_MS, "稳定等待(ms)"),
                        default=DEFAULTS["settle_ms"], metavar="MS",
                        help="滚动后等待渲染稳定的毫秒数（默认 180）")
    parser.add_argument("--quality", type=_type_int_range(1, 100, "JPEG 质量"),
                        default=DEFAULTS["quality"], metavar="Q",
                        help="JPEG 质量 1~100（默认 85，文字截图肉眼无损且体积约 95 的 40%%）")
    parser.add_argument("--page-ratio", dest="page_ratio", type=_type_ratio,
                        default=DEFAULTS["page_ratio"], metavar="R",
                        help="页高/页宽，1.414=A4 纵向（默认）")
    parser.add_argument("--strip-top", dest="strip_top",
                        type=_type_int_range(0, 10000, "顶部剔除像素"),
                        default=0, metavar="PX", help="拼接前裁掉顶部固定条（表头）")
    parser.add_argument("--strip-bottom", dest="strip_bottom",
                        type=_type_int_range(0, 10000, "底部剔除像素"),
                        default=0, metavar="PX", help="拼接前裁掉底部固定条（状态栏/悬浮按钮）")
    parser.add_argument("--max-frames", dest="max_frames",
                        type=_type_int_range(1, 10_000_000, "最大帧数"),
                        default=DEFAULTS["max_frames"], metavar="N",
                        help="安全上限，防止死循环（默认 20000）")
    parser.add_argument("--max-pages", dest="max_pages",
                        type=_type_int_range(0, 100_000, "最大页数"),
                        default=0, metavar="N", help="达到页数自动收尾，0=不限（默认）")
    parser.add_argument("--keep-temp", dest="keep_temp", action="store_true",
                        help="保留中间页 JPEG（调试用，正常会清理）")
    parser.add_argument("--manual", dest="manual", action="store_true",
                        help="手动滚动：程序不投滚轮，你自己滚，它只负责抓帧拼接"
                             "（适合要看一眼再滚、或程序不吃合成滚轮）")
    parser.add_argument("--idle-timeout", dest="idle_timeout", type=float,
                        default=DEFAULTS["idle_timeout"], metavar="S",
                        help="仅 --manual：多少秒没有滚动就自动收尾，0 = 一直等"
                             "（默认 60）")
    parser.add_argument("--quiet", action="store_true", help="不打印进度行")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="只打印将要做什么（区域/估算页数/输出路径），不截图")
    parser.add_argument("--selftest", action="store_true",
                        help="离线端到端自测（虚拟屏幕 + 合成 PDF），不碰真实桌面")
    return parser


def default_out_path() -> Path:
    """默认输出路径：桌面（跟随 OneDrive / XDG 重定向），没有就退回主目录。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return paths.desktop_dir() / f"longshot_{stamp}.pdf"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        region = resolve_region(args)
        out_path = Path(args.out) if args.out else default_out_path()
        out_path = paths.with_suffix_for(out_path, ".pdf")
        # 预检：保留设备名（CON 之类会静默写出 0 字节）、结尾空格、上级是文件
        why = paths.check_target(out_path)
        if why:
            raise _InputError(why)
        cfg, ignored = make_config(region, out_path, args)

        if args.dry_run:
            print_dry_run(region, out_path, args, ignored)
            return EXIT_OK

        for name in ignored:
            print(
                f"注意：当前 pipeline 尚未支持 --{name.replace('_', '-')}，本次忽略",
                file=sys.stderr,
            )

        progress = ProgressLine(enabled=not args.quiet)
        print(f"区域 {region}  →  {out_path}")
        result, interrupted = run_with_interrupt(cfg, progress)
        progress.close()
        return report(result, interrupted)
    except _InputError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except RuntimeError as exc:
        # 拿不到桌面尺寸、gui 不可用等环境问题
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except KeyboardInterrupt:
        # 兜底：理论上 run_with_interrupt 已经处理了，这里覆盖「启动阶段被 Ctrl+C」
        print("\n已取消", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001
        print(f"运行异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
