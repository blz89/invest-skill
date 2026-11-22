# -*- coding: utf-8 -*-
"""技能与 stock 数据层之间的适配（invest 共用）。

`stock.skilldata.datasource` 是**唯一实现**，但它不在 import 时读配置
（同一进程可能承载两个技能的不同 WORK_DIR）。技能 config 末尾调用
`install(flavor, config_module)` 注入配置，并把实现挂到 `sys.modules`
的 `datasource` / `codes` / `fuyao` 名下——策略代码照旧 `import datasource`，
技能目录里不再保留同名副本。
"""
from __future__ import annotations


def load(config_module, flavor="invest"):
    """注入配置并返回实现模块。"""
    from . import datasource as impl

    impl.init(config_module, flavor=flavor)
    return impl


def install(flavor, config_module):
    """注入配置并把数据层挂到技能惯用的模块名上（幂等）。"""
    import sys

    from .. import codes as codes_mod
    from ..sources import fuyao as fuyao_mod

    impl = load(config_module, flavor=flavor)
    sys.modules.setdefault("datasource", impl)
    sys.modules.setdefault("codes", codes_mod)
    sys.modules.setdefault("fuyao", fuyao_mod)
    return impl


__all__ = ["load", "install", "datasource"]
