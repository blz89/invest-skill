# -*- coding: utf-8 -*-
"""小资金执行层：把「目标权重」翻译成「真能下单的整手 + 现金零头」。

适用区间：本部署本金 5 万（单账本）；``config.CAPITAL["min"]/["max"]`` 只是防极端值的兜底校验，不是推荐口径。
低于 5 万：整手取整 + 逆回购 1,000 元/手的粒度会让「配置」变成掷硬币；
远超本部署本金：单票 10% 的额度足够大，整手不再构成约束，无需这层适配。

核心恒等式（全部是除法，没有"感觉"）：

    1 手金额        = 价格 × 100
    单票 10% 上限   ⇒ 本金 ≥ 1 手金额 ÷ 10%        ← 否则这只票一手都买不了
    分 3 批建仓     ⇒ 本金 ≥ 1 手金额 ÷ (10% × 25%) ← 否则最后一批买不到 1 手

本模块**不联网、不改任何筛选/打分阈值**，只做算术与可读性输出，可离线测试
（见 `_selftest.py`）。
"""
from __future__ import annotations

import math

import config

CAP = config.CAPITAL
EXEC = config.EXEC


class CapitalRangeError(ValueError):
    """本金超出 config.CAPITAL 的兜底区间。"""


# ---------------------------------------------------------------- 区间校验
def validate_capital(capital: float) -> float:
    """校验本金落在兜底区间内，越界就抛错（不许静默取默认值）。"""
    try:
        c = float(capital)
    except (TypeError, ValueError):
        raise CapitalRangeError(f"本金必须是数字，收到 {capital!r}")
    if not math.isfinite(c):
        raise CapitalRangeError(f"本金必须是有限数字，收到 {capital!r}")
    lo, hi = CAP["min"], CAP["max"]
    if c < lo or c > hi:
        raise CapitalRangeError(
            f"本金 {c:,.0f} 元超出本金校验区间 [{lo:,.0f}, {hi:,.0f}] 元。\n"
            f"  · 低于 {lo:,.0f} 元：整手（1 手=100 股）与逆回购 1,000 元/手的粒度太粗，"
            f"「按权重配置」无法落地，不适合用这套规则。\n"
            f"  · 高于 {hi:,.0f} 元：单票 10% 的额度足够大，整手不再构成约束，无需小资金适配。\n"
            f"  请确认金额；本部署口径为 5 万。")
    return c


# ---------------------------------------------------------------- 整手算术
def lot_cost(price: float) -> float:
    return round(float(price) * CAP["lot_size"], 2)


def lots_for_amount(amount: float, price: float) -> int:
    """给定金额最多能买几手（向下取整，不允许透支）。"""
    lc = float(price) * CAP["lot_size"]
    if lc <= 0 or amount <= 0:
        return 0
    return int(math.floor((float(amount) + 1e-9) / lc))


def min_capital_single(price: float) -> float:
    """让「1 手」刚好不超过单票 10% 上限所需的本金。"""
    return round(lot_cost(price) / EXEC["max_single_position"], 2)


def min_capital_3tranche(price: float) -> float:
    """让三批建仓中最小的一批（25%）也买得起 1 手所需的本金。"""
    min_ratio = min(TRANCHE_RATIOS)
    return round(lot_cost(price) / (EXEC["max_single_position"] * min_ratio), 2)


TRANCHE_RATIOS = (0.40, 0.35, 0.25)


def split_lots(total_lots: int, ratios: tuple[float, ...] = TRANCHE_RATIOS) -> list[int]:
    """把总手数按 40/35/25 拆成三批（最大余数法，能保证每批 ≥1 手时不出现 0 手）。"""
    n = len(ratios)
    if total_lots <= 0:
        return [0] * n
    if total_lots < n:
        return [1 if i < total_lots else 0 for i in range(n)]
    raw = [total_lots * r for r in ratios]
    base = [int(math.floor(x)) for x in raw]
    rem = total_lots - sum(base)
    order = sorted(range(n), key=lambda i: -(raw[i] - base[i]))
    for i in order[:rem]:
        base[i] += 1
    return base


