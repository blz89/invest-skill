#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""情绪层（涨停池 / 跌停池 / 炸板池 / 龙虎榜 / 热股榜 / 异动原因）—— **只在决策日（周三/周六）采集**。

为什么要这一层
--------------
事件面（`news.py`）看的是「公司自己发生了什么」，情绪层看的是「市场正在怎么对待它」。
两件事经常背离：一边业绩预增，一边被游资砸到炸板；一边没人提，一边龙虎榜机构净买。
对持有期 1–3 个月、单票 5%–10% 仓位的组合，后者决定**短期回撤**，前者决定**中期方向**。

这一层只做**风险旗标与温度计**，不产生买卖信号：
- 持仓跌停 / 炸板 / 龙虎榜净卖出 / 有异动原因 → 标出来，交给 T4 研究员与 Lead 判断；
- 市场温度（涨跌停家数、炸板数、龙虎榜只数）→ 作为「现在是不是个正常的市场」的背景，
  不参与打分、不改变仓位（仓位由 regime 规则决定）。

数据全部来自同花顺特色数据（`fuyao.py`）。没 key / 接口挂了 → 整层跳过（fail-soft）。

输入
----
- `out/stage3_plan.json` 持仓（个股）
- `out/stage1_candidates.csv` 候选（可选，最多 SENTIMENT["max_codes"] 只）

输出
----
- `out/sentiment.json`：机器可读（温度 + 每只的旗标）
- `out/sentiment_brief.md`：给人看的三五条

用法
----
    python scripts/sentiment.py                 # 扫持仓 + 候选
    python scripts/sentiment.py --codes 000999,601058
    python scripts/sentiment.py --plan out/stage3_plan.json --json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import fuyao

SENT = getattr(config, "SENTIMENT", {}) or {}
JSON_PATH = os.path.join(config.OUT_DIR, "sentiment.json")
BRIEF_PATH = os.path.join(config.OUT_DIR, "sentiment_brief.md")
LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}


# ---------------------------------------------------------------- 纯函数（可测）
from codes import norm_code  # noqa: E402  （唯一实现见 codes.py）


def code_of(item: dict) -> str:
    """从同花顺条目里取 6 位代码（优先 ticker，其次 thscode）。"""
    for k in ("ticker", "thscode", "code"):
        v = (item or {}).get(k)
        if v:
            return norm_code(str(v).split(".")[0])
    return ""


def market_temp(up: list[dict], down: list[dict], brk: list[dict],
                dt_rows: list[dict], hot: list[dict]) -> dict:
    """把几份清单压成一个温度字典（纯计算，方便断言）。"""
    up_n, down_n, brk_n = len(up or []), len(down or []), len(brk or [])
    # 封板率：涨停 / (涨停 + 炸板)，炸板=冲板未封住
    denom = up_n + brk_n
    seal = (up_n / denom) if denom else 0.0
    boards = {}
    for it in up or []:
        cnt = it.get("continue_day_cnt")
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            cnt = 1
        boards[cnt] = boards.get(cnt, 0) + 1
    # 数据存疑标记：涨停明明有一批，跌停/炸板却恰好为 0——同花顺这两个池子偶尔
    # 空返回（2026-10-29 实测：涨停 39 家、跌停 0、炸板 0）。不能悄悄当成「真的 0 家」，
    # 否则「情绪偏热」的结论会建立在缺失数据上。
    suspect = []
    if up_n >= 20 and brk_n == 0:
        suspect.append("炸板池空返回")
    if up_n >= 20 and down_n == 0:
        suspect.append("跌停池空返回")
    return {
        "limit_up": up_n,
        "limit_down": down_n,
        "break": brk_n,
        "seal_ratio": round(seal, 4),
        "max_board": max(boards) if boards else 0,
        "board_dist": {str(k): v for k, v in sorted(boards.items())},
        "dragon_tiger": len(dt_rows or []),
        "suspect": suspect,
        "hot_top3": [{"code": code_of(x), "name": x.get("name") or ""}
                     for x in (hot or [])[:3]],
    }


def _day(v) -> str:
    s = str(v or "").strip()
    if s.isdigit() and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def readiness(temp: dict, trade_date: str = "", today: str | None = None) -> dict:
    """数据是否就绪 + 是否过期（纯函数，方便断言）。

    - ready：三个池子至少有一个非空。全空 ≠「今天 0 家」——详见 market_temp 的 suspect 设计。
    - stale_days：交易日距今的日历天数（周末留 2 天容差；>4 天视为过期，
      说明池子没更新，不能把旧温度当今天的用）。
    """
    up = int(temp.get("limit_up") or 0)
    dn = int(temp.get("limit_down") or 0)
    brk = int(temp.get("break") or 0)
    ready = bool(up or dn or brk)
    td = _day(trade_date)
    stale_days = None
    if td and today:
        try:
            stale_days = (dt.date.fromisoformat(_day(today))
                          - dt.date.fromisoformat(td)).days
        except ValueError:
            stale_days = None
    stale = stale_days is not None and stale_days > 4
    return {"ready": ready, "trade_date": td, "stale_days": stale_days, "stale": stale}


