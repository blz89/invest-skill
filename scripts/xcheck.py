# -*- coding: utf-8 -*-
"""交叉校验：拿同花顺口径撞我们自算的数字，撞不上就说明有一边错了。

用法
----
    python xcheck.py                     # 全跑（场外基金 / ETF / 估值 / 财务）
    python xcheck.py --otc-top 8         # 场外池取前 8 只做校验
    python xcheck.py --skip-etf          # 跳过 ETF（不出网取日线）
    python xcheck.py --out out/xcheck.md # 落盘路径

判定口径（写在代码里，避免每次讨论口径）
----------------------------------------
- 收差 |Δ| ≤ 0.30pp → 一致；≤ 0.80pp → 接近（口径差可解释）；> 0.80pp → 冲突，需查
- 估值 PE 相对差 ≤ 5% → 一致；≤ 15% → 接近；> 15% → 冲突（注意时点差：我方收盘价 vs 对方快照）
- 财务 ROE 差 ≤ 1.0pp → 一致；≤ 3.0pp → 接近；> 3.0pp → 冲突（注意「加权/摊薄」「年报/最新季」口径）

**本工具不写任何策略产物，只产出一份诊断报告** —— 它替不了回测，只回答「我们的数字可不可信」。
"""
from __future__ import annotations

import argparse
import csv
import json
import os

import config
import fuyao


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _grade(delta_pp: float | None, tol_ok: float, tol_near: float) -> str:
    if delta_pp is None:
        return "不可比"
    a = abs(delta_pp)
    if a <= tol_ok:
        return "一致"
    if a <= tol_near:
        return "接近"
    return "冲突"


def _grade_rel(ours: float | None, theirs: float | None) -> tuple[str, float | None]:
    if ours in (None, 0) or theirs is None:
        return "不可比", None
    delta = (theirs - ours) / abs(ours) * 100.0
    a = abs(delta)
    if a <= 5:
        return "一致", round(delta, 2)
    if a <= 15:
        return "接近", round(delta, 2)
    return "冲突", round(delta, 2)


# ---------------------------------------------------------------- A 场外基金
def check_otc(top: int = 5) -> list[dict]:
    rows = _read_csv(os.path.join(config.OUT_DIR, "stage1_otc.csv"))
    if not rows:
        return [{"section": "A", "item": "stage1_otc.csv", "verdict": "缺失",
                 "note": "先跑 python otc.py"}]
    rows.sort(key=lambda r: _f(r.get("score"), 0.0), reverse=True)
    out = []
    for r in rows[:top]:
        code = str(r.get("code") or "").zfill(6)
        item = {"section": "A", "code": code, "name": r.get("name"),
                "ours_ret_1y": _f(r.get("ret_1y")), "ours_ret_3y": _f(r.get("ret_3y"))}
        try:
            fr = fuyao.fund_returns(code)
        except fuyao.FuyaoError as e:
            item.update({"verdict": "取数失败", "note": str(e)[:80]})
            out.append(item)
            continue
        if not fr:
            item.update({"verdict": "无数据", "note": "同花顺无该基金收益"})
            out.append(item)
            continue
        y = _f(fr.get("return_year"))
        t = _f(fr.get("return_tyear"))
        item["theirs_ret_1y"] = None if y is None else y / 100.0
        ann3 = None
        if t is not None:
            ann3 = (1 + t / 100.0) ** (1 / 3.0) - 1
        item["theirs_ret_3y"] = ann3
        item["d1_pp"] = None if (y is None or item["ours_ret_1y"] is None) else \
            round((y / 100.0 - item["ours_ret_1y"]) * 100, 3)
        item["d3_pp"] = None if (ann3 is None or item["ours_ret_3y"] is None) else \
            round((ann3 - item["ours_ret_3y"]) * 100, 3)
        v1 = _grade(item["d1_pp"], 0.30, 0.80)
        v3 = _grade(item["d3_pp"], 0.30, 0.80)
        item["verdict"] = "冲突" if "冲突" in (v1, v3) else (
            "接近" if "接近" in (v1, v3) else ("一致" if "一致" in (v1, v3) else "不可比"))
        out.append(item)
    return out


