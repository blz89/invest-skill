# -*- coding: utf-8 -*-
"""场外债券基金「影子账」——把 R2 层的场外腿也记进模拟盘账本。

为什么需要单独一层
------------------
场内（股票/ETF）按「决策日收盘价」成交，场外基金是**未知价交易**：提交当天按当日
收盘后的**累计净值**成交、T+1 确认份额。两者成交模型不一致，所以场外腿不能走
`paper.apply_fill()`（那套是「价格 × 整手股数」）；这里用**金额法**单独记：

    持仓市值 = 投入金额 × (当日累计净值 / 买入日累计净值)

* 用**累计净值**（不是单位净值）——分红/拆分会把单位净值压低，用单位净值算收益会
  系统性偏低（`datasource.fetch_fund_nav_history` 的口径说明）；
* 金额法不需要份额精度，场外本来也是按金额申购（10 元起、可小数份额）；
* 赎回费按持有天数：不足 `HOLD_DAYS`（90 天，与 `otc.py` 同口径）按最严档罚，
  满 90 天用该基金实测的 `redeem_fee_hold`（多数为 0）。

状态结构（挂在 paper 账本 JSON 里，老账本没有这三键 → `ensure()` 自动补空）：
    state["otc_holdings"] = {"OTC:012310": {name, layer, level, amount, acc_nav,
                                            buy_date, confirm_date, nav, nav_date,
                                            redeem_fee_hold}}
    state["otc_trades"]   = [{date, code, name, action, nav, amount, fee,
                              realized_pnl, order_id, note}]
    state["otc_orders"]   = [{id, code, name, action, amount, effective_from,
                              status, nav_ref, nav_ref_cum, nav_ref_date, fee_pct,
                              redeem_fee_hold, reason, filled_date, filled_nav, fee}]

幂等与 fail-closed
------------------
* 订单只在 `effective_from` 当天有净值时成交；取不到净值就**保持挂单**并记事件，
  绝不按猜的价格入账（宁可晚记，不记假账）；
* 同一 `effective_from` 重复跑不会重复成交（`filled` 状态挡着）。
"""
from __future__ import annotations

import datetime as dt

HOLD_KEY = "otc_holdings"
TRADE_KEY = "otc_trades"
ORDER_KEY = "otc_orders"

LAYER = "理财(R2)"
LEVEL = "R2"
HOLD_DAYS = 90          # 赎回费持有期阈值：满 90 天用实测档（多为 0），不足按最严档罚
SHORT_FEE = 0.015       # 不足 90 天的保守赎回费率（7 天内最严档 1.5%）
MIN_TRADE = 100.0       # 单笔申购/赎回最小金额：低于 100 元不折腾（场外 10 元起，但没必要）
MN_AMOUNT = 10.0        # 场外最低申购金额兜底（plan 里通常给 10 元）


def ensure(state: dict) -> dict:
    """老账本兼容：补齐三个容器。"""
    for k in (HOLD_KEY, ORDER_KEY, TRADE_KEY):
        if not isinstance(state.get(k), list if k != HOLD_KEY else dict):
            state[k] = {} if k == HOLD_KEY else []
    return state


def _days(a: str, b: str) -> int:
    try:
        return (dt.date.fromisoformat(str(b)) - dt.date.fromisoformat(str(a))).days
    except (TypeError, ValueError):
        return 0


def short_code(code: str) -> str:
    """`OTC:012310` → `012310`（取净值时用 6 位代码）。"""
    s = str(code or "")
    return s.split(":", 1)[1] if ":" in s else s


# ------------------------------------------------------------------ 订单
def pending(state: dict, action: str = None) -> list:
    ensure(state)
    rows = [o for o in state[ORDER_KEY] if o.get("status") == "pending"]
    return [o for o in rows if action is None or o["action"] == action]


def _placed(state: dict, action: str) -> set:
    return {o["code"] for o in pending(state, action)}


def _next_id(state: dict, as_of: str) -> str:
    n = sum(1 for o in state.get(ORDER_KEY) or [] if str(o.get("id", "")).startswith(f"O{as_of}"))
    return f"O{as_of}-{n + 1:03d}"


