# -*- coding: utf-8 -*-
"""paper.py 账本自检（完全离线，用合成日线，不联网）。

跑法：
    python scripts/_selftest_paper.py

覆盖：
  1. 订单生效日之前不成交（防未来函数）
  2. buy_open 按次日开盘价成交 + 费用摊进成本
  3. buy_limit 只在最低价触及限价时成交，且按限价
  4. sell 记已实现盈亏、持仓清零
  5. sell_stop 跳空按开盘价（更差的价格）
  6. 初始止损 / 移动止损只上移、浮盈 <10% 不动
  7. 组合回撤熔断全清
  8. enforce_cash 按「第 1 批优先、金额大的优先」砍单
  9. target_orders 三批拆分（**全部限价单**，2026-11-18 起）+ 已持有不偏离不交易
 10. settle 净值/回撤 + stats 胜率与盈亏比
"""
import json
import os
import shutil
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_osp = os.path   # 目录/存在性判断用

import config
import otc_book
import paper

import contextlib


@contextlib.contextmanager
def _legacy_repo7():
    """把 config.cash_rates() 的 cash_policy 临时压回旧口径（观望资金→7/14/28 天期逆回购）。

    2026-11-15 起默认口径改为「半流动性货基池」，本文件第 14 节等
    逆回购计息断言仍按旧口径验证（回滚路径必须永远可用），故在整段 main()
    执行期间把 policy 压回 orders_mf_idle_repo7；新默认口径由 _check_pool_default() 单独验。
    """
    orig = config.cash_rates

    def patched(*a, **k):
        d = dict(orig(*a, **k))
        d["cash_policy"] = "orders_mf_idle_repo7"
        return d

    config.cash_rates = patched
    try:
        yield
    finally:
        config.cash_rates = orig


def _check_pool_default():
    """新默认口径：观望资金 → 半流动性货基池（90 天口径前 N 支、每支 ≤1 万快赎）。"""
    st = paper.new_state(10000.0, "B", "2026-10-16", 4000.0)
    st["cash"] = 10000.0
    p = paper.cash_yield_plan(st)
    check(p["cash_policy"] == "orders_mf_idle_pool", "默认 policy = orders_mf_idle_pool")
    check(p["repo_amount"] == 0.0, "默认不再买逆回购（repo_amount = 0）")
    check(abs(p["sleeve_amount"] + p["money_fund"] - p["deployable"]) < 0.01,
          "半流动性金额 + 朝朝宝 = 现金 − 2% 缓冲")
    check(all(float(h["amount"]) <= 10000.0 + 1e-9 for h in p["sleeve_holdings"]),
          "每支货基 ≤1 万快赎额度")
    check(paper.cash_yield_plan(st, container="repo7")["repo_amount"] == 9000.0,
          "显式 container='repo7' 仍回滚到旧口径（9 手 9,000 元）")


def _check_pool_accrual():
    """货基池计息：排行过期沿用最后持仓；逐日万份收益入账；持仓持久化到 sleeve_lots。"""
    import cash_sleeve as cs
    st = paper.new_state(50000.0, "B", "2026-10-16", 4000.0)
    st["cash"] = 30000.0
    st["holdings"] = {}
    st["orders"] = []
    st["nav"] = [{"date": "2026-11-18", "nav": 50000.0, "cash": 30000.0}]
    st["cash_marks"] = {"last_accrued": "2026-11-18"}
    rank = {"as_of": "2026-11-19", "data_date": "2026-01-01", "rows": [
        {"code": "A", "name": "甲货基", "y90": 0.0131, "buy": "限制大额申购"},
        {"code": "B", "name": "乙货基", "y90": 0.0130, "buy": "开放赎回"},
    ]}
    _old_rank, _old_wf, _old_rh = cs.load_rank, paper._wanfen_daily, paper._rate_history
    cs.load_rank = lambda path=None: rank
    paper._wanfen_daily = (
        lambda code, amount, a, b: (round(float(amount) * (0.00004 if code == "A" else 0.00003), 4), 2, 0))
    paper._rate_history = lambda: {"2026-11-18": {"r1": 0.0106}}
    try:
        got = paper.accrue_cash_lots(st, "2026-11-20",
                                     ["2026-11-18", "2026-11-19", "2026-11-20"])
        lots = (st.get("sleeve_lots") or {}).get("holdings") or []
        check(len(lots) == 2, f"排行过期仍按最后持仓计息并写入 sleeve_lots（实际 {len(lots)} 支）")
        pool = [h for h in st.get("repo_history", []) if h.get("kind") == "pool"]
        check(len(pool) == 2 and all(str(h.get("basis", "")).startswith("万份收益逐日")
                                     for h in pool),
              "池子利息按逐日万份收益入账（basis=万份收益逐日）")
        check(abs(float(got) - 1.2227) < 0.05,
              f"池子万份收益 0.70 + 朝朝宝 0.52 ≈ 1.22 元（实际 {got}）")
        _cy = [e for e in st.get("events", []) if e.get("level") == "cash_yield"]
        _msg = str(_cy[-1].get("msg")) if _cy else ""
        check("按逐日万份收益实得" in _msg and "按逐日利率实得" in _msg,
              "计息文案写实得金额/实得年化（真实口径，不写标签利率）")
        check("只用于选基、不是计息利率" in _msg,
              "计息文案标明 90 天口径只用于选基")
        check(any("货基池持仓更新" in str(e.get("msg")) for e in st.get("events", [])),
              "首次计息记录货基池持仓快照")
        check(paper.accrue_cash_lots(st, "2026-11-20", ["2026-11-20"]) == 0.0,
              "同日重复计息返回 0")
        # 排行刷新（data_date 变、名次反转）→ 持仓快照按新排名更新
        rank2 = {"as_of": "2026-11-21", "data_date": "2026-11-20", "rows": [
            {"code": "B", "name": "乙货基", "y90": 0.0132, "buy": "开放赎回"},
            {"code": "A", "name": "甲货基", "y90": 0.0131, "buy": "限制大额申购"},
        ]}
        cs.load_rank = lambda path=None: rank2
        paper.accrue_cash_lots(st, "2026-11-21", ["2026-11-19", "2026-11-20", "2026-11-21"])
        lots2 = (st.get("sleeve_lots") or {}).get("holdings") or []
        check(bool(lots2) and lots2[0]["code"] == "B", "排行刷新后按新排名更新持仓快照")

        def _boom(path=None):
            raise RuntimeError("rank gone")

        cs.load_rank = _boom
        _cash0 = float(st["cash"])
        got3 = paper.accrue_cash_lots(st, "2026-11-22",
                                      ["2026-11-20", "2026-11-21", "2026-11-22"])
        pool3 = [h for h in st.get("repo_history", []) if h.get("kind") == "pool"
                 and h.get("date") == "2026-11-22"]
        check(len(pool3) == 2, "排行文件不可用 → 沿用 sleeve_lots 的 2 支继续计息")
        check(got3 > 0.5 and float(st["cash"]) > _cash0, "计息成功、现金增加")
    finally:
        cs.load_rank, paper._wanfen_daily, paper._rate_history = _old_rank, _old_wf, _old_rh


def _check_daily_accrual():
    """生息类逐日结算：朝朝宝按逐日利率、货基按逐日万份收益（缺失日顺延，不回落年化）。"""
    hist = {"2026-11-18": {"r1": 0.02}, "2026-11-20": {"r1": 0.01}}
    check(paper._rate_on(hist, "2026-11-19", "r1") == 0.02,
          "逐日利率：缺失日沿用之前最近一天")
    check(paper._rate_on(hist, "2026-11-17", "r1") is None,
          "逐日利率：窗口前无值返回 None")

    import datasource as ds

    class _FakeDF:
        columns = ["date", "wanfen"]

        def __init__(self, rows):
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def iterrows(self):
            return iter(list(enumerate(self.rows)))

    _old = ds.fetch_money_fund_hist
    try:
        ds.fetch_money_fund_hist = lambda code, days=0: _FakeDF([
            {"date": "2026-11-19", "wanfen": 0.5},
            {"date": "2026-11-20", "wanfen": 0.4},
        ])
        it, covered, carried = paper._wanfen_daily("A", 10000.0, "2026-11-18", "2026-11-20")
        check(covered == 2 and carried == 0, "逐日万份收益：每天各按当日值计息")
        check(abs(it - 0.9000) < 0.001, f"逐日万份收益按日复投（实际 {it}）")
        ds.fetch_money_fund_hist = lambda code, days=0: _FakeDF([
            {"date": "2026-11-18", "wanfen": 0.3},
        ])
        it2, cov2, car2 = paper._wanfen_daily("A", 10000.0, "2026-11-18", "2026-11-21")
        check(cov2 == 3 and car2 == 3, "万份收益缺失日沿用前值（3 天）")
        check(abs(it2 - 0.900027) < 0.001, f"沿用前值仍按日复投（实际 {it2}）")
        ds.fetch_money_fund_hist = lambda code, days=0: _FakeDF([])
        check(paper._wanfen_daily("A", 10000.0, "2026-11-18", "2026-11-20")[0] is None,
              "万份收益完全没有数据 → None，不编造")
    finally:
        ds.fetch_money_fund_hist = _old

    # 朝朝宝：1 天、当日利率 3.65% → 利息 = 本金 × 3.65% / 365
    import cash_sleeve as cs
    st = paper.new_state(50000.0, "B", "2026-10-16", 4000.0)
    st["cash"] = 30000.0
    st["holdings"] = {}
    st["orders"] = []
    st["nav"] = [{"date": "2026-11-18", "nav": 50000.0, "cash": 30000.0}]
    st["cash_marks"] = {"last_accrued": "2026-11-18"}
    plan = paper.cash_yield_plan(st)
    mf_amt = 10000.0
    p = dict(plan)
    p.update({"money_fund": mf_amt, "sleeve_holdings": [], "repo_amount": 0.0,
              "deployable": 30000.0, "repo_locked": 0.0})
    _old_rh, _old_cp = paper._rate_history, paper.cash_yield_plan
    paper._rate_history = lambda: {"2026-11-18": {"r1": 0.0365}}
    paper.cash_yield_plan = (lambda state, close_map=None, container=None,
                                    cap_amount=None, allow_stale_sleeve=False: p)
    try:
        paper.accrue_cash_lots(st, "2026-11-19", ["2026-11-18", "2026-11-19"])
    finally:
        paper._rate_history, paper.cash_yield_plan = _old_rh, _old_cp
    mf_h = [h for h in st.get("repo_history", []) if h.get("kind") == "mf"]
    check(bool(mf_h) and str(mf_h[-1].get("basis", "")).startswith("逐日利率"),
          "朝朝宝按逐日利率入账（basis=逐日利率）")
    check(abs(float(mf_h[-1]["interest"]) - round(mf_amt * 0.0365 / 365.0, 4)) < 0.0002,
          "朝朝宝 1 天利息 = 本金 × 当日利率 / 365")


import capital

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL += 1
        print(f"  XX   {msg}")


def fresh(capital=10000.0):
    st = paper.new_state(capital, "B", "2026-10-23", 4105.33)
    st["nav"] = []
    return st


def bar(date, o, h, l, c):
    return {"date": date, "open": o, "high": h, "low": l, "close": c}


def buy_etf(st, code="sh510300", shares=200, price=4.0, eff="2026-10-26", typ="buy_open",
            limit=None):
    o = paper.make_order(st, "2026-10-23", code, "沪深300ETF", "etf", "buy", shares, typ,
                         "测试买入", limit_price=limit, effective_from=eff)
    if price is not None:
        o["ref_price"] = price
    return o


def _check_chain_accounting():
    """年度结算后：总收益用 cum_index 链式，已实现盈利按 baseline 不重复放大。"""
    print("== 年度结算口径：链式总收益 + 已实现盈利基线 ==")
    st = fresh()
    st["holdings"] = {}
    st["cash"] = 55000.0
    st["meta"]["capital"] = 50000.0
    st["meta"]["cum_index"] = 1.1
    p = paper.portfolio(st, {})
    # A3：分母已是「累计投入」（初始本金 + Σ注资），cum_index 再乘一次会把历史收益放大一遍。
    check(abs(p["total_return"] - 0.10) < 1e-9,
          "总收益 = (nav + 已划出奖励) / 累计投入 − 1（不再乘 cum_index）")
    st["trades"] = [{"action": "sell", "realized_pnl": 1000.0}]
    st["repo_history"] = [{"interest": 100.0, "fee": 0.0}]
    st["meta"]["annual_baseline"] = {"realized": 1000.0, "cash_interest": 100.0}
    inv = paper.investable_capital(st)
    check(inv["gain"] == 0.0 and abs(inv["investable"] - 50000.0) < 1e-9,
          "基线内的已实现盈利不重复放大仓位")
    st["trades"].append({"action": "sell", "realized_pnl": 200.0})
    check(abs(paper.investable_capital(st)["gain"] - 200.0) < 1e-9,
          "基线之后的新增盈利才计入可投资本")