# ---------------------------------------------------------------- B ETF 收益/回撤
def check_etf() -> list[dict]:
    path = os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(path):
        return [{"section": "B", "item": "stage3_plan.json", "verdict": "缺失",
                 "note": "先跑 python plan.py"}]
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    out = []
    seen = set()
    for p in plan.get("positions") or []:
        code = str(p.get("code") or "")
        m = p.get("etf_metrics") or {}
        if code in seen or not code or "ret_1y" not in m:
            continue
        seen.add(code)
        item = {"section": "B", "code": code, "name": p.get("name"),
                "ours_ret_1y": _f(m.get("ret_1y")), "ours_dd_1y": _f(m.get("dd_1y"))}
        try:
            df = fuyao.etf_hist(code)
        except fuyao.FuyaoError as e:
            item.update({"verdict": "取数失败", "note": str(e)[:80]})
            out.append(item)
            continue
        if df is None or len(df) < 60:
            item.update({"verdict": "无数据", "note": "同花顺无该 ETF 日线"})
            out.append(item)
            continue
        cl = df["close"].tolist()
        n = min(251, len(cl))
        theirs_ret = cl[-1] / cl[-n] - 1
        peak, dd = cl[-n], 0.0
        for x in cl[-n:]:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1)
        item["theirs_ret_1y"] = round(theirs_ret, 4)
        item["theirs_dd_1y"] = round(dd, 4)
        item["d1_pp"] = None if item["ours_ret_1y"] is None else \
            round((theirs_ret - item["ours_ret_1y"]) * 100, 3)
        item["ddd_pp"] = None if item["ours_dd_1y"] is None else \
            round((dd - item["ours_dd_1y"]) * 100, 3)
        v1 = _grade(item["d1_pp"], 0.30, 0.80)
        v2 = _grade(item["ddd_pp"], 0.30, 0.80)
        item["verdict"] = "冲突" if "冲突" in (v1, v2) else (
            "接近" if "接近" in (v1, v2) else ("一致" if "一致" in (v1, v2) else "不可比"))
        out.append(item)
    return out


# ---------------------------------------------------------------- C 个股估值
def check_valuation(top: int = 5) -> list[dict]:
    rows = [r for r in _read_csv(os.path.join(config.OUT_DIR, "stage1_all.csv"))
            if str(r.get("data_ok")).strip().lower() == "true"
            and str(r.get("stage1_pass")).strip().lower() == "true"]
    if not rows:
        return [{"section": "C", "item": "stage1_all.csv", "verdict": "缺失",
                 "note": "先跑 python screen.py（stage1_pass=True 的行）"}]
    rows.sort(key=lambda r: _f(r.get("roe_avg5"), -99), reverse=True)
    picks = rows[:top]
    try:
        vals = fuyao.valuations([r.get("code") for r in picks])
    except fuyao.FuyaoError as e:
        return [{"section": "C", "item": "valuations", "verdict": "取数失败",
                 "note": str(e)[:80]}]
    out = []
    for r in picks:
        code = str(r.get("code") or "").zfill(6)
        ours = _f(r.get("pe"))
        v = vals.get(code) or {}
        theirs = _f(v.get("pe_ttm"))
        verdict, delta = _grade_rel(ours, theirs)
        out.append({"section": "C", "code": code, "name": r.get("name"),
                    "ours_pe": ours, "theirs_pe_ttm": theirs,
                    "rel_pct": delta, "verdict": verdict})
    return out


# ---------------------------------------------------------------- D 个股财务
KEYMAP = {"index_weighted_avg_roe": "加权ROE", "assets_debt_ratio": "资产负债率",
          "net_profit_cash_content": "净利润现金含量", "index_deduct_weighted_avg_roe": "扣非ROE"}


def check_financials(top: int = 5) -> list[dict]:
    rows = [r for r in _read_csv(os.path.join(config.OUT_DIR, "stage1_all.csv"))
            if str(r.get("data_ok")).strip().lower() == "true"]
    if not rows:
        return [{"section": "D", "item": "stage1_all.csv", "verdict": "缺失", "note": "-"}]
    rows.sort(key=lambda r: _f(r.get("roe_avg5"), -99), reverse=True)
    out = []
    for r in rows[:top]:
        code = str(r.get("code") or "").zfill(6)
        item = {"section": "D", "code": code, "name": r.get("name")}
        # CSV 侧：roe_list 最后一非空值 = 最近一个年报 ROE；debt_ratio
        roe_list = []
        raw = r.get("roe_list") or "[]"
        try:
            roe_list = json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
        ours_roe = next((x for x in reversed(roe_list) if x is not None), None)
        item["ours_roe"] = ours_roe
        item["ours_debt"] = _f(r.get("debt_ratio"))
        try:
            ind = fuyao.indicators(code, "2025-4")
        except fuyao.FuyaoError as e:
            item.update({"verdict": "取数失败", "note": str(e)[:80]})
            out.append(item)
            continue
        flat = {}
        for blk in ind.values():
            flat.update(blk)
        theirs_roe = _f(flat.get("index_weighted_avg_roe"))
        item["theirs_roe"] = theirs_roe
        item["theirs_debt"] = _f(flat.get("assets_debt_ratio"))
        if ours_roe is None or theirs_roe is None:
            item["verdict"] = "不可比"
        else:
            d = round(theirs_roe - ours_roe, 2)
            item["d_roe_pp"] = d
            item["verdict"] = _grade(d, 1.0, 3.0)
        out.append(item)
    return out


