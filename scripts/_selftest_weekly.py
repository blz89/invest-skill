# -*- coding: utf-8 -*-
"""weekly.py 的「本金匹配」闸门自检（离线，不读账本、不联网）。

为什么要有它：这道闸门是决策日的**硬中止**——`out/stage3_plan.json` 的本金与账本
可投资本对不上就 return 2，整周不下单。它拦的是真串账（10 万账本的组合拿去改 5 万账本），
但不能被「上周赚了 18 元」这种可投资本的小差额误伤，否则每周都卡在最后一步。
锁死：
  * 计划本金 = 可投资本 → 放行、无提示；
  * 计划本金 = 账本本金（可投资本略高，如 50,000 vs 50,018）→ 放行 + 提示差额；
  * 计划本金既不等于可投资本也不等于本金（真串账）→ 中止；
  * 旧产物没写 capital → 放行；
  * 1 元容差（浮点/四舍五入）。

跑法：python scripts/_selftest_weekly.py（离线）
"""
import atexit
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import weekly  # noqa: E402

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


def t_capital_match():
    print("[1] 本金匹配闸门")
    cm = weekly.capital_match

    ok, note = cm(50000.0, 50000.0, 50000.0)
    check("计划本金=可投资本 → 放行且无提示", ok and note == "", f"ok={ok} note={note!r}")

    ok, note = cm(50000.0, 50018.0, 50000.0)
    check("计划按本金生成、账本可投资本略高（+18）→ 放行", ok, f"ok={ok} note={note!r}")
    check("提示里写明差额 +18", "+18" in note and "50,000" in note, note)

    ok, note = cm(50018.0, 50018.0, 50000.0)
    check("计划按可投资本生成 → 放行且无提示", ok and note == "", f"ok={ok} note={note!r}")

    ok, _ = cm(100000.0, 50018.0, 50000.0)
    check("10 万本金的组合拿到 5 万账本 → 中止", not ok)

    ok, _ = cm(50000.0, 50018.0, 80000.0)
    check("计划本金 ≠ 账本本金也 ≠ 可投资本 → 中止", not ok)

    ok, note = cm(None, 50018.0, 50000.0)
    check("旧产物没写 capital → 放行", ok and note == "")

    ok, _ = cm(50018.7, 50018.0, 50000.0)
    check("1 元内浮点差 → 放行", ok)

    ok, _ = cm(50020.0, 50018.0, 50000.0)
    check("超过 1 元 → 中止", not ok)


