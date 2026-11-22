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

运行：& $py _selftest_review.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import review  # noqa: E402
import risk  # noqa: E402

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
