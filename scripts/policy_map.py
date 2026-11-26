#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""政策面：可证伪的「政策 → 行业」映射 + 前向跟踪（只记录，不参与打分）。

为什么要有它
------------
政策是当前唯一「有信息、但没有可验证信号」的面：`news.py` 只把政策当背景，
因为它既没有历史快照、也没定过什么算「命中」。本脚本把这件事变成可证伪的：

规则（写死、可复核）
--------------------
1) 映射：政策标题/标签命中 `RULES` 关键词 → 映射到东财（同花顺）一级行业；
   一条政策可映射多个行业，取并集；一个都不命中 → 不跟踪（不硬凑）。
2) 预测：政策发布后 **20 个交易日**（HORIZON），映射行业的「等权组合」
   （`out/industry_map.csv` 里该行业、且有行情的个股等权）相对沪深300 的超额收益
   **> 0** 记「命中」，否则「未命中」。
3) 护栏：某行业可用成员 < 3 只 → 该行业标「成员不足」不判命中；
   所有映射行业都不足 → 整条政策标「样本不足」。多行业取算术平均。
4) 前向性：`policy_events.csv` 没有历史全量，所以不做「历史回放」假装回测；
   事件发生时写预测进 `out/policy_track.json`（status=pending），到期后 `evaluate`
   才算命中。**只记录，不改变事件面 R/C 打分、不改仓位。**

用法
----
    python scripts/policy_map.py                 # 建/更新跟踪表 + 评估到期条目
    python scripts/policy_map.py --json          # 机器可读
    python scripts/policy_map.py --no-eval       # 只建表（决策日用）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: F401  （装 datasource 的 cache hook）

OUT = config.OUT_DIR
EVENTS = os.path.join(OUT, "policy_events.csv")
TRACK = os.path.join(OUT, "policy_track.json")
EVAL_JSON = os.path.join(OUT, "policy_eval.json")
EVAL_MD = os.path.join(OUT, "policy_eval.md")
HORIZON = 20
MIN_MEMBERS = 3

# 关键词 → 行业（行业名与 out/industry_map.csv 的 industry 列同口径；
# 新增一条必须有明确映射依据，宁缺毋滥——映射不准会让「命中率」变成噪声）
RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("降准", "降息", "逆回购", "流动性", "信贷", "社融", "MLF", "LPR", "再贷款"),
     ("银行",)),
    (("险资", "保险资金", "保费", "保险机构"), ("保险",)),
    (("券商", "资本市场", "注册制", "印花税", "并购重组", "中长期资金入市"), ("证券",)),
    (("地产", "房地产", "保交楼", "房贷", "限购", "商品房", "城中村"), ("房地产",)),
    (("基建", "专项债", "重大项目", "城市更新", "水利", "以工代赈"),
     ("建筑装饰", "建筑材料")),
    (("光伏", "风电", "储能", "特高压", "充电桩", "新型电力系统"),
     ("光伏设备", "风电设备", "电网设备", "电池", "其他电源设备")),
    (("半导体", "芯片", "集成电路", "光刻", "存储器", "晶圆"), ("半导体",)),
    (("军工", "国防", "航空发动机", "商业航天", "低空经济"), ("军工电子", "军工装备")),
    (("创新药", "医保", "集采", "药品", "医疗器械", "生物医药"),
     ("化学制药", "生物制品", "医疗器械", "医疗服务", "医药商业")),
    (("白酒", "食品饮料", "促消费", "内需", "以旧换新"), ("白酒", "饮料制造", "食品加工制造")),
    (("家电", "白色家电"), ("白色家电",)),
    (("购置税", "汽车", "新能源车"), ("汽车整车", "汽车零部件")),
    (("煤炭", "电力保供", "电价"), ("煤炭开采加工", "电力")),
    (("人工智能", "算力", "数据要素", "数字经济", "大模型", "AI"),
     ("计算机设备", "软件开发", "IT服务", "通信设备")),
    (("游戏", "影视", "版权", "传媒"), ("游戏", "影视院线", "文化传媒")),
    (("种业", "粮食安全", "生猪", "农业"), ("养殖业", "农产品加工", "农化制品")),
    (("黄金", "稀土", "铜", "有色金属"), ("贵金属", "工业金属", "小金属", "能源金属")),
    (("石油", "天然气", "油价"), ("石油加工贸易", "油气开采及服务")),
    (("航运", "港口", "物流", "铁路", "民航", "机场"),
     ("港口航运", "物流", "公路铁路运输", "机场航运", "交通运输")),
]


# ------------------------------------------------------------------ 纯函数
def map_industries(title: str, tag: str = "") -> list[str]:
    """政策文本 → 行业列表（保持 RULES 顺序，去重）。"""
    text = f"{title or ''} {tag or ''}"
    out: list[str] = []
    for kws, inds in RULES:
        if any(k in text for k in kws):
            for ind in inds:
                if ind not in out:
                    out.append(ind)
    return out


