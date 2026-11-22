# -*- coding: utf-8 -*-
"""东方财富直连：**基金 F10 历史净值** 与 **行情快照** 两条快通道。

为什么单独写
------------
- 基金历史：F10 的 lsjz 是单次 JSON（0.2 秒/只）；翻页型接口要 160 页 / 25–35 秒。
- 行情快照：push2 支持按 secid 批量（≤100 只/次），全市场快照一次拿完。

坑（都已处理）
--------------
1. F10 必须带 `Referer: https://fundf10.eastmoney.com/`，否则 404。
2. 万份收益/7 日年化里有 `'---'` 空值，必须 `to_numeric(errors="coerce")`。
3. push2 的字段是编号（f2=最新价、f3=涨跌幅、f5=成交量、f6=成交额、f12=代码、
   f13=市场、f14=名称、f15=最高、f16=最低、f17=今开、f18=昨收、f8=换手率、
   f9=市盈率、f23=市净率、f20=总市值、f21=流通市值）。
"""
from __future__ import annotations

import pandas as pd

from .. import cache, codes, config, http

# ---------------------------------------------------------------- 日线（push2his）
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def kline(code: str, start: str = "", end: str = "", adjust: str = "qfq",
          kind: str | None = None, period: str = "daily") -> pd.DataFrame:
    """日线 K 线（push2his）：`FQT` 1=前复权 2=后复权 0=不复权。

    返回 date/open/close/high/low/volume/amount/amplitude_pct/chg_pct/chg_amt/turnover_pct
    （volume 单位「手」，amount 单位「元」）
    """
    secid = codes.to_em_secid(code, kind)
    fqt = {"qfq": 1, "hfq": 2, "": 0, None: 0}.get(adjust, 1)
    klt = {"daily": 101, "weekly": 102, "monthly": 103}.get(period, 101)
    beg = (start or "19900101").replace("-", "")
    endd = (end or "20500101").replace("-", "")
    params = {"secid": secid, "klt": klt, "fqt": fqt, "beg": beg, "end": endd,
              "fields1": KLINE_FIELDS1, "fields2": KLINE_FIELDS2, "lmt": 100000}

    def load():
        js = http.get_json(KLINE, params=params)
        d = (js.get("data") or {}) if isinstance(js, dict) else {}
        return pd.DataFrame({"k": d.get("klines") or []})

    raw = cache.frame("em_kline", params, load, source="eastmoney.push2his")
    if raw.empty:
        return pd.DataFrame()
    parts = raw["k"].astype(str).str.split(",", expand=True)
    parts.columns = ["date", "open", "close", "high", "low", "volume", "amount",
                     "amplitude_pct", "chg_pct", "chg_amt", "turnover_pct"]
    out = parts.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in out.columns:
        if c != "date":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- 基金
FUND_LSJZ = "https://api.fund.eastmoney.com/f10/lsjz"


def fund_lsjz(code: str, page_size: int = 250, page_index: int = 1) -> pd.DataFrame:
    """单只基金历史净值：date / nav(单位净值) / acc_nav(累计净值) / wanfen(万份收益) /
    y7_pct(7日年化%) / buy(申购状态) / sell(赎回状态)。**升序、去重**。

    货基的「万份收益」在 `DWJZ` 字段、7 日年化在 `LJJZ`；非货基 DWJZ 是单位净值、
    LJJZ 是累计净值——按 `是否货基` 统一成两列都有了，调用方按需取。
    """
    sym = codes.bare(code)
    params = {"fundCode": sym, "pageIndex": page_index, "pageSize": page_size}

    def load():
        js = http.get_json(FUND_LSJZ, params=params,
                           headers={"Referer": config.EM_REFERER})
        data = (js or {}).get("Data") or {}
        rows = data.get("LSJZList") or []
        return pd.DataFrame(rows)

    raw = cache.frame("em_fund_lsjz", params, load, source="eastmoney.lsjz")
    if raw.empty:
        return pd.DataFrame(columns=["date", "nav", "acc_nav", "wanfen", "y7_pct", "buy", "sell"])
    df = pd.DataFrame({
        "date": raw.get("FSRQ"),
        "nav": pd.to_numeric(raw.get("DWJZ"), errors="coerce"),
        "acc_nav": pd.to_numeric(raw.get("LJJZ"), errors="coerce"),
        "wanfen": pd.to_numeric(raw.get("DWJZ"), errors="coerce"),
        "y7_pct": pd.to_numeric(raw.get("LJJZ"), errors="coerce"),
        "buy": raw.get("SGZT"),
        "sell": raw.get("SHZT"),
    })
    df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- 行情
QUOTE_FIELDS = "f2,f3,f5,f6,f8,f9,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23"
PUSH2 = "https://push2.eastmoney.com/api/qt/ulist.np/get"


def quote_snapshot(codes_in) -> pd.DataFrame:
    """批量行情快照（≤100 只/次，超过自动分批）。

    返回列：code/market/name/price/chg_pct/vol/amount/turnover/pe/pb/high/low/open/pre_close/mktcap/float_cap
    """
    secids = [codes.to_em_secid(c) for c in ([codes_in] if isinstance(codes_in, str) else codes_in)]
    frames = []
    for i in range(0, len(secids), 100):
        chunk = secids[i:i + 100]
        params = {"fltt": 2, "invt": 2, "fields": QUOTE_FIELDS, "secids": ",".join(chunk)}

        def load(chunk=chunk, params=params):
            js = http.get_json(PUSH2, params=params)
            return pd.DataFrame((js.get("data") or {}).get("diff") or [])

        raw = cache.frame("em_quote", {"secids": ",".join(chunk)}, load, source="eastmoney.push2")
        if not raw.empty:
            frames.append(raw)
    if not frames:
        return pd.DataFrame()
    raw = pd.concat(frames, ignore_index=True)
    df = pd.DataFrame({
        "code": raw.get("f12"), "market": raw.get("f13"), "name": raw.get("f14"),
        "price": pd.to_numeric(raw.get("f2"), errors="coerce"),
        "chg_pct": pd.to_numeric(raw.get("f3"), errors="coerce"),
        "vol": pd.to_numeric(raw.get("f5"), errors="coerce"),
        "amount": pd.to_numeric(raw.get("f6"), errors="coerce"),
        "turnover": pd.to_numeric(raw.get("f8"), errors="coerce"),
        "pe": pd.to_numeric(raw.get("f9"), errors="coerce"),
        "pb": pd.to_numeric(raw.get("f23"), errors="coerce"),
        "high": pd.to_numeric(raw.get("f15"), errors="coerce"),
        "low": pd.to_numeric(raw.get("f16"), errors="coerce"),
        "open": pd.to_numeric(raw.get("f17"), errors="coerce"),
        "pre_close": pd.to_numeric(raw.get("f18"), errors="coerce"),
        "mktcap": pd.to_numeric(raw.get("f20"), errors="coerce"),
        "float_cap": pd.to_numeric(raw.get("f21"), errors="coerce"),
    })
    df = df.dropna(subset=["code"]).reset_index(drop=True)
    df["code"] = df["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    return df
