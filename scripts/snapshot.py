#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 交接契约：data_snapshot.csv 的生产者。

契约（team.py handoff_contract 里的第 1 条）规定了 `out/data_snapshot.csv`
的列名，但一直没有生产者——研究员的 T2 任务里写着「读 out/data_snapshot.csv」，
实际那个文件根本不存在，只能退回去读 stage1_candidates.csv（只有候选，没有全池底座）。

这个脚本把它补上：从已有的阶段一/二产物纯拼装，**不取任何网络数据**，
所以它便宜、可复现、可以放进确定性的两次跑里比较。

数据来源（全部本地文件，谁缺就报错，不猜）：
  out/stage1_all.csv     → 个股全池（缺它直接失败，这是主表）
  out/stage2_judged.csv  → 候选的自有 PE 分位（pe_pct_own，只有 21 只候选有）
  out/stage1_etf.csv     → ETF 池（只取条数与最新的 price_date 进 meta）
  out/stage1_otc.csv     → 场外债基池（同上）

L1 的定位是「个股全池快照」，所以**只出个股行**：
  - ETF 与场外基金没有 ROE/现金流/负债率，塞进同一张表只会让下游到处写 if；
  - 它们各自已有 `stage1_etf.csv` / `stage1_otc.csv` 两个契约外的稳定产物；
  - meta 里记下两者的行数与新鲜度，研究员要交叉核对时仍然看得见。

契约里 `amt20_yi` 与实际口径不一致：引擎算的是 **60 日均额**（avg_amt60_yi），
所以这里如实输出 `amt60_yi`，同时把 team.py 契约表与 team-framework §3 一并改过来
——契约写错的成本比改契约高得多。

用法：
  python snapshot.py                 # 生成 out/data_snapshot.csv
  python snapshot.py --today 2026-10-28   # 指定「今天」（stale_days 用），便于复现
  python snapshot.py --strict        # 缺主表/关键列直接 exit 2（默认就是这个行为）

退出码：0 正常；2 输入缺失或不可用。
"""
import argparse
import csv
import io
import json
import os
import sys
from datetime import date, datetime

import config

OUT = config.OUT_DIR

# L1 契约列（与 team.py handoff_contract 第一条保持一致，改一处必须改另一处）
COLS = ["code", "name", "industry", "scale_yi", "amt60_yi", "roe5y_min",
        "ocf_ni_3y", "debt_ratio", "rev_cagr3", "pe", "pe_pct_self",
        "pb_pct_ind", "mdd3y", "nav_date", "source", "stale_days"]

# stage1_all.csv 必需列：缺任何一个都说明上游改过表，必须停下来改代码
REQUIRED = ["code", "name", "industry", "mktcap_yi", "avg_amt60_yi", "roe_list",
            "ocf_ni_3y", "debt_ratio", "rev_cagr3", "pe", "pb_pct_ind", "max_dd_3y"]


def _industry(raw, code: str) -> str:
    """行业标签归一（2026-11-07 B 类 #6）：代码级修正 + 缺失 → 「未分类」。

    快照是 screen/plan/team 的共同上游，必须与 datasource 入口同一口径；
    这里再兜一次是因为老 out/stage1_all.csv 可能是修正前生成的，直接读会带回旧标签。
    """
    import industry as _ind
    return _ind.normalize(raw, code)


def _read_csv(name: str) -> list[dict]:
    """读 out/ 下的 CSV。用 utf-8-sig：这些文件有的带 BOM（Excel 友好），
    不处理的话第一列会变成 '\ufeffcode'，按列名取会全部为空。"""
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(v):
    """空串/None/'-' → None；能转 float 就转。不许把 '' 当 0。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s in ("-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roe_min5(roe_list: str):
    """roc_list 长这样：'[None, 10.47, 9.84, 8.99, 7.73]'。
    取非空最小值；全空返回 None（不许当 0，0 会被读成「极低 ROE」）。"""
    vals = []
    for piece in str(roe_list or "").strip("[]").split(","):
        x = _num(piece)
        if x is not None:
            vals.append(x)
    return min(vals) if vals else None


def _as_of(judged: list[dict], detail_path: str) -> str:
    """数据日：优先用 stage2_detail.json 里每只票的 metrics.as_of；
    没有再退到 plan 的 data_as_of；都没有就今天（并让 stale_days=0）。"""
    try:
        with io.open(detail_path, encoding="utf-8") as f:
            j = json.load(f)
        cands = [r.get("metrics", {}).get("as_of") for r in j.get("results", [])]
        cands = [c for c in cands if c]
        if cands:
            return max(cands)
    except (OSError, ValueError, KeyError):
        pass
    for name in ("stage3_plan.json", "stage1_funnel.json"):
        p = os.path.join(OUT, name)
        if not os.path.exists(p):
            continue
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        for k in ("data_as_of", "as_of"):
            if isinstance(d.get(k), str) and d[k]:
                return d[k]
    return date.today().isoformat()


def build(today: str | None = None) -> tuple[list[dict], dict]:
    """拼装 L1 快照。返回 (rows, meta)。不做任何 IO 写入，方便自测直接调。"""
    today = today or date.today().isoformat()
    base = _read_csv("stage1_all.csv")
    if not base:
        raise FileNotFoundError("out/stage1_all.csv 缺失或为空——L1 快照的主表，必须先跑 screen.py")

    miss = [c for c in REQUIRED if c not in base[0]]
    if miss:
        raise KeyError(f"out/stage1_all.csv 缺必需列 {miss}；现有列：{list(base[0])}")

    judged = _read_csv("stage2_judged.csv")
    own_pct = {}
    for r in judged:
        v = _num(r.get("pe_pct_own"))
        if v is not None and r.get("code"):
            own_pct[str(r["code"]).zfill(6)] = v

    nav_date = _as_of(judged, os.path.join(OUT, "stage2_detail.json"))
    try:
        stale = (date.fromisoformat(today) - date.fromisoformat(nav_date)).days
        stale = max(0, int(stale))
    except ValueError:
        stale = 0

    rows, skipped = [], []
    for r in base:
        # data_ok 为假的行（取数失败）不进快照：宁可少，不可假
        ok = str(r.get("data_ok", "")).strip().lower()
        if ok not in ("true", "1", "yes"):
            skipped.append(r.get("code"))
            continue
        code = str(r.get("code") or "").strip().zfill(6)
        if not code or code == "000000":
            skipped.append(r.get("code"))
            continue
        rows.append({
            "code": code,
            "name": r.get("name") or "",
            "industry": _industry(r.get("industry"), code),
            "scale_yi": _num(r.get("mktcap_yi")),
            "amt60_yi": _num(r.get("avg_amt60_yi")),
            "roe5y_min": roe_min5(r.get("roe_list")),
            "ocf_ni_3y": _num(r.get("ocf_ni_3y")),
            "debt_ratio": _num(r.get("debt_ratio")),
            "rev_cagr3": _num(r.get("rev_cagr3")),
            "pe": _num(r.get("pe")),
            "pe_pct_self": own_pct.get(code),   # 只有进了阶段二的候选才有
            "pb_pct_ind": _num(r.get("pb_pct_ind")),
            "mdd3y": _num(r.get("max_dd_3y")),
            "nav_date": nav_date,
            "source": "stage1_all.csv+stage2_judged.csv(本地拼装,无网络)",
            "stale_days": stale,
        })

    # 覆盖率自检：关键列全空说明上游改了字段名，不能静默出一张空表
    cov = {}
    for c in ("scale_yi", "amt60_yi", "roe5y_min", "ocf_ni_3y", "debt_ratio",
              "rev_cagr3", "pe", "pb_pct_ind", "mdd3y"):
        cov[c] = round(sum(1 for x in rows if x[c] is not None) / max(1, len(rows)), 4)
    dead = [c for c, v in cov.items() if v == 0.0]
    if dead:
        raise ValueError(f"L1 快照关键列全空 {dead}——上游字段名可能改过，先核对 stage1_all.csv 表头")

    etf = _read_csv("stage1_etf.csv")
    otc = _read_csv("stage1_otc.csv")
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "nav_date": nav_date,
        "stale_days": stale,
        "rows": len(rows),
        "skipped_no_data": len(skipped),
        "skipped_sample": skipped[:5],
        "pe_pct_self_coverage": len(own_pct),
        "etf_rows": len(etf),
        "otc_rows": len(otc),
        "columns": COLS,
        "coverage": cov,
        "pools": {"stock": len(rows), "etf": len(etf), "otc": len(otc)},
        "note": "L1 只含个股全池；ETF/场外见 stage1_etf.csv / stage1_otc.csv",
    }
    return rows, meta