def t_refresh_net():
    """决策日第 10 步 --refresh-net：回归「new_md 未赋值先引用」崩溃。

    用假 billing/score/report_html/paper 隔离，只验证刷新逻辑本身不崩、且真的改写了行。
    """
    import shutil
    import tempfile
    import types

    print("[2] --refresh-net 刷新周报净利润")
    tmp = tempfile.mkdtemp(prefix="weekly_net_")
    fake_b = types.ModuleType("billing")
    fake_b.budget_state = lambda *a, **k: {"spent_cny": 1.5}
    fake_s = types.ModuleType("score")

    def _eval(ret, spent, cap):
        return {"gross_profit": ret * cap, "net_profit": ret * cap - spent,
                "net_rate": (ret * cap - spent) / cap, "grade": "D",
                "reward_pending": True, "reward_note": "评估期未到"}

    fake_s.evaluate = _eval
    fake_r = types.ModuleType("report_html")
    fake_r.report_path = lambda *a, **k: ""
    fake_p = types.SimpleNamespace(stats=lambda st: {"total_return": 0.0})
    saved = (weekly.PAPER_OUT, weekly.paper,
             sys.modules.get("billing"), sys.modules.get("score"), sys.modules.get("report_html"))
    try:
        weekly.PAPER_OUT = tmp
        weekly.paper = fake_p
        sys.modules["billing"] = fake_b
        sys.modules["score"] = fake_s
        sys.modules["report_html"] = fake_r
        md = os.path.join(tmp, "weekly_2026-11-20.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("| **净利润 / 净收益率** | **旧值** |\n| 档位 / 奖励 | **旧档** / 旧 |\n")
        rc = weekly.refresh_net("50k", "2026-11-20", {"meta": {"capital": 50000.0}})
        with open(md, "r", encoding="utf-8") as f:
            text = f.read()
        check("refresh_net 不崩溃（exit 0）", rc == 0, f"rc={rc!r}")
        check("净利润行被改写（-1.50 元）", "-1.50 元" in text, text)
        check("档位行不再由 refresh_net 维护（仍为旧档）",
              "**旧档** / 旧" in text, text)
    finally:
        weekly.PAPER_OUT, weekly.paper = saved[0], saved[1]
        for name, mod in zip(("billing", "score", "report_html"), saved[2:]):
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        shutil.rmtree(tmp, ignore_errors=True)


def t_period_snapshot():
    print("[2] 上期快照按数据日归档（2026-11-21 修「上期=今天 / 相隔 0 天」）")
    import json as _json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="weekly_period_")
    saved = weekly.PAPER_OUT
    try:
        weekly.PAPER_OUT = tmp
        for name in ("decision_latest.json", "decision_2026-11-20.json"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                _json.dump({"as_of": "2026-11-20", "regime": "防守"}, f)
        with open(os.path.join(tmp, "weekly_2026-11-13.md"), "w", encoding="utf-8") as f:
            f.write("# 周报\n")
        check("同数据日重跑 → 不把本期当上期",
              weekly.prev_snapshot("", "2026-11-20") is None)
        check("快照缺失 → 区间起点兜底到上一份周报日",
              weekly.prev_report_as_of("", "2026-11-20") == "2026-11-13")
        check("没有更早周报 → 兜底为空", weekly.prev_report_as_of("", "2026-11-13") == "")
        with open(os.path.join(tmp, "decision_2026-11-13.json"), "w", encoding="utf-8") as f:
            _json.dump({"as_of": "2026-11-13", "regime": "防守"}, f)
        _prev = weekly.prev_snapshot("", "2026-11-20")
        check("有更早的按日归档 → 上期取 11-13",
              (_prev or {}).get("as_of") == "2026-11-13", str(_prev))
        weekly.save_snapshot("", {"as_of": "2026-11-20", "regime": "防守"})
        check("save_snapshot 仍写 latest",
              os.path.exists(os.path.join(tmp, "decision_latest.json")))
        check("save_snapshot 同时写按日归档",
              os.path.exists(os.path.join(tmp, "decision_2026-11-20.json")))
    finally:
        weekly.PAPER_OUT = saved
        shutil.rmtree(tmp, ignore_errors=True)


def t_backup_history():
    print("[3] 周报/HTML/订单覆盖前备份到统一 _history 文件夹（2026-11-21 用户要求）")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="weekly_hist_")
    saved = weekly.PAPER_OUT
    try:
        weekly.PAPER_OUT = tmp
        p = os.path.join(tmp, "weekly_2026-11-20.md")
        check("无旧文件时不备份", weekly._backup_before_write(p) == "")
        with open(p, "w", encoding="utf-8") as f:
            f.write("old-weekly")
        b1 = weekly._backup_before_write(p)
        check("旧周报备份成功", bool(b1) and os.path.exists(b1))
        check("备份落在统一 _history/ 且文件名不变",
              b1 == os.path.join(tmp, "_history", "weekly_2026-11-20.md"), b1)
        with open(b1, encoding="utf-8") as f:
            check("备份内容=旧版", f.read() == "old-weekly")
        with open(p, "w", encoding="utf-8") as f:
            f.write("new-weekly")
        b2 = weekly._backup_before_write(p)
        check("再写不覆盖备份（保留最早原版，供 diff）", b2 == b1)
        with open(b1, encoding="utf-8") as f:
            check("备份仍是原版", f.read() == "old-weekly")
        hit = [n for n in os.listdir(os.path.join(tmp, "_history"))]
        check("一个数据日只留一份备份", len(hit) == 1, str(hit))
        with open(p, encoding="utf-8") as f:
            check("当前文件是新版（覆盖照常）", f.read() == "new-weekly")
    finally:
        weekly.PAPER_OUT = saved
        shutil.rmtree(tmp, ignore_errors=True)


