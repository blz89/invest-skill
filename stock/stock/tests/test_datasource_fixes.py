# -*- coding: utf-8 -*-
"""数据层修复的离线自检（不联网、不碰生产缓存）。

覆盖 2026-11-16 两个生产事故对应的修复：
1. ``sina_symbol`` 按股票规则给 ETF 拼前缀 → 5xxxxx/1xxxxx 全被拼成 ``bj`` → 新浪 404；
2. ``fetch_etf_hist`` 入参不归一、且 19:00 拿不到当天 bar（B-7 事故）→
   用同源 ETF 行情表（腾讯 ``etf_spot_tx``）补最后一根，单位与新浪契约对齐；
3. ``_MARKET_HOOK``（invest 百度行业 → 同花顺一级行业）在**缓存回读路径**也生效
   （共享缓存下 invest 读别处写的 market_snapshot 时行业口径不能回归）。

跑法：``python -m unittest stock.tests.test_datasource_fixes -v``
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from stock import endpoints
from stock.skilldata import datasource as ds


class _Base(unittest.TestCase):
    """每个测试：临时 CACHE_DIR/OUT_DIR + 干净的进程内状态。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.cache_dir = root / "cache"
        self.out_dir = root / "out"
        self.cache_dir.mkdir()
        self.out_dir.mkdir()
        cfg = types.SimpleNamespace(
            CACHE_DIR=str(self.cache_dir), OUT_DIR=str(self.out_dir),
            FORCE_IPV4=False, PRICE_YEARS=4, EM_ENABLED=False,
        )
        ds.init(cfg, flavor="invest")
        ds.set_market_hook(None)
        ds.set_cache_hook(None)
        ds._SPOT_BAR_MEMO.clear()
        ds._prov.clear()
        # 兜底源默认不可用：单测一律不碰外网/同花顺（需要它的用例自己再 patch）
        self._ths = mock.patch.object(ds, "_ths_today_bar", return_value=None)
        self.ths = self._ths.start()
        self.addCleanup(self._ths.stop)
        env = mock.patch.dict(os.environ, {
            "A_SHARE_DATA_DATE": "", "A_SHARE_OFFLINE": "", "A_SHARE_FORCE_REFRESH": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        ds.set_market_hook(None)
        ds.set_cache_hook(None)
        ds._SPOT_BAR_MEMO.clear()
        ds._prov.clear()


def _hist(last_day="2026-11-12", n=3):
    """假的新浪 ETF 日线：最后 bar 停在 last_day。"""
    last = dt.date.fromisoformat(last_day)
    dates = [last - dt.timedelta(days=k) for k in range(n - 1, -1, -1)]
    return pd.DataFrame({
        "date": dates,
        "open": [1.0 + 0.01 * i for i in range(n)],
        "high": [1.10 + 0.01 * i for i in range(n)],
        "low": [0.90 + 0.01 * i for i in range(n)],
        "close": [1.00 + 0.01 * i for i in range(n)],
        "volume": [1000 + i for i in range(n)],
        "amount": [1e6 + i for i in range(n)],
    })


def _spot(quote_day="2026-11-13", hhmmss="161430", code="159845"):
    """假的腾讯 ETF 行情表（收盘后字段=当日最终值；volume 手、amount 元）。"""
    q = quote_day.replace("-", "") + hhmmss
    return pd.DataFrame({
        "code": [code], "name": ["测试ETF"],
        "price": [2.570], "prev_close": [2.647], "open": [2.627],
        "high": [2.630], "low": [2.560],
        "volume": [9095020.0], "amount": [2356004868.0],
        "aum_yi": [100.0], "turnover_pct": [5.0], "quote_time": [q],
    })


class TestSinaSymbol(_Base):
    """事故 ①：ETF 被按股票规则拼成 bj 前缀。"""

    CASES = {
        # 股票：口径必须保持不变
        "600519": "sh600519", "688981": "sh688981", "900901": "sh900901",
        "000001": "sz000001", "002594": "sz002594", "300750": "sz300750",
        "430047": "bj430047", "830799": "bj830799",
        # ETF：事故根因（原来全部返回 bjXXXXXX）
        "510300": "sh510300", "511360": "sh511360", "512100": "sh512100",
        "588000": "sh588000", "159845": "sz159845", "159915": "sz159915",
        "160632": "sz160632",
        # 已带前缀/后缀的写法也要能吃
        "sh510300": "sh510300", "159845.SZ": "sz159845", "SZ159915": "sz159915",
    }

    def test_prefixes(self):
        for raw, want in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(ds.sina_symbol(raw), want)

    def test_etf_codes_recognised(self):
        for c in ("510300", "511360", "588000", "159845", "159915", "160632"):
            with self.subTest(code=c):
                self.assertTrue(ds.is_etf_code(c))
        for c in ("600519", "000001", "430047"):
            with self.subTest(code=c):
                self.assertFalse(ds.is_etf_code(c))


class TestFuyaoThscode(unittest.TestCase):
    """同一类 bug（运维 实测）：fuyao.thscode 把 15/16 开头的深市 ETF 拼成 .SH
    → 同花顺 fund/market/historical 报 3001 Fund not found: 159845.SH。"""

    CASES = {
        "159845": "159845.SZ", "159915": "159915.SZ", "160632": "160632.SZ",
        "510300": "510300.SH", "511360": "511360.SH", "588000": "588000.SH",
        "600519": "600519.SH", "000001": "000001.SZ", "399001": "399001.SZ",
        "430047": "430047.BJ",
        "sh600519": "600519.SH", "600519.SH": "600519.SH", "SZ159915": "159915.SZ",
    }

    def test_market_suffix(self):
        from stock.sources import fuyao

        for raw, want in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(fuyao.thscode(raw), want)


class TestFetchEtfHistInput(_Base):
    """事故 ①：fetch_etf_hist 必须自己把裸代码/带前缀代码归一。"""

    def _fake_sina(self, seen):
        def fake_sina(symbol="", **kwargs):
            seen.append(symbol)
            return _hist(last_day="2026-11-13")
        return fake_sina

    def test_bare_code_normalised_and_cached(self):
        seen = []
        with mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=self._fake_sina(seen)), \
             mock.patch.object(ds, "_etf_spot_table", return_value=None):
            df = ds.fetch_etf_hist("159845")
            self.assertEqual(seen, ["sz159845"])
            self.assertEqual(len(df), 3)
            # 缓存 key 用归一后的 symbol（与历史缓存同名，不产生裸代码文件）
            want = self.cache_dir / "etf_daily" / f"sz159845__{ds.data_date()}.csv"
            self.assertTrue(want.exists(), f"缓存文件没写到 {want}")

            # 裸代码 / 带前缀 / 带后缀三种写法命中同一份缓存，不再打接口
            ds.fetch_etf_hist("sz159845")
            ds.fetch_etf_hist("159845.SZ")
            self.assertEqual(seen, ["sz159845"])

    def test_sh_etf(self):
        seen = []
        with mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=self._fake_sina(seen)), \
             mock.patch.object(ds, "_etf_spot_table", return_value=None):
            ds.fetch_etf_hist("510300")
            ds.fetch_etf_hist("sh510300")
        self.assertEqual(seen, ["sh510300"])

    def test_bad_code(self):
        with self.assertRaises(ValueError):
            ds.fetch_etf_hist("abcdef")


