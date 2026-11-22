# -*- coding: utf-8 -*-
"""新浪行情直连（money.finance.sina.com.cn / hq.sinajs.cn）：日线**备用通道**。

特点
----
- `getKLineData` 只给最近 N 根（`datalen`），不复权；适合补最近一段和交叉校验；
- 实时 `hq.sinajs.cn` 一次 100 只，字段少但够用（价格/涨跌/量额）。
"""
from __future__ import annotations

import pandas as pd

from .. import cache, codes, http

KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
QUOTE = "https://hq.sinajs.cn/list="


def kline(code: str, datalen: int = 640, scale: int = 240, kind: str | None = None,
          start: str = "", end: str = "", adjust: str = "", **_kw) -> pd.DataFrame:
    """最近 `datalen` 根日线（scale=240 分钟=日线；**不复权**）。

    `start/end/adjust` 只为与腾讯接口签名兼容（新浪这条接口不支持区间与复权），
    需要区间时由调用方自行切片。
    """
    sym = codes.to_sina(code, kind)
    params = {"symbol": sym, "scale": scale, "ma": "no", "datalen": datalen}

    def load():
        js = http.get_json(KLINE, params=params, headers={"Referer": "https://finance.sina.com.cn/"})
        return pd.DataFrame(js or [])

    raw = cache.frame("sina_kline", params, load, source="sina.kline")
    if raw.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": raw.get("day"), "open": pd.to_numeric(raw.get("open"), errors="coerce"),
        "high": pd.to_numeric(raw.get("high"), errors="coerce"),
        "low": pd.to_numeric(raw.get("low"), errors="coerce"),
        "close": pd.to_numeric(raw.get("close"), errors="coerce"),
        "volume": pd.to_numeric(raw.get("volume"), errors="coerce") / 100.0,  # 股 → 手
    })
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    return out.reset_index(drop=True)


def snapshot(codes_in) -> pd.DataFrame:
    """实时快照（hq.sinajs.cn，≤100 只/次）。只取核心字段。"""
    lst = [codes_in] if isinstance(codes_in, str) else list(codes_in)
    syms = [codes.to_sina(c) for c in lst]
    rows = []
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]

        def load(chunk=chunk):
            txt = http.get_text(QUOTE + ",".join(chunk),
                                headers={"Referer": "https://finance.sina.com.cn/"})
            return pd.DataFrame([{"line": x} for x in txt.split(";") if "=" in x])

        df = cache.frame("sina_quote", {"syms": ",".join(chunk)}, load, source="sina.quote")
        for line in df.get("line", []):
            try:
                head, _, body = str(line).partition("=")
                f = body.strip().strip('"').split(",")
                if len(f) < 32:
                    continue
                rows.append({
                    "code": head.split("_")[-1][2:], "name": f[0],
                    "price": float(f[3] or "nan"), "pre_close": float(f[2] or "nan"),
                    "open": float(f[1] or "nan"), "high": float(f[4] or "nan"),
                    "low": float(f[5] or "nan"), "volume": float(f[8] or "nan") / 100.0,
                    "amount": float(f[9] or "nan"),
                })
            except Exception:
                continue
    out = pd.DataFrame(rows)
    if not out.empty and not pd.isna(out["pre_close"]).all():
        out["chg_amt"] = out["price"] - out["pre_close"]
        out["chg_pct"] = (out["chg_amt"] / out["pre_close"] * 100).round(4)
    return out
