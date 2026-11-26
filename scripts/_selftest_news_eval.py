#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`news_eval.py` / `policy_map.py` 的纯函数自测（不联网、不写 out/）。

覆盖：
- 事件方向、交易日对齐、超额收益、到期/进行中判定
- 分档聚合（命中率/样本不足/进行中）
- 政策→行业映射、跟踪表去重与保留、到期命中计算、成员不足护栏
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import news_eval
import policy_map

BAD: list[str] = []
N = [0]


def check(name: str, cond: bool) -> None:
    N[0] += 1
    if not cond:
        BAD.append(name)


def eq(name: str, got, want) -> None:
    check(f"{name}（期望 {want!r}，实得 {got!r}）", got == want)


def close(name: str, got: float, want: float, tol: float = 1e-9) -> None:
    check(f"{name}（期望 {want}±{tol}，实得 {got}）", abs(got - want) <= tol)


DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def test_news_eval() -> None:
    # 方向
    eq("方向 降权", news_eval.direction_of("降权15", 0), -1)
    eq("方向 加分", news_eval.direction_of("加分10", 0), 1)
    eq("方向 不干预", news_eval.direction_of("不干预", 0), 0)
    eq("方向 无档位但净分为负", news_eval.direction_of("", -30), -1)
    eq("方向 无档位但净分为正", news_eval.direction_of("", 18), 1)

    # 事件日不是交易日 → 顺延到之后第一个交易日
    bench = {d: 1000.0 for d in DAYS}
    px = {d: 10.0 for d in DAYS}
    r = news_eval.excess(px, bench, DAYS, "2026-01-03", 2)   # 周六 → 01-05
    eq("非交易日顺延起点", r and r["start"], "2026-01-05")
    eq("非交易日顺延后成熟", r and r["matured"], True)

    # 超额 = 个股涨幅 − 基准涨幅
    px2 = dict(px)
    px2["2026-01-07"] = 11.0       # +10%
    bench2 = dict(bench)
    bench2["2026-01-07"] = 1020.0  # +2%
    r = news_eval.excess(px2, bench2, DAYS, "2026-01-05", 2)
    close("超额 10%-2%", r["excess"], 0.08)

    # 不够期 → 进行中（matured=False），且只能用最近交易日
    r = news_eval.excess(px, bench, DAYS, "2026-01-08", 5)
    eq("不足期未成熟", r["matured"], False)
    eq("不足期终点=最后交易日", r["end"], DAYS[-1])

    # 分档聚合：命中率按方向判
    ev = [
        {"code": "000001", "name": "A", "date": "2026-01-05", "tier": "降权15",
         "tag": "减持", "level": "高", "net_score": -30, "direction": -1},
        {"code": "000002", "name": "B", "date": "2026-01-05", "tier": "降权15",
         "tag": "减持", "level": "高", "net_score": -30, "direction": -1},
    ]
    prices = {"000001": {**px, "2026-01-07": 9.0}, "000002": {**px, "2026-01-07": 10.5}}
    rows = news_eval.evaluate(ev, prices, bench, horizons=(2,))
    eq("evaluate 行数", len(rows), 2)
    agg = news_eval.aggregate(rows)
    b = agg["降权15|2"]
    eq("降权命中 n", b["stats"]["n"], 2)
    close("降权命中率 1/2", b["stats"]["hit_rate"], 0.5)
    eq("降权样本不足标记", b["insufficient"], True)   # n=2 < 10

    # 进行中与到期分开
    ev2 = [{**ev[0], "date": "2026-01-08"}]
    rows2 = news_eval.evaluate(ev2, prices, bench, horizons=(5,))
    agg2 = news_eval.aggregate(rows2)["降权15|5"]
    eq("到期 n=0", agg2["stats"]["n"], 0)
    eq("进行中 n=1", agg2["running_stats"]["n"], 1)

    # 报告
    md = news_eval.render_md(rows, "2026-01-10")
    check("事件报告含口径", "口径" in md)
    check("事件报告含样本不足", "样本不足" in md)
    check("事件报告含边界声明", "不构成投资建议" in md)

    # 月报摘要：没文件 → 提示；有文件 → 输出命中率
    with tempfile.TemporaryDirectory() as td:
        missing = os.path.join(td, "nope.json")
        check("news section 缺文件有提示", "尚无评估结果" in news_eval.section_md(missing)[0])
        p = os.path.join(td, "news_eval.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"asof": "2026-01-10",
                       "summary": [{"tier": "降权15", "horizon": 20,
                                    "stats": {"n": 12, "hit_rate": 0.75, "mean": -0.02},
                                    "insufficient": False}]}, f)
        line = "\n".join(news_eval.section_md(p))
        check("news section 含命中率", "75%" in line)
        check("news section 含样本数", "n=12" in line)


