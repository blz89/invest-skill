# -*- coding: utf-8 -*-
"""cninfo（巨潮公告）离线自测 —— 不联网、不需要任何 key。

覆盖：代码归一 / 市场判定 / 请求参数 / 事件分类 / 风险扫描装配 / 失败必须抛错 / 溯源落盘。
联网行为不在自测范围（人工用 `python cninfo.py --code 601058` 验）。
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import cninfo  # noqa: E402

N = {"ok": 0, "fail": 0}


def check(name, cond, detail=""):
    if cond:
        N["ok"] += 1
        print(f"  OK   {name}")
    else:
        N["fail"] += 1
        print(f"  FAIL {name}  {detail}")


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except Exception:                   # noqa: BLE001
        return True


def main():
    print("== A. 代码归一与市场判定 ==")
    for raw, want in (("sh601058", "601058"), ("601058.SH", "601058"),
                      ("601058", "601058"), (" sz000568 ", "000568"),
                      ("300140.SZ", "300140")):
        check(f"_norm({raw}) == {want}", cninfo._norm(raw) == want, cninfo._norm(raw))
    check("_norm 无法识别时抛错", raises(cninfo._norm, "abc"))

    for code, want in (("600519", ("sse", "sh")), ("688981", ("sse", "sh")),
                       ("000568", ("szse", "sz")), ("300140", ("szse", "sz")),
                       ("002648", ("szse", "sz")), ("830799", ("bj", "bj"))):
        got = cninfo.market(code)
        check(f"market({code}) == {want}", got == want, str(got))
    check("market 无法判断时抛错", raises(cninfo.market, "999999"))

    print("\n== B. 请求参数（字段名/格式是接口契约，错一个就返回 0 条）==")
    p = cninfo._query_params("601058", "9900009410", "2026-01-01", "2026-10-28", 2, 30)
    check("stock = code,orgId", p["stock"] == "601058,9900009410", p["stock"])
    check("seDate = start~end", p["seDate"] == "2026-01-01~2026-10-28", p["seDate"])
    check("column=sse", p["column"] == "sse", p["column"])
    check("plate=sh", p["plate"] == "sh", p["plate"])
    check("tabName=fulltext", p["tabName"] == "fulltext")
    check("isHLtitle=true", p["isHLtitle"] == "true")
    check("pageNum/pageSize 为 int", isinstance(p["pageNum"], int) and isinstance(p["pageSize"], int))
    p2 = cninfo._query_params("000568", "9900000001", "2025-01-01", "2025-12-31", 1, 30)
    check("深市 column=szse plate=sz", (p2["column"], p2["plate"]) == ("szse", "sz"))

    print("\n== C. 事件分类（纯规则、可复现）==")
    cases = [
        ("关于收到中国证监会立案告知书的公告", "立案调查"),
        ("关于公司股票被实施退市风险警示的公告", "退市风险"),
        ("关于收到行政处罚决定书的公告", "行政处罚"),
        ("关于收到浙江证监局警示函的公告", "监管措施"),
        ("关于计提商誉减值准备的公告", "商誉减值"),
        ("关于2026年前三季度计提各项资产减值准备的公告", "资产减值"),
        ("2026年前三季度业绩预告", "业绩快报"),
        ("关于控股股东减持股份计划的公告", "减持"),
        ("关于部分限售股份上市流通的提示性公告", "解禁"),
        ("关于回购公司股份的进展公告", "回购"),
        ("2026年半年度权益分派实施公告", "分红"),
        ("关于股东增持公司股份的公告", "增持"),
        ("关于非公开发行股票预案的公告", "再融资"),
        ("关于会计差错更正及追溯调整的公告", "会计问题"),
        ("关于涉及诉讼事项的公告", "诉讼仲裁"),
    ]
    for title, tag in cases:
        tags = cninfo.classify(title)
        check(f"classify「{title[:16]}…」含 {tag}", tag in tags, str(tags))
    check("无关标题无标签", cninfo.classify("关于召开2026年第二次临时股东大会的通知") == [])
    check("空标题不炸", cninfo.classify("") == [])
    check("硬风险标签都是 risk", all(cninfo.tag_kind(t) == "risk" for t in cninfo._RISK_TAGS))
    check("诉讼仲裁算硬风险", "诉讼仲裁" in cninfo._RISK_TAGS)
    check("商誉减值算硬风险、资产减值不算",
          "商誉减值" in cninfo._RISK_TAGS and "资产减值" not in cninfo._RISK_TAGS)
    for t in ("减持", "解禁", "质押冻结", "再融资"):
        check(f"{t} 是软信号（进默认扫描）", t in cninfo._SOFT_TAGS, str(cninfo._SOFT_TAGS))
    for t in ("担保", "资产减值"):
        check(f"{t} 是噪声（不进默认扫描）",
              t not in cninfo._SOFT_TAGS and t not in cninfo._RISK_TAGS)
    for t in ("回购", "增持", "分红", "重大合同"):
        check(f"{t} 是 positive", cninfo.tag_kind(t) == "positive")

    print("\n== D. 风险扫描装配（离线，桩掉网络）==")
    fake = {
        "601058": [
            {"code": "601058", "name": "赛轮轮胎", "date": "2026-09-01",
             "title": "关于控股股东减持股份计划的公告", "type": "", "url": ""},
            {"code": "601058", "name": "赛轮轮胎", "date": "2026-09-05",
             "title": "2026年半年度权益分派实施公告", "type": "", "url": ""},
        ],
        "000568": [
            {"code": "000568", "name": "泸州老窖", "date": "2026-08-01",
             "title": "关于收到行政处罚决定书的公告", "type": "", "url": ""},
        ],
    }
    orig = cninfo.announcements
    cninfo.announcements = lambda code, start, end, **kw: fake[str(code)[-6:]]
    try:
        res = cninfo.risk_scan(["601058", "000568"], "2026-10-28", 365)
        d = {r["code"]: r for r in res}
        check("默认只返回硬风险股票", set(d) == {"000568"}, str(set(d)))
        check("软信号默认不出现", "601058" not in d)
        res_soft = cninfo.risk_scan(["601058", "000568"], "2026-10-28", 365, include_soft=True)
        ds = {r["code"]: r for r in res_soft}
        check("include_soft 后软信号也出现", set(ds) == {"601058", "000568"}, str(set(ds)))
        check("减持在 tags 里且 hard 为空",
              any("减持" in i["tags"] and not i["hard"] for i in ds["601058"]["items"]))
        check("分红不产生风险记录",
              not any("分红" in i["tags"] for i in ds["601058"]["items"]))
        check("行政处罚进 hard", any("行政处罚" in i["hard"] for i in ds["000568"]["items"]))
        check("每条带日期（PIT 前提）", all(i["date"] for i in ds["000568"]["items"]))
    finally:
        cninfo.announcements = orig

    print("\n== E. 失败必须抛错，不许静默空表 ==")
    def boom(*a, **kw):
        raise cninfo.CninfoError("模拟网络失败")

    orig_http = cninfo._http
    orig_cached = cninfo._cached
    cninfo._http = boom
    cninfo._cached = lambda path, loader, ttl_hours=None: loader()   # 绕过缓存，确保走网络桩
    try:
        try:
            cninfo.org_id("601058")
            check("org_id 网络失败时抛 CninfoError", False, "没抛")
        except cninfo.CninfoError:
            check("org_id 网络失败时抛 CninfoError", True)
    finally:
        cninfo._http = orig_http
        cninfo._cached = orig_cached

    print("\n== F. 溯源落盘 ==")
    # 2026-11-26 审计 A3：dump_provenance() 默认写生产 cache/cninfo_provenance.json，
    # 自检必须把 PROV_FILE 指到临时目录，并断言生产文件 hash 未变。
    import hashlib
    import shutil
    import tempfile

    _prov_tmp = tempfile.mkdtemp(prefix="cninfo_prov_")
    _prod_prov, cninfo.PROV_FILE = cninfo.PROV_FILE, os.path.join(
        _prov_tmp, "cninfo_provenance.json")
    _prod_sha = (hashlib.sha256(open(_prod_prov, "rb").read()).hexdigest()
                 if os.path.isfile(_prod_prov) else None)
    cninfo._PROV.clear()
    cninfo._prov_add("announcement", "601058:test", 2, "unit-test")
    payload = cninfo.dump_provenance()
    check("溯源文件存在", os.path.exists(cninfo.PROV_FILE), cninfo.PROV_FILE)
    with open(cninfo.PROV_FILE, encoding="utf-8") as f:
        on_disk = json.load(f)
    check("溯源 source=cninfo", on_disk.get("source", "").startswith("cninfo"))
    check("溯源条目数正确", len(on_disk.get("items", [])) == 1)
    check("溯源含 rows/fetched_at",
          all(k in on_disk["items"][0] for k in ("rows", "fetched_at", "kind", "key")))
    check("dump 返回值与落盘一致", payload["items"][0]["key"] == "601058:test")
    cninfo.PROV_FILE = _prod_prov
    _prod_sha2 = (hashlib.sha256(open(_prod_prov, "rb").read()).hexdigest()
                  if os.path.isfile(_prod_prov) else None)
    check("溯源不写生产文件", _prod_sha == _prod_sha2, _prod_prov)
    shutil.rmtree(_prov_tmp, ignore_errors=True)

    print("\n== G. 全市场增量流（mock，不联网） ==")
    fake = [
        {"code": "000999", "name": "华润三九", "date": "2026-11-18",
         "title": "关于公司被中国证监会立案调查的公告", "type": "", "url": ""},
        {"code": "600000", "name": "浦发银行", "date": "2026-11-18",
         "title": "关于股东减持股份的公告", "type": "", "url": ""},
        {"code": "002648", "name": "卫星化学", "date": "2026-11-19",
         "title": "2026年第三季度报告", "type": "", "url": ""},
        {"code": "300001", "name": "特锐德", "date": "2026-11-17",
         "title": "关于股东解除限售的公告", "type": "", "url": ""},
    ]
    orig_market = cninfo.market_announcements
    cninfo.market_announcements = lambda a="", b="": list(fake)
    try:
        r_hard = cninfo.market_scan(None, days=2, asof="2026-11-19")
        check("全市场扫描默认只报硬风险", [x["code"] for x in r_hard] == ["000999"], r_hard)
        r_soft = cninfo.market_scan(None, days=2, asof="2026-11-19", include_soft=True)
        check("软信号按 --soft 才带出",
              {x["code"] for x in r_soft} == {"000999", "600000", "300001"},
              sorted({x["code"] for x in r_soft}))
        r_watch = cninfo.market_scan(["000999", "002648"], days=2, asof="2026-11-19")
        check("codes 过滤生效、无信号的不出现",
              [x["code"] for x in r_watch] == ["000999"], r_watch)
    finally:
        cninfo.market_announcements = orig_market

    _tmp = os.path.join(cninfo.CACHE_DIR, "_watch_test.json")
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump({"meta": {}, "holdings": {"000999": {}, "sh510050": {}},
                   "targets": [{"code": "002648"}],
                   "orders": [{"code": "601058"}]}, f)
    try:
        cs = cninfo.codes_from_watch(_tmp)
        check("watch 提取持仓+目标+订单", cs == ["000999", "510050", "002648", "601058"], cs)
    finally:
        os.remove(_tmp)

    print("\n== H. 正文解析：分清「拟减持/已实施」与「问询/处罚」 ==")
    check("正文：拟减持 → 计划减持",
          "计划减持" in cninfo.body_flags("公司股东拟减持不超过 1% 的股份"))
    check("正文：已实施完毕 → 已实施",
          "已实施" in cninfo.body_flags("截至本公告日，本次减持计划已实施完毕"))
    check("正文：否定句「未受到行政处罚」不算命中",
          "行政处罚" not in cninfo.body_flags("公司最近三年未受到中国证监会行政处罚"))
    check("正文：条件清单「正被立案调查」不算命中",
          "立案调查" not in cninfo.body_flags(
              "上市公司不得存在下列情形：涉嫌违法违规正被中国证监会立案调查"))
    check("正文：行政处罚决定书 → 行政处罚",
          "行政处罚" in cninfo.body_flags("公司收到中国证监会《行政处罚决定书》"))
    check("正文：问询函 → 问询监管",
          "问询监管" in cninfo.body_flags("收到交易所出具的《审核问询函》"))
    check("状态：减持+已实施 → 已实施",
          cninfo.body_status(["已实施"], ["减持"]) == "已实施")
    check("状态：供给类之外的「已实施」不算状态",
          cninfo.body_status(["已实施"], ["再融资"]) == "")
    check("状态：立案 → 已定性",
          cninfo.body_status(["立案调查"], ["再融资"]) == "已定性")

    orig_text = cninfo.notice_text
    cninfo.notice_text = lambda url: ("公司收到中国证监会《行政处罚决定书》" if "2.PDF" in url
                                      else "公司股东拟减持股份，本计划尚未实施")
    try:
        items = [{"code": "000999", "name": "华润三九", "date": "2026-11-18",
                  "title": "关于股东减持股份的公告", "url": "http://x/1.PDF",
                  "tags": ["减持"], "hard": [], "soft": ["减持"]}]
        cninfo.refine_body(items, max_items=5)
        check("refine：标题减持 + 正文计划中 → tag 标注（计划）",
              items[0]["tags"] == ["减持（计划）"] and items[0]["status"] == "计划中",
              items[0])
        items2 = [{"code": "600000", "name": "浦发", "date": "2026-11-18",
                   "title": "关于公司收到行政处罚决定书的公告", "url": "http://x/2.PDF",
                   "tags": ["监管措施"], "hard": ["监管措施"], "soft": []}]
        cninfo.refine_body(items2, max_items=5)
        check("refine：标题监管措施 + 正文处罚决定书 → 升级硬风险",
              "行政处罚" in (items2[0].get("hard") or []), items2[0])
        items3 = [{"code": "300001", "name": "特锐德", "date": "2026-11-18",
                   "title": "关于保荐机构核查意见的公告", "url": "http://x/3.PDF",
                   "tags": ["再融资"], "hard": [], "soft": []}]
        cninfo.refine_body(items3, max_items=5)
        check("refine：中介机构文件不解析正文",
              items3[0].get("body_tags") == [] and items3[0].get("body_checked"), items3[0])
    finally:
        cninfo.notice_text = orig_text
    print(f"\n断言：{N['ok'] + N['fail']} 项｜通过 {N['ok']}｜失败 {N['fail']}")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
