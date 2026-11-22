# -*- coding: utf-8 -*-
"""风险等级（R1–R5）分类与组合评审。

口径（2026-10-26 用户纠正）：金融产品风险等级 R1（低）→ R5（高），衡量的是
「本金损失的可能性 + 净值波动幅度」；投资者风险承受能力 C1（谨慎）→ C5（激进）。
核心原则：**不能买风险等级高于自身承受能力的产品**。

等级按**产品类型/交易场所/板块**判定，不按自定义波动率阈值降级：
  R1 低风险   = 国债/银行存款/货币基金/银行现金管理类理财/逆回购 → 现金层；
  R2 中低风险 = 固定收益类理财/地方政府债/政策性金融债/高等级信用债 → 场内债券 ETF 代理；
  R3 中风险   = A 股**主板**股票（可转债、混合型基金、备兑期权不在本技能范围）；
  R4 中高风险 = 创业板/科创板/北交所股票、港股通、融资融券、个股期权买入、权益类基金…；
  R5 高风险   = 期货、期权、私募、分级基金、复杂结构化产品、场外衍生品。

另加一道**准入门槛**（资金/交易经验）：创业板 10 万元+24 个月、科创板 50 万元+24 个月、
北交所 50 万元+24 个月、港股通/融资融券/个股期权 50 万元——本部署 5 万本金全部不满足，
所以 R4 就算等级放开也开不了权限。

本模块只做四件事：个股按板块定级、场内基金按品种定级、准入门槛说明、组合评审。
不取数、不选股、不下单、不算钱——所以可以离线单测（见 _selftest_risk.py）。
"""
from __future__ import annotations

import re

import config

RISK = config.RISK
CFG_W = RISK["wealth"]
PROFILE = RISK.get("investor") or {}
QUALIFICATION = RISK.get("qualification") or {}
ETF_MAP = RISK.get("etf_map") or {}

ORDER = ["R1", "R2", "R3", "R4", "R5"]
NUM = {lv: i + 1 for i, lv in enumerate(ORDER)}     # R2 → 2、R3 → 3
LABEL = {lv: (RISK["levels"].get(lv) or {}).get("label", lv) for lv in ORDER}
INCLUDE = set(RISK.get("focus") or ["R2", "R3"])


# ---------------------------------------------------------------- 等级工具
def level_num(level: str) -> int | None:
    return NUM.get(str(level)) if level else None


def num_level(value: float) -> str:
    """加权等级数值 → 最接近的等级标签（用于展示，不参与判定）。"""
    return ORDER[min(max(int(round(value)), 1), 5) - 1]


def level_label(level: str) -> str:
    return f"{level} {LABEL.get(level, '')}" if level in LABEL else str(level or "未评级")


def included(level: str) -> bool:
    """该等级是否在技能的可投范围内（默认 R2/R3）。"""
    return str(level) in INCLUDE


def focus_order() -> list[str]:
    """焦点层（RISK["focus"]）的**定序**列表：按 ORDER 的固定阶梯排序。

    为什么不能 `list(INCLUDE)`：INCLUDE 是 set，字符串集合的遍历顺序随
    PYTHONHASHSEED 变化（同一输入两次运行 R2/R3 会互换）→ stage3_plan.json 的
    `focus` 数组不可复现，determinism.py 会随机判「不可复现」。输出一律走这里。
    """
    return [lv for lv in ORDER if lv in INCLUDE]


def investor_line() -> str:
    """投资者风险承受能力一行说明（报告/终端用）。"""
    lv = PROFILE.get("level", "C3")
    lab = PROFILE.get("label", "")
    allowed = "/".join(PROFILE.get("allowed_levels") or ["R2", "R3"])
    return f"风险承受能力 {lv}（{lab or '平衡型'}）：可买 R1–R3，R4/R5 不买（{allowed}）"


def qualification(kind: str) -> dict:
    """某类产品的准入门槛（资金/经验）。kind 见 config.RISK['qualification']。"""
    return dict(QUALIFICATION.get(kind) or {})


