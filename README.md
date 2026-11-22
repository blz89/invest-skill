# invest（投资）

A 股三阶段策略 + 小资金执行层 + 周度模拟盘 + 决策/复盘编排（脚本 + fresh 子代理）：
**筛选 → 精确判断 → 执行计划 → 整手落地 → 模拟盘验证 → 记分卡复盘**。

规则阈值全部集中在 `scripts/config.py`，同一份数据必得同一个结论，每个数字都能在 `out/provenance*.json` 里追溯来源。

> **本金：5 万元（本部署单账本）**。`config.CAPITAL["min"]` 等只是防极端值的代码兜底，不是推荐区间。
>
> 不构成投资建议；不接券商下单；历史数据推不出未来。

**不同读者各看一份，不互相复制**（每类信息只有一个出处）：

| 你想要的 | 看这里 |
|---|---|
| 怎么跑、产物在哪、出事怎么办 | 本文件 |
| 执行手册：硬约束、阈值来历、字段陷阱、汇报格式 | `SKILL.md` |
| 设计说明：阈值表、回放与显著性证据、已知限制、设计依据 | `references/engine-notes.md` |
| 数据流 / 公式 / 护栏 / 效率（一页地图） | `references/logic-map.md` |
| 数据源：选源表、限制、缺口、需要用户提供什么 | `references/data-sources.md` |
| 事件面：采集与初判流程、R/C 打分、校准证据 | `references/news-rules.md` |
| 编排框架：角色/流程/契约/评分/计费/网格/反思 | `references/team-framework.md` |

---

## 安装

本仓库是一个 **DSH（DeepSeek Harness）技能**，默认安装到 DSH 的技能目录（Windows `%USERPROFILE%\.dsh\skills\invest`，macOS/Linux `~/.dsh/skills/invest`）：

```powershell
git clone https://github.com/blz89/invest-skill "$env:USERPROFILE\.dsh\skills\invest"
cd "$env:USERPROFILE\.dsh\skills\invest"

# Python 3.10+（开发机用 3.13）；取数接口层的依赖装在技能自带的 venv 里
python -m venv stock\.venv
stock\.venv\Scripts\python.exe -m pip install -r requirements.txt
stock\.venv\Scripts\python.exe -m pip install -e .\stock
```

下文用 **`<本技能目录>`** 指代这个安装目录。不装 DSH 也能直接跑全部脚本（只是少了 20:00 巡检 / 周六决策那两条会话编排）；取数用的环境变量（`A_SHARE_WORKDIR`、`FUYAO_API_KEY` 等）见下文「环境」一节。

---

## 快速开始

> **本部署只跑 5 万单账本**：`scripts/config.py` 的 `PAPER["ledgers"]` 只有 `50k`，三条定时任务（巡检 / 决策 / 月报复盘；1 月 1 日月报任务自动切年报）与决策日步骤均已单账本化。要加账本先让用户拍板并改配置。
>
> **本部署取数与决策分工**：**基础数据统一由 stock 在本机跑**——交易日（周一至周五；法定假日与调休补班的周末不开市）**19:00** Windows 计划任务先 `python -m stock prefetch`（全市场基础数据唯一取数点，共享缓存 `<本技能目录>\stock\data\shared\cache`，0 token）→ invest `run.py --capital 50000`（**不带 --refresh**，只读热缓存）产出 `out/` 全套；**DSH 负责账本计算与记录**（交易日 20:00 会话跑 `patrol.py` 结算账本并出简报；周六 20:00 决策）。**周六决策日不跑全量取数管线**，直接用周一~周五已取的数据（周末行情不变；只有周末新增公告/新闻是新的，由采集端脚本采集；`weekly.py` 撮合/场外净值的小额查询除外）。

