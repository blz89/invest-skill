#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`industry.py` / `sentiment.py` / `macro.py` 的纯函数自测（不联网、不写 out/）。

只测「算得对不对」，不测接口通不通——接口是运气，算法是本事。
    python scripts/_selftest_layers.py
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import industry
import macro
import sentiment

N = [0]
FAIL = []


def check(name: str, cond: bool) -> None:
    N[0] += 1
    if not cond:
        FAIL.append(name)


def eq(name: str, got, want) -> None:
    check(f"{name}（期望 {want!r}，实得 {got!r}）", got == want)


def close(name: str, got, want, tol=1e-6) -> None:
    ok = got is not None and want is not None and abs(float(got) - float(want)) <= tol
    check(f"{name}（期望 {want}±{tol}，实得 {got}）", ok)


# ================================================================ 行业层
def test_industry() -> None:
    eq("industry.norm_code 去后缀", industry.norm_code("601058.SH"), "601058")
    eq("industry.norm_code 补零", industry.norm_code("1"), "000001")
    eq("industry.norm_code 非数字原样", industry.norm_code("abc"), "abc")
    eq("industry.norm_code 去 sh 前缀（与 codes.py 统一）", industry.norm_code("sh510300"), "510300")
    eq("industry.norm_code 去 bj 前缀（与 codes.py 统一）", industry.norm_code("bj430047"), "430047")

    raw = {
        "000999.SZ": [{"name": "中药", "type": "industry"},
                      {"name": "深圳", "type": "region"},
                      {"name": "高股息股", "type": "tszs"},
                      {"name": "同花顺全A(沪深)", "type": "tszs"}],
        "600519.SH": [{"name": "白酒", "type": "industry"},
                      {"name": "贵州", "type": "region"}],
    }
    rows = industry.rows_of(["000999", "600519"], raw)
    eq("rows_of 行数", len(rows), 2)
    eq("rows_of 000999 行业", rows[0]["industry"], "中药")
    eq("rows_of 000999 地域", rows[0]["region"], "深圳")
    eq("rows_of 000999 特色去指数", rows[0]["tszs"], "高股息股")
    eq("rows_of 600519 行业", rows[1]["industry"], "白酒")
    eq("rows_of 600519 地域", rows[1]["region"], "贵州")
    eq("rows_of 600519 无特色", rows[1]["tszs"], "")
    # fail-closed：数据源没给行业 → 标「未分类」，不能留空串（空串会被下游当成「无行业」
    # 而绕过行业分散上限；2026-11-07 B 类 #6）。
    eq("rows_of 缺失代码 → 未分类", industry.rows_of(["000001"], {})[0]["industry"], "未分类")
    eq("normalize 空串 → 未分类", industry.normalize(""), "未分类")
    eq("normalize nan → 未分类", industry.normalize("nan"), "未分类")
    eq("normalize '-' → 未分类", industry.normalize(" - "), "未分类")
    eq("normalize 正常行业原样", industry.normalize(" 银行 "), "银行")
    # 600233 圆通速递：新浪板块成员表还是借壳前「大杨创世（服装鞋类）」的旧标签，
    # 代码级修正表必须把它拉回交通运输（否则 pe_pct_ind 在服装样本里算分位）。
    eq("600233 修正为交通运输", industry.normalize("服装鞋类", "600233"), "交通运输")
    eq("600233 带后缀也修正", industry.normalize("服装鞋类", "600233.SH"), "交通运输")
    eq("未知代码不瞎改", industry.normalize("服装鞋类", "600999"), "服装鞋类")
    eq("industry_map 修正表命中", industry.INDUSTRY_FIX.get("600233"), "交通运输")
    # 覆盖率不下降：修正表只动 600233，其它真实行业标签必须原样保留（不能被一刀切成未分类）
    for _lab in ("银行", "白酒", "半导体", "物流", "交通运输", "服装鞋类"):
        eq(f"真实行业原样保留：{_lab}", industry.normalize(_lab, "000001"), _lab)

    eq("_pick 按 type 过滤", industry._pick(raw["000999.SZ"], "region"), ["深圳"])
    eq("_pick 空输入", industry._pick(None, "industry"), [])

    pos = [{"code": "1", "name": "A", "amount": 3000, "weight": 0.3, "industry": "中药"},
           {"code": "2", "name": "B", "amount": 2000, "weight": 0.2, "industry": "中药"},
           {"code": "3", "name": "C", "amount": 5000, "weight": 0.5, "industry": "白酒"}]
    res = industry.concentration(pos, cap=0.25, base=10000)
    eq("concentration 行业数", len(res["rows"]), 2)
    eq("concentration 最大行业", res["rows"][0]["industry"], "中药")
    close("concentration 中药占比", res["rows"][0]["weight"], 0.5)
    check("concentration 中药超限", res["rows"][0]["over"] is True)
    check("concentration 白酒也超限（50%）", res["rows"][1]["over"] is True)
    eq("concentration 超限个数", len(res["violated"]), 2)
    eq("concentration 合计", res["total"], 10000.0)

    res2 = industry.concentration(pos, cap=0.25, base=20000)
    close("concentration 分母放大后中药=25%", res2["rows"][0]["weight"], 0.25)
    check("concentration 恰好等于上限不算超", res2["rows"][0]["over"] is False)

    eq("concentration 空持仓", industry.concentration([], 0.25, 0)["rows"], [])
    _unk = industry.concentration([{"code": "9", "amount": 100}], 0.25, 100)
    eq("concentration 未分类不进行业 rows（数据缺失 ≠ 一个行业）", _unk["rows"], [])
    close("concentration 未分类占比单独返回", _unk["unclassified_weight"], 1.0)
    check("concentration 未分类不算超限", _unk["violated"] == [])
    _mix = industry.concentration(
        [{"code": "1", "amount": 5000, "industry": "中药"},
         {"code": "9", "amount": 5000}], 0.25, 10000)
    close("concentration 中药按含未分类的分母算 50%", _mix["rows"][0]["weight"], 0.5)
    close("concentration 未分类占一半", _mix["unclassified_weight"], 0.5)
    eq("res_total 求和", industry.res_total(pos), 10000.0)


