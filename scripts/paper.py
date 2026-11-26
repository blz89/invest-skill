# -*- coding: utf-8 -*-
"""模拟盘账本（paper trading）——纯标准库，可离线单测。

职责边界（和现有分层保持一致）：
  * 只管「钱 / 股数 / 成交 / 净值 / 绩效」，不取数、不选股、不打分；
  * 价格一律由调用方（weekly.py）从缓存日线传进来，账本自己不猜价格；
  * 费用口径复用 capital.trade_fee，与阶段三计划的 50 元/100 元估算完全一致；
  * 幂等：同一订单重复结算不会重复成交；同一天重复结算覆盖当天净值（不追加）。

状态文件结构（JSON，可人工审阅 / 可回滚）：
{
  "meta": {"version":1, "mode":"paper", "capital":50000.0, "tier":"B",
           "created_at":"...", "baseline_date":"2026-10-23", "baseline_bench":4105.33},
  "cash": 50000.0,
  "holdings": {"sh512500": {...}},
  "trades":  [{"date","code","name","kind","action","price","shares","amount","fee","realized_pnl",...}],
  "orders":  [{"id","code","action","order_type","shares","limit_price","effective_from",
               "expire_on","status","filled_date","filled_price","fee","reason"}],
  "nav":     [{"date","nav","cash","market_value","positions","bench","bench_nav","drawdown","peak_nav"}],
  "events":  [{"date","level","msg"}],
  "cash_marks": {"last_accrued": "2026-10-23"}   // 现金层计息到位日，防重复计息
}
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import capital as cap  # noqa: E402
import config  # noqa: E402
import otc_book  # noqa: E402  （纯标准库；场外债券基金影子账）

CFG = config.PAPER
LOT = int(config.CAPITAL["lot_size"])
STATE_PATH = CFG["state_file"]


class LedgerError(Exception):
    """账本层面的非法操作（现金不足、卖超持仓、重复订单等）。"""


# ------------------------------------------------------------------ 基础
def _money(x) -> float:
    return round(float(x) + 1e-9, 2)


def _px(x) -> float:
    """价格精度：A 股股票 2 位小数，ETF/基金 3 位小数，统一保留 3 位。"""
    return round(float(x) + 1e-9, 3)


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _lot(price: float) -> float:
    return round(float(price) * LOT, 2)


def _shares_for_amount(amount: float, price: float) -> int:
    """给定金额最多能买几股（整手，向下取整，绝不允许透支）。"""
    if price <= 0 or amount <= 0:
        return 0
    return int(amount // _lot(price)) * LOT


# ------------------------------------------------------------------ 状态
def new_state(capital: float, tier: str = None, baseline_date: str = None,
              baseline_bench: float = None) -> dict:
    capital = float(capital)
    cap.validate_capital(capital)  # 越界直接抛错（兜底校验）
    return {
        "meta": {
            "version": 1,
            "mode": "paper",
            "capital": _money(capital),
            "tier": tier or config.CAPITAL["default_tier"],
            "created_at": _now(),
            "baseline_date": baseline_date,
            "baseline_bench": baseline_bench,
            "bench_index": CFG["bench_index"],
        },
        "cash": _money(capital),
        "holdings": {},
        "trades": [],
        "orders": [],
        "nav": [],
        "events": [],
        # 场外债券基金影子账（R2 层）：金额法记账，见 otc_book.py
        "otc_holdings": {},
        "otc_orders": [],
        "otc_trades": [],
    }


def load(path: str = None) -> dict:
    path = path or STATE_PATH
    if not os.path.exists(path):
        raise LedgerError(f"模拟盘状态文件不存在：{path}；先用 `python weekly.py --init` 初始化")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(state: dict, path: str = None) -> str:
    path = path or STATE_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def backup_state(path: str = None, keep: int = None) -> str:
    """决策/补跑前给账本做一份带时间戳的备份，只保留最近 keep 份。

    无人值守系统里最贵的错误是「不可恢复」：账本被写坏、重复下单、误改一手。
    备份目录在账本同级的 `_backup/` 下，文件名 `<账本名>.<YYYYMMDD-HHMMSS>.json`，
    按时间排序只留最后 keep 份（config.PAPER["backup_keep"]，默认 30）。
    按「保留天数」删旧备份不在这里（周决策时由 prune_backups() 做，口径见 config.PAPER["backup_keep_days"]）。
    返回备份路径；账本不存在时返回空字符串（首次建账前不备份）。
    """
    path = path or STATE_PATH
    if not os.path.exists(path):
        return ""
    d = os.path.join(os.path.dirname(os.path.abspath(path)), "_backup")
    os.makedirs(d, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    # 带微秒：两次备份落在同一秒（演练/回滚脚本里很常见）时不能互相覆盖
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = os.path.join(d, f"{stem}.{ts}.json")
    shutil.copy2(path, dest)
    keep = int(keep if keep is not None else (CFG.get("backup_keep") or 30))
    files = sorted(glob.glob(os.path.join(d, f"{stem}.*.json")))
    for old in files[:-keep] if keep > 0 else files:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def list_backups(path: str = None) -> list:
    """列出该账本的备份文件（按时间升序）。"""
    path = path or STATE_PATH
    d = os.path.join(os.path.dirname(os.path.abspath(path)), "_backup")
    stem = os.path.splitext(os.path.basename(path))[0]
    return sorted(glob.glob(os.path.join(d, f"{stem}.*.json")))


_BACKUP_TS_RE = re.compile(r"\.(\d{8}-\d{6}-\d{6})\.json$")


def backup_created_at(path: str) -> dt.datetime:
    """备份的生成时间：优先取文件名里的时间戳（定长格式、排序即时间序），解析不出退回 mtime。"""
    m = _BACKUP_TS_RE.search(os.path.basename(path))
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S-%f")
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(os.path.getmtime(path))


def prune_backups(path: str = None, keep: int = None, keep_days: int = None,
                  dry: bool = False, now: dt.datetime = None) -> dict:
    """清掉过期/超量的备份，返回哪些被删、剩几份。

    2026-11-17：「周决策顺便删除上周的备份账本」；同日澄清口径「删上一周所有的账本，
    就是 14-8 天前」→ **只留最近 7 天**（删 8 天前及更早的，上一周整批清掉）。
    两条闸门同时生效（都来自 config.PAPER，调用方通常不传参）：
      ① 保留天数 backup_keep_days（默认 7 天）：早于 now − keep_days 的备份删掉；
      ② 份数上限 backup_keep（默认 30）：先按天数删完，剩下的仍然超过上限就删最旧的。
    **最新一份永不删**：哪怕很久没跑过决策、唯一那份也已过期，也留着——
    回滚能力不能因为「久久没决策」被清成 0（那样正好在最需要回滚时没有备份）。
    `dry=True` 只算不删（人工核对用）。返回
    {"deleted", "expired", "over_count", "kept", "keep", "keep_days"}，路径按时间升序。
    """
    path = path or STATE_PATH
    keep = int(keep if keep is not None else (CFG.get("backup_keep") or 30))
    keep = max(1, keep)          # 至少留一份，最新那份永远是回滚底线
    keep_days = int(keep_days if keep_days is not None else (CFG.get("backup_keep_days") or 7))
    files = list_backups(path)
    res = {"deleted": [], "expired": [], "over_count": [],
           "kept": len(files), "keep": keep, "keep_days": keep_days}
    if not files:
        return res
    expired = []
    if keep_days > 0:
        cutoff = (now or dt.datetime.now()) - dt.timedelta(days=keep_days)
        # files[-1] 是最新一份：删过期备份时把它排除在外（回滚底线）
        expired = [f for f in files[:-1] if backup_created_at(f) < cutoff]
    rest = [f for f in files if f not in set(expired)]
    over = rest[:-keep]
    victims = expired + over
    if not dry:
        for f in victims:
            try:
                os.remove(f)
            except OSError:
                pass
    res.update({"deleted": victims, "expired": expired, "over_count": over,
                "kept": len(files) - len(victims)})
    return res


def restore_state(state_path: str = None, backup_path: str = None,
                  index: int = -1) -> dict:
    """把账本回滚到某份备份（回滚本身也可逆：会先把当前账本再备份一次）。

    `backup_path` 指定具体备份；否则用 `index` 在 list_backups()（按时间升序）里取，
    默认 -1 = 最近一份。恢复前校验备份结构（缺必需键就拒绝，不拿坏文件覆盖好账本）。
    返回 {"restored_from", "previous_backup", "state"}。
    """
    state_path = state_path or STATE_PATH
    bks = list_backups(state_path)
    if backup_path:
        if not os.path.exists(backup_path):
            raise LedgerError(f"备份不存在：{backup_path}")
        src = backup_path
    else:
        if not bks:
            raise LedgerError("没有可回滚的备份（备份在决策/补跑前自动生成）")
        if not (-len(bks) <= index < len(bks)):
            raise LedgerError(f"备份序号越界：{index}（共 {len(bks)} 份，-1=最近）")
        src = bks[index]
    with open(src, "r", encoding="utf-8") as f:
        st = json.load(f)
    missing = [k for k in ("meta", "cash", "holdings", "trades", "orders", "nav") if k not in st]
    if missing:
        raise LedgerError(f"备份缺少必需键 {missing}，拒绝用它覆盖账本：{src}")
    prev = backup_state(state_path)
    save(st, state_path)
    return {"restored_from": src, "previous_backup": prev, "state": st}


def event(state: dict, level: str, msg: str, date: str = None,
          amount: float = None) -> None:
    """记一条事件。`amount` 只用于可加总的现金层事件（如 cash_yield），便于复盘时
    把「现金利息」和「权益盈亏」拆开报——不许只报总收益（SKILL.md §8.4）。"""
    ev = {"date": date or dt.date.today().isoformat(), "level": level, "msg": msg}
    if amount is not None:
        ev["amount"] = _money(amount)
    state.setdefault("events", []).append(ev)


# ------------------------------------------------------------------ 订单登记
def _next_order_id(state: dict, as_of: str) -> str:
    n = sum(1 for o in state["orders"] if str(o.get("id", "")).startswith(as_of)) + 1
    return f"{as_of}-{n:02d}"


def make_order(state: dict, as_of: str, code: str, name: str, kind: str, action: str,
               shares: int, order_type: str, reason: str, limit_price: float = None,
               effective_from: str = None, expire_on: str = None, batch: int = 1,
               stop_price: float = None, target_price: float = None,
               thesis: str = None, falsifiers: list = None, layer: str = "ETF",
               level: str = None, bracket: bool = False, active: bool = True,
               parent_order_id: str = None) -> dict:
    """登记一张待结算订单（不成交，成交由 resolve_orders 按日线价格做）。

    bracket/active/parent_order_id 是 2026-11-07 新增的括号单字段：
      * `bracket=True` 的卖单是某张买入单的保护止损，`parent_order_id` 指向买入单；
      * `active=False` 表示「父买单还没成交」，**不得触发**；
      * 老账本里的订单没有这三个键 → 读侧一律按 `bracket=False / active=True` 处理
        （`.get("bracket")` / `.get("active", True)`），保证历史结算不受影响。
    """
    if shares <= 0:
        raise LedgerError(f"订单股数必须 >0：{code} {action} {shares}")
    if action not in ("buy", "sell"):
        raise LedgerError(f"未知 action：{action}")
    if order_type not in ("buy_open", "sell_open", "buy_limit", "sell_stop"):
        raise LedgerError(f"未知 order_type：{order_type}")
    o = {
        "id": _next_order_id(state, as_of),
        "created": as_of,
        "code": code,
        "name": name,
        "kind": kind,              # "stock" / "etf"
        "action": action,
        "order_type": order_type,
        "shares": int(shares),
        "limit_price": None if limit_price is None else _px(limit_price),
        "effective_from": effective_from or as_of,   # 最早成交日
        "expire_on": expire_on,
        "batch": batch,
        "reason": reason,
        "layer": layer,
        "level": level,            # 风险等级 R2/R3（组合评审与体检用；缺省 None）
        "stop_price": stop_price,
        "target_price": target_price,
        "thesis": thesis,
        "falsifiers": falsifiers or [],
        "bracket": bool(bracket),
        "active": bool(active),
        "parent_order_id": parent_order_id,
        "status": "pending",
        "filled_date": None,
        "filled_price": None,
        "fee": None,
    }
    state["orders"].append(o)
    return o


def _bracket_children(state: dict, parent_id: str) -> list:
    """某张买入单挂着的括号止损（含已失效的，调用方自己按 status 过滤）。"""
    if not parent_id:
        return []
    return [o for o in state["orders"]
            if o.get("parent_order_id") == parent_id and o.get("bracket")]


def bracket_order(state: dict, parent: dict, as_of: str = None) -> dict | None:
    """给一张计划买入配保护止损（括号单）：买入成交后才激活，未成交则一直冻结。

    为什么需要（2026-11-07 B 类 #10）：决策日下单、次一交易日的巡检会话才结算，中间有空窗；买入若在
    次一交易日成交、再到下一个巡检日才跑 stop_orders，这段跌破止损也没人管。括号单在**下单时**
    就把止损挂上，成交即生效，把空窗填掉。

    约束：只在父单有 `stop_price` 时生成；一个父单只配一张；`limit_price` 先取父单的
    **计划止损占位价**（安全边际买入价×0.85），父单成交时由 `activate_brackets` 按持仓止损
    （实际成交成本×0.85）重锚——2026-11-18 起；`effective_from` 与父单一致（激活时会被
    改写成成交日）；`active=False` 冻结。
    """
    if parent.get("action") != "buy" or parent.get("bracket"):
        return None
    sp = parent.get("stop_price")
    shares = int(parent.get("shares") or 0)
    if not sp or float(sp) <= 0 or shares <= 0:
        return None
    if _bracket_children(state, parent["id"]):
        return None
    o = make_order(state, as_of or parent["created"], parent["code"], parent["name"],
                   parent.get("kind") or "stock", "sell", shares, "sell_stop",
                   f"保护止损（括号单：{parent['id']} 成交后生效）跌破 {float(sp):.3f}",
                   limit_price=float(sp),
                   effective_from=parent.get("effective_from") or (as_of or parent["created"]),
                   expire_on=parent.get("expire_on"), batch=int(parent.get("batch") or 1),
                   stop_price=float(sp), layer=parent.get("layer") or "ETF",
                   level=parent.get("level"), bracket=True, active=False,
                   parent_order_id=parent["id"])
    o["ref_price"] = _money(float(sp))
    event(state, "info", f"为买入单 {parent['id']} 配保护止损 {o['id']}"
                         f"（{float(sp):.3f}，成交后激活）", as_of or parent["created"])
    return o


def activate_brackets(state: dict, parent_id: str, date: str,
                      stop: float = None) -> None:
    """父买单成交 → 激活它的括号止损（生效日 = 成交日）。

    2026-11-18 起：括号单的触发价以**成交后的持仓止损**为准（`stop`，新开仓 =
    成交成本 ×0.85），计划阶段写进父单的 stop_price 只是占位；不传 `stop`（加仓）
    则沿用原限价，避免动已有持仓的决策。
    """
    for c in _bracket_children(state, parent_id):
        if c["status"] == "pending" and not c.get("active", True):
            c["active"] = True
            c["effective_from"] = date
            if stop is not None and float(stop) > 0:
                old = float(c.get("limit_price") or 0)
                c["limit_price"] = _px(float(stop))
                c["stop_price"] = round(float(stop), 3)
                c["ref_price"] = _money(float(stop))
                if abs(float(stop) - old) > 1e-9:
                    event(state, "info",
                          f"括号单 {c['id']} 止损从 {old:.3f} 重锚到 {float(stop):.3f}"
                          f"（按成交成本重算，父单 {parent_id}）", date)
            event(state, "info", f"括号单 {c['id']} 激活（父单 {parent_id} 已成交，"
                                 f"止损 {float(c.get('limit_price') or 0):.3f}）", date)


def cancel_brackets(state: dict, parent_id: str, why: str) -> None:
    """父买单未成交/过期/取消 → 括号止损同步作废（不能留一张裸卖单）。"""
    for c in _bracket_children(state, parent_id):
        if c["status"] == "pending":
            cancel_order(state, c, f"父买入单失效：{why}")


def pending_orders(state: dict, action: str = None) -> list:
    """未成交且**当前有效**的订单（冻结的括号止损不算）。"""
    return [o for o in state["orders"] if o["status"] == "pending"
            and not (o.get("bracket") and o.get("active") is False)
            and (action is None or o["action"] == action)]


def cancel_order(state: dict, order: dict, why: str) -> None:
    order["status"] = "cancelled"
    order["cancel_reason"] = why
    event(state, "warn", f"订单 {order['id']} 取消：{why}", order["created"])
    # 买入单取消 → 它挂的括号止损一并作废（否则会留下一张无持仓可卖的裸卖单）
    if order.get("action") == "buy":
        cancel_brackets(state, order["id"], why)


# ------------------------------------------------------------------ 现金与合并
def reserved_cash(state: dict, exclude_ids=()) -> float:
    """已被挂单占用、但还没成交的现金（按成交价/限价估算，偏保守）。"""
    total = 0.0
    for o in pending_orders(state, "buy"):
        if o["id"] in exclude_ids:
            continue
        px = o.get("limit_price") or o.get("ref_price") or 0.0
        total += float(px) * o["shares"]
    total += otc_book.reserved(state)   # 在挂的场外申购单同样占用现金
    return round(total, 2)


def available_cash(state: dict, exclude_ids=()) -> float:
    """可动用现金 = 现金 − 挂单占用 − 现金缓冲。"""
    buffer = float(state["meta"]["capital"]) * float(CFG["cash_buffer"])
    return round(float(state["cash"]) - reserved_cash(state, exclude_ids) - buffer, 2)


def repo_cap(state: dict) -> float:
    """在途逆回购本金可保留上限 = 可用现金向下取整到 1,000 元整手。

    `cash_yield_plan` 已按可用现金排 lot；本函数用于**收敛旧规则遗留的在途 lot**
    （2026-11-08 A 类：旧口径未扣挂单/场外占款，11-05 的 39,000 元在途超过当日可用现金）。
    到期兑现前先把本金收敛到上限，避免给「账上不存在的钱」计息。
    """
    lot = float((config.cash_rates() or {}).get("repo_min_lot") or 1000.0)
    avail = max(0.0, float(state["cash"]) - reserved_cash(state)
                - float(state["meta"]["capital"]) * float(CFG.get("cash_buffer", 0.02)))
    return round(int(avail / lot + 1e-9) * lot, 2)


def position_exposure(state: dict, pf: dict) -> tuple[float, float]:
    """已成交仓位与「含在挂/场外」的目标敞口（都按净值占比）。

    第一个数 = 持仓市值/净值；第二个数再把在挂买单（按限价/参考价估算）和
    场外在挂申购金额加进来——这些钱还在现金里，但已经是「将要变成仓位」的钱。
    周报/HTML 用它解释「为什么账本看着轻仓」。
    """
    nav = float(pf.get("nav") or 0.0)
    mv = float(pf.get("market_value") or 0.0)
    if nav <= 0:
        return 0.0, 0.0
    return mv / nav, (mv + reserved_cash(state)) / nav


# ------------------------------------------------------------------ 成交
def apply_fill(state: dict, order: dict, date: str, price: float, note: str = "") -> dict:
    """按给定价格成交一张订单：更新现金/持仓/成交流水/订单状态。"""
    if order["status"] != "pending":
        raise LedgerError(f"订单 {order['id']} 状态为 {order['status']}，不能重复成交")
    price = _px(float(price))
    if price <= 0:
        raise LedgerError(f"成交价非法：{price}")
    shares = int(order["shares"])
    amount = _money(price * shares)
    is_etf = order["kind"] == "etf"
    fee = cap.trade_fee(amount, is_etf=is_etf, side=order["action"])["total"]
    code = order["code"]

    if order["action"] == "buy":
        need = _money(amount + fee)
        if need > float(state["cash"]) + 1e-6:
            raise LedgerError(f"现金不足：买 {code} {shares} 股需 {need:.2f}，"
                              f"现有 {float(state['cash']):.2f}")
        state["cash"] = _money(float(state["cash"]) - need)
        h = state["holdings"].get(code)
        is_new = h is None
        if is_new:
            h = {
                "code": code, "name": order["name"], "kind": order["kind"],
                "layer": order.get("layer", "ETF"),
                "level": order.get("level") or "R5",   # 风险等级（缺省按最坏 R5 fail-closed；R2 层为 R2）
                "shares": 0, "cost_basis": 0.0, "opened_at": date,
                "last_buy_date": date, "last_close": price, "highest_close": price,
                "stop_price": None,
                "target_price": order.get("target_price"),
                "thesis": order.get("thesis"),
                "falsifiers": order.get("falsifiers") or [],
            }
            state["holdings"][code] = h
        total_cost = h["shares"] * h["cost_basis"] + amount + fee
        h["shares"] += shares
        h["cost_basis"] = round(total_cost / h["shares"], 6)   # 买入费用摊进成本
        h["last_buy_date"] = date
        # 静态止损锚定「实际成交成本」，不是计划里的安全边际买入价：
        # 计划阶段写进 order.stop_price 的是「安全边际价 ×0.85」，而实盘按市价成交，成交价通常
        # 比安全边际价贵 10%~20%，照抄会让 -15% 硬止损悄悄放大成 -23%~-29%。所以**新开仓**一律
        # 在成交后按「成本 ×(1+initial_stop_pct)」重设；加仓沿用原止损（不改已有持仓的决策，
        # 上移只由移动止损 stop_orders 负责）。
        if is_new:
            init_pct = float(config.EXEC.get("initial_stop_pct", -0.15))
            h["stop_price"] = round(float(h["cost_basis"]) * (1 + init_pct), 3)
            event(state, "info",
                  f"{code} {h['name']} 初始止损 {h['stop_price']:.3f}"
                  f"（成交成本 {float(h['cost_basis']):.3f} × {1 + init_pct:.0%}）", date)
        elif order.get("stop_price") and h["stop_price"] is None:
            h["stop_price"] = order["stop_price"]
        trade = {"date": date, "code": code, "name": order["name"], "kind": order["kind"],
                 "action": "buy", "price": _px(price), "shares": shares,
                 "amount": amount, "fee": fee, "realized_pnl": None,
                 "order_id": order["id"], "note": note}
    else:
        h = state["holdings"].get(code)
        if h is None or h["shares"] < shares:
            raise LedgerError(f"持仓不足：{code} 想卖 {shares}，实际 "
                              f"{0 if h is None else h['shares']}")
        proceeds = _money(amount - fee)
        cost = h["cost_basis"] * shares
        realized = round(proceeds - cost, 2)
        state["cash"] = _money(float(state["cash"]) + proceeds)
        h["shares"] -= shares
        if h["shares"] == 0:
            del state["holdings"][code]
        trade = {"date": date, "code": code, "name": order["name"], "kind": order["kind"],
                 "action": "sell", "price": _px(price), "shares": shares,
                 "amount": amount, "fee": fee, "realized_pnl": realized,
                 "cost_basis": round(cost, 2),
                 "pnl_pct": round(realized / cost, 6) if cost else None,
                 "order_id": order["id"], "note": note}
    state["trades"].append(trade)
    order["status"] = "filled"
    order["filled_date"] = date
    order["filled_price"] = _px(price)
    order["fee"] = fee
    # 买入成交 → 括号止损从「冻结」转「生效」；卖出成交不触发任何级联
    if order["action"] == "buy":
        activate_brackets(state, order["id"], date,
                          stop=h.get("stop_price") if is_new else None)
    return trade


def _affordable_shares(state: dict, price: float, exclude_ids=()) -> int:
    cash = available_cash(state, exclude_ids=exclude_ids)
    if cash <= 0:
        return 0
    # 留出这笔交易的费用（最少 5 元），避免「刚好买满」后现金为负
    return _shares_for_amount(cash - 5.0, price)


def _board_locked(bar: dict, prev_close: float) -> int:
    """当日是否全日封死在涨/跌停价上（一字板）。返回 +1 涨停封死 / -1 跌停封死 / 0 正常。

    判据：当日最高 == 最低（全天没有区间），且收盘相对前收的幅度正好落在某一档涨跌停
    （5% / 10% / 20% / 30%）上。停牌（无成交）虽然也 high==low，但涨跌幅接近 0，不受影响。

    用途：一字跌停的票当天卖不出去、一字涨停的票当天买不到，撮合时不能用「开盘价」
    假装成交（否则纸面账本会出现「跌停当天精准跑掉」这种不真实的乐观）。
    """
    if not prev_close or prev_close <= 0:
        return 0
    try:
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return 0
    if hi != lo:
        return 0
    chg = close / prev_close - 1.0
    for lim in (0.05, 0.10, 0.20, 0.30):
        if abs(abs(chg) - lim) <= 0.006:
            return 1 if chg > 0 else -1
    return 0


def resolve_orders(state: dict, as_of: str, bars_map: dict) -> list:
    """把所有 pending 订单按日线撮合到 as_of 为止。

    成交口径（全部来自缓存日线，不手填价格）：
      * buy_open  / sell_open  ：最早一个交易日按开盘价成交
                                  （buy_open 仅兼容旧账本；2026-11-18 起 target_orders 只生成 buy_limit）；
      * buy_limit             ：开盘价 ≤ 限价则按开盘价（价格更优），否则最低价触及限价则按限价；
      * sell_stop             ：开盘价 ≤ 触发价则按开盘价（跳空更差），否则最低价触及则按触发价；
      * 到期未触发 → expired；买不起 → cancelled（记录原因，不静默跳过）。
    """
    events = []
    for o in list(state["orders"]):
        if o["status"] != "pending" or o["effective_from"] > as_of:
            continue
        # 冻结的括号止损（父买单还没成交）不得触发；active 缺失按 True 兼容老账本
        if o.get("bracket") and not o.get("active", True):
            continue
        bars = bars_map.get(o["code"]) or []
        window = [b for b in bars if o["effective_from"] <= b["date"] <= as_of]
        prev_close = {}
        for _i, _b in enumerate(bars):
            if _i:
                try:
                    prev_close[_b["date"]] = float(bars[_i - 1]["close"])
                except (KeyError, TypeError, ValueError):
                    pass
        fill = None
        t = o["order_type"]
        if window:
            for b in window:
                locked = _board_locked(b, prev_close.get(b["date"]))
                # 一字跌停卖不出去、一字涨停买不到：跳过当天，顺延到有成交的日子
                if t in ("sell_open", "sell_stop") and locked == -1:
                    continue
                if t in ("buy_open", "buy_limit") and locked == 1:
                    continue
                if t in ("buy_open", "sell_open"):
                    fill = (b["date"], float(b["open"]))
                    break
                limit = float(o["limit_price"])
                o_open = float(b["open"])
                if o_open <= limit:
                    fill = (b["date"], o_open)
                    break
                if float(b["low"]) <= limit:
                    fill = (b["date"], limit)
                    break
        if fill is None:
            if o.get("expire_on") and as_of > o["expire_on"]:
                o["status"] = "expired"
                events.append(("expired", f"{o['code']} {o['name']} 条件单到期未触发（{o['order_type']}）"))
                if o.get("action") == "buy":
                    cancel_brackets(state, o["id"], "买入条件单到期未触发")
            continue
        date, price = fill
        if o["action"] == "buy":
            afford = _affordable_shares(state, price, exclude_ids={o["id"]})
            if afford < LOT:
                cancel_order(state, o, f"现金不足：按 {price:.3f} 元买 1 手需 {_lot(price):.2f} 元")
                events.append(("cancelled", f"{o['code']} {o['name']} 买不起 1 手"))
                continue
            if afford < o["shares"]:
                old = o["shares"]
                o["shares"] = afford
                event(state, "warn", f"订单 {o['id']} 股数从 {old} 降到 {afford}（现金只够这些）", date)
        apply_fill(state, o, date, price, note=f"{o['order_type']} 撮合")
        events.append(("filled", f"{date} {o['action']} {o['code']} {o['name']} "
                                 f"{o['shares']} 股 @ {price:.3f}"))
    return events


def execute_at_close(state: dict, close_map: dict, as_of: str, orders: list,
                     why: str = "决策日收盘价成交") -> list:
    """把刚生成的订单按「as_of 当日收盘价」立即成交（模拟盘口径，见 config.PAPER.fill_price）。

    用途：决策在收盘后做、账本也是模拟盘，所以买入价直接取决策日收盘价，
    不做「下一交易日开盘价」（避免隔夜跳空，也避免次日才知道成交价的错位）。
    注意：这里会把传进来的 pending 单一律按收盘价成交——包括三批梯子里的 buy_limit，
    因为「按决策日收盘价买」意味着不再等更低的价，目标股数当期一次买满。
      * 只用 close_map 里已有的收盘价，取不到就留 pending（不瞎填价格）；
      * 买不起 1 手 → cancelled 并写原因；现金只够部分 → 降到买得起的整手数；
      * 卖单同样按收盘价成交（止损/止盈的立即执行部分）。
    """
    events = []
    for o in orders:
        # 括号止损从不在决策日收盘立即成交：它由下一期 resolve_orders 按日线触发，
        # 否则「当天买入 + 当天止损」会因为同一根 K 线低点被误判。
        if o.get("bracket"):
            continue
        if o["status"] != "pending":
            continue
        px = (close_map or {}).get(o["code"])
        if not px:
            continue
        price = _px(px)
        if o["action"] == "buy":
            afford = _affordable_shares(state, price, exclude_ids={o["id"]})
            if afford < LOT:
                cancel_order(state, o, f"现金不足：按 {price:.3f} 元买 1 手需 {_lot(price):.2f} 元")
                events.append(("cancelled", f"{o['code']} {o['name']} 买不起 1 手"))
                continue
            if afford < o["shares"]:
                old = o["shares"]
                o["shares"] = afford
                event(state, "warn", f"订单 {o['id']} 股数从 {old} 降到 {afford}（现金只够这些）", as_of)
        apply_fill(state, o, as_of, price, note=why)
        events.append(("filled", f"{as_of} {o['action']} {o['code']} {o['name']} "
                                 f"{o['shares']} 股 @ {price:.3f}（{why}）"))
    return events


# ------------------------------------------------------------------ 风险动作
def current_drawdown(state: dict) -> float | None:
    """当前回撤（负数）。**空净值序列返回 None = 无法判定**，调用方不得当成 0。

    新账本第一天没有净值点，回撤确实无从算起 —— 返回 0 会让「组合熔断」看起来
    永远安全，所以这里显式返回 None。
    """
    navs = [n["nav"] for n in state.get("nav", [])]
    if not navs:
        return None
    peak = max(navs)
    return navs[-1] / peak - 1 if peak else 0.0


def _has_stop_only(state: dict, code: str) -> bool:
    """该代码是否只有 GTC 止损单、没有开盘卖出单（有 GTC 时跌破止损要换成开盘卖出）。"""
    sells = [o for o in pending_orders(state, "sell") if o["code"] == code]
    if not sells:
        return False
    return all(o["order_type"] == "sell_stop" for o in sells)


def _exit_covered(state: dict, code: str) -> int:
    """该代码已挂的「非 GTC 止损」卖出股数。

    只算 sell_open / 限价卖单：GTC 止损单（sell_stop）是盘中条件单，
    跌停跌破时必须换成「开盘全卖」，不能当成「已经安排好了」。
    """
    return sum(int(o.get("shares") or 0) for o in pending_orders(state, "sell")
               if o["code"] == code and o["order_type"] != "sell_stop")


def _cancel_open_sells(state: dict, code: str, reason: str) -> None:
    """撤掉该代码所有「非 GTC 止损」的卖出单（要改成一次性全卖时用，防止零碎单叠着卖）。"""
    for o in list(pending_orders(state, "sell")):
        if o["code"] == code and o["order_type"] != "sell_stop":
            cancel_order(state, o, reason)


def _cancel_stops(state: dict, code: str, reason: str) -> None:
    """撤掉某只的 GTC 条件止损单（要改用开盘价卖出时先撤，否则会重复卖）。"""
    for o in list(pending_orders(state, "sell")):
        if o["code"] == code and o["order_type"] == "sell_stop":
            cancel_order(state, o, reason)


def stop_orders(state: dict, as_of: str, close_map: dict, atr_map: dict = None,
                fill_from: str = None) -> list:
    """止损三件事：①初始/移动止损 ②组合回撤熔断 ③止损失效后的现金不补偿。

    移动止损规则与 SKILL.md 一致：浮盈 >10% 才启用，止损上移到「**当日收盘** − 2.5×ATR14」，
    只上移不下移。`highest_close` 只做展示/回撤参考，不参与止损计算（旧账本字段，保留兼容）。
    fill_from 是「最早成交日」（决策日之后的第一个交易日），不允许当天开盘成交（那是未来函数）。
    """
    atr_map = atr_map or {}
    fill_from = fill_from or _add_trading_days(as_of, 1)
    out = []
    existing_sell = {o["code"] for o in pending_orders(state, "sell")}
    dd = current_drawdown(state)
    halt = (dd is not None) and (dd <= float(config.EXEC["portfolio_dd_halt"]))
    for code, h in list(state["holdings"].items()):
        close = float(close_map.get(code) or h.get("last_close") or 0.0)
        if close <= 0:
            continue
        h["last_close"] = close
        h["highest_close"] = max(float(h.get("highest_close") or 0), close)
        gain = close / h["cost_basis"] - 1 if h["cost_basis"] else 0.0
        atr = float(atr_map.get(code) or 0.0)
        # 初始止损兜底：没有 stop_price 的持仓（历史账本 / 手工补录 / 场外转来）按成本×(1+initial_stop_pct)
        # 补一条，否则它会一路没有 GTC 止损单、也不参与跌破判断（2026-10-30 审计 #4）。
        if h.get("stop_price") is None and h["cost_basis"]:
            init_pct = float(CFG.get("initial_stop_pct", -0.15))
            h["stop_price"] = round(float(h["cost_basis"]) * (1 + init_pct), 3)
            event(state, "info", f"{code} {h['name']} 补初始止损 {h['stop_price']:.3f}"
                                 f"（成本 {float(h['cost_basis']):.3f} ×{1 + init_pct:.0%}，原账本无止损价）", as_of)
        if gain >= float(CFG.get("trailing_start", 0.10)) and atr > 0:
            new_stop = close - float(config.EXEC["atr_mult"]) * atr
            if h.get("stop_price") is None or new_stop > float(h["stop_price"]):
                old = h.get("stop_price")
                h["stop_price"] = round(new_stop, 3)
                event(state, "info", f"{code} {h['name']} 移动止损上移 "
                                     f"{'-' if old is None else f'{old:.3f} → '}{new_stop:.3f}（浮盈 {gain:.1%}）", as_of)
        stop = h.get("stop_price")
        if halt:
            if _exit_covered(state, code) < int(h["shares"]):
                _cancel_stops(state, code, "组合熔断，改为开盘全部卖出")
                _cancel_open_sells(state, code, "组合熔断，改为一次性全卖")
                o = make_order(state, as_of, code, h["name"], h["kind"], "sell", h["shares"],
                               "sell_open", f"组合回撤 {dd:.1%} 触发熔断线 "
                                            f"{config.EXEC['portfolio_dd_halt']:.0%}，全部减仓",
                               effective_from=fill_from, layer=h.get("layer", "ETF"), level=h.get("level"))
                out.append(o)
                existing_sell.add(code)
                event(state, "risk", f"{code} {h['name']} 触发组合熔断，全部卖出", as_of)
            continue
        if stop and close <= float(stop):
            if _exit_covered(state, code) < int(h["shares"]):
                _cancel_stops(state, code, "收盘跌破止损，改为开盘卖出")
                _cancel_open_sells(state, code, "收盘跌破止损，改为一次性全卖")
                o = make_order(state, as_of, code, h["name"], h["kind"], "sell", h["shares"],
                               "sell_open", f"收盘 {close:.3f} 跌破止损价 {float(stop):.3f}",
                               effective_from=fill_from, layer=h.get("layer", "ETF"), level=h.get("level"))
                out.append(o)
                existing_sell.add(code)
                event(state, "risk", f"{code} {h['name']} 跌破止损 {float(stop):.3f}（收盘 {close:.3f}）", as_of)
            continue
        # 未跌破止损 → 维持一张 GTC 条件止损单，让两次运行之间的盘中跌破也能成交
        # （注意：GTC 单本身就在 pending 里，所以「更新」分支必须排在「已存在卖单」判断之前）
        if stop:
            gtc = next((o for o in pending_orders(state, "sell")
                        if o["code"] == code and o["order_type"] == "sell_stop"), None)
            if gtc is not None:
                changed = False
                if abs(float(gtc.get("limit_price") or 0) - float(stop)) > 1e-9:
                    gtc["limit_price"] = round(float(stop), 3)
                    gtc["stop_price"] = round(float(stop), 3)
                    gtc["reason"] = f"条件止损上移到 {float(stop):.3f}（GTC）"
                    event(state, "info", f"{code} {h['name']} 条件止损单更新为 {float(stop):.3f}", as_of)
                    changed = True
                if int(gtc.get("shares") or 0) != int(h["shares"]):
                    gtc["shares"] = int(h["shares"])
                    changed = True
                # 价格/股数变了 = 新参数只对未来有效：effective_from 顺延到下一交易日。
                # 否则撮合会拿「今天才上移的止损价」去重放过去两周的日线，凭空成交
                # （2026-11-21 审计：601058 止损从 8.17 上移到 11.623 后，历史窗口会
                #  在 11-02 以 11.13 补一笔卖出——未来函数）。
                if changed:
                    gtc["effective_from"] = fill_from
            elif code not in existing_sell:
                o = make_order(state, as_of, code, h["name"], h["kind"], "sell", h["shares"],
                               "sell_stop", f"条件止损：盘中触及 {float(stop):.3f} 卖出（GTC）",
                               limit_price=float(stop), effective_from=fill_from,
                               stop_price=float(stop), layer=h.get("layer", "ETF"), level=h.get("level"))
                out.append(o)
                existing_sell.add(code)
    return out


def consolidate_stop_orders(state: dict, as_of: str = "", effective_from: str = None) -> list:
    """把同一持仓的多张 GTC 止损合并成一张、覆盖全部股数（只上移）。

    为什么会出现多张：加仓成交时括号止损被激活（只覆盖新增股数），而原来的
    GTC 止损仍按加仓前的股数挂着 → 同一持仓两张 sell_stop、合计股数超过持仓
    （账面 300 股 vs 持仓 200 股）。真要跌破时第二张会在 `apply_fill` 抛
    「持仓不足」，把整次决策运行打挂；就算不报错也会卖超。

    规则与移动止损一致：**只上移不下移**。每只只留价格最高的那一张，股数改写成
    持仓股数，其余撤单；`holding.stop_price` 同步到该价格。返回可读消息（周报/告警用）。
    """
    out = []
    for code, h in list((state.get("holdings") or {}).items()):
        shares = int(h.get("shares") or 0)
        if shares <= 0:
            continue
        stops = [o for o in pending_orders(state, "sell")
                 if o["code"] == code and o["order_type"] == "sell_stop"]
        if not stops:
            continue
        cur = float(h.get("stop_price") or 0.0)
        prices = [float(o.get("stop_price") or o.get("limit_price") or 0.0) for o in stops]
        target = max([cur] + prices)
        covered = sum(int(o.get("shares") or 0) for o in stops)
        if len(stops) == 1 and covered == shares and abs(target - cur) <= 1e-9:
            continue
        keep = max(stops, key=lambda o: (float(o.get("stop_price") or o.get("limit_price") or 0.0),
                                         str(o.get("id") or "")))
        for o in stops:
            if o is keep:
                continue
            cancel_order(state, o, f"止损合并：{code} 只保留一张覆盖全部 {shares} 股")
        keep["shares"] = shares
        keep["stop_price"] = round(target, 3)
        keep["limit_price"] = round(target, 3)
        keep["reason"] = f"条件止损（合并重复单）：盘中触及 {target:.3f} 卖出全部 {shares} 股（GTC）"
        # 合并 = 参数变了（张数/股数/价格），只对未来生效，不能拿它重放历史窗口。
        if effective_from:
            keep["effective_from"] = max(str(keep.get("effective_from") or ""), str(effective_from))
        h["stop_price"] = round(target, 3)
        msg = (f"{code} {h.get('name') or ''} 止损合并：{len(stops)} 张 → 1 张，"
               f"覆盖 {shares} 股 @{target:.3f}")
        out.append(msg)
        event(state, "info", msg, as_of or str(keep.get("created") or ""))
    return out


# ------------------------------------------------------------------ 目标组合 → 订单
def month_trade_count(state: dict, as_of: str) -> int:
    """本月已发生的买入笔数（已成交 + 在挂），供 `max_trades_per_month` 使用。

    只数买入：卖出（含止损/熔断）不计入换手上限——该撤的时候必须能撤，否则
    「每月 ≤2 次」会变成「跌了也不许卖」。同一笔成交与它的订单不会重复计数
    （成交单状态是 filled，这里只补 pending 的新单）。
    """
    ym = str(as_of)[:7]
    n = sum(1 for t in (state.get("trades") or [])
            if str(t.get("date") or "").startswith(ym) and t.get("action") == "buy")
    n += sum(1 for o in (state.get("orders") or [])
             if str(o.get("created") or "").startswith(ym)
             and o.get("action") == "buy" and o.get("status") == "pending")
    return n


def target_orders(state: dict, targets: list, close_map: dict, as_of: str,
                  min_trade_value: float = None, no_trade_band: float = None,
                  fill_from: str = None, calendar: list = None,
                  mode: str = "full", bad_codes: list = None,
                  month_trades: int = 0) -> list:
    """把「阶段三给出的目标整手数」翻译成买卖单。

    targets: [{"code","name","kind","layer","shares","price","stop_price","target_price",
               "thesis","falsifiers","ladder":[{tranche,price,shares},...]}]
    fill_from: 最早成交日（决策日之后的第一个交易日），避免用决策日当天开盘价成交。
    mode:
      full        = 每周按目标全额再平衡（回测年化转负，不采用）
      incremental = 只在「有必要」时动（2026-10-25+ 回测证据）：
                    持有中的标的，只要没被判「回避/数据不足」（bad_codes），
                    即使这周没进目标组合也继续持有，不因为排名变化卖出；
                    只有 bad_codes 里的、或目标股数下调的才卖。
      once        = 只建仓一次，之后不再因排名/目标变化买卖（2026-10-25 回测证据：
                    小本金下「建仓一次 + 止损」-4.47%/6 笔，事件驱动 -26.39%/390 笔，
                    差别全在换手成本）；只靠 stop_orders 的止损/熔断撤资。
    规则：
      1. 清仓：full 下是「不在目标里的全部卖」；incremental 下只卖 bad_codes；once 下不卖；
      2. 新开仓 → 按三批**全部挂限价条件单**（限价 = 该批触发价——第 1 批 = min(现价, 买入价×1.02)，
         第 2/3 批 = 买入价×0.95/0.90；`limit_expire_days` 个交易日后失效；2026-11-18
         「以后都用挂单买」）：不会追开盘价（旧口径实测 5/5 成交价高于计划价 8%–16.7%）；
         开盘价 ≤ 限价时按开盘价（价格更优）成交，否则等盘中触价；
      3. 已持有 → 只有市值偏离超过 no_trade_band 才调，且单笔 ≥ min_trade_value；
         加仓单同样是限价条件单（限价 = 决策日收盘价，跌回该价才买）；
      4. 现金不够时按「第 1 批优先、金额大的优先」保留，砍最后的买单（不砍止损单）。
    """
    bad = {str(c) for c in (bad_codes or [])}
    min_trade_value = float(min_trade_value if min_trade_value is not None
                            else CFG["min_trade_value"])
    no_trade_band = float(no_trade_band if no_trade_band is not None
                          else CFG["no_trade_band"])
    fill_from = fill_from or _add_trading_days(as_of, 1, calendar)
    expire = _add_trading_days(fill_from, int(CFG["limit_expire_days"]), calendar)
    orders, existing_sell = [], {o["code"] for o in pending_orders(state, "sell")}
    target_by_code = {t["code"]: t for t in targets}
    # 已挂未成交的买单必须先扣掉，否则「持仓 100 + 挂着 200 限价」还会再下 200，
    # 全部成交就超出目标股数（2026-10-25 回放实测复现）。
    pending_buy = {}
    for o in pending_orders(state, "buy"):
        pending_buy[o["code"]] = pending_buy.get(o["code"], 0) + int(o["shares"] or 0)

    # 未知模式必须显式报错：2026-10-26 审计发现 weekly.py 曾把 "regime_change" 原样传进来，
    # 这里没有对应分支 → 静默落入 full 语义（状态没变也卖旧买新），与报告文案直接冲突。
    if mode not in ("full", "incremental", "once"):
        raise ValueError(f"未知调仓模式「{mode}」：target_orders 只接受 full/incremental/once；"
                         f"regime_change 的「本期不调仓」由 weekly.py 在调用前直接跳过")

    # 月度换手上限（2026-11-07 B 类 #3）：只约束 incremental 的**再平衡新买单**。
    # 首次建仓（账本还没有持仓）不受限——那是配置动作，不是换手；止损卖出、regime_change
    # 重建（weekly 走 full）也不受限，否则「该撤的时候撤不掉」。
    cap_n = int(CFG.get("max_trades_per_month") or 0)
    used = int(month_trades or 0)
    cap_on = bool(cap_n and mode == "incremental" and state.get("holdings"))

    def _buy_allowed(code: str) -> bool:
        nonlocal used
        if not cap_on:
            return True
        if used >= cap_n:
            event(state, "warn",
                  f"本月换手已达上限 {cap_n} 次，跳过 {code} 的新买单（下月再评估）", as_of)
            return False
        used += 1
        return True

    # 0) once 模式：只要已经建过仓（有过成交 或 还挂着买单），本期一张新单都不下，
    #    也不撤挂单、不因排名变化卖出；撤资只走 stop_orders（止损/熔断）。
    if mode == "once" and (state["trades"] or pending_buy):
        event(state, "info", "once 模式：已建仓，本期不做再平衡（只保留止损/熔断）", as_of)
        return []

    # 1) 清仓：full=不在目标里的全卖；incremental=只卖 bad_codes（回避/数据不足）
    for code, h in list(state["holdings"].items()):
        if code in existing_sell:
            continue
        if mode == "incremental":
            if code not in bad:
                if code not in target_by_code:
                    event(state, "info",
                          f"{code} 这周没进目标组合，但没被判回避，继续持有（{h['shares']} 股）", as_of)
                continue
            reason = "阶段二判为回避/数据不足，撤出"
        else:
            if code in target_by_code:
                continue
            reason = "不在本期目标组合（阶段三未选中）"
        close = float(close_map.get(code) or h.get("last_close") or 0.0)
        if close <= 0:
            continue
        value = close * h["shares"]
        if value < min_trade_value:
            event(state, "info", f"{code} 应撤出，但市值 {value:.0f} 元 < {min_trade_value:.0f} 元下限，暂不卖", as_of)
            continue
        _cancel_stops(state, code, "改为开盘卖出")
        orders.append(make_order(state, as_of, code, h["name"], h["kind"], "sell",
                                 h["shares"], "sell_open", reason, layer=h.get("layer", "ETF"), level=h.get("level")))

    # 1b) 不在目标里的代码，把还挂着的买单撤掉（否则上期的限价条件单本期仍会成交）
    for o in list(pending_orders(state, "buy")):
        if o["code"] not in target_by_code:
            cancel_order(state, o, "本期不在目标组合，撤销挂单")
            event(state, "info", f"撤销 {o['code']} {o['name']} 的挂单 {o['id']}（不在本期目标组合）", as_of)

    # 2) 逐个目标：新开仓走三批，已持有看偏离
    for t in sorted(targets, key=lambda x: float(x.get("weight") or 0), reverse=True):
        code = t["code"]
        close = float(close_map.get(code) or t.get("price") or 0.0)
        if close <= 0:
            event(state, "warn", f"{code} {t['name']} 没有价格，跳过", as_of)
            continue
        cur = state["holdings"].get(code)
        cur_shares = cur["shares"] if cur else 0
        tgt_shares = int(t.get("shares") or 0)
        if tgt_shares <= 0:
            continue
        pend = pending_buy.get(code, 0)
        diff = tgt_shares - cur_shares - pend
        if diff < 0 and pend > 0:
            # 目标下调：先撤还没成交的买单，再谈卖出，不要「一边撤单一边卖」
            for o in list(pending_orders(state, "buy")):
                if o["code"] == code:
                    cancel_order(state, o, "目标股数下调，撤销挂单")
            diff = tgt_shares - cur_shares
            pend = 0
        cur_val, tgt_val = cur_shares * close, tgt_shares * close
        if diff != 0 and (cur_shares > 0 or pend > 0):
            if tgt_val > 0 and abs(tgt_val - cur_val) / tgt_val < no_trade_band:
                continue
            if abs(diff) * close < min_trade_value:
                continue
        if diff > 0:
            if cur_shares == 0 and pend == 0 and t.get("ladder"):
                ladder = [b for b in t["ladder"] if int(b.get("shares") or 0) > 0]
                for b in ladder:
                    if not _buy_allowed(code):
                        break
                    batch = int(b.get("tranche") or 1)
                    px = float(b.get("price") or close)
                    common = dict(limit_price=px, effective_from=fill_from,
                                  stop_price=t.get("stop_price"),
                                  target_price=t.get("target_price"),
                                  thesis=t.get("thesis"), falsifiers=t.get("falsifiers"),
                                  layer=t.get("layer", "ETF"), level=t.get("level"))
                    if batch == 1:
                        # 2026-11-18：买入一律挂限价条件单，不吃开盘价。
                        # 旧口径（现价≤计划价时按次日开盘买）实测 5/5 成交价高于计划价
                        # 8%–16.7%，成本被抬高、止损锚点也被动跟着抬高；改为限价后成交价
                        # 永远 ≤ 计划价。代价是可能不成交（10 个交易日后失效）。
                        o = make_order(state, as_of, code, t["name"], t["kind"], "buy",
                                       int(b["shares"]), "buy_limit",
                                       f"第 1 批（{float(b.get('ratio') or 0.40):.0%}，"
                                       f"限价 {px:.3f} 才买）：{t.get('reason') or '阶段三目标组合'}",
                                       expire_on=expire, batch=batch, **common)
                    else:
                        o = make_order(state, as_of, code, t["name"], t["kind"], "buy",
                                       int(b["shares"]), "buy_limit",
                                       f"新开仓第 {batch} 批（条件单：跌到 {px:.3f} 才买）",
                                       expire_on=expire, batch=batch, **common)
                    o["ref_price"] = _money(px)
                    orders.append(o)
            else:
                if not _buy_allowed(code):
                    continue
                # 2026-11-18：加仓也用限价条件单（限价 = 决策日收盘价），
                # 跌回该价才买，不吃开盘价；到期未成交自动失效。
                o = make_order(state, as_of, code, t["name"], t["kind"], "buy", diff, "buy_limit",
                               f"加仓到目标整手数（{cur_shares}→{tgt_shares} 股，"
                               f"限价 {_money(close):.3f} 才买）：{t.get('reason') or '阶段三目标组合'}",
                               limit_price=_money(close), expire_on=expire,
                               effective_from=fill_from, stop_price=t.get("stop_price"),
                               target_price=t.get("target_price"),
                               thesis=t.get("thesis"), falsifiers=t.get("falsifiers"),
                               layer=t.get("layer", "ETF"), level=t.get("level"))
                o["ref_price"] = _money(close)
                orders.append(o)
        elif diff < 0:
            _cancel_stops(state, code, "目标股数下调，改为开盘减仓")
            orders.append(make_order(state, as_of, code, t["name"], t["kind"], "sell",
                                     abs(diff), "sell_open",
                                     f"减仓到目标整手数（{cur_shares}→{tgt_shares} 股）",
                                     layer=t.get("layer", "ETF"), level=t.get("level")))
    # 4) 每张计划买入同步挂括号保护止损（2026-11-07 B 类 #10）：
    #    下单即挂、成交才激活；买入未成交/过期/取消由 cancel_brackets 级联作废。
    for o in list(orders):
        if o.get("action") == "buy":
            bracket_order(state, o, as_of)
    return orders


def cap_trim_orders(state: dict, cap_amount: float, close_map: dict, as_of: str,
                    priority: dict = None, fill_from: str = None,
                    min_trade_value: float = None) -> list:
    """仓位上限保护（incremental 模式用）：当前权益市值超过上限时，按最差的先卖。

    用户口径：平时不折腾，但「超出市场状态允许的仓位」属于必须撤资的情形。
    priority: {code: score}，分数越低越先卖；缺省按市值从大到小。
    """
    cap_amount = float(cap_amount or 0.0)
    if cap_amount <= 0:
        return []
    min_trade_value = float(min_trade_value if min_trade_value is not None
                            else CFG["min_trade_value"])
    priority = priority or {}
    rows = []
    for code, h in state["holdings"].items():
        close = float(close_map.get(code) or h.get("last_close") or 0.0)
        if close <= 0:
            continue
        rows.append((code, h, close, close * h["shares"]))
    equity = sum(r[3] for r in rows)
    tol = float(CFG.get("cap_tolerance") or 0.05)
    if equity - cap_amount <= max(cap_amount * tol, min_trade_value):
        return []
    excess = equity - cap_amount
    rows.sort(key=lambda r: (priority.get(r[0], 0.0), -r[3]))
    orders = []
    for code, h, close, value in rows:
        if excess <= 0:
            break
        lots = int(h["shares"] // LOT)
        if lots <= 0:
            continue
        need_shares = int(math.ceil(excess / close / LOT) * LOT)
        sell_shares = min(need_shares, lots * LOT)
        if sell_shares <= 0 or sell_shares * close < min_trade_value:
            continue
        _cancel_stops(state, code, "超出仓位上限，改为开盘减仓")
        orders.append(make_order(state, as_of, code, h["name"], h["kind"], "sell",
                                 sell_shares, "sell_open",
                                 f"超出市场状态允许的仓位上限（{cap_amount:.0f} 元），减仓 {sell_shares} 股",
                                 effective_from=fill_from, layer=h.get("layer", "ETF"), level=h.get("level")))
        excess -= sell_shares * close
    if orders:
        event(state, "warn", f"仓位超上限：权益 {equity:.0f} 元 > {cap_amount:.0f} 元，减仓 {len(orders)} 笔", as_of)
    return orders


def equity_value(state: dict, close_map: dict) -> float:
    """场内权益市值（不含场外腿）：持仓股数 × 当日收盘价，缺价用 last_close。"""
    total = 0.0
    for code, h in state["holdings"].items():
        close = float(close_map.get(code) or h.get("last_close") or 0.0)
        total += close * int(h.get("shares") or 0)
    return round(total, 2)


def cap_topup_orders(state: dict, targets: list, close_map: dict, cap_amount: float,
                     as_of: str, fill_from: str = None, calendar: list = None,
                     bad_codes: list = None, month_trades: int = 0,
                     min_topup: float = None, buffer: float = None) -> list:
    """市场状态未变时的「补建仓」：只买不卖，把权益补到 cap_amount。

    2026-11-15口径（B 类授权）：状态没变 ≠ 永远不建仓——闲置现金太多
    本身就是风险（现金层跑不过存款）。规则边界：
      * 只买不卖：卖出只由止损/熔断/超仓位减仓触发，避免每周来回换手；
      * 累计买入 ≤（权益上限 − 现有权益）×（1−buffer），留缓冲防超配；
      * 沿用 target_orders 的整手/三批/现金约束，不追高。
    返回保留的买单；不该做的候选单（卖单、超上限的买单）已就地取消。
    """
    cap_amount = float(cap_amount or 0.0)
    if cap_amount <= 0:
        return []
    headroom = cap_amount - equity_value(state, close_map)
    min_topup = float(min_topup if min_topup is not None
                      else CFG.get("min_topup_value") or 1000.0)
    if headroom < min_topup:
        return []
    buffer = float(buffer if buffer is not None else CFG.get("topup_buffer") or 0.02)
    cand = target_orders(state, targets, close_map, as_of, fill_from=fill_from,
                         calendar=calendar, mode="incremental", bad_codes=bad_codes,
                         month_trades=month_trades)
    limit = headroom * (1.0 - buffer)
    keep, cum = [], 0.0
    for o in cand:
        amt = float(o.get("ref_price") or o.get("limit_price") or 0.0) * int(o.get("shares") or 0)
        if o.get("action") != "buy":
            cancel_order(state, o, "补建仓只买不卖（状态未变，卖出只走止损/熔断/超限减仓）")
            continue
        if cum + amt > limit + 1e-9:
            cancel_order(state, o, f"补建仓受权益上限约束（剩余空间 {headroom:,.0f} 元）")
            continue
        keep.append(o)
        cum += amt
    if keep:
        event(state, "info",
              f"状态未变但权益低于上限 → 补建仓 {len(keep)} 张（约 {cum:,.0f} 元，只买不卖）", as_of)
    return keep


def enforce_cash(state: dict, new_orders: list, cash_buffer: float = None) -> list:
    """按「第 1 批优先、金额大的优先」保留买单，现金不够就砍（止损/卖出单不受影响）。

    注意：new_orders 已经登记进 state["orders"]，所以计算可动用现金时必须把它们排除，
    否则会把自己的占用算两遍。
    """
    cash_buffer = float(cash_buffer if cash_buffer is not None
                        else CFG["cash_buffer"])
    buffer = float(state["meta"]["capital"]) * cash_buffer
    cash = float(state["cash"])
    new_ids = {o["id"] for o in new_orders}
    reserved = reserved_cash(state, exclude_ids=new_ids)   # 只算「别的」挂单
    buys = [o for o in new_orders if o["action"] == "buy"]

    def priority(o):
        batch = int(o.get("batch") or 1)
        value = float(o.get("ref_price") or o.get("limit_price") or 0) * o["shares"]
        return (batch, -value)

    dropped = []
    for o in sorted(buys, key=priority):
        px = float(o.get("ref_price") or o.get("limit_price") or 0)
        need = px * o["shares"]
        if cash - reserved - need >= buffer:
            reserved += need
            continue
        cancel_order(state, o, f"现金约束：需要 {need:.0f} 元，可用 {cash - reserved:.0f} 元"
                               f"（保留 {cash_buffer:.0%} 缓冲）")
        dropped.append(o)
    return dropped


# ------------------------------------------------------------------ 现金层（逆回购 + 货基）
def _pool_plan(amount: float, allow_stale: bool = False) -> dict:
    """半流动性货币增值切分（读 cash_pool.py 落的排行榜）。失败一律 fail-closed 返回空。

    allow_stale=True 只给「已有持仓计息」用：排行过期也沿用最后一份持仓，不退回朝朝宝。
    """
    try:
        import cash_sleeve as cs
        return cs.plan(amount, allow_stale=allow_stale)
    except Exception as exc:      # 排行文件缺失/损坏：整笔退回朝朝宝，不阻断结算
        return {"amount": 0.0, "holdings": [], "weighted_rate": 0.0, "residual": amount,
                "fallback": True, "stale": False, "degraded": False, "data_date": "", "as_of": "",
                "per_fund_cap": 10000.0, "top_n": 3,
                "note": f"半流动性切分失败（{type(exc).__name__}: {exc}），整笔退回朝朝宝"}


def cash_yield_plan(state: dict, close_map: dict = None, container: str = None,
                    cap_amount: float = None, allow_stale_sleeve: bool = False) -> dict:
    """现金层分工计划（2026-11-15 起为默认口径）：

      建仓弹药（权益上限 − 现有场内权益）→ 朝朝宝/券商现金（T+0，下周决策立即可用）；
      其中**在挂买单已冻结的部分**（场内挂单占用）本就含在弹药里，两者取较大者、
        不重复扣（「那就是吃不到朝朝宝的利息了」后修正）；
      观望资金（扣掉 2% 缓冲、场外申购占款、建仓弹药/场内挂单的较大者、在途逆回购）
        → **半流动性货币增值**：全市场货基按 90 天实得年化取前 3 支、每支 ≤1 万快赎，
        零头留朝朝宝（不再绑招行朝朝盈2号那 100 支）。

    为什么 T+0 部分要留够：
      决策在周三/周六、买单次一交易日生效，而场外货基赎回 T+1（快赎要人手动操作且未必赶得上开盘）。
      所以「可能变成买单的钱」必须决策日次一交易日前就在券商账户——权益上限与现有权益之间的差额一律留 T+0 工具。
      权益上限优先取参数 `cap_amount`，其次取账本 `meta["equity_cap"]`（周报每期写入）；
      都没有则按 0 处理（旧账本，行为与改动前一致）。

    旧口径 `cash_policy="orders_mf_idle_repo7"` 仍完整保留（观望资金买 7/14/28 天期逆回购），
    把 config 里 `cash_policy` 改回去即回滚；`container="repo"/"money_fund"/"r1"/"auto"`
    仍是回滚/验证口径（这几条不扣建仓弹药，仅用于切换验证）。
    速率全部来自 `config.cash_rates()`；半流动性加权利率来自排行文件（90 天实得年化）。
    """
    close_map = close_map or {}
    buffer = float(state["meta"]["capital"]) * float(CFG.get("cash_buffer", 0.02))
    deployable = max(0.0, float(state["cash"]) - buffer)
    reserved = reserved_cash(state)
    otc_res = round(float(otc_book.reserved(state)), 2)
    inhouse_res = round(max(0.0, reserved - otc_res), 2)   # 场内挂单冻结的现金
    locked = round(float((state.get("repo_open") or {}).get("amount") or 0.0), 2)
    eq_cap = float(cap_amount if cap_amount is not None
                   else (state["meta"].get("equity_cap") or 0.0) or 0.0)
    ammo = max(0.0, eq_cap - equity_value(state, close_map)) if eq_cap > 0 else 0.0
    # 场内挂单的钱成交后就是权益、本来就落在弹药里 → 取较大者，不能相加
    # （相加会把同一笔钱扣两遍，池子少算 ≈ 在挂买单金额）
    t0_keep = max(ammo, inhouse_res)
    # 观望资金 = 可动用 − 在途逆回购 − 场外申购占款 − T+0 保留（弹药/挂单取大）
    # （这些钱下周决策要能立刻变成买单，不能锁在场外货基里等赎回）
    idle = max(0.0, deployable - locked - otc_res - t0_keep)
    cash = float(state["cash"])
    policy = str((config.cash_rates() or {}).get("cash_policy") or "")
    sleeve = {}
    if container in (None, "auto", "policy", "repo7", "pool"):
        sp = cap.cash_split(idle, "repo7")
        # 显式 container="pool" 也强制走池子分支（自测/复盘要看池子切分时用）
        if ("pool" in policy or container == "pool") and container in (None, "auto", "policy", "pool"):
            sleeve = _pool_plan(idle, allow_stale=allow_stale_sleeve)
            repo_amount = 0.0
        else:
            repo_amount = round(sp["repo_amount"], 2)
        _sl_amt = round(sum(float(h["amount"]) for h in (sleeve.get("holdings") or [])), 2)
        money_fund = round(max(0.0, deployable - locked - repo_amount - _sl_amt), 2)
    elif container in ("repo", "money_fund", "r1"):
        sp = cap.cash_split(max(0.0, deployable - locked - reserved), container)
        repo_amount = round(sp["repo_amount"], 2) if container == "repo" else 0.0
        money_fund = round(max(0.0, deployable - locked - repo_amount), 2)
    else:
        raise ValueError(f"未知现金容器 {container!r}（只接受 None/auto/policy/repo7/pool/repo/money_fund/r1）")
    return {"deployable": _money(deployable), "reserved_cash": _money(reserved),
            "idle_base": _money(idle), "repo_base": _money(idle),
            "repo_locked": _money(locked),
            "equity_cap": _money(eq_cap), "ammo": _money(ammo),
            "t0_keep": _money(t0_keep),
            "inhouse_reserved": _money(inhouse_res), "otc_reserved": _money(otc_res),
            "repo_amount": _money(repo_amount),
            "repo_lots": sp["repo_lots"], "money_fund": _money(money_fund),
            "container": sp["container"], "container_requested": sp["requested"],
            "repo_term_days": int(sp.get("repo_term_days") or 7),
            "repo_rate": sp["repo_rate"], "money_fund_rate": sp["money_fund_rate"],
            "repo_fee_rate": sp["repo_fee_rate"],
            "repo_trades_per_year": float(sp["repo_trades_per_year"]),
            "repo_fee_annual_rate": sp.get("repo_fee_annual_rate") or 0.0,
            "repo_commission_discount": sp.get("repo_commission_discount", 1.0),
            "cash_side": sp.get("cash_side"),
            "cash_side_name": sp.get("cash_side_name") or "货基",
            "repo_net_rate": sp["repo_net_rate"],
            "money_fund_net_rate": sp["money_fund_net_rate"],
            "cash_policy": policy,
            # 半流动性货币增值（90 天口径前 N 支、每支 ≤1 万快赎）
            "sleeve_amount": _money(round(sum(float(h["amount"]) for h in
                                              (sleeve.get("holdings") or [])), 2)),
            "sleeve_rate": float(sleeve.get("weighted_rate") or 0.0),
            "sleeve_holdings": list(sleeve.get("holdings") or []),
            "sleeve_data_date": str(sleeve.get("data_date") or ""),
            "sleeve_stale": bool(sleeve.get("stale")),
            "sleeve_degraded": bool(sleeve.get("degraded")),
            "sleeve_fallback": bool(sleeve.get("fallback")),
            "sleeve_note": str(sleeve.get("note") or ""),
            "rate_as_of": sp["rate_as_of"], "rate_source": config.cash_rates()["source"],
            "buffer": _money(buffer)}



def repo_lot_schedule(cal, start: str, end: str, op_days=None, terms=None,
                      term_days: int = None) -> list:
    """把「上次结算日 start → 本期结算日 end」之间的**操作日**拆成逆回购 lot。

    操作日来自 config.PAPER["cash_yield"]["operation_days"]（2026-11-15 起 [0] = 周一）：
