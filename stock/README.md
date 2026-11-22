# stock — 自建行情/财务数据脚本组

把取数逻辑收成**一个包**：49 个取数接口（直连东财/新浪/腾讯/中证/交易所/金十/财联社/百度…）+ 同花顺 fuyao，
统一缓存、统一错误处理、统一列名。新写的脚本都放进来，不再到处复制粘贴。

```
<本技能目录>\stock\
  .venv\              ← 本机解释器（Python 3.13 + pandas/requests/py_mini_racer；gitignored，不入库）
  stock\              ← 包（import stock）
    config.py         ← 路径/缓存/限流/UA，全部可环境变量覆盖
    cache.py          ← 磁盘缓存 + 溯源（provenance.jsonl）
    http.py           ← 统一 UA / 超时 / 退避重试 / 单域名限流 / 可注入 transport
    codes.py          ← 代码规范化：600519 ⇄ sh600519 ⇄ 1.600519 ⇄ 600519.SH
    patch.py          ← 数据清洗层：脏值转数字、列名归一、缺失值统一
    api.py            ← 统一门面（推荐入口）：daily / quote / fund_lsjz / ...
    cli.py            ← python -m stock <子命令>
    endpoints\        ← 取数接口层（49 个接口，唯一出口）
      __init__.py     ← 49 个接口的 `__all__` + `__version__`
      em.py           ← 东财系：基金列表/净值/费率/排行、数据中心宏观、估值明细
      sina.py         ← 新浪系：A 股/指数/债券日线、板块、财务、ETF 名单、交易日历
      misc.py         ← 中证指数、沪深交易所概况、金十、商务部、财联社、乐咕、百度、同花顺、蛋卷、巨潮全市场公告
      ohlc.py         ← 通用 K 线（腾讯行情，支持复权）
      common.py       ← 公共件：请求转出、字段解析、数值清洗
    skilldata\        ← 数据层（策略脚本用的那一套：缓存/重试/溯源/口径）
    sources\          ← 具体数据源的直连实现（业务稳定壳 + 东财/腾讯/新浪/同花顺）
      facade.py       ← 业务稳定壳（货基、基金净值、逆回购、财务…）
      eastmoney.py    ← 东财直连（基金 F10 净值、批量行情快照、K 线兜底）
      fuyao.py        ← 同花顺 Financial-API（财务/估值/新闻/涨停池/指数成分，需 key）
      tencent.py      ← 腾讯行情（日线主通道，支持复权）
      sina.py         ← 新浪行情（日线备用通道、实时快照）
    tests\            ← 离线单测（不联网）+ 联网冒烟（STOCK_LIVE=1）
  scripts\          ← 常驻脚本（demo_brief.py、daily_patrol.ps1）
  tools\            ← 运维/开发工具（smoke.py 全量冒烟），不参与日常运行
  data\             ← **所有跑出来的数据都在这里**
    cache\          ← 本包取数缓存（parquet/csv，按 kind+参数哈希分目录）
    out\            ← 本包产物与 provenance.jsonl
    shared\         ← **两个技能共用的一份取数缓存 + 每次全量取数的记录**
      cache\        ← datasource 缓存：<kind>\<key>__<date>.csv（唯一一份）
      snapshots\    ← <date>\manifest.json / summary.json / summary.md（当晚取了什么、覆盖是否完整）
      out\          ← prefetch 的 provenance
      latest.json / history.jsonl ← 最近一次 / 每次一行
    invest\         ← 投资技能运行目录（cache/ out/ paper/；**cache\ 是指向 shared\cache 的 junction**）
    logs\           ← 每日巡检日志
    archive\        ← 每日归档（按日期目录）
```

> `invest\cache` 已用 Windows 目录联接（junction）指向 `data\shared\cache`，
> 所以两边读到的、写下的都是同一份缓存。迁移/重建用
> `scripts\setup_shared_cache.ps1`（默认 dry-run，`-Execute` 才动手；重复跑安全）。

## 快速开始

