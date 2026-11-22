# -*- coding: utf-8 -*-
"""业务稳定壳：业务只认这里的函数名与列名，底层换源不影响上层。

修了什么（`stock.patch` 里统一做，这里只调用）
---------------------------------------------
- `'---'` / `''` → NaN：部分接口把缺失值写成字符串，直接 `to_numeric` 会报错。
- 列名中英混排 → 统一 ascii 列名（date/open/high/low/close/volume/amount/...）。
- 成交额单位不统一（元 / 万元）→ 统一成**元**。

约定：本模块所有函数**不做业务判断**（不挑池子、不算收益），只负责「干净地取到数」。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from .. import cache, codes, patch

_layer = None


def layer():
    """惰性导入取数接口层（40 个接口）。"""
    global _layer
    if _layer is None:
        from .. import endpoints as _mod
        _layer = _mod
    return _layer


def _day(d: str | dt.date | None) -> str:
    if d is None:
        return ""
    if isinstance(d, (dt.date, dt.datetime)):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "").replace("/", "").strip()


# ---------------------------------------------------------------- 日线
def stock_daily(code: str, start: str = "", end: str = "", adjust: str = "qfq",
                kind: str | None = None) -> pd.DataFrame:
    """个股日线（前复权默认）。列：date open high low close volume amount turnover_pct"""
    sym = codes.bare(code)
    params = {"fn": "stock_zh_a_hist", "code": sym, "s": _day(start), "e": _day(end), "adj": adjust}

    def load():
        return layer().stock_zh_a_hist(symbol=sym, period="daily",
                                    start_date=_day(start) or "19900101",
                                    end_date=_day(end) or _day(dt.date.today()), adjust=adjust)

    df = cache.frame("stock_daily", params, load, source="stock_zh_a_hist")
    return patch.stdout_frames(df, kind="daily")


def index_daily(symbol: str = "000300", start: str = "", end: str = "",
                kind: str | None = None) -> pd.DataFrame:
    """指数日线：列 date open high low close volume amount（`kind` 仅为接口签名兼容）。"""
    params = {"fn": "stock_zh_index_daily", "symbol": symbol, "s": _day(start), "e": _day(end)}

    def load():
        return layer().stock_zh_index_daily(symbol="sh000300" if symbol.startswith("000") else symbol)

    df = cache.frame("index_daily", params, load, source="stock_zh_index_daily")
    return patch.stdout_frames(df, kind="index")


def etf_daily(symbol: str = "510300", start: str = "", end: str = "", adjust: str = "qfq") -> pd.DataFrame:
    params = {"fn": "fund_etf_hist_em", "code": symbol, "s": _day(start), "e": _day(end), "adj": adjust}

    def load():
        return layer().fund_etf_hist_em(symbol=symbol, period="daily",
                                     start_date=_day(start) or "19900101",
                                     end_date=_day(end) or _day(dt.date.today()),
                                     adjust=adjust)

    df = cache.frame("etf_daily", params, load, source="fund_etf_hist_em")
    return patch.stdout_frames(df, kind="daily")


# ---------------------------------------------------------------- 日历 / 利率
def trade_calendar() -> pd.DataFrame:
    """交易日历（新浪源，含未来日期）。列 date / is_open(1/0)"""
    def load():
        return layer().tool_trade_date_hist_sina()

    df = cache.frame("trade_calendar", {}, load, source="trade_date_hist_sina")
    df = df.rename(columns={"trade_date": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    df["is_open"] = 1
    return df.reset_index(drop=True)


def repo_rate(symbol: str = "204001") -> pd.DataFrame:
    """交易所逆回购日线（GC001=sh204001 / GC007=sh204007 / R-007=sz131801）。"""
    sec = symbol if symbol.startswith(("sh", "sz")) else (
        "sh" + symbol if symbol[:2] in ("20", "19") else "sz" + symbol)
    params = {"fn": "bond_zh_hs_daily", "code": sec}

    def load():
        return layer().bond_zh_hs_daily(symbol=sec)

    df = cache.frame("repo_daily", params, load, source="bond_zh_hs_daily")
    df = df.rename(columns={"日期": "date", "date": "date", "收盘": "close",
                            "开盘": "open", "最高": "high", "最低": "low"})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df = df.dropna(subset=["date"]).sort_values("date")
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- 货基 / 基金
def money_fund_rates() -> pd.DataFrame:
    """546 只货基的 T-1 万份收益 + 7 日年化（列名带日期，取最后一列）。"""
    def load():
        return layer().fund_money_fund_daily_em()

    df = cache.frame("money_fund_daily", {}, load, source="fund_money_fund_daily_em")
    return patch.money_fund_frames(df)


def fund_nav(code: str, indicator: str = "单位净值走势") -> pd.DataFrame:
    """场外基金净值走势：date / nav / acc_nav（按 indicator 选单位或累计）。"""
    sym = codes.bare(code)
    params = {"fn": "fund_open_fund_info_em", "code": sym, "ind": indicator}

    def load():
        return layer().fund_open_fund_info_em(symbol=sym, indicator=indicator)

    df = cache.frame("fund_nav", params, load, source="fund_open_fund_info_em")
    return patch.stdout_frames(df, kind="fund")
