# 信息源全景（全部免费）

> 单一出处：数据源选型/限制/缺口只在本文件维护；执行口径见 `SKILL.md`，阈值数值见 `scripts/config.py`，公式与历史证据见 `references/engine-notes.md`，角色与团队见 `references/team-framework.md`。

> 用法：先看「按用途选源」，再看「已知限制」。任何源失败都必须抛错，不许静默返回空表。
> 每个源都把「接口名 + 参数 + rows + fetched_at」写进 provenance，`doctor.py` 可查。

## 1. 一张表

| 源 | 模块 | 覆盖什么 | 凭据 | 稳定性 | 定位 |
|---|---|---|---|---|---|
| 取数接口层（新浪/中证/百度） | `datasource.py` | A 股行情、财务指标（2019 起）、PE/PB（近五年）、ETF 行情/列表、49 个板块、货基榜、交易日历 | 无 | 中（东财系接口被本机网络层拦截，不可用） | **主源** |
| 同花顺 Fuyao | `fuyao.py` | 行情快照、历史 K 线、财务三表、财务指标 5 类、估值快照、ETF 日线、场外基金净值/区间收益/排名、交易日历、**指数成分（当前）/指数快照/近 5 年指数日 K、分红送转复权事件** | key（`HITHINK_FINANCE_API_KEY` → `FUYAO_API_KEY` → 官方 `%APPDATA%\hithink-finance\credentials.env` → `$WORK_DIR/fuyao.key`） | 高（官方 REST；契约按官方 `docs/api` 复核，客户端预校验窗口/批量，只限 QPS 不限总量） | **第二源 / 交叉校验** |
| 巨潮资讯 | `cninfo.py` | 公司公告（按股票+日期区间 / **全市场按披露日**），可分类出风险事件 | 无 | 高（法定披露平台） | **事件面（新增）** |
| 巨潮全市场公告 → 事件面 | `news.py`（读 `cache/cninfo_market/<日期>.csv`） | 全池事件面：候选只留「高」、持仓留高/中；0 额外请求 | 无 | 高（交易所披露） | **事件面** |
| 政策面（财联社电报） | `news.py` → `ds.fetch_global_news()` | 全市场政策/监管快讯关键词过滤，落 `out/policy_events.csv`；只做背景不改权重 | 无 | 中（快讯） | **事件面背景** |
| 东财 F10 财报三表 | `datasource.fetch_fin_balance/income/cashflow` | 资产负债表 / 利润表 / 现金流量表（含 `NOTICE_DATE` 披露日 → 可做 PIT）；`fin_quality()` 给应收/存货/有息负债同比、经营现金流÷净利 | 无 | 中（东财数据中心 datacenter-web，与 SEC 主机等价） | **财报细项** |
in_quality() 给应收/存货/有息负债同比、经营现金流÷净利 | 无 | 中（东财数据中心 datacenter-web，与 SEC 主机等价） | **财报细项** |
| 东财业绩预告 | `datasource.fetch_forecast` | `RPT_PUBLIC_OP_NEWPREDICT`：预告类型、净利润上下限、同比区间、公告日、是否最新 | 无 | 中 | **业绩预告（新增）** |
| 巨潮公告正文 | `cninfo.py --body` | 公告 PDF 正文（区分拟减持/已实施、问询/处罚）；按 URL 永久缓存 | 无（需 `pypdf`） | 高（原文） | **事件面** |
| 同花顺行业标签 | `industry.py` | 个股的同花顺行业 / 地域 / 特色标签（按 `type` 字段过滤，去掉指数成分） | key | 高（官方 API） | **行业层（新增）** |
| 同花顺情绪数据 | `sentiment.py` | 涨停池 / 跌停池 / 炸板池 / 龙虎榜 / 热榜 / 异动（**当日快照**） | key | 高（只限 QPS） | **情绪层（新增）** |
| 取数接口层宏观（东财/国统局/交易中心） | `macro.py` | LPR、SHIBOR、CPI、PPI、PMI、M1/M2 增速 | 无 | 中（月度数据、滞后 10–40 天） | **宏观层（新增）** |
| baostock | `pit.py` | **财务 `pubDate` 真实披露日**、历史指数成分（hs300/zz500）、TTM EPS、审计意见、交易日历 | 免费 pip | 中高（能跑，但无人维护，字段稀疏） | **PIT 补盲（新增）** |
| 中债国债收益率曲线 | `datasource.fetch_cn_treasury_yield` | 1 年期无风险利率（**现取**，只作参照、不作达标线） | 无 | 高 | 债/现金定价基准 |
| 新浪逆回购 sh204001 | `datasource.fetch_repo_rate` | GC001 近 250 日均值（毛） | 无 | 高 | 现金层 |
| 天天基金 / 东财 | `otc.py`（`fund_*_em`） | 场外基金全量（净值/规模/费率） | 无 | 中高（`fund_*_em` 实测可用；**天天基金网页版不可用**，被本机网络层拦截） | **场外基金主源**；同花顺 fund 仅做独立复算/校验 |
| 基金规模（双源） | `datasource.fetch_fund_scale` | 蛋卷基金档案（主源）→ 东财 fundmobapi（兜底，债基/货基也覆盖）→ ETF 总市值 | 无 | 中高（蛋卷会风控返回空；兜底后池内 60 只实测 60/60） | **场外池规模过滤**；`fund_scale_source()` 可查实际源 |

