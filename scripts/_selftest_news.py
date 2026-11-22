#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`scripts/news.py` 与事件面配置的自测。纯离线，不联网。

跑法：python scripts/_selftest_news.py
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import news

_ok = 0
_bad: list[str] = []


def check(label: str, cond, extra: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
    else:
        _bad.append(f"{label}{('  ' + extra) if extra else ''}")
        print(f"  ✗ {label} {extra}")


def sec(t: str) -> None:
    print(f"\n=== {t} ===")


# ---------------------------------------------------------------- A 级别判定
def test_level() -> None:
    sec("A 级别判定与噪音门")
    cases = [
        ("关于持股5%以上股东减持股份的预披露公告", "高", "减持"),
        ("公司收到中国证监会立案告知书", "高", "立案"),
        ("关于计提商誉减值准备的公告", "高", "商誉减值"),
        ("2026年半年度业绩预亏公告", "高", "预亏"),
        ("关于股东部分股份解除质押的公告", "低", ""),    # 解除质押是利好，不当风险
        ("关于股东所持股份质押的公告", "中", "质押"),   # 2026-11-21 降档：只记一笔，不降权
        ("关于公司股票评级下调的风险提示", "高", "评级下调"),  # 硬词不受噪音门降级
        ("关于中标重大合同的公告", "中", "中标"),
        ("关于回购公司股份的进展公告", "中", "回购"),
        ("股权激励计划首次授予", "中", "股权激励"),
        ("张坤、杨宗昌、杨锐文调仓动向曝光", "低", ""),
        ("公募顶流三季度调仓：刘彦春减持贵州茅台", "低", ""),
        ("机构评级|华泰证券给予某公司增持评级", "低", ""),
        ("JPMorgan Chase & Co.减持潍柴动力约104.28万股", "高", "减持"),
        ("公司发布日常经营公告", "低", ""),
        ("", "低", ""),
    ]
    for text, want_lv, want_kw in cases:
        lv, kw = news.level_of(text)
        check(f'level_of({text[:16]!r}) -> {want_lv}', lv == want_lv, f"实际 {lv}/{kw}")
        if want_kw:
            check(f'  kw({text[:12]!r}) 含 {want_kw}', kw == want_kw, f"实际 {kw}")
    check("is_noise(研报点评)=True", news.is_noise("机构研报点评：维持买入"))
    check("is_noise(关于回购股份的公告)=False", not news.is_noise("关于回购股份的公告"))
    # 高优先级必须先于中
    check("高优先于中（同句含减持+回购）", news.level_of("回购并减持")[0] == "高")


# ---------------------------------------------------------------- B 窗口
def test_window() -> None:
    sec("B 时间窗口")
    check("窗口下界含（等于 cutoff 算命中）", news.in_window("2026-09-29", "2026-09-29"))
    check("窗口外（早于 cutoff 不算）", not news.in_window("2026-09-01", "2026-09-29"))
    check("空日期不算", not news.in_window("", "2026-09-29"))
    check("带时间戳自动截取日期", news.in_window("2026-10-27T08:00:00", "2026-09-29"))
    check("非法日期不算", not news.in_window("not-a-date", "2026-09-29"))


# ---------------------------------------------------------------- C 排序
def test_sort() -> None:
    sec("C 排序可复现")
    ev = [
        {"level": "低", "date": "2026-10-27", "code": "000001", "title": "b"},
        {"level": "高", "date": "2026-10-01", "code": "000002", "title": "a"},
        {"level": "中", "date": "2026-10-27", "code": "000003", "title": "c"},
        {"level": "中", "date": "2026-10-28", "code": "000004", "title": "d"},
    ]
    s = news.sort_events(ev)
    check("高在最前", s[0]["level"] == "高")
    check("低在最后", s[-1]["level"] == "低")
    check("同级日期新的在前", s[1]["date"] == "2026-10-28" and s[2]["date"] == "2026-10-27")
    check("排序不改输入长度", len(s) == len(ev))
    check("两次排序结果一致", [x["title"] for x in news.sort_events(ev)] == [x["title"] for x in s])
    check("无日期排在同级最后", news.sort_events(
        [{"level": "中", "date": "", "code": "1", "title": ""},
         {"level": "中", "date": "2026-10-01", "code": "2", "title": ""}])[-1]["code"] == "1")


# ---------------------------------------------------------------- D 简报
def test_brief() -> None:
    sec("D 简报渲染")
    ev = [
        {"layer": "持仓", "code": "000999", "name": "华润三九", "kind": "新闻", "level": "中",
         "tag": "回购", "date": "2026-10-26", "title": "回购进展公告", "source": "同花顺iNews",
         "url": "https://example.org/a"},
        {"layer": "候选", "code": "000338", "name": "潍柴动力", "kind": "新闻", "level": "高",
         "tag": "减持", "date": "2026-10-27", "title": "摩根减持潍柴", "source": "智通财经",
         "url": "https://example.org/b"},
    ]
    held = [("000999", "华润三九"), ("002648", "卫星化学")]
    a = news.render_brief(ev, "2026-09-29", 20, {"000999": ["中药"]}, [], held)
    b = news.render_brief(ev, "2026-09-29", 20, {"000999": ["中药"]}, [], held)
    check("同输入两次逐字一致", a == b)
    check("含高优先级段落", "## 高（可能改变结论" in a)
    check("含中优先级段落", "## 中（记一笔" in a)
    check("高事件带⚠️", "⚠️" in a and "000338 潍柴动力" in a)
    check("列出无事件的持仓（卫星化学）", "卫星化学" in a and "窗口内无事件" in a)
    check("持仓与候选都在（华润三九）", "华润三九" in a)
    check("不含墙钟时间（无『生成于』）", "生成于" not in a and "今天" not in a)
    check("行业归属段存在", "## 行业归属" in a)
    check("空事件也能渲染", bool(news.render_brief([], "2026-09-29", 0, {}, [], [])))
    check("空事件仍列出持仓", "000999" in news.render_brief([], "2026-09-29", 0, {}, [], held))
    check("汇总行含扫描只数", "扫描 20 只" in a)


# ---------------------------------------------------------------- E 写盘契约
def test_write() -> None:
    sec("E 写盘契约")
    ev = [{"layer": "持仓", "code": "000999", "name": "华润三九", "kind": "新闻",
           "level": "高", "tag": "减持", "date": "2026-10-27", "title": "减持公告",
           "source": "同花顺iNews", "url": "https://example.org/x"}]
    old_out = config.OUT_DIR
    tmp = tempfile.mkdtemp(prefix="news_selftest_")
    try:
        config.OUT_DIR = tmp
        res = {"events": ev, "scanned": 1, "skipped": [], "cutoff": "2026-09-29",
               "industries": {}, "held": [("000999", "华润三九")]}
        paths = news.write_outputs(res)
        check("产出 csv 存在", os.path.exists(paths["csv"]))
        check("产出 md 存在", os.path.exists(paths["md"]))
        with io.open(paths["csv"], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        check("csv 回读 1 行", len(rows) == 1)
        check("csv 列与 COLS 一致", list(rows[0].keys()) == news.COLS)
        check("csv layer 规范", rows[0]["layer"] in ("持仓", "候选"))
        check("csv level 规范", rows[0]["level"] in ("高", "中", "低"))
        check("csv 空值写成空串而非 None", "None" not in "".join(rows[0].values()))
    finally:
        config.OUT_DIR = old_out


# ---------------------------------------------------------------- F 配置
def test_config() -> None:
    sec("F 事件面配置 / 计费时段")
    for k in ("enabled", "days", "news_size", "max_codes", "high_keywords",
              "mid_keywords", "out_csv", "out_md"):
        check(f"NEWS 有键 {k}", k in news.NEWS)
    hi = set(config.NEWS.get("high_keywords") or [])
    mid = set(config.NEWS.get("mid_keywords") or [])
    check("高/中关键词非空", bool(hi) and bool(mid))
    check("高/中关键词无交集", not (hi & mid))
    check("is_offpeak 工作日 11:00 = 高峰(False)",
          not config.is_offpeak(dt.datetime(2026, 10, 29, 11, 0)))
    check("is_offpeak 工作日 12:30 = 空闲(True)",
          config.is_offpeak(dt.datetime(2026, 10, 29, 12, 30)))
    check("is_offpeak 工作日 00:05 = 空闲(True)",
          config.is_offpeak(dt.datetime(2026, 10, 29, 0, 5)))
    check("is_offpeak 周六 22:00 = 空闲(True)",
          config.is_offpeak(dt.datetime(2026, 10, 31, 22, 0)))
    check("is_offpeak 工作日 14:30 = 高峰(False)",
          not config.is_offpeak(dt.datetime(2026, 10, 29, 14, 30)))
    check("peak_windows 两个区间", len(config.OFFPEAK.get("peak_windows") or []) == 2)


# ---------------------------------------------------------------- G 集成
def test_integration() -> None:
    sec("G 集成（离线，不联网）")
    held = news._plan_held()
    check("_plan_held 返回 (code,name) 元组", all(isinstance(x, tuple) and len(x) == 2 for x in held))
    check("_plan_held 代码为 6 位数字", all(str(c).isdigit() and len(str(c)) == 6 for c, _ in held))
    res = news.collect(offline=True)
    check("offline 不抛异常", isinstance(res, dict))
    check("offline 事件为空", res["events"] == [])
    check("offline 记录 skipped", len(res["skipped"]) >= 0)
    check("offline 带 cutoff", bool(res["cutoff"]))
    check("collect 返回 held 键", "held" in res)


def _ev(tag, level="高", source="公告", date="2026-10-28", title=""):
    return {"tag": tag, "level": level, "source": source, "date": date,
            "title": title or tag, "kind": "公告"}


# ---------------------------------------------------------------- H 双向评分
def test_score() -> None:
    sec("H 双向评分 R/C（口径见 references/news-rules.md）")
    asof = "2026-10-29"
    for tag in ("立案调查", "退市风险", "业绩预亏", "会计问题", "诉讼仲裁"):
        s = news.score_event(_ev(tag), asof)
        check(f"{tag} → 否决", s["tier"] == "否决" and s["mult"] == 0.0,
              f'{s["r_score"]}/{s["tier"]}')
        check(f"{tag} R≥58", s["r_score"] >= 58, str(s["r_score"]))
    check("减持 → 降权15", news.score_event(_ev("减持", "中"), asof)["tier"] == "降权15")
    check("商誉减值 → 降权40", news.score_event(_ev("商誉减值"), asof)["tier"] == "降权40")
    for tag in ("担保", "行政处罚", "质押冻结", "解禁", "再融资"):
        s = news.score_event(_ev(tag, "中"), asof)
        check(f"{tag} → 不干预", s["tier"] == "不干预", f'{s["r_score"]}/{s["tier"]}')
    s = news.score_event(_ev("回购", "中"), asof)
    check("回购 → 加分15", s["tier"] == "加分15" and s["mult"] == 1.15,
          f'{s["c_score"]}')
    check("分红 → 加分10", news.score_event(_ev("分红", "中"), asof)["tier"] == "加分10")
    check("股权激励 → 加分10",
          news.score_event(_ev("股权激励", "中"), asof)["tier"] == "加分10")
    check("增持不加分", news.score_event(_ev("增持", "中"), asof)["tier"] == "不干预")
    # 关键回归：没有风险事件时，来源与时效不得凭空造出风险分
    s = news.score_event(_ev("", "低"), asof)
    check("无风险事件 R=0", s["r_score"] == 0, str(s["r_score"]))
    check("无风险事件不干预", s["tier"] == "不干预")
    # 时效单调：同一事件越旧 R 越低
    new = news.score_event(_ev("立案调查", date="2026-10-28"), asof)["r_score"]
    old = news.score_event(_ev("立案调查", date="2026-01-05"), asof)["r_score"]
    check("时效单调（越旧分越低）", new > old, f"{new} vs {old}")
    # 利好不能冲抵利空（R 优先）
    s = news.score_event(_ev("立案调查"), asof)
    check("利好不冲抵利空", s["tier"] == "否决" and s["net_score"] < 0)


def test_negation() -> None:
    sec("A2 否定句不算命中")
    check("「不存在退市风险」→ 低", news.level_of("公司生产经营正常，不存在退市风险")[0] == "低")
    check("「未涉及立案调查」→ 低", news.level_of("说明：公司未涉及立案调查")[0] == "低")
    check("「被立案调查」仍是高", news.level_of("公司被中国证监会立案调查")[0] == "高")
    check("「否认减持」→ 低/中", news.level_of("控股股东否认减持计划")[0] in ("低", "中"))
    check("正常减持仍是中/高", news.level_of("控股股东拟减持不超过2%股份")[0] in ("中", "高"))
    check("质押降为中档（不降权）", news.level_of("关于控股股东部分股份质押的公告")[0] == "中")
    check("转债下修降为中档", news.level_of("关于预计触发转债转股价格向下修正条件的公告")[0] == "中")
    check("变更会计仍是高", news.level_of("关于变更会计师事务所的公告")[0] == "高")


def test_announcements() -> None:
    sec("A3 公告流并进事件面")
    tmp = tempfile.mkdtemp(prefix="news_ann_")
    old_cache, old_out = config.CACHE_DIR, config.OUT_DIR
    try:
        cache = os.path.join(tmp, "cache")
        out = os.path.join(tmp, "out")
        os.makedirs(os.path.join(cache, "cninfo_market"))
        os.makedirs(out)
        today = dt.date.today()
        day = today.isoformat()
        old_day = (today - dt.timedelta(days=200)).isoformat()
        cols = ["代码", "名称", "公告标题", "公告类型", "公告日期", "网址"]
        with io.open(os.path.join(cache, "cninfo_market", day + ".csv"),
                     "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({"代码": "000999", "名称": "华润三九", "公告标题": "关于回购注销部分限制性股票的公告",
                        "公告类型": "其他", "公告日期": day, "网址": "http://x/1"})
            w.writerow({"代码": "000999", "名称": "华润三九", "公告标题": "关于回购注销部分限制性股票的公告",
                        "公告类型": "其他", "公告日期": day, "网址": "http://x/1"})  # 重复
            w.writerow({"代码": "000999", "名称": "华润三九", "公告标题": "生产正常，不存在退市风险",
                        "公告类型": "澄清", "公告日期": day, "网址": "http://x/2"})  # 否定句
            w.writerow({"代码": "600000", "名称": "浦发银行", "公告标题": "关于被立案调查的公告",
                        "公告类型": "重大", "公告日期": day, "网址": "http://x/3"})  # 池外
        with io.open(os.path.join(cache, "cninfo_market", old_day + ".csv"),
                     "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({"代码": "000999", "名称": "华润三九", "公告标题": "关于退市风险警示的公告",
                        "公告类型": "重大", "公告日期": old_day, "网址": "http://x/4"})  # 窗口外
        config.CACHE_DIR, config.OUT_DIR = cache, out
        pairs = [("000999", "华润三九")]
        ev, days = news.announcement_events(pairs, pairs, (today - dt.timedelta(days=14)).isoformat())
        check("只留池内代码", all(e["code"] == "000999" for e in ev), str([e["code"] for e in ev]))
        check("窗口外日期不进", all(e["date"] == day for e in ev))
        check("去重后 1 条（否定句不算）", len(ev) == 1, str([e["title"] for e in ev]))
        check("覆盖交易日计数=1", days == 1, str(days))
        check("来源标巨潮公告", ev and ev[0]["source"] == "巨潮公告")
    finally:
        config.CACHE_DIR, config.OUT_DIR = old_cache, old_out
        shutil.rmtree(tmp, ignore_errors=True)


