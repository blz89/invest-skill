"""navstats 自测：日度净值表现的纯函数口径（不联网、不写盘）。"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import navstats as NS   # noqa: E402

_ok = 0
_fail = []


def ck(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
    else:
        _fail.append(f"{name}{(' ← ' + detail) if detail else ''}")


def navs(*pairs, bench=None):
    out = []
    for i, (d, v) in enumerate(pairs):
        row = {"date": d, "nav": v, "cash": v, "market_value": 0.0,
               "otc_value": 0.0, "positions": 0}
        if bench is not None:
            row["bench_nav"] = bench[i]
        out.append(row)
    return out


def test_empty():
    r = NS.stats([])
    ck("空序列 ok=False", r["ok"] is False and r["n"] == 0)
    ck("空序列 stats 不抛", isinstance(r, dict))
    md = NS.render_md(r)
    ck("空 md 含「没有日度净值」", "没有日度净值" in md)
    ck("空 md 明说不是收益0", "不是「收益为 0」" in md)
    html = NS.render_html(r)
    ck("空 html 含 note", "note" in html and "没有数据" in html)


def test_single():
    r = NS.stats(navs(("2026-10-30", 50000.0)), capital=50000.0)
    ck("单点 ok", r["ok"] is True)
    ck("单点 n=1", r["n"] == 1)
    ck("单点日收益数=0", r["n_days"] == 0)
    ck("单点收益=0", abs(r["ret"]) < 1e-12)
    ck("单点波动=0", r["vol"] == 0.0)
    ck("单点赢率 None", r["win_rate"] is None)


def test_three():
    r = NS.stats(navs(("2026-10-26", 50000.0),
                      ("2026-10-27", 55000.0),
                      ("2026-10-28", 49500.0)), capital=50000.0)
    ck("三点 n=3", r["n"] == 3)
    ck("三点日收益=2", r["n_days"] == 2)
    ck("三点区间收益=-1%", abs(r["ret"] - (49500.0 / 50000.0 - 1)) < 1e-12, str(r["ret"]))
    ck("三点最大回撤=-10%", abs(r["mdd"] - (49500.0 / 55000.0 - 1)) < 1e-12, str(r["mdd"]))
    ck("回撤日期=10-28", r["mdd_day"] == "2026-10-28")
    ck("正收益 1/2", r["win"] == 1 and r["n_days"] == 2)
    ck("赢率 50%", abs(r["win_rate"] - 0.5) < 1e-12)
    ck("相对本金 -500", abs(r["profit"] - (-500.0)) < 1e-6, str(r["profit"]))
    ck("最好单日 +10%", abs(r["best"]["ret"] - 0.10) < 1e-12)
    ck("最差单日 -10%", abs(r["worst"]["ret"] - (-0.10)) < 1e-12)
    ck("日净值表含 10-27", "2026-10-27" in NS.render_md(r))


def test_window():
    ns = navs(("2026-10-26", 50000.0), ("2026-10-27", 50500.0),
              ("2026-10-28", 51005.0), ("2026-10-29", 51005.0))
    r = NS.stats(ns, capital=50000.0, start="2026-10-27")
    ck("start 生效 n=3", r["n"] == 3, str(r.get("n")))
    ck("start 起点=10-27", r["start"] == "2026-10-27")
    ck("start 区间收益=约1%", abs(r["ret"] - (51005.0 / 50500.0 - 1)) < 1e-12)
    r2 = NS.stats(ns, start="2026-10-26", end="2026-10-27")
    ck("end 生效 n=2", r2["n"] == 2)
    ck("end 终点=10-27", r2["end"] == "2026-10-27")


def test_sort_and_skip():
    ns = [{"date": "2026-10-28", "nav": 102.0},
          {"date": "2026-10-26", "nav": 100.0},
          {"date": "", "nav": 9.0},
          {"date": "2026-10-27", "nav": 0},
          {"date": "2026-10-27", "nav": None},
          {"date": "2026-10-27", "nav": "abc"}]
    r = NS.stats(ns)
    ck("跳掉脏行只剩 2 点", r["n"] == 2, str(r.get("n")))
    ck("乱序已排序", [d["date"] for d in r["daily"]] == ["2026-10-28"])
    ck("区间收益=+2%", abs(r["ret"] - 0.02) < 1e-12)


def test_bench():
    r = NS.stats(navs(("2026-10-26", 50000.0), ("2026-10-27", 50500.0),
                      bench=[4000.0, 4040.0]), capital=50000.0)
    ck("基准收益=+1%", abs(r["bench_ret"] - 0.01) < 1e-12)
    ck("超额=0", abs(r["excess"]) < 1e-12)
    md = NS.render_md(r)
    ck("md 含沪深300 对比", "沪深300" in md and "超额" in md)
    r2 = NS.stats(navs(("2026-10-26", 50000.0), ("2026-10-27", 50500.0)), capital=50000.0)
    ck("无基准时 bench_ret None", r2["bench_ret"] is None and r2["excess"] is None)
    ck("无基准时 md 不编超额", "超额" not in NS.render_md(r2))


def test_render_html():
    r = NS.stats(navs(("2026-10-26", 50000.0), ("2026-10-27", 51000.0)), capital=50000.0)
    html = NS.render_html(r)
    ck("html 有 h2", "<h2>" in html)
    ck("html 有区间收益", "+2.00%" in html, html[:80])
    ck("html 有表头", "<th>日期</th>" in html)


def test_capital_none():
    r = NS.stats(navs(("2026-10-26", 50000.0), ("2026-10-27", 51000.0)))
    ck("无本金 profit=None", r["profit"] is None)
    ck("无本金 md 不写本金行", "相对本金" not in NS.render_md(r))


def main():
    for fn in (test_empty, test_single, test_three, test_window,
               test_sort_and_skip, test_bench, test_render_html, test_capital_none):
        fn()
    if _fail:
        print(f"navstats 自测：通过 {_ok} 项，失败 {len(_fail)} 项")
    else:
        print(f"navstats 自测：通过 {_ok} 项，失败 0 项")
    for f in _fail:
        print("  ✗", f)
    return 0 if not _fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