定「观望资金买 7 天以上逆回购」，所以每周一买一笔**期限刚好到下一个周一**
    的逆回购（正常 7 天期 R-007；长假 gap 变长时自动选能覆盖的最长档 14/28 天）：

      * 周一买 → 7 天期（R-007），下一个周一到期；
      * 长假前 gap 变长时，选能覆盖的最长档（≤ gap），覆盖不到的部分才是空转；
      * 窗口里最后一笔若到期日超过 end，只当作在途 lot（下次结算兑现，不在末尾截断）。

    返回 [{buy, mature, days, term}]，按 buy 升序。
    """
    cy = CFG.get("cash_yield") or {}
    if op_days is None:
        op_days = cy.get("operation_days") or [0, 3]
    op_days = [int(x) for x in op_days]
    if terms is None:
        terms = sorted(int(k) for k in (cy.get("term_fee_rates") or {})) or [1, 2, 3, 4]
    day_list = [str(x) for x in cal]
    out = []
    for i, t in enumerate(day_list):
        if not (str(start) < t <= str(end)):
            continue
        day = dt.date.fromisoformat(t)
        if day.weekday() not in op_days:
            continue
        nxt = None
        for u in day_list[i + 1:]:
            if dt.date.fromisoformat(u).weekday() in op_days:
                nxt = dt.date.fromisoformat(u)
                break
        if nxt is None:
            # 日历到头了：按下一个符合 operation_days 的自然日推（只为算持有天数）
            for add in range(1, 15):
                cand = dt.date.fromisoformat(day_list[-1]) + dt.timedelta(days=add)
                if cand.weekday() in op_days:
                    nxt = cand
                    break
        if nxt is None:
            continue
        gap = (nxt - day).days
        if term_days:
            #：按金额分档的期限（7/14/28），不必「刚好到下一个操作日」；
            # 在途期间不会重复买入（accrue_cash_lots 已有 prev 打开时不再开新 lot）。
            term = int(term_days)
        else:
            term = max([x for x in terms if x <= gap], default=1)
        mature = (day + dt.timedelta(days=term)).isoformat()
        out.append({"buy": t, "mature": mature, "days": int(term), "term": int(term)})
    return out


def _cash_last_date(state: dict) -> str:
    navs = state.get("nav") or []
    marks = state.get("cash_marks") or {}
    last = max((str(n.get("date")) for n in navs), default=None) or state["meta"].get("baseline_date")
    la = marks.get("last_accrued")
    if la and str(la) > str(last):
        last = str(la)
    return str(last) if last else ""


def _rate_history() -> dict:
    """逐日现金利率历史（cash_rates_history.jsonl）→ {date: record}；缺文件/坏行跳过。"""
    path = os.path.join(config.WORK_DIR, "cash_rates_history.jsonl")
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("date"):
                    out[str(rec["date"])[:10]] = rec
    except OSError:
        pass
    return out


def _rate_on(hist: dict, day: str, key: str) -> float | None:
    """取 day 当日利率；当日缺失时用之前最近一天的（逐日顺延，绝不回头用今天）。"""
    rec = hist.get(day)
    if rec is not None and rec.get(key) is not None:
        return float(rec[key])
    prev = [d for d in hist if d < day and hist[d].get(key) is not None]
    return float(hist[max(prev)][key]) if prev else None


def _day_list(start: str, end: str) -> list[str]:
    """(start, end] 的连续自然日。"""
    s = dt.date.fromisoformat(str(start))
    e = dt.date.fromisoformat(str(end))
    return [(s + dt.timedelta(days=i)).isoformat() for i in range(1, (e - s).days + 1)]


def _wanfen_daily(code: str, amount: float, start: str, end: str):
    """按逐日万份收益计息（每日收益再投入）；缺失日沿用最近已知值。

    返回 (interest|None, covered_days, carried_days)。窗口内完全没有数据 → None，
    由调用方「本笔不计息并告警」——**不允许**用年化 × 天数顶替。
    """
    days = _day_list(start, end)
    if amount <= 0 or not days:
        return 0.0, 0, 0
    if not code:
        return None, 0, len(days)
    try:
        import datasource as ds  # 延迟导入：只读账本时不需要引擎
        df = ds.fetch_money_fund_hist(code, days=max(5, len(days) + 12))
    except Exception:  # noqa: BLE001
        return None, 0, len(days)
    if df is None or len(df) == 0 or "wanfen" not in df.columns:
        return None, 0, len(days)
    mp = {}
    try:
        for _, row in df.iterrows():
            d = str(row.get("date"))[:10]
            try:
                wf = float(row.get("wanfen"))
            except (TypeError, ValueError):
                continue
            if d and wf == wf:      # NaN 自身不等，跳过
                mp[d] = wf
    except Exception:  # noqa: BLE001
        return None, 0, len(days)
    if not mp:
        return None, 0, len(days)
    before = [d for d in mp if d < days[0]]
    last = mp[max(before)] if before else None
    bal, covered, carried = amount, 0, 0
    for d in days:
        if d in mp:
            last = mp[d]
        elif last is None:
            continue
        else:
            carried += 1
        bal += bal * last / 10000.0
        covered += 1
    if covered == 0:
        return None, 0, len(days)
    return round(bal - amount, 4), covered, carried


def accrue_cash_lots(state: dict, as_of: str, calendar) -> float:
    """按「1 天期逆回购逐日滚动 + 场外货基零头」的 lot 级模型给现金计息。

    与旧 accrue_cash 的区别：不再「年化 × 天数」一把算，而是逐笔记录
    买入日/到期日/期限/本金/利率/利息/手续费，用于复盘归因。
    lot 只在操作日（默认每周一，见 repo_lot_schedule）产生，期限按观望金额分档（7/14/28 天期）。

    结算规则（避免重复计息 / 漏计）：
      * 只有**已到期的 lot** 才把利息落进现金，并写进 repo_history；
      * 窗口里的最后一笔（到期日 > as_of）记到 `state["repo_open"]`，下次结算时兑现；
      * 上次留下的 repo_open 若已到期，本次一并兑现。
    返回本次净利息（元）。
    """
    plan = cash_yield_plan(state, allow_stale_sleeve=True)
    term_days = int(plan.get("repo_term_days") or 0) or None
    last = _cash_last_date(state)
    if not last or str(as_of) <= last:
        return 0.0
    days_all = (dt.date.fromisoformat(str(as_of)) - dt.date.fromisoformat(last)).days
    sched = repo_lot_schedule(calendar, last, str(as_of), term_days=term_days)
    repo_amt = float(plan["repo_amount"])
    mf_amt = float(plan["money_fund"])
    rate = float(plan["repo_rate"])
    mf_rate = float(plan["money_fund_rate"])
    fee_rate = float(plan["repo_fee_rate"])
    disc = float(plan.get("repo_commission_discount", 1.0))
    _cash_name = str(plan.get("cash_side_name") or "货基").strip()
    hist = state.setdefault("repo_history", [])
    gross = fee = repo_it = 0.0
    mf_it = 0.0
    term_rates = {int(k): float(v) for k, v in ((CFG.get("cash_yield") or {})
                                               .get("term_fee_rates") or {}).items()}

    def realize(lot):
        nonlocal gross, fee, repo_it
        # 本金取 lot 自己记的金额：现金容器可能已从逆回购切到货基，但在途 lot 仍按
        # 买入时的本金到期兑现（否则切容器那一刻利息会凭空变大/变小）。
        amt = round(float(lot.get("amount") or repo_amt), 2)
        it = round(amt * rate * lot["days"] / 365.0, 4)
        term = int(lot.get("term") or lot["days"])
        fr = term_rates.get(term, fee_rate)
        fe = round(amt * fr * disc, 4)
        gross += it
        repo_it += it
        fee += fe
        hist.append({"date": lot["buy"], "kind": "repo", "mature": lot["mature"],
                     "days": lot["days"], "term": term, "amount": amt,
                     "rate": rate, "fee_rate": fr, "interest": it, "fee": fe})

    prev = state.get("repo_open")
    if prev:
        cap_amt = repo_cap(state)
        amt = float(prev.get("amount") or 0.0)
        if amt > cap_amt + 1e-9:
            new_amt = round(cap_amt, 2)
            pdays = int(prev.get("days") or 0)
            pterm = int(prev.get("term") or pdays)
            pfr = term_rates.get(pterm, fee_rate)
            event(state, "warn",
                  f"在途逆回购本金超可用现金，已收敛 {amt:,.0f} → {new_amt:,.0f} 元"
                  f"（现金层旧口径未扣挂单/场外占款，2026-11-08 修复）", as_of)
            prev = dict(prev, amount=new_amt,
                        interest=round(new_amt * rate * pdays / 365.0, 4),
                        fee=round(new_amt * pfr * disc, 4), fee_rate=pfr)
            state["repo_open"] = prev
    if prev and str(prev.get("mature")) <= str(as_of):
        realize(prev)
        state.pop("repo_open", None)
        prev = None
    # 容器是货基（repo_amt=0）时不再新排逆回购 lot；操作日规则（每周一）仍只作用于
    # 真的走逆回购的情形，切换容器不会改动 operation_days。
    sched = (repo_lot_schedule(calendar, last, str(as_of), term_days=term_days)
             if repo_amt > 0 else [])
    open_lot = None
    for lot in sched:
        if lot["mature"] <= str(as_of):
            realize(lot)
        elif open_lot is None and prev is None:
            # 已有在途 lot（长期限锁定中）时不再开新 lot，否则会覆盖上一笔的本金/期限
            open_lot = lot
    # 窗口内没有操作日时 sched 为空、open_lot 为 None——此时必须保留上一次留下的在途 lot，
    # 否则「周一买、周四到期」那笔会在周二/周三的结算里被抹掉，到周四再也兑不出来。
    state["repo_open"] = open_lot or prev
    if open_lot:
        term = int(open_lot.get("term") or open_lot["days"])
        fr = term_rates.get(term, fee_rate)
        state["repo_open"] = dict(open_lot, amount=round(repo_amt, 2), rate=rate,
                                  term=term, fee_rate=fr,
                                  interest=round(repo_amt * rate * open_lot["days"] / 365.0, 4),
                                  fee=round(repo_amt * fr * disc, 4))
    # 货基池持仓：优先沿用账本持久化的 sleeve_lots（实际买入的那几支），只有排行
    # 刷新（data_date 变化）才换仓；排行过期时继续按旧持仓计息，不退回朝朝宝。
    plan_holds = [dict(h) for h in (plan.get("sleeve_holdings") or [])]
    lots = state.get("sleeve_lots") if isinstance(state.get("sleeve_lots"), dict) else {}
    plan_dd = str(plan.get("sleeve_data_date") or "")
    if plan_holds and (not lots.get("holdings") or str(lots.get("data_date") or "") != plan_dd):
        state["sleeve_lots"] = {"created": str(as_of), "data_date": plan_dd,
                                "holdings": plan_holds}
        lots = state["sleeve_lots"]
        event(state, "info", f"货基池持仓更新：{len(plan_holds)} 支（排行数据日 {plan_dd or '-'}）", as_of)
    holds = [dict(h) for h in (lots.get("holdings") or plan_holds)]
    sleeve_amt = round(sum(float(h.get("amount") or 0.0) for h in holds), 2)
    # 额度收敛：持仓按当时现金切分；现金若已被股票/逆回购占用，按可用现金等比缩仓
    avail = max(0.0, round(float(plan["deployable"]) - float(plan["repo_locked"])
                           - float(plan["repo_amount"]), 2))
    if sleeve_amt > avail + 0.01:
        k = (avail / sleeve_amt) if sleeve_amt else 0.0
        holds = [dict(h, amount=round(float(h.get("amount") or 0.0) * k, 2)) for h in holds]
        sleeve_amt = round(sum(float(h["amount"]) for h in holds), 2)
        state["sleeve_lots"] = dict(lots, holdings=holds)
        event(state, "warn",
              f"货基池持仓超过可用现金，按 {k:.1%} 等比缩仓（可用 {avail:,.2f} 元）", as_of)
    sleeve_rate = (round(sum(float(h.get("amount") or 0.0) * float(h.get("rate") or 0.0)
                             for h in holds) / sleeve_amt, 6) if sleeve_amt > 0 else 0.0)
    if sleeve_amt > 0 and not plan_holds:
        mf_amt = round(max(0.0, mf_amt - sleeve_amt), 2)   # 排行不可用时避免与朝朝宝重复计
    if mf_amt > 0 and days_all > 0:
        # 朝朝宝：按**逐日利率**计息（每日利息再投入）。利率取 cash_rates_history.jsonl
        # 当日值；当日缺失就沿用之前最近一天的（逐日顺延），不拿一段窗口的年化一把算。
        rh = _rate_history()
        bal, rate_gaps = mf_amt, 0
        for d in _day_list(last, str(as_of)):
            r = _rate_on(rh, d, "r1")
            if r is None:
                r = mf_rate
                rate_gaps += 1
            bal += bal * r / 365.0
        it = round(bal - mf_amt, 4)
        mf_it = it
        gross += it
        hist.append({"date": str(as_of), "kind": "mf", "mature": "",
                     "days": int(days_all), "amount": round(mf_amt, 2),
                     "rate": mf_rate, "interest": it, "fee": 0.0,
                     "basis": "逐日利率" + (f"（{rate_gaps}天沿用最近利率）" if rate_gaps else "")})
    # 半流动性货币增值：**按逐日万份收益**计息（每 10,000 份的当日收益，每日再投入）。
    # 选基用 90 天实得年化（cash_pool.py），只用于排序；入账必须是逐日真金白银。
    # 窗口内完全取不到某只的万份收益时，本笔不计息并告警——绝不拿「年化 × 天数」顶替。
    pool_it = 0.0
    pool_missing = []
    if sleeve_amt > 0 and days_all > 0:
        for h in holds:
            ha = round(float(h.get("amount") or 0.0), 2)
            hr = float(h.get("rate") or 0.0)
            code_h = str(h.get("code") or "")
            one, covered, carried = _wanfen_daily(code_h, ha, last, str(as_of))
            if one is None:
                pool_missing.append(code_h or h.get("name") or "?")
                hist.append({"date": str(as_of), "kind": "pool", "mature": "",
                             "days": int(days_all), "amount": ha, "rate": hr,
                             "name": h.get("name") or "", "code": code_h,
                             "interest": 0.0, "fee": 0.0, "basis": "缺数未计"})
                continue
            pool_it += one
            hist.append({"date": str(as_of), "kind": "pool", "mature": "",
                         "days": int(days_all), "amount": ha, "rate": hr,
                         "name": h.get("name") or "", "code": code_h,
                         "interest": one, "fee": 0.0, "covered_days": covered,
                         "basis": "万份收益逐日" + (f"（{carried}天沿用前值）" if carried else "")})
        pool_it = round(pool_it, 4)
        gross += pool_it
        if pool_missing:
            event(state, "warn",
                  f"货基缺数未计息：{('、'.join(pool_missing))}"
                  f"（{last}～{as_of} 无万份收益数据）；本笔本次不计，不拿年化估算顶替", as_of)
        state["sleeve_lots"] = {"created": str((state.get("sleeve_lots") or {}).get("created") or as_of),
                                "data_date": str((state.get("sleeve_lots") or {}).get("data_date") or plan_dd),
                                "holdings": holds}
    net = round(gross - fee, 2)
    if net == 0:
        return 0.0
    state["cash"] = _money(float(state["cash"]) + net)
    state.setdefault("cash_marks", {})["last_accrued"] = str(as_of)

    def _realized_ann(it: float, amt: float) -> float:
        """实得年化：本窗口实收利息 ÷ 本金 ÷ 天数 × 365（真实入账口径，不是预测/标签）。"""
        return (it * 365.0 / (amt * days_all)) if (amt > 0 and days_all > 0) else 0.0

    _segs = []
    if sleeve_amt > 0:
        _stale_note = ("（排行过期，沿用最后持仓）"
                       if (plan.get("sleeve_stale") or plan.get("sleeve_degraded")) else "")
        _segs.append(
            f"半流动性 {sleeve_amt:,.0f} 元按逐日万份收益实得 {pool_it:+.2f} 元"
            f"（实得年化 {_realized_ann(pool_it, sleeve_amt):.2%}；{len(holds)} 支，"
            f"90 天口径 {sleeve_rate:.2%} 只用于选基、不是计息利率"
            + (f"；{len(pool_missing)} 支缺数未计" if pool_missing else "")
            + _stale_note + "）")
    elif repo_amt > 0 or repo_it:
        _segs.append(
            f"逆回购 {repo_amt:,.0f} 元"
            f"（{len([h for h in sched if h['mature'] <= str(as_of)])} 笔已到期 + 1 笔在途）"
            f"按 {rate:.2%} 计息实得 {repo_it:+.2f} 元")
    if mf_amt > 0 and days_all > 0:
        _segs.append(f"{_cash_name} {mf_amt:,.2f} 元按逐日利率实得 {mf_it:+.2f} 元"
                     f"（实得年化 {_realized_ann(mf_it, mf_amt):.2%}）")
    detail = (f"现金层计息 {days_all} 天：" + "；".join(_segs)
              + f" − 手续费 {fee:.2f} 元 = {net:+.2f} 元")
    event(state, "cash_yield", detail, as_of, amount=net)
    return net


def repo_pnl(state: dict, since: str = None) -> dict:
    """逆回购 / 货基盈亏分析（只读，不改账本）。

    指标口径：
      * 操作笔数 ops：窗口内**已到期**的笔数（只在周一操作，7/14/28 天期对应约 52/26/13 笔/年）；
        在途那笔算进覆盖天数/利息，不算笔数；
      * 覆盖天数 covered：这些 lot 实际计息的自然日之和；
      * 自然日跨度 span：从第一笔买入日到最后到期日（或本期结算日）的自然日；
      * 空转天数 idle = span − covered：现金躺在账上没进逆回购的天数（真金白银的损失）；
      * 手续费 fees：按期限收（1 天 0.001%、7 天 0.005%、14 天 0.010%、28 天 0.020%）；
      * 实得年化 eff = (利息 − 手续费) / (平均投入 × covered/365)。
    """
    hist = state.get("repo_history") or []
    if since:
        hist = [r for r in hist if str(r.get("date")) >= str(since)]
    repo = [r for r in hist if r.get("kind") == "repo"]
    mf = [r for r in hist if r.get("kind") == "mf"]
    pool = [r for r in hist if r.get("kind") == "pool"]
    op = state.get("repo_open") or {}
    ops = len(repo)
    invested = sum(float(r.get("amount") or 0.0) for r in repo)
    covered = sum(int(r.get("days") or 0) for r in repo)
    interest = round(sum(float(r.get("interest") or 0.0) for r in repo), 2)
    fees = round(sum(float(r.get("fee") or 0.0) for r in repo), 2)
    mf_interest = round(sum(float(r.get("interest") or 0.0) for r in mf), 2)
    pool_interest = round(sum(float(r.get("interest") or 0.0) for r in pool), 2)
    # 在途 lot：利息还没落现金，但「已锁定资金 / 覆盖天数 / 应得利息」都要算出来，
    # 否则刚买完那一期会出现「覆盖 3 天、跨度 0 天、年化 0%」的假象。
    op_int = op_fee = 0.0
    if op:
        op_int = round(float(op.get("interest") or 0.0), 2)
        op_fee = round(float(op.get("fee") or 0.0), 2)
        covered += int(op.get("days") or 0)
    net = round(interest - fees + mf_interest + pool_interest + op_int - op_fee, 2)
    avg_amt = invested / ops if ops else float(op.get("amount") or 0.0)
    basis = avg_amt * covered / 365.0
    eff = (round((interest - fees + op_int - op_fee) / basis, 6)
           if basis > 0 else 0.0)
    firsts = [str(r["date"]) for r in repo]
    lasts = [str(r.get("mature") or r["date"]) for r in repo]
    if op:
        firsts.append(str(op.get("buy") or ""))
        lasts.append(str(op.get("mature") or op.get("buy") or ""))
    firsts = [x for x in firsts if x]
    lasts = [x for x in lasts if x]
    span = 0
    if firsts and lasts:
        span = max(0, (dt.date.fromisoformat(max(lasts))
                       - dt.date.fromisoformat(min(firsts))).days)
    idle = max(0, span - covered)
    return {"ops": ops, "invested": round(invested, 2), "avg_amount": round(avg_amt, 2),
            "covered_days": covered, "span_days": span, "idle_days": idle,
            "interest": round(interest + op_int, 2), "fees": round(fees + op_fee, 2),
            "mf_interest": mf_interest,
            "pool_interest": pool_interest,
            "net": net, "effective_annualized": eff,
            "fee_drag": (round((fees + op_fee) / (interest + op_int), 4)
                         if interest + op_int > 0 else 0.0),
            "open_lot": state.get("repo_open") or {}, "lots": repo[-20:],
            "mf_rows": mf[-10:], "pool_rows": pool[-10:]}


def cash_interest_split(state: dict) -> dict:
    """现金利息「已落账 + 在途」双行口径（2026-11-07 B 类 #12②）。

    booked  = 账本现金里**已经加过**的净利息（`cash_yield` 事件金额之和，含逆回购+货基）；
    pending = 已买入但还没到期的逆回购利息估算（`repo_open` 的 interest − fee）；
    total   = 两者合计。

    为什么必须拆两行：旧周报「本期利息 0.00 元」和「逆回购利息 X 元」会同时出现在
    一页上——一个只算当天落账、一个算含在途，读者会以为账本算错了。统一由本函数输出，
    weekly.py 的 md/HTML 与 report_html.py 的 KPI 都调它，口径只此一处。
    """
    booked = round(sum(float(e.get("amount") or 0.0)
                       for e in (state.get("events") or [])
                       if e.get("level") == "cash_yield"), 2)
    op = state.get("repo_open") or {}
    pending = round(float(op.get("interest") or 0.0) - float(op.get("fee") or 0.0), 2)
    return {"booked": booked, "pending": pending,
            "total": round(booked + pending, 2), "open_lot": op}


def _wanfen_interest(code: str, amount: float, start: str, end: str) -> float | None:
    """窗口汇总口径：Σ万份收益 × amount / 10000（测试/对账用）。

    线上计息用 `_wanfen_daily`（逐日复投、缺失日顺延、缺数不编造）；本函数只看整段
    合计，缺少「每一天」的语义，**不能**用于结算。
    """
    if amount <= 0 or not code:
        return None
    try:
        n = (dt.date.fromisoformat(str(end)) - dt.date.fromisoformat(str(start))).days
    except Exception:  # noqa: BLE001
        return None
    try:
        import datasource as ds  # 延迟导入：只读账本时不需要引擎
        hist = ds.fetch_money_fund_hist(code, days=max(5, n + 12))
    except Exception:  # noqa: BLE001
        return None
    if hist is None or len(hist) == 0 or "wanfen" not in hist.columns:
        return None
    try:
        rows = hist[(hist["date"].astype(str) > str(start))
                    & (hist["date"].astype(str) <= str(end))]
        if len(rows) == 0:
            return None
        total = float(rows["wanfen"].fillna(0).sum())
    except Exception:  # noqa: BLE001
        return None
    return round(amount * total / 10000.0, 4)


def accrue_cash(state: dict, as_of: str, close_map: dict = None,
                days: int = None, calendar=None) -> float:
    """给闲置现金计息并记进现金（生产路径 = 逐日口径）。

    生产路径：weekly.py / patrol.py / paper_exec.py 都会传 calendar → 直接走
    `accrue_cash_lots`（货基逐日万份收益、朝朝宝逐日利率、逆回购 lot 锁定利率）。
    本函数自己保留的「年化 × 天数」只用于**离线测试/无交易日历**的回滚口径，
    不参与线上结算；一旦走到那里会打印告警，防止悄悄退回偷懒口径。

    返回本次净利息（元，已扣逆回购手续费）。同一天重复调用不重复计息（days<=0 直接返回 0），
    所以 weekly.py / paper_exec.py 里先 accrue 再 settle 是幂等的。
    """
    if calendar is not None and days is None:
        return accrue_cash_lots(state, as_of, calendar)
    print("[paper] ⚠ 未传交易日历：现金计息走回滚口径（年化×天数），仅离线/测试可用",
          file=sys.stderr)
    plan = cash_yield_plan(state, close_map)
    marks = state.setdefault("cash_marks", {})
    if days is None:
        navs = state.get("nav") or []
        # 用「最晚的结算日」而不是最后插入的那条：settle() 会按日期排序，
        # 但补数/演练时可能先有未来日期、后插入历史日期，取 max 才不会漏计/重复计。
        last = max((str(n.get("date")) for n in navs), default=None) or state["meta"].get("baseline_date")
        # 已计息到哪天也要算进来：万一只计息没来得及结算就中断，重跑不能重复计息
        last_accrued = marks.get("last_accrued")
        if last_accrued and str(last_accrued) > str(last):
            last = last_accrued
        try:
            days = (dt.date.fromisoformat(str(as_of)) - dt.date.fromisoformat(str(last))).days
        except Exception:
            days = 0
    if days <= 0:
        return 0.0
    repo_amt = plan["repo_amount"]
    mf_amt = plan["money_fund"]
    gross = repo_amt * plan["repo_rate"] * days / 365.0 + mf_amt * plan["money_fund_rate"] * days / 365.0
    # 逆回购佣金按 7 天期年费率（52 笔 × 0.005% = 0.26%/本金 × 券商折扣）摊到天数；现金侧 0 手续费
    fee = repo_amt * float(plan.get("repo_fee_annual_rate") or 0.0) * days / 365.0
    net = round(gross - fee, 2)
    if net == 0:
        return 0.0
    state["cash"] = _money(float(state["cash"]) + net)
    marks["last_accrued"] = str(as_of)
    detail = ("（回滚口径：年化×天数，仅离线/测试使用，线上结算走逐日万份收益/逐日利率）"
              f"现金层计息 {days} 天：逆回购 {repo_amt:,.0f} 元"
              f"（{plan['repo_lots']} 手 × {1000 if repo_amt == 0 else repo_amt / max(plan['repo_lots'], 1):,.0f} 元）"
              f"@{plan['repo_rate']:.1%} + {plan.get('cash_side_name') or '货基'} "
              f"{mf_amt:,.2f} 元@{plan['money_fund_rate']:.1%}"
              f" − 手续费 {fee:.2f} 元 = {net:+.2f} 元")
    event(state, "cash_yield", detail, as_of, amount=net)
    return net


# ------------------------------------------------------------------ 结算
def settle(state: dict, as_of: str, close_map: dict, bench_close: float = None) -> dict:
    """按 as_of 收盘价结算：更新持仓市值、净值、回撤、峰值。"""
    market_value = 0.0
    stale = []
    for code, h in state["holdings"].items():
        close = close_map.get(code)
        if close is None or float(close) <= 0:
            close = h.get("last_close") or h.get("cost_basis") or 0.0
            stale.append(code)
        close = float(close)
        h["last_close"] = close
        h["highest_close"] = max(float(h.get("highest_close") or 0), close)
        market_value += h["shares"] * close
    otc_mv = otc_book.market_value(state)   # 场外腿市值（已在 mark() 里用最新净值刷新）
    nav = _money(float(state["cash"]) + market_value + otc_mv)
    bench_nav = None
    b0 = state["meta"].get("baseline_bench")
    if bench_close and b0:
        # A2：基准用「累计投入」而不是当前本金——注资会让本金跳变，基准跟着跳就是假超额。
        bench_nav = _money(contributed_capital(state) * float(bench_close) / float(b0))
    navs = state.setdefault("nav", [])
    entry = {"date": as_of, "nav": nav, "cash": _money(state["cash"]),
             "market_value": _money(market_value), "otc_value": _money(otc_mv),
             "positions": len(state["holdings"]) + len(state.get("otc_holdings") or {}),
             "bench": None if bench_close is None else _money(bench_close),
             "bench_nav": bench_nav,
             "peak_nav": nav, "drawdown": 0.0}
    replaced = False
    for i, n in enumerate(navs):
        if n["date"] == as_of:
            navs[i] = entry
            replaced = True
            break
    if not replaced:
        navs.append(entry)
        navs.sort(key=lambda n: n["date"])
    peak = max(n["nav"] for n in navs)
    entry["peak_nav"] = _money(peak)
    entry["drawdown"] = round(nav / peak - 1, 6) if peak else 0.0
    if stale:
        event(state, "warn", f"结算时缺价格（沿用上次收盘）：{', '.join(stale)}", as_of)
    return entry


# ------------------------------------------------------------------ 分红送转
_DIV_TAX = ((365, 0.0), (30, 0.10))  # 持股 ≥1 年免税；1 月–1 年 10%；<1 月 20%


def _as_date(s):
    try:
        return dt.date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def _div_tax_rate(held_days: int) -> float:
    if held_days >= 365:
        return 0.0
    if held_days >= 30:
        return 0.10
    return 0.20


def apply_dividends(state: dict, as_of: str, div_map: dict, today: str = None) -> dict:
    """把持仓的现金分红 / 送转入账（幂等，按 ``meta.dividend_paid`` 防重复）。

    ``div_map`` = ``{code: [{"ex_date", "cash_per_share", "share_ratio"}]}``
    （``share_ratio`` 是每 10 股送转股数；现金按每股、税前）。

    口径：
    * 现金按**税后**入账——持股 ≥1 年免税、1 月–1 年扣 10%、<1 月扣 20%
      （用 ``opened_at`` 算持有天数；真实券商是派发时先放税前、卖出时补扣，
      这里保守按税后记，差异在 2027 年复盘时能对上）；
    * 送转按比例增股、总成本不变（``cost_basis`` 摊薄），**止损价不动**
      （止损锚定的是买入成本，分红不是亏损，不该因为除权自动下调止损）；
    * 只处理除权日 ≤ ``as_of`` 且晚于上次已入账日期的方案。
    """
    meta = state.setdefault("meta", {})
    paid = meta.setdefault("dividend_paid", {})
    day = str(today or as_of)[:10]
    n, cash_add, share_add = 0, 0.0, 0
    for h in (state.get("holdings") or {}).values():
        if str(h.get("kind")) != "stock":
            continue
        code = str(h.get("code") or "").strip().zfill(6)
        rows = (div_map or {}).get(code) or []
        if not rows:
            continue
        sh = int(h.get("shares") or 0)
        if sh <= 0:
            continue
        opened = _as_date(h.get("opened_at"))
        last = str(paid.get(code) or "")
        for r in rows:
            ex = _as_date(r.get("ex_date"))
            if ex is None or ex.isoformat() > str(as_of)[:10]:
                continue
            if last and ex.isoformat() <= last:
                continue
            # 除权日才建仓 = 没赶上股权登记日，不派息
            if opened and ex <= opened:
                continue
            per = float(r.get("cash_per_share") or 0.0) or 0.0
            song = float(r.get("share_ratio") or 0.0) or 0.0
            if per <= 0 and song <= 0:
                continue
            held = (ex - opened).days if opened else 365
            rate = _div_tax_rate(held)
            gross = round(sh * per, 2)
            net = round(gross * (1 - rate), 2)
            add = 0
            if net:
                state["cash"] = round(float(state["cash"]) + net, 2)
                cash_add += net
            if song > 0:
                add = int(round(sh * song / 10.0))
                if add > 0:
                    total_cost = float(h.get("cost_basis") or 0.0) * sh
                    h["shares"] = sh + add
                    h["cost_basis"] = round(total_cost / float(h["shares"]), 4)
                    sh = h["shares"]
                    share_add += add
            event(state, "dividend",
                  f"{h.get('name') or code} 除权 {ex.isoformat()}："
                  f"{gross:.2f} 元（含税）红利税 {rate:.0%}（持有 {held} 天）"
                  f"，到账 {net:.2f} 元" + (f"；送转 {add} 股" if add else ""), day)
            paid[code] = ex.isoformat()
            last = ex.isoformat()
            n += 1
    return {"count": n, "cash": round(cash_add, 2), "shares": share_add}


# ------------------------------------------------------------------ 风险警示处置
def is_risk_warn_name(name: str) -> bool:
    """当前简称是否带风险警示/退市整理标记（ST、*ST、退市整理期「XX退」）。

    只用交易所给的**当前简称**判断——这比公告标题可靠：主板被实施「其他风险警示」
    时简称会变成 ST 开头，但公告标题里未必出现「退市风险」四个字。
    """
    s = str(name or "").strip()
    return ("ST" in s.upper()) or ("退" in s)


def risk_exit_orders(state: dict, warn_codes, as_of: str, fill_from: str = None) -> list:
    """持仓被实施风险警示（ST/*ST/退市整理）→ 下一交易日开盘清仓。

    这是**纪律动作**，和止损/熔断同级：不参与「状态未变不调仓」的限制，
    也不受每月新买次数限制（那是买入侧的事）。清仓后当周不回补——回补由
    决策层的候选池拦住（这类票进不了候选池）。
    """
    codes = {str(c).strip()[-6:] for c in (warn_codes or set())}
    out = []
    for code, h in (state.get("holdings") or {}).items():
        if str(h.get("kind")) != "stock":
            continue
        c6 = str(code).strip()[-6:]
        if c6 not in codes:
            continue
        if _exit_covered(state, code) >= int(h.get("shares") or 0):
            continue  # 已经安排全卖了，不重复下单
        _cancel_stops(state, code, "持仓被实施风险警示，改为开盘清仓")
        _cancel_open_sells(state, code, "持仓被实施风险警示，改为一次性清仓")
        o = make_order(state, as_of, code, h.get("name") or code, h.get("kind"), "sell",
                       int(h["shares"]), "sell_open",
                       f"持仓 {c6} 被实施风险警示（ST/退市整理）：下一交易日开盘清仓，"
                       f"当周不回补",
                       effective_from=fill_from,
                       layer=h.get("layer", "ETF"), level=h.get("level"))
        out.append(o)
        event(state, "risk",
              f"{code} {h.get('name')} 被实施风险警示：全部卖出（下一交易日开盘），当周不回补",
              as_of)
    return out


def realized_pnl_total(state: dict) -> float:
    """累计已实现盈亏（已扣买入费用与卖出费用；只算卖出流水）。"""
    stock = sum(float(t.get("realized_pnl") or 0.0)
                for t in (state.get("trades") or []) if t.get("action") == "sell")
    return round(stock + otc_book.realized_total(state), 2)


def cash_interest_total(state: dict) -> float:
    """累计现金层净利息（逆回购 + 货基，已到账部分；在途 lot 不算，因为它还没进现金）。

    这笔钱直接加到 `state["cash"]`，不产生卖出流水；早期只按卖出流水算「已实现盈利」，
    现金层收益就被漏掉了 —— 那部分同样是赚到的钱，必须进复利口径。
    """
    tot = 0.0
    for r in (state.get("repo_history") or []):
        tot += float(r.get("interest") or 0.0) - float(r.get("fee") or 0.0)
    return round(tot, 2)


def capital_flows(state: dict) -> list:
    """注资现金流（按到账日排序）：``[{date, amount, month}]``。"""
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


def initial_capital(state: dict) -> float:
    """初始本金（首次注资之前的本金）——累计收益的分母基准。"""
    meta = state.get("meta") or {}
    for k in ("initial_capital", "bench_baseline_capital", "capital"):
        if meta.get(k):
            return float(meta[k])
    return 0.0


def contributed_capital(state: dict) -> float:
    """累计投入本金 = 初始本金 + Σ注资（不含盈亏）——**收益率/金额的分母**。"""
    return round(initial_capital(state) + sum(f["amount"] for f in capital_flows(state)), 2)


def withdrawn_total(state: dict) -> float:
    """已划出的年度奖励合计（不是亏损，算收益时要加回来）。"""
    return round(float(((state.get("meta") or {}).get("withdrawn_total")) or 0.0), 2)


def cumulative_pnl(state: dict) -> float:
    rows = [n for n in (state.get("nav") or []) if n.get("nav") is not None]
    nav = float(rows[-1]["nav"]) if rows else 0.0
    contrib = contributed_capital(state)
    return round(nav + withdrawn_total(state) - contrib, 2) if contrib > 0 else 0.0


def cumulative_return(state: dict) -> float | None:
    contrib = contributed_capital(state)
    return round(cumulative_pnl(state) / contrib, 8) if contrib > 0 else None


def investable_capital(state: dict) -> dict:
    """可投资本 = 账本本金 + 累计**已实现**盈利（用户规则：赚到的钱要再投出去）。

    已实现盈利 = 卖出落袋盈亏 + 现金层净利息（逆回购/货基）。

    只算已实现、不算浮盈，理由有两条：
      ① 浮盈不是钱——按浮盈放大仓位等于追涨加杠杆，回撤时会反噬；
      ② 可投资本上限由 ``config.CAPITAL["max"]`` 兜底，超出部分留在现金层（逆回购/货基），不越界扩大。
    亏钱时不缩本金（只把「盈利」加进去），避免下跌里再被动减仓。
    返回结构里带 raw / clamped / growth_pct 便于周报与体检解释差异。
    """
    base = float(state["meta"]["capital"])
    realized = round(realized_pnl_total(state) + cash_interest_total(state), 2)
    # 年度结算时已实现盈利会被重设基线（本金已重置为「年末净值 − 奖励」），
    # 只把 baseline 之后新赚的已实现盈利再加进可投资本，避免同一笔钱重复放大仓位。
    bl = (state["meta"].get("annual_baseline") or {})
    realized0 = round(float(bl.get("realized") or 0.0)
                      + float(bl.get("cash_interest") or 0.0), 2)
    # A7：动态本金跑过之后（capital_history 非空），meta.capital 每月已更新为
    # 「上月末净值 + 注资」，盈亏已含在里面；再加一次已实现盈利会把仓位上限放大。
    # 未启用动态本金的账本仍按旧口径（盈利再投资），那里 capital 不会自动更新。
    _dyn_active = bool((state.get("meta") or {}).get("capital_history"))
    gain = 0.0 if _dyn_active else max(0.0, realized - realized0)
    raw = base + gain
    hi = float(config.CAPITAL["max"])
    inv = min(hi, raw)
    return {
        "base": round(base, 2),
        "realized": realized,
        "gain": round(gain, 2),
        "raw": round(raw, 2),
        "investable": round(inv, 2),
        "clamped": raw - inv > 0.01,
        "growth_pct": round(inv / base - 1.0, 6) if base else 0.0,
    }


def rebuild_due(state: dict, regime, mode: str, investable: float = None) -> bool:
    """regime_change 模式：只有市场状态变化（含首次建仓）才重建目标组合。

    与 backtest.py 的 regime_change 语义保持一致（回放里 `_state["regime"]` 变化才重新
    生成目标并调仓；不变时一张单都不下，只保留止损/熔断）。其余模式恒为 True。
    市场状态由阶段三 market_regime.regime 给出（进攻/平衡/防守/未知）。
    """
    if mode != "regime_change":
        return True
    prev = (state.get("meta") or {}).get("last_regime")
    if prev is None or str(prev) != str(regime):
        return True
    # 复利兜底：市场状态没变，但可投资本已经涨/跌 ≥1%，也要重建一次目标，
    # 否则「赚到的钱」会一直躺在现金里不进仓位（2026-10-30明确要求复利）。
    if investable is not None:
        last = float((state.get("meta") or {}).get("last_rebuild_capital") or 0.0)
        if last > 0 and abs(float(investable) / last - 1.0) >= 0.01:
            return True
    return False


def portfolio(state: dict, close_map: dict) -> dict:
    """当前组合明细：各行市值/成本/浮动盈亏/权重，以及总市值、现金、净值。"""
    rows, mv = [], 0.0
    for code, h in state["holdings"].items():
        close = float(close_map.get(code) or h.get("last_close") or 0.0)
        value = close * h["shares"]
        cost = h["cost_basis"] * h["shares"]
        mv += value
        rows.append({
            "code": code, "name": h["name"], "kind": h["kind"], "layer": h.get("layer"),
            "shares": h["shares"], "cost_basis": round(h["cost_basis"], 4),
            "close": round(close, 4), "value": _money(value),
            "unrealized": _money(value - cost),
            "unrealized_pct": round(value / cost - 1, 6) if cost else None,
            "stop_price": h.get("stop_price"),
            "opened_at": h.get("opened_at"),
        })
    for r0 in otc_book.holdings_rows(state):
        rows.append({"code": r0["code"], "name": r0["name"], "kind": "fund",
                     "layer": r0["layer"], "shares": None, "cost_basis": None,
                     "close": r0["nav"], "value": r0["value"],
                     "unrealized": r0["unrealized"],
                     "unrealized_pct": r0["unrealized_pct"],
                     "stop_price": None, "opened_at": r0["buy_date"], "nav_date": r0["nav_date"],
                     "redeem_fee": r0["redeem_fee"], "hold_days": r0["hold_days"]})
        mv += r0["value"]
    nav = _money(float(state["cash"]) + mv)
    cap0 = contributed_capital(state) or float(state["meta"]["capital"])
    for r in rows:
        r["weight"] = round(r["value"] / nav, 6) if nav else 0.0
    last_nav = state["nav"][-1]["nav"] if state.get("nav") else nav
    bench_nav = state["nav"][-1].get("bench_nav") if state.get("nav") else None
    # 年度结算会划出奖励并把 capital 重置为「年末净值 − 奖励」；
    # cum_index 记录结算后的累计增长链，保证总收益不被重置口径吃掉。
    base_idx = float((state.get("meta") or {}).get("cum_index") or 1.0)
    return {"rows": rows, "market_value": _money(mv), "cash": _money(state["cash"]),
            "nav": nav, "last_settled_nav": last_nav,
            # 分母已是「累计投入」，cum_index 再乘一次会把历史收益放大一遍（A3）。
            "total_return": (round((nav + withdrawn_total(state)) / cap0 - 1, 6)
                             if cap0 else None),
            "bench_return": (round(bench_nav / cap0 - 1, 6)
                             if bench_nav and cap0 else None),
            "drawdown": round(nav / max(n["nav"] for n in state["nav"]) - 1, 6)
            if state.get("nav") else 0.0}


def stats(state: dict) -> dict:
    """绩效统计：总收益、年化、最大回撤、胜率、盈亏比、费用、超额。"""
    navs = state.get("nav", [])
    cap0 = contributed_capital(state) or float(state["meta"]["capital"])
    if not navs:
        return {"settled": False, "note": "还没有结算记录"}
    peak, mdd = navs[0]["nav"], 0.0
    for n in navs:
        peak = max(peak, n["nav"])
        mdd = min(mdd, n["nav"] / peak - 1)
    last = navs[-1]
    days = (dt.date.fromisoformat(last["date"]) - dt.date.fromisoformat(navs[0]["date"])).days
    base_idx = float((state.get("meta") or {}).get("cum_index") or 1.0)
    total_ret = (last["nav"] + withdrawn_total(state)) / cap0 - 1
    bench_ret = (last["bench_nav"] / cap0 - 1) if last.get("bench_nav") else None
    sells = [t for t in state["trades"] if t["action"] == "sell"]
    wins = [t for t in sells if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in sells if (t.get("realized_pnl") or 0) <= 0]
    realized = sum(t.get("realized_pnl") or 0 for t in sells)
    fees = sum(t.get("fee") or 0 for t in state["trades"])
    unrealized = 0.0
    for code, h in state["holdings"].items():
        close = float(h.get("last_close") or 0)
        unrealized += close * h["shares"] - h["cost_basis"] * h["shares"]
    avg_win = (sum(t["realized_pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["realized_pnl"] for t in losses) / len(losses)) if losses else 0.0
    pf = (sum(t["realized_pnl"] for t in wins) / abs(sum(t["realized_pnl"] for t in losses))
          if losses and sum(t["realized_pnl"] for t in losses) else None)
    return {
        "settled": True,
        "days": days,
        "nav": last["nav"],
        "total_return": round(total_ret, 6),
        "annualized": (round(((last["nav"] + withdrawn_total(state)) / cap0)
                             ** (365 / days) - 1, 6)
                       if days >= 1 else None),
        "max_drawdown": round(mdd, 6),
        "bench_return": None if bench_ret is None else round(bench_ret, 6),
        "excess_return": (None if bench_ret is None else round(total_ret - bench_ret, 6)),
        "closed_trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(sells), 4) if sells else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": None if pf is None else round(pf, 3),
        "realized_pnl": _money(realized),
        "unrealized_pnl": _money(unrealized),
        "fees_total": _money(fees + otc_book.fees_total(state)),
        "otc_realized_pnl": _money(otc_book.realized_total(state)),
        "otc_unrealized_pnl": _money(otc_book.unrealized(state)),
        "otc_market_value": _money(otc_book.market_value(state)),
        "otc_cost": _money(otc_book.cost_total(state)),
        "fees_pct_of_capital": round(fees / cap0, 6) if cap0 else None,
        "trades_total": len(state["trades"]),
        "as_of": last["date"],
    }


# ------------------------------------------------------------------ 交易日工具
def _add_trading_days(date: str, n: int, calendar: list = None) -> str:
    """在给定交易日历里往后数 n 个交易日；没有日历就用「跳过周末」近似。"""
    d = dt.date.fromisoformat(date)
    if calendar:
        later = [c for c in calendar if c > date]
        if later:
            idx = min(n - 1, len(later) - 1)
            return later[idx]
    added = 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.isoformat()


# ------------------------------------------------------------------ Markdown 片段
def holdings_md(state: dict, close_map: dict) -> str:
    p = portfolio(state, close_map)
    if not p["rows"]:
        return "（空仓）\n"
    lines = ["| 代码 | 名称 | 类型 | 股数 | 成本价 | 现价 | 市值 | 浮动盈亏 | 权重 | 止损价 | 已持天数 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    today = dt.date.today()
    for r in p["rows"]:
        try:
            held = f"{(today - dt.date.fromisoformat(r['opened_at'])).days} 天"
        except Exception:
            held = "—"
        stop = "—" if r["stop_price"] is None else f"{float(r['stop_price']):.3f}"
        pnl_pct = "—" if r["unrealized_pct"] is None else f"{r['unrealized_pct']:+.1%}"
        # 场外影子持仓（otc_book）没有「每份成本价/现价」概念，这两列用份额净值，缺就用 —。
        # 2026-10-30 修：原来直接 f"{cost_basis:.3f}"，场外行 cost_basis=None → TypeError，周报必崩。
        cost_txt = "—" if r["cost_basis"] is None else f"{float(r['cost_basis']):.3f}"
        close_txt = "—" if r["close"] is None else f"{float(r['close']):.3f}"
        unreal_txt = "—" if r["unrealized"] is None else f"{float(r['unrealized']):+.2f}"
        weight_txt = "—" if r["weight"] is None else f"{float(r['weight']):.1%}"
        lines.append(
            f"| {r['code']} | {r['name']} | {'ETF' if r['kind'] == 'etf' else ('场外' if r['kind'] == 'otc' else '个股')} | "
            f"{r['shares']} | {cost_txt} | {close_txt} | {r['value']:.2f} | "
            f"{unreal_txt} ({pnl_pct}) | {weight_txt} | {stop} | {held} |")
    return "\n".join(lines) + "\n"


def orders_md(orders: list) -> str:
    if not orders:
        return "（无）\n"
    lines = ["| 生效日 | 动作 | 代码 | 名称 | 类型 | 股数 | 限价/参考价 | 说明 |",
             "|---|---|---|---|---|---|---|---|"]
    for o in orders:
        px = o.get("limit_price") or o.get("ref_price")
        lines.append(
            f"| {o['effective_from']} | {'买入' if o['action'] == 'buy' else '卖出'} | "
            f"{o['code']} | {o['name']} | {o['order_type']} | {o['shares']} | "
            f"{'—' if px is None else f'{float(px):.3f}'} | {o.get('reason', '')} |")
    return "\n".join(lines) + "\n"


def report_md(state: dict, close_map: dict, title: str = None) -> str:
    """生成一段可直接放进周报的账本摘要（不含阶段一/二/三的原始结论）。"""
    p = portfolio(state, close_map)
    s = stats(state)
    cap0 = float(state["meta"]["capital"])
    out = []
    out.append(f"# {title or '模拟盘结算'}")
    out.append("")
    out.append(f"生成时间：{_now()}　账户：模拟盘（paper）　初始本金：{cap0:,.0f} 元"
               f"　档位：{state['meta'].get('tier')}")
    out.append("")
    out.append("## 一、账户总览")
    out.append("")
    out.append(f"- 净值 **{p['nav']:,.2f}** 元（累计 {p['total_return']:+.2%}）")
    out.append(f"- 现金 {p['cash']:,.2f} 元｜持仓市值 {p['market_value']:,.2f} 元"
               f"｜仓位 {p['market_value'] / p['nav']:.1%}" if p["nav"] else "")
    if p.get("bench_return") is not None:
        out.append(f"- 同期沪深300：{p['bench_return']:+.2%}｜超额："
                   f"{p['total_return'] - p['bench_return']:+.2%}")
    out.append(f"- 距峰值回撤：{p['drawdown']:+.2%}（熔断线 "
               f"{float(config.EXEC['portfolio_dd_halt']):.0%}）")
    out.append("")
    out.append("## 二、持仓明细")
    out.append("")
    out.append(holdings_md(state, close_map))
    out.append("## 三、绩效统计")
    out.append("")
    if s.get("settled"):
        out.append(f"- 已结算天数：{s['days']} 天｜成交笔数：{s['trades_total']} 笔")
        win_rate = "—" if s["win_rate"] is None else format(s["win_rate"], ".1%")
        out.append(f"- 已平仓 {s['closed_trades']} 笔：胜 {s['wins']} / 负 {s['losses']}"
                   f"｜胜率 {win_rate}")
        pf = "—" if s["profit_factor"] is None else format(s["profit_factor"], ".2f")
        out.append(f"- 盈亏比：{pf}｜平均盈利 {s['avg_win']:+,.0f} 元"
                   f"｜平均亏损 {s['avg_loss']:+,.0f} 元")
        fees_pct = "—" if s["fees_pct_of_capital"] is None else format(s["fees_pct_of_capital"], ".2%")
        out.append(f"- 已实现盈亏 {s['realized_pnl']:+,.2f} 元｜浮动盈亏 "
                   f"{s['unrealized_pnl']:+,.2f} 元｜累计费用 {s['fees_total']:,.2f} 元"
                   f"（{fees_pct} 本金）")
        ann = "—" if s["annualized"] is None else format(s["annualized"], "+.2%")
        out.append(f"- 最大回撤：{s['max_drawdown']:+.2%}｜年化（样本不足时仅供参考）：{ann}")
    else:
        out.append("- 尚无结算记录")
    out.append("")
    return "\n".join(x for x in out if x)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="模拟盘账本查看/维护")
    ap.add_argument("--state", default=None, help="状态文件路径")
    ap.add_argument("--init", action="store_true", help="初始化模拟盘（本金 5 万起）")
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--tier", default=config.CAPITAL["default_tier"])
    ap.add_argument("--baseline-date", default=None)
    ap.add_argument("--baseline-bench", type=float, default=None)
    ap.add_argument("--force", action="store_true", help="初始化时覆盖已有状态")
    args = ap.parse_args()

    path = args.state or STATE_PATH
    if args.init:
        if os.path.exists(path) and not args.force:
            print(f"✗ 状态文件已存在：{path}（要重建加 --force）", file=sys.stderr)
            raise SystemExit(2)
        st = new_state(args.capital, args.tier, args.baseline_date, args.baseline_bench)
        save(st, path)
        print(f"✓ 模拟盘已初始化：{path}｜本金 {args.capital:,.0f} 元｜档位 {args.tier}")
        return
    st = load(path)
    print(report_md(st, {}, title="模拟盘状态"))


if __name__ == "__main__":
    main()
