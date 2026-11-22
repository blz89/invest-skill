# -*- coding: utf-8 -*-
"""数据源。三个源各管一段，互不依赖：

- `facade`      —— 业务稳定壳（A 股/ETF/指数日线、货基、基金净值、逆回购）
- `eastmoney`   —— 东财直连（基金 F10 历史净值、批量行情快照；快、无需 key）
- `fuyao`       —— 同花顺 Financial-API（财务/估值/公告新闻/涨停池/指数成分；需 key）
- `tencent`     —— 腾讯行情（日线主通道：支持复权、稳定、无 key）
- `sina`        —— 新浪行情（日线备用通道、实时快照）

用法：显式导入子模块（如 ``from stock.sources import facade``）。
"""
from __future__ import annotations
