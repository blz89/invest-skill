# -*- coding: utf-8 -*-
"""_selftest_review.py — 记分卡风险组合：场外 R2 逐只计入 + 单只上限回归（离线）。

背景（2026-11-14 A 类修复）：review.py 原先把整条场外 R2 影子账合成一个 `OTC:R2`
仓位，3 只各约 4.8% 的债券基金被误算成「单只 14.0% > R2 上限 12%」，记分卡出现
假违规。本套件锁住：
  ① 场外基金逐只成条、权重=占本金；
  ② 3 只小仓合起来不再触发单只上限（假违规回归）；
  ③ 真正单只超限（14%）仍被拦；
  ④ 场内持仓路径不变；
  ⑤ 边界（空账本、缺 level）。

2026-11-25 追加「⑦ 多周期加权评分」：季 50% / 年 20% / 长期 30%，各自折年化
（用户口径「都要折算成年化率」），不满 12 周的窗口只显示不判档；并锁住
judge() 在新口径（multi 满样本）与老口径（退回整段净年化）之间的切换。

运行：& $py _selftest_review.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import review  # noqa: E402
import risk  # noqa: E402
import config  # noqa: E402
import score  # noqa: E402
import capital_dyn  # noqa: E402

n = 0
fails = []


def check(name, cond):
    global n
    n += 1
    if not cond:
        fails.append(name)
        print(f"✗ {name}")
    else:
        print(f"✓ {name}")


def _otc(code, name, amount, nav=1.0, level="R2"):
    return {"code": code, "name": name, "level": level, "amount": amount,
            "acc_nav": 1.0, "nav": nav}


def main() -> int:
    _orig = review.paper.portfolio
    review.paper.portfolio = lambda st, px: {"nav": 50000.0}
    try:
        print("== 一、场外 R2 逐只计入 ==")
        st = {"holdings": {}, "otc_holdings": {
            "OTC:005025": _otc("OTC:005025", "交银丰盈收益债券C", 2400.0),
            "OTC:016800": _otc("OTC:016800", "建信鑫和30天持有期债券C", 2300.0),
            "OTC:018193": _otc("OTC:018193", "建信鑫弘180天持有期债券C", 2300.0)}}
        items = review.risk_items(st)
        check("3 只场外基金各成一条", len(items) == 3)
        check("代码逐只保留", {i["code"] for i in items} == set(st["otc_holdings"]))
        ws = sorted(round(i["weight"], 4) for i in items)
        check("权重=占本金（4.6%/4.6%/4.8%）", ws == [0.046, 0.046, 0.048])
        check("不再出现 OTC:R2 合并仓位", all(i["code"] != "OTC:R2" for i in items))

        print("\n== 二、假违规回归（3 只小仓不再触发单只上限） ==")
        rv = risk.portfolio_review(items)
        check("组合评审 ok", rv["ok"] is True)
        check("无「单只占本金」违规", not any("单只占本金" in v for v in rv["violations"]))

        print("\n== 三、真实单只超限仍拦 ==")
        st2 = {"holdings": {}, "otc_holdings": {
            "OTC:BIG": _otc("OTC:BIG", "单只超标基金", 7000.0)}}
        rv2 = risk.portfolio_review(review.risk_items(st2))
        check("单只 14% > 12% → 违规", any("单只占本金" in v for v in rv2["violations"]))
        check("违规信息带基金代码", any("OTC:BIG" in v for v in rv2["violations"]))

        print("\n== 四、场内持仓路径不变 ==")
        st3 = {"holdings": {"600000": {"name": "浦发银行", "shares": 100,
                                       "last_close": 5.0, "level": "R3"}},
               "otc_holdings": {}}
        items3 = review.risk_items(st3)
        check("场内 1 条", len(items3) == 1 and items3[0]["code"] == "600000")
        check("场内权重 1%", abs(items3[0]["weight"] - 0.01) < 1e-9)

        print("\n== 五、边界 ==")
        check("空账本 → 空条目",
              review.risk_items({"holdings": {}, "otc_holdings": {}}) == [])
        st4 = {"holdings": {}, "otc_holdings": {"OTC:NOLV": {
            "code": "OTC:NOLV", "name": "无等级", "amount": 1000.0,
            "acc_nav": 1.0, "nav": 1.0}}}
        items4 = review.risk_items(st4)
        check("缺 level 默认 R2", bool(items4) and items4[0]["level"] == "R2")

        print("\n== 六、权益层为负提示按总收益符号分支（2026-11-14 A 类） ==")
        _w1 = review.equity_warning(0.012, -800.0, 650.0)
        check("总收益为正 + 权益为负 → 吃利息提示",
              bool(_w1) and "总收益为正" in _w1)
        _w2 = review.equity_warning(-0.002, -800.0, 650.0)
        check("总收益为负 + 权益为负 → 权益亏损提示",
              bool(_w2) and "累计收益为负" in _w2)
        check("权益为正 → 无提示", review.equity_warning(0.012, 100.0, 650.0) is None)
        check("无现金利息 → 无提示", review.equity_warning(-0.002, -800.0, 0.0) is None)
        check("权益盈亏缺失 → 无提示", review.equity_warning(0.012, None, 650.0) is None)
    finally:
        review.paper.portfolio = _orig

    print("\n== 七、多周期加权评分：季50/年20/长期30，各自折年化（2026-11-25） ==")
    import score  # noqa: F401 — review 已 import，这里显式拿引用

    # 1) 年化折算（用户 2026-11-25 拍板：都要折算成年化率）
    _a = score.annualize_rate(0.012, 84)
    check("季 1.2% / 84 天 → 年化 ≈5.32%",
          _a is not None and abs(_a - ((1.012 ** (365.0 / 84)) - 1)) < 1e-12)
    check("天数 <1 → None（不编数）", score.annualize_rate(0.012, 0) is None)
    check("1+ret ≤ 0 → None", score.annualize_rate(-1.5, 84) is None)
    check("缺参 → None", score.annualize_rate(None, 84) is None)

    # 2) 三项都满样本：加权 = 0.5·季 + 0.2·年 + 0.3·长期
    _w = {"quarter": {"net_return": 0.012, "days": 84},
          "year": {"net_return": 0.020, "days": 364},
          "long": {"net_return": 0.025, "days": 1095}}
    _c = score.composite(_w, deposit_rate=0.013)
    _exp = sum(float(score.config.PERF["period_weights"][k])
               * score.annualize_rate(_w[k]["net_return"], _w[k]["days"])
               for k in ("quarter", "year", "long"))
    check("加权年化 = 0.5·季 + 0.2·年 + 0.3·长期",
          _c["score_ann"] is not None and abs(_c["score_ann"] - _exp) < 1e-12)
    check("三项都满样本", _c["sample_ok"] is True
          and set(_c["used_windows"]) == {"quarter", "year", "long"})
    check("used 权重和 = 1", abs(sum(t["weight_used"] for t in _c["terms"].values()) - 1.0) < 1e-12)
    check("超额 = 加权年化 − 定存",
          abs(_c["excess_ann"] - (_c["score_ann"] - 0.013)) < 1e-12)
    check("档位按加权年化判（≈3.31% → B+）", _c["grade"] == "B+")
    check("evaluate_multi 的 bar_ok 与 weighted>定存 一致",
          score.evaluate_multi(_w, deposit_rate=0.013)["bar_ok"] is True)

    # 3) 三项都不满 12 周 → 只显示不判档（用户账本现状）
    _c2 = score.composite({k: {"net_return": 0.004, "days": 27}
                           for k in ("quarter", "year", "long")}, deposit_rate=0.013)
    check("三项都 27 天 → sample_ok False", _c2["sample_ok"] is False)
    check("→ score_ann None、不判档", _c2["score_ann"] is None and _c2["grade"] is None)
    check("→ note 明说样本不足", "样本不足" in (_c2["note"] or ""))

    # 4) 部分窗口满：权重按比例重分配（年度只有 60 天 < 84 天门槛 → 不参与加权）
    _c3 = score.composite({"quarter": {"net_return": 0.012, "days": 84},
                           "year": {"net_return": 0.020, "days": 60},
                           "long": {"net_return": 0.050, "days": 1095}}, deposit_rate=0.013)
    check("只剩季+长期 → 权重 50/30 重分配为 62.5%/37.5%",
          abs(_c3["terms"]["quarter"]["weight_used"] - 0.625) < 1e-12
          and abs(_c3["terms"]["long"]["weight_used"] - 0.375) < 1e-12)
    check("未满样本的年度不进加权", _c3["terms"]["year"]["weight_used"] == 0.0)
    check("missing_windows 标出年度", _c3["missing_windows"] == ["year"])

    # 5) windows_from_nav：窗口内 token 实花、账本不足窗口时退回成立日
    _navs = [{"date": "2026-10-28", "nav": 50000.0},
             {"date": "2026-11-24", "nav": 49869.84}]
    _runs = [{"ended_at": "2026-11-10 20:00:00", "cost_cny": 1.5},
             {"ended_at": "2026-09-01 20:00:00", "cost_cny": 9.9}]   # 窗口外，不许计入
    _wf = score.windows_from_nav(_navs, as_of="2026-11-24", runs=_runs, capital=50000.0)
    check("三窗口都退回成立日（inception）",
          all(_wf[k]["inception"] for k in ("quarter", "year", "long")))
    check("窗口外 token 不计入（只算 1.5 元）",
          abs(_wf["quarter"]["token_cost_cny"] - 1.5) < 1e-12)
    check("净收益 = 毛收益 − token/本金",
          abs(_wf["quarter"]["net_return"]
              - ((49869.84 / 50000.0 - 1.0) - 1.5 / 50000.0)) < 1e-12)
    check("days = 27（2026-10-28 → 11-24）", int(_wf["quarter"]["days"]) == 27)

    # 6) judge：样本满 + 加权年化低于定存 → 未达标且理由是加权口径
    _net_multi = {"net_pnl": 50.0, "token_spent": 20.0, "net_annualized": 0.02,
                  "net_return": 0.001, "deposit_rate": 0.013,
                  "multi": {"sample_ok": True, "score_ann": 0.004,
                            "deposit_rate": 0.013, "terms": {}}}
    _lbl, _why = review.judge({"weeks": 12}, {"weeks": 12}, 0.0, -0.01, 0.0007, 12, 0.0,
                              net=_net_multi)
    check("满样本 + 加权年化 0.4% < 1.3% → 未达标", _lbl == "未达标")
    check("理由点名「加权年化」", any("加权年化" in r for r in _why))
    _net_multi["multi"]["score_ann"] = 0.03
    _lbl2, _why2 = review.judge({"weeks": 12}, {"weeks": 12}, 0.0, -0.01, 0.0007, 12, 0.0,
                                net=_net_multi)
    check("加权年化 3% > 1.3% + 净利润>0 → 暂达标", _lbl2 == "暂达标（跑赢存款）")

    # 7) 老口径回归：没有 multi 时仍按整段净年化判
    _net_old = {"net_pnl": 50.0, "token_spent": 20.0, "net_annualized": 0.005,
                "net_return": 0.001, "deposit_rate": 0.013}
    _lbl3, _why3 = review.judge({"weeks": 12}, {"weeks": 12}, 0.0, -0.01, 0.0007, 12, 0.0,
                                net=_net_old)
    check("无 multi → 退回整段净年化判未达标", _lbl3 == "未达标")
    check("老口径理由带「扣 token 实花后年化」", any("扣 token 实花后年化" in r for r in _why3))

    # 8) 样本不足时也要把多周期窗口摆出来（不判档）
    _lbl4, _why4 = review.judge({"weeks": 4}, {"weeks": 4}, 0.0, -0.01, 0.0007, 0, 0.0,
                                net={"net_pnl": -150.0, "deposit_rate": 0.013,
                                     "multi": {"sample_ok": False, "min_window_days": 84,
                                               "terms": {"quarter": {"days": 27}}}})
    check("样本不足 → 判定样本不足", _lbl4 == "样本不足")
    check("但理由里带多周期窗口与 84 天门槛",
          any("多周期加权" in r and "84" in r for r in _why4))

    # ---- 八、季度计价（B 类 #22）：本季累计是主指标，年化只是外推脚注 ----
    print("\n八、季度口径")

    def ck(name, cond, actual=""):
        check(name if not actual else f"{name}（实际 {actual}）", bool(cond))

    ck("config.PERF.period = quarter", str(config.PERF.get("period")) == "quarter",
       str(config.PERF.get("period")))
    ck("季达标线 = 年化达标线/4",
       abs(float(config.PERF.get("accept_quarter_rate")) - float(config.PERF["deposit_rate"]) / 4) < 1e-9,
       str(config.PERF.get("accept_quarter_rate")))
    ck("token 季度预算 = 年度预算/4",
       abs(float(config.PERF.get("token_cost_quarter_cny")) - 150.0 / 4) < 1e-9,
       str(config.PERF.get("token_cost_quarter_cny")))

    _navs = [{"date": "2026-10-01", "nav": 50000.0},
             {"date": "2026-11-20", "nav": 50100.0}]
    _runs = [{"ended_at": "2026-11-01 10:00:00", "cost_cny": 10.0},
             {"ended_at": "2026-09-20 10:00:00", "cost_cny": 5.0}]   # 季外，不能计入
    _q = score.quarter_metrics(_navs, as_of="2026-11-20", runs=_runs, capital=50000.0,
                               deposit_rate=0.013)
    ck("本季窗口 = 自然季首日 → as_of（10-01 → 11-20）",
       _q.get("start") == "2026-10-01" and _q.get("end") == "2026-11-20", str(_q.get("start")))
    ck("季内 token 只计本季（10 元，不含季外 5 元）",
       abs(float(_q.get("token_cost_cny")) - 10.0) < 1e-6, str(_q.get("token_cost_cny")))
    ck("本季净收益率 = 账户 0.2% − token 10/50000",
       abs(float(_q.get("net_return")) - (0.002 - 10.0 / 50000.0)) < 1e-9,
       str(_q.get("net_return")))
    ck("季目标 = 0.325%（162.5 元）",
       abs(float(_q.get("target_rate")) - 0.00325) < 1e-9
       and abs(float(_q.get("target_cny")) - 162.5) < 1e-6, str(_q.get("target_cny")))
    ck("季末有年化外推值", _q.get("net_annualized_extrapolated") is not None)

    _q2 = score.quarter_metrics(_navs + [{"date": "2027-01-05", "nav": 50200.0}],
                                as_of="2027-01-05", runs=_runs, capital=50000.0,
                                deposit_rate=0.013)
    ck("跨季自动换到 2027Q1", _q2.get("quarter") == "2027Q1", str(_q2.get("quarter")))
    ck("季初早于账本起点 → 标 inception（自建账起）",
       score.quarter_metrics([{"date": "2026-11-20", "nav": 50000.0}],
                             as_of="2026-11-20", capital=50000.0).get("inception") is True)
    ck("没有净值点 → available=False，不抛异常",
       score.quarter_metrics([], as_of="2026-11-20", capital=50000.0).get("available") is False)

    # 档位满 12 周才出：渲染层按 net.multi_sample_ok 决定印「—」
    _net_ns = {"grade": "D", "multi_sample_ok": False,
               "quarter": {"available": True, "quarter": "2026Q4", "net_return": -0.003,
                           "target_rate": 0.00325, "bar_ok": False,
                           "token_budget_used_pct": 0.553}}
    ck("样本不足时档位字段仍为 D、由渲染层印「—」",
       _net_ns["grade"] == "D" and _net_ns["multi_sample_ok"] is False)

    print("\n== 九、计分时点（2026-11-26 用户拍板：计分放年报） ==")
    import team as _team
    _gp = str((config.PERF or {}).get("grade_period") or "")
    ck("config.PERF.grade_period == annual", _gp == "annual", _gp)
    ck("月报口径默认不出档位（_show_grade() is False）", review._show_grade() is False)
    ck("年报口径出档位（_show_grade(True) is True）", review._show_grade(True) is True)
    _ev = score.evaluate(0.035, 1.2, 50000, -0.02)
    _blk_m = _team._grade_block(_ev, annual=False)
    _blk_a = _team._grade_block(_ev, annual=True)
    ck("月报记分卡区块：不给档位、印「年度评分」",
       "年度评分" in _blk_m and "→ 评分" not in _blk_m, _blk_m.splitlines()[1] if "\n" in _blk_m else "")
    ck("年报记分卡区块：给档位 B+", "→ 评分" in _blk_a and "B+" in _blk_a)
    ck("月报记分卡区块：奖励行标「年度结算评定」", "年度结算" in _blk_m)
    _md_m = review.to_md([], "2026-11-26 22:00", annual=False)
    ck("月报记分卡头：写明计分时点只在年报", "计分时点" in _md_m and "只在年报" in _md_m)
    ck("to_md 兼容两参数调用（默认按 config）", isinstance(review.to_md([], "t"), str))

    print("\n十、动态本金：年报定档走「各月等价年化算术平均」")
    _ev_o = score.evaluate(0.010, 1.0, 50000, None, days=30, annual_rate_override=0.032)
    ck("override 优先于 days 年化（net_rate_ann=3.2%）",
       abs(float(_ev_o["net_rate_ann"]) - 0.032) < 1e-12, _ev_o["net_rate_ann"])
    ck("annual_metric = monthly_mean_equiv",
       _ev_o["annual_metric"] == "monthly_mean_equiv", _ev_o["annual_metric"])
    ck("档位按 override 定（3.2% → B+）", _ev_o["grade"] == "B+", _ev_o["grade"])
    _ev_d = score.evaluate(0.010, 1.0, 50000, None, days=30)
    ck("不给 override 时仍走 days 年化",
       _ev_d["annual_metric"] == "api_days" and _ev_d["net_rate_ann"] is not None,
       _ev_d["annual_metric"])
    _ev_n = score.evaluate(0.010, 1.0, 50000)
    ck("都不给时 annual_metric = none",
       _ev_n["annual_metric"] == "none" and _ev_n["net_rate_ann"] is None)
    _st = {"meta": {"capital": 50000.0}, "cash": 50000.0, "nav": [
        {"date": "2025-12-01", "nav": 50000.0}, {"date": "2025-12-30", "nav": 50100.0},
        {"date": "2026-01-02", "nav": 50100.0}, {"date": "2026-01-30", "nav": 50150.0}],
        "events": []}
    _st["meta"]["monthly_rates"] = {x["month"]: x for x in score.monthly_equivs(_st["nav"])}
    _me = capital_dyn.mean_equiv(_st)
    _exp = sum(float(x["equiv"]) for x in _st["meta"]["monthly_rates"].values()) / 2.0
    ck("mean_equiv = 各月等价年化的算术平均", abs(float(_me) - _exp) < 1e-12, _me)
    _src = open(os.path.join(os.path.dirname(os.path.abspath(review.__file__)), "team.py"),
                encoding="utf-8").read()
    ck("team.py 把 mean_equiv 传给 score.evaluate（年报定档链路）",
       "annual_rate_override" in _src and "mean_equiv" in _src)

    print("\n十一、记分卡渲染（真实 row → md；测试审计 A-3）")
    import copy as _copy
    _row = {
        "label": "50k", "name": "5 万账本", "capital": 50000.0, "nav": 50008.30,
        "total_return": 0.000166, "annualized": 0.0, "tier": "B",
        "window": {"base": "2026-10-28", "last": "2026-11-26", "weeks": 4, "days": 21},
        "verdict": "样本不足（还不能判断）",
        "verdict_reasons": ["观察窗 4 周 < 12 周", "平仓 0 笔 < 10 笔"],
        "net": {
            "gross_pnl": 8.30, "token_spent": 2.4401, "token_cost": 2.4401,
            "token_cost_basis": "out/budget.json 实测", "deposit_rate": 0.013,
            "deposit_as_of": "2026-10-27", "deposit_source": "1 年期定存",
            "net_pnl": 5.86, "net_return": 0.0001172, "net_annualized": -0.04,
            "years": 0.074, "bar_ok": False, "grade": "D", "grade_basis": "测试",
            "reward_pending": True, "reward_note": "评估期未到（2027-10-30 起）",
            "multi_sample_ok": False, "multi_grade": None,
            "quarter": {
                "available": True, "quarter": "2026Q4", "start": "2026-10-28",
                "end": "2026-11-26", "days": 30, "inception": True,
                "gross_return": 0.000166, "token_cost_cny": 2.4401,
                "token_cost_rate": 4.88e-05, "net_return": 0.0001172,
                "net_annualized_extrapolated": -0.04, "target_rate": 0.00325,
                "target_cny": 162.5, "excess_return": -0.0031328,
                "excess_cny": -156.64, "bar_ok": False,
                "token_quarter_budget_cny": 37.5, "token_budget_used_pct": 0.0651},
        },
        "max_drawdown": -0.0011, "bench_return": -0.0294, "excess": 0.0296,
        "closed_trades": 0, "trades_total": 1, "win_rate": None,
        "profit_factor": None, "expectancy": None, "fees_total": 35.11,
        "fees_pct": 0.0007, "exposure_avg": 0.28, "stop_outs": 0,
        "cash_yield_total": 3.2, "equity_pnl": 5.1,
        "realized_by_kind": {}, "float_by_kind": {}, "risk": {},
        "holdings": 1, "pending": 6, "state_file": "ledger/state_50k.json",
    }
    _md = review.to_md([_row], "2026-11-26 22:00")
    check("月报口径：档位行印「年度评分定档」",
          "—（年度评分：每年 1 月 1 日年报定档）" in _md)
    check("月报口径：奖励行印「月报不判档」",
          "奖励：—（年度评分后才计提；月报不判档）" in _md)
    check("真实 row 渲染出本季行（2026Q4）", "本季 **2026Q4**" in _md)
    check("真实 row 渲染出 token 季度预算行", "季度预算 37.5" in _md)
    check("净年化行标「外推值，不作达标判据」",
          "外推值，不作达标判据" in _md)
    check("表格里档位列印「—」", "| 50k | 50,000 |" in _md and "| — |" in _md)
    _md_ann = review.to_md([_row], "2026-11-26 22:00", annual=True)
    check("年报口径 + 样本不足 → 印「满 12 周才出档」",
          "—（样本不足，满 12 周才出档）" in _md_ann)
    check("年报口径：奖励行不再写「月报不判档」",
          "月报不判档" not in _md_ann and "评估期未到" in _md_ann)
    _row_ok = _copy.deepcopy(_row)
    _row_ok["net"]["multi_sample_ok"] = True
    _row_ok["net"]["multi_grade"] = "B+"
    _row_ok["net"]["grade"] = "B+"      # to_md 渲染的是 net.grade（judge 已按加权年化定档）
    _md_ok = review.to_md([_row_ok], "2026-11-26 22:00", annual=True)
    check("年报口径 + 满样本 → 印真档位 B+", "→ 档位 **B+**" in _md_ok)
    _md_err = review.to_md([{"label": "50k", "name": "50k", "error": "读不到账本"}],
                           "2026-11-26 22:00")
    check("错误账本渲染为 error 段（不整体抛异常）", "读不到账本" in _md_err)

    print("\n== 结果 ==")
    if fails:
        print(f"✗ {len(fails)} 项失败：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"✓ 全部通过（{n} 项断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
