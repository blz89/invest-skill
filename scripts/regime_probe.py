# -*- coding: utf-8 -*-
"""牛熊确认探针（只读，不参与打分、不改仓位）。

为什么要「牛熊都探」：现有 `market_regime` 只拿沪深300 对 MA200 的偏离分三档，
能识别「趋势弱」，但分不清「普通回调」「熊市」「牛市」。牛熊都用**价格事实**
判断（不用 PMI 这类滞后景气指标——2014–15 就是反例）：

熊市确认（≥2 条）
1. 沪深300 距 2 年高点回撤 ≤ -20%（深跌）
2. 沪深300 相对 MA200 偏离 ≤ -10%（跌破长期均线）
3. 全市场宽度（缓存个股收盘 > MA200 的比例）< 30%（多数个股走弱）

牛市确认（≥2 条）
1. 沪深300 距 2 年低点涨幅 ≥ +25%（显著反弹）
2. 沪深300 相对 MA200 偏离 ≥ +10%（站上均线且显著偏高）
3. 全市场宽度 > 60%（多数个股在均线上方）

四态结论：熊市确认 / 牛市确认 / 中性 / 信号冲突（两边同时成立，例如指数靠
权重股撑住、多数个股在跌）/ 数据不足（可用判据 <2 条）。

阈值故意不对称：熊市要「跌得深」（-20%）才算，牛市要「贴近高位」而不是
「从低点涨 20%」——后者在熊市反弹里会假报一次。

本脚本只做判定并落盘（`out/regime.json`）；**档位映射在 `plan.market_regime()`**：
深熊 → 25%，熊 → 防守档 35%，牛 → 进攻档 80%，普通/未确认 → 按沪深300 对
MA200 的三档（80/60/35）。判定带两层保护：**幅度**（熊市确认且回撤 ≤ -35%、
宽度 < 20% → 深熊再压一档）与**滞回**（进入靠确认，退出要回撤回到 -10% 以内
且宽度 > 45%，避免阈值附近来回换档、反复重建组合）。滞回状态存在本文件的
`stance` 字段里，下一次判定先读上次结果。数据全部来自本地缓存
（`stock/data/shared/cache`），0 取数、0 token。

用法：
    python scripts/regime_probe.py            # 打印 + 写 out/regime.json、out/regime_brief.md
    python scripts/regime_probe.py --json     # 只打印 JSON
    python scripts/regime_probe.py --no-write # 只看不写
    python scripts/regime_probe.py --limit 300 # 宽度抽样（快）
"""
import argparse
import datetime as dt
import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

P = config.REGIME_PROBE
OUT_JSON = os.path.join(config.OUT_DIR, "regime.json")
OUT_MD = os.path.join(config.OUT_DIR, "regime_brief.md")


# ---------- 纯函数（自检直接测它们，不碰缓存） ----------

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # 排除 NaN


def dev_ma(close, n=200):
    """收盘相对 MA(n) 的偏离；数据不足或均线异常返回 None。"""
    vals = [v for v in (_num(x) for x in close) if v is not None]
    if len(vals) < n + 1:
        return None
    ma = sum(vals[-n:]) / float(n)
    if ma <= 0:
        return None
    return vals[-1] / ma - 1.0


def dd_from_high(close, window=500):
    """收盘距近 window 根最高收盘的回撤（负数）；数据不足返回 None。"""
    vals = [v for v in (_num(x) for x in close) if v is not None]
    if len(vals) < 2:
        return None
    seg = vals[-window:]
    hi = max(seg)
    if hi <= 0:
        return None
    return vals[-1] / hi - 1.0


def up_from_low(close, window=500):
    """收盘距近 window 根最低收盘的涨幅（正数）；数据不足返回 None。"""
    vals = [v for v in (_num(x) for x in close) if v is not None]
    if len(vals) < 2:
        return None
    seg = vals[-window:]
    lo = min(seg)
    if lo <= 0:
        return None
    return vals[-1] / lo - 1.0


def breadth_ratio(flags):
    """传入若干 bool，返回上方占比；空集返回 None。"""
    flags = list(flags)
    if not flags:
        return None
    return sum(1 for f in flags if f) / float(len(flags))