```powershell
$env:PYTHONIOENCODING='utf-8'; $env:TQDM_DISABLE='1'
cd <本技能目录>\scripts

$py = '<本技能目录>\stock\.venv\Scripts\python.exe'   # 本机实机可用

# 一键三阶段（首次全量约 5–8 分钟，之后走缓存几十秒）
& $py run.py --capital 50000             # 5 万（默认）
& $py run.py --capital 50000 --tier B    # 档位 A/B/C（B 为默认档）
& $py run.py --skip-screen --capital 50000   # 复用上次筛选，直接从判断开始
& $py run.py --no-otc                    # 跳过 R2 场外债券基金池（默认包含）
& $py run.py --capital 50000 --refresh   # 手工补数/调试（正常取数=交易日 19:00 的 stock prefetch）；决策日不跑取数
& $py otc.py --limit 3                   # 单独跑场外池（调试用）

# 阶段四：周度模拟盘（每周六 20:00 决策，用交易日 19:00 本机统一取数后数据、决策日不取数；卖出按下一交易日开盘价、买入按限价条件单；本部署单账本 50k）
& $py decide.py                          # 手工全量快捷方式（含取数；决策日不跑）：体检 → 50k 账本 → 报告；完整 10 步见 SKILL §8.1.1
& $py decide.py --refresh                # 手工全量重取（工作日补数/调试；决策日不取数）
& $py decide.py --reuse                  # 当天已跑过管线：只出周报（约 10 秒）
& $py decide.py --init                   # 建缺失的账本（不动已有账本）
& $py weekly.py --ledger 50k --init --tier B                  # 5 万首次建账
& $py weekly.py --ledger 50k --refresh-net      # 决策日收尾：刷新周报净利润（只读，不取数、不动账本；不刷新档位）
& $py paper_exec.py --ledger 50k --date <交易日>     # 补跑：按该日撮合（卖出开盘价 / 买入限价触价）+ 结算
& $py review.py --ledger all             # 记分卡：样本不足 / 未达标 / 暂达标
& $py report_html.py --ledger all        # ★ HTML 周报（分析 + 购买策略 + 未来计划）
& $py monthly.py --month 2026-10         # ★ 月报（md + html）
& $py monthly.py --month 2026-12 --annual   # ★ 年报（每年 1 月 1 日）：年度总结 + 年度结算（奖励划出、本金重置、总收益链式累计；重渲染加 --no-settle）
& $py score_panel.py --archive --date 2026-11-20   # 选股评分面板：归档本期四维评分（决策日 weekly 会自动做，可手动补）
& $py score_panel.py --health --year 2026          # 选股评分体检：分维度 IC / 五分组差 / 命中率（年报自动带出；面板 <26 周只写样本不足）
& $py doctor.py --ledger all             # 体检（只读）：GO 才继续跑决策
& $py rollback.py --ledger 50k --list    # 账本损坏时：先 --list → --dry-run → 再执行

# 编排（详见 references/team-framework.md）
& $py team.py --run --pipeline --capital 50000 --ledger 50k   # 代码步骤 T0–T7 + 任务书（不评分）
& $py team.py --run-t8 --score-only --capital 50000 --ledger 50k  # 月报：T8 记分卡（决策日不评分，不覆盖计划书）
& $py team.py --budget --capital 50000 --ledger 50k           # 预算分配
& $py team.py --contract                                      # 六条交接契约
# 本部署执行者：Lead 主会话 + fresh 子代理（S1 研究 / S2 深取 / S3 复核 / S4 巡检；月报 T10 复盘）；Lead 跑规则引擎/行情单与 T9，验收用 --ingest
& $py score.py --capital 50000 --return 0.035 --token-cost 1.2 --mdd -0.08
& $py billing.py --status
& $py grid.py --signal                                        # 网格只算信号，不下单

# 历史回放与可复现性（回测只覆盖 ETF 承接层，个股层缺 PIT 数据）
& $py backtest.py --start 2020-01-01 --compare                # 规则变体对比
& $py determinism.py --capital 50000                         # 同缓存跑两遍逐字比对
```

跑完先看 `out/stage3_plan.md`（给人看的执行计划），再看 `out/stage2_report.md`（每只股票为什么买/不买）。

## 目录结构

