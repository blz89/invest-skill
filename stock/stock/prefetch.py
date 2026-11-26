# -*- coding: utf-8 -*-
"""stock prefetch —— 一次性取全 invest 当天要用的数据，统一落 ``data\\shared``。

为什么要有这个入口
------------------
2026-11-16 起 invest 的取数层都收口到本仓库 ``stock/``，但缓存仍按技能各存一份
（``data\\invest\\cache``、``data\\旧策略\\cache``）：同一份全市场日线/财务/ETF 行情被两个
技能各取一遍，19:00 的 Windows 任务要跑两次取数，任一处接口抖动都会拖垮整条链
（11-16 就是 invest 段被 1800 秒超时杀掉）。

本模块把「两个技能当天会请求的全部 (kind, key)」在本机一次取完，写进唯一共享缓存
``data\\shared\\cache``（目录结构与 datasource 完全一致），并把统一记录写进
``data\\shared\\snapshots\\<数据日>\\{manifest.json,summary.json,summary.md}``。
两个技能的 ``CACHE_DIR`` 用目录 junction 指到 ``data\\shared\\cache`` 后，Windows 任务
只剩「一条 stock prefetch + 两段热缓存计算」。

本模块就是对两个技能的**取数承诺**（改这里 = 改承诺）：

+--------------+--------------------------------------------------------------+
| 组           | 覆盖的 (kind, key)                                           |
+==============+==============================================================+
| ref          | calendar/trade, market_snapshot/sina_industry,               |
|              | fund_purchase/all, fund_nav_daily/all, money_rank/all,       |
|              | cn_treasury_yield/gov_curve, repo_rate/gc001_204001,         |
|              | repo_rate_7d/r-007_sz131801                                  |
| index        | index_daily/{000300,000905,000852,000688,399006},            |
|              | index_cons/{000300,000905}                                   |
| etf          | etf_spot_tx/all（腾讯行情）+ etf_universe/ths + 规模表 +      |
|              | etf_daily/<sh|sz><code>（当日成交额≥0.3 亿 ∪ pin 名单）       |
| fund         | fund_nav/<code>__累计净值走势 + fund_fee/<code>__运作费用 /    |
|              | 赎回费率（按 invest otc.py 的 L1–L6 复现出来的候选）          |
| stocks       | daily/<sina_symbol>__4y + fin/<code> +                       |
|              | val/<code>__市盈率(TTM) / 市净率（指数成分并集 ≈800 只）      |
| weekly(可选) | money_hist/<code>（朝朝盈2号货基池，翻页慢，默认不进夜间任务）|
+--------------+--------------------------------------------------------------+

``fund`` 组为什么要在 stock 里复现 invest 的漏斗：场外债基池的 L5（750 天历史 + 3 年回撤）
与 L6（A/C 份额去重）依赖每只基金的净值历史，不先过滤就得把 4825 只全取一遍
（11-15 实测漏斗 L1 4825 → L5 1944 → L6 1101）。规则是从
``invest/scripts/otc.py`` 原样抄的，并用 11-15/11-16 两天的生产漏斗 JSON 对过数
（L1–L4 完全一致、L5/L6 完全一致），改规则必须重对。

统一记录的判据
--------------
* ``coverage``：扫 ``shared/cache/<kind>/*__<数据日>.csv`` 的文件数，与本次「应取」数量比对；
  缺口超过容差 → rc=2（ps1 会照常往下跑并把 summary.ok 标 false，但日志里红字）。
* ``data_bar_date``：``index_daily/000300`` 最后一个交易日。它必须等于「应有数据日」
  （日历里 ≤ 数据日的最后一个交易日），否则 数据准备 会 ``_purge_date_keys``
  删掉共享缓存的当日键、把一次取数变成两次取数 → 此时 rc=2（可用 ``--allow-stale-bar``
  在演练里放开）。``etf_bar_date`` 是全体 ETF 日线里最旧的末根 bar，同样必须等于应有
  交易日；否则 20:00 的 ETF 条件单会拿 T-1 的 bar 判状态（B-7 的教训）。
* 退出码：0 成功；1 取数过程异常（有组整体失败）；2 覆盖自检/bar 校验不通过；
  3 盘中守护拒绝（交易日 15:05 前默认不写「今天」的键，``--allow-intraday`` 或
  ``--date`` 回放才放行）。

用法
----
    python -m stock prefetch                    # 夜间任务默认：ref+index+etf+fund+stocks
    python -m stock prefetch --verify-only      # 只做覆盖自检（不取数）
    python -m stock prefetch --dry-run          # 只打印计划（不取数）
    python -m stock prefetch --only ref,index   # 只跑指定组
    python -m stock prefetch --skip-stocks      # 跳过个股逐只预热
    python -m stock prefetch --with-weekly      # 附带货基历史（周六/决策日用）
    python -m stock prefetch --force            # 忽略当天缓存重取（A_SHARE_FORCE_REFRESH=1）
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import codes, config
from .skilldata import datasource as ds

TOOL = "stock-prefetch"
SCHEMA = 1

# ---------------------------------------------------------------- 取数范围契约
# 指数日线：指数池 5 条（bench/mid/small/growth + 000688）+ invest bench 000300
INDEX_DAILY = ("000300", "000905", "000852", "000688", "399006")
# 指数成分：两技能 L0 股票池声明（核心池 000300+000905，invest hs300=000300）
INDEX_CONS = ("000300", "000905")

# ETF 日线目标：当日成交额 ≥ 该阈值（11-16 实测 0.3 亿覆盖 全部候选 254/254）
ETF_MIN_AMT_YI = 0.3
# 必须 pin 的 ETF：持仓/挂单（旧策略 ledger）+ 各类代表 + 防御篮 + invest 常用场内债基
ETF_PIN = (
    # 持仓/挂单（B-7 事故标的）
    "516160", "159922", "159845", "512100", "159915", "159948",
    # 宽基/指数
    "510300", "159919", "510500", "588000", "512880", "512010", "159901", "159949",
    # gold
    "518880", "159937", "159934",
    # bond / money
    "511010", "511090", "511110", "511130", "511360", "511660", "511700", "159001",
    "510720", "511120", "511150", "512000",
    # convertible
    "511180", "511380",
    # commodity / cross
    "159985", "513100", "513310",
    # 熔断防御篮（paper.py 按名称关键词选：红利低波 + 沪深300）
    "563020", "515450", "512890",
)

# 个股估值指标：旧策略 用 市盈率(TTM)；invest 用 市盈率(TTM)+市净率
VALUATION_INDICATORS = ("pe_ttm", "pb")
# datasource 的 indicator 中文映射（与 datasource._VALUATION_INDICATORS 保持一致）
VAL_CN = {"pe_ttm": "市盈率(TTM)", "pe": "市盈率(TTM)", "pb": "市净率",
          "pe_static": "市盈率(静)", "ps": "市现率", "pcf": "市现率", "mktcap": "总市值"}

# 两技能 config.PRICE_YEARS 都是 4 → daily 缓存键固定 <sym>__4y
PRICE_YEARS = 4
FIN_START_YEAR = "2019"

# ---------------------------------------------------------------- invest OTC 漏斗
# 抄自 D:\skills\invest\scripts\otc.py（11-15/11-16 生产漏斗对过数）。
# 改这里的任何一个阈值都必须重跑对账，否则共享缓存里会少一批基金。
OTC = {
    "ftypes": ("债券型-中短债", "债券型-长债", "债券型-信用债", "指数型-固收"),
    "name_exclude": ("联接", "可转债", "转债", "增强", "双债", "混合一级", "混合二级"),
    "purchase_status_ok": ("开放申购", "限大额"),
    "min_day_limit": 10000.0,        # 日限额 0<dl<10000 淘汰（0/空=不限，放行）
    "max_min_purchase": 1000.0,      # 起购 >1000 淘汰
    "max_fee_pct": 0.15,             # 申购费 >0.15% 淘汰（None 淘汰＝fail-closed）
    "min_hist_days": 750,            # 累计净值历史 ≥750 行
    "dd_by_ftype": {"债券型-中短债": -0.05, "指数型-固收": -0.05,
                    "债券型-信用债": -0.10, "债券型-长债": -0.10},
    "dd_default": -0.05,
    "min_scale_yi": 1.0,             # 规模 <1 亿淘汰；查不到不淘汰（与 otc.py 一致）
    "share_prefer": "C",
}
NAV_FUND_INDICATOR = "累计净值走势"
FEE_INDICATORS = ("运作费用", "赎回费率")
# 规模（fund_scale）预热：蛋卷档案主源 2026-11 起时好时坏（东财 fund_scale_em 兜底）；
# 先探 120 只，可用率 <20% 就跳过剩余（消费侧 fail-open，与 otc.py 一致）。
SCALE_PROBE = 120
SCALE_PROBE_MIN_RATIO = 0.2
# weekly 组货基历史的窗口（自然日）：cash_pool.py 只用 tail(180)，留一倍余量；
# 全历史 4815 行要翻 241 页 ≈40–60 秒/只，窗口约 4 秒/只。
MONEY_HIST_DAYS = 400

# 货基池（weekly 组用；invest 现金层脚本的固定池）
CASH_POOL_JSON = os.environ.get("STOCK_CASH_POOL_JSON") or os.path.join(
    os.path.expanduser("~"), ".dsh", "skills", "invest", "scripts", "cash_pool_chaochao2.json")

# 覆盖自检：硬性下限（两技能 verify 的最小要求，数据准备._DEFAULT_VERIFY 同源）
REQUIRED_MIN = {
    "calendar": 1,
    "market_snapshot": 1,
    "index_daily": len(INDEX_DAILY),
    "index_cons": len(INDEX_CONS),
    "etf_daily": 20,
    "daily": 200,
    "fin": 200,
}
# 期望数量与本次应取数量比对时的容差（比例, 绝对下限）；未列出的 kind 只查 REQUIRED_MIN
TOLERANCE = {
    "daily": (0.02, 20),
    "fin": (0.02, 20),
    "etf_daily": (0.05, 30),
    "fund_nav": (0.02, 80),
    "fund_fee": (0.02, 50),
    "val": (0.10, 100),
    "money_hist": (0.10, 10),
    # 同花顺 ETF 代码池是增强源：THS 挂但腾讯/新浪兜底可用时，universe 缺失只记
    # tolerant（1 条），不把整晚 prefetch 判红（行情表本身另有 etf_spot 存在性检查）。
    "etf_universe": (0.0, 1),
}


# ---------------------------------------------------------------- 基础设施
def _force() -> bool:
    return str(os.environ.get("A_SHARE_FORCE_REFRESH", "")).strip().lower() in ("1", "true", "yes")


def _shim() -> types.SimpleNamespace:
    """给 datasource.init 注入的伪 config：字段与两技能 config 的超集，口径全部固定。"""
    em = str(os.environ.get("STOCK_EM_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    return types.SimpleNamespace(
        CACHE_DIR=str(config.SHARED_CACHE_DIR),
        OUT_DIR=str(config.SHARED_DIR / "out"),
        PRICE_YEARS=PRICE_YEARS,
        FIN_START_YEAR=FIN_START_YEAR,
        VALUATION_PERIOD="近五年",
        EM_ENABLED=em,
        SCALE_CACHE_MAX_TRADE_DAYS=5,
        SCALE_CACHE_FILENAME="etf_scale_lastgood.json",
        TX_QUOTE_BATCH=60,
        TX_QUOTE_SLEEP=0.25,
        FORCE_IPV4=True,
    )


def _init(force: bool = False, date: str = "") -> None:
    """把 datasource 指向共享缓存；prefetch 必须联网，所以清掉 offline/数据日相关 env。"""
    if force:
        os.environ["A_SHARE_FORCE_REFRESH"] = "1"
    else:
        os.environ.pop("A_SHARE_FORCE_REFRESH", None)
    os.environ.pop("A_SHARE_OFFLINE", None)
    if date:
        os.environ["A_SHARE_DATA_DATE"] = date
    else:
        os.environ.pop("A_SHARE_DATA_DATE", None)
    for p in (config.SHARED_DIR, config.SHARED_CACHE_DIR, config.SNAPSHOTS_DIR,
              config.SHARED_DIR / "out"):
        Path(p).mkdir(parents=True, exist_ok=True)
    ds.init(_shim(), flavor="shared")


def _cache_root() -> Path:
    return Path(config.SHARED_CACHE_DIR)


def _cache_path(kind: str, key: str) -> Path:
    return Path(ds._cache_file(kind, key))


def _num(v):
    """与技能侧 m.f 等价：None/NaN/空串/不可转 → None。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "none", "null", "--"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _row_count(x) -> int:
    if x is None:
        return 0
    if isinstance(x, pd.DataFrame):
        return int(len(x))
    if isinstance(x, pd.Series):
        return int(len(x))
    if isinstance(x, dict):
        return len(x)
    try:
        return int(len(x))
    except TypeError:
        return 1


