#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宏观层（LPR / SHIBOR / CPI / PPI / PMI / M1-M2 / 社融）—— 每月刷新，作为背景板。

为什么要这一层
--------------
技能的仓位上限只看「沪深300 vs MA200」，没有任何宏观变量。这不一定是错的
（宏观择时在 A 股很难验证），但**完全不看**会漏掉两类信息：

1. **现金层的收益环境**：逆回购 / 货基收益跟着政策利率走。LPR 3.0%、SHIBOR 3M 1.40%
   的环境下，现金层 1.0% 是「正常的低」，不是「策略失效」。
2. **估值的分母**：LPR 与 CPI 决定无风险利率与通胀，进而决定「多少倍 PE 算贵」。
   现在估值分位是横截面对比，没有宏观锚。

**这一层不参与打分、不改变仓位。** 只写进报告当背景，并在 Lead 复盘时提供一句环境描述。
没有 A 股上的宏观择时回测证据，所以按本技能的第一原则——不可验证的东西不进决策链。

数据来自 stock 取数接口层（免费、无需 key）。任一接口挂了就跳过该项（fail-soft）。

输出
----
- `out/macro.json`：机器可读
- `out/macro_brief.md`：五行以内的人话

用法
----
    python scripts/macro.py            # 刷新并打印
    python scripts/macro.py --json     # 只打印 JSON
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

JSON_PATH = os.path.join(config.OUT_DIR, "macro.json")
BRIEF_PATH = os.path.join(config.OUT_DIR, "macro_brief.md")


# ---------------------------------------------------------------- 纯函数（可测）
def month_key(text) -> str:
    """把 '2026年09月份' / '202609' / '2026-09' 统一成 '2026-09'；认不出返回 ''。"""
    s = str(text or "")
    m = re.search(r"(\d{4})\D{0,2}(\d{1,2})", s)
    if not m:
        return ""
    y, mo = m.group(1), int(m.group(2))
    if not (1 <= mo <= 12):
        return ""
    return f"{y}-{mo:02d}"


def latest_by(df, key_col: str, parse=month_key):
    """按 key_col 取最新一行（各数据源的宏观表顺序不统一，必须自己排）。

    接受 DataFrame 或 list[dict]（方便单测不引入 pandas）。
    """
    if df is None:
        return {}
    if hasattr(df, "columns"):
        if len(df) == 0 or key_col not in df.columns:
            return {}
        recs = df.to_dict("records")
    else:
        recs = [r for r in df if isinstance(r, dict)]
    if not recs or key_col not in recs[0]:
        return {}
    rows = []
    for rec in recs:
        k = parse(rec.get(key_col))
        if k:
            rows.append((k, rec))
    if not rows:
        return recs[-1]
    rows.sort(key=lambda x: x[0])
    return rows[-1][1]


def num(x):
    try:
        v = float(x)
        return None if v != v else v          # NaN -> None
    except (TypeError, ValueError):
        return None


def real_rate(lpr1y, cpi_yoy) -> float | None:
    """实际政策利率 ≈ LPR1Y − CPI 同比（两个都是百分数）。"""
    a, b = num(lpr1y), num(cpi_yoy)
    return None if a is None or b is None else round(a - b, 2)


def m1_m2_gap(m1_yoy, m2_yoy) -> float | None:
    a, b = num(m1_yoy), num(m2_yoy)
    return None if a is None or b is None else round(a - b, 2)


def pmi_state(pmi) -> str:
    v = num(pmi)
    if v is None:
        return ""
    return "扩张" if v > 50 else ("持平" if v == 50 else "收缩")