# ---------------------------------------------------------------- 交易成本
def trade_fee(amount: float, is_etf: bool = False, side: str = "buy") -> dict:
    """单笔费用：佣金（有最低值）+ 过户费（仅股票）+ 印花税（仅股票卖出）。"""
    amount = max(0.0, float(amount))
    if amount <= 0:
        return {"commission": 0.0, "transfer": 0.0, "stamp": 0.0, "total": 0.0,
                "ratio": 0.0}
    commission = max(amount * CAP["commission_rate"], CAP["min_commission"])
    transfer = 0.0 if is_etf else amount * CAP["transfer_fee_rate"]
    stamp = 0.0 if (is_etf or side != "sell") else amount * CAP["stamp_duty_sell"]
    total = commission + transfer + stamp
    return {"commission": round(commission, 2), "transfer": round(transfer, 2),
            "stamp": round(stamp, 2), "total": round(total, 2),
            "ratio": round(total / amount, 6)}


def fee_summary(orders: list[dict], capital: float) -> dict:
    """汇总所有订单的预估费用，并给出「佣金是否吃掉收益」的判断。"""
    buy_fee = sell_fee = 0.0
    worst = []
    for o in orders:
        amt = float(o.get("amount") or 0)
        is_etf = bool(o.get("is_etf"))
        f = trade_fee(amt, is_etf=is_etf, side="buy")
        buy_fee += f["total"]
        sell_fee += trade_fee(amt, is_etf=is_etf, side="sell")["total"]
        if f["ratio"] > CAP["commission_ratio_warn"]:
            worst.append({"code": o.get("code"), "name": o.get("name"),
                          "amount": round(amt, 2), "fee": f["total"],
                          "ratio": f["ratio"]})
    round_trip = round(buy_fee + sell_fee, 2)
    _min_fee = float(CAP["min_commission"] or 0.0)
    _fee_note = (f"单笔佣金按万 {CAP['commission_rate'] * 1e4:.1f} 估算"
                 + (f"（最低 {_min_fee:.0f} 元）" if _min_fee > 0
                    else "（账户已免五，无最低收费）")
                 + "；实际以交割单为准。")
    return {
        "buy_fee": round(buy_fee, 2),
        "sell_fee": round(sell_fee, 2),
        "round_trip_fee": round_trip,
        "round_trip_pct_of_capital": round(round_trip / capital, 6) if capital else None,
        "high_fee_orders": worst,
        "note": _fee_note,
    }


