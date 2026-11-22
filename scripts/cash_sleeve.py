# -*- coding: utf-8 -*-
"""cash_sleeve.py —— 半流动性货币增值：按 **90 天实得年化** 从全市场货基取前 N 支。

选择 90 日年化最高的几个作为半流动性货币增值策略；朝朝盈2号不再使用，
直接对接 90 天利润最高的货基——
排序宇宙由 cash_pool.py 决定（默认全市场货基 500–600 只粗排后精算），本模块只认
排行文件里的 90 天口径，渠道无关（同一支基金代码在券商/银行/直销都能买）。
本模块只做**纯计算**：把一笔钱按排名切成「每支 ≤1 万」的持仓。取数由 cash_pool.py
（排行落盘 out/cash_pool_rank.json），落账由 paper.accrue_cash_lots 负责——三层分开，
是为了让账本能离线重算，不因为某天接口挂了就记不出利息。

为什么是 90 天而不是 7 日年化：
  App 的「产品明细」按 7 日年化排，而 7 日年化是**滞后滚动均值**——单日万份收益
  爆一下（国投瑞银增利宝 2026-11-12 的 0.998 元/万份 ≈ 单日年化 3.6%），7 日年化立刻
  从 0.95% 跳到 1.34%，7 天后又掉回 1.0%。实测 2026-11-13：招行 App 前三（1.40/1.36/1.34%）
  的 90 天实得年化只有 1.27/1.12/0.99%，92 只里排 #25/#66/#84——照 App 排名买，
  等于专挑「刚爆完单日收益、即将回落」的那只。

约束（来自招行 App 规则，是硬约束不是偏好）：
  * 每支货基**每日快赎 1 万元** → 单支上限 = per_fund_cap(10000)，多的摊下一支；
  * 池内多数「限制大额申购」，单笔别超 1 万；
  * 钱再多也有上限：top_n × cap（默认 3 万），超出部分留朝朝宝（T+0 随时可用）。

fail-closed：排行文件缺失 / 数据日超过 max_stale_days → `plan()` 默认返回空持仓
（`fallback=True`），调用方把整笔钱退回朝朝宝口径并记一条 warn——绝不拿过期排名做新配置。
例外：`allow_stale=True` 只用于**给已有持仓计息**（沿用最后一份排行的持仓，不新增配置），
否则已有货基会被退回朝朝宝年化，逐日万份收益直接失效、利息少算。
"""
from __future__ import annotations

import datetime as dt
import json
import os

import config  # noqa: E402

OUT_DIR = getattr(config, "OUT_DIR", None) or os.path.join(config.WORK_DIR, "out")
RANK_FILE = os.path.join(OUT_DIR, "cash_pool_rank.json")


def _cfg() -> dict:
    """半流动性策略参数（config.CAPITAL.cash_layer.semi_liquid），缺省即默认值。"""
    c = (getattr(config, "CAPITAL", {}) or {}).get("cash_layer") or {}
    s = c.get("semi_liquid") or {}
    return {
        "enabled": bool(s.get("enabled", True)),
        "rank_file": str(s.get("rank_file") or RANK_FILE),
        "top_n": int(s.get("top_n") or 3),
        "per_fund_cap": float(s.get("per_fund_cap") or 10000.0),
        "max_stale_days": int(s.get("max_stale_days") or 5),
        "rerank_days": int(s.get("rerank_days") or 30),
        "min_rate": float(s.get("min_rate") or 0.0),
    }


def load_rank(path: str = None) -> dict:
    """读排行文件；返回 {"as_of","data_date","rows":[...按 y90 降序]}，坏文件抛 ValueError。"""
    p = path or _cfg()["rank_file"]
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    rows = [r for r in (raw.get("rows") or []) if r.get("y90")]
    rows.sort(key=lambda r: -float(r["y90"]))
    data_date = str(raw.get("data_date") or (rows[0].get("last_date") if rows else ""))
    return {"as_of": str(raw.get("as_of") or ""), "data_date": data_date,
            "rows": rows, "cash": float(raw.get("cash") or 0.0)}


def data_age_days(rank: dict, today: str = None) -> int | None:
    """排行数据日距今多少天（None = 没有可用数据日）。"""
    dd = str(rank.get("data_date") or "")
    if not dd:
        return None
    d0 = dt.date.fromisoformat(dd)
    d1 = dt.date.fromisoformat(str(today)) if today else dt.date.today()
    return (d1 - d0).days


