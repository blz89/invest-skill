# -*- coding: utf-8 -*-
"""一次跑完技能的全部离线自检套件，汇总成一张表（不联网的部分优先，末尾跑冒烟）。

用法：
    python selftest_all.py              # 全跑（约 2–3 分钟）
    python selftest_all.py --fast       # 跳过会取数的套件（fuyao/news/xcheck 相关）
    python selftest_all.py --only plan  # 只跑文件名含 plan 的套件

退出码：全部通过 → 0；有任何一套失败 → 1（fail-closed，方便挂到 CI / 定时任务前）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

# 控制台可能是 GBK（Windows 默认），本脚本会打印 ↳ / ✗ 这类符号：
# 遇到编码不了就替换成 ?，不要因为「打印失败」把自检结果变成崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))

# 顺序：纯离线 → 依赖本地产物 → 会取数（--fast 时跳过 NETWORK 里的）
SUITES = [
    ("_selftest.py", "小资金算术/手数/费用/档位"),
    ("_selftest_screen.py", "筛选：风险名单剔除/停牌门槛"),
    ("_selftest_regime_probe.py", "数据：熊市确认探针"),
    ("_selftest_plan.py", "排名篮子/上限/回滚/新闻闸"),
    ("_selftest_etf.py", "ETF 代码前缀/连续失败护栏（2026-11-16 bj 前缀事故）"),
    ("_selftest_paper.py", "撮合/止损/结算/现金层/回滚"),
    ("_selftest_risk.py", "R1–R5 定级与组合评审"),
    ("_selftest_review.py", "记分卡风险组合：场外逐只计入/单只上限回归"),
    ("_selftest_otc.py", "场外基金池漏斗/份额去重"),
    ("_selftest_team.py", "评分奖励/计费预算/网格/编排"),
    ("_selftest_patrol.py", "巡检触发条件"),
    ("_selftest_doctor.py", "体检逻辑"),
    ("_selftest_preflight.py", "决策日第 3 步预检（数据日期/本金/成功率/日摘要）"),
    ("_selftest_snapshot.py", "L1 交接契约快照"),
    ("_selftest_layers.py", "行业/情绪/宏观三层"),
    ("_selftest_fund.py", "基金复算与交叉校验"),
    ("_selftest_pit.py", "PIT 时点正确性"),
    ("_selftest_cninfo.py", "巨潮公告扫描"),
    ("_selftest_housekeeping.py", "daily_log 记账/跨年归档/保留策略/安全阀"),
    ("_selftest_otc_book.py", "场外影子账/费用/复利计入"),
    ("_selftest_monthly.py", "月报策略复盘/技能优化/A 类清单"),
    ("_selftest_score_panel.py", "选股评分面板归档/前向超额/年报体检"),
    ("_selftest_navstats.py", "日度净值表现（收益/回撤/胜率）"),
    ("_selftest_weekly.py", "周决策本金匹配闸门（防串账/防小差额误中止）"),
    ("_selftest_billing.py", "计费快照差额/缺快照回落"),
    ("_selftest_breakdown.py", "收益拆解（权益/场外/现金）与周报/月报接线"),
    ("_selftest_cash_sleeve.py", "半流动性货基池切片/逆回购脉冲择时"),
]
NETWORK = [
    ("_selftest_fuyao.py", "同花顺数据层（要联网）"),
    ("_selftest_news.py", "事件面评分（要联网）"),
    ("_smoke.py", "数据层冒烟：茅台逐项核对（要联网）"),
]

PATS = [
    r"通过\s*(\d+)\s*项",          # 通过 89 项，失败 0 项
    r"通过\s*(\d+)\s*/\s*\d+\s*项",  # 通过 106 / 106 项
    r"(\d+)\s*项断言",             # OK 全部通过（115 项断言）
    r"断言[：:]\s*(\d+)\s*项",      # 断言：85 项｜通过 85
    r"断言\s*(\d+)\s*项",           # 断言 197 项｜失败 0 项
    r"PASS\s*全部\s*(\d+)\s*项",    # PASS 全部 68 项
    r"全部\s*(\d+)\s*项",           # 全部 64 项
]


def count_of(text: str) -> int | None:
    for pat in PATS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    if re.search(r"全部通过|结果[：:]\s*PASS|OK 全部通过", text):
        return 0  # 通过但没报数（如 _selftest.py）
    return None


def run_one(name: str, timeout: int = 900) -> tuple[int, int | None, str]:
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", TQDM_DISABLE="1")
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, name)], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return 124, None, f"超时 >{timeout}s"
    except OSError as e:  # 脚本不存在等
        return 127, None, str(e)[:120]
    sec = time.time() - t0
    tail = " | ".join([x.strip() for x in out.strip().splitlines()[-2:] if x.strip()])[:150]
    return rc, count_of(out), f"{sec:5.1f}s  {tail}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过要联网的套件")
    ap.add_argument("--only", default="", help="只跑文件名含该子串的套件")
    a = ap.parse_args()

    suites = list(SUITES) + ([] if a.fast else list(NETWORK))
    if a.only:
        suites = [s for s in suites if a.only in s[0]]
    if not suites:
        print(f"没有匹配的套件（--only {a.only!r}）")
        return 2

    print("=" * 78)
    print(f"技能离线自检：{len(suites)} 套{'（fast：已跳过联网套件）' if a.fast else ''}")
    print("=" * 78)
    bad, total = [], 0
    unknown = 0
    for name, desc in suites:
        rc, n, note = run_one(name)
        ok = "OK  " if rc == 0 else f"FAIL({rc})"
        if rc == 0:
            total += n or 0
            if n is None:
                unknown += 1
        else:
            bad.append(name)
        print(f"{ok:9} {name:24} {('—' if n is None else n):>4} 项  {desc}")
        if rc != 0:
            print(f"          ↳ {note}")
    print("-" * 78)
    if bad:
        print(f"结果：FAIL — {len(bad)} 套失败：{', '.join(bad)}")
        return 1
    extra = f"（另有 {unknown} 套只报「全部通过」不报数）" if unknown else ""
    print(f"结果：PASS — {len(suites)} 套全过，合计 {total} 项断言{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


