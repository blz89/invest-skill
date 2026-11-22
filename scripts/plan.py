# -*- coding: utf-8 -*-
"""阶段三：执行。

把「判断」翻译成「动作」：买什么、买多少、分几批、什么价买、什么价走。

设计原则（每条都能对应到调研到的开源项目原文）：
1. **先定风险预算，再定仓位**（agiprolabs）：账户级回撤上限先写死，仓位由预算倒推，
   而不是先挑股票再拼权重。
2. **分批建仓**：单笔一次性买入等于把择时错误放大到 100%。个股默认 3 批（40/35/25），
   单批金额 < 5,000 元时自动合并（最少 1 批）；ETF 默认单批（`ETF.tranche_mode` 可回滚）。
3. **止损价不是感觉，是算出来的**：初始硬止损 = **实际成交成本 ×0.85**（成交时由
   `paper.apply_fill` 写入；下单阶段的「安全边际买入价 ×0.85」只是限价/括号单的占位价，
   **不是成交后的止损**）。ATR 跟踪只在浮盈 >10% 后启用，且只上移。
4. **砍掉的敞口回现金，不重新分配**（ai-hedge-fund）：止损后不许立刻换一只买回，
   否则等于用更高的不确定性维持同样的风险敞口。
5. **熔断优先于一切**：日亏 3% 停手当日，周亏 5% 仓位减半，连亏 3 笔停 24 小时。
6. **执行边界**：脚本只产出「订单清单」，不接券商下单接口。自动下单需要券商
   QMT/miniQMT/Ptrade 权限，是否启用由用户自己决定。

小资金（本金 5 万）额外三层，见 `capital.py`：
  · 整手化：目标金额 → 能买几手（1 手 = 100 股/份），买不了 1 手的标的改配 ETF 或现金；
  · ETF 承接：个股一手超单票上限时，用场内 ETF 把权益预算精确落地；
  · 现金增值：现金零头拆成「逆回购整手 + 场外货基」，并算清手续费；
  · 场外承接：债券 ETF 一手都买不起时（5 万本金单只 12% 上限也常买不起），R2 预算改由
    **场外债券基金**承接（起投 100 元整数倍、按金额申购、无整手约束；影子账 T+1 记入模拟账本）。

输出：
  out/stage3_plan.md      给人看的执行计划
  out/stage3_plan.json    机器可读（供 AI 逐步执行/提醒）
  out/stage3_orders.csv   订单清单（层/代码/批次/价格/手数/金额/预估费用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import pandas as pd

import capital as cap
import config
import datasource as ds
import industry
import metrics as m
import risk

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = config.OUT_DIR

TRANCHE_RATIO = (0.40, 0.35, 0.25)     # 三批建仓比例

# 被风险等级筛掉的标的（股票/ETF），写进 plan["risk_filter"] 供评审与复盘追溯
_RISK_DROPPED: list = []
# 个股板块 → 准入门槛 key（config.RISK["qualification"]）：被 R4 剔除时把「差多少钱」写清楚
_RISK_QUAL = {"创业板": "创业板", "科创板": "科创板", "北交所": "北交所", "可转债": "可转债"}



# 行业口径：优先同花顺一级行业（细、全市场覆盖），退回百度分类
_THS_IND: dict | None = None


def _ths_industry_map() -> dict:
    """读 `out/industry_map.csv`（industry.py --refresh 产出）。缺失 → {}，fail-soft。"""
    global _THS_IND
    if _THS_IND is not None:
        return _THS_IND
    mp: dict[str, str] = {}
    try:
        import csv as _csv
        p = os.path.join(config.OUT_DIR, "industry_map.csv")
        if os.path.isfile(p):
            with open(p, encoding="utf-8-sig", newline="") as f:
                for r in _csv.DictReader(f):
                    c = str(r.get("code") or "").strip()
                    ind = (r.get("industry") or "").strip()
                    if c and ind:
                        mp[c.zfill(6)] = ind
    except OSError:
        mp = {}
    _THS_IND = mp
    return mp


def ind_of(row: dict) -> str:
    """一只股票的行业：同花顺优先，其次百度，最后「未分类」。

    统一走 `industry.normalize()`（2026-11-07 B 类 #9）：代码级修正表优先——
    `out/industry_map.csv` 与百度快照都可能带借壳前的旧行业（600233 圆通速递
    ｜大杨创世 → 「服装鞋类」），不在这里再兜一次，计划书的行业分散与
    `pe_pct_ind` 会继续按错行业算。
    """
    c = str(row.get("code") or "").strip().zfill(6)
    return industry.normalize(_ths_industry_map().get(c) or row.get("industry"), c)

# ------------------------------------------------------------------ 市场状态
def _probe_regime() -> dict | None:
    """牛熊探针（只读派生）：读 `out/regime.json`；缺失或超过 1 天 → 就地重算并写回。

    写回是为了保住**滞回状态**（`stance` 字段）：否则每次重算都从「无历史」开始，
    指数在阈值附近抖动时会反复换档。fail-soft：任何异常都返回 None，
    退回「沪深300 对 MA200」的原三档判定。
    """
    try:
        import regime_probe as rp
    except Exception:
        return None
    path = os.path.join(config.OUT_DIR, "regime.json")
    prev = None
    try:
        prev = rp.load_doc(path)
        if prev:
            ts = str(prev.get("generated_at") or "")
            age = (dt.datetime.now()
                   - dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0
            if age <= 1.0 and prev.get("state") and prev.get("stance"):
                return prev
    except Exception:
        prev = None
    try:
        doc = rp.build(prev=prev)
    except Exception:
        return None
    try:
        rp.write_doc(doc, path)  # 持久化滞回状态（best-effort，失败不影响决策）
    except Exception:
        pass
    return doc


def market_regime() -> dict:
    """决定总仓位上限：**牛熊探针优先**（按 stance/severity 映射），没有立场时退回 MA200 三档。

    档位：深熊 → 25%；熊 → 防守 35%；牛 → 进攻 `EXEC.max_gross`；
    普通/探针缺失/未确认 → 按沪深300 对 MA200 三档（进攻 80% / 中性 60% / 防守 35%）。
    """
    bench = ds.fetch_index_daily("000300")
    close = pd.to_numeric(bench["close"], errors="coerce").dropna()
    price = float(close.iloc[-1])
    ma200 = m.ma(close, 200)
    dev = (price / ma200 - 1.0) if ma200 else None
    if dev is None:
        base_regime, base_gross = "未知", 0.40
        note = "基准均线数据不足，按最低仓位处理"
    elif dev > 0.02:
        base_regime, base_gross = "进攻", config.EXEC["max_gross"]
        note = (f"沪深300 收于 {price:.2f}，高于 200 日均线 {ma200:.2f}（{dev * 100:+.1f}%）→ 进攻")
    elif dev > -0.08:
        base_regime, base_gross = "中性", 0.60
        note = (f"沪深300 收于 {price:.2f}，{('高于' if dev > 0 else '低于')} "
                f"200 日均线 {ma200:.2f}（{dev * 100:+.1f}%）→ 中性")
    else:
        base_regime, base_gross = "防守", 0.35
        note = (f"沪深300 收于 {price:.2f}，低于 200 日均线 {ma200:.2f}（{dev * 100:+.1f}%）→ 防守")
    regime, gross, probe_state, probe_note = base_regime, base_gross, None, ""
    probe_stance = probe_severity = None
    probe = _probe_regime()
    if probe:
        probe_state = probe.get("state")
        probe_stance = probe.get("stance")
        probe_severity = probe.get("severity")
        bear = probe.get("bear") or {}
        bull = probe.get("bull") or {}
        if probe_stance == "bear" and probe_severity == "深熊":
            regime, gross = "防守", config.REGIME_PROBE["deep_bear_gross"]
            probe_note = (f"牛熊探针：深熊（{bear.get('hits')}/{bear.get('usable')} 条价格判据，"
                          f"{probe.get('stance_why')}）→ 按深熊档 {gross * 100:.0f}%")
        elif probe_stance == "bear":
            regime, gross = "防守", config.REGIME_PROBE["defend_gross"]
            probe_note = (f"牛熊探针：熊市（{probe.get('stance_why')}）"
                          f"→ 按防守档 {gross * 100:.0f}%")
        elif probe_stance == "bull":
            regime, gross = "进攻", config.EXEC["max_gross"]
            probe_note = (f"牛熊探针：牛市（{bull.get('hits')}/{bull.get('usable')} 条价格判据）"
                          f"→ 按进攻档 {gross * 100:.0f}%")
    return {"regime": regime, "base_gross": gross, "bench_price": round(price, 2),
            "bench_ma200": None if ma200 is None else round(ma200, 2),
            "dev": None if dev is None else round(dev, 4),
            "dev_regime": base_regime, "dev_gross": base_gross,
            "probe_state": probe_state, "probe_stance": probe_stance,
            "probe_severity": probe_severity,
            "note": note + (f"；{probe_note}" if probe_note else "")}


# ------------------------------------------------------------------ 选标的
# 研究闸的统计（写进 plan["research_filter"]，pick() 也会把结论写进 note）
_RESEARCH_INFO: dict = {}


def apply_research(results: list[dict], as_of: str = "") -> tuple[list[dict], dict]:
    """把 T2/T4 研究结论接进引擎（研究层是可选增强，读不到就原样放行）。

    为什么需要：`out/stage1b_research.csv` 原先没有任何脚本读，子代理的研究结论
    对计划零影响。这里按契约消费它：
      action=reject     → 剔除
      action=downgrade  → 保留，权重系数 ×0.5（不因一句主观判断整只砍掉）
      action=keep / 其它 → 不动

    新鲜度：产物早于本期数据日（`as_of`）就**不采用**。否则某周 S1 没交卷（或决策日
    跳过研究层）时，`run.py --only-plan` 会把上一轮遗留结论当成本期结论，静默改计划。
    """
    import csv

    path = os.path.join(config.OUT_DIR, "stage1b_research.csv")
    info = {"applied": False, "path": path, "rejected": [], "downgraded": [],
            "n_rows": 0, "note": ""}
    if not os.path.isfile(path):
        info["note"] = "研究层未启用（没有 stage1b_research.csv）"
        return results, info
    if as_of:
        try:
            made = dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()
            if made < str(as_of)[:10]:
                info["note"] = (f"研究层产物早于本期数据日（{made} < {str(as_of)[:10]}），"
                                "不采用——防止上一轮结论顶替本期判断")
                return results, info
        except (OSError, ValueError):
            pass
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f)
                    if any((v or "").strip() for v in r.values())]
    except OSError as e:
        info["note"] = f"研究层读取失败，原样放行：{e}"
        return results, info
    if not rows:
        info["note"] = "研究层产物是空表，原样放行"
        return results, info

    act = {}
    for r in rows:
        code = str(r.get("code") or "").strip()
        a = str(r.get("action") or "").strip().lower()
        if code:
            act[code] = a
    info["n_rows"] = len(rows)

    # 池外代码一律忽略：研究层只能否决/降权池内标的，不能借研究之名加新票（SKILL §3.1）
    present = {str(r.get("code") or "").strip() for r in results}
    info["rejected"] = sorted(c for c, a in act.items() if a == "reject" and c in present)
    info["downgraded"] = sorted(c for c, a in act.items() if a == "downgrade" and c in present)
    info["ignored_outside"] = sorted(c for c in act if c and c not in present)

    kept = []
    for r in results:
        a = act.get(str(r.get("code") or "").strip())
        if a == "reject":
            continue
        r["_research_action"] = a
        r["_research_mult"] = 0.5 if a == "downgrade" else 1.0
        kept.append(r)
    info["applied"] = True
    parts = []
    if info.get("ignored_outside"):
        parts.append(f"忽略池外代码 {len(info['ignored_outside'])} 个（不在候选池，不予采纳）")
    if info["rejected"]:
        parts.append(f"研究层剔除 {len(info['rejected'])} 只（{','.join(info['rejected'][:6])}）")
    if info["downgraded"]:
        parts.append(f"降级 {len(info['downgraded'])} 只（权重×0.5：{','.join(info['downgraded'][:6])}）")
    info["note"] = ("研究层已接入：" + "；".join(parts)) if parts else "研究层已接入：无剔除/降级"
    return kept, info


# 新闻闸的统计（写进 plan["news_filter"]，pick() 的 note 也会带上）
_NEWS_INFO: dict = {}


def apply_news(results: list[dict], as_of: str = "") -> tuple[list[dict], dict]:
    """新闻闸：把 `news.py` 的 R/C 分接进选股权重（读不到产物就原样放行）。

    为什么**只调权重系数、不改判定与分数**：
      70 分买入线是硬阈值，让主观新闻分参与判定会污染阈值语义（今天 68 分因一条
      新闻变买入区，明天同样的 68 分又不算）。所以新闻只乘在 `pick()` 的权重上。
    系数来自 `references/news-rules.md` 的两池 12 期校准：
      否决(R≥60 或硬雷) → 剔除；降权40 → ×0.6；降权15 → ×0.85；
      加分15(C≥22) → ×1.15；加分10(C≥15) → ×1.10；其余 ×1.0。
    同一只股票多条事件不叠加，取最坏一条（`news.code_scores()` 已做）。
    开关 `config.NEWS["gate"]`；关掉即「仅记录」，新闻只写简报。

    新鲜度：产物早于本期数据日（`as_of`）就不采用——事件面只在决策日采，若某周
    第 4 步没跑成功，旧新闻会让上周的「否决/降权」继续改本周计划。
    """
    info = {"applied": False, "enabled": bool((getattr(config, "NEWS", {}) or {}).get("gate", True)),
            "rejected": [], "adjusted": [], "n_codes": 0, "note": ""}
    if not info["enabled"]:
        info["note"] = "新闻闸已关闭（NEWS.gate=False），事件面只记录不干预"
        return results, info
    try:
        import news as news_mod
        scores = news_mod.code_scores()
    except Exception as e:                      # noqa: BLE001 - 事件面不能阻断主流程
        info["note"] = f"新闻层不可用，原样放行：{type(e).__name__}: {e}"
        return results, info
    if not scores:
        info["note"] = "新闻层未启用（没有 news_events.csv），原样放行"
        return results, info
    if as_of:
        try:
            p = os.path.join(config.OUT_DIR,
                             (getattr(config, "NEWS", {}) or {}).get("out_csv")
                             or "news_events.csv")
            made = dt.date.fromtimestamp(os.path.getmtime(p)).isoformat()
            if made < str(as_of)[:10]:
                info["note"] = (f"新闻产物早于本期数据日（{made} < {str(as_of)[:10]}），"
                                "不采用——防止上周事件继续改本周权重")
                return results, info
        except (OSError, ValueError):
            pass

    info["n_codes"] = len(scores)
    kept = []
    for r in results:
        code = str(r.get("code") or "").strip()
        s = scores.get(code)
        if not s:
            r["_news_mult"] = 1.0
            kept.append(r)
            continue
        if s["mult"] <= 0:
            info["rejected"].append({"code": code, "name": s.get("name") or r.get("name"),
                                     "r": s["r"], "tags": s["tags"]})
            continue
        r["_news_mult"] = float(s["mult"])
        r["_news_tier"] = s["tier"]
        r["_news_r"], r["_news_c"] = s["r"], s["c"]
        r["_news_tags"] = s["tags"]
        if abs(float(s["mult"]) - 1.0) > 1e-9:
            info["adjusted"].append({"code": code, "name": s.get("name") or r.get("name"),
                                     "tier": s["tier"], "mult": float(s["mult"]),
                                     "r": s["r"], "c": s["c"], "tags": s["tags"]})
        kept.append(r)

    info["applied"] = True
    parts = []
    if info["rejected"]:
        parts.append(f"新闻否决 {len(info['rejected'])} 只（"
                     f"{','.join(x['code'] for x in info['rejected'][:6])}）")
    if info["adjusted"]:
        adj_txt = ",".join(f"{x['code']}×{x['mult']:g}" for x in info["adjusted"][:6])
        parts.append(f"新闻调权 {len(info['adjusted'])} 只（{adj_txt}）")
    info["note"] = ("新闻闸已接入：" + "；".join(parts)) if parts else "新闻闸已接入：无否决/调权"
    return kept, info


def _names_for_gross(gross) -> int:
    """按权益比例决定排名篮子只数（config.EXEC.max_stock_names_by_ratio）。"""
    table = list(config.EXEC.get("max_stock_names_by_ratio") or [])
    fallback = max(1, int(config.EXEC.get("max_stock_names") or 4))
    if gross is None or not table:
        return fallback
    g = float(gross)
    for lim, n in table:
        if g <= float(lim) + 1e-9:
            return max(1, int(n))
    return max(1, int(table[-1][1]))


def pick(results: list[dict], regime: dict,
         gross: float | None = None) -> tuple[list[dict], str]:
    """选个股组合：默认「排名篮子」（config.EXEC["stock_weighting"]），conviction 口径可选。

    rank（默认）：
      买入区与观察区按总分合并排序，取前 N 名（N 随档位：≤35% 4 只、≤60% 5 只、>60% 6 只），
      按 rank_weights 给线性权重系数（1.0/0.85/0.70/0.55）。
      为什么：买入线 70 是绝对门槛，大多数周只有 0–1 只达标；conviction 口径下这一只
      独占个股层 50%+，组合外观变成「押注一只股票」。排名篮子让「达标但没进买入区」
      的标的也按排名分到仓位，评分仍是唯一排序依据——没有新增或下调任何阈值。
    conviction（可选开关，仅回滚用）：
      买入区 = 核心仓（系数 1.0，最多 max_n 只）；观察区前 2 名 = 试探仓（系数 0.5）。
      注意：回滚口径仍按 `gross / 单票上限` 推算只数，不读 `max_stock_names_by_ratio`。
    """
    buy = sorted([r for r in results if r["verdict"] == "买入区"],
                 key=lambda r: -(r["scores"].get("total") or 0))
    watch = sorted([r for r in results if r["verdict"] == "观察"],
                   key=lambda r: -(r["scores"].get("total") or 0))

    if config.EXEC.get("stock_weighting", "rank") == "conviction":
        return _pick_conviction(buy, watch, regime)

    ranked = sorted(buy + watch, key=lambda r: -(r["scores"].get("total") or 0))
    if not ranked:
        return [], "没有任何标的通过判断，本期不建仓、保持现金"
    max_n = _names_for_gross(gross)
    weights = list(config.EXEC.get("rank_weights") or [1.0, 0.85, 0.70, 0.55])
    n = min(max_n, len(ranked))
    chosen = []
    for i, r in enumerate(ranked[:n]):
        base_w = weights[i] if i < len(weights) else weights[-1]
        # 研究层降级 ×0.5、新闻闸按档位调权（两者都没启用时缺省 1.0）
        research_mult = float(r.get("_research_mult") or 1.0)
        news_mult = float(r.get("_news_mult") or 1.0)
        # 观察区权重（2026-11-07 B 类 #9）：默认 0 = 只观察、不建仓；回滚改 EXEC.watch_weight=0.5。
        # 为什么不再给 0.5：观察区是「没到买入线」的标的，给它仓位等于把买入线 70 悄悄降了。
        is_watch = r["verdict"] == "观察"
        watch_mult = float(config.EXEC.get("watch_weight", 0.0)) if is_watch else 1.0
        w = round(base_w * research_mult * news_mult * watch_mult, 4)
        chosen.append({"r": r, "mult": w, "base": base_w, "rank": i + 1,
                       "watch": is_watch})

    def _weight_desc(c: dict) -> str:
        """把「排名权重 × 研究/新闻乘数」拆开展示，避免报告里只出现 1.10 无法解释。"""
        extra = [("研究乘数", float(c["r"].get("_research_mult") or 1.0)),
                 ("新闻乘数", float(c["r"].get("_news_mult") or 1.0))]
        parts = [f"排名权重 {float(c['base']):.2f}"]
        parts += [f"{name} {val:.2f}" for name, val in extra if abs(val - 1.0) > 1e-9]
        if len(parts) == 1:
            return f"{c['mult']:.2f}"
        return f"{c['mult']:.2f}（{' × '.join(parts)}）"

    wstr = "/".join(_weight_desc(c) for c in chosen)
    names = "、".join(f"第{c['rank']}名 {c['r']['name']} {c['r']['scores'].get('total')}"
                      for c in chosen)
    note = (f"排名篮子 {n} 只（可投 {len(ranked)} 只按总分排序，"
            f"权重系数 = 排名权重 × 研究/新闻乘数：{wstr}）：{names}")
    if len(ranked) > n:
        note += (f"；另有 {len(ranked) - n} 只可投标的受「最多持 {max_n} 只」约束未纳入"
                 f"（单只不足 1 手资金效率低）")
    if _RESEARCH_INFO.get("note"):
        note += "；" + _RESEARCH_INFO["note"]
    if _NEWS_INFO.get("note"):
        note += "；" + _NEWS_INFO["note"]
    return chosen, note


def _pick_conviction(buy: list[dict], watch: list[dict],
                     regime: dict) -> tuple[list[dict], str]:
    """conviction 口径：核心仓 = 买入区，试探仓 = 观察区前 2 名。"""
    max_n = max(1, int(round(regime["base_gross"] / config.EXEC["max_single_position"])))
    watch_mult = float(config.EXEC.get("watch_weight", 0.0))
    chosen = [{"r": r, "mult": 1.0} for r in buy[:max_n]]
    n_probe = 0
    room = max_n - len(chosen)
    if room > 0 and watch and watch_mult > 0:
        n_probe = min(room, 3)
        chosen += [{"r": r, "mult": watch_mult, "watch": True} for r in watch[:n_probe]]

    if not chosen:
        return [], "没有任何标的通过判断，本期不建仓、保持现金"

    parts = []
    if buy[:max_n]:
        parts.append(f"核心仓 {len(buy[:max_n])} 只（买入区，权重系数 1.0）")
    if n_probe:
        parts.append(f"试探仓 {n_probe} 只（观察区，权重系数 {watch_mult:.2f}）")
    note = "；".join(parts)
    if len(buy) > max_n:
        note += f"。买入区共 {len(buy)} 只，受单票上限约束只取前 {max_n} 只"
    return chosen, note


# ------------------------------------------------------------------ 组仓
def allocate(chosen: list[dict], regime: dict,
             gross: float | None = None) -> list[dict]:
    """按权重系数分配目标权重，再逐个套单票 / 行业 / 总敞口上限。

    削减下来的部分全部留现金，**不做补偿性买入**——这是风险预算制，
    不是资金利用率制。宁可拿着闲置资金，也不为了「用满仓位」去降低标准。

    `gross` 可传档位算出的权益比例（小资金三档）；不传则用市场状态上限。
    """
    if not chosen:
        return []
    gross = regime["base_gross"] if gross is None else float(gross)
    const = sum(c["mult"] for c in chosen)

    out, used_ind, total = [], {}, 0.0
    for c in chosen:
        r, mult = c["r"], c["mult"]
        ind = ind_of(r)
        raw = gross * mult / const
        room_ind = (float("inf") if ind == industry.UNCLASSIFIED
                    else config.EXEC["max_industry"] - used_ind.get(ind, 0.0))
        room_all = config.EXEC["max_gross"] - total
        take = max(0.0, min(raw, config.EXEC["max_single_position"], room_ind, room_all))
        if take <= 0:
            # 观察区（权重乘数 0）仍要留在计划里，只是 0 手 0 元——报告必须让人看见
            # 「它被选中了但没买」，否则读者以为漏了标的。
            if float(c.get("mult") or 0.0) <= 0:
                out.append({"result": r, "weight": 0.0, "probe": False,
                            "rank": c.get("rank"), "raw_weight": 0.0,
                            "watch_only": True})
            continue
        used_ind[ind] = used_ind.get(ind, 0.0) + take
        total += take
        out.append({"result": r, "weight": round(take, 4), "probe": mult < 1.0,
                    "rank": c.get("rank"), "raw_weight": round(raw, 4)})

    if total < config.EXEC["min_gross"]:
        out.append({"result": None, "weight": round(1 - total, 4), "cash": True,
                    "note": f"目标敞口仅 {total * 100:.0f}%，低于最低 "
                            f"{config.EXEC['min_gross'] * 100:.0f}%，差额留现金"})
    return out


# ------------------------------------------------------------------ 价格梯队
def price_ladder(r: dict) -> tuple[list[dict], str]:
    """三批的触发价：现价 / 买入价×0.95 / 买入价×0.90（个股）；ETF 用现价×1/0.95/0.90。"""
    pr = r.get("pricing") or {}
    x = r.get("metrics") or {}
    price = x.get("price")
    buy = pr.get("buy_price")
    if not price:
        return [], "缺现价"
    anchor = buy if buy else price
    if buy and price <= buy * 1.02:
        p1, state = round(price, 2), "现价即限价（已进入安全边际区；挂限价单，开盘不高于该价即成交）"
    elif buy:
        p1 = round(buy * 1.02, 2)
        state = (f"挂条件单：回落到 {buy * 1.02:.2f} 元以下再买"
                 f"（现价 {price} 高于买入价 {buy}）")
    else:
        p1, state = round(price, 2), "缺买入价，按现价挂限价单分批"
    prices = [p1, round(anchor * 0.95, 2), round(anchor * 0.90, 2)]
    return [{"tranche": i, "ratio": rt, "price": px}
            for i, (rt, px) in enumerate(zip(TRANCHE_RATIO, prices), 1)], state


# ------------------------------------------------------------------ ETF 层
def load_etf_pool() -> tuple[list[dict], str]:
    """读阶段一补充的 ETF 候选（stage1_etf.csv）；没有就如实说明。

    只有 R2/R3 进入组合：宽基/策略 ETF 是 R3（中风险），行业 ETF 是 R4（不做），
    债券/货币类在这里本来就被 etf.py 的排除规则剔掉了（R2 池由 wealth.py 负责）。
    """
    path = os.path.join(OUT, "stage1_etf.csv")
    if not os.path.exists(path):
        return [], "未找到 out/stage1_etf.csv（先跑 etf.py）→ 本期不配 ETF"
    # 注意：这里**不能** clear _RISK_DROPPED——调用前 main() 已经把个股 R4/R5
    # 的剔除记录放进去，clear 会把它们抹掉（2026-10-26 发现的真实 bug：
    # risk_filter 只剩 ETF，个股风险等级剔除无记录，与报告/engine-notes 口径不符）。
    df = pd.read_csv(path, dtype={"sym": str})
    df = df[~df["name"].astype(str).str.contains("NaN", na=False)]
    rows, dropped = [], []
    for _, r in df.iterrows():
        try:
            row = {
                "sym": str(r["sym"]), "name": str(r["name"]), "cls": str(r["cls"]),
                "underlying": str(r.get("underlying") or r["name"]),
                "price": float(r["price"]), "score": float(r["score"]),
                "ret_3y": None if pd.isna(r.get("ret_3y")) else float(r["ret_3y"]),
                "dd_3y": None if pd.isna(r.get("dd_3y")) else float(r["dd_3y"]),
                "amt20_yi": None if pd.isna(r.get("amt20_yi")) else float(r["amt20_yi"]),
                "vs_ma200": None if pd.isna(r.get("vs_ma200")) else float(r["vs_ma200"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        lv = risk.etf_level(row["name"], row["cls"])
        if not risk.included(lv["level"]):
            dropped.append({"sym": row["sym"], "name": row["name"],
                            "level": lv["level"], "reason": lv["reason"]})
            continue
        row["level"], row["level_reason"] = lv["level"], lv["reason"]
        rows.append(row)
    # 优先宽基（分散）→ 策略（因子）→ 行业（行业押注，最后才用）；同类里按综合分
    _cls_rank = {"宽基": 0, "策略": 1, "行业": 2}
    rows.sort(key=lambda x: (_cls_rank.get(x["cls"], 3), -x["score"]))
    for cut in dropped:
        _RISK_DROPPED.append(cut)
    return rows, (f"ETF 候选 {len(rows)} 只（宽基→策略→行业，同类按综合分排序；"
                  f"宽基{sum(1 for r in rows if r['cls'] == '宽基')}/"
                  f"策略{sum(1 for r in rows if r['cls'] == '策略')}/"
                  f"行业{sum(1 for r in rows if r['cls'] == '行业')}；"
                  f"风险等级筛掉的 {len(dropped)} 只见 risk_filter）")


def etf_layer(pool: list[dict], budget: float, capital: float,
              used_ind: dict, etf_budget_ratio: float) -> dict:
    """用剩余权益预算买 ETF：按 10% 单票上限 + 最小单笔金额决定只数与权重。"""
    cfg = config.ETF
    out = {"budget": round(budget, 2), "positions": [], "unaffordable": [],
           "note": ""}
    if budget <= 0 or not pool:
        out["note"] = "无剩余权益预算或无 ETF 候选"
        return out

    min_pos = cfg.get("min_position_value", 800.0)
    n_by_budget = int(budget // min_pos)
    n_by_cap = max(1, int(math.ceil(etf_budget_ratio / cfg["max_single"] - 1e-9)))
    n = max(0, min(cfg["max_etf_n"], n_by_budget, n_by_cap))
    if n == 0:
        out["note"] = (f"剩余权益预算 {budget:,.0f} 元 < 单笔最小 {min_pos:,.0f} 元，"
                       f"转现金层（小资金下拆太碎，5 元最低佣金会吃掉收益）")
        return out

    per = min(budget / n, capital * cfg["max_single"])
    used = dict(used_ind)
    chosen = []
    for e in pool:
        if len(chosen) >= n:
            break
        # 行业类 ETF 计入所属行业额度（用跟踪标的键，同一行业的 ETF 合并计算）；
        # 宽基/策略类本身分散，不占行业额度
        ind_key = e["underlying"] if e["cls"] == "行业" else None
        if ind_key is not None:
            room_ind = config.EXEC["max_industry"] - used.get(ind_key, 0.0)
            if room_ind <= 0:
                continue
        row = cap.lot_table([{**e, "weight": per / capital}], capital, is_etf=True)[0]
        if not row["feasible"]:
            out["unaffordable"].append({
                "sym": e["sym"], "name": e["name"],
                "lot_cost": row["lot_cost"],
                "note": f"1 手需 {row['lot_cost']:,.0f} 元 > 单只预算 {per:,.0f} 元"})
            continue
        if ind_key is not None:
            used[ind_key] = used.get(ind_key, 0.0) + row["actual_weight"]
        chosen.append({**row, "ind_key": ind_key})

    # 实际买到的钱少于预算 → 差额留给现金层（不为了用满预算去降标准）
    spent = sum(c["actual_amount"] for c in chosen)
    out["positions"] = chosen
    out["spent"] = round(spent, 2)
    out["note"] = (f"剩余权益预算 {budget:,.0f} 元 → 配 {len(chosen)} 只 ETF，"
                   f"实际落地 {spent:,.0f} 元；单只 ≈{per:,.0f} 元"
                   f"（≥单笔最小 {min_pos:,.0f} 元，保证最低佣金不吃掉收益）")
    return out


def _merge_stock_ladder(ladder: list[dict], min_amount: float,
                        lot_size: int) -> list[dict]:
    """把个股三批里金额 < `min_amount` 的批次并入相邻批（2026-11-07 B 类 #3）。

    为什么：单笔 2,500 → 5,000 元后，5 万本金下 10% 的单票上限只有 5,000 元，
    40/35/25 三批天然是 2,000/1,750/1,250 元，全都不达标。若硬性要求每批 ≥5,000，
    计划会变成「一只都买不了」。取舍：**按手数能买就买**——先保证整手与单票 10% 上限
    不变，再把买不起的批次合并成更少的批（最少 1 批 = 一次买入），金额仍受 10% 约束。
    合并后 `merged_from` 记录来源批次，报告里能看出「为什么只有 1 批」。
    """
    out = [dict(b) for b in ladder if int(b.get("lots") or 0) > 0]
    while len(out) > 1:
        for b in out:
            b["amount"] = round(b["lots"] * b["price"] * lot_size, 2)
        small = next((k for k, b in enumerate(out) if b["amount"] < min_amount), None)
        if small is None:
            break
        if small > 0:
            dst, src = small - 1, out[small]
            out.pop(small)
        else:
            # 第 1 批太小：把第 2 批并进第 1 批（仍按现价，不会追高）
            dst, src = 0, out[1]
            out.pop(1)
        out[dst]["lots"] = int(out[dst]["lots"]) + int(src["lots"])
        out[dst]["merged_from"] = sorted(set(
            list(out[dst].get("merged_from") or []) + [src["tranche"]]
            + list(src.get("merged_from") or [])))
    total_lots = sum(int(b["lots"]) for b in out)
    for i, b in enumerate(out, 1):
        b["tranche"] = i
        b["shares"] = int(b["lots"]) * lot_size
        b["ratio"] = round(b["lots"] / total_lots, 4) if total_lots else 0.0
        b["amount"] = round(b["lots"] * b["price"] * lot_size, 2)
        b["state"] = (b.get("state") or "") if i == 1 else f"价格跌到 {b['price']} 元买第 {i} 批"
        if b.get("merged_from"):
            b["merge_note"] = (f"原第 {'、'.join(str(x) for x in b['merged_from'])} 批金额不足 "
                               f"{min_amount:,.0f} 元，已并入本批（整手降级，不突破单票 10% 上限）")
    return out


def etf_ladder_steps(px: float, lots: int, min_amount: float = None,
                     mode: str = None) -> list[dict]:
    """ETF 建仓批次：默认**单批一次买入**（2026-11-07 B 类 #8），可回滚三批。

    为什么改单批：5 万本金下 ETF 单只 ≈2,900 元，按 40/35/25 拆三批后尾批只有
    500–700 元；5 元最低佣金把 574 元碎单的单边成本抬到 0.87%，而一次买入 2,900 元
    只有 0.17%。在「最低佣金 5 元」的账户里，分批的择时收益抵不过碎单成本。

    参数化：`config.ETF["tranche_mode"]`（"single" 默认 / "three" 回滚），
    `config.ETF["min_tranche_value"]` 只用于文案与三批模式的最小单笔。
    three 模式保留原逻辑：40%/35%/25%，金额 < `min_amount`（默认 800）的批次并入相邻批，
    批次数可能降到 2 或 1，合并来源写 `merged_from` / `merge_note`。
    """
    px = float(px or 0.0)
    total = max(0, int(lots or 0))
    lot_size = int(config.CAPITAL.get("lot_size") or 100)
    if px <= 0 or total <= 0:
        return []
    mode = str(mode or (config.ETF or {}).get("tranche_mode") or "three")
    min_tranche = float((config.ETF or {}).get("min_tranche_value") or 1500.0)
    if mode == "single":
        amt = round(total * px * lot_size, 2)
        return [{"tranche": 1, "ratio": 1.0, "price": round(px, 3),
                 "lots": total, "shares": total * lot_size, "amount": amt,
                 "merged_from": [1, 2, 3],
                 "merge_note": (f"单批模式：原 40/35/25 三批合并为一次买入"
                                f"（目标单笔 ≥{min_tranche:,.0f} 元，避免碎单被 "
                                f"5 元最低佣金放大；回滚改 ETF.tranche_mode=three）")}]
    if min_amount is None:
        min_amount = float((config.ETF or {}).get("min_position_value") or 800.0)
    steps = []
    for i, (ratio, n) in enumerate(zip(cap.TRANCHE_RATIOS, cap.split_lots(total)), 1):
        steps.append({"tranche": i, "ratio": ratio,
                      "price": round(px * (1.0 - 0.05 * (i - 1)), 3),
                      "lots": n, "merged_from": []})
    while len(steps) > 1:
        for s in steps:
            s["amount"] = round(s["lots"] * s["price"] * lot_size, 2)
        empty = next((k for k, s in enumerate(steps) if s["lots"] <= 0), None)
        if empty is not None:
            steps.pop(empty)
            continue
        small = next((k for k, s in enumerate(steps) if s["amount"] < min_amount), None)
        if small is None:
            break
        if small > 0:                       # 并入上一批（价格更稳的那批）
            dst = small - 1
            src = steps[small]
            steps[dst]["merged_from"] += [src["tranche"]] + src.get("merged_from", [])
            steps[dst]["lots"] += src["lots"]
            steps.pop(small)
        else:
            # 第 1 批太小：把下一批并进来（仍按现价，px=现价，不存在追高；
            # 反过来把第 1 批并进下一批会让整只仓位都等回调，且金额仍可能 <800）。
            src = steps[1]
            steps[0]["merged_from"] += [src["tranche"]] + src.get("merged_from", [])
            steps[0]["lots"] += src["lots"]
            steps.pop(1)
    out = []
    for k, s in enumerate(steps, 1):
        amt = round(s["lots"] * s["price"] * lot_size, 2)
        merged = sorted(set(s.get("merged_from") or []))
        note = ""
        if merged:
            note = (f"原第 {'、'.join(str(x) for x in merged)} 批金额不足 "
                    f"{min_amount:,.0f} 元，已并入本批")
        out.append({"tranche": k, "ratio": round(s["lots"] / total, 4),
                    "price": s["price"], "lots": s["lots"],
                    "shares": s["lots"] * lot_size, "amount": amt,
                    "merged_from": merged, "merge_note": note})
    return out


# ------------------------------------------------------------------ R2 理财层
def load_wealth_pool() -> tuple[list[dict], str]:
    """读 R2 候选池（stage1_wealth.csv，由 wealth.py 产出）。"""
    path = os.path.join(OUT, "stage1_wealth.csv")
    if not os.path.exists(path):
        return [], "未找到 out/stage1_wealth.csv（先跑 wealth.py）→ 本期不配 R2 理财层"
    df = pd.read_csv(path, dtype={"sym": str})
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append({
                "sym": str(r["sym"]), "name": str(r["name"]),
                "key": str(r.get("key") or r["name"]),
                "price": float(r["price"]), "score": float(r["score"]),
                "ret_1y": None if pd.isna(r.get("ret_1y")) else float(r["ret_1y"]),
                "dd_1y": None if pd.isna(r.get("dd_1y")) else float(r["dd_1y"]),
                "amt20_yi": None if pd.isna(r.get("amt20_yi")) else float(r["amt20_yi"]),
                "level": str(r.get("level") or "R2"),
                "level_reason": str(r.get("level_reason") or "场内债券 ETF"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: -x["score"])
    return rows, f"R2 债券 ETF 候选 {len(rows)} 只（按综合分排序）"


# ------------------------------------------------------------------ R2 场外兜底（债券基金）
# 为什么要有这条腿：5 万本金下 R2 预算约 7,000 元，而一手场内债券 ETF 约 1.1–1.2 万元，
# 单只上限 12%×5 万 = 6,000 元 < 一手 → 场内仍然配不出。场外债券基金实测 99.5% 起购
# ≤10 元、申购费中位数 0（77.6% 为 0）、持有满 90 天赎回费 0（7–30 天档仍收
# 0.10%–0.75%，见 _otc_fee_rule_txt），是小资金 R2 层
# 唯一买得起的承接工具。场内优先、场外兜底，二者互斥（见 SKILL.md §1「场外基金池 = R2 的第二条腿」）。
def load_otc_pool() -> tuple[list[dict], str]:
    """读场外债券基金池（out/stage1_otc.csv，由 otc.py 产出）。

    缺失/读失败/空表都**不抛异常**：场外兜底是可选的第二腿，不能让主流程（选股 +
    ETF + 现金）因为场外数据没跑而整期不出计划。返回 ([], note)，note 写进计划文本与 JSON。
    """
    path = os.path.join(OUT, "stage1_otc.csv")
    if not os.path.exists(path):
        return [], ("场外池缺失：未找到 out/stage1_otc.csv（先跑 otc.py）→ "
                    "R2 层场内 ETF 一手买不起时退回现金层")
    try:
        df = pd.read_csv(path, dtype={"code": str})
    except Exception as e:  # noqa: BLE001  —— 兜底腿的数据坏了不该拖垮整期计划
        return [], f"场外池缺失：读 out/stage1_otc.csv 失败（{e}）→ R2 层退回现金层"
    if df is None or df.empty:
        return [], "场外池缺失：out/stage1_otc.csv 为空表 → R2 层退回现金层"
    # 历史列名：2026-10-27 前 otc.py 输出的是 redeem_fee_7d（语义 = 「刚满 7 天」最严档，
    # 会把成本高估），T1 已改名 redeem_fee_hold（持有满 90 天口径）——不做兼容读取，
    # 否则旧产物会带着错口径静默进入计划。缺列直接提示重跑（不抛异常，主流程照走）。
    if "redeem_fee_hold" not in df.columns:
        return [], ("场外池缺失：out/stage1_otc.csv 缺 redeem_fee_hold 列（旧版 otc.py 产物）→ "
                    "请重跑 python scripts/otc.py。旧产物里的赎回费是「刚满 7 天」最严档，"
                    "成本会被高估，所以不兼容")

    def _txt(r, key: str) -> str:
        v = r.get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("nan", "none", "nat") else s

    def _num(r, key: str, default=None):
        v = r.get(key)
        if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
            return default
        try:
            if pd.isna(v):
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    rows = []
    for _, r in df.iterrows():
        code = _txt(r, "code")
        name = _txt(r, "name")
        if not code or not name:
            continue
        status = _txt(r, "purchase_status")
        # 只有「暂停申购/封闭期/认购期」不能买；「限大额」对小本金无影响（限额 1000 万+）
        if any(x in status for x in ("暂停", "封闭", "认购", "不可")):
            continue
        level = _txt(r, "level") or "R2"
        if not level.startswith("R2"):       # 等级不是 R2 的不得进 R2 层（fail-closed）
            continue
        min_purchase = _num(r, "min_purchase", 100.0)
        if min_purchase is None or min_purchase < 0:
            min_purchase = 100.0
        rows.append({
            "code": code, "name": name, "ftype": _txt(r, "ftype"),
            "nav_date": _txt(r, "nav_date"),
            "unit_nav": _num(r, "unit_nav"), "acc_nav": _num(r, "acc_nav"),
            "purchase_status": status,
            "min_purchase": min_purchase,
            "day_limit": _num(r, "day_limit"),
            "fee": _num(r, "fee", 0.0),
            "hist_days": _num(r, "hist_days", 0.0),
            "ret_1y": _num(r, "ret_1y"), "ret_3y": _num(r, "ret_3y"),
            "dd_1y": _num(r, "dd_1y"), "dd_3y": _num(r, "dd_3y"),
            "vol_3y": _num(r, "vol_3y"), "mgmt_fee": _num(r, "mgmt_fee"),
            "redeem_fee_hold": _num(r, "redeem_fee_hold"),
            "cost_1y": _num(r, "cost_1y"),
            "score": _num(r, "score", 0.0),
            "level": level,
            "level_reason": _txt(r, "level_reason") or "场外债券基金（R2 固定收益类）",
            "key": _txt(r, "key") or name,
            "share_class": _txt(r, "share_class"),
        })
    # 排序键必须全序（含 code 兜底），否则同分基金顺序不确定 → determinism 会挂
    rows.sort(key=lambda x: (-(x["score"] or 0.0),
                             x["cost_1y"] if x["cost_1y"] is not None else 9.99,
                             x["min_purchase"], x["code"]))
    return rows, f"场外债券基金候选 {len(rows)} 只（按综合分排序）"


def check_pool_duplicates(etf_pool: list[dict], otc_pool: list[dict]) -> None:
    """场内 ETF 池 ∩ 场外基金池（code 归一化后）必须为空——重复必须 fail loud。

    为什么只能在 plan.py 做：otc.py 只能在产品层面去重（排除 ETF 联接/货基/A-C 同类
    份额），它看不到场内 ETF 池；而两池的 code 命名空间不同（场内 `sh511520`、场外
    `000033`），只能用 ds.norm_code() 归一化后比 6 位数字。
    为什么是 raise 不是 warning：同一风险敞口被算两次 = R2 预算被吃掉双份，静默的重复
    会让计划看起来「配满了」，实际是把同一个东西买了两遍（用户原话「切记别弄重复了」）。
    """
    def _codes(pool: list[dict], keys: tuple) -> set:
        got = set()
        for r in pool or []:
            for k in keys:
                v = r.get(k)
                if v:
                    got.add(ds.norm_code(v))
                    break
        return got

    etf_codes = _codes(etf_pool, ("code", "sym"))
    otc_codes = _codes(otc_pool, ("code",))
    dup = sorted(otc_codes & etf_codes)
    if dup:
        raise ValueError(f"场外池与场内池重复（code 归一化后）：{dup} —— "
                         "同一风险敞口会被算两次，拒绝出计划（见 SKILL.md §1「场外基金池 = R2 的第二条腿」）")


def _otc_fee_txt(sel: list[dict]) -> str:
    """所选场外基金「持有满 90 天赎回费」的口径描述——数据驱动，不写死「0」。

    赎回费按持有天数阶梯。这里描述的是本计划成本口径：持有满 90 天取档
    （otc.py 的 HOLD_DAYS=90）——别用「刚满 7 天」最严档。
    """
    reds = [float(e.get("redeem_fee_hold") or 0.0) for e in sel or []]
    if not reds:
        return "持有满 90 天赎回费见成本口径"
    mx = max(reds)
    if mx <= 1e-9:
        return "持有满 90 天赎回费 0（所选基金实测档位）"
    return f"持有满 90 天赎回费最高 {mx:.2f}%（按所选基金实测档位，已计入成本口径）"


def _otc_fee_rule_txt() -> str:
    """赎回费阶梯的常驻说明（避免把「R2 中低风险」读成「满 7 天就能免费赎回的活期」）。

    为什么必须写下限：redeem_fee_hold 是 90 天口径的值，多数基金为 0；但 7–30 天
    这一档仍收费（样本 0.10%–0.75%），个别基金甚至设 90 天档。只写「赎回费 0」
    会让用户以为持有 7 天就能免费跑——那是这句常驻说明要堵的坑。
    """
    return ("赎回费按持有期分档：不满 7 天 1.50%（惩罚性）；满 7 天至 30 天前仍收 "
            "0.10%–0.75%（样本区间，因基金而异）；满 30 天起通常为 0，个别基金设 90 天档"
            "——以本次所选基金的实测档位为准（计划逐只打印）。R2 层是 12 个月不动的钱，"
            "cost_1y 按持有 90 天取档；别把「R2 中低风险」读成「满 7 天就能免费赎回的活期」。")


def _otc_fee_range_txt(sel: list[dict]) -> str:
    """所选场外基金申购费率区间（百分数），用于计划文本的成本说明。"""
    fees = [float(e.get("fee") or 0.0) for e in sel or []]
    if not fees:
        return "申购费率见成本口径"
    lo, hi = min(fees), max(fees)
    return f"申购费率 {lo:.2f}%" if abs(hi - lo) < 1e-9 else f"申购费率 {lo:.2f}%–{hi:.2f}%"


def _otc_unit(e: dict) -> int:
    """单笔最小申购单位：max(购买起点, 100) 向上取整到 100 元的整数倍。

    为什么不是整手：场外基金按**金额**申购，没有「1 手 = 100 份」的概念；
    100 元是最小颗粒（再碎没意义，且报告里读着费劲）。起投 1000 元的 A 类基金
    最小单位就是 1000 元，硬凑 100 元会直接申购失败。
    """
    base = max(float(e.get("min_purchase") or 0.0), 100.0)
    return int(math.ceil(base / 100.0)) * 100


def otc_layer(pool: list[dict], budget: float, capital: float,
              single_cap: float) -> dict:
    """场外兜底腿：把 R2 预算按**金额**分到 1–N 只场外债券基金。

    与场内 ETF 腿的区别：场外没有最低佣金（场内 5 元最低佣金在千元仓位上就是 1%），
    所以 min_position_value 默认 100 元，而不是场内那条腿的 800 元。
    """
    cfgw = config.RISK["wealth"]
    oc = cfgw.get("otc") or {}
    min_pos = float(oc.get("min_position_value") or 100.0)
    max_n = int(oc.get("max_n") or cfgw.get("max_n") or 3)
    cap_amt = round(capital * single_cap, 2)
    out = {"budget": round(budget, 2), "positions": [], "unaffordable": [],
           "note": "", "spent": 0.0, "venue": "OTC",
           "min_position_value": min_pos, "max_n": max_n,
           "single_cap_amount": cap_amt}
    if oc.get("enabled") is False:
        out["note"] = "场外兜底在 config 中关闭（RISK.wealth.otc.enabled=False）→ 退回现金层"
        return out
    if budget <= 0:
        out["note"] = "无 R2 预算，场外兜底不启用"
        return out
    if not pool:
        out["note"] = "场外池缺失（stage1_otc.csv 不存在或为空）→ R2 层退回现金层"
        return out
    units = sorted(_otc_unit(e) for e in pool)
    cheapest = units[0] if units else 0
    start = max(cheapest, int(math.ceil(min_pos / 100.0)) * 100)
    if budget < start:
        out["note"] = (f"R2 预算 {budget:,.0f} 元 < 场外最小起投 {start:,.0f} 元 "
                       f"→ 场外兜底不启用，预算退回现金层")
        return out
    n = max(1, min(max_n, len(pool), int(budget // start)))
    per = min(budget / n, cap_amt)
    chosen, spent, remain = [], 0.0, budget
    for e in pool:
        if len(chosen) >= n or remain <= 0:
            break
        unit = _otc_unit(e)
        amt_cap = min(per, cap_amt, remain)
        k = int(amt_cap // unit)
        if k < 1 or k * unit < min_pos:
            out["unaffordable"].append({
                "code": e["code"], "name": e["name"], "unit": unit,
                "min_capital_single": round(unit / single_cap, 2) if single_cap else 0.0,
                "note": f"最小申购单位 {unit:,.0f} 元 > 单只预算 {amt_cap:,.0f} 元"})
            continue
        amt = round(k * unit, 2)
        chosen.append({**e, "unit": unit, "amount": amt,
                       "actual_weight": round(amt / capital, 4) if capital else 0.0,
                       "budget": round(per, 2)})
        spent += amt
        remain = round(remain - amt, 2)
    # 预算零头再按申购单位「补一手」到已选中的基金上（顺序固定 = 综合分序，保证可复现），
    # 这样 1,400 元预算不会凭空多留 200 元在现金层（现金 1.08% vs 短债 1.36%）。
    for pos in chosen:
        if remain <= 0:
            break
        unit = pos["unit"]
        while (remain >= unit and pos["amount"] + unit <= min(cap_amt, budget)
               and pos["amount"] + unit <= pos["budget"] * 1.25):
            pos["amount"] = round(pos["amount"] + unit, 2)
            pos["actual_weight"] = round(pos["amount"] / capital, 4) if capital else 0.0
            spent += unit
            remain = round(remain - unit, 2)
    out["positions"] = chosen
    out["spent"] = round(spent, 2)
    if chosen:
        # 「一手多少钱」不写死（2026-10-30 审计：文档写 1.1–1.2 万、这里硬编码 1.5 万，
        # 同一份计划书里出现两个一手价）。ETF 一手价随行市变，本函数只报「买不起」这一事实。
        out["note"] = (
            f"R2 预算 {budget:,.0f} 元：场内债券 ETF 一条都配不出（一手金额 > 单只上限 "
            f"{cap_amt:,.0f} 元）→ 由场外债券基金承接 {len(chosen)} 只、落地 {spent:,.0f} 元"
            f"（起投 max(购买起点,100) 元、取 100 元整数倍；"
            f"{_otc_fee_range_txt(chosen)}、{_otc_fee_txt(chosen)}；"
            f"剩余 {round(budget - spent, 2):,.0f} 元回现金层）")
    else:
        out["note"] = (f"R2 预算 {budget:,.0f} 元：场内买不起、场外也凑不出最小申购单位 "
                       f"{start:,.0f} 元 → 预算退回现金层")
    return out


def _otc_fallback(out: dict, pool: list[dict], budget: float, capital: float,
                  single_cap: float, allow_otc: bool = True) -> None:
    """ETF 腿一条都配不出时才调用：场外兜底承接同一份 R2 预算（两腿互斥）。"""
    if not allow_otc:
        # --no-otc：完全回到改造前的行为（note 一个字都不加，md 与改造前逐字一致）；
        # 「开关被关掉」这个事实只记在 JSON 的 otc_layer.enabled/pool_note 里，不污染计划文本。
        out["venue"] = "CASH"
        return
    res = otc_layer(pool, budget, capital, single_cap)
    out["venue"] = "OTC" if res["positions"] else "CASH"
    out["otc_positions"] = res["positions"]
    out["otc_unaffordable"] = res["unaffordable"]
    out["otc"] = {k: v for k, v in res.items() if k not in ("positions", "unaffordable")}
    out["spent"] = res["spent"]
    if res["positions"]:
        out["note"] = res["note"]
    else:
        out["note"] = f"{out.get('note') or ''}；{res['note']}".lstrip("；")


def build_otc_positions(sel: list[dict], capital: float) -> list[dict]:
    """把场外腿选中的基金翻译成计划持仓条目。

    这些条目进计划与报告；账本侧由 `otc_book.py` 按**影子账**记账：
    申购按提交日的下一交易日累计净值成交（T+1 确认），市值按「投入 × 累计净值比」算，
    赎回费按持有天数取实测档——所以账本净值里**包含**场外腿的收益。
    """
    rows = []
    for e in sel:
        amt = round(float(e.get("amount") or 0.0), 2)
        fee_rate = e.get("fee")
        fee_amt = round(amt * float(fee_rate or 0.0) / 100.0, 2)   # CSV 的 fee 是百分比
        nav = e.get("unit_nav")
        rows.append({
            "layer": "理财(R2)", "code": f"OTC:{e['code']}", "name": e["name"],
            "venue": "OTC", "fill_model": "otc_nav_t1",
            "industry": None, "verdict": "R2 场外债券基金", "probe": False,
            "score": e.get("score") or 0.0,
            "weight": round(amt / capital, 4) if capital else 0.0,
            "target_weight": round(amt / capital, 4) if capital else 0.0,
            "lots": 0,
            "shares": round(amt / nav, 2) if nav else 0.0,
            "lot_cost": round(float(e.get("unit") or 0.0), 2),
            "amount": amt, "price": nav,
            "nav_date": e.get("nav_date") or "", "unit_nav": nav,
            # 参考净值（累计口径）：建单日 otc.py 抓到的累计净值。单位净值会被分红/拆分压低，
            # 报告里必须同时展示，否则「单位净值 1.12、近 1 年 +2.9%」看着像自相矛盾。
            "nav_ref_cum": e.get("acc_nav"),
            "fee": fee_rate if fee_rate is not None else 0.0,
            "fee_amount": fee_amt,
            "min_purchase": e.get("min_purchase"),
            "redeem_fee_hold": e.get("redeem_fee_hold"),
            "buy_price": None, "target_price": None, "stop_price": None,
            "level": "R2",
            "level_reason": e.get("level_reason") or "场外债券基金（R2 固定收益类）",
            "etf_metrics": {"ret_1y": e.get("ret_1y"), "dd_1y": e.get("dd_1y"),
                            "ret_3y": e.get("ret_3y"), "dd_3y": e.get("dd_3y")},
            "thesis": (f"R2 场外债券基金（{e.get('key') or e['name']}，{e.get('ftype') or '债券型'}）："
                       f"近 1 年收益 {(e.get('ret_1y') or 0) * 100:+.2f}%、"
                       f"近 1 年最大回撤 {(e.get('dd_1y') or 0) * 100:.2f}%；"
                       f"起购 {e.get('min_purchase')} 元、申购费 {fee_rate if fee_rate is not None else 0}%、"
                       f"{_otc_fee_txt([e])}；按金额申购（不是整手）"),
            "falsifiers": [
                "近 1 年最大回撤超过 -3%（中低风险定位被破坏，按 R4 处理）",
                "申购状态转为暂停申购/封闭期（买不进，预算退回现金层）",
                "持有不足 7 天（赎回费最高 1.5%，短炒不划算）",
            ],
            "ladder": [],
        })
    return rows


def watch_position(a: dict) -> dict:
    """把「观察区」标的翻成计划条目：0 手 0 元，只占名单、不建仓（B 类 #5）。

    为什么权重直接写 0（`config.EXEC["watch_weight"]=0.0`）：观察区的意思是「分数到了
    候选线、没到买入线」，买它是把「还不错」当成「该买」；5 万本金下 0.5 权重会真的
    占掉 2,500 元预算，把这笔钱从买入区挤走。回滚只需把 `watch_weight` 改回 0.5，
    `pick()`/`allocate()` 会自动恢复「试探仓」行为，本函数只负责 0 权重这一支。

    仍然保留一手成本、买入价/止损价/目标价与论点：观察名单要能被复核，否则下周
    没人知道当初为什么只是看而没有买。
    """
    r = a["result"]
    x, pr = r.get("metrics") or {}, r.get("pricing") or {}
    th = r.get("thesis") or {}
    px = x.get("price")
    return {
        "layer": "个股", "code": r["code"], "name": r["name"],
        "industry": ind_of(r), "verdict": r["verdict"], "probe": False,
        "rank": a.get("rank"), "score": r["scores"].get("total"),
        "level": r.get("level") or "R5",
        "level_reason": r.get("level_reason") or "未评级（按 R3 处理）",
        "weight": 0.0, "target_weight": 0.0, "lots": 0, "shares": 0,
        "lot_cost": round(float(px) * config.CAPITAL["lot_size"], 2) if px else None,
        "amount": 0.0, "price": px,
        "buy_price": pr.get("buy_price"), "target_price": pr.get("target_price"),
        "stop_price": pr.get("stop_price"),
        "thesis": th.get("statement") or "", "falsifiers": th.get("falsifiers") or [],
        "ladder": [], "watch_only": True,
        "watch_note": ("观察区：总分未到买入线 "
                       f"{config.VERDICT['buy_total']}，本期只观察、不建仓"),
    }


def wealth_layer(pool: list[dict], budget: float, capital: float,
                 max_n: int | None = None, otc_pool: list[dict] | None = None,
                 allow_otc: bool = True) -> dict:
    """R2 层：用场内债券 ETF 承接「理财产品」预算。

    与 ETF 层的三点不同：
      1. 单只上限用 RISK.levels.R2.single_cap（12%），不是 EXEC.max_single_position（10%）——
         债券 ETF 一手 1.1–1.35 万元，5 万本金按 10% 永远买不了（见 engine-notes §五 全部阈值）；
      2. 不占行业额度（债券不分行业），且单只通常只买 1 手；
      3. 预算买不起一手时**不降标准、直接留现金层**，并写明需要多少本金。
    """
    cfgw = config.RISK["wealth"]
    lv = config.RISK["levels"]["R2"]
    single_cap = float(lv.get("single_cap") or 0.10)
    out = {"budget": round(budget, 2), "positions": [], "unaffordable": [],
           "note": "", "spent": 0.0, "single_cap": single_cap,
           "venue": "", "otc_positions": [], "otc_unaffordable": [], "otc": {}}
    if budget <= 0 or not pool:
        out["note"] = "无 R2 预算或无债券 ETF 候选"
        _otc_fallback(out, otc_pool or [], budget, capital, single_cap, allow_otc)
        return out

    min_pos = float(cfgw.get("min_position_value") or 800.0)
    cheapest = min((round(e["price"] * config.CAPITAL["lot_size"], 2) for e in pool),
                   default=0.0)
    n_by_budget = int(budget // min_pos)
    n_by_lot = int(budget // cheapest) if cheapest > 0 else 0
    n = max(0, min(int(max_n or cfgw.get("max_n") or 3), n_by_budget, n_by_lot))
    if n == 0:
        need = round(cheapest / single_cap, 2) if single_cap else 0.0
        out["note"] = (f"R2 预算 {budget:,.0f} 元买不起任何一手债券 ETF"
                       f"（最便宜一手 {cheapest:,.0f} 元；按 {single_cap * 100:.0f}% 单只上限"
                       f"需要本金 ≥ {need:,.0f} 元）→ 本期 R2 层由现金层（朝朝宝/逆回购）承担")
        _otc_fallback(out, otc_pool or [], budget, capital, single_cap, allow_otc)
        return out

    per = min(budget / n, capital * single_cap)
    chosen, spent = [], 0.0
    for e in pool:
        if len(chosen) >= n:
            break
        lc = round(e["price"] * config.CAPITAL["lot_size"], 2)
        cap_lots = int(capital * single_cap // lc)
        lots = min(int(round(per / lc)), int(per * 1.12 // lc), cap_lots)
        if lots < 1:
            out["unaffordable"].append({
                "sym": e["sym"], "name": e["name"], "lot_cost": lc,
                "min_capital_single": round(lc / single_cap, 2),
                "note": f"1 手需 {lc:,.0f} 元 > 单只预算 {per:,.0f} 元"})
            continue
        shares = lots * config.CAPITAL["lot_size"]
        amt = round(lots * lc, 2)
        chosen.append({**e, "lots": lots, "shares": shares, "lot_cost": lc,
                       "actual_amount": amt, "actual_weight": round(amt / capital, 4),
                       "budget": round(per, 2)})
        spent += amt
    out["positions"] = chosen
    out["spent"] = round(spent, 2)
    if chosen:
        out["note"] = (f"R2 预算 {budget:,.0f} 元 → 配 {len(chosen)} 只债券 ETF，"
                       f"实际落地 {spent:,.0f} 元（单只 ≈{per:,.0f} 元；"
                       f"单只上限 {single_cap * 100:.0f}%）")
    else:
        cheapest = min((round(e["price"] * config.CAPITAL["lot_size"], 2) for e in pool),
                       default=0.0)
        need = round(cheapest / single_cap, 2) if single_cap else 0.0
        out["note"] = (f"R2 预算 {budget:,.0f} 元买不起任何一手债券 ETF"
                       f"（最便宜一手 {cheapest:,.0f} 元；按 {single_cap * 100:.0f}% 单只上限"
                       f"需要本金 ≥ {need:,.0f} 元）→ 本期 R2 层由现金层（朝朝宝/逆回购）承担")
        _otc_fallback(out, otc_pool or [], budget, capital, single_cap, allow_otc)
    return out


# ------------------------------------------------------------------ 现金层
def build_cash_plan(capital: float, equity_spent: float, reserved: float = None,
                    ammo: float = None) -> dict:
    """现金层计划。

    挂单占用的现金（reserved）与**建仓弹药**（ammo = 权益上限 − 已排进的权益金额）
    都要留 T+0（要随时能成交），其余观望资金（idle）交给货基池。reserved 不传时从
    各账本 state 的挂单占款现算，保证计划/报告与账本口径一致（不把挂单的钱算成可投）；
    ammo 不传按 0（旧行为不变），阶段三主流程传「权益预算 − 计划已花权益」。
    """
    cash = round(capital - equity_spent, 2)
    if reserved is None:
        reserved = 0.0
        try:
            import paper as _paper
            for _k, _v in (config.PAPER.get("ledgers") or {}).items():
                _sf = (_v or {}).get("state_file")
                if _sf and os.path.exists(_sf):
                    reserved += _paper.reserved_cash(_paper.load(_sf))
        except Exception:
            reserved = 0.0
    plan = cap.cash_layer(cash, reserved=reserved, ammo=ammo)
    plan["equity_spent"] = round(equity_spent, 2)
    return plan


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description="阶段三：生成执行计划（本部署本金 5 万）")
    ap.add_argument("--detail", default=os.path.join(OUT, "stage2_detail.json"))
    ap.add_argument("--capital", type=float, default=50000.0,
                    help=f"可用本金，必须在 {config.CAPITAL['min']:,.0f}–"
                         f"{config.CAPITAL['max']:,.0f} 元之间")
    ap.add_argument("--tier", default=config.CAPITAL["default_tier"],
                    choices=sorted(config.CAPITAL["tiers"]), help="小资金档位 A/B/C")
    ap.add_argument("--override-regime", action="store_true",
                    help="允许档位突破市场状态仓位上限（属规则外做法，会在计划里标红）")
    ap.add_argument("--no-etf", action="store_true", help="不配 ETF 层")
    ap.add_argument("--no-wealth", action="store_true", help="不配 R2 理财层")
    ap.add_argument("--no-otc", action="store_true",
                    help="不读场外债券基金池（stage1_otc.csv）；R2 层只剩场内 ETF 一条腿")
    args = ap.parse_args()

    try:
        capital = cap.validate_capital(args.capital)
    except cap.CapitalRangeError as e:
        print(f"✗ {e}", file=sys.stderr)
        raise SystemExit(2)

    with open(args.detail, encoding="utf-8") as f:
        results = json.load(f)["results"]
    # 数据日期（价格数据截至哪一天）：报告头部要如实显示，防止把「缓存没更新」
    # 误读成「策略没变」——2026-10-26 用户质疑「怎么一直纠结一个股票」时，
    # 一部分原因就是同一天中午与晚上两次运行复用了同一份上一交易日数据。
    data_as_of = max((str((r.get("metrics") or {}).get("as_of") or "") for r in results),
                     default="")

    # ---- 风险等级闸：只留 R2/R3（个股按板块定级：沪深主板 R3；创业板/科创板/
    # 北交所 R4 不做；ST/退 R5；等级不再按波动率阈值降级——2026-10-26 用户纠正口径）----
    _RISK_DROPPED.clear()
    kept = []
    for r in results:
        lv = risk.stock_level(r.get("code"), r.get("name"), r.get("metrics") or {})
        r["level"], r["level_reason"] = lv["level"], lv["reason"]
        if risk.included(lv["level"]):
            kept.append(r)
        else:
            reason = lv["reason"]
            qkind = _RISK_QUAL.get(lv.get("board") or "")
            if qkind:
                _ok, acc_txt = risk.access_ok(qkind, capital)
                # 准入闸必须能否决：原来只把 acc_txt 拼进文案、ok 被丢掉，等于恒真（2026-10-30 审计 #26）
                if not _ok:
                    _RISK_DROPPED.append({"code": r["code"], "name": r["name"],
                                          "level": lv["level"], "board": lv.get("board"),
                                          "reason": f"{reason}；准入不满足：{acc_txt}"})
                    continue
                reason += f"；准入：{acc_txt}"
            _RISK_DROPPED.append({"code": r["code"], "name": r["name"],
                                  "level": lv["level"], "board": lv.get("board"),
                                  "reason": reason})
    results = kept

    # ---- 研究闸：消费 T2/T4 子代理的结论（reject 剔除 / downgrade 权重×0.5）
    results, _ri = apply_research(results, as_of=data_as_of)
    _RESEARCH_INFO.update(_ri)

    # ---- 新闻闸：消费 news.py 的 R/C 分（否决剔除 / 按档位乘权重系数）
    results, _ni = apply_news(results, as_of=data_as_of)
    _NEWS_INFO.update(_ni)

    regime = market_regime()
    tier = cap.tier_plan(capital, args.tier, regime["base_gross"],
                         override=args.override_regime)
    equity_budget = tier["equity_amount"]

    # ---- R2 理财层先占预算（r2_target=40% of 权益预算）----
    r2_target = float(config.RISK["portfolio"]["r2_target"])
    wealth_res = {"positions": [], "spent": 0.0, "budget": 0.0,
                  "note": "未启用（--no-wealth）", "unaffordable": [],
                  "otc_positions": [], "otc_pool_note": "未启用（--no-wealth）"}
    if not args.no_wealth:
        wpool, wnote = load_wealth_pool()
        r2_budget = round(equity_budget * r2_target, 2)
        # 场外池只作为 R2 的第二条腿：缺失/坏掉都不阻断主流程，note 会写进计划
        otc_pool, otc_note = (([], "未启用（--no-otc）") if args.no_otc
                              else load_otc_pool())
        # 去重铁律（见 SKILL.md §1「场外基金池 = R2 的第二条腿」）：两池 code 归一化后不得有交集，重复 fail loud
        check_pool_duplicates(wpool, otc_pool)
        wealth_res = wealth_layer(wpool, r2_budget, capital,
                                  otc_pool=otc_pool, allow_otc=not args.no_otc)
        wealth_res["pool_note"] = wnote
        wealth_res["otc_pool_note"] = otc_note
    r2_spent = round(float(wealth_res.get("spent") or 0.0), 2)
    r3_budget = round(max(0.0, equity_budget - r2_spent), 2)
    r3_frac = (r3_budget / capital) if capital else 0.0

    picked, pick_note = pick(results, regime, gross=r3_frac)
    alloc = allocate(picked, regime, gross=r3_frac)

    # ---- 个股：整手化 ----
    items, meta = [], {}
    watch_items = []
    for a in alloc:
        if a.get("cash"):
            continue
        if a.get("watch_only"):
            watch_items.append(a)
            continue
        r, w = a["result"], a["weight"]
        x, pr = r.get("metrics") or {}, r.get("pricing") or {}
        items.append({"code": r["code"], "name": r["name"], "price": x.get("price"),
                      "weight": w, "industry": r.get("industry"),
                      "verdict": r["verdict"], "probe": a["probe"],
                      "rank": a.get("rank")})
        meta[r["code"]] = r

    stock_rows = cap.lot_table(items, capital) if items else []
    positions, orders, unaffordable = [], [], []
    stock_spent = 0.0
    used_ind: dict = {}

    for row in stock_rows:
        r = meta[row["code"]]
        if not row["feasible"]:
            unaffordable.append({
                "code": row["code"], "name": row["name"],
                "price": row["price"], "lot_cost": row["lot_cost"],
                "budget": row["budget"],
                "min_capital_single": row["min_capital_single"],
                "min_capital_3tranche": row["min_capital_3tranche"],
                "note": row["note"]})
            continue
        pr, x = r.get("pricing") or {}, r.get("metrics") or {}
        pl, pstate = price_ladder(r)
        ll = cap.lot_ladder(row)
        ladder = []
        for pstep, lstep in zip(pl, ll):
            amt = round(lstep["lots"] * pstep["price"] * config.CAPITAL["lot_size"], 2)
            ladder.append({"tranche": pstep["tranche"], "ratio": pstep["ratio"],
                           "price": pstep["price"], "lots": lstep["lots"],
                           "shares": lstep["shares"], "amount": amt,
                           "merged_from": [],
                           "state": pstate if pstep["tranche"] == 1 else
                                    f"价格跌到 {pstep['price']} 元买第 {pstep['tranche']} 批"})
        # 单笔下限 5,000 元：买不起就合并批次（整手降级），绝不为了凑金额突破 10% 上限
        ladder = _merge_stock_ladder(
            ladder, float(config.PAPER.get("min_order_value") or 5000.0),
            int(config.CAPITAL["lot_size"]))
        for step in ladder:
            if step["lots"] <= 0:
                continue
            orders.append({"layer": "个股", "code": row["code"], "name": row["name"],
                           "action": "限价买入" if step["tranche"] == 1 else "条件买入",
                           "tranche": step["tranche"], "price": step["price"],
                           "lots": step["lots"], "shares": step["shares"],
                           "amount": step["amount"], "is_etf": False})
        stock_spent += row["actual_amount"]
        ind = ind_of(row)
        used_ind[ind] = used_ind.get(ind, 0.0) + row["actual_weight"]
        pos = {
            "layer": "个股", "code": row["code"], "name": row["name"],
            "industry": ind_of(row), "verdict": row["verdict"],
            "probe": row["probe"], "rank": row.get("rank"),
            "score": r["scores"].get("total"),
            "level": r.get("level") or "R5",
            "level_reason": r.get("level_reason") or "未评级（按 R3 处理）",
            "weight": row["actual_weight"], "target_weight": row["weight"],
            "lots": row["lots"], "shares": row["shares"],
            "lot_cost": row["lot_cost"], "amount": row["actual_amount"],
            "price": x.get("price"), "buy_price": pr.get("buy_price"),
            "target_price": pr.get("target_price"), "stop_price": pr.get("stop_price"),
            "thesis": r["thesis"]["statement"],
            "falsifiers": r["thesis"]["falsifiers"],
            "ladder": ladder,
        }
        positions.append(pos)

    # ---- 观察区占位：0 手 0 元，只进计划/报告，不生成订单、不占预算 ----
    for a in watch_items:
        positions.append(watch_position(a))

    # ---- ETF 层：承接个股买不了的预算 ----
    etf_res = {"positions": [], "note": "未启用（--no-etf）", "budget": 0.0}
    if not args.no_etf:
        pool, pool_note = load_etf_pool()
        etf_budget = max(0.0, round(r3_budget - stock_spent, 2))
        etf_res = etf_layer(pool, etf_budget, capital, used_ind,
                            etf_budget_ratio=etf_budget / capital if capital else 0.0)
        etf_res["pool_note"] = pool_note
        for row in etf_res["positions"]:
            e = row
            px = e["price"]
            ladder = []
            for step in etf_ladder_steps(px, e["lots"]):
                state = ("现价即限价（ETF 无估值锚，按现价挂一张限价单，开盘不高于该价即成交）"
                         if step["tranche"] == 1 and abs(step["price"] - px) < 1e-9 else
                         f"价格跌到 {step['price']} 元买第 {step['tranche']} 批")
                # merge_note 由 to_markdown 单独渲染成 bullet，这里不再拼进 state，
                # 否则报告里同一句会重复两遍（2026-11-07 B 类 #8）。
                ladder.append({**step, "state": state})
                # 2026-11-18 起买入一律限价单：第 1 批限价 = 现价，第 2/3 批限价 = 计划价。
                action = ("限价买入" if step["tranche"] == 1 and abs(step["price"] - px) < 1e-9
                          else "条件买入")
                orders.append({"layer": "ETF", "code": e["sym"], "name": e["name"],
                               "action": action,
                               "tranche": step["tranche"], "price": step["price"],
                               "lots": step["lots"], "shares": step["shares"],
                               "amount": step["amount"], "is_etf": True})
            positions.append({
                "layer": "ETF", "code": e["sym"], "name": e["name"],
                "industry": e["ind_key"], "verdict": e["cls"], "probe": False,
                "score": e["score"], "weight": e["actual_weight"],
                "target_weight": e["weight"], "lots": e["lots"],
                "shares": e["shares"], "lot_cost": e["lot_cost"],
                "amount": e["actual_amount"], "price": e["price"],
                "level": e.get("level") or "R5",
                "level_reason": e.get("level_reason") or "宽基/策略 ETF（中风险）",
                "buy_price": None, "target_price": None, "stop_price": None,
                "etf_metrics": {"ret_3y": e.get("ret_3y"), "dd_3y": e.get("dd_3y"),
                                "amt20_yi": e.get("amt20_yi"),
                                "vs_ma200": e.get("vs_ma200")},
                "thesis": (f"{e['cls']} ETF，跟踪 {e.get('underlying', e['name'])}；"
                           f"近 3 年收益 {(e.get('ret_3y') or 0) * 100:+.1f}%、"
                           f"近 3 年最大回撤 {(e.get('dd_3y') or 0) * 100:.1f}%、"
                           f"20 日均成交 {(e.get('amt20_yi') or 0):.2f} 亿"),
                "falsifiers": [
                    "近 20 日均成交额跌到 3000 万以下（流动性恶化，买得进卖不出）",
                    "跟踪指数相对沪深300 连续 2 个季度跑输且份额持续净赎回",
                    "单只 ETF 权重超过本金 10%（超限即减回）",
                ],
                "ladder": ladder,
            })

    # ---- R2 理财层：场内债券 ETF（占权益预算的 r2_target）----
    r2_positions = []
    for e in wealth_res.get("positions") or []:
        px = e["price"]
        # 2026-11-07 B 类 #8：债券 ETF 与权益 ETF 共用 `ETF.tranche_mode`——默认单批，
        # 回滚 "three" 时保持原来的 40/35/25 三批；一手 ≈1.1 万元，碎批问题比权益 ETF 轻，
        # 但同一层里混用两种批次口径会让报告与账本对不上，所以一并参数化。
        _single = str((config.ETF or {}).get("tranche_mode") or "three") == "single"
        if _single:
            pl = [{"tranche": 1, "ratio": 1.0, "price": round(px, 3)}]
            ll = [{"tranche": 1, "ratio": 1.0, "lots": int(e["lots"]),
                   "shares": int(e["shares"])}]
        else:
            pl = [{"tranche": 1, "ratio": 0.40, "price": round(px, 3)},
                  {"tranche": 2, "ratio": 0.35, "price": round(px * 0.98, 3)},
                  {"tranche": 3, "ratio": 0.25, "price": round(px * 0.96, 3)}]
            ll = cap.lot_ladder(e)
        ladder = []
        for pstep, lstep in zip(pl, ll):
            amt = round(lstep["lots"] * pstep["price"] * config.CAPITAL["lot_size"], 2)
            ladder.append({"tranche": pstep["tranche"], "ratio": pstep["ratio"],
                           "price": pstep["price"], "lots": lstep["lots"],
                           "shares": lstep["shares"], "amount": amt,
                           "state": ("现价即限价（债券 ETF 无估值锚，按现价挂限价单一次买入）"
                                     if pstep["tranche"] == 1 and _single else
                                     "现价即限价（债券 ETF 无估值锚，按现价挂限价单分批）"
                                     if pstep["tranche"] == 1 else
                                     f"价格跌到 {pstep['price']} 元买第 {pstep['tranche']} 批")})
            if lstep["lots"] > 0:
                orders.append({"layer": "理财(R2)", "code": e["sym"], "name": e["name"],
                               "action": "限价买入" if pstep["tranche"] == 1 else "条件买入",
                               "tranche": pstep["tranche"], "price": pstep["price"],
                               "lots": lstep["lots"], "shares": lstep["shares"],
                               "amount": amt, "is_etf": True})
        r2_positions.append({
            "layer": "理财(R2)", "code": e["sym"], "name": e["name"],
            "industry": None, "verdict": "R2 债券ETF", "probe": False,
            "score": e["score"], "weight": e["actual_weight"],
            "target_weight": round(e["budget"] / capital, 4),
            "lots": e["lots"], "shares": e["shares"], "lot_cost": e["lot_cost"],
            "amount": e["actual_amount"], "price": e["price"],
            "buy_price": None, "target_price": None, "stop_price": None,
            "level": "R2", "level_reason": e.get("level_reason") or "场内债券 ETF",
            "etf_metrics": {"ret_1y": e.get("ret_1y"), "dd_1y": e.get("dd_1y"),
                            "amt20_yi": e.get("amt20_yi")},
            "thesis": (f"R2 理财产品层：场内债券 ETF（{e.get('key', e['name'])}），"
                       f"近 1 年收益 {(e.get('ret_1y') or 0) * 100:+.1f}%、"
                       f"近 1 年最大回撤 {(e.get('dd_1y') or 0) * 100:.2f}%、"
                       f"20 日均成交 {(e.get('amt20_yi') or 0):.2f} 亿；"
                       f"承担组合的中低风险部分（目标占权益预算 {r2_target * 100:.0f}%）"),
            "falsifiers": [
                "近 20 日均成交额跌到 3000 万以下（流动性恶化，买得进卖不出）",
                "近 1 年最大回撤超过 -3%（中低风险定位被破坏，按 R4 处理）",
                "份额出现折价且持续 5 个交易日以上（申赎异常）",
            ],
            "ladder": ladder,
        })
    # ---- R2 场外兜底：进计划/报告，账本由 otc_book 的影账接手（T+1 确认净值）----
    otc_positions = build_otc_positions(wealth_res.get("otc_positions") or [], capital)
    positions = r2_positions + positions

    # ---- 现金增值层 ----
    equity_spent = round(sum(p["amount"] for p in positions)
                         + sum(p["amount"] for p in otc_positions), 2)
    cash_plan = build_cash_plan(
        capital, equity_spent,
        ammo=max(0.0, round(equity_budget - equity_spent, 2)))

    # ---- 组合风险评审（R2/R3 闸门，ERROR 时 fail-closed 不出订单）----
    risk_items = []
    for p in positions:
        w = (p["amount"] / capital) if capital else 0.0
        risk_items.append({"code": p["code"], "name": p["name"],
                           "level": p.get("level") or "R5", "weight": round(w, 6)})
    for p in otc_positions:      # 场外持仓也计入 R2 占比，否则评审会把 R2 占比算低
        w = (p["amount"] / capital) if capital else 0.0
        risk_items.append({"code": p["code"], "name": p["name"],
                           "level": "R2", "weight": round(w, 6)})
    review = risk.portfolio_review(risk_items)
    if not review["ok"]:
        review["note"] = "组合评审未通过（见 violations）→ 本期不生成新订单（fail-closed）"
        orders = []
    review["filtered_n"] = len(_RISK_DROPPED)
    if not risk_items:
        review["note"] = "本期无持仓（所有候选被否决或买不起）→ 无新订单，不是「评审通过」"
        orders = []

    # ---- 费用测算 ----
    fees = cap.fee_summary(orders, capital)

    gross = round(equity_spent / capital, 4) if capital else 0.0
    plan = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "capital": capital,
        "capital_check": cap.describe(capital),
        "tier": tier,
        "rule_override": bool(args.override_regime and tier["tier_ratio"] > tier["regime_cap"]),
        "market_regime": regime,
        "pick_note": pick_note,
        "data_as_of": data_as_of,
        "stock_weighting": config.EXEC.get("stock_weighting", "rank"),
        "max_stock_names": int(_names_for_gross(r3_frac)),
        "rank_weights": list(config.EXEC.get("rank_weights") or []),
        "target_gross": gross,
        "equity_spent": equity_spent,
        "positions": positions,
        "unaffordable": unaffordable,
        "etf_layer": {k: v for k, v in etf_res.items() if k != "positions"},
        "etf_positions": [p for p in positions if p["layer"] == "ETF"],
        "wealth_layer": {k: v for k, v in wealth_res.items()
                         if k not in ("positions", "otc_positions")},
        "wealth_positions": r2_positions + otc_positions,
        "otc_layer": {
            "enabled": bool(not args.no_otc and not args.no_wealth),
            "venue": wealth_res.get("venue") or "",
            "pool_note": wealth_res.get("otc_pool_note") or "",
            "summary": wealth_res.get("otc") or {},
            "spent": round(sum(p["amount"] for p in otc_positions), 2),
            "fill_model": "otc_nav_t1" if otc_positions else "",
            "redeem_fee_rule": _otc_fee_rule_txt() if otc_positions else "",
            "paper_note": (
                "模拟账本按影子账记账（`otc_book.py`）：申购按提交日的下一交易日累计净值成交、"
                "T+1 确认；市值 = 投入 ×（当日累计净值 / 买入日累计净值）；赎回费按持有天数取实测档。"
                "净值序列缺失时挂单保持 pending（fail-closed），绝不编价格。"
                "真实操作请在基金 APP 按计划金额申购，APP 确认会比账本晚 1–2 个交易日。") if otc_positions else "",
        },
        "otc_positions": otc_positions,
        "risk_review": review,
        "risk_filter": list(_RISK_DROPPED),
        "research_filter": dict(_RESEARCH_INFO),
        "news_filter": dict(_NEWS_INFO),
        "cash_layer": cash_plan,
        "fee_summary": fees,
        "orders": orders,
        "risk_controls": {
            "单票上限": config.EXEC["max_single_position"],
            "行业上限": config.EXEC["max_industry"],
            "总敞口区间": [config.EXEC["min_gross"], config.EXEC["max_gross"]],
            "组合回撤熔断": config.EXEC["portfolio_dd_halt"],
            "单票止损": config.EXEC["stop_loss_pct"],
            "时间止损": f"{config.EXEC['time_stop_months']} 个月",
            "日亏停手": -0.03, "周亏减仓": -0.05, "连亏3笔停手": "24 小时",
            "止损后现金不重新分配": True,
        },
    }

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "stage3_plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    order_cols = ["layer", "code", "name", "action", "tranche", "price",
                  "lots", "shares", "amount", "is_etf"]
    pd.DataFrame(orders, columns=order_cols).to_csv(
        os.path.join(OUT, "stage3_orders.csv"), index=False, encoding="utf-8-sig",
        float_format="%.3f")
    with open(os.path.join(OUT, "stage3_plan.md"), "w", encoding="utf-8") as f:
        f.write(to_markdown(plan))

    print(f"[阶段三] 市场状态：{regime['regime']}（上限 {regime['base_gross'] * 100:.0f}%）"
          f"｜档位 {tier['tier']}：权益 {tier['equity_ratio'] * 100:.1f}% "
          f"= {tier['equity_amount']:,.0f} 元")
    print(f"          {pick_note}")
    _r2_amt = sum(p["amount"] for p in r2_positions)
    _otc_amt = sum(p["amount"] for p in otc_positions)
    if r2_positions:
        _r2_etf_n = len([x for x in r2_positions if (x.get("venue") or "ETF") != "OTC"])
        _r2_otc_n = len(r2_positions) - _r2_etf_n
        _r2_desc = (f"{_r2_etf_n} 只债券 ETF" if _r2_etf_n else "") or ""
        if _r2_otc_n:
            _r2_desc = (_r2_desc + " + " if _r2_desc else "") + f"{_r2_otc_n} 只场外债券基金"
        print(f"          R2 理财层：{_r2_desc} 落地 {_r2_amt:,.0f} 元")
    elif otc_positions:
        print(f"          R2 理财层：场内 ETF 一手买不起 → 场外 {len(otc_positions)} 只债券基金 "
              f"落地 {_otc_amt:,.0f} 元（账本按影账 T+1 确认净值成交）")
    elif wealth_res.get("note"):
        print(f"          R2 理财层：{wealth_res['note']}")
    print(f"          风险评审：{risk.summary_line(review)}")
    if unaffordable:
        _to = "已转 ETF/现金" if not args.no_etf else "已全部转现金（--no-etf）"
        print(f"          个股买不了 {len(unaffordable)} 只（1 手超单票预算）{_to}")
    for p in positions:
        tag = "个股" if p["layer"] == "个股" else "ETF "
        print(f"  {tag} {p['code']:<8} {p['name']:<12} {p['lots']:>2} 手 ≈{p['amount']:>8,.0f} 元  "
              f"{p['weight'] * 100:>4.1f}%  [{p['verdict']}]")
    for p in otc_positions:
        print(f"  场外 {p['code']:<12} {p['name']:<12} 金额 ≈{p['amount']:>8,.0f} 元  "
              f"{p['weight'] * 100:>4.1f}%  [R2 场外，影账 T+1 确认]")
    print(f"  现金 {cash_plan['cash_total']:,.0f} 元：T+0 保留 "
          f"{cash_plan.get('t0_keep', 0):,.0f}（建仓弹药 {cash_plan.get('ammo_amount', 0):,.0f}"
          f" / 挂单冻结 {cash_plan.get('reserved_amount', 0):,.0f}）+ 货基池 "
          f"{cash_plan.get('sleeve_amount', 0):,.0f} + 现金侧 {cash_plan['money_fund_amount']:,.0f}"
          f"（容器 {cash_plan.get('container')}：{cash_plan.get('note', '')}）")
    print(f"  一次性买入费用约 {fees['buy_fee']:,.0f} 元；输出: "
          f"{os.path.join(OUT, 'stage3_plan.md')}")


def to_markdown(plan: dict) -> str:
    reg = plan["market_regime"]
    tier = plan["tier"]
    cc = plan["capital_check"]
    cash = plan["cash_layer"]
    fees = plan["fee_summary"]
    stocks = [p for p in plan["positions"] if p["layer"] == "个股"]
    etfs = [p for p in plan["positions"] if p["layer"] == "ETF"]
    rv = plan.get("risk_review") or {}

    L = ["# 阶段三：执行计划", "",
         f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}　"
         f"本金：{plan['capital']:,.0f} 元", ""]
    if plan.get("rule_override"):
        L += ["> ⚠️ **本计划由 `--override-regime` 生成：权益比例突破了市场状态上限 "
              f"（档位 {tier['tier_ratio'] * 100:.0f}% > 上限 {tier['regime_cap'] * 100:.0f}%）。"
              "这是**规则外**做法，回撤会超出风险预算，请自行确认。", ""]

    L += ["## 一、本金覆盖区间与档位", "",
          f"- {cc['note']}",
          f"- 档位 **{tier['tier']}（{tier['tier_label']}）**：目标权益 "
          f"{tier['tier_ratio'] * 100:.0f}%，实际 **{tier['equity_ratio'] * 100:.1f}%** "
          f"= {tier['equity_amount']:,.0f} 元；现金 {tier['cash_amount']:,.0f} 元",
          f"- {tier['note']}", ""]

    L += ["## 二、市场状态", "",
          f"- 当前判定：**{reg['regime']}**，市场状态给的仓位上限 **{reg['base_gross'] * 100:.0f}%**"
          + (f"（牛熊探针：{reg.get('probe_state')} / 立场 {reg.get('probe_stance')}"
             f"（{reg.get('probe_severity')}）；MA200 口径为 {reg.get('dev_regime')} "
             f"{reg.get('dev_gross', 0) * 100:.0f}%）" if reg.get("probe_state") else ""),
          f"- {reg['note']}", ""]

    L += ["## 三、个股（已按整手取整）", "",
          f"> {plan.get('pick_note', '')}"]
    if plan.get("stock_weighting") == "conviction":
        L += ["", "> 选股口径：conviction（核心仓 1.0 / 试探仓 0.5）。"]
    L.append("")
    if not stocks:
        L += ["> 本期没有「买得起一手」的个股，权益预算全部交给 ETF 层（见第四节）。", ""]
    else:
        L += ["| 代码 | 名称 | 行业 | 判断 | 得分 | 目标权重 | 实际权重 | 1 手成本 | 手数 | 实际金额 | 现价 | 买入价 | 止损价（占位） | 目标价 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in stocks:
            if p.get("rank"):
                flag = f"（第{p['rank']}名）"
            else:
                flag = "（试探）" if p.get("probe") else ""
            L.append(f"| {p['code']} | {p['name']}{flag} | {p['industry']} | {p['verdict']} | "
                     f"{p['score']} | {p['target_weight'] * 100:.1f}% | "
                     f"{p['weight'] * 100:.1f}% | {p['lot_cost']:,.0f} | {p['lots']} | "
                     f"{p['amount']:,.0f} | {p['price']} | {p['buy_price']} | "
                     f"{p['stop_price']} | {p['target_price']} |")
        L.append("")
    if plan["unaffordable"]:
        L += ["### 买不了一手的个股（不是不好，是本金不够整手）", "",
              "| 代码 | 名称 | 现价 | 1 手成本 | 单票预算 | 让 1 手不超 10% 需本金 |",
              "|---|---|---|---|---|---|"]
        for u in plan["unaffordable"]:
            L.append(f"| {u['code']} | {u['name']} | {u['price']} | {u['lot_cost']:,.0f} | "
                     f"{u['budget']:,.0f} | {u['min_capital_single']:,.0f} |")
        L += ["", "> 结论：这些标的不是被策略否掉，而是「1 手金额 > 单票预算」。"
              "要么加本金到上表最后一列，要么用 ETF 承接同一份权益暴露。", ""]

    L += ["## 四、ETF 层（承接个股买不了的预算）", "",
          f"- {plan['etf_layer'].get('pool_note', '')}",
          f"- {plan['etf_layer'].get('note', '')}", ""]
    if etfs:
        L += ["| 代码 | 名称 | 类型 | 综合分 | 权重 | 1 手成本 | 手数 | 实际金额 | 现价 | 近3年收益 | 近3年回撤 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in etfs:
            L.append(f"| {p['code']} | {p['name']} | {p['verdict']} | {p['score']} | "
                     f"{p['weight'] * 100:.1f}% | {p['lot_cost']:,.0f} | {p['lots']} | "
                     f"{p['amount']:,.0f} | {p['price']} | "
                     f"{_pct(_etf_val(p, 'ret_3y'))} | {_pct(_etf_val(p, 'dd_3y'))} |")
        L.append("")
    for u in plan["etf_layer"].get("unaffordable", []) or []:
        L.append(f"- {u['sym']} {u['name']}：{u['note']}")
    if etfs or plan["etf_layer"].get("unaffordable"):
        L.append("")

    # ---- R2 理财层（场内 ETF 优先 / 场外债券基金兜底，两腿互斥）----
    wl = plan.get("wealth_layer") or {}
    r2p = plan.get("wealth_positions") or []
    otcp = plan.get("otc_positions") or []
    r2_etf = [p for p in r2p if p.get("venue") != "OTC"]
    # --no-otc / --no-wealth 时标题与场外说明行都不出现，保证与改造前逐字一致
    otc_on = bool((plan.get("otc_layer") or {}).get("enabled"))
    L += [("## 五、R2 理财层（场内债券 ETF 优先 / 场外债券基金兜底）" if otc_on
           else "## 五、R2 理财层（场内债券 ETF）"), "",
          f"- 技能只做 **R2/R3**（R1 由现金层承担，R4/R5 不做）；R2 目标占权益预算 "
          f"**{config.RISK['portfolio']['r2_target'] * 100:.0f}%** = {wl.get('budget', 0):,.0f} 元，"
          f"单只上限 {wl.get('single_cap', 0) * 100:.0f}%",
          f"- {wl.get('pool_note', '')}"]
    if otc_on:
        L.append(f"- {wl.get('otc_pool_note', '')}")
    L += [f"- {wl.get('note', '')}", ""]
    if r2_etf:
        L += ["| 代码 | 名称 | 综合分 | 权重 | 1 手成本 | 手数 | 实际金额 | 近1年收益 | 近1年回撤 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for p in r2_etf:
            em = p.get("etf_metrics") or {}
            L.append(f"| {p['code']} | {p['name']} | {p['score']} | {p['weight'] * 100:.1f}% | "
                     f"{p['lot_cost']:,.0f} | {p['lots']} | {p['amount']:,.0f} | "
                     f"{_pct(em.get('ret_1y'))} | {_pct(em.get('dd_1y'))} |")
        L.append("")
    if otcp:
        L += ["### 场外债券基金（承接 R2 预算；账本按影账 T+1 确认净值成交）", "",
              "| 代码 | 名称 | 金额 | 起购 | 申购费率 | 单位净值 | 参考净值（累计） | 净值日期 | 近1年收益 | 近1年回撤 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for p in otcp:
            em = p.get("etf_metrics") or {}
            nav = p.get("unit_nav")
            cum = p.get("nav_ref_cum")
            L.append(f"| {p['code']} | {p['name']} | {p['amount']:,.0f} | "
                     f"{(p.get('min_purchase') or 0):,.0f} | {(p.get('fee') or 0):.2f}% | "
                     f"{nav if nav is not None else '—'} | "
                     f"{cum if cum is not None else '—'} | {p.get('nav_date') or '—'} | "
                     f"{_pct(em.get('ret_1y'))} | {_pct(em.get('dd_1y'))} |")
        L += ["", "> **两列净值都要看**：成交与市值一律按**累计净值**（金额法：市值 = 投入 × 当日累计净值 / "
              "买入日累计净值）；单位净值会被分红/拆分压低，只看单位净值会误以为亏了。", ""]
        L += ["", "> **场外腿怎么进账本**：模拟盘用**影子账**（`otc_book.py`）按"
              "「提交日的下一交易日累计净值」成交、T+1 确认；市值为「投入 × 累计净值比」，"
              "赎回费按持有天数取实测档。真实下单请在基金 APP 按上表金额申购，"
              "APP 的确认净值与确认日通常比账本晚 1–2 个交易日。", ""]
    for u in wl.get("unaffordable", []) or []:
        L.append(f"- {u['sym']} {u['name']}：{u['note']}（需本金 ≥ "
                 f"{u.get('min_capital_single', 0):,.0f} 元）")
    for u in wl.get("otc_unaffordable", []) or []:
        L.append(f"- 场外 {u['code']} {u['name']}：{u['note']}（需本金 ≥ "
                 f"{u.get('min_capital_single', 0):,.0f} 元）")
    if r2_etf or otcp or wl.get("unaffordable") or wl.get("otc_unaffordable"):
        L.append("")
    if otcp:
        # 风险数字**从所选基金的数据算**，不写死。早期这里抄的是「中短债类型中位数 1.36%/回撤 -0.43%」，
        # 但那是我 `--limit 3` 中间产物里的数，和最终 1,087 只全量池（ret_3y 中位 2.35%）对不上，
        # 也和本次所选 3 只（3.08%/3.64%/3.22%）对不上——写死数字必然说谎，改成现算。
        _r3 = [(x.get("etf_metrics") or {}).get("ret_3y") for x in otcp]
        _d3 = [(x.get("etf_metrics") or {}).get("dd_3y") for x in otcp]
        _r3 = [float(v) for v in _r3 if v is not None]
        _d3 = [float(v) for v in _d3 if v is not None]
        _risk_txt = ""
        if _r3 and _d3:
            _risk_txt = (f"（所选 {len(_r3)} 只近 3 年年化 {min(_r3) * 100:.2f}%–{max(_r3) * 100:.2f}%、"
                         f"3 年最大回撤最深 {min(_d3) * 100:.2f}%）")
        L += ["> 为什么这次用场外债券基金当「理财产品」：5 万本金下 R2 预算约 7,000 元，"
              "而一手场内债券 ETF 约 1.1–1.2 万元、单只上限 12%×5 万 = 6,000 元 < 一手 → 场内配不出。"
              f"本次所选 {len(otcp)} 只：{_otc_fee_range_txt(otcp)}、{_otc_fee_txt(otcp)}"
              "（费用已计入 otc.py 的 cost_1y 口径）；场外债基起购门槛通常只有 10 元"
              "（本池 99.5% 为 10 元起），所以小资金下 R2 层能真的配出去、而不是整层退回现金层"
              f"{_risk_txt}。场内买得起时优先场内，两条腿互斥。", "",
              f"> {_otc_fee_rule_txt()}", ""]
    else:
        L += ["> 为什么 R2 优先用品内债券 ETF：场内可交易、有实时价、能按当日收盘算回撤，"
              "费率和规模都可溯源；银行 R2 理财没有可靠的 point-in-time 数据"
              "（写进策略等于编数字），不参与筛选。场内一手买不起时由场外债券基金承接"
              "（本次未启用或池为空，原因见上）。", ""]

    _min_order = float(config.PAPER.get("min_order_value") or 5000.0)
    _etf_mode = str(config.ETF.get("tranche_mode") or "three")
    _etf_batch = ("ETF 单批一次买入（`ETF.tranche_mode=single`，可回滚 `three`）"
                  if _etf_mode == "single" else "ETF 按 40% / 35% / 25% 三批")
    L += [f"## 六、建仓批次（单笔 ≥{_min_order:,.0f} 元；买不起按手数合并）", "",
          f"> 个股按 40% / 35% / 25% 三批挂价格条件单；单批金额 < {_min_order:,.0f} 元时"
          "自动并入相邻批（最少 1 批、一次买入）。**整手化与单票 10% 上限不变**："
          "先保证一手买得起，再保证单票不超 10%，最后才要求单笔够大。"
          f"{_etf_batch}：5 万本金下 ETF 单只 ≈2,900 元，拆三批的尾批 500–700 元，"
          "5 元最低佣金会把碎单单边成本从 0.1% 抬到 0.87%。", ""]
    _acc = getattr(config, "ACCOUNT", {})
    if _acc.get("order_window"):
        _w0, _w1 = _acc["order_window"]
        L += [f"> **下单窗口 {_w0}–{_w1}**（收盘后）→ 下一交易日执行："
              f"**卖出**（减仓/清仓/止损）按下一交易日开盘价成交；"
              f"**买入一律限价条件单**（跌到限价才成交，`limit_expire_days` 个交易日后失效，2026-11-18 起）。"
              f"本表的止损价是**计划占位价**（安全边际买入价×0.85），成交后按实际成交成本×0.85 重设。", ""]
    for p in plan["positions"]:
        if p.get("watch_only"):
            L += [f"**{p['name']}（{p['code']}・观察区）** 总分 {p.get('score')} 未到买入线 "
                  f"{config.VERDICT['buy_total']}，本期**不建仓**（0 手、0 元）；"
                  f"一手需 {p.get('lot_cost') or 0:,.0f} 元。{p.get('watch_note') or ''}", ""]
            if p.get("thesis"):
                L += ["> 观察理由：" + p["thesis"], ""]
            continue
        L += [f"**{p['name']}（{p['code']}・{p['layer']}）** 目标 {p['target_weight'] * 100:.1f}% "
              f"≈ {p['amount']:,.0f} 元，共 {p['lots']} 手", ""]
        for step in p["ladder"]:
            if step["lots"] == 0:
                L.append(f"- 第 {step['tranche']} 批：**跳过**（{step['price']} 元时不足 1 手，该批次预算留在现金层）")
                continue
            L.append(f"- 第 {step['tranche']} 批：**{step['price']} 元** × "
                     f"{step['lots']} 手（{step['shares']} 份）≈ {step['amount']:,.0f} 元 —— "
                     f"{step['state']}")
            if step.get("merge_note"):
                L.append(f"  - {step['merge_note']}")
        if p["layer"] == "个股":
            L += ["", "> 论点依据：" + p["thesis"], "", "> 出现以下任一情况，无论盈亏立即离场："]
            L += [f"> - {x}" for x in p["falsifiers"]]
        L.append("")
    if otcp:
        L += ["### 场外申购不分批（按金额，不是整手）", "",
              "场外基金按金额申购、T+1 确认，不能像场内那样拆 3 批挂价格条件单；"
              "本计划按「起投 max(购买起点,100) 元、取 100 元整数倍」一次性申购第五节上表金额。"
              "想分两次，可在第一次确认后自行拆单（申购费按比例收，拆单不省费）。", ""]

    _is_pool = str(cash.get("container")) == "policy_pool"
    L += ["## 七、现金增值层（挂单资金 + 建仓弹药→T+0，观望资金→全市场货基池）", "",
          f"- 现金合计 **{cash['cash_total']:,.0f} 元**（占本金 "
          f"{cash['cash_total'] / plan['capital'] * 100:.0f}%）"]
    _side = cash.get("cash_side_name") or "朝朝宝/货基"
    _t0 = float(cash.get("t0_keep") or 0.0)
    if _t0:
        L.append(f"- T+0 保留 **{_t0:,.0f} 元**（建仓弹药 "
                 f"{float(cash.get('ammo_amount') or 0):,.0f} + 挂单冻结 "
                 f"{float(cash.get('reserved_amount') or 0):,.0f}，取较大者）→ 留{_side}，"
                 "随时可成交/买股，不锁货基")
    if _is_pool:
        _hold = cash.get("sleeve_holdings") or []
        L.append(f"- 全市场货基池：**{float(cash.get('sleeve_amount') or 0):,.0f} 元**"
                 f"（加权 {float(cash.get('sleeve_rate') or 0) * 100:.2f}%，数据日 "
                 f"{cash.get('sleeve_data_date') or '-'}，每支 ≤1 万快赎）")
        for _h in _hold:
            L.append(f"  - {_h.get('name')} {float(_h.get('amount') or 0):,.0f} 元"
                     f"（90 天实得年化 {float(_h.get('rate') or 0) * 100:.2f}%）")
    elif cash.get("repo_lots"):
        L.append(f"- 国债逆回购（旧口径回滚）：**{cash['repo_lots']} 手 "
                 f"= {cash['repo_amount']:,.0f} 元**，{int(cash.get('repo_term_days') or 7)} 天期")
    L += [f"- {_side}侧：**{cash['money_fund_amount']:,.0f} 元**"
          f"（{cash['money_fund_net_rate'] * 100:.2f}%），预期 "
          f"{cash['money_fund_gross_year']:,.0f} 元/年，0 手续费",
          f"- 合计预期约 **{cash['total_net_year']:,.0f} 元/年（{cash['net_yield'] * 100:.2f}%）**，"
          f"这是「不承担权益风险」的部分，不是承诺收益",
          f"- {cash['note']}",
          "", "> 操作要点：观望资金进全市场货基池、每 30 天按 90 天实得年化重排一次；"
          "挂单资金与建仓弹药不能进池（周末赎不出、周一一早要能买），留朝朝宝 T+0；"
          "逆回购只在季末/长假前的脉冲窗口做（见 §5.7c），不是每周固定操作。", ""]
    if otcp:
        L += [f"> 现金层已扣除场外申购金额 {sum(p['amount'] for p in otcp):,.0f} 元"
              "（场外申购按金额划走、T+1 确认，确认前不在现金也不在份额里）。", ""]

    L += ["## 八、费用测算（小资金的真摩擦）", "",
          f"- 一次性买入佣金等合计约 **{fees['buy_fee']:,.0f} 元**"
          f"（占本金 {fees['buy_fee'] / plan['capital'] * 100:.2f}%）",
          f"- 来回（买+卖）合计约 **{fees['round_trip_fee']:,.0f} 元**"
          f"（占本金 {fees['round_trip_pct_of_capital'] * 100:.2f}%）",
          f"- {fees['note']}"]
    if fees["high_fee_orders"]:
        L += ["", "| 高佣金订单（佣金 >0.5%） | 金额 | 佣金 | 占比 |", "|---|---|---|---|"]
        for h in fees["high_fee_orders"]:
            L.append(f"| {h['code']} {h['name']} | {h['amount']:,.0f} | {h['fee']:,.0f} | "
                     f"{h['ratio'] * 100:.2f}% |")
        L += ["", "> 建议：把单笔金额做到 5,000 元以上，或把账户谈成「免 5 元最低佣金」；"
              "否则小资金每次调仓都会被手续费吃掉 0.5%–1%。", ""]
    if otcp:
        _otc_fee = round(sum(float(p.get("fee_amount") or 0.0) for p in otcp), 2)
        L += [f"- 场外债券基金申购费约 **{_otc_fee:,.2f} 元**（按上表费率估算，"
              "场外无 5 元最低佣金，不计入场内佣金口径）", ""]

    L += ["## 九、组合级风控（先于选股，不可调）", "",
          "| 规则 | 阈值 | 触发后的动作 |", "|---|---|---|",
          f"| 单票上限 | {config.EXEC['max_single_position'] * 100:.0f}% | 超出即减回，不加仓 |",
          f"| 行业上限 | {config.EXEC['max_industry'] * 100:.0f}% | 同行业合计超限即停止买入该行业 |",
          f"| 总敞口 | {config.EXEC['min_gross'] * 100:.0f}%–{config.EXEC['max_gross'] * 100:.0f}% | "
          f"低于下限说明机会不足，允许长期空仓 |",
          f"| 组合回撤 | {config.EXEC['portfolio_dd_halt'] * 100:.0f}% | "
          f"全部卖出（清仓），当周不新建仓 |",
          f"| 单票止损 | 实际成交成本 ×{config.EXEC['stop_loss_pct'] * 100:.0f}% | "
          f"成交时按成本写入，买入后无条件挂在止损价上；计划里的买入价×0.85 只是占位 |",
          f"| 移动止损 | 浮盈 >{config.PAPER['trailing_start'] * 100:.0f}% 后启用 | "
          f"止损上移到「现价 − {config.EXEC['atr_mult']}×ATR14」，只上移不下移 |",
          f"| 时间止损 | {config.EXEC['time_stop_months']} 个月 | "
          f"巡检只告警，论点未兑现由决策日判断是否退出 |", "",
          "**止损后处置**：卖出所得回到现金，**不许立刻换一只买回**。"
          "否则等于用更差的信息维持同样的风险敞口。", "",
          "## 十、风险等级评审（R2/R3 闸门）", "",
          "- 风险等级按**产品类型/板块**判定（金融产品 R1–R5 通用口径），不按自定义"
          "波动率阈值：R1 国债/存款/货基/逆回购（现金层）、R2 固定收益类理财/地方债/"
          "政金债/高等级信用债、R3 A 股主板股票、R4 创业板/科创板/北交所股票/权益类"
          "基金/跨境、R5 期货期权/分级/场外衍生品",
          f"- 风险承受能力：**{rv.get('investor_line') or risk.investor_line()}**"
          f"（投资者等级与产品等级必须匹配）", "",
          f"- 组合加权等级 **{rv.get('level_label', '—')}（R{float(rv.get('level') or 0):.2f}）**，"
          f"允许区间 R{(rv.get('range') or [2.2, 3.0])[0]:.1f}–R{(rv.get('range') or [2.2, 3.0])[1]:.1f}；"
          f"R1 低风险由现金层承担，R2 中低 / R3 中为可投范围，R4/R5 不做",
          "- 等级分布：" + ("／".join(f"{lv} {w * 100:.0f}%"
                                     for lv, w in (rv.get("weights") or {}).items()) or "—")
          + f"；R2 占比 {float(rv.get('r2_share') or 0) * 100:.1f}%"
            f"（目标 {float(rv.get('r2_target') or 0) * 100:.0f}%）",
          f"- 按风险等级筛掉的候选 **{len(plan.get('risk_filter') or [])} 只**（明细见下）", ""]
    if rv.get("violations"):
        L += ["**违规（必须处理；本期订单已清空）**："] + [f"- {x}" for x in rv["violations"]] + [""]
    if rv.get("warnings"):
        L += ["**提示**："] + [f"- {x}" for x in rv["warnings"]] + [""]
    rf = plan.get("risk_filter") or []
    if rf:
        L += ["| 被筛掉的标的 | 代码 | 等级 | 原因 |", "|---|---|---|---|"]
        for x in rf[:12]:
            L.append(f"| {x.get('name')} | {x.get('code') or x.get('sym')} | {x.get('level')} | "
                     f"{x.get('reason')} |")
        if len(rf) > 12:
            L.append(f"| …还有 {len(rf) - 12} 只 | | | |")
        L.append("")

    L += ["## 十一、复核节奏", "",
          "- 每季度财报后：核对论点支柱是否仍然成立，重跑阶段二",
          "- 每日：只看止损价与熔断线，不看盈亏百分比",
          "- 每半年：重跑阶段一，确认标的池是否已经变化", "",
          "## 十二、执行边界（必须知道）", "",
          "- 本计划只产出**订单清单**，不下单。实际下单需人手动执行，或由你自行接入券商 QMT/miniQMT/Ptrade。",
          "- 价格是「触发价」不是「保证成交价」，跳空低开时以实际成交为准。",
          "- 所有判断基于公开历史数据，不构成投资建议；亏损由你自己承担。", ""]
    if otcp:
        L += ["- **场外基金申购需人工在基金 APP 操作**：账本按影子账记账（T+1 确认净值），"
              "真实成交以 APP 确认为准，两者会差 1–2 个交易日；"
              "别把「计划里写了」当成「已经买了」——下单前确认申购状态与起投金额。", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 小工具
def _etf_val(p: dict, key: str):
    return (p.get("etf_metrics") or {}).get(key)


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


if __name__ == "__main__":
    main()
