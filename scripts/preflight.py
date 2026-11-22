#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""决策日第 3 步预检（只读、不取数）：确认「工作日 19:00 取的那份数据」齐、新、口径一致。

为什么单独做一个脚本：这一步原本是人工读三个 JSON 的四个字段，键还分两层
（`invest_data.data_as_of` / `prefetch_data.expected_trade_date`），看错一次就会
拿旧数据或半截数据做决策。把它做成一条命令，判据固定、可测，决策日只需要看结论。

四条判据（全过 READY；任一不过 NOT-READY，决策日停下、不现场补取）：
1. `out/stage3_plan.json` 的 `data_as_of` = 最近交易日；
2. 本金一致：计划 `capital` = 账本本金，或 = 可投资本（差 ≤1 元，利润再投资口径）；
3. 漏斗取数成功率 ≥90%：`进入逐只取数/股票池` 且 `取数成功/进入逐只取数`；
4. `logs/summary_<日期>.json` 是最近交易日的：`partial=false`、prefetch/invest `rc=0`、
   `invest_data.data_as_of=最近交易日`、`bar_ok/coverage_ok` 不为 false。

用法：
    python scripts/preflight.py                         # 默认账本 50k、今天
    python scripts/preflight.py --today 2026-11-21      # 复跑某天
    python scripts/preflight.py --out-dir D:\\tmp\\out --logs-dir D:\\tmp\\logs --state D:\\tmp\\s.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import paper  # noqa: E402

# 本脚本在 scripts/，技能根目录是上一级；STOCK_HOME 可用环境变量覆盖（与 config.py 同口径）
STOCK_HOME = os.environ.get("STOCK_HOME") or os.path.join(os.path.dirname(HERE), "stock")
LOGS_DIR = os.environ.get("STOCK_LOGS_DIR") or os.path.join(STOCK_HOME, "data", "logs")

MIN_RATE = 0.90


