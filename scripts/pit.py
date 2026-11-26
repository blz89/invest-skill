# -*- coding: utf-8 -*-
r"""point-in-time 财务快照 + 历史成分股（baostock，免费、无需 key）。

为什么需要它：
个股层**回测的关键不在行情，而在财务数据有没有「披露日」**——只有报告期就拿不到当时真正看得到的信息。
用报告期做 as-of 过滤 = 偷看未来（2024 年报在 2024-12-31 就知道了，实际要等 2025-04 才披露）。
同花顺 API 补不上（它的 report_date_ms 是假的，系统性滞后约 1 年）。

baostock 的季频报表带 **pubDate（真实披露日）**——实测：
    茅台 2024 年报 statDate=2024-12-31  pubDate=2025-04-03   （与实际披露日一致）
所以「这一天能不能看到这份财报」从今天起是可查询的事实，不是假设。
另外 `query_hs300_stocks(date)` / `query_zz500_stocks(date)` 支持按历史日期取成分，
这直接修掉了「幸存者偏差」（我们原来只有当期成分）。

本模块只做四件事：
1. `financials(code, year, quarter)`  单期六张指标表合并（都带 pubDate）
2. `asof(code, asof)`                 as-of 时点可见的最新 N 期（按 pubDate 过滤）
3. `eps_ttm(code, asof)` + `pe_ttm`    当时可见的 TTM 每股收益 → 算 TTM 市盈率
4. `index_members(index, date)` / `trade_days(start, end)`  历史成分、交易日历

依赖：baostock（已装在 D:\skills\invest\stock\.venv）。未安装时 `available()` 为 False，所有函数返回
None/空表并抛 PitError —— **不影响既有链路**。

用法：
    python pit.py --code 600519 --asof 2026-10-28
    python pit.py --members hs300 --date 2019-06-28
    python pit.py --pe 600519 --price 1450
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
from datetime import datetime

import config
import datasource as ds

CACHE_DIR = os.path.join(config.CACHE_DIR, "pit")
PROV_FILE = os.path.join(config.CACHE_DIR, "pit_provenance.json")
_TTL = int(os.environ.get("PIT_TTL_HOURS", 24 * 7))
_PROV: list[dict] = []
_LOGIN = {"ok": False, "tried": False}

# 六张指标表：每张的数值字段（其余 code/statDate/pubDate 是公共字段）
TABLE_FIELDS = {
    "profit": ["roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM",
               "MBRevenue", "totalShare", "liqaShare"],
    "balance": ["currentRatio", "quickRatio", "cashRatio", "YOYLiability",
                "liabilityToAsset", "assetToEquity"],
    "cashflow": ["CAToAsset", "NCAToAsset", "tangibleAssetToAsset",
                 "ebitToInterest", "CFOToNP", "CFOToGr"],
    "dupont": ["dupontROE", "dupontAssetStoEquity", "dupontAssetTurn",
               "dupontPnitoni", "dupontNitogr", "dupontTaxBurden",
               "dupontIntburden", "dupontEbittogr"],
    "growth": ["YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"],
    "operation": ["NRTurnRatio", "NRTurnDays", "INVTunRatio", "INVTunDays",
                  "CATurnRatio", "CATurnDays", "AssetTurnRatio"],
}
# 这些字段在 baostock 里是「小数比率」，而我们项目内部用百分数
PCT_FIELDS = {"roeAvg", "npMargin", "gpMargin", "YOYLiability", "liabilityToAsset",
              "dupontROE", "YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"}


class PitError(RuntimeError):
    """baostock 取数失败。故意不静默返回空——空表会被误读成"没有数据"。"""


# ------------------------------------------------------------------ 基础设施
def _bs():
    """惰性 import + 单次 login。baostock 是进程级会话，不能并发。"""
    try:
        import baostock as bs
    except ImportError as e:            # noqa: BLE001
        raise PitError("未安装 baostock（pip install baostock）") from e
    if not _LOGIN["tried"]:
        _LOGIN["tried"] = True
        r = bs.login()
        if str(r.error_code) != "0":
            raise PitError(f"baostock 登录失败：{r.error_code} {r.error_msg}")
        _LOGIN["ok"] = True

        @atexit.register
        def _bye():
            try:
                bs.logout()
            except Exception:           # noqa: BLE001
                pass

    if not _LOGIN["ok"]:
        raise PitError("baostock 未登录")
    return bs


def available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("baostock") is not None
    except Exception:                   # noqa: BLE001
        return False


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _rows(rs) -> list[dict]:
    if str(rs.error_code) != "0":
        raise PitError(f"baostock error {rs.error_code} {rs.error_msg}")
    out = []
    while rs.next():
        out.append(dict(zip(rs.fields, rs.get_row_data())))
    return out


def _num(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cached(path: str, loader, ttl_hours: int | None = None):
    ttl = _TTL if ttl_hours is None else ttl_hours
    if ttl > 0 and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= ttl:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:           # noqa: BLE001
                pass
    data = loader()
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    return data


def _prov_add(kind: str, key: str, rows: int, note: str = ""):
    _PROV.append({"kind": kind, "key": key, "rows": int(rows), "note": note,
                  "source": "baostock",
                  "fetched_at": datetime.now().isoformat(timespec="seconds")})


def dump_provenance() -> dict:
    _ensure_dir()
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": "baostock", "items": _PROV}
    with open(PROV_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


# ------------------------------------------------------------------ 代码转换
def bs_code(code: str) -> str:
    """600519 / sh600519 / 600519.SH → sh.600519"""
    s = str(code).strip().lower().replace(".sh", "").replace(".sz", "").replace(".bj", "")
    s = s.replace("sh", "").replace("sz", "").replace("bj", "")
    c = "".join(ch for ch in s if ch.isdigit())
    if len(c) != 6:
        raise PitError(f"无法识别的代码：{code!r}")
    if c[0] in "65":
        return f"sh.{c}"
    if c[0] in "03":
        return f"sz.{c}"
    if c[0] in "48":
        return f"bj.{c}"
    raise PitError(f"无法判断市场：{c}")


def _plain(code: str) -> str:
    return bs_code(code).split(".", 1)[1]


# ------------------------------------------------------------------ 单期财务
def financials(code: str, year: int, quarter: int) -> dict | None:
    """一期六表合并。没有这一期就返回 None（不是空 dict）。"""
    bc = bs_code(code)
    path = os.path.join(CACHE_DIR, f"fin_{bc}_{year}Q{quarter}.json")

    def load():
        bs = _bs()
        merged: dict = {"code": bc}
        got = 0
        for table, fields in TABLE_FIELDS.items():
            fn = getattr(bs, "query_" + {"profit": "profit_data",
                                         "balance": "balance_data",
                                         "cashflow": "cash_flow_data",
                                         "dupont": "dupont_data",
                                         "growth": "growth_data",
                                         "operation": "operation_data"}[table])
            try:
                rs = fn(code=bc, year=year, quarter=quarter)
                rows = _rows(rs)
            except PitError:
                raise
            except Exception:           # noqa: BLE001 - 单表失败不影响其余
                rows = []
            if not rows:
                continue
            r = rows[0]
            got += 1
            for k in ("statDate", "pubDate"):
                if r.get(k):
                    merged[k] = r[k]
            for k in fields:
                v = _num(r.get(k))
                if v is None:
                    continue
                merged[k] = v
                if k in PCT_FIELDS:
                    merged[k + "_pct"] = round(v * 100.0, 4)
            time.sleep(0.05)
        return merged if got else None

    return _cached(path, load, ttl_hours=24 * 30)   # 已披露的报表不会变，缓存久一点


# ------------------------------------------------------------------ as-of 查询
def periods(asof: str, n: int = 8) -> list[tuple[int, int]]:
    """从 asof 往前枚举 (year, quarter)，最多 n 期。"""
    d = datetime.strptime(asof, "%Y-%m-%d")
    out = []
    y, q = d.year, (d.month - 1) // 3 + 1
    while len(out) < n and y >= 2000:
        # 报告期必须在 asof 之前（至少隔一个季度，pubDate 再过滤一次）
        if (y, q) <= (d.year, (d.month - 1) // 3):
            out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def asof(code: str, asof_date: str, n: int = 8) -> list[dict]:
    """按 pubDate ≤ asof_date 过滤后，最近的 n 期（新的在前）。

    这就是回测里「这一天能看到什么」的答案。pubDate 缺失的期**整期丢弃**
    （宁少不多：看不到就是看不到）。
    """
    rows = []
    for (y, q) in periods(asof_date, n * 2 + 2):
        d = financials(code, y, q)
        if not d or not d.get("pubDate"):
            continue
        if d["pubDate"] <= asof_date:
            rows.append({"year": y, "quarter": q, **d})
        if len(rows) >= n:
            break
    return rows


def latest_asof(code: str, asof_date: str) -> dict | None:
    r = asof(code, asof_date, n=1)
    return r[0] if r else None


def eps_ttm(code: str, asof_date: str) -> float | None:
    """as-of 时点可见的 TTM 每股收益。"""
    r = latest_asof(code, asof_date)
    if not r:
        return None
    v = r.get("epsTTM")
    return v if v is not None else None


def pe_ttm(code: str, price: float, asof_date: str) -> float | None:
    """用 as-of 可见的 epsTTM 算 TTM 市盈率（补上「我们的 PE 是静态」这个缺口）。"""
    e = eps_ttm(code, asof_date)
    if e is None or e <= 0 or price is None:
        return None
    return round(float(price) / e, 4)


# ------------------------------------------------------------------ 成分股/日历
_MEMBER_FN = {"hs300": "query_hs300_stocks", "zz500": "query_zz500_stocks"}


def index_members(index: str, date: str) -> list[str]:
    """按历史日期取指数成分（修幸存者偏差）。代码统一成 6 位数字。"""
    key = str(index).lower()
    if key not in _MEMBER_FN:
        raise PitError(f"不支持的指数：{index}（可用 hs300 / zz500）")
    path = os.path.join(CACHE_DIR, f"members_{key}_{date}.json")

    def load():
        bs = _bs()
        rows = _rows(getattr(bs, _MEMBER_FN[key])(date=date))
        return [str(r.get("code", "")).split(".")[-1] for r in rows if r.get("code")]

    codes = _cached(path, load, ttl_hours=24 * 7)
    _prov_add("index_members", f"{key}:{date}", len(codes))
    return codes


def stock_basic(force: bool = False):
    """A 股/指数基础信息（含上市日、退市日）。"""
    return ds.fetch_stock_basic(force=force)


def delisted(force: bool = False):
    """历史退市股票名单（代码/名称/上市日/退市日，按退市日倒序）。"""
    return ds.fetch_delisted(force=force)


def listed_asof(date: str):
    """某日仍在市的股票清单（PIT 口径）。

    判定：上市日 ≤ 该日，且（退市日为空 或 退市日 > 该日）。
    回测里拿它和当日指数成分取交集，既不用「未来才上市的票」，
    也不把「后来退市的票」整只删掉——这是幸存者偏差的两头。
    """
    b = ds.fetch_stock_basic()
    d = str(date)[:10]
    stk = b[b["类型"] == "股票"]
    ipo = stk["上市日"].astype(str).str.strip()
    out = stk["退市日"].astype(str).str.strip()
    keep = (ipo != "") & (ipo <= d) & ((out == "") | (out > d))
    return sorted(stk.loc[keep, "代码"].astype(str).tolist())


def st_intervals(code: str):
    """某只股票的风险警示（ST/*ST）区间：DataFrame(起始日, 结束日)。"""
    return ds.fetch_st_intervals(code)


def trade_days(start: str, end: str) -> list[str]:
    path = os.path.join(CACHE_DIR, f"td_{start}_{end}.json")

    def load():
        bs = _bs()
        rows = _rows(bs.query_trade_dates(start_date=start, end_date=end))
        return [r["calendar_date"] for r in rows if str(r.get("is_trading_day")) == "1"]

    return _cached(path, load, ttl_hours=24 * 30)


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="point-in-time 财务 / 历史成分股（baostock）")
    ap.add_argument("--code")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--members", choices=["hs300", "zz500"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--pe", dest="pe_code", default=None, help="算 TTM PE 的股票代码")
    ap.add_argument("--price", type=float, default=None)
    ap.add_argument("--basic", action="store_true", help="列 A 股基础信息（含上市/退市日）")
    ap.add_argument("--delisted", action="store_true", help="列历史退市名单（含退市日）")
    ap.add_argument("--listed-asof", default=None, help="某日仍在市的股票清单（PIT 口径）")
    ap.add_argument("--st", dest="st_code", default=None, help="列某只股票的 ST/*ST 区间")
    ap.add_argument("--out", default=None, help="把清单写到 CSV")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    asof_d = a.asof or datetime.now().strftime("%Y-%m-%d")

    if not available():
        print("baostock 未安装 → PIT 能力不可用（其余链路不受影响）")
        return 2

    if a.basic or a.delisted:
        df = delisted() if a.delisted else stock_basic()
        title = "历史退市名单" if a.delisted else "A 股基础信息"
        print(f"{title}：{len(df)} 行")
        if len(df):
            print(df.head(10).to_string(index=False))
        if a.out:
            df.to_csv(a.out, index=False, encoding="utf-8-sig")
            print(f"已写 {a.out}")
        dump_provenance()
        return 0

    if a.listed_asof:
        codes = listed_asof(a.listed_asof)
        print(f"{a.listed_asof} 仍在市的股票：{len(codes)} 只")
        if a.json:
            print(json.dumps(codes, ensure_ascii=False))
        else:
            print("  前 10 只：" + ", ".join(codes[:10]))
        if a.out:
            io_import = __import__("pandas")
            io_import.DataFrame({"代码": codes}).to_csv(a.out, index=False, encoding="utf-8-sig")
            print(f"已写 {a.out}")
        dump_provenance()
        return 0

    if a.st_code:
        df = st_intervals(a.st_code)
        print(f"{_plain(a.st_code)} ST/*ST 区间：{len(df)} 段")
        if len(df):
            print(df.to_string(index=False))
        dump_provenance()
        return 0

    if a.members:
        d = a.date or asof_d
        codes = index_members(a.members, d)
        print(f"{a.members} 在 {d} 的成分：{len(codes)} 只")
        if a.json:
            print(json.dumps(codes, ensure_ascii=False))
        else:
            print("  前 10 只：" + ", ".join(codes[:10]))
        dump_provenance()
        return 0

    if a.pe_code:
        e = eps_ttm(a.pe_code, asof_d)
        p = pe_ttm(a.pe_code, a.price, asof_d) if a.price is not None else None
        print(f"{_plain(a.pe_code)} as-of {asof_d}: epsTTM={e}  price={a.price}  PE_TTM={p}")
        if e is None:
            print("  （该时点还没有可见的 epsTTM → 不要用当期 PE 冒充）")
        dump_provenance()
        return 0

    if not a.code:
        ap.error("需要 --code / --members / --pe")
    rows = asof(a.code, asof_d, a.n)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(f"{_plain(a.code)} 在 {asof_d} 可见的最近 {len(rows)} 期（按披露日过滤）")
        for r in rows:
            print(f"  {r.get('statDate')}  披露 {r.get('pubDate')}  "
                  f"ROE={r.get('roeAvg_pct')}%  净利率={r.get('npMargin_pct')}%  "
                  f"资产负债率={r.get('liabilityToAsset_pct')}%  epsTTM={r.get('epsTTM')}")
    dump_provenance()
    return 0


if __name__ == "__main__":
    sys.exit(main())
