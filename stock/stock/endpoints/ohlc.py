# -*- coding: utf-8 -*-
"""通用 K 线接口。

``stock_zh_a_hist`` 走腾讯 K 线（东财 push2his 在本机不可用）；腾讯不提供成交额/换手率，
对应列留 NaN，其余列与下游约定一致。
"""
from __future__ import annotations

import pandas as pd

_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}


def stock_zh_a_hist(symbol: str = "000001", period: str = "daily",
                    start_date: str = "19700101", end_date: str = "20500101",
                    adjust: str = "") -> pd.DataFrame:
    """A 股日/周/月 K 线（成交额与换手率腾讯不给，为 NaN）。"""
    from ..sources.tencent import kline

    start = "-".join([start_date[:4], start_date[4:6], start_date[6:8]]) if len(start_date) == 8 else start_date
    end = "-".join([end_date[:4], end_date[4:6], end_date[6:8]]) if len(end_date) == 8 else end_date
    df = kline(symbol, start=start, end=end, adjust=adjust or "", period=_PERIOD.get(period, "day"))
    if df is None or df.empty:
        return pd.DataFrame(columns=["日期", "股票代码", "开盘", "收盘", "最高", "最低",
                                     "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"])
    out = pd.DataFrame({
        "日期": pd.to_datetime(df["date"]),
        "股票代码": str(symbol)[-6:],
        "开盘": pd.to_numeric(df["open"], errors="coerce"),
        "收盘": pd.to_numeric(df["close"], errors="coerce"),
        "最高": pd.to_numeric(df["high"], errors="coerce"),
        "最低": pd.to_numeric(df["low"], errors="coerce"),
        "成交量": pd.to_numeric(df["volume"], errors="coerce"),
        "成交额": pd.NA,
    })
    prev_close = out["收盘"].shift(1)
    out["振幅"] = ((out["最高"] - out["最低"]) / prev_close * 100).round(2)
    out["涨跌幅"] = ((out["收盘"] - prev_close) / prev_close * 100).round(2)
    out["涨跌额"] = (out["收盘"] - prev_close).round(2)
    out["换手率"] = pd.NA
    return out[["日期", "股票代码", "开盘", "收盘", "最高", "最低",
                "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]]
