#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fund.py` 的纯函数自测（不联网、不写 out/）。

测的是「复算得对不对」，不是「同花顺通不通」——后者要看 `fund.py --health` 的实跑结果。
    python scripts/_selftest_fund.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fund

N = [0]
FAIL = []


def check(name: str, cond: bool) -> None:
    N[0] += 1
    if not cond:
        FAIL.append(name)


def eq(name: str, got, want) -> None:
    check(f"{name}（期望 {want!r}，实得 {got!r}）", got == want)


def close(name: str, got, want, tol=1e-9) -> None:
    ok = got is not None and want is not None and abs(float(got) - float(want)) <= tol
    check(f"{name}（期望 {want}±{tol}，实得 {got}）", ok)


def series_from(points, start="2023-01-02", step_days=1):
    """[(日期偏移天数, 净值)] → [(YYYY-MM-DD, nav)]。"""
    d0 = dt.date.fromisoformat(start)
    return [((d0 + dt.timedelta(days=off)).isoformat(), float(v)) for off, v in points]


# ------------------------------------------------------------------ 代码归一
def test_norm() -> None:
    eq("norm_code 纯数字", fund.norm_code("018074"), "018074")
    eq("norm_code .OF", fund.norm_code("018074.OF"), "018074")
    eq("norm_code OF 前缀", fund.norm_code("of018074"), "018074")
    eq("norm_code 补零", fund.norm_code("18074"), "018074")
    eq("norm_code 空", fund.norm_code(""), "")
    eq("norm_code None", fund.norm_code(None), "")
    eq("norm_code 大写含后缀", fund.norm_code("018074.of"), "018074")


def test_small() -> None:
    eq("_f 正常", fund._f("1.5"), 1.5)
    eq("_f nan", fund._f(float("nan")), None)
    eq("_f None", fund._f(None), None)
    eq("_f 空格", fund._f(" "), None)
    eq("_d 横线", fund._d("2026-10-29"), dt.date(2026, 10, 29))
    eq("_d 紧凑", fund._d("20261029"), dt.date(2026, 10, 29))
    eq("_d 斜线", fund._d("2026/10/29"), dt.date(2026, 10, 29))
    eq("_d 非法", fund._d("abc"), None)
    eq("_d 带时间", fund._d("2026-10-29 12:00:00"), dt.date(2026, 10, 29))
    eq("_d 毫秒时间戳", fund._d(1793203200000), dt.date(2026, 10, 29))
    eq("_d 秒时间戳", fund._d(1793203200), dt.date(2026, 10, 29))
    eq("_d 负数", fund._d(-1), None)
    eq("_d 字符串数字", fund._d("20261029"), dt.date(2026, 10, 29))

    close("_ann 3 年累计 9.86% → 年化", fund._ann(0.0986, 3.0), (1.0986 ** (1 / 3)) - 1, 1e-12)
    eq("_ann 全亏光", fund._ann(-1.0, 3.0), None)
    eq("_ann 年数 0", fund._ann(0.1, 0), None)
    eq("_ann None", fund._ann(None, 3), None)


def test_slice_dd() -> None:
    s = series_from([(0, 1.0), (100, 1.1), (200, 0.9), (300, 1.2)])
    seg = fund._slice(s, 365)
    eq("_slice 全在窗口内", len(seg), 4)
    eq("_slice 窗口外剔除", len(fund._slice(s, 150)), 2)
    eq("_slice 不足 2 条", fund._slice(s, 1), [])
    eq("_slice 空序列", fund._slice([], 365), [])

    close("_dd 峰 1.1 谷 0.9", fund._dd(series_from([(0, 1.0), (1, 1.1), (2, 0.9)])), -0.1818181818, 1e-9)
    close("_dd 一路上涨为 0", fund._dd(series_from([(0, 1.0), (1, 1.1), (2, 1.2)])), 0.0)
    eq("_dd 单点", fund._dd(series_from([(0, 1.0)])), None)

    # 恒定日收益 0 → 波动 0
    flat = series_from([(i, 1.0) for i in range(80)])
    close("_vol 不涨不跌=0", fund._vol(flat), 0.0, 1e-12)
    eq("_vol 样本不足", fund._vol(series_from([(i, 1.0) for i in range(10)])), None)