## 2. 按用途选源

| 我要什么 | 用哪个 | 备注 |
|---|---|---|
| 个股/ETF 行情 | `datasource.fetch_price` / `fetch_etf_hist` | 同花顺 `fuyao.etf_hist` 可交叉校验（实测与新浪一致） |
| ETF **前复权**日线（ATR/波动用） | `datasource.fetch_etf_hist(code, adjust="qfq")` | 东财前复权（分红下沉、序列连续）；取不到自动回退不复权并记 provenance。收盘价/成交价仍用不复权 |
| ETF **折溢价** | `datasource.fetch_etf_premium(code)` | 不复权收盘 ÷ 同日**单位净值**（天天基金净值通路）− 1；净值未披露会标 `nav_date`。巡检对持仓 ETF 自动体检 |
| 风险警示 / 退市整理名单（当前） | `datasource.fetch_code_names()` | 交易所简称名单（全市场约 5,600 只），简称含 `ST`/`*ST`/「退」即剔除；随 prefetch 落缓存，名单缺失时退回名称判断 |
| A 股基础信息（上市/退市日） | `datasource.fetch_stock_basic()` / `pit.stock_basic()` | baostock `query_stock_basic`，全市场约 8,900 行（股票 5.5 千 + 指数）；随 prefetch 落缓存 |
| 历史退市名单 | `datasource.fetch_delisted()` / `pit.delisted()` | 约 300 只，列：代码/名称/上市日/退市日（按退市日倒序），用来量化幸存者偏差 |
| 某日仍在市（PIT 清单） | `pit.listed_asof(day)` | 上市日 ≤ 该日 < 退市日；和当日指数成分取交集得到「当时可选池」 |
| 历史 ST 区间 | `datasource.fetch_st_intervals(code)` / `is_st_on(code, day)` | baostock 日线 `isST` 逐日标记合并成区间；空表=从来没 ST 过（会落缓存，不重复取） |
| 停牌判定 | `screen.py` L1：最近一根日线日期 == 数据日 | 停牌股最后一根 K 线停在停牌前一天；不用成交量为 0 倒推 |
| 财务指标（ROE/负债率/现金流） | `datasource.fetch_financials` | 只有指标、无三表；2019 起 |
| 财务三表明细 | `fuyao.financials(code, "income"/"balance"/"cashflow")` | 单季/年度，≤10 年窗口 |
| 指数当前成分 / 指数点位 / 近 5 年指数日 K | `fuyao.index_members("000300.SH")` / `fuyao.index_snapshot` / `fuyao.index_hist` | 只有**当前**成分（回测仍走 `pit.index_members` 的历史成分）；指数历史只覆盖近 5 个自然年 |
| 分红/送转/配股事件（复权因子） | `fuyao.adjust_factors(code)` | 附 `ex_date`；用于总收益口径与分红核对，不做打分 |
| 分红现金/送转明细（入账用） | `datasource.fetch_dividends(code)`（东财数据中心 `RPT_SHAREBONUS_DET`） | 每 10 股派息（含税）/送转比例、股权登记日、除权日、方案原文、进度；无 key；账本按它入现金与增股 |
| **某个历史日“当时能看到什么”** | `pit.asof(code, asof, n)` / `pit.latest_asof` | **唯一有真实披露日的源**。同花顺 `report_date` 系统性滞后约 1 年，**不可用于 PIT** |
| 当时 PE_TTM | `pit.pe_ttm(code, price, asof)` | 用当时可见的 EPS 自算，避免偷看未来 |
| 历史指数成分 | `pit.index_members("hs300", asof)` | 回测防幸存者偏差 |
| 事件面风险（立案/处罚/减持/减值/诉讼…） | `cninfo.risk_scan(codes, asof, days)` | 默认硬风险；`include_soft=True` 带软信号 |
| 估值（当期） | `fuyao.valuations(codes)` 或 `datasource.fetch_valuation` | 两个口径交叉看 |
| 场外债券基金池 | `otc.py`（东财 `fund_*_em`） | 数量与列口径见 `out/stage1_otc.csv`；**唯一生产者**，换版本必须整池重跑 |
| 场外基金**复算/校验**（备用源） | `fund.py`（同花顺 `fund/performance/*`） | 复权净值复算 1Y/3Y/回撤 + 抽检池子健康；**不写池子** |
| 无风险利率 | `config.cash_rates()` | 单一出口，**禁止**在别处写死；逆回购/货基 7 日年化由 `rates.py` 实测（**货基每天、逆回购每周五**；写 `cash_rates_live.json`，`cash_rates()` 优先读取，实测不到才回落到常数兜底）。同一进程同时追加 `cash_rates_history.jsonl`（按数据日、同日只留最新一条），供**逐日计息**复算（朝朝宝按当日利率、货基按当日万份收益） |
| 个股行业 / 行业集中度 | `industry.concentration(positions, cap)` | 分母是**权益层**，不是总资产；25% 上限与原口径一致 |
| 市场情绪温度 / 个股异动标记 | `sentiment.market_temp()` / `sentiment.flags_of()` | **只做风险提示**，不产生买卖信号、不改评分 |
| 宏观背景（为什么现在只能偏防守） | `macro_brief.md` | **不参与打分**；月度数据有滞后 |
| 牛熊确认（价格/宽度派生，0 取数） | `scripts/regime_probe.py` → `out/regime.json` / `regime_brief.md`；`plan.market_regime()` 消费 | 双向各三条：**熊市**=距 2 年高点回撤 ≤ −20% / 相对 MA200 ≤ −10% / 宽度 < 30%；**牛市**=距 2 年低点涨幅 ≥ +25% / 相对 MA200 ≥ +10% / 宽度 > 60%；命中 ≥2 条才确认，四态（熊市/牛市/中性/信号冲突）。**直接决定档位**：探针立场 深熊→25%、熊→防守 35%、牛→进攻 80%、普通→MA200 三档。深熊=熊市确认且回撤 ≤ −35%、宽度 < 20%；滞回=进熊看确认、退熊要回撤回到 −10% 以内且宽度 > 45%（退牛：偏离 < +4% 且宽度 < 45%），立场存 `out/regime.json` 的 `stance` |

