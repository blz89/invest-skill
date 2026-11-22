# -*- coding: utf-8 -*-
"""决策/月报编排：Lead + fresh 子代理 + 无 agent 规则引擎，T0–T10。

为什么要这一层：
    技能原本是「一堆脚本」，谁先跑谁后跑靠人记。加了 LLM 角色之后，
    必须有一张**唯一的编排表**写明：每一步谁负责、产物是什么、能不能跳过、
    失败怎么降级。否则 agent 之间会互相猜，猜错一次就是一整轮错账。

第一原则（框架的地基，改之前先读 config.TEAM 的注释）：
    凡是要重复执行的动作 → 规则引擎代码；凡只需判断一次的动作 → 才交给 LLM。

计费（周期结算）：下一次决策会话第 2 步 `billing.py --task-end` 补记「上次结算终点 → 现在」；
本次决策自己的花费留到下一次第 2 步（会话没结束时数不准），不按 run 窗口计时。

用法：
    python team.py --status
    python team.py --dry-run --capital 50000 --ledger 50k
    python team.py --run --capital 50000 --ledger 50k [--pipeline] [--monthly]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys

import config
import billing
import score

T = config.TEAM
ROLES = T["roles"]
STEPS = T["steps"]
STEP_BY_ID = {s["id"]: s for s in STEPS}
LLM_STEPS = [s["id"] for s in STEPS if s.get("llm")]
STATE_FILE = T["state_file"]
TASK_DIR = T["task_dir"]
TEAM_DIR = os.path.dirname(STATE_FILE)

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


# ---------------------------------------------------------------- 状态机
def new_state(capital: float, ledger: str) -> dict:
    return {
        "version": 1,
        "ledger": ledger,
        "capital": float(capital),
        "started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_ms": int(_dt.datetime.now().timestamp() * 1000),
        "ended_ms": None,
        "run_id": billing.new_run_id(ledger),
        "steps": {s["id"]: {"status": "pending", "at": None, "note": ""} for s in STEPS},
        "billing": None,
    }


def load_state() -> dict | None:
    if not os.path.isfile(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_state(st: dict) -> str:
    os.makedirs(TEAM_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return STATE_FILE


def mark(st: dict, sid: str, status: str, note: str = "") -> None:
    st["steps"][sid] = {"status": status,
                        "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "note": note}


# ---------------------------------------------------------------- 契约
def _who(key: str) -> str:
    """角色键 → 中文名。engine 是规则引擎，不是 agent。"""
    return ROLES[key]["name"] if key in ROLES else "规则引擎"


def handoff_contract() -> list[dict]:
    """agent 之间的唯一交接契约。不许口头传，只许走文件。"""
    return [
        {"name": "L1 个股全池快照", "path": "out/data_snapshot.csv",
         "cols": "code,name,industry,scale_yi,amt60_yi,roe5y_min,ocf_ni_3y,debt_ratio,"
                 "rev_cagr3,pe,pe_pct_self,pb_pct_ind,mdd3y,nav_date,source,stale_days",
         "producer": "data", "consumers": ["engine", "research"]},
        {"name": "L2 深取", "path": "out/deep/<code>.json",
         "cols": "code,name,公告_近90天,业绩预告,商誉占比,股东减持,监管处罚,"
                 "财报最新期,pe_hist_5y_pct,可证伪风险点,per_field_source,fetched_at",
         "producer": "data", "consumers": ["research"]},
        {"name": "研究结论", "path": "out/stage1b_research.csv",
         "cols": "code,action(reject/keep/downgrade),reason,need_deep,confidence",
         "producer": "research", "consumers": ["engine", "lead"]},
        {"name": "深取标签", "path": "out/stage1b_tags.csv",
         "cols": "code,risk_tag,event_tag,falsify_cond,source",
         "producer": "research", "consumers": ["engine"]},
        {"name": "计划", "path": "out/stage3_plan.json",
         "cols": "capital,tier,regime,positions,orders,cash_layer,risk_review",
         "producer": "engine", "consumers": ["report", "lead"]},
        {"name": "记分卡", "path": "out/scorecard.md",
         "cols": "本期净收益,评分,奖励,现金层/R2/R3 归因,可证伪巡检",
         "producer": "report", "consumers": ["lead", "review"]},
    ]


def budget_plan(capital: float, ledger: str) -> list[dict]:
    base = float(config.SCORE["budget"].get(ledger, 150.0))
    out = []
    for k, r in ROLES.items():
        out.append({"role": k, "name": r["name"], "kind": r["kind"],
                    "budget_pct": float(r["budget_pct"]),
                    "budget_cny": round(base * float(r["budget_pct"]), 2)})
    return out


# ---------------------------------------------------------------- LLM 任务文件
def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _ctx(st: dict) -> str:
    return (f"- 账本：`{st['ledger']}`｜本金：{st['capital']:,.0f} 元\n"
            f"- run_id：`{st['run_id']}`｜启动：{st['started_at']}\n"
            f"- 工作目录：`{config.OUT_DIR}`\n")


def _role_block(role_key: str) -> str:
    r = ROLES[role_key]
    return (f"**角色**：{r['name']}（{r['kind']}）\n"
            f"**职责**：{r['duty']}\n"
            f"**禁止**：{r['forbidden']}\n"
            f"**预算占比**：{float(r['budget_pct']):.0%}\n")


def task_T2(st: dict) -> str:
    return f"""# T2 研究员第一轮：30 → N（只做减法）

{_role_block('research')}
## 输入（只读，不许改）
- `out/stage1_candidates.csv` —— 规则引擎漏斗留下的候选（约 30 只）
- `out/data_snapshot.csv` —— 数据官出的**个股全池**快照（300 只，含规模/流动性/ROE/现金流/负债/成长/估值/回撤/新鲜度）。
  注意：`amt60_yi` 是 60 日均额；`pe_pct_self` 只有进了阶段二的候选才有，空值不是 0。
  ETF 与场外债基不在这张表里（见 `out/stage1_etf.csv` / `out/stage1_otc.csv`）。
- `out/news_events.csv` / `out/news_brief.md` —— 第 4 步事件面（个股资讯 + 全市场公告流 + 政策面）。
- `out/regime.json` —— 市场档位（深熊/熊/牛/普通、回撤、MA200 偏离、宽度）。

{_t2_background()}## 你要做的
1. 逐只判断**有没有会让我踩雷的事件风险**（公告/业绩预告/商誉/减持/监管）：先看下面的背景段（第 4 步事件面），背景没覆盖的再用快照事实判断。
2. 只允许三种动作：`reject`（剔除）/`downgrade`（降级）/`keep`（保留）。
3. 需要更多信息才能判断的，写进 `need_deep`（≤10 只，批量一次性要，不许逐只要）。

## 输出（严格按列，UTF-8 CSV）
`out/stage1b_research.csv`：code,action,reason,need_deep,confidence

## 硬约束
- **不许提名个股、不许给目标价、不许看历史涨跌幅做判断**（没有 PIT 数据，历史涨跌是噪声）。
- 你没有取数权：`datasource.py` / `otc.py` / 缓存目录都不许碰，也不许自己再去抓数据；只读上面列出的文件。
- 档位只作背景：**不许因为牛熊改变 keep/reject 的判定标准，也不许据此加池外票**；reject 必须点名具体事实，不许只写「因为熊市」。
- 事件面涉及候选的「高」级事件必须在 reason 里点名（代码 + 标题关键词）；事件面缺失就写「事件面未采集」，不许猜。
- 拿不准一律 `keep`（fail-open 在筛选层，fail-closed 在买入层）。

## 你是执行者（只做这一步）

> **边界（硬）**：只评估任务书给出的候选代码（`out/stage1_candidates.csv` / `stage1_etf.csv` / `stage1_otc.csv` 内的）。池外标的再看好也不许加；你的结论只能「否决 / 降权」，**不能新增标的、不能放宽条件**。候选不够就写不够，不许凑数。
- 你由 Lead 派来（fresh 子代理），**不是 Lead 本人**：只做 T2，不许做其他步骤、不许改脚本/config/账本。
- 做完必须把产物写进上面写死的路径，然后回报三行：**产物路径 / 行数或只数 / 你有多大把握**。
- 回报后由 Lead 跑 `python team.py --ingest T2 --capital <本金> --ledger <账本>` 验收；
  验收不过就重做这一步，不要自己往下走。

{_ctx(st)}"""


def task_T4(st: dict) -> str:
    return f"""# T4 研究员第二轮：回填风险/事件标签

{_role_block('research')}
## 输入（只读）
- `out/deep/<code>.json` —— 数据官批量深取的结果（只对 need_deep 里的代码）
- `out/stage1b_research.csv` —— 你第一轮的结论

## 你要做的
给每只保留的候选打两类标签，并写一条**可证伪条件**（什么情况下这只该被踢出去）。

## 输出（严格按列）
`out/stage1b_tags.csv`：code,risk_tag,event_tag,falsify_cond,source

## 硬约束
- 每个字段必须能追溯到 `deep/<code>.json` 里的具体字段（`source` 列写字段名）。
- 查不到就写 `unknown`，**不许猜**。

## 你是执行者（只做这一步）