def _max_bar(x) -> str:
    """结果里最后一根 bar 的日期（manifest 的 bar 校验用它，不信磁盘 CSV）。"""
    if not isinstance(x, pd.DataFrame):
        return ""
    for col in ("date", "trade_date"):
        if col in x.columns:
            try:
                s = pd.to_datetime(x[col], errors="coerce").dropna()
            except Exception:  # noqa: BLE001
                return ""
            if len(s):
                return str(s.max().date())
    return ""


def _anchor_bar(recs) -> str:
    """bar 校验的锚点：``index_daily/000300`` 的末根 bar。

    不能取 ``index_daily`` 里的 max——2026-11-17 首跑就是被 000905/000852/000688/399006
    的当日 bar 掩盖了 000300 停在 T-1 的陈旧缓存，manifest 误报 ``bar_ok=true``，
    随后 数据准备 读到 000300 的 T-1 bar 就整批 purge 当日键重取（白烧 17 分钟）。
    """
    for r in recs:
        if r.get("kind") == "index_daily" and r.get("key") == "000300":
            return str(r.get("max_bar_date") or "")
    return ""


def _item(label: str, kind: str, key: str, fn, required: bool = False,
          note: str = "", keep: bool = False) -> dict:
    return {"label": label, "kind": kind, "key": key, "fn": fn,
            "required": required, "note": note, "keep": keep}


def _frame_or_none(fn):
    try:
        df = fn()
        return df if isinstance(df, pd.DataFrame) else None
    except Exception:  # noqa: BLE001 —— 拿不到就当没有，由覆盖自检暴露
        return None