# ---------------------------------------------------------------- 现金增值层
def cash_net_rates() -> dict:
    """两种现金容器的**扣费后**年化：逆回购扣全年佣金，朝朝宝/货基无交易费。

    逆回购佣金按单笔期限收（交易所上限）：1 天期 0.001%、7 天期 0.005%、14 天期 0.010%、
    28 天期 0.020%；年化后 1 天期日滚 0.25%、7/14/28 天期 0.26%/本金。券商可在上限下
    打折，用 `repo_commission_discount` 折算（1.0=上限、0.1=一折、0=免收）。只比毛利率会
    把逆回购抬成假优势，`prefer` 一律用扣费后净收益比较（打平按现金侧，fail-closed）。
    """
    r = config.cash_rates()
    _annual = float(r.get("repo_fee_annual_rate")
                    or float(r["repo_fee_rate"]) * float(r["repo_trades_per_year"]))
    _disc = float(r.get("repo_commission_discount", 1.0))
    fee = _annual * _disc
    repo_net = round(float(r["repo"]) - fee, 6)
    mf_net = round(float(r["money_fund"]), 6)
    # R1 现金管理类理财（「都默认朝朝宝里存着」）：登记了朝朝宝/R1
    # 就默认放朝朝宝，否则退回货基。注意这里**不再取「货基和 R1 谁高用谁」**：用户要默认
    # 朝朝宝，账面计息利率必须与产品一致，不能混用更高的那个（否则账本利率不可解释）。
    r1 = r.get("r1")
    r1_net = round(float(r1), 6) if r1 else None
    cash_side = "r1" if r1_net is not None else "money_fund"
    cash_side_rate = r1_net if r1_net is not None else mf_net
    mf_eff = max(mf_net, r1_net) if r1_net is not None else mf_net
    # 7 天期逆回购（观望资金买 7 天以上）：
    # 毛利率取 rates.py 实测 R-007；年佣金 52 笔 × 0.005% = 0.26%/本金（同样可乘券商折扣）。
    repo7_gross = round(float(r.get("repo7") or 0.0), 6)
    repo7_fee = round(float(r.get("repo7_fee_annual_rate") or 0.0026)
                      * float(r.get("repo_commission_discount", 1.0)), 6)
    repo7_net = round(repo7_gross - repo7_fee, 6)
    return {"repo_gross": round(float(r["repo"]), 6), "repo_fee": round(fee, 6),
            "repo_net": repo_net, "repo_fee_annual_rate": round(_annual, 6),
            "repo_commission_discount": float(_disc),
            "money_fund": mf_eff, "money_fund_gross": mf_net,
            "repo7_gross": repo7_gross, "repo7_fee": repo7_fee, "repo7_net": repo7_net,
            "repo7_fee_annual_rate": float(r.get("repo7_fee_annual_rate") or 0.0026),
            "repo7_trades_per_year": int(r.get("repo7_trades_per_year") or 52),
            "repo7_term_days": int(r.get("repo7_term_days") or 7),
            "r1": r1_net, "r1_name": r.get("r1_name") or "",
            "r1_as_of": r.get("r1_as_of") or "",
            "r1_source": r.get("r1_source") or "",
            "cash_side": cash_side, "cash_side_rate": cash_side_rate,
            "prefer": "repo" if repo_net > cash_side_rate + 1e-12 else cash_side,
            "cash_policy": str(r.get("cash_policy") or ""),
            "semi_liquid": dict(r.get("semi_liquid") or {}),
            "as_of": r["as_of"]}


def cash_split(amount: float, container: str = None) -> dict:
    """把现金拆成 (逆回购整手, 现金侧=朝朝宝/货基)。container: auto / repo / repo7 / money_fund / r1。

    - auto = 按 `cash_net_rates()` 扣费后净收益比较（登记朝朝宝后默认朝朝宝，打平按现金侧）；
    - repo7 = 观望资金专用：7 天期以上逆回购，1,000 元整手，利率取 R-007 实测、年费率 0.26%，
      期限按 repo_term_ladder 金额分档（7/14/28 天期），零头进朝朝宝/货基；
    - repo = 人工锁定 1 天期逆回购（回滚/验证用），1,000 元整手；
    - money_fund = 全部放货基（忽略朝朝宝登记）；r1 = 全部放朝朝宝（未登记时报错）。
    **操作日窗口规则不在这里**（见 paper.repo_lot_schedule）；本函数只决定钱放哪一侧。
    """
    cash = max(0.0, round(float(amount), 2))
    r = config.cash_rates()
    want = str(container or config.CAPITAL["cash_layer"].get("container") or "auto")
    rates = cash_net_rates()
    if want not in ("auto", "repo", "repo7", "money_fund", "r1"):
        raise ValueError(f"未知现金容器 {want!r}（只接受 auto/repo/repo7/money_fund/r1）")
    if want == "r1" and rates.get("r1") is None:
        raise ValueError("朝朝宝/R1 利率未登记（CAPITAL.cash_layer.r1_annual_rate），"
                         "不能指定 container='r1'；可先用 auto 走货基")
    side = rates["prefer"] if want == "auto" else want
    is_repo = side in ("repo", "repo7")
    lots = int(math.floor((cash + 1e-9) / r["repo_min_lot"])) if is_repo else 0
    repo_amt = round(lots * r["repo_min_lot"], 2)
    repo_rate = float(rates["repo7_gross"] if side == "repo7" else r["repo"])
    fee_annual = float(rates["repo7_fee_annual_rate"] if side == "repo7"
                       else rates["repo_fee_annual_rate"])
    tpy = int(rates["repo7_trades_per_year"] if side == "repo7"
              else r["repo_trades_per_year"])
    term = int(rates["repo7_term_days"] if side == "repo7" else 1)
    if side == "repo7":
        # 按观望资金金额分档（7/14/28 天期，年费率同为 0.26%）：
        # 金额越大越不急用、锁越久；流动性由朝朝宝（T+0）顶着。
        for _thr, _td in sorted(((int(a), int(b)) for a, b in
                                 (r.get("repo_term_ladder") or [[0, term]])), reverse=True):
            if repo_amt >= _thr:
                term = _td
                break
    return {"container": side, "requested": want, "cash_total": cash,
            "repo_lots": lots, "repo_amount": repo_amt,
            "money_fund_amount": round(cash - repo_amt, 2),
            "repo_min_lot": float(r["repo_min_lot"]),
            "repo_rate": repo_rate, "money_fund_rate": float(rates["cash_side_rate"]),
            "money_fund_gross_rate": float(r["money_fund"]),
            "cash_side": rates["cash_side"], "cash_side_rate": rates["cash_side_rate"],
            "cash_side_name": (rates["r1_name"] if rates["cash_side"] == "r1" else "货基"),
            "repo_term_days": term,
            "repo_fee_rate": float(r["repo_fee_rate"]),
            "repo_trades_per_year": tpy,
            "repo_fee_annual_rate": fee_annual,
            "repo_commission_discount": float(r.get("repo_commission_discount", 1.0)),
            "repo_net_rate": (rates["repo7_net"] if side == "repo7" else rates["repo_net"]),
            "money_fund_net_rate": rates["money_fund"],
            "rates": rates, "rate_as_of": r["as_of"]}


