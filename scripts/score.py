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
             net_profit: float | None = None) -> dict:
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
    if days and float(days) >= 1.0 and (1.0 + nr) > 0.0:
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
