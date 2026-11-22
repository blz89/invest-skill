# -*- coding: utf-8 -*-
"""保留期清理的离线测试：只动临时目录，不碰真实数据。"""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from stock import config, retention


def _touch(p: Path, age_days: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def _age_dir(p: Path, age_days: float) -> None:
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        root = Path(self.td.name)
        self.d = {
            "cache": root / "cache",
            "invest": root / "invest",
            "out": root / "out",
            "logs": root / "logs",
            "archive": root / "archive",
        }
        patcher = mock.patch.multiple(
            config,
            CACHE_DIR=self.d["cache"], INVEST_DIR=self.d["invest"],
            OUT_DIR=self.d["out"], LOGS_DIR=self.d["logs"],
            ARCHIVE_DIR=self.d["archive"], RETENTION_DAYS=90,
        )
        patcher.start()
        self.addCleanup(self.td.cleanup)
        self.addCleanup(patcher.stop)

    def _build(self):
        _touch(self.d["cache"] / "daily" / "600519__2026-01-01.csv", 200)
        _touch(self.d["cache"] / "daily" / "600519__2026-11-14.csv", 1)
        _touch(self.d["out"] / "report_2026-01-01.html", 200)
        _touch(self.d["out"] / "report_2026-11-14.html", 1)
        _touch(self.d["out"] / "paper" / "report_2026-01-01.html", 400)   # paper 整棵保留
        _touch(self.d["invest"] / "cache" / "x.csv", 200)
        _touch(self.d["invest"] / "out" / "weekly_old.md", 200)
        _touch(self.d["invest"] / "out" / "paper" / "state_50k.json", 500)
        _touch(self.d["invest"] / "cash_rates_live.json", 400)            # 根目录状态文件不参与
        _touch(self.d["invest"] / "fuyao.key", 500)
        _touch(self.d["logs"] / "patrol_old.log", 200)
        _touch(self.d["logs"] / "latest.log", 0)
        _touch(self.d["archive"] / "2026-01-01" / "summary.json", 300)
        _age_dir(self.d["archive"] / "2026-01-01", 300)
        _touch(self.d["archive"] / "2026-11-14" / "summary.json", 1)

    def test_scan_finds_only_expired(self):
        self._build()
        plan = retention.scan(90)
        names = {p.name for p in plan["files"]}
        self.assertIn("600519__2026-01-01.csv", names)
        self.assertIn("report_2026-01-01.html", names)
        self.assertIn("weekly_old.md", names)
        self.assertIn("patrol_old.log", names)
        self.assertNotIn("600519__2026-11-14.csv", names)
        self.assertNotIn("report_2026-11-14.html", names)
        self.assertNotIn("latest.log", names)
        self.assertNotIn("state_50k.json", names)
        self.assertNotIn("fuyao.key", names)
        self.assertNotIn("cash_rates_live.json", names)
        self.assertEqual([p.name for p in plan["dirs"]], ["2026-01-01"])

    def test_dry_run_does_not_delete(self):
        self._build()
        r = retention.prune(90, dry=True)
        self.assertTrue(r["dry"])
        self.assertEqual(r["deleted_files"], 0)
        self.assertTrue((self.d["cache"] / "daily" / "600519__2026-01-01.csv").exists())
        self.assertGreaterEqual(r["planned_files"], 5)

    def test_prune_deletes_and_keeps_safe_paths(self):
        self._build()
        r = retention.prune(90)
        self.assertFalse(r["dry"])
        self.assertGreaterEqual(r["deleted_files"], 5)
        self.assertFalse((self.d["cache"] / "daily" / "600519__2026-01-01.csv").exists())
        self.assertFalse((self.d["archive"] / "2026-01-01").exists())
        # 这些必须还在
        self.assertTrue((self.d["cache"] / "daily" / "600519__2026-11-14.csv").exists())
        self.assertTrue((self.d["invest"] / "out" / "paper" / "state_50k.json").exists())
        self.assertTrue((self.d["out"] / "paper" / "report_2026-01-01.html").exists())
        self.assertTrue((self.d["invest"] / "fuyao.key").exists())
        self.assertTrue((self.d["invest"] / "cash_rates_live.json").exists())
        self.assertTrue((self.d["archive"] / "2026-11-14" / "summary.json").exists())

    def test_disabled_when_zero(self):
        self._build()
        r = retention.prune(0)
        self.assertIn("skipped", r)
        self.assertTrue((self.d["cache"] / "daily" / "600519__2026-01-01.csv").exists())


if __name__ == "__main__":
    unittest.main()
