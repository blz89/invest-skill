# -*- coding: utf-8 -*-
"""风险等级（R1–R5）自检：标准产品分类 + 准入门槛 + 组合评审。

口径（2026-10-26 用户纠正）：R1–R5 是金融产品风险等级的通用划分，按产品类型/
交易场所/板块定级；投资者 C1–C5 与之匹配，不能买高于自身承受能力的产品。
**波动率不作为等级依据**——主板股票波动再大也是 R3，创业板股票波动再小也是 R4。

运行：& $py _selftest_risk.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

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


def main() -> int:
    print("== 一、等级工具 ==")
    check("level_num(R2)==2", risk.level_num("R2") == 2)
    check("level_num(R5)==5", risk.level_num("R5") == 5)
    check("level_num('X') is None", risk.level_num("X") is None)
    check("included(R2) and included(R3)", risk.included("R2") and risk.included("R3"))
    check("not included(R1/R4/R5)", not any(risk.included(x) for x in ("R1", "R4", "R5")))
    check("num_level(2.4)=='R2'", risk.num_level(2.4) == "R2")
    check("level_label(R3) 含 中风险", "中风险" in risk.level_label("R3"))
    check("investor_line 含 C3", "C3" in risk.investor_line())

    print("\n== 二、准入门槛（资金/经验） ==")
    check("科创板需 50 万", risk.qualification("科创板")["min_capital"] == 500000)
    check("创业板需 10 万", risk.qualification("创业板")["min_capital"] == 100000)
    check("5 万不满足科创板", risk.access_ok("科创板", 10000)[0] is False)
    check("10 万不满足科创板", risk.access_ok("科创板", 100000)[0] is False)
    check("50 万满足科创板", risk.access_ok("科创板", 500000)[0] is True)
    check("5 万不满足创业板", risk.access_ok("创业板", 10000)[0] is False)
    check("10 万满足创业板", risk.access_ok("创业板", 100000)[0] is True)
    check("主板无门槛", risk.access_ok("主板股票", 10000)[0] is True)

    print("\n== 三、个股按板块定级（不看波动率） ==")
    check("000999 主板 R3", risk.board_of("000999")["level"] == "R3")
    check("600406 主板 R3", risk.board_of("600406")["level"] == "R3")
    check("300750 创业板 R4", risk.board_of("300750")["level"] == "R4")
    check("688981 科创板 R4", risk.board_of("688981")["level"] == "R4")
    check("830799 北交所 R4", risk.board_of("830799")["level"] == "R4")
    check("113050 可转债 R3", risk.board_of("113050")["level"] == "R3")
    check("sh600000 带前缀也能认", risk.board_of("sh600000")["board"] == "主板")
    check("空代码 R5 fail-closed", risk.board_of("")["level"] == "R5")
    check("主板+高波动仍是 R3",
          risk.stock_level("600000", "浦发银行", {"vol_3y": 0.60, "max_dd_3y": -0.55})["level"] == "R3")
    check("创业板+低波动仍是 R4",
          risk.stock_level("300059", "东方财富", {"vol_3y": 0.18, "max_dd_3y": -0.10})["level"] == "R4")
    check("ST 股 R5", risk.stock_level("600999", "ST某某", {})["level"] == "R5")
    check("兼容旧签名 dict 调用",
          risk.stock_level({"code": "000999", "name": "华润三九", "vol_3y": 0.22})["level"] == "R3")
    check("波动提示写进 reason 但不改等级",
          "不改变等级" in risk.stock_level("000999", "华润三九", {"vol_3y": 0.22})["reason"])

    print("\n== 四、场内基金按品种定级 ==")
    cases = [
        ("512500", "中证500ETF华夏", "宽基", "R3"),
        ("510050", "上证50ETF华夏", "宽基", "R3"),
        ("159628", "中证1000增强ETF", "策略", "R3"),
        ("512880", "证券ETF", "行业", "R4"),
        ("159915", "创业板ETF", "宽基", "R4"),
        ("588000", "科创50ETF", "宽基", "R4"),
        ("513100", "纳指ETF", "跨境", "R4"),
        ("518880", "黄金ETF", "商品", "R5"),
        ("159985", "豆粕ETF", "商品", "R5"),
        ("511520", "政金债ETF富国", "债券", "R2"),
        ("511010", "国债ETF", "债券", "R2"),
        ("511990", "华宝添益", "货币", "R1"),
        ("某奇怪基金", "认不出的ETF", None, "R5"),
    ]
    for code, name, cls, lv in cases:
        got = risk.etf_level(name, cls)["level"]
        check(f"{name} → {lv}（实得 {got}）", got == lv)

    print("\n== 五、组合评审（权重=占本金） ==")
    good = risk.portfolio_review([
        {"code": "511520", "name": "政金债ETF", "level": "R2", "weight": 0.12},
        {"code": "000999", "name": "华润三九", "level": "R3", "weight": 0.08},
        {"code": "510050", "name": "上证50ETF", "level": "R3", "weight": 0.05},
    ])
    check("正常组合 ok", good["ok"] is True)
    check("加权等级 R2~R3", 2.2 <= good["level"] <= 3.0)
    check("R2 占比对", abs(good["r2_share"] - 0.12 / 0.25) < 1e-6)
    check("评审结果带投资者说明", "C3" in good["investor_line"])

    bad_scope = risk.portfolio_review([
        {"code": "300750", "name": "宁德时代", "level": "R4", "weight": 0.10}])
    check("R4 持仓 → 未通过", bad_scope["ok"] is False and bad_scope["violations"])

    bad_single = risk.portfolio_review([
        {"code": "000999", "name": "华润三九", "level": "R3", "weight": 0.20}])
    check("R3 单只 20% → 违规", bad_single["ok"] is False)

    heavy_bond = risk.portfolio_review([
        {"code": "511520", "name": "政金债ETF", "level": "R2", "weight": 0.12},
        {"code": "511010", "name": "国债ETF", "level": "R2", "weight": 0.10}])
    check("全债 → 只告警不拦", heavy_bond["ok"] is True and heavy_bond["warnings"])

    unknown = risk.portfolio_review([{"code": "x", "name": "x", "level": "", "weight": 0.1}])
    check("无等级 → fail-closed", unknown["ok"] is False)

    empty = risk.portfolio_review([])
    check("空组合 ok 且提示", empty["ok"] is True and empty["warnings"])

    line = risk.summary_line(good)
    check("summary_line 含组合加权", "组合加权" in line)

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