class TestTodayBarAppend(_Base):
    """事故 ②（B-7 事故）：19:00 新浪还没有当天 bar → 用行情表补。"""

    def test_appends_today_bar_with_sina_units(self):
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=lambda force=False: _spot()):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(df["date"].iloc[-1], pd.Timestamp("2026-11-13"))
        self.assertEqual(len(df), 4)                       # 3 根新浪 + 1 根补的
        row = df.iloc[-1]
        self.assertAlmostEqual(float(row["open"]), 2.627)
        self.assertAlmostEqual(float(row["high"]), 2.630)
        self.assertAlmostEqual(float(row["low"]), 2.560)
        self.assertAlmostEqual(float(row["close"]), 2.570)
        # 单位契约：volume 手×100 → 股；amount 已经是元
        self.assertAlmostEqual(float(row["volume"]), 9095020.0 * 100)
        self.assertAlmostEqual(float(row["amount"]), 2356004868.0)
        # 溯源里要能看出这根不是新浪日线给的；同时要有真实 max_bar_date 供上层判断
        srcs = [p.get("source") for p in ds._prov]
        self.assertIn("etf_spot_today_bar", srcs)
        self.assertEqual(ds._prov[-1]["source"], "fetch_etf_hist")
        self.assertEqual(ds._prov[-1]["max_bar_date"], "2026-11-13")

    def test_merged_bar_written_back_to_cache(self):
        """补出来的 bar 必须落盘：否则共享缓存读者/manifest 按 CSV 算 max_bar_date 会误报。"""
        day = "2026-11-13"
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": day}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=lambda force=False: _spot()), \
             mock.patch.object(ds, "_ths_today_bar",
                               side_effect=AssertionError("行情表 ready 时不该走同花顺")):
            ds.fetch_etf_hist("sz159845")

        cache = self.cache_dir / "etf_daily" / f"sz159845__{day}.csv"
        self.assertTrue(cache.exists())
        disk = pd.read_csv(cache)
        self.assertEqual(len(disk), 4)
        self.assertEqual(str(disk["date"].iloc[-1])[:10], "2026-11-13")
        # 合并后的缓存已经含当天 bar → 再读时连行情表都不该碰（数据日必须固定，否则会去取今天的新鲜数据）
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": day}), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=AssertionError("缓存已有当天 bar，不该读行情表")), \
             mock.patch.object(ds, "_now",
                               side_effect=AssertionError("已有当天 bar，不该看时钟")):
            again = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(again), 4)
        self.assertEqual(str(again["date"].iloc[-1])[:10], "2026-11-13")

    def test_stale_provenance_when_no_supplement(self):
        """行情表/同花顺都不可用：缓存末 bar 停在 T-1，但必须留 etf_hist_stale 信号。"""
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table", return_value=None):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 3)
        stale = [p for p in ds._prov if p.get("source") == "etf_hist_stale"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["max_bar_date"], "2026-11-12")
        self.assertEqual(stale[0]["data_date"], "2026-11-13")

    def test_ths_fallback_when_spot_unavailable(self):
        """行情表不可用 → 用同花顺补最后一根（THS 的 volume 已是股、amount 已是元，不再换算）。"""
        def fake_ths(code, day):
            return {"date": day, "open": 2.627, "high": 2.630, "low": 2.560,
                    "close": 2.570, "volume": 909501959.0, "amount": 2356004868.0,
                    "_source": "etf_ths_today_bar"}

        # 未 patch 全局兜底（局部 side_effect 覆盖 setUp 里的 return_value）
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table", return_value=None), \
             mock.patch.object(ds, "_ths_today_bar", side_effect=fake_ths):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 4)
        row = df.iloc[-1]
        self.assertEqual(str(row["date"])[:10], "2026-11-13")
        self.assertAlmostEqual(float(row["volume"]), 909501959.0)   # 股，不再 ×100
        self.assertAlmostEqual(float(row["amount"]), 2356004868.0)
        self.assertIn("etf_ths_today_bar", [p.get("source") for p in ds._prov])

    def test_em_branch_not_appended(self):
        """东财 qfq 分支（key=None）不补不复权 bar、也不写 etf_daily 缓存。"""
        out = ds._append_today_bar(_hist(last_day="2026-11-12"), "159845", None)
        self.assertEqual(len(out), 3)
        self.assertFalse((self.cache_dir / "etf_daily").exists())

    def test_idempotent_across_cache_hit(self):
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=lambda force=False: _spot()):
            d1 = ds.fetch_etf_hist("sz159845")     # 新取数 + 补 bar
            d2 = ds.fetch_etf_hist("sz159845")     # 缓存回读 + 再补一次
        self.assertEqual(len(d1), len(d2))
        self.assertEqual(d1["date"].tolist(), d2["date"].tolist())
        self.assertEqual(int((d2["date"] == pd.Timestamp("2026-11-13")).sum()), 1)

    def test_sina_already_has_today_no_append(self):
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-13")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=AssertionError("已有当天 bar 时不该读行情表")):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 3)
        self.assertEqual(df["date"].iloc[-1], pd.Timestamp("2026-11-13"))

    def test_suspended_etf_not_fabricated(self):
        """行情表里该 ETF 今天没成交（quote_time 还是昨天）→ 不补假 bar。"""
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=lambda force=False: _spot(quote_day="2026-11-12")):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 3)
        self.assertEqual(df["date"].iloc[-1], pd.Timestamp("2026-11-12"))

    def test_intraday_table_not_used(self):
        """行情表是盘中写的（quote_time 10:00）→ 不是最终值，不补。"""
        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=lambda force=False: _spot(hhmmss="100500")):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 3)

    def test_before_close_skipped(self):
        """数据日=今天、但还没收盘（10:00）→ 连行情表都不读。"""
        now = dt.datetime.combine(dt.date.today(), dt.time(10, 0))
        with mock.patch.object(ds, "_now", return_value=now), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=lambda **k: _hist(last_day="2026-11-12")), \
             mock.patch.object(ds, "_etf_spot_table",
                               side_effect=AssertionError("收盘前不该读行情表")):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(len(df), 3)

    def test_offline_replay_uses_cached_table_without_network(self):
        """决策日离线读快照：数据日=历史某天时也补，但绝不强制刷新（不联网）。"""
        calls = []

        def fake_cached(force=False):
            calls.append(force)
            return _spot()

        # 离线前提：当天（11-13）的 etf_daily 已由 19:00 数据更新落盘
        cache = self.cache_dir / "etf_daily" / "sz159845__2026-11-13.csv"
        cache.parent.mkdir(parents=True, exist_ok=True)
        _hist(last_day="2026-11-12").to_csv(cache, index=False, encoding="utf-8-sig")

        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-13",
                                          "A_SHARE_OFFLINE": "1"}), \
             mock.patch.object(endpoints, "fund_etf_hist_sina",
                               side_effect=AssertionError("离线模式不许联网")), \
             mock.patch.object(ds, "_etf_spot_tx_cached", side_effect=fake_cached):
            df = ds.fetch_etf_hist("sz159845")
        self.assertEqual(df["date"].iloc[-1], pd.Timestamp("2026-11-13"))
        self.assertEqual(calls, [False])          # 只读缓存，没有 force 重取


