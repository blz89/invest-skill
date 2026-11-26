# 数据层排错手册

> 排错细则在本文件；SKILL.md 提供排错入口。

## 5. 数据层的坑（排错时优先查这里）

### 5.0 信息源全景（选源/限制/缺口：`references/data-sources.md`）

主源 stock 取数接口层（**唯一实现在 `D:\skills\invest\stock\stock\skilldata\datasource.py`**，
由 `config.py` 末尾 `skilldata.install("invest", ...)` 注入）；第二源同花顺（见 5.1，唯一实现 `D:\skills\invest\stock\stock\sources\fuyao.py`）；
**事件面**巨潮公告（`cninfo.py`，见 5.2）与同花顺资讯（`news.py`，见 5.2b，含 R/C 评分与六档处置）；
**PIT** 真实披露日与历史成分（`pit.py`，见 5.3）。所有源失败必须抛错，**不许静默返回空表**。

> **数据层归属 stock**：`datasource.py`/`codes.py`/`fuyao.py` 由 stock 提供，
> `config.py` 末尾 `skilldata.install()` 向 stock 注入本技能 config（缓存/产出目录仍走本技能 config）。**改取数逻辑改 `D:\skills\invest\stock`**，
> 不要改这里；行业归一这类策略口径在本技能（`datasource.set_market_hook()` 注册）。
> 数据层归属与验证方式见 `D:\skills\invest\stock\README.md`「数据层归属」。

| 现象 | 真实原因 | 处理 |
|---|---|---|
| 现金流指标全是 0.0x | `经营现金净流量与净利润的比率(%)` 列名带 `(%)` 但**实际是纯倍数**（茅台 2021 = 1.1491 = 640.29亿/557.21亿） | 列值即倍数，不许再除 100 |
| 第二次运行估值全 NaN、L3 全 0 | CSV 缓存读回类型退化：`"000063"`→整数 63、日期变字符串 | 归一化在 `_cached(..., post=...)` 里做，两条路径都过它；不要绕过它直接读缓存 |
| 改了窗口长度但筛选结果不变 | 缓存键没带窗口长度 | 缓存键须用 `{sym}__{years}y` |
| PowerShell 报 `NativeCommandError` 且退出码 1，但数据其实取到了 | 取数层的 tqdm 进度走 stderr | `TQDM_DISABLE=1` 关掉进度条；仍出现时先看产出文件是否新鲜，不要盲目重跑 |
| 退出码 `2147483651`，堆栈里有 `py_mini_racer\mini_racer.dll` | `native/sina.py` 内联 JS 解码并 `atexit` 关引擎（不要每次调用新建 V8 实例，多线程下会偶发原生崩溃） | `run.py` 自动重试一次；单独跑 `screen.py` 时手动重跑一次 |

### 5.1 第二数据源：同花顺金融数据 API（可选，唯一实现 `D:\skills\invest\stock\stock\sources\fuyao.py`）

**定位：只做交叉校验与补空白，不替换主通道**——单一数据源出错无法自证。
现金流列名带 `(%)` 实为倍数、份额折算污染净值涨幅、CSV 读回代码丢前导零，都靠第二来源才定位。

- **契约来源**：同花顺官方仓库 `Hithink-Tech-Org/Financial-API`（MIT）的 `docs/api/`。
  `stock.sources.fuyao` 只做**零依赖的官方 REST 封装**（不引官方 CLI/SDK 依赖），窗口/批量上限已在本地预校验。
- **启用**（顺序与官方 SDK 一致）：环境变量 `HITHINK_FINANCE_API_KEY`
  → `FUYAO_API_KEY` → `FUYAO_TOKEN`/`API_KEY` → 官方用户级凭据文件 `%APPDATA%\hithink-finance\credentials.env`
  （`HITHINK_FINANCE_API_KEY=xxx`，**不能加引号**）→ `$WORK_DIR/fuyao.key`（一行一个）。
  全都没有时 `fuyao.available()` 为 False，全链路走取数接口层，零依赖。
- **体检**：`python xcheck.py` → 出 `out/xcheck.md`，逐项对照场外基金区间收益、ETF 近 1 年收益/回撤、
  个股 PE、个股 ROE，判定 一致 / 接近 / 冲突（结果逐项看表，文档不写死一致数）。
  单独体检（打印 key 来源，**不打印 key**，并逐个端点实跑）：
