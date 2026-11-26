# invest（投资）

> **公开脱敏版**：本仓库为私有部署导出的公开演示版本（版本 v1.3.0）。运行时数据（账本 `ledger/`、缓存 `stock/data/`、产出 `out/`、token 计费）与个人路径均未包含；文中形如 `D:\skills\invest` 的路径是占位路径，请替换为自己的实际目录。仅供学习研究，不构成投资建议。


A 股三阶段策略 + 小资金执行层 + 周度模拟盘 + 决策/复盘编排：
**筛选 → 精确判断 → 执行计划 → 整手落地 → 模拟盘验证 → 记分卡复盘**。

规则阈值全部集中在 `scripts/config.py`，同一份数据必得同一结论，每个数字都能在 `out/provenance*.json` 里追溯来源。

> **本金：5 万元（单账本）**。`config.CAPITAL["min"]` 等只是防极端值的兜底，不是推荐区间。
> 不构成投资建议；不接券商下单；历史数据推不出未来。

**各看一份，不互相复制**（每类信息只有一个出处）：本文件＝怎么跑/产物在哪/出事怎么办；`SKILL.md`＝硬约束/执行步骤与指针；`references/thresholds.md`＝阈值与小资金参数；`references/report-formats.md`＝周报/HTML/月报顺序；`references/risk-review.md`＝R1–R5 与四条闸门；`references/orchestration.md`＝编排摘要；`references/engine-notes.md`＝阈值表/回放与显著性证据/已知限制/设计依据；`references/logic-map.md`＝数据流/公式/护栏/效率；`references/data-sources.md`＝选源表/限制/缺口/需要用户提供什么；`references/news-rules.md`＝采集与初判流程/R/C 打分/校准证据；`references/data-layer.md`＝数据层排错/缓存/留存/现金层口径；`references/paper-ops.md`＝paper 执行细节（命令/账本口径/回滚/新鲜度/排名篮子）；`references/repo-analysis.md`＝逆回购盈亏分析框架；`references/team-framework.md`＝角色/流程/契约/评分/计费/网格/反思。

---

---

## 安装

本仓库是一个 **DSH（DeepSeek Harness）技能**，默认安装到 DSH 的技能目录（Windows `%USERPROFILE%\.dsh\skills\invest`，macOS/Linux `~/.dsh/skills/invest`）。

### 一键安装（推荐；agent 可直接执行）

Windows（PowerShell 5.1+）：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/blz89/invest-skill/main/install.ps1)))
```

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/blz89/invest-skill/main/install.sh | bash
```

脚本一次做完：克隆/更新仓库 → 建 `stock/.venv` → 装依赖（`requirements.txt` + `pip install -e stock` + `pypdf`）→ 跑 `scripts/selftest_all.py --fast` 离线自检（失败返回非 0，可直接 fail-closed）。

可选项：Windows 用参数 `-Dir <目录>` / `-Ref <分支或 tag>` / `-SkipTest`；macOS/Linux 用环境变量 `INVEST_SKILL_DIR` / `INVEST_SKILL_REF` / `INVEST_SKILL_SKIP_TEST=1`。

### 手动安装

```powershell
git clone https://github.com/blz89/invest-skill "$env:USERPROFILE\.dsh\skills\invest"
cd "$env:USERPROFILE\.dsh\skills\invest"

# Python 3.10+（开发机用 3.13）；取数接口层的依赖装在技能自带的 venv 里
python -m venv stock\.venv
stock\.venv\Scripts\python.exe -m pip install -r requirements.txt
stock\.venv\Scripts\python.exe -m pip install -e .\stock
stock\.venv\Scripts\python.exe -m pip install pypdf
```

下文用 **`<本技能目录>`** 指代这个安装目录。不装 DSH 也能直接跑全部脚本（只是少了 20:00 巡检 / 周三/周六 20:30 双决策会话）；取数用的环境变量（`A_SHARE_WORKDIR`、`FUYAO_API_KEY` 等）见下文「环境」一节。

---

---

## 快速开始

