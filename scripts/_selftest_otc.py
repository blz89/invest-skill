# -*- coding: utf-8 -*-
"""otc.py（R2 场外债券基金层）的离线自检。

为什么要有它：场外池是小本金能不能真正落到 R2 的唯一通道，它的坑都是**静默**的：
  * 份额没拆干净 → 同一只基金 A/C 各占一个坑，R2 预算被同一只基金重复吃掉；
  * ETF 联接基金混进来 → 和场内 ETF 层重复持仓（用户明确要求「别弄重复」）；
  * 货币基金混进来 → R1 已由现金层承担，重复计风险预算；
  * 首年成本把百分数当小数（或反过来）→ 选出来的「最便宜」其实是 100 倍贵的；
  * 缓存回读把 6 位代码变 int（000682 → 682）→ 下游按 code 关联时静默错配。
这些都不报错，只出错数，所以必须锁死。

跑法：python scripts/_selftest_otc.py（不联网；只读 out/ 已有产物做一致性检查）
"""
import atexit
import shutil
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config       # noqa: E402
import datasource as ds  # noqa: E402
import metrics as m      # noqa: E402
import otc          # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def skip(name, why=""):
    global SKIP
    SKIP += 1
    print(f"  - 跳过 {name}  {why}")


OUT = config.OUT_DIR
CSV = os.path.join(OUT, "stage1_otc.csv")
ETF_CSV = os.path.join(OUT, "stage1_wealth.csv")

_CONTRACT_COLS = ["code", "name", "ftype", "nav_date", "unit_nav", "acc_nav",
                  "purchase_status", "min_purchase", "day_limit", "fee", "hist_days",
                  "ret_1y", "ret_3y", "dd_1y", "dd_3y", "vol_3y", "mgmt_fee",
                  "redeem_fee_hold", "cost_1y", "score", "level", "level_reason",
                  "key", "share_class", "hold_period_days",
                  "scale_yi", "fwd_net_rate", "fwd_gap_vs_cash"]


# ---------------------------------------------------------------- A. 配置可追溯
def t_config():
    print("\n[A] 配置可追溯：config.RISK.wealth.otc 必须压过模块内默认值")
    cfg_otc = dict((config.RISK.get("wealth") or {}).get("otc") or {})
    check("config 里确实有 otc 子段（不是靠模块默认值跑）", len(cfg_otc) > 0,
          "config.RISK.wealth.otc 为空")
    bad = []
    for k, v in cfg_otc.items():
        got = otc.CFG_O.get(k)
        if isinstance(v, dict):
            if not isinstance(got, dict) or any(got.get(kk) != vv for kk, vv in v.items()):
                bad.append(k)
        elif got != v:
            bad.append(k)
    check("config 的每个键都生效（同一数据同一结论）", not bad, f"未生效: {bad}")

    # 兜底默认值只负责「config 漏写时」不至于崩，不能被当成事实来源
    check("默认值与 config 值不冲突（除 config 显式覆盖的键外逐项相等）",
          all(otc.CFG_O[k] == v for k, v in otc._OTC_DEFAULTS.items()
              if k not in cfg_otc),
          "默认值与 config 冲突")

    check("min_hist_days ≥ 750（3 年回撤至少要 750 个交易日）",
          int(otc.CFG_O["min_hist_days"]) >= 750, str(otc.CFG_O.get("min_hist_days")))
    check("ftypes 只含债券/固收型（货币基金必须不在里面，否则与现金层重复）",
          not any("货币" in str(x) for x in otc.CFG_O["ftypes"]),
          str(otc.CFG_O.get("ftypes")))
    check("name_exclude 含「联接」（ETF 联接基金 = 场内 ETF 的场外镜像，必须排掉）",
          "联接" in [str(x) for x in otc.CFG_O["name_exclude"]],
          str(otc.CFG_O.get("name_exclude")))
    check("申购状态允许「限大额」（本金远低于限额，不该当排除项）",
          "限大额" in [str(x) for x in otc.CFG_O["purchase_status_ok"]],
          str(otc.CFG_O.get("purchase_status_ok")))


