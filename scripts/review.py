# -*- coding: utf-8 -*-
"""复盘记分卡：把模拟盘账本累计成一张「够不够格上真钱」的表。

只读账本与 out/ 产物，不取数、不下单、不改规则。
判定口径（2026-11-07 用户定，见 SKILL.md §8.7）：满 12 周且平仓 ≥10 笔才谈期望；
之后的硬标准只有两条——**①净利润 = 账户累计收益 − token 实花（out/budget.json 累计 spent）≥ 0；
②扣掉 token 实花后的净年化 > 无风险利率基准（= 用户 1 年期定存 1.3%，与 SCORE.floor_rate 同口径；见 config.PERF.deposit_rate 及其 as_of）。
档位按净收益率判；奖励评估期 2027-10-30 起评判，此前一律 0。**
四条诊断项（超额 > 0 / 回撤 > -15% / 单笔期望 > 0 / 费用占比 < 2%）只作诊断，不参与一票否决。

为什么基准要带 as_of 且 fail loud：无风险利率会移动（2026-10 已降到 1 年期 1.18%），
用写死/兜底的旧值等于悄悄把达标线往下挪；同时**达标只代表跑赢无风险利率，不代表绝对金额可观**，
所以判定输出会同时给绝对收益额（元）。

用法：
  python scripts/review.py                 # 全部账本都算（本部署只有 50k）
  python scripts/review.py --ledger 50k
  python scripts/review.py --token-cost 4.91 --deposit-rate 0.013   # 按实际账单/最新基准覆盖
产出：out/paper/scorecard.md（给人读）与 out/paper/scorecard.json（给程序读）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import billing  # noqa: E402
import paper  # noqa: E402
import score  # noqa: E402

CFG = config.PAPER
PERF = getattr(config, "PERF", {})          # 验收口径：token 成本 + 存款利率
OUT = CFG["package_dir"]

MIN_WEEKS = 12
MIN_CLOSED = 10
MAX_DD = -0.15          # 诊断线（不再一票否决）：与 EXEC.portfolio_dd_halt 同量级
MAX_FEE_RATIO = 0.02    # 诊断线：累计费用占本金 2% 以上视为拖累，写警告
BENCH_NAME = "沪深300"
TOKEN_COST_OVERRIDE = None   # --token-cost：覆盖 token 累计实花（元），只影响本次报告


def _f(v, nd=2, suffix=""):
    if v is None:
        return "n/a"
    return f"{v:,.{nd}f}{suffix}"


def _pct(v, nd=2):
    if v is None:
        return "n/a"
    if abs(v) < 5e-5:
        return f"{0:.{nd}%}"
    return f"{v:+.{nd}%}"


def deposit_rate() -> float:
    """验收基准（无风险利率）唯一取值入口：缺字段/数值离谱就 fail loud，不用旧值兜底。

    为什么不兜底：2026-10-27 实测 1 年期国债 1.1799%，已低于旧写死的 1.5%。
    兜底会让达标线悄悄走偏（要么自己放水、要么自己加码），判定就失去意义。
    """
    v = PERF.get("deposit_rate")
    if v is None:
        raise RuntimeError("config.PERF.deposit_rate 缺失：验收基准必须显式给出（含 deposit_rate_as_of / "
                           "deposit_rate_source），不允许静默兜底旧值")
    try:
        rate = float(v)
    except (TypeError, ValueError):
        raise RuntimeError(f"config.PERF.deposit_rate={v!r} 不是数字")
    if not (0.0 <= rate <= 0.10):
        raise RuntimeError(f"config.PERF.deposit_rate={rate!r} 不合理（年化应在 0–10% 之间）")
    return rate


def deposit_meta() -> dict:
    """基准的完整口径（rate + as_of + source + basis），报告里必须把 as_of 一起写出来。"""
    return {"rate": deposit_rate(),
            "as_of": str(PERF.get("deposit_rate_as_of") or ""),
            "source": str(PERF.get("deposit_rate_source") or ""),
            "basis": str(PERF.get("deposit_rate_basis") or "")}


def _weekkey(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return (y, w)


def window(state):
    navs = state.get("nav") or []
    base = (state.get("meta") or {}).get("baseline_date")
    last = navs[-1]["date"] if navs else base
    weeks = len({_weekkey(n["date"]) for n in navs}) - 1 if navs else 0
    return {"base": base, "last": last, "weeks": max(weeks, 0), "days": len(navs)}


def exposure_avg(state):
    navs = state.get("nav") or []
    rs = [n["market_value"] / n["nav"] for n in navs if n.get("nav")]
    return sum(rs) / len(rs) if rs else 0.0


def bench_return(state):
    navs = state.get("nav") or []
    if not navs:
        return None
    first = navs[0].get("bench_nav")
    last = navs[-1].get("bench_nav")
    if not first or not last:
        return None
    return last / first - 1


def kind_attribution(state):
    """已实现（按卖出成交记账）与浮动（按持仓 last_close 估）分别按 kind 汇总。"""
    real = {}
    for t in state.get("trades") or []:
        if t.get("action") != "sell":
            continue
        k = t.get("kind") or "stock"
        real[k] = real.get(k, 0.0) + float(t.get("realized_pnl") or 0.0)
    float_ = {}
    for h in (state.get("holdings") or {}).values():
        k = h.get("kind") or "stock"
        last = float(h.get("last_close") or h.get("cost_basis") or 0.0)
        float_[k] = float_.get(k, 0.0) + float(h["shares"]) * (last - float(h["cost_basis"] or 0.0))
    return real, float_


def stop_outs(state):
    hits = 0
    for t in state.get("trades") or []:
        if t.get("action") != "sell":
            continue
        why = str(t.get("why") or t.get("reason") or "")
        if "止损" in why or "熔断" in why:
            hits += 1
    return hits


def expectancy(st, win_rate, avg_win, avg_loss):
    """单笔期望 = 胜率×平均盈利 + 败率×平均亏损（avg_loss 为负数）。"""
    if win_rate is None:
        return None
    return win_rate * (avg_win or 0.0) + (1 - win_rate) * (avg_loss or 0.0)


def net_metrics(state, st, win, led_cfg, token_cost=None, ledger=None):
    """按2026-11-07口径算「净利润」：账户累计收益 − token **实花**。

    口径（2026-11-07 用户定）：
      净利润   = 账户累计收益（元）− token 实花（元）
      净收益率 = 净利润 / 本金
      token 实花 = `out/budget.json` 里该账本累计 `spent_cny`（真实扣费），
                   不是 `config.PERF.token_cost_annual_cny` 的年度计提（旧口径已废）。
    档位用净收益率（`score.grade_of`，阈值语义不变）；奖励评估期未到（2027-10-30 前）一律 0，
    文案见 `config.SCORE.reward_eval_note`。

    净年化 = 用自然日跨度把净收益率年化（样本不足时仍算，但判定会被样本闸拦住）。
    基准 = `config.PERF.deposit_rate`（1 年期定存/国债口径，见 config 及其 as_of），必须带 as_of。
    """
    # A6：金额/收益率的分母用「累计投入」（初始 + Σ注资）；配置本金只作展示。
    cap = (paper.contributed_capital(state)
           or float((state.get("meta") or {}).get("capital") or 0.0)
           or float(led_cfg.get("capital") or 0.0))
    total = st.get("total_return")
    gross_pnl = (total * cap) if (total is not None and cap) else None
    years = None
    base, last = win.get("base"), win.get("last")
    if base and last:
        try:
            years = max((dt.date.fromisoformat(last) - dt.date.fromisoformat(base)).days / 365.0, 1e-9)
        except ValueError:
            years = None

    # token 实花（budget.json 累计）；文件缺失/账本未登记时退回 0 并写明，不编数字。
    token_src = "out/budget.json 累计 spent"
    if token_cost is None:
        try:
            bs = billing.budget_state(ledger or "50k", cap)
            token_spent = float(bs.get("spent_cny") or 0.0)
            token_src = f"out/budget.json（{ledger or '50k'} 累计 spent）"
        except Exception as exc:  # noqa: BLE001 — 记分卡不能因为缺预算文件而崩
            token_spent = 0.0
            token_src = f"out/budget.json 不可用（{exc}）→ 按 0，偏高"
    else:
        token_spent = float(token_cost)
        token_src = "命令行 --token-cost 覆盖（累计实花）"

    # A5：token 已从现金扣过时，nav 已是净口径，再减一次就是重复扣。
    try:
        import capital_dyn as _cd
        # 只有在账本真的把 token 记账进净值（大写 meta.token_booked 有记录）时才算「已扣」，
        # 否则测试/旧账本/未收口月份仍按「毛收益 − token」口径（A5）。
        _tok_in_nav = bool(_cd.enabled() and _cd.token_from_cash()
                           and ((state.get("meta") or {}).get("token_booked") or {}))
        _flows = _cd.flows(state)
    except Exception:  # noqa: BLE001
        _cd, _tok_in_nav, _flows = None, False, []
    net_pnl = (gross_pnl if _tok_in_nav else (gross_pnl - token_spent)) \
        if gross_pnl is not None else None
    if net_pnl is not None and abs(net_pnl) < 0.005:  # 避免 -0.00 显示噪声
        net_pnl = 0.0
    net_return = (net_pnl / cap) if (net_pnl is not None and cap) else None
    dmeta = deposit_meta()          # 缺基准直接抛错，不静默用旧值
    deposit = dmeta["rate"]
    net_ann = None
    # 窗口不足 7 天不做年化：0 天样本会把成本/收益放大成极端年化，纯属噪声
    if net_return is not None and years and years >= 7 / 365.0:
        net_ann = (1 + net_return) ** (1 / years) - 1 if net_return > -1 else -1.0
    bar_ok = bool(net_pnl is not None and net_pnl >= 0
                  and net_ann is not None and net_ann > deposit)
    grade = score.grade_of(net_return) if net_return is not None else None
    reward_open = score.reward_period_open()
    # ---- 多周期加权评分（2026-11-25 用户拍板：季 50% / 年 20% / 长期 30%，各窗口折年化）----
    # 为什么放在 net_metrics 里：验收判据只有一处口径，报表/记分卡/HTML 都从这里取，
    # 免得月报一套、周报一套。窗口 token 成本按 runs 的实际日期落在窗口内求和。
    multi = None
    quarter = None
    try:
        _as_of = win.get("last") or None
        _bs = billing.budget_state(ledger or "50k", cap)
        _runs = _bs.get("runs") or []
        _w = score.windows_from_nav(state.get("nav") or [], as_of=_as_of,
                                    runs=_runs, capital=cap, flows=_flows,
                                    token_in_nav=_tok_in_nav)
        multi = score.evaluate_multi(_w, deposit_rate=deposit)
        for _k, _v in (multi.get("terms") or {}).items():
            _raw = (_w or {}).get(_k) or {}
            _v["label"] = {"quarter": "季度（12 周滚动）", "year": "年度（52 周滚动）",
                           "long": "三年（不足则自成立）"}.get(_k, _k)
            _v["start"] = _raw.get("start")
            _v["end"] = _raw.get("end")
            _v["gross_return"] = _raw.get("gross_return")
            _v["token_cost_cny"] = _raw.get("token_cost_cny")
    except Exception as exc:  # noqa: BLE001 — 加权评分失败不影响老口径与报表
        multi = {"error": f"多周期加权评分失败：{exc}"}
    # ---- 本季累计（B 类 #22 主指标）：季初→as_of，口径与滚动窗口一致 ----
    try:
        quarter = score.quarter_metrics(state.get("nav") or [], as_of=win.get("last"),
                                        runs=_runs, capital=cap, deposit_rate=deposit,
                                        flows=_flows, token_in_nav=_tok_in_nav)
        quarter["token_quarter_budget_cny"] = float(
            (config.PERF or {}).get("token_cost_quarter_cny") or 0.0)
        _tq = quarter.get("token_cost_cny")
        _bq = quarter.get("token_quarter_budget_cny")
        if _tq is not None and _bq:
            quarter["token_budget_used_pct"] = _tq / _bq
    except Exception as exc:  # noqa: BLE001 — 季度口径失败不能再把老报表带崩
        quarter = {"available": False, "note": f"本季口径失败：{exc}"}
    return {
        "capital": cap, "token_spent": round(token_spent, 2),
        "token_cost": round(token_spent, 2),          # 兼容旧字段名
        "token_cost_basis": token_src,
        "deposit_rate": deposit, "deposit_as_of": dmeta["as_of"],
        "deposit_source": dmeta["source"],
        "gross_pnl": gross_pnl, "net_pnl": net_pnl, "net_return": net_return,
        "net_annualized": net_ann, "years": years, "bar_ok": bar_ok,
        "grade": grade, "grade_basis": "净收益率（账户收益 − token 实花）/ 本金",
        "multi": multi,
        "multi_score_ann": (multi or {}).get("score_ann"),
        "multi_excess_ann": (multi or {}).get("excess_ann"),
        "multi_sample_ok": bool((multi or {}).get("sample_ok")),
        "multi_grade": (multi or {}).get("grade"),
        "quarter": quarter,
        "quarter_net_return": (quarter or {}).get("net_return"),
        "quarter_bar_ok": (quarter or {}).get("bar_ok"),
        "reward_pending": not reward_open,
        "reward_note": (config.SCORE.get("reward_eval_note")
                        if not reward_open else "评估期已到（可按档位计提奖励）"),
    }


def judge(st, win, excess, mdd, fee_ratio, closed, exp, net=None):
    """先卡样本闸，再按用户两条验收线判「达标/未达标」，旧四指标只作诊断。"""
    lack = []
    if win["weeks"] < MIN_WEEKS:
        lack.append(f"还差 {MIN_WEEKS - win['weeks']} 周")
    if closed < MIN_CLOSED:
        lack.append(f"还差 {MIN_CLOSED - closed} 笔平仓")
    if lack:
        # 样本不足也要把多周期加权摆出来（只显示、不判档），否则用户看不到新口径在跑。
        reasons = [f"观察窗 {win['weeks']} 周 / 平仓 {closed} 笔；" + "、".join(lack) + "才能谈期望"]
        multi = (net or {}).get("multi") or {}
        if multi.get("error"):
            reasons.append(f"多周期加权：{multi['error']}")
        elif multi.get("sample_ok") and multi.get("score_ann") is not None:
            _parts = []
            for _k in ("quarter", "year", "long"):
                _t = (multi.get("terms") or {}).get(_k) or {}
                if _t.get("sample_ok"):
                    _parts.append(f"{_k} {_pct(_t.get('ann_rate'))}"
                                  f"（{int(_t.get('days') or 0)} 天，权重 {_t.get('weight_used', 0):.0%}）")
            reasons.append(f"多周期加权（季50/年20/长期30，各窗口折年化）："
                           + "｜".join(_parts)
                           + f" → 加权年化 {_pct(multi.get('score_ann'))}"
                           f"（无风险基准 {_pct(multi.get('deposit_rate'))}）")
        elif multi:
            _t = (multi.get("terms") or {})
            _shown = "、".join(
                f"{_k} {int(_v.get('days') or 0)} 天" for _k, _v in _t.items() if _v.get("days"))
            reasons.append(f"多周期加权（季50/年20/长期30）：窗口 {_shown or '无数据'}，"
                           f"都还没满 {int(multi.get('min_window_days') or 84)} 天 → 只显示不判档")
        return "样本不足", reasons
    # 用户验收口径：①赚回 token 钱 ②扣成本后年化 > 银行存款
    # 2026-11-25 起第②条改成「加权年化」（季 50 / 年 20 / 长期 30，各窗口折年化），
    # 加权分还没满最小样本（各自 <12 周）时退回老的整段净年化，避免拿 27 天噪声判档。
    bar_bad = []
    if net:
        if net.get("net_pnl") is not None and net["net_pnl"] < 0:
            bar_bad.append(f"累计净利润 {_f(net['net_pnl'])} 元 < 0：token 实花 "
                           f"（{_f(net.get('token_spent'))} 元）还没赚回来")
        multi = net.get("multi") or {}
        if multi.get("sample_ok") and multi.get("score_ann") is not None:
            if multi["score_ann"] <= net["deposit_rate"]:
                bar_bad.append(f"加权年化净收益率 {_pct(multi['score_ann'])} "
                               f"≤ 无风险基准 {_pct(net['deposit_rate'])}"
                               f"（{net.get('deposit_as_of') or '未标注口径'}；权重 季50/年20/长期30）")
        elif net.get("net_annualized") is not None and net["net_annualized"] <= net["deposit_rate"]:
            bar_bad.append(f"扣 token 实花后年化 {_pct(net['net_annualized'])} "
                           f"≤ 无风险基准 {_pct(net['deposit_rate'])}"
                           f"（{net.get('deposit_as_of') or '未标注口径'}）"
                           f"；净利润 {_f(net.get('net_pnl'))} 元")
        if bar_bad:
            return "未达标", bar_bad + ["（用户验收口径：净利润 ≥ 0 且 加权年化净收益率 > 无风险利率）"]
    notes = []
    if net:
        notes.append(f"净利润 {_f(net.get('net_pnl'))} 元（token 实花 {_f(net.get('token_spent'))} 元）"
                     f"｜净年化 {_pct(net.get('net_annualized'))} > 无风险基准 "
                     f"{_pct(net.get('deposit_rate'))}（{net.get('deposit_as_of') or '未标注口径'}）")
        multi = net.get("multi") or {}
        if multi.get("error"):
            notes.append(f"多周期加权：{multi['error']}")
        elif multi.get("sample_ok") and multi.get("score_ann") is not None:
            _parts = []
            for _k in ("quarter", "year", "long"):
                _t = (multi.get("terms") or {}).get(_k) or {}
                if _t.get("sample_ok"):
                    _parts.append(f"{_t.get('label') or _k} {_pct(_t.get('ann_rate'))}"
                                  f"（{int(_t.get('days') or 0)} 天"
                                  f"{'，自成立' if _t.get('inception') else ''}，"
                                  f"权重 {_t.get('weight_used', 0):.0%}）")
            notes.append(f"多周期加权（季50/年20/长期30，各窗口折年化）："
                         + "｜".join(_parts)
                         + f" → **加权年化 {_pct(multi.get('score_ann'))}**")
        else:
            notes.append(f"多周期加权：{multi.get('note') or '样本不足，只显示不判档'}")
        if net.get("multi_grade"):
            notes.append(f"多周期档位 **{net['multi_grade']}**（按加权年化净收益率判）")
        if net.get("grade"):
            notes.append(f"净收益率 {_pct(net.get('net_return'))} → 档位 **{net['grade']}**"
                         f"（按净收益率判，阈值语义不变）")
        # 防自欺：达标线是配置里的无风险利率（现值见下），利率越低达标越容易 →
        # 必须同时报绝对金额并说清「达标 ≠ 赚得多」
        notes.append(f"提示：基准已随无风险利率下调（现 {_pct(net.get('deposit_rate'))}），"
                     f"达标只代表跑赢无风险利率，不代表绝对金额可观"
                     f"（本账本净赚 {_f(net.get('net_pnl'))} 元 / 本金 {_f(net.get('capital'), 0)} 元）")
    if excess is not None and excess <= 0:
        notes.append(f"诊断：超额 {_pct(excess)} ≤ 0（本轮没跑赢{BENCH_NAME}，用户口径不强制）")
    if mdd is not None and mdd <= MAX_DD:
        notes.append(f"诊断：最大回撤 {_pct(mdd)} 超过 {MAX_DD:.0%} 的容忍线")
    if exp is not None and exp <= 0:
        notes.append(f"诊断：单笔期望 {exp:+.2f} 元 ≤ 0（胜率×盈亏比不成立）")
    if fee_ratio is not None and fee_ratio > MAX_FEE_RATIO:
        notes.append(f"诊断：累计费用占本金 {_pct(fee_ratio)} 偏高（>{MAX_FEE_RATIO:.0%}），注意摩擦成本")
    return "暂达标（跑赢存款）", notes


def risk_items(state: dict) -> list:
    """把账本持仓转成风险评审条目（weight = 占本金比例）。

    - 场内持仓：`state["holdings"]` 逐只；
    - 场外 R2 影子账：`state["otc_holdings"]` **逐只基金**计入，单只上限按每只基金校验。

    2026-11-14 A 类修复：原来把整条场外腿合成一个 `OTC:R2` 仓位，3 只各约 4.8% 的
    债券基金被误算成「单只 14.0% > R2 上限 12%」，记分卡出现假违规。
    """
    nav = float((paper.portfolio(state, {}) or {}).get("nav") or 0.0)
    items = []
    for code, h in (state.get("holdings") or {}).items():
        px = float(h.get("last_close") or h.get("cost") or 0.0)
        amt = float(h.get("shares") or 0) * px
        items.append({"code": code, "name": h.get("name"), "level": h.get("level") or "R5",
                      "weight": round((amt / nav) if nav else 0.0, 6)})
    import otc_book
    for code, h in (state.get(otc_book.HOLD_KEY) or {}).items():
        mv = otc_book.mv_of(h)
        if mv <= 0 or not nav:
            continue
        items.append({"code": code, "name": h.get("name"),
                      "level": h.get("level") or "R2",
                      "weight": round(mv / nav, 6)})
    return items


def one_ledger(name, led_cfg):
    path = led_cfg.get("state_file")
    if not path or not os.path.exists(path):
        return {"name": name, "label": led_cfg.get("label"), "error": f"账本不存在：{path}"}
    state = paper.load(path)
    st = paper.stats(state)
    win = window(state)
    p = paper.portfolio(state, {})
    bench = bench_return(state)
    total = st.get("total_return")
    excess = (total - bench) if (total is not None and bench is not None) else None
    mdd = st.get("max_drawdown")
    fee_ratio = st.get("fees_pct_of_capital")
    closed = st.get("closed_trades") or 0
    exp = expectancy(st, st.get("win_rate"), st.get("avg_win"), st.get("avg_loss"))
    real, float_ = kind_attribution(state)
    cash_yield_total = round(sum(float(e.get("amount") or 0.0)
                                 for e in (state.get("events") or [])
                                 if e.get("level") == "cash_yield"), 2)
    equity_pnl = (round(total * paper.contributed_capital(state) - cash_yield_total, 2)
                  if total is not None else None)
    net = net_metrics(state, st, win, led_cfg, token_cost=TOKEN_COST_OVERRIDE, ledger=name)
    label, reasons = judge(st, win, excess, mdd, fee_ratio, closed, exp, net=net)
    # 风险等级分布与加权等级（R1–R5，只做 R2/R3）
    try:
        import risk
        _items = risk_items(state)
        _rv = risk.portfolio_review(_items) if _items else None
        risk_dist = {
            "level": (_rv or {}).get("level"),
            "level_label": (_rv or {}).get("level_label"),
            "weights": (_rv or {}).get("weights") or {},
            "r2_share": (_rv or {}).get("r2_share"),
            "ok": (_rv or {}).get("ok"),
            "violations": (_rv or {}).get("violations") or [],
            "warnings": (_rv or {}).get("warnings") or [],
            "count": len(_items),
        }
    except Exception as exc:  # 评审失败不影响记分卡主结论，但要留痕
        risk_dist = {"error": f"风险评审失败：{exc}"}
    return {
        "name": name, "label": led_cfg.get("label"), "capital": led_cfg.get("capital"),
        "tier": led_cfg.get("tier"), "window": win, "nav": p.get("nav"),
        "total_return": total, "annualized": st.get("annualized"),
        "bench_return": bench, "excess": excess, "max_drawdown": mdd,
        "win_rate": st.get("win_rate"), "profit_factor": st.get("profit_factor"),
        "closed_trades": closed, "trades_total": st.get("trades_total"),
        "expectancy": exp, "fees_total": st.get("fees_total"), "fees_pct": fee_ratio,
        "exposure_avg": exposure_avg(state), "stop_outs": stop_outs(state),
        "realized_by_kind": real, "float_by_kind": float_,
        "cash_yield_total": cash_yield_total, "equity_pnl": equity_pnl,
        "net": net,
        "risk": risk_dist,
        "holdings": len(state.get("holdings") or {}),
        "pending": len(paper.pending_orders(state)),
        "verdict": label, "verdict_reasons": reasons,
        "state_file": path,
    }


_ANNUAL = False   # --annual：年报模式；默认按 config.PERF.grade_period 判（annual → 月报只出数字）


def _grade_period() -> str:
    return str((config.PERF or {}).get("grade_period") or "")


def _show_grade(annual=None) -> bool:
    """档位/奖励是否在本次记分卡里出（2026-11-26 用户拍板：计分放年报）。"""
    if annual is None:
        annual = _ANNUAL
    return bool(annual) if _grade_period() == "annual" else True


def to_md(rows, generated, annual=None):
    _show = _show_grade(annual)
    _dm = deposit_meta()            # 基准 + as_of，缺就抛错（不允许不标口径的判定）
    L = ["# 模拟盘记分卡（累计）", ""]
    L.append(f"生成时间：{generated}｜观察口径：满 {MIN_WEEKS} 周且平仓 ≥{MIN_CLOSED} 笔才判期望")
    L.append("")
    L.append(f"判定标准（2026-11-25 更新）：**净利润 = 账户累计收益（元）− token 实花（元）"
             f"；token 实花取 out/budget.json 该账本累计 spent（不是年度计提）"
             f"；①净利润 ≥ 0 ②**加权年化**净收益率 > 无风险利率基准 {_pct(_dm['rate'])}"
             f"（{_dm['as_of'] or '口径未标注'}）—— 加权口径 = 季度 50% / 年度 20% / 三年 30%，"
             f"各窗口先折成年化再加权（不足三年时长期项用「自成立」），不满 12 周的窗口只显示不判档"
             f"；档位按加权年化净收益率判，阈值语义不变**。"
             f"**主指标 = 本季累计净收益率**（vs 季达标线 = 年化达标线/4 = "
             f"{_pct(((config.PERF or {}).get('accept_quarter_rate')))}"
             f"{(lambda _c: f'，5 万本金 ≈ {_c:,.0f} 元/季')(50000 * float(((config.PERF or {}).get('accept_quarter_rate')) or 0))}"
             f"；token 季度预算 {_f((config.PERF or {}).get('token_cost_quarter_cny'))} 元）；"
             f"年化只作外推脚注、不作达标判据。"
             f"档位满 12 周才出（样本不足时印「—」，不拿 27 天噪声判能力）。"
             f"奖励评估期 2027-10-30 起评判，此前一律 0。**计分时点（2026-11-26 用户拍板）：档位/奖励只在年报（每年 1 月 1 日）判定，月报只报四项口径与季进度、不判档**（`config.PERF.grade_period`）。"
             f"旧四指标（超额/回撤/期望/费用）降级为诊断信息，不再一票否决。")
    L.append("")
    L.append(f"> 基准口径：{_dm['source'] or '未标注'}。**基准是达标线，不是收益预测**；"
             f"无风险利率下行会把达标线一起压低，所以达标 ≠ 绝对金额可观，下表同时给净赚金额。")
    L.append("")
    ok = [r for r in rows if "error" not in r]
    L.append("| 账本 | 本金 | 净值 | 账户收益(累计) | 年化 | token实花 | 净利润 | 净收益率 | 本季净收益 | 档位 | 净年化 | 无风险基准 | 最大回撤 | 同期" + BENCH_NAME + " | 超额 | 平仓 | 胜率 | 盈亏比 | 单笔期望 | 费用占比 | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in ok:
        _nt = r.get("net") or {}
        _q = _nt.get("quarter") or {}
        L.append("| {label} | {cap} | {nav} | {tot} | {ann} | {tok} | {npnl} | {nret} | {qret} | {grade} | {nann} | {dep} | {mdd} | {bench} | {exc} | {closed} | {wr} | {pf} | {exp} | {fee} | **{vd}** |".format(
            label=r["label"], cap=_f(r["capital"], 0), nav=_f(r["nav"]),
            tot=_pct(r["total_return"]), ann=_pct(r["annualized"]),
            tok=_f(_nt.get("token_spent")), npnl=_f(_nt.get("net_pnl")),
            nret=_pct(_nt.get("net_return")),
            qret=_pct(_q.get("net_return")) if _q.get("available") else "n/a",
            grade=(((_nt.get("grade") or "n/a") if _nt.get("multi_sample_ok") else "—") if _show else "—"),
            nann=_pct(_nt.get("net_annualized")),
            dep=_pct(_nt.get("deposit_rate")),
            mdd=_pct(r["max_drawdown"]), bench=_pct(r["bench_return"]), exc=_pct(r["excess"]),
            closed=r["closed_trades"], wr=_pct(r["win_rate"], 1) if r["win_rate"] is not None else "n/a",
            pf=_f(r["profit_factor"]), exp=_f(r["expectancy"]),
            fee=_pct(r["fees_pct"]), vd=r["verdict"]))
    L.append("")
    for r in rows:
        if "error" in r:
            L.append(f"## {r['label'] or r['name']}：{r['error']}")
            L.append("")
            continue
        w = r["window"]
        L.append(f"## {r['label']}（{r['name']}）")
        L.append("")
        L.append(f"- 观察窗：{w['base']} → {w['last']}，{w['weeks']} 周 / {w['days']} 个交易日结算点"
                 f"｜本金 {_f(r['capital'], 0)} 元｜档位 {r['tier']}")
        L.append(f"- 判定：**{r['verdict']}**")
        for x in r["verdict_reasons"]:
            L.append(f"  - {x}")
        nt = r.get("net") or {}
        q = nt.get("quarter") or {}
        _yrs = nt.get("years")
        _yrs_txt = f"{_yrs:.2f}" if _yrs is not None else "n/a"
        # 档位只在样本满 12 周后出（B 类 #22③）：不然 27 天的噪声会直接印成「D 档」，
        # 读者会把它当成对能力的判决，而样本闸本身已经说明了「还不能判断」。
        if not _show:
            _grade_txt = "—（年度评分：每年 1 月 1 日年报定档）"
        elif not nt.get("multi_sample_ok"):
            _grade_txt = "—（样本不足，满 12 周才出档）"
        else:
            _grade_txt = (nt.get("grade") or "n/a")
        L.append(f"- 验收（2026-11-07口径）：账户收益 **{_f(nt.get('gross_pnl'))} 元**"
                 f"（{_pct(r['total_return'])}）｜token 实花 **{_f(nt.get('token_spent'))} 元**"
                 f"（{nt.get('token_cost_basis') or 'budget.json'}，`--token-cost` 可覆盖）"
                 f"｜净利润 **{_f(nt.get('net_pnl'))} 元**｜净收益率 **{_pct(nt.get('net_return'))}**"
                 f" → 档位 **{_grade_txt}**")
        # 主指标：本季累计净收益率（季度计价，B 类 #22②）；年化降级为脚注外推值
        if q.get("available"):
            _qflag = "✓ 超过季目标" if q.get("bar_ok") else "✗ 低于季目标"
            L.append(f"- 本季 **{q.get('quarter')}**（{q.get('start')} → {q.get('end')}，"
                     f"{q.get('days')} 天{'，自建账起' if q.get('inception') else ''}）"
                     f"累计净收益率 **{_pct(q.get('net_return'))}**"
                     f"（含账户 {_pct(q.get('gross_return'))} − token "
                     f"{_f(q.get('token_cost_cny'))} 元）vs 季目标 "
                     f"{_pct(q.get('target_rate'))}"
                     f"（{_f(q.get('target_cny'))} 元）→ {_qflag}"
                     f"（超额 {_f(q.get('excess_cny'))} 元）")
            _tb = q.get("token_quarter_budget_cny")
            if _tb:
                L.append(f"  - 本季 token：实花 {_f(q.get('token_cost_cny'))} 元 / 季度预算 "
                         f"{_f(_tb)} 元（{_pct(q.get('token_budget_used_pct'))}）")
        elif q.get("note"):
            L.append(f"- 本季：{q.get('note')}")
        L.append(f"  - 净年化 **{_pct(nt.get('net_annualized'))}**（外推值，不作达标判据；动态本金下年报定档用「各月等价年化率的算术平均」，见下）vs 无风险基准 "
                 f"{_pct(nt.get('deposit_rate'))}（{nt.get('deposit_as_of') or '口径未标注'}）"
                 f" → {'✓ 达标' if nt.get('bar_ok') else '✗ 未达标'}")
        if not _show:
            L.append("  - 奖励：—（年度评分后才计提；月报不判档）")
        elif nt.get("reward_pending"):
            L.append(f"  - 奖励：{nt.get('reward_note') or '未到评估期（2027-10-30 起）'}")
        else:
            L.append("  - 奖励：评估期已到，可按档位计提（见 score.py）")
        if nt.get("bar_ok"):
            # 防自欺：达标线随配置的无风险利率移动，达标变容易时须同时报绝对金额
            L.append(f"  - 提醒：基准已随无风险利率下调（现 {_pct(nt.get('deposit_rate'))}），"
                     f"达标只代表跑赢无风险利率，不代表绝对金额可观 —— 本账本净赚 "
                     f"**{_f(nt.get('net_pnl'))} 元** / 本金 {_f(nt.get('capital'), 0)} 元")
        if nt.get("token_cost_basis"):
            L.append(f"  - token 成本依据：{nt['token_cost_basis']}")
        L.append(f"- 绩效：净值 {_f(r['nav'])}，累计 {_pct(r['total_return'])}，年化 {_pct(r['annualized'])}，"
                 f"最大回撤 {_pct(r['max_drawdown'])}，同期{BENCH_NAME} {_pct(r['bench_return'])}，超额 {_pct(r['excess'])}")
        L.append(f"- 交易：成交 {r['trades_total'] or 0} 笔（平仓 {r['closed_trades']} 笔），"
                 f"胜率 {_pct(r['win_rate'], 1) if r['win_rate'] is not None else 'n/a'}，"
                 f"盈亏比 {_f(r['profit_factor'])}，单笔期望 {_f(r['expectancy'])} 元，"
                 f"止损/熔断出场 {r['stop_outs']} 次")
        L.append(f"- 成本：累计费用 {_f(r['fees_total'])} 元（本金 {_pct(r['fees_pct'])}）｜平均仓位 {_pct(r['exposure_avg'], 1)}")
        cy = r.get("cash_yield_total") or 0.0
        eq = r.get("equity_pnl")
        L.append(f"- 拆开看（不许只报总收益）：现金层累计利息 **{_f(cy)} 元**"
                 f"（逆回购 + 货基）｜权益层盈亏 **{_f(eq)} 元**（含已实现 + 浮动 − 费用）"
                 f"｜两者之和 = 累计 {_pct(r['total_return'])}")
        if eq is not None and eq <= 0 and cy:
            _eq_note = equity_warning(r.get("total_return"), eq, cy)
            if _eq_note:
                L.append(_eq_note)
        rk = r["realized_by_kind"] or {}
        fk = r["float_by_kind"] or {}
        kinds = sorted(set(rk) | set(fk))
        if kinds:
            seg = "；".join(f"{'ETF' if k == 'etf' else '个股'} 已实现 {_f(rk.get(k, 0))} / 浮动 {_f(fk.get(k, 0))}" for k in kinds)
            L.append(f"- 归因：{seg}")
        L.append(f"- 账本：{r['state_file']}｜当前持仓 {r['holdings']} 只｜未成交挂单 {r['pending']} 张")
        rk = r.get("risk") or {}
        if rk.get("error"):
            L.append(f"- 风险等级：{rk['error']}")
        elif rk.get("count"):
            _w = rk.get("weights") or {}
            _dist = "｜".join(f"{k} {_pct(v, 1)}" for k, v in sorted(_w.items()))
            L.append(f"- 风险等级：组合 **{rk.get('level_label')}**"
                     f"（R{float(rk.get('level') or 0):.2f}）｜{_dist}"
                     f"｜R2 占比 {_pct(rk.get('r2_share'), 1)}"
                     f"｜{'✓ 合规' if rk.get('ok') else '✗ 有违规：' + '；'.join(rk.get('violations') or [])}")
        else:
            L.append("- 风险等级：当前空仓（现金层 R1）")
        L.append("")
    L.append("## 下一步（不自动改规则）")
    L.append("")
    labels = {r["verdict"] for r in ok}
    if not ok:
        L.append("账本都还没建好，先跑 `weekly.py --ledger <账本> --init`。")
    elif labels == {"样本不足"}:
        L.append("样本不足：继续按每周三、周六 20:30 决策跑下去，账本会在下次决策自动结算"
                 "（卖出按开盘价、买入按限价单触价）；"
                 "样本满 12 周/10 笔平仓后按「净利润 ≥ 0 且 加权年化净收益率 > 无风险利率基准」判定，"
                 "不要因为一两周涨跌改规则。")
    elif "未达标" in labels:
        L.append("有账本未达标：先看是「token 还没赚回」还是「加权年化没跑过无风险基准」——"
                 "前者需要利润或时间，后者要降低换手/提高有效仓位；"
                 "**改任何阈值都必须走一次 git 提交并在 engine-notes.md §六 留纪律**，说明依据是分布/复盘统计，不是某只标的的输赢。")
    else:
        L.append("两条验收线暂时全过（净利润 ≥ 0，加权年化 > 无风险利率基准）：这只是起点，"
                 "继续跑到 6 个月以上，确认不是靠一两次运气；同时看旧四指标是否也转好。"
                 "注意基准已随无风险利率降到 1 年期国债水平，达标门槛随之变低，"
                 "请以上面的「净赚金额 / 本金」判断是否真的值得继续。")
    L.append("")
    L.append("边界：模拟盘、非投资建议、不构成任何收益承诺；判定只描述历史账本，不预测未来。")
    return "\n".join(L) + "\n"


def equity_warning(total_return, equity_pnl, cash_yield):
    """权益层为负时的提示文案（按总收益符号分支）；无需提示 → None。

    2026-11-14 A 类修复：原文案假定「总收益为正」，累计收益为负时会给出误导结论。
    单独成函数是为了让 `_selftest_review.py` 能直接锁住两个分支。
    """
    if equity_pnl is None or equity_pnl > 0 or not cash_yield:
        return None
    if total_return is not None and total_return > 0:
        return ("  - **注意**：总收益为正但权益层为负 —— 现在的正收益来自闲置现金吃利息，"
                "不是策略本身在赚钱（回测结论同此，见 SKILL.md §8.7）。")
    return ("  - **注意**：累计收益为负且权益层为负 —— 亏损来自权益持仓，"
            "现金利息只能部分对冲（不是策略在赚钱；回测结论同此，见 SKILL.md §8.7）。")


def main(argv=None):
    ap = argparse.ArgumentParser(description="模拟盘账本累计记分卡")
    ap.add_argument("--ledger", default="all", help="账本名 / all")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--token-cost", type=float, default=None,
                    help="覆盖 token 累计实花（元，默认取 out/budget.json 该账本 spent_cny）；"
                         "按实际账单填，别用 0 假装免费")
    ap.add_argument("--deposit-rate", type=float, default=None,
                    help="覆盖无风险利率基准（年化小数，默认取 config.PERF.deposit_rate = 1 年期国债；"
                         "覆盖后报告里的 as_of 会标成「命令行覆盖」，别忘同步 config）")
    ap.add_argument("--annual", action="store_true",
                    help="年报模式：出档位/奖励（默认按 config.PERF.grade_period=annual → 月报不出档位，--strict 也不因未达标失败）")
    ap.add_argument("--strict", action="store_true",
                    help="有账本「未达标」时退出码 1（样本不足不算失败；供 decide.py / 定时任务用）")
    args = ap.parse_args(argv)
    global _ANNUAL
    _ANNUAL = bool(args.annual)
    global TOKEN_COST_OVERRIDE
    if args.token_cost is not None:
        TOKEN_COST_OVERRIDE = float(args.token_cost)
    if args.deposit_rate is not None:
        PERF["deposit_rate"] = float(args.deposit_rate)
        PERF["deposit_rate_as_of"] = "命令行覆盖（--deposit-rate）"
        PERF["deposit_rate_source"] = "命令行覆盖（--deposit-rate），未同步 config.PERF"

    leds = CFG.get("ledgers") or {"50k": {"capital": CFG.get("capital"), "tier": "B",
                                         "state_file": CFG["state_file"], "label": "5 万"}}
    names = sorted(leds) if args.ledger == "all" else [args.ledger]
    rows = [one_ledger(n, leds[n]) for n in names if n in leds]
    os.makedirs(OUT, exist_ok=True)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = to_md(rows, generated, annual=args.annual)
    md_path = os.path.join(OUT, "scorecard.md")
    json_path = os.path.join(OUT, "scorecard.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(md)
    print(f"[review] 记分卡：{md_path}")
    print(f"[review] 机器可读：{json_path}")
    if args.strict and _show_grade(args.annual) and any(str(r.get("verdict") or "").startswith("未达标") for r in rows):
        print("[review] ✗ 有账本未达标（--strict → 退出码 1；账本不会被改）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
