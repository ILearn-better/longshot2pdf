"""LongShot2PDF —— 任意界面滚动截图 → 无缝长图 → 多页 PDF。"""

from .pipeline import CaptureConfig, RunResult, run

__version__ = "0.1.0"
__all__ = ["CaptureConfig", "RunResult", "run"]
