#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件面扫描（公告 / 个股新闻 / 异动 / 除权）—— 决策日 T2 阶段跑，纯代码、无 LLM。

为什么要这一层
--------------
技能的漏斗和四维打分只看财务与估值，**看不见事件**：减持、立案、问询、诉讼、
商誉减值、解禁、评级下调这些，全都发生在两次财报之间。以前这一格是空的，
财报要等几个月，新闻只要一天。

什么时候跑
----------
**只在决策日**（每周六 20:00 那一轮的 T2 阶段；**周六不重取行情，只采周末新增公告/新闻**），**不是每日巡检**。
新闻的价值 3 天就衰减一大半，每天扫一遍却不用等于烧额度；交易日 20:00 的巡检只做
账本/数据/任务书体检，不看新闻（见 `references/news-rules.md` 第二章）。

数据全部来自同花顺 iNews（`fuyao.py`）。没 key / 接口挂了 → 整层跳过，
不写空壳文件，主链路照常跑（fail-soft）。

输入
----
- `out/stage3_plan.json`：当前持仓（个股）
- `out/stage1_candidates.csv`：候选（可选，最多取 NEWS["max_codes"] 只）

输出
----
- `out/news_events.csv`：一条事件一行（可追溯、可比对）
- `out/news_brief.md`：给人/给 Lead 看的简报，高优先级排最前

用法
----
    python scripts/news.py                # 扫默认窗口（NEWS["days"]）
    python scripts/news.py --days 7       # 只扫近 7 天
    python scripts/news.py --limit 5      # 只扫 5 只（调试用）
    python scripts/news.py --offline      # 断网时出空简报，不报错
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import fuyao

NEWS = getattr(config, "NEWS", {}) or {}
COLS = ["layer", "code", "name", "kind", "level", "tag", "date",
        "title", "source", "url",
        "r_score", "c_score", "net_score", "tier", "mult"]
LEVEL_ORDER = {"高": 0, "中": 1, "低": 2, "": 3}


# ---------------------------------------------------------------- 纯函数（可测）
# 噪音词：出现这些说明讲的是「谁在买卖」或「别人怎么看」，不是公司自己的事件
NOISE = ["基金经理", "公募", "私募", "调仓", "重仓", "持仓曝光", "龙虎榜",
         "研报", "点评", "策略会", "融资余额", "北向资金", "ETF", "指数纳入",
         "资金流向", "主力资金", "复盘", "评级", "机构调研", "解除质押"]
HARD = set(NEWS.get("hard_keywords") or [])
# 实质词：出现这些说明讲的是公司自己发生了什么事（哪怕标题里带「机构调研/评级」也不降级）
SUBSTANTIVE = HARD | {"临床试验", "重大合同", "中标", "签约", "收购", "定增",
                      "股权激励", "获批", "注册", "许可", "分红", "回购",
                      "业绩预告", "业绩快报", "重大资产重组", "对外投资"}

# 否定词：关键词前面出现这些，说明是「澄清/否认/假设」，不是事件本身
# 例：「公司生产经营正常，不存在退市风险」→ 不算「退市风险」
NEG_BEFORE = ("不存在", "不涉及", "未涉及", "没有", "否认", "辟谣", "无此事",
              "并非", "不代表", "尚未", "未见")


def _negated(t: str, kw: str) -> bool:
    """关键词前 12 字内出现否定词 → 不算命中。"""
    i = (t or "").find(kw)
    while i >= 0:
        head = (t or "")[max(0, i - 12):i]
        if any(n in head for n in NEG_BEFORE):
            return True
        i = t.find(kw, i + 1)
    return False


def is_noise(title: str) -> bool:
    t = title or ""
    return any(w in t for w in NOISE)


def level_of(title: str) -> tuple[str, str]:
    """按**标题**判定影响级别，返回 (级别, 命中词)。

    只吃标题不吃摘要：摘要里经常顺带提到别家公司的减持/诉讼，用摘要会大面积误报。
    标题里出现噪音词（基金调仓、研报点评…）直接降到「低」——那是观点，不是事实。
    """
    t = title or ""
    noise = is_noise(t)
    if noise and "：" in t:
        tail = t.split("：", 1)[1]
        if any(kw in tail for kw in SUBSTANTIVE):
            noise = False           # 冒号后是公司自己的实质事件：噪音词只说明来源

    for kw in NEWS.get("high_keywords") or []:
        if kw in t and not _negated(t, kw):
            if kw in HARD:
                return "高", kw          # 硬风险词：噪音门不放行
            return ("低" if noise else "高"), kw
    for kw in NEWS.get("mid_keywords") or []:
        if kw in t and not _negated(t, kw):
            return ("低" if noise else "中"), kw
    return "低", ""


