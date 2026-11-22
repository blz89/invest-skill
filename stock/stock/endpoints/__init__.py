# -*- coding: utf-8 -*-
"""取数接口层：49 个数据接口的统一出口。

每个接口一个函数，按数据来源分组实现：

- :mod:`.em`     —— 东方财富（基金、ETF、估值、市场概况、宏观）
- :mod:`.sina`   —— 新浪财经（债券、外汇、A 股、指数、板块、快讯、交易日历）
- :mod:`.misc`   —— 其他来源（中证指数、沪深交易所、金十、财联社、百度、乐咕、蛋卷）
- :mod:`.ohlc`   —— 通用 K 线（腾讯行情，支持前/后复权）

所有接口都走 :mod:`stock.http`（统一 UA、限流、重试、JSONP 剥壳），
返回 DataFrame 的列名与顺序在 ``stock/skilldata/datasource.py`` 里被下游直接使用，
**改名或改列都会破坏策略脚本**。

加新接口：在对应分组模块里实现，再把名字加进本文件 ``__all__``。
"""
from __future__ import annotations

from . import em, misc, ohlc, sina

__all__ = [
    # 债券 / 外汇 / 宏观
    "bond_china_yield",
    "bond_zh_hs_daily",
    "currency_boc_sina",
    "macro_china_cpi",
    "macro_china_lpr",
    "macro_china_money_supply",
    "macro_china_pmi",
    "macro_china_ppi",
    "macro_china_shibor_all",
    "macro_china_shrzgm",
    # 货币基金 / 开放式基金 / ETF
    "fund_money_fund_daily_em",
    "fund_money_fund_info_em",
    "fund_open_fund_daily_em",
    "fund_open_fund_info_em",
    "fund_purchase_em",
    "fund_etf_hist_em",
    "fund_etf_spot_em",
    "fund_etf_category_sina",
    "fund_etf_hist_sina",
    "fund_etf_spot_ths",
    "fund_fee_em",
    "fund_money_rank_em",
    "fund_individual_basic_info_xq",
    "fund_scale_em",
    # 指数 / 板块 / 市场概况
    "index_stock_cons_csindex",
    "stock_zh_index_daily",
    "stock_zh_index_hist_csindex",
    "stock_sector_detail",
    "stock_sector_spot",
    "stock_sse_deal_daily",
    "stock_szse_summary",
    # 个股
    "stock_zh_a_daily",
    "stock_zh_a_hist",
    "stock_info_global_cls",
    "stock_info_global_sina",
    "stock_market_activity_legu",
    "stock_value_em",
    "stock_dividend_em",
    "stock_zh_valuation_baidu",
    "stock_financial_abstract",
    "stock_financial_analysis_indicator",
    "stock_notice_report",
    "stock_notice_text",
    "stock_info_a_code_name",
    # 交易日历
    "tool_trade_date_hist_sina",
    "stock_fin_balance_em",
    "stock_fin_income_em",
    "stock_fin_cashflow_em",
    "stock_forecast_em",
]

#: 接口层版本（``python -m stock doctor`` 会打印）
__version__ = "2026.11"

_GROUPS = (em, sina, misc, ohlc)

_missing: list[str] = []
for _name in __all__:
    for _mod in _GROUPS:
        _fn = getattr(_mod, _name, None)
        if _fn is not None:
            globals()[_name] = _fn
            break
    else:
        _missing.append(_name)

if _missing:  # 导入期就 fail loud，别等运行到某个接口才发现
    raise ImportError("取数接口层缺函数：%s" % ", ".join(_missing))

del _name, _mod, _fn
