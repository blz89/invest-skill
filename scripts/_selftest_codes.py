# -*- coding: utf-8 -*-
"""stock.codes 离线自测（2026-11-26 新增）。

为什么单列：`_selftest_fuyao.py` 在 NETWORK 列表（--fast 跳过），而短代码补零
（"82" → 000082.SZ）是 2026-11-26 审计发现的隐藏红——独立成纯离线套件后，
`--fast` 就能钉住这条回归。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402,F401  （先 import 注入 stock 数据层路径）
from stock import codes  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}  {detail}")


def main():
    check("6 位股票代码", codes.parse("600519")["symbol"] == "600519")
    check("短代码补零：82 → 000082", codes.parse("82")["symbol"] == "000082",
          str(codes.parse("82")))
    check("单数字补零：9 → 000009", codes.parse("9")["symbol"] == "000009",
          str(codes.parse("9")))
    check("4 位短代码补零：2013 → 002013", codes.parse("2013")["symbol"] == "002013",
          str(codes.parse("2013")))
    check("短代码市场：82 归深市", codes.parse("82")["market"] == "sz")
    check("thscode：82 → 000082.SZ", codes.to_thscode("82") == "000082.SZ",
          codes.to_thscode("82"))
    check("thscode：sh510300 → 510300.SH", codes.to_thscode("sh510300") == "510300.SH")
    check("sina：600519 → sh600519", codes.to_sina("600519") == "sh600519")
    check("北交所 8 开头", codes.to_thscode("830799").endswith(".BJ"),
          codes.to_thscode("830799"))
    bad = False
    try:
        codes.parse("abc")
    except codes.CodeError:
        bad = True
    check("非法代码抛 CodeError", bad)
    bad5 = False
    try:
        codes.parse("60051")
    except codes.CodeError:
        bad5 = True
    check("5 位数字不按短代码补零（防截断 typo 变成另一个代码）", bad5)
    print()
    if FAIL:
        print(f"XX 失败 {FAIL} 项 / 通过 {PASS} 项")
        return 1
    print(f"OK 全部通过（{PASS} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
