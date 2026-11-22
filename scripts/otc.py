# -*- coding: utf-8 -*-
"""R2 场外债券基金层（OTC）：场内债券 ETF 一手买不起时的兜底腿 —— 阶段一·场外池。

为什么需要这条腿（「加入场外基金」）：
  场内债券 ETF 一手 ≈ 1–1.2 万元。5 万本金按 R2 单只 12% 上限（6,000 元）买不起 1 只，
  小本金**常常买不起**一手债券 ETF —— 于是 R2 层在 5 万账本上名义有预算、实际无标的。
  场外债券基金 10/100 元起购、申购费 1 折后常为 0，小本金能精确落到 R2 预算的任何金额。
  代价：场外 T+1/T+2 确认、不能盘中成交、单位净值只按日更新 → 只做「买了放着」的稳健层，
  不承担择时与盘中交易。

与 wealth.py（场内债券 ETF）的分工 —— 用户「切记别弄重复了」的落地：
  1. 名称含「联接」→ 排除：ETF 联接基金就是同一只场内 ETF 的场外镜像，重复持仓；
  2. 货币基金 → 排除：R1 已由现金层（逆回购 + 货基）承担，放进 R2 会重复计风险预算；
  3. 「可转债/转债/增强/双债/混合一级/混合二级」→ 排除：股性或打新成分，波动不是 R2；
  4. 同一基金 A/C/E/I 份额 → 只留一只（优先 C 类：免申购费，短持有期更省）；
  5. 场内买得起就只用场内，买不起才用场外（plan 层互斥，由 plan.py 保证）。

漏斗（先便宜后昂贵，每层淘汰数全部写进 out/stage1_otc_funnel.json）：
  L0 fund_purchase_em 全表 27,884
  L1 基金类型 ∈ RISK.wealth.otc.ftypes（中短债/长债/信用债/固收指数）
  L2 名称排除词（联接/可转债/转债/增强/双债/混合一级/混合二级）
  L3 申购状态 ∈ {开放申购, 限大额}：**「限大额」不是排除项** —— 本金远低于限额；
     日累计限定金额不能太小（限额太小会让分批买入卡住；0/空 视为不限）
  L4 购买起点 ≤ 1,000 元；申购费（折后）≤ 0.15%
  L5 逐只取「累计净值走势」：历史 ≥ 750 交易日（要算 3 年回撤）；
     近 3 年最大回撤 ≥ 阈值（中短债/固收指数 −5%，长债/信用债 −10%）
  L6 份额类别去重（同基金 A/C/E/I 只留一只，优先 C）
  L7 规模补查（best-effort，失败不阻断也不淘汰，只记 scale_unavailable）：≥ 1 亿

打分（复用 metrics.score_linear / score_inverse，与 wealth.py 同一套几何）：
  近 1 年收益 40 + 近 1 年最大回撤 35 + 首年总成本（反向）25
  首年总成本 = 申购费（已 1 折）+ 一年运作费（管理+托管+销售服务）+ 持有 HOLD_DAYS 天的赎回费
  「首年」而不是「年化」：申购费只交一次，摊到第 2 年就不成立；用户问的是「今年买它
  要先付多少」。

⚠️ 算收益必须用**累计净值走势**：分红/拆分会把单位净值压低，用单位净值算出来的收益
是系统性偏低的假数（本文件 ret_1y / ret_3y / dd_* / vol_3y 全部基于累计净值）。

⚠️ 单位约定：datasource 里场外段的 `*_pct` 列是百分数（0.15 = 0.15%），本文件负责
÷100 换成小数；CSV 里一律输出**小数**（回撤 -0.018 表示 -1.8%）。

产出：out/stage1_otc.csv、out/stage1_otc_funnel.json、out/provenance_otc.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config            # noqa: E402
import datasource as ds  # noqa: E402
import metrics as m      # noqa: E402
import risk              # noqa: E402

OUT = config.OUT_DIR
CFG_W = config.RISK["wealth"]

# 估算首年赎回费时假设的持有天数。为什么是 90：R2 是稳健持有层，三批建仓 + regime 触发
# 调仓，实际持有期是数月而非 7 天；接口的赎回阶梯通常是「<7天 1.5% / 7–29天 0.75% /
# ≥30天 0%」，按 7 天档估会系统性高估成本，还恰好惩罚「短期赎回贵、长期免费」的基金
# （这类恰恰更适合长期拿）。取一个季度：比按 7 天/30 天估更接近实际，比按最短持有估保守一档。
HOLD_DAYS = 90

# config 落在 RISK["wealth"]["otc"]（由 T2 维护，otc.py 只读）。这里保留同值兜底，
# 保证「config 还没补上这个子段」时也能跑出同一结论；补上后自动以 config 为准。
_OTC_DEFAULTS = {
    "enabled": True,
    "ftypes": ["债券型-中短债", "债券型-长债", "债券型-信用债", "指数型-固收"],
    "name_exclude": ["联接", "可转债", "转债", "增强", "双债", "混合一级", "混合二级"],
    "purchase_status_ok": ["开放申购", "限大额"],
    "min_day_limit": 10000.0,
    "max_min_purchase": 1000.0,
    "max_fee_pct": 0.15,
    "min_hist_days": 750,
    "max_dd_3y_by_ftype": {"债券型-中短债": -0.05, "指数型-固收": -0.05,
                           "债券型-信用债": -0.10, "债券型-长债": -0.10},
    "max_dd_3y": -0.05,
    "min_scale_yi": 1.0,
    "min_position_value": 100.0,
    "share_class_prefer": "C",
    "score": {"ret_1y": 40, "dd_1y": 35, "cost_1y": 25},
    "score_range": {"ret_1y": [0.0, 0.03], "dd_1y": [-0.002, -0.02],
                    "cost_1y": [0.001, 0.01]},
}


def _merge_cfg() -> dict:
    """config 的 otc 子段 → 与默认值深合并（config 只覆盖它写了的键）。"""
    raw = dict(CFG_W.get("otc") or {})
    out = json.loads(json.dumps(_OTC_DEFAULTS, ensure_ascii=False))   # 深拷贝（值都可 JSON）
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


CFG_O = _merge_cfg()

_COLS = ["code", "name", "ftype", "nav_date", "unit_nav", "acc_nav", "purchase_status",
         "min_purchase", "day_limit", "fee", "hist_days", "ret_1y", "ret_3y", "dd_1y",
         "dd_3y", "vol_3y", "mgmt_fee", "redeem_fee_hold", "cost_1y", "score", "level",
         "level_reason", "key", "share_class"]

# 份额类别后缀：只认单个字母（A/B/C/D/E/F/H/I）。字母前面的字符不能是数字 ——
# 债基名称里「30天」「7天」「1年」「3个月」这类期限后缀太常见，误当份额会把 A 类吃掉。
_CLASS = ("A", "B", "C", "D", "E", "F", "H", "I")
_FULLWIDTH = str.maketrans("ＡＢＣＤＥＦＨＩ", "ABCDEFHI")


def share_split(name: str) -> tuple[str, str]:
    """'华夏短债债券C' → ('华夏短债债券', 'C')；'华夏短债债券' → ('华夏短债债券', '')。

    同时兼容「华夏短债-C」「华夏短债C类」写法，先归一化再切，保证同一基金的 A/C 两个
    份额算出来的 key 完全一致（L6 去重靠它）。
    """
    nm = str(name).strip().translate(_FULLWIDTH)
    t = nm.replace("-", "").replace(" ", "")
    cls = ""
    if len(t) >= 2 and t[-1] in _CLASS and not t[-2].isdigit():
        cls, t = t[-1], t[:-1]
    elif len(t) >= 3 and t[-1] == "类" and t[-2] in _CLASS and not t[-3].isdigit():
        cls, t = t[-2], t[:-2]
    return t, cls


def excluded_word(name: str) -> str | None:
    """名称里命中的排除词（返回命中的那个词，便于漏斗统计「为什么被排掉」）。"""
    nm = str(name)
    for w in CFG_O.get("name_exclude") or []:
        if w and w in nm:
            return str(w)
    return None


def nav_metrics(code: str) -> dict:
    """用「累计净值走势」算收益/回撤/波动。历史不足 60 天视为取数失败。"""
    df = ds.fetch_fund_nav_history(code, "累计净值走势")
    if df is None or len(df) < 60:
        return {"ok": False, "reason": f"累计净值仅 {0 if df is None else len(df)} 条"}
    nav = pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                    index=pd.DatetimeIndex(df["date"])).dropna()
    if len(nav) < 60:
        return {"ok": False, "reason": f"有效累计净值仅 {len(nav)} 条"}

    def win(k: int) -> pd.Series:
        return nav.tail(min(len(nav), k))

    def ret(s: pd.Series) -> float | None:
        return None if len(s) < 2 else float(s.iloc[-1] / s.iloc[0] - 1.0)

    def cagr(s: pd.Series) -> float | None:
        """年化（CAGR）用真实日历长度折算：244/年 只是交易日近似，跨闰年/长假会偏。"""
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        if len(s) < 2 or yrs <= 0:
            return None
        return float((s.iloc[-1] / s.iloc[0]) ** (1.0 / yrs) - 1.0)

    c1, c3 = win(244), win(750)
    rets = nav.pct_change().dropna()
    return {
        "ok": True,
        "nav_date": str(nav.index[-1].date()),
        "acc_nav": round(float(nav.iloc[-1]), 4),
        "hist_days": int(len(nav)),
        "ret_1y": ret(c1),
        # 契约列名是 ret_3y；这里的值是**近 3 年 CAGR**（年度化，便于和理财/存款比）
        "ret_3y": cagr(c3),
        "dd_1y": m.f(m.max_drawdown(c1)),
        "dd_3y": m.f(m.max_drawdown(c3)),
        "vol_3y": m.f(m.volatility(c3)),
        "max_1d": m.f(float(rets.abs().max())) if len(rets) else None,
    }


def _nav_metrics_safe(code: str):
    """线程池用：单只失败不能拖垮整轮（返回 error 串便于漏斗里记账）。"""
    try:
        return code, nav_metrics(code), None
    except Exception as e:  # noqa: BLE001
        return code, None, f"{type(e).__name__}: {e}"


def dd_limit(ftype: str) -> float:
    """该类型的 3 年回撤上限（小数，负数）。中短债/固收指数 −5%，长债/信用债 −10%。"""
    by = CFG_O.get("max_dd_3y_by_ftype") or {}
    return float(m.f(by.get(ftype), m.f(CFG_O.get("max_dd_3y"), -0.05)))


_START_GE = re.compile(r"(?:大于等于|大于|>=|≥)\s*(\d+)\s*(年|个月|月|天|日)?")
_START_LE = re.compile(r"(?:小于等于|小于|<=|≤)\s*(\d+)\s*(年|个月|月|天|日)?")
_UNIT_DAYS = {"年": 365, "个月": 30, "月": 30, "天": 1, "日": 1}


def _span_days(num: str, unit: str | None) -> int:
    """`3年` → 1095、`6个月` → 180、`7天` → 7。**必须换单位**：把「大于等于1年」当成 1 天
    会把长持有期档误判成「90 天内适用」，直接低估赎回费（甚至把 0% 的长期档当成 90 天档）。
    """
    return int(num) * _UNIT_DAYS.get(str(unit or "天"), 1)


def _start_day(period: str) -> int:
    """赎回费阶梯的「起始持有天数」：`大于等于7天，小于等于29天` → 7；`小于7天` → 0；
    `大于等于1年` → 365（换单位，见 `_span_days`）。

    取下界而不是上界：阶梯是「从第 N 天起适用这一档」。`小于` 开头说明下界是 0 天。
    """
    s = str(period).strip().replace(" ", "")
    m_ge = _START_GE.search(s)
    if m_ge:
        return _span_days(m_ge.group(1), m_ge.group(2))
    if _START_LE.search(s):
        return 0
    m = re.search(r"(\d+)\s*(年|个月|月|天|日)?", s)   # 「30天以上」这类没有比较词的写法
    return _span_days(m.group(1), m.group(2)) if m else 0


def redeem_fee_at_hold(periods: list[str], fees: list[float],
                       hold_days: int = HOLD_DAYS) -> float | None:
    """按 `hold_days` 天持有期估赎回费（百分数）。纯函数，方便离线断言（不联网取费表）。

    实现说明（旧版 bug：docstring 说取宽松档、代码却取「刚满 7 天」那档，导致 000033 取到
    0.75% 而不是 0.00%，系统性高估成本）：
      1. 先判「只有惩罚档」：费率表只列了「小于7天 1.50%」这类**上界档**、没有任何下界档时，
         未列出的区间（持有 ≥7 天）按费表通行写法 = 免费，返回 0.0（不是 1.5）；
      2. 否则取「起始天数 ≤ hold_days 的档里起始天数最大」的那一档；
      3. 一档都不满足时取最小费率档兜底（真出现乱七八糟的费表时宁可低估也不抛错）。
    """
    pairs: list[tuple[int, float, int | None]] = []
    for p, fee in zip(periods, fees):
        v = m.f(fee)
        if v is None:
            continue
        ps = str(p).strip().replace(" ", "")
        ge = _START_GE.search(ps)
        le = _START_LE.search(ps)
        start = _span_days(ge.group(1), ge.group(2)) if ge else 0
        upper = _span_days(le.group(1), le.group(2)) if (le and not ge) else None
        pairs.append((start, float(v), upper))
    if not pairs:
        return None
    if all(s == 0 for s, _, _ in pairs):
        # 只有「小于 X 天」的上界档：持有 ≥X 天没有对应档 = 免费
        cap = min([u for _, _, u in pairs if u is not None] or [7])
        return 0.0 if hold_days >= cap else min(v for _, v, _ in pairs)
    hit = [(s, v) for s, v, _ in pairs if s <= hold_days]
    if hit:
        return max(hit, key=lambda x: x[0])[1]
    return min(v for _, v, _ in pairs)


def fee_parts(code: str) -> dict:
    """运作费 + 赎回费（百分数）。管理费缺失按 0 计；托管/销售服务费同。

    赎回费按 `redeem_fee_at_hold()`（默认持有一个季度）估，不再取「刚满 7 天」那档；
    只有「7 天内赎回」的高惩罚档会被排除（我们不做 7 天内赎回）。
    """
    op = ds.fetch_fund_fee(code, "运作费用")
    redeem = ds.fetch_fund_fee(code, "赎回费率")

    def _item(item_name: str) -> float | None:
        row = op[op["item"] == item_name]
        return None if len(row) == 0 else m.f(row["value_pct"].iloc[0])

    parts = {k: _item(v) for k, v in (("mgmt_fee", "管理费率"), ("custody_fee", "托管费率"),
                                      ("sales_fee", "销售服务费率"))}
    op_pct = float(sum(v for v in parts.values() if v is not None))

    periods = [str(x) for x in redeem["period"].tolist()]
    fees = [m.f(v) for v in redeem["fee_pct"].tolist()]
    red = redeem_fee_at_hold(periods, fees)
    parts.update({"op_fee": round(op_pct, 4), "redeem_fee_hold": red})
    return parts


def _fee_parts_safe(code: str):
    try:
        return code, fee_parts(code), None
    except Exception as e:  # noqa: BLE001
        return code, None, f"{type(e).__name__}: {e}"


def cost_1y(fee_pct, op_pct, redeem_pct) -> float:
    """首年总成本（小数）：申购费 + 一年运作费 + 持有 HOLD_DAYS 天赎回费（见 redeem_fee_at_hold）。"""
    buy = (m.f(fee_pct, 0.0) or 0.0) / 100.0
    run = (m.f(op_pct, 0.0) or 0.0) / 100.0
    red = (m.f(redeem_pct, 0.0) or 0.0) / 100.0
    return round(buy + run + red, 6)


def score_of(r: dict) -> float:
    """综合分 0–100：近 1 年收益 40 + 近 1 年回撤 35 + 首年总成本（反向）25。"""
    w = CFG_O.get("score") or {}
    rg = CFG_O.get("score_range") or {}

    def rng(key: str, dflt: list) -> tuple[float, float]:
        v = rg.get(key) or dflt
        return float(v[0]), float(v[1])

    lo, hi = rng("ret_1y", [0.0, 0.03])
    s = m.score_linear(r.get("ret_1y"), lo, hi, float(w.get("ret_1y", 40)))
    good, bad = rng("dd_1y", [-0.002, -0.02])
    s += m.score_inverse(r.get("dd_1y"), good, bad, float(w.get("dd_1y", 35)))
    clo, chi = rng("cost_1y", [0.001, 0.01])
    s += m.score_inverse(r.get("cost_1y"), clo, chi, float(w.get("cost_1y", 25)))
    return round(float(s), 2)


def next_level(level: str) -> str:
    """按 config.RISK["levels"] 的登记顺序取下一档（R2 → R3）。等级降级不在这里写死字符串。"""
    keys = [k for k in (config.RISK.get("levels") or {})]
    try:
        i = keys.index(level)
    except ValueError:
        return level
    return keys[i + 1] if i + 1 < len(keys) else level


# 场外基金按类型给的定级理由。**不能复用 risk.etf_level() 的文案**：那是场内 ETF 口径，
# 会把「场内债券 ETF」写进 level_reason，用户会以为买了场内 ETF（旧版文案 bug）。
# 为什么按类型分开写：中短债/长债的久期敞口差一档，回撤量级不同，理由里要写清。
_LEVEL_TEXT = {
    "债券型-中短债": "中短债基金：久期短、波动低，R2 中低风险",
    "债券型-短债": "短债基金：久期短、波动低，R2 中低风险",
    "债券型-长债": "长债基金：久期较长，R2 中低风险（回撤可能 >2%）",
    "债券型-信用债": "信用债基金：久期较长且承担信用利差，R2 中低风险（回撤可能 >2%）",
    "指数型-固收": "债券指数基金（场外）：R2 中低风险；有久期敞口，非保本",
}


def level_of(ftype: str, name: str, met: dict) -> dict:
    """给场外基金定级：基准等级仍取 risk/config（不硬写字符串），文案换成场外口径。

    1. 基准等级：先用 risk.etf_level()（同一套 R1–R5，命中「债」→ config.RISK.etf_map.bond）；
       名称不含债券关键词时 etf_level 会 fail-closed 给 R5，但 L1 已限定 ftype 属债券/固收型，
       名称缺关键词不代表风险更高 → 退回 etf_map.bond 的口径（配置驱动）。
    2. fail-closed 降级：3 年回撤越过本类型阈值（即 L5 用的 dd_limit）就不留 R2，降到下一档。
       当前池子 L5 已用同一阈值剔除，所以这条**在正常数据下不会触发**，是双保险；
       触发时理由里写清越界数值，避免「硬编码 R2」。
    """
    bond_lv = str((config.RISK.get("etf_map") or {}).get("bond") or "R2")
    lv = risk.etf_level(name, "", met)
    level = bond_lv if str(lv.get("level")) != bond_lv else str(lv["level"])
    text = _LEVEL_TEXT.get(ftype) or f"{ftype}：R2 中低风险"
    vol = m.f(met.get("vol_3y"))
    vol_txt = f"；年化波动 {vol * 100:.1f}%（仅作风险提示，不改变等级）" if vol is not None else ""
    dd = m.f(met.get("dd_3y"))
    lim = dd_limit(ftype)
    if level == bond_lv and dd is not None and dd < lim:
        up = next_level(bond_lv)
        label = str((config.RISK.get("levels") or {}).get(up, {}).get("label") or "")
        return {"level": up,
                "reason": (f"{text}；但 3 年最大回撤 {dd * 100:.2f}% 已超过本类型 R2 边界 "
                           f"{lim * 100:.0f}%（L5 同阈值）→ 降为 {up}"
                           f"{'（' + label + '）' if label else ''}，不进 R2 层{vol_txt}")}
    return {"level": level, "reason": f"{text}{vol_txt}"}


def nav_daily_map() -> dict:
    """code → {nav_date, unit_nav, acc_nav}：一张全表补齐「完整净值日期 + 单位净值」，
    省掉几千次单只请求（单只接口只有「成立来」历史，没给「今天的单位净值」）。"""
    try:
        df = ds.fetch_fund_nav_daily()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for rec in df.to_dict("records"):
        out[str(rec["code"])] = {"nav_date": str(rec["nav_date"]),
                                 "unit_nav": m.f(rec["unit_nav"]),
                                 "acc_nav": m.f(rec["acc_nav"])}
    return out


def scale_of(code: str) -> float | None:
    """规模（亿元）；取不到返回 None（L7 只对「查到且太小」的淘汰，查不到不静默淘汰）。"""
    try:
        return ds.fetch_fund_scale(code)
    except Exception:  # noqa: BLE001
        return None


def run_screen(limit: int | None = None, verbose: bool = True,
               workers: int = 6) -> tuple[pd.DataFrame, dict]:
    t0 = time.time()
    if not CFG_O.get("enabled", True):
        raise SystemExit("[otc] config.RISK.wealth.otc.enabled = False：场外池已关闭")

    snap = ds.fetch_fund_purchase_snapshot()
    stages: list[dict] = []
    funnel: dict = {"limited": bool(limit), "limit": int(limit or 0),
                    "L0_total": int(len(snap))}
    fail: dict[str, int] = {}

    def _mark(reason: str) -> None:
        fail[reason] = fail.get(reason, 0) + 1

    def _stage(layer: str, left: int, dropped: int, reason: str) -> None:
        stages.append({"layer": layer, "left": int(left), "dropped": int(dropped),
                       "reason": reason})

    if verbose:
        print(f"[otc] L0 场外基金全表 {len(snap)} 只")

    # ---- L1 基金类型 -------------------------------------------------------
    ftypes = set(CFG_O.get("ftypes") or [])
    pool = [r for r in snap.to_dict("records") if r["ftype"] in ftypes]
    funnel["L1_ftype_not_match"] = int(len(snap)) - len(pool)
    funnel["L1_after"] = len(pool)
    _stage("L1", len(pool), int(len(snap)) - len(pool),
           "基金类型只留 " + "/".join(sorted(ftypes)))

    # ---- L2 名称排除 -------------------------------------------------------
    words: dict[str, int] = {}
    keep2 = []
    for r in pool:
        w = excluded_word(r["name"])
        if w is None:
            keep2.append(r)
        else:
            words[w] = words.get(w, 0) + 1
    funnel["L2_excluded_words"] = {w: int(words.get(w, 0))
                                   for w in (CFG_O.get("name_exclude") or [])}
    funnel["L2_after"] = len(keep2)
    _stage("L2", len(keep2), len(pool) - len(keep2),
           "名称排除 联接/可转债/转债/增强/双债/混合一二级")

    # ---- L3 申购状态 + 日限额 ---------------------------------------------
    ok_status = set(CFG_O.get("purchase_status_ok") or [])
    min_day_limit = float(CFG_O.get("min_day_limit") or 0.0)
    keep3 = []
    n_status = n_limit = 0
    for r in keep2:
        if r["purchase_status"] not in ok_status:
            n_status += 1
            _mark("申购状态不可买")
            continue
        dl = m.f(r.get("day_limit"))
        if dl is not None and 0 < dl < min_day_limit:     # 0/空 = 不限，放行
            n_limit += 1
            _mark("日限额过小")
            continue
        keep3.append(r)
    funnel.update({"L3_status_dropped": n_status, "L3_day_limit_dropped": n_limit,
                   "L3_after": len(keep3)})
    _stage("L3", len(keep3), n_status + n_limit,
           "申购状态 ∈ 开放申购/限大额（限大额可买）；日限额不能太小")

    # ---- L4 购买起点 + 申购费 ---------------------------------------------
    max_min = float(CFG_O.get("max_min_purchase") or 0.0)
    max_fee = float(CFG_O.get("max_fee_pct") or 0.0)
    keep4 = []
    n_start = n_fee_unknown = n_fee_high = 0
    for r in keep3:
        start = m.f(r.get("min_purchase"))
        if start is None or start > max_min:
            n_start += 1
            _mark("起购金额过高或缺失")
            continue
        fee = m.f(r.get("fee_pct"))
        if fee is None:            # fail-closed：费用都不知道就不进池（不按 0 冒充免费）
            n_fee_unknown += 1
            _mark("申购费未知")
            continue
        if fee > max_fee:
            n_fee_high += 1
            _mark("申购费过高")
            continue
        r["share_key"], r["share_class"] = share_split(r["name"])
        keep4.append(r)
    funnel.update({"L4_min_purchase_dropped": n_start, "L4_fee_unknown": n_fee_unknown,
                   "L4_fee_too_high": n_fee_high, "L4_after": len(keep4)})
    _stage("L4", len(keep4), n_start + n_fee_unknown + n_fee_high,
           f"起购 ≤{max_min:.0f} 元；申购费（折后）≤{max_fee}%")

    # ---- L5 逐只历史：收益/回撤 -------------------------------------------
    tested = keep4[: int(limit)] if limit else keep4
    got: dict[str, tuple] = {}
    if tested:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(_nav_metrics_safe, r["code"]): r["code"] for r in tested}
            done = 0
            for fut in as_completed(futs):
                code, met, err = fut.result()
                got[code] = (met, err)
                done += 1
                if verbose and done % 200 == 0:
                    print(f"[otc] 净值进度 {done}/{len(tested)}")
    min_hist = int(CFG_O.get("min_hist_days") or 0)
    rows5: list[dict] = []
    n_nav = n_hist = n_dd = 0
    err_samples: list[str] = []
    for r in tested:
        met, err = got.get(r["code"], (None, None))
        if not met or not met.get("ok"):
            n_nav += 1
            _mark("净值取数失败")
            if err:
                err_samples.append(f"{r['code']} {err[:60]}")
            continue
        if met["hist_days"] < min_hist:
            n_hist += 1
            _mark("历史不足 3 年")
            continue
        lim = dd_limit(r["ftype"])
        if met.get("dd_3y") is None:
            n_dd += 1
            _mark("3 年回撤取不到")
            continue
        if met["dd_3y"] < lim:
            n_dd += 1
            _mark("3 年回撤过深")
            continue
        rows5.append({**r, **met})
    funnel.update({"L5_nav_failed": n_nav, "L5_hist_short": n_hist, "L5_dd_deep": n_dd,
                   "L5_after": len(rows5),
                   "L5_error_samples": sorted(err_samples)[:5]})
    _stage("L5", len(rows5), n_nav + n_hist + n_dd,
           f"历史 ≥{min_hist} 交易日；3 年回撤 ≥ 分类型阈值（−5%/长债信用债 −10%）")

    # ---- L6 份额类别去重（同基金 A/C/E/I 只留一只）-------------------------
    prefer = str(CFG_O.get("share_class_prefer") or "C")

    def _rank(r: dict):
        fee = m.f(r.get("fee_pct"), 9.9)
        return (0 if r["share_class"] == prefer else 1, float(fee), r["code"])

    best: dict[str, dict] = {}
    for r in sorted(rows5, key=_rank):      # 先按偏好排序再 setdefault → 结果确定
        best.setdefault(r["share_key"], r)
    pool6 = [best[k] for k in sorted(best)]
    funnel.update({"L6_dup_dropped": len(rows5) - len(pool6), "L6_after": len(pool6)})
    _stage("L6", len(pool6), len(rows5) - len(pool6),
           f"同一基金多份额只留一只（优先 {prefer} 类）")

    # ---- L7 规模补查（best-effort）----------------------------------------
    min_scale = float(CFG_O.get("min_scale_yi") or 0.0)
    scales: dict[str, float | None] = {}
    codes6 = [r["code"] for r in pool6]
    if codes6:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for code, val in zip(codes6, ex.map(scale_of, codes6)):
                scales[code] = val
    pool7 = []
    n_scale_na = n_scale_small = 0
    for r in pool6:
        sc = scales.get(r["code"])
        r["scale_yi"] = sc
        if sc is None:
            n_scale_na += 1
        elif sc < min_scale:
            n_scale_small += 1
            _mark("规模 <1 亿")
            continue
        pool7.append(r)
    funnel.update({"L7_scale_unavailable": n_scale_na, "L7_scale_small": n_scale_small,
                   "L7_after": len(pool7)})
    _stage("L7", len(pool7), n_scale_small,
           f"规模 ≥{min_scale:g} 亿（查不到不淘汰，只记 scale_unavailable）")

    # ---- 费率 + 打分 + 等级（只对 L7 幸存者取，省掉一大批请求）-------------
    fees: dict[str, dict] = {}
    fee_err = 0
    codes7 = [r["code"] for r in pool7]
    if codes7:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for code, parts, err in ex.map(_fee_parts_safe, codes7):
                if parts is None:
                    fee_err += 1
                    continue
                fees[code] = parts

    daily = nav_daily_map()
    rows: list[dict] = []
    dropped_fee = 0
    for r in pool7:
        f = fees.get(r["code"])
        if f is None:                     # 费率取不到 → 成本算不准 → 不进池（fail-closed）
            dropped_fee += 1
            _mark("费率取数失败")
            continue
        c1 = cost_1y(r.get("fee_pct"), f.get("op_fee"), f.get("redeem_fee_hold"))
        tmp = {**r, "cost_1y": c1}
        tmp["score"] = score_of(tmp)
        lv = level_of(r["ftype"], r["name"], r)
        dm = daily.get(r["code"]) or {}
        rows.append({
            "code": r["code"], "name": r["name"], "ftype": r["ftype"],
            "nav_date": dm.get("nav_date") or r.get("nav_date"),
            "unit_nav": dm.get("unit_nav"), "acc_nav": r.get("acc_nav"),
            "purchase_status": r["purchase_status"],
            "min_purchase": m.f(r.get("min_purchase")),
            "day_limit": m.f(r.get("day_limit")),
            "fee": m.f(r.get("fee_pct")),
            "hist_days": r["hist_days"],
            "ret_1y": m.f(r.get("ret_1y")), "ret_3y": m.f(r.get("ret_3y")),
            "dd_1y": m.f(r.get("dd_1y")), "dd_3y": m.f(r.get("dd_3y")),
            "vol_3y": m.f(r.get("vol_3y")),
            "mgmt_fee": f.get("mgmt_fee"), "redeem_fee_hold": f.get("redeem_fee_hold"),
            "cost_1y": c1, "score": tmp["score"],
            "level": lv["level"], "level_reason": lv["reason"],
            "key": r["share_key"], "share_class": r["share_class"],
        })
    _mark_n = fail.get("费率取数失败", 0)
    funnel["L8_fee_failed"] = int(_mark_n)     # 与 L7 之后并列，不算独立层
    funnel["final"] = len(rows)
    _stage("final", len(rows), dropped_fee + fee_err, "费率取数失败（fail-closed）")

    rows.sort(key=lambda x: (-(x["score"] or 0.0), x["code"]))   # 同分按代码，保证确定性
    funnel["fail_reasons"] = {k: int(fail[k]) for k in sorted(fail)}
    funnel["stages"] = stages
    funnel["elapsed_sec"] = round(time.time() - t0, 1)
    # 注意：generated_at 是唯一的时间戳，determinism.py 的 VOLATILE_KEYS 会剔除它
    funnel["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    funnel["cfg"] = {k: CFG_O[k] for k in
                     ("ftypes", "purchase_status_ok", "min_day_limit", "max_min_purchase",
                      "max_fee_pct", "min_hist_days", "max_dd_3y_by_ftype", "min_scale_yi",
                      "share_class_prefer", "score", "score_range")}

    df = pd.DataFrame(rows, columns=_COLS)
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, "stage1_otc.csv"), index=False,
              encoding="utf-8-sig", float_format="%.4f")
    with open(os.path.join(OUT, "stage1_otc_funnel.json"), "w", encoding="utf-8") as f:
        json.dump(funnel, f, ensure_ascii=False, indent=2)
    ds.dump_provenance("provenance_otc")

    if verbose:
        for s in stages:
            print(f"[otc] {s['layer']:<5} 剩 {s['left']:>5}  淘汰 {s['dropped']:>5}  {s['reason']}")
        print(f"[otc] 合格 {len(df)} 只，耗时 {funnel['elapsed_sec']}s")
        for r in rows[:10]:
            print(f"  {r['code']} {r['name']} {r['ftype']} 1年 {r['ret_1y']} "
                  f"回撤 {r['dd_1y']} 成本 {r['cost_1y']} 分 {r['score']} {r['level']}")
    return df, funnel


def main():
    ap = argparse.ArgumentParser(description="R2 场外债券基金层（OTC）筛选")
    ap.add_argument("--limit", type=int, default=None,
                    help="只取前 N 只进 L5（调试用；L5 之后的数是部分值）")
    ap.add_argument("--workers", type=int, default=6, help="逐只取数的线程数（默认 6）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    df, funnel = run_screen(limit=args.limit, verbose=not args.quiet,
                            workers=args.workers)
    keys = ("L0_total", "L1_after", "L2_after", "L3_after", "L4_after",
            "L5_after", "L6_after", "L7_after", "final")
    print(json.dumps({k: funnel.get(k) for k in keys}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