# ================================================================ 情绪层
def test_sentiment() -> None:
    eq("sentiment.norm_code", sentiment.norm_code("000999.SZ"), "000999")
    eq("sentiment.code_of ticker 优先", sentiment.code_of({"ticker": "600118", "thscode": "600118.SH"}), "600118")
    eq("sentiment.code_of 退 thscode", sentiment.code_of({"thscode": "000678.SZ"}), "000678")
    eq("sentiment.code_of 空", sentiment.code_of({}), "")

    up = [{"ticker": "600118", "name": "中国卫星", "continue_day_cnt": 1,
           "continue_day_text": "首板"},
          {"ticker": "000678", "name": "襄阳轴承", "continue_day_cnt": 3,
           "continue_day_text": "3连板"}]
    down = [{"ticker": "600000", "name": "浦发银行", "price_change_ratio_pct": -10.0}]
    brk = [{"ticker": "300001", "name": "特锐德"}]
    dt_items = [{"ticker": "000999", "name": "华润三九", "net_value": -40138486.69,
                 "net_rate": -0.01868205}]
    hot = [{"ticker": "000678", "name": "襄阳轴承", "rank": 1},
           {"ticker": "688836", "name": "宇树科技", "rank": 2}]

    t = sentiment.market_temp(up, down, brk, dt_items, hot)
    eq("market_temp 涨停数", t["limit_up"], 2)
    eq("market_temp 跌停数", t["limit_down"], 1)
    eq("market_temp 炸板数", t["break"], 1)
    close("market_temp 封板率 2/(2+1)", t["seal_ratio"], 0.6667, 1e-4)
    eq("market_temp 最高连板", t["max_board"], 3)
    eq("market_temp 连板分布", t["board_dist"], {"1": 1, "3": 1})
    eq("market_temp 龙虎榜数", t["dragon_tiger"], 1)
    eq("market_temp 热榜前三", [x["name"] for x in t["hot_top3"]], ["襄阳轴承", "宇树科技"])
    check("temp_comment 有内容", len(sentiment.temp_comment(t)) > 4)

    eq("temp_comment 全零", "没有涨跌停样本" in sentiment.temp_comment(
        sentiment.market_temp([], [], [], [], [])), True)
    cold = sentiment.market_temp([], down * 3, [], [], [])
    check("temp_comment 跌停多判偏冷", "偏冷" in sentiment.temp_comment(cold))
    hotm = sentiment.market_temp(up * 20, [], [], [], [])
    check("temp_comment 高封板判偏强", "偏强" in sentiment.temp_comment(hotm))

    flags = sentiment.flags_of(["600000", "000999", "000678", "300001"],
                               up, down, brk, dt_items, hot, {"000999": [{"reason": "业绩预增"}]})
    kinds = {f["kind"] for f in flags}
    check("flags 含跌停", "跌停" in kinds)
    check("flags 含炸板", "炸板" in kinds)
    check("flags 含涨停", "涨停" in kinds)
    check("flags 含龙虎榜", "龙虎榜" in kinds)
    check("flags 含异动", "异动" in kinds)
    eq("flags 高级别排最前", flags[0]["level"], "高")
    dtf = [f for f in flags if f["kind"] == "龙虎榜"][0]
    check("flags 龙虎榜净额写成万元", "-4014 万" in dtf["detail"])

    eq("flags 忽略不在关注表里的", sentiment.flags_of(["600518"], up, [], [], [], [], {}), [])
    md = sentiment.brief_md(t, flags, "2026-10-29 00:00:00")
    check("brief 含标题", md.startswith("# 情绪层简报"))
    check("brief 声明不产生信号", "不产生买卖信号" in md)

    # 就绪/过期判定（2026-11-25 加：空返回不能当成「今天 0 家」）
    rd = sentiment.readiness(t, "2026-10-29", "2026-10-29")
    eq("readiness 有样本即就绪", rd["ready"], True)
    eq("readiness 未过期", rd["stale"], False)
    empty = sentiment.market_temp([], [], [], [], [])
    eq("readiness 全空未就绪", sentiment.readiness(empty, "", "2026-10-29")["ready"], False)
    old = sentiment.readiness(t, "2026-10-01", "2026-10-29")
    eq("readiness 过期天数", old["stale_days"], 28)
    eq("readiness 过期标记", old["stale"], True)
    md2 = sentiment.brief_md(empty, [], "2026-10-29 00:00:00")
    check("brief 未就绪有警告", "数据未就绪" in md2)
    md3 = sentiment.brief_md({**t, **old}, [], "2026-10-29 00:00:00")
    check("brief 过期有警告", "数据过期" in md3)