# ---------------------------------------------------------------- B. 份额拆分类
def t_share():
    print("\n[B] 份额类别拆分：同一只基金的 A/C 必须算出同一个 key（L6 去重的前提）")
    cases = [
        ("华夏短债债券C", ("华夏短债债券", "C")),
        ("华夏短债债券A", ("华夏短债债券", "A")),
        ("华夏短债债券", ("华夏短债债券", "")),
        ("华夏短债债券-C", ("华夏短债债券", "C")),
        ("华夏短债债券C类", ("华夏短债债券", "C")),
        ("华夏短债债券Ｃ", ("华夏短债债券", "C")),          # 全角
        ("易方达中债7-10年国开行债券C", ("易方达中债710年国开行债券", "C")),
        ("某基金30天持有A", ("某基金30天持有", "A")),
        ("某基金90天C", ("某基金90天", "C")),
    ]
    for name, want in cases:
        got = otc.share_split(name)
        check(f"share_split({name!r})", got == want, f"want={want} got={got}")

    # 关键断言：A/C 两个份额拆出来的 key 必须相同，否则 L6 去重失效
    a = otc.share_split("某中短债债券A")
    c = otc.share_split("某中短债债券C")
    check("A/C 两个份额 key 相同（去重的前提）", a[0] == c[0] and a[1] != c[1],
          f"A={a} C={c}")

    # 期限后缀里的字母/数字不能被误当份额：'30天' 的 '天' 前是数字
    got = otc.share_split("某基金5G")
    check("份额字母前是数字时不拆（避免误切期限后缀）", got == ("某基金5G", ""), str(got))
    got = otc.share_split("某基金")
    check("无份额后缀时类别为空串", got == ("某基金", ""), str(got))
    got = otc.share_split("C")
    check("单字符名称不崩", got == ("C", ""), str(got))


# ---------------------------------------------------------------- C. 排除词
def t_exclude():
    print("\n[C] 排除词：与场内 ETF 层/现金层重复的品种必须被挡在池外")
    must_hit = ["某某中债1-3年国开行ETF联接C", "某某可转债债券A", "某某转债增强",
                "某某双债增强债券", "某某增强收益债券", "某某混合一级债券",
                "某某混合二级债券"]
    for nm in must_hit:
        w = otc.excluded_word(nm)
        check(f"排除 {nm!r}", w is not None, "未被排除")

    must_pass = ["华夏短债债券C", "易方达信用债债券A", "南方中债7-10年国开行债券C"]
    for nm in must_pass:
        w = otc.excluded_word(nm)
        check(f"保留 {nm!r}", w is None, f"误排除，命中词={w!r}")

    # 「可转债」应先命中长词，而不是先命中「转债」，否则漏斗统计说不清原因
    check("长词优先命中（可转债 → 报 '可转债' 而不是 '转债'）",
          otc.excluded_word("某某可转债债券A") == "可转债",
          str(otc.excluded_word("某某可转债债券A")))


# ---------------------------------------------------------------- D. 首年成本
def t_cost():
    print("\n[D] 首年总成本：百分数 → 小数必须只转一次（错一次就是 100 倍）")
    check("全 0 → 0.0", otc.cost_1y(0.0, 0.0, 0.0) == 0.0, str(otc.cost_1y(0, 0, 0)))
    # 申购 0.15%（1 折后）+ 运作 0.40%（管理+托管+销售服务合计）+ 赎回 0 = 0.55%
    check("0.15% + 0.40% + 0% = 0.0055",
          abs(otc.cost_1y(0.15, 0.40, 0.0) - 0.0055) < 1e-9, str(otc.cost_1y(0.15, 0.4, 0)))
    check("0.0% + 0.20% + 0.50% = 0.0070",
          abs(otc.cost_1y(0.0, 0.20, 0.50) - 0.0070) < 1e-9, str(otc.cost_1y(0, 0.2, 0.5)))
    check("None 按 0 计，不抛错", otc.cost_1y(None, None, None) == 0.0,
          str(otc.cost_1y(None, None, None)))
    # 数量级护栏：真·债基首年成本不可能 ≥2%（2% 意味着百分数被当小数用了 100 倍）
    check("典型债基首年成本落在 (0, 2%) 区间", 0.0 < otc.cost_1y(0.15, 0.4, 0.0) < 0.02,
          str(otc.cost_1y(0.15, 0.4, 0.0)))


