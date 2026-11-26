# -*- coding: utf-8 -*-
r"""数据层：取数接口层（东财/新浪/中证/百度/同花顺/腾讯/金十/交易所）封装 + 磁盘缓存 + 重试 + 溯源。

职责
----
各策略技能（invest）共用的数据层：本模块是唯一实现，技能侧只注入各自的
config 模块，共享同一份缓存、同一套自检。详见 D:\skills\invest\stock\README.md「取数接口层」。

三条铁律
--------
1. 每个数字都要能追到「哪个接口、哪天取的」—— 写入 provenance。
2. 接口失败就抛异常，绝不返回空值冒充数据（fail loud，不静默降级为中性）。
3. 同一自然日内重复运行走缓存，避免把接口打挂。

⚠️ 缓存类型陷阱（踩过一次）：
   CSV 缓存读回来时 "000063" 会变成整数 63、日期列会变成字符串。
   所以每个 fetch 都带一个 `post` 归一化函数，**缓存命中与接口新取都走同一套**
   类型转换，否则会出现「第一次跑通、第二次跑全 NaN」的诡异现象。

配置注入（重要）
----------------
本模块**不在 import 时读配置**：同一进程里可能有两个技能各自的 WORK_DIR/CACHE_DIR。
技能侧的兼容层先 `init(技能config, flavor=...)`，之后所有 fetch 才可用；
未初始化就调用会抛 RuntimeError 并说明原因。

本机数据源现状（2026-11-15 实跑复核）
-------------------------------------
- 新浪系（日线/财务/ETF 日线/交易日历）、中证指数、百度估值：可用；
- ETF 行情/规模主源：同花顺 ``fund_etf_spot_ths`` ETF 全列表 × 腾讯 ``qt.gtimg.cn``
  批量行情（60 只/批，含成交额与「总市值」≈规模亿元）；失败回退新浪 ETF 全表；
- 东方财富 push2 系（``fund_etf_spot_em`` / ``fund_etf_hist_em`` / push2his 日线）：
  本机出口被风控（TLS 直接断开 / RemoteDisconnected），默认关闭，置
  ``EM_ENABLED=1`` 显式开启；基金类东财接口（lsjz、fund_money_*）不受影响；
- 雪球基金档案 ``fund_individual_basic_info_xq``：蛋卷公开接口（不带登录态），会因风控返回
  空数据（抛 ``ValueError``）、偶发限流；连续失败 3 次才进程内熔断 120 秒，规模按
  「东财 ``fund_scale_em`` → ETF 总市值」逐级兜底（见 ``fetch_fund_scale``）；
- 若某个接口整体失效，必须在调用处给出兜底并写 notes（估值/ETF 规模即如此），
  不能让单只数据缺失把整池判死（见各 fetch 的 docstring）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request

import pandas as pd

from ..codes import ETF_PREFIXES, etf_symbol, is_etf_code, norm_code, to_sina

# ---------------------------------------------------------------- 注入态
_C = None                    # 技能的 config 模块（init 时注入）
FLAVOR = "invest"             # 技能标识：少数源选择/后处理按它分支
ROOT = ""                    # stock 包根（D:\skills\invest\stock）
CACHE_ROOT = ""              # 技能缓存目录（config.CACHE_DIR）
OUT_ROOT = ""                # 技能产出目录（config.OUT_DIR）
_SCALE_CACHE_FILE = ""       # ETF 规模 last-good 缓存文件
_MARKET_HOOK = None          # 技能侧可注册的全市场快照后处理（invest 的行业归一）
_CACHE_HOOK = None           # 测试钩子：fn(kind, key) -> 缓存文件路径（见 set_cache_hook）


def init(config_module, flavor="invest"):
    """注入技能配置并做一次性初始化；幂等（重复调用只更新配置）。"""
    global _C, FLAVOR, ROOT, CACHE_ROOT, OUT_ROOT, _SCALE_CACHE_FILE
    global _FUND_SCALE_XQ_BROKEN, _FUND_SCALE_LAST_SOURCE
    for attr in ("CACHE_DIR", "OUT_DIR"):
        if not hasattr(config_module, attr):
            raise RuntimeError(f"datasource.init: 配置缺少 {attr}")
    _C = config_module
    FLAVOR = str(flavor)
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CACHE_ROOT = _C.CACHE_DIR
    OUT_ROOT = _C.OUT_DIR
    _SCALE_CACHE_FILE = os.path.join(
        _C.CACHE_DIR, getattr(_C, "SCALE_CACHE_FILENAME", "etf_scale_lastgood.json"))
    # 进程级 memo / 熔断 / 规模快照状态都跟「数据日」绑定：init 时统一重置，
    # 避免同一进程反复 init（测试、paper 重跑）时串到上一个数据日（CORE-1/13）。
    _SPOT_BAR_MEMO.clear()
    _CAL_MEMO.clear()
    _SCALE_SAVED.clear()
    _SCALE_CACHE_STATUS.update({"mode": "none", "as_of": None, "source": None, "stale_days": None})
    _FUND_SCALE_XQ_BROKEN = False
    _FUND_SCALE_LAST_SOURCE = "none"
    _FETCH_STATS.clear()
    _PREV_DIR_MEMO.clear()
    _install_ipv4_first()
    return True


def set_market_hook(fn):
    """注册全市场快照后处理钩子（callable(df)->df）；传 None 取消。"""
    global _MARKET_HOOK
    _MARKET_HOOK = fn


def set_cache_hook(fn):
    """测试钩子：fn(kind, key) -> 缓存文件路径；传 None 恢复默认。

    为什么需要：实现搬到 stock 后，技能里的 datasource.py 只是转出层，
    `ds._cache_file = ...` 这种 monkeypatch 打在转出层的名字上不会生效
    （函数真正查的是本模块的全局名）。测试里用这个钩子把缓存指到临时 CSV
    （见 stock/tests/test_datasource_fixes.py）。
    """
    global _CACHE_HOOK
    _CACHE_HOOK = fn


def _config():
    if _C is None:
        raise RuntimeError(
            "datasource 尚未初始化：请先调用 datasource.init(技能的config, flavor=...)；"
            "技能里的 datasource.py 兼容层会自动完成这一步。")
    return _C


def _install_ipv4_first():
    """进程级 IPv4 优先：本机到东财的 IPv6 出口被对端断开，强制 IPv4 才通。

    2026-11-14 实测：push2.eastmoney.com 解析出 AAAA，TLS 被立刻断开
    （schannel: remote party requests renegotiation → server closed abruptly），
    浏览器同样 ERR_EMPTY_RESPONSE；curl -4 立即 200。
    关闭方式：技能的 config.FORCE_IPV4=False（缺省视为开启）。
    """
    if not getattr(_C, "FORCE_IPV4", True):
        return
    if getattr(_install_ipv4_first, "_done", False):
        return
    _orig = socket.getaddrinfo

    def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        infos = _orig(host, port, family, type, proto, flags)
        if family in (0, socket.AF_UNSPEC):
            v4 = [inf for inf in infos if inf[0] == socket.AF_INET]
            if v4:
                return v4
        return infos

    socket.getaddrinfo = _ipv4_first
    _install_ipv4_first._done = True



_prov: list[dict] = []


# ---------------------------------------------------------------- 基础工具
def today() -> str:
    return dt.date.today().isoformat()


def _now() -> dt.datetime:
    """当前本地时间。单拎出来是为了让「收盘后补 ETF 当日 bar」的时点判断可测。"""
    return dt.datetime.now()


# ============================ 数据日 / 离线冻结（2026-11-16 架构改造）================
# 新架构（用户 2026-11-16 口径）：
#   数据更新与巡检全部在本机自跑（Windows 计划任务 \\DSH-DailyPatrol，交易日 19:00 起），
#   DSH 只做筛选与记录；决策日（周三/周六 20:30）**不跑取数**，读本机最后一份完整快照。
# 因此数据层需要两个开关：
#   A_SHARE_DATA_DATE=YYYY-MM-DD  把「读/写哪一天的数据」钉死（默认=真实今天）
#   A_SHARE_OFFLINE=1             只读缓存，缺什么就报错，绝不联网
# 两者都由 team.py 的决策前置校验设置，日常取数/巡检不设置 → 行为与改造前完全一致。
def data_date() -> str:
    """数据日（缓存文件名的日期键）：默认真实今天；A_SHARE_DATA_DATE 可钉住。"""
    raw = os.environ.get("A_SHARE_DATA_DATE", "").strip()
    if not raw:
        return today()
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise RuntimeError(
            f"A_SHARE_DATA_DATE 不是 YYYY-MM-DD：{raw!r}（决策日冻结数据时由 team.py 设置）")


def offline() -> bool:
    """A_SHARE_OFFLINE=1 → 只读缓存、绝不联网（决策日冻结数据用）。"""
    return os.environ.get("A_SHARE_OFFLINE", "").strip().lower() in ("1", "true", "yes")


_OFFLINE_BLOCKS: list[dict] = []      # 本进程内被离线模式拦下的取数请求（count 进 provenance）


def _offline_guard(kind: str, key: str, path: str) -> None:
    """离线模式下缓存没有这份数据 → 明确报错（不静默降级成空值，也不偷偷联网）。"""
    item = {"kind": kind, "key": key, "expect": path}
    _OFFLINE_BLOCKS.append(item)
    _prov.append({"kind": kind, "key": key, "source": "offline_blocked",
                  "expected": os.path.basename(path)})
    raise RuntimeError(
        f"离线模式拦截取数：{kind}/{key} 无数据日 {data_date()} 的缓存"
        f"（期待 {os.path.basename(path)}）。本机每日取数（stock prefetch / 计划任务 DSH-DailyPatrol）"
        f"未覆盖这份数据；请先跑 `python -m stock prefetch`，或由数据官补齐后重试。")


def ensure_dirs() -> None:
    os.makedirs(CACHE_ROOT, exist_ok=True)
    os.makedirs(OUT_ROOT, exist_ok=True)


def _safe_key(key: str) -> str:
    """缓存键 → 文件名安全片段（允许 alnum 与 ``-_``，其余替换成 ``_``，截 120 字）。"""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in key)[:120]


def _cache_file(kind: str, key: str) -> str:
    if _CACHE_HOOK is not None:      # 测试注入口（见 set_cache_hook）
        return _CACHE_HOOK(kind, key)
    if not CACHE_ROOT:
        # 没有注入缓存根（裸进程直接 import）时绝不能往当前工作目录写缓存：
        # 之前就这样在 stock/dividends/ 和当前目录下的 cninfo_market/ 各漏过一份。
        raise RuntimeError("数据层缓存根未初始化：先 import 技能 config 或 set_cache_hook()")
    d = os.path.join(CACHE_ROOT, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_safe_key(key)}__{data_date()}.csv")


# ---------------------------------------------------------------- 取数模式统计
# 本进程内「增量/全量」计数：prefetch 会把它写进 manifest/summary，便于每天核对
# 「今晚省了多少请求」以及「有多少只回退成全量」。只统计 fetch_fin / fund_nav 两条增量路径。
_FETCH_STATS: dict[str, int] = {}
_FETCH_STATS_LOCK = threading.Lock()


def _count(mode: str, n: int = 1) -> None:
    with _FETCH_STATS_LOCK:
        _FETCH_STATS[mode] = _FETCH_STATS.get(mode, 0) + n


def fetch_stats(reset: bool = False) -> dict[str, int]:
    """本进程取数模式计数快照：{模式: 次数}；``reset=True`` 取完清空。

    模式口径：``fin_incremental`` / ``fin_full`` / ``fin_incr_fallback``、
    ``nav_append``（从 bulk 表追加了新行）/ ``nav_reuse``（无需追加）/ ``nav_full`` /
    ``nav_incr_fallback``。
    """
    with _FETCH_STATS_LOCK:
        out = dict(_FETCH_STATS)
        if reset:
            _FETCH_STATS.clear()
    return out


# ---------------------------------------------------------------- 跨日缓存复用
# 缓存文件按「数据日」命名（`<safe>__YYYY-MM-DD.csv`）：每天全量冷取。fin（新浪财务，
# 每只 1+8 页）与 fund_nav（东财逐只全历史）合计约占 prefetch 墙钟的 80%，所以让它们
# 只取「今天变化的那几行」，往年/历史行复用上一份缓存。任何不确定都必须回退全量。
_CACHE_NAME_RE = re.compile(r"^(?P<safe>.+?)__(?P<day>\d{4}-\d{2}-\d{2})\.csv$")
_PREV_CACHE_MAX_AGE_DAYS = 8          # 断跑超过一周就重新全量，避免拿太旧的基线拼历史
_PREV_DIR_MEMO: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
_PREV_DIR_LOCK = threading.Lock()


def _prev_index(kind: str) -> dict[str, tuple[str, str]]:
    """kind 目录下「日期 < 数据日」的最近一份缓存：{safe: (day, path)}（每数据日只扫一次）。"""
    today = data_date()
    k = (kind, today)
    with _PREV_DIR_LOCK:
        hit = _PREV_DIR_MEMO.get(k)
        if hit is not None:
            return hit
    d = os.path.join(CACHE_ROOT, kind)
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    try:
        today_day = dt.date.fromisoformat(today)
    except ValueError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for n in names:
        m = _CACHE_NAME_RE.match(n)
        if not m:
            continue
        day = m.group("day")
        if day >= today:
            continue                    # 今天刚写的文件不能当「昨日基线」
        try:
            age = (today_day - dt.date.fromisoformat(day)).days
        except ValueError:
            continue
        if age <= 0 or age > _PREV_CACHE_MAX_AGE_DAYS:
            continue
        cur = out.get(m.group("safe"))
        if cur is None or day > cur[0]:
            out[m.group("safe")] = (day, os.path.join(d, n))
    with _PREV_DIR_LOCK:
        _PREV_DIR_MEMO[k] = out
    return out


def _prev_cache_file(kind: str, key: str) -> str | None:
    """同 (kind,key) 上一份按日缓存路径；测试钩子/无基线/超龄 → None（调用方回退全量）。"""
    if _CACHE_HOOK is not None:
        return None
    hit = _prev_index(kind).get(_safe_key(key))
    return hit[1] if hit else None


def _force_refresh() -> bool:
    """A_SHARE_FORCE_REFRESH=1（技能侧 run.py --refresh）或 STOCK_REFRESH=1（stock 自身）
    → 忽略当天缓存，重新取数。

    为什么需要：缓存文件按「日期」命名，同一天内多次运行会复用当天第一次抓的数据。
    如果当天中午先跑过一次（拿到的是上一交易日收盘），当晚的决策会继续用
    那份旧数据——报告看起来「和上次一模一样」，实际是数据没更新，不是策略没变。
    默认关闭（保护接口、保证同一天内可复现）；只有人工要「最新收盘」时才显式刷新。
    """
    truthy = ("1", "true", "yes", "on")
    return (os.environ.get("A_SHARE_FORCE_REFRESH", "").strip().lower() in truthy
            or os.environ.get("STOCK_REFRESH", "").strip().lower() in truthy)


def _fix_code_col(series) -> pd.Series:
    """把代码列恢复成 6 位字符串："682" → "000682"；非纯数字（HS300/sh600000）原样保留。

    ⚠️ 这是本项目**第二次**踩同一个坑：CSV 里 "000682" 被 pandas 推断成整数 682，
    缓存回读后 code 就悄悄变成 "682"。code 是连接键，下游按 code 关联产品时会静默错配
    （第一次是旧注记里的 "000063" → 63）。
    为什么不能一律 zfill：`"HS300".zfill(6)` = `"0HS300"`，会把指数/带前缀代码写坏；
    所以只有「纯数字且长度 ≤6」才补零，其它原样返回。
    """
    s = pd.Series([str(v).strip() for v in series.tolist()], index=series.index)
    s = s.str.replace(r"\.0$", "", regex=True)   # 682.0 → 682（float 读回）

    def _z(v: str) -> str:
        return v.zfill(6) if v.isdigit() and len(v) <= 6 else v

    return s.map(_z)


_KEY_LOCKS: dict[tuple[str, str], "threading.Lock"] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(kind: str, key: str) -> "threading.Lock":
    """同一 (kind, key) 的进程内锁：冷启动并发去重（CORE-8 single-flight）。"""
    k = (kind, key)
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(k)
        if lock is None:
            lock = _KEY_LOCKS[k] = threading.Lock()
        return lock


def _cached(kind: str, key: str, loader, post=None, fail_ttl_h: float = 0.0,
            force: bool = False):
    """读缓存或调 loader；两条路径都过 post。loader 抛错则向上抛（不吞）。

    `force=True` 时忽略当天缓存重新取数（并覆盖缓存）——只给「盘中写过一次、
    收盘后必须拿到最新值」的少数调用用（ETF 当日 bar 补齐用的 ETF 行情表）。

    `fail_ttl_h > 0` 时启用**负缓存**：本次失败会在同目录留下 `<key>.failed` 标记，
    在 TTL 内再调直接抛同样的错、不再打接口。

    为什么需要：`_cached` 只缓存成功。像场外费率这种「一只基金一个请求、跑 1000+ 次」的
    批量取数，第一次跑（冷缓存）必然有一批瞬时失败（超时/限流），第二次跑会去重试它们、
    多数成功——于是两次跑拿到的池子不同（实测 1011 只 vs 1080 只），
    `determinism.py` 判不可复现，而这不是代码回归、是网络掷骰子。
    加了负缓存后，同一天内的失败集合被固定住：失败即「本期不入池」（fail-closed，
    不按 0 费率冒充），要立刻重试就删标记或设 A_SHARE_FORCE_REFRESH=1。
    """
    path = _cache_file(kind, key)
    fail = (path[:-4] if path.endswith(".csv") else path) + ".failed"
    off = offline()
    # 离线模式（决策日冻结快照）下忽略强制刷新：A_SHARE_FORCE_REFRESH 是「允许联网重取」的
    # 授权，与「绝不联网」互斥，冻结优先——paper.py 的陈旧自愈会临时置这个变量，不能因此
    # 让决策日跑出联网取数。
    force = (force or _force_refresh()) and not off

    def _mark_fail():
        if fail_ttl_h > 0:
            try:
                with open(fail, "w", encoding="utf-8") as _f:
                    _f.write(dt.datetime.now().isoformat(timespec="seconds"))
            except OSError:
                pass

    if fail_ttl_h > 0 and os.path.exists(fail) and not force and not off:
        try:
            age = time.time() - os.path.getmtime(fail)
        except OSError:
            age = 1e9
        if age < fail_ttl_h * 3600:
            raise RuntimeError(
                f"{kind}/{key}: {age / 60:.0f} 分钟前取数失败，负缓存 {fail_ttl_h:g}h 内不再重试；"
                f"要立刻重试就删 {fail} 或设 A_SHARE_FORCE_REFRESH=1")

    def _apply(df):
        # 缓存回读路径统一先修代码列：各 fetch 的 post 可能有「已归一化则 early-return」
        # 分支，一旦 read_csv 把 "000682" 推断成整数，那些分支就没机会修（见 _fix_code_col）。
        if "code" in df.columns:
            df["code"] = _fix_code_col(df["code"])
        return post(df.copy()) if post else df

    # 冷启动 single-flight（CORE-8）：并发线程同时 miss 同一 (kind,key) 时只让一个真的
    # 去取数，其余等它写完直接读缓存；否则 8 线程会把同一接口打 8 遍（还并发写同一份 CSV）。
    with _key_lock(kind, key):
        if os.path.exists(path) and not force:
            try:
                df = pd.read_csv(path)
                if len(df) > 0:
                    _prov.append({"kind": kind, "key": key, "source": "cache",
                                  "cached_at": dt.datetime.fromtimestamp(
                                      os.path.getmtime(path)).isoformat(timespec="seconds")})
                    return _apply(df)
            except Exception:
                pass  # 缓存损坏 → 重取

        if off:                      # 离线模式：缓存没有就明确报错，绝不联网
            _offline_guard(kind, key, path)

        t0 = time.time()
        try:
            df = loader()
        except Exception:
            _mark_fail()
            raise
        if not isinstance(df, pd.DataFrame) or len(df) == 0:
            _mark_fail()
            raise ValueError(f"{kind}/{key}: 接口返回空表")
        df = _apply(df)
        if len(df) == 0:
            # post 过滤后为空：如果照常写「只有表头的 CSV」，下次 read_csv 读回 0 行、
            # 命中失败又重取，永久循环且每次都打接口（CORE-12）→ 记负缓存并报错。
            _mark_fail()
            raise ValueError(f"{kind}/{key}: post 处理后为空")
        # 原子写：prefetch 会同时跑 etf/fund/stocks 三组线程，同一份缓存可能被别组读到；
        # 非原子 to_csv 会让读者看到半截文件（read_csv 要么报错重取、要么更糟地被当成
        # 有效缓存读进少几行）。tmp + os.replace 保证读者只会看到完整旧文件或完整新文件。
        # 写失败必须 fail loud：接口取到的数据没法落盘，下次还得重取，不能当成成功。
        if not _write_cache_atomic(df, path, kind=kind, key=key):
            raise OSError(f"{kind}/{key}: 写缓存失败（见 stderr / provenance cache_write_error）")
        try:                      # 这次成功了，清掉可能残留的失败标记
            if os.path.exists(fail):
                os.remove(fail)
        except OSError:
            pass
        _prov.append({"kind": kind, "key": key,
                      "source": "endpoints(force)" if _force_refresh() else "endpoints",
                      "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                      "secs": round(time.time() - t0, 2), "rows": int(len(df))})
        return df


def _retry(fn, tries: int = 3, base_sleep: float = 1.2):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(base_sleep * (2 ** i))
    raise RuntimeError(f"重试 {tries} 次仍失败: {type(last).__name__}: {last}") from last


# ---------------------------------------------------------------- 代码格式


def sina_symbol(code) -> str:
    """代码 → 新浪 symbol（``sh600519`` / ``sz159915`` / ``bj430047``）。

    2026-11-16 修复（invest 182 只 ETF 取数卡 45 分钟的根因）：原实现只按**股票**
    首位规则拼前缀（6/9→sh、0/2/3→sz、其余→bj），场内 ETF 全部拼错——
    5xxxxx（510300/511360/588000）和 15/16xxxx（159845/160632）都被拼成 ``bj``，
    新浪基金接口 404，单只重试 4 次 ≈15s，一步吃满 1800s 超时。
    现统一走 :func:`stock.codes.parse` 的市场判断（6/9→sh、0/2/3→sz、
    15/16/18→sz、5→sh、4/8→bj），股票/指数口径不变。
    """
    return to_sina(norm_code(code))


# ---------------------------------------------------------------- 归一化 post
def _post_codes(df: pd.DataFrame) -> pd.DataFrame:
    if "code" in df.columns:
        s = df["code"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        # 只给「纯数字且 ≤6 位」补零：'nan'/None/带前缀的代码原样保留，
        # 否则 `"nan".zfill(6)` = "000nan"（CORE-17，与 _fix_code_col 口径对齐）。
        digit = s.str.fullmatch(r"\d{1,6}", na=False)
        df["code"] = s.where(~digit, s.str.zfill(6))
    if "name" in df.columns:
        df["name"] = df["name"].astype(str).str.strip()
    return df


def _post_daily(df: pd.DataFrame) -> pd.DataFrame:
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume", "amount", "outstanding_share", "turnover"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return (df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True))


def _post_fin(df: pd.DataFrame) -> pd.DataFrame:
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    out = df.dropna(subset=["日期"])
    if len(out) == 0:
        # 全非法日期不能返回空表冒充成功（CORE-17）：post 之后为空由 _cached 记负缓存。
        raise ValueError("fin: 财务表日期列全部无效")
    return out.sort_values("日期").reset_index(drop=True)


def _post_val(df: pd.DataFrame) -> pd.DataFrame:
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().sort_values("date").reset_index(drop=True)


def _post_index_daily(df: pd.DataFrame) -> pd.DataFrame:
    return _post_daily(df)


# ---------------------------------------------------------------- 股票池
def fetch_index_cons(index_code: str) -> pd.DataFrame:
    """中证指数官方成分股。返回 code/name。"""
    from .. import endpoints as ak

    def load():
        df = ak.index_stock_cons_csindex(symbol=index_code)
        return pd.DataFrame({"code": df["成分券代码"].astype(str),
                             "name": df["成分券名称"].astype(str)})

    return _cached("index_cons", index_code, load,
                   post=lambda d: _post_codes(d).drop_duplicates("code").reset_index(drop=True))


def fetch_index_daily(index_code: str) -> pd.DataFrame:
    """指数日线（新浪），用于基准、相对强弱与风浪模型的量能项。

    早期改动（相对 invest 原版）：保留 open/high/low/volume。
    风浪模型的「量能萎缩」项需要 volume；ATR 需要 high/low。原版只留 date/close。
    """
    from .. import endpoints as ak

    # 指数 symbol 也统一走 codes 的市场判断：000xxx/930xxx/950xxx（上证/中证系列）
    # → sh、399xxx（深证系列）→ sz。原实现只认 "000" 开头，带前缀的 "sh000300"
    # 会被拼成 "szsh000300"；930/950 系列会被错拼成 sz。
    sym = to_sina(norm_code(index_code), "index")

    def load():
        df = ak.stock_zh_index_daily(symbol=sym)
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], unit="ms")
        cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
        return df[cols]

    return _cached("index_daily", norm_code(index_code), load, post=_post_index_daily)


# ---------------------------------------------------------------- 个股
def fetch_daily(code, years: int | None = None) -> pd.DataFrame:
    """个股日线（新浪，前复权）。含 outstanding_share → 可算总市值。"""
    from .. import endpoints as ak

    years = years or _config().PRICE_YEARS
    # 用「数据日」而不是真实今天（CORE-4）：钉住 A_SHARE_DATA_DATE 回放/冻结时，
    # 真实 today 会让 end_date 走到未来（前视），start_date 也跟着漂移。
    end = dt.date.fromisoformat(data_date())
    start = end - dt.timedelta(days=int(365 * years + 40))
    sym = sina_symbol(code)

    def load():
        df = ak.stock_zh_a_daily(symbol=sym, start_date=start.strftime("%Y%m%d"),
                                 end_date=end.strftime("%Y%m%d"), adjust="qfq")
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df

    # 缓存键必须带 years：改了窗口长度就得重新取，否则会静默复用短窗口
    return _cached("daily", f"{sym}__{years}y", load, post=_post_daily)


def _fin_full_refresh_today(code: str, dd: str | None = None) -> bool:
    """今天是否该把这只股票的财务表全量重取一次（每只每周一档，周一~周五摊开）。

    为什么需要：增量只抓「本年 + 上年」两页，往年行一律复用缓存；如果新浪事后修订了
    往年报表（重述/补录），只靠增量永远看不到。按代码散列到星期几做全量对账，保证
    任何一只股票的历史修订最多 7 天内被吸收；周末兜底全量（prefetch 周末不自跑）。
    """
    try:
        wd = dt.date.fromisoformat(dd or data_date()).weekday()
    except ValueError:
        return True
    if wd > 4:                  # 周六/周日（演练/补跑）：没有轮值日，直接全量
        return True
    try:
        return int(code[-6:]) % 5 == wd
    except ValueError:
        return True


def _fin_incremental(code: str, start_year: str) -> pd.DataFrame | None:
    """财务表跨日增量：往年行复用上一份缓存 + 只抓「本年/上年」两页。

    返回 ``None`` 表示「无法安全增量」→ 调用方回退全量（宁慢勿错）。等价性已实测
    （2026-11-17 对 6 只抽样，与全量逐年抓页的结果在列名/列序/dtype/逐值/行序上完全一致），
    要点：
    - 只对**新抓的两页**做 ``dropna()``（与全量函数收尾口径一致：对原始字符串做，
      ``'--'`` 不算 NaN）；缓存里的往年行早已是数值+NaN，**绝不能再 dropna**，否则会
      把「当年披露值全为空」的旧报告期整行删掉；
    - 列集/列序必须与缓存完全一致，否则回退全量（接口改版时不能拼出错列）；
    - 丢弃缓存里「>= 上年」的行，由新抓的两页接管，避免与旧数据重复。
    """
    from ..endpoints import sina as _sina

    base_path = _prev_cache_file("fin", f"{code}__{start_year}")
    if not base_path:
        return None
    try:
        base = pd.read_csv(base_path)
    except Exception:            # noqa: BLE001 —— 缓存损坏 → 全量
        return None
    if len(base) == 0 or "日期" not in base.columns:
        return None
    base["日期"] = pd.to_datetime(base["日期"], errors="coerce")
    base = base.dropna(subset=["日期"])
    if len(base) == 0:
        return None

    cur_year = int(data_date()[:4])
    years = [str(cur_year - 1), str(cur_year)]
    raw = _sina.stock_financial_analysis_indicator_years(code, years)
    if raw is None or len(raw) == 0:
        return None
    raw = raw.dropna()
    if len(raw) == 0:
        return None
    new = raw.reset_index()
    if "index" not in new.columns:                 # index 已有名字时 reset_index 不叫 index
        new = new.rename(columns={new.columns[0]: "日期"})
    else:
        new = new.rename(columns={"index": "日期"})
    if list(new.columns) != list(base.columns):
        return None
    new["日期"] = pd.to_datetime(new["日期"], errors="coerce")
    new = new.dropna(subset=["日期"])
    if len(new) == 0:
        return None
    for col in new.columns[1:]:
        new[col] = pd.to_numeric(new[col], errors="coerce")

    keep = base[base["日期"].dt.year < cur_year - 1]
    merged = pd.concat([keep, new], ignore_index=True)
    if len(merged) == 0:
        return None
    return merged.sort_values("日期", ignore_index=True)


def fetch_fin(code) -> pd.DataFrame:
    """财务指标（新浪，按报告期，累计口径）。

    取数模式（2026-11 起）：默认**跨日增量**——复用上一份缓存里的往年行，只抓
    「本年 + 上年」两页（约 2 个请求，原全量为 1+8 个）；每只股票每周全量对账一次
    （:func:`_fin_full_refresh_today`）。强制刷新、缓存缺失/损坏、列结构变化、页面解析
    异常都自动回退全量，输出与全量口径完全一致。
    """
    from .. import endpoints as ak

    c = norm_code(code)
    start_year = _config().FIN_START_YEAR

    def load():
        if (not _force_refresh() and not _fin_full_refresh_today(c)
                and _env_on("STOCK_FIN_INCREMENTAL")):
            try:
                inc = _fin_incremental(c, start_year)
            except Exception:      # noqa: BLE001 —— 增量路径任何异常都回退全量
                inc = None
            if inc is not None:
                _count("fin_incremental")
                return inc
            _count("fin_incr_fallback")
        _count("fin_full")
        df = ak.stock_financial_analysis_indicator(symbol=c, start_year=start_year)
        return df.copy()

    # 缓存键带 FIN_START_YEAR（CORE-16）：改起点后必须重新取，不能静默复用短窗口。
    return _cached("fin", f"{c}__{start_year}", load, post=_post_fin)


# 百度估值接口的 indicator 只认中文名（2026-11 实测）；英文/简称统一映射到中文。
_VALUATION_INDICATORS = {
    "pe": "市盈率(TTM)",
    "pe_ttm": "市盈率(TTM)",
    "pe_static": "市盈率(静)",
    "市盈率": "市盈率(TTM)",
    "pb": "市净率",
    "ps": "市现率",
    "pcf": "市现率",
    "mktcap": "总市值",
    "总市值": "总市值",
}


def fetch_valuation(code, indicator: str) -> pd.Series:
    """百度估值历史（PE/PB/PS/总市值）。返回 date → value。

    2026-11 实测的两个坑：
    1) `ak.stock_zh_valuation_baidu` 的 ``indicator`` 只认中文名
       {"总市值", "市盈率(TTM)", "市盈率(静)", "市净率", "市现率"}；
       传 "pe" 会直接抛 ``TypeError: 'NoneType' object is not subscriptable``
       （百度接口内部按字典取值失败），看起来像接口坏了，其实是参数非法。
       这里统一做中英映射，调用方仍可传 "pe"/"pb"。
    2) 估值历史是 best-effort 数据，单只股票取不到只能算「该维度缺失」，
       不能让它把整只股票判死（screen.py 的 ``_eval_stock`` 里一抛就把 505 只
       股票全变 data_ok=False，整池空转）——所以吞掉异常返回空 Series，
       由 screen.py 用当日快照 PE 兜底并写 notes。

    `fail_ttl_h=12`：负缓存把「接口不可用」固定住，同一天内不再每只股票重打一次。
    """
    from .. import endpoints as ak

    c = norm_code(code)
    ind = _VALUATION_INDICATORS.get(str(indicator).strip().lower(), str(indicator))

    def load():
        df = ak.stock_zh_valuation_baidu(symbol=c, indicator=ind,
                                         period=_config().VALUATION_PERIOD)
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], unit="ms")
        return df

    try:
        df = _cached("val", f"{c}__{ind}", load, post=_post_val, fail_ttl_h=12)
    except Exception:  # noqa: BLE001 —— 估值历史不可用按缺失处理，不阻断选池
        return pd.Series(dtype=float, name=indicator)
    if df is None or len(df) == 0 or "value" not in df.columns:
        return pd.Series(dtype=float, name=indicator)
    return pd.Series(df["value"].values, index=pd.DatetimeIndex(df["date"]), name=indicator)


# ---------------------------------------------------------------- ETF / 基金
# ETF 双源（v0.3.17）：东财 push2 在本机出口被风控（2026-11-14 实测连浏览器都
# ERR_EMPTY_RESPONSE，冷却 5 分钟后仍 TLS 直断），改为：
# - 同花顺 ``fund_etf_spot_ths()``：1783 只 ETF 代码/名称（只作代码池，无行情）；
# - 腾讯 ``qt.gtimg.cn``：60 只/批，含现价/昨收/开盘/高/低/成交量/成交额（万元）/
#   换手率/「总市值（亿元）」——ETF 的总市值 ≈ 最新价×总份额 ≈ 基金规模，作规模主源；
# - 行情与规模同表缓存（etf_spot_tx），一次跑批只打约 30 个腾讯请求。
_TX_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")


def _fetch_etf_universe_ths() -> pd.DataFrame:
    """同花顺 ETF 全列表（代码池）：约 1783 只，含最新单位净值；无成交额。"""
    from .. import endpoints as ak

    def load():
        return _retry(lambda: ak.fund_etf_spot_ths().copy(), tries=3, base_sleep=2.0)

    def post(df):
        need = ("基金代码", "基金名称")
        miss = [c for c in need if c not in df.columns]
        if miss:
            raise KeyError(
                f"fund_etf_spot_ths 返回表结构变了：缺 {miss}；现有 {list(df.columns)[:20]}")
        out = pd.DataFrame({
            "code": _pick(df, "基金代码").astype(str).str.strip().str.zfill(6),
            "name": _pick(df, "基金名称").astype(str).str.strip(),
        })
        out = out[out["code"].str.match(r"^\d{6}$")
                  & out["code"].str.startswith(ETF_PREFIXES)]
        return out.drop_duplicates("code").reset_index(drop=True)

    return _cached("etf_universe", "ths", load, post=post)


def _tx_quote_batch(symbols: list[str]) -> list[dict]:
    """腾讯批量行情（≤60 只/次）；返回统一字段。接口是 GBK，必须按 gbk 解。"""
    qs = ",".join(symbols)
    req = urllib.request.Request(
        "https://qt.gtimg.cn/q=" + qs,
        headers={"User-Agent": _TX_UA, "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode("gbk", "replace")
    rows: list[dict] = []
    for line in txt.splitlines():
        if "=" not in line or '"' not in line:
            continue
        v = line.split('"', 1)[1].split("~")
        if len(v) < 46:
            continue
        try:
            code = str(v[2]).strip().zfill(6)
            price = float(v[3])
        except (TypeError, ValueError, IndexError):
            continue

        def _f(i):
            try:
                return float(v[i])
            except (TypeError, ValueError, IndexError):
                return None

        rows.append({
            "code": code,
            "name": str(v[1]).strip(),
            "price": price,
            "prev_close": _f(4),
            "open": _f(5),
            "high": _f(33),
            "low": _f(34),
            "volume": _f(6),           # 手
            "amount": _f(37),          # 万元（loader 里换算成元）
            "aum_yi": _f(45) or _f(44),  # 总市值/流通市值（亿元）≈ 基金规模
            "turnover_pct": _f(38),
            "quote_time": str(v[30]).strip(),
        })
    return rows


def _fetch_etf_spot_tx() -> pd.DataFrame:
    """ETF 行情+规模主源：同花顺代码池 × 腾讯批量行情（loader，金额在此换算成元）。"""
    uni = _fetch_etf_universe_ths()
    codes = uni["code"].tolist()
    rows: list[dict] = []
    batch = int(getattr(_config(), "TX_QUOTE_BATCH", 60))
    sleep_s = float(getattr(_config(), "TX_QUOTE_SLEEP", 0.25))
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        rows.extend(_tx_quote_batch([etf_symbol(c) for c in chunk]))
        if i + batch < len(codes):
            time.sleep(sleep_s)
    if not rows:
        raise RuntimeError("腾讯 ETF 行情为空（qt.gtimg.cn 未返回可解析行）")
    df = pd.DataFrame(rows)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 1e4   # 万元→元
    df["aum_yi"] = pd.to_numeric(df["aum_yi"], errors="coerce")
    return df.sort_values(["amount", "code"], ascending=[False, True]).reset_index(drop=True)


def _etf_spot_tx_cached(force: bool = False) -> pd.DataFrame:
    """缓存版腾讯 ETF 表（当天一次；post 只做类型归一，不做二次换算）。

    `force=True`：忽略当天缓存重新抓（行情表盘中写过一次、收盘后要最终值）。
    """
    def post(df):
        for c in ("price", "prev_close", "open", "high", "low", "volume",
                  "amount", "aum_yi", "turnover_pct"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["price"]).reset_index(drop=True)

    return _cached("etf_spot_tx", "all", _fetch_etf_spot_tx, post=post, force=force)



def fetch_etf_spot() -> pd.DataFrame:
    """ETF 全表（统一旧列名）：v0.3.17 主源 = 同花顺池 + 腾讯行情；失败回退新浪。

    返回列：代码/名称/最新价/成交额/成交量/涨跌幅（下游 screen/storm/paper 不感知源）。
    """
    try:
        tx = _etf_spot_tx_cached()
        out = pd.DataFrame({
            "代码": tx["code"].astype(str),
            "名称": tx["name"].astype(str),
            "最新价": pd.to_numeric(tx["price"], errors="coerce"),
            "成交额": pd.to_numeric(tx["amount"], errors="coerce"),
            "成交量": pd.to_numeric(tx["volume"], errors="coerce"),
        })
        prev = pd.to_numeric(tx.get("prev_close"), errors="coerce")
        out["涨跌幅"] = (out["最新价"] / prev - 1.0) * 100
        out = out.dropna(subset=["最新价"]).reset_index(drop=True)
        if len(out) >= 500:      # 池子明显小于 500 只说明源降级了，回退新浪
            return out
    except Exception:  # noqa: BLE001 —— 腾讯不可用 → 回退新浪 ETF 全表
        pass

    from .. import endpoints as ak

    def load():
        # 新浪在并发抓取时会返回 HTML（反爬）而不是 JSON，必须重试，不能一次就判失败
        return _retry(lambda: ak.fund_etf_category_sina(symbol="ETF基金").copy(),
                      tries=5, base_sleep=3.0)

    def post(df):
        df["代码"] = df["代码"].astype(str).str.strip()
        for c in ("最新价", "成交额", "成交量", "涨跌幅"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["最新价"]).reset_index(drop=True)

    return _cached("etf_spot", "all", load, post=post)


_ETF_EM_COLS = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover",
}


def _etf_hist_em(code: str) -> pd.DataFrame:
    """ETF 日线（东财，前复权）。带 amount/换手率，是 ETF 流动性与拥挤度的主数据源。"""
    from .. import endpoints as ak

    end = dt.date.fromisoformat(data_date())   # CORE-4：别用真实 today（会前视/漂移）
    start = end - dt.timedelta(days=int(365 * _config().PRICE_YEARS + 40))

    def load():
        df = _retry(
            lambda: ak.fund_etf_hist_em(
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust="qfq"),
            tries=3, base_sleep=2.0)
        return df.rename(columns=_ETF_EM_COLS).copy()

    return _cached("etf_daily_em", code, load, post=_post_daily)


# ---------------------------------------------------------------- ETF 当日 bar 补齐
# 背景（2026-11-16 B-7 事故）：19:00 数据更新时新浪 ETF 日线当天 bar 还没发布——
# 实测 11-12/11-14/11-16 三天 18:00、19:12、20:46 三次抓取的最后 bar 都停在 T-1，
# 直到 21:00 之后才出现当天 bar（invest 21:21 那次跑到了 11-16）。结果 19:30 的
# 13 笔 ETF 条件单拿着 T-1 的价/量判当前状态，当日无法成交。
#
# 更优日线源都探过，**没有一个能整段替换新浪**（证据见 stock-audit/data-layer-fixes.md）：
#   * 东财 push2his：历史上 18:00 就有当天 bar，但本机出口被风控（fund_etf_hist_em
#     实测 3 个 host 全挂、回退腾讯且 amount=NaN），且其 qfq 与新浪不复权历史价差
#     22%+（510880 2022-10-10 close 2.411 vs 2.965 = 累积分红因子），换源会改口径；
#   * 腾讯 K 线：T+0 但只有 6 列（无 amount），volume=手，adv20_yi 会直接缺；
#   * 同花顺 fuyao ``fund_hist``：修好 thscode（159845.SZ）后可用、T+0、968 根、
#     **带 amount(元)、volume=股**；但默认口径是**前复权**且 adjust 参数被服务端忽略
#     （510880 2022-10-10 close=2.411=东财 qfq），整段历史不能当不复权用——只有
#     「当天这一根」的 qfq 价 = 当日真实成交价，所以它只适合补最后一根（兜底）；
#   * 新浪 ETF 日线的另一个模板（非 klc2）与 klc2 同为 T+1。
# 所以：日线**历史段保持新浪**（不复权、amount 元、volume 股），缺当天 bar 时补最后一根，
# 补 bar 源按序：①腾讯 ``etf_spot_tx`` 行情表（批量 ~20 请求取全市场，收盘后即最终值）
# → ②同花顺 fuyao ``fund_hist``（逐只 1 请求，仅行情表不可用时兜底）。两源单位都用同日
# 实测对齐（2026-11-16 vs 21:2x 新浪最终 bar，样本 159845/510300/512100/159915/510880/
# 588000/511360）：spot OHLC 全等、volume 手×100 ≈ 股（差 <50 股）、amount 元（差 <0.002%）；
# THS OHLC 全等、volume/amount 比 1.0000（159845/510300/511360 三只实测）。
# 只补「当天 + 已收盘 + 确实在交易」的一根，且新浪当天 bar 一旦发布就自动不再补。
# 补出来的合并表会**原子写回当天缓存**（运维 要求：否则磁盘 CSV 末 bar 仍是 T-1，
# prefetch manifest/审计按 CSV 算 max_bar_date 会误报）；补失败则落 provenance
# ``etf_hist_stale``（带真实 max_bar_date），供上层 fail 判定——B-7 当时生产 ok=true
# 就是因为没有这个信号。
_SPOT_BAR_MEMO: dict = {}     # 进程内复用行情表：{(data_date, force): {"spot": df|None, "ts": 时间戳}}
_SPOT_BAR_LOCK = threading.Lock()   # single-flight：并发补 bar 时只让一个线程真的去拉行情表
_SPOT_BAR_FORCE_TTL = 300.0   # force 结果只认 5 分钟：14:00 的盘中表不能当 15:30 的收盘表用


def _etf_spot_table(force: bool = False):
    """ETF 行情表（腾讯 ``etf_spot_tx``）；进程内复用，不可用返回 None。

    8 线程并发补最后一根 bar 时会同时 miss memo：原来各拉一遍腾讯行情（8×20 请求）
    还并发写同一份缓存。加锁 + double-check，先到的线程取数，其余直接等它填 memo。

    memo 键带数据日（CORE-13）；force 结果带 TTL（CORE-2）——盘中强制刷出来的表
    只代表当时的行情，收盘后补 bar 必须重新取，不能拿 14:00 的价当收盘价。
    """
    key = (data_date(), bool(force))
    hit = _SPOT_BAR_MEMO.get(key)
    if hit is not None and (not force or time.time() - hit["ts"] < _SPOT_BAR_FORCE_TTL):
        return hit["spot"]
    with _SPOT_BAR_LOCK:
        hit = _SPOT_BAR_MEMO.get(key)
        if hit is not None and (not force or time.time() - hit["ts"] < _SPOT_BAR_FORCE_TTL):
            return hit["spot"]      # 等锁期间别的线程已经填好
        try:
            spot = _etf_spot_tx_cached(force=force)
        except Exception:      # noqa: BLE001 —— 补 bar 是增强，行情表不可用就退回新浪原样数据
            spot = None
        _SPOT_BAR_MEMO[key] = {"spot": spot, "ts": time.time()}
    return spot


def _quote_stamp(value) -> str | None:
    """行情时间 → ``YYYYMMDDHHMMSS`` 字符串（腾讯 ``quote_time``，缓存读回可能是 int）。"""
    try:
        s = str(int(float(value)))
    except (TypeError, ValueError):
        return None
    return s[:14] if len(s) >= 14 and s[:14].isdigit() else None


def _spot_max_quote(spot) -> str | None:
    """行情表里最新的行情时间；取不到返回 None。"""
    if spot is None or "quote_time" not in getattr(spot, "columns", []):
        return None
    stamps = [s for s in (_quote_stamp(v) for v in spot["quote_time"].tolist()) if s]
    return max(stamps) if stamps else None


def _spot_ready(spot, day: str) -> bool:
    """行情表是否已是 ``day`` 的收盘最终值（表内最新行情时间 ≥ 15:00）。"""
    q = _spot_max_quote(spot)
    return bool(q) and q[:8] == str(day).replace("-", "") and int(q[8:12]) >= 1500


def _spot_today_bar(spot, code: str, day: str):
    """从 ETF 行情表拼 ``day`` 当天一根 bar：date/open/high/low/close/volume(股)/amount(元)。"""
    if spot is None or "code" not in getattr(spot, "columns", []):
        return None
    codes_ = spot["code"].astype(str).str.zfill(6)
    rows = spot[codes_ == code]
    if rows.empty:
        return None
    r = rows.iloc[0]
    q = _quote_stamp(r.get("quote_time"))
    if not q or q[:8] != str(day).replace("-", ""):
        return None                            # 今天没成交（停牌/未上市）→ 不补假 bar

    def _f(name):
        try:
            return float(r.get(name))
        except (TypeError, ValueError):
            return None

    px, op, hi, lo = _f("price"), _f("open"), _f("high"), _f("low")
    vol, amt = _f("volume"), _f("amount")
    if not all(v is not None and v > 0 for v in (px, op, hi, lo)):
        return None
    if vol is None or vol < 0 or amt is None or amt < 0:
        return None
    return {"date": day, "open": op, "high": hi, "low": lo, "close": px,
            # 腾讯 volume 是「手」，新浪日线 volume 是「股」→ ×100 对齐既有契约；
            # amount 在 loader 里已把「万元」换算成元，与新浪 amount 同单位。
            "volume": vol * 100.0, "amount": amt,
            "_source": "etf_spot_today_bar"}


def _ths_today_bar(code: str, day: str):
    """同花顺 fuyao 兜底：只取 ``day`` 当天这一根 bar（行情表不可用时才走）。

    口径（2026-11-16 实测）：``fund/market/historical`` 默认是**前复权**且服务端忽略
    ``adjust`` 参数（510880 2022-10-10 close 2.411=东财 qfq，≠新浪不复权 2.965），
    所以整段历史不能换源；但「当天这根」的前复权价就是当日真实成交价。
    单位与新浪同日实测一致：volume 比 1.0000（股）、amount 比 1.000000（元），
    OHLC 全等（159845/510300/511360 三只样本）。单只 1 请求（±30 天窗口）。
    """
    if offline():          # CORE-3：冻结/离线会话绝不联网（真实数据日补 bar 也不行）
        return None
    try:
        from ..sources import fuyao

        end = dt.date.fromisoformat(day)
        df = fuyao.etf_hist(code, start=(end - dt.timedelta(days=30)).isoformat(), end=day)
    except Exception:      # noqa: BLE001 —— 兜底源，失败就退回新浪原样数据
        return None
    if df is None or len(df) == 0 or "date" not in df.columns:
        return None
    last = df.iloc[-1]
    if str(last.get("date"))[:10] != day:
        return None
    try:
        bar = {k: float(last[k]) for k in ("open", "high", "low", "close", "volume", "amount")}
    except (TypeError, ValueError):
        return None
    if not all(bar[k] > 0 for k in ("open", "high", "low", "close")):
        return None
    return {"date": day, **bar, "_source": "etf_ths_today_bar"}


def _etf_today_bar(code: str, day: str):
    """``day`` 当天一根 bar：腾讯 ETF 行情表优先，同花顺兜底（都不行返回 None）。"""
    spot = _etf_spot_table()
    if not _spot_ready(spot, day) and not offline() and day == today():
        spot = _etf_spot_table(force=True)     # 行情表是盘中缓存 → 收盘后强制刷一次
    bar = _spot_today_bar(spot, code, day) if _spot_ready(spot, day) else None
    return bar if bar is not None else _ths_today_bar(code, day)


def _write_cache_atomic(df: pd.DataFrame, path: str, kind: str | None = None,
                        key: str | None = None) -> bool:
    """原子写缓存（唯一 tmp + os.replace），避免并发读到半截文件。

    tmp 名带 pid/线程 id：prefetch 的多个组可能同时写同一份缓存（如 etf 组与 fund 组
    都要写 etf_spot_tx/all），共用 ``path + ".tmp"`` 会互相截断写入。os.replace 在同一
    卷上是原子的，读者要么看到完整旧文件、要么看到完整新文件。

    返回是否成功；失败**不静默**：记一条 provenance ``cache_write_error``（manifest/
    审计可见）+ stderr 一行警告，由调用方决定是否 fail loud。
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    err: OSError | None = None
    try:
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
    except OSError as e:          # 磁盘满 / 无权限
        err = e
    if err is None:
        # Windows 上 os.replace 可能撞上「目标被读者/杀毒短暂占用」（WinError 5/32）。
        # 重试几次；同一卷上的 replace 是原子操作，成功后读者只会看到完整文件。
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError as e:
                err = e
                time.sleep(0.05 * (attempt + 1))
            except OSError as e:
                err = e
                break
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    _prov.append({"kind": kind or "cache", "key": key or os.path.basename(path),
                  "source": "cache_write_error",
                  "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "error": f"{type(err).__name__}: {err}", "path": path})
    print(f"[datasource] 警告：写缓存失败 {path}: {err}", file=sys.stderr)
    return False


def _append_today_bar(df: pd.DataFrame, code: str, key: str | None = None) -> pd.DataFrame:
    """新浪 ETF 日线缺当天 bar 时补最后一根，并把合并结果写回当天缓存（幂等）。

    ``key``：缓存键（``etf_daily/<key>``）。传 ``None`` 表示这批数据不是新浪不复权
    口径（东财 qfq 分支）——不补、不写缓存，避免把不复权 spot bar 混进 qfq 序列。

    新浪已发布当天 bar / 已经补过（缓存里有当天 bar）时原样返回。两个兜底源都不可用
    时**不静默**：落一条 provenance ``etf_hist_stale``（带真实 ``max_bar_date``），
    供 prefetch manifest / prep verify 判定——B-7 当时生产 ok=true 就是因为没这个信号。
    """
    if df is None or len(df) == 0 or "date" not in df.columns:
        return df
    day = data_date()
    last = pd.to_datetime(df["date"].iloc[-1], errors="coerce")
    if pd.isna(last) or last.date().isoformat() >= day or key is None:
        return df                              # 新浪已发布当天 bar（或已补过）
    max_bar_date = last.date().isoformat()
    if day == today():
        now = _now()                           # 收盘（15:05）之前不补，盘中更不补
        if now.hour * 60 + now.minute < 15 * 60 + 5 or now.weekday() >= 5:
            # 盘中/周末不补（CORE-11）：明确留一条 provenance，审计时能区分
            # 「确定不补」和「补失败」，而不是两者都只是「末 bar 停在旧日期」。
            _prov.append({"kind": "etf_daily", "key": key, "source": "etf_today_bar_pending",
                          "max_bar_date": max_bar_date, "data_date": day,
                          "note": "未到收盘（15:05）或周末，未补当天 bar"})
            return df
    bar = _etf_today_bar(code, day)
    if bar is None:
        _prov.append({"kind": "etf_daily", "key": key, "source": "etf_hist_stale",
                      "max_bar_date": max_bar_date, "data_date": day,
                      "note": "新浪当日 bar 未发布，ETF 行情表/同花顺兜底都不可用；"
                              "缓存末 bar 仍停在旧交易日，调用方须按 max_bar_date 判断"})
        return df
    row = {c: bar.get(c) for c in df.columns}
    row["date"] = pd.Timestamp(day)
    out = _post_daily(pd.concat([df, pd.DataFrame([row])], ignore_index=True))
    wrote = _write_cache_atomic(out, _cache_file("etf_daily", key), kind="etf_daily", key=key)
    _prov.append({"kind": "etf_daily", "key": key, "source": bar.get("_source"),
                  "date": day, "max_bar_date": day,
                  "note": "新浪当日 bar 未发布，补最后一根（volume 股、amount 元，口径对齐）"
                          if wrote else
                          "新浪当日 bar 未发布，补最后一根但**写缓存失败**（磁盘末 bar 仍为 "
                          f"{max_bar_date}，见 cache_write_error）"})
    return out


def fetch_etf_hist(sym, adjust: str = "") -> pd.DataFrame:
    """ETF 日线（新浪历史段，不复权，含 amount）。入参可以是裸代码或带前缀代码。

    ``adjust="qfq"`` 时改走东财前复权（分红下沉，序列连续，ATR/波动类计算用它；
    东财不可用时自动回退不复权并记 provenance）；默认 ``""`` 行为不变（不复权）。

    2026-11-16 修两个生产事故：
    1) **入参不归一**（invest 182 只 ETF 卡 45 分钟）：原来把 ``sym`` 原样丢给新浪
       基金接口——裸代码 ``159845`` 和按股票规则拼出的 ``bj159845`` 都 404，每只重试
       4 次 ≈15s。现在统一经 ``codes.norm_code`` + ``is_etf_code``/``etf_symbol``
       归一成 ``sh510300``/``sz159845``（缓存 key 也用归一值，与历史缓存同名，
       不会多出重复缓存文件）。
    2) **当天 bar 缺失**（B-7 事故）：见 ``_append_today_bar``（行情表/同花顺补
       最后一根，合并表写回缓存；补失败落 ``etf_hist_stale`` provenance）。
    每次调用都在 provenance 里记 ``max_bar_date``（真实末 bar 日期），供 prefetch
    manifest / 上层校验判断「当日键里装的是不是当日行情」。

    ``adjust="qfq"`` 时**无条件**先走 ``_etf_hist_em``（其内部自分派东财→腾讯），
    失败才回退新浪不复权并记 ``etf_qfq_fallback``；不复权分支仍受 ``EM_ENABLED`` 门控。
    """
    from .. import endpoints as ak

    raw = str(sym).strip()
    code = norm_code(raw)
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"fetch_etf_hist: 认不出代码 {sym!r}（应为 6 位数字或带 sh/sz 前缀）")
    etf = is_etf_code(code)
    canonical = etf_symbol(code) if etf else sina_symbol(code)

    if adjust not in ("", "qfq"):
        raise ValueError(f"fetch_etf_hist: 不支持的复权方式 {adjust!r}（只支持 '' / 'qfq'）")
    from_em = False
    df = None
    if adjust == "qfq" and etf:
        # 前复权只认东财口径（新浪基金接口不提供复权）；失败回退不复权，宁可口径差不让调用方挂
        try:
            df = _etf_hist_em(code)
            from_em = df is not None and len(df) > 0
        except Exception:      # noqa: BLE001 —— 东财被风控时回退新浪，不让 ETF 分支挂掉
            df = None
        if not from_em:
            _count("etf_qfq_fallback")
    elif getattr(_config(), "EM_ENABLED", False) and etf:
        try:
            df = _etf_hist_em(code)
            from_em = df is not None and len(df) > 0
        except Exception:      # noqa: BLE001 —— 东财被风控时回退新浪，不让 ETF 分支挂掉
            df = None
    if not from_em:
        def load():
            d = _retry(lambda: ak.fund_etf_hist_sina(symbol=canonical), tries=4, base_sleep=2.0)
            d = d.copy()
            if "date" in d.columns:
                d["date"] = pd.to_datetime(d["date"], errors="coerce")
            return d

        df = _cached("etf_daily", canonical, load, post=_post_daily)
    if etf:
        df = _append_today_bar(df, code, None if from_em else canonical)
    try:
        max_bar_date = (str(pd.to_datetime(df["date"].iloc[-1]))[:10]
                        if df is not None and len(df) else None)
    except (TypeError, ValueError):
        max_bar_date = None
    _prov.append({"kind": "etf_daily_qfq" if adjust == "qfq" else "etf_daily",
                  "key": canonical, "source": "fetch_etf_hist",
                  "adjust": adjust or None,
                  "max_bar_date": max_bar_date, "data_date": data_date(),
                  "rows": int(len(df)) if df is not None else 0})
    return df


def fetch_etf_nav(code: str) -> pd.DataFrame:
    """ETF 单位净值序列（date / value），数据来自场外基金净值通路（天天基金）。

    折溢价必须用**单位净值**；算收益仍应用累计净值（见 ``fetch_fund_nav_history``）。
    """
    return fetch_fund_nav_history(norm_code(code), indicator="单位净值走势")


def fetch_etf_premium(code: str) -> dict:
    """ETF 折溢价：最新不复权收盘价 ÷ 同日单位净值 − 1（%）。

    返回 ``{"code","date","price","nav","nav_date","premium_pct"}``；价格或净值取不到
    抛 RuntimeError（fail-loud：宁可报错，不用陈旧值假装算出了折溢价）。
    净值披露日期比价格早一天时，nav_date 会如实标出。
    """
    c = norm_code(code)
    df = fetch_etf_hist(c)
    if df is None or len(df) == 0:
        raise RuntimeError(f"fetch_etf_premium: 取不到 {c} 日线")
    last = df.iloc[-1]
    day = str(pd.to_datetime(last["date"]))[:10]
    price = float(last["close"])
    nav_df = fetch_etf_nav(c)
    if nav_df is None or len(nav_df) == 0:
        raise RuntimeError(f"fetch_etf_premium: 取不到 {c} 单位净值")
    nav_df = nav_df.copy()
    nav_df["date"] = pd.to_datetime(nav_df["date"]).dt.strftime("%Y-%m-%d")
    nav_df = nav_df[nav_df["date"] <= day].sort_values("date")
    if len(nav_df) == 0:
        raise RuntimeError(f"fetch_etf_premium: {c} 净值序列没有 ≤ {day} 的数据")
    nav_row = nav_df.iloc[-1]
    nav = float(nav_row["value"])
    if not (nav > 0) or not (price > 0):
        raise RuntimeError(f"fetch_etf_premium: {c} 价格/净值异常（price={price}, nav={nav}）")
    return {"code": c, "date": day, "price": round(price, 4), "nav": round(nav, 4),
            "nav_date": str(nav_row["date"]),
            "premium_pct": round((price / nav - 1.0) * 100.0, 3)}



# ---------------------------------------------------------------- 全市场快照
def fetch_market_snapshot() -> pd.DataFrame:
    """新浪行业板块 → 全市场快照：行业标签 + 当前 PE/PB + 总市值。

    49 个板块各取一次成分，共 49 次调用即可覆盖约 3000 只股票。
    单位换算：mktcap/nmc 万元 → 亿元；amount 元 → 亿元。
    """
    from .. import endpoints as ak

    def load():
        sectors = ak.stock_sector_spot(indicator="新浪行业")
        frames, failed = [], []
        for _, r in sectors.iterrows():
            label = str(r["label"])
            d = None
            for attempt in (0, 1):       # 单板块重试一次：新浪偶发超时/空表（CORE-5）
                try:
                    d = ak.stock_sector_detail(sector=label)
                    if d is None or len(d) == 0:
                        raise ValueError("空表")
                    break
                except Exception:        # noqa: BLE001
                    d = None
                    if attempt == 0:
                        time.sleep(0.4)
            if d is None:
                failed.append(label)
                continue
            d = d[["code", "name", "per", "pb", "mktcap", "nmc", "amount"]].copy()
            d["industry"] = str(r["板块"]).strip()
            frames.append(d)
        if failed:
            # 静默丢板块 = 整个数据日少几百只股票却不报错（CORE-5）：记 provenance；
            # 失败超过 10% 直接 fail loud，宁可不写缓存也不要一份「看着成功」的残表。
            _prov.append({"kind": "market_snapshot", "key": "sina_industry",
                          "source": "sector_fail", "failed": failed,
                          "sectors": int(len(sectors))})
            print(f"[datasource] market_snapshot: {len(failed)}/{len(sectors)} 板块取数失败: "
                  f"{','.join(failed[:8])}", file=sys.stderr)
            if len(failed) >= max(2, int(0.1 * len(sectors))):
                raise RuntimeError(
                    f"market_snapshot: {len(failed)}/{len(sectors)} 板块取数失败，拒绝写残表"
                    f"（{','.join(failed[:8])}）")
        if not frames:
            raise RuntimeError("行业快照全部失败")
        return pd.concat(frames, ignore_index=True)

    _OUT_COLS = ["code", "name", "industry", "per", "pb",
                 "mktcap_yi", "nmc_yi", "amount_yi"]

    def post(df):
        df = _post_codes(df)
        # 缓存回读的是本函数归一化后的表（只有 *_yi 列、没有 mktcap/nmc/amount）。
        # 旧版在这里无条件重算 *_yi，缓存路径必然 KeyError('mktcap')，而 _cached 把
        # 「缓存损坏」和「post 失败」都当重取处理 → 这份缓存从来没被命中：每次运行都重打
        # 49 个板块，离线冻结（决策日）时则直接抛「离线模式拦截取数」。已归一化就只做轻量整理。
        if "mktcap_yi" in df.columns and "mktcap" not in df.columns:
            out = df.copy()
            for c in ("code", "name", "industry"):
                if c in out.columns:
                    out[c] = out[c].astype(str).str.strip()
            cols = [c for c in _OUT_COLS if c in out.columns]
            return out.drop_duplicates("code")[cols].reset_index(drop=True)
        for c in ("per", "pb", "mktcap", "nmc", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["mktcap_yi"] = df["mktcap"] / 1e4
        df["nmc_yi"] = df["nmc"] / 1e4
        df["amount_yi"] = df["amount"] / 1e8
        df["industry"] = df["industry"].astype(str).str.strip()
        return (df.drop_duplicates("code")
                  [_OUT_COLS]
                  .reset_index(drop=True))

    df = _cached("market_snapshot", "sina_industry", load, post=post)
    if _MARKET_HOOK is not None:
        # 2026-11-16：钩子必须在**缓存回读**路径也生效。原实现只在 load 新取数的
        # post 里调，invest 读别处（stock 统一取数 / flavor=shared）写好的
        # market_snapshot 缓存时行业标签不会归一 → 行业集中度/门禁口径静默回归。
        # 放在 _cached 之外 = 新取数与缓存回读两条路径都过一次；缓存里始终保留
        # 原始（百度行业）口径，不把某个技能的策略口径写进共享缓存（旧策略没注册
        # 钩子，行为完全不变）。钩子是纯映射（幂等），重复应用结果不变；
        # 钩子抛错就 fail loud（不静默丢掉行业归一）。
        df = _MARKET_HOOK(df)
    return df


def fetch_code_names() -> pd.DataFrame:
    """全市场 A 股代码 / 名称（按数据日缓存，列固定为 code / name）。

    用途：按交易所简称识别「风险警示（ST/*ST）」与「退市整理（含「退」）」名单。
    比 :func:`fetch_market_snapshot` 更全——快照是按新浪行业板块拼的，个别股票会缺行；
    名单口径也覆盖停牌股（停牌不影响简称）。
    取不到不缓存空表：下游据此剔除候选池，空名单等于没筛，必须 fail loud。
    """
    from .. import endpoints as ak

    def load():
        df = ak.stock_info_a_code_name()
        if df is None or len(df) == 0:
            raise RuntimeError("A 股简称名单为空：新浪 hs_a 接口异常")
        return df[['code', 'name']].copy()

    return _cached("code_names", "a_share", load)

# ---------------------------------------------------------------- 交易日历
_CAL_MEMO = {}   # 进程内缓存：日历一年才变，每次 read_csv 纯浪费（backtest/paper_exec/weekly 反复取）


def fetch_global_news(symbol: str = "全部", force: bool = False) -> "pd.DataFrame":
    """财联社电报（全市场快讯，含政策）。返回 标题/内容/发布日期/发布时间。

    个股资讯层只按标的抓，捞不到全市场政策；这一层补政策快讯。
    缓存 15 分钟（先正缓存，失败不落负缓存——快讯漏一轮不值得判死）。
    """
    from .. import endpoints as ak

    return _cached("global_news", str(symbol),
                   lambda: ak.stock_info_global_cls(symbol=symbol).copy(),
                   force=force)


def fetch_trade_calendar() -> pd.DataFrame:
    """A 股交易日历（新浪），trade_date 为 YYYY-MM-DD 字符串，含已排定的未来交易日。

    用途：模拟盘要判断「决策日 / 最早成交日（下一个交易日）/ 条件单到期日」，
    不能用「跳过周末」近似——国庆春节长假会把近似算错。
    首次调用后缓存在进程内存里（返回副本，调用方随便改不影响别人）。
    """
    memo_key = (data_date(), "df")     # 键带数据日（CORE-13）：同进程换数据日不会读到旧日历
    hit = _CAL_MEMO.get(memo_key)
    if hit is not None:
        return hit.copy()
    from .. import endpoints as ak

    def load():
        df = ak.tool_trade_date_hist_sina()
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df[["trade_date"]]

    df = _cached("calendar", "trade", load,
                 post=lambda d: d.dropna().drop_duplicates().reset_index(drop=True))
    _CAL_MEMO[memo_key] = df
    return df.copy()


def is_trading_day(day: str = "") -> bool:
    """该日是否为交易所交易日。

    交易日 = 周一至周五且非法定节假日。**调休补班的周末交易所也不开市**，故周六/周日
    一律 False；day 支持 ''（今天）/``YYYYMMDD``/``YYYY-MM-DD``。排任务、判数据新鲜度
    一律用它，别用「星期几」近似。
    """
    d = _day_arg(day) or today()
    cal = fetch_trade_calendar()
    col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
    return d in set(cal[col].astype(str).str[:10])


def last_trading_day(day: str = "") -> str:
    """≤ day 的最近一个交易日（'' = 今天），没有则返回 ''。"""
    d = _day_arg(day) or today()
    cal = fetch_trade_calendar()
    col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
    days = sorted(set(cal[col].astype(str).str[:10]))
    prev = [x for x in days if x <= d]
    return prev[-1] if prev else ""


# ---------------------------------------------------------------- 场外基金 / 利率锚
# 为什么单开一段：场外开放式债券基金是 R2 层「场内 ETF 一手买不起」时的兜底腿
# （小本金买不起一手债券 ETF，但场外 10 元起购）。
# 本段接口全部是天天基金（东方财富）系 + 中债 + 交易所回购，与新浪系互相独立。
#
# ⚠️ 单位约定：本段内**所有 `*_pct` 列都是百分数**（0.15 表示 0.15%），与接口原始值一致，
#    方便对着接口核对；换算成小数由调用方（otc.py）负责。唯一例外是
#    fetch_cn_treasury_yield()，按契约直接返回小数（0.011799 = 1.1799%）。
#
# ⚠️ 缓存陷阱（见文件头）：缓存写的是 post 归一化之后的表，读回来会**再过一遍 post**。
#    所以每个 post 都必须同时对「接口原始表」和「自己的归一化表」成立——能判就别重算，
#    取列一律走 _pick()（原名/新名都认）。
def _pick(df: pd.DataFrame, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    raise KeyError(f"缺少列 {names}；现有列 {list(df.columns)[:12]}")


def _num(series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _pct_of(value) -> float | None:
    """'0.13%（每年）' / '1.50%' / '0.00%' → 0.13 / 1.50 / 0.0（仍是百分数）。

    「运作费用」接口把标签和数值混在一行里，数值都是这种带单位后缀的字符串，
    脏值形如 '---'（新基金未披露）→ 返回 None（不冒充 0，0 费率是有意义的结论）。
    """
    s = str(value).strip()
    if not s or s in ("---", "nan", "None"):
        return None
    s = s.replace("%", "").replace("（每年）", "").replace("(每年)", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _pct_series(series) -> pd.Series:
    """把 '0.15%' 这类字符串列转成 float 百分数。"""
    return _num(pd.Series([str(v).replace("%", "").strip() for v in series.tolist()],
                          index=series.index))


def fetch_fund_purchase_snapshot() -> pd.DataFrame:
    """场外基金申购快照全表（天天基金），27,884 只，一行一只。

    用途：场外债券基金池的 L0 全表 —— 基金类型 / 申购状态 / 购买起点 / 日累计限额 /
    申购费全在这张表里，一次请求就能跑完 L0–L4 的粗筛，省掉几千次净值请求。
    返回列：code / name / ftype / nav / nav_date(只有 '10-27'，无年份) /
            purchase_status / redeem_status / min_purchase / day_limit / fee_pct
    """
    from .. import endpoints as ak

    def load():
        # 天天基金偶发超时；重试 3 次仍失败就抛（fail loud，绝不返回旧缓存冒充今天）
        return _retry(lambda: ak.fund_purchase_em().copy(), tries=3, base_sleep=3.0)

    def post(df):
        out = pd.DataFrame({
            "code": _pick(df, "基金代码", "code").astype(str)
                    .str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6),
            "name": _pick(df, "基金简称", "name").astype(str).str.strip(),
            "ftype": _pick(df, "基金类型", "ftype").astype(str).str.strip(),
            "nav": _num(_pick(df, "最新净值/万份收益", "nav")),
            # 报告时间只有「10-27」这种月-日，没有年份 → 完整日期取 nav_daily 的 nav_date
            "nav_date": _pick(df, "最新净值/万份收益-报告时间", "nav_date").astype(str),
            "purchase_status": _pick(df, "申购状态", "purchase_status").astype(str).str.strip(),
            "redeem_status": _pick(df, "赎回状态", "redeem_status").astype(str).str.strip(),
            "min_purchase": _num(_pick(df, "购买起点", "min_purchase")),
            "day_limit": _num(_pick(df, "日累计限定金额", "day_limit")),
            "fee_pct": _num(_pick(df, "手续费", "fee_pct")),
        })
        return out.drop_duplicates("code").reset_index(drop=True)

    return _cached("fund_purchase", "all", load, post=post)


def fetch_fund_nav_daily() -> pd.DataFrame:
    """场外基金当日净值快照（单位净值 + 累计净值），用来给候选补「完整净值日期」。

    ⚠️ 接口列名是 `2026-10-27-单位净值` 这种「日期-字段」，日期每天在变，所以 post 里
    按后缀动态找最新/前一交易日的两列（缓存文件按自然日命名，不会跨日串期）。
    返回列：code / name / nav_date / unit_nav / acc_nav / prev_unit_nav / prev_acc_nav /
            prev_nav_date / growth_pct / fee_pct（后三项为百分数）。

    ``prev_nav_date`` 是「前一交易日」两列的日期前缀（只用来让 fund_nav 增量追加
    判断「bulk 表里的 prev 值属于哪一天」；老缓存文件没有这一列时统一回填空串）。
    """
    from .. import endpoints as ak

    _NUM_COLS = ("unit_nav", "acc_nav", "prev_unit_nav", "prev_acc_nav", "growth_pct", "fee_pct")

    def load():
        return _retry(lambda: ak.fund_open_fund_daily_em().copy(), tries=3, base_sleep=3.0)

    def post(df):
        if "unit_nav" in df.columns:      # 已是归一化表（缓存读回）→ 只做类型兜底
            for c in _NUM_COLS:
                if c in df.columns:
                    df[c] = _num(df[c])
            if "code" in df.columns:      # 防「缓存读回把 000084 变 84」（见 _fix_code_col）
                df["code"] = _fix_code_col(df["code"])
            for c in ("nav_date", "prev_nav_date"):
                if c in df.columns:
                    df[c] = df[c].astype(str)
            if "prev_nav_date" not in df.columns:   # 旧缓存（2026-11 前）没有该列
                df["prev_nav_date"] = ""
            return df
        unit_cols = sorted([str(c) for c in df.columns if str(c).endswith("-单位净值")])
        if not unit_cols:
            raise ValueError("fund_nav_daily: 找不到「-单位净值」列")
        acc_map = {str(c)[: -len("-累计净值")]: str(c) for c in df.columns
                   if str(c).endswith("-累计净值")}
        latest = unit_cols[-1][: -len("-单位净值")]
        prev = unit_cols[-2][: -len("-单位净值")] if len(unit_cols) > 1 else None
        return pd.DataFrame({
            "code": _pick(df, "基金代码", "code").astype(str)
                    .str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6),
            "name": _pick(df, "基金简称", "name").astype(str).str.strip(),
            "nav_date": latest,
            "unit_nav": _num(df[unit_cols[-1]]),
            "acc_nav": _num(df[acc_map[latest]]) if latest in acc_map else None,
            "prev_unit_nav": _num(df[unit_cols[-2]]) if prev else None,
            "prev_acc_nav": _num(df[acc_map[prev]]) if prev and prev in acc_map else None,
            "prev_nav_date": prev,
            "growth_pct": _num(_pick(df, "日增长率", "growth_pct")),
            "fee_pct": _pct_series(_pick(df, "手续费", "fee_pct")),
        }).drop_duplicates("code").reset_index(drop=True)

    return _cached("fund_nav_daily", "all", load, post=post)


# ---------------------------------------------------------------- ETF 规模 last-good 缓存（B-3）
# 背景：2026-11-14 周度决策时雪球基金规模（主源）与东财 fund_etf_spot_em（兜底）
# 同时不可用 → 全部 ETF 候选 aum=None → 规模闸门 fail-closed → 核心仓为空、计划被拦截。
# 用户 B-3 批准：允许复用最近一次成功取到的 ETF 规模快照，但仅限
# _config().SCALE_CACHE_MAX_TRADE_DAYS 个交易日内，且必须在漏斗/报告里显式标注
# 「规模数据陈旧 N 个交易日」；超过窗格仍按缺失处理（继续 fail-closed）。
_SCALE_CACHE_STATUS = {"mode": "none", "as_of": None, "source": None, "stale_days": None}
_SCALE_SAVED: dict = {}        # {"day": 数据日, "source": 源, "path": 缓存文件}：每数据日只写一次
_SCALE_SAVE_LOCK = threading.Lock()


def _load_etf_scale_cache():
    """读 last-good ETF 规模快照；不存在/损坏返回 None。"""
    try:
        with open(_SCALE_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        aum = {}
        for k, v in (raw.get("aum") or {}).items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                ks = str(k).strip()
                if ks.endswith(".0"):          # 人工/外部种子可能把代码写成 float 文本
                    ks = ks[:-2]
                aum[ks.zfill(6)] = fv
        as_of = str(raw.get("as_of") or "")
        if not aum or not as_of:
            return None
        return {"aum": aum, "as_of": as_of,
                "source": str(raw.get("source") or "unknown")}
    except (OSError, ValueError, TypeError):
        return None


def _save_etf_scale_cache(aum: dict[str, float], source: str, force: bool = False) -> bool:
    """成功取数后保存 last-good 快照（原子替换；每数据日只写一次；失败记 provenance）。

    CORE-1：原实现固定 `.tmp` 名且无锁——prefetch 的 fund L7 每只兜底都会走
    fetch_fund_scale → fetch_etf_scale_map → 这里，8 线程并发写会把 JSON 写坏
    （drill3b：12 轮里 9 轮损坏，`_load_etf_scale_cache` 直接返回 None）。
    改为 pid+tid 临时文件 + 进程锁 + 每数据日只写一次（同一数据日的行情表重算结果等价）。
    """
    day = data_date()
    with _SCALE_SAVE_LOCK:
        if not force and _SCALE_SAVED.get("day") == day and _SCALE_SAVED.get("source") == source:
            return True
        tmp = f"{_SCALE_CACHE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(_config().CACHE_DIR, exist_ok=True)
            payload = {"as_of": day, "source": source,
                       "aum": {k: round(float(v), 6) for k, v in aum.items()}}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, _SCALE_CACHE_FILE)
        except (OSError, ValueError, TypeError) as e:      # noqa: BLE001
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            _prov.append({"kind": "etf_scale", "key": "lastgood",
                          "source": "scale_cache_write_error",
                          "error": f"{type(e).__name__}: {e}"})
            print(f"[datasource] ETF 规模 last-good 写缓存失败：{type(e).__name__}: {e}",
                  file=sys.stderr)
            return False
        _SCALE_SAVED.update({"day": day, "source": source, "path": _SCALE_CACHE_FILE})
        return True


def _trade_days_since(start_date: str, end_date: str):
    """start→end 之间经历的交易日数（end 当天算 1 个；取不到日历返回 None）。"""
    try:
        cal = fetch_trade_calendar()["trade_date"].astype(str).tolist()
    except Exception:  # noqa: BLE001 —— 无日历不猜陈旧天数，宁可继续 fail-closed
        return None
    a, b = str(start_date), str(end_date)
    return sum(1 for d in cal if a < d <= b)


def etf_scale_status() -> dict:
    """最近一次 ETF 规模取数状态：{"mode": live|cache_stale|none, "as_of", "stale_days"}。"""
    return dict(_SCALE_CACHE_STATUS)


def fetch_etf_scale_map() -> dict[str, float]:
    """ETF 规模表（亿元）：v0.3.17 主源腾讯行情「总市值」；东财仅显式开启；最后 B-3 last-good。

    实测（2026-11-14）：腾讯 ``qt.gtimg.cn`` 的「总市值」字段对 ETF ≈ 最新价×总份额
    ≈ 基金规模（510300 = 1862.80 亿、159915 = 1093.61 亿、159922 = 94.65 亿），
    无需登录态且实时；东财 push2 系被本机出口风控，默认关闭（``_config().EM_ENABLED``）。
    ``_SCALE_CACHE_STATUS`` 记录 mode/source/stale_days，漏斗与报告据此标注来源。
    """
    global _SCALE_CACHE_STATUS

    # 1) 主源：腾讯行情「总市值（亿元）」——与 ETF 行情同表缓存，不多打接口
    try:
        tx = _etf_spot_tx_cached()
        out: dict[str, float] = {}
        for _, r in tx.iterrows():
            try:
                v = float(r["aum_yi"])
            except (TypeError, ValueError):
                continue
            if v > 0:
                out[str(r["code"]).zfill(6)] = v
        if out:
            _save_etf_scale_cache(out, "tencent_qt")
            _SCALE_CACHE_STATUS = {"mode": "live", "as_of": data_date(),
                                   "source": "tencent", "stale_days": 0}
            return out
    except Exception:  # noqa: BLE001 —— 腾讯不可用 → 继续走东财/缓存，不中断
        pass

    # 2) 东财兜底（默认关闭：本机出口被风控；显式 STOCK_EM_ENABLED=1 才尝试）
    if getattr(_config(), "EM_ENABLED", False):
        from .. import endpoints as ak

        def load():
            return _retry(lambda: ak.fund_etf_spot_em().copy(), tries=3, base_sleep=2.0)

        def post(df):
            out = df.copy()
            if "代码" in out.columns:                     # 新取数：中文列 → 统一列名
                out = out.rename(columns={"代码": "code"})
                aum = pd.to_numeric(out.get("总市值"), errors="coerce")
                if aum.isna().all():
                    aum = pd.to_numeric(out.get("流通市值"), errors="coerce")
                out["aum_yi"] = aum / 1e8 if aum is not None else None
            out["code"] = _fix_code_col(out["code"])
            out["aum_yi"] = pd.to_numeric(out["aum_yi"], errors="coerce")
            return (out.dropna(subset=["aum_yi"])
                       .loc[:, ["code", "aum_yi"]].reset_index(drop=True))

        try:
            df = _cached("etf_spot_em", "scale", load, post=post)
            em: dict[str, float] = {}
            for _, r in df.iterrows():
                try:
                    v = float(r["aum_yi"])
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    em[str(r["code"]).zfill(6)] = v
            if em:
                _save_etf_scale_cache(em, "etf_spot_em")
                _SCALE_CACHE_STATUS = {"mode": "live", "as_of": data_date(),
                                       "source": "etf_em", "stale_days": 0}
                return em
        except Exception:  # noqa: BLE001 —— 东财不可用，落到 last-good
            pass

    # 3) B-3 last-good 缓存（≤ SCALE_CACHE_MAX_TRADE_DAYS 个交易日，报告标注陈旧）
    cached = _load_etf_scale_cache()
    max_days = int(getattr(_config(), "SCALE_CACHE_MAX_TRADE_DAYS", 5))
    if cached:
        days = _trade_days_since(cached["as_of"], data_date())
        if days is not None and 0 <= days <= max_days:
            _SCALE_CACHE_STATUS = {"mode": "cache_stale", "as_of": cached["as_of"],
                                   "source": "lastgood", "stale_days": int(days)}
            return dict(cached["aum"])
    _SCALE_CACHE_STATUS = {"mode": "none", "as_of": None, "source": None, "stale_days": None}
    raise RuntimeError(
        f"ETF 规模主源（腾讯行情）不可用，且无 ≤{max_days} 个交易日的 last-good 快照")


_FUND_SCALE_XQ_FAILS = 0         # 雪球档案接口连续失败次数（成功即清零）
_FUND_SCALE_XQ_COOLDOWN = 0.0     # 熔断冷却截止（time.time() 秒）；到点后允许再试
_FUND_SCALE_XQ_FAIL_THRESHOLD = 3  # 连续失败这么多次才熔断（单个代码不存在不该拖垮整批）
_FUND_SCALE_XQ_COOLDOWN_SEC = 120.0
_FUND_SCALE_XQ_BROKEN = False     # 兼容旧名：True 表示当前处于冷却期
_FUND_SCALE_LAST_SOURCE = "none"  # 最近一次规模来源：xq / etf_em / none


def fund_scale_source() -> str:
    """最近一次 fetch_fund_scale 实际用的数据源（供 screen 写 notes，不猜）。"""
    return _FUND_SCALE_LAST_SOURCE


def _scale_yi(raw: str) -> float | None:
    """蛋卷「最新规模」文本 → 亿元。单位可能是亿（大基金）或万（小基金，如「6584.51万」）。"""
    txt = str(raw).strip().replace(",", "").replace("元", "")
    mult = 1.0
    if txt.endswith("亿"):
        txt = txt[:-1]
    elif txt.endswith("万"):
        txt = txt[:-1]
        mult = 1e-4
    try:
        return float(txt) * mult
    except ValueError:
        return None


def fetch_fund_scale(code: str) -> float | None:
    """基金最新规模（亿元）。主源雪球/蛋卷基金档案，兜底 1 东财基金规模（债基/货基
    也覆盖），兜底 2 东财 ETF 总市值。

    雪球接口 2026-11 起时好时坏（风控返回里没有 ``data``）：**连续**失败 3 次才在
    本进程内熔断 120 秒，冷却结束自动再试——单个代码不存在/偶发限流不该让整批规模
    都变 None（否则 OTC 池的规模过滤会整段静默失效）。
    best-effort：三路都取不到返回 None（调用方记 scale_unavailable 并放行，不做
    「查不到就淘汰」的静默淘汰）。``fund_scale_source()`` 记实际用的源（xq/em/etf_*）。"""
    global _FUND_SCALE_XQ_BROKEN, _FUND_SCALE_XQ_FAILS, _FUND_SCALE_XQ_COOLDOWN
    global _FUND_SCALE_LAST_SOURCE
    from .. import endpoints as ak

    c = norm_code(code)

    def load():
        try:
            return _retry(lambda: ak.fund_individual_basic_info_xq(symbol=c).copy(),
                          tries=2, base_sleep=1.5)
        except RuntimeError as e:
            # _retry 把最后一次异常包成 RuntimeError；把「蛋卷明确无此基金」
            # 还原成 ValueError，好让上层区分「没这只基金」与「接口故障」。
            if isinstance(e.__cause__, ValueError) or isinstance(e.__context__, ValueError):
                raise ValueError(str(e)) from None
            raise

    def post(df):
        out = df.copy()
        out["item"] = _pick(out, "item").astype(str).str.strip()
        out["value"] = _pick(out, "value").astype(str).str.strip()
        return out

    now = time.time()
    if not offline() and now >= _FUND_SCALE_XQ_COOLDOWN:
        try:
            df = _cached("fund_scale", c, load, post=post)
            row = df[df["item"] == "最新规模"]
            if len(row):
                v = _scale_yi(str(row["value"].iloc[0]))
                if v is not None:
                    _FUND_SCALE_XQ_FAILS = 0
                    _FUND_SCALE_XQ_COOLDOWN = 0.0
                    _FUND_SCALE_XQ_BROKEN = False
                    _FUND_SCALE_LAST_SOURCE = "xq"
                    return v
        except ValueError:  # 蛋卷明确「没有这只基金」：不是接口故障，不计数、不熔断
            pass
        except Exception:  # noqa: BLE001 —— 主源不可用 → 计数，连续失败才熔断
            _FUND_SCALE_XQ_FAILS += 1
            if _FUND_SCALE_XQ_FAILS >= _FUND_SCALE_XQ_FAIL_THRESHOLD:
                _FUND_SCALE_XQ_COOLDOWN = now + _FUND_SCALE_XQ_COOLDOWN_SEC
                _FUND_SCALE_XQ_BROKEN = True
                _count("fund_scale_xq_cooldown")

    # 兜底 1：东财基金规模（债基/货基也覆盖，蛋卷没收的代码这里能取到）
    if not offline():
        try:
            df = _cached("fund_scale_em", c, lambda: _retry(
                lambda: ak.fund_scale_em(symbol=c).copy(), tries=2, base_sleep=1.5))
            yuan = float(df["最新规模(元)"].iloc[0])
            if yuan > 0:
                _FUND_SCALE_LAST_SOURCE = "em"
                return round(yuan / 1e8, 4)
        except Exception:  # noqa: BLE001 —— 兜底 1 也失败就继续走 ETF 兜底
            _count("fund_scale_em_miss")

    try:
        v = fetch_etf_scale_map().get(c)
    except Exception:  # noqa: BLE001 —— 兜底也失败按缺失，不抛
        v = None
    _st = etf_scale_status()
    if v is not None and _st.get("mode") == "cache_stale":
        _FUND_SCALE_LAST_SOURCE = "etf_stale"
    elif v is not None and _st.get("source") == "tencent":
        _FUND_SCALE_LAST_SOURCE = "etf_tx"
    else:
        _FUND_SCALE_LAST_SOURCE = "etf_em" if v is not None else "none"
    return v


def fetch_money_fund_rates() -> pd.DataFrame:
    """全市场货币基金收益排行（数量随市场变化，通常 500–600 只），给现金层利率实测用。

    返回列（百分比列一律带 `_pct` 后缀，单位=百分数）：code / name / date /
    wanfen（万份收益，元）/ y7_pct / y14_pct / y28_pct / ret_1y_pct / fee_pct
    调用方要年化小数：`float(df["y7_pct"].median()) / 100`（中位数才稳，别用均值）。
    """
    from .. import endpoints as ak

    def load():
        return _retry(lambda: ak.fund_money_rank_em().copy(), tries=3, base_sleep=3.0)

    NEED = ("基金代码", "基金简称", "年化收益率7日")

    def post(df):
        if "y7_pct" in df.columns:        # 归一化表（缓存读回）
            for c in ("wanfen", "y7_pct", "y14_pct", "y28_pct", "ret_1y_pct", "fee_pct"):
                if c in df.columns:
                    df[c] = _num(df[c])
            if "code" in df.columns:      # 防「缓存读回把 000682 变 682」（见 _fix_code_col）
                df["code"] = _fix_code_col(df["code"])
                bad = int((~df["code"].astype(str).str.fullmatch(r"\d{6}")).sum())
                if bad:
                    raise ValueError(
                        f"money_rank 缓存损坏：{bad}/{len(df)} 行 code 不是 6 位"
                        f"（示例 {list(df['code'].astype(str).head(3))}）。"
                        "上游表结构变过，把类型名当成了代码；"
                        "请删掉 cache/money_rank/ 下的缓存文件后重跑取数。")
            for c in ("name", "date"):
                if c in df.columns:
                    df[c] = df[c].astype(str)
            return df
        missing = [c for c in NEED if c not in df.columns]
        if missing:
            raise KeyError(
                "fund_money_rank_em 返回的表结构变了：缺少列 %s；现有列 %s"
                % (missing, list(df.columns)[:20]))
        return pd.DataFrame({
            "code": _pick(df, "基金代码", "code").astype(str)
                    .str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6),
            "name": _pick(df, "基金简称", "name").astype(str).str.strip(),
            "date": _pick(df, "日期", "date").astype(str),
            "wanfen": _num(_pick(df, "万份收益", "wanfen")),
            "y7_pct": _num(_pick(df, "年化收益率7日", "y7_pct")),
            "y14_pct": _num(_pick(df, "年化收益率14日", "y14_pct")),
            "y28_pct": _num(_pick(df, "年化收益率28日", "y28_pct")),
            "ret_1y_pct": _num(_pick(df, "近1年", "ret_1y_pct")),
            "fee_pct": _pct_series(_pick(df, "手续费", "fee_pct")),
        })

    return _cached("money_rank", "all", load, post=post)


def fetch_cn_treasury_yield() -> dict:
    """中债国债收益率曲线最新一日（无风险利率锚，契约 §E）。

    用途：doctor / review 核对验收基准（1 年期国债 vs _config().PERF['deposit_rate']）。
    返回 dict，单位**小数**（0.011799 = 1.1799%）：{"date","y1","y3","y5","y10"}。
    接口原始值是百分数，这里 ÷100 —— 与 _config().PERF["deposit_rate"] 同为小数（现值见配置），
    调用方不用再换算（换算错一次的代价是「基准差一个数量级」，比多一次除法危险）。
    """
    from .. import endpoints as ak

    # 用数据日而不是真实今天（CORE-4）：回放/冻结时窗口必须跟数据日一致，否则会前视。
    today = dt.date.fromisoformat(data_date())
    end_s = today.strftime("%Y%m%d")

    def _fetch_window(days: int) -> pd.DataFrame:
        start_s = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
        return _retry(lambda: ak.bond_china_yield(start_date=start_s, end_date=end_s).copy(),
                      tries=2, base_sleep=2.0)

    def load():
        # ⚠️ 实测坑（2026-10-27 踩过一次）：这个接口的查询区间**必须 ≤365 天**。传 400 天
        # 它不报错，直接返回**0 行空表**（而且空表的收益率列被撑成 int64），于是看起来
        # 像「曲线名对不上」——我第一版就是在 400 天窗口上踩了这个假象。
        # 所以这里按窗口收敛：365 → 180 → 60 天，任一窗口拿到数据就用；全空才 fail loud
        # （报错里带区间，下次能一眼看出是窗口问题而不是数据问题）。
        last_len = 0
        for days in (365, 180, 60):
            raw = _fetch_window(days)
            last_len = int(len(raw))
            if last_len == 0:
                continue
            nm = raw["曲线名称"].astype(str).str.strip()
            g = raw[nm == "中债国债收益率曲线"].copy()
            if len(g) == 0:      # 兜底：曲线名后缀有变体（去掉 (AAA) 之类）时仍能命中
                g = raw[nm.str.contains("国债收益率曲线", na=False)].copy()
            if len(g) > 0:
                return g.sort_values("日期").reset_index(drop=True)
        raise ValueError(
            f"bond_china_yield: 近 365/180/60 天窗口都取不到「中债国债收益率曲线」"
            f"（最后一次 {end_s} 返回 {last_len} 行；该接口区间 >365 天会返回空表）")

    _MAP = (("3月", "y3m"), ("6月", "y6m"), ("1年", "y1"), ("3年", "y3"),
            ("5年", "y5"), ("7年", "y7"), ("10年", "y10"), ("30年", "y30"))

    def post(df):
        if "y1" in df.columns:            # 归一化表（缓存读回）
            out = df.copy()
            out["date"] = pd.to_datetime(out["date"], errors="coerce")
            for _, dst in _MAP:
                if dst in out.columns:
                    out[dst] = _num(out[dst])
            return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        out = pd.DataFrame({"date": pd.to_datetime(df["日期"], errors="coerce")})
        for src, dst in _MAP:
            out[dst] = (_num(df[src]) / 100.0) if src in df.columns else None
        return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    df = _cached("cn_treasury_yield", "gov_curve", load, post=post)
    last = df.iloc[-1]

    def _v(col):
        try:
            v = float(last[col])
        except (KeyError, TypeError, ValueError):
            return None
        return None if v != v else round(v, 6)

    return {
        "date": str(pd.to_datetime(last["date"]).date()),
        "y1": _v("y1"), "y3": _v("y3"), "y5": _v("y5"), "y10": _v("y10"),
        "y3m": _v("y3m"), "y6m": _v("y6m"), "y7": _v("y7"), "y30": _v("y30"),
        "curve": "中债国债收益率曲线",
        "source": "ak.bond_china_yield",
        "is_proxy": False,
    }


def fetch_repo_rate() -> dict | None:
    """交易所逆回购利率实测（年化，小数）。

    口径（2026-10-27 实测）：上交所 GC001(204001) 一天期国债逆回购的**日线收盘价就是
    当日年化利率（百分数）**，新浪 bond_zh_hs_daily 给出 2016-11-24 起 2408 个交易日。
    取「近 250 个交易日收盘价均值」而不是某一天：逆回购是天天滚的，某一天的盘中价
    （0.6%–2.0% 之间乱跳）不代表全年水平，均值才是策略该假设的口径。
    候选顺序（契约 §B）：①bond_zh_hs_daily(sh204001) ②stock_zh_a_daily(sh204001)
    ③SHIBOR 1W 近 250 交易日均值（**代理**，basis 里必须写明「非交易所实际成交」）。
    三条全失败 → None（附带 basis 说明），绝不假装有数。
    """
    from .. import endpoints as ak

    def _summarize(df: pd.DataFrame, col: str) -> dict:
        """近 250 行均值 → 小数 + 可审计的样本期间。"""
        d = df.dropna(subset=[col]).copy()
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.dropna(subset=["date"]).sort_values("date")
        t = d.tail(250)
        vals = _num(t[col]).dropna()
        mean_pct = float(vals.mean())
        return {
            "rate": round(mean_pct / 100.0, 6),
            "rate_pct": round(mean_pct, 4),
            "samples": int(len(vals)),
            "sample_start": str(t["date"].iloc[0].date()),
            "sample_end": str(t["date"].iloc[-1].date()),
            "last_pct": round(float(vals.iloc[-1]), 4),
            "mean_20_pct": round(float(_num(d[col]).dropna().tail(20).mean()), 4),
            "mean_60_pct": round(float(_num(d[col]).dropna().tail(60).mean()), 4),
        }

    # ① 交易所真实成交：GC001 日线（收盘价=年化%）
    try:
        def load_gc():
            return _retry(lambda: ak.bond_zh_hs_daily(symbol="sh204001").copy(),
                          tries=2, base_sleep=2.0)

        df = _cached("repo_rate", "gc001_204001", load_gc, post=_post_daily)
        if len(df) >= 60:
            s = _summarize(df, "close")
            s.update({
                "source": "ak.bond_zh_hs_daily(sh204001)",
                "interface": "bond_zh_hs_daily",
                "symbol": "sh204001",
                "is_proxy": False,
                "basis": (f"GC001(204001) 交易所 1 天期国债逆回购，近 {s['samples']} 个交易日"
                          f"收盘价均值 {s['rate_pct']:.3f}%"
                          f"（样本 {s['sample_start']}→{s['sample_end']}），非代理"),
            })
            return s
    except Exception:
        pass      # ① 挂 → 试 ②

    # ② 同一标的换个通道（实测该接口对 204001 返回 JSONDecodeError，留作兜底）
    try:
        def load_gc2():
            return _retry(lambda: ak.stock_zh_a_daily(symbol="sh204001").copy(),
                          tries=2, base_sleep=2.0)

        df = _cached("repo_rate", "gc001_204001_alt", load_gc2, post=_post_daily)
        if len(df) >= 60:
            s = _summarize(df, "close")
            s.update({
                "source": "ak.stock_zh_a_daily(sh204001)",
                "interface": "stock_zh_a_daily",
                "symbol": "sh204001",
                "is_proxy": False,
                "basis": (f"GC001(204001) 近 {s['samples']} 个交易日收盘价均值"
                          f" {s['rate_pct']:.3f}%（样本 {s['sample_start']}→{s['sample_end']}），非代理"),
            })
            return s
    except Exception:
        pass      # ② 挂 → 用代理

    # ③ 代理：SHIBOR 1 周（银行间报价，不是交易所成交价）
    try:
        def load_shibor():
            return _retry(lambda: ak.macro_china_shibor_all().copy(), tries=2, base_sleep=3.0)

        def post_shibor(df):
            if "date" in df.columns and "w1_pct" in df.columns:
                out = df.copy()
                out["date"] = pd.to_datetime(out["date"], errors="coerce")
                out["w1_pct"] = _num(out["w1_pct"])
                return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
            return pd.DataFrame({
                "date": pd.to_datetime(_pick(df, "日期", "date"), errors="coerce"),
                "w1_pct": _num(_pick(df, "1W-定价", "w1_pct")),
            }).dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

        df = _cached("shibor", "all", load_shibor, post=post_shibor)
        if len(df) >= 60:
            s = _summarize(df, "w1_pct")
            s.update({
                "source": "ak.macro_china_shibor_all(1W)",
                "interface": "macro_china_shibor_all",
                "symbol": "SHIBOR-1W",
                "is_proxy": True,
                "basis": (f"代理：SHIBOR 1 周近 {s['samples']} 个交易日均值 {s['rate_pct']:.3f}%"
                          f"（样本 {s['sample_start']}→{s['sample_end']}），"
                          f"银行间报价，非交易所实际成交"),
            })
            return s
    except Exception:
        pass

    return None


# ---------------------------------------------------------------- 溯源
def dump_provenance(name: str = "provenance") -> str:
    ensure_dirs()
    path = os.path.join(OUT_ROOT, f"{name}.json")
    agg: dict = {}
    for p in _prov:
        agg.setdefault(f'{p["kind"]}::{p["key"]}', []).append(p)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "data_layer": _ak_version(),
                   "data_date": data_date(), "offline": offline(),
                   "offline_blocked": len(_OFFLINE_BLOCKS),
                   "count": len(_prov), "items": agg}, f,
                  ensure_ascii=False, indent=1)
    return path


