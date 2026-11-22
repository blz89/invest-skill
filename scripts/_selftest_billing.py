# -*- coding: utf-8 -*-
"""billing.py 的快照差额计费自检（离线：假用量记录 + 临时目录）。

口径（用户定）：任务花费 = 收尾时 usage 记录累计值 − --task-begin 时写下的快照。
子代理会话落在同一份用量记录里，天然包含；缺快照时回落窗口求和。

跑法：python scripts/_selftest_billing.py（不联网、不碰正式账本与 out/）
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import billing

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def ev(ts, cost, tokens):
    return {"ts": ts, "day": "2026-11-20", "model": "deepseek-flash",
            "cost": cost, "tokens": tokens}


def data(*events):
    return {"events": list(events)}


def main():
    print("billing：快照差额计费")

    d0 = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
    snap = billing.usage_snapshot(data=d0)
    check("累计快照-调用数", snap["n_calls"] == 2, str(snap))
    check("累计快照-token", snap["tokens"] == 3500, str(snap))
    check("累计快照-金额", abs(snap["cost_cny"] - 0.35) < 1e-9, str(snap))

    cut = billing.usage_snapshot(at_ms=1500, data=d0)
    check("at_ms 只算之前的事件", cut["n_calls"] == 1 and abs(cut["cost_cny"] - 0.10) < 1e-9,
          str(cut))

    later = billing.usage_snapshot(data=data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500),
                                             ev(3000, 0.05, 500)))
    delta = billing.snapshot_delta(snap, later)
    check("差额-调用数", delta["n_calls"] == 1, str(delta))
    check("差额-token", delta["tokens"] == 500, str(delta))
    check("差额-金额", abs(delta["cost_cny"] - 0.05) < 1e-9, str(delta))
    check("缺快照返回空", billing.snapshot_delta(None, later) == {}, "应返回 {}")

    tmp = tempfile.mkdtemp(prefix="bill_selftest_")
    saved = (billing.TASK_OPEN, billing.BILLING_JSONL, billing.BUDGET_JSON,
             billing.CYCLE_FILE, billing.load_usage)
    prod_cycle = saved[3]
    try:
        with open(prod_cycle, "r", encoding="utf-8") as f:
            prod_cycle_before = f.read()
    except OSError:
        prod_cycle_before = None
    try:
        billing.TASK_OPEN = os.path.join(tmp, "task_open.json")
        billing.BILLING_JSONL = os.path.join(tmp, "billing.jsonl")
        billing.BUDGET_JSON = os.path.join(tmp, "budget.json")
        # 必须在这里隔离：第一次 task_settle 也会推进周期终点（窗口分支），
        # 晚于任何 settle 才重定向会把生产 billing_cycle.json 推到「现在」。
        billing.CYCLE_FILE = os.path.join(tmp, "billing_cycle.json")
        state = {"data": data(ev(1000, 0.10, 1000))}
        billing.load_usage = lambda path=None: state["data"]

        doc = billing.task_begin(ledgers=["50k"], note="selftest")
        snap_doc = doc.get("snapshot") or {}
        check("开窗写快照", abs(float(snap_doc.get("cost_cny") or 0) - 0.10) < 1e-9,
              str(snap_doc))

        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        res = billing.task_settle(note="selftest", close=True)
        check("结算返回 ok", res.get("ok") is True, str(res))
        check("结算金额用差额", abs(float(res.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res.get("cost_cny")))
        check("结算标记 basis=snapshot",
              (res.get("window") or {}).get("basis") == "snapshot",
              str((res.get("window") or {}).get("basis")))

        with open(billing.BUDGET_JSON, "r", encoding="utf-8") as f:
            budget = json.load(f)
        spent = float(((budget.get("ledgers") or {}).get("50k") or {}).get("spent_cny") or 0)
        check("预算已花=差额", abs(spent - 0.25) < 1e-9, str(spent))

        with open(billing.BILLING_JSONL, "r", encoding="utf-8") as f:
            row = json.loads(f.read().strip().splitlines()[-1])
        check("流水 basis=snapshot", row.get("basis") == "snapshot", str(row.get("basis")))
        check("流水金额=差额", abs(float(row.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(row.get("cost_cny")))
        # 分项口径：同一窗口的逐轮事件差额（本用例 0.25）——与主口径金额分开记
        check("流水事件分项=逐轮差额", abs(float(row.get("events_cost_cny") or 0) - 0.25) < 1e-9,
              str(row.get("events_cost_cny")))

        # 旧 task_open.json（无 snapshot）→ 拒绝结算，不估算、不回落求和
        billing.TASK_OPEN = os.path.join(tmp, "task_open2.json")
        with open(billing.TASK_OPEN, "w", encoding="utf-8") as f:
            json.dump({"since_ms": 0, "ledgers": ["50k"], "begun_at": "x", "steps": []}, f)
        res2 = billing.task_settle(note="legacy", close=True)
        check("缺快照-拒绝结算", res2.get("ok") is False, str(res2))
        check("缺快照-不计费", float(res2.get("cost_cny") or 0) == 0.0,
              str(res2.get("cost_cny")))
        check("缺快照-说明原因", "快照" in str(res2.get("reason") or ""),
              str(res2.get("reason")))

        # ---- 记账口径：逐轮事件差额（账户观测只作参考字段）----
        def ledger(units, evs=None):
            return {"date": "2026-11-21", "lastBalance": 80.0,
                    "events": list(evs or []),
                    "accounting": {"active": "bk-CNY", "books": {"bk-CNY": {
                        "days": {"2026-11-21": {"day": "2026-11-21",
                                                 "debitUnits": units}},
                        "lastAt": 1795191654379}}}}

        billing.TASK_OPEN = os.path.join(tmp, "task_open3.json")
        state["data"] = ledger(14_000_000, [ev(1000, 0.10, 1000)])
        doc3 = billing.task_begin(ledgers=["50k"], note="acct")
        check("开窗写账户快照", int((doc3.get("account") or {}).get("units") or 0) == 14_000_000,
              str(doc3.get("account")))
        state["data"] = ledger(19_000_000, [ev(1000, 0.10, 1000), ev(2000, 0.25, 2500)])
        res3 = billing.task_settle(note="acct", close=True)
        w3 = res3.get("window") or {}
        check("记账用逐轮事件差额", abs(float(res3.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res3.get("cost_cny")))
        check("记账口径 basis=snapshot", w3.get("basis") == "snapshot", str(w3.get("basis")))
        check("账户差额只作参考字段",
              abs(float((w3.get("account_delta") or {}).get("cny") or 0) - 0.05) < 1e-9,
              str(w3.get("account_delta")))

        billing.TASK_OPEN = os.path.join(tmp, "task_open4.json")
        state["data"] = ledger(14_000_000, [ev(1000, 0.10, 1000)])
        billing.task_begin(ledgers=["50k"], note="unobs")
        state["data"] = ledger(14_000_000, [ev(1000, 0.10, 1000), ev(2000, 0.25, 2500)])
        res4 = billing.task_settle(note="unobs", close=True)
        w4 = res4.get("window") or {}
        check("账户无新观测也照常记账（不告警）",
              abs(float(res4.get("cost_cny") or 0) - 0.25) < 1e-9 and w4.get("basis") == "snapshot",
              str((res4.get("cost_cny"), w4.get("basis"))))

        billing.TASK_OPEN = os.path.join(tmp, "task_open5.json")
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="noacct")
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        res5 = billing.task_settle(note="noacct", close=True)
        w5 = res5.get("window") or {}
        check("无账户快照-回落逐轮口径", abs(float(res5.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res5.get("cost_cny")))
        check("无账户快照-basis=snapshot", w5.get("basis") == "snapshot", str(w5.get("basis")))

        # ---- 读不到插件记录：必须拒绝结算，不能静默记 0、更不能推进终点 ----
        billing.TASK_OPEN = os.path.join(tmp, "task_open_none.json")
        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_end_ms": 1500}, f)
        state["data"] = {}
        res5b = billing.task_settle(note="nousage", close=True)
        check("读不到扣费记录→拒绝结算", res5b.get("ok") is False, str(res5b))
        check("读不到扣费记录→不推进终点",
              int(billing._cycle_read().get("last_end_ms") or 0) == 1500,
              str(billing._cycle_read()))

        # ---- 周期结算：没有任务窗口时直接查插件 events 记录（上次结算终点 → 现在）----
        billing.TASK_OPEN = os.path.join(tmp, "task_open_none.json")
        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_end_ms": 1500}, f)
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500), ev(3000, 0.05, 500))
        res6 = billing.task_settle(note="cycle", close=True)
        w6 = res6.get("window") or {}
        check("周期结算-ok", res6.get("ok") is True, str(res6))
        check("周期结算-basis=events-cycle", w6.get("basis") == "events-cycle", str(w6.get("basis")))
        check("周期结算-只算上次终点之后",
              abs(float(res6.get("cost_cny") or 0) - 0.30) < 1e-9, str(res6.get("cost_cny")))
        check("周期结算-推进终点", int(billing._cycle_read().get("last_end_ms") or 0) > 1500,
              str(billing._cycle_read()))
        res7 = billing.task_settle(note="cycle2", close=True)
        check("周期结算-不重复计数", abs(float(res7.get("cost_cny") or 0)) < 1e-9,
              str(res7.get("cost_cny")))
    finally:
        try:
            with open(prod_cycle, "r", encoding="utf-8") as f:
                prod_cycle_after = f.read()
        except OSError:
            prod_cycle_after = None
        check("自检不写生产 billing_cycle", prod_cycle_before == prod_cycle_after)
        (billing.TASK_OPEN, billing.BILLING_JSONL, billing.BUDGET_JSON,
         billing.CYCLE_FILE, billing.load_usage) = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"断言：{PASS + FAIL} 项｜通过 {PASS}｜失败 {FAIL}")
    print("OK 全部通过" if FAIL == 0 else "✗ 有断言失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