def _exec(group: str, items: list, workers: int, log, keep: bool = False,
          bar_expected: str = "") -> list:
    """执行一组取数任务。返回每条 (kind,key) 的记录：ok/rows/secs/max_bar_date/error。

    `_cached` 自己负责缓存读写，这里只做「并发调度 + 记账」；同一 (kind,key) 只跑一次。
    """
    seen, uniq = set(), []
    for it in items:
        k = (it["kind"], it["key"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    if not uniq:
        return []

    pre = {}
    for it in uniq:
        try:
            pre[(it["kind"], it["key"])] = _cache_path(it["kind"], it["key"]).is_file()
        except Exception:  # noqa: BLE001
            pre[(it["kind"], it["key"])] = None

    def one(it: dict) -> dict:
        t0 = time.time()
        rec = {"label": it["label"], "kind": it["kind"], "key": it["key"],
               "required": bool(it["required"]), "note": it["note"]}
        try:
            res = it["fn"]()
            rec.update(ok=True, rows=_row_count(res), secs=round(time.time() - t0, 2))
            bar = _max_bar(res)
            if bar:
                rec["max_bar_date"] = bar
            if keep and it["keep"]:
                rec["result"] = res
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整组，交给覆盖自检
            rec.update(ok=False, rows=0, secs=round(time.time() - t0, 2),
                       error=f"{type(exc).__name__}: {exc}")
        had = pre.get((it["kind"], it["key"]))
        rec["from_cache"] = bool(had) and not _force()
        return rec

    out, done = [], 0
    n = len(uniq)
    t_start = time.time()
    if workers <= 1 or n == 1:
        for it in uniq:
            out.append(one(it))
            done += 1
            if log and done % 100 == 0:
                log(f"    [{group}] {done}/{n} …")
    else:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, it): it for it in uniq}
            for fut in as_completed(futs):
                out.append(fut.result())
                done += 1
                if log and done % 100 == 0:
                    log(f"    [{group}] {done}/{n} …")
    order = {(it["kind"], it["key"]): i for i, it in enumerate(uniq)}
    out.sort(key=lambda r: order.get((r["kind"], r["key"]), 10 ** 9))

    # 失败重试一轮：清掉负缓存标记后把失败条目单跑一次（瞬时网络/空表不该拖到第二天）。
    failed = [r for r in out if not r["ok"]]
    if failed:
        by_key = {(it["kind"], it["key"]): it for it in uniq}
        fixed = 0
        for r in failed:
            it = by_key.get((r["kind"], r["key"]))
            if it is None:
                continue
            try:
                fp = str(_cache_path(it["kind"], it["key"]))
                if fp.endswith(".csv"):
                    fp = fp[:-4]
                fp += ".failed"
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass
            time.sleep(0.4)
            r2 = one(it)
            r2["retried"] = True
            if r2["ok"]:
                fixed += 1
                r.pop("error", None)
            r.update(r2)
        if log:
            log(f"  [{group}] 失败重试 {len(failed)} 条 → 成功 {fixed} 条")

    # 同日陈旧 bar 自愈：bar 类数据「同名当日缓存 + 末根 bar 不够新」说明这份缓存是
    # 盘中/盘前误跑留下的（2026-11-17 首跑事故）。删掉该缓存重取一次，避免 from_cache
    # 把 T-1 的档位复用到今天，进而让 数据准备 整批 purge。
    if bar_expected:
        stale = [r for r in out
                 if r.get("ok") and r.get("from_cache")
                 and r.get("max_bar_date") and r["max_bar_date"] < bar_expected]
        if stale:
            by_key = {(it["kind"], it["key"]): it for it in uniq}
            fixed_bar = 0
            for r in stale:
                it = by_key.get((r["kind"], r["key"]))
                if it is None:
                    continue
                try:
                    p = _cache_path(r["kind"], r["key"])
                    if p.is_file():
                        p.unlink()
                    fp = str(p)
                    if fp.endswith(".csv"):
                        fp = fp[:-4]
                    fp += ".failed"
                    if os.path.isfile(fp):
                        os.remove(fp)
                except OSError:
                    pass
                r2 = one(it)
                r2["stale_refreshed"] = True
                if r2["ok"]:
                    fixed_bar += 1
                r.update(r2)
            if log:
                log(f"  [{group}] 同日陈旧 bar 刷新 {len(stale)} 条 → 成功 {fixed_bar} 条")

    ok_n = sum(1 for r in out if r["ok"])
    if log:
        log(f"  [{group}] {ok_n}/{n} ok，{time.time() - t_start:.1f}s")
    return out


def _skip_record(group: str, kind: str, key: str, reason: str) -> dict:
    return {"label": f"[跳过] {group}", "kind": kind, "key": key, "required": False,
            "note": reason, "ok": False, "rows": 0, "secs": 0.0,
            "error": reason, "skipped": True, "from_cache": False}


def _purge_failed(dd: str) -> list:
    """清掉共享缓存里「数据日 == dd」的负缓存标记（一次失败不该毒死当天全链）。"""
    removed = []
    root = _cache_root()
    if not root.is_dir():
        return removed
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for p in d.glob(f"*__{dd}.failed"):
            try:
                p.unlink()
                removed.append(f"{d.name}/{p.name}")
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- ref / index
def group_ref(workers: int, log) -> tuple[list, dict]:
    items = [
        _item("交易日历", "calendar", "trade", ds.fetch_trade_calendar, True),
        _item("全市场板块快照", "market_snapshot", "sina_industry",
              ds.fetch_market_snapshot, True),
        _item("A股简称名单", "code_names", "a_share", ds.fetch_code_names),
        # 简称名单用于剔除 ST/*ST 与退市整理（缺了只是退回名称判断，不阻断取数）
        _item("A股基础信息", "stock_basic", "bs", ds.fetch_stock_basic),
        # 含退市日与上市日：回测/筛选还原「当时是否在市」，补幸存者偏差
        _item("历史退市名单", "delisted", "bs", ds.fetch_delisted),
        _item("场外基金申购表", "fund_purchase", "all",
              ds.fetch_fund_purchase_snapshot, True),
        _item("场外基金日净值", "fund_nav_daily", "all", ds.fetch_fund_nav_daily),
        _item("货基排行", "money_rank", "all", ds.fetch_money_fund_rates),
        _item("国债收益率曲线", "cn_treasury_yield", "gov_curve", ds.fetch_cn_treasury_yield),
        _item("逆回购 GC001", "repo_rate", "gc001_204001", ds.fetch_repo_rate),
        _item("逆回购 R-007", "repo_rate_7d", "r-007_sz131801", ds.fetch_repo_rate_7d),
    ]
    recs = _exec("ref", items, max(2, min(4, workers)), log)
    # 后续组要用这两张表（缓存读回，post 会重新归一化）
    ctx = {
        "purchase": _frame_or_none(ds.fetch_fund_purchase_snapshot),
        "money_rank": _frame_or_none(ds.fetch_money_fund_rates),
    }
    return recs, ctx


def _pool_from_cons(recs: list) -> list:
    pool = set()
    for r in recs:
        if r.get("kind") == "index_cons" and r.get("ok") and isinstance(r.get("result"), pd.DataFrame):
            df = r["result"]
            col = "code" if "code" in df.columns else df.columns[0]
            for v in df[col]:
                s = str(v).strip()
                if s:
                    pool.add(s.zfill(6) if s.isdigit() and len(s) <= 6 else s)
    for r in recs:
        r.pop("result", None)
    return sorted(pool)


def group_index(workers: int, log, bar_expected: str = "") -> tuple[list, dict]:
    items = []
    for idx in INDEX_DAILY:
        items.append(_item(f"指数日线 {idx}", "index_daily", idx,
                           functools.partial(ds.fetch_index_daily, idx), True))
    for idx in INDEX_CONS:
        items.append(_item(f"指数成分 {idx}", "index_cons", idx,
                           functools.partial(ds.fetch_index_cons, idx), True, keep=True))
    recs = _exec("index", items, max(2, min(4, workers)), log, keep=True,
                 bar_expected=bar_expected)
    return recs, {"pool": _pool_from_cons(recs)}


# ---------------------------------------------------------------- etf
def _etf_hist_targets(spot: pd.DataFrame | None) -> list:
    """ETF 日线目标：当日成交额 ≥ ETF_MIN_AMT_YI 亿 ∪ pin 名单。"""
    out = set(ETF_PIN)
    if isinstance(spot, pd.DataFrame) and len(spot):
        amt = None
        for col in ("成交额", "amount"):
            if col in spot.columns:
                amt = pd.to_numeric(spot[col], errors="coerce")
                break
        code_col = "代码" if "代码" in spot.columns else ("code" if "code" in spot.columns else None)
        if amt is not None and code_col is not None:
            keep = spot.loc[amt >= ETF_MIN_AMT_YI * 1e8, code_col]
            for v in keep:
                s = str(v).strip()
                if s:
                    out.add(s.zfill(6) if s.isdigit() and len(s) <= 6 else s)
    good = []
    for c in out:
        try:
            good.append(codes.norm_code(c))
        except Exception:  # noqa: BLE001
            good.append(c)
    return sorted(set(good))


