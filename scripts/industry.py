#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业层（同花顺一级行业 + 特色标签）—— 替代百度行业分类，做真实行业集中度。

为什么要这一层
--------------
技能的行业集中度门禁（单行业 ≤ 25%）一直用的是百度行业（`stock_sector_detail`），
那是**旧版行业分类**，颗粒度粗、更新慢，还常出现「同一家公司被归到两个板块」。
同花顺一级行业是新的、细的、全市场覆盖的（中药 / 化学制品 / 汽车零部件 / 白酒），
而且同一个接口顺带给出**特色标签**（高股息、低市盈率、先进制造、机构重仓…），
这比「行业」更接近选股时真正关心的风格。

数据全部来自同花顺 `fuyao.py`（`ths-index-list-by-stock`，一次可查 100 只）。
没 key / 接口挂了 → 这一层跳过，集中度退回百度行业，主链路照常跑（fail-soft）。

输入
----
- `out/stage1_all.csv`：候选池（`build` 时取 code 列）
- `out/stage3_plan.json`：持仓（`--check` 时算集中度）

输出
----
- `out/industry_map.csv`：code,name,industry,region,tszs（一行一只，可缓存复用）
- `out/industry_brief.md`：行业分布速览（可选）

用法
----
    python scripts/industry.py --refresh              # 刷全池（默认取 stage1_all）
    python scripts/industry.py --refresh --limit 50   # 只刷前 50 只（调试）
    python scripts/industry.py --check                # 看当前持仓的行业集中度
    python scripts/industry.py --check --cap 0.20     # 换阈值
    python scripts/industry.py --show                 # 打印已有映射
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import fuyao

MAP_PATH = os.path.join(config.OUT_DIR, "industry_map.csv")
BRIEF_PATH = os.path.join(config.OUT_DIR, "industry_brief.md")
COLS = ["code", "name", "industry", "region", "tszs", "source", "fetched_at"]

# 特色标签里值得单独看一眼的（只做展示，不参与打分）
FLAVOR = ["高股息股", "低市盈率", "高ROE股", "行业龙头", "先进制造",
          "机构重仓股", "社保新进", "养老金持股", "百元股", "周期", "消费"]


