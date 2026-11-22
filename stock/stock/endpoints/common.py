# -*- coding: utf-8 -*-
"""取数接口层的公共件。

设计约束
--------
- 只用 ``stock.http`` 发请求（统一 UA / 限流 / 退避重试），不直接用 requests；
- 返回的 DataFrame **列名、单位、索引** 是下游契约，
  改动必须同步 ``skilldata/datasource.py``；
- 不做缓存：缓存由调用方负责（``stock.cache`` 或 ``stock.skilldata.datasource``），
  这里保持"一次调用一次请求"，便于离线单测。
"""
from __future__ import annotations

import time

from ..http import get_json, get_text  # noqa: F401  （转出，供各分组模块使用）

#: 东财数据中心（宏观、估值等）统一入口
EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

EM_HEADERS = {
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}
FUND_HEADERS = {
    "Referer": "https://fundf10.eastmoney.com/",
}


def em_datacenter(**params) -> dict:
    """调东财数据中心接口，返回原始 JSON（dict）。"""
    return get_json(EM_DC, params=params, headers=EM_HEADERS)


def em_dc_rows(payload: dict) -> list[dict]:
    """从东财数据中心返回里取 ``result.data``，兼容 result 为 None 的情况。"""
    result = (payload or {}).get("result") or {}
    rows = result.get("data") or []
    if not isinstance(rows, list):
        raise ValueError("东财数据中心返回结构异常：%r" % (str(payload)[:200],))
    return rows


def ts() -> int:
    """毫秒时间戳（东财部分接口要 `_` 参数防缓存）。"""
    return int(time.time() * 1000)