def group_etf_base(workers: int, log) -> tuple[list, dict]:
    """ETF 组的「前置两张表」：全表行情（etf_spot_tx）与规模表。

    必须**串行先跑**、不能与 fund 组并行：fund 组的 L7 会经 ``fetch_fund_scale``
    兜底调 ``fetch_etf_scale_map``，而后者内部也要读写同一份 ``etf_spot_tx/all``
    缓存。两组同时冷启动会各打一遍腾讯行情、并发写同一个缓存文件（2026-11-17 审计）。
    """
    recs = []
    # 1) ETF 全表行情：腾讯 qt.gtimg.cn（缓存落 etf_spot_tx/all）
    r = _exec("etf.spot", [_item("ETF 全表行情（腾讯）", "etf_spot", "all",
                                ds.fetch_etf_spot, True, keep=True,
                                note="缓存实为 etf_spot_tx/all")], 1, log, keep=True)
    spot = r[0].get("result")
    for x in r:
        x.pop("result", None)
    recs += r
    # 2) ETF 规模表：腾讯行情总市值（≈ 规模）+ last-good json
    r = _exec("etf.scale", [_item("ETF 规模表", "etf_scale", "all",
                                  ds.fetch_etf_scale_map, False, keep=True,
                                  note="腾讯 aum + lastgood json")], 1, log, keep=True)
    scale = r[0].get("result")
    for x in r:
        x.pop("result", None)
    recs += r
    return recs, {"spot": spot, "scale_map": scale}


def group_etf(workers: int, log, meta: dict | None = None,
              bar_expected: str = "") -> tuple[list, dict]:
    """ETF 日线（逐只）：前置表已在 ``group_etf_base`` 里取好（meta 带 spot）。"""
    meta = meta or {}
    spot = meta.get("spot")
    if spot is None:                      # 演练/单跑兜底：自己没有就现取一次
        _, base = group_etf_base(workers, log)
        spot = base.get("spot")
    targets = _etf_hist_targets(spot)
    items = []
    for c in targets:
        sym = codes.etf_symbol(c)
        items.append(_item(f"ETF 日线 {c}", "etf_daily", sym,
                           functools.partial(ds.fetch_etf_hist, sym), True,
                           note="成交额池|pin"))
    recs = _exec("etf.hist", items, workers, log, bar_expected=bar_expected)
    return recs, {"hist_codes": targets}



# ---------------------------------------------------------------- fund（场外债基）
def _share_split(name: str) -> tuple[str, str]:
    """A/C 份额识别：与 invest otc.py::share_split 等价的简化实现。

    规则：全角字母转半角、去掉 '-' 与空格；末尾是单个字母（A–I）且前一位不是数字
    → 份额类；兼容「X-C」「XC类」两种写法。
    """
    s = str(name or "")
    table = {ord(c): ord(c) - 0xFEE0 for c in "ＡＢＣＤＥＦＧＨＩ"}
    s = s.translate(table).replace("-", "").replace(" ", "").replace("　", "")
    s = s.strip()
    if not s:
        return s, ""
    m = re.search(r"([A-I])类?$", s)
    if not m or (len(s) >= 2 and s[-2].isdigit()):
        return s, ""
    cls = m.group(1)
    # key 里必须去掉份额字母：A/C 两个份额要算出同一个 key，L6 去重才生效（与 invest otc.py 一致）
    return s[:m.start()], cls


def _dd_limit(ftype: str) -> float:
    return OTC["dd_by_ftype"].get(ftype, OTC["dd_default"])


def _otc_lt4(purchase: pd.DataFrame) -> tuple[list, dict]:
    """复现 invest otc.py 的 L1–L4（ftype/名称/申赎状态/起购+费率）。"""
    f = {"L0": int(len(purchase)), "L1": 0, "L2": 0, "L3": 0, "L4": 0}
    pool = []
    cols = set(purchase.columns)
    c_ftype = "ftype" if "ftype" in cols else None
    c_name = "name" if "name" in cols else None
    c_code = "code" if "code" in cols else None
    c_status = "purchase_status" if "purchase_status" in cols else None
    c_limit = "day_limit" if "day_limit" in cols else None
    c_min = "min_purchase" if "min_purchase" in cols else None
    c_fee = "fee_pct" if "fee_pct" in cols else None
    if not all((c_ftype, c_name, c_code, c_status, c_limit, c_min, c_fee)):
        raise RuntimeError(f"场外申购表列缺失：现有 {sorted(cols)}")
    for _, row in purchase.iterrows():
        ftype = str(row[c_ftype])
        if ftype not in OTC["ftypes"]:
            continue
        f["L1"] += 1
        name = str(row[c_name])
        if any(w in name for w in OTC["name_exclude"]):
            continue
        f["L2"] += 1
        if str(row[c_status]) not in OTC["purchase_status_ok"]:
            continue
        dl = _num(row[c_limit])
        if dl is not None and 0 < dl < OTC["min_day_limit"]:
            continue
        f["L3"] += 1
        start = _num(row[c_min])
        if start is None or start > OTC["max_min_purchase"]:
            continue
        fee = _num(row[c_fee])
        if fee is None or fee > OTC["max_fee_pct"]:
            continue
        f["L4"] += 1
        code = str(row[c_code]).strip().zfill(6)
        skey, scls = _share_split(name)
        pool.append({"code": code, "name": name, "ftype": ftype,
                     "fee_pct": fee, "share_key": skey or code, "share_class": scls})
    return pool, f


def _nav_metrics(df: pd.DataFrame | None) -> tuple[int, float] | None:
    """与 invest otc.py::nav_metrics 等价：hist_days + 近 750 个净值点的最大回撤。"""
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 60:
        return None
    if "value" not in df.columns or "date" not in df.columns:
        return None
    v = pd.to_numeric(df["value"], errors="coerce")
    idx = pd.to_datetime(df["date"], errors="coerce")
    nav = pd.Series(v.values, index=idx).dropna()
    if nav.empty:
        return None
    nav = nav.sort_index()
    hist_days = int(len(nav))
    tail = nav.tail(750)
    dd = float((tail / tail.cummax() - 1.0).min())
    return hist_days, dd


def _otc_l5_l6(pool4: list, metrics: dict) -> tuple[list, dict]:
    """L5（750 天历史 + 3 年回撤）→ L6（A/C 份额去重）。"""
    c = {"navfail": 0, "hist_short": 0, "dd_deep": 0, "eligible": 0, "dup": 0}
    best = {}
    for r in pool4:
        m = metrics.get(r["code"])
        if m is None:
            c["navfail"] += 1
            continue
        hist_days, dd = m
        if hist_days < OTC["min_hist_days"]:
            c["hist_short"] += 1
            continue
        if dd < _dd_limit(r["ftype"]):
            c["dd_deep"] += 1
            continue
        c["eligible"] += 1
        rank = (0 if r["share_class"] == OTC["share_prefer"] else 1,
                r["fee_pct"] if r["fee_pct"] is not None else 9.9, r["code"])
        prev = best.get(r["share_key"])
        if prev is None or rank < prev[0]:
            best[r["share_key"]] = (rank, r)
    c["dup"] = c["eligible"] - len(best)
    return [best[k][1] for k in sorted(best)], c