## 3. 可靠性设计（已在代码里）

- **失败必须炸**：`CninfoError` / `PitError` / `FuyaoError`，只有确定性空结果才返回 `[]`。
- **缓存分层**：取数接口层 `_cached`（按日命名空间）；同花顺 TTL 24h；巨潮 24h；baostock 进程内。
- **限速**：巨潮 0.20s/请求，同花顺 `min_interval_s=0.20`，baostock 单进程登录。
- **溯源**：`CACHE_DIR/cninfo_provenance.json`、`fuyao_provenance.json`。
- **交叉校验**：`xcheck.py`（同花顺 vs 我方），实跑：场外基金 5/5 一致、ETF 行情 4/4 一致、PE 口径 4/5 差 20–50%（同花顺 `pe_ttm` vs 我方**个股**百度 TTM：同为 TTM，差异来自样本与时点；新浪 `per` 只用于行业截面中位数，不参与这只比对）。
- **契约校验在本地**：`fuyao.py` 按官方 SDK 同一套规则预校验代码格式 / 窗口 / 批量上限，错在本地就炸，不浪费一次 1003/1004 往返；key 解析顺序与官方 `marketdb.credentials` 一致（官方凭据文件内容必须**不带引号**）。

## 4. 已知限制（别指望它们）

**同花顺（已按官方契约复核）**
- 公开 API **不提供**：分钟 K / tick / Level-2 / 港股美股 / 宏观 / 公告新闻原文 / 研报原文（官方 README「数据能力与边界」明确列出）。
- `capital-flow` / `etf-flow` / `high-frequency` 返回 `2004`：官方文档标「**端内专用**」，端外永久取不到，**不是参数错，不要重试**。
- `fund/performance/drawdowns` 全部返回 0 → 回撤必须自算。
- 估值只有快照，**无历史序列**（历史分位要自己攒或走 `pit` 自算）。
- 窗口/批量硬约束（`fuyao.py` 已做客户端预校验，与官方 SDK 一致）：三表 ≤10 自然年；单只日 K >10 年**自动切片拼接**；**指数历史只近 5 个自然年**；ETF 历史与基金日度指标 ≤5 自然年；快照/估值/板块批量 ≤100 只、异动 ≤50 只。
- 指数成分：`ths-stock-list` 可用（`000300.SH` → 300 只；`index_snapshot` / `index_hist` 实跑通过）。

