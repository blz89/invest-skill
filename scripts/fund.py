#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金层：同花顺场外基金接口的**备用源 + 交叉校验**。

为什么要有这一层
----------------
`otc.py` 的场外债券基金池（1,087 只）主源是东财 `fund_*_em` 系列。它能给费率/申购状态，
但**净值与收益只有一份**——主源一变（字段改名、限流、口径调整），池子就会静默变形。
本层不替代主源，只做三件事：

1. **独立复算**：从同花顺取净值序列，用与 `otc.py` 相同的公式重算 1Y/3Y 年化与最大回撤；
2. **交叉校验**：把复算结果与 `out/stage1_otc.csv` 里的数字对表，超阈值就报；
3. **健康检查**：抽 N 只跑一遍，产出 `out/fund_health.md` —— 这是「主源还靠得住吗」的体检单。

铁律：本层**不写** `out/stage1_otc.csv`。池子只有一个生产者（`otc.py`），两个生产者必然打架。

用法
----
    python fund.py --code 018074          # 单只：净值序列 + 复算指标 + 同花顺区间收益
    python fund.py --health --n 5         # 抽样健康检查 → out/fund_health.md
    python fund.py --health --no-write    # 只打印
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import fuyao
from codes import norm_fund_code          # 场外基金代码归一（唯一实现，见 codes.py）

# 兼容旧名：本模块内部仍按基金口径调用
norm_code = norm_fund_code

HEALTH_PATH = os.path.join(config.OUT_DIR, "fund_health.md")
OTC_CSV = os.path.join(config.OUT_DIR, "stage1_otc.csv")
TRADING_DAYS = 244          # 与 otc.py 的 TRADING_DAYS 一致，跨源对比才有意义


class FundError(RuntimeError):
    pass


# ------------------------------------------------------------------ 小工具
def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None          # 滤掉 nan


