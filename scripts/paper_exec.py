# -*- coding: utf-8 -*-
"""「执行日」账本结算命令：把上一期挂出的订单按真实日线成交，然后结算净值。

这是每周四 / 周一早上（或晚上）由执行 agent 跑的那条命令。模拟盘阶段它只做
「按缓存日线的开盘价/限价/触发价记成交」；将来接券商时，真实下单动作就插在这里
（paper.apply_fill 换成券商回报），其余账本逻辑不用动。

用法：
  python scripts/paper_exec.py                 # 结算到今天之前最后一个交易日
  python scripts/paper_exec.py --date 2026-10-26
  python scripts/paper_exec.py --dry-run       # 只打印会发生什么，不写账本
"""
import argparse
import copy
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import config
import datasource as ds
import metrics as mx
import paper

CFG = config.PAPER
PAPER_OUT = CFG["package_dir"]
WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def last_trading_day(cal, day):
    prior = [d for d in cal if d <= day]
    if not prior:
        raise SystemExit(f"交易日历里找不到 {day} 之前的交易日")
    return prior[-1]


def next_trading_day(cal, day):
    later = [d for d in cal if d > day]
    if not later:
        raise SystemExit(f"交易日历里找不到 {day} 之后的交易日")
    return later[0]


def fetch_bars(codes, etf_codes, upto=None):
    """返回 (bars_map, close_map, atr_map)。

    ETF 的 ATR 用前复权序列（分红除权日不复权会假跳空、撑大 ATR）；
    收盘价与撮合用价仍用不复权序列（止损价、成交价必须是真实价格）。
    """
    bars_map, close_map, atr_map = {}, {}, {}
    for code in sorted(set(codes)):
        try:
            df = ds.fetch_etf_hist(code) if code in etf_codes else ds.fetch_daily(code)
        except Exception as exc:  # noqa: BLE001
            print(f"[exec] 取 {code} 日线失败：{exc}")
            continue
        if df is None or len(df) == 0:
            continue
        b = []
        for _, r in df.iterrows():
            d = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])[:10]
            b.append({"date": d, "open": float(r["open"]), "high": float(r["high"]),
                      "low": float(r["low"]), "close": float(r["close"])})
        if upto:
            b = [x for x in b if x["date"] <= upto]
        if not b:
            continue
        bars_map[code] = b
        close_map[code] = b[-1]["close"]
        atr_src = None
        if code in etf_codes:
            try:
                dq = ds.fetch_etf_hist(code, adjust="qfq")
                if dq is not None and len(dq):
                    if upto:
                        dq = dq[pd.to_datetime(dq["date"]).dt.strftime("%Y-%m-%d") <= upto]
                    if len(dq):
                        atr_src = dq
            except Exception as exc:      # noqa: BLE001 —— 复权取不到就退回不复权
                print(f"[exec] {code} 前复权日线取不到，ATR 用不复权：{exc}")
        try:
            atr_map[code] = mx.atr(atr_src if atr_src is not None else pd.DataFrame(b), 14)
        except Exception:  # noqa: BLE001
            atr_map[code] = None
    return bars_map, close_map, atr_map


def bench_close(day):
    df = ds.fetch_index_daily(CFG["bench_index"]).copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    sub = df[df["date"] <= day]
    return float(sub["close"].iloc[-1]) if len(sub) else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="执行日：撮合挂单 + 结算净值")
    ap.add_argument("--ledger", default=CFG.get("default_ledger", "50k"),
                    choices=sorted((CFG.get("ledgers") or {"50k": {}}).keys()),
                    help="账本名（本部署单账本：50k）")
    ap.add_argument("--state", default=None)
    ap.add_argument("--date", default=None, help="结算日（默认今天）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.state:
        args.state = ((CFG.get("ledgers") or {}).get(args.ledger) or {}).get("state_file") or CFG["state_file"]

    if not os.path.exists(args.state):
        raise SystemExit(f"账本不存在：{args.state}，请先跑 weekly.py --init")
    state = paper.load(args.state)

    cal = [str(x) for x in ds.fetch_trade_calendar()["trade_date"].tolist()]
    day = args.date or dt.date.today().isoformat()
    as_of = last_trading_day(cal, day)
    print(f"[exec] 结算日 {as_of}（{WEEKDAY[dt.date.fromisoformat(as_of).weekday()]}）")

    pending = paper.pending_orders(state)
    etf_codes = {o["code"] for o in pending if o["kind"] == "etf"}
    etf_codes |= {c for c, h in state["holdings"].items() if h.get("kind") == "etf"}
    codes = {h["code"] for h in state["holdings"].values()} | {o["code"] for o in pending}
    bars_map, close_map, atr_map = fetch_bars(codes, etf_codes, as_of)

    n0 = len(state["trades"])
    events = paper.resolve_orders(state, as_of, bars_map)
    fills = [t for t in state["trades"][n0:]]
    bc = bench_close(as_of)
    nxt = next_trading_day(cal, as_of)
    if args.dry_run:
        state = copy.deepcopy(state)
        paper.accrue_cash(state, as_of, close_map, calendar=cal)
        paper.settle(state, as_of, close_map, bc)
        new_stops = paper.stop_orders(state, as_of, close_map, atr_map, fill_from=nxt)
    else:
        paper.accrue_cash(state, as_of, close_map, calendar=cal)
        paper.settle(state, as_of, close_map, bc)
        # 成交当天就补上保护性止损单：否则周一成交后要等到下周六 20:00 决策才挂 GTC，
        # 中间两个交易日的盘中跌破没有单子保护。
        new_stops = paper.stop_orders(state, as_of, close_map, atr_map, fill_from=nxt)
        paper.save(state, args.state)

    print(f"[exec] 撮合事件 {len(events)} 条")
    for _, msg in events:
        print(f"  - {msg}")
    print(f"[exec] 本期成交 {len(fills)} 笔：")
    for t in fills:
        print(f"  - {t['date']} {'买入' if t['action'] == 'buy' else '卖出'} {t['code']} "
              f"{t['name']} {t['shares']} 股 @ {t['price']:.3f}，金额 {t['amount']:,.2f}，"
              f"费用 {t['fee']:.2f}")
    if not fills:
        print("  （无成交：挂单未触发或无可成交挂单）")

    p = paper.portfolio(state, close_map)
    ratio = (p["market_value"] / p["nav"]) if p["nav"] else 0.0
    print(f"[exec] 结算后：净值 {p['nav']:,.2f} 元（{p['total_return']:+.2%}），"
          f"现金 {p['cash']:,.2f} 元，仓位 {ratio:.1%}，"
          + (f"回撤 {_dd:+.2%}" if (_dd := paper.current_drawdown(state)) is not None
             else "回撤 无法判定（尚无净值点）"))
    if new_stops:
        print(f"[exec] 新建/更新保护性止损单 {len(new_stops)} 张（下一交易日 {nxt} 生效）")
        for o in new_stops:
            print(f"  - {o['id']} {o['code']} {o['name']} {o['shares']} 股 "
                  f"{o['order_type']} @ {o.get('limit_price')}（{o['reason']}）")
    left = paper.pending_orders(state)
    print(f"[exec] 仍未成交挂单 {len(left)} 张")
    for o in left:
        print(f"  - {o['id']} {o['action']} {o['code']} {o['name']} {o['shares']} 股 "
              f"{o['order_type']} @ {o.get('limit_price')}（{o['reason']}）")

    if args.dry_run:
        print("[exec] --dry-run：未写账本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
