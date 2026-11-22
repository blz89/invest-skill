# -*- coding: utf-8 -*-
"""跨日增量复用（fin / fund_nav）离线自检：不联网、不碰生产缓存。

覆盖 2026-11-17 起的提速改造：
1. ``_prev_cache_file`` 只认「日期 < 数据日、且不超龄」的上一份缓存；
2. ``_fin_incremental`` = 缓存里往年行 + 新浪「本年/上年」两页，列集/列序/日期必须对齐，
   任何不一致都回退全量；
3. ``_nav_incremental`` 从 bulk 快照表追加新发布的 1 行；缓存已最新则原样复用；
   交易日历不连续/缺基金 → 回退全量；
4. ``STOCK_FIN_INCREMENTAL=0`` / ``STOCK_NAV_INCREMENTAL=0`` 一行回滚到全量。

跑法：``python -m unittest stock.tests.test_incremental -v``
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from stock.skilldata import datasource as ds


def _cfg(cache_dir: Path, out_dir: Path):
    return types.SimpleNamespace(
        CACHE_DIR=str(cache_dir), OUT_DIR=str(out_dir),
        PRICE_YEARS=4, FIN_START_YEAR="2019", VALUATION_PERIOD="近五年",
        EM_ENABLED=False, SCALE_CACHE_MAX_TRADE_DAYS=5,
        SCALE_CACHE_FILENAME="etf_scale_lastgood.json",
        TX_QUOTE_BATCH=60, TX_QUOTE_SLEEP=0.25, FORCE_IPV4=True,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.cache = root / "cache"
        self.out = root / "out"
        (self.cache / "fin").mkdir(parents=True)
        (self.cache / "fund_nav").mkdir(parents=True)
        self.out.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "A_SHARE_DATA_DATE": "2026-11-17", "A_SHARE_OFFLINE": "",
            "A_SHARE_FORCE_REFRESH": "", "STOCK_FIN_INCREMENTAL": "",
            "STOCK_NAV_INCREMENTAL": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ds.init(_cfg(self.cache, self.out), flavor="shared")
        ds.set_cache_hook(None)
        self.addCleanup(lambda: ds.set_cache_hook(None))


class TestPrevCacheFile(_Base):
    def _touch(self, kind: str, name: str):
        (self.cache / kind / name).write_text("a\n1\n", encoding="utf-8")

    def test_picks_latest_before_today(self):
        self._touch("fin", "000001__2019__2026-11-14.csv")
        self._touch("fin", "000001__2019__2026-11-16.csv")
        self._touch("fin", "000001__2019__2026-11-17.csv")   # 今天 → 不能当基线
        got = ds._prev_cache_file("fin", "000001__2019")
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("__2026-11-16.csv"))

    def test_ignores_expired_and_missing(self):
        self._touch("fin", "000001__2019__2026-10-01.csv")   # 超 8 天 → 忽略
        self.assertIsNone(ds._prev_cache_file("fin", "000001__2019"))
        self._touch("fin", "000002__2019__2026-11-16.csv")
        self.assertIsNone(ds._prev_cache_file("fin", "999999__2019"))

    def test_cache_hook_disables_reuse(self):
        self._touch("fin", "000001__2019__2026-11-16.csv")
        ds.set_cache_hook(lambda kind, key: str(self.cache / kind / "x.csv"))
        self.assertIsNone(ds._prev_cache_file("fin", "000001__2019"))


def _sina_pages(_rows: dict[str, list[tuple[str, float, float]]]):
    """mock 新浪「按年份抓页」：返回 index=报告期字符串、列=指标 的原始表。"""
    def fake(symbol, years):
        frames = []
        for y in years:
            rows = _rows.get(str(y))
            if not rows:
                continue
            frames.append(pd.DataFrame(
                {"指标A": [r[1] for r in rows], "指标B": [r[2] for r in rows]},
                index=[r[0] for r in rows]))
        return pd.concat(frames) if frames else pd.DataFrame()
    return fake


class TestFinIncremental(_Base):
    def _write_base(self, code: str = "000002", day: str = "2026-11-16"):
        path = self.cache / "fin" / f"{code}__2019__{day}.csv"
        pd.DataFrame({
            "日期": ["2023-12-31", "2024-12-31", "2025-03-31"],
            "指标A": [1.0, 2.0, 3.0],
            "指标B": [4.0, 5.0, 6.0],
        }).to_csv(path, index=False)
        return path

    def test_merges_old_rows_and_new_pages(self):
        self._write_base()
        pages = {"2025": [("2025-03-31", 3.5, 6.5), ("2025-06-30", 4.0, 7.0)],
                 "2026": [("2026-03-31", 5.0, 8.0)]}
        with mock.patch("stock.endpoints.sina.stock_financial_analysis_indicator_years",
                        side_effect=_sina_pages(pages)):
            out = ds._fin_incremental("000002", "2019")
        self.assertIsNotNone(out)
        self.assertEqual(list(out.columns), ["日期", "指标A", "指标B"])
        self.assertEqual(list(out["日期"].dt.strftime("%Y-%m-%d")),
                         ["2023-12-31", "2024-12-31", "2025-03-31", "2025-06-30", "2026-03-31"])
        # 新页覆盖缓存里 >= 上年的行（2025-03-31 被重取，值以新页为准）
        self.assertEqual(list(out["指标A"]), [1.0, 2.0, 3.5, 4.0, 5.0])

    def test_fallback_on_column_mismatch(self):
        path = self.cache / "fin" / "000002__2019__2026-11-16.csv"
        pd.DataFrame({"日期": ["2024-12-31"], "别的列": [1.0]}).to_csv(path, index=False)
        with mock.patch("stock.endpoints.sina.stock_financial_analysis_indicator_years",
                        side_effect=_sina_pages({"2025": [("2025-03-31", 1.0, 2.0)]})):
            self.assertIsNone(ds._fin_incremental("000002", "2019"))

    def test_fallback_when_no_base(self):
        with mock.patch("stock.endpoints.sina.stock_financial_analysis_indicator_years") as f:
            self.assertIsNone(ds._fin_incremental("000777", "2019"))
            f.assert_not_called()

    def test_fetch_fin_uses_incremental(self):
        self._write_base()
        pages = {"2025": [("2025-06-30", 4.0, 7.0)],
                 "2026": [("2026-03-31", 5.0, 8.0)]}
        with mock.patch("stock.endpoints.sina.stock_financial_analysis_indicator_years",
                        side_effect=_sina_pages(pages)), \
                mock.patch("stock.endpoints.stock_financial_analysis_indicator") as full:
            out = ds.fetch_fin("000002")
            full.assert_not_called()
        self.assertEqual(len(out), 4)
        self.assertEqual(ds.fetch_stats()["fin_incremental"], 1)

    def test_kill_switch_forces_full(self):
        self._write_base()
        os.environ["STOCK_FIN_INCREMENTAL"] = "0"
        full = pd.DataFrame({"日期": ["2024-12-31"], "指标A": [9.0], "指标B": [9.0]})
        with mock.patch("stock.endpoints.stock_financial_analysis_indicator",
                        return_value=full) as f:
            out = ds.fetch_fin("000002")
            f.assert_called_once()
        self.assertEqual(ds.fetch_stats()["fin_full"], 1)
        self.assertEqual(len(out), 1)


class TestNavIncremental(_Base):
    def _base_hist(self, code: str = "000015", day: str = "2026-11-16"):
        path = self.cache / "fund_nav" / f"{code}__累计净值走势__{day}.csv"
        pd.DataFrame({"date": ["2026-11-13", "2026-11-16"], "value": [1.10, 1.20]}
                     ).to_csv(path, index=False)
        return path

    def _bulk(self, code="000015", nav_date="2026-11-17", acc=1.30,
              prev_date="2026-11-16", prev_acc=1.20):
        return pd.DataFrame({"code": [code], "name": ["测试基金"],
                             "nav_date": [nav_date], "unit_nav": [1.30],
                             "acc_nav": [acc], "prev_unit_nav": [1.20],
                             "prev_acc_nav": [prev_acc], "prev_nav_date": [prev_date],
                             "growth_pct": [1.0], "fee_pct": [0.1]})

    def _cal(self):
        return pd.DataFrame({"trade_date": ["2026-11-13", "2026-11-14", "2026-11-16",
                                           "2026-11-17", "2026-11-18"]})

    def test_append_one_row(self):
        self._base_hist()
        os.environ["A_SHARE_DATA_DATE"] = "2026-11-18"
        with mock.patch.object(ds, "fetch_fund_nav_daily", return_value=self._bulk()), \
                mock.patch.object(ds, "fetch_trade_calendar", return_value=self._cal()):
            mode, out = ds._nav_incremental("000015", "累计净值走势")
        self.assertEqual(mode, "append")
        self.assertEqual(list(out["date"].dt.strftime("%Y-%m-%d")),
                         ["2026-11-13", "2026-11-16", "2026-11-17"])
        self.assertEqual(list(out["value"]), [1.10, 1.20, 1.30])

    def test_reuse_when_cache_is_latest(self):
        self._base_hist()
        os.environ["A_SHARE_DATA_DATE"] = "2026-11-17"
        bulk = self._bulk(nav_date="2026-11-17", acc=float("nan"),
                          prev_date="2026-11-16", prev_acc=1.20)
        with mock.patch.object(ds, "fetch_fund_nav_daily", return_value=bulk), \
                mock.patch.object(ds, "fetch_trade_calendar", return_value=self._cal()):
            mode, out = ds._nav_incremental("000015", "累计净值走势")
        self.assertEqual(mode, "reuse")
        self.assertEqual(len(out), 2)

    def test_fallback_on_calendar_gap(self):
        # base 停在 11-13，bulk 只给 11-16/11-17 两行 → 交易日历里 11-16 缺 → 全量
        path = self.cache / "fund_nav" / "000015__累计净值走势__2026-11-16.csv"
        pd.DataFrame({"date": ["2026-11-13"], "value": [1.10]}).to_csv(path, index=False)
        os.environ["A_SHARE_DATA_DATE"] = "2026-11-18"
        with mock.patch.object(ds, "fetch_fund_nav_daily", return_value=self._bulk()), \
                mock.patch.object(ds, "fetch_trade_calendar", return_value=self._cal()):
            mode, out = ds._nav_incremental("000015", "累计净值走势")
        self.assertEqual(mode, "fallback")
        self.assertIsNone(out)

    def test_fetch_history_writes_appended_cache(self):
        self._base_hist()
        os.environ["A_SHARE_DATA_DATE"] = "2026-11-18"
        with mock.patch.object(ds, "fetch_fund_nav_daily", return_value=self._bulk()), \
                mock.patch.object(ds, "fetch_trade_calendar", return_value=self._cal()):
            out = ds.fetch_fund_nav_history("000015")
        self.assertEqual(len(out), 3)
        self.assertEqual(ds.fetch_stats()["nav_append"], 1)
        cached = list((self.cache / "fund_nav").glob("*__2026-11-18.csv"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(len(pd.read_csv(cached[0])), 3)

    def test_kill_switch_forces_full(self):
        os.environ["STOCK_NAV_INCREMENTAL"] = "0"
        raw = pd.DataFrame({"净值日期": ["2026-11-16", "2026-11-17"],
                            "累计净值": [1.20, 1.30]})
        with mock.patch("stock.endpoints.fund_open_fund_info_em", return_value=raw) as f, \
                mock.patch.object(ds, "fetch_fund_nav_daily") as bulk:
            out = ds.fetch_fund_nav_history("000015")
            f.assert_called_once()
            bulk.assert_not_called()
        self.assertEqual(ds.fetch_stats()["nav_full"], 1)
        self.assertEqual(len(out), 2)


class TestFundNavDailyPrevDate(_Base):
    def _seed_bulk_cache(self, with_prev_date: bool):
        path = self.cache / "fund_nav_daily" / "all__2026-11-17.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"code": ["000015"], "name": ["测试基金"], "nav_date": ["2026-11-17"],
               "unit_nav": [float("nan")], "acc_nav": [float("nan")],
               "prev_unit_nav": [1.11], "prev_acc_nav": [1.16],
               "growth_pct": [0.5], "fee_pct": [0.15]}
        if with_prev_date:
            row["prev_nav_date"] = ["2026-11-13"]
        pd.DataFrame(row).to_csv(path, index=False)
        return path

    def test_cache_with_prev_nav_date_kept(self):
        self._seed_bulk_cache(True)
        out = ds.fetch_fund_nav_daily()
        self.assertIn("prev_nav_date", out.columns)
        self.assertEqual(str(out["prev_nav_date"].iloc[0]), "2026-11-13")

    def test_old_cache_without_prev_nav_date_gets_blank(self):
        self._seed_bulk_cache(False)
        out = ds.fetch_fund_nav_daily()
        self.assertIn("prev_nav_date", out.columns)
        self.assertEqual(str(out["prev_nav_date"].iloc[0]), "")


class TestPrefetchFetchModeText(unittest.TestCase):
    def test_renders_counters(self):
        from stock import prefetch as pf
        txt = pf._fetch_mode_txt({"fin_incremental": 8, "fin_full": 2, "fin_incr_fallback": 0,
                                  "nav_append": 3, "nav_reuse": 5, "nav_full": 0,
                                  "nav_incr_fallback": 0})
        self.assertEqual(txt, "fin 增量 8 / 全量 2 / 回退 0；"
                              "nav 追加 3 / 复用 5 / 全量 0 / 回退 0")

    def test_empty_stats(self):
        from stock import prefetch as pf
        self.assertEqual(pf._fetch_mode_txt(None), "n/a")


if __name__ == "__main__":
    unittest.main()