def test_policy() -> None:
    sec("A4 政策面（财联社电报，只做背景）")
    today = dt.date.today()
    d = today.isoformat()
    old = (today - dt.timedelta(days=30)).isoformat()
    rows = [
        {"标题": "央行宣布降准0.5个百分点", "内容": "释放长期资金", "发布日期": d},
        {"标题": "证监会发布减持新规征求意见稿", "内容": "", "发布日期": d},
        {"标题": "央行宣布降准0.5个百分点", "内容": "重复", "发布日期": d},   # 去重
        {"标题": "某公司中标大单", "内容": "", "发布日期": d},               # 非政策
        {"标题": "年内险资调研A股超1.46万次", "内容": "", "发布日期": d},      # 行业动态，不是政策
        {"标题": "公募基金规模创新高", "内容": "", "发布日期": d},             # 行业动态，不是政策
        {"标题": "国务院部署专项债", "内容": "", "发布日期": old},           # 窗口外
    ]
    ev = news.policy_events(days=7, rows=rows)
    titles = [e["title"] for e in ev]
    check("只留政策类", len(ev) == 2, str(titles))
    check("按标题去重", len(set(titles)) == len(titles))
    check("命中关键词记录", all(e["tag"] for e in ev), str(ev))
    check("来源标财联社", all(e["source"] == "财联社电报" for e in ev))
    check("窗口外不进", all(e["date"] >= (today - dt.timedelta(days=7)).isoformat() for e in ev))
    md = news.render_brief([], (today - dt.timedelta(days=7)).isoformat(), 3, policy=ev)
    check("简报有政策面段", "## 政策面" in md)
    check("政策条目不混进高/中事件段", md.count("央行宣布降准0.5个百分点") == 1, md[:400])
    check("政策条目没有 None 评分行", "RNone" not in md, md[:400])


def main() -> int:
    print("[news:selftest] 开始")
    test_level()
    test_window()
    test_sort()
    test_brief()
    test_write()
    test_config()
    test_integration()
    test_score()
    test_negation()
    test_announcements()
    test_policy()
    total = _ok + len(_bad)
    print(f"\n[news:selftest] 通过 {_ok} / {total} 项")
    if _bad:
        print("[news:selftest] 失败项：")
        for b in _bad:
            print("   -", b)
        return 1
    print("[news:selftest] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
