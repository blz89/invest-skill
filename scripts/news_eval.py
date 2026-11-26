#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件面「事后命中率」评估（只记录、不参与打分）。

为什么要有它
------------
`news.py` 给事件打 R/C 分、给降权/加分系数，这些数字至今没被收益验证过。
本脚本回答一个问题：**被降权的事件，事后真的跌了吗？被加分的事件，真的涨了吗？**
只统计，不自动改权重——样本不足时只写「维持现状」。

口径（可证伪）
-------------
- 事件来源：`out/news_events.csv`（news.py 的产出）。
- 命中定义（方向性）：
  * 降权类（tier 以「降权」开头，net_score < 0）：事件后 N 个交易日，个股相对沪深300
    超额收益 **< 0** 记为命中；
  * 加分类（tier 为「加分」，net_score > 0）：超额收益 **> 0** 记为命中；
  * 「不干预」不判命中，只统计方向（用来看有没有漏判）。
- 收益口径：事件日（非交易日顺延到之后第一个交易日）→ 之后第 N 个交易日收盘；
  个股用前复权日线，基准用沪深300指数；N ∈ {20, 60, 120}（约 1/3/6 个月）。
- 未到期：只有到最近交易日不足 N 个交易日的，标「进行中」，报当前超额，不计入命中率。
- 样本不足：某档到期样本 < 10 条 → 只报数字并标「样本不足，不用于调权重」。
  （规则：任何「用少样本改否决阈值/打分权重」的动作都不允许，见 SKILL §12。）

用法
----
    python scripts/news_eval.py                # 评估 out/news_events.csv
    python scripts/news_eval.py --json         # 机器可读
    python scripts/news_eval.py --asof 2026-11-27
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: F401  （装 datasource 的 cache hook）

OUT = config.OUT_DIR
EVENTS = os.path.join(OUT, "news_events.csv")
JSON_PATH = os.path.join(OUT, "news_eval.json")
MD_PATH = os.path.join(OUT, "news_eval.md")
BENCH_CODE = "000300"
HORIZONS = (20, 60, 120)
MIN_SAMPLE = 10