# ---------------------------------------------------------------- E2. 赎回费档位
def t_redeem():
    print("\n[E2] 赎回费按「持有 90 天」取档，不能取到「刚满 7 天」那档")
    # 真档位：2026-10-27 取自 datasource.fetch_fund_fee(code, "赎回费率")，
    # 这两只都是当日在池里通过 L5 的短债 C 类。核心断言是「持有数月时赎回费为 0」。
    check(f"持有期常量 HOLD_DAYS = 90（{otc.HOLD_DAYS}）", otc.HOLD_DAYS == 90,
          str(otc.HOLD_DAYS))

    p33 = ["小于等于6天", "大于等于7天，小于等于29天", "大于等于30天"]
    f33 = [1.50, 0.75, 0.00]
    check("000033 实档：持有 90 天 → 0.00%（不是 0.75%）",
          otc.redeem_fee_at_hold(p33, f33, 90) == 0.00,
          str(otc.redeem_fee_at_hold(p33, f33, 90)))
    check("000033 实档：持有 7 天 → 0.75%",
          otc.redeem_fee_at_hold(p33, f33, 7) == 0.75,
          str(otc.redeem_fee_at_hold(p33, f33, 7)))

    p16 = ["小于7天", "大于等于7天，小于30天", "大于等于30天"]
    f16 = [1.5, 0.1, 0.0]
    check("000016 实档：持有 90 天 → 0.00%（不是 0.10%）",
          otc.redeem_fee_at_hold(p16, f16, 90) == 0.0,
          str(otc.redeem_fee_at_hold(p16, f16, 90)))

    # 只有惩罚档时，持有 90 天应为 0——惩罚档不能外推到 90 天，否则 cost_1y 虚高 1.5%
    check("只有「小于7天 1.50%」一档：持有 90 天 → 0.0",
          otc.redeem_fee_at_hold(["小于7天"], [1.5], 90) == 0.0,
          str(otc.redeem_fee_at_hold(["小于7天"], [1.5], 90)))
    check("空表 → None（不是 0；费率取不到要交给上层 fail-closed）",
          otc.redeem_fee_at_hold([], [], 90) is None,
          str(otc.redeem_fee_at_hold([], [], 90)))
    # 「大于等于30天」也要能被识别出起始天数，不能只认含 7 的那档
    check("长档「大于等于90天」也认（不是只认含 7 的档）",
          otc.redeem_fee_at_hold(["大于等于7天", "大于等于90天"], [0.5, 0.0], 90) == 0.0,
          str(otc.redeem_fee_at_hold(["大于等于7天", "大于等于90天"], [0.5, 0.0], 90)))


# ---------------------------------------------------------------- E. 回撤阈值
def t_dd():
    print("\n[E] 3 年回撤阈值按类型分档（中短债/固收回撤天然比长债浅）")
    check("债券型-中短债 → -5%", otc.dd_limit("债券型-中短债") == -0.05,
          str(otc.dd_limit("债券型-中短债")))
    check("指数型-固收 → -5%", otc.dd_limit("指数型-固收") == -0.05,
          str(otc.dd_limit("指数型-固收")))
    check("债券型-长债 → -10%", otc.dd_limit("债券型-长债") == -0.10,
          str(otc.dd_limit("债券型-长债")))
    check("债券型-信用债 → -10%", otc.dd_limit("债券型-信用债") == -0.10,
          str(otc.dd_limit("债券型-信用债")))
    check("未知类型回落到 max_dd_3y 兜底（不静默放行）",
          otc.dd_limit("未知类型") == float(otc.CFG_O.get("max_dd_3y", -0.05)),
          str(otc.dd_limit("未知类型")))


# ---------------------------------------------------------------- F. 打分
plan_fwd = None
try:                                  # plan.py 是上层编排，import 失败时只跳过这条断言
    import plan as _plan
    plan_fwd = _plan._otc_fwd_yield()[0]
except Exception:                     # noqa: BLE001
    plan_fwd = None


