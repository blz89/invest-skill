# -*- coding: utf-8 -*-
"""自检：收益拆解（权益 / 场外 R2 / 现金三层）——breakdown.py 与周报/月报接线。

跑法：python scripts/_selftest_breakdown.py     （不联网、不读写真实账本）
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import breakdown  # noqa: E402
import report_html  # noqa: E402

N = [0]
BAD = []


def check(ok, msg):
    N[0] += 1
    if ok:
        print(f"  OK   {msg}")
    else:
        BAD.append(msg)
        print(f"  FAIL {msg}")


# 构造自洽账本：本金 1 万；权益成本 1,000（市值 1,200）；场外投入 1,000（市值 1,050）；
# 现金 8,020（基准 8,000，现金净收益 +20）；在途逆回购利息 5 − 费 1 = 4
STATE = {
    "meta": {"capital": 10000.0},
    "cash": 8020.0,
    "holdings": {"A": {"shares": 100, "cost_basis": 10.0, "last_close": 12.0}},
    "otc_holdings": {"OTC:X": {"amount": 1000.0, "acc_nav": 1.0, "nav": 1.05}},
    "repo_open": {"interest": 5.0, "fee": 1.0},
    "nav": [{"date": "2026-11-06", "nav": 10270.0, "cash": 8020.0,
             "market_value": 1200.0, "otc_value": 1050.0}],
}

print("1) compute：三层成本/市值/盈亏")
bd = breakdown.compute(STATE)
check(abs(bd["equity"]["cost"] - 1000.0) < 1e-9, "权益成本 = 1,000")
check(abs(bd["equity"]["value"] - 1200.0) < 1e-9, "权益市值 = 1,200（优先 nav.market_value）")
check(abs(bd["equity"]["pnl"] - 200.0) < 1e-9, "权益盈亏 = +200")
check(abs(bd["otc"]["cost"] - 1000.0) < 1e-9, "场外成本 = 1,000")
check(abs(bd["otc"]["value"] - 1050.0) < 1e-9, "场外市值 = 1,050（金额法累计净值）")
check(abs(bd["otc"]["pnl"] - 50.0) < 1e-9, "场外盈亏 = +50")
check(abs(bd["cash"]["baseline"] - 8000.0) < 1e-9, "现金基准 = 本金 − 权益成本 − 场外成本")
check(abs(bd["cash"]["pnl"] - 20.0) < 1e-9, "现金层盈亏 = +20（残差）")
check(abs(bd["cash"]["in_transit"] - 4.0) < 1e-9, "在途逆回购利息 = 4（单列不并入）")
check(abs(bd["total_pnl"] - 270.0) < 1e-9, "三层合计 = +270")
check(abs(bd["nav_pnl"] - 270.0) < 1e-9, "净值−本金 = 270，与三层合计一致")
check(bd["ok"] is True and abs(bd["diff"]) < 1e-9, "自洽校验 ok（diff=0）")
check(abs(bd["equity"]["share"] - 200.0 / 270.0) < 1e-3, "权益占总收益 = 200/270")
check(bd["date"] == "2026-11-06", "日期取 nav 最后一点")

print("2) 渲染：周报 md / HTML / 月报可复用")
md = breakdown.render_md(bd)
check("## 收益拆解（权益 / 场外 R2 / 现金三层）" in md, "md 标题")
check("权益层" in md and "场外 R2" in md and "现金层" in md, "md 三层行齐全")
check("270.00" in md, "md 合计金额")
check("在途逆回购利息 4.00 元" in md, "md 在途提示")
html = breakdown.render_html(bd)
check("<h2>收益拆解" in html and "<table>" in html and "270.00" in html, "html 表格与金额")
check(breakdown.render_md({}) == "" and breakdown.render_html({}) == "", "空拆解不渲染")
check("收益拆解" in report_html._section_breakdown(STATE), "report_html 周报小节接线")
check("未生成" in report_html._section_breakdown([]) or
      report_html._section_breakdown([]) == "", "坏输入不炸周报（空/提示）")

print("3) 边界：空账本 / 无持仓 / 数据不自洽")
empty = breakdown.compute({"meta": {"capital": 0.0}, "cash": 0.0})
check(empty["total_pnl"] == 0 and empty["capital"] == 0, "空账本全 0 不崩")
check(breakdown.compute({}) == {} and breakdown.compute(None) == {}, "完全空输入返回 {}")
check("数据可能不自洽" not in breakdown.note_text(empty), "空 state 不误报不自洽")
bad_state = dict(STATE)
bad_state["cash"] = 9999.0  # 人为打破恒等式
bad = breakdown.compute(bad_state)
check(bad["ok"] is False and "数据可能不自洽" in breakdown.note_text(bad), "差值>1 元给 warn")

print("=" * 78)
if BAD:
    print(f"FAIL 共 {len(BAD)} 项失败 / {N[0]} 项")
    for b in BAD:
        print("  - " + b)
    sys.exit(1)
print(f"OK 全部通过（{N[0]} 项断言）")
