#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""历史回放：用真实日线检验「市场状态 + ETF 打分」这条可执行策略。

回放的是小本金下**实际会执行的那部分**：按市场状态定总仓位上限，
按档位取实际权益，用现成的 ETF 打分规则（近3年收益 40 + 回撤 35 + 流动性 25）
在候选池里挑前几只、按整手买入，收盘价触发止损、次日开盘成交。

> 口径提醒：回测的**买入也按次一交易日开盘价成交**（旧口径）；线上自 2026-11-18 起
> 「买入一律限价条件单」——跌到限价才成交、可能不成交。所以回测数字用于比较策略变体，
> 不代表线上一定能按那个价格买到（回测偏乐观）。

不回放个股路径：选股依赖「当日截面」的财务与估值分位，公开接口给的是最新修订值，
没有 point-in-time 数据，强行回放会引入前视偏差（用未来修正过的财报做过去决策）。
个股层用前向模拟盘（paper.py/weekly.py）验证，两者合起来才是完整证据。

已知偏差（写进报告，不藏）：
  1. 候选池取的是**今天**通过筛选的 ETF（stage1_etf.csv），历史上被淘汰的没进来
     → 生存者偏差，结果偏乐观；
  2. 再平衡按「一次到位」成交，实盘是三批建仓 + 不交易带，回测换手略高；
  3. 现金按「逆回购 + 场外货基」计息，利率一律取 config.cash_rates()（唯一来源，含 as_of；
     2026-10-27 前是写死的「逆回购 2% / 货基 1.4%」，已过期）。历史上逆回购偶有季末高息，
     这里按常态估、不追高，所以现金层收益偏保守——但这是假设，不是对未来的承诺。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capital
import config
import datasource as ds
import etf
import metrics as m

CFG = config.ETF


def _f2(x, nd=2):
    """Sharpe 这类指标在样本不足时会是 None，格式化时不能用 :.2f。"""
    return "n/a" if x is None else f"{x:.{nd}f}"
CAP = config.CAPITAL
EXEC = config.EXEC
MIN_POS = CFG.get("min_position_value", 800.0)
# B 类修复（2026-11-26）：回测 ETF 承接层只数必须与线上「权益比例 → ETF 只数」档位一致
ETF_N_BY_RATIO = [(0.35, 1), (0.60, 2), (1.0, 3)]

# 参数覆盖：按区间扫描 B 类参数（状态阈值/档位/止损/集中度）。默认值与线上 config 同源，
# 只有显式传入 params 时才偏离；用于「同一规则、不同参数」的对比，不改变线上行为。
DEFAULT_PARAMS = {
    "attack_dev": 0.02,
    "defense_dev": -0.08,
    "neutral_cap": 0.60,
    "defense_cap": 0.35,
    "tier_ratio": None,          # None = 用 CAP.tiers[tier]
    "stop_loss_pct": EXEC["stop_loss_pct"],
    "trail_start": 0.10,
    "atr_mult": EXEC["atr_mult"],
    "halt_dd": EXEC["portfolio_dd_halt"],
    "max_single": CFG["max_single"],
    "max_etf_n": CFG["max_etf_n"],
}


# ------------------------------------------------------------------ 数据包装
class Series:
    """一只标的的日线，按日期升序；用 searchsorted 做「截至某日」切片。"""

    def __init__(self, df: pd.DataFrame):
        df = df.copy()
        d = pd.to_datetime(df["date"])
        self.ord = np.array([x.date().toordinal() for x in d], dtype=np.int64)

        def _col(name: str) -> np.ndarray:
            ser = df.get(name)
            if ser is None:
                return np.full(len(df), np.nan)
            return pd.to_numeric(ser, errors="coerce").to_numpy(dtype=float)

        self.open = _col("open")
        self.close = _col("close")
        self.high = _col("high")
        self.low = _col("low")
        self.amount = _col("amount")

    def upto(self, day) -> int:
        return int(np.searchsorted(self.ord, day.toordinal(), side="right"))

    def has(self, day) -> bool:
        i = self.upto(day)
        return i > 0 and self.ord[i - 1] == day.toordinal()

    def at(self, day, field="close"):
        i = self.upto(day)
        if i == 0 or self.ord[i - 1] != day.toordinal():
            return None
        v = getattr(self, field)[i - 1]
        return None if v is None or not np.isfinite(v) else float(v)


def atr(close: np.ndarray, high: np.ndarray, low: np.ndarray, i: int, n: int = 14) -> float | None:
    """截至第 i 根（不含）的 ATR。"""
    if i < n + 1:
        return None
    trs = []
    for k in range(i - n, i):
        if not (np.isfinite(high[k]) and np.isfinite(low[k]) and np.isfinite(close[k - 1])):
            return None
        trs.append(max(high[k] - low[k], abs(high[k] - close[k - 1]), abs(low[k] - close[k - 1])))
    return float(np.mean(trs))


# ------------------------------------------------------------------ 指标（与 etf.py 同口径）
def etf_eval(s: Series, day) -> dict | None:
    i = s.upto(day)
    if i < 60:
        return None
    c = s.close[:i]
    amt = s.amount[:i]
    price = float(c[-1])
    win = min(len(c), 750)
    ret_3y = float(c[-1] / c[-win] - 1) if win >= 2 else None
    dd_3y = _max_dd(c[-win:])
    amt20 = float(np.nanmean(amt[-20:])) / 1e8 if len(amt) and np.isfinite(amt[-1]) else None
    rets = np.diff(c[-win:]) / c[-win:-1]
    max_1d = float(np.nanmax(np.abs(rets))) if len(rets) else 0.0
    a = atr(s.close, s.high, s.low, i)
    return {
        "price": price, "hist_days": i, "ret_3y": ret_3y, "dd_3y": dd_3y,
        "amt20_yi": amt20, "max_1d_move": max_1d, "atr14": a,
    }


def _max_dd(a: np.ndarray) -> float:
    peak, dd = -np.inf, 0.0
    for x in a:
        if not np.isfinite(x):
            continue
        peak = max(peak, x)
        if peak > 0:
            dd = min(dd, x / peak - 1.0)
    return float(dd)


