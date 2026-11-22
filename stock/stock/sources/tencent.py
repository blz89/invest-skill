# -*- coding: utf-8 -*-
"""腾讯行情直连（web.ifzq.gtimg.cn / qt.gtimg.cn）：**日线主通道**。

为什么是它
----------
- 东财 push2his 在本机被网络重置（RemoteDisconnected），不可用；
- 腾讯 K 线接口**稳定、无需 key、支持前/后复权**，一次可取 640 根（可翻页）；
- 实时快照 qt.gtimg.cn 一次 60 只，字段简单。

返回列
------
日线：date/open/close/high/low/volume(手)/amount(元，可缺)
快照：code/name/price/pre_close/open/high/low/volume/amount/chg_pct/chg_amt/turnover_pct/pe/pb/mktcap/float_cap
"""
from __future__ import annotations

import pandas as pd

from .. import cache, codes, http

KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE = "https://qt.gtimg.cn/q="


def kline(code: str, start: str = "", end: str = "", adjust: str = "qfq",
          kind: str | None = None, period: str = "day") -> pd.DataFrame:
    """日/周/月 K 线。adjust: qfq/hfq/""（不复权）"""
    sym = codes.to_sina(code, kind)
    adj = {"qfq": "qfq", "hfq": "hfq", "": "", None: ""}.get(adjust, "qfq")
    # 腾讯要求 begin/end 是 `YYYY-MM-DD`；未来日期会直接返回空，所以缺省传空串
    begin = (start or "").strip()
    finish = (end or "").strip()
    params = {"param": f"{sym},{period},{begin},{finish},640,{adj}"}

    def load():
        js = http.get_json(KLINE, params=params)
        node = ((js or {}).get("data") or {}).get(sym) or {}
        rows = node.get(f"{adj}{period}") or node.get(period) or []
        return pd.DataFrame(rows)

    raw = cache.frame("tx_kline", params, load, source="tencent.kline")
    if raw.empty:
        return pd.DataFrame()
    ncol = raw.shape[1]
    names = ["date", "open", "close", "high", "low", "volume"][:ncol]
    out = raw.copy()
    out.columns = names + [f"x{i}" for i in range(ncol - len(names))]
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ("open", "close", "high", "low", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.drop(columns=[c for c in out.columns if c.startswith("x")])
    out = out.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    return out.reset_index(drop=True)


def snapshot(codes_in) -> pd.DataFrame:
    """实时快照（qt.gtimg.cn，≤60 只/次）。"""
    lst = [codes_in] if isinstance(codes_in, str) else list(codes_in)
    syms = [codes.to_sina(c) for c in lst]
    frames = []
    for i in range(0, len(syms), 60):
        chunk = syms[i:i + 60]

        def load(chunk=chunk):
            raw = http.get_text(QUOTE + ",".join(chunk))
            return pd.DataFrame([{"line": x} for x in raw.split(";") if "=" in x])

        df = cache.frame("tx_quote", {"syms": ",".join(chunk)}, load, source="tencent.quote")
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    rows = []
    for line in pd.concat(frames, ignore_index=True)["line"]:
        try:
            head, _, body = str(line).partition("=")
            fields = body.strip().strip('"').split("~")
            if len(fields) < 40:
                continue
            rows.append({
                "code": fields[2], "name": fields[1],
                "price": float(fields[3] or "nan"), "pre_close": float(fields[4] or "nan"),
                "open": float(fields[5] or "nan"), "volume": float(fields[6] or "nan"),
                "amount": float(fields[37] or "nan") * 10000.0,   # 万元 → 元
                "chg_amt": float(fields[31] or "nan"), "chg_pct": float(fields[32] or "nan"),
                "high": float(fields[33] or "nan"), "low": float(fields[34] or "nan"),
                "turnover_pct": float(fields[38] or "nan"),
                "pe": float(fields[39] or "nan"),
                "pb": float(fields[46] or "nan") if len(fields) > 46 else float("nan"),
                "mktcap": float(fields[45] or "nan") * 100000000.0 if len(fields) > 45 else float("nan"),
                "float_cap": float(fields[44] or "nan") * 100000000.0 if len(fields) > 44 else float("nan"),
            })
        except Exception:
            continue
    return pd.DataFrame(rows)
