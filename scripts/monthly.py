# -*- coding: utf-8 -*-
"""月度复盘/年报：汇总账本一个月的净值/交易/费用/归因，产出 Markdown + HTML。

平时（月报）只读账本、不取数、不下单；`--annual` 是年度结算日，会写账本
（备份 → 奖励从现金划出 → 本金重置 → `meta.annual_settlements`，同年幂等）。
月报里的「权益层盈亏」= 月内净值变化 − 月内现金层利息，
和 review.py 的口径一致：不许把现金利息冒充策略收益。

用法
    python monthly.py                      # 默认当月（按今天）
    python monthly.py --month 2026-10
    python monthly.py --month 2026-10 --ledger 50k
产出
    out/paper/monthly_2026-10.md            # 给 agent/人读的汇总
    out/paper/monthly_report_2026-10.html   # 给用户看的 HTML 报告
"""
import argparse
import calendar
import datetime
import io
import os
import re
import sys

import breakdown
import capital_dyn
import config
import paper
import report_html
import score_panel

CFG = config.PAPER
LEDGERS = CFG.get("ledgers") or {}
POUT = CFG.get("package_dir") or os.path.join(config.OUT_DIR, "paper")
STOP_WORDS = ("止损", "熔断")
# T10 复盘员的产物。月报的第一、二节直接摘它：月报不能只做统计。
RETRO_PATH = os.path.join(config.OUT_DIR, "team", "retro.md")
_HEAD_RE = re.compile(r"^(#{2,6})\s+(.*)$")


def _f(x, nd=2, dash="—"):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return dash


def _pct(x, nd=2, arrow=False):
    try:
        v = float(x) * 100.0
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.{nd}f}%" if arrow else f"{v:.{nd}f}%"


def _year_token_spent(ledger: str, year: int) -> float:
    """当年 token 实花：budget.json 的 runs 按结束/开始日期落在该自然年的 cost 求和。"""
    try:
        import billing as _B
        _bs = _B.budget_state(ledger)
        total = 0.0
        for r in (_bs.get("runs") or []):
            when = str(r.get("ended_at") or r.get("started_at") or "")
            if when[:4] == str(int(year)):
                total += float(r.get("cost_cny") or 0.0)
        return round(total, 6)
    except Exception:  # noqa: BLE001 — 缺预算文件时按 0 计，并在 net 块标 source
        return 0.0


def _grab_section(txt, keys):
    """取「标题含 keys 任一词」的 markdown 小节（到下一个同级/更高级标题为止）。

    找不到返回 ""。只认 2~6 级标题，避免把正文里的粗体当成小节。
    """
    lines = (txt or "").splitlines()
    start = lvl = None
    for i, ln in enumerate(lines):
        m = _HEAD_RE.match(ln)
        if not m:
            continue
        if start is None:
            if any(k in m.group(2) for k in keys):
                start, lvl = i, len(m.group(1))
            continue
        if len(m.group(1)) <= lvl:
            # 去掉小节标题行本身（2026-11-01 修）：之前把标题一起返回，月报
            # 「一、上月策略复盘与改进」下会再出现「## 六、上月策略复盘」，编号断裂。
            return "\n".join(lines[start + 1:i]).strip()
    if start is None:
        return ""
    return "\n".join(lines[start + 1:]).strip()


def read_retro(path=None):
    """读 out/team/retro.md，摘出「策略复盘」「技能优化」两节（月报第一、二节）。"""
    p = path or RETRO_PATH
    out = {"exists": False, "strategy": "", "optimization": "", "path": p}
    try:
        txt = io.open(p, encoding="utf-8").read()
    except OSError:
        return out
    out["exists"] = True
    out["strategy"] = _grab_section(txt, ("策略复盘",))
    out["optimization"] = _grab_section(txt, ("技能优化",))
    return out


VIABILITY_PLACEHOLDER = ("> ⚠ 未找到 `out/team/viability_{key}.md`（月报会话必须在第 8 步前写："
                         "五问表 + 结论三选一）。缺这一节 = 月报没有回答「策略还可行吗」，不能算完成。")


def read_viability(key):
    """读月度/年度「策略可行性判定（五问）」，key = YYYY-MM 或 YYYY。"""
    p = os.path.join(config.OUT_DIR, "team", f"viability_{key}.md")
    try:
        txt = io.open(p, encoding="utf-8").read().strip()
    except OSError:
        return None
    return txt or None


def read_annual_plan():
    """年报写给月报的指挥文件；缺失返回 None。"""
    p = os.path.join(config.OUT_DIR, "team", "annual_plan.md")
    try:
        txt = io.open(p, encoding="utf-8").read().strip()
    except OSError:
        return None
    return txt or None


def score_health_md(year=None):
    """年报「选股评分体检」正文；面板不足时返回「维持现状」的说明。"""
    try:
        return score_panel.health(year)["md"]
    except Exception as exc:  # noqa: BLE001 — 体检失败不能拖垮年报
        return f"## 选股评分体检\n\n- 生成失败：{exc}（不影响年报其余部分）\n"


def opt_stats(text):
    """技能优化清单完成度：{"total": 条目数, "done": 已勾选数}。"""
    items = re.findall(r"^\s*[-*]\s*\[( |x|X)\]\s*", text or "", re.M)
    return {"total": len(items), "done": sum(1 for x in items if x.strip().lower() == "x")}


def ledger_diagnostics(state, month, m):
    """自动诊断信号：代码只报事实（数字/日期/对比），判断留给复盘员。

    为什么要它：月报不能只堆统计，但让 LLM 去翻原始账本容易漏；这里把可机器判断的
    异常先列出来（结算点太少、无效挂单偏多、场外腿空着、现金层零利息），复盘直接引用。
    返回 [(level, text)]，level ∈ {"info","warn"}。
    """
    m = m or {}
    sig = []
    navs = [n for n in (state.get("nav") or [])
            if str(n.get("date") or "").startswith(month)]
    if len(navs) < 4:
        sig.append(("warn", f"本月只有 {len(navs)} 个净值结算点（正常应 ≥4，查巡检/决策是否漏跑）"))
    else:
        sig.append(("info", f"本月净值结算点 {len(navs)} 个"))
    orders = [o for o in (state.get("orders") or [])
              if str(o.get("effective_from") or "").startswith(month)]
    if orders:
        ct = {"filled": 0, "expired": 0, "cancelled": 0, "pending": 0}
        for o in orders:
            k = str(o.get("status") or "pending")
            ct[k] = ct.get(k, 0) + 1
        dead = ct["expired"] + ct["cancelled"]
        sig.append(("info", f"月内生效挂单 {len(orders)} 张：成交 {ct['filled']}｜失效 "
                            f"{ct['expired']}｜取消 {ct['cancelled']}｜仍在挂 {ct['pending']}"))
        if dead and dead >= max(2, int(0.4 * (ct["filled"] + dead))):
            sig.append(("warn", f"无效挂单占比过半（{dead}/{ct['filled'] + dead}），"
                                "查限价/失效期/一手金额是否设得不合理"))
    else:
        sig.append(("info", "本月没有新挂单（未调仓或计划为空）"))
    fees_pct = float(m.get("fees_pct") or 0.0)
    sig.append(("info", f"月末仓位 {_pct(m.get('weight_end'))}｜费用 {_f(m.get('fees'))} 元"
                        f"（{_pct(fees_pct)} 本金）｜止损/熔断 {int(m.get('stop_outs') or 0)} 次"))
    if fees_pct > 0.005:
        sig.append(("warn", "费用/本金超过 0.5%，查换手与单笔佣金（万 1 最低 5 元/笔）"))
    # 场外腿（R2 影子账）
    otc_n = len(state.get("otc_holdings") or {})
    otc_pending = len([o for o in (state.get("otc_orders") or [])
                       if str(o.get("status") or "") == "pending"])
    try:
        import otc_book
        otc_mv = float(otc_book.market_value(state) or 0.0)
    except Exception:  # noqa: BLE001
        otc_mv = 0.0
    sig.append(("info", f"场外腿（R2 影子账）：持仓 {otc_n} 只｜市值 {_f(otc_mv)} 元｜在挂 {otc_pending} 张"))
    if otc_n == 0 and otc_pending == 0:
        sig.append(("warn", "场外 R2 腿为空（计划有承接层却没落地，查补建/在挂/净值可取性）"))
    # 现金层
    cy = float(m.get("cash_yield") or 0.0)
    if cy <= 0:
        sig.append(("warn", "本月现金层利息为 0（朝朝宝/逆回购没操作或没覆盖）"))
    else:
        _ca = m.get("cash_yield_actual_annual")
        _cal = (f"｜实得年化 {float(_ca) * 100:.2f}%（口径：events 实际落账 ÷ 现金日，"
                f"非计划利率×天数）" if _ca is not None else "")
        sig.append(("info", f"现金层利息 {_f(cy)} 元｜逆回购净额 {_f(m.get('repo_net'))} 元"
                            f"（覆盖 {int(m.get('repo_covered_days') or 0)} 天）{_cal}"))
    return sig