def main(summary: bool = True):
    print("=== 1. 生效日之前不成交 ===")
    st = fresh()
    buy_etf(st, shares=200, price=4.0, eff="2026-10-26")
    bars = {"sh510300": [bar("2026-10-23", 3.9, 4.0, 3.8, 3.95),
                         bar("2026-10-26", 4.0, 4.2, 3.9, 4.1)]}
    ev = paper.resolve_orders(st, "2026-10-23", bars)
    check(not ev and st["orders"][0]["status"] == "pending", "10-23 结算：订单仍未成交")
    ev = paper.resolve_orders(st, "2026-10-26", bars)
    check(len(ev) == 1 and ev[0][0] == "filled", "10-26 结算：按开盘价成交")
    tr = st["trades"][-1]
    check(tr["price"] == 4.0 and tr["date"] == "2026-10-26", f"成交价 4.0 成交日 10-26（实际 {tr['price']}）")
    _fee = max(800 * config.CAPITAL["commission_rate"],
               float(config.CAPITAL["min_commission"] or 0.0))
    check(abs(tr["fee"] - _fee) < 1e-9, f"佣金 = {_fee}（实际 {tr['fee']}）")
    check(abs(float(st["cash"]) - (10000 - 800 - _fee)) < 0.01,
          f"现金 = {10000 - 800 - _fee}（实际 {st['cash']}）")
    h = st["holdings"]["sh510300"]
    check(abs(h["cost_basis"] - (800 + _fee) / 200) < 1e-6,
          f"成本价含手续费 = {(800 + _fee) / 200}（实际 {h['cost_basis']}）")

    print("=== 2. buy_limit 只在最低价触及限价时成交 ===")
    st = fresh()
    buy_etf(st, shares=200, typ="buy_limit", limit=3.90, eff="2026-10-27")
    bars = {"sh510300": [bar("2026-10-27", 3.95, 4.0, 3.92, 3.96),
                         bar("2026-10-28", 3.92, 3.94, 3.85, 3.88)]}
    ev = paper.resolve_orders(st, "2026-10-27", bars)
    check(not ev, "10-27 最低 3.92 > 限价 3.90，不成交")
    ev = paper.resolve_orders(st, "2026-10-28", bars)
    tr = st["trades"][-1]
    check(ev and abs(tr["price"] - 3.90) < 1e-9, f"10-28 最低 3.85 触价，按限价 3.90 成交（实际 {tr['price']}）")

    print("=== 3. sell 记已实现盈亏、持仓清零 ===")
    st = fresh()
    buy_etf(st, shares=200, price=4.0, eff="2026-10-26")
    bars = {"sh510300": [bar("2026-10-26", 4.0, 4.2, 3.9, 4.1),
                         bar("2026-11-02", 4.5, 4.6, 4.4, 4.5)]}
    paper.resolve_orders(st, "2026-10-26", bars)
    so = paper.make_order(st, "2026-10-30", "sh510300", "沪深300ETF", "etf", "sell", 200,
                          "sell_open", "测试卖出", effective_from="2026-11-02")
    paper.resolve_orders(st, "2026-11-02", bars)
    tr = st["trades"][-1]
    check("sh510300" not in st["holdings"], "持仓已清零")
    import capital as _cap
    _fb = _cap.trade_fee(800, is_etf=True)["total"]
    _fs = _cap.trade_fee(900, is_etf=True, side="sell")["total"]
    exp = 900 - _fs - (800 + _fb)
    check(abs(tr["realized_pnl"] - exp) < 0.01, f"已实现盈亏 = {exp:.2f}（实际 {tr['realized_pnl']}）")
    _cash_exp = 10000 - 800 - _fb + 900 - _fs
    check(abs(float(st["cash"]) - _cash_exp) < 0.01,
          f"现金 = {_cash_exp:.2f}（实际 {st['cash']}）")
    check(so["status"] == "filled", "卖单已成交")

    print("=== 4. sell_stop 跳空按开盘价 ===")
    st = fresh()
    buy_etf(st, shares=100, price=4.0, eff="2026-10-26")
    paper.resolve_orders(st, "2026-10-26", {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
    paper.make_order(st, "2026-10-27", "sh510300", "沪深300ETF", "etf", "sell", 100,
                     "sell_stop", "跌破止损", limit_price=3.80, effective_from="2026-10-28")
    paper.resolve_orders(st, "2026-10-28", {"sh510300": [bar("2026-10-28", 3.60, 3.70, 3.50, 3.55)]})
    tr = st["trades"][-1]
    check(abs(tr["price"] - 3.60) < 1e-9, f"跳空按开盘 3.60 成交（实际 {tr['price']}）")

    print("=== 5. 初始止损 + 移动止损只上移 ===")
    st = fresh()
    buy_etf(st, shares=100, price=4.0, eff="2026-10-26")
    paper.resolve_orders(st, "2026-10-26", {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
    st["holdings"]["sh510300"]["stop_price"] = 3.40      # 初始止损（买入价 -15%）
    paper.settle(st, "2026-10-27", {"sh510300": 3.60})
    paper.stop_orders(st, "2026-10-27", {"sh510300": 3.60}, {"sh510300": 0.10})
    check(st["holdings"]["sh510300"]["stop_price"] == 3.40, "浮盈 <10%：止损不动")
    gtc = [o for o in paper.pending_orders(st, "sell") if o["order_type"] == "sell_stop"]
    check(len(gtc) == 1 and abs(gtc[0]["limit_price"] - 3.40) < 1e-9, "未跌破时挂 GTC 条件止损单 @3.40")
    paper.settle(st, "2026-10-28", {"sh510300": 4.60})   # 浮盈约 14%
    paper.stop_orders(st, "2026-10-28", {"sh510300": 4.60}, {"sh510300": 0.10})
    check(abs(st["holdings"]["sh510300"]["stop_price"] - 4.35) < 1e-9,
          f"浮盈 >10%：止损上移到 4.60-2.5*0.10=4.35（实际 {st['holdings']['sh510300']['stop_price']}）")
    check(abs(gtc[0]["limit_price"] - 4.35) < 1e-9, "GTC 条件止损单同步上移到 4.35")
    paper.settle(st, "2026-10-29", {"sh510300": 4.20})
    paper.stop_orders(st, "2026-10-29", {"sh510300": 4.20}, {"sh510300": 0.10})
    check(abs(st["holdings"]["sh510300"]["stop_price"] - 4.35) < 1e-9, "回落时止损不下移（4.35 保持）")

    print("=== 6. 组合回撤熔断全清 ===")
    st = fresh()
    buy_etf(st, shares=100, price=4.0, eff="2026-10-26")
    paper.resolve_orders(st, "2026-10-26", {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
    st["nav"] = [{"date": "2026-10-26", "nav": 10000.0, "cash": 9595.0,
                  "market_value": 400.0, "drawdown": 0.0, "bench": None},
                 {"date": "2026-10-27", "nav": 8000.0, "cash": 9595.0,
                  "market_value": 300.0, "drawdown": -0.2, "bench": None}]
    _dd = paper.current_drawdown(st)
    check(_dd is not None and _dd <= config.EXEC["portfolio_dd_halt"],
          f"当前回撤 {_dd:.1%} 已破熔断线 {config.EXEC['portfolio_dd_halt']:.0%}")
    check(paper.current_drawdown({"nav": []}) is None,
          "空净值序列的回撤是 None（无法判定），不当成 0")
    ev = paper.stop_orders(st, "2026-10-27", {"sh510300": 3.0})
    check(any(o["action"] == "sell" for o in ev), f"触发熔断并生成清仓单（订单 {[o['code'] for o in ev]}）")
    check(any(o["action"] == "sell" for o in paper.pending_orders(st)), "存在待成交卖单")

    print("=== 7. enforce_cash 砍单 ===")
    st = fresh(10000.0)        # 可用 10000-200=9800
    o1 = buy_etf(st, code="sh510300", shares=2000, price=4.0, eff="2026-10-26")   # 8000
    o2 = buy_etf(st, code="sh510500", shares=2000, price=4.0, eff="2026-10-26")   # 8000
    o1["batch"], o2["batch"] = 1, 2
    dropped = paper.enforce_cash(st, [o1, o2])
    check(len(dropped) == 1 and dropped[0]["code"] == "sh510500", "第 2 批被砍、第 1 批保留")
    check(o2["status"] == "cancelled" and "现金约束" in o2["cancel_reason"], "砍单原因可追溯")

    print("=== 8. target_orders 三批 + 不偏离不交易 ===")
    st = fresh(100000.0)
    targets = [{"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "layer": "ETF",
                "shares": 300, "price": 4.0, "stop_price": 3.4, "target_price": 5.2,
                "thesis": "测试", "falsifiers": [],
                "ladder": [{"tranche": 1, "price": 4.0, "shares": 120},
                           {"tranche": 2, "price": 3.8, "shares": 105},
                           {"tranche": 3, "price": 3.6, "shares": 75}]}]
    orders = paper.target_orders(st, targets, {"sh510300": 4.0}, "2026-10-23",
                                fill_from="2026-10-26")
    check(len(orders) == 3, f"生成 3 张订单（实际 {len(orders)}）")
    check(orders[0]["order_type"] == "buy_limit",
          "第 1 批也是限价条件单（2026-11-18 起买入一律挂单，不吃开盘价）")
    check(abs(orders[0]["limit_price"] - 4.0) < 1e-9 and orders[0].get("expire_on"),
          f"第 1 批限价 = 计划价 4.0 且有失效日（实际 {orders[0]['limit_price']}/{orders[0].get('expire_on')}）")
    check(all(o["order_type"] == "buy_limit" for o in orders[1:]), "第 2/3 批限价条件单")
    check(sum(o["shares"] for o in orders) == 300, "三批股数之和 = 目标 300")
    # 现价高于第 1 批计划价：限价单只会等回落，绝不追开盘价。
    st_hi = fresh(100000.0)
    orders_hi = paper.target_orders(st_hi, targets, {"sh510300": 4.5}, "2026-10-23",
                                    fill_from="2026-10-26")
    check(len(orders_hi) == 3 and orders_hi[0]["order_type"] == "buy_limit",
          "现价 4.5 > 第 1 批计划价 4.0：第 1 批挂限价条件单")
    check(abs(orders_hi[0]["limit_price"] - 4.0) < 1e-9,
          f"第 1 批限价 = 计划价 4.0（实际 {orders_hi[0]['limit_price']}）")
    check("限价" in orders_hi[0]["reason"] and "第 1 批" in orders_hi[0]["reason"],
          "第 1 批限价备注可解释")
    # 现价已在买入区（close <= px）：仍是限价单，开盘价不高于限价即按开盘价成交。
    st_lo = fresh(100000.0)
    orders_lo = paper.target_orders(st_lo, targets, {"sh510300": 3.9}, "2026-10-23",
                                    fill_from="2026-10-26")
    check(orders_lo[0]["order_type"] == "buy_limit"
          and orders_lo[0]["limit_price"] >= 3.9,
          "现价 3.9 <= 计划价 4.0：仍挂限价单（开盘价通常低于限价，照样成交）")
    paper.enforce_cash(st, orders)
    st2 = fresh(100000.0)
    st2["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                   "layer": "ETF", "shares": 300, "cost_basis": 4.0,
                                   "opened_at": "2026-10-26", "last_buy_date": "2026-10-26",
                                   "stop_price": 3.4, "target_price": 5.2}
    o = paper.target_orders(st2, targets, {"sh510300": 4.0}, "2026-10-23",
                            fill_from="2026-10-26")
    check(not o, "已持有 300 股 = 目标 300，不交易")

    print("=== 9. settle 净值/回撤 + stats 胜率盈亏比 ===")
    st = fresh(10000.0)
    paper.settle(st, "2026-10-26", {})
    check(abs(st["nav"][-1]["nav"] - 10000) < 0.01, "空仓净值 = 10000")
    st["trades"] = [
        {"date": "d1", "code": "a", "name": "A", "kind": "etf", "action": "sell", "price": 1.0,
         "shares": 100, "amount": 100, "fee": 5, "realized_pnl": 100.0, "pnl_pct": 0.1, "order_id": "x"},
        {"date": "d2", "code": "b", "name": "B", "kind": "etf", "action": "sell", "price": 1.0,
         "shares": 100, "amount": 100, "fee": 5, "realized_pnl": -50.0, "pnl_pct": -0.05, "order_id": "y"},
    ]
    s = paper.stats(st)
    check(abs(s["win_rate"] - 0.5) < 1e-9, f"胜率 50%（实际 {s['win_rate']}）")
    check(abs(s["profit_factor"] - 2.0) < 1e-9, f"盈亏比 2.0（实际 {s['profit_factor']}）")
    check(s["closed_trades"] == 2, "已平仓 2 笔")

    print("=== 10. 挂单去重 / 撤挂单 / 跌破止损撤 GTC ===")
    st = fresh(100000.0)
    t300 = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "layer": "ETF",
            "shares": 300, "price": 4.0, "stop_price": 3.4, "target_price": 5.2,
            "ladder": [{"tranche": 1, "price": 4.0, "shares": 120},
                       {"tranche": 2, "price": 3.8, "shares": 105},
                       {"tranche": 3, "price": 3.6, "shares": 75}]}
    first = paper.target_orders(st, [t300], {"sh510300": 4.0}, "2026-10-23", fill_from="2026-10-26")
    check(len(first) == 3, f"首期按三批挂出 3 张买单（实际 {len(first)}）")
    first[0]["status"] = "filled"      # 第 1 批限价单成交 120 股，剩 180 股还挂着
    st["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                  "layer": "ETF", "shares": 120, "cost_basis": 4.0,
                                  "opened_at": "2026-10-26", "last_buy_date": "2026-10-26",
                                  "stop_price": 3.4, "target_price": 5.2}
    again = paper.target_orders(st, [t300], {"sh510300": 4.0}, "2026-10-30", fill_from="2026-11-02")
    check(not again, f"持仓 120 + 挂单 180 = 目标 300，不再重复下单（实际 {len(again)} 张）")
    paper.target_orders(st, [dict(t300, shares=120)], {"sh510300": 4.0}, "2026-11-06",
                        fill_from="2026-11-09")
    check(not paper.pending_orders(st, "buy"), "目标下调后未成交买单全部撤销")

    st2 = fresh()
    st2["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                   "layer": "ETF", "shares": 200, "cost_basis": 4.2,
                                   "opened_at": "2026-10-23", "last_buy_date": "2026-10-23",
                                   "stop_price": 3.4, "target_price": 5.2}
    paper.stop_orders(st2, "2026-10-23", {"sh510300": 4.0})
    check(any(o["order_type"] == "sell_stop" for o in paper.pending_orders(st2, "sell")),
          "未跌破止损时挂一张 GTC 条件止损单")
    st2["holdings"]["sh510300"]["stop_price"] = 4.5
    paper.stop_orders(st2, "2026-10-30", {"sh510300": 4.0})
    sells = paper.pending_orders(st2, "sell")
    check(any(o["order_type"] == "sell_open" for o in sells), "跌破止损后生成开盘卖出单")
    check(not any(o["order_type"] == "sell_stop" for o in sells), "同时撤掉 GTC，不会重复卖")

    print("=== 11. incremental 模式：不因排名变化卖出，只卖 bad_codes ===")
    hold = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "layer": "ETF",
            "shares": 200, "cost_basis": 4.0, "opened_at": "2026-10-23",
            "last_buy_date": "2026-10-23", "stop_price": 3.4, "target_price": 5.2}
    st = fresh()
    st["holdings"]["sh510300"] = dict(hold)
    t_other = {"code": "sh510500", "name": "中证500ETF", "kind": "etf", "layer": "ETF",
               "shares": 100, "price": 3.0, "stop_price": 2.55, "target_price": 4.0,
               "ladder": [{"tranche": 1, "price": 3.0, "shares": 100}]}
    inc = paper.target_orders(st, [t_other], {"sh510300": 4.0, "sh510500": 3.0}, "2026-10-23",
                              fill_from="2026-10-26", mode="incremental")
    check(not any(o["action"] == "sell" for o in inc),
          "incremental：持仓没进本期目标、也没被判回避 → 不卖")
    check(any(o["code"] == "sh510500" for o in inc), "incremental：新目标照常买入")

    st2 = fresh()
    st2["holdings"]["sh510300"] = dict(hold)
    bad = paper.target_orders(st2, [], {"sh510300": 4.0}, "2026-10-23",
                              fill_from="2026-10-26", mode="incremental",
                              bad_codes=["sh510300"])
    check(any(o["action"] == "sell" and o["shares"] == 200 for o in bad),
          "incremental：被判回避 → 全部撤出 200 股")

    st3 = fresh()
    st3["holdings"]["sh510300"] = dict(hold)
    full = paper.target_orders(st3, [], {"sh510300": 4.0}, "2026-10-23",
                               fill_from="2026-10-26", mode="full")
    check(any(o["action"] == "sell" for o in full), "full 模式仍然清掉不在目标里的持仓")

    print("=== 12. cap_trim：超出仓位上限按最差的先卖 ===")
    st4 = fresh(100000.0)
    st4["holdings"] = {
        "sh510300": dict(hold, code="sh510300", name="A", shares=1000, cost_basis=4.0),
        "sh510500": dict(hold, code="sh510500", name="B", shares=1000, cost_basis=4.0),
    }
    closes = {"sh510300": 4.0, "sh510500": 4.0}   # 各 4000 元，合计 8000
    trim = paper.cap_trim_orders(st4, 5000.0, closes, "2026-10-23", priority={"sh510300": 40.0, "sh510500": 80.0},
                                 fill_from="2026-10-26")
    check(len(trim) == 1, f"超上限 3000 元 → 先卖最差的 1 只（实际 {len(trim)} 笔）")
    check(trim[0]["code"] == "sh510300", "优先卖分数低的那只")
    check(trim[0]["shares"] == 800, f"卖到刚好不超上限（实际 {trim[0]['shares']} 股）")
    no_trim = paper.cap_trim_orders(st4, 9000.0, closes, "2026-10-23", fill_from="2026-10-26")
    check(not no_trim, "未超上限 → 不动")
    no_trim2 = paper.cap_trim_orders(st4, 7800.0, closes, "2026-10-23", fill_from="2026-10-26")
    check(not no_trim2, "超上限但未超容忍带（5% 与 300 元取大）→ 不砍，省一手手续费")

    print("=== 13. once 模式：只建仓一次，之后不再因排名/目标变化买卖 ===")
    st_once = fresh()
    t_first = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "layer": "ETF",
               "shares": 200, "price": 4.0, "stop_price": 3.4, "target_price": 5.2,
               "ladder": [{"tranche": 1, "price": 4.0, "shares": 100},
                          {"tranche": 2, "price": 3.8, "shares": 100}]}
    first = paper.target_orders(st_once, [t_first], {"sh510300": 4.0}, "2026-10-23",
                                fill_from="2026-10-26", mode="once")
    check(len(first) == 2, f"once：首次决策照常建仓（3 批拆单，实际 {len(first)} 张）")
    # 建仓后（挂着买单）再决策：一张新单都不下
    t_new = {"code": "sh510500", "name": "中证500ETF", "kind": "etf", "layer": "ETF",
             "shares": 100, "price": 3.0, "stop_price": 2.55, "target_price": 4.0,
             "ladder": [{"tranche": 1, "price": 3.0, "shares": 100}]}
    again = paper.target_orders(st_once, [t_new], {"sh510300": 4.0, "sh510500": 3.0},
                               "2026-10-30", fill_from="2026-11-02", mode="once")
    check(not again, "once：已建仓 → 不再新买、不因排名变化换仓")
    # 已成交后同样不动，且不撤原挂单
    st_once2 = fresh()
    st_once2["trades"].append({"date": "2026-10-26", "code": "sh510300", "name": "沪深300ETF",
                               "kind": "etf", "action": "buy", "shares": 100, "price": 4.0,
                               "amount": 400.0, "fee": 5.0})
    st_once2["holdings"]["sh510300"] = dict(hold, shares=100, cost_basis=4.05)
    again2 = paper.target_orders(st_once2, [t_new], {"sh510300": 3.9, "sh510500": 3.0},
                                "2026-10-30", fill_from="2026-11-02", mode="once")
    check(not again2, "once：已有成交 → 也不做再平衡")

    print("=== 14. 现金层计息（挂单资金→朝朝宝，观望资金→7/14/28 天期逆回购） ===")
    # 2026-11-15：挂单资金留朝朝宝（T+0）；观望资金按金额分档买
    # 7/14/28 天期逆回购（年费率同为 0.26%），朝朝宝顶流动性。以下期望全部从
    # config.cash_rates() / capital.cash_net_rates() 推导，不写死市场利率。
    st_y = fresh()
    plan_y = paper.cash_yield_plan(st_y, {})
    check(plan_y["deployable"] == 9800.0,
          f"可动用现金 = 现金 − 2% 缓冲（实际 {plan_y['deployable']}）")
    check(plan_y["container"] == "repo7",
          f"默认容器=观望资金逆回购（实际 {plan_y['container']}）")
    check(plan_y["repo_amount"] == 9000.0 and plan_y["repo_lots"] == 9,
          f"观望 9,800 元 → 9 手 9,000 元（实际 {plan_y['repo_amount']} / {plan_y['repo_lots']}）")
    check(plan_y["repo_term_days"] == 14,
          f"9,000 元档 → 14 天期（实际 {plan_y['repo_term_days']}）")
    check(plan_y["money_fund"] == 800.0,
          f"零头 800 元留朝朝宝（实际 {plan_y['money_fund']}）")
    plan_yr = paper.cash_yield_plan(st_y, {}, container="repo")
    check(plan_yr["container"] == "repo" and plan_yr["repo_lots"] == 9
          and plan_yr["repo_amount"] == 9000.0 and plan_yr["repo_term_days"] == 1,
          f"显式 repo（回滚口径）→ 1 天期 9 手（实际 {plan_yr['repo_amount']} / "
          f"{plan_yr['repo_term_days']} 天期）")
    check(plan_yr["money_fund"] == 800.0,
          f"逆回购回滚口径零头 800 元留朝朝宝（实际 {plan_yr['money_fund']}）")
    # 14b) 现金层必须扣「已挂单占用」（含场外在挂申购）——2026-11-08 A 类修复。
    st_res = fresh()
    st_res["orders"].append({"id": "2026-10-23-01", "as_of": "2026-10-23",
                             "code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                             "action": "buy", "shares": 100, "limit_price": 10.0,
                             "ref_price": 10.0, "status": "pending", "batch": 1,
                             "created": "2026-10-23"})
    otc_book.ensure(st_res)
    st_res["otc_orders"].append({"id": "O1", "code": "OTC:012345", "name": "测试债基",
                                 "action": "buy", "amount": 500.0, "status": "pending",
                                 "effective_from": "2026-10-23", "created": "2026-10-23"})
    plan_res = paper.cash_yield_plan(st_res, {}, container="repo")
    check(plan_res["deployable"] == 9800.0 and plan_res["reserved_cash"] == 1500.0,
          f"计息基数 9800 / 挂单+场外占款 1500（实际 {plan_res['deployable']} / "
          f"{plan_res.get('reserved_cash')}）")
    check(plan_res["repo_amount"] == 8000.0,
          f"逆回购只排「扣掉占款后」的可用现金整手 8000（实际 {plan_res['repo_amount']}）")
    check(plan_res["money_fund"] == 1800.0,
          f"朝朝宝基数含在挂占款（9800−8000=1800，实际 {plan_res['money_fund']}）")
    st_lk = fresh()
    st_lk["cash"] = 10000.0
    st_lk["repo_open"] = {"buy": "2026-10-23", "mature": "2026-10-30", "days": 7,
                          "term": 7, "amount": 2000.0, "rate": 0.0125,
                          "fee_rate": 0.00001, "interest": 0.48, "fee": 0.02}
    plan_lk = paper.cash_yield_plan(st_lk, container="money_fund")
    check(plan_lk["repo_locked"] == 2000.0 and plan_lk["money_fund"] == 7800.0,
          f"在途逆回购本金不重复计朝朝宝息（锁定 {plan_lk.get('repo_locked')} / "
          f"朝朝宝 {plan_lk['money_fund']}）")
    # 建仓弹药不进 90 天货基池（周三/周六决策、次一交易日要能买）
    st_am = fresh()
    st_am["cash"] = 10000.0
    st_am["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                     "shares": 500, "last_close": 4.0, "cost_basis": 4.0,
                                     "stop_price": 3.4, "target_price": None,
                                     "layer": "ETF", "level": "R3"}
    st_am["meta"]["equity_cap"] = 8000.0
    p_am = paper.cash_yield_plan(st_am)
    check(p_am["equity_cap"] == 8000.0 and p_am["ammo"] == 6000.0,
          f"建仓弹药 = 权益上限 8000 − 现有权益 2000 = 6000（实际 {p_am['ammo']}）")
    check(p_am["idle_base"] == 3800.0,
          f"观望资金扣掉弹药：9800 − 6000 = 3800（实际 {p_am['idle_base']}）")
    orig_pool = paper._pool_plan
    paper._pool_plan = lambda amt, allow_stale=False: {"amount": amt,
                                    "holdings": [{"code": "000000", "name": "unit", "amount": amt,
                                                  "rate": 0.013}],
                                    "weighted_rate": 0.013,
                                    "residual": 0.0, "fallback": False, "stale": False,
                                    "data_date": "", "as_of": "", "per_fund_cap": 10000.0,
                                    "top_n": 3, "note": "unit-test"}
    try:
        p_am_pool = paper.cash_yield_plan(st_am, container="pool")
    finally:
        paper._pool_plan = orig_pool
    check(p_am_pool["sleeve_amount"] == 3800.0 and p_am_pool["money_fund"] == 6000.0,
          f"弹药留朝朝宝、不入货基池（池 {p_am_pool['sleeve_amount']} / 朝朝宝 {p_am_pool['money_fund']}）")
    p_am2 = paper.cash_yield_plan(st_am, cap_amount=10000.0)
    check(p_am2["ammo"] == 8000.0 and p_am2["idle_base"] == 1800.0,
          f"显式 cap_amount 优先于 meta（弹药 {p_am2['ammo']} / 观望 {p_am2['idle_base']}）")
    st_am3 = fresh()
    st_am3["cash"] = 10000.0
    p_am3 = paper.cash_yield_plan(st_am3)
    check(p_am3["ammo"] == 0.0 and p_am3["idle_base"] == 9800.0,
          f"旧账本无 equity_cap → 不扣弹药（观望 {p_am3['idle_base']}）")
    # 场内挂单的钱本身就是弹药的一部分，不能扣两遍（2026-11-19后修正）
    st_am4 = fresh()
    st_am4["cash"] = 10000.0
    st_am4["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                      "shares": 500, "last_close": 4.0, "cost_basis": 4.0,
                                      "stop_price": 3.4, "target_price": None,
                                      "layer": "ETF", "level": "R3"}
    st_am4["meta"]["equity_cap"] = 8000.0            # 弹药 6000
    buy_etf(st_am4, shares=1000, price=3.0)          # 场内挂单占 3000（已含在弹药里）
    p_am4 = paper.cash_yield_plan(st_am4)
    check(p_am4["t0_keep"] == 6000.0 and p_am4["inhouse_reserved"] == 3000.0,
          f"T+0 保留 = max(弹药 6000, 挂单 3000) = 6000（实际 {p_am4['t0_keep']}）")
    check(p_am4["idle_base"] == 3800.0,
          f"挂单部分不重复扣：9800 − 6000 = 3800（实际 {p_am4['idle_base']}）")
    st_am5 = fresh()
    st_am5["cash"] = 10000.0
    st_am5["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                      "shares": 500, "last_close": 4.0, "cost_basis": 4.0,
                                      "stop_price": 3.4, "target_price": None,
                                      "layer": "ETF", "level": "R3"}
    st_am5["meta"]["equity_cap"] = 4000.0            # 弹药 2000 < 挂单 3000
    buy_etf(st_am5, shares=1000, price=3.0)
    p_am5 = paper.cash_yield_plan(st_am5)
    check(p_am5["t0_keep"] == 3000.0 and p_am5["idle_base"] == 6800.0,
          f"挂单超过弹药时按挂单留（保留 {p_am5['t0_keep']} / 观望 {p_am5['idle_base']}）")
    st_cap = fresh()
    check(paper.repo_cap(st_cap) == 9000.0,
          f"在途逆回购本金上限 = 可用现金整手 9000（实际 {paper.repo_cap(st_cap)}）")
    st_cl = fresh()
    st_cl["nav"] = [{"date": "2026-10-20", "nav": 10000.0, "cash": 10000.0,
                     "market_value": 0.0, "positions": 0, "bench": None,
                     "bench_nav": None, "peak_nav": 10000.0, "drawdown": 0.0}]
    st_cl["repo_open"] = {"buy": "2026-10-20", "mature": "2026-11-09", "days": 20,
                          "term": 20, "amount": 20000.0, "interest": 13.0, "fee": 0.8}
    _cal = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
    paper.accrue_cash_lots(st_cl, "2026-10-23", _cal)
    check(st_cl.get("repo_open") and float(st_cl["repo_open"]["amount"]) == 9000.0,
          f"旧规则遗留的在途逆回购超可用现金 → 自动收敛到 9000"
          f"（实际 {st_cl.get('repo_open', {}).get('amount')}）")
    check(any("在途逆回购本金超可用现金" in (e.get("msg") or "")
              for e in (st_cl.get("events") or [])),
          "收敛在途逆回购时写 warn 事件留痕")
    st_y2 = fresh()
    st_y2["nav"] = [{"date": "2026-10-23", "nav": 10000.0, "cash": 10000.0,
                     "market_value": 0.0, "positions": 0, "bench": 4105.33,
                     "bench_nav": 10000.0, "peak_nav": 10000.0, "drawdown": 0.0}]
    plan_now = paper.cash_yield_plan(st_y2)
    expect = round(plan_now["repo_amount"] * plan_now["repo_rate"] * 7 / 365
                   + plan_now["money_fund"] * plan_now["money_fund_rate"] * 7 / 365
                   - plan_now["repo_amount"] * plan_now["repo_fee_annual_rate"] * 7 / 365, 2)
    got = paper.accrue_cash(st_y2, "2026-10-30")   # 7 天
    check(got == expect,
          f"7 天净利息 = {expect} 元（逆回购 {plan_now['repo_amount']:,.0f}@"
          f"{plan_now['repo_rate']:.2%} + 朝朝宝 {plan_now['money_fund']:,.0f}@"
          f"{plan_now['money_fund_rate']:.2%} − 费，实际 {got}）")
    check(st_y2["cash"] == round(10000.0 + expect, 2),
          f"利息记进现金（现金 {st_y2['cash']}）")
    check(any(e["level"] == "cash_yield" for e in st_y2["events"]),
          "计息写 cash_yield 事件（可追溯）")
    _cy2 = [e for e in st_y2["events"] if e.get("level") == "cash_yield"]
    check(bool(_cy2) and str(_cy2[-1].get("msg", "")).startswith("（回滚口径"),
          "回滚口径计息文案显式标注（年化×天数，仅离线/测试）")
    _split = paper.cash_interest_split(st_y2)
    check(_split["booked"] == expect and _split["pending"] == 0.0
          and _split["total"] == expect,
          f"利息双行：已落账 {expect} + 在途 0 = {expect}（实际 {_split['booked']} / "
          f"{_split['pending']} / {_split['total']}）")
    check(paper.accrue_cash(st_y2, "2026-10-30") == 0.0,
          "同一天重复计息不重复（幂等，返回 0）")
    # 挂单占用的钱也照常计息（挂单资金留朝朝宝，T+0）
    st_y3 = fresh()
    st_y3["orders"].append({"id": "y1", "code": "sh510300", "name": "沪深300ETF",
                            "kind": "etf", "action": "buy", "order_type": "buy_open",
                            "shares": 200, "limit_price": None, "ref_price": 4.0,
                            "status": "pending", "effective_from": "2026-10-26",
                            "expire_on": None, "batch": 1, "created": "2026-10-23"})
    plan_y3 = paper.cash_yield_plan(st_y3, {"sh510300": 4.0})
    check(plan_y3["deployable"] == 9800.0,
          f"挂单只占预算、不冻结计息（可动用仍 {plan_y3['deployable']} 元）")
    _y3_idle = round(plan_y3["deployable"] - plan_y3["repo_locked"]
                     - plan_y3["reserved_cash"], 2)
    check(plan_y3["repo_amount"] == round(_y3_idle // 1000 * 1000, 2)
          and plan_y3["money_fund"]
          == round(plan_y3["deployable"] - plan_y3["repo_locked"]
                   - plan_y3["repo_amount"], 2),
          f"挂单占用后观望资金只排整手逆回购（逆回购 {plan_y3['repo_amount']} / "
          f"朝朝宝 {plan_y3['money_fund']}）")
    # 年化对齐：一年净收益必须等于 config.cash_rates() 推出来的拆分结果
    st_y4 = fresh()
    st_y4["nav"] = [{"date": "2026-01-01", "nav": 10000.0, "cash": 10000.0,
                     "market_value": 0.0, "positions": 0, "bench": 4000.0,
                     "bench_nav": 10000.0, "peak_nav": 10000.0, "drawdown": 0.0}]
    plan_y4 = paper.cash_yield_plan(st_y4)
    paper.accrue_cash(st_y4, "2027-01-01")        # 365 天
    y_net = round(st_y4["cash"] - 10000.0, 2)
    y_expect = round(plan_y4["repo_amount"] * plan_y4["repo_rate"]
                     + plan_y4["money_fund"] * plan_y4["money_fund_rate"]
                     - plan_y4["repo_amount"] * plan_y4["repo_fee_annual_rate"], 2)
    check(abs(y_net - y_expect) < 0.03,
          f"一年闲置现金净收益 = {y_expect} 元（{y_expect / 10000.0:.2%}，与实际 {y_net} 元一致），"
          f"口径= config.cash_rates() 的 7 天期逆回购 + 朝朝宝")

    print("=== 16. 账本备份（无人值守下的可回滚） ===")
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        sp = str(_osp.join(_td, "state.json"))
        check(paper.backup_state(sp) == "", "账本文件不存在时备份返回空串、不报错")
        st_b = fresh()
        paper.save(st_b, sp)
        b1 = paper.backup_state(sp, keep=3)
        b2 = paper.backup_state(sp, keep=3)
        b3 = paper.backup_state(sp, keep=3)
        check(bool(b1) and _osp.exists(b1), "备份文件已生成")
        check(_osp.dirname(b1) == _osp.join(_td, "_backup"), "备份放在账本同目录的 _backup/")
        check(len(paper.list_backups(sp)) <= 3, f"只保留最近 keep 份（实际 {len(paper.list_backups(sp))} 份）")
        back = paper.load(b1)
        check(back["meta"]["capital"] == 10000.0, "备份内容可 load 回来、账本结构完整")

        # 备份保留天数（周决策顺便删上一周整批，只留最近 7 天）
        _bkdir = _osp.join(_td, "_backup")
        _now = dt.datetime(2026, 6, 10, 20, 0)

        def _mk(days):
            _ts = (_now - dt.timedelta(days=days)).strftime("%Y%m%d-%H%M%S-%f")
            p = _osp.join(_bkdir, f"state.{_ts}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({}, f)
            return p

        check(config.PAPER.get("backup_keep_days") == 7,
              "config.PAPER['backup_keep_days'] = 7（只留最近 7 天 / 删 8 天前及更早）")
        _d14 = _mk(14)
        _d8 = _mk(8)
        _d7 = _mk(7)
        _d3 = _mk(3)
        pr = paper.prune_backups(sp, now=_now, dry=True)
        check(sorted(pr["expired"]) == sorted([_d14, _d8]) and _osp.exists(_d14) and _osp.exists(_d8),
              "dry=True 只算不删：8 天前及更早的 2 份被点名、都还在磁盘上")
        pr = paper.prune_backups(sp, now=_now)
        check(not _osp.exists(_d14) and not _osp.exists(_d8),
              "周决策备份轮转：删掉 8 天前及更早的上一周整批旧备份")
        check(_osp.exists(_d7) and _osp.exists(_d3),
              "最近 7 天的备份保留（7 天前那份恰在边界上、不删）")
        check(len(paper.list_backups(sp)) == 5 and pr["kept"] == 5,
              f"保留 3 份原有 + 2 份近期（实际 {len(paper.list_backups(sp))} 份）")

        # 最新一份永不删：唯一一份即使已过期也留着（回滚底线）
        _td2 = _osp.join(_td, "only")
        os.makedirs(_osp.join(_td2, "_backup"))
        _sp2 = _osp.join(_td2, "state.json")
        paper.save(fresh(), _sp2)
        _only = _osp.join(_td2, "_backup", "state.20240101-000000-000000.json")
        with open(_only, "w", encoding="utf-8") as f:
            json.dump({}, f)
        pr2 = paper.prune_backups(_sp2)
        check(_osp.exists(_only) and pr2["kept"] == 1,
              "最新一份备份永不删（唯一一份即使过期也留着，回滚能力不归零）")
        check(paper.prune_backups(_osp.join(_td2, "no_such_ledger.json"))["kept"] == 0,
              "没有备份时 prune_backups 返回 0 份、不报错")
        # 份数上限仍生效：先按天数删、再按份数删（35 份近期 + 1 份旧 → 留 30）
        _base = dt.datetime.now()
        for _i in range(35):
            _ts = (_base - dt.timedelta(minutes=_i + 1)).strftime("%Y%m%d-%H%M%S-%f")
            with open(_osp.join(_td2, "_backup", f"state.{_ts}.json"), "w", encoding="utf-8") as f:
                json.dump({}, f)
        pr3 = paper.prune_backups(_sp2, keep=30)
        check(len(paper.list_backups(_sp2)) == 30 and pr3["kept"] == 30,
              f"备份份数上限仍生效：{len(paper.list_backups(_sp2))} 份（上限 30）")
        check(not os.path.exists(_only) and len(pr3["deleted"]) == 6,
              "旧备份先按保留天数删、再按份数删（共删 6 份：1 过期 + 5 超量）")

    print("=== 17. 可投资本（本金 + 已实现盈利，上限见 config.CAPITAL） ===")
    st_c = fresh()
    inv0 = paper.investable_capital(st_c)
    check(inv0["investable"] == 10000.0 and inv0["gain"] == 0.0,
          "没有成交时：可投资本 = 本金 10000")
    st_c["trades"] = [
        {"action": "buy", "realized_pnl": None, "fee": 5.0},
        {"action": "sell", "realized_pnl": 1200.0, "fee": 5.0},
    ]
    inv1 = paper.investable_capital(st_c)
    check(paper.realized_pnl_total(st_c) == 1200.0, "已实现盈亏只汇总卖出流水（1200）")
    check(inv1["investable"] == 11200.0 and inv1["growth_pct"] > 0,
          f"盈利 1200 → 可投资本 11200（实际 {inv1['investable']}）")
    st_c["trades"].append({"action": "sell", "realized_pnl": -1800.0, "fee": 5.0})
    inv2 = paper.investable_capital(st_c)
    check(inv2["gain"] == 0.0 and inv2["investable"] == 10000.0,
          "净亏损时不缩本金：可投资本仍按本金 10000（不因亏损被动减仓）")
    st_big = fresh(100000.0)
    st_big["trades"] = [{"action": "sell", "realized_pnl": 25000.0, "fee": 5.0}]
    inv3 = paper.investable_capital(st_big)
    check(inv3["investable"] == 100000.0 and inv3["clamped"] is True,
          "超过技能上限：可投资本封顶，超出部分留现金层")
    st_half = fresh(10000.0)
    st_half["meta"]["capital"] = 10000.0
    check(paper.investable_capital(st_half)["clamped"] is False, "未到上限时 clamped=False")
    # 现金层利息也是「赚到的钱」，必须进复利口径（它不产生卖出流水，早前被漏掉）
    st_int = fresh()
    st_int["repo_history"] = [{"kind": "repo", "interest": 60.0, "fee": 6.0},
                              {"kind": "mf", "interest": 10.0, "fee": 0.0}]
    check(paper.cash_interest_total(st_int) == 64.0,
          f"现金层净利息 = 60-6+10 = 64（实际 {paper.cash_interest_total(st_int)}）")
    check(paper.investable_capital(st_int)["investable"] == 10064.0,
          "现金层利息 64 元进可投资本（10000 → 10064）")

    print("=== 18. 交易日/整手工具 ===")
    cal = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]
    check(paper._add_trading_days("2026-10-23", 1, cal) == "2026-10-26", "10-23 后 1 个交易日 = 10-26")
    check(paper._add_trading_days("2026-10-23", 2, cal) == "2026-10-27", "10-23 后 2 个交易日 = 10-27")
    check(paper._shares_for_amount(1000, 4.0) == 200, "1000 元 / 4.0 元 = 200 股（整手）")
    check(paper._shares_for_amount(1000, 4.3) == 200, "1000 元 / 4.3 元 = 200 股（向下取整）")

    print("=== 19. 账本回滚（restore_state，回滚本身可逆）===")
    tmp = tempfile.mkdtemp(prefix="paper_rb_")
    try:
        rp = os.path.join(tmp, "state.json")
        st_r = fresh(10000.0)
        paper.save(st_r, rp)
        good = paper.backup_state(rp)                  # 备份 A：好账本
        st_r["cash"] = 1234.0                          # 手改成坏账本
        paper.save(st_r, rp)
        bad = paper.backup_state(rp)                   # 备份 B：坏账本
        check(good != bad and len(paper.list_backups(rp)) == 2,
              "同一秒内两次备份不互相覆盖（文件名带微秒）")
        res = paper.restore_state(rp, index=-2)         # 回到 A
        check(paper.load(rp)["cash"] == 10000.0,
              f"回滚到好账本：现金恢复 10000（实际 {paper.load(rp)['cash']}）")
        check(bool(res["previous_backup"]) and os.path.exists(res["previous_backup"]),
              "回滚前先把当前账本再备份一份（回滚可逆）")
        check(len(paper.list_backups(rp)) == 3,
              f"回滚会产生一份新备份（3 份，实际 {len(paper.list_backups(rp))}）")
        brk = os.path.join(tmp, "_backup", "state.20990101-000000-000000.json")
        with open(brk, "w", encoding="utf-8") as f:
            json.dump({"cash": 1.0}, f)
        try:
            paper.restore_state(rp, backup_path=brk)
            check(False, "缺必需键的备份应被拒绝")
        except paper.LedgerError:
            check(True, "缺必需键的备份被拒绝（不拿坏文件覆盖好账本）")
        check(paper.load(rp)["cash"] == 10000.0, "拒绝坏备份后账本保持原样")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 20. 决策日收盘价成交
    print("\n=== 20. 决策日收盘价成交（execute_at_close） ===")
    st = paper.new_state(10000.0, "B", "2026-01-05", 4000.0)
    st["cash"] = 3000.0
    closes = {"sh111": 3.88, "sh222": 1.485}
    o1 = paper.make_order(st, "2026-01-05", "sh111", "甲", "etf", "buy", 200,
                          "buy_open", "测试买开")
    o2 = paper.make_order(st, "2026-01-05", "sh111", "甲", "etf", "buy", 100,
                          "buy_limit", "梯子第二批", limit_price=3.20)
    ev = paper.execute_at_close(st, closes, "2026-01-05", [o1, o2])
    check(len(ev) == 2 and all(k == "filled" for k, _ in ev), "两批梯子都按决策日收盘价成交")
    check(abs(st["holdings"]["sh111"]["shares"] - 300) < 1e-9, "持仓 = 目标股数（不等更低价）")
    import capital as _cap2
    _f1 = _cap2.trade_fee(200 * 3.88, is_etf=True)["total"]
    _f2 = _cap2.trade_fee(100 * 3.88, is_etf=True)["total"]
    check(abs(st["holdings"]["sh111"]["cost_basis"] - (300 * 3.88 + _f1 + _f2) / 300) < 1e-6,
          "成本含两笔佣金")
    check(o1["status"] == "filled" and o2["status"] == "filled", "订单状态都是 filled")
    st2 = paper.new_state(10000.0, "B", "2026-01-05", 4000.0)
    st2["cash"] = 200.0
    o3 = paper.make_order(st2, "2026-01-05", "sh111", "甲", "etf", "buy", 100,
                          "buy_open", "买不起")
    ev2 = paper.execute_at_close(st2, closes, "2026-01-05", [o3])
    check(ev2 and ev2[0][0] == "cancelled" and o3["status"] == "cancelled", "现金不足 → cancelled")
    check(abs(st2["cash"] - 200.0) < 1e-9, "cancelled 不动现金")
    o4 = paper.make_order(st2, "2026-01-05", "sh999", "无价", "etf", "buy", 100,
                          "buy_open", "无收盘价")
    ev3 = paper.execute_at_close(st2, closes, "2026-01-05", [o4])
    check(not ev3 and o4["status"] == "pending", "无收盘价的单保持 pending（不瞎填价）")
    st3 = paper.new_state(10000.0, "B", "2026-01-05", 4000.0)
    st3["cash"] = 3000.0
    b = paper.make_order(st3, "2026-01-05", "sh111", "甲", "etf", "buy", 100, "buy_open", "先买")
    paper.execute_at_close(st3, closes, "2026-01-05", [b])
    s = paper.make_order(st3, "2026-01-06", "sh111", "甲", "etf", "sell", 100, "sell_open", "卖")
    ev4 = paper.execute_at_close(st3, {"sh111": 4.0}, "2026-01-06", [s])
    check(ev4 and s["status"] == "filled" and not st3["holdings"], "卖出按收盘价清仓")
    tr = st3["trades"][-1]
    check(tr["realized_pnl"] > 0, "卖出有已实现盈亏")

    print("\n=== 21. 逆回购 lot 级账本（每周一 7 天期 / 在途 / 佣金；2026-11-15 新口径） ===")
    # 2026-11-15：观望资金每周一买 7 天期逆回购（R-007），不再是周一+周四。
    CAL = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
           "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    sch = paper.repo_lot_schedule(CAL, "2026-10-15", "2026-10-23")
    check(len(sch) == 1, f"每周只有周一一个操作日，实际 {len(sch)} 笔")
    check(sch[0]["buy"] == "2026-10-19" and sch[0]["term"] == 7
          and sch[0]["mature"] == "2026-10-26",
          "周一买 7 天期（R-007），次一交易日到期")
    check(not [x for x in sch if x["buy"] != "2026-10-19"], "非周一不产生 lot")
    sch2 = paper.repo_lot_schedule(CAL, "2026-10-15", "2026-10-23", term_days=28)
    check(bool(sch2) and sch2[0]["term"] == 28, "term_days=28 时按金额分档锁 28 天期")

    st = paper.new_state(10000.0, "B", "2026-10-16", 4000.0)
    st["cash"] = 10000.0
    # 挂单占用 2,000 元（挂单资金留朝朝宝）→ 观望资金 7,800 元 → 7 天期档
    st["orders"].append({"id": "r1", "code": "sh510300", "name": "沪深300ETF",
                          "kind": "etf", "action": "buy", "order_type": "buy_open",
                          "shares": 200, "limit_price": None, "ref_price": 10.0,
                          "status": "pending", "effective_from": "2026-10-16",
                          "expire_on": None, "batch": 1, "created": "2026-10-16"})
    _p0 = paper.cash_yield_plan(st)
    check(_p0["repo_term_days"] == 7,
          f"7,800 元观望资金 → 7 天期（实际 {_p0['repo_term_days']}）")
    w1 = paper.accrue_cash_lots(st, "2026-10-23", CAL)
    check(st.get("repo_open") and st["repo_open"]["buy"] == "2026-10-19",
          "窗口最后一笔记为在途（周一买、次一交易日到期）")
    check(not [h for h in st.get("repo_history", []) if h.get("mature") == "2026-10-26"],
          "在途 lot 还没进已实现历史（不重复计息）")
    paper.accrue_cash_lots(st, "2026-10-24", CAL)
    check(st.get("repo_open") and st["repo_open"]["buy"] == "2026-10-19",
          "窗口内没有操作日时，在途 lot 原样保留（不被覆盖成 None）")
    w2 = paper.accrue_cash_lots(st, "2026-10-28", CAL)
    check(w2 > 0 and any(h.get("date") == "2026-10-19" and h.get("days") == 7
                         for h in st["repo_history"]),
          "下次结算时在途 lot 兑现为已实现（7 天）")
    check(paper.accrue_cash_lots(st, "2026-10-28", CAL) == 0.0, "同日重复计息返回 0（幂等）")

    st_clean = paper.new_state(10000.0, "B", "2026-10-16", 4000.0)
    st_clean["cash"] = 10000.0
    plan = paper.cash_yield_plan(st_clean)
    check(plan["repo_amount"] == 9000.0 and 0.0 < plan["money_fund"] < 1000.0,
          "观望资金 → 逆回购 9 手 9000 + 零头留朝朝宝")
    check(plan["repo_term_days"] == 14,
          f"9,000 元档 → 14 天期（实际 {plan['repo_term_days']}）")
    rp = paper.repo_pnl(st)
    check(rp["ops"] >= 1 and rp["avg_amount"] > 0, "repo_pnl 汇总笔数与平均投入")
    _cr = config.cash_rates()
    check(0 < rp["effective_annualized"] < _cr["repo7"],
          f"实得年化落在 0~7 天期假设 {_cr['repo7']:.2%} 之间"
          f"（实际 {rp['effective_annualized']:.2%}，已扣手续费）")
    check(rp["fees"] > 0 and 0 < rp["fee_drag"] < 0.3,
          f"手续费侵蚀被单独报出（{rp['fee_drag']:.1%}）")

    # ---- 11. regime_change：只在市场状态变化时重建（2026-10-26 用户验收口径）
    st = paper.new_state(100000, "B", "2026-10-01", 4000.0)
    check(paper.rebuild_due(st, "防守", "regime_change") is True,
          "regime_change：首次（无 last_regime）应重建")
    st["meta"]["last_regime"] = "防守"
    check(paper.rebuild_due(st, "防守", "regime_change") is False,
          "regime_change：市场状态未变化 → 本期不重建")
    check(paper.rebuild_due(st, "进攻", "regime_change") is True,
          "regime_change：市场状态变化（防守→进攻）→ 重建")
    st["meta"]["last_rebuild_capital"] = 10000.0
    check(paper.rebuild_due(st, "防守", "regime_change", investable=10200.0) is True,
          "状态没变但可投资本涨了 2%（≥1%）→ 也要重建，赚到的钱不能一直躺着")
    check(paper.rebuild_due(st, "防守", "regime_change", investable=10050.0) is False,
          "可投资本只涨 0.5%（<1%）→ 不重建，避免为小钱反复交易")
    check(paper.rebuild_due(st, "进攻", "incremental") is True,
          "非 regime_change 模式恒为 True（不影响原有模式）")

    # ---- 12. 验收口径（2026-11-07 改：净利润 = 账户累计收益 − token 实花，不再按年计提）
    import review
    _old_perf = dict(review.PERF)
    # 基准夹具：显式给 1.5%，验证「净年化 > 基准」的比较方向——与线上 config 值无关。
    # （fail loud 校验仍然生效：这里给的是合法数字，不是兜底路径）
    review.PERF["deposit_rate"] = 0.015
    _st = paper.new_state(10000, "B", "2026-01-01", 4000.0)
    _win = {"base": "2026-01-01", "last": "2027-01-01"}   # 365 天 = 1 年整
    _net = review.net_metrics(_st, {"total_return": 0.03}, _win, {"capital": 10000.0},
                              token_cost=100.0)
    check(abs(_net["token_spent"] - 100.0) < 1e-6,
          "token 实花 = 显式覆盖的 100 元（累计 spent）")
    check(abs(_net["net_pnl"] - 200.0) < 1e-6, "净利润 = 300 − 100 = 200 元（先赚回 token）")
    check("覆盖" in (_net["token_cost_basis"] or ""), "覆盖时 token_cost_basis 写明来源")
    # 1 年口径下净年化 = 净利润/本金（300−100=200 → 2%）；用 net_pnl 表达，不写死百分数
    check(abs(_net["net_annualized"] - _net["net_pnl"] / 10000.0) < 0.002,
          f"净年化 = 净利润/本金（{_net['net_annualized']:.2%}）")
    check(_net["bar_ok"] is True, "净年化 > 基准夹具 1.5% → 达标")
    _win_half = {"base": "2026-01-01", "last": "2026-07-01"}   # 181 天
    _net_h = review.net_metrics(_st, {"total_return": 0.03}, _win_half, {"capital": 10000.0},
                                token_cost=100.0)
    check(abs(_net_h["token_spent"] - 100.0) < 1e-6,
          "实花不随统计窗口缩短而变小（不再按年计提）")
    # 默认路径从 budget.json 取累计 spent（这里打桩，避免读真实产物）
    _old_budget = review.billing.budget_state
    review.billing.budget_state = lambda *a, **k: {"spent_cny": 42.0}
    try:
        _net_b = review.net_metrics(_st, {"total_return": 0.03}, _win, {"capital": 10000.0})
        check(abs(_net_b["token_spent"] - 42.0) < 1e-6,
              "默认 token 实花取 budget.json 的 spent_cny")
        check(abs(_net_b["net_pnl"] - 258.0) < 1e-6, "默认净利润 = 300 − 42")
    finally:
        review.billing.budget_state = _old_budget
    _net2 = review.net_metrics(_st, {"total_return": 0.005}, _win, {"capital": 10000.0},
                               token_cost=100.0)
    check(_net2["net_pnl"] < 0 and _net2["bar_ok"] is False, "收益没赚回 token → 不达标")
    _v, _why = review.judge({}, {"weeks": 13}, 0.0, -0.05, 0.01, 12, 10.0, net=_net2)
    check(_v == "未达标" and any("token" in x for x in _why),
          "judge：样本足够但 token 未回本 → 未达标")
    review.PERF.clear()
    review.PERF.update(_old_perf)

    # ---- 22. 括号止损（bracket）+ 月换手上限（2026-11-07 B 类 #10 / #3）
    print("\n=== 22. 括号止损：下单即挂、成交才激活、取消/过期级联、旧账本兼容 ===")
    st = fresh()
    p = buy_etf(st, shares=200, price=4.0, eff="2026-10-26")
    p["stop_price"] = 3.5
    br = paper.bracket_order(st, p)
    check(br is not None and br["bracket"] is True and br["action"] == "sell"
          and br["shares"] == 200 and br["parent_order_id"] == p["id"],
          "买入单同步生成 bracket（sell_stop、同股数、挂父单 id）")
    check(br["active"] is False and br["effective_from"] == p["effective_from"]
          and abs(float(br["limit_price"]) - 3.5) < 1e-9,
          "bracket 初始冻结、生效日与止损价同父单")
    check(br not in paper.pending_orders(st)
          and len(paper.pending_orders(st, "buy")) == 1,
          "冻结的 bracket 不算有效挂单（不占现金、不参与撮合）")
    check(paper.bracket_order(st, p) is None, "同一父单不重复生成 bracket")
    # 买入未成交 → bracket 始终不生效：最低价跌破止损也不许卖
    st2 = fresh()
    p2 = buy_etf(st2, shares=200, typ="buy_limit", limit=3.40, eff="2026-10-27")
    p2["stop_price"] = 3.50
    paper.bracket_order(st2, p2)
    _bars = {"sh510300": [bar("2026-10-27", 3.60, 3.65, 3.45, 3.50)]}
    _ev = paper.resolve_orders(st2, "2026-10-27", _bars)
    _br2 = [o for o in st2["orders"] if o.get("bracket")][0]
    check(not _ev and _br2["status"] == "pending" and _br2["active"] is False,
          "买入条件单未触发 → bracket 冻结，跌破止损也不卖")
    # 买入成交 → 激活；静态止损按「成交成本 ×0.85」重设，下一交易日跌破新止损 → 按止损价卖出
    st3 = fresh()
    p3 = buy_etf(st3, shares=200, price=4.0, eff="2026-10-26")
    p3["stop_price"] = 3.50
    paper.bracket_order(st3, p3)
    _b3 = {"sh510300": [bar("2026-10-26", 4.0, 4.1, 3.9, 4.0),
                        bar("2026-10-27", 3.8, 3.85, 3.40, 3.6)]}
    paper.resolve_orders(st3, "2026-10-26", _b3)
    _h3 = st3["holdings"]["sh510300"]
    _cost3 = round(float(_h3["cost_basis"]), 3)
    _exp_stop = round(_cost3 * 0.85, 3)
    check(abs(float(_h3["stop_price"]) - _exp_stop) < 1e-9,
          f"新开仓止损 = 成交成本 {_cost3} ×0.85 = {_exp_stop}"
          f"（不是计划价 3.50；实际 {_h3['stop_price']}）")
    _br3 = [o for o in st3["orders"] if o.get("bracket")][0]
    check(_br3["active"] is True and _br3["effective_from"] == "2026-10-26",
          "买入成交后 bracket 激活（生效日 = 成交日）")
    check(abs(float(_br3["limit_price"]) - _exp_stop) < 1e-9,
          f"括号单限价重锚到持仓止损 {_exp_stop}（实际 {_br3['limit_price']}）")
    paper.resolve_orders(st3, "2026-10-27", _b3)
    _sell = [t for t in st3["trades"] if t["action"] == "sell"]
    check(len(_sell) == 1 and abs(_sell[0]["price"] - _exp_stop) < 1e-9,
          f"跌破新止损 → 按 {_exp_stop} 卖出（实际 {_sell[0]['price'] if _sell else None}）")
    check("sh510300" not in st3["holdings"], "bracket 成交后持仓清空")
    # 加仓不改既有止损（2026-11-18 规则变更只对新开仓/重建生效，不动已有持仓决策）
    st3b = fresh()
    p3b = buy_etf(st3b, shares=200, price=4.0, eff="2026-10-26")
    p3b["stop_price"] = 3.50
    paper.bracket_order(st3b, p3b)
    paper.resolve_orders(st3b, "2026-10-26", {"sh510300": [bar("2026-10-26", 4.0, 4.1, 3.9, 4.0)]})
    _stop_before = float(st3b["holdings"]["sh510300"]["stop_price"])
    st3b["holdings"]["sh510300"]["stop_price"] = 3.90   # 模拟已被移动止损抬过
    _add = paper.make_order(st3b, "2026-10-26", "sh510300", "沪深300ETF", "etf", "buy",
                            100, "buy_open", "加仓", effective_from="2026-10-27",
                            stop_price=3.50)
    paper.bracket_order(st3b, _add)
    paper.resolve_orders(st3b, "2026-10-27",
                         {"sh510300": [bar("2026-10-27", 4.0, 4.05, 3.95, 4.0)]})
    check(abs(float(st3b["holdings"]["sh510300"]["stop_price"]) - 3.90) < 1e-9,
          f"加仓不改既有止损（3.90 保持；成交前 {_stop_before}）")
    _br_add = [o for o in st3b["orders"] if o.get("bracket") and o["parent_order_id"] == _add["id"]]
    check(not _br_add or abs(float(_br_add[0]["limit_price"]) - 3.50) < 1e-9,
          "加仓的括号单沿用计划价、不按成本重锚")
    # 买入撤单 → bracket 同步作废
    st4 = fresh()
    p4 = buy_etf(st4, shares=200, typ="buy_limit", limit=3.8, eff="2026-10-27")
    p4["stop_price"] = 3.5
    _b4 = paper.bracket_order(st4, p4)
    paper.cancel_order(st4, p4, "测试撤单")
    check(_b4["status"] == "cancelled", "买入单取消 → bracket 同步取消")
    # 买入条件单到期 → bracket 同步作废（取消或过期都算）
    st5 = fresh()
    p5 = buy_etf(st5, shares=200, typ="buy_limit", limit=3.40, eff="2026-10-27")
    p5["stop_price"] = 3.50
    p5["expire_on"] = "2026-10-28"
    _b5 = paper.bracket_order(st5, p5)
    paper.resolve_orders(st5, "2026-10-29",
                         {"sh510300": [bar("2026-10-29", 3.60, 3.65, 3.55, 3.60)]})
    check(p5["status"] == "expired" and _b5["status"] in ("cancelled", "expired"),
          f"买入单到期 → bracket 同步失效（{_b5['status']}）")
    # 旧账本兼容：没有 bracket/active/parent_order_id 字段的订单照常撮合与结算
    st6 = fresh()
    _old = paper.make_order(st6, "2026-10-23", "sh510300", "沪深300ETF", "etf", "buy",
                            200, "buy_open", "旧账本订单", effective_from="2026-10-26")
    for _k in ("bracket", "active", "parent_order_id"):
        _old.pop(_k, None)
    check(paper.pending_orders(st6) == [_old], "旧订单缺新字段仍算有效挂单")
    paper.resolve_orders(st6, "2026-10-26",
                         {"sh510300": [bar("2026-10-26", 4.0, 4.1, 3.9, 4.0)]})
    check(_old["status"] == "filled" and "sh510300" in st6["holdings"],
          "旧账本订单照常成交（缺字段按 False）")
    paper.settle(st6, "2026-10-26", {"sh510300": 4.0})
    check(st6["nav"] and st6["nav"][-1]["nav"] > 0, "旧账本可正常结算净值")

    print("\n=== 23. 月换手上限：只限 incremental 新增买入，不限卖出/首次建仓 ===")
    st = fresh()
    p = buy_etf(st, shares=200, price=4.0, eff="2026-10-26")
    paper.resolve_orders(st, "2026-10-26",
                         {"sh510300": [bar("2026-10-26", 4.0, 4.1, 3.9, 4.0)]})
    check(paper.month_trade_count(st, "2026-10-27") == 1,
          "month_trade_count 数到本月 1 笔买入")
    _tg = [{"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "shares": 400,
            "price": 4.0, "layer": "ETF", "level": "R3", "stop_price": 3.5}]
    _cm = {"sh510300": 4.0}
    _blocked = paper.target_orders(st, _tg, _cm, "2026-10-27",
                                   mode="incremental", month_trades=2)
    check(not [o for o in _blocked if o["action"] == "buy"],
          "本月已 2 笔（上限 2）→ 不再下新买单")
    check(any("换手已达上限" in e["msg"] for e in st["events"]),
          "超限时写 warn 事件（可追溯为什么没买）")
    _ok = paper.target_orders(st, _tg, _cm, "2026-10-27",
                              mode="incremental", month_trades=0)
    _buys = [o for o in _ok if o["action"] == "buy"]
    check(len(_buys) == 1, "未到上限 → 正常下 1 张加仓单")
    check(_buys[0]["order_type"] == "buy_limit"
          and abs(float(_buys[0]["limit_price"]) - 4.0) < 1e-9 and _buys[0].get("expire_on"),
          "加仓单也是限价单（限价 = 决策日收盘价 4.0，带失效日）")
    _brk = [o for o in st["orders"]
            if o.get("bracket") and o.get("parent_order_id") == _buys[0]["id"]]
    check(len(_brk) == 1 and _brk[0]["active"] is False,
          "新买入单同样同步挂冻结的 bracket")
    # 首次建仓（账本无持仓）不受月上限约束
    st_new = fresh()
    _first = paper.target_orders(st_new, _tg, _cm, "2026-10-27",
                                 mode="incremental", month_trades=2)
    check(any(o["action"] == "buy" for o in _first),
          "首次建仓不受「每月 2 次」限制")

    # ---- 24. 状态未变时的「补建仓」（闲置现金太多要投出去）
    print("\n=== 24. 补建仓：只买不卖、累计不超权益上限、接近上限不折腾 ===")
    st = fresh(50000.0)
    st["cash"] = 50000.0
    st["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                  "shares": 200, "cost_basis": 4.0, "last_close": 4.0,
                                  "layer": "ETF", "level": "R3"}
    check(abs(paper.equity_value(st, {"sh510300": 4.0}) - 800.0) < 1e-9,
          "equity_value = 持仓市值（200 股 × 4.0 = 800 元）")
    _tg = [{"code": "sh510300", "name": "沪深300ETF", "kind": "etf", "shares": 1000,
            "price": 4.0, "layer": "ETF", "level": "R3"},
           {"code": "600000", "name": "浦发银行", "kind": "stock", "shares": 200,
            "price": 10.0, "layer": "个股", "level": "R3"}]
    _cm = {"sh510300": 4.0, "600000": 10.0}
    _keep = paper.cap_topup_orders(st, _tg, _cm, 17500.0, "2026-10-23",
                                   fill_from="2026-10-24")
    _buys = [o for o in _keep if o["action"] == "buy"]
    check(len(_buys) == 2 and not [o for o in _keep if o["action"] == "sell"],
          "剩余空间充足 → 补 2 张买单、一张卖单都没有（只买不卖）")
    _total = sum(float(o.get("ref_price") or o.get("limit_price") or 0) * o["shares"]
                 for o in _buys)
    check(_total <= (17500.0 - 800.0) * 0.98 + 1e-6,
          f"累计买入 {_total:.0f} 元 ≤ 剩余空间×98%（不突破该状态仓位上限）")
    # 剩余空间不足 1,000 元 → 不折腾（返回空，且不产生任何新单）
    st2 = fresh(50000.0)
    st2["holdings"]["sh510300"] = dict(st["holdings"]["sh510300"])
    _n0 = len(st2["orders"])
    check(paper.cap_topup_orders(st2, _tg, _cm, 1300.0, "2026-10-23") == [],
          "剩余空间 500 元 < 1,000 元下限 → 不补建仓")
    check(len(st2["orders"]) == _n0, "不折腾时不会留下候选订单")
    # 只保留买单：阶段二判为回避的持仓卖单会被撤销，不许「一边补仓一边卖旧」
    st3 = fresh(50000.0)
    st3["holdings"]["sh510300"] = {"code": "sh510300", "name": "沪深300ETF", "kind": "etf",
                                   "shares": 200, "cost_basis": 4.0, "last_close": 4.0,
                                   "layer": "ETF", "level": "R3"}
    _keep3 = paper.cap_topup_orders(st3, _tg, _cm, 17500.0, "2026-10-23",
                                    fill_from="2026-10-24", bad_codes=["sh510300"])
    check(all(o["action"] == "buy" for o in _keep3) and
          any(o["status"] == "cancelled" and o["action"] == "sell" for o in st3["orders"]),
          "被判定回避的卖单在补建仓模式下撤销（只买不卖）")
    # 单张买单超过剩余空间 → 撤掉该单、其余照常
    st4 = fresh(50000.0)
    _tg_big = [{"code": "600000", "name": "浦发银行", "kind": "stock", "shares": 2000,
                "price": 10.0, "layer": "个股", "level": "R3"}]
    _keep4 = paper.cap_topup_orders(st4, _tg_big, _cm, 11000.0, "2026-10-23",
                                    fill_from="2026-10-24")
    check(_keep4 == [] and any(o["status"] == "cancelled" for o in st4["orders"]),
          "单张超标买单被撤销（20,000 元 > 剩余空间）")
    # R1 现金管理类理财：登记真实年化后才启用，未登记完全不影响原口径
    import capital as _cap
    _old_r1 = config.CAPITAL["cash_layer"].get("r1_annual_rate")
    config.CAPITAL["cash_layer"]["r1_annual_rate"] = None
    config._CASH_MEMO["stamp"] = None
    _cr0 = _cap.cash_net_rates()
    check(_cr0.get("r1") is None, "未登记 R1 年化 → r1=None（不启用）")
    config.CAPITAL["cash_layer"]["r1_annual_rate"] = 0.018
    config._CASH_MEMO["stamp"] = None
    _cr1 = _cap.cash_net_rates()
    check(abs(_cr1["r1"] - 0.018) < 1e-9 and _cr1["money_fund"] >= 0.018 - 1e-9,
          "登记 R1 年化 1.8% → 现金侧=1.8%、money_fund 取较高者")
    config.CAPITAL["cash_layer"]["r1_annual_rate"] = _old_r1
    config._CASH_MEMO["stamp"] = None

    # ---------- 16) 分红送转入账（apply_dividends） ----------
    def _div_state(shares, cost, opened, cash=10000.0):
        st = paper.new_state(10000.0, "B", "2026-10-16", 4000.0)
        st["cash"] = cash
        st["holdings"]["000999"] = {
            "code": "000999", "name": "华润三九", "kind": "stock", "shares": shares,
            "cost_basis": cost, "opened_at": opened, "stop_price": 8.5,
            "target_price": 30.0, "last_close": cost}
        return st

    _dm = {"000999": [{"ex_date": "2026-11-10", "cash_per_share": 0.5, "share_ratio": 3.0}]}
    _st16 = _div_state(100, 10.0, "2026-01-05")
    _r16 = paper.apply_dividends(_st16, "2026-11-20", _dm)
    check(_r16["count"] == 1, f"分红：除权日入账 1 笔 {_r16}")
    # 持有 2026-01-05 → 2026-11-10 = 309 天 → 10% 税；100 股 × 0.5 = 50 含税 → 45 到账
    check(abs(float(_st16["cash"]) - 10045.0) < 1e-6,
          f"分红：现金税后到账 45 元（309 天 → 10%）{_st16['cash']}")
    check(int(_st16["holdings"]["000999"]["shares"]) == 130,
          f"分红：送转 30 股（每 10 股送转 3）{_st16['holdings']['000999']['shares']}")
    check(abs(float(_st16["holdings"]["000999"]["cost_basis"]) - 7.6923) < 1e-3,
          f"分红：成本摊薄 1000/130≈7.6923 {_st16['holdings']['000999']['cost_basis']}")
    check(float(_st16["holdings"]["000999"]["stop_price"]) == 8.5,
          "分红：止损价不因送转改动")
    _r16b = paper.apply_dividends(_st16, "2026-11-20", _dm)
    check(_r16b["count"] == 0, f"分红：重复调用幂等（count=0）{_r16b}")
    check(abs(float(_st16["cash"]) - 10045.0) < 1e-6, "分红：幂等时不重复加钱")
    check(_st16["meta"].get("dividend_paid", {}).get("000999") == "2026-11-10",
          f"分红：meta.dividend_paid 已记 {_st16['meta'].get('dividend_paid')}")

    # 短期持有（11 天）→ 20% 税
    _dm2 = {"000999": [{"ex_date": "2026-11-10", "cash_per_share": 1.0, "share_ratio": 0.0}]}
    _st16b = _div_state(1000, 10.0, "2026-10-30")
    _r16c = paper.apply_dividends(_st16b, "2026-11-20", _dm2)
    check(_r16c["count"] == 1 and abs(float(_st16b["cash"]) - 10800.0) < 1e-6,
          f"分红：持有 11 天 → 20% 税，1000 元红利到账 800 {_r16c} cash={_st16b['cash']}")

    # 除权日早于/等于建仓日 → 不入账（我们没持有那天）
    _st16c = _div_state(100, 10.0, "2026-11-10")
    _r16d = paper.apply_dividends(_st16c, "2026-11-20", _dm)
    check(_r16d["count"] == 0, f"分红：除权日 ≤ opened_at 不入账 {_r16d}")

    # 除权日还没到 → 不入账
    _st16d = _div_state(100, 10.0, "2026-01-05")
    _r16e = paper.apply_dividends(_st16d, "2026-11-09", _dm)
    check(_r16e["count"] == 0, f"分红：as_of 早于除权日不入账 {_r16e}")

    # ETF 不受分红逻辑影响（走累计净值）
    _st16e = _div_state(100, 10.0, "2026-01-05")
    _st16e["holdings"] = {"sh510050": {"code": "sh510050", "name": "上证50ETF",
                                       "kind": "etf", "shares": 200, "cost_basis": 2.9,
                                       "opened_at": "2026-01-05", "stop_price": 2.4,
                                       "last_close": 2.9}}
    _r16f = paper.apply_dividends(_st16e, "2026-11-20",
                                 {"sh510050": [{"ex_date": "2026-03-01",
                                                "cash_per_share": 0.1, "share_ratio": 0}]})
    check(_r16f["count"] == 0, f"分红：ETF 不入账（累计净值口径）{_r16f}")

    # ---------- 17) 风险警示处置（ST/退市整理 → 清仓） ----------
    check(paper.is_risk_warn_name("ST华铁") and paper.is_risk_warn_name("*ST起步"),
          "风险名单：ST/*ST 简称都命中")
    check(paper.is_risk_warn_name("国华退") and paper.is_risk_warn_name("萃华退"),
          "风险名单：退市整理（简称带「退」）命中")
    check(not paper.is_risk_warn_name("华润三九")
          and not paper.is_risk_warn_name("上证50ETF"),
          "风险名单：正常简称不命中")

    _st17 = _div_state(100, 10.0, "2026-01-05")
    _st17["holdings"]["000999"]["stop_price"] = 8.5
    # 先挂一张 GTC 条件止损（清仓时应被撤掉，避免重复卖）
    paper.make_order(_st17, "2026-11-19", "000999", "华润三九", "stock", "sell", 100,
                     "sell_stop", "条件止损", limit_price=8.5, effective_from="2026-11-20")
    _ex17 = paper.risk_exit_orders(_st17, {"000999"}, "2026-11-20", fill_from="2026-11-23")
    check(len(_ex17) == 1 and _ex17[0]["order_type"] == "sell_open" and _ex17[0]["shares"] == 100,
          f"风险清仓：生成一张 sell_open 全卖 {_ex17}")
    check("风险警示" in str(_ex17[0].get("reason") or ""),
          f"风险清仓：reason 说明原因 {_ex17[0].get('reason')}")
    check(_ex17[0].get("effective_from") == "2026-11-23",
          f"风险清仓：下一交易日生效 {_ex17[0].get('effective_from')}")
    _stops17 = [o for o in paper.pending_orders(_st17, "sell") if o["order_type"] == "sell_stop"
                and o["status"] == "pending"]
    check(not _stops17, f"风险清仓：原 GTC 止损已撤 {len(_stops17)}")
    _ex17b = paper.risk_exit_orders(_st17, {"000999"}, "2026-11-20", fill_from="2026-11-23")
    check(_ex17b == [], "风险清仓：重复调用不重复下单")
    _ex17c = paper.risk_exit_orders(_st17, {"002648"}, "2026-11-20")
    check(_ex17c == [], "风险清仓：不在名单里的持仓不动")

    if summary:
        print()
        if FAIL:
            print(f"XX 失败 {FAIL} 项 / 通过 {PASS} 项")
            return 1
        print(f"OK 全部通过（{PASS} 项断言）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    _check_daily_accrual()
    with _legacy_repo7():
        _rc = main(summary=False)
    if _rc == 0:
        _check_pool_default()
        _check_pool_accrual()
        _check_chain_accounting()
        # ---------- 18) 一字板撮合闸门（跌停卖不出、涨停买不到） ----------
        check(paper._board_locked(bar("d", 11.0, 11.0, 11.0, 11.0), 10.0) == 1,
              "一字板：涨停封死判为 +1")
        check(paper._board_locked(bar("d", 9.0, 9.0, 9.0, 9.0), 10.0) == -1,
              "一字板：跌停封死判为 -1")
        check(paper._board_locked(bar("d", 9.5, 9.5, 9.5, 9.5), 10.0) == -1,
              "一字板：ST 5% 跌停也认出来")
        check(paper._board_locked(bar("d", 10.2, 10.6, 9.9, 10.4), 10.0) == 0,
              "一字板：有日内区间不算封死")
        check(paper._board_locked(bar("d", 10.0, 10.0, 10.0, 10.0), 10.0) == 0,
              "一字板：停牌（价格没动）不算封死")

        # 持仓 + 一字跌停的 GTC 止损：当天不成交，顺延到第二天打开后按开盘价卖
        _b18 = fresh()
        _b18["cash"] = 0.0
        _b18["holdings"]["000999"] = {
            "code": "000999", "name": "华润三九", "kind": "stock", "shares": 100,
            "cost_basis": 10.0, "stop_price": 9.0, "opened_at": "2026-10-20",
            "layer": "stock", "level": "R3", "target_price": None, "highest_close": 10.0}
        paper.make_order(_b18, "2026-10-23", "000999", "华润三九", "stock", "sell", 100,
                         "sell_stop", "条件止损", limit_price=9.0, effective_from="2026-10-26")
        _map18 = {"000999": [bar("2026-10-23", 10.0, 10.0, 10.0, 10.0),  # 前收，用于算板价
                         bar("2026-10-26", 9.0, 9.0, 9.0, 9.0),   # 一字跌停，卖不掉
                             bar("2026-10-27", 8.6, 8.9, 8.4, 8.7)]}  # 打开，开盘 8.6
        _ev18 = paper.resolve_orders(_b18, "2026-10-27", _map18)
        _filled18 = [e for e in _ev18 if e[0] == "filled"]
        check(len(_filled18) == 1 and "2026-10-27" in _filled18[0][1] and "8.600" in _filled18[0][1],
              f"跌停封死：顺延到打开日按开盘价成交 {_filled18}")

        # 一字涨停的限价买单：当天不成交，第二天回落后成交
        _b18b = fresh()
        paper.make_order(_b18b, "2026-10-23", "sh510300", "沪深300ETF", "etf", "buy", 200,
                         "buy_limit", "限价买入", limit_price=4.0, effective_from="2026-10-26")
        _map18b = {"sh510300": [bar("2026-10-23", 4.0, 4.0, 4.0, 4.0),     # 前收
                            bar("2026-10-26", 4.4, 4.4, 4.4, 4.4),      # 一字涨停
                                bar("2026-10-27", 4.05, 4.10, 3.95, 4.02)]}
        _ev18b = paper.resolve_orders(_b18b, "2026-10-27", _map18b)
        _filled18b = [e for e in _ev18b if e[0] == "filled"]
        check(len(_filled18b) == 1 and "2026-10-27" in _filled18b[0][1],
              f"涨停封死：买单顺延到打开日成交 {_filled18b}")

        # 普通波动日照常成交（闸门不误伤）
        _b18c = fresh()
        paper.make_order(_b18c, "2026-10-23", "sh510300", "沪深300ETF", "etf", "buy", 200,
                         "buy_limit", "限价买入", limit_price=4.0, effective_from="2026-10-26")
        _map18c = {"sh510300": [bar("2026-10-26", 3.9, 4.1, 3.8, 4.0)]}
        _ev18c = paper.resolve_orders(_b18c, "2026-10-26", _map18c)
        check(len([e for e in _ev18c if e[0] == "filled"]) == 1,
              "普通日：限价单当天正常成交")

        print("\n19) 现金层：货基按逐日万份收益计息")
        import pandas as _pd
        import datasource as _ds
        _wf_old = _ds.fetch_money_fund_hist
        _wf_hist = _pd.DataFrame({"date": ["2026-11-18", "2026-11-19", "2026-11-20"],
                                  "wanfen": [9.9, 0.4, 0.3]})
        _ds.fetch_money_fund_hist = lambda code, days=None: _wf_hist
        try:
            _it = paper._wanfen_interest("007866", 10000.0, "2026-11-18", "2026-11-20")
            check(abs((_it or 0.0) - 0.7) < 1e-9,
                  "两天万份收益 0.4+0.3 → 1 万元利息 0.7 元（起点日不含）")
            check(paper._wanfen_interest("007866", 10000.0, "2026-11-20", "2026-11-20") is None,
                  "窗口内无披露行 → None（调用方不计息，不回落年化）")
            _ds.fetch_money_fund_hist = lambda code, days=None: (_ for _ in ()).throw(
                RuntimeError("接口挂了"))
            check(paper._wanfen_interest("007866", 10000.0, "2026-11-18", "2026-11-20") is None,
                  "取数失败 → None，不抛")
            _ds.fetch_money_fund_hist = lambda code, days=None: _pd.DataFrame()
            check(paper._wanfen_interest("007866", 10000.0, "2026-11-18", "2026-11-20") is None,
                  "空表 → None，不抛")
        finally:
            _ds.fetch_money_fund_hist = _wf_old

        print("\n20) 同一持仓多张 GTC 止损合并")
        _b20 = fresh()
        buy_etf(_b20, shares=200, price=4.0, eff="2026-10-26")
        paper.resolve_orders(_b20, "2026-10-26",
                             {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
        _b20["holdings"]["sh510300"]["stop_price"] = 3.40
        paper.make_order(_b20, "2026-10-27", "sh510300", "沪深300ETF", "etf", "sell", 200,
                         "sell_stop", "旧 GTC（覆盖全部股数）", limit_price=3.40,
                         stop_price=3.40, effective_from="2026-10-28")
        paper.make_order(_b20, "2026-10-27", "sh510300", "沪深300ETF", "etf", "sell", 100,
                         "sell_stop", "括号单（加仓后激活）", limit_price=3.30,
                         stop_price=3.30, effective_from="2026-10-28")
        _msg20 = paper.consolidate_stop_orders(_b20, "2026-10-27", effective_from="2026-10-28")
        _stops20 = [o for o in paper.pending_orders(_b20, "sell")
                    if o["order_type"] == "sell_stop"]
        check(len(_stops20) == 1, f"合并后只剩一张止损（实际 {len(_stops20)} 张）")
        check(_stops20[0]["shares"] == 200 and abs(_stops20[0]["limit_price"] - 3.40) < 1e-9,
              f"覆盖全部 200 股、价格取高者 3.40（实际 {_stops20[0]['shares']} 股"
              f"@{_stops20[0]['limit_price']}）")
        check(abs(float(_b20["holdings"]["sh510300"]["stop_price"]) - 3.40) < 1e-9,
              "holding.stop_price 同步为 3.40")
        check(len(_msg20) == 1 and "2 张 → 1 张" in _msg20[0], f"消息可读：{_msg20}")
        check(_stops20[0]["effective_from"] == "2026-10-28",
              f"合并后 effective_from 顺延到下一交易日（实际 {_stops20[0]['effective_from']}）")
        check(not paper.consolidate_stop_orders(_b20, "2026-10-27"),
              "已是单张完整覆盖 → 幂等，不再动")

        _b20b = fresh()
        buy_etf(_b20b, shares=200, price=4.0, eff="2026-10-26")
        paper.resolve_orders(_b20b, "2026-10-26",
                             {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
        _b20b["holdings"]["sh510300"]["stop_price"] = 3.40
        paper.make_order(_b20b, "2026-10-27", "sh510300", "沪深300ETF", "etf", "sell", 200,
                         "sell_stop", "旧 GTC", limit_price=3.40, stop_price=3.40,
                         effective_from="2026-10-28")
        paper.make_order(_b20b, "2026-10-27", "sh510300", "沪深300ETF", "etf", "sell", 100,
                         "sell_stop", "括号单（更紧）", limit_price=3.60, stop_price=3.60,
                         effective_from="2026-10-28")
        paper.consolidate_stop_orders(_b20b, "2026-10-27")
        _stops20b = [o for o in paper.pending_orders(_b20b, "sell")
                     if o["order_type"] == "sell_stop"]
        check(len(_stops20b) == 1 and abs(_stops20b[0]["limit_price"] - 3.60) < 1e-9,
              f"括号单价格更高 → 合并上移到 3.60（实际 "
              f"{_stops20b[0]['limit_price'] if _stops20b else None}）")
        _bars20 = {"sh510300": [bar("2026-10-28", 3.30, 3.35, 3.20, 3.25)]}
        paper.resolve_orders(_b20b, "2026-10-28", _bars20)
        check("sh510300" not in _b20b["holdings"], "合并后触发止损：持仓清零")
        _sell20 = [t for t in _b20b["trades"] if t["action"] == "sell"]
        check(len(_sell20) == 1 and _sell20[0]["shares"] == 200,
              f"只产生一笔 200 股卖出，不再因重复单报「持仓不足」"
              f"（实际 {[t['shares'] for t in _sell20]}）")

        print("\n21) 止损单改价后 effective_from 顺延（防未来函数）")
        _b21 = fresh()
        buy_etf(_b21, shares=100, price=4.0, eff="2026-10-26")
        paper.resolve_orders(_b21, "2026-10-26",
                             {"sh510300": [bar("2026-10-26", 4.0, 4.0, 4.0, 4.0)]})
        _b21["holdings"]["sh510300"]["stop_price"] = 3.40
        _gtc21 = paper.make_order(_b21, "2026-10-27", "sh510300", "沪深300ETF", "etf",
                                  "sell", 100, "sell_stop", "初始 GTC", limit_price=3.40,
                                  stop_price=3.40, effective_from="2026-10-30")
        # 模拟决策日 11-02 按 ATR 把止损上移到 3.90（stop_orders 更新既有 GTC）
        _b21["holdings"]["sh510300"]["stop_price"] = 3.90
        paper.stop_orders(_b21, "2026-11-02", {"sh510300": 4.10}, {})
        check(abs(float(_gtc21["limit_price"]) - 3.90) < 1e-9,
              f"止损单价格已上移到 3.90（实际 {_gtc21['limit_price']}）")
        check(_gtc21["effective_from"] == "2026-11-03",
              f"改价后 effective_from 顺延到下一交易日 11-03（实际 {_gtc21['effective_from']}）")
        # 10-30 最低 3.50 > 3.40（旧价不成交）；11-02 最低 3.60 夹在旧价与新价之间：
        # 若拿新价 3.90 重放 11-02 会凭空成交——顺延后就不该成交。
        _bars21 = {"sh510300": [bar("2026-10-30", 3.80, 3.85, 3.50, 3.60),
                                bar("2026-11-02", 3.80, 3.85, 3.60, 3.70)]}
        _ev21 = paper.resolve_orders(_b21, "2026-11-02", _bars21)
        check(not _ev21 and "sh510300" in _b21["holdings"],
              "旧窗口（11-02）不按新止损价成交")
        check(_gtc21["status"] == "pending", "止损单仍 pending，等新价格生效日")
        # 11-03 起新价格生效：开盘 3.80 ≤ 3.90 → 按开盘价成交
        _ev21b = paper.resolve_orders(_b21, "2026-11-03",
                                      {"sh510300": [bar("2026-11-03", 3.80, 3.85, 3.50, 3.55)]})
        check(any(e[0] == "filled" for e in _ev21b) and "sh510300" not in _b21["holdings"],
              f"新价格生效日（11-03）正常触发止损：{_ev21b}")

        print()
        if FAIL:
            print(f"XX 失败 {FAIL} 项 / 通过 {PASS} 项")
            _rc = 1
        else:
            print(f"OK 全部通过（{PASS} 项断言）")
    raise SystemExit(_rc)
