# -*- coding: utf-8 -*-
"""pit（baostock 披露日 / 历史成分）离线自测 —— 不联网、不需要 baostock 登录。

覆盖：代码转换 / 报告期枚举 / **as-of 过滤（核心）** / TTM PE / 百分数换算 / 失败语义。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pit  # noqa: E402

N = {"ok": 0, "fail": 0}


def check(name, cond, detail=""):
    if cond:
        N["ok"] += 1
        print(f"  OK   {name}")
    else:
        N["fail"] += 1
        print(f"  FAIL {name}  {detail}")


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except Exception:                   # noqa: BLE001
        return True


def main():
    print("== A. 代码转换 ==")
    for raw, want in (("600519", "sh.600519"), ("sh600519", "sh.600519"),
                      ("600519.SH", "sh.600519"), ("000568", "sz.000568"),
                      ("sz000568", "sz.000568"), ("300140", "sz.300140"),
                      ("688981", "sh.688981"), ("830799", "bj.830799")):
        got = pit.bs_code(raw)
        check(f"bs_code({raw}) == {want}", got == want, got)
    check("bs_code 位数不对时抛错", raises(pit.bs_code, "60051"))
    check("bs_code 未知市场抛错", raises(pit.bs_code, "999999"))
    check("_plain 去前缀", pit._plain("sh600519") == "600519")

    print("\n== B. 报告期枚举（不含未来季度）==")
    got = pit.periods("2026-10-28", 4)
    check("asof 2026-10-28 枚举", got == [(2026, 3), (2026, 2), (2026, 1), (2025, 4)], str(got))
    check("不含当年未到的 Q4", (2026, 4) not in got)
    got2 = pit.periods("2026-01-15", 2)
    check("年初只到上一年 Q4", got2 == [(2025, 4), (2025, 3)], str(got2))
    check("n 限制生效", len(pit.periods("2026-10-28", 2)) == 2)

    print("\n== C. as-of 过滤（这是修 point-in-time 的核心，必须严格）==")
    # 造一份「披露日」序列：2026Q2 在 08-15 才可见；2026Q3 还没披露
    fake = {
        (2026, 3): None,                                   # 尚未披露
        (2026, 2): {"pubDate": "2026-08-15", "statDate": "2026-06-30", "roeAvg_pct": 17.95},
        (2026, 1): {"pubDate": "2026-04-25", "statDate": "2026-03-31", "roeAvg_pct": 10.57},
        (2025, 4): {"pubDate": "2026-04-17", "statDate": "2025-12-31", "roeAvg_pct": 34.46},
        (2025, 3): {"pubDate": "2025-10-29", "statDate": "2025-09-30", "roeAvg_pct": 26.0},
    }
    orig = pit.financials
    pit.financials = lambda code, y, q: fake.get((y, q))
    try:
        rows = pit.asof("600519", "2026-10-28", 3)
        check("只返回 pubDate<=asof 的期", [r["pubDate"] for r in rows] ==
              ["2026-08-15", "2026-04-25", "2026-04-17"], str([r["pubDate"] for r in rows]))
        check("新的在前", rows[0]["statDate"] == "2026-06-30")
        check("未披露期被丢弃", all(r["statDate"] != "2026-09-30" for r in rows))
        early = pit.asof("600519", "2026-04-20", 5)
        check("as-of 2026-04-20 最近可见是 2025 年报",
              bool(early) and early[0]["pubDate"] == "2026-04-17",
              str([r["pubDate"] for r in early]))
        check("as-of 2026-04-20 看不到 2026 一季报（04-25 才披露）",
              all(r["pubDate"] != "2026-04-25" for r in early),
              str([r["pubDate"] for r in early]))
        check("latest_asof 取最近一期", pit.latest_asof("600519", "2026-10-28")["pubDate"] == "2026-08-15")
        check("无可见期时返回 None", pit.latest_asof("600519", "2020-01-01") is None)

        # pubDate 缺失的期必须整期丢弃（宁少不多）
        pit.financials = lambda code, y, q: (
            {"statDate": "2025-06-30"} if (y, q) == (2025, 2) else None)
        check("pubDate 缺失的期被丢弃", pit.asof("600519", "2026-10-28", 3) == [])
    finally:
        pit.financials = orig

    print("\n== D. TTM PE ==")
    orig_eps = pit.eps_ttm
    pit.eps_ttm = lambda code, d: 65.142936
    try:
        v = pit.pe_ttm("600519", 1450.0, "2026-10-28")
        check("PE_TTM = price/epsTTM", abs(v - 22.2587) < 1e-3, str(v))
    finally:
        pit.eps_ttm = orig_eps
    orig_eps2 = pit.eps_ttm
    pit.eps_ttm = lambda code, d: None
    try:
        check("无 epsTTM 时返回 None（不伪造）", pit.pe_ttm("600519", 1450.0, "2026-10-28") is None)
    finally:
        pit.eps_ttm = orig_eps2
    pit.eps_ttm = lambda code, d: -1.0
    try:
        check("epsTTM<=0 时返回 None（亏损股不报 PE）", pit.pe_ttm("600519", 1450.0, "2026-10-28") is None)
    finally:
        pit.eps_ttm = orig_eps

    print("\n== E. 数值换算与缺失值 ==")
    check("_num 空串 → None", pit._num("") is None)
    check("_num None → None", pit._num(None) is None)
    check("_num 数字串 → float", pit._num("0.384283") == 0.384283)
    check("_num 非法 → None", pit._num("n/a") is None)
    check("百分比字段集合非空", len(pit.PCT_FIELDS) >= 8)
    check("ROE 在百分比字段里", "roeAvg" in pit.PCT_FIELDS)
    check("六张表字段都在", set(pit.TABLE_FIELDS) ==
          {"profit", "balance", "cashflow", "dupont", "growth", "operation"})

    print("\n== F. 失败语义与可用性 ==")
    check("available() 返回 bool", isinstance(pit.available(), bool))
    check("不支持的指数抛错", raises(pit.index_members, "sz50", "2026-10-28"))
    check("有 PitError 类型", issubclass(pit.PitError, RuntimeError))

    print("\n== G. 历史退市 / PIT 在市口径 ==")
    import pandas as pd

    fake = pd.DataFrame([
        {"代码": "000001", "名称": "平安银行", "上市日": "1991-04-03", "退市日": "",
         "类型": "股票", "状态": "上市"},
        {"代码": "000004", "名称": "国华退", "上市日": "1991-01-14", "退市日": "2026-07-14",
         "类型": "股票", "状态": "退市"},
        {"代码": "600000", "名称": "浦发银行", "上市日": "1999-11-10", "退市日": "",
         "类型": "股票", "状态": "上市"},
        {"代码": "000300", "名称": "沪深300", "上市日": "2005-04-08", "退市日": "",
         "类型": "指数", "状态": "上市"},
    ])
    _old_basic = pit.ds.fetch_stock_basic
    pit.ds.fetch_stock_basic = lambda force=False: fake.copy()
    try:
        before = pit.listed_asof("2026-07-13")
        check("退市前一个交易日仍在市", "000004" in before, str(before))
        after = pit.listed_asof("2026-07-15")
        check("退市日之后剔除", "000004" not in after, str(after))
        check("未上市剔除", "600000" not in pit.listed_asof("1999-01-01"),
              str(pit.listed_asof("1999-01-01")))
        check("指数不进股票清单", "000300" not in after, str(after))
        dl = pit.delisted()
        check("退市名单只留股票且带退市日",
              list(dl["代码"]) == ["000004"] and str(dl.iloc[0]["退市日"]) == "2026-07-14",
              dl.to_dict("records"))
    finally:
        pit.ds.fetch_stock_basic = _old_basic

    print(f"\n断言：{N['ok'] + N['fail']} 项｜通过 {N['ok']}｜失败 {N['fail']}")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