def _norm_day(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit() and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def load_events(path: str = EVENTS) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            day = _norm_day(r.get("date"))
            title = str(r.get("title") or "").strip()
            if not day or not title:
                continue
            inds = map_industries(title, str(r.get("tag") or ""))
            rows.append({"date": day, "title": title[:160], "kind": r.get("kind") or "政策",
                         "tag": r.get("tag") or "", "level": r.get("level") or "中",
                         "source": r.get("source") or "", "industries": inds})
    return rows


def entry_id(date: str, title: str) -> str:
    return hashlib.sha1(f"{date}|{title}".encode("utf-8")).hexdigest()[:12]


def build_track(events: list[dict], existing: dict | None = None,
                horizon: int = HORIZON, today: str | None = None) -> dict:
    """把新政策事件并进跟踪表（已存在的不重置；未映射到行业的不跟踪，但记 skipped）。"""
    old = (existing or {}).get("entries") or []
    by_id = {e.get("id"): e for e in old}
    skipped = 0
    for ev in events:
        if not ev.get("industries"):
            skipped += 1
            continue
        eid = entry_id(ev["date"], ev["title"])
        if eid in by_id:
            continue
        by_id[eid] = {
            "id": eid, "date": ev["date"], "title": ev["title"],
            "tag": ev.get("tag") or "", "industries": ev["industries"],
            "horizon": int(horizon), "status": "pending",
            "created_at": today or dt.date.today().isoformat(), "result": None,
        }
    return {"generated_at": today or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": int(horizon), "min_members": MIN_MEMBERS,
            "skipped_unmapped": skipped,
            "entries": sorted(by_id.values(), key=lambda x: (x.get("date") or "", x.get("id") or ""), reverse=True)}


def _first_td_on_or_after(days: list[str], day: str) -> str | None:
    later = [d for d in days if d >= day]
    return later[0] if later else None


def evaluate_entries(entries: list[dict], bench: dict[str, float],
                     px_getter, members: dict[str, list[str]],
                     horizon: int = HORIZON, min_members: int = MIN_MEMBERS,
                     today: str | None = None) -> tuple[list[dict], dict]:
    """给到期条目算命中；未到期保持 pending。px_getter(code) -> {date: close}。"""
    days = sorted(bench.keys())
    done_n = hit_n = pending_n = 0
    for e in entries:
        if e.get("status") == "done":
            r = e.get("result") or {}
            done_n += 1
            hit_n += 1 if r.get("hit") else 0
            continue
        d0 = _first_td_on_or_after(days, e.get("date") or "")
        if not d0 or d0 not in bench:
            pending_n += 1
            continue
        i = days.index(d0)
        j = i + int(e.get("horizon") or horizon)
        if j >= len(days):
            pending_n += 1
            continue
        d1 = days[j]
        b = bench[d1] / bench[d0] - 1.0
        per: dict[str, float] = {}
        short: list[str] = []
        for ind in e.get("industries") or []:
            codes = [c for c in members.get(ind) or []]
            rets = []
            for c in codes:
                px = px_getter(c) or {}
                if d0 in px and d1 in px:
                    rets.append(px[d1] / px[d0] - 1.0)
            if len(rets) < min_members:
                short.append(ind)
                continue
            per[ind] = st.mean(rets) - b
        if not per:
            e["status"] = "sample_short"
            e["result"] = {"start": d0, "end": d1, "short": short, "hit": None}
            pending_n += 1
            continue
        overall = st.mean(per.values())
        e["status"] = "done"
        e["result"] = {"start": d0, "end": d1, "by_industry": {k: round(v, 6) for k, v in per.items()},
                       "short": short, "excess": round(overall, 6), "hit": overall > 0}
        done_n += 1
        hit_n += 1 if overall > 0 else 0
    summary = {"done": done_n, "hit": hit_n, "pending": pending_n,
               "hit_rate": (hit_n / done_n) if done_n else None,
               "horizon": horizon, "min_members": min_members,
               "sample_insufficient": done_n < 5}
    return entries, summary


def render_md(track: dict, summary: dict) -> str:
    entries = track.get("entries") or []
    L = [f'# 政策面跟踪（生成 {track.get("generated_at") or "-"}）', "",
         f'口径：政策发布后 {HORIZON} 个交易日，映射行业等权组合相对沪深300 超额 >0 记命中；'
         f'成员 < {MIN_MEMBERS} 只不判。前向记录，不是历史回测。', ""]
    line = (f'- 跟踪条目 {len(entries)} 条｜已到期 {summary["done"]}'
            f'｜命中 {summary["hit"]}｜未到期 {summary["pending"]}')
    if summary.get("hit_rate") is not None:
        line += "｜命中率 {:.0%}".format(summary["hit_rate"])
    if summary.get("sample_insufficient"):
        line += "｜样本不足，不用于改规则"
    L += [line, ""]
    if not entries:
        L.append("- 暂无政策事件（policy_events.csv 为空或都未映射到行业）。")
    else:
        L += ["| 日期 | 政策 | 映射行业 | 状态 | 结果 |", "|---|---|---|---|---|"]
        for e in entries[:30]:
            r = e.get("result") or {}
            res = "—"
            if e.get("status") == "done":
                res = f'超额 {r.get("excess", 0):+.2%}｜{"命中" if r.get("hit") else "未命中"}'
            elif e.get("status") == "sample_short":
                res = "成员不足"
            elif e.get("status") == "pending":
                res = "未到期"
            L.append(f'| {e.get("date")} | {str(e.get("title"))[:40]} | '
                     f'{"、".join(e.get("industries") or [])} | {e.get("status")} | {res} |')
    L += ["", "边界：模拟盘记录，不构成投资建议，不承诺收益。"]
    return "\n".join(L)


def section_md(path: str | None = None) -> list[str]:
    """月报用：几行摘要（文件不存在时不抛异常）。"""
    p = path or EVAL_JSON
    if not os.path.exists(p):
        return ["- 政策面跟踪：尚无记录（决策日先跑 `python scripts/policy_map.py --no-eval`）。"]
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001
        return ["- 政策面跟踪：文件读取失败。"]
    s = doc.get("summary") or {}
    line = (f'- 政策面跟踪：条目 {len(doc.get("entries") or [])}｜已到期 {s.get("done", 0)}'
            f'｜命中 {s.get("hit", 0)}')
    if s.get("hit_rate") is not None:
        line += f'｜命中率 {s["hit_rate"]:.0%}'
    if s.get("sample_insufficient"):
        line += "｜样本不足（不下结论）"
    return [line]


# ------------------------------------------------------------------ 取数/落盘
def _bench() -> dict[str, float]:
    try:
        import datasource as ds
        df = ds.fetch_index_daily("000300")
        return {_norm_day(r.get("date")): float(r.get("close"))
                for r in df.to_dict("records") if r.get("close") is not None}
    except Exception as e:  # noqa: BLE001
        print(f"  ! 基准取不到：{e}")
        return {}


def _price_cache():
    cache: dict[str, dict] = {}

    def get(code: str) -> dict:
        if code not in cache:
            try:
                import datasource as ds
                df = ds.fetch_daily(code)
                cache[code] = {_norm_day(r.get("date")): float(r.get("close"))
                               for r in df.to_dict("records")
                               if r.get("close") is not None}
            except Exception:  # noqa: BLE001
                cache[code] = {}
        return cache[code]
    return get


def _members() -> dict[str, list[str]]:
    import industry
    out: dict[str, list[str]] = {}
    for code, row in industry.load_map().items():
        ind = (row.get("industry") or "").strip()
        if ind:
            out.setdefault(ind, []).append(code)
    return out


def run(no_eval: bool = False, events_path: str = EVENTS,
        track_path: str = TRACK, write: bool = True) -> dict:
    events = load_events(events_path)
    existing = None
    if os.path.exists(track_path):
        try:
            with open(track_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001
            existing = None
    track = build_track(events, existing)
    summary = {"done": 0, "hit": 0, "pending": 0, "hit_rate": None,
               "horizon": HORIZON, "min_members": MIN_MEMBERS, "sample_insufficient": True}
    if not no_eval:
        bench = _bench()
        if bench:
            members = _members()
            track["entries"], summary = evaluate_entries(
                track["entries"], bench, _price_cache(), members)
    doc = {"generated_at": track["generated_at"], "entries": track["entries"],
           "summary": summary, "rules": [{"keywords": list(k), "industries": list(v)}
                                         for k, v in RULES]}
    if write:
        os.makedirs(OUT, exist_ok=True)
        with open(track_path, "w", encoding="utf-8") as f:
            json.dump(track, f, ensure_ascii=False, indent=1)
        with open(EVAL_JSON, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        with open(EVAL_MD, "w", encoding="utf-8") as f:
            f.write(render_md(track, summary))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="政策面映射与前向跟踪")
    ap.add_argument("--no-eval", action="store_true", help="只建/更新跟踪表，不评估")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    doc = run(no_eval=a.no_eval)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    s = doc["summary"]
    print(f'[policy_map] 条目 {len(doc["entries"])}｜已到期 {s["done"]}｜命中 {s["hit"]}'
          f'｜未到期 {s["pending"]}')
    for e in doc["entries"][:10]:
        r = e.get("result") or {}
        tag = ("命中" if r.get("hit") else "未命中") if e.get("status") == "done" else e.get("status")
        print(f'  {e["date"]} {e["title"][:40]}｜{"、".join(e["industries"])}｜{tag}')
    print(f'[policy_map] 明细 {EVAL_MD}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