def _d(day) -> dt.date | None:
    """日期归一：接受 "YYYY-MM-DD" / "YYYYMMDD" / "YYYY/MM/DD"，
    也接受同花顺的**毫秒 Unix 时间戳**（int）与秒级时间戳。"""
    if isinstance(day, (int, float)) and day == day and day > 0:
        v = float(day)
        if v > 1e11:                 # 毫秒
            v /= 1000.0
        try:
            # 固定按北京时间换算：时间戳来自境内披露口径，按运行机器的本地时区
            # 换算会在 UTC 机器（CI/服务器）上整体差一天
            tz = dt.timezone(dt.timedelta(hours=8))
            return dt.datetime.fromtimestamp(v, tz=tz).date()
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(str(day or "").strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ 数据
def nav_series(code, rng: str = "tyear") -> list[tuple[str, float]]:
    """同花顺场外基金净值序列 → [(YYYY-MM-DD, 单位净值)]，按日期升序、去重。

    `rng` 是接口的 `range`（year≈近一年、tyear≈近三年），不是条数。
    """
    code = norm_code(code)
    if not code:
        raise FundError("基金代码为空")
    rows = fuyao.fund_nav(code, rng=rng)
    got: dict[str, float] = {}
    for r in rows or []:
        d = _d(r.get("nav_date") or r.get("date") or "")
        # **必须用复权净值**：分红会让单位净值台阶式下跌，用 unit_nav 算收益会系统性少算
        # （实测 012310：unit 1Y 0.40% vs adj 3.02%，差一个分红）
        v = _f(r.get("adj_nav")) or _f(r.get("unit_nav"))
        if d and v and v > 0:
            got[d.isoformat()] = v
    if not got:
        return []
    return sorted(got.items())


def _slice(series, days: int):
    """取最近 days 个自然日内的净值段（含端点最近一条）。"""
    if not series:
        return []
    last = _d(series[-1][0])
    if last is None:
        return []
    start = last - dt.timedelta(days=days)
    out = [(d0, v) for d0, v in series if d0 >= start.isoformat()]
    return out if len(out) >= 2 else []


def _dd(seg) -> float | None:
    """区间最大回撤（负值）。"""
    if len(seg) < 2:
        return None
    peak, worst = seg[0][1], 0.0
    for _, v in seg:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def _ann(cum: float | None, years: float) -> float | None:
    if cum is None or years <= 0 or cum <= -1:
        return None
    return (1.0 + cum) ** (1.0 / years) - 1.0


def _vol(seg) -> float | None:
    """日收益率标准差年化。"""
    rets = []
    for i in range(1, len(seg)):
        prev = seg[i - 1][1]
        if prev > 0:
            rets.append(seg[i][1] / prev - 1.0)
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def metrics_from_nav(series) -> dict:
    """从净值（**复权**）序列复算指标。ret_1y / ret_3y 是**年化**，dd 是负值。"""
    if not series or len(series) < 60:
        return {}
    last = series[-1][1]
    out: dict = {"nav_date": series[-1][0], "n_days": len(series)}

    seg1 = _slice(series, 365)
    if seg1:
        out["ret_1y"] = last / seg1[0][1] - 1.0
        out["dd_1y"] = _dd(seg1)
        out["span_1y_days"] = (_d(series[-1][0]) - _d(seg1[0][0])).days

    seg3 = _slice(series, 1095)
    if seg3:
        cum3 = last / seg3[0][1] - 1.0
        span = (_d(series[-1][0]) - _d(seg3[0][0])).days
        years = max(span / 365.0, 0.1)
        out["ret_3y_cum"] = cum3
        out["ret_3y"] = _ann(cum3, years)
        out["dd_3y"] = _dd(seg3)
        out["vol_3y"] = _vol(seg3)
        out["span_3y_days"] = span
    return out


THEIR_MAP = {
    "return_year": "ret_1y", "return_tyear": "ret_3y", "return_fyear": "ret_5y",
    "return_nowyear": "ret_ytd", "return_now": "ret_since",
    "peer_average_tyear": "peer_ret_3y",
    "rank_tyear": "rank_3y", "rank_total_tyear": "rank_total_3y",
}


def their_returns(code) -> dict:
    """同花顺区间收益（**累计百分比原值**，非年化）→ 我方字段名。"""
    code = norm_code(code)
    if not code:
        return {}
    r = fuyao.fund_returns(code) or {}
    return {v: r[k] for k, v in THEIR_MAP.items() if k in r}


def their_drawdowns(code) -> dict:
    """同花顺区间最大回撤（负百分数原值）→ 小数。"""
    code = norm_code(code)
    if not code:
        return {}
    r = fuyao.fund_drawdowns(code) or {}
    out = {}
    for k in ("year", "tyear", "fyear", "now"):
        v = _f(r.get(k))
        if v is not None:
            out[k] = v / 100.0
    return out


# ------------------------------------------------------------------ 校验
def compare(code, ours: dict | None = None) -> dict:
    """单只：复算 vs CSV 里的我方数字 vs 同花顺区间收益。返回一行字典。"""
    code = norm_code(code)
    row = {"code": code}
    try:
        series = nav_series(code)
    except FundError as e:
        row["error"] = str(e)
        return row
    calc = metrics_from_nav(series)
    if not calc:
        row["error"] = "净值序列不足（<60 条）"
        return row
    theirs = their_returns(code)

    ours = ours or {}
    row["nav_date"] = calc.get("nav_date")
    row["n_days"] = calc.get("n_days")
    row["calc_ret_1y"] = calc.get("ret_1y")
    row["ours_ret_1y"] = _f(ours.get("ret_1y"))
    row["calc_ret_3y"] = calc.get("ret_3y")
    row["ours_ret_3y"] = _f(ours.get("ret_3y"))
    row["calc_dd_3y"] = calc.get("dd_3y")
    row["ours_dd_3y"] = _f(ours.get("dd_3y"))

    t3 = _f(theirs.get("ret_3y"))
    row["theirs_tyear_cum"] = t3 / 100.0 if t3 is not None else None
    row["theirs_tyear_ann"] = _ann(row["theirs_tyear_cum"], 3.0) if t3 is not None else None
    dd = their_drawdowns(code)
    row["theirs_dd_3y"] = dd.get("tyear")
    row["theirs_rank_3y"] = theirs.get("rank_3y")
    row["theirs_rank_total_3y"] = theirs.get("rank_total_3y")

    diffs = []
    for a, b, name in (("calc_ret_1y", "ours_ret_1y", "1Y"),
                       ("calc_ret_3y", "ours_ret_3y", "3Y")):
        x, y = row.get(a), row.get(b)
        if x is not None and y is not None:
            diffs.append((name, x - y))
    row["diffs"] = diffs
    row["verdict"] = _verdict(diffs)
    return row


def _verdict(diffs) -> str:
    """阈值：1Y 差 ≤50bp 算一致、≤120bp 算接近；3Y 差 ≤60bp 算一致、≤150bp 算接近。"""
    if not diffs:
        return "无可比数据"
    worst = "一致"
    for name, d in diffs:
        a = abs(d)
        tol_ok, tol_near = (0.0050, 0.0120) if name == "1Y" else (0.0060, 0.0150)
        if a > tol_near:
            return "冲突"
        if a > tol_ok:
            worst = "接近"
    return worst


def _read_otc() -> dict:
    if not os.path.exists(OTC_CSV):
        return {}
    out = {}
    with io.open(OTC_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = norm_code(r.get("code"))
            if c:
                out[c] = r
    return out


def health(csv_path: str | None = None, n: int = 5, write: bool = True) -> dict:
    """抽样健康检查：优先取池子里得分最高的 n 只，再补 2 只固定样本。"""
    path = csv_path or OTC_CSV
    rows: dict = {}
    if os.path.exists(path):
        with io.open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                c = norm_code(r.get("code"))
                if c:
                    rows[c] = r
    pool = list(rows.values())
    if pool:
        def _score(r):
            v = _f(r.get("score"))
            return v if v is not None else -1e9
        pool.sort(key=_score, reverse=True)
        picked = [r["code"] for r in pool[:max(1, int(n))]]
    else:
        picked = []
    for extra in ("018074", "012310"):
        if extra not in picked:
            picked.append(extra)
    if not picked:
        raise FundError(f"没有可检查的基金：{path} 不存在且无默认样本")

    results = [compare(c, rows.get(c)) for c in picked[: max(1, int(n)) + 2]]
    ok = [r for r in results if not r.get("error")]
    bad = [r for r in results if r.get("error")]
    conflict = [r for r in ok if r.get("verdict") == "冲突"]
    out = {
        "checked_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_csv": path,
        "n_checked": len(results),
        "n_ok": len(ok),
        "n_error": len(bad),
        "n_conflict": len(conflict),
        "rows": results,
        "ok": (len(bad) == 0 and len(conflict) == 0),
    }
    if write:
        md = brief_md(out)
        os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
        io.open(HEALTH_PATH, "w", encoding="utf-8", newline="").write(md)
        out["path"] = HEALTH_PATH
    return out


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def brief_md(res: dict) -> str:
    L = ["# 基金层健康检查", "",
         f"- 时间：{res['checked_at']}",
         f"- 样本源：`{os.path.basename(str(res['source_csv']))}`",
         f"- 样本 {res['n_checked']} 只：正常 {res['n_ok']}｜取数失败 {res['n_error']}｜口径冲突 {res['n_conflict']}",
         f"- 结论：**{'主源与备用源一致，场外池可信' if res['ok'] else '有异常，见下表'}**",
         "",
         "> 本层只**校验**，不写 `stage1_otc.csv`——池子只有一个生产者（`otc.py`）。",
         "", "| 代码 | 净值日 | 我方 1Y | 复算 1Y | 差 | 我方 3Y | 复算 3Y | 差 | 同花顺 3Y 年化 | 判定 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        if r.get("error"):
            L.append(f"| {r.get('code')} | — | — | — | — | — | — | — | — | 取数失败：{r['error']} |")
            continue
        d = dict(r.get("diffs") or [])
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r.get("code"), r.get("nav_date") or "—",
            _pct(r.get("ours_ret_1y")), _pct(r.get("calc_ret_1y")),
            "—" if "1Y" not in d else f"{d['1Y'] * 10000:+.0f}bp",
            _pct(r.get("ours_ret_3y")), _pct(r.get("calc_ret_3y")),
            "—" if "3Y" not in d else f"{d['3Y'] * 10000:+.0f}bp",
            _pct(r.get("theirs_tyear_ann")), r.get("verdict") or "—"))
    L += ["", "## 这东西怎么用", "",
          "- **全绿**：场外池照常用，不用管。",
          "- **有「接近」**：净值口径可能有细微差异（分红再投 vs 单位净值），继续用，记一笔。",
          "- **有「冲突」**：去查 `otc.py` 的净值/收益取数是否变了。**不要**直接换成同花顺口径——",
          "  换口径要重建整个池子并重跑全部下游，属于一级变更，必须人确认。",
          "- 本文件由 `python fund.py --health` 生成，不要手改。"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ CLI
def _print_row(r: dict) -> None:
    if r.get("error"):
        print(f"  {r.get('code')}  取数失败：{r['error']}")
        return
    print(f"  {r.get('code')}  净值日 {r.get('nav_date')}｜序列 {r.get('n_days')} 条")
    print(f"    1Y：我方 {_pct(r.get('ours_ret_1y'))}  复算 {_pct(r.get('calc_ret_1y'))}")
    print(f"    3Y：我方 {_pct(r.get('ours_ret_3y'))}  复算 {_pct(r.get('calc_ret_3y'))}"
          f"  同花顺年化 {_pct(r.get('theirs_tyear_ann'))}")
    print(f"    3Y 回撤：复算 {_pct(r.get('calc_dd_3y'))}｜判定：{r.get('verdict')}")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="基金层：场外基金的备用源与交叉校验")
    ap.add_argument("--code", action="append", default=[], help="基金代码，可重复")
    ap.add_argument("--health", action="store_true", help="抽样健康检查 → out/fund_health.md")
    ap.add_argument("--n", type=int, default=5, help="健康检查抽样只数（默认 5）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不写文件")
    a = ap.parse_args(argv)

    if not a.code and not a.health:
        ap.print_help()
        return 0

    if not fuyao.available():
        print("同花顺不可用（没有 key 或未启用）——基金层是备用源，主源 otc.py 不受影响。")
        return 2

    rc = 0
    ours = _read_otc()
    for c in a.code:
        r = compare(c, ours.get(norm_code(c)))
        _print_row(r)
        if r.get("verdict") == "冲突" or r.get("error"):
            rc = 1

    if a.health:
        res = health(n=a.n, write=not a.no_write)
        print(f"\n健康检查：{res['n_checked']} 只｜正常 {res['n_ok']}｜失败 {res['n_error']}"
              f"｜冲突 {res['n_conflict']}｜{'PASS' if res['ok'] else 'FAIL'}")
        for r in res["rows"]:
            _print_row(r) if r.get("verdict") != "一致" else None
        if res.get("path"):
            print(f"→ {res['path']}")
        if not res["ok"]:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