def test_metrics() -> None:
    # 3.2 年、每天 +0.01%：1Y 与 3Y 都应该是正、且 3Y 年化≈日收益×243
    pts = [(i, 1.0 * (1.0001 ** i)) for i in range(0, 1170)]
    m = fund.metrics_from_nav(series_from(pts))
    check("metrics 有 ret_1y", "ret_1y" in m)
    check("metrics 有 ret_3y", "ret_3y" in m)
    check("metrics 有 dd_3y", "dd_3y" in m)
    close("metrics 一路上涨回撤=0", m["dd_3y"], 0.0)
    # 序列按自然日每天 +0.01%，所以 1 年 = 365 个点
    want = 1.0001 ** 365 - 1
    check("metrics 1Y 年化≈(1.0001^365-1)", abs(m["ret_1y"] - want) < 0.004)
    check("metrics 3Y 年化≈(1.0001^365-1)", abs(m["ret_3y"] - want) < 0.004)
    check("metrics 记录序列条数", m["n_days"] == 1170)

    # 先跌 10% 再回：3Y 回撤必须是负的（要点数 >= 60 才进 metrics）
    down = []
    for i in range(0, 1001):
        if i <= 200:
            v = 1.0
        elif i <= 400:
            v = 1.0 - 0.10 * (i - 200) / 200.0
        elif i <= 800:
            v = 0.90 + 0.10 * (i - 400) / 400.0
        else:
            v = 1.0 + 0.05 * (i - 800) / 200.0
        down.append((i, v))
    m2 = fund.metrics_from_nav(series_from(down))
    close("metrics 跌 10% → dd", m2["dd_3y"], -0.10, 1e-9)

    eq("metrics 太短返回 {}", fund.metrics_from_nav(series_from([(i, 1.0) for i in range(10)])), {})
    eq("metrics 空序列", fund.metrics_from_nav([]), {})


def test_verdict() -> None:
    eq("一致", fund._verdict([("1Y", 0.0010), ("3Y", 0.0020)]), "一致")
    eq("接近（1Y 80bp）", fund._verdict([("1Y", 0.0080)]), "接近")
    eq("冲突（1Y 200bp）", fund._verdict([("1Y", 0.0200)]), "冲突")
    eq("冲突（3Y 200bp）", fund._verdict([("3Y", 0.0200)]), "冲突")
    eq("无可比数据", fund._verdict([]), "无可比数据")
    eq("边界 1Y 恰好 60bp 算接近", fund._verdict([("1Y", 0.0060)]), "接近")


def test_brief() -> None:
    res = {"checked_at": "2026-10-29 22:00:00", "source_csv": r"C:\x\stage1_otc.csv",
           "n_checked": 2, "n_ok": 1, "n_error": 1, "n_conflict": 0, "ok": False,
           "rows": [{"code": "018074", "nav_date": "2026-10-28",
                     "ours_ret_1y": 0.0294, "calc_ret_1y": 0.0293,
                     "ours_ret_3y": 0.0308, "calc_ret_3y": 0.0307,
                     "theirs_tyear_ann": 0.0318, "diffs": [("1Y", -0.0001), ("3Y", -0.0001)],
                     "verdict": "一致"},
                    {"code": "011111", "error": "净值序列不足（<60 条）"}]}
    md = fund.brief_md(res)
    check("brief 有标题", md.startswith("# 基金层健康检查"))
    check("brief 结论写明异常", "有异常" in md)
    check("brief 有误差 bp 列", "bp" in md)
    check("brief 失败行有原因", "净值序列不足" in md)
    check("brief 声明不写池子", "只有一个生产者" in md)
    check("brief 声明换口径要人确认", "换口径" in md)


def main() -> int:
    print("=" * 60)
    print("_selftest_fund：基金层复算（纯函数，不联网）")
    print("=" * 60)
    test_norm()
    test_small()
    test_slice_dd()
    test_metrics()
    test_verdict()
    test_brief()
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
