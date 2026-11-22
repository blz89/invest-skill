# -*- coding: utf-8 -*-
r"""stock —— 自建行情/财务数据脚本组。

统一入口：
    import sys; sys.path.insert(0, r"<stock 仓库根目录>")
    from stock import api

命令行：
    python -m stock daily 600519 --start 2026-01-01
    python -m stock quote 600519 000300
    python -m stock doctor
"""
from __future__ import annotations

__version__ = "0.1.0"

from . import api  # noqa: F401  (方便 `from stock import api`)

__all__ = ["api", "__version__"]