def side_verdict(dev, dd, breadth, side, p=None, up=None):
    """单方向判定。side = "bear" | "bull"；up = 距 2 年低点涨幅（bull 用）。

    返回 {"side","conditions":[[key,label,value,threshold,hit]],"hits","usable",
          "confirmed": bool|None}；可用判据 <2 条 → confirmed None（数据不足）。
    """
    p = p or P
    if side == "bear":
        specs = [("dd_bear", "距 2 年高点回撤", dd, p["dd_bear"], lambda v, t: v <= t),
                 ("dev_bear", "相对 MA200 偏离", dev, p["dev_bear"], lambda v, t: v <= t),
                 ("breadth_bear", "全市场宽度", breadth, p["breadth_bear"], lambda v, t: v < t)]
    else:
        specs = [("bull_low_rebound", "距 2 年低点涨幅", up, p["bull_low_rebound"], lambda v, t: v >= t),
                 ("bull_dev", "相对 MA200 偏离", dev, p["bull_dev"], lambda v, t: v >= t),
                 ("bull_breadth", "全市场宽度", breadth, p["bull_breadth"], lambda v, t: v > t)]
    conds = [[k, lb, v, th, bool(fn(v, th))] for k, lb, v, th, fn in specs if v is not None]
    hits = sum(1 for c in conds if c[4])
    usable = len(conds)
    confirmed = None if usable < int(p["min_hits"]) else hits >= int(p["min_hits"])
    return {"side": side, "conditions": conds, "hits": hits, "usable": usable,
            "confirmed": confirmed}


def verdict(dev, dd, breadth, p=None):
    """熊市单方向判定（旧接口，等价于 side_verdict(...,"bear") 的扁平返回）。"""
    r = side_verdict(dev, dd, breadth, "bear", p)
    return {"conditions": r["conditions"], "hits": r["hits"], "usable": r["usable"],
            "bear": r["confirmed"],
            "verdict": "数据不足" if r["confirmed"] is None else ("熊市确认" if r["confirmed"] else "熊市未确认")}


EXPLAIN = {
    "熊市确认": "至少 2 条价格事实成立：深回撤 / 跌破 MA200 / 宽度收缩。这是「确认已经跌了」，不是预测继续跌。",
    "牛市确认": "至少 2 条价格事实成立：贴近两年高位 / 站上 MA200 且偏离 +10% / 宽度 > 60%。同样是确认趋势，不是预测。",
    "中性": "两个方向都不足 2 条命中：既不是确认的熊市，也不是确认的牛市，按原三档规则执行即可。",
    "信号冲突": "熊市与牛市判据各自都够 2 条（常见于指数靠权重股撑住、多数个股在跌）：不硬下结论，按原规则 + 看宽度细节判断。",
    "数据不足": "可用判据不到 2 条（指数缓存缺或宽度算不出）：不判断，先补数据。",
}


def assess(dev, dd, breadth, up=None, p=None):
    """牛熊四态总判定；up = 距 2 年低点涨幅（缺失时牛市少一条判据）。"""
    b = side_verdict(dev, dd, breadth, "bear", p)
    u = side_verdict(dev, dd, breadth, "bull", p, up=up)
    if b["confirmed"] is None or u["confirmed"] is None:
        state = "数据不足"
    elif b["confirmed"] and u["confirmed"]:
        state = "信号冲突"
    elif b["confirmed"]:
        state = "熊市确认"
    elif u["confirmed"]:
        state = "牛市确认"
    else:
        state = "中性"
    return {"bear": b, "bull": u, "state": state, "explain": EXPLAIN[state]}


def _fmt(x, digits=1):
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def stance_of(state, dev, dd, breadth, prev=None, p=None):
    """把「四态确认」落成带**幅度**与**滞回**的档位立场。

    stance：bear / bull / normal；severity：深熊 / 熊 / 牛 / 普通。
    - 幅度：熊市确认且回撤 ≤ -35%、宽度 < 20% → 深熊（再压一档）
    - 滞回：进入靠确认；退出要「两边都回来」，避免指数在阈值附近抖动时
      反复换档（每次换档都会触发组合重建，换手与费用都上来了）。
    """
    p = p or P
    prev_stance = str((prev or {}).get("stance") or "normal")

    def _severity():
        if (dd is not None and dd <= p["deep_bear_dd"]
                and breadth is not None and breadth < p["deep_bear_breadth"]):
            return "深熊"
        return "熊"

    if state == "熊市确认":
        return {"stance": "bear", "severity": _severity(),
                "why": f"熊市确认进入（回撤 {_fmt(dd)}，宽度 {_fmt(breadth)}）"}
    if state == "牛市确认":
        return {"stance": "bull", "severity": "牛",
                "why": f"牛市确认进入（偏离 {_fmt(dev)}，宽度 {_fmt(breadth)}）"}
    if prev_stance == "bear":
        back = (dd is not None and breadth is not None
                and dd > p["exit_bear_dd"] and breadth > p["exit_bear_breadth"])
        if not back:
            return {"stance": "bear", "severity": _severity(),
                    "why": (f"滞回保留熊市：退出需回撤 > {_fmt(p['exit_bear_dd'])} 且宽度 > "
                            f"{_fmt(p['exit_bear_breadth'])}（现回撤 {_fmt(dd)}、宽度 {_fmt(breadth)}）")}
        return {"stance": "normal", "severity": "普通",
                "why": (f"退出熊市：回撤 {_fmt(dd)} > {_fmt(p['exit_bear_dd'])} 且宽度 "
                        f"{_fmt(breadth)} > {_fmt(p['exit_bear_breadth'])}")}
    if prev_stance == "bull":
        off = (dev is not None and breadth is not None
               and dev < p["exit_bull_dev"] and breadth < p["exit_bull_breadth"])
        if not off:
            return {"stance": "bull", "severity": "牛",
                    "why": (f"滞回保留牛市：退出需偏离 < {_fmt(p['exit_bull_dev'])} 且宽度 < "
                            f"{_fmt(p['exit_bull_breadth'])}（现偏离 {_fmt(dev)}、宽度 {_fmt(breadth)}）")}
        return {"stance": "normal", "severity": "普通",
                "why": (f"退出牛市：偏离 {_fmt(dev)} < {_fmt(p['exit_bull_dev'])} 且宽度 "
                        f"{_fmt(breadth)} < {_fmt(p['exit_bull_breadth'])}")}
    return {"stance": "normal", "severity": "普通", "why": f"未确认（{state}）→ 按 MA200 三档"}


