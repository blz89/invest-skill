# -*- coding: utf-8 -*-
"""doctor.py 的离线自检：造一批「坏账本」，确认体检该报 ERROR 的一定报。

为什么要有它：doctor.py 是每次决策前的闸门。
  * 漏报（该拦没拦）→ 带着坏账本跑决策，账本记错钱；
  * 误报（好账本被拦）→ 决策日直接卡住，白白错过一次调仓。
两种都不可接受，所以体检逻辑本身也要有回归测试。

跑法：python scripts/_selftest_doctor.py（不联网、不碰正式账本与 out/，全程用临时目录）
"""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import doctor
import paper

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


def base_state(capital=10000.0):
    return {
        "meta": {"version": 1, "mode": "paper", "capital": capital, "tier": "B",
                 "created_at": "2026-10-25T21:10:05", "baseline_date": "2026-10-23",
                 "baseline_bench": 4105.329, "bench_index": "000300"},
        "cash": 9000.0,
        "holdings": {"sh512500": {"shares": 200, "cost_basis": 3.8105,
                                  "last_close": 3.80, "stop": 3.298}},
        "trades": [],
        "orders": [
            {"id": "o1", "code": "sh512500", "name": "中证500ETF", "action": "buy",
             "order_type": "buy_open", "status": "pending", "shares": 100,
             "limit_price": None, "effective_from": "2026-10-26", "expire_on": None},
            {"id": "o2", "code": "sh510050", "name": "上证50ETF", "action": "buy",
             "order_type": "buy_limit", "status": "pending", "shares": 100,
             "limit_price": 2.726, "effective_from": "2026-10-26", "expire_on": "2026-11-09"},
        ],
        "nav": [{"date": "2026-10-23", "nav": 10000.0, "cash": 9000.0,
                 "market_value": 760.0, "drawdown": 0.0, "bench": 4105.329,
                 "bench_nav": 10000.0, "peak_nav": 10000.0}],
        "events": [],
    }


