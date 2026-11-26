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

    w0 = billing.window_events(1500, 2500, data=d0)
    check("时间窗-只算区间内", w0["n_calls"] == 1 and abs(w0["cost_cny"] - 0.25) < 1e-9, str(w0))
    w0b = billing.window_events(1000, 2000, data=d0)
    check("时间窗-左开右闭", w0b["n_calls"] == 1 and abs(w0b["cost_cny"] - 0.25) < 1e-9, str(w0b))
    check("时间窗-空区间", billing.window_events(2500, 2600, data=d0)["n_calls"] == 0)

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

        doc = billing.task_begin(ledgers=["50k"], note="selftest", since_ms=1500)
        snap_doc = doc.get("snapshot") or {}
        check("开窗写快照", abs(float(snap_doc.get("cost_cny") or 0) - 0.10) < 1e-9,
              str(snap_doc))

        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        res = billing.task_settle(note="selftest", close=True)
        check("结算返回 ok", res.get("ok") is True, str(res))
        check("结算金额用差额", abs(float(res.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res.get("cost_cny")))
        check("结算标记 basis=events-window",
              (res.get("window") or {}).get("basis") == "events-window",
              str((res.get("window") or {}).get("basis")))
        check("交叉校验一致", bool(((res.get("window") or {}).get("cross_check") or {}).get("ok")) is True,
              str((res.get("window") or {}).get("cross_check")))

        with open(billing.BUDGET_JSON, "r", encoding="utf-8") as f:
            budget = json.load(f)
        spent = float(((budget.get("ledgers") or {}).get("50k") or {}).get("spent_cny") or 0)
        check("预算已花=差额", abs(spent - 0.25) < 1e-9, str(spent))

        with open(billing.BILLING_JSONL, "r", encoding="utf-8") as f:
            row = json.loads(f.read().strip().splitlines()[-1])
        check("流水 basis=events-window", row.get("basis") == "events-window", str(row.get("basis")))
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
        doc3 = billing.task_begin(ledgers=["50k"], note="acct", since_ms=1500)
        check("开窗写账户快照", int((doc3.get("account") or {}).get("units") or 0) == 14_000_000,
              str(doc3.get("account")))
        state["data"] = ledger(19_000_000, [ev(1000, 0.10, 1000), ev(2000, 0.25, 2500)])
        res3 = billing.task_settle(note="acct", close=True)
        w3 = res3.get("window") or {}
        check("记账用逐轮事件差额", abs(float(res3.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res3.get("cost_cny")))
        check("记账口径 basis=events-window", w3.get("basis") == "events-window", str(w3.get("basis")))
        check("账户差额只作参考字段",
              abs(float((w3.get("account_delta") or {}).get("cny") or 0) - 0.05) < 1e-9,
              str(w3.get("account_delta")))

        billing.TASK_OPEN = os.path.join(tmp, "task_open4.json")
        state["data"] = ledger(14_000_000, [ev(1000, 0.10, 1000)])
        billing.task_begin(ledgers=["50k"], note="unobs", since_ms=1500)
        state["data"] = ledger(14_000_000, [ev(1000, 0.10, 1000), ev(2000, 0.25, 2500)])
        res4 = billing.task_settle(note="unobs", close=True)
        w4 = res4.get("window") or {}
        check("账户无新观测也照常记账（不告警）",
              abs(float(res4.get("cost_cny") or 0) - 0.25) < 1e-9 and w4.get("basis") == "events-window",
              str((res4.get("cost_cny"), w4.get("basis"))))

        billing.TASK_OPEN = os.path.join(tmp, "task_open5.json")
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="noacct", since_ms=1500)
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        res5 = billing.task_settle(note="noacct", close=True)
        w5 = res5.get("window") or {}
        check("无账户快照-回落逐轮口径", abs(float(res5.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(res5.get("cost_cny")))
        check("无账户快照-basis=events-window", w5.get("basis") == "events-window", str(w5.get("basis")))

        # ---- 时间窗内查不到 events（时钟异常/晚落盘）→ 回落快照差额并明确标注 ----
        billing.TASK_OPEN = os.path.join(tmp, "task_open_fb.json")
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="fallback", since_ms=1500)
        state["data"] = data(ev(1000, 0.10, 1000), ev(1200, 0.25, 2500))
        resf = billing.task_settle(note="fallback", close=True)
        wf = resf.get("window") or {}
        check("时间窗缺事件-回落快照差额", abs(float(resf.get("cost_cny") or 0) - 0.25) < 1e-9,
              str(resf.get("cost_cny")))
        check("时间窗缺事件-basis=snapshot-fallback",
              wf.get("basis") == "snapshot-fallback", str(wf.get("basis")))

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
        # ---- A1/A2/A3/A4：2026-11-26 第二轮审计修复的守卫 ----
        billing.TASK_OPEN = os.path.join(tmp, "task_open_a1.json")
        with open(billing.TASK_OPEN, "w", encoding="utf-8") as f:
            json.dump({"since_ms": 0, "ledgers": ["50k"], "begun_at": "x", "steps": []}, f)
        _a1 = False
        try:
            billing.task_begin(ledgers=["50k"], note="a1")
        except SystemExit:
            _a1 = True
        check("A1 旧窗结算失败→拒绝覆盖开窗", _a1)
        check("A1 旧窗仍在", billing._task_read() is not None)

        billing.TASK_OPEN = os.path.join(tmp, "task_open_a2.json")
        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_end_ms": 1500}, f)
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="a2", since_ms=1500)
        state["data"] = {"_error": "模拟：用量文件损坏"}
        _a2 = billing.task_settle(note="a2", close=True)
        check("A2 用量不可读→拒绝结算", _a2.get("ok") is False, str(_a2))
        check("A2 不推进终点", int(billing._cycle_read().get("last_end_ms") or 0) == 1500,
              str(billing._cycle_read()))
        check("A2 不删窗口", billing._task_read() is not None)

        billing.TASK_OPEN = os.path.join(tmp, "task_open_a2b.json")
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        billing.task_begin(ledgers=["50k"], note="a2b", since_ms=2500)
        state["data"] = data(ev(2600, 0.05, 500))
        _a2b = billing.task_settle(note="a2b", close=True)
        check("A2b 用量被轮转→拒绝结算", _a2b.get("ok") is False, str(_a2b))

        billing.TASK_OPEN = os.path.join(tmp, "task_open_a3.json")
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="a3", since_ms=0)
        _d3 = billing._task_read()
        _d3["since_ms"] = 1
        billing._task_write(_d3)
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
        _a3 = billing.task_settle(note="a3", close=True)
        check("A3 跨日窗口标记 stale",
              bool((_a3.get("window") or {}).get("stale_window")) is True,
              str(_a3.get("window")))

        billing.TASK_OPEN = os.path.join(tmp, "task_open_a4.json")
        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_end_ms": 1500}, f)
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="a4", since_ms=2000)
        state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500), ev(3000, 0.05, 500))
        _a4 = billing.task_settle_cycle(note="a4")
        check("A4 cycle-end 合并结算 ok", _a4.get("ok") is True, str(_a4))
        check("A4 空档+窗口合计 0.30", abs(float(_a4.get("cost_cny") or 0) - 0.30) < 1e-9,
              str(_a4.get("cost_cny")))

        # ---- B4：周期终点/交叉校验/账户上界（2026-11-26 第三轮审计缺口）----
        import contextlib as _ctx
        import io as _io

        billing.TASK_OPEN = os.path.join(tmp, "task_open_b4.json")
        try:
            os.remove(billing.CYCLE_FILE)
        except OSError:
            pass
        _since0, _now0 = billing.cycle_span()
        _zero = billing._dt.datetime.combine(billing._dt.date.today(),
                                             billing._dt.time())
        check("B4 首周期无终点→今天 0 点起算",
              int(_since0) == int(_zero.timestamp() * 1000), str((_since0, _now0)))

        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            f.write("{bad json")
        _buf = _io.StringIO()
        with _ctx.redirect_stdout(_buf):
            _bad_cycle = billing._cycle_read()
        check("B4 坏终点文件→按无终点处理", _bad_cycle == {}, str(_bad_cycle))
        check("B4 坏终点文件→有告警", "不可读" in _buf.getvalue(),
              _buf.getvalue()[:120])

        billing.TASK_OPEN = os.path.join(tmp, "task_open_b4x.json")
        state["data"] = data(ev(1000, 0.10, 1000))
        billing.task_begin(ledgers=["50k"], note="cross", since_ms=1500)
        _orig_we = billing.window_events
        billing.window_events = lambda s, u=None, d=None: {
            "since_ms": s, "until_ms": 9999, "since": "x", "until": "y",
            "n_calls": 2, "tokens": 100, "cost_cny": 0.40}
        try:
            state["data"] = data(ev(1000, 0.10, 1000), ev(2000, 0.25, 2500))
            _buf2 = _io.StringIO()
            with _ctx.redirect_stdout(_buf2):
                _rc = billing.task_settle(note="cross", close=True)
        finally:
            billing.window_events = _orig_we
        _wx = _rc.get("window") or {}
        check("B4 交叉校验不一致→cross_check.ok=False",
              (_wx.get("cross_check") or {}).get("ok") is False, str(_wx.get("cross_check")))
        check("B4 交叉校验不一致→有告警", "交叉校验不一致" in _buf2.getvalue(),
              _buf2.getvalue()[:160])

        billing.TASK_OPEN = os.path.join(tmp, "task_open_b4u.json")
        _nowms = int(billing._dt.datetime.now().timestamp() * 1000)
        with open(billing.CYCLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_end_ms": _nowms - 3_600_000}, f)
        _orig_ub = billing.day_debit_cny
        billing.day_debit_cny = lambda day, data=None: 0.05
        try:
            state["data"] = data(ev(_nowms - 60_000, 0.30, 3000))
            _buf3 = _io.StringIO()
            with _ctx.redirect_stdout(_buf3):
                _ru = billing.task_settle(note="ub", close=True)
        finally:
            billing.day_debit_cny = _orig_ub
        _wu = _ru.get("window") or {}
        check("B4 上界告警→标记 account_ub_exceeded",
              bool(_wu.get("account_ub_exceeded")) is True,
              str((_wu.get("account_ub_cny"), _wu.get("cost_cny"))))
        check("B4 上界告警→有告警文案", "上界" in _buf3.getvalue(),
              _buf3.getvalue()[:160])

        # A9：同一 run_id 重复记账必须幂等（否则重复扣费）
        _win9 = {"since": "2026-11-26 10:00:00", "until": "2026-11-26 10:05:00",
                 "cost_cny": 0.0123, "tokens": 1234, "n_calls": 2}
        billing.record_run("run-dup-1", "50k", _win9, note="A9 幂等")
        _r2 = billing.record_run("run-dup-1", "50k", _win9, note="A9 幂等")
        check("A9 同 run_id 第二次 record_run 判重", bool(_r2.get("dup")), str(_r2)[:120])
        _doc9 = billing.load_budget()
        _runs9 = ((_doc9.get("ledgers") or {}).get("50k") or {}).get("runs") or []
        _ids9 = [str(x.get("run_id")) for x in _runs9]
        check("A9 budget.json 同 run_id 只留一条", _ids9.count("run-dup-1") == 1, str(_ids9[-4:]))

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
