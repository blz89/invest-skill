# -*- coding: utf-8 -*-
"""收益拆解：把账户累计收益拆成 权益层 / 场外 R2 / 现金层 三块（周报、月报共用）。

用户要求：周报、月报都要体现三层收益结构，不许只报一个总收益。
口径：
- 权益层：成本 = Σ 持仓股数 × `cost_basis`（cost_basis 已含买入佣金）；
  市值 = 账本 `nav` 最后一点的 `market_value`（与净值同源），缺失时才按 `last_close` 现算。
- 场外 R2：成本 = 投入金额合计（`otc_book.cost_total`）；市值 = `otc_book.market_value`
  （金额法：投入金额 × 当日累计净值 / 买入日累计净值）。
- 现金层：现金 −（本金 − 权益成本 − 场外成本）；差额即现金层净收益
  （朝朝宝/逆回购利息 − 现金层手续费）。若发生卖出平仓，已实现盈亏也会落在现金层，
  这属于口径约定（报告里会注明）。
- 在途逆回购利息（`repo_open` 的 interest − fee）单列提示，**不计入**（尚未落账）。
- 合计应等于「净值 − 本金」；误差 > 1 元视为数据不自洽，报告里给 warn 不静默。

本模块只读，不写账本、不取数、不下单。
"""

import config  # noqa: F401  (保持与其它脚本一致的导入面，便于替换数据源)
import otc_book