def make(state: dict, as_of: str, effective_from: str, code: str, name: str,
         action: str, amount: float, nav_ref: float = None, nav_ref_date: str = None,
         nav_ref_cum: float = None, fee_pct: float = 0.0, redeem_fee_hold: float = 0.0,
         reason: str = "", min_purchase: float = None) -> dict:
    """登记一张场外申购/赎回单（`amount` 是金额，不是份额）。

    `nav_ref` 是建单日**单位净值**、`nav_ref_cum` 是**累计净值**：成交与市值只用累计
    净值（`fill/mv_of`），单位净值只作「基民在 APP 上看到的那个数」的参考。两列都记，
    否则分红/拆分后单位净值下跌会被误读成亏损（2026-11-07 B 类 #11）。
    """
    ensure(state)
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError(f"场外单金额非法：{amount}")
    lo = float(min_purchase or MN_AMOUNT)
    if action == "buy" and amount < lo:
        amount = round(lo, 2)
    order = {
        "id": _next_id(state, as_of), "code": code, "name": name, "action": action,
        "amount": amount, "status": "pending", "created_on": as_of,
        "effective_from": effective_from, "nav_ref": nav_ref,
        "nav_ref_cum": nav_ref_cum, "nav_ref_date": nav_ref_date,
        "fee_pct": float(fee_pct or 0.0), "redeem_fee_hold": float(redeem_fee_hold or 0.0),
        "layer": LAYER, "level": LEVEL, "reason": reason,
        "filled_date": None, "filled_nav": None, "fee": None,
    }
    state[ORDER_KEY].append(order)
    return order


def cancel(state: dict, order: dict, why: str) -> None:
    order["status"] = "cancelled"
    order["cancel_reason"] = why


def reserved(state: dict) -> float:
    """在挂的申购单占用的现金（赎回单不占现金）。"""
    return round(sum(float(o["amount"]) for o in pending(state, "buy")), 2)


def bootstrap_needed(state: dict) -> bool:
    """场外腿「首次补建」判定：从无持仓、无在挂单、无历史场外成交。

    为什么需要这条（2026-10-30 修）：影子账上线前的账本（10-28 决策建仓）里没有场外
    持仓，但计划目标已经有 R2 场外腿；`regime_change` 模式下市场状态没变就「不调仓」，
    这只腿会一直空着，直到下一次状态切换——与「场外腿真正进账本」的口径相矛盾。
    补建只在「三无」时触发一次（成交后 otc_trades 非空 → 永远不再触发），既不是调仓，
    也不会每周重复下单；赎回清空后也不会自动追买（尊重人为动作）。
    """
    ensure(state)
    return not state[HOLD_KEY] and not pending(state) and not state[TRADE_KEY]


# ------------------------------------------------------------------ 净值
def nav_at(series, day: str):
    """取「day 当天或之前最近一个」净值。series = [(date, value), ...]（升序）。

    返回 (date, value)；取不到（day 早于该基金首个净值日）返回 (None, None)。
    """
    best = (None, None)
    for d, v in series or []:
        if str(d) <= str(day):
            best = (str(d), float(v))
        else:
            break
    return best


def nav_on_or_after(series, day: str):
    """取「day 当天或之后最近一个」净值（成交用）。

    为什么不能用 nav_at（≤day）：订单的 effective_from 是「最早可成交日」，
    用更早的净值成交等于拿旧价格买入，与「不猜价格」矛盾（2026-10-30 审计 #6）。
    """
    for d, v in series or []:
        if str(d) >= str(day):
            return (str(d), float(v))
    return (None, None)


def latest(series):
    if not series:
        return (None, None)
    return (str(series[-1][0]), float(series[-1][1]))


def _weighted_acc(a_amt: float, a_acc: float, b_amt: float, b_acc: float) -> float:
    """两笔金额合并后的等效累计净值（成本加权）。"""
    denom = (a_amt / a_acc if a_acc else 0.0) + (b_amt / b_acc if b_acc else 0.0)
    return round((a_amt + b_amt) / denom, 6) if denom else b_acc


def mv_of(h: dict) -> float:
    """持仓市值 = 投入金额 × 最新累计净值 / 买入日累计净值。"""
    if not h:
        return 0.0
    acc0 = float(h.get("acc_nav") or 0.0)
    acc = float(h.get("nav") or acc0)
    if acc0 <= 0:
        return round(float(h.get("amount") or 0.0), 2)
    return round(float(h.get("amount") or 0.0) * acc / acc0, 2)


def mark(state: dict, nav_map: dict) -> dict:
    """用最新净值刷新每只持仓的 nav/nav_date，返回 {code: 市值}。"""
    ensure(state)
    out = {}
    for code, h in state[HOLD_KEY].items():
        d, v = latest((nav_map or {}).get(code) or [])
        if v is not None:
            h["nav"] = round(v, 6)
            h["nav_date"] = d
        out[code] = mv_of(h)
    return out


def market_value(state: dict) -> float:
    ensure(state)
    return round(sum(mv_of(h) for h in state[HOLD_KEY].values()), 2)


def cost_total(state: dict) -> float:
    ensure(state)
    return round(sum(float(h.get("amount") or 0.0) for h in state[HOLD_KEY].values()), 2)


def unrealized(state: dict) -> float:
    return round(market_value(state) - cost_total(state), 2)