def group_fund(workers: int, log, ctx: dict) -> tuple[list, dict]:
    purchase = ctx.get("purchase")
    if purchase is None or len(purchase) == 0:
        purchase = _frame_or_none(ds.fetch_fund_purchase_snapshot)
    if purchase is None or len(purchase) == 0:
        return [_skip_record("fund", "fund_nav", "all", "无场外基金申购表，跳过 OTC 组")], {}
    pool4, funnel = _otc_lt4(purchase)
    recs = []
    nav_items = []
    for r in pool4:
        nav_items.append(_item(f"场外净值 {r['code']}", "fund_nav",
                               f"{r['code']}__{NAV_FUND_INDICATOR}",
                               functools.partial(ds.fetch_fund_nav_history, r["code"],
                                                 NAV_FUND_INDICATOR),
                               True, keep=True))
    nav_recs = _exec("fund.otc_nav", nav_items, workers, log, keep=True)
    metrics = {}
    for r in nav_recs:
        if not r.get("ok"):
            continue
        m = _nav_metrics(r.get("result"))
        if m is not None:
            metrics[str(r["key"]).split("__")[0]] = m
    for r in nav_recs:
        r.pop("result", None)
    recs += nav_recs
    pool6, c56 = _otc_l5_l6(pool4, metrics)
    funnel.update(c56)
    funnel["L5"] = c56["eligible"]
    funnel["L6"] = len(pool6)
    # L7 规模：invest otc.py 对 L6 幸存者逐只 fetch_fund_scale()（雪球基金档案，
    # 失败进程内熔断后走平台兜底）。这里一次性预热，19:00 消费链就不必再联网；
    # 查不到不淘汰（与 otc.py 的 fail-open 口径一致）。
    # 雪球 2026-11 起时好时坏（需登录态）：先探 120 只，可用率过低说明源不可用，
    # 剩余跳过并在 manifest 记明——别把 20 分钟预算浪费在注定返回 None 的请求上。
    def _scale_plan(codes: list) -> list:
        return [_item(f"规模 {c}", "fund_scale", c,
                      functools.partial(ds.fetch_fund_scale, c), keep=True)
                for c in codes]

    def _scale_values(recs_: list) -> dict:
        vals = {}
        for r in recs_:
            v = r.get("result")
            r.pop("result", None)
            if not r.get("ok") or isinstance(v, bool):
                continue
            try:
                fv = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not math.isnan(fv):
                vals[str(r["key"])] = fv
        return vals

    codes6 = [r["code"] for r in pool6]
    probe_n = min(SCALE_PROBE, len(codes6))
    scale_recs = _exec("fund.otc_scale", _scale_plan(codes6[:probe_n]), workers, log,
                       keep=True)
    xq_scale = _scale_values(scale_recs)
    ratio = (len(xq_scale) / probe_n) if probe_n else 0.0
    funnel["L7_scale_probe"] = {"n": probe_n, "available": len(xq_scale),
                                "ratio": round(ratio, 3)}
    skipped = 0
    if ratio >= SCALE_PROBE_MIN_RATIO and probe_n < len(codes6):
        rest = _exec("fund.otc_scale", _scale_plan(codes6[probe_n:]), workers, log,
                     keep=True)
        scale_recs += rest
        xq_scale.update(_scale_values(rest))
    elif probe_n < len(codes6):
        skipped = len(codes6) - probe_n
        funnel["L7_scale_skipped"] = skipped
        if log:
            log(f"  [fund.otc_scale] 探针可用率 {ratio:.0%} < "
                f"{SCALE_PROBE_MIN_RATIO:.0%}（规模源不可用），跳过剩余 {skipped} 只")
    recs += scale_recs
    funnel["L7_scale_checked"] = len(scale_recs)
    funnel["L7_scale_available"] = len(xq_scale)
    # 平台规模表（ETF 总市值口径，etf_scale_map）+ 本次逐只结果合并后只做淘汰。
    scale_map = dict(ctx.get("scale_map") or {})
    scale_map.update(xq_scale)
    if scale_map:
        kept, small = [], 0
        for r in pool6:
            sc = scale_map.get(r["code"])
            if sc is not None and sc < OTC["min_scale_yi"]:
                small += 1
                continue
            kept.append(r)
        funnel["L7_scale_small"] = small
        funnel["L7"] = len(kept)
        pool7 = kept
    else:
        funnel["L7_scale_small"] = 0
        funnel["L7"] = len(pool6)
        pool7 = pool6
    fee_items = []
    for r in pool7:
        for ind in FEE_INDICATORS:
            fee_items.append(_item(f"费率 {r['code']} {ind}", "fund_fee",
                                   f"{r['code']}__{ind}",
                                   functools.partial(ds.fetch_fund_fee, r["code"], ind)))
    fee_recs = _exec("fund.otc_fee", fee_items, workers, log)
    recs += fee_recs
    funnel["L8_fee_targets"] = len(pool7) * len(FEE_INDICATORS)
    funnel["L8_fee_failed"] = sum(1 for r in fee_recs if not r["ok"])
    if funnel["L8_fee_failed"]:
        funnel["L8_failed_codes"] = sorted({str(r["key"]).split("__")[0]
                                            for r in fee_recs if not r["ok"]})[:40]
    return recs, {"funnel": funnel, "pool4": len(pool4), "fee_codes": len(pool7),
                  "scale_codes": len(scale_recs), "scale_available": len(xq_scale)}


# ---------------------------------------------------------------- stocks
def group_stocks(workers: int, log, pool: list,
                 bar_expected: str = "") -> tuple[list, dict]:
    if not pool:
        return [_skip_record("stocks", "daily", "all", "股票池为空（index_cons 未取到）")], {}
    items = []
    for c in pool:
        sym = ds.sina_symbol(c)
        items.append(_item(f"日线 {c}", "daily", f"{sym}__{PRICE_YEARS}y",
                           functools.partial(ds.fetch_daily, c), True))
        items.append(_item(f"财务 {c}", "fin", c, functools.partial(ds.fetch_fin, c), True))
        for ind in VALUATION_INDICATORS:
            cn = VAL_CN.get(ind, ind)
            items.append(_item(f"估值 {c} {ind}", "val", f"{c}__{cn}",
                               functools.partial(ds.fetch_valuation, c, ind)))
    recs = _exec("stocks", items, workers, log, bar_expected=bar_expected)
    return recs, {"pool": len(pool)}


# ---------------------------------------------------------------- weekly（货基历史）
def _money_hist_targets() -> list:
    p = Path(CASH_POOL_JSON)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in (data.get("funds") or []):
        c = row.get("code")
        if c:
            out.append(str(c).strip().zfill(6))
    return sorted(set(out))


def group_weekly(workers: int, log) -> tuple[list, dict]:
    targets = _money_hist_targets()
    if not targets:
        return [_skip_record("weekly", "money_hist", "all",
                             f"货基池文件不存在或为空：{CASH_POOL_JSON}")], {}
    # 只取最近 MONEY_HIST_DAYS 个自然日：cash_pool.py 只消费 tail(180)，全历史要翻
    # 241 页/只 ≈40–60 秒，98 只就是 1.5 小时；窗口路径约 4 秒/只。缓存键不变，
    # 后续 cash_pool 不带 days 也能命中这份窗口缓存（它只要最后 180 行）。
    items = [_item(f"货基历史 {c}", "money_hist", c,
                   functools.partial(ds.fetch_money_fund_hist, c, days=MONEY_HIST_DAYS))
             for c in targets]
    return _exec("weekly", items, workers, log), {"money_hist_targets": len(targets)}


# ---------------------------------------------------------------- 覆盖自检
def _scan_counts(dd: str) -> dict:
    counts = {}
    root = _cache_root()
    if not root.is_dir():
        return counts
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            n = sum(1 for _ in d.glob(f"*__{dd}.csv"))
        except OSError:
            n = 0
        if n:
            counts[d.name] = n
    return counts


def _expected_trade_date(dd: str) -> str:
    p = _cache_path("calendar", "trade")
    if not p.is_file():
        return ""
    try:
        df = pd.read_csv(p, usecols=["trade_date"])
        s = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
    except Exception:  # noqa: BLE001
        return ""
    if not len(s):
        return ""
    s = s[s <= pd.Timestamp(dd)]
    return str(s.max().date()) if len(s) else ""


def _coverage(dd: str, expected: dict, skip_min=()) -> dict:
    counts = _scan_counts(dd)
    problems, tolerated = [], []
    for kind, need in REQUIRED_MIN.items():
        if kind in skip_min:
            continue
        have = counts.get(kind, 0)
        if have < need:
            problems.append(f"{kind}: {have} < 必需 {need}")
    if "etf_spot" not in skip_min and not (counts.get("etf_spot_tx") or counts.get("etf_spot")):
        problems.append("etf_spot_tx/etf_spot: 0（ETF 全表行情缺失）")
    exp = {k: v for k, v in expected.items() if v}
    for kind, want in exp.items():
        have = counts.get(kind, 0)
        if have >= want:
            continue
        ratio, absmin = TOLERANCE.get(kind, (0.0, 0))
        gap = want - have
        allowed = max(absmin, int(want * ratio))
        if allowed > 0 and gap <= allowed:
            tolerated.append(f"{kind}: {have}/{want}（容差内）")
        else:
            problems.append(f"{kind}: {have} < 应取 {want}")
    return {"ok": not problems, "counts": counts, "expected": exp,
            "problems": problems, "tolerated": tolerated}