class TestMarketHook(_Base):
    """共享缓存兼容性：_MARKET_HOOK 必须在缓存回读路径也生效。"""

    def _patch_sector(self):
        def sector_spot(indicator=None):
            return pd.DataFrame({"板块": ["服装鞋类"], "label": ["new_fashion"]})

        def sector_detail(sector=None):
            return pd.DataFrame({
                "code": ["600519"], "name": ["贵州茅台"], "per": [20.0], "pb": [5.0],
                "mktcap": [1.0e8], "nmc": [8.0e7], "amount": [1.0e6],
            })

        return mock.patch.multiple(endpoints, stock_sector_spot=sector_spot,
                                   stock_sector_detail=sector_detail)

    @staticmethod
    def _hook(df):
        return df.assign(industry=["纺织服饰" for _ in range(len(df))])

    def test_hook_on_fresh_fetch_and_cache_hit(self):
        ds.set_market_hook(self._hook)
        with self._patch_sector():
            d1 = ds.fetch_market_snapshot()
        self.assertEqual(d1["industry"].tolist(), ["纺织服饰"])

        cache_file = (self.cache_dir / "market_snapshot"
                      / f"sina_industry__{ds.data_date()}.csv")
        self.assertTrue(cache_file.exists())
        raw = pd.read_csv(cache_file)
        # 缓存里保留原始（百度行业）口径，不把 invest 的策略口径写进共享缓存
        self.assertEqual(raw["industry"].tolist(), ["服装鞋类"])

        # 缓存命中：接口一律不许再打，但钩子仍要生效
        with mock.patch.object(endpoints, "stock_sector_spot",
                               side_effect=AssertionError("应走缓存")), \
             mock.patch.object(endpoints, "stock_sector_detail",
                               side_effect=AssertionError("应走缓存")):
            d2 = ds.fetch_market_snapshot()
        self.assertEqual(d2["industry"].tolist(), ["纺织服饰"])
        self.assertEqual(d2["code"].tolist(), ["600519"])

    def test_no_hook_keeps_raw_industry(self):
        """旧策略没注册钩子 → 行为不变（原始行业标签）。"""
        with self._patch_sector():
            d1 = ds.fetch_market_snapshot()          # 新取数
            d2 = ds.fetch_market_snapshot()          # 缓存回读
        self.assertEqual(d1["industry"].tolist(), ["服装鞋类"])
        self.assertEqual(d2["industry"].tolist(), ["服装鞋类"])

    def test_hook_is_idempotent(self):
        ds.set_market_hook(lambda df: df.assign(industry=df["industry"]))
        with self._patch_sector():
            d1 = ds.fetch_market_snapshot()
            d2 = ds.fetch_market_snapshot()
        self.assertEqual(d1["industry"].tolist(), d2["industry"].tolist())

    def test_hook_error_fails_loud(self):
        def boom(df):
            raise RuntimeError("行业归一表坏了")
        ds.set_market_hook(boom)
        with self._patch_sector(), self.assertRaises(RuntimeError):
            ds.fetch_market_snapshot()