# ================================================================ 宏观层
def test_macro() -> None:
    eq("month_key 中文年月", macro.month_key("2026年09月份"), "2026-09")
    eq("month_key YYYYMM", macro.month_key("202609"), "2026-09")
    eq("month_key 带横线", macro.month_key("2026-9"), "2026-09")
    eq("month_key 非法", macro.month_key("abc"), "")
    eq("month_key 月份越界", macro.month_key("2026年13月份"), "")

    eq("num 正常", macro.num("1.5"), 1.5)
    eq("num NaN 转 None", macro.num(float("nan")), None)
    eq("num 空串", macro.num(""), None)

    rows = [{"月份": "2026年01月份", "v": 1}, {"月份": "2026年09月份", "v": 9},
            {"月份": "2025年12月份", "v": 0}]
    eq("latest_by 取最新", macro.latest_by(rows, "月份")["v"], 9)
    eq("latest_by 空表", macro.latest_by([], "月份"), {})
    eq("latest_by 无 key 列", macro.latest_by([{"x": 1}], "月份"), {})

    close("real_rate", macro.real_rate(3.0, 0.9), 2.1)
    eq("real_rate 缺参", macro.real_rate(None, 0.9), None)
    close("m1_m2_gap", macro.m1_m2_gap(3.2, 7.2), -4.0)
    eq("pmi_state 扩张", macro.pmi_state(50.1), "扩张")
    eq("pmi_state 持平", macro.pmi_state(50), "持平")
    eq("pmi_state 收缩", macro.pmi_state(49.9), "收缩")
    eq("pmi_state 空", macro.pmi_state(None), "")

    sh = [{"3M-定价": 1.40}, {"3M-定价": 1.42}, {"3M-定价": 1.44}]
    tr = macro.shibor_trend(sh, days=3)
    close("shibor 最新", tr["last"], 1.44)
    close("shibor 均值", tr["mean20"], (1.40 + 1.42 + 1.44) / 3, 1e-9)
    close("shibor 偏离 bp", tr["delta_bp"], 2.0, 0.02)
    eq("shibor 空", macro.shibor_trend([]), {})

    doc = {"generated_at": "2026-10-29 00:00:00", "real_rate": 2.1, "m1_m2_gap": -4.0,
           "data": {"lpr": {"lpr1y": 3.0, "lpr5y": 3.5, "month": "2026-10-20"},
                    "shibor": {"last": 1.4009, "mean20": 1.4213, "delta_bp": -2.0},
                    "cpi": {"yoy": 0.9, "month": "2026-09"},
                    "ppi": {"yoy": 5.0, "month": "2026-09"},
                    "pmi": {"manufacturing": 50.1, "non_manufacturing": 50.2},
                    "money_supply": {"m1_yoy": 3.2, "m2_yoy": 7.2, "month": "2026-09"}}}
    md = macro.brief_md(doc)
    check("macro brief 含 LPR", "LPR：1Y 3.0%" in md)
    check("macro brief 含实际利率", "实际政策利率 ≈ 2.1%" in md)
    check("macro brief 含剪刀差", "-4.0pp" in md)
    check("macro brief 声明背景板", "不参与打分" in md)


# ================================================================ 数据层钩子
def test_market_hook() -> None:
    """取数层注入后，行业列必须走 invest 的修正表（stock 只给原始板块名）。"""
    import config  # noqa: F401  确保 install() + set_market_hook() 已执行
    import datasource as ds

    check("datasource 已注册行业归一钩子", ds._MARKET_HOOK is not None)
    if ds._MARKET_HOOK is None:
        return
    import pandas as pd

    df = pd.DataFrame({"code": ["600233", "000001", "999999"],
                       "industry": ["服装鞋类", "银行", float("nan")]})
    out = ds._MARKET_HOOK(df)
    eq("行业归一：600233 服装鞋类→交通运输", out["industry"].iloc[0], "交通运输")
    eq("行业归一：000001 银行 保持", out["industry"].iloc[1], "银行")
    eq("行业归一：空值→未分类", out["industry"].iloc[2], "未分类")
    check("行业归一不改动原 df", df["industry"].iloc[0] == "服装鞋类")


def main() -> int:
    print("=" * 60)
    print("_selftest_layers：行业层 / 情绪层 / 宏观层（纯函数，不联网）")
    print("=" * 60)
    test_industry()
    test_sentiment()
    test_macro()
    test_market_hook()
    print("-" * 60)
    if FAIL:
        print(f"通过 {N[0] - len(FAIL)} 项，失败 {len(FAIL)} 项")
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print(f"通过 {N[0]} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