```
invest/
├── SKILL.md            AI 执行手册（怎么跑、硬约束、字段陷阱、汇报格式）
├── README.md           本文件（入口：命令 / 产物 / 运维）
├── requirements.txt    依赖
├── scripts/
│   ├── config.py       全部阈值集中在此；末尾 skilldata.install() 把本技能 config
│   │                   注入 stock 数据层（codes/datasource/fuyao 本目录已无副本）
│   ├── metrics.py      纯计算层：原始数据 → 指标，不做判断
│   ├── screen.py       阶段一：筛选（漏斗）
│   ├── judge.py        阶段二：精确判断（否决 + 打分 + 定价 + 论点）
│   ├── etf.py          阶段一补充：R3 权益 ETF 筛选（承接个股买不起的预算）
│   ├── wealth.py       阶段一补充：R2 理财层筛选（场内债券 ETF）
│   ├── otc.py          阶段一补充：R2 场外债券基金池（场内一手买不起时兜底；与场内互斥）
│   ├── risk.py         风险等级层：R1–R5 定级 + 组合评审闸门
│   ├── capital.py      小资金执行层：整手 · 交易费用 · 档位 · 现金增值
│   ├── plan.py         阶段三：执行计划（仓位 + 分批 + 止损 + ETF 层 + 场外 R2 + 现金层）
│   ├── run.py          一键跑完三阶段
│   ├── paper.py        阶段四：模拟盘账本（撮合/费用/止损/净值/绩效）
│   ├── otc_book.py     场外影子账本：场外腿成交价/份额/市值/费用/到期/结算收益（paper 与 weekly 共用）
│   ├── navstats.py     账本表现：用巡检日度净值算区间收益/波动/回撤/胜率/超额（周报 md 与 HTML 共用）
│   ├── breakdown.py    收益拆解：权益 / 场外 R2 / 现金三层（周报、月报共用）
│   ├── weekly.py       阶段四：周度决策（目标组合 → 差额挂单 + 周报）
│   ├── paper_exec.py   阶段四：执行日工具（撮合挂单、结算、补挂止损单）
│   ├── review.py       阶段四：记分卡（12 周口径，只读账本）
│   ├── doctor.py       阶段四：系统体检（只读，GO/NO-GO）
│   ├── rollback.py     阶段四：账本回滚（可逆）
│   ├── decide.py       决策日快捷方式（无研究层）：体检 → 50k 账本 → 记分卡；正式决策日按 SKILL §8.1.1 十步
│   ├── backtest.py     历史回放（ETF 承接层；--pool pit 用 PIT 全集，--bootstrap 显著性）
│   ├── etf_pit.py      PIT 全集构建 → out/etf_universe.csv
│   ├── snapshot.py     L1 交接契约：个股全池快照 out/data_snapshot.csv（本地拼装，不取数）
│   ├── report_html.py  HTML 报告渲染（周报 / 月报 / 年报）
│   ├── monthly.py      月度复盘 / 年报（统计 + 上月策略复盘 + 技能优化 A 类当场执行；--annual 年度结算）
│   ├── score_panel.py  选股评分面板：每周归档 judge 评分 + 年报体检（IC/分组差/命中率，只记录不改评分）
│   ├── team.py         决策/月报编排（T 步骤、任务书与验收；判断交 fresh 子代理）
│   ├── score.py        评分与奖励（A/B+/B/B-/C/D，12 期分布；奖励 2027-10-30 起评判）
│   ├── billing.py      token 计费与预算（读 DSH 计费账本）
│   ├── grid.py         低买高卖网格（回测未通过，enabled=False，只算信号）
│   ├── xcheck.py       同花顺 vs 我方 交叉校验 → out/xcheck.md
│   ├── cninfo.py       事件面：巨潮公告检索 + 风险扫描（免费、无 key）
│   ├── pit.py          PIT：真实披露日 / 历史成分 / TTM PE（依赖 baostock）
│   ├── industry.py     行业层：同花顺行业标签 + 行业集中度
│   ├── sentiment.py    情绪层：涨停/跌停/炸板/龙虎榜/异动 → 风险提示（不产生信号）
│   ├── macro.py        宏观层：LPR/SHIBOR/CPI/PPI/PMI/M1-M2 → 背景板
│   ├── fund.py         基金层：场外池的备用源（复权净值复算 + 抽检，不写池子）
│   ├── news.py         事件面：公告/新闻/异动 → R/C 双打分 + 五档处置（决策日跑）
│   ├── news_calib.py   新闻评分校准器（12 期历史回放，只记录不干预）
│   ├── rates.py        现金层实测：货基每日、逆回购每周五→ config.cash_rates()
│   ├── patrol.py       ★ 账本巡检（交易日 20:00 DSH 会话，纯代码）：算账本/挂单/止损/利率/记录表；取数由 19:00 Windows 任务（stock prefetch）统一完成
│   ├── housekeeping.py 巡检记账与清场：daily_log 一天一行 + 缓存滚动清理 + 年报底稿
│   ├── determinism.py  可复现性验证（同缓存跑两遍比对）
│   ├── _smoke.py       数据层冒烟测试（对茅台逐项核对公开数据）
│   ├── selftest_all.py     ★ 一次跑完全部自检套件并汇总（CI/定时任务前跑这个）
│   ├── _selftest.py            小资金算术/手数/费用/档位
│   ├── _selftest_plan.py       个股排名篮子/权重上限/回滚/新闻闸
│   ├── _selftest_paper.py      撮合/止损/结算/现金层计息/回滚/验收口径
│   ├── _selftest_risk.py       R1–R5 定级与组合评审
│   ├── _selftest_otc.py        场外基金池漏斗/份额去重/打分
│   ├── _selftest_team.py       评分/计费/网格/编排/分阶段执行
│   ├── _selftest_patrol.py     巡检触发条件/去重/账本快照/归因
│   ├── _selftest_layers.py     行业/情绪/宏观三层
│   ├── _selftest_fund.py       基金层复算与交叉校验
│   ├── _selftest_doctor.py     体检逻辑
│   ├── _selftest_fuyao.py      同花顺客户端
│   ├── _selftest_cninfo.py     巨潮公告分类/装配/失败语义
│   ├── _selftest_pit.py        PIT as-of 过滤/TTM PE
│   ├── _selftest_snapshot.py   L1 快照拼装/失败路径
│   ├── _selftest_news.py       事件面评分/去重/分池
│   ├── _selftest_otc_book.py    场外影子账：市值/费用/到期/告警/结算收益
│   ├── _selftest_navstats.py    日度净值表现：收益/回撤/胜率/基准超额
│   ├── _selftest_monthly.py    月报：策略复盘/技能优化/A 类清单
│   ├── _selftest_score_panel.py 选股评分面板：归档/前向超额/年报体检
│   ├── _selftest_breakdown.py  收益拆解：三层口径/渲染/自洽校验
│   └── _selftest_housekeeping.py daily_log 记账/跨年归档/保留策略/安全阀
└── references/
    ├── engine-notes.md   设计说明：阈值表、验证记录、已知限制、设计依据
    ├── logic-map.md      数据分析/计算逻辑：数据流、公式、护栏、效率
    ├── data-sources.md   信息源全景：选源表、限制、缺口、需要用户提供什么
    ├── news-rules.md     事件面：采集与初判流程/六步/R-C 表/校准证据
    └── team-framework.md 编排框架唯一说明
```

