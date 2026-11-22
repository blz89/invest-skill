# -*- coding: utf-8 -*-
"""统一门面：所有取数入口都从这里走，底层可换源。

命名空间
--------
- `api.data` —— 取数接口层（40 个接口：A 股/ETF/指数日线、货基、基金净值、逆回购，见 `endpoints/`）
- `api.em`  —— 东方财富直连（基金 F10 历史净值、批量行情快照，快）
- `api.ts`  —— 同花顺 fuyao（财务、估值、公告新闻、涨停池、指数成分…，需 key）

约定
----
1. 门面的 `daily/index_daily/etf_daily` 返回**标准列** DataFrame（date/open/high/low/close/...）；
   同花顺返回的 list[dict] 是原始结构，不做强行合并——字段体系不同，混了反而错。
2. 多源回退只发生在**语义等价**的查询上：`quote()`（快照）、`fund_lsjz()`（净值）。
3. 任何源失败都**抛出**，由调用方决定要不要兜底；门面自己不做静默降级（除了 `quote`）。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import __version__, cache, codes, config
from .sources import facade as data
from .sources import eastmoney as em
from .sources import fuyao as ts
from .sources import sina as sn
from .sources import tencent as tx

__all__ = [
    "data", "em", "ts", "tx", "sn", "cache", "codes", "config",
    "daily", "index_daily", "etf_daily", "quote", "fund_lsjz", "fund_nav",
    "money_fund_rates", "repo_rate", "trade_calendar", "financials",
    "valuation", "doctor", "__version__",
]


def _chain(name: str, fns: list, *args, **kwargs):
    """按顺序试多个源，第一个返回非空 DataFrame 的胜出；全失败则抛「汇总错误」。"""
    errs = []
    for fn in fns:
        try:
            df = fn(*args, **kwargs)
            if isinstance(df, pd.DataFrame) and not df.empty:
                df.attrs.setdefault("source", getattr(fn, "__module__", "?"))
                return df
            errs.append(f"{fn.__module__}.{fn.__name__}: empty")
        except Exception as exc:
            errs.append(f"{fn.__module__}.{fn.__name__}: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"{name} 全部源失败 → " + " | ".join(errs))


# ---------------------------------------------------------------- 日线
def daily(code: str, start: str = "", end: str = "", adjust: str = "qfq") -> pd.DataFrame:
    """个股日线（默认前复权）。源顺序：腾讯 → 新浪 → 东财 K 线 → 接口层。"""
    return _chain("daily", [tx.kline, sn.kline, em.kline, data.stock_daily],
                  code, start=start, end=end, adjust=adjust)


def index_daily(symbol: str = "000300", start: str = "", end: str = "") -> pd.DataFrame:
    """指数日线（`000300` 请写成 `000300.SH` 以显式区分沪深指数与深市股票）。"""
    return _chain("index_daily", [tx.kline, sn.kline, em.kline, data.index_daily],
                  symbol, start=start, end=end, kind="index")


def etf_daily(symbol: str = "510300", start: str = "", end: str = "", adjust: str = "qfq") -> pd.DataFrame:
    return _chain("etf_daily", [tx.kline, sn.kline, em.kline, data.etf_daily],
                  symbol, start=start, end=end, adjust=adjust)


# ---------------------------------------------------------------- 快照（多源）
def quote(codes_in, source: str | None = None) -> pd.DataFrame:
    """批量行情快照。source=None 时按 em → tx → sn → ts 的顺序回退（谁先给非空表谁赢）。

    返回列：code/market/name/price/chg_pct/vol/amount/turnover/pe/pb/high/low/open/pre_close/mktcap/float_cap
    """
    lst = [codes_in] if isinstance(codes_in, str) else list(codes_in)
    order = [source] if source else ["em", "tx", "sn", "ts"]
    errs = []
    for src in order:
        try:
            if src == "em":
                df = em.quote_snapshot(lst)
            elif src == "tx":
                df = tx.snapshot(lst)
            elif src == "sn":
                df = sn.snapshot(lst)
            elif src == "ts":
                rows = ts.snapshot([codes.to_thscode(c) for c in lst])
                df = pd.DataFrame(rows).rename(columns={"thscode": "code"}) if rows else pd.DataFrame()
            else:
                raise ValueError(f"unknown source {src!r}")
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
            errs.append(f"{src}: empty")
        except Exception as exc:  # 回退链：记下来，继续下一个源
            errs.append(f"{src}: {exc}")
    raise RuntimeError("quote 全部失败: " + " | ".join(errs))


# ---------------------------------------------------------------- 基金
def fund_lsjz(code: str, page_size: int = 250) -> pd.DataFrame:
    """场外基金历史净值（东财 F10，快通道）；失败回退接口层。"""
    try:
        df = em.fund_lsjz(code, page_size=page_size)
        if not df.empty:
            return df
    except Exception:
        pass
    return data.fund_nav(code)


def fund_nav(code: str, indicator: str = "单位净值走势") -> pd.DataFrame:
    return data.fund_nav(code, indicator=indicator)


def money_fund_rates() -> pd.DataFrame:
    return data.money_fund_rates()


# ---------------------------------------------------------------- 利率 / 日历
def repo_rate(symbol: str = "204001") -> pd.DataFrame:
    return data.repo_rate(symbol)


def trade_calendar() -> pd.DataFrame:
    return data.trade_calendar()


# ---------------------------------------------------------------- 财务 / 估值（同花顺）
def financials(code: str, statement: str = "income", n: int = 5) -> list[dict]:
    """同花顺年报/季报（statement: income/balance/cashflow；需 key）。"""
    return ts.annual_rows(code, n=n, statement=statement)


def valuation(code: str) -> dict:
    return ts.valuations([codes.to_thscode(code)])


# ---------------------------------------------------------------- 自检
def doctor() -> dict:
    """不联网的健康检查：源可用性 + 缓存统计 + 目录状态。"""
    info = {
        "version": __version__,
        "root": str(config.ROOT),
        "dirs": config.paths(),
        "data_layer": None,
        "fuyao": {"available": ts.available(), "key_source": ts.key_source()},
        "cache": cache.stats(),
        "today": dt.date.today().isoformat(),
    }
    try:
        info["data_layer"] = getattr(data.layer(), "__version__", "unknown")
    except Exception as exc:
        info["data_layer"] = f"unavailable: {exc}"
    return info