def access_ok(kind: str, capital: float) -> tuple[bool, str]:
    """按资金判断是否满足准入（交易经验视为已满足；不满足需人工确认）。

    返回 (ok, 说明)；说明里带门槛，便于报告里写清「为什么买不了」。
    """
    q = qualification(kind)
    need = float(q.get("min_capital") or 0)
    months = int(q.get("min_months") or 0)
    note = str(q.get("note") or "")
    if need <= 0:
        return True, note
    need_txt = f"{need / 10000:.0f} 万元" + (f"+{months} 个月经验" if months else "")
    if float(capital) >= need:
        return True, f"需 ≥{need_txt}，本金满足（交易经验/权限需自行确认；{note}）"
    return False, f"需 ≥{need_txt}，本金 {float(capital):,.0f} 元不满足（{note}）"


def _f(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        if v != v:            # NaN
            return default
        return v
    except (TypeError, ValueError):
        return default


def _vol_note(metrics: dict | None) -> str:
    """波动率只作提示，不改变等级（2026-10-26 用户纠正后的口径）。"""
    m = metrics or {}
    vol = _f(m.get("vol_3y"))
    dd = _f(m.get("max_dd_3y"))
    if vol is None and dd is None:
        return ""
    parts = []
    if vol is not None:
        parts.append(f"年化波动 {vol * 100:.1f}%")
    if dd is not None:
        parts.append(f"近 3 年回撤 {dd * 100:.1f}%")
    return "；" + "、".join(parts) + "（仅作风险提示，不改变等级）"


def _match_pats(pats, name: str) -> str | None:
    for p in pats:
        if re.search(p, name):
            return p
    return None


# ---------------------------------------------------------------- 个股：按板块定级
# 顺序即优先级：先判可转债，再判 R4 板块，最后主板 R3；认不出 → R5（fail-closed）
_BOARD_RULES = [
    (r"^(110|111|113|118|123|127|128)", "可转债", "R3",
     "可转债：R3 中风险（技能范围只做 A 股 + ETF，不买可转债）"),
    (r"^(688|689)", "科创板", "R4",
     "科创板股票：R4 中高风险（准入需 50 万元资产 + 24 个月交易经验）"),
    (r"^(300|301)", "创业板", "R4",
     "创业板股票：R4 中高风险（准入需 10 万元资产 + 24 个月交易经验）"),
    (r"^(43|83|87|88|92)", "北交所", "R4",
     "北交所股票：R4 中高风险（准入需 50 万元资产 + 24 个月交易经验）"),
    (r"^(600|601|603|605|000|001|002|003)", "主板", "R3",
     "沪深主板 A 股：R3 中风险（按板块定级，不按波动率调整）"),
]
_BOARD_TO_QUAL = {"主板": "主板股票", "创业板": "创业板", "科创板": "科创板",
                  "北交所": "北交所", "可转债": "可转债"}


def code_digits(code) -> str:
    """统一股票代码：去 sh/sz/bj 前缀、去 .SH 后缀，返回 6 位数字串。"""
    s = str(code or "").strip().lower()
    s = re.sub(r"^(sh|sz|bj)", "", s)
    s = s.split(".")[0]
    return s


def board_of(code) -> dict:
    """股票代码 → {board, level, reason}；认不出板块 → R5（fail-closed）。"""
    d = code_digits(code)
    if not d:
        return {"board": "未知", "level": "R5", "reason": "缺少股票代码，无法判定板块（fail-closed）"}
    for pat, board, lv, reason in _BOARD_RULES:
        if re.match(pat, d):
            return {"board": board, "level": lv, "reason": reason}
    return {"board": "未知", "level": "R5",
            "reason": f"代码 {d} 无法识别交易板块（fail-closed，不买）"}


def stock_level(code=None, name: str | None = None, metrics: dict | None = None) -> dict:
    """个股 → {level, reason, board, qualification}。

    等级只由**板块**决定（主板 R3；创业板/科创板/北交所 R4；可转债 R3）；
    ST/退市整理 → R5。波动率/回撤只写进提示，不改变等级。
    兼容旧调用：stock_level({"name": ..., "vol_3y": ...})。
    """
    if isinstance(code, dict):          # 兼容旧签名 stock_level(metrics)
        metrics = code
        code = (metrics or {}).get("code")
        name = name or (metrics or {}).get("name")
    m = metrics or {}
    nm = str(name if name is not None else (m.get("name") or ""))
    up = nm.upper()
    if "ST" in up or "退" in nm:
        return {"level": "R5", "board": "风险警示", "qualification": {},
                "reason": "ST/*ST/退市整理股票（风险警示）：R5，不做"}
    b = board_of(code if code is not None else m.get("code"))
    reason = b["reason"] + _vol_note(m)
    qkind = _BOARD_TO_QUAL.get(b["board"])
    return {"level": b["level"], "board": b["board"],
            "qualification": qualification(qkind) if qkind else {},
            "reason": reason}


# ---------------------------------------------------------------- 场内基金：按品种定级
_CROSS_WORDS = ["纳斯达克", "纳指", "标普", "道琼斯", "美国", "港股", "恒生", "中概", "香港",
                "日经", "日本", "德国", "法国", "欧洲", "英国", "东南亚", "沙特", "印度", "越南",
                "国际"]
_COMMODITY_WORDS = ["黄金", "白银", "上海金", "金ETF", "豆粕", "原油", "商品", "有色期货",
                    "国债期货", "能源化工", "期货"]
_LEVER_WORDS = ["杠杆", "双倍", "两倍", "三倍", "2倍", "3倍", "分级"]
_MONEY_WORDS = ["货币", "现金", "添益", "日利", "保证金", "理财", "快线", "活期宝"]
_CONVERT_WORDS = ["可转债", "转债"]


def _etf_reason(lv: str, why: str, metrics: dict | None = None) -> dict:
    return {"level": lv, "reason": f"{why}{_vol_note(metrics)}"}


def etf_level(name: str, cls: str | None = None, metrics: dict | None = None) -> dict:
    """场内基金 → {level, reason}。

    判定顺序（fail-closed，认不出就是 R5 不买）：
      1. 杠杆/分级 → R5；商品/期货 → R5
      2. 货币/现金 → R1（只做现金层）
      3. 可转债/转债 → R3（与可转债同属 R3；策略不纳入）
      4. 跨境（港/美/日/欧等）→ R4
      5. 债券类（-债/-国债/-政金债…且非股债混合）→ R2
      6. 跟踪创业板/科创板/北交所指数的基金 → R4
      7. 权益类：行业 → R4；宽基/策略 → config.RISK['etf_map']['equity_etf_level']
         （默认 3，是登记过的例外；填 4 则严格执行「权益类基金 = R4」）
      8. 认不出 → R5
    """
    nm = str(name or "")
    m = metrics or {}
    if any(w in nm for w in _LEVER_WORDS):
        return _etf_reason("R5", "杠杆/分级基金：R5 高风险，不做", m)
    if any(w in nm for w in _COMMODITY_WORDS):
        return _etf_reason("R5", "商品/期货类 ETF：R5 高风险，不做", m)
    if any(w in nm for w in _MONEY_WORDS):
        return _etf_reason("R1", "货币/现金管理类 ETF：R1 低风险，只做现金层", m)
    if any(w in nm for w in _CONVERT_WORDS):
        return _etf_reason("R3", "可转债/转债 ETF：R3 中风险（策略不纳入）", m)
    if any(w in nm for w in _CROSS_WORDS):
        return _etf_reason("R4", "跨境权益类 ETF：R4 中高风险，不做", m)
    bond = _match_pats(CFG_W.get("patterns") or [], nm)
    if bond and not _match_pats(CFG_W.get("exclude_patterns") or [], nm):
        return _etf_reason("R2", f"场内债券 ETF（命中「{bond}」）：R2 中低风险，作为理财产品层", m)
    if any(w in nm for w in ("科创", "创业板", "双创", "北证")):
        return _etf_reason("R4", "跟踪创业板/科创板/北交所指数的基金：R4 中高风险，不做", m)
    if cls == "行业":
        return _etf_reason("R4", "行业/主题 ETF：R4 中高风险（单一行业押注），不做", m)
    if cls in ("宽基", "策略"):
        try:
            lvl = int(ETF_MAP.get("equity_etf_level", 3) or 3)
        except (TypeError, ValueError):
            lvl = 3
        if lvl <= 3:
            return _etf_reason("R3", f"{cls} ETF：R3 中风险（登记例外：宽基/策略 ETF 按"
                                      f"分散型权益工具评 R3，无杠杆、无单一行业押注）", m)
        return _etf_reason("R4", f"{cls} ETF：R4 中高风险（权益类基金按标准 R4），不做", m)
    return _etf_reason("R5", "认不出标的类型（fail-closed，不买）", m)


# ---------------------------------------------------------------- 组合评审
def portfolio_review(items: list[dict]) -> dict:
    """组合级评审。

    items: [{code, name, level, weight}]，**weight = 占本金的比例**（不是占已投比例），
    所以单只上限可以直接和 levels[*].single_cap（占本金）比较。
    返回：
      ok         布尔（无 ERROR 级违规）
      level      加权等级（按占比）
      weights    {等级: 占已投比例}
      r2_share   R2 占已投比例
      violations ERROR 级（必须阻止新订单）
      warnings   WARN 级（只在报告里提示）
    """
    p = RISK["portfolio"]
    violations, warnings = [], []
    wcap = {}
    for it in items:
        lv = str(it.get("level") or "")
        # 权重必须是可解析的有限数：原来 `_f(...,0.0) or 0.0` 把 NaN/缺字段都当 0，
        # 单票上限校验被直接绕过（2026-10-30 审计 #8：ok=True、violations=[]）。
        _w = _f(it.get("weight"), None)
        if _w is None or _w != _w:
            violations.append(f"{it.get('code')} {it.get('name')}：权重缺失或非数值（fail-closed）")
            continue
        w = max(0.0, float(_w))
        if not level_num(lv):
            violations.append(f"{it.get('code')} {it.get('name')}：没有风险等级（fail-closed）")
            continue
        if not included(lv):
            violations.append(f"{it.get('code')} {it.get('name')}：{level_label(lv)} 不在 R2/R3 范围内")
        cap = float((RISK["levels"].get(lv) or {}).get("single_cap") or 0.10)
        tol = float(p.get("single_level_tol") or 0.0)
        if w > cap + tol:
            violations.append(f"{it.get('code')} {it.get('name')}：单只占本金 {w * 100:.1f}% "
                              f"> {lv} 上限 {cap * 100:.0f}%")
        wcap[lv] = round(wcap.get(lv, 0.0) + w, 6)
    total = round(sum(wcap.values()), 6)
    weights = {lv: round(v / total, 6) for lv, v in wcap.items()} if total > 0 else {}
    num_sum = sum(wcap[lv] * NUM[lv] for lv in wcap)
    level = round(num_sum / total, 4) if total > 0 else 0.0
    r2_share = round(weights.get("R2", 0.0), 4) if total > 0 else 0.0
    if total > 0:
        if level > float(p["max_level"]):
            violations.append(f"组合加权等级 R{level:.2f} > 上限 R{float(p['max_level']):.1f}")
        if level < float(p["min_level"]):
            warnings.append(f"组合加权等级 R{level:.2f} < 下限 R{float(p['min_level']):.1f}"
                            f"（太保守，等于只买债）")
        if r2_share < float(p["r2_min"]):
            warnings.append(f"R2 占比 {r2_share * 100:.1f}% < 目标下限 {float(p['r2_min']) * 100:.0f}%"
                            f"（R2 层预算不足或选池为空）")
        if r2_share > float(p["r2_max"]):
            warnings.append(f"R2 占比 {r2_share * 100:.1f}% > 目标上限 {float(p['r2_max']) * 100:.0f}%"
                            f"（过于保守）")
    else:
        warnings.append("本期没有已投资标的（全部现金），组合等级视为现金 R1")
    return {"ok": not violations, "level": level, "level_label": num_level(level) if total else "R1",
            "weights": weights, "r2_share": r2_share,
            "r2_target": float(p["r2_target"]),
            "invested": total,
            "violations": violations, "warnings": warnings,
            "focus": focus_order(), "range": [float(p["min_level"]), float(p["max_level"])],
            "investor": PROFILE.get("level", "C3"),
            "investor_line": investor_line()}


def summary_line(review: dict) -> str:
    """一行摘要，给周报/终端用。"""
    if not review:
        return "风险评估：未评级"
    v = review
    parts = [f"组合加权 {v.get('level_label')}（R{float(v.get('level') or 0):.2f}，"
             f"允许 R{v['range'][0]:.1f}–R{v['range'][1]:.1f}）"]
    w = v.get("weights") or {}
    if w:
        parts.append("／".join(f"{lv} {w[lv] * 100:.0f}%" for lv in ORDER if w.get(lv)))
    parts.append(f"R2 占比 {float(v.get('r2_share') or 0) * 100:.0f}%"
                 f"（目标 {float(v.get('r2_target') or 0) * 100:.0f}%）")
    if v.get("violations"):
        parts.append(f"× {len(v['violations'])} 项违规")
    elif v.get("warnings"):
        parts.append(f"! {len(v['warnings'])} 项提示")
    else:
        parts.append("✓ 评审通过")
    return "｜".join(parts)