自测全跑：`& $py selftest_all.py`（约 2–3 分钟，汇总成一张表；`--fast` 跳过要联网的 3 套）。
共 23 套自检（20 套离线 + 3 套联网），全部 exit 0 才算健康；断言项数以 `selftest_all.py` 汇总行为准，文档不写死。

## 数据与产出落在哪

代码目录只读也能跑。缓存与产出默认落在 `<本技能目录>\stock\data\invest/`（要换位置：`$env:A_SHARE_WORKDIR='D:\data\invest'`）：

```
├── cache/    按日期分目录的原始数据缓存（沪深300 全量）
├── out/      stage1_*.csv|json（含 stage1_wealth.*：R2 债券 ETF 池；stage1_otc.*：R2 场外债券基金池）
│             data_snapshot.csv|_meta.json（L1 个股全池快照，snapshot.py 本地拼装，不取数）
│             stage2_*.csv|json|md、stage3_*.md|json|csv、provenance*.json|provenance_otc.json
│             scorecard.md、billing.jsonl、budget.json
│   ├── paper/    weekly_{日期}.md、orders_{日期}.csv、report_{日期}.html、
│   │             monthly_{YYYY-MM}.md|html、doctor.md（体检结论，只留最新一份）
│   ├── team/     state.json（编排状态）、contract.md（交接契约）、lead_plan.md（Lead 汇总）
│   ├── deep/     T3 深取产物 <code>.json（研究员 T4 的输入）
│   ├── agent_tasks/  T2/T4/T8/T9/T10 的任务书（LLM 步骤的输入；T8 仅月报）
│   └── stage1b_research.csv、stage1b_tags.csv、T8_*.md、T9_*.md、T10_review.md（LLM 步骤的产出）
├── patrol_latest.json     工作日巡检结果（DSH 20:00 会话跑 patrol.py；最新一份；按天存 out/patrol_*.md）
├── daily_log.md          巡检记录表：一天一行（13 列：资产/累计/回撤/仓位/当日盈亏/token 花费/环境/告警），当年在主文件、往年归档 `_archive/`（永久保留；年报底稿 `housekeeping.py --year`）
├── cache/                数据缓存（同一 key 留最近 2 天；同花顺与公告的按日快照同此）
└── paper/
    └── state_50k.json    5 万模拟盘账本（在技能目录 ledger/，由 DSH 巡检计算；现金/持仓/成交/挂单/净值序列），唯一事实来源
```

## 环境