def _all_trades(state, month):
    """本月成交：场内 trades + 场外影子账 otc_trades（字段对齐后按日期排序）。

    2026-10-30 修（A 类 bug）：3d76657 把 gather 改成调 _all_trades 合并场外成交，但函数体
    没落地 → monthly.py 从 01:04 起一跑就 NameError（月报直接挂）。这里补回；场外行 shares
    留空（基金按金额）、price=成交净值、原因标「场外申购/赎回（影子账）」。
    """
    rows = []
    for t in (state.get("trades") or []):
        if str(t.get("date") or "").startswith(month):
            rows.append(dict(t))
    for t in (state.get("otc_trades") or []):
        if not str(t.get("date") or "").startswith(month):
            continue
        act = "buy" if t.get("action") == "buy" else "sell"
        rows.append({"date": t.get("date"), "code": t.get("code"), "name": t.get("name"),
                     "action": act, "shares": None, "price": t.get("nav"),
                     "amount": t.get("amount"), "fee": t.get("fee"),
                     "realized_pnl": t.get("realized_pnl"),
                     "why": "场外申购（影子账 T+1）" if act == "buy" else "场外赎回（影子账）"})
    return sorted(rows, key=lambda x: str(x.get("date") or ""))


def month_metrics(state, month, ledger=None, period=None):
    """一个区间内的净值和交易指标（没有结算点时用本金兜底）。

    ``period`` 缺省用 ``month``；传 ``"2026"`` 即为自然年统计（年报复用同一口径）。
    另给「净利润」口径：净利润 = 账户累计收益 − token 实花，
    token 实花取 out/budget.json 该账本累计 spent（不是年度计提）。
    """
    p_name = str(period or month)
    # A2：累计收益率/金额的分母是「累计投入」（初始本金 + Σ注资），
    # 当前本金 meta.capital 每月会被重写成「上月末净值 + 注资」，当历史分母会失真。
    cap0 = capital_dyn.contributed_capital(state) or float(state["meta"].get("capital") or 0.0)
    navs = sorted((n for n in (state.get("nav") or []) if n.get("date")), key=lambda n: n["date"])
    before = [n for n in navs if str(n["date"]) < f"{p_name}-01"]
    inside = [n for n in navs if str(n["date"]).startswith(p_name)]
    start = before[-1] if before else None
    # 账本首个净值点就落在本月（首月）时 before 为空：改用本月首点作起点，
    # 否则 bench 起点缺失 → 月报「同期沪深300/超额」显示「—」（2026-11-01 修）。
    base = start or (inside[0] if inside else None)
    nav_start = float(base["nav"]) if base else cap0
    bench_start = base.get("bench_nav") if base else None
    nav_end = float(inside[-1]["nav"]) if inside else nav_start
    # 现金「cash-days」：日加权现金余额，用来把 events 里**实际落账**的 cash_yield 折成实得年化。
    # 2026-11-25 A 类修复 #8：月报里同时出现过「计划口径 0.885%」（假设利率×天数，含逆回购
    # 0.26%/年手续费侵蚀）与「实得 0.829%」（events 实际落账），两个数没有名字，读者会当成误差。
    # 现在实得这个数由代码现算并显式标口径，不再和计划口径混在一句话里。
    _cash_pts = [(str(n.get("date")), float(n.get("cash") or 0.0))
                 for n in (before + inside) if n.get("cash") is not None]
    cash_days = 0.0
    for (_d0, _c0), (_d1, _c1) in zip(_cash_pts, _cash_pts[1:]):
        try:
            _gap = (datetime.date.fromisoformat(_d1) - datetime.date.fromisoformat(_d0)).days
        except ValueError:
            _gap = 0
        cash_days += float(_c0) * max(0, _gap)
    bench_end = inside[-1].get("bench_nav") if inside else bench_start
    bench_month = None
    if bench_start and bench_end:
        try:
            bench_month = float(bench_end) / float(bench_start) - 1.0
        except ZeroDivisionError:
            bench_month = None
    # A1：注资是现金流，不是收益 —— 把本期到账的注资从区间两端剔除。
    try:
        _flow_m = capital_dyn.flows_between(
            state, str(base["date"]) if base else "",
            str(inside[-1]["date"]) if inside else "")
    except Exception:  # noqa: BLE001
        _flow_m = 0.0
    ret_month = ((nav_end - _flow_m) / nav_start - 1.0) if nav_start else None
    excess = (ret_month - bench_month) if (ret_month is not None and bench_month is not None) else None
    # 月内最大回撤（以月内起点为基准）
    peak, mdd = nav_start, 0.0
    for n in inside:
        peak = max(peak, float(n["nav"]))
        if peak:
            mdd = min(mdd, float(n["nav"]) / peak - 1.0)
    # 交易（场内 + 场外影子账，口径统一后费用/笔数才不会漏场外）
    all_tr = _all_trades(state, p_name)
    buys = [t for t in all_tr if t.get("action") == "buy"]
    sells = [t for t in all_tr if t.get("action") == "sell"]
    trades = all_tr
    fees = round(sum(float(t.get("fee") or 0.0) for t in all_tr), 2)
    realized = round(sum(float(t.get("realized_pnl") or 0.0) for t in sells), 2)
    # paper 写的是 reason，旧版这里读 why → 止损次数恒 0（2026-10-30 审计 #28）
    stops = [t for t in sells
             if any(w in str(t.get("reason") or t.get("why") or "") for w in STOP_WORDS)]
    # 现金层利息
    cash_yield = round(sum(float(e.get("amount") or 0.0) for e in (state.get("events") or [])
                           if e.get("level") == "cash_yield"
                           and str(e.get("date") or "").startswith(p_name)), 2)
    # 实得年化 = 本月落账利息 ÷ (cash-days/365)。口径写清楚才不会被拿去和「计划口径」比：
    #   实得口径 = events 里 level=="cash_yield" 实际落账（含在途未计息、手续费侵蚀）
    #   计划口径 = config.cash_rates() 假设利率 × 天数（paper.cash_yield_plan 算，仅供计划）
    cash_yield_actual_annual = (round(cash_yield / (cash_days / 365.0), 6)
                                if cash_days > 0 else None)
    # 逆回购：本月落地的 lot（含在途 lot 的应收利息）
    repo_rows = [r for r in (state.get("repo_history") or [])
                 if r.get("kind") == "repo" and str(r.get("date") or "").startswith(p_name)]
    op = state.get("repo_open") or {}
    if op and str(op.get("buy") or "").startswith(p_name):
        repo_rows = repo_rows + [dict(op, kind="repo")]
    mf_rows = [r for r in (state.get("repo_history") or [])
               if r.get("kind") == "mf" and str(r.get("date") or "").startswith(p_name)]
    repo_ops = len(repo_rows)
    repo_covered = sum(int(r.get("days") or 0) for r in repo_rows)
    repo_interest = round(sum(float(r.get("interest") or 0.0) for r in repo_rows), 2)
    repo_fees = round(sum(float(r.get("fee") or 0.0) for r in repo_rows), 2)
    repo_mf = round(sum(float(r.get("interest") or 0.0) for r in mf_rows), 2)
    repo_net = round(repo_interest - repo_fees + repo_mf, 2)
    repo_principal_days = sum(float(r.get("amount") or 0.0) * int(r.get("days") or 0)
                              for r in repo_rows)
    repo_eff = (round((repo_interest - repo_fees) / (repo_principal_days / 365.0), 6)
                if repo_principal_days else 0.0)
    p = paper.portfolio(state, {})
    pnl_month = round(nav_end - nav_start - _flow_m, 2)
    equity_pnl = round(pnl_month - cash_yield, 2)
    inv = paper.investable_capital(state)
    # 按标的的已实现盈亏
    by = {}
    for t in sells:
        code = t.get("code")
        item = by.setdefault(code, {"code": code, "name": t.get("name"), "realized_pnl": 0.0, "trades": 0})
        item["realized_pnl"] = round(item["realized_pnl"] + float(t.get("realized_pnl") or 0.0), 2)
        item["trades"] += 1
    by_symbol = sorted(by.values(), key=lambda x: x["realized_pnl"], reverse=True)
    # 风险等级分布（月末口径）：持仓等级必须 ⊆ R2/R3
    try:
        import risk
        _items = []
        for code, h in (state.get("holdings") or {}).items():
            _px = float(h.get("last_close") or h.get("cost") or 0.0)
            _amt = float(h.get("shares") or 0) * _px
            _w = (_amt / p["nav"]) if p.get("nav") else 0.0
            _items.append({"code": code, "name": h.get("name"), "level": h.get("level") or "R5",
                           "weight": round(_w, 6)})
        _rv = risk.portfolio_review(_items) if _items else {}
        risk_block = {"level": _rv.get("level"), "level_label": _rv.get("level_label"),
                      "weights": _rv.get("weights") or {}, "r2_share": _rv.get("r2_share"),
                      "ok": _rv.get("ok", True), "violations": _rv.get("violations") or [],
                      "count": len(_items)}
    except Exception as exc:  # noqa: BLE001
        risk_block = {"error": f"风险评审失败：{exc}"}
    # 浮动盈亏 = 总市值 − 总成本。场外影子持仓的市值含在场外腿里，成本必须一起减，
    # 否则「场外本金整额被当成浮盈」（2026-10-30 审计 #7：5000 本金被算成 5000 浮盈，真值 250）。
    try:
        import otc_book
        _otc_cost = float(otc_book.cost_total(state) or 0.0)
    except Exception:  # noqa: BLE001
        _otc_cost = 0.0
    _stock_cost = sum(float(h.get("cost_basis") or 0.0) * float(h.get("shares") or 0)
                      for h in state["holdings"].values())
    # 净利润：账户累计收益 − token 实花（budget.json 累计 spent）。
    net_block = {"token_spent": None, "net_profit": None, "net_rate": None,
                 "grade": None, "reward_pending": None, "reward_note": "",
                 "gross_profit": None, "source": "out/budget.json 累计 spent"}
    try:
        import billing as _B
        import score as _S
        _bs = _B.budget_state(ledger or "50k", cap0)
        _spent = float(_bs.get("spent_cny") or 0.0)
        _ret = (nav_end / cap0 - 1.0) if cap0 else None
        if _ret is not None and cap0:
            _tok_in_nav = bool(capital_dyn.enabled() and capital_dyn.token_from_cash())
            _spent_eff = 0.0 if _tok_in_nav else _spent
            _ev = _S.evaluate(_ret, _spent_eff, cap0)
            net_block.update({
                "token_spent": round(_spent, 2),
                "token_in_nav": bool(capital_dyn.enabled() and capital_dyn.token_from_cash()),
                "gross_profit": round(_ev["gross_profit"], 2),
                "net_profit": round(_ev["net_profit"], 2),
                "net_rate": _ev["net_rate"], "grade": _ev["grade"],
                "reward_cny": round(_ev["reward_cny"], 2),
                "reward_pending": _ev["reward_pending"],
                "reward_note": _ev["reward_note"],
            })
    except Exception as exc:  # noqa: BLE001 — 月报不能因为缺预算文件而挂
        net_block["error"] = f"净利润口径计算失败：{exc}"
    return {
        "nav_start": round(nav_start, 2), "nav_end": round(nav_end, 2),
        "pnl_month": pnl_month, "ret_month": ret_month, "bench_month": bench_month, "excess": excess,
        "no_data": not bool(inside),
        "total_return": ((nav_end + paper.withdrawn_total(state)) / cap0 - 1.0
                         if cap0 else None),
        "total_pnl": round(nav_end + paper.withdrawn_total(state) - cap0, 2),
        "contributed": cap0, "flow_month": round(_flow_m, 2),
        "mdd": round(mdd, 6), "trades": len(trades), "buys": len(buys), "sells": len(sells),
        "fees": fees, "fees_pct": (fees / cap0) if cap0 else None,
        "realized": realized, "unrealized": round(p.get("market_value", 0.0)
                                                   - _stock_cost - _otc_cost, 2),
        "cash_yield": cash_yield, "equity_pnl": equity_pnl,
        "cash_days": round(cash_days, 2), "cash_yield_actual_annual": cash_yield_actual_annual,
        "repo_ops": repo_ops, "repo_covered_days": repo_covered,
        "repo_interest": repo_interest, "repo_fees": repo_fees,
        "repo_net": repo_net, "repo_effective": repo_eff,
        "risk": risk_block,
        "weight_end": (p.get("market_value", 0.0) / p["nav"]) if p.get("nav") else 0.0,
        "stop_outs": len(stops), "investable": inv.get("investable"),
        "capital": cap0, "gain": inv.get("gain"),
        "net": net_block,
        "dynamic": capital_dyn.summary(state, month),
    }


