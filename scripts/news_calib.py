#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新闻/公告评分的 12 期校准（只记录、不干预）。

为什么要它
----------
新闻员的评分权重不能拍脑袋。这个脚本把「历史公告」和「公告后 20 个交易日的
超额收益」对上，用实际伤害/实际兑现来排序，回答三个问题：

1. 哪类事件真的会跌？（命中率）→ 决定风险分 R 的维度权重
2. 哪类事件被误杀？（误杀率）→ 决定哪些标签降级为「仅记录」
3. 哪类正面事件真的兑现？→ 决定催化分 C 能不能给、给多少

口径
----
- 事件：巨潮公告标题，走 `cninfo.classify()` 规则打标（纯代码、可复现）
- 收益：事件日 → 之后第 20 个交易日，个股前复权涨幅 − 沪深300ETF(510300) 同期涨幅
- 期：12 个不重叠窗口，每个 10 个交易日；窗口末留 21 个交易日做前视，防未来函数
- 对照：同一只股票、同一窗口内**没有任何风险/预警标签**，记为对照组（基准超额）

用法
----
    python scripts/news_calib.py                 # 默认 20 只 × 12 期
    python scripts/news_calib.py --n 10          # 只取 10 只（快）
    python scripts/news_calib.py --no-cache      # 强制重抓
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import cninfo
import fuyao

OUT = config.OUT_DIR
FWD = 20            # 前视交易日
PERIOD = 10         # 每期交易日数
NPERIOD = 12        # 期数
BENCH = "sh510300"  # 沪深300ETF，作为基准代理


# ------------------------------------------------------------------ 数据准备
def _read_codes(n: int) -> list[str]:
    """候选池优先，其次阶段一全池；只取个股（6 位数字）。"""
    out: list[str] = []
    for fn, col in (("stage2_judged.csv", "score"), ("stage1_all.csv", "score")):
        p = os.path.join(OUT, fn)
        if not os.path.exists(p):
            continue
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        rows = [r for r in rows if (r.get("code") or "").strip().isdigit()]

        def key(r):
            try:
                return -float(r.get(col) or 0)
            except Exception:
                return 0.0

        for r in sorted(rows, key=key):
            c = r["code"].strip()
            if c not in out:
                out.append(c)
    # 持仓一定要在里面
    plan_p = os.path.join(OUT, "stage3_plan.json")
    if os.path.exists(plan_p):
        import json
        plan = json.load(open(plan_p, encoding="utf-8"))
        for p in plan.get("positions") or []:
            if p.get("layer") == "stock":
                c = str(p.get("code") or "").strip()
                if c.isdigit() and c not in out:
                    out.insert(0, c)
    return out[:n]