- Python 3.13 + `<本技能目录>\stock 包（取数接口层）` + `pandas`（`pip install -r requirements.txt`）
- 本机已验证路径：`<本技能目录>\stock\.venv\Scripts\python.exe`（Python 3.13.16 + pandas 3.1.0 + stock 取数接口层）
- 必设 `PYTHONIOENCODING=utf-8`、`TQDM_DISABLE=1`
- 东方财富行情接口（`*_em` 的股票/行情系）在本机被网络层拦截，引擎走新浪 / 中证指数 / 百度；
  场外基金与费率系接口（`fund_*_em`）实测可用，`otc.py` 依赖它们
- **可选**第二数据源：同花顺金融数据 API（唯一实现 `<本技能目录>\stock\stock\sources\fuyao.py`，技能内 `import fuyao` 仍可用）。设 `FUYAO_API_KEY`，或把 key 一行写进
  `$WORK_DIR/fuyao.key`（**不要放进技能目录**）。没有 key 时全链路照旧走取数接口层，零依赖、零回归。
  体检命令 `python xcheck.py` → `out/xcheck.md`（把收益/回撤/PE/ROE 与同花顺逐项对照）。
  它能补什么、**不能**补什么（PIT 财报、历史估值分位、资金流都取不到），见 `SKILL.md` §5.1
- **免费无 key 的两个新源**：
  - 巨潮公告 `python cninfo.py --code 601058 --days 365`（事件面风险扫描 `--scan 池.csv --soft`）
  - baostock `python pit.py --code 600519 --asof 2026-10-28`（**唯一有真实披露日**的源）
- **三层扩展（都在主流程里，不是摆设）**：
  - 行业层 `python industry.py --refresh`（或 `--check` 全量对账；`--limit` 只影响 `--refresh` 的抓取条数，check 分支不读它）
  - 情绪层 `python sentiment.py --codes 601058,600519`
  - 宏观层 `python macro.py`（每月一次足够）
  - 基金层 `python fund.py --health --n 6`（月度抽检场外池，冲突了先查主源，别换口径）
  - 三层都进 T0 体检；任一缺失只提示不阻断；**情绪层不产生买卖信号**，宏观层不参与打分
  - 安装：`baostock==0.9.4` 已装进 `<本技能目录>\stock\.venv`；包与数据落在独立数据目录，技能目录不放第三方文件
  - 选源规则、已知限制、还缺什么：`references/data-sources.md`
- 某个数字怎么算出来的：`references/logic-map.md`

## 五层结构

| 层 | 做什么 | 在哪看阈值 |
|---|---|---|
| 一 · 漏斗 | 全市场 → 候选个股（`top_n` 为上限）+ 权益 ETF + 债券 ETF + 场外债基 | `SKILL.md` §1、`config.py` |
| 二 · 判断 | 四道否决 → 四维打分 → 买入价/目标价/止损价/可证伪条件 | `SKILL.md` 阶段二 |
| 三 · 执行 | 市场状态定仓位上限 → 排名配仓（前 4–6 只随档位，观察区 0 权重不建仓）→ 三批建仓（单批 <5,000 元按手数合并；ETF 单批）→ 整手化 + ETF/场外承接 + 现金层 | `SKILL.md` 阶段三 |
| 四 · 模拟盘 | 每周六 20:00 决策（用交易日 19:00 本机统一取数后数据，决策日不取数、不评分；交易日 19:00 取数 + 20:00 巡检（休市日自动跳过）；巡检提示词底稿 `prompts/daily_patrol.md`，每周可调附加任务）→ 卖出按下一交易日开盘价、买入按限价条件单 → 净值/胜率/超额 → 周报只给净利润；**评分/档位/奖励只在月报** | `SKILL.md` §8 |
| 五 · 编排 | T0–T10：代码做机械、LLM 只做一次性判断；评分 + 计费 + 复盘 | `references/team-framework.md` |

## 利润与奖励口径

- **净利润 = 账户累计收益（元）− token 实花（元）**；**净收益率 = 净利润 ÷ 本金**。
  token 实花取 `out/budget.json` 该账本累计 `spent_cny`（真实扣费），不用年度计提。
  周报不展开运行时费用（核心表只给净利润/净收益率）；月报、记分卡、`score.py`、T8/T9 与汇报必须同时给「账户收益 / token 实花 / 净利润 / 净收益率」四项。