def year_metrics(state, year, ledger=None):
    """自然年指标：与月度同口径（period=str(year)），另给逐月分解。"""
    y = int(year)
    m = month_metrics(state, f"{y}-12", ledger=ledger, period=str(y))
    cap0 = capital_dyn.contributed_capital(state) or float(state["meta"].get("capital") or 0.0)
    navs = sorted((n for n in (state.get("nav") or []) if n.get("date")), key=lambda n: n["date"])
    first = str(navs[0]["date"])[:7] if navs else ""
    months = []
    for mon in range(1, 13):
        key = f"{y}-{mon:02d}"
        if first and key < first:      # 建仓之前的月份：没有数据，不假装 0.00%
            months.append({"month": key, "nav_end": None, "ret_month": None, "total_return": None})
            continue
        mm = month_metrics(state, key, ledger=ledger)
        months.append({
            "month": key,
            "nav_end": mm["nav_end"],
            "ret_month": mm["ret_month"],
            "total_return": (mm["nav_end"] / cap0 - 1.0) if cap0 else None,
        })
    m["months"] = months
    m["year"] = y
    m["ret_year"] = m["ret_month"]
    m["pnl_year"] = m["pnl_month"]
    # 年度口径：净利润 = 当年毛利 − 当年 token 实花；档位按等价年化净收益率
    # （不足一年也公平），奖励仍按当年净利润。与月报的「累计口径」分开。
    try:
        import score as _S
        spent_y = _year_token_spent(ledger or "50k", y)
        dates = [str(n["date"])[:10] for n in navs if str(n["date"])[:4] == str(y)]
        days = ((datetime.date.fromisoformat(max(dates))
                 - datetime.date.fromisoformat(min(dates))).days + 1) if dates else None
        cap_start = float(m.get("nav_start") or cap0 or 0.0)
        # A4：token_from_cash 下 nav 已是净口径（年度收益已扣 token），再减一次会把
        # 正收益打成负的（实测 +87.5 元 → −50 元、C 档 → D 档）。
        _tok_in_nav_y = bool(capital_dyn.enabled() and capital_dyn.token_from_cash())
        _tok_arg = 0.0 if _tok_in_nav_y else spent_y
        net_y = round(float(m.get("pnl_year") or 0.0) - _tok_arg, 2)
        ev = _S.evaluate(float(m.get("ret_year") or 0.0), _tok_arg, cap_start or 1.0,
                         mdd=m.get("mdd"), days=days, net_profit=net_y,
                         as_of=f"{y + 1}-01-01")
        m["net"] = {
            "token_spent": round(spent_y, 2),
            "gross_profit": round(float(m.get("pnl_year") or 0.0), 2),
            "net_profit": net_y,
            "net_rate": ev["net_rate"],
            "net_rate_ann": ev["net_rate_ann"],
            "annualized": ev["annualized"],
            "days": days,
            "grade": ev["grade"],
            "reward_cny": round(ev["reward_cny"], 2),
            "reward_pending": ev["reward_pending"],
            "reward_note": ev["reward_note"],
            "source": "当年毛利 − 当年 token 实花（budget runs 按年）",
        }
    except Exception as exc:  # noqa: BLE001 — 年报不能因为预算文件缺失而挂
        m["net"] = {"error": f"年度净利润口径计算失败：{exc}", "source": "当年口径"}
    m["months"] = months
    m["year"] = y
    return m


