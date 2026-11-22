# -*- coding: utf-8 -*-
"""命令行：`python -m stock <子命令>`。

设计原则：**只读、不改盘**（除缓存）。输出尽量是「能直接贴进对话/文档」的表格。
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from . import api, cache, config


def _print_df(df: pd.DataFrame, max_rows: int | None = None) -> None:
    if df is None or len(df) == 0:
        print("(空)")
        return
    with pd.option_context("display.width", 200, "display.max_columns", 40,
                           "display.max_rows", max_rows or len(df)):
        print(df.to_string(index=False))


def cmd_doctor(_args) -> int:
    info = api.doctor()
    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
    return 0 if info.get("fuyao") else 1


def cmd_daily(args) -> int:
    df = api.daily(args.code, start=args.start, end=args.end, adjust=args.adjust)
    cache.record("cli", "daily", {"code": args.code}, len(df))
    _print_df(df.tail(args.n))
    return 0


def cmd_index(args) -> int:
    _print_df(api.index_daily(args.symbol, start=args.start, end=args.end).tail(args.n))
    return 0


def cmd_etf(args) -> int:
    _print_df(api.etf_daily(args.symbol, start=args.start, end=args.end).tail(args.n))
    return 0


def cmd_quote(args) -> int:
    _print_df(api.quote(args.codes, source=args.source))
    return 0


def cmd_fund(args) -> int:
    df = api.fund_lsjz(args.code, page_size=args.n)
    _print_df(df.tail(args.n))
    return 0


def cmd_money(args) -> int:
    df = api.money_fund_rates()
    if df.empty:
        print("(空)")
        return 1
    df = df.dropna(subset=["y7_pct"]).sort_values("y7_pct", ascending=False)
    if args.top:
        df = df.head(args.top)
    _print_df(df)
    return 0


def cmd_repo(args) -> int:
    _print_df(api.repo_rate(args.symbol).tail(args.n))
    return 0


def cmd_cal(args) -> int:
    df = api.trade_calendar()
    _print_df(df.tail(args.n))
    return 0


def cmd_cache(args) -> int:
    if args.clear:
        n = cache.clear(args.clear if args.clear != "all" else None)
        print(f"已清理 {n} 个缓存文件")
        return 0
    print(json.dumps(cache.stats(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_prune(args) -> int:
    from . import retention
    return retention.main(["--days", str(args.days)] + (["--dry"] if args.dry else []))


def cmd_prefetch(args) -> int:
    from . import prefetch
    return prefetch.run(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stock", description="自建行情/财务数据脚本组")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="源可用性 + 缓存状态")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("daily", help="个股日线")
    s.add_argument("code")
    s.add_argument("--start", default="")
    s.add_argument("--end", default="")
    s.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", ""])
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("index", help="指数日线")
    s.add_argument("symbol", nargs="?", default="000300")
    s.add_argument("--start", default="")
    s.add_argument("--end", default="")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("etf", help="ETF 日线")
    s.add_argument("symbol", nargs="?", default="510300")
    s.add_argument("--start", default="")
    s.add_argument("--end", default="")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_etf)

    s = sub.add_parser("quote", help="批量行情快照")
    s.add_argument("codes", nargs="+")
    s.add_argument("--source", default=None, choices=["em", "ts"])
    s.set_defaults(func=cmd_quote)

    s = sub.add_parser("fund", help="场外基金历史净值")
    s.add_argument("code")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_fund)

    s = sub.add_parser("money", help="货基 7 日年化排行")
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(func=cmd_money)

    s = sub.add_parser("repo", help="交易所逆回购日线")
    s.add_argument("symbol", nargs="?", default="204001")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_repo)

    s = sub.add_parser("cal", help="交易日历")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_cal)

    s = sub.add_parser("cache", help="缓存统计 / 清理")
    s.add_argument("--clear", nargs="?", const="all", default="")
    s.set_defaults(func=cmd_cache)

    s = sub.add_parser("prune", help="清理过期运行数据（默认 90 天，账本/凭据不动）")
    s.add_argument("--days", type=int, default=config.RETENTION_DAYS)
    s.add_argument("--dry", action="store_true", help="只看会删什么，不真删")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("prefetch", help="一次性取全当天要用的数据（统一落 data\\shared）")
    s.add_argument("--workers", type=int, default=0, help="组内并发（默认 8）")
    s.add_argument("--only", default="", help="只跑这些组：ref,index,etf,fund,stocks,weekly")
    s.add_argument("--skip", default="", help="跳过这些组（逗号分隔）")
    s.add_argument("--skip-stocks", action="store_true", help="跳过个股逐只预热（演练/救急）")
    s.add_argument("--with-weekly", action="store_true", help="附带货基历史（翻页慢，周六/决策日用）")
    s.add_argument("--force", action="store_true", help="忽略当天缓存重取（A_SHARE_FORCE_REFRESH=1）")
    s.add_argument("--date", default="", help="钉住数据日（默认真实今天；演练回放用）")
    s.add_argument("--dry-run", action="store_true", help="只打印计划，不取数")
    s.add_argument("--verify-only", action="store_true", help="只做覆盖自检，不取数")
    s.add_argument("--allow-stale-bar", action="store_true",
                   help="允许指数末根 bar 未到应有交易日（仅演练；生产别开）")
    s.add_argument("--allow-intraday", action="store_true",
                   help="允许交易日收盘前（<15:05）取数（会把当天键写成未收盘数据，仅演练）")
    s.add_argument("--json", action="store_true", help="末尾打印 latest.json")
    s.set_defaults(func=cmd_prefetch)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.ensure_dirs()
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
