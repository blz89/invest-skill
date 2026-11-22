# -*- coding: utf-8 -*-
"""构建「时间点无关的 ETF 全集」（point-in-time universe），用于消除回测的生存者偏差。

为什么需要：
  `out/stage1_etf.csv` 是「今天」筛选出来的 20 只，用它回放 2020 年等于提前知道
  「哪些 ETF 能活到今天、且今天还合格」——这是生存者偏差，回测收益会被系统性高估。
  正确做法：把 2020 年当时**存在**的 ETF（只要当时满足历史长度/流动性/收益/回撤）
  都放进候选池，而不是只用今天活下来的那批。

本脚本做两件事：
  1. 从新浪 ETF 全表（约 1700 只）里，用 `etf.exclude_reason` + `etf.classify`
     留下「A 股权益类、能分类」的标的（认不出的 fail-closed 剔除，与线上一致）；
  2. 逐只取日线（走 datasource 缓存），把「取到多少根、起止日期」写进 out/etf_universe.csv。

之后 `backtest.py --pool pit` 用这份全集做回放：每个决策日只用**当天的数据**判合格，
当天不存在的 ETF 自然不在池里（历史长度不足 750 根即不合格），生存者偏差消失。

用法：
    python etf_pit.py              # 首次全量抓取（约 1400 只，10–25 分钟，有缓存后会快）
    python etf_pit.py --limit 100  # 调试：只取前 100 只
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import datasource as ds  # noqa: E402
import etf  # noqa: E402

OUT = config.OUT_DIR
POOL_PATH = os.path.join(OUT, "etf_universe.csv")
MIN_ROWS = 300  # 少于 300 根的直接不进全集（连 750 根门槛都过不了，省内存）


def build(limit: int | None = None, verbose: bool = True) -> list[dict]:
    spot = ds.fetch_etf_spot()
    syms: list[tuple[str, str]] = []
    dropped = {"exclude": 0, "unclassifiable": 0}
    for _, row in spot.iterrows():
        sym = str(row["代码"]).strip()
        name = str(row["名称"]).strip()
        if not sym or not name:
            continue
        if sym in config.ETF["exclude_codes"]:
            dropped["exclude"] += 1
            continue
        if etf.exclude_reason(name):
            dropped["exclude"] += 1
            continue
        if not etf.classify(name):
            dropped["unclassifiable"] += 1
            continue
        syms.append((sym, name))
    if limit:
        syms = syms[:limit]
    if verbose:
        print(f"[universe] ETF 全表 {len(spot)} 只 → 权益类可分类 {len(syms)} 只"
              f"（剔除范围外 {dropped['exclude']}、认不出 {dropped['unclassifiable']}）")

    rows: list[dict] = []
    t0 = time.time()
    for k, (sym, name) in enumerate(syms, 1):
        try:
            df = ds.fetch_etf_hist(sym)
        except Exception as exc:  # noqa: BLE001 - 取数失败不中断，只跳过并记录
            if verbose:
                print(f"  [{k}/{len(syms)}] {sym} {name} 取数失败：{exc}")
            continue
        if df is None or len(df) < MIN_ROWS:
            continue
        cls = etf.classify(name)
        rows.append({
            "sym": sym, "name": name, "cls": cls,
            "n_rows": int(len(df)),
            "first_date": str(df["date"].iloc[0])[:10],
            "last_date": str(df["date"].iloc[-1])[:10],
            "last_close": float(df["close"].iloc[-1]),
        })
        if verbose and k % 100 == 0:
            print(f"  [{k}/{len(syms)}] 已取 {len(rows)} 只，用时 {time.time() - t0:.0f}s")

    os.makedirs(OUT, exist_ok=True)
    import pandas as pd  # noqa: E402

    pd.DataFrame(rows).to_csv(POOL_PATH, index=False, encoding="utf-8-sig")
    if verbose:
        print(f"[universe] 写入 {POOL_PATH}：{len(rows)} 只，用时 {time.time() - t0:.0f}s")
    return rows


def main():
    ap = argparse.ArgumentParser(description="构建 point-in-time ETF 全集（消除生存者偏差）")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只（调试）")
    args = ap.parse_args()
    rows = build(limit=args.limit)
    if not rows:
        print("[universe] 没有取到任何 ETF，检查网络/缓存")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
