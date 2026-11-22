# -*- coding: utf-8 -*-
"""etf.py 自测：场内基金代码前缀映射、连续取数失败护栏（离线、不联网、不写盘）。

背景：stock 数据层收口后，`sina_symbol()` 按**股票**规则拼前缀，
场内基金 5xxxxx/1xxxxx 被拼成 bj 开头 → 新浪基金接口不认 → 每只重试 4 次≈15s，
全表 200+ 只吃满 run.py 单步 30 分钟超时（etf 分支被杀、stage2/3 未产出）。
这里锁住两件事：①前缀必须按基金号段拼；②整体性取数失败要 fail loud 中止，不能静默烧满超时。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pandas as pd  # noqa: E402

import etf  # noqa: E402
from etf_codes import etf_sym as _etf_sym_shared  # noqa: E402

_ok = 0
_fail = []


def ck(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
    else:
        _fail.append(f"{name}{(' ← ' + detail) if detail else ''}")


def test_sym():
    cases = {
        "510050": "sh510050",      # 沪市 ETF（原 bug：sj 数据层拼成 bj510050）
        "588000": "sh588000",      # 科创 50 ETF
        "560010": "sh560010",
        "159915": "sz159915",      # 深市 ETF（原 bug：拼成 bj159915）
        "161725": "sz161725",      # 深市 LOF
        "sh510300": "sh510300",    # 已带前缀：原样返回
        "SZ159915": "sz159915",    # 大小写归一
    }
    for code, want in cases.items():
        got = etf._etf_sym(code)
        ck(f"_etf_sym({code}) → {want}", got == want, got)


def test_consecutive_fail_guard():
    """整体性取数失败（如前缀全错）→ 连错 8 只就 raise，不再空烧 30 分钟超时。"""
    codes = ["510300", "510500", "588000", "159915", "512880", "515000",
             "159919", "510050", "588080", "159949", "512660", "560010"]
    spot = pd.DataFrame({"代码": codes, "名称": ["沪深300ETF"] * len(codes),
                         "成交额": [5.0e9] * len(codes)})
    calls = {"n": 0}
    old_spot, old_metrics = etf.ds.fetch_etf_spot, etf.etf_metrics

    def fake_spot():
        return spot

    def fake_metrics(sym, name):
        calls["n"] += 1
        raise RuntimeError(f"{sym} 取数失败（模拟接口异常）")

    etf.ds.fetch_etf_spot = fake_spot
    etf.etf_metrics = fake_metrics
    try:
        try:
            etf.run_screen(verbose=False)
            ck("连续失败触发中止", False, "未抛 RuntimeError")
        except RuntimeError as e:
            msg = str(e)
            ck("连续失败触发中止", "连续 8 只取数失败" in msg, msg)
            ck("中止前只试了 8 只（不烧满超时）", calls["n"] == etf._CONSEC_FAIL_LIMIT, str(calls["n"]))
            ck("中止信息带最近失败原因", "取数失败" in msg, msg)
    finally:
        etf.ds.fetch_etf_spot, etf.etf_metrics = old_spot, old_metrics


def test_single_failure_does_not_abort():
    """零星失败（单只有问题）不应中止全表：失败计数清零、其余照常走完。"""
    spot = pd.DataFrame({"代码": ["510300", "510500"], "名称": ["沪深300ETF", "中证500ETF"],
                         "成交额": [5.0e9, 5.0e9]})
    old_spot, old_metrics = etf.ds.fetch_etf_spot, etf.etf_metrics

    def fake_metrics(sym, name):
        if sym == "sh510300":
            raise RuntimeError("单只接口异常")
        return {"ok": True, "sym": sym, "name": name, "price": 1.0, "lot_cost": 100.0,
                "price_date": "2026-11-16", "hist_days": 800, "amt20_yi": 2.0,
                "vs_ma200": 0.0, "vs_ma60": 0.0, "ret_1y": 0.0, "ret_3y": 0.2,
                "dd_1y": -0.1, "dd_3y": -0.2, "max_1d_move": 0.02, "split_suspect": False}

    etf.ds.fetch_etf_spot = lambda: spot
    etf.etf_metrics = fake_metrics
    try:
        df, funnel = etf.run_screen(verbose=False)
        ck("单只失败不中止全表", funnel.get("history_fetched") == 1, str(funnel.get("history_fetched")))
        ck("单只失败记入 history_failed", funnel.get("history_failed") == 1, str(funnel.get("history_failed")))
        ck("正常那只仍产出", len(df) == 1, str(len(df)))
    finally:
        etf.ds.fetch_etf_spot, etf.etf_metrics = old_spot, old_metrics


def test_limit_still_works():
    """--limit 截断后仍走同一套护栏（候选为空时不炸）。"""
    spot = pd.DataFrame({"代码": [], "名称": [], "成交额": []})
    old_spot = etf.ds.fetch_etf_spot
    etf.ds.fetch_etf_spot = lambda: spot
    try:
        df, funnel = etf.run_screen(limit=2, verbose=False)
        ck("空候选表不抛且输出空表", df is not None and len(df) == 0)
        ck("空候选表漏斗 spot_total=0", funnel.get("spot_total") == 0, str(funnel.get("spot_total")))
    finally:
        etf.ds.fetch_etf_spot = old_spot


def test_shared_helper_is_single_source():
    """etf.py 的 _etf_sym 只是 etf_codes.etf_sym 的别名，避免两处各写一份前缀规则。"""
    for code in ("510050", "159915", "sh510300", "SZ159915", "000001"):
        ck(f"etf._etf_sym({code}) 与 etf_codes 一致",
           etf._etf_sym(code) == _etf_sym_shared(code),
           f"{etf._etf_sym(code)} vs {_etf_sym_shared(code)}")


def test_wealth_uses_etf_prefix():
    """wealth.py 从新浪 ETF 全表拿的是裸代码，必须补前缀再取历史（2026-11-16 511360 事故）。"""
    import wealth
    spot = pd.DataFrame({"代码": ["511360", "159649"], "名称": ["短融ETF", "国债ETF"],
                         "成交额": [8.0e8, 8.0e8]})
    seen = []
    old_spot, old_hist = wealth.ds.fetch_etf_spot, wealth.ds.fetch_etf_hist
    wealth.ds.fetch_etf_spot = lambda: spot
    wealth.ds.fetch_etf_hist = lambda sym: (seen.append(sym), None)[1]
    try:
        wealth.run_screen(verbose=False)
    finally:
        wealth.ds.fetch_etf_spot, wealth.ds.fetch_etf_hist = old_spot, old_hist
    ck("wealth 沪市基金补 sh 前缀", "sh511360" in seen, str(seen))
    ck("wealth 深市基金补 sz 前缀", "sz159649" in seen, str(seen))
    ck("wealth 不再把裸代码喂给数据层", "511360" not in seen and "159649" not in seen, str(seen))


def main():
    for fn in (test_sym, test_consecutive_fail_guard, test_single_failure_does_not_abort,
               test_limit_still_works, test_shared_helper_is_single_source,
               test_wealth_uses_etf_prefix):
        fn()
    if _fail:
        print(f"etf 自测：通过 {_ok} 项，失败 {len(_fail)} 项")
    else:
        print(f"etf 自测：通过 {_ok} 项，失败 0 项")
    for f in _fail:
        print("  ✗", f)
    return 0 if not _fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