def write(rows: list[dict], meta: dict) -> tuple[str, str]:
    os.makedirs(OUT, exist_ok=True)
    p_csv = os.path.join(OUT, "data_snapshot.csv")
    with io.open(p_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in COLS})
    p_meta = os.path.join(OUT, "data_snapshot_meta.json")
    with io.open(p_meta, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, sort_keys=True)
    return p_csv, p_meta


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 L1 交接契约 out/data_snapshot.csv（本地拼装，不取数）")
    ap.add_argument("--today", default=None, help="把哪一天当作今天（算 stale_days），默认系统日期")
    ap.add_argument("--strict", action="store_true",
                    help="缺主表/关键列直接 exit 2（这是默认行为，保留该开关只为让文档命令可直接复制）")
    ap.add_argument("--quiet", action="store_true", help="只打印一行结果")
    args = ap.parse_args()

    try:
        rows, meta = build(args.today)
        p_csv, p_meta = write(rows, meta)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"✗ L1 快照生成失败：{e}", file=sys.stderr)
        return 2

    if args.quiet:
        print(f"✓ data_snapshot.csv {meta['rows']} 行（数据日 {meta['nav_date']}，滞后 {meta['stale_days']} 天）")
        return 0

    print("=" * 70)
    print("L1 全池快照 → out/data_snapshot.csv")
    print("=" * 70)
    print(f"个股行数      {meta['rows']}（跳过取数失败 {meta['skipped_no_data']}）")
    print(f"数据日        {meta['nav_date']}（滞后 {meta['stale_days']} 天）")
    print(f"自有 PE 分位  覆盖 {meta['pe_pct_self_coverage']} 只（只有阶段二候选有）")
    print(f"其他池        ETF {meta['etf_rows']} 行｜场外债基 {meta['otc_rows']} 行")
    print("\n字段覆盖率（越低越要查上游）：")
    for c, v in meta["coverage"].items():
        bar = "█" * int(round(v * 20))
        print(f"  {c:<12} {v:>6.1%} {bar}")
    print(f"\n输出：{p_csv}")
    print(f"      {p_meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
