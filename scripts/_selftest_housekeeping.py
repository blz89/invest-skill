# -*- coding: utf-8 -*-
"""housekeeping.py 自测：记账幂等/跨年归档/保留策略/安全阀。

全部在临时目录里跑，不碰真实 work/ 与缓存。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config      # noqa: E402
import housekeeping as HK  # noqa: E402

_PASS = _FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("PASS  %s" % name)
    else:
        _FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


def _sandbox():
    """把 HK 的路径与 config 的路径全指向临时目录。"""
    root = os.path.join(tempfile.gettempdir(), "hk_selftest")
    shutil.rmtree(root, ignore_errors=True)
    work = os.path.join(root, "work")
    out = os.path.join(work, "out")
    cache = os.path.join(work, "cache")
    for d in (work, out, cache):
        os.makedirs(d, exist_ok=True)
    HK.WORK = work
    HK.LOG_PATH = os.path.join(work, "daily_log.md")
    HK.ARCHIVE_DIR = os.path.join(work, "_archive")
    config.OUT_DIR = out
    config.CACHE_DIR = cache
    config.PATROL = dict(getattr(config, "PATROL", {}),
                         cache_keep_days=2, patrol_md_keep_days=60)
    return root, work, out, cache


def _mkcache(cache, key, dates, sub="fund_nav"):
    d = os.path.join(cache, sub)
    os.makedirs(d, exist_ok=True)
    for ds in dates:
        with io.open(os.path.join(d, "%s__%s.csv" % (key, ds)), "w", encoding="utf-8") as f:
            f.write("x")
    return d


def test_row_format():
    row = {"日期": "2026-10-30", "总资产(万)": "5.00", "累计%": "0.00", "回撤%": "-0.50",
           "仓位%": "60", "当日盈亏": "50.00", "花费": "1.20", "沪深300": "4083.87",
           "档位": "防守", "现金净利率": "1.25/1.08", "涨停/跌停": "39/42",
           "巡检(高/总)": "0/2", "决策": "已出计划"}
    line = HK._row_line(row)
    check("行以 '| 2026-10-30' 开头", line.startswith("| 2026-10-30 |"))
    check("列数 = 13+2 个竖线", line.count("|") == len(HK.COLS) + 1, line)
    check("表头列数一致", ("| " + " | ".join(HK.COLS) + " |").count("|") == line.count("|"))
    check("空值列不会漏成空串", "|  |" not in line, line)


def test_append_idempotent(_root, work, _out, _cache):
    row = {"日期": "2026-10-30", "沪深300": "1", "档位": "防守"}
    act1 = HK.append(row)
    rows = HK.tail(0)
    check("首次追加 = appended", act1 == "appended", act1)
    check("首次只有 1 行数据", len(rows) == 1, len(rows))
    row2 = dict(row, 现金净利率="1.25/1.08")
    act2 = HK.append(row2)
    rows2 = HK.tail(0)
    check("同日重跑 = replaced", act2 == "replaced", act2)
    check("同日重跑仍 1 行（不重复）", len(rows2) == 1, len(rows2))
    check("重跑后内容被替换", "1.25/1.08" in rows2[0], rows2[0])
    HK.append({"日期": "2026-10-29"})
    rows3 = HK.tail(0)
    check("补昨天 = 2 行", len(rows3) == 2, len(rows3))
    check("按日期升序", rows3[0].startswith("| 2026-10-29") and rows3[1].startswith("| 2026-10-30"))
    check("tail(1) 只返回最后一行", len(HK.tail(1)) == 1)
    check("文件里有表头", "每日巡检记录" in io.open(HK.LOG_PATH, encoding="utf-8").read())


def test_rotation(_root, _work, _out, _cache):
    HK.append({"日期": "2025-12-31"}, "2025-12-31")
    main = io.open(HK.LOG_PATH, encoding="utf-8").read()
    check("主文件不再含去年行", "| 2025-12-31" not in main)
    ap = os.path.join(HK.ARCHIVE_DIR, "daily_log_2025.md")
    check("旧年归档文件已生成", os.path.exists(ap))
    check("归档里含去年行", "| 2025-12-31" in io.open(ap, encoding="utf-8").read())
    check("主文件仍含当年行", "| 2026-10-30" in main)


def test_prune(cache):
    for d in os.listdir(cache):
        shutil.rmtree(os.path.join(cache, d), ignore_errors=True)
    _mkcache(cache, "000015", ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"])
    _mkcache(cache, "000016", ["2026-10-28"], sub="fund_nav")
    _mkcache(cache, "sh510030", ["2026-10-25", "2026-10-28"], sub="etf_daily")
    # 无日期后缀的文件不该被动
    with io.open(os.path.join(cache, "kept.json"), "w", encoding="utf-8") as f:
        f.write("{}")

    to_del, stats = HK.plan_prune(keep_last=2)
    names = sorted(os.path.basename(p) for p in to_del)
    check("plan_prune 命中 2 个旧快照", len(to_del) == 2, names)
    check("同 key 留最近 2 天", not any("2026-10-27" in n or "2026-10-28" in n for n in names), names)
    check("只有 1 天的 key 不删", "000016__2026-10-28.csv" not in names)

    r = HK.prune_cache(keep_last=2, dry=True)
    check("dry 模式报候选数", r["ok"] and r["deleted"] == 2 and r["candidates"] == 2, r)
    check("dry 模式真没删", len(HK.plan_prune(2)[0]) == 2)

    r2 = HK.prune_cache(keep_last=2)
    check("实删 2 个", r2["ok"] and r2["deleted"] == 2, r2)
    check("剩余 csv = 2+1+2", len([p for p in _all(cache) if p.endswith(".csv")]) == 5)
    check("无日期文件保留", os.path.exists(os.path.join(cache, "kept.json")))

    # 安全阀：keep_last=0 会被夹到 1；构造「只剩 1 个日期」的场景不会触发
    r3 = HK.prune_cache(keep_last=1)
    check("keep_last<1 被夹成 1", r3.get("keep_last") == 1, r3)


def _all(root):
    out = []
    for dp, _d, fs in os.walk(root):
        for f in fs:
            out.append(os.path.join(dp, f))
    return out


def test_guard(cache):
    for d in os.listdir(cache):
        shutil.rmtree(os.path.join(cache, d), ignore_errors=True)
    # 100 个 key，每个 10 个日期，keep_last=2 → 要删 800/1000 = 80%，正好卡边界
    for i in range(100):
        _mkcache(cache, "k%03d" % i, ["2026-10-%02d" % d for d in range(1, 11)])
    r = HK.prune_cache(keep_last=2, guard=0.5)
    check("超阈值拒删并给原因", r["ok"] is False and "拒删" in r["error"], r.get("error"))
    check("拒删时未动任何文件", len([p for p in _all(cache) if p.endswith(".csv")]) == 1000)


def test_patrol_md(out):
    for fn, txt in [("patrol_2026-01-01.md", "old"), ("patrol_2026-10-30.md", "new"),
                    ("other.md", "x")]:
        with io.open(os.path.join(out, fn), "w", encoding="utf-8") as f:
            f.write(txt)
    r = HK.prune_patrol_md(keep_days=60, today="2026-10-30")
    check("旧巡检 md 被删", r["deleted"] == 1, r)
    check("旧文件确实不在了", not os.path.exists(os.path.join(out, "patrol_2026-01-01.md")))
    check("当天文件保留", os.path.exists(os.path.join(out, "patrol_2026-10-30.md")))
    check("非 patrol_*.md 不动", os.path.exists(os.path.join(out, "other.md")))


def test_paper_reports(out):
    """报告保留（2026-11-21 用户要求）：周报/HTML/订单按文件名日期保留，年报后再统一删；
    `_history/` 与 `decision_*.json` 永不在此删除，每类最新一份永不删。"""
    pdir = os.path.join(out, "paper")
    hdir = os.path.join(pdir, "_history")
    os.makedirs(hdir, exist_ok=True)
    old = dict(getattr(config, "PAPER", {}) or {})
    try:
        config.PAPER = dict(old, package_dir=pdir, paper_keep_days=400)
        for fn in ["weekly_2025-01-01.md", "weekly_2025-01-08.md", "report_2025-01-08.html",
                   "orders_2025-01-08.csv", "report_2026-10-30.html", "weekly_2026-10-30.md",
                   "decision_2025-01-08.json", "decision_latest.json", "notes.txt"]:
            with io.open(os.path.join(pdir, fn), "w", encoding="utf-8") as f:
                f.write("x")
        with io.open(os.path.join(hdir, "weekly_2025-01-01.20260101-000000.md"),
                     "w", encoding="utf-8") as f:
            f.write("x")
        r = HK.prune_paper_reports(keep_days=60, today="2026-10-30")
        check("报告清理返回 keep_days", r["keep_days"] == 60, r)
        check("超期旧周报/旧 HTML 被删（3 个）", r["deleted"] == 3, r)
        check("旧周报确实不在了", not os.path.exists(os.path.join(pdir, "weekly_2025-01-01.md")))
        check("旧 HTML 确实不在了", not os.path.exists(os.path.join(pdir, "report_2025-01-08.html")))
        check("当期周报保留", os.path.exists(os.path.join(pdir, "weekly_2026-10-30.md")))
        check("当期 HTML 保留", os.path.exists(os.path.join(pdir, "report_2026-10-30.html")))
        check("每类最新一份永不删（旧订单也在）",
              os.path.exists(os.path.join(pdir, "orders_2025-01-08.csv")))
        check("期次快照不在此删", os.path.exists(os.path.join(pdir, "decision_2025-01-08.json")))
        check("decision_latest 不在此删", os.path.exists(os.path.join(pdir, "decision_latest.json")))
        check("_history 里的旧版备份不在此删",
              os.path.exists(os.path.join(hdir, "weekly_2025-01-01.20260101-000000.md")))
        check("非报告文件不动", os.path.exists(os.path.join(pdir, "notes.txt")))
        with io.open(os.path.join(pdir, "weekly_2024-01-01.md"), "w", encoding="utf-8") as f:
            f.write("x")
        rd = HK.prune_paper_reports(keep_days=60, today="2026-10-30", dry=True)
        check("dry=True 只报不删", rd["deleted"] == 1
              and os.path.exists(os.path.join(pdir, "weekly_2024-01-01.md")), rd)
        check("默认保留天数取 config.PAPER['paper_keep_days']",
              (config.PAPER.get("paper_keep_days") or 0) == 400, config.PAPER.get("paper_keep_days"))
    finally:
        config.PAPER = old


def test_build_row(out):
    row = HK.build_row({}, "2026-10-30")
    check("build_row 无产物也给出日期", row["日期"] == "2026-10-30")
    check("无产物时利率写 未实测", row["现金净利率"] == "未实测/未实测", row["现金净利率"])
    check("总资产有值或是 -",
          row["总资产(万)"] == "-" or row["总资产(万)"].replace(".", "").isdigit(),
          row["总资产(万)"])
    check("回撤有值或是 -",
          row["回撤%"] == "-" or row["回撤%"].lstrip("-").replace(".", "").isdigit(),
          row["回撤%"])
    check("新表头 13 列", len(HK.COLS) == 13, len(HK.COLS))
    check("年报需要的列都在",
          all(c in HK.COLS for c in ("总资产(万)", "累计%", "回撤%", "仓位%", "当日盈亏", "花费")),
          HK.COLS)

    with io.open(os.path.join(out, "stage3_plan.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": "2026-10-30 00:05:00",
                   "market_regime": {"regime": "防守", "bench_price": 4083.87}}, f)
    with io.open(os.path.join(out, "sentiment.json"), "w", encoding="utf-8") as f:
        json.dump({"temp": {"limit_up": 39, "limit_down": 42}}, f)
    res = {"date": "2026-10-30", "n_high": 1,
           "alerts": [{"kind": "x", "level": "高"}],
           "rates": {"live": True, "repo": 0.0125, "money_fund": 0.0108}}
    row = HK.build_row(res, "2026-10-30")
    check("档位取 market_regime.regime", row["档位"] == "防守", row["档位"])
    check("指数取 bench_price", row["沪深300"] == "4083.87", row["沪深300"])
    check("情绪 39/42", row["涨停/跌停"] == "39/42", row["涨停/跌停"])
    with io.open(os.path.join(out, "sentiment.json"), "w", encoding="utf-8") as f:
        json.dump({"temp": {"limit_up": 39, "limit_down": 0, "break": 0,
                   "suspect": ["跌停池空返回"]}}, f)
    check("空返回时加⚠标记", HK.build_row(res, "2026-10-30")["涨停/跌停"] == "39/0⚠",
          HK.build_row(res, "2026-10-30")["涨停/跌停"])
    check("巡检 高/总 = 1/1", row["巡检(高/总)"] == "1/1", row["巡检(高/总)"])
    check("决策 已出计划", row["决策"] == "已出计划", row["决策"])
    check("利率合成一格 1.25/1.08", row["现金净利率"] == "1.25/1.08", row["现金净利率"])


def test_ledger_stats(out):
    """账本四件套：总资产/累计/回撤/仓位 + 老键兼容 + 无 nav 回退（含场外）。"""
    sf = os.path.join(out, "_st.json")
    with io.open(sf, "w", encoding="utf-8") as f:
        json.dump({"cash": 30000.0,
                   "holdings": {"sh510050": {"shares": 1000, "last_close": 2.0}},
                   "nav": [{"date": "2026-10-29", "nav": 50000.0},
                           {"date": "2026-10-30", "nav": 48000.0}]}, f)
    old = dict(HK.config.PAPER)
    try:
        HK.config.PAPER = {"default_ledger": "T", "ledgers": {
            "T": {"capital": 50000.0, "state_file": sf}}}
        st = HK._ledger_stats("T")
    finally:
        HK.config.PAPER = old
    check("总资产取 nav 末值（生产键 nav）", st.get("total") == 48000.0, st.get("total"))
    check("累计收益 -4%", abs((st.get("cum") or 0) + 4.0) < 1e-6, st.get("cum"))
    check("回撤 -4%", abs((st.get("dd") or 0) + 4.0) < 1e-6, st.get("dd"))
    check("仓位 4.17%", abs((st.get("pos") or 0) - 2000.0 / 48000.0 * 100) < 1e-6, st.get("pos"))
    check("当日盈亏 -2000", st.get("pnl") == -2000.0, st.get("pnl"))

    # 2026-10 之前的老 nav 行用 day/value 存净值——读的时候要兼容，不能退回错误口径
    with io.open(sf, "w", encoding="utf-8") as f:
        json.dump({"cash": 0.0, "holdings": {},
                   "nav": [{"day": "2026-10-29", "value": 50000.0},
                           {"day": "2026-10-30", "value": 48000.0}]}, f)
    try:
        HK.config.PAPER = {"default_ledger": "T", "ledgers": {
            "T": {"capital": 50000.0, "state_file": sf}}}
        st2 = HK._ledger_stats("T")
    finally:
        HK.config.PAPER = old
    check("老格式 day/value 键兼容", st2.get("total") == 48000.0, st2.get("total"))

    # 没有 nav（首日/账本刚建）时退回现金+场内+场外：场外腿不能漏，否则总资产会凭空少一截
    with io.open(sf, "w", encoding="utf-8") as f:
        json.dump({"cash": 30000.0,
                   "holdings": {"sh510050": {"shares": 1000, "last_close": 2.0}},
                   "otc_holdings": {"OTC:016800": {
                       "code": "OTC:016800", "name": "债基", "amount": 2400.0,
                       "acc_nav": 1.1252, "nav": 1.1385, "nav_date": "2026-10-30"}},
                   "nav": []}, f)
    try:
        HK.config.PAPER = {"default_ledger": "T", "ledgers": {
            "T": {"capital": 50000.0, "state_file": sf}}}
        st3 = HK._ledger_stats("T")
    finally:
        HK.config.PAPER = old
    import otc_book
    expect = 30000.0 + 2000.0 + otc_book.mv_of(
        {"amount": 2400.0, "acc_nav": 1.1252, "nav": 1.1385})
    check("无 nav 回退含场外市值", abs((st3.get("total") or 0) - expect) < 0.01,
          f"{st3.get('total')} != {expect}")

    # 9 列老行 → 13 列新行
    legacy = "| 2026-10-01 | 3900.00 | 防守 | 1.25% | 1.08% | 30/20 | 2/1/0 | 0/1 | 已出计划 |"
    up = HK._upgrade_row(legacy)
    cells = [c.strip() for c in up.strip().strip("|").split("|")]
    check("老行升级到 13 列", len(cells) == len(HK.COLS), len(cells))
    check("老行利率合并", cells[HK.COLS.index("现金净利率")] == "1.25/1.08",
          cells[HK.COLS.index("现金净利率")])
    check("老行保留指数", cells[HK.COLS.index("沪深300")] == "3900.00")
    check("老行新列填 '-'", cells[HK.COLS.index("总资产(万)")] == "-")


def test_year(_root, _work, _out, _cache):
    for d, emo, alarm in (("2026-01-05", "30/20", "0/1"), ("2026-01-06", "39/0⚠", "1/3")):
        HK.append({"日期": d, "档位": "防守", "现金净利率": "1.25/1.08",
                   "涨停/跌停": emo, "巡检(高/总)": alarm, "决策": "已出计划"})
    st = HK.year_stat("2026")
    check("年报底稿天数 ≥2", st["天数"] >= 2, st["天数"])
    check("涨停合计含 ⚠ 行", st["涨停合计"] == 69, st["涨停合计"])
    check("存疑天数记 1", st["存疑天数"] == 1, st["存疑天数"])
    check("决策日计数 ≥2", st["决策日"] >= 2, st["决策日"])
    check("利率区间取到实测值", "1.25%" in HK.year_md("2026").replace("1.2500%", "1.25%"),
          HK.year_md("2026")[:120])
    check("年报 md 带年份标题", "巡检年报底稿 · 2026" in HK.year_md("2026"))
    # 年报必须能算出「收益 / 回撤 / 花费」三块，缺一块就等于没有年报
    for d, asset, cum, dd, cost in (("2026-01-05", "5.00", "0.00", "0.00", "1.20"),
                                    ("2026-01-06", "5.05", "1.00", "-0.50", "0.80")):
        HK.append({"日期": d, "档位": "防守", "总资产(万)": asset, "累计%": cum,
                   "回撤%": dd, "仓位%": "60", "当日盈亏": "50.00", "花费": cost,
                   "涨停/跌停": "30/20", "巡检(高/总)": "0/1", "决策": "已出计划"})
    st2 = HK.year_stat("2026")
    check("年报收到资产序列", 5.00 in st2["资产"], st2["资产"])
    check("年报收到最大回撤", min(st2["回撤"]) == -0.5, st2["回撤"])
    check("年报收到 token 花费", abs(st2["花费合计"] - (1.2 + 0.8)) < 1e-9, st2["花费合计"])
    md = HK.year_md("2026")
    check("年报 md 含资产区间", "资产：" in md, md[:200])
    check("年报 md 含最大回撤", "最大回撤" in md)
    check("年报 md 含 token 花费", "token 花费合计" in md)


def test_sweep(_root, _work, _out, cache, out):
    for d in os.listdir(cache):
        shutil.rmtree(os.path.join(cache, d), ignore_errors=True)
    _mkcache(cache, "k1", ["2026-10-28", "2026-10-29", "2026-10-30"])
    r = HK.sweep(today="2026-10-30")
    check("sweep 返回 cache+patrol_md+paper+archive",
          set(r) == {"cache", "patrol_md", "paper", "archive"}, r)
    check("sweep 保留 2 天", r["cache"]["keep_last"] == 2, r["cache"])
    check("sweep 删掉 1 个旧快照", r["cache"]["deleted"] == 1, r["cache"])


def test_week(root, work, out, cache):
    """周汇总：读周一到周五的 daily_log 与账本 nav（2026-10-31 加）。"""
    print("\n周汇总：读周一到周五的 daily_log 与账本 nav")
    log = os.path.join(work, "daily_log.md")
    rows = [
        "| 2026-11-02 | 5.00 | 0.00 | 0.00 | 0 | - | 1.00 | 4000.00 | 防守 | 1.25/1.09 | 39/0 | 0/0 | 未跑 |",
        "| 2026-11-03 | 5.05 | 0.10 | 0.00 | 10 | - | 1.00 | 4010.00 | 防守 | 1.25/1.09 | 30/2 | 1/1 | 未跑 |",
        "| 2026-11-06 | 5.10 | 0.20 | -0.10 | 20 | - | 1.00 | 4020.00 | 进攻 | 1.27/1.10 | 20/5 | 0/2 | 已出计划 |",
    ]
    io.open(log, "w", encoding="utf-8").write("\n".join(HK._head() + rows) + "\n")
    old_log, old_out, old_lg = HK.LOG_PATH, config.OUT_DIR, config.PAPER.get("ledgers")
    try:
        HK.LOG_PATH, config.OUT_DIR = log, out
        config.PAPER["ledgers"] = {
            "w50": {"capital": 50000.0, "state_file": os.path.join(work, "s50.json")},
            "w100": {"capital": 100000.0, "state_file": os.path.join(work, "s100.json")},
        }
        for lg, cap, navs in (("w50", 50000.0, [(2, 50000.0), (3, 50500.0), (6, 51000.0)]),
                              ("w100", 100000.0, [(2, 100000.0), (6, 100000.0)])):
            st = {"meta": {"capital": cap}, "cash": cap, "holdings": {}, "trades": [],
                  "orders": [], "events": [],
                  "nav": [{"date": "2026-11-%02d" % d, "nav": v} for d, v in navs]}
            io.open(os.path.join(work, "s%s.json" % lg[1:]), "w", encoding="utf-8").write(
                json.dumps(st, ensure_ascii=False))
        dig = HK.week_digest("2026-11-02", "2026-11-06")
        check("周汇总识别 3 个巡检日", len(dig["days"]) == 3, str(dig["days"]))
        check("告警累加（0/0+1/1+0/2 → 高 1 总 3）",
              dig["n_high"] == 1 and dig["n_alert"] == 3, str((dig["n_high"], dig["n_alert"])))
        check("50k 周涨跌 50000→51000 = +2.00%",
              abs(dig["ledgers"]["w50"]["week_chg"] - 0.02) < 1e-9,
              str(dig["ledgers"]["w50"]["week_chg"]))
        check("利率取 daily_log 的现金净利率列（最新 1.27%）",
              abs(dig["rates"]["2026-11-06"]["repo"] - 0.0127) < 1e-9)
        md = HK.week_md(dig)
        check("周汇总 md 有表头「日期」", "| 日期 |" in md)
        check("周汇总 md 有「本周合计」", "**本周合计**" in md)
        p = HK.write_week("2026-11-02")
        check("write_week 落盘 patrol_weekly_<周一>.md",
              os.path.basename(p["path"]) == "patrol_weekly_2026-11-02.md")
    finally:
        HK.LOG_PATH, config.OUT_DIR = old_log, old_out
        if old_lg is not None:
            config.PAPER["ledgers"] = old_lg


def test_archive(work):
    """每日快照目录保留（2026-11-26 清冗余）：只删纯日期且超期的；回滚目录与最新一份不动。"""
    root = HK.archive_root()
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for d in ["2026-11-01", "2026-11-10", "2026-11-24", "2026-11-26",
              "2026-11-26_pre_fresh_start", "_legacy_cache"]:
        os.makedirs(os.path.join(root, d), exist_ok=True)
        with io.open(os.path.join(root, d, "x.bin"), "wb") as f:
            f.write(b"x" * (700 * 1024))  # 每份 700KB，让 freed_mb 有可读数字
    to_del, stats = HK.plan_archive_prune(keep_days=7, today="2026-11-26")
    got = sorted(os.path.basename(p) for p in to_del)
    check("每日快照：只删超期纯日期目录", got == ["2026-11-01", "2026-11-10"], str(got))
    check("每日快照：回滚目录不删", os.path.isdir(os.path.join(root, "2026-11-26_pre_fresh_start")))
    r = HK.prune_archive(keep_days=7, today="2026-11-26", dry=True)
    check("每日快照：dry 只报不删",
          r["ok"] and r["deleted"] == 2 and os.path.isdir(os.path.join(root, "2026-11-01")), str(r))
    r = HK.prune_archive(keep_days=7, today="2026-11-26")
    check("每日快照：实删 2 个并释放空间",
          r["ok"] and r["deleted"] == 2 and r["freed_mb"] > 0
          and not os.path.isdir(os.path.join(root, "2026-11-01")), str(r))
    check("每日快照：保留期内不动", os.path.isdir(os.path.join(root, "2026-11-24")))
    check("每日快照：sweep 带 archive 项", "archive" in HK.sweep(dry=True))
    # 安全阀：超期目录占比 >80% 时拒删（防日期解析/路径配错）
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for d in ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]:
        os.makedirs(os.path.join(root, d))
    r = HK.prune_archive(keep_days=1, today="2026-11-26")
    check("每日快照：>80% 拒删并给原因", r["ok"] is False and "拒删" in r["error"], str(r))
    shutil.rmtree(root, ignore_errors=True)


def main():
    root, work, out, cache = _sandbox()
    try:
        test_row_format()
        test_append_idempotent(root, work, out, cache)
        test_rotation(root, work, out, cache)
        test_prune(cache)
        test_guard(cache)
        test_patrol_md(out)
        test_archive(work)
        test_paper_reports(out)
        test_build_row(out)
        test_ledger_stats(out)
        test_year(root, work, out, cache)
        test_sweep(root, work, out, cache, out)
        test_week(root, work, out, cache)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("-" * 62)
    print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