def run_checks(state, capital=10000.0, ledgers=None, make_backup=False):
    """对给定的 state 跑一遍账本检查，返回 Report（不写文件）。"""
    rep = doctor.Report()
    tmp = tempfile.mkdtemp(prefix="doctor_selftest_")
    path = os.path.join(tmp, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    if make_backup:
        paper.backup_state(path)
    cfg = {"capital": capital, "tier": "B", "state_file": path, "rebalance_mode": "once"}
    st = doctor.check_ledger(rep, "50k", cfg)
    shutil.rmtree(tmp, ignore_errors=True)
    return rep, st


def errors_of(rep):
    return [r for r in rep.items if r["level"] == "ERROR"]


def test_products() -> None:
    tmp = tempfile.mkdtemp(prefix="doctor_products_")
    old_out, old_cfg = config.OUT_DIR, dict(config.INDUSTRY)
    try:
        config.OUT_DIR = tmp
        config.INDUSTRY["min_coverage"] = 0.9
        with open(os.path.join(tmp, "stage1_all.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("code,name\n")
            for i in range(10):
                f.write("00000%d,x\n" % i)
        with open(os.path.join(tmp, "industry_map.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("code,industry\n")
            for i in range(5):
                f.write("00000%d,银行\n" % i)
        with open(os.path.join(tmp, "macro.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-11-20T00:00:00",
                       "data": {"social_financing": {"month": "2026-04", "increment_yi": 6245}}}, f)
        rep = doctor.Report()
        doctor.check_products(rep)
        areas = [(i["name"], i["level"], i["detail"]) for i in rep.items if i["area"] == "产物"]
        cov = [i for i in areas if i[0] == "行业映射覆盖"]
        sf = [i for i in areas if i[0] == "社融数据"]
        check("行业覆盖不足报警", cov and cov[0][1] == "WARN" and "5/10" in cov[0][2], str(cov))
        check("社融停更报警", sf and sf[0][1] == "WARN" and "停更" in sf[0][2], str(sf))
        # 覆盖够 + 社融新鲜 → 两项都 ok
        with open(os.path.join(tmp, "industry_map.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("code,industry\n")
            for i in range(10):
                f.write("00000%d,银行\n" % i)
        with open(os.path.join(tmp, "macro.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-11-20T00:00:00",
                       "data": {"social_financing": {"month": "2026-10", "increment_yi": 6245}}}, f)
        rep2 = doctor.Report()
        doctor.check_products(rep2)
        lv = {i["name"]: i["level"] for i in rep2.items if i["area"] == "产物"}
        check("覆盖够→INFO", lv.get("行业映射覆盖") == "INFO", str(lv))
        check("社融新鲜→INFO", lv.get("社融数据") == "INFO", str(lv))
    finally:
        config.OUT_DIR = old_out
        config.INDUSTRY.clear()
        config.INDUSTRY.update(old_cfg)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=== 1. 好账本 → 无 ERROR ===")
    rep, _ = run_checks(base_state())
    check("干净账本没有 ERROR", len(errors_of(rep)) == 0,
          str([r["name"] for r in errors_of(rep)]))

    print("=== 2. 结构性错误都要被抓到 ===")
    cases = []
    s = base_state(); s["cash"] = -5.0
    cases.append(("负现金", s, "现金非负"))
    s = base_state(); s["orders"][1]["id"] = s["orders"][0]["id"]
    cases.append(("订单 id 重复", s, "订单 id 唯一"))
    s = base_state(); s["orders"].append(
        {"id": "o3", "code": "sh512500", "name": "中证500ETF", "action": "sell",
         "order_type": "sell_stop", "status": "pending", "shares": 500,
         "limit_price": 3.2, "effective_from": "2026-10-26", "expire_on": None})
    cases.append(("卖单超过持仓", s, "挂单口径"))
    # 2026-11-15 回归：冻结的括号止损（bracket 且 active=False）不参与持仓占用校验
    s = base_state(); s["orders"].append(
        {"id": "o4", "code": "sh512500", "name": "中证500ETF", "action": "sell",
         "order_type": "sell_stop", "status": "pending", "shares": 500,
         "limit_price": 3.2, "effective_from": "2026-10-26", "expire_on": None,
         "bracket": True, "active": False, "parent_order_id": "o1"})
    rep, _ = run_checks(s)
    check("冻结括号卖单超持仓不误报", not any("挂单口径" in r["name"] for r in errors_of(rep)),
          str([r["name"] for r in errors_of(rep)]))
    s = copy.deepcopy(s); s["orders"][-1]["active"] = True
    rep, _ = run_checks(s)
    check("已激活括号卖单超持仓仍报错", any("挂单口径" in r["name"] for r in errors_of(rep)),
          str([r["name"] for r in errors_of(rep)]))
    s = copy.deepcopy(s); s["orders"][-1].pop("bracket"); s["orders"][-1].pop("active")
    rep, _ = run_checks(s)
    check("普通卖单超持仓仍报错（旧账本兼容路径）",
          any("挂单口径" in r["name"] for r in errors_of(rep)),
          str([r["name"] for r in errors_of(rep)]))
    s = base_state(); s["holdings"]["sh512500"]["shares"] = 150
    cases.append(("持仓非整手", s, "持仓口径"))
    s = base_state(); del s["meta"]["tier"]
    cases.append(("meta 缺键", s, "meta 完整"))
    s = base_state(); s["nav"] = s["nav"] + [
        {"date": "2026-10-22", "nav": 10000.0, "cash": 9000.0,
         "market_value": 760.0, "drawdown": 0.0, "bench": 4100.0,
         "bench_nav": 10000.0, "peak_nav": 10000.0}]   # 10-23 在 10-22 之前 = 乱序
    cases.append(("净值日期乱序", s, "净值升序"))
    s = base_state(); s["nav"][0]["date"] = "2099-01-01"
    cases.append(("净值日期超前", s, "净值不超前"))
    s = base_state(); s["orders"][0]["effective_from"] = "下周"
    cases.append(("生效日不可解析", s, "挂单口径"))
    s = base_state(); del s["cash"]
    cases.append(("缺必需键", s, "结构完整"))
    for label, state, expect_word in cases:
        rep, _ = run_checks(state)
        errs = errors_of(rep)
        hit = any(expect_word in r["name"] for r in errs)
        check(f"{label} → 报 ERROR（{expect_word}）", hit,
              f"实际 {[r['name'] for r in errs]}")

    print("=== 2b. 基准口径（账本基准一致性回归） ===")
    s = base_state()
    check("正常账本：基准口径无问题", doctor.nav_baseline_problems(s) == [])
    s = copy.deepcopy(base_state()); s["nav"][0]["bench_nav"] = 8000.0
    check("首点 bench_nav ≠ 本金 → 报错", bool(doctor.nav_baseline_problems(s)))
    s = copy.deepcopy(base_state()); s["nav"][0]["date"] = "2026-10-22"
    check("首点日期 ≠ baseline_date → 报错", bool(doctor.nav_baseline_problems(s)))
    s = copy.deepcopy(base_state()); s["nav"] = []
    check("nav 为空不误报（首次决策前）", doctor.nav_baseline_problems(s) == [])
    tmp_cache = tempfile.mkdtemp(prefix="doctor_cache_")
    old_cache = config.CACHE_DIR
    try:
        os.makedirs(os.path.join(tmp_cache, "index_daily"))
        with open(os.path.join(tmp_cache, "index_daily", "000300__2026-10-23.csv"),
                  "w", encoding="utf-8") as f:
            f.write("date,close\n2026-10-22,4100.0\n2026-10-23,4105.329\n")
        config.CACHE_DIR = tmp_cache
        bad, checked = doctor.bench_baseline_problems(base_state())
        check("baseline_bench=当日收盘 → 通过且已核对", (not bad) and checked, f"{bad} {checked}")
        s = copy.deepcopy(base_state()); s["meta"]["baseline_bench"] = 4049.552
        bad, checked = doctor.bench_baseline_problems(s)
        check("baseline_bench 与当日收盘不符 → 报错", checked and bool(bad), f"{bad} {checked}")
        s = copy.deepcopy(base_state()); s["meta"]["baseline_date"] = "2026-10-24"
        bad, checked = doctor.bench_baseline_problems(s)
        check("缓存缺该日期 → 跳过不误报", (not checked) and not bad, f"{bad} {checked}")
        s = copy.deepcopy(base_state()); s["nav"][0]["bench_nav"] = 8000.0
        rep, _ = run_checks(s)
        check("端到端：首点 bench_nav 错 → doctor 报 ERROR",
              any("基准与净值一致" in r["name"] for r in errors_of(rep)),
              str([r["name"] for r in errors_of(rep)]))
        s = copy.deepcopy(base_state()); s["meta"]["baseline_bench"] = 4049.552
        rep, _ = run_checks(s)
        check("端到端：baseline_bench 错 → doctor 报 ERROR",
              any("基准收盘可核对" in r["name"] for r in errors_of(rep)),
              str([r["name"] for r in errors_of(rep)]))
    finally:
        config.CACHE_DIR = old_cache
        shutil.rmtree(tmp_cache, ignore_errors=True)

    print("=== 3. 本金与配置不一致 → ERROR ===")
    rep, _ = run_checks(base_state(capital=100000.0), capital=10000.0)
    check("账本本金与配置不符", any("本金与配置一致" in r["name"] for r in errors_of(rep)))

    print("=== 4. 产物一致性：属于另一个账本是 INFO，不属于任何账本是 WARN ===")
    tmp_out = tempfile.mkdtemp(prefix="doctor_out_")
    old_out = config.OUT_DIR
    try:
        def plan_json(cap):
            return {"capital": cap, "generated_at": "2026-10-25T21:56:23",
                    "tier": {"equity_amount": 3500}, "market_regime": {"regime": "防守"}}

        cfg50 = {"capital": 50000.0, "tier": "B", "state_file": "x", "rebalance_mode": "once"}
        ledgers = {"50k": {"capital": 50000.0}, "alt": {"capital": 100000.0}}
        for cap, label, want in ((100000.0, "属于 alt 账本", "INFO"),
                                 (10000.0, "谁都不属于", "WARN"),
                                 (50000.0, "属于自己", "INFO")):
            with open(os.path.join(tmp_out, "stage3_plan.json"), "w", encoding="utf-8") as f:
                json.dump(plan_json(cap), f)
            config.OUT_DIR = tmp_out
            rep = doctor.Report()
            doctor.check_plan(rep, "50k", cfg50, base_state(capital=50000.0), ledgers)
            levels = [r["level"] for r in rep.items]
            check(f"{label} → {want}", want in levels, str(rep.items))
    finally:
        config.OUT_DIR = old_out
        shutil.rmtree(tmp_out, ignore_errors=True)

    print("=== 5. 缺账本文件只算 WARN（首次建账不该被拦） ===")
    rep = doctor.Report()
    cfg = {"capital": 10000.0, "tier": "B",
           "state_file": os.path.join(tempfile.gettempdir(), "no_such_state_xyz.json"),
           "rebalance_mode": "once"}
    st = doctor.check_ledger(rep, "50k", cfg)
    check("账本不存在 → WARN 且不 ERROR",
          st is None and rep.count("ERROR") == 0 and rep.count("WARN") >= 1)

    print("=== 6. 可投资本（利润再投资）与备份 ===")
    s = base_state(); s["trades"] = [{"action": "sell", "realized_pnl": 2000.0, "fee": 5.0}]
    rep, _ = run_checks(s)
    ok_inv = [r for r in rep.items if "可投资本" in r["name"]]
    check("有盈利时体检报出可投资本 12,000", bool(ok_inv) and "12,000" in ok_inv[0]["detail"],
          str([r["detail"] for r in ok_inv]))
    check("可投资本正常时不报 ERROR", len(errors_of(rep)) == 0,
          str([r["name"] for r in errors_of(rep)]))
    s = base_state()
    s["trades"] = [{"action": "sell", "realized_pnl": 2000.0, "fee": 5.0}]
    s["cash"] = 11000.0
    s["nav"][0]["nav"] = 11760.0        # 已实现 2000 但持仓浮亏 2.1 → 小幅高于净值，只 WARN
    rep, _ = run_checks(s)
    check("可投资本略高于净值 → 正常项（不 WARN、不 ERROR）",
          any("可投资本 vs 净值" in r["name"] and r["level"] not in ("WARN", "ERROR")
              for r in rep.items)
          and not any("可投资本 ≤ 净值" in r["name"] for r in errors_of(rep)),
          str([(r["level"], r["name"]) for r in rep.items if "可投资本" in r["name"]]))
    s = base_state()
    s["trades"] = [{"action": "sell", "realized_pnl": 2000.0, "fee": 5.0}]
    s["nav"][0]["nav"] = 5000.0        # 可投资本 12000 远高于净值 5000
    rep, _ = run_checks(s)
    check("可投资本远高于净值 → ERROR",
          any("可投资本 ≤ 净值" in r["name"] for r in errors_of(rep)),
          str([r["name"] for r in errors_of(rep)]))
    rep, _ = run_checks(base_state())
    check("没有备份 → WARN（提醒但不拦）",
          any("有可回滚备份" in r["name"] and r["level"] == "WARN" for r in rep.items),
          str([(r["level"], r["name"]) for r in rep.items if "备份" in r["name"]]))
    rep, _ = run_checks(base_state(), make_backup=True)
    check("有备份 → INFO 且不 WARN",
          any("有可回滚备份" in r["name"] and r["level"] == "INFO" for r in rep.items),
          str([(r["level"], r["name"]) for r in rep.items if "备份" in r["name"]]))

    print("=== 7. 决策新鲜度（漏跑一期不会报错，只会静默拖期） ===")
    import datetime as _dt
    today = _dt.date.today()
    tmp_f = tempfile.mkdtemp(prefix="doctor_fresh_")
    try:
        def fresh_report(days_ago, suffix="", as_of=None, ledger="50k"):
            path = os.path.join(tmp_f, f"decision{suffix}_latest.json")
            if days_ago is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                d = (as_of or (today - _dt.timedelta(days=days_ago)).isoformat())
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"as_of": d, "ledger": ledger}, f)
            rep = doctor.Report()
            doctor.check_freshness(rep, ledger, pout=tmp_f)
            return rep

        rep = fresh_report(None)
        check("没有决策快照 → WARN（首次决策后生成）",
              any("决策新鲜度" in r["name"] and r["level"] == "WARN" for r in rep.items),
              str(rep.items))
        rep = fresh_report(3)
        check("3 天前决策过 → INFO",
              any("决策新鲜度" in r["name"] and r["level"] == "INFO" for r in rep.items),
              str(rep.items))
        rep = fresh_report(30)
        check("30 天没决策 → WARN（漏跑）",
              any("决策新鲜度" in r["name"] and r["level"] == "WARN" for r in rep.items),
              str(rep.items))
        rep = fresh_report(None, as_of=(today + _dt.timedelta(days=3)).isoformat())
        check("快照日期在未来 → WARN（时钟/数据异常）",
              any("决策新鲜度" in r["name"] and r["level"] == "WARN" for r in rep.items),
              str(rep.items))
        rep = fresh_report(None, suffix="_alt", as_of="2026-10-23", ledger="alt")
        check("alt 找 decision_alt_latest.json（不串账）",
              any("alt" in r["name"] and any(x in r["detail"] for x in ("上次决策", "未来", "还没有"))
                  for r in rep.items),
              str(rep.items))
    finally:
        shutil.rmtree(tmp_f, ignore_errors=True)

    print("=== 8. 产物自检（行业覆盖 / 社融停更） ===")
    test_products()

    print()
    print(f"断言：{PASS + FAIL} 项｜通过 {PASS}｜失败 {FAIL}")
    print("OK 全部通过" if FAIL == 0 else "✗ 有断言失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
