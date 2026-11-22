# -*- coding: utf-8 -*-
"""阶段一补充：ETF（场内基金）筛选。

为什么单列一层：本金较小时，单票 10% 上限只有几千元，
而「1 手 = 100 股」，价格 >10 元的个股一手就超限、根本买不了。
场内 ETF 一手 120–430 元，能把权益预算精确卡进 10% 上限。

漏斗（与个股同一套「先便宜后昂贵」的思路）：
  L0 新浪 ETF 全表（1,700+）→ 按名称剔除 债券/货币/商品/跨境（正则 config.ETF["exclude_patterns"]），
     并要求名称能认出是 A 股权益类（宽基/策略/行业关键词），认不出的也剔除（fail-closed）
  L1 当日成交额 ≥ config.ETF["prefilter_spot_amt_yi"] 亿（省掉上千次历史请求）
  L2 逐只取 3 年日线：日均成交额 / 历史长度 / 近 3 年收益 / 近 3 年回撤（config.S1_FUND）
  L3 份额折算体检：单日涨跌幅 > config.ETF["split_suspect_move"] 视为折算/数据污染
     → 标 split_suspect，**不参与排序**（这类序列的前复权收益是假的）
  L4 同一跟踪指数只留一只（按综合分），避免「买 5 只沪深300」

产出：out/stage1_etf.csv、out/stage1_etf_funnel.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # noqa: E402
import datasource as ds  # noqa: E402
import metrics as m    # noqa: E402
from etf_codes import etf_sym  # noqa: E402

OUT = config.OUT_DIR
CFG_F = config.S1_FUND
CFG_E = config.ETF

# 连续取数失败上限：数据层若整体异常（代码前缀拼错/接口被限流），单只重试 4 次≈15s，
# 200+ 只会吃满 run.py 单步 30 分钟超时、把 stage2/3 一起拖死；连错到这里 fail loud 中止。
_CONSEC_FAIL_LIMIT = 8


def _etf_sym(code: str) -> str:
    """场内基金代码 → 新浪 symbol；实现统一在 etf_codes.etf_sym（见其文档）。"""
    return etf_sym(code)


# ---------------------------------------------------------------- 分类
_EXCLUDE_RE = [re.compile(p) for p in CFG_E["exclude_patterns"]]
_EXCLUDE_CODES = set(CFG_E.get("exclude_codes") or [])


def exclude_reason(name: str) -> str | None:
    for pat in _EXCLUDE_RE:
        mt = pat.search(name)
        if mt:
            return mt.group(0)
    return None


def classify(name: str) -> str | None:
    """宽基 / 策略 / 行业；认不出是 A 股权益类的返回 None（fail-closed：认不出就不买）。"""
    if any(k in name for k in CFG_E["broad_keywords"]):
        return "宽基"
    if any(k in name for k in CFG_E["strategy_keywords"]):
        return "策略"
    if any(k in name for k in CFG_E["industry_keywords"]):
        return "行业"
    return None


# 同一跟踪指数只留一只：按顺序匹配，先匹配到的优先（长关键词在前，避免「创业板50」被「创业板」吃掉）
_INDEX_KEYS = [
    ("红利低波", "红利低波"), ("红利", "红利"),
    ("科创50", "科创50"), ("科创100", "科创100"), ("科创板", "科创板"),
    ("创业板50", "创业板50"), ("创业板", "创业板"),
    ("中证A500", "中证A500"), ("中证A50", "中证A50"), ("中证A100", "中证A100"),
    ("沪深300", "沪深300"), ("上证380", "上证380"), ("上证180", "上证180"),
    ("上证50", "上证50"), ("科创综指", "科创综指"),
    ("中证1000", "中证1000"), ("中证2000", "中证2000"), ("中证500", "中证500"),
    ("中证800", "中证800"), ("中证100", "中证100"),
    ("深证100", "深证100"), ("深证成指", "深证成指"), ("全指", "全指"),
    ("MSCI", "MSCI"), ("国证2000", "国证2000"), ("国证1000", "国证1000"),
]


def index_key(name: str) -> str:
    """跟踪标的键：用于「同一指数只留一只」与行业额度归属。"""
    for kw, key in _INDEX_KEYS:
        if kw in name:
            return key
    # 行业类：用行业关键词做键，避免「银行ETF华夏」和「银行ETF易方达」被当成两个行业
    if classify(name) == "行业":
        for kw in CFG_E["industry_keywords"]:
            if kw in name:
                return f"行业-{kw}"
    # 策略类：红利 / 红利低波 / 价值 等本来就是不同标的，各自成键
    for kw in CFG_E["strategy_keywords"]:
        if kw in name:
            return f"策略-{kw}"
    return name


# ---------------------------------------------------------------- 指标
def _slice_days(close: pd.Series, days: int) -> pd.Series:
    return close.tail(min(len(close), days))


def etf_metrics(sym: str, name: str) -> dict:
    df = ds.fetch_etf_hist(sym)
    if df is None or df.empty or len(df) < 60:
        return {"ok": False, "reason": "历史数据不足 60 天"}
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    amount = pd.to_numeric(df.get("amount"), errors="coerce")
    if close.empty:
        return {"ok": False, "reason": "收盘价全为空"}
    rets = close.pct_change().dropna()
    max_1d = float(rets.abs().max()) if len(rets) else 0.0

    days_hist = int(len(close))
    amt20 = float(amount.tail(20).mean()) / 1e8 if amount is not None and len(amount) else None
    price = float(close.iloc[-1])
    ma200 = m.ma(close, 200)
    ma60 = m.ma(close, 60)
    c3 = _slice_days(close, 750)
    c1 = _slice_days(close, 244)

    def ret(series):
        return None if len(series) < 2 else float(series.iloc[-1] / series.iloc[0] - 1)

    return {
        "ok": True,
        "sym": sym, "name": name,
        "price": round(price, 3),
        "lot_cost": round(price * config.CAPITAL["lot_size"], 2),
        "price_date": str(df["date"].iloc[-1].date()),
        "hist_days": days_hist,
        "amt20_yi": None if amt20 is None else round(amt20, 4),
        "vs_ma200": None if ma200 is None else round(price / ma200 - 1, 4),
        "vs_ma60": None if ma60 is None else round(price / ma60 - 1, 4),
        "ret_1y": ret(c1), "ret_3y": ret(c3),
        "dd_1y": m.max_drawdown(c1), "dd_3y": m.max_drawdown(c3),
        "max_1d_move": round(max_1d, 4),
        "split_suspect": bool(max_1d > CFG_E["split_suspect_move"]),
    }


def score_etf(r: dict) -> float:
    """综合分 0–100：近 3 年收益 40 + 回撤 35 + 流动性 25（都是规则化的线性映射）。"""
    s = 0.0
    s += m.score_linear(r.get("ret_3y"), 0.0, 0.60, 40)
    s += m.score_inverse(r.get("dd_3y"), -0.05, -0.45, 35) if r.get("dd_3y") is not None else 0.0
    s += m.score_linear(r.get("amt20_yi"), 0.5, 5.0, 25)
    return round(s, 2)


def run_screen(limit: int | None = None, verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    spot = ds.fetch_etf_spot()
    funnel = {"spot_total": int(len(spot))}

    rows = []
    n_excluded = n_unclassified = 0
    for _, s in spot.iterrows():
        nm = str(s.get("名称") or "")
        code = str(s.get("代码") or "").strip()
        if not nm or not code:
            continue
        sym0 = _etf_sym(code)
        if sym0 in _EXCLUDE_CODES:   # 商品期货 ETF：名称像行业 ETF，但跟踪的是期货，不在 A 股股票范围
            n_excluded += 1
            continue
        if exclude_reason(nm):
            n_excluded += 1
            continue
        cls = classify(nm)
        if cls is None:              # fail-closed：认不出是 A 股权益类的不买
            n_unclassified += 1
            continue
        amt = s.get("成交额")
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        if amt / 1e8 < CFG_E["prefilter_spot_amt_yi"]:
            continue
        rows.append({"sym": sym0, "name": nm, "cls": cls,
                     "underlying": index_key(nm), "spot_amt_yi": round(amt / 1e8, 3)})

    funnel["excluded_out_of_scope"] = n_excluded
    funnel["excluded_unclassified"] = n_unclassified
    funnel["after_exclude_and_liquidity"] = len(rows)
    if limit:
        rows = rows[:limit]

    ok, failed = [], []
    consec_fail = 0
    for i, r in enumerate(rows, 1):
        try:
            d = etf_metrics(r["sym"], r["name"])
        except Exception as e:  # 单只失败不中断全表，但要记录下来（fail loud 到漏斗里）
            failed.append({"sym": r["sym"], "name": r["name"], "error": f"{type(e).__name__}: {e}"})
            consec_fail += 1
            if consec_fail >= _CONSEC_FAIL_LIMIT:
                raise RuntimeError(
                    f"ETF 日线连续 {consec_fail} 只取数失败，中止以免吃满 run.py 单步超时"
                    f"（最近失败：{r['sym']} {type(e).__name__}: {e}）")
            continue
        if not d.get("ok"):
            failed.append({"sym": r["sym"], "name": r["name"], "error": d.get("reason")})
            continue
        consec_fail = 0
        d.update({"cls": r["cls"], "underlying": r["underlying"],
                  "spot_amt_yi": r["spot_amt_yi"]})
        ok.append(d)
        if verbose and i % 25 == 0:
            print(f"  [{i}/{len(rows)}] 已取 {len(ok)} 只", flush=True)

    funnel["history_fetched"] = len(ok)
    funnel["history_failed"] = len(failed)

    # L2 四道门槛（config.S1_FUND，与个股同一份配置）
    passed, why = [], {}
    for d in ok:
        fails = []
        if d["hist_days"] < CFG_F["min_history_days"]:
            fails.append(f"历史不足 {CFG_F['min_history_days']} 天")
        if d["amt20_yi"] is not None and d["amt20_yi"] < CFG_F["min_avg_amt20_yi"]:
            fails.append(f"20 日均成交 {d['amt20_yi']:.2f} 亿 < {CFG_F['min_avg_amt20_yi']}")
        if d["ret_3y"] is not None and d["ret_3y"] <= CFG_F["min_return_3y"]:
            fails.append(f"近 3 年收益 {d['ret_3y'] * 100:.1f}% ≤ 0")
        if d["dd_3y"] is not None and d["dd_3y"] < CFG_F["max_drawdown_3y"]:
            fails.append(f"近 3 年回撤 {d['dd_3y'] * 100:.1f}% 深于 {CFG_F['max_drawdown_3y'] * 100:.0f}%")
        for f in fails:
            why[f] = why.get(f, 0) + 1
        if not fails:
            passed.append(d)
    funnel["after_fund_thresholds"] = len(passed)
    funnel["fail_reasons"] = why

    for d in passed:
        d["score"] = score_etf(d)
    passed.sort(key=lambda x: -x["score"])

    # L3 折算污染：标出来、不参与排序（保留在表里供审计，但 plan.py 会过滤）
    clean = [d for d in passed if not d["split_suspect"]]
    suspect = [d for d in passed if d["split_suspect"]]
    funnel["split_suspect_excluded"] = len(suspect)
    funnel["after_split_check"] = len(clean)

    # L4 同一跟踪指数只留一只
    seen, dedup = set(), []
    for d in clean:
        k = d["underlying"]
        if k in seen:
            continue
        seen.add(k)
        dedup.append(d)
    funnel["after_dedupe_index"] = len(dedup)
    funnel["dropped_suspect"] = [{"sym": d["sym"], "name": d["name"],
                                  "max_1d_move": d["max_1d_move"]} for d in suspect]

    df = pd.DataFrame(dedup)
    return df, funnel


def main():
    ap = argparse.ArgumentParser(description="阶段一补充：ETF 筛选")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只（调试用）")
    args = ap.parse_args()

    print("[ETF] 拉取新浪 ETF 全表并筛选…", flush=True)
    t0 = dt.datetime.now()
    df, funnel = run_screen(limit=args.limit)

    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, "stage1_etf.csv")
    cols = ["sym", "name", "cls", "underlying", "price", "lot_cost", "price_date",
            "amt20_yi", "hist_days", "ret_1y", "ret_3y", "dd_1y", "dd_3y",
            "vs_ma200", "vs_ma60", "max_1d_move", "score"]
    df_out = df[[c for c in cols if c in df.columns]].copy()
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig", float_format="%.4f")

    funnel["elapsed_sec"] = round((dt.datetime.now() - t0).total_seconds(), 1)
    funnel["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(OUT, "stage1_etf_funnel.json"), "w", encoding="utf-8") as f:
        json.dump(funnel, f, ensure_ascii=False, indent=1)
    ds.dump_provenance("provenance_etf")

    print(f"[ETF] 全表 {funnel['spot_total']}"
          f" → 剔范围外 {funnel['excluded_out_of_scope']}"
          f" / 认不出 {funnel['excluded_unclassified']}"
          f" → 范围/流动性 {funnel['after_exclude_and_liquidity']}"
          f" → 取到历史 {funnel['history_fetched']} → 四道门槛 {funnel['after_fund_thresholds']}"
          f" → 剔除折算 {funnel['split_suspect_excluded']}"
          f" → 同指数去重 {funnel['after_dedupe_index']}", flush=True)
    if funnel.get("dropped_suspect"):
        print("  折算/数据污染已剔除：", ", ".join(
            f"{d['sym']}({d['max_1d_move'] * 100:.0f}%)" for d in funnel["dropped_suspect"]), flush=True)
    print(f"输出: {csv_path}  |  用时 {funnel['elapsed_sec']}s")


if __name__ == "__main__":
    main()