`D:\skills\invest\stock\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, r'D:\skills\invest\stock'); from stock.sources import fuyao; raise SystemExit(fuyao.main())"`
- **指数域**：`index_members("000300.SH")` → 当前成分 300 只；
  `index_snapshot` 指数点位；`index_hist` 近 5 个自然年日 K。只有**当前**成分，回测历史成分走 `pit.py`。
  `adjust_factors(code)` 取分红/送转/配股事件（附 `ex_date`），补总收益口径。
- **不可用，不要试**：
  1. `financials` 的 `report_date_ms` **不是真实披露日**（系统性滞后约 1 年）→ 真实披露日必须走 baostock（见 5.3），**不要拿这个字段做 PIT**；
  2. `fund/performance/drawdowns` 对样本基金全部返回 0 → 回撤自算；
  3. `capital-flow` / `etf-flow` / `high-frequency` 返回 `code=2004`：官方文档标「**端内专用**」，端外永久取不到，**不是参数写错，不要重试**；
  4. 估值只有当期快照，**没有历史分位序列** → 历史分位用百度近五年；
  5. 公开 API 不含分钟 K / tick / Level-2 / 宏观 / 公告原文 / 研报原文（官方 README 明列）→ 别在这几个方向提需求。
- **薄封装、不参与决策**：龙虎榜 / 涨停池 / 热榜 / 异动监控能取到，但属短线情绪数据，与 R2/R3 中长线无关。
- **pe 有两个来源，必须分开说（汇报时说错就是数据源错）**：
  ①**个股 `pe`（`pe_cur`/`pe_pct_own`）= 百度近五年「市盈率(TTM)」序列**（`datasource.fetch_valuation`），是 TTM；
  ②**新浪板块快照的 `per` 只用于 L3 的「同行业截面中位数」**，不参与个股打分，也不写入个股 `pe`。
  与同花顺 `pe_ttm` 对照会差 20%–50%，属同一 TTM 口径但样本/时点不同，不代表口径不同。

### 5.2 事件面：巨潮公告（`scripts/cninfo.py`，免费、无 key）

- `python cninfo.py --code 601058 --days 365` → 公告标题/日期/链接；`--json` 出机器可读
- `python cninfo.py --scan 候选池.csv --soft` → 批量风险扫描（默认只报硬风险）
- `python cninfo.py --market --market-days 7` → **全市场增量扫描**（一次取全 A 股当日公告，按标题打标；覆盖「池子外」的持仓突发公告）
- `python cninfo.py --market --watch ledger/state_50k.json` → 同上，但只看持仓/目标的公告（巡检每天自动跑这一条）
- `python fin_extra.py --code 000999` → **财报细项 + 业绩预告**：最新报告期应收/存货/有息负债同比、经营现金流÷净利、近三期利润表（带披露日）、最近几条业绩预告（类型/净利区间/同比区间）；`--codes out/stage1_candidates.csv --limit 20` 批量看
- `python cninfo.py --market --body` → 再读**正文 PDF**（`--body-max` 控制篇数，默认 20）：区分「拟减持 / 已减持完毕」「问询函 / 处罚决定书」；巡检命中硬风险时自动读最多 5 篇
- **硬风险**（进四道否决的**前置核查**，提示人看；真正的硬雷拦截在新闻层 `news.py` 的 R 分 → `mult=0`）：立案调查 / 退市风险 / 行政处罚 / 监管措施 / 商誉减值 / 业绩预亏 / 会计问题 / 诉讼仲裁
- **软信号**（只提请注意、不否决）：减持 / 解禁 / 质押冻结 / 再融资
- **业绩预告体检**（巡检每日）：持仓个股最近一期预告命中 预亏/首亏/续亏/增亏 → 高危，预减/略减 → 中危（`patrol.check_forecasts`，看最近 180 天公告期；只报不自动卖，预亏进 §8 复核）。业绩快报没有结构化接口，走公告流标题 + 正文
- **噪声不上报**：常规担保、季度例行「计提资产减值准备」（几乎每家都有，没有区分度）
- 限制：正文解析只扫**前 2500 字**（后段是模板条款，噪声大）、跳过保荐书/法律意见/审计报告等中介文件；
  全市场流按披露日落 CSV 缓存（非交易日无公告属正常），接口异常会 fail-loud 而不是静默空表；
  PDF 正文按 URL 永久缓存（同一篇不重复下载），需要 `pypdf`（`pip install pypdf`）

### 5.2b 事件面评分：同花顺资讯 + R/C 双向打分（`scripts/news.py`）

