# -*- coding: utf-8 -*-
"""离线自检：小资金执行层（capital.py）算术 + 档位/手数/费用/现金层。

不联网、不读输出目录，几毫秒跑完。用于改完 config.py / capital.py 后立刻验证。
用法：python _selftest.py      # 全通过退出码 0，否则 1
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capital as cap  # noqa: E402
import config  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


def approx(a, b, tol=0.01) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def main():
    print("=== 1. 本金区间校验（本部署 5 万；兜底区间）===")
    for c in (10000, 30000, 100000, 10000.0, 99999.99):
        check(f"{c:,.2f} 元在区间内", approx(cap.validate_capital(c), c, 0.001))
    for bad in (9999.99, 100000.01, 5000, 200000, 0, -1000):
        try:
            cap.validate_capital(bad)
            check(f"{bad:,.2f} 元应被拒绝", False)
        except cap.CapitalRangeError as e:
            check(f"{bad:,.2f} 元被拒绝", "超出本金校验区间" in str(e))
    try:
        cap.validate_capital(float("nan"))
        check("NaN 应被拒绝", False)
    except cap.CapitalRangeError:
        check("NaN 被拒绝", True)

    print("\n=== 2. 整手算术 ===")
    check("1 手成本 = 价格×100", approx(cap.lot_cost(25.57), 2557.0))
    check("1000 元买 25.57 元的票 = 0 手", cap.lots_for_amount(1000, 25.57) == 0)
    check("1000 元买 9.5 元的票 = 1 手", cap.lots_for_amount(1000, 9.5) == 1)
    check("3000 元买 9.5 元的票 = 3 手", cap.lots_for_amount(3000, 9.5) == 3)
    check("买 1 手不超 10% 需本金 = 2557/0.1",
          approx(cap.min_capital_single(25.57), 25570.0))
    check("三批最小一批 = 2557/(0.1*0.25)",
          approx(cap.min_capital_3tranche(25.57), 102280.0))

    print("\n=== 3. 三批拆分（最大余数法） ===")
    for total, expect in [(2, [1, 1, 0]), (3, [1, 1, 1]), (4, [2, 1, 1]),
                          (7, [3, 2, 2]), (8, [3, 3, 2]), (10, [4, 4, 2])]:
        got = cap.split_lots(total)
        check(f"{total} 手 → {expect}", got == expect, f"实际 {got}")
    check("0 手 → 全 0", cap.split_lots(0) == [0, 0, 0])
    check("拆分后手数守恒", all(sum(cap.split_lots(n)) == n for n in range(0, 100)))

    print("\n=== 4. 交易费用 ===")
    _rate = float(config.CAPITAL["commission_rate"])
    _minf = float(config.CAPITAL["min_commission"] or 0.0)

    def _wb(amt):
        return max(amt * _rate, _minf)

    f_etf = cap.trade_fee(900, is_etf=True)
    check(f"ETF 900 元：佣金 = {_wb(900):.3f} 元", approx(f_etf["commission"], _wb(900), 0.01))
    check("ETF 无过户费/印花税", approx(f_etf["transfer"], 0.0) and approx(f_etf["stamp"], 0.0))
    if _minf > 0:
        check(f"最低收费保护生效（900 元被抬到 {_minf:.0f} 元）",
              approx(f_etf["commission"], _minf))
    else:
        check("免五：900 元按费率收，不抬到 5 元", approx(f_etf["commission"], 900 * _rate, 0.01))
    f_stock_buy = cap.trade_fee(2557, is_etf=False, side="buy")
    check(f"股票买入 2557 元：佣金 = {_wb(2557):.3f} 元",
          approx(f_stock_buy["commission"], _wb(2557), 0.01))
    check("股票买入有过户费", f_stock_buy["transfer"] > 0)
    check("股票买入无印花税", approx(f_stock_buy["stamp"], 0.0))
    f_stock_sell = cap.trade_fee(2557, is_etf=False, side="sell")
    check("股票卖出有印花税 0.05%", approx(f_stock_sell["stamp"], 2557 * 0.0005, 0.01))
    big = cap.trade_fee(50000, is_etf=False)
    check(f"大额：佣金 = {_wb(50000):.2f} 元", approx(big["commission"], _wb(50000), 0.01))
    _r900, _r9000 = cap.trade_fee(900)["ratio"], cap.trade_fee(9000)["ratio"]
    if _minf > 0:
        check("费用占比：小单占比 > 大单占比（最低收费拖高小单）", _r900 > _r9000)
    else:
        check("免五：成本率与金额无关，小单/大单占比相同（固定成本消失）",
              abs(_r900 - _r9000) < 1e-6)

    print("\n=== 5. 现金层（挂单资金→朝朝宝；观望资金→半流动性货基池；逆回购已移出） ===")
    # 观望资金走「全市场货基 90 天口径前 3 支 × 每支 ≤1 万快赎」，不再绑招行朝朝盈2号池。
    # 逆回购从现金层移出，改为投资层脉冲择时（repo_pulse.py）。旧口径 == container="repo7" 回滚。
    c1 = cap.cash_layer(7423.5)
    _nr = cap.cash_net_rates()
    check("默认容器 = 现金层分工策略（货基池）", c1["container"] == "policy_pool",
          f"container={c1['container']}")
    check("现金层不再默认买逆回购（repo_lots = 0）", c1["repo_lots"] == 0)
    check("半流动性金额 + 朝朝宝金额 = 现金合计",
          approx(c1["sleeve_amount"] + c1["money_fund_amount"], c1["cash_total"]),
          f"{c1['sleeve_amount']}+{c1['money_fund_amount']} vs {c1['cash_total']}")
    check("每支货基 ≤1 万快赎额度",
          all(h["amount"] <= 10000.0 + 1e-9 for h in c1["sleeve_holdings"]),
          f"{[h['amount'] for h in c1['sleeve_holdings']]}")
    check("半流动性加权利率落在 0.1%–5% 合理区间",
          (0.001 < c1["sleeve_rate"] < 0.05) if c1["sleeve_holdings"] else True,
          f"rate={c1['sleeve_rate']}")
    c4 = cap.cash_layer(7423.5, reserved=2000.0)
    check("挂单资金 2,000 元留朝朝宝、不进货基池",
          approx(c4["reserved_amount"], 2000.0)
          and approx(c4["sleeve_amount"] + c4["money_fund_amount"], c4["cash_total"]),
          f"res={c4['reserved_amount']} sleeve={c4['sleeve_amount']}")
    c4b = cap.cash_layer(10000.0, reserved=3000.0, ammo=6000.0)
    check("挂单冻结与建仓弹药取较大者留 T+0（不重复扣）",
          approx(c4b["t0_keep"], 6000.0) and approx(c4b["idle_amount"], 4000.0),
          f"keep={c4b['t0_keep']} idle={c4b['idle_amount']}")
    c4c = cap.cash_layer(10000.0, reserved=6000.0, ammo=2000.0)
    check("弹药小于挂单冻结时按挂单留 T+0",
          approx(c4c["t0_keep"], 6000.0) and approx(c4c["idle_amount"], 4000.0),
          f"keep={c4c['t0_keep']} idle={c4c['idle_amount']}")
    # ---- 回滚口径：container="repo7" 仍是「观望资金按金额分档买 7/14/28 天期逆回购」----
    r7 = cap.cash_layer(7423.5, container="repo7")
    check("回滚 repo7：7,423.5 元 → 7 手 + 零头 423.5 元",
          r7["repo_lots"] == 7 and approx(r7["repo_amount"], 7000.0)
          and approx(r7["money_fund_amount"], 423.5),
          f"{r7['repo_lots']}/{r7['repo_amount']}/{r7['money_fund_amount']}")
    check("回滚 repo7：<8,000 元 → 7 天期", r7["repo_term_days"] == 7, f"term={r7['repo_term_days']}")
    check("回滚 repo7：9,000 元 → 14 天期",
          cap.cash_layer(9000, container="repo7")["repo_term_days"] == 14)
    check("回滚 repo7：25,000 元 → 28 天期",
          cap.cash_layer(25000, container="repo7")["repo_term_days"] == 28)
    check("逆回购净年化 = 7 天期毛 − 0.26%×券商折扣",
          approx(r7["repo_net_rate"],
                 round(_nr["repo7_gross"]
                       - _nr["repo7_fee"] * _nr["repo_commission_discount"], 6)),
          f"expected {_nr['repo7_gross']}-{_nr['repo7_fee']}, got {r7['repo_net_rate']}")
    c1r = cap.cash_layer(7423.5, container="repo")
    check("显式 repo（回滚口径）→ 1 天期 7 手",
          c1r["repo_lots"] == 7 and c1r["repo_term_days"] == 1)
    check("显式 repo 净年化 = 1 天期毛 − 年费 × 券商折扣",
          approx(c1r["repo_net_rate"], round(_nr["repo_gross"] - _nr["repo_fee"], 6)))
    try:
        cap.cash_layer(1000, container="x")
        _bad_container = False
    except ValueError:
        _bad_container = True
    check("未知容器 fail-closed", _bad_container)
    c5 = cap.cash_layer(900)
    check("现金 900 元：货基池无整手限制，全额进池（不再留 0 手逆回购）",
          c5["repo_lots"] == 0
          and approx(c5["sleeve_amount"] + c5["money_fund_amount"], 900.0),
          f"sleeve={c5['sleeve_amount']} mf={c5['money_fund_amount']}")
    c6 = cap.cash_layer(0)
    check("0 现金不炸", c6["repo_lots"] == 0 and approx(c6["total_net_year"], 0.0))
    check("现金层年化落在 0.1%–5% 合理区间",
          c1["net_yield"] is not None and 0.001 < c1["net_yield"] < 0.05,
          f"net_yield={c1['net_yield']}")

    print("\n=== 6. 档位（不得突破市场状态上限） ===")
    t_b = cap.tier_plan(10000, "B", 0.35)
    check("B 档 46% 被 35% 上限压回", approx(t_b["equity_ratio"], 0.35) and t_b["capped_by_regime"])
    check("被压回后权益金额 = 3500", approx(t_b["equity_amount"], 3500.0))
    t_a = cap.tier_plan(10000, "A", 0.35)
    check("A 档 26% 不受上限影响", approx(t_a["equity_ratio"], 0.26) and not t_a["capped_by_regime"])
    t_c = cap.tier_plan(10000, "C", 0.80)
    check("C 档 64% < 80% 上限 → 64%", approx(t_c["equity_ratio"], 0.64))
    t_ov = cap.tier_plan(10000, "B", 0.35, override=True)
    check("override 时允许 46%", approx(t_ov["equity_ratio"], 0.46) and t_ov["override"])
    check("档位越低权益越少",
          cap.tier_plan(10000, "A", 0.9)["equity_amount"]
          < cap.tier_plan(10000, "B", 0.9)["equity_amount"]
          < cap.tier_plan(10000, "C", 0.9)["equity_amount"])
    try:
        cap.tier_plan(10000, "Z", 0.35)
        check("未知档位应报错", False)
    except ValueError:
        check("未知档位报错", True)

    print("\n=== 7. 逐标的可行性表 ===")
    rows = cap.lot_table([
        {"code": "000999", "name": "华润三九", "price": 25.57, "weight": 0.10},
        {"code": "601058", "name": "赛轮轮胎", "price": 12.41, "weight": 0.10},
        {"code": "sh515390", "name": "沪深300ETF", "price": 1.235, "weight": 0.09},
    ], 10000)
    check("25.57 元票在小本金下买不了 1 手", not rows[0]["feasible"] and rows[0]["lots"] == 0)
    check("12.41 元票也买不了（1 手 1241 > 1000 预算）",
          not rows[1]["feasible"] and rows[1]["lots"] == 0)
    check("1.235 元 ETF 可买", rows[2]["feasible"] and rows[2]["lots"] >= 1)
    check("不可买行给出所需本金", rows[1]["min_capital_single"] > 10000)
    ladder = cap.lot_ladder(rows[2])
    check("三批手数之和 = 总手数", sum(x["lots"] for x in ladder) == rows[2]["lots"])
    check("三批金额之和 ≈ 实际金额",
          approx(sum(x["amount"] for x in ladder), rows[2]["actual_amount"], 0.5))

    print("\n=== 8. 费用汇总 ===")
    orders = [
        {"code": "sh515390", "name": "沪深300ETF", "amount": 864.5, "is_etf": True},
        {"code": "000999", "name": "华润三九", "amount": 2557.0, "is_etf": False},
    ]
    fs = cap.fee_summary(orders, 10000)
    _want_buy = _wb(864.5) + _wb(2557.0) + 2557 * config.CAPITAL["transfer_fee_rate"]
    check(f"买入费用 = 两笔佣金 + 过户费（{_want_buy:.2f} 元）",
          approx(fs["buy_fee"], _want_buy, 0.05))
    check("来回费用含卖出印花税", fs["round_trip_fee"] > fs["buy_fee"])
    if _minf > 0:
        check("高佣金订单被标出（小单被最低收费拖高）",
              len(fs["high_fee_orders"]) == 1, str(fs["high_fee_orders"]))
    else:
        check("免五下小单成本极低，无高佣金订单", fs["high_fee_orders"] == [],
              str(fs["high_fee_orders"]))
        # 反向验证警报机制本身没坏：临时把最低收费调回 5 元，应立刻亮 1 单
        _bak = config.CAPITAL["min_commission"]
        config.CAPITAL["min_commission"] = 5.0
        try:
            _hi = cap.fee_summary(orders, 10000)["high_fee_orders"]
            check("把最低收费调回 5 元后，警报机制仍会亮", len(_hi) == 1, str(_hi))
        finally:
            config.CAPITAL["min_commission"] = _bak
    check("区间说明含本金", "本金" in cap.describe(10000)["note"])

    print()
    if FAILED:
        print(f"✗ 失败 {len(FAILED)} 项：" + "；".join(FAILED))
        raise SystemExit(1)
    print(f"✓ 全部通过（{PASSED} 项断言）")


if __name__ == "__main__":
    main()
