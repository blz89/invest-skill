# -*- coding: utf-8 -*-
"""阶段一：筛选。把 300–800 只候选压缩到 20–30 只。

漏斗顺序是刻意安排的——先便宜后贵：
  L0 股票池        1 次调用（中证指数成分）
  L1 规模/流动性   全市场快照，0 次额外调用
  L2 质量/成长     每只 1 次财务调用（最贵，放在估值之前）
  L3 估值           同龄（同行业）比价，0 次额外调用
  L4 粗排           取前 N 进阶段二

⚠️ 阶段一刻意不做「精细判断」：它只负责**排除**和**排序**。
   真正的判断在阶段二，那里才允许下结论。

用法：
    python screen.py                      # 默认股票池
    python screen.py --universe zz500
    python screen.py --codes 600519,000858
    python screen.py --limit 60           # 只跑前 60 只（调试）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import datasource as ds
import metrics as m

WORKERS = 4
OUT = config.OUT_DIR


# ---------------------------------------------------------------- 工具
def is_financial(name: str) -> bool:
    return any(k in str(name) for k in config.FIN_INDUSTRY_KEYWORDS)


def is_st(name: str) -> bool:
    n = str(name).upper()
    return "ST" in n or "退" in n




def risk_warn_codes(names: pd.DataFrame) -> set:
    """从全市场简称名单里挑出风险警示（ST/*ST）与退市整理（简称含「退」）的代码。

    名单来自交易所简称，不依赖行情：停牌股照样能被识别；空名单返回空集合（调用方
    会退回逐只名称判断）。
    """
    if names is None or len(names) == 0:
        return set()
    codes = names["code"].astype(str).str.strip().str.zfill(6)
    labels = names["name"].astype(str)
    mask = labels.str.upper().str.contains("ST", na=False) | labels.str.contains("退", na=False)
    return set(codes[mask])


def is_suspended(row: dict) -> bool:
    """最近一根日线不是数据日 → 停牌或数据缺失（不通过 L1 停牌门槛）。"""
    return str(row.get("last_bar") or "") != ds.data_date()

def pct_rank_in(vals: pd.Series, x: float) -> float | None:
    """x 在同行业样本里的分位。"""
    s = pd.to_numeric(vals, errors="coerce")
    s = s[s > 0].dropna()
    if len(s) < 8 or x is None or x <= 0:
        return None
    return float((s <= x).mean())


# ---------------------------------------------------------------- 股票池
def build_universe() -> pd.DataFrame:
    if config.CUSTOM_CODES:
        snap = ds.fetch_market_snapshot()
        codes = [ds.norm_code(c) for c in config.CUSTOM_CODES]
        df = snap[snap["code"].isin(codes)][["code", "name", "industry"]].copy()
        missing = set(codes) - set(df["code"])
        for c in missing:            # 快照里没有的直接查名称
            df.loc[len(df)] = {"code": c, "name": c, "industry": "未分类"}
        return df.reset_index(drop=True)

    indexes = config.UNIVERSE_PRESETS.get(config.UNIVERSE, [])
    if not indexes:
        raise SystemExit(f"未知股票池: {config.UNIVERSE}")
    frames = []
    for idx in indexes:
        cons = ds.fetch_index_cons(idx)
        cons["index"] = idx
        frames.append(cons)
    df = pd.concat(frames, ignore_index=True).drop_duplicates("code")

    snap = ds.fetch_market_snapshot()
    df = df.merge(snap[["code", "industry"]], on="code", how="left")
    df["industry"] = df["industry"].fillna("未分类")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- 单只评估
def eval_one(code: str, name: str, industry: str) -> dict:
    """取数 + 算指标。任何异常都记录在 error 里，不中断整批。"""
    row = {"code": code, "name": name, "industry": industry,
           "error": None, "data_ok": False}
    try:
        daily = ds.fetch_daily(code)
        fin = ds.fetch_fin(code)
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return row

    if len(daily) < 120 or len(fin) < 8:
        row["error"] = f"数据不足 daily={len(daily)} fin={len(fin)}"
        return row

    close = daily["close"]
    last_close = float(close.iloc[-1])
    shares = m.f(daily["outstanding_share"].iloc[-1]) if "outstanding_share" in daily else None
    row["price"] = round(last_close, 2)
    row["last_bar"] = str(pd.to_datetime(daily["date"].iloc[-1]).date())
    row["list_days"] = len(daily)
    row["mktcap_yi"] = round(last_close * shares / 1e8, 1) if shares else None
    row["avg_amt60_yi"] = m.f(daily["amount"].tail(60).mean())
    row["avg_amt60_yi"] = round(row["avg_amt60_yi"] / 1e8, 2) if row["avg_amt60_yi"] else None

    ann = m.annual_rows(fin)
    last = m.latest_row(fin)
    if last is None:
        row["error"] = "无财务记录"
        return row

    # ---- ROE：近 5 个年报 ----
    roes = [m.roe(r) for _, r in ann.tail(5).iterrows()]
    row["roe_list"] = [None if x is None else round(x, 2) for x in roes]
    row["roe_avg5"] = None if not [x for x in roes if x] else round(
        sum(x for x in roes if x) / len([x for x in roes if x]), 2)
    row["roe_years_ok"] = sum(1 for x in roes if x is not None and x >= config.S1_STOCK["roe_min"])

    # ---- 现金流质量：近 3 年年报 OCF/NI 均值 ----
    ocf = [m.ocf_to_ni(r) for _, r in ann.tail(3).iterrows()]
    row["ocf_ni_3y"] = None if m.mean_ignore_none(ocf) is None else round(
        m.mean_ignore_none(ocf), 3)

    # ---- 负债率（用最新报告期）----
    row["debt_ratio"] = None if m.debt_ratio(last) is None else round(m.debt_ratio(last), 2)

    # ---- 毛利率（最新报告期）----
    row["gross_margin"] = None if m.gross_margin(last) is None else round(m.gross_margin(last), 2)

    # ---- 成长：近 3 年年报同比增长率链式还原 CAGR ----
    rev_g = m.chain_cagr([m.rev_growth(r) for _, r in ann.tail(3).iterrows()])
    pft_g = m.chain_cagr([m.profit_growth(r) for _, r in ann.tail(3).iterrows()])
    row["rev_cagr3"] = None if rev_g is None else round(rev_g, 4)
    row["profit_cagr3"] = None if pft_g is None else round(pft_g, 4)

    # ---- 风险类指标（给阶段二用，阶段一不设门槛）----
    row["max_dd_3y"] = round(m.max_drawdown(close), 4)
    row["vol_3y"] = round(m.volatility(close), 4)
    row["ma200"] = m.ma(close, 200)
    row["atr14"] = m.atr(daily, 14)
    row["data_ok"] = True
    return row


# ---------------------------------------------------------------- 门槛判定
def apply_gates(row: dict, ind_stats: dict) -> dict:
    """逐条判定，每条都留下『值』与『是否通过』，不通过的原因可追溯。"""
    s1 = config.S1_STOCK
    fin_flag = is_financial(row["name"])
    g = {}

    g["L1_size"] = (row.get("mktcap_yi") or 0) >= s1["min_mktcap_yi"]
    g["L1_liquidity"] = (row.get("avg_amt60_yi") or 0) >= s1["min_avg_amt60_yi"]
    g["L1_history"] = (row.get("list_days") or 0) >= s1["min_list_days"]
    g["L1_not_st"] = not is_st(row["name"])
    g["L1_no_suspend"] = not is_suspended(row)

    g["L2_roe"] = row.get("roe_years_ok", 0) >= s1["roe_need_years"]
    g["L2_cash"] = (row.get("ocf_ni_3y") is not None
                    and row["ocf_ni_3y"] >= s1["ocf_ni_min"])
    if fin_flag:
        g["L2_debt"] = True          # 金融业豁免
    else:
        g["L2_debt"] = (row.get("debt_ratio") is not None
                        and row["debt_ratio"] <= s1["debt_max"])
    g["L2_growth"] = ((row.get("rev_cagr3") is not None and row["rev_cagr3"] >= s1["revenue_cagr3_min"])
                      and (row.get("profit_cagr3") is not None
                           and row["profit_cagr3"] >= s1["profit_cagr3_min"]))

    # 估值：同行业比价（阶段一用相对分位，不查个股历史，省 600 次调用）
    st = ind_stats.get(row["industry"], {})
    row["pe"] = None
    row["pb"] = None
    row["pe_pct_ind"] = None
    row["pb_pct_ind"] = None
    g["L3_valuation"] = None      # 由 merge 后的估值列回填

    row["is_financial"] = fin_flag
    row["gates"] = g
    row["stage1_pass"] = all(v for v in g.values() if v is not None) and g["L3_valuation"] is not False
    return row


def apply_valuation_gate(row: dict, snap_index: pd.DataFrame, ind_stats: dict) -> dict:
    s1 = config.S1_STOCK
    try:
        rec = snap_index.loc[row["code"]]
        pe, pb = m.f(rec.get("per")), m.f(rec.get("pb"))
    except KeyError:
        pe = pb = None
    row["pe"], row["pb"] = pe, pb
    st = ind_stats.get(row["industry"], {})
    row["pe_pct_ind"] = pct_rank_in(st.get("pe_series", pd.Series(dtype=float)), pe)
    row["pb_pct_ind"] = pct_rank_in(st.get("pb_series", pd.Series(dtype=float)), pb)
    ok_pe = row["pe_pct_ind"] is not None and row["pe_pct_ind"] <= s1["pe_pct_max"]
    ok_pb = row["pb_pct_ind"] is not None and row["pb_pct_ind"] <= s1["pb_pct_max"]
    ok_pe = bool(ok_pe) and (pe or 0) > 0
    ok_pb = bool(ok_pb) and (pb or 0) > 0
    row["gates"]["L3_valuation"] = bool(ok_pe and ok_pb)
    row["stage1_pass"] = bool(all(v for v in row["gates"].values() if v is not None))
    return row


# ---------------------------------------------------------------- 粗排
def rank_score(row: dict) -> float:
    """阶段一粗排分：只用于排序，不用于下结论。

    权重刻意简单透明：质量 40% / 估值 30% / 现金流 20% / 成长 10%。
    """
    roe = row.get("roe_avg5") or 0
    ocf = row.get("ocf_ni_3y") or 0
    pe_pct = row.get("pe_pct_ind")
    pb_pct = row.get("pb_pct_ind")
    val = 1.0 - ((pe_pct if pe_pct is not None else 1.0) * 0.6
                 + (pb_pct if pb_pct is not None else 1.0) * 0.4)
    growth = (row.get("rev_cagr3") or 0) * 0.5 + (row.get("profit_cagr3") or 0) * 0.5
    q = m.score_linear(roe, 8, 35, 1.0)
    c = m.score_linear(ocf, 0.5, 1.2, 1.0)
    g = m.score_linear(growth, -0.05, 0.25, 1.0)
    return round(0.40 * q + 0.30 * max(0.0, min(1.0, val)) + 0.20 * c + 0.10 * g, 4)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=None)
    ap.add_argument("--codes", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top", type=int, default=None)
    args = ap.parse_args()
    if args.universe:
        config.UNIVERSE = args.universe
    if args.codes:
        config.CUSTOM_CODES = [c.strip() for c in args.codes.split(",") if c.strip()]

    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    print("[L0] 构建股票池 ...", flush=True)
    uni = build_universe()
    if args.limit:
        uni = uni.head(args.limit)
    print(f"      股票池 {len(uni)} 只", flush=True)

    # 历史退市名单（PIT 口径）：候选池理论上不该出现已退市代码（它们没有当日行情），
    # 但指数成分/名单口径偶尔滞后，这里防御性剔一次，并把结果记进漏斗。
    n_uni0, n_risk, n_delisted = len(uni), 0, 0
    try:
        hit = uni['code'].isin(risk_warn_codes(ds.fetch_code_names()))
        n_risk = int(hit.sum())
        if n_risk:
            print('      剔除风险警示/退市整理 ' + str(n_risk) + ' 只：' + ','.join(uni.loc[hit, 'code'].head(5)), flush=True)
        uni = uni[~hit].reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        print('      风险警示名单未取到（' + type(e).__name__ + '），退回名称判断：' + str(e)[:80], flush=True)

    try:
        dl = ds.fetch_delisted()
        if dl is not None and len(dl):
            hit = uni['code'].isin(dl['代码'].astype(str).str.zfill(6))
            n_delisted = int(hit.sum())
            if n_delisted:
                print('      剔除已退市 ' + str(n_delisted) + ' 只：' + ','.join(uni.loc[hit, 'code'].head(5)), flush=True)
            uni = uni[~hit].reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        print('      退市名单未取到（' + type(e).__name__ + '）：' + str(e)[:80], flush=True)

    print("[L0] 拉全市场快照 ...", flush=True)
    snap = ds.fetch_market_snapshot()
    snap_index = snap.set_index("code")
    # 预计算每个行业的 PE/PB 序列（供分位使用）
    ind_stats = {}
    for ind, grp in snap.groupby("industry"):
        ind_stats[ind] = {"pe_series": grp["per"], "pb_series": grp["pb"],
                          "pe_med": m.f(grp["per"].median()), "pb_med": m.f(grp["pb"].median())}
    print(f"      行业数 {len(ind_stats)}，覆盖 {len(snap)} 只", flush=True)

    print("[L2] 逐只取财务与行情（最慢的一步）...", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(eval_one, r["code"], r["name"], r["industry"]): r
                for _, r in uni.iterrows()}
        for fut in as_completed(futs):
            done += 1
            try:
                rows.append(fut.result())
            except Exception:                       # noqa: BLE001
                r = futs[fut]
                rows.append({"code": r["code"], "name": r["name"],
                             "industry": r["industry"], "data_ok": False,
                             "error": traceback.format_exc()[-160:]})
            if done % 25 == 0 or done == len(futs):
                print(f"      {done}/{len(futs)}  用时 {time.time()-t0:.0f}s", flush=True)

    df = pd.DataFrame(rows)
    ok = df[df["data_ok"] == True]  # noqa: E712
    print(f"\n[L2] 取数成功 {len(ok)}/{len(df)}，失败 {len(df)-len(ok)}", flush=True)

    # 逐条门槛判定
    judged = [apply_gates(r, ind_stats) for r in ok.to_dict("records")]
    for r in judged:
        apply_valuation_gate(r, snap_index, ind_stats)
    jdf = pd.DataFrame(judged)
    if len(ok) == 0 or jdf.empty or "gates" not in jdf.columns:
        raise SystemExit(
            f"[L2] 全部 {len(df)} 只取数失败，阶段一无法继续：先检查网络/数据源"
            "（stock/skilldata/datasource.py）与缓存目录，再重跑；"
            "取数失败时不许用旧结果冒充新结果（SKILL §1 硬规矩）。")

    funnel = {"L0_排除风险警示": n_risk, "L0_已退市": n_delisted}
    for gate in ["L1_not_st", "L1_no_suspend", "L1_size", "L1_liquidity", "L1_history",
                 "L2_roe", "L2_cash", "L2_debt", "L2_growth", "L3_valuation"]:
        funnel[gate] = int(jdf["gates"].apply(lambda g, k=gate: bool(g.get(k))).sum())
    survivors = jdf[jdf["stage1_pass"] == True].copy()  # noqa: E712
    funnel["全部门槛通过"] = int(len(survivors))

    survivors["rank_score"] = survivors.apply(rank_score, axis=1)
    survivors = survivors.sort_values("rank_score", ascending=False)
    top_n = args.top or config.S1_STOCK["top_n"]
    selected = survivors.head(top_n)

    cols = ["code", "name", "industry", "price", "mktcap_yi", "avg_amt60_yi",
            "roe_avg5", "roe_list", "ocf_ni_3y", "debt_ratio", "gross_margin",
            "rev_cagr3", "profit_cagr3", "pe", "pb", "pe_pct_ind", "pb_pct_ind",
            "max_dd_3y", "vol_3y", "ma200", "atr14", "is_financial", "rank_score"]
    all_path = os.path.join(OUT, "stage1_all.csv")
    # 按代码排序：逐只取数是 4 线程并行完成的，若按完成顺序落盘，
    # 同一份数据跑两遍会得到行序不同的 stage1_all.csv（结论不受影响，但审计/对比会误报差异）。
    jdf.sort_values("code").to_csv(all_path, index=False, encoding="utf-8-sig")
    sel_path = os.path.join(OUT, "stage1_candidates.csv")
    selected[cols].to_csv(sel_path, index=False, encoding="utf-8-sig")

    funnel["股票池"] = n_uni0
    funnel["进入逐只取数"] = len(uni)
    funnel["取数成功"] = len(ok)
    funnel["进入阶段二"] = len(selected)
    funnel["elapsed_sec"] = round(time.time() - t0, 1)
    with open(os.path.join(OUT, "stage1_funnel.json"), "w", encoding="utf-8") as fh:
        json.dump(funnel, fh, ensure_ascii=False, indent=1)
    ds.dump_provenance("provenance_stage1")

    print("\n=== 漏斗 ===")
    for k, v in funnel.items():
        print(f"  {k}: {v}")
    print(f"\n=== 进入阶段二（前 {len(selected)}）===")
    for _, r in selected.iterrows():
        print(f"  {r['code']} {r['name']:<8}{r['industry']:<10} "
              f"ROE5={r['roe_avg5']}%  PE={r['pe']}  PB={r['pb']}  "
              f"行业PE分位={None if r['pe_pct_ind'] is None else round(r['pe_pct_ind'],2)}  "
              f"粗排={r['rank_score']}")
    print(f"\n输出: {sel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
