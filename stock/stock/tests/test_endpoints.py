# -*- coding: utf-8 -*-
"""离线自检：取数接口层 ``stock.endpoints`` 的完整性。

三条断言
--------
1. ``stock.endpoints`` 暴露 49 个接口名，且都可调用；
2. 四个分组模块（em / sina / misc / ohlc）各自至少实现一个接口；
3. 接口层不直接依赖第三方 HTTP 库（统一走 :mod:`stock.http`）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # <本技能目录>\stock\stock
ENDPOINTS = ROOT / "endpoints"

EXPECTED = [
    "__version__", "bond_china_yield", "bond_zh_hs_daily", "currency_boc_sina",
    "macro_china_cpi", "macro_china_lpr", "macro_china_money_supply", "macro_china_pmi",
    "macro_china_ppi", "macro_china_shibor_all", "macro_china_shrzgm",
    "fund_money_fund_daily_em", "fund_money_fund_info_em", "fund_open_fund_daily_em",
    "fund_open_fund_info_em", "fund_purchase_em",
    "fund_etf_hist_em", "fund_etf_spot_em", "fund_etf_category_sina", "fund_etf_hist_sina",
    "fund_etf_spot_ths", "fund_fee_em", "fund_money_rank_em", "fund_individual_basic_info_xq",
    "fund_scale_em",
    "index_stock_cons_csindex", "stock_zh_index_daily", "stock_zh_index_hist_csindex",
    "stock_sector_detail", "stock_sector_spot", "stock_sse_deal_daily", "stock_szse_summary",
    "stock_zh_a_daily", "stock_zh_a_hist", "stock_info_global_cls", "stock_info_global_sina",
    "stock_market_activity_legu", "stock_value_em", "stock_zh_valuation_baidu",
    "stock_financial_abstract", "stock_financial_analysis_indicator", "stock_notice_report",
    "stock_notice_text",
    "stock_info_a_code_name",
    "stock_dividend_em",
    "stock_fin_balance_em", "stock_fin_income_em",
    "stock_fin_cashflow_em", "stock_forecast_em",
    "tool_trade_date_hist_sina",
]

FORBIDDEN_IMPORTS = ("import requests", "from requests")


class TestEndpointSurface(unittest.TestCase):
    def test_exports_all(self):
        from stock import endpoints as ep
        missing = [n for n in EXPECTED if not hasattr(ep, n)]
        self.assertEqual(missing, [], "接口层缺函数：%s" % missing)
        not_callable = [n for n in EXPECTED if n != "__version__" and not callable(getattr(ep, n))]
        self.assertEqual(not_callable, [], "不可调用：%s" % not_callable)

    def test_groups_exist(self):
        for group in ("em", "sina", "misc", "ohlc"):
            mod = __import__("stock.endpoints.%s" % group, fromlist=["x"])
            self.assertTrue(any(hasattr(mod, n) for n in EXPECTED), group)

    def test_no_direct_third_party_http(self):
        files = sorted(ENDPOINTS.glob("*.py"))
        self.assertGreater(len(files), 3)
        for p in files:
            text = p.read_text(encoding="utf-8")
            for bad in FORBIDDEN_IMPORTS:
                self.assertNotIn(bad, text, "%s 里还有 %r" % (p.name, bad))

    def test_import_in_clean_subprocess(self):
        code = textwrap.dedent(
            """
            import stock.endpoints as ep
            names = [n for n in ep.__all__ if callable(getattr(ep, n))]
            print(len(names))
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT.parent)
        env["PYTHONIOENCODING"] = "utf-8"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(int(out.stdout.strip() or 0), 49)


class TestEtfFallbackMappers(unittest.TestCase):
    """ETF 兜底映射的离线自检（不联网）：列序、单位、派生列。"""

    def setUp(self):
        if str(ROOT.parent) not in sys.path:
            sys.path.insert(0, str(ROOT.parent))
        import pandas as pd
        from stock.endpoints import em
        self.pd = pd
        self.em = em

    def test_spot_columns_and_units(self):
        pd = self.pd
        raw = pd.DataFrame({
            "代码": ["sz159998"], "名称": ["计算机ETF天弘"], "最新价": ["0.820"],
            "涨跌额": ["0.011"], "涨跌幅": ["1.36"], "买入": ["0.819"], "卖出": ["0.820"],
            "昨收": ["0.809"], "今开": ["0.810"], "最高": ["0.830"], "最低": ["0.805"],
            "成交量": ["57689600"], "成交额": ["47853677"],
        })
        out = self.em._map_spot_sina(raw)
        self.assertEqual(list(out.columns), list(self.em._ETF_SPOT_COLUMNS))
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual(row["代码"], "159998")
        self.assertAlmostEqual(float(row["成交量"]), 576896.0, places=3)  # 份 -> 手，只除一次
        self.assertAlmostEqual(float(row["振幅"]), (0.830 - 0.805) / 0.809 * 100, places=2)
        self.assertNotEqual(row["换手率"], row["换手率"])  # 兜底源无换手率，占位 NaN
        self.assertAlmostEqual(float(row["最新价"]), 0.820, places=6)

    def test_spot_empty_input(self):
        out = self.em._map_spot_sina(self.pd.DataFrame())
        self.assertEqual(list(out.columns), list(self.em._ETF_SPOT_COLUMNS))
        self.assertTrue(out.empty)

    def test_hist_daily_columns(self):
        pd = self.pd
        df = pd.DataFrame({
            "date": ["2026-11-12", "2026-11-13"],
            "open": [4.000, 4.100], "close": [4.100, 4.200],
            "high": [4.150, 4.250], "low": [3.980, 4.090],
            "volume": [1000, 2000], "amount": [400000.0, 830000.0],
        })
        out = self.em._map_hist_ohlc(df, "daily", derived=True)
        self.assertEqual(list(out.columns), list(self.em._ETF_HIST_COLUMNS))
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[1]["日期"], "2026-11-13")
        self.assertAlmostEqual(float(out.iloc[1]["涨跌额"]), 0.1, places=3)
        self.assertAlmostEqual(float(out.iloc[1]["涨跌幅"]), 0.1 / 4.1 * 100, places=2)

    def test_hist_weekly_resample(self):
        pd = self.pd
        df = pd.DataFrame({
            "date": ["2026-11-02", "2026-11-06", "2026-11-09", "2026-11-13"],
            "open": [1.0, 1.1, 1.2, 1.3], "close": [1.1, 1.2, 1.3, 1.4],
            "high": [1.15, 1.25, 1.35, 1.45], "low": [0.95, 1.05, 1.15, 1.25],
            "volume": [100, 200, 300, 400],
        })
        out = self.em._map_hist_ohlc(df, "weekly", derived=False)
        self.assertEqual(list(out.columns), list(self.em._ETF_HIST_COLUMNS))
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(float(out.iloc[0]["收盘"]), 1.2, places=6)
        self.assertAlmostEqual(float(out.iloc[0]["成交量"]), 300.0, places=6)

    def test_host_cooldown(self):
        em = self.em
        em._reset_host_down()
        hosts = ("https://push2his.eastmoney.com/a",)
        self.assertEqual(em._live_hosts(hosts), list(hosts))
        em._mark_host_down(hosts[0])
        self.assertEqual(em._live_hosts(hosts), [])
        em._reset_host_down()
        self.assertEqual(em._live_hosts(hosts), list(hosts))


if __name__ == "__main__":
    unittest.main()
