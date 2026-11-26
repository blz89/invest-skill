# -*- coding: utf-8 -*-
"""housekeeping.py —— 每日巡检的「记账」与「清场」。

两件事，都在每个工作日巡检（DSH 20:00 会话跑 patrol.py）末尾由 `patrol.run()` 调一次：

1. **daily_log**：把当天的巡检结论压成**一行**，追加到 `work/daily_log.md`。
   这是给人和 AI 看的时间轴——一年 365 行、几十 KB，永不膨胀。
   同一天重跑是**幂等**的（替换当天那行，不追加第二条）；跨年自动把旧年
   的行搬到 `work/_archive/daily_log_YYYY.md`，主文件只留当年。

2. **prune**：缓存与中间产物的保留策略。
   `datasource._cache_file()` 的文件名带 `__YYYY-MM-DD`，**每天一整套新快照、
   不覆盖旧快照**——不清理的话按实测速度 ≈100 MB/天 无上限增长
   （2026-10-30 实测：5 天 28,009 个文件 / 536 MB）。
   策略：同一个 key 只保留最近 `keep_last` 个日期的快照（默认 2 天，防跨零点
   重跑时需要昨天那份），更早的删掉；`out/patrol_*.md` 超过 `md_keep_days`
   天也删掉（结论已经在 daily_log 里留了一行，md 只是当日快照）。
   `stock/data/archive/<YYYY-MM-DD>/` 每日快照目录保留 `archive_keep_days` 天
   （默认 14 天，每份约 2.5MB；最新一份永不删，名字带 `_pre_fresh_start` 等
   非纯日期的回滚目录一律不动）。

安全阀：一次要删掉超过 80% 的缓存文件时**拒绝执行**并报错——这只会发生在
「命名规则变了/路径配错」的时候，宁可留着也不能误删。

CLI：
    python housekeeping.py --show [--n 30]     # 打印 daily_log 末尾 N 行
python housekeeping.py --week [YYYY-MM-DD] # 周汇总：读周一到周五，写 out/patrol_weekly_<周一>.md
    python housekeeping.py --prune [--dry]     # 单独跑清理（dry=只报不删）
    python housekeeping.py --year 2026 [--json]  # 年报底稿（资产区间/年化/回撤/正收益天数/token）
    python housekeeping.py --append 2026-10-30   # 用现有 patrol 结果补记一天

退出码：0 正常；1 没给参数（打印本说明）；2 参数错误。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

WORK = config.WORK_DIR
LOG_PATH = os.path.join(WORK, "daily_log.md")
ARCHIVE_DIR = os.path.join(WORK, "_archive")

# 一行一天。**列的作用是让年报能直接从这张表算出来**：
# 收益三件（总资产/累计/当日盈亏）、风险两件（回撤/仓位）、成本一件（花费）、
# 环境三件（沪深300/档位/利率）、情绪、合规（巡检告警）、执行（决策）。
# 列宽控制：单个格子只放数字，不写句子，人眼扫一列能看出趋势。
COLS = ["日期", "总资产(万)", "累计%", "回撤%", "仓位%", "当日盈亏",
        "花费", "沪深300", "档位", "现金净利率", "涨停/跌停", "巡检(高/总)", "决策"]

# 2026-10-30 之前的老行只有 9 列，读的时候按老口径解析，不丢历史
LEGACY_COLS = ["日期", "沪深300", "档位", "逆回购净", "货基", "涨停/跌停",
               "新闻(条/降/避)", "巡检(高/总)", "决策"]

DATE_RE = re.compile(r"__(20\d{2}-\d{2}-\d{2})\.(?:csv|json|parquet|xlsx)$")
MD_RE = re.compile(r"^patrol_(20\d{2}-\d{2}-\d{2})\.md$")


# ---------------------------------------------------------------- 小工具
def _p(*a):
    return os.path.join(*a)


def _load_json(path):
    """读 json，任何异常都返回 {}——巡检记账不能因为某个产物缺失就崩。"""
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _num(x, nd=0):
    if x is None or x == "":
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return ("%." + str(nd) + "f") % v


def _pct_pct(x, nd=2):
    """利率既可能是小数(0.0125)也可能是百分数(1.25)，统一显示成百分数。"""
    if x is None or x == "":
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    if abs(v) <= 0.5:          # 小数口径
        v *= 100.0
    return ("%." + str(nd) + "f%%") % v


# ------------------------------------------------------- 从产物里抽一行
def _csv_rows(path):
    """读 csv 成一个 dict 列表（列名来自表头）；文件不存在返回 []。"""
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []
    if not lines:
        return []
    head = [h.strip() for h in lines[0].split(",")]
    out = []
    for ln in lines[1:]:
        parts = ln.split(",")
        out.append(dict(zip(head, parts)))
    return out


def _ledger_stats(ledger: str) -> dict:
    """主力账本的收益/风险四件套：总资产、累计收益、回撤、仓位。

    数据源是账本 `state_*.json` 的 `nav` 序列（每笔结算一行
    `{date,nav,cash,market_value,otc_value,...}`；2026-10 前的老行用 `value` 存净值）。
    没有 nav 就退回「现金 + 场内市值 + 场外市值」的当期值——宁可少一个数，不编一个数。
    """
    try:
        sf = config.PAPER["ledgers"][ledger]["state_file"]
        cap = float(config.PAPER["ledgers"][ledger].get("capital") or 0.0)
    except (KeyError, TypeError, ValueError):
        return {}
    st = _load_json(sf)
    if not st:
        return {}
    nav = [r for r in (st.get("nav") or []) if isinstance(r, dict)]
    vals = []
    for r in nav:
        try:
            v = r.get("nav")
            if v is None:
                v = r.get("value")                 # 老格式兼容
            vals.append((str(r.get("day") or r.get("date") or ""), float(v or 0.0)))
        except (TypeError, ValueError):
            continue
    try:
        total = float((vals[-1][1] if vals else 0.0)) or None
    except (TypeError, ValueError):
        total = None
    if not total:
        cash = float(st.get("cash") or 0.0)
        mv = 0.0
        for h in (st.get("holdings") or {}).values():
            try:
                mv += int(h.get("shares") or 0) * float(h.get("last_close") or 0.0)
            except (TypeError, ValueError):
                continue
        try:                                       # 场外影子账也是总资产的一部分，不能漏
            import otc_book
            mv += float(otc_book.market_value(st) or 0.0)
        except Exception:                          # noqa: BLE001 - 影账坏了只退回场内口径
            pass
        total = cash + mv
    peak = max([v for _, v in vals] + [total or 0.0]) or 0.0
    out = {"total": total, "capital": cap}
    out["cum"] = (total / cap - 1.0) * 100.0 if cap > 0 and total else None
    out["dd"] = (total / peak - 1.0) * 100.0 if peak > 0 and total else None
    out["pnl"] = (vals[-1][1] - vals[-2][1]) if len(vals) >= 2 else None
    try:
        mv = 0.0
        for h in (st.get("holdings") or {}).values():
            mv += int(h.get("shares") or 0) * float(h.get("last_close") or 0.0)
        out["pos"] = (mv / total * 100.0) if total else None
    except (TypeError, ValueError, ZeroDivisionError):
        out["pos"] = None
    return out


def _token_cost(day: str) -> str:
    """当日 LLM 花费（元）——年报要算「净收益 = 收益 − token」。"""
    try:
        import billing
        v = billing.day_debit_cny(day)
        return "-" if v is None else "%.2f" % float(v)
    except Exception:                                   # noqa: BLE001 - 计费读不到不阻塞巡检
        return "-"


def build_row(patrol_res, day=None):
    """把巡检结果 + 当日各层产物压成 dict（缺的填空缺 '-')。"""
    day = day or (patrol_res or {}).get("date") or dt.date.today().isoformat()

    # 账本：收益/风险（取默认账本，本部署 = 5 万那本）
    main_ledger = str((getattr(config, "PAPER", {}) or {}).get("default_ledger") or "")
    st = _ledger_stats(main_ledger) if main_ledger else {}

    # 市场：仓位档位与基准点位以当日计划为准
    plan = _load_json(_p(config.OUT_DIR, "stage3_plan.json"))
    reg = plan.get("market_regime") or {}
    tier = reg.get("regime") or ""
    idx = reg.get("bench_price")

    # 情绪：同花顺涨停/跌停（当日快照，trade_date 可能滞后一天）
    sent = _load_json(_p(config.OUT_DIR, (getattr(config, "SENTIMENT", {}) or {}).get("out_json", "sentiment.json")))
    temp = sent.get("temp") or {}
    up, down = temp.get("limit_up"), temp.get("limit_down")
    emo = "-" if up is None and down is None else "%s/%s" % (_num(up), _num(down))
    if temp.get("suspect"):
        # 跌停/炸板池空返回时不要显示成「0 家」，加 ⚠ 让读的人知道这个数不可信
        emo += "⚠"

    alerts = (patrol_res or {}).get("alerts") or []
    n_high = (patrol_res or {}).get("n_high", sum(1 for a in alerts if a.get("level") == "高"))

    # 决策：当天是否产出过计划
    gen = str(plan.get("generated_at") or "")
    decision = "已出计划" if gen[:10] == day else "未跑"

    rates = (patrol_res or {}).get("rates") or {}
    live = rates.get("live")
    repo = _pct_pct(rates.get("repo")) if live else "未实测"
    mf = _pct_pct(rates.get("money_fund")) if live else "未实测"
    total_wan = _num((st.get("total") or 0) / 10000.0, 2) if st.get("total") else "-"
    return {
        "日期": day,
        "总资产(万)": total_wan,
        "累计%": _num(st.get("cum"), 2),
        "回撤%": _num(st.get("dd"), 2),
        "仓位%": _num(st.get("pos"), 0),
        "当日盈亏": _num(st.get("pnl"), 2),
        "花费": _token_cost(day),
        "沪深300": _num(idx, 2),
        "档位": tier or "-",
        "现金净利率": "%s/%s" % (repo.replace("%", ""), mf.replace("%", "")),
        "涨停/跌停": emo,
        "巡检(高/总)": "%s/%s" % (_num(n_high), _num(len(alerts))),
        "决策": decision,
    }


# ---------------------------------------------------------- daily_log IO
def _row_line(row):
    return "| " + " | ".join(str(row.get(c, "-")) for c in COLS) + " |"


def _head():
    return ["# 每日巡检记录", "",
            "> 一天一行，由 `housekeeping.py` 在 DSH 工作日巡检（20:00 会话跑 patrol.py）末尾自动追加。",
            "> 主文件滚动保留**当年**，跨年归档到 `work/_archive/daily_log_YYYY.md`（归档**不删**，"
            "一年一档，做年报直接读归档）。",
            "> 想知道某天到底发生了什么 → 看当天的 `out/patrol_<日期>.md`（保留 365 天）。",
            "> 年报底稿：`python housekeeping.py --year 2026`。",
            "",
            "| " + " | ".join(COLS) + " |",
            "|" + "---|" * len(COLS)]


def split_rows(lines):
    """把 daily_log 拆成 (表头行, 数据行数组)——数据行 = 以 '| 20' 开头的行。"""
    head, rows = [], []
    for ln in lines:
        if ln.startswith("| 20"):
            rows.append(ln)
        else:
            head.append(ln)
    return head, rows


def _upgrade_row(line):
    """把 2026-10-30 之前的 9 列老行升级成当前 13 列（老字段保留，新字段留 '-'）。

    升级一次就写回文件；老行里 逆回购净/货基 两列合并成「现金净利率」一格。
    """
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) == len(COLS):
        return line
    if len(cells) != len(LEGACY_COLS):
        return line
    old = dict(zip(LEGACY_COLS, cells))
    row = {c: "-" for c in COLS}
    for c in ("日期", "沪深300", "档位", "涨停/跌停", "巡检(高/总)", "决策"):
        row[c] = old.get(c, "-")
    row["现金净利率"] = "%s/%s" % (old.get("逆回购净", "-").replace("%", ""),
                                   old.get("货基", "-").replace("%", ""))
    return _row_line(row)


def append(row, day=None):
    """幂等追加：同一天已有行就替换，不重复；跨年自动归档。返回动作字符串。"""
    day = day or row.get("日期") or dt.date.today().isoformat()
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(LOG_PATH):
        with io.open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    else:
        lines = []
    head, rows = split_rows([ln for ln in lines if ln.strip()])
    if not head or len(head) == 0:
        head = _head()
    # 表头是旧版（列数对不上）→ 换新表头，并把老行逐行升级
    for ln in head:
        if ln.startswith("| ") and ln.count("|") >= 3:
            if len([c for c in ln.strip().strip("|").split("|")]) != len(COLS):
                head = _head()
            break
    rows = [_upgrade_row(r) for r in rows]
    line = _row_line(row)

    # 先并入当天行（同日替换），再按年份拆分——顺序反了会把新行也归档出去
    replaced = False
    for i, r in enumerate(rows):
        if r.startswith("| " + day + " |"):
            rows[i] = line
            replaced = True
            break
    if not replaced:
        rows.append(line)
    rows.sort(key=lambda r: r.split("|")[1].strip())

    # 跨年：主文件只留**最新年份**的行，更早年份搬去 _archive/
    # （按「现有行里最大的年份」判，而不是按本次写入的日期——补记旧日期不该触发归档）
    years = sorted({r.split("|")[1].strip()[:4] for r in rows})
    keep_year = years[-1] if years else day[:4]
    moved = [r for r in rows if not r.startswith("| " + keep_year)]
    rows = [r for r in rows if r.startswith("| " + keep_year)]

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    if moved:
        by_year = {}
        for r in moved:
            by_year.setdefault(r.split("|")[1].strip()[:4], []).append(r)
        for y, rs in by_year.items():
            ap = _p(ARCHIVE_DIR, "daily_log_%s.md" % y)
            old = []
            if os.path.exists(ap):
                with io.open(ap, "r", encoding="utf-8") as f:
                    old = [ln for ln in f.read().split("\n") if ln.strip()]
            oh, orows = split_rows(old)
            if not oh:
                oh = _head()
            merged = sorted(set(orows + rs), key=lambda r: r.split("|")[1].strip())
            with io.open(ap, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(oh + merged) + "\n")

    with io.open(LOG_PATH, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(head + rows) + "\n")
    return "replaced" if replaced else "appended"


def tail(n=30):
    """读末尾 n 行数据（给 AI 用；不要一次读全年）。"""
    if not os.path.exists(LOG_PATH):
        return []
    with io.open(LOG_PATH, "r", encoding="utf-8") as f:
        _, rows = split_rows([ln.strip() for ln in f.read().split("\n") if ln.strip()])
    return rows[-n:] if n else rows


def year_rows(year):
    """取某一年的全部数据行：当年读主文件，往年读归档文件。返回 [(columns, values)]。"""
    year = str(year)
    paths = [_p(WORK, "_archive", "daily_log_%s.md" % year)]
    if year == dt.date.today().isoformat()[:4]:
        paths.insert(0, LOG_PATH)
    out = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with io.open(path, "r", encoding="utf-8") as f:
            _, rows = split_rows([ln.strip() for ln in f.read().split("\n") if ln.strip()])
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if len(cells) == len(COLS):
                cols = COLS
            elif len(cells) == len(LEGACY_COLS):
                cols = LEGACY_COLS          # 2026-10-30 之前的 9 列老行，不丢历史
            else:
                continue
            if cells[0].startswith(year):
                out.append(dict(zip(cols, cells)))
    out.sort(key=lambda d: d.get("日期", ""))
    return out


def _fnum(x):
    """把格子里的数字抠出来；抠不出返回 None。"""
    try:
        return float(str(x).replace("%", "").replace("⚠", "").strip())
    except (TypeError, ValueError):
        return None


def year_stat(year):
    """把一年的 daily_log 压成年报底稿要的聚合数（纯统计，不做判断）。"""
    rows = year_rows(year)
    st = {"年份": str(year), "天数": len(rows), "首日": rows[0]["日期"] if rows else "-",
          "末日": rows[-1]["日期"] if rows else "-", "档位": {}, "决策日": 0,
          "巡检告警": 0, "涨停合计": 0, "跌停合计": 0, "存疑天数": 0,
          "逆回购净": [], "货基": [], "资产": [], "累计": [], "回撤": [],
          "pnl": [], "花费合计": 0.0, "正收益天数": 0}
    for d in rows:
        st["档位"][d.get("档位", "-")] = st["档位"].get(d.get("档位", "-"), 0) + 1
        if d.get("决策") not in (None, "未跑", "-"):
            st["决策日"] += 1
        hi = str(d.get("巡检(高/总)", "")).split("/")
        if len(hi) == 2 and hi[1] not in ("-", ""):
            try:
                st["巡检告警"] += int(float(hi[1]))
            except ValueError:
                pass
        emo = str(d.get("涨停/跌停", ""))
        if emo.endswith("⚠"):
            st["存疑天数"] += 1
        emo = emo.rstrip("⚠").split("/")
        if len(emo) == 2:
            try:
                st["涨停合计"] += int(float(emo[0]))
                st["跌停合计"] += int(float(emo[1]))
            except ValueError:
                pass
        # 利率：新行是一个格子 "1.00/1.09"，老行是两列
        if "现金净利率" in d:
            pair = str(d.get("现金净利率", "")).split("/")
            if len(pair) == 2:
                for k, v in zip(("逆回购净", "货基"), pair):
                    n = _fnum(v)
                    if n is not None:
                        st[k].append(n)
        else:
            for k in ("逆回购净", "货基"):
                n = _fnum(str(d.get(k, "")).replace("%", ""))
                if n is not None:
                    st[k].append(n)
        for key, col in (("资产", "总资产(万)"), ("累计", "累计%"),
                         ("回撤", "回撤%"), ("pnl", "当日盈亏")):
            n = _fnum(d.get(col))
            if n is not None:
                st[key].append(n)
        if (_fnum(d.get("当日盈亏")) or 0) > 0:
            st["正收益天数"] += 1
        n = _fnum(d.get("花费"))
        if n is not None:
            st["花费合计"] += n
    return st


def year_md(year):
    """年报底稿（markdown），只写数，不下结论。"""
    st = year_stat(year)
    rows = year_rows(year)
    tiers = " / ".join("%s %d 天" % (k, v) for k, v in
                       sorted(st["档位"].items(), key=lambda kv: -kv[1]))
    def _rng(k):
        v = st[k]
        return "%.2f%%–%.2f%%（均值 %.2f%%）" % (min(v), max(v), sum(v) / len(v)) if v else "-"
    lines = [
        "# 巡检年报底稿 · %s" % st["年份"],
        "",
        "- 覆盖：%s 天（%s → %s），其中决策日 %d 天" % (
            st["天数"], st["首日"], st["末日"], st["决策日"]),
        "- 仓位档位分布：%s" % (tiers or "-"),
    ]
    asset = st["资产"]
    if len(asset) >= 2 and asset[0]:
        ret = asset[-1] / asset[0] - 1.0
        lines.append("- 资产：%.2f 万 → %.2f 万（区间 %+.2f%%）" % (
            asset[0], asset[-1], ret * 100.0))
        try:
            days = (dt.date.fromisoformat(st["末日"]) - dt.date.fromisoformat(st["首日"])).days
        except ValueError:
            days = 0
        if days > 0:
            lines.append("- 年化（按区间天数折算）：%+.2f%%（%d 天）" % (
                ((1.0 + ret) ** (365.0 / days) - 1.0) * 100.0, days))
    if st["回撤"]:
        worst = min(st["回撤"])
        hit = [d.get("日期") for d in rows if _fnum(d.get("回撤%")) == worst]
        lines.append("- 最大回撤：%.2f%%（%s）" % (worst, hit[0] if hit else "-"))
    if st["累计"]:
        lines.append("- 表内最后一个累计值：%+.2f%%" % st["累计"][-1])
    if st["pnl"]:
        lines.append("- 正收益天数：%d / %d 天" % (st["正收益天数"], len(st["pnl"])))
    lines += [
        "- token 花费合计：%.2f 元（净收益 = 收益额 − 花费）" % st["花费合计"],
        "- 逆回购净利率区间：%s" % _rng("逆回购净"),
        "- 货基利率区间：%s" % _rng("货基"),
        "- 涨停/跌停合计：%d / %d 家（跌停池空返回的存疑天数 %d）" % (
            st["涨停合计"], st["跌停合计"], st["存疑天数"]),
        "- 巡检告警累计：%d 条" % st["巡检告警"],
        "",
        "> 这是聚合底稿。要事件明细：`out/patrol_<日期>.md`；要净值曲线："
        "`paper/state_*.json` 的 `nav`（每个工作日巡检写一条）。",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- 清理策略
# ---------------------------------------------------- 周汇总（决策日后整理）
def _row_dict(line: str) -> dict:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cols = COLS if len(cells) == len(COLS) else LEGACY_COLS
    return dict(zip(cols, cells))


def _log_rows() -> list:
    if not os.path.exists(LOG_PATH):
        return []
    with io.open(LOG_PATH, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]
    return [_row_dict(ln) for ln in split_rows(lines)[1]]


def week_range(start: str | None = None) -> tuple:
    """默认取「上一个完整工作周」周一到周五；显式传 start 时取该周一到周五。"""
    if start:
        s = dt.date.fromisoformat(start)
    else:
        s = dt.date.today() - dt.timedelta(days=1)
        while s.weekday() != 0:
            s -= dt.timedelta(days=1)
        s -= dt.timedelta(days=7)          # 默认上一周
    return s.isoformat(), (s + dt.timedelta(days=4)).isoformat()


def week_digest(start: str | None = None, end: str | None = None) -> dict:
    """把周一到周五的每日巡检压成一张周表：逐日净值/累计 + 告警 + 成交 + 利率。

    只读已落盘的 daily_log、账本 nav、patrol_<日期>.json，不联网、不重算行情。
    """
    start, end = (start, end) if (start and end) else week_range(start)
    rows = [r for r in _log_rows() if start <= (r.get("日期") or "") <= end]
    days = sorted({r["日期"] for r in rows if r.get("日期")})
    ledgers = {}
    for lg, cfg_ in (config.PAPER.get("ledgers") or {}).items():
        st = _load_json(cfg_.get("state_file") or "")
        cap = float(cfg_.get("capital") or 0.0)
        navs = sorted([n for n in (st.get("nav") or [])
                       if isinstance(n, dict) and n.get("date") and start <= n["date"] <= end],
                      key=lambda n: n["date"])
        prev = [n for n in (st.get("nav") or [])
                if isinstance(n, dict) and (n.get("date") or "") < start]
        base = float(prev[-1]["nav"]) if prev else cap
        peak, mdd = base, 0.0
        for n in navs:
            peak = max(peak, float(n["nav"]))
            mdd = min(mdd, float(n["nav"]) / peak - 1 if peak else 0.0)
        trades = [t for t in (st.get("trades") or [])
                  if start <= str(t.get("date") or "")[:10] <= end]
        ledgers[lg] = {
            "capital": cap, "base": base,
            "rows": [{"date": n["date"], "nav": float(n["nav"]),
                      "ret": (float(n["nav"]) / cap - 1) if cap else 0.0} for n in navs],
            "end_nav": float(navs[-1]["nav"]) if navs else base,
            "week_chg": (float(navs[-1]["nav"]) / base - 1) if (navs and base) else 0.0,
            "mdd": mdd, "n_trades": len(trades),
        }
    n_alert = n_high = 0
    for r in rows:
        cell = (r.get("巡检(高/总)") or "0/0").replace(" ", "")
        try:
            hi, tot = cell.split("/")
            n_high += int(hi)
            n_alert += int(tot)
        except (ValueError, TypeError):
            continue
    # 利率直接取 daily_log 的「现金净利率」列（形如 1.25/1.09，单位 %）——巡检当天就记下了
    rates = {}
    for r in rows:
        cell = (r.get("现金净利率") or "").replace(" ", "")
        if "/" not in cell:
            continue
        try:
            a, b = cell.split("/")[:2]
            rates[r["日期"]] = {"repo": float(a) / 100.0, "money_fund": float(b) / 100.0}
        except (ValueError, TypeError):
            continue
    return {"start": start, "end": end, "days": days, "ledgers": ledgers,
            "n_alert": n_alert, "n_high": n_high, "rates": rates,
            "n_rows": len(rows)}


def week_md(dig: dict) -> str:
    """周汇总的 markdown（读上一个决策周期的工作日）。"""
    L = ["# 巡检周报 %s ~ %s" % (dig["start"], dig["end"]), "",
         "> 由 `housekeeping.py --week` 生成（0 token）：只汇总已落盘的周一到周五巡检记录，不重算行情。",
         "> 逐日明细看 `out/patrol_<日期>.md`；要一年口径看 `--year`。", ""]
    lgs = dig.get("ledgers") or {}
    if not dig.get("days"):
        L.append("本周没有巡检记录（可能没跑巡检，或都落在别的周）。")
        return "\n".join(L) + "\n"
    head = ["日期"] + ["%s 净值" % lg for lg in lgs] + ["%s 累计" % lg for lg in lgs]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    by_day = {lg: {r["date"]: r for r in (v.get("rows") or [])} for lg, v in lgs.items()}
    for d in dig["days"]:
        cells = [d]
        for lg in lgs:
            r = by_day[lg].get(d)
            cells.append("%.4f" % (r["nav"] / lgs[lg]["capital"]) if r else "—")
        for lg in lgs:
            r = by_day[lg].get(d)
            cells.append("%+.2f%%" % (r["ret"] * 100) if r else "—")
        L.append("| " + " | ".join(cells) + " |")
    L.append("")
    L.append("**本周合计**")
    L.append("")
    for lg, v in lgs.items():
        L.append("- `%s`：周涨跌 %+.2f%%，周末净值 %.2f 元，本周最大回撤 %.2f%%，成交 %d 笔"
                 % (lg, v["week_chg"] * 100, v["end_nav"], v["mdd"] * 100, v["n_trades"]))
    L.append("- 巡检告警 %d 条（高 %d 条）；记录 %d 天"
             % (dig["n_alert"], dig["n_high"], dig["n_rows"]))
    rts = dig.get("rates") or {}
    if rts:
        ks = sorted(rts)
        a, b = rts[ks[0]], rts[ks[-1]]
        L.append("- 现金层利率（实测）：逆回购 %.2f%% → %.2f%%，货基 %.2f%% → %.2f%%"
                 % (a["repo"] * 100, b["repo"] * 100, a["money_fund"] * 100, b["money_fund"] * 100))
    else:
        L.append("- 现金层利率：本周没有实测记录（巡检没跑或取数失败）")
    return "\n".join(L) + "\n"


def write_week(start: str | None = None) -> dict:
    dig = week_digest(start)
    md = week_md(dig)
    path = _p(config.OUT_DIR, "patrol_weekly_%s.md" % dig["start"])
    os.makedirs(config.OUT_DIR, exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(md)
    dig["path"] = path
    return dig


def _walk_dated(root):
    """返回 {key: {date: path}}——key = 去掉 __日期 的那部分文件名。"""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            m = DATE_RE.search(fn)
            if not m:
                continue
            key = (dirpath, DATE_RE.sub("", fn))
            out.setdefault(key, {})[m.group(1)] = os.path.join(dirpath, fn)
    return out


def plan_prune(keep_last=2):
    """算出该删哪些（不删）。返回 (to_delete, stats)。"""
    keep_last = max(1, int(keep_last))
    stats = {"keys": 0, "files": 0, "keep": 0}
    to_delete = []
    for (dirpath, key), by_date in _walk_dated(config.CACHE_DIR).items():
        stats["keys"] += 1
        dates = sorted(by_date)
        stats["files"] += len(dates)
        stats["keep"] += min(keep_last, len(dates))
        for d in dates[:-keep_last] if len(dates) > keep_last else []:
            to_delete.append(by_date[d])
    return to_delete, stats


def prune_cache(keep_last=2, dry=False, guard=0.8):
    """按「同 key 只留最近 keep_last 个日期」清理缓存。返回统计 dict。"""
    to_delete, stats = plan_prune(keep_last)
    total = stats["files"]
    n = len(to_delete)
    if total and n > total * guard:
        return {"ok": False, "error": "拒删：要删除 %d/%d（>%.0f%%），疑似命名规则或路径异常"
                % (n, total, guard * 100), "deleted": 0, "freed_mb": 0.0}
    freed = 0
    done = 0
    for path in to_delete:
        try:
            sz = os.path.getsize(path)
        except OSError:
            continue
        if dry:
            freed += sz
            done += 1
            continue
        try:
            os.remove(path)
            freed += sz
            done += 1
        except OSError:
            pass
    return {"ok": True, "deleted": done, "candidates": n, "freed_mb": round(freed / 1048576.0, 1),
            "keep_last": keep_last, "keys": stats["keys"]}


def prune_patrol_md(keep_days=60, today=None, dry=False):
    """删掉 out/patrol_<date>.md 里超过 keep_days 天的（结论已进 daily_log）。"""
    today = today or dt.date.today().isoformat()
    t = dt.date.fromisoformat(today)
    gone = 0
    for fn in sorted(os.listdir(config.OUT_DIR)) if os.path.isdir(config.OUT_DIR) else []:
        m = MD_RE.match(fn)
        if not m:
            continue
        try:
            age = (t - dt.date.fromisoformat(m.group(1))).days
        except ValueError:
            continue
        if age <= keep_days:
            continue
        p = _p(config.OUT_DIR, fn)
        if not dry:
            try:
                os.remove(p)
            except OSError:
                continue
        gone += 1
    return {"deleted": gone, "keep_days": keep_days, "today": today}


# 报告文件（out/paper 根目录）：文件名里带日期的那几类
_PAPER_DATE_RE = re.compile(r"^(?:weekly|orders|report|monthly|monthly_report)[_A-Za-z0-9]*?"
                            r"(\d{4}-\d{2}-\d{2})\.(?:md|html|csv)$")


def prune_paper_reports(keep_days=None, today=None, dry=False):
    """报告保留：按文件名日期保留周报/HTML/订单/月报 `keep_days` 天（默认 400 天，年报后再统一删）。

    - 只动 `config.PAPER["package_dir"]`（out/paper）**根目录**下带日期的报告文件；
    - `_history/`（同数据日重跑前的旧版备份）与 `decision_*.json`（期次快照）**永不删**；
    - 每一类文件的最新一份**永不删**（哪怕超过 keep_days，防止日期解析/时钟异常把近期报告清空）。
    """
    today = today or dt.date.today().isoformat()
    t = dt.date.fromisoformat(today)
    keep_days = int(keep_days if keep_days is not None
                    else (getattr(config, "PAPER", {}) or {}).get("paper_keep_days", 400))
    base = (getattr(config, "PAPER", {}) or {}).get("package_dir") or ""
    if not base or not os.path.isdir(base):
        return {"deleted": 0, "keep_days": keep_days, "today": today}
    cands = []          # [(age, fn, path)]
    newest = {}         # kind -> (age, fn) 最新一份（永不删）
    for fn in sorted(os.listdir(base)):
        p = _p(base, fn)
        if not os.path.isfile(p):
            continue
        m = _PAPER_DATE_RE.match(fn)
        if not m:
            continue
        try:
            age = (t - dt.date.fromisoformat(m.group(1))).days
        except ValueError:
            continue
        kind = fn.replace(m.group(1), "")      # weekly_.md / report_.html / orders_.csv …
        cands.append((age, fn, p))
        if kind not in newest or age < newest[kind][0]:
            newest[kind] = (age, fn)
    keep_names = {fn for _, fn in newest.values()}
    gone = 0
    for age, fn, p in cands:
        if age <= keep_days or fn in keep_names:
            continue
        if not dry:
            try:
                os.remove(p)
            except OSError:
                continue
        gone += 1
    return {"deleted": gone, "keep_days": keep_days, "today": today}


# stock/data/archive/<YYYY-MM-DD>/ —— 每日快照目录（人工留档，非仓库产物）
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def archive_root():
    """每日快照目录的根：`<工作目录的父级>/archive`（技能里即 stock/data/archive）。"""
    return os.path.join(os.path.dirname(os.path.abspath(WORK)), "archive")


def plan_archive_prune(keep_days=14, today=None):
    """算出该删哪些每日快照目录（不删）。返回 (to_delete, stats)。

    只认**纯日期**目录名（YYYY-MM-DD）；名字带 `_pre_fresh_start`/`pre_reset` 等
    后缀的回滚目录与 `_*` 一律不动；最新一份永远保留（防时钟异常把近期快照清空）。
    """
    keep_days = max(0, int(keep_days))
    today = today or dt.date.today().isoformat()
    t = dt.date.fromisoformat(today)
    root = archive_root()
    cands = []
    if os.path.isdir(root):
        for fn in sorted(os.listdir(root)):
            p = _p(root, fn)
            if not os.path.isdir(p) or not _DATE_DIR_RE.match(fn):
                continue
            try:
                age = (t - dt.date.fromisoformat(fn)).days
            except ValueError:
                continue
            cands.append((age, p))
    stats = {"dirs": len(cands), "keep_days": keep_days, "today": today}
    if not cands:
        stats["keep"] = 0
        return [], stats
    newest = min(cands, key=lambda x: x[0])[1]
    to_delete = [p for age, p in cands if age > keep_days and p != newest]
    stats["keep"] = len(cands) - len(to_delete)
    return to_delete, stats


def prune_archive(keep_days=None, today=None, dry=False, guard=0.8):
    """按保留天数删 `stock/data/archive/<YYYY-MM-DD>/` 每日快照目录。返回统计 dict。"""
    cfg = getattr(config, "PATROL", {}) or {}
    keep_days = int(keep_days if keep_days is not None
                    else cfg.get("archive_keep_days", 14))
    to_delete, stats = plan_archive_prune(keep_days, today=today)
    total = stats["dirs"]
    n = len(to_delete)
    if total and n > total * guard:
        return {"ok": False, "error": "拒删：要删除 %d/%d 个目录（>%.0f%%），疑似日期解析或路径异常"
                % (n, total, guard * 100), "deleted": 0, "freed_mb": 0.0}
    freed = 0
    done = 0
    for path in to_delete:
        sz = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _d, fs in os.walk(path) for f in fs)
        if dry:
            freed += sz
            done += 1
            continue
        try:
            shutil.rmtree(path)
            freed += sz
            done += 1
        except OSError:
            pass
    return {"ok": True, "deleted": done, "candidates": n,
            "freed_mb": round(freed / 1048576.0, 1),
            "keep_days": keep_days, "kept_dirs": stats["keep"]}


def sweep(keep_last=None, md_keep_days=None, today=None, dry=False, paper_keep_days=None,
          archive_keep_days=None):
    """巡检末尾一次做完：记账 + 清缓存 + 清旧巡检 md + 清超期报告 + 清旧每日快照。"""
    cfg = getattr(config, "PATROL", {}) or {}
    keep_last = int(keep_last if keep_last is not None else cfg.get("cache_keep_days", 2))
    md_keep_days = int(md_keep_days if md_keep_days is not None else cfg.get("patrol_md_keep_days", 365))
    return {"cache": prune_cache(keep_last, dry=dry),
            "patrol_md": prune_patrol_md(md_keep_days, today=today, dry=dry),
            "paper": prune_paper_reports(paper_keep_days, today=today, dry=dry),
            "archive": prune_archive(archive_keep_days, today=today, dry=dry)}


# ------------------------------------------------------------------ CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="housekeeping.py",
        description="每日巡检的「记账」与「清场」：daily_log 一天一行 + 缓存滚动清理。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="打印 daily_log 末尾 N 行")
    g.add_argument("--prune", action="store_true", help="单独跑清理（缓存 + 旧巡检 md + 超期报告 + 旧每日快照）")
    g.add_argument("--week", metavar="YYYY-MM-DD", nargs="?", const="auto",
                   help="周汇总：读出周一到周五的巡检记录（默认上一个完整工作周），写 out/patrol_weekly_<周一>.md")
    g.add_argument("--year", metavar="YYYY", help="年报底稿（资产区间/年化/回撤/正收益天数/token）")
    g.add_argument("--append", metavar="YYYY-MM-DD", help="用现有 patrol_latest.json 补记一天")
    p.add_argument("--n", type=int, default=30, help="--show 的行数（默认 30）")
    p.add_argument("--dry", action="store_true", help="--prune 只报不删")
    p.add_argument("--json", action="store_true", help="--year 输出 JSON")
    return p


def main(argv=None):
    p = build_parser()
    if not (list(sys.argv[1:] if argv is None else argv)):
        print(__doc__)
        return 1
    a = p.parse_args(sys.argv[1:] if argv is None else argv)

    if a.show:
        rows = tail(a.n)
        print("\n".join(_head() + rows) if rows else "（还没有记录：巡检跑一次就有）")
        return 0

    if a.prune:
        r = sweep(dry=a.dry)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0

    if a.year:
        if not re.match(r"^\d{4}$", a.year):
            p.error("--year 要 4 位年份，例如 2026")
        if a.json:
            print(json.dumps(year_stat(a.year), ensure_ascii=False, indent=1))
        else:
            print(year_md(a.year))
        return 0

    if a.week:
        st = None if a.week == "auto" else a.week
        if st and not re.match(r"^\d{4}-\d{2}-\d{2}$", st):
            p.error("--week 要 YYYY-MM-DD（该周周一），或直接 --week 取上一个完整工作周")
        dig = write_week(st)
        print(week_md(dig) if not a.json else json.dumps(dig, ensure_ascii=False, indent=1))
        print("已写 %s" % dig["path"], file=sys.stderr)
        return 0

    if a.append:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", a.append):
            p.error("--append 要 YYYY-MM-DD，例如 2026-10-30")
        res = _load_json(_p(config.OUT_DIR, "patrol_latest.json"))
        row = build_row(res, a.append)
        act = append(row, a.append)
        print("%s %s" % (act, _row_line(row)))
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