def cash_layer(cash_amount: float, container: str = None, reserved: float = 0.0,
               ammo: float = 0.0) -> dict:
    """现金层分工（默认策略）：挂单资金 + 建仓弹药 → T+0，观望资金 → 全市场货基池。

    - 默认（container=None/"policy"）：`reserved` = 在挂买单占用的现金、`ammo` = 建仓弹药
      （权益上限 − 现有场内权益）。两者**取较大者**留 T+0（挂单冻结的钱成交后就是权益、
      本来就含在弹药里，不能重复扣）；剩余观望资金交给货基池（`cash_policy` 含 pool 时）
      或按旧口径分档买 7/14/28 天期逆回购。
    - container=auto/repo/money_fund/r1：回滚/验证用的旧口径（见 `cash_split`）。

    「货基」= 场外货币基金/券商现金理财（0 手续费、1 元起）；「朝朝宝」= 招银理财朝招金
    （R1 现金管理类，登记后为默认现金侧）。逆回购佣金按期限收（7/14/28 天期 0.26%/本金·年，
    1 天期日滚 0.25%），交易所上限，券商可打折。
    """
    cash = max(0.0, round(float(cash_amount), 2))
    rates = cash_net_rates()
    side_name = rates["r1_name"] if rates.get("cash_side") == "r1" else "货基"
    want = str(container or "policy")
    policy = str(rates.get("cash_policy") or "")
    if want in ("policy", "auto_policy"):
        res = min(cash, max(0.0, round(float(reserved or 0.0), 2)))
        am = min(cash, max(0.0, round(float(ammo or 0.0), 2)))
        keep = max(res, am)          # 挂单冻结含在弹药里 → 取较大者，不重复扣
        idle = round(max(0.0, cash - keep), 2)
        _sleeve = {}
        if "pool" in policy:
            # 半流动性货币增值（2026-11-15 起；2026-11-19改为全市场货基）：
            # 90 天口径前 N 支 × 每支 ≤1 万（旧朝朝盈2号 100 支口径可用 cash_pool.py --universe chaochao2 回滚）。
            try:
                import cash_sleeve as cs
                _sleeve = cs.plan(idle)
            except Exception as exc:      # 排行缺失/损坏 → 退回朝朝宝，不阻断计划
                _sleeve = {"holdings": [], "weighted_rate": 0.0, "fallback": True,
                           "note": f"半流动性切分失败（{type(exc).__name__}: {exc}），整笔退回朝朝宝"}
            sl_amt = round(sum(float(h["amount"]) for h in (_sleeve.get("holdings") or [])), 2)
            sl_rate = float(_sleeve.get("weighted_rate") or 0.0)
            repo_amt, lots = 0.0, 0
            repo_rate, fee_annual = 0.0, 0.0
            term, tpy = int(rates.get("repo7_term_days") or 7), 0
            repo_gross = repo_fee = 0.0
            sleeve_year = round(sl_amt * sl_rate, 2)
            mf_amt = round(cash - sl_amt, 2)
            mf_gross = mf_amt * rates["cash_side_rate"]
            note = (f"T+0 保留 {keep:,.0f} 元（建仓弹药 {am:,.0f} / 挂单冻结 {res:,.0f}，取较大者）"
                    f"放{side_name}（随时可用于成交）；"
                    f"观望资金 {idle:,.0f} 元做半流动性货币增值："
                    f"{_sleeve.get('note') or '（未分配）'}；"
                    f"利率取各支 90 天实得年化（数据日 {_sleeve.get('data_date') or '-'}，"
                    f"是历史实得、不是未来收益承诺）。")
            return {"cash_total": cash, "container": "policy_pool", "container_requested": want,
                    "cash_side": rates.get("cash_side"), "cash_side_name": side_name,
                    "reserved_amount": res, "idle_amount": idle,
                    "ammo_amount": am, "t0_keep": keep,
                    "repo_lots": 0, "repo_amount": 0.0, "repo_term_days": term,
                    "repo_trades_per_year": 0,
                    "repo_fee_annual_rate": 0.0,
                    "repo_gross_year": 0.0, "repo_fee_year": 0.0, "repo_net_year": 0.0,
                    "sleeve_amount": sl_amt, "sleeve_rate": sl_rate,
                    "sleeve_holdings": list(_sleeve.get("holdings") or []),
                    "sleeve_data_date": str(_sleeve.get("data_date") or ""),
                    "sleeve_stale": bool(_sleeve.get("stale")),
                    "sleeve_fallback": bool(_sleeve.get("fallback")),
                    "sleeve_note": str(_sleeve.get("note") or ""),
                    "sleeve_year": sleeve_year,
                    "money_fund_amount": mf_amt, "money_fund_gross_year": round(mf_gross, 2),
                    "total_net_year": round(sleeve_year + mf_gross, 2),
                    "net_yield": (round((sleeve_year + mf_gross) / cash, 6) if cash else None),
                    "repo_net_rate": float(rates.get("repo7_net") or 0.0),
                    "money_fund_net_rate": rates["cash_side_rate"],
                    "rates": rates, "rate_as_of": rates["as_of"], "note": note,
                    "policy": policy}
        sp = cash_split(idle, "repo7")
        repo_amt, lots = round(sp["repo_amount"], 2), int(sp["repo_lots"])
        mf_amt = round(cash - repo_amt, 2)
        repo_rate, fee_annual = sp["repo_rate"], sp["repo_fee_annual_rate"]
        term, tpy = sp["repo_term_days"], sp["repo_trades_per_year"]
        repo_gross = repo_amt * repo_rate
        repo_fee = repo_amt * fee_annual if lots else 0.0
        mf_gross = mf_amt * sp["money_fund_rate"]
        note = (f"T+0 保留 {keep:,.0f} 元（建仓弹药 {am:,.0f} / 挂单冻结 {res:,.0f}，取较大者）"
                f"放{side_name}（随时可用于成交）；"
                f"观望资金 {idle:,.0f} 元按金额分档买 {term} 天期逆回购"
                f"（{lots} 手 × {sp['repo_min_lot']:,.0f} 元，深市 R-007、1,000 元起，每周一操作；"
                f"毛 {repo_rate:.2%} − 年费 {fee_annual:.2%} = 净 {sp['repo_net_rate']:.2%}；"
                f"期限按观望金额分档 7/14/28 天，朝朝宝顶流动性）；"
                f"{side_name}合计 {mf_amt:,.0f} 元（{sp['cash_side_rate']:.2%}，0 手续费）。"
                f"利率假设（{sp['rate_as_of']} 口径，是当前时点假设、不是未来收益承诺）。")
        return {"cash_total": cash, "container": "policy", "container_requested": want,
                "cash_side": sp["cash_side"], "cash_side_name": side_name,
                "reserved_amount": res, "idle_amount": idle,
                "ammo_amount": am, "t0_keep": keep,
                "repo_lots": lots, "repo_amount": repo_amt, "repo_term_days": term,
                "repo_trades_per_year": tpy,
                "repo_fee_annual_rate": fee_annual,
                "repo_gross_year": round(repo_gross, 2), "repo_fee_year": round(repo_fee, 2),
                "repo_net_year": round(repo_gross - repo_fee, 2),
                "money_fund_amount": mf_amt, "money_fund_gross_year": round(mf_gross, 2),
                "total_net_year": round(repo_gross - repo_fee + mf_gross, 2),
                "net_yield": round((repo_gross - repo_fee + mf_gross) / cash, 6) if cash else None,
                "repo_net_rate": sp["repo_net_rate"],
                "money_fund_net_rate": sp["money_fund_rate"],
                "rates": rates, "rate_as_of": sp["rate_as_of"], "note": note}
    # ---- 旧口径（回滚/验证）：显式容器 ----
    sp = cash_split(cash, want)
    lots, repo_amt, mf_amt = sp["repo_lots"], sp["repo_amount"], sp["money_fund_amount"]
    repo_gross = repo_amt * sp["repo_rate"]
    repos_annual = float(sp.get("repo_fee_annual_rate") or 0.0)
    repo_fee = (repo_amt * repos_annual if lots else 0.0)
    mf_gross = mf_amt * sp["money_fund_rate"]
    _why = (f"人工指定容器 {sp['container']}（逆回购净 {sp['repo_net_rate']:.2%}、"
            f"{side_name} {sp['money_fund_rate']:.2%}）")
    return {"cash_total": cash, "container": sp["container"],
            "container_requested": sp["requested"],
            "cash_side": sp.get("cash_side"), "cash_side_name": side_name,
            "reserved_amount": 0.0, "idle_amount": cash,
            "ammo_amount": 0.0, "t0_keep": 0.0,
            "repo_lots": lots, "repo_amount": repo_amt,
            "repo_term_days": sp.get("repo_term_days", 1),
            "repo_trades_per_year": sp.get("repo_trades_per_year"),
            "repo_fee_annual_rate": repos_annual,
            "repo_gross_year": round(repo_gross, 2), "repo_fee_year": round(repo_fee, 2),
            "repo_net_year": round(repo_gross - repo_fee, 2),
            "money_fund_amount": mf_amt, "money_fund_gross_year": round(mf_gross, 2),
            "total_net_year": round(repo_gross - repo_fee + mf_gross, 2),
            "net_yield": round((repo_gross - repo_fee + mf_gross) / cash, 6) if cash else None,
            "repo_net_rate": sp["repo_net_rate"], "money_fund_net_rate": sp["money_fund_rate"],
            "rates": rates, "rate_as_of": sp["rate_as_of"],
            "note": f"{_why}。"}


