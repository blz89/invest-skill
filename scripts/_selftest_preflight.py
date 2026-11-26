#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_selftest_preflight.py：决策日第 3 步预检（只读四判据）回归。

不需要联网：用临时目录造假产物，覆盖 READY / 每一条判据失败的情况。
"""
from __future__ import annotations

import json
import atexit
import shutil
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import preflight  # noqa: E402

FAILED = []
TOTAL = 0


def check(name, ok, extra=""):
    global TOTAL
    TOTAL += 1
    print(("  ✓ " if ok else "  ✗ ") + name + (f"｜{extra}" if extra and not ok else ""))
    if not ok:
        FAILED.append(name)


TD = "2026-11-20"


def plan_doc(**kw):
    d = {"capital": 50000, "data_as_of": TD, "generated_at": TD + "T19:00:00"}
    d.update(kw)
    return d


def funnel_doc(pool=300, cand=300, ok=290):
    return {"股票池": pool, "进入逐只取数": cand, "取数成功": ok}


def summary_doc(**kw):
    d = {"partial": False,
         "prefetch_data": {"rc": 0, "expected_trade_date": TD, "bar_ok": True,
                           "coverage_ok": True},
         "invest_data": {"rc": 0, "data_as_of": TD}}
    d.update(kw)
    return d


def state_doc(capital=50000.0, nav=None):
    return {"meta": {"capital": capital, "ledger": "50k"},
            "cash": 1000.0, "holdings": {}, "trades": [],
            "nav": nav or [{"date": TD, "nav": capital}], "orders": []}


def run_case(td=TD, plan=None, funnel=None, summary=None, state=None, logs_name=None):
    tmp = tempfile.mkdtemp(prefix="preflight_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    outdir = os.path.join(tmp, "out")
    logs = os.path.join(tmp, "logs")
    os.makedirs(outdir)
    os.makedirs(logs)
    if plan is not None:
        with open(os.path.join(outdir, "stage3_plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
    if funnel is not None:
        with open(os.path.join(outdir, "stage1_funnel.json"), "w", encoding="utf-8") as f:
            json.dump(funnel, f, ensure_ascii=False)
    if summary is not None:
        with open(os.path.join(logs, (logs_name or f"summary_{td}.json")), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
    sp = os.path.join(tmp, "state.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(state if state is not None else state_doc(), f, ensure_ascii=False)
    return preflight.collect(outdir, logs, sp, td), tmp


def items_map(items):
    return {n: (ok, det) for n, ok, det in items}


print("=== 1. 四条判据各自的正例 ===")
items, _ = run_case(plan=plan_doc(), funnel=funnel_doc(), summary=summary_doc())
m = items_map(items)
check("正常数据 → 四条全过", all(ok for _, ok, _ in items), str(items))
check("数据日期判据名", "数据日期" in m and m["数据日期"][0])
check("本金判据接受可投资本", preflight.check_capital(plan_doc(), 50000, 50018.0)[0]
      and preflight.check_capital(plan_doc(capital=50018), 50000, 50018.0)[1])

print("=== 2. 判据 1：数据日期 ===")
check("无计划 → FAIL", not preflight.check_plan(None, TD)[1])
check("data_as_of 缺失 → FAIL", not preflight.check_plan(plan_doc(data_as_of=""), TD)[1])
check("data_as_of 不是最近交易日 → FAIL",
      not preflight.check_plan(plan_doc(data_as_of="2026-11-19"), TD)[1])
check("data_as_of 正确 → PASS", preflight.check_plan(plan_doc(), TD)[1])

print("=== 3. 判据 2：本金一致 ===")
check("计划=本金 → PASS", preflight.check_capital(plan_doc(), 50000, 50018.0)[1])
check("计划=可投资本（差 ≤1）→ PASS",
      preflight.check_capital(plan_doc(capital=50018), 50000, 50018.0)[1])
check("计划≠本金也≠可投资本 → FAIL",
      not preflight.check_capital(plan_doc(capital=100000), 50000, 50018.0)[1])
check("计划缺 capital → FAIL", not preflight.check_capital({"capital": 0}, 50000, 50018.0)[1])

print("=== 4. 判据 3：取数成功率 ===")
check("100% → PASS", preflight.check_funnel(funnel_doc())[1])
check("进入逐只取数 85% → FAIL",
      not preflight.check_funnel(funnel_doc(pool=300, cand=255, ok=255))[1])
check("取数成功 80% → FAIL",
      not preflight.check_funnel(funnel_doc(pool=300, cand=300, ok=240))[1])
check("股票池 0 → FAIL", not preflight.check_funnel(funnel_doc(pool=0))[1])
check("漏斗缺失 → FAIL", not preflight.check_funnel(None)[1])

print("=== 5. 判据 4：日摘要 ===")
check("完整摘要 → PASS", preflight.check_summary(summary_doc(), TD)[1])
check("partial=true → FAIL", not preflight.check_summary(summary_doc(partial=True), TD)[1])
check("prefetch rc≠0 → FAIL",
      not preflight.check_summary(
          summary_doc(prefetch_data={"rc": 1, "expected_trade_date": TD}), TD)[1])
check("invest rc≠0 → FAIL",
      not preflight.check_summary(summary_doc(invest_data={"rc": 2, "data_as_of": TD}), TD)[1])
check("data_as_of 落后 → FAIL",
      not preflight.check_summary(summary_doc(invest_data={"rc": 0, "data_as_of": "2026-11-19"}), TD)[1])
check("expected_trade_date 对不上 → FAIL",
      not preflight.check_summary(
          summary_doc(prefetch_data={"rc": 0, "expected_trade_date": "2026-11-19"}), TD)[1])
check("bar_ok=false → FAIL",
      not preflight.check_summary(
          summary_doc(prefetch_data={"rc": 0, "expected_trade_date": TD, "bar_ok": False}), TD)[1])
check("coverage_ok=false → FAIL",
      not preflight.check_summary(
          summary_doc(prefetch_data={"rc": 0, "expected_trade_date": TD,
                                     "coverage_ok": False}), TD)[1])
check("摘要缺失 → FAIL", not preflight.check_summary(None, TD)[1])

print("=== 6. collect：摘要文件按最近交易日命名 ===")
items, _ = run_case(plan=plan_doc(), funnel=funnel_doc(), summary=summary_doc(),
                    logs_name="summary_2026-11-19.json")
check("只有前一日摘要 → 日摘要 FAIL",
      items_map(items)["日摘要"][0] is False, str(items))

print("=== 7. collect：账本读不出 → 追加 FAIL，不抛异常 ===")
tmp = tempfile.mkdtemp(prefix="preflight_bad_")
atexit.register(shutil.rmtree, tmp, ignore_errors=True)
outdir = os.path.join(tmp, "out")
os.makedirs(outdir)
items = preflight.collect(outdir, os.path.join(tmp, "logs"),
                          os.path.join(tmp, "no_such_state.json"), TD)
m = items_map(items)
check("账本读不出 → 账本 FAIL", "账本可读" in m and m["账本可读"][0] is False)
check("其余判据仍全部返回", len(items) == 5, str(len(items)))

print()
print(f"断言：{TOTAL} 项｜通过 {TOTAL - len(FAILED)} 项｜失败 {len(FAILED)} 项")
if FAILED:
    for f in FAILED:
        print("  - " + f)
    raise SystemExit(1)
print("OK 全部通过")

