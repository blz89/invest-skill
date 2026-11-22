# -*- coding: utf-8 -*-
"""系统体检（read-only）：每次决策前先跑一遍，回答「现在能不能跑、跑出来会不会是错的」。

为什么要有它：这套系统是无人值守的（每周六 20:00 决策、交易日 19:00 由 Windows 计划任务统一取数、20:00 DSH 巡检会话算账本；决策日不取数），
最大的风险不是模型算错，而是**静默失败**——账本被手改过、out/ 是别的本金生成的、
数据没更新、挂单和持仓对不上。这些问题不会让脚本报错，只会让账本悄悄记错钱。

检查项（全部只读，不改任何文件）：
  A. 运行环境：Python 版本、取数接口层/pandas 是否可用、工作目录可写；
  B. 账本结构：必需键、现金非负、整手、挂单与持仓口径、订单 id 唯一；
  B2. 基准口径：nav 首点日期==baseline_date、首点 bench_nav≈本金、baseline_bench==当日指数收盘
      （「外部进程把 50k 账本写成本金不符的订单 + 错误基准」事故后加，静默记错钱必须拦）；
  C. 决策一致性：out/stage3_plan.json 的本金/日期是否与账本匹配（防串账）；
  D. 数据新鲜度：最近交易日缓存文件数（最近交易日没取到数 → 决策会用到旧数据；决策日不取数）；
  E. 账本摘要：净值/现金/持仓/挂单/最近结算日；
  F. 利率时效：现金层假设利率与验收基准（1 年期国债）和实测值是否已偏离到「过期」（只 WARN）。
  G. 决策新鲜度：out/paper/decision*_latest.json 是否为最近一个交易日（超过 8 个自然日 = 漏跑，WARN）；
  H. 持仓等级：持仓必须 ⊆ R2/R3 且加权等级在区间内（R4/R5 = ERROR）；
  I. 决策数据日期：out/stage3_plan.json 的 data_as_of 是否等于最近交易日（落后：周六决策日 = ERROR，其它日 = WARN；字段缺失跳过）。

用法：
  python scripts/doctor.py                  # 体检全部账本（本部署只有 50k）
  python scripts/doctor.py --ledger 50k     # 只体检一个
  python scripts/doctor.py --json           # 机器可读输出
  python scripts/doctor.py --selftest       # 附带跑两套离线自检（约 5 秒）

退出码：0=GO（可跑）；1=NO-GO（有 ERROR，先修再跑）；2=参数错误。
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import paper

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_OUT = config.PAPER["package_dir"]
CONFIG_PAPER = config.PAPER
CONFIG_CAPITAL_MAX = config.CAPITAL["max"]
TODAY = dt.date.today().isoformat()


def _trading_days_upto(base):
    """本地日历缓存里 ≤ base 的交易日（升序）；没有缓存返回 None。"""
    try:
        caldir = os.path.join(config.CACHE_DIR, "calendar")
        names = sorted(f for f in os.listdir(caldir)
                       if f.startswith("trade__") and f.endswith(".csv"))
        days = set()
        for fn in names[-3:]:
            with open(os.path.join(caldir, fn), "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()[:10]
                    if len(s) == 10 and s[:4].isdigit() and s <= base:
                        days.add(s)
        return sorted(days) or None
    except OSError:
        return None


def _weekday_back(day_iso):
    """无日历缓存时的兜底：≤ day 的最近工作日。"""
    cur = dt.date.fromisoformat(day_iso)
    for _ in range(8):
        if cur.weekday() < 5:
            return cur.isoformat()
        cur -= dt.timedelta(days=1)
    return day_iso


def last_trading_day(day=None):
    """数据意义上「最近已收盘/已取数」的交易日（只读本地日历缓存，绝不联网）。

    本部署取数在交易日 19:00 由本机任务（`stock prefetch`）统一完成、决策日（周六）不取数，所以
    「数据是否新鲜」要按最近交易日算，而不是按今天算。日历缓存在
    <CACHE_DIR>/calendar/trade__*.csv（列 trade_date）；没有缓存时退化为跳过周末
    （节假日以缓存为准）。

    今天是交易日、但还没到本机取数完成时间（取数 19:00 启动，代码按 19:30 保守判就绪）时，返回上一个交易日——
    否则周一凌晨会把「周五收盘」误判成「落后」。
    """
    base = str(day or TODAY)[:10]
    days = _trading_days_upto(base)
    if days is None:
        days = [_weekday_back(base)]
    ltd = days[-1]
    if day is None and ltd == TODAY:
        now = dt.datetime.now()
        if (now.hour, now.minute) < (19, 30):
            prev = [d for d in days if d < ltd]
            ltd = prev[-1] if prev else ltd
    return ltd

STATE_KEYS = ["meta", "cash", "holdings", "trades", "orders", "nav", "events"]
META_KEYS = ["version", "mode", "capital", "tier", "created_at", "baseline_date",
             "baseline_bench", "bench_index"]


class Report:
    """收集检查结果：ERROR 会拦下这次决策，WARN 只提醒。"""

    def __init__(self):
        self.items = []   # (level, area, name, ok, detail)

    def add(self, level, area, name, ok, detail=""):
        self.items.append({"level": level, "area": area, "name": name,
                           "ok": bool(ok), "detail": detail})

    def ok(self, area, name, detail=""):
        self.add("INFO", area, name, True, detail)

    def warn(self, area, name, detail=""):
        self.add("WARN", area, name, False, detail)

    def error(self, area, name, detail=""):
        self.add("ERROR", area, name, False, detail)

    def count(self, level=None, failed_only=False):
        rows = self.items
        if level:
            rows = [r for r in rows if r["level"] == level]
        if failed_only:
            rows = [r for r in rows if not r["ok"]]
        return len(rows)

    @property
    def verdict(self):
        if self.count("ERROR"):
            return "NO-GO"
        return "GO"


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _bench_close_cached(index_code, date_str):
    """从本地缓存取基准指数某日收盘；没有缓存/没有该日期返回 None（不联网、不改文件）。

    为什么读缓存而不调 datasource.fetch_index_daily：doctor 可能在决策日之外的任意时刻被
    人工拉起来排查，绝不能因为核对一个历史基准点就联网取数（也就不会污染当天缓存）。
    """
    import glob

    pat = os.path.join(config.CACHE_DIR, "index_daily", f"{index_code}__*.csv")
    files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    try:
        import pandas as pd
    except ImportError:
        return None
    for path in files:
        try:
            df = pd.read_csv(path, dtype=str)
        except Exception:
            continue
        if "date" not in df.columns or "close" not in df.columns:
            continue
        d = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        hit = df.loc[d == str(date_str), "close"]
        if len(hit):
            try:
                return float(hit.iloc[0])
            except (TypeError, ValueError):
                return None
    return None


def nav_baseline_problems(state):
    """「基准口径」一致性：nav 首点必须落在 baseline_date、首点 bench_nav 必须≈本金。

    2026-10-31 13:53 事故回归：外部进程把 `state_50k.json` 写成「14 张本金不符的订单 +
    baseline_bench=10-27 收盘 + nav 只剩一个 10-30 点」，而 doctor 当时仍报 GO——
    原检查只看「nav 非空/升序/不超前」，抓不到「基准与首点被换掉」这类静默记错钱。
    """
    meta = state.get("meta") or {}
    nav = state.get("nav") or []
    try:
        cap = float(meta.get("capital"))
    except (TypeError, ValueError):
        return []
    bd = str(meta.get("baseline_date") or "")
    if not nav or cap <= 0 or not bd:
        return []
    out = []
    first = nav[0]
    d0 = str(first.get("date") or "")
    if d0 != bd:
        out.append(f"nav 首点 {d0 or '缺失'} ≠ baseline_date {bd}")
    try:
        bn = float(first.get("bench_nav"))
    except (TypeError, ValueError):
        bn = None
    if bn is None or abs(bn - cap) > max(1.0, cap * 0.001):
        out.append(f"nav 首点 bench_nav {first.get('bench_nav')!r} ≠ 本金 {cap:,.0f}")
    return out


def bench_baseline_problems(state):
    """baseline_bench 必须等于 baseline_date 当日基准指数收盘（缓存可用时）。

    返回 (problems, checked)：checked=False 表示本地缓存查不到该指数/日期，
    此时只提示「跳过」、不拦决策（非决策日没取过数属正常；本部署取数在交易日 19:00 由本机任务（`stock prefetch`）统一完成，决策日不再取数）。
    """
    meta = state.get("meta") or {}
    bd = str(meta.get("baseline_date") or "")
    idx = str(meta.get("bench_index") or "000300")
    try:
        bb = float(meta.get("baseline_bench"))
    except (TypeError, ValueError):
        return (["baseline_bench 缺失或不是数字"], False)
    close = _bench_close_cached(idx, bd)
    if close is None:
        return ([], False)
    if abs(bb - close) > max(0.01, close * 0.005):
        return ([f"baseline_bench {bb:.3f} ≠ {bd} {idx} 收盘 {close:.3f}"], True)
    return ([], True)


def check_env(rep):
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        rep.ok("环境", "Python ≥3.10", f"{v.major}.{v.minor}.{v.micro}")
    else:
        rep.error("环境", "Python ≥3.10", f"当前 {v.major}.{v.minor}.{v.micro}，脚本用了 3.10+ 语法")
    try:
        from stock import endpoints
        rep.ok("环境", "取数接口层可用（stock 自带）", getattr(endpoints, "__version__", "?"))
    except Exception as e:                                     # pragma: no cover - 环境相关
        rep.error("环境", "取数接口层可用（stock 自带）", f"import 失败：{e}；确认 stock/ 在 sys.path（技能 config.py 会自动加）")
    try:
        import pandas  # noqa: F401
        rep.ok("环境", "pandas 可用", getattr(pandas, "__version__", "?"))
    except Exception as e:                                     # pragma: no cover
        rep.error("环境", "pandas 可用", f"import 失败：{e}")
    for label, path in (("工作目录", config.WORK_DIR), ("缓存目录", config.CACHE_DIR),
                        ("产出目录", config.OUT_DIR), ("周报目录", PAPER_OUT)):
        if os.path.isdir(path):
            rep.ok("环境", f"{label}存在", path)
        else:
            rep.warn("环境", f"{label}不存在", f"{path}（首次运行会自动建）")
    # 可写性：只在产出目录试写临时文件，失败说明 20:00 会写不出周报
    try:
        probe = os.path.join(config.OUT_DIR, ".doctor_probe")
        os.makedirs(config.OUT_DIR, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        rep.ok("环境", "产出目录可写")
    except Exception as e:
        rep.error("环境", "产出目录可写", f"{e}")


def check_ledger(rep, name, cfg):
    """账本结构 + 口径检查。返回 (state, close_map 用的持仓成本快照)。"""
    path = cfg["state_file"]
    capital = float(cfg["capital"])
    if not os.path.exists(path):
        rep.warn("账本", f"{name} 账本不存在", f"{path}（首次决策会新建）")
        return None
    try:
        st = _load_json(path)
    except Exception as e:
        rep.error("账本", f"{name} 账本可解析", f"{path}：{e}")
        return None
    missing = [k for k in STATE_KEYS if k not in st]
    if missing:
        rep.error("账本", f"{name} 结构完整", f"缺键 {missing}")
        return st
    rep.ok("账本", f"{name} 结构完整")
    meta = st.get("meta") or {}
    missing_meta = [k for k in META_KEYS if k not in meta]
    if missing_meta:
        rep.error("账本", f"{name} meta 完整", f"缺键 {missing_meta}")
    else:
        rep.ok("账本", f"{name} meta 完整", f"本金 {float(meta['capital']):,.0f} 元 / {meta['tier']} 档")
    if abs(float(meta.get("capital") or 0) - capital) > 0.01:
        rep.error("账本", f"{name} 本金与配置一致",
                  f"账本 {float(meta.get('capital') or 0):,.0f} vs 配置 {capital:,.0f}")
    cash = float(st.get("cash") or 0)
    if cash < -0.01:
        rep.error("账本", f"{name} 现金非负", f"现金 {cash:,.2f}")
    else:
        rep.ok("账本", f"{name} 现金非负", f"{cash:,.2f} 元")

    # 持仓：股数为整手、成本为正
    bad_hold = []
    for code, h in (st.get("holdings") or {}).items():
        sh = int(h.get("shares") or 0)
        if sh <= 0 or sh % 100 != 0:
            bad_hold.append(f"{code} 股数 {sh}")
        if float(h.get("cost_basis") or 0) <= 0:
            bad_hold.append(f"{code} 成本 {h.get('cost_basis')}")
    if bad_hold:
        rep.error("账本", f"{name} 持仓口径", "；".join(bad_hold[:5]))
    else:
        rep.ok("账本", f"{name} 持仓口径", f"{len(st.get('holdings') or {})} 只")

    # 挂单：id 唯一、买单有 effective_from、卖单不超持仓、日期格式可解析
    orders = st.get("orders") or []
    ids = [o.get("id") for o in orders]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        rep.error("账本", f"{name} 订单 id 唯一", f"重复 {sorted(dup)[:5]}")
    else:
        rep.ok("账本", f"{name} 订单 id 唯一", f"{len(orders)} 张")
    problems = []
    for o in orders:
        if o.get("status") != "pending":
            continue
        # 冻结的括号止损（bracket=True 且 active=False）随父买单挂单、成交才激活，
        # 父单未成交时不占持仓，不参与「卖单不超持仓」校验（2026-11-15 修：
        # 曾把 sh510050 冻结 bracket 900 股当成有效卖单报 ERROR，doctor NO-GO）。
        if o.get("bracket") and not o.get("active", True):
            continue
        code = o.get("code")
        try:
            dt.date.fromisoformat(str(o.get("effective_from")))
        except Exception:
            problems.append(f"{code} effective_from={o.get('effective_from')!r}")
        if o.get("action") == "sell":
            have = int((st.get("holdings") or {}).get(code, {}).get("shares") or 0)
            if int(o.get("shares") or 0) > have:
                problems.append(f"{code} 卖 {o.get('shares')} > 持仓 {have}")
    if problems:
        rep.error("账本", f"{name} 挂单口径", "；".join(problems[:5]))
    else:
        rep.ok("账本", f"{name} 挂单口径", f"待成交 {len(paper.pending_orders(st))} 张")

    # 净值序列：按日期升序、最后一条不晚于今天、回撤非正
    nav = st.get("nav") or []
    if not nav:
        rep.warn("账本", f"{name} 净值序列", "为空（还没结算过）")
    else:
        dates = [str(n.get("date")) for n in nav]
        if dates != sorted(dates):
            rep.error("账本", f"{name} 净值升序", "日期乱序")
        elif dates[-1] > TODAY:
            rep.error("账本", f"{name} 净值不超前", f"最后一条 {dates[-1]} 晚于今天 {TODAY}")
        else:
            rep.ok("账本", f"{name} 净值序列", f"{len(nav)} 个结算点，最后 {dates[-1]}")
        if float(nav[-1].get("drawdown") or 0) > 0.0001:
            rep.warn("账本", f"{name} 回撤口径", f"回撤为正 {nav[-1]['drawdown']}（应为 0 或负）")

    # 基准口径（2026-10-31 账本污染事故回归）：首点日期/首点基准值；baseline_bench 对当日指数收盘
    base_bad = nav_baseline_problems(st)
    if base_bad:
        rep.error("账本", f"{name} 基准与净值一致", "；".join(base_bad))
    else:
        rep.ok("账本", f"{name} 基准与净值一致",
               f"首点 {str(nav[0].get('date'))} / 基准 {float(meta.get('baseline_bench') or 0):.3f}" if nav else "无净值点")
    bench_bad, bench_checked = bench_baseline_problems(st)
    if bench_bad:
        rep.error("账本", f"{name} 基准收盘可核对", "；".join(bench_bad))
    elif bench_checked:
        rep.ok("账本", f"{name} 基准收盘可核对", f"{meta.get('baseline_date')} 收盘 ✓")
    else:
        rep.ok("账本", f"{name} 基准收盘可核对", "本地无该指数缓存，跳过（非决策日正常）")

    # 可投资本（利润再投资）：不得超上限、不得超当前净值、负数说明口径错
    try:
        inv = paper.investable_capital(st)
    except Exception as e:
        rep.warn("账本", f"{name} 可投资本", f"算不出：{e}")
        inv = None
    if inv:
        nav_now = float(nav[-1].get("nav")) if nav else float(st.get("cash") or 0)
        if inv["investable"] > float(CONFIG_CAPITAL_MAX) + 0.01:
            rep.error("账本", f"{name} 可投资本不超上限",
                      f"{inv['investable']:,.0f} > {float(CONFIG_CAPITAL_MAX):,.0f}")
        gap = inv["investable"] - nav_now
        if gap > 0.2 * max(inv["base"], 1.0):
            rep.error("账本", f"{name} 可投资本 ≤ 净值",
                      f"可投资本 {inv['investable']:,.0f} 比净值 {nav_now:,.2f} 高 {gap:,.0f} 元（账本与利润口径不一致）")
        elif gap > 1.0:
            # 有已实现盈利、但持仓浮亏/费用大于它时，可投资本天然略高于净值——正常口径，不告警
            rep.ok("账本", f"{name} 可投资本 vs 净值",
                   f"可投资本 {inv['investable']:,.0f} 高于净值 {nav_now:,.2f} 共 {gap:,.0f} 元"
                   "（本金 + 已实现盈利 vs 含未实现盈亏/费用，正常）")
        else:
            rep.ok("账本", f"{name} 可投资本", (f"{inv['investable']:,.0f} 元"
                                               f"（本金 {inv['base']:,.0f}"
                                               f" + 已实现盈利 {inv['gain']:+,.0f}）"
                                               + ("，已达可投资本上限" if inv["clamped"] else "")))

    # 备份：无人值守下最贵的错误是不可恢复——没有备份只提醒，不拦决策
    try:
        bks = paper.list_backups(path)
    except Exception as e:
        bks = []
        rep.warn("账本", f"{name} 备份可读", f"{e}")
    if not bks:
        rep.warn("账本", f"{name} 有可回滚备份", "还没有（首次决策会自动生成）")
    else:
        latest = os.path.basename(bks[-1])
        keep = int((CONFIG_PAPER.get("backup_keep") or 30))
        keep_days = int((CONFIG_PAPER.get("backup_keep_days") or 7))
        if len(bks) > keep:
            rep.warn("账本", f"{name} 备份轮转", f"{len(bks)} 份 > 上限 {keep}")
        else:
            rep.ok("账本", f"{name} 有可回滚备份",
                   f"{len(bks)} 份（保留 {keep} 份 / 最近 {keep_days} 天，周决策时删上一周整批），最近 {latest}")
    return st


def check_plan(rep, name, cfg, state, ledgers=None):
    """out/stage3_plan.json 与账本是否属于同一次决策（防「用别的本金的组合改本账本」）。

    各账本共用一份 out/，所以「本金不匹配」本身不是错——错的是拿 A 账本的组合去改 B 账本。
    因此这里只做区分：out/ 属于另一个账本 → INFO 提示决策日要重跑管线；谁都不属于 → WARN。
    """
    path = os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(path):
        rep.warn("决策", "阶段三产物", f"{path} 还没有（首次决策会生成；今天跑过管线后就会有）")
        return
    try:
        plan = _load_json(path)
    except Exception as e:
        rep.error("决策", "阶段三产物可解析", f"{e}")
        return
    pcap = float(plan.get("capital") or 0)
    capital = float(cfg["capital"])
    match = abs(pcap - capital) <= 0.01
    if match:
        rep.ok("决策", f"{name} 本金匹配", f"{pcap:,.0f} 元")
    else:
        siblings = {k: float(v.get("capital") or 0) for k, v in (ledgers or {}).items()
                    if k != name}
        owner = [k for k, c in siblings.items() if abs(pcap - c) <= 0.01]
        who = f"（{owner[0]} 账本）" if owner else "（不属于任何已配置账本）"
        detail = (f"out/ 目前是 {pcap:,.0f} 元本金{who}生成的；"
                  f"{name} 决策日要先跑 run.py --capital {capital:,.0f}")
        if owner:
            rep.ok("决策", f"{name} 本金匹配", detail)
        else:
            rep.warn("决策", f"{name} 本金匹配", detail)
    ltd = last_trading_day()
    gen = str(plan.get("generated_at") or "")[:10]
    if gen and gen < ltd:
        rep.warn("决策", f"{name} 产物新鲜度",
                 f"generated_at={plan.get('generated_at')}，早于最近交易日 {ltd}（决策日不取数，先在工作日补跑）")
    elif gen:
        rep.ok("决策", f"{name} 产物新鲜度", plan.get("generated_at"))
    dao = str(plan.get("data_as_of") or "")[:10]
    if not dao:
        pass  # 旧产物没有这个字段：跳过，不误报
    elif dao > ltd:
        rep.warn("决策", f"{name} 数据日期",
                 f"data_as_of={dao} 晚于最近交易日 {ltd}（时钟/数据异常）")
    elif dao < ltd:
        det = (f"data_as_of={dao}，落后最近交易日 {ltd}"
               f"（本机交易日 19:00 统一取数失败/未更新；决策日不取数）")
        if dt.date.fromisoformat(TODAY).weekday() == 5:
            rep.error("决策", f"{name} 数据日期", det + "；周六决策日视为异常（NO-GO）")
        else:
            rep.warn("决策", f"{name} 数据日期", det)
    else:
        rep.ok("决策", f"{name} 数据日期", f"data_as_of={dao}（最近交易日）")
    tier = plan.get("tier") or {}
    if match and state is not None:
        rep.ok("决策", f"{name} 市场状态",
               f"{((plan.get('market_regime') or {}).get('regime'))} / "
               f"cap {float(tier.get('equity_amount') or 0):,.0f} 元 / "
               f"模式 {cfg.get('rebalance_mode') or config.PAPER.get('rebalance_mode')}")


def check_freshness(rep, name, pout=None):
    """决策新鲜度：上次决策快照是哪一天？（无人值守下「漏跑一次」不会报错，只会静默拖期）

    `out/paper/decision*_latest.json` 由 weekly.py 每期写一次，as_of 就是决策日。
    每周六一次，所以超过 8 个自然日没更新，说明至少漏了一期——只 WARN，不拦决策。
    阈值 8 = 最长正常间隔 7 天 + 1 天缓冲（决策当晚 doctor 会看到「7 天前」的快照）。
    """
    pout = pout or PAPER_OUT
    suffix = "" if name == config.PAPER.get("default_ledger", "50k") else f"_{name}"
    path = os.path.join(pout, f"decision{suffix}_latest.json")
    if not os.path.exists(path):
        rep.warn("决策", f"{name} 决策新鲜度", "还没有决策快照（首次决策后生成）")
        return
    try:
        snap = _load_json(path)
        as_of = str(snap.get("as_of") or "")
        d0 = dt.date.fromisoformat(as_of)
    except Exception as e:  # noqa: BLE001
        rep.warn("决策", f"{name} 决策新鲜度", f"快照可解析：{e}")
        return
    if as_of > TODAY:
        rep.warn("决策", f"{name} 决策新鲜度", f"快照 as_of={as_of} 在未来（时钟/数据异常）")
        return
    age = (dt.date.fromisoformat(TODAY) - d0).days
    if age > 8:
        rep.warn("决策", f"{name} 决策新鲜度",
                 f"上次决策 {as_of}，已过 {age} 天（每周六应有一次，可能漏跑）")
    else:
        rep.ok("决策", f"{name} 决策新鲜度", f"上次决策 {as_of}（{age} 天前）")


def check_risk(rep, name, state):
    """风险等级体检：持仓等级必须 ⊆ R2/R3、加权等级在 [min_level, max_level] 内、R4/R5 持仓为 0。"""
    try:
        import risk
    except Exception as e:  # noqa: BLE001
        rep.warn("风险", f"{name} 风险等级", f"risk 模块不可用：{e}")
        return
    holdings = state.get("holdings") or {}
    if not holdings:
        rep.ok("风险", f"{name} 风险等级", "空仓（现金层视为 R1），无持仓等级需要检查")
        return
    nav = 0.0
    if state.get("nav"):
        nav = float(state["nav"][-1].get("nav") or 0.0)
    items, bad = [], []
    for code, h in holdings.items():
        lv = str(h.get("level") or "")
        if not risk.level_num(lv):
            bad.append(f"{code} 没有等级")
            continue
        if not risk.included(lv):
            bad.append(f"{code} {h.get('name')} 是 {lv}")
        px = float(h.get("last_close") or h.get("cost") or 0.0)
        amt = float(h.get("shares") or 0) * px
        w = (amt / nav) if nav else 0.0
        items.append({"code": code, "name": h.get("name"), "level": lv, "weight": round(w, 6)})
    rv = risk.portfolio_review(items) if items else {}
    n_hold = len(items)
    # 场外 R2 影子账也要计入组合评审：review.py 的记分卡是「逐只基金」计入的，
    # doctor 若只看场内持仓，R2 占比会显示 0%，与记分卡（33%）互相矛盾，
    # 也会把「R2 层空着」这种真问题藏起来（2026-11-21 审计）。
    try:
        import otc_book
        for _c, _h in (state.get(otc_book.HOLD_KEY) or {}).items():
            _mv = otc_book.mv_of(_h)
            if _mv <= 0 or not nav:
                continue
            items.append({"code": _c, "name": _h.get("name"),
                          "level": _h.get("level") or "R2",
                          "weight": round(_mv / nav, 6)})
        if items:
            rv = risk.portfolio_review(items)
    except Exception as exc:  # noqa: BLE001 —— 场外腿读不出来只提示，不影响其余体检
        rep.warn("风险", f"{name} 场外 R2 计入", f"场外影子账不可用：{exc}")
    if bad:
        rep.error("风险", f"{name} 持仓等级", "不在 R2/R3：" + "；".join(bad[:6]))
    else:
        rep.ok("风险", f"{name} 持仓等级", f"{n_hold} 只场内持仓全部在 R2/R3"
                                    f"（另有场外 R2 在场外影子账，已计入组合评审）")
    if rv:
        lo, hi = rv["range"]
        line = (f"加权 R{float(rv.get('level') or 0):.2f}（允许 R{lo:.1f}–R{hi:.1f}）"
                f"｜R2 占比 {float(rv.get('r2_share') or 0):.1%}")
        if rv.get("violations"):
            rep.error("风险", f"{name} 组合等级", line + "｜违规：" + "；".join(rv["violations"][:4]))
        elif float(rv.get("level") or 0) > hi or float(rv.get("level") or 0) < lo:
            rep.warn("风险", f"{name} 组合等级", line + "（超出允许区间）")
        else:
            rep.ok("风险", f"{name} 组合等级", line)


def check_products(rep):
    """产物自检：行业映射覆盖度、社融停更（都只 WARN——不让规则静默失效/背景失真）。"""
    try:
        p1 = os.path.join(config.OUT_DIR, "stage1_all.csv")
        pool_n = 0
        if os.path.exists(p1):
            with open(p1, encoding="utf-8-sig", newline="") as f:
                pool_n = max(0, sum(1 for _ in f) - 1)
        mpath = os.path.join(config.OUT_DIR, config.INDUSTRY.get("out_csv") or "industry_map.csv")
        cov = 0
        if os.path.exists(mpath):
            import csv as _csv
            with open(mpath, encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    if str(row.get("industry") or "").strip():
                        cov += 1
        need = float(config.INDUSTRY.get("min_coverage") or 0.9)
        if pool_n and cov / pool_n < need:
            rep.warn("产物", "行业映射覆盖",
                     f"{cov}/{pool_n} = {cov/pool_n:.0%}（<{need:.0%}）：未覆盖按「未分类」计，"
                     f"行业 25% 上限可能静默失效；先跑 industry.py --refresh")
        elif pool_n:
            rep.ok("产物", "行业映射覆盖", f"{cov}/{pool_n} = {cov/pool_n:.0%}")
    except Exception as e:  # noqa: BLE001
        rep.warn("产物", "行业映射覆盖", f"检查失败：{e}")
    try:
        import json as _json
        p = os.path.join(config.OUT_DIR, "macro.json")
        mon = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                doc = _json.load(f)
            mon = str(((doc.get("data") or {}).get("social_financing") or {}).get("month") or "")
        if not mon:
            rep.warn("产物", "社融数据", "macro.json 没有社融数据（先跑 macro.py）")
        else:
            y2, m2 = int(TODAY[:4]), int(TODAY[5:7])
            y1, m1 = int(mon[:4]), int(mon[5:7])
            gap = (y2 - y1) * 12 + (m2 - m1)
            if gap > 3:
                rep.warn("产物", "社融数据",
                         f"最新 {mon}，落后 {gap} 个月：mofcom 源已停更，仅作参考（不伪造替代源）")
            else:
                rep.ok("产物", "社融数据", f"最新 {mon}")
    except Exception as e:  # noqa: BLE001
        rep.warn("产物", "社融数据", f"检查失败：{e}")


def check_cache(rep):
    today_dir = config.CACHE_DIR
    ltd = last_trading_day()
    if not os.path.isdir(today_dir):
        rep.warn("数据", "缓存目录", "不存在（决策日不取数；先在工作日跑本机取数）")
        return
    n_ltd = 0
    n_total = 0
    for root, _dirs, files in os.walk(today_dir):
        for f in files:
            n_total += 1
            if f.endswith(f"__{ltd}.csv"):
                n_ltd += 1
    if n_ltd:
        rep.ok("数据", "最近交易日缓存", f"{n_ltd} 个文件（{ltd} 取过数）")
    else:
        rep.warn("数据", "最近交易日缓存",
                 f"0 个（共 {n_total} 个旧缓存）——最近交易日 {ltd} 还没取过数，或取数失败"
                 "（决策日不取数，先在工作日补跑本机取数）")
    # 阶段一漏斗：池子是否为空（空池 = 上游接口挂了）
    fpath = os.path.join(config.OUT_DIR, "stage1_funnel.json")
    if os.path.exists(fpath):
        try:
            fun = _load_json(fpath)
            pool = int(fun.get("股票池") or 0)
            ok_cnt = int(fun.get("取数成功") or 0)
            to2 = fun.get("进入阶段二") or 0
            if pool and ok_cnt and ok_cnt < pool * 0.9:
                rep.warn("数据", "阶段一取数成功率", f"{ok_cnt}/{pool}（<90%，接口可能不稳）")
            elif not pool:
                rep.warn("数据", "阶段一漏斗", "股票池为 0（指数成分接口可能失败）")
            else:
                rep.ok("数据", "阶段一漏斗", f"池 {pool} / 取数成功 {ok_cnt} / 进阶段二 {to2}")
        except Exception as e:
            rep.warn("数据", "阶段一漏斗可解析", f"{e}")


def _to_rate(v):
    """把「1.077 / 1.077% / 0.01077 / '-' / None」这类值统一成小数年化，解析不了返回 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().replace("%", "").replace(",", "")
        if not s or s in ("-", "--", "—"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
    if f != f:            # NaN
        return None
    # 百分数（1.077）与小数（0.01077）都收：货币基金/国债收益率都不可能出现 >20% 的年化
    return f / 100.0 if abs(f) > 0.2 else f


def _records(data):
    """把 DataFrame / list[dict] / dict 统一成 list[dict]。"""
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        try:
            data = data.to_dict("records")
        except Exception:
            return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, (list, tuple)):
        return [x for x in data if isinstance(x, dict)]
    return []


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# 列名候选：`y7_pct` 是 T1 在 datasource.fetch_money_fund_rates() 里归一化后的列（单位=百分数，
# 例如 1.077 表示 1.077%）；其余是取数层原始/历史列名，留作兜底。
MONEY_FUND_KEYS = ("y7_pct", "7日年化", "7日年化收益率", "七日年化", "seven_day_yield", "yield_7d", "yield7d")
# `y1` 是 T1 的 fetch_cn_treasury_yield() 返回 dict 的键（小数，0.011799 = 1.1799%）
TREASURY_1Y_KEYS = ("y1", "1年", "1年收益率", "1Y", "1y", "cn_1y", "treasury_1y")


