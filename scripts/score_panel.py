# -*- coding: utf-8 -*-
"""选股评分面板：归档每周候选的四维评分，年度体检看「样本外排序能力」。

只做记录与统计，**不参与选股、不改任何分值/阈值**。年报用它回答：
「这套评分还把好票排在前面吗？」样本不足时只能写「维持现状」。

数据：
  out/archive/scoring/<数据日>.csv   每周决策后归档（judge 的 stage2_detail.json 为源）
  out/score_panel.md / .json         体检结果（脚本运行时生成）

用法：
  python scripts/score_panel.py --archive --date 2026-11-20
  python scripts/score_panel.py --health --year 2026
"""
import argparse
import datetime as dt
import json
import math
import os

import numpy as np
import pandas as pd

import config
import datasource as ds

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
DIMS = ("total", "quality", "growth", "valuation", "risk")
MIN_WEEKS = 26          # 半年周度面板才够看一眼方向
MIN_N = 30              # 单一体检窗口最少候选观测
BENCH = "000300"


def archive_dir():
    return os.path.join(config.OUT_DIR, "archive", "scoring")


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _row_from_result(r, date, basket_codes):
    sc = r.get("scores") or {}
    m = r.get("metrics") or {}
    pr = r.get("pricing") or {}
    row = {
        "date": date,
        "code": str(r.get("code") or "").zfill(6),
        "name": r.get("name") or "",
        "industry": r.get("industry") or "",
        "verdict": r.get("verdict") or "",
        "total": _f(sc.get("total")),
        "quality": _f(sc.get("quality")),
        "growth": _f(sc.get("growth")),
        "valuation": _f(sc.get("valuation")),
        "risk": _f(sc.get("risk")),
        "completeness": _f(r.get("completeness")),
        "veto": ";".join(v.get("rule", "") for v in (r.get("veto") or [])),
        "price": _f(m.get("price")) or _f(pr.get("buy_price")),
        "buy_price": _f(pr.get("buy_price")),
        "target_price": _f(pr.get("target_price")),
        "in_basket": 1 if str(r.get("code")).zfill(6) in basket_codes else 0,
    }
    for k in ("quality_parts", "growth_parts", "valuation_parts", "risk_parts"):
        parts = sc.get(k) or {}
        row[k] = json.dumps(parts, ensure_ascii=False) if parts else ""
    return row


def archive(as_of, basket=None, directory=None):
    """把本期候选评分归档到 out/archive/scoring/<as_of>.csv（同日期覆盖，幂等）。"""
    as_of = str(as_of)[:10]
    basket_codes = {str(c)[-6:].zfill(6) for c in (basket or [])}
    rows = []
    detail = os.path.join(config.OUT_DIR, "stage2_detail.json")
    if os.path.isfile(detail):
        try:
            with open(detail, encoding="utf-8") as f:
                doc = json.load(f)
            rows = [_row_from_result(r, as_of, basket_codes)
                    for r in (doc.get("results") or []) if r.get("code")]
        except (OSError, ValueError):
            rows = []
    if not rows:
        judged = os.path.join(config.OUT_DIR, "stage2_judged.csv")
        if os.path.isfile(judged):
            try:
                df = pd.read_csv(judged, dtype={"code": str})
            except (OSError, ValueError):
                df = pd.DataFrame()
            for _, r in df.iterrows():
                code = str(r.get("code") or "").zfill(6)
                if not code.strip("0"):
                    continue
                rows.append({
                    "date": as_of, "code": code, "name": r.get("name") or "",
                    "industry": r.get("industry") or "", "verdict": r.get("verdict") or "",
                    "total": _f(r.get("total")), "quality": _f(r.get("quality")),
                    "growth": _f(r.get("growth")), "valuation": _f(r.get("valuation")),
                    "risk": _f(r.get("risk")), "completeness": _f(r.get("completeness")),
                    "veto": r.get("veto") or "", "price": _f(r.get("price")),
                    "buy_price": _f(r.get("buy_price")),
                    "target_price": _f(r.get("target_price")),
                    "in_basket": 1 if code in basket_codes else 0,
                })
    if not rows:
        return {"ok": False, "n": 0,
                "reason": "本期 stage2 产物为空（候选 0 只），不归档——不代表评分失效"}
    d = directory or archive_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{as_of}.csv")
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return {"ok": True, "n": len(rows), "path": path}


