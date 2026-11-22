# -*- coding: utf-8 -*-
"""低买高卖（仓位网格）：把「长时运行」这个优势变成机械执行的纪律。

为什么必须是代码而不是 LLM：
    网格是**重复执行**的动作，要可回测、可复算、同一数据同一结论。
    判断一次的事情（个股有没有雷）才交给 LLM。这是框架的第一原则。

成本算术（硬约束，不是建议；费率一律取 `config.CAPITAL`，此处只列当前账户口径）：
    当前账户：佣金万 1、**最低 5 元/笔**、ETF 免印花、个股卖出印花 0.05%。
    → ETF 往返：1,000 元 1.00%、2,500 元 0.40%、5,000 元 0.20%（成本率 = 10 元 ÷ 金额）
    → 个股往返：1,000 元 1.05%、2,500 元 0.45%、5,000 元 0.25%
    所以：只在 ETF 上做网格（个股印花吃光价差）、单笔 >= 5,000 元、间距 >= 5%、年换手 <= 2 次。
    （2026-11-07 B 类 #3 收紧：原先 2,500 元/4 次在 5 万本金下仍会把成本率抬到 0.4%，且换手过密。）

状态：`config.GRID["enabled"] = False`、`verified = False`。
    回测没通过之前，网格**不进决策链**。`--verify` 就是把 enabled 从 False 变 True 的唯一合法路径。

用法：
    python grid.py --signal                     # 按最新持仓算买卖信号（只打印，不下单）
    python grid.py --verify                     # 回测验证（联网取 ETF 历史）
    python grid.py --verify --offline FILE.csv  # 用本地 CSV（code,date,close）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config
import capital

G = config.GRID
VERIFY_JSON = os.path.join(config.OUT_DIR, "grid_verify.json")


# ---------------------------------------------------------------- 纯逻辑
def ma(values: list[float], n: int) -> float | None:
    """简单移动平均。样本不足返回 None（不猜）。"""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def deviation(price: float, ma_value: float | None) -> float | None:
    """偏离度 = (价格 − 均线) / 均线。价格或均线缺失返回 None。"""
    if ma_value is None or ma_value <= 0 or price is None:
        return None
    return (float(price) - float(ma_value)) / float(ma_value)


def target_weight(dev: float | None, base_weight: float, params: dict | None = None) -> float:
    """按偏离度分档给目标权重：越跌越买、越涨越卖。

    dev = -2 × band → target = base + 2 × step（跌了加仓）
    dev = +2 × band → target = base − 2 × step（涨了减仓）
    结果夹在 [weight_floor, weight_ceiling]。
    """
    p = params or G
    if dev is None:
        return float(base_weight)
    band = float(p["band_pct"])
    step = float(p["step_pct"])
    if band <= 0:
        return float(base_weight)
    levels = int(abs(dev) / band) * (1 if dev > 0 else -1)
    # 只看整数档，避免线性放大过拟合
    w = float(base_weight) - levels * step
    return max(float(p["weight_floor"]), min(float(p["weight_ceiling"]), w))


def commission_cny(order_cny: float, instrument: str = "etf") -> float:
    """单边佣金。**不再自带费率表**，统一走 `capital.trade_fee`。

    费率只允许有一个出处：`capital.trade_fee` 是唯一实现，不要在这里自带费率表——
    自带会与 `config.CAPITAL` 形成两套，免五后一处生效、另一处偷偷多算成本。
    """
    return capital.trade_fee(abs(float(order_cny)),
                             is_etf=(instrument != "stock"))["commission"]


def round_trip_cost_rate(order_cny: float, instrument: str = "etf") -> float:
    """一次完整往返（买入 + 卖出）的成本率，含卖出印花（若为个股）。"""
    notional = abs(float(order_cny))
    if notional <= 0:
        return 0.0
    is_etf = instrument != "stock"
    buy = capital.trade_fee(notional, is_etf=is_etf, side="buy")["total"]
    sell = capital.trade_fee(notional, is_etf=is_etf, side="sell")["total"]
    return (buy + sell) / notional


def gate_order(order_cny: float, instrument: str = "etf",
               params: dict | None = None) -> tuple[bool, str]:
    """成本门禁：不满足硬约束就拒绝这笔网格交易（fail-closed）。"""
    p = params or G
    n = abs(float(order_cny))
    if p.get("etf_only", True) and instrument != "etf":
        return False, f"网格只做 ETF（个股印花 0.05% 吃光价差），收到 {instrument}"
    if n < float(p["min_order_cny"]):
        return False, (f"单笔 {n:.0f} 元 < 下限 {p['min_order_cny']:.0f} 元："
                       f"往返成本率 {round_trip_cost_rate(n, instrument):.2%}，"
                       f"最低佣金吃掉价差")
    rt = round_trip_cost_rate(n, instrument)
    spacing = float(p["spacing_pct"])
    if spacing < 2 * rt:
        return False, (f"网格间距 {spacing:.2%} < 往返成本 {rt:.2%} 的 2 倍，"
                       f"价差不够覆盖成本")
    return True, f"通过：单笔 {n:.0f} 元、往返成本 {rt:.2%}、间距 {spacing:.2%}"


def signals(rows: list[dict], params: dict | None = None) -> list[dict]:
    """按当前持仓给网格信号。rows: [{code, price, ma200, current_weight, weight_value}]"""
    p = params or G
    out = []
    for r in rows:
        dev = deviation(r.get("price"), r.get("ma200"))
        if dev is None:
            out.append({"code": r.get("code"), "action": "skip", "reason": "缺价格或均线"})
            continue
        base = float(r.get("current_weight") or r.get("base_weight") or p["weight_ceiling"])
        tgt = target_weight(dev, base, p)
        cur = float(r.get("current_weight") or base)
        delta = tgt - cur
        notional = abs(delta) * float(r.get("weight_value") or 0.0)
        if abs(delta) < 1e-9:
            action = "hold"
            reason = f"偏离 {dev:+.2%} 未越档"
        elif delta > 0:
            action = "buy"
            reason = f"偏离 {dev:+.2%} → 目标权重 {tgt:.0%}（+{delta:.0%}）"
        else:
            action = "sell"
            reason = f"偏离 {dev:+.2%} → 目标权重 {tgt:.0%}（{delta:.0%}）"
        ok, gate_reason = gate_order(notional, "etf", p)
        out.append({
            "code": r.get("code"), "action": action if ok or action == "hold" else "skip",
            "deviation": dev, "target_weight": tgt, "current_weight": cur,
            "delta_weight": delta, "order_cny": notional,
            "gate_ok": ok, "reason": reason if ok or action == "hold" else gate_reason,
        })
    return out


# ---------------------------------------------------------------- 回测验证
def _mdd(curve: list[float]) -> float:
    peak, worst = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def backtest(closes: list[float], params: dict | None = None,
             budget: float = 17500.0, start: int = 200) -> dict | None:
    """单标的网格 vs 两个对照组。budget = 权益 sleeve 预算（默认 5 万 × 35%）。

    三个策略（同一预算、同一区间、同一成本口径）：
      A) 网格    ：起点按 base_weight 建仓，之后按偏离度在 floor~ceiling 之间调；
      B) 固定持有：起点按 base_weight 建仓，之后**不交易**（同平均仓位对照）；
      C) 满仓持有：起点全额买入（100% 仓位，风险基准，不是公平对照）。
    网格有没有价值，看的是 A > B 多少，而不是 A > C —— A 天然仓位低，
    拿 A 比 C 只会得出「仓位低所以赚得少」这种废话。

    假设（写出来，避免被当成精确结论）：
      - 只在收盘成交，信号当天执行（无未来函数，也拿不到更好的价）；
      - 现金收益记 0（三个策略同口径，不影响比较）；
      - ETF 免印花，佣金按 `config.CAPITAL`（万 1、最低 5 元），买卖都计；
      - 从第 start 天开始才有 200 日均线；建仓按 start 日收盘价。
    """
    p = params or G
    n = len(closes)
    if n <= start + 20:
        return None
    base_w = float(p.get("base_weight") or 0.5)
    min_order = float(p["min_order_cny"])

    px0 = closes[start]
    span = closes[start:]

    # --- C) 满仓买入持有（风险基准）
    bh_units = (budget - commission_cny(budget)) / px0
    bh_curve = [bh_units * c for c in span]
    bh_ret = (bh_units * closes[-1] - budget) / budget
    bh_mdd = _mdd(bh_curve)

    # --- B) 固定 base 权重持有（公平对照：同平均仓位、零交易）
    fx_units = 0.0
    fx_cash = budget
    if base_w > 0:
        spend = budget * base_w
        fee = commission_cny(spend)
        fx_units = (spend - fee) / px0
        fx_cash = budget - spend
    fx_curve = [fx_cash + fx_units * c for c in span]
    fx_ret = (fx_cash + fx_units * closes[-1] - budget) / budget
    fx_mdd = _mdd(fx_curve)

    # --- A) 网格：起点按 base 建仓，之后按偏离度调
    cash, units, trades, fees = budget, 0.0, 0, 0.0
    if base_w > 0:
        spend = budget * base_w
        fee = commission_cny(spend)
        units = (spend - fee) / px0
        cash = budget - spend - fee
        fees += fee
    curve = []
    for i in range(start, n):
        px = closes[i]
        w = ma(closes[:i + 1], 200)
        equity = cash + units * px
        curve.append(equity)
        if w is None:
            continue
        dev = deviation(px, w)
        tw = target_weight(dev, base_w, p)
        target_value = equity * tw
        cur_value = units * px
        delta = target_value - cur_value
        if abs(delta) < min_order:
            continue
        fee = commission_cny(delta)
        if delta > 0:                      # 买
            spend = min(delta, cash - fee)
            if spend < min_order:
                continue
            units += spend / px
            cash -= spend + fee
        else:                              # 卖
            sell_units = min(units, -delta / px)
            if sell_units * px < min_order:
                continue
            units -= sell_units
            cash += sell_units * px - fee
        trades += 1
        fees += fee

    grid_ret = (cash + units * closes[-1] - budget) / budget
    years = (n - start) / 250.0
    return {
        "n_days": n - start,
        "years": years,
        "budget": budget,
        "grid_return": grid_ret,
        "fixed_return": fx_ret,          # 公平对照
        "bh_return": bh_ret,             # 满仓基准
        "delta_pp": (grid_ret - fx_ret) * 100,   # 网格 vs 固定持有（核心指标）
        "bh_delta_pp": (grid_ret - bh_ret) * 100,
        "grid_mdd": _mdd(curve),
        "fixed_mdd": fx_mdd,
        "bh_mdd": bh_mdd,
        "trades": trades,
        "fees_cny": fees + commission_cny(budget) + (commission_cny(budget * base_w) if base_w else 0.0),
        "trades_per_year": trades / max(years, 1e-9),
    }


def verify(codes: list[str] | None = None, params: dict | None = None,
           offline_csv: str | None = None) -> dict:
    """跑回测并给出「网格是否值得启用」的结论。不联网失败时返回 ok=False，不抛。"""
    p = params or G
    codes = codes or list(p["verify_sample"])
    series: dict[str, list[float]] = {}

    if offline_csv:
        try:
            import csv
            buf: dict[str, list[tuple[str, float]]] = {}
            with open(offline_csv, "r", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    c = row["code"].strip()
                    d = row.get("date") or row.get("dt") or ""
                    buf.setdefault(c, []).append((d, float(row["close"])))
            for c, rows in buf.items():
                rows.sort(key=lambda x: x[0])
                series[c] = [x[1] for x in rows]
        except (OSError, KeyError, ValueError) as e:
            return {"ok": False, "reason": f"本地 CSV 解析失败：{e}"}
    else:
        try:
            import datasource as ds
            for c in codes:
                df = ds.fetch_etf_hist(c)
                if df is None or df.empty or "close" not in df.columns:
                    continue
                s = [float(x) for x in df["close"].tolist() if x == x]
                if len(s) > 260:
                    series[c] = s
        except Exception as e:  # noqa: BLE001 - 网络/依赖问题一律降级，不阻断
            return {"ok": False, "reason": f"取数失败（可离线跑 --offline）：{type(e).__name__}: {e}"}

    if not series:
        return {"ok": False, "reason": "没有可用的 ETF 历史序列"}

    rows = []
    for c, s in series.items():
        r = backtest(s, p)
        if r:
            r["code"] = c
            rows.append(r)

    if not rows:
        return {"ok": False, "reason": "样本太短，无法回测"}

    avg_delta = sum(r["delta_pp"] for r in rows) / len(rows)
    max_trades = max(r["trades_per_year"] for r in rows)
    worst_mdd_gap = min(r["grid_mdd"] - r["fixed_mdd"] for r in rows)

    # 通过条件（写死，不许事后调参凑数）：
    #   ① 网格平均战胜「同仓位的固定持有」> 0.5pp/年 —— 这才是低买高卖的超额
    #   ② 年换手 <= 上限（一次往返 = 2 笔）
    #   ③ 回撤不比固定持有差超过 3pp（网格不该靠冒更多风险换收益）
    cond = {
        "delta_ok": avg_delta > 0.5,
        "turnover_ok": max_trades <= float(p["max_round_trips_per_year"]) * 2,
        "mdd_ok": worst_mdd_gap >= -0.03,
    }
    ok = all(cond.values())
    result = {
        "ok": ok,
        "conditions": cond,
        "baseline": "固定持有（同 base_weight 仓位、零交易）",
        "avg_delta_pp": avg_delta,
        "max_trades_per_year": max_trades,
        "worst_mdd_gap": worst_mdd_gap,
        "rows": rows,
        "params": {k: p[k] for k in ("base_weight", "band_pct", "step_pct",
                                     "min_order_cny", "spacing_pct",
                                     "weight_floor", "weight_ceiling")},
        "verdict": ("通过：网格可启用（把 config.GRID.enabled / verified 置 True，"
                    "并在 git 提交里登记）" if ok else
                    "未通过：网格继续 disabled，不要进决策链"),
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="低买高卖网格（信号 + 回测验证）")
    ap.add_argument("--signal", action="store_true", help="按 out/stage3_plan.json 持仓算信号")
    ap.add_argument("--verify", action="store_true", help="回测验证")
    ap.add_argument("--offline", default=None, help="本地 CSV（code,date,close）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.verify:
        res = verify(offline_csv=a.offline)
        os.makedirs(config.OUT_DIR, exist_ok=True)
        try:
            with open(VERIFY_JSON, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError:
            pass
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"网格回测：{'通过' if res.get('ok') else '未通过'} — {res.get('verdict') or res.get('reason')}")
            for r in res.get("rows", []):
                print(f"  {r['code']}: 网格 {r['grid_return']:+.2%} vs 固定持有 {r['fixed_return']:+.2%} "
                      f"({r['delta_pp']:+.2f}pp) | 满仓 {r['bh_return']:+.2%} | "
                      f"MDD 网格 {r['grid_mdd']:.2%} / 固定 {r['fixed_mdd']:.2%} / 满仓 {r['bh_mdd']:.2%} | "
                      f"{r['trades']} 笔（{r['trades_per_year']:.1f}/年）| 费用 {r['fees_cny']:.1f} 元")
            if res.get("conditions"):
                print(f"  条件：{res['conditions']} 平均超额 {res.get('avg_delta_pp', 0):+.2f}pp "
                      f"（基准：{res.get('baseline')}）")
            print(f"  报告：{VERIFY_JSON}")
        return 0

    if a.signal:
        plan_path = os.path.join(config.OUT_DIR, "stage3_plan.json")
        rows = []
        if os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                cap = float(plan.get("capital") or 50000.0)
                for p in (plan.get("positions") or []):
                    if str(p.get("venue") or "").upper() == "OTC":
                        continue
                    w = float(p.get("weight") or 0.0)
                    rows.append({"code": p.get("code"), "price": p.get("price"),
                                 "ma200": p.get("ma200"),
                                 "current_weight": w, "weight_value": cap * w})
            except (OSError, json.JSONDecodeError, ValueError):
                rows = []

        # 网格只做 ETF：6 位纯数字 = A 股个股（印花 0.05% 吃光价差），先剔除并写明原因
        import re as _re
        etf_rows, skipped = [], []
        for r in rows:
            if _re.fullmatch(r"\d{6}", str(r.get("code") or "")):
                skipped.append({"code": r.get("code"), "action": "skip",
                                "reason": "网格只做 ETF（个股卖出印花 0.05%）"})
            else:
                etf_rows.append(r)
        rows = etf_rows

        # 计划里没有价格/均线时，本地补算（不然信号永远是「缺价格或均线」）
        for r in rows:
            if r.get("ma200") is not None and r.get("price") is not None:
                continue
            code = str(r.get("code") or "")
            if not code:
                continue
            try:
                import datasource as ds
                df = ds.fetch_etf_hist(code)
                if df is None or df.empty or "close" not in df.columns:
                    continue
                s = [float(x) for x in df["close"].tolist() if x == x]
                if s:
                    r["price"] = s[-1]
                    r["ma200"] = ma(s, 200)
            except Exception:  # noqa: BLE001 - 取数失败就保持缺失，信号自动 skip
                continue

        if not rows:
            for s in skipped:
                print(f"  {s['code']}: {s['action']:<5} {s['reason']}")
            print("没有可做网格的 ETF 持仓（网格只作用于场内 ETF）。")
            return 0
        out = signals(rows) + skipped
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for s in out:
                print(f"  {s['code']}: {s['action']:<5} {s.get('reason')}")
            print("\n注意：这里只算信号，不下单。网格当前 "
                  f"{'已启用' if G.get('enabled') else '未启用'}（config.GRID）。")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