# ---------------------------------------------------------------- 记录文件
def _json_default(o):
    if isinstance(o, (datetime,)):
        return o.isoformat(timespec="seconds")
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:  # noqa: BLE001
            return str(o)
    return str(o)


def _fetch_mode_txt(stats: dict | None) -> str:
    """取数模式摘要（增量省了多少、有多少回退全量）：写进 summary.md / PASS 日志。"""
    s = stats or {}
    parts = []
    fin_inc = int(s.get("fin_incremental", 0))
    fin_full = int(s.get("fin_full", 0))
    fin_fb = int(s.get("fin_incr_fallback", 0))
    if fin_inc or fin_full or fin_fb:
        parts.append(f"fin 增量 {fin_inc} / 全量 {fin_full} / 回退 {fin_fb}")
    nav_ap = int(s.get("nav_append", 0))
    nav_re = int(s.get("nav_reuse", 0))
    nav_full = int(s.get("nav_full", 0))
    nav_fb = int(s.get("nav_incr_fallback", 0))
    if nav_ap or nav_re or nav_full or nav_fb:
        parts.append(f"nav 追加 {nav_ap} / 复用 {nav_re} / 全量 {nav_full} / 回退 {nav_fb}")
    return "；".join(parts) if parts else "n/a"


def _summary_md(m: dict) -> str:
    lines = [f"# stock prefetch 取数记录 {m['data_date']}", ""]
    lines.append(f"- 结果：**{'PASS' if m['ok'] else 'FAIL'}**，退出码 {m['rc']}，"
                 f"耗时 {m['seconds']:.1f}s（{m['seconds'] / 60:.1f} 分钟）")
    bar_txt = "n/a" if m["bar_ok"] is None else ("OK" if m["bar_ok"] else "不匹配")
    etf_txt = "n/a" if m.get("etf_bar_ok") is None else ("OK" if m["etf_bar_ok"] else "不匹配")
    lines.append(f"- 数据日：{m['data_date']}　应有交易日：{m['expected_trade_date'] or 'n/a'}"
                 f"　最后一根指数 bar：{m['data_bar_date'] or 'n/a'}"
                 f"（{bar_txt}）")
    lines.append(f"- ETF 日线末根 bar：{m.get('etf_bar_date') or 'n/a'}（{etf_txt}）")
    lines.append(f"- 统一数据目录：`{m['shared_cache']}`")
    lines.append(f"- 统一记录目录：`{m['snapshot_dir']}`")
    failed_items = [it for it in m["items"] if not it.get("ok")]
    failed_kinds = {}
    for it in failed_items:
        failed_kinds[it["kind"]] = failed_kinds.get(it["kind"], 0) + 1
    top = "、".join(f"{k} {v}" for k, v in sorted(failed_kinds.items(), key=lambda kv: -kv[1])[:4])
    lines.append(f"- 取数条目：{m['totals']['items']} 条，失败 {m['totals']['failed']} 条"
                 + (f"（{top}；逐条见下）" if top else ""))
    if m.get("fetch_mode"):
        lines.append(f"- 取数模式（跨日增量）：{_fetch_mode_txt(m['fetch_mode'])}")
    if m.get("purged_failed"):
        lines.append(f"- 启动清理负缓存标记：{len(m['purged_failed'])} 个")
    lines += ["", "## 分组", "", "| 组 | 条目 | 失败 | 网络 | 缓存 | 耗时(s) |", "|---|---|---|---|---|---|"]
    for name, g in m["groups"].items():
        lines.append(f"| {name} | {g['items']} | {g['failed']} | {g['net']} | {g['cache']} | {g['seconds']:.1f} |")
    lines += ["", "## 覆盖自检", "", "| kind | 文件数 | 应取 | 判据 |", "|---|---|---|---|"]
    cov = m["coverage"]
    tolerated_kinds = {t.split(":", 1)[0].strip() for t in cov["tolerated"]}
    kinds = sorted(set(list(cov["counts"].keys()) + list(cov["expected"].keys())))
    for k in kinds:
        have = cov["counts"].get(k, 0)
        want = cov["expected"].get(k, "")
        if not want or have >= want:
            flag = "OK"
        else:
            flag = "容差内" if k in tolerated_kinds else "缺口"
        lines.append(f"| {k} | {have} | {want} | {flag} |")
    if cov["problems"]:
        lines += ["", "**缺口：**"]
        lines += [f"- {p}" for p in cov["problems"]]
    if cov["tolerated"]:
        lines += ["", "容差内：" + "；".join(cov["tolerated"])]
    funnel = m.get("funnel") or {}
    if funnel:
        lines += ["", "## 场外债基漏斗（stock 侧复现 invest otc.py）", ""]
        order = ["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8_fee_targets",
                 "L8_fee_failed", "navfail", "hist_short", "dd_deep", "dup", "L7_scale_small"]
        lines.append("　".join(f"{k}={funnel[k]}" for k in order if k in funnel))
    fails = [r for r in m["items"] if not r["ok"] and not r.get("skipped")]
    if fails:
        lines += ["", "## 失败条目（前 60 条）", ""]
        for r in fails[:60]:
            lines.append(f"- {r['kind']}/{r['key']}：{str(r.get('error', ''))[:180]}")
        if len(fails) > 60:
            lines.append(f"- … 另有 {len(fails) - 60} 条，见 manifest.json")
    lines.append("")
    return "\n".join(lines) + "\n"


