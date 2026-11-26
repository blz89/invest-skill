# -*- coding: utf-8 -*-
"""HTML 复盘报告：把一期决策渲染成一份自包含的 HTML（分析 + 购买策略 + 未来计划）。

设计原则
- 只读：不改账本、不下单、不取数（数据全部来自 out/ 产物与账本）。
- 自包含：内联 CSS，没有外部图片/字体/JS，双击就能看，也能直接打印成 PDF。
- 数字全部来自 out/stage3_plan.json、out/stage2_judged.csv、账本 state_*.json，
  不在渲染层做任何二次计算（除了仓位/收益这类账本本来就算好的数）。

用法
    python report_html.py --ledger 50k            # 用 out/ 最新计划 + 账本渲染
    python report_html.py --ledger all            # 每个已配置账本各出一份（本部署只有 50k）
weekly.py 决策时也会自动调用 write_report()，产出 out/paper/report_<日期>.html。
"""
import argparse
import csv
import html
import io
import json
import os
import re
import sys
import datetime

import breakdown
import config
import otc_book
import paper

CFG = config.PAPER
OUT = config.OUT_DIR
POUT = CFG.get("package_dir") or os.path.join(OUT, "paper")
LEDGERS = CFG.get("ledgers") or {}
BAD = {"回避", "数据不足"}


# --------------------------------------------------------------------------- 小工具
def _esc(x):
    return html.escape("" if x is None else str(x))


def _num(x, nd=2, dash="—"):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return dash
    return f"{v:,.{nd}f}"


def _money(x, nd=2):
    return _num(x, nd, "—")


def _pct(x, nd=2, arrow=False):
    """x 是小数（0.0123 → 1.23%）。"""
    try:
        v = float(x) * 100.0
    except (TypeError, ValueError):
        return "—"
    s = f"{v:+.{nd}f}%" if arrow else f"{v:.{nd}f}%"
    return s