def t_score():
    print("\n[F] 综合分：0–100、方向正确、前瞻优先（2026-11-25 B-12）")
    # 夹具一律从 CFG_O["score_range"] 反推：区间是按实测分布校准的，硬写数值会在下次校准时误伤测试
    _rg = otc.CFG_O.get("score_range") or {}
    f_lo, f_hi = [float(x) for x in _rg["fwd_net_rate"]]
    d_good, d_bad = [float(x) for x in _rg["dd_1y"]]
    c_lo, c_hi = [float(x) for x in _rg["cost_1y"]]
    s_lo, s_hi = [float(x) for x in _rg["scale_yi"]]
    r_lo, r_hi = [float(x) for x in _rg["ret_1y"]]
    # 满分样本：5 个维度都顶到位
    best = {"fwd_net_rate": f_hi, "dd_1y": d_good, "cost_1y": c_lo,
            "scale_yi": s_hi, "ret_1y": r_hi}
    # 零分样本：每一项都在下限之外（前瞻低于下限、回撤最深、成本最高、规模 0、无收益）
    worst = {"fwd_net_rate": f_lo - 0.001, "dd_1y": d_bad, "cost_1y": c_hi,
             "scale_yi": 0.0, "ret_1y": 0.0}
    sb, sw = otc.score_of(best), otc.score_of(worst)
    check("最好样本 = 100 分", abs(sb - 100.0) < 1e-6, str(sb))
    check("最差样本 = 0 分", abs(sw - 0.0) < 1e-6, str(sw))
    check("前瞻区间是按实测分布校准的（下限 ≤0.5%，不是旧口径 1.1%）",
          f_lo <= 0.005 and f_hi <= 0.02, str((f_lo, f_hi)))
    check("分差方向正确（前瞻/回撤/成本/规模/收益 全优者高分）", sb > sw, f"{sb} vs {sw}")

    w = otc.CFG_O.get("score") or {}
    check("权重和 = 100（改口径只调比例，不加总量）",
          abs(sum(float(v) for v in w.values()) - 100.0) < 1e-6, str(w))
    check("前瞻净票息是第一大项（≥30）", float(w.get("fwd_net_rate") or 0) >= 30.0, str(w))
    check("历史 1 年收益不再是主轴（≤15）", float(w.get("ret_1y") or 0) <= 15.0, str(w))

    base = {"fwd_net_rate": (f_lo + f_hi) / 2, "dd_1y": (d_good + d_bad) / 2,
            "cost_1y": (c_lo + c_hi) / 2, "scale_yi": (s_lo + s_hi) / 2,
            "ret_1y": (r_lo + r_hi) / 2}
    for key, val, label in (("fwd_net_rate", f_hi, "前瞻净票息↑"),
                            ("ret_1y", r_hi, "收益↑"),
                            ("scale_yi", s_hi, "规模↑")):
        up = dict(base, **{key: val})
        check(f"{label} → 分↑", otc.score_of(up) > otc.score_of(base),
              f"{otc.score_of(up)} vs {otc.score_of(base)}")
    shallow = dict(base, dd_1y=d_good)
    check("回撤浅 → 分↑", otc.score_of(shallow) > otc.score_of(base),
          f"{otc.score_of(shallow)} vs {otc.score_of(base)}")
    cheap = dict(base, cost_1y=c_lo)
    check("成本低 → 分↑", otc.score_of(cheap) > otc.score_of(base),
          f"{otc.score_of(cheap)} vs {otc.score_of(base)}")

    # 关键回归（B-12 的要点）：016426 那种「历史 5.61%」在旧口径下能拿 40 分，
    # 新口径下历史项封顶 15 分 —— 光靠后视收益撑不起排名。
    hist_only = {"fwd_net_rate": f_lo, "dd_1y": d_bad, "cost_1y": c_hi,
                 "scale_yi": s_lo, "ret_1y": 0.0561}
    check("历史收益高但前瞻/回撤/成本都差 → 分 ≤15（历史不再是主轴）",
          otc.score_of(hist_only) <= 15.0 + 1e-6, str(otc.score_of(hist_only)))
    check("缺前瞻字段（旧产物）不抛异常且该项记 0",
          isinstance(otc.score_of({"ret_1y": 0.02}), float), "抛异常")
    # 测试审计 A-6：只断言「是 float」会被任何实现蒙过，钉死精确分值。
    check("历史收益项精确：0.02/0.05 × 15 分 = 6.0 分",
          abs(otc.score_of({"ret_1y": 0.02}) - 6.0) < 1e-9,
          str(otc.score_of({"ret_1y": 0.02})))
    check("空 dict → 0.0 分（不许拿默认值蒙分）",
          abs(otc.score_of({})) < 1e-12, str(otc.score_of({})))
    check("0 ≤ 分 ≤ 100",
          all(0.0 <= otc.score_of(x) <= 100.0 for x in (best, worst, base)), "越界")

    _orig_fby = m.fwd_bond_yield
    if m.fwd_bond_yield()[0] is None:      # CI 无本机国债缓存 → 注入固定曲线
        m.fwd_bond_yield = lambda *a, **k: (0.01283, "2026-11-25")
    fwd, asof = m.fwd_bond_yield()
    check("前瞻票息可解析（3Y/5Y 国债均值 ∈ 0–10%）且有日期",
          fwd is not None and 0 < fwd < 0.10 and bool(asof), f"{fwd} asof={asof}")
    if plan_fwd is None:
        skip("plan/otc 同一份前瞻票息", "plan 未导入（跳过）")
    else:
        check("plan 侧与 otc 侧用的是同一份前瞻票息（不许各写一遍）",
              abs(float(plan_fwd) - float(fwd or 0.0)) < 1e-9,
              f"plan={plan_fwd} metrics={fwd}")
    m.fwd_bond_yield = _orig_fby

