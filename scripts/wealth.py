# -*- coding: utf-8 -*-
"""R2 理财产品层：场内债券 ETF 筛选（阶段一·R2 池）。

为什么用品内债券 ETF 当「理财产品」：
  - 银行 R2 理财 / 场外债基没有可靠的 point-in-time 数据，写进策略等于编数字；
  - 场内债券 ETF 可取数、可交易、可溯源、费率透明，与逆回购/货基一起构成 R2 现金与
    中低风险层（技能只做 R2/R3，见 config.RISK）。

漏斗（与个股/权益 ETF 同一套「先便宜后昂贵」的思路）：
  L0 新浪 ETF 全表 → 名称含债券关键词（RISK.wealth.patterns）且不含排除词（可转债/增强…）
  L1 当日成交额 ≥ min_spot_amt_yi 亿（省掉上千次历史请求）
  L2 逐只历史数据：历史 ≥ min_hist_days、日均成交额 ≥ min_amt20_yi 亿、
     近 3 年回撤 ≥ max_dd_3y（中低风险的硬门槛）
  L3 折算污染：单日涨跌幅 > split_suspect_move 视为份额折算，剔除（前复权收益是假的）
  L4 同一细分类去重（「科创债」「5年地方债」「国开债」…只留综合分最高的一只）

产出：out/stage1_wealth.csv、out/stage1_wealth_funnel.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # noqa: E402
import datasource as ds  # noqa: E402
import metrics as m    # noqa: E402
import risk            # noqa: E402
from etf_codes import etf_sym  # noqa: E402

OUT = config.OUT_DIR
CFG_W = config.RISK["wealth"]

_NAME_RE = [re.compile(p) for p in CFG_W["patterns"]]
_EXCL_RE = [re.compile(p) for p in (CFG_W.get("exclude_patterns") or [])]


def name_reason(name: str) -> tuple[str | None, str | None]:
    """返回 (命中原因, 排除原因)。"""
    hit = None
    for pat in _NAME_RE:
        mt = pat.search(name)
        if mt:
            hit = mt.group(0)
            break
    if hit is None:
        return None, None
    for pat in _EXCL_RE:
        mt = pat.search(name)
        if mt:
            return hit, mt.group(0)
    return hit, None


def dedupe_key(name: str) -> str:
    """同一细分类的键：取「ETF」之前的部分，去掉公司名后缀。"""
    base = re.split(r"ETF|基金", name)[0].strip()
    return base or name


def metrics_of(sym: str, name: str) -> dict:
    df = ds.fetch_etf_hist(sym)
    if df is None or df.empty or len(df) < 60:
        return {"ok": False, "reason": "历史数据不足 60 天"}
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    amount = pd.to_numeric(df.get("amount"), errors="coerce")
    if close.empty:
        return {"ok": False, "reason": "收盘价全为空"}
    rets = close.pct_change().dropna()
    max_1d = float(rets.abs().max()) if len(rets) else 0.0
    c3 = close.tail(min(len(close), 750))
    c1 = close.tail(min(len(close), 244))

    def ret(s):
        return None if len(s) < 2 else float(s.iloc[-1] / s.iloc[0] - 1)

    vol3 = None
    if len(rets) >= 60:
        vol3 = float(rets.tail(750).std() * math.sqrt(244))

    return {
        "ok": True, "sym": sym, "name": name,
        "price": round(float(close.iloc[-1]), 3),
        "price_date": str(df["date"].iloc[-1].date()),
        "hist_days": int(len(close)),
        "amt20_yi": round(float(amount.tail(20).mean()) / 1e8, 4) if amount is not None and len(amount) else None,
        "ret_1y": ret(c1), "ret_3y": ret(c3),
        "dd_1y": m.max_drawdown(c1), "dd_3y": m.max_drawdown(c3),
        "vol_3y": None if vol3 is None else round(vol3, 4),
        "max_1d_move": round(max_1d, 4),
    }


def score_of(r: dict) -> float:
    """综合分 0–100：近 1 年收益 40 + 近 1 年回撤 35 + 流动性 25。"""
    s = 0.0
    s += m.score_linear(r.get("ret_1y"), 0.0, 0.06, 40)
    s += m.score_inverse(r.get("dd_1y"), -0.005, -0.03, 35) if r.get("dd_1y") is not None else 0.0
    s += m.score_linear(r.get("amt20_yi"), 0.5, 5.0, 25)
    return round(s, 2)


def run_screen(limit: int | None = None, verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    t0 = time.time()
    spot = ds.fetch_etf_spot()
    funnel = {
        "spot_total": int(len(spot)),
        "name_not_match": 0,
        "name_excluded": 0,
        "after_name_and_spot": 0,
        "history_failed": 0,
        "after_thresholds": 0,
        "dropped_suspect": [],
        "after_split_check": 0,
        "after_dedupe": 0,
        "final": 0,
        "fail_reasons": {},
    }
    cand: list[dict] = []
    for _, s in spot.iterrows():
        # 2026-11-16 事故：新浪 ETF 全表只给裸代码，直接喂 fetch_etf_hist 会被股票规则
        # 拼成 bj… → 404（511360 就是在这一步把 run.py 整链打断的）。统一走 etf_sym()。
        sym = etf_sym(s["代码"])
        name = str(s["名称"])
        hit, excl = name_reason(name)
        if hit is None:
            funnel["name_not_match"] += 1
            continue
        if excl:
            funnel["name_excluded"] += 1
            continue
        try:
            spot_amt_yi = float(s["成交额"]) / 1e8
        except (TypeError, ValueError):
            funnel["name_excluded"] += 1
            continue
        if spot_amt_yi < float(CFG_W["min_spot_amt_yi"]):
            funnel["name_excluded"] += 1
            continue
        cand.append({"sym": sym, "name": name, "hit": hit, "spot_amt_yi": round(spot_amt_yi, 4)})
    funnel["after_name_and_spot"] = len(cand)
    if verbose:
        print(f"[wealth] 名称命中债券类且当日成交额达标：{len(cand)} 只（全表 {funnel['spot_total']}）")

    rows: list[dict] = []
    for i, c in enumerate(cand):
        if limit and len(rows) >= limit:
            break
        d = metrics_of(c["sym"], c["name"])
        if not d.get("ok"):
            funnel["history_failed"] += 1
            continue
        hist_days = d["hist_days"]
        amt20 = d["amt20_yi"]
        dd3 = d["dd_3y"]
        if hist_days < int(CFG_W["min_hist_days"]):
            funnel["fail_reasons"]["历史不足"] = funnel["fail_reasons"].get("历史不足", 0) + 1
            continue
        if amt20 is None or amt20 < float(CFG_W["min_amt20_yi"]):
            funnel["fail_reasons"]["流动性不足"] = funnel["fail_reasons"].get("流动性不足", 0) + 1
            continue
        if dd3 is not None and dd3 < float(CFG_W["max_dd_3y"]):
            funnel["fail_reasons"]["回撤过深"] = funnel["fail_reasons"].get("回撤过深", 0) + 1
            continue
        d.update({"hit": c["hit"], "spot_amt_yi": c["spot_amt_yi"], "key": dedupe_key(c["name"])})
        d["score"] = score_of(d)
        d["split_suspect"] = bool(float(d["max_1d_move"] or 0) > float(CFG_W["split_suspect_move"]))
        lv = risk.etf_level(c["name"], "", d)
        d["level"] = lv["level"]
        d["level_reason"] = lv["reason"]
        rows.append(d)
        if verbose and (i + 1) % 10 == 0:
            print(f"[wealth] 已取 {i + 1}/{len(cand)}，合格 {len(rows)}")
    funnel["after_thresholds"] = len(rows)

    good = [r for r in rows if not r["split_suspect"]]
    funnel["dropped_suspect"] = [{"sym": r["sym"], "name": r["name"],
                                  "max_1d_move": r["max_1d_move"]}
                                 for r in rows if r["split_suspect"]]
    funnel["after_split_check"] = len(good)

    best: dict = {}
    for r in sorted(good, key=lambda x: -x["score"]):
        if r["key"] not in best:
            best[r["key"]] = r
    final = sorted(best.values(), key=lambda x: -x["score"])
    funnel["after_dedupe"] = len(final)
    funnel["final"] = len(final)
    funnel["elapsed_sec"] = round(time.time() - t0, 1)
    funnel["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    funnel["cfg"] = {k: CFG_W[k] for k in ("min_spot_amt_yi", "min_hist_days", "min_amt20_yi",
                                          "max_dd_3y", "split_suspect_move", "max_n")}

    cols = ["sym", "name", "key", "price", "price_date", "hist_days", "amt20_yi",
            "ret_1y", "ret_3y", "dd_1y", "dd_3y", "vol_3y", "max_1d_move", "score",
            "level", "level_reason"]
    jdf = pd.DataFrame(final, columns=cols)
    os.makedirs(OUT, exist_ok=True)
    jdf.to_csv(os.path.join(OUT, "stage1_wealth.csv"), index=False,
               encoding="utf-8-sig", float_format="%.4f")
    with open(os.path.join(OUT, "stage1_wealth_funnel.json"), "w", encoding="utf-8") as f:
        json.dump(funnel, f, ensure_ascii=False, indent=2)
    ds.dump_provenance("provenance_wealth")
    if verbose:
        print(f"[wealth] 合格 {len(final)} 只，耗时 {funnel['elapsed_sec']}s")
        for r in final[:10]:
            print(f"  {r['sym']} {r['name']} 价 {r['price']} 1年 {r['ret_1y']} "
                  f"回撤 {r['dd_1y']} 均额 {r['amt20_yi']}亿 分 {r['score']} {r['level']}")
    return jdf, funnel


def main():
    ap = argparse.ArgumentParser(description="R2 理财产品层（场内债券 ETF）筛选")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只（调试用）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    df, funnel = run_screen(limit=args.limit, verbose=not args.quiet)
    print(json.dumps({k: funnel[k] for k in ("spot_total", "after_name_and_spot",
                                            "after_thresholds", "after_split_check",
                                            "after_dedupe", "final")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
