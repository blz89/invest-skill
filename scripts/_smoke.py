# -*- coding: utf-8 -*-
"""冒烟测试：用贵州茅台验证取数 + 指标计算是否正确。

期望值（公开数据，2025 年报 / 2026 年 10 月）：
- 2025 年 ROE 约 34–36%
- 毛利率约 91–92%
- 资产负债率约 15–20%
- PE(TTM) 约 19–21，PB 约 6.0–6.8
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config, datasource as ds, metrics as m

CODE = "600519"

fin = ds.fetch_fin(CODE)
daily = ds.fetch_daily(CODE)
pe = ds.fetch_valuation(CODE, "市盈率(TTM)")
pb = ds.fetch_valuation(CODE, "市净率")

ann = m.annual_rows(fin)
print("=== 财务行数 ===", len(fin), "| 年报行数", len(ann))
print("年报日期:", [d.strftime("%Y") for d in ann["日期"]][-6:])

print("\n=== 近5个年报 ===")
for _, r in ann.tail(5).iterrows():
    print(f"{r['日期'].strftime('%Y')}  ROE={m.roe(r):6.2f}%  毛利率={m.gross_margin(r):6.2f}%  "
          f"负债率={m.debt_ratio(r):6.2f}%  OCF/NI={m.ocf_to_ni(r)}  "
          f"营收增={m.rev_growth(r)}  净利增={m.profit_growth(r)}")

last = m.latest_row(fin)
print("\n最新报告期:", last["日期"].strftime("%Y-%m-%d"))
print("  毛利率(反推)=", m.gross_margin(last), " 负债率=", m.debt_ratio(last))

g3 = m.chain_cagr([m.rev_growth(r) for _, r in ann.tail(3).iterrows()])
p3 = m.chain_cagr([m.profit_growth(r) for _, r in ann.tail(3).iterrows()])
print("  近3年营收CAGR=", None if g3 is None else round(g3, 4),
      " 近3年净利CAGR=", None if p3 is None else round(p3, 4))

print("\n=== 估值 ===")
print("  PE 序列:", len(pe), "最新", round(float(pe.iloc[-1]), 2),
      "分位", round(m.pct_rank(pe, float(pe.iloc[-1])), 4))
print("  PB 序列:", len(pb), "最新", round(float(pb.iloc[-1]), 2),
      "分位", round(m.pct_rank(pb, float(pb.iloc[-1])), 4))

print("\n=== 行情 ===")
close = daily["close"]
print("  最新收盘", round(float(close.iloc[-1]), 2), "| 天数", len(daily),
      "| 区间", daily['date'].iloc[0].date(), "→", daily['date'].iloc[-1].date())
shares = float(daily["outstanding_share"].iloc[-1])
print("  总市值(亿)=", round(float(close.iloc[-1]) * shares / 1e8, 1))
print("  近60日均成交额(亿)=", round(float(daily["amount"].tail(60).mean()) / 1e8, 2))
print("  近3年最大回撤=", round(m.max_drawdown(close), 4))
print("  年化波动=", round(m.volatility(close), 4), " 夏普=", round(m.sharpe(close), 3))
print("  ATR14=", round(m.atr(daily, 14), 2))

bench = ds.fetch_index_daily("000300")
bo = bench.set_index("date")["close"]
co = daily.set_index("date")["close"]
print("  250日超额(个股-沪深300)=", round(m.rel_strength(co, bo, 250), 4))

print("\n=== 溯源 ===")
print(ds.dump_provenance("provenance_smoke"))
print("OK")
