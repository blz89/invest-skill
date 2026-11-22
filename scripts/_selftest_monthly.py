# -*- coding: utf-8 -*-
"""自检：月报「策略复盘 + 技能优化」链路（monthly.py / report_html.py / team.retro_missing）。

跑法：python scripts/_selftest_monthly.py     （不联网、不读写真实账本）
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import monthly  # noqa: E402
import paper  # noqa: E402
import report_html  # noqa: E402
import team  # noqa: E402

N = [0]
BAD = []


def check(msg, ok):
    N[0] += 1
    if ok:
        print(f"  OK   {msg}")
    else:
        BAD.append(msg)
        print(f"  FAIL {msg}")


RETRO = """# T10 复盘

## 五、五层
L1 归因：现金 carry 55 元。

## 六、上月策略复盘
- 证据：月末仓位 18.7%，权益预算未用满。
### 自动诊断信号
- 场外腿在挂 3 张。

## 七、技能优化清单
- [x] `otc_book.py:64` 加补建闸门 → 老账本场外腿空着 → `_selftest_otc_book.py`
- [ ] `weekly.py:930` HTML 订单表加场外行 → 报告不可见 → `_selftest_otc_book.py`
- [ ] `README.md:238` 版本号过期 → 用户读不到真实版本 → 人工核对