# ---------- 读缓存 ----------

def _latest(kind, code):
    fs = sorted(glob.glob(os.path.join(config.CACHE_DIR, kind, f"{code}__*.csv")))
    return fs[-1] if fs else None


def _as_of(path):
    base = os.path.basename(path or "")
    stem = os.path.splitext(base)[0]
    return stem.split("__")[-1] if "__" in stem else ""


def _read_close(path):
    import pandas as pd  # 延迟导入：--help 不需要 pandas
    df = pd.read_csv(path, usecols=["close"])
    return [v for v in pd.to_numeric(df["close"], errors="coerce").dropna().tolist()]


def index_metrics():
    out = []
    for code, name in P["indexes"]:
        path = _latest("index_daily", code)
        if not path:
            out.append({"code": code, "name": name, "close": None, "dev200": None,
                        "dd_2y": None, "as_of": "", "error": "无缓存"})
            continue
        close = _read_close(path)
        out.append({"code": code, "name": name,
                    "close": round(close[-1], 4) if close else None,
                    "dev200": dev_ma(close, P["ma_window"]),
                    "dd_2y": dd_from_high(close, P["dd_window"]),
                    "up_2y": up_from_low(close, P["dd_window"]),
                    "rows": len(close), "as_of": _as_of(path), "error": ""})
    return out


def breadth(limit=None):
    fs = sorted(glob.glob(os.path.join(config.CACHE_DIR, "daily", "*__4y__*.csv")))
    if limit:
        fs = fs[:int(limit)]
    flags = []
    for f in fs:
        try:
            close = _read_close(f)
        except Exception:
            continue
        d = dev_ma(close, P["ma_window"])
        if d is None:
            continue
        flags.append(d > 0)
    return {"ratio": breadth_ratio(flags), "above": sum(1 for f in flags if f),
            "total": len(flags)}


def macro_snapshot():
    """宏观只作背景（不进判据）：PMI / M1-M2 / 实际利率 / PPI-CPI 缺口。"""
    path = os.path.join(config.OUT_DIR, "macro.json")
    if not os.path.exists(path):
        return {"exists": False}
    try:
        d = json.load(io.open(path, encoding="utf-8"))
    except Exception as e:  # 坏文件不拖垮探针
        return {"exists": False, "error": f"{type(e).__name__}: {e}"}
    g = d.get("data") or {}
    pmi = g.get("pmi") or {}
    ms = g.get("money_supply") or {}
    cpi = g.get("cpi") or {}
    ppi = g.get("ppi") or {}
    return {"exists": True, "generated_at": d.get("generated_at"),
            "pmi_manufacturing": pmi.get("manufacturing"), "pmi_month": pmi.get("month"),
            "m1_yoy": ms.get("m1_yoy"), "m2_yoy": ms.get("m2_yoy"),
            "m1_m2_gap": d.get("m1_m2_gap"),
            "cpi_yoy": cpi.get("yoy"), "ppi_yoy": ppi.get("yoy"),
            "real_rate": d.get("real_rate"),
            "note": d.get("note")}


# ---------- 渲染 ----------

def _pct(x, digits=1):
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def load_doc(path=None):
    """读上一次的判定产物（用于滞回记忆）；缺失/损坏返回 None。"""
    path = path or OUT_JSON
    try:
        if os.path.exists(path):
            return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        pass
    return None