```powershell
# 1) 让 Python 找到包（包根是 <本技能目录>\stock，包名是 stock）
$env:PYTHONPATH = "<本技能目录>\stock"

# 2) 自检（不联网）：看源可用性、缓存状态
<本技能目录>\stock\.venv\Scripts\python.exe -m stock doctor

# 3) 常用
<本技能目录>\stock\.venv\Scripts\python.exe -m stock daily 600519 -n 5
<本技能目录>\stock\.venv\Scripts\python.exe -m stock index 000300 -n 5
<本技能目录>\stock\.venv\Scripts\python.exe -m stock etf 510300 -n 5
<本技能目录>\stock\.venv\Scripts\python.exe -m stock quote 600519 000300.SH 510300
<本技能目录>\stock\.venv\Scripts\python.exe -m stock fund 004985 -n 5
<本技能目录>\stock\.venv\Scripts\python.exe -m stock money --top 10
<本技能目录>\stock\.venv\Scripts\python.exe -m stock repo 204001 -n 5
<本技能目录>\stock\.venv\Scripts\python.exe -m stock cache            # 统计
<本技能目录>\stock\.venv\Scripts\python.exe -m stock cache --clear    # 清空
```

代码里：

```python
import sys; sys.path.insert(0, r"<本技能目录>\stock")
from stock import api

df = api.daily("600519", start="2026-10-01")        # date/open/close/high/low/volume
snap = api.quote(["600519", "000300.SH"])            # 多源回退
nav = api.fund_lsjz("004985", page_size=30)          # 货基：wanfen/y7_pct/buy/sell
rows = api.ts.financials("600519.SH")                # 同花顺年报（需 key）
```

## 数据源分工

| 源 | 管什么 | 是否需 key | 备注 |
|---|---|---|---|
| 接口层 `endpoints/`（49 个接口，见下节） | 货基 545 只、基金净值/费率/排行、逆回购、交易日历、宏观、财务、板块、快讯 | 否 | 直连各数据源；东财 push2 系在本机**被网络重置**，行情走腾讯 |
| 腾讯 `web.ifzq.gtimg.cn` | **日线主通道**（前/后复权、640 根/次） | 否 | 稳定；支持 `start/end`（`YYYY-MM-DD`） |
| 新浪 `money.finance.sina.com.cn` | 日线备用、实时快照 | 否 | 不复权、最近 640 根 |
| 东财 `push2.eastmoney.com` | 批量行情快照（≤100 只/批） | 否 | 字段编号 f2/f3/f12… |
| 东财 `fund.eastmoney.com` | 基金 F10 历史净值（0.2 秒/只） | 否 | 必须带 Referer |
| 同花顺 fuyao | 财务/估值/公告新闻/涨停池/指数成分 | **是** | key 见下 |

**日线门面的回退顺序**：腾讯 → 新浪 → 东财 K 线 → 接口层。哪个能用用哪个，失败会带上每个源的错误信息（不静默降级）。

## 取数接口层（49 个接口）

按数据来源分成四组，统一从 `stock.endpoints` 出口：

```python
from stock import endpoints as ep
ep.fund_money_fund_daily_em()      # 货基快照（545 只，含 7 日年化）
ep.bond_zh_hs_daily("sh204001")    # 逆回购 GC001 日线
ep.tool_trade_date_hist_sina()     # 交易日历
```

| 层 | 文件 | 说明 |
|---|---|---|
| 唯一入口 | `stock/endpoints/__init__.py` | `__all__` 49 个接口 + `__version__`（当前 `2026.11`） |
| 实现分组 | `stock/endpoints/{em,sina,misc,ohlc}.py` | 按数据源分组；全部走 `stock.http`（UA/限流/退避） |
| 在线冒烟 | `tools/smoke.py` | 49 个接口挨个调一遍，打印 shape/耗时 |
| 离线自检 | `stock/tests/test_endpoints.py` | 42 个名字齐全、分组齐全、不直接依赖 HTTP 库 |

- **想加接口**：在 `endpoints/<组>.py` 里实现 → 把名字加进 `endpoints/__init__.py` 的 `__all__` → 跑一次 `tools/smoke.py`。
- **巨潮全市场公告**：`ep.stock_notice_report(date)` 一次取全 A 股当日全部公告（三市合并、约 1,300–2,100 条、约 10 秒），
  供策略侧做「候选池之外」的事件面扫描；按披露日缓存到数据层 `cninfo_market/<date>.csv`（`ds.fetch_market_announcements`）。