> **只跑 5 万单账本**：`PAPER["ledgers"]` 只有 `50k`；四条定时任务（交易日 19:00 取数为 Windows 计划任务；巡检/决策/月报复盘为 DSH 调度，1 月 1 日自动切年报）与决策日步骤均为单账本；加账本先让用户拍板并改配置。
>
> **取数与决策分工**：数据统一由 stock 在本机取——交易日（周一至周五；法定假日与调休补班的周末不开市）19:00 Windows 计划任务跑 `python -m stock prefetch`（全市场唯一取数点，共享缓存 `D:\skills\invest\stock\data\shared\cache`，0 token）→ `run.py --capital 50000`（**不带 --refresh**，只读热缓存）产出 `out/`；DSH 只做账本计算与记录（20:00 `patrol.py` 结算出简报；周三、周六 20:30 决策）。**决策日不跑全量取数**，直接用交易日 19:00 已取好的数据（周三决策用当日收盘；周六决策时周末无新行情，只有新增公告/新闻需采集；`weekly.py` 撮合/场外净值的小额查询除外）。

```powershell
$env:PYTHONIOENCODING='utf-8'; $env:TQDM_DISABLE='1'
cd <本技能目录>\scripts

$py = 'D:\skills\invest\stock\.venv\Scripts\python.exe'   # 本机实机可用

# 一键三阶段（首次全量约 5–8 分钟，之后走缓存几十秒）
& $py run.py --capital 50000             # 5 万（默认）
& $py run.py --capital 50000 --tier B    # 档位 A/B/C
& $py run.py --skip-screen --capital 50000   # 复用上次筛选
& $py run.py --no-otc                    # 跳过 R2 场外池
& $py run.py --capital 50000 --refresh   # 手工补数/调试；决策日不跑取数
& $py otc.py --limit 3                   # 单独跑场外池（调试用）

# 阶段四：周度模拟盘（每周三、周六 20:30 双决策，用交易日 19:00 本机统一取数后数据、决策日不取数；卖出按下一交易日开盘价、买入按限价条件单；本部署单账本 50k）
& $py decide.py                          # 手工全量（含取数；决策日不跑）：体检 → 50k 账本 → 报告；10 步见 `SKILL.md` §8.1.1
& $py decide.py --refresh                # 手工全量重取（工作日补数/调试；决策日不取数）
& $py decide.py --reuse                  # 只出周报（约 10 秒）
& $py decide.py --init                   # 只建缺失的账本
& $py weekly.py --ledger 50k --init --tier B                  # 5 万首次建账
& $py weekly.py --ledger 50k --refresh-net      # 决策日收尾：刷新净利润（只读；不取数、不动账本、不刷档位）
& $py paper_exec.py --ledger 50k                 # 巡检第 2 步：撮合已触发条件单 + 结算
& $py paper_exec.py --ledger 50k --date <交易日>     # 补跑/回溯：按该日撮合 + 结算
& $py review.py --ledger all             # 记分卡：样本不足/未达标/暂达标
& $py report_html.py --ledger all        # ★ HTML 周报
& $py monthly.py --month 2026-10         # ★ 月报（md + html）
& $py monthly.py --month 2026-12 --annual   # ★ 年报（每年 1 月 1 日）：年度总结 + 年度结算（奖励划出、本金重置、总收益链式累计；重渲染加 --no-settle）
& $py score_panel.py --archive --date 2026-11-20   # 归档本期四维评分（weekly 自动做，可手动补）
& $py score_panel.py --health --year 2026          # 体检：分维度 IC/五分组差/命中率（面板 <26 周只写样本不足）
& $py doctor.py --ledger all             # 体检（只读）：GO 才继续跑决策
& $py rollback.py --ledger 50k --list    # 账本损坏时：先 --list → --dry-run → 再执行

# 编排（详见 references/team-framework.md）
& $py team.py --run --pipeline --capital 50000 --ledger 50k   # T0–T7 + 任务书（不评分）
& $py team.py --run-t8 --score-only --capital 50000 --ledger 50k  # 月报：T8 只取数字（不判档）
& $py team.py --run-t8 --score-only --annual --capital 50000 --ledger 50k  # 年报（1/1）：定档/奖励
& $py team.py --budget --capital 50000 --ledger 50k           # 预算分配
& $py team.py --contract                                      # 六条交接契约
& $py team.py --dry-run --capital 50000 --ledger 50k          # 只走编排不调用管线（调试）
& $py team.py --run --monthly --capital 50000 --ledger 50k    # 月度编排（含 T10 复盘）
& $py score.py --capital 50000 --return 0.02 --grades B,B,B-,C  # 12 期分布考核
& $py grid.py --verify                                        # 网格回测（联网，只验证不启用）
# 本部署执行者：Lead 主会话 + fresh 子代理（S1 研究 / S2 深取 / S3 复核 / S4 巡检；月报 T10 复盘）；Lead 跑规则引擎/行情单与 T9，验收用 --ingest
& $py score.py --capital 50000 --return 0.035 --token-cost 1.2 --mdd -0.08
& $py billing.py --status
& $py grid.py --signal                                        # 网格只算信号，不下单

# 历史回放与可复现性（回测只覆盖 ETF 承接层，个股层缺 PIT 数据）
& $py backtest.py --start 2020-01-01 --compare                # 规则变体对比
& $py determinism.py --capital 50000                         # 同缓存跑两遍逐字比对
```