> **边界（硬）**：只评估任务书给出的候选代码（`out/stage1_candidates.csv` / `stage1_etf.csv` / `stage1_otc.csv` 内的）。池外标的再看好也不许加；你的结论只能「否决 / 降权」，**不能新增标的、不能放宽条件**。候选不够就写不够，不许凑数。
- 你由 Lead 派来（fresh 子代理），**不是 Lead 本人**：只做 T4，不许做其他步骤、不许改脚本/config/账本。
- 做完必须把产物写进上面写死的路径，然后回报三行：**产物路径 / 行数或只数 / 你有多大把握**。
- 回报后由 Lead 跑 `python team.py --ingest T4 --capital <本金> --ledger <账本>` 验收；
  验收不过就重做这一步，不要自己往下走。

{_ctx(st)}"""


def task_T8(st: dict) -> str:
    return f"""# T8 月报记分卡：评分（只在月报做）

{_role_block('report')}
## 自动完成（代码，不需要 LLM）
- 评分与奖励：`python score.py --capital {st['capital']:.0f} --return <本期收益> --token-cost <费用> --mdd <回撤>`
- 计费：**本步不结算、不记账**——月报消耗由下一次决策会话第 2 步 `billing.py --task-end` 一并补记
- 产出：`out/scorecard.md`、`out/paper/scorecard.json`；统一用 `team.py --run-t8 --score-only`（不覆盖周决策写好的计划书）

## 需要 LLM 的部分（只有这一小段）
把记分卡写成**人能看懂**的三段：①本期赚亏归因（现金层/R2/R3 各贡献多少）
②评分与奖励算账 ③下一期要注意的一条。

## 硬约束
- 不许改策略、不许改账本、不许追加大额预算（预算追加是 B 级，必须用户拍板）。

{_ctx(st)}"""


def task_T9(st: dict) -> str:
    return f"""# T9 Lead：汇总 → 策略结论 → 未来规划 → 计费核对（不结算）

{_role_block('lead')}
## 输入（全部只读）
- `out/stage3_plan.md` / `out/stage3_plan.json` —— 本期计划
- `out/paper/weekly_<日期>.md` —— 本期周报（账本表现 / 巡检汇总 / 收益拆解）
- `out/budget.json` —— 预算账本
- `out/team/pending_changes.md` —— 复盘员攒的 B 级待批改动（上次的）
- 上期 `out/team/lead_plan.md` —— 你上次的规划（用来对比执行情况）
- `out/news_brief.md` + `out/news_events.csv` —— 事件面（公告/新闻/异动/除权）；
  **「高」级事件必须在结论里点名**，不能因为「不影响打分」就省略

## 你要产出的四段
1. **本期结论**：一句话说清这期赚没赚、赚在哪、亏在哪。
2. **策略调整**：只允许动 A 类（预先授权）动作；B 类写进 `pending_changes.md` 等用户批。
3. **未来 1–3 期规划**：下一期、下两期分别盯什么信号、什么条件下改变仓位。**必须含「下周日巡检附加任务」一行**：想清楚下周每天的巡检除了固定三行，是否要额外盯一件事（比如某只持仓的移动止损是否该上移、某个已知 bug 是否复现、多读一份文件报一行）；有就写成一句能直接粘进巡检提示词「四、本周附加任务」的话，**没有就写「无」**——但不许跳过这个思考、不许省略这一行。
4. **计费核对（本次不结算）**：核对上一周期已补记的计费结果与预算余额；本次决策的消耗留到下一次决策第 2 步补记，**本步不许跑 `billing.py --task-end`**。

## 硬约束
- 不许跳过审计直接改账本。
- 不许为了拿 A 去破风控（regime 仓位上限、单票上限、MDD 否决）。
- **骨架不算交卷**：`do_t9` 生成的骨架带 `T9_SKELETON` 标记，`--ingest T9` 会拒收；四段齐全、正文 ≥200 字、删掉该标记才算交卷。
- **决策日不评分**：评分/记分卡只在月报做（`team.py --run-t8`），周决策这一期只写事实、决策与计划，不写评级和奖励。

## 你是执行者（只做这一步）

> **边界（硬）**：只评估任务书给出的候选代码（`out/stage1_candidates.csv` / `stage1_etf.csv` / `stage1_otc.csv` 内的）。池外标的再看好也不许加；你的结论只能「否决 / 降权」，**不能新增标的、不能放宽条件**。候选不够就写不够，不许凑数。
- **T9 是 Lead 的职责：默认由 Lead 亲自写计划书**；若 Lead 派你执行，你只做 T9，不许做其他步骤、不许改脚本/config/账本。
- 做完必须把产物写进上面写死的路径，然后回报三行：**产物路径 / 行数或只数 / 你有多大把握**。
- 回报后由 Lead 跑 `python team.py --ingest T9 --capital <本金> --ledger <账本>` 验收；
  验收不过就重做这一步，不要自己往下走。

{_ctx(st)}"""


def task_T10(st: dict) -> str:
    return f"""# T10 复盘员：5 层反思闭环 + 月报策略复盘 + 技能优化清单（月度/异常触发）

{_role_block('review')}
## 一、五层（原有，保留）
1. **L1 归因**：现金 carry / R2 carry / R3 beta / alpha（alpha 不可测，直接写"不可测"）
2. **L2 记分卡**：本期评分 + 12 期分布 vs 目标 {config.SCORE['dist_target']}
3. **L3 可证伪巡检**：把历史写下的 `falsify_cond` 逐条回查，触发的标红
4. **L4 诊断树**：token 成本率 > 0.30% → 现金 carry < 1.0% → R2 实际占比 < 名义 → 权益 beta 为负 → 参数复核
5. **L5 参数复核**：只许动可回测层（现金容器/网格间距/再平衡阈值），每次只改一个参数

## 二、上月策略复盘（月报必写，不能只报数字）
用上月账本/计划/成交/新闻当证据，逐条回查「这套策略哪里值得改」。每条必须带数字或文件路径，不许凭感觉：
- **市场状态**：regime 判定是否提前/滞后？对照月内沪深300 与 MA200、状态切换日、当期实际仓位。
- **权益预算**：档位/状态给的权益上限用满了吗？没用满的钱停在哪、为什么（没标的/买不起一手/挂单没成交）。
- **承接层**：ETF 承接、R2 场外腿是否按计划落地（持仓/在挂/未成交）；没落地是规则问题还是执行问题。
- **三批建仓与条件单**：多少挂单成交/失效/仍在挂；失效是否集中在同一类（限价太低、失效期太短、一手太贵）。
- **止损与熔断**：触发几次、事后看是保护还是被洗；能判断就给数字，判断不了写「不可判断」。
- **现金层**：逆回购/货基收益 vs 假设利率/定存，是否跑赢、资金是否空转。
- **费用与换手**：费用/本金、佣金率、有没有高佣金小单。
- **单账本执行复盘**：本部署只跑 5 万账本，聚焦 5 万账本的整手/买不起/场外承接等执行细节。
最后给 **≤5 条「值得改进」**，每条写：证据 / 改什么 / 预期影响 / 归 A 类还是 B 类。随机波动不改规则，写「样本不足，不改规则」。

## 三、技能优化清单（A 类由 Lead 当场执行，B 类写待批）
扫技能自身（不是市场）：脚本 bug/边界、文档与代码不一致、报告不可读、取数低效/重复、失败兜底缺口、测试缺口、定时任务/流程缺口、已知坑复发。每条写成可照做的一行：
- **A 类**（低风险，Lead 直接改）：bug 修复 / 文档 / 报告可见性 / 性能 / 日志提示 / 测试补强
- **B 类**（会改变策略结论，用户拍板）：阈值 / 权重 / 预算 / 仓位上限 / 交易参数 / 启用停用数据源
A 类清单格式（必须一行一条）：
- [ ] `<file:line>` 改什么 → 为什么（证据）→ 怎么验证（测试/命令）
B 类同样一行一条写进 `pending_changes.md`，写清「改什么/为什么/预期影响/怎么回滚」。
Lead 会在月报任务里直接执行 A 类并把清单回填成 `- [x]（commit <hash>）`；你只负责列清单，不要自己改。

## 输出
- `out/team/retro.md`：**必须含 `## 六、上月策略复盘`（或「策略复盘」标题）与 `## 七、技能优化`（或「技能优化」标题）**，monthly.py 会把这两节原样摘进月报；另含五层内容。
- `out/team/pending_changes.md`：B 级待批。

## 硬约束
- **只读不改**。你没有任何写脚本/config/账本/策略文件的权限；A 类由 Lead 执行。
- 禁止复核：个股四维打分权重、四道否决阈值（没有 PIT 数据，改了只是换一种自信）——除非本月有新证据，且只能提为 B 类。