- **基金规模双源**：`ep.fund_individual_basic_info_xq(代码)`（蛋卷，主源，历史上会被风控返回空数据）
  - **A 股基础信息 / 历史退市 / 曾 ST**：`ds.fetch_stock_basic()`（含上市日、退市日，baostock `query_stock_basic`）、`ds.fetch_delisted()`（约 300 只退市股，按退市日倒序）、`ds.fetch_st_intervals(code)` / `ds.is_st_on(code, day)`（日线 isST 合并成区间）。随 prefetch 落缓存；用来量化回测的幸存者偏差、还原「那天还在不在市 / 是不是 ST」。
→ `ep.fund_scale_em(代码)`（东财 fundmobapi，兜底，债券/货币基金也覆盖）；数据层
  `ds.fetch_fund_scale()` 自动逐级回退，`fund_scale_source()` 告诉你实际用的哪个源。
- **改取数逻辑**：直接改 `endpoints/` 对应分组。
- **验证**：`python <本技能目录>\stock\tools\smoke.py`（联网）；`python -m unittest discover -s stock\tests`（离线）。
- **兜底与域名冷却**：ETF 快照主源（东财 push2）失败 → 新浪 ETF 列表（927 只，成交量「份」÷100 转「手」）；
  ETF 历史失败 → 腾讯 K 线（支持 qfq/hfq、日/周/月）→ 新浪 ETF 日线（不复权，周/月用 `W-FRI`/`ME` 重采样）。
  失败过的东财域名进 10 分钟冷却表（`_HOST_DOWN`），冷却期内直接跳过：首调约 10–12 秒，之后 0.4 秒。
  乐咕在函数内做 5 次退避，仍失败时返回空表（fail-soft，不抛异常）。

## 和策略脚本的关系

`stock/skilldata/datasource.py` 是给策略脚本用的数据层（缓存/重试/溯源/口径都在这里）。
策略脚本自己的 `config.py` 末尾一行把它挂进 `sys.modules`：

```python
from stock import skilldata as _skilldata
_skilldata.install("invest", _sys.modules[__name__])
```

`install()` 把**技能自己的 config 模块**注入实现（缓存目录/产出目录/可选属性都从技能 config 读），
并把实现/`codes`/`fuyao` 挂到 `sys.modules` 的惯用名下——策略代码照旧
`import datasource as ds`、`import codes`、`import fuyao`，不需要改一行。挂上去的是**实现模块本体**，
所以 `ds.set_cache_hook(...)` 这类注入直接有效。

- **改取数逻辑 → 改 stock**，改完两个技能同时生效；技能侧只留策略口径（例：行业归一表走
  `datasource.set_market_hook()`，不塞进 stock）。
- **改技能 config 里的数据口径**（缓存目录、TTL、可选属性）仍在技能 `config.py` 里，`install()` 实时读取。
- 运行数据在 `<本技能目录>\stock\data\invest`（技能 config 的 `WORK_DIR` 默认值，见下节），
  但两边的 `cache\` 是同一份（junction → `data\shared\cache`），**取数只取一次**。

## 同花顺 key

按顺序找：环境变量 `HITHINK_FINANCE_API_KEY` / `FUYAO_API_KEY` →
`%APPDATA%\hithink-finance\credentials.env` → `<本技能目录>\stock\data\fuyao.key`。
`python -m stock doctor` 里的 `fuyao.key_source` 会告诉你 key 从哪来（只报来源，不打印值）。

## 约定

1. **取数只走 `api`/`endpoints`/`sources`，不在脚本里直接 `requests`**——否则缓存、限流、UA、重试全都绕过了。
2. 新增源 = `sources/xxx.py` + 在 `sources/__init__.py` 的 `_NAMES` 注册；对外暴露用 `api` 门面。
3. 列名统一 ascii：`date/open/high/low/close/volume/amount`、基金 `nav/acc_nav/wanfen/y7_pct`。
4. 缓存默认 1 天；`STOCK_REFRESH=1` 强制刷新；`STOCK_HOME` 可把整个工作目录挪走。
5. 依赖：`pandas`（硬）、`requests`、`py_mini_racer`（新浪日线解密）、`beautifulsoup4`/`lxml`（表格解析）、
   `openpyxl`/`xlrd`（Excel 解析）、`pyarrow`（可选，有则 parquet）。
6. 新脚本（策略、回测、报表）建议放 `<本技能目录>\stock\scripts\`，只 `import stock`，别再复制取数实现。

## 测试

```powershell
# 离线（改完代码必跑）
<本技能目录>\stock\.venv\Scripts\python.exe -m unittest discover -s <本技能目录>\stock\stock\tests -p "test_*.py" -v
# test_endpoints.py 专门验证「49 个接口齐全、分组齐全、不直接依赖 HTTP 库」