class TestCachedForce(_Base):
    def test_force_ignores_cache(self):
        calls = []

        def loader():
            calls.append(1)
            return pd.DataFrame({"x": [len(calls)]})

        with mock.patch.object(ds, "_cache_file",
                               side_effect=lambda kind, key: str(self.cache_dir / "probe.csv")):
            ds._cached("probe", "k", loader)
            ds._cached("probe", "k", loader)               # 命中缓存
            ds._cached("probe", "k", loader, force=True)   # 强制刷新
        self.assertEqual(len(calls), 2)


class TestMoneyFundLsjz(_Base):
    """货基历史快通道（Lead m00455）：参数不全 / pageSize>20 时东财静默回空，快通道从未成功过。"""

    @staticmethod
    def _resp(lst):
        r = mock.Mock()
        r.raise_for_status = mock.Mock()
        r.json.return_value = {"Data": {"LSJZList": lst}}
        return r

    def test_params_complete_and_pagesize_clamped(self):
        row = {"FSRQ": "2026-11-14", "DWJZ": "0.5", "LJJZ": "1.2",
               "SGZT": "开放", "SHZT": "开放"}
        with mock.patch("requests.get", return_value=self._resp([row])) as g:
            df = ds._em_fund_lsjz("000198", page_size=250)
        self.assertEqual(len(df), 1)
        params = g.call_args.kwargs["params"]
        self.assertEqual(params["fundCode"], "000198")
        self.assertEqual(params["pageIndex"], 1)
        self.assertEqual(params["pageSize"], 20)          # 250 → clamp 20
        self.assertEqual(params["startDate"], "")          # 缺这三个参数服务端静默回空
        self.assertEqual(params["endDate"], "")
        self.assertTrue(params["_"].isdigit() and len(params["_"]) == 13)

    def test_later_empty_page_returns_empty_not_raise(self):
        """翻到末尾（第 2 页起 Data:null）是正常终止，不当失败。"""
        with mock.patch("requests.get",
                        return_value=mock.Mock(raise_for_status=mock.Mock(),
                                               json=lambda: {"Data": None})):
            df = ds._em_fund_lsjz("000198", page_index=2)
        self.assertTrue(df.empty)

    def test_window_stops_paging(self):
        def fake_lsjz(code, page_size=20, page_index=1, start_date="", end_date=""):
            base = dt.datetime(2026, 11, 16) - dt.timedelta(days=(page_index - 1) * 20)
            return pd.DataFrame({
                "date": [(base - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(20)],
                "wanfen": [0.5] * 20, "y7_pct": [1.2] * 20,
                "buy": ["开放"] * 20, "sell": ["开放"] * 20,
            })

        with mock.patch.dict(os.environ, {"A_SHARE_DATA_DATE": "2026-11-16"}), \
             mock.patch.object(ds, "_em_fund_lsjz", side_effect=fake_lsjz) as m:
            df = ds.fetch_money_fund_hist("000198", days=400)
        self.assertEqual(m.call_count, 20)                 # 400/20 行停翻，不是 241 页
        self.assertEqual(len(df), 400)
        self.assertEqual(m.call_args_list[0].kwargs["start_date"], "2025-10-12")

    def test_full_history_stops_on_short_page(self):
        pages = {1: 20, 2: 20, 3: 5}

        def fake_lsjz(code, page_size=20, page_index=1, start_date="", end_date=""):
            n = pages.get(page_index, 0)
            if not n:
                return pd.DataFrame()
            base = dt.datetime(2026, 11, 16) - dt.timedelta(days=(page_index - 1) * 20)
            return pd.DataFrame({
                "date": [(base - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)],
                "wanfen": [0.5] * n, "y7_pct": [1.2] * n,
                "buy": ["开放"] * n, "sell": ["开放"] * n,
            })

        with mock.patch.object(ds, "_em_fund_lsjz", side_effect=fake_lsjz) as m:
            df = ds.fetch_money_fund_hist("000198")
        self.assertEqual(m.call_count, 3)
        self.assertEqual(len(df), 45)
        self.assertEqual(m.call_args_list[0].kwargs["start_date"], "")   # 全历史不带窗口

    def test_fallback_to_slow_path(self):
        raw = pd.DataFrame({
            "净值日期": ["2026-11-14"], "每万份收益": ["0.5"], "7日年化收益率": ["1.2"],
            "申购状态": ["开放"], "赎回状态": ["开放"],
        })
        with mock.patch.object(ds, "_em_fund_lsjz", side_effect=RuntimeError("boom")), \
             mock.patch.object(endpoints, "fund_money_fund_info_em", return_value=raw) as slow:
            df = ds.fetch_money_fund_hist("000198")
        self.assertTrue(slow.called)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["wanfen"].iloc[0]), 0.5)


