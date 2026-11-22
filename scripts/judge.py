# -*- coding: utf-8 -*-
"""阶段二：精确判断。

设计原则（来自对六个开源项目的原文调研）：
1. **可复现**：同一份数据、同一天运行，必然得到同样的分数与结论。
2. **可证伪**：每只股票必须给出「什么事实成立就说明我错了」（falsifier），
   给不出可证伪条件的论点一律不算论点。
3. **绝对价位**：只输出具体价格（安全边际买入价/目标价/计划止损占位价），不输出「偏低」「有空间」这类形容词。
   注意：这里的「止损价」是**计划占位价**（安全边际买入价 ×0.85）；成交后的硬止损由
   `paper.apply_fill` 按**实际成交成本 ×0.85** 重设，两者不要混用。
4. **一票否决优先于打分**：先过否决项，再谈分数——避免用高分掩盖致命缺陷。
5. **数据不足即拒绝判断**：完整性 < 阈值直接标「数据不足」，不许用推断补齐。

输出：
  out/stage2_judged.csv     每只一行，便于排序筛选
  out/stage2_detail.json    每只的完整证据与论点（给 AI 或人复核）
  out/stage2_report.md      可读版判断卡
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import pandas as pd

import config
import datasource as ds
import metrics as m

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = config.OUT_DIR


# ------------------------------------------------------------------ 辅助
def _pct(x, digits=2):
    return None if x is None else round(x * 100, digits)


def _r(x, digits=2):
    return None if x is None else round(float(x), digits)


def industry_pe_median(snap: pd.DataFrame, industry: str) -> float | None:
    """同行业 PE 中位数（剔除亏损与异常值）。"""
    g = snap[(snap["industry"] == industry) & (snap["per"] > 0) & (snap["per"] < 150)]
    if len(g) < 5:
        return None
    return float(g["per"].median())


def bench_excess(daily: pd.DataFrame, bench: pd.DataFrame, days: int = 250) -> float | None:
    """近 N 个交易日相对沪深300的超额收益。"""
    c = pd.to_numeric(daily["close"], errors="coerce").dropna().values
    if len(c) <= days:
        return None
    b = pd.to_numeric(bench["close"], errors="coerce").dropna().values
    if len(b) <= days:
        return None
    return float(c[-1] / c[-days - 1] - 1.0) - float(b[-1] / b[-days - 1] - 1.0)


# ------------------------------------------------------------------ 数据装配
def load_bundle(code: str) -> dict:
    """取齐一只股票判断所需的全部数据。任何一项缺失都记录，不静默。"""
    b: dict = {"code": code}
    b["daily"] = ds.fetch_daily(code)
    b["fin"] = ds.fetch_fin(code)
    try:
        b["pe"] = ds.fetch_valuation(code, "市盈率(TTM)")
    except Exception as e:                                    # noqa: BLE001
        b["pe"] = None
        b["pe_error"] = f"{type(e).__name__}: {e}"
    try:
        b["pb"] = ds.fetch_valuation(code, "市净率")
    except Exception as e:                                    # noqa: BLE001
        b["pb"] = None
        b["pb_error"] = f"{type(e).__name__}: {e}"
    try:
        b["bench"] = ds.fetch_index_daily("000300")
    except Exception:                                         # noqa: BLE001
        b["bench"] = None
    return b


def compute(b: dict, snap: pd.DataFrame) -> dict:
    """把原始数据算成指标字典。"""
    daily, fin = b["daily"], b["fin"]
    ann = m.annual_rows(fin)
    latest = m.latest_row(fin)

    close = pd.to_numeric(daily["close"], errors="coerce").dropna()
    price = float(close.iloc[-1])

    roes = [m.roe(ann.iloc[i]) for i in range(len(ann))]
    roes = [x for x in roes if x is not None]
    last5 = roes[-5:]

    out: dict = {
        "price": _r(price, 3),
        "as_of": str(pd.to_datetime(daily["date"]).iloc[-1].date()),
        "roe_list": [_r(x) for x in last5],
        "roe_avg5": _r(m.mean_ignore_none(last5)),
        "roe_min5": _r(min(last5) if last5 else None),
        "gross_margin": _r(m.gross_margin(latest)),
        "debt_ratio": _r(m.debt_ratio(latest)),
        "ocf_ni_3y": _r(m.mean_ignore_none([m.ocf_to_ni(ann.iloc[i])
                                            for i in range(max(0, len(ann) - 3), len(ann))])),
    }

    # 成长：用连续 3 年同比增长率还原 CAGR
    g_rev = [m.rev_growth(ann.iloc[i]) for i in range(max(0, len(ann) - 3), len(ann))]
    g_pro = [m.profit_growth(ann.iloc[i]) for i in range(max(0, len(ann) - 3), len(ann))]
    out["rev_cagr3"] = _r(m.chain_cagr(g_rev), 4)
    out["profit_cagr3"] = _r(m.chain_cagr(g_pro), 4)
    out["rev_yoy_latest"] = _r(m.rev_growth(latest), 4)
    out["profit_yoy_latest"] = _r(m.profit_growth(latest), 4)

    # 价格风险
    out["max_dd_3y"] = _r(m.max_drawdown(close), 4)
    out["vol_3y"] = _r(m.volatility(close), 4)
    out["ma200"] = _r(m.ma(close, 200), 3)
    out["atr14"] = _r(m.atr(daily, 14), 4)
    out["atr_pct"] = _r((out["atr14"] / price) if out["atr14"] else None, 4)
    out["excess_250"] = _r(bench_excess(daily, b["bench"]) if b["bench"] is not None else None, 4)
    out["price_vs_ma200"] = _r(price / out["ma200"] - 1.0, 4) if out["ma200"] else None

    # 估值分位
    def q(series, p):
        if series is None:
            return None
        s = pd.to_numeric(series, errors="coerce").dropna()
        s = s[s > 0]
        return float(s.quantile(p)) if len(s) >= 60 else None

    pe_med = q(b["pe"], 0.5)
    def _last(series):
        if series is None:
            return None
        s = pd.to_numeric(series, errors="coerce").dropna()
        return float(s.iloc[-1]) if len(s) else None

    out["pe_cur"] = _r(_last(b["pe"]))
    out["pb_cur"] = _r(_last(b["pb"]))
    out["pe_p25"] = _r(q(b["pe"], 0.25))
    out["pe_p50"] = _r(pe_med)
    out["pb_p25"] = _r(q(b["pb"], 0.25))
    out["pb_p50"] = _r(q(b["pb"], 0.5))
    out["pe_pct_own"] = _r(m.pct_rank(b["pe"], out["pe_cur"]), 4)
    out["pb_pct_own"] = _r(m.pct_rank(b["pb"], out["pb_cur"]), 4)
    return out


# ------------------------------------------------------------------ 否决与打分
KEY_METRICS = ["roe_avg5", "gross_margin", "ocf_ni_3y", "debt_ratio", "rev_cagr3",
               "profit_cagr3", "pe_cur", "pb_cur", "pe_pct_own", "pb_pct_own",
               "max_dd_3y", "vol_3y"]


def check_veto(x: dict, is_financial: bool) -> list[dict]:
    """一票否决。每条都写清「触发了什么、实测值多少、门槛多少」。"""
    v = []
    roes = [r for r in (x.get("roe_list") or []) if r is not None]
    low = [r for r in roes if r < config.VETO["roe_low_level"]]
    if len(low) >= config.VETO["roe_low_years"]:
        v.append({"rule": "长期低ROE", "hit": True,
                  "evidence": f"近{len(roes)}个年报中有{len(low)}年ROE<{config.VETO['roe_low_level']}%，"
                              f"最低{min(low):.2f}%",
                  "threshold": f"≥{config.VETO['roe_low_level']}% 且最多1年低于此线"})

    ocf = x.get("ocf_ni_3y")
    if ocf is None:
        # 缺数据不能当「没触发」：原来 `if ocf is not None and ...` 让数据缺失直接放行，
        # 实测缺 OCF + 缺负债率仍 completeness=0.833>0.8、veto=[] → 进了买入区（2026-10-30 审计 #9）。
        v.append({"rule": "利润不落袋（数据缺失）", "hit": True,
                  "evidence": "近3年经营现金流/净利润取不到（fail-closed：不买看不懂的）",
                  "threshold": f"≥{config.VETO['ocf_ni_floor']} 且必须有数据"})
    elif ocf < config.VETO["ocf_ni_floor"]:
        v.append({"rule": "利润不落袋", "hit": True,
                  "evidence": f"近3年平均经营现金流/净利润={ocf:.2f}（<{config.VETO['ocf_ni_floor']}）",
                  "threshold": f"≥{config.VETO['ocf_ni_floor']}"})

    debt = x.get("debt_ratio")
    if debt is None and not is_financial:
        v.append({"rule": "负债率缺失（数据缺失）", "hit": True,
                  "evidence": "非金融企业取不到资产负债率（fail-closed）",
                  "threshold": f"非金融企业 <{config.VETO['debt_hard']}% 且必须有数据"})
    elif debt is not None and debt > config.VETO["debt_hard"] and not is_financial:
        v.append({"rule": "负债率过高", "hit": True,
                  "evidence": f"资产负债率={debt:.2f}%（>{config.VETO['debt_hard']}）",
                  "threshold": f"非金融企业 <{config.VETO['debt_hard']}%"})

    pe = x.get("pe_cur")
    if pe is None or pe <= 0:
        v.append({"rule": "无法估值", "hit": True,
                  "evidence": f"当前PE={pe}（亏损或接口缺失）",
                  # 只在 PE≤0/缺失时触发；「历史点 <60」不构成否决（分位计算里单独降级）
                  "threshold": "PE 必须为正（PE≤0 或取不到即否决）"})
    return v


def completeness(x: dict) -> float:
    have = sum(1 for k in KEY_METRICS if x.get(k) is not None)
    return round(have / len(KEY_METRICS), 4)


def score(x: dict, ind_pe: float | None, buy_price: float | None) -> dict:
    """四维打分，各 25 分。所有分项都基于已计算的数字，无主观填空。"""
    fin = bool(x.get("is_financial"))
    s = {}

    # 质量 25
    q_roe = m.score_linear(x["roe_avg5"], 8, 25, 10)
    q_stab = m.score_linear(x["roe_min5"], 0, 15, 5)
    q_gm = (2.5 if fin else m.score_linear(x["gross_margin"], 10, 60, 5))
    q_ocf = m.score_linear(x["ocf_ni_3y"], 0.5, 1.5, 5)
    s["quality"] = round(q_roe + q_stab + q_gm + q_ocf, 2)
    s["quality_parts"] = {"roe水平": round(q_roe, 2), "roe稳定性": round(q_stab, 2),
                          "毛利率": round(q_gm, 2), "现金含量": round(q_ocf, 2)}

    # 成长 25
    g_rev = m.score_linear(x["rev_cagr3"], -0.05, 0.25, 12)
    g_pro = m.score_linear(x["profit_cagr3"], -0.05, 0.30, 8)
    g_now = m.score_linear(x["rev_yoy_latest"], -0.10, 0.30, 5)
    s["growth"] = round(g_rev + g_pro + g_now, 2)
    s["growth_parts"] = {"营收CAGR3": round(g_rev, 2), "利润CAGR3": round(g_pro, 2),
                         "最新营收增速": round(g_now, 2)}

    # 估值 25
    v_pe = m.score_inverse(x["pe_pct_own"], 0.10, 0.80, 10)
    v_pb = m.score_inverse(x["pb_pct_own"], 0.10, 0.85, 5)
    ratio = (x["pe_cur"] / ind_pe) if (ind_pe and x.get("pe_cur")) else None
    v_ind = m.score_inverse(ratio, 0.80, 1.50, 5)
    v_safe = (m.score_inverse(x["price"] / buy_price, 0.90, 1.50, 5)
              if buy_price else 0.0)
    s["valuation"] = round(v_pe + v_pb + v_ind + v_safe, 2)
    s["valuation_parts"] = {"自身PE分位": round(v_pe, 2), "自身PB分位": round(v_pb, 2),
                            "相对行业": round(v_ind, 2), "相对安全边际价": round(v_safe, 2)}

    # 风险 25（分数越高＝风险越低）
    r_dd = m.score_inverse(abs(x["max_dd_3y"]) if x["max_dd_3y"] is not None else None,
                           0.20, 0.60, 8)
    r_vol = m.score_inverse(x["vol_3y"], 0.20, 0.55, 6)
    r_trend = m.score_linear(x["price_vs_ma200"], -0.20, 0.20, 5)
    r_atr = m.score_inverse(x["atr_pct"], 0.015, 0.050, 6)
    s["risk"] = round(r_dd + r_vol + r_trend + r_atr, 2)
    s["risk_parts"] = {"回撤控制": round(r_dd, 2), "波动率": round(r_vol, 2),
                       "趋势位置": round(r_trend, 2), "ATR波幅": round(r_atr, 2)}

    s["total"] = round(s["quality"] + s["growth"] + s["valuation"] + s["risk"], 2)
    return s


# ------------------------------------------------------------------ 定价
def price_it(x: dict, ind_pe: float | None) -> dict:
    """把估值换算成绝对价位：安全边际价、目标价、计划止损占位价。

    `stop_price` 只是「安全边际买入价 ×0.85」的**计划占位价**（写进下单/括号单参考）；
    成交后由 `paper.apply_fill` 按「实际成交成本 ×0.85」重设，加仓不重设。
    """
    price, pe = x.get("price"), x.get("pe_cur")
    pe_p25, pe_p50 = x.get("pe_p25"), x.get("pe_p50")
    out = {"eps_ttm": None, "fair_pe": None, "target_price": None,
           "buy_price": None, "stop_price": None, "upside_to_target": None}
    if not (price and pe and pe > 0):
        return out

    eps = price / pe
    out["eps_ttm"] = _r(eps, 4)

    cands = [v for v in (pe_p50, ind_pe) if v and v > 0]
    fair = min(cands) if cands else pe
    if pe_p25:
        fair = max(fair, pe_p25)          # 合理估值不应低于自身 5 年 1/4 分位
    out["fair_pe"] = _r(fair, 4)
    out["target_price"] = _r(eps * fair, 2)

    # 安全边际价 = 自身PE 25分位 × EPS × 0.7（百分位法 + 三成折扣）
    if pe_p25:
        out["buy_price"] = _r(eps * pe_p25 * 0.70, 2)
    else:
        out["buy_price"] = _r(eps * fair * 0.70, 2)

    # 这里算的是「计划止损占位价」= 安全边际买入价 ×0.85，用于下单/括号单参考。
    # 真正的初始硬止损 = 实际成交成本 ×0.85，由 paper.apply_fill 在成交时写入。
    # 不用 ATR 做第一道防线：低波动股的 2.5×ATR 只有 5% 左右，等于把正常波动当成破位，
    # 会被噪音反复扫出局。ATR 跟踪止损留到有浮盈之后再启用（见 trail_rule）。
    if out["buy_price"]:
        out["stop_price"] = _r(out["buy_price"] * (1 + config.EXEC["stop_loss_pct"]), 2)
        out["trail_rule"] = (f"浮盈超过 10% 后，止损上移到「现价 − {config.EXEC['atr_mult']}×ATR(14)」，"
                             f"只上移、绝不下移")
        if out["target_price"]:
            out["upside_to_target"] = _r(out["target_price"] / out["buy_price"] - 1.0, 4)
    return out


# ------------------------------------------------------------------ 论点
def build_thesis(x: dict, s: dict, pr: dict, verdict: str) -> dict:
    """把数字翻译成可证伪的论点。每一条都必须能对应到一个具体数字或事件。"""
    name = x["name"]
    pillars, risks, catalysts, falsifiers = [], [], [], []

    if x.get("roe_avg5") is not None:
        pillars.append(f"近5年平均ROE {x['roe_avg5']:.1f}%（最低 {x['roe_min5']:.1f}%），"
                       f"说明赚的是真钱而不是账面利润")
    if x.get("ocf_ni_3y") is not None and x["ocf_ni_3y"] >= 1.0:
        pillars.append(f"近3年经营现金流/净利润 = {x['ocf_ni_3y']:.2f}，利润有现金支撑")
    if x.get("profit_cagr3") is not None and x["profit_cagr3"] > 0.10:
        pillars.append(f"近3年净利润复合增速 {x['profit_cagr3'] * 100:.0f}%，成长可验证")
    if x.get("pe_pct_own") is not None and x["pe_pct_own"] <= 0.35:
        pillars.append(f"当前PE处于自身近5年 {x['pe_pct_own'] * 100:.0f}% 分位，估值在历史低位")

    if x.get("roe_min5") is not None and x["roe_min5"] < 12:
        risks.append(f"ROE 曾掉到 {x['roe_min5']:.1f}%，盈利能力并不稳定，"
                     f"若再出现一次就推翻「优质」这个前提")
    if x.get("max_dd_3y") is not None:
        risks.append(f"近3年最大回撤 {x['max_dd_3y'] * 100:.0f}%，"
                     f"持有过程可能先亏掉这个数，这是必须预先接受的成本")
    if x.get("debt_ratio") is not None and x["debt_ratio"] > 55:
        risks.append(f"资产负债率 {x['debt_ratio']:.1f}% 偏高，利率或需求波动会放大业绩波动")
    if x.get("rev_yoy_latest") is not None and x["rev_yoy_latest"] < 0:
        risks.append(f"最新报告期营收同比 {x['rev_yoy_latest'] * 100:.1f}%，需求端已经转负")
    if not risks:
        risks.append("未识别出明显风险——这本身是危险信号，可能意味着数据覆盖不足")

    catalysts.append("下一期财报中 ROE 与经营现金流同时不低于当前水平")
    if pr.get("target_price"):
        catalysts.append(f"估值回到自身5年中位 PE {pr.get('fair_pe')} 倍时，对应股价约 {pr['target_price']} 元")

    falsifiers.append(f"若任一年报 ROE 跌破 {config.VETO['roe_low_level']}%（连续两年即触发否决）")
    falsifiers.append(f"若经营现金流/净利润连续两年低于 {config.VETO['ocf_ni_floor']}")
    if pr.get("stop_price"):
        falsifiers.append(f"若买入后收盘价跌破硬止损（实际成交成本 ×0.85；按安全边际价 {pr['buy_price']} 元的"
                          f"计划占位约 {pr['stop_price']} 元），说明买入逻辑已破，无条件离场")

    statement = (f"{name}（{x['code']}）当前价 {x['price']} 元、PE {x.get('pe_cur')}、"
                 f"PE历史分位 {_pct(x.get('pe_pct_own'), 0)}%，"
                 f"综合判断为「{verdict}」。")
    if pr.get("buy_price"):
        statement += (f"安全边际买入价 {pr['buy_price']} 元，目标价 {pr['target_price']} 元，"
                      f"计划止损占位价 {pr['stop_price']} 元（成交后按实际成本 ×0.85 重设）。")

    return {"statement": statement, "pillars": pillars[:5], "risks": risks[:5],
            "catalysts": catalysts[:3], "falsifiers": falsifiers[:4],
            "stop_loss_trigger": (f"收盘价跌破硬止损（实际成交成本×0.85；计划占位 {pr['stop_price']} 元）"
                                  if pr.get("stop_price") else "无（缺价格数据）"),
            "review_frequency": "每季度财报后强制复核一次"}


# ------------------------------------------------------------------ 单只判断
def judge_one(row: dict, snap: pd.DataFrame) -> dict:
    code, name = str(row["code"]).zfill(6), str(row["name"])
    industry = str(row.get("industry") or "")
    try:
        bundle = load_bundle(code)
        x = compute(bundle, snap)
    except Exception as e:                                    # noqa: BLE001
        return {"code": code, "name": name, "industry": industry,
                "error": f"{type(e).__name__}: {e}", "verdict": "数据不足",
                "is_financial": bool(row.get("is_financial")),
                "completeness": 0.0, "scores": {}, "veto": [], "pricing": {},
                "thesis": {"statement": f"{name}: 取数失败（{type(e).__name__}），拒绝判断",
                           "pillars": [], "risks": [str(e)], "catalysts": [], "falsifiers": []}}

    x["code"], x["name"], x["industry"] = code, name, industry
    x["is_financial"] = bool(row.get("is_financial"))
    x["mktcap_yi"] = _r(row.get("mktcap_yi"))
    x["avg_amt60_yi"] = _r(row.get("avg_amt60_yi"))

    ind_pe = industry_pe_median(snap, industry)
    veto = check_veto(x, x["is_financial"])
    comp = completeness(x)
    pr = price_it(x, ind_pe)
    sc = score(x, ind_pe, pr.get("buy_price"))

    if comp < config.VERDICT["min_data_completeness"]:
        verdict = "数据不足"
    elif veto:
        verdict = "回避"
    elif sc["total"] >= config.VERDICT["buy_total"]:
        verdict = "买入区"
    elif sc["total"] >= config.VERDICT["watch_total"]:
        verdict = "观察"
    else:
        verdict = "回避"

    thesis = build_thesis(x, sc, pr, verdict)
    return {"code": code, "name": name, "industry": industry,
            "is_financial": x["is_financial"], "completeness": comp,
            "scores": sc, "veto": veto, "pricing": pr, "metrics": x,
            "ind_pe_median": ind_pe, "verdict": verdict, "thesis": thesis,
            "data_notes": {"pe_error": bundle.get("pe_error"), "pb_error": bundle.get("pb_error")}}


# ------------------------------------------------------------------ 输出
def to_markdown(results: list[dict]) -> str:
    L = ["# 阶段二：精确判断结果", "",
         f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for r in results:
        L += [f"## {r['name']}（{r['code']}）— {r['verdict']}", ""]
        if r.get("error"):
            L += [f"> 取数失败：{r['error']}", ""]
            continue
        sc, pr, x = r["scores"], r["pricing"], r["metrics"]
        L += [f"- 行业：{r['industry']}　数据完整性：{r['completeness'] * 100:.0f}%",
              f"- 综合得分：**{sc['total']}** / 100 "
              f"（质量 {sc['quality']}｜成长 {sc['growth']}｜估值 {sc['valuation']}｜风险 {sc['risk']}）",
              f"- 现价 {x['price']} 元　PE {x.get('pe_cur')}（历史分位 {_pct(x.get('pe_pct_own'), 0)}%）"
              f"　PB {x.get('pb_cur')}（分位 {_pct(x.get('pb_pct_own'), 0)}%）",
              f"- 安全边际买入价 **{pr.get('buy_price')}** 元　目标价 **{pr.get('target_price')}** 元　"
              f"计划止损占位价 **{pr.get('stop_price')}** 元（成交后按实际成本 ×0.85 重设）", ""]
        if r["veto"]:
            L += ["**一票否决：**", ""]
            L += [f"- {v['rule']}：{v['evidence']}" for v in r["veto"]]
            L += [""]
        t = r["thesis"]
        L += [f"**论点**：{t['statement']}", "", "支持的支柱："]
        L += [f"- {p}" for p in t["pillars"]] or ["- （无）"]
        L += ["", "会推翻论点的事："]
        L += [f"- {p}" for p in t["risks"]]
        L += ["", "可证伪条件（出现即认错）："]
        L += [f"- {p}" for p in t["falsifiers"]]
        L += ["", "---", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="阶段二：对阶段一候选做精确判断")
    ap.add_argument("--input", default=os.path.join(OUT, "stage1_candidates.csv"))
    ap.add_argument("--limit", type=int, default=None, help="只判断前 N 只")
    ap.add_argument("--codes", default=None, help="指定代码，逗号分隔")
    args = ap.parse_args()

    snap = ds.fetch_market_snapshot()   # 先取快照：--codes 模式要靠它补齐名称/行业/市值/金融标志

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
        idx = snap.set_index("code")
        rows = []
        for c in codes:
            if c in idx.index:
                s = idx.loc[c]
                if isinstance(s, pd.DataFrame):        # 快照里若有重复代码，取第一行
                    s = s.iloc[0]
                name = str(s["name"])
                mc = s.get("mktcap_yi")               # 快照里已是「亿元」
                rows.append({
                    "code": c,
                    "name": name,
                    "industry": str(s["industry"]),
                    # 与 screen.py 完全一致的判定口径（按名称关键词，不是按行业字段）
                    "is_financial": any(k in name for k in config.FIN_INDUSTRY_KEYWORDS),
                    "mktcap_yi": float(mc) if (mc is not None and pd.notna(mc)) else None,
                })
            else:                                       # 快照里没有（停牌/退市/非全市场标的）
                rows.append({"code": c, "name": c, "industry": "未分类", "is_financial": False})
    else:
        df = pd.read_csv(args.input, dtype={"code": str})   # 必须 dtype=str，否则丢前导零
        df["code"] = df["code"].str.zfill(6)
        rows = df.to_dict("records")
        if args.limit:
            rows = rows[: args.limit]

    print(f"[阶段二] 待判断 {len(rows)} 只")
    if args.codes:
        print("[阶段二] 注意：--codes 跳过阶段一的池管理门槛（市值/流动性/上市年限/成长/估值分位），"
              "仅四道否决线与四维打分生效；汇报时须说明该股未经阶段一筛选")

    results = []
    for i, row in enumerate(rows, 1):
        r = judge_one(row, snap)
        results.append(r)
        flag = "!" if r.get("error") else " "
        print(f"  {flag}{i}/{len(rows)} {r['code']} {r['name']:<8} "
              f"{r['verdict']:<5} 总分={r['scores'].get('total', 0):>5}")

    os.makedirs(OUT, exist_ok=True)
    pd.DataFrame([{
        "code": r["code"], "name": r["name"], "industry": r["industry"],
        "verdict": r["verdict"], "total": r["scores"].get("total"),
        "quality": r["scores"].get("quality"), "growth": r["scores"].get("growth"),
        "valuation": r["scores"].get("valuation"), "risk": r["scores"].get("risk"),
        "price": r.get("metrics", {}).get("price"),
        "pe": r.get("metrics", {}).get("pe_cur"),
        "pe_pct_own": r.get("metrics", {}).get("pe_pct_own"),
        "buy_price": r.get("pricing", {}).get("buy_price"),
        "target_price": r.get("pricing", {}).get("target_price"),
        "stop_price": r.get("pricing", {}).get("stop_price"),
        "completeness": r["completeness"],
        "veto": ";".join(v["rule"] for v in r.get("veto", [])),
        "error": r.get("error", ""),
    } for r in results]).to_csv(os.path.join(OUT, "stage2_judged.csv"),
                                index=False, encoding="utf-8-sig")

    with open(os.path.join(OUT, "stage2_detail.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "results": results}, f, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT, "stage2_report.md"), "w", encoding="utf-8") as f:
        f.write(to_markdown(results))

    ds.dump_provenance("provenance_stage2")
    ok = [r for r in results if r["verdict"] == "买入区"]
    print(f"\n买入区 {len(ok)} 只 / 共 {len(results)} 只")
    print(f"输出: {os.path.join(OUT, 'stage2_judged.csv')}")
    print(f"      {os.path.join(OUT, 'stage2_report.md')}")


if __name__ == "__main__":
    main()
