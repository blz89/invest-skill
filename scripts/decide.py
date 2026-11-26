# -*- coding: utf-8 -*-
"""手工全量快捷方式（无研究层时，**含取数**）：体检 → 按可投资本跑管线 → 出周报与订单 → 记分卡。

注意：本部署**决策日（周三/周六 20:30）不取数**，决策日不走本脚本；它只用于**工作日补跑/调试**或无研究层时的保底。

正式决策日按 SKILL §8.1.1「决策日 10 步」执行：研究层 T2/T3/T4 必须插在第 4–6 步之间、
事件面也只在决策日采集，本脚本不包含这些步骤（它只保留无研究层时的保底一条龙）。

为什么要有这个脚本：本技能是无人值守运行（交易日 19:00 本机统一取数、周三/周六 20:30 决策），原来要手工敲 5 条命令、
还要自己记住「out/ 只能是一个本金生成的」这条口径。这里把它固化成一条命令，
顺序、本金、账本后缀都由脚本保证，减少「跑错顺序 / 用错本金 / 忘了体检」的失误。

用法：
  python scripts/decide.py                 # 完整流程（warm cache 约 2 分钟：5 万全量取数+判断）
  python scripts/decide.py --refresh       # 忽略当天缓存强制重取（工作日补数/调试用；决策日不取数）
  python scripts/decide.py --reuse         # 当天已跑过管线：只出周报（约 10 秒）
  python scripts/decide.py --ledger 50k    # 只跑一个账本
  python scripts/decide.py --init          # 账本不存在时先建账（冷启动/换机器/误删恢复）
  python scripts/decide.py --no-doctor     # 跳过体检（不推荐）

为什么能省时间：screen/etf/wealth/judge 四个阶段的结果**与本金无关**，同一天只需要算一次；
后续账本用 `run.py --only-plan` 只重跑 plan.py（约 2 秒），不再重复取数与打分。

为什么有 --refresh（以及为什么决策日不用它）：数据缓存按「日期」命名，同一天内多次运行会复用当天第一次
抓的数据；工作日白天先跑过一次（拿到的是上一交易日收盘），之后要按最新收盘重取才加 --refresh。
本部署把**取数固定在交易日 19:00 本机任务（`stock prefetch` 统一取数）**，**周三/周六 20:30 决策日不取数**——所以决策日不要跑本脚本，
`--refresh` 只用于工作日补数/调试。默认关闭以保护接口、保证同一天内可复现。

退出码：0 = 全部成功；1 = 体检 NO-GO、某步失败或验收未达标（失败即停，不写坏账本）；2 = 参数/本金不符。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import paper  # noqa: E402

SC = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CFG = config.PAPER
OUT = CFG["package_dir"]


def _env() -> dict:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["TQDM_DISABLE"] = "1"
    if _FORCE_REFRESH:
        e["A_SHARE_FORCE_REFRESH"] = "1"
    return e


_FORCE_REFRESH = False


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def run(cmd: list[str], *, capture: bool = False) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, env=_env(), cwd=SC,
                       capture_output=capture, text=True, encoding="utf-8", errors="replace")
    if capture and r.stdout:
        sys.stdout.write(r.stdout)
    return r.returncode


def investable_of(led_cfg: dict) -> float:
    """可投资本 = 本金 + 已实现盈利（与 weekly.py / paper.investable_capital 同口径）。"""
    path = led_cfg.get("state_file")
    if path and os.path.exists(path):
        try:
            return float(paper.investable_capital(paper.load(path))["investable"])
        except Exception as e:  # noqa: BLE001
            # 不能静默按本金跑：账本读不出时用 50000 出计划会与真实可投资本脱节（审计 #11）
            print(f"  ✗ 读账本失败：{e}", file=sys.stderr)
            raise SystemExit(2)
    return float(led_cfg.get("capital") or 0.0)


def plan_capital() -> float | None:
    """读 out/stage3_plan.json 里的本金（判断当前 out/ 是哪本账的产物）。"""
    path = os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(json.load(f).get("capital") or 0.0)
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="手工全量快捷方式（含取数；无研究层）：体检 + 账本决策 + 记分卡；决策日不跑")
    ap.add_argument("--ledger", default="all", help="默认 all（本部署只有 50k）")
    ap.add_argument("--reuse", action="store_true",
                    help="能复用就复用当天 out/；本金不符时仍会自动重跑该账本管线（仍走缓存）")
    ap.add_argument("--no-doctor", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略当天缓存强制重取（工作日补数/调试用；决策日不取数，约多花 1–2 分钟）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tier", default="")
    ap.add_argument("--override-regime", action="store_true")
    ap.add_argument("--init", action="store_true",
                    help="缺账本先建账（用配置里的本金/档位），再正常决策；账本已存在则不动")
    a = ap.parse_args(argv)

    global _FORCE_REFRESH
    _FORCE_REFRESH = bool(a.refresh)
    if _FORCE_REFRESH:
        print("[decide] --refresh：本次忽略当天缓存强制重取（工作日补数/调试；决策日不取数）")

    leds = CFG.get("ledgers") or {"50k": {"capital": CFG.get("capital"), "tier": "B",
                                         "state_file": CFG["state_file"], "label": "5 万"}}
    # 顺序固定：按本金从小到大（本部署单账本只有 5 万）
    order = sorted(leds, key=lambda n: float((leds[n] or {}).get("capital") or 0.0))
    names = order if a.ledger == "all" else [a.ledger]
    for n in names:
        if n not in leds:
            print(f"[decide] ✗ 没有账本 {n}；可用：{', '.join(order)}")
            return 2

    if not a.no_doctor:
        step("0. 体检（只读）")
        rc = run([PY, os.path.join(SC, "doctor.py"), "--ledger", a.ledger])
        if rc != 0:
            print("\n[decide] ✗ doctor 判 NO-GO：先把上面的 ERROR 修掉再决策（本次不写任何账本）。")
            return 1
        print("[decide] ✓ 体检 GO")

    # 本次调用内是否已生成可复用的阶段一/二产物（供第二个账本走 --only-plan）。
    # 只有「本次真的跑过完整管线」才置 True；复用旧 out/ 不算，避免拿旧数据算新账本。
    stage12_ready = False

    for n in names:
        led = leds[n]
        state_path = led.get("state_file")
        # 冷启动/账本丢失：先建账（只建账不出订单），否则后面的决策会直接失败。
        # 无人值守时这是最可能的「第一条就跑不动」的场景（换机器、清目录、误删）。
        if a.init and state_path and not os.path.exists(state_path):
            step(f"{n}：账本不存在 → 先建账（--init）")
            rc = run([PY, os.path.join(SC, "weekly.py"), "--ledger", n, "--init"])
            if rc != 0:
                print(f"\n[decide] ✗ {n} 建账失败（exit {rc}）。")
                return 1
        inv = investable_of(led)
        mode = led.get("rebalance_mode") or CFG.get("rebalance_mode", "regime_change")
        tier = a.tier or led.get("tier") or "B"
        cur = plan_capital()
        reusable = a.reuse and cur is not None and abs(cur - inv) <= 1.0
        ran_pipeline = False   # 本次是否刚跑过管线（跑完 out/ 已对口，weekly 必须 --reuse）
        step(f"{n}（{led.get('label')}）｜可投资本 {inv:,.0f} 元｜档位 {tier}｜调仓 {mode}")
        if a.reuse and not reusable:
            print(f"  （out/ 是 {('%.0f' % cur) if cur is not None else '空'} 元的计划，与 {inv:,.0f} 元不符 → 重跑管线，仍走缓存）")
        if not reusable:
            # 关键提速：同一天所有账本共用一次「取数 + 筛选 + 判断」。
            # 本次调用里已经跑过完整管线 → 第二个账本只重跑 plan.py（本金/档位不同）。
            # 不用 --reuse 时也适用；可复用的旧 out/ 不当作「本次已取数」，避免拿旧数据算新账本。
            only_plan = bool(stage12_ready)
            if only_plan:
                cmd = [PY, os.path.join(SC, "run.py"), "--only-plan",
                       "--capital", f"{inv:.0f}", "--tier", tier]
                print("  （复用本次已生成的 stage1/stage2：取数与判断与本金无关，只重跑 plan.py，省约 70 秒）")
            else:
                cmd = [PY, os.path.join(SC, "run.py"), "--capital", f"{inv:.0f}", "--tier", tier]
            if a.limit:
                cmd += ["--limit", str(a.limit)]
            if a.override_regime:
                cmd.append("--override-regime")
            rc = run(cmd)
            if rc != 0:
                print(f"\n[decide] ✗ {n} 管线失败（exit {rc}），已停止，账本未改动。")
                return 1
            if _FORCE_REFRESH:
                # 第一个账本已经按最新收盘重取并覆盖了当日缓存；后面的账本直接复用这份新缓存，
                # 不需要再强制刷新一遍（否则每个账本各重取 300 只，白花约 2 分钟）。
                print("[decide] 已取到最新收盘：后续账本复用当日新缓存（不再重复 --refresh）")
                _FORCE_REFRESH = False
            stage12_ready = True
            ran_pipeline = True
        # 管线刚跑完，out/ 就是这本账的目标本金 → weekly.py 直接用 --reuse，绝不能再跑一遍管线
        # （2026-10-26 审计发现：原来只有「复用旧 out/」才传 --reuse，导致每个账本白跑 2 次完整管线）。
        wcmd = [PY, os.path.join(SC, "weekly.py"), "--ledger", n] + (
            ["--reuse"] if (reusable or ran_pipeline) else [])
        if a.limit:
            wcmd += ["--limit", str(a.limit)]
        rc = run(wcmd)
        if rc != 0:
            print(f"\n[decide] ✗ {n} 决策失败（exit {rc}），已停止。")
            return 1

    step("9. 记分卡")
    rc = run([PY, os.path.join(SC, "review.py"), "--ledger", a.ledger, "--strict"])
    if rc != 0:
        print("\n[decide] ! 有账本「未达标」（token 未赚回或净年化没跑过存款）——账本已更新，验收未过。")
        return 1

    step("完成")
    print(f"周报与订单：{OUT}")
    _default = config.PAPER.get("default_ledger", "50k")
    for n in names:
        print(f"  - {n}: weekly_{'' if n == _default else n + '_'}<决策日>.md")
    print("\n下一步：把周报结论按 SKILL §6 汇报；真钱阶段才需要下一交易日 09:35–10:00 下单，"
          "模拟盘无需执行任务（卖出在下次决策按开盘价撮合，买入限价单按触价成交）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