def realized_total(state: dict) -> float:
    ensure(state)
    return round(sum(float(t.get("realized_pnl") or 0.0)
                     for t in state[TRADE_KEY] if t.get("action") == "sell"), 2)


def fees_total(state: dict) -> float:
    ensure(state)
    return round(sum(float(t.get("fee") or 0.0) for t in state[TRADE_KEY]), 2)


def redeem_fee_rate(h: dict, day: str) -> float:
    """按持有天数给赎回费率：满 90 天用实测档（多为 0），不足按最严档保守罚。"""
    held = _days(h.get("buy_date") or day, day)
    if held >= HOLD_DAYS:
        return float(h.get("redeem_fee_hold") or 0.0)
    return max(SHORT_FEE, float(h.get("redeem_fee_hold") or 0.0))


# ------------------------------------------------------------------ 成交
def fill(state: dict, order: dict, day: str, nav_map: dict,
         note: str = "") -> dict | None:
    """按 `effective_from` 当天的累计净值成交。取不到净值 → 保持挂单，返回 None。"""
    ensure(state)
    if order.get("status") != "pending":
        raise ValueError(f"场外单 {order.get('id')} 状态为 {order.get('status')}，不能重复成交")
    series = (nav_map or {}).get(order["code"]) or []
    taken_date, nav = nav_on_or_after(series, order["effective_from"])
    if nav is None or nav <= 0:
        return None
    amount = round(float(order["amount"]), 2)
    code = order["code"]
    if order["action"] == "buy":
        fee = round(amount * float(order.get("fee_pct") or 0.0) / 100.0, 2)
        net = round(amount - fee, 2)
        if net <= 0:
            raise ValueError(f"场外申购费吃光金额：{code} {amount} 元")
        # 先判后扣：原来先扣再判，异常时现金已经被扣掉（实测 100 → -900 → -1900，
        # 重试还会继续扣，审计 #5）。
        if round(float(state["cash"]) - amount, 2) < -0.01:
            raise ValueError(f"现金不足：场外申购 {code} 需 {amount:.2f}，"
                             f"现有 {float(state['cash']):.2f}")
        state["cash"] = round(float(state["cash"]) - amount, 2)
        h = state[HOLD_KEY].get(code)
        if h:
            h["amount"] = round(float(h["amount"]) + net, 2)
            h["acc_nav"] = _weighted_acc(float(h["amount"]) - net, float(h["acc_nav"]),
                                         net, nav)
            h["buy_date"] = taken_date            # 赎回费按「最后一次买入」的成交净值日算（不是结算日）
        else:
            h = {"code": code, "name": order.get("name"), "layer": LAYER, "level": LEVEL,
                 "amount": net, "acc_nav": round(nav, 6), "buy_date": taken_date,
                 "confirm_date": None, "nav": round(nav, 6), "nav_date": taken_date,
                 "redeem_fee_hold": float(order.get("redeem_fee_hold") or 0.0)}
            state[HOLD_KEY][code] = h
        trade = {"date": taken_date, "code": code, "name": order.get("name"),
                 "action": "buy", "nav": round(nav, 6), "amount": amount, "fee": fee,
                 "realized_pnl": None, "order_id": order.get("id"),
                 "note": note or f"场外申购（{order.get('effective_from')} 净值成交）"}
    else:
        h = state[HOLD_KEY].get(code)
        if not h:
            cancel(state, order, "持仓已不存在")
            return None
        mv = mv_of(h)
        gross = min(amount, mv)                   # 不能赎超过市值
        rate = redeem_fee_rate(h, taken_date)
        fee = round(gross * rate, 2)
        proceeds = round(gross - fee, 2)
        cost_part = round(float(h["amount"]) * (gross / mv), 2) if mv else 0.0
        realized = round(proceeds - cost_part, 2)
        state["cash"] = round(float(state["cash"]) + proceeds, 2)
        h["amount"] = round(float(h["amount"]) - cost_part, 2)
        h["nav"] = round(nav, 6)
        h["nav_date"] = taken_date
        if h["amount"] <= 0.01:
            del state[HOLD_KEY][code]
        trade = {"date": taken_date, "code": code, "name": order.get("name"),
                 "action": "sell", "nav": round(nav, 6), "amount": gross, "fee": fee,
                 "realized_pnl": realized, "cost_basis": cost_part,
                 "hold_days": _days(h.get("buy_date") or taken_date, taken_date),
                 "order_id": order.get("id"),
                 "note": note or f"场外赎回（{order.get('effective_from')} 净值成交，"
                                 f"赎回费率 {rate:.2%}）"}
    state[TRADE_KEY].append(trade)
    order["status"] = "filled"
    order["filled_date"] = taken_date
    order["filled_nav"] = round(nav, 6)
    order["fee"] = fee
    return trade