# 联网冒烟（每个源打一发，约 1 分钟）
$env:STOCK_LIVE="1"; <本技能目录>\stock\.venv\Scripts\python.exe -m unittest discover -s <本技能目录>\stock\stock\tests -p "test_live.py" -v
```

## 统一取数（`stock prefetch`，一条命令）

两个技能晚上要的是同一批数据：全市场快照、指数/成分、ETF 行情与日线、场外债基池、
现金层利率……以前各取各的（两份缓存、同一份行情取两遍，19:00 常因取数超时）。
现在只跑一条命令，把两边当晚要用的数据**一次取全**，统一落在 `data\shared\cache`，
并写一份「今晚取了什么、覆盖是否完整」的记录：

```powershell
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prefetch            # 全量（默认 8 线程，约 10-15 分钟）
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prefetch --dry-run  # 只打印计划
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prefetch --verify-only   # 只做覆盖自检
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prefetch --skip-stocks   # 演练/救急：跳过个股逐只预热
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prefetch --with-weekly   # 附带货基历史（慢，决策日用）
```

- 覆盖：参考快照（交易日历/板块/指数/ETF 全表/申购表/当日净值/货基排行/利率）、
  指数日线 5 条 + 沪深300/中证500 成分（≈800 只）、ETF 日线（当日成交额 ≥0.3 亿 ∪ pin 名单，≈600 只）、
  场外债基候选（复现 invest OTC 的 L1–L4 → 累计净值 → L5/L6 去重 → 规模 → 费率，约 3200 次）、
  个股 800 只 × 日线/财务/估值（市盈率TTM + 市净率）。
  「场外规模」是 best-effort：主源雪球需登录态，prefetch 先探 120 只、可用率
  <20% 即跳过剩余（funnel 记 `L7_scale_probe`/`L7_scale_skipped`），消费侧查不到不淘汰。
- 记录：`data\shared\snapshots\<date>\manifest.json`（每条 kind/key 的 ok/rows/耗时/`max_bar_date`/错误，
  分组、覆盖自检、OTC 漏斗计数）、`summary.json`、`summary.md`，以及 `data\shared\latest.json`、
  `snapshots\history.jsonl`；数据层溯源在 `data\shared\out\prefetch_provenance.json`。
- 覆盖/bar 自检：`daily/fin ≥200`、`etf_daily ≥20`、指数/板块/日历/ETF 池 ≥1；指数末根 bar 必须等于
  应有交易日（否则当天数据准备会清缓存重取）；ETF 日线末根 bar 同样校验（最旧一只落后即报）。
  不足 → 退出码 2，整体异常 → 1，OK → 0；**盘中/凌晨 15:05 前写当天键 → 退出码 3**（用 --date 回放或
  --allow-intraday 仅演练放行）。
- 数据日 = 真实今天（与两技能默认一致）；`--date` 可钉住（演练回放），`--force` 忽略当日缓存重取。
- 启动时清掉共享缓存里当天的 `*.failed` 负缓存标记——一次失败不该毒死当晚全链。`n- 所有写盘都是原子写（tmp+`os.replace`）：prefetch 内部三组并发、20:00 会话只读，不会读到半截 CSV/JSON。

## 每日巡检与记录（交易日 19:00）

计划任务 **`DSH-DailyPatrol`**：周一至周五 **19:00**（`StartWhenAvailable` 开机补跑，
给冷启动 ~16 分钟的 prefetch 留出到 20:00 会话的余量），
调用 `<本技能目录>\stock\scripts\daily_patrol.ps1 -Ledger 50k`，用 `<本技能目录>\stock\.venv\Scripts\python.exe` 串行跑四步
（只取数 + 记录，**不算账本**；账本由 20:00 的 DSH 巡检会话结算）：

