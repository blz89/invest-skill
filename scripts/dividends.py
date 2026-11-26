# -*- coding: utf-8 -*-
"""分红方案取数 → 交给 ``paper.apply_dividends`` 入账。

为什么要单独一层：``paper.py`` 只记账、不联网（纯标准库）；取数放这里，
巡检（每个工作日）和周决策调同一套口径，避免两边算出不同的分红金额。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402,F401  先 import config：它把 stock 数据层注入 sys.modules

import datasource  # noqa: E402


def _d(x) -> str:
    s = str(x or "")[:10]
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        return ""
    return s


def _f(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x  # NaN → 0


def map_for(codes, as_of: str, lookback_days: int = 30) -> dict:
    """``{code: [{"ex_date","cash_per_share","share_ratio"}]}``（除权日落在窗口内）。

    ``lookback_days`` 只是兜底窗口：账本 ``meta.dividend_paid`` 已记着入账过的
    除权日，正常不会重复；回看 30 天避免每天把十年的分红全拉一遍。
    取数失败只跳过该只，不抛——分红不该拖垮记账。
    """
    end = _d(as_of)
    if not end:
        return {}
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=max(1, int(lookback_days)))).isoformat()
    out: dict = {}
    for raw in codes or []:
        code = str(raw or "").strip().zfill(6)
        if not code:
            continue
        try:
            df = datasource.fetch_dividends(code)
        except Exception:  # noqa: BLE001 - 单只取不到就跳过
            continue
        if df is None or len(df) == 0:
            continue
        rows = []
        for _, r in df.iterrows():
            ex = _d(r.get("除权除息日"))
            if not ex or ex > end or ex < start:
                continue
            cash = round(_f(r.get("每10股派息")) / 10.0, 6)
            song = _f(r.get("每10股送转"))
            if cash <= 0 and song <= 0:
                continue
            rows.append({"ex_date": ex, "cash_per_share": cash, "share_ratio": song})
        if rows:
            out[code] = rows
    return out
