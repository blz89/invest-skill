# -*- coding: utf-8 -*-
"""prefetch / 数据层健壮性修复的离线自检（不联网、不碰生产缓存）。

覆盖 2026-11-17 凌晨 bug 审计后的修复：
1. ``_intraday_block``：交易日收盘前拒绝跑 prefetch（否则当天缓存键被写成未收盘数据，
   19:00 计划任务会把它当缓存命中而静默消费陈旧数据）；
2. ``_write_cache_atomic``：多线程并发写同一缓存文件不互相截断、不留 tmp；
3. ``_exec`` 失败重试一轮：清掉 ``.failed`` 负缓存后重跑，成功则 ``retried=True``；
4. ``_etf_hist_targets``：ETF 日线目标 = 当日成交额 ≥ 阈值 ∪ pin 名单（代码归一）。

跑法：``python -m unittest stock.tests.test_prefetch_fixes -v``
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from stock import codes, prefetch
from stock.skilldata import datasource as ds


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


# ---------------------------------------------------------------- 盘中守卫
class TestIntradayBlock(_Base):
    """交易日 15:05 前应拒绝；收盘后 / 非交易日 / 显式回放 / 显式放行都放行。"""

    def _block(self, date_arg="", allow=False, dd="2026-11-17", now=None,
               exp_today="2026-11-17"):
        with mock.patch.object(prefetch, "_expected_trade_date", return_value=exp_today):
            return prefetch._intraday_block(date_arg, allow, dd, now=now)

    def test_blocks_before_close_on_trading_day(self):
        self.assertTrue(self._block(now=dt.datetime(2026, 11, 17, 0, 40)))
        self.assertTrue(self._block(now=dt.datetime(2026, 11, 17, 14, 59)))

    def test_allows_after_close(self):
        self.assertFalse(self._block(now=dt.datetime(2026, 11, 17, 15, 5)))
        self.assertFalse(self._block(now=dt.datetime(2026, 11, 17, 18, 40)))

    def test_allows_explicit_replay_and_override(self):
        self.assertFalse(self._block(date_arg="2026-11-16",
                                     now=dt.datetime(2026, 11, 17, 10, 0)))
        self.assertFalse(self._block(allow=True, now=dt.datetime(2026, 11, 17, 10, 0)))

    def test_allows_non_trading_day(self):
        # 周六：应有交易日是周五 → 不是交易日 → 不拦（写出的键也不与下一交易日冲突）
        self.assertFalse(self._block(dd="2026-11-21", exp_today="2026-11-20",
                                     now=dt.datetime(2026, 11, 21, 10, 0)))


# ---------------------------------------------------------------- 原子写
class TestWriteCacheAtomic(_Base):
    def test_concurrent_writes_leave_complete_csv_and_no_tmp(self):
        path = self.cache / "kind" / "key__2026-11-16.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        errors = []

        def writer(n):
            df = pd.DataFrame({"code": [f"{n}{i:05d}" for i in range(50)],
                               "value": range(50)})
            try:
                for _ in range(5):
                    ds._write_cache_atomic(df, str(path))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        back = pd.read_csv(path)
        self.assertEqual(len(back), 50)                 # 任何一次完整写入，绝不是半截
        self.assertEqual(list(back.columns), ["code", "value"])
        self.assertEqual(list(path.parent.glob("*.tmp")), [])


# ---------------------------------------------------------------- 重试一轮
class TestExecRetry(_Base):
    def test_failed_item_retried_once_and_failed_marker_cleared(self):
        # datasource 的负缓存约定：<缓存路径去掉 .csv>.failed（如 k__2026-11-16.failed）
        csv_path = self.cache / "fake" / "k__2026-11-16.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        marker = Path(str(csv_path)[:-4] + ".failed")
        marker.write_text("x", encoding="utf-8")
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("临时空表")
            return pd.DataFrame({"date": ["2026-11-16"], "close": [1.0]})

        item = prefetch._item("假条目", "fake", "k", flaky, required=False)
        with mock.patch.object(prefetch, "_cache_path", return_value=csv_path), \
             mock.patch.object(prefetch, "_force", return_value=False), \
             mock.patch.object(prefetch.time, "sleep", return_value=None):
            recs = prefetch._exec("fake", [item], 1, None)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec.get("retried"))
        self.assertNotIn("error", rec)
        self.assertEqual(calls["n"], 2)
        self.assertFalse(marker.exists(), "重试前应清掉 .failed 负缓存标记")


# ---------------------------------------------------------------- ETF 日线目标
class TestEtfHistTargets(_Base):
    def test_amount_threshold_union_pin(self):
        spot = pd.DataFrame({
            "代码": ["510300", "159915", "512999"],
            "成交额": [5.0e8, 1.0e8, 0.1e8],       # 元：两只 ≥ 0.3 亿，一只不够
        })
        got = set(prefetch._etf_hist_targets(spot))
        self.assertIn("510300", got)
        self.assertIn("159915", got)
        self.assertNotIn("512999", got)             # 不够阈值且不在 pin 名单时会被排除
        pin = {codes.norm_code(c) for c in prefetch.ETF_PIN}
        self.assertTrue(pin <= got, "pin 名单一个都不能少")
        self.assertIn("512100", got)                # pin 里的挂单 ETF 即使成交额不够也保留

    def test_accepts_amount_column_and_none(self):
        spot = pd.DataFrame({"code": ["510500"], "amount": [1.0e8]})
        self.assertIn("510500", set(prefetch._etf_hist_targets(spot)))
        self.assertEqual(set(prefetch._etf_hist_targets(None)),
                         {codes.norm_code(c) for c in prefetch.ETF_PIN})


# ---------------------------------------------------------------- bar 锚点 / 同日陈旧自愈
class TestAnchorBar(_Base):
    def test_anchor_is_000300_not_max(self):
        """其它指数的新 bar 不能掩盖 000300 的陈旧（2026-11-17 首跑事故）。"""
        recs = [
            {"kind": "index_daily", "key": "000905", "max_bar_date": "2026-11-17"},
            {"kind": "index_daily", "key": "000300", "max_bar_date": "2026-11-16"},
            {"kind": "etf_daily", "key": "sh510300", "max_bar_date": "2026-11-17"},
        ]
        self.assertEqual(prefetch._anchor_bar(recs), "2026-11-16")
        self.assertEqual(prefetch._anchor_bar([]), "")


class TestStaleBarRefresh(_Base):
    def _run(self, csv_path, loader):
        item = prefetch._item("指数日线 000300", "index_daily", "000300", loader, True)
        with mock.patch.object(prefetch, "_cache_path", return_value=csv_path), \
             mock.patch.object(prefetch, "_force", return_value=False), \
             mock.patch.object(prefetch.time, "sleep", return_value=None):
            return prefetch._exec("index", [item], 1, None, bar_expected="2026-11-17")

    def test_stale_same_day_cache_refetched(self):
        csv_path = self.cache / "index_daily" / "000300__2026-11-17.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"date": ["2026-11-16"], "close": [1.0]}).to_csv(csv_path, index=False)
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            if csv_path.is_file():
                return pd.read_csv(csv_path)          # 模拟 _cached 命中陈旧当日缓存
            return pd.DataFrame({"date": ["2026-11-17"], "close": [2.0]})

        rec = self._run(csv_path, loader)[0]
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec.get("stale_refreshed"))
        self.assertEqual(rec["max_bar_date"], "2026-11-17")
        self.assertEqual(calls["n"], 2)
        self.assertFalse(csv_path.exists(), "陈旧缓存应被删掉后重取")

    def test_fresh_cache_not_refetched(self):
        csv_path = self.cache / "index_daily" / "000300__2026-11-17.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"date": ["2026-11-17"], "close": [2.0]}).to_csv(csv_path, index=False)
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return pd.read_csv(csv_path)

        rec = self._run(csv_path, loader)[0]
        self.assertTrue(rec["ok"], rec)
        self.assertFalse(rec.get("stale_refreshed"))
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