def t_level():
    print("\n[G] 风险等级：一律走 risk 模块，不在本文件硬写字符串")
    bond_lv = str((config.RISK.get("etf_map") or {}).get("bond"))
    lv = otc.level_of("债券型-中短债", "华夏短债债券C", {"vol_3y": 0.008})
    check(f"债基名称 → {bond_lv}", str(lv["level"]) == bond_lv, str(lv))
    check("等级带说明（可追溯）", bool(lv.get("reason")), str(lv))
    # 名称不带「债」字时不能 fail-closed 成 R5（L1 已限定债券型），否则整池子被打成高风险
    lv2 = otc.level_of("债券型-中短债", "某某稳健C", {"vol_3y": 0.008})
    check(f"名称未命中「债」仍按 {bond_lv}（不用 etf_level 的 R5 兜底）",
          str(lv2["level"]) == bond_lv, str(lv2))
    why = str(lv2.get("reason"))
    # 依据要写清（提到基金类型），且**不能**出现「ETF」字样——场外基金写「场内债券 ETF」是误导；
    # 也不能 fail-closed 落到 R5（L1 已限定债券型，落 R5 等于整池子被打成高风险）。
    check("reason 写明依据（提到中短债 / 不出现 ETF / 不落到 R5）",
          ("中短债" in why) and ("ETF" not in why) and ("R5" not in why), why)


# ---------------------------------------------------------------- H. 表结构契约
def t_schema():
    print("\n[H] 输出表结构 = 契约列（plan.py 按名取列，少一列就 KeyError）")
    check("_COLS 与契约逐项一致且顺序一致", list(otc._COLS) == _CONTRACT_COLS,
          f"差异: {set(otc._COLS) ^ set(_CONTRACT_COLS)}")
    check("_COLS 无重复列名", len(set(otc._COLS)) == len(otc._COLS), "有重复列")
    check("必含下游要用的 5 列：code/ret_1y/dd_1y/cost_1y/score",
          all(c in otc._COLS for c in ("code", "ret_1y", "dd_1y", "cost_1y", "score")),
          str(otc._COLS))