{_ctx(st)}"""


def retro_missing(txt: str) -> list[str]:
    """T10 产物必须含的两节（月报会把它们摘成「策略复盘 / 技能优化」）。"""
    need = ("策略复盘", "技能优化")
    return [k for k in need if k not in (txt or "")]


TASK_BUILDERS = {"T2": task_T2, "T4": task_T4, "T8": task_T8, "T9": task_T9, "T10": task_T10}


def write_tasks(st: dict, only: list[str] | None = None) -> list[str]:
    """给所有 LLM 步骤生成任务文件。dry-run 和真实运行都走这里。"""
    out = []
    wanted = set()
    os.makedirs(TASK_DIR, exist_ok=True)
    # 清掉上一轮遗留、这一轮不生成的步骤文件（只删 T<n>_*.md，不动别的）
    for sid in LLM_STEPS:
        if only and sid not in only:
            continue
        b = TASK_BUILDERS.get(sid)
        if not b:
            continue
        p = os.path.join(TASK_DIR, f"{sid}_{ROLES[STEP_BY_ID[sid]['owner']]['name']}.md")
        wanted.add(os.path.basename(p))
        out.append(_write(p, b(st)))
    try:
        import glob
        for f in glob.glob(os.path.join(TASK_DIR, "T*.md")):
            if os.path.basename(f) not in wanted:
                os.remove(f)
    except OSError:
        pass
    # 契约表
    lines = ["# agent 间交接契约（唯一事实来源，不许口头传）", ""]
    for c in handoff_contract():
        lines += [f"## {c['name']}", f"- 路径：`{c['path']}`",
                  f"- 列：`{c['cols']}`",
                  f"- 产出：{_who(c['producer'])}｜消费："
                  + "、".join(_who(x) for x in c["consumers"]), ""]
    out.append(_write(os.path.join(TEAM_DIR, "contract.md"), "\n".join(lines)))
    return out

# ------------------------------------------- 分阶段执行（把 LLM 步骤交给执行者：fresh 子代理）
# 为什么需要这层：run() 只生成任务文件，LLM 步骤从来没人执行 → 自动化里看不到执行者。
# 拆成 --stage（出任务书）/ --ingest（验收产物）后，Lead 每一步都能真正起一个执行者（subagent/队友）。
STAGE_ORDER = ["T2", "T3", "T4", "T8", "T9", "T10"]

CSV_SPEC = {
    "T2": ("stage1b_research.csv", ["code", "action", "reason", "need_deep", "confidence"]),
    "T4": ("stage1b_tags.csv", ["code", "risk_tag", "event_tag", "falsify_cond", "source"]),
}


def _out(*parts: str) -> str:
    return os.path.join(config.OUT_DIR, *parts)


def _state_for(capital: float, ledger: str) -> dict:
    """续跑用：同一个账本 + 同一个本金就接着上次的状态，不许把已完成的步骤冲回 pending。"""
    st = load_state()
    same = bool(st) and st.get("ledger") == ledger \
        and abs(float(st.get("capital") or 0.0) - float(capital)) <= 1.0
    if not same:
        st = new_state(capital, ledger)
        save_state(st)
    return st


def _split_codes(raw) -> list[str]:
    import re
    return [x.strip() for x in re.split(r"[,，;；、\s]+", str(raw or "")) if x.strip()]


def _csv_rows(name: str, need: list[str]) -> dict:
    """读 subagent 写的 CSV 并校验表头。缺文件/空表/缺列都算没通过。"""
    import csv
    p = _out(name)
    if not os.path.isfile(p):
        return {"ok": False, "reason": "缺产物 %s" % p}
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            rows = [r for r in rd if any((v or "").strip() for v in r.values())]
    except OSError as e:
        return {"ok": False, "reason": "读不了 %s：%s" % (name, e)}
    if not rows:
        return {"ok": False, "reason": "%s 是空表（subagent 没写东西）" % name}
    cols = list(rows[0].keys())
    miss = [c for c in need if c not in cols]
    if miss:
        return {"ok": False, "reason": "%s 缺列：%s（表头必须严格按契约：%s）"
                % (name, ",".join(miss), ",".join(need))}
    return {"ok": True, "rows": rows, "path": p}


def _at_epoch(at) -> float:
    """步骤时间戳（本地时间字符串）→ epoch 秒；解析不了返回 0。"""
    try:
        return _dt.datetime.strptime(str(at), "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return 0.0


def _artifact_fresh(path: str, at) -> tuple[bool, str]:
    """产物是不是本轮任务书之后写的。

    为什么要有它：`out/` 是跨周共用的，上一轮的 `stage1b_research.csv` / `stage1b_tags.csv`
    会留在盘上。执行者这轮没交东西时，`--ingest` 若只看表头就会把**上一轮结论**当本轮
    结论吃进去（静默、不报错）。这里用「产物 mtime ≥ 本轮任务书生成时间」兜住，
    拿旧文件来交直接拒收。
    """
    ts = _at_epoch(at)
    if ts <= 0 or not os.path.isfile(path):
        return True, ""
    mt = os.path.getmtime(path)
    if mt + 1.0 < ts:
        got = _dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
        return False, (f"{os.path.basename(path)} 比任务书旧（产物 {got} < 任务 {at}）"
                       f"——执行者没交本轮结果，不能拿上一轮遗留的结论顶上")
    return True, ""


_CODE_RE = __import__("re").compile(r"^(?:(?:sh|sz|bj)\d{6}|\d{6})$", __import__("re").I)


def _is_code(x) -> bool:
    return bool(_CODE_RE.match(str(x or "").strip()))


def _need_deep(rows: list[dict]) -> tuple[list[str], list[str]]:
    """从研究结论里抽出要深取的代码（跳过 reject），去重后最多 10 只。

    返回 (codes, bad)：bad 是 need_deep 里既不是空、也不是证券代码的值。
    旧契约把 need_deep 写成 true/false，那种表必须打回重做，不能静默忽略——
    静默忽略等于把研究结论扔掉，还会让 T3 去找 `true.json` 这种文件。
    """
    codes, bad = [], []
    for r in rows:
        if str(r.get("action", "")).strip().lower() == "reject":
            continue
        for x in _split_codes(r.get("need_deep")):
            if _is_code(x):
                codes.append(x)
            else:
                bad.append(x)
    return list(dict.fromkeys(codes))[:10], bad


def _task_path(sid: str) -> str:
    return os.path.join(TASK_DIR, "%s_%s.md" % (sid, ROLES[STEP_BY_ID[sid]["owner"]]["name"]))


def _codes_for_t3(st: dict | None = None) -> list[str]:
    codes = list((st or {}).get("_need_deep") or [])
    if codes:
        return codes
    c = _csv_rows("stage1b_research.csv", CSV_SPEC["T2"][1])
    return _need_deep(c["rows"])[0] if c["ok"] else []


def task_T3(st: dict, codes: list[str]) -> str:
    lst = "\n".join("%d. `%s`" % (i + 1, c) for i, c in enumerate(codes))
    return f"""# T3 数据官：批量深取 {len(codes)} 只（一次取完，不许逐只来）

{_role_block('data')}
## 你要做的
对下面每一只，按契约字段深取一次，落盘 `out/deep/<code>.json`：

{lst}

## 每只 JSON 的字段（严格）
`code,name,公告_近90天,业绩预告,商誉占比,股东减持,监管处罚,财报最新期,pe_hist_5y_pct,可证伪风险点,per_field_source,fetched_at`

- 公告 / 减持 / 监管处罚：`python cninfo.py --code <code> --days 90 --json`（巨潮公告，带 pubDate；`--soft` 连软信号一起报）
- 批量：`python cninfo.py --scan <候选池.csv> --days 365 --json`
- 全市场（含池外持仓的突发公告）：`python cninfo.py --market --market-days 7 --json`；只看持仓/目标：`--market --watch ledger/state_50k.json`
- 财报最新期 / 历史估值分位：`python pit.py --code <code>`
- 查不到就写 `unknown`，**不许猜、不许用相邻年份顶替**。
- `per_field_source` 逐字段写清来源（脚本+接口），`fetched_at` 写 ISO 时间。

## 硬约束
- **行情/财务/估值一律用本机交易日 19:00 统一取数（`stock prefetch`）后的产物**（`out/stage1_*` / `out/stage2_*` / `out/data_snapshot.csv`）——**不许重跑全量取数**；公告/新闻/监管等脚本拿不到或周末新增的信息由你补采（`cninfo.py` / `news.py`）。
- 只取数，不做判断、不筛选、不给结论。
- 每只单独一个文件，文件名 `<6位代码>.json`。

## 你是执行者（只做这一步）

> **边界（硬）**：只评估任务书给出的候选代码（`out/stage1_candidates.csv` / `stage1_etf.csv` / `stage1_otc.csv` 内的）。池外标的再看好也不许加；你的结论只能「否决 / 降权」，**不能新增标的、不能放宽条件**。候选不够就写不够，不许凑数。
- 你由 Lead 派来（fresh 子代理），**不是 Lead 本人**：只做 T3，不许做别的步骤、不许改脚本/config/账本。
- 做完回报三行：**产物目录 / 成功几只 / 你有多大把握**。
- 回报后由 Lead 跑 `python team.py --ingest T3 ...` 验收；不过就重做这一步。