def _first_rate(rows, keys):
    """按候选列名从（按日期升序的）记录里取最新一条的利率。

    先做**精确键名匹配**再退化为子串匹配：否则 `y1` 会命中 `y10`（10 年期）这类前缀冲突。
    """
    for r in reversed(rows):
        for k in keys:
            if k in r:
                rate = _to_rate(r[k])
                if rate is not None:
                    return rate, str(k)
        for k in r:
            if any(h in str(k) for h in keys):
                rate = _to_rate(r[k])
                if rate is not None:
                    return rate, str(k)
    return None, ""


def check_rate_staleness(rep):
    """核实现金层利率与验收基准是否过期（只读；数据来自 datasource 的实测函数）。

    为什么要有它：2026-10-27 实测全市场 545 只货基 7 日年化中位数为 1.077%，
    而旧假设是「逆回购 2.0% / 货基 1.4%」——过期数据会让报表、回测、账本一起说错话，
    且「扣 token 后年化 > 无风险利率」的判定直接建立在这些数上。
    实测值与 config 假设偏离超过阈值只报 WARN（不拦决策），提示重测而不是继续用旧数。
    """
    r = config.cash_rates()
    cap = config.CAPITAL["cash_layer"]
    stale_bp = float(cap.get("rate_stale_bp") or 25)
    perf = getattr(config, "PERF", {}) or {}
    dep = float(perf.get("deposit_rate") or 0.0)
    dep_bp = float(perf.get("deposit_rate_stale_bp") or 20)
    dep_as_of = perf.get("deposit_rate_as_of") or ""

    try:
        import datasource as ds       # 延迟导入：doctor 其余检查不依赖取数层
    except Exception as e:                                    # pragma: no cover
        rep.warn("利率", "利率时效", f"datasource 导入失败，无法核对实测值：{e}")
        return

    # ---- 1. 货基 7 日年化 vs config.CAPITAL.cash_layer.rate_money_fund
    fn = getattr(ds, "fetch_money_fund_rates", None)
    if fn is None:
        rep.warn("利率", "现金层利率时效",
                 f"未取到 datasource.fetch_money_fund_rates()（datasource 版本不匹配）：当前按 {r['as_of']} "
                 f"的假设（逆回购 {r['repo']:.2%} / 货基 {r['money_fund']:.2%}），尚未与实测中位数核对")
    else:
        try:
            rows = _records(fn())
            vals = [v for v in (_to_rate(x) for row in rows for k, x in row.items()
                                if any(h in str(k) for h in MONEY_FUND_KEYS)) if v is not None]
            med = _median(vals)
            if med is None:
                rep.warn("利率", "现金层利率时效",
                         f"fetch_money_fund_rates() 返回结构未识别（{len(rows)} 条，列名对不上 "
                         f"{MONEY_FUND_KEYS}），需与 T1 对齐列名")
            else:
                diff_bp = (med - r["money_fund"]) * 10000.0
                detail = (f"实测 {len(vals)} 只货基 7 日年化中位数 {med:.3%}"
                          f" vs 假设 {r['money_fund']:.2%}（{r['as_of']}）"
                          f" 差 {diff_bp:+.0f}bp，阈值 ±{stale_bp:.0f}bp")
                if abs(diff_bp) > stale_bp:
                    rep.warn("利率", "现金层利率假设过期",
                             detail + "——请重测后改 config.CAPITAL[\"cash_layer\"] 的 rate_repo/rate_money_fund（假设不是承诺）")
                else:
                    rep.ok("利率", "现金层利率时效", detail)
        except Exception as e:
            rep.warn("利率", "现金层利率时效", f"取实测货基利率失败（网络/接口不稳）：{e}")

    # ---- 1b. 逆回购实测（GC001 历史均值）vs config.CAPITAL.cash_layer.rate_repo
    fn = getattr(ds, "fetch_repo_rate", None)
    if fn is None:
        rep.warn("利率", "逆回购利率时效",
                 f"未取到 datasource.fetch_repo_rate()（datasource 版本不匹配）：当前按 {r['as_of']} 的 GC001 "
                 f"近 250 交易日均值 {r['repo']:.2%}（历史样本，不是未来收益承诺）")
    else:
        try:
            got = fn() or {}
            rate = _to_rate(got.get("rate"))
            if rate is None:
                rep.warn("利率", "逆回购利率时效",
                         f"fetch_repo_rate() 无可用实测值（basis={got.get('basis') or '未给出'}）："
                         f"沿用 {r['repo']:.2%} 假设，请人工核实后再用")
            else:
                diff_bp = (rate - r["repo"]) * 10000.0
                detail = (f"实测逆回购 {rate:.3%}（{got.get('source') or '来源未标注'}，"
                          f"样本 {got.get('sample_start') or '?'}→{got.get('sample_end') or '?'}，"
                          f"{got.get('samples') or '?'} 个样本{'，代理值' if got.get('is_proxy') else ''}）"
                          f" vs 假设 {r['repo']:.2%}（{r['as_of']}）差 {diff_bp:+.0f}bp，"
                          f"阈值 ±{stale_bp:.0f}bp")
                if abs(diff_bp) > stale_bp:
                    # 逆回购的偏离两个方向含义不同，不能一律叫「过期」：
                    # 假设低于实测 = 保守（少计收益，不算风险）；假设高于实测 = 高估（该下调）。
                    if diff_bp > 0:
                        tail = "——假设低于实测（保守方向，现金层收益被低估，不必改，知悉即可）"
                    else:
                        tail = "——假设高于实测（高估方向），请重测后下调 config.CAPITAL[\"cash_layer\"].rate_repo 及其 basis"
                    rep.warn("利率", "逆回购利率假设与近一年实测偏离",
                             detail + tail)
                else:
                    rep.ok("利率", "逆回购利率时效", detail)
        except Exception as e:
            rep.warn("利率", "逆回购利率时效", f"取逆回购实测失败（网络/接口不稳）：{e}")

    # ---- 1c. 半流动性货基池：策略启用时排行文件必须新鲜（fail-closed 的守卫）
    # 现金层默认策略（cash_policy 含 "pool"）靠 out/cash_pool_rank.json 切片；排行过期时
    # paper.cash_yield_plan 会把整笔观望资金退回朝朝宝（1.06%），账本会少计约 25bp——
    # 这不是崩溃，是静默降级，所以必须由 doctor 显式喊出来。
    if "pool" in str(r.get("cash_policy") or ""):
        try:
            import cash_sleeve as _cs
            try:
                rk = _cs.load_rank()
                age = _cs.data_age_days(rk)
                maxd = _cs._cfg()["max_stale_days"]
                top = "、".join(f"{x.get('name')} {float(x['y90']):.2%}"
                                for x in rk["rows"][:3])
                detail = (f"策略 {r.get('cash_policy')}：排行 {len(rk['rows'])} 只，"
                          f"数据日 {rk.get('data_date') or '?'}（{age} 天前，阈值 {maxd} 天）；"
                          f"前 3 支 {top}")
                if age is None or age > maxd:
                    rep.warn("利率", "半流动性选基排行过期",
                             detail + "——先跑 `python cash_pool.py` 刷新，否则结算时整笔退回朝朝宝")
                else:
                    rep.ok("利率", "半流动性选基排行", detail)
            except Exception as e:
                rep.warn("利率", "半流动性选基排行",
                         f"排行文件不可用（{type(e).__name__}: {e}）——结算会 fail-closed 退回朝朝宝")
        except ImportError:
            rep.warn("利率", "半流动性选基排行", "cash_sleeve.py 缺失（cash_policy=pool 却无切片模块）")

    # ---- 2. 1 年期国债收益率 vs config.PERF.deposit_rate（验收基准）
    fn = getattr(ds, "fetch_cn_treasury_yield", None)
    if fn is None:
        rep.warn("利率", "验收基准时效",
                 f"未取到 datasource.fetch_cn_treasury_yield()（datasource 版本不匹配）：当前按 {dep_as_of or '未标注'} "
                 f"的假设 无风险基准 {dep:.2%}，尚未与实测 1 年期国债核对")
    else:
        try:
            one_y, col = _first_rate(_records(fn()), TREASURY_1Y_KEYS)
            if one_y is None:
                rep.warn("利率", "验收基准时效",
                         "fetch_cn_treasury_yield() 返回结构未识别（找不到 1 年期列），需与 T1 对齐")
            else:
                diff_bp = (one_y - dep) * 10000.0
                detail = (f"实测 1 年期国债（列 {col}）{one_y:.3%} vs 验收基准 {dep:.2%}"
                          f"（{dep_as_of or '未标注'}）差 {diff_bp:+.0f}bp，阈值 ±{dep_bp:.0f}bp")
                if abs(diff_bp) > dep_bp:
                    rep.warn("利率", "验收基准过期",
                             detail + "——请重测后改 config.PERF 的 deposit_rate 及其 _as_of/_source")
                else:
                    rep.ok("利率", "验收基准时效", detail)
        except Exception as e:
            rep.warn("利率", "验收基准时效", f"取国债收益率失败（网络/接口不稳）：{e}")


