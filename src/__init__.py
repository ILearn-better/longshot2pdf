"""LongShot2PDF —— 任意界面滚动截图 → 无缝长图 → 多页 PDF。"""

from .pipeline import CaptureConfig, Deps, RunResult, run

__version__ = "0.2.0"
__all__ = ["CaptureConfig", "Deps", "RunResult", "run"]
