# -*- coding: utf-8 -*-
"""示例脚本：把几个源拼成一份「盘后简报」。

演示三件事：
1. 日线用 `api.daily/index_daily`（腾讯主通道，自动回退）；
2. 快照用 `api.quote`（东财→腾讯→新浪→同花顺，自动回退）；
3. 现金层用 `api.money_fund_rates()` + `api.repo_rate()`。

跑法：`python stock/scripts/demo_brief.py`（也可以在任何目录跑，脚本自己补 sys.path）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 包在仓库的 stock/stock，所以把仓库根放进 path（不要放上一级，那会撞上同名目录变成命名空间包）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock import api  # noqa: E402


def main() -> int:
    print("== 指数（近 5 日）==")
    idx = api.index_daily("000300.SH")
    if idx.empty:
        print("(取不到)")
    else:
        tail = idx.tail(5)[["date", "open", "close", "high", "low"]]
        last, prev = tail["close"].iloc[-1], tail["close"].iloc[-2]
        print(tail.to_string(index=False))
        print(f"沪深300 收盘 {last:.2f}（{(last/prev-1)*100:+.2f}%）\n")

    print("== 持仓/关注快照 ==")
    snap = api.quote(["600519", "510300", "159915"])
    cols = [c for c in ("code", "name", "price", "chg_pct", "amount") if c in snap.columns]
    print(snap[cols].to_string(index=False), "\n")

    print("== 现金层：货基 7 日年化前 5 ==")
    mf = api.money_fund_rates().dropna(subset=["y7_pct"]).sort_values("y7_pct", ascending=False)
    print(mf.head(5)[["code", "name", "wanfen", "y7_pct", "data_date"]].to_string(index=False), "\n")

    print("== 逆回购 GC001 最近 3 日 ==")
    repo = api.repo_rate("204001")
    print(repo.tail(3)[["date", "open", "high", "low", "close"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
