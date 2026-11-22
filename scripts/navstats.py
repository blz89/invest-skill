"""账本表现：用「每日巡检记下的日度净值」算区间收益，而不是只取期初/期末两个点。

为什么单独成模块（决策日做账本时要参考每日巡检的数据计算收益）：
- 巡检（交易日 20:00 DSH 会话，纯代码）每天往 `paper/state_*.json` 的 `nav` 里追加一条净值，
  一周 5 个点；但周报原来只比较「上期决策日 → 本期决策日」两个点，中间 4 天白记了。
- 这里把 `nav` 序列拿来做收益/波动/回撤/胜率，口径写死在这个文件里，全库只有这一份实现。

约定：
- 只做纯计算，不读文件、不写文件、不联网；调用方把 `state["nav"]` 传进来。
- 区间起点含、终点含：用上期决策日那条净值当基准（同一天的值在决策日结算时已被覆盖）。
- 日收益按「相邻两点」算，所以 n 个点 = n-1 个日收益；年化按 244 个交易日。
"""

import math

ANNUAL_DAYS = 244


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def slice_navs(navs, start=None, end=None) -> list:
    """截取 [start, end] 区间的净值点（按日期升序，跳掉日期/净值缺失的行）。"""
    rows = []
    for n in (navs or []):
        d = str(n.get("date") or "")
        nav = _f(n.get("nav"))
        if not d or nav is None or nav <= 0:
            continue
        if start and d < str(start):
            continue
        if end and d > str(end):
            continue
        rows.append({"date": d, "nav": nav,
                     "bench_nav": _f(n.get("bench_nav")),
                     "cash": _f(n.get("cash")),
                     "market_value": _f(n.get("market_value")),
                     "otc_value": _f(n.get("otc_value")),
                     "positions": n.get("positions")})
    rows.sort(key=lambda r: r["date"])
    return rows


def stats(navs, capital=None, start=None, end=None,
          annual_days: int = ANNUAL_DAYS) -> dict:
    """区间表现。start 建议传上期决策日 as_of，不传就从第一个净值点起算。

    返回 ok=False 时只有 n=0，调用方应原样展示「本期没有日度净值」而不是编数字。
    """
    rows = slice_navs(navs, start, end)
    if not rows:
        return {"ok": False, "n": 0}
    first, last = rows[0], rows[-1]
    ret = last["nav"] / first["nav"] - 1.0

    daily = []
    peak = first["nav"]
    mdd = 0.0
    mdd_day = first["date"]
    for prev, cur in zip(rows, rows[1:]):
        daily.append({"date": cur["date"],
                      "nav": cur["nav"],
                      "ret": cur["nav"] / prev["nav"] - 1.0})
        if cur["nav"] > peak:
            peak = cur["nav"]
        dd = cur["nav"] / peak - 1.0
        if dd < mdd:
            mdd, mdd_day = dd, cur["date"]

    n_ret = len(daily)
    mean = sum(d["ret"] for d in daily) / n_ret if n_ret else 0.0
    if n_ret > 1:
        var = sum((d["ret"] - mean) ** 2 for d in daily) / (n_ret - 1)
        vol = math.sqrt(var) * math.sqrt(annual_days)
    else:
        vol = 0.0
    win = sum(1 for d in daily if d["ret"] > 0)
    best = max(daily, key=lambda d: d["ret"]) if daily else None
    worst = min(daily, key=lambda d: d["ret"]) if daily else None

    bf, bl = first.get("bench_nav"), last.get("bench_nav")
    bench_ret = (bl / bf - 1.0) if (bf and bl and bf > 0) else None
    cap = _f(capital)

    return {
        "ok": True,
        "n": len(rows),
        "start": first["date"],
        "end": last["date"],
        "nav_first": first["nav"],
        "nav_last": last["nav"],
        "ret": ret,
        "daily": daily,
        "mdd": mdd,
        "mdd_day": mdd_day,
        "mean": mean,
        "vol": vol,
        "n_days": n_ret,
        "win": win,
        "win_rate": (win / n_ret) if n_ret else None,
        "best": best,
        "worst": worst,
        "bench_ret": bench_ret,
        "excess": (ret - bench_ret) if bench_ret is not None else None,
        "capital": cap,
        "profit": (last["nav"] - cap) if cap else None,
        "positions": last.get("positions"),
        "cash": last.get("cash"),
        "market_value": last.get("market_value"),
    }