def _day(s) -> str:
    return str(s or "")[:10]


# ============================================================ 双向评分（R 风险 / C 催化）
# 打分口径的来历：见 references/news-rules.md（12 期校准，2026-10-29）。
# 原则：R 扣分只能"否决/降权"，C 加分只能"放大权重上限 1.30"，谁都不能改变资格。
# 校准值与先验值分开标注：`校准` = 两池 12 期实测标定（references/news-rules.md）；
# `先验` = 样本不足、按常识保守取值，标 [先验] 的权重不许对外说成「实测」。
R_EVENT = {                      # 事件基线分（0–60）
    # —— 校准：坏股池原始超额（20 日，相对沪深300）
    "立案调查": 60,              # -23.4%（n=6，跌超5% 100%）
    "业绩预亏": 60,              # -12.4%（n=2）
    "会计问题": 55,              # -20.8%（n=2，跌超5% 100%）
    "退市风险": 45,              # -10.4%（n=3）
    "诉讼仲裁": 45,              # -14.3%（n=6，误杀 0%）
    "减持": 20,                  # 坏股池 -21.8%、优质池 -0.8% → 情境依赖，只降权不否决
    "质押冻结": 6,               # 两池均无稳定负超额（DiD +3.6% / +6.2%）→ 噪音
    "行政处罚": 8,               # 坏股池 +2.9%、误杀 50% → 几乎无信息量
    "担保": 5,                   # 噪音
    "资产减值": 5,               # 噪音
    # —— [先验] 未取得样本
    "商誉减值": 30, "监管措施": 15, "解禁": 6, "再融资": 6,
}
C_EVENT = {                      # 催化分（0–22）
    # —— 校准：优质池原始超额
    "回购": 14,                  # +3.26%（n=9，唯一稳定正超额）
    "分红": 12,                  # +3.11%（n=9）
    "业绩快报": 10,              # +2.80%（n=6，坏股池）
    "股权激励": 8,               # +1.07%（n=3）
    "增持": 5,                   # -1.94%（n=3，不加分也行，留一点）
    # —— [先验]
    "重大合同": 6, "业绩预增": 10, "中标": 5,
}
# 一票否决（不看总分，直接出局）：伤的是"还能不能持有"，不是"值不值"
VETO_TAGS = {"立案调查", "退市风险", "会计问题"}
# 事实确定性：按来源分层（0–15）
SRC_TIER = [("公告", 15), ("巨潮", 15), ("问询", 12), ("交易所", 12),
            ("证券时报", 8), ("上海证券报", 8), ("中国证券报", 8),
            ("同花顺", 8), ("东方财富", 8), ("新浪", 6), ("财经", 6),
            ("快讯", 4), ("自媒体", 0)]
# 噪音/观点类来源：不给分（讲"谁在买卖/别人怎么看"的，不是公司自己的事实）
OPINION_SRC = ("研报", "评级", "点评", "策略", "龙虎榜", "资金")


def _days_between(a: str, b: str) -> int | None:
    try:
        return (dt.date.fromisoformat(_day(b)) - dt.date.fromisoformat(_day(a))).days
    except Exception:
        return None


def _tag_key(tag: str) -> str:
    """把标题级标签归到评分表的键（除权事件给的是「分红/送股/配股」这种组合标签）。"""
    t = tag or ""
    if any(w in t for w in ("分红", "送股", "配股", "权益分派", "转增")):
        return "分红"
    if any(w in t for w in ("减持", "减仓")):
        return "减持"
    if any(w in t for w in ("增持",)):
        return "增持"
    if "回购" in t:
        return "回购"
    return t


