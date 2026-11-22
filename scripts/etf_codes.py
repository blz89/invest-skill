# -*- coding: utf-8 -*-
"""场内基金代码 → 新浪 symbol（基金号段自己拼交易所前缀）。

2026-11-16 19:00 巡检事故后抽出（etf.py 先修、wealth.py 随后又踩同一个坑）：
stock 数据层的 `sina_symbol()` 按**股票**规则拼前缀（首位 6/9→sh、0/2/3→sz、
其余→bj），场内基金 5xxxxx（沪）/1xxxxx（深）会被拼成 bj510050 / bj159915
这类新浪基金接口不认的代码 → 每只 `_retry` 4 次≈15s，全表 200+ 只直接吃满
run.py 单步 30 分钟超时（etf 分支被杀、stage2/3 未产出、stage3_plan 停在旧日期）。

凡是拿裸代码调 `fetch_etf_hist()` 的地方，一律先过 `etf_sym()`：
5 开头=沪市基金、1 开头=深市基金，已带 sh/sz/bj 前缀的原样返回，认不出的回退数据层。
"""
from __future__ import annotations

import datasource as ds


def etf_sym(code) -> str:
    """场内基金代码 → 带交易所前缀的新浪 symbol。"""
    c = str(code).strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if not c.isdigit():
        return ds.sina_symbol(c)
    if c[0] == "5":
        return "sh" + c
    if c[0] == "1":
        return "sz" + c
    return ds.sina_symbol(c)


__all__ = ["etf_sym"]