# ---------------------------------------------------------------- 报告
def to_markdown(res: dict) -> str:
    L = ["# 数据交叉校验报告（同花顺 vs 本技能自算）", "",
         f"生成时间：{__import__('datetime').datetime.now().isoformat(timespec='seconds')}",
         f"数据源：{fuyao.BASE_URL}", ""]
    counts = {"一致": 0, "接近": 0, "冲突": 0, "不可比": 0, "缺失": 0, "取数失败": 0, "无数据": 0}
    for sec, title in (("A", "A. 场外基金区间收益（我们：自算年化；对方：累计区间收益折年化）"),
                       ("B", "B. ETF 近 1 年收益与最大回撤"),
                       ("C", "C. 个股 PE（我们：新浪板块快照 per；对方：同花顺 pe_ttm）"),
                       ("D", "D. 个股 ROE/资产负债率（对方：2025 年报）")):
        rows = res.get(sec) or []
        L += [f"## {title}", ""]
        if not rows:
            L += ["（无样本）", ""]
            continue
        for r in rows:
            counts[r.get("verdict", "缺失")] = counts.get(r.get("verdict", "缺失"), 0) + 1
        if sec == "A":
            L += ["| 代码 | 名称 | 我方 1Y | 对方 1Y | Δpp | 我方 3Y年化 | 对方 3Y年化 | Δpp | 判定 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
            for r in rows:
                L.append("| {} | {} | {:.2%} | {} | {} | {:.2%} | {} | {} | {} |".format(
                    r.get("code"), r.get("name") or "",
                    _f(r.get("ours_ret_1y"), 0) or 0,
                    f"{r['theirs_ret_1y']:.2%}" if r.get("theirs_ret_1y") is not None else "—",
                    r.get("d1_pp", "—"),
                    _f(r.get("ours_ret_3y"), 0) or 0,
                    f"{r['theirs_ret_3y']:.2%}" if r.get("theirs_ret_3y") is not None else "—",
                    r.get("d3_pp", "—"), r.get("verdict")))
        elif sec == "B":
            L += ["| 代码 | 名称 | 我方 1Y | 对方 1Y | Δpp | 我方回撤 | 对方回撤 | Δpp | 判定 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
            for r in rows:
                L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    r.get("code"), r.get("name") or "",
                    f"{_f(r.get('ours_ret_1y')):.2%}" if _f(r.get("ours_ret_1y")) is not None else "—",
                    f"{r['theirs_ret_1y']:.2%}" if r.get("theirs_ret_1y") is not None else "—",
                    r.get("d1_pp", "—"),
                    f"{_f(r.get('ours_dd_1y')):.2%}" if _f(r.get("ours_dd_1y")) is not None else "—",
                    f"{r['theirs_dd_1y']:.2%}" if r.get("theirs_dd_1y") is not None else "—",
                    r.get("ddd_pp", "—"), r.get("verdict")))
        elif sec == "C":
            L += ["| 代码 | 名称 | 我方 PE | 对方 pe_ttm | 相对差 | 判定 |",
                  "| --- | --- | --- | --- | --- | --- |"]
            for r in rows:
                L.append("| {} | {} | {} | {} | {} | {} |".format(
                    r.get("code"), r.get("name") or "", r.get("ours_pe", "—"),
                    r.get("theirs_pe_ttm", "—"),
                    f"{r['rel_pct']:+.2f}%" if r.get("rel_pct") is not None else "—",
                    r.get("verdict")))
        else:
            L += ["| 代码 | 名称 | 我方 ROE | 对方 ROE | Δpp | 我方负债率 | 对方负债率 | 判定 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
            for r in rows:
                L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    r.get("code"), r.get("name") or "",
                    r.get("ours_roe", "—"), r.get("theirs_roe", "—"), r.get("d_roe_pp", "—"),
                    r.get("ours_debt", "—"), r.get("theirs_debt", "—"), r.get("verdict")))
        L.append("")
    L += ["## 汇总", "",
          "｜".join(f"{k} {v}" for k, v in counts.items() if v), "",
          "判读指南：", "",
          "- **一致/接近**：两边独立算出来能对上，说明数字可信，继续用。",
          "- **冲突**：先查时点差（我方取上一交易日收盘/年度报告期，对方是快照/最新期），",
          "  再查口径（加权 vs 摊薄 ROE、TTM vs 静态 PE）；仍冲突则说明有一边数据错，**以能溯源的一边为准**。",
          "- 本报告不改变任何策略参数，只是一份体检单。", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="同花顺 vs 自算 数据交叉校验")
    ap.add_argument("--otc-top", type=int, default=5)
    ap.add_argument("--stock-top", type=int, default=5)
    ap.add_argument("--skip-etf", action="store_true")
    ap.add_argument("--out", default=os.path.join(config.OUT_DIR, "xcheck.md"))
    a = ap.parse_args()

    if not fuyao.available():
        print("[xcheck] 没有同花顺 key（FUYAO_API_KEY 或 config.FUYAO['key_file']），跳过。")
        return 0

    res = {"A": check_otc(a.otc_top), "C": check_valuation(a.stock_top),
           "D": check_financials(a.stock_top)}
    res["B"] = [] if a.skip_etf else check_etf()

    md = to_markdown(res)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(md)

    for sec in ("A", "B", "C", "D"):
        for r in res[sec]:
            print(f"[{sec}] {r.get('code') or r.get('item'):8s} "
                  f"{str(r.get('name') or '')[:14]:14s} {r.get('verdict')}"
                  f"{('  ' + r['note']) if r.get('note') else ''}")
    print(f"[xcheck] 报告 -> {a.out}")
    print(f"[xcheck] provenance -> {fuyao.dump_provenance()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
