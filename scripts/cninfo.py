# -*- coding: utf-8 -*-
"""巨潮资讯网（cninfo）公告查询 —— 事件面数据层。

为什么需要它：公告 / 业绩预告 / 减持 / 商誉减值 / 监管处罚是四道否决看不到的即时证据，
它把面从「滞后财报」扩到「当天发生的事」。巨潮是证监会指定的信息披露平台，
A 股全部公告原文都在这里，且**每条带披露时间戳**——时间戳是 point-in-time 的前提。

设计原则（与 fuyao.py 一致，但它不需要 key、不需要账号）：
- 只在被显式调用时联网；本模块 import 本身零 IO。
- 失败 fail-loud：抛 `CninfoError`，不返回"看起来正常的空表"。
- 每条结果带 `source` / `fetched_at`，写溯源文件。
- 本地缓存（默认 24h），同一查询不重复打站点。

公开接口（无 key，需带 Referer/UA）：
1. `POST /new/information/topSearch/query`  form: keyWord=<code>&maxNum=10
   → [{code, orgId, zwjc, category, plate}]  用来拿 orgId
2. `POST /new/hisAnnouncement/query`  form: 见 `_query_params`
   → {announcements: [{secCode, secName, announcementTitle, announcementTime(ms),
                       adjunctUrl, announcementTypeName}], totalRecordNum}

用法：
    python cninfo.py --code 601058 --start 2025-01-01 --end 2026-10-28
    python cninfo.py --scan out/stage1_all.csv --asof 2026-10-28   # 批量风险扫描
    python cninfo.py --market --market-days 7                      # 全市场增量风险公告
    python cninfo.py --market --watch ledger/state_50k.json        # 只看持仓/目标的公告
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import config

CACHE_DIR = os.path.join(config.CACHE_DIR, "cninfo")
PROV_FILE = os.path.join(config.CACHE_DIR, "cninfo_provenance.json")

BASE = "http://www.cninfo.com.cn"
TOP_SEARCH = BASE + "/new/information/topSearch/query"
HIS_QUERY = BASE + "/new/hisAnnouncement/query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ",
    "Referer": BASE + "/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_TTL_HOURS = 24
_TTL = int(os.environ.get("CNINFO_TTL_HOURS", DEFAULT_TTL_HOURS))
_PROV: list[dict] = []


class CninfoError(RuntimeError):
    """巨潮接口失败。故意不吞异常——静默空表会让人误以为"没有公告"。"""


# ------------------------------------------------------------------ 基础设施
def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _http(url: str, form: dict, timeout: int = 20, retries: int = 2) -> dict:
    """POST form，返回解析后的 JSON。失败抛 CninfoError。"""
    import requests

    last = None
    for i in range(retries + 1):
        try:
            r = requests.post(url, data=form, headers=HEADERS, timeout=timeout)
            if r.status_code != 200:
                raise CninfoError(f"HTTP {r.status_code} from {url}")
            txt = r.text.strip()
            if not txt:
                raise CninfoError(f"empty body from {url}")
            return json.loads(txt)
        except Exception as e:          # noqa: BLE001 - 统一转成 CninfoError
            last = e
            if i < retries:
                time.sleep(0.8 * (i + 1))
    raise CninfoError(f"{url} 请求失败：{last}")


def _cached(path: str, loader, ttl_hours: int | None = None):
    """文件缓存。ttl 内直接读盘；过期则重新拉取。"""
    ttl = _TTL if ttl_hours is None else ttl_hours
    if ttl > 0 and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= ttl:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:           # noqa: BLE001 - 缓存坏了就当没有
                pass
    data = loader()
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    return data


def _prov_add(kind: str, key: str, rows: int, note: str = ""):
    _PROV.append({
        "kind": kind,
        "key": key,
        "rows": int(rows),
        "note": note,
        "source": "cninfo",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    })


def dump_provenance() -> dict:
    _ensure_dir()
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "cninfo(巨潮资讯网)",
        "items": _PROV,
    }
    with open(PROV_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


# ------------------------------------------------------------------ 代码/市场
def _norm(code: str) -> str:
    """把 sh600519 / 600519.SH / 600519 统一成 6 位数字。"""
    s = str(code).strip().upper()
    s = s.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    s = s.replace("SH", "").replace("SZ", "").replace("BJ", "")
    m = re.search(r"(\d{6})", s)
    if not m:
        raise CninfoError(f"无法识别的股票代码：{code!r}")
    return m.group(1)


def market(code: str) -> tuple[str, str]:
    """返回 (column, plate)。column 决定接口分区，plate 决定板块。"""
    c = _norm(code)
    if c[0] in "65":
        return "sse", "sh"
    if c[0] in "03":
        return "szse", "sz"
    if c[0] in "48":
        return "bj", "bj"
    raise CninfoError(f"无法判断市场：{c}")


def org_id(code: str) -> str:
    """巨潮内部机构号（orgId）。查不到就抛错，不猜。"""
    c = _norm(code)
    path = os.path.join(CACHE_DIR, f"orgid_{c}.json")

    def load():
        js = _http(TOP_SEARCH, {"keyWord": c, "maxNum": 10})
        # 接口可能直接返回数组，也可能包在 keyBoardList/data 里，两种都兜住
        if isinstance(js, dict):
            cands = js.get("keyBoardList") or js.get("data") or []
        else:
            cands = js or []
        for x in cands:
            if isinstance(x, dict) and str(x.get("code")) == c and x.get("orgId"):
                return {"code": c, "orgId": str(x["orgId"]), "name": x.get("zwjc", "")}
        raise CninfoError(f"巨潮 topSearch 未找到 {c}（返回 {str(js)[:200]}）")

    try:
        d = _cached(path, load, ttl_hours=24 * 30)
        return str(d["orgId"])
    except CninfoError:
        raise
    except Exception as e:              # noqa: BLE001
        raise CninfoError(f"解析 orgId 失败 {c}: {e}") from e


# ------------------------------------------------------------------ 公告查询
def _query_params(code: str, oid: str, start: str, end: str, page: int, size: int) -> dict:
    column, plate = market(code)
    return {
        "pageNum": page,
        "pageSize": size,
        "column": column,
        "tabName": "fulltext",
        "plate": plate,
        "stock": f"{_norm(code)},{oid}",
        "searchkey": "",
        "secdate": "",
        "t0": "", "t1": "", "t2": "", "t3": "", "t4": "",
        "dateBef": "", "dateAft": "",
        "category": "", "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }


def announcements(code: str, start: str, end: str, page_size: int = 30,
                  max_pages: int = 10, use_cache: bool = True) -> list[dict]:
    """拉一只股票在 [start, end] 内的全部公告（自动翻页）。"""
    c = _norm(code)
    oid = org_id(c)
    key = f"ann_{c}_{start}_{end}_{page_size}_{max_pages}.json"
    path = os.path.join(CACHE_DIR, key)

    def load():
        out: list[dict] = []
        total = None
        for page in range(1, max_pages + 1):
            js = _http(HIS_QUERY, _query_params(c, oid, start, end, page, page_size))
            total = int(js.get("totalRecordNum") or 0)
            items = js.get("announcements") or []
            for it in items:
                ms = int(it.get("announcementTime") or 0)
                day = datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d") if ms else ""
                out.append({
                    "code": c,
                    "name": it.get("secName") or "",
                    "date": day,
                    "title": (it.get("announcementTitle") or "").strip(),
                    "type": it.get("announcementTypeName") or "",
                    "url": (BASE + "/new/anon/notice/" + it["adjunctUrl"]
                            if it.get("adjunctUrl") else ""),
                })
            if not items or len(out) >= total:
                break
            time.sleep(0.15)
        return {"code": c, "start": start, "end": end, "total": int(total or 0), "items": out}

    d = _cached(path, load) if use_cache else load()
    _prov_add("announcement", f"{c}:{start}~{end}", len(d.get("items", [])),
              f"total={d.get('total')}")
    return d.get("items", [])


# ------------------------------------------------------------------ 事件分类
# 顺序有意义：先判硬风险，再判中性/正面。规则只用标题，不改写原文。
_RULES: list[tuple[str, str, str]] = [
    # (正则, 标签, 风险/中性/正面)
    (r"立案|立案调查", "立案调查", "risk"),
    (r"退市风险|终止上市|暂停上市|\*ST", "退市风险", "risk"),
    (r"行政处罚|处罚决定|罚款|没收违法所得", "行政处罚", "risk"),
    (r"警示函|监管函|通报批评|公开谴责|监管关注|责令改正", "监管措施", "risk"),
    (r"商誉减值", "商誉减值", "risk"),
    (r"计提.{0,8}减值|资产减值准备|存货跌价", "资产减值", "info"),
    (r"业绩预亏|预计亏损|首亏|预减|续亏", "业绩预亏", "risk"),
    (r"会计差错更正|前期会计差错|追溯调整|非标意见|保留意见|无法表示意见", "会计问题", "risk"),
    (r"诉讼|仲裁", "诉讼仲裁", "risk"),
    (r"减持", "减持", "warn"),
    (r"解除限售|限售股份?上市流通|解禁", "解禁", "warn"),
    (r"质押|冻结|司法拍卖", "质押冻结", "warn"),
    (r"担保", "担保", "info"),
    (r"非公开发行|向特定对象发行|可转债|配股|定增", "再融资", "warn"),
    (r"业绩预告|业绩快报", "业绩快报", "info"),
    (r"回购", "回购", "positive"),
    (r"增持", "增持", "positive"),
    (r"权益分派|利润分配|现金分红", "分红", "positive"),
    (r"股权激励|员工持股", "股权激励", "info"),
    (r"重大合同|中标", "重大合同", "positive"),
]

_RISK_TAGS = {"立案调查", "退市风险", "行政处罚", "监管措施", "商誉减值",
              "业绩预亏", "会计问题", "诉讼仲裁"}
# 软信号走 config.CNINFO["soft_risk_tags"]，不在这里硬编码（便于调噪）
_SOFT_TAGS = set(config.CNINFO.get("soft_risk_tags") or
                 ["减持", "解禁", "质押冻结", "再融资"])


def classify(title: str) -> list[str]:
    """给公告标题打标签。纯规则、可复现、不调 LLM。"""
    tags = []
    t = str(title or "")
    for pat, tag, _kind in _RULES:
        if re.search(pat, t) and tag not in tags:
            tags.append(tag)
    return tags


def tag_kind(tag: str) -> str:
    for _pat, tg, kind in _RULES:
        if tg == tag:
            return kind
    return "info"


# ------------------------------------------------------------------ 正文（PDF）解析
# 标题规则分不清「拟减持」和「已减持完毕」、「问询函」和「行政处罚决定书」；
# 正文能分。正文按 PDF URL 永久缓存，只在显式要求时取（默认不打，省请求）。
_BODY_RULES: list[tuple[str, str]] = [
    (r"行政处罚决定书|行政处罚事先告知书|市场禁入决定书", "行政处罚"),
    (r"立案告知书|被立案调查|被中国证监会立案|立案调查书", "立案调查"),
    (r"退市风险警示|终止上市决定|面值退市", "退市风险"),
    (r"问询函|关注函|监管工作函|警示函", "问询监管"),
    (r"(?:减持|回购|增持)[^。；\n]{0,40}?(?:已实施完毕|实施完毕|已完成|完成)", "已实施"),
    (r"(?:拟|计划|将)[^。；\n]{0,20}?(?:减持|回购|增持)", "计划减持"),
]
# 公告里大量「未受到行政处罚」「不存在被立案调查」这类否定句，按「前文有否定词」跳过
_NEG_BEFORE = re.compile(r"(?:未|没有|不存在|无|不涉及|不构成|并非)[^。；\n]{0,12}$")
# 「不得存在下列情形：正被立案调查…」这类资格/条件清单，不是公司事件
_COND_BEFORE = re.compile(r"(?:不得|情形|条件|禁止|资格|负面清单|门槛)")
# 这几类风险最容易在模板条款里被误伤，左文出现条件词就跳过
_GUARDED_TAGS = {"行政处罚", "立案调查", "退市风险"}


def _body_hit(text: str, pat: str, tag: str = "") -> bool:
    for m in re.finditer(pat, text):
        left = text[max(0, m.start() - 16):m.start()]
        if _NEG_BEFORE.search(left):
            continue
        if tag in _GUARDED_TAGS:
            wide = text[max(0, m.start() - 60):m.start()]
            if _COND_BEFORE.search(wide):
                continue
        return True
    return False


_BODY_SCAN_CHARS = 2500     # 公告开头的主体段落才有公司事件；后面的附件模板噪声大


def body_flags(text: str) -> list[str]:
    """正文里命中的动作/状态标记（去重、保序；否定句不算命中）。

    只扫前 ``_BODY_SCAN_CHARS`` 个字符：公告后段大量「不得存在下列情形」这类
    模板句会误伤（例如把「正被立案调查」的条件清单当成公司被立案）。
    """
    t = str(text or "")[:_BODY_SCAN_CHARS]
    out: list[str] = []
    for pat, tag in _BODY_RULES:
        if tag not in out and _body_hit(t, pat, tag):
            out.append(tag)
    return out


def notice_text(url: str) -> str:
    """取公告 PDF 正文（经 stock 数据层按 URL 缓存；失败返回空串，不抛）。"""
    if not str(url or "").strip():
        return ""
    try:
        import datasource as ds
        return ds.fetch_notice_text(url)
    except Exception:                               # noqa: BLE001 - 正文拿不到就退回标题口径
        return ""


def body_status(flags, tags=None) -> str:
    """把正文标记收敛成一个状态词（空串 = 正文没给出状态）。

    「已实施/计划中」只对减持/解禁/回购/增持这类**供给类**公告有意义；
    再融资、诉讼这类公告里出现「回购完成」多半是模板句，不当状态。
    """
    f = set(flags or [])
    tg = set(tags or [])
    supply = bool(tg & {"减持", "解禁", "回购", "增持"})
    if supply and "已实施" in f:
        return "已实施"
    if supply and "计划减持" in f:
        return "计划中"
    if f & {"行政处罚", "立案调查", "退市风险"}:
        return "已定性"
    if "问询监管" in f:
        return "问询"
    return ""


_BODY_SKIP_TITLE = re.compile(r"保荐|法律意见|审计报告|验资报告|核查意见")


def refine_body(items: list[dict], max_items: int = 20) -> list[dict]:
    """给已命中的公告补正文标记（最多取 ``max_items`` 篇）。

    只做三件事：①记 ``body_tags``/``status``；②标题是「减持」的区分
    已实施/计划中（``tags`` 里替换）；③标题是「监管措施」但正文是处罚决定书的，
    升进 ``hard``（它其实是硬风险）。

    中介机构文件（保荐书/法律意见/审计报告/核查意见）不解析：它们的正文是
    模板条款清单（「不得存在下列情形：正被立案调查…」），误伤率高于信息量。
    """
    for it in (items or [])[: max(0, int(max_items))]:
        if it.get("body_checked"):
            continue
        if _BODY_SKIP_TITLE.search(str(it.get("title") or "")):
            it["body_checked"] = True
            it["body_tags"] = []
            continue
        text = notice_text(it.get("url") or "")
        it["body_checked"] = True
        if not text:
            it.setdefault("body_tags", [])
            continue
        flags = body_flags(text)
        it["body_tags"] = flags
        st = body_status(flags, it.get("tags") or [])
        if st:
            it["status"] = st
        if st in ("已实施", "计划中"):
            new_tag = "减持（已实施）" if st == "已实施" else "减持（计划）"
            it["tags"] = [new_tag if t == "减持" else t for t in (it.get("tags") or [])]
        if "行政处罚" in flags:
            hard = list(it.get("hard") or [])
            if "行政处罚" not in hard:
                it["hard"] = hard + ["行政处罚"]
    return items


def risk_scan(codes, asof: str, lookback_days: int = 365,
              include_soft: bool = False) -> list[dict]:
    """对一批股票扫「近 N 天公告里的风险信号」，供四道否决前置使用。

    - 默认只返回**硬风险**（立案/退市/处罚/监管措施/减值/预亏/会计问题）
      → 用于否决前置核查，宁缺毋滥。
    - `include_soft=True` 时把软信号（减持/解禁/质押/担保/再融资）也带出来
      → 用于周报提示，不参与否决。
    只返回有信号的记录，无信号的不出现，避免噪声。
    """
    end = asof
    start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    out = []
    for code in codes:
        try:
            items = announcements(code, start, end)
        except CninfoError as e:
            out.append({"code": _norm(code), "error": str(e), "items": []})
            continue
        hits = []
        for it in items:
            tags = classify(it["title"])
            hard = [t for t in tags if t in _RISK_TAGS]
            soft = [t for t in tags if t in _SOFT_TAGS]
            if hard or (include_soft and soft):
                hits.append({**it, "tags": tags, "hard": hard, "soft": soft})
        if hits:
            out.append({"code": _norm(code), "error": "", "items": hits})
    return out


# ------------------------------------------------------------------ 全市场增量流
def market_announcements(date_from: str = "", date_to: str = "") -> list[dict]:
    """全市场公告（按披露日，经 stock 数据层 `fetch_market_announcements`）。

    与逐只 `announcements(code, ...)` 的区别：一次拿全 A 股当天全部公告
    （约 1,300–2,100 条），因此能覆盖「不在候选池里、但已经是持仓」的标的突发公告。
    缓存按披露日落成 CSV（公告发布后内容不再变，跨日永久复用）。
    """
    import datasource as ds

    df = ds.fetch_market_announcements(date_from, date_to)
    out: list[dict] = []
    for r in df.to_dict("records"):
        out.append({
            "code": str(r.get("代码") or "").strip(),
            "name": str(r.get("名称") or "").strip(),
            "date": str(r.get("公告日期") or "").strip()[:10],
            "title": str(r.get("公告标题") or "").strip(),
            "type": str(r.get("公告类型") or "").strip(),
            "url": str(r.get("网址") or "").strip(),
        })
    return out


def market_scan(codes=None, days: int = 7, include_soft: bool = False,
                asof: str = "", body: bool = False, body_max: int = 20) -> list[dict]:
    """全市场增量扫描：只返回打了风险标签的公告（默认仅硬风险）。

    - `codes=None` → 扫全市场（找「池子之外」的突发利空）；
    - `codes` 给定时只保留这些代码（持仓 + 目标），打标口径与 `classify()` 一致；
    - `body=True` 时再取前 ``body_max`` 篇 PDF 正文，区分「拟减持/已实施」、
      并识别「问询函 vs 行政处罚决定书」（取数失败自动退回标题口径）。
    """
    end = asof or datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d")
             - timedelta(days=max(0, int(days) - 1))).strftime("%Y-%m-%d")
    want = None if codes is None else {_norm(c) for c in codes if str(c or "").strip()}
    out: list[dict] = []
    for it in market_announcements(start, end):
        if want is not None and it["code"] not in want:
            continue
        tags = classify(it["title"])
        hard = [t for t in tags if t in _RISK_TAGS]
        soft = [t for t in tags if t in _SOFT_TAGS]
        if not hard and not (include_soft and soft):
            continue
        out.append({**it, "tags": tags, "hard": hard, "soft": soft})
    out.sort(key=lambda r: (r.get("date") or "", r.get("code") or ""), reverse=True)
    if body and out:
        refine_body(out, max_items=body_max)
    return out


def codes_from_watch(path: str) -> list[str]:
    """从 plan.json / state_50k.json 里提取关注代码（持仓 + 目标 + 订单）。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        js = json.load(f)
    out: list[str] = []

    def _add(raw):
        c = _norm(raw)
        if c and c not in out:
            out.append(c)

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) in ("holdings", "positions") and isinstance(v, dict):
                    for ck in v.keys():
                        _add(ck)
                elif str(k) in ("code", "codes", "symbol", "ts_code"):
                    if isinstance(v, str):
                        _add(v)
                    elif isinstance(v, list):
                        for x in v:
                            _add(x)
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(js)
    return out


