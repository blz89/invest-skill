# -*- coding: utf-8 -*-
"""数据清洗/兼容层：把各家原始返回规整成统一列名与数值类型。

这里放两类东西：
1. `num/pct` —— 把 `'---'`、`''`、`'--'`、`'1.23%'` 安全转成数字；
2. `stdout_frames` —— 把中英混排的列名统一成 ascii 标准列（date/open/close/...）。

**只在 import 时改函数行为，不联网、不发请求。**
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

# 中文列名 → 标准列名（覆盖日线/指数/ETF/场外基金四类）
COLUMN_MAP = {
    "日期": "date", "净值日期": "date", "时间": "date",
    "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount",
    "振幅": "amplitude_pct", "涨跌幅": "chg_pct", "涨跌额": "chg_amt",
    "换手率": "turnover_pct",
    "单位净值": "nav", "累计净值": "acc_nav", "日增长率": "chg_pct",
    "基金代码": "code", "基金简称": "name", "股票代码": "code",
    "万份收益": "wanfen", "7日年化%": "y7_pct", "7日年化": "y7_pct",
    "申购状态": "buy", "赎回状态": "sell",
    "代码": "code", "名称": "name",
}

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def num(x: Any) -> float:
    """把任意脏值转 float：`'---'`→NaN，`'1.23%'`→1.23，`'1,234'`→1234。"""
    if x is None:
        return float("nan")
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "---", "nan", "None", "null"):
        return float("nan")
    m = _NUM_RE.search(s)
    return float(m.group(0)) if m else float("nan")


def pct(x: Any) -> float:
    """百分比文本转**小数**：`'1.23%'` → 0.0123；`1.23`（已是数字）按 0.0123 理解吗？不——数字原样返回。"""
    if isinstance(x, str) and "%" in x:
        return num(x) / 100.0
    return num(x)


def stdout_frames(df: pd.DataFrame, kind: str = "daily") -> pd.DataFrame:
    """统一列名 + 数值化 + 按日期升序去重。

    kind="daily" / "index" / "fund" / "money"
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.rename(columns={c: COLUMN_MAP.get(str(c).strip(), str(c).strip()) for c in df.columns})
    # 有些接口列名带日期前缀（货基快照），单独处理
    if kind == "money":
        return money_fund_frames(out)
    keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount",
                        "chg_pct", "turnover_pct", "amplitude_pct", "chg_amt",
                        "nav", "acc_nav", "code", "name") if c in out.columns]
    out = out[keep].copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date")
    for c in out.columns:
        if c in ("date", "code", "name"):
            continue
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.reset_index(drop=True)


def money_fund_frames(df: pd.DataFrame) -> pd.DataFrame:
    """货基快照规范化：code/name + 最新一组「万份收益 / 7日年化」。

    东财快照列名形如 `2026-11-13-万份收益`、`2026-11-13-7日年化%`（每天一组），
    这里取**最后一组**（最新交易日），并保留日期字段 `data_date`。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    cols = {str(c): str(c) for c in df.columns}
    code_col = next((c for c in cols if "基金代码" in c or c == "code"), None)
    name_col = next((c for c in cols if "基金简称" in c or "基金名称" in c or c == "name"), None)
    date_cols = [c for c in cols if re.match(r"^\d{4}-\d{2}-\d{2}", c)]
    data_date = ""
    if date_cols:
        data_date = sorted({re.match(r"^(\d{4}-\d{2}-\d{2})", c).group(1) for c in date_cols})[-1]
        same = [c for c in date_cols if c.startswith(data_date)]
        wan = next((c for c in same if "万份收益" in c), None)
        y7 = next((c for c in same if "7日年化" in c), None)
    else:
        wan = next((c for c in cols if "万份收益" in c), None)
        y7 = next((c for c in cols if "7日年化" in c), None)
    out = pd.DataFrame({
        "code": df[code_col] if code_col else pd.Series(dtype=str),
        "name": df[name_col] if name_col else pd.Series(dtype=str),
        "wanfen": df[wan].map(num) if wan else float("nan"),
        "y7_pct": df[y7].map(num) if y7 else float("nan"),
        "data_date": data_date,
    })
    try:
        out["code"] = out["code"].astype(str).str.zfill(6)
    except Exception:
        pass
    return out.dropna(subset=["wanfen", "y7_pct"], how="all").reset_index(drop=True)
