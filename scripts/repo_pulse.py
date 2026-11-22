# -*- coding: utf-8 -*-
"""repo_pulse.py —— 逆回购：**投资层脉冲择时**（不再属于现金层）。

2026-11-15：「把逆回购从现金层删掉，作为投资层，以后尽量选择获取利润
最高的时期才逆回购」。现金层的底仓从此是「朝朝宝 + 半流动性货基池」（见 cash_sleeve.py），
逆回购只在**它那段年化明显高于底仓**时才做——它不再是现金的默认去处，而是一笔有门槛、
有窗口、要挑时机的机会型 operation。

为什么必须有门槛（别再「每周一固定买」）：
  现金层底仓（朝朝宝 1.06% / 货基池约 1.31%）是**随时可赎回**的，逆回购要锁 1–28 天。
  1 天期的年化费率折算是 0.001%×250 笔 = 0.365%/本金·年，所以：
      超额年化 = 报价 − 底仓年化 − 手续费年化(期限)
  2026-11-15 实测：GC001 近 250 日均 1.352%，对底仓 1.31% 的超额只有 −0.32%（倒贴）；
  只有报价冲到 **≥1.68%** 才勉强打平、≥2.0% 才有 30bp 超额——这正是脉冲发生的时候
  （2025-01-17 3.955%、2025-06-27 3.890%、跨长假前的 9 天期）。

真正的甜点是**跨长假**：1 天期吃 9 天息、只收 1 笔费，2025-01-27 那种 2.815% 的报价
折年化 2.774%（`--days 9` 即可算）。所以本脚本给的是**GO / NO-GO 判断 + 金额 + 期限建议**，
不是「每天买一点」。

期限规则：**按利润最大选期限**——用同一窗口的
**净 %-天**（含手续费）比较 1 天期与 7 天期（给了 14 天期报价时也比 14 天期），取最大者；
跨周末/长假按**实际占款天数**计息（到期日遇非交易日顺延、顺延期间也计息），用交易日历算。
`--need-build`（锁定期内要建仓）时强制 1 天期，防止把建仓弹药锁死。28 天期不自动推荐。

用法：
  python repo_pulse.py                        # 自动比较 1 天期 / 7 天期，取利润最大
  python repo_pulse.py --rate 2.2             # 用手里的实时报价（百分数）
  python repo_pulse.py --days 9 --rate 2.8    # 跨长假：占款 9 天
  python repo_pulse.py --rate14 2.4           # 春节前把 14 天期报价给它，一起比
  python repo_pulse.py --need-build           # 锁定期内要建仓 → 强制只用 1 天期
  python repo_pulse.py --term 7 --rate7 2.6   # 人工指定期限（跳过规则）
  python repo_pulse.py --amount 27000 --bench 0.0131

产物：out/repo_pulse.md（判断 + 算账），stdout 同步打印。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # noqa: E402
import cash_sleeve     # noqa: E402

OUT_DIR = getattr(config, "OUT_DIR", None) or os.path.join(config.WORK_DIR, "out")


def _cfg() -> dict:
    c = (getattr(config, "CAPITAL", {}) or {}).get("cash_layer") or {}
    p = c.get("repo_pulse") or {}
    return {"enabled": bool(p.get("enabled", True)),
            "min_edge_bp": float(p.get("min_edge_bp") or 30.0),
            "max_hold_days": int(p.get("max_hold_days") or 10),
            "min_days": int(p.get("min_days") or 3),
            "bench_from": str(p.get("bench_from") or "sleeve")}


def bench_rate(amount: float = 27000.0) -> tuple[float, str]:
    """底仓年化：��流动性货基池的加权 90 天实得年化；拿不到就退回朝朝宝。"""
    try:
        sl = cash_sleeve.plan(amount)
        if sl.get("holdings"):
            return float(sl["weighted_rate"]), (f"半流动性货基池 {len(sl['holdings'])} 支加权"
                                                f"（数据日 {sl.get('data_date') or '-'}）")
    except Exception:
        pass
    r1 = float(config.cash_rates().get("r1") or 0.0)
    return r1, "朝朝宝（货基池排行不可用，回退）"


def fee_rate(term: int = 1) -> float:
    """按**合约期限**的单笔费率（1 天期 0.001%、7 天 0.005%、14 天 0.010%、28 天 0.020%）。

    注意：交易日收的是「这一笔」的费，而交易所按**实际占款天数**计息——所以跨长假买
    1 天期、占款 9 天，只收 1 笔 0.001%（折年化 0.0406%），不是按 14 天期收 0.010%。
    """
    rates = {int(k): float(v) for k, v in
             ((getattr(config, "PAPER", {}) or {}).get("cash_yield") or {})
             .get("term_fee_rates", {}).items()}
    if not rates:
        rates = {1: 0.00001, 2: 0.00002, 3: 0.00003, 4: 0.00004,
                 7: 0.00005, 14: 0.0001, 28: 0.0002}
    key = min((k for k in rates if k >= int(term)), default=max(rates))
    disc = float(config.cash_rates().get("repo_commission_discount", 1.0))
    return rates[key] * disc


def fee_annual(term: int = 1, days: int = None) -> float:
    """单笔费率折算成**占本金年化**（分母是实际占款天数，不是合约期限）。"""
    d = int(days or term)
    return fee_rate(term) * 365.0 / max(1, d)


def _trade_cal() -> set[str] | None:
    """交易日历（含已排定未来交易日）；取不到返回 None（退化为跳周末）。"""
    try:
        import datasource as ds
        df = ds.fetch_trade_calendar()
        days = {str(x)[:10] for x in df["trade_date"].tolist()}
        return days or None
    except Exception:                              # noqa: BLE001 - 日历取不到不能拖垮判断
        return None


def settle_day(as_of: dt.date, cal: set[str] | None = None) -> dt.date:
    """成交日：as_of 是交易日就用它，否则顺延到下一个交易日。"""
    cur = as_of
    for _ in range(60):
        ok = (cur.isoformat() in cal) if cal else (cur.weekday() < 5)
        if ok:
            return cur
        cur += dt.timedelta(days=1)
    return as_of


def actual_days(as_of: dt.date, term: int, cal: set[str] | None = None) -> int:
    """合约 `term` 天的**实际占款自然日**（交易所计息口径）。

    到期日 = 成交日 + term 个自然日；到期日遇非交易日顺延到下一个交易日，顺延期间
    照样按成交利率计息。所以周五买 1 天期 = 3 天、节前最后交易日买 1 天期 = 整个假期。
    """
    settle = settle_day(as_of, cal)
    cur = settle + dt.timedelta(days=max(1, int(term)))
    for _ in range(90):
        ok = (cur.isoformat() in cal) if cal else (cur.weekday() < 5)
        if ok:
            return max(1, (cur - settle).days)
        cur += dt.timedelta(days=1)
    return max(1, (int(term)))


def choose_term(rate_1d: float, days_1d: int, rate_7d: float | None, bench: float,
                need_build: bool = False, rate_14: float | None = None,
                days_7: int | None = None, days_14: int | None = None) -> dict:
    """期限选择：**按同一窗口的净 %-天 取最大**（「尽量争取最大利润」）。

    同一个窗口 H = max(各候选实际占款天数)，对每个候选算「这笔钱到 H 天为止赚多少」：
        候选收益 = 报价 × 实际占款天数 + 底仓 × (H − 实际占款天数) − 手续费(折 365 天)
    手续费按**合约期限**只收一笔（1 天 0.001%、7 天 0.005%、14 天 0.010%），所以长
    期限的费摊到每天更低；1 天期跨周末/长假本来就吃 3–11 天息、只收一笔费。
    同等收益取期限短的（流动性更好、费更低）。`need_build=True` 时强制 1 天期：锁定期
    里可能要建仓，长钱不能动。
    """
    days_1d = max(1, int(days_1d))
    cands = [(1, float(rate_1d), days_1d)]
    if rate_7d is not None:
        cands.append((7, float(rate_7d), max(1, int(days_7 or 7))))
    if rate_14 is not None:
        cands.append((14, float(rate_14), max(1, int(days_14 or 14))))
    h = max(d for _, _, d in cands)
    b = float(bench)
    scores = {term: float(rate) * d + b * (h - d) - fee_rate(term) * 365.0
              for term, rate, d in cands}
    best_term, _, best_days = max(cands, key=lambda c: (scores[c[0]], -c[0]))
    out = {"term": best_term, "window_days": h, "need_build": bool(need_build),
           "one_day_score": round(scores[1], 4),
           "seven_day_score": round(scores[7], 4) if 7 in scores else None,
           "fourteen_day_score": round(scores[14], 4) if 14 in scores else None,
           "scores": {str(t): round(v, 4) for t, v in sorted(scores.items())},
           "days": {str(term): d for term, _, d in cands}}
    detail = "，".join(f"{t} 天期 {scores[t]:.2f}" for t in sorted(scores))
    if need_build and best_term != 1:
        out["term"] = 1
        out["why"] = (f"1 天期：锁定期内要建仓弹药，长钱不能动"
                      f"（净 %-天：{detail}）")
    elif need_build:
        out["why"] = f"1 天期：锁定期内要建仓弹药（净 %-天：{detail}）"
    else:
        out["why"] = (f"{best_term} 天期：同窗口净 %-天最高（{detail}；"
                      f"窗口 H={h} 天，已扣手续费）")
    return out


def decide(rate: float, days: int, bench: float, amount: float,
           min_edge_bp: float = 30.0, max_hold_days: int = 10,
           term: int = 1, min_days: int = 3) -> dict:
    """纯函数：给报价/占款天数/底仓，判断这笔逆回购值不值得做。

    rate/bench 为小数（0.02 = 2%）；days 为占款自然日；term 为合约期限（默认 1 天期，
    跨长假也是买 1 天期、吃 9 天息）。返回可以 JSON 化的判断结果。

    **占款 < min_days 直接 NO-GO**：平时 1 天期只多赚几毛钱，却要把钱从货基搬出来
    再搬回去（赎回 T+1），得不偿失；只在跨周末（≥3 天）和节前长假窗口做。
    """
    days = int(days)
    term = int(term)
    rate = float(rate)
    bench = float(bench)
    amt = round(max(0.0, float(amount)), 2)
    fa = fee_annual(term, days)
    edge_annual = round(rate - bench - fa, 6)
    gross = round(amt * rate * days / 365.0, 2)
    fee = round(amt * fa * days / 365.0, 2)
    edge = round(gross - fee - amt * bench * days / 365.0, 2)
    reasons = []
    if days < int(min_days):
        reasons.append(f"实际占款 {days} 天 < {int(min_days)} 天窗口（平时不做："
                       "多赚的几毛钱抵不上把钱搬出搬回的损耗，只等跨周末/长假）")
    if days > int(max_hold_days):
        reasons.append(f"占款 {days} 天 > 上限 {int(max_hold_days)} 天（会锁住建仓弹药）")
    if amt < float(config.CAPITAL["cash_layer"]["repo_min_lot"]):
        reasons.append(f"金额 {amt:,.0f} 元 < 1,000 元整手")
    if edge_annual * 10000.0 < float(min_edge_bp):
        reasons.append(f"超额年化 {edge_annual:.2%} < 门槛 {float(min_edge_bp) / 100.0:.2%}"
                       f"（报价要 ≥ {(bench + fa + float(min_edge_bp) / 10000.0):.2%} 才够）")
    return {"go": not reasons, "rate": rate, "days": days, "term": term, "bench": bench,
            "fee_annual": round(fa, 6), "edge_annual": edge_annual,
            "gross": gross, "fee": fee, "edge": edge,
            "amount": amt, "reasons": reasons,
            "breakeven_rate": round(bench + fa + float(min_edge_bp) / 10000.0, 6)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逆回购脉冲择时判断（投资层，不是现金层）")
    ap.add_argument("--rate", type=float, default=None, help="逆回购年化报价（百分数，如 2.2）")
    ap.add_argument("--days", type=int, default=None,
                    help="指定占款自然日（默认按交易日历算：周五=3、节前=整个假期）")
    ap.add_argument("--term", type=int, default=None,
                    help="强制合约期限（1/7/14）；不给就按利润最大自动选")
    ap.add_argument("--rate7", type=float, default=None,
                    help="7 天期（R-007）年化报价（百分数）；不给就取最新收盘")
    ap.add_argument("--rate14", type=float, default=None,
                    help="14 天期（R-014）年化报价（百分数）；给了才参与比较，需手工从券商看")
    ap.add_argument("--need-build", action="store_true",
                    help="锁定期内需要建仓弹药 → 只能用 1 天期，不许锁长期限")
    ap.add_argument("--amount", type=float, default=None,
                    help="金额（默认取朝朝宝机动额度 = T+0 保留 − 挂单冻结）")
    ap.add_argument("--bench", type=float, default=None, help="底仓年化（小数，默认自动取货基池加权）")
    ap.add_argument("--json", default=os.path.join(OUT_DIR, "repo_pulse.json"))
    ap.add_argument("--md", default=os.path.join(OUT_DIR, "repo_pulse.md"))
    a = ap.parse_args(argv)

    today = dt.date.today()
    cal = _trade_cal()
    days = a.days if a.days is not None else actual_days(today, 1, cal)
    # 金额：默认取「观望资金」（可动用 − 挂单占用），用账本算；拿不到就 27,000
    amount = a.amount
    if amount is None:
        # 只用**朝朝宝里的机动额度**：T+0 保留 − 场内挂单冻结。
        # 货基池的钱要先赎回（T+1），为几块钱的脉冲把整池钱搬出来不值得，
        # 建仓弹药（周一可能变买单）也不许锁。
        amount = 0.0
        try:
            import paper as _paper
            st = _paper.load()
            plan = _paper.cash_yield_plan(st)
            amount = max(0.0, float(plan.get("t0_keep") or 0.0)
                         - float(plan.get("inhouse_reserved") or 0.0))
        except Exception:
            amount = 0.0
    bench, bench_src = (a.bench, "人工指定") if a.bench is not None else bench_rate(amount)
    rate = a.rate
    rate_src = "人工指定报价"
    if rate is None:
        try:
            import datasource as ds
            r = ds.fetch_repo_rate() or {}
            rate = float(r.get("last_pct") or 0.0)
            rate_src = f"GC001 最新收盘（{r.get('sample_end') or '-'}）"
        except Exception as exc:
            print(f"取 GC001 报价失败（{type(exc).__name__}: {exc}），用 --rate 手动给", file=sys.stderr)
            return 2
    if not rate:
        print("没有报价，无法判断（用 --rate 手动给）", file=sys.stderr)
        return 2

    c = _cfg()
    # 期限规则：按净 %-天 取最大——自动比 1 天期与
    # 7 天期；给了 --rate14 再把 14 天期放进来比。--need-build 强制 1 天期。
    rate7 = a.rate7
    rate7_src = "人工指定 7 天期报价"
    if rate7 is None and not a.term:
        try:
            import datasource as ds
            q = ds.fetch_repo_rate_7d() or {}
            rate7 = float(q.get("last_pct") or 0.0) or None
            if rate7:
                rate7_src = f"R-007 最新收盘（{q.get('sample_end') or '-'}）"
        except Exception:                       # noqa: BLE001 - 7 天期取不到就只算 1 天期
            rate7 = None
    days7 = actual_days(today, 7, cal)
    days14 = actual_days(today, 14, cal)
    rate14_src = "人工指定 14 天期报价（券商 App）"
    term, days_used, choice = 1, days, None
    if a.term:                                  # 人工指定期限：按该期限算占款天数
        term = int(a.term)
        days_used = a.days if a.days is not None else actual_days(today, term, cal)
        if term == 7 and rate7:
            rate, rate_src = rate7, rate7_src
        elif term == 14 and a.rate14:
            rate, rate_src = a.rate14, rate14_src
    else:
        choice = choose_term(float(rate) / 100.0, days,
                             float(rate7) / 100.0 if rate7 else None, bench, a.need_build,
                             rate_14=float(a.rate14) / 100.0 if a.rate14 else None,
                             days_7=days7, days_14=days14)
        term = int(choice["term"])
        if term == 7 and rate7:                  # 锁 7 天：按 7 天期报价与 7 天期占款算
            rate, days_used, rate_src = rate7, days7, rate7_src
        elif term == 14 and a.rate14:
            rate, days_used, rate_src = a.rate14, days14, rate14_src
    d = decide(float(rate) / 100.0, days_used, bench, amount, c["min_edge_bp"], c["max_hold_days"],
               term=term, min_days=c["min_days"])
    d.update({"date": today.isoformat(), "rate_source": rate_src, "bench_source": bench_src,
              "min_edge_bp": c["min_edge_bp"], "max_hold_days": c["max_hold_days"],
              "min_days": c["min_days"],
              "term": term, "term_choice": choice})

    verdict = "GO（值得做）" if d["go"] else "NO-GO（不如留在货基池）"
    lines = [f"## 逆回购脉冲判断 {today.isoformat()}：**{verdict}**", "",
             f"- 报价 {d['rate']:.3%}（{rate_src}）／底仓 {d['bench']:.3%}（{bench_src}）"
             f"／占款 {days_used} 天／期限 {term} 天期／金额 {d['amount']:,.0f} 元",
             f"- 期限选择：{choice['why'] if choice else '人工指定 ' + str(term) + ' 天期'}",
             f"- 手续费折算年化 {d['fee_annual']:.3%}；**超额年化 {d['edge_annual']:+.3%}**"
             f"（门槛 {c['min_edge_bp'] / 100:.2f}%）",
             f"- 这笔算账：毛利 {d['gross']:,.2f} 元 − 费 {d['fee']:,.2f} 元 − 底仓机会 "
             f"{d['gross'] - d['fee'] - d['edge']:,.2f} 元 = **超额 {d['edge']:+,.2f} 元**",
             f"- 打平报价（含 {c['min_edge_bp']:.0f}bp 门槛）：**{d['breakeven_rate']:.3%}**"]
    if not d["go"]:
        lines.append(f"- 不做：{'；'.join(d['reasons'])}")
    else:
        _code = f"R-{d['term']:03d}（深）或 GC{d['term']:03d}（沪）"
        lines.append(f"- 做：{d['amount']:,.0f} 元买 **{d['term']} 天期**（{_code}），"
                     f"实际占款 {days_used} 天（跨周末/长假按实际天数计息）")
    md = "\n".join(lines) + "\n"
    print(md)
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        open(a.md, "w", encoding="utf-8").write(md)
        json.dump(d, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError:
        pass
    return 0 if d["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