def plan(amount: float, rank: dict = None, today: str = None,
         top_n: int = None, per_fund_cap: float = None,
         allow_stale: bool = False) -> dict:
    """把 amount 元按 90 天排名切成持仓（每支 ≤ per_fund_cap）。

    返回 {"amount","holdings":[{code,name,amount,rate,rank}],"weighted_rate",
          "residual","as_of","data_date","stale","fallback","degraded","note"}
    fallback=True 时 holdings 为空、residual=amount（调用方退回朝朝宝口径）。
    allow_stale=True 时即使排行过期也按最后一份排行切持仓（仅供计息沿用，不用于新配置），
    此时 stale=True、degraded=True，且不按「暂停申购/最低年化」过滤已有持仓。
    """
    c = _cfg()
    top_n = int(top_n if top_n is not None else c["top_n"])
    cap = float(per_fund_cap if per_fund_cap is not None else c["per_fund_cap"])
    amt = round(max(0.0, float(amount)), 2)
    out = {"amount": amt, "holdings": [], "weighted_rate": 0.0, "residual": amt,
           "as_of": "", "data_date": "", "stale": False, "fallback": False,
           "degraded": False, "top_n": top_n, "per_fund_cap": cap,
           "note": ""}
    if amt <= 0:
        out["note"] = "观望资金为 0，无需分配"
        return out
    if not c["enabled"]:
        out.update(fallback=True, note="半流动性策略未启用（config semi_liquid.enabled=False）")
        return out
    try:
        rk = rank if rank is not None else load_rank(c["rank_file"])
    except Exception as exc:
        out.update(fallback=True, note=f"排行文件不可用（{type(exc).__name__}），整笔退回朝朝宝：{exc}")
        return out
    rows = rk.get("rows") or []
    if not rows:
        out.update(fallback=True, note="排行文件里没有可用基金（y90 全缺），整笔退回朝朝宝")
        return out
    age = data_age_days(rk, today)
    stale = age is not None and age > c["max_stale_days"]
    out.update(as_of=rk.get("as_of", ""), data_date=rk.get("data_date", ""), stale=stale)
    if stale:
        if not allow_stale:
            out.update(fallback=True,
                       note=f"排行数据日 {rk.get('data_date')} 已超过 {c['max_stale_days']} 天，"
                            f"整笔退回朝朝宝（先跑 cash_pool.py 刷新）")
            return out
        # 计息路径：沿用最后一份排行持仓，不因排行过期把已有货基退回朝朝宝
        out.update(degraded=True)
    remain = amt
    for i, r in enumerate(rows, 1):
        if len(out["holdings"]) >= top_n or remain <= 0:
            break
        rate = float(r["y90"])
        if not allow_stale:
            if c["min_rate"] and rate < c["min_rate"]:
                continue
            if "暂停申购" in str(r.get("buy") or ""):
                continue                       # 名次再高，买不进等于没有
        take = round(min(cap, remain), 2)
        out["holdings"].append({"code": str(r.get("code") or ""), "name": str(r.get("name") or ""),
                                "amount": take, "rate": rate, "rank": i,
                                "y90": rate, "y30": r.get("y30"), "buy": r.get("buy") or ""})
        remain = round(remain - take, 2)
    out["residual"] = round(max(0.0, remain), 2)
    tot = sum(h["amount"] for h in out["holdings"])
    out["weighted_rate"] = (round(sum(h["amount"] * h["rate"] for h in out["holdings"]) / tot, 6)
                            if tot > 0 else 0.0)
    if not out["holdings"]:
        out.update(fallback=True, note="排行里没有可买的基金（全部暂停申购/低于门槛），整笔退回朝朝宝")
        return out
    out["note"] = (f"半流动性 {tot:,.0f} 元按 90 天实得年化摊 {len(out['holdings'])} 支"
                   f"（每支 ≤{cap:,.0f} 元快赎），加权 {out['weighted_rate']:.2%}；"
                   f"零头 {out['residual']:,.0f} 元留朝朝宝")
    if stale:
        out["note"] += (f"；⚠ 排行数据日 {rk.get('data_date')} 已超过 {c['max_stale_days']} 天："
                        f"按最后一份排行持仓计息（不新增配置），先跑 cash_pool.py 刷新")
    return out


def describe(plan_dict: dict) -> str:
    """一行中文说明（报表/计划里直接贴）。"""
    p = plan_dict or {}
    hs = p.get("holdings") or []
    if not hs:
        return f"半流动性暂不启用：{p.get('note') or '无持仓'}"
    body = "；".join(f"{h['name']} {h['amount']:,.0f} 元（{h['rate']:.2%}）" for h in hs)
    return (f"半流动性货币增值 {sum(h['amount'] for h in hs):,.0f} 元 → {body}；"
            f"加权 {p.get('weighted_rate', 0):.2%}（数据日 {p.get('data_date') or '-'}，"
            f"每支 ≤{p.get('per_fund_cap', 0):,.0f} 元快赎）")


def rerank_due(last_as_of: str, today: str, days: int = None) -> bool:
    """离上次重排是否已到 rerank_days（默认 30 天）——只提醒，不自动下单。"""
    c = _cfg()
    days = int(days if days is not None else c["rerank_days"])
    if not last_as_of:
        return True
    return (dt.date.fromisoformat(str(today)) - dt.date.fromisoformat(str(last_as_of))).days >= days