# ---------------------------------------------------------------- H2. 持有期过滤（2026-11-25 A 类）
def t_hold_period():
    print("\n[H2] 产品持有期硬过滤（018193「180 天持有期」教训）")
    check("名称解析：180 天 → 180", m.hold_period_days("建信鑫弘180天持有期债券C") == 180,
          str(m.hold_period_days("建信鑫弘180天持有期债券C")))
    check("名称解析：30 天 → 30", m.hold_period_days("建信鑫和30天持有期债券C") == 30)
    check("名称解析：无持有期条款 → None",
          m.hold_period_days("永赢中债3-5年政金债指数C") is None,
          str(m.hold_period_days("永赢中债3-5年政金债指数C")))
    check("名称解析：中文数字「一年持有」→ 365",
          m.hold_period_days("某某一年持有期混合") == 365,
          str(m.hold_period_days("某某一年持有期混合")))
    # A11：定开/封闭产品名称里常没有「N天持有」，必须 fail-closed 当超长锁定
    check("A11 「定开」无天数 → 按 3650 天（fail-closed）",
          m.hold_period_days("鑫元金融债3个月定开") == 3650,
          str(m.hold_period_days("鑫元金融债3个月定开")))
    check("A11 「封闭」无天数 → 按 3650 天（fail-closed）",
          m.hold_period_days("某某两年封闭运作债券") == 3650,
          str(m.hold_period_days("某某两年封闭运作债券")))
    cfg_otc = dict((config.RISK.get("wealth") or {}).get("otc") or {})
    check("config.RISK.wealth.otc.max_hold_period_days 存在且 ≤ 90",
          0 < int(cfg_otc.get("max_hold_period_days") or 0) <= 90,
          str(cfg_otc.get("max_hold_period_days")))
    if os.path.exists(CSV):
        df = pd.read_csv(CSV, dtype={"code": str})
        if "hold_period_days" in df.columns:
            over = df[pd.to_numeric(df["hold_period_days"], errors="coerce")
                      > int(cfg_otc.get("max_hold_period_days") or 30)]
            check("产物池里没有持有期 > 上限的基金", len(over) == 0,
                  f"仍有 {len(over)} 只：{list(over['code'])[:5]}")
        else:
            skip("产物池持有期列", "旧产物没有 hold_period_days 列（重跑 otc.py 后生效）")
    else:
        skip("产物池持有期列", "没有 stage1_otc.csv")


