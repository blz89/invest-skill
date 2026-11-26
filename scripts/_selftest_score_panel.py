# -*- coding: utf-8 -*-
"""自检：选股评分面板（score_panel.py）与年报「选股评分体检」接线。

跑法：python scripts/_selftest_score_panel.py     （不联网、不读写真实账本与真实面板）
"""
from __future__ import annotations

import json
import atexit
import shutil
import os
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import monthly  # noqa: E402
import report_html  # noqa: E402
import score_panel  # noqa: E402

N = [0]
BAD = []


def check(msg, ok):
    N[0] += 1
    if ok:
        print(f"  OK   {msg}")
    else:
        BAD.append(msg)
        print(f"  FAIL {msg}")


def _doc():
    sc = {"total": 72.5, "quality": 20.0, "growth": 18.0, "valuation": 17.5, "risk": 17.0,
          "quality_parts": {"ROE": 10}, "growth_parts": {"营收": 12},
          "valuation_parts": {"PE 分位": 10}, "risk_parts": {"回撤控制": 8}}
    return {"results": [
        {"code": "000001", "name": "甲", "industry": "银行", "verdict": "买入区",
         "completeness": 1.0, "veto": [], "scores": sc,
         "metrics": {"price": 10.0}, "pricing": {"buy_price": 9.0, "target_price": 13.0}},
        {"code": "600000", "name": "乙", "industry": "银行", "verdict": "观察",
         "completeness": 0.5, "veto": [{"rule": "PE<=0"}],
         "scores": {**sc, "total": 60.0}, "metrics": {"price": 8.0},
         "pricing": {"buy_price": 7.5, "target_price": 10.0}},
    ]}


print("1) 归档：stage2_detail.json → out/archive/scoring/<date>.csv")
tmp = tempfile.mkdtemp()
atexit.register(shutil.rmtree, tmp, ignore_errors=True)
_orig_out = config.OUT_DIR
config.OUT_DIR = tmp
try:
    os.makedirs(os.path.join(tmp, "archive"), exist_ok=True)
    with open(os.path.join(tmp, "stage2_detail.json"), "w", encoding="utf-8") as f:
        json.dump(_doc(), f, ensure_ascii=False)
    r1 = score_panel.archive("2026-11-20", basket=["000001"])
    check("归档成功且两条", r1["ok"] and r1["n"] == 2)
    check("写入日期文件", r1["path"].endswith("2026-11-20.csv") and os.path.isfile(r1["path"]))
    df = pd.read_csv(r1["path"], dtype={"code": str})
    check("进篮标记只给 000001", int(df.loc[df["code"] == "000001", "in_basket"].iloc[0]) == 1
          and int(df.loc[df["code"] == "600000", "in_basket"].iloc[0]) == 0)
    check("保留四维与子项 JSON", "quality_parts" in df.columns and df["quality_parts"].notna().all())
    before = open(r1["path"], encoding="utf-8-sig").read()
    r2 = score_panel.archive("2026-11-20", basket=["000001"])
    after = open(r2["path"], encoding="utf-8-sig").read()
    check("同日重复归档幂等（覆盖同内容）", before == after)

    print("2) 归档回落：没有 detail 时读 stage2_judged.csv")
    os.remove(os.path.join(tmp, "stage2_detail.json"))
    pd.DataFrame([{"code": "000002", "name": "丙", "industry": "地产", "verdict": "观察",
                   "total": 58.0, "quality": 16, "growth": 14, "valuation": 14,
                   "risk": 14, "completeness": 0.9, "veto": "", "price": 5.0,
                   "buy_price": 4.6, "target_price": 6.5}]).to_csv(
        os.path.join(tmp, "stage2_judged.csv"), index=False, encoding="utf-8-sig")
    r3 = score_panel.archive("2026-11-21", basket=[])
    check("回落 judged 成功", r3["ok"] and r3["n"] == 1)
finally:
    config.OUT_DIR = _orig_out

print("3) load_panel：多日合并去重")
d = tempfile.mkdtemp()
atexit.register(shutil.rmtree, d, ignore_errors=True)
try:
    for day, codes in (("2026-01-02", ["000001", "600000"]), ("2026-01-09", ["000001"])):
        pd.DataFrame([{"date": day, "code": c, "name": c, "verdict": "观察", "total": 60,
                       "quality": 15, "growth": 15, "valuation": 15, "risk": 15,
                       "completeness": 1.0, "veto": "", "price": 10, "buy_price": 9,
                       "target_price": 12, "in_basket": 0} for c in codes]).to_csv(
            os.path.join(d, f"{day}.csv"), index=False, encoding="utf-8-sig")
    panel = score_panel.load_panel(d)
    check("合并 3 行（同日同代码去重）", len(panel) == 3)
    check("日期排序", list(panel["date"]) == ["2026-01-02", "2026-01-02", "2026-01-09"])
