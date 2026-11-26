# -*- coding: utf-8 -*-
"""联网冒烟测试（默认跳过）：`STOCK_LIVE=1 python -m unittest stock.tests.test_live -v`。

每个源各打一发，验证「能连通 + 返回结构对得上」，不做业务断言。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # D:\

from stock import api  # noqa: E402

LIVE = os.environ.get("STOCK_LIVE") == "1"


@unittest.skipUnless(LIVE, "需要 STOCK_LIVE=1 才跑联网测试")
class TestLive(unittest.TestCase):
    def test_endpoints_daily(self):
        df = api.daily("600519", start="2026-11-01", end="2026-11-13")
        self.assertFalse(df.empty)
        for col in ("date", "open", "high", "low", "close", "volume"):
            self.assertIn(col, df.columns)
        self.assertTrue(df["close"].notna().all())

    def test_endpoints_index(self):
        df = api.index_daily("000300", start="2026-11-01", end="2026-11-13")
        self.assertFalse(df.empty)
        self.assertIn("close", df.columns)

    def test_eastmoney_fund(self):
        df = api.fund_lsjz("004985", page_size=10)
        self.assertFalse(df.empty)
        self.assertIn("nav", df.columns)

    def test_eastmoney_quote(self):
        df = api.quote(["600519", "000300.SH", "510300"], source="em")
        self.assertEqual(len(df), 3)
        self.assertTrue((df["price"] > 0).all())

    def test_money_funds(self):
        df = api.money_fund_rates()
        self.assertGreater(len(df), 100)
        self.assertGreater(df["y7_pct"].max(), 0)

    def test_repo(self):
        df = api.repo_rate("204001")
        self.assertFalse(df.empty)
        self.assertIn("close", df.columns)

    def test_fuyao_if_key(self):
        from stock.sources import fuyao as ts
        if not ts.available():
            self.skipTest("无同花顺 key")
        rows = ts.snapshot(["600519.SH"])
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
