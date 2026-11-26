# -*- coding: utf-8 -*-
"""纯计算层：把原始数据变成可判定的指标。

这里不取数、不打印、不做判断，只做数学。好处是可单独测试：
同样的输入永远得到同样的输出，方便日后回测与复核。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- 安全取值
def f(x, default=None):
    """转 float；NaN/None/空串 → default。"""
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def col(rec, name: str, default=None):
    """从一行里按列名取值，缺列不报错。"""
    if name not in rec.index:
        return default
    return f(rec.get(name), default)


# ---------------------------------------------------------------- 财务切片
def annual_rows(fin: pd.DataFrame) -> pd.DataFrame:
    """只保留年报（12-31），按日期升序。"""
    if fin is None or len(fin) == 0:
        return pd.DataFrame()
    df = fin.copy()
    df["日期"] = pd.to_datetime(df["日期"])
    df = df[df["日期"].dt.month == 12]
    return df.sort_values("日期").reset_index(drop=True)


def latest_row(fin: pd.DataFrame) -> pd.Series | None:
    if fin is None or len(fin) == 0:
        return None
    df = fin.copy()
    df["日期"] = pd.to_datetime(df["日期"])
    return df.sort_values("日期").iloc[-1]


def gross_margin(rec) -> float | None:
    """毛利率。新浪的『销售毛利率』常为空，用 100 - 主营业务成本率 反推。"""
    gm = col(rec, "销售毛利率(%)")
    if gm is not None and 0 < gm < 100:
        return gm
    cost = col(rec, "主营业务成本率(%)")
    if cost is not None and 0 <= cost < 100:
        return 100.0 - cost
    return None


def roe(rec) -> float | None:
    """净资产收益率（累计口径；年报行即全年）。"""
    return col(rec, "净资产收益率(%)")


def debt_ratio(rec) -> float | None:
    return col(rec, "资产负债率(%)")


def ocf_to_ni(rec) -> float | None:
    """经营现金净流量 / 净利润。

    ⚠️ 字段陷阱：新浪这列叫「经营现金净流量与净利润的比率(%)」，
    但实测是**纯倍数**不是百分数。证据：茅台 2021 年报该列 = 1.1491，
    而 640.29亿 / 557.21亿 = 1.149 —— 完全吻合。故不除以 100。
    """
    return col(rec, "经营现金净流量与净利润的比率(%)")


def rev_growth(rec) -> float | None:
    """主营业务收入同比增长率（小数）。"""
    v = col(rec, "主营业务收入增长率(%)")
    return None if v is None else v / 100.0


def profit_growth(rec) -> float | None:
    v = col(rec, "净利润增长率(%)")
    return None if v is None else v / 100.0


def chain_cagr(growths: list[float | None]) -> float | None:
    """由连续几期同比增长率反推复合年化增速。

    (1+g1)(1+g2)...(1+gn) = 累计倍数 → CAGR = 累计倍数^(1/n) - 1
    这是从『增长率』列还原 CAGR 的标准做法，避免依赖绝对营收数据。
    """
    gs = [g for g in growths if g is not None]
    if len(gs) < 2:
        return None
    mult = 1.0
    for g in gs:
        if g <= -1:
            return None
        mult *= (1.0 + g)
    if mult <= 0:
        return None
    return mult ** (1.0 / len(gs)) - 1.0


# ---------------------------------------------------------------- 价格序列
def pct_rank(series: pd.Series, x: float) -> float | None:
    """x 在历史序列中的分位（0=最低，1=最高）。只取正值样本。"""
    if series is None or x is None:
        return None
    s = pd.to_numeric(series, errors="coerce").dropna()
    s = s[s > 0]
    if len(s) < 60 or x <= 0:
        return None
    return float((s <= x).mean())


def max_drawdown(close: pd.Series) -> float:
    """区间最大回撤（负数）。"""
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 20:
        return float("nan")
    peak = c.cummax()
    return float((c / peak - 1.0).min())


def total_return(close: pd.Series) -> float:
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 2:
        return float("nan")
    return float(c.iloc[-1] / c.iloc[0] - 1.0)


def annualized_return(close: pd.Series) -> float:
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 60:
        return float("nan")
    yrs = len(c) / 244.0
    if yrs <= 0:
        return float("nan")
    return float((c.iloc[-1] / c.iloc[0]) ** (1.0 / yrs) - 1.0)


def volatility(close: pd.Series, periods: int = 244) -> float:
    r = pd.to_numeric(close, errors="coerce").dropna().pct_change().dropna()
    if len(r) < 60:
        return float("nan")
    return float(r.tail(periods).std() * math.sqrt(244))


def sharpe(close: pd.Series, rf: float = 0.02) -> float:
    a = annualized_return(close)
    v = volatility(close)
    if not v or math.isnan(v) or v <= 0 or math.isnan(a):
        return float("nan")
    return float((a - rf) / v)


def ma(close: pd.Series, n: int) -> float | None:
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < n:
        return None
    return float(c.tail(n).mean())


def atr(df: pd.DataFrame, n: int = 14) -> float | None:
    """平均真实波幅。df 需含 high/low/close。"""
    if df is None or len(df) < n + 1:
        return None
    d = df.tail(n + 1)
    high = pd.to_numeric(d["high"], errors="coerce")
    low = pd.to_numeric(d["low"], errors="coerce")
    close = pd.to_numeric(d["close"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr = tr.dropna()
    if len(tr) == 0:
        return None
    return float(tr.mean())


def rel_strength(close: pd.Series, bench: pd.Series, days: int = 250) -> float | None:
    """相对基准的超额收益（个股收益 − 基准收益）。"""
    c = pd.to_numeric(close, errors="coerce").dropna()
    b = pd.to_numeric(bench, errors="coerce").dropna()
    if len(c) < days or len(b) < days:
        return None
    rc = c.iloc[-1] / c.iloc[-days] - 1.0
    rb = b.iloc[-1] / b.iloc[-days] - 1.0
    return float(rc - rb)


# ---------------------------------------------------------------- 评分工具
def score_linear(x: float | None, lo: float, hi: float, max_score: float) -> float:
    """线性打分：x ≤ lo 得 0 分，x ≥ hi 得满分，中间线性。"""
    if x is None:
        return 0.0
    if hi == lo:
        return max_score if x >= hi else 0.0
    t = (x - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    return t * max_score


def score_inverse(x: float | None, good: float, bad: float, max_score: float) -> float:
    """越小越好：x ≤ good 满分，x ≥ bad 得 0。"""
    if x is None:
        return 0.0
    if bad == good:
        return max_score if x <= good else 0.0
    t = (x - good) / (bad - good)
    t = max(0.0, min(1.0, t))
    return (1.0 - t) * max_score


def mean_ignore_none(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return float(np.mean(v))


# ---------------------------------------------------------------- 基金「持有期」解析（2026-11-25）
# 为什么放在 metrics（纯函数模块）而不是 plan.py 或 otc.py：两处都要用，而这个解析是
# 「从名字里读合同条款」的纯字符串活，跟计划/取数都无关。放在任一侧都会造成 import 方向问题
# （otc.py 不能 import plan.py：plan 是更上层编排），所以下沉到两边都 import 的 metrics。
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_HOLD_PERIOD_RE = re.compile(
    r"(?P<n>\d+|[一二两三四五六七八九十]{1,3})\s*(?P<u>个月|月|天|日|年|周)\s*(?:滚动)?持有")


def hold_period_days(name) -> int | None:
    """从基金名称解析「持有期」天数；None = 名称里没有持有期条款（随时可赎）。

    >>> hold_period_days("建信鑫弘180天持有期债券C")
    180
    >>> hold_period_days("建信鑫和30天持有期债券C")
    30
    >>> hold_period_days("永赢中债3-5年政金债指数C") is None
    True

    为什么需要：2026-11-02 买了 018193 建信鑫弘「180 天持有期」C（2,300 元），钱锁到 2027-05-01。
    选池只看了业绩/回撤/费率/规模，**没看合同锁定期**；而 SKILL 的「R2 不足 90 天不赎回」
    只是策略纪律 —— 锁定期比 90 天更长时，纪律根本轮不到生效。
    """
    s = str(name or "")
    mm = _HOLD_PERIOD_RE.search(s)
    if not mm:
        # A11：定开/封闭产品名里常常不写「N 天持有期」，但封闭期内同样不能赎回。
        # fail-closed 按最长持有期处理（10 年），选池与赎回守卫都会把它挡掉。
        if any(k in s for k in ("定开", "封闭")):
            return 3650
        return None
    raw, unit = mm.group("n"), mm.group("u")
    n = int(raw) if raw.isdigit() else _CN_NUM.get(raw)
    if not n:
        return None
    if unit == "年":
        return n * 365
    if unit == "周":
        return n * 7
    if unit in ("个月", "月"):
        return n * 30
    return n


# ---------------------------------------------------------------- 债券「前瞻票息」（2026-11-25，B-12）
# 为什么放这里：otc.py 给 R2 池打分要用，plan.py 的 R2 钱账也要用；两边都 import metrics。
# 为什么一定要前瞻口径：R2 池的 ret_1y/ret_3y 全部落在 2023–2026 的债牛里（SKILL §8.11 原话：
# 「这是过去 3 年落在债牛里的历史净值口径，不是预期收益」）。用后视均值当预期 = 把后视镜当挡风玻璃；
# 前瞻能拿到的确定性数据只有国债曲线的到期收益率。
def fwd_bond_yield(cache_dir=None, years=(3, 5)):
    """国债曲线缓存里 3Y/5Y 最新到期收益率的算术均值（小数）。取不到 → (None, "")。

    缓存文件：`cache/cn_treasury_yield/gov_curve__YYYY-MM-DD.csv`，
    列 y3/y5 是**百分数**（1.240 表示 1.24%），这里统一换成小数。
    """
    import os
    if cache_dir is None:
        try:
            import config
            cache_dir = os.path.join(config.CACHE_DIR, "cn_treasury_yield")
        except Exception:          # noqa: BLE001 —— 纯计算模块不硬依赖 config
            return None, ""
    try:
        files = sorted(f for f in os.listdir(cache_dir)
                       if f.startswith("gov_curve__") and f.endswith(".csv"))
    except OSError:
        return None, ""
    if not files:
        return None, ""
    asof = files[-1][len("gov_curve__"):-len(".csv")]
    try:
        df = pd.read_csv(os.path.join(cache_dir, files[-1]))
    except Exception:              # noqa: BLE001
        return None, ""
    if df is None or df.empty:
        return None, ""
    row = df.iloc[-1]
    vals = []
    for i in years:
        x = f(row.get("y%d" % i))
        if x is None:
            continue
        vals.append(x / 100.0 if x > 0.2 else x)   # 曲线 CSV 是百分数
    if not vals:
        return None, ""
    return float(np.mean(vals)), asof