class TestFundScaleParse(_Base):
    """蛋卷规模文本单位（亿/万）+ 单只失效代码不再拖垮整批。"""

    def setUp(self):
        super().setUp()
        ds._FUND_SCALE_XQ_FAILS = 0
        ds._FUND_SCALE_XQ_COOLDOWN = 0.0
        ds._FUND_SCALE_XQ_BROKEN = False

    def test_units(self):
        cases = {"11.72亿": 11.72, "6584.51万": 0.658451, "329.25万": 0.032925,
                 "1,234.5亿": 1234.5, "118.89亿": 118.89, "—": None, "": None}
        for raw, want in cases.items():
            got = ds._scale_yi(raw)
            if want is None:
                self.assertIsNone(got, raw)
            else:
                self.assertAlmostEqual(got, want, places=6, msg=raw)

    def test_wan_unit_and_single_failure_not_poison(self):
        good = pd.DataFrame({"item": ["基金代码", "最新规模"],
                             "value": ["005025", "6584.51万"]})

        def fake(symbol="", timeout=None):
            if symbol == "999999":
                raise KeyError("data")
            return good

        with mock.patch.object(endpoints, "fund_individual_basic_info_xq", side_effect=fake), \
             mock.patch.object(ds, "fetch_etf_scale_map", return_value={}):
            self.assertIsNone(ds.fetch_fund_scale("999999"))
            self.assertAlmostEqual(ds.fetch_fund_scale("005025"), 0.658451, places=6)

    def test_em_fallback_when_xq_has_no_fund(self):
        """蛋卷没有这只基金（ValueError）→ 东财基金规模兜底，且不算接口故障。"""
        em = pd.DataFrame({"基金代码": ["018138"], "基金简称": ["x"],
                           "最新规模(元)": [57108735.95], "规模日期": ["2026-11-19"]})
        with mock.patch.object(endpoints, "fund_individual_basic_info_xq",
                               side_effect=ValueError("蛋卷基金档案无数据：018138")), \
             mock.patch.object(endpoints, "fund_scale_em", return_value=em) as em_mock, \
             mock.patch.object(ds, "fetch_etf_scale_map", return_value={}):
            v = ds.fetch_fund_scale("018138")
        self.assertTrue(em_mock.called)
        self.assertAlmostEqual(v, 0.5711, places=4)
        self.assertEqual(ds.fund_scale_source(), "em")
        self.assertEqual(ds._FUND_SCALE_XQ_FAILS, 0)   # 缺基金不算接口失败

    def test_em_fallback_after_xq_error(self):
        """蛋卷被判风控（普通异常）→ 仍能用东财兜底出数。"""
        em = pd.DataFrame({"基金代码": ["000198"], "基金简称": ["x"],
                           "最新规模(元)": [645136387527.52], "规模日期": ["2026-11-19"]})
        with mock.patch.object(endpoints, "fund_individual_basic_info_xq",
                               side_effect=RuntimeError("blocked")), \
             mock.patch.object(endpoints, "fund_scale_em", return_value=em), \
             mock.patch.object(ds, "fetch_etf_scale_map", return_value={}):
            v = ds.fetch_fund_scale("000198")
        self.assertAlmostEqual(v, 6451.3639, places=3)
        self.assertEqual(ds.fund_scale_source(), "em")