def load_panel(directory=None):
    d = directory or archive_dir()
    if not os.path.isdir(d):
        return pd.DataFrame()
    frames = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".csv"):
            try:
                frames.append(pd.read_csv(os.path.join(d, fn), dtype={"code": str}))
            except (OSError, ValueError):
                continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)


def _series(df, code_col="date", val_col="close"):
    m = {}
    for _, r in df.iterrows():
        m[str(r[code_col])[:10]] = _f(r[val_col])
    return m


def _price_map(code, years=6):
    try:
        df = ds.fetch_daily(code, years=years)
    except Exception:
        return {}
    if df is None or getattr(df, "empty", True):
        return {}
    return _series(df)


def _bench_map():
    try:
        df = ds.fetch_index_daily(BENCH)
    except Exception:
        return {}
    if df is None or getattr(df, "empty", True):
        return {}
    return _series(df)


def add_forward(panel, horizons=None, price_lookup=None, bench_lookup=None):
    """给面板补前向超额收益列 ex_1m/ex_3m/ex_6m/ex_12m（相对沪深300，小数）。"""
    if panel is None or panel.empty:
        return panel
    hs = horizons or HORIZONS
    bench = bench_lookup() if bench_lookup else _bench_map()
    bdates = sorted(bench)
    if not bdates:
        return panel
    pos = {d: i for i, d in enumerate(bdates)}
    cache = {}
    get_px = price_lookup or _price_map
    out = panel.copy()
    for name, h in hs.items():
        vals = []
        for _, r in out.iterrows():
            d = str(r["date"])[:10]
            p = pos.get(d)
            if p is None or p + h >= len(bdates):
                vals.append(np.nan)
                continue
            code = str(r["code"]).zfill(6)
            if code not in cache:
                cache[code] = get_px(code)
            pm = cache[code]
            p0, p1 = pm.get(d), pm.get(bdates[p + h])
            b0, b1 = bench.get(d), bench.get(bdates[p + h])
            if not all(v and v > 0 for v in (p0, p1, b0, b1)):
                vals.append(np.nan)
            else:
                vals.append(round(p1 / p0 - b1 / b0, 6))
        out[f"ex_{name}"] = vals
    return out


def _spearman(x, y, min_n=MIN_N):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < min_n:
        return None
    xr = pd.Series(x[m]).rank(method="average").to_numpy()
    yr = pd.Series(y[m]).rank(method="average").to_numpy()
    if xr.std() == 0 or yr.std() == 0:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])


def metrics(panel, horizons=("3m", "6m")):
    """IC（分维度）、五分组差、命中率、覆盖率。"""
    res = {"n": int(len(panel)), "weeks": int(panel["date"].nunique()) if len(panel) else 0,
           "horizons": {}}
    if panel is None or panel.empty:
        return res
    res["coverage"] = {c: round(float(panel[c].notna().mean()), 3)
                       for c in ("total",) + DIMS[1:] if c in panel.columns}
    for h in horizons:
        col = f"ex_{h}"
        if col not in panel.columns:
            continue
        sub = panel[np.isfinite(pd.to_numeric(panel[col], errors="coerce"))]
        item = {"n": int(len(sub)), "ic": {}, "quintile_spread": None}
        if len(sub) < MIN_N:
            res["horizons"][h] = item
            continue
        y = sub[col].astype(float)
        for dim in DIMS:
            if dim in sub.columns:
                item["ic"][dim] = (round(_spearman(sub[dim], y), 3)
                                   if _spearman(sub[dim], y) is not None else None)
        try:
            q = pd.qcut(sub["total"].astype(float), 5, labels=False, duplicates="drop")
            if q.notna().sum() >= MIN_N and q.max() >= 1:
                high = y[q == q.max()].mean()
                low = y[q == q.min()].mean()
                item["quintile_spread"] = round(float(high - low), 4)
        except (ValueError, TypeError):
            pass
        hit = {}
        for v, label in (("买入区", "buy"), ("观察", "watch"), ("回避", "avoid")):
            m = sub["verdict"] == v
            if m.sum():
                hit[label] = {"n": int(m.sum()),
                              "mean_excess": round(float(y[m].mean()), 4),
                              "hit_rate": round(float((y[m] > 0).mean()), 3)}
        item["hit"] = hit
        res["horizons"][h] = item
    return res


