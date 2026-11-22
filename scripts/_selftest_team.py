# -*- coding: utf-8 -*-
"""编排框架自测：score / billing / grid / team 四块的纯逻辑与产物。

跑法：python _selftest_team.py
原则：只用合成数据，不联网、不写真实账本（临时目录里跑）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import datetime as _dt
import config
import score
import billing
import grid
import team

_fail = 0
_n = 0


def ck(name: str, cond: bool, detail: str = "") -> None:
    global _fail, _n
    _n += 1
    if not cond:
        _fail += 1
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def section(t: str) -> None:
    print(f"\n[{t}]")


# ============================================================ A. score
def t_score() -> None:
    section("A. 评分与奖励")
    SC = config.SCORE
    # 档位边界：下界含、上界不含
    ck("A 边界", score.grade_of(SC["grade_lines"]["A"]) == "A")
    ck("A 上界外", score.grade_of(SC["grade_lines"]["A"] - 1e-9) == "B+")
    ck("B+ 边界", score.grade_of(SC["grade_lines"]["B+"]) == "B+")
    ck("B 边界", score.grade_of(SC["grade_lines"]["B"]) == "B")
    ck("B- 边界", score.grade_of(SC["grade_lines"]["B-"]) == "B-")
    ck("C 边界", score.grade_of(0.0) == "C")
    ck("D 负数", score.grade_of(-1e-9) == "D")

    # 净收益率 = 收益 − token/本金
    ck("token 成本率", abs(score.token_cost_rate(150, 50000) - 0.003) < 1e-12)
    ck("净收益率", abs(score.net_return(0.03, 150, 50000) - 0.027) < 1e-12)
    ck("本金为 0 报错", _raises(lambda: score.token_cost_rate(1, 0)))

    # 50000 本金 3.5% 收益、1.2 元 → B+（奖励须等评估期到：显式传 2027-11-01）
    ev = score.evaluate(0.035, 1.2, 50000, as_of="2027-11-01")
    ck("evaluate 档位 B+", ev["grade"] == "B+", ev["grade"])
    ck("奖励比例 7%", abs(ev["reward_pct"] - 0.07) < 1e-12, ev["reward_pct"])
    ck("奖励 = 净利润×7%", abs(ev["reward_cny"] - ev["net_profit"] * 0.07) < 1e-9)
    ck("奖励全部转次年预算", abs(ev["reward_to_budget_cny"] - ev["reward_cny"]) < 1e-9)
    ck("奖励不再归 AI 现金", ev["reward_to_ai_cny"] == 0.0)
    ck("留存 = 净利润 − 奖励",
       abs(ev["kept_cny"] - (ev["net_profit"] - ev["reward_cny"])) < 1e-9)
    ck("留存率", abs(ev["kept_rate"] - ev["kept_cny"] / 50000) < 1e-12)
    ck("净利润 = 账户收益 − token 实花",
       abs(ev["net_profit"] - (ev["gross_profit"] - 1.2)) < 1e-9, str(ev["net_profit"]))
    ck("账户收益 = 本金×收益率", abs(ev["gross_profit"] - 50000 * 0.035) < 1e-9)
    ck("评估期已到 → 奖励照算", ev["reward_pending"] is False, str(ev["reward_pending"]))

    # 评估期未到（2027-10-30 前）：档位照判，奖励一律 0 并给文案
    evp = score.evaluate(0.035, 1.2, 50000, as_of="2026-11-07")
    ck("评估期未到仍评 B+", evp["grade"] == "B+", evp["grade"])
    ck("评估期未到奖励=0", evp["reward_cny"] == 0.0, repr(evp["reward_cny"]))
    ck("评估期未到不出现 -0.0", str(evp["reward_cny"]) == "0.0", str(evp["reward_cny"]))
    ck("评估期未到标记", evp["reward_pending"] is True)
    ck("评估期文案", "评估期未到" in (evp["reward_note"] or ""), evp["reward_note"])
    ck("评估起点 2027-10-30", evp["eval_start"] == "2027-10-30", evp["eval_start"])
    ck("评估期边界：前一天 0", score.evaluate(0.035, 1.2, 50000,
                                              as_of="2027-10-29")["reward_cny"] == 0.0)
    ck("评估期边界：当天起算", score.evaluate(0.035, 1.2, 50000,
                                             as_of="2027-10-30")["reward_pending"] is False)
    _t = score.format_report(evp)
    ck("pending 汇报含文案", "评估期未到" in _t, _t)
    ck("pending 汇报不写奖励金额", "奖励 0" not in _t and "奖励 ¥0" not in _t)

    # 年报口径：不足一年按等价年化净收益率定档；奖励仍按当年净利润（不按年化理论值）
    _nr = 0.02 - 100.0 / 50000
    _ann = (1 + _nr) ** (365 / 90) - 1
    eva = score.evaluate(0.02, 100.0, 50000, days=90, net_profit=600.0, as_of="2027-11-01")
    ck("等价年化定档", eva["grade"] == score.grade_of(_ann), str(eva["grade"]))
    ck("年化净收益率正确", abs(eva["net_rate_ann"] - _ann) < 1e-12)
    ck("annualized 标记", eva["annualized"] is True)
    ck("奖励按当年净利润 × 档位比例", abs(eva["reward_cny"] - 600.0 * eva["reward_pct"]) < 1e-9)
    ck("net_profit 覆盖生效", abs(eva["net_profit"] - 600.0) < 1e-9)
    ck("未给 days 不年化", score.evaluate(0.02, 100.0, 50000)["annualized"] is False)

    # 亏损不给奖励，且不出现 -0.00
    ev2 = score.evaluate(-0.03, 150, 50000)
    ck("亏损 D", ev2["grade"] == "D", ev2["grade"])
    ck("亏损奖励为 0", ev2["reward_cny"] == 0.0, repr(ev2["reward_cny"]))
    ck("亏损不出 -0.0", str(ev2["reward_cny"]) == "0.0", str(ev2["reward_cny"]))
    ck("净亏损含 token", ev2["net_rate"] < -0.03, ev2["net_rate"])

    # 风险否决：A 档 + 回撤 25% → 降 B+、奖励减半
    ev3 = score.evaluate(0.06, 150, 50000, mdd=-0.449)
    ck("否决触发", ev3["vetoed"] is True)
    ck("否决前是 A", ev3["grade_before_veto"] == "A", ev3["grade_before_veto"])
    ck("否决后 B+", ev3["grade"] == SC["drawdown_veto_grade"], ev3["grade"])
    ck("否决有理由", "风险否决" in ev3["veto_reason"])
    # 回撤浅 → 不否决
    ev4 = score.evaluate(0.06, 150, 50000, mdd=-0.05)
    ck("回撤浅不否决", ev4["vetoed"] is False and ev4["grade"] == "A")
    # 非 A 档不适用否决
    ev5 = score.evaluate(0.02, 0, 50000, mdd=-0.5)
    ck("B 档不否决", ev5["vetoed"] is False and ev5["grade"] == "B")

    # 预算档位
    ck("预算 50k", score._ledger_key(50000) == "50k", score._ledger_key(50000))
    ck("预算档位回退 50k", score._ledger_key(100000) == "50k", score._ledger_key(100000))
    ck("预算 8 万落到 50k", score._ledger_key(80000) == "50k", score._ledger_key(80000))

    # 12 期分布
    d = score.dist_eval(["B"] * 6 + ["B+"] * 3 + ["A"] + ["C", "B-"])
    ck("分布通过", d["pass"] is True, str(d))
    d2 = score.dist_eval(["A", "B+", "B", "B", "B", "B", "B", "B+", "B-", "C", "C", "D"])
    ck("分布 D 不通过", d2["pass"] is False)
    ck("分布计数", d2["count"]["D"] == 1 and d2["count"]["C"] == 2)

    # 汇报文本不崩
    txt = score.format_report(ev)
    ck("report 含评级", "B+" in txt)
    ck("report 含奖励", "奖励" in txt)
    ck("report 写全转次年预算", "全部转次年 token 预算" in txt, txt)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


# ============================================================ B. billing
def t_billing() -> None:
    section("B. 计费")
    ck("parse_ts 秒", billing.parse_ts(1700000000) == 1700000000000)
    ck("parse_ts 毫秒", billing.parse_ts(1700000000000) == 1700000000000)
    ck("parse_ts 字符串", billing.parse_ts("2026-10-28") > 0)


    ck("new_run_id 含账本", "50k" in billing.new_run_id("50k"))
    ck("new_run_id 唯一", billing.new_run_id("50k") != billing.new_run_id("50k"))

    # 预算账本：记录一笔后 spent 增加、remaining 减少（写到临时目录）
    old_out = config.OUT_DIR
    tmp = tempfile.mkdtemp(prefix="team_billing_")
    try:
        config.OUT_DIR = tmp
        billing.BUDGET_JSON = os.path.join(tmp, "budget.json")
        billing.BILLING_JSONL = os.path.join(tmp, "billing.jsonl")
        st0 = billing.budget_state("50k", 50000)
        ck("初始预算 150", abs(st0["remaining_cny"] - 150.0) < 1e-9, st0["remaining_cny"])
        billing.record_run("rid-1", "50k", {"cost_cny": 12.5, "tokens": 1000,
                                            "n_calls": 3, "source": "test",
                                            "since": "a", "until": "b"},
                           capital=50000, note="selftest")
        st1 = billing.budget_state("50k", 50000)
        ck("已花累计", abs(st1["spent_cny"] - 12.5) < 1e-9, st1["spent_cny"])
        ck("剩余减少", abs(st1["remaining_cny"] - 137.5) < 1e-9, st1["remaining_cny"])
        ck("运行次数", st1.get("runs") and len(st1["runs"]) >= 1)
        ck("流水文件存在", os.path.isfile(billing.BILLING_JSONL))
        # 奖励转入预算
        billing.add_bonus("50k", 30.0, "奖励")
        st2 = billing.budget_state("50k", 50000)
        ck("奖励转入预算", abs(st2["remaining_cny"] - 167.5) < 1e-9, st2["remaining_cny"])
        ck("预算按账本键取（与本金无关）",
           abs(billing.budget_state("50k", 100000)["remaining_cny"] - 167.5) < 1e-9)
    finally:
        config.OUT_DIR = old_out
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ C. grid
def t_grid() -> None:
    section("C. 低买高卖（网格）")
    ck("ma 不足返回 None", grid.ma([1, 2, 3], 5) is None)
    ck("ma 正确", abs(grid.ma([1, 2, 3, 4], 2) - 3.5) < 1e-12)
    ck("deviation 缺失", grid.deviation(1.0, None) is None)
    ck("deviation 正确", abs(grid.deviation(110, 100) - 0.10) < 1e-12)

    G = config.GRID
    base = float(G["base_weight"])
    ck("偏离 0 权重=base", abs(grid.target_weight(0.0, base) - base) < 1e-12)
    ck("跌 1 档加仓", grid.target_weight(-G["band_pct"], base) > base)
    ck("涨 1 档减仓", grid.target_weight(G["band_pct"], base) < base)
    ck("权重不越上界", grid.target_weight(-99, base) <= float(G["weight_ceiling"]) + 1e-12)
    ck("权重不越下界", grid.target_weight(99, base) >= float(G["weight_floor"]) - 1e-12)

    # 成本口径：断言跟着 config.CAPITAL 走，不写死绝对数（免五/费率一变就不会假红）
    import capital as _capc

    def _rt(n, inst="etf"):
        _e = inst != "stock"
        _b = _capc.trade_fee(n, is_etf=_e, side="buy")["total"]
        _s = _capc.trade_fee(n, is_etf=_e, side="sell")["total"]
        return (_b + _s) / n

    for _n in (1000, 2500, 5000):
        ck(f"ETF {_n} 元往返 = {_rt(_n):.4%}（grid 与 capital 同源）",
           abs(grid.round_trip_cost_rate(_n) - _rt(_n)) < 1e-9)
    ck(f"个股 5000 元往返 = {_rt(5000, 'stock'):.4%}（含印花）",
       abs(grid.round_trip_cost_rate(5000, "stock") - _rt(5000, "stock")) < 1e-9)
    if float(_capc.CAP["min_commission"] or 0) == 0:
        ck("免五：ETF 往返成本率与金额无关",
           abs(_rt(1000) - _rt(5000)) < 1e-6)
    else:
        ck("有最低收费：小单往返成本率更高", _rt(1000) > _rt(5000))

    # 成本门禁（2026-11-07 B 类 #3：单笔下限 2,500 → 5,000，年换手 4 → 2）
    ck("1000 元被拒", grid.gate_order(1000)[0] is False)
    ck("2500 元被拒（低于 5,000 下限）", grid.gate_order(2500)[0] is False)
    ck("5000 元通过", grid.gate_order(5000)[0] is True)
    ck("个股被拒", grid.gate_order(5000, "stock")[0] is False)
    ck("6000 元通过", grid.gate_order(6000)[0] is True)
    ck("网格单笔下限 = 5,000 元", float(config.GRID["min_order_cny"]) == 5000.0,
       config.GRID["min_order_cny"])
    ck("网格年换手上限 = 2 次往返", int(config.GRID["max_round_trips_per_year"]) == 2,
       config.GRID["max_round_trips_per_year"])

    # 回测不变量
    import math
    closes = [100 * (1 + 0.3 * math.sin(i / 40.0)) * (1 + 0.0002 * i) for i in range(1200)]
    r = grid.backtest(closes, G)
    ck("回测有结果", r is not None)
    ck("回测三策略都在", all(k in r for k in ("grid_return", "fixed_return", "bh_return")))
    ck("delta = 网格 − 固定",
       abs(r["delta_pp"] - (r["grid_return"] - r["fixed_return"]) * 100) < 1e-9)
    ck("MDD 非正", r["grid_mdd"] <= 0 and r["fixed_mdd"] <= 0 and r["bh_mdd"] <= 0)
    ck("序列太短返回 None", grid.backtest([1.0] * 50, G) is None)

    # 验证器：本征结论必须是「未通过」——回测没过就不许启用
    v = grid.verify(offline_csv=_write_grid_csv(closes))
    ck("verify 有 ok 字段", "ok" in v)
    ck("verify 有结论", bool(v.get("verdict") or v.get("reason")))
    if v.get("rows"):
        ck("verify 三个条件齐", set(v["conditions"]) == {"delta_ok", "turnover_ok", "mdd_ok"})
        ck("verify 基准是固定持有", "固定持有" in str(v.get("baseline")))
    ck("网格默认关闭", config.GRID["enabled"] is False)
    # 信号层：非 ETF 被剔除
    sg = grid.signals([{"code": "000999", "price": 10, "ma200": 9,
                        "current_weight": 0.05, "weight_value": 2500}])
    ck("signals 对个股也给结果", len(sg) == 1)


def _write_grid_csv(closes: list[float]) -> str:
    p = os.path.join(tempfile.mkdtemp(prefix="team_grid_"), "px.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("code,date,close\n")
        for i, c in enumerate(closes):
            f.write(f"T{i//250:04d},{2000 + i//250:04d}-{i%250+1:03d}-01,{c:.6f}\n")
    return p


# ============================================================ D. team
def t_team() -> None:
    section("D. 编排")
    # 契约表：每个字段都有产出方与消费方
    cs = team.handoff_contract()
    ck("契约非空", len(cs) >= 6)
    names = [c["name"] for c in cs]
    ck("契约名唯一", len(names) == len(set(names)))
    for c in cs:
        ck(f"契约 {c['name']} 有路径", bool(c.get("path")))
        ck(f"契约 {c['name']} 有列定义", bool(c.get("cols")))
        ck(f"契约 {c['name']} 有消费方", len(c.get("consumers") or []) >= 1)
        ck(f"契约 {c['name']} 产出方合法",
           c["producer"] in team.ROLES or c["producer"] == "engine")

    # 步骤表：T0–T10 齐全、owner 合法、T2/T4/T8/T9/T10 是 LLM 步
    ids = [s["id"] for s in team.STEPS]
    ck("步骤 T0 到 T10", ids == [f"T{i}" for i in range(11)], ids)
    for s in team.STEPS:
        ck(f"{s['id']} owner 合法", s["owner"] in team.ROLES or s["owner"] == "engine")
    ck("LLM 步骤集合", set(team.LLM_STEPS) == {"T2", "T4", "T8", "T9", "T10"}, team.LLM_STEPS)

    # 预算：五个角色占比合计 100%
    tot = sum(float(r["budget_pct"]) for r in team.ROLES.values())
    ck("角色预算合计 100%", abs(tot - 1.0) < 1e-9, tot)
    bp = team.budget_plan(50000, "50k")
    ck("预算分行 5 条", len(bp) == 5)
    ck("预算合计=年度预算",
       abs(sum(b["budget_cny"] for b in bp) - 150.0) < 0.02,
       sum(b["budget_cny"] for b in bp))
    ck("预算档位回退 50k",
       abs(sum(b["budget_cny"] for b in team.budget_plan(100000, "50k")) - 150.0) < 0.02)

    # 任务文件内容：每个 LLM 步都要写明「禁止事项」
    st = team.new_state(50000, "50k")
    ck("state 有 11 步", len(st["steps"]) == 11)
    ck("state 初始全 pending", all(v["status"] == "pending" for v in st["steps"].values()))
    ck("run_id 唯一", team.new_state(50000, "50k")["run_id"] != st["run_id"])
    team.mark(st, "T0", "ok", "自测")
    ck("mark 生效", st["steps"]["T0"]["status"] == "ok")

    for sid in ("T2", "T4", "T8", "T9", "T10"):
        txt = team.TASK_BUILDERS[sid](st)
        ck(f"{sid} 任务有角色", "**角色**" in txt)
        ck(f"{sid} 任务有禁止", "**禁止**" in txt)
        ck(f"{sid} 任务有上下文", "run_id" in txt)
    ck("T2 不许碰取数", "datasource" in team.task_T2(st))
    ck("T2 只做减法", "reject" in team.task_T2(st))

    # T2 背景段：第 4 步事件面 + 档位（只作背景，不改权限）
    _t2 = team.task_T2(st)
    ck("T2 带事件面输入", "news_events.csv" in _t2 and "news_brief.md" in _t2)
    ck("T2 带档位背景", "regime.json" in _t2)
    ck("T2 事件面只认池内", "只认候选池内代码" in _t2)
    ck("T2 档位不改权限", "不许因为牛熊" in _t2 and "不许只写「因为熊市」" in _t2)
    ck("T2 篮子映射来自 config",
       any(f"≤{float(c):.0%}" in team._basket_ladder()
           for c, _ in config.EXEC["max_stock_names_by_ratio"]),
       team._basket_ladder())

    # regime_summary：缺文件 fail-soft；合成文件能读出主指数回撤/宽度
    _old_out = config.OUT_DIR
    _tmp = tempfile.mkdtemp()
    try:
        config.OUT_DIR = _tmp
        ck("缺 regime.json → exists=False", team.regime_summary()["exists"] is False)
        ck("缺事件面 → T2 写「未生成」", "未生成" in team.task_T2(st))
        team._write(os.path.join(_tmp, "regime.json"), json.dumps({
            "generated_at": "2026-11-21 10:00:00", "main_index": "000300",
            "indexes": [{"code": "000300", "dev200": -0.14, "dd_2y": -0.218},
                        {"code": "000905", "dev200": -0.24, "dd_2y": -0.33}],
            "breadth": {"ratio": 0.197, "above": 314, "total": 1596},
            "state": "熊市确认", "stance": "bear", "severity": "熊",
            "stance_why": "熊市确认进入（回撤 -21.8%，宽度 19.7%）"}, ensure_ascii=False))
        _rg = team.regime_summary()
        ck("regime_summary 读档位", _rg["exists"] and _rg["severity"] == "熊")
        ck("regime_summary 读主指数回撤", abs(float(_rg["dd_2y"]) + 0.218) < 1e-9,
           _rg["dd_2y"])
        ck("regime_summary 读宽度", abs(float(_rg["breadth"]) - 0.197) < 1e-9,
           _rg["breadth"])
        _t2b = team.task_T2(st)
        ck("T2 任务书渲染档位", "市场档位：熊" in _t2b)
        ck("T2 任务书渲染回撤", "-21.8%" in _t2b)
        ck("T2 缺事件面不许猜", "不许猜事件" in _t2b)

        # 旧格式 regime.json（只有 state、无 stance/severity）→ 按 state 推断并告警
        team._write(os.path.join(_tmp, "regime.json"), json.dumps({
            "generated_at": "2026-11-21 10:12:14", "main_index": "000300",
            "indexes": [{"code": "000300", "dev200": -0.14, "dd_2y": -0.218}],
            "breadth": {"ratio": 0.197, "above": 314, "total": 1596},
            "state": "熊市确认"}, ensure_ascii=False))
        _rgl = team.regime_summary()
        ck("旧格式 regime → 按 state 推断档位",
           _rgl["legacy"] is True and _rgl["severity"] == "熊", str(_rgl.get("severity")))
        ck("旧格式 regime → 任务书告警", "旧格式" in team.task_T2(st))
    finally:
        config.OUT_DIR = _old_out
        shutil.rmtree(_tmp, ignore_errors=True)
    ck("T10 只读", "只读不改" in team.task_T10(st))
    _t10 = team.task_T10(st)
    ck("T10 含策略复盘（月报必写）", "策略复盘" in _t10)
    ck("T10 含技能优化清单", "技能优化" in _t10)
    ck("T10 分 A/B 类", "A 类" in _t10 and "B 类" in _t10)
    ck("T10 A 类清单可勾选", "- [ ]" in _t10)
    ck("retro_missing 两节齐全 → 空",
       team.retro_missing("## 六、上月策略复盘\n- x\n## 七、技能优化\n- y") == [])
    ck("retro_missing 缺技能优化", team.retro_missing("## 策略复盘") == ["技能优化"])
    ck("retro_missing 全缺 → 两个", len(team.retro_missing("")) == 2)
    ck("T9 不许破风控", "风控" in team.task_T9(st))

    # 数据体检：结构完整（不判内容）
    chk = team.check_data()
    ck("体检有 ok", "ok" in chk)
    ck("体检有 checked_at", bool(chk.get("checked_at")))
    ck("体检 4 个文件（含 L1 快照）", len(chk["files"]) == 4, len(chk["files"]))
    ck("体检清单含 data_snapshot.csv",
       any(f["file"] == "data_snapshot.csv" for f in chk["files"]))
    for f in chk["files"]:
        ck(f"体检 {f['file']} 有 exists", "exists" in f)
        ck(f"体检 {f['file']} 有 fresh", "fresh" in f)

    # plan_mdd / paper_return 不崩（允许返回 None）
    ck("plan_mdd 可调用", team.plan_mdd() is None or isinstance(team.plan_mdd(), float))
    ck("paper_return 可调用", team.paper_return("50k") is None
       or isinstance(team.paper_return("50k"), float))

    # status 不崩
    s = team.status()
    ck("status 有结构", "state" in s)

    # 配置自洽
    ck("TEAM.enabled", config.TEAM.get("enabled") is True)
    ck("评分下界 1.3%（银行利率）",
       abs(float(config.SCORE["floor_rate"]) - 0.013) < 1e-9, config.SCORE["floor_rate"])
    ck("奖励比例 B- 5%", abs(config.SCORE["reward_pct"]["B-"] - 0.05) < 1e-9)
    ck("奖励比例 B 5%", abs(config.SCORE["reward_pct"]["B"] - 0.05) < 1e-9)
    ck("奖励比例 B+ 7%", abs(config.SCORE["reward_pct"]["B+"] - 0.07) < 1e-9)
    ck("奖励比例 A 10%", abs(config.SCORE["reward_pct"]["A"] - 0.10) < 1e-9)
    ck("风险否决 20%", abs(float(config.SCORE["drawdown_veto"]) - 0.20) < 1e-9)


# ============================================================ E. 分阶段执行
def t_stage() -> None:
    section("E. 分阶段执行（--stage / --ingest）")
    import inspect
    import shutil
    import tempfile

    ck("STAGE_ORDER 覆盖 LLM 步骤", team.STAGE_ORDER == ["T2", "T3", "T4", "T8", "T9", "T10"],
       team.STAGE_ORDER)
    ck("CSV_SPEC 只含 T2/T4", set(team.CSV_SPEC) == {"T2", "T4"})
    ck("T2 契约列含 action", "action" in team.CSV_SPEC["T2"][1])
    ck("T4 契约列含 source", "source" in team.CSV_SPEC["T4"][1])

    ck("代码识别 6 位", team._is_code("000001"))
    ck("代码识别带前缀", team._is_code("sh510300"))
    ck("非代码 true 不算代码", not team._is_code("true"))
    ck("空串不算代码", not team._is_code(""))
    ck("_split_codes 混合分隔", len(team._split_codes("000001,000002; 000003")) == 3)
    ck("_split_codes 全角逗号", len(team._split_codes("300274，000568")) == 2)

    rows = [
        {"code": "000001", "action": "keep", "need_deep": "300274, 000568"},
        {"code": "000002", "action": "reject", "need_deep": "600519"},
        {"code": "000003", "action": "downgrade", "need_deep": ""},
        {"code": "000004", "action": "keep", "need_deep":
            "000005,000006,000007,000008,000009,000010,000011,000012,"
            "000013,000014,000015,000016"},
    ]
    codes, bad = team._need_deep(rows)
    ck("reject 不进 need_deep", "600519" not in codes)
    ck("need_deep 去重后 ≤10", len(codes) == 10, len(codes))
    ck("合法 CSV 无 bad", bad == [])

    _, bad2 = team._need_deep([{"code": "000001", "action": "keep", "need_deep": "true"}])
    ck("旧契约 true 被判 bad", bad2 == ["true"], bad2)

    ck("未知步骤 stage 被拒", team.stage("T11", 50000.0, "50k")["ok"] is False)
    ck("未知步骤 ingest 被拒", team.ingest("T11", 50000.0, "50k")["ok"] is False)

    ck("T2 任务书声明执行者身份", "你是执行者" in inspect.getsource(team.task_T2))
    ck("T4 任务书声明执行者身份", "你是执行者" in inspect.getsource(team.task_T4))
    ck("T9 任务书：Lead 亲自写/派执行者", "你是执行者" in inspect.getsource(team.task_T9))
    ck("T3 任务书含深取目录", "out/deep/<code>.json" in inspect.getsource(team.task_T3))
    ck("run 支持 resume 续跑", "resume" in inspect.signature(team.run).parameters)
    ck("T9 任务书不再写余额口径 settle",
       "--settle" not in inspect.getsource(team.task_T8))

    # ---- T8 结算时点（2026-11-07 B 类 #5）：pipeline 在 weekly 之前跑，净值还没落账，
    # 自动跑 T8 只会得到「净收益 0.00%/D」的假象 → 改成 weekly 之后单独 --run-t8。
    import sys as _sys
    ck("run 支持 t8 开关", "t8" in inspect.signature(team.run).parameters)
    ck("do_t8 独立成函数", callable(getattr(team, "do_t8", None)))
    _src_run = inspect.getsource(team.run)
    ck("do_t8 声明结算时点", "结算时点" in inspect.getsource(team.do_t8))
    ck("pipeline 跳过 T8/T9", "T8/T9 跳过" in _src_run)
    ck("T8 专属路径先评分后 T9 骨架",
       "do_t8" in _src_run and "do_t9" in _src_run
       and _src_run.index("do_t8") < _src_run.index("do_t9"))
    ck("CLI 有 --run-t8", "--run-t8" in inspect.getsource(team.main))
    ck("CLI 有 --score-only", "--score-only" in inspect.getsource(team.main))
    ck("run 支持 sketch 开关", "sketch" in inspect.signature(team.run).parameters)
    ck("T9 任务书不再要求 scorecard",
       "out/scorecard.md" not in inspect.getsource(team.task_T9))
    ck("T9 任务书声明决策日不评分",
       "决策日不评分" in inspect.getsource(team.task_T9))
    _t9 = inspect.getsource(team.task_T9)
    ck("T9 任务书要求写「下周日巡检附加任务」", "下周日巡检附加任务" in _t9)
    ck("T9 巡检任务允许写「无」但不许跳过", "没有就写「无」" in _t9)
    _sk = inspect.getsource(team.do_t9)
    ck("T9 骨架含巡检附加任务占位", "下周日巡检附加任务" in _sk)

    captured = {}
    _orig_run = team.run
    _argv = _sys.argv

    def _fake_run(*args, **kw):
        captured.update(kw)
        captured["args"] = args
        return {"log": [], "aborted": False, "t8": kw.get("t8")}

    team.run = _fake_run
    _sys.argv = ["team.py", "--run-t8", "--capital", "50000", "--ledger", "50k", "--json"]
    try:
        rc = team.main()
    finally:
        team.run = _orig_run
        _sys.argv = _argv
    ck("--run-t8 走 t8 分支", captured.get("t8") is True, captured)
    ck("--run-t8 不重跑管线", captured.get("pipeline") is False, captured)
    ck("--run-t8 本金/账本照传", captured.get("args")[:2] == (50000.0, "50k"), captured)
    ck("--run-t8 返回 0", rc == 0, rc)
    ck("--run-t8 默认生成 T9 骨架", captured.get("sketch") is True, captured)

    captured2 = {}

    def _fake_run2(*args, **kw):
        captured2.update(kw)
        captured2["args"] = args
        return {"log": [], "aborted": False}

    team.run = _fake_run2
    _sys.argv = ["team.py", "--run-t8", "--score-only", "--capital", "50000",
                 "--ledger", "50k", "--json"]
    try:
        team.main()
    finally:
        team.run = _orig_run
        _sys.argv = _argv
    ck("--score-only 不覆盖 T9 骨架", captured2.get("sketch") is False, captured2)

    # T2 拒收时不得把 T2 记成 ok（否则下一次 ingest 会以为已交卷）
    _st2 = {"meta": {}, "steps": {"T2": {"status": "pending", "at": "2026-11-21T20:00:00"}}}
    _orig_t2 = (team._state_for, team._csv_rows, team.mark, team.save_state)
    _marks = []
    try:
        team._state_for = lambda *a, **k: _st2
        team._csv_rows = lambda name, cols: {
            "ok": True, "path": "x",
            "rows": [{"code": "600000", "action": "BUY", "need_deep": ""}]}
        team.mark = lambda st, sid, status, msg="": _marks.append((sid, status))
        team.save_state = lambda st: None
        _r_bad = team.ingest("T2", 50000.0, "50k")
    finally:
        team._state_for, team._csv_rows, team.mark, team.save_state = _orig_t2
    ck("T2 非法 action 被拒收", _r_bad["ok"] is False, _r_bad)
    ck("T2 拒收不标 ok", all(s != "ok" for _, s in _marks), _marks)

    # T9 计划书：只查「文件存在」会让空白/缺段/上周遗留的计划书静默过关。
    _t9_dir = tempfile.mkdtemp(prefix="t9plan_")
    _orig_ing9 = (team.TEAM_DIR, team._state_for, team.mark, team.save_state)
    _marks9 = []

    def _st9(at="2020-01-01 00:00:00"):
        return {"meta": {}, "started": "2026-11-21 20:00:00",
                "steps": {"T9": {"status": "pending", "at": at}}}

    try:
        team.TEAM_DIR = _t9_dir
        team.mark = lambda st, sid, status, msg="": _marks9.append((sid, status))
        team.save_state = lambda st: None
        _st_cur = _st9()
        team._state_for = lambda *a, **k: _st_cur
        _r_missing = team.ingest("T9", 50000.0, "50k")
        ck("T9 缺文件拒收", _r_missing["ok"] is False and "缺" in _r_missing["reason"],
           _r_missing)

        _p9 = os.path.join(_t9_dir, "lead_plan.md")
        _full = ("# 计划书\n\n## 1. 本期结论\n" + "市场熊市确认，按纪律不建新仓。" * 8 +
                 "\n\n## 2. 策略调整\n" + "本周无 A 类调整，B 类记 pending。" * 8 +
                 "\n\n## 3. 未来 1–3 期规划\n" + "等 regime 修复再谈加仓。" * 8 +
                 "\n\n## 4. 计费核对\n" + "净利润按脚本口径，本次不结算。" * 8)

        with open(_p9, "w", encoding="utf-8") as f:
            f.write(_full.replace("## 3. 未来 1–3 期规划", "## 3. 别的段落"))
        _r_seg = team.ingest("T9", 50000.0, "50k")
        ck("T9 缺段落拒收", _r_seg["ok"] is False and "缺段落" in _r_seg["reason"],
           _r_seg)

        with open(_p9, "w", encoding="utf-8") as f:
            f.write("## 本期结论\n行\n## 策略调整\n行\n## 未来\n行\n## 计费核对\n行\n")
        _r_short = team.ingest("T9", 50000.0, "50k")
        ck("T9 正文太短拒收", _r_short["ok"] is False and "太短" in _r_short["reason"],
           _r_short)

        with open(_p9, "w", encoding="utf-8") as f:
            f.write(_full)
        _st_old = _st9(at="2099-01-01 00:00:00")
        team._state_for = lambda *a, **k: _st_old
        _r_stale = team.ingest("T9", 50000.0, "50k")
        ck("T9 上周遗留计划书拒收", _r_stale["ok"] is False
           and "旧" in _r_stale["reason"], _r_stale)

        with open(_p9, "w", encoding="utf-8") as f:
            f.write(_full + "\n> 骨架标记 `T9_SKELETON`：这是 do_t9 生成的骨架，ingest 会拒收。\n")
        _st_cur = _st9()
        team._state_for = lambda *a, **k: _st_cur
        _r_skel = team.ingest("T9", 50000.0, "50k")
        ck("T9 骨架未填拒收", _r_skel["ok"] is False and "骨架" in _r_skel["reason"],
           _r_skel)

        _marks9.clear()
        with open(_p9, "w", encoding="utf-8") as f:
            f.write(_full)
        _r_ok9 = team.ingest("T9", 50000.0, "50k")
        ck("T9 四段齐全通过", _r_ok9["ok"] is True and _r_ok9.get("next") is None, _r_ok9)
        ck("T9 通过才标 ok", ("T9", "ok") in _marks9, _marks9)
    finally:
        team.TEAM_DIR, team._state_for, team.mark, team.save_state = _orig_ing9
        shutil.rmtree(_t9_dir, ignore_errors=True)

    # T4 之后下一步指向 T9（决策日不评分）：先 weekly 落 nav、review 验收，再由 Lead 写计划书。
    _orig_ing = (team._state_for, team.mark, team.save_state, team._csv_rows, team.run)
    try:
        team._state_for = lambda *a, **k: {"meta": {}, "steps": {}}
        team.mark = lambda *a, **k: None
        team.save_state = lambda st: None
        team._csv_rows = lambda name, cols: {
            "ok": True, "rows": [{"code": "600000", "name": "X", "source": "t"}]}
        team.run = lambda *a, **k: {"aborted": False, "log": ["pipeline"]}
        _r4 = team.ingest("T4", 50000.0, "50k")
    finally:
        (team._state_for, team.mark, team.save_state,
         team._csv_rows, team.run) = _orig_ing
    ck("T4 之后指向 T9", _r4.get("next") == "T9", _r4)
    ck("T4 提示先 weekly 再 T9", "weekly.py" in (_r4.get("hint") or ""), _r4)
    ck("T4 提示说明评分在月报", "月报" in (_r4.get("hint") or ""), _r4)

    old = config.OUT_DIR
    tmp = tempfile.mkdtemp(prefix="team_stage_")
    try:
        config.OUT_DIR = tmp
        ck("缺文件被拦", team._csv_rows("nope.csv", ["code"])["ok"] is False)
        with open(os.path.join(tmp, "empty.csv"), "w", encoding="utf-8") as f:
            f.write("code,action\n")
        ck("空表被拦", team._csv_rows("empty.csv", ["code", "action"])["ok"] is False)
        with open(os.path.join(tmp, "bad.csv"), "w", encoding="utf-8") as f:
            f.write("code\n000001\n")
        r = team._csv_rows("bad.csv", ["code", "action"])
        ck("缺列被拦", r["ok"] is False and "缺列" in r["reason"], r.get("reason"))
        with open(os.path.join(tmp, "ok.csv"), "w", encoding="utf-8-sig") as f:
            f.write("code,action,reason\n000001,keep,ok\n")
        r = team._csv_rows("ok.csv", ["code", "action"])
        ck("合法表通过", r["ok"] is True and len(r["rows"]) == 1)

        # 旧产物保护：上一轮遗留的 CSV 不能被静默当成本轮结论（out/ 跨周共用）
        p_ok = os.path.join(tmp, "ok.csv")
        now = _dt.datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        ck("产物比任务书新 → 通过",
           team._artifact_fresh(p_ok, (now - _dt.timedelta(minutes=1)).strftime(fmt))[0] is True)
        stale_ok, stale_why = team._artifact_fresh(
            p_ok, (now + _dt.timedelta(minutes=1)).strftime(fmt))
        ck("产物比任务书旧 → 拒收", stale_ok is False)
        ck("拒收理由写明「上一轮遗留」", "上一轮" in stale_why, stale_why)
        ck("任务书时间缺失 → 放行", team._artifact_fresh(p_ok, None)[0] is True)
        ck("任务书时间格式错 → 放行", team._artifact_fresh(p_ok, "坏时间")[0] is True)
    finally:
        config.OUT_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def t_data() -> None:
    section("F. T0 数据新鲜度参考日（2026-11-21 S4 A 类回归）")
    old = config.OUT_DIR
    tmp = tempfile.mkdtemp(prefix="team_t0_")
    try:
        config.OUT_DIR = tmp
        today = _dt.date(2026, 11, 21)
        ck("没有 stage3_plan.json → 参考日=今天", team.product_ref_date(today) == today)
        p = os.path.join(tmp, "stage3_plan.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"data_as_of": "2026-11-20"}, f)
        ck("参考日取 data_as_of（周五数据日）",
           str(team.product_ref_date(today)) == "2026-11-20", team.product_ref_date(today))
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"data_as_of": "2099-01-01"}, f)
        ck("data_as_of 在未来 → 回退今天", team.product_ref_date(today) == today)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken json")
        ck("产物损坏 → 回退今天", team.product_ref_date(today) == today)

        # check_data 用参考日判新鲜度：周五生成的产物在周六决策日不该被判过期
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"data_as_of": "2026-11-20"}, f)
        import time as _time
        _ts = _time.mktime(_dt.datetime(2026, 11, 20, 18, 58).timetuple())
        for fn in ["stage1_candidates.csv", "stage2_judged.csv", "data_snapshot.csv"]:
            fp = os.path.join(tmp, fn)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("code\n600000\n")
            os.utime(fp, (_ts, _ts))
        os.utime(p, (_ts, _ts))
        _d = team.check_data()
        ck("T0 参考日 = 2026-11-20", _d.get("ref_date") == "2026-11-20", _d.get("ref_date"))
        ck("周五产物在周六决策日 → 新鲜（不再误杀）", _d.get("ok") is True, _d.get("stale"))
        os.utime(os.path.join(tmp, "stage2_judged.csv"), (_ts - 3 * 86400, _ts - 3 * 86400))
        _d2 = team.check_data()
        ck("早于参考日的产物 → 仍判过期", _d2.get("ok") is False and
           "stage2_judged.csv" in (_d2.get("stale") or []), _d2.get("stale"))
    finally:
        config.OUT_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    t_score()
    t_billing()
    t_grid()
    t_team()
    t_stage()
    t_data()
    print(f"\n{'=' * 60}\n断言 {_n} 项｜失败 {_fail} 项")
    if _fail:
        print("结果：FAIL")
        return 1
    print("结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