{_ctx(st)}"""


def stage(sid: str, capital: float, ledger: str) -> dict:
    """生成某一步的任务书，并把该步标成 pending。"""
    sid = sid.upper()
    if sid not in STAGE_ORDER:
        return {"ok": False, "reason": "--stage 只支持 %s" % ", ".join(STAGE_ORDER)}
    st = _state_for(capital, ledger)
    if sid == "T3":
        codes = _codes_for_t3(st)
        if not codes:
            return {"ok": False, "reason": "T2 没写 need_deep → 跳过 T3，直接 --stage T4"}
        text = task_T3(st, codes)
    else:
        text = TASK_BUILDERS[sid](st)
    p = _write(_task_path(sid), text)
    mark(st, sid, "pending", "任务书已生成（等执行者写产物）")
    save_state(st)
    return {"ok": True, "sid": sid, "task_file": p, "text": text}


def ingest(sid: str, capital: float, ledger: str, dry: bool = False) -> dict:
    """校验执行者（subagent/队友）的产物 → 标记完成 → 自动推进下一步。"""
    sid = sid.upper()
    if sid not in STAGE_ORDER:
        return {"ok": False, "reason": "--ingest 只支持 %s" % ", ".join(STAGE_ORDER)}
    st = _state_for(capital, ledger)

    # ---- T2 / T4：CSV 产物
    if sid in ("T2", "T4"):
        name, cols = CSV_SPEC[sid]
        c = _csv_rows(name, cols)
        if not c["ok"]:
            return {"ok": False, "reason": c["reason"]}
        fresh, why = _artifact_fresh(c.get("path"), (st.get("steps", {}).get(sid) or {}).get("at"))
        if not fresh:
            return {"ok": False, "reason": why}
        rows = c["rows"]
        if sid == "T2":
            acts = [str(r.get("action", "")).strip().lower() for r in rows]
            bad = [r.get("code") for r, a in zip(rows, acts) if a not in ("reject", "keep", "downgrade")]
            if bad:
                return {"ok": False,
                        "reason": "action 只允许 reject/keep/downgrade，非法行：%s" % bad[:5]}
            bad_codes = [r.get("code") for r in rows if not _is_code(r.get("code"))]
            if bad_codes:
                return {"ok": False,
                        "reason": "code 列必须是 6 位证券代码，非法行：%s" % bad_codes[:5]}
            codes, bad = _need_deep(rows)
            if bad:
                return {"ok": False,
                        "reason": ("need_deep 必须是证券代码清单（逗号分隔），"
                                   "发现非代码值：%s —— 若整列是 true/false，说明是旧契约产物，"
                                   "请删掉 out/stage1b_research.csv 重跑 T2") % bad[:5]}
            # 校验全过才标完成：否则拒收后 state 会留下「T2 ok」，下次 ingest 会以为已交卷
            n_rej = sum(1 for a in acts if a == "reject")
            mark(st, "T2", "ok", "%d 只：reject %d｜保留 %d" % (len(rows), n_rej, len(rows) - n_rej))
            st["_need_deep"] = codes
            save_state(st)
            nxt = "T3" if codes else "T4"
            out = stage(nxt, capital, ledger)
            return {"ok": out["ok"], "sid": "T2", "next": nxt,
                    "task_file": out.get("task_file"), "reason": out.get("reason", "")}
        mark(st, "T4", "ok", "%d 只已打标签" % len(rows))
        save_state(st)
        r = run(capital, ledger, pipeline=True, dry=dry, resume=True)
        # pipeline 只跑到 T7（计划生成）；评分（T8）改到月报做，决策日不再评分。
        # 所以下一步指 T9：先 weekly 落 nav、review 验收，再 --stage T9 写计划书。
        return {"ok": not r["aborted"], "sid": "T4", "next": "T9",
                "log": r["log"],
                "hint": "先跑 run.py --only-plan → weekly.py --ledger %s --reuse → "
                        "review.py，再 team.py --stage T9 --ledger %s 写计划书"
                        "（评分在月报，决策日不出评分）" % (ledger, ledger),
                "reason": "" if not r["aborted"] else "下游引擎失败（见 log）"}

    # ---- T3：深取产物齐全性
    if sid == "T3":
        codes = _codes_for_t3(st)
        if not codes:
            return {"ok": False, "reason": "拿不到 need_deep 清单，先 --ingest T2"}
        missing = [x for x in codes if not os.path.isfile(_out("deep", "%s.json" % x))]
        if missing:
            return {"ok": False,
                    "reason": "out/deep 缺 %d/%d 只：%s" % (len(missing), len(codes), missing[:8])}
        t3_at = (st.get("steps", {}).get("T3") or {}).get("at")
        stale = [x for x in codes
                 if not _artifact_fresh(_out("deep", "%s.json" % x), t3_at)[0]]
        if stale:
            return {"ok": False,
                    "reason": ("out/deep 有 %d 只是上一轮遗留（比 T3 任务书旧）：%s"
                               "——深取必须本轮重跑，不能拿旧结论顶上") % (len(stale), stale[:8])}
        mark(st, "T3", "ok", "%d 只深取齐全" % len(codes))
        save_state(st)
        out = stage("T4", capital, ledger)
        return {"ok": out["ok"], "sid": "T3", "next": "T4",
                "task_file": out.get("task_file"), "reason": out.get("reason", "")}

    # ---- T10：月度复盘（五层 + 策略复盘 + 技能优化，月报直接摘用）
    if sid == "T10":
        p = os.path.join(TEAM_DIR, "retro.md")
        if not os.path.isfile(p):
            return {"ok": False, "reason": "缺 %s" % p}
        try:
            txt = open(p, encoding="utf-8").read()
        except OSError as exc:
            return {"ok": False, "reason": "读不了 %s（%s）" % (p, exc)}
        miss = retro_missing(txt)
        if miss:
            return {"ok": False,
                    "reason": ("%s 缺「%s」小节——月报要求策略复盘+技能优化"
                               "（模板见 T10 任务书，title 含这两个词即可）"
                               % (os.path.basename(p), "、".join(miss)))}
        mark(st, "T10", "ok", "五层+策略复盘+技能优化已落地")
        save_state(st)
        return {"ok": True, "sid": "T10", "next": None,
                "reason": "复盘完成 → 进入 T9（计费由下一次决策第 2 步补记）"}

    # ---- T8 / T9：LLM 只是把代码产物写成人的语言
    if sid == "T8":
        p = _out("scorecard.md")
        if not os.path.isfile(p):
            return {"ok": False, "reason": "缺 %s" % p}
        mark(st, "T8", "ok", "记分卡已补成人话")
        save_state(st)
        out = stage("T9", capital, ledger)
        return {"ok": out["ok"], "sid": "T8", "next": "T9",
                "task_file": out.get("task_file"), "reason": out.get("reason", "")}
    p = os.path.join(TEAM_DIR, "lead_plan.md")
    if not os.path.isfile(p):
        return {"ok": False, "reason": "缺 %s" % p}
    # T9 是决策日唯一由人写的内容：只查「文件存在」会让空白/上期/缺段落的计划书静默过关，
    # 决策记录就断了。这里至少校验：本轮写的（不是上周遗留）+ 四段标题都在 + 有正文。
    _fresh, _why = _artifact_fresh(p, (st.get("steps", {}).get("T9") or {}).get("at")
                                   or st.get("started"))
    if not _fresh:
        return {"ok": False, "reason": _why}
    try:
        with open(p, encoding="utf-8") as f:
            _txt = f.read()
    except OSError as exc:
        return {"ok": False, "reason": "读不了 %s：%s" % (p, exc)}
    _need = ["本期结论", "策略调整", "未来", "计费核对"]
    if "T9_SKELETON" in _txt:
        return {"ok": False,
                "reason": "计划书仍是 do_t9 生成的骨架（含 `T9_SKELETON` 标记）："
                          "Lead 必须把四段改写成本期真实内容并删掉标记，"
                          "骨架不算交卷（模板见 --stage T9 任务书）"}
    _miss = [k for k in _need if k not in _txt]
    if _miss:
        return {"ok": False,
                "reason": "计划书缺段落：%s（四段：本期结论 / 策略调整 / 未来 1–3 期规划 / 计费核对，"
                          "模板见 --stage T9 任务书）" % "、".join(_miss)}
    _body = [ln.strip() for ln in _txt.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if len("".join(_body)) < 200:
        return {"ok": False, "reason": "计划书正文太短（<200 字）：像是只留了骨架、没写内容"}
    mark(st, "T9", "ok", "Lead 四段已填")
    save_state(st)
    return {"ok": True, "sid": "T9", "next": None,
            "reason": "T9 完成 → 第 10 步收口（计费留到下次决策第 2 步补记）"}



# ---------------------------------------------------------------- 事件面（T0 附带）
def news_summary() -> dict:
    """读 `out/news_events.csv` 汇总事件面。缺失/旧版 → exists=False，不阻断主链路。

    这一层是 fail-soft：新闻挂了不影响买卖决策，但**有高优先级事件必须让 Lead 看见**。
    """
    path = os.path.join(config.OUT_DIR, "news_events.csv")
    brief = os.path.join(config.OUT_DIR, "news_brief.md")
    out = {"exists": False, "n": 0, "high": 0, "mid": 0, "low": 0,
           "date": None, "csv": path, "brief": brief, "high_items": []}
    try:
        csv = __import__("csv")
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                out["exists"] = True
                out["n"] += 1
                lv = (row.get("level") or "").strip()
                if lv == "高":
                    out["high"] += 1
                    out["high_items"].append(
                        "%s %s｜%s｜%s" % (row.get("date"), row.get("code"),
                                           row.get("tag") or row.get("kind"),
                                           (row.get("title") or "")[:60]))
                elif lv == "中":
                    out["mid"] += 1
                else:
                    out["low"] += 1
                d = (row.get("date") or "").strip()
                if d and (out["date"] is None or d > out["date"]):
                    out["date"] = d
    except OSError:
        pass
    out["brief_exists"] = os.path.isfile(brief)
    return out


def regime_summary() -> dict:
    """读 `out/regime.json`：市场档位立场（深熊/熊/牛/普通）。缺失 → exists=False，不阻断。

    只作背景：档位决定第 6 步的权益上限与排名篮子只数，**不改研究层的 keep/reject 权限**。
    """
    path = os.path.join(config.OUT_DIR, "regime.json")
    out = {"exists": False, "path": path, "severity": None, "stance": None,
           "state": None, "why": "", "dd_2y": None, "dev200": None,
           "breadth": None, "above": None, "total": None, "generated_at": None,
           "legacy": False}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    main = next((x for x in (doc.get("indexes") or [])
                 if x.get("code") == doc.get("main_index")), {})
    br = doc.get("breadth") or {}
    state = str(doc.get("state") or "")
    sev, stance = doc.get("severity"), doc.get("stance")
    legacy = not (sev or stance)  # 旧版产物只有 state，没有 stance/severity
    if legacy:
        sev = ("熊" if "熊市确认" in state else
               "牛" if "牛市确认" in state else "普通" if state else "未知")
        stance = ("bear" if sev == "熊" else "bull" if sev == "牛"
                  else "normal" if state else None)
    out.update({"exists": True,
                "severity": sev, "stance": stance,
                "state": doc.get("state"),
                "why": doc.get("stance_why") or (doc.get("explain") or "" if legacy else ""),
                "dd_2y": main.get("dd_2y"), "dev200": main.get("dev200"),
                "breadth": br.get("ratio"), "above": br.get("above"),
                "total": br.get("total"), "generated_at": doc.get("generated_at"),
                "legacy": legacy})
    return out


def _pct(x, nd: int = 1) -> str:
    return "—" if x is None else f"{float(x) * 100:.{nd}f}%"


def _basket_ladder() -> str:
    """把 config 的「权益比例 → 篮子只数」映射写成任务书里的一句人话（阈值只读 config）。"""
    rows = list(config.EXEC.get("max_stock_names_by_ratio") or [])
    if not rows:
        return "按档位 4–6 只"
    return "、".join(f"权益 ≤{float(cap):.0%} → {int(n)} 只" for cap, n in rows)


def _t2_background() -> str:
    """T2 任务书的背景段：第 4 步事件面 + 市场档位。

    为什么给研究层看这两样：
      1) 事件面原来只进脚本新闻闸和 T3 深取，T2 第一轮「判事件风险」实际没有输入，
         等于凭快照字段猜事件；这里把候选相关的事件摘要直接摆出来。
      2) 牛熊档位决定权益上限和篮子只数（4/5/6），研究层不知道档位就会拿
         「反正只买 4 只」当理由多剔；但档位只作背景，**不许据此改权限、加池外票**。
    两样产物缺失时如实写「未生成」，不阻断任务书生成（fail-soft）。
    """
    lines = ["## 背景（只作判断依据，不改你的权限）", ""]
    rg = regime_summary()
    if rg["exists"]:
        head = rg.get("severity") or rg.get("stance") or "未知"
        lines.append(f"- **市场档位：{head}**（{rg.get('state') or '—'}）")
        if rg.get("legacy"):
            lines.append("  - ⚠ 该 `regime.json` 是旧格式（缺 stance/severity 字段），档位按 state 推断；"
                         "先跑第 4 步 `regime_probe.py` 重写，别用旧文件下结论。")
        if rg.get("why"):
            lines.append(f"  - 判定依据：{rg['why']}")
        lines.append("  - 沪深300 距 2 年高点回撤 %s｜相对 MA200 %s｜全市场宽度 %s"
                     "（%s/%s 只在 MA200 上方）"
                     % (_pct(rg.get("dd_2y")), _pct(rg.get("dev200")),
                        _pct(rg.get("breadth")), rg.get("above"), rg.get("total")))
        lines.append(f"  - 本期排名篮子只数（第 6 步按档位算，不由你定）：{_basket_ladder()}")
        lines.append("  - 档位怎么用：**只影响你对风险证据严重性的判断**，不改你的权限。")
        lines.append("    - 熊/深熊：同样的利空更可能是永久损失（估值下杀 + 流动性收缩），"
                     "风险类证据按更重看待；但 reject 必须点名具体事实（公告/预告/财务/估值分位），"
                     "**不许只写「因为熊市」**。")
        lines.append("    - 牛：不许因为「牛市」忽略风险或放宽条件；牛市里篮子更宽（5–6 只），"
                     "你的任务是别把好票错剔。")
        lines.append("    - 普通：按事实判断，不因档位加减分。")
        lines.append("  - （regime.json 生成于 %s；若不是今天，先跑第 4 步刷新）"
                     % (rg.get("generated_at") or "未知"))
    else:
        lines.append("- 市场档位：`out/regime.json` 未生成（先跑第 4 步 `regime_probe.py`）；"
                     "本条不阻断，但不要瞎猜牛熊。")
    lines.append("")
    nev = news_summary()
    if nev["exists"]:
        lines.append("- **事件面**：`out/news_events.csv` 共 %d 条（高 %d / 中 %d / 低 %d，"
                     "事件日期最晚 %s，含已公告的未来除权/解禁日）；简报 `out/news_brief.md`。"
                     % (nev["n"], nev["high"], nev["mid"], nev["low"],
                        nev["date"] or "未知"))
        if nev["high_items"]:
            lines.append("  - 高优先级事件（涉及候选的，必须在 reason 里点名代码+标题关键词）：")
            for it in nev["high_items"][:8]:
                lines.append(f"    - {it}")
        lines.append("  - 用法：**只认候选池内代码的事件**；池外事件再大也不能当加票理由。"
                     "事件与快照冲突时以事件面为准，但 reason 要写清证据来源（日期+标题关键词）。")
    else:
        lines.append("- 事件面：`out/news_events.csv` 未生成（先跑第 4 步 `news.py`）；"
                     "**不许猜事件**，缺就写「事件面未采集」。")
        lines.append("  - 用法不变：**只认候选池内代码的事件**；池外事件再大也不能当加票理由。")
    lines.append("- 情绪（`out/sentiment.json`）/ 宏观（`out/macro.json`）是背景板，可读，"
                 "但不进你的 keep/reject 依据。")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------- 行业/情绪/宏观（T0 附带）
def industry_summary() -> dict:
    """读 `out/industry_map.csv` + plan，给行业分布与超限。缺文件 → exists=False。"""
    mp_path = os.path.join(config.OUT_DIR, "industry_map.csv")
    out = {"exists": False, "n": 0, "map": mp_path, "top": [], "over": [],
           "max_weight": 0.0, "max_industry": None, "basis": "股票层"}
    if not os.path.isfile(mp_path):
        return out
    try:
        import industry as ind_mod
        rows = ind_mod.load_map()
        out["exists"] = bool(rows)
        out["n"] = len(rows)
        pos = ind_mod.plan_positions()
        if pos:
            base = ind_mod.plan_base() or ind_mod.res_total(pos)
            res = ind_mod.concentration(pos, float(config.RISK.get("industry_cap") or 0.25), base)
            out.update({"top": [(r["industry"], r["weight"]) for r in res["rows"][:5]],
                        "over": [r["industry"] for r in res["violated"]],
                        "max_weight": res["max_weight"],
                        "max_industry": res["rows"][0]["industry"] if res["rows"] else None,
                        "basis": "权益层"})
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def sentiment_summary() -> dict:
    """读 `out/sentiment.json`：市场温度 + 持仓旗标。缺文件 → exists=False。"""
    path = os.path.join(config.OUT_DIR, "sentiment.json")
    brief = os.path.join(config.OUT_DIR, "sentiment_brief.md")
    out = {"exists": False, "brief": brief, "temp": {}, "flags": []}
    if not os.path.isfile(path):
        return out
    try:
        import json as _json
        with open(path, encoding="utf-8") as f:
            doc = _json.load(f)
        out.update({"exists": True, "temp": doc.get("temp") or {},
                    "flags": doc.get("flags") or [],
                    "generated_at": doc.get("generated_at")})
    except (OSError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def macro_summary() -> dict:
    """读 `out/macro.json`：背景板。缺文件 → exists=False。"""
    path = os.path.join(config.OUT_DIR, "macro.json")
    brief = os.path.join(config.OUT_DIR, "macro_brief.md")
    out = {"exists": False, "brief": brief, "data": {}, "real_rate": None,
           "m1_m2_gap": None}
    if not os.path.isfile(path):
        return out
    try:
        import json as _json
        with open(path, encoding="utf-8") as f:
            doc = _json.load(f)
        out.update({"exists": True, "data": doc.get("data") or {},
                    "real_rate": doc.get("real_rate"),
                    "m1_m2_gap": doc.get("m1_m2_gap"),
                    "generated_at": doc.get("generated_at")})
    except (OSError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------- 巡检清单（T0 体检用）
def patrol_summary() -> dict:
    """读 DSH 工作日巡检产物 `out/patrol_latest.json`（20:00 会话跑 patrol.py 写，纯代码）。

    巡检在工作日 20:00 跑一次，这里只做展示；文件缺失不算 fail（账本空仓期巡检为空是正常的）。
    """
    p = os.path.join(config.OUT_DIR, "patrol_latest.json")
    if not os.path.isfile(p):
        return {"exists": False, "date": None, "alerts": [], "n_alerts": 0, "n_high": 0,
                "ledgers": [], "path": p}
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"exists": False, "date": None, "alerts": [], "n_alerts": 0, "n_high": 0,
                "ledgers": [], "path": p}
    doc["exists"] = True
    doc["path"] = p
    return doc


# ---------------------------------------------------------------- 数据体检（T0）
def product_ref_date(today: _dt.date | None = None) -> _dt.date:
    """产物的「参考日」：优先 stage3_plan.json 的 data_as_of，缺失/坏了才用今天。

    为什么不能直接拿「今天」判新鲜度：决策日固定周六 20:00，行情数据是工作日 19:00
    取的最后交易日（周五）的——stage1_candidates/stage2_judged 天然是周五生成，
    用「mtime == 今天」会把它们误判成过期、fail-closed 掉整条 T5–T7（2026-11-21 实测）。
    正确口径：产物只要不早于本次决策所用的数据日（data_as_of）就是新鲜的；
    数据日本身是否足够新，由决策日第 3 步与 doctor 负责，不在这里重复判。
    """
    p = os.path.join(config.OUT_DIR, "stage3_plan.json")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            v = str(d.get("data_as_of") or "")[:10]
            ref = _dt.date.fromisoformat(v)
            if ref <= (today or _dt.date.today()):
                return ref
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return today or _dt.date.today()


def check_data() -> dict:
    """T0：检查 out/ 关键产物是否存在、是否早于本次数据日。不新鲜就 fail-closed。"""
    need = ["stage1_candidates.csv", "stage2_judged.csv", "data_snapshot.csv",
            "stage3_plan.json"]
    rows, stale = [], []
    ref = product_ref_date()
    for f in need:
        p = os.path.join(config.OUT_DIR, f)
        if not os.path.isfile(p):
            rows.append({"file": f, "exists": False, "mtime": None, "fresh": False})
            stale.append(f)
            continue
        mt = _dt.date.fromtimestamp(os.path.getmtime(p))
        fresh = (mt >= ref)
        rows.append({"file": f, "exists": True, "mtime": str(mt), "fresh": fresh,
                     "bytes": os.path.getsize(p)})
        if not fresh:
            stale.append(f)
    return {"ok": not stale, "ref_date": str(ref), "files": rows, "stale": stale,
            "news": news_summary(),
            "industry": industry_summary(),
            "sentiment": sentiment_summary(),
            "macro": macro_summary(),
            "patrol": patrol_summary(),
            "checked_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------------------------------------------------------- 计划与收益
def paper_return(ledger: str) -> float | None:
    """从模拟盘账本算组合收益率。拿不到返回 None（不编）。"""
    try:
        sf = config.PAPER["ledgers"][ledger]["state_file"]
    except (KeyError, TypeError):
        return None
    if not os.path.isfile(sf):
        return None
    try:
        with open(sf, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    meta = st.get("meta") or {}
    cap = float(meta.get("capital") or 0.0)
    if cap <= 0:
        return None
    # 优先用 nav 序列末值
    nav = st.get("nav") or st.get("nav_series") or []
    if isinstance(nav, list) and nav:
        last = nav[-1]
        v = last.get("nav") if isinstance(last, dict) else last
        try:
            v = float(v)
            if v > 0:
                # 账本 nav 存的是绝对净值（元），不是比值：>2 视为绝对净值，否则按比值兼容
                return (v / cap - 1.0) if v > 2.0 else (v - 1.0)
        except (TypeError, ValueError):
            pass
    # 退而求其次：现金 + 持仓市值（positions 列表或 holdings 字典都接）
    cash = st.get("cash")
    pos = st.get("positions")
    if pos is None:
        pos = list((st.get("holdings") or {}).values())
    if isinstance(cash, (int, float)):
        mv = 0.0
        for p in pos:
            try:
                mv += float(p.get("shares") or 0) * float(p.get("last_price") or p.get("price") or 0)
            except (TypeError, ValueError):
                continue
        return (float(cash) + mv - cap) / cap
    return None


def plan_mdd() -> float | None:
    p = os.path.join(config.OUT_DIR, "stage3_plan.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    for k in ("mdd", "max_drawdown", "mdd_3y"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    perf = d.get("performance") or {}
    for k in ("mdd", "max_drawdown"):
        v = perf.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    # plan.py 不写组合回撤键 → 用持仓权重 × 各自回撤估组合口径（加权平均，code 兜底，不编造）
    tot = wsum = 0.0
    for it in (d.get("positions") or []):
        try:
            w = float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        dd = it.get("max_dd_3y")
        if not isinstance(dd, (int, float)):
            dd = (it.get("etf_metrics") or {}).get("dd_1y") or (it.get("etf_metrics") or {}).get("dd_3y")
        if isinstance(dd, (int, float)) and w > 0:
            tot += w * float(dd)
            wsum += w
    if wsum > 0:
        return tot / wsum
    return None


# ---------------------------------------------------------------- 运行
def _call(script: str, *args: str, timeout: int = 1800) -> tuple[int, str]:
    cmd = [PY, os.path.join(HERE, script), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, cwd=HERE)
        tail = (r.stdout or "").strip().splitlines()
        return r.returncode, ("\n".join(tail[-3:]) if tail else (r.stderr or "").strip()[-300:])
    except (OSError, subprocess.TimeoutExpired) as e:
        return 99, f"{type(e).__name__}: {e}"


def do_t9(st: dict, chk: dict | None, ledger: str, capital: float,
          ev: dict, ret: float, token_cost: float, cost_src: str,
          run_cost: float | None = None) -> list[str]:
    """生成 T9 Lead 规划骨架（`out/team/lead_plan.md`），返回日志行。

    必须在 T8 之后调用：第 1 段「本期结论」直接引用 T8 的净收益/评分/token 成本；
    若 T8 还没结算（净收益假 0），T9 会把假数字写进 Lead 的决策依据（2026-11-07 B 类 #5）。
    `chk` 可传 None（--run-t8 单跑时不重复体检），此时各层摘要自行回退读取产物。
    """
    log: list[str] = []
    news = (chk or {}).get("news") or news_summary()
    ind = (chk or {}).get("industry") or industry_summary()
    sent = (chk or {}).get("sentiment") or sentiment_summary()
    mac = (chk or {}).get("macro") or macro_summary()
    pat = (chk or {}).get("patrol") or patrol_summary()
    _t = sent.get("temp") or {}
    sent_line = (f"交易日 {_t.get('trade_date') or '-'}｜涨停 {_t.get('limit_up', 0)}"
                 f"｜跌停 {_t.get('limit_down', 0)}｜炸板 {_t.get('break', 0)}"
                 f"｜封板率 {float(_t.get('seal_ratio') or 0):.0%}"
                 if sent.get("exists") else "情绪层未刷新（先跑 sentiment.py）")
    ind_line = (f"映射 {ind.get('n', 0)} 只｜最大行业 {ind.get('max_industry') or '-'}"
                f" 占{float(ind.get('max_weight') or 0):.1%}（{ind.get('basis')}）"
                if ind.get("exists") else "行业层未刷新（先跑 industry.py --refresh）")
    _m = mac.get("data") or {}
    _lpr = (_m.get("lpr") or {}).get("lpr1y")
    _cpi = (_m.get("cpi") or {}).get("yoy")
    _pmi = (_m.get("pmi") or {}).get("manufacturing")
    _sh = (_m.get("shibor") or {}).get("last")
    mac_line = (f"LPR1Y {_lpr}%｜CPI 同比 {_cpi}%｜PMI {_pmi}｜SHIBOR 3M {_sh}%"
                f"｜实际利率约 {mac.get('real_rate')}%"
                if mac.get("exists") else "宏观层未刷新（先跑 macro.py）")
    pat_line = (f"巡检日 {pat.get('date') or '-'}｜待处理 {pat.get('n_alerts', 0)} 条"
                f"（高 {pat.get('n_high', 0)}）"
                if pat.get("exists") else "巡检未跑（先跑 patrol.py，工作日 20:00 定时任务会跑）")
    plan_md = [
        f"# Lead 规划 — {ledger}｜{st['started_at']}",
        "",
        "> 骨架标记 `T9_SKELETON`：这是 do_t9 生成的骨架，`ingest T9` 见到本行会拒收。"
        "Lead 必须把四段改写成**本期真实内容**，并删掉本行（含本行整段提示）。",
        "",
        "## 1) 本期结论（一句话）",
        f"- 账户收益 {ev['gross_profit']:.2f} 元（{ret:.2%}）｜token 实花 {token_cost:.4f} 元"
        f"（{cost_src}）｜净利润 {ev['net_profit']:.2f} 元｜净收益率 {ev['net_rate']:.2%}"
        f" → 评分 {ev['grade']}。",
        (f"- 奖励：{ev.get('reward_note')}" if ev.get("reward_pending")
         else f"- 奖励 {ev['reward_cny']:.2f} 元（{ev['reward_pct']:.0%}，评估期已到）"),
        "",
        "## 2) 策略调整（只允许 A 类，B 类进 pending_changes）",
        "- A 类候选：切现金层容器 / 停买 / 按 regime 降档 / 超支降级",
        "- B 类（必须用户拍板）：改参数、改规则、启用新数据源、调权益上限、追加预算",
        "",
        "## 3) 未来 1–3 期规划",
        "- 下一期盯：现金层实际 carry、R2 实际占比、权益 beta、token 成本率",
        "- 触发条件：",
        "  - 现金 carry < 1.0% → 只该持国债，不再承担波动",
        "  - R2 实际占比 < 名义 40% → 检查场外池是否缺失",
        "  - token 成本率 > 0.30% → 降级 LLM 步骤",
        "- 下周日巡检附加任务（可写「无」，但这一行必须写）：",
        "  - 想清楚下周每天 20:00 的巡检除了固定三行，是否要额外盯一件事"
        "（如某只持仓的移动止损是否该上移 / 某个已知 bug 是否复现 / 多读一份文件报一行）",
        "  - 有 → 写成一句可直接粘进巡检提示词「四、本周附加任务」的话；没有 → 写「无」",
        "",
        "## 4) 计费核对（本次不结算）",
        f"- token 实花（累计）{token_cost:.4f} 元（口径 {cost_src}）"
        + (f"｜本次 run 观测 {run_cost:.4f} 元（不结算，留到下次决策第 2 步补记）"
           if run_cost is not None else ""),
        f"- 净利润 {ev['net_profit']:.2f} 元｜净收益率 {ev['net_rate']:.2%}"
        f"（毛利口径 {ret:.2%}）",
        f"- 预算余额见 `out/budget.json`：`{ledger}` 剩余 "
        f"{billing.budget_state(ledger, capital)['remaining_cny']:.4f} 元",
        "",
        "## 附1) 事件面（公告/新闻/异动）",
        f"- 扫描日期 {news.get('date') or '（无）'}｜事件 {news.get('n', 0)} 条"
        f"（高 {news.get('high', 0)}｜中 {news.get('mid', 0)}）"
        f"｜来源 `{os.path.basename(news.get('brief') or '')}`",
    ] + ([f"- ⚠️ 必须点名：{x}" for x in (news.get("high_items") or [])[:8]]
         or ["- 无高优先级事件（这是好消息，写清楚，别省略）"]) + [
        "",
        "## 附2) 情绪层（市场温度 + 持仓旗标）",
        f"- {sent_line}",
    ] + ([f"- ⚠️ 旗标：{x['level']} {x['code']} {x['name']}｜{x['kind']}：{x['detail']}"
          for x in (sent.get("flags") or [])[:10]]
         or ["- 关注标的无异动/涨跌停/龙虎榜/异动原因。"]) + [
        "",
        "## 附3) 行业集中度（同花顺一级行业）",
        f"- {ind_line}",
    ] + ([f"- ⚠️ 超限行业：{x}" for x in (ind.get("over") or [])]
         or ["- 没有行业超限。"]) + [
        "",
        "## 附4) 宏观背景（只做背景板，不参与打分、不改变仓位）",
        f"- {mac_line}",
        "",
        "## 附5) 巡检清单（工作日 20:00 纯代码巡检，0 token）",
        f"- {pat_line}",
    ] + ([f"- ⚠️ [{x.get('level')}] {x.get('kind')}｜{x.get('code') or ''} "
          f"{x.get('name') or ''}｜{x.get('detail')} → {x.get('action')}"
          for x in (pat.get("alerts") or [])[:12]]
         or ["- 无待处理事项。"]) + [
        "",
        "## 附6) 本次 run 的步骤状态",
    ]
    for s in STEPS:
        v = st["steps"][s["id"]]
        plan_md.append(f"- {s['id']} {ROLES[s['owner']]['name'] if s['owner'] in ROLES else '规则引擎'}："
                       f"{v['status']} {v['note']}")
    _write(os.path.join(TEAM_DIR, "lead_plan.md"), "\n".join(plan_md) + "\n")
    mark(st, "T9", "pending", "骨架已生成，等 Lead LLM 填四段")
    log.append("T9 ✓ Lead 规划骨架 → out/team/lead_plan.md")
    return log


def run(capital: float, ledger: str, pipeline: bool = False,
        monthly: bool = False, dry: bool = False, resume: bool = False,
        t8: bool = False, sketch: bool = True) -> dict:
    st = _state_for(capital, ledger) if resume else new_state(capital, ledger)
    log: list[str] = []
    # 周期结算模式：默认不开计费窗口（想单独量某次任务窗口时才显式 --task-begin）；
    # 本次花费由下一次决策会话第 2 步 `billing.py --task-end` 统一补记。
    _ = billing  # 保留模块引用（T8 里还会用到）

    # ---- T8 专用路径（月报：月度复盘员/Lead 在月底跑；决策日不再评分）
    # 不重复体检、不重算计划：这两步当天已经跑过，重跑会覆盖 out/ 产物且把 nav 又推回上一期。
    if t8:
        info = do_t8(st, capital, ledger, dry=dry)
        log += info["log"]
        if sketch:
            log += do_t9(st, None, ledger, capital, ev=info["ev"], ret=info["ret"],
                         token_cost=info["token_cost"], cost_src=info["cost_src"],
                         run_cost=info.get("run_cost"))
        else:
            log.append("T9 骨架跳过（--score-only：月报只取记分卡，不覆盖本期计划书）")
        if monthly:
            mark(st, "T10", "pending", "月度复盘任务文件已生成")
        st["ended_ms"] = int(_dt.datetime.now().timestamp() * 1000)
        save_state(st)
        return {"state": st, "log": log, "aborted": False, "t8": True}

    # ---- T0 数据体检
    chk = check_data()
    os.makedirs(TEAM_DIR, exist_ok=True)
    _write(os.path.join(config.OUT_DIR, "data_manifest.json"),
           json.dumps(chk, ensure_ascii=False, indent=2) + "\n")
    if dry:
        mark(st, "T0", "skipped", "dry-run 只做体检，不因不新鲜退出")
        log.append(f"T0 数据体检：{'新鲜' if chk['ok'] else '过期/缺失 → ' + ','.join(chk['stale'])}")
    elif not chk["ok"]:
        mark(st, "T0", "failed", f"数据不新鲜：{','.join(chk['stale'])}")
        log.append(f"T0 ✗ 数据不新鲜（{','.join(chk['stale'])}），fail-closed 退出")
        st["ended_ms"] = int(_dt.datetime.now().timestamp() * 1000)
        save_state(st)
        return {"state": st, "log": log, "aborted": True}
    else:
        mark(st, "T0", "ok", "数据新鲜")
        log.append("T0 ✓ 数据体检通过")

    # ---- T1/T5/T6/T7 规则引擎（调用既有管线）
    if pipeline and not dry:
        code, tail = _call("run.py", "--capital", str(capital), "--tier",
                           str(config.PAPER["ledgers"][ledger]["tier"]),
                           "--only-plan")
        if code != 0:
            for sid in ("T1", "T5", "T6", "T7"):
                mark(st, sid, "failed", tail)
            log.append(f"规则引擎失败（exit {code}）：{tail}")
            st["ended_ms"] = int(_dt.datetime.now().timestamp() * 1000)
            save_state(st)
            return {"state": st, "log": log, "aborted": True}
        for sid in ("T1", "T5", "T6", "T7"):
            mark(st, sid, "ok", "run.py --only-plan 完成")
        log.append("T1/T5/T6/T7 ✓ 规则引擎完成（plan 已按本金重算）")
    else:
        for sid in ("T1", "T5", "T6", "T7"):
            mark(st, sid, "ok" if dry else "skipped", "复用既有产物")
        log.append("T1/T5/T6/T7 复用既有产物" if not pipeline else "T1/T5/T6/T7 跳过")

    # ---- T2/T4/T8/T9/T10 任务文件
    want = [s for s in LLM_STEPS if monthly or s != "T10"]
    tasks = write_tasks(st, only=want)
    for sid in ("T2", "T4"):
        if st["steps"][sid]["status"] != "ok":
            mark(st, sid, "pending", "等 LLM 执行（任务文件已生成）")
    if st["steps"]["T3"]["status"] != "ok":
        mark(st, "T3", "pending", "等 T2 的 need_deep")
    log.append("LLM 任务文件：" + str(len(tasks)) + " 个已生成")

    # ---- T8 计分（月报）
    # 2026-11-07 B 类 #5 + 2026-11-21 用户拍板：一周看不出能力，评分改到月报做。
    # pipeline 不碰 T8；月报里单独 `team.py --run-t8 --score-only`（只出记分卡、不覆盖计划书）。
    # dry-run 仍出记分卡骨架（0 计费），方便编排自测。
    info = do_t8(st, capital, ledger, dry=True) if dry else None
    if not info:
        log.append("T8/T9 跳过：pipeline 不自动评分；评分在月报（team.py --run-t8 --score-only）")
        if monthly:
            mark(st, "T10", "pending", "月度复盘任务文件已生成")
        st["ended_ms"] = int(_dt.datetime.now().timestamp() * 1000)
        save_state(st)
        return {"state": st, "log": log, "aborted": False, "t8": False}
    log += info["log"]
    log += do_t9(st, chk, ledger, capital, ev=info["ev"], ret=info["ret"],
                 token_cost=info["token_cost"], cost_src=info["cost_src"],
                 run_cost=info.get("run_cost"))

    if monthly:
        mark(st, "T10", "pending", "月度复盘任务文件已生成")
    st["ended_ms"] = int(_dt.datetime.now().timestamp() * 1000)
    save_state(st)
    return {"state": st, "log": log, "aborted": False, "t8": True}




def do_t8(st: dict, capital: float, ledger: str, dry: bool = False) -> dict:
    """T8 计分结算（只读账本；周期结算模式不在此分段），返回 {log, ev, ret, token_cost, cost_src}。

    为什么独立成函数（2026-11-07 B 类 #5）：`run()` 里的 pipeline 在 weekly.py **之前**
    执行，账本 nav 还是上一期期末值 → `paper_return()` 恒等于上一期的收益，新账本则是
    「无净值 → 0.00%」，评分与奖励被假 0 系统性压低。因此 T8 从 pipeline 拆出来，决策日
    必须按 `run.py → weekly.py → team.py --run-t8 → review.py` 的顺序跑。

    只读：不改账本、不下单；写 `out/scorecard.md` 与 `out/team/state.json` 的 billing 段。
    """
    log: list[str] = []
    # 周期结算模式：不做窗口旁证（旧的 sum_window 已删）；窗口字段只为记分卡兼容留空。
    win = {"since": "", "until": "", "n_calls": 0, "tokens": 0, "cost_cny": 0.0}
    # 周期结算模式下没有任务窗口：T8 不结算（成本留到下一次决策第 2 步 --task-end 按周期记账）
    settle = (billing.task_settle(note=f"team.py T8 分段结算（{ledger}）")
              if (not dry and billing.task_open())
              else {"ok": False, "reason": "周期结算模式：不在此分段"})
    run_cost = float(settle.get("cost_cny") or 0.0) if settle.get("ok") else 0.0

    # 评分口径（User 2026-11-07）：净利润 = 账户累计收益 − token 实花；
    # token 实花取 out/budget.json 该账本累计 spent（不是年度计提、也不是本次窗口）。
    budget_ok = os.path.isfile(billing.BUDGET_JSON)
    bs = billing.budget_state(ledger, capital)
    token_cost = float(bs.get("spent_cny") or 0.0)
    cost_src = "out/budget.json 累计 spent" if budget_ok else "budget.json 缺失，按 0（偏高）"

    ret = paper_return(ledger)
    mdd = plan_mdd()
    if ret is None:
        ret = 0.0
        note = "无账本净值，收益按 0 计（新账本未开始交易）"
    else:
        note = ""
    ev = score.evaluate(ret, token_cost, capital, mdd)
    if not budget_ok and not dry:
        _order = list(getattr(score, "_GRADE_ORDER", ["A", "B+", "B", "B-", "C", "D"]))
        _cap = "B-"
        if ev.get("grade") in _order and _order.index(ev["grade"]) < _order.index(_cap):
            ev["grade_note"] = (f"out/budget.json 缺失，token 实花按 0 算会偏高 → 档位封顶 {_cap}")
            ev["grade"] = _cap
    st["billing"] = {"window": win, "settle": settle,
                     "token_cost_cny": token_cost, "token_spent_cny": token_cost,
                     "run_cost_cny": run_cost, "cost_source": cost_src, "score": ev,
                     "return_source": note or "paper_state"}

    os.makedirs(TEAM_DIR, exist_ok=True)
    card = [
        f"# 记分卡 — {ledger}｜{_dt.date.today()}",
        "",
        f"本金 {capital:,.0f} 元｜账户收益 {ev['gross_profit']:.2f} 元（{ret:.2%}）"
        + (f"（{note}）" if note else ""),
        f"token 实花 {token_cost:.4f} 元（口径 {cost_src}）"
        f"｜净利润 {ev['net_profit']:.2f} 元｜净收益率 {ev['net_rate']:.2%}",
        (f"  本次任务窗口：{settle['window']['since']} → {settle['window']['until']}｜"
         f"{settle['window']['n_calls']} 次调用｜{settle['cost_cny']:.4f} 元"
         if settle.get("ok") else f"  本次窗口未结算：{settle.get('reason')}"),
        "",
        "## 评分（净收益率口径）",
        "```",
        score.format_report(ev),
        "```",
        "",
        "## 12 期分布目标",
        f"`{config.SCORE['dist_target']}` —— 单次评分是运气，分布才是能力。",
        "",
        "## 诚实边界",
        "- 净利润 = 账户累计收益 − token 实花；token 实花取 out/budget.json 该账本累计 spent，不是计提。",
        "- 任务窗口记的是「本次任务花了多少」，只作对账；评分用累计实花，避免每期重复扣。",
        "- 奖励评估期：2027-10-30 起评判；在此之前 reward 一律 0（档位照常给）。",
        "- 结算时点：T8 只在 weekly.py 落完当日 nav 之后跑；pipeline 阶段不再评分（否则净收益恒 0）。",
    ]
    _write(os.path.join(config.OUT_DIR, "scorecard.md"), "\n".join(card) + "\n")
    mark(st, "T8", "ok", f"净收益 {ev['net_rate']:.2%} 评分 {ev['grade']}")
    log.append(f"T8 ✓ 记分卡：账户收益 {ret:.2%}｜token 实花 {token_cost:.4f} 元｜"
               f"净收益 {ev['net_rate']:.2%} → {ev['grade']}"
               + ("（奖励：评估期未到）" if ev.get("reward_pending") else ""))
    return {"log": log, "ev": ev, "ret": ret, "token_cost": token_cost,
            "cost_src": cost_src, "note": note, "run_cost": run_cost}


def status() -> dict:
    st = load_state()
    if not st:
        return {"state": None, "msg": "没有运行记录（先跑 team.py --dry-run）"}
    rows = []
    for s in STEPS:
        v = st["steps"].get(s["id"], {})
        owner = ROLES[s["owner"]]["name"] if s["owner"] in ROLES else "规则引擎"
        rows.append({"id": s["id"], "owner": owner, "llm": s.get("llm", False),
                     "status": v.get("status"), "at": v.get("at"), "note": v.get("note")})
    return {"state": st, "steps": rows,
            "billing": st.get("billing")}


def main():
    ap = argparse.ArgumentParser(description="决策/月报编排（Lead + fresh 子代理 + 规则引擎，T0–T10）")
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--ledger", default="50k", choices=sorted(config.PAPER["ledgers"].keys()))
    ap.add_argument("--dry-run", action="store_true", help="只走编排与产物，不调用管线、不写账")
    ap.add_argument("--run", action="store_true", help="真实运行")
    ap.add_argument("--pipeline", action="store_true", help="调用 run.py --only-plan 重算计划")
    ap.add_argument("--run-t8", action="store_true",
                    help="只跑 T8 评分（月报用；决策日不再评分）")
    ap.add_argument("--score-only", action="store_true",
                    help="配合 --run-t8：只出记分卡，不生成/覆盖 T9 计划书骨架（月报默认）")
    ap.add_argument("--monthly", action="store_true", help="生成 T10 复盘任务")
    ap.add_argument("--stage", metavar="T2|T3|T4|T8|T9",
                    help="生成某一步的任务书（Lead 拿去派给执行者：subagent/队友）")
    ap.add_argument("--ingest", metavar="T2|T3|T4|T8|T9",
                    help="校验执行者产物 → 标记完成 → 推进下一步")
    ap.add_argument("--status", action="store_true", help="看上次运行状态")
    ap.add_argument("--contract", action="store_true", help="打印交接契约表")
    ap.add_argument("--budget", action="store_true", help="打印预算分配")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.contract:
        for c in handoff_contract():
            print(f"{c['name']:<12} {c['path']:<28} {_who(c['producer'])}")
            print(f"             列: {c['cols']}")
        return 0

    if a.budget:
        for b in budget_plan(a.capital, a.ledger):
            print(f"  {b['name']:<10} {b['kind']:<7} {b['budget_pct']:.0%}  {b['budget_cny']:.2f} 元/年")
        return 0

    if a.stage:
        r = stage(a.stage, a.capital, a.ledger)
        if a.json:
            print(json.dumps({k: v for k, v in r.items() if k != "text"},
                             ensure_ascii=False, indent=2))
        elif r.get("ok"):
            print("任务书：%s" % r["task_file"])
            print("-" * 64)
            print(r["text"])
        else:
            print("失败：%s" % r.get("reason"))
        return 0 if r.get("ok") else 1

    if a.ingest:
        r = ingest(a.ingest, a.capital, a.ledger, dry=a.dry_run)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif r.get("ok"):
            for line in (r.get("log") or []):
                print(line)
            print("%s ✓ 已验收%s" % (r["sid"], (" → 下一步 " + r["next"]) if r.get("next") else ""))
            if r.get("task_file"):
                print("  任务书：%s" % r["task_file"])
            if r.get("reason"):
                print("  %s" % r["reason"])
        else:
            print("未通过：%s" % r.get("reason"))
        return 0 if r.get("ok") else 1

    if a.status or not (a.dry_run or a.run or a.run_t8):
        s = status()
        if not s["state"]:
            print(s["msg"])
            return 0
        st = s["state"]
        print(f"run_id {st['run_id']}｜{st['ledger']}｜本金 {st['capital']:,.0f}｜启动 {st['started_at']}")
        for r in s["steps"]:
            llm = "LLM" if r["llm"] else "代码"
            print(f"  {r['id']:<4} {r['owner']:<8} {llm:<4} {r['status']:<8} {r['note']}")
        pend = [r["id"] for r in s["steps"] if r["status"] == "pending"]
        if pend:
            print("  待执行：%s → `python team.py --stage %s` 生成任务书"
                  % (",".join(pend), pend[0]))
        b = s.get("billing") or {}
        if b.get("score"):
            ev = b["score"]
            print(f"  账户收益 {ev['gross_profit']:.2f} 元（{ev['total_return']:.2%}）"
                  f"｜token 实花 {ev['token_cost_cny']:.4f} 元（{b.get('cost_source', '?')}）")
            print(f"  净利润 {ev['net_profit']:.2f} 元｜净收益率 {ev['net_rate']:.2%}"
                  f" → 评分 {ev['grade']}"
                  + ("｜奖励：评估期未到（2027-10-30 起评判）"
                     if ev.get("reward_pending") else "｜奖励已计提"))
        return 0

    # --run-t8：月报的独立评分步骤（决策日不再评分）。沿用上一次 run 的步骤状态，
    # 避免把 T0–T7 的完成记录清空（账本与记分卡都依赖步骤状态的连续性）。
    res = run(a.capital, a.ledger, pipeline=a.pipeline, monthly=a.monthly,
              dry=a.dry_run, resume=(a.run_t8 and load_state() is not None),
              t8=a.run_t8, sketch=not a.score_only)
    if a.json:
        print(json.dumps({"log": res["log"], "aborted": res["aborted"]},
                         ensure_ascii=False, indent=2))
    else:
        for line in res["log"]:
            print(line)
        if not res["aborted"]:
            print(f"\n状态文件：{STATE_FILE}")
            print(f"任务文件：{TASK_DIR}")
    return 0 if not res["aborted"] else 1


if __name__ == "__main__":
    sys.exit(main())