def health(year=None, panel=None, directory=None):
    """年度体检：返回 {weeks, candidates, ok, conclusion, metrics, md}。"""
    panel = load_panel(directory) if panel is None else panel
    if panel is None or panel.empty:
        return {"weeks": 0, "candidates": 0, "ok": False,
                "conclusion": "样本不足：评分面板不存在（先让 weekly 归档），维持现状、不调评分。",
                "md": "## 选股评分体检\n\n- 面板为空：先跑 weekly 决策归档（`out/archive/scoring/`），暂无评估。\n"}
    if year is not None:
        panel = panel[panel["date"].astype(str).str.startswith(str(year))].copy()
    m = metrics(panel)
    weeks, n = m["weeks"], m["n"]
    if weeks < MIN_WEEKS:
        conclusion = (f"样本不足：已归档 {weeks} 周 / 需 {MIN_WEEKS} 周（{n} 条候选观测），"
                      "维持现状，不调分值、不加评分项。")
        ok = False
    elif n < MIN_N:
        conclusion = f"样本量不足：{n} 条候选观测 < {MIN_N}，维持现状。"
        ok = False
    else:
        conclusion = ("面板达到最低样本：可参考下方 IC / 分组差；任何分值或子项改动仍须影子 12 个月、"
                      "且只能在下一年 1 月 1 日生效。")
        ok = True
    return {"weeks": weeks, "candidates": n, "ok": ok, "conclusion": conclusion, "metrics": m,
            "md": health_md(year, m, conclusion)}


def health_md(year, m, conclusion, title="选股评分体检"):
    if m.get("n", 0) == 0:
        return f"## {title}\n\n- 面板为空：暂无评估。\n"
    L = [f"## {title}", "",
         f"- 面板：{m['weeks']} 周 / {m['n']} 条候选观测" + (f"（{year} 年）" if year else ""),
         f"- 结论：**{conclusion}**", "",
         "| 维度 | 覆盖率 |", "|---|---|"]
    for k, v in (m.get("coverage") or {}).items():
        L.append(f"| {k} | {v:.0%} |")
    L.append("")
    for h in ("3m", "6m"):
        it = (m.get("horizons") or {}).get(h)
        if not it:
            continue
        L += [f"### 前向 {h}（相对沪深300，样本 {it['n']}）", "",
              "| 项目 | 值 |", "|---|---|"]
        for dim, v in (it.get("ic") or {}).items():
            L.append(f"| IC（{dim}） | {v if v is not None else '—'} |")
        if it.get("quintile_spread") is not None:
            L.append(f"| 五分组差（最高−最低） | {it['quintile_spread']:.2%} |")
        for k, v in (it.get("hit") or {}).items():
            L.append(f"| {k}：均值超额 / 跑赢比例 | {v['mean_excess']:.2%} / {v['hit_rate']:.0%}（n={v['n']}） |")
        L.append("")
    L += ["口径：IC 为分维度分数与前向超额的秩相关；分组差为按总分五等分后最高组减最低组；"
          "hit 为买入区/观察/回避各自的前向超额均值与跑赢比例。只记录，不参与选股。"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="选股评分面板：归档与年度体检（只读，不改评分）")
    ap.add_argument("--archive", action="store_true", help="归档本期评分面板")
    ap.add_argument("--date", default="", help="归档数据日（默认最近交易日）")
    ap.add_argument("--health", action="store_true", help="输出体检结果")
    ap.add_argument("--year", type=int, default=None, help="体检年份（默认全部）")
    ap.add_argument("--dir", default="", help="面板目录（默认 out/archive/scoring）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    a = ap.parse_args(argv)
    d = a.dir or None
    if a.archive:
        date = a.date or (ds.last_trading_day(dt.date.today().isoformat()) or "")
        r = archive(date, directory=d)
        print(f"[score_panel] 归档：{r}")
        if not r["ok"]:
            return 1
    if a.health or not a.archive:
        h = health(a.year, directory=d)
        if a.json:
            print(json.dumps(h, ensure_ascii=False, indent=1))
        else:
            print(h["md"])
        out = os.path.join(config.OUT_DIR, "score_panel.md")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(h["md"])
            with open(out.replace(".md", ".json"), "w", encoding="utf-8") as f:
                json.dump(h, f, ensure_ascii=False, indent=1)
            print(f"\n[score_panel] 输出：{out}")
        except OSError as exc:
            print(f"[score_panel] 写输出失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
