# -*- coding: utf-8 -*-
"""离线测试（不联网、无需 key）：`python -m pytest stock/tests -q` 或 `python -m unittest`。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # D:\

from stock import cache, codes, config, http, patch  # noqa: E402
from stock.sources import eastmoney as em  # noqa: E402


class TestCodes(unittest.TestCase):
    def test_parse_and_formats(self):
        c = codes.parse("600519")
        self.assertEqual(c["kind"], "stock")
        self.assertEqual(c["market"], "sh")
        self.assertEqual(codes.to_sina("600519"), "sh600519")
        self.assertEqual(codes.to_em_secid("600519"), "1.600519")
        self.assertEqual(codes.to_thscode("600519"), "600519.SH")
        self.assertEqual(codes.to_em_secid("000001"), "0.000001")      # 默认按深市股票
        self.assertEqual(codes.to_em_secid("000300.SH"), "1.000300")   # 显式后缀 → 沪市指数
        self.assertEqual(codes.to_em_secid("510300"), "1.510300")
        self.assertEqual(codes.to_em_secid("159915"), "0.159915")
        self.assertEqual(codes.bare("sh600519"), "600519")

    def test_invalid(self):
        with self.assertRaises(codes.CodeError):
            codes.parse("abc")
        with self.assertRaises(codes.CodeError):
            codes.parse("60051")


class TestPatch(unittest.TestCase):
    def test_num(self):
        self.assertEqual(patch.num("1.23%"), 1.23)
        self.assertEqual(patch.num("1,234.5"), 1234.5)
        self.assertTrue(pd.isna(patch.num("---")))
        self.assertTrue(pd.isna(patch.num("")))
        self.assertEqual(patch.pct("1.23%"), 0.0123)

    def test_stdout_frames(self):
        raw = pd.DataFrame({
            "日期": ["2026-11-13", "2026-11-12", "2026-11-13"],
            "开盘": ["10.0", "9.9", "99"], "收盘": ["10.5", "10.0", "99"],
            "最高": ["10.6", "10.1", "99"], "最低": ["9.8", "9.8", "99"],
            "成交量": ["1000", "900", "1"], "成交额": ["---", "9000", "1"],
        })
        df = patch.stdout_frames(raw, kind="daily")
        self.assertEqual(list(df.columns), ["date", "open", "high", "low", "close", "volume", "amount"])
        self.assertEqual(len(df), 2)                      # 去重
        self.assertEqual(df["date"].iloc[0], "2026-11-12")  # 升序
        self.assertTrue(pd.isna(df["amount"].iloc[-1]))   # 去重保留的第一条 11-13，成交额是 "---"

    def test_money_fund_frames(self):
        raw = pd.DataFrame({
            "基金代码": ["004985", "000868"], "基金简称": ["博时合晶货币B", "国投瑞银增利宝货币A"],
            "2026-11-13-万份收益": ["0.3411", "---"],
            "2026-11-13-7日年化%": ["1.406", "1.339"],
        })
        df = patch.money_fund_frames(raw)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["data_date"].iloc[0], "2026-11-13")
        self.assertAlmostEqual(df["y7_pct"].iloc[0], 1.406)
        self.assertTrue(pd.isna(df["wanfen"].iloc[1]))


class TestCache(unittest.TestCase):
    def test_frame_caches_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
                 mock.patch.object(cache.config, "OUT_DIR", Path(td)):
                calls = {"n": 0}

                def loader():
                    calls["n"] += 1
                    return pd.DataFrame({"a": [1, 2]})

                d1 = cache.frame("t_kind", {"x": 1}, loader, source="test")
                d2 = cache.frame("t_kind", {"x": 1}, loader, source="test")
                self.assertEqual(calls["n"], 1)
                self.assertEqual(len(d1), len(d2))
                prov = Path(td) / "provenance.jsonl"
                self.assertTrue(prov.exists())
                self.assertIn("test", prov.read_text(encoding="utf-8"))

    def test_post_hook(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
                 mock.patch.object(cache.config, "OUT_DIR", Path(td)):
                raw = pd.DataFrame({"日期": ["2026-11-13"], "收盘": ["10"]})
                df = cache.frame("t2", {}, lambda: raw, source="t",
                                 post=lambda x: patch.stdout_frames(x, "daily"))
                self.assertEqual(list(df.columns), ["date", "close"])


class TestTencent(unittest.TestCase):
    def test_kline_parse(self):
        from stock.sources import tencent as tx
        fake = {"data": {"sh600519": {"qfqday": [
            ["2026-11-12", "1395.13", "1410.27", "1411.00", "1388.11", "32373"],
            ["2026-11-13", "1404.00", "1435.26", "1437.69", "1394.00", "41780"],
        ]}}}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
             mock.patch.object(cache.config, "OUT_DIR", Path(td)), \
             mock.patch.object(http, "get_json", return_value=fake):
            df = tx.kline("600519", start="2026-11-01", end="2026-11-13")
        self.assertEqual(list(df.columns), ["date", "open", "close", "high", "low", "volume"])
        self.assertEqual(df["date"].iloc[-1], "2026-11-13")
        self.assertAlmostEqual(df["close"].iloc[-1], 1435.26)

    def test_kline_uses_dashed_dates(self):
        """腾讯要求 begin/end 是 YYYY-MM-DD，未来日期返回空——参数必须原样透传。"""
        from stock.sources import tencent as tx
        seen = {}

        def fake_get(url, params=None, headers=None, **kw):
            seen["param"] = params["param"]
            return {"data": {"sh600519": {"qfqday": []}}}

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
             mock.patch.object(cache.config, "OUT_DIR", Path(td)), \
             mock.patch.object(http, "get_json", side_effect=fake_get):
            tx.kline("600519", start="2026-11-01", end="2026-11-13")
        self.assertEqual(seen["param"], "sh600519,day,2026-11-01,2026-11-13,640,qfq")


class TestHttp(unittest.TestCase):
    def test_retry_then_ok(self):
        calls = {"n": 0}

        def transport(url, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return 500, "boom"
            return 200, '{"ok": 1}'

        with mock.patch.object(http.time, "sleep", lambda *_: None):
            js = http.get_json("https://x", transport=transport, retries=2)
        self.assertEqual(js, {"ok": 1})
        self.assertEqual(calls["n"], 2)

    def test_4xx_no_retry(self):
        calls = {"n": 0}

        def transport(url, headers, timeout):
            calls["n"] += 1
            return 404, "nope"

        with self.assertRaises(http.HttpError):
            http.request("https://x", transport=transport, retries=3)
        self.assertEqual(calls["n"], 1)

    def test_jsonp_unwrap(self):
        def transport(url, headers, timeout):
            return 200, 'cb({"data": {"f2": 1}})'

        js = http.get_jsonp("https://x", transport=transport)
        self.assertEqual(js["data"]["f2"], 1)


class TestEastmoney(unittest.TestCase):
    def test_quote_snapshot_mapping(self):
        fake = {"data": {"diff": [
            {"f2": 10.5, "f3": 1.2, "f5": 100, "f6": 1050, "f8": 0.5, "f9": 20.1, "f12": "600519",
             "f13": 1, "f14": "贵州茅台", "f15": 10.6, "f16": 9.8, "f17": 10.0, "f18": 10.4,
             "f20": 1e12, "f21": 1e12, "f23": 8.5},
        ]}}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
             mock.patch.object(cache.config, "OUT_DIR", Path(td)), \
             mock.patch.object(http, "get_json", return_value=fake):
            df = em.quote_snapshot(["600519"])
        self.assertEqual(len(df), 1)
        self.assertEqual(df["code"].iloc[0], "600519")
        self.assertAlmostEqual(df["price"].iloc[0], 10.5)

    def test_fund_lsjz_mapping(self):
        fake = {"Data": {"LSJZList": [
            {"FSRQ": "2026-11-13", "DWJZ": "1.10", "LJJZ": "1.30", "SGZT": "开放申购", "SHZT": "开放赎回"},
            {"FSRQ": "2026-11-12", "DWJZ": "1.09", "LJJZ": "1.29", "SGZT": "开放申购", "SHZT": "开放赎回"},
        ]}}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(cache.config, "CACHE_DIR", Path(td)), \
             mock.patch.object(cache.config, "OUT_DIR", Path(td)), \
             mock.patch.object(http, "get_json", return_value=fake):
            df = em.fund_lsjz("000001", page_size=2)
        self.assertEqual(list(df["date"]), ["2026-11-12", "2026-11-13"])
        self.assertAlmostEqual(df["nav"].iloc[-1], 1.10)


class TestConfig(unittest.TestCase):
    def test_force_refresh_env(self):
        with mock.patch.dict(os.environ, {"STOCK_REFRESH": "1"}):
            self.assertTrue(config.force_refresh())
        with mock.patch.dict(os.environ, {"STOCK_REFRESH": "0"}):
            self.assertFalse(config.force_refresh())


if __name__ == "__main__":
    unittest.main(verbosity=2)

