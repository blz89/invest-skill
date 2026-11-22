# -*- coding: utf-8 -*-
"""stock 全局配置：路径、缓存、限流、凭据来源。

原则
----
1. **一处配置**：所有路径/超时/限流都从这里读，其它模块不写死盘符。
2. **可用环境变量覆盖**：`STOCK_HOME` 换根目录，`STOCK_CACHE_DIR` 换缓存盘，
   `STOCK_REFRESH=1` 强制刷新（跳过当日缓存）。
3. **凭据不进代码**：同花顺 key 走环境变量或凭据文件（见 sources/fuyao.py 文档）。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 目录
ROOT = Path(os.environ.get("STOCK_HOME") or Path(__file__).resolve().parents[1]).resolve()
DATA_DIR = ROOT / "data"
CACHE_DIR = Path(os.environ.get("STOCK_CACHE_DIR") or (DATA_DIR / "cache"))
OUT_DIR = Path(os.environ.get("STOCK_OUT_DIR") or (DATA_DIR / "out"))
WORK_DIR = Path(os.environ.get("STOCK_WORK_DIR") or DATA_DIR)   # 工作目录（部分脚本按 WORK_DIR 找状态文件）

# ---------------------------------------------------------------- 统一运行数据
# 所有「跑出来的数据」都落 ROOT/data 下，不再分散在 ~/.dsh/work/<skill>：
#   data\invest  投资技能的 cache/out/paper
#   data\logs    每日巡检日志（原 _ops\logs）
#   data\archive 每日归档（原 _ops\archive）
# 账本/持仓状态（paper\state_*.json、*.key、*_latest.json）不参与清理。
INVEST_DIR = Path(os.environ.get("STOCK_INVEST_DIR") or (DATA_DIR / "invest"))
LOGS_DIR = Path(os.environ.get("STOCK_LOGS_DIR") or (DATA_DIR / "logs"))
ARCHIVE_DIR = Path(os.environ.get("STOCK_ARCHIVE_DIR") or (DATA_DIR / "archive"))

# ---------------------------------------------------------------- 共享数据（统一取数）
# 2026-11-16 起：所有技能共用 stock 的**同一份**取数结果，不再各取一遍：
#   data\shared\cache\<kind>\<key>__<date>.csv   ← 数据层缓存布局（datasource._cache_file 原样）
#   data\shared\snapshots\<date>\manifest.json   ← 每次全量取数的统一记录（覆盖了哪些 kind/key）
#
# 技能的 CACHE_DIR（data\invest\cache）用 Windows 目录 junction 指到
# SHARED_CACHE_DIR（见 scripts/setup_shared_cache.ps1），技能代码零改动：
# config.CACHE_DIR 仍然有效，只是落盘位置统一了。
SHARED_DIR = Path(os.environ.get("STOCK_SHARED_DIR") or (DATA_DIR / "shared"))
SHARED_CACHE_DIR = Path(os.environ.get("STOCK_SHARED_CACHE_DIR") or (SHARED_DIR / "cache"))
SNAPSHOTS_DIR = Path(os.environ.get("STOCK_SNAPSHOTS_DIR") or (SHARED_DIR / "snapshots"))

# 运行数据保留期：默认 90 天（约三个月）。STOCK_RETENTION_DAYS=0 → 不清理。
RETENTION_DAYS = int(os.environ.get("STOCK_RETENTION_DAYS", "90"))
# 共享取数缓存单独更短：按日分文件，一天约 200MB，留 7 天够回放/对账，留 90 天会白占十几 GB。
# STOCK_SHARED_CACHE_RETENTION_DAYS=0 → 不清理共享缓存（跟随 RETENTION_DAYS 的 90 天）。
SHARED_CACHE_RETENTION_DAYS = int(os.environ.get("STOCK_SHARED_CACHE_RETENTION_DAYS", "7"))

# ---------------------------------------------------------------- 缓存
CACHE_TTL_DAYS = float(os.environ.get("STOCK_CACHE_TTL_DAYS", "1"))   # 日线类当日复用
CACHE_TTL_HOURS = CACHE_TTL_DAYS * 24.0

# ---------------------------------------------------------------- 网络
MIN_INTERVAL_S = float(os.environ.get("STOCK_MIN_INTERVAL_S", "0.20"))  # 单域名最小间隔
TIMEOUT_S = float(os.environ.get("STOCK_TIMEOUT_S", "30"))
RETRIES = int(os.environ.get("STOCK_RETRIES", "3"))
UA = os.environ.get(
    "STOCK_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 stock/0.1",
)
# 东财基金 F10 接口要求带上页面 Referer，否则 404
EM_REFERER = "https://fundf10.eastmoney.com/"


def force_refresh() -> bool:
    """`STOCK_REFRESH=1` 时忽略缓存，强制重新取数（排查/补数用）。"""
    return str(os.environ.get("STOCK_REFRESH", "0")).strip().lower() in ("1", "true", "yes", "on")


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, OUT_DIR, INVEST_DIR, LOGS_DIR, ARCHIVE_DIR,
              SHARED_DIR, SHARED_CACHE_DIR, SNAPSHOTS_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)


def paths() -> dict:
    """给 doctor/CLI 打印用的路径快照。"""
    return {
        "root": str(ROOT), "data": str(DATA_DIR), "cache": str(CACHE_DIR),
        "out": str(OUT_DIR),
        "invest": str(INVEST_DIR),
        "logs": str(LOGS_DIR), "archive": str(ARCHIVE_DIR),
        "shared": str(SHARED_DIR), "shared_cache": str(SHARED_CACHE_DIR),
        "snapshots": str(SNAPSHOTS_DIR),
        "retention_days": RETENTION_DAYS,
    }