def _f(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(x, nd=2):
    if x is None:
        return "—"
    try:
        return f"{float(x) * 100:+,.{nd}f}%"
    except (TypeError, ValueError):
        return "—"


def compute(state: dict) -> dict:
    """返回三层收益拆解（全部只读，异常字段按 0 / 空处理）。"""
    state = state or {}
    if not state:
        return {}
    meta = state.get("meta") or {}
    capital = float(meta.get("capital") or 0.0)
    cash = float(state.get("cash") or 0.0)
    navs = state.get("nav") or []
    last_nav = navs[-1] if navs else {}

    holdings = state.get("holdings") or {}
    eq_cost = 0.0
    for h in holdings.values():
        try:
            eq_cost += float(h.get("shares") or 0) * float(h.get("cost_basis") or 0)
        except (TypeError, ValueError):
            continue
    eq_val = None
    if last_nav.get("market_value") is not None:
        try:
            eq_val = float(last_nav.get("market_value") or 0.0)
        except (TypeError, ValueError):
            eq_val = None
    if eq_val is None:
        eq_val = 0.0
        for h in holdings.values():
            try:
                eq_val += float(h.get("shares") or 0) * float(h.get("last_close") or 0)
            except (TypeError, ValueError):
                continue

    try:
        otc_cost = float(otc_book.cost_total(state) or 0.0)
        otc_val = float(otc_book.market_value(state) or 0.0)
        otc_err = ""
    except Exception as exc:  # noqa: BLE001 - 报表不该因场外腿异常而挂
        otc_cost, otc_val, otc_err = 0.0, 0.0, str(exc)

    nav_total = last_nav.get("nav")
    try:
        nav_total = float(nav_total) if nav_total is not None else cash + eq_val + otc_val
    except (TypeError, ValueError):
        nav_total = cash + eq_val + otc_val

    op = state.get("repo_open") or {}
    try:
        in_transit = float(op.get("interest") or 0.0) - float(op.get("fee") or 0.0)
    except (TypeError, ValueError):
        in_transit = 0.0

    cash_baseline = capital - eq_cost - otc_cost
    cash_pnl = cash - cash_baseline
    eq_pnl = eq_val - eq_cost
    otc_pnl = otc_val - otc_cost
    total_pnl = eq_pnl + otc_pnl + cash_pnl
    nav_pnl = nav_total - capital
    diff = total_pnl - nav_pnl
    return {
        "capital": round(capital, 2), "nav": round(nav_total, 2),
        "nav_pnl": round(nav_pnl, 2), "total_pnl": round(total_pnl, 2),
        "diff": round(diff, 2), "ok": abs(diff) <= 1.0,
        "equity": {"cost": round(eq_cost, 2), "value": round(eq_val, 2),
                   "pnl": round(eq_pnl, 2),
                   "share": round(eq_pnl / total_pnl, 4) if total_pnl else None},
        "otc": {"cost": round(otc_cost, 2), "value": round(otc_val, 2),
                "pnl": round(otc_pnl, 2), "error": otc_err,
                "share": round(otc_pnl / total_pnl, 4) if total_pnl else None},
        "cash": {"value": round(cash, 2), "baseline": round(cash_baseline, 2),
                 "pnl": round(cash_pnl, 2), "in_transit": round(in_transit, 2),
                 "share": round(cash_pnl / total_pnl, 4) if total_pnl else None},
        "date": last_nav.get("date") or "",
    }


def _rows(bd: dict) -> list:
    eq = bd.get("equity") or {}
    otc = bd.get("otc") or {}
    cash = bd.get("cash") or {}
    return [
        ("权益层（A 股 / 场内 ETF）", eq.get("cost"), eq.get("value"), eq.get("pnl"), eq.get("share")),
        ("场外 R2（债券基金）", otc.get("cost"), otc.get("value"), otc.get("pnl"), otc.get("share")),
        ("现金层（朝朝宝 + 逆回购，残差）", cash.get("baseline"), cash.get("value"),
         cash.get("pnl"), cash.get("share")),
    ]


def note_text(bd: dict) -> str:
    cash = bd.get("cash") or {}
    otc = bd.get("otc") or {}
    s = ("口径：权益层成本含买入佣金；场外 R2 用金额法（累计净值）；"
         "现金层 = 现金 −（本金 − 权益成本 − 场外成本），含朝朝宝/逆回购利息与现金层手续费；"
         "若本期有卖出，已实现盈亏也体现在现金层。")
    s += (" 现金层年化有两个口径，别混：**实得口径** = events 里 cash_yield 实际落账合计 "
          "÷ (现金日/365)，含在途未计息与手续费侵蚀；**计划口径** = config.cash_rates() 假设利率×天数"
          "（paper.cash_yield_plan），只用于事前列计划，事后汇报一律用实得口径。")
    if cash.get("in_transit"):
        s += f" 在途逆回购利息 {_f(cash.get('in_transit'))} 元未落账（到期后计入现金层）。"
    if otc.get("error"):
        s += f" ⚠ 场外腿读取异常：{otc['error']}（该层按 0 计）。"
    if not bd.get("ok"):
        s += (f" ⚠ 三层合计与「净值−本金」差 {_f(bd.get('diff'))} 元"
              "（>1 元，数据可能不自洽，请核对账本）。")
    return s


def table_md(bd: dict) -> str:
    L = ["| 资产层 | 投入成本 | 当前价值 | 盈亏 | 占总收益 |", "|---|---:|---:|---:|---:|"]
    for lab, cost, val, pnl, share in _rows(bd):
        L.append(f"| {lab} | {_f(cost)} | {_f(val)} | **{_f(pnl)}** | {_pct(share)} |")
    L.append(f"| **合计** | {_f(bd.get('capital'))} | {_f(bd.get('nav'))} | "
             f"**{_f(bd.get('total_pnl'))}** | 100% |")
    return "\n".join(L)


def render_md(bd: dict, heading: str = "## 收益拆解（权益 / 场外 R2 / 现金三层）") -> str:
    if not bd:
        return ""
    return (heading + "\n\n" + table_md(bd) + "\n\n> " + note_text(bd) + "\n\n")


def table_html(bd: dict) -> str:
    rows = []
    for lab, cost, val, pnl, share in _rows(bd):
        rows.append(
            "<tr><th>" + lab + "</th>"
            f"<td class='num'>{_f(cost)}</td><td class='num'>{_f(val)}</td>"
            f"<td class='num'>{_f(pnl)}</td><td class='num'>{_pct(share)}</td></tr>")
    rows.append(
        "<tr><th><b>合计</b></th>"
        f"<td class='num'><b>{_f(bd.get('capital'))}</b></td>"
        f"<td class='num'><b>{_f(bd.get('nav'))}</b></td>"
        f"<td class='num'><b>{_f(bd.get('total_pnl'))}</b></td>"
        "<td class='num'>100%</td></tr>")
    head = ("<table><tr><th>资产层</th><th class='num'>投入成本</th>"
            "<th class='num'>当前价值</th><th class='num'>盈亏</th>"
            "<th class='num'>占总收益</th></tr>")
    return head + "".join(rows) + "</table>"


def render_html(bd: dict, heading: str = "收益拆解（权益 / 场外 R2 / 现金三层）",
                level: int = 2) -> str:
    if not bd:
        return ""
    return (f"<h{level}>{heading}</h{level}><div class='card'>" + table_html(bd)
            + f"<div class='note'>{note_text(bd)}</div></div>")