def source_score(source: str, title: str) -> int:
    """事实确定性（0–15）。观点类来源（研报/评级/龙虎榜/资金流）直接 0。"""
    s = f"{source or ''}{title or ''}"
    if any(w in s for w in OPINION_SRC):
        return 0
    for kw, v in SRC_TIER:
        if kw in s:
            return v
    return 4


def fresh_score(day: str, asof: str) -> int:
    """时效（0–15）：≤3 天 15，≤7 天 12，≤30 天 8，≤90 天 4，更早 0。"""
    n = _days_between(day, asof)
    if n is None:
        return 0
    return 15 if n <= 3 else 12 if n <= 7 else 8 if n <= 30 else 4 if n <= 90 else 0


def quant_score(title: str) -> int:
    """财务可量化（0–10）。**不进 R/C 公式**，只供简报判断"这条能不能算出数"。"""
    t = title or ""
    has_pct = any(u in t for u in ("%", "％", "个百分点"))
    has_money = any(u in t for u in ("万元", "亿元", "元", "万股"))
    if has_pct and has_money:
        return 10
    return 6 if (has_pct or has_money) else 2


def r_score(event: dict, asof: str) -> int:
    """风险分 0–100 = 事件基线（0–60）+ 0.5×（事实确定性 0–15 + 时效 0–15）。

    **没有风险事件时 R 恒为 0**：来源与时效只放大「已经发生的风险」，
    不能凭空制造风险（否则一条普通公告也会被降权——这是 2026-10-29 修掉的 bug）。
    口径来历：`references/news-rules.md`（两池 12 期校准）。
    """
    tag = _tag_key(event.get("tag"))
    base = R_EVENT.get(tag, 0)
    if base == 0 and event.get("level") == "高":
        base = 40                    # 命中高危词但不在表里：保守当强风险
    if base == 0:
        return 0
    r = base + 0.5 * (source_score(event.get("source"), event.get("title"))
                      + fresh_score(event.get("date"), asof))
    return int(min(round(r), 100))


def c_score(event: dict, asof: str) -> int:
    """催化分 0–30 = 事件强度（0–22）+ 来源（0–4）+ 时效（0–4）。"""
    tag = _tag_key(event.get("tag"))
    t = event.get("title") or ""
    base = C_EVENT.get(tag, 0)
    if base == 0 and ("预增" in t or "扭亏" in t):
        base = 10
    if base == 0:
        return 0
    c = base + source_score(event.get("source"), t) * (4 / 15.0) \
        + fresh_score(event.get("date"), asof) * (4 / 15.0)
    return int(min(round(c), 30))


def tier_of(r: int, c: int, tag: str) -> tuple[str, float]:
    """按 R 优先处置（利好不能冲抵利空）。返回 (档位, 权重系数)。

    阈值来历：单票上限 = 本金 10%；5 万本金下单票 5,000 元，
    一次往返（5,000 元单）成本约 0.2% → 事件要对单票产生 >0.2% 影响才值得动手。
    校准里强正面事件只有 +1.1%~+3.3%、强风险事件 -10%~-23%，
    所以「否决要狠、加分要轻」，加分上限只到 1.15。
    """
    if tag in VETO_TAGS or r >= 60:
        return "否决", 0.0
    if r >= 40:
        return "降权40", 0.6
    if r >= 25:
        return "降权15", 0.85
    if c >= 22:
        return "加分15", 1.15
    if c >= 15:
        return "加分10", 1.10
    return "不干预", 1.0


def score_event(event: dict, asof: str) -> dict:
    r = r_score(event, asof)
    c = c_score(event, asof)
    tier, mult = tier_of(r, c, _tag_key(event.get("tag")))
    return {"r_score": r, "c_score": c, "net_score": c - r,
            "tier": tier, "mult": mult,
            "r_basis": (f'基线 {R_EVENT.get(_tag_key(event.get("tag")), 0)}/60'
                        f'+来源{source_score(event.get("source"), event.get("title"))}/15'
                        f'+时效{fresh_score(event.get("date"), asof)}/15'),
            "quant": quant_score(event.get("title"))}