def _atomic_text(path: Path, text: str) -> None:
    """原子写文本文件（CORE-11③）：manifest/summary/latest/history 不能被读到半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _write_outputs(m: dict, log) -> dict:
    dd = m["data_date"]
    snap_dir = Path(config.SNAPSHOTS_DIR) / dd
    snap_dir.mkdir(parents=True, exist_ok=True)
    m["snapshot_dir"] = str(snap_dir)
    manifest_path = snap_dir / "manifest.json"
    _atomic_text(manifest_path, json.dumps(m, ensure_ascii=False, indent=2,
                                           default=_json_default))
    summary_obj = {k: m[k] for k in ("schema", "tool", "version", "data_date", "ok", "rc",
                                     "seconds", "started_at", "finished_at", "args",
                                     "shared_cache", "snapshot_dir", "expected_trade_date",
                                     "data_bar_date", "bar_ok", "etf_bar_date", "etf_bar_ok",
                                     "coverage", "totals", "fetch_mode",
                                     "groups", "funnel", "purged_failed", "warnings")}
    _atomic_text(snap_dir / "summary.json",
                 json.dumps(summary_obj, ensure_ascii=False, indent=2, default=_json_default))
    _atomic_text(snap_dir / "summary.md", _summary_md(m))
    latest = {"schema": m["schema"], "tool": m["tool"], "version": m["version"],
              "data_date": dd, "ok": m["ok"], "rc": m["rc"],
              "started_at": m["started_at"], "finished_at": m["finished_at"],
              "seconds": m["seconds"], "args": m["args"],
              "items": m["totals"]["items"], "failed": m["totals"]["failed"],
              "manifest": str(manifest_path),
              "summary_json": str(snap_dir / "summary.json"),
              "summary_md": str(snap_dir / "summary.md"),
              "shared_cache": m["shared_cache"],
              "expected_trade_date": m["expected_trade_date"],
              "data_bar_date": m["data_bar_date"],
              "bar_ok": m["bar_ok"],
              "etf_bar_date": m.get("etf_bar_date"),
              "etf_bar_ok": m.get("etf_bar_ok"),
              "fetch_mode": m.get("fetch_mode") or {},
              "counts": m["coverage"]["counts"],
              "coverage_ok": m["coverage"]["ok"]}
    _atomic_text(Path(config.SHARED_DIR) / "latest.json",
                 json.dumps(latest, ensure_ascii=False, indent=2, default=_json_default))
    # history.jsonl：同一天重跑覆盖当天行，不重复追加
    hist = Path(config.SHARED_DIR) / "snapshots" / "history.jsonl"
    lines = []
    if hist.is_file():
        try:
            for ln in hist.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    if json.loads(ln).get("data_date") == dd:
                        continue
                except Exception:  # noqa: BLE001
                    pass
                lines.append(ln)
        except OSError:
            lines = []
    lines.append(json.dumps({"data_date": dd, "ok": m["ok"], "rc": m["rc"],
                             "finished_at": m["finished_at"],
                             "seconds": round(m["seconds"], 1),
                             "items": m["totals"]["items"],
                             "failed": m["totals"]["failed"],
                             "coverage_ok": m["coverage"]["ok"],
                             "data_bar_date": m["data_bar_date"]},
                            ensure_ascii=False, default=_json_default))
    try:
        _atomic_text(hist, "\n".join(lines) + "\n")
    except OSError as exc:  # noqa: BLE001
        m["warnings"].append(f"history.jsonl 写入失败：{exc}")
    try:
        prov = ds.dump_provenance("prefetch_provenance")
        if log:
            log(f"  [记录] provenance → {prov}")
    except Exception as exc:  # noqa: BLE001
        m["warnings"].append(f"provenance 写入失败：{exc}")
    return latest


def _version() -> str:
    try:
        from . import __version__  # type: ignore[attr-defined]
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------- main
GROUP_ORDER = ("ref", "index", "etf", "fund", "stocks")
GROUP_HELP = {
    "ref": "参考表（日历/板块快照/场外申购/日净值/货基排行/利率/逆回购）",
    "index": "指数日线 + 指数成分（两技能股票池声明）",
    "etf": "ETF 全表行情 + 规模表 + ETF 日线（成交额池∪pin）",
    "fund": "场外债基候选的累计净值 + 费率（复现 invest otc.py L1-L6）",
    "stocks": "指数成分并集逐只 日线/财务/估值",
    "weekly": "货基历史（朝朝盈2号池，翻页慢，默认不进夜间任务）",
}


def _split(v: str) -> list:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _plan_text(active: list) -> str:
    lines = [f"{TOOL} 计划（{datetime.now().isoformat(timespec='seconds')}）",
             f"  共享缓存：{config.SHARED_CACHE_DIR}",
             f"  统一记录：{config.SNAPSHOTS_DIR}"]
    for g in active:
        lines.append(f"  - {g}: {GROUP_HELP.get(g, '')}")
    return "\n".join(lines)


def _min_skips(active: list) -> set:
    """本次没跑的组，其 kind 不参与「硬性下限」判定（--only/--skip 演练用）。"""
    drop = set()
    if "ref" not in active:
        drop |= {"calendar", "market_snapshot"}
    if "stocks" not in active:
        drop |= {"daily", "fin"}
    if "etf" not in active:
        drop |= {"etf_daily", "etf_spot"}
    if "index" not in active:
        drop |= {"index_daily", "index_cons"}
    return drop


def _intraday_block(date_arg: str, allow_intraday: bool, dd: str,
                    now: datetime | None = None) -> bool:
    """交易日收盘前跑 prefetch 是否应拒绝（避免把「今天」的键写成未收盘数据）。

    背景：``_cached`` 按「数据日」命名缓存文件。若交易日盘中（含凌晨）跑一次，
    当天键里会装满「上一交易日」的 bar；19:00 计划任务读缓存时看到当天键已存在，
    直接命中、不再刷新 → 静默消费陈旧数据（2026-11-17 00:4x 演练真实制造过 8573 个）。
    生产任务 19:00 触发，正常不受影响；盘中演练用 ``--allow-intraday`` 或
    ``--date YYYY-MM-DD`` 回放。
    """
    if date_arg or allow_intraday:
        return False
    now = now or datetime.now()
    exp_today = _expected_trade_date(dd)
    is_trading = (exp_today == dd) if exp_today else now.weekday() < 5
    return is_trading and now.hour * 60 + now.minute < 15 * 60 + 5


def run(args) -> int:
    t_all = time.time()
    was_offline = ds.offline()          # _init 会清掉 A_SHARE_OFFLINE（prefetch 必须联网），先记下
    _init(force=bool(getattr(args, "force", False)), date=str(getattr(args, "date", "") or ""))
    dd = ds.data_date()
    workers = int(getattr(args, "workers", 0) or os.environ.get("STOCK_PREFETCH_WORKERS") or 8)
    workers = max(1, min(64, workers))
    quiet = bool(getattr(args, "quiet", False))

    # 休市日直接退出：调休补班的周末交易所也不开市（判据=交易日历，不是星期几）。
    if (not getattr(args, "force", False) and not getattr(args, "verify_only", False)
            and not getattr(args, "dry_run", False)
            and not str(getattr(args, "date", "") or "") and not ds.is_trading_day()):
        if not quiet:
            print("今日休市（最近交易日 %s）：跳过取数；要强制取数加 --force"
                  % (ds.last_trading_day() or "-"), flush=True)
        return 0

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    only = _split(getattr(args, "only", ""))
    skip = set(_split(getattr(args, "skip", "")))
    if getattr(args, "skip_stocks", False):
        skip.add("stocks")
    active = [g for g in GROUP_ORDER if (not only or g in only) and g not in skip]
    if getattr(args, "with_weekly", False) and (not only or "weekly" in only):
        active.append("weekly")
    if "stocks" in active and "index" not in active:
        active.insert(active.index("stocks"), "index")
    active = [g for g in GROUP_ORDER + ("weekly",) if g in active]

    if getattr(args, "verify_only", False):
        exp = {}
        latest = Path(config.SHARED_DIR) / "latest.json"
        manifest = Path(config.SNAPSHOTS_DIR) / dd / "manifest.json"
        for src in (manifest, latest):
            if not src.is_file():
                continue
            try:
                j = json.loads(src.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if j.get("data_date") != dd:
                continue
            if src is manifest:
                # 用当天 manifest 里「应取数量」复检：文件被人删/清理后能立刻发现
                exp = {k: v for k, v in (j.get("coverage", {}).get("expected") or {}).items() if v}
            break
        cov = _coverage(dd, exp)
        bar = ""
        p = _cache_path("index_daily", "000300")
        if p.is_file():
            try:
                bar = _max_bar(pd.read_csv(p, usecols=["date"]))
            except Exception:  # noqa: BLE001
                bar = ""
        exp_trade = _expected_trade_date(dd)
        bar_ok = (bar == exp_trade) if (bar and exp_trade) else None
        print(json.dumps({"data_date": dd, "coverage": cov, "data_bar_date": bar,
                          "expected_trade_date": exp_trade, "bar_ok": bar_ok,
                          "shared_cache": str(config.SHARED_CACHE_DIR)},
                         ensure_ascii=False, indent=2, default=_json_default))
        return 0 if cov["ok"] and bar_ok is not False else 2

    if getattr(args, "dry_run", False):
        print(_plan_text(active))
        return 0

    log(f"[prefetch] 数据日 {dd} | 组 {','.join(active)} | workers {workers} | "
        f"共享缓存 {config.SHARED_CACHE_DIR}")
    if was_offline:
        print("[prefetch] 错误：A_SHARE_OFFLINE=1 时不能取数（这是本机更新脚本；"
              "20:00 冻结会话只读缓存）", file=sys.stderr)
        return 1
    # 交易日收盘前跑会把「今天」的缓存键写成未收盘数据：19:00 计划任务随后会把它们当
    # 缓存命中而跳过刷新（2026-11-17 00:4x 的演练就制造过 8573 个这样的键，已手工清理）。
    # 生产任务 19:00 触发不受影响；盘中演练请 --allow-intraday 或 --date 回放。
    if _intraday_block(str(getattr(args, "date", "") or ""),
                       bool(getattr(args, "allow_intraday", False)), dd):
        print("[prefetch] 错误：现在是交易日收盘前（<15:05），此时取数会把「今天」的缓存键"
              "写成未收盘数据，19:00 计划任务会把它当缓存命中而不再刷新。"
              "确需盘中演练请加 --allow-intraday，或用 --date YYYY-MM-DD 回放上一交易日。",
              file=sys.stderr)
        return 3
    purged = _purge_failed(dd)
    if purged:
        log(f"[prefetch] 清理当日负缓存标记 {len(purged)} 个")

    started = datetime.now().isoformat(timespec="seconds")
    groups, all_recs, funnel = {}, [], {}
    warnings, errors = [], []
    ctx = {}
    stats = {}

    def run_group(name: str, fn, *a):
        t0 = time.time()
        try:
            recs, c = fn(*a)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log(f"  [{name}] 组异常：{type(exc).__name__}: {exc}")
            if not quiet:
                traceback.print_exc()
            recs, c = [_skip_record(name, name, "group", f"组异常：{exc}")], {}
        groups[name] = recs
        all_recs.extend(recs)
        if c:
            ctx.update(c)
        n_ok = sum(1 for r in recs if r["ok"])
        stats[name] = {"items": len(recs), "failed": len(recs) - n_ok,
                       "net": sum(1 for r in recs if not r.get("from_cache")),
                       "cache": sum(1 for r in recs if r.get("from_cache")),
                       "seconds": round(time.time() - t0, 1)}
        log(f"[group] {name} 完成 {time.time() - t0:.1f}s")

    # 1) ref：串行前置（后面 OTC 要它的申购表）
    if "ref" in active:
        run_group("ref", group_ref, workers, log)
    # 参考组写完 calendar 后才知道「应有交易日」；后面的 bar 类数据靠它做同日陈旧自愈。
    exp_trade = _expected_trade_date(dd)
    # 2) index：拿到两技能股票池声明的成分
    if "index" in active:
        run_group("index", group_index, workers, log, exp_trade)
    # 2.5) etf 前置表：全表行情 + 规模表先串行取好。fund 组的 L7 规模兜底要读
    # etf_scale_map（内部同样读写 etf_spot_tx/all），两组并冷启动会重复打腾讯行情
    # 并并发写同一份缓存；先取一次，后面 fund/etf.hist 都命中缓存。
    if "etf" in active:
        run_group("etf.base", group_etf_base, workers, log)
    # 3) etf 日线 / fund / stocks 三组并行（不同域名，互不阻塞）
    pool = ctx.get("pool") or []
    parallel = [g for g in ("etf", "fund", "stocks") if g in active]
    if parallel:
        with ThreadPoolExecutor(max_workers=len(parallel)) as ex:
            futs = {}
            if "etf" in parallel:
                futs[ex.submit(run_group, "etf", group_etf, workers, log, ctx,
                               exp_trade)] = "etf"
            if "fund" in parallel:
                futs[ex.submit(run_group, "fund", group_fund, workers, log, ctx)] = "fund"
            if "stocks" in parallel:
                futs[ex.submit(run_group, "stocks", group_stocks, workers, log, pool,
                               exp_trade)] = "stocks"
            for fut in as_completed(futs):
                fut.result()
    # 4) weekly：货基历史（慢）
    if "weekly" in active:
        run_group("weekly", group_weekly, workers, log)

    # ---- 汇总
    expected = {}
    if "index" in active:
        expected["index_daily"] = len(INDEX_DAILY)
        expected["index_cons"] = len(INDEX_CONS)
    if "etf" in active:
        expected["etf_daily"] = len(ctx.get("hist_codes") or ETF_PIN)
        expected["etf_universe"] = 1
    if "fund" in active:
        f = ctx.get("funnel") or {}
        expected["fund_nav"] = int(f.get("L4") or 0)
        expected["fund_fee"] = int(f.get("L8_fee_targets") or 0)
    if "stocks" in active:
        n = int(ctx.get("pool") or 0)
        expected["daily"] = n
        expected["fin"] = n
        expected["val"] = n * len(VALUATION_INDICATORS)
    if "weekly" in active:
        expected["money_hist"] = int(ctx.get("money_hist_targets") or 0)

    cov = _coverage(dd, expected, skip_min=_min_skips(active))
    # 锚点只看 000300 单根（不能取 max）：2026-11-17 首跑被其它指数的新 bar 掩盖了
    # 000300 停在 T-1 的陈旧缓存，bar_ok 误报 true → 数据准备 整批 purge。
    bar = _anchor_bar(all_recs)
    # ETF 日线也要校验 bar 日（CORE-11②）：只看 index_daily 会放过「ETF 全是 T-1」的
    # 数据日——B-7 就是这样让 13 笔 ETF 条件单拿着 T-1 的价判成当日无法成交。
    etf_bars = sorted({r["max_bar_date"] for r in all_recs
                       if r.get("kind") == "etf_daily" and r.get("max_bar_date")})
    etf_bar = etf_bars[0] if etf_bars else ""      # 最旧的那只 = 全体下限
    bar_ok = (bar == exp_trade) if (bar and exp_trade) else None
    etf_bar_ok = (etf_bar == exp_trade) if (etf_bar and exp_trade) else None
    allow_stale = bool(getattr(args, "allow_stale_bar", False))
    if bar_ok is False and not allow_stale:
        cov["problems"].append(
            f"index_daily/000300 末根 bar={bar} != 应有交易日 {exp_trade}"
            "（数据准备 会 purge 共享缓存当日键）")
        cov["ok"] = False
    if etf_bar_ok is False and "etf" in active and not allow_stale:
        cov["problems"].append(
            f"etf_daily 最旧末根 bar={etf_bar} != 应有交易日 {exp_trade}"
            f"（{sum(1 for r in all_recs if r.get('kind') == 'etf_daily'
                     and r.get('max_bar_date') and r['max_bar_date'] < etf_bars[-1])} 只落后）")
        cov["ok"] = False
    total_secs = time.time() - t_all
    failed = [r for r in all_recs if not r["ok"] and not r.get("skipped")]
    m = {
        "schema": SCHEMA, "tool": TOOL, "version": _version(),
        "data_date": dd, "started_at": started,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(total_secs, 1),
        "args": {"workers": workers, "only": only, "skip": sorted(skip),
                 "with_weekly": bool(getattr(args, "with_weekly", False)),
                 "force": bool(getattr(args, "force", False)),
                 "skip_stocks": bool(getattr(args, "skip_stocks", False))},
        "shared_cache": str(config.SHARED_CACHE_DIR),
        "snapshot_dir": str(Path(config.SNAPSHOTS_DIR) / dd),
        "expected_trade_date": exp_trade, "data_bar_date": bar, "bar_ok": bar_ok,
        "etf_bar_date": etf_bar, "etf_bar_ok": etf_bar_ok,
        "coverage": cov,
        "fetch_mode": ds.fetch_stats(),
        "totals": {"items": len(all_recs), "failed": len(failed),
                   "from_cache": sum(1 for r in all_recs if r.get("from_cache"))},
        "groups": stats,
        "items": [{k: v for k, v in r.items() if k != "result"} for r in all_recs],
        "funnel": ctx.get("funnel") or {},
        "purged_failed": purged,
        "warnings": warnings,
    }
    rc = 0
    if errors:
        rc = 1
    if not cov["ok"]:
        rc = 2
    m["ok"] = rc == 0
    m["rc"] = rc
    latest = _write_outputs(m, log)
    log("")
    log(f"[prefetch] {'PASS' if m['ok'] else 'FAIL'} rc={rc} | 数据日 {dd} | "
        f"耗时 {total_secs / 60:.1f} 分钟 | 条目 {m['totals']['items']}（失败 {m['totals']['failed']}）| "
        f"bar {bar or 'n/a'} (应有 {exp_trade or 'n/a'})")
    if m.get("fetch_mode"):
        log(f"[prefetch] 取数模式：{_fetch_mode_txt(m['fetch_mode'])}")
    log(f"[prefetch] 统一记录 → {m['snapshot_dir']}\\manifest.json")
    if cov["problems"]:
        for p in cov["problems"]:
            log(f"[prefetch] 缺口：{p}")
    if errors:
        for e in errors:
            log(f"[prefetch] 异常：{e}")
    if getattr(args, "json", False):
        print(json.dumps(latest, ensure_ascii=False, indent=2, default=_json_default))
    return rc