跑完先看 `out/stage3_plan.md`（执行计划），再看 `out/stage2_report.md`（每只买/不买的理由）。

## 目录结构

```
invest/
├── SKILL.md AI 执行手册 · README.md 本文件（命令/产物/运维）· requirements.txt 依赖
├── scripts/  config.py 全部阈值（末尾 skilldata.install() 注入 stock 数据层）；codes/datasource/fuyao
│   阶段一/二：metrics 纯计算 · screen 漏斗 · judge 否决+打分+定价+论点 · etf R3 权益 ETF · etf_codes ETF 代码 · fin_extra 财务补数 · wealth R2 场内债 ETF · otc R2 场外债基池（一手买不起时兜底）· risk R1–R5 定级 + 组合评审闸门
│   执行/模拟盘：capital · plan（仓位/分批/止损/ETF 层/场外 R2/现金层）· paper 账本 · otc_book 影子账 · navstats · breakdown · weekly · paper_exec · review（12 周口径）· doctor · rollback · decide（`SKILL.md` §8.1.1 十步）· monthly（含上月复盘）· preflight 决策前就绪检查 · cash_pool 货基池 · cash_sleeve 现金层 · repo_pulse 逆回购脉冲 · dividends 分红
│   回放/扩展：backtest（--pool pit、--bootstrap）· etf_pit · snapshot L1 全池快照（本地拼装，不取数）· score（A/B+/B/B-/C/D，12 期；2027-10-30 起评判）· grid（enabled=False）· news R/C + 五档处置 · news_calib（12 期回放）· news_eval（事件面事后命中率，月报只记录）· policy_map（政策→行业可证伪跟踪）· sentiment 涨停/跌停/炸板/龙虎榜（不出信号）· rates 货基/逆回购
│   运维/自检：patrol ★（交易日 20:00；取数由 19:00 Windows 任务完成）· housekeeping · determinism · _smoke · selftest_all ★ · _selftest_*（小资金算术/篮子权重/撮合/风险/场外/编排/巡检/扩展/基金/体检/同花顺/公告/PIT/快照/事件/影子账/净值/月报/面板/拆解/归档）
├── references/  engine-notes · logic-map · data-sources · news-rules · team-framework · data-layer · repo-analysis · paper-ops
│                · thresholds · report-formats · risk-review · orchestration
└── tools/  doc_gate（文档 token 契约闸门）· doc_audit · doc_verify · doc_coverage（压缩/改写审计）
```

自测全跑：`& $py selftest_all.py`（约 2–3 分钟；`--fast` 跳过联网的 3 套）。共 33 套（30 套离线 + 3 套联网），全部 exit 0 才算健康；断言项数以 `selftest_all.py` 汇总行为准。

## 文档体检（防膨胀）

规则就写在文档里，所以**文档削减与改写要走闸门**：新增的细节/解释一律进 `references/`，`SKILL.md` 只放执行规则、步骤与指针；每次改完文档跑一次契约校验，token 缺一即失败。

```
& $py ..\tools\doc_gate.py              # 契约校验：数字/反引号/脚本/--flag/标题/规则词不许丢
& $py ..\tools\doc_gate.py --update     # 人工确认内容确有增删后重建契约（commit 里写明理由）
& $py ..\tools\doc_gate.py --list       # 看契约覆盖的文件与 token 数
```

- 契约存 `tools/doc_contract.json`，对 `SKILL.md`、`README.md`、`references/*.md`、`prompts/*.md` 各存一份 token 集合（数字、反引号 span、脚本路径、`--flag`、标题、规则词、代码围栏数）。
- 压缩/改写长文档时另用：`doc_audit.py --old 基线 --new 新稿 [--also 外迁文件]`、`doc_verify.py --old 基线 --new 新稿`（标题序列/frontmatter/§ 引用）、`doc_coverage.py --old 基线 --new 新稿 --threshold 0.35`（段落覆盖度，防整段删）。
- 闸门 FAIL 不等于文档错：先确认该 token 是否真的有意删除；有意删才 `--update` 重建契约，并在 commit message 写明删了什么、为什么。

