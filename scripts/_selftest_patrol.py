# -*- coding: utf-8 -*-
"""巡检器 patrol.py 的离线自检（不联网、不读真实账本）。

为什么单独建文件：巡检的价值全在「会不会漏报」——
止损跌破没报、超限没报、过期挂单没报，都比程序报错更危险（报错至少会被看见）。
这里用合成账本把每条触发条件钉死。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import patrol  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def mk_state(cash=10000.0, holdings=None, orders=None, nav=None) -> dict:
    return {"meta": {"capital": 50000.0, "version": 1},
            "cash": cash,
            "holdings": {h["code"]: h for h in (holdings or [])},
            "trades": [], "orders": orders or [],
            "nav": nav or [], "events": []}


def hold(code, name, shares=100, cost=10.0, last=10.0, stop=None,
         industry="", opened_at="2026-06-01") -> dict:
    return {"code": code, "name": name, "kind": "stock", "layer": "个股", "level": "R3",
            "shares": shares, "cost_basis": cost, "opened_at": opened_at,
            "last_buy_date": opened_at, "last_close": last, "highest_close": last,
            "stop_price": stop, "industry": industry, "target_price": None,
            "thesis": None, "falsifiers": []}


def kinds(one: dict) -> list:
    return [a["kind"] for a in one.get("alerts") or []]


def main() -> int:
    global PASS, FAIL
    tmp = tempfile.mkdtemp(prefix="patrol_selftest_")
    old_out = config.OUT_DIR
    old_ledgers = config.PAPER.get("ledgers")
    try:
        config.OUT_DIR = tmp
        config.PAPER["ledgers"] = {
            "t1": {"capital": 50000.0, "state_file": os.path.join(tmp, "state_t1.json")},
            "t2": {"capital": 50000.0, "state_file": os.path.join(tmp, "missing.json")},
        }
        # ---------- 1) 止损触发 + 单票超限 + 挂单过期 + 时间止损 ----------
        st = mk_state(cash=1000.0, holdings=[
            hold("600111", "破位股", shares=1000, cost=10.0, last=8.0, stop=8.5),
        ], orders=[
            {"id": "o1", "code": "600222", "name": "过期挂单", "action": "buy",
             "shares": 100, "status": "pending", "expire_on": "2026-10-20"},
            {"id": "o2", "code": "600333", "name": "未过期", "action": "buy",
             "shares": 100, "status": "pending", "expire_on": "2026-12-01"},
        ])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        one = patrol.check_ledger("t1", "2026-10-29")
        check("止损跌破 → 报「止损触发」", "止损触发" in kinds(one), str(kinds(one)))
        check("止损触发是「高」优先级",
              any(a["level"] == "高" for a in one["alerts"] if a["kind"] == "止损触发"))
        check("8.0/9.0=88.9% → 报「单票超限」", "单票超限" in kinds(one), str(kinds(one)))
        check("过期挂单 → 报「挂单过期」", "挂单过期" in kinds(one), str(kinds(one)))
        check("未过期挂单不报",
              sum(1 for a in one["alerts"] if a["kind"] == "挂单过期") == 1)
        check("总资产 = 现金 + 市值", abs(one["total"] - (1000.0 + 8000.0)) < 1e-6, str(one["total"]))

        # ---------- 2) 时间止损（持有 > 12 个月）----------
        st2 = mk_state(cash=5000.0, holdings=[
            hold("600444", "长持股", shares=100, cost=10.0, last=10.5, stop=None,
                 opened_at="2025-01-01"),
        ])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st2, f, ensure_ascii=False)
        one2 = patrol.check_ledger("t1", "2026-10-29")
        check("持有 > 12 个月 → 报「时间止损」", "时间止损" in kinds(one2), str(kinds(one2)))
        check("未亏损且无止损价 → 不报止损触发", "止损触发" not in kinds(one2))

        # ---------- 3) 浮盈 > 10% → 提示止损可上移 ----------
        # 2026-11-21 修（S4 巡检 A 类）：不能再只看浮盈，必须按 收盘−2.5×ATR14 现算新止损，
        # 只有真能上移才报；ATR14 来自 out/stage1_candidates.csv（与阶段三同一列）。
        with io.open(os.path.join(tmp, "stage1_candidates.csv"), "w",
                     encoding="utf-8-sig", newline="") as f:
            f.write("code,atr14\n600555,0.5\n")
        st3 = mk_state(cash=5000.0, holdings=[
            hold("600555", "盈利股", shares=100, cost=10.0, last=11.5, stop=9.0),
        ])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st3, f, ensure_ascii=False)
        one3 = patrol.check_ledger("t1", "2026-10-29")
        check("浮盈 15% 且新止损可上移 → 报「止损可上移」", "止损可上移" in kinds(one3), str(kinds(one3)))
        check("浮盈提示是「中」优先级",
              all(a["level"] == "中" for a in one3["alerts"] if a["kind"] == "止损可上移"))
        check("提示里给出算出的新止损（11.5−2.5×0.5=10.250）",
              any("10.250" in a["detail"] for a in one3["alerts"] if a["kind"] == "止损可上移"),
              str([a["detail"] for a in one3["alerts"] if a["kind"] == "止损可上移"]))

        # ---------- 4) 行业超限（分母 = 权益层市值）----------
        st4 = mk_state(cash=0.0, holdings=[
            hold("600666", "银行甲", shares=1000, cost=10.0, last=10.0, industry="银行"),
            hold("600777", "银行乙", shares=1000, cost=10.0, last=10.0, industry="银行"),
            hold("600888", "半导体", shares=100, cost=10.0, last=10.0, industry="半导体"),
        ])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st4, f, ensure_ascii=False)
        one4 = patrol.check_ledger("t1", "2026-10-29")
        over = [a for a in one4["alerts"] if a["kind"] == "行业超限"]
        check("两行业合计 66% → 报「行业超限」", len(over) == 1, str(kinds(one4)))
        check("超限行业名对得上", over and over[0]["name"] == "银行甲、银行乙", str(over))

        # ---------- 4b) 账本无行业字段 → 用 industry_map 回填后再算集中度 ----------
        st4b = mk_state(cash=0.0, holdings=[
            hold("600666", "银行甲", shares=1000, cost=10.0, last=10.0),
            hold("600777", "银行乙", shares=1000, cost=10.0, last=10.0),
            hold("600888", "半导体", shares=100, cost=10.0, last=10.0),
        ])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st4b, f, ensure_ascii=False)
        _old_load = patrol.industry.load_map
        patrol.industry.load_map = lambda: {
            "600666": {"industry": "银行"},
            "600777": {"industry": "银行"},
            "600888": {"industry": "半导体"},
        }
        try:
            one4b = patrol.check_ledger("t1", "2026-10-29")
        finally:
            patrol.industry.load_map = _old_load
        check("无行业字段 → 回填后仍报「行业超限」",
              any(a["kind"] == "行业超限" for a in one4b["alerts"]), str(kinds(one4b)))
        check("回填后超限行业是「银行」而非未分类",
              any(a["kind"] == "行业超限" and "银行" in a["detail"] for a in one4b["alerts"]),
              str([a["detail"] for a in one4b["alerts"] if a["kind"] == "行业超限"]))
        check("回填结果写进行业字段",
              {p["code"]: p["industry"] for p in one4b["holdings"]}.get("600666") == "银行",
              str(one4b["holdings"]))

        # ---------- 5) 回撤熔断 ----------
        st5 = mk_state(cash=1000.0, holdings=[
            hold("600999", "回撤股", shares=100, cost=10.0, last=8.0),
        ], nav=[{"date": "2026-09-01", "value": 12000.0},
                {"date": "2026-10-29", "value": 9000.0}])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st5, f, ensure_ascii=False)
        one5 = patrol.check_ledger("t1", "2026-10-29")
        check("净值 -25% → 报「回撤熔断」", "回撤熔断" in kinds(one5), str(kinds(one5)))
        # 2026-11-17 修：nav 行改用 "nav" 键后峰值必须能从新键读出（旧代码只读 "value" → peak=0、永不熔断）
        st5b = mk_state(cash=1000.0, holdings=[
            hold("600998", "回撤股新格式", shares=100, cost=10.0, last=8.0),
        ], nav=[{"date": "2026-09-01", "nav": 12000.0},
                {"date": "2026-10-29", "nav": 9000.0}])
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st5b, f, ensure_ascii=False)
        one5b = patrol.check_ledger("t1", "2026-10-29")
        check("新格式 nav 键：净值 -25% → 报「回撤熔断」",
              "回撤熔断" in kinds(one5b), str(kinds(one5b)))

        # ---------- 6) 缺账本 / 空仓：不崩，只记 note ----------
        one6 = patrol.check_ledger("t2", "2026-10-29")
        check("账本不存在 → ok=False 且不抛异常", one6["ok"] is False, str(one6.get("notes")))
        check("账本不存在有说明", bool(one6.get("notes")))

        # ---------- 7) 数据新鲜度：缺失要报 ----------
        alerts7 = patrol.check_data("2026-10-29")
        check("产物缺失 → 报「数据缺失」", any(a["kind"] == "数据缺失" for a in alerts7),
              str([a["kind"] for a in alerts7]))
        io.open(os.path.join(tmp, "news_events.csv"), "w", encoding="utf-8").write("code\n")
        old = 100 * 86400
        os.utime(os.path.join(tmp, "news_events.csv"),
                 (__import__("time").time() - old, __import__("time").time() - old))
        alerts8 = patrol.check_data("2026-10-29")
        stale = [a for a in alerts8 if a["kind"] == "数据断更"]
        check("100 天前的产物 → 报「数据断更」", len(stale) == 1, str([a["detail"] for a in stale]))

        # ---------- 8) run() 整体：写盘 + 汇总 + 退出码语义 ----------
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(mk_state(cash=1000.0, holdings=[
                hold("600111", "破位股", shares=1000, cost=10.0, last=8.0, stop=8.5)]),
                f, ensure_ascii=False)
        _old_rates = patrol.check_rates
        patrol.check_rates = lambda today, refresher=None: {
            "ok": True, "live": True, "repo": 0.0125, "money_fund": 0.0108,
            "as_of": "2026-10-29", "drift_bp": 0.0, "alerts": [], "notes": []}
        # 2026-11-26 审计 A2：run(write=True) 会经 housekeeping 写生产 daily_log.md 并
        # sweep 生产 WORK；check_sentiment 还会联网抓热股榜。自检必须全部隔离/打桩。
        import hashlib

        import housekeeping as HK
        _prod_log = HK.LOG_PATH
        _prod_sha = (hashlib.sha256(io.open(_prod_log, "rb").read()).hexdigest()
                     if os.path.isfile(_prod_log) else None)
        _old_log, _old_sweep = HK.LOG_PATH, HK.sweep
        _old_sent = patrol.check_sentiment
        HK.LOG_PATH = os.path.join(tmp, "daily_log.md")
        HK.sweep = lambda *a, **k: {}
        patrol.check_sentiment = lambda day: {"ok": False, "alerts": [],
                                              "notes": ["selftest stub"]}
        try:
            res = patrol.run("2026-10-29")
        finally:
            HK.LOG_PATH, HK.sweep, patrol.check_sentiment = _old_log, _old_sweep, _old_sent
        _prod_sha2 = (hashlib.sha256(io.open(_prod_log, "rb").read()).hexdigest()
                      if os.path.isfile(_prod_log) else None)
        check("run() 不写生产 daily_log.md", _prod_sha == _prod_sha2, _prod_log)
        check("run() 汇总 n_alerts > 0", res["n_alerts"] > 0, str(res["n_alerts"]))
        check("run() 汇总 n_high ≥ 1", res["n_high"] >= 1, str(res["n_high"]))
        check("run() 写出 patrol_latest.json",
              os.path.isfile(patrol.json_path()))
        md = io.open(os.path.join(tmp, "patrol_2026-10-29.md"), encoding="utf-8").read()
        check("md 含要处理表头", "要处理" in md and "| 级别 | 类型 |" in md, md[:80])
        check("md 含账本快照", "账本快照" in md)
        check("JSON 里的 alerts 带 ledger 字段",
              all("ledger" in a for a in json.loads(
                  io.open(patrol.json_path(), encoding="utf-8").read())["alerts"]
                  if a["kind"] != "数据断更" and a["kind"] != "数据缺失" and a["kind"] != "预算异常"
                  and a.get("name") not in ("场外规模源", "取数通路", "公告流")
                  and not str(a.get("kind") or "").startswith("情绪")))

        # ---------- 8b) 基准日自动取最近交易日 + 简报结构 ----------
        check("last_trading_day(周六) 回退到上一个交易日",
              patrol.last_trading_day("2026-10-31") == "2026-10-30",
              patrol.last_trading_day("2026-10-31"))
        check("last_trading_day(交易日) 原样返回",
              patrol.last_trading_day("2026-10-30") == "2026-10-30")
        check("_today('auto') = 最近交易日（不跨日串日期）",
              patrol._today("auto") == patrol.last_trading_day(None))
        check("_today(显式日期) 原样返回", patrol._today("2026-10-29") == "2026-10-29")
        check("md 第一节是持仓体检", md.index("一、持仓体检") < md.index("二、要处理"), "order")
        check("md 维护项在最后", md.index("三、账本快照") < md.index("四、维护项"))
        check("md 持仓表含标的", "600111" in md, md[:200])

        # ---------- 9) 每日实测利率 ----------
        patrol.check_rates = _old_rates
        check("md 含现金层利率节", "现金层利率（每日实测）" in md)
        import rates as R
        _rp = {"rate_pct": 1.3564, "mean_60_pct": 1.2877, "mean_20_pct": 1.247,
               "sample_end": "2026-10-28"}
        got = R.compute(_rp, 1.0765)
        check("compute 逆回购取两窗口较小者再下取整（1.2877→1.25%）",
              abs(got["repo"] - 0.0125) < 1e-9, str(got["repo"]))
        check("compute 货基中位数四舍五入（1.0765→1.08%）",
              abs(got["money_fund"] - 0.0108) < 1e-9, str(got["money_fund"]))
        check("compute as_of 取数据实际日期", got["as_of"] == "2026-10-28", str(got["as_of"]))
        check("compute 缺货基时只给逆回购", R.compute(_rp, None)["money_fund"] is None)
        check("compute 全缺时为 None", R.compute(None, None)["repo"] is None)
        bad = patrol.check_rates("2026-10-29", refresher=lambda write=True: (_ for _ in ()).throw(RuntimeError("断网")))
        check("check_rates 取数异常不崩且报中优先级",
              bad["ok"] is False and bad["alerts"][0]["kind"] == "利率实测失败"
              and bad["alerts"][0]["level"] == "中", str(bad["alerts"][:1]))
        empty = patrol.check_rates("2026-10-29", refresher=lambda write=True: {"repo": None, "money_fund": None})
        check("check_rates 实测不全时报「利率实测不全」",
              any(a["kind"] == "利率实测不全" for a in empty["alerts"]),
              str([a["kind"] for a in empty["alerts"]]))
        okr = patrol.check_rates("2026-10-29", refresher=lambda write=True: {"repo": 0.0125, "money_fund": 0.0108})
        check("check_rates 正常时返回生效值与 live 标记",
              okr["ok"] is True and "live" in okr and okr["repo"] is not None, str(okr))
        # 9c) 逆回购平滑利率改每周五刷一次
        _old_load, _old_refresh = R.load, R.refresh
        R.load = lambda: {"repo_as_of": "2026-11-13"}
        try:
            check("逆回购：周五必刷", patrol._repo_refresh_due("2026-11-20") is True)
            check("逆回购：周四且上周五刷过 → 不刷", patrol._repo_refresh_due("2026-11-19") is False)
            check("逆回购：超过 7 个自然日 → 补刷", patrol._repo_refresh_due("2026-11-21") is True)
            _seen = {}

            def _stub_refresh(write=True, include_repo=True):
                _seen["include_repo"] = include_repo
                return {"repo": 0.0125, "money_fund": 0.0108}

            R.refresh = _stub_refresh
            patrol.check_rates("2026-11-19")
            check("巡检周四只刷货基（include_repo=False）", _seen.get("include_repo") is False,
                  str(_seen))
            patrol.check_rates("2026-11-20")
            check("巡检周五连逆回购一起刷（include_repo=True）", _seen.get("include_repo") is True,
                  str(_seen))
        finally:
            R.load, R.refresh = _old_load, _old_refresh
        # 9b) 货基首次实测为空 → 自动强制刷新重试一次（2026-11-14 A 类兜底）
        import pandas as _pd
        import datasource as _ds
        _of, _orr, _or7 = (_ds.fetch_money_fund_rates, _ds.fetch_repo_rate,
                           _ds.fetch_repo_rate_7d)
        _calls = {"n": 0}

        def _mf_stub():
            _calls["n"] += 1
            if _calls["n"] == 1:
                return _pd.DataFrame()          # 首次空表（模拟接口抖动）
            return _pd.DataFrame({"y7_pct": [1.10, 1.08, 1.06],
                                  "date": ["2026-11-10"] * 3,
                                  "code": ["000001", "000002", "000003"]})

        _ds.fetch_money_fund_rates = _mf_stub
        _ds.fetch_repo_rate = lambda: None
        _ds.fetch_repo_rate_7d = lambda: None
        _env0 = os.environ.get("A_SHARE_FORCE_REFRESH")
        try:
            _got2 = R._fetch()
        finally:
            (_ds.fetch_money_fund_rates, _ds.fetch_repo_rate,
             _ds.fetch_repo_rate_7d) = _of, _orr, _or7
        check("货基首次为空 → 自动重试一次并拿到实测",
              _calls["n"] == 2 and _got2.get("money_fund") is not None,
              f"calls={_calls['n']} mf={_got2.get('money_fund')}")
        check("货基重试后取中位数（1.08%）",
              abs(float(_got2.get("money_fund") or 0) - 0.0108) < 1e-9,
              str(_got2.get("money_fund")))
        check("重试后恢复 A_SHARE_FORCE_REFRESH 原值",
              os.environ.get("A_SHARE_FORCE_REFRESH") == _env0)
        # 9d) include_repo=False：沿用上次平滑值，不联网取逆回购
        _prev = {"raw": {"repo": {"rate_pct": 1.4, "mean_60_pct": 1.3, "sample_end": "2026-11-13"}}}
        _ds.fetch_money_fund_rates = lambda: _pd.DataFrame(
            {"y7_pct": [1.10], "date": ["2026-11-17"], "code": ["000001"]})
        _ds.fetch_repo_rate = lambda: (_ for _ in ()).throw(RuntimeError("不该被调用"))
        _ds.fetch_repo_rate_7d = lambda: (_ for _ in ()).throw(RuntimeError("不该被调用"))
        try:
            _got3 = R._fetch(include_repo=False, prev=_prev)
        finally:
            (_ds.fetch_money_fund_rates, _ds.fetch_repo_rate,
             _ds.fetch_repo_rate_7d) = _of, _orr, _or7
        check("include_repo=False 沿用上次平滑值（1.30%）且不联网",
              abs(float(_got3.get("repo") or 0) - 0.013) < 1e-9, str(_got3.get("repo")))

        # ---------- 11) 场外影子账进场外巡检 ----------
        st_otc = mk_state(cash=20000.0, holdings=[])
        st_otc["otc_holdings"] = {
            "OTC:012310": {"code": "OTC:012310", "name": "民生加银", "layer": "理财(R2)",
                           "level": "R2", "amount": 2400.0, "acc_nav": 1.1252,
                           "buy_date": "2026-10-29", "confirm_date": None,
                           "nav": 1.1385, "nav_date": "2026-10-20",
                           "redeem_fee_hold": 0.0}}
        st_otc["otc_orders"] = []
        st_otc["otc_trades"] = []
        with io.open(os.path.join(tmp, "state_t1.json"), "w", encoding="utf-8") as f:
            json.dump(st_otc, f, ensure_ascii=False)
        one3 = patrol.check_ledger("t1", "2026-10-29")
        exp_mv = round(2400.0 * 1.1385 / 1.1252, 2)
        check("场外市值计入总资产",
              abs(one3["total"] - (20000.0 + exp_mv)) < 0.05,
              f"{one3['total']} vs {20000.0 + exp_mv}")
        check("场外净值得出 otc 块", (one3.get("otc") or {}).get("market_value") is not None,
              str(one3.get("otc")))
        check("场外净值陈旧 → 报「场外净值陈旧」",
              "场外净值陈旧" in kinds(one3), str(kinds(one3)))
        check("持有不足 90 天 → 报「场外赎回费窗口」",
              "场外赎回费窗口" in kinds(one3), str(kinds(one3)))
        check("场外巡检不误报止损", "止损触发" not in kinds(one3), str(kinds(one3)))

        # ---------- 10) 日期工具 ----------
        check("_days_since 正常", patrol._days_since("2026-10-20", "2026-10-29") == 9)
        check("_days_since 脏值返回 None", patrol._days_since("", "2026-10-29") is None)
        check("_days_since 非法日期返回 None", patrol._days_since("abc", "2026-10-29") is None)
        check("json_path() 指向 out 目录", patrol.json_path().endswith("patrol_latest.json"))
    finally:
        config.OUT_DIR = old_out
        if old_ledgers is not None:
            config.PAPER["ledgers"] = old_ledgers
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 12) 排班口径（2026-11-25 改定：周三/周六双决策、巡检取最近交易日）----------
    print("12) 排班口径：周三/周六双决策、巡检按最近交易日")
    import datetime as _dt
    import report_html as _rh
    check("2026-11-25（周三）→ 下次决策 11-28 周六", _rh.next_decision_day("2026-11-25") == ("2026-11-28", "周六"))
    check("2026-11-28（周六）→ 下次决策 12-02 周三", _rh.next_decision_day("2026-11-28") == ("2026-12-02", "周三"))
    check("2026-11-26（周四）→ 下次决策 11-28 周六", _rh.next_decision_day("2026-11-26") == ("2026-11-28", "周六"))
    _w = {a[0]: a[2] for a in patrol.WATCH}
    check("决策日产物新鲜度阈值 = 8 天（周更，不会周二误报）",
          all(_w.get(k) == 8 for k in ("stage1_candidates.csv", "stage3_plan.json",
                                       "news_events.csv", "sentiment.json", "data_snapshot.csv")), str(_w))
    check("last_trading_day(周六) 回退到周五", patrol.last_trading_day("2026-10-31") == "2026-10-30")
    _auto = patrol._today("auto")
    check("_today('auto') 落在工作日", _dt.date.fromisoformat(_auto).weekday() < 5, _auto)
    check("_today(显式日期) 原样返回", patrol._today("2026-10-29") == "2026-10-29")

    # ---------- 13) 日度盯市：多账本各记各的 + 同日幂等 ----------
    print("13) 日度盯市：多账本各记各的、同日不重复")
    tmp2 = tempfile.mkdtemp(prefix="patrol_mark_")
    _old_out, _old_lg = config.OUT_DIR, config.PAPER.get("ledgers")
    _old_bench = patrol._bench_close
    try:
        config.OUT_DIR = tmp2
        config.PAPER["ledgers"] = {
            "t1": {"capital": 50000.0, "state_file": os.path.join(tmp2, "state_t1.json")},
            "t2": {"capital": 60000.0, "state_file": os.path.join(tmp2, "state_t2.json")},
        }
        for _lg, _cap in (("t1", 50000.0), ("t2", 60000.0)):
            _st = mk_state(cash=_cap, nav=[])
            _st["meta"]["capital"] = _cap
            with io.open(os.path.join(tmp2, f"state_{_lg}.json"), "w", encoding="utf-8") as f:
                json.dump(_st, f, ensure_ascii=False)
        patrol._bench_close = lambda: 4000.0      # 不联网
        m1 = patrol.mark_daily("t1", "2026-10-30")
        m2 = patrol.mark_daily("t2", "2026-10-30")
        m1b = patrol.mark_daily("t1", "2026-10-30")
        s1 = json.load(io.open(os.path.join(tmp2, "state_t1.json"), encoding="utf-8"))
        s2 = json.load(io.open(os.path.join(tmp2, "state_t2.json"), encoding="utf-8"))
        check("t1 盯市 nav = 本金 50000", bool(m1) and m1["nav"] == 50000.0, str(m1))
        check("t2 盯市 nav = 本金 60000", bool(m2) and m2["nav"] == 60000.0, str(m2))
        check("t1 文件本金没被 t2 覆盖", s1["meta"]["capital"] == 50000.0, str(s1["meta"].get("capital")))
        check("t2 文件本金仍是 60000", s2["meta"]["capital"] == 60000.0, str(s2["meta"].get("capital")))
        check("同日重复盯市仍只有 1 条 nav", len(s1["nav"]) == 1, str(s1["nav"]))
        check("重复盯市不改净值", bool(m1b) and m1b["nav"] == m1["nav"])
        _res = {"date": "2026-10-30", "generated_at": "2026-10-30 22:00:00",
                "n_alerts": 0, "n_high": 0, "alerts": [],
                "ledgers": [{"ledger": "t1", "ok": True, "cash": 50000.0, "market_value": 0.0,
                             "total": 50000.0, "holdings": [], "notes": [],
                             "mark": {"nav": 50000.0, "day_chg": 0.0, "total_ret": 0.0,
                                      "drawdown": 0.0}}],
                "data": [], "rates": {}, "budget": []}
        _md = patrol.render_md(_res)
        check("巡检 md 快照含「净值」列", "| 净值 |" in _md)
        _brief = patrol.render_md(_res, brief=True)
        check("日常简报不含「三、账本快照」（只两节）", "三、账本快照" not in _brief)
        check("日常简报保留「二、要处理」", "二、要处理" in _brief)
        check("完整版仍有「三、账本快照」", "三、账本快照" in _md)
        check("巡检 md 快照净值 = 1.0000", "1.0000" in _md)
    finally:
        patrol._bench_close = _old_bench
        config.OUT_DIR = _old_out
        if _old_lg is not None:
            config.PAPER["ledgers"] = _old_lg
        shutil.rmtree(tmp2, ignore_errors=True)

    # ---------- 14) mark_daily 先计息再 settle：现金利息不再永久为 0 ----------
    print("14) 日度盯市先计息：现金计息窗口不再恒为 0 天")
    tmp3 = tempfile.mkdtemp(prefix="patrol_accrue_")
    _old_out, _old_lg = config.OUT_DIR, config.PAPER.get("ledgers")
    _old_bench, _old_cal = patrol._bench_close, patrol.trade_calendar
    try:
        config.OUT_DIR = tmp3
        config.PAPER["ledgers"] = {"t3": {"capital": 50000.0,
                                          "state_file": os.path.join(tmp3, "state_t3.json")}}
        _st = mk_state(cash=50000.0, nav=[{"date": "2026-10-23", "nav": 50000.0}])
        _st["cash_marks"] = {"last_accrued": "2026-10-23"}
        # 现金层政策：观望资金按金额分档买 7/14/28 天期。
        # 这里让一笔 7 天期在途 lot 在 10-27 到期，验证窗口（10-23→10-30）内兑现、
        # 利息落账与 last_accrued 推进；否则 49,000 元会被排成 28 天期、窗口内无兑现。
        _st["repo_open"] = {"buy": "2026-10-20", "mature": "2026-10-27", "days": 7,
                            "term": 7, "amount": 9000.0, "rate": 0.0135,
                            "fee_rate": 0.00005, "interest": 2.33, "fee": 0.45}
        with io.open(os.path.join(tmp3, "state_t3.json"), "w", encoding="utf-8") as f:
            json.dump(_st, f, ensure_ascii=False)
        _cal = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28",
                "2026-10-29", "2026-10-30"]
        patrol._bench_close = lambda: 4000.0
        patrol.trade_calendar = lambda: _cal
        m = patrol.mark_daily("t3", "2026-10-30")
        s3 = json.load(io.open(os.path.join(tmp3, "state_t3.json"), encoding="utf-8"))
        check("巡检计息：现金增加（利息不再是 0）", s3["cash"] > 50000.0, str(s3["cash"]))
        check("巡检计息：last_accrued 推进到 10-30",
              s3.get("cash_marks", {}).get("last_accrued") == "2026-10-30",
              str(s3.get("cash_marks")))
        check("巡检计息：窗口 7 天（10-23 → 10-30）",
              any(e.get("level") == "cash_yield" and "7 天" in str(e.get("msg") or "")
                  for e in s3["events"]), str([e.get("level") for e in s3["events"]]))
        check("巡检计息：nav 含已入账利息",
              bool(m) and abs(m["nav"] - s3["cash"]) < 1e-6,
              f"nav={m and m['nav']} cash={s3['cash']}")
        _cash1 = s3["cash"]
        patrol.mark_daily("t3", "2026-10-30")
        s3b = json.load(io.open(os.path.join(tmp3, "state_t3.json"), encoding="utf-8"))
        check("同日重复计息不重复入账", abs(s3b["cash"] - _cash1) < 1e-9, str(s3b["cash"]))
        patrol.trade_calendar = lambda: None      # 日历取不到 → 等价无操作
        m3 = patrol.mark_daily("t3", "2026-10-31")
        s3c = json.load(io.open(os.path.join(tmp3, "state_t3.json"), encoding="utf-8"))
        check("日历取不到：不报错且不计息", bool(m3) and abs(s3c["cash"] - _cash1) < 1e-9,
              str(s3c["cash"]))
    finally:
        patrol._bench_close = _old_bench
        patrol.trade_calendar = _old_cal
        config.OUT_DIR = _old_out
        if _old_lg is not None:
            config.PAPER["ledgers"] = _old_lg
        shutil.rmtree(tmp3, ignore_errors=True)

    # ---------- 15) 先盯市再巡检：报表/告警用当日收盘价，不用上一次结算日的旧价 ----------
    print("15) 巡检顺序：先盯市再体检（当日收盘价不滞后一天）")
    tmp4 = tempfile.mkdtemp(prefix="patrol_order_")
    _old_out, _old_lg = config.OUT_DIR, config.PAPER.get("ledgers")
    _old_fns = (patrol._close_prices, patrol._bench_close, patrol.trade_calendar,
                patrol.check_data, patrol.check_rates, patrol.check_budget)
    import housekeeping as _HK
    _old_hk = (_HK.append, _HK.sweep)
    try:
        config.OUT_DIR = tmp4
        config.PAPER["ledgers"] = {"t9": {"capital": 50000.0,
                                          "state_file": os.path.join(tmp4, "state_t9.json")}}
        _st = mk_state(cash=10000.0, holdings=[
            hold("600111", "旧价股", shares=1000, cost=10.0, last=8.0, stop=8.5)])
        with io.open(os.path.join(tmp4, "state_t9.json"), "w", encoding="utf-8") as f:
            json.dump(_st, f, ensure_ascii=False)
        patrol._close_prices = lambda codes, etf_codes=None: {c: 9.0 for c in codes}  # 当日收盘 9.0（账本旧价 8.0）
        patrol._bench_close = lambda: 4000.0
        patrol.trade_calendar = lambda: None                          # 不计息，隔离现金干扰
        patrol.check_data = lambda day: []
        patrol.check_rates = lambda day: {"repo": None, "money_fund": None, "as_of": None,
                                          "live": False, "drift_bp": None,
                                          "notes": [], "alerts": []}
        patrol.check_budget = lambda lg: {"ledger": lg, "ok": True, "base": 150.0,
                                          "spent": 1.0, "remaining": 149.0,
                                          "remaining_pct": 0.9933}
        _HK.append = lambda row, day=None: "appended"
        _HK.sweep = lambda today=None: {}
        _res = patrol.run("2026-10-30", write=True, full_md=True)
        _one = _res["ledgers"][0]
        check("巡检结果带当日盯市 mark", bool(_one.get("mark")), str(_one.get("mark")))
        check("持仓现价 = 当日收盘 9.0（不再是账本旧价 8.0）",
              _one["holdings"][0]["last"] == 9.0, str(_one["holdings"][0].get("last")))
        check("总资产 = 当日 nav（现金+当日市值，不混旧价）",
              abs(float(_one["total"]) - float(_one["mark"]["nav"])) < 1e-6,
              f"total={_one['total']} nav={_one['mark']['nav']}")
        check("止损不再按旧价误报（9.0 > 止损 8.5）",
              "止损触发" not in [a.get("kind") for a in (_one.get("alerts") or [])],
              str([a.get("kind") for a in (_one.get("alerts") or [])]))
        _md = io.open(os.path.join(tmp4, "patrol_2026-10-30.md"), encoding="utf-8").read()
        check("完整 md 报表现价为 9.000", "| 9.000 |" in _md)
        check("完整 md 总资产 = 19,000.00（与 nav 一致）", "19,000.00" in _md)
    finally:
        (patrol._close_prices, patrol._bench_close, patrol.trade_calendar,
         patrol.check_data, patrol.check_rates, patrol.check_budget) = _old_fns
        _HK.append, _HK.sweep = _old_hk
        config.OUT_DIR = _old_out
        if _old_lg is not None:
            config.PAPER["ledgers"] = _old_lg
        shutil.rmtree(tmp4, ignore_errors=True)

    # ---------- 16) ETF 走基金日线接口：别用股票代码拼法把 k 线取丢 ----------
    print("16) ETF 盯市走 fetch_etf_hist，不被股票代码拼法吞掉")
    tmp5 = tempfile.mkdtemp(prefix="patrol_etf_")
    _old_out, _old_lg = config.OUT_DIR, config.PAPER.get("ledgers")
    _old_fns = (patrol.datasource.fetch_daily, patrol.datasource.fetch_etf_hist,
                patrol._bench_close, patrol.trade_calendar)
    try:
        config.OUT_DIR = tmp5
        config.PAPER["ledgers"] = {"t8": {"capital": 50000.0,
                                          "state_file": os.path.join(tmp5, "state_t8.json")}}
        _etf_hold = hold("sh510050", "上证50ETF", shares=200, cost=2.9, last=2.8)
        _etf_hold["kind"], _etf_hold["layer"] = "etf", "ETF"
        _st = mk_state(cash=10000.0, holdings=[
            _etf_hold, hold("600111", "股票甲", shares=100, cost=10.0, last=10.0)])
        with io.open(os.path.join(tmp5, "state_t8.json"), "w", encoding="utf-8") as f:
            json.dump(_st, f, ensure_ascii=False)
        _calls = []

        class _FakeCol:
            def __init__(self, px):
                self.iloc = [px]

        class _FakeDF:
            def __init__(self, px):
                self._px = px

            def __len__(self):
                return 1

            def __getitem__(self, key):
                return _FakeCol(self._px)

        patrol.datasource.fetch_daily = \
            lambda code, years=None: (_calls.append(("daily", code)), _FakeDF(25.0))[1]
        patrol.datasource.fetch_etf_hist = \
            lambda code: (_calls.append(("etf", code)), _FakeDF(3.0))[1]
        _etfs = patrol._etf_codes(_st)
        check("_etf_codes 认账本 kind=etf", _etfs == {"sh510050"}, str(_etfs))
        _closes = patrol._close_prices(["sh510050", "600111"], _etfs)
        check("ETF 取到基金日线收盘 3.0", _closes.get("sh510050") == 3.0, str(_closes))
        check("股票仍走股票日线 25.0", _closes.get("600111") == 25.0, str(_closes))
        check("ETF 走 fetch_etf_hist、股票走 fetch_daily",
              ("etf", "sh510050") in _calls and ("daily", "sh510050") not in _calls, str(_calls))
        patrol._bench_close = lambda: 4000.0
        patrol.trade_calendar = lambda: None          # 不计息，隔离现金干扰
        _m = patrol.mark_daily("t8", "2026-11-13")
        _s = json.load(io.open(os.path.join(tmp5, "state_t8.json"), encoding="utf-8"))
        check("盯市把 ETF 收盘价写回账本（2.8 → 3.0）",
              abs(float(_s["holdings"]["sh510050"]["last_close"]) - 3.0) < 1e-9,
              str(_s["holdings"]["sh510050"].get("last_close")))
        check("盯市 nav 含 ETF 市价（10000 + 200×3.0 + 100×25.0 = 13100）",
              bool(_m) and abs(_m["nav"] - 13100.0) < 1e-6, str(_m))
    finally:
        (patrol.datasource.fetch_daily, patrol.datasource.fetch_etf_hist,
         patrol._bench_close, patrol.trade_calendar) = _old_fns
        config.OUT_DIR = _old_out
        if _old_lg is not None:
            config.PAPER["ledgers"] = _old_lg
        shutil.rmtree(tmp5, ignore_errors=True)

    # ---------- 9) 三行简报 --brief（2026-11-17 加入，20:00 巡检任务直接引用） ----------
    res9 = {"date": "2026-11-17",
            "ledgers": [{"ledger": "50k", "ok": True,
                         "holdings": [{"code": "600111"}, {"code": "600222"}, {"code": "600333"}]}],
            "alerts": [
                {"kind": "止损触发", "level": "高", "code": "600111", "name": "破位股",
                 "detail": "收盘 8.0 ≤ 止损价 8.5", "action": "次日开盘卖出（硬止损）"},
                {"kind": "场外赎回费窗口", "level": "低", "code": "OTC:016800", "name": "债基A",
                 "detail": "持有 11 天", "action": "拿满 90 天"},
                {"kind": "场外赎回费窗口", "level": "低", "code": "OTC:011984", "name": "债基B",
                 "detail": "持有 11 天", "action": "拿满 90 天"},
            ],
            "rates": {"live": True, "repo": 0.0125, "money_fund": 0.0107, "as_of": "2026-11-17"}}
    st9 = {"total": 50148.2, "pnl": 90.95, "cum": 0.2964, "dd": -0.3451, "pos": 18.8}
    lines9 = patrol.brief_lines(res9, stats=st9)
    check("简报三行", len(lines9) == 3, str(len(lines9)))
    check("状态行含 nav/当日/仓位",
          "nav 50,148.20" in lines9[0] and "当日 +90.95 元" in lines9[0]
          and "仓位 19%" in lines9[0] and "持仓 3 只" in lines9[0], lines9[0])
    check("第二行列需动作告警", lines9[1].startswith("需动作：") and "止损触发" in lines9[1], lines9[1])
    check("第三行汇总低危并标无需动作",
          "低危 2 条" in lines9[2] and lines9[2].endswith("无需动作"), lines9[2])
    res9b = {"date": "2026-11-17",
             "ledgers": [{"ledger": "50k", "ok": True, "holdings": []}],
             "alerts": [], "rates": {"live": True, "as_of": "2026-11-17"}}
    lines9b = patrol.brief_lines(res9b, stats={"total": 50000.0})
    check("无告警无低危 → 两行且「今晚无需动作」",
          len(lines9b) == 2 and lines9b[1] == "今晚无需动作", str(lines9b))
    lines9c = patrol.brief_lines(dict(res9b, rates={"live": False, "as_of": "2026-11-10"}),
                                 stats={"total": 50000.0})
    check("利率未实测 → 第三行提示兜底", len(lines9c) == 3 and "兜底" in lines9c[-1], str(lines9c))
    lines9d = patrol.brief_lines({"date": "2026-11-17",
                                  "ledgers": [{"ledger": "50k", "ok": False,
                                               "notes": ["账本文件存在但解析失败"]}],
                                  "alerts": [], "rates": {"live": True}})
    check("账本坏了 → 降级行且不抛异常",
          lines9d[0].startswith("巡检降级") and "读不出" not in lines9d[0], lines9d[0])

    # ---------- 10) ETF 折溢价体检（收盘价 ÷ 同日单位净值 − 1） ----------
    _old_prem = patrol.datasource.fetch_etf_premium
    _old_load10 = patrol._load_state
    patrol._load_state = lambda lg: {"holdings": {"sh510050": {"code": "sh510050",
                                                               "kind": "etf", "shares": 200}}}
    patrol.datasource.fetch_etf_premium = lambda c: {
        "code": c, "date": "2026-11-19", "price": 3.06, "nav": 3.0,
        "nav_date": "2026-11-19", "premium_pct": 2.0}
    a10 = patrol.check_premium("50k", "2026-11-19")
    check("溢价 2% → 报「ETF 溢价（买贵）」中危",
          len(a10) == 1 and "溢价" in a10[0]["kind"] and a10[0]["level"] == "中", str(a10))
    patrol.datasource.fetch_etf_premium = lambda c: {
        "code": c, "date": "2026-11-19", "price": 3.0, "nav": 3.0,
        "nav_date": "2026-11-18", "premium_pct": 0.0}
    a10b = patrol.check_premium("50k", "2026-11-19")
    check("净值落后只报低优先级、平价不报买贵",
          len(a10b) == 1 and a10b[0]["kind"] == "净值日期落后" and a10b[0]["level"] == "低",
          str(a10b))

    def _prem_boom(c):
        raise RuntimeError("净值通路挂了")

    patrol.datasource.fetch_etf_premium = _prem_boom
    a10c = patrol.check_premium("50k", "2026-11-19")
    check("折溢价取数失败 fail-soft（低优先级一条）",
          len(a10c) == 1 and a10c[0]["kind"] == "折溢价未取到"
          and a10c[0]["level"] == "低", str(a10c))
    patrol.datasource.fetch_etf_premium = _old_prem
    patrol._load_state = _old_load10

    # ---------- 11) 场外规模源可用率（读 stock prefetch manifest） ----------
    _old_snap = getattr(config, "PREFETCH_SNAP_DIR", "")

    def _mk_manifest(root: str, day: str, probe: dict | None) -> None:
        d = os.path.join(root, day)
        os.makedirs(d, exist_ok=True)
        man = {"funnel": {} if probe is None else {"L7_scale_probe": probe}}
        with io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(man))

    root11 = tempfile.mkdtemp(prefix="patrol_scale_")
    try:
        # 1) 目录里没有任何快照 → 低危一条、不抛
        config.PREFETCH_SNAP_DIR = os.path.join(root11, "empty")
        os.makedirs(config.PREFETCH_SNAP_DIR, exist_ok=True)
        s11a = patrol.check_scale_source("2026-11-19")
        check("manifest 缺失 → 低危「取数通路未体检」且不抛",
              s11a["ok"] is False and len(s11a["alerts"]) == 1
              and s11a["alerts"][0]["level"] == "低", str(s11a.get("alerts")))

        # 2) 可用率 8.3% → 中危「场外规模源掉线」，文案点明规模过滤失效
        _mk_manifest(root11, "2026-11-19",
                     {"n": 120, "available": 10, "ratio": 0.083})
        config.PREFETCH_SNAP_DIR = root11
        s11b = patrol.check_scale_source("2026-11-19")
        check("可用率 8.3% → 中危且点明「规模过滤」",
              s11b["ok"] is True and len(s11b["alerts"]) == 1
              and s11b["alerts"][0]["level"] == "中"
              and "规模过滤" in s11b["alerts"][0]["detail"],
              str(s11b.get("alerts")))

        # 3) 可用率 90% → 无告警
        _mk_manifest(root11, "2026-11-19",
                     {"n": 120, "available": 108, "ratio": 0.9})
        s11c = patrol.check_scale_source("2026-11-19")
        check("可用率 90% → 无告警", s11c["ok"] is True and not s11c["alerts"],
              str(s11c.get("alerts")))

        # 4) manifest 过期（距今 > 2 天）→ 低危「取数通路陈旧」（单独目录，避免读到上面的新快照）
        root11b = os.path.join(root11, "old")
        _mk_manifest(root11b, "2026-11-10",
                     {"n": 120, "available": 108, "ratio": 0.9})
        config.PREFETCH_SNAP_DIR = root11b
        s11d = patrol.check_scale_source("2026-11-19")
        check("manifest 过期 → 低危「取数通路陈旧」",
              len(s11d["alerts"]) == 1 and "陈旧" in s11d["alerts"][0]["kind"],
              str(s11d.get("alerts")))
    finally:
        config.PREFETCH_SNAP_DIR = _old_snap
        shutil.rmtree(root11, ignore_errors=True)


    # ---------- 12) 取数通路体检（manifest 主链路 + 行情/公告缓存） ----------
    _old_snap12 = getattr(config, "PREFETCH_SNAP_DIR", "")
    _old_cache12 = config.CACHE_DIR
    _old_ltd12 = patrol.last_trading_day
    root12 = tempfile.mkdtemp(prefix="patrol_sources_")
    cache12 = os.path.join(root12, "cache")
    os.makedirs(os.path.join(cache12, "cninfo_market"), exist_ok=True)

    def _mk_man(root: str, day: str, **fields) -> None:
        d = os.path.join(root, day)
        os.makedirs(d, exist_ok=True)
        man = {"ok": True, "rc": 0, "bar_ok": True, "etf_bar_ok": True,
               "seconds": 300.0, "coverage": {"ok": True, "counts": {"daily": 800}}}
        man.update(fields)
        with io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(man))

    def _mk_ann(rows: int) -> None:
        p = os.path.join(cache12, "cninfo_market", "2026-11-19.csv")
        with io.open(p, "w", encoding="utf-8") as f:
            f.write("代码,名称,公告标题\n")
            for i in range(rows):
                f.write(f"{i:06d},某公司,公告{i}\n")

    try:
        config.PREFETCH_SNAP_DIR = root12
        config.CACHE_DIR = cache12
        patrol.last_trading_day = lambda d=None: "2026-11-19"
        _mk_man(root12, "2026-11-19")
        _mk_ann(500)
        s12a = patrol.check_sources("2026-11-19")
        check("全绿 → 无告警", s12a["ok"] is True and not s12a["alerts"], str(s12a.get("alerts")))

        _mk_man(root12, "2026-11-19", ok=False, rc=1)
        s12b = patrol.check_sources("2026-11-19")
        check("prefetch ok=False → 中危「取数失败」",
              len(s12b["alerts"]) == 1 and s12b["alerts"][0]["kind"] == "取数失败"
              and s12b["alerts"][0]["level"] == "中", str(s12b.get("alerts")))

        _mk_man(root12, "2026-11-19", bar_ok=False)
        s12c = patrol.check_sources("2026-11-19")
        check("bar_ok=False → 中危「个股日线未就绪」",
              len(s12c["alerts"]) == 1 and s12c["alerts"][0]["kind"] == "个股日线未就绪",
              str(s12c.get("alerts")))

        _mk_man(root12, "2026-11-19", coverage={"ok": False, "counts": {"daily": 12}})
        s12d = patrol.check_sources("2026-11-19")
        check("覆盖不足 → 中危「缓存覆盖不足」且带计数",
              len(s12d["alerts"]) == 1 and s12d["alerts"][0]["kind"] == "缓存覆盖不足"
              and "daily=12" in s12d["alerts"][0]["detail"], str(s12d.get("alerts")))

        _mk_man(root12, "2026-11-19", seconds=2400.0)
        s12e = patrol.check_sources("2026-11-19")
        check("取数 2400 秒 → 低危「取数变慢」",
              len(s12e["alerts"]) == 1 and s12e["alerts"][0]["kind"] == "取数变慢"
              and s12e["alerts"][0]["level"] == "低", str(s12e.get("alerts")))

        _mk_man(root12, "2026-11-19")
        _mk_ann(5)
        s12f = patrol.check_sources("2026-11-19")
        check("公告只剩 5 条 → 中危「公告流异常」",
              len(s12f["alerts"]) == 1 and s12f["alerts"][0]["kind"] == "公告流异常"
              and "5 条" in s12f["alerts"][0]["detail"], str(s12f.get("alerts")))

        os.remove(os.path.join(cache12, "cninfo_market", "2026-11-19.csv"))
        s12g = patrol.check_sources("2026-11-19")
        check("公告未落盘 → 低危「公告流未落盘」",
              len(s12g["alerts"]) == 1 and s12g["alerts"][0]["kind"] == "公告流未落盘",
              str(s12g.get("alerts")))

        root12b = os.path.join(root12, "stale")
        _mk_man(root12b, "2026-11-18")
        _mk_ann(500)
        config.PREFETCH_SNAP_DIR = root12b
        s12h = patrol.check_sources("2026-11-19")
        check("今天还没取数 → 中危「取数未跑」",
              len(s12h["alerts"]) == 1 and s12h["alerts"][0]["kind"] == "取数未跑",
              str(s12h.get("alerts")))
    finally:
        config.PREFETCH_SNAP_DIR = _old_snap12
        config.CACHE_DIR = _old_cache12
        patrol.last_trading_day = _old_ltd12
        shutil.rmtree(root12, ignore_errors=True)
    # ---------- 13) 场外持仓：日度盯市顺手刷新累计净值 ----------
    _old_nav13 = patrol.datasource.fetch_fund_nav_history
    try:
        st13 = {"meta": {"capital": 50000.0}, "cash": 20000.0, "holdings": {},
                "otc_holdings": {"OTC:016800": {"code": "016800", "name": "建信鑫和",
                                                "amount": 1000.0, "acc_nav": 1.0,
                                                "nav": 1.0, "nav_date": "2026-11-13"}},
                "otc_orders": [], "otc_trades": [], "orders": [], "nav": []}
        seen13 = {}

        def _fake_nav13(code, indicator=""):
            seen13["code"] = code
            seen13["indicator"] = indicator
            return {"date": ["2026-11-19", "2026-11-20"], "value": [1.05, 1.06]}

        patrol.datasource.fetch_fund_nav_history = _fake_nav13
        n13 = patrol.otc_book.refresh_navs(st13, "2026-11-20")
        check("refresh_navs 用 6 位代码 + 累计净值取数",
              seen13.get("code") == "016800" and seen13.get("indicator") == "累计净值走势",
              str(seen13))
        check("refresh_navs 刷新只数 = 1", n13 == 1, str(n13))
        h13 = st13["otc_holdings"]["OTC:016800"]
        check("refresh_navs 后 nav_date 跟上最新披露日",
              h13.get("nav_date") == "2026-11-20" and abs(float(h13.get("nav") or 0) - 1.06) < 1e-9,
              str(h13))

        def _boom13(code, indicator=""):
            raise RuntimeError("网络抽风")

        patrol.datasource.fetch_fund_nav_history = _boom13
        check("refresh_navs 取数失败不抛（沿用旧净值）",
              patrol.otc_book.refresh_navs(st13, "2026-11-20") == 0, "应返回 0")
        check("无场外持仓时不取数",
              patrol.otc_book.refresh_navs({"meta": {}, "otc_holdings": {}}, "2026-11-20") == 0, "")
    finally:
        patrol.datasource.fetch_fund_nav_history = _old_nav13
    # ---------- 14) 情绪层体检：key / 接口 / 产物 ----------
    import fuyao as _fy14
    _old_out14 = config.OUT_DIR
    _old_avail14, _old_hot14 = _fy14.available, _fy14.hot_stock_list
    root14 = tempfile.mkdtemp(prefix="patrol14_")
    try:
        config.OUT_DIR = root14
        _fy14.available = lambda: False
        s14a = patrol.check_sentiment("2026-11-20")
        check("情绪源未配置 → 中危",
              len(s14a["alerts"]) == 1 and s14a["alerts"][0]["kind"] == "情绪源未配置"
              and s14a["alerts"][0]["level"] == "中", str(s14a.get("alerts")))

        _fy14.available = lambda: True

        def _boom14(ttl_hours=1.0):
            raise RuntimeError("接口重置")

        _fy14.hot_stock_list = _boom14
        s14b = patrol.check_sentiment("2026-11-20")
        check("key 在但接口挂 → 中危「情绪源接口异常」",
              bool(s14b["alerts"]) and s14b["alerts"][0]["kind"] == "情绪源接口异常",
              str(s14b.get("alerts")))

        _fy14.hot_stock_list = lambda ttl_hours=1.0: [{"code": "600000"}]
        s14c = patrol.check_sentiment("2026-11-20")
        check("接口正常但没产物 → 低危「情绪层未生成」",
              bool(s14c["alerts"]) and s14c["alerts"][0]["kind"] == "情绪层未生成",
              str(s14c.get("alerts")))

        _p14 = os.path.join(root14, "sentiment.json")
        with io.open(_p14, "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-11-20 10:00:00",
                       "temp": {"limit_up": 0, "limit_down": 0, "break": 0}, "flags": []}, f)
        s14d = patrol.check_sentiment("2026-11-20")
        check("产物样本全 0 → 低危「情绪层空样本」",
              bool(s14d["alerts"]) and s14d["alerts"][0]["kind"] == "情绪层空样本",
              str(s14d.get("alerts")))

        with io.open(_p14, "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-11-20 10:00:00",
                       "temp": {"limit_up": 26, "limit_down": 72, "break": 24}, "flags": []}, f)
        s14e = patrol.check_sentiment("2026-11-20")
        check("正常产物 → 无告警", s14e["ok"] and not s14e["alerts"], str(s14e.get("alerts")))
    finally:
        config.OUT_DIR = _old_out14
        _fy14.available, _fy14.hot_stock_list = _old_avail14, _old_hot14
        shutil.rmtree(root14, ignore_errors=True)
    # ---------- 15) 持仓风险警示（当前简称 ST/退市整理） ----------
    import pandas as _pd15
    _old_n15 = patrol._load_state

    def _mk_state15(holds):
        return {"holdings": holds}

    def _ck15(mp):
        return _pd15.DataFrame({"code": list(mp), "name": list(mp.values())})

    patrol._load_state = lambda lg: _mk_state15({
        "000999": {"kind": "stock", "name": "华润三九", "shares": 100},
        "002648": {"kind": "stock", "name": "卫星化学", "shares": 100}})
    _old_fetch15 = patrol.datasource.fetch_code_names
    patrol.datasource.fetch_code_names = lambda: _ck15(
        {"000999": "ST华润", "002648": "卫星化学"})
    try:
        _a15 = patrol.check_holdings_risk("50k", "2026-11-20")
        check("持仓被 ST → 高危告警", bool(_a15) and _a15[0]["level"] == "高"
              and _a15[0]["code"] == "000999", str(_a15))
        check("持仓被 ST → 提示清仓动作", "清仓" in str(_a15[0].get("action") or ""),
              str(_a15[0].get("action")))

        patrol.datasource.fetch_code_names = lambda: _ck15(
            {"000999": "华润三九", "002648": "卫星化学"})
        check("持仓简称正常 → 无告警", patrol.check_holdings_risk("50k", "2026-11-20") == [])

        def _boom15():
            raise RuntimeError("名单接口挂了")

        patrol.datasource.fetch_code_names = _boom15
        _a15b = patrol.check_holdings_risk("50k", "2026-11-20")
        check("名单取不到 → 低危 fail-soft", bool(_a15b) and _a15b[0]["level"] == "低",
              str(_a15b))

        patrol._load_state = lambda lg: _mk_state15({"sh510050": {"kind": "etf", "shares": 200}})
        check("只有 ETF 持仓 → 不查名单", patrol.check_holdings_risk("50k", "2026-11-20") == [])
    finally:
        patrol._load_state = _old_n15
        patrol.datasource.fetch_code_names = _old_fetch15

    # ---------- 16) 业绩预告（持仓预亏/预减） ----------
    import pandas as _pd16
    _old_n16 = patrol._load_state
    _old_f16 = patrol.datasource.fetch_forecast
    _old_l16 = patrol.PATROL.get("forecast_lookback_days")

    def _fc16(typ, report="2026-09-30", notice="2026-10-20"):
        return _pd16.DataFrame([{
            "代码": "000999", "名称": "华润三九", "报告期": report,
            "公告日": notice, "指标": "净利润", "预告类型": typ,
            "净利润下限": 1000.0, "净利润上限": 2000.0,
            "同比下限": -60.0, "同比上限": -40.0, "是否最新": "T", "预告摘要": ""}])

    try:
        patrol._load_state = lambda lg: {"holdings": {
            "000999": {"kind": "stock", "name": "华润三九", "shares": 100}}}
        patrol.PATROL["forecast_lookback_days"] = 180

        patrol.datasource.fetch_forecast = lambda c: _fc16("预亏")
        _a16 = patrol.check_forecasts("50k", "2026-11-20")
        check("业绩预告预亏 → 高危", bool(_a16) and _a16[0]["level"] == "高"
              and _a16[0]["code"] == "000999", str(_a16))

        patrol.datasource.fetch_forecast = lambda c: _fc16("略减")
        _a16b = patrol.check_forecasts("50k", "2026-11-20")
        check("业绩预告略减 → 中危", bool(_a16b) and _a16b[0]["level"] == "中", str(_a16b))

        patrol.datasource.fetch_forecast = lambda c: _fc16("预增")
        check("业绩预告预增 → 无告警", patrol.check_forecasts("50k", "2026-11-20") == [])

        patrol.datasource.fetch_forecast = lambda c: _fc16("预亏", notice="2025-01-01")
        check("过期预告（>180 天）→ 不报", patrol.check_forecasts("50k", "2026-11-20") == [])

        def _boom16(c):
            raise RuntimeError("预告接口挂了")

        patrol.datasource.fetch_forecast = _boom16
        _a16c = patrol.check_forecasts("50k", "2026-11-20")
        check("预告取不到 → 低危 fail-soft", bool(_a16c) and _a16c[0]["level"] == "低", str(_a16c))

        patrol.datasource.fetch_forecast = lambda c: _pd16.DataFrame([{"代码": "000999"}])
        _a16d = patrol.check_forecasts("50k", "2026-11-20")
        check("预告缺列 → 不抛、低危", bool(_a16d) and _a16d[0]["level"] == "低"
              and "缺列" in _a16d[0]["detail"], str(_a16d))

        patrol._load_state = lambda lg: {"holdings": {"sh510050": {"kind": "etf", "shares": 200}}}
        check("只有 ETF 持仓 → 不查预告", patrol.check_forecasts("50k", "2026-11-20") == [])
    finally:
        patrol._load_state = _old_n16
        patrol.datasource.fetch_forecast = _old_f16
        if _old_l16 is None:
            patrol.PATROL.pop("forecast_lookback_days", None)
        else:
            patrol.PATROL["forecast_lookback_days"] = _old_l16

    print("\n17) 休市日：只报一行、不结算")
    _it_old, _lt_old = patrol.datasource.is_trading_day, patrol.datasource.last_trading_day
    try:
        patrol.datasource.is_trading_day = lambda day="": False
        patrol.datasource.last_trading_day = lambda day="": "2026-11-20"
        _note = patrol.market_closed_note()
        check("休市" in _note and "2026-11-20" in _note, "休市日给出「今日休市（最近交易日 …）」")
        patrol.datasource.is_trading_day = lambda day="": True
        check(patrol.market_closed_note() == "", "交易日不拦")
    finally:
        patrol.datasource.is_trading_day, patrol.datasource.last_trading_day = _it_old, _lt_old

    print("\n18) S4 A 类回归：ATR 移动止损 / 权益层分母 / 未分类文案 / 行业映射合并")
    tmp18 = tempfile.mkdtemp(prefix="patrol_s18_")
    _old_out18, _old_lg18 = config.OUT_DIR, config.PAPER.get("ledgers")
    try:
        config.OUT_DIR = tmp18
        config.PAPER["ledgers"] = {"t18": {"capital": 50000.0,
                                           "state_file": os.path.join(tmp18, "s18.json")}}
        # ---------- 18a) ATR14 读取 ----------
        check("无 stage1_candidates.csv → 空表", patrol._atr14_map() == {})
        with io.open(os.path.join(tmp18, "stage1_candidates.csv"), "w",
                     encoding="utf-8-sig", newline="") as f:
            f.write("code,atr14\n600555,0.5\n601058,0.4387\n")
        _am = patrol._atr14_map()
        check("atr14 按 6 位代码入表",
              _am.get("600555") == 0.5 and _am.get("601058") == 0.4387, str(_am))

        # ---------- 18b) 移动止损提示只在真能上移时报 ----------
        _pos = [{"code": "600555", "name": "盈利股", "stop_price": 9.0,
                 "cost": 10.0, "last": 11.5}]
        _ta = patrol._trail_stop_alerts(_pos, _am)
        check("新止损高于现止损 → 报「中」", len(_ta) == 1 and _ta[0]["level"] == "中", str(_ta))
        check("动作写明由决策日自动上移", "自动上移" in _ta[0]["action"], str(_ta))
        test2 = patrol._trail_stop_alerts(_pos, {"600555": 2.0})
        check("算出的新止损低于现止损 → 不报", test2 == [], str(test2))
        _ta3 = patrol._trail_stop_alerts(_pos, {})
        check("缺 ATR14 → 只报「低」并提示补列",
              len(_ta3) == 1 and _ta3[0]["level"] == "低" and "ATR14" in _ta3[0]["detail"],
              str(_ta3))
        _pos_lo = [{"code": "600556", "name": "小赚股", "stop_price": 9.0,
                    "cost": 10.0, "last": 10.5}]
        check("浮盈 ≤10% → 不报", patrol._trail_stop_alerts(_pos_lo, {"600556": 0.1}) == [])

        # ---------- 18c) 行业集中度分母 = 权益层市值（不含场外 R2 腿）----------
        st18 = mk_state(cash=0.0, holdings=[
            hold("600666", "银行甲", shares=1000, cost=10.0, last=10.0, industry="银行"),
            hold("600777", "银行乙", shares=1000, cost=10.0, last=10.0, industry="银行"),
            hold("600888", "半导体", shares=100, cost=10.0, last=10.0, industry="半导体"),
        ])
        with io.open(os.path.join(tmp18, "s18.json"), "w", encoding="utf-8") as f:
            json.dump(st18, f, ensure_ascii=False)
        _cap18 = {}
        _old_conc18, _old_mv18 = patrol.industry.concentration, patrol.otc_book.market_value
        patrol.industry.concentration = (
            lambda pos, cap=None, base=None: (_cap18.__setitem__("base", base),
                                              {"violated": [], "cap": 0.25})[1])
        patrol.otc_book.market_value = lambda st: 5000.0
        try:
            patrol.check_ledger("t18", "2026-10-29")
        finally:
            patrol.industry.concentration, patrol.otc_book.market_value = _old_conc18, _old_mv18
        check("集中度分母 = 权益层 21000（不含场外 5000）", _cap18.get("base") == 21000.0,
              str(_cap18))

        # ---------- 18d) 「未分类」不再算行业超限，改为映射缺失告警 ----------
        st18b = mk_state(cash=0.0, holdings=[
            hold("600666", "未知甲", shares=2000, cost=10.0, last=10.0),
            hold("600777", "已知乙", shares=1000, cost=10.0, last=10.0, industry="银行"),
        ])
        with io.open(os.path.join(tmp18, "s18.json"), "w", encoding="utf-8") as f:
            json.dump(st18b, f, ensure_ascii=False)
        _old_map18 = patrol.industry.load_map
        patrol.industry.load_map = lambda: {}
        try:
            one18b = patrol.check_ledger("t18", "2026-10-29")
        finally:
            patrol.industry.load_map = _old_map18
        _ov18 = [a for a in one18b["alerts"] if a["kind"] == "行业超限"]
        _unk18 = [a for a in one18b["alerts"] if a["kind"] == "行业映射缺失"]
        _known = [a for a in _ov18 if "银行" in a["detail"]]
        check("未分类不再出现在行业超限", not any("未分类" in a["detail"] for a in _ov18), str(_ov18))
        check("未分类占比 66.7% → 报「行业映射缺失」中危",
              len(_unk18) == 1 and _unk18[0]["level"] == "中", str(_unk18))
        check("映射缺失动作=先补映射",
              bool(_unk18) and "补映射" in _unk18[0]["action"], str(_unk18))
        check("已知行业超限仍提示减仓（分支没误伤）",
              bool(_known) and all("优先减" in a["action"] for a in _known), str(_ov18))

        # ---------- 18e) 宽基 ETF 不算「未分类」，也不占行业额度 ----------
        st18e = mk_state(cash=0.0, holdings=[
            {"code": "sh510050", "name": "上证50ETF华夏", "kind": "etf", "layer": "ETF",
             "level": "R3", "shares": 1000, "cost_basis": 2.0, "opened_at": "2026-06-01",
             "last_buy_date": "2026-06-01", "last_close": 3.0, "highest_close": 3.0,
             "stop_price": None, "industry": "", "target_price": None,
             "thesis": None, "falsifiers": []},
            hold("600777", "已知乙", shares=1000, cost=10.0, last=10.0, industry="银行"),
        ])
        with io.open(os.path.join(tmp18, "s18.json"), "w", encoding="utf-8") as f:
            json.dump(st18e, f, ensure_ascii=False)
        one18e = patrol.check_ledger("t18", "2026-10-29")
        check("宽基 ETF 无行业字段不算「未分类」",
              not any(a["kind"] == "行业映射缺失" for a in one18e["alerts"]),
              str(kinds(one18e)))
        check("宽基 ETF 不占行业额度：银行按权益层分母 76.9% 报超限",
              any(a["kind"] == "行业超限" and "76.9%" in a["detail"]
                  for a in one18e["alerts"]),
              str([a["detail"] for a in one18e["alerts"] if a["kind"] == "行业超限"]))

        # ---------- 18f) 场外净值陈旧按交易日算（长假不误报）----------
        _old_cal18 = patrol.datasource.fetch_trade_calendar
        patrol.datasource.fetch_trade_calendar = lambda: {
            "trade_date": ["2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
                           "2026-10-09", "2026-10-12", "2026-10-13"]}
        try:
            _td, _fb = patrol._trading_days_since("2026-09-30", "2026-10-13")
            check("跨国庆 13 个自然日 = 3 个交易日（10-09/10-12/10-13）",
                  _td == 3 and _fb is False, f"{_td},{_fb}")
            _td2, _ = patrol._trading_days_since("2026-10-12", "2026-10-13")
            check("相邻交易日 = 1", _td2 == 1, str(_td2))
        finally:
            patrol.datasource.fetch_trade_calendar = _old_cal18
        _old_cal18b = patrol.datasource.fetch_trade_calendar

        def _boom():
            raise RuntimeError("no calendar")

        patrol.datasource.fetch_trade_calendar = _boom
        try:
            _td3, _fb3 = patrol._trading_days_since("2026-10-01", "2026-10-13")
            check("日历缺失 → 退回自然日并标注", _td3 == 12 and _fb3 is True, f"{_td3},{_fb3}")
        finally:
            patrol.datasource.fetch_trade_calendar = _old_cal18b

        # ---------- 18e) industry --refresh 合并旧映射（不再整表覆盖）----------
        _old_rows = [{"code": "600001", "industry": "银行"}]
        _new_rows = [{"code": "600001", "industry": "证券"}, {"code": "600002", "industry": "半导体"}]
        _mg = {r["code"]: r["industry"] for r in patrol.industry.merge_rows(_old_rows, _new_rows, [])}
        check("合并：旧行保留、同码新值优先",
              _mg.get("600001") == "证券" and _mg.get("600002") == "半导体", str(_mg))
        _mg2 = {r["code"]: r["industry"] for r in
                patrol.industry.merge_rows(_old_rows, [{"code": "600001", "industry": ""}], ["600001"])}
        check("持仓新行没行业 → 旧值兜底", _mg2.get("600001") == "银行", str(_mg2))
        check("覆盖率 95/100 → 接受", patrol.industry.coverage_ok(100, 95) is True)
        check("覆盖率 80/100 → 拒绝覆盖", patrol.industry.coverage_ok(100, 80) is False)
        check("旧表为空 → 接受", patrol.industry.coverage_ok(0, 0) is True)
    finally:
        config.OUT_DIR = _old_out18
        config.PAPER["ledgers"] = _old_lg18
        shutil.rmtree(tmp18, ignore_errors=True)

    print(f"\n{'-' * 60}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