def score(r: dict) -> float:
    s = 0.0
    s += m.score_linear(r.get("ret_3y"), 0.0, 0.60, 40)
    s += m.score_inverse(r.get("dd_3y"), -0.05, -0.45, 35) if r.get("dd_3y") is not None else 0.0
    s += m.score_linear(r.get("amt20_yi"), 0.5, 5.0, 25)
    return round(s, 2)


def eligible(r: dict) -> bool:
    f = config.S1_FUND
    if r["hist_days"] < f["min_history_days"]:
        return False
    if r["amt20_yi"] is None or r["amt20_yi"] < f["min_avg_amt20_yi"]:
        return False
    if r["ret_3y"] is None or r["ret_3y"] <= f["min_return_3y"]:
        return False
    if r["dd_3y"] is None or r["dd_3y"] <= f["max_drawdown_3y"]:
        return False
    if r["max_1d_move"] > CFG["split_suspect_move"]:
        return False
    return True


# ------------------------------------------------------------------ 市场状态
def regime_at(bench: Series, day, p: dict | None = None) -> dict:
    p = p or DEFAULT_PARAMS
    i = bench.upto(day)
    if i < 200:
        return {"regime": "未知", "cap": 0.40, "dev": None}
    price = float(bench.close[i - 1])
    ma200 = float(np.mean(bench.close[i - 200:i]))
    dev = price / ma200 - 1.0
    if dev > p["attack_dev"]:
        return {"regime": "进攻", "cap": EXEC["max_gross"], "dev": dev}
    if dev > p["defense_dev"]:
        return {"regime": "中性", "cap": p["neutral_cap"], "dev": dev}
    return {"regime": "防守", "cap": p["defense_cap"], "dev": dev}