class TestDelistedBasic(_Base):
    """历史退市 / 基础信息 / ST 区间（baostock 口径的解析层，不联网）。"""

    def test_bs_symbol(self):
        self.assertEqual(ds._bs_symbol("600000"), "sh.600000")
        self.assertEqual(ds._bs_symbol("000004"), "sz.000004")
        self.assertEqual(ds._bs_symbol("300750"), "sz.300750")
        self.assertEqual(ds._bs_symbol("sh.601058"), "sh.601058")
        with self.assertRaises(ValueError):
            ds._bs_symbol("430047")  # 北交所 baostock 不支持

    def test_basic_frame_and_norm(self):
        rows = [
            ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
            ["sz.000004", "国华退", "1991-01-14", "2026-07-14", "1", "0"],
            ["sh.000300", "沪深300", "2005-04-08", "", "2", "1"],
        ]
        df = ds._norm_basic(ds._basic_frame(rows))
        self.assertEqual(list(df.columns), ds._STOCK_BASIC_COLS)
        self.assertEqual(list(df["代码"]), ["600000", "000004", "000300"])
        self.assertEqual(df.loc[1, "状态"], "退市")
        self.assertEqual(df.loc[1, "类型"], "股票")
        self.assertEqual(df.loc[2, "类型"], "指数")

    def test_basic_frame_empty_raises(self):
        with self.assertRaises(RuntimeError):
            ds._basic_frame([])

    def test_st_spans_merge(self):
        rows = [
            ["2026-04-01", "0"], ["2026-04-02", "1"], ["2026-04-03", "1"],
            ["2026-04-06", "1"], ["2026-04-07", "0"], ["2026-05-06", "1"],
            ["2026-05-07", "0"], ["2026-05-08", "0"],
        ]
        df = ds._st_spans(rows)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0].to_dict(), {"起始日": "2026-04-02", "结束日": "2026-04-06"})
        self.assertEqual(df.iloc[1].to_dict(), {"起始日": "2026-05-06", "结束日": "2026-05-06"})

    def test_st_spans_open_tail_and_empty(self):
        df = ds._st_spans([["2026-05-06", "1"], ["2026-05-07", "1"]])
        self.assertEqual(df.iloc[0].to_dict(), {"起始日": "2026-05-06", "结束日": "2026-05-07"})
        self.assertEqual(len(ds._st_spans([])), 0)