finally:
    pass

print("4) add_forward：前向超额收益")
dates = [f"2026-{m:02d}-{d:02d}" for m, d in ((1, 2), (2, 2), (3, 2))]
bmap = {dates[0]: 100.0, dates[1]: 110.0, dates[2]: 121.0}
pmap = {"000001": {dates[0]: 10.0, dates[1]: 12.0, dates[2]: 11.0}}
p = pd.DataFrame([{"date": dates[0], "code": "000001", "total": 70.0}]
                 + [{"date": dates[0], "code": "600000", "total": 50.0}])
out = score_panel.add_forward(p, horizons={"1m": 1},
                              price_lookup=lambda c: pmap.get(c, {}),
                              bench_lookup=lambda: bmap)
v = out.loc[out["code"] == "000001", "ex_1m"].iloc[0]
check("1m 超额 = 股票收益 − 基准收益", abs(v - (0.2 - 0.1)) < 1e-9)
check("无价格代码为 NaN", pd.isna(out.loc[out["code"] == "600000", "ex_1m"].iloc[0]))

print("5) metrics / health：IC、分组差与样本门槛")
rows = []
for i in range(40):
    rows.append({"date": f"2026-{1 + i // 20:02d}-{i % 20 + 1:02d}", "code": f"{i:06d}",
                 "name": f"x{i}", "verdict": "买入区" if i >= 30 else ("观察" if i >= 10 else "回避"),
                 "total": float(i), "quality": float(i), "growth": 50.0,
                 "valuation": 50.0, "risk": 50.0, "completeness": 1.0, "veto": "",
                 "price": 10, "buy_price": 9, "target_price": 12, "in_basket": 0,
                 "ex_3m": i / 100.0})
pn = pd.DataFrame(rows)
m = score_panel.metrics(pn, horizons=("3m",))
h = (m.get("horizons") or {}).get("3m") or {}
check("total 维度 IC 接近 1", (h.get("ic") or {}).get("total") is not None
      and h["ic"]["total"] > 0.99)
check("五分组差为正", (h.get("quintile_spread") or 0) > 0)
check("命中率给出买入区", "buy" in (h.get("hit") or {}))
check("样本不足：5 周 → 维持现状",
      "样本不足" in score_panel.health(2026, panel=pn.head(5).assign(date="2026-01-02"))["conclusion"])
h2 = score_panel.health(2026, panel=pn)
check("40 条观测 → 可评估", h2["ok"] is True and "可参考" in h2["conclusion"])
check("体检 md 含标题与 IC", "选股评分体检" in h2["md"] and "IC" in h2["md"])

print("6) 年报接线：md 含「六、选股评分体检」，记分卡后移")
avd = {"ledgers": {}, "scorecard": "sc", "retro": {},
       "viability": "## 结论\n- 微调执行层", "score_health": "## 选股评分体检\n\n- 面板：3 周（样本不足，维持现状）",
       "settlement": {}}
amd = monthly.to_md_annual(2026, avd, "sc", "2026-01-01", "2026-12-31")
check("年报 md 含选股评分体检", "## 六、选股评分体检" in amd and "维持现状" in amd)
check("年报 md 记分卡改为七", "## 七、记分卡" in amd)
ah = report_html.build_annual_report(year=2026, data=avd)
check("年报 HTML 含选股评分体检", "选股评分体检" in ah and "维持现状" in ah)

print("7) 接线与无副作用")
_w = open(os.path.join(ROOT, "weekly.py"), encoding="utf-8").read()
_m = open(os.path.join(ROOT, "monthly.py"), encoding="utf-8").read()
check("weekly 归档且失败不阻断", "score_panel.archive(" in _w and "评分面板归档失败" in _w)
check("monthly 年报带 score_health", '"score_health"' in _m and "score_health_md" in _m)

print("=" * 78)
if BAD:
    print(f"FAIL 共 {len(BAD)} 项失败 / {N[0]} 项")
    for b in BAD:
        print("  - " + b)
    sys.exit(1)
print(f"OK 全部通过（{N[0]} 项断言）")