def t_industry_cap_block():
    """行业上限硬拦截：买单不能把某行业买到 25% 之上（#17）。"""
    print("[4] 行业 25% 硬拦截（只拦买单）")
    import weekly as _w

    saved = _w.industry.load_map
    _w.industry.load_map = lambda: {
        "600276": {"code": "600276", "industry": "化学制品"},
        "002648": {"code": "002648", "industry": "化学制品"},
        "000999": {"code": "000999", "industry": "中药"},
    }
    try:
        state = {"holdings": [
            {"code": "600276", "name": "恒瑞医药", "kind": "stock", "shares": 100,
             "cost": 50.0, "last": 50.0},
        ], "orders": [
            {"code": "002648", "kind": "stock", "action": "buy", "status": "pending",
             "shares": 100, "limit_price": 20.0},
        ]}
        close = {"600276": 50.0, "002648": 20.0, "000999": 30.0}
        orders = [
            {"code": "000999", "name": "华润三九", "kind": "stock", "action": "buy",
             "shares": 100, "limit_price": 30.0},
            {"code": "002648", "name": "卫星化学", "kind": "stock", "action": "buy",
             "shares": 100, "limit_price": 20.0},
            {"code": "000999", "name": "华润三九", "kind": "stock", "action": "sell",
             "shares": 100, "limit_price": 30.0},
        ]
        # 权益上限 10,000 → 化学制品上限 2,500；已有 5,000 + 在挂 2,000 = 7,000 → 再买 2,000 超限被拦
        kept, blocked = _w.industry_cap_block(orders, state, close, 10000.0)
        check("化学制品超限的买单被拦下", any(o["code"] == "002648" for o in blocked), str(blocked))
        check("被拦的是 002648 与 000999（两张都超 25%）",
              sorted(o["code"] for o in blocked) == ["000999", "002648"], str(blocked))
        _c = next(o for o in blocked if o["code"] == "002648")
        check("拦截记录带行业与拦截后金额",
              _c.get("blocked_industry") == "化学制品" and _c.get("industry_after") == 9000.0,
              str(_c))
        check("同行业卖单不受影响",
              any(o["code"] == "000999" and o["action"] == "sell" for o in kept))
        # 中药 100 股 × 30 = 3,000 > 2,500 上限 → 也应被拦（只剩那张卖单）
        check("中药买单也超限（3,000 > 2,500）→ 被拦", len(kept) == 1, str(kept))
        # 未分类不占额度
        kept2, blocked2 = _w.industry_cap_block(
            [{"code": "600999", "name": "未映射", "kind": "stock", "action": "buy",
              "shares": 100, "limit_price": 100.0}], {"holdings": [], "orders": []},
            {"600999": 100.0}, 10000.0)
        check("未分类买单不占 25% 额度", len(kept2) == 1 and not blocked2)
        # ETF 不计入行业额度
        kept3, blocked3 = _w.industry_cap_block(
            [{"code": "sh510050", "kind": "etf", "action": "buy", "shares": 1000,
              "limit_price": 3.0}], {"holdings": [], "orders": []}, {"sh510050": 3.0}, 10000.0)
        check("ETF 买单不参与行业拦截", len(kept3) == 1 and not blocked3)
        # 无上限（0）时不拦截
        kept4, blocked4 = _w.industry_cap_block(orders, state, close, 0.0)
        check("权益上限为 0 时不做拦截（旧产物兜底）", len(kept4) == 3 and not blocked4)
        # 2026-11-26（A 类）：本期新单已由 target_orders 登记进 state["orders"]，
        # 扫描挂单时不能把它自己再算一遍（否则额度翻倍、买单被静默误拦）
        _self1 = {"id": "o1", "code": "000999", "name": "华润三九", "kind": "stock",
                  "action": "buy", "shares": 100, "limit_price": 30.0,
                  "created": "2026-11-25"}
        _st1 = {"holdings": [], "orders": [dict(_self1)], "events": []}
        kept5, blocked5 = _w.industry_cap_block([_self1], _st1, {"000999": 30.0}, 20000.0)
        check("行业额度不重复计算本期新单自身（3,000 ≤ 5,000 应保留）",
              len(kept5) == 1 and not blocked5, str(blocked5))
        # 被拦下的买单要就地取消，否则账本留幽灵挂单、报告说已拦
        _self2 = {"id": "o2", "code": "000999", "name": "华润三九", "kind": "stock",
                  "action": "buy", "shares": 100, "limit_price": 30.0,
                  "created": "2026-11-25"}
        _st2 = {"holdings": [], "orders": [dict(_self2)], "events": []}
        _k2, _b2 = _w.industry_cap_block([_self2], _st2, {"000999": 30.0}, 10000.0)
        check("被拦的买单就地取消（status=cancelled + cancel_reason）",
              not _k2 and _self2.get("status") == "cancelled"
              and "上限" in _self2.get("cancel_reason", ""), str(_self2))
        # 2026-11-26（A 方案，用户拍板）：分母统一为**本金**，不再用权益层上限。
        # 5 万本金 → 行业上限 12,500 元；防守档 17,500 元权益时的 4,412 元买单应放行。
        _o3 = {"id": "o3", "code": "002648", "name": "卫星化学", "kind": "stock",
               "action": "buy", "shares": 200, "limit_price": 22.06, "ref_price": 22.06,
               "created": "2026-11-25"}
        _st3 = {"holdings": [], "orders": [dict(_o3)], "events": [],
                "meta": {"capital": 50000.0}}
        _k3, _b3 = _w.industry_cap_block([_o3], _st3, {"002648": 22.7})
        check("行业上限分母=本金（50,000×25%=12,500，4,412 元买单放行）",
              len(_k3) == 1 and not _b3, str(_b3))
        # 已经占了 9,000 元同行业 → 再买 4,000 元 = 13,000 > 12,500 才拦
        _o4 = {"id": "o4", "code": "002648", "name": "卫星化学", "kind": "stock",
               "action": "buy", "shares": 200, "limit_price": 20.0, "created": "2026-11-25"}
        _st4 = {"holdings": [], "events": [],
                "orders": [dict(_o4),
                           {"id": "o9", "code": "600276", "kind": "stock",
                            "action": "buy", "status": "pending",
                            "shares": 180, "limit_price": 50.0,
                            "created": "2026-11-25"}],
                "meta": {"capital": 50000.0}}
        _k4, _b4 = _w.industry_cap_block([_o4], _st4, {"002648": 20.0, "600276": 50.0})
        check("本金分母下仍能拦超限（9,000+4,000 > 12,500）",
              any(o["code"] == "002648" for o in _b4), str(_b4))
    finally:
        _w.industry.load_map = saved