def gather_annual(year, only=None):
    """年报数据：年度指标 + 逐月分解 + 全年交易。"""
    ledgers, order = {}, (only or list(LEDGERS.keys()))
    for name in order:
        cfg = LEDGERS.get(name) or {}
        path = cfg.get("state_file")
        if not path or not os.path.exists(path):
            continue
        state = paper.load(path)
        m = year_metrics(state, year, ledger=name)
        trades = _all_trades(state, str(year))
        by = {}
        for t in trades:
            if t.get("action") != "sell" or not t.get("code"):
                continue
            item = by.setdefault(t["code"], {"code": t["code"], "name": t.get("name"),
                                             "realized_pnl": 0.0, "trades": 0})
            item["realized_pnl"] = round(item["realized_pnl"] + float(t.get("realized_pnl") or 0.0), 2)
            item["trades"] += 1
        ledgers[name] = {
            "label": cfg.get("label") or name,
            "mode": cfg.get("rebalance_mode") or CFG.get("rebalance_mode"),
            "year": m, "trades": trades,
            "by_symbol": sorted(by.values(), key=lambda x: x["realized_pnl"], reverse=True),
            "breakdown": breakdown.compute(state),
        }
    return ledgers


def gather(month, only=None):
    ledgers, order = {}, (only or list(LEDGERS.keys()))
    for name in order:
        cfg = LEDGERS.get(name) or {}
        path = cfg.get("state_file")
        if not path or not os.path.exists(path):
            continue
        state = paper.load(path)
        m = month_metrics(state, month, ledger=name)
        trades = _all_trades(state, month)
        ledgers[name] = {
            "label": cfg.get("label") or name,
            "mode": cfg.get("rebalance_mode") or CFG.get("rebalance_mode"),
            "month": m, "trades": trades, "by_symbol": [],
            "diagnostics": ledger_diagnostics(state, month, m),
            "breakdown": breakdown.compute(state),
        }
        by = {}
        for t in trades:
            if t.get("action") != "sell" or not t.get("code"):
                continue
            item = by.setdefault(t["code"], {"code": t["code"], "name": t.get("name"),
                                             "realized_pnl": 0.0, "trades": 0})
            item["realized_pnl"] = round(item["realized_pnl"] + float(t.get("realized_pnl") or 0.0), 2)
            item["trades"] += 1
        ledgers[name]["by_symbol"] = sorted(by.values(), key=lambda x: x["realized_pnl"], reverse=True)
    return ledgers


def _fmt_window(w):
    if not w:
        return "—"
    if isinstance(w, dict):
        return f"{w.get('base')}~{w.get('last')}（{w.get('weeks')} 周）"
    return w


def scorecard_text(only=None):
    """复用 review.py 的判定，拼成一段文字（样本不足/未达标/暂达标）。"""
    try:
        import review
    except Exception:
        return ""
    lines = []
    for name in (only or list(LEDGERS.keys())):
        cfg = LEDGERS.get(name) or {}
        try:
            row = review.one_ledger(name, cfg)
        except Exception as exc:  # 报表不该因为记分卡失败而挂
            lines.append(f"- {name}：记分卡读取失败（{exc}）")
            continue
        if row.get("error"):
            lines.append(f"- {name}：{row['error']}")
            continue
        lines.append(
            f"- {row.get('label') or name}：{row.get('verdict')}"
            f"｜窗口 {_fmt_window(row.get('window'))}｜平仓 {row.get('closed_trades')} 笔"
            f"｜总收益 {_pct(row.get('total_return'), arrow=True)}"
            f"｜超额 {_pct(row.get('excess'), arrow=True)}"
            f"｜回撤 {_pct(row.get('max_drawdown'), arrow=True)}"
            f"｜本季净收益 {_pct(((row.get('net') or {}).get('quarter') or {}).get('net_return'), arrow=True)}"
            f"（季目标 {_pct(((row.get('net') or {}).get('quarter') or {}).get('target_rate'), arrow=True)}）"
            f"｜费用占比 {_pct(row.get('fees_pct'))}"
)
        for r in (row.get("verdict_reasons") or [])[:3]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


_ACT_CN = {"buy": "买入", "sell": "卖出", "buy_open": "买入", "sell_open": "卖出",
           "buy_limit": "买入", "sell_stop": "卖出"}


def _act_cn(action) -> str:
    a = str(action or "")
    return _ACT_CN.get(a.lower(), a)


