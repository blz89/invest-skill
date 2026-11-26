# -*- coding: utf-8 -*-
"""每日巡检（纯代码、0 token）：把「已投资项目的交易情况」列成一张表。

为什么要有这个文件：
  决策每周两次（周三、周六 20:30），持仓每天都要盯——跌破止损、单票/行业超限、
  挂单过期、场外净值陈旧、回撤熔断。这些**全是确定性判断**，不该花 token 让模型
  每天读原始数据。所以巡检做成纯代码：每个交易日 20:00 由 DSH 巡检会话先跑 paper_exec.py
  撮合「已触发」的条件单（止损/限价买，未触发继续挂），再跑本文件，只出清单；
  除止损价被击穿（或熔断）外不做任何调仓，其余告警留到决策日处理。

日期口径（2026-10-31 定）：
  默认 `--date auto` = **最近一个交易日**（≤ 今天）。20:00 跑不跨日；即便被推迟到
  次日凌晨或碰上节假日，也只会落到「上一个交易日」，不会把周二的数据当成周一的。

它与其它模块的分工（行情/财务/估值由交易日 19:00 本机任务先统一取好（`stock prefetch` → `run.py` 只读缓存），本文件只读已落盘数据）：
  * `paper.py` 只管账本记账，不判断风险；
  * `risk.py` 只在选股准入时给等级，不看已持仓；
  * `plan.py` 是决策日算新计划，不是巡检；
  * 本文件**不下单**（巡检主体只读 + 出清单；唯一写操作是 mark_daily 记日净值/现金计息）。

产物：`out/patrol_latest.json`（机器读）+ `out/patrol_<日期>.md`（人读）。
退出码恒为 0：巡检是「报告工具」，不该因为发现风险就让调用方以为程序坏了。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import datasource  # noqa: E402
import etf  # noqa: E402
import industry  # noqa: E402
import otc_book  # noqa: E402
import paper  # noqa: E402

PATROL = getattr(config, "PATROL", {}) or {}


def ledgers() -> list:
    """账本名列表（运行时取，便于测试里替换 config.PAPER）。"""
    return list((config.PAPER.get("ledgers") or {}).keys())


def json_path() -> str:
    return os.path.join(config.OUT_DIR, "patrol_latest.json")


def last_trading_day(day: str | None = None) -> str:
    """≤ day 的最近一个交易日（day 缺省=今天）。

    优先用 `datasource.fetch_trade_calendar()`（含已排定的未来交易日）；
    取不到日历时退化为「跳过周末」，至少不会把周六当成交易日。
    """
    base = (day or dt.date.today().strftime("%Y-%m-%d"))[:10]
    try:
        import datasource as ds
        cal = set(str(x)[:10] for x in ds.fetch_trade_calendar()["trade_date"])
        cur = dt.date.fromisoformat(base)
        for _ in range(31):
            if cur.strftime("%Y-%m-%d") in cal:
                return cur.strftime("%Y-%m-%d")
            cur -= dt.timedelta(days=1)
    except Exception:                              # noqa: BLE001 - 日历取不到不能拖垮巡检
        pass
    cur = dt.date.fromisoformat(base)
    for _ in range(31):
        if cur.weekday() < 5:
            return cur.strftime("%Y-%m-%d")
        cur -= dt.timedelta(days=1)
    return base


def trade_calendar() -> list | None:
    """交易日历（升序）；取不到返回 None。

    巡检里的现金计息必须用与 weekly.py 同一份日历（datasource 缓存），
    取不到就跳过计息（等价无操作）——不联网现拉、不拿历史/自然日凑。
    """
    try:
        df = datasource.fetch_trade_calendar()
        days = sorted(str(x)[:10] for x in df["trade_date"].tolist())
        return days or None
    except Exception:                              # noqa: BLE001 - 计息失败不能拖垮巡检
        return None


def _today(day: str | None = None) -> str:
    """`--date auto`（或省略）= 最近一个交易日；显式传日期则原样使用。"""
    if day and day != "auto":
        return day[:10]
    return last_trading_day(day if (day and day != "auto") else None)


def _days_since(day: str, today: str) -> int | None:
    try:
        a = dt.date.fromisoformat(str(day)[:10])
        b = dt.date.fromisoformat(today[:10])
        return (b - a).days
    except (ValueError, TypeError):
        return None


def _trading_days_since(day: str, today: str) -> tuple[int | None, bool]:
    """(净值日后过了几个交易日, 是否退化为自然日)。

    场外基金按交易日更新净值——长假里 5 个自然日才是 1~2 个交易日，
    用自然日会在春节/国庆必报「陈旧」。日历取不到时退回自然日并标注。
    """
    nat = _days_since(day, today)
    if nat is None:
        return None, False
    try:
        import datasource as ds
        cal = set(str(x)[:10] for x in ds.fetch_trade_calendar()["trade_date"])
    except Exception:                              # noqa: BLE001 - 日历缺失退回自然日
        return nat, True
    cur = dt.date.fromisoformat(str(day)[:10])
    end = dt.date.fromisoformat(today[:10])
    n = 0
    while cur < end:
        cur += dt.timedelta(days=1)
        if cur.strftime("%Y-%m-%d") in cal:
            n += 1
    return n, False


def _state_path(ledger: str) -> str:
    try:
        return config.PAPER["ledgers"][ledger]["state_file"]
    except (KeyError, TypeError):
        base = getattr(config, "LEDGER_DIR", None) or config.OUT_DIR
        return os.path.join(base, f"state_{ledger}.json")


_ETF_CODE_RE = re.compile(r"^(?:sh|sz)?(?:51|52|56|58|15|16)\d{4}$")


def _etf_codes(state: dict) -> set:
    """持仓里哪些是 ETF：优先认账本的 kind/layer 字段，再用代码号段兜底。

    为什么必须区分：ETF 走 `fetch_daily` 会被 `sina_symbol()` 按股票规则拼成
    bj510050 之类的错代码，取不到就静默沿用上次收盘（净值/止损判断都滞后）。
    ETF 的日线要走 `fetch_etf_hist`（新浪基金接口）。
    """
    out = set()
    for c, h in (state.get("holdings") or {}).items():
        code = str(c)
        h = h or {}
        if str(h.get("kind") or "").lower() == "etf" or str(h.get("layer") or "") == "ETF":
            out.add(code)
        elif _ETF_CODE_RE.match(code.strip().lower()):
            out.add(code)
    return out


def _close_prices(codes: list[str], etf_codes: set | None = None) -> dict:
    """取持仓当日收盘价（失败就沿用账本里的 last_close，不编价格）。"""
    etf = set(etf_codes or ())
    out = {}
    for c in codes:
        try:
            if c in etf:
                df = datasource.fetch_etf_hist(c)
            else:
                df = datasource.fetch_daily(c, years=1)
            if df is not None and len(df):
                out[c] = float(df["close"].iloc[-1])
        except Exception:                           # noqa: BLE001 - 取不到就退回 last_close
            pass
    return out


def _atr14_map() -> dict:
    """从 `out/stage1_candidates.csv` 读 atr14（与阶段三算移动止损同一列），缺失返回空表。"""
    p = os.path.join(config.OUT_DIR, "stage1_candidates.csv")
    out = {}
    if not os.path.isfile(p):
        return out
    try:
        with io.open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                c = industry.norm_code(r.get("code"))
                v = r.get("atr14")
                if not c or v in (None, ""):
                    continue
                try:
                    out[c] = float(v)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return out
    return out


def _is_broad_etf(p: dict) -> bool:
    """宽基/策略 ETF：不占行业额度，也不参与「未分类」占比告警（本身没有行业字段）。

    2026-11-23 A 类修复：行业集中度原来把所有无行业字段的持仓都算「未分类」，
    上证50ETF（宽基）因此被误报成「行业映射缺失 22.3%」。宽基/策略 ETF 按
    config.PAPER 的行业注释本来就不占行业额度，应从集中度口径里排除；
    行业类 ETF 仍照常计入（缺映射时该报的「未分类」照报）。
    """
    if str(p.get("layer") or "") != "ETF":
        return False
    try:
        return etf.classify(str(p.get("name") or "")) in ("宽基", "策略")
    except Exception:                               # noqa: BLE001 - 分类失败不影响巡检
        return False


def _trail_stop_alerts(pos: list[dict], atr_map: dict) -> list[dict]:
    """移动止损提示：浮盈>10% **且**按 收盘−2.5×ATR14 算出的新止损确实高于现止损才报。

    2026-11-21 修（S4 巡检 A 类）：原实现只看浮盈>10% 就报，不算新止损——
    会给出「可上移」却不知道移到哪，且决策日 paper.py 本来就会自动上移并改 GTC，
    巡检重复提示只会诱发手工重复改单。现在只报真能上移的，并标明由决策日自动执行。
    """
    mult = float(PATROL.get("trail_atr_mult", 2.5))
    alerts = []
    for p in pos:
        sp = p.get("stop_price")
        cost = p.get("cost")
        last = p.get("last")
        if not (sp and cost and last) or float(cost) <= 0:
            continue
        gain = float(last) / float(cost) - 1.0
        if gain <= 0.10:
            continue
        atr = atr_map.get(industry.norm_code(p.get("code")))
        if not atr:
            alerts.append({
                "kind": "止损可上移", "level": "低", "code": p["code"], "name": p["name"],
                "detail": f"浮盈 {gain * 100:.1f}%，现止损 {float(sp):.3f}（缺 ATR14，算不出新止损）",
                "action": "先补 out/stage1_candidates.csv 的 atr14，再由决策日 paper 自动上移"})
            continue
        new_stop = round(float(last) - mult * float(atr), 3)
        if new_stop <= float(sp) + 1e-9:
            continue
        alerts.append({
            "kind": "止损可上移", "level": "中", "code": p["code"], "name": p["name"],
            "detail": (f"浮盈 {gain * 100:.1f}%，现止损 {float(sp):.3f} → 新止损 {new_stop:.3f}"
                       f"（收盘 − {mult}×ATR14）"),
            "action": "决策日 paper 自动上移并改 GTC（巡检只提示，不手动改单）"})
    return alerts


def _bench_close() -> float | None:
    try:
        df = datasource.fetch_index_daily("000300")
        return float(df["close"].iloc[-1]) if df is not None and len(df) else None
    except Exception:                               # noqa: BLE001
        return None


def _apply_dividends(st: dict, today: str) -> None:
    """持仓现金分红入账（幂等）。分红取不到就跳过——不该拖垮天天跑的盯市。"""
    codes = [str(h.get("code") or "").zfill(6)
             for h in (st.get("holdings") or {}).values()
             if str(h.get("kind")) == "stock" and h.get("code")]
    if not codes:
        return
    try:
        import dividends as dv
        paper.apply_dividends(st, today, dv.map_for(codes, today))
    except Exception as exc:                     # noqa: BLE001 - 分红失败不影响盯市
        paper.event(st, "warn", f"分红入账跳过：{exc}", today)



def mark_daily(ledger: str, today: str) -> dict | None:
    """日度盯市：先给现金层计息，再把当日净值记进账本 nav 曲线。

    为什么不等决策日：决策日一周才一次，净值曲线会变成「一周一个点」；
    巡检每个工作日都跑，顺手记一个点就是日频曲线，成本 0 token。
    为什么要在 settle 之前计息：计息窗口取「上次结算日 → today」；若只 settle
    不 accrue，`_cash_last_date` 会被 nav 拉到 today，窗口恒为 0 天、利息永远 0。
    同一天重复跑是幂等的（accrue 看 last_accrued，settle 按 date 覆盖）。
    """
    st = _load_state(ledger)
    if st is None:
        return None
    cal = trade_calendar()
    if cal:
        try:
            paper.accrue_cash(st, today, calendar=cal)
        except Exception as exc:                   # noqa: BLE001 - 计息失败不能中断巡检
            paper.event(st, "warn", f"现金计息失败（按无计息继续）：{exc}", today)
    _apply_dividends(st, today)
    codes = [str(h.get("code")) for h in (st.get("holdings") or {}).values() if h.get("code")]
    closes = _close_prices(codes, _etf_codes(st)) if codes else {}
    for c in codes:                                 # 取不到当日价的，沿用账本 last_close
        if closes.get(c) is None:
            h = next((x for x in st["holdings"].values() if str(x.get("code")) == c), None)
            if h and h.get("last_close"):
                closes[c] = float(h["last_close"])
    # 场外持仓：决策日才刷新的话，净值会陈旧一周（巡检天天报「场外净值陈旧」）；
    # 这里每个工作日顺手刷一次累计净值，取不到就沿用旧值，不影响盯市。
    if st.get(otc_book.HOLD_KEY):
        try:
            otc_book.refresh_navs(st, today)    # 与 paper_exec 同一入口：两条结算路径不能各刷各的
        except Exception:                   # noqa: BLE001 - 场外净值坏了不拖垮盯市
            pass
    prior = [n for n in (st.get("nav") or []) if n.get("date") < today]
    entry = paper.settle(st, today, closes, _bench_close())
    paper.save(st, _state_path(ledger))   # 必须写回本账本文件，不能落默认账本
    prev_nav = prior[-1]["nav"] if prior else float(st["meta"].get("capital") or 0.0)
    cap0 = float(st["meta"].get("capital") or 0.0)
    return {"date": today, "nav": entry["nav"],
            "day_chg": round(entry["nav"] / prev_nav - 1, 6) if prev_nav else None,
            "total_ret": round(entry["nav"] / cap0 - 1, 6) if cap0 else None,
            "drawdown": entry.get("drawdown"),
            "path": _state_path(ledger)}


def _load_state(ledger: str) -> dict | None:
    p = _state_path(ledger)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ 账本巡检
def check_ledger(ledger: str, today: str) -> dict:
    """单个账本的风险巡检。返回 {ledger, cash, holdings, market_value, alerts[]}。"""
    out = {"ledger": ledger, "ok": True, "cash": 0.0, "holdings": [],
           "market_value": 0.0, "total": 0.0, "alerts": [], "notes": []}
    st = _load_state(ledger)
    if st is None:
        out["ok"] = False
        path = _state_path(ledger)
        if os.path.isfile(path):
            # 文件在、解析失败 = 真故障，必须报警（2026-10-30 审计 #13：原来只写一句 note）
            out["alerts"].append({"kind": "账本读不出", "level": "高", "code": "", "name": ledger,
                                  "detail": f"账本文件存在但解析失败：{path}",
                                  "action": "检查文件是否被写坏，必要时从 _backup/ 恢复"})
        else:
            out["notes"].append(f"账本文件不存在（尚未开始记账）：{path}")
        return out

    cash = float(st.get("cash") or 0.0)
    holds = list((st.get("holdings") or {}).values())
    mv = 0.0
    pos = []
    for h in holds:
        try:
            shares = int(h.get("shares") or 0)
            last = float(h.get("last_close") or 0.0)
            cost = float(h.get("cost_basis") or 0.0)
        except (TypeError, ValueError):
            continue
        amt = round(shares * last, 2)
        mv += amt
        pos.append({"code": h.get("code"), "name": h.get("name"),
                    "industry": h.get("industry") or h.get("_industry") or "",
                    "amount": amt, "shares": shares, "last": last, "cost": cost,
                    "stop_price": h.get("stop_price"), "layer": h.get("layer")})
    # 行业字段回填（2026-11-16 修）：账本 holdings 不存行业，新迁账本/旧账本常为空，
    # 行业集中度会把全部持仓算成「未分类」而误报超限；用 out/industry_map.csv 补。
    try:
        _mp = industry.load_map()
        if _mp:
            for p in pos:
                if not p.get("industry"):
                    _row = _mp.get(industry.norm_code(p.get("code"))) or {}
                    if _row.get("industry"):
                        p["industry"] = _row["industry"]
    except Exception as e:                          # noqa: BLE001 - 映射缺失不能影响巡检主流程
        out["notes"].append(f"行业映射回填失败：{type(e).__name__}: {e}")
    # 场外影子账（R2 腿）：市值计入总资产，否则账本净值会少算这块
    otc_rows, otc_mv, otc_alerts = [], 0.0, []
    try:
        otc_book.ensure(st)
        otc_rows = otc_book.holdings_rows(st)
        otc_mv = float(otc_book.market_value(st) or 0.0)
        for r in otc_rows:
            age, nat_fallback = _trading_days_since(str(r.get("nav_date") or "")[:10], today)
            stale_lim = int(PATROL.get("otc_nav_stale_days") or 5)
            if age is not None and age > stale_lim:
                otc_alerts.append({
                    "kind": "场外净值陈旧", "level": "中", "code": r["code"], "name": r["name"],
                    "detail": (f"最新净值日 {r['nav_date']}，已 {age} 个交易日没更新"
                               + ("（日历缺失，按自然日计）" if nat_fallback else "")),
                    "action": "净值缺失时影账不编价格（挂单保持 pending），先查数据源再决策"})
            # 2026-11-25（A 类）：条件从「持有 <90 天」改为「**实测费率 >0**」。
            # 旧写法对 011984 这种「满 7 天即 0%」的产品会误报「赎回费窗口」，把「随时可赎」
            # 说成「别动」。现在只看实测档；费表取不到时 otc_book 退回 1.5% 假设，条件仍成立。
            _rf = float(r.get("redeem_fee") or 0)
            if _rf > 0 and float(r.get("amount") or 0) > 0:
                otc_alerts.append({
                    "kind": "场外赎回费窗口", "level": "低", "code": r["code"], "name": r["name"],
                    "detail": (f"持有 {r['hold_days']} 天，赎回费 {_rf * 100:.2f}%"
                               f"（{r.get('redeem_fee_basis') or '口径未知'}）"),
                    "action": "不急用就等费率降到 0 再赎；若急着用，这笔费用要算进成本"})
    except Exception as e:                          # noqa: BLE001 - 巡检不能因影账异常而崩
        out["notes"].append(f"场外影账算不出：{type(e).__name__}: {e}")
    equity_mv = round(mv, 2)                        # 权益层市值（不含场外 R2 腿）：行业集中度分母
    mv = round(mv + otc_mv, 2)
    total = round(cash + mv, 2)
    out.update({"cash": round(cash, 2), "market_value": mv,
                "total": total, "holdings": pos,
                "otc": {"market_value": round(otc_mv, 2), "rows": otc_rows,
                        "cost": round(float(otc_book.cost_total(st) or 0.0), 2),
                        "unrealized": round(float(otc_book.unrealized(st) or 0.0), 2)},
                "alerts": out["alerts"] + otc_alerts})

    # 1) 止损：收盘价已跌破止损价 → 必须处置（硬止损，无条件）
    for p in pos:
        sp = p.get("stop_price")
        if sp and p["last"] and float(p["last"]) <= float(sp) + 1e-9:
            out["alerts"].append({
                "kind": "止损触发", "level": "高", "code": p["code"], "name": p["name"],
                "detail": f"收盘 {p['last']:.3f} ≤ 止损价 {float(sp):.3f}",
                "action": "次日开盘卖出（硬止损）"})
        # 2) 移动止损提示：浮盈>10% 后止损只上移；新止损按 收盘−2.5×ATR14 现算，
        #    算不出（缺 ATR14）或新止损不高于现止损就不报（2026-11-21 S4 A 类）
        out["alerts"].extend(_trail_stop_alerts(pos, _atr14_map()))

    # 3) 单票上限（对总资产）
    cap_single = float(config.EXEC.get("max_single_position") or 0.10)
    if total > 0:
        for p in pos:
            w = p["amount"] / total
            if w > cap_single + 1e-9:
                out["alerts"].append({
                    "kind": "单票超限", "level": "高", "code": p["code"], "name": p["name"],
                    "detail": f"占 {w * 100:.1f}% > 上限 {cap_single * 100:.0f}%",
                    "action": "减到上限内（或下一决策日不再加仓）"})

    # 4) 行业集中度（分母 = 权益层市值，与技能口径一致）
    if pos:
        try:
            # 宽基/策略 ETF 不占行业额度（见 config.PAPER 行业注释），本身也没有行业字段：
            # 必须排除在行业集中度口径外，否则会被当成「未分类」误报映射失效
            # （2026-11-23 A 类修复：上证50ETF 占权益 22.3% 触发「行业映射缺失」中危）。
            conc = industry.concentration([p for p in pos if not _is_broad_etf(p)],
                                          cap=float(config.EXEC.get("max_industry") or 0.25),
                                          base=equity_mv or None)
            for r in conc.get("violated") or []:
                out["alerts"].append({
                    "kind": "行业超限", "level": "中", "code": "",
                    "name": "、".join(r.get("names") or []),
                    "detail": f"行业「{r['industry']}」{r['weight'] * 100:.1f}%"
                              f" > 上限 {conc['cap'] * 100:.0f}%",
                    "action": "下一决策日优先减该行业"})
            unk_lim = float(PATROL.get("unclassified_warn") or 0.20)
            unk_w = float(conc.get("unclassified_weight") or 0.0)
            if unk_w > unk_lim + 1e-9:
                out["alerts"].append({
                    "kind": "行业映射缺失", "level": "中", "code": "", "name": "未分类持仓",
                    "detail": f"未分类占权益 {unk_w * 100:.1f}% > {unk_lim * 100:.0f}%"
                              "（不占 25% 上限，但映射失效时超限检查形同虚设）",
                    "action": "跑 industry.py --refresh 补映射，再判断行业集中度"})
        except Exception as e:                      # noqa: BLE001 - 巡检不能因行业数据缺失而崩
            out["notes"].append(f"行业集中度算不出：{type(e).__name__}: {e}")

    # 5) 挂单过期
    for o in (st.get("orders") or []):
        if str(o.get("status") or "") != "pending":
            continue
        exp = str(o.get("expire_on") or "")[:10]
        d = _days_since(exp, today) if exp else None
        if d is not None and d > 0:
            out["alerts"].append({
                "kind": "挂单过期", "level": "中", "code": o.get("code"), "name": o.get("name"),
                "detail": f"{o.get('action')} {o.get('shares')} 股，expire_on {exp}（逾期 {d} 天）",
                "action": "撤单或重挂（paper.py 不会自动撮合过期单）"})

    # 6) 时间止损：持有超 12 个月且论点未兑现
    months = int(config.EXEC.get("time_stop_months") or 12)
    for h in holds:
        d = _days_since(str(h.get("opened_at") or "")[:10], today)
        if d is not None and d > months * 30:
            out["alerts"].append({
                "kind": "时间止损", "level": "中", "code": h.get("code"), "name": h.get("name"),
                "detail": f"已持有 {d} 天 > {months} 个月",
                "action": "检视论点是否兑现，未兑现则退出"})

    # 7) 回撤熔断（净值口径，paper.current_drawdown 同算法）
    nav = list(st.get("nav") or [])
    peak = 0.0
    for row in nav:
        try:
            # 2026-11-17 修：nav 行 2026-10 后改用 "nav" 键（老行才是 "value"），
            # 只读 "value" 会让 peak 恒为 0、回撤熔断永不触发（补测试见 _selftest_patrol 5）
            v = row.get("nav")
            if v is None:
                v = row.get("value")
            peak = max(peak, float(v or 0.0))
        except (TypeError, ValueError):
            continue
    if peak > 0 and total > 0:
        dd = total / peak - 1.0
        halt = float(config.EXEC.get("portfolio_dd_halt") or -0.15)
        if dd <= halt + 1e-9:
            out["alerts"].append({
                "kind": "回撤熔断", "level": "高", "code": "", "name": "",
                "detail": f"净值回撤 {dd * 100:.1f}% ≤ 熔断线 {halt * 100:.0f}%",
                "action": "全部减到半仓，停止开新仓"})

    out["ok"] = True
    if not holds:
        out["notes"].append("空仓（只在建仓期正常）")
    return out


# ------------------------------------------------------------------ 数据新鲜度
# 阈值按「多久本该重跑一次」定，不是拍脑袋（2026-10-31 排班改定后重算）：
#   决策日产物（候选/计划/快照/事件面）= 每周两次（周三/周六） → 最长正常间隔 4 天，仍取 8 天缓冲（覆盖长假）；
#   行业映射 30 天、宏观 32 天（月度口径）。
WATCH = [
    ("stage1_candidates.csv", "选股候选", 8),
    ("stage3_plan.json", "执行计划", 8),
    ("news_events.csv", "事件扫描", 8),
    ("industry_map.csv", "行业映射", 30),
    ("sentiment.json", "情绪层", 8),
    ("macro.json", "宏观层", 32),
    ("data_snapshot.csv", "L1 数据快照", 8),
]


def check_sentiment(today: str) -> dict:
    """情绪层体检：同花顺 fuyao key 在不在、取了没取到、产物坏没坏。

    情绪层**不进任何买卖规则**——只在决策日研究段当背景、给持仓/候选标异动旗标
    （见 sentiment.py 与 team.py 的消费方）。所以这里一律低/中危，绝不阻断巡检，
    也不该抢在行情/账本告警前面。
    """
    out = {"ok": True, "alerts": []}
    try:
        import fuyao
    except Exception as exc:                        # noqa: BLE001 - 模块缺失也只报不抛
        out["ok"] = False
        out["alerts"].append({
            "kind": "情绪源不可用", "level": "中", "code": "", "name": "",
            "detail": f"import fuyao 失败：{type(exc).__name__}: {exc}",
            "action": "确认 stock/sources 在路径里；情绪层只影响研究背景，不影响买卖"})
        return out
    if not fuyao.available():
        out["ok"] = False
        out["alerts"].append({
            "kind": "情绪源未配置", "level": "中", "code": "", "name": "",
            "detail": "同花顺 fuyao key 不可用：情绪层整层跳过（决策日没有市场温度/龙虎榜背景）",
            "action": "配置 FUYAO_KEY（或 data/fuyao.key）；若不再需要情绪层，把 sentiment.py 从流程里摘掉"})
        return out
    path = os.path.join(config.OUT_DIR,
                        (getattr(config, "SENTIMENT", {}) or {}).get("out_json", "sentiment.json"))
    if not os.path.exists(path):
        try:
            fuyao.hot_stock_list(ttl_hours=0)
        except Exception as exc:                    # noqa: BLE001 - key 在但接口挂
            out["ok"] = False
            out["alerts"].append({
                "kind": "情绪源接口异常", "level": "中", "code": "", "name": "",
                "detail": f"fuyao key 在但调用失败：{type(exc).__name__}: {exc}",
                "action": "等接口恢复；期间情绪层为空，决策照常按确定性规则走"})
            return out
        out["ok"] = False
        out["alerts"].append({
            "kind": "情绪层未生成", "level": "低", "code": "", "name": "",
            "detail": "out/sentiment.json 不存在（接口正常，只是还没跑）",
            "action": "跑 python scripts/sentiment.py 生成"})
        return out
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as exc:                        # noqa: BLE001 - 产物坏了
        out["ok"] = False
        out["alerts"].append({
            "kind": "情绪层产物损坏", "level": "低", "code": "", "name": "",
            "detail": f"out/sentiment.json 解析失败：{type(exc).__name__}: {exc}",
            "action": "删掉重跑 python scripts/sentiment.py"})
        return out
    temp = doc.get("temp") or {}
    total = sum(int(temp.get(k) or 0) for k in ("limit_up", "limit_down", "break"))
    if total == 0:
        out["ok"] = False
        out["alerts"].append({
            "kind": "情绪层空样本", "level": "低", "code": "", "name": "",
            "detail": "涨跌停/炸板样本全 0：接口没返回或当天数据缺失（市场温度不可用）",
            "action": "重跑 sentiment.py 确认；连续为空再查 fuyao 接口"})
    return out


def check_holdings_risk(ledger: str, today: str) -> list:
    """持仓当前简称是否已被交易所打上风险警示（ST/*ST/退市整理）——高优先级。

    为什么按简称查、不按公告查：主板被实施「其他风险警示」时简称直接变成 ST 开头，
    但公告标题里不一定出现「退市风险」；公告流是「近 N 天新增事件」，而这个是
    「现在是什么状态」。两者互补：公告抓新增，简称抓存量。
    """
    st = _load_state(ledger)
    if not st:
        return []
    holds = st.get("holdings") or {}
    codes = [c for c, h in holds.items()
             if isinstance(h, dict) and str(h.get("kind")) == "stock"]
    if not codes:
        return []
    try:
        names = datasource.fetch_code_names()
        mp = {str(c).strip().zfill(6): str(n) for c, n in
              zip(names["code"].tolist(), names["name"].tolist())}
    except Exception as exc:                        # noqa: BLE001 - 名单取不到只报不拦
        return [{"kind": "持仓警示名单未取到", "level": "低", "code": "", "name": "",
                 "detail": f"取 A 股简称名单失败：{type(exc).__name__}: {exc}",
                 "action": "确认 19:00 取数任务是否正常；名单缺失期间只能看公告面"}]
    out = []
    for code in codes:
        c6 = str(code).strip()[-6:]
        cur = mp.get(c6, "")
        if not paper.is_risk_warn_name(cur):
            continue
        old = str((holds.get(code) or {}).get("name") or "")
        out.append({
            "kind": "持仓风险警示", "level": "高", "code": c6, "name": cur,
            "detail": f"{c6} 当前简称「{cur}」（账本记为「{old}」）：已被实施风险警示/退市整理",
            "action": "决策日按规则清仓（paper.risk_exit_orders：下一交易日开盘全部卖出、"
                      "当周不回补）；连续跌停卖不出时账本按最后收盘估值，不虚构成交"})
    return out


def check_announcements(ledger: str, today: str, days: int = 7) -> list:
    """持仓标的近 N 天的硬风险公告（巨潮全市场增量流，fail-soft）。

    为什么不逐只查：逐只只覆盖候选池（30–300 只）且每次多打 N 次站点；全市场增量流
    一次拿全 A 股当天全部公告再按代码过滤——覆盖面更大、请求更少，还能抓到「池外的
    持仓突发公告」。接口/缓存不可用只报一条低优先级告警，绝不拖垮巡检。
    """
    try:
        import cninfo
        state = _load_state(ledger)
        codes = [str(c) for c in ((state or {}).get("holdings") or {}).keys()]
        # 命中硬风险时再读正文：区分「拟减持/已实施」「问询函/处罚决定书」；
        # 只解析前几篇（每篇一次 PDF 请求），取不到自动退回标题口径。
        res = cninfo.market_scan(codes, days=days, asof=today, body=True,
                                 body_max=int(PATROL.get("announcement_body_max", 5)))
    except Exception as e:                      # noqa: BLE001 - 公告面失败不拖垮巡检
        return [{"kind": "公告面未取到", "level": "低", "code": "", "name": ledger,
                 "detail": f"全市场公告流不可用：{type(e).__name__}: {e}",
                 "action": "检查 stock 全市场公告接口/缓存；不影响其余巡检"}]
    alerts = []
    for it in res:
        for tag in it.get("hard") or []:
            alerts.append({"kind": tag, "level": "高", "code": it["code"],
                           "name": it.get("name") or "",
                           "detail": f"{it['date']} {it['title']}",
                           "action": "查原文核对是否触发证伪条件；硬风险进 §8 复核，不自动卖"})
    return alerts


_FORECAST_BAD = {"预亏": "高", "首亏": "高", "续亏": "高", "增亏": "高",
                 "预减": "中", "略减": "中"}


def check_forecasts(ledger: str, today: str) -> list:
    """持仓个股最近一期业绩预告里「预亏/预减」这类结论性信号——fail-soft。

    为什么用业绩预告而不是业绩快报：业绩快报没有结构化接口（东财未开放），只能
    靠公告标题 + 正文；业绩预告有结构化字段（净利润上下限、同比区间、是否最新、
    公告日），能直接判断「预亏/续亏/预减」。
    """
    st = _load_state(ledger)
    if not st:
        return []
    holds = st.get("holdings") or {}
    pairs = [(str(c).strip()[-6:], str((h or {}).get("name") or ""))
             for c, h in holds.items()
             if isinstance(h, dict) and str(h.get("kind")) == "stock"]
    if not pairs:
        return []
    lookback = int(PATROL.get("forecast_lookback_days", 180))
    try:
        cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=lookback)).isoformat()
    except ValueError:
        return []
    out, errs = [], []
    for c6, nm in pairs:
        try:
            df = datasource.fetch_forecast(c6)
        except Exception as exc:                    # noqa: BLE001 - 预告取不到只报不拦
            errs.append(f"{c6}:{type(exc).__name__}")
            continue
        if df is None or getattr(df, "empty", True):
            continue
        rows = df.copy()
        if "报告期" not in rows.columns or "公告日" not in rows.columns:
            errs.append(f"{c6}:缺列")
            continue
        try:
            rows["_r"] = rows["报告期"].astype(str)
            rows["_n"] = rows["公告日"].astype(str)
        except Exception:                        # noqa: BLE001 - 列类型异常也只报不拦
            errs.append(f"{c6}:列异常")
            continue
        rows = rows[(rows["_n"] >= cutoff) & (rows["_r"] != "")]
        if rows.empty:
            continue
        latest = rows["_r"].max()
        latest_rows = rows[rows["_r"] == latest].drop_duplicates(subset=["预告类型"])
        for _, r in latest_rows.iterrows():
            typ = str(r.get("预告类型") or "")
            level = _FORECAST_BAD.get(typ)
            if not level:
                continue
            out.append({"kind": f"业绩预告{typ}", "level": level, "code": c6,
                        "name": nm,
                        "detail": (f"{c6} {latest} 业绩预告：{typ}（净利 "
                                   f"{r.get('净利润下限')}~{r.get('净利润上限')}，同比 "
                                   f"{r.get('同比下限')}%~{r.get('同比上限')}%，公告 "
                                   f"{r.get('公告日')}）"),
                        "action": "查预告原文核对原因；预亏/续亏进 §8 复核，按证伪条件处理，不自动卖"})
    if errs and not out:
        out.append({"kind": "业绩预告未取到", "level": "低", "code": "", "name": ledger,
                    "detail": "取业绩预告失败：" + ",".join(errs[:3]),
                    "action": "检查 stock 业绩预告接口/缓存；不影响其余巡检"})
    return out


def check_premium(ledger: str, today: str) -> list:
    """持仓 ETF 的折溢价体检（收盘价 ÷ 同日单位净值 − 1）。

    宽基 ETF 正常在 ±0.3% 内；超过 ``PATROL["etf_premium_alert_pct"]`` 说明场内价格
    相对净值明显跑偏（多为流动性/情绪异常），买进就是「买贵」。只告警不交易。
    净值还没披露（日期落后）就只报一条低优先级，不拿陈旧净值硬算。
    """
    try:
        state = _load_state(ledger)
    except Exception:                            # noqa: BLE001
        return []
    codes = sorted(_etf_codes(state or {}))
    if not codes:
        return []
    limit = float(PATROL.get("etf_premium_alert_pct", 1.5))
    alerts = []
    for code in codes:
        try:
            p = datasource.fetch_etf_premium(code)
        except Exception as e:                   # noqa: BLE001 - 折溢价失败不拖垮巡检
            alerts.append({"kind": "折溢价未取到", "level": "低", "code": code, "name": "",
                           "detail": f"{type(e).__name__}: {e}",
                           "action": "忽略；连续多日出现再查 ETF 净值通路"})
            continue
        if p["nav_date"] != p["date"]:
            alerts.append({"kind": "净值日期落后", "level": "低", "code": code, "name": "",
                           "detail": f"收盘 {p['date']}，单位净值 {p['nav_date']}（未更新）",
                           "action": "净值披露滞后属正常，明日再看"})
        prem = float(p["premium_pct"])
        if abs(prem) >= limit:
            side = "溢价（买贵）" if prem > 0 else "折价（买便宜）"
            alerts.append({"kind": f"ETF {side}", "level": "中", "code": code, "name": "",
                           "detail": f"{p['date']} 收盘 {p['price']} / 单位净值 {p['nav']} = {prem:+.2f}%",
                           "action": "溢价：等回落或改用成分股承接；折价：正常买入不吃亏"})
    return alerts


# ------------------------------------------------------------------ 取数通路可用率
def _latest_manifest() -> tuple:
    """stock prefetch 最新一份 manifest（按快照目录日期排序）。"""
    root = getattr(config, "PREFETCH_SNAP_DIR", "")
    try:
        dirs = sorted(d for d in os.listdir(root)
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
                      and os.path.isfile(os.path.join(root, d, "manifest.json")))
    except OSError as e:
        raise RuntimeError(f"读不到 prefetch 快照目录 {root}：{e}") from e
    if not dirs:
        raise RuntimeError(f"{root} 下没有带 manifest.json 的快照")
    day = dirs[-1]
    with io.open(os.path.join(root, day, "manifest.json"), encoding="utf-8") as f:
        return day, json.load(f)


def check_scale_source(today: str) -> dict:
    """场外规模源（雪球/蛋卷）可用率体检：读 stock prefetch 的 L7_scale_probe。

    为什么盯这个：otc.py 的 L7 规模过滤对「取不到」是放行的（查不到不淘汰），接口一
    掉线就静默退化成「不按规模过滤」，账本和计划里看不出任何异常。prefetch 每天探
    N 只、把成功比例写进 manifest，巡检读它就能把「静默降级」变成一条可见告警。
    阈值 `PATROL.scale_probe_min_ratio`；manifest 缺失/过期/解析失败只报低危。
    """
    min_ratio = float(PATROL.get("scale_probe_min_ratio", 0.5))
    stale_days = int(PATROL.get("scale_probe_stale_days", 2))
    out = {"ok": False, "as_of": None, "ratio": None, "n": None,
           "available": None, "alerts": []}
    try:
        day, man = _latest_manifest()
    except Exception as e:                          # noqa: BLE001 - 体检失败不拖垮巡检
        out["alerts"].append({"kind": "取数通路未体检", "level": "低", "code": "",
                              "name": "场外规模源",
                              "detail": f"{type(e).__name__}: {e}",
                              "action": "确认交易日 19:00 stock prefetch 是否在跑"})
        return out
    out["as_of"] = day
    d = _days_since(day, today)
    if d is not None and d > stale_days:
        out["alerts"].append({"kind": "取数通路陈旧", "level": "低", "code": "",
                              "name": "场外规模源",
                              "detail": f"最新 prefetch manifest 是 {day}（距今 {d} 天 > {stale_days} 天）",
                              "action": "确认 19:00 取数任务是否漏跑"})
    probe = ((man.get("funnel") or {}).get("L7_scale_probe") or {})
    n, avail, ratio = probe.get("n"), probe.get("available"), probe.get("ratio")
    out["n"], out["available"], out["ratio"] = n, avail, ratio
    if ratio is None or not n:
        out["alerts"].append({"kind": "规模可用率缺项", "level": "低", "code": "",
                              "name": "场外规模源",
                              "detail": f"{day} 的 manifest 里没有 L7_scale_probe（旧版 prefetch？）",
                              "action": "升级/重跑 stock prefetch 后再看"})
        return out
    out["ok"] = True
    if float(ratio) < min_ratio:
        out["alerts"].append({"kind": "场外规模源掉线", "level": "中", "code": "",
                              "name": "场外规模源",
                              "detail": f"{day} 探 {n} 只只取到 {avail} 只（{float(ratio):.1%}，"
                                        f"低于 {min_ratio:.0%}）：OTC 池的规模过滤静默失效",
                              "action": "确认雪球/蛋卷接口是否限流或需要登录态；连续多日失败"
                                        "再考虑换源，或把规模过滤降级为提示"})
    return out


def check_sources(today: str) -> dict:
    """取数通路体检（不联网）：prefetch manifest 的主链路状态 + 行情/公告缓存。

    为什么要有它：行情/财务/公告坏了大多**不报错**——取不到就回落到上一份缓存，或者
    整晚没跑（照旧用昨天的数据），计划、评分、止损价位都还是「像真的」。这里只读
    prefetch 自己写的 manifest（ok/rc/bar_ok/etf_bar_ok/coverage/seconds）和本地缓存
    文件，不联网、不拖慢巡检；缺东西只降级告警，不中断巡检。

    判据：manifest 是不是本交易日的、主链路是否成功、缓存覆盖是否达标、是否慢到会撞
    20:00 巡检；以及最近一个交易日的公告流缓存有没有落盘、条数是否骤减。
    """
    out = {"ok": False, "as_of": None, "coverage": {}, "seconds": None, "alerts": []}
    slow = float(PATROL.get("prefetch_slow_seconds", 1200))
    min_rows = int(PATROL.get("announcement_min_rows", 200))
    try:
        day, man = _latest_manifest()
    except Exception as e:                          # noqa: BLE001 - 体检失败不拖垮巡检
        out["alerts"].append({"kind": "取数通路未体检", "level": "低", "code": "",
                              "name": "取数通路", "detail": f"{type(e).__name__}: {e}",
                              "action": "确认交易日 19:00 stock prefetch 是否在跑"})
        return out
    out["as_of"] = day
    cov = man.get("coverage") or {}
    out["coverage"] = cov.get("counts") or {}
    out["seconds"] = man.get("seconds")
    expect = last_trading_day(today)
    if str(day) != str(expect):
        out["alerts"].append({"kind": "取数未跑", "level": "中", "code": "",
                              "name": "取数通路",
                              "detail": f"最新快照 {day}，本交易日 {expect} 还没有 manifest（19:00 任务没跑或没跑完）",
                              "action": "查 DSH-DailyPatrol 任务与 stock/data/shared/snapshots"})
    if not man.get("ok", True) or int(man.get("rc") or 0) != 0:
        out["alerts"].append({"kind": "取数失败", "level": "中", "code": "",
                              "name": "取数通路",
                              "detail": f"prefetch ok={man.get('ok')} rc={man.get('rc')} seconds={man.get('seconds')}",
                              "action": "看 logs/daily 那次落单日志，修完重跑 stock prefetch"})
    if man.get("bar_ok") is False:
        out["alerts"].append({"kind": "个股日线未就绪", "level": "中", "code": "",
                              "name": "取数通路",
                              "detail": "manifest.bar_ok=False：个股日线没更新到位，止损/评分会按旧价算",
                              "action": "重跑 stock prefetch；连续失败查行情源"})
    if man.get("etf_bar_ok") is False:
        out["alerts"].append({"kind": "ETF 日线未就绪", "level": "中", "code": "",
                              "name": "取数通路",
                              "detail": "manifest.etf_bar_ok=False：ETF 日线没更新到位，折溢价/ATR 会用旧价",
                              "action": "重跑 stock prefetch；连续失败查 ETF 行情源"})
    if cov.get("ok") is False:
        counts = cov.get("counts") or {}
        brief = ", ".join(f"{k}={v}" for k, v in list(counts.items())[:8])
        out["alerts"].append({"kind": "缓存覆盖不足", "level": "中", "code": "",
                              "name": "取数通路",
                              "detail": f"{day} 缓存条数没达到预期（{brief}）",
                              "action": "看 manifest.problems 与 logs/daily，确认是接口失败还是提前退出"})
    try:
        if out["seconds"] is not None and float(out["seconds"]) > slow:
            out["alerts"].append({"kind": "取数变慢", "level": "低", "code": "",
                                  "name": "取数通路",
                                  "detail": f"{day} 取数耗时 {float(out['seconds']):.0f} 秒（>{slow:.0f} 秒，会挤到 20:00 巡检）",
                                  "action": "看是不是回落到逐只全量；必要时把巡检推迟或拆任务"})
    except (TypeError, ValueError):
        pass
    ann = os.path.join(config.CACHE_DIR, "cninfo_market", f"{expect}.csv")
    if not os.path.isfile(ann):
        out["alerts"].append({"kind": "公告流未落盘", "level": "低", "code": "",
                              "name": "公告流",
                              "detail": f"没有 {expect} 的全市场公告缓存（巡检会自己补取）",
                              "action": "若连续多日都缺，查巨潮接口"})
    else:
        try:
            with io.open(ann, encoding="utf-8") as f:
                rows = max(0, sum(1 for _ in f) - 1)
            if rows < min_rows:
                out["alerts"].append({"kind": "公告流异常", "level": "中", "code": "",
                                      "name": "公告流",
                                      "detail": f"{expect} 只有 {rows} 条公告（正常约 1,000+，低于 {min_rows} 视为接口静默失败）",
                                      "action": "删该日缓存重取；连续失败换接口或降级为标题兜底"})
        except OSError:
            pass
    out["ok"] = not out["alerts"]
    return out


def check_data(today: str) -> list:
    """产物是否断更。用文件 mtime（比内嵌日期更可靠：没重写就是没更新）。"""
    alerts = []
    for name, label, maxd in WATCH:
        p = os.path.join(config.OUT_DIR, name)
        if not os.path.isfile(p):
            alerts.append({"kind": "数据缺失", "level": "中", "code": "", "name": label,
                           "detail": f"{name} 不存在", "action": "按需重跑对应步骤"})
            continue
        try:
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d")
        except OSError:
            continue
        d = _days_since(mtime, today)
        if d is not None and d > maxd:
            alerts.append({"kind": "数据断更", "level": "中", "code": "", "name": label,
                           "detail": f"{name} 最后写入 {mtime}（距今 {d} 天 > {maxd} 天）",
                           "action": "确认调度是否漏跑"})
    return alerts


# ------------------------------------------------------------------ 现金层利率
def _repo_refresh_due(today: str) -> bool:
    """逆回购平滑利率要不要今天刷。

    规则：每周五刷一次；周五漏跑（节假日/任务失败）时补刷——实测文件里的 `repo_as_of`
    超过 7 个自然日（≈5 个交易日）就再来一次。货基不受影响，仍然每天刷。
    """
    try:
        d = dt.date.fromisoformat(str(today)[:10])
    except ValueError:
        return True
    if d.weekday() == 4:                       # 周五
        return True
    try:
        import rates as R
        live = R.load() or {}
        ra = str(live.get("repo_as_of") or "")[:10]
        if not ra:
            return True
        return (d - dt.date.fromisoformat(ra)).days >= 7
    except Exception:                          # noqa: BLE001
        return True


def check_rates(today: str, refresher=None) -> dict:
    """现金层利率实测：货基每日刷、逆回购每周五刷；失败就报警并回落兜底常数（不中断巡检）。

    `refresher` 只给自测用（签名 `refresher(write=True)`，保持通吃逆回购+货基）。
    """
    alerts, notes = [], []
    try:
        import rates as R
        if refresher is not None:
            got = refresher(write=True)
        else:
            got = R.refresh(write=True, include_repo=_repo_refresh_due(today))
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "live": False, "repo": None, "money_fund": None,
                "as_of": None, "drift_bp": None, "notes": [f"实测失败：{type(e).__name__}"],
                "alerts": [{"kind": "利率实测失败", "level": "中", "code": "",
                            "name": "现金层利率",
                            "detail": f"{type(e).__name__}: {e}",
                            "action": "检查逆回购/货基接口；本次先按兜底常数跑"}]}
    if not (got.get("repo") and got.get("money_fund")):
        notes.append("实测不完整（缺逆回购或货基）")
        alerts.append({"kind": "利率实测不全", "level": "中", "code": "",
                       "name": "现金层利率",
                       "detail": f"repo={got.get('repo')} money_fund={got.get('money_fund')}",
                       "action": "确认数据源是否可用；本次先按兜底常数跑"})
    c = config.cash_rates()
    cfg = config.CAPITAL["cash_layer"]
    drift = None
    if c.get("live"):
        drift = max(abs(c["repo"] - float(cfg["rate_repo"])) * 1e4,
                    abs(c["money_fund"] - float(cfg["rate_money_fund"])) * 1e4)
        if drift > float(cfg["rate_stale_bp"]):
            alerts.append({"kind": "利率偏离", "level": "低", "code": "",
                           "name": "现金层利率",
                           "detail": f"实测 {c['repo'] * 100:.2f}%/{c['money_fund'] * 100:.2f}% "
                                     f"与兜底常数偏离 {drift:.1f}bp（> {cfg['rate_stale_bp']}bp），"
                                     f"本次已用实测值",
                           "action": "确认后把常数也更新到 config，免得实测文件失效时回落旧值"})
    else:
        alerts.append({"kind": "利率未实测", "level": "中", "code": "",
                       "name": "现金层利率",
                       "detail": f"没有新鲜的实测文件，本次用兜底常数 "
                                 f"{c['repo'] * 100:.2f}%/{c['money_fund'] * 100:.2f}%（as_of {c['as_of']}）",
                       "action": "跑 python rates.py 或检查 DSH 工作日巡检任务（20:00 会话跑 patrol.py）是否执行"})
    return {"ok": True, "live": bool(c.get("live")), "repo": c["repo"],
            "money_fund": c["money_fund"], "as_of": c["as_of"],
            "drift_bp": None if drift is None else round(drift, 1),
            "notes": notes, "alerts": alerts}

# ------------------------------------------------------------------ 预算
def check_budget(ledger: str) -> dict:
    try:
        import billing
        b = billing.budget_state(ledger)
    except Exception as e:                          # noqa: BLE001
        return {"ledger": ledger, "ok": False, "note": f"预算读取失败：{type(e).__name__}: {e}"}
    base = float(b.get("base_cny") or 0.0)
    remain = float(b.get("remaining_cny") or 0.0)
    warn = float(PATROL.get("warn_budget_pct") or 0.2)
    return {"ledger": ledger, "ok": remaining_ok(remain, base, warn), "base": base,
            "spent": float(b.get("spent_cny") or 0.0), "remaining": remain,
            "remaining_pct": round(remain / base, 4) if base else None}


def remaining_ok(remain: float, base: float, warn: float) -> bool:
    return not (base > 0 and remain / base < warn)


def run(today: str | None = None, write: bool = True, full_md: bool = False) -> dict:
    day = _today(today)
    res = {"date": day, "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ledgers": [], "data": [], "rates": {}, "budget": [], "alerts": [], "n_alerts": 0,
           "n_high": 0}
    for lg in ledgers():
        # 先盯市再体检：mark_daily 会把当日收盘价写回账本，止损/超限/报表现价才是今天的价；
        # 反过来的话巡检读的是上一次结算日的旧价（2026-11-09 发现：报表混用旧价与当日 nav）
        mark = None
        if write:
            try:
                mark = mark_daily(lg, day)
            except Exception as e:                  # noqa: BLE001 - 盯市失败不能拖垮巡检
                print(f"[patrol] {lg} 盯市失败（不影响结论）：{type(e).__name__}: {e}",
                      file=sys.stderr)
        try:
            one = check_ledger(lg, day)
        except Exception as e:                      # noqa: BLE001 - 单账本坏了不能拖垮巡检
            one = {"ledger": lg, "ok": False, "alerts": [],
                   "notes": [f"巡检异常：{type(e).__name__}: {e}"]}
        if one.get("ok") and mark is not None:
            one["mark"] = mark
        res["ledgers"].append(one)
        for a in one.get("alerts") or []:
            res["alerts"].append(dict(a, ledger=lg))
        for a in check_holdings_risk(lg, day):
            res["alerts"].append(dict(a, ledger=lg))
        for a in check_announcements(lg, day):
            res["alerts"].append(dict(a, ledger=lg))
        for a in check_forecasts(lg, day):
            res["alerts"].append(dict(a, ledger=lg))
        for a in check_premium(lg, day):
            res["alerts"].append(dict(a, ledger=lg))
    res["data"] = check_data(day)
    res["alerts"].extend(res["data"])
    res["scale"] = check_scale_source(day)
    res["alerts"].extend(res["scale"]["alerts"])
    res["sources"] = check_sources(day)
    res["alerts"].extend(res["sources"]["alerts"])
    res["sentiment"] = check_sentiment(day)
    res["alerts"].extend(res["sentiment"]["alerts"])
    res["rates"] = check_rates(day)
    res["alerts"].extend(res["rates"]["alerts"])
    res["budget"] = [check_budget(lg) for lg in ledgers()]
    for b in res["budget"]:
        if b.get("ok") is False:
            res["alerts"].append({"kind": "预算异常", "level": "高", "code": "",
                                  "name": b["ledger"],
                                  "detail": b.get("note") or f"余额 {b.get('remaining')} 元",
                                  "action": "停机等人工处理"})
    res["n_alerts"] = len(res["alerts"])
    res["n_high"] = sum(1 for a in res["alerts"] if a.get("level") == "高")
    if write:
        # 记账 + 清场（失败不影响巡检结论：清场删错了比缓存变大严重）
        try:
            import housekeeping as HK
            res["housekeeping"] = {
                "log": HK.append(HK.build_row(res, day), day),
                "sweep": HK.sweep(today=day),
            }
        except Exception as e:                      # noqa: BLE001
            print(f"[patrol] 记账/清场失败（不影响结论）：{type(e).__name__}: {e}",
                  file=sys.stderr)
        try:
            os.makedirs(config.OUT_DIR, exist_ok=True)
            with io.open(json_path(), "w", encoding="utf-8") as f:
                f.write(json.dumps(res, ensure_ascii=False, indent=1, sort_keys=False))
            # 日常只写两节（无告警时）；有告警或 --full-md 才写完整四节
            full = bool(full_md) or res["n_alerts"] > 0
            with io.open(os.path.join(config.OUT_DIR, f"patrol_{day}.md"), "w",
                         encoding="utf-8") as f:
                f.write(render_md(res, brief=not full))
        except OSError as e:
            print(f"[patrol] 写盘失败（不影响结论）：{e}", file=sys.stderr)
    return res


def _money(x) -> str:
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def brief_lines(res: dict, stats: dict | None = None) -> list:
    """巡检三行简报（2026-11-17 起 20:00 巡检任务直接引用；--brief 输出）。

    口径与 daily_log 同源（housekeeping._ledger_stats）：nav=总资产、当日盈亏=nav 曲线相邻两点差、
    仓位=场内权益市值/总资产。为什么写进脚本：简报每天出一次，交给会话现拼口径容易漂、工具往返也多；
    脚本只算数与排版，第二行「需动作」仍由 Lead 按事实判断。
    第三行只在「知道但不需动作」时出现（低危告警/利率未实测），否则两行结束。
    """
    day = str(res.get("date") or "")
    lg = (ledgers() or ["50k"])[0]
    one = next((x for x in (res.get("ledgers") or []) if x.get("ledger") == lg), None)
    if one is None:
        one = {"ok": False, "notes": [f"巡检结果里没有账本 {lg}"]}
    if stats is None:
        stats = {}
        try:
            if one.get("ok", True):
                import housekeeping as HK
                stats = HK._ledger_stats(lg) or {}
        except Exception:                          # noqa: BLE001 - 统计失败也要能出简报
            stats = {}
    # ---- 第一行：状态 ----
    if not one.get("ok", True):
        notes = "；".join(str(n) for n in (one.get("notes") or [])) or "读不出账本"
        head = f"巡检降级 · {day} · 账本 {lg} {notes}"
    else:
        head = (f"巡检正常 · {day} · 持仓 {len(one.get('holdings') or [])} 只"
                f" · nav {_money(stats.get('total'))}")
        seg = []
        pnl = stats.get("pnl")
        total = stats.get("total")
        if pnl is not None:
            piece = f"当日 {float(pnl):+,.2f} 元"
            if total:
                prev = float(total) - float(pnl)
                if prev:
                    piece += f"（{float(pnl) / prev * 100:+.2f}%）"
            seg.append(piece)
        if stats.get("cum") is not None:
            seg.append(f"累计 {float(stats['cum']):+.2f}%")
        if stats.get("dd") is not None:
            seg.append(f"回撤 {float(stats['dd']):+.2f}%")
        if stats.get("pos") is not None:
            seg.append(f"仓位 {float(stats['pos']):.0f}%")
        if seg:
            head += " · " + "，".join(seg)
    # ---- 第二行：下一交易日开盘要动作的（高/中） ----
    need = [a for a in (res.get("alerts") or []) if a.get("level") in ("高", "中")]
    if need:
        items = []
        for a in need[:3]:
            nm = str(a.get("name") or a.get("code") or "").strip()
            items.append(f"{a.get('level')} {a.get('kind')}"
                         + (f" {nm}" if nm else "")
                         + (f"（{a.get('action')}）" if a.get("action") else ""))
        line2 = "需动作：" + "；".join(items)
        if len(need) > 3:
            line2 += f"；另有 {len(need) - 3} 条"
    else:
        line2 = "今晚无需动作"
    # ---- 第三行：知道即可（低危/利率兜底） ----
    extra = []
    low = [a for a in (res.get("alerts") or []) if a.get("level") == "低"]
    if low:
        cnt: dict = {}
        for a in low:
            k = str(a.get("kind") or "其他")
            cnt[k] = cnt.get(k, 0) + 1
        extra.append(f"低危 {len(low)} 条（" + "、".join(f"{k}×{v}" for k, v in cnt.items()) + "）")
    rates = res.get("rates") or {}
    if rates and not rates.get("live"):
        extra.append(f"利率用兜底常数（as_of {rates.get('as_of') or '—'}）")
    if extra:
        return [head, line2, "；".join(extra) + "，无需动作"]
    return [head, line2]


def _gap(a, b) -> str:
    """a/b-1 的百分比；b 缺失或为 0 时返回 —。"""
    try:
        return f"{(float(a) / float(b) - 1) * 100:+.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def render_md(res: dict, brief: bool = False) -> str:
    """巡检 md。brief=True 只出「持仓体检 + 要处理」两节（日常）；False 出四节（有告警或决策日汇总）。"""
    L = [f"# 持仓巡检 {res['date']}", "",
         f"> 纯代码生成（0 token）· 生成于 {res['generated_at']} · 基准日 {res['date']}（最近一个交易日）",
         f"> 命中 {res['n_alerts']} 条，其中高优先级 {res['n_high']} 条。",
         "> 只报告不调仓：除止损价被击穿（或熔断）由挂着的条件单撮合外，其余告警留到决策日处理。", ""]
    if brief:
        L.append("> 日常简报：只看「体检」与「要处理」。周一到周五的汇总在 `out/patrol_weekly_*.md`。")
        L.append("")

    # ---- 一、持仓体检（每天只需要看这一节）----
    L.append("## 一、持仓体检")
    L.append("")
    n_pos = 0
    for one in res["ledgers"]:
        if not one.get("ok"):
            L.append(f"- `{one['ledger']}`：账本读不出（{'；'.join(one.get('notes') or [])}）")
            continue
        pos = one.get("holdings") or []
        if pos:
            L.append(f"**{one['ledger']}**（场内 {len(pos)} 只）")
            L.append("")
            L.append("| 代码 | 名称 | 层 | 成本 | 现价 | 浮盈 | 止损 | 距止损 |")
            L.append("|---|---|---|---|---|---|---|---|")
            for p in pos:
                cost, last = p.get("cost") or 0.0, p.get("last") or 0.0
                sp = p.get("stop_price")
                L.append(f"| {p.get('code') or ''} | {(p.get('name') or '')[:9]} | "
                         f"{p.get('layer') or '个股'} | {cost:.3f} | {last:.3f} | "
                         f"{_gap(last, cost)} | {float(sp):.3f} | {_gap(last, sp)} |"
                         if sp else
                         f"| {p.get('code') or ''} | {(p.get('name') or '')[:9]} | "
                         f"{p.get('layer') or '个股'} | {cost:.3f} | {last:.3f} | "
                         f"{_gap(last, cost)} | — | — |")
                n_pos += 1
        otc_rows = ((one.get("otc") or {}).get("rows") or [])
        if otc_rows:
            L.append("")
            L.append(f"**{one['ledger']} · 场外 R2**（{len(otc_rows)} 只，T+1 确认；"
                     f"赎回费按实际持有天数取实测档，费表取不到才按 1.5% 最严档假设）")
            L.append("")
            L.append("| 代码 | 名称 | 投入 | 净值日 | 持有天数 | 赎回费 | 口径 |")
            L.append("|---|---|---|---|---|---|---|")
            for r in otc_rows:
                L.append(f"| {r.get('code') or ''} | {(r.get('name') or '')[:9]} | "
                         f"{float(r.get('amount') or 0):,.0f} | {r.get('nav_date') or '—'} | "
                         f"{int(r.get('hold_days') or 0)} | "
                         f"{float(r.get('redeem_fee') or 0) * 100:.2f}% | "
                         f"{r.get('redeem_fee_basis') or '—'} |")
                n_pos += 1
        if not pos and not otc_rows:
            L.append(f"- `{one['ledger']}`：空仓（现金 {one.get('cash', 0):,.2f}）")
    if not n_pos:
        L.append("（当前无持仓）")

    # ---- 二、要处理（按严重度）----
    L.append("")
    L.append("## 二、要处理（按严重度）")
    order = {"高": 0, "中": 1, "低": 2}
    if not res["alerts"]:
        L.append("")
        L.append("今日无需处理。")
    else:
        L.append("")
        L.append("| 级别 | 类型 | 标的 | 事实 | 建议动作 |")
        L.append("|---|---|---|---|---|")
        for a in sorted(res["alerts"], key=lambda x: order.get(x.get("level"), 9)):
            L.append(f"| {a.get('level')} | {a.get('kind')} | {a.get('code') or ''} "
                     f"{a.get('name') or ''} | {a.get('detail')} | {a.get('action')} |")

    if brief:
        return "\n".join(L) + "\n"

    # ---- 三、账本快照 ----
    L.append("")
    L.append("## 三、账本快照")
    L.append("")
    L.append("| 账本 | 净值 | 今日 | 累计 | 回撤 | 现金 | 持仓市值 | 总资产 | 持仓数 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for one in res["ledgers"]:
        if not one.get("ok"):
            L.append(f"| {one['ledger']} | — | — | — | — | — | — | — | — | "
                     f"{'；'.join(one.get('notes') or [])} |")
            continue
        m = one.get("mark") or {}
        nav_txt = f"{m['nav'] / float(config.PAPER['ledgers'][one['ledger']]['capital']):.4f}" \
            if m.get("nav") else "—"
        L.append(f"| {one['ledger']} | {nav_txt} | "
                 f"{(m.get('day_chg') or 0) * 100:+.2f}% | {(m.get('total_ret') or 0) * 100:+.2f}% | "
                 f"{(m.get('drawdown') or 0) * 100:+.2f}% | "
                 f"{one['cash']:,.2f} | {one['market_value']:,.2f} | "
                 f"{one['total']:,.2f} | {len(one.get('holdings') or [])} | "
                 f"{'；'.join(one.get('notes') or []) or ''} |")

    # ---- 四、维护类（不必看）----
    L.append("")
    L.append("## 四、维护项（不必看，脚本自用）")
    r = res.get("rates") or {}
    L.append("")
    L.append("现金层利率（每日实测）：")
    L.append("")
    if r.get("repo") is not None:
        L.append(f"- 逆回购 {r['repo'] * 100:.2f}%｜货基 {r['money_fund'] * 100:.2f}%"
                 f"｜测点 {r.get('as_of') or '—'}（{'实测' if r.get('live') else '兜底常数'}）")
        if r.get("drift_bp") is not None:
            L.append(f"- 与常数偏离 {r['drift_bp']}bp")
    else:
        L.append("- 实测失败，本次按兜底常数")
    L.append("")
    L.append("| 账本 | 年度预算 | 已花 | 剩余 | 剩余占比 |")
    L.append("|---|---|---|---|---|")
    for b in res["budget"]:
        if b.get("ok") is False:
            L.append(f"| {b['ledger']} | — | — | — | {b.get('note') or '读取失败'} |")
            continue
        pct = f"{b['remaining_pct'] * 100:.0f}%" if b.get("remaining_pct") is not None else "—"
        L.append(f"| {b['ledger']} | {b['base']:,.0f} | {b['spent']:,.2f} | "
                 f"{b['remaining']:,.2f} | {pct} |")
    L.append("")
    return "\n".join(L) + "\n"


def market_closed_note() -> str:
    """今天休市时返回一句说明（含最近交易日），否则返回 ''。"""
    try:
        if datasource.is_trading_day():
            return ""
        last = datasource.last_trading_day()
    except Exception:  # noqa: BLE001
        return ""
    return "今日休市（最近交易日 %s）：无新增数据，不做结算" % (last or "-")


def main() -> int:
    ap = argparse.ArgumentParser(description="每日巡检（只读、0 token）")
    ap.add_argument("--date", default="auto",
                    help="巡检基准日：auto=最近一个交易日（默认）；也可传 YYYY-MM-DD")
    ap.add_argument("--no-write", action="store_true", help="只打印，不写文件")
    ap.add_argument("--json", action="store_true", help="打印 JSON")
    ap.add_argument("--full-md", action="store_true",
                    help="即使无告警也写完整四节（决策日汇总前手动跑一次用）")
    ap.add_argument("--brief", action="store_true",
                    help="只打印三行简报（20:00 巡检任务直接引用；仍写盘）")
    a = ap.parse_args()
    _closed = market_closed_note() if a.date in ("auto", "", None) else ""
    if _closed:
        print(_closed)
        return 0
    res = run(a.date, write=not a.no_write, full_md=a.full_md)
    if a.brief:
        print("\n".join(brief_lines(res)))
    elif a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"[patrol] {res['date']}｜命中 {res['n_alerts']} 条（高 {res['n_high']}）")
        for one in res["ledgers"]:
            if one.get("ok"):
                print(f"  {one['ledger']}: 现金 {one['cash']:,.2f}｜市值 {one['market_value']:,.2f}"
                      f"｜总 {one['total']:,.2f}｜持仓 {len(one['holdings'])}")
            else:
                print(f"  {one['ledger']}: {one.get('notes')}")
        for a_ in res["alerts"][:12]:
            print(f"  [{a_.get('level')}] {a_.get('kind')}｜{a_.get('code') or ''} "
                  f"{a_.get('name') or ''}｜{a_.get('detail')}")
        if not a.no_write:
            print(f"[patrol] 产出 {json_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