- `python news.py --days 14` → `out/news_events.csv` + `out/news_brief.md`（逐只扫持仓+候选的同花顺资讯/异动/除权；**并把本地全市场公告缓存并进事件面**，覆盖 `stage1_all.csv` 全池约 300 只，0 额外请求）
- **两套分、方向相反**：`R 风险 0–100`（决定还能不能持有）、`C 催化 0–30`（决定值不值得多买）
- **六档处置（否决 / 降权40 / 降权15 / 加分15 / 加分10 / 不干预）**：`R≥60 或硬雷 → 否决(系数 0)`｜`R≥40 → 降权40(0.6)`｜`R≥25 → 降权15(0.85)`｜
  `C≥22 → 加分15(1.15)`｜`C≥15 → 加分10(1.10)`｜其余不干预。**利好不能冲抵利空**
- **没有风险事件时 R 恒为 0**：来源与时效只放大已发生的风险，不许凭空造分
- **岗位职责、执行流程（六步）、输入输出契约、失败处理**：**`references/news-rules.md`（数据官 + Lead 的事件面手册）**
- 权重与阈值、两池校准证据、[先验] 标记、局限：同一份手册的后半部分
- **政策面**：`news.py` 另从**财联社电报**（`ds.fetch_global_news()`，`config.POLICY` 关键词过滤）捞政策类快讯，落 `out/policy_events.csv`、进简报「政策面」段——**只做背景与研究输入，不参与 R/C 打分、不改选股权重**（政策影响面大但难量化，不硬塞进打分）。
- **覆盖范围**：候选+持仓逐只扫同花顺资讯（`NEWS.max_codes` 20 只）；另把 `cache/cninfo_market/<日期>.csv`（全市场公告，缓存约 7 天）并进事件面，覆盖 `stage1_all.csv` 全池（约 300 只）——候选只留「高」，持仓留高/中，按 (代码,标题) 去重；标题里「不存在/未涉及/否认…」+ 风险词不算命中。
- 事件面**不做每日巡检**，只在**决策日**出分（T2/T4 研究员交卷后、T9 计划书之前）；改权重前后必须跑 `_selftest_news.py`

### 5.3 PIT：真实披露日与历史成分（`scripts/pit.py`，依赖免费包 `baostock`）