# ------------------------------------------------------------------ 组合构建
def build_target(pool: list[dict], capital: float, equity_ratio: float,
                 p: dict | None = None) -> dict:
    p = p or DEFAULT_PARAMS
    """按 plan.etf_layer 的规则算目标股数（整手）。"""
    budget = round(capital * equity_ratio, 2)
    n_by_budget = int(budget // MIN_POS)
    n_by_cap = max(1, int(math.ceil(equity_ratio / p["max_single"] - 1e-9)))
    n_ratio = next((int(k) for cap_r, k in ETF_N_BY_RATIO if equity_ratio <= cap_r + 1e-9),
                   int(p["max_etf_n"]))
    n = max(0, min(n_ratio, n_by_budget, n_by_cap))
    if n == 0:
        return {"targets": {}, "budget": budget, "note": "预算不足单笔最小"}
    per = min(budget / n, capital * p["max_single"])
    targets = {}
    for e in pool:
        if len(targets) >= n:
            break
        lc = e["price"] * CAP["lot_size"]
        cap_lots = int(capital * p["max_single"] // lc)
        lots = min(int(round(per / lc)), int(per * 1.12 // lc), cap_lots)
        if lots < 1:
            continue
        targets[e["sym"]] = {"shares": lots * CAP["lot_size"], "price": e["price"],
                             "name": e["name"], "score": e["score"]}
        per_used = lots * lc
        budget -= per_used
        if budget <= MIN_POS:
            break
    return {"targets": targets, "budget": budget, "note": f"目标 {len(targets)} 只"}


# ------------------------------------------------------------------ 费用
def fee(amount: float, is_etf: bool = False, side: str = "buy") -> float:
    """单笔费用 —— 必须与实盘同源（唯一实现 capital.trade_fee：

    佣金（有最低值）+ 过户费（仅股票）+ 印花税（仅股票卖出）。
    早期这里只算佣金，导致回测成本口径与实盘不一致，已废弃。
    """
    if amount <= 0:
        return 0.0
    return float(capital.trade_fee(amount, is_etf=is_etf, side=side)["total"])


# ------------------------------------------------------------------ 主回放
def run(capital: float, tier: str, start: str, end: str, decisions: str = "wed_sat",
        rebalance: str = "weekly", stops: bool = True,
        repo_rate: float = None, mf_rate: float = None,
        reinvest: bool = False, bootstrap_n: int = 0, pool: str = "stage1",
        params: dict | None = None):
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    cal = pd.to_datetime(ds.fetch_trade_calendar()["trade_date"])
    cal = [x.date() for x in cal]
    cal = [d for d in cal if d <= pd.Timestamp(end).date()]
    d0, d1 = pd.Timestamp(start).date(), (pd.Timestamp(end).date())

    if pool == "pit":
        # 时间点无关的全集：只要「当时」满足历史长度/流动性/收益/回撤就进池，
        # 不要求它活到今天、也不要求它今天还合格 → 消除生存者偏差。
        pit_path = os.path.join(config.OUT_DIR, "etf_universe.csv")
        if not os.path.exists(pit_path):
            raise SystemExit(f"[backtest] 缺 {pit_path}，先跑 etf_pit.py 构建全集")
        pdf = pd.read_csv(pit_path, dtype={"sym": str})
        pool_meta = []
        for _, r in pdf.iterrows():
            sym, name = str(r["sym"]), str(r["name"])
            cls = str(r.get("cls") or "") or (etf.classify(name) or "")
            if not cls:
                continue
            pool_meta.append((sym, name, cls, name))
        print(f"[backtest] 标的池：PIT 全集 {len(pool_meta)} 只"
              f"（每个决策日按当日数据判合格）")
    else:
        # 候选池：今天通过筛选的 ETF（有生存者偏差，作对照用）
        pool_path = os.path.join(config.OUT_DIR, "stage1_etf.csv")
        pdf = pd.read_csv(pool_path, dtype={"sym": str})
        pdf = pdf[~pdf["name"].astype(str).str.contains("NaN", na=False)]
        pool_meta = [(str(r["sym"]), str(r["name"]), str(r["cls"]),
                      str(r.get("underlying") or r["name"])) for _, r in pdf.iterrows()]

    hist = {}
    for sym, name, cls, und in pool_meta:
        try:
            hist[sym] = Series(ds.fetch_etf_hist(sym))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {sym} 取数失败：{e}")
    bench = Series(ds.fetch_index_daily("000300"))

    # 决策日：历史回测按双决策日（周三/周六）回放；2026-11-25 起线上也改为每周三、周六 20:30
    # 双决策，与 `wed_sat` 口径一致（B12：旧注释「不代表线上节奏」已过时）。
    # 周三（数据到周三收盘，周四开盘成交）/ 周六（数据到周五收盘，周一开盘成交）
    # 交易日历里周六不是交易日（交易所周六不开市），所以 `--decisions sat` 落成「周五决策」：
    # as_of=周五收盘、fill=次一交易日开盘；想同时看周三节奏用 wed_sat。
    # week = 每周两个决策日（周三、周六），与线上 2026-11-25 起的 wed_sat 同义
    want = {"wed_sat": (2, 4), "wed": (2,), "sat": (4,), "week": (2, 4)}[decisions]
    decision_days = sorted({d for d in cal if d0 <= d <= d1 and d.weekday() in want})
    plan_list = []
    for d in decision_days:
        as_of = max([c for c in cal if c <= d], default=None)
        if as_of is None:
            continue
        fill = next((c for c in cal if c > as_of), None)
        if fill is None:
            continue
        if plan_list and plan_list[-1]["as_of"] == as_of:
            continue
        plan_list.append({"day": d, "as_of": as_of, "fill": fill})

    # 逐日推进
    tier_ratio = float(p["tier_ratio"] if p.get("tier_ratio") is not None
                       else CAP["tiers"][tier]["equity_ratio"])
    holdings: dict[str, dict] = {}
    cash = capital
    nav_curve = []
    benchmark_curve = []
    trades = []
    halt_until = None
    forced_sell: list[str] = []
    pending_buy: dict = {}
    pending_trim: dict = {}
    peak_nav = capital

    # 现金层计息（逆回购 + 场外货基）：阶段三一直算这块收益，回放不记就会系统性低估
    # 利率唯一来源 config.cash_rates()；--cash-repo-rate/--cash-mf-rate 只做敏感性覆盖
    _r = config.cash_rates()
    _repo_rate = float(repo_rate if repo_rate is not None else _r["repo"])
    _mf_rate = float(mf_rate if mf_rate is not None else _r["money_fund"])
    _repo_min = _r["repo_min_lot"]
    _repo_fee = float(_r.get("repo_fee_annual_rate")
                      or float(_r["repo_fee_rate"]) * float(_r["repo_trades_per_year"]))
    _repo_fee *= float(_r.get("repo_commission_discount", 1.0))
    _cash_buf = float(config.PAPER.get("cash_buffer", 0.02)) * capital
    _last_acc = plan_list[0]["as_of"]
    cash_earned = 0.0
    cash_daily = []          # 每日现金层利息（用于把权益层和现金层拆开做显著性检验）
    realized_gain = 0.0      # 累计已实现盈亏（利润再投资口径：只加已实现，不缩本金）
    _cap_max = float(CAP["max"])

    def _invest_base() -> float:
        # 可投资本 = 本金 + 已实现盈利，上限见 config.CAPITAL（与 paper.investable_capital 同口径）
        if not reinvest:
            return capital
        return min(_cap_max, capital + max(0.0, realized_gain))

    def _realize(h: dict, sh: int, proceeds: float, fee_amt: float) -> float:
        """卖出时把成本按比例结转，记已实现盈亏（含买入费用的成本口径）。"""
        nonlocal realized_gain
        cp = h["cost"] * (sh / h["shares"]) if h["shares"] else 0.0
        realized_gain += proceeds - fee_amt - cp
        h["cost"] = max(0.0, h["cost"] - cp)
        return cp

    plan_idx = 0
    _state: dict = {}
    for day in [c for c in cal if c >= plan_list[0]["as_of"]]:
        # --- 0. 过夜现金计息（1 天期逆回购 T+1 到期，开盘前资金已可用，所以挂单不冻结）
        _d = (day - _last_acc).days if _last_acc is not None else 0
        _net_day = 0.0
        if _d > 0 and cash > 0:
            deployable = max(0.0, cash - _cash_buf)
            lots = int(deployable // _repo_min) if _repo_min > 0 else 0
            repo_amt = lots * _repo_min
            mf_amt = deployable - repo_amt
            # 回测只有固定利率假设、没有历史日频利率/万份收益序列，这里用「年化 × 天数」
            # 是数学等值的逐日结算简化；线上按逐日实际利率/万份收益入账（paper.accrue_cash_lots），
            # 两者的差异只来自利率波动，不改变回测结论方向。
            net = (repo_amt * _repo_rate * _d / 365.0
                   + mf_amt * _mf_rate * _d / 365.0
                   - repo_amt * _repo_fee * _d / 365.0)
            cash += net
            cash_earned += net
            _net_day = net
        _last_acc = day
        cash_daily.append(_net_day)
        # --- 1. 开盘成交（来自上一决策或止损触发）
        if pending_buy or forced_sell or pending_trim:
            # 1a. 超仓位上限的砍仓（事件驱动模式专用，先卖后买，卖出释放现金）
            for sym, sh in list(pending_trim.items()):
                s, pos = hist.get(sym), holdings.get(sym)
                px = s.at(day, "open") if s else None
                if not s or not pos or px is None:
                    pending_trim.pop(sym, None)
                    continue
                sh = min(int(sh), int(pos["shares"]))
                if sh <= 0:
                    pending_trim.pop(sym, None)
                    continue
                amt = sh * px
                _f = fee(amt)
                cash += amt - _f
                _realize(pos, sh, amt, _f)
                trades.append({"day": day, "sym": sym, "side": "sell", "shares": sh,
                               "price": px, "amount": amt, "why": "超仓位上限砍仓"})
                pos["shares"] -= sh
                if pos["shares"] == 0:
                    del holdings[sym]
                pending_trim.pop(sym, None)
            for sym in list(forced_sell):
                s, pos = hist.get(sym), holdings.get(sym)
                if not s or not pos:
                    forced_sell.remove(sym)
                    continue
                px = s.at(day, "open")
                if px is None:
                    continue
                amt = pos["shares"] * px
                _f = fee(amt)
                cash += amt - _f
                _realize(pos, int(pos["shares"]), amt, _f)
                trades.append({"day": day, "sym": sym, "side": "sell", "shares": pos["shares"],
                               "price": px, "amount": amt, "why": pos.get("exit_why", "止损")})
                del holdings[sym]
                forced_sell.remove(sym)
            for sym, tgt in list(pending_buy.items()):
                s = hist.get(sym)
                if not s:
                    pending_buy.pop(sym, None)
                    continue
                px = s.at(day, "open")
                if px is None:
                    continue
                cur = holdings.get(sym, {}).get("shares", 0)
                need = tgt["shares"] - cur
                _min_tv = config.PAPER["min_trade_value"]
                _band = config.PAPER["no_trade_band"]
                if need > 0:
                    amt = need * px
                    if cur > 0 and (amt < _min_tv or amt / (cur * px) < _band):
                        pending_buy.pop(sym, None)   # 不交易带内：手续费比差额还贵
                        continue
                    if amt + fee(amt) > cash:
                        continue  # 钱不够，等下次决策
                    cash -= amt + fee(amt)
                    if cur == 0:
                        holdings[sym] = {"shares": need, "cost": amt + fee(amt), "name": tgt["name"],
                                         "stop": px * (1 + p["stop_loss_pct"]), "peak": px}
                    else:
                        h = holdings[sym]
                        h["shares"] += need
                        h["cost"] += amt + fee(amt)
                        h["peak"] = max(h["peak"], px)
                    trades.append({"day": day, "sym": sym, "side": "buy", "shares": need,
                                   "price": px, "amount": amt, "why": "建仓/加仓"})
                elif need < 0:
                    h = holdings.get(sym)
                    if h:
                        sell_sh = min(-need, h["shares"])
                        if tgt["shares"] > 0:
                            trim_val = sell_sh * px
                            if trim_val < _min_tv or trim_val / (h["shares"] * px) < _band:
                                pending_buy.pop(sym, None)
                                continue
                        amt = sell_sh * px
                        _f = fee(amt)
                        cash += amt - _f
                        _realize(h, sell_sh, amt, _f)
                        h["shares"] -= sell_sh
                        trades.append({"day": day, "sym": sym, "side": "sell", "shares": sell_sh,
                                       "price": px, "amount": amt, "why": "减仓"})
                        if h["shares"] == 0:
                            del holdings[sym]
                pending_buy.pop(sym, None)
            pending_buy = {}

        # --- 2. 收盘估价 + 止损更新（先结算再决策：决策看的就是当天收盘价）
        mv = 0.0
        for sym, h in list(holdings.items()):
            s = hist.get(sym)
            px = s.at(day, "close") if s else None
            if px is None:
                # PIT 全集里会遇到「曾经存在、后来清盘/退市」的 ETF：按最后一次成交价清算，
                # 否则持仓永远卖不掉、只能按归零计（会把结果压得比现实更差）。
                lseen = h.get("last_seen")
                if lseen is not None and (day - lseen).days > 15:
                    lp = h.get("last_px") or 0.0
                    amt = h["shares"] * lp
                    _f = fee(amt)
                    cash += amt - _f
                    _realize(h, int(h["shares"]), amt, _f)
                    trades.append({"day": day, "sym": sym, "side": "sell", "shares": h["shares"],
                                   "price": lp, "amount": amt, "why": "停牌/清盘清算（按最后成交价）"})
                    del holdings[sym]
                continue
            h["last_px"], h["last_seen"] = px, day
            mv += h["shares"] * px
            i = s.upto(day)
            cost_px = h["cost"] / h["shares"]
            gain = px / cost_px - 1
            if gain > p["trail_start"]:
                a = atr(s.close, s.high, s.low, i)
                if a:
                    h["stop"] = max(h["stop"], px - p["atr_mult"] * a)
            if px <= h["stop"] and stops:
                h["exit_why"] = f"跌破止损 {h['stop']:.3f}"
                if sym not in forced_sell:
                    forced_sell.append(sym)
        nav = cash + mv
        peak_nav = max(peak_nav, nav)
        dd = nav / peak_nav - 1.0
        nav_curve.append({"day": day, "nav": nav, "cash": cash, "mv": mv, "dd": dd})
        benchmark_curve.append({"day": day, "close": bench.at(day, "close")})

        if halt_until is None and dd <= p["halt_dd"]:
            # 组合熔断：清仓，暂停 4 周
            for sym in list(holdings):
                holdings[sym]["exit_why"] = "组合回撤熔断"
                if sym not in forced_sell:
                    forced_sell.append(sym)
            halt_until = day + pd.Timedelta(weeks=4)

        # --- 3. 新的决策：算目标、排单（次日开盘成交）
        if plan_idx < len(plan_list) and plan_list[plan_idx]["as_of"] == day:
            if halt_until is not None and day < halt_until:
                plan_idx += 1   # 熔断冷静期内不做新决策
                continue
            reg = regime_at(bench, day, p)
            do_rebuild = True
            if rebalance == "regime_change":
                do_rebuild = _state.get("regime") is None or reg["regime"] != _state.get("regime")
            elif rebalance == "monthly":
                do_rebuild = _state.get("month") is None or _state.get("month") != day.month
            elif rebalance == "once":
                do_rebuild = _state.get("built") is None
            _state["built"] = True
            _state["regime"] = reg["regime"]
            _state["month"] = day.month
            if not do_rebuild:
                plan_idx += 1
                continue
            ratio = min(tier_ratio, reg["cap"])
            if reg["regime"] == "未知":
                ratio = min(tier_ratio, 0.40)
            rows = []
            for sym, name, cls, und in pool_meta:
                s = hist.get(sym)
                if not s or s.upto(day) < 60:
                    continue
                r = etf_eval(s, day)
                if not r or not eligible(r):
                    continue
                rows.append({"sym": sym, "name": name, "cls": cls, "underlying": und,
                             "score": score(r), **r})
            _rank = {"宽基": 0, "策略": 1, "行业": 2}
            rows.sort(key=lambda x: (_rank.get(x["cls"], 3), -x["score"]))
            _inv = _invest_base()
            tgt = build_target(rows, _inv, ratio, p)
            pending_buy = {k: v for k, v in tgt["targets"].items() if k not in forced_sell}
            if rebalance == "incremental":
                # 事件驱动：持仓没被判坏就留着，只在「超仓位上限」时按最差的先砍
                cap_val = ratio * _inv
                _tol = max(cap_val * float(config.PAPER.get("cap_tolerance") or 0.05),
                           float(config.PAPER["min_trade_value"]))
                excess = mv - cap_val
                if excess > _tol:
                    scored = {x["sym"]: x["score"] for x in rows}
                    trim = {}
                    for sym in sorted(holdings, key=lambda s_: scored.get(s_, -1.0)):
                        if excess <= 1e-6:
                            break
                        s = hist.get(sym)
                        px = s.at(day, "close") if s else None
                        if not s or px is None:
                            continue
                        sh = int(np.ceil(excess / px / 100.0)) * 100
                        sh = min(sh, int(holdings[sym]["shares"]))
                        if sh <= 0:
                            continue
                        trim[sym] = sh
                        excess -= sh * px
                    pending_trim = trim
            else:
                # 目标外的持仓清仓
                for sym in list(holdings):
                    if sym not in pending_buy:
                        s = hist.get(sym)
                        px = s.at(day, "close") if s else None
                        if px is not None and px * holdings[sym]["shares"] >= 1:
                            holdings[sym]["exit_why"] = "调出目标组合"
                            forced_sell.append(sym)
            plan_idx += 1

    # ------------------------------------------------------------------ 统计
    navs = pd.DataFrame(nav_curve)
    navs["bench"] = [x["close"] for x in benchmark_curve]
    navs["bench_nav"] = navs["bench"] / navs["bench"].dropna().iloc[0] * capital
    navs["strategy_nav"] = navs["nav"]

    def stats_of(series: pd.Series) -> dict:
        s = series.dropna()
        if len(s) < 2:
            return {}
        total = float(s.iloc[-1] / s.iloc[0] - 1)
        yrs = max((navs["day"].iloc[-1] - navs["day"].iloc[0]).days / 365.25, 1e-9)
        cagr = float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1)
        peak, dd = -np.inf, 0.0
        for x in s:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1.0)
        daily = s.pct_change().dropna()
        return {"total_return": total, "cagr": cagr, "max_dd": dd,
                "vol": float(daily.std() * math.sqrt(244)),
                "sharpe": float(daily.mean() / daily.std() * math.sqrt(244)) if daily.std() else None}

    # ---- 显著性检验（block bootstrap）：策略/超额/权益层是否显著不为 0
    def _bootstrap(n: int = 0) -> dict:
        if not n:
            return {}
        rng = np.random.default_rng(7)
        base_nav = navs["strategy_nav"].to_numpy()
        strat_r = navs["strategy_nav"].pct_change().fillna(0.0).to_numpy()
        bench_r = navs["bench_nav"].pct_change().fillna(0.0).to_numpy()
        prev_nav = np.concatenate([[base_nav[0]], base_nav[:-1]])
        eq_r = strat_r - np.asarray(cash_daily, dtype=float) / prev_nav
        excess = strat_r - bench_r

        def _boot(x) -> dict:
            m, blk = len(x), 5
            out = []
            for _ in range(n):
                arr: list[float] = []
                while len(arr) < m:
                    st = int(rng.integers(0, max(1, m - blk + 1)))
                    arr.extend(x[st:st + blk])
                out.append(float(np.mean(arr[:m]) * 244))
            a = np.asarray(out)
            return {"mean": float(a.mean()), "p_lo": float(np.percentile(a, 2.5)),
                    "p_hi": float(np.percentile(a, 97.5)),
                    "p_gt0": float((a > 0).mean())}
        return {"n": n, "block": 5, "strategy": _boot(strat_r),
                "excess": _boot(excess), "equity_layer": _boot(eq_r)}

    res = {
        "period": {"start": str(navs["day"].iloc[0]), "end": str(navs["day"].iloc[-1]),
                   "days": int((navs["day"].iloc[-1] - navs["day"].iloc[0]).days),
                   "decisions": len(plan_list), "capital": capital, "tier": tier,
                   "rebalance": rebalance, "stops": bool(stops), "pool": pool,
                   "pool_n": len(pool_meta)},
        "strategy": stats_of(navs["strategy_nav"]),
        "benchmark": stats_of(navs["bench_nav"]),
        "final": {"nav": round(float(navs["strategy_nav"].iloc[-1]), 2),
                  "bench_nav": round(float(navs["bench_nav"].iloc[-1]), 2),
                  "cash": round(cash, 2), "holdings": len(holdings)},
        "trades": len(trades), "fees": round(sum(fee(t["amount"]) for t in trades), 2),
        "cash_yield": round(cash_earned, 2),
        "reinvest": bool(reinvest), "realized_gain": round(realized_gain, 2),
        "investable_end": round(_invest_base(), 2),
        "bootstrap": _bootstrap(bootstrap_n),
        "cash_rates": {"repo": _repo_rate, "mf": _mf_rate},
        "halted_days": 0 if halt_until is None else 1,
    }
    res["strategy"]["excess"] = res["strategy"]["total_return"] - res["benchmark"]["total_return"]
    res["trades_log"] = [{"day": str(t["day"]), "sym": t["sym"], "side": t["side"],
                          "shares": t["shares"], "price": round(t["price"], 3),
                          "amount": round(t["amount"], 2), "fee": round(fee(t["amount"]), 2),
                          "why": t["why"]} for t in trades]
    navs["exposure"] = navs["mv"] / navs["nav"]

    # 年度收益
    navs["year"] = [d.year for d in navs["day"]]
    yearly = []
    for y, g in navs.groupby("year"):
        if len(g) < 2:
            continue
        yearly.append({"year": int(y),
                       "strategy": round(float(g["strategy_nav"].iloc[-1] / g["strategy_nav"].iloc[0] - 1), 4),
                       "benchmark": round(float(g["bench_nav"].iloc[-1] / g["bench_nav"].iloc[0] - 1), 4)})
    res["yearly"] = yearly
    return res, navs


# ------------------------------------------------------------------ 报告
def report(res: dict) -> str:
    p, s, b = res["period"], res["strategy"], res["benchmark"]
    _r = config.cash_rates()   # 报告里的现金层利率也从唯一来源取，不写死
    lines = ["# 历史回放报告（市场状态 + ETF 打分）", "",
             f"- 区间：{p['start']} → {p['end']}（{p['days']} 天，{p['decisions']} 次决策）",
             f"- 本金：{p['capital']:,.0f} 元 · {p['tier']} 档 · 调仓模式 {p['rebalance']} · "
             f"{'含' if p['stops'] else '不含'}止损 · 回测假定周三/周六双决策、次一交易日开盘成交（与线上每周三、周六 20:30 一致；且"
             f"买入已改限价条件单，回测按开盘价买入，成交假设偏乐观）",
             f"- 标的池：{'PIT 全集（' + str(p.get('pool_n', 0)) + ' 只，按当日数据判合格 → 无生存者偏差）' if p.get('pool') == 'pit' else '今日筛选结果（' + str(p.get('pool_n', 0)) + ' 只，有生存者偏差、结果偏乐观）'}",
             f"- 期末：策略 {res['final']['nav']:,.2f} 元 vs 沪深300 {res['final']['bench_nav']:,.2f} 元", "",
             "## 一、总览", "",
             "| 指标 | 策略 | 沪深300（买入持有） |", "|---|---|---|",
             f"| 总收益 | {s['total_return']:+.2%} | {b['total_return']:+.2%} |",
             f"| 年化 | {s['cagr']:+.2%} | {b['cagr']:+.2%} |",
             f"| 最大回撤 | {s['max_dd']:+.2%} | {b['max_dd']:+.2%} |",
             f"| 年化波动 | {s['vol']:.2%} | {b['vol']:.2%} |",
             f"| 夏普(rf 2%，沿用 metrics.sharpe 默认值，未随无风险利率更新) | {_f2(s['sharpe'])} | {_f2(b['sharpe'])} |",
             f"| 超额收益 | {s['excess']:+.2%} | — |", "",
             "## 二、年度收益", "",
             "| 年份 | 策略 | 沪深300 | 差额 |", "|---|---|---|---|"]
    for y in res["yearly"]:
        lines.append(f"| {y['year']} | {y['strategy']:+.2%} | {y['benchmark']:+.2%} | "
                     f"{y['strategy'] - y['benchmark']:+.2%} |")
    lines += ["", "## 三、成交与费用", "",
              f"- 成交笔数：{res['trades']}；累计费用：{res['fees']:,.2f} 元"
              f"（占本金 {res['fees'] / p['capital']:.2%}）",
              f"- 现金层计息：+{res.get('cash_yield', 0.0):,.2f} 元"
              f"（逆回购 {res.get('cash_rates', {}).get('repo', _r['repo']):.2%} / "
              f"货基 {res.get('cash_rates', {}).get('mf', _r['money_fund']):.2%}，"
              f"按现金流实际闲置天数逐段计；{_r['as_of']} 口径的假设，不是未来收益承诺）", "",
              "## 四、显著性检验（block bootstrap，5 日块）", ""]
    bs = res.get("bootstrap") or {}
    if bs:
        lines += [f"- 重抽样 {bs['n']} 次，块长 {bs['block']} 个交易日；表中为**年化**收益的 95% 区间与「>0 的概率」。",
                  "",
                  "| 序列 | 均值 | 95% 区间 | P(年化>0) |", "|---|---|---|---|",
                  f"| 策略日收益 | {bs['strategy']['mean']:+.2%} | "
                  f"[{bs['strategy']['p_lo']:+.2%}, {bs['strategy']['p_hi']:+.2%}] | "
                  f"{bs['strategy']['p_gt0']:.1%} |",
                  f"| 超额（策略−沪深300） | {bs['excess']['mean']:+.2%} | "
                  f"[{bs['excess']['p_lo']:+.2%}, {bs['excess']['p_hi']:+.2%}] | "
                  f"{bs['excess']['p_gt0']:.1%} |",
                  f"| **权益层（剔掉现金利息）** | {bs['equity_layer']['mean']:+.2%} | "
                  f"[{bs['equity_layer']['p_lo']:+.2%}, {bs['equity_layer']['p_hi']:+.2%}] | "
                  f"{bs['equity_layer']['p_gt0']:.1%} |",
                  "",
                  "读法：区间跨过 0、P(>0) 只有五成左右，就说明「这份收益和运气分不开」——"
                  "策略层不能宣称有稳定 alpha；权益层那一行才是「选股/择时赚没赚到钱」。", ""]
    else:
        lines += ["本次未做（加 `--bootstrap 2000`）。", ""]
    if res.get("reinvest"):
        lines += [f"- 利润再投资：已实现盈亏 {res.get('realized_gain', 0.0):+,.2f} 元，"
                  f"期末可投资本 {res.get('investable_end', 0.0):,.2f} 元（本金 + 已实现盈利，上限见 config.CAPITAL）。", ""]
    lines += ["## 五、这个数字能信到什么程度", "",
              ("回放的是**PIT 全集**：每个决策日只用当时已有的 ETF、按当时的数据判合格，"
               "清盘/退市的按最后成交价清算 → 生存者偏差已消除；" if p.get("pool") == "pit" else
               "回放的是**今天通过筛选的 20 只 ETF**，历史上被淘汰的没进池子 → 生存者偏差，结果偏乐观；"),
              "再平衡按一次到位成交（实盘三批 + 不交易带），换手略高。",
              "现金按逆回购 + 场外货基计息（与阶段三 `capital.cash_layer()` 同一份利率，"
              f"取自 config.cash_rates()：逆回购 {_r['repo']:.2%} / 货基 {_r['money_fund']:.2%}，"
              f"{_r['as_of']} 口径）。这是当前时点的假设，不是对未来收益的承诺；"
              "真实的逆回购偶有季末高息，这里按常态估、不追高。",
              "个股路径没有回放：财务与估值分位没有 point-in-time 数据，强行回放是前视偏差。",
              "所以这张表只回答一件事：**在 ETF 承接这条可执行路径上，这套规则有没有正期望**；"
              "个股层由前向模拟盘（12 周）回答。", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--tier", default="B")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=str(pd.Timestamp.today().date()))
    ap.add_argument("--decisions", default="wed_sat", choices=["wed_sat", "wed", "sat", "week"])
    ap.add_argument("--rebalance", default="weekly",
                    choices=["weekly", "regime_change", "monthly", "once", "incremental"])
    ap.add_argument("--no-stops", action="store_true")
    ap.add_argument("--compare", action="store_true", help="跑多组参数对比并写对比报告")
    ap.add_argument("--sweep", action="store_true",
                    help="B 类参数区间扫描（状态阈值/档位/止损/集中度），写 out/backtest_sweep.md")
    ap.add_argument("--out", default="")
    ap.add_argument("--cash-repo-rate", type=float, default=None,
                    help="逆回购年化覆盖，默认取 config.cash_rates() 的 repo；敏感性测试用")
    ap.add_argument("--cash-mf-rate", type=float, default=None,
                    help="场外货基年化覆盖，默认取 config.cash_rates() 的 money_fund")
    ap.add_argument("--reinvest", action="store_true",
                    help="利润再投资：可投资本 = 本金 + 已实现盈利（上限见 config.CAPITAL），与线上账本同口径")
    ap.add_argument("--bootstrap", type=int, default=0,
                    help="block bootstrap 重抽样次数（如 2000）做显著性检验；0 = 不做")
    ap.add_argument("--pool", default="stage1", choices=["stage1", "pit"],
                    help="stage1=今天筛出的候选池（有生存者偏差）；pit=时间点无关全集（先跑 etf_pit.py）")
    a = ap.parse_args()
    print(f"[backtest] {a.start} → {a.end} 本金 {a.capital:,.0f} 档位 {a.tier}")


    if a.sweep:
        variants = [
            ("基准（现行）", {}),
            ("攻击线 +1%", {"attack_dev": 0.01}),
            ("攻击线 +3%", {"attack_dev": 0.03}),
            ("攻击线 +5%", {"attack_dev": 0.05}),
            ("防守线 -5%", {"defense_dev": -0.05}),
            ("防守线 -10%", {"defense_dev": -0.10}),
            ("防守线 -12%", {"defense_dev": -0.12}),
            ("中性上限 50%", {"neutral_cap": 0.50}),
            ("中性上限 70%", {"neutral_cap": 0.70}),
            ("防守上限 25%", {"defense_cap": 0.25}),
            ("防守上限 45%", {"defense_cap": 0.45}),
            ("档位 A（26%）", {"tier_ratio": 0.26}),
            ("档位 C（64%）", {"tier_ratio": 0.64}),
            ("止损 -10%", {"stop_loss_pct": -0.10}),
            ("止损 -20%", {"stop_loss_pct": -0.20}),
            ("ATR 2.0", {"atr_mult": 2.0}),
            ("ATR 3.5", {"atr_mult": 3.5}),
            ("移动止损启用 5%", {"trail_start": 0.05}),
            ("移动止损启用 15%", {"trail_start": 0.15}),
            ("熔断 -12%", {"halt_dd": -0.12}),
            ("熔断 -20%", {"halt_dd": -0.20}),
            ("单票上限 15%", {"max_single": 0.15}),
        ]
        rows, base = [], None
        for label, params in variants:
            res, _ = run(a.capital, a.tier, a.start, a.end, a.decisions, a.rebalance,
                         not a.no_stops, a.cash_repo_rate, a.cash_mf_rate, a.reinvest, 0,
                         pool=a.pool, params=params)
            st, bm = res["strategy"], res["benchmark"]
            if base is None:
                base = bm
            calmar = (st["cagr"] / abs(st["max_dd"])) if st["max_dd"] else None
            rows.append({"variant": label, "params": params,
                         "cagr": st["cagr"], "total": st["total_return"],
                         "max_dd": st["max_dd"], "sharpe": st["sharpe"],
                         "calmar": calmar, "excess": st["excess"],
                         "trades": res["trades"], "fees": res["fees"]})
            print(f"  {label}: 年化 {st['cagr']:+.2%} / 回撤 {st['max_dd']:+.2%} / "
                  f"Calmar {_f2(calmar)} / 夏普 {_f2(st['sharpe'])} / 成交 {res['trades']} 笔")
        ranked = sorted(rows, key=lambda r: (r["calmar"] is not None, r["calmar"] or -9), reverse=True)
        md = ["# B 类参数区间扫描（回测）", "",
              f"- 区间 {a.start} → {a.end[0:10]}，本金 {a.capital:,.0f}，{a.tier} 档，"
              f"调仓 {a.rebalance}，决策 {a.decisions}",
              f"- 标的池：{'PIT 全集' if a.pool == 'pit' else '今日筛选结果（生存者偏差，绝对值偏乐观、只比相对差异）'}",
              f"- 沪深300 基准：年化 {base['cagr']:+.2%} / 回撤 {base['max_dd']:+.2%}", "",
              "| 参数 | 年化 | 最大回撤 | Calmar | 夏普 | 超额 | 成交 | 费用 |",
              "|---|---|---|---|---|---|---|---|"]
        for r in ranked:
            md.append(f"| {r['variant']} | {r['cagr']:+.2%} | {r['max_dd']:+.2%} | "
                      f"{_f2(r['calmar'])} | {_f2(r['sharpe'])} | {r['excess']:+.2%} | "
                      f"{r['trades']} | {r['fees']:,.0f} |")
        md += ["", "读法：单参数改动的绝对值只用于比较方向；标的池有生存者偏差时，"
               "不要拿绝对值当预期收益。Calmar = 年化 / |最大回撤|。", ""]
        outp = a.out or os.path.join(config.OUT_DIR, "backtest_sweep.md")
        with open(outp, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        with open(outp.replace(".md", ".json"), "w", encoding="utf-8") as f:
            json.dump({"benchmark": base, "rows": rows}, f, ensure_ascii=False, indent=2)
        print("\n".join(md))
        print(f"\n[backtest] 扫描报告 {outp}")
        return

    if a.compare:
        variants = [
            ("历史对照（建仓一次 once + 止损）", "once", True),
            ("每周全额再平衡+止损（旧版 full）", "weekly", True),
            ("每周再平衡，不止损", "weekly", False),
            ("只在市场状态变化时调仓+止损", "regime_change", True),
            ("只在市场状态变化时调仓，不止损", "regime_change", False),
            ("每月调仓一次+止损", "monthly", True),
            ("每月调仓一次，不止损", "monthly", False),
            ("只建仓一次，永不调仓，不止损", "once", False),
            ("事件驱动 incremental + 止损", "incremental", True),
            ("事件驱动，不止损", "incremental", False),
        ]
        rows, markdown, base = [], [], None
        for label, rb, st in variants:
            res, navs = run(a.capital, a.tier, a.start, a.end, a.decisions, rb, st,
                            a.cash_repo_rate, a.cash_mf_rate, a.reinvest, a.bootstrap,
                            pool=a.pool)
            s, b = res["strategy"], res["benchmark"]
            if base is None:
                base = b
            rows.append({"variant": label, "rebalance": rb, "stops": st,
                         "total": s["total_return"], "cagr": s["cagr"], "max_dd": s["max_dd"],
                         "sharpe": s["sharpe"], "vol": s["vol"], "excess": s["excess"],
                         "trades": res["trades"], "fees": res["fees"],
                         "cash_yield": res.get("cash_yield", 0.0),
                         "final_nav": res["final"]["nav"]})
            print(f"  {label}: 总收益 {s['total_return']:+.2%} / 年化 {s['cagr']:+.2%} / "
                  f"最大回撤 {s['max_dd']:+.2%} / 夏普 {_f2(s['sharpe'])} / "
                  f"成交 {res['trades']} 笔 / 费用 {res['fees']:.0f} 元")
        b = base
        markdown = ["# 历史回放：规则变体对比", "",
                    f"- 区间：{a.start} → {a.end[0:10]}，本金 {a.capital:,.0f} 元，{a.tier} 档",
                    f"- 标的池：{'PIT 全集（按当日数据判合格，无生存者偏差）' if a.pool == 'pit' else '今日筛选结果（有生存者偏差、结果偏乐观）'}",
                    f"- 沪深300 买入持有：总收益 {b['total_return']:+.2%}，年化 {b['cagr']:+.2%}，"
                    f"最大回撤 {b['max_dd']:+.2%}，夏普 {_f2(b['sharpe'])}", "",
                    "| 变体 | 总收益 | 年化 | 最大回撤 | 夏普 | 超额(vs HS300) | 现金层利息 | 成交笔数 | 费用 |",
                    "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            markdown.append(f"| {r['variant']} | {r['total']:+.2%} | {r['cagr']:+.2%} | "
                            f"{r['max_dd']:+.2%} | {_f2(r['sharpe'])} | {r['excess']:+.2%} | "
                            f"{r.get('cash_yield', 0.0):,.0f} 元 | {r['trades']} | {r['fees']:,.0f} 元 |")
        markdown += ["", "十组都只是「同一套选股/择时逻辑 + 不同换手与止损」，",
                     "差别在于：交易越频繁，最低佣金 5 元 / 笔在 800 元仓位上就是 0.6%+ 的固定成本。",
                     "权益层 = 总收益 − 现金层利息 ÷ 本金：先把「闲置现金吃利息」和「买卖赚/亏」分开，"
                     "再判断策略本身有没有 alpha。",
                     "同一规则在不同本金下差异极大：最低佣金在 800 元仓位上就是 0.6%+，"
                     "所以「策略好不好」必须按本金分别看；历史回测数字见 "
                     "references/engine-notes.md §七。",
                     "线上现行口径：**单账本 50k，rebalance_mode = regime_change**"
                     "（只在沪深300 状态变化时重建；历史数字见 references/engine-notes.md §七）；"
                     "once / incremental 保留为回滚选项。", ""]
        outp = a.out or os.path.join(config.OUT_DIR, "backtest_compare.md")
        with open(outp, "w", encoding="utf-8") as f:
            f.write("\n".join(markdown))
        with open(outp.replace(".md", ".json"), "w", encoding="utf-8") as f:
            json.dump({"benchmark": base, "variants": rows}, f, ensure_ascii=False, indent=2)
        print("\n".join(markdown))
        print(f"\n[backtest] 对比报告 {outp}")
        return

    res, navs = run(a.capital, a.tier, a.start, a.end, a.decisions, a.rebalance, not a.no_stops,
                    a.cash_repo_rate, a.cash_mf_rate, a.reinvest, a.bootstrap, pool=a.pool)
    md = report(res)
    out = a.out or os.path.join(config.OUT_DIR, "backtest_report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    jpath = out.replace(".md", ".json")
    trades_log = res.pop("trades_log", [])
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    if trades_log:
        pd.DataFrame(trades_log).to_csv(out.replace(".md", "_trades.csv"),
                                        index=False, encoding="utf-8-sig")
    csvp = out.replace(".md", "_nav.csv")
    navs.to_csv(csvp, index=False, encoding="utf-8-sig")
    print(md)
    print(f"\n[backtest] 报告 {out}\n[backtest] 净值 {csvp}")


if __name__ == "__main__":
    main()
