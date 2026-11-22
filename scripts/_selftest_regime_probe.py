# -*- coding: utf-8 -*-
"""regime_probe.py 的离线自检（不读缓存、不联网）。

为什么要有它：牛熊判定是**静默**的——判据写反、阈值读错、缺数据时把「数据不足」
当成「中性」或「熊市未确认」，都不会报错，只会让仓位判断悄悄偏一个方向。锁死：
  * dev_ma / dd_from_high / up_from_low 的数学（含数据不足 → None）；
  * breadth_ratio 空集 → None；
  * 熊市：2/3 命中才算；缺一条时按可用条数判；可用 <2 条 → 「数据不足」；
  * 牛市：距低点涨幅 / MA200 偏离 / 宽度三判据，同样 2 条才算；
  * 四态：熊市确认 / 牛市确认 / 中性 / 信号冲突（两边都够 2 条）；
  * 阈值全部来自 config.REGIME_PROBE（不写死在脚本里）。

跑法：python scripts/_selftest_regime_probe.py（不联网）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import regime_probe  # noqa: E402

P = config.REGIME_PROBE
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


def t_math():
    print("[1] 数学口径")
    flat = [100.0] * 210
    check("均线之上 dev=0", abs(regime_probe.dev_ma(flat) - 0.0) < 1e-9, str(regime_probe.dev_ma(flat)))
    up = [100.0] * 200 + [110.0]
    d = regime_probe.dev_ma(up)
    check("站上均线 dev≈+10%", d is not None and 0.09 < d < 0.11, str(d))
    check("数据不足 → None", regime_probe.dev_ma([1.0, 2.0, 3.0]) is None)
    check("含 NaN 也能算", regime_probe.dev_ma([100.0] * 200 + [None, 100.0]) is not None)
    dd = regime_probe.dd_from_high([100.0, 120.0, 90.0], window=3)
    check("回撤 = 90/120-1", abs(dd - (90.0 / 120.0 - 1)) < 1e-9, str(dd))
    check("单点无回撤 → None", regime_probe.dd_from_high([100.0], window=3) is None)
    up2 = regime_probe.up_from_low([100.0, 80.0, 120.0], window=3)
    check("反弹 = 120/80-1", abs(up2 - (120.0 / 80.0 - 1)) < 1e-9, str(up2))
    check("单点无反弹 → None", regime_probe.up_from_low([100.0], window=3) is None)
    check("空集宽度 → None", regime_probe.breadth_ratio([]) is None)
    check("宽度 1/4 = 25%", regime_probe.breadth_ratio([True, False, False, False]) == 0.25)


def t_bear():
    print("[2] 熊市判据")
    v = regime_probe.verdict(-0.15, -0.25, 0.20)
    check("三条命中 → 熊市确认", v["bear"] is True and v["hits"] == 3, str(v))
    v = regime_probe.verdict(-0.05, -0.10, 0.20)
    check("一条命中 → 未确认", v["bear"] is False and v["hits"] == 1, str(v))
    v = regime_probe.verdict(-0.05, None, 0.10)
    check("缺 dd 时按可用两条判（1 命中→未确认）",
          v["bear"] is False and v["usable"] == 2 and v["hits"] == 1, str(v))
    v = regime_probe.verdict(None, None, 0.10)
    check("可用 <2 条 → 数据不足", v["bear"] is None and v["verdict"] == "数据不足", str(v))
    check("阈值来自 config", P["dd_bear"] == -0.20 and P["min_hits"] == 2, str(P["min_hits"]))
    v = regime_probe.verdict(P["dev_bear"], P["dd_bear"], P["breadth_bear"] - 0.01)
    check("边界值按 ≤ / < 判定", v["hits"] == 3, str(v["hits"]))


def t_bull():
    print("[3] 牛市判据")
    r = regime_probe.side_verdict(0.15, -0.25, 0.70, "bull", up=0.30)
    check("三条命中 → 牛市确认", r["confirmed"] is True and r["hits"] == 3, str(r))
    r = regime_probe.side_verdict(0.05, -0.30, 0.70, "bull", up=0.10)
    check("只命中宽度 → 未确认", r["confirmed"] is False and r["hits"] == 1, str(r))
    r = regime_probe.side_verdict(0.15, -0.30, 0.40, "bull", up=None)
    check("缺低点涨幅时按可用两条判（dev 命中→1 条→未确认）",
          r["usable"] == 2 and r["hits"] == 1 and r["confirmed"] is False, str(r))
    check("牛市阈值来自 config",
          P["bull_low_rebound"] == 0.25 and P["bull_dev"] == 0.10 and P["bull_breadth"] == 0.60, str(P))


def t_state():
    print("[4] 四态结论")
    a = regime_probe.assess(-0.15, -0.25, 0.20, up=-0.10)
    check("熊市三判据 → 熊市确认", a["state"] == "熊市确认", a["state"])
    a = regime_probe.assess(0.15, -0.02, 0.70, up=0.40)
    check("牛市三判据 → 牛市确认", a["state"] == "牛市确认", a["state"])
    a = regime_probe.assess(0.0, -0.05, 0.45, up=0.15)
    check("两边都不足 2 条 → 中性", a["state"] == "中性", a["state"])
    a = regime_probe.assess(0.12, -0.25, 0.20, up=0.30)
    check("熊 2 条 + 牛 2 条 → 信号冲突", a["state"] == "信号冲突", a["state"])
    a = regime_probe.assess(None, None, 0.10)
    check("判据不足 → 数据不足", a["state"] == "数据不足", a["state"])
    a = regime_probe.assess(-0.15, -0.25, 0.20, up=-0.10)
    check("每种结论都有解释文本", isinstance(a["explain"], str) and len(a["explain"]) > 10, a["explain"])


def t_stance():
    """档位立场：幅度（深熊一档）+ 滞回（不来回切）。"""
    P = regime_probe.P
    s = regime_probe.stance_of("熊市确认", -0.15, -0.22, 0.18)
    check("熊市确认进入 → bear/熊", s["stance"] == "bear" and s["severity"] == "熊", str(s))
    s = regime_probe.stance_of("熊市确认", -0.15, -0.40, 0.15)
    check("回撤 ≤-35% 且宽度<20% → 深熊", s["stance"] == "bear" and s["severity"] == "深熊", str(s))
    s = regime_probe.stance_of("牛市确认", 0.12, -0.05, 0.65, prev={"stance": "normal"})
    check("牛市确认进入 → bull/牛", s["stance"] == "bull" and s["severity"] == "牛", str(s))
    s = regime_probe.stance_of("中性", -0.05, -0.15, 0.30, prev={"stance": "bear"})
    check("滞回：熊市未退出（回撤 -15% 未回到 -10% 内）", s["stance"] == "bear", str(s))
    s = regime_probe.stance_of("中性", -0.05, -0.05, 0.50, prev={"stance": "bear"})
    check("滞回：回撤 -5% 且宽度 50% → 退出熊市", s["stance"] == "normal", str(s))
    s = regime_probe.stance_of("中性", 0.02, -0.20, 0.55, prev={"stance": "bull"})
    check("滞回：牛市未退出（偏离 +2% 未跌破 +4%）", s["stance"] == "bull", str(s))
    s = regime_probe.stance_of("中性", 0.01, -0.20, 0.40, prev={"stance": "bull"})
    check("滞回：偏离 +1% 且宽度 40% → 退出牛市", s["stance"] == "normal", str(s))
    s = regime_probe.stance_of("中性", 0.0, -0.05, 0.55, prev=None)
    check("无历史 + 未确认 → normal/普通", s["stance"] == "normal" and s["severity"] == "普通", str(s))
    check("深熊/退出阈值来自 config",
          P["deep_bear_gross"] < P["defend_gross"] and P["exit_bear_dd"] > P["dd_bear"],
          str(P["deep_bear_gross"]))


def main():
    t_math()
    t_bear()
    t_bull()
    t_state()
    t_stance()
    print(f"\n断言：{PASS + FAIL} 项｜通过 {PASS}｜失败 {FAIL}")
    if FAIL:
        print("FAILED")
        return 1
    print("OK 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