- `python pit.py --code 600519 --asof 2026-10-28` → 只列**当时已经披露**的报告期
- `python pit.py --members hs300 --date 2019-06-28` → 当时成分股（回测防幸存者偏差）
- `python pit.py --listed-asof 2020-01-02` → 当时仍在市的股票清单（上市日 ≤ 该日 < 退市日，PIT 口径）
- `python pit.py --delisted` → 历史退市名单（代码/名称/上市日/退市日，约 300 只）；`--basic` 看全市场基础信息
- `python pit.py --st 000004` → 某只股票的 ST/*ST 区间（baostock 逐日 isST 合并成段），用来还原「当时是不是 ST」
- 术语铁律：**报告期末 ≠ 披露日**。`pit.asof()` 按 `pubDate` 过滤；缺 `pubDate` 的期**整期丢弃**（宁少不多）
- 限制：字段稀疏、无 ETF、无公告；进程内单连接，不能与其它 baostock 调用并发

### 5.4 行业层：同花顺行业标签（`scripts/industry.py`）

- `python industry.py --refresh` → `out/industry_map.csv`（按池子批量取，**100 只/次**）
- `python industry.py --check --limit 30` → 按持仓算行业集中度
- **集中度分母是权益层，不是总资产**（25% 上限与原口径一致）
- 消费方：`plan.py` 的行业分散、`team.py` T9 的「行业集中度」段、T0 体检
- 限制：同花顺行业 ≠ 申万/中信，不能直接对齐研报

### 5.5 情绪层：同花顺涨停/龙虎榜/异动（`scripts/sentiment.py`）

- `python sentiment.py --codes 601058,600519` → `out/sentiment.json` + `out/sentiment_brief.md`
- 数据：涨停池 / 跌停池 / 炸板池 / 龙虎榜 / 热榜 / 异动——**当日快照**
- **铁律：只做风险提示，不产生买卖信号、不改评分、不改仓位**
- 限制：没有历史快照 → **无法回测**；所以它永远不能进打分

### 5.6 宏观层：LPR/SHIBOR/CPI/PPI/PMI/M1-M2（`scripts/macro.py`）

- `python macro.py` → `out/macro.json` + `out/macro_brief.md`（每月 1 次足够）
- 自算两个值：实际政策利率（LPR1Y − CPI 同比）、M1−M2 剪刀差
- **不参与打分**，只解释「为什么现在只能这么偏防守」
- 数据源：取数接口层里的东财/国统局/新浪接口；**同花顺没有宏观接口**（实测 `/api/macro/*` 404；官方 README 也明列公开 API 不含宏观）

### 5.7 基金层：场外池的备用源（`scripts/fund.py`）

- `python fund.py --code 018074` → 复权净值复算 1Y/3Y/回撤 + 同花顺区间收益对表
- `python fund.py --health --n 6` → `out/fund_health.md`（抽检池子，**不写池子**）
- **铁律**：池子只有一个生产者（`otc.py`）；本层只校验，冲突了就查主源，**不许**直接换口径
- 两个坑：接口参数是 `range`（不是 `limit`，不传只给 1 条）；收益必须用 `adj_nav`（单位净值遇分红会少算）
- 抽检结果逐项对表，复算 3Y 与同花顺累计收益折年化差以 `out/fund_health.md` 为准（不写死 bp）。

### 5.7b 现金层选基：全市场货基 90 天实得年化排行（`scripts/cash_pool.py`）

- `python cash_pool.py [--universe market|chaochao2] [--shortlist 80] [--top 20] [--cash 27767]` → `out/cash_pool_rank.md` + `out/cash_pool_rank.json`。**默认 `market`：全市场货基**（`ds.fetch_money_fund_rates()` 通常 500–600 只）按近 1 年收益粗排取前 `--shortlist` 只，再逐只取逐日万份收益、按 90 天实得年化精排；`--universe chaochao2` 是对照口径（`scripts/cash_pool_chaochao2.json`，招行 App「产品明细」截图录入的 100 支），只作对照。
- **不用朝朝盈2号，直接对接 90 天利润最高的货基**。理由：朝朝盈2号只是招行 App 的代销入口，不是产品范围；同一支基金代码在券商 App 基金频道 / 基金公司直销 / 银行 App 都能买，货基申购赎回费都是 0（销售服务费已含在净值里），所以排全市场、按 90 天实得年化取最高的即可，渠道无关。买入/换仓仍是人工（场外基金申购走销售系统，agent 只能下交易所委托）。
- **为什么不能直接照抄 App 的排名**：App 按 **7 日年化** 从高到低排，而 7 日年化是**滞后的滚动均值**——某只基金单日万份收益爆一下就能把它顶上去，7 天后掉回原形。实测某一期招行前三（博时合晶 1.40% / 中银机构 1.36% / 国投瑞银 1.34%）的 **90 天实得年化** 只有 1.27% / 1.12% / 0.99%，在全池 92 只里排 #25 / #66 / #84。按它买 = 专挑「刚爆完单日收益、即将回落」的那只。
- **口径**：窗口实得年化 = Σ每万份收益 / 10000 × 365 / 窗口**自然日**数（货基按自然日计息，分母用自然日跨度而非行数）；同时出 7 / 30 / 90 / 180 天四档，排名不稳的一眼能看出来。
- **快赎约束**：每支每日 1 万快赎额度是监管对货基 T+0 的行业规则（与渠道无关），所以推荐按 90 天排名**每支 1 万摊开**，不要单支堆大额——额度按支算，单支 2 万只有 1 万能 T+0。全市场口径下还会用申购快照筛掉「暂停申购 / 起购 >1 万 / 日限购 <1 万」的。
- 取数：`datasource.fetch_money_fund_hist(code, days=200)` 走东财 lsjz JSON（约 0.2 秒/只；`fund_money_fund_info_em` 同源但要翻 160 页、25–35 秒/只，只作回退）。东财对高频请求会限流，**冷缓存跑 60 只约几分钟到十几分钟**，之后按日缓存、当日重跑秒回。
- **它是现金层的执行层**：`cash_sleeve.py` 读这份排行，按 90 天口径取前 `semi_liquid.top_n` 支、每支 ≤1 万、跳过「暂停申购」，产出 `paper.cash_yield_plan()` 的 `sleeve_*` 字段。遇到排行缺失/超过 `max_stale_days`（5 天）→ fail-closed 整笔退回朝朝宝（`sleeve_fallback=True`，doctor 检查 1c 会 WARN）；**计息**走另一条路：`paper.accrue_cash_lots()` 以 `allow_stale=True` 切分，并把实际持仓写入账本 `sleeve_lots`（代码/金额/数据日），排行过期也按这份持仓算逐日万份收益，只有排行刷新（data_date 变化）才换仓。**刷新时点：每月 1 日月报跑一次 `& $py cash_pool.py`**（周决策与巡检都不刷；`rerank_days=30` 只是提示间隔），让掉队的基金退出；月报若刷新失败，沿用上一份排行并在月报标数据日。
- **进池子的只有「观望资金」**：`cash_yield_plan()` 先扣在途逆回购、场外申购占款，再扣 **T+0 保留 = max(建仓弹药, 场内挂单冻结)**，剩下的才交 `cash_sleeve` 切片。**建仓弹药 = max(0, 权益上限 − 现有场内权益)**；在挂买单冻结的钱成交后就是权益、本来就包含在弹药里，所以两者**取较大者、不能相加**（相加等于同一笔钱扣两遍，池子会少算约一个在挂买单金额）。权益上限优先级：调用参数 `cap_amount` > 账本 `meta["equity_cap"]`（周报每期写入）> 0（账本无此键时不扣）。理由是决策日下单、次一交易日开盘成交，而场外货基赎回 T+1（快赎未必赶得上开盘）；把「随时可能变成买单的钱」锁进 90 天池子，等于周一没钱可用。`cash_yield_plan()` 返回值里 `equity_cap`/`ammo`/`t0_keep`/`inhouse_reserved`/`otc_reserved` 可直接核对。

### 5.7c 逆回购：投资层脉冲择时（`scripts/repo_pulse.py`）

- **定位**：逆回购是**等报价的投资层工具**，不是现金层的常驻仓位——平时钱在货基池（1.3% 上下，示例；现利率取 `config.cash_rates()` 实时值），只有 GC001 报价明显高于「货基底仓 + 手续费 + 最低超额」时才临时打一笔。
- `python repo_pulse.py [--rate 2.2] [--days 1] [--amount 27770] [--bench 0.01306] [--need-build] [--term 7 --rate7 2.6]` → `out/repo_pulse.md` + `.json`；GO 退出码 0、NO-GO 1（可直接接定时任务）。
- **三条判断**：①`bench` = 半流动性货基加权 90 天年化（失败回退朝朝宝）——这是**机会成本**，不是 0；②费率按合约期限收（1 天 0.001%），折算年化时分母是**占款天数**：单笔按占款 1 天折算 ≈ 0.365%/年（每个交易日滚一遍，全年合计 ≈ 0.25%/本金），但**跨长假买 1 天期占款 9 天只收 1 笔费** ⇒ 0.0406%/年；③`edge_annual = 报价 − bench − 费年化`，要 ≥ `repo_pulse.min_edge_bp`（30bp）才 GO，且占款天数 ≤ `max_hold_days`（10 天，防止把建仓弹药锁死）。
- **期限规则（「尽量争取最大利润」，`repo_pulse.choose_term()`）**：**按同一窗口的净 %-天取最大**——每个候选算「报价 × 实际占款天数 + 底仓 × 剩余天数 − 手续费折算」，取最大者（同分取短期限）；1 天期与 7 天期（自动取 R-007 报价）默认参与比较，14 天期只在给了 `--rate14` 时参与（交易所取数只有 1 天期与 7 天期）；跨周末/长假按**实际占款天数**计息（到期日遇非交易日顺延、顺延期间也计息），用交易日历 `actual_days()` 算；**锁定期内可能要建仓时传 `--need-build` 强制 1 天期**，不把建仓弹药锁死；28 天期不自动推荐。
- **打平报价**（1 天、非周末，单笔口径）= bench + 0.365% + 30bp ≈ 1.97%（bench 1.31% 时）；**跨长假**（占款 9 天）打平降到 ≈ 1.65%。脉冲高发点是**季末/半年末/长假前的最后一个交易日**（历史 GC001 脉冲都在这些点；2024 年以来幅度明显变小，季末最后一日均值约 2%、极端日 3.6%–4.7%），不是天天滚。
- 不写账本、不占 `repo_open`：它只出 GO/NO-GO 与预期金额，实际下单仍是人工/定时任务提醒（模拟盘）。

---

### 5.8 留存与记录表（巡检数据怎么存）

`daily_log.md` 的列按「能不能直接写年报」定：
`日期 | 总资产(万) | 累计% | 回撤% | 仓位% | 当日盈亏 | 花费 | 沪深300 | 档位 | 现金净利率 | 涨停/跌停 | 巡检(高/总) | 决策`。
收益三件（资产/累计/当日盈亏）、风险两件（回撤/仓位）、成本一件（token 花费）、环境三件（指数/档位/利率）、情绪、合规（告警）、执行（决策日）——缺一块年报就写不全。
年报底稿：`python housekeeping.py --year 2026`（资产区间、年化、最大回撤、正收益天数、token 花费合计）。

**周汇总**：`python housekeeping.py --week [YYYY-MM-DD]` → `out/patrol_weekly_<该周周一>.md`。读的是**周一到周五**已落盘的 `daily_log.md` 行、账本 `nav`、以及 `daily_log` 里的利率列，不联网、不重算行情。内容 = 逐日净值/累计表 + 本周周涨跌/最大回撤/成交笔数 + 告警合计 + 利率起止。周报的「本周巡检汇总」一节直接调 `housekeeping.week_digest()`，口径与它同源。

每个工作日巡检（DSH 20:00 会话跑 `patrol.py`）末尾由 `patrol.run()` 调一次 `housekeeping.py`，做两件事：

1. **记一行账** → `$WORK_DIR/daily_log.md`：一天一行，只留**能事后复盘的关键值**
   （情绪三数/行业集中度/宏观三数/现金层加权净利率/持仓数与浮盈/触发的规则/数据日期）。
   这是唯一需要人读的巡检历史，读它就不用翻一堆 json。
2. **滚动清理** → `WORK_DIR/cache/` 里同一个 key 只留最近 2 天的快照（防跨零点重跑要昨天那份；
   `fuyao` 与 `cninfo` 的按日快照同规则）、`out/paper/doctor.md` 只保留最新一份、
   **`out/patrol_*.md` 保留 365 天**（一年，年报要用每天的事件明细）。
   报告文件（`out/paper/` 的 `weekly_*` / `report_*` / `orders_*` / `monthly_*`）按文件名日期保留
   **`config.PAPER["paper_keep_days"]`（400 天）**，出完年报底稿后再统一删；同数据日重跑前
   重跑前先备份到统一文件夹 `out/paper/_history/`（`weekly.py::_backup_before_write`：
   每个数据日只留一份原版、之后再重跑不覆盖备份，方便直接 diff），`_history/` 与
   `decision_*.json` 不自动删（年报后人工统一清）。
   设计前提：缓存 99% 的用途是「当天重跑别重复取数」，跨天复用没有意义——
   同花顺按日快照的日期写在文件名里，删旧快照不影响正确性。

**年报**：`python housekeeping.py --year 2026`（或 `--json`）输出聚合底稿——覆盖天数、
档位分布、逆回购（1/7/14/28 天期）/朝朝宝利率区间、涨停跌停合计、存疑天数、告警累计。**只聚合，不下结论**；
要事件明细回看当天的 `out/patrol_<日期>.md`，要净值曲线读 `ledger/state_50k.json` 的 `nav`（每个工作日巡检用 patrol.py 写入一条）。
`daily_log.md` 主文件只留当年，往年自动归档到 `WORK_DIR/_archive/daily_log_YYYY.md`，**归档永不删除**。

**读法**：要「最近一段时间的情绪/利率/集中度趋势」就 `daily_log.md`；
要「今天到底触发了什么」就 `out/patrol_<日期>.md`（当日巡检明细，`patrol_latest.json` 是同一份的机器可读版）；要「某个数的来源」才去翻 `provenance*.json`。

**存疑标记**：跌停/炸板池偶尔空返回（同花顺的接口行为）。
`sentiment.market_temp()` 会打 `suspect`，巡检表里该格显示 `39/0⚠`（示例），此时**不许**把跌停 0 当成「真的 0 家」去下结论。

### 5.9 信号面验证（只记录、不改规则；`scripts/news_eval.py` / `scripts/policy_map.py`）

- 事件面事后命中率：`& $py news_eval.py` → `out/news_eval.md` / `out/news_eval.json`（按 tier × 20/60/120 交易日超额，未到期标进行中，样本 <10 不下结论）。
- 政策面可证伪映射与前向跟踪：`& $py policy_map.py` → `out/policy_eval.md` / `out/policy_track.json`（政策→同花顺一级行业，20 个交易日后比行业等权超额；成员 <3 或未到期只标状态）。
- 两节都只进月报「信号面验证」，**不进打分、不改权重、不改买入线**。
