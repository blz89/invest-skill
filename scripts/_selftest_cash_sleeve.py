# -*- coding: utf-8 -*-
"""自检：现金层「半流动性货币增值」切片 + 逆回购脉冲择时（投资层）。

跑法：python scripts/_selftest_cash_sleeve.py     （不联网、不读写真实账本/排行文件）

覆盖：
  1) cash_sleeve.plan：每支 ≤1 万快赎、按 90 天降序、金额不足/超额/暂停申购/fail-closed
  2) cash_sleeve 的 stale 判定与 rerank_due
  3) repo_pulse.decide：门槛、跨长假只收 1 笔费、打平报价公式
  4) repo_pulse.fee_annual / fee_rate：1 天期 0.365%、跨 9 天 0.0406%
  5) repo_pulse.choose_term / actual_days / settle_day：按净 %-天 取最大期限
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import cash_sleeve  # noqa: E402
import repo_pulse   # noqa: E402

N = [0]
BAD = []


def check(ok, msg):
    N[0] += 1
    if ok:
        print(f"  OK   {msg}")
    else:
        BAD.append(msg)
        print(f"  FAIL {msg}")


def near(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


RANK = {"as_of": "2026-11-15", "data_date": "2026-11-13", "rows": [
    {"code": "A", "name": "永赢天天利货币A", "y90": 0.01309, "y30": 0.01296,
     "y7": 0.01483, "y180": 0.01293, "buy": "限制大额申购", "last_date": "2026-11-13"},
    {"code": "B", "name": "博时兴荣货币B", "y90": 0.01305, "y30": 0.01297,
     "y7": 0.01260, "y180": 0.01291, "buy": "限制大额申购", "last_date": "2026-11-13"},
    {"code": "C", "name": "创金合信货币C", "y90": 0.01304, "y30": 0.01317,
     "y7": 0.01331, "y180": 0.01320, "buy": "限制大额申购", "last_date": "2026-11-13"},
    {"code": "D", "name": "暂停的那只", "y90": 0.01300, "y30": 0.01,
     "y7": 0.01, "y180": 0.01, "buy": "暂停申购", "last_date": "2026-11-13"},
]}

print("1) plan：27,000 元 → 3 支 × ≤1 万，加权 = 金额加权 90 天年化")
p = cash_sleeve.plan(27000, rank=RANK, today="2026-11-15")
check(len(p["holdings"]) == 3, "取 3 支")
check(all(h["amount"] <= 10000.0 + 1e-9 for h in p["holdings"]), "每支 ≤1 万（快赎额度）")
check([h["amount"] for h in p["holdings"]] == [10000.0, 10000.0, 7000.0], "按排名先填满 1 万再下一支")
check(near(p["residual"], 0.0), "27,000 元分完，零头 0")
_w = (10000 * 0.01309 + 10000 * 0.01305 + 7000 * 0.01304) / 27000
check(near(p["weighted_rate"], _w, 1e-6), f"加权利率 = {_w:.4%}")
check(p["fallback"] is False and p["stale"] is False, "正常情况不 fail-closed")

print("2) plan：超额金额 → 溢出进 residual；暂停申购的被跳过")
p2 = cash_sleeve.plan(45000, rank=RANK, today="2026-11-15")
check(len(p2["holdings"]) == 3 and near(p2["residual"], 15000.0),
      "45,000 元只切 30,000，溢出 15,000 留朝朝宝")
check(all(h["code"] != "D" for h in p2["holdings"]), "暂停申购的不买（名次再高也买不进）")

print("3) plan：小额只买一支")
p3 = cash_sleeve.plan(6000, rank=RANK, today="2026-11-15")
check(len(p3["holdings"]) == 1 and near(p3["holdings"][0]["amount"], 6000.0), "6,000 元买 1 支")

print("4) plan：0 元 / 排行过期 / 排行缺失 → fail-closed，整笔退回")
check(cash_sleeve.plan(0, rank=RANK)["residual"] == 0.0, "0 元不动")
stale = cash_sleeve.plan(27000, rank=RANK, today="2026-11-20")
check(stale["fallback"] and stale["stale"] and not stale["holdings"],
      "数据日 11-13 到 11-20（7 天）> 5 天阈值 → 不切、整笔退回")
empty = cash_sleeve.plan(27000, rank={"rows": [], "data_date": "2026-11-13"})
check(empty["fallback"] and near(empty["residual"], 27000.0), "排行里没有可用基金 → fallback")

print("4b) plan：allow_stale 只用于计息——沿用最后持仓，不退回朝朝宝")
stale_ok = cash_sleeve.plan(27000, rank=RANK, today="2026-11-20", allow_stale=True)
check(stale_ok["stale"] and stale_ok["degraded"] and not stale_ok["fallback"],
      "排行过期 + allow_stale → stale/degraded 标记，不 fail-closed")
check([h["amount"] for h in stale_ok["holdings"]] == [10000.0, 10000.0, 7000.0],
      "金额切分与过期前一致（沿用最后持仓）")
check("超过" in stale_ok["note"] or "未更新" in stale_ok["note"], "note 说明排行已过期")
RANK_PAUSED = {"as_of": "2026-11-15", "data_date": "2026-11-13", "rows": [
    {"code": "D", "name": "暂停的那只", "y90": 0.0200, "buy": "暂停申购"},
    {"code": "B", "name": "博时兴荣货币B", "y90": 0.0130, "buy": "限制大额申购"},
    {"code": "C", "name": "创金合信货币C", "y90": 0.0120, "buy": "限制大额申购"},
]}
fresh_skip = cash_sleeve.plan(25000, rank=RANK_PAUSED, today="2026-11-15")
stale_keep = cash_sleeve.plan(25000, rank=RANK_PAUSED, today="2026-11-20", allow_stale=True)
check(all(h["code"] != "D" for h in fresh_skip["holdings"]),
      "新配置跳过暂停申购（名次再高也不买）")
check([h["code"] for h in stale_keep["holdings"]][:1] == ["D"],
      "计息路径保留已有持仓，不因暂停申购剔除")

print("5) data_age_days / rerank_due")
check(cash_sleeve.data_age_days(RANK, "2026-11-15") == 2, "数据日 11-13 → 11-15 是 2 天")
check(cash_sleeve.rerank_due("", "2026-11-15") is True, "没有上次重排日 → 该重排")
check(cash_sleeve.rerank_due("2026-11-01", "2026-11-15", days=30) is False, "14 天 < 30 天 → 不用重排")
check(cash_sleeve.rerank_due("2026-10-01", "2026-11-15", days=30) is True, "45 天 ≥ 30 天 → 该重排")

print("6) describe：一行中文能读")
d = cash_sleeve.describe(p)
check("半流动性货币增值" in d and "加权" in d, "describe 含口径与加权")

print("7) repo_pulse 费率：1 天期年化 0.365%；跨 9 天只收 1 笔 → 0.0406%")
check(near(repo_pulse.fee_annual(1, 1), 0.00001 * 365.0, 1e-9), "1 天期占款 1 天 = 0.365%/年")
check(abs(repo_pulse.fee_annual(1, 9) - 0.00001 * 365.0 / 9) < 1e-9,
      "跨长假买 1 天期占款 9 天 = 0.0406%/年（只收 1 笔费）")
check(abs(repo_pulse.fee_annual(7, 7) - 0.00005 * 365.0 / 7) < 1e-9, "7 天期 = 0.26%/年")

print("8) repo_pulse.decide：底仓 1.31% 下，1.5% 不够、2.2% 跨长假够")
bench = 0.0131
no = repo_pulse.decide(0.015, 1, bench, 27000)
check(no["go"] is False, "1.5% 普通日 NO-GO（扣费后倒贴底仓）")
check(abs(no["edge_annual"] - (0.015 - bench - 0.00001 * 365.0)) < 1e-9, "超额年化 = 报价−底仓−费年化")
go = repo_pulse.decide(0.022, 9, bench, 27000)
check(go["go"] is True, "2.2% + 占款 9 天 GO")
check(abs(go["breakeven_rate"] - (bench + repo_pulse.fee_annual(1, 9) + 0.003)) < 1e-6,
      f"打平报价 = 底仓 + 费年化 + 30bp，实算 {go['breakeven_rate']:.4%}")
check(near(go["edge"], round(go["gross"] - go["fee"] - 27000 * bench * 9 / 365.0, 2)),
      "超额金额 = 毛利 − 费 − 底仓机会成本")
big = repo_pulse.decide(0.03, 30, bench, 27000)
check(big["go"] is False and any("上限" in x for x in big["reasons"]), "占款 30 天 > 10 天上限 → NO-GO")
small = repo_pulse.decide(0.03, 9, bench, 500)
check(small["go"] is False and any("整手" in x for x in small["reasons"]), "500 元 < 1,000 元整手 → NO-GO")
flat = repo_pulse.decide(0.05, 1, bench, 27000)
check(flat["go"] is False and any("3 天" in x for x in flat["reasons"]),
      "占款 1 天 < 3 天窗口 → NO-GO（利率再高也不做，只等跨周末/长假）")
wk = repo_pulse.decide(0.05, 3, bench, 27000)
check(wk["go"] is True, "跨周末占款 3 天 + 5% → GO（刚过窗口闸）")

print("9) repo_pulse.choose_term：按同窗口净 %-天 取最大（尽量争取最大利润）")
c1 = repo_pulse.choose_term(0.040, 1, 0.005, bench)
check(c1["term"] == 1, "脉冲 4.0% / 7 天期只有 0.5%：1 天期赚更多")
c2 = repo_pulse.choose_term(0.020, 1, 0.028, bench)
check(c2["term"] == 7, "脉冲 2.0% / 7 天期 2.8%：锁 7 天更优")
c3 = repo_pulse.choose_term(0.020, 1, 0.028, bench, need_build=True)
check(c3["term"] == 1 and "建仓" in c3["why"], "要建仓时 7 天期再好也只能用 1 天期")
c4 = repo_pulse.choose_term(0.028, 9, 0.020, bench)
check(c4["term"] == 1, "跨长假占款 9 天：1 天期吃 9 天息，长期限不换")
c5 = repo_pulse.choose_term(0.028, 1, None, bench)
check(c5["term"] == 1, "没有 7 天期报价 → 默认 1 天期")
c6 = repo_pulse.choose_term(0.020, 1, 0.019, bench, rate_14=0.030)
check(c6["term"] == 14 and c6["scores"]["14"] == max(c6["scores"].values()),
      "春节前 14 天期 3.0%：净 %-天 最大 → 选 14 天期")
c7 = repo_pulse.choose_term(0.040, 1, 0.005, bench)
check(c7["term"] == 1 and "14" not in c7["scores"] and c7["fourteen_day_score"] is None,
      "没给 14 天期报价 → 不参与比较")
check(repo_pulse.actual_days(dt.date(2026, 11, 20), 1) == 3, "周五买 1 天期：实际占款 3 天")
check(repo_pulse.actual_days(dt.date(2026, 11, 19), 1) == 1, "周四买 1 天期：实际占款 1 天")
check(repo_pulse.settle_day(dt.date(2026, 11, 21)) == dt.date(2026, 11, 23),
      "周六成交顺延到周一")
check(repo_pulse.actual_days(dt.date(2026, 11, 20), 7) == 7, "周五买 7 天期：7 天后仍是周五")

# 6) 逐日利率历史：同一天去重、跨日追加
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import rates  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="rates_hist_")
_old_hist = rates.HIST_FILE
rates.HIST_FILE = os.path.join(_tmp, "cash_rates_history.jsonl")
try:
    rates._append_history({"as_of": "2026-11-19", "money_fund": 0.0107,
                           "repo": 0.0125, "repo7": 0.0135}, "t1")
    rates._append_history({"as_of": "2026-11-19", "money_fund": 0.0108,
                           "repo": 0.0125, "repo7": 0.0135}, "t2")
    with open(rates.HIST_FILE, encoding="utf-8") as _fh:
        rows = [json.loads(x) for x in _fh if x.strip()]
    check(len(rows) == 1 and rows[0]["money_fund"] == 0.0108,
          "利率历史：同一天只留最新一条")
    rates._append_history({"as_of": "2026-11-20", "money_fund": 0.0106,
                           "repo": 0.0125, "repo7": 0.0135}, "t3")
    with open(rates.HIST_FILE, encoding="utf-8") as _fh:
        rows = [json.loads(x) for x in _fh if x.strip()]
    check(len(rows) == 2 and rows[-1]["date"] == "2026-11-20",
          "利率历史：跨日追加")
finally:
    rates.HIST_FILE = _old_hist
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if BAD:
    print(f"FAIL — {len(BAD)} 项失败：")
    for b in BAD:
        print(f"  - {b}")
    sys.exit(1)
print(f"PASS — {N[0]} 项断言全过")