# ------------------------------------------------------------------ CLI
def _print(items: list[dict], as_json: bool):
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    if not items:
        print("（无公告）")
        return
    for it in items:
        tags = "".join(f"[{t}]" for t in it.get("tags", []))
        print(f"{it['date']}  {it['code']} {it['name']}  {tags} {it['title']}")
        if it.get("url"):
            print(f"          {it['url']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="巨潮资讯网公告查询（事件面）")
    ap.add_argument("--code", help="股票代码，如 601058 / sh601058 / 601058.SH")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--asof", default=None, help="as-of 日期，默认今天")
    ap.add_argument("--days", type=int, default=365, help="回溯天数（配合 --scan/--asof）")
    ap.add_argument("--scan", help="CSV 文件（含 code 列）批量风险扫描")
    ap.add_argument("--market", action="store_true",
                    help="全市场公告增量扫描（一次拿全 A 股，不再逐只查）")
    ap.add_argument("--market-days", type=int, default=7, help="增量窗口天数（配合 --market）")
    ap.add_argument("--watch", help="plan.json / state_50k.json：只报这些文件里的代码（持仓+目标）")
    ap.add_argument("--body", action="store_true",
                    help="取公告正文（PDF）区分「拟减持/已实施」「问询函/处罚决定书」")
    ap.add_argument("--body-max", type=int, default=20, help="最多解析几篇正文（配合 --body）")
    ap.add_argument("--soft", action="store_true", help="连软信号（减持/解禁/质押/担保/再融资）一起报")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)

    asof = a.asof or datetime.now().strftime("%Y-%m-%d")
    end = a.end or asof
    if a.start:
        start = a.start
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=a.days)).strftime("%Y-%m-%d")

    if a.market:
        codes = codes_from_watch(a.watch) if a.watch else None
        res = market_scan(codes, a.market_days, include_soft=a.soft, asof=asof,
                          body=a.body, body_max=a.body_max)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            scope = f"关注池 {len(codes)} 只" if codes is not None else "全市场"
            print(f"{scope}：近 {a.market_days} 天 {len(res)} 条风险公告"
                  f"（as-of {asof}，{'硬+软' if a.soft else '仅硬风险'}"
                  f"{'，已读正文' if a.body else ''}）")
            for it in res:
                marks = "、".join(it.get("hard") or it.get("soft") or [])
                st = f"  {it['status']}" if it.get("status") else ""
                bt = ("  正文：" + "、".join(it.get("body_tags") or [])) if it.get("body_tags") else ""
                print(f"  {it['date']} {it['code']} {it['name']} [{marks}]{st}  {it['title'][:60]}")
                if bt:
                    print(f"         {bt}")
                if it.get("url"):
                    print(f"          {it['url']}")
        dump_provenance()
        return 0

    if a.scan:
        import pandas as pd

        df = pd.read_csv(a.scan, dtype={"code": str})
        codes = [c for c in df.get("code", []) if c]
        res = risk_scan(codes, asof, a.days, include_soft=a.soft)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"扫描 {len(codes)} 只，命中 {len(res)} 只（回溯 {a.days} 天，as-of {asof}，"
                  f"{'硬+软' if a.soft else '仅硬风险'}）")
            for r in res:
                if r["error"]:
                    print(f"  {r['code']}  ERROR {r['error']}")
                    continue
                for it in r["items"]:
                    marks = "、".join(it.get("hard") or it.get("soft") or [])
                    print(f"  {r['code']} {it['date']} [{marks}]  {it['title'][:60]}")
        dump_provenance()
        return 0

    if not a.code:
        ap.error("需要 --code 或 --scan")
    items = announcements(a.code, start, end, use_cache=not a.no_cache)
    for it in items:
        it["tags"] = classify(it["title"])
    _print(items, a.json)
    dump_provenance()
    return 0


if __name__ == "__main__":
    sys.exit(main())
