# -*- coding: utf-8 -*-
"""io/数据层 bug 修复的离线自检（2026-11-17 bug-io 审计后的修复）。

覆盖：
- ``codes.parse`` 北交所 920/4/8 开头不再误判沪市；``get_market_id`` 一致；
- ``codes.norm_code`` 兼容 ``sh.600519`` / ``.SS`` / ``.0`` 等写法；
- ``http.throttle`` 并发下仍保持同域名最小间隔（原 read-then-sleep 无锁竞态）；
- ``http.get_json`` 剥 UTF-8 BOM；
- ``_etf_spot_table`` single-flight（并发补 bar 只拉一次行情表）；
- ``_write_cache_atomic`` 写失败返回 False 且记 provenance（不再静默吞 OSError）；
- ``_coverage`` 对 ``etf_universe`` 的 expected 缺口按容差处理（THS 兜底可用时不判红）；
- ``config.OUT_DIR`` 读 ``STOCK_OUT_DIR``（-Root 演练不再写生产 out）；
- ``retention.targets`` 包含 ``data\\shared\\snapshots``（统一记录不会无界增长）。

跑法：``python -m unittest stock.tests.test_bugfix_io -v``
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from stock import codes, config, http, prefetch, retention
from stock.endpoints import em as em_ep
from stock.skilldata import datasource as ds

REPO = Path(__file__).resolve().parents[2]


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        cfg = types.SimpleNamespace(
            CACHE_DIR=str(self.cache), OUT_DIR=str(self.out),
            FORCE_IPV4=False, PRICE_YEARS=4, EM_ENABLED=False,
        )
        ds.init(cfg, flavor="shared")
        env = mock.patch.dict(os.environ, {
            "A_SHARE_DATA_DATE": "", "A_SHARE_OFFLINE": "", "A_SHARE_FORCE_REFRESH": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(ds._prov.clear)
        ds._SPOT_BAR_MEMO.clear()
        self.addCleanup(ds._SPOT_BAR_MEMO.clear)


class TestCodes(unittest.TestCase):
    def test_bse_920(self):
        self.assertEqual(codes.parse("920001")["market"], "bj")
        self.assertEqual(codes.to_em_secid("920001"), "0.920001")
        self.assertEqual(codes.to_thscode("920001"), "920001.BJ")
        self.assertEqual(codes.to_sina("920001"), "bj920001")

    def test_sh_b_900_and_bse_4_8(self):
        self.assertEqual(codes.parse("900901")["market"], "sh")
        self.assertEqual(codes.parse("430047")["market"], "bj")
        self.assertEqual(codes.parse("830799")["market"], "bj")
        self.assertEqual(codes.parse("600519")["market"], "sh")
        self.assertEqual(codes.parse("000001")["market"], "sz")

    def test_get_market_id(self):
        self.assertEqual(em_ep.get_market_id("920001"), 0)
        self.assertEqual(em_ep.get_market_id("430047"), 0)
        self.assertEqual(em_ep.get_market_id("830799"), 0)
        self.assertEqual(em_ep.get_market_id("600519"), 1)
        self.assertEqual(em_ep.get_market_id("510300"), 1)
        self.assertEqual(em_ep.get_market_id("000001"), 0)
        self.assertEqual(em_ep.get_market_id("159915"), 0)

    def test_norm_code_formats(self):
        cases = {
            "600519": "600519",
            "600519.SH": "600519",
            "600519.SS": "600519",
            "sh600519": "600519",
            "SH600519": "600519",
            "sh.600519": "600519",
            "600519.0": "600519",
            "000001": "000001",
            "430047.BJ": "430047",
            "bj430047": "430047",
        }
        for raw, want in cases.items():
            self.assertEqual(codes.norm_code(raw), want, raw)
        self.assertEqual(codes.norm_code("abc"), "abc")
        self.assertEqual(codes.norm_code("abc", strict=True), "")


class TestHttpThrottle(_Base):
    def setUp(self):
        super().setUp()
        http._last_hit.clear()
        http._host_locks.clear()
        self.addCleanup(http._last_hit.clear)
        self.addCleanup(http._host_locks.clear)

    def test_same_host_serialized_under_concurrency(self):
        gap = 0.05
        stamps: list[float] = []
        lock = threading.Lock()

        def worker():
            http.request("https://throttle.invalid/x",
                         transport=lambda url, hdrs, timeout: b'{"ok": 1}')
            with lock:
                stamps.append(time.monotonic())

        with mock.patch.object(http.config, "MIN_INTERVAL_S", gap):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            t0 = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - t0

        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(elapsed, gap * 5 * 0.9)
        for g in gaps:
            self.assertGreaterEqual(g, gap * 0.8, gaps)


class TestHttpGetJson(_Base):
    def test_bom_is_stripped(self):
        body = '\ufeff{"ok": 1}'.encode("utf-8")
        out = http.get_json("https://bom.invalid/x",
                            transport=lambda url, hdrs, timeout: body)
        self.assertEqual(out, {"ok": 1})


class TestSpotTableSingleFlight(_Base):
    def test_concurrent_calls_fetch_once(self):
        calls = {"n": 0}
        lock = threading.Lock()

        def fake(force=False):
            with lock:
                calls["n"] += 1
            time.sleep(0.05)
            return pd.DataFrame({"code": ["510300"]})

        with mock.patch.object(ds, "_etf_spot_tx_cached", side_effect=fake):
            out: list = []
            threads = [threading.Thread(target=lambda: out.append(ds._etf_spot_table()))
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(calls["n"], 1, "并发补 bar 只应拉一次行情表")
        self.assertEqual(len(out), 8)
        self.assertTrue(all(v is out[0] for v in out))


class TestWriteCacheAtomic(_Base):
    def test_failure_returns_false_and_records_prov(self):
        bad = self.root / "no_such_dir" / "x__2026-11-16.csv"
        df = pd.DataFrame({"a": [1, 2, 3]})
        ok = ds._write_cache_atomic(df, str(bad), kind="demo", key="k1")
        self.assertFalse(ok)
        self.assertTrue(any(p.get("source") == "cache_write_error" for p in ds._prov))

    def test_success_returns_true_and_writes(self):
        path = self.cache / "demo" / "k1__2026-11-16.csv"
        path.parent.mkdir(parents=True)
        ok = ds._write_cache_atomic(pd.DataFrame({"a": [1, 2]}), str(path),
                                    kind="demo", key="k1")
        self.assertTrue(ok)
        self.assertEqual(len(pd.read_csv(path)), 2)


class TestCoverageTolerance(unittest.TestCase):
    def test_etf_universe_gap_tolerated(self):
        counts = {"calendar": 1, "market_snapshot": 1, "index_daily": 5,
                  "index_cons": 2, "etf_daily": 600, "daily": 800, "fin": 800,
                  "etf_spot_tx": 1}
        with mock.patch.object(prefetch, "_scan_counts", return_value=counts):
            cov = prefetch._coverage("2026-11-16", {"etf_universe": 1})
        self.assertTrue(cov["ok"], cov["problems"])
        self.assertTrue(any("etf_universe" in t for t in cov["tolerated"]))


class TestConfigOutDir(unittest.TestCase):
    def test_stock_out_dir_env(self):
        out = Path(tempfile.mkdtemp(prefix="stock-out-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        env = dict(os.environ)
        env["STOCK_OUT_DIR"] = str(out)
        env["PYTHONPATH"] = str(REPO)
        env["PYTHONIOENCODING"] = "utf-8"
        code = ("from stock import config; "
                "print(config.OUT_DIR); print(config.paths()['out'])")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        self.assertEqual(Path(lines[0]), out)
        self.assertEqual(Path(lines[1]), out)


class TestRetentionSnapshots(unittest.TestCase):
    def test_targets_include_snapshots(self):
        snaps = Path(config.SNAPSHOTS_DIR).resolve(strict=False)
        got = [Path(p).resolve(strict=False) for p in retention.targets()]
        self.assertIn(snaps, got)
        with_days = {Path(p).resolve(strict=False): d for p, d in retention.targets_with_days()}
        self.assertIn(snaps, with_days)
        self.assertEqual(with_days[snaps], config.RETENTION_DAYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