def render_md(st: dict, title: str = "账本表现（按每日巡检的日度净值计算）") -> str:
    """周报用的 markdown 小节。st 为 stats() 的返回值。"""
    L = [f"## {title}", ""]
    if not st or not st.get("ok"):
        L.append("- 本期没有日度净值记录（巡检还没跑过，或账本刚建）。"
                 "这不是「收益为 0」，是**没有数据**，别当收益写。")
        L.append("")
        return "\n".join(L)

    head = (f"- 区间 **{st['start']} → {st['end']}**，共 **{st['n']} 个净值点**"
            f"（{st['n_days']} 个日收益）")
    L.append(head)
    line = f"- 区间收益 **{st['ret']:+.2%}**（{st['nav_first']:,.2f} → {st['nav_last']:,.2f} 元）"
    if st.get("bench_ret") is not None:
        line += (f"；同期沪深300 **{st['bench_ret']:+.2%}**，"
                 f"超额 **{st['excess']:+.2%}**")
    L.append(line)
    if st.get("profit") is not None:
        L.append(f"- 相对本金 {st['capital']:,.0f} 元：**{st['profit']:+,.2f} 元**")
    day = (f"- 日度：均值 {st['mean']:+.3%}｜年化波动 {st['vol']:.2%}｜"
           f"最大回撤 **{st['mdd']:.2%}**（{st['mdd_day']}）")
    if st.get("win_rate") is not None:
        day += f"｜正收益 {st['win']}/{st['n_days']} 天（{st['win_rate']:.0%}）"
    L.append(day)
    if st.get("best") and st.get("worst"):
        L.append(f"- 最好单日 {st['best']['ret']:+.2%}（{st['best']['date']}）｜"
                 f"最差单日 {st['worst']['ret']:+.2%}（{st['worst']['date']}）")
    if st.get("daily"):
        L.append("")
        L.append("| 日期 | 净值 | 日收益 |")
        L.append("|---|---|---|")
        L.append(f"| {st['start']} | {st['nav_first']:,.2f} | — |")
        for d in st["daily"]:
            L.append(f"| {d['date']} | {d['nav']:,.2f} | {d['ret']:+.2%} |")
    L.append("")
    L.append("> 日度净值由交易日 20:00 的 DSH 巡检会话（纯代码）记入账本 `nav`（0 token）；"
             "决策日只负责结算与决策，不再另算一遍收益。")
    L.append("")
    return "\n".join(L)


def render_html(st: dict, title: str = "账本表现（按每日巡检的日度净值计算）") -> str:
    """周报 HTML 小节。"""
    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    if not st or not st.get("ok"):
        return (f"<h2>{esc(title)}</h2><div class='card note'>"
                "本期没有日度净值记录（巡检还没跑过，或账本刚建）。"
                "这不是「收益为 0」，是没有数据。</div>")
    rows = []
    for d in st.get("daily") or []:
        rows.append(f"<tr><td>{esc(d['date'])}</td><td>{d['nav']:,.2f}</td>"
                    f"<td>{d['ret']:+.2%}</td></tr>")
    table = ("<table><thead><tr><th>日期</th><th>净值</th><th>日收益</th></tr></thead>"
             "<tbody><tr><td>" + esc(st["start"]) + f"</td><td>{st['nav_first']:,.2f}</td>"
             "<td>—</td></tr>" + "".join(rows) + "</tbody></table>")
    bench = (f"同期沪深300 <b>{st['bench_ret']:+.2%}</b>，"
             f"超额 <b>{st['excess']:+.2%}</b>"
             if st.get("bench_ret") is not None else "（缺基准净值，未算超额）")
    dstat = (f"日度均值 {st['mean']:+.3%}｜年化波动 {st['vol']:.2%}｜"
             f"最大回撤 <b>{st['mdd']:.2%}</b>（{esc(st['mdd_day'])}）")
    if st.get("win_rate") is not None:
        dstat += f"｜正收益 {st['win']}/{st['n_days']} 天（{st['win_rate']:.0%}）"
    return (f"<h2>{esc(title)}</h2><div class='card'>"
            f"<p>区间 <b>{esc(st['start'])} → {esc(st['end'])}</b>，共 "
            f"<b>{st['n']} 个净值点</b>（{st['n_days']} 个日收益）；"
            f"区间收益 <b>{st['ret']:+.2%}</b>"
            f"（{st['nav_first']:,.2f} → {st['nav_last']:,.2f} 元）。</p>"
            f"<p>{bench}</p><p>{dstat}</p>{table}</div>")


def main() -> int:
    """手工自查：打印账本最近一段日度表现。"""
    import argparse
    import json
    import os
    import sys

    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import config

    ap = argparse.ArgumentParser(description="按日度净值算账本表现")
    ap.add_argument("--ledger", default="all",
                    choices=sorted(list(config.PAPER["ledgers"].keys())) + ["all"])
    ap.add_argument("--start", default=None, help="起点日期 YYYY-MM-DD（默认全部）")
    ap.add_argument("--end", default=None, help="终点日期 YYYY-MM-DD（默认最新）")
    a = ap.parse_args()

    lines = config.PAPER["ledgers"]
    targets = list(lines) if a.ledger == "all" else [a.ledger]
    for led in targets:
        path = os.path.join(os.path.dirname(lines[led]["state_file"]) or ".",
                            lines[led]["state_file"].split(os.sep)[-1])
        if not os.path.exists(path):
            print(f"[{led}] 账本不存在：{path}")
            continue
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        r = stats(st.get("nav") or [], capital=float(st["meta"]["capital"]),
                  start=a.start, end=a.end)
        print(f"===== {led}（{config.PAPER['ledgers'][led]['label']}）=====")
        print(render_md(r, title=f"账本表现 · {led}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
