#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot.py（L1 交接契约生产者）自测。

只测纯逻辑：用临时目录造假 CSV，不碰真实 out/、不取网络。
运行：python _selftest_snapshot.py      # 全过 exit 0
"""
import csv
import io
import os
import shutil
import sys
import tempfile

import snapshot

PASS = FAIL = 0
TMP = tempfile.mkdtemp(prefix="snap_")
_orig_out = snapshot.OUT


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"  [{extra}]" if extra else ""))


def W(name, text):
    with io.open(os.path.join(TMP, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


ALL_HDR = ("code,name,industry,error,data_ok,price,list_days,mktcap_yi,avg_amt60_yi,"
           "roe_list,roe_avg5,roe_years_ok,ocf_ni_3y,debt_ratio,gross_margin,"
           "rev_cagr3,profit_cagr3,max_dd_3y,vol_3y,ma200,atr14,pe,pb,"
           "pe_pct_ind,pb_pct_ind,is_financial,gates,stage1_pass")


def stage1(rows):
    with io.open(os.path.join(TMP, "stage1_all.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_HDR.split(","), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def base_row(code, **kw):
    r = {k: "" for k in ALL_HDR.split(",")}
    r.update({"code": code, "name": "测试" + code, "industry": "制造业",
              "data_ok": "True", "mktcap_yi": "2194.8", "avg_amt60_yi": "12.34",
              "roe_list": "[None, 10.47, 9.84, 8.99, 7.73]", "ocf_ni_3y": "3.607",
              "debt_ratio": "55.2", "rev_cagr3": "0.12", "max_dd_3y": "-0.3893",
              "pe": "5.464", "pb_pct_ind": "0.14"})
    r.update(kw)
    return r


try:
    snapshot.OUT = TMP
    print("=== A. 正常拼装 ===")
    stage1([
        base_row("000001"),
        base_row("600519", roe_list="[None, None, 22.1, 20.3]", pe="30.1", pb_pct_ind="0.62"),
        base_row("000002", data_ok="False"),          # 取数失败 → 必须被跳过
        base_row(""),                                  # 空代码 → 跳过
    ])
    W("stage2_judged.csv",
      "code,name,industry,verdict,total,quality,growth,valuation,risk,price,pe,"
      "pe_pct_own,buy_price,target_price,stop_price,completeness,veto,error\n"
      "600519,贵州茅台,酿酒行业,买入,80,25,25,15,15,1500,30.1,0.4523,1400,1800,1200,1.0,,\n")
    W("stage1_etf.csv", "sym,name,cls,underlying,price,lot_cost,price_date,amt20_yi,"
                        "hist_days,ret_1y,ret_3y,dd_1y,dd_3y,vs_ma200,vs_ma60,"
                        "max_1d_move,score\n510300,沪深300ETF,宽基,,,,,,,,,,,,,,,\n")
    W("stage1_otc.csv", "code,name\n018074,中银债\n012310,民生债\n")
    W("stage2_detail.json",
      '{"generated_at":"2026-10-28T23:59:50","results":'
      '[{"metrics":{"as_of":"2026-10-28"}}]}')

    rows, meta = snapshot.build(today="2026-10-29")
    ok("只保留 data_ok=True 且有代码的行 → 2 行", len(rows) == 2, f"实际 {len(rows)}")
    ok("跳过计数 = 2", meta["skipped_no_data"] == 2, meta["skipped_no_data"])
    ok("列名与契约一致", meta["columns"] == snapshot.COLS)
    ok("amt60_yi 取自 avg_amt60_yi", rows[0]["amt60_yi"] == 12.34, rows[0]["amt60_yi"])
    ok("roe5y_min 忽略 None 取最小 → 7.73", rows[0]["roe5y_min"] == 7.73, rows[0]["roe5y_min"])
    ok("roe5y_min 第二行 = 20.3", rows[1]["roe5y_min"] == 20.3, rows[1]["roe5y_min"])
    ok("mdd3y 取 max_dd_3y", rows[0]["mdd3y"] == -0.3893)
    ok("pe_pct_self 只有候选有", rows[0]["pe_pct_self"] is None and rows[1]["pe_pct_self"] == 0.4523,
       f"{rows[0]['pe_pct_self']} / {rows[1]['pe_pct_self']}")
    ok("nav_date 取 stage2_detail 的 as_of", meta["nav_date"] == "2026-10-28", meta["nav_date"])
    ok("stale_days = 1", meta["stale_days"] == 1, meta["stale_days"])
    ok("pe_pct_self 覆盖数 = 1", meta["pe_pct_self_coverage"] == 1)
    ok("etf_rows = 1", meta["etf_rows"] == 1)
    ok("otc_rows = 2", meta["otc_rows"] == 2)
    ok("meta 记明 L1 只含个股", "只含个股" in meta["note"])
    ok("source 标明无网络", "无网络" in rows[0]["source"])

    print("=== B. 落盘 ===")
    p_csv, p_meta = snapshot.write(rows, meta)
    text = io.open(p_csv, encoding="utf-8").read()
    hdr = text.splitlines()[0]
    ok("CSV 表头 = 契约列（顺序一致）", hdr == ",".join(snapshot.COLS), hdr)
    ok("CSV 行数 = 1 表头 + 2 数据", len(text.splitlines()) == 3)
    ok("空值写成空串而不是 None", ",None" not in text and "None" not in text)
    ok("meta 文件存在", os.path.exists(p_meta))

    print("=== C. 失败路径（fail-closed，不许静默出空表）===")
    os.remove(os.path.join(TMP, "stage1_all.csv"))
    try:
        snapshot.build()
        ok("缺主表应抛 FileNotFoundError", False)
    except FileNotFoundError:
        ok("缺主表应抛 FileNotFoundError", True)

    # 表头被上游改过：少一列 → KeyError
    stage1([base_row("000001")])
    hdr_p = os.path.join(TMP, "stage1_all.csv")
    txt = io.open(hdr_p, encoding="utf-8").read().replace("mktcap_yi", "mktcap_renamed")
    io.open(hdr_p, "w", encoding="utf-8", newline="").write(txt)
    try:
        snapshot.build()
        ok("缺必需列应抛 KeyError", False)
    except KeyError:
        ok("缺必需列应抛 KeyError", True)

    # 字段名都在但值全空 → 覆盖率 0 → ValueError
    stage1([base_row("000001", mktcap_yi="", avg_amt60_yi="", roe_list="[]",
                     ocf_ni_3y="", debt_ratio="", rev_cagr3="", max_dd_3y="",
                     pe="", pb_pct_ind="")])
    try:
        snapshot.build()
        ok("关键列全空应抛 ValueError", False)
    except ValueError:
        ok("关键列全空应抛 ValueError", True)

    print("=== D. 边界 ===")
    ok("空 roe_list → None（不当作 0）", snapshot.roe_min5("[]") is None)
    ok("roe_list 全 None → None", snapshot.roe_min5("[None, None]") is None)
    ok("_num('-') → None", snapshot._num("-") is None)
    ok("_num('') → None", snapshot._num("") is None)
    ok("_num('0') → 0.0（0 是有效值）", snapshot._num("0") == 0.0)
finally:
    snapshot.OUT = _orig_out
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