def _norm_day(v) -> str:
    """统一成 YYYY-MM-DD：兼容 '20260401' / 20260401 / 毫秒时间戳 / '2026-04-01'。"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit():
        n = len(s)
        if n == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if n >= 12:                      # 毫秒时间戳
            return dt.datetime.fromtimestamp(int(s) / 1000.0).strftime("%Y-%m-%d")
    return s[:10]


def _series(sym: str, start: str, end: str, etf: bool) -> dict[str, float]:
    """日线收盘序列 {YYYY-MM-DD: close}。ETF 走 DataFrame，个股走 item[].close_price。"""
    try:
        out: dict[str, float] = {}
        if etf:
            df = fuyao.etf_hist(sym, start, end)
            for r in (df.to_dict("records") if hasattr(df, "to_dict") else (df or [])):
                d = _norm_day(r.get("date"))
                c = r.get("close")
                if d and c is not None:
                    out[d] = float(c)
            return out
        raw = fuyao._data("historical", thscode=fuyao.thscode(sym), interval="1d",
                          start=fuyao._ms(start), end=fuyao._ms(end), adjust="forward")
        items = raw.get("item") if isinstance(raw, dict) else raw
        for r in (items or []):
            if not isinstance(r, dict):
                continue
            d = _norm_day(r.get("date_ms") or r.get("date"))
            c = r.get("close_price") if "close_price" in r else r.get("close")
            if d and c is not None:
                out[d] = float(c)
        return out
    except Exception as e:
        print(f"  ! {sym} 行情失败：{e}")
        return {}


def _periods(days: list[str]) -> list[tuple[str, str]]:
    """切成 12 个不重叠窗口，末尾留 FWD+1 个交易日做前视。"""
    usable = days[: len(days) - (FWD + 1)]
    need = NPERIOD * PERIOD
    if len(usable) < need:
        return []
    span = usable[-need:]
    return [(span[i * PERIOD], span[(i + 1) * PERIOD - 1]) for i in range(NPERIOD)]


def _fwd_return(px: dict[str, float], days: list[str], day: str,
                bench: dict[str, float]) -> float | None:
    """事件日 → 第 20 个交易日的超额收益（个股 − 基准）。"""
    if day not in days:
        # 事件日不是交易日（公告常在周末）：取之后第一个交易日
        later = [d for d in days if d >= day]
        if not later:
            return None
        day = later[0]
    i = days.index(day)
    j = i + FWD
    if j >= len(days):
        return None
    d0, d1 = day, days[j]
    if d0 not in px or d1 not in px or d0 not in bench or d1 not in bench:
        return None
    r = px[d1] / px[d0] - 1.0
    b = bench[d1] / bench[d0] - 1.0
    return r - b


# ------------------------------------------------------------------ 主流程
def run(n: int = 20, anchor: str | None = None, use_cache: bool = True,
        codes_arg: str | None = None) -> int:
    today = anchor or dt.date.today().strftime("%Y-%m-%d")
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=420)).strftime("%Y-%m-%d")
    print(f"[1/4] 取交易日历 {start} ~ {today}")
    days = [_norm_day(x) for x in (fuyao.trade_days(start, today) or [])]
    days = [d for d in days if len(d) == 10 and d[4] == "-"]
    per = _periods(days)
    if not per:
        print("  ! 交易日不足，无法切出 12 期")
        return 1
    print(f"  交易日 {len(days)} 个，窗口：{per[0][0]} ~ {per[-1][1]}（12 期 × 10 日，前视 {FWD} 日）")

    print(f"[2/4] 取基准 {BENCH}")
    bench = _series(BENCH, start, today, etf=True)
    if not bench:
        print("  ! 基准取不到，退出")
        return 1

    codes = ([c.strip() for c in codes_arg.split(",") if c.strip()]
             if codes_arg else _read_codes(n))
    print(f"[3/4] 标的 {len(codes)} 只：{','.join(codes[:6])}{' …' if len(codes) > 6 else ''}")

    rows: list[dict] = []
    episode: set[tuple[str, str]] = set()      # 风险类按「一只股票一个事件一次」计，防进度公告刷次数
    for k, code in enumerate(codes, 1):
        px = _series(code, start, today, etf=False)
        if not px:
            continue
        for (s, e) in per:
            try:
                anns = cninfo.announcements(code, s, e, use_cache=use_cache)
            except Exception as ex:
                print(f"  ! {code} {s}~{e} 公告失败：{ex}")
                continue
            seen: set[str] = set()
            has_risk = False
            for a in anns:
                tags = cninfo.classify(a.get("title") or "")
                for tag in tags:
                    kind = cninfo.tag_kind(tag)
                    if kind in ("risk", "warn"):
                        has_risk = True
                    if tag in seen:
                        continue          # 同窗口同类只记第一次
                    seen.add(tag)
                    if kind in ("risk", "warn"):
                        if (code, tag) in episode:
                            continue      # 风险类：一只股票一个事件只计一次
                        episode.add((code, tag))
                    ex = _fwd_return(px, days, a.get("date") or "", bench)
                    if ex is None:
                        continue
                    rows.append({
                        "code": code, "period_start": s, "period_end": e,
                        "tag": tag, "kind": kind, "date": a.get("date"),
                        "excess_20d": round(ex, 6),
                        "title": (a.get("title") or "")[:60],
                    })
            if not has_risk:
                ex = _fwd_return(px, days, s, bench)
                if ex is not None:
                    rows.append({"code": code, "period_start": s, "period_end": e,
                                 "tag": "__control__", "kind": "control", "date": s,
                                 "excess_20d": round(ex, 6), "title": ""})
        print(f"  [{k}/{len(codes)}] {code} 累计事件 {len(rows)}")

    print(f"[4/4] 汇总（{len(rows)} 条）")
    _write(rows)
    _report(rows)
    return 0


def _write(rows: list[dict]) -> None:
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "news_calib.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "period_start", "period_end",
                                          "tag", "kind", "date", "excess_20d", "title"])
        w.writeheader()
        w.writerows(rows)
    print(f"  明细 → {p}")


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": st.mean(vals),
        "median": st.median(vals),
        "hit": sum(1 for v in vals if v <= -0.05) / len(vals),   # 跌超 5%
        "kill": sum(1 for v in vals if v >= 0.05) / len(vals),   # 反而涨超 5%
        "pos": sum(1 for v in vals if v >= 0.05) / len(vals),
    }


def _report(rows: list[dict]) -> None:
    by: dict[str, list[float]] = {}
    kind_of: dict[str, str] = {}
    # 每只股票自己的「无事件窗口」均值，用来做差中差：事件窗口超额 − 该股平时超额
    ctrl_by_code: dict[str, list[float]] = {}
    for r in rows:
        if r["tag"] == "__control__":
            ctrl_by_code.setdefault(r["code"], []).append(float(r["excess_20d"]))
    code_base = {c: st.mean(v) for c, v in ctrl_by_code.items() if v}
    adj: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["tag"], []).append(float(r["excess_20d"]))
        kind_of[r["tag"]] = r["kind"]
        if r["tag"] != "__control__":
            b = code_base.get(r["code"])
            if b is not None:
                adj.setdefault(r["tag"], []).append(float(r["excess_20d"]) - b)

    ctrl = _stats(by.get("__control__", []))
    print("\n## 对照组（同窗口无任何风险/预警标签）")
    if ctrl.get("n"):
        print(f"n={ctrl['n']}  均值超额 {ctrl['mean']:+.2%}  中位 {ctrl['median']:+.2%}"
              f"  跌超5% {ctrl['hit']:.0%}  涨超5% {ctrl['pos']:.0%}")

    print("\n## 按事件标签（按差中差均值排序，越负越该扣分）")
    print("| 标签 | 性质 | n | 差中差 | 原始超额 | 跌超5% | 误杀(涨超5%) |")
    print("|---|---|---|---|---|---|---|")
    items = [(t, v) for t, v in by.items() if t != "__control__" and v]
    rows_out = []
    for tag, vals in items:
        s = _stats(vals)
        a = _stats(adj.get(tag, []))
        rows_out.append((tag, s, a))
    rows_out.sort(key=lambda x: (x[2]["mean"] if x[2].get("n") else x[1]["mean"]))
    for tag, s, a in rows_out:
        did = f"{a['mean']:+.2%}" if a.get("n") else "—"
        print(f"| {tag} | {kind_of.get(tag,'')} | {s['n']} | {did} | "
              f"{s['mean']:+.2%} | {s['hit']:.0%} | {s['kill']:.0%} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="新闻/公告评分 12 期校准")
    ap.add_argument("--n", type=int, default=20, help="标的数（默认 20）")
    ap.add_argument("--anchor", help="锚定日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--no-cache", action="store_true", help="强制重抓公告")
    ap.add_argument("--codes", help="指定标的池，逗号分隔（默认取候选池）")
    a = ap.parse_args(argv)
    return run(a.n, a.anchor, not a.no_cache, a.codes)


if __name__ == "__main__":
    sys.exit(main())