## 八、其他
不该被摘进去的内容。
"""

print("=" * 78)
print("1) _grab_section：按标题摘小节")
_s = monthly._grab_section(RETRO, ("策略复盘",))
check("策略复盘小节非空" if _s else "策略复盘小节非空", bool(_s))
check("含证据正文", "证据：月末仓位" in _s)
check("含子标题", "### 自动诊断信号" in _s)
check("不串到下一节", "技能优化清单" not in _s and "不该被摘进去" not in _s)
_o = monthly._grab_section(RETRO, ("技能优化",))
check("技能优化小节含勾选项", "- [x]" in _o and "- [ ]" in _o)
check("技能优化不串到 八、其他", "不该被摘进去" not in _o)
check("找不到的标题 → 空", monthly._grab_section(RETRO, ("不存在的节",)) == "")
check("空文本 → 空", monthly._grab_section("", ("策略复盘",)) == "")

print("2) read_retro：从文件摘两节")
tmp = tempfile.mkdtemp(prefix="selftest_monthly_")
p = os.path.join(tmp, "retro.md")
with open(p, "w", encoding="utf-8") as f:
    f.write(RETRO)
r = monthly.read_retro(p)
check("exists=True", r["exists"] is True)
check("strategy 非空", "月末仓位" in r["strategy"])
check("optimization 非空", "- [x]" in r["optimization"] and "- [ ]" in r["optimization"])
r2 = monthly.read_retro(os.path.join(tmp, "nope.md"))
check("文件不存在时不炸", r2["exists"] is False and r2["strategy"] == "" and r2["optimization"] == "")
r3 = monthly.read_retro()
check("默认路径可调用（不要求文件存在）", isinstance(r3, dict) and "strategy" in r3)

print("3) opt_stats：清单完成度")
st = monthly.opt_stats(_o)
check("total=3" if st["total"] == 3 else f"total=3（实际 {st['total']}）", st["total"] == 3)
check("done=1" if st["done"] == 1 else f"done=1（实际 {st['done']}）", st["done"] == 1)
check("空文本 0/0", monthly.opt_stats("") == {"total": 0, "done": 0})

print("4) ledger_diagnostics：自动诊断信号（只报事实）")
s = paper.new_state(50000.0)
meta = s["meta"]
meta["baseline_date"] = "2026-10-28"
s["nav"] = [{"date": "2026-10-28", "nav": 50000.0}, {"date": "2026-10-30", "nav": 50000.5}]
s["orders"] = [
    {"code": "000999", "name": "A", "action": "buy", "order_type": "buy_open",
     "effective_from": "2026-10-29", "status": "filled"},
    {"code": "002648", "name": "B", "action": "buy", "order_type": "buy_limit",
     "effective_from": "2026-10-29", "status": "expired"},
    {"code": "002001", "name": "C", "action": "buy", "order_type": "buy_limit",
     "effective_from": "2026-10-29", "status": "expired"},
]
diag = monthly.ledger_diagnostics(s, "2026-10",
                                  {"fees_pct": 0.001, "fees": 50.0, "weight_end": 0.2,
                                   "stop_outs": 0, "cash_yield": 0.0, "repo_net": 0.0,
                                   "repo_covered_days": 0})
_txt = " ".join(t for _l, t in diag)
check("结算点少 → 告警", any(l == "warn" and "净值结算点" in t for l, t in diag))
check("挂单状态统计", "成交 1" in _txt and "失效 2" in _txt)
check("无效挂单偏多 → 告警", any(l == "warn" and "无效挂单" in t for l, t in diag))
check("场外腿为空 → 告警", any(l == "warn" and "场外 R2 腿为空" in t for l, t in diag))
check("现金层零利息 → 告警", any(l == "warn" and "现金层利息为 0" in t for l, t in diag))
check("每条信号二元组", all(isinstance(x, tuple) and len(x) == 2 for x in diag))

print("5) to_md：月报第一、二节是复盘与技能优化")
data = {"ledgers": {}, "scorecard": "",
        "retro": {"exists": True, "strategy": "## 六、上月策略复盘\n- 证据A",
                  "optimization": "## 七、技能优化清单\n- [x] a\n- [ ] b"},
        "diagnostics": [{"label": "¥50,000", "items": [("warn", "测试告警")]}],
        "opt_stats": {"total": 2, "done": 1},
        "start": "2026-10-01", "end": "2026-10-31"}
md = monthly.to_md("2026-10", data, "", "2026-10-01", "2026-10-31")
check("md 含一、上月策略复盘", "## 一、上月策略复盘与改进" in md)
check("md 摘入策略复盘文本", "证据A" in md)
check("md 含二、技能优化", "## 二、技能优化（A 类已执行 / B 类待拍板）" in md)
check("md 含完成度 1/2", "清单完成度：**1/2**" in md)
check("md 含自动诊断信号", "测试告警" in md)
check("md 含三、数据统计", "## 三、数据统计" in md)
md0 = monthly.to_md("2026-10", {"ledgers": {}, "scorecard": ""}, "", "2026-10-01", "2026-10-31")
check("无 retro 时给明确提示（不伪造）", "未找到" in md0 and "策略复盘" in md0 and "技能优化" in md0)

# 月度收益与总收益必须同时出现
data2 = dict(data, ledgers={"50k": {
    "label": "5 万", "mode": "regime_change",
    "month": {
        "nav_start": 50000.0, "nav_end": 50100.0, "pnl_month": 100.0,
        "ret_month": 0.002, "total_return": 0.004, "total_pnl": 200.0,
        "bench_month": -0.01, "excess": 0.012, "mdd": -0.001,
        "trades": 1, "buys": 1, "sells": 0, "fees": 5.0, "fees_pct": 0.0001,
        "cash_yield": 10.0, "equity_pnl": 90.0, "weight_end": 0.2,
        "investable": 50200.0, "capital": 50000.0, "gain": 200.0, "stop_outs": 0,
        "repo_ops": 0, "repo_covered_days": 0, "repo_interest": 0.0,
        "repo_fees": 0.0, "repo_net": 0.0, "repo_effective": 0.0, "risk": {},
        "net": {"gross_profit": 200.0, "token_spent": 1.0, "net_profit": 199.0,
                "net_rate": 0.00398, "grade": "B-", "reward_pending": True,
                "reward_note": "评估期未到"},
    }, "trades": [], "by_symbol": [], "diagnostics": [],
}})
md2 = monthly.to_md("2026-10", data2, "", "2026-10-01", "2026-10-31")
check("md 有月度收益", "月度收益" in md2)
check("md 有总收益（累计）", "总收益（累计" in md2)
check("md 三、数据统计含总收益列", "| 总收益（累计） |" in md2)

# 5b) 策略可行性判定（五问）接入 md/HTML
data_v = dict(data2, viability="① 样本：3 周、0 平仓\n**结论：继续**（附最强反方证据）")
mdv = monthly.to_md("2026-10", data_v, "", "2026-10-01", "2026-10-31")
check("md 含五、策略可行性判定", "## 五、策略可行性判定（五问）" in mdv)
check("md 摘入 viability 正文", "结论：继续" in mdv)
check("缺 viability → 占位提示", "viability_2026-10.md" in md2 and "策略还可行吗" in md2)
# 5c) 本年计划对齐（annual_plan.md：月报只读、逐条回答每月必答）
_ap = "## 1. 年度目标\n- 净收益率 ≥ 0\n## 4. 每月必答\n- 仓位用满了吗？"
md_ap = monthly.to_md("2026-10", dict(data2, annual_plan=_ap), "", "2026-10-01", "2026-10-31")
check("md 含六、本年计划对齐", "## 六、本年计划对齐" in md_ap)
check("md 摘入 annual_plan 正文", "每月必答" in md_ap)
check("md 年度计划子标题降级", "\n### 1. 年度目标\n" in md_ap and "\n## 1. 年度目标\n" not in md_ap)
check("缺 annual_plan → 写无年度计划", "本年度无年度计划" in md2)
check("md 下月计划顺延为八", "## 八、下月计划" in md2)

print("6) build_monthly_report：HTML 第一、二节可见")
html = report_html.build_monthly_report(month="2026-10", data=data)
check("HTML 含一、上月策略复盘与改进", "一、上月策略复盘与改进" in html)
check("HTML 摘入复盘文本", "证据A" in html)
check("HTML 含二、技能优化", "二、技能优化（A 类已执行 / B 类待拍板）" in html)
check("HTML 含完成度 1/2", "清单完成度：1/2" in html)
check("HTML 含自动诊断信号", "测试告警" in html)
check("HTML 含三、数据统计", "三、数据统计" in html)
html2 = report_html.build_monthly_report(month="2026-10", data=data2)
check("HTML 有月度收益", "月度收益" in html2)
check("HTML 有总收益（累计）", "总收益（累计" in html2)
htmlv = report_html.build_monthly_report(month="2026-10", data=data_v)
check("HTML 含策略可行性判定标题", "策略可行性判定（五问）" in htmlv)
check("HTML 摘入 viability 正文", "结论：继续" in htmlv)
check("HTML 缺 viability → warn 卡", "viability_" in html2 and "月报必须回答" in html2)
check("HTML 含本年计划对齐", "本年计划对齐" in html2 and "本年度无年度计划" in html2)
_html_ap = report_html.build_monthly_report(month="2026-10", data=dict(data2, annual_plan=_ap))
check("HTML 摘入 annual_plan", "每月必答" in _html_ap and "本年计划对齐" in _html_ap)
html0 = report_html.build_monthly_report(month="2026-10", data={"ledgers": {}})
check("无 retro 时 HTML 给提示不伪造", "未找到" in html0 and "上月策略复盘" in html0)
_x = report_html.build_monthly_report(
    month="2026-10",
    data={"ledgers": {}, "retro": {"exists": True, "strategy": "## 策略复盘\n<script>x</script>",
                                   "optimization": ""}})
check("复盘文本被转义（不执行注入）", "&lt;script&gt;" in _x and "<script>x</script>" not in _x)

print("7) team.retro_missing：T10 验收必须的两节")
check("两节齐全 → 空", team.retro_missing(RETRO) == [])
check("缺技能优化", team.retro_missing("## 六、上月策略复盘\n- x") == ["技能优化"])
check("全缺 → 两个", len(team.retro_missing("")) == 2)

print("8) 年度结算：奖励划出、本金重置、链式总收益、幂等")
# settle_year 会经 billing.add_bonus 写预算/流水/周期文件；测试必须先把这三条路径
# 重定向到临时目录，否则每跑一次自检就给生产 out/budget.json 追加一笔「年报结算奖励」
# （2026-11-22 事故：30+ 次自检把 bonus 累到 5300 元，评估期前本应恒为 0）。
import billing as _bill_mod  # noqa: E402

_bill_saved = (_bill_mod.BUDGET_JSON, _bill_mod.BILLING_JSONL, _bill_mod.CYCLE_FILE)
_bill_mtime = os.path.getmtime(_bill_saved[0]) if os.path.exists(_bill_saved[0]) else None
_bill_mod.BUDGET_JSON = os.path.join(tmp, "budget.json")
_bill_mod.BILLING_JSONL = os.path.join(tmp, "billing.jsonl")
_bill_mod.CYCLE_FILE = os.path.join(tmp, "billing_cycle.json")
_tmp_state = os.path.join(tmp, "state_annual.json")
st_yr = {"meta": {"capital": 50000.0, "nav": []}, "cash": 1000.0,
         "holdings": {}, "trades": [], "orders": [], "events": [], "nav": []}
paper.save(st_yr, _tmp_state)
_m = {"nav_start": 50000.0, "nav_end": 50500.0, "ret_year": 0.01, "pnl_year": 500.0,
      "net": {"net_profit": 300.0, "net_rate": 0.006, "net_rate_ann": 0.006,
              "grade": "C", "reward_cny": 0.0, "reward_pending": True}}
r0 = monthly.settle_year(st_yr, 2026, _m, _tmp_state, ledger="50k", as_of="2027-01-01")
check("首次结算 already=False", r0["already"] is False)
check("本金重置为年末净值（奖励 0）", abs(r0["record"]["capital_after"] - 50500.0) < 1e-9)
check("链式总收益 = 年度收益", abs(r0["record"]["total_return"] - 0.01) < 1e-9)
check("cum_index 写入 1.01", abs(st_yr["meta"]["cum_index"] - 1.01) < 1e-9)
check("年度基线写入", st_yr["meta"]["annual_baseline"]["capital"] == 50500.0)
check("幂等：第二次已结算", monthly.settle_year(st_yr, 2026, _m, _tmp_state,
                                               ledger="50k")["already"] is True)

st_w = {"meta": {"capital": 50000.0, "nav": []}, "cash": 50500.0,
        "holdings": {}, "trades": [], "orders": [], "events": [], "nav": []}
path_w = _tmp_state + ".w.json"
paper.save(st_w, path_w)
_w = {"nav_start": 50000.0, "nav_end": 50500.0, "ret_year": 0.01, "pnl_year": 500.0,
      "net": {"net_profit": 300.0, "net_rate": 0.006, "net_rate_ann": 0.006,
              "grade": "B-", "reward_cny": 200.0, "reward_pending": False}}
monthly.settle_year(st_w, 2026, _w, path_w, ledger="50k", as_of="2027-01-01")
check("奖励从现金划出", abs(st_w["cash"] - 50300.0) < 1e-9)
check("本金 = 年末净值 − 奖励", abs(st_w["meta"]["capital"] - 50300.0) < 1e-9)
check("withdrawn_total 累计", abs(st_w["meta"]["withdrawn_total"] - 200.0) < 1e-9)
check("奖励不改变链式收益", abs((paper.portfolio(st_w, {})["total_return"] or 0) - 0.01) < 1e-6)
check("自检不写生产预算", os.path.getmtime(_bill_saved[0]) == _bill_mtime
      if os.path.exists(_bill_saved[0]) else True)

# 恢复生产计费路径
(_bill_mod.BUDGET_JSON, _bill_mod.BILLING_JSONL, _bill_mod.CYCLE_FILE) = _bill_saved

# 年度策略可行性判定（五问）
_avd = {"ledgers": {}, "year": 2026, "start": "2026-01-01", "end": "2026-12-31",
        "viability": "① 样本：一年\n**结论：微调执行层**"}
_amd = monthly.to_md_annual(2026, _avd, "", "2026-01-01", "2026-12-31")
check("年报 md 含五、策略可行性判定", "## 五、策略可行性判定（五问）" in _amd)
check("年报 md 摘入 viability", "结论：微调执行层" in _amd)
check("年报 md 记分卡改为七", "## 七、记分卡" in _amd)
check("年报 md 含选股评分体检节", "## 六、选股评分体检" in _amd)
_avd2 = dict(_avd, score_health="## 选股评分体检\n\n- 面板：3 周（样本不足，维持现状）")
_amd2 = monthly.to_md_annual(2026, _avd2, "", "2026-01-01", "2026-12-31")
check("年报 md 摘入评分体检", "维持现状" in _amd2)
check("年报评分体检标题只出现一次", _amd2.count("选股评分体检") == 1)
_ah2 = report_html.build_annual_report(year=2026, data=_avd2)
check("年报 HTML 含选股评分体检", "选股评分体检" in _ah2 and "维持现状" in _ah2)
_ah = report_html.build_annual_report(year=2026, data=_avd)
check("年报 HTML 含策略可行性判定", "策略可行性判定（五问）" in _ah)

print("9) 当年 token 实花：只算落在该年的 runs")
import billing as _billing_mod  # noqa: E402
_orig_bs = _billing_mod.budget_state
_billing_mod.budget_state = lambda *a, **k: {"runs": [
    {"ended_at": "2026-11-01T10:00:00", "cost_cny": 1.5},
    {"ended_at": "2025-12-31T10:00:00", "cost_cny": 9.0},
    {"started_at": "2026-01-02T10:00:00", "cost_cny": 0.5}]}
try:
    check("按自然年求和", abs(monthly._year_token_spent("50k", 2026) - 2.0) < 1e-9)
finally:
    _billing_mod.budget_state = _orig_bs

print("=" * 78)
if BAD:
    print(f"FAIL 共 {len(BAD)} 项失败 / {N[0]} 项")
    for b in BAD:
        print("  - " + b)
    sys.exit(1)
print(f"OK 全部通过（{N[0]} 项断言）")
