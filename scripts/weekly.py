# -*- coding: utf-8 -*-
"""周度决策 + 模拟盘账本编排（**每周六 20:00 跑**；数据用交易日 19:00 本机统一取数产物，**决策日不重新取数**；2026-10-31 起周三已取消）。

它把三件事串起来：
  1. 拿到「本期目标组合」：决策日默认 `--reuse` 复用工作日已跑好的 out/ 产物（决策日不取数）；只有手工补跑/首次才跑三阶段管线（run.py）；
  2. 用 paper.py 的账本把这套目标翻译成订单、撮合上一个交易日以来的成交、结算净值；
  3. 出一份可直接读的周报 + 一份「下一交易日执行」的订单清单和命令。

用法：
  # 首次建账（只建 state，不跑管线）
  python scripts/weekly.py --init --capital 50000 --tier B

  # 决策日法（周六 20:00）：不取数，复用工作日已取产物
  python scripts/weekly.py --reuse --capital 50000 --tier B

  # 复用当天已经跑好的 out/ 产物（调试或补跑）
  python scripts/weekly.py --reuse

  # 指定决策日做复盘
  python scripts/weekly.py --reuse --date 2026-10-24

设计约束：
  * 决策日 as_of = 今天之前（含）的最后一个交易日；最早成交日 fill_from = as_of 的下一个交易日。
    这样「周六晚上看到周五收盘价 → 下周一卖出按开盘价 / 买入按限价条件单执行」，不存在用当天开盘价成交的未来函数。
  * 决策日之后才可能成交，所以每一期都要先把上一期挂着的订单撮合掉，再生成新订单。
  * 状态文件默认 LEDGER_DIR/state_50k.json（本部署 = <本技能目录>/ledger/state_50k.json；可用 --state 覆盖）；决策日收尾用 `--refresh-net` 刷新周报净利润（不取数、不动账本）。
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import capital as cap
import config
import datasource as ds
import industry
import metrics as mx
import otc_book
import paper

CFG = config.PAPER
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = config.OUT_DIR
PAPER_OUT = CFG["package_dir"]
WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 小工具
def today_str() -> str:
    return dt.date.today().isoformat()


def load_calendar() -> list:
    df = ds.fetch_trade_calendar()
    cal = [str(x) for x in df["trade_date"].tolist()]
    return sorted(cal)


def last_trading_day(cal: list, day: str) -> str:
    prior = [d for d in cal if d <= day]
    if not prior:
        raise RuntimeError(f"交易日历里找不到 {day} 之前的交易日")
    return prior[-1]


def next_trading_day(cal: list, day: str) -> str:
    after = [d for d in cal if d > day]
    if not after:
        raise RuntimeError(f"交易日历里找不到 {day} 之后的交易日（日历需要更新）")
    return after[0]


def weekday_cn(day: str) -> str:
    return WEEKDAY[dt.date.fromisoformat(day).weekday()]


def run_pipeline(capital: float, tier: str, override: bool = False, limit: int = None) -> None:
    cmd = [sys.executable, os.path.join(HERE, "run.py"),
           "--capital", str(capital), "--tier", tier]
    if override:
        cmd.append("--override-regime")
    if limit:
        cmd += ["--limit", str(limit)]
    print(f"[weekly] 跑三阶段管线：{' '.join(cmd)}")
    r = subprocess.run(cmd, env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                 "TQDM_DISABLE": "1"})
    if r.returncode != 0:
        raise SystemExit(f"三阶段管线失败（退出码 {r.returncode}），本期不出订单")


def load_plan() -> dict:
    path = os.path.join(OUT, "stage3_plan.json")
    if not os.path.exists(path):
        raise SystemExit(f"找不到 {path}，请先跑 run.py 或去掉 --reuse")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_verdicts() -> dict:
    """阶段二的结论表：{code: {"verdict":…, "score":…}}，用来判断「该不该撤出」。"""
    path = os.path.join(OUT, "stage2_judged.csv")
    out = {}
    if not os.path.exists(path):
        return out
    df = pd.read_csv(path, dtype={"code": str})
    for _, r in df.iterrows():
        code = str(r.get("code") or "").strip()
        if not code:
            continue
        try:
            score = float(r.get("total"))
        except (TypeError, ValueError):
            score = 0.0
        out[code] = {"verdict": str(r.get("verdict") or ""), "score": score}
    return out


BAD_VERDICTS = {"回避", "数据不足"}


# ---------------------------------------------------------------- 行情
def bars_for(codes: list, etf_codes: set, upto: str = None) -> tuple:
    """取每只标的自上市以来的日线（只保留 ≤ upto 的部分），返回 (bars_map, close_map, atr_map)。"""
    bars_map, close_map, atr_map = {}, {}, {}
    for code in sorted(set(codes)):
        try:
            if code in etf_codes:
                df = ds.fetch_etf_hist(code)
            else:
                df = ds.fetch_daily(code)
        except Exception as exc:  # noqa: BLE001
            print(f"[weekly] 取 {code} 日线失败：{exc}")
            continue
        if df is None or len(df) == 0:
            continue
        b = []
        for _, r in df.iterrows():
            b.append({"date": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime")
                      else str(r["date"])[:10],
                      "open": float(r["open"]), "high": float(r["high"]),
                      "low": float(r["low"]), "close": float(r["close"])})
        if upto:
            b = [x for x in b if x["date"] <= upto]
        if not b:
            continue
        bars_map[code] = sorted(b, key=lambda x: x["date"])
        close_map[code] = bars_map[code][-1]["close"]
        atr_src = df
        if code in etf_codes:
            # ETF 的 ATR 用前复权序列：分红除权日不复权收盘会假跳空、把 ATR 撑大，
            # 移动止损区间跟着放大。收盘价/成交价仍用不复权（止损价必须是真实价格）。
            try:
                dq = ds.fetch_etf_hist(code, adjust="qfq")
                if dq is not None and len(dq):
                    if upto:
                        dq = dq[dq["date"].astype(str).str[:10] <= upto]
                    if len(dq):
                        atr_src = dq
            except Exception as exc:      # noqa: BLE001 —— 复权取不到就退回不复权
                print(f"[weekly] {code} 前复权日线取不到，ATR 用不复权：{exc}")
        try:
            atr_map[code] = mx.atr(atr_src, 14)
        except Exception:  # noqa: BLE001
            atr_map[code] = None
    return bars_map, close_map, atr_map


def otc_navs(codes) -> dict:
    """场外基金「累计净值」序列：{code: [(date, value), ...]}（升序）。

    用累计净值而不是单位净值：分红/拆分会把单位净值压低，用单位净值算收益系统性偏低。
    取不到就返回空表（fail-closed：那笔挂单不成交，绝不按猜的价格入账）。
    """
    out = {}
    for code in sorted({str(c) for c in (codes or []) if c}):
        try:
            df = ds.fetch_fund_nav_history(otc_book.short_code(code), "累计净值走势")
        except Exception as e:  # noqa: BLE001
            print(f"[weekly] 场外净值取数失败 {code}：{e}")
            continue
        if df is None or len(df) == 0:
            continue
        out[code] = [(str(d)[:10], float(v)) for d, v in zip(df["date"], df["value"])]
    return out


def bench_close(day: str) -> float:
    df = ds.fetch_index_daily(CFG["bench_index"])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    sub = df[df["date"] <= day]
    if len(sub) == 0:
        return None
    return float(sub["close"].iloc[-1])


# ---------------------------------------------------------------- 目标组合
def _risk_warn_holdings(state: dict) -> set:
    """持仓里当前简称带风险警示（ST/*ST/退市整理）的 6 位代码集合。

    用交易所**当前**简称判断（`ds.fetch_code_names()`），不看账本里存的旧简称——
    被实施风险警示后交易所会立刻改名，账本里记的还是建仓时的名字。
    """
    holds = state.get("holdings") or {}
    codes = [c for c, h in holds.items()
             if isinstance(h, dict) and str(h.get("kind")) == "stock"]
    if not codes:
        return set()
    names = ds.fetch_code_names()
    mp = {str(c).strip().zfill(6): str(n) for c, n in zip(names["code"].tolist(),
                                                          names["name"].tolist())}
    return {str(c).strip()[-6:] for c in codes
            if paper.is_risk_warn_name(mp.get(str(c).strip()[-6:], ""))}


def build_targets(plan: dict) -> list:
    """把阶段三的「目标整手数」翻译成 paper.target_orders 需要的数据结构。

    * ETF 在阶段三没有止损价（ETF 没有估值锚），模拟盘统一套用 EXEC.stop_loss_pct 生成
      **计划止损占位价**，让「买入后亏 15% 必走」这条风控对 ETF 同样生效；
    * 个股计划止损占位价优先用 judge 给的（安全边际买入价 × 0.85），没有才退回价格 × (1-15%)；
      这两者都只是下单/括号单占位，**成交后由 paper.apply_fill 按「实际成交成本 ×0.85」重设**。
    """
    targets, seen = [], set()
    for row in list(plan.get("positions") or []) + list(plan.get("etf_positions") or []):
        code = row.get("code")
        shares = int(row.get("shares") or 0)
        if not code or shares <= 0 or code in seen:
            continue
        seen.add(code)
        is_etf = (row.get("layer") in ("ETF", "理财(R2)")) or str(code).startswith(
            ("sh5", "sz1", "sh51", "sz15"))
        price = float(row.get("price") or 0)
        stop = row.get("stop_price")
        if stop is None and price > 0:
            # 下单时的占位价（安全边际买入价 ×0.85）：成交后由 paper.apply_fill 按
            # 「实际成交成本 ×0.85」重设，括号单同步重锚（2026-11-18 规则）
            stop = round(price * (1 + float(config.EXEC["stop_loss_pct"])), 3)
        targets.append({
            "code": code,
            "name": row.get("name"),
            "kind": "etf" if is_etf else "stock",
            "layer": row.get("layer") or ("ETF" if is_etf else "核心仓"),
            "level": row.get("level") or ("R2" if row.get("layer") == "理财(R2)" else "R5"),
            "shares": shares,
            "price": price,
            "stop_price": stop,
            "target_price": row.get("target_price"),
            "score": float(row.get("score") or 0.0),
            "verdict": row.get("verdict") or "",
            "thesis": row.get("thesis"),
            "falsifiers": row.get("falsifiers") or [],
            "reason": f"{row.get('verdict') or ''}｜综合分 {row.get('score')}",
            "ladder": [{"tranche": int(b.get("tranche") or 1),
                        "price": float(b.get("price") or price),
                        "shares": int(b.get("shares") or 0)}
                       for b in (row.get("ladder") or []) if int(b.get("shares") or 0) > 0],
        })
    return targets


def industry_cap_block(orders: list, state: dict, close_map: dict,
                       cap_amount: float) -> tuple[list, list]:
    """行业上限硬拦截（#17）：买单不得把某个行业买到上限之上。

    技能口径：单行业 ≤25%，分母是**权益层上限**（cap_amount），不是总资产；
    只算股票层（个股 R3），ETF/债券层不占行业额度（与 plan 的分配口径一致）。
    现有持仓按市值、在挂买单按限价×股数计入名义额；未分类不占额度（另由
    巡检的「行业映射缺失」告警管）。返回 (保留的订单, 被拦下的订单)。
    """
    cap = float(config.EXEC.get("max_industry") or 0.25) * float(cap_amount or 0.0)
    if not orders or cap <= 0:
        return list(orders or []), []
    mp = industry.load_map()

    def _ind(code: str, fallback: str = "") -> str:
        row = mp.get(industry.norm_code(code)) or {}
        return industry.normalize(row.get("industry") or fallback, code)

    used: dict[str, float] = {}
    for h in state.get("holdings") or []:
        if str(h.get("kind") or "") != "stock":
            continue
        px = float(close_map.get(h.get("code")) or h.get("last") or h.get("cost") or 0.0)
        val = float(h.get("shares") or 0) * px
        ind = _ind(h.get("code"), h.get("industry"))
        used[ind] = used.get(ind, 0.0) + val
    for o in state.get("orders") or []:
        if str(o.get("status") or "pending") != "pending" or o.get("action") != "buy":
            continue
        if str(o.get("kind") or "") != "stock":
            continue
        px = float(o.get("limit_price") or o.get("ref_price") or 0.0)
        ind = _ind(o.get("code"), o.get("industry"))
        used[ind] = used.get(ind, 0.0) + px * int(o.get("shares") or 0)

    kept, blocked = [], []
    for o in orders:
        if o.get("action") != "buy" or str(o.get("kind") or "") != "stock":
            kept.append(o)
            continue
        px = float(o.get("limit_price") or o.get("ref_price") or 0.0)
        amt = px * int(o.get("shares") or 0)
        ind = _ind(o.get("code"), o.get("industry"))
        if ind != industry.UNCLASSIFIED and used.get(ind, 0.0) + amt > cap + 1e-6:
            blocked.append({**o, "blocked_industry": ind,
                            "industry_after": used.get(ind, 0.0) + amt})
            continue
        used[ind] = used.get(ind, 0.0) + amt
        kept.append(o)
    return kept, blocked


# ---------------------------------------------------------------- 期次快照与差异
def snap_file(suffix: str = "") -> str:
    return os.path.join(PAPER_OUT, f"decision{suffix}_latest.json")


def load_snapshot(suffix: str = "") -> dict:
    path = snap_file(suffix)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _backup_before_write(path: str) -> str:
    """写同名报告前先把旧版备份到统一文件夹 `PAPER_OUT/_history/`（2026-11-21 用户要求：
    「覆盖就覆盖吧，到时候读个备份对比一下不就行咯，可以统一备份到一个周报文件夹」）。

    备份名 = 原文件名（不带时间戳）——**每个数据日只留一份原版**：第一次写入前拷走的是
    原版，之后再重跑不再覆盖备份，方便直接 diff「备份 vs 当前」。返回备份路径；无旧文件
    或备份失败时返回 ""（失败只打 stderr，不阻断出报）。
    """
    try:
        if not path or not os.path.exists(path):
            return ""
        import shutil
        hdir = os.path.join(PAPER_OUT, "_history")
        os.makedirs(hdir, exist_ok=True)
        dst = os.path.join(hdir, os.path.basename(path))
        if os.path.exists(dst):
            return dst              # 已备份过：保留最早那份原版，不覆盖
        shutil.copy2(path, dst)
        return dst
    except OSError as exc:              # noqa: BLE001 - 备份失败不能拖垮决策
        print(f"[weekly] 报告备份失败（不影响出报）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return ""


def snap_archive_file(suffix: str, as_of: str) -> str:
    """按**数据日**归档的期次快照路径（decision{后缀}_{YYYY-MM-DD}.json）。"""
    return os.path.join(PAPER_OUT, f"decision{suffix}_{as_of}.json")


def _dated_snaps(suffix: str) -> list:
    """[(as_of, path)]，按数据日升序。只认归档文件，不认 decision*_latest.json。"""
    import re as _re
    pat = _re.compile(r"^decision" + _re.escape(suffix) + r"_(\d{4}-\d{2}-\d{2})\.json$")
    rows = []
    try:
        for name in os.listdir(PAPER_OUT):
            mt = pat.match(name)
            if mt:
                rows.append((mt.group(1), os.path.join(PAPER_OUT, name)))
    except OSError:
        return []
    rows.sort()
    return rows


def prev_snapshot(suffix: str, cur_as_of: str) -> dict:
    """上一期快照：只认数据日**早于本期**的期次。

    为什么要有它（patrol_findings 2026-11-21）：原实现只读 decision*_latest.json，
    同一数据日重跑一次就把「上期」写成本期的数据日 → 周报出现「上期决策日 = 今天、
    相隔 0 天、区间 0 收益」。这里优先读按数据日归档的快照，latest 只在它的
    as_of 早于本期时才兜底（老账本第一周转新口径时用）。
    """
    for as_of, path in reversed(_dated_snaps(suffix)):
        if as_of < str(cur_as_of):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
    snap = load_snapshot(suffix)
    if snap and str(snap.get("as_of") or "") < str(cur_as_of):
        return snap
    return None


def prev_report_as_of(suffix: str, cur_as_of: str) -> str:
    """上一份周报的数据日：期次快照缺失时，账本表现区间起点的兜底。

    只读文件名（weekly{后缀}_{YYYY-MM-DD}.md），不读内容、不联网。
    """
    import re as _re
    pat = _re.compile(r"^weekly" + _re.escape(suffix) + r"_(\d{4}-\d{2}-\d{2})\.md$")
    dates = []
    try:
        for name in os.listdir(PAPER_OUT):
            mt = pat.match(name)
            if mt and mt.group(1) < str(cur_as_of):
                dates.append(mt.group(1))
    except OSError:
        return ""
    return max(dates) if dates else ""


def build_snapshot(plan: dict, targets: list, state: dict, as_of: str,
                   ledger: str, capital: float, mode: str) -> dict:
    """把「这一期到底决定了什么」冻结成一份快照，供下一期算变化。"""
    reg = plan.get("market_regime") or {}
    return {
        "as_of": as_of,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ledger": ledger,
        "capital": capital,
        "mode": mode,
        "regime": reg.get("regime"),
        "bench": reg.get("bench_price"),
        "dev": reg.get("dev"),
        "targets": [{
            "code": t["code"], "name": t["name"], "kind": t["kind"],
            "shares": int(t["shares"]), "price": float(t["price"] or 0.0),
            "stop": None if t.get("stop_price") is None else float(t["stop_price"]),
            "target": None if t.get("target_price") is None else float(t["target_price"]),
            "score": round(float(t.get("score") or 0.0), 2),
            "verdict": t.get("verdict") or "",
        } for t in targets],
        "holdings": {c: {"name": h.get("name"), "shares": int(h.get("shares") or 0),
                         "stop": h.get("stop_price"),
                         "cost": round(float(h.get("cost_basis") or 0.0), 4)}
                     for c, h in (state.get("holdings") or {}).items()},
        "pending": [{"id": o["id"], "code": o["code"], "action": o["action"],
                     "type": o["order_type"], "shares": int(o["shares"]),
                     "limit": o.get("limit_price")} for o in paper.pending_orders(state)],
    }


def save_snapshot(suffix: str, snap: dict) -> str:
    path = snap_file(suffix)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    # 同时按数据日归档：同一数据日重跑覆盖同一份，不会覆盖上一期；
    # 下一期靠归档文件认出「上期」，不再被 latest 的同日重跑带偏（2026-11-21 修）。
    as_of = str(snap.get("as_of") or "").strip()
    if as_of:
        try:
            with open(snap_archive_file(suffix, as_of), "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
        except OSError as exc:                       # noqa: BLE001 - 归档失败不影响本期出报
            print(f"[weekly] 期次快照归档失败（{exc}），仅写 latest", file=sys.stderr)
    return path


def diff_lines(prev: dict, cur: dict, bad_codes: list = None) -> list:
    """本期 vs 上期的可读变化。这是「每周复盘」的原料：先看清变了什么，再谈要不要改规则。"""
    if not prev:
        return ["- **首次决策**：没有上期快照可比；从今天起每期都会记录，下次给出变化对比。"]
    out = []
    try:
        days = (dt.date.fromisoformat(str(cur["as_of"]))
                - dt.date.fromisoformat(str(prev.get("as_of")))).days
    except (ValueError, TypeError):
        days = None
    out.append(f"- 对比基准：上期决策日 **{prev.get('as_of')}**"
               + (f"（相隔 {days} 天）" if days is not None else ""))
    if prev.get("regime") != cur.get("regime"):
        out.append(f"- 市场状态：{prev.get('regime')} → **{cur.get('regime')}**"
                   f"（沪深300 偏离 {float(prev.get('dev') or 0):+.1%} → "
                   f"{float(cur.get('dev') or 0):+.1%}）")
    else:
        out.append(f"- 市场状态：仍为 **{cur.get('regime')}**（偏离 "
                   f"{float(prev.get('dev') or 0):+.1%} → {float(cur.get('dev') or 0):+.1%}）")
    pt = {t["code"]: t for t in (prev.get("targets") or [])}
    ct = {t["code"]: t for t in (cur.get("targets") or [])}
    added = [ct[c] for c in ct if c not in pt]
    removed = [pt[c] for c in pt if c not in ct]
    kept = [c for c in ct if c in pt]
    if added or removed:
        seg = []
        if added:
            seg.append("新增 " + "、".join(f"{t['name']}({t['code']})" for t in added))
        if removed:
            seg.append("移出 " + "、".join(f"{t['name']}({t['code']})" for t in removed))
        out.append("- 目标组合变化：" + "；".join(seg) + f"；不变 {len(kept)} 只")
    else:
        out.append(f"- 目标组合：{len(ct)} 只全部不变")
    score_chg = []
    for c in kept:
        d0, d1 = float(pt[c].get("score") or 0.0), float(ct[c].get("score") or 0.0)
        if abs(d1 - d0) >= 0.5:
            score_chg.append((abs(d1 - d0), f"{ct[c]['name']}({c}) {d0:.2f} → **{d1:.2f}**"
                                           f"（{d1 - d0:+.2f}）"))
    if score_chg:
        score_chg.sort(reverse=True)
        out.append("- 评分变化（≥0.5 分）：" + "；".join(x[1] for x in score_chg[:6]))
    else:
        out.append("- 评分变化：不变（所有目标标的打分变动 <0.5 分）")
    stop_chg = []
    for c in kept:
        s0, s1 = pt[c].get("stop"), ct[c].get("stop")
        if s0 is not None and s1 is not None and abs(float(s1) - float(s0)) > 1e-9:
            stop_chg.append(f"{ct[c]['name']}({c}) {float(s0):.3f} → **{float(s1):.3f}**")
    if stop_chg:
        out.append("- 目标止损价调整：" + "；".join(stop_chg))
    ph, ch = (prev.get("holdings") or {}), (cur.get("holdings") or {})
    hstop = []
    for c in ch:
        if c in ph and ph[c].get("stop") is not None and ch[c].get("stop") is not None \
                and abs(float(ch[c]["stop"]) - float(ph[c]["stop"])) > 1e-9:
            hstop.append(f"{ch[c].get('name') or c}({c}) {float(ph[c]['stop']):.3f} → "
                         f"**{float(ch[c]['stop']):.3f}**")
    if hstop:
        out.append("- 持仓止损价上移：" + "；".join(hstop))
    pp = {o["id"] for o in (prev.get("pending") or [])}
    cp = {o["id"] for o in (cur.get("pending") or [])}
    if cp - pp or pp - cp:
        out.append(f"- 挂单变化：新增 {len(cp - pp)} 张、撤销/成交 {len(pp - cp)} 张"
                   f"（当前挂单 {len(cp)} 张）")
    if bad_codes:
        out.append("- 持仓被判回避/数据不足（触发撤出）：" + "、".join(bad_codes))
    out.append("- 说明：这里只陈述变化，不据此改任何阈值；要改规则必须走一次 git 提交。")
    return out


# ---------------------------------------------------------------- 报告
def decision_md(plan: dict, targets: list) -> str:
    reg = plan.get("market_regime") or {}
    tier = plan.get("tier") or {}
    etf_layer = plan.get("etf_layer") or {}
    lines = ["## 一、本期市场与目标组合", ""]
    lines.append(f"- 市场状态：**{reg.get('regime') or '未知'}**（沪深300 "
                 f"{reg.get('bench_price')} vs MA200 {reg.get('bench_ma200')}，"
                 f"偏离 {float(reg.get('dev') or 0):+.1%}）→ 总敞口上限 {tier.get('regime_cap'):.0%}")
    lines.append(f"- 档位 {tier.get('tier')}（{tier.get('tier_label')}）目标 "
                 f"{float(tier.get('tier_ratio') or 0):.0%}，"
                 f"实际权益 {float(tier.get('equity_ratio') or 0):.0%} = "
                 f"{float(tier.get('equity_amount') or 0):,.0f} 元"
                 f"{'（被市场状态压回）' if tier.get('capped_by_regime') else ''}")
    if plan.get("pick_note"):
        lines.append(f"- 选股：{plan['pick_note']}")
    if etf_layer.get("note"):
        lines.append(f"- ETF 层：{etf_layer['note']}")
    wl = plan.get("wealth_layer") or {}
    if wl:
        _spent = float(wl.get("spent") or 0)
        if _spent > 0:
            lines.append(f"- R2 理财层：预算 {float(wl.get('budget') or 0):,.0f} 元，"
                         f"落地 {_spent:,.0f} 元"
                         f"（{_wealth_desc(plan)}）")
        elif wl.get("note"):
            lines.append(f"- R2 理财层：{wl['note']}")
    rv = plan.get("risk_review") or {}
    if rv:
        _w = rv.get("weights") or {}
        _wtxt = "／".join(f"{k} {float(v) * 100:.0f}%" for k, v in sorted(_w.items()))
        _rv_line = (f"- 风险等级评审：组合 **{rv.get('level_label')}**"
                    f"（加权 R{float(rv.get('level') or 0):.2f}，允许 R{rv['range'][0]:.1f}–R{rv['range'][1]:.1f}）"
                    f"｜{_wtxt or '无持仓'}｜R2 占比 {float(rv.get('r2_share') or 0):.1%}"
                    f"（目标 {float(rv.get('r2_target') or 0):.0%}）"
                    f"｜{'✓ 通过' if rv.get('ok') else '✗ 未通过，本期不新建仓'}")
        lines.append(_rv_line)
        for _v in (rv.get("violations") or [])[:4]:
            lines.append(f"  - ✗ {_v}")
        for _wmsg in (rv.get("warnings") or [])[:4]:
            lines.append(f"  - ! {_wmsg}")
    lines.append("")
    lines.append("| 代码 | 名称 | 类型 | 目标股数 | 参考价 | 目标金额 | 止损价（占位） | 目标价 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for t in targets:
        amt = (t["price"] or 0) * t["shares"]
        stop = "—" if t["stop_price"] is None else f"{float(t['stop_price']):.3f}"
        tgt = "—" if t["target_price"] is None else f"{float(t['target_price']):.3f}"
        lines.append(f"| {t['code']} | {t['name']} | {'ETF' if t['kind'] == 'etf' else '个股'} | "
                     f"{t['shares']} | {t['price']:.3f} | {amt:,.0f} | {stop} | {tgt} |")
    lines.append("")
    lines.append("> 口径：「目标金额」= 现价 × 目标整手数（目标市值，不是当期实际挂单金额）。实际下单按三批建仓价，"
                 "**三批一律是限价条件单**（第 1 批限价 = min(现价, 买入价×1.02)，第 2/3 批 = 买入价×0.95/0.90）："
                 "跌到该价才成交，10 个交易日不成交自动失效；"
                 "卖出（减仓/清仓/止损）按下一交易日开盘价或 GTC 止损价成交。"
                 "表里的「止损价（占位）」= 安全边际买入价×0.85，成交后按**实际成交成本×0.85** 重设。")
    lines.append("")
    return "\n".join(lines)


def position_text(state: dict, pf: dict) -> str:
    """一句话仓位：有在挂/场外目标时标出「含在挂目标敞口」。"""
    ratio, exposure = paper.position_exposure(state, pf)
    txt = f"仓位 {ratio:.1%}"
    if exposure - ratio > 0.0005:
        txt += f"（含在挂目标敞口 {exposure:.1%}）"
    return txt


def weekly_md(state: dict, plan: dict, targets: list, as_of: str, fill_from: str,
              fills: list, new_orders: list, actions: list = None,
              label: str = "", mode: str = "", cash_yield: float = 0.0,
              changes: list = None, investable: dict = None,
              fill_mode: str = "", filled_now: list = None,
              repo: dict = None, decision_day: str = "",
              data_as_of: str = "", week: dict = None,
              navst: dict = None, cash_int: dict = None,
              net: dict = None) -> str:
    nav_before = state["nav"][-1]["nav"] if state.get("nav") else None
    base_cap = float(state["meta"]["capital"])
    inv = investable or {}
    cap_txt = f"本金 {base_cap:,.0f} 元"
    if inv and abs(float(inv.get("investable", base_cap)) - base_cap) > 0.01:
        cap_txt += (f"｜**可投资本 {float(inv['investable']):,.0f} 元**"
                    f"（已实现盈利 {float(inv['gain']):+,.0f} 元"
                    + ("，已达可投资本上限，超出部分留现金层" if inv.get("clamped") else "") + "）")
    out = [f"# 投资周报 · {as_of}（{weekday_cn(as_of)}）{'· ' + label if label else ''}", ""]
    fill_txt = (f"成交口径：决策日 {as_of} 收盘价（模拟盘）"
                if fill_mode == "decision_close"
                else f"最早成交日 {fill_from}（{weekday_cn(fill_from)}）")
    dec_txt = (f"决策时点 {decision_day}（{weekday_cn(decision_day)}）20:00｜"
               if decision_day and decision_day != as_of else "")
    data_txt = (f"数据截至 {data_as_of}（{weekday_cn(data_as_of)}）收盘"
                if data_as_of else f"数据截至 {as_of}（{weekday_cn(as_of)}）收盘")
    stale_warn = ""
    if data_as_of and data_as_of < as_of:
        stale_warn = (f"｜⚠️ 数据不是最新交易日（应有 {as_of} 收盘，实际 {data_as_of}；"
                      f"多为本机交易日 19:00 统一取数失败/未更新——决策日不重新取数，"
                      f"先在工作日补跑本机取数再重新决策）")
    out.append(f"> 模拟盘（paper）｜{cap_txt}｜"
               f"档位 {state['meta']['tier']}｜{dec_txt}"
               f"{data_txt}｜"
               f"{fill_txt}｜"
               f"调仓模式 {mode or CFG.get('rebalance_mode', 'regime_change')}{stale_warn}")
    out.append("")
    out.append("")
    p = paper.portfolio(state, {c: h["last_close"] for c, h in state["holdings"].items()})
    st = paper.stats(state) or {}
    _tr, _br = p.get("total_return"), p.get("bench_return")
    _ex = (_tr - _br) if (_tr is not None and _br is not None) else None
    _nav = p.get("nav") or 0.0
    _gross = (_nav - base_cap) if _nav else None
    _ann = p.get("annualized")
    _pos = ((p.get("market_value") or 0) / _nav) if _nav else 0.0
    _bk = float((cash_int or {}).get("booked") or 0.0)
    _pd = float((cash_int or {}).get("pending") or 0.0)
    out.append("## 一、收益与消耗（核心）")
    out.append("")
    out.append("| 指标 | 数值 |")
    out.append("|---|---|")
    out.append(f"| 净值 / 本金 | **{_nav:,.2f}** / {base_cap:,.0f} 元 |")
    if _gross is not None:
        out.append(f"| 账户收益 | **{_gross:+,.2f} 元（{_tr:+.2%}，年化 "
                   f"{_ann:+.2%}）** |" if _ann is not None else
                   f"| 账户收益 | **{_gross:+,.2f} 元（{_tr:+.2%}）** |")
    if _br is not None:
        _ex_txt = f"{_ex * 100:+.2f}pp" if _ex is not None else "—"
        out.append(f"| 同期沪深300 / 超额 | {_br:+.2%} / **{_ex_txt}** |")
    out.append(f"| 最大回撤 / 仓位 | {(p.get('drawdown') or 0):+.2%} / {_pos:.1%} |")
    out.append(f"| 现金 | {p.get('cash', 0):,.2f} 元"
               f"（已落账利息 {_bk:+,.2f} + 在途 {_pd:+,.2f}） |")
    if net:
        out.append(f"| **净利润 / 净收益率** | **{net['net']:+,.2f} 元 / {net['rate']:+.2%}** |")
    out.append("")
    out.append("> 口径：净利润 = 账户累计收益 − token 实扣（真实扣费，非年度计提）；周报不展开运行时费用；"
               "评分 / 档位 / 奖励只在月报做（一周看不出能力），明细见月报 scorecard。")
    out.append("")
    try:
        import config as _cfg
        _cr = _cfg.cash_rates()
        _side_name = "朝朝宝" if _cr.get("r1") else "货基"
        _mf = float(_cr.get("r1") or _cr.get("money_fund") or 0.0)
        _r7 = float(_cr.get("repo7") or 0.0)
        _f7 = (float(_cr.get("repo7_fee_annual_rate") or 0.0026)
               * float(_cr.get("repo_commission_discount", 1.0)))
        _dep = float(_cfg.PERF.get("deposit_rate") or 0.0)
        _cash = float(p.get("cash") or 0.0)
        if "pool" in str(_cr.get("cash_policy") or ""):
            _sl = _cr.get("semi_liquid") or {}
            _top = int(_sl.get("top_n") or 3)
            _cap = float(_sl.get("per_fund_cap") or 10000.0)
            out.append(
                f"> 现金层分工：挂单资金放{_side_name}（{_mf:.2%}，T+0）；"
                f"观望资金做**半流动性货币增值**——全市场货基按 **90 天实得年化** "
                f"取前 {_top} 支、每支 ≤{_cap:,.0f} 元快赎（单支每日快赎额度 1 万），"
                f"排名见 `out/cash_pool_rank.md`、每 30 天重排一次；"
                f"建仓弹药（权益上限 − 现有权益）留 T+0 等周一买单，不进池子。"
                f"同金额对照：全放{_side_name} ≈ {_cash * _mf:,.0f} 元/年；"
                f"定存线 {_dep:.2%} ≈ {_cash * _dep:,.0f} 元/年。"
                f"（逆回购已从现金层移出，改为投资层脉冲择时，见 `scripts/repo_pulse.py`。）")
            out.append("")
        elif _r7 and _mf:
            out.append(
                f"> 现金层分工：挂单资金放{_side_name}（{_mf:.2%}，T+0）；"
                f"观望资金买 7 天期以上逆回购（{_r7:.2%} 毛 − 年费 {_f7:.2%} = 净 {_r7 - _f7:.2%}；"
                f"7/14/28 天期按观望金额分档、年费率同为 0.26%，朝朝宝顶流动性）。"
                f"同金额对照：全放{_side_name} ≈ {_cash * _mf:,.0f} 元/年；全买逆回购 ≈ "
                f"{_cash * (_r7 - _f7):,.0f} 元/年；定存线 {_dep:.2%} ≈ {_cash * _dep:,.0f} 元/年。")
            out.append("")
    except Exception as exc:                        # noqa: BLE001 - 对照行失败不影响周报
        print(f"[weekly] 现金层对照行跳过（{exc}）")
    out.append("")
    out.append("## 二、本期操作")
    out.append("")
    out.append("### 成交")
    out.append("")
    if fills:
        out.append("| 日期 | 动作 | 代码 | 名称 | 股数 | 成交价 | 金额 | 费用 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for t_ in fills:
            out.append(f"| {t_['date']} | {'买入' if t_['action'] == 'buy' else '卖出'} | "
                       f"{t_['code']} | {t_['name']} | {t_['shares']} | {t_['price']:.3f} | "
                       f"{t_['amount']:,.1f} | {t_['fee']:.2f} |")
    else:
        out.append("- 本期无成交。")
    out.append("")
    out.append("### 订单与挂单")
    out.append("")
    if fill_mode == "decision_close":
        out.append("决策日收盘价当场成交的订单见「成交」；这里只剩未触发的限价/止损条件单。")
        out.append("")
        out.append(paper.orders_md([o for o in new_orders if o.get("status") == "pending"]
                                   + [o for o in paper.pending_orders(state)
                                      if o["order_type"] in ("buy_limit", "sell_stop")]))
    else:
        out.append(f"执行日：**{fill_from}（{weekday_cn(fill_from)}）**")
        out.append("")
        out.append(paper.orders_md(new_orders))
        pend_old = [o for o in paper.pending_orders(state) if o.get("status") == "pending"]
        if not new_orders and pend_old:
            out.append("")
            out.append(f"> 账本另有 {len(pend_old)} 张此前挂出、仍未成交的订单（本期未重复下单）；"
                       f"它们与本期新单一起，按各自生效日撮合：卖出按开盘价、买入按限价触价。")
    out.append("")
    out.append("### 为什么动 / 不动")
    out.append("")
    if actions:
        for a in actions:
            out.append(f"- {a}")
    else:
        out.append("- 本期不操作：持仓标的都没触发卖出条件（止损/回避/超仓位），也没有需要新买的目标。")
    out.append("")
    _dec = decision_md(plan, targets).replace("## 一、本期市场与目标组合", "### 市场状态与目标组合")
    out.append("## 三、策略与持仓（市场 / 目标组合 / 风险）")
    out.append("")
    out.append(_dec)
    out.append("")
    out.append("## 四、下期观察点")
    out.append("")
    if targets:
        for t_ in targets:
            fals = (t_.get("falsifiers") or [])[:2]
            sp = t_.get("stop_price")
            stop_txt = "—" if sp is None else f"{float(sp):.3f}"
            out.append(f"- **{t_['code']} {t_['name']}**：跌破 {stop_txt} 止损；"
                       + ("；".join(fals) if fals else "季度复核基本面是否仍成立"))
    else:
        out.append("- 本期目标组合为空，等下一期信号；现金继续按分工放朝朝宝/逆回购。")
    out.append("")
    out.append("## 五、边界")
    out.append("")
    out.append("- 这是**模拟盘**：不接券商、不产生真实委托；所有成交按缓存日线（开盘价/限价/触发价）撮合。")
    out.append("- 触发价不是保证成交价；跳空时按开盘价成交，可能比止损价更差。")
    out.append("- 不构成投资建议；策略在 A 股主板 + 场内 ETF 范围内验证（本部署本金 5 万）。")
    out.append("")
    out.append("## 附录：明细（按需看）")
    out.append("")
    try:
        import navstats as NS
        _ns = NS.render_md(navst or {})
        if _ns.lstrip().startswith("#"):
            _nl = _ns.splitlines()
            for _i, _ln in enumerate(_nl):
                if _ln.startswith("#"):
                    _nl[_i] = "### 账本表现（日度净值）"
                    break
            _ns = "\n".join(_nl)
        out.append(_ns)
        out.append("")
    except Exception as exc:                        # noqa: BLE001 - 表现小节失败不影响周报
        print(f"[weekly] 账本表现小节跳过（{exc}）")
    led = paper.report_md(state, {c: h["last_close"] for c, h in state["holdings"].items()},
                          "五、账户账本")
    _ll = led.splitlines()
    if _ll and _ll[0].startswith("#"):
        _ll[0] = "### 账户账本（持仓与订单明细）"
    _led = "\n".join(_ll).replace("\n## ", "\n### ")
    _led = (_led.replace("\n### 一、账户总览", "\n### 账户总览（账本原始）")
                  .replace("\n### 二、持仓明细", "\n### 持仓明细")
                  .replace("\n### 三、绩效统计", "\n### 绩效统计"))
    out.append(_led)
    out.append("")
    out.append("### 与上期相比的变化（复盘原料）")
    out.append("")
    if changes:
        out.extend(changes)
    else:
        out.append("- 无上期快照（首次决策）。")
    out.append("")
    if week and (week.get("days") or []):
        out.append("### 本周巡检汇总（周一到周五）")
        out.append("")
        out.append("> 只读已落盘的每日巡检记录，不重算行情；逐日明细见 `out/patrol_<日期>.md`。")
        out.append("")
        import housekeeping as _HK
        body = _HK.week_md(week).splitlines()
        body = [ln for ln in body if not ln.startswith("# ") and not ln.startswith("> ")
                and ln.strip()]
        out.extend(body)
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 主流程
def _wealth_desc(plan: dict) -> str:
    """R2 落地口径：场内债券 ETF 与场外债券基金分开写，不混称「债券 ETF」。"""
    wps = plan.get("wealth_positions") or []
    etf_n = len([p for p in wps if (p.get("venue") or "ETF") != "OTC"])
    otc_n = len(wps) - etf_n
    parts = []
    if etf_n:
        parts.append(f"{etf_n} 只债券 ETF")
    if otc_n:
        parts.append(f"{otc_n} 只场外债券基金（影账 T+1 确认）")
    return " + ".join(parts) if parts else "0 只"


def otc_md(state: dict) -> str:
    """场外腿明细（R2 影子账）：持仓 + 在挂单，单独一节，免得和场内混在一起。"""
    rows = otc_book.holdings_rows(state)
    pend = otc_book.pending(state)
    if not rows and not pend:
        return ""
    md = "\n\n## 场外债券基金（R2 层影子账）\n\n"
    md += ("场外按**金额法**记账：市值 = 投入金额 × (当日累计净值 / 买入日累计净值)；"
           "申购按提交日的下一交易日累计净值成交、T+1 确认；赎回费按持有天数取实测档"
           "（不足 90 天按最严档扣，满 90 天用基金披露的长期档）。\n\n")
    if rows:
        md += ("| 代码 | 名称 | 等级 | 投入 | 市值 | 浮动 | 累计净值（买入→最新） "
               "| 净值日 | 持有天数 | 赎回费率 |\n")
        md += "|---|---|---|---|---|---|---|---|---|---|\n"
        for r in rows:
            md += (f"| `{r['code']}` | {r['name']} | {r['level']} | {r['amount']:,.2f} | "
                   f"{r['value']:,.2f} | {r['unrealized']:+,.2f} "
                   f"({(r['unrealized_pct'] or 0):+.2%}) | "
                   f"{(r.get('acc_nav') or 0):.4f} → {(r.get('nav') or 0):.4f} | "
                   f"{r['nav_date']} | {r['hold_days']} | {r['redeem_fee']:.2%} |\n")
        md += (f"\n场外合计：投入 {otc_book.cost_total(state):,.2f} 元、"
               f"市值 {otc_book.market_value(state):,.2f} 元、"
               f"浮动 {otc_book.unrealized(state):+,.2f} 元、"
               f"已实现 {otc_book.realized_total(state):+,.2f} 元。\n")
    if pend:
        md += ("\n在挂（参考净值 单位/累计；成交按 `effective_from` 当日累计净值）：\n\n")
        for o in pend:
            _u, _c = o.get("nav_ref"), o.get("nav_ref_cum")
            _ref = (f"，参考 单位 {_u:.4f} / 累计 {_c:.4f}"
                    if _u is not None and _c is not None else "")
            md += (f"- `{o['id']}` {o['code']} {o['name']} "
                   f"{'申购' if o['action'] == 'buy' else '赎回'} {o['amount']:,.2f} 元"
                   f"（{o['effective_from']}{_ref}）\n")
    return md


def otc_allow_rebalance(state: dict, mode: str, rebuild: bool) -> bool:
    """场外腿是否允许动：非 regime_change / 本期重建 / 首次补建（三无账本）。"""
    if mode != "regime_change" or rebuild:
        return True
    return otc_book.bootstrap_needed(state)


def otc_targets(plan: dict) -> list:
    """阶段三「场外腿目标」：只取 `OTC:` 前缀的条目。

    为什么必须过滤（2026-10-31 修）：`wealth_positions` 混放两种 R2 承接物——
    买得起一手的场内债券 ETF（如 sh511520，走场内单成交）和买不起一手的场外
    债券基金（`OTC:*`，走影子账）。若把场内 ETF 也交给
    `otc_book.target_orders`，影子账没有这笔场内持仓 → 差额被误判成「场外申购」，
    凭空多挂一张买单、虚占现金（当晚即出现此类虚单）。
    """
    return [r for r in (plan.get("wealth_positions") or [])
            if str(r.get("code") or "").startswith("OTC:")]


def otc_nav_codes(state: dict, plan: dict) -> list:
    """取场外净值序列要覆盖的代码：持仓 + 在挂单 + 本期计划目标（去重、排序）。

    为什么必须带上在挂单（2026-11-07 修）：`effective_from` 已到的在挂申购单要靠
    净值序列撮合；若只取「持仓 + 新计划目标」，而新计划换了基金（本次 10-30 挂的
    3 只与新计划目标不同），旧挂单的净值永远取不到 → fail-closed 一直挂到下次
    重建，R2 腿实际落不了地。把在挂单代码并入即可正常 T+1 确认。
    """
    codes = set((state.get("otc_holdings") or {}).keys())
    codes |= {str(o.get("code")) for o in (state.get("otc_orders") or []) if o.get("code")}
    codes |= {str(r.get("code")) for r in otc_targets(plan) if r.get("code")}
    return sorted(codes)


def html_orders(new_orders: list, otc_new: list) -> list:
    """HTML 报告订单表：场内新单 + 场外腿新单（R2 影子账，别只藏在 CSV 里）。"""
    out = list(new_orders or [])
    out.extend({
        "layer": o.get("layer") or "理财(R2)", "code": o["code"], "name": o["name"],
        "action": "申购" if o["action"] == "buy" else "赎回",
        "tranche": "场外", "price": o.get("nav_ref"), "lots": None,
        "shares": None, "amount": o["amount"],
        # 场外订单必须同时带单位净值与累计净值：成交只用累计净值（otc_book.fill），
        # 报告里两列都展示，避免「单位净值 1.12、收益 +2.9%」被读成矛盾（B 类 #11）。
        "price_cum": o.get("nav_ref_cum"),
    } for o in (otc_new or []))
    return out


def refresh_net(ledger, as_of, state, suffix=""):
    """决策日收尾刷新：T8/计费结算后重算 token 实花/净利润，改写周报 md 与 HTML。

    为什么要有它（patrol_findings 2026-11-14）：weekly.py 在 T8 之前生成周报，
    当时 budget.spent 还没含本次决策的 token；T8 分段结算与 billing task-end 之后
    再跑一次本函数，周报里的净利润才与 scorecard/计费口径一致。
    只读账本、不重跑管线、不取数。
    """
    import re
    import billing as _B
    import score as _S
    cap_now = float(state["meta"]["capital"])
    spent = float(_B.budget_state(ledger, cap_now).get("spent_cny") or 0.0)
    ret_now = paper.stats(state).get("total_return") or 0.0
    ev = _S.evaluate(ret_now, spent, cap_now)
    net = {"gross": ev["gross_profit"], "spent": spent, "net": ev["net_profit"],
           "rate": ev["net_rate"], "grade": ev["grade"],
           "reward_pending": ev["reward_pending"], "reward_note": ev["reward_note"]}
    changed = []

    md_path = os.path.join(PAPER_OUT, f"weekly{suffix}_{as_of}.md")
    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        new_md = re.sub(r"\| \*\*净利润 / 净收益率\*\* \|.*",
                        f"| **净利润 / 净收益率** | **{net['net']:+,.2f} 元 / {net['rate']:+.2%}** |", md)
        if new_md != md:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(new_md)
            changed.append(md_path)

    html_path = ""
    try:
        import report_html as _R
        html_path = _R.report_path(ledger, as_of)
    except Exception as exc:                        # noqa: BLE001 - 刷新失败不影响账本
        print(f"[weekly] HTML 报告路径取不到（{exc}），只刷新 md")
    if html_path and os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        new_html = re.sub(r"｜净利润 [^｜<]*? 元（已扣[^）]*?）",
                          f"｜净利润 {net['net']:,.2f} 元（已扣运行时成本）", html)
        _cls = "pos" if net["net"] > 0 else ("neg" if net["net"] < 0 else "")
        new_html = re.sub(r"<tr><th>净利润 / 净收益率</th><td class='num[^']*'>.*?</td></tr>",
                          f"<tr><th>净利润 / 净收益率</th><td class='num {_cls}'>"
                          f"{net['net']:,.2f} 元 / {net['rate']:+.2%}</td></tr>", new_html)
        if new_html != html:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(new_html)
            changed.append(html_path)

    print(f"[weekly] --refresh-net：净利润 {net['net']:+,.2f} 元（{net['rate']:+.2%}）")
    print(f"[weekly]   已刷新：{', '.join(changed) if changed else '无文件变化（本期周报可能尚未生成）'}")
    return 0


def capital_match(plan_cap, invest: float, base: float) -> tuple:
    """out/stage3_plan.json 的本金是否属于这个账本（防「用别的本金的组合改本账本」）。

    返回 (ok, note)：
      * plan_cap 为空（旧产物没写 capital）→ 放行；
      * 计划本金 = 账本**可投资本**（本金+已实现盈利）→ 放行；
      * 计划本金 = 账本**本金**（决策日 `run.py --capital 50000` 按本金生成，
        账本已实现盈利使可投资本略高，差几十元）→ 放行，但打印差额；
        不放行会让「上周赚了 18 元」这种小差额把整周决策中止（无单一订单）。
      * 其余 → 中止（真串账，比如 10 万账本的组合拿给 5 万账本）。
    """
    if plan_cap is None:
        return True, ""
    pc = float(plan_cap)
    if abs(pc - invest) <= 1.0:
        return True, ""
    if base > 0 and abs(pc - base) <= 1.0:
        return True, (f"out/ 按本金 {pc:,.0f} 元生成，账本可投资本 {invest:,.0f} 元"
                      f"（差 {invest - pc:+,.0f}；按本金认同一账本继续）")
    return False, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="周度决策 + 模拟盘账本")
    ap.add_argument("--ledger", default=CFG.get("default_ledger", "50k"),
                    choices=sorted(CFG.get("ledgers", {"50k": {}}).keys()),
                    help="账本名（本部署单账本：50k）")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--tier", default=None)
    ap.add_argument("--override-regime", action="store_true")
    ap.add_argument("--state", default=None)
    ap.add_argument("--date", default=None,
                    help="决策日（默认今天；补跑/维护时用它把决策时点固定到真实决策日，否则周报会记成今天）")
    ap.add_argument("--reuse", action="store_true",
                    help="复用已跑好的 out/ 产物，不重跑管线（决策日周六 20:00 必须加：决策日不取数）")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只（调试）")
    ap.add_argument("--init", action="store_true", help="只建账不跑管线")
    ap.add_argument("--refresh-net", action="store_true",
                    help="决策日收尾：T8/计费结算后刷新周报 md+HTML 的 token 实花/净利润/档位三行"
                         "（不重跑管线、不取数、不动账本）")
    args = ap.parse_args(argv)

    led_cfg = (CFG.get("ledgers") or {}).get(args.ledger) or {}
    args.capital = float(args.capital if args.capital is not None
                         else led_cfg.get("capital") or 50000.0)
    args.tier = args.tier or led_cfg.get("tier") or config.CAPITAL["default_tier"]
    args.state = args.state or led_cfg.get("state_file") or CFG["state_file"]
    label = led_cfg.get("label") or f"{args.capital:,.0f} 元"
    # 调仓模式按账本取（本部署只有 50k，口径 regime_change：只在市场状态变化时重建；
    # once 保留为可选对照开关，incremental 保留兼容）。
    mode = led_cfg.get("rebalance_mode") or CFG.get("rebalance_mode", "regime_change")
    suffix = "" if args.ledger == CFG.get("default_ledger") else f"_{args.ledger}"
    print(f"[weekly] 账本 {args.ledger}（{label}）｜本金 {args.capital:,.0f} 元｜档位 {args.tier}｜"
          f"调仓模式 {mode}")

    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    os.makedirs(PAPER_OUT, exist_ok=True)
    cap.validate_capital(args.capital)

    cal = load_calendar()
    day = args.date or today_str()
    if not args.date and weekday_cn(day) != "周六":
        print(f"[weekly] ⚠ 今天不是决策日（{weekday_cn(day)}，决策日固定周六 20:00）："
              f"周报「决策时点」会记成 {day}。补跑/维护请加 --date <决策日 YYYY-MM-DD>，避免报表落错日期。")
    as_of = last_trading_day(cal, day)
    fill_from = next_trading_day(cal, as_of)
    print(f"[weekly] 决策日 {as_of}（{weekday_cn(as_of)}）→ 最早成交日 {fill_from}（{weekday_cn(fill_from)}）")

    bc = bench_close(as_of)
    if os.path.exists(args.state):
        state = paper.load(args.state)
        print(f"[weekly] 读账本 {args.state}：现金 {float(state['cash']):,.2f}，"
              f"持仓 {len(state['holdings'])} 只，挂单 {len(paper.pending_orders(state))} 张")
        if args.init:
            print("[weekly] 账本已存在，--init 不做任何修改")
            return 0
    else:
        state = paper.new_state(args.capital, args.tier, as_of, bc)
        paper.save(state, args.state)
        print(f"[weekly] 新建模拟盘账本 {args.state}：本金 {args.capital:,.0f} 元，"
              f"基准 {CFG['bench_index']} @ {bc}")
        if args.init:
            print("[weekly] --init 完成（只建账，不出订单）")
            return 0

    if args.refresh_net:
        return refresh_net(args.ledger, as_of, state, suffix)

    # 可投资本：本金 + 累计已实现盈利（用户规则「赚到的钱要再投出去」）。
    # 只加已实现、不缩本金、超过可投资本上限的部分留现金层——口径见 paper.investable_capital()。
    inv = paper.investable_capital(state)
    invest = inv["investable"]
    if abs(invest - inv["base"]) > 0.01:
        print(f"[weekly] 可投资本 {invest:,.0f} 元（本金 {inv['base']:,.0f} + 已实现盈利 "
              f"{inv['gain']:+,.0f}，{inv['growth_pct']:+.1%}）"
              + ("；已达可投资本上限，超出部分留在现金层" if inv["clamped"] else ""))

    if args.reuse:
        print("[weekly] --reuse：直接复用 out/ 里的阶段结果")
    else:
        if dt.date.fromisoformat(day).weekday() == 5:
            print("[weekly] ⚠️ 今天是周六（决策日）却未加 --reuse：会重新跑管线/取数。"
                  "决策日口径要求不取数，建议改用 --reuse；本次仍继续执行。")
        run_pipeline(invest, args.tier, args.override_regime, args.limit)

    plan = load_plan()
    plan_cap = plan.get("capital")
    data_as_of = str(plan.get("data_as_of") or "")
    base_cap = float((state.get("meta") or {}).get("capital") or 0.0)
    cap_ok, cap_note = capital_match(plan_cap, invest, base_cap)
    if cap_note:
        print(f"[weekly] ⚠ {cap_note}")
    if not cap_ok:
        print(f"[weekly] ✗ out/stage3_plan.json 是「{float(plan_cap):,.0f} 元」本金生成的，"
              f"与账本 {args.ledger} 当前可投资本「{invest:,.0f} 元」不符。")
        print(f"[weekly]   各账本共用一份 out/，决策日必须先跑对应本金的管线："
              f"run.py --capital {invest:,.0f} --tier {args.tier}，再跑 weekly.py --ledger {args.ledger}")
        print("[weekly]   已中止，避免用错本金的组合去改账本（会重复下单/超配）。")
        return 2
    targets = build_targets(plan)
    # 风险警示持仓：先算出来再从目标池剔除（清仓后本周不回补；规则在 paper.risk_exit_orders）
    try:
        _warn_codes = _risk_warn_holdings(state)
    except Exception as exc:                        # noqa: BLE001 - 名单取不到不阻断决策
        _warn_codes = set()
        print(f"[weekly] 风险警示名单未取到（跳过强制处置）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
    if _warn_codes:
        targets = [t for t in targets if str(t.get("code")).strip()[-6:] not in _warn_codes]
        print(f"[weekly] ⚠ 持仓被实施风险警示：{', '.join(sorted(_warn_codes))} → "
              f"下一交易日开盘清仓，本周不回补（已从目标池剔除）")
    verdicts = load_verdicts()
    mode = led_cfg.get("rebalance_mode") or CFG.get("rebalance_mode", "regime_change")
    # regime_change：与 backtest.py 同语义——只有市场状态（进攻/平衡/防守）变化才重建目标组合；
    # 状态没变 → 本期一张新单都不下，只保留止损/熔断/已挂限价单（2026-10-26验收口径：
    # 少交易、把收益建立在现金层 + 状态切换上，而不是每周交佣金）。
    cur_regime = (plan.get("market_regime") or {}).get("regime")
    rebuild = paper.rebuild_due(state, cur_regime, mode, investable=invest)
    eff_mode = "full" if (mode == "regime_change" and rebuild) else mode
    if mode == "regime_change":
        print(f"[weekly] regime_change：市场状态 {cur_regime}，"
              + ("重建目标组合（full 口径）" if rebuild else "与上期相同 → 本期不调仓（只保留止损/熔断）"))
    if rebuild:
        # 记下本次建仓用的可投资本，下次用它判断「赚到的钱够不够多、要不要再调一次仓」
        state.setdefault("meta", {})["last_rebuild_capital"] = round(float(invest), 2)
    prev_snap = prev_snapshot(suffix, as_of)  # 上期快照：只认更早数据日；同日重跑不把上期变成「今天」
    prev_period = str((prev_snap or {}).get("as_of") or "") or prev_report_as_of(suffix, as_of)
    # 该卖就卖：阶段二把持仓判成「回避/数据不足」→ 撤出；ETF 无阶段二结论，不算 bad
    bad_codes = [c for c in state["holdings"]
                 if (verdicts.get(c) or {}).get("verdict") in BAD_VERDICTS]
    etf_codes = {t["code"] for t in targets if t["kind"] == "etf"} | {
        c for c, h in state["holdings"].items() if h.get("kind") == "etf"}
    codes = ({h["code"] for h in state["holdings"].values()} | {t["code"] for t in targets}
             | {o["code"] for o in paper.pending_orders(state)})
    bars_map, close_map, atr_map = bars_for(codes, etf_codes, as_of)
    print(f"[weekly] 取到 {len(bars_map)} 只标的日线")
    otc_nav_map = otc_navs(otc_nav_codes(state, plan))
    if otc_nav_map:
        print(f"[weekly] 场外基金净值序列 {len(otc_nav_map)} 只")

    n_trades_before = len(state["trades"])
    bk = paper.backup_state(args.state)  # 改账本之前先留一份可回滚的快照
    if bk:
        _keep_days = CFG.get("backup_keep_days", 7)
        print(f"[weekly] 账本备份：{os.path.basename(bk)}"
              f"（保留最近 {_keep_days} 天 / {CFG.get('backup_keep', 30)} 份）")
        # 周决策顺便清理旧备份（只留最近 backup_keep_days=7 天，
        # 即删掉 8 天前及更早的上一周整批）。最新一份永不删（回滚底线）；失败不影响决策。
        try:
            pr = paper.prune_backups(args.state)
        except Exception as e:  # noqa: BLE001 备份清理是辅助动作，不能拖垮决策
            print(f"[weekly] 备份轮转失败（不影响决策）：{e}")
        else:
            msg = f"[weekly] 备份轮转：保留 {pr['kept']} 份（只留最近 {pr['keep_days']} 天）"
            if pr["expired"]:
                names = ", ".join(os.path.basename(x) for x in pr["expired"])
                msg += f"，删除上一周旧备份 {len(pr['expired'])} 份：{names}"
            if pr["over_count"]:
                msg += f"，删除超量 {len(pr['over_count'])} 份"
            print(msg)
    fill_mode = str(CFG.get("fill_price") or "next_open")
    if fill_mode == "decision_close":
        # 成交口径已改为「决策日收盘价」（模拟盘观察完就按收盘价买）。
        # 历史 buy_open（定在下一交易日开盘成交）不再使用，交给 target_orders 按本期目标重下并当场成交；
        # 已经挂着的条件单（buy_limit / sell_stop）保持不动——它们本来就是「等价格到了才买/卖」。
        stale = [o for o in paper.pending_orders(state, "buy") if o.get("fill_mode") != "decision_close"]
        for o in stale:
            paper.cancel_order(state, o, "成交口径改为决策日收盘价，重下")
        if stale:
            print(f"[weekly] 作废历史买单 {len(stale)} 张（整条梯子重下，改为决策日收盘价成交）")
    fills = paper.resolve_orders(state, as_of, bars_map)
    print(f"[weekly] 撮合：{len(fills)} 条事件")
    for kind, msg in fills:
        print(f"  - {msg}")

    # 现金层：先按上次结算日到今天的天数给现金计息（挂单资金朝朝宝 + 观望资金逆回购），再结算净值
    yield_amt = paper.accrue_cash(state, as_of, close_map, calendar=cal)
    if yield_amt:
        print(f"[weekly] 现金层计息：{yield_amt:+.2f} 元")
    # 场外影子账：先按 effective_from 当日累计净值撮合上一期挂的申购/赎回
    otc_book.ensure(state)
    for _lvl, _msg in otc_book.resolve(state, as_of, otc_nav_map):
        print(f"  - [场外] {_msg}")
    otc_book.mark(state, otc_nav_map)
    # 分红：除权日落在窗口内就记进现金（patrol 每日也会做，这里兜底；幂等）
    try:
        import dividends as _div
        _codes = [str(h.get("code") or "").zfill(6)
                  for h in (state.get("holdings") or {}).values()
                  if str(h.get("kind")) == "stock" and h.get("code")]
        if _codes:
            _got = paper.apply_dividends(state, as_of, _div.map_for(_codes, as_of))
            if _got.get("count"):
                print(f"[weekly] 分红入账：{_got['count']} 笔，现金 {_got['cash']:+.2f} 元")
    except Exception as _exc:                  # noqa: BLE001 - 分红取数失败不阻断决策
        paper.event(state, "warn", f"分红入账跳过：{_exc}", as_of)
    paper.settle(state, as_of, close_map, bc)
    repo = paper.repo_pnl(state)
    # 现金利息双行：已落账（events 里的 cash_yield）+ 在途（repo_open 应得利息估算）。
    # md 与 HTML 必须共用同一函数，否则 HTML 会出现「本期利息 0.00」而正文写「利息 12 元」。
    cash_int = paper.cash_interest_split(state)
    print(f"[weekly] 现金利息：已落账 {cash_int['booked']:+.2f} 元 + 在途 "
          f"{cash_int['pending']:+.2f} 元 = {cash_int['total']:+.2f} 元")
    if repo["ops"] or repo.get("open_lot"):
        n_open = 1 if repo.get("open_lot") else 0
        print(f"[weekly] 逆回购盈亏：{repo['ops']} 笔到期 + {n_open} 笔在途（每周一操作），"
              f"覆盖 {repo['covered_days']} 天（自然日跨度 {repo['span_days']} 天、空转 {repo['idle_days']} 天），"
              f"利息 {repo['interest']:.2f} 元 − 手续费 {repo['fees']:.2f} 元 = {repo['net']:.2f} 元，"
              f"实得年化 {repo['effective_annualized']:.2%}")
    _stop_orders = paper.stop_orders(state, as_of, close_map, atr_map, fill_from=fill_from)
    # 风险警示强制处置（与止损同级）：持仓被 ST/退市整理 → 下一交易日开盘清仓
    _risk_exits = (paper.risk_exit_orders(state, _warn_codes, as_of, fill_from=fill_from)
                   if _warn_codes else [])
    # 组合回撤熔断 = 清仓 + 本周不新建仓（2026-10-30 修：原来熔断后本周仍会挂新买单，
    # 与文档「熔断就停手」相反；文档侧统一改成「清仓 + 当周不建新仓」）。
    _halted = any("熔断" in str(o.get("reason") or "") for o in _stop_orders)
    # 风险等级评审闸门：阶段二的 plan 里带 risk_review，ERROR 级违规 → 本期不建新仓
    # （只保留止损/熔断；持仓不动、不新建、不加仓）
    _review = plan.get("risk_review")
    if not isinstance(_review, dict):
        _review = {"ok": False,
                   "violations": ["plan 里没有 risk_review 字段（fail-closed：写计划时没做组合评审）"]}
    if _halted:
        print("[weekly] ⚠ 组合回撤已触发熔断：本期全部清仓、不新建仓")
        paper.event(state, "warn", "组合回撤熔断：本期清仓，且当周不再新建仓", as_of)
        new_orders = []
    elif _review.get("ok") is not True:
        print("[weekly] ✗ 组合风险评审未通过，本期不新建仓（fail-closed）：")
        for _v in _review.get("violations") or []:
            print(f"    - {_v}")
        paper.event(state, "warn",
                    "组合风险评审未通过，本期不新建仓：" + "；".join(_review.get("violations") or []),
                    as_of)
        new_orders = []
    elif mode == "regime_change" and not rebuild:
        # 2026-11-15口径（B 类授权）：市场状态没变 ≠ 永远不建仓——
        # 闲置现金太多本身就是风险（现金层跑不过存款）。规则：
        #   * 只买不卖：卖出仍只由止损/熔断/超仓位减仓触发，避免每周来回换手；
        #   * 累计买入 ≤（权益上限 − 现有权益）×（1−缓冲），不突破该状态仓位上限；
        #   * 沿用 target_orders 的整手/三批/现金约束，不追高。
        _tier = plan.get("tier") or {}
        _cap = float(_tier.get("equity_amount") or 0) or float(plan.get("target_gross") or 0) * invest
        _eq0 = paper.equity_value(state, close_map)
        new_orders = paper.cap_topup_orders(
            state, targets, close_map, _cap, as_of, fill_from=fill_from, calendar=cal,
            bad_codes=bad_codes, month_trades=paper.month_trade_count(state, as_of))
        if new_orders:
            _amt = sum(float(o.get("ref_price") or o.get("limit_price") or 0) * int(o["shares"])
                       for o in new_orders)
            print(f"[weekly] regime_change：市场状态未变，但权益 {_eq0:,.0f} < 上限 {_cap:,.0f} → "
                  f"补建仓 {len(new_orders)} 张（约 {_amt:,.0f} 元，只买不卖）")
            paper.event(state, "info",
                        f"市场状态仍为 {cur_regime}；权益 {_eq0:,.0f}/{_cap:,.0f} 元，"
                        f"本期只做补建仓（{len(new_orders)} 张，不卖旧）", as_of)
        else:
            print("[weekly] regime_change：状态未变且权益接近上限 → 本期不下新单（只保留止损/熔断）")
            paper.event(state, "info",
                        f"市场状态仍为 {cur_regime}，权益 {_eq0:,.0f}/{_cap:,.0f}，本期不调仓", as_of)
    else:
        new_orders = paper.target_orders(state, targets, close_map, as_of,
                                         fill_from=fill_from, calendar=cal,
                                         mode=eff_mode, bad_codes=bad_codes,
                                         month_trades=paper.month_trade_count(state, as_of))
    # 行业上限硬拦截（#17）：只拦买单，止损/清仓/减仓单不受影响；
    # 分母用本周权益上限（cap_amount），与技能「单行业 ≤25%（权益层）」一致。
    if new_orders:
        _cap_amt = float((plan.get("tier") or {}).get("equity_amount") or 0) \
            or float(plan.get("target_gross") or 0) * invest
        new_orders, _ind_blocked = industry_cap_block(new_orders, state, close_map, _cap_amt)
        if _ind_blocked:
            _desc = "；".join(f"{o.get('name') or o['code']}（{o['blocked_industry']}"
                             f" → {o['industry_after']:,.0f} 元）" for o in _ind_blocked)
            print(f"[weekly] ⚠ 行业上限拦截 {len(_ind_blocked)} 张买单：{_desc}")
            paper.event(state, "warn", f"行业 >25% 上限，拦截买单：{_desc}", as_of)
    # 风险警示清仓单与本期新单合并（不受「状态未变不调仓」「熔断后不建仓」限制）
    if _risk_exits:
        new_orders = list(_risk_exits) + list(new_orders)
    # 仓位上限保护：超过市场状态允许的权益额就减仓（这是「必须撤资」的情形之一）
    # only once 模式不做（与 backtest 的 once 变体一致：建仓时定死仓位，之后只靠止损/熔断撤资），
    # 否则「买入持有对照组」会因行情漂移被反复砍仓，回测 -4.47% 的口径就不成立了。
    # 2026-10-30 修：原来写 `mode not in ("once","regime_change")`，而现行口径就是 regime_change
    # → 仓位上限减仓一次都不执行，「超过上限必须减仓」的护栏等于没接上（审计 #3）。
    tier = plan.get("tier") or {}
    cap_amount = float(tier.get("equity_amount") or 0) or float(plan.get("target_gross") or 0) * invest
    priority = {c: (verdicts.get(c) or {}).get("score", 0.0) for c in state["holdings"]}
    trim = []
    if eff_mode != "once" and not _halted:
        trim = paper.cap_trim_orders(state, cap_amount, close_map, as_of,
                                     priority=priority, fill_from=fill_from)
    new_orders += trim
    dropped = paper.enforce_cash(state, new_orders)
    print(f"[weekly] 新订单 {len(new_orders)} 张（其中减仓 {len(trim)} 张），现金约束砍掉 {len(dropped)} 张")

    # 成交口径 = 决策日收盘价：本期新单当场成交（买/卖都按 as_of 收盘价），
    # 止损/熔断产生的 sell_open 同样是「今天已跌破、今天就按收盘卖」，不留到次日开盘。
    filled_now = []
    if fill_mode == "decision_close":
        for o in new_orders:
            o["fill_mode"] = "decision_close"  # 打标记：下一期别把它当历史单作废
        filled_now += paper.execute_at_close(state, close_map, as_of, new_orders)
        same_day_sells = [o for o in paper.pending_orders(state, "sell")
                          if o["order_type"] == "sell_open" and o.get("effective_from") == fill_from]
        filled_now += paper.execute_at_close(state, close_map, as_of, same_day_sells,
                                             why="决策日收盘价成交（止损/熔断）")
        for kind, msg in filled_now:
            print(f"  - {msg}")
        if filled_now:
            paper.settle(state, as_of, close_map, bc)  # 成交后重算当日净值（同日覆盖）
            # 新买入的持仓也要有保护性止损单：成交后立刻补挂 GTC sell_stop（否则要等下一期决策）
            paper.stop_orders(state, as_of, close_map, atr_map, fill_from=fill_from)

    # 同一持仓被多张 GTC 止损重复覆盖（加仓括号单 + 旧 GTC）→ 合并成一张，只上移。
    # 不合并的话，跌破时第二张会因「持仓不足」抛错，把整次决策打挂。
    for _msg in paper.consolidate_stop_orders(state, as_of, effective_from=fill_from):
        print(f"[weekly] 止损合并：{_msg}")

    # 场外腿（R2 影子账）：只对 plan 里 `OTC:` 前缀的目标补差（场内债券 ETF 不进场外腿，
    # 2026-10-31 修）；与场内同一套 rebuild 口径。
    # 例外：三无账本（无持仓/无在挂单/无历史场外成交）允许首次补建一次——否则影子账上线
    # 前建仓的老账本，在 regime_change 状态未变时这条腿会一直空着（2026-10-30 修）。
    otc_allow = otc_allow_rebalance(state, mode, rebuild)
    otc_new = otc_book.target_orders(
        state, as_of, fill_from, otc_targets(plan), otc_nav_map,
        paper.available_cash(state) + otc_book.reserved(state), allow=otc_allow)
    if otc_new:
        cash_p = otc_book.reserved(state)
        print(f"[weekly] 场外腿新单 {len(otc_new)} 张（占用现金 {cash_p:,.2f} 元，"
              f"按 {fill_from} 累计净值成交）")
        for o in otc_new:
            print(f"  - {o['code']} {o['name']} {'申购' if o['action'] == 'buy' else '赎回'} "
                  f"{o['amount']:,.2f} 元 —— {o['reason']}")
    elif not otc_allow and (state.get("otc_holdings") or otc_targets(plan)):
        print("[weekly] 场外腿：本期不调仓（regime_change 且市场状态未变）")

    # 本期「为什么动/为什么不动」的说明，写进周报第三节
    actions = []
    if mode == "regime_change":
        if rebuild:
            actions.append(f"市场状态变化（→ {cur_regime}）→ 按目标组合重建（full 口径）")
        elif new_orders:
            actions.append(f"市场状态仍为 {cur_regime}（未变化）→ 只做补建仓：权益低于该状态上限、"
                           f"闲置现金太多，只买不卖（卖出仍只走止损/熔断/超限减仓）")
        else:
            actions.append(f"市场状态仍为 {cur_regime}（未变化）→ 权益已接近该状态上限，"
                           f"本期不调仓（只保留止损/熔断与在挂限价单）")
    for t in state["trades"][n_trades_before:]:
        actions.append(f"{t['date']} {'买入' if t['action'] == 'buy' else '卖出'} "
                       f"{t['code']} {t['name']} {t['shares']} 股 @ {t['price']:.3f}")
    for o in new_orders:
        tag = "已按决策日收盘价成交" if o.get("status") == "filled" else "待执行"
        actions.append(f"{tag}：{o['code']} {o['name']} "
                       f"{'买入' if o['action'] == 'buy' else '卖出'} {o['shares']} 股 —— {o['reason']}")
    for code, h in state["holdings"].items():
        if code in bad_codes:
            continue
        note = verdicts.get(code) or {}
        if note:
            actions.append(f"继续持有 {code} {h['name']}（阶段二 {note.get('verdict')} "
                           f"{note.get('score', 0):.2f} 分，未触发卖出条件）")
        else:
            actions.append(f"继续持有 {code} {h['name']}（ETF 无阶段二结论，未触发止损/超仓位）")
    if cap_amount > 0:
        actions.append(f"仓位上限校验：目标权益 {cap_amount:,.0f} 元，"
                       f"实际 {'未超' if not trim else '已超并减仓'}")

    # regime_change 的判定依据：本期市场状态存进账本，下期比对其是否变化。
    state["meta"]["last_regime"] = cur_regime
    # 权益上限也存一份：现金层要按「权益上限 − 现有权益」
    # 留出建仓弹药，钱必须周一就在券商账户（周六决策、场外货基周末不能赎回）。
    if cap_amount > 0:
        state["meta"]["equity_cap"] = round(float(cap_amount), 2)
    paper.save(state, args.state)

    period_trades = state["trades"][n_trades_before:]
    snap = build_snapshot(plan, targets, state, as_of, args.ledger, invest, mode)
    changes = diff_lines(prev_snap, snap, bad_codes)
    if prev_snap is None and prev_period:
        changes = [f"- **上期快照缺失**（数据日 {prev_period} 的期次快照已被同数据日重跑覆盖或未归档）："
                   f"区间起点取上一份周报日 {prev_period}，本期不给出 regime/目标/挂单的逐项差异。"]
    save_snapshot(suffix, snap)
    # 本周巡检汇总（周一到周五）：只读 daily_log 与账本 nav，0 token
    week = None
    try:
        import housekeeping as HK
        _d = dt.date.fromisoformat(as_of)
        _mon = (_d - dt.timedelta(days=_d.weekday())).isoformat()
        _fri = (dt.date.fromisoformat(_mon) + dt.timedelta(days=4)).isoformat()
        week = HK.week_digest(_mon, _fri)
    except Exception as exc:                        # noqa: BLE001 - 汇总失败不能拖垮周报
        print(f"[weekly] 本周巡检汇总生成失败（{exc}），其余照常")

    # 账本表现：用巡检工作日记下的日度净值算区间收益（不再只比两个决策日）
    navst = None
    try:
        import navstats as NS
        navst = NS.stats(state.get("nav") or [],
                         capital=float(state["meta"]["capital"]),
                         start=prev_period or None,
                         end=as_of)
    except Exception as exc:                        # noqa: BLE001
        print(f"[weekly] 账本表现计算失败（{exc}），周报照出")

    # 净利润口径：账户累计收益 − out/budget.json 该账本累计实扣（真实扣费，不用年度计提）。
    # 周报不展开运行时费用，只给净利润/档位；明细看 scorecard / 月报。
    net = None
    try:
        import billing as _B
        import score as _S
        cap_now = float(state["meta"]["capital"])
        spent = float(_B.budget_state(args.ledger, cap_now).get("spent_cny") or 0.0)
        ret_now = paper.stats(state).get("total_return") or 0.0
        _ev = _S.evaluate(ret_now, spent, cap_now)
        net = {"gross": _ev["gross_profit"], "spent": spent,
               "net": _ev["net_profit"], "rate": _ev["net_rate"], "grade": _ev["grade"],
               "reward_pending": _ev["reward_pending"], "reward_note": _ev["reward_note"]}
    except Exception as exc:                        # noqa: BLE001
        print(f"[weekly] 净利润口径计算失败（{exc}），周报仍出但缺该行")

    md = weekly_md(state, plan, targets, as_of, fill_from, period_trades, new_orders,
                   actions=actions, label=label, mode=mode, cash_yield=yield_amt, repo=repo,
                   changes=changes, investable=inv,
                   fill_mode=fill_mode, filled_now=filled_now,
                   decision_day=day, data_as_of=data_as_of, week=week, navst=navst,
                   cash_int=cash_int, net=net)
    # 收益拆解（权益 / 场外 R2 / 现金三层）：2026-11-08周报固定体现三层结构
    try:
        import breakdown as BD
        _bd = BD.render_md(BD.compute(state))
        _bd = "\n".join(("### " + _l[3:]) if _l.startswith("## ") else _l
                         for _l in _bd.splitlines())
        md += _bd
    except Exception as exc:                        # noqa: BLE001 - 拆解失败不能拖垮周报
        print(f"[weekly] 收益拆解生成失败（{exc}），其余照常")
    _otc = otc_md(state)
    _otc = "\n".join(("### " + _l[3:]) if _l.startswith("## ") else _l
                     for _l in _otc.splitlines())
    md += _otc
    rep_path = os.path.join(PAPER_OUT, f"weekly{suffix}_{as_of}.md")
    _backup_before_write(rep_path)      # 同数据日重跑前保留旧版（用户要求：周报有备份）
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(md)

    # 订单清单 CSV：留给「执行 agent」照着敲
    csv_path = os.path.join(PAPER_OUT, f"orders{suffix}_{as_of}.csv")
    rows = []
    for o in paper.pending_orders(state):
        px = float(o.get("limit_price") or o.get("ref_price") or 0)
        rows.append({"order_id": o["id"], "code": o["code"], "name": o["name"],
                     "kind": o["kind"], "action": o["action"], "order_type": o["order_type"],
                     "shares": o["shares"], "limit_or_ref": px,
                     "amount_est": round(px * o["shares"], 2),
                     "effective_from": o["effective_from"],
                     "expire_on": o.get("expire_on") or "GTC",
                     "reason": o["reason"]})
    for o in otc_book.pending(state):
        rows.append({"order_id": o["id"], "code": o["code"], "name": o["name"],
                     "kind": "fund", "action": o["action"], "order_type": "otc_nav",
                     "shares": None, "limit_or_ref": o.get("nav_ref"),
                     "amount_est": round(float(o["amount"]), 2),
                     "effective_from": o["effective_from"], "expire_on": "T+1确认",
                     "reason": o["reason"]})
    _backup_before_write(csv_path)      # 同数据日重跑前保留旧订单清单
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    # 选股评分面板：归档本期 judge 评分（只记录，不改任何分值；失败不影响账本/订单）
    try:
        import score_panel
        _basket_codes = [t["code"] for t in targets if (t.get("shares") or 0) > 0]
        _arch = score_panel.archive(data_as_of, basket=_basket_codes)
        print(f"[weekly] 评分面板：{_arch}")
    except Exception as exc:  # noqa: BLE001 — 归档失败不阻断决策
        print(f"[weekly] 评分面板归档失败（{exc}），不影响账本")

    # HTML 报告：分析 + 购买策略 + 未来计划（只读渲染，失败不影响账本/订单）
    html_path = ""
    try:
        import report_html
        judged, funnel = report_html.load_context()
        _html_p = report_html.report_path(args.ledger, as_of)
        _backup_before_write(_html_p)   # 同数据日重跑前保留旧 HTML（用户要求：报告有备份）
        # 订单表同时列场外腿（R2 影子账）新单：HTML 报告里也要看得见，别只藏在 CSV 里
        html_path = report_html.write_report(
            _html_p,
            as_of=as_of, ledger=args.ledger, plan=plan, state=state,
            judged=judged, funnel=funnel, targets=targets,
            orders=html_orders(new_orders, otc_new),
            changes=changes, investable=inv, mode=mode, cash_yield=yield_amt,
            cash_int=cash_int, repo=repo, data_as_of=data_as_of, decision_day=day,
            week=week, navst=navst,
            net=net)
    except Exception as exc:
        print(f"[weekly] HTML 报告生成失败（{exc}），md/csv 已写出")

    print()
    print("=" * 66)
    print(md.split("## 三、")[0].rstrip())
    print("=" * 66)
    print(f"[weekly] 周报：{rep_path}")
    print(f"[weekly] 订单：{csv_path}")
    if html_path:
        print(f"[weekly] HTML：{html_path}")
    if fill_mode == "decision_close":
        print("[weekly] 成交口径：决策日收盘价；本期新单已按收盘价当场成交，剩余条件单继续挂着。")
        print(f"[weekly] 想看某天的成交，可选补跑："
              f"{sys.executable} {os.path.join(HERE, 'paper_exec.py')} --ledger {args.ledger} --date <交易日>")
    else:
        print(f"[weekly] 成交口径：卖出按下一交易日开盘价、买入按限价条件单（{fill_from}）。不需要人工操作，"
              f"成交会在下一次决策时按订单类型自动撮合落账。")
        print(f"[weekly] 想提前看某天的成交，可补跑："
              f"{sys.executable} {os.path.join(HERE, 'paper_exec.py')} --ledger {args.ledger} --date {fill_from}")
    print(f"[weekly] 账本：{args.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