# ---------------------------------------------------------------- 档位
def tier_plan(capital: float, tier: str, regime_gross: float,
              override: bool = False) -> dict:
    """三档权益比例，且**不得突破市场状态给的仓位上限**（除非显式 override）。"""
    tiers = CAP["tiers"]
    tier = (tier or CAP["default_tier"]).upper()
    if tier not in tiers:
        raise ValueError(f"未知档位 {tier!r}，可选 {sorted(tiers)}")
    ratio = tiers[tier]["equity_ratio"]
    capped = (not override) and ratio > regime_gross
    eff = regime_gross if capped else ratio
    return {
        "tier": tier,
        "tier_label": tiers[tier]["label"],
        "tier_ratio": ratio,
        "regime_cap": regime_gross,
        "equity_ratio": round(eff, 4),
        "equity_amount": round(capital * eff, 2),
        "cash_amount": round(capital * (1 - eff), 2),
        "capped_by_regime": capped,
        "override": bool(override),
        "note": (f"档位 {tier}（{tiers[tier]['label']}）目标权益 {ratio * 100:.1f}%，"
                 + (f"受市场状态上限 {regime_gross * 100:.0f}% 压回 {eff * 100:.1f}%"
                    if capped else f"市场状态上限 {regime_gross * 100:.0f}% 允许")
                 + ("；⚠️ 用户指定越出市场状态上限，属规则外做法"
                    if override and ratio > regime_gross else "")),
    }