| 步骤 | 内容 |
|---|---|
| `prefetch` | `python -m stock prefetch --workers 8`（**唯一取数点**：一次取全两技能当晚要用的数据） |
| `invest_data` | `run.py --capital 50000`（读热缓存 → 漏斗 → 计划；0 token；**不带 `--refresh`**） |
| `prune` | `python -m stock prune --days 90`（保留期清理，删失败不影响巡检结论） |

**记录全部落在 `<本技能目录>\stock\data\`，且每个文件名都带日期**：

| 位置 | 文件 | 内容 |
|---|---|---|
| `data\shared\snapshots\<date>\` | `manifest.json` / `summary.json` / `summary.md` | **今晚取了什么**（kind/key/行数/耗时/bar/覆盖自检/OTC 漏斗） |
| `data\shared\` | `latest.json`、`snapshots\history.jsonl` | 最近一次 / 每次一行 |
| `data\logs\` | `patrol_<yyyyMMdd_HHmmss>.log` | 单次巡检完整日志（每行带 `yyyy-MM-dd HH:mm:ss`） |
| `data\logs\` | `<stamp>_<step>.out.log` / `.err.log` | 每步 stdout/stderr，首行带日期与 rc |
| `data\logs\` | `summary_<yyyy-MM-dd>.json` | 当日机器可读摘要（20:00 简报读它；含 `prefetch_data` 段） |
| `data\logs\` | `history.log` | 每次一行：时间 / ok / 耗时 / prefetch_rc / 两步 rc |
| `data\logs\` | `last_run.json`、`latest.log` | 最近一次（含 `retention_days`、`prefetch`） |
| `data\archive\<yyyy-MM-dd>\` | 当日巡检日志、`summary.json`、`shared_prefetch_manifest.json`、当日报告/计划（有则拷）；**不拷账本**（由 DSH 结算），过期文件跳过并写 `_archive_notes.json` | 单日记录自包含 |

**保留期**：`prune` 对共享取数缓存按 **7 天**（`STOCK_SHARED_CACHE_RETENTION_DAYS`，一天约 200MB）清理，
其余 `cache/out/logs/archive` 按 **90 天**（`STOCK_RETENTION_DAYS`）；`paper/`（账本、持仓、成交）、
`*.key`、`state*.json`、`*_latest.json`、`*.lastgood.json` 一律不动。`daily_patrol.ps1 -RetentionDays N`
可改（同时作用于 prune 与归档清扫）。先看要删什么：

```powershell
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prune --dry     # 只统计
<本技能目录>\stock\.venv\Scripts\python.exe -m stock prune           # 真删（默认 90 天）
```

`-Root <dir>` 用于演练（不碰真实数据、不执行 prune；会在演练根里自建 `shared\cache` 与两个 junction，并把 `STOCK_OUT_DIR` 也指到演练根）；
`-SkipPrefetch` / `-SkipInvest` / `-SkipPrune` 用于救急（必须声明成 switch，未声明参数会被 PowerShell 5.1 静默忽略）；`-PrefetchTimeoutSec`（默认 2400）、`-Workers`（默认 8）可调。

> 计划任务本体仍只指向 `daily_patrol.ps1`，改步骤不用重注册；当前生产任务：**Mon-Fri 19:00、`ExecutionTimeLimit` 3h**。
> 若某天 prefetch 明显变慢（19:25 前还没收尾），用 `scripts\register_task.ps1 -Time 18:20` 再提前。

## 已知坑（都已在代码里处理）

- **东财 push2his 日线在本机不可用**（`RemoteDisconnected`），所以日线主通道是腾讯；
- 腾讯 `param` 的 `begin/end` 必须是 `YYYY-MM-DD`，给未来日期直接返回空；
- 新浪 K 线**不复权**、只给最近 640 根；
- 东财快照 `f12` 可能返回数字（`300`），必须 `zfill(6)`；
- 货基快照的 `'---'` 要转 NaN，7 日年化列名带日期前缀（取最后一组）；
- PowerShell here-string 里写 `\"` 会 `SyntaxError`，脚本一律落 `.py` 再执行；
- `PYTHONPATH` 要用 `<本技能目录>\stock`（不要用盘符根目录当 PYTHONPATH，否则目录名会被当成命名空间包遮住真包）。
