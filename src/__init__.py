"""LongShot2PDF —— 任意界面滚动截图 → 无缝长图 → 多页 PDF。"""

__version__ = "0.8.0"

__all__ = ["CaptureConfig", "Deps", "RunResult", "run"]


def __getattr__(name: str):
    """按需导入 pipeline，别在包初始化时就 import。

    这一行改变了可用性：``from src import paths``（只要桌面定位这类纯标准库
    功能）原本会连带执行 ``src/pipeline.py``，于是把 numpy / mss 一起拖进来 ——
    在没装依赖的机器上，``python cli.py --help`` 会因为「找不到 mss」而失败，
    而这跟它要干的事毫无关系。惰性之后，只有真正取 pipeline 的符号才付这个代价。
    """
    if name in __all__:
        from . import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