class TestDividends(_Base):
    """分红送配：缓存 CSV 回读不能把 000999 读成 999；空表不炸。"""

    def test_code_zero_padded_after_cache_roundtrip(self):
        frame = pd.DataFrame({
            "代码": ["000999", "000999"],
            "名称": ["华润三九", "华润三九"],
            "公告日期": ["2026-10-09", "2026-05-21"],
            "股权登记日": ["2026-10-15", "2026-05-28"],
            "除权除息日": ["2026-10-16", "2026-05-29"],
            "每10股派息": [4.15, 5.90],
            "每10股送转": [0.0, 0.0],
            "方案": ["10派4.15元(含税,扣税后3.735元)", "10派5.90元(含税,扣税后5.31元)"],
            "进度": ["实施分配", "实施分配"],
        })
        with mock.patch.object(endpoints, "stock_dividend_em", return_value=frame) as mk:
            first = ds.fetch_dividends("000999", force=True)
            self.assertEqual(mk.call_count, 1)
            second = ds.fetch_dividends("000999")          # 走 CSV 缓存回读
            self.assertEqual(mk.call_count, 1)
        for df in (first, second):
            self.assertEqual(str(df.iloc[0]["代码"]), "000999")
            self.assertEqual(len(df), 2)

    def test_empty_frame(self):
        empty = pd.DataFrame(columns=["代码", "名称", "公告日期", "股权登记日",
                                      "除权除息日", "每10股派息", "每10股送转", "方案", "进度"])
        with mock.patch.object(endpoints, "stock_dividend_em", return_value=empty):
            df = ds.fetch_dividends("000001", force=True)
        self.assertEqual(len(df), 0)