def _cls(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


def _load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return None


def _load_csv(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def ledger_suffix(ledger):
    return "" if ledger == CFG.get("default_ledger", "50k") else f"_{ledger}"


def report_path(ledger, as_of, kind="report"):
    return os.path.join(POUT, f"{kind}{ledger_suffix(ledger)}_{as_of}.html")


def next_decision_day(as_of):
    """as_of 之后最近的决策日（**每周三、周六** 20:30，返回 (日期, 说明)）。"""
    try:
        d = datetime.date.fromisoformat(str(as_of))
    except Exception:
        return None, ""
    for i in range(1, 8):
        n = d + datetime.timedelta(days=i)
        if n.weekday() in (2, 5):
            return n.isoformat(), ("周三" if n.weekday() == 2 else "周六")
    return None, ""


_CAL_CACHE = None


def _trade_calendar():
    """交易日历（ISO 字符串升序）；取不到返回 None（调用方退化为只跳周末）。"""
    global _CAL_CACHE
    if _CAL_CACHE is None:
        try:
            import datasource as _ds
            df = _ds.fetch_trade_calendar()
            _CAL_CACHE = sorted(str(x)[:10] for x in df["trade_date"].tolist())
        except Exception:                        # noqa: BLE001 - 日历缺失退回近似
            _CAL_CACHE = []
    return _CAL_CACHE or None


def next_fill_day(decision_day):
    """决策日之后第一个交易日（优先交易日历；取不到日历时退化为只跳周末）。"""
    try:
        d = datetime.date.fromisoformat(str(decision_day))
    except Exception:
        return None
    key = d.isoformat()
    cal = _trade_calendar()
    if cal:
        for x in cal:
            if x > key:
                return x
        return None
    for i in range(1, 15):
        n = d + datetime.timedelta(days=i)
        if n.weekday() < 5:
            return n.isoformat()
    return None


# --------------------------------------------------------------------------- CSS
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--muted:#6b7785;--line:#e3e7ec;
 --up:#c62828;--down:#1b7f3b;--accent:#1f5fa8;--warn:#b26a00;--ok:#1b7f3b}
*{box-sizing:border-box}
body{margin:0;padding:28px 18px 60px;background:var(--bg);color:var(--ink);
 font:15px/1.65 "Microsoft YaHei","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC",system-ui,sans-serif}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}
h2{font-size:17px;margin:26px 0 10px;padding-left:9px;border-left:4px solid var(--accent)}
h3{font-size:15px;margin:16px 0 6px;color:#2a3542}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0;
 box-shadow:0 1px 2px rgba(20,30,50,.04)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .k{color:var(--muted);font-size:12px}
.kpi .v{font-size:21px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.kpi .n{color:var(--muted);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f0f3f7;font-weight:600;color:#33414f;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:#fafbfd}
.up{color:var(--up);font-weight:600}
.down{color:var(--down);font-weight:600}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:12px;border:1px solid var(--line);background:#f7f9fb}
.tag.buy{color:var(--up);border-color:#f0c8c8;background:#fdf3f3}
.tag.watch{color:var(--warn);border-color:#f0dfc0;background:#fdf8ef}
.tag.avoid{color:var(--muted);background:#f4f5f7}
.note{color:var(--muted);font-size:13px}
.warn{color:var(--warn)}
.ok{color:var(--ok);font-weight:600}
.bad{color:var(--up);font-weight:600}
ul{margin:6px 0 6px 20px;padding:0}
li{margin:3px 0}
.foot{margin-top:28px;padding-top:12px;border-top:1px dashed var(--line);color:var(--muted);font-size:12.5px}
details{margin:14px 0;border:1px solid var(--line);border-radius:10px;padding:10px 16px;background:var(--card)}
summary{cursor:pointer;color:var(--muted);font-size:14px;font-weight:600}
details[open] summary{margin-bottom:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
@media print{body{background:#fff;padding:0}.card,.kpi{box-shadow:none}.wrap{max-width:none}}
"""


def _doc(title, body):
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">"
        f"{body}"
        "<div class=\"foot\">本报告由 invest 阶段的脚本自动生成，是模拟盘记录，"
        "不构成投资建议、不承诺收益。所有数字来自本地账本与本地取数缓存，"
        "成交价口径见正文说明。</div></div></body></html>"
)


# --------------------------------------------------------------------------- 周报各段
def _position_text(state, pf) -> str:
    """仓位 + 含在挂/场外目标敞口（口径见 paper.position_exposure）。"""
    ratio, exposure = paper.position_exposure(state, pf)
    txt = f"仓位 {_pct(ratio)}"
    if exposure - ratio > 0.0005:
        txt += f"（含在挂目标敞口 {_pct(exposure)}）"
    return txt


def _position_note(state, pf) -> str:
    ratio, exposure = paper.position_exposure(state, pf)
    note = f"持仓市值 {_money(pf.get('market_value'))}"
    if exposure - ratio > 0.0005:
        note += f"；含在挂目标敞口 {_pct(exposure)}"
    return note


def _section_kpi(pf, state, cash_yield, cash_int=None):
    tr = pf.get("total_return")
    br = pf.get("bench_return")
    ex = (tr - br) if (tr is not None and br is not None) else None
    res = []
    res.append(("净值", _money(pf.get("nav")), f"本金 {_money(state['meta'].get('capital'))}"))
    res.append(("累计收益", _pct(tr, arrow=True), f"同期沪深300 {_pct(br, arrow=True)}", _cls(tr)))
    res.append(("超额", _pct(ex, arrow=True), "策略 − 基准", _cls(ex)))
    res.append(("仓位", _pct(pf.get("market_value", 0) / pf["nav"] if pf.get("nav") else 0),
                _position_note(state, pf), ""))
    # 利息双行：与周报正文共用 paper.cash_interest_split()。原来只印「本期利息 0.00」，
    # 而同一页「逆回购」小节写着利息 XX 元——两处口径不一致（B 类 #12）。
    if cash_int:
        _bk = float(cash_int.get("booked") or 0.0)
        _pd = float(cash_int.get("pending") or 0.0)
        _tt = float(cash_int.get("total") if cash_int.get("total") is not None
                    else _bk + _pd)
        res.append(("现金", _money(pf.get("cash")),
                    f"已落账利息 {_money(_bk)} + 在途 {_money(_pd)} "
                    f"= 合计 {_money(_tt)}", ""))
    else:
        res.append(("现金", _money(pf.get("cash")), f"本期利息 {_money(cash_yield)}", ""))
    res.append(("回撤", _pct(pf.get("drawdown"), arrow=True), "相对历史峰值", _cls(pf.get("drawdown"))))
    cells = []
    for item in res:
        k, v, n = item[0], item[1], item[2]
        cls = item[3] if len(item) > 3 else ""
        cells.append(
            f'<div class="kpi"><div class="k">{_esc(k)}</div>'
            f'<div class="v {cls}">{_esc(v)}</div><div class="n">{_esc(n)}</div></div>'
)
    return '<div class="kpis">' + "".join(cells) + "</div>"


def _section_market(plan, investable):
    tier = plan.get("tier") or {}
    reg = plan.get("market_regime") or {}
    rows = [
        ("市场状态", f"{reg.get('regime') or '—'}（仓位上限 {_pct(reg.get('base_gross'))}）"),
        ("沪深300", f"{_num(reg.get('bench_price'))} vs 200 日均线 {_num(reg.get('bench_ma200'))}"
                    f"（偏离 {_pct(reg.get('dev'), arrow=True)}）"),
        ("档位", f"{tier.get('tier') or '—'} 档（{tier.get('tier_label') or '—'}，"
                 f"目标 {_pct(tier.get('equity_ratio'))}"
                 f"{'，被市场状态压回' if tier.get('capped_by_regime') else ''}）"),
        ("权益预算", f"{_money(tier.get('equity_amount'))}｜现金 {_money(tier.get('cash_amount'))}"),
    ]
    if investable and investable.get("investable"):
        inv = investable["investable"]
        extra = ""
        if investable.get("gain"):
            extra = (f"（本金 {_money(investable.get('base'))} + 已实现盈利 "
                     f"{_money(investable.get('gain'))}，{_pct(investable.get('growth_pct'), arrow=True)}）")
        rows.append(("可投资本", f"{_money(inv)} {extra}"))
    if plan.get("pick_note"):
        rows.append(("选股口径", plan.get("pick_note")))
    body = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _section_analysis(funnel, judged, plan=None):
    out = []
    if funnel:
        keys = [
            ("股票池", "全市场/指数成分"), ("取数成功", "数据拿到的"),
            ("L3_valuation", "估值分位过关"), ("全部门槛通过", "进入阶段二"),
        ]
        body = "".join(
            f"<tr><td>{_esc(k)}</td><td class='num'>{_esc(funnel.get(k, '—'))}</td>"
            f"<td class='note'>{_esc(desc)}</td></tr>" for k, desc in keys
)
        out.append("<h3>漏斗</h3><table><tr><th>层级</th><th class='num'>数量</th><th>说明</th></tr>"
                   + body + "</table>")
        note = ("漏斗表里的数字是「单独通过该条判据的数量」，不是累计；"
                "进阶段二是九道门槛的交集。")
        out.append(f'<div class="note">{_esc(note)}</div>')
    if judged:
        buy = [r for r in judged if r.get("verdict") == "买入区"]
        watch = [r for r in judged if r.get("verdict") == "观察"]
        avoid = [r for r in judged if r.get("verdict") in BAD]
        out.append(
            f"<h3>阶段二结论分布：买入区 <b>{len(buy)}</b> 只｜观察 <b>{len(watch)}</b> 只"
            f"｜回避/数据不足 <b>{len(avoid)}</b> 只</h3>"
)
        for title, rows in (("买入区（≥70 分）", buy), ("观察（58–69 分）", watch), ("回避（<58 或有否决项）", avoid)):
            if not rows:
                continue
            body = ""
            for r in rows:
                tag = "buy" if title.startswith("买入") else ("watch" if title.startswith("观察") else "avoid")
                body += (
                    "<tr>"
                    f"<td>{_esc(r.get('code'))}</td><td>{_esc(r.get('name'))}</td>"
                    f"<td>{_esc(r.get('industry'))}</td>"
                    f"<td><span class='tag {tag}'>{_esc(r.get('verdict'))}</span></td>"
                    f"<td class='num'>{_num(r.get('total'))}</td>"
                    f"<td class='num'>{_num(r.get('quality'))}</td>"
                    f"<td class='num'>{_num(r.get('growth'))}</td>"
                    f"<td class='num'>{_num(r.get('valuation'))}</td>"
                    f"<td class='num'>{_num(r.get('risk'))}</td>"
                    f"<td class='num'>{_num(r.get('price'), 3)}</td>"
                    f"<td class='num'>{_num(r.get('buy_price'), 3)}</td>"
                    f"<td class='num'>{_num(r.get('target_price'), 3)}</td>"
                    f"<td class='num'>{_num(r.get('stop_price'), 3)}</td>"
                    f"<td class='note'>{_esc(r.get('veto') or '')}</td>"
                    "</tr>"
)
            out.append(
                f"<h3>{_esc(title)}</h3><table><tr><th>代码</th><th>名称</th><th>行业</th><th>结论</th>"
                "<th class='num'>总分</th><th class='num'>质量</th><th class='num'>成长</th>"
                "<th class='num'>估值</th><th class='num'>风险</th><th class='num'>现价</th>"
                "<th class='num'>买入价</th><th class='num'>目标价</th><th class='num'>止损价（占位）</th>"
                "<th>否决/备注</th></tr>" + body + "</table>"
)
        # ---- 全候选排名表：直接回答「为什么组合里总是这几只 / 为什么只有 1 只买入区」----
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        ranked = sorted(judged, key=lambda r: -_f(r.get("total")))
        sel = {}
        for p in (plan or {}).get("positions") or []:
            if p.get("layer") == "个股":
                sel[p.get("code")] = p
        rows = ""
        for i, r in enumerate(ranked, 1):
            total = _f(r.get("total"))
            p = sel.get(r.get("code"))
            if p:
                cell = f"<span class='ok'>✓ 第 {p.get('rank') or '—'} 名</span>"
            else:
                cell = "—"
            tag = ("buy" if r.get("verdict") == "买入区"
                   else ("watch" if r.get("verdict") == "观察" else "avoid"))
            rows += (
                "<tr>"
                f"<td class='num'>{i}</td><td>{_esc(r.get('code'))}</td>"
                f"<td>{_esc(r.get('name'))}</td>"
                f"<td><span class='tag {tag}'>{_esc(r.get('verdict'))}</span></td>"
                f"<td class='num'>{_num(total)}</td>"
                f"<td class='num'>{total - 70:+.2f}</td>"
                f"<td>{cell}</td>"
                "</tr>"
)
        note = (
            "买入线 <b>70 分</b>是绝对门槛（不看排名）；组合里的个股按<b>总分排名</b>"
            "取前 N 只（3–5 只，随权益档位：≤35% 3 只、≤60% 4 只、>60% 5 只；权重系数 1.0/0.85/0.70/0.55，第 4/5 名沿用 0.55），单票仍受本金 10% 上限约束。"
            "同一份数据下排名完全稳定——连续几周看到同一只排第一，是评分粘性"
            "（基本面与估值分位变化慢），不是系统卡住；数据是否更新看报告头部的数据日期。"
)
        out.append(
            "<h3>全候选排名（按总分，含距买入线差值）</h3>"
            "<table><tr><th class='num'>排名</th><th>代码</th><th>名称</th><th>结论</th>"
            "<th class='num'>总分</th><th class='num'>距买入线</th><th>本期组合</th></tr>"
            + rows + "</table>"
            f'<div class="note">{note}</div>'
)
    return "".join(out)


def _section_strategy(plan, targets):
    out = []
    rows = []
    for t in (targets or []):
        rows.append(
            "<tr>"
            f"<td>{_esc(t.get('code'))}</td><td>{_esc(t.get('name'))}</td>"
            f"<td>{_esc(t.get('kind') or t.get('layer'))}</td>"
            f"<td class='num'>{_num(t.get('shares'), 0)}</td>"
            f"<td class='num'>{_num(t.get('price'), 3)}</td>"
            f"<td class='num'>{_money(t.get('amount'))}</td>"
            f"<td class='num'>{_num(t.get('stop_price'), 3)}</td>"
            f"<td class='num'>{_num(t.get('target_price'), 3)}</td>"
            f"<td class='num'>{_num(t.get('score'))}</td>"
            "</tr>"
)
    if rows:
        out.append(
            "<h3>目标组合（本期要持有的东西）</h3>"
            "<table><tr><th>代码</th><th>名称</th><th>类型</th><th class='num'>目标股数</th>"
            "<th class='num'>参考价</th><th class='num'>目标金额</th><th class='num'>止损价（占位）</th>"
            "<th class='num'>目标价</th><th class='num'>评分</th></tr>" + "".join(rows) + "</table>"
)
    # 三批建仓价
    ladder_rows = []
    for t in (targets or []):
        for lg in (t.get("ladder") or []):
            ladder_rows.append(
                "<tr>"
                f"<td>{_esc(t.get('code'))}</td><td>{_esc(t.get('name'))}</td>"
                f"<td class='num'>{_esc(lg.get('tranche'))}</td>"
                f"<td class='num'>{_pct(lg.get('ratio'), 0)}</td>"
                f"<td class='num'>{_num(lg.get('price'), 3)}</td>"
                f"<td class='num'>{_num(lg.get('shares'), 0)}</td>"
                f"<td class='num'>{_money(lg.get('amount'))}</td>"
                f"<td class='note'>{_esc(lg.get('state'))}</td>"
                "</tr>"
)
    if ladder_rows:
        out.append(
            "<h3>三批建仓价（40% / 35% / 25%，跌到价才买）</h3>"
            "<table><tr><th>代码</th><th>名称</th><th class='num'>批次</th><th class='num'>比例</th>"
            "<th class='num'>触发价</th><th class='num'>股数</th><th class='num'>金额</th>"
            "<th>说明</th></tr>" + "".join(ladder_rows) + "</table>"
)
    # ETF 承接层
    etf = plan.get("etf_layer") or {}
    if etf.get("note") or etf.get("pool_note"):
        out.append(
            "<h3>买不起一手时的 ETF 承接</h3>"
            f'<div class="card"><div>{_esc(etf.get("note") or "")}</div>'
            f'<div class="note">{_esc(etf.get("pool_note") or "")}</div></div>'
)
    unf = plan.get("unaffordable") or []
    if unf:
        body = "".join(
            f"<tr><td>{_esc(u.get('code'))}</td><td>{_esc(u.get('name'))}</td>"
            f"<td class='num'>{_num(u.get('price'), 3)}</td>"
            f"<td class='num'>{_money(u.get('lot_cost'))}</td>"
            f"<td class='num'>{_money(u.get('budget'))}</td>"
            f"<td class='note'>{_esc(u.get('note') or '')}</td></tr>" for u in unf
)
        out.append(
            "<h3>一手超单票上限、本期买不了的个股</h3>"
            "<table><tr><th>代码</th><th>名称</th><th class='num'>现价</th>"
            "<th class='num'>1 手成本</th><th class='num'>单票预算</th><th>说明</th></tr>"
            + body + "</table>"
)
    # 现金层
    cash = plan.get("cash_layer") or {}
    if cash:
        _cont = cash.get("container") or "policy"
        _side = cash.get("cash_side_name") or "场外货基"
        _is_policy = _cont in ("policy", "repo7")
        _is_pool = _cont == "policy_pool"
        _term = int(cash.get("repo_term_days") or 1)
        _rates = cash.get("rates") or {}
        _title = ("现金层分工（挂单资金 + 建仓弹药→朝朝宝 T+0；观望资金→全市场货基池，90 天口径前 3 支、每支 ≤1 万快赎）"
                  if _is_pool else
                  "现金层分工（挂单资金→朝朝宝 T+0；观望资金→7 天期以上逆回购）"
                  if _is_policy else "现金层（人工指定容器，回滚口径）")
        _repo_gross = _rates.get("repo7_gross") if _is_policy else _rates.get("repo_gross")
        _parts = [
            f"<h3>{_title}</h3>",
            "<table>",
            f"<tr><th>现金合计</th><td class='num'>{_money(cash.get('cash_total'))}</td></tr>",
        ]
        if _is_policy or _is_pool:
            _parts.append(
                f"<tr><th>其中 T+0 保留</th><td class='num'>{_money(cash.get('t0_keep'))}"
                f"（建仓弹药 {_money(cash.get('ammo_amount'))} / 挂单冻结 {_money(cash.get('reserved_amount'))}，"
                f"取较大者；留{_esc(_side)}，随时可成交）</td></tr>")
        if _is_pool:
            _hold = cash.get("sleeve_holdings") or []
            _hbody = "".join(
                f"<tr><td>{_esc(h.get('name'))}</td><td class='num'>{_money(h.get('amount'))}</td>"
                f"<td class='num'>{_pct(h.get('rate'))}</td></tr>" for h in _hold)
            _parts += [
                f"<tr><th>半流动性货基</th><td>{_money(cash.get('sleeve_amount'))}"
                f"（加权 {_pct(cash.get('sleeve_rate'))}，数据日 {_esc(cash.get('sleeve_data_date'))}）"
                f"<table><tr><th>基金</th><th>金额</th><th>90 天实得年化</th></tr>{_hbody}</table></td></tr>",
                f"<tr><th>{_esc(_side)}</th><td>{_money(cash.get('money_fund_amount'))}，"
                f"预计年收益 {_money(cash.get('money_fund_gross_year'))}"
                f"（{_pct(cash.get('money_fund_net_rate'))}）</td></tr>",
                f"<tr><th>合计年化</th><td>{_pct(cash.get('net_yield'))}"
                f"（约 {_money(cash.get('total_net_year'))} 元/年）</td></tr>",
                f"<tr><th>注意</th><td class='note'>{_esc(cash.get('note') or '')}</td></tr>",
                "</table>",
            ]
            out.append("".join(_parts))
        else:
            _repo_head = "国债逆回购" + (f"（{_term} 天期）" if cash.get("repo_lots") else "")
            _parts += [
                f"<tr><th>{_repo_head}</th>"
                f"<td>{_num(cash.get('repo_lots'), 0)} 手 × 1,000 元 = {_money(cash.get('repo_amount'))}，"
                f"毛 {_pct(_repo_gross)} − 年费 {_pct(cash.get('repo_fee_annual_rate'))}"
                f"（{_money(cash.get('repo_fee_year'))} 元/年）→ 预计年收益 {_money(cash.get('repo_net_year'))}</td></tr>",
                f"<tr><th>{_esc(_side)}</th><td>{_money(cash.get('money_fund_amount'))}，"
                f"预计年收益 {_money(cash.get('money_fund_gross_year'))}"
                f"（{_pct(cash.get('money_fund_net_rate'))}）</td></tr>",
                f"<tr><th>合计年化</th><td>{_pct(cash.get('net_yield'))}"
                f"（约 {_money(cash.get('total_net_year'))} 元/年）</td></tr>",
                f"<tr><th>注意</th><td class='note'>{_esc(cash.get('note') or '')}</td></tr>",
                "</table>",
            ]
            out.append("".join(_parts))
    # 费用
    fee = plan.get("fee_summary") or {}
    if fee:
        out.append(
            "<h3>费用</h3>"
            "<table>"
            f"<tr><th>一次性买入</th><td class='num'>{_money(fee.get('buy_fee'))} 元</td>"
            f"<th>来回（买+卖）</th><td class='num'>{_money(fee.get('round_trip_fee'))} 元"
            f"（本金 {_pct(fee.get('round_trip_pct_of_capital'))}）</td></tr>"
            f"<tr><th>说明</th><td colspan='3' class='note'>{_esc(fee.get('note') or '')}</td></tr>"
            "</table>"
)
        hf = fee.get("high_fee_orders") or []
        if hf:
            body = "".join(
                f"<tr><td>{_esc(o.get('code'))}</td><td>{_esc(o.get('name'))}</td>"
                f"<td class='num'>{_money(o.get('amount'))}</td>"
                f"<td class='num'>{_money(o.get('fee'))}</td>"
                f"<td class='num warn'>{_pct(o.get('ratio'))}</td></tr>" for o in hf[:12]
)
            _mc = float(getattr(config, "CAPITAL", {}).get("min_commission") or 0.0)
            _why = (f"最低佣金 {_mc:.0f} 元在小单上很贵" if _mc > 0
                    else "账户已免五；能进这张表说明费用结构仍值得看一眼")
            out.append(
                "<div class='note'>佣金占比 &gt;0.5% 的订单（" + _why + "）：</div>"
                "<table><tr><th>代码</th><th>名称</th><th class='num'>金额</th>"
                "<th class='num'>佣金</th><th class='num'>占比</th></tr>" + body + "</table>"
)
    return "".join(out)


def _section_orders(orders, fill_note, mode):
    if not orders:
        return ("<h3>订单与挂单</h3><div class='card'>本期没有新订单"
                "（持仓与挂单已满足目标，或对照账本 once 模式不调仓）。</div>")
    body = "".join(
        "<tr>"
        f"<td>{_esc(o.get('layer') or '')}</td>"
        f"<td>{_esc(o.get('code'))}</td><td>{_esc(o.get('name'))}</td>"
        f"<td>{_esc(o.get('action'))}</td>"
        f"<td class='num'>{_esc(o.get('tranche') or '')}</td>"
        f"<td class='num'>{_num(o.get('price'), 3)}"
        + (f"<br><span class='note'>累计 {_num(o.get('price_cum'), 4)}</span>"
           if o.get("price_cum") is not None else "")
        + "</td>"
        f"<td class='num'>{_num(o.get('lots'), 0)}</td>"
        f"<td class='num'>{_num(o.get('shares'), 0)}</td>"
        f"<td class='num'>{_money(o.get('amount'))}</td>"
        "</tr>" for o in orders
)
    note = fill_note or (
        f"成交口径 `PAPER.fill_price={CFG.get('fill_price')}`："
        "决策每周三、周六 20:30 各一次（数据用交易日 19:00 本机统一取数的收盘）→ 次一交易日挂限价单/开盘卖出。"
        "**买入一律限价条件单**：开盘不高于限价按开盘价成交、否则盘中触及限价按限价成交，"
        "未触及则挂到失效日；**卖出**（减仓/清仓）按次一交易日开盘价，止损单盘中触价即卖。"
        "本报告里的价格是**决策日参考价**，不等于成交价。"
)
    return (
        "<h3>订单与挂单</h3>"
        f'<div class="note">{_esc(note)}</div>'
        "<table><tr><th>层</th><th>代码</th><th>名称</th><th>动作</th><th class='num'>批次</th>"
        "<th class='num'>参考价</th><th class='num'>手数</th><th class='num'>股数</th>"
        "<th class='num'>金额</th></tr>" + body + "</table>"
        f'<div class="note">调仓模式：{_esc(mode or CFG.get("rebalance_mode"))}</div>'
)


def _section_future(plan, targets, state, as_of, decision_day="", skip_next=False):
    out = []
    nd, nd_name = next_decision_day(decision_day or as_of)
    if nd and not skip_next:
        fd = next_fill_day(nd)
        out.append(
            "<div class='card'>"
            f"<b>下一决策日：{_esc(nd)}（{_esc(nd_name)} 20:30）</b>"
            f"{'，' + _esc(fd) + ' 起卖出按开盘价、买入按限价条件单' if fd else ''}。"
            "决策日会先体检（doctor）并核对工作日已取数据（决策日不取数），NO-GO 就停下，不会带着脏数据下单。</div>"
)
    # 操作触发点
    rows = []
    for t in (targets or []):
        stop = t.get("stop_price")
        rows.append(
            "<tr>"
            f"<td>{_esc(t.get('code'))}</td><td>{_esc(t.get('name'))}</td>"
            f"<td class='num'>{_num(t.get('shares'), 0)}</td>"
            f"<td class='num'>{_num(stop, 3)}</td>"
            f"<td class='num'>{_num(t.get('target_price'), 3)}</td>"
            f"<td class='note'>{_esc((t.get('falsifiers') or [''])[0] if t.get('falsifiers') else '')}</td>"
            "</tr>"
)
    if rows:
        out.append(
            "<h3>触发价与可证伪条件（到了就执行，不临时改主意）</h3>"
            "<table><tr><th>代码</th><th>名称</th><th class='num'>目标股数</th>"
            "<th class='num'>止损价（占位）</th><th class='num'>目标价</th><th>第一条可证伪条件</th></tr>"
            + "".join(rows) + "</table>"
)
    rc = plan.get("risk_controls") or {}
    if rc:
        rc_rows = [
            ("单票上限", _pct(rc.get("单票上限"))),
            ("行业上限", _pct(rc.get("行业上限"))),
            ("总敞口区间", f"{_pct(rc.get('总敞口区间')[0])} – {_pct(rc.get('总敞口区间')[1])}"
                          if isinstance(rc.get("总敞口区间"), (list, tuple)) and len(rc.get("总敞口区间") or []) == 2 else "—"),
            ("组合回撤熔断", _pct(rc.get("组合回撤熔断"), arrow=True)),
            ("单票止损", _pct(rc.get("单票止损"), arrow=True)),
            ("时间止损", str(rc.get("时间止损"))),
            ("日亏停手", _pct(rc.get("日亏停手"), arrow=True)),
            ("周亏减仓", _pct(rc.get("周亏减仓"), arrow=True)),
            ("连亏 3 笔", str(rc.get("连亏3笔停手"))),
            ("止损后现金", "不重新分配（不摊平/不补仓）" if rc.get("止损后现金不重新分配") else "—"),
        ]
        out.append(
            "<h3>组合级风控线</h3><table>"
            + "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rc_rows)
            + "</table>"
)
    out.append(
        "<h3>验证口径（不承诺收益）</h3>"
        "<ul>"
        "<li>12 周窗口内看四个数：超额（策略 − 沪深300）、最大回撤、胜率 × 盈亏比、费用占比；</li>"
        "<li>总收益必须拆成「权益层盈亏」与「现金层利息」两块看——现金利息不是策略 alpha；</li>"
        "<li>样本不足（&lt;12 周或 &lt;10 笔平仓）时只记录、不下结论；</li>"
        "<li>任何阈值/规则改动都要走一次 git 提交（纪律见 references/engine-notes.md §六），不许边跑边调。</li>"
        "</ul>"
)
    return "".join(out)


def _section_future_brief(plan, targets, state, as_of, decision_day=""):
    """首屏③：下一决策日 + 现有持仓的止损触发表（风控线/验证口径/计划触发价放折叠区）。"""
    nd, nd_name = next_decision_day(decision_day or as_of)
    fd = next_fill_day(nd) if nd else None
    out = []
    if nd:
        out.append(
            f"<div class='note'><b>下一决策日：{_esc(nd)}（{_esc(nd_name)} 20:30）</b>"
            + (f"，{_esc(fd)} 起卖出按开盘价、买入按限价条件单。" if fd else "。")
            + "</div>")
    hold = state.get("holdings") or {}
    rows = []
    for code, h in sorted(hold.items(), key=lambda kv: -(float(kv[1].get("shares") or 0))):
        rows.append(
            "<tr>"
            f"<td>{_esc(h.get('name') or code)}</td>"
            f"<td class='num'>{_num(h.get('shares'), 0)}</td>"
            f"<td class='num'>{_num(h.get('stop_price'), 3)}</td>"
            f"<td class='num'>{_num(h.get('target_price'), 3)}</td>"
            "</tr>")
    if rows:
        out.append(
            "<table><tr><th>名称</th><th class='num'>股数</th>"
            "<th class='num'>止损价</th><th class='num'>目标价</th></tr>"
            + "".join(rows) + "</table>")
        out.append("<div class='note'>跌破止损就卖（按次日开盘价）；到目标价不卖，靠移动止损锁盈。</div>")
    else:
        out.append("<div class='note'>当前无持仓，等下次决策建仓。</div>")
    return "".join(out)


def _section_changes(changes):
    if not changes:
        return ("<h2>六、与上期相比的变化</h2><div class='card'>首次决策，没有上期快照可比。</div>")
    items = "".join(f"<li>{_esc(c)}</li>" for c in changes)
    return (
        "<h3>与上期相比的变化（复盘原料）</h3>"
        f"<div class='card'><ul>{items}</ul>"
        "<div class='note'>这里只陈述变化，不据此改阈值。</div></div>"
)


# --------------------------------------------------------------------------- 周报入口
def _auto_net(state, ledger):
    """未显式传入时按 budget.json 累计 spent 现算净利润；失败返回 None。"""
    try:
        import billing
        import score
        cap = float((state.get("meta") or {}).get("capital") or 0.0)
        spent = float(billing.budget_state(ledger, cap).get("spent_cny") or 0.0)
        ret = float((paper.stats(state) or {}).get("total_return") or 0.0)
        ev = score.evaluate(ret, spent, cap)
        return {"gross": ev["gross_profit"], "spent": spent, "net": ev["net_profit"],
                "rate": ev["net_rate"], "grade": ev["grade"],
                "reward_pending": ev["reward_pending"], "reward_note": ev["reward_note"]}
    except Exception:
        return None


def _section_summary(pf, state, net=None, cash_int=None, cash_yield=0.0):
    """收益与消耗（核心）：把净值/收益/回撤/token/净利润放同一张表，一眼看完。"""
    tr = pf.get("total_return")
    br = pf.get("bench_return")
    ex = (tr - br) if (tr is not None and br is not None) else None
    nav = pf.get("nav") or 0.0
    cap = float(state["meta"].get("capital") or 0)
    gross = (nav - cap) if nav else None
    ann = pf.get("annualized")
    pos = (pf.get("market_value") or 0) / nav if nav else 0.0
    rows = [
        ("净值 / 本金", f"{_money(nav)} / {_money(cap)}"),
        ("账户收益", f"{_money(gross)} 元（{_pct(tr, arrow=True)}"
                    + (f"，年化 {_pct(ann, arrow=True)}）" if ann is not None else "）"),
         _cls(tr)),
        ("同期沪深300 / 超额", f"{_pct(br, arrow=True)} / {_pct(ex, arrow=True)}", _cls(ex)),
        ("最大回撤 / 仓位", f"{_pct(pf.get('drawdown'), arrow=True)} / {_pct(pos)}"),
    ]
    if cash_int:
        _bk = float(cash_int.get("booked") or 0.0)
        _pd = float(cash_int.get("pending") or 0.0)
        rows.append(("现金", f"{_money(pf.get('cash'))}（已落账利息 {_money(_bk)} + 在途 {_money(_pd)}）"))
    else:
        rows.append(("现金", f"{_money(pf.get('cash'))}（本期利息 {_money(cash_yield)}）"))
    if net:
        rows.append(("净利润 / 净收益率",
                     f"{_money(net.get('net'))} 元 / {_pct(net.get('rate'), arrow=True)}",
                     _cls(net.get("net"))))
    body = ""
    for item in rows:
        k, v = item[0], item[1]
        cls = item[2] if len(item) > 2 else ""
        body += f"<tr><th>{_esc(k)}</th><td class='num {cls}'>{v}</td></tr>"
    try:
        import config as _cfg
        _cr = _cfg.cash_rates()
        _r1 = _cr.get("r1")
        _side_name = "朝朝宝" if _r1 else "货基"
        _mf = float(_r1) if _r1 else float(_cr.get("money_fund") or 0.0)
        _dep = float(_cfg.PERF.get("deposit_rate") or 0.0)
        _cash = float(pf.get("cash") or 0.0)
        if _mf and _dep and _cash:
            body += (f"<tr><td colspan='2' class='note'>现金层对照：现金 {_money(_cash)} 元按{_side_name} "
                     f"{_pct(_mf)} ≈ {_money(_cash * _mf)} 元/年；同等本金按定存线 {_pct(_dep)} ≈ "
                     f"{_money(_cash * _dep)} 元/年（差 {_money(_cash * (_mf - _dep))} 元/年）——"
                     f"现金层结构性跑不过存款，达标只能靠 R2/R3（SKILL §8.4）。</td></tr>")
    except Exception:                               # noqa: BLE001 - 对照行失败不影响主表
        pass
    return f"<table>{body}</table>"


def _section_hero(pf, state, net=None):
    """首屏四个数：账户收益 / token 实花 / 净利润 / 超额（其余数字进折叠区）。"""
    tr = pf.get("total_return")
    br = pf.get("bench_return")
    ex = (tr - br) if (tr is not None and br is not None) else None
    cap = float((state.get("meta") or {}).get("capital") or 0)
    nav = pf.get("nav") or 0.0
    gross = (nav - cap) if (nav and cap) else None
    spent = (net or {}).get("spent")
    net_v = (net or {}).get("net")
    rate = (net or {}).get("rate")
    items = [
        ("账户收益", f"{_money(gross)} 元" if gross is not None else "—",
         _pct(tr, arrow=True), _cls(tr)),
        ("token 实花", f"{_money(spent)} 元" if spent is not None else "—",
         "运行时实扣，不是计提", ""),
        ("净利润", f"{_money(net_v)} 元" if net_v is not None else "—",
         f"净收益率 {_pct(rate)}" if rate is not None else "已扣 token", _cls(net_v)),
        ("超额", _pct(ex, arrow=True), "策略 − 沪深300", _cls(ex)),
    ]
    cells = "".join(
        f'<div class="kpi"><div class="k">{_esc(k)}</div>'
        f'<div class="v {cls}">{_esc(v)}</div><div class="n">{_esc(n)}</div></div>'
        for k, v, n, cls in items
    )
    pos = ((pf.get("market_value") or 0) / nav) if nav else 0.0
    extra = (f"最大回撤 {_pct(pf.get('drawdown'), arrow=True)}｜仓位 {_pct(pos)}"
             f"｜现金 {_money(pf.get('cash'))}")
    return f'<div class="kpis">{cells}</div><div class="note" style="margin-top:10px">{extra}</div>'


def _section_holdings(state):
    """现在拿着什么：按市值排序的持仓表 + 三层资金一行（事实来自账本，不是计划）。"""
    hold = state.get("holdings") or {}
    navv = (state.get("nav") or [{}])[-1]
    mv = float(navv.get("market_value") or 0.0)
    otc = float(navv.get("otc_value") or 0.0)
    nav = float(navv.get("nav") or 0.0)
    cash = float(state.get("cash") or 0.0)
    pos = (mv + otc) / nav if nav else 0.0
    if not hold:
        return ("<div class='note'>当前无持仓。</div>"
                f"<div class='note' style='margin-top:8px'>现金 {_money(cash)}｜仓位 {_pct(pos)}</div>")
    rows = []
    for code, h in sorted(
            hold.items(),
            key=lambda kv: -(float(kv[1].get("shares") or 0) * float(kv[1].get("last_close") or 0))):
        cost = float(h.get("cost_basis") or 0)
        px = float(h.get("last_close") or cost or 0)
        pnl = (px / cost - 1) if cost else 0.0
        rows.append(
            "<tr>"
            f"<td>{_esc(h.get('name') or code)}</td>"
            f"<td class='num'>{_num(h.get('shares'), 0)}</td>"
            f"<td class='num'>{_num(cost, 3)}</td>"
            f"<td class='num'>{_num(px, 3)}</td>"
            f"<td class='num {_cls(pnl)}'>{_pct(pnl, arrow=True)}</td>"
            f"<td class='num'>{_num(h.get('stop_price'), 3)}</td>"
            "</tr>")
    return (
        "<table><tr><th>名称</th><th class='num'>股数</th><th class='num'>成本</th>"
        "<th class='num'>现价</th><th class='num'>盈亏</th><th class='num'>止损</th></tr>"
        + "".join(rows) + "</table>"
        + (f"<div class='note' style='margin-top:8px'>权益 {_money(mv)}｜场外债基 {_money(otc)}"
           f"｜现金 {_money(cash)}｜仓位 {_pct(pos)}</div>")
    )


def build_report(*, as_of, ledger, plan, state, judged=None, funnel=None,
                 targets=None, orders=None, changes=None, investable=None,
                 mode="", cash_yield=0.0, cash_int=None, fill_note="", repo=None,
                 data_as_of="", decision_day="", week=None, navst=None, net=None):
    if net is None:
        net = _auto_net(state, ledger)
    cfg = LEDGERS.get(ledger) or {}
    label = cfg.get("label") or ledger
    pf = paper.portfolio(state, {})
    stats = paper.stats(state)
    title = f"投资周报（模拟盘）· {as_of} · {label}账本"
    data_txt = f"数据截至：{_esc(data_as_of or as_of)}（最近交易日）收盘"
    stale_html = ""
    if data_as_of and data_as_of < as_of:
        stale_html = (
            f"<div class='warn'>! 数据不是最新交易日：报告日期 {_esc(as_of)}，"
            f"实际价格数据只到 {_esc(data_as_of)}（多为本机交易日 19:00 统一取数失败/未更新；"
            f"决策日不重新取数——先在工作日补跑本机取数，再重新决策）。"
            f"本期结论按 {_esc(data_as_of)} 数据口径，不代表今天收盘后的状态。</div>"
)
    head = [
        f"<h1>{_esc(title)}</h1>",
        '<div class="sub">'
        f"{_esc(label)}账本｜调仓模式：{_esc(mode or cfg.get('rebalance_mode') or CFG.get('rebalance_mode'))}｜"
        f"{data_txt}｜"
        f"生成：{_esc(datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))}"
        "</div>",
        stale_html,
    ]
    one = (
        f"净值 <b>{_money(pf.get('nav'))}</b>（累计 {_pct(pf.get('total_return'), arrow=True)}，"
        f"同期沪深300 {_pct(pf.get('bench_return'), arrow=True)}）"
        + (f"｜净利润 {_money(net.get('net'))} 元（已扣运行时成本）"
           if net else "")
        + f"｜本期新订单 {len(orders or [])} 张。"
)
    body = [
        "".join(head),
        f'<div class="card"><b>一句话：</b>{one}</div>',
        "<h2>① 这周赚了没</h2>",
        f"<div class='card'>{_section_hero(pf, state, net=net)}</div>",
        "<h2>② 现在拿着什么</h2>",
        f"<div class='card'>{_section_holdings(state)}</div>",
        "<h2>③ 下期什么条件动手</h2>",
        f"<div class='card'>{_section_future_brief(plan, targets, state, as_of, decision_day)}</div>",
        "<details><summary>看明细：计划与订单 / 收益拆解 / 筛选打分 / 账本表现 / 风控 / 场外</summary>",
        "<h3>计划（市场状态、档位、目标组合、建仓价、现金层）</h3>",
        f"<div class='card'>{_section_market(plan, investable)}{_section_strategy(plan, targets)}"
        f"{_section_future(plan, targets, state, as_of, decision_day, skip_next=True)}</div>",
        "<h3>本期操作</h3>",
        _section_orders(orders, fill_note, mode),
        _section_changes(changes),
        "<h3>收益与消耗明细</h3>",
        f"<div class='card'>{_section_summary(pf, state, net=net, cash_int=cash_int, cash_yield=cash_yield)}</div>",
        _section_netprofit(net),
        "<h3>筛选与打分</h3>",
        f"<div class='card'>{_section_analysis(funnel, judged, plan)}</div>",
        _section_navstats(navst),
        _section_breakdown(state),
        _section_week(week),
        _section_repo(repo, as_of),
        "<h3>风险等级评审（R2/R3）</h3>",
        f"<div class='card'>{_section_risk(plan)}</div>",
        _section_otc(state),
        "</details>",
        "<h2>边界</h2>",
        "<div class='card note'>模拟盘，不构成投资建议；触发价不是保证成交价（跳空按开盘价）；"
        "仅 A 股主板 + 场内 ETF（本部署本金 5 万）。</div>",
    ]
    return _doc(title, "".join(body))


def _section_navstats(navst) -> str:
    """账本表现（按每日巡检的日度净值）：渲染逻辑全在 navstats.py，本文件只做拼装。"""
    try:
        import navstats as NS
        return NS.render_html(navst or {})
    except Exception as exc:                        # noqa: BLE001
        return f"<h2>账本表现</h2><div class='card note'>未生成（{_esc(exc)}）</div>"


def _section_breakdown(state) -> str:
    """收益拆解（权益 / 场外 R2 / 现金三层）：2026-11-08周报固定体现。"""
    try:
        return breakdown.render_html(breakdown.compute(state))
    except Exception as exc:                        # noqa: BLE001
        return f"<h2>收益拆解</h2><div class='card note'>未生成（{_esc(exc)}）</div>"


def _section_netprofit(net) -> str:
    """净利润口径：账户收益 − token 实花。评分/档位/奖励只在年报出，周报不显示（2026-11-22 定，2026-11-26 计分时点改年报）。"""
    if not net:
        return ""
    return (
        "<h2>账本表现（净利润口径）</h2>"
        "<div class='card'>"
        f"净利润 <b>{_money(net.get('net'))}</b> 元；"
        f"净收益率 <b>{_pct(net.get('rate'))}</b>。"
        "<br><span class='note'>口径：净利润 = 账户累计收益 − 运行时实扣（真实扣费，不是年度计提）；"
        "评分 / 档位 / 奖励只在年报出（每年 1 月 1 日），周报不展开运行时费用。</span>"
        "</div>")


def _section_week(week) -> str:
    """七、本周巡检汇总（周一到周五）：净值逐日 + 周合计 + 告警数；没有记录时返回空串。"""
    if not week or not (week.get("days") or []):
        return ""
    lgs = week.get("ledgers") or {}
    if not lgs:
        return ""
    by_day = {lg: {r["date"]: r for r in (v.get("rows") or [])} for lg, v in lgs.items()}
    head = ["日期"] + [f"{lg} 净值" for lg in lgs] + [f"{lg} 累计" for lg in lgs]
    rows = ["<tr>" + "".join(f"<th>{_esc(h)}</th>" for h in head) + "</tr>"]
    for d in week["days"]:
        cells = [f"<td>{_esc(d)}</td>"]
        for lg in lgs:
            rr = by_day[lg].get(d)
            cells.append("<td>%.4f</td>" % (rr["nav"] / lgs[lg]["capital"]) if rr else "<td>—</td>")
        for lg in lgs:
            rr = by_day[lg].get(d)
            cells.append("<td>%+.2f%%</td>" % (rr["ret"] * 100) if rr else "<td>—</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    total = []
    for lg, v in lgs.items():
        total.append(f"<li><code>{_esc(lg)}</code>：周涨跌 {v['week_chg'] * 100:+.2f}%，"
                     f"周末净值 {_money(v['end_nav'])} 元，本周最大回撤 {v['mdd'] * 100:.2f}%，"
                     f"成交 {v['n_trades']} 笔</li>")
    rts = week.get("rates") or {}
    if rts:
        ks = sorted(rts)
        a, b = rts[ks[0]], rts[ks[-1]]
        total.append("<li>现金层利率（实测）：逆回购 %.2f%% → %.2f%%，货基 %.2f%% → %.2f%%</li>"
                     % (a["repo"] * 100, b["repo"] * 100,
                        a["money_fund"] * 100, b["money_fund"] * 100))
    return ("<h3>本周巡检汇总（周一到周五）</h3>"
            "<div class='card'><table>" + "".join(rows) + "</table>"
            f"<div class='note'>巡检告警 {week['n_alert']} 条（高 {week['n_high']} 条），"
            f"记录 {week['n_rows']} 天。</div>"
            "<ul>" + "".join(total) + "</ul></div>")


def _section_otc(state) -> str:
    """场外 R2 腿（影子账）：持仓 + 在挂单；没有场外腿时返回空串。"""
    if not state:
        return ""
    try:
        rows = otc_book.holdings_rows(state)
        pend = otc_book.pending(state)
    except Exception:
        return ""
    if not rows and not pend:
        return ""
    out = ["<h3>场外债券基金（R2 层影子账）</h3>", "<div class='card'>",
           "<div class='note'>按金额法记账：市值 = 投入金额 × (当日累计净值 / 买入日累计净值)；"
           "申购按提交日的下一交易日累计净值成交、T+1 确认；赎回费按持有天数取实测档"
           "（不足 90 天按最严档）。真实操作仍需在基金 APP 手动申购，APP 确认比账本晚 1–2 个交易日。</div>"]
    if rows:
        body = "".join(
            "<tr>"
            f"<td>{_esc(r['code'])}</td><td>{_esc(r['name'])}</td>"
            f"<td class='num'>{_money(r['amount'])}</td>"
            f"<td class='num'>{_money(r['value'])}</td>"
            f"<td class='num'>{_money(r['unrealized'])}（{_pct(r['unrealized_pct'], 2, arrow=True)}）</td>"
            f"<td class='num'>{_num(r.get('acc_nav'), 4)} → {_num(r.get('nav'), 4)}</td>"
            f"<td>{_esc(r['nav_date'])}</td>"
            f"<td class='num'>{_num(r['hold_days'], 0)}</td>"
            f"<td class='num'>{_pct(r['redeem_fee'])}</td>"
            "</tr>" for r in rows)
        out.append("<table><tr><th>代码</th><th>名称</th><th class='num'>投入</th>"
                   "<th class='num'>市值</th><th class='num'>浮动</th>"
                   "<th class='num'>累计净值（买入日 → 最新）</th><th>净值日</th>"
                   "<th class='num'>持有天数</th><th class='num'>赎回费率</th></tr>"
                   + body + "</table>")
        out.append(f"<div class='note'>场外合计：投入 {_money(otc_book.cost_total(state))} 元、"
                   f"市值 {_money(otc_book.market_value(state))} 元、"
                   f"浮动 {_money(otc_book.unrealized(state))} 元、"
                   f"已实现 {_money(otc_book.realized_total(state))} 元。"
                   "参考净值一列是<b>累计净值</b>（不是单位净值）：分红/拆分会压低单位净值，"
                   "市值与成交只看累计净值。</div>")
    if pend:
        items = "".join(
            f"<li><code>{_esc(o['id'])}</code> {_esc(o['code'])} {_esc(o['name'])} "
            f"{'申购' if o['action'] == 'buy' else '赎回'} {_money(o['amount'])} 元"
            f"（参考净值 单位 {_num(o.get('nav_ref'), 4)} / 累计 "
            f"{_num(o.get('nav_ref_cum'), 4)}，按 {_esc(o['effective_from'])} 累计净值成交）</li>"
            for o in pend)
        out.append(f"<b>在挂（{len(pend)} 张）：</b><ul>{items}</ul>")
    out.append("</div>")
    return "".join(out)


def _section_risk(plan):
    """风险等级评审：只做 R2/R3，R1 归现金层、R4/R5 不做。"""
    rv = plan.get("risk_review") or {}
    wl = plan.get("wealth_layer") or {}
    dropped = plan.get("risk_filter") or []
    if not rv:
        return "本期没有风险等级评审结果（旧版产物）。"
    ok = rv.get("ok")
    badge = ("<span class='ok'>✓ 通过</span>" if ok
             else "<span class='bad'>✗ 未通过 —— 本期不新建仓（fail-closed）</span>")
    rng = rv.get("range") or [0, 0]
    rows = [
        "<b>等级口径（金融产品 R1–R5 通用定义）：</b>按<b>产品类型/交易场所/板块</b>定级，"
        "不按自定义波动率阈值——R1 国债/存款/货币基金/逆回购（<b>现金层</b>）；"
        "R2 固定收益类理财/地方政府债/政策性金融债/高等级信用债（<b>场内债券 ETF 代理</b>）；"
        "R3 A 股<b>主板</b>股票（可转债、混合型基金不在本技能范围）；"
        "R4 创业板/科创板/北交所股票、权益类基金、跨境 ETF、港股通、融资融券；"
        "R5 期货/期权/分级基金/场外衍生品。",
        f"<b>投资者匹配：</b>{_esc(rv.get('investor_line') or 'C3 平衡型：可买 R1–R3，R4/R5 不买')}。"
        "创业板/科创板/北交所另有 <b>10–50 万元准入门槛</b>（+24 个月经验），"
                "创业板需 ≥10 万元、科创板/北交所/两融/个股期权需 ≥50 万元——本部署 5 万本金开不了权限。",
        "<b>本技能定位：</b>只做 <b>R2（中低风险）</b> 与 <b>R3（中风险）</b>；"
        "R1 的低收益由<b>现金层</b>（挂单资金/建仓弹药朝朝宝 T+0 + 观望资金全市场货基池）承担；"
        "逆回购只在季末/长假前的脉冲窗口做；R4/R5 <b>不参与</b>。",
        f"<b>评审结论：</b>{badge} 组合加权等级 <b>{_esc(rv.get('level_label'))}</b>"
        f"（R{float(rv.get('level') or 0):.2f}，允许 R{float(rng[0]):.1f}–R{float(rng[1]):.1f}）；"
        f"R2 占比 {_pct(rv.get('r2_share'), 1)}（目标 {_pct(rv.get('r2_target'), 0)}）。",
    ]
    w = rv.get("weights") or {}
    if w:
        dist = "｜".join(f"{_esc(k)} {_pct(v, 1)}" for k, v in sorted(w.items()))
        rows.append(f"<b>等级分布（占已投）：</b>{dist}")
    for v in (rv.get("violations") or []):
        rows.append(f"<div class='bad'>✗ {_esc(v)}</div>")
    for v in (rv.get("warnings") or []):
        rows.append(f"<div class='warn'>! {_esc(v)}</div>")
    items, seen = [], set()
    for p in (plan.get("positions") or []) + (plan.get("wealth_positions") or []):
        code = p.get("code")
        if code in seen:
            continue
        seen.add(code)
        lv = p.get("level") or "R3"
        amt = float(p.get("amount") or 0)
        items.append(f"<tr><td>{_esc(p.get('code'))}</td><td>{_esc(p.get('name'))}</td>"
                     f"<td>{_esc(lv)}</td><td>{_esc(p.get('layer') or '')}</td>"
                     f"<td>{_money(amt)}</td><td>{_esc(p.get('level_reason') or '')}</td></tr>")
    if items:
        rows.append("<table><tr><th>代码</th><th>名称</th><th>等级</th><th>层</th>"
                    "<th>金额</th><th>定级理由</th></tr>" + "".join(items) + "</table>")
    if wl:
        rows.append(f"<b>R2 理财层：</b>预算 {_money(wl.get('budget'))}，"
                    f"单只上限 {_pct(wl.get('single_cap'), 0)}（占本金），"
                    f"落地 {_money(wl.get('spent'))}。")
        if wl.get("note"):
            rows.append(f"<div class='warn'>{_esc(wl['note'])}</div>")
        rows.append("<div class='warn'>为什么 R2 优先用品内债券 ETF：场内可交易、有实时价、能按当日收盘算回撤，"
                    "费率和规模都可溯源。银行 R2 理财<b>没有可靠的 point-in-time 数据</b>（写进策略等于编数字），"
                    "不参与筛选。单只上限装不下一手场内 ETF 时，由<b>场外债券基金</b>承接同一份 R2 预算"
                    "（取数见 out/stage1_otc.csv；两条腿互斥、不重复持仓）。</div>")
    if dropped:
        names = "、".join(f"{_esc(d.get('code'))} {_esc(d.get('name'))}"
                          f"（{_esc(d.get('level'))}）" for d in dropped[:12])
        rows.append(f"<b>被等级筛掉的标的（{len(dropped)} 只，只列前 12）：</b>{names}")
    return "<br>".join(rows)


def _section_repo(repo, as_of):
    """现金层：逆回购盈亏分析（按持有天数计息，与权益层的交易窗口分开看）。"""
    head = "<h3>现金层：逆回购盈亏分析</h3>"
    note = (
        '<div class="card"><b>口径说明（为什么两层要用不同窗口）：</b><br>'
        "· <b>权益层</b>按「决策→成交→评估」窗口算：周三/周六 20:30 决策 → 次一交易日挂限价单/开盘卖出 → 下一个决策日收盘价评估；"
        "若决策日没有变更（继续持仓/观望），则本期只比「上一决策日收盘价 → 本决策日收盘价」。<br>"
        "· <b>现金层</b>按持有天数算：观望资金放全市场货基池（T+0 快赎每支 1 万/日）、朝朝宝顶流动性；"
        "逆回购只在季末/长假前的脉冲窗口操作（见技能 §5.7c）；"
        "长假前买更长档。跨期比较时把「覆盖天数」和"
        "「自然日跨度」分开报，否则会把周末/假期的利息误当成策略赚的。</div>")
    if not repo:
        return head + note + '<div class="card">本期还没有逆回购计息记录。</div>'
    kpi = (
        '<div class="kpis">'
        f'<div class="kpi"><div class="k">操作笔数</div><div class="v">{repo.get("ops")}{" +1 在途" if repo.get("open_lot") else ""}</div>'
        f'<div class="n">只在高息窗口日操作（1 天期优先，见 §5.7c）</div></div>'
        f'<div class="kpi"><div class="k">覆盖天数 / 自然日</div>'
        f'<div class="v">{repo.get("covered_days")} / {repo.get("span_days")}</div>'
        f'<div class="n">空转 {repo.get("idle_days")} 天（现金没进逆回购）</div></div>'
        f'<div class="kpi"><div class="k">累计净利息</div><div class="v">{_money(repo.get("net"))}</div>'
        f'<div class="n">利息 {_money(repo.get("interest"))} − 手续费 {_money(repo.get("fees"))}'
        f' + 货基 {_money(repo.get("mf_interest"))}</div></div>'
        f'<div class="kpi"><div class="k">实得年化</div><div class="v">{_pct(repo.get("effective_annualized"))}</div>'
        f'<div class="n">手续费侵蚀 {_pct(repo.get("fee_drag"))}</div></div>'
        "</div>")
    lots = repo.get("lots") or []
    if lots:
        rows = "".join(
            f"<tr><td>{_esc(l.get('date'))}</td><td>{_esc(l.get('mature'))}</td>"
            f"<td>{int(l.get('days') or 0)}</td><td>{_money(l.get('amount'))}</td>"
            f"<td>{_pct(l.get('rate'))}</td><td>{_num(l.get('interest'), 2)}</td>"
            f"<td>{_num(l.get('fee'), 2)}</td></tr>" for l in lots)
        table = ("<table><thead><tr><th>买入日</th><th>到期日</th><th>持有天数</th>"
                 "<th>本金</th><th>年化</th><th>利息(元)</th><th>手续费(元)</th></tr></thead>"
                 f"<tbody>{rows}</tbody></table>"
                 "<div class='note'>只列最近 20 笔已到期的 lot。持有天数 = 该 lot 的实际期限（7/14/28 天期；历史含旧规则 3/4 天期）。</div>")
    else:
        table = ""
    _cr = config.cash_rates()      # 逆回购利率唯一来源（不写死 2%）
    read = ('<div class="card"><b>怎么读：</b>'
            "①「空转天数」> 0 就是现金在账上白躺（或长假覆盖不完，要换更长档）；"
            "②「手续费侵蚀」= 手续费/利息，年费率按 7/14/28 天期 0.26%/本金（1 天期日滚 0.25%）；"
            f"③实得年化应接近逆回购利率 {_cr['repo']:.2%}（7 天期 {float(_cr.get('repo7') or 0):.2%}、"
            f"朝朝宝 {float(_cr.get('r1') or _cr.get('money_fund') or 0):.2%}；{_cr['as_of']} 口径的假设，"
            "不是未来收益承诺）减掉手续费，明显偏低说明有资金没进逆回购"
            "（被挂单占用、低于 1000 元门槛，或操作日没跟上）；"
            "④「在途」那笔要等下一个操作日到期才落现金，本表已把它的覆盖天数与应得利息算进来。</div>")
    return head + note + kpi + table + read


def args_state_placeholder(ledger):
    return (LEDGERS.get(ledger) or {}).get("state_file") or "—"


def write_report(path, **kw):
    html_txt = build_report(**kw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(html_txt)
    return path


# --------------------------------------------------------------------------- 月报
def _inline_md(escaped):
    """已转义文本上的行内 markdown：`**粗体**`、`` `代码` ``。"""
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _md_block(text):
    """极简 markdown → HTML（月报 retro 小节用）。

    为什么不用 <pre>：retro.md 是给人看的 markdown，原样塞进 <pre> 会把 `##`、`**`、
    反引号直接显示出来（月报核对时发现）。这里只做够用的转换：
    小标题 → h4~h6、粗体/行内代码 → <strong>/<code>、无序列表 → <ul><li>、其余成段。
    """
    if not text:
        return ""
    out, in_ul = [], False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in str(text).splitlines():
        ln = raw.rstrip()
        m = re.match(r"^(#{2,6})\s+(.*)$", ln)
        if m:
            close_ul()
            lvl = min(4 + len(m.group(1)) - 2, 6)  # ## → h4，### → h5，更深 → h6
            out.append(f"<h{lvl}>{_inline_md(_esc(m.group(2)))}</h{lvl}>")
            continue
        if not ln.strip():
            close_ul()
            continue
        lm = re.match(r"^\s*[-*]\s+(.*)$", ln)
        if lm:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(_esc(lm.group(1)))}</li>")
            continue
        close_ul()
        out.append(f"<p>{_inline_md(_esc(ln))}</p>")
    close_ul()
    return "\n".join(out)


def _border_card() -> str:
    return ("<h2>边界</h2><div class='card note'>模拟盘，不构成投资建议；触发价不是保证成交价"
            "（跳空按开盘价）；仅 A 股主板 + 场内 ETF（本部署本金 5 万）。</div>")


def _m_metrics_table(m) -> str:
    rows = [
        ("月度收益", f"{_pct(m.get('ret_month'), arrow=True)}（{_money(m.get('pnl_month'))} 元）"),
        ("总收益（累计，自建仓）", f"{_pct(m.get('total_return'), arrow=True)}"
                                f"（{_money(m.get('total_pnl'))} 元；本金 {_money(m.get('capital'))} 元）"),
        ("同期沪深300", _pct(m.get("bench_month"), arrow=True)),
        ("超额", _pct(m.get("excess"), arrow=True)),
        ("月内最大回撤", _pct(m.get("mdd"), arrow=True)),
        ("期末仓位", _pct(m.get("weight_end"))),
        ("现金层利息", f"{_money(m.get('cash_yield'))} 元"),
        ("已实现盈亏", f"{_money(m.get('realized'))} 元"),
        ("浮动盈亏", f"{_money(m.get('unrealized'))} 元"),
        ("费用", f"{_money(m.get('fees'))} 元（本金 {_pct(m.get('fees_pct'))}）"),
        ("成交笔数", f"{m.get('trades', 0)} 笔（买 {m.get('buys', 0)} / 卖 {m.get('sells', 0)}）"),
        ("止损/熔断", f"{m.get('stop_outs', 0)} 次"),
        ("可投资本", f"{_money(m.get('investable'))}"
                    f"（本金 {_money(m.get('capital'))} + 已实现盈利 {_money(m.get('gain'))}）"),
    ]
    return ("<table>" + "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
            + "</table>")


_ACT_CN = {"buy": "买入", "sell": "卖出", "buy_open": "买入", "sell_open": "卖出",
           "buy_limit": "买入", "sell_stop": "卖出"}


def _act_cn(action) -> str:
    """成交动作中文化（账本存 buy/sell 等代码，报告给人看）。"""
    a = str(action or "")
    return _ACT_CN.get(a.lower(), a)


def _m_trades_table(trades) -> str:
    if not trades:
        return "<div class='note'>本月没有成交（挂单未触发或对照账本不调仓）。</div>"
    head = ("<table><tr><th>日期</th><th>名称</th><th>动作</th><th class='num'>股数</th>"
            "<th class='num'>价</th><th class='num'>金额</th><th class='num'>已实现</th></tr>")
    body = "".join(
        "<tr>"
        f"<td>{_esc(t.get('date'))}</td>"
        f"<td>{_esc(t.get('name') or t.get('code'))}</td>"
        f"<td>{_esc(_act_cn(t.get('action')))}</td>"
        f"<td class='num'>{_num(t.get('shares'), 0)}</td>"
        f"<td class='num'>{_num(t.get('price'), 3)}</td>"
        f"<td class='num'>{_money(t.get('amount'))}</td>"
        f"<td class='num {_cls(t.get('realized_pnl'))}'>{_money(t.get('realized_pnl'))}</td>"
        "</tr>" for t in trades)
    return head + body + "</table>"


def _monthly_main(m, info, data) -> list:
    """月报主区（周报式）：一句话 + ①赚没赚 + ②做了什么 + ③记分卡 + ④下月计划。"""
    nt = m.get("net") or {}
    parts = []
    one = (f"本月净值 <b>{_money(m.get('nav_end'))}</b>，账户收益 {_money(m.get('pnl_month'))} 元"
           f"（{_pct(m.get('ret_month'), arrow=True)}）；同期沪深300 {_pct(m.get('bench_month'), arrow=True)}，"
           f"超额 {_pct(m.get('excess'), arrow=True)}")
    if not nt.get("error") and nt.get("net_profit") is not None:
        one += (f"；累计净利润（扣 token 实花）{_money(nt.get('net_profit'))} 元"
                f"（已扣 token 实花 {_money(nt.get('token_spent'))} 元，净收益率 {_pct(nt.get('net_rate'))}）")
    one += f"；成交 {m.get('trades', 0)} 笔。"
    parts.append(f'<div class="card"><b>一句话：</b>{one}</div>')

    cells = "".join(
        f'<div class="kpi"><div class="k">{_esc(k)}</div><div class="v {_cls(c)}">{_esc(v)}</div>'
        f'<div class="n">{_esc(n)}</div></div>'
        for k, v, n, c in (
            ("月度收益", f"{_money(m.get('pnl_month'))} 元", _pct(m.get("ret_month"), arrow=True),
             m.get("ret_month")),
            ("累计收益", _pct(m.get("total_return"), arrow=True),
             f"{_money(m.get('total_pnl'))} 元（本金 {_money(m.get('capital'))} 元）", m.get("total_return")),
            ("同期沪深300", _pct(m.get("bench_month"), arrow=True), "同期基准", m.get("bench_month")),
            ("超额", _pct(m.get("excess"), arrow=True), "策略 − 沪深300", m.get("excess")),
        ))
    extra = (f"最大回撤 {_pct(m.get('mdd'), arrow=True)}｜期末仓位 {_pct(m.get('weight_end'))}"
             f"｜现金层利息 {_money(m.get('cash_yield'))} 元｜费用 {_money(m.get('fees'))} 元")
    parts.append("<h2>① 这个月赚了没</h2>")
    parts.append(f'<div class="card"><div class="kpis">{cells}</div>'
                 f'<div class="note" style="margin-top:10px">{extra}</div></div>')

    parts.append("<h2>② 这个月做了什么</h2>")
    trades = info.get("trades") or []
    parts.append(f"<div class='card'>{_m_trades_table(trades)}"
                 f"<div class='note' style='margin-top:8px'>"
                 f"费用 {_money(m.get('fees'))} 元｜已实现 {_money(m.get('realized'))} 元"
                 f"｜浮动 {_money(m.get('unrealized'))} 元｜止损/熔断 {m.get('stop_outs', 0)} 次</div></div>")

    sc = data.get("scorecard") or ""
    parts.append("<h2>③ 记分卡判定</h2>")
    if sc:
        lines = [x.strip() for x in sc.splitlines() if x.strip()]
        parts.append("<div class='card'>" + _md_block("\n".join(lines[:4])) + "</div>")
    else:
        parts.append("<div class='card note'>本月未生成记分卡。</div>")

    parts.append("<h2>④ 下月什么条件动手</h2>")
    nxt = data.get("next_month_plan") or [
        "每周三、周六 20:30 继续按同一套规则决策，不为单月结果调阈值；",
        "净值低于峰值 15% 触发组合熔断，当月不再开新仓；",
        "样本满 12 周且 ≥10 笔平仓后，才用记分卡做「可不可行」的判定；",
        "任何规则改动必须先走一次 git 提交（纪律见 references/engine-notes.md §六）。",
    ]
    parts.append("<div class='card'><ul>" + "".join(f"<li>{_esc(x)}</li>" for x in nxt) + "</ul></div>")
    return parts


def _monthly_details(data, ledgers) -> list:
    """月报明细（折叠）：复盘 / 技能优化 / 诊断 / 五问 / 本年计划 / 收益拆解 / 账本对比 / 完整指标。"""
    retro = data.get("retro") or {}
    sc = data.get("scorecard") or ""
    parts = ["<details><summary>看明细：复盘 / 技能优化 / 诊断 / 五问 / 本年计划 / "
             "收益拆解 / 按标的 / 完整指标</summary>"]
    parts.append("<h3>上月策略复盘与改进</h3>")
    if retro.get("strategy"):
        parts.append("<div class='card'>" + _md_block(retro["strategy"]) + "</div>")
    else:
        parts.append("<div class='card note'>⚠ 未找到 <code>out/team/retro.md</code> 的「策略复盘」小节"
                     "（先跑 T10 复盘员）。</div>")
    parts.append("<h3>技能优化（A 类已执行 / B 类待拍板）</h3>")
    if retro.get("optimization"):
        parts.append("<div class='card'>" + _md_block(retro["optimization"]) + "</div>")
        ost = data.get("opt_stats") or {}
        if ost.get("total"):
            parts.append(f"<div class='card'><b>清单完成度：{ost.get('done')}/{ost.get('total')}</b>"
                         "（A 类月报任务内直接改代码/文档并提交；B 类写 "
                         "<code>out/team/pending_changes.md</code> 等拍板）</div>")
    else:
        parts.append("<div class='card note'>⚠ 未找到 <code>out/team/retro.md</code> 的「技能优化」小节。</div>")
    diag = data.get("diagnostics") or []
    if diag:
        rows = []
        for d in diag:
            for item in (d.get("items") or []):
                # C7：历史诊断产物可能是 (level, text) 之外的形状，不能让报告整页崩
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    rows.append(f"<li><b>{_esc(d.get('label'))}</b>｜{_esc(item)}</li>")
                    continue
                _lvl, _txt = item[0], item[1]
                rows.append(f"<li>{'⚠ ' if _lvl == 'warn' else ''}"
                            f"<b>{_esc(d.get('label'))}</b>｜{_esc(_txt)}</li>")
        parts.append("<h3>自动诊断信号（代码只报事实，判断在复盘）</h3>"
                     "<div class='card'><ul>" + "".join(rows) + "</ul></div>")
    parts.append("<h3>策略可行性判定（五问）</h3>")
    _v = data.get("viability") or ""
    if _v:
        parts.append("<div class='card'>" + _md_block(_v) + "</div>")
    else:
        parts.append("<div class='card warn'>⚠ 未找到 <code>out/team/viability_&lt;月&gt;.md</code>："
                     "月报必须回答「策略还可行吗」（五问 + 结论三选一）。</div>")
    parts.append("<h3>本年计划对齐</h3>")
    _ap = data.get("annual_plan") or ""
    if _ap:
        parts.append("<div class='card'>" + _md_block(_ap) + "</div>")
    else:
        parts.append("<div class='card'>本年度无年度计划（年报未生成）；下一年 1 月 1 日年报会写 "
                     "<code>out/team/annual_plan.md</code>。</div>")
    if sc:
        parts.append("<h3>记分卡全文</h3><div class='card'>" + _md_block(sc) + "</div>")
    for name, inf in ledgers.items():
        mm = inf.get("month") or {}
        parts.append(f"<h3>{_esc(inf.get('label') or name)}账本（{_esc(name)}）</h3>"
                     f"<div class='card'>{_m_metrics_table(mm)}</div>")
        bd = inf.get("breakdown") or {}
        if bd:
            try:
                parts.append("<h4>收益拆解（权益 / 场外 R2 / 现金三层）</h4>"
                             "<div class='card'>" + breakdown.table_html(bd)
                             + f"<div class='note'>{_esc(breakdown.note_text(bd))}</div></div>")
            except Exception as exc:                    # noqa: BLE001
                print(f"[report_html] 收益拆解生成失败，已跳过：{exc}", file=sys.stderr)
                parts.append(f"<div class='card note'>收益拆解未生成（{_esc(exc)}）</div>")
        by_sym = inf.get("by_symbol") or []
        if by_sym:
            body = "".join(
                f"<tr><td>{_esc(s.get('code'))}</td><td>{_esc(s.get('name'))}</td>"
                f"<td class='num'>{_money(s.get('realized_pnl'))}</td>"
                f"<td class='num'>{s.get('trades', 0)}</td></tr>" for s in by_sym)
            parts.append("<h4>本月已实现盈亏（按标的）</h4>"
                         "<table><tr><th>代码</th><th>名称</th>"
                         "<th class='num'>已实现</th><th class='num'>成交笔数</th></tr>" + body + "</table>")
        parts.append(
            "<div class='card'><b>拆开看（不许只报总收益）：</b>"
            f"本月 现金层利息 {_money(mm.get('cash_yield'))} 元 + 权益层盈亏 "
            f"{_money(mm.get('equity_pnl'))} 元 ≈ 月度收益 "
            f"{_pct(mm.get('ret_month'), arrow=True)}（{_money(mm.get('pnl_month'))} 元）｜"
            f"<b>总收益（累计，自建仓）{_pct(mm.get('total_return'), arrow=True)}"
            f"（{_money(mm.get('total_pnl'))} 元）</b></div>")
    parts.append("</details>")
    return parts


def build_monthly_report(*, month, data):
    """月报 HTML（简单版，与周报同风格）：首屏短，明细折叠。"""
    ledgers = data.get("ledgers") or {}
    title = f"投资月报（模拟盘）· {month}"
    start, end = data.get("start"), data.get("end")
    names = list(ledgers.keys())
    primary = names[0] if names else None
    info = ledgers.get(primary) or {}
    m = info.get("month") or {}
    label = info.get("label") or primary or ""
    parts = [f"<h1>{_esc(title)}</h1>",
             '<div class="sub">'
             f"统计区间：{_esc(start)} ~ {_esc(end)}｜账本：{_esc(label)}｜"
             f"生成：{_esc(datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))}"
             "</div>"]
    if m and not m.get("no_data"):
        parts.extend(_monthly_main(m, info, data))
    else:
        parts.append("<div class='card warn'>本月没有可读的净值/交易数据"
                     "（账本刚重置或该月无净值点，报告不渲染虚假的 0.00% 收益）；"
                     "复盘/五问等文字仍见下方明细。</div>")
    parts.extend(_monthly_details(data, ledgers))
    parts.append(_border_card())
    return _doc(title, "".join(parts))


def write_monthly_report(path, **kw):
    txt = build_monthly_report(**kw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(txt)
    return path


def build_annual_report(*, year, data):
    """年报（每年 1 月 1 日，年度结算日）：年度收益 + 累计总收益 + 逐月分解 + 结算记录。"""
    title = f"投资年报（模拟盘）· {year}（年度结算）"
    parts = [f"<h1>{_esc(title)}</h1>",
             f'<div class="sub">统计区间：{_esc(data.get("start"))} ~ {_esc(data.get("end"))}｜'
             f"生成：{_esc(datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))}</div>"]
    settlement = data.get("settlement") or {}
    for name, info in (data.get("ledgers") or {}).items():
        y = info.get("year") or {}
        nt = y.get("net") or {}
        parts.append(f"<h2>{_esc(info.get('label'))}账本（{_esc(name)}）</h2>")
        cards = [
            f'<div class="kpi"><div class="k">年度收益</div>'
            f'<div class="v {_cls(y.get("ret_month"))}">{_pct(y.get("ret_month"), arrow=True)}</div>'
            f'<div class="n">{_money(y.get("pnl_month"))} 元</div></div>',
            f'<div class="kpi"><div class="k">总收益（累计，自建仓）</div>'
            f'<div class="v {_cls(y.get("total_return"))}">{_pct(y.get("total_return"), arrow=True)}</div>'
            f'<div class="n">{_money(y.get("total_pnl"))} 元</div></div>',
            f'<div class="kpi"><div class="k">净利润（扣 token）</div>'
            f'<div class="v">{_money(nt.get("net_profit"))} 元</div>'
            f'<div class="n">净收益率 {_pct(nt.get("net_rate"), arrow=True)}'
            + (f'｜等价年化 {_pct(nt.get("net_rate_ann"), arrow=True)}' if nt.get("annualized") else "")
            + f'｜档位 {_esc(nt.get("grade") or "n/a")}</div></div>',
            f'<div class="kpi"><div class="k">超额（vs 沪深300）</div>'
            f'<div class="v {_cls(y.get("excess"))}">{_pct(y.get("excess"), arrow=True)}</div>'
            f'<div class="n">同期 {_pct(y.get("bench_month"), arrow=True)}</div></div>',
        ]
        parts.append('<div class="kpis">' + "".join(cards) + "</div>")
        reward = (f"{_money(nt.get('reward_cny'))} 元（全部转次年 token 预算）"
                  if not nt.get("reward_pending") else (nt.get("reward_note") or "评估期未到"))
        rows = [
            ("年度收益", f"{_pct(y.get('ret_month'), arrow=True)}（{_money(y.get('pnl_month'))} 元；"
                         f"净值 {_money(y.get('nav_start'))} → {_money(y.get('nav_end'))}）"),
            ("总收益（累计，自建仓）", f"{_pct(y.get('total_return'), arrow=True)}"
                                    f"（{_money(y.get('total_pnl'))} 元；本金 {_money(y.get('capital'))} 元）"),
            ("同期沪深300 / 超额", f"{_pct(y.get('bench_month'), arrow=True)} / {_pct(y.get('excess'), arrow=True)}"),
            ("年内最大回撤", _pct(y.get("mdd"), arrow=True)),
            ("费用", f"{_money(y.get('fees'))} 元（本金 {_pct(y.get('fees_pct'))}）"),
            ("成交", f"{y.get('trades', 0)} 笔（买 {y.get('buys', 0)} / 卖 {y.get('sells', 0)}）"),
            ("现金层利息 / 权益层盈亏", f"{_money(y.get('cash_yield'))} / {_money(y.get('equity_pnl'))} 元"),
            ("净利润 / 净收益率", f"{_money(nt.get('net_profit'))} 元 / {_pct(nt.get('net_rate'), arrow=True)}"
                              + (f"（等价年化 {_pct(nt.get('net_rate_ann'), arrow=True)}）"
                                 if nt.get("annualized") else "")),
            ("档位 / 奖励", f"{_esc(nt.get('grade') or 'n/a')} / {_esc(reward)}"),
        ]
        parts.append('<div class="card"><table>'
                     + "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
                     + "</table></div>")
        months = y.get("months") or []
        if months:
            body = "".join(
                f"<tr><td>{_esc(r.get('month'))}</td>"
                f"<td class='num'>{_money(r.get('nav_end'))}</td>"
                f"<td class='num {_cls(r.get('ret_month'))}'>{_pct(r.get('ret_month'), arrow=True)}</td>"
                f"<td class='num {_cls(r.get('total_return'))}'>{_pct(r.get('total_return'), arrow=True)}</td></tr>"
                for r in months)
            parts.append("<h3>月度分解</h3><table><tr><th>月份</th><th class='num'>月末净值</th>"
                         "<th class='num'>月度收益</th><th class='num'>累计总收益</th></tr>" + body + "</table>")
        s = settlement.get(name) or {}
        rec = s.get("record") or {}
        if rec:
            flag = "已结算过（不重复入账）" if s.get("already") else "本次结算"
            parts.append(f"<div class='card'><b>年度结算：</b>{_esc(flag)}｜结算日 {_esc(rec.get('settled_at'))}｜"
                         f"年度收益 {_pct(rec.get('year_return'), arrow=True)}｜"
                         f"总收益 {_pct(rec.get('total_return'), arrow=True)}｜"
                         f"净利润 {_money(rec.get('net_profit'))} 元｜奖励 {_money(rec.get('reward_cny'))} 元"
                         f"<br><span class='note'>本金 {_money(rec.get('capital_before'))} → "
                         f"{_money(rec.get('capital_after'))} 元（奖励从现金划出）；"
                         f"下一年收益率以剩余净值为起点，总收益按链式累计。"
                         f"结算记录写进账本 meta.annual_settlements.{_esc(str(year))}；"
                         f"账本已备份；旧年份不追溯重算。</span></div>")
        elif s.get("error"):
            parts.append(f"<div class='card warn'><b>年度结算失败：</b>{_esc(s['error'])}</div>")
        else:
            parts.append("<div class='card note'>本次未写年度结算记录（--no-settle 或未跑结算）。</div>")
    _v = data.get("viability") or ""
    parts.append("<h2>策略可行性判定（五问）</h2>")
    if _v:
        parts.append("<div class='card'>" + _md_block(_v) + "</div>")
    else:
        parts.append("<div class='card warn'>⚠ 未找到 <code>out/team/viability_&lt;年&gt;.md</code>："
                     "年报必须回答「策略还可行吗」（五问 + 结论三选一）。</div>")
    _sh = data.get("score_health") or ""
    parts.append("<h2>选股评分体检</h2>")
    if _sh:
        parts.append("<div class='card'>" + _md_block(_sh) + "</div>")
    else:
        parts.append("<div class='card note'>未生成评分体检：先归档 <code>out/archive/scoring/</code>，"
                     "再跑 <code>python scripts/score_panel.py --health</code>。面板不足 26 周时只维持现状。</div>")
    parts.append("<div class='card note'>年报是年度结算日：只读账本、不重新决策；"
                 "净利润 = 账户累计收益 − token 实花；奖励（评估期 2027-10-30 后）全部转次年 token 预算。</div>")
    return _doc(title, "".join(parts))


def write_annual_report(path, **kw):
    txt = build_annual_report(**kw)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(txt)
    return path


# --------------------------------------------------------------------------- CLI
def plan_targets(plan):
    """从阶段三计划里取出目标组合（个股 positions + ETF etf_positions，按代码去重、去掉 0 股）。"""
    targets, seen = [], set()
    for key in ("positions", "etf_positions"):
        for t in (plan.get(key) or []):
            code = t.get("code")
            if not code or code in seen:
                continue
            if not (t.get("shares") or t.get("ladder")):
                continue
            seen.add(code)
            targets.append(t)
    return targets


def load_context():
    """读 out/ 里的阶段一漏斗与阶段二打分（不存在就返回空）。"""
    return (
        _load_csv(os.path.join(OUT, "stage2_judged.csv")),
        _load_json(os.path.join(OUT, "stage1_funnel.json")),
)


def _state_path(ledger):
    cfg = LEDGERS.get(ledger) or {}
    return cfg.get("state_file") or ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 out/ 产物 + 账本渲染成 HTML 报告")
    ap.add_argument("--ledger", default=CFG.get("default_ledger", "50k"),
                    choices=list(LEDGERS.keys()) + ["all"])
    ap.add_argument("--state", default="", help="指定账本文件（默认按 --ledger 取配置）")
    ap.add_argument("--date", default="", help="决策日（默认取 out/ 最近一次产物的最后交易日）")
    ap.add_argument("--out", default="", help="输出 HTML 路径（默认 out/paper/report_<日期>.html）")
    a = ap.parse_args(argv)

    plan = _load_json(os.path.join(OUT, "stage3_plan.json"))
    if not plan:
        print("[report] 缺少 out/stage3_plan.json，先跑 run.py")
        return 1
    judged = _load_csv(os.path.join(OUT, "stage2_judged.csv"))
    funnel = _load_json(os.path.join(OUT, "stage1_funnel.json"))

    as_of = a.date
    if not as_of:
        cal_path = os.path.join(OUT, "stage3_plan.json")
        as_of = (plan.get("generated_at") or "")[:10]
    ledgers = list(LEDGERS.keys()) if a.ledger == "all" else [a.ledger]
    written = []
    for led in ledgers:
        path = a.state or _state_path(led)
        if not path or not os.path.exists(path):
            print(f"[report] 跳过 {led}：账本不存在 {path}")
            continue
        state = paper.load(path)
        targets = plan_targets(plan)
        out_path = a.out if (a.out and len(ledgers) == 1) else report_path(led, as_of)
        kw = dict(as_of=as_of, ledger=led, plan=plan, state=state, judged=judged,
                  funnel=funnel, targets=targets, orders=plan.get("orders") or [],
                  changes=None, investable=paper.investable_capital(state),
                  data_as_of=str(plan.get("data_as_of") or ""),
                  mode=(LEDGERS.get(led) or {}).get("rebalance_mode") or CFG.get("rebalance_mode"))
        p = write_report(out_path, **kw)
        written.append(p)
        print(f"[report] 已写出 {p}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
