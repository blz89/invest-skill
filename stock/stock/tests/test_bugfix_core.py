# -*- coding: utf-8 -*-
"""CORE 审计（stock-audit/bugs/core-bugs.md）修复的回归测试。

覆盖：
* CORE-1  ETF 规模 last-good 并发写（atomic + 锁 + 每数据日一次）
* CORE-2  ETF 行情 memo 的 force TTL（收盘后必须能刷到新表）
* CORE-3  同花顺兜底补 bar 的 offline 守卫（冻结会话不得联网）
* CORE-4  用 data_date() 而不是真实 today（回放/冻结不前视）
* CORE-5  行业快照单板块失败：记 provenance，超过 10% fail loud
* CORE-8  _cached 冷启动 single-flight（同 key 只取一次）
* CORE-12 post 过滤成空：报错而不是写表头 CSV 导致永久重取
* CORE-16 缓存键带参数（fin 带 FIN_START_YEAR；index 归一化）

所有用例离线、只写临时目录，不碰生产 data/。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import pandas as pd

from stock.skilldata import datasource as ds


class _EnvCase(unittest.TestCase):
    """统一准备临时 CACHE_DIR/OUT_DIR + 清环境变量。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cache = os.path.join(self.tmp, "cache")
        self.out = os.path.join(self.tmp, "out")
        self._env = {k: os.environ.get(k) for k in
                     ("A_SHARE_DATA_DATE", "A_SHARE_OFFLINE", "STOCK_REFRESH",
                      "A_SHARE_FORCE_REFRESH")}
        for k in self._env:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)
        self.shim = types.SimpleNamespace(
            CACHE_DIR=self.cache, OUT_DIR=self.out,
            PRICE_YEARS=4, FIN_START_YEAR="2019", VALUATION_PERIOD="近五年",
            EM_ENABLED=False, SCALE_CACHE_MAX_TRADE_DAYS=5,
            SCALE_CACHE_FILENAME="etf_scale_lastgood.json",
            TX_QUOTE_BATCH=60, TX_QUOTE_SLEEP=0.25, FORCE_IPV4=False)
        ds.init(self.shim, flavor="test")

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestScaleCacheRace(_EnvCase):
    """CORE-1：8 线程并发写 last-good，JSON 必须完整且无 tmp 残余。"""

    def test_concurrent_save_stays_valid_json(self):
        aum = {f"{i:06d}": float(i) + 0.5 for i in range(1, 300)}
        errors = []

        def worker():
            try:
                for _ in range(5):
                    ds._save_etf_scale_cache(aum, "tencent_qt", force=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        path = ds._SCALE_CACHE_FILE
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(len(raw["aum"]), len(aum))
        self.assertEqual(raw["as_of"], ds.data_date())
        leftovers = [n for n in os.listdir(self.cache) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_once_per_data_day_unless_forced(self):
        ds._save_etf_scale_cache({"510300": 1800.0}, "tencent_qt")
        path = ds._SCALE_CACHE_FILE
        mtime = os.path.getmtime(path)
        time.sleep(0.02)
        ds._save_etf_scale_cache({"510300": 9999.0}, "tencent_qt")   # 同数据日 → 跳过
        self.assertEqual(os.path.getmtime(path), mtime)
        ds._save_etf_scale_cache({"510300": 9999.0}, "tencent_qt", force=True)
        self.assertNotEqual(os.path.getmtime(path), mtime)


class TestSpotMemo(_EnvCase):
    """CORE-2：force 结果带 TTL；非 force 同数据日只取一次。"""

    def test_force_ttl_and_data_date_key(self):
        calls = []
        fake = pd.DataFrame({"code": ["510300"], "aum_yi": [1800.0]})

        def fake_tx(force=False):
            calls.append(bool(force))
            return fake.copy()

        with mock.patch.object(ds, "_etf_spot_tx_cached", side_effect=fake_tx):
            ds._etf_spot_table()
            ds._etf_spot_table()
            self.assertEqual(len(calls), 1)              # 非 force memo 复用
            ds._etf_spot_table(force=True)
            ds._etf_spot_table(force=True)
            self.assertEqual(len(calls), 2)              # force 第二次命中 TTL
            old = ds._SPOT_BAR_FORCE_TTL
            ds._SPOT_BAR_FORCE_TTL = 0.0
            try:
                ds._etf_spot_table(force=True)          # TTL 过期 → 重新取
            finally:
                ds._SPOT_BAR_FORCE_TTL = old
            self.assertEqual(len(calls), 3)


class TestOfflineGuard(_EnvCase):
    """CORE-3：离线时同花顺兜底不得联网。"""

    def test_ths_today_bar_offline_returns_none(self):
        os.environ["A_SHARE_OFFLINE"] = "1"
        calls = []
        import stock.sources.fuyao as fuyao
        with mock.patch.object(fuyao, "etf_hist",
                               side_effect=lambda *a, **k: calls.append(1) or pd.DataFrame()):
            self.assertIsNone(ds._ths_today_bar("510300", ds.data_date()))
        self.assertEqual(calls, [])


class TestDataDate(_EnvCase):
    """CORE-4/16：daily/index/treasury 用数据日；键带参数。"""

    def test_fetch_daily_uses_data_date(self):
        os.environ["A_SHARE_DATA_DATE"] = "2026-06-30"
        seen = {}

        def fake_daily(symbol, start_date, end_date, adjust):
            seen.update(symbol=symbol, start=start_date, end=end_date)
            return pd.DataFrame({"date": ["2026-06-30"], "close": [10.0]})

        import stock.endpoints as ak
        with mock.patch.object(ak, "stock_zh_a_daily", side_effect=fake_daily):
            df = ds.fetch_daily("600519")
        self.assertEqual(len(df), 1)
        self.assertEqual(seen["end"], "20260630")
        files = os.listdir(os.path.join(self.cache, "daily"))
        self.assertTrue(any("2026-06-30" in n for n in files), files)

    def test_index_daily_key_is_normalized(self):
        import stock.endpoints as ak

        def fake_index(symbol):
            return pd.DataFrame({"date": [0], "close": [1.0]})

        with mock.patch.object(ak, "stock_zh_index_daily", side_effect=fake_index):
            ds.fetch_index_daily("sh000300")
            ds.fetch_index_daily("000300")
        files = os.listdir(os.path.join(self.cache, "index_daily"))
        self.assertEqual(len(files), 1, files)           # 两个入参写同一份缓存

    def test_fin_key_includes_start_year(self):
        import stock.endpoints as ak

        def fake_fin(symbol, start_year):
            return pd.DataFrame({"日期": ["2025-12-31"], "净资产收益率(%)": [10.0]})

        with mock.patch.object(ak, "stock_financial_analysis_indicator", side_effect=fake_fin):
            ds.fetch_fin("600519")
        files = os.listdir(os.path.join(self.cache, "fin"))
        self.assertTrue(any("2019" in n for n in files), files)

    def test_stock_refresh_env_counts_as_force(self):
        os.environ["STOCK_REFRESH"] = "1"
        self.assertTrue(ds._force_refresh())


class TestMarketSnapshotSectors(_EnvCase):
    """CORE-5：单板块失败记 provenance；≥10% 失败 fail loud。"""

    def _patch_endpoints(self, fail_labels):
        import stock.endpoints as ak

        sectors = pd.DataFrame({"label": ["a", "b", "c"],
                                "板块": ["行业A", "行业B", "行业C"]})

        def detail(sector):
            if sector in fail_labels:
                raise RuntimeError("sector boom")
            return pd.DataFrame({"code": ["600000"], "name": ["x"], "per": [10.0],
                                 "pb": [1.0], "mktcap": [1e6], "nmc": [1e6], "amount": [1e8]})

        return (mock.patch.object(ak, "stock_sector_spot", return_value=sectors),
                mock.patch.object(ak, "stock_sector_detail", side_effect=detail))

    def test_one_sector_fail_records_provenance(self):
        p1, p2 = self._patch_endpoints({"a"})
        with p1, p2:
            df = ds.fetch_market_snapshot()
        self.assertGreaterEqual(len(df), 1)
        prov = [p for p in ds._prov if p.get("source") == "sector_fail"]
        self.assertTrue(prov and "a" in prov[-1]["failed"])

    def test_two_of_three_sectors_fail_loud(self):
        p1, p2 = self._patch_endpoints({"a", "b"})
        with p1, p2:
            with self.assertRaises(RuntimeError):
                ds.fetch_market_snapshot()
        files = os.path.join(self.cache, "market_snapshot")
        self.assertFalse(os.path.isdir(files) and os.listdir(files), "残表不得写缓存")


class TestCachedGuards(_EnvCase):
    """CORE-8 single-flight + CORE-12 post 空表。"""

    def test_single_flight_same_key(self):
        calls = []

        def loader():
            time.sleep(0.05)
            calls.append(1)
            return pd.DataFrame({"a": [1]})

        def worker():
            ds._cached("demo", "k", loader)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)

    def test_post_empty_raises_and_marks_fail(self):
        def loader():
            return pd.DataFrame({"a": [1, 2]})

        with self.assertRaises(ValueError):
            ds._cached("demo", "empty", loader,
                       post=lambda d: d.iloc[0:0], fail_ttl_h=6.0)
        path = ds._cache_file("demo", "empty")
        self.assertTrue(os.path.exists(path[:-4] + ".failed"))


if __name__ == "__main__":
    unittest.main()