## 数据与产出落在哪

代码只读也能跑。缓存与产出默认落 `D:\skills\invest\stock\data\invest/`（换位置：`$env:A_SHARE_WORKDIR='D:\some\dir'`）：

```
cache/    按日分目录的原始缓存（沪深300 全量；同一 key 留 2 天，同花顺与公告按日快照同此）
out/      stage1_*.csv|json（stage1_wealth.*：R2 债券 ETF；stage1_otc.*：R2 场外债基池）
          data_snapshot.csv|_meta.json（L1 全池快照，本地拼装，不取数）
          stage2_*.csv|json|md、stage3_*.md|json|csv、provenance*.json|provenance_otc.json
          scorecard.md、billing.jsonl、budget.json
          paper/  weekly_{日期}.md、orders_{日期}.csv、report_{日期}.html、monthly_{YYYY-MM}.md|html、doctor.md（只留最新）
          team/   state.json（编排状态）、contract.md（契约）、lead_plan.md（Lead 汇总）
          deep/ T3 深取产物 <code>.json（T4 输入）· agent_tasks/ T2/T4/T8/T9/T10 任务书（T8 仅月报）
          stage1b_research.csv、stage1b_tags.csv、T8_*.md、T9_*.md、T10_review.md（LLM 产出）
patrol_latest.json     工作日巡检（DSH 20:00 跑 patrol.py；按天存 out/patrol_*.md）
daily_log.md           巡检记录：一天一行（13 列：资产/累计/回撤/仓位/当日盈亏/token 花费/环境/告警）；当年在主文件、往年归档 `_archive/`（永久保留；年报底稿 `housekeeping.py --year`）
ledger/state_50k.json  5 万模拟盘账本（技能目录 ledger/，DSH 巡检计算；现金/持仓/成交/挂单/净值序列），唯一事实来源
```

## 环境