def to_md(month, data, sc, start, end):
    retro = data.get("retro") or {}
    L = [f"# 投资月报（模拟盘）· {month}", "",
         f"统计区间：{start} ~ {end}；生成：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    # 一、策略复盘（月报不能只做统计：2026-10-30）
    L += ["## 一、上月策略复盘与改进", ""]
    if retro.get("strategy"):
        L += [retro["strategy"], ""]
    else:
        L += ["> ⚠ 未找到 `out/team/retro.md` 的「策略复盘」小节（先跑 T10 复盘员）。"
              "月报不能只做统计：本节要写清上月的策略假设哪里值得改，每条带证据。", ""]
    diag = data.get("diagnostics") or []
    if diag:
        L += ["### 自动诊断信号（代码只报事实，判断在复盘）", ""]
        for d in diag:
            for lvl, txt in (d.get("items") or []):
                L.append(f"- **{d.get('label')}**｜{'⚠ ' if lvl == 'warn' else ''}{txt}")
        L.append("")
    # 信号面验证（2026-11-25 起）：事件面/政策面的预测兑现度，只记录，不改打分
    L += ["### 信号面验证（只记录，不改规则）", ""]
    try:
        import news_eval
        L += news_eval.section_md()
    except Exception as exc:  # noqa: BLE001
        L += [f"- 事件面命中率：读取失败（{type(exc).__name__}）。"]
    try:
        import policy_map
        L += policy_map.section_md()
    except Exception as exc:  # noqa: BLE001
        L += [f"- 政策面跟踪：读取失败（{type(exc).__name__}）。"]
    L.append("")
    # 二、技能优化（A 类当场执行、B 类待拍板）
    L += ["## 二、技能优化（A 类已执行 / B 类待拍板）", ""]
    if retro.get("optimization"):
        L += [retro["optimization"], ""]
        ost = data.get("opt_stats") or opt_stats(retro.get("optimization") or "")
        if ost.get("total"):
            L += [f"> 清单完成度：**{ost.get('done')}/{ost.get('total')}**"
                  "（A 类由 Lead 在月报任务内直接改代码/文档并 git 提交；B 类写 "
                  "`out/team/pending_changes.md` 等用户拍板）", ""]
    else:
        L += ["> ⚠ 未找到 `out/team/retro.md` 的「技能优化」小节（先跑 T10 复盘员）。"
              "T10 必须扫技能自身的 bug/文档/报告/性能/测试缺口，A 类由 Lead 当场执行。", ""]
    # 三、数据统计
    L += ["## 三、数据统计", ""]
    L += ["| 账本 | 调仓模式 | 月度收益 | 总收益（累计） | 同期沪深300 | 超额 | 最大回撤 | 成交 | 费用 | 现金层利息 | 权益层盈亏 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, info in (data.get("ledgers") or {}).items():
        m = info.get("month") or {}
        L.append(f"| {info.get('label')} | {info.get('mode')} | {_pct(m.get('ret_month'), arrow=True)} | "
                 f"{_pct(m.get('total_return'), arrow=True)} | "
                 f"{_pct(m.get('bench_month'), arrow=True)} | {_pct(m.get('excess'), arrow=True)} | "
                 f"{_pct(m.get('mdd'), arrow=True)} | {m.get('trades', 0)} 笔 | {_f(m.get('fees'))} 元 | "
                 f"{_f(m.get('cash_yield'))} 元 | {_f(m.get('equity_pnl'))} 元 |")
    L.append("")
    # 毛利 → 净利对照（一切利润扣 token 后计算，不得只给毛利）
    L += ["> 净利润口径：**净利润 = 账户累计收益 − token 实花**；"
          "token 实花取 `out/budget.json` 该账本累计 spent（不是年度计提）；"
          "净收益率 = 净利润/本金；奖励评估期 2027-10-30 起评判，此前一律 0。", ""]
    L += ["| 账本 | 账户累计收益 | token 实花 | 净利润 | 净收益率 | 档位 |",
          "|---|---|---|---|---|---|"]
    for name, info in (data.get("ledgers") or {}).items():
        nt = ((info.get("month") or {}).get("net") or {})
        if nt.get("error"):
            L.append(f"| {info.get('label')} | — | — | 读取失败 | — | — |")
            continue
        L.append(f"| {info.get('label')} | {_f(nt.get('gross_profit'))} 元 | "
                 f"{_f(nt.get('token_spent'))} 元 | **{_f(nt.get('net_profit'))} 元** | "
                 f"**{_pct(nt.get('net_rate'), arrow=True)}** | {nt.get('grade') or 'n/a'} |")
    L.append("")
    for name, info in (data.get("ledgers") or {}).items():
        m = info.get("month") or {}
        _nt = m.get("net") or {}
        _net_line = (
            f"- 毛利 → 净利：账户收益 {_f(_nt.get('gross_profit'))} 元 − token 实花 "
            f"{_f(_nt.get('token_spent'))} 元 = 净利润 **{_f(_nt.get('net_profit'))} 元**"
            f"｜净收益率 **{_pct(_nt.get('net_rate'))}**（档位 {_nt.get('grade') or 'n/a'}）"
            + (f"｜奖励：{_nt.get('reward_note')}" if _nt.get("reward_pending") else ""))
        L += [f"## {info.get('label')}账本（{name}）", "",
              f"- 月度收益：{_pct(m.get('ret_month'), arrow=True)}（{_f(m.get('pnl_month'))} 元，"
              f"净值 {_f(m.get('nav_start'))} → {_f(m.get('nav_end'))}）",
              f"- 总收益（累计，自建仓）：{_pct(m.get('total_return'), arrow=True)}"
              f"（{_f(m.get('total_pnl'))} 元；本金 {_f(m.get('capital'))} 元）",
              f"- 期末仓位：{_pct(m.get('weight_end'))}；可投资本：{_f(m.get('investable'))} 元"
              f"（本金 {_f(m.get('capital'))} + 已实现盈利 {_f(m.get('gain'))}）",
              f"- 费用：{_f(m.get('fees'))} 元（本金 {_pct(m.get('fees_pct'))}）；止损/熔断：{m.get('stop_outs', 0)} 次",
              _net_line,
              (f"- 逆回购：{m.get('repo_ops', 0)} 笔 1 天期、覆盖 {m.get('repo_covered_days', 0)} 天，"
               f"利息 {_f(m.get('repo_interest'))} 元 − 手续费 {_f(m.get('repo_fees'))} 元 = "
               f"{_f(m.get('repo_net'))} 元（实得年化 {_pct(m.get('repo_effective'))}）"),
              ""]
        rk = m.get("risk") or {}
        if rk.get("error"):
            L += [f"- 风险等级：{rk['error']}", ""]
        elif rk.get("count"):
            _w = rk.get("weights") or {}
            _dist = "｜".join(f"{k} {_pct(v, 1)}" for k, v in sorted(_w.items()))
            L += [f"- 风险等级（月末）：组合 **{rk.get('level_label')}**（R{float(rk.get('level') or 0):.2f}）"
                  f"｜{_dist}｜R2 占比 {_pct(rk.get('r2_share'), 1)}"
                  f"｜{'✓ 合规（只做 R2/R3）' if rk.get('ok') else '✗ 违规：' + '；'.join(rk.get('violations') or [])}",
                  ""]
        else:
            L += ["- 风险等级（月末）：空仓（现金层 R1）", ""]
        trades = info.get("trades") or []
        if trades:
            L += ["| 日期 | 代码 | 名称 | 动作 | 股数 | 价 | 金额 | 费用 | 已实现 | 原因 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
            for t in trades:
                L.append(f"| {t.get('date')} | {t.get('code')} | {t.get('name')} | {_act_cn(t.get('action'))} | "
                         f"{_f(t.get('shares'), 0)} | {_f(t.get('price'), 3)} | {_f(t.get('amount'))} | "
                         f"{_f(t.get('fee'))} | {_f(t.get('realized_pnl'))} | {t.get('why') or ''} |")
            L.append("")
        else:
            L += ["本月没有成交。", ""]
        by = info.get("by_symbol") or []
        if by:
            L += ["| 标的 | 已实现 | 笔数 |", "|---|---|---|"]
            for s in by:
                L.append(f"| {s.get('code')} {s.get('name')} | {_f(s.get('realized_pnl'))} | {s.get('trades')} |")
            L.append("")
    # 四、收益拆解（权益 / 场外 R2 / 现金三层）：2026-11-08月报同样体现
    bds = [(name, info.get("breakdown") or {}) for name, info in (data.get("ledgers") or {}).items()]
    if any(bd for _, bd in bds):
        L += ["## 四、收益拆解（权益 / 场外 R2 / 现金三层）", ""]
        L += ["| 账本 | 资产层 | 投入成本 | 当前价值 | 盈亏 | 占总收益 |",
              "|---|---|---:|---:|---:|---:|"]
        for name, bd in bds:
            if not bd:
                continue
            _lab = ((data.get("ledgers") or {}).get(name) or {}).get("label") or name
            for lab, cost, val, pnl, share in breakdown._rows(bd):
                L.append(f"| {_lab} | {lab} | {_f(cost)} | {_f(val)} | **{_f(pnl)}** | "
                         f"{_pct(share)} |")
            L.append(f"| {_lab} | **合计** | {_f(bd.get('capital'))} | {_f(bd.get('nav'))} | "
                     f"**{_f(bd.get('total_pnl'))}** | 100% |")
            L += ["", f"> {breakdown.note_text(bd)}", ""]
    # 五、策略可行性判定（五问：样本/钱从哪来/风险预算/执行偏差/证伪命中）
    _dyn = (data.get("ledgers", {}) or {})
    for _lg, _info in _dyn.items():
        _dd = (_info.get("month") or {}).get("dynamic") or {}
        if not _dd or not _dd.get("enabled"):
            continue
        L += ["### 动态本金与月度等价年化（口径 2026-11-26 起）", ""]
        if _dd.get("this_month_injected"):
            L.append(f"- 本月报时刻注入 {_f(_dd.get('injection_cny'))} 元；本金 = 上月末净值 + 注资 = "
                     f"**{_f(_dd.get('capital'))} 元**（累计注资 {_f(_dd.get('injected_total'))} 元 / "
                     f"{_dd.get('injections')} 次）")
        else:
            L.append(f"- 本月**没有**注资（起始月 {_dd.get('start_month')}，已累计注资 "
                     f"{_f(_dd.get('injected_total'))} 元 / {_dd.get('injections')} 次）；"
                     f"当前本金 {_f(_dd.get('capital'))} 元，口径 = 上月末净值 + 历史注资")
        L.append("- 月收益率 = 月末净值 / 月初净值 − 1（**不含**当月注资）；"
                 "等价年化 = (1+月收益率)^(365/该月实际运行天数) − 1")
        L += ["", "| 月份 | 实际运行天数 | 月收益率 | 等价年化 | 盈亏（元） |",
              "|---|---|---|---|---|"]
        for _r in (_dd.get("rates") or []):
            L.append("| %s | %s | %s | %s | %s |" % (
                _r.get("month"), _r.get("days") or "—",
                _pct(_r.get("r"), arrow=True), _pct(_r.get("equiv"), arrow=True),
                _f(_r.get("pnl"))))
        L += ["", f"- **本年（{_dd.get('year')}）平均（年初至本月，年报定档口径）："
                  f"{_pct(_dd.get('year_mean_equiv'), arrow=True)}**（{_dd.get('year_months')} 个月）"]
        _thin = [r for r in (_dd.get("rates") or []) if (r.get("days") or 0) < 5]
        if _thin:
            L.append("- ⚠ 样本偏薄：" + "、".join(f"{r.get('month')} 只运行 {r.get('days')} 天" for r in _thin)
                     + " → 该月等价年化是外推值，不构成结论（首月或账本重置后常见）。")
        _miss = _dd.get("year_missing_months") or []
        if _miss:
            L.append("- ⚠ 这些月份样本不足、未计入本年平均：" + "、".join(_miss))
        if (_dd.get("months") or 0) > (_dd.get("year_months") or 0):
            L.append(f"- 全部历史累计平均（{_dd.get('months')} 个月）：{_pct(_dd.get('mean_equiv'), arrow=True)}"
                     "（跨年，只作参考，不进年报定档）")
        if (_dd.get("year_months") or 0) < 3:
            L.append(f"- 本年只有 {_dd.get('year_months')} 个月样本，平均值只作观察。")
        L += ["- token 已从账户现金出账（净值 = 净口径）：`收益 = 净值 − 累计投入` 已含 token 成本，"
              "不再二次扣减。", ""]
    L += ["## 五、策略可行性判定（五问）", ""]
    _v = data.get("viability")
    L += [_v if _v else VIABILITY_PLACEHOLDER.format(key=month), ""]
    # 六、本年计划对齐（年报写的 annual_plan.md；月报只读、逐条回答「每月必答」）
    _ap = data.get("annual_plan")
    if _ap:
        _ap = "\n".join(("### " + ln[3:]) if ln.startswith("## ") else ln for ln in _ap.splitlines())
    L += ["## 六、本年计划对齐", ""]
    L += [_ap if _ap else "本年度无年度计划（年报未生成）；下一年 1 月 1 日年报会写 `out/team/annual_plan.md`。", ""]
    if sc:
        L += ["## 七、记分卡（累计）", "", sc, ""]
    L += ["## 八、下月计划", "",
          "- 按「一、上月策略复盘」的改进项执行：A 类已改的从下期生效，B 类等用户拍板后生效；",
          "- 每周三、周六 20:30 继续按同一套规则决策；不为单月结果调阈值。",
          "- 净值回撤触发组合熔断线后全部卖出，当周不再开新仓。",
          "- 样本满 12 周且 ≥10 笔平仓后，用记分卡做「可不可行」的判定。",
          "- 任何规则改动走一次 git 提交（原值/新值/依据写进 commit message）。", "",
          "边界：模拟盘记录，不构成投资建议，不承诺收益。"]
    return "\n".join(L)


def to_md_annual(year, data, sc, start, end):
    """年报 Markdown：一年收益 + 累计总收益 + 逐月分解 + 年度结算。"""
    L = [f"# 投资年报 · {year}（年度结算）", "",
         f"- 统计区间：{start} ~ {end}（自然年）",
         f"- 生成：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
         "- 口径：净利润 = 账户累计收益 − token 实花；年报是结算日，只读账本、不重新决策。", "",
         "## 一、年度结论"]
    for name, info in (data.get("ledgers") or {}).items():
        y = info.get("year") or {}
        nt = y.get("net") or {}
        reward = (f"{_f(nt.get('reward_cny'))} 元（全部转次年 token 预算）"
                  if not nt.get("reward_pending") else (nt.get("reward_note") or "评估期未到"))
        L += [f"### {info.get('label')}（{name}）",
              f"- **年度收益：{_pct(y.get('ret_year'), arrow=True)}**（{_f(y.get('pnl_year'))} 元；"
              f"净值 {_f(y.get('nav_start'))} → {_f(y.get('nav_end'))}）",
              f"- **总收益（累计，自建仓）：{_pct(y.get('total_return'), arrow=True)}**"
              f"（{_f(y.get('total_pnl'))} 元；本金 {_f(y.get('capital'))} 元）",
              f"- 同期沪深300：{_pct(y.get('bench_month'), arrow=True)}｜超额：{_pct(y.get('excess'), arrow=True)}",
              f"- 年内最大回撤：{_pct(y.get('mdd'), arrow=True)}｜费用：{_f(y.get('fees'))} 元"
              f"（本金 {_pct(y.get('fees_pct'))}）",
              f"- 净利润：{_f(nt.get('net_profit'))} 元｜净收益率：{_pct(nt.get('net_rate'), arrow=True)}"
              + (f"｜等价年化净收益率：{_pct(nt.get('net_rate_ann'), arrow=True)}"
                 f"（{nt.get('days') or '—'} 天）" if nt.get("annualized") else "")
              + f"｜档位：{nt.get('grade') or 'n/a'}｜奖励：{reward}", ""]
        s = (data.get("settlement") or {}).get(name) or {}
        rc = s.get("record") or {}
        if rc:
            L += [f"- 年度结算：本金 {_f(rc.get('capital_before'))} → "
                  f"{_f(rc.get('capital_after'))} 元（奖励从现金划出 {_f(rc.get('reward_cny'))} 元；"
                  f"清算后总收益 {_pct(rc.get('total_return'), arrow=True)}）", ""]
    L.append("## 二、月度分解")
    L += ["| 月份 | 月末净值 | 月度收益 | 累计总收益 |", "|---|---|---|---|"]
    for name, info in (data.get("ledgers") or {}).items():
        for row in ((info.get("year") or {}).get("months") or []):
            L.append(f"| {row['month']} | {_f(row.get('nav_end'))} | "
                     f"{_pct(row.get('ret_month'), arrow=True)} | {_pct(row.get('total_return'), arrow=True)} |")
    L.append("")
    L.append("## 三、全年交易与现金")
    for name, info in (data.get("ledgers") or {}).items():
        y = info.get("year") or {}
        L += [f"### {info.get('label')}（{name}）",
              f"- 成交：{y.get('trades', 0)} 笔（买 {y.get('buys', 0)} / 卖 {y.get('sells', 0)}）；"
              f"已实现盈亏 {_f(y.get('realized'))} 元｜浮动盈亏 {_f(y.get('unrealized'))} 元",
              f"- 现金层利息 {_f(y.get('cash_yield'))} 元｜权益层盈亏 {_f(y.get('equity_pnl'))} 元",
              f"- 逆回购：{y.get('repo_ops', 0)} 笔、覆盖 {y.get('repo_covered_days', 0)} 天，"
              f"净收益 {_f(y.get('repo_net'))} 元（实得年化 {_pct(y.get('repo_effective'))}）",
              f"- 止损/熔断：{y.get('stop_outs', 0)} 次｜期末仓位：{_pct(y.get('weight_end'))}", ""]
    L.append("## 四、年度结算")
    st = data.get("settlement") or {}
    for name, info in (data.get("ledgers") or {}).items():
        s = st.get(name) or {}
        rec = s.get("record") or {}
        if rec:
            L.append(f"- {info.get('label')}（{name}）：结算日 {rec.get('settled_at')}｜"
                     f"{'已结算过（不重复入账）' if s.get('already') else '本次结算'}｜"
                     f"年度收益 {_pct(rec.get('year_return'), arrow=True)}｜"
                     f"总收益 {_pct(rec.get('total_return'), arrow=True)}｜"
                     f"净利润 {_f(rec.get('net_profit'))} 元｜奖励 {_f(rec.get('reward_cny'))} 元")
        else:
            L.append(f"- {info.get('label')}（{name}）：未结算（--no-settle 或结算失败）")
    L += ["", "结算口径：写 `meta.annual_settlements.<年>`（账本先备份）；奖励（评估期后）全部转次年 token 预算；"
              "旧年份结算不追溯重算。", "",
          "## 五、策略可行性判定（五问）", ""]
    _v = data.get("viability")
    L += [_v if _v else VIABILITY_PLACEHOLDER.format(key=year), ""]
    _sh = (data.get("score_health") or "（未生成：先归档评分面板，再跑 `python scripts/score_panel.py --health`）").strip()
    if _sh.startswith("## "):
        _sh = "\n".join(_sh.splitlines()[1:]).strip()
    _sh = "\n".join(("### " + ln[3:]) if ln.startswith("## ") else ln for ln in _sh.splitlines())
    for _lg, _info in (data.get("ledgers") or {}).items():
        _dd = ((_info.get("year") or {}).get("dynamic")) or {}
        if not _dd or not _dd.get("enabled"):
            continue
        L += ["## 五b、动态本金与月度等价年化（年报定档口径）", ""]
        L.append(f"- 年中累计注资 {_f(_dd.get('injected_total'))} 元（{_dd.get('injections')} 次）；"
                 f"当前本金 {_f(_dd.get('capital'))} 元")
        L += ["", "| 月份 | 实际运行天数 | 月收益率 | 等价年化 | 盈亏（元） |", "|---|---|---|---|---|"]
        for _r in (_dd.get("rates") or []):
            L.append("| %s | %s | %s | %s | %s |" % (
                _r.get("month"), _r.get("days") or "—",
                _pct(_r.get("r"), arrow=True), _pct(_r.get("equiv"), arrow=True), _f(_r.get("pnl"))))
        _rows = _dd.get("rates") or []
        if _rows:
            L.append(f"- 先记录最后一个月（{_rows[-1].get('month')}）的等价年化："
                     f"{_pct(_rows[-1].get('equiv'), arrow=True)}"
                     f"（{_rows[-1].get('days') or '—'} 天）→ 再取本年所有月平均")
        L += ["", f"- **年报定档年化 = 本年（{_dd.get('year')}）{_dd.get('year_months')} 个月"
                  f"等价年化率的算术平均：{_pct(_dd.get('year_mean_equiv'), arrow=True)}**"]
        _thin = [r for r in (_dd.get("rates") or []) if (r.get("days") or 0) < 5]
        if _thin:
            L.append("- ⚠ 样本偏薄：" + "、".join(f"{r.get('month')} 只运行 {r.get('days')} 天" for r in _thin)
                     + " → 这些月的等价年化是外推值，请结合月份数一起看。")
        _miss = _dd.get("year_missing_months") or []
        if _miss:
            L.append("- ⚠ 这些月份样本不足、未计入本年平均：" + "、".join(_miss))
        L.append("")
    L += ["## 六、选股评分体检", "", _sh, ""]
    L += ["## 七、记分卡", sc or "（无）", "",
          "边界：模拟盘记录，不构成投资建议，不承诺收益。"]
    return "\n".join(L)


def settle_year(state, year, metrics, path, ledger="50k", as_of=None):
    """年度结算：备份 → 奖励从现金划出 → 本金重置为「年末净值 − 奖励」 → 写 annual_settlements。

    奖励 = 当年净利润 × 档位比例（档位按等价年化净收益率定）；奖励转次年 token 预算。
    本金重置后，下一年收益率以剩余净值为起点；总收益用 `meta.cum_index` 跨年链式累计。
    幂等：同一年已结算过直接返回，不重算旧账。
    """
    y = str(int(year))
    meta = state.setdefault("meta", {})
    recs = meta.setdefault("annual_settlements", {})
    if y in recs:
        return {"already": True, "record": recs[y]}
    nt = metrics.get("net") or {}
    reward = round(float(nt.get("reward_cny") or 0.0), 2)
    settled_at = as_of or datetime.date.today().isoformat()
    nav_end = round(float(metrics.get("nav_end") or 0.0), 2)
    cap_before = round(float(meta.get("capital") or 0.0), 2)
    year_ret = float(metrics.get("ret_year") or 0.0)
    paper.backup_state(path)
    if reward > 0:
        state["cash"] = round(max(0.0, float(state["cash"]) - reward), 2)
        # A3：奖励是净值流出，最后一根 nav 必须同步减——否则 capital（按 nav 算）
        # 会比实际现金多出一个 reward，下一年收益被系统性高估。
        _navs = state.get("nav") or []
        if _navs:
            _last = _navs[-1]
            _last["nav"] = round(max(0.0, float(_last.get("nav") or 0.0) - reward), 2)
            if "cash" in _last:
                _last["cash"] = round(max(0.0, float(_last["cash"] or 0.0) - reward), 2)
        paper.event(state, "info",
                    f"年度结算：奖励 {reward:,.2f} 元从现金划出（转次年 token 预算）",
                    date=settled_at)
    nav_after = round(nav_end - reward, 2)
    cum = round(float(meta.get("cum_index") or 1.0) * (1.0 + year_ret), 8)
    meta.setdefault("initial_capital", cap_before)
    meta["cum_index"] = cum
    meta["capital"] = nav_after
    meta["withdrawn_total"] = round(float(meta.get("withdrawn_total") or 0.0) + reward, 2)
    meta["annual_baseline"] = {
        "year": int(year), "capital": nav_after,
        "realized": round(paper.realized_pnl_total(state), 2),
        "cash_interest": round(paper.cash_interest_total(state), 2),
    }
    rec = {
        "year": int(year), "settled_at": settled_at,
        "nav_start": metrics.get("nav_start"), "nav_end": nav_end,
        "nav_before_reward": nav_end, "nav_after_reward": nav_after,
        "capital_before": cap_before, "capital_after": nav_after,
        "year_return": year_ret, "year_pnl": metrics.get("pnl_year"),
        "total_return": round(cum - 1.0, 6),
        "total_pnl": round(float(meta["initial_capital"]) * (cum - 1.0), 2),
        "withdrawn_total": meta["withdrawn_total"],
        "net_profit": nt.get("net_profit"), "net_rate": nt.get("net_rate"),
        "net_rate_ann": nt.get("net_rate_ann"),
        "grade": nt.get("grade"), "reward_cny": reward,
        "reward_pending": nt.get("reward_pending"),
    }
    recs[y] = rec
    paper.event(state, "info",
                f"年度结算 {year}：年度收益 {_pct(rec['year_return'], arrow=True)}，"
                f"总收益 {_pct(rec['total_return'], arrow=True)}，"
                f"净利润 {_f(rec['net_profit'])} 元，奖励 {_f(reward)} 元，"
                f"本金 {_f(cap_before)} → {_f(nav_after)} 元", date=settled_at)
    paper.save(state, path)
    if reward > 0:
        try:
            import billing
            billing.add_bonus(ledger, reward, note=f"年报结算 {year} 奖励（{rec.get('grade')}）")
        except Exception as exc:  # noqa: BLE001 — 奖励入账失败要报，但账本结算已落
            print(f"[monthly] ⚠ 奖励转次年预算失败：{exc}")
    return {"already": False, "record": rec}


def main(argv=None):
    ap = argparse.ArgumentParser(description="月度复盘：Markdown + HTML（--annual 为年报/年度结算）")
    ap.add_argument("--month", default="", help="YYYY-MM，默认按今天所在月；--annual 时写 <年>-12")
    ap.add_argument("--ledger", default="all", choices=list(LEDGERS.keys()) + ["all"])
    ap.add_argument("--out-dir", default=POUT)
    ap.add_argument("--annual", action="store_true",
                    help="年报模式（每年 1 月 1 日）：按自然年统计 + 年度结算（写 meta.annual_settlements）")
    ap.add_argument("--no-settle", action="store_true", help="只出年报，不写年度结算记录（重渲染用）")
    a = ap.parse_args(argv)

    month = a.month or datetime.date.today().strftime("%Y-%m")
    if a.annual and not a.month:
        month = f"{datetime.date.today().year - 1}-12"
    try:
        datetime.datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError:
        print(f"[monthly] --month 格式应为 YYYY-MM，收到 {month!r}")
        return 2

    only = None if a.ledger == "all" else [a.ledger]
    os.makedirs(a.out_dir, exist_ok=True)

    if a.annual:
        if not month.endswith("-12"):
            print(f"[monthly] 年报请用 --month <年>-12（例如 2026-12 表示 2026 年报），收到 {month!r}")
            return 2
        year = int(month[:4])
        ledgers = gather_annual(year, only)
        if not ledgers:
            print("[monthly] 没有可读的账本（先跑 decide.py --init 建账）")
            return 1
        sc = scorecard_text(only)
        retro = read_retro()
        data = {"ledgers": ledgers, "scorecard": sc, "retro": retro,
                "viability": read_viability(str(year)),
                "score_health": score_health_md(year),
                "start": f"{year}-01-01", "end": f"{year}-12-31", "year": year}
        settlement = {}
        if not a.no_settle:
            for name, info in ledgers.items():
                path = (LEDGERS.get(name) or {}).get("state_file")
                if not path:
                    continue
                try:
                    st = paper.load(path)
                    # A1：settle(month) 的 month = **刚结束的报告月**，注入算下一个月；
                    # 年报对应 12 月，注资才算次年 1 月（原来传 1 月会把 12 月那笔吞掉）。
                    _rm = f"{year}-12"
                    _jm = f"{year + 1}-01"
                    # 收口（token 出账 → 重算月率）失败时必须留痕且**不注资**：
                    # 否则下一年本金基准会建在「token 未出账、月率未落」的旧净值上（测试审计 A-1）。
                    _settle_err = ""
                    try:
                        capital_dyn.settle(st, name, _rm, inject_after=False)
                    except Exception as exc:  # noqa: BLE001
                        _settle_err = f"动态本金收口失败：{exc}"
                    _row = settle_year(st, year, info["year"], path, ledger=name)
                    settlement[name] = _row if isinstance(_row, dict) else dict(_row or {})
                    if _settle_err:
                        settlement[name]["settle_error"] = _settle_err
                    else:
                        try:
                            _inj = capital_dyn.inject(st, _jm)
                            if _inj.get("ok") or _inj.get("already"):
                                paper.save(st, path)
                        except Exception as exc:  # noqa: BLE001
                            settlement[name]["inject_error"] = str(exc)
                except Exception as exc:  # noqa: BLE001 — 结算失败要报，但年报照出
                    settlement[name] = {"error": str(exc)}
        data["settlement"] = settlement
        html_path = report_html.write_annual_report(
            os.path.join(a.out_dir, f"annual_report_{year}.html"), year=year, data=data)
        md_path = os.path.join(a.out_dir, f"annual_{year}.md")
        io.open(md_path, "w", encoding="utf-8", newline="\n").write(
            to_md_annual(year, data, sc, data["start"], data["end"]))
        print(f"[monthly] 年报 HTML：{html_path}")
        print(f"[monthly] 年报 md  ：{md_path}")
        for name, info in ledgers.items():
            y = info["year"]
            s = settlement.get(name) or {}
            flag = ("已结算过" if s.get("already") else
                    ("结算失败：" + s["error"] if s.get("error") else
                     ("已结算" if s else "未结算（--no-settle）")))
            print(f"[monthly] {info['label']}：年度收益 {_pct(y['ret_month'], arrow=True)}（{_f(y['pnl_month'])} 元），"
                  f"总收益 {_pct(y['total_return'], arrow=True)}（{_f(y['total_pnl'])} 元），"
                  f"净利润 {_f((y.get('net') or {}).get('net_profit'))} 元｜{flag}")
        return 0

    dyn_res = {}
    if not a.no_settle:
        for _name in (only or list(LEDGERS.keys())):
            _p = (LEDGERS.get(_name) or {}).get("state_file")
            if not _p or not os.path.exists(_p):
                continue
            try:
                _st = paper.load(_p)
                dyn_res[_name] = capital_dyn.settle(_st, _name, month)
                paper.save(_st, _p)
            except Exception as exc:  # noqa: BLE001 — 动态本金失败不能把月报整个搞挂
                dyn_res[_name] = {"error": str(exc)}
    ledgers = gather(month, only)
    if not ledgers:
        print("[monthly] 没有可读的账本（先跑 decide.py --init 建账）")
        return 1
    sc = scorecard_text(only)
    retro = read_retro()
    diagnostics = [{"label": info.get("label") or name, "items": info.get("diagnostics") or []}
                   for name, info in ledgers.items()]
    os.makedirs(a.out_dir, exist_ok=True)
    # 统计区间终点写当月最后一天（2026-11-01 修：原写「-月末」在报告里像个半成品）
    _last_day = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    data = {"ledgers": ledgers, "scorecard": sc, "retro": retro,
            "diagnostics": diagnostics,
            "viability": read_viability(month),
            "annual_plan": read_annual_plan(),
            "opt_stats": opt_stats(retro.get("optimization") or ""),
            "start": f"{month}-01", "end": f"{month}-{_last_day:02d}"}
    html_path = report_html.write_monthly_report(
        os.path.join(a.out_dir, f"monthly_report_{month}.html"), month=month, data=data)
    md_path = os.path.join(a.out_dir, f"monthly_{month}.md")
    io.open(md_path, "w", encoding="utf-8", newline="\n").write(
        to_md(month, data, sc, data["start"], data["end"]))
    print(f"[monthly] 月报 HTML：{html_path}")
    print(f"[monthly] 月报 md  ：{md_path}")
    print(f"[monthly] 复盘摘取：策略复盘 {'✓' if retro.get('strategy') else '⚠ 缺'}｜"
          f"技能优化 {'✓' if retro.get('optimization') else '⚠ 缺'}（源 {retro.get('path')}）")
    for name, info in ledgers.items():
        m = info["month"]
        print(f"[monthly] {info['label']}：净值 {_f(m['nav_start'])} → {_f(m['nav_end'])}"
              f"（{_pct(m['ret_month'], arrow=True)}），成交 {m['trades']} 笔，"
              f"现金层 {_f(m['cash_yield'])} 元 / 权益层 {_f(m['equity_pnl'])} 元"
              + (f"｜注资 {_f((dyn_res.get(name) or {}).get('injection', {}).get('injection'))}"
                 f" / 本金 {_f((dyn_res.get(name) or {}).get('injection', {}).get('capital'))}"
                 if ((dyn_res.get(name) or {}).get("injection") or {}).get("ok") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