class TestFinStatements(_Base):
    """财报三表 / 业绩预告：证券代码后缀与返回列契约。"""

    def test_secucode_mapping(self):
        from stock.endpoints import em
        self.assertEqual(em._secucode("000999"), "000999.SZ")
        self.assertEqual(em._secucode("600999"), "600999.SH")
        self.assertEqual(em._secucode("688387"), "688387.SH")
        self.assertEqual(em._secucode("830799"), "830799.BJ")
        self.assertEqual(em._secucode("430047"), "430047.BJ")

    def test_forecast_columns(self):
        from stock.endpoints import em
        self.assertEqual(len(em.FORECAST_COLS), 12)
        self.assertIn("预告类型", em.FORECAST_COLS)
        self.assertIn("公告日", em.FORECAST_COLS)


class TestFinQualityYoY(_Base):
    """``fin_quality`` 的「同比」必须对上年同期，不能拿上一报告期（环比）。"""

    @staticmethod
    def _bal():
        return pd.DataFrame({
            "REPORT_DATE": ["2026-09-30", "2026-06-30", "2025-09-30"],
            "NOTICE_DATE": ["2026-10-24", "2026-08-20", "2025-10-25"],
            "ACCOUNTS_RECE": [110.0, 200.0, 100.0],
            "INVENTORY": [55.0, 50.0, 50.0],
            "SHORT_LOAN": [12.0, 10.0, 10.0],
            "LONG_LOAN": [0.0, 0.0, 0.0],
        })

    def test_yoy_uses_same_period_last_year(self):
        bal = self._bal()
        inc = pd.DataFrame({"REPORT_DATE": ["2026-09-30"], "PARENT_NETPROFIT": [10.0]})
        cfl = pd.DataFrame({"REPORT_DATE": ["2026-09-30"], "NETCASH_OPERATE": [12.0]})
        with mock.patch.object(ds, "fetch_fin_balance", return_value=bal), \
                mock.patch.object(ds, "fetch_fin_income", return_value=inc), \
                mock.patch.object(ds, "fetch_fin_cashflow", return_value=cfl):
            q = ds.fin_quality("000999")
        self.assertEqual(q["report"], "2026-09-30")
        # vs 2025-09-30（100）；若错用了 2026-06-30 的 200 会得到 -0.45
        self.assertAlmostEqual(q["receivable_yoy"], 0.10)
        self.assertAlmostEqual(q["inventory_yoy"], 0.10)
        self.assertAlmostEqual(q["debt_yoy"], 0.20)
        self.assertAlmostEqual(q["cfo_to_np"], 1.2)

    def test_missing_prior_year_returns_none(self):
        bal = self._bal().iloc[[0, 1]].reset_index(drop=True)   # 只有本期与上一报告期
        with mock.patch.object(ds, "fetch_fin_balance", return_value=bal), \
                mock.patch.object(ds, "fetch_fin_income", return_value=pd.DataFrame()), \
                mock.patch.object(ds, "fetch_fin_cashflow", return_value=pd.DataFrame()):
            q = ds.fin_quality("000999")
        self.assertIsNone(q["receivable_yoy"])
        self.assertIsNone(q["debt_yoy"])


class TestAnnouncementEmptyCache(_Base):
    """非交易日的空表缓存 = 已确认当天没有公告，不能重新联网取。"""

    def test_empty_cache_does_not_refetch(self):
        root = self.cache_dir / "cninfo_market"
        root.mkdir()
        (root / "2026-11-15.csv").write_text(
            "代码,名称,公告标题,公告类型,公告日期,网址\n", encoding="utf-8")
        with mock.patch.object(endpoints, "stock_notice_report",
                               side_effect=AssertionError("空表缓存命中时不应联网")):
            df = ds.fetch_market_announcements("2026-11-15", "2026-11-15")
        self.assertEqual(len(df), 0)
        self.assertTrue((self.cache_dir / "cninfo_market" / "2026-11-15.csv").exists())


class TestTradingDay(_Base):
    """交易日历 helper：周六周日永远不是交易日（调休补班也不开市）。"""

    def test_is_trading_day(self):
        cal = pd.DataFrame({"trade_date": ["2026-11-19", "2026-11-20", "2026-11-23"]})
        with mock.patch.object(ds, "fetch_trade_calendar", return_value=cal):
            self.assertTrue(ds.is_trading_day("2026-11-20"))
            self.assertFalse(ds.is_trading_day("2026-11-21"))   # 周六
            self.assertFalse(ds.is_trading_day("2026-11-22"))   # 周日
            self.assertEqual(ds.last_trading_day("2026-11-22"), "2026-11-20")
            self.assertEqual(ds.last_trading_day("2026-11-18"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