# ------------------------------------------------------------------ 纯函数
def _norm_day(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit():
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if len(s) >= 12:
            return dt.datetime.fromtimestamp(int(s) / 1000.0).strftime("%Y-%m-%d")
    return s[:10]


def direction_of(tier: str, net_score: float | int | None = None) -> int:
    """事件方向：-1 利空（该跌）/ +1 利好（该涨）/ 0 不干预或未知。"""
    t = str(tier or "")
    if t.startswith("降权"):
        return -1
    if t.startswith("加分"):
        return 1
    try:
        s = float(net_score)
    except (TypeError, ValueError):
        return 0
    if s < 0:
        return -1
    if s > 0:
        return 1
    return 0


def load_events(path: str = EVENTS) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            code = str(r.get("code") or "").strip()
            if not code.isdigit():
                continue
            day = _norm_day(r.get("date"))
            if not day:
                continue
            rows.append({
                "code": code, "name": r.get("name") or "", "date": day,
                "kind": r.get("kind") or "", "level": r.get("level") or "",
                "tag": r.get("tag") or "", "tier": r.get("tier") or "",
                "net_score": r.get("net_score") or 0,
                "r_score": r.get("r_score") or 0, "c_score": r.get("c_score") or 0,
                "title": r.get("title") or "",
                "direction": direction_of(r.get("tier"), r.get("net_score")),
            })
    return rows


def _first_td_on_or_after(days: list[str], day: str) -> str | None:
    later = [d for d in days if d >= day]
    return later[0] if later else None


def excess(px: dict[str, float], bench: dict[str, float], days: list[str],
           day: str, horizon: int) -> dict | None:
    """事件日 → 之后第 horizon 个交易日的超额收益（个股 − 基准）。

    days 是基准交易日序列（升序）。不足 horizon 时返回截至最近交易日的「进行中」超额，
    并把 matured 标成 False（调用方不计入命中率）。
    """
    d0 = _first_td_on_or_after(days, day)
    if not d0 or d0 not in px or d0 not in bench:
        return None
    i = days.index(d0)
    j = min(i + horizon, len(days) - 1)
    if j <= i:
        return None
    d1 = days[j]
    if d1 not in px or d1 not in bench:
        return None
    r = px[d1] / px[d0] - 1.0
    b = bench[d1] / bench[d0] - 1.0
    return {"excess": r - b, "start": d0, "end": d1,
            "matured": (i + horizon) < len(days), "horizon": horizon}


def evaluate(events: list[dict], prices: dict[str, dict], bench: dict[str, float],
             horizons: tuple[int, ...] = HORIZONS) -> list[dict]:
    days = sorted(bench.keys())
    out = []
    for e in events:
        px = prices.get(e["code"]) or {}
        if not px:
            continue
        for h in horizons:
            r = excess(px, bench, days, e["date"], int(h))
            if not r:
                continue
            out.append({**{k: e[k] for k in ("code", "name", "date", "tier", "tag",
                                            "level", "net_score", "direction")},
                        **r})
    return out


def _stats(vals: list[float], direction: int) -> dict:
    n = len(vals)
    if not n:
        return {"n": 0}
    hits = sum(1 for v in vals if (v > 0) == (direction > 0)) if direction else 0
    return {"n": n, "mean": st.mean(vals), "median": st.median(vals),
            "hit_rate": (hits / n) if direction else None,
            "pos_rate": sum(1 for v in vals if v > 0) / n}


def aggregate(rows: list[dict]) -> dict:
    """按 (档位, 周期) 聚合；只统计已到期样本。"""
    agg: dict[str, dict] = {}
    for r in rows:
        key = f'{r["tier"]}|{r["horizon"]}'
        bucket = agg.setdefault(key, {"tier": r["tier"], "horizon": r["horizon"],
                                     "direction": r["direction"],
                                     "matured": [], "running": []})
        (bucket["matured"] if r["matured"] else bucket["running"]).append(float(r["excess"]))
    for b in agg.values():
        b["stats"] = _stats(b["matured"], b["direction"])
        b["running_stats"] = _stats(b["running"], b["direction"])
        b["insufficient"] = 0 < b["stats"].get("n", 0) < MIN_SAMPLE
    return agg


def render_md(rows: list[dict], asof: str) -> str:
    agg = aggregate(rows)
    L = [f"# 事件面事后命中率（as of {asof}）", "",
         "口径：降权类事后超额 <0 记命中，加分类 >0 记命中；基准=沪深300；"
         "N=20/60/120 个交易日。只记录，不改打分。", ""]
    if not rows:
        L += ["- 没有可评估的事件（news_events.csv 为空或事件日缺行情）。", ""]
        return "\n".join(L)
    L += ["| 档位 | 周期 | 到期 n | 命中率 | 平均超额 | 中位超额 | 进行中 n | 当前平均超额 | 备注 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(agg, key=lambda k: (agg[k]["tier"], agg[k]["horizon"])):
        b = agg[key]
        s, rs = b["stats"], b["running_stats"]
        if not s.get("n"):
            L.append(f'| {b["tier"]} | {b["horizon"]}日 | 0 | — | — | — | '
                     f'{rs.get("n", 0)} | '
                     f'{rs.get("mean", 0):+.2%} | 未到期 |')
            continue
        note = "样本不足（不用于调权重）" if b["insufficient"] else "可用于观察"
        L.append(f'| {b["tier"]} | {b["horizon"]}日 | {s["n"]} | '
                 f'{(s["hit_rate"] or 0):.0%} | {s["mean"]:+.2%} | {s["median"]:+.2%} | '
                 f'{rs.get("n", 0)} | {rs.get("mean", 0):+.2%} | {note} |')
    L += ["", "## 进行中的事件（未到期，只报当前超额）", ""]
    running = [r for r in rows if not r["matured"]]
    if not running:
        L.append("- 无。")
    else:
        for r in sorted(running, key=lambda x: (x["date"], x["code"]))[:20]:
            L.append(f'- {r["date"]} {r["code"]} {r["name"]}｜{r["tier"]}｜'
                     f'{r["horizon"]}日窗口 {r["start"]}→{r["end"]}｜当前超额 '
                     f'{r["excess"]:+.2%}')
    L += ["", "边界：模拟盘记录，不构成投资建议，不承诺收益。"]
    return "\n".join(L)


def section_md(path: str | None = None) -> list[str]:
    """月报用：压缩成几行。文件不存在时给一句提示（不抛异常）。"""
    p = path or JSON_PATH
    if not os.path.exists(p):
        return ["- 事件面命中率：尚无评估结果（先跑 `python scripts/news_eval.py`）。"]
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return ["- 事件面命中率：评估文件读取失败。"]
    lines = [f'- 事件面命中率（as of {data.get("asof") or "-"}）：']
    any_row = False
    for item in data.get("summary") or []:
        s = item.get("stats") or {}
        if not s.get("n"):
            continue
        any_row = True
        lines.append(f'  - {item["tier"]}（{item["horizon"]}日）：n={s["n"]}'
                     f'｜命中率 {(s.get("hit_rate") or 0):.0%}'
                     f'｜平均超额 {s["mean"]:+.2%}'
                     f'{"｜样本不足" if item.get("insufficient") else ""}')
    if not any_row:
        lines.append("  - 无到期样本（事件窗口未成熟），本月只记录进行中。")
    return lines


# ------------------------------------------------------------------ 取数
def _series(code: str) -> dict[str, float]:
    try:
        import datasource as ds
        df = ds.fetch_daily(code)
        if df is None or not hasattr(df, "to_dict"):
            return {}
        out = {}
        for r in df.to_dict("records"):
            d = _norm_day(r.get("date"))
            c = r.get("close")
            if d and c is not None:
                out[d] = float(c)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ! {code} 行情失败：{e}")
        return {}


def _bench() -> dict[str, float]:
    try:
        import datasource as ds
        df = ds.fetch_index_daily(BENCH_CODE)
        out = {}
        for r in df.to_dict("records"):
            d = _norm_day(r.get("date"))
            c = r.get("close")
            if d and c is not None:
                out[d] = float(c)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ! 基准取不到：{e}")
        return {}


def run(events_path: str = EVENTS, asof: str | None = None, write: bool = True) -> dict:
    events = load_events(events_path)
    asof = asof or dt.date.today().isoformat()
    bench = _bench()
    if not bench:
        print("[error] 基准行情取不到，无法评估")
        return {"error": "no_bench"}
    codes = sorted({e["code"] for e in events})
    prices = {c: _series(c) for c in codes}
    rows = evaluate(events, prices, bench)
    agg = aggregate(rows)
    summary = [{"tier": b["tier"], "horizon": b["horizon"],
                "direction": b["direction"], "stats": b["stats"],
                "running_stats": b["running_stats"],
                "insufficient": b["insufficient"]}
               for b in agg.values()]
    doc = {"asof": asof, "events": len(events), "rows": rows,
           "summary": summary, "min_sample": MIN_SAMPLE}
    if write:
        os.makedirs(OUT, exist_ok=True)
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        with open(MD_PATH, "w", encoding="utf-8") as f:
            f.write(render_md(rows, asof))
        doc["paths"] = {"json": JSON_PATH, "md": MD_PATH}
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="事件面事后命中率")
    ap.add_argument("--events", default=EVENTS)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    doc = run(a.events, a.asof)
    if doc.get("error"):
        return 1
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    print(f'[news_eval] 事件 {doc["events"]} 条｜分档统计 {len(doc["summary"])} 行')
    for it in doc["summary"]:
        s = it.get("stats") or {}
        print(f'  {it["tier"]} {it["horizon"]}日：到期 {s.get("n", 0)}'
              f'{"" if not s.get("n") else f"，命中率 {(s.get("hit_rate") or 0):.0%}，平均超额 {s["mean"]:+.2%}"}'
              f'{"" if not it.get("insufficient") else "（样本不足）"}')
    print(f'[news_eval] 明细 {doc.get("paths", {}).get("md", MD_PATH)}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