# ---------------------------------------------------------------- 逐标的可行性
def lot_table(items: list[dict], capital: float,
              is_etf: bool = False) -> list[dict]:
    """items: [{code,name,price,weight,...}] → 每只的「1 手成本 / 能买几手 / 实际金额」。

    买不了的标的会被标 feasible=False 并给出「至少要多少本金」，供上层改配 ETF 或现金。
    """
    rows = []
    for it in items:
        price = it.get("price")
        w = float(it.get("weight") or 0)
        budget = round(capital * w, 2)
        if not price:
            rows.append({**it, "lot_cost": None, "lots": 0, "actual_amount": 0.0,
                         "actual_weight": 0.0, "feasible": False,
                         "min_capital_single": None,
                         "min_capital_3tranche": None,
                         "note": "缺价格，无法换算手数"})
            continue
        lc = lot_cost(price)
        # 手数取「最近整数」而不是一律向下取整：小资金下向下取整会把 8.75% 的目标打成 4.4%，
        # 现金白白多留。允许实际金额比目标超 12% 以内，同时不得超过单票 10% 上限。
        cap_lots = int(math.floor((capital * EXEC["max_single_position"] + 1e-9) / lc)) if lc > 0 else 0
        lots = min(int(round(budget / lc)), int(math.floor((budget * 1.12 + 1e-9) / lc)), cap_lots)
        lots = max(0, lots)
        actual = round(lots * lc, 2)
        rows.append({
            **it,
            "budget": budget,
            "lot_cost": lc,
            "lots": lots,
            "shares": lots * CAP["lot_size"],
            "actual_amount": actual,
            "actual_weight": round(actual / capital, 4) if capital else None,
            "is_etf": is_etf,
            "feasible": lots >= 1,
            "min_capital_single": min_capital_single(price),
            "min_capital_3tranche": min_capital_3tranche(price),
            "note": ("可买" if lots >= 1 else
                     f"预算 {budget:,.0f} 元买不到 1 手（1 手需 {lc:,.0f} 元）；"
                     f"要让 1 手不超单票 10% 需本金 ≥{min_capital_single(price):,.0f} 元"),
        })
    return rows