def check_selftest(rep):
    """可选：跑两套离线自检（不联网，约 5 秒）。"""
    for script, label, expected in (("_selftest.py", "本金/整手/费用自检", "通过"),
                                    ("_selftest_paper.py", "账本撮合/止损自检", "75 项")):
        path = os.path.join(HERE, script)
        try:
            out = subprocess.run([sys.executable, path], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=300)
            tail = (out.stdout or "").strip().splitlines()[-1:] or [""]
            if out.returncode == 0:
                rep.ok("自检", label, tail[0][:80])
            else:
                rep.error("自检", label, f"退出码 {out.returncode}：{(out.stderr or tail[0])[:120]}")
        except Exception as e:                                 # pragma: no cover
            rep.error("自检", label, f"跑不起来：{e}")


def summarize(rep, name, cfg, state):
    if state is None:
        return
    try:
        p = paper.portfolio(state, {c: h.get("last_close") or h.get("cost_basis") or 0
                                   for c, h in (state.get("holdings") or {}).items()})
        ratio = (p["market_value"] / p["nav"]) if p["nav"] else 0.0
        rep.ok("摘要", f"{name} 账户",
               f"净值 {p['nav']:,.2f}（{p['total_return']:+.2%}）/ 现金 {p['cash']:,.0f} / "
               f"仓位 {ratio:.1%} / 持仓 {len(state.get('holdings') or {})} 只 / "
               f"挂单 {len(paper.pending_orders(state))} 张")
    except Exception as e:
        rep.warn("摘要", f"{name} 账户", f"算不出来：{e}")