def shibor_trend(rows: list[dict], days: int = 20) -> dict:
    """SHIBOR 3M 近 N 个交易日均值 vs 最新，给出方向。"""
    vals = [num(r.get("3M-定价")) for r in (rows or [])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {}
    last = vals[-1]
    hist = vals[-days:] if len(vals) > 1 else vals
    mean = sum(hist) / len(hist)
    return {"last": round(last, 4), "mean20": round(mean, 4),
            "delta_bp": round((last - mean) * 100, 2)}


def _month_gap(month: str, ref: str) -> int | None:
    """month/ref 都是 YYYY-MM；返回 ref 比 month 落后几个月（负数=领先）。解析失败返回 None。"""
    try:
        a = [int(x) for x in str(month).split("-")[:2]]
        b = [int(x) for x in str(ref).split("-")[:2]]
    except (ValueError, TypeError):
        return None
    if len(a) < 2 or len(b) < 2:
        return None
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def brief_md(doc: dict) -> str:
    g = doc.get("data") or {}
    lines = [f"# 宏观环境（{doc.get('generated_at', '')}）", ""]
    lpr = g.get("lpr") or {}
    if lpr:
        lines.append(f"- LPR：1Y {lpr.get('lpr1y')}%｜5Y {lpr.get('lpr5y')}%"
                     f"（{lpr.get('month')}）")
    sh = g.get("shibor") or {}
    if sh:
        lines.append(f"- SHIBOR 3M：{sh.get('last')}%（20 日均值 {sh.get('mean20')}%，"
                     f"偏离 {sh.get('delta_bp'):+.1f}bp）")
    cpi = g.get("cpi") or {}
    if cpi:
        lines.append(f"- CPI 同比：{cpi.get('yoy')}%（{cpi.get('month')}）｜"
                     f"实际政策利率 ≈ {doc.get('real_rate')}%")
    ppi = g.get("ppi") or {}
    if ppi:
        lines.append(f"- PPI 同比：{ppi.get('yoy')}%（{ppi.get('month')}）")
    pmi = g.get("pmi") or {}
    if pmi:
        lines.append(f"- PMI：制造业 {pmi.get('manufacturing')}（{pmi_state(pmi.get('manufacturing'))}）"
                     f"｜非制造业 {pmi.get('non_manufacturing')}")
    ms = g.get("money_supply") or {}
    if ms:
        lines.append(f"- M1 同比 {ms.get('m1_yoy')}%｜M2 同比 {ms.get('m2_yoy')}%"
                     f"｜剪刀差 {doc.get('m1_m2_gap')}pp（{ms.get('month')}）")
    sf = g.get("social_financing") or {}
    if sf:
        mon = str(sf.get("month") or "")
        gap = _month_gap(mon, str(doc.get("generated_at") or "")[:7])
        stale = f"，源已停更 {gap} 个月，仅参考" if (gap is not None and gap > 3) else ""
        lines.append(f"- 社融增量：{sf.get('increment_yi')} 亿（{mon}{stale}）")
    lines += ["", "> 这一层只做背景板：不参与打分、不改变仓位。", ""]
    return "\n".join(lines)


def _safe(fn, name: str):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 宏观取数失败（{name}）：{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- 取数
def collect() -> dict:
    import warnings
    warnings.filterwarnings("ignore")
    try:
        from stock import endpoints as ak
    except Exception as e:  # noqa: BLE001
        print(f"[error] stock 的取数接口层不可用：{e}")
        return {}

    data: dict = {}

    df = _safe(ak.macro_china_lpr, "lpr")
    if df is not None:
        r = latest_by(df, "TRADE_DATE", lambda x: str(x)[:10])
        data["lpr"] = {"lpr1y": num(r.get("LPR1Y")), "lpr5y": num(r.get("LPR5Y")),
                       "month": str(r.get("TRADE_DATE"))[:10]}

    df = _safe(ak.macro_china_shibor_all, "shibor")
    if df is not None:
        recs = df.to_dict("records")
        recs.sort(key=lambda r: str(r.get("日期") or ""))
        tr = shibor_trend(recs)
        if tr:
            tr["date"] = str(recs[-1].get("日期"))
            data["shibor"] = tr

    df = _safe(ak.macro_china_cpi, "cpi")
    if df is not None:
        r = latest_by(df, "月份")
        data["cpi"] = {"yoy": num(r.get("全国-同比增长")),
                       "mom": num(r.get("全国-环比增长")),
                       "month": month_key(r.get("月份"))}

    df = _safe(ak.macro_china_ppi, "ppi")
    if df is not None:
        r = latest_by(df, "月份")
        data["ppi"] = {"yoy": num(r.get("当月同比增长")),
                       "month": month_key(r.get("月份"))}

    df = _safe(ak.macro_china_pmi, "pmi")
    if df is not None:
        r = latest_by(df, "月份")
        data["pmi"] = {"manufacturing": num(r.get("制造业-指数")),
                       "non_manufacturing": num(r.get("非制造业-指数")),
                       "month": month_key(r.get("月份"))}

    df = _safe(ak.macro_china_money_supply, "money_supply")
    if df is not None:
        r = latest_by(df, "月份")
        data["money_supply"] = {"m1_yoy": num(r.get("货币(M1)-同比增长")),
                                "m2_yoy": num(r.get("货币和准货币(M2)-同比增长")),
                                "month": month_key(r.get("月份"))}

    df = _safe(ak.macro_china_shrzgm, "shrzgm")
    if df is not None:
        r = latest_by(df, "月份", lambda x: month_key(str(x)[:6]) if len(str(x)) >= 6 else "")
        data["social_financing"] = {"increment_yi": num(r.get("社会融资规模增量")),
                                    "month": month_key(r.get("月份"))}
    return data


def run(write: bool = True) -> dict:
    data = collect()
    doc = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "native:macro_china_lpr/shibor/cpi/ppi/pmi/money_supply/shrzgm",
        "data": data,
        "real_rate": real_rate((data.get("lpr") or {}).get("lpr1y"),
                               (data.get("cpi") or {}).get("yoy")),
        "m1_m2_gap": m1_m2_gap((data.get("money_supply") or {}).get("m1_yoy"),
                               (data.get("money_supply") or {}).get("m2_yoy")),
        "note": "背景板，不参与打分、不改变仓位",
    }
    if write and data:
        os.makedirs(config.OUT_DIR, exist_ok=True)
        with io.open(JSON_PATH, "w", encoding="utf-8", newline="") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        with io.open(BRIEF_PATH, "w", encoding="utf-8", newline="") as f:
            f.write(brief_md(doc))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="宏观层（背景板，不参与打分）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    doc = run(write=not a.no_write)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0 if doc.get("data") else 1
    if not doc.get("data"):
        print("[error] 一项宏观数据都没取到")
        return 1
    print(brief_md(doc))
    if not a.no_write:
        print(f"[ok] 已写 {JSON_PATH} 与 {BRIEF_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