def code_scores(path: str | None = None) -> dict:
    """按 code 聚合 `news_events.csv`，给决策层用：`{code: {r,c,tier,mult,n,tags,name}}`。

    聚合规则（为什么这样定）：
      * R 取「最坏那一条」，不叠加——同一件事被多家媒体报道不该被罚三次；
      * `mult` 取最小值：同时有利多与利空时风险优先（利好不能冲抵利空）；
      * 只有完全没有风险事件时才可能拿到加分（mult > 1）；
      * 任一事件 `tier=否决` → 该 code 整体否决（mult = 0）。
    读不到文件返回 `{}`：事件面是可选增强，不能阻断主流程。
    """
    p = path or os.path.join(config.OUT_DIR, NEWS.get("out_csv") or "news_events.csv")
    if not os.path.isfile(p):
        return {}
    try:
        with io.open(p, "r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("code") or "").strip()]
    except OSError:
        return {}
    out: dict = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        try:
            rs = int(float(r.get("r_score") or 0))
            cs = int(float(r.get("c_score") or 0))
            mult = float(r.get("mult") or 1.0)
        except (TypeError, ValueError):
            continue
        cur = out.get(code)
        if cur is None:
            cur = out[code] = {"r": 0, "c": 0, "tier": "不干预", "mult": 1.0,
                               "n": 0, "tags": [], "name": (r.get("name") or "").strip()}
        cur["n"] += 1
        cur["r"] = max(cur["r"], rs)
        cur["c"] = max(cur["c"], cs)
        tier = (r.get("tier") or "").strip() or "不干预"
        tag = (r.get("tag") or "").strip()
        if mult <= 0:
            cur.setdefault("_veto", []).append((mult, tier))
        elif mult < 1.0:
            cur.setdefault("_risk", []).append((mult, tier))
            if tag and tag not in cur["tags"]:
                cur["tags"].append(tag)
        elif mult > 1.0:
            cur.setdefault("_bonus", []).append((mult, tier))
    for c in out.values():
        # 风险优先；没有风险事件时才给加分（中性事件不该把加分稀释掉）
        veto = c.pop("_veto", None)
        risk = c.pop("_risk", None)
        bonus = c.pop("_bonus", None)
        if veto:
            c["mult"], c["tier"] = min(veto)[0], "否决"
        elif risk:
            c["mult"], c["tier"] = min(risk)
        elif bonus:
            c["mult"], c["tier"] = max(bonus)
    return out


def in_window(day: str, cutoff: str) -> bool:
    """day 是否落在 [cutoff, 今天]。日期不合法/为空一律不算（宁可漏，不可脏）。"""
    d = _day(day)
    if len(d) != 10 or d[4] != "-" or d[7] != "-":
        return False
    if not (d[:4] + d[5:7] + d[8:10]).isdigit():
        return False
    return d >= cutoff


def sort_events(events: list[dict]) -> list[dict]:
    """高→中→低，同级按日期新→旧，再按代码，保证可复现。"""
    return sorted(events, key=lambda e: (LEVEL_ORDER.get(e.get("level", ""), 3),
                                         _neg_date(e.get("date")),
                                         e.get("code") or "",
                                         e.get("title") or ""))


def _neg_date(d) -> str:
    """把日期转成「越新越小」的排序键（9-每一位，纯数字，可与 99999999 直接比）。"""
    s = _day(d).replace("-", "")
    if len(s) != 8 or not s.isdigit():
        return "99999999"
    return "".join(str(9 - int(c)) for c in s)


def render_brief(events: list[dict], cutoff: str, scanned: int,
                 ind: dict | None = None, skipped: list[str] | None = None,
                 held: list | None = None, pool_n: int | None = None,
                 ann_n: int | None = None, ann_days: int | None = None,
                 policy: list[dict] | None = None) -> str:
    """生成 markdown 简报。**不带墙钟时间**，保证同样输入逐字一致。"""
    rows = sort_events(events)
    asof = max([_day(e.get("date")) for e in rows] or [cutoff])
    if asof == cutoff and not rows:
        asof = cutoff
    out = [f"# 事件面简报（窗口 {cutoff} ~ {asof}）", "",
           f"扫描 {scanned} 只；命中 {len(rows)} 条事件。", ""]
    pol = list(policy or [])
    if pol:
        out.append(f"## 政策面 — {len(pol)} 条（只做背景，不改权重）")
        for e in pol:
            out.append(f'- {e.get("date")} ｜{e.get("tag")}｜{e.get("title")}')
        out.append("")
    if ann_n is not None:
        out.append(f"公告流：读本地全市场公告缓存（{ann_days} 个交易日），"
                   f"覆盖 {pool_n} 只池内标的，命中 {ann_n} 条。")
        out.append("")
    for lv, label in (("高", "高（可能改变结论，必须在汇报里点名）"),
                      ("中", "中（记一笔，看后续）"),
                      ("低", "低（只归档）")):
        sub = [e for e in rows if e.get("level") == lv]
        if not sub:
            continue
        out.append(f"## {label} — {len(sub)} 条")
        for e in sub:
            who = f'{e.get("code")} {e.get("name") or ""}'.strip()
            flag = "⚠️ " if lv == "高" else ""
            out.append(f'- {flag}{e.get("date")} **{who}**｜{e.get("kind")}'
                       f'｜{e.get("tag") or "—"}｜{e.get("title")}')
            out.append(f'  R{e.get("r_score")} / C{e.get("c_score")}'
                       f' → **{e.get("tier")}**（系数 {e.get("mult")}）')
            if e.get("url"):
                out.append(f'  <{e["url"]}>')
        out.append("")
    # 处置汇总：按 R/C 落到哪一档（这就是要交给 Lead 的结论）
    tiers: dict[str, list[dict]] = {}
    for e in rows:
        tiers.setdefault(e.get("tier") or "不干预", []).append(e)
    if any(k != "不干预" for k in tiers):
        out.append("## 处置汇总（R 优先，利好不冲抵利空）")
        for name in ("否决", "降权40", "降权15", "加分15", "加分10"):
            sub = tiers.get(name) or []
            if not sub:
                continue
            code_list = "、".join(sorted({f'{e.get("code")} {e.get("name") or ""}'.strip()
                                          for e in sub})[:12])
            out.append(f"- **{name}**（{len(sub)} 条）：{code_list}")
        out.append("")
    # 持仓状态：**全部**持仓都列出来（没有事件的也要列，否则看不出「扫过了、干净」）
    hold = [e for e in rows if e.get("layer") == "持仓"]
    keys = {(e.get("code"), e.get("name") or "") for e in hold}
    for c, n in (held or []):
        keys.add((c, n))
    if keys:
        out.append("## 持仓状态")
        seen: dict[str, dict] = {}
        for e in hold:
            k = (e.get("code"), e.get("name") or "")
            st = seen.setdefault(k, {"高": 0, "中": 0, "低": 0, "last": ""})
            st[e.get("level") or "低"] = st.get(e.get("level") or "低", 0) + 1
            st["last"] = max(st["last"], _day(e.get("date")))
        for k in sorted(keys):
            st = seen.get(k) or {"高": 0, "中": 0, "低": 0, "last": ""}
            flag = "⚠️ " if st["高"] else ("· " if not st["last"] else "")
            last = st["last"] or "窗口内无事件"
            out.append(f'- {flag}{k[0]} {k[1]}：高 {st["高"]}｜中 {st["中"]}'
                       f'｜低 {st["低"]}｜最新 {last}')
        out.append("")
    if ind:
        out.append("## 行业归属（同花顺反查，补 screen 的行业口径）")
        for code, names in sorted(ind.items()):
            out.append(f'- {code}：{"、".join(names) if names else "（无）"}')
        out.append("")
    out.append("## 取数说明")
    out.append(f"- 数据源：同花顺 iNews（公告/个股新闻/异动/除权），窗口 {cutoff} 起")
    out.append("- 关键词分级见 `config.NEWS`；级别只表示「值得看」，不等于买卖信号")
    out.append("- R/C 分与处置档：口径与校准依据见 `references/news-rules.md`"
               "（两池 12 期、602 条事件；标 [先验] 的权重未经实测）")
    if skipped:
        out.append(f'- 跳过 {len(skipped)} 只（无新闻或接口失败）：{"、".join(skipped[:20])}')
    else:
        out.append("- 无跳过项")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- 取数
def _plan_codes() -> list[tuple[str, str]]:
    """持仓个股 + 候选，返回 [(code, name)]，去重保序。"""
    out: dict[str, str] = {}
    path = os.path.join(config.OUT_DIR, "stage3_plan.json")
    try:
        import json
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
        for p in plan.get("positions") or []:
            c = str(p.get("code") or "").strip()
            if c.isdigit() and len(c) == 6:
                out.setdefault(c, p.get("name") or "")
    except Exception:                       # noqa: BLE001
        pass
    cap = int(NEWS.get("max_codes") or 20)
    path2 = os.path.join(config.OUT_DIR, "stage1_candidates.csv")
    try:
        with open(path2, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if len(out) >= cap:
                    break
                c = str(row.get("code") or "").strip().zfill(6)
                if c.isdigit() and len(c) == 6:
                    out.setdefault(c, row.get("name") or "")
    except OSError:
        pass
    return list(out.items())[:cap]


def _plan_held() -> list[tuple[str, str]]:
    """当前持仓的个股 [(code, name)]（只有这些算「持仓」，其余都算候选）。"""
    import json
    try:
        with open(os.path.join(config.OUT_DIR, "stage3_plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
    except Exception:                       # noqa: BLE001
        return []
    out = []
    for p in plan.get("positions") or []:
        c = str(p.get("code") or "").strip()
        if c.isdigit() and len(c) == 6:
            out.append((c, p.get("name") or ""))
    return out


def policy_events(days: int | None = None, rows: list[dict] | None = None) -> list[dict]:
    """政策面：从财联社电报里捞「政策/监管」类快讯（只做背景，不进 R/C 打分）。

    `rows` 参数给测试注入用（不传则取 `datasource.fetch_global_news()`）。
    返回 [{date,title,kind,tag,level,source}]；按标题去重、按日期倒序。
    """
    cfg = getattr(config, "POLICY", {}) or {}
    if not cfg.get("enabled", True):
        return []
    kws = cfg.get("keywords") or []
    subjects = cfg.get("subjects") or []
    actions = cfg.get("actions") or []
    days = int(days or cfg.get("days") or 7)
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    if rows is None:
        try:
            import datasource as ds
            df = ds.fetch_global_news()
            rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df or [])
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 政策面取数失败：{type(e).__name__}: {e}")
            return []
    out: dict[str, dict] = {}
    for row in rows or []:
        title = str(row.get("标题") or "").strip()
        body = str(row.get("内容") or "")
        day = _day(row.get("发布日期"))
        if not title or not day or day < cutoff:
            continue
        text = title + " " + body[:400]
        hit = next((k for k in kws if k in text), "")
        if not hit and subjects and actions:
            # 一般政策要「主体 + 动作」同时出现：否则「险资调研1.46万次」这种行业动态也会被骗进来
            subj = next((s for s in subjects if s in text), "")
            act = next((a for a in actions if a in text), "")
            if subj and act:
                hit = f"{subj}+{act}"
        if not hit:
            continue
        key = title[:80]
        if key in out:
            continue
        out[key] = {"date": day, "title": title[:160], "kind": "政策",
                    "tag": hit, "level": "中", "source": "财联社电报"}
    return sorted(out.values(), key=lambda e: e["date"], reverse=True)


def _pool_codes() -> list[tuple[str, str]]:
    """全池代码（stage1_all.csv → 回退 stage1_candidates.csv，再并持仓）。

    公告流按「全池」扫，不受 NEWS.max_codes（20 只）限制——公告是 0 额外请求的本地数据。
    """
    out: dict[str, str] = {}
    for fn in ("stage1_all.csv", "stage1_candidates.csv"):
        path = os.path.join(config.OUT_DIR, fn)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    c = str(row.get("code") or "").strip().zfill(6)
                    if c.isdigit() and len(c) == 6:
                        out.setdefault(c, str(row.get("name") or ""))
        except OSError:
            continue
        if len(out) >= 100:
            break
    for c, n in _plan_held():
        out.setdefault(c, n)
    return list(out.items())


def announcement_events(pairs: list[tuple[str, str]], held: list[tuple[str, str]],
                        cutoff: str, existing: list[dict] | None = None) -> tuple[list[dict], int]:
    """把「全市场公告流」并进事件面（读本地 cache/cninfo_market/<日期>.csv，0 额外请求）。

    只留池内标的；持仓保留高/中，候选只留「高」；按 (代码, 标题) 去重。
    返回 (事件列表, 覆盖的交易日数)。
    """
    import glob
    base = os.path.join(config.CACHE_DIR, "cninfo_market")
    files = sorted(glob.glob(os.path.join(base, "*.csv")))
    names = {c: n for c, n in pairs}
    codes = set(names)
    held_set = {c for c, _ in (held or [])}
    seen = {(e.get("code"), str(e.get("title") or "")[:200]) for e in (existing or [])}
    events: list[dict] = []
    days = 0
    for f in files:
        d = os.path.splitext(os.path.basename(f))[0]
        if not in_window(d, cutoff):
            continue
        days += 1
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except (OSError, UnicodeDecodeError):
            continue
        for row in rows:
            c = str(row.get("代码") or "").strip().zfill(6)
            if c not in codes:
                continue
            title = str(row.get("公告标题") or "").strip()
            if not title:
                continue
            lv, kw = level_of(title)
            if lv == "低":
                continue
            layer = "持仓" if c in held_set else "候选"
            if layer == "候选" and lv != "高":
                continue
            key = (c, title[:200])
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "layer": layer, "code": c,
                "name": names.get(c) or str(row.get("名称") or ""),
                "kind": "公告", "level": lv, "tag": kw or str(row.get("公告类型") or ""),
                "date": _day(row.get("公告日期") or d), "title": title[:200],
                "source": "巨潮公告", "url": str(row.get("网址") or "")})
    return events, days


def collect(days: int | None = None, limit: int | None = None,
            offline: bool = False) -> dict:
    """扫一轮事件面。返回 {events, scanned, skipped, cutoff, industries}。"""
    days = int(days or NEWS.get("days") or 30)
    limit = int(limit or NEWS.get("max_codes") or 20)
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    pairs = _plan_codes()[:limit]
    res = {"events": [], "scanned": len(pairs), "skipped": [],
           "cutoff": cutoff, "industries": {}, "held": _plan_held()}
    if offline or not fuyao.available() or not pairs:
        res["skipped"] = [c for c, _ in pairs]
        return res

    codes = [c for c, _ in pairs]
    names = dict(pairs)
    held_pairs = _plan_held()
    held = {c for c, _ in held_pairs}
    res["held"] = held_pairs
    # 1) 当日异动（一次批量）
    try:
        for it in fuyao.anomaly_stock(codes):
            c = str(it.get("thscode") or "").split(".")[0]
            content = (it.get("analysis_content") or "").strip()
            lv, kw = level_of(content[:80])
            res["events"].append({
                "layer": "持仓" if c in held else "候选",
                "code": c, "name": it.get("stock_name") or names.get(c, ""),
                "kind": "异动", "level": lv, "tag": it.get("tag_name") or kw,
                "date": today.isoformat(),
                "title": f'当日异动：{it.get("tag_name") or ""}｜{content[:120]}',
                "source": "同花顺异动", "url": ""})
    except fuyao.FuyaoError as e:
        res["skipped"].append(f"异动:{e}")
    # 2) 个股新闻 + 除权事件
    seen: set = set()
    mid_count: dict = {}
    size = int(NEWS.get("news_size") or 20)
    pages = int(NEWS.get("news_pages") or 1)
    for c in codes:
        try:
            items = fuyao.stock_news(c, size=size, pages=pages)
        except fuyao.FuyaoError:
            items = []
            res["skipped"].append(c)
        hit = 0
        layer = "持仓" if c in held else "候选"
        for it in items:
            d = _day(it.get("published"))
            if not in_window(d, cutoff):
                continue
            title = (it.get("title") or "").strip()
            lv, kw = level_of(title)
            if lv == "低":
                continue        # 只留高/中，压低噪音
            if layer == "候选" and lv != "高":
                continue        # 候选只保留可能否决的风险事件
            if layer == "持仓" and lv == "中" and mid_count.get(c, 0) >= 5:
                continue        # 持仓的中档每只最多 5 条
            if (c, title) in seen:
                continue
            seen.add((c, title))
            if lv == "中":
                mid_count[c] = mid_count.get(c, 0) + 1
            hit += 1
            res["events"].append({
                "layer": layer, "code": c, "name": names.get(c, ""),
                "kind": "新闻", "level": lv, "tag": kw,
                "date": d, "title": title[:200],
                "source": it.get("source") or "", "url": it.get("url") or ""})
        if not hit and not items:
            res["skipped"].append(c)
        try:
            for it in (fuyao.adjust_factors(c, start=cutoff) if c in held else []):
                res["events"].append({
                    "layer": layer, "code": c, "name": names.get(c, ""),
                    "kind": "除权", "level": "中", "tag": "分红/送股/配股",
                    "date": _day(it.get("ex_date")),
                    "title": "除权除息：每股分红 %s 元" % (it.get("dividend_per_share") or 0),
                    "source": "同花顺复权因子", "url": ""})
        except fuyao.FuyaoError:
            pass
    # 2a) 政策面（财联社电报，只做背景）
    try:
        res["policy"] = policy_events()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 政策面失败：{type(e).__name__}: {e}")
        res["policy"] = []
    # 2b) 全市场公告流（本地缓存，覆盖全池；0 额外请求）
    res["pool_n"] = 0
    res["ann_n"] = 0
    res["ann_days"] = 0
    try:
        pool = _pool_codes()
        res["pool_n"] = len(pool)
        ann, ann_days = announcement_events(pool, held_pairs, cutoff, res["events"])
        res["events"].extend(ann)
        res["ann_n"], res["ann_days"] = len(ann), ann_days
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 公告流合并失败：{type(e).__name__}: {e}")
    # 3) 行业反查（补 screen 的行业口径）
    try:
        for code, lst in fuyao.stock_industries(codes, "industry").items():
            res["industries"][str(code).split(".")[0]] = [x.get("name") or "" for x in lst]
    except fuyao.FuyaoError:
        pass
    # 4) 双向评分（纯代码，0 token）
    asof = today.isoformat()
    for e in res["events"]:
        e.update(score_event(e, asof))
    return res


# ---------------------------------------------------------------- 写盘
def write_outputs(res: dict) -> dict:
    os.makedirs(config.OUT_DIR, exist_ok=True)
    rows = sort_events(res["events"])
    path_csv = os.path.join(config.OUT_DIR, NEWS.get("out_csv") or "news_events.csv")
    with io.open(path_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for e in rows:
            w.writerow({k: (e.get(k) if e.get(k) is not None else "") for k in COLS})
    pol_rows = [e for e in res.get("policy") or []]
    if pol_rows:
        pcfg = getattr(config, "POLICY", {}) or {}
        p_csv = os.path.join(config.OUT_DIR, pcfg.get("out_csv") or "policy_events.csv")
        with io.open(p_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "title", "kind", "tag", "level", "source"],
                               extrasaction="ignore")
            w.writeheader()
            for e in pol_rows:
                w.writerow(e)
    path_md = os.path.join(config.OUT_DIR, NEWS.get("out_md") or "news_brief.md")
    with io.open(path_md, "w", encoding="utf-8", newline="") as f:
        f.write(render_brief(rows, res["cutoff"], res["scanned"],
                             res.get("industries"), res.get("skipped"),
                             res.get("held"), res.get("pool_n"),
                             res.get("ann_n"), res.get("ann_days"),
                             res.get("policy")))
    return {"csv": path_csv, "md": path_md, "n": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser(description="事件面扫描（公告/新闻/异动/除权）")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offline", action="store_true", help="不联网，只出空简报")
    a = ap.parse_args()
    res = collect(a.days, a.limit, a.offline)
    paths = write_outputs(res)
    high = [e for e in res["events"] if e.get("level") == "高"]
    print(f'[news] 扫描 {res["scanned"]} 只｜事件 {paths["n"]} 条'
          f'（高 {len(high)}）｜窗口起 {res["cutoff"]}')
    if res["skipped"]:
        print(f'[news] 跳过 {len(res["skipped"])} 项：{", ".join(map(str, res["skipped"][:10]))}')
    for e in sort_events(high)[:5]:
        print(f'  ⚠️ {e["date"]} {e["code"]} {e["name"]}｜{e["kind"]}｜{e["tag"]}｜{e["title"][:60]}')
    if res.get("policy"):
        print(f'[news] 政策面 {len(res["policy"])} 条（只做背景）')
    print(f'[news] 产出 {paths["csv"]}')
    print(f'[news] 产出 {paths["md"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