def resolve(state: dict, as_of: str, nav_map: dict) -> list:
    """撮合所有 `effective_from <= as_of` 的场外挂单，返回 [(level, msg)]。"""
    ensure(state)
    msgs = []
    for o in pending(state):
        if str(o.get("effective_from") or "") > str(as_of):
            continue
        t = fill(state, o, as_of, nav_map)
        if t is None:
            msgs.append(("warn", f"场外单 {o['id']} {o['code']} 取不到 "
                                 f"{o['effective_from']} 的净值 → 保持挂单（不猜价格）"))
            continue
        act = "申购" if t["action"] == "buy" else "赎回"
        msgs.append(("info", f"场外{act} {o['code']} {o['name']} {t['amount']:.2f} 元 "
                             f"@ 累计净值 {t['nav']:.4f}（{t['date']}），费用 {t['fee']:.2f}"))
    return msgs


# ------------------------------------------------------------------ 按计划下单
def target_orders(state: dict, as_of: str, effective_from: str, wealth: list,
                  nav_map: dict, cash_available: float, allow: bool = True,
                  min_trade: float = MIN_TRADE) -> list:
    """按阶段三的「场外腿目标」补差下单：多退少补，只在 `allow` 时动。"""
    ensure(state)
    if not allow:
        return []
    mark(state, nav_map)
    tgt = {}
    for r in wealth or []:
        code = str(r.get("code") or "")
        if not code:
            continue
        tgt[code] = r
    out = []
    # 1) 已持有但不在目标里 → 全额赎回
    for code in list(state[HOLD_KEY]):
        if code in tgt or code in _placed(state, "sell"):
            continue
        mv = mv_of(state[HOLD_KEY][code])
        if mv < min_trade:
            continue
        cum_now = nav_at((nav_map or {}).get(code) or [], as_of)[1]
        out.append(make(state, as_of, effective_from, code,
                        state[HOLD_KEY][code].get("name"), "sell", mv,
                        nav_ref_cum=cum_now,
                        reason="场外腿目标里已没有这只 → 赎回"))
    # 2) 目标里每只：与当前市值比，差额够大才动
    avail = max(0.0, float(cash_available) - reserved(state))
    for code, r in tgt.items():
        want = float(r.get("amount") or 0.0)
        h = state[HOLD_KEY].get(code)
        cur = mv_of(h) if h else 0.0
        delta = round(want - cur, 2)
        if delta >= min_trade and code not in _placed(state, "buy"):
            amt = min(delta, avail)
            if amt >= max(min_trade, float(r.get("min_purchase") or MN_AMOUNT)):
                cum_now = nav_at((nav_map or {}).get(code) or [], as_of)[1]
                out.append(make(state, as_of, effective_from, code,
                                r.get("name"), "buy", amt,
                                nav_ref=r.get("unit_nav"), nav_ref_date=r.get("nav_date"),
                                nav_ref_cum=(r.get("nav_ref_cum") if r.get("nav_ref_cum")
                                             is not None else cum_now),
                                fee_pct=r.get("fee"), redeem_fee_hold=r.get("redeem_fee_hold"),
                                reason=f"场外腿补到目标 {want:,.0f} 元（当前 {cur:,.0f}）",
                                min_purchase=r.get("min_purchase")))
                avail = max(0.0, avail - amt)
        elif delta <= -min_trade and code not in _placed(state, "sell"):
            out.append(make(state, as_of, effective_from, code, r.get("name"), "sell",
                            -delta, nav_ref=r.get("unit_nav"),
                            nav_ref_date=r.get("nav_date"),
                            redeem_fee_hold=r.get("redeem_fee_hold"),
                            reason=f"场外腿减到目标 {want:,.0f} 元（当前 {cur:,.0f}）"))
    return out


def holdings_rows(state: dict) -> list:
    """持仓明细行（给周报/月报/持仓 md 用）。"""
    ensure(state)
    rows = []
    for code, h in state[HOLD_KEY].items():
        mv = mv_of(h)
        cost = float(h.get("amount") or 0.0)
        rows.append({
            "code": code, "name": h.get("name"), "kind": "fund", "layer": h.get("layer"),
            "level": h.get("level"), "amount": round(cost, 2),
            "nav": h.get("nav"), "acc_nav": h.get("acc_nav"),
            "nav_date": h.get("nav_date"),
            "buy_date": h.get("buy_date"), "value": mv,
            "unrealized": round(mv - cost, 2),
            "unrealized_pct": round(mv / cost - 1, 6) if cost else None,
            "redeem_fee": redeem_fee_rate(h, h.get("nav_date") or h.get("buy_date") or "9999-12-31"),
            "hold_days": _days(h.get("buy_date") or h.get("nav_date"), h.get("nav_date")),
        })
    return rows
