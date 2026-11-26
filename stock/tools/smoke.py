# -*- coding: utf-8 -*-
r"""联网冒烟：把取数接口层的 40 个接口挨个调一遍，打印 shape / 耗时 / 错误。

用法::

    $env:PYTHONPATH='D:\skills\invest\stock'; $env:PYTHONIOENCODING='utf-8'
    & D:\skills\invest\stock\.venv\Scripts\python.exe D:\skills\invest\stock\tools\smoke.py
    ... -f stock_zh_a_daily         # 只跑一个
    ... -q                          # 只打印失败项

退出码：0 = 全部拿到数据；1 = 有接口失败（失败的会列出原因）。
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

import pandas as pd

from stock import endpoints as ep

#: 带参数的接口：无参调用会走到默认值（例如 fund_fee_em 默认的"认购费率"页面没有该表，
#: 会返回空表被误报 EMPTY）。这里给一个能真正取到数的输入。
ARGS = {
    "fund_fee_em": {"symbol": "015641", "indicator": "赎回费率"},
    "stock_zh_a_hist": {"symbol": "600519", "start_date": "20261001", "end_date": "20261113"},
    "fund_open_fund_info_em": {"symbol": "000001", "indicator": "单位净值走势"},
}

#: 已知"上游本身不可用"的接口：失败不算回归（东财 push2/push2his 在本机被网络重置，
#: 乐咕乐股会随机 429/504）。其它接口失败一律视为回归。
KNOWN_DOWN = {
    "stock_market_activity_legu": "乐咕乐股 WAF 限流（429/504），单源无替代",
}

#: ETF 两个接口已带兜底（东财挂 → 新浪/腾讯），不再算"已知不可用"：
#: fund_etf_spot_em → 新浪 ETF 列表；fund_etf_hist_em → 腾讯 K 线 → 新浪日线。


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--func", action="append")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    names = a.func or list(ep.__all__)
    bad = 0
    print("取数接口层冒烟：%d 个接口\n" % len(names))
    for name in names:
        fn = getattr(ep, name, None)
        if fn is None:
            print("[MISS] %-34s 适配器里没有" % name)
            bad += 1
            continue
        t0 = time.time()
        try:
            out = fn(**ARGS.get(name, {}))
            dt = time.time() - t0
            if isinstance(out, pd.DataFrame):
                detail = "%dx%d" % (len(out), len(out.columns))
            elif isinstance(out, (dict, list)):
                detail = "%s(%d)" % (type(out).__name__, len(out))
            else:
                detail = type(out).__name__
            ok = not (isinstance(out, pd.DataFrame) and out.empty)
            print("[%-4s] %-34s %-10s %5.1fs" % ("OK" if ok else "EMPTY", name, detail, dt))
        except Exception as e:  # noqa: BLE001
            dt = time.time() - t0
            tag = "DOWN" if name in KNOWN_DOWN else "FAIL"
            if tag != "DOWN":
                bad += 1
            print("[%-4s] %-34s %5.1fs %s: %s" % (tag, name, dt, type(e).__name__, str(e)[:110]))
            if tag == "FAIL" and not a.quiet:
                print("       " + traceback.format_exc().strip().splitlines()[-1])
    print("\n结论：%s（%d 个非预期失败）" % ("全通" if bad == 0 else "有失败", bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
