# -*- coding: utf-8 -*-
"""财报细项 / 业绩预告查询工具（决策日研究用，日常不跑）。

用法：
    python fin_extra.py --code 000999
    python fin_extra.py --codes out/stage1_candidates.csv --limit 20
    python fin_extra.py --code 000999 --json

打印内容：最新报告期的应收/存货/有息负债同比、经营现金流/净利润、最近三期利润表
关键行，以及最近几条业绩预告（含预告类型与同比区间）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402  （触发缓存目录注入）
import datasource as ds  # noqa: E402


def _num(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def one(code: str) -> dict:
    q = ds.fin_quality(code)
    out = {"code": code, "quality": q}
    try:
        inc = ds.fetch_fin_income(code)
        rows = []
        for _, r in inc.head(3).iterrows():
            rows.append({
                "report": str(r.get("REPORT_DATE") or ""),
                "notice": str(r.get("NOTICE_DATE") or ""),
                "营收": _num(r.get("TOTAL_OPERATE_INCOME")),
                "归母净利": _num(r.get("PARENT_NETPROFIT")),
            })
        out["income"] = rows
    except Exception as exc:  # noqa: BLE001
        out["income_error"] = f"{type(exc).__name__}: {exc}"
    try:
        fc = ds.fetch_forecast(code)
        out["forecast"] = fc.head(3).to_dict("records") if fc is not None and not fc.empty else []
    except Exception as exc:  # noqa: BLE001
        out["forecast_error"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="财报细项 + 业绩预告（决策日研究用）")
    ap.add_argument("--code", default="", help="6 位代码")
    ap.add_argument("--codes", default="", help="CSV/JSON 文件，取其「代码」列或 code 字段")
    ap.add_argument("--limit", type=int, default=10, help="--codes 时最多看几只")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    codes = []
    if a.code:
        codes = [a.code.strip()]
    elif a.codes:
        p = a.codes
        if p.lower().endswith(".json"):
            data = json.load(open(p, encoding="utf-8"))
            for item in data if isinstance(data, list) else [data]:
                c = item.get("code") or item.get("代码") if isinstance(item, dict) else None
                if c:
                    codes.append(str(c).strip()[-6:])
        else:
            import pandas as pd
            df = pd.read_csv(p, dtype=str)
            col = "代码" if "代码" in df.columns else ("code" if "code" in df.columns else df.columns[0])
            codes = [str(v).strip()[-6:] for v in df[col].dropna().tolist()]
        codes = codes[: a.limit]
    else:
        ap.print_help()
        return 2

    results = [one(c) for c in codes]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0

    for r in results:
        q = r["quality"]
        print(f"\n=== {r['code']}  最新报告 {q.get('report')}（披露 {q.get('notice')}）")
        print(f"  应收同比 {q.get('receivable_yoy')}  存货同比 {q.get('inventory_yoy')}  "
              f"有息负债同比 {q.get('debt_yoy')}  经营现金流/净利 {q.get('cfo_to_np')}")
        for row in r.get("income") or []:
            print(f"  利润表 {row['report']}（披露 {row['notice']}）"
                  f" 营收 {row['营收']}  归母净利 {row['归母净利']}")
        for fc in r.get("forecast") or []:
            print(f"  预告 {fc.get('报告期')} {fc.get('指标')} {fc.get('预告类型')} "
                  f"净利 {fc.get('净利润下限')}~{fc.get('净利润上限')} "
                  f"同比 {fc.get('同比下限')}%~{fc.get('同比上限')}%（公告 {fc.get('公告日')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