def test_policy_map() -> None:
    eq("政策映射 降准→银行", policy_map.map_industries("央行宣布降准0.25个百分点"), ["银行"])
    eq("政策映射 险资+半导体", policy_map.map_industries("险资加速调研半导体公司"),
       ["保险", "半导体"])
    eq("政策映射 无关键词", policy_map.map_industries("某公司发布年度报告"), [])
    eq("政策映射 去重", policy_map.map_industries("降息与LPR下调"), ["银行"])

    eid = policy_map.entry_id("2026-01-05", "央行降准")
    eq("跟踪 id 稳定", eid, policy_map.entry_id("2026-01-05", "央行降准"))
    check("跟踪 id 随标题变化", eid != policy_map.entry_id("2026-01-05", "央行降息"))

    ev = [
        {"date": "2026-01-05", "title": "央行宣布降准", "tag": "降准", "industries": ["银行"]},
        {"date": "2026-01-06", "title": "公司发布年报", "tag": "", "industries": []},
    ]
    tr = policy_map.build_track(ev, existing=None, horizon=2, today="2026-01-07")
    eq("建表跳过未映射", tr["skipped_unmapped"], 1)
    eq("建表条目数", len(tr["entries"]), 1)
    # 重复建表不重复
    tr2 = policy_map.build_track(ev, existing=tr, horizon=2, today="2026-01-07")
    eq("重复建表幂等", len(tr2["entries"]), 1)

    # 到期命中计算：银行组合 +4%，基准 +2% → 超额 +2% → 命中
    bench = {d: 1000.0 * (1 + 0.005 * i) for i, d in enumerate(DAYS)}   # 每天 +0.5%
    bench[DAYS[2]] = 1020.0
    px = {c: {d: 10.0 * (1 + 0.02 * i) for i, d in enumerate(DAYS)}
          for c in ("000001", "000002", "000003")}
    members = {"银行": ["000001", "000002", "000003"]}
    entries = [{"id": "x", "date": DAYS[0], "title": "降准", "industries": ["银行"],
                "horizon": 2, "status": "pending"}]
    out, summary = policy_map.evaluate_entries(entries, bench, lambda c: px[c],
                                               members, horizon=2)
    eq("政策到期 status", out[0]["status"], "done")
    eq("政策命中", out[0]["result"]["hit"], True)
    check("政策超额>0", out[0]["result"]["excess"] > 0)
    eq("政策汇总 done", summary["done"], 1)

    # 成员不足护栏
    entries2 = [{"id": "y", "date": DAYS[0], "title": "降准", "industries": ["银行"],
                 "horizon": 2, "status": "pending"}]
    out2, _ = policy_map.evaluate_entries(entries2, bench, lambda c: px[c],
                                          {"银行": ["000001"]}, horizon=2, min_members=3)
    eq("政策成员不足", out2[0]["status"], "sample_short")

    # 未到期保持 pending
    entries3 = [{"id": "z", "date": DAYS[-1], "title": "降准", "industries": ["银行"],
                 "horizon": 2, "status": "pending"}]
    out3, s3 = policy_map.evaluate_entries(entries3, bench, lambda c: px[c],
                                           members, horizon=2)
    eq("政策未到期 pending", out3[0]["status"], "pending")
    eq("政策未到期计数", s3["pending"], 1)

    md = policy_map.render_md(tr, {"done": 0, "hit": 0, "pending": 1,
                                   "hit_rate": None, "sample_insufficient": True})
    check("政策报告含口径", "口径" in md)
    check("政策报告声明非回测", "不是历史回测" in md)

    with tempfile.TemporaryDirectory() as td:
        check("policy section 缺文件有提示",
              "尚无记录" in policy_map.section_md(os.path.join(td, "nope.json"))[0])
        p = os.path.join(td, "policy_eval.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"entries": [{"id": "a"}],
                       "summary": {"done": 1, "hit": 1, "hit_rate": 1.0,
                                   "sample_insufficient": True}}, f)
        line = policy_map.section_md(p)[0]
        check("policy section 含命中率", "100%" in line)
        check("policy section 提示样本不足", "样本不足" in line)


def main() -> int:
    test_news_eval()
    test_policy_map()
    if BAD:
        print(f"FAIL: {len(BAD)}/{N[0]} 项失败")
        for b in BAD:
            print("  ✗ " + b)
        return 1
    print(f"OK 全部通过（{N[0]} 项断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
