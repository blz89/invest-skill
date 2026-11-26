# -*- coding: utf-8 -*-
"""评分与奖励：把「赚了多少」翻译成 A/B+/B/B-/C/D 和奖励金额。

口径（写死，改之前先读 config.SCORE 的注释）：

    净利润   = 账户累计收益（元） − token 实花（元）
    净收益率 = 净利润 / 本金                     ← 评分用这个
    奖励     = 净利润 × 档位比例                 ← 从本金划出，全部转次年 token 预算（奖励即 token）
    实际留存 = 净利润 − 奖励
    赚到的钱继续投资（本金复利），奖励按当年净利润算。

**token 实花**取 out/budget.json 里该账本累计 spent（不是年度计提、不是本次窗口）；
调用方（team.py T8 / review.py）负责把 budget 的 spent 传进来。

奖励评估期：基准日 2026-10-28 起满 12 个月 = **2027-10-30** 才评判奖励。
未到评估期：档位照常给（按净收益率），但 reward 一律 0，文案「评估期未到（2027-10-30 起评判）」。

两条护栏：
    1. 评分是结果不是目标：单次评分是运气，12 期分布才是能力（dist_target）。
    2. 风险否决：净收益达标但回撤过大 → 降档 B+、奖励减半，防止「赌一把拿 A」。

用法：
    python score.py --capital 50000 --return 0.035 --token-cost 4.91 --mdd -0.08
    python score.py --capital 50000 --return 0.035 --token-cost 4.91 --as-of 2027-11-01
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

import config

SC = config.SCORE

_GRADE_ORDER = ["A", "B+", "B", "B-", "C", "D"]


def as_date(as_of=None) -> date:
    """把 as_of（date/datetime/'YYYY-MM-DD'/ISO）归一成 date；None = 今天。"""
    if as_of is None:
        return date.today()
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    s = str(as_of).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(s[:10])


def eval_start_date() -> date:
    """奖励评估起点：config.SCORE.reward_eval_start（2027-10-30）。"""
    return as_date(SC.get("reward_eval_start") or "2027-10-30")


def reward_period_open(as_of=None) -> bool:
    """评估期是否已到（未到则奖励冻结为 0）。"""
    return as_date(as_of) >= eval_start_date()


def token_cost_rate(token_cost_cny: float, capital: float) -> float:
    """token 成本率 = token 花费 / 本金。本金必须为正。"""
    if capital <= 0:
        raise ValueError("capital 必须为正数")
    return float(token_cost_cny) / float(capital)


def net_return(total_return: float, token_cost_cny: float, capital: float) -> float:
    """净收益率 = 组合收益率 − token 成本率。"""
    return float(total_return) - token_cost_rate(token_cost_cny, capital)


def grade_of(net_rate: float) -> str:
    """按 config.SCORE.grade_lines 打分。下界含、上界不含。"""
    lines = SC["grade_lines"]
    if net_rate >= lines["A"]:
        return "A"
    if net_rate >= lines["B+"]:
        return "B+"
    if net_rate >= lines["B"]:
        return "B"
    if net_rate >= lines["B-"]:
        return "B-"
    if net_rate >= 0:
        return "C"
    return "D"


def _veto_check(grade: str, mdd: float | None) -> tuple[bool, str]:
    """风险否决：A 档且回撤深于 SCORE.drawdown_veto → 降档 B+、奖励减半。

    mdd 用负数表示（-0.25 = 回撤 25%），与技能内部口径一致。
    """
    if grade != "A":
        return False, ""
    if mdd is None:
        return False, ""
    limit = -abs(float(SC["drawdown_veto"]))
    if float(mdd) < limit:
        return True, (f"风险否决：净收益达 A 但最大回撤 {float(mdd):.2%} 深于 "
                      f"{limit:.2%} → 降档 {SC['drawdown_veto_grade']}、奖励减半")
    return False, ""


def evaluate(total_return: float, token_cost_cny: float, capital: float,
             mdd: float | None = None, as_of=None, days: float | None = None,
             net_profit: float | None = None,
             annual_rate_override: float | None = None) -> dict:
    """一次完整评估：返回评分、奖励、留存、下一步预算等全部中间量。

    ``token_cost_cny`` 是 **token 实花**（budget.json 累计 spent），不是年度计提。
    ``as_of`` < 2027-10-30 时奖励冻结为 0（reward_pending=True），档位照常按净收益率给。
    ``days`` 给定时按**等价年化净收益率**定档（年报结算用：不足一年也能公平比档位），
    奖励金额仍取 ``net_profit``（当年净利润）——不按年化后的理论值发钱。

    返回字段（都能直接进报表，不要在下游重算）：
        capital, total_return, gross_profit, token_cost_cny, token_cost_rate,
        net_rate, net_profit, grade, grade_before_veto, vetoed, veto_reason,
        reward_pct, reward_cny, reward_to_budget_cny, reward_to_ai_cny,
        kept_cny, kept_rate, next_budget_cny,
        reward_pending, reward_note, eval_start, eval_as_of
    """
    cap = float(capital)
    tr = float(total_return)
    cost = float(token_cost_cny)
    tcr = token_cost_rate(cost, cap)
    nr = tr - tcr
    net_given = net_profit
    net_profit = cap * nr if net_given is None else float(net_given)
    gross_profit = cap * tr

    # 年报结算：不足一年也按等价年化净收益率定档；奖励仍按当年净利润（不是年化理论值）。
    net_rate_ann = None
    grade_basis = nr
    if annual_rate_override is not None and float(annual_rate_override) > -1.0:
        # 动态本金（2026-11-26）：年报定档 = 各月等价年化率的算术平均，整段/加权年化只作参考
        net_rate_ann = float(annual_rate_override)
        grade_basis = net_rate_ann
    elif days and float(days) >= 1.0 and (1.0 + nr) > 0.0:
        net_rate_ann = (1.0 + nr) ** (365.0 / float(days)) - 1.0
        grade_basis = net_rate_ann
    grade = grade_of(grade_basis)
    vetoed, veto_reason = _veto_check(grade, mdd)
    grade_final = SC["drawdown_veto_grade"] if vetoed else grade

    pct = float(SC["reward_pct"].get(grade_final, 0.0))
    reward = net_profit * pct
    if vetoed:
        reward *= float(SC["drawdown_veto_reward_scale"])
    if reward <= 0:          # <= 0 而不是 < 0：负利润 × 0% 会得到 -0.0，打印成 "-0.00 元"
        reward = 0.0

    as_d = as_date(as_of)
    pending = as_d < eval_start_date()
    if pending:              # 评估期未到：奖励冻结（档位照常给）
        reward = 0.0

    split = float(SC["reward_split_to_budget"])
    to_budget = reward * split
    to_ai = reward - to_budget
    kept = net_profit - reward

    base = float(SC["budget"].get(_ledger_key(cap), 150.0))

    return {
        "capital": cap,
        "total_return": tr,
        "gross_profit": gross_profit,
        "token_cost_cny": cost,
        "token_cost_rate": tcr,
        "net_rate": nr,
        "net_rate_ann": net_rate_ann,
        "grade_basis": grade_basis,
        "annualized": net_rate_ann is not None,
        "annual_metric": ("monthly_mean_equiv" if annual_rate_override is not None else
                          ("api_days" if net_rate_ann is not None else "none")),
        "net_profit": net_profit,
        "grade": grade_final,
        "grade_before_veto": grade,
        "vetoed": vetoed,
        "veto_reason": veto_reason,
        "reward_pct": pct,
        "reward_cny": reward,
        "reward_split_to_budget": split,
        "reward_to_budget_cny": to_budget,
        "reward_to_ai_cny": to_ai,
        "kept_cny": kept,
        "kept_rate": kept / cap if cap else 0.0,
        "next_budget_cny": base + to_budget,
        "reward_pending": pending,
        "reward_note": (str(SC.get("reward_eval_note")
                            or "评估期未到（2027-10-30 起评判）") if pending else ""),
        "eval_start": eval_start_date().isoformat(),
        "eval_as_of": as_d.isoformat(),
    }


# ============================================================================
# 多周期加权评分（2026-11-25 用户拍板）
# ============================================================================
# 用户口径（m00796）：「引入新算法，季度收益 50%，年度收益 20%，三年收益 30%，
# 不够三年计算到创始」；补一句（m00799）「都要折算成年化率」。
# 于是三项**全部先折算成年化收益率**再加权 —— 这一步是必须的：季度 +1.0% 与年度
# +3.0% 不是同一个量纲，直接加权会把季度结构性压扁（同样「一年 3%」的水平，
# 三个月的数只值 0.75%）。折算成年化后，「加权年化收益率 ≥ 定存 1.30%」与
# 「加权年化超额 ≥ 0」是同一个判据（因为权重和为 1），不必二选一。
# 窗口默认**滚动**（12 周 / 52 周 / 3 年）：账本 2026-10-28 起步，若用自然季度，
# 2026Q4 只剩 9 周、永远凑不满最小样本，前几期只能全是「样本不足」。


def annualize_rate(ret: float | None, days: float | None) -> float | None:
    """累计收益率 → 年化收益率。天数 <1 或 1+ret ≤ 0 时返回 None（不编数）。"""
    if ret is None or days is None:
        return None
    try:
        r, d = float(ret), float(days)
    except (TypeError, ValueError):
        return None
    if d < 1.0 or (1.0 + r) <= 0.0:
        return None
    return (1.0 + r) ** (365.0 / d) - 1.0


def composite(windows: dict, deposit_rate: float | None = None) -> dict:
    """多周期加权：季 50% / 年 20% / 长期 30%，各窗口先折年化再加权。

    ``windows`` = {"quarter": {"net_return": 累计净收益率, "days": 天数,
                   "inception": 是否「计算到创始」}, "year": {...}, "long": {...}}
    ``net_return`` 必须是**净**口径（账户收益 − 窗口内 token 实花，再 / 本金）。
    未满 ``config.PERF.min_window_days`` 的窗口只显示、不参与加权（权重按比例
    重分配给已满样本的窗口）；一个都不满 → score_ann=None，明确「样本不足，不判档」。
    """
    perf = getattr(config, "PERF", {}) or {}
    weights = dict(perf.get("period_weights")
                   or {"quarter": 0.50, "year": 0.20, "long": 0.30})
    min_days = float(perf.get("min_window_days") or 84.0)
    dep = float(deposit_rate if deposit_rate is not None else (perf.get("deposit_rate") or 0.0))

    terms = {}
    ok_keys = []
    for key, w in weights.items():
        raw = (windows or {}).get(key) or {}
        nr, dys = raw.get("net_return"), raw.get("days")
        ann = annualize_rate(nr, dys)
        days_ok = dys is not None and float(dys) >= min_days
        sample_ok = bool(ann is not None and days_ok)
        terms[key] = {
            "net_return": None if nr is None else float(nr),
            "days": None if dys is None else float(dys),
            "inception": bool(raw.get("inception")),
            "ann_rate": ann,
            "weight_nominal": float(w),
            "weight_used": 0.0,
            "sample_ok": sample_ok,
            "min_days": min_days,
        }
        if sample_ok:
            ok_keys.append(key)

    avail = sum(terms[k]["weight_nominal"] for k in ok_keys)
    score_ann = None
    if ok_keys and avail > 0:
        score_ann = 0.0
        for k in ok_keys:
            terms[k]["weight_used"] = terms[k]["weight_nominal"] / avail
            score_ann += terms[k]["weight_used"] * terms[k]["ann_rate"]

    excess = None if score_ann is None else score_ann - dep
    return {
        "terms": terms,
        "weights": {k: float(v) for k, v in weights.items()},
        "min_window_days": min_days,
        "deposit_rate": dep,
        "score_ann": score_ann,
        "excess_ann": excess,
        "sample_ok": bool(ok_keys),
        "used_windows": ok_keys,
        "missing_windows": [k for k in weights if k not in ok_keys],
        "grade": grade_of(score_ann) if score_ann is not None else None,
        "note": ("样本不足：季/年/长期都还没满最小窗口（%.0f 天），加权分只显示不判档"
                 % min_days) if not ok_keys else
                ("" if not [k for k in weights if k not in ok_keys] else
                 "部分窗口未满最小样本，权重已按比例重分配给已满窗口"),
        "basis": "各窗口净收益率先折年化，再按 季50%/年20%/长期30% 加权（权重和=1）",
    }


def windows_from_nav(navs: list, as_of=None, runs: list | None = None,
                     capital: float | None = None, flows: list | None = None,
                     token_in_nav: bool | None = None) -> dict:
    """从账本净值点 + budget.json 的 runs 生成 composite() 需要的三个窗口。

    - 窗口起点 = as_of − config.PERF.period_windows[key] 天；若账本起点更晚，
      就退到账本起点并标 ``inception=True``（= 用户说的「不够三年计算到创始」）。
    - 净收益率 = nav_end/nav_start − 1 − 窗口内 token 实花 / 本金。
    - 窗口内 token 实花按 runs 的 ``ended_at``（缺则 ``started_at``）落在区间内求和。
    """
    perf = getattr(config, "PERF", {}) or {}
    spans = dict(perf.get("period_windows")
                 or {"quarter": 84, "year": 364, "long": 1095})
    end_d = as_date(as_of)
    pts = sorted(((as_date(n.get("date")), float(n.get("nav") or 0.0))
                  for n in (navs or []) if n.get("date") and n.get("nav") is not None),
                 key=lambda x: x[0])
    if not pts:
        return {}
    cap = float(capital or 0.0)
    out = {}
    for key, span in spans.items():
        start_d = end_d - __import__("datetime").timedelta(days=int(span))
        prev = [p for p in pts if p[0] <= start_d]
        inception = False
        if prev:
            s_d, s_nav = prev[-1]
        else:
            s_d, s_nav = pts[0]
            inception = True
        last = [p for p in pts if p[0] <= end_d]
        if not last or s_nav <= 0:
            continue
        e_d, e_nav = last[-1]
        days = max((e_d - s_d).days, 0)
        # A1：注资是现金流不是收益——窗口末值先减掉窗口内到账的注资
        _flow = 0.0
        for _f in (flows or []):
            try:
                _fd = as_date((_f or {}).get("date"))
                _fa = float((_f or {}).get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
            if s_d < _fd <= e_d:
                _flow += _fa
        gross = (e_nav - _flow) / s_nav - 1.0
        tok = _token_in_window(runs, s_d, e_d)
        # A5：token 已从现金扣掉（nav 净口径）时不能再减一次
        _tinv = bool(token_in_nav)          # 默认 False：调用方负责声明「token 已在净值里」
        tcr = 0.0 if _tinv else ((tok / cap) if cap > 0 else 0.0)
        out[key] = {"net_return": gross - tcr, "days": float(days),
                    "inception": inception, "gross_return": gross,
                    "flow": round(_flow, 2), "token_in_nav": _tinv,
                    "start": s_d.isoformat(), "end": e_d.isoformat(),
                    "token_cost_cny": round(tok, 6), "token_cost_rate": tcr}
    return out


def evaluate_multi(windows: dict, deposit_rate: float | None = None) -> dict:
    """composite() 的一行式封装：直接可进报表（字段名与 evaluate() 对齐）。"""
    res = composite(windows, deposit_rate=deposit_rate)
    res["bar_ok"] = bool(res["sample_ok"] and res["score_ann"] is not None
                         and res["score_ann"] > res["deposit_rate"])
    return res


def _token_in_window(runs: list | None, start_d, end_d) -> float:
    """窗口内 token 实花：按 runs 的 ``ended_at``（缺则 ``started_at``）落在 (start, end] 内求和。

    单独抽出来是因为「滚动窗口」和「自然季」两处都要用同一套落窗规则，各写一遍必然漂移。
    """
    tok = 0.0
    for r in (runs or []):
        when = str(r.get("ended_at") or r.get("started_at") or "")[:10]
        if not when:
            continue
        try:
            w_d = as_date(when)
        except ValueError:
            continue
        if start_d < w_d <= end_d:
            tok += float(r.get("cost_cny") or 0.0)
    return tok


def quarter_metrics(navs: list, as_of=None, runs: list | None = None,
                    capital: float | None = None, deposit_rate: float | None = None,
                    flows: list | None = None, token_in_nav: bool | None = None) -> dict:
    """**本自然季**累计（B-10② 的主指标）：季初 → as_of，口径与滚动窗口完全一致。

    为什么主指标用「本季累计净收益率」而不是年化：5 万本金、往返摩擦 0.46%，
    一个季度目标才 0.325%（162.5 元）——年化会把 27 天的噪声放大成 −4% 那种数字，
    读者会以为策略一年要亏 4 个点。年化保留，但只作「外推值」脚注，不作达标判据。

    - 起点 = 自然季首日；账本起步晚于季首就退到账本首个净值点并标 ``inception=True``
      （首季只剩 9 周，永远凑不满 12 周最小样本，所以逐季滚动看的是「这一季拿了多少」，
      而 12 周最小样本闸只管「判不判档」，两者不是一回事）。
    - 净收益率 = nav_end/nav_start − 1 − 季内 token 实花 / 本金。
    - 目标 = ``config.PERF.accept_quarter_rate``（= 年化达标线 / 4）× 本金。
    """
    perf = getattr(config, "PERF", {}) or {}
    dep = float(deposit_rate if deposit_rate is not None else (perf.get("deposit_rate") or 0.0))
    target_rate = perf.get("accept_quarter_rate")
    if target_rate is None:
        target_rate = dep / 4.0
    target_rate = float(target_rate)

    end_d = as_date(as_of)
    pts = sorted(((as_date(n.get("date")), float(n.get("nav") or 0.0))
                  for n in (navs or []) if n.get("date") and n.get("nav") is not None),
                 key=lambda x: x[0])
    if not pts:
        return {"available": False, "note": "账本还没有净值点"}
    last = [p for p in pts if p[0] <= end_d]
    if not last:
        return {"available": False, "note": f"{end_d.isoformat()} 之前没有净值点"}
    e_d, e_nav = last[-1]
    q_start = e_d.replace(month=((e_d.month - 1) // 3) * 3 + 1, day=1)
    prev = [p for p in pts if p[0] <= q_start]
    inception = False
    if prev:
        s_d, s_nav = prev[-1]
    else:
        s_d, s_nav = pts[0]
        inception = True
    if s_nav <= 0:
        return {"available": False, "note": "季初净值为 0，无法算收益率"}
    days = max((e_d - s_d).days, 0)
    _flow = 0.0
    for _f in (flows or []):
        try:
            _fd = as_date((_f or {}).get("date"))
            _fa = float((_f or {}).get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        if s_d < _fd <= e_d:
            _flow += _fa
    gross = (e_nav - _flow) / s_nav - 1.0
    cap = float(capital or 0.0)
    tok = _token_in_window(runs, s_d, e_d)
    _tinv = bool(token_in_nav)          # 默认 False：调用方负责声明「token 已在净值里」
    tcr = 0.0 if _tinv else ((tok / cap) if cap > 0 else 0.0)
    net_return = gross - tcr
    target_cny = (cap * target_rate) if cap > 0 else None
    return {
        "available": True,
        "quarter": f"{e_d.year}Q{(e_d.month - 1) // 3 + 1}",
        "start": s_d.isoformat(), "end": e_d.isoformat(), "days": days,
        "inception": inception, "period": str(perf.get("period") or "quarter"),
        "gross_return": gross, "token_cost_cny": round(tok, 6), "token_cost_rate": tcr,
        "net_return": net_return,
        "net_annualized_extrapolated": annualize_rate(net_return, days),
        "target_rate": target_rate,
        "target_cny": None if target_cny is None else round(target_cny, 2),
        "excess_return": net_return - target_rate,
        "excess_cny": None if target_cny is None else round(cap * (net_return - target_rate), 2),
        "bar_ok": bool(net_return >= target_rate),
        "basis": "本季累计净收益率 =（季末净值/季初净值 − 1）− 季内 token 实花/本金；"
                 "目标 = 年化达标线/4；年化值仅作外推参考",
    }


def _ledger_key(capital: float) -> str:
    """本金 → 预算档位键（本部署 5 万）。取最接近且不超过的档。"""
    budgets = SC["budget"]
    best = None
    for k, v in budgets.items():
        cap_k = float(k.rstrip("k")) * 1000.0 if k.endswith("k") else float(v)
        if float(capital) >= cap_k - 1.0:
            if best is None or cap_k > float(best[0].rstrip("k")) * 1000.0:
                best = (k, v)
    if best:
        return best[0]
    return sorted(budgets, key=lambda x: float(x.rstrip("k")) if x.endswith("k") else 0)[0]


def dist_eval(grades: list[str]) -> dict:
    """12 期分布考核：单次评分是运气，分布才是能力。"""
    from collections import Counter
    cnt = Counter(grades)
    got = {g: cnt.get(g, 0) for g in _GRADE_ORDER}
    target = SC["dist_target"]
    n = len(grades)

    # D 必须为 0；C 不超过上限；B 及以上合计不低于目标合计
    d_ok = got["D"] == 0
    c_ok = got["C"] <= int(target["C"])
    good = got["B"] + got["B+"] + got["A"]
    good_target = int(target["B"]) + int(target["B+"]) + int(target["A"])
    # 按比例缩放目标（期数不等于 12 时）
    scale = n / 12.0 if n else 1.0
    good_target_s = good_target * scale
    good_ok = good >= good_target_s

    return {
        "n": n,
        "count": got,
        "target": target,
        "d_ok": d_ok,
        "c_ok": c_ok,
        "good_count": good,
        "good_target_scaled": good_target_s,
        "good_ok": good_ok,
        "pass": bool(d_ok and c_ok and good_ok),
    }


def format_report(ev: dict) -> str:
    """人读的一段汇报文本（周报里直接用）。"""
    gross = ev.get("gross_profit", ev["capital"] * ev["total_return"])
    if ev.get("reward_pending"):
        reward_line = f"奖励：{ev.get('reward_note') or '评估期未到（2027-10-30 起评判）'}"
    elif float(ev.get("reward_split_to_budget", 1.0)) >= 0.9999:
        reward_line = (f"奖励 {ev['reward_cny']:.2f} 元（{ev['reward_pct']:.0%}）"
                       f"= 全部转次年 token 预算")
    else:
        reward_line = (f"奖励 {ev['reward_cny']:.2f} 元（{ev['reward_pct']:.0%}）"
                       f"= 转预算 {ev['reward_to_budget_cny']:.2f} + 归 AI {ev['reward_to_ai_cny']:.2f}")
    lines = [
        f"本金 {ev['capital']:.0f} 元 | 账户收益 {gross:.2f} 元（{ev['total_return']:.2%}）"
        f" | token 实花 {ev['token_cost_cny']:.2f} 元（{ev['token_cost_rate']:.3%}）",
        f"净利润 {ev['net_profit']:.2f} 元 | 净收益率 {ev['net_rate']:.2%} → 评分 **{ev['grade']}**"
        + (f"（原 {ev['grade_before_veto']}，{ev['veto_reason']}）" if ev["vetoed"] else ""),
        reward_line,
        f"实际留存 {ev['kept_cny']:.2f} 元（{ev['kept_rate']:.2%}）| 次年 token 预算 {ev['next_budget_cny']:.2f} 元/年",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="评分与奖励（净收益率 → 评级 → 奖励 → 次年预算）")
    ap.add_argument("--capital", type=float, required=True, help="本金（元），如 50000")
    ap.add_argument("--return", dest="ret", type=float, required=True,
                    help="组合收益率（小数，如 0.035 = 3.5%%）")
    ap.add_argument("--token-cost", type=float, default=None,
                    help="本次 token 花费（元）；不给则按 config.token_cost_annual(本金) 取基准（默认 0 会让评级虚高）")
    ap.add_argument("--mdd", type=float, default=None,
                    help="区间最大回撤（-0.25 或 0.25 都会归一成回撤深度，不会因符号写错而漏否决）")
    ap.add_argument("--grades", default=None,
                    help="逗号分隔的历史评级，如 A,B,B+,B —— 给定时额外做 12 期分布考核")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="评判日（默认今天）；早于 2027-10-30 时奖励为 0")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是人读文本")
    a = ap.parse_args()
    if a.token_cost is None:   # 2026-10-30 修：默认 0 会把净收益算高、评级虚高
        try:
            a.token_cost = float(config.token_cost_annual(a.capital))
        except Exception:  # noqa: BLE001
            a.token_cost = 0.0
        print(f"[score] 未给 --token-cost，按本金取基准 {a.token_cost:.2f} 元/年")

    mdd = None if a.mdd is None else -abs(float(a.mdd))   # 符号归一：只认回撤「深度」
    ev = evaluate(a.ret, a.token_cost, a.capital, mdd, as_of=a.as_of)
    out = {"evaluation": ev}
    if a.grades:
        gl = [g.strip() for g in a.grades.split(",") if g.strip()]
        out["dist"] = dist_eval(gl)

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_report(ev))
        if "dist" in out:
            d = out["dist"]
            print(f"\n12 期分布考核（n={d['n']}）：{'通过' if d['pass'] else '未通过'} "
                  f"| 计数 {d['count']} | 目标 {d['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def monthly_equivs(navs, as_of=None, flows=None):
    """按月切窗口，算每个月的「等价年化率」（2026-11-26 用户拍板口径）。

    ``days`` = 该窗口**实际运行天数**（起点 → 月内最后一个净值点），不是固定 30 天。
    起点用「上一个月最后一个净值点」，因为注资在月初到账、记在当月：
    用当月首点做起点会把 5,000 当成当月涨幅（A1）。窗口内到账的注资从末值减掉
    （注资是现金流，不是收益）。月末净值已扣 token（capital_dyn.book_token 先现金出账），
    所以是净口径，不再二次扣。
    """
    buckets = {}
    for n in sorted([x for x in (navs or []) if x.get("date") and x.get("nav") is not None],
                    key=lambda x: str(x["date"])):
        buckets.setdefault(str(n["date"])[:7], []).append(n)
    chrono = sorted([x for x in (navs or []) if x.get("date") and x.get("nav") is not None],
                    key=lambda x: str(x["date"]))
    out = []
    for mth in sorted(buckets):
        rows = buckets[mth]
        prev = [x for x in chrono if str(x["date"]) < f"{mth}-01"]
        base_row = prev[-1] if prev else rows[0]
        rec = {"month": mth, "start": str(base_row["date"]), "end": str(rows[-1]["date"]),
               "points": len(rows), "days": 0, "r": None, "equiv": None, "pnl": None,
               "flow": 0.0}
        # 起点是上月最后一点时，本月哪怕只有 1 个净值点也能算（A1：注资月初到账）。
        if rows and base_row is not None:
            try:
                d0 = as_date(base_row["date"])
                d1 = as_date(rows[-1]["date"])
                rec["days"] = int((d1 - d0).days)
            except Exception:  # noqa: BLE001
                rec["days"] = 0
            _flow = 0.0
            for _f in (flows or []):
                try:
                    _fd = as_date((_f or {}).get("date"))
                    _fa = float((_f or {}).get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                if d0 < _fd <= d1:
                    _flow += _fa
            rec["flow"] = round(_flow, 2)
            v0 = float(base_row["nav"])
            v1 = float(rows[-1]["nav"])
            if v0:
                rec["r"] = (v1 - _flow) / v0 - 1.0
                rec["pnl"] = round(v1 - v0 - _flow, 2)
            if rec["r"] is not None and rec["days"] >= 1 and (1.0 + rec["r"]) > 0:
                rec["equiv"] = (1.0 + rec["r"]) ** (365.0 / rec["days"]) - 1.0
        out.append(rec)
    return out



def monthly_mean_equiv(series):
    """各月等价年化率的**算术平均**（年报定档口径；不是几何/复利平均）。"""
    vals = [float(x["equiv"]) for x in (series or []) if x.get("equiv") is not None]
    return (sum(vals) / len(vals)) if vals else None