# ---------------------------------------------------------------- I. 缓存往返
def t_cache():
    print("\n[I] 缓存回读：6 位基金代码不能被 pandas 读成 int（000682 → 682）")
    if ds._force_refresh():
        skip("缓存往返", "A_SHARE_FORCE_REFRESH 已开启，会走网络")
        return
    tmp = tempfile.mkdtemp(prefix="otc_selftest_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    p = os.path.join(tmp, "money_rank.csv")
    # 模拟「归一化后写进缓存」再被 pandas 读回：整数 682 而非字符串 '000682'
    pd.DataFrame({"code": [682, 2234, 4121], "name": ["甲", "乙", "丙"],
                  "date": ["2026-10-27"] * 3, "wanfen": [0.5] * 3,
                  "y7_pct": [1.2] * 3, "y14_pct": [1.1] * 3, "y28_pct": [1.0] * 3,
                  "ret_1y_pct": [1.4] * 3, "fee_pct": [0.0] * 3}).to_csv(p, index=False)
    # 缓存路径改由 stock 实现层的钩子注入（转出层的名字 patch 不生效：实现函数查的是自己模块的全局名）
    if hasattr(ds, "set_cache_hook"):
        ds.set_cache_hook(lambda kind, key: p)
    else:  # 老实现（未迁移前）
        orig = ds._cache_file
        ds._cache_file = lambda kind, key: p
    try:
        df = ds.fetch_money_fund_rates()
    finally:
        if hasattr(ds, "set_cache_hook"):
            ds.set_cache_hook(None)
        else:
            ds._cache_file = orig
    codes = [str(x) for x in df["code"].tolist()]
    check("缓存命中路径也把 code 补回 6 位（连接键不能静默丢前导零）",
          codes == ["000682", "002234", "004121"], f"got={codes}")

    # datasource 侧的可复用断言（T1 落地）：三次召回，第二次才是缓存路径
    fn = getattr(ds, "selftest_cache_codes", None)
    if fn is None:
        skip("datasource.selftest_cache_codes", "该函数尚未提供")
    else:
        bad = fn()
        check("真实缓存往返：money_rank/nav_daily/purchase 全表 code 均 6 位", bad == 0,
              f"失败 {bad} 处")


# ---------------------------------------------------------------- J. 产物一致性
def t_artifact():
    print("\n[J] 产物一致性：out/stage1_otc.csv 的每条都必须守规矩")
    if not os.path.exists(CSV):
        skip("stage1_otc.csv 一致性", f"{CSV} 不存在（先跑 scripts/otc.py）")
        return
    df = pd.read_csv(CSV, dtype={"code": str})
    # 中间产物护栏：--limit N 跑出来的池子不是真池子，不能拿它当验收通过
    fjson = os.path.join(OUT, "stage1_otc_funnel.json")
    if os.path.exists(fjson):
        import json as _json
        with open(fjson, encoding="utf-8") as f:
            fun = _json.load(f)
        if fun.get("limited"):
            skip("stage1_otc.csv 一致性",
                 f"该文件是 --limit {fun.get('limit')} 的中间产物（L5 只测了前 N 只），"
                 f"不是全量池；全量跑完再验收")
            return
        check("全量跑的漏斗总数 = 全市场场外基金表行数",
              int(fun.get("L0_total") or 0) > 10000, f"L0={fun.get('L0_total')}")
        check("L5 之后仍有 ≥3 只（少于 3 只则 R2 配不出篮子）",
              int(fun.get("L5_after") or 0) >= 3, f"L5_after={fun.get('L5_after')}")
    else:
        skip("漏斗 JSON", f"{fjson} 不存在")
    check("非空", len(df) > 0, "0 行")
    # 契约列比对允许「旧产物少新列」：2026-11-25 新增 hold_period_days，磁盘上的 CSV 是上一版
    # otc.py 产物，重跑才会补上。严格相等会让「改了代码还没重跑数据」变成红灯 —— 那属于
    # 数据新鲜度问题（下面 A 段的 funnel final 会看），不是契约破坏。缺**老列**才是破坏。
    _new_cols = {"hold_period_days", "scale_yi", "fwd_net_rate", "fwd_gap_vs_cash"}
    check("列 = 契约列（允许旧产物缺新增列）",
          list(df.columns) == _CONTRACT_COLS
          or (set(df.columns) == set(_CONTRACT_COLS) - _new_cols),
          f"差异: {set(df.columns) ^ set(_CONTRACT_COLS)}")
    if "hold_period_days" not in df.columns:
        skip("产物持有期列", "旧产物（重跑 otc.py 后补齐；plan 侧已按名称兜底解析）")
    if "fwd_net_rate" not in df.columns:
        skip("产物前瞻列", "旧产物（重跑 otc.py 后补齐；plan 侧已按名称兜底解析）")

    codes = df["code"].astype(str).tolist()
    check("code 全是 6 位数字（前导零没被吃掉）",
          all(len(c) == 6 and c.isdigit() for c in codes),
          f"异常: {[c for c in codes if not (len(c) == 6 and c.isdigit())][:5]}")
    check("code 无重复", len(set(codes)) == len(codes), "有重复 code")
    check("share key 无重复（A/C 去重真的生效了）",
          df["key"].astype(str).duplicated().sum() == 0,
          f"重复 {int(df['key'].astype(str).duplicated().sum())} 条")

    ftypes = set(str(x) for x in otc.CFG_O["ftypes"])
    bad_ft = [x for x in df["ftype"].unique() if str(x) not in ftypes]
    check("类型全在允许范围内", not bad_ft, f"越界类型: {bad_ft}")
    check("没有货币基金（否则与现金层重复计风险预算）",
          not any("货币" in str(x) for x in df["ftype"].unique()),
          str(df["ftype"].unique()))

    ex = [(c, otc.excluded_word(n)) for c, n in zip(df["code"], df["name"])
          if otc.excluded_word(n) is not None]
    check("名称不含排除词（联接/可转债/增强/双债…）", not ex, f"命中: {ex[:5]}")

    check("申购状态 ⊆ {开放申购, 限大额}",
          set(df["purchase_status"].astype(str)) <= set(
              str(x) for x in otc.CFG_O["purchase_status_ok"]),
          str(df["purchase_status"].unique()))
    check("起购 ≤ max_min_purchase",
          float(df["min_purchase"].max()) <= float(otc.CFG_O["max_min_purchase"]),
          str(df["min_purchase"].max()))
    check("申购费 ≤ max_fee_pct",
          float(df["fee"].max()) <= float(otc.CFG_O["max_fee_pct"]),
          str(df["fee"].max()))
    check("历史 ≥ min_hist_days（算不出 3 年回撤的不进池）",
          int(df["hist_days"].min()) >= int(otc.CFG_O["min_hist_days"]),
          str(df["hist_days"].min()))

    dd_bad = [c for c, t, d in zip(df["code"], df["ftype"], df["dd_3y"])
              if float(d) < otc.dd_limit(str(t)) - 1e-9]
    check("3 年回撤都在分类型阈值内", not dd_bad, f"越界: {dd_bad[:5]}")

    check("score ∈ [0,100]",
          bool(((df["score"] >= 0) & (df["score"] <= 100)).all()),
          str(df["score"].min()) + "~" + str(df["score"].max()))
    check("按 score 降序（plan.py 取前 N 只时依赖这个顺序）",
          bool(df["score"].is_monotonic_decreasing), "未降序")
    check("等级一律 R2（R2 层只放 R2 标的）",
          set(df["level"].astype(str)) == {"R2"}, str(df["level"].unique()))
    # 数量级护栏：真·债基首年成本不可能 ≥5%（5% 意味着百分数被当小数用了 100 倍）。
    # 上限从 2% 放宽到 5% 的原因：池里确实存在「持有 <365 天收 1.50%」的基金，
    # 其 90 天口径赎回费就是 1.5%，加运作费后 cost_1y 可达 2.7%——这是真数据，不是 bug。
    check("首年成本都合理（0 < cost_1y < 5%）",
          bool((df["cost_1y"] > 0).all() and (df["cost_1y"] < 0.05).all()),
          str(df["cost_1y"].min()) + "~" + str(df["cost_1y"].max()))

    # 与场内 ETF 池互斥（用户要求「别弄重复」的最终验收）
    if os.path.exists(ETF_CSV):
        etf = pd.read_csv(ETF_CSV)
        etf_codes = {ds.norm_code(x) for x in etf["code"].astype(str)} \
            if "code" in etf.columns else set()
        inter = set(codes) & etf_codes
        check("场外池 ∩ 场内 ETF 池 = ∅（不重复持仓）", not inter, f"交集: {sorted(inter)[:5]}")
    else:
        skip("与场内 ETF 池互斥", f"{ETF_CSV} 不存在")


# ---------------------------------------------------------------- K. 开关
def _sig(p):
    """文件的 (mtime_ns, sha256)；不存在返回 None。用于「测试没写产物」断言。"""
    import hashlib
    try:
        st = os.stat(p)
        with open(p, "rb") as f:
            return (st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    except OSError:
        return None


def t_switch():
    print("\n[K] enabled=False 时必须显式拒绝，而不是静默跑出空池")
    _before = _sig(CSV)
    old = otc.CFG_O.get("enabled")
    otc.CFG_O["enabled"] = False
    try:
        otc.run_screen(limit=1, verbose=False)
        raised = False
    except SystemExit:
        raised = True
    except Exception:      # noqa: BLE001
        raised = False
    finally:
        otc.CFG_O["enabled"] = old
    check("enabled=False → SystemExit（fail loud）", raised, "没有抛 SystemExit")
    # 测试审计 A-5：拒绝路径也必须证明没动生产产物（哈希 + mtime 前后相等）
    check("enabled=False 的 run_screen 不写产物（stage1_otc.csv 哈希/mtime 不变）",
          _sig(CSV) == _before, f"{_before} -> {_sig(CSV)}")


def main():
    print("otc.py 离线自检")
    t_config()
    t_share()
    t_exclude()
    t_cost()
    t_redeem()
    t_dd()
    t_score()
    t_level()
    t_schema()
    t_hold_period()
    t_cache()
    t_artifact()
    t_switch()
    print()
    print(f"断言：{PASS + FAIL} 项｜通过 {PASS}｜失败 {FAIL}｜跳过 {SKIP}")
    print("OK 全部通过" if FAIL == 0 else "✗ 有断言失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