def _ak_version() -> str:
    try:
        from .. import endpoints as ak
        return ak.__version__
    except Exception:
        return "unknown"


def _em_fund_lsjz(code: str, page_size: int = 20, page_index: int = 1,
                  start_date: str = "", end_date: str = "") -> pd.DataFrame:
    """东财基金历史净值 JSON（货基行=万份收益/7日年化/申赎状态）。

    2026-11-16 实测口径（旧 docstring 的「pageSize=250 一次拿回，单只约 0.2 秒」已被证伪）：
    - **pageSize 上限 20**：传 250 服务端回 `{"Data":null,"TotalCount":0}`（不报错、静默空），
      传 20 才正常 → 默认 20，超过自动 clamp；
    - 只传 fundCode/pageIndex/pageSize 也返回空，**必须补** `startDate`/`endDate`（可空串）
      与 `_`（毫秒时间戳）三个参数，否则快通道从未成功过、每次都退到慢路径；
    - 一页 20 行、约 0.2 秒；一只 4815 行的货基全历史仍要 241 页 ≈ 40–60 秒，与慢路径
      `fund_money_fund_info_em`（也是 20 行/页）同量级，快通道的价值在于
      `fetch_money_fund_hist(days=...)` 只取最近窗口（约 20 页 ≈ 4 秒）。
    - 第一页为空 → 抛 ValueError（真失败，调用方退回慢路径）；
      后续页为空 → 返回空表（翻到末尾，正常终止）。
    """
    import requests

    page_size = max(1, min(int(page_size), 20))      # 服务端 pageSize>20 静默返回空
    page_index = max(1, int(page_index))
    empty = pd.DataFrame(columns=["date", "wanfen", "y7_pct", "buy", "sell"])
    last = None
    for i in range(3):
        try:
            r = requests.get(
                "https://api.fund.eastmoney.com/f10/lsjz",
                params={
                    "fundCode": str(code),
                    "pageIndex": page_index,
                    "pageSize": page_size,
                    "startDate": start_date or "",
                    "endDate": end_date or "",
                    "_": str(int(time.time() * 1000)),
                },
                headers={"Referer": "https://fundf10.eastmoney.com/",
                         "User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            r.raise_for_status()
            lst = ((r.json().get("Data") or {}).get("LSJZList")) or []
            if not lst:
                if page_index == 1:
                    raise ValueError(f"fund {code}: lsjz 第一页返回空列表")
                return empty
            return pd.DataFrame({
                "date": [x.get("FSRQ", "") for x in lst],
                "wanfen": [x.get("DWJZ") for x in lst],
                "y7_pct": [x.get("LJJZ") for x in lst],
                "buy": [x.get("SGZT", "") for x in lst],
                "sell": [x.get("SHZT", "") for x in lst],
            })
        except Exception as exc:      # 东财对高频请求会限流（挂到 20s 超时），退避重试
            last = exc
            time.sleep(1.0 + i * 1.5)
    raise last  # type: ignore[misc]


def fetch_fund_fee(code: str, indicator: str) -> pd.DataFrame:
    """场外基金费率（天天基金）。indicator ∈ 运作费用 / 赎回费率 / 申购费率 / 认购费率。

    ⚠️ 「运作费用」返回的是**无列名的扁表**：一行 6 格，按「标签, 数值」成对排列
    （管理费率 / 托管费率 / 销售服务费率），所以 post 按位置拆成长表 item/value_pct。
    ⚠️ 「申购费率」实测返回 0 行（该接口已不再给申购费）→ 申购费请用
    fetch_fund_purchase_snapshot().fee_pct（已含 1 折）；空表按 fail loud 抛异常。
    返回列：运作费用 → item/value_pct；其余 → period/fee_pct（均为百分数）
    """
    from .. import endpoints as ak

    c = norm_code(code)

    def load():
        return _retry(lambda: ak.fund_fee_em(symbol=c, indicator=indicator).copy(),
                      tries=2, base_sleep=2.0)

    def post(df):
        if "item" in df.columns:          # 运作费用归一化表
            out = df.copy()
            out["value_pct"] = _num(out["value_pct"])
            out["item"] = out["item"].astype(str).str.strip()
            return out
        if "period" in df.columns:        # 适用期限归一化表
            out = df.copy()
            out["period"] = out["period"].astype(str).str.strip()
            out["fee_pct"] = _num(out["fee_pct"])
            return out
        if indicator == "运作费用":
            vals = list(df.iloc[0].tolist()) if len(df) else []
            rows = [{"item": str(vals[i]).strip(), "value_pct": _pct_of(vals[i + 1])}
                    for i in range(0, len(vals) - 1, 2)]
            return pd.DataFrame(rows, columns=["item", "value_pct"])
        return pd.DataFrame({
            "period": df.iloc[:, 0].astype(str).str.strip(),
            "fee_pct": [_pct_of(v) for v in df.iloc[:, 1].tolist()],
        })

    # 6h 负缓存：单只失败不该让「同一晚上的两次跑」出现两个不同的池子（见 _cached 说明）
    return _cached("fund_fee", f"{c}__{indicator}", load, post=post, fail_ttl_h=6)


def _env_on(name: str, default: bool = True) -> bool:
    """环境开关（``STOCK_FIN_INCREMENTAL`` / ``STOCK_NAV_INCREMENTAL``）：未设=开，0/false/no/off=关。"""
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def _nav_incremental(code: str, indicator: str) -> tuple[str, pd.DataFrame | None]:
    """场外基金历史净值（累计净值走势）跨日增量：昨天那份缓存 + bulk 表里的新行。

    为什么要增量：取数时东财 bulk 快照表 ``fund_open_fund_daily_em`` 里已经带着
    全部基金「前一交易日」的累计净值；而每只基金的全历史 LSJZ 请求（约 1.2k 行/只）
    每天只多出末尾一行。3080 只 × 1 个请求 ≈ 616s 就是 prefetch 里 fund 组的限流下限。

    返回 ``(mode, df)``：``("reuse", base)`` 缓存已是最新（无需追加）、``("append", merged)``、
    ``("fallback", None)`` 无法安全增量（调用方走全量）。任何时候不确定都回退全量。
    """
    base_path = _prev_cache_file("fund_nav", f"{code}__{indicator}")
    if not base_path:
        return "fallback", None
    try:
        base = pd.read_csv(base_path)
        base["date"] = pd.to_datetime(base["date"], errors="coerce")
        base["value"] = pd.to_numeric(base["value"], errors="coerce")
        base = (base.dropna(subset=["date", "value"])
                    .sort_values("date").reset_index(drop=True))
    except Exception:            # noqa: BLE001 —— 缓存损坏 → 全量
        return "fallback", None
    if len(base) == 0:
        return "fallback", None
    t_base = base["date"].iloc[-1].date()

    bulk = fetch_fund_nav_daily()          # 当日 bulk 表（1 个请求，本进程内已有缓存）
    if bulk is None or len(bulk) == 0 or "code" not in bulk.columns:
        return "fallback", None
    row = bulk[bulk["code"].astype(str).str.zfill(6) == code]
    if len(row) == 0:
        return "fallback", None
    r = row.iloc[0]

    def _add(day, value, out: dict):
        try:
            d = pd.Timestamp(str(day)).date()
        except Exception:        # noqa: BLE001 —— 日期文本异常 → 忽略该候选
            return
        if pd.isna(value):
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        out[d] = v

    latest_day, prev_day = r.get("nav_date"), r.get("prev_nav_date")
    latest_v, prev_v = r.get("acc_nav"), r.get("prev_acc_nav")
    published = {}
    _add(latest_day, latest_v, published)
    _add(prev_day, prev_v, published)
    if not published:                      # bulk 里这只基金一行可用值都没有 → 全量
        return "fallback", None
    up_to = max(published)
    cands = {d: v for d, v in published.items() if d > t_base}
    if not cands:
        if up_to <= t_base:                # 缓存已覆盖 bulk 的最新一天 → 原样复用
            return "reuse", base
        return "fallback", None
    # 交易日历校验：base 到最新候选日之间每个交易日都必须恰好有一行，否则说明断跑过
    # （中间漏了几天），只补两行会留下空洞 → 回退全量。
    try:
        cal = fetch_trade_calendar()["trade_date"].astype(str)
        need = {dt.date.fromisoformat(d) for d in cal}
    except Exception:          # noqa: BLE001 —— 日历不可用 → 全量
        return "fallback", None
    span = {d for d in need if t_base < d <= up_to}
    if span != set(cands):
        return "fallback", None
    nd = pd.DataFrame([{"date": pd.Timestamp(d), "value": v}
                       for d, v in sorted(cands.items())])
    for col in base.columns:              # 保持列集/列序与缓存一致（growth_pct 等补空）
        if col not in nd.columns:
            nd[col] = float("nan")
    nd = nd[list(base.columns)]
    merged = pd.concat([base, nd], ignore_index=True)
    merged = (merged.drop_duplicates("date", keep="last")
                    .sort_values("date").reset_index(drop=True))
    return "append", merged


def fetch_fund_nav_history(code: str, indicator: str = "累计净值走势") -> pd.DataFrame:
    """场外基金历史净值（成立来，逐只）。

    ⚠️ 算收益**必须用「累计净值走势」**：分红/拆分会把单位净值压低，用单位净值算出来的
    收益是系统性偏低的假数（这正是「同一份数据同一结论」要防的那种错）。
    indicator 另支持「单位净值走势」（带日增长率）等，缓存键带 indicator。
    返回列：date / value（累计净值或单位净值，随 indicator）/ growth_pct（有则给）

    取数模式（2026-11 起）：默认对「累计净值走势」走**跨日增量追加**——复用上一份缓存，
    只把 bulk 快照表里新发布的 1–2 行（前一交易日/当日）追加进去；断跑、缓存缺失、
    交易日历不连续、板块取值异常一律回退全量。实测（2026-11-17）3074 只逐值等价。
    """
    from .. import endpoints as ak

    c = norm_code(code)

    def load():
        if (indicator == "累计净值走势" and not _force_refresh()
                and _env_on("STOCK_NAV_INCREMENTAL")):
            try:
                mode, df = _nav_incremental(c, indicator)
            except Exception:      # noqa: BLE001 —— 增量路径任何异常都回退全量
                mode, df = "fallback", None
            if df is not None:
                _count("nav_reuse" if mode == "reuse" else "nav_append")
                return df
            _count("nav_incr_fallback")
        _count("nav_full")
        return _retry(lambda: ak.fund_open_fund_info_em(
            symbol=c, indicator=indicator, period="成立来").copy(), tries=2, base_sleep=2.0)

    def post(df):
        if "date" in df.columns and "value" in df.columns:   # 归一化表（缓存读回）
            out = df.copy()
        else:
            vcol = None
            for cand in ("累计净值", "单位净值", "净值"):
                if cand in df.columns:
                    vcol = df[cand]
                    break
            if vcol is None:
                raise KeyError(f"fund_nav/{c}: 找不到净值列（{list(df.columns)}）")
            out = pd.DataFrame({"date": _pick(df, "净值日期", "date"), "value": vcol})
            if "日增长率" in df.columns:
                out["growth_pct"] = _num(df["日增长率"])
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["value"] = _num(out["value"])
        return (out.dropna(subset=["date", "value"])
                   .sort_values("date").reset_index(drop=True))

    return _cached("fund_nav", f"{c}__{indicator}", load, post=post)


def fetch_money_fund_hist(code: str, page_size: int = 20,
                          days: int | None = None) -> pd.DataFrame:
    """单只货币基金的逐日收益历史。

    days=None（默认，行为与之前一致）→ 全历史（约 4815 行；东财 lsjz 每页 20 行翻 241 页
    ≈40–60 秒，快通道失败再退回慢路径 `fund_money_fund_info_em`）；
    days=N → 只取最近 N 个自然日（`startDate=数据日-N天`，翻到够 N 行即停，约 N/20 页
    ≈ N/100 秒）。缓存键仍是 `money_hist/<code>`，内容是「全历史或窗口」——下游
    cash_pool.py 只消费 tail(180)，因此同一只基金先窗口后全历史的缓存命中差异不影响结论。

    返回列：date（YYYY-MM-DD）/ wanfen（每万份收益，元）/ y7_pct（7日年化，百分数）/
    buy / sell（申购/赎回状态原文）；按 date 升序、去重。

    为什么要逐日历史而不只用 7 日年化：7 日年化是**滞后的滚动均值**，一只基金单日
    万份收益爆一下（例如国投瑞银增利宝 2026-11-12 的 0.998 元/万份，折单日约 0.01%
    ≈ 年化 3.6%），7 日年化会瞬间从 0.95% 跳到 1.34%，7 天后又掉回 1.0% 附近。
    招行「朝朝盈2号」按 7 日年化排序，等于系统性地推荐「刚爆过单日收益」的基金。
    用 Σ万份收益 算 90 天实得年化才不会被单日脉冲骗（见 cash_pool.py）。
    """
    page_size = max(1, min(int(page_size), 20))      # 与服务端上限一致，保证短页判断正确

    def _paged(start_date: str = "", stop_rows: int | None = None) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        got = 0
        page = 1
        while page <= 400:
            chunk = _em_fund_lsjz(code, page_size, page_index=page, start_date=start_date)
            if chunk.empty:
                break
            frames.append(chunk)
            got += len(chunk)
            if len(chunk) < page_size or (stop_rows is not None and got >= stop_rows):
                break
            page += 1
        if not frames:
            raise ValueError(f"fund {code}: lsjz 分页无数据")
        return pd.concat(frames, ignore_index=True)

    def load():
        try:
            if days is None:
                return _paged()
            since = dt.date.fromisoformat(data_date()) - dt.timedelta(days=max(1, int(days)))
            return _paged(start_date=since.strftime("%Y-%m-%d"), stop_rows=int(days))
        except Exception as e:               # 快通道失败 → 退回取数层（慢但稳）
            # 静默降级会让「窗口取数」悄悄变成「全历史」而不自知（CORE-6）：
            # 记 provenance + stderr，复盘时能看出这一只走了慢路径。
            _prov.append({"kind": "money_hist", "key": str(code),
                          "source": "lsjz_fallback",
                          "error": f"{type(e).__name__}: {e}"})
            print(f"[datasource] money_hist {code}: lsjz 分页失败（{type(e).__name__}），"
                  f"退回全历史慢路径", file=sys.stderr)
            from .. import endpoints as ak
            return _retry(lambda: ak.fund_money_fund_info_em(symbol=str(code)).copy(),
                          tries=2, base_sleep=2.0)

    def post(df):
        if "date" in df.columns and "wanfen" in df.columns:   # 已归一化（缓存读回）
            df["date"] = df["date"].astype(str)
            df["wanfen"] = _num(df["wanfen"])
            df["y7_pct"] = _num(df["y7_pct"])
            for c in ("buy", "sell"):
                if c in df.columns:
                    df[c] = df[c].astype(str)
        else:
            NEED = ("净值日期", "每万份收益", "7日年化收益率")
            missing = [c for c in NEED if c not in df.columns]
            if missing:
                raise KeyError(
                    "货基历史表结构变了：缺少列 %s；现有列 %s"
                    % (missing, list(df.columns)[:20]))
            df = pd.DataFrame({
                "date": _pick(df, "净值日期", "date").astype(str),
                "wanfen": _num(_pick(df, "每万份收益", "wanfen")),
                "y7_pct": _num(_pick(df, "7日年化收益率", "y7_pct")),
                "buy": _pick(df, "申购状态", "buy").astype(str),
                "sell": _pick(df, "赎回状态", "sell").astype(str),
            })
        df = df[df["date"].astype(str).str.len() >= 8].copy()
        df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        return df

    # 缓存键按窗口分桶（CORE-6）：全历史与「最近 N 天」是两份不同内容，
    # 共用 money_hist/<code> 会让窗口模式读到全历史缓存、或反过来（下游只看 tail）。
    cache_key = f"{code}__{int(days)}d" if days else str(code)
    return _cached("money_hist", cache_key, load, post=post, fail_ttl_h=12.0)


def fetch_repo_rate_7d() -> dict | None:
    """深交所 R-007(131801) 7 天期国债逆回购利率实测（年化，小数）。

    2026-11-15 用户 m01500：观望资金买 7 天以上逆回购 → 现金层需要 7 天期的实测利率。
    口径与 `fetch_repo_rate()` 一致：交易所日线收盘价 = 当日年化（%），取近 250 个交易日均值；
    R-007 1 手 = 1,000 元起，适合 5 万本金；沪市 GC007(204007) 10 万元起，仅作兜底。
    全部失败 → None（不编数据）。
    """
    from .. import endpoints as ak

    for symbol, label in (("sz131801", "R-007"), ("sh204007", "GC007")):
        try:
            def load():
                return _retry(lambda: ak.bond_zh_hs_daily(symbol=symbol).copy(),
                              tries=2, base_sleep=2.0)

            df = _cached("repo_rate_7d", f"{label.lower()}_{symbol}", load, post=_post_daily)
            if len(df) < 60:
                continue
            dd = df.dropna(subset=["close"]).copy()
            dd["date"] = pd.to_datetime(dd["date"], errors="coerce")
            dd = dd.dropna(subset=["date"]).sort_values("date")
            t = dd.tail(250)
            vals = _num(t["close"]).dropna()
            mean_pct = float(vals.mean())
            return {"rate": round(mean_pct / 100.0, 6),
                    "rate_pct": round(mean_pct, 4),
                    "samples": int(len(vals)),
                    "sample_start": str(t["date"].iloc[0].date()),
                    "sample_end": str(t["date"].iloc[-1].date()),
                    "last_pct": round(float(vals.iloc[-1]), 4),
                    "mean_60_pct": round(float(_num(dd["close"]).dropna().tail(60).mean()), 4),
                    "source": f"ak.bond_zh_hs_daily({symbol})",
                    "interface": "bond_zh_hs_daily", "symbol": symbol,
                    "is_proxy": False,
                    "basis": (f"{label}({symbol}) 交易所 7 天期国债逆回购，近 {len(vals)} 个交易日"
                              f"收盘价均值 {mean_pct:.3f}%（样本 {t['date'].iloc[0].date()}→"
                              f"{t['date'].iloc[-1].date()}），非代理")}
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# 全市场公告（巨潮，按披露日）
# --------------------------------------------------------------------------- #
_NOTICE_COLS = ["代码", "名称", "公告标题", "公告类型", "公告日期", "网址"]


def _day_arg(raw: str) -> str:
    """``YYYYMMDD`` / ``YYYY-MM-DD`` / ``''`` → ``YYYY-MM-DD`` 或 ``''``。"""
    s = str(raw or "").strip().replace("/", "-")
    if not s:
        return ""
    s = s.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期需为 YYYYMMDD 或 YYYY-MM-DD：{raw!r}")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _notice_frame(df) -> pd.DataFrame:
    """把公告表规整成契约列（缺列报错，不静默补空）。"""
    if df is None:
        return pd.DataFrame(columns=_NOTICE_COLS)
    if len(df) == 0:
        return pd.DataFrame(columns=_NOTICE_COLS)
    missing = [c for c in _NOTICE_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"全市场公告表结构变了：缺少列 {missing}；现有列 {list(df.columns)[:12]}")
    out = df[_NOTICE_COLS].copy()
    out["代码"] = out["代码"].astype(str).str.strip().str.zfill(6)
    for c in ("名称", "公告标题", "公告类型", "网址"):
        out[c] = out[c].fillna("").astype(str)
    out["公告日期"] = out["公告日期"].astype(str).str.slice(0, 10)
    return out.drop_duplicates(subset=["代码", "公告标题", "公告日期"]).reset_index(drop=True)


def _market_is_trading_day(day: str) -> bool | None:
    """day 是否 A 股交易日；日历取不到时返回 None（调用方按「不知道」处理）。"""
    try:
        cal = fetch_trade_calendar()
        return str(day) in set(cal["trade_date"].astype(str))
    except Exception:
        return None


def fetch_market_announcements(date_from: str = "", date_to: str = "",
                               force: bool = False) -> pd.DataFrame:
    """全市场公告（巨潮，按披露日，覆盖候选池之外的标的）。

    为什么要单独一条链路：``invest/scripts/cninfo.py`` 逐只查公告只覆盖候选池
    （30–300 只），持仓或池外标的的突发利空（立案、退市、商誉减值…）会漏。巨潮的
    全市场查询一次能拿全 A 股当天全部公告（约 1,300–2,100 条），按标题正则打标即可。

    缓存：每个披露日一份 ``cninfo_market/<YYYY-MM-DD>.csv``。公告发布后内容不再变，
    因此跨日永久复用（与资金流/估值这类会变的日频快照不同）。
    离线模式（``A_SHARE_OFFLINE=1``）缺当日缓存 → 抛错，绝不联网；接口返回空表也抛错
    （空表更像接口异常，不能当成「今天没有公告」）。

    契约列：``代码 / 名称 / 公告标题 / 公告类型 / 公告日期 / 网址``
    """
    from .. import endpoints as ak

    day_to = _day_arg(date_to) or data_date()
    day_from = _day_arg(date_from) or day_to
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    d0 = dt.date.fromisoformat(day_from)
    d1 = dt.date.fromisoformat(day_to)
    span = (d1 - d0).days + 1
    if span > 31:
        raise ValueError(f"单次最多取 31 天公告：{day_from}→{day_to}（{span} 天）")

    root = os.path.join(CACHE_ROOT, "cninfo_market")
    frames: list[pd.DataFrame] = []
    d = d0
    while d <= d1:
        day = d.isoformat()
        path = os.path.join(root, f"{day}.csv")
        df = None
        cached = False
        if os.path.exists(path) and not force:
            try:
                df = _notice_frame(pd.read_csv(path, dtype={"代码": str}, encoding="utf-8"))
                cached = True
            except Exception:
                df, cached = None, False
            if cached:
                _prov.append({"kind": "cninfo_market", "key": day,
                              "source": "cache", "rows": int(len(df))})
        if not cached:
            if offline():
                _offline_guard("cninfo_market", day, path)
            df = _notice_frame(ak.stock_notice_report(day))
            if len(df) == 0:
                # 非交易日没有公告是正常的（周末/节假日）；交易日返回空表则更像接口异常
                if _market_is_trading_day(day) is False:
                    os.makedirs(root, exist_ok=True)
                    pd.DataFrame(columns=_NOTICE_COLS).to_csv(
                        path, index=False, encoding="utf-8")
                    _prov.append({"kind": "cninfo_market", "key": day,
                                  "source": "endpoints", "rows": 0,
                                  "note": "非交易日无公告"})
                    d = d + dt.timedelta(days=1)
                    continue
                raise RuntimeError(
                    f"巨潮 {day} 全市场公告返回空表（接口异常，不当作「没有公告」）")
            os.makedirs(root, exist_ok=True)
            df.to_csv(path, index=False, encoding="utf-8")
            _prov.append({"kind": "cninfo_market", "key": day,
                          "source": "endpoints", "rows": int(len(df))})
        frames.append(df)
        d = d + dt.timedelta(days=1)

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["代码", "公告标题", "公告日期"])
    return out.sort_values(["公告日期", "代码"]).reset_index(drop=True)


def fetch_notice_text(url: str, force: bool = False) -> str:
    """巨潮公告 PDF 正文（纯文本，按 URL 永久缓存）。

    为什么要正文：公告标题只能打到「减持/立案」这一层，正文才能分清
    「拟减持」和「已减持完成」、「问询函」和「行政处罚决定书」。
    公告 PDF 发布后不再变，因此按 URL 的 sha1 永久缓存，不做日期失效。
    离线模式（``A_SHARE_OFFLINE=1``）缺缓存 → 抛错，绝不联网。

    :param url: ``ep.stock_notice_report`` 返回的「网址」列（巨潮 PDF 地址）
    """
    from .. import endpoints as ak

    u = str(url or "").strip()
    if not u.lower().startswith("http"):
        raise ValueError(f"url 需为巨潮公告 PDF 地址：{url!r}")
    if not CACHE_ROOT:
        raise RuntimeError("缓存目录未初始化：先在技能里 import config 再取数")
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]
    root = os.path.join(CACHE_ROOT, "notice_text")
    path = os.path.join(root, key + ".txt")
    if os.path.isfile(path) and not force:
        with io.open(path, encoding="utf-8") as f:
            text = f.read()
        _prov.append({"kind": "notice_text", "key": key, "url": u,
                      "source": "cache", "chars": len(text)})
        return text
    if offline():
        _offline_guard("notice_text", key, path)
    text = ak.stock_notice_text(u)
    if not str(text or "").strip():
        raise RuntimeError(f"公告正文为空（PDF 解析失败？）：{u}")
    os.makedirs(root, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _prov.append({"kind": "notice_text", "key": key, "url": u,
                  "source": "endpoints", "chars": len(text)})
    return text


# ---------------------------------------------------------------------------
# A 股基础信息 / 历史退市 / ST 区间（baostock，PIT 口径）
# ---------------------------------------------------------------------------
# 为什么要这三样：回测只能看见「今天还活着」的票，退市/曾 ST 的样本消失了，
# 年化会偏高、回撤会偏小（幸存者偏差）；筛选的「非 ST」也只能看今天的简称。
# baostock 的 query_stock_basic 带 outDate（退市日）、日线带 isST（当日是否风险警示），
# 用它把「当时是否还在市、当时是不是 ST」还原出来。
_STOCK_BASIC_COLS = ["代码", "名称", "上市日", "退市日", "类型", "状态"]
_ST_INTERVAL_COLS = ["起始日", "结束日"]
_BS_TYPE = {"1": "股票", "2": "指数", "3": "其它"}


def _bs_login():
    """baostock 登录（PIT 层同源）；调用方负责 logout。"""
    import baostock as bs
    lg = bs.login()
    if str(getattr(lg, "error_code", "1")) != "0":
        raise RuntimeError(f"baostock 登录失败：{getattr(lg, 'error_msg', '')}")
    return bs


def _bs_rows(rs) -> list[list[str]]:
    if str(getattr(rs, "error_code", "1")) != "0":
        raise RuntimeError(f"baostock 取数失败：{getattr(rs, 'error_msg', '')}")
    out = []
    while rs.next():
        out.append(rs.get_row_data())
    return out


def _bs_symbol(code: str) -> str:
    """6 位代码 → baostock 代码（sh.600000 / sz.000001）。"""
    p = norm_code(code)
    if len(p) != 6 or not p.isdigit():
        raise ValueError(f"不是有效 A 股代码：{code!r}")
    if p.startswith(("6", "9", "5")):
        return "sh." + p
    if p.startswith(("0", "1", "2", "3")):
        return "sz." + p
    raise ValueError(f"baostock 不支持该代码：{code!r}")


def _basic_frame(rows: list[list[str]]) -> pd.DataFrame:
    out = []
    for r in rows:
        d = dict(zip(["code", "code_name", "ipoDate", "outDate", "type", "status"], r))
        out.append({
            "代码": norm_code(d.get("code")),
            "名称": str(d.get("code_name") or "").strip(),
            "上市日": str(d.get("ipoDate") or "").strip(),
            "退市日": str(d.get("outDate") or "").strip(),
            "类型": _BS_TYPE.get(str(d.get("type")), str(d.get("type"))),
            "状态": "上市" if str(d.get("status")) == "1" else "退市",
        })
    df = pd.DataFrame(out, columns=_STOCK_BASIC_COLS)
    if df.empty:
        raise RuntimeError("A 股基础信息为空：baostock query_stock_basic 返回 0 行")
    return df.drop_duplicates(["代码"]).reset_index(drop=True)


def _norm_basic(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["代码"] = out["代码"].astype(str).str.strip().str.zfill(6)
    for c in ("上市日", "退市日"):
        if c in out.columns:
            out[c] = out[c].fillna("").astype(str).str.strip()
    return out


def fetch_stock_basic(force: bool = False) -> pd.DataFrame:
    """A 股/指数基础信息（baostock query_stock_basic，含退市日）。

    返回列：代码/名称/上市日/退市日/类型/状态。全市场约 9 千行（股票 5.5 千）。
    """
    def load():
        bs = _bs_login()
        try:
            rows = _bs_rows(bs.query_stock_basic())
        finally:
            bs.logout()
        return _basic_frame(rows)

    return _norm_basic(_cached("stock_basic", "bs", load, force=force))


def fetch_delisted(force: bool = False) -> pd.DataFrame:
    """历史退市股票名单（按退市日倒序）：代码/名称/上市日/退市日。

    只留股票（不含指数），只留已有 outDate 的——这是回测里「当时还在市」的
    时间下界，也是幸存者偏差的量化口径。
    """
    df = fetch_stock_basic(force=force)
    out = df[(df["类型"] == "股票") & (df["退市日"].astype(str).str.strip() != "")].copy()
    out = out[["代码", "名称", "上市日", "退市日"]]
    if not out.empty:
        out = out.sort_values("退市日", ascending=False).reset_index(drop=True)
    return out


def _st_spans(rows: list[list[str]]) -> pd.DataFrame:
    spans, cur, last = [], None, None
    for r in rows:
        d, st = str(r[0]), str(r[1]).strip()
        if st == "1":
            if cur is None:
                cur = d
            last = d
        elif cur is not None:
            spans.append({"起始日": cur, "结束日": last})
            cur, last = None, None
    if cur is not None:
        spans.append({"起始日": cur, "结束日": last})
    return pd.DataFrame(spans, columns=_ST_INTERVAL_COLS)


def fetch_st_intervals(code: str, start: str = "", end: str = "", force: bool = False) -> pd.DataFrame:
    """某只股票的历史风险警示（ST/*ST）区间：起始日/结束日（含端点）。

    数据来自 baostock 日线 isST 字段（逐日标记），连续标记合并成区间；
    查不到 isST=1 时返回空表（不是错误）。
    """
    p = norm_code(code)
    key = p if not (start or end) else f"{p}_{str(start).replace('-', '')}_{str(end).replace('-', '')}"
    path = _cache_file("st_hist", key)
    do_force = (force or _force_refresh()) and not offline()
    if os.path.isfile(path) and not do_force:
        try:
            df = pd.read_csv(path, dtype=str).fillna("")
            if len(df.columns) > 0:
                _prov.append({"kind": "st_hist", "key": key, "source": "cache",
                              "spans": len(df)})
                return df[_ST_INTERVAL_COLS]
        except Exception:
            pass  # 缓存坏了重取

    def load():
        bs = _bs_login()
        try:
            rs = bs.query_history_k_data_plus(
                _bs_symbol(p), "date,isST",
                start_date=(str(start) or "1990-01-01"),
                end_date=(str(end) or data_date()),
                frequency="d", adjustflag="3")
            rows = _bs_rows(rs)
        finally:
            bs.logout()
        return _st_spans(rows)

    if offline():
        _offline_guard("st_hist", key, path)
    df = load()
    # 空表（从来没被 ST 过）也要落盘：否则每次调用都重打接口，且 _cached 会把空表当错误
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_csv(path, index=False)
    except Exception as exc:  # noqa: BLE001 —— 落盘失败不影响本次返回，但要留痕
        _prov.append({"kind": "st_hist", "key": key, "source": "endpoints",
                      "write_error": f"{type(exc).__name__}: {exc}"})
    _prov.append({"kind": "st_hist", "key": key, "source": "endpoints",
                  "spans": len(df)})
    return df


def is_st_on(code: str, day: str) -> bool:
    """某只股票在某个交易日是否处于风险警示（ST/*ST）状态。"""
    df = fetch_st_intervals(code)
    if df is None or len(df) == 0:
        return False
    d = str(day)[:10]
    return bool(((df["起始日"].astype(str) <= d) & (df["结束日"].astype(str) >= d)).any())


# ---------------------------------------------------------------- 个股分红送配
def fetch_dividends(code, force: bool = False) -> pd.DataFrame:
    """个股分红送配（东财数据中心）。

    返回列：代码/名称/公告日期/股权登记日/除权除息日/每10股派息/每10股送转/方案/进度，
    只保留**已落地**（有除权除息日）的方案；``每10股派息`` 是含税口径，换算每股要 /10。
    缓存按天（方案落地后不再变），``force=True`` 强制重取。
    """
    from .. import endpoints as ak

    c = norm_code(code)
    load = lambda: ak.stock_dividend_em(symbol=c).copy()  # noqa: E731
    cols = ["代码", "名称", "公告日期", "股权登记日", "除权除息日",
            "每10股派息", "每10股送转", "方案", "进度"]
    try:
        df = _cached("dividends", c, load, force=force)
    except ValueError as exc:
        # 「从没分过红」是正常状态，不是取数失败：返回空表，别让上层当异常处理
        if "空表" in str(exc):
            return pd.DataFrame(columns=cols)
        raise
    if df is not None and len(df) and "代码" in df.columns:
        # 落盘缓存是 CSV、读回来 pandas 会把 000999 推成 int 999，这里统一补回 6 位
        df = df.copy()
        df["代码"] = (df["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
                      .str.zfill(6))
    return df



# ---------------------------------------------------------------- 财务报表 / 业绩预告
def fetch_fin_balance(code, force: bool = False) -> pd.DataFrame:
    """资产负债表（东财 F10，最近 8 期，含 NOTICE_DATE 披露日；可用于 PIT 过滤）。"""
    from .. import endpoints as ak
    c = norm_code(code)
    return _cached("fin_balance", c,
                   lambda: ak.stock_fin_balance_em(symbol=c, limit=8).copy(), force=force)


def fetch_fin_income(code, force: bool = False) -> pd.DataFrame:
    """利润表（东财 F10，最近 8 期，含 NOTICE_DATE）。"""
    from .. import endpoints as ak
    c = norm_code(code)
    return _cached("fin_income", c,
                   lambda: ak.stock_fin_income_em(symbol=c, limit=8).copy(), force=force)


def fetch_fin_cashflow(code, force: bool = False) -> pd.DataFrame:
    """现金流量表（东财 F10，最近 8 期，含 NOTICE_DATE）。"""
    from .. import endpoints as ak
    c = norm_code(code)
    return _cached("fin_cashflow", c,
                   lambda: ak.stock_fin_cashflow_em(symbol=c, limit=8).copy(), force=force)


def fetch_forecast(code) -> pd.DataFrame:
    """业绩预告/快报口径（东财 RPT_PUBLIC_OP_NEWPREDICT，全部历史期）。

    只保留「归属于上市公司股东的净利润」这一口径，避免同一报告期出现多条
    （扣非/每股收益等）把下游用乱；返回列含 指标/预告类型/净利润上下限/同比上下限。
    """
    from .. import endpoints as ak
    c = norm_code(code)
    df = _cached("forecast", c,
                 lambda: ak.stock_forecast_em(symbol=c, limit=40).copy())
    if df is None or df.empty:
        return pd.DataFrame(columns=ak.FORECAST_COLS)
    if "指标" in df.columns:
        keep = df["指标"].astype(str).str.contains("净利润", na=False)
        hit = df[keep]
        if not hit.empty:
            df = hit
    return df.reset_index(drop=True)


def _prev_year_row(bal, report):
    """取「上年同期」那一行（2026-09-30 ↔ 2025-09-30）；找不到返回 None。

    为什么不退回上一报告期：``REPORT_DATE`` 相邻两行是环比（Q2 vs Q1），
    拿它当「同比」会系统性地把季节性波动算成同比变化。
    """
    if bal is None or getattr(bal, "empty", True) or "REPORT_DATE" not in bal.columns:
        return None
    txt = str(report or "")[:10]
    try:
        rd = dt.date.fromisoformat(txt)
        want = rd.replace(year=rd.year - 1).isoformat()
    except ValueError:
        return None
    hit = bal[bal["REPORT_DATE"].astype(str).str.slice(0, 10) == want]
    return hit.iloc[0] if len(hit) else None


def fin_quality(code) -> dict:
    """从三表抽一组「质量变化」指标（最新报告期 vs 上年同期，单位：元）。

    返回 ``{"report": 最新报告期, "notice": 披露日, "receivable_yoy": 应收同比,
    "inventory_yoy": 存货同比, "debt_yoy": 有息负债同比(有界口径), "cfo_to_np": 现金流/净利}``；
    字段缺失或是 None 时对应值为 None（不猜）。
    """
    bal = fetch_fin_balance(code)
    inc = fetch_fin_income(code)
    cfl = fetch_fin_cashflow(code)
    out = {"report": "", "notice": "", "receivable_yoy": None,
           "inventory_yoy": None, "debt_yoy": None, "cfo_to_np": None}
    if bal is None or bal.empty:
        return out
    b = bal.iloc[0]
    out["report"] = str(b.get("REPORT_DATE") or "")
    out["notice"] = str(b.get("NOTICE_DATE") or "")

    prev_row = _prev_year_row(bal, out["report"])

    def _yoy(col):
        if col not in bal.columns or prev_row is None:
            return None
        try:
            cur = float(b.get(col))
            prev = float(prev_row.get(col))
        except (TypeError, ValueError):
            return None
        if prev <= 0:
            return None
        return round(cur / prev - 1.0, 4)

    out["receivable_yoy"] = _yoy("ACCOUNTS_RECE")
    out["inventory_yoy"] = _yoy("INVENTORY")
    # 有息负债口径：短期借款 + 一年内到期非流动负债 + 长期借款 + 应付债券
    debt_cols = [c for c in ("SHORT_LOAN", "NONCURRENT_LIAB_1YEAR",
                             "LONG_LOAN", "BOND_PAYABLE") if c in bal.columns]
    if debt_cols:
        try:
            cur = sum(float(b.get(c) or 0) for c in debt_cols)
            prev = sum(float(prev_row.get(c) or 0) for c in debt_cols) if prev_row is not None else 0.0
            if prev > 0:
                out["debt_yoy"] = round(cur / prev - 1.0, 4)
        except (TypeError, ValueError, IndexError):
            pass
    try:
        np_ = float(inc.iloc[0].get("PARENT_NETPROFIT") or 0)
        cfo = float(cfl.iloc[0].get("NETCASH_OPERATE") or 0)
        if np_ > 0:
            out["cfo_to_np"] = round(cfo / np_, 3)
    except (TypeError, ValueError, IndexError):
        pass
    return out