def lot_ladder(row: dict) -> list[dict]:
    """把一只标的的整手数按 40/35/25 拆成三批（返回每批手数/股数/金额）。"""
    lc = row.get("lot_cost") or 0
    total = int(row.get("lots") or 0)
    parts = split_lots(total)
    ladder, cum = [], 0
    for i, (ratio, lots) in enumerate(zip(TRANCHE_RATIOS, parts), 1):
        amt = round(lots * lc, 2)
        cum += lots
        ladder.append({
            "tranche": i, "ratio": ratio, "lots": lots,
            "shares": lots * CAP["lot_size"], "amount": amt,
            "cum_lots": cum,
        })
    if total and total < 3:
        for s in ladder:
            s["note"] = (f"本金不足：整只只能买 {total} 手，无法凑够 3 批，"
                         f"实际按 {total} 批执行")
    return ladder


def describe(capital: float) -> dict:
    """给报告开头用的区间说明。"""
    return {
        "capital": round(capital, 2),
        "range": [CAP.get("coverage_min", CAP["min"]), CAP["max"]],
        "lot_size": CAP["lot_size"],
        "note": (f"本金 {capital:,.0f} 元（本部署单账本）；"
                 f"1 手 = {CAP['lot_size']} 股(份)，"
                 f"单票 10% 上限 = {capital * EXEC['max_single_position']:,.0f} 元/只。"),
    }
