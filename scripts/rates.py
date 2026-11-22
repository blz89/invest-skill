# -*- coding: utf-8 -*-
"""现金层利率的「每日实测层」。

为什么要有它：技能里的 1.25% / 1.09% 是**兜底常数**（现值见 config.CAPITAL["cash_layer"]），
只在拿不到实测值时用。
每个工作日巡检（DSH 20:00 会话跑 patrol.py，内调用本模块 refresh）会实测一次，写进
WORK_DIR/cash_rates_live.json；config.cash_rates() 优先读当日实测值（超过 live_max_age_days
个交易日就回落到常数）。

刷新节奏：
- 货基：每个工作日实测（7 日年化中位数会缓慢变化，且账本每日计息要用）；
- 逆回购：**每周五实测一次**（改为每周五）。它取的是近 250 日 / 近 60 日均值，日更没有意义；
  非周五的巡检传 `include_repo=False`，沿用上次的平滑值（`repo_as_of` 单独记录）。
  逆回购的**脉冲报价**是另一回事，不走这里：见 repo_pulse.py（只在窗口日看实时报价）。

取数规则（保守、可审计，不用「拍脑袋」）：
- 逆回购：GC001 的近 250 日均值 与 近 60 日均值 取**较小者**，向下取整到 0.05% 台阶。
  理由：单日值噪声极大（最新 1.495%，近 20 日 1.247%），用短长两个窗口的较小者，
  既不吃到单日尖峰，也不会在利率台阶下行时用高估的长均值。
- 货基：全市场货基 7 日年化**中位数**，四舍五入到 0.01%。

两个数都只是「当前时点假设」，不是收益承诺。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

LIVE_FILE = os.path.join(config.WORK_DIR, "cash_rates_live.json")
HIST_FILE = os.path.join(config.WORK_DIR, "cash_rates_history.jsonl")
STEP = 0.0005          # 逆回购利率台阶：0.05%


def _floor(x: float, step: float = STEP) -> float:
    return math.floor(x / step + 1e-9) * step


def compute(repo: dict | None, mf_median_pct: float | None,
            repo7: dict | None = None) -> dict:
    """纯函数：由取数结果算保守利率（不联网，可离线自测）。

    repo=1 天期（GC001）；repo7=7 天期（R-007，后为观望资金容器）。
    """
    out = {"repo": None, "repo7": None, "money_fund": None, "as_of": None,
           "raw": {"repo": repo or {}, "repo7": repo7 or {},
                   "money_fund": {"median_pct": mf_median_pct}}}
    days = []
    if repo:
        cands = [v for v in (repo.get("rate_pct"), repo.get("mean_60_pct")) if v]
        if cands:
            out["repo"] = round(_floor(min(cands) / 100.0), 6)
        end = repo.get("sample_end") or ""
        if end:
            days.append(str(end)[:10])
    if repo7:
        cands7 = [v for v in (repo7.get("rate_pct"), repo7.get("mean_60_pct")) if v]
        if cands7:
            out["repo7"] = round(_floor(min(cands7) / 100.0), 6)
        end7 = repo7.get("sample_end") or ""
        if end7:
            days.append(str(end7)[:10])
    if mf_median_pct:
        out["money_fund"] = round(float(mf_median_pct) / 100.0, 4)
    md = (out["raw"]["money_fund"] or {}).get("date")
    if md:
        days.append(str(md)[:10])
    out["as_of"] = max(days) if days else None
    return out


def _money_fund_stats(force: bool = False):
    """取全市场货基 7 日年化中位数，返回 (median_pct, date, n)；空表/失败 → (None, None, 0)。

    `force=True` 时临时打开 A_SHARE_FORCE_REFRESH（忽略当天缓存重新取数），
    用于「首次实测返回 0 只」的一次性重试（2026-11-14 A 类兜底，见 patrol_findings.md）。
    """
    import datasource as ds
    _old = os.environ.get("A_SHARE_FORCE_REFRESH")
    if force:
        os.environ["A_SHARE_FORCE_REFRESH"] = "1"
    try:
        df = ds.fetch_money_fund_rates()
    finally:
        if force:
            if _old is None:
                os.environ.pop("A_SHARE_FORCE_REFRESH", None)
            else:
                os.environ["A_SHARE_FORCE_REFRESH"] = _old
    col = "y7_pct" if "y7_pct" in df.columns else None
    if not col:
        return None, None, 0
    s = df[col].dropna()
    if not len(s):
        return None, None, 0
    vals = sorted(float(x) for x in s)
    n = len(vals)
    med = (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0)
    date = str(df["date"].max())[:10] if "date" in df.columns and len(df["date"]) else None
    return med, date, n


def _fetch(include_repo: bool = True, prev: dict | None = None):
    """取实测值。任一项失败返回 None（由调用方决定回落还是告警）。

    `include_repo=False` 时沿用上次实测文件里的逆回购/7 天期结果：账本用的逆回购利率是
    「近 250 日 / 近 60 日均值取小」，一天动不了几个 bp，没必须每日联网重取
    （巡检不再每日看逆回购，改为每周五刷一次）。
    """
    import datasource as ds
    prev = prev or {}
    _prev_raw = prev.get("raw") or {}
    repo = None
    if include_repo:
        try:
            repo = ds.fetch_repo_rate()
        except Exception:  # noqa: BLE001 - 取数失败不抛给上层，交给 fail-soft 逻辑
            repo = None
        if repo is None:            # 本周五取数失败：沿用上次的平滑值，别把账本打回兜底常数
            repo = _prev_raw.get("repo") or None
    else:
        repo = _prev_raw.get("repo") or None
    mf_med, mf_date, mf_n = None, None, 0
    for _attempt in (0, 1):
        try:
            mf_med, mf_date, mf_n = _money_fund_stats(force=_attempt > 0)
        except Exception:  # noqa: BLE001
            mf_med, mf_date, mf_n = None, None, 0
        if mf_med is not None:
            break
        if _attempt == 0:
            # 接口抖动/空表只在当次出现：强制刷新重试一次，避免污染当天 daily_log
            print("[rates] 货基实测为空 → 强制刷新重试一次", file=sys.stderr)
    repo7 = None
    if include_repo:
        try:
            repo7 = ds.fetch_repo_rate_7d()
        except Exception:  # noqa: BLE001 - 7 天期取数失败不阻塞 1 天期/货基
            repo7 = None
        if repo7 is None:
            repo7 = _prev_raw.get("repo7") or None
    else:
        repo7 = _prev_raw.get("repo7") or None
    res = compute(repo, mf_med, repo7)
    res["raw"]["money_fund"]["n"] = mf_n
    res["raw"]["money_fund"]["date"] = mf_date
    if res["repo"] and res["money_fund"]:
        parts = [f"逆回购=GC001 近250日均值{repo.get('rate_pct')}%、近60日"
                 f"{repo.get('mean_60_pct')}%，取较小者下取整到0.05%台阶={res['repo'] * 100:.2f}%；"
                 f"货基=全市场{mf_n}只7日年化中位数{mf_med:.4f}%，取{res['money_fund'] * 100:.2f}%；"]
        if repo7:                       # 7 天期可能取数失败/沿用不到旧值
            parts.append(f"7天期=R-007 近250日均值{repo7.get('rate_pct')}%、近60日"
                         f"{repo7.get('mean_60_pct')}%，取较小者下取整到0.05%台阶="
                         f"{(res.get('repo7') or 0) * 100:.2f}%；")
        parts.append(f"as_of {res['as_of']}")
        res["source"] = "".join(parts)
    return res


def _append_history(got: dict, now: str) -> None:
    """追加当日利率快照（同一天只留最新一条），供逐日计息复算使用。

    为什么必须留历史：朝朝宝按「每日实测年化」逐日计息、货基按逐日万份收益，
    计息需要知道每一天当时的利率，不能拿今天的利率乘整段天数。
    """
    day = str(got.get("as_of") or "")[:10]
    if not day:
        return
    try:
        r1 = (config.cash_rates() or {}).get("r1")
    except Exception:  # noqa: BLE001
        r1 = None
    rec = {"date": day, "r1": r1, "money_fund": got.get("money_fund"),
           "repo": got.get("repo"), "repo7": got.get("repo7"), "generated_at": now}
    rows = []
    try:
        with io.open(HIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and str(r.get("date")) != day:
                    rows.append(r)
    except OSError:
        rows = []
    rows.append(rec)
    try:
        os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
        with io.open(HIST_FILE, "w", encoding="utf-8") as f:
            for r in rows[-400:]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[rates] 利率历史写盘失败（不影响本次结论）：{e}", file=sys.stderr)


def refresh(write: bool = True, include_repo: bool = True) -> dict:
    """实测并（可选）写盘。

    `include_repo=False`：只刷货基，逆回购/7 天期沿用上次文件的平滑值——巡检每天跑，
    逆回购改每周五刷一次。
    """
    prev = load() or {}
    prev_raw = prev.get("raw") or {}
    got = _fetch(include_repo=include_repo, prev=prev)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    got["generated_at"] = now
    # 逆回购的「数据日」单独记：货基日期每天变，但逆回购可能是上周五的平滑值，
    # 不能被 max() 掩盖（cash_rates 的 as_of 取三者最大，只用于新鲜度判断）。
    raw = got.get("raw") or {}
    fresh = include_repo and raw.get("repo") is not None and raw.get("repo") is not prev_raw.get("repo")
    if fresh:
        got["repo_as_of"] = str((raw.get("repo") or {}).get("sample_end") or "")[:10] or None
        got["repo_refreshed_at"] = now
    else:
        got["repo_as_of"] = (prev.get("repo_as_of")
                             or str((raw.get("repo") or {}).get("sample_end") or "")[:10] or None)
        got["repo_refreshed_at"] = prev.get("repo_refreshed_at")
    if write:
        try:
            os.makedirs(os.path.dirname(LIVE_FILE), exist_ok=True)
            with io.open(LIVE_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps(got, ensure_ascii=False, indent=1))
            _append_history(got, now)
        except OSError as e:
            print(f"[rates] 写盘失败（不影响本次结论）：{e}", file=sys.stderr)
    return got


def load() -> dict | None:
    try:
        with io.open(LIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="现金层利率实测（写 cash_rates_live.json）")
    ap.add_argument("--show", action="store_true", help="只读当前实测文件，不联网")
    ap.add_argument("--json", action="store_true", help="打印 JSON")
    a = ap.parse_args()
    got = load() if a.show else refresh(write=True)
    if a.json or not got:
        print(json.dumps(got, ensure_ascii=False, indent=1))
        return 0 if got else 1
    c = config.cash_rates()
    print(f"[rates] 实测 repo={got.get('repo')} repo7={got.get('repo7')} "
          f"mf={got.get('money_fund')} as_of={got.get('as_of')}")
    print(f"[rates] 生效 repo={c['repo']:.4f} mf={c['money_fund']:.4f} "
          f"live={c.get('live')} as_of={c['as_of']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