def to_md(rep, args):
    lines = [f"# 投资系统体检 · {TODAY}", ""]
    lines.append(f"**结论：{rep.verdict}**｜ERROR {rep.count('ERROR')} / WARN {rep.count('WARN')}"
                 f"｜检查项 {len(rep.items)}")
    lines.append("")
    lines.append("| 级别 | 领域 | 检查 | 结果 | 细节 |")
    lines.append("|---|---|---|---|---|")
    for r in rep.items:
        mark = "✓" if r["ok"] else ("✗" if r["level"] == "ERROR" else "!")
        lines.append(f"| {r['level']} | {r['area']} | {r['name']} | {mark} | {r['detail']} |")
    lines.append("")
    if rep.verdict == "NO-GO":
        lines.append("**先修 ERROR 再跑决策**——带着这些错误跑，账本会记错钱。")
    elif rep.count("WARN"):
        lines.append("可以跑，但上面的 WARN 要看一眼（多半是「最近交易日还没取数」或「out/ 是另一个本金生成的」）。")
    else:
        lines.append("全部通过，可以跑决策。")
    lines.append("")
    lines.append("边界：体检只读不写，不取数、不下单、不改账本；它不能证明策略能挣钱，只能证明"
                 "「引擎和账本现在是自洽的」。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="all",
                    choices=sorted(list((config.PAPER.get("ledgers") or {}).keys()) + ["all"]))
    ap.add_argument("--state", default=None, help="只体检这个账本文件（调试/复核用）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="附带跑两套离线自检")
    args = ap.parse_args(argv)

    rep = Report()
    check_env(rep)
    ledgers = config.PAPER.get("ledgers") or {}
    names = list(ledgers) if args.ledger == "all" else [args.ledger]
    for name in names:
        cfg = dict(ledgers[name])
        if args.state:
            cfg["state_file"] = args.state
        state = check_ledger(rep, name, cfg)
        check_plan(rep, name, cfg, state, ledgers)
        check_freshness(rep, name)
        if state:
            check_risk(rep, name, state)
        summarize(rep, name, cfg, state)
    check_cache(rep)
    check_products(rep)
    check_rate_staleness(rep)
    if args.selftest:
        check_selftest(rep)

    os.makedirs(PAPER_OUT, exist_ok=True)
    md = to_md(rep, args)
    with open(os.path.join(PAPER_OUT, "doctor.md"), "w", encoding="utf-8") as f:
        f.write(md + "\n")
    payload = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
               "verdict": rep.verdict,
               "errors": rep.count("ERROR"), "warns": rep.count("WARN"),
               "items": rep.items}
    with open(os.path.join(PAPER_OUT, "doctor.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for r in rep.items:
            mark = "✓" if r["ok"] else ("✗" if r["level"] == "ERROR" else "!")
            print(f"  {mark} [{r['level']}] {r['area']}·{r['name']}"
                  + (f"：{r['detail']}" if r["detail"] else ""))
        print()
        print(f"[doctor] 结论：{rep.verdict}（ERROR {rep.count('ERROR')} / WARN {rep.count('WARN')}）")
        print(f"[doctor] 体检报告：{os.path.join(PAPER_OUT, 'doctor.md')}")
    return 1 if rep.verdict == "NO-GO" else 0


if __name__ == "__main__":
    raise SystemExit(main())
