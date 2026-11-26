# -*- coding: utf-8 -*-
"""screen.py 风险名单剔除 / 停牌门槛的离线自检。

为什么要有它：这两处是**静默**的——名单取不到、简称口径变了、停牌判据写反，
都不会报错，只会让 ST 票或停牌票悄悄进候选池（回测里表现为「买进入了退市股」）。
锁死三件事：
  * 简称含 ST/*ST/「退」必须被识别（含带空格的「英 力 特」式简称、`ST 中安` 这种写法）；
  * 名单为空时返回空集合，不能把整池误杀；
  * 停牌判据是「最近一根日线 == 数据日」，旧日期与缺失都必须判为停牌；
    数据日由主流程按「全池最新一根日线」传入（不是墙钟日期，凌晨/周末跑管线时墙钟会
    指向还没有日线的今天，把全池误判停牌并静默 0 候选）；
  * `apply_gates` 里确实挂着 `L1_no_suspend` 这道门槛。

跑法：python scripts/_selftest_screen.py（不联网）
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import screen  # noqa: E402

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


def t_names():
    print("[1] 风险名单剔除")
    names = pd.DataFrame({
        "code": ["000001", "000002", "000003", "000004", "000005", "600000"],
        "name": ["平安银行", "*ST 田野", "ST 中安", "国华退", "英 力 特", "浦发银行"],
    })
    hit = screen.risk_warn_codes(names)
    check("ST/*ST 命中", {"000002", "000003"} <= hit, str(sorted(hit)))
    check("退市整理命中", "000004" in hit, str(sorted(hit)))
    check("正常股不命中", not ({"000001", "000005", "600000"} & hit), str(sorted(hit)))
    check("空名单返回空集", screen.risk_warn_codes(pd.DataFrame(columns=["code", "name"])) == set())
    check("None 名单不炸", screen.risk_warn_codes(None) == set())
    check("代码补零", all(len(c) == 6 for c in hit), str(sorted(hit)))


def t_suspend():
    print("[2] 停牌门槛")
    old = screen.ds.data_date
    screen.ds.data_date = lambda: "2026-11-19"
    try:
        check("今日有日线 → 非停牌", not screen.is_suspended({"last_bar": "2026-11-19"}))
        check("昨日日线 → 停牌", screen.is_suspended({"last_bar": "2026-11-18"}))
        check("缺 last_bar → 停牌", screen.is_suspended({}))

        row = {"name": "平安银行", "industry": "银行", "last_bar": "2026-11-19"}
        gates = screen.apply_gates(row, {})["gates"]
        check("apply_gates 含 L1_no_suspend 且通过", gates.get("L1_no_suspend") is True, str(gates.get("L1_no_suspend")))

        row2 = {"name": "平安银行", "industry": "银行", "last_bar": "2026-11-18"}
        gates2 = screen.apply_gates(row2, {})["gates"]
        check("停牌股不通过 L1_no_suspend", gates2.get("L1_no_suspend") is False, str(gates2.get("L1_no_suspend")))

        row3 = {"name": "*ST 田野", "industry": "银行", "last_bar": "2026-11-19"}
        gates3 = screen.apply_gates(row3, {})["gates"]
        check("ST 不通过 L1_not_st", gates3.get("L1_not_st") is False)

        # 数据日由调用方给（= 全池最新一根日线）：凌晨/周末墙钟指向前一天还没有的日期时，
        # 不许把全池误判停牌（2026-11-26 凌晨首次运行静默 0 候选的正是这个坑）。
        check("data_day 参数优先于墙钟", not screen.is_suspended({"last_bar": "2026-11-19"}, "2026-11-19"))
        check("data_day 比日线新 → 停牌", screen.is_suspended({"last_bar": "2026-11-18"}, "2026-11-19"))
        check("market_data_day 取全池最新",
              screen.market_data_day([{"last_bar": "2026-11-18"},
                                      {"last_bar": "2026-11-19"}, {}]) == "2026-11-19")
        check("market_data_day 空池退回 data_date", screen.market_data_day([]) == "2026-11-19")
        row4 = {"name": "平安银行", "industry": "银行", "last_bar": "2026-11-19"}
        gates4 = screen.apply_gates(row4, {}, "2026-11-19")["gates"]
        check("全池日线 == data_day 时不误判停牌", gates4.get("L1_no_suspend") is True)
    finally:
        screen.ds.data_date = old


def main():
    t_names()
    t_suspend()
    print()
    print(f"断言：{PASS + FAIL} 项｜通过 {PASS}｜失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