def _load(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_plan(plan, last_td: str) -> tuple:
    """判据 1：计划存在且 data_as_of 是最近交易日。"""
    if not isinstance(plan, dict):
        return ("数据日期", False, "out/stage3_plan.json 不存在或不可解析（19:00 管线没跑完？）")
    dao = str(plan.get("data_as_of") or "")[:10]
    if not dao:
        return ("数据日期", False, "stage3_plan.json 缺 data_as_of（旧产物，重跑 run.py --only-plan）")
    if dao != last_td:
        return ("数据日期", False, f"data_as_of={dao}，最近交易日 {last_td}（产物是旧的）")
    return ("数据日期", True, f"data_as_of={dao}")


def check_capital(plan, state_cap, invest_cap) -> tuple:
    """判据 2：计划本金 = 账本本金，或 = 可投资本（利润再投资，差 ≤1 元）。"""
    if not isinstance(plan, dict):
        return ("本金一致", False, "没有计划，无法核对本金")
    pc = float(plan.get("capital") or 0.0)
    if pc <= 0:
        return ("本金一致", False, "stage3_plan.json 缺 capital")
    if state_cap and abs(pc - float(state_cap)) <= 1.0:
        return ("本金一致", True, f"计划 {pc:,.0f} = 账本本金 {float(state_cap):,.0f}")
    if invest_cap and abs(pc - float(invest_cap)) <= 1.0:
        return ("本金一致", True,
                f"计划 {pc:,.0f} = 可投资本 {float(invest_cap):,.0f}（利润再投资口径）")
    return ("本金一致", False,
            f"计划 {pc:,.0f} ≠ 账本本金 {float(state_cap or 0):,.0f}、也 ≠ 可投资本 "
            f"{float(invest_cap or 0):,.0f}（产物可能属于别的本金/账本）")


def check_funnel(funnel) -> tuple:
    """判据 3：漏斗取数成功率 ≥90%（两段：进逐只取数、取数成功）。"""
    if not isinstance(funnel, dict):
        return ("取数成功率", False, "out/stage1_funnel.json 不存在或不可解析")
    try:
        pool = int(funnel.get("股票池") or 0)
        cand = int(funnel.get("进入逐只取数") or 0)
        ok = int(funnel.get("取数成功") or 0)
    except (TypeError, ValueError):
        return ("取数成功率", False, "stage1_funnel.json 计数不是整数")
    if pool <= 0:
        return ("取数成功率", False, "漏斗股票池为 0")
    r_cand = cand / pool
    r_ok = (ok / cand) if cand else 0.0
    detail = (f"进入逐只取数 {cand}/{pool}（{r_cand:.1%}）/ "
              f"取数成功 {ok}/{cand}（{r_ok:.1%}）")
    return ("取数成功率", r_cand >= MIN_RATE and r_ok >= MIN_RATE, detail)


def check_summary(summary, last_td: str) -> tuple:
    """判据 4：最近交易日那份 summary 完整（partial=false、rc=0、日期对得上）。"""
    if not isinstance(summary, dict):
        return ("日摘要", False, f"logs/summary_{last_td}.json 不存在（19:00 任务没跑完？）")
    if summary.get("partial"):
        return ("日摘要", False, "partial=true：管线只跑了一半，产物可能缺文件")
    pf = summary.get("prefetch_data") or {}
    iv = summary.get("invest_data") or {}
    if int(pf.get("rc") or 0) != 0:
        return ("日摘要", False, f"prefetch rc={pf.get('rc')}（取数失败，看 summary 的 error_tail）")
    if int(iv.get("rc") or 0) != 0:
        return ("日摘要", False, f"invest 管线 rc={iv.get('rc')}（run.py 失败）")
    dao = str(iv.get("data_as_of") or "")[:10]
    if dao and dao != last_td:
        return ("日摘要", False, f"invest_data.data_as_of={dao}，最近交易日 {last_td}")
    exp = str(pf.get("expected_trade_date") or "")[:10]
    if exp and exp != last_td:
        return ("日摘要", False, f"prefetch expected_trade_date={exp}，最近交易日 {last_td}")
    if pf.get("bar_ok") is False:
        return ("日摘要", False, "bar_ok=false（日线未就绪）")
    if pf.get("coverage_ok") is False:
        return ("日摘要", False, "coverage_ok=false（缓存覆盖不足）")
    return ("日摘要", True,
            f"partial=false / prefetch rc=0 / invest rc=0 / data_as_of={dao or '未写'}")


def collect(out_dir: str, logs_dir: str, state_path: str, last_td: str) -> list:
    """跑四条判据，返回 [(名称, ok, 说明)]。全部只读。"""
    plan = _load(os.path.join(out_dir, "stage3_plan.json"))
    funnel = _load(os.path.join(out_dir, "stage1_funnel.json"))
    summary = _load(os.path.join(logs_dir, f"summary_{last_td}.json"))
    try:
        state = paper.load(state_path)
    except Exception:                                # noqa: BLE001 - 账本读不出来单独报
        state = None
    meta = (state or {}).get("meta") or {}
    state_cap = float(meta.get("capital") or 0.0)
    try:
        invest_cap = float(paper.investable_capital(state)["investable"]) if state else 0.0
    except Exception:                                # noqa: BLE001
        invest_cap = 0.0
    items = [check_plan(plan, last_td), check_capital(plan, state_cap, invest_cap),
             check_funnel(funnel), check_summary(summary, last_td)]
    if state is None:
        items.append(("账本可读", False, f"读不出账本 {state_path}"))
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="决策日第 3 步预检（只读、不取数）")
    ap.add_argument("--ledger", default="50k", choices=sorted(config.PAPER["ledgers"].keys()))
    ap.add_argument("--out-dir", default=config.OUT_DIR)
    ap.add_argument("--logs-dir", default=LOGS_DIR)
    ap.add_argument("--state", default=None, help="账本文件（默认按 --ledger 取配置）")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD，默认今天")
    args = ap.parse_args(argv)

    cfg = config.PAPER["ledgers"][args.ledger]
    state_path = args.state or cfg["state_file"]
    try:
        import patrol  # 复用巡检的交易日历口径（含未来交易日，失败退化为跳周末）
        last_td = patrol.last_trading_day(args.today)
    except Exception as e:                          # noqa: BLE001 - 日历坏了也要能报
        print(f"[preflight] 取不到最近交易日：{type(e).__name__}: {e}")
        return 1
    items = collect(args.out_dir, args.logs_dir, state_path, last_td)
    print(f"[preflight] 账本 {args.ledger}｜最近交易日 {last_td}")
    for name, ok, detail in items:
        print(f"  {'✓' if ok else '✗'} {name}：{detail}")
    bad = [x for x in items if not x[1]]
    if bad:
        print(f"[preflight] 结论：NOT-READY（{len(bad)} 项不过）——停，不现场补取、不拿旧数据硬跑；"
              f"先查 19:00 的 stock prefetch 与 run.py")
        return 1
    print("[preflight] 结论：READY（数据齐、新、口径一致，可进第 4 步）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
