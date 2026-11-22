# -*- coding: utf-8 -*-
"""`fuyao.py` / `xcheck.py` 的离线自测（不出网、不需要 key）。

跑法：python _selftest_fuyao.py
只测「不依赖网络」的部分：key 解析（含官方凭据顺序）、fail-closed、代码格式、日期换算、
信封解析、缓存键稳定性、as-of 过滤、官方契约预校验（窗口/批量/切片）、xcheck 的判定阈值与报告渲染。
真实取数由 `python fuyao.py` / `python xcheck.py` 单独验（要 key，且会出网）。
"""
from __future__ import annotations

import os
import sys

import config
import fuyao
import xcheck

FAIL = []
OK = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if cond:
        OK += 1
    else:
        FAIL.append(f"{name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------- A key 与 fail-closed
def sec_a() -> None:
    print("== A. key 解析 / fail-closed ==")
    saved_env = os.environ.get(fuyao.KEY_ENV)
    os.environ.pop(fuyao.KEY_ENV, None)
    # 没有 key 时：available False；_data 返回 None（不抛）；_http 抛 FuyaoError
    had_file = os.path.exists(fuyao.KEY_FILE)
    if had_file:
        os.rename(fuyao.KEY_FILE, fuyao.KEY_FILE + ".selftest_bak")
    try:
        check("无 key → available() False", fuyao.available() is False)
        check("无 key → api_key() None", fuyao.api_key() is None)
        check("无 key → _data 返回 None", fuyao._data("snapshot", thscodes="600519.SH") is None)
        try:
            fuyao._http("snapshot", thscodes="600519.SH")
            check("无 key → _http 抛 FuyaoError", False, "没有抛")
        except fuyao.FuyaoError:
            check("无 key → _http 抛 FuyaoError", True)
        check("无 key → snapshot 空列表", fuyao.snapshot(["600519"]) == [])
        check("无 key → financials 空列表", fuyao.financials("600519") == [])
        check("无 key → fund_returns 空 dict", fuyao.fund_returns("018074") == {})
        check("无 key → etf_hist None", fuyao.etf_hist("sh510300") is None)
    finally:
        if had_file:
            os.rename(fuyao.KEY_FILE + ".selftest_bak", fuyao.KEY_FILE)
        if saved_env is not None:
            os.environ[fuyao.KEY_ENV] = saved_env
    # 环境变量优先于文件
    os.environ[fuyao.KEY_ENV] = "env-key"
    check("env key 优先", fuyao.api_key() == "env-key")
    os.environ[fuyao.KEY_ENV] = "  spaced-key  "
    check("env key 去空白", fuyao.api_key() == "spaced-key")
    os.environ.pop(fuyao.KEY_ENV, None)


# ---------------------------------------------------------------- B 代码格式
def sec_b() -> None:
    print("== B. 代码格式 thscode / of_code ==")
    cases = [("600519", "600519.SH"), ("000001", "000001.SZ"), ("300033", "300033.SZ"),
             ("830799", "830799.BJ"), ("sh510300", "510300.SH"), ("sz159915", "159915.SZ"),
             ("600519.SH", "600519.SH"), ("82", "000082.SZ"), ("9", "000009.SZ")]
    for raw, want in cases:
        check(f"thscode({raw})", fuyao.thscode(raw) == want, f"得到 {fuyao.thscode(raw)}")
    check("of_code 补零", fuyao.of_code("18074") == "018074.OF")
    check("of_code 已带后缀", fuyao.of_code("018074.OF") == "018074.OF")


# ---------------------------------------------------------------- C 日期与解析
def sec_c() -> None:
    print("== C. 日期换算 / 信封解析 ==")
    ms = fuyao._ms("2024-06-01")
    check("_ms 是当日零点", fuyao._day(ms) == "2024-06-01")
    check("_day(None) None", fuyao._day(None) is None)
    check("_day(0) None", fuyao._day(0) is None)

    env = {"code": 0, "data": {"timestamp": 1, "item": [{"a": 1}, {"a": 2}]}}
    check("_items 取 item", len(fuyao._items(env["data"])) == 2)
    check("_rows_of 计行数", fuyao._rows_of(env["data"]) == 2)
    check("_items(list)", fuyao._items([1, 2, 3]) == [1, 2, 3])
    check("_items(None) 空", fuyao._items(None) == [])
    check("_items(abilities 无 item) 空", fuyao._items({"abilities": []}) == [])

    a = fuyao._sig({"b": "2", "a": "1"})
    b = fuyao._sig({"a": "1", "b": "2"})
    check("_sig 与 dict 顺序无关", a == b, f"{a} vs {b}")
    p1 = fuyao._cache_path("snapshot", {"thscodes": "600519.SH"})
    p2 = fuyao._cache_path("snapshot", {"thscodes": "600519.SH"})
    check("缓存路径稳定", p1 == p2)
    check("缓存路径无非法字符",
          all(ch.isalnum() or ch in "-_." for ch in os.path.basename(p1)),
          os.path.basename(p1))
    check("缓存路径按日期分目录", p1.endswith(".json") and "__" in p1)


# ---------------------------------------------------------------- D 高层函数（打桩）
def sec_d() -> None:
    print("== D. 高层函数（_data 打桩，不出网） ==")
    orig = fuyao._data
    rows = [
        {"fiscal_year": 2025, "period_end_ms": fuyao._ms("2025-12-31"),
         "report_date_ms": fuyao._ms("2026-04-17"), "operating_income": 1.0},
        {"fiscal_year": 2024, "period_end_ms": fuyao._ms("2024-12-31"),
         "report_date_ms": fuyao._ms("2025-04-02"), "operating_income": 2.0},
        {"fiscal_year": 2023, "period_end_ms": fuyao._ms("2023-12-31"),
         "report_date_ms": fuyao._ms("2024-04-03"), "operating_income": 3.0},
    ]
    try:
        fuyao._data = lambda name, ttl_hours=24, **kw: {"item": rows}  # type: ignore
        got = fuyao.annual_rows("600519", 3)
        check("annual_rows 带可读日期",
              got[0]["period_end"] == "2025-12-31" and got[0]["report_date"] == "2026-04-17")
        r = fuyao.financials("600519", statement="income", limit=3)
        check("financials 透传行数", len(r) == 3)
        # as-of：report_date <= 2025-06-01 → 只剩 2024/2023 两期（2025 年报披露日 2026-04-17 被挡）
        asof = fuyao.asof_rows("600519", "2025-06-01", n=3)
        check("asof_rows 挡住未来披露", [x["fiscal_year"] for x in asof] == [2024, 2023],
              str([x["fiscal_year"] for x in asof]))
        check("asof_rows 不超 n", len(fuyao.asof_rows("600519", "2026-12-31", n=2)) == 2)

        fuyao._data = lambda name, ttl_hours=24, **kw: {  # type: ignore
            "abilities": [{"ability": "profitability",
                           "indicators": [{"index_id": "index_weighted_avg_roe", "value": "16.07"},
                                          {"index_id": "x", "value": None}]}]}
        ind = fuyao.indicators("000999", "2025-4")
        check("indicators 拍平能力块",
              ind["profitability"]["index_weighted_avg_roe"] == "16.07" and
              ind["profitability"]["x"] is None)
        try:
            fuyao.financials("600519", statement="利润表")
            check("financials 支持中文表名", True)
        except ValueError:
            check("financials 支持中文表名", False, "抛了 ValueError")
        try:
            fuyao.financials("600519", statement="不存在")
            check("financials 拒未知表名", False, "没有抛")
        except ValueError:
            check("financials 拒未知表名", True)

        fuyao._data = lambda name, ttl_hours=24, **kw: {"item": [  # type: ignore
            {"date_ms": fuyao._ms("2026-10-26"), "close_price": 3.0,
             "open_price": 2.9, "high_price": 3.1, "low_price": 2.8,
             "volume": 1, "turnover": 2},
            {"date_ms": fuyao._ms("2026-10-27"), "close_price": 3.2,
             "open_price": 3.0, "high_price": 3.3, "low_price": 3.0,
             "volume": 1, "turnover": 2}]}
        df = fuyao.etf_hist("sh510300")
        check("etf_hist 有 close 列", df is not None and "close" in df.columns)
        check("etf_hist 按日期排序", df["date"].tolist() == ["2026-10-26", "2026-10-27"])
        check("etf_hist 收盘值正确", abs(df["close"].iloc[-1] - 3.2) < 1e-9)
    finally:
        fuyao._data = orig  # type: ignore


# ---------------------------------------------------------------- E xcheck 判定
def sec_e() -> None:
    print("== E. xcheck 判定阈值 / 报告渲染 ==")
    check("收益 0.30pp → 一致", xcheck._grade(0.30, 0.30, 0.80) == "一致")
    check("收益 0.31pp → 接近", xcheck._grade(0.31, 0.30, 0.80) == "接近")
    check("收益 0.81pp → 冲突", xcheck._grade(0.81, 0.30, 0.80) == "冲突")
    check("收益 None → 不可比", xcheck._grade(None, 0.30, 0.80) == "不可比")
    check("负数同样按绝对值", xcheck._grade(-0.85, 0.30, 0.80) == "冲突")

    v, d = xcheck._grade_rel(10.0, 10.5)
    check("PE 相对差 5% → 一致", v == "一致" and abs(d - 5.0) < 1e-6, f"{v} {d}")
    v, d = xcheck._grade_rel(10.0, 11.4)
    check("PE 相对差 14% → 接近", v == "接近", f"{v} {d}")
    v, d = xcheck._grade_rel(10.0, 11.6)
    check("PE 相对差 16% → 冲突", v == "冲突", f"{v} {d}")
    v, d = xcheck._grade_rel(None, 10.0)
    check("PE 我方 None → 不可比", v == "不可比" and d is None)
    v, d = xcheck._grade_rel(10.0, None)
    check("PE 对方 None → 不可比", v == "不可比" and d is None)

    # 报告渲染：空结果也要能出完整骨架
    md = xcheck.to_markdown({"A": [], "B": [], "C": [], "D": []})
    for must in ("# 数据交叉校验报告", "## A.", "## B.", "## C.", "## D.", "## 汇总"):
        check(f"空报告含「{must}」", must in md)
    md2 = xcheck.to_markdown({
        "A": [{"code": "018074", "name": "测试", "ours_ret_1y": 0.0294,
               "theirs_ret_1y": 0.0281, "d1_pp": -0.13, "ours_ret_3y": 0.0308,
               "theirs_ret_3y": 0.0318, "d3_pp": 0.104, "verdict": "一致"}],
        "B": [], "C": [], "D": []})
    check("报告含表格行", "018074" in md2 and "一致" in md2)
    check("汇总计数", "一致 1" in md2, [ln for ln in md2.splitlines() if "一致" in ln][-1:])


# ---------------------------------------------------------------- F 配置与隔离
def sec_f() -> None:
    print("== F. 配置与目录隔离 ==")
    check("config.FUYAO 存在", isinstance(getattr(config, "FUYAO", None), dict))
    check("FUYAO.key_file 在 WORK_DIR 下（不进技能目录）",
          os.path.dirname(os.path.abspath(config.FUYAO["key_file"])) ==
          os.path.abspath(config.WORK_DIR),
          config.FUYAO["key_file"])
    check("FUYAO.role 是 crosscheck",
          config.FUYAO.get("role", "crosscheck") == "crosscheck")
    check("cache 在 CACHE_DIR 下",
          os.path.abspath(fuyao.CACHE_ROOT).startswith(os.path.abspath(config.CACHE_DIR)))
    check("unusable 清单非空", len(config.FUYAO.get("unusable") or []) >= 3)
    src = os.path.dirname(os.path.abspath(fuyao.__file__))
    check("技能目录里没有 key 文件",
          not os.path.exists(os.path.join(src, "fuyao.key")))
    check("xcheck 默认输出在 OUT_DIR",
          os.path.join(os.path.abspath(config.OUT_DIR), "xcheck.md").startswith(
              os.path.abspath(config.OUT_DIR)))


# ---------------------------------------------------------------- G 官方契约
def sec_g() -> None:
    print("== G. 官方契约：key 顺序 / 预校验 / 切片 ==")
    saved = {k: os.environ.get(k) for k in fuyao.KEY_ENVS}
    saved_cred, saved_kf = fuyao.CRED_FILE, fuyao.KEY_FILE
    for k in fuyao.KEY_ENVS:
        os.environ.pop(k, None)
    try:
        check("KEY_ENVS 含官方名", "HITHINK_FINANCE_API_KEY" in fuyao.KEY_ENVS)
        os.environ["FUYAO_API_KEY"] = "old-key"
        os.environ["HITHINK_FINANCE_API_KEY"] = "new-key"
        check("官方 env 名优先于旧名", fuyao.api_key() == "new-key", str(fuyao.api_key()))
        check("key_source 报来源", fuyao.key_source() == "env:HITHINK_FINANCE_API_KEY",
              fuyao.key_source())

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "credentials.env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# comment\nHITHINK_FINANCE_API_KEY=from-cred\n")
            fuyao.CRED_FILE = p
            fuyao.KEY_FILE = os.path.join(td, "no-such-key-file")
            for k in fuyao.KEY_ENVS:
                os.environ.pop(k, None)
            check("官方凭据文件可解析", fuyao.api_key() == "from-cred", str(fuyao.api_key()))
            with open(p, "w", encoding="utf-8") as f:
                f.write('HITHINK_FINANCE_API_KEY="quoted"\n')
            check("带引号的凭据文件被拒", fuyao.api_key() is None, str(fuyao.api_key()))
    finally:
        fuyao.CRED_FILE, fuyao.KEY_FILE = saved_cred, saved_kf
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # thscode / 批量 / 窗口预校验
    check("_check_thscode 标准化", fuyao._check_thscode("600519.sh") == "600519.SH")
    try:
        fuyao._check_thscode("600519")
        check("_check_thscode 拒无后缀", False, "没有抛")
    except fuyao.FuyaoError:
        check("_check_thscode 拒无后缀", True)
    check("_check_batch 恰好上限通过",
          len(fuyao._check_batch([f"{i:06d}" for i in range(100)], 100, "x")) == 100)
    try:
        fuyao._check_batch(["x"] * 101, 100, "x")
        check("_check_batch 超限抛错", False, "没有抛")
    except fuyao.FuyaoError:
        check("_check_batch 超限抛错", True)
    fuyao._check_window("2020-01-01", "2030-01-01", 10, "x")
    check("_check_window 恰好 10 年放行", True)
    try:
        fuyao._check_window("2020-01-01", "2030-01-02", 10, "x")
        check("_check_window 超 10 年抛错", False, "没有抛")
    except fuyao.FuyaoError:
        check("_check_window 超 10 年抛错", True)

    # 超长日 K 自动切片：2000→2025 应切成 3 段，首尾相接、不重不漏
    wins = fuyao._slice_windows("2000-01-01", "2025-01-01", 10)
    check("_slice_windows 切 3 段", len(wins) == 3, str(wins))
    check("_slice_windows 首段起点", wins[0][0] == "2000-01-01")
    check("_slice_windows 末段终点", wins[-1][1] == "2025-01-01")
    check("_slice_windows 段间连续",
          all(wins[i][1] < wins[i + 1][0] for i in range(len(wins) - 1)), str(wins))

    orig = fuyao._data
    calls: list[dict] = []

    def fake(name, ttl_hours=24, **kw):
        calls.append(kw)
        return {"item": [{"date_ms": 1, "close_price": 1.0}]}

    try:
        fuyao._data = fake  # type: ignore
        rows = fuyao.historical("600519", "2000-01-01", "2025-01-01")
        check("historical >10 年自动切片（3 次请求）", len(calls) == 3, str(len(calls)))
        check("historical 切片结果拼接", len(rows) == 3)
        calls.clear()
        try:
            fuyao.historical("600519", "2020-01-01", "2021-01-01", adjust="qfq")
            check("historical 拒未知 adjust", False, "没有抛")
        except fuyao.FuyaoError:
            check("historical 拒未知 adjust", True)
        check("historical 拒绝时未出网", calls == [], str(calls))
        calls.clear()
        try:
            fuyao.valuations([f"{i:06d}" for i in range(101)])
            check("valuations 超 100 只抛错", False, "没有抛")
        except fuyao.FuyaoError:
            check("valuations 超 100 只抛错", True)
        check("valuations 拒绝时未出网", calls == [], str(calls))
        # index_hist：近 5 年外的起点在本地拦下
        calls.clear()
        try:
            fuyao.index_hist("000300.SH", start="2015-01-01", end="2016-01-01")
            check("index_hist 拦 5 年外窗口", False, "没有抛")
        except fuyao.FuyaoError:
            check("index_hist 拦 5 年外窗口", True)
        check("index_hist 拒绝时未出网", calls == [], str(calls))
        # financials：period / 窗口
        try:
            fuyao.financials("600519", period="monthly")
            check("financials 拒未知 period", False, "没有抛")
        except fuyao.FuyaoError:
            check("financials 拒未知 period", True)
        try:
            fuyao.financials("600519", start="2010-01-01", end="2025-01-01")
            check("financials 拦 >10 年窗口", False, "没有抛")
        except fuyao.FuyaoError:
            check("financials 拦 >10 年窗口", True)
    finally:
        fuyao._data = orig  # type: ignore


def main() -> int:
    for fn in (sec_a, sec_b, sec_c, sec_d, sec_e, sec_f, sec_g):
        fn()
    print()
    if FAIL:
        print(f"[_selftest_fuyao] FAIL {len(FAIL)} 项 / PASS {OK} 项")
        for f in FAIL:
            print("  [FAIL] " + f)
        return 1
    print(f"[_selftest_fuyao] PASS 全部 {OK} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