- Python 3.13 + `D:\skills\invest\stock 包（取数接口层）` + `pandas`（`pip install -r requirements.txt`）；必设 `PYTHONIOENCODING=utf-8`、`TQDM_DISABLE=1`
- 本机已验证：`D:\skills\invest\stock\.venv\Scripts\python.exe`（Python 3.13.16 + pandas 3.1.0）
- 东财行情接口（`*_em` 股票/行情系）被本机网络层拦截，引擎走新浪/中证指数/百度；场外基金与费率系（`fund_*_em`）可用，`otc.py` 依赖
- **可选**第二数据源：同花顺 API（唯一实现 `D:\skills\invest\stock\stock\sources\fuyao.py`，技能内 `import fuyao` 可用）。设 `FUYAO_API_KEY` 或写进 `$WORK_DIR/fuyao.key`（**不要放进技能目录**）；无 key 也能跑全链路，零依赖。体检 `python xcheck.py` → `out/xcheck.md`（收益/回撤/PE/ROE 对照）；能补与不能补（PIT 财报、历史估值分位、资金流取不到）见 `references/data-layer.md` §5.1
- **免费无 key 两源**：巨潮公告 `python cninfo.py --code 601058 --days 365`（风险扫描 `--scan 池.csv --soft`）；baostock `python pit.py --code 600519 --asof 2026-10-28`（**唯一有真实披露日**）
- **三层扩展**：行业 `python industry.py --refresh`（`--check` 全量对账；`--limit` 只影响 `--refresh` 抓取条数）；情绪 `python sentiment.py --codes 601058,600519`；宏观 `python macro.py`（每月一次足够）；基金 `& $py fund.py --health --n 6`（月度抽检场外池，冲突先查主源）。都进 T0 体检，缺失只提示不阻断；**情绪层不产生买卖信号**，宏观层不参与打分
- 安装：`baostock==0.9.4` 已装进 `D:\skills\invest\stock\.venv`；包与数据落 `D:\baostock\`，技能目录不放第三方文件
- 选源规则、已知限制、还缺什么：`references/data-sources.md`；某个数字怎么算出来：`references/logic-map.md`

## 密钥与隐私

本仓库**不含任何个人凭据**：

- 需要 key 的数据源只从环境变量或仓库外文件读取（同花顺：`FUYAO_API_KEY`，或 `$WORK_DIR/fuyao.key`），仓库里只有变量名与用法说明。
- `ledger/`、`stock/data/`、`stock/.venv/`、`out/`、`*.key`、`.env*` 都是本地运行时产物，已被 `.gitignore` 排除。
- `stock/stock/endpoints/em.py` 中有三处东方财富公开网页接口的固定参数（`token` / `ut`，与 akshare 相同的公共常量），用于签公开行情请求，**不是个人凭据**；删掉会导致对应接口不可用。

## 五层结构

**五层**：① 漏斗 全市场 → 候选个股（`top_n` 上限）+ 权益 ETF + 债券 ETF + 场外债基（`SKILL.md` §1、`config.py`）；② 判断 四道否决 → 四维打分 → 买入价/目标价/止损价/可证伪条件（阶段二）；③ 执行 状态定仓位上限 → 前 3–5 只随档位、观察区 0 权重不建仓 → 三批建仓（单批 <5,000 元按手数合并，ETF 单批）→ 整手化 + ETF/场外承接 + 现金层（阶段三）；④ 模拟盘 周三、周六 20:30 决策（19:00 取数；决策日不取数、不评分）→ 卖出按下一交易日开盘价、买入限价条件单 → 净值/胜率/超额 → 周报只给净利润，档位/奖励只在年报（每年 1 月 1 日）定档（`SKILL.md` §8）；⑤ 编排 T0–T10 代码做机械、LLM 只做一次性判断，评分 + 计费 + 复盘（`references/team-framework.md`）。

## 利润与奖励口径

- **净利润 = 账户累计收益（元）− token 实花（元）**；**净收益率 = 净利润 ÷ 本金**。token 实花取 `out/budget.json` 该账本累计 `spent_cny`（真实扣费），不用年度计提。周报核心表只给净利润/净收益率；月报、记分卡、`score.py`、T8/T9 与汇报必须给「账户收益/token 实花/净利润/净收益率」四项。
- **周报固定结构见 `references/report-formats.md` §6**（收益与消耗置顶、策略后置、附录折叠；同一数字不在正文重复）。
- **奖励评估期**：基准日 2026-10-28 + 12 个月 = **2027-10-30**；2027-10-30 之前 reward 一律 0，文案「评估期未到（2027-10-30 起评判）」。档位阈值不变，输入为净收益率。
- **计分时点 = 年报**：`config.PERF.grade_period="annual"`；档位/奖励只在每年 1 月 1 日年报判定（`team.py --run-t8 --score-only --annual`、`review.py --ledger 50k --strict --annual`），月报只报四项口径与本季进度、不判档，`review.py --strict` 在月报不因「未达标」失败。**定档年化口径 = 本年各月等价年化率的算术平均**（年报时先记录上月的等价年化再取平均；不足 3 个月只作参考，缺数据月份列 missing 不进平均）。
- **季计价**：日常主指标 = **本季累计净收益率** vs 季达标线（年化 ÷ 4 = **0.325%**，5 万本金 ≈162.5 元/季）；token 季度预算 **37.5 元**；年化只作「外推值」脚注；护栏「连续 2 季不达标才算失败」。
- **动态本金**（2026-12-01 起，`config.PERF.dynamic_capital`）：每月月报时刻注资 **5,000 元**；上月 token 实花从**现金**出账；月收益率 = （月末净值 − 窗口内注资）÷ 上月最后净值点 − 1；月等价年化 = `(1+r)^(365/该月实际运行天数) − 1`；本金 = 上月末净值（已扣 token）+ 5,000。留痕 `meta.capital_history / meta.monthly_rates / meta.token_booked`。**统计口径**：`meta.capital` 只用于仓位上限，累计收益/收益率用「累计投入」（初始本金 + Σ注资 − Σ划出）；注资是**现金流、不写净值点**；token 出账后净值为净口径、报表不二次扣；年度奖励划出时同步下调年末净值（cash/nav/本金三方一致）；`plan.py`/`run.py`/`team.py` 不传 `--capital` 时自动读账本累计投入。
- **两道下单闸门 + 赔率**：单笔成本率 ≤0.65%（`CAPITAL.commission_ratio_block`，按 5 元最低佣金 ÷ 单笔）、往返成本 ≤0.60%（`EXEC.max_roundtrip_cost`）、赔率 ≥1.5（`EXEC.min_risk_reward`，按第 1 批限价算）；拦下的单落 `out/stage3_plan.json` 的 `cost_blocked` / `rr_blocked` 并在报告里解释。
- **计费只认实扣**：取小鲸鱼记账账本 `events[].cost`（`$DSH_HOME/.dshw-usage.json`），按会话窗口/周期区间累计——插件按余额下降记账户口径、按 token 逐轮记 `events[]`，单价表由插件维护，技能不自己按单价核算。每日巡检会话第 1 步 `billing.py --task-begin --ledger 50k --note 每日巡检`、收口第 5 步 `--task-end --note 每日巡检` 实结本会话（按对话时间窗求和 `events[]`、两次累计快照差额对账，`source=task-window`、`estimated=False`）；决策第 2 步 `billing.py --cycle-end --ledger 50k` 补上一周期、第 10 步 `--task-end --note 决策 <数据日>` 补该次会话；周报单列「上周技能花费（实扣）」（`weekly.py` 的 `week_skill_cost`）。结算后把终点写进 `out/team/billing_cycle.json`，不重复计；缺记录不记账并如实报错，`--flat-yuan` 只留历史对账；`--purge-test` 清掉自测误写流水（真实账本不要留假数据），`--settle --day <YYYY-MM-DD>` 只读对账（不记账）。

## 出事怎么办（运维速查）

决策、取数、巡检都无人值守（休市日跳过；决策日不取数）；巡检提示词底稿 `prompts/daily_patrol.md`，每周可调附加任务。出问题先对号入座，**不要手工改 `paper/*.json`**。

| 症状 | 先做什么 | 命令 |
|---|---|---|
| 这一期没跑/晚了几个小时 | 确认数据已是最近交易日，再补跑（幂等） | `weekly.py --ledger 50k --reuse` + `team.py --stage T9` 填四段 → `--ingest T9`（数据旧就先在工作日补数） |
| 报告和上期一样/数据日期不是最近交易日 | **本机工作日取数失败**（决策日不重取）；先补跑再决策 | `python -m stock prefetch` → `run.py --capital 50000` |
| 体检 NO-GO | 按 ERROR 逐条修，不绕过、不改账本 | `doctor.py --ledger all` |
| 账本被改坏/疑似多下单 | 先预览再回滚，回滚后立刻体检 | `rollback.py --ledger 50k --list` → `--dry-run` → 执行 |
| 想确认某天会成交什么 | dry-run 不写账本 | `paper_exec.py --ledger 50k --date <交易日> --dry-run` |
| 账本丢了/换机器/清过目录 | 先建账再决策（只建缺失的） | `decide.py --init` |
| 只想跑决策、不跑研究层 | 决策日按 10 步手工串（只做事件面+计划+周报）；`decide.py` 含取数，只用于工作日补跑 | `weekly.py --ledger 50k --reuse` |
| 报「out/ 是另一个本金的计划」 | 按本账本本金重跑取数 | 工作日跑 `python -m stock prefetch` → `run.py --capital 50000` |
| 数据接口大面积失败 | 不许编数据；报告失败比例，稍后重跑 | 看 `out/stage1_funnel.json` 的「取数成功/股票池」 |

## 边界

仅中国大陆 A 股 + 国内 ETF + 场外债券基金，本部署本金 5 万元。不做择时；回测只覆盖 ETF 承接层
（个股层缺 point-in-time 数据）；不覆盖港美股/期货/加密、不接券商、不保证收益。
佣金按**万 1、最低 5 元/笔**（`config.ACCOUNT` 唯一出处，交割单口径）；真钱下单窗口 15:00–15:30（模拟盘不用）；卖出按下一交易日开盘价成交，买入一律限价条件单（跌到限价才买、10 个交易日不成交自动失效）。R2 场外债券基金腿由**影子账**记账（`otc_book.py`：按提交日下一交易日累计净值成交、T+1 确认，市值 = 投入 × 累计净值比，赎回费按持有天数取实测档；净值缺失时挂单保持 pending）；场外基金无 point-in-time 数据、不做历史回放。
评分与奖励是**记账口径**，不是收益承诺；LLM 只做一次性判断（fresh 子代理），不产生新信息，价值在动机分离与并行。角色与流程见 `references/team-framework.md` §1、§2.1。

## 版本库

`git@github.com:blz89/invest-public.git`（SSH）· <https://github.com/blz89/invest-public>

当前版本 **v1.3.0**（tag `v1.3.0`）。
