# -*- coding: utf-8 -*-
"""一键跑完三个阶段：筛选 → 精确判断 → 执行计划（本部署本金 5 万）。

用法：
    python run.py --capital 50000      # 5 万本金（默认；自动做整手化 + ETF 承接 + 现金层）
    python run.py --limit 30           # 只筛前 30 只（调试用）
    python run.py --skip-screen --capital 50000   # 复用上次筛选，直接从判断开始（本金仍需在兜底区间内）

本金必须在 config.CAPITAL["min"]–["max"] 兜底区间内，越界直接报错退出；
本部署口径是 5 万。原始参数与阈值仍全部在 config.py。

每一步的输出都落在 out/ 目录，可以单独重跑而不用从头开始（有缓存）。

本部署分工：**基础数据由交易日 19:00 的 Windows 任务 `python -m stock prefetch` 统一取满**（0 token，两技能共用热缓存）；本脚本默认只读热缓存跑三阶段（`run.py --capital 50000`，**不带 --refresh**）；**周三/周六 20:30 决策日不取数**——`--refresh` 只用于工作日手工补数/调试。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import capital as cap  # noqa: E402
import config  # noqa: E402

OUT = config.OUT_DIR


def run(script: str, *args: str, retries: int = 1, strict: bool = True) -> None:
    cmd = [sys.executable, os.path.join(ROOT, script), *args]
    print(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}", flush=True)
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", TQDM_DISABLE="1")
    # 不用 capture：让子进程直接继承 stdio，避免管道缓冲把进度条卡住
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    if rc != 0:
        if retries > 0:
            # py_mini_racer(V8) 偶发原生崩溃（退出码 2147483651），不是数据问题；
            # 已取到的数据都已落缓存，原样重试代价很小。
            print(f"⚠️ {script} 退出码 {rc}，疑似 V8 原生崩溃，重试一次（已取的数据在缓存里）",
                  file=sys.stderr, flush=True)
            return run(script, *args, retries=retries - 1, strict=strict)
        if not strict:
            # 场外债券基金池是 R2 层的**第二条腿**（可选）：取数失败不该让整期计划出不来。
            # plan.py 会在计划文本与 JSON 里写明「场外池缺失」，不会假装配了场外。
            print(f"⚠️ {script} 退出码 {rc}（非阻断阶段，已跳过）→ 计划里会写明「场外池缺失」",
                  file=sys.stderr, flush=True)
            return
        raise SystemExit(f"✗ {script} 退出码 {rc}，重试后仍失败，流程中止（不会用旧结果冒充新结果）")
    print(f"✓ {script} 完成，用时 {time.time() - t0:.1f}s", flush=True)


def apply_refresh(args) -> None:
    """--refresh：忽略当天缓存强制重取（与 decide.py --refresh 同语义）。

    datasource._force_refresh() 是在**每次取数时**读环境变量的，所以只要在启动各阶段
    子进程之前把 A_SHARE_FORCE_REFRESH 写进 os.environ，子进程继承后整条管线都会重取。
    """
    if getattr(args, "refresh", False):
        os.environ["A_SHARE_FORCE_REFRESH"] = "1"


def main():
    ap = argparse.ArgumentParser(description="三阶段投资策略一键运行（本部署本金 5 万）")
    ap.add_argument("--limit", type=int, default=None, help="阶段一只筛前 N 只")
    ap.add_argument("--capital", type=float, default=0.0,
                    help=f"可用本金，必须在 {config.CAPITAL['min']:,.0f}–"
                         f"{config.CAPITAL['max']:,.0f} 元之间")
    ap.add_argument("--tier", default=config.CAPITAL["default_tier"],
                    choices=sorted(config.CAPITAL["tiers"]), help="小资金档位 A/B/C")
    ap.add_argument("--override-regime", action="store_true",
                    help="允许档位突破市场状态仓位上限（规则外做法，计划里会标红）")
    ap.add_argument("--judge-limit", type=int, default=None, help="阶段二只判断前 N 只")
    ap.add_argument("--skip-screen", action="store_true", help="复用上次筛选结果，直接从判断开始")
    ap.add_argument("--only-plan", action="store_true",
                    help="只重跑 plan.py：复用已有的 stage1/stage2/ETF/理财池产物（同一天两个本金共用一次取数+判断，省约 70 秒）")
    ap.add_argument("--no-etf", action="store_true", help="不跑 ETF 层（个股一手买不起时就没有替代）")
    ap.add_argument("--no-wealth", action="store_true",
                    help="不跑 R2 理财层（场内债券 ETF 池）")
    ap.add_argument("--no-otc", action="store_true",
                    help="不跑场外债券基金池（stage1_otc.csv）：R2 层只剩场内 ETF 一条腿")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略当天缓存强制重取（仅工作日手工补数/调试用；正常取数=19:00 `stock prefetch`；= 给子进程 A_SHARE_FORCE_REFRESH=1。决策日不取数）")
    args = ap.parse_args()
    apply_refresh(args)

    try:
        if not args.capital:  # A6：不传 = 按账本累计投入自动
            try:
                import capital_dyn as _cd_auto
                args.capital = _cd_auto.live_capital()
            except Exception:  # noqa: BLE001
                args.capital = 50000.0
        capital = cap.validate_capital(args.capital)
    except cap.CapitalRangeError as e:
        print(f"✗ {e}", file=sys.stderr)
        raise SystemExit(2)

    if args.only_plan:
        # stage1/stage2/ETF/理财池都与本金无关：同一天多账本时只取数判断一次。
        # 这里只做存在性校验，避免「悄悄拿旧数据算新账本」：缺产物就直接报错，让人先跑全量。
        need = ["stage1_funnel.json", "stage1_candidates.csv", "stage2_judged.csv"]
        if not args.no_etf:
            need.append("stage1_etf.csv")
        if not args.no_wealth:
            need.append("stage1_wealth.csv")
        missing = [f for f in need if not os.path.exists(os.path.join(OUT, f))]
        if missing:
            print(f"✗ --only-plan 需要已有的阶段一/二产物，缺少：{', '.join(missing)}", file=sys.stderr)
            print("  先不要加 --only-plan（或先跑一次日全量管线），再复用。", file=sys.stderr)
            raise SystemExit(2)

    t0 = time.time()
    if not args.skip_screen and not args.only_plan:
        a = ["--limit", str(args.limit)] if args.limit else []
        run("screen.py", *a)
        if not args.no_etf:
            run("etf.py")
        if not args.no_wealth:
            run("wealth.py")
        if not args.no_wealth and not args.no_otc:
            # 场外债券基金池：R2 层买不起场内一手时的兜底腿。非阻断阶段——
            # 取数失败只让计划写明「场外池缺失」，不能让整期计划出不来。
            run("otc.py", strict=False)

    if not args.only_plan:
        a = ["--limit", str(args.judge_limit)] if args.judge_limit else []
        run("judge.py", *a)
    # L1 交接契约：研究员 T2 读 out/data_snapshot.csv。纯本地拼装、不取数，
    # 所以 --only-plan 复用产物时也要刷新（否则 T2 拿到的是上一版快照）。
    run("snapshot.py", "--quiet", strict=False)
    plan_args = ["--capital", str(capital), "--tier", args.tier]
    if args.override_regime:
        plan_args.append("--override-regime")
    if args.no_etf:
        plan_args.append("--no-etf")
    if args.no_wealth:
        plan_args.append("--no-wealth")
    if args.no_otc:
        plan_args.append("--no-otc")
    run("plan.py", *plan_args)

    print(f"\n{'=' * 70}")
    print(f"全部完成，总用时 {time.time() - t0:.1f}s｜本金 {capital:,.0f} 元｜档位 {args.tier}")
    print("输出目录：" + OUT)
    for f in ("stage1_funnel.json", "stage1_candidates.csv", "data_snapshot.csv",
              "stage1_etf.csv", "stage1_wealth.csv", "stage1_otc.csv",
              "stage2_judged.csv", "stage2_report.md", "stage3_plan.md",
              "stage3_orders.csv"):
        p = os.path.join(OUT, f)
        print(f"  {'✓' if os.path.exists(p) else '✗'} {f}")


if __name__ == "__main__":
    main()