# ---------------------------------------------------------------- 纯函数（可测）
def _pick(items, want: str) -> list[str]:
    """从一个 type 分组里取出名字列表（同花顺返回 [{thscode,name,type}]）。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") and it["type"] != want:
            continue
        nm = (it.get("name") or "").strip()
        if nm:
            out.append(nm)
    return out


from codes import norm_code  # noqa: E402  （唯一实现见 codes.py）

UNCLASSIFIED = "未分类"

# 代码级行业修正（2026-11-07 B 类 #6）：
# 新浪 49 板块的成员表是**历史快照**，借壳/更名后不会自动迁移，于是留下旧行业标签。
#   600233 圆通速递 —— 借壳前是「大杨创世」（服装），新浪仍把它归在「服装鞋类」；
#   screen.py 的 pe_pct_ind / pb_pct_ind 按同行业样本算分位，用服装样本给快递股打分位
#   会系统性失真（快递 PE 低、服装 PE 高 → 百分位被压得过低）。
# 只放**能确证**的代码（同花顺一级行业 = 交通运输），不做关键词猜测；新增一条必须有
# 第二个数据源交叉验证，否则宁可让它留在「未分类」。
INDUSTRY_FIX = {"600233": "交通运输"}


def normalize(raw, code: str = None) -> str:
    """行业标签归一：先查代码级修正，再清洗，缺失一律 fail-closed 成「未分类」。

    为什么缺失必须显式成「未分类」而不是空串：空串在 groupby / 集中度里会被静默丢弃，
    等于把「不知道」当成「没有风险」；显式「未分类」会作为一个行业参与 25% 集中度
    计算并触发告警（render 时也看得见）。这是方向性选择：宁可多报风险，不可漏报。
    """
    c = norm_code(code) if code else ""
    if c in INDUSTRY_FIX:
        return INDUSTRY_FIX[c]
    s = str(raw if raw is not None else "").strip()
    if s.lower() in ("", "nan", "none", "null", "nat", "-", "--", "—"):
        return UNCLASSIFIED
    return s


def rows_of(codes: list[str], raw: dict) -> list[dict]:
    """把接口返回拼成 CSV 行。raw: {ths_code: [ {name,type}, ... ]}"""
    out = []
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code in codes:
        c = norm_code(code)
        key = c + ".SH" if c.startswith(("6", "9")) else c + ".SZ"
        items = raw.get(key) or raw.get(c) or []
        ind = _pick(items, "industry")
        reg = _pick(items, "region")
        tszs = [x for x in _pick(items, "tszs")
                if x not in ("同花顺全A(沪深)", "同花顺全A(沪深京)", "沪股通", "深股通")]
        out.append({
            "code": c,
            "name": "",
            "industry": normalize(ind[0] if ind else "", c),
            "region": reg[0] if reg else "",
            "tszs": "|".join(tszs[:8]),
            "source": "ths",
            "fetched_at": stamp,
        })
    return out


def concentration(positions: list[dict], cap: float = 0.25,
                  base: float | None = None) -> dict:
    """按行业汇总金额占「权益层」的比。positions: [{code,name,amount}]

    base 是分母：技能的行业上限 25% 是对**权益层**说的，不是对股票层小计说的，
    所以默认用 plan 的 equity_spent；没给才退回股票层小计。

    「未分类」不计入 25% 上限（数据缺失 ≠ 一个真实行业），单独返回
    `unclassified` / `unclassified_weight` 供调用方按占比告警。
    """
    total = 0.0
    unknown = 0.0
    by: dict[str, float] = {}
    names: dict[str, list[str]] = {}
    for p in positions or []:
        amt = float(p.get("amount") or 0)
        if amt <= 0:
            continue
        total += amt
        ind = normalize(p.get("industry"), p.get("code"))
        if ind == UNCLASSIFIED:
            # 未分类是「数据缺」不是「同一个行业」：不占 25% 行业上限，
            # 单独按占比告警（占比过高说明映射失效，比误报超限更该修数据）
            unknown += amt
            continue
        by[ind] = by.get(ind, 0.0) + amt
        names.setdefault(ind, []).append(p.get("name") or p.get("code") or "?")
    total = float(base or 0) or total or 1.0
    rows = []
    for ind, amt in sorted(by.items(), key=lambda kv: -kv[1]):
        w = amt / total
        rows.append({"industry": ind, "amount": round(amt, 2),
                     "weight": round(w, 6), "over": w > cap + 1e-9,
                     "names": names.get(ind) or []})
    return {"total": round(total, 2), "cap": cap, "rows": rows,
            "max_weight": round(rows[0]["weight"], 6) if rows else 0.0,
            "unclassified": round(unknown, 2),
            "unclassified_weight": round(unknown / total, 6),
            "violated": [r for r in rows if r["over"]]}


def res_total(positions: list[dict]) -> float:
    return sum(float(p.get("amount") or 0) for p in positions or [])


# ---------------------------------------------------------------- 取数
def load_map() -> dict[str, dict]:
    if not os.path.exists(MAP_PATH):
        return {}
    out = {}
    with io.open(MAP_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = norm_code(r.get("code"))
            if c:
                out[c] = r
    return out


def save_map(rows: list[dict]) -> str:
    os.makedirs(config.OUT_DIR, exist_ok=True)
    tmp = MAP_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLS})
    os.replace(tmp, MAP_PATH)
    return MAP_PATH


def merge_rows(old_rows: list[dict], new_rows: list[dict],
               keep_codes: list[str] | None = None) -> list[dict]:
    """新映射优先、旧映射兜底；keep_codes（当前持仓）强制保留旧行。

    2026-11-21 修（S4 巡检 A 类）：`--refresh` 原来整表覆盖，同花顺某一批失败
    （`fetch_rows` 跳批）就会把已知代码**抹掉**，次日巡检把它们算成「未分类」，
    误报行业超限（11-20 的 002648/601058 即此）。现在保留旧值兜底。
    """
    merged: dict[str, dict] = {}
    for r in old_rows or []:
        c = norm_code(r.get("code"))
        if c:
            merged[c] = r
    for r in new_rows or []:
        c = norm_code(r.get("code"))
        if c:
            merged[c] = r
    keep = set(norm_code(c) for c in (keep_codes or []) if norm_code(c))
    out = []
    for c, r in merged.items():
        if c in keep and not (r.get("industry") or "").strip():
            # 持仓代码的新行没拿到行业（接口返回空）→ 用旧行
            old = next((x for x in (old_rows or []) if norm_code(x.get("code")) == c), None)
            if old and (old.get("industry") or "").strip():
                r = old
        out.append({k: r.get(k, "") for k in COLS})
    return out


def coverage_ok(old_n: int, new_n: int, min_ratio: float = 0.9) -> bool:
    """新映射比旧映射少太多（<90%）→ 视为刷新失败，拒绝覆盖（fail-closed）。"""
    if old_n <= 0:
        return True
    return new_n >= old_n * min_ratio


def holdings_codes(ledger: str = "50k") -> list[str]:
    """当前账本持仓代码（用于 --refresh 强制保留旧映射）。"""
    try:
        sf = config.PAPER["ledgers"][ledger]["state_file"]
        with io.open(sf, encoding="utf-8") as f:
            st = json.load(f)
    except (KeyError, TypeError, OSError, ValueError):
        return []
    codes = []
    for c in (st.get("holdings") or {}):
        c2 = norm_code(c)
        if c2:
            codes.append(c2)
    return codes


def codes_from_stage1(limit: int | None = None) -> list[str]:
    p = os.path.join(config.OUT_DIR, "stage1_all.csv")
    if not os.path.exists(p):
        return []
    codes = []
    with io.open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = norm_code(r.get("code"))
            if c and len(c) == 6:
                codes.append(c)
    if limit:
        codes = codes[:limit]
    return codes


def fetch_rows(codes: list[str], chunk: int = 100, sleep: float = 0.25) -> list[dict]:
    """分批取（同花顺单次上限 100 只）。任何一批失败就跳过这批，不整体失败。"""
    import time
    rows: list[dict] = []
    ok = fail = 0
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        try:
            raw = fuyao.stock_industries(part)
            rows.extend(rows_of(part, raw or {}))
            ok += len(part)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 行业批次 {i//chunk+1} 失败：{type(e).__name__}: {e}")
            fail += len(part)
        time.sleep(sleep)
    print(f"[info] 行业映射：成功 {ok} 只 / 失败 {fail} 只")
    return rows


def plan_base(plan_path: str | None = None) -> float:
    """行业上限的分母：权益层实际支出（equity_spent），没有就用 equity_amount。"""
    import json
    p = plan_path or os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(p):
        return 0.0
    with io.open(p, encoding="utf-8") as f:
        plan = json.load(f)
    for k in ("equity_spent", "equity_amount"):
        v = plan.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def plan_positions(plan_path: str | None = None) -> list[dict]:
    import json
    p = plan_path or os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        plan = json.load(f)
    mp = load_map()
    out = []
    for pos in plan.get("positions") or []:
        if pos.get("layer") not in (None, "", "个股", "stock"):
            continue
        c = norm_code(pos.get("code"))
        rec = mp.get(c) or {}
        out.append({
            "code": c, "name": pos.get("name") or "",
            "amount": pos.get("amount") or 0,
            "weight": pos.get("weight") or 0,
            "industry": normalize(rec.get("industry"), c),
        })
    return out


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="行业层（同花顺一级行业 + 特色标签）")
    ap.add_argument("--refresh", action="store_true", help="刷新 out/industry_map.csv")
    ap.add_argument("--check", action="store_true", help="算当前持仓的行业集中度")
    ap.add_argument("--show", action="store_true", help="打印已有映射")
    ap.add_argument("--limit", type=int, default=None, help="--refresh 时最多取多少只")
    ap.add_argument("--cap", type=float, default=0.25, help="单行业上限（默认 0.25）")
    ap.add_argument("--plan", default=None, help="指定 plan JSON 路径")
    a = ap.parse_args()

    if not fuyao.available():
        print("[error] 没有同花顺 API key（环境变量 FUYAO_API_KEY 或 work/fuyao.key）")
        return 2

    if a.refresh:
        codes = codes_from_stage1(a.limit)
        if not codes:
            print("[error] out/stage1_all.csv 不存在或没有 code 列，先跑 run.py")
            return 1
        old_rows = list(load_map().values())
        rows = fetch_rows(codes)
        if not rows:
            print("[error] 一只都没取到")
            return 1
        if not coverage_ok(len(old_rows), len(rows)):
            print(f"[error] 新映射 {len(rows)} 行 < 旧 {len(old_rows)} 行的 90%："
                  f"接口可能大面积失败，拒绝覆盖（保留旧 {MAP_PATH}）")
            return 1
        merged = merge_rows(old_rows, rows, holdings_codes())
        path = save_map(merged)
        named = sum(1 for r in merged if str(r.get("industry") or "").strip())
        carried = len(merged) - len(rows)
        print(f"[ok] {path}｜{len(merged)} 行（新 {len(rows)} / 旧兜底 {carried}）｜有行业 {named} 只")
        from collections import Counter
        top = Counter(str(r["industry"]) for r in merged if str(r.get("industry") or "").strip()).most_common(8)
        print("[info] 行业分布 top8：" + "，".join(f"{k} {v}" for k, v in top))
        return 0

    if a.check:
        pos = plan_positions(a.plan)
        if not pos:
            print("[error] 没读到持仓（plan 不存在或没有 stock 层）")
            return 1
        base = plan_base(a.plan) or res_total(pos)
        res = concentration(pos, a.cap, base)
        print(f"[行业集中度] 分母（权益层）{res['total']:.2f} 元，单行业上限 {res['cap']:.0%}")
        for r in res["rows"]:
            mark = "  ✗超限" if r["over"] else ""
            print(f"  {r['industry']:12s} {r['amount']:>10.2f} 元  {r['weight']:>7.2%}{mark}"
                  f"  {('、'.join(r['names']))[:40]}")
        # 覆盖度自检：映射不全时未覆盖的按「未分类」计，行业上限会静默失效
        try:
            p1 = os.path.join(config.OUT_DIR, "stage1_all.csv")
            pool_n = 0
            if os.path.exists(p1):
                with open(p1, encoding="utf-8-sig", newline="") as fh:
                    pool_n = max(0, sum(1 for _ in fh) - 1)
            mp = load_map()
            cov = sum(1 for _, r in mp.items() if str(r.get("industry") or "").strip())
            need = float(config.INDUSTRY.get("min_coverage") or 0.9)
            if pool_n and cov / pool_n < need:
                print(f"[warn] 行业映射覆盖 {cov}/{pool_n} = {cov/pool_n:.0%}"
                      f"（<{need:.0%}）：未覆盖按「未分类」计，行业上限可能静默失效，先跑 --refresh")
        except Exception:  # noqa: BLE001
            pass
        if res["violated"]:
            print(f"[fail] {len(res['violated'])} 个行业超限")
            return 1
        print("[ok] 没有行业超限")
        return 0

    if a.show:
        mp = load_map()
        print(f"[info] {MAP_PATH}｜{len(mp)} 只")
        for c, r in list(mp.items())[:20]:
            print(f"  {c} {r.get('industry',''):12s} {r.get('tszs','')[:50]}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