def write_doc(doc, path=None, md=None):
    path = path or OUT_JSON
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="").write(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    io.open(OUT_MD, "w", encoding="utf-8", newline="").write(md if md is not None else render_md(doc))
    return path


def build(limit=None, prev=None):
    idx = index_metrics()
    main = next((r for r in idx if r["code"] == P["main_index"]), idx[0] if idx else {})
    br = breadth(limit=limit)
    a = assess(main.get("dev200"), main.get("dd_2y"), br.get("ratio"), up=main.get("up_2y"))
    st = stance_of(a["state"], main.get("dev200"), main.get("dd_2y"), br.get("ratio"), prev=prev)
    return {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "basis": "仅本地缓存，0 取数",
            "main_index": P["main_index"],
            "indexes": idx, "breadth": br,
            "state": a["state"], "explain": a["explain"],
            "bear": a["bear"], "bull": a["bull"],
            # 档位立场（带幅度与滞回）
            "stance": st["stance"], "severity": st["severity"], "stance_why": st["why"],
            "prev_stance": str((prev or {}).get("stance") or ""),
            # 旧字段（浏览器/文档引用兼容）：verdict 等价 state、bear_flag 等价熊市确认
            "verdict": a["state"], "bear_flag": a["bear"]["confirmed"],
            "thresholds": {"dd_bear": P["dd_bear"], "dev_bear": P["dev_bear"],
                           "breadth_bear": P["breadth_bear"],
                           "bull_low_rebound": P["bull_low_rebound"], "bull_dev": P["bull_dev"],
                           "bull_breadth": P["bull_breadth"], "min_hits": P["min_hits"],
                           "deep_bear_dd": P["deep_bear_dd"],
                           "deep_bear_breadth": P["deep_bear_breadth"],
                           "exit_bear_dd": P["exit_bear_dd"],
                           "exit_bear_breadth": P["exit_bear_breadth"]},
            "macro": macro_snapshot(),
            "note": "判定产物：档位映射在 plan.market_regime（深熊→25%、熊→防守 35%、牛→进攻 80%、"
                    "普通/未确认→按 MA200 三档）；滞回状态保存在本文件 stance 字段"} 


def _side_md(title, r):
    lines = [f"**{title}**：命中 {r['hits']}/{r['usable']} 条"
             + ("" if r["confirmed"] is not None else "（可用判据不足 2 条）")]
    for key, label, val, th, hit in r["conditions"]:
        lines.append(f"- {'✔' if hit else '✘'} {label} {_pct(val)}（阈值 {_pct(th)}）")
    return lines


def render_md(doc):
    lines = [f"# 牛熊确认探针（{doc['state']}）", ""]
    lines.append("| 指数 | 收盘 | 距 MA200 | 距 2 年高点 | 距 2 年低点 |")
    lines.append("|---|---|---|---|---|")
    for r in doc["indexes"]:
        lines.append(f"| {r['name']}（{r['code']}） | {r['close']} | {_pct(r['dev200'])} | "
                     f"{_pct(r['dd_2y'])} | {_pct(r.get('up_2y'))} |")
    br = doc["breadth"]
    lines.append("")
    lines.append(f"- 全市场宽度：{br['above']}/{br['total']} 只站在 MA200 上方（{_pct(br['ratio'])}）")
    lines.append("")
    lines.extend(_side_md("熊市判据", doc["bear"]))
    lines.append("")
    lines.extend(_side_md("牛市判据", doc["bull"]))
    lines.append("")
    lines.append(f"→ **结论：{doc['state']}**")
    lines.append(f"  {doc['explain']}")
    lines.append("")
    lines.append(f"→ **档位立场：{doc.get('stance')}（{doc.get('severity')}）** — {doc.get('stance_why')}")
    lines.append("- 下期档位上限：深熊 25% / 熊 35% / 牛 80% / 普通按 MA200 三档"
                 "（滞回：进熊看确认，退熊要回撤回到 -10% 以内且宽度 > 45%）")
    m = doc.get("macro") or {}
    if m.get("exists"):
        lines.append(f"- 宏观背景（不进判据）：PMI 制造 {m.get('pmi_manufacturing')}"
                     f"｜M1-M2 {m.get('m1_m2_gap')}pp｜CPI {m.get('cpi_yoy')}% / PPI {m.get('ppi_yoy')}%"
                     f"｜实际利率 {m.get('real_rate')}%")
    lines.append("")
    lines.append("> 探针只做判定；档位映射在 `plan.market_regime()`：深熊→25%、熊→防守 35%、"
                 "牛→进攻 80%、普通/未确认→按 MA200 三档（带滞回：进熊看确认，退熊要回撤回到 "
                 "-10% 以内且宽度 > 45%）。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="牛熊确认探针（只读）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON")
    ap.add_argument("--no-write", action="store_true", help="不写 out/ 文件")
    ap.add_argument("--limit", type=int, default=None, help="宽度抽样只数（默认全部）")
    a = ap.parse_args(argv)
    prev = load_doc()
    doc = build(limit=a.limit, prev=prev)
    md = render_md(doc)
    if not a.no_write:
        write_doc(doc, md=md)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print(md)
        if not a.no_write:
            print(f"产物：{OUT_JSON}\n      {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
