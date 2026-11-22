# -*- coding: utf-8 -*-
"""cash_pool.py —— 全市场货基：按 90 天实得年化排行，给现金层选基。

宇宙（「朝朝盈2号就放弃不用了，直接对接 90 天利润最高的货基」）：
  不再绑招行「朝朝盈2号」那 100 支（那是招行 App 的代销入口，不是产品范围）。
  默认 universe=market：用 `ds.fetch_money_fund_rates()` 取全市场货基（通常 500–600 只），
  按近 28 日年化 / 近 1 年收益做粗排取前 --shortlist 只，再逐只取逐日万份收益、
  按 90 天实得年化精排；**渠道无关**——同一支基金代码在券商 App 基金频道、基金公司
  直销、银行 App 都能买，货基申购/赎回费都是 0（销售服务费已含在净值里）。
  universe=chaochao2：旧口径（读 cash_pool_chaochao2.json 的 100 支），仅作回滚/对照。
  每支每日快赎上限 1 万元是监管对货基 T+0 快赎的行业规则，与渠道无关，照旧保留。

为什么需要这个脚本（而不是直接看 App 的排名）：
  App「产品明细」按 **7 日年化** 从高到低排，而 7 日年化是**滞后的滚动均值**——
  某只基金单日万份收益爆一下（国投瑞银增利宝 2026-11-12 的 0.998 元/万份，
  折算 1 天 0.00998% ≈ 年化 3.6%），7 日年化会立刻从 0.95% 跳到 1.34%，7 天后
  又掉回 1.0% 附近。照这个排名买，等于专挑「刚爆完单日收益、即将回落」的那只。
  本脚本改用 **Σ每万份收益 在该窗口的实得年化**（不被单日脉冲骗），默认按 90 天排，
  并同时给 30/7 天，方便看排名是否稳定。

口径（全部是「占本金年化」，可直接和朝朝宝/逆回购比）：
  窗口实得年化 = Σ(每万份收益) / 10000 × 365 / 窗口自然日数
  注意：货基按自然日计息（周末节假日照计），所以分母用自然日跨度而非行数。

用法：
  python cash_pool.py                  # 打印排行 + 推荐，落 out/cash_pool_rank.md
  python cash_pool.py --top 25
  python cash_pool.py --cash 27000     # 推荐时按这个金额算加权年化和年收益
  python cash_pool.py --min-days 90    # 历史不足的基金直接跳过（默认 90）

产物：out/cash_pool_rank.md（排行 + 推荐）、out/cash_pool_rank.json（机器可读）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # noqa: E402
import datasource as ds  # noqa: E402

POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "cash_pool_chaochao2.json")
OUT_DIR = getattr(config, "OUT_DIR", None) or os.path.join(config.WORK_DIR, "out")

# 窗口（自然日）→ 用于算实得年化
WINDOWS = (7, 30, 90, 180)


def load_pool(path: str = POOL_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["funds"]


def load_universe_market(shortlist: int = 60) -> list[dict]:
    """全市场货基粗排：取收益靠前的一批，交给逐日数据去精算 90 天实得年化。

    粗排只用于缩小取数范围（600 只逐只翻历史太慢），口径为近 1 年收益
    （ret_1y_pct），缺失时退回 28 日年化（y28_pct）。最终排名一律以 90 天
    实得年化为准，粗排不参与推荐。
    """
    df = ds.fetch_money_fund_rates()
    if df is None or df.empty:
        raise RuntimeError("全市场货基排行取不到（ds.fetch_money_fund_rates 空）")
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}")]
    df["name"] = df["name"].astype(str).str.strip()
    df = df[df["name"] != ""]
    for c in ("ret_1y_pct", "y28_pct", "y7_pct", "wanfen"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    key = df["ret_1y_pct"].where(df["ret_1y_pct"].notna(), df["y28_pct"])
    df = df.assign(_key=key).sort_values("_key", ascending=False, na_position="last")
    out = []
    for _, r in df.head(max(1, int(shortlist))).iterrows():
        out.append({"code": r["code"], "name": r["name"], "app_name": r["name"],
                    "app_yield": (float(r["y28_pct"]) if pd.notna(r.get("y28_pct")) else None),
                    "ret_1y": (float(r["ret_1y_pct"]) if pd.notna(r.get("ret_1y_pct")) else None)})
    return out


def purchase_filter(codes: list[str]) -> tuple[set[str], dict[str, str]]:
    """用场外基金申购快照筛掉「暂停申购 / 起购门槛高于 1 万 / 日限购 < 1 万」的货基。

    注意「限制大额申购」不是不能买，而是超过某个日限购金额就不能买——只要日限购
    ≥1 万（货基快赎单支上限就是 1 万/日）就可以正常买。快照取不到时 fail-open
    （全部放行并返回空 reason），不让排行整个跑不出来。
    """
    try:
        snap = ds.fetch_fund_purchase_snapshot()
    except Exception:      # noqa: BLE001 —— 快照是增强项，不是必需项
        return set(codes), {}
    if snap is None or snap.empty:
        return set(codes), {}
    snap = snap[snap["code"].astype(str).isin(set(codes))]
    ok, why = set(), {}
    for _, r in snap.iterrows():
        code = str(r["code"])
        status = str(r.get("purchase_status") or "")
        if "暂停" in status or not status.strip():
            why[code] = f"申购状态「{status or '未知'}」"
            continue
        min_buy = r.get("min_purchase")
        if pd.notna(min_buy) and float(min_buy) > 10000.0:
            why[code] = f"起购 {float(min_buy):,.0f} 元 > 1 万"
            continue
        cap = r.get("day_limit")
        if "限大额" in status and pd.notna(cap) and 0 < float(cap) < 10000.0:
            why[code] = f"日限购 {float(cap):,.0f} 元 < 1 万"
            continue
        ok.add(code)
    return ok, why


def window_return(df: pd.DataFrame, days: int) -> float | None:
    """窗口内实得年化（小数）。df 需含 date/wanfen，date 升序。"""
    if df.empty:
        return None
    end = pd.to_datetime(df["date"].iloc[-1])
    start = end - pd.Timedelta(days=days)
    win = df[pd.to_datetime(df["date"]) > start]
    if len(win) < max(5, days // 3):      # 数据缺口太大 → 不报
        return None
    span = (pd.to_datetime(win["date"].iloc[-1]) - pd.to_datetime(win["date"].iloc[0])).days + 1
    tot = float(pd.to_numeric(win["wanfen"], errors="coerce").fillna(0).sum()) / 10000.0
    age = (dt.date.today() - pd.to_datetime(df["date"].iloc[-1]).date()).days
    if age > 7:                            # 快照太旧，年化不可信
        return None
    return tot * 365.0 / span


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全市场货基 90 天实得年化排行（现金层选基）")
    ap.add_argument("--universe", choices=("market", "chaochao2"), default="market",
                    help="market=全市场货基（默认）；chaochao2=旧口径招行 100 支池（回滚用）")
    ap.add_argument("--shortlist", type=int, default=80,
                    help="全市场粗排后进入精算的只数（默认 80，越大越慢越全）")
    ap.add_argument("--pool", default=POOL_FILE, help="chaochao2 口径的池文件")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--cash", type=float, default=27000.0,
                    help="按这个金额给加权年化与年收益（默认 27000，即当前观望资金）")
    ap.add_argument("--min-days", type=int, default=90, help="历史少于多少天的跳过")
    ap.add_argument("--hist-days", type=int, default=200,
                    help="逐只取多少天万份收益历史（默认 200，够算 180 天窗口）")
    ap.add_argument("--json", default=os.path.join(OUT_DIR, "cash_pool_rank.json"))
    ap.add_argument("--md", default=os.path.join(OUT_DIR, "cash_pool_rank.md"))
    a = ap.parse_args(argv)

    if a.universe == "market":
        pool = load_universe_market(a.shortlist)
        have = [e for e in pool if e.get("code")]
        missing = []
        ok_codes, why_blocked = purchase_filter([e["code"] for e in have])
        if ok_codes and len(ok_codes) < len(have):
            have = [e for e in have if e["code"] in ok_codes]
    else:
        pool = load_pool(a.pool)
        have = [e for e in pool if e.get("code")]
        missing = [e["app_name"] for e in pool if not e.get("code")]
        why_blocked = {}

    rows, errs = [], [(f"限购/暂停（{c}）", w) for c, w in why_blocked.items()]
    print(f"候选 {len(have)} 只（粗排前 {a.shortlist}，筛掉限购 {len(why_blocked)} 只），"
          f"逐只取 {a.hist_days} 天历史…", file=sys.stderr, flush=True)
    for i, e in enumerate(have, 1):
        if i % 10 == 0:
            print(f"  取数 {i}/{len(have)} …", file=sys.stderr, flush=True)
        try:
            h = ds.fetch_money_fund_hist(e["code"], days=a.hist_days)
            if len(h) < a.min_days:
                errs.append((e["app_name"], f"历史仅 {len(h)} 天"))
                continue
            h = h.sort_values("date").reset_index(drop=True)
            r = {"code": e["code"], "name": e["app_name"],
                 "app_y7": e.get("app_yield"),
                 "y7": window_return(h, 7), "y30": window_return(h, 30),
                 "y90": window_return(h, 90), "y180": window_return(h, 180),
                 "last_date": str(h["date"].iloc[-1]),
                 "buy": str(h["buy"].iloc[-1]), "sell": str(h["sell"].iloc[-1])}
            r["y7_ak"] = float(pd.to_numeric(h["y7_pct"], errors="coerce").iloc[-1]) / 100.0
            rows.append(r)
        except Exception as exc:      # 单只失败不该毁掉整份排行
            errs.append((e["app_name"], f"{type(exc).__name__}: {exc}"))

    ok = [r for r in rows if r.get("y90") is not None]
    ok.sort(key=lambda r: -r["y90"])
    if not ok:
        print("没有可用的 90 天数据：", errs[:5])
        return 1

    # ---- 推荐：每支 1 万快赎额度，按 90 天排名填满（切分逻辑与账本共用 cash_sleeve）----
    data_date = max(str(r.get("last_date") or "") for r in ok)
    try:
        import cash_sleeve as cs
        sl = cs.plan(a.cash, rank={"rows": ok, "data_date": data_date,
                                   "as_of": dt.date.today().isoformat()})
        lots = sl["holdings"]
        wavg = sl["weighted_rate"]
    except Exception:      # 切分模块坏了也不能让排行跑不出来：回退到本文件的简单切分
        lots, remain = [], a.cash
        for r in ok:
            if remain <= 0:
                break
            amt = min(10000.0, remain)
            lots.append({"code": r["code"], "name": r["name"], "amount": amt,
                         "y90": r["y90"], "rate": r["y90"]})
            remain -= amt
        wavg = (sum(l["amount"] * l["y90"] for l in lots) / sum(l["amount"] for l in lots)
                if lots else 0.0)

    r1 = float(config.cash_rates().get("r1") or 0.0)
    print(f"可用 {len(ok)} 只（候选 {len(have)} / 池 {len(pool)}，跳过 {len(errs)} 只）"
          f"  数据日 {ok[0]['last_date']}")
    print(f"\n{'#':>3} {'名称':<22}{'7日':>7}{'30日':>8}{'90日':>8}{'180日':>9}  {'状态'}")
    for i, r in enumerate(ok[:a.top], 1):
        f = lambda v: f"{v * 100:6.3f}%" if v is not None else "   n/a"
        print(f"{i:>3} {r['name']:<22}{f(r['y7'])}{f(r['y30'])}{f(r['y90'])}{f(r['y180'])}  {r['buy']}")

    print(f"\n朝朝宝基准 {r1 * 100:.2f}%；按 90 天实得年化取前 {len(lots)} 支（每支 1 万快赎额度）")
    for l in lots:
        print(f"  {l['name']:<22} {l['amount']:>8.0f} 元  90日 {l['y90'] * 100:.3f}%")
    print(f"  加权 {wavg * 100:.3f}%  → {a.cash:,.0f} 元年收益约 {a.cash * wavg:,.0f} 元"
          f"（朝朝宝同金额 {a.cash * r1:,.0f} 元）")

    # ---- 落盘 ----
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump({"as_of": dt.date.today().isoformat(),
               "data_date": data_date,
               "universe": a.universe, "shortlist": a.shortlist,
               "pool_total": len(pool), "resolved": len(have), "ranked": len(ok),
               "cash": a.cash, "weights": lots, "weighted_y90": wavg,
               "r1": r1, "rows": ok, "unresolved": missing,
               "skipped": [{"name": n, "why": w} for n, w in errs]},
              open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    title = ("全市场货基" if a.universe == "market" else "朝朝盈2号 快赎货基池")
    md = [f"## {title} · 90 天实得年化排行（数据日 {ok[0]['last_date']}）", "",
          "口径：窗口内 Σ每万份收益 / 10000 × 365 / 自然日数，占本金年化。",
          "渠道无关：同一支货基代码在券商 App 基金频道 / 基金公司直销 / 银行 App 都能买，"
          "申购赎回费均为 0；每支每日快赎 1 万元是监管规则，与渠道无关。",
          f"对照：朝朝宝 {r1 * 100:.2f}%。", "",
          "| # | 基金 | 7日 | 30日 | 90日 | 180日 | 申购状态 |",
          "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(ok[:a.top], 1):
        p = lambda v: f"{v * 100:.3f}%" if v is not None else "n/a"
        md.append(f"| {i} | {r['name']} | {p(r['y7'])} | {p(r['y30'])} | {p(r['y90'])} | "
                  f"{p(r['y180'])} | {r['buy']} |")
    md += ["", f"**{a.cash:,.0f} 元方案**（每支 1 万快赎额度）："
               + "；".join(f"{l['name']} {l['amount']:,.0f} 元" for l in lots),
           "", f"加权 90 天年化 **{wavg * 100:.3f}%** → 年收益约 **{a.cash * wavg:,.0f} 元**"
               f"（朝朝宝同金额 {a.cash * r1:,.0f} 元）。"]
    if missing:
        md += ["", f"未解析代码（App 有、取数快照没匹配上）：{'、'.join(missing)}"]
    open(a.md, "w", encoding="utf-8").write("\n".join(md) + "\n")
    print(f"\n已写 {a.md}\n已写 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