- **周报固定结构**：一、收益与消耗（核心表）→ 二、本期操作 → 三、策略与持仓 → 四、下期观察点 → 五、边界 → 附录：明细（账本表现 / 账户账本 / token 明细 / 与上期变化 / 巡检汇总 / 收益拆解三层 / 场外影子账，按需看）。收益与消耗在前、策略在后，同一数字不在正文重复。
- **奖励评估期**：基准日 2026-10-28 + 12 个月 = **2027-10-30**；此前 reward 一律 0，文案「评估期未到（2027-10-30 起评判）」。档位阈值不变，输入为净收益率。
- **计费只认实扣（周期结算）**：小鲸鱼插件按余额下降记账户口径、按 token 逐轮记 `events[]`；技能直接取 `events[].cost` 累计，不自己按单价核算（单价表由插件维护）。
计费只认实扣（周期结算）：结算时读小鲸鱼记账账本 `events[]`（`$DSH_HOME/.dshw-usage.json`）在「**上次结算终点 → 现在**」区间内的 `cost` 累计，`billing.py --task-end` 结算后把终点写进 `out/team/billing_cycle.json`，不重复计；不需要开窗（`--task-begin` 仅在想单独量某次任务窗口时用），区间内所有本机消耗都计入（记录没有会话 id），账户余额观测只作参考；**结算发生在下一次决策会话第 2 步**（补记上一周期）。

## 出事怎么办（运维速查）

周六 20:00 决策、交易日 19:00 取数 + 20:00 巡检（休市日自动跳过）都是无人值守运行（决策日不取数），出问题时先对号入座，**不要手工改 `paper/*.json`**。

| 症状 | 先做什么 | 命令 |
|---|---|---|
| 这一期没跑 / 晚了几个小时 | 决策日不取数：确认工作日数据已是最近交易日，再补跑一次（幂等，不会重复下单） | `weekly.py --ledger 50k --reuse` + `team.py --stage T9` 填四段 → `--ingest T9`（数据旧就先在工作日补跑取数） |
| 报告和上期一模一样 / 数据日期不是最近交易日 | 说明**本机工作日取数失败/没更新**（决策日不重取）；先在工作日补跑本机取数，再重新决策 | `python -m stock prefetch`（取数）→ `run.py --capital 50000`（工作日本机任务同款） |
| 体检 NO-GO | 按 ERROR 逐条修，不要绕过，不要手工改账本 | `doctor.py --ledger all` |
| 账本被改坏 / 疑似多下单 | 先预览再回滚，回滚后立刻体检 | `rollback.py --ledger 50k --list` → `--dry-run` → 执行 |
| 想确认某天会成交什么 | dry-run 不写账本 | `paper_exec.py --ledger 50k --date <交易日> --dry-run` |
| 账本丢了 / 换机器 / 清过目录 | 先建账再决策（只新建缺失的） | `decide.py --init` |
| 只想跑决策、不跑研究层 | 决策日按 10 步手工串（数据已就绪，只做事件面+计划+周报）；`decide.py` 含取数，只用于工作日补跑 | `weekly.py --ledger 50k --reuse` |
| 报「out/ 是另一个本金的计划」 | 本机工作日取数管线要按本账本本金重跑（DSH 决策日不补取） | 工作日跑 `python -m stock prefetch` → `run.py --capital 50000` |
| 数据接口大面积失败 | 不许编数据；报告失败比例，稍后重跑 | 看 `out/stage1_funnel.json` 的「取数成功/股票池」 |

## 边界

仅中国大陆 A 股 + 国内 ETF + 场外债券基金，本部署本金 5 万元。不做择时；回测只覆盖 ETF 承接层
（个股层缺 point-in-time 数据）；不覆盖港美股/期货/加密、不接券商下单、不保证收益。
佣金按**万 1、最低 5 元/笔**（`config.ACCOUNT` 是唯一出处，用户交割单口径）；下单窗口 15:00–15:30；卖出按下一交易日开盘价成交，买入一律限价条件单（跌到限价才买、10 个交易日不成交自动失效）。R2 场外债券基金腿由**影子账**记账（`otc_book.py`：按提交日的下一交易日累计净值成交、T+1 确认，市值 = 投入 × 累计净值比，
赎回费按持有天数取实测档；净值缺失时挂单保持 pending）；场外基金没有 point-in-time 数据、不做历史回放。
评分与奖励是**记账口径**，不是收益承诺；LLM 只做一次性判断（fresh 子代理），不产生新信息，价值在动机分离与并行。角色与执行流程见 `references/team-framework.md` §1、§2.1。

## 版本库

公开仓库：<https://github.com/blz89/invest-skill>

当前版本 **v1.1.0**（tag `v1.1.0`）；变更历史见 `git log`。
