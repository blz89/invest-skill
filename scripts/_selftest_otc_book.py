# -*- coding: utf-8 -*-
"""自检：场外债券基金影子账（otc_book.py）+ 与 paper 账本的接入。

跑法：python scripts/_selftest_otc_book.py     （不联网、不读写真实账本）
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import otc_book  # noqa: E402
import paper  # noqa: E402

N = [0]
BAD = []


def check(ok, msg):
    N[0] += 1
    if ok:
        print(f"  OK   {msg}")
    else:
        BAD.append(msg)
        print(f"  FAIL {msg}")


def fresh(cash=50000.0):
    st = paper.new_state(cash)
    st["meta"]["baseline_date"] = "2026-10-28"
    return st


def series(vals):
    """{'2026-10-27': 1.0} 形式 → [(date, value)]"""
    return sorted(vals.items())


print("=" * 78)
print("1) 老账本兼容：ensure() 补容器")
st = {"cash": 1.0, "holdings": {}}
otc_book.ensure(st)
check(st["otc_holdings"] == {} and st["otc_orders"] == [] and st["otc_trades"] == [],
      "缺三个容器时 ensure() 补空 dict/list")
check(otc_book.reserved(st) == 0.0, "空账本挂单占用 0")

print("2) 买入成交（金额法 + 申购费）")
st = fresh()
o = otc_book.make(st, "2026-10-28", "2026-10-29", "OTC:012310", "民生加银",
                  "buy", 2400.0, fee_pct=0.04, redeem_fee_hold=0.0)
nav = {"OTC:012310": series({"2026-10-29": 1.1252, "2026-10-30": 1.1280})}
t = otc_book.fill(st, o, "2026-10-30", nav)
check(t is not None and o["status"] == "filled", "有效净值日到达 → 成交")
check(abs(st["cash"] - (50000.0 - 2400.0)) < 1e-6, "现金减少申购金额 2400")
check(abs(t["fee"] - 0.96) < 1e-6, f"申购费 2400×0.04% = 0.96（实际 {t['fee']}）")
h = st["otc_holdings"]["OTC:012310"]
check(abs(h["amount"] - 2399.04) < 1e-6, "持仓投入 = 2400 − 0.96 = 2399.04")
check(t["date"] == "2026-10-29", "按 effective_from(10-29) 的净值成交，不是最新净值")

print("3) 市值/浮动（用累计净值比例）")
otc_book.mark(st, {"OTC:012310": series({"2026-10-30": 1.1385})})
mv = otc_book.market_value(st)
exp = round(2399.04 * 1.1385 / 1.1252, 2)
check(abs(mv - exp) < 0.02, f"市值 = 投入×(新累计/买时累计) = {exp}（实际 {mv}）")
check(abs(otc_book.unrealized(st) - (mv - 2399.04)) < 0.02, "浮动 = 市值 − 投入")

print("4) 加仓的加权累计净值")
o2 = otc_book.make(st, "2026-10-30", "2026-11-02", "OTC:012310", "民生加银",
                   "buy", 1000.0, fee_pct=0.0)
otc_book.fill(st, o2, "2026-11-03",
              {"OTC:012310": series({"2026-11-02": 1.1400})})
h = st["otc_holdings"]["OTC:012310"]
check(abs(h["amount"] - 3399.04) < 1e-6, "两笔投入合并 2399.04 + 1000")
check(1.1252 < float(h["acc_nav"]) < 1.1400, f"合并后累计净值在两次之间（{h['acc_nav']}）")

print("5) 赎回：满 90 天费率 0")
o3 = otc_book.make(st, "2027-02-01", "2027-02-02", "OTC:012310", "民生加银",
                   "sell", 1500.0, redeem_fee_hold=0.0)
nav_long = {"OTC:012310": series({"2027-02-02": 1.1600})}
t3 = otc_book.fill(st, o3, "2027-02-02", nav_long)
check(t3 is not None and t3["fee"] == 0.0, "持有超 90 天 → 赎回费 0")
check(t3["realized_pnl"] is not None, "赎回落袋给出 realized_pnl")
check(abs(st["cash"] - (50000 - 2400 - 1000 + 1500)) < 1e-6,
      f"现金回到 {st['cash']:.2f}（+1500 赎回款）")

print("6) 不足 90 天赎回 → 按最严档 1.5% 罚")
st2 = fresh()
o4 = otc_book.make(st2, "2026-10-28", "2026-10-29", "OTC:018074", "中银",
                   "buy", 2300.0, redeem_fee_hold=0.0)
otc_book.fill(st2, o4, "2026-10-30", {"OTC:018074": series({"2026-10-29": 1.0800})})
o5 = otc_book.make(st2, "2026-11-02", "2026-11-03", "OTC:018074", "中银", "sell", 1000.0)
t5 = otc_book.fill(st2, o5, "2026-11-03", {"OTC:018074": series({"2026-11-03": 1.0820})})
check(abs(t5["fee"] - round(1000.0 * otc_book.SHORT_FEE, 2)) < 1e-6,
      f"持有 5 天赎回费 = 1000×1.5% = 15（实际 {t5['fee']}）")

print("7) nav_at：取「当日或之前最近一个」")
s = series({"2026-10-27": 1.0, "2026-10-29": 1.1, "2026-11-02": 1.2})
check(otc_book.nav_at(s, "2026-10-28") == ("2026-10-27", 1.0), "周末取前一交易日净值")
check(otc_book.nav_at(s, "2026-10-29") == ("2026-10-29", 1.1), "当天有净值就取当天")
check(otc_book.nav_at(s, "2026-10-26") == (None, None), "早于首个净值日 → 取不到")

print("8) fail-closed：取不到净值就保持挂单")
st3 = fresh()
o6 = otc_book.make(st3, "2026-10-28", "2026-10-29", "OTC:999999", "无净值", "buy", 100.0)
r = otc_book.fill(st3, o6, "2026-10-30", {})
check(r is None and o6["status"] == "pending", "没有净值 → 不成交、仍是 pending")

print("9) resolve：到期才撮合，未到期不动")
st4 = fresh()
ok1 = otc_book.make(st4, "2026-10-28", "2026-10-29", "OTC:012310", "A", "buy", 1000.0)
ok2 = otc_book.make(st4, "2026-10-28", "2026-11-05", "OTC:018074", "B", "buy", 500.0)
nav4 = {"OTC:012310": series({"2026-10-29": 1.1}), "OTC:018074": series({"2026-10-29": 1.0})}
msgs = otc_book.resolve(st4, "2026-10-30", nav4)
check(ok1["status"] == "filled" and ok2["status"] == "pending", "到期的成交、未到期的保持")
check(any("场外申购" in m for _, m in msgs), "返回可读的中文消息")
check(abs(otc_book.reserved(st4) - 500.0) < 1e-6, "在挂单占用现金 500")

print("10) target_orders：多退少补 + allow 开关")
st5 = fresh()
w = [{"code": "OTC:012310", "name": "A", "amount": 2000.0, "fee": 0.0,
      "redeem_fee_hold": 0.0, "min_purchase": 10.0}]
nav5 = {"OTC:012310": series({"2026-10-29": 1.0})}
orders = otc_book.target_orders(st5, "2026-10-28", "2026-10-29", w, nav5,
                                cash_available=50000.0, allow=True)
check(len(orders) == 1 and orders[0]["action"] == "buy", "空仓 → 补到目标（申购 2000）")
check(abs(orders[0]["amount"] - 2000.0) < 1e-6, "申购金额 = 目标 − 当前市值")
orders2 = otc_book.target_orders(st5, "2026-10-28", "2026-10-29", [], nav5,
                                 cash_available=50000.0, allow=False)
check(orders2 == [], "allow=False（状态未变）→ 不下单")
otc_book.resolve(st5, "2026-10-30", nav5)
h5 = st5["otc_holdings"]["OTC:012310"]
h5["nav"] = 1.10                      # 市值涨到 2200 > 目标 2000
orders3 = otc_book.target_orders(st5, "2026-11-02", "2026-11-03", w, 
                                 {"OTC:012310": series({"2026-11-02": 1.10})},
                                 cash_available=50000.0, allow=True)
check(len(orders3) == 1 and orders3[0]["action"] == "sell"
      and abs(orders3[0]["amount"] - 200.0) < 1.0, "超过目标 → 赎回差额 200")

print("10b) 参考净值：订单同时记单位净值与累计净值（建单日）")
st5b = fresh()
_o_nav = otc_book.make(st5b, "2026-10-28", "2026-10-29", "OTC:012310", "A", "buy",
                       1000.0, nav_ref=1.1000, nav_ref_date="2026-10-27",
                       nav_ref_cum=1.1788)
check(_o_nav.get("nav_ref") == 1.1 and _o_nav.get("nav_ref_cum") == 1.1788,
      "make() 同时落 unit/cum 两列参考净值")
check(_o_nav.get("nav_ref_cum") is not None
      and "nav_ref_cum" in otc_book.make(st5b, "2026-10-28", "2026-10-30",
                                         "OTC:012310", "A", "buy", 500.0),
      "老调用不传 nav_ref_cum 时字段也在（None，向后兼容）")
_w_cum = [{"code": "OTC:012310", "name": "A", "amount": 1500.0, "fee": 0.0,
           "redeem_fee_hold": 0.0, "min_purchase": 10.0,
           "nav_ref_cum": 1.2500}]
_st5c = fresh()
_ord_cum = otc_book.target_orders(_st5c, "2026-10-28", "2026-10-29", _w_cum,
                                  {"OTC:012310": series({"2026-10-28": 1.2})},
                                  cash_available=50000.0, allow=True)
check(_ord_cum and _ord_cum[0].get("nav_ref_cum") == 1.25,
      f"计划给累计净值 → 订单照抄（{_ord_cum[0].get('nav_ref_cum') if _ord_cum else None}）")
_w_nocum = [dict(_w_cum[0])]
_w_nocum[0].pop("nav_ref_cum")
_st5d = fresh()
_ord_fb = otc_book.target_orders(_st5d, "2026-10-28", "2026-10-29", _w_nocum,
                                 {"OTC:012310": series({"2026-10-28": 1.2})},
                                 cash_available=50000.0, allow=True)
check(_ord_fb and _ord_fb[0].get("nav_ref_cum") == 1.2,
      f"计划缺累计净值 → 用 nav_map 当日累计净值兜底（{_ord_fb[0].get('nav_ref_cum') if _ord_fb else None}）")
st5e = fresh()
otc_book.fill(st5e, otc_book.make(st5e, "2026-10-28", "2026-10-29", "OTC:012310",
                                  "A", "buy", 1000.0),
              "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.0})})
otc_book.mark(st5e, {"OTC:012310": series({"2026-10-30": 1.05})})
_rows5 = otc_book.holdings_rows(st5e)
check(_rows5 and _rows5[0].get("acc_nav") == 1.0,
      "holdings_rows 带买入日累计净值（acc_nav）")

print("11) paper 接入：新账本容器 / 净值含场外 / 复利含场外已实现")
st6 = fresh()
check(st6.get("otc_holdings") == {} and st6.get("otc_orders") == []
      and st6.get("otc_trades") == [], "paper.new_state() 自带三个场外容器")
o7 = otc_book.make(st6, "2026-10-28", "2026-10-29", "OTC:012310", "A", "buy", 1000.0)
otc_book.fill(st6, o7, "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.0})})
otc_book.mark(st6, {"OTC:012310": series({"2026-10-30": 1.05})})
e = paper.settle(st6, "2026-10-30", {}, None)
check(e["otc_value"] == 1050.0, f"结算净值含场外市值 1050（实际 {e['otc_value']}）")
check(abs(e["nav"] - (float(st6["cash"]) + 1050.0)) < 0.02, "总净值 = 现金 + 场外市值")
check(e["positions"] == 1, "持仓数含场外 1 只")
o8 = otc_book.make(st6, "2027-02-01", "2027-02-02", "OTC:012310", "A", "sell", 1050.0,
                   redeem_fee_hold=0.0)
otc_book.fill(st6, o8, "2027-02-02", {"OTC:012310": series({"2027-02-02": 1.05})})
real = paper.realized_pnl_total(st6)
check(real > 40.0, f"复利口径含场外已实现（{real} > 40）")

print("12) paper.reserved_cash 把场外挂单算进去")
st7 = fresh()
otc_book.make(st7, "2026-10-28", "2026-10-29", "OTC:012310", "A", "buy", 3000.0)
check(abs(paper.reserved_cash(st7) - 3000.0) < 1e-6, "场外在挂申购占用现金 3000")
check(abs(paper.available_cash(st7) - (50000.0 - 3000.0 - 1000.0)) < 1e-6,
      "可动用现金扣掉场外占款与 2% 缓冲")

print("13) portfolio 明细含场外行")
st8 = fresh()
o9 = otc_book.make(st8, "2026-10-28", "2026-10-29", "OTC:012310", "民生加银",
                   "buy", 1000.0)
otc_book.fill(st8, o9, "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.0})})
otc_book.mark(st8, {"OTC:012310": series({"2026-10-30": 1.02})})
pf = paper.portfolio(st8, {})
codes = [r["code"] for r in pf["rows"]]
check("OTC:012310" in codes, "portfolio.rows 含场外持仓")
check(abs(pf["market_value"] - 1020.0) < 0.02, "总市值含场外 1020")

print("14) 周报渲染 + 剧本（weekly.otc_md）")
import weekly  # noqa: E402
st9 = fresh()
o10 = otc_book.make(st9, "2026-10-28", "2026-10-29", "OTC:012310", "民生加银",
                    "buy", 2400.0, fee_pct=0.04, redeem_fee_hold=0.0)
otc_book.fill(st9, o10, "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.1252})})
otc_book.mark(st9, {"OTC:012310": series({"2026-10-30": 1.1500})})
otc_book.make(st9, "2026-11-02", "2026-11-03", "OTC:018074", "中银", "buy", 2300.0,
              nav_ref=1.0844, nav_ref_cum=1.1053)
_md = weekly.otc_md(st9)
check("## 场外债券基金（R2 层影子账）" in _md, "周报含场外小节")
check("OTC:012310" in _md and "2,4" in _md.replace("，", ","), "周报列出持仓与市值")
check("累计净值（买入→最新）" in _md, "周报持仓表标注累计净值两列")
check("1.1252 → 1.1500" in _md, "周报显示 买入日→最新 累计净值")
check("OTC:018074" in _md and "在挂" in _md, "周报列出在挂单")
check("参考 单位 1.0844 / 累计 1.1053" in _md, "在挂单显示单位/累计两列参考净值")
check("下一交易日累计净值" in _md, "周报写明影账口径")
check(weekly.otc_md(fresh()) == "", "无场外持仓时不出小节")

print("15) 场外腿首次补建闸门（bootstrap_needed / weekly.otc_allow_rebalance）")
st10 = fresh()
check(otc_book.bootstrap_needed(st10) is True, "三无账本 → 需要首次补建")
check(weekly.otc_allow_rebalance(st10, "regime_change", False) is True,
      "regime_change 且未重建时，三无账本仍允许补建")
st11 = fresh()
otc_book.make(st11, "2026-10-30", "2026-11-02", "OTC:012310", "A", "buy", 1000.0)
check(otc_book.bootstrap_needed(st11) is False, "有在挂申购单 → 不算三无")
st12 = fresh()
o11 = otc_book.make(st12, "2026-10-28", "2026-10-29", "OTC:012310", "A", "buy", 1000.0)
otc_book.fill(st12, o11, "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.0})})
check(otc_book.bootstrap_needed(st12) is False, "有场外持仓/历史成交 → 不再补建")
check(st12[otc_book.HOLD_KEY]["OTC:012310"]["buy_date"] == "2026-10-29",
      "buy_date 记成交净值日（10-29）而非结算日（10-30），持有天数/赎回费才正确")
check(weekly.otc_allow_rebalance(st12, "regime_change", False) is False,
      "regime_change 未重建 + 已有场外腿 → 不调仓")
check(weekly.otc_allow_rebalance(fresh(), "full", False) is True, "full 口径 → 允许动")
check(weekly.otc_allow_rebalance(fresh(), "regime_change", True) is True,
      "本期重建 → 允许动")

print("16) HTML 报告：场外小节 + 订单表可见性（report_html / weekly.html_orders）")
import report_html  # noqa: E402
_h = report_html._section_otc(st9)
check("场外债券基金（R2 层影子账）" in _h, "HTML 含场外小节")
check("OTC:012310" in _h, "HTML 列出场外持仓")
check("在挂（1 张）" in _h and "OTC:018074" in _h, "HTML 列出在挂场外单")
check(report_html._section_otc(fresh()) == "", "无场外腿时不出小节")
check(report_html._section_otc(None) == "", "state 为空时不出小节")
_fake_otc = [{"code": "OTC:012310", "name": "A", "action": "buy", "amount": 2400.0,
              "nav_ref": 1.1252, "layer": "理财(R2)"}]
_rows = weekly.html_orders([{"code": "000001", "name": "X", "action": "buy"}], _fake_otc)
check(len(_rows) == 2, "订单表 = 场内新单 + 场外新单")
check(_rows[1]["code"] == "OTC:012310" and _rows[1]["action"] == "申购"
      and _rows[1]["tranche"] == "场外", "场外单渲染为申购/场外")
check(weekly.html_orders([], None) == [], "无新单时订单表为空")

print("17) 场外目标只取 OTC: 前缀（weekly.otc_targets 虚单回归）")
_mix = {"wealth_positions": [
    {"code": "sh511520", "name": "政金债ETF富国", "amount": 11954.0},
    {"code": "OTC:016800", "name": "建信鑫和30天持有期债券C", "amount": 2400.0},
]}
_tg = weekly.otc_targets(_mix)
check(len(_tg) == 1 and _tg[0]["code"] == "OTC:016800",
      "wealth_positions 里的场内债券 ETF 不进场外腿目标")
check(weekly.otc_targets({}) == [], "无财富层目标 → 空")
check(weekly.otc_targets({"wealth_positions": [{"code": "sh510050"}]}) == [],
      "只有场内 ETF → 空（不虚下场外单）")

print("18) 周报仓位可见性：含在挂/场外目标敞口（paper.position_exposure）")
st13 = fresh()
o13 = otc_book.make(st13, "2026-10-28", "2026-10-29", "OTC:012310", "民生加银",
                    "buy", 2400.0, fee_pct=0.0)
otc_book.fill(st13, o13, "2026-10-30", {"OTC:012310": series({"2026-10-29": 1.1000})})
paper.make_order(st13, "2026-10-30", "sh510050", "上证50ETF", "etf", "buy", 100,
                 "buy_limit", "周报敞口自检", limit_price=10.0)
otc_book.make(st13, "2026-11-02", "2026-11-03", "OTC:018074", "中银", "buy", 2300.0)
_pf13 = paper.portfolio(st13, {})
_ratio13, _expo13 = paper.position_exposure(st13, _pf13)
_expected = (_pf13["market_value"] + 100 * 10.0 + 2300.0) / _pf13["nav"]
check(abs(_expo13 - _expected) < 1e-9,
      f"含在挂目标敞口 = (持仓市值+在挂买单+场外在挂申购)/nav（{_expo13:.4f}）")
check("含在挂目标敞口" in weekly.position_text(st13, _pf13),
      "周报 md 的仓位标明含在挂目标敞口")
check("含在挂目标敞口" in report_html._section_kpi(_pf13, st13, 0.0),
      "周报 HTML 的仓位备注标明含在挂目标敞口")
_st14 = fresh()
_pf14 = paper.portfolio(_st14, {})
check("含在挂目标敞口" not in weekly.position_text(_st14, _pf14),
      "无在挂/场外时仓位不重复标注")

print("19) 场外净值取数覆盖：持仓 + 在挂单 + 计划目标（weekly.otc_nav_codes）")
st15 = fresh()
o15 = otc_book.make(st15, "2026-10-30", "2026-11-02", "OTC:016800", "建信鑫和", "buy", 2400.0)
check(weekly.otc_nav_codes(st15, {}) == ["OTC:016800"],
      "在挂场外单的代码必须取净值（否则 effective_from 到了也撮合不了）")
otc_book.fill(st15, o15, "2026-11-02", {"OTC:016800": series({"2026-11-02": 1.16})})
_plan15 = {"wealth_positions": [
    {"code": "OTC:016800", "name": "建信鑫和", "amount": 2400.0},
    {"code": "OTC:012310", "name": "民生加银", "amount": 2400.0},
    {"code": "sh511520", "name": "政金债ETF富国", "amount": 11954.0},
]}
_codes15 = weekly.otc_nav_codes(st15, _plan15)
check(_codes15 == ["OTC:012310", "OTC:016800"],
      "持仓+计划目标合并去重、只取 OTC: 前缀、排序（场内 ETF 不进）")
check(weekly.otc_nav_codes(fresh(), {}) == [], "空账本空计划 → 空表")

print("20) 评审：场外持仓计入 R2 风险占比（review.one_ledger）")
import json  # noqa: E402
import tempfile  # noqa: E402
import review  # noqa: E402

st16 = fresh()
st16["cash"] = 30000.0
st16["holdings"]["600000"] = {
    "code": "600000", "name": "浦发银行", "kind": "stock", "layer": "个股",
    "level": "R3", "shares": 1000, "cost": 10.0, "cost_basis": 10.0,
    "last_close": 10.0,
    "industry": "银行", "stop_price": 8.5, "opened_at": "2026-10-01"}
st16["otc_holdings"]["OTC:012310"] = {
    "code": "OTC:012310", "name": "民生加银", "layer": otc_book.LAYER,
    "level": otc_book.LEVEL, "amount": 10000.0, "acc_nav": 1.0, "nav": 1.0,
    "buy_date": "2026-09-01", "confirm_date": "2026-09-02",
    "nav_date": "2026-10-28", "redeem_fee_hold": 0.0}
_e16 = paper.settle(st16, "2026-10-30", {"600000": 10.0})
_tmp16 = os.path.join(tempfile.mkdtemp(prefix="otc_review_"), "state.json")
with open(_tmp16, "w", encoding="utf-8") as f:
    json.dump(st16, f, ensure_ascii=False)
_res16 = review.one_ledger("t", {"state_file": _tmp16, "capital": 50000.0,
                                 "label": "测试"})
_rk16 = _res16.get("risk") or {}
check(_rk16.get("count") == 2,
      f"风险评审含场外持仓（count=2，实际 {_rk16.get('count')}）")
check(abs(float((_rk16.get("weights") or {}).get("R2") or 0.0) - 0.5) < 1e-6,
      f"R2 占比 = 场外市值/已投 = 50%（实际 {(_rk16.get('weights') or {}).get('R2')}）")
check(abs(float(_rk16.get("r2_share") or 0.0) - 0.5) < 1e-6,
      "r2_share 与计划口径一致（0.5）")
check(not any("没有风险等级" in str(x) for x in (_rk16.get("violations") or [])),
      "场外腿带 R2 等级，不触发「没有风险等级」fail-closed")
check(abs(float((_e16 or {}).get("otc_value") or 0.0) - 10000.0) < 1e-6,
      "结算净值含场外市值 10,000")

print("21) HTML KPI：现金利息「已落账 + 在途」双行（不再出现 0.00 矛盾）")
_pf16 = paper.portfolio(st16, {"600000": 10.0})
_h16 = report_html._section_kpi(_pf16, st16, 0.0,
                                {"booked": 12.34, "pending": 2.66, "total": 15.0})
check("已落账" in _h16 and "在途" in _h16,
      "现金卡同时给已落账与在途两行")
check("本期利息 0.00" not in _h16,
      "传 cash_int 后不再出现「本期利息 0.00」")
check("15.00" in _h16, "合计利息与 cash_interest_split 同源（15.00）")

print("=" * 78)
if BAD:
    print(f"FAIL 共 {len(BAD)} 项失败 / {N[0]} 项")
    for b in BAD:
        print("  - " + b)
    sys.exit(1)
print(f"OK 全部通过（{N[0]} 项断言）")
