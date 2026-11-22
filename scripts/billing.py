# -*- coding: utf-8 -*-
"""token 计费：从 DSH 的计费账本精确读「这一次运行」花了多少钱。

数据源（按优先级）：
    1. `$DSH_HOME/.dshw-usage.json` 里的 `events[]`
       每条 {ts(ms), day, model, cost(元), tokens}，这是**精确到单次请求**的真实花费，
       比查账号页面精确得多（账号页只有按天汇总）。
    2. `accounting.books[book].days[day].debitUnits / 1e8` —— 按天扣费，用于交叉校验。
    3. `lastBalance` —— 当前账户余额，用于 run 前后快照复核。
    4. 都拿不到时**不估算、不记账**：记账只认实扣，缺数据就 fail loud。

计费口径（周期结算）：本次花费 = 插件扣费记录 events[] 在「上次结算终点 → 现在」区间的累计
（金额 / token / 调用次数）；结算完把终点推进到现在（out/team/billing_cycle.json），绝不重复计数。
结算发生在**下一次决策会话的第 2 步**（本次会话还没结束、数不准）：一次结算覆盖上一次决策会话 +
期间巡检（巡检按 0.01 元/天定额记）；子代理会话落在同一份用量记录里，天然包含在内。
账户余额观测（accounting.debitUnits）只作参考字段，不参与记账。

可选：想单独量某一次任务窗口时，仍可 --task-begin 记快照、--task-end 结算该窗口（basis=snapshot）；
默认不需要开窗。

用法：
    python billing.py --task-end --ledger 50k          # 决策第 2 步：补记上一周期
    python billing.py --flat-yuan 0.01 --ledger 50k    # 巡检：0.01 元/天定额
    python billing.py --status --ledger 50k
    python billing.py --settle --day 2026-11-21       # 只读对账
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import uuid

import config

USAGE_CANDIDATE_ENV = "DSHW_USAGE"
BILLING_JSONL = os.path.join(config.OUT_DIR, "billing.jsonl")
BUDGET_JSON = os.path.join(config.OUT_DIR, "budget.json")


# ---------------------------------------------------------------- 数据源定位
def usage_path() -> str | None:
    """定位 .dshw-usage.json。找不到返回 None（不抛异常，让上层降级）。"""
    cands = []
    env = os.environ.get(USAGE_CANDIDATE_ENV)
    if env:
        cands.append(env)
    home = os.environ.get("DSH_HOME")
    if home:
        cands.append(os.path.join(home, ".dshw-usage.json"))
    # 兜底：从 WORK_DIR 往上找两级的同级目录（本机 DSH_HOME 常与 work 同级）
    wd = os.path.abspath(config.WORK_DIR)
    for _ in range(3):
        wd = os.path.dirname(wd)
        if not wd or wd == os.path.dirname(wd):
            break
        cands.append(os.path.join(wd, ".dshw-usage.json"))
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def load_usage(path: str | None = None) -> dict:
    p = path or usage_path()
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def events(data: dict | None = None) -> list[dict]:
    d = data if data is not None else load_usage()
    ev = d.get("events") or []
    return [e for e in ev if isinstance(e, dict) and "ts" in e and "cost" in e]


def usage_snapshot(at_ms: int | None = None, data: dict | None = None) -> dict:
    """用量记录的累计快照（快照差额计费的唯一数据源）。

    at_ms 给定时只累加 ts <= at_ms 的事件（--task-begin 可选窗口用）。
    """
    d = data if data is not None else load_usage()
    rows = [e for e in events(d) if at_ms is None or int(e["ts"]) <= int(at_ms)]
    tok = sum(int(e.get("tokens") or 0) for e in rows)
    cost = sum(float(e.get("cost") or 0.0) for e in rows)
    last = max((int(e["ts"]) for e in rows), default=0)
    return {"n_calls": len(rows), "tokens": tok,
            "cost_cny": round(cost, 6), "last_ts": last}


def snapshot_delta(before: dict | None, after: dict | None) -> dict:
    """收尾快照 - 开启快照。任一侧缺失返回 {}（调用方回落窗口求和）。"""
    if not before or not after:
        return {}
    return {
        "n_calls": int(after.get("n_calls") or 0) - int(before.get("n_calls") or 0),
        "tokens": int(after.get("tokens") or 0) - int(before.get("tokens") or 0),
        "cost_cny": round(float(after.get("cost_cny") or 0.0)
                          - float(before.get("cost_cny") or 0.0), 6),
    }


def balance(data: dict | None = None) -> float | None:
    """当前账户余额（元）。拿不到返回 None。"""
    d = data if data is not None else load_usage()
    v = d.get("lastBalance")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


ACCOUNT_SCALE = 1e8  # 小鲸鱼记账 accounting 的金额单位：1 元 = 1e8 units


def account_snapshot(data: dict | None = None) -> dict:
    """账户余额观测口径的累计实扣快照（小鲸鱼记账 accounting 账本）。

    取 `accounting.books[active].days[*].debitUnits` 求和：debitUnits 是「观测到余额下降」的
    累计值，所以跨天求和就是「至今观测到的实扣」。单位 1e8 = 1 元。
    返回 {} 表示没有账户口径（旧账本/插件没写过）→ 调用方回落逐轮事件口径并标注。
    """
    d = data if data is not None else load_usage()
    acc = d.get("accounting") or {}
    book = acc.get("active")
    books = acc.get("books") or {}
    if not book or book not in books:
        return {}
    b = books.get(book) or {}
    days = b.get("days") or {}
    if not isinstance(days, dict) or not days:
        return {}
    units = 0
    for info in days.values():
        try:
            units += int((info or {}).get("debitUnits") or 0)
        except (TypeError, ValueError):
            continue
    return {"units": units, "cny": round(units / ACCOUNT_SCALE, 8),
            "balance": balance(d), "observed_at": int(b.get("lastAt") or 0),
            "day": str(d.get("date") or ""), "books": len(books)}


def account_delta(before: dict | None, after: dict | None) -> dict:
    """收尾账户快照 − 开启账户快照。任一侧缺失返回 {}。"""
    if not before or not after:
        return {}
    try:
        bu, au = int(before.get("units") or 0), int(after.get("units") or 0)
    except (TypeError, ValueError):
        return {}
    return {"units": au - bu, "cny": round((au - bu) / ACCOUNT_SCALE, 8),
            "observed_at": int(after.get("observed_at") or 0),
            "day": str(after.get("day") or "")}


# ---------------------------------------------------------------- 时间口径
def parse_ts(x) -> int:
    """把 'YYYY-MM-DD HH:MM[:SS]' / ISO / 毫秒时间戳 统一成毫秒时间戳。"""
    if isinstance(x, (int, float)):
        v = int(x)
        # 10 位视为秒
        return v * 1000 if v < 10_000_000_000 else v
    s = str(x).strip()
    if not s:
        raise ValueError("空时间")
    if s.isdigit():
        return parse_ts(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(_dt.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    # 最后一搏：fromisoformat
    try:
        return int(_dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        raise ValueError(f"无法解析时间：{x!r}（用 'YYYY-MM-DD HH:MM' 或毫秒时间戳）")


def ts_to_str(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 核心：窗口计量
def day_debit_cny(day: str, data: dict | None = None) -> float | None:
    """交叉校验：accounting.books[active].days[day].debitUnits / 1e8 = 当日扣费（元）。"""
    d = data if data is not None else load_usage()
    acc = (d.get("accounting") or {})
    book = acc.get("active")
    books = acc.get("books") or {}
    if not book or book not in books:
        return None
    info = ((books[book].get("days") or {}).get(day))
    if not info:
        return None
    try:
        return float(info.get("debitUnits") or 0) / 1e8
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 任务窗口（唯一计费口径）
TASK_OPEN = os.path.join(config.OUT_DIR, "team", "task_open.json")

# 周期结算：账本按「上次结算结束 → 本次结算结束」切分（用户口径，直接查插件扣费记录）。
# events[] 没有 session id，所以切分只能靠时间边界；边界落盘，绝不重复计数。
CYCLE_FILE = os.path.join(config.OUT_DIR, "team", "billing_cycle.json")


def _cycle_read() -> dict:
    try:
        with open(CYCLE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _cycle_write(doc: dict) -> None:
    os.makedirs(os.path.dirname(CYCLE_FILE), exist_ok=True)
    with open(CYCLE_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)


def cycle_span(now_ms: int | None = None) -> tuple[int, int]:
    """返回 (上次结算终点, now)。没有历史终点时退化为「今天 0 点」。"""
    now = int(now_ms if now_ms is not None else _dt.datetime.now().timestamp() * 1000)
    prev = int(_cycle_read().get("last_end_ms") or 0)
    if prev <= 0:
        zero = _dt.datetime.combine(_dt.date.today(), _dt.time())
        prev = int(zero.timestamp() * 1000)
    return prev, now


def cycle_cost(now_ms: int | None = None) -> dict:
    """查插件 events 记录：返回 [上次终点, now] 内的积分（金额/token/调用次数）。"""
    since, now = cycle_span(now_ms)
    d = load_usage()
    rows = [e for e in events(d) if since < int(e.get("ts") or 0) <= now]
    return {"since_ms": since, "until_ms": now,
            "since": ts_to_str(since), "until": ts_to_str(now),
            "n_calls": len(rows),
            "tokens": int(sum(int(e.get("tokens") or 0) for e in rows)),
            "cost_cny": round(sum(float(e.get("cost") or 0.0) for e in rows), 6)}


def cycle_advance(now_ms: int | None = None) -> dict:
    """把周期终点推进到 now（结算后调用；只前移，不回退）。"""
    _, now = cycle_span(now_ms)
    doc = _cycle_read()
    prev = int(doc.get("last_end_ms") or 0)
    if now > prev:
        doc["last_end_ms"] = now
        doc["last_end"] = ts_to_str(now)
        doc["updated_at"] = ts_to_str(int(_dt.datetime.now().timestamp() * 1000))
        _cycle_write(doc)
    return doc


def _task_read() -> dict | None:
    try:
        with open(TASK_OPEN, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def task_open() -> bool:
    """任务窗口是否已开（只读判断，不结算、不关闭；team.py 用它决定要不要分段结算）。"""
    return _task_read() is not None


def _task_write(doc: dict) -> None:
    os.makedirs(os.path.dirname(TASK_OPEN), exist_ok=True)
    with open(TASK_OPEN, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)


def task_begin(ledgers: list | None = None, since_ms: int | None = None,
               note: str = "") -> dict:
    """任务开始：登记窗口起点。**定时任务的第一步必须调它。**

    为什么必须由任务自己登记：`events[]` 里没有 session id，无法事后区分「哪次消耗属于
    哪个任务」。唯一可靠的切分方式就是任务自己写下开始时刻。
    """
    lgs = sorted(ledgers or list(config.PAPER["ledgers"].keys()))
    now = int(_dt.datetime.now().timestamp() * 1000)
    if _task_read() is not None:
        # 上次任务忘了 --task-end：先把它结算掉，别让旧窗口把本次的时间一起吃掉
        leftover = task_settle(note="上一个任务窗口未收口，自动结算", close=True)
        _w = leftover.get("window") or {}
        print(f"[任务窗口] 上一轮未关窗（自 {_w.get('since', '?')} 起），"
              f"已自动结算 {float(leftover.get('cost_cny') or 0.0):.4f} 元")
    start_ms = int(since_ms if since_ms is not None else now)
    snap = usage_snapshot(at_ms=start_ms)
    acc = account_snapshot()
    doc = {"since_ms": start_ms,
           "ledgers": lgs, "begun_at": ts_to_str(now), "note": note, "steps": [],
           "snapshot": snap, "snapshot_at": ts_to_str(now),
           "account": acc, "account_at": ts_to_str(now)}
    _task_write(doc)
    return doc


def task_settle(note: str = "", close: bool = False) -> dict:
    """结算 [since_ms, now] 这段窗口，并把 since_ms 推到 now（分段结算、不重复）。

    close=True → 删掉 task_open.json（任务结束）。
    无窗口文件时走周期结算（上次终点 → 现在，ok=True）；窗口文件存在时按快照差额结算该窗口；失败才返回 ok=False，调用方不得自己编数字。
    """
    doc = _task_read()
    now = int(_dt.datetime.now().timestamp() * 1000)
    d = load_usage()
    if doc is None and not d.get("events"):
        # 读不到插件记录时不能当成「本周期花了 0 元」——那会让决策日第 2 步静默漏账、还把终点推过去
        return {"ok": False, "cost_cny": 0.0, "source": "events-cycle",
                "reason": (f"读不到插件扣费记录（{usage_path() or '未找到 .dshw-usage.json'}）："
                           "不记账、不推进结算终点；先确认 DSH_HOME 与用量插件是否正常")}
    if doc is None:
        # 周期结算：上次结算终点 → 现在，直接查插件 events 记录（用户口径，不用开窗）
        cc = cycle_cost(now)
        win = {"since": cc["since"], "until": cc["until"],
               "n_calls": cc["n_calls"], "tokens": cc["tokens"],
               "cost_cny": cc["cost_cny"], "basis": "events-cycle",
               "snapshot": {}, "events_cost_cny": cc["cost_cny"],
               "account_delta": account_delta(account_snapshot(), account_snapshot(data=d))}
        lgs = sorted(list(config.PAPER["ledgers"].keys()))
        each = round(float(win["cost_cny"]) / len(lgs), 6) if lgs else 0.0
        by_ledger = {}
        for i, lg in enumerate(lgs):
            amt = each if i < len(lgs) - 1 else round(float(win["cost_cny"]) - each * (len(lgs) - 1), 6)
            by_ledger[lg] = amt
            if abs(amt) < 1e-9:
                continue
            record_run(new_run_id(lg), lg,
                       {"since": win["since"], "until": win["until"], "cost_cny": amt,
                        "tokens": win["tokens"], "n_calls": win["n_calls"],
                        "basis": win["basis"], "events_cost_cny": amt,
                        "source": "events-cycle", "estimated": False},
                       note=(f"{note or '周期结算'}：{win['since']} → {win['until']}，"
                             f"{win['n_calls']} 次调用，{len(lgs)} 本均分"))
        cycle_advance(now)
        return {"ok": True, "window": win, "cost_cny": round(float(win["cost_cny"]), 6),
                "by_ledger": by_ledger, "closed": True, "ledgers": lgs,
                "source": "events-cycle", "estimated": False}
    snap0 = doc.get("snapshot") or {}
    if not snap0:
        return {"ok": False, "cost_cny": 0.0, "source": "task-window",
                "reason": "窗口缺少开启快照（旧版 task_open.json）：不估算、不记账；"
                          "请重新执行 billing.py --task-begin"}
    delta = snapshot_delta(snap0, usage_snapshot(data=d))
    ev_cost = round(max(0.0, float(delta.get("cost_cny") or 0.0)), 6) if delta else 0.0
    acc0 = doc.get("account") or {}
    acc1 = account_snapshot(data=d)
    ad = account_delta(acc0, acc1)
    # 记账口径：逐轮事件快照差额（每次调用都落一条 event；结算在下一次决策第 2 步，本次会话结束后才准）。
    # 账户余额观测差额只作为参考字段写进流水（观测是抽样的，结算瞬间可能还没反映最后一轮）。
    cost = ev_cost
    basis = "snapshot"
    win = {"since": ts_to_str(int(doc["since_ms"])), "until": ts_to_str(now),
           "n_calls": int(delta["n_calls"]) if delta else 0,
           "tokens": int(delta["tokens"]) if delta else 0,
           "cost_cny": cost, "basis": basis,
           "snapshot": delta or {},
           "events_cost_cny": ev_cost,
           "account_delta": ad or {}}
    lgs = doc.get("ledgers") or sorted(config.PAPER["ledgers"].keys())
    each = round(float(win["cost_cny"]) / len(lgs), 6) if lgs else 0.0
    by_ledger = {}
    for i, lg in enumerate(lgs):
        amt = each if i < len(lgs) - 1 else round(float(win["cost_cny"]) - each * (len(lgs) - 1), 6)
        by_ledger[lg] = amt
        if abs(amt) < 1e-9:
            continue
        record_run(new_run_id(lg), lg,
                   {"since": win["since"], "until": win["until"], "cost_cny": amt,
                    "tokens": win["tokens"], "n_calls": win["n_calls"],
                    "basis": win["basis"],
                    "events_cost_cny": round(ev_cost / len(lgs), 6) if lgs else 0.0,
                    "source": "task-window", "estimated": False},
                   note=(f"{note or '任务窗口结算'}：窗口 {win['since']} → {win['until']}，"
                         f"{win['n_calls']} 次调用，口径 {win['basis']}，{len(lgs)} 本均分"))
    doc["steps"] = (doc.get("steps") or []) + [
        {"at": ts_to_str(now), "since": win["since"], "until": win["until"],
         "cost_cny": round(float(win["cost_cny"]), 6), "n_calls": win["n_calls"]}]
    doc["since_ms"] = now
    cycle_advance(now)
    if close:
        try:
            os.remove(TASK_OPEN)
        except OSError:
            pass
    else:
        _task_write(doc)
    return {"ok": True, "window": win, "cost_cny": round(float(win["cost_cny"]), 6),
            "by_ledger": by_ledger, "closed": bool(close), "ledgers": lgs,
            "source": "task-window", "estimated": False}


def day_settled(day: str) -> float:
    """当天已进账本的金额（只认任务窗口口径）。用于和对账单做差额展示。"""
    doc = load_budget()
    total = 0.0
    for _lg, b in (doc.get("ledgers") or {}).items():
        for r in (b.get("runs") or []):
            if str(r.get("source")) not in ("task-window", "balance-accounting"):
                continue
            if str(r.get("recorded_at") or "")[:10] == day:
                total += float(r.get("cost_cny") or 0.0)
    return round(total, 6)


def rebaseline(drop_sources: tuple = ("balance-accounting", "dshw-usage.events"),
               note: str = "") -> dict:
    """把按天均分口径的历史流水剔掉，重算 spent/remaining（只动指定 source）。"""
    doc = load_budget()
    removed = []
    for lg, b in (doc.get("ledgers") or {}).items():
        keep = []
        for r in (b.get("runs") or []):
            if str(r.get("source") or "") in drop_sources:
                removed.append({"ledger": lg, "cost_cny": r.get("cost_cny"),
                                "source": r.get("source")})
            else:
                keep.append(r)
        b["runs"] = keep
        b["spent_cny"] = round(sum(float(r.get("cost_cny") or 0) for r in keep), 6)
        b["remaining_cny"] = round(float(b["base_cny"]) + float(b.get("bonus_in_cny") or 0)
                                   + float(b.get("carry_over_cny") or 0) - b["spent_cny"], 6)
        if note:
            b.setdefault("notes", []).append(
                {"at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "note": note, "bonus_cny": 0.0})
        doc["ledgers"][lg] = b
    save_budget(doc)
    return {"removed": removed, "budget": doc}


def purge_test(dry: bool = False) -> dict:
    """清掉 selftest / 手工测试写进真实预算账本的假记录，重算 spent/remaining。

    只动两样：source in (test, selftest) 的 run；note in (奖励, selftest) 的假奖励。
    真实奖励请用 `billing.py --bonus` 重新记（note 写成「评分奖励 A」这类可区分的名字）。
    """
    doc = load_budget()
    removed = []
    for lg, b in (doc.get("ledgers") or {}).items():
        keep = []
        for r in (b.get("runs") or []):
            if str(r.get("source") or "").lower() in ("test", "selftest"):
                removed.append({"ledger": lg, "kind": "run", "run_id": r.get("run_id"),
                                "cost_cny": r.get("cost_cny"), "note": r.get("note")})
            else:
                keep.append(r)
        b["runs"] = keep
        keepn = []
        for n in (b.get("notes") or []):
            if str(n.get("note") or "") in ("奖励", "selftest"):
                removed.append({"ledger": lg, "kind": "bonus_note",
                                "bonus_cny": n.get("bonus_cny"), "note": n.get("note")})
            else:
                keepn.append(n)
        b["notes"] = keepn
        b["bonus_in_cny"] = round(sum(float(n.get("bonus_cny") or 0) for n in keepn), 6)
        b["spent_cny"] = round(sum(float(r.get("cost_cny") or 0) for r in keep), 6)
        b["remaining_cny"] = round(float(b["base_cny"]) + b["bonus_in_cny"]
                                   + float(b.get("carry_over_cny") or 0.0) - b["spent_cny"], 6)
        doc["ledgers"][lg] = b
    if not dry:
        save_budget(doc)
    return {"removed": removed, "budget": doc}


# ---------------------------------------------------------------- 预算账本
def _empty_budget(capital: float, ledger: str) -> dict:
    base = float(config.SCORE["budget"].get(ledger, 150.0))
    return {
        "ledger": ledger,
        "capital": float(capital),
        "year": _dt.date.today().year,
        "base_cny": base,
        "bonus_in_cny": 0.0,
        "carry_over_cny": 0.0,
        "spent_cny": 0.0,
        "remaining_cny": base,
        "runs": [],
    }


def load_budget() -> dict:
    if not os.path.isfile(BUDGET_JSON):
        return {"version": 1, "ledgers": {}}
    try:
        with open(BUDGET_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("version", 1)
        d.setdefault("ledgers", {})
        return d
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "ledgers": {}}


def save_budget(doc: dict) -> str:
    os.makedirs(os.path.dirname(BUDGET_JSON), exist_ok=True)
    with open(BUDGET_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return BUDGET_JSON


def budget_state(ledger: str, capital: float | None = None) -> dict:
    doc = load_budget()
    b = doc["ledgers"].get(ledger)
    if b is None:
        cap = float(capital if capital is not None else _ledger_capital(ledger))
        b = _empty_budget(cap, ledger)
    return b


def _ledger_capital(ledger: str) -> float:
    try:
        return float(config.PAPER["ledgers"][ledger]["capital"])
    except (KeyError, TypeError):
        return float(config.SCORE["budget"].get(ledger, 150.0)) / 3.0 * 1000.0


def record_run(run_id: str, ledger: str, win: dict, capital: float | None = None,
               note: str = "") -> dict:
    """把一次运行的花费写进 billing.jsonl 与 budget.json，返回更新后的预算状态。"""
    doc = load_budget()
    cap = float(capital if capital is not None else _ledger_capital(ledger))
    b = doc["ledgers"].get(ledger) or _empty_budget(cap, ledger)
    b["capital"] = cap

    row = {
        "run_id": run_id,
        "ledger": ledger,
        "capital": cap,
        "started_at": win["since"],
        "ended_at": win["until"],
        "cost_cny": round(float(win["cost_cny"]), 6),
        "tokens": int(win["tokens"]),
        "n_calls": int(win["n_calls"]),
        "basis": str(win.get("basis") or "events"),
        "source": win.get("source", ""),
        "estimated": bool(win.get("estimated", False)),
        "account_delta_cny": (round(float((win.get("account_delta") or {}).get("cny") or 0.0), 8)
                              if win.get("account_delta") else None),
        "account_units": ((win.get("account_delta") or {}).get("units")
                          if win.get("account_delta") else None),
        "events_cost_cny": (round(float(win["events_cost_cny"]), 6)
                            if win.get("events_cost_cny") is not None else None),
        "note": note,
        "recorded_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    os.makedirs(os.path.dirname(BILLING_JSONL), exist_ok=True)
    with open(BILLING_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    b["runs"] = (b.get("runs") or []) + [row]
    b["spent_cny"] = float(b.get("spent_cny") or 0.0) + row["cost_cny"]
    b["remaining_cny"] = (float(b["base_cny"]) + float(b.get("bonus_in_cny") or 0.0)
                          + float(b.get("carry_over_cny") or 0.0) - b["spent_cny"])
    doc["ledgers"][ledger] = b
    save_budget(doc)
    return b


def add_bonus(ledger: str, bonus_cny: float, note: str = "") -> dict:
    """把奖励转成的预算增量写进账本（由 score.py 的结果驱动）。"""
    doc = load_budget()
    cap = _ledger_capital(ledger)
    b = doc["ledgers"].get(ledger) or _empty_budget(cap, ledger)
    b["bonus_in_cny"] = float(b.get("bonus_in_cny") or 0.0) + float(bonus_cny)
    b["remaining_cny"] = (float(b["base_cny"]) + float(b["bonus_in_cny"])
                          + float(b.get("carry_over_cny") or 0.0) - float(b.get("spent_cny") or 0.0))
    if note:
        b.setdefault("notes", []).append(
            {"at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note,
             "bonus_cny": float(bonus_cny)})
    doc["ledgers"][ledger] = b
    save_budget(doc)
    return b


def record_flat(ledger: str, amount_cny: float, note: str = "") -> dict:
    """按定额直接记一笔费用（不走 token 用量账本）。

    用于按次定额的运维类任务（如 20:00 工作日巡检简报），由 Lead 在任务收口时调用；
    与 record_run 写同一份 billing.jsonl / budget.json，保持净利润口径完整。
    """
    doc = load_budget()
    cap = _ledger_capital(ledger)
    b = doc["ledgers"].get(ledger) or _empty_budget(cap, ledger)
    b["capital"] = cap
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "run_id": new_run_id(ledger),
        "ledger": ledger,
        "capital": cap,
        "started_at": now,
        "ended_at": now,
        "cost_cny": round(float(amount_cny), 6),
        "tokens": 0,
        "n_calls": 1,
        "source": "flat",
        "estimated": True,
        "note": note or "billing.py --flat-yuan",
        "recorded_at": now,
    }
    os.makedirs(os.path.dirname(BILLING_JSONL), exist_ok=True)
    with open(BILLING_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    b["runs"] = (b.get("runs") or []) + [row]
    b["spent_cny"] = float(b.get("spent_cny") or 0.0) + row["cost_cny"]
    b["remaining_cny"] = (float(b["base_cny"]) + float(b.get("bonus_in_cny") or 0.0)
                          + float(b.get("carry_over_cny") or 0.0) - b["spent_cny"])
    doc["ledgers"][ledger] = b
    save_budget(doc)
    return b


def new_run_id(ledger: str) -> str:
    return f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{ledger}-{uuid.uuid4().hex[:6]}"


def snapshot(ledger: str) -> dict:
    """run 前快照：记余额，供事后人工复核。"""
    d = load_usage()
    return {
        "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ledger": ledger,
        "balance_cny": balance(d),
        "usage_file": usage_path(),
        "today_usage_cny": d.get("todayUsage"),
    }


def last_task(ledger: str | None = None) -> dict:
    """最近一次已结算任务（读 out/billing.jsonl 最后一行，实扣口径，只读）。"""
    best = None
    try:
        with open(BILLING_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if ledger and str(r.get("ledger") or "") != str(ledger):
                    continue
                best = r
    except OSError:
        return {"ok": False, "error": "out/billing.jsonl 不存在（还没记过账）"}
    if not best:
        return {"ok": False, "error": "还没有已结算的任务"}
    return {"ok": True, "ledger": best.get("ledger"),
            "started_at": best.get("started_at"), "ended_at": best.get("ended_at"),
            "cost_cny": float(best.get("cost_cny") or 0.0),
            "tokens": int(best.get("tokens") or 0),
            "n_calls": int(best.get("n_calls") or 0),
            "basis": best.get("basis") or "snapshot",
            "note": best.get("note") or ""}


# ---------------------------------------------------------------- CLI
def _print_status(ledger: str):
    b = budget_state(ledger)
    print(f"账本 {ledger} | 本金 {b['capital']:.0f} 元 | {b['year']} 年")
    print(f"  基准预算 {b['base_cny']:.2f} + 奖励转入 {b['bonus_in_cny']:.2f}"
          f" + 结转 {b['carry_over_cny']:.2f} = 可用 "
          f"{float(b['base_cny'])+float(b['bonus_in_cny'])+float(b['carry_over_cny']):.2f} 元")
    print(f"  已花 {b['spent_cny']:.4f} 元 | 剩余 {b['remaining_cny']:.4f} 元"
          f" | 运行 {len(b.get('runs') or [])} 次")
    _open = _task_read()
    if _open is not None:
        print(f"  [进行中] 可选任务窗口自 {_open.get('begun_at')} 起"
              f"（账本 {'/'.join(_open.get('ledgers') or [])}），等 --task-end 收口")
    _day = _dt.date.today().strftime("%Y-%m-%d")
    _debit = day_debit_cny(_day)
    _done = day_settled(_day)
    if _debit is not None:
        print(f"  今日 {_day} DS 全账户实扣 {_debit:.4f} 元 | 本技能已入账 {_done:.4f} 元"
              f" | 未计入 {_debit - _done:+.4f} 元（开发/对话等非任务消耗，正常）")
    runs = b.get("runs") or []
    for r in runs[-5:]:
        print(f"    {r['started_at']} → {r['ended_at']}  {r['cost_cny']:.4f} 元  "
              f"{r['tokens']} tok  {r['n_calls']} 次调用  [{r['source']}]")



def main():
    ap = argparse.ArgumentParser(description="token 计费与预算账本（读 DSH .dshw-usage.json）")
    ap.add_argument("--ledger", default="50k", choices=sorted(config.PAPER["ledgers"].keys()),
                    help="账本（决定预算档位），默认 50k")
    ap.add_argument("--flat-yuan", type=float, default=None, dest="flat_yuan",
                    help="按定额（元）直接记一笔费用（不走 token 用量），配合 --note 使用")
    ap.add_argument("--note", default="", help="[--flat-yuan] 备注")
    ap.add_argument("--snapshot", action="store_true", help="打印 run 前余额快照")
    ap.add_argument("--status", action="store_true", help="打印预算状态")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--task-begin", action="store_true",
                    help="可选：登记任务窗口起点（默认走周期结算，不需要开窗）")
    ap.add_argument("--task-end", action="store_true",
                    help="【决策第 2 步】补记上一周期：上次结算终点 → 现在（查插件扣费记录）")
    ap.add_argument("--settle", action="store_true",
                    help="只读对账：今日全账户实扣 vs 本技能已入账（不记账）")
    ap.add_argument("--day", default=None, help="对账日 YYYY-MM-DD，默认今天")
    ap.add_argument("--rebaseline", action="store_true",
                    help="剔除历史「按天均分」流水（source=balance-accounting），重算预算")
    ap.add_argument("--purge-test", action="store_true",
                    help="清掉 selftest/手工测试写进真实账本的假记录")
    a = ap.parse_args()

    if a.flat_yuan is not None:
        b = record_flat(a.ledger, a.flat_yuan, a.note)
        if a.json:
            print(json.dumps({"ledger": a.ledger, "flat_yuan": a.flat_yuan,
                              "budget": b}, ensure_ascii=False, indent=2))
        else:
            print(f"已记账（定额）：{a.ledger} 记 {a.flat_yuan:.4f} 元"
                  f"｜已花 {b['spent_cny']:.4f} / 剩余 {b['remaining_cny']:.4f} 元")
        return 0

    if a.purge_test:
        r = purge_test()
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            if not r["removed"]:
                print("没有需要清理的测试记录")
            for x in r["removed"]:
                print(f"  移除 [{x['ledger']}] {x['kind']} "
                      f"{x.get('cost_cny', x.get('bonus_cny'))} 元  {x.get('note') or ''}")
            for lg, b in r["budget"]["ledgers"].items():
                print(f"  {lg}: 已花 {b['spent_cny']:.4f} 元 | 剩余 {b['remaining_cny']:.4f} 元")
        return 0

    if a.task_begin:
        doc = task_begin(note="billing.py --task-begin")
        if a.json:
            print(json.dumps(doc, ensure_ascii=False, indent=2))
        else:
            print(f"任务窗口已开：{doc['begun_at']} 起｜账本 {', '.join(doc['ledgers'])}")
            _snap = doc.get("snapshot") or {}
            print(f"  逐轮事件快照：{_snap.get('n_calls', 0)} 次调用 / "
                  f"{_snap.get('tokens', 0)} tok / "
                  f"{float(_snap.get('cost_cny') or 0.0):.4f} 元（记账口径：收尾相减）")
            _acc = doc.get("account") or {}
            if _acc:
                print(f"  账户实扣快照（仅参考）：累计 {float(_acc.get('cny') or 0.0):.4f} 元"
                      f"（余额 {_acc.get('balance')}｜观测于 "
                      f"{ts_to_str(int(_acc.get('observed_at') or 0)) if _acc.get('observed_at') else '—'}）")
            print(f"  {TASK_OPEN}")
        return 0

    if a.task_end:
        r = task_settle(note="billing.py --task-end", close=True)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            if not r.get("ok"):
                print("未结算：" + str(r.get("reason")))
                return 1
            w = r["window"]
            print(f"任务窗口结算：{w['since']} → {w['until']}｜{w['n_calls']} 次调用"
                  f"｜共 {r['cost_cny']:.4f} 元｜口径 {w.get('basis')}")
            _ad = w.get("account_delta") or {}
            if _ad:
                print(f"  账户口径差额：{float(_ad.get('cny') or 0.0):+.4f} 元"
                      f"（{_ad.get('units')} units）")
            print(f"  逐轮事件分项：{float(w.get('events_cost_cny') or 0.0):.4f} 元")
            for lg, amt in r["by_ledger"].items():
                print(f"  {lg} 记 {amt:.4f} 元")
        return 0

    if a.rebaseline:
        r = rebaseline(note="剔除按天均分口径的历史流水（改为任务窗口口径）")
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            for x in r["removed"]:
                print(f"  剔除 [{x['ledger']}] {x['cost_cny']} 元 ({x['source']})")
            for lg, b in r["budget"]["ledgers"].items():
                print(f"  {lg}: 已花 {b['spent_cny']:.4f} 元 | 剩余 {b['remaining_cny']:.4f} 元")
        return 0

    if a.settle:
        day = a.day or _dt.date.today().strftime("%Y-%m-%d")
        debit = day_debit_cny(day)
        done = day_settled(day)
        if a.json:
            print(json.dumps({"day": day, "day_debit_cny": debit, "settled_cny": done,
                              "excluded_cny": (None if debit is None else round(debit - done, 6))},
                             ensure_ascii=False, indent=2))
        else:
            if debit is None:
                print(f"{day} 没有按天扣费记录")
            else:
                print(f"{day} DS 全账户实扣 {debit:.4f} 元｜本技能已入账 {done:.4f} 元"
                      f"｜未计入 {debit - done:+.4f} 元")
                print("  说明：未计入部分 = 开发会话/用户对话等非定时任务消耗，按口径不进账本。")
                print("  任务花费由下一次决策第 2 步统一补记；想当场结算也可直接 --task-end。")
        return 0

    if a.snapshot:
        s = snapshot(a.ledger)
        print(json.dumps(s, ensure_ascii=False, indent=2) if a.json else
              f"余额 {s['balance_cny']} 元 | 今日已用 {s['today_usage_cny']} 元 | 数据源 {s['usage_file']}")
        return 0

    _print_status(a.ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
