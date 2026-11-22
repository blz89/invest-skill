# -*- coding: utf-8 -*-
"""阶段三选股/组仓的离线自检（不联网、不读写 out/）。

覆盖 2026-10-26 新增的「排名篮子」：pick() 的 rank/conviction 两种口径、
allocate() 的单票/行业/总敞口上限、以及 decide.py --refresh 的开关。
为什么单独建这个文件：这些是「组合构造」规则，改错了不会报错、只会悄悄
把仓位押到一只股票上——必须用断言锁住行为。
"""
from __future__ import annotations

import csv
import inspect
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import news  # noqa: E402
import plan  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def mk(code: str, name: str, total: float, verdict: str, industry: str = "") -> dict:
    return {"code": code, "name": name, "industry": industry or f"行业{code}",
            "verdict": verdict, "scores": {"total": total},
            "metrics": {"price": 10.0}, "pricing": {"buy_price": 9.0}}


def main() -> int:
    global PASS, FAIL
    mode0 = config.EXEC.get("stock_weighting")
    try:
        config.EXEC["stock_weighting"] = "rank"
        regime = {"base_gross": 0.35}

        print("1) 排名篮子：1 只买入区 + 3 只观察区 → 4 只都在篮子里，按排名给权重")
        res = [mk("a", "甲", 72.6, "买入区"), mk("b", "乙", 69.0, "观察"),
               mk("c", "丙", 68.1, "观察"), mk("d", "丁", 66.0, "观察")]
        chosen, note = plan.pick(res, regime)
        check("返回 4 只", len(chosen) == 4, str(len(chosen)))
        check("权重系数：买入区 1.00，观察区一律 0（不建仓）",
              [c["mult"] for c in chosen] == [1.0, 0.0, 0.0, 0.0],
              str([c["mult"] for c in chosen]))
        check("排名 1..4", [c["rank"] for c in chosen] == [1, 2, 3, 4])
        check("按总分降序", [c["r"]["name"] for c in chosen] == ["甲", "乙", "丙", "丁"])
        check("note 说明是排名篮子", "排名篮子" in note and "华润" not in note)
        check("note 列出每只", all(n in note for n in ["甲", "乙", "丙", "丁"]), note)

        print("2) 没有买入区也能成篮子（这正是「只有 1 只」问题的直接修复）")
        res2 = [mk("b", "乙", 69.0, "观察"), mk("c", "丙", 68.1, "观察")]
        chosen2, note2 = plan.pick(res2, regime)
        # 观察区只占候选名额、不建仓（B 类 #5，EXEC.watch_weight=0.0）：仍然留在篮子里
        # 让人看见「选中了但没买」，权重乘数一律 0。
        check("2 只观察都留在候选、权重全 0",
              len(chosen2) == 2 and [c["mult"] for c in chosen2] == [0.0, 0.0],
              str([c["mult"] for c in chosen2]))
        check("note 标出 0 权重（观察区不建仓）",
              "0.00/0.00" in note2 and all(c["r"]["verdict"] == "观察" for c in chosen2), note2)
        check("note 不含核心仓", "核心仓" not in note2)

        print("8b) 排名篮子只数随档位：≤35% 4 只、≤60% 5 只、>60% 6 只")
        _many = [mk("c%d" % i, "C%d" % i, 80 - i, "买入区") for i in range(1, 9)]
        _reg = {"base_gross": 0.35}
        c35, _ = plan.pick(_many, _reg, gross=0.35)
        check("权益 35% 取 4 只", len(c35) == 4, len(c35))
        c60, _ = plan.pick(_many, _reg, gross=0.60)
        check("权益 60% 取 5 只", len(c60) == 5, len(c60))
        c80, _ = plan.pick(_many, _reg, gross=0.80)
        check("权益 80% 取 6 只", len(c80) == 6, len(c80))
        cdef, _ = plan.pick(_many, _reg)
        check("不传 gross 用兜底 4 只", len(cdef) == 4, len(cdef))

        print("3) 不传档位时按兜底 4 只：第 5 名起不纳入")
        res3 = [mk(str(i), chr(65 + i), 70 - i, "观察") for i in range(6)]
        chosen3, note3 = plan.pick(res3, regime)
        check("只取 4 只", len(chosen3) == 4)
        check("note 说明有 2 只未纳入", "2 只" in note3, note3)

        print("4) 空池 / 全回避 → 不建仓、保持现金")
        chosen4, note4 = plan.pick([], regime)
        check("空池返回空", chosen4 == [] and "不建仓" in note4)
        chosen4b, _ = plan.pick([mk("x", "X", 40, "回避"), mk("y", "Y", 45, "回避")], regime)
        check("全回避返回空", chosen4b == [])

        print("5) 组仓：4 只不同行业 → 总敞口≈35%，单票≤10%，第一名占个股层 <35%")
        # 观察区 0 权重后，只有买入区才真建仓；这里全部用买入区，保持「4 只都建仓」的口径。
        res5 = [mk("a", "甲", 72.6, "买入区"), mk("b", "乙", 71.0, "买入区"),
                mk("c", "丙", 70.5, "买入区"), mk("d", "丁", 70.1, "买入区")]
        chosen5 = [c for c in plan.pick(res5, regime)[0]]
        alloc = [a for a in plan.allocate(chosen5, regime, gross=0.35) if a.get("result")]
        total = sum(a["weight"] for a in alloc)
        check("目标总敞口≈35%", abs(total - 0.35) < 0.02, f"{total:.4f}")
        check("单票≤10%", max(a["weight"] for a in alloc) <= 0.10 + 1e-9,
              f"{max(a['weight'] for a in alloc):.4f}")
        sleeve_share = alloc[0]["weight"] / total if total else 0
        check("第一名占个股层 <35%", sleeve_share < 0.35, f"{sleeve_share:.3f}")
        check("4 只都有仓位", len(alloc) == 4)

        print("6) 组仓：同行业两只 → 行业上限 25% 生效（跳过一只、不超配）")
        same = [mk("a", "甲", 72.6, "买入区", "同行业"),
                mk("b", "乙", 71.0, "买入区", "同行业"),
                mk("c", "丙", 70.5, "买入区", "另一个")]
        alloc6 = [a for a in plan.allocate(plan.pick(same, regime)[0], regime, gross=0.35)
                  if a.get("result")]
        used = {}
        for a in alloc6:
            ind = a["result"]["industry"]
            used[ind] = used.get(ind, 0.0) + a["weight"]
        check("同行业合计≤25%", max(used.values()) <= 0.25 + 1e-9, str(used))

        print("7) 组仓：总敞口低于 30% → 差额留现金（不补偿性买入）")
        alloc7 = plan.allocate(chosen5, regime, gross=0.20)
        cash = [a for a in alloc7 if a.get("cash")]
        check("出现现金行", len(cash) == 1, str(alloc7))
        check("现金行说明原因", "低于最低" in (cash[0].get("note") or "") if cash else False)

        print("8) conviction 开关：核心仓 1.0；观察区默认不试探（0 权重），可回滚 0.5")
        config.EXEC["stock_weighting"] = "conviction"

        ch8, note8 = plan.pick(res, regime)
        check("核心仓 1 只、观察区 0 权重不试探",
              [c["mult"] for c in ch8] == [1.0], str([c["mult"] for c in ch8]))
        check("note 不含试探仓", "试探仓" not in note8, note8)
        # 回滚开关：EXEC.watch_weight 改回 0.5，观察区恢复「半仓试探」的旧行为。
        config.EXEC["watch_weight"] = 0.5
        try:
            ch8b, note8b = plan.pick(res, regime)
        finally:
            config.EXEC["watch_weight"] = 0.0
        check("回滚 watch_weight=0.5 → 试探仓 0.5×2",
              [c["mult"] for c in ch8b] == [1.0, 0.5, 0.5], str([c["mult"] for c in ch8b]))
        check("回滚后 note 含试探仓", "试探仓" in note8b, note8b)
        config.EXEC["stock_weighting"] = "rank"

        print("9) 配置默认值")
        check("stock_weighting 默认 rank", mode0 == "rank", str(mode0))
        check("兜底 max_stock_names=4", int(config.EXEC.get("max_stock_names")) == 4)
        check("rank_weights 4 档", list(config.EXEC.get("rank_weights") or []) ==
              [1.0, 0.85, 0.70, 0.55])

        print("10) decide.py --refresh 开关：默认不刷新，打开后注入环境变量")
        import decide
        check("默认关闭", decide._FORCE_REFRESH is False)
        check("默认子进程无刷新变量", "A_SHARE_FORCE_REFRESH" not in decide._env())
        decide._FORCE_REFRESH = True
        check("打开后子进程带 A_SHARE_FORCE_REFRESH=1",
              decide._env().get("A_SHARE_FORCE_REFRESH") == "1")
        decide._FORCE_REFRESH = False

        print("11) datasource._force_refresh 读环境变量")
        import datasource
        old = os.environ.get("A_SHARE_FORCE_REFRESH")
        os.environ.pop("A_SHARE_FORCE_REFRESH", None)
        check("未设置=False", datasource._force_refresh() is False)
        os.environ["A_SHARE_FORCE_REFRESH"] = "1"
        check("设置为 1=True", datasource._force_refresh() is True)
        os.environ["A_SHARE_FORCE_REFRESH"] = "0"
        check("设置为 0=False", datasource._force_refresh() is False)
        if old is None:
            os.environ.pop("A_SHARE_FORCE_REFRESH", None)
        else:
            os.environ["A_SHARE_FORCE_REFRESH"] = old
        print("11b) run.py --refresh 开关：与 decide.py 同语义（整条管线强刷）")
        import argparse as _argparse
        import inspect as _inspect

        import run as run_mod
        _bak = os.environ.get("A_SHARE_FORCE_REFRESH")
        os.environ.pop("A_SHARE_FORCE_REFRESH", None)
        run_mod.apply_refresh(_argparse.Namespace(refresh=False))
        check("--refresh 关闭不设变量", "A_SHARE_FORCE_REFRESH" not in os.environ)
        run_mod.apply_refresh(_argparse.Namespace(refresh=True))
        check("--refresh 打开设 A_SHARE_FORCE_REFRESH=1",
              os.environ.get("A_SHARE_FORCE_REFRESH") == "1")
        check("argparse 注册了 --refresh（仅工作日手工补数/调试用）",
              '"--refresh"' in _inspect.getsource(run_mod.main))
        if _bak is None:
            os.environ.pop("A_SHARE_FORCE_REFRESH", None)
        else:
            os.environ["A_SHARE_FORCE_REFRESH"] = _bak
        print("12) R2 场外兜底：ETF 一条都配不出时由场外债券基金承接预算（小样本 CSV）")
        tmp = tempfile.mkdtemp(prefix="otc_selftest_")
        old_out = plan.OUT
        _otc_cols = ["code", "name", "ftype", "nav_date", "unit_nav", "acc_nav",
                     "purchase_status", "min_purchase", "day_limit", "fee", "hist_days",
                     "ret_1y", "ret_3y", "dd_1y", "dd_3y", "vol_3y", "mgmt_fee",
                     "redeem_fee_hold", "cost_1y", "score", "level", "level_reason",
                     "key", "share_class"]

        def _otc_row(**kw: object) -> dict:
            base = {"code": "001021", "name": "中短债A", "ftype": "债券型",
                    "nav_date": "2026-10-24", "unit_nav": 1.2345, "acc_nav": 1.5,
                    "purchase_status": "开放申购", "min_purchase": 10,
                    "day_limit": 1000000, "fee": 0.0, "hist_days": 1200,
                    "ret_1y": 0.0136, "ret_3y": 0.041, "dd_1y": -0.0043,
                    "dd_3y": -0.02, "vol_3y": 0.01, "mgmt_fee": 0.3,
                    "redeem_fee_hold": 0.0, "cost_1y": 0.003, "score": 88.0,
                    "level": "R2", "level_reason": "短债基金", "key": "中短债A",
                    "share_class": "A"}
            base.update(kw)
            return base

        otc_rows = [
            _otc_row(),
            _otc_row(code="000086", name="短债B", score=80.0, fee=0.03,
                     min_purchase=100, cost_1y=0.004, key="短债B"),
            _otc_row(code="003358", name="高分红债C", score=95.0, fee=0.10,
                     min_purchase=1000, cost_1y=0.006, key="高分红债C"),
            _otc_row(code="009999", name="暂停申购D", score=99.0,
                     purchase_status="暂停申购", key="暂停申购D"),
            _otc_row(code="008888", name="可转债E", score=99.0, level="R3",
                     level_reason="可转债增强", key="可转债E"),
        ]
        try:
            plan.OUT = tmp
            with open(os.path.join(tmp, "stage1_otc.csv"), "w", encoding="utf-8-sig",
                      newline="") as f:
                w = csv.DictWriter(f, fieldnames=_otc_cols)
                w.writeheader()
                for r in otc_rows:
                    w.writerow(r)
            pool, note = plan.load_otc_pool()
            codes = [p["code"] for p in pool]
            check("读到 3 只（暂停申购/R3 被剔除）", len(pool) == 3, str(codes))
            check("按综合分降序", [p["score"] for p in pool] == [95.0, 88.0, 80.0],
                  str([p["score"] for p in pool]))
            check("code 保留前导 0", "001021" in codes, str(codes))
            check("note 写明候选数", "3" in note, note)
            pool2, _ = plan.load_otc_pool()
            check("两次读取结果一致（可复现）", pool == pool2)
            check("赎回费取到 redeem_fee_hold（持满档口径）",
                  all(p.get("redeem_fee_hold") is not None for p in pool),
                  str([p.get("redeem_fee_hold") for p in pool]))
            # 旧列名（redeem_fee_7d，语义是「刚满 7 天」最严档）不兼容读取：必须有 note 指路，
            # 否则旧产物的错口径会静默进入计划、成本被高估。
            old_cols = ["redeem_fee_7d" if c == "redeem_fee_hold" else c for c in _otc_cols]
            with open(os.path.join(tmp, "stage1_otc.csv"), "w", encoding="utf-8-sig",
                      newline="") as f:
                w = csv.DictWriter(f, fieldnames=old_cols)
                w.writeheader()
                for r in otc_rows:
                    w.writerow({("redeem_fee_7d" if k == "redeem_fee_hold" else k): v
                                for k, v in r.items()})
            pool_old, old_note = plan.load_otc_pool()
            check("旧列名 redeem_fee_7d 不兼容（不静默读、返回空池）",
                  pool_old == [], str(old_note))
            check("缺列时 note 指路「重跑 otc.py」且说明成本会被高估",
                  "redeem_fee_hold" in old_note and "重跑" in old_note
                  and "高估" in old_note, old_note)

            res = plan.otc_layer(pool, 1400.0, 10000.0, 0.12)
            check("场外承接了非空仓位", len(res["positions"]) >= 1, str(res["note"]))
            check("合计不超 R2 预算", res["spent"] <= 1400.0 + 1e-9, f"{res['spent']}")
            check("单只不超 12% 上限",
                  max(p["amount"] for p in res["positions"]) <= 1200.0 + 1e-9,
                  str([p["amount"] for p in res["positions"]]))
            check("金额是 100 元整数倍",
                  all(abs(p["amount"] % 100) < 1e-9 for p in res["positions"]),
                  str([p["amount"] for p in res["positions"]]))
            check("金额 ≥ max(起购,100)",
                  all(p["amount"] >= max(p["min_purchase"], 100) - 1e-9
                      for p in res["positions"]),
                  str([(p["amount"], p["min_purchase"]) for p in res["positions"]]))
            check("venue=OTC 且 spent>0", res["venue"] == "OTC" and res["spent"] > 0)

            pos = plan.build_otc_positions(res["positions"], 10000.0)
            check("持仓 venue=OTC", all(p["venue"] == "OTC" for p in pos))
            check("code 形如 OTC:xxxxxx",
                  all(p["code"].startswith("OTC:") and len(p["code"]) > 4 for p in pos),
                  str([p["code"] for p in pos]))
            check("场外持仓走影账口径（fill_model=otc_nav_t1）",
                  all(p.get("fill_model") == "otc_nav_t1" for p in pos))
            check("不再带 paper_unsupported 死标记",
                  all("paper_unsupported" not in p for p in pos))
            check("带 nav_date/unit_nav/fee",
                  all(p.get("nav_date") and p.get("unit_nav") is not None
                      and p.get("fee") is not None for p in pos))
            check("金额合计与 spent 一致",
                  abs(sum(p["amount"] for p in pos) - res["spent"]) < 0.011)
            # 赎回费文案必须同时写下限（7–30 天档仍收费）与 90 天口径，别把 R2 读成活期
            rule = plan._otc_fee_rule_txt()
            check("赎回费常驻说明含下限档位 0.10%–0.75%", "0.10%–0.75%" in rule, rule)
            check("赎回费常驻说明含 90 天成本口径",
                  "90 天" in rule and "90 天" in plan._otc_fee_txt(res["positions"]),
                  plan._otc_fee_txt(res["positions"]))

            print("13) 场内优先 / 场外兜底互斥；--no-otc 退回现金")
            etf_pool = [{"sym": "511010", "name": "国债ETF", "key": "国债ETF",
                         "price": 1.0, "score": 90.0, "ret_1y": 0.03, "dd_1y": -0.01,
                         "amt20_yi": 5.0, "level": "R2", "level_reason": "场内债券ETF"}]
            w1 = plan.wealth_layer(etf_pool, 5000.0, 100000.0, otc_pool=pool)
            check("场内买得起 → ETF 优先、场外为空",
                  bool(w1["positions"]) and w1["otc_positions"] == [], w1["note"])
            w2 = plan.wealth_layer([], 1400.0, 10000.0, otc_pool=pool)
            check("ETF 池空 → 场外承接且 venue=OTC",
                  w2["positions"] == [] and bool(w2["otc_positions"])
                  and w2["venue"] == "OTC", w2["note"])
            w3 = plan.wealth_layer([], 1400.0, 10000.0, otc_pool=pool, allow_otc=False)
            check("--no-otc → 不配场外、退回现金",
                  w3["otc_positions"] == [] and w3["venue"] == "CASH", w3["note"])
            # 代码层证据：开关关掉时即使有场外池，也不许在场内买得起的情况下偷塞场外
            w4 = plan.wealth_layer(etf_pool, 5000.0, 100000.0, otc_pool=pool, allow_otc=False)
            check("--no-otc + 场内买得起 → venue 不是 OTC、场外仓位为空",
                  bool(w4["positions"]) and w4["venue"] != "OTC"
                  and w4["otc_positions"] == [], str(w4["venue"]))
        finally:
            plan.OUT = old_out
            shutil.rmtree(tmp, ignore_errors=True)

        print("14) 场外池缺失/坏掉不抛异常（不许打断主流程）")
        missing_dir = os.path.join(tempfile.gettempdir(), "otc_selftest_missing_dir")
        shutil.rmtree(missing_dir, ignore_errors=True)
        os.makedirs(missing_dir, exist_ok=True)
        try:
            plan.OUT = missing_dir
            pool_m, note_m = plan.load_otc_pool()
            check("缺失返回空列表", pool_m == [])
            check("缺失 note 写明「场外池缺失」", "场外池缺失" in note_m, note_m)
            with open(os.path.join(missing_dir, "stage1_otc.csv"), "w",
                      encoding="utf-8-sig") as f:
                f.write("code,name\n")            # 只有表头 → 空表
            pool_e, note_e = plan.load_otc_pool()
            check("空表返回空列表 + note", pool_e == [] and "场外池缺失" in note_e, note_e)
        finally:
            plan.OUT = old_out
            shutil.rmtree(missing_dir, ignore_errors=True)

        print("15) config 里 R2 场外阈值默认值（T2 未改也要能跑）")
        oc = (config.RISK.get("wealth") or {}).get("otc") or {}
        check("otc.min_position_value 默认 100",
              float(oc.get("min_position_value") or 100.0) == 100.0)
        check("R2 单只上限由 RISK.levels.R2.single_cap 提供",
              float(config.RISK["levels"]["R2"].get("single_cap") or 0) ==
              float(plan.config.RISK["levels"]["R2"]["single_cap"]))

        print("16) 两池 code 级互斥：重复必须 fail loud（不许静默把同一敞口算两次）")
        try:
            plan.check_pool_duplicates([{"sym": "sh511520", "name": "政金债ETF"}],
                                       [{"code": "511520", "name": "重复的场外基金"}])
            raised_dup = False
        except ValueError as e:
            raised_dup = "重复" in str(e)
        check("归一化后同一 code → 抛 ValueError", raised_dup)
        plan.check_pool_duplicates([{"sym": "sh511520"}], [{"code": "000033"}])
        check("不同 code → 不抛错", True)
        # 真实产物（out/ 里两个池都在时）：喂进互斥检查必须不抛，否则说明两腿重复
        real_etf, _ = plan.load_wealth_pool()
        real_otc, _ = plan.load_otc_pool()
        try:
            plan.check_pool_duplicates(real_etf, real_otc)
            check("真实产物两池无重复（文件缺失时也通过）", True,
                  f"场内 {len(real_etf)} 只 / 场外 {len(real_otc)} 只")
        except ValueError as e:
            check("真实产物两池无重复（文件缺失时也通过）", False, str(e))

        print("17) 焦点层顺序可复现（旧 bug：list(set()) 随 PYTHONHASHSEED 变）")
        import risk                      # noqa: E402  —— 只在这一节用，避免顶部多一个依赖
        want = [lv for lv in risk.ORDER if lv in risk.INCLUDE]
        got = risk.focus_order()
        check("focus_order() 返回 list", isinstance(got, list), repr(type(got)))
        check("focus_order() == 按 ORDER 定序", got == want, f"{got} vs {want}")
        check("连续调用两次相同", risk.focus_order() == risk.focus_order())
        # 换两个 PYTHONHASHSEED 起子进程：这才是真正能复现/证伪 set 顺序 bug 的检查
        sc_dir = os.path.dirname(os.path.abspath(plan.__file__))
        outs = []
        for seed in ("0", "1"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            try:
                cp = subprocess.run(
                    [sys.executable, "-c",
                     "import sys; sys.path.insert(0, sys.argv[1]); import risk; "
                     "print(risk.focus_order())", sc_dir],
                    capture_output=True, text=True, env=env, timeout=120)
                outs.append(cp.stdout.strip())
            except Exception as e:       # noqa: BLE001
                outs.append(f"<error: {e}>")
        check("PYTHONHASHSEED=0/1 两次 focus 输出全等",
              len(outs) == 2 and outs[0] == outs[1] and outs[0].startswith("["),
              f"{outs[0]!r} vs {outs[1]!r}")
    finally:
        config.EXEC["stock_weighting"] = mode0


    # ---- 18) 研究闸：把 T2/T4 子代理的结论接进引擎
    print("\n18) 研究闸：reject 剔除 / downgrade 权重×0.5 / 缺文件放行")
    import shutil as _sh
    import tempfile as _tf

    def _mk(code, name, total):
        return {"code": code, "name": name, "verdict": "买入区",
                "scores": {"total": total}, "metrics": {}}

    _tmp = _tf.mkdtemp(prefix="rg_")
    _old_out = config.OUT_DIR
    try:
        config.OUT_DIR = _tmp
        _res = [_mk("000999", "华润三九", 72), _mk("002648", "卫星化学", 69),
                _mk("601058", "赛轮轮胎", 67)]
        _out, _info = plan.apply_research([dict(x) for x in _res])
        check("缺 stage1b_research.csv 时原样放行", len(_out) == 3 and _info["applied"] is False)
        check("note 写明研究层未启用", "未启用" in _info["note"])

        with open(os.path.join(_tmp, "stage1b_research.csv"), "w",
                  encoding="utf-8-sig", newline="") as _f:
            _f.write("code,action,reason,need_deep,confidence\n")
            _f.write("000999,reject,商誉暴雷风险,,\n")
            _f.write("002648,downgrade,减持待核,,\n")
            _f.write("601058,keep,无异常,,\n")
        _out, _info = plan.apply_research([dict(x) for x in _res])
        _codes = [x["code"] for x in _out]
        check("reject 被剔除", "000999" not in _codes, str(_codes))
        check("reject 记进 research_filter", _info["rejected"] == ["000999"])
        check("downgrade 权重系数 0.5",
              [x for x in _out if x["code"] == "002648"][0]["_research_mult"] == 0.5)
        check("keep 权重系数 1.0",
              [x for x in _out if x["code"] == "601058"][0]["_research_mult"] == 1.0)

        plan._RESEARCH_INFO.clear()
        plan._RESEARCH_INFO.update(_info)
        _chosen, _note = plan.pick(_out, {"base_gross": 0.35})
        check("剔除后篮子剩 2 只", len(_chosen) == 2, str(len(_chosen)))
        check("降级票系数真乘进权重（1.00×0.5）",
              abs([c for c in _chosen if c["r"]["code"] == "002648"][0]["mult"] - 0.5) < 1e-9)
        check("计划 note 带研究层结论", "研究层" in _note, _note)
        check("note 拆开显示「排名权重 × 研究乘数」（1.00×0.50 可解释）",
              "排名权重 1.00" in _note and "研究乘数 0.50" in _note, _note)

        # 池外代码必须被忽略：研究层只能否决/降权池内标的，不许加新票（SKILL §3.1）
        with open(os.path.join(_tmp, "stage1b_research.csv"), "w",
                  encoding="utf-8-sig", newline="") as _f:
            _f.write("code,action,reason,need_deep,confidence\n")
            _f.write("000999,reject,商誉暴雷风险,,\n")
            _f.write("999999,reject,池外标的,,\n")
            _f.write("888888,downgrade,池外标的,,\n")
        _out, _info = plan.apply_research([dict(x) for x in _res])
        check("池外代码不改变篮子（仍只剩池内 2 只）", len(_out) == 2, str([x["code"] for x in _out]))
        check("ignored_outside 记下池外代码", _info.get("ignored_outside") == ["888888", "999999"],
              str(_info.get("ignored_outside")))
        check("rejected 只含池内代码", _info["rejected"] == ["000999"], str(_info["rejected"]))
        check("note 写明忽略池外", "忽略池外代码" in _info["note"], _info["note"])

        # 新鲜度：产物早于本期数据日 → 不采用（防上一轮结论顶替本期，2026-11-21 大检查）
        import datetime as _d
        _p = os.path.join(_tmp, "stage1b_research.csv")
        with open(_p, "w", encoding="utf-8-sig", newline="") as _f:
            _f.write("code,action,reason,need_deep,confidence\n")
            _f.write("000999,reject,上期结论,,\n")
        _stamp = _d.datetime(2026, 11, 14, 22, 0).timestamp()
        os.utime(_p, (_stamp, _stamp))
        _out, _info = plan.apply_research([dict(x) for x in _res], as_of="2026-11-20")
        check("研究产物早于数据日不采用", len(_out) == 3 and _info["applied"] is False)
        check("note 写明旧研究不采用", "早于本期数据日" in _info["note"], _info["note"])
        _out, _info = plan.apply_research([dict(x) for x in _res], as_of="2026-11-14")
        check("数据日与产物同日仍采用", _info["applied"] is True and len(_out) == 2,
              _info["note"])

        open(os.path.join(_tmp, "stage1b_research.csv"), "w",
             encoding="utf-8").write("code,action\n")
        _out, _info = plan.apply_research([dict(x) for x in _res])
        check("空表原样放行", len(_out) == 3 and _info["applied"] is False)
    finally:
        config.OUT_DIR = _old_out
        _sh.rmtree(_tmp, ignore_errors=True)

    # ---------------- 新闻闸：news.code_scores() 聚合 + plan.apply_news() 剔除/调权
    _tmp = _tf.mkdtemp(prefix="ng_")
    _old_out = config.OUT_DIR
    try:
        config.OUT_DIR = _tmp
        _hdr = ("layer,code,name,kind,level,tag,date,title,source,url,"
                "r_score,c_score,net_score,tier,mult\n")
        _rows = [
            "候选,600111,北方稀土,公告,高,立案调查,2026-10-28,,,,65,0,-65,否决,0\n",
            "候选,600222,太龙药业,新闻,高,减持,2026-10-28,,,,42,0,-42,降权40,0.6\n",
            "候选,600333,长春燃气,新闻,中,减持,2026-10-28,,,,30,0,-30,降权15,0.85\n",
            "候选,600444,国机通用,新闻,中,,2026-10-28,,,,0,23,23,加分15,1.15\n",
            "候选,600555,九龙山,新闻,中,,2026-10-28,,,,0,16,16,加分10,1.1\n",
            "候选,600555,九龙山,新闻,低,,2026-10-27,,,,0,0,0,不干预,1.0\n",
        ]
        with open(os.path.join(_tmp, "news_events.csv"), "w", encoding="utf-8-sig") as f:
            f.write(_hdr + "".join(_rows))
        _sc = news.code_scores()
        check("新闻聚合：立案 → 否决/mult 0",
              _sc["600111"]["mult"] == 0.0 and _sc["600111"]["tier"] == "否决", str(_sc.get("600111")))
        check("新闻聚合：R42 → ×0.6", abs(_sc["600222"]["mult"] - 0.6) < 1e-9)
        check("新闻聚合：R30 → ×0.85 且带 tag", abs(_sc["600333"]["mult"] - 0.85) < 1e-9
              and _sc["600333"]["tags"] == ["减持"])
        check("新闻聚合：C23 → ×1.15", abs(_sc["600444"]["mult"] - 1.15) < 1e-9)
        check("新闻聚合：同码加分不被中性事件稀释",
              abs(_sc["600555"]["mult"] - 1.1) < 1e-9, str(_sc.get("600555")))

        _res2 = [mk("600111", "北方稀土", 80.0, "买入区"),
                 mk("600222", "太龙药业", 78.0, "买入区"),
                 mk("600444", "国机通用", 70.0, "买入区")]
        _out2, _info2 = plan.apply_news([dict(x) for x in _res2])
        check("新闻闸：否决票被剔除", [r["code"] for r in _out2] == ["600222", "600444"],
              str([r["code"] for r in _out2]))
        check("新闻闸：降权票带 ×0.6 系数",
              [r for r in _out2 if r["code"] == "600222"][0]["_news_mult"] == 0.6)
        check("新闻闸：加价票带 ×1.15 系数",
              [r for r in _out2 if r["code"] == "600444"][0]["_news_mult"] == 1.15)
        plan._NEWS_INFO.clear()
        plan._NEWS_INFO.update(_info2)
        _ch2, _note2 = plan.pick(_out2, {"base_gross": 0.35})
        _w = list(config.EXEC.get("rank_weights") or [1.0, 0.85, 0.70, 0.55])
        _exp = round((_w[1] if len(_w) > 1 else _w[-1]) * 1.15, 4)
        check(f"新闻闸系数真乘进权重（{_w[1]:.2f}×1.15={_exp}）",
              abs([c for c in _ch2 if c["r"]["code"] == "600444"][0]["mult"] - _exp) < 1e-9,
              str([(c["r"]["code"], c["mult"]) for c in _ch2]))
        check("计划 note 带新闻闸结论", "新闻闸" in _note2, _note2)
        check("note 拆开显示「排名权重 × 新闻乘数」（0.85×1.15 可解释）",
              "排名权重 0.85" in _note2 and "新闻乘数 1.15" in _note2, _note2)

        # 新鲜度：新闻产物早于本期数据日 → 不采用（防上周事件继续改本周权重）
        import datetime as _d
        _p = os.path.join(_tmp, "news_events.csv")
        with open(_p, "w", encoding="utf-8-sig") as f:
            f.write(_hdr + "候选,600111,北方稀土,公告,高,立案调查,2026-10-28,,,,65,0,-65,否决,0\n")
        _stamp = _d.datetime(2026, 11, 14, 22, 0).timestamp()
        os.utime(_p, (_stamp, _stamp))
        _out5, _info5 = plan.apply_news([dict(x) for x in _res2], as_of="2026-11-20")
        check("旧新闻产物不采用", len(_out5) == 3 and _info5["applied"] is False,
              _info5.get("note"))
        check("note 写明旧新闻不采用", "早于本期数据日" in _info5["note"], _info5["note"])

        os.remove(os.path.join(_tmp, "news_events.csv"))
        _out3, _info3 = plan.apply_news([dict(x) for x in _res2])
        check("缺 news_events.csv → 原样放行",
              len(_out3) == 3 and _info3["applied"] is False, _info3.get("note"))
        _gate0 = config.NEWS.get("gate")
        config.NEWS["gate"] = False
        _out4, _info4 = plan.apply_news([dict(x) for x in _res2])
        check("开关 gate=False → 只记录不干预",
              len(_out4) == 3 and _info4["applied"] is False and "关闭" in _info4["note"])
        config.NEWS["gate"] = _gate0
    finally:
        plan._NEWS_INFO.clear()
        config.OUT_DIR = _old_out
        _sh.rmtree(_tmp, ignore_errors=True)

    # ---------------- ETF 批次：默认单批（B 类 #8），可回滚三批
    print("19) ETF 批次：默认单批（≥1,500 元），三批模式下才合并碎单")
    _pool = [{"sym": "sh510050", "name": "上证50ETF华夏", "cls": "宽基",
              "underlying": "上证50", "price": 2.862, "score": 57.89,
              "ret_3y": 0.102, "dd_3y": -0.1519, "amt20_yi": 26.86, "level": "R3"}]
    _er = plan.etf_layer(_pool, 2892.0, 50000.0, {}, 2892.0 / 50000)
    _pos = _er["positions"][0]
    check("ETF 默认批次模式 = single（可回滚 three）",
          str(config.ETF.get("tranche_mode")) == "single", config.ETF.get("tranche_mode"))
    _steps = plan.etf_ladder_steps(_pos["price"], _pos["lots"])
    check("5 万防守档：ETF 不走 3 批（只出 1 批）", len(_steps) == 1,
          str([(s["tranche"], s["amount"]) for s in _steps]))
    check("单批金额 ≥1,500 元（或本金买不到 1,500 时至少整手一批）",
          _steps[0]["amount"] >= 1500 or _steps[0]["lots"] == _pos["lots"],
          str(_steps[0]["amount"]))
    check("单批记下合并来源 1/2/3", _steps[0]["merged_from"] == [1, 2, 3],
          str(_steps[0].get("merged_from")))
    check("单批带说明（可回滚 three）", "单批" in (_steps[0].get("merge_note") or ""),
          _steps[0].get("merge_note"))
    check("单批股数守恒 = 原整手股数",
          sum(s["shares"] for s in _steps) == _pos["lots"] * config.CAPITAL["lot_size"],
          str([s["shares"] for s in _steps]))
    _steps3 = plan.etf_ladder_steps(_pos["price"], _pos["lots"], mode="three")
    check("回滚 three：每批金额 ≥800 元",
          all(s["amount"] >= 800 for s in _steps3),
          str([(s["tranche"], s["amount"]) for s in _steps3]))
    check("three 模式碎单合并有说明",
          len(_steps3) <= 3 and any(s.get("merge_note") for s in _steps3),
          str([(s["tranche"], s["amount"], s.get("merge_note")) for s in _steps3]))
    check("three 模式股数守恒",
          sum(s["shares"] for s in _steps3) == _pos["lots"] * config.CAPITAL["lot_size"],
          str([s["shares"] for s in _steps3]))
    _small = plan.etf_ladder_steps(2.87, 3)
    check("3 手（约 861 元）→ 单批（不够拆也不硬拆）",
          len(_small) == 1 and _small[0]["amount"] >= 800,
          str([(s["tranche"], s["amount"]) for s in _small]))

    # ---------------- 个股批次：单批 <5,000 元按手数合并（B 类 #3）
    print("19b) 个股三批：单批 <5,000 元 → 合并到相邻批次，不突破整手/10% 上限")
    _lad = [{"tranche": 1, "ratio": 0.40, "price": 10.0, "lots": 4, "shares": 400,
             "amount": 4000.0, "merged_from": []},
            {"tranche": 2, "ratio": 0.35, "price": 9.5, "lots": 3, "shares": 300,
             "amount": 2850.0, "merged_from": []},
            {"tranche": 3, "ratio": 0.25, "price": 9.0, "lots": 2, "shares": 200,
             "amount": 1800.0, "merged_from": []}]
    _mg = plan._merge_stock_ladder(_lad, 5000.0, 100)
    check("三批都 <5,000 → 合成 1 批", len(_mg) == 1, str([(s["tranche"], s["amount"]) for s in _mg]))
    check("合并后金额 = 总手数 × 合并批价格（一次买入只有一个价）",
          abs(_mg[0]["amount"] - 9000.0) < 1e-6, str(_mg[0]["amount"]))
    check("合并记录 merged_from（第 1 批留下，2/3 并入）", _mg[0]["merged_from"] == [2, 3],
          str(_mg[0].get("merged_from")))
    check("合并说明写明 5,000 元下限", "5,000" in (_mg[0].get("merge_note") or ""),
          _mg[0].get("merge_note"))
    _mg2 = plan._merge_stock_ladder(
        [dict(_lad[0], amount=6000.0), dict(_lad[1], amount=3000.0),
         dict(_lad[2], amount=5500.0)], 5000.0, 100)
    check("大额批各自独立、只并小额批", all(s["amount"] >= 5000 or len(_mg2) == 1 for s in _mg2),
          str([(s["tranche"], s["amount"]) for s in _mg2]))
    check("合并后仍按手数整手（shares = lots×100）",
          all(s["shares"] == s["lots"] * 100 for s in _mg2),
          str([(s["lots"], s["shares"]) for s in _mg2]))

    # ---------------- 观察区：0 权重、0 手、0 元、不进订单（B 类 #5）
    print("19c) 观察区：权重 0、不建仓，计划里仍列出观察名单与原因")
    _wp = plan.watch_position({"result": mk("600999", "观察股", 66.0, "观察"),
                               "rank": 3, "weight": 0.0, "watch_only": True})
    check("观察区 0 手 0 元", _wp["lots"] == 0 and _wp["shares"] == 0
          and _wp["amount"] == 0.0 and _wp["weight"] == 0.0, str(_wp))
    check("观察区标记 watch_only", _wp.get("watch_only") is True)
    check("观察区不含批次（不会生成订单）", _wp.get("ladder") == [])
    check("观察区保留判定/理由", _wp["verdict"] == "观察" and "不建仓" in _wp["watch_note"],
          _wp.get("watch_note"))
    check("观察区仍给一手成本（让人知道贵不贵）", _wp.get("lot_cost") == 1000.0,
          _wp.get("lot_cost"))
    check("计划 md 明写观察区不建仓", "不建仓" in inspect.getsource(plan.to_markdown))

    # ---------------- 场外参考净值（B 类 #6）
    print("19d) 场外腿：计划带 nav_ref_cum（建单日累计净值）")
    _otc = plan.build_otc_positions(
        [{"code": "012310", "name": "债基A", "amount": 1000.0, "unit_nav": 1.1,
          "acc_nav": 1.2, "unit": 100, "fee": 0.04, "nav_date": "2026-11-06",
          "min_purchase": 10.0, "redeem_fee_hold": 0.0}], 50000.0)
    check("计划场外腿带 nav_ref_cum = 累计净值", _otc[0]["nav_ref_cum"] == 1.2,
          str(_otc[0].get("nav_ref_cum")))
    check("单位净值仍单独保留", _otc[0]["unit_nav"] == 1.1, str(_otc[0].get("unit_nav")))

    # ---------------- 行业修正（B 类 #9）：计划书的行业列也要走同一张修正表
    print("19e) 行业映射：600233 → 交通运输、空值 → 未分类")
    check("600233 圆通速递不再标服装鞋类",
          plan.ind_of({"code": "600233", "industry": "服装鞋类"}) == "交通运输",
          plan.ind_of({"code": "600233", "industry": "服装鞋类"}))
    check("未知/空行业 fail-closed 为未分类",
          plan.ind_of({"code": "999998", "industry": ""}) == "未分类",
          plan.ind_of({"code": "999998", "industry": ""}))

    # ---------------- 现金层：建仓弹药留 T+0（与账本 paper.cash_yield_plan 同口径）
    print("19f) 阶段三现金层：建仓弹药与挂单冻结取较大者留 T+0，不进货基池")
    _bp = plan.build_cash_plan(50000.0, 9500.0, reserved=1000.0, ammo=8000.0)
    check("T+0 保留 = max(弹药 8000, 挂单 1000) = 8000",
          abs(_bp["t0_keep"] - 8000.0) < 1e-6, str(_bp.get("t0_keep")))
    check("观望资金 = 现金 40500 − 8000 = 32500",
          abs(_bp["idle_amount"] - 32500.0) < 1e-6, str(_bp.get("idle_amount")))
    check("货基池 + 朝朝宝侧 = 现金合计",
          abs(_bp["sleeve_amount"] + _bp["money_fund_amount"] - _bp["cash_total"]) < 1e-6,
          f"{_bp['sleeve_amount']}+{_bp['money_fund_amount']} vs {_bp['cash_total']}")
    _bp0 = plan.build_cash_plan(50000.0, 9500.0, reserved=1000.0)
    check("不传 ammo → 旧行为不变（只扣挂单 1000）",
          abs(_bp0["t0_keep"] - 1000.0) < 1e-6, str(_bp0.get("t0_keep")))

    # ---------------- 牛熊探针直接决定档位（熊市→防守、牛市→进攻、深熊→25%、滞回）
    print("19g) market_regime：牛熊立场优先（深熊/熊/牛/普通），其余按 MA200 三档")
    import pandas as pd  # noqa: E402  仅本段用

    _saved = (plan.ds.fetch_index_daily, plan.m.ma, plan._probe_regime)
    try:
        plan.ds.fetch_index_daily = lambda code: pd.DataFrame({"close": [90.0] * 5})

        def _probe(state, bh=3, bu=3, uh=0, uu=3, stance=None, severity=None):
            st = stance or ("bear" if state == "熊市确认" else
                            "bull" if state == "牛市确认" else "normal")
            sv = severity or ("熊" if st == "bear" else "牛" if st == "bull" else "普通")
            return {"state": state, "stance": st, "severity": sv, "stance_why": "测试",
                    "bear": {"hits": bh, "usable": bu}, "bull": {"hits": uh, "usable": uu}}

        # dev = 90/100-1 = -10% → 基础防守
        plan.m.ma = lambda close, n: 100.0
        plan._probe_regime = lambda: _probe("熊市确认")
        _r = plan.market_regime()
        check("熊市确认 → 防守 35%", _r["regime"] == "防守" and abs(_r["base_gross"] - 0.35) < 1e-9, str(_r))
        check("探针状态/立场与 MA200 原始判定都留痕",
              _r["probe_state"] == "熊市确认" and _r["probe_stance"] == "bear"
              and _r["probe_severity"] == "熊" and _r["dev_regime"] == "防守", str(_r))

        # 深熊（回撤 ≤-35% 且宽度<20%）→ 25%
        plan._probe_regime = lambda: _probe("熊市确认", stance="bear", severity="深熊")
        _r = plan.market_regime()
        check("深熊 → 25%", _r["regime"] == "防守" and abs(_r["base_gross"] - 0.25) < 1e-9, str(_r))

        # 滞回：状态中性但立场仍 bear → 防守 35%（不切回 MA200 三档）
        plan._probe_regime = lambda: _probe("中性", stance="bear", severity="熊")
        _r = plan.market_regime()
        check("滞回保留熊市（状态中性）→ 防守 35%", _r["regime"] == "防守"
              and abs(_r["base_gross"] - 0.35) < 1e-9, str(_r))

        # dev = 90/80-1 = +12.5% → 基础进攻；探针熊市 → 压回防守
        plan.m.ma = lambda close, n: 80.0
        plan._probe_regime = lambda: _probe("熊市确认")
        _r = plan.market_regime()
        check("MA200 基础进攻 + 熊市确认 → 防守 35%",
              _r["regime"] == "防守" and _r["dev_regime"] == "进攻", str(_r))

        # dev = -10% → 基础防守；探针牛市 → 提到进攻
        plan.m.ma = lambda close, n: 100.0
        plan._probe_regime = lambda: _probe("牛市确认", 0, 3, 3, 3)
        _r = plan.market_regime()
        check("牛市确认 → 进攻 80%",
              _r["regime"] == "进攻" and abs(_r["base_gross"] - config.EXEC["max_gross"]) < 1e-9, str(_r))

        # 普通/中性/冲突/数据不足 → 按 MA200 三档
        for _st in ("中性", "信号冲突", "数据不足"):
            plan._probe_regime = lambda st=_st: _probe(st, stance="normal")
            _r = plan.market_regime()
            check(f"{_st}（立场普通）→ 按 MA200（防守 35%）",
                  _r["regime"] == "防守" and abs(_r["base_gross"] - 0.35) < 1e-9, str(_r))

        # 探针缺失 → 原三档
        plan._probe_regime = lambda: None
        _r = plan.market_regime()
        check("探针取不到 → 原三档（防守 35%）且 probe_state 为 None",
              _r["regime"] == "防守" and _r["probe_state"] is None, str(_r))
    finally:
        plan.ds.fetch_index_daily, plan.m.ma, plan._probe_regime = _saved

    # ---------------- _probe_regime 缓存新鲜度：旧格式（缺 stance）必须重算
    print("19h) _probe_regime：旧格式产物不被当成新鲜缓存（否则滞回 stance 永远读不到）")
    import regime_probe as _rp  # noqa: E402
    _saved_rp = (_rp.load_doc, _rp.build, _rp.write_doc)
    _now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _writes = []
        _rp.load_doc = lambda p: {"generated_at": _now, "state": "熊市确认"}
        _rp.build = lambda prev=None: {"state": "熊市确认", "stance": "bear",
                                       "severity": "熊", "stance_why": "测试"}
        _rp.write_doc = lambda doc, path: _writes.append(doc)
        _d = plan._probe_regime()
        check("旧格式（无 stance）→ 重算", (_d or {}).get("stance") == "bear", str(_d))
        check("旧格式重算后写回", len(_writes) == 1 and _writes[0].get("stance") == "bear")

        _rp.load_doc = lambda p: {"generated_at": _now, "state": "中性",
                                  "stance": "bear", "severity": "熊"}

        def _boom(prev=None):
            raise AssertionError("有 stance 的缓存不该重算")

        _rp.build = _boom
        _d2 = plan._probe_regime()
        check("有 stance → 直接用缓存", (_d2 or {}).get("stance") == "bear", str(_d2))
    finally:
        _rp.load_doc, _rp.build, _rp.write_doc = _saved_rp

    print(f"\n{'-' * 60}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