def temp_comment(t: dict) -> str:
    """给温度配一句话（只描述，不预测）。"""
    up_n, dn_n = t.get("limit_up") or 0, t.get("limit_down") or 0
    if up_n == 0 and dn_n == 0:
        return "今天没有涨跌停样本（非交易日或数据未就绪）。"
    if t.get("suspect"):
        # 先给结论再挂存疑尾巴：封板率只由涨停与炸板决定，跌停池空返回不影响它，
        # 但跌停/炸板两个数在空返回时不能当成「真的 0 家」去下结论。
        warn = f"（注意：{t['suspect'][0]}，跌停/炸板按缺失看）"
    else:
        warn = ""
    if dn_n > up_n:
        return f"跌停 {dn_n} 家 > 涨停 {up_n} 家，情绪偏冷。{warn}"
    if (t.get("seal_ratio") or 0) >= 0.8 and up_n >= 30:
        return f"涨停 {up_n} 家、封板率 {t['seal_ratio']:.0%}，赚钱效应偏强。{warn}"
    return (f"涨停 {up_n} 家 / 跌停 {dn_n} 家 / 炸板 {t.get('break') or 0} 家，"
            f"情绪中性。{warn}")


def flags_of(codes: list[str], up: list[dict], down: list[dict], brk: list[dict],
             dt_items: list[dict], hot: list[dict], anomalies: dict) -> list[dict]:
    """给每只关注标的打旗标。返回 [{code,name,level,kind,detail}]，按 level 排序。"""
    want = {norm_code(c) for c in codes}
    out: list[dict] = []

    def hit(rows, kind, level, fmt):
        for it in rows or []:
            c = code_of(it)
            if c in want:
                out.append({"code": c, "name": it.get("name") or "",
                            "level": level, "kind": kind,
                            "detail": fmt(it)})

    hit(down, "跌停", "高", lambda it: f"今日跌停（{it.get('price_change_ratio_pct')}%）")
    hit(brk, "炸板", "中", lambda it: "盘中触板未封住")
    hit(up, "涨停", "低", lambda it: f"今日涨停／{it.get('continue_day_text') or '首板'}")
    hit(dt_items, "龙虎榜", "中",
        lambda it: f"上榜，净额 {float(it.get('net_value') or 0)/1e4:.0f} 万元"
                   f"（占成交 {float(it.get('net_rate') or 0)*100:.1f}%）")
    hit(hot, "热榜", "低", lambda it: f"热股榜第 {it.get('rank')} 名")

    for c, items in (anomalies or {}).items():
        cc = norm_code(c)
        if cc not in want:
            continue
        for it in items or []:
            why = it.get("reason") or it.get("analysis") or it.get("desc") or ""
            if why:
                out.append({"code": cc, "name": "", "level": "中",
                            "kind": "异动", "detail": str(why)[:120]})

    out.sort(key=lambda r: (LEVEL_ORDER.get(r["level"], 9), r["code"]))
    return out


def brief_md(temp: dict, flags: list[dict], stamp: str) -> str:
    lines = [f"# 情绪层简报（{stamp}）", "", "## 市场温度", ""]
    if not temp.get("ready", True):
        lines.append("> ⚠ **数据未就绪**：三个池子都空返回（已重试），本简报不构成市场温度结论。")
        lines.append("")
    if temp.get("stale"):
        lines.append(f"> ⚠ 数据过期：池子交易日 {temp.get('trade_date') or '-'}"
                     f"（距今 {temp.get('stale_days')} 天），不能当今天的温度用。")
        lines.append("")
    lines.append(f"- 涨停 {temp.get('limit_up')} 家｜跌停 {temp.get('limit_down')} 家"
                 f"｜炸板 {temp.get('break')} 家｜封板率 {(temp.get('seal_ratio') or 0):.0%}")
    lines.append(f"- 最高 {temp.get('max_board')} 连板｜龙虎榜 {temp.get('dragon_tiger')} 只")
    top3 = temp.get("hot_top3") or []
    if top3:
        lines.append("- 热股榜前三：" + "、".join(f"{x['name']}" for x in top3))
    lines.append(f"- 解读：{temp_comment(temp)}")
    lines += ["", "## 关注标的旗标", ""]
    if not flags:
        lines.append("- 无异动、无涨跌停、无龙虎榜、无异动原因。")
    for f in flags:
        lines.append(f"- **{f['level']}** `{f['code']}` {f['name']}｜{f['kind']}：{f['detail']}")
    lines += ["", "> 这一层只做风险提示与温度计，不产生买卖信号，不改变仓位。", ""]
    return "\n".join(lines)


def _safe(fn, default):
    """fail-soft：单接口挂了不影响整层。"""
    try:
        return fn() or default
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 情绪层取数失败（{getattr(fn, '__name__', fn)}）：{type(e).__name__}: {e}")
        return default


