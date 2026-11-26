# -*- coding: utf-8 -*-
"""动态本金（2026-11-26 用户拍板）：每月月报时刻注资 5,000 元。

口径三条（写在代码里，防止以后又被「优化」掉）：

1. **月收益率** `r = 月末净值 / 月初净值 − 1`。月初净值取上月最后一个净值点（此时还没注资），
   注资发生在「本月月报时刻」（本月 1 日 22:00），所以本月内没有资金流入需要剔除——
   注资只抬高新月的起点，不参与上个月收益（用户原话「计算上一个月等价年利率（不包括新加的 5000）」）。
2. **等价年化** `e = (1+r) ** (365/d) − 1`，`d` = 该月**实际运行天数**（月内首个净值点 →
   最后一个净值点），不是固定 30 天：首月只有 6 天就只折 6 天（用户 2026-11-26 补充口径）。
3. **年化（年报定档用）** = 各月 `e` 的**算术平均**（用户原话「取每个月的等价年利率平均值」）。

本金更新（用户原话「重新更新本金（包括挣的钱和消耗的钱以及 token 费用）」）：
    新本金 = 上月末净值（已扣当月 token）+ 注资 5,000
    —— 挣的钱体现在上月末净值里，token 由 `book_token` 从现金扣掉，所以「净值 − 累计投入」是真净利。

token：每月月报把**上一个月**的 token 实花从账户现金扣一次（记 `events.level="token_cost"`），
净值从此是净口径；不扣的话「本金含 token」与「净值不含 token」会打架，收益会多算一笔。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import config
import paper
import score

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _cfg() -> dict:
    return dict((config.PERF or {}).get("dynamic_capital") or {})


def enabled() -> bool:
    return bool(_cfg().get("enabled"))


def injection_cny() -> float:
    return float(_cfg().get("monthly_injection_cny") or 0.0)


def start_month() -> str:
    return str(_cfg().get("start_month") or "9999-12")


def token_from_cash() -> bool:
    return bool(_cfg().get("token_from_cash", True))


def prev_month(month: str) -> str:
    y, m = int(str(month)[:4]), int(str(month)[5:7])
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def last_day(month: str) -> str:
    y, m = int(str(month)[:4]), int(str(month)[5:7])
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    return (nxt - dt.timedelta(days=1)).isoformat()


def next_month(month: str) -> str:
    y, m = int(str(month)[:4]), int(str(month)[5:7])
    if m == 12:
        return f"{y + 1}-01"
    return f"{y}-{m + 1:02d}"


def flows(state: dict) -> list:
    """历年注资现金流：[{date, amount}]，date 为资金到账日（报告日）。

    注资是**现金流**，不是净值点：`inject` 绝不改任何已记录的 nav 行。
    """
    out = []
    for h in ((state.get("meta") or {}).get("capital_history") or []):
        try:
            amt = float(h.get("injection") or 0.0)
        except (TypeError, ValueError):
            continue
        day = str(h.get("date") or "")[:10]
        if amt > 0 and day:
            out.append({"date": day, "amount": amt, "month": str(h.get("month") or "")})
    return sorted(out, key=lambda x: x["date"])


def flows_between(state: dict, start_iso: str, end_iso: str) -> float:
    """落进 (start_iso, end_iso] 的注资合计（窗口收益要从末值里减掉它）。"""
    tot = 0.0
    for f in flows(state):
        if str(start_iso) < f["date"] <= str(end_iso):
            tot += float(f["amount"])
    return round(tot, 2)


def initial_capital(state: dict) -> float:
    """初始本金（注资前），用于「累计投入」基准。"""
    meta = state.get("meta") or {}
    if meta.get("initial_capital"):
        return float(meta["initial_capital"])
    if meta.get("capital"):
        return float(meta["capital"])
    return 0.0


def contributed_capital(state: dict) -> float:
    """累计投入本金 = 初始本金 + Σ注资（不含盈亏）；做收益率/金额分母。"""
    return round(initial_capital(state) + sum(f["amount"] for f in flows(state)), 2)


def withdrawn_total(state: dict) -> float:
    return round(float(((state.get("meta") or {}).get("withdrawn_total")) or 0.0), 2)


def cumulative(state: dict) -> dict:
    """账户累计口径（含已划出的奖励，奖金不是亏损）。"""
    rows = [n for n in (state.get("nav") or []) if n.get("nav") is not None]
    nav = float(rows[-1]["nav"]) if rows else 0.0
    contrib = contributed_capital(state)
    wd = withdrawn_total(state)
    pnl = round(nav + wd - contrib, 2) if contrib > 0 else 0.0
    ret = round(pnl / contrib, 8) if contrib > 0 else 0.0
    return {"nav": nav, "contributed": contrib, "withdrawn": wd,
            "pnl": pnl, "ret": ret, "initial": initial_capital(state)}


def _nav_rows(state: dict, month: str) -> list:
    return [n for n in (state.get("nav") or [])
            if str(n.get("date") or "").startswith(str(month))]


def _last_nav(state: dict, month: str):
    rows = _nav_rows(state, month)
    return rows[-1] if rows else None


def token_spent_in(ledger: str, month: str) -> float:
    """`out/budget.json` 里该账本在 `month` 内结算的 token 实花（元）。"""
    try:
        import billing
        bs = billing.budget_state(ledger, None)
    except Exception:  # noqa: BLE001 — 预算文件缺失不该让月报挂掉
        return 0.0
    tot = 0.0
    for r in (bs.get("runs") or []):
        stamp = str(r.get("ended_at") or r.get("started_at") or r.get("recorded_at") or "")
        if stamp[:7] == str(month):
            try:
                tot += float(r.get("cost_cny") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(tot, 4)


def book_token(state: dict, ledger: str, month: str, as_of: str | None = None) -> dict:
    """把 `month` 的 token 实花从现金扣掉（幂等，按 `meta.token_booked` 记账）。"""
    if not token_from_cash():
        return {"skipped": "token_from_cash=False"}
    meta = state["meta"]
    booked = meta.setdefault("token_booked", {})
    if month in booked:
        return {"already": True, "amount": booked[month], "month": month}
    tok = token_spent_in(ledger, month)
    if tok <= 0:
        # A8：0 元不能写成「已出账」，否则晚到的 token 永远不会补扣。
        meta.setdefault("token_checked", {})[month] = 0.0
        return {"amount": 0.0, "month": month, "note": "本月暂无 token 实花记录（可后续补扣）"}
    state["cash"] = round(float(state["cash"]) - tok, 2)
    row = _last_nav(state, month)
    if row is not None:
        row["nav"] = round(float(row["nav"]) - tok, 2)
        row["cash"] = round(float(row["cash"]) - tok, 2)
    paper.event(state, "token_cost",
                f"token 实花出账（{month}）：{tok:.4f} 元从账户现金扣 → 净值转净口径"
                f"（此后「收益 = 净值 − 累计投入」已含 token 成本）",
                date=as_of or last_day(month), amount=-tok)
    booked[month] = tok
    return {"amount": tok, "month": month}


def record_month(state: dict) -> list:
    """用净值序列重算各月等价年化并写回 `meta.monthly_rates`（覆盖式，幂等）。"""
    series = score.monthly_equivs(state.get("nav") or [], flows=flows(state))
    state["meta"]["monthly_rates"] = {x["month"]: x for x in series}
    return series


def inject(state: dict, month: str, as_of: str | None = None) -> dict:
    """在 `month` 月报时刻注资并更新本金（幂等，按 `meta.capital_history` 记账）。"""
    if not enabled():
        return {"skipped": "dynamic_capital.enabled=False"}
    amt = injection_cny()
    if amt <= 0:
        return {"skipped": "monthly_injection_cny<=0"}
    if str(month) < start_month():
        return {"skipped": f"未到起始月 {start_month()}"}
    meta = state["meta"]
    hist = meta.setdefault("capital_history", [])
    if any(str(h.get("month")) == str(month) for h in hist):
        return {"already": True, "capital": meta.get("capital")}
    old = float(meta.get("capital") or 0.0)
    # A1：注资是现金流，不是净值点。只进现金、只更新「本金」（仓位上限用），
    # 绝不改任何已记录的 nav 行——否则报告月的收益率会把 5,000 算成自己的涨幅。
    if str(month) < start_month():
        return {"skipped": f"未到起始月 {start_month()}"}
    prev = _last_nav(state, prev_month(month))
    base = float(prev["nav"]) if prev else old
    cap_new = round(base + amt, 2)
    if not meta.get("initial_capital"):
        meta["initial_capital"] = old
    state["cash"] = round(float(state["cash"]) + amt, 2)
    meta["capital"] = cap_new
    day = as_of or dt.date.today().isoformat()[:8] + "01"
    paper.event(state, "capital_injection",
                f"动态本金注资 {amt:,.0f} 元（{month}）：本金 {old:,.0f} → {cap_new:,.0f}"
                f"（= 上月末净值 {base:,.2f} + 注资；不含本月盈亏）",
                date=day, amount=amt)
    hist.append({"month": str(month), "date": day, "capital_before": old,
                 "prev_nav": base, "injection": amt, "capital": cap_new})
    return {"ok": True, "month": str(month), "injection": amt,
            "capital_before": old, "capital": cap_new, "prev_nav": base}


def settle(state: dict, ledger: str, month: str, as_of: str | None = None,
           inject_after: bool = True) -> dict:
    """月报收口三件事：上月 token 出账 → 重算各月等价年化 → 本月注资 + 更新本金。

    ``inject_after=False`` 用于年报：先收口（token/年化），年度结算重置本金后**再**单独调
    ``inject()``，否则注资会被年度结算覆盖掉。
    """
    rep_m = str(month)                    # 报告月（刚结束的那个月）
    nxt = next_month(rep_m)               # 注资落到的月份（新月）
    res = {"report_month": rep_m, "inject_month": nxt}
    res["token"] = book_token(state, ledger, rep_m, as_of=as_of)
    res["rates"] = record_month(state)
    res["injection"] = (inject(state, nxt, as_of=as_of)
                        if inject_after else {"skipped": "inject_after=False"})
    return res


def live_rates(state: dict) -> list:
    """按净值现算各月等价年化（**含最后一个月**，不读 meta、不写盘）。"""
    return [x for x in score.monthly_equivs(state.get("nav") or [], flows=flows(state))]


def rates_list(state: dict, live: bool = False) -> list:
    if live:
        return live_rates(state)
    rates = (state.get("meta") or {}).get("monthly_rates") or {}
    return [rates[k] for k in sorted(rates)]


def _avg(rows) -> float | None:
    vals = [float(x["equiv"]) for x in (rows or []) if x.get("equiv") is not None]
    return (sum(vals) / len(vals)) if vals else None


def mean_equiv(state: dict, rows=None) -> float | None:
    """各月等价年化率的算术平均（不传 rows 时按净值现算，含最后一个月）。"""
    return _avg(rows if rows is not None else live_rates(state))


def year_rates(state: dict, year) -> list:
    """某一年内的各月（按净值现算，含该年最后一个月）。"""
    y = str(int(year))
    return [x for x in live_rates(state) if str(x.get("month") or "").startswith(y + "-")]


def missing_year_months(state: dict, year, rows=None) -> list:
    """该年**有净值点但算不出等价年化**的月份（样本 <2 点、净值差 ≤ −100% 等）。"""
    y = str(int(year))
    rows = rows if rows is not None else year_rates(state, y)
    have = {str(x.get("month")) for x in rows if x.get("equiv") is not None}
    navs = [str(n.get("date"))[:7] for n in (state.get("nav") or []) if n.get("date")]
    return [m for m in sorted({m for m in navs if m.startswith(y + "-")}) if m not in have]


def year_mean_equiv(state: dict, year) -> dict:
    """年报定档口径：**本年**（不是全部历史）各月等价年化的算术平均。

    年报时刻先用净值现算一次最后一个月的等价年化（等价于「先记录上月」），
    再取本年所有已运行月份的平均——跨年月份（如上一年 12 月）不计入。
    """
    rows = year_rates(state, year)
    return {
        "year": int(year),
        "value": _avg(rows),
        "months": len([x for x in rows if x.get("equiv") is not None]),
        "rows": rows,
        "missing": missing_year_months(state, year, rows),
    }


def grade_year(state: dict, as_of: str | None = None) -> int | None:
    """年报评级的年份 = 净值数据所在的最后一个自然年。

    年末（12 月）跑年报取该年；1 月 1 日补跑年报时最后一个月仍是上一年 12 月，
    同样取上一年；年内中途跑（测试/重渲）取本年当前数据所属年份。
    """
    dates = sorted(str(n.get("date"))[:10] for n in (state.get("nav") or []) if n.get("date"))
    return int(dates[-1][:4]) if dates else None


def annual_net_pnl(state: dict, rows=None) -> float | None:
    """各月盈亏之和（净值差，已含 token 扣减）；无月度记录时返回 None。"""
    rows = rows if rows is not None else live_rates(state)
    vals = [float(x.get("pnl")) for x in rows if x.get("pnl") is not None]
    return round(sum(vals), 2) if vals else None


def live_capital(ledger: str = "50k", default: float = 50000.0) -> float:
    """账本当前「累计投入」，仓位上限/下单金额该用的本金（A6）。

    动态本金下配置或命令行里的 50,000 会过期（每月 +5,000 且盈亏在滚），
    所以 plan/run 在不显式传 --capital 时应读账本。
    """
    try:
        path = (((config.PAPER.get("ledgers") or {}).get(ledger) or {}).get("state_file"))
        if path and os.path.isfile(path):
            st = paper.load(path)
            val = float(contributed_capital(st)) or float((st.get("meta") or {}).get("capital") or 0.0)
            if val > 0:
                return round(val, 2)
    except Exception:  # noqa: BLE001 — 读不到就退回默认值
        pass
    return float(default)


def summary(state: dict, month: str | None = None) -> dict:
    meta = state.get("meta") or {}
    hist = meta.get("capital_history") or []
    rates = live_rates(state)                    # 现算：含最后一个月，不依赖 meta 是否已收口
    mth = str(month) if month else None
    y = int(mth[:4]) if mth else None
    y_rows = year_rates(state, y) if y else []
    return {
        "enabled": enabled(),
        "capital": float(meta.get("capital") or 0.0),
        "injection_cny": injection_cny(),
        "injected_total": round(sum(float(h.get("injection") or 0.0) for h in hist), 2),
        "injections": len(hist),
        "months": len([x for x in rates if x.get("equiv") is not None]),
        "mean_equiv": _avg(rates),
        "year": y,
        "year_mean_equiv": _avg(y_rows) if y else None,
        "year_months": len([x for x in y_rows if x.get("equiv") is not None]),
        "year_missing_months": missing_year_months(state, y, y_rows) if y else [],
        "annual_net_pnl": annual_net_pnl(state, rates),
        "rates": rates,
        "start_month": start_month(),
        "report_month": mth,
        "this_month_injected": bool(mth) and any(str(h.get("month")) == mth for h in hist),
    }