**baostock**
- 财务字段稀疏（部分公司季度缺项），且不同接口字段命名不一致。
- 无 ETF、无公告、无实时行情。
- 长期无人维护，接口随时可能变。

**巨潮**
- 按单个 `orgId` 查公告会漏「池外持仓」的突发公告；**全市场增量流已接**：`cninfo.py --market` → `datasource.fetch_market_announcements()` → `endpoints.stock_notice_report()`（一次取全 A 股当日全部公告，按披露日 CSV 缓存；非交易日无公告属正常）。
- 正文已接（`ep.stock_notice_text` → `ds.fetch_notice_text` → `cninfo.body_flags`）：只扫前 2500 字、
  跳过中介机构文件、否定句/条件清单不算命中；标题仍是主信号，正文用于区分状态。

**取数接口层**
- 东财系接口（`*_em`）在本机被网络层拦截，一律不用。
- 没有 point-in-time 财务、没有历史成分股（这两项已由 baostock 补）。

## 5. 还缺什么（按价值排序）

| 缺口 | 影响 | 能否补 |
|---|---|---|
| ~~业绩快报~~ | 只有业绩预告是结构化的；快报没有现成接口 | 不补接口：走公告流标题「业绩快报」+ PDF 正文（`cninfo.py --body`） |
| **卖方一致预期**（EPS 预测、目标价） | 目标价只能用历史分位，偏保守 | 无免费源；付费（Wind/Choice）才有 |
| ~~全市场公告增量流~~ | 事件面已能覆盖全市场（含池外持仓），不只靠候选池 | **已接**：`ep.stock_notice_report(date)` + `ds.fetch_market_announcements()` + `cninfo.py --market`；巡检 `check_announcements` 自动看持仓近 7 天硬风险 |
| **北向资金 / 两融余额** | 资金面只有龙虎榜，缺主力线索 | 无免费稳定源（东财被拦） |
| **统一行业分类（申万/中信）** | 已有同花顺行业（`industry.py`），但仍非申万/中信，不能直接对齐研报 | 中证有官方分类可试，未接 |
| ~~退市 / 曾 ST 名单~~ | 当前名单能剔除 ST/*ST 与退市整理，但没有历史口径 | **已接**：当前名单 `ep.stock_info_a_code_name()` + `ds.fetch_code_names()`；历史退市 `ds.fetch_delisted()`（约 300 只，带退市日）；曾 ST 用 `ds.fetch_st_intervals(code)`（baostock 日线 isST 区间）；停牌用「最近一根日线 == 数据日」判定 |
| **ETF 份额变化**（申赎套利、情绪） | 折溢价已有，但看不到份额增减 | 交易所/基金公司披露可得，未接 |
| ~~分红除权明细~~ | 总收益口径不完整 | **已接**：`fuyao.adjust_factors`（复权事件）与东财 `RPT_SHAREBONUS_DET`（`ds.fetch_dividends`，现金/送转金额）；账本现在按除权日实际入账（税后、送转摊薄、止损不动） |
| **财务附注明细**（应收账龄、存货结构） | 只能看汇总指标，看不到质量 | PDF 解析，成本高 |

## 6. 现金层容器比价（已并入实测）

> 银行/理财 App 的容器比价已并入 `rates.py` 实测（货基每天、逆回购每周五），无需人工维护；利率统一取 `config.cash_rates()`。

## 7. 需要用户提供的

1. **券商的现金理财/闲钱管家 7 日年化 + 赎回到账时间**（报数或截图即可）
   → 用于现金层比价。现金层约 3.25 万元，年化每高 20–50bp = 65–163 元/年，零市场风险。
2. **账户事实（已确认，无需再提供）**：见 `config.ACCOUNT`——佣金**万 1**、**最低 5 元/笔**（不免五）、**下单窗口 15:00–15:30**（收盘后下单 → 次一交易日执行：卖出按开盘价、买入为限价条件单）。佣金口径与「笔数越少、单笔越大」的推导见 `SKILL.md` §1，此处不重复。

**不需要你提供**：身份证、银行卡、交易密码、券商登录；也不要让脚本登录任何券商账户。数据源侧（行情/财务/公告/基金）已全部自建完成。

## 8. 三层扩展：行业 / 情绪 / 宏观

三层的共同定位：**接进主流程、但不污染可复现性**。前四层（行情/财务/PIT/事件）决定「买什么」，
这三层只决定「怎么解释、提醒什么风险」。任何一层缺失，T0 只提示不阻断。

| 层 | 模块 | 产物 | 谁消费 | 铁律 |
|---|---|---|---|---|
| 行业 | `industry.py` | `out/industry_map.csv` | `plan.py` 行业分散、`team.py` T9、T0 体检 | 集中度分母是权益层 |
| 情绪 | `sentiment.py` | `out/sentiment.json` + `out/sentiment_brief.md` | `team.py` T9、T0 体检 | **不产生买卖信号**；只有当日快照，无法回测 |
| 宏观 | `macro.py` | `out/macro.json` + `out/macro_brief.md` | `team.py` T9、T0 体检 | **不参与打分** |
| 基金 | `fund.py` | `out/fund_health.md` | 人工/月度体检 | **不写池子**，只校验 |

**为什么不让情绪/宏观参与决策**：情绪择时与宏观择时在现有回放样本上都没有正超额证据
（**未单独标定**，见 `engine-notes.md` §八 已知限制）。接了它们进打分，等于把「不可验证的信号」
塞进「必须可复现」的流程，破坏单一事实源。

**额度**：行业层按池子 100 只/次批量取；情绪层每次 6 个请求；宏观层每月 6 个请求。
按每周 1 次、池子 300 只算，一年约 8 千条（以实际采集日为准），远低于同花顺 500 万条/年的总额度。

### 8.1 基金层：场外池的备用源（`fund.py`）

`otc.py` 的场外债券基金池（数量见 `out/stage1_otc.csv`）主源是东财，**净值与收益只有一份**。`fund.py` 用同花顺做独立复算与交叉校验，不替代主源。
`fund/performance/nav|returns|drawdowns` 做三件事，**不替代主源**：

| 做什么 | 怎么算 | 谁来用 |
|---|---|---|
| 独立复算 | 同花顺**复权净值**（`adj_nav`）→ 1Y/3Y 年化、最大回撤 | 对表 |
| 交叉校验 | 复算 vs `stage1_otc.csv`，1Y 差 >120bp 或 3Y 差 >150bp 记「冲突」 | 体检 |
| 抽检池子 | `--health --n 6` → `out/fund_health.md` | 月度 |

两个踩过的坑（写下来省下一次）：
- **接口参数是 `range` 不是 `limit`**：不传 `range` 只返回最新一条净值（第一版就栽在这，拿到 1 条以为基金没数据）。
- **必须用复权净值**：分红会让单位净值台阶式下跌。实测 012310 用 `unit_nav` 算 1Y 只有 0.40%，
  用 `adj_nav` 是 3.02%，而我方 CSV 是 3.05% —— 差的是一整个分红。

实跑 6 只：全部「一致」，复算 3Y 年化与同花顺自己的累计收益折年化差 ≤2bp。

## 留存与清理（本地落盘策略）

留存与清理的唯一口径见 `SKILL.md` §5.8，此处不重复表。