# ---------------------------------------------------------------- 关注标的
def codes_from_plan(plan_path: str | None = None) -> list[str]:
    p = plan_path or os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        plan = json.load(f)
    out = []
    for pos in plan.get("positions") or []:
        if pos.get("layer") not in (None, "", "个股", "stock"):
            continue
        c = norm_code(pos.get("code"))
        if len(c) == 6:
            out.append(c)
    return out


def codes_from_candidates(limit: int) -> list[str]:
    p = os.path.join(config.OUT_DIR, "stage1_candidates.csv")
    if not os.path.exists(p) or limit <= 0:
        return []
    out = []
    with io.open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = norm_code(r.get("code"))
            if len(c) == 6:
                out.append(c)
            if len(out) >= limit:
                break
    return out


def _fetch_pools() -> tuple[list, list, list]:
    """取三个涨跌停池；任一接口失败按空处理（fail-soft）。"""
    return (_safe(fuyao.limit_up_pool, []),
            _safe(fuyao.limit_down_pool, []),
            _safe(fuyao.limit_break_pool, []))


def run(codes: list[str] | None = None, write: bool = True,
        today: str | None = None) -> dict:
    codes = codes or sorted(set(codes_from_plan() +
                                codes_from_candidates(int(SENT.get("max_codes") or 0))))
    up, down, brk = _fetch_pools()
    # 空返回重试：同花顺三个池子偶尔整批空返回（2026-10-29 实测涨停 39 家、跌停 0、炸板 0）。
    # 全空时重试几次；仍空就显式标「未就绪」，绝不当成「今天 0 家」往下写结论。
    tries = 0
    retry_n = int(SENT.get("retry") or 2)
    while not (up or down or brk) and tries < retry_n:
        tries += 1
        time.sleep(float(SENT.get("retry_wait") or 2.0))
        up, down, brk = _fetch_pools()
    hot = _safe(fuyao.hot_stock_list, [])
    dt_data = _safe(fuyao.dragon_tiger, {})
    dt_items = (dt_data or {}).get("stock_items") or []
    anomalies = _safe(lambda: fuyao.anomaly_stock(codes), {}) if codes else {}

    temp = market_temp(up, down, brk, dt_items, hot)
    temp["trade_date"] = (dt_data or {}).get("trade_date") or ""
    rd = readiness(temp, temp["trade_date"], today or dt.date.today().isoformat())
    temp.update(rd)
    temp["retry_tries"] = tries
    flags = flags_of(codes, up, down, brk, dt_items, hot, anomalies or {})

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = {"generated_at": stamp, "codes": codes, "temp": temp,
           "ready": rd["ready"], "flags": flags,
           "source": "fuyao:limit/dragon-tiger/hot/anomaly",
           "note": "只做风险提示与温度计，不产生买卖信号"}
    if write:
        os.makedirs(config.OUT_DIR, exist_ok=True)
        with io.open(JSON_PATH, "w", encoding="utf-8", newline="") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        with io.open(BRIEF_PATH, "w", encoding="utf-8", newline="") as f:
            f.write(brief_md(temp, flags, stamp))
    return doc


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="情绪层（涨跌停池/龙虎榜/热股榜/异动原因）")
    ap.add_argument("--codes", default=None, help="逗号分隔代码；默认读 plan + candidates")
    ap.add_argument("--plan", default=None, help="指定 plan JSON")
    ap.add_argument("--json", action="store_true", help="打印 JSON")
    ap.add_argument("--no-write", action="store_true", help="不写 out/ 文件")
    a = ap.parse_args()

    if not fuyao.available():
        print("[error] 没有同花顺 API key（环境变量 FUYAO_API_KEY 或 work/fuyao.key）")
        return 2

    codes = None
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.plan:
        codes = codes_from_plan(a.plan)

    doc = run(codes, write=not a.no_write)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0

    t = doc["temp"]
    print(f"[情绪层] {doc['generated_at']}｜交易日 {t.get('trade_date') or '-'}")
    if not t.get("ready", True):
        print(f"  ⚠ 数据未就绪：三个池子全空返回（已重试 {t.get('retry_tries') or 0} 次）")
    if t.get("stale"):
        print(f"  ⚠ 数据过期：池子交易日距今 {t.get('stale_days')} 天")
    print(f"  涨停 {t['limit_up']} / 跌停 {t['limit_down']} / 炸板 {t['break']}"
          f"｜封板率 {t['seal_ratio']:.0%}｜龙虎榜 {t['dragon_tiger']} 只")
    print(f"  解读：{temp_comment(t)}")
    if doc["flags"]:
        print(f"  旗标 {len(doc['flags'])} 条：")
        for f in doc["flags"][:15]:
            print(f"    [{f['level']}] {f['code']} {f['name']} {f['kind']}：{f['detail'][:60]}")
    else:
        print("  关注标的无毒要旗标。")
    if not a.no_write:
        print(f"[ok] 已写 {JSON_PATH} 与 {BRIEF_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
