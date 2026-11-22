# -*- coding: utf-8 -*-
r"""同花顺金融数据 API（fuyao.aicubes.cn）—— **可选**数据源，只加不替。

定位
----
本模块是取数接口层之外的**第二数据源**，用途只有两个：
  1. 交叉校验：拿同花顺口径去撞我们自算的数字，撞不上就说明有一边错了；
  2. 补空白：ETF 前复权日线、场外基金区间收益/净值、批量估值快照。
**没有 API Key 时全部函数返回 None/空**，主链路照走取数接口层 —— 默认零依赖、零回归。

三条纪律（与 datasource.py 一致）
--------------------------------
1. 溯源：每次真实取数记 `kind/key/source/rows`，落到 `CACHE_DIR/fuyao_provenance.json`；
2. fail loud：没有 key / 网络错 / 服务 5xx 一律抛 `FuyaoError`，不返回假数据；
3. 日内缓存：同一自然日复用，跨日重取（与 `config.CACHE_TTL_DAYS` 同口径）。

契约来源：同花顺官方仓库 Hithink-Tech-Org/Financial-API（MIT）的 `docs/api/`，
2026-11-14 逐条复核过；本模块只是**零依赖的官方 REST 封装**，不引入官方 CLI/SDK 依赖。

⚠️ 能力边界（2026-11-14 复核，官方文档口径）
--------------------------------------------
1. **公开 API 不提供**：分钟 K、tick、Level-2、港股/美股行情、宏观数据、新闻/公告原文、研报原文
   （官方 README「数据能力与边界」明确列出）。别在这几个方向提需求，换源。
2. **端内专用（`code=2004`）**：`capital-flow`（主力资金）、`fund/etf-flow`（ETF 净流入）、
   `high-frequency`（高频动向）—— 官方文档标「端内专用 · 同花顺AI客户端可用」，
   **不是参数写错，端外永久取不到**，不要重试、不要当 bug 修。
3. `financials/*` 的 `report_date_ms` **不是真实披露日**，系统性滞后约 1 年：
   茅台 2024 年报 report_date=2026-04-17（实际 2025-04-02），2023 年报=2025-04-03（实际 2024-04）。
   → 本模块**不承诺 point-in-time 能力**；`asof_rows()` 只是「数据不早于」的保守代理，
   用它回测会**低估**当时可获得的信息（安全方向，但失真），不能宣称修好了幸存者偏差。
4. `fund/performance/drawdowns` 对 018074/012310/110017/000033 全部返回 0，
   与我方自算（018074 三年 -1.37%）直接冲突 → **不可用**，回撤继续自算。
5. 窗口硬约束（客户端现在**预校验**，与官方 SDK 的校验一致，省一次 1003 往返）：
   - 三表（financials）：≤10 个自然年；
   - 单只日 K（historical）：>10 年**自动切片拼接**（官方 SDK 同策略）；
   - 指数历史（index_hist）：**只覆盖近 5 个自然年**；
   - ETF 历史（fund/market/historical）：≤5 个自然年；
   - 基金日度指标（fund_indicators_hist）：≤5 个自然年；
   - 批量上限：估值 100 只/次、异动 50 只/次、快照 100 只/次。

已实测可用的端点
----------------
- `GET /api/a-share/prices/snapshot|historical`
- `GET /api/a-share/financials/{income-statements,balance-sheets,cash-flow-statements}`
- `GET /api/a-share/financials/indicators`（一次一个报告期）
- `GET /api/a-share/valuations/snapshot`（≤100 只/次）
- `GET /api/a-share-index/constituents/ths-stock-list` —— **2026-11-14 复测可用**：
  `thscode=000300.SH` → 300 只（当年记的「REST 404」作废，真正原因应是旧参数/旧路由）
- `GET /api/a-share-index/prices/snapshot|historical`（指数快照 / 近 5 年日 K）
- `GET /api/a-share/corporate-actions/adjustment-factors`（分红送转复权事件）
- `GET /api/fund/profile/detail`、`/api/fund/performance/nav|returns`、`/api/fund/market/historical`
- `GET /api/a-share/calendar/trading-days`（未封装，需要时按 `_data()` 直接调）
- 特色数据（涨跌停池/龙虎榜/热榜/异动）：可取，但属短线情绪，与本站「R2/R3 中长线」无关，
  仅保留薄封装，不参与决策（避免把噪音引进来）。

Key 放哪（顺序与官方 SDK 对齐）
-------------------------------
1. 环境变量 `HITHINK_FINANCE_API_KEY`（官方推荐名）
2. 环境变量 `FUYAO_API_KEY`（本站旧名，保留）
3. 环境变量 `FUYAO_TOKEN` / `API_KEY`（官方 legacy 名）
4. 官方用户级凭据文件 `%APPDATA%\hithink-finance\credentials.env`
   （内容形如 `HITHINK_FINANCE_API_KEY=xxx`，**不能加引号**，与官方 `marketdb.credentials` 解析一致）
5. `WORK_DIR/fuyao.key`（每行一个 key，`#` 开头忽略）
**key 不进技能目录、不进文档、不进 git。**
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config

BASE_URL = "https://fuyao.aicubes.cn"
OFFICIAL_REPO = "https://github.com/Hithink-Tech-Org/Financial-API"
KEY_ENV = "FUYAO_API_KEY"          # 本站旧名，保留兼容
KEY_ENVS = ("HITHINK_FINANCE_API_KEY", "FUYAO_API_KEY", "FUYAO_TOKEN", "API_KEY")
KEY_FILE = os.path.join(config.WORK_DIR, "fuyao.key")
# 官方用户级凭据文件（与 marketdb.credentials.credential_file_path 同路径）
CRED_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser(
    "~/AppData/Roaming"), "hithink-finance", "credentials.env")
CACHE_ROOT = os.path.join(config.CACHE_DIR, "fuyao")

# 限流：不限累计次数，但要求别打太密（4001 / HTTP 429 = 触发限流）
MIN_INTERVAL_S = 0.20
TIMEOUT_S = 30

# 请求契约（照抄官方 SDK fuyao_client.py 的客户端校验，先在本地拦下再出网）
ADJUSTS = ("none", "forward", "backward")
PERIODS = ("annual", "quarterly")
MAX_SNAPSHOT_CODES = 100       # 行情快照批量上限
MAX_VALUATION_CODES = 100      # 估值快照批量上限
MAX_INDUSTRY_CODES = 100       # 板块归属批量上限
MAX_ANOMALY_CODES = 50         # 个股异动批量上限
MAX_FINANCIAL_YEARS = 10       # 三表窗口（自然年）
MAX_KLINE_YEARS = 10           # 单只日 K 窗口（超出自动切片拼接）
MAX_INDEX_YEARS = 5            # 指数历史只覆盖近 5 个自然年
MAX_FUND_HIST_YEARS = 5        # ETF/基金历史、基金日度指标窗口

# 端点表：name -> (path, 参数白名单)
ENDPOINTS = {
    "snapshot": "/api/a-share/prices/snapshot",
    "historical": "/api/a-share/prices/historical",
    "income": "/api/a-share/financials/income-statements",
    "balance": "/api/a-share/financials/balance-sheets",
    "cashflow": "/api/a-share/financials/cash-flow-statements",
    "indicators": "/api/a-share/financials/indicators",
    "valuations": "/api/a-share/valuations/snapshot",
    "fund_profile": "/api/fund/profile/detail",
    "fund_nav": "/api/fund/performance/nav",
    "fund_returns": "/api/fund/performance/returns",
    "fund_hist": "/api/fund/market/historical",
    "fund_drawdowns": "/api/fund/performance/drawdowns",
    "fund_indicators_hist": "/api/fund/performance/indicators-historical",
    "calendar": "/api/a-share/calendar/trading-days",
    # 资讯域（同花顺 iNews）
    "news_stock": "/api/news/stock/timeline-list",
    "news_flash": "/api/news/flash/list",
    "news_notice": "/api/news/notice/detail",
    "news_anomaly": "/api/news/anomaly/analysis",
    "anomaly_stock": "/api/a-share/special-data/anomaly-analysis-stock",
    # 指数 / 板块（当前成分，非历史）
    "index_members": "/api/a-share-index/constituents/ths-stock-list",
    "index_snapshot": "/api/a-share-index/prices/snapshot",
    "index_hist": "/api/a-share-index/prices/historical",
    "stock_industries": "/api/a-share-index/constituents/ths-index-list-by-stock",
    # 除复权事件 / 全市场全量导出
    "adjust_factors": "/api/a-share/corporate-actions/adjustment-factors",
    "dump_daily_k": "/api/dump/market-dumps/daily-k/download-url",
    "dump_daily_k_10d": "/api/dump/market-dumps/daily-k-10d/download-url",
    "dump_adjustment_factors": "/api/dump/market-dumps/adjustment-factors/download-url",
    # 特色数据（情绪层）：涨跌停池、炸板池、龙虎榜、热股榜、异动监控
    "limit_up_pool": "/api/a-share/special-data/limit-up-pool",
    "limit_down_pool": "/api/a-share/special-data/limit-down-pool",
    "limit_break_pool": "/api/a-share/special-data/limit-break-pool",
    "dragon_tiger": "/api/a-share/special-data/dragon-tiger-list",
    "dragon_tiger_inst": "/api/a-share/special-data/dragon-tiger-stock-institution",
    "hot_stock_list": "/api/a-share/special-data/hot-stock-list",
    "anomaly_monitor": "/api/a-share/special-data/abnormal-monitor-list",
}
STATEMENTS = {"income": "income", "balance": "balance", "cashflow": "cashflow",
              "利润表": "income", "资产负债表": "balance", "现金流量表": "cashflow"}

_prov: list[dict] = []
_last_call = [0.0]


class FuyaoError(RuntimeError):
    """取数失败（无 key / 网络 / 服务端错 / 被限流重试后仍失败）。"""


# ---------------------------------------------------------------- key 与可用性
def _cred_file_key() -> str | None:
    """官方用户级凭据文件 %APPDATA%\\hithink-finance\\credentials.env。

    只认 `HITHINK_FINANCE_API_KEY=xxx` 一行（官方解析器要求**不带引号**，加了引号视为非法）。
    """
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                name, _, val = s.partition("=")
                if name.strip() == "HITHINK_FINANCE_API_KEY":
                    v = val.strip()
                    if v and v[0] not in ("'", '"'):
                        return v
    except OSError:
        return None
    return None


def _key_from_envs() -> tuple[str | None, str]:
    for name in KEY_ENVS:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v, f"env:{name}"
    return None, ""


def api_key() -> str | None:
    """按官方 SDK 的顺序找 key：环境变量 → 官方凭据文件 → WORK_DIR/fuyao.key。"""
    k, _ = _key_from_envs()
    if k:
        return k
    k = _cred_file_key()
    if k:
        return k
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    return s
    except OSError:
        return None
    return None


def key_source() -> str:
    """key 从哪来（**只报来源，不报值**），用于自检排障。"""
    k, src = _key_from_envs()
    if k:
        return src
    if _cred_file_key():
        return f"file:{CRED_FILE}"
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    return f"file:{KEY_FILE}"
    except OSError:
        pass
    return "MISSING"


def available() -> bool:
    return bool(api_key())


def _throttle() -> None:
    gap = time.time() - _last_call[0]
    if gap < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - gap)
    _last_call[0] = time.time()


# ---------------------------------------------------------------- 缓存与请求
def _cache_path(name: str, params: dict) -> str:
    raw = name + "?" + urllib.parse.urlencode(sorted(params.items()))
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in raw)[:150]
    return os.path.join(CACHE_ROOT, name, f"{safe}__{dt.date.today().isoformat()}.json")


def _http(name: str, **params) -> dict:
    """真正发请求，返回响应信封；网络/服务错抛 FuyaoError，业务错原样返回。"""
    key = api_key()
    if not key:
        raise FuyaoError("没有 FUYAO_API_KEY（或 WORK_DIR/fuyao.key），同花顺数据源不可用")
    path = ENDPOINTS[name]
    q = urllib.parse.urlencode({k: v for k, v in params.items()
                                if v is not None and v != ""})
    url = BASE_URL + path + (("?" + q) if q else "")
    last = None
    for attempt in range(3):
        _throttle()
        req = urllib.request.Request(url, headers={"X-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body)
        except urllib.error.HTTPError as e:                     # 429 = 限流
            last = f"HTTP {e.code}"
            if e.code != 429:
                raise FuyaoError(f"{name}: {last} {path}") from e
        except TimeoutError as e:
            last = f"timeout {TIMEOUT_S}s"
        except Exception as e:                                   # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.8 * (2 ** attempt))
    raise FuyaoError(f"{name}: 重试 3 次仍失败（{last}）")


def _data(name: str, ttl_hours: int = 24, **params) -> dict | None:
    """带日内缓存的取数；业务错误码返回 None（并记 provenance），不抛。"""
    key = api_key()
    if not key:
        return None
    path = _cache_path(name, params)
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl_hours * 3600:
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
            _prov.append({"kind": name, "key": _sig(params), "source": "cache",
                          "rows": blob.get("rows"), "cached_at": blob.get("fetched_at")})
            return blob.get("data")
        except Exception:  # noqa: BLE001
            pass                                    # 缓存坏 → 重取
    t0 = time.time()
    env = _http(name, **params)
    code = env.get("code")
    if code != 0:
        _prov.append({"kind": name, "key": _sig(params), "source": "fuyao",
                      "code": code, "message": env.get("message"), "rows": 0})
        return None                                 # 业务错：无权限/无数据/参数错
    data = env.get("data")
    rows = _rows_of(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "params": params, "rows": rows, "data": data}, f, ensure_ascii=False)
    _prov.append({"kind": name, "key": _sig(params), "source": "fuyao",
                  "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "secs": round(time.time() - t0, 2), "rows": rows})
    return data


def _sig(params: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(params.items()))


def _rows_of(data) -> int:
    if isinstance(data, dict):
        for k in ("item", "items", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
        return 1
    return len(data) if isinstance(data, list) else 0


def _items(data) -> list:
    if isinstance(data, dict):
        for k in ("item", "items", "list"):
            if isinstance(data.get(k), list):
                return data[k]
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- 契约校验
def _check_thscode(value: str, what: str = "thscode") -> str:
    s = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+\.[A-Z0-9]+", s):
        raise FuyaoError(f"{what} 格式不对：{value!r}（应为 代码.交易所，如 600519.SH）")
    return s


def _check_batch(codes, maximum: int, what: str) -> list[str]:
    lst = list(codes) if not isinstance(codes, str) else [codes]
    if not lst:
        raise FuyaoError(f"{what}：标的列表为空")
    if len(lst) > maximum:
        raise FuyaoError(f"{what}：单次最多 {maximum} 只（收到 {len(lst)}），请分批")
    return lst


def _plus_years(day: str, years: int) -> dt.date:
    d = dt.date.fromisoformat(day)
    try:
        return d.replace(year=d.year + years)
    except ValueError:                       # 2/29 → 2/28
        return d.replace(year=d.year + years, day=28)


def _check_window(start: str, end: str, years: int, what: str) -> None:
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if e < s:
        raise FuyaoError(f"{what}：end 早于 start（{start} → {end}）")
    if e > _plus_years(start, years):
        raise FuyaoError(
            f"{what}：窗口最多 {years} 个自然年（{start} → {end}），请缩短或分批")


def _slice_windows(start: str, end: str, years: int) -> list[tuple[str, str]]:
    """把 [start, end] 切成 ≤years 自然年的若干段（官方 SDK 对 >10 年日 K 也这样切）。"""
    out: list[tuple[str, str]] = []
    cur = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while cur <= last:
        edge = _plus_years(cur.isoformat(), years) - dt.timedelta(days=1)
        stop = min(edge, last)
        out.append((cur.isoformat(), stop.isoformat()))
        cur = stop + dt.timedelta(days=1)
    return out


def _check_day(day: str) -> str:
    try:
        dt.date.fromisoformat(day)
    except ValueError as e:
        raise FuyaoError(f"日期格式应为 YYYY-MM-DD，收到 {day!r}") from e
    return day


def dump_provenance(name: str = "fuyao_provenance") -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    path = os.path.join(config.CACHE_DIR, f"{name}.json")
    agg: dict = {}
    for p in _prov:
        agg.setdefault(f'{p["kind"]}::{p["key"]}', []).append(p)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "base_url": BASE_URL, "count": len(_prov), "items": agg},
                  f, ensure_ascii=False, indent=1)
    return path


# ---------------------------------------------------------------- 代码格式
def thscode(code) -> str:
    """'sh510300' / '510300.SH' / 600519 / 159845 → 带后缀 thscode（如 ``159845.SZ``）。

    2026-11-16 修复（同 ``datasource.sina_symbol`` 那类 bug）：原实现按 A 股首位规则
    拼后缀（6/9→.SH、0/3→.SZ、4/8→.BJ、其余一律 .SH），深市 ETF（15/16 开头的
    159845/159915/160632...）和 5 开头的沪市 ETF 判定依赖首位巧合——159xxx 被拼成
    ``159845.SH``，同花顺 ``fund/market/historical`` 直接报
    ``3001 Fund not found: 159845.SH``，于是 ``fuyao.etf_hist()`` 对全部深市 ETF 返回 None。
    现统一走 :func:`stock.codes.to_thscode`（6/9→SH、0/2/3→SZ、15/16/18→SZ、5→SH、
    4/8→BJ），股票/指数口径不变。
    """
    from ..codes import to_thscode

    return to_thscode(code)


def of_code(code) -> str:
    s = str(code).strip()
    return s if s.upper().endswith(".OF") else s.zfill(6) + ".OF"


def _ms(day: str) -> int:
    """'2024-06-01' → Asia/Shanghai 当日零点毫秒戳。"""
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return int(d.timestamp() * 1000)


def _day(ms_value) -> str | None:
    if not ms_value:
        return None
    return dt.datetime.fromtimestamp(ms_value / 1000).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 高层取数
def snapshot(codes) -> list[dict]:
    """行情快照（批量，≤100 只/次）。"""
    cs = _check_batch([thscode(c) for c in codes], MAX_SNAPSHOT_CODES, "snapshot")
    return _items(_data("snapshot", thscodes=",".join(cs)))


def historical(code, start: str, end: str, adjust: str = "forward") -> list[dict]:
    """单只标的日 K；单段窗口 ≤10 自然年，超长**自动切片拼接**（官方 SDK 同策略）。"""
    if adjust not in ADJUSTS:
        raise FuyaoError(f"adjust 只能是 {ADJUSTS}，收到 {adjust!r}")
    _check_day(start)
    _check_day(end)
    out: list[dict] = []
    for s, e in _slice_windows(start, end, MAX_KLINE_YEARS):
        data = _data("historical", thscode=thscode(code), interval="1d",
                     start=_ms(s), end=_ms(e), adjust=adjust)
        out.extend(_items(data))
    return out


def financials(code, statement: str = "income", period: str = "annual",
               limit: int | None = None, start: str | None = None,
               end: str | None = None) -> list[dict]:
    """三表多期序列。`period='annual'` 只给年报；`'quarterly'` 给季报。

    `limit` 与 `start/end` **互斥**（服务端 code=1004）；窗口 ≤10 年（code=1003）。
    每条含 `period_end_ms`（报告期末）与 `report_date_ms`（⚠️ 非真实披露日，见模块头）。
    """
    name = STATEMENTS.get(statement, statement)
    if name not in ("income", "balance", "cashflow"):
        raise ValueError(f"未知报表 {statement}")
    if period not in PERIODS:
        raise FuyaoError(f"period 只能是 {PERIODS}，收到 {period!r}")
    params = {"thscode": thscode(code), "period": period}
    if start and end:
        _check_window(start, end, MAX_FINANCIAL_YEARS, "三表")
        params["start"], params["end"] = _ms(start), _ms(end)
    else:
        params["limit"] = int(limit or 4)
    data = _data(name, **params)
    return _items(data)


def indicators(code, report: str) -> dict[str, dict[str, str | None]]:
    """五类财务指标。`report` 形如 '2025-4'（4=年报，1/2/3=季报）。"""
    data = _data("indicators", thscode=thscode(code), report=str(report))
    out: dict[str, dict[str, str | None]] = {}
    if isinstance(data, dict):
        for blk in data.get("abilities") or []:
            out[blk.get("ability") or ""] = {
                it.get("index_id"): it.get("value") for it in (blk.get("indicators") or [])}
    return out


def valuations(codes) -> dict[str, dict]:
    """批量最新估值快照（≤100 只/次，固定 5 个指标，**无历史序列**）。"""
    cs = _check_batch([thscode(c) for c in codes], MAX_VALUATION_CODES, "valuations")
    data = _data("valuations", thscodes=",".join(cs))
    return {it.get("ticker"): it for it in _items(data)}


def _first_item(data) -> dict:
    items = _items(data)
    return items[0] if items else {}


def fund_returns(code) -> dict:
    """场外基金区间收益（**累计百分比**，非年化）+ 同类平均/排名。

    字段名全部带 `return_` 前缀：return_year 近一年、return_tyear 近三年、
    return_fyear 近五年、return_nowyear 今年以来、return_now 成立以来；
    同类平均 peer_average_*，排名 rank_* / rank_total_*。
    """
    return _first_item(_data("fund_returns", thscode=of_code(code)))


NAV_RANGES = ("week", "month", "tmonth", "hyear", "year",
              "twoyear", "tyear", "fyear")


def fund_nav(code, rng: str | None = None, nav_type: str = "unit,adj") -> list[dict]:
    """场外基金净值序列（nav_date 毫秒 / unit_nav / adj_nav）。

    注意：参数是 `range` 而**不是** limit。不传 range 时只返回最新一条，
    传 "year" 得近一年、"tyear" 得近三年（按 nav_date 升序）。
    """
    params = {"thscode": of_code(code), "nav_type": nav_type}
    if rng:
        if rng not in NAV_RANGES:
            raise FuyaoError(f"未知 range：{rng}（可选 {', '.join(NAV_RANGES)}）")
        params["range"] = rng
    return _items(_data("fund_nav", **params))


def fund_drawdowns(code) -> dict:
    """场外基金区间最大回撤（**负的百分数原值**：week/month/tmonth/hyear/year/
    twoyear/tyear/fyear/nowyear/now）。"""
    return _first_item(_data("fund_drawdowns", thscode=of_code(code)))


def fund_indicators_hist(code, start, end) -> list[dict]:
    """基金日度指标：rsi_pct（净值波动）/ donchian_channel（趋势强弱）/
    track_index_pe_ttm_five_year_percentile（跟踪指数 PE 五年分位）。窗口 ≤5 自然年。"""
    _check_window(start, end, MAX_FUND_HIST_YEARS, "基金日度指标")
    return _items(_data("fund_indicators_hist", thscode=of_code(code),
                        start=_ms(start), end=_ms(end)))


def fund_profile(code) -> dict:
    data = _data("fund_profile", thscode=of_code(code))
    items = _items(data)
    return items[0] if items else {}


def etf_hist(sym, start: str | None = None, end: str | None = None):
    """场内 ETF 前复权日线（DataFrame：date/close/open/high/low/volume）。

    与 `datasource.fetch_etf_hist()` 同构，可直接互换；无 key 返回 None。
    """
    import pandas as pd

    end = end or dt.date.today().isoformat()
    start = start or (dt.date.fromisoformat(end) - dt.timedelta(days=365 * 4)).isoformat()
    _check_window(start, end, MAX_FUND_HIST_YEARS, "ETF 历史")
    data = _data("fund_hist", thscode=thscode(sym), start=_ms(start), end=_ms(end))
    items = _items(data)
    if not items:
        return None
    df = pd.DataFrame([{
        "date": _day(it.get("date_ms")),
        "open": it.get("open_price"), "high": it.get("high_price"),
        "low": it.get("low_price"), "close": it.get("close_price"),
        "volume": it.get("volume"), "amount": it.get("turnover"),
    } for it in items])
    return df.sort_values("date").reset_index(drop=True)


def trade_days(start: str, end: str) -> list[str]:
    data = _data("calendar", start=_ms(start), end=_ms(end))
    return [it.get("date") for it in _items(data)]


# ---------------------------------------------------------------- PIT（受限）
def annual_rows(code, n: int = 5, statement: str = "income") -> list[dict]:
    """近 n 期年报，附 `period_end`/`report_date` 两个可读日期。"""
    rows = financials(code, statement=statement, period="annual", limit=max(1, min(n, 20)))
    for r in rows:
        r["period_end"] = _day(r.get("period_end_ms"))
        r["report_date"] = _day(r.get("report_date_ms"))
    return rows


def asof_rows(code, asof: str, n: int = 5, statement: str = "income") -> list[dict]:
    """按 `report_date <= asof` 过滤的「保守 as-of」视图。

    ⚠️ 因为 report_date_ms 系统性滞后约 1 年（见模块头），这个视图**偏保守**：
    它只会少给你信息，不会多给。可用于「不偷看未来」的复核，**不能**当作真实披露时点。
    """
    rows = annual_rows(code, n=max(n, 8), statement=statement)
    keep = [r for r in rows if (r.get("report_date") or "9999") <= asof]
    return keep[:n]


# ---------------------------------------------------------------- 资讯域（iNews）
def stock_news(code, size: int = 20, pages: int = 1, ttl_hours: float = 2) -> list[dict]:
    """个股分时新闻。offset 首屏传 'first'，后续原样回传。

    归一化返回：`published`/`title`/`summary`/`source`/`url`。
    `jump_url` 不保证能查详情，但可以给人点开。
    """
    out: list[dict] = []
    offset = "first"
    for _ in range(max(1, pages)):
        data = _data("news_stock", ttl_hours=ttl_hours, thscode=thscode(code),
                     offset=offset, size=max(1, min(int(size), 100)))
        items = _items(data)
        if not items:
            break
        for it in items:
            out.append({
                "published": _day(it.get("publish_time_ms")),
                "title": (it.get("title") or "").strip(),
                "summary": (it.get("summary") or "").strip(),
                "source": it.get("source") or "",
                "url": it.get("jump_url") or "",
            })
        data = data or {}
        if not data.get("has_more"):
            break
        offset = data.get("offset") or ""
        if not offset:
            break
    return out


def flash(tag_id: int = 21103, pages: int = 1, ttl_hours: float = 2) -> list[dict]:
    """7×24 快讯流。首屏 seq 传字符串 '0'，后续回传上一屏末条 seq。"""
    out: list[dict] = []
    seq = "0"
    for _ in range(max(1, pages)):
        data = _data("news_flash", ttl_hours=ttl_hours, tag_id=int(tag_id), seq=seq)
        items = _items(data)
        if not items:
            break
        for it in items:
            out.append({"seq": str(it.get("seq") or ""),
                        "title": (it.get("title") or "").strip(),
                        "summary": (it.get("summary") or "").strip(),
                        "url": it.get("encoded_url") or ""})
        seq = out[-1]["seq"] if out else ""
        if not seq:
            break
    return out


def anomaly_stock(codes) -> list[dict]:
    """当日个股异动原因（批量，去重前 ≤50 个）。当日无异动的代码会被忽略。"""
    lst = [thscode(c) for c in (codes if isinstance(codes, (list, tuple, set)) else [codes])]
    if not lst:
        return []
    lst = _check_batch(lst, MAX_ANOMALY_CODES, "anomaly_stock")
    data = _data("anomaly_stock", ttl_hours=2, thscodes=",".join(lst))
    return _items(data)


# ---------------------------------------------------------------- 指数 / 板块
def index_members(index_code="000300.SH") -> list[dict]:
    """指数**当前**成分股（官方 REST 可用：`thscode=000300.SH` → 300 只）。

    支持标准指数（000300.SH / 399300.SZ）与同花顺板块指数（886042.TI）。
    ⚠️ 只有**当前**成分，不是历史成分；回测请走 `pit.py`（baostock 的历史成分）。
    """
    data = _data("index_members", ttl_hours=24 * 7,
                 thscode=_check_thscode(thscode(index_code)))
    return [{"code": it.get("ticker"), "name": it.get("name"),
             "thscode": it.get("thscode")} for it in _items(data)]


def index_snapshot(index_code) -> dict:
    """指数行情快照（单只）：最新点位/涨跌幅/成交额。"""
    data = _data("index_snapshot", ttl_hours=1,
                 thscode=_check_thscode(thscode(index_code)))
    return _first_item(data)


def index_hist(index_code, start: str | None = None, end: str | None = None) -> list[dict]:
    """指数日 K；官方**只覆盖近 5 个自然年**（更早的窗口直接拦下，不浪费往返）。"""
    end = end or dt.date.today().isoformat()
    start = start or (dt.date.fromisoformat(end).replace(
        year=dt.date.fromisoformat(end).year - MAX_INDEX_YEARS)).isoformat()
    floors = _plus_years(dt.date.today().isoformat(), -MAX_INDEX_YEARS)
    if dt.date.fromisoformat(start) < floors:
        raise FuyaoError(f"指数历史只覆盖近 {MAX_INDEX_YEARS} 个自然年（最早 {floors}）")
    _check_window(start, end, MAX_INDEX_YEARS, "指数历史")
    data = _data("index_hist", thscode=_check_thscode(thscode(index_code)),
                 start=_ms(start), end=_ms(end), interval="1d")
    return _items(data)


def stock_industries(codes, kind: str | None = None) -> dict[str, list[dict]]:
    """个股 → 概念/行业/地域/特色板块（批量 ≤100）。

    kind ∈ {cn_concept, industry, region, tzs}；None = 四类全要。
    """
    lst = [thscode(c) for c in (codes if isinstance(codes, (list, tuple, set)) else [codes])]
    if not lst:
        return {}
    lst = _check_batch(lst, MAX_INDUSTRY_CODES, "stock_industries")
    data = _data("stock_industries", ttl_hours=24 * 7,
                 thscode=",".join(lst), type=(kind or None))
    out: dict[str, list[dict]] = {}
    for it in _items(data):
        out[it.get("thscode") or ""] = [
            {"thscode": x.get("thscode"), "name": x.get("name"), "type": x.get("type")}
            for x in (it.get("index_list") or [])]
    return out


# ---------------------------------------------------------------- 除复权 / 全量导出
def adjust_factors(code, start: str | None = None, end: str | None = None) -> list[dict]:
    """复权因子事件流（分红/送股/配股），附 `ex_date` 可读日期。"""
    params = {"thscode": thscode(code)}
    if start:
        params["from"] = start
    if end:
        params["to"] = end
    data = _data("adjust_factors", ttl_hours=24 * 7, **params)
    rows = _items(data)
    for r in rows:
        r["ex_date"] = _day(r.get("ex_date_ms"))
    return rows


DUMP_KINDS = {"daily-k": "全市场 10 年日 K（parquet）",
              "daily-k-10d": "最近 10 交易日日 K",
              "adjustment-factors": "全量复权因子事件"}


def _pool(name: str, page: int = 1, size: int = 100, ttl_hours: float = 1.0) -> list[dict]:
    """涨跌停/炸板池：返回当前交易日的清单（00:00 跑时=上一交易日收盘数据）。"""
    data = _data(name, ttl_hours=ttl_hours, page=page, size=size)
    return _items(data)


def limit_up_pool(page: int = 1, size: int = 100, ttl_hours: float = 1.0) -> list[dict]:
    return _pool("limit_up_pool", page, size, ttl_hours)


def limit_down_pool(page: int = 1, size: int = 100, ttl_hours: float = 1.0) -> list[dict]:
    return _pool("limit_down_pool", page, size, ttl_hours)


def limit_break_pool(page: int = 1, size: int = 100, ttl_hours: float = 1.0) -> list[dict]:
    return _pool("limit_break_pool", page, size, ttl_hours)


def dragon_tiger(board: str = "all", ttl_hours: float = 6.0) -> dict:
    """龙虎榜：{trade_date, count, stock_items:[{thscode,ticker,name,change,net_value,net_rate,...}]}"""
    data = _data("dragon_tiger", ttl_hours=ttl_hours, board_type=board)
    return data or {}


def hot_stock_list(ttl_hours: float = 1.0) -> list[dict]:
    return _items(_data("hot_stock_list", ttl_hours=ttl_hours))


def anomaly_monitor(ttl_hours: float = 2.0) -> list[dict]:
    return _items(_data("anomaly_monitor", ttl_hours=ttl_hours))


def dump_url(kind: str = "daily-k", ttl_hours: float = 0.05) -> dict:
    """全量导出预签名下载地址（约 5 分钟有效，所以缓存很短）。

    `daily-k` 是**全市场 10 年日 K 的一个 parquet**：一次请求顶几千次单只日线。
    """
    if kind not in DUMP_KINDS:
        raise FuyaoError("未知导出 kind: %s（可选 %s）" % (kind, list(DUMP_KINDS)))
    name = "dump_" + kind.replace("-", "_")
    data = _data(name, ttl_hours=ttl_hours)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 自检
def main() -> int:
    ok = available()
    print(f"[fuyao] base={BASE_URL} key={'OK' if ok else 'MISSING'}"
          f" source={key_source()}")
    print(f"[fuyao] 契约来源：{OFFICIAL_REPO} 的 docs/api（官方 MIT；本模块零依赖封装）")
    if not ok:
        print("[fuyao] 无 key：所有取数返回 None/空，主链路走取数接口层（这是预期行为）")
        return 0
    for label, fn in (("snapshot 600519", lambda: snapshot(["600519"])),
                      ("income 600519×3", lambda: annual_rows("600519", 3)),
                      ("valuations×2", lambda: valuations(["600519", "000999"])),
                      ("fund_returns 018074", lambda: fund_returns("018074")),
                      ("stock_news 600519", lambda: stock_news("600519", size=5)),
                      ("flash A股", lambda: flash(21103, 1)),
                      ("anomaly_stock", lambda: anomaly_stock(["600519", "000999"])),
                      ("index_members 沪深300（期望≈300）", lambda: index_members("000300.SH")),
                      ("index_snapshot 沪深300", lambda: [index_snapshot("000300.SH")]),
                      ("index_hist 沪深300（近1年）", lambda: index_hist("000300.SH", start="2025-11-14")),
                      ("stock_industries", lambda: stock_industries(["600519"], "industry")),
                      ("adjust_factors 600519", lambda: adjust_factors("600519")),
                      ("dump_url daily-k", lambda: [dump_url("daily-k")])):
        try:
            got = fn()
            n = len(got) if isinstance(got, (list, dict)) else 0
            print(f"  [ok] {label}: {n} 项")
        except FuyaoError as e:
            print(f"  [FAIL] {label}: {e}")
    print(f"[fuyao] provenance -> {dump_provenance()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