def t_week_skill_cost():
    """上周技能花费（实扣）：只统计 recorded_at 落在 [as_of-6, as_of] 的已入账 run。"""
    import json
    import tempfile

    import billing as _b

    print("[6] 上周技能花费（实扣）")
    tmp = tempfile.mkdtemp(prefix="weekly_skillcost_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    saved = _b.BUDGET_JSON
    try:
        _b.BUDGET_JSON = os.path.join(tmp, "budget.json")
        doc = {"version": 1, "ledgers": {"50k": {
            "base_cny": 150.0, "bonus_in_cny": 0.0, "capital": 50000.0,
            "carry_over_cny": 0.0, "remaining_cny": 150.0, "spent_cny": 0.0,
            "runs": [
                {"recorded_at": "2026-11-20 20:00:47", "cost_cny": 0.12,
                 "source": "task-window", "note": "每日巡检"},
                {"recorded_at": "2026-11-26 20:00:47", "cost_cny": 0.90,
                 "source": "task-window", "note": "每日巡检"},
                {"recorded_at": "2026-11-21 20:00:28", "cost_cny": 1.20,
                 "source": "events-cycle", "note": "决策"},
                {"recorded_at": "2026-11-19 20:00:00", "cost_cny": 0.50,
                 "source": "flat", "note": "旧定额"},
                {"recorded_at": "", "cost_cny": 9.99, "source": "flat", "note": "坏时间"},
            ]}}}
        with open(_b.BUDGET_JSON, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        sc = weekly.week_skill_cost("50k", "2026-11-26")
        check("区间为 as_of-6 ~ as_of", sc["since"] == "2026-11-20" and sc["until"] == "2026-11-26",
              str((sc["since"], sc["until"])))
        check("只计区间内 run 的合计（0.12+0.90+1.20=2.22）",
              abs(sc["cost_cny"] - 2.22) < 1e-9, str(sc["cost_cny"]))
        check("运行次数 3", sc["runs"] == 3, str(sc["runs"]))
        check("区间外（11-19）与坏时间行不计入",
              all(it["date"] in ("2026-11-20", "2026-11-21", "2026-11-26") for it in sc["items"]),
              str(sc["items"]))
        # 表格行：金额与次数、以及无数据时的占位
        row = weekly.skill_cost_row(sc)
        check("该行写明金额与次数", "2.22 元" in row and "3 次运行" in row, row)
        check("无数据时给占位、不猜数", "—" in weekly.skill_cost_row(None))
        check("无数据时占位不出现金额", "0.00 元" not in weekly.skill_cost_row(None))
    finally:
        _b.BUDGET_JSON = saved


def t_plan_date_guard():
    print("plan_date_guard（A5 data_as_of 闸门）")
    check("同日放行", weekly.plan_date_guard("2026-11-26", "2026-11-26", False) == "")
    check("空数据日放行（旧产物）", weekly.plan_date_guard("", "2026-11-26", False) == "")
    m = weekly.plan_date_guard("2026-11-27", "2026-11-26", False)
    check("数据日晚于决策日→拒绝", "未来数据" in m, m)
    m = weekly.plan_date_guard("2026-11-25", "2026-11-26", False)
    check("数据日落后→拒绝", "落后" in m, m)
    m = weekly.plan_date_guard("2026-11-26", "2026-10-24", True)
    check("回填历史日→拒绝", "回填" in m, m)


def t_next_fill_day():
    print("next_fill_day（交易日历 vs 周末近似）")
    import report_html as RH
    _old = RH._CAL_CACHE
    try:
        RH._CAL_CACHE = ["2026-09-30", "2026-10-09", "2026-11-27", "2026-11-30"]
        check("长假：决策日 09-30 → 下一交易日 10-09",
              RH.next_fill_day("2026-09-30") == "2026-10-09", str(RH.next_fill_day("2026-09-30")))
        check("周末：决策日 11-27（周五）→ 11-30（周一）",
              RH.next_fill_day("2026-11-27") == "2026-11-30", str(RH.next_fill_day("2026-11-27")))
        RH._CAL_CACHE = []
        check("无日历：退化为只跳周末（长假会错，文档标注近似）",
              RH.next_fill_day("2026-09-30") == "2026-10-01", str(RH.next_fill_day("2026-09-30")))
    finally:
        RH._CAL_CACHE = _old


def main():
    t_capital_match()
    t_refresh_net()
    t_period_snapshot()
    t_backup_history()
    t_industry_cap_block()
    t_week_skill_cost()
    t_plan_date_guard()
    t_next_fill_day()
    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
