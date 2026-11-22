# -*- coding: utf-8 -*-
"""东方财富系接口（16 个）。

约束
----
- 只用 :mod:`stock.http`（经 ``.common`` 转出）取数，不直接依赖 HTTP 库；
- 返回的 DataFrame **列名 / 列顺序 / 单位 / 排序** 是下游契约，**不可随意改**；
- 不做缓存（缓存由 ``stock.cache`` / ``stock.skilldata.datasource`` 负责）。

数据源
------
- 数据中心：``datacenter-web.eastmoney.com/api/data/v1/get``（宏观、估值）
- 基金列表/净值：``fund.eastmoney.com``、``fundf10.eastmoney.com``、``api.fund.eastmoney.com``
- 行情：``push2delay.eastmoney.com`` / ``push2his.eastmoney.com``（部分内网被重置，
  这两个接口已带兜底：ETF 快照→新浪列表，ETF 历史→腾讯 K 线→新浪日线，失败域名冷却 10 分钟）

需要解析 JS 对象字面量的接口，这里用"括号配对 + json.loads"，不引入 JS 运行时。
"""
from __future__ import annotations

import json
import math
import re
import time
from io import StringIO

import pandas as pd

from .common import (
    FUND_HEADERS,
    em_datacenter,
    em_dc_rows,
    get_json,
    get_text,
    ts,
)

__all__ = [
    "macro_china_cpi",
    "macro_china_ppi",
    "macro_china_pmi",
    "macro_china_lpr",
    "macro_china_money_supply",
    "stock_value_em",
    "fund_money_fund_daily_em",
    "fund_open_fund_daily_em",
    "fund_purchase_em",
    "fund_open_fund_info_em",
    "fund_money_rank_em",
    "fund_fee_em",
    "fund_money_fund_info_em",
    "fund_etf_spot_em",
    "fund_etf_hist_em",
    "stock_fin_balance_em",
    "stock_fin_income_em",
    "stock_fin_cashflow_em",
    "stock_forecast_em",
]

#: 东财行情：实时/延时列表
EM_QUOTE_HOSTS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
#: 东财行情：K 线（push2his 在部分网络被重置，故留 push2 作为等价回退）
EM_KLINE_HOSTS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2.eastmoney.com/api/qt/stock/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
)

#: 基金列表（开放式基金净值 / 申购状态）共用入口
FUND_JJJZ_DATA = "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx"
#: 基金档案（费率）
FUND_FEE_PAGE = "https://fundf10.eastmoney.com/jjfl_{symbol}.html"
#: 基金分红送配
FUND_FHSP_PAGE = "https://fundf10.eastmoney.com/fhsp_{symbol}.html"
#: 基金净值走势 JS（pingzhongdata）
FUND_PZ_DATA = "https://fund.eastmoney.com/pingzhongdata/{symbol}.js"


# ---------------------------------------------------------------- 小工具
def _num(df: pd.DataFrame, cols) -> pd.DataFrame:
    """把若干列就地转成 numeric（errors="coerce"）。"""
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _balanced_slice(text: str, start: int) -> str:
    """从 ``text[start]``（必须是 ``[`` 或 ``{``）取出括号配对的整段字面量。"""
    depth = 0
    in_str = False
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("JS 字面量括号不配对：%r" % (text[max(0, start - 40): start + 80],))


def _js_literal(text: str, marker: str) -> str:
    """找到 ``marker``（如 ``var Data_x =``）后的第一个 ``[``/``{``，返回配对字面量。"""
    i = text.find(marker)
    if i < 0:
        raise ValueError("JS 里找不到 %s" % (marker,))
    j = i + len(marker)
    while j < len(text) and text[j] not in "[{":
        if text[j] == ";":
            raise ValueError("JS 里 %s 没有字面量" % (marker,))
        j += 1
    if j >= len(text):
        raise ValueError("JS 里 %s 没有字面量" % (marker,))
    return _balanced_slice(text, j)


def _js_var(text: str, name: str):
    """解析 pingzhongdata 里的 ``var <name> = <json 数组/对象>;``。"""
    return json.loads(_js_literal(text, "var %s" % name))


def _js_array_field(text: str, key: str):
    """解析东财 JS 对象字面量里的裸键数组，如 ``datas:[...]`` / ``showday:[...]``。"""
    m = re.search(r"\b%s\s*:" % re.escape(key), text)
    if not m:
        raise ValueError("JS 里找不到字段 %s" % (key,))
    j = m.end()
    while j < len(text) and text[j] not in "[{":
        j += 1
    return json.loads(_balanced_slice(text, j))


# ---------------------------------------------------------------- 宏观（数据中心）
def _dc_report(report_name: str, columns: str, page_size: int = 2000) -> list[dict]:
    """宏观类统一分页参数。"""
    payload = em_datacenter(
        columns=columns,
        pageNumber=1,
        pageSize=page_size,
        sortColumns="REPORT_DATE",
        sortTypes=-1,
        source="WEB",
        client="WEB",
        reportName=report_name,
        p=1,
        pageNo=1,
        pageNum=1,
    )
    return em_dc_rows(payload)


def macro_china_cpi() -> pd.DataFrame:
    """东方财富-中国居民消费价格指数（月度，最新在前）。"""
    rows = _dc_report(
        "RPT_ECONOMY_CPI",
        "REPORT_DATE,TIME,NATIONAL_SAME,NATIONAL_BASE,NATIONAL_SEQUENTIAL,"
        "NATIONAL_ACCUMULATE,CITY_SAME,CITY_BASE,CITY_SEQUENTIAL,CITY_ACCUMULATE,"
        "RURAL_SAME,RURAL_BASE,RURAL_SEQUENTIAL,RURAL_ACCUMULATE",
    )
    temp_df = pd.DataFrame(rows)
    temp_df.columns = [
        "-",
        "月份",
        "全国-同比增长",
        "全国-当月",
        "全国-环比增长",
        "全国-累计",
        "城市-同比增长",
        "城市-当月",
        "城市-环比增长",
        "城市-累计",
        "农村-同比增长",
        "农村-当月",
        "农村-环比增长",
        "农村-累计",
    ]
    temp_df = temp_df[
        [
            "月份",
            "全国-当月",
            "全国-同比增长",
            "全国-环比增长",
            "全国-累计",
            "城市-当月",
            "城市-同比增长",
            "城市-环比增长",
            "城市-累计",
            "农村-当月",
            "农村-同比增长",
            "农村-环比增长",
            "农村-累计",
        ]
    ]
    return _num(temp_df, list(temp_df.columns[1:]))


def macro_china_ppi() -> pd.DataFrame:
    """东方财富-中国工业品出厂价格指数（月度）。"""
    rows = _dc_report(
        "RPT_ECONOMY_PPI", "REPORT_DATE,TIME,BASE,BASE_SAME,BASE_ACCUMULATE"
    )
    temp_df = pd.DataFrame(rows)
    temp_df.columns = ["-", "月份", "当月", "当月同比增长", "累计"]
    temp_df = temp_df[["月份", "当月", "当月同比增长", "累计"]]
    return _num(temp_df, ["当月", "当月同比增长", "累计"])


def macro_china_pmi() -> pd.DataFrame:
    """东方财富-中国采购经理人指数（月度）。"""
    rows = _dc_report(
        "RPT_ECONOMY_PMI", "REPORT_DATE,TIME,MAKE_INDEX,MAKE_SAME,NMAKE_INDEX,NMAKE_SAME"
    )
    temp_df = pd.DataFrame(rows)
    temp_df.columns = [
        "-",
        "月份",
        "制造业-指数",
        "制造业-同比增长",
        "非制造业-指数",
        "非制造业-同比增长",
    ]
    temp_df = temp_df[
        ["月份", "制造业-指数", "制造业-同比增长", "非制造业-指数", "非制造业-同比增长"]
    ]
    return _num(temp_df, list(temp_df.columns[1:]))


def macro_china_money_supply() -> pd.DataFrame:
    """东方财富-货币供应量（月度，M2/M1/M0）。"""
    rows = _dc_report(
        "RPT_ECONOMY_CURRENCY_SUPPLY",
        "REPORT_DATE,TIME,BASIC_CURRENCY,BASIC_CURRENCY_SAME,BASIC_CURRENCY_SEQUENTIAL,"
        "CURRENCY,CURRENCY_SAME,CURRENCY_SEQUENTIAL,FREE_CASH,FREE_CASH_SAME,"
        "FREE_CASH_SEQUENTIAL",
    )
    temp_df = pd.DataFrame(rows)
    temp_df.columns = [
        "-",
        "月份",
        "货币和准货币(M2)-数量(亿元)",
        "货币和准货币(M2)-同比增长",
        "货币和准货币(M2)-环比增长",
        "货币(M1)-数量(亿元)",
        "货币(M1)-同比增长",
        "货币(M1)-环比增长",
        "流通中的现金(M0)-数量(亿元)",
        "流通中的现金(M0)-同比增长",
        "流通中的现金(M0)-环比增长",
    ]
    temp_df = temp_df[
        [
            "月份",
            "货币和准货币(M2)-数量(亿元)",
            "货币和准货币(M2)-同比增长",
            "货币和准货币(M2)-环比增长",
            "货币(M1)-数量(亿元)",
            "货币(M1)-同比增长",
            "货币(M1)-环比增长",
            "流通中的现金(M0)-数量(亿元)",
            "流通中的现金(M0)-同比增长",
            "流通中的现金(M0)-环比增长",
        ]
    ]
    return _num(temp_df, list(temp_df.columns[1:]))


def macro_china_lpr() -> pd.DataFrame:
    """东方财富-贷款市场报价利率（LPR，日频，升序）。"""
    base_params = {
        "reportName": "RPTA_WEB_RATE",
        "columns": "ALL",
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "token": "894050c76af8597a853f5b408b759f5d",
        "pageNumber": "1",
        "pageSize": "500",
        "p": "1",
        "pageNo": "1",
        "pageNum": "1",
    }
    payload = em_datacenter(**base_params)
    result = payload.get("result") or {}
    pages = int(result.get("pages") or 1)
    frames = [pd.DataFrame(result.get("data") or [])]
    for page in range(2, pages + 1):
        p = dict(base_params)
        p["pageNumber"] = page
        frames.append(pd.DataFrame(em_dc_rows(em_datacenter(**p))))
    big_df = pd.concat(frames, ignore_index=True)

    big_df["TRADE_DATE"] = pd.to_datetime(big_df["TRADE_DATE"], errors="coerce").dt.date
    big_df = _num(big_df, ["LPR1Y", "LPR5Y", "RATE_1", "RATE_2"])
    big_df.sort_values(by=["TRADE_DATE"], inplace=True)
    big_df.reset_index(inplace=True, drop=True)
    return big_df


# ---------------------------------------------------------------- 个股估值（数据中心）
def stock_value_em(symbol: str = "300766") -> pd.DataFrame:
    """东方财富-数据中心-估值分析（单只股票全部历史，升序）。"""
    payload = em_datacenter(
        sortColumns="TRADE_DATE",
        sortTypes="-1",
        pageSize=5000,
        pageNumber=1,
        reportName="RPT_VALUEANALYSIS_DET",
        columns="ALL",
        quoteColumns="",
        source="WEB",
        client="WEB",
        filter=f'(SECURITY_CODE="{symbol}")',
    )
    temp_df = pd.DataFrame(em_dc_rows(payload))
    temp_df.rename(
        columns={
            "TRADE_DATE": "数据日期",
            "CLOSE_PRICE": "当日收盘价",
            "CHANGE_RATE": "当日涨跌幅",
            "TOTAL_MARKET_CAP": "总市值",
            "NOTLIMITED_MARKETCAP_A": "流通市值",
            "TOTAL_SHARES": "总股本",
            "FREE_SHARES_A": "流通股本",
            "PE_TTM": "PE(TTM)",
            "PE_LAR": "PE(静)",
            "PB_MRQ": "市净率",
            "PEG_CAR": "PEG值",
            "PCF_OCF_TTM": "市现率",
            "PS_TTM": "市销率",
        },
        inplace=True,
    )
    temp_df = temp_df[
        [
            "数据日期",
            "当日收盘价",
            "当日涨跌幅",
            "总市值",
            "流通市值",
            "总股本",
            "流通股本",
            "PE(TTM)",
            "PE(静)",
            "市净率",
            "PEG值",
            "市现率",
            "市销率",
        ]
    ]
    temp_df["数据日期"] = pd.to_datetime(temp_df["数据日期"], errors="coerce").dt.date
    _num(temp_df, list(temp_df.columns[1:]))
    temp_df.sort_values(by="数据日期", ignore_index=True, inplace=True)
    return temp_df


# ---------------------------------------------------------------- 基金列表 / 净值
def fund_purchase_em() -> pd.DataFrame:
    """天天基金-基金申购状态（全市场，按代码升序）。"""
    payload_text = get_text(
        FUND_JJJZ_DATA,
        params={"t": "8", "page": "1,50000", "js": "reData", "sort": "fcode,asc"},
        headers=FUND_HEADERS,
        encoding="utf-8",
    )
    rows = _js_array_field(payload_text, "datas")
    temp_df = pd.DataFrame(rows)
    temp_df.insert(0, "序号", range(1, len(temp_df) + 1))
    temp_df.columns = [
        "序号",
        "基金代码",
        "基金简称",
        "基金类型",
        "最新净值/万份收益",
        "最新净值/万份收益-报告时间",
        "申购状态",
        "赎回状态",
        "下一开放日",
        "购买起点",
        "日累计限定金额",
        "-",
        "-",
        "手续费",
    ]
    temp_df = temp_df[
        [
            "序号",
            "基金代码",
            "基金简称",
            "基金类型",
            "最新净值/万份收益",
            "最新净值/万份收益-报告时间",
            "申购状态",
            "赎回状态",
            "下一开放日",
            "购买起点",
            "日累计限定金额",
            "手续费",
        ]
    ]
    temp_df["下一开放日"] = pd.to_datetime(
        temp_df["下一开放日"], errors="coerce"
    ).dt.date
    temp_df["手续费"] = temp_df["手续费"].str.strip("%")
    return _num(
        temp_df, ["最新净值/万份收益", "购买起点", "日累计限定金额", "手续费"]
    )


def fund_open_fund_daily_em() -> pd.DataFrame:
    """天天基金-开放式基金每日净值（最新两个交易日，按日增长率降序）。"""
    payload_text = get_text(
        FUND_JJJZ_DATA,
        params={
            "t": "1",
            "lx": "1",
            "letter": "",
            "gsid": "",
            "text": "",
            "sort": "zdf,desc",
            "page": "1,50000",
            "dt": "1580914040623",
            "atfc": "",
            "onlySale": "0",
        },
        headers=FUND_HEADERS,
        encoding="utf-8",
    )
    rows = _js_array_field(payload_text, "datas")
    show_day = _js_array_field(payload_text, "showday")
    temp_df = pd.DataFrame(rows)
    temp_df.columns = [
        "基金代码",
        "基金简称",
        "-",
        f"{show_day[0]}-单位净值",
        f"{show_day[0]}-累计净值",
        f"{show_day[1]}-单位净值",
        f"{show_day[1]}-累计净值",
        "日增长值",
        "日增长率",
        "申购状态",
        "赎回状态",
        "-",
        "-",
        "-",
        "-",
        "-",
        "-",
        "手续费",
        "-",
        "-",
        "-",
    ]
    return temp_df[
        [
            "基金代码",
            "基金简称",
            f"{show_day[0]}-单位净值",
            f"{show_day[0]}-累计净值",
            f"{show_day[1]}-单位净值",
            f"{show_day[1]}-累计净值",
            "日增长值",
            "日增长率",
            "申购状态",
            "赎回状态",
            "手续费",
        ]
    ]


def fund_money_fund_daily_em() -> pd.DataFrame:
    """天天基金-货币型基金每日收益一览（页面表格，最新在前）。"""
    text = get_text(
        "https://fund.eastmoney.com/HBJJ_pjsyl.html",
        headers=FUND_HEADERS,
        encoding="gb2312",
    )
    table = pd.read_html(StringIO(text))[1]
    show_day = table.iloc[0, 5:11].tolist()
    temp_df = table.iloc[1:, 2:]
    temp_df_columns = temp_df.iloc[0, :].tolist()[1:]
    temp_df = temp_df.iloc[1:, 1:]
    temp_df.columns = temp_df_columns
    temp_df["基金简称"] = temp_df["基金简称"].str.strip("基金吧档案")
    temp_df.columns = [
        "基金代码",
        "基金简称",
        f"{show_day[0]}-万份收益",
        f"{show_day[1]}-7日年化%",
        f"{show_day[2]}-单位净值",
        f"{show_day[3]}-万份收益",
        f"{show_day[4]}-7日年化%",
        f"{show_day[5]}-单位净值",
        "日涨幅",
        "成立日期",
        "基金经理",
        "手续费",
        "可购全部",
    ]
    return temp_df


def fund_open_fund_info_em(
    symbol: str = "710001", indicator: str = "单位净值走势", period: str = "成立来"
) -> pd.DataFrame:
    """天天基金-开放式基金历史净值（含累计净值/收益率/排名/分红拆分）。"""
    text = get_text(FUND_PZ_DATA.format(symbol=symbol), headers=FUND_HEADERS)

    # 单位净值走势
    if indicator == "单位净值走势":
        data_json = _js_var(text, "Data_netWorthTrend")
        temp_df = pd.DataFrame(data_json)
        if temp_df.empty:
            return pd.DataFrame()
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["净值日期", "单位净值", "日增长率", "_"]
        temp_df = temp_df[["净值日期", "单位净值", "日增长率"]]
        temp_df["净值日期"] = pd.to_datetime(
            temp_df["净值日期"], errors="coerce"
        ).dt.date
        return _num(temp_df, ["单位净值", "日增长率"])

    # 累计净值走势
    if indicator == "累计净值走势":
        data_json = _js_var(text, "Data_ACWorthTrend")
        temp_df = pd.DataFrame(data_json)
        if temp_df.empty:
            return pd.DataFrame()
        temp_df.columns = ["x", "y"]
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["净值日期", "累计净值"]
        temp_df = temp_df[["净值日期", "累计净值"]]
        temp_df["净值日期"] = pd.to_datetime(
            temp_df["净值日期"], errors="coerce"
        ).dt.date
        return _num(temp_df, ["累计净值"])

    # 每万份收益
    if indicator == "每万份收益":
        data_json = _js_var(text, "Data_millionCopiesIncome")
        temp_df = pd.DataFrame(data_json)
        if temp_df.empty:
            return pd.DataFrame()
        temp_df.columns = ["x", "y"]
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["净值日期", "每万份收益"]
        temp_df = temp_df[["净值日期", "每万份收益"]]
        temp_df["净值日期"] = pd.to_datetime(
            temp_df["净值日期"], errors="coerce"
        ).dt.date
        return _num(temp_df, ["每万份收益"])

    # 7日年化收益率
    if indicator == "7日年化收益率":
        data_json = _js_var(text, "Data_sevenDaysYearIncome")
        temp_df = pd.DataFrame(data_json)
        if temp_df.empty:
            return pd.DataFrame()
        temp_df.columns = ["x", "y"]
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["净值日期", "7日年化收益率"]
        temp_df = temp_df[["净值日期", "7日年化收益率"]]
        temp_df["净值日期"] = pd.to_datetime(
            temp_df["净值日期"], errors="coerce"
        ).dt.date
        return _num(temp_df, ["7日年化收益率"])

    # 累计收益率走势
    if indicator == "累计收益率走势":
        period_map = {
            "1月": "m",
            "3月": "q",
            "6月": "hy",
            "1年": "y",
            "3年": "try",
            "5年": "fiy",
            "今年来": "sy",
            "成立来": "se",
        }
        payload = get_json(
            "https://api.fund.eastmoney.com/pinzhong/LJSYLZS",
            params={
                "fundCode": symbol,
                "indexcode": "000300",
                "type": period_map[period],
            },
            headers={"Referer": "https://fund.eastmoney.com/"},
        )
        temp_df = pd.DataFrame(payload["Data"][0]["data"])
        temp_df.columns = ["日期", "累计收益率"]
        temp_df["日期"] = pd.to_datetime(
            temp_df["日期"], unit="ms", utc=True
        ).dt.tz_convert("Asia/Shanghai")
        temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
        return _num(temp_df, ["累计收益率"])

    # 同类排名走势
    if indicator == "同类排名走势":
        data_json = _js_var(text, "Data_rateInSimilarType")
        temp_df = pd.DataFrame(data_json)
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["报告日期", "同类型排名-每日近三月排名", "总排名-每日近三月排名"]
        temp_df = temp_df[
            ["报告日期", "同类型排名-每日近三月排名", "总排名-每日近三月排名"]
        ]
        temp_df["报告日期"] = pd.to_datetime(
            temp_df["报告日期"], errors="coerce"
        ).dt.date
        return _num(
            temp_df, ["同类型排名-每日近三月排名", "总排名-每日近三月排名"]
        )

    # 同类排名百分比
    if indicator == "同类排名百分比":
        data_json = _js_var(text, "Data_rateInSimilarPersent")
        temp_df = pd.DataFrame(data_json)
        temp_df.columns = ["x", "y"]
        temp_df["x"] = pd.to_datetime(temp_df["x"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        temp_df["x"] = temp_df["x"].dt.date
        temp_df.columns = ["报告日期", "同类型排名-每日近3月收益排名百分比"]
        temp_df = temp_df[["报告日期", "同类型排名-每日近3月收益排名百分比"]]
        temp_df["报告日期"] = pd.to_datetime(
            temp_df["报告日期"], errors="coerce"
        ).dt.date
        return _num(temp_df, ["同类型排名-每日近3月收益排名百分比"])

    # 分红送配详情 / 拆分详情
    if indicator in ("分红送配详情", "拆分详情"):
        text = get_text(FUND_FHSP_PAGE.format(symbol=symbol), headers=FUND_HEADERS)
        tables = pd.read_html(StringIO(text))
        if indicator == "分红送配详情":
            temp_df = tables[1] if len(tables) == 3 else tables[0]
            if temp_df.iloc[0, 1] == "暂无分红信息!":
                return pd.DataFrame()
        else:
            temp_df = tables[2] if len(tables) == 3 else tables[1]
            if temp_df.iloc[0, 1] == "暂无拆分信息!":
                return pd.DataFrame()
        return temp_df

    return pd.DataFrame()


def fund_money_fund_info_em(symbol: str = "000009") -> pd.DataFrame:
    """天天基金-货币型基金历史净值（F10 逐页，全量约 3000+ 天）。"""
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    headers = {"Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html"}
    params = {
        "fundCode": symbol,
        "pageIndex": "1",
        "pageSize": "20",
        "startDate": "",
        "endDate": "",
        "_": ts(),
    }
    payload = get_json(url, params=params, headers=headers)
    total_count = int(payload.get("TotalCount") or 0)
    total_page = math.ceil(total_count / 20)
    frames = [pd.DataFrame((payload.get("Data") or {}).get("LSJZList") or [])]
    for page in range(2, total_page + 1):
        p = dict(params)
        p["pageIndex"] = page
        payload = get_json(url, params=p, headers=headers)
        frames.append(pd.DataFrame((payload.get("Data") or {}).get("LSJZList") or []))
    big_df = pd.concat(frames, ignore_index=True)
    big_df = big_df.rename(
        columns={
            "FSRQ": "净值日期",
            "DWJZ": "每万份收益",
            "LJJZ": "7日年化收益率",
            "SGZT": "申购状态",
            "SHZT": "赎回状态",
        }
    )
    big_df.sort_values(by=["净值日期"], inplace=True, ignore_index=True)
    big_df = big_df[["净值日期", "每万份收益", "7日年化收益率", "申购状态", "赎回状态"]]
    big_df["净值日期"] = pd.to_datetime(big_df["净值日期"], errors="coerce").dt.date
    return _num(big_df, ["每万份收益", "7日年化收益率"])


def fund_money_rank_em() -> pd.DataFrame:
    """天天基金-货币型基金排行（按近1年收益降序，全市场）。"""
    payload = get_json(
        "https://api.fund.eastmoney.com/FundRank/GetHbRankList",
        params={
            "intCompany": "0",
            "MinsgType": "",
            "IsSale": "1",
            "strSortCol": "SYL_1N",
            "orderType": "desc",
            "pageIndex": "1",
            "pageSize": "10000",
        },
        headers={"Referer": "https://fund.eastmoney.com/fundguzhi.html"},
    )
    temp_df = pd.DataFrame(payload["Data"])
    temp_df.insert(0, "序号", range(1, len(temp_df) + 1))
    temp_df.columns = [
        "序号",
        "近1年",
        "近2年",
        "近3年",
        "近5年",
        "_",
        "_",
        "基金代码",
        "基金简称",
        "日期",
        "万份收益",
        "年化收益率7日",
        "_",
        "年化收益率14日",
        "年化收益率28日",
        "近1月",
        "近3月",
        "近6月",
        "今年来",
        "成立来",
        "_",
        "手续费",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    temp_df = temp_df[
        [
            "序号",
            "基金代码",
            "基金简称",
            "日期",
            "万份收益",
            "年化收益率7日",
            "年化收益率14日",
            "年化收益率28日",
            "近1月",
            "近3月",
            "近6月",
            "近1年",
            "近2年",
            "近3年",
            "近5年",
            "今年来",
            "成立来",
            "手续费",
        ]
    ]
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    return _num(
        temp_df,
        [
            "万份收益",
            "年化收益率7日",
            "年化收益率14日",
            "年化收益率28日",
            "近1月",
            "近3月",
            "近6月",
            "近1年",
            "近2年",
            "近3年",
            "近5年",
            "今年来",
            "成立来",
        ],
    )


def fund_fee_em(symbol: str = "015641", indicator: str = "认购费率") -> pd.DataFrame:
    """天天基金-基金档案-购买信息（交易规则/费率表，按 indicator 取对应表）。"""
    from bs4 import BeautifulSoup

    url = FUND_FEE_PAGE.format(symbol=symbol)
    text = get_text(url, headers=FUND_HEADERS)
    soup = BeautifulSoup(text, features="html.parser")
    tables_dict: dict[str, pd.DataFrame] = {}
    for title_elem in soup.find_all(name="h4", class_="t"):
        title_text = re.sub(r"\s+", " ", title_elem.get_text(strip=True)).strip()
        if title_text == "申购与赎回金额":
            tables = title_elem.find_all_next("table")
            df_1 = pd.read_html(StringIO(str(tables[0])))[0]
            df_2 = pd.read_html(StringIO(str(tables[1])))[0]
            tables_dict[title_text] = pd.concat([df_1, df_2], ignore_index=True)
            continue
        next_table = title_elem.find_next("table")
        if next_table:
            try:
                tables_dict[title_text] = pd.read_html(StringIO(str(next_table)))[0]
            except Exception:  # noqa: BLE001 —— 单表解析失败就跳过，不影响整体
                continue

    if indicator in ("交易状态", "申购与赎回金额", "交易确认日", "运作费用"):
        return tables_dict[indicator]
    if indicator == "认购费率（前端）":
        temp_df = tables_dict[indicator]
        temp_df[["原费率", "天天基金优惠费率"]] = temp_df[
            "原费率|天天基金优惠费率"
        ].str.split("|", expand=True)
        del temp_df["原费率|天天基金优惠费率"]
        temp_df.loc[3, "天天基金优惠费率"] = temp_df.loc[3, "原费率"]
        temp_df["原费率"] = temp_df["原费率"].str.strip()
        temp_df["天天基金优惠费率"] = temp_df["天天基金优惠费率"].str.strip()
        return temp_df
    if indicator == "认购费率（后端）":
        return tables_dict[indicator]
    if indicator == "申购费率（前端）":
        temp_df = tables_dict[indicator]
        if "原费率|天天基金优惠费率 银行卡购买|活期宝购买" not in temp_df.columns:
            return temp_df
        splited = temp_df["原费率|天天基金优惠费率 银行卡购买|活期宝购买"].str.split(
            "|", expand=True
        )
        if splited.shape[1] == 1:
            temp_df.rename(
                columns={"原费率|天天基金优惠费率 银行卡购买|活期宝购买": "原费率"},
                inplace=True,
            )
            temp_df["天天基金优惠费率-银行卡购买"] = temp_df["原费率"]
            temp_df["天天基金优惠费率-活期宝购买"] = temp_df["原费率"]
        else:
            temp_df[
                ["原费率", "天天基金优惠费率-银行卡购买", "天天基金优惠费率-活期宝购买"]
            ] = splited
            temp_df["天天基金优惠费率-银行卡购买"] = temp_df[
                "天天基金优惠费率-银行卡购买"
            ].fillna(temp_df["原费率"])
            temp_df["天天基金优惠费率-活期宝购买"] = temp_df[
                "天天基金优惠费率-活期宝购买"
            ].fillna(temp_df["原费率"])
            del temp_df["原费率|天天基金优惠费率 银行卡购买|活期宝购买"]
        temp_df["原费率"] = temp_df["原费率"].str.strip()
        temp_df["天天基金优惠费率-银行卡购买"] = temp_df[
            "天天基金优惠费率-银行卡购买"
        ].str.strip()
        temp_df["天天基金优惠费率-活期宝购买"] = temp_df[
            "天天基金优惠费率-活期宝购买"
        ].str.strip()
        return temp_df
    if indicator in ("赎回费率", "赎回费率（前端）", "赎回费率（后端）"):
        temp_df = tables_dict[indicator]
        if "原费率|天天基金优惠费率" in temp_df.columns:
            temp_df[["原费率", "天天基金优惠费率"]] = temp_df[
                "原费率|天天基金优惠费率"
            ].str.split("|", expand=True)
            del temp_df["原费率|天天基金优惠费率"]
        return temp_df
    return pd.DataFrame([])


# ---------------------------------------------------------------- ETF 行情

#: 东财行情域名的"最近失败时间"。内网里 push2/push2his 会被对端重置，每次都要
#: 等 3 次连接超时才回退，太慢；这里把失败域名冷却 10 分钟，直接跳过。
_HOST_DOWN: dict[str, float] = {}
_HOST_DOWN_TTL = 600.0


def _live_hosts(hosts):
    """过滤掉冷却期内的域名。返回空列表 = 最近全挂，调用方应直接走兜底源
    （别再白等 3×3 次连接超时；10 分钟后自动重试东财）。"""
    now = time.time()
    return [h for h in hosts if now - _HOST_DOWN.get(h, 0.0) >= _HOST_DOWN_TTL]


def _mark_host_down(url: str) -> None:
    _HOST_DOWN[url] = time.time()


def _reset_host_down() -> None:
    """清空冷却表（自检/排查用）。"""
    _HOST_DOWN.clear()


#: 东财 ETF 快照的完整列序（兜底源按此 reindex，保证业务层看到的列不变）
_ETF_SPOT_COLUMNS = [
    "代码", "名称", "最新价", "IOPV实时估值", "基金折价率", "涨跌额", "涨跌幅",
    "成交量", "成交额", "开盘价", "最高价", "最低价", "昨收", "振幅", "换手率",
    "量比", "委比", "外盘", "内盘", "主力净流入-净额", "主力净流入-净占比",
    "超大单净流入-净额", "超大单净流入-净占比", "大单净流入-净额", "大单净流入-净占比",
    "中单净流入-净额", "中单净流入-净占比", "小单净流入-净额", "小单净流入-净占比",
    "现手", "买一", "卖一", "最新份额", "流通市值", "总市值", "数据日期", "更新时间",
]

#: 东财 ETF 历史行情的列序
_ETF_HIST_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
                     "振幅", "涨跌幅", "涨跌额", "换手率"]


def _map_spot_sina(raw: pd.DataFrame) -> pd.DataFrame:
    """把新浪 ETF 列表映射成东财快照的列结构（纯函数，离线可测）。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_ETF_SPOT_COLUMNS)
    out = pd.DataFrame(index=raw.index)
    out["代码"] = raw["代码"].astype(str).str.extract(r"(\d{6})")[0]
    out["名称"] = raw["名称"]
    for src, dst in [("最新价", "最新价"), ("涨跌额", "涨跌额"), ("涨跌幅", "涨跌幅"),
                     ("成交额", "成交额"), ("今开", "开盘价"), ("最高", "最高价"),
                     ("最低", "最低价"), ("昨收", "昨收"), ("买入", "买一"), ("卖出", "卖一")]:
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    out["成交量"] = pd.to_numeric(raw["成交量"], errors="coerce") / 100.0  # 份 -> 手
    prev = out["昨收"].where(out["昨收"] > 0)
    out["振幅"] = ((out["最高价"] - out["最低价"]) / prev * 100).round(2)
    return out.reindex(columns=_ETF_SPOT_COLUMNS)


def _etf_spot_sina_fallback() -> pd.DataFrame:
    """新浪 ETF 列表兜底（实时报价）。成交量：新浪是"份"，东财是"手"，÷100。"""
    from . import sina as _sina

    try:
        raw = _sina.fund_etf_category_sina(symbol="ETF基金")
    except Exception:  # noqa: BLE001
        raise
    return _map_spot_sina(raw)


def _etf_hist_fallback(symbol: str, period: str, start_date: str,
                       end_date: str, adjust: str) -> pd.DataFrame:
    """腾讯 K 线优先、新浪日线兜底；统一转成东财的 11 列。"""
    from ..sources import tencent
    from . import sina as _sina

    period_map = {"daily": "day", "weekly": "week", "monthly": "month"}
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if len(start_date) == 8 else ""
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}" if len(end_date) == 8 else ""

    df = pd.DataFrame()
    try:
        tx = tencent.kline(symbol, start=start, end=end,
                           adjust=adjust or "", kind=None,
                           period=period_map.get(period, "day"))
        if not tx.empty:
            df = tx.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                    "high": "最高", "low": "最低", "volume": "成交量"})
            df["成交额"] = float("nan")
    except Exception:  # noqa: BLE001 —— 腾讯失败再退新浪
        df = pd.DataFrame()

    if df.empty:
        sx = _sina.fund_etf_hist_sina(symbol=tencent.codes.to_sina(symbol, None))
        if sx.empty:
            return pd.DataFrame(columns=_ETF_HIST_COLUMNS)
        df = sx.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                "high": "最高", "low": "最低", "volume": "成交量",
                                "amount": "成交额"})
        df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce") / 100.0  # 股→手
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        if period in ("weekly", "monthly"):
            df = df.dropna(subset=["日期"]).set_index(pd.to_datetime(df["日期"]))
            rule = "W-FRI" if period == "weekly" else "ME"
            agg = {"开盘": "first", "收盘": "last", "最高": "max", "最低": "min",
                   "成交量": "sum", "成交额": "sum"}
            df = df.resample(rule).agg(agg).dropna(subset=["收盘"])
            df.index = df.index.strftime("%Y-%m-%d")
            df = df.reset_index().rename(columns={"index": "日期"})

    return _map_hist_ohlc(df, period, derived=True)


def _map_hist_ohlc(df: pd.DataFrame, period: str, derived: bool = True) -> pd.DataFrame:
    """把 date/open/close/high/low/volume[/amount] 或已重命名的中文列，统一成东财 11 列。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=_ETF_HIST_COLUMNS)
    out = df.copy()
    if "日期" not in out.columns and "date" in out.columns:
        out = out.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                  "high": "最高", "low": "最低", "volume": "成交量",
                                  "amount": "成交额"})
    if "日期" in out.columns:
        out["日期"] = pd.to_datetime(out["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.dropna(subset=["日期"]).sort_values("日期")
    for col in ("开盘", "收盘", "最高", "最低", "成交量", "成交额"):
        if col not in out.columns:
            out[col] = float("nan")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if period in ("weekly", "monthly") and not derived:
        out = out.set_index(pd.to_datetime(out["日期"]))
        rule = "W-FRI" if period == "weekly" else "ME"
        agg = {"开盘": "first", "收盘": "last", "最高": "max", "最低": "min",
               "成交量": "sum", "成交额": "sum"}
        out = out.resample(rule).agg(agg).dropna(subset=["收盘"])
        out.index = out.index.strftime("%Y-%m-%d")
        out = out.reset_index().rename(columns={"index": "日期"})
    prev = out["收盘"].shift(1)
    out["涨跌额"] = (out["收盘"] - prev).round(3)
    out["涨跌幅"] = ((out["收盘"] - prev) / prev * 100).round(2)
    out["振幅"] = ((out["最高"] - out["最低"]) / prev * 100).round(2)
    out["换手率"] = float("nan")
    return out.reindex(columns=_ETF_HIST_COLUMNS)


def _fetch_paginated(
    hosts, base_params: dict, headers: dict | None = None
) -> pd.DataFrame:
    """东财 clist 分页拉全量（首屏决定总页数，逐页 concat）。"""
    last_exc: Exception | None = None
    params = dict(base_params)
    hosts_live = _live_hosts(hosts)
    if not hosts_live:
        raise RuntimeError("东财行情域名最近均失败（冷却中），直接走兜底源")
    for url in hosts_live:
        try:
            payload = get_json(url, params=params, headers=headers)
            break
        except Exception as exc:  # noqa: BLE001 —— 换等价域名重试
            last_exc = exc
            _mark_host_down(url)
    else:
        raise last_exc  # type: ignore[misc]

    data = payload.get("data") or {}
    diff = data.get("diff") or []
    total = data.get("total") or 0
    frames = [pd.DataFrame(diff)]
    per_page_num = len(diff)
    if per_page_num:
        total_page = math.ceil(total / per_page_num)
        for page in range(2, total_page + 1):
            params["pn"] = page
            payload = get_json(url, params=params, headers=headers)
            frames.append(pd.DataFrame((payload.get("data") or {}).get("diff") or []))
    temp_df = pd.concat(frames, ignore_index=True)
    temp_df["f3"] = pd.to_numeric(temp_df["f3"], errors="coerce")
    temp_df.sort_values(by=["f3"], ascending=False, inplace=True, ignore_index=True)
    temp_df.reset_index(inplace=True)
    temp_df["index"] = temp_df["index"].astype(int) + 1
    return temp_df


def fund_etf_spot_em() -> pd.DataFrame:
    """ETF 实时行情（东财优先，失败回退新浪 ETF 列表）。

    东财 ``push2delay`` / ``push2`` 在内网实测被对端重置（RemoteDisconnected），
    此时自动回退新浪（``fund_etf_category_sina``），列名/列序与东财版保持一致，
    新浪没有的字段（资金流向/份额/IOPV 等）留 NaN。
    """
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "wbp2u": "|0|0|0|web",
        "fid": "f12",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
        "fields": (
            "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,"
            "f12,f13,f14,f15,f16,f17,f18,f20,f21,"
            "f23,f24,f25,f22,f11,f30,f31,f32,f33,"
            "f34,f35,f38,f62,f63,f64,f65,f66,f69,"
            "f72,f75,f78,f81,f84,f87,f115,f124,f128,"
            "f136,f152,f184,f297,f402,f441"
        ),
    }
    try:
        temp_df = _fetch_paginated(EM_QUOTE_HOSTS, params)
    except Exception:  # noqa: BLE001 —— 东财 push2 被重置时回退新浪
        return _etf_spot_sina_fallback()
    temp_df.rename(
        columns={
            "f12": "代码",
            "f14": "名称",
            "f2": "最新价",
            "f4": "涨跌额",
            "f3": "涨跌幅",
            "f5": "成交量",
            "f6": "成交额",
            "f7": "振幅",
            "f17": "开盘价",
            "f15": "最高价",
            "f16": "最低价",
            "f18": "昨收",
            "f8": "换手率",
            "f10": "量比",
            "f30": "现手",
            "f31": "买一",
            "f32": "卖一",
            "f33": "委比",
            "f34": "外盘",
            "f35": "内盘",
            "f62": "主力净流入-净额",
            "f184": "主力净流入-净占比",
            "f66": "超大单净流入-净额",
            "f69": "超大单净流入-净占比",
            "f72": "大单净流入-净额",
            "f75": "大单净流入-净占比",
            "f78": "中单净流入-净额",
            "f81": "中单净流入-净占比",
            "f84": "小单净流入-净额",
            "f87": "小单净流入-净占比",
            "f38": "最新份额",
            "f21": "流通市值",
            "f20": "总市值",
            "f402": "基金折价率",
            "f441": "IOPV实时估值",
            "f297": "数据日期",
            "f124": "更新时间",
        },
        inplace=True,
    )
    temp_df = temp_df[
        [
            "代码",
            "名称",
            "最新价",
            "IOPV实时估值",
            "基金折价率",
            "涨跌额",
            "涨跌幅",
            "成交量",
            "成交额",
            "开盘价",
            "最高价",
            "最低价",
            "昨收",
            "振幅",
            "换手率",
            "量比",
            "委比",
            "外盘",
            "内盘",
            "主力净流入-净额",
            "主力净流入-净占比",
            "超大单净流入-净额",
            "超大单净流入-净占比",
            "大单净流入-净额",
            "大单净流入-净占比",
            "中单净流入-净额",
            "中单净流入-净占比",
            "小单净流入-净额",
            "小单净流入-净占比",
            "现手",
            "买一",
            "卖一",
            "最新份额",
            "流通市值",
            "总市值",
            "数据日期",
            "更新时间",
        ]
    ]
    _num(
        temp_df,
        [
            "最新价",
            "涨跌额",
            "涨跌幅",
            "成交量",
            "成交额",
            "开盘价",
            "最高价",
            "最低价",
            "昨收",
            "换手率",
            "量比",
            "委比",
            "外盘",
            "内盘",
            "流通市值",
            "总市值",
            "振幅",
            "现手",
            "买一",
            "卖一",
            "最新份额",
            "IOPV实时估值",
            "基金折价率",
            "主力净流入-净额",
            "主力净流入-净占比",
            "超大单净流入-净额",
            "超大单净流入-净占比",
            "大单净流入-净额",
            "大单净流入-净占比",
            "中单净流入-净额",
            "中单净流入-净占比",
            "小单净流入-净额",
            "小单净流入-净占比",
        ],
    )
    temp_df["数据日期"] = pd.to_datetime(
        temp_df["数据日期"], format="%Y%m%d", errors="coerce"
    )
    temp_df["更新时间"] = (
        pd.to_datetime(temp_df["更新时间"], unit="s", errors="coerce")
        .dt.tz_localize("UTC")
        .dt.tz_convert("Asia/Shanghai")
    )
    return temp_df


def get_market_id(symbol: str) -> int:
    """东财 secid 市场号：5/6 开头上证 1；深市/北交所 0（含 4/8/920 开头）；异常兜底 1。"""
    if symbol.startswith(("4", "8", "920")):   # 北交所：4xx/8xx 老段 + 920 新段，secid 前缀都是 0
        return 0
    if symbol.startswith(("0", "1", "3", "2", "5", "6")):
        if symbol.startswith(("5", "6")):
            return 1
        return 0
    return 1


def fund_etf_hist_em(
    symbol: str = "159707",
    period: str = "daily",
    start_date: str = "19700101",
    end_date: str = "20500101",
    adjust: str = "",
) -> pd.DataFrame:
    """ETF 历史行情（日/周/月线；东财优先，失败回退腾讯 K 线、再回退新浪）。

    主源 ``push2his`` 在内网实测被重置，代码按 push2his → push2 → push2delay
    顺序换等价域名；全失败时回退腾讯 ``kline``（支持复权），再回退新浪日线。
    列名/列序与东财版一致；回退源拿不到的 「成交额/换手率」留 NaN，或按前收自算振幅/涨跌幅。
    """
    adjust_dict = {"qfq": "1", "hfq": "2", "": "0"}
    period_dict = {"daily": "101", "weekly": "102", "monthly": "103"}
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": period_dict[period],
        "fqt": adjust_dict[adjust],
        "beg": start_date,
        "end": end_date,
        "secid": f"{get_market_id(symbol)}.{symbol}",
    }
    headers = {"Referer": f"https://quote.eastmoney.com/{symbol}.html"}
    last_exc: Exception | None = None
    payload = None
    hosts_live = _live_hosts(EM_KLINE_HOSTS)
    if not hosts_live:
        return _etf_hist_fallback(symbol, period, start_date, end_date, adjust)
    for url in hosts_live:
        try:
            payload = get_json(url, params=params, headers=headers)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _mark_host_down(url)
    if payload is None:
        return _etf_hist_fallback(symbol, period, start_date, end_date, adjust)
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        return pd.DataFrame()
    temp_df = pd.DataFrame([item.split(",") for item in klines])
    temp_df.columns = [
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
        "振幅",
        "涨跌幅",
        "涨跌额",
        "换手率",
    ]
    temp_df.index = pd.to_datetime(temp_df["日期"], errors="coerce")
    temp_df.reset_index(inplace=True, drop=True)
    return _num(
        temp_df,
        ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"],
    )

# ---------------------------------------------------------------- 分红送配（数据中心）
def stock_dividend_em(symbol: str = "000001", limit: int = 50) -> pd.DataFrame:
    """东方财富-数据中心-分红送配（单只股票，按除权除息日倒序）。

    ``每10股派息`` 是**含税**口径（``PRETAX_BONUS_RMB``）；``每10股送转`` = 送股
    （``BONUS_RATIO``）+ 转增（``IT_RATIO``）。只保留有除权除息日的方案，
    否则是「已预案未实施」的行，落地后除权日才有。
    """
    code = str(symbol).strip().zfill(6)
    payload = em_datacenter(
        sortColumns="EX_DIVIDEND_DATE",
        sortTypes="-1",
        pageSize=max(1, int(limit)),
        pageNumber=1,
        reportName="RPT_SHAREBONUS_DET",
        columns="ALL",
        quoteColumns="",
        source="WEB",
        client="WEB",
        filter=f'(SECURITY_CODE="{code}")',
    )
    cols = ["代码", "名称", "公告日期", "股权登记日", "除权除息日", "每10股派息", "每10股送转", "方案", "进度"]
    rows = em_dc_rows(payload)
    if not rows:
        return pd.DataFrame(columns=cols)

    def _day(v) -> str:
        s = str(v or "")[:10]
        return s if s[:4].isdigit() else ""

    def _f(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    out = []
    for r in rows:
        song = _f(r.get("BONUS_RATIO")) + _f(r.get("IT_RATIO"))
        out.append({
            "代码": str(r.get("SECURITY_CODE") or code).zfill(6),
            "名称": str(r.get("SECURITY_NAME_ABBR") or ""),
            "公告日期": _day(r.get("NOTICE_DATE")),
            "股权登记日": _day(r.get("EQUITY_RECORD_DATE")),
            "除权除息日": _day(r.get("EX_DIVIDEND_DATE")),
            "每10股派息": _f(r.get("PRETAX_BONUS_RMB")),
            "每10股送转": round(song, 4),
            "方案": str(r.get("IMPL_PLAN_PROFILE") or ""),
            "进度": str(r.get("ASSIGN_PROGRESS") or ""),
        })
    df = pd.DataFrame(out, columns=cols)
    df = df[df["除权除息日"] != ""].reset_index(drop=True)
    return df



# ---------------------------------------------------------------- 财务报表 / 业绩预告
def _secucode(symbol) -> str:
    """6 位代码 → 东财 SECUCODE（000999.SZ / 600000.SH / 830799.BJ）。"""
    code = str(symbol).strip().lower()
    for pre in ("sh", "sz", "bj"):
        if code.startswith(pre):
            code = code[len(pre):]
    code = code.zfill(6)
    if code[0] in ("6", "9"):
        return code + ".SH"
    if code[0] in ("4", "8"):
        return code + ".BJ"
    return code + ".SZ"


def _fin_dates(df):
    """把 REPORT_DATE / NOTICE_DATE 规范成 YYYY-MM-DD 字符串。"""
    for col in ("REPORT_DATE", "NOTICE_DATE"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def _fin_statement(report: str, symbol="000001", limit: int = 8) -> pd.DataFrame:
    sec = _secucode(symbol)
    payload = em_datacenter(
        reportName=report,
        columns="ALL",
        filter=f'(SECUCODE="{sec}")',
        pageSize=max(1, int(limit)),
        pageNumber=1,
        source="WEB",
        client="WEB",
        sortColumns="REPORT_DATE",
        sortTypes="-1",
    )
    rows = em_dc_rows(payload)
    if not rows:
        raise ValueError(f"东财财务报表为空：{report} {sec}")
    return _fin_dates(pd.DataFrame(rows))


def stock_fin_balance_em(symbol="000001", limit: int = 8) -> pd.DataFrame:
    """资产负债表（东财 F10，按报告期倒序）。含 TOTAL_ASSETS / TOTAL_LIAB /
    MONETARYFUNDS / ACCOUNTS_RECE / INVENTORY / TOTAL_EQUITY / NOTICE_DATE。"""
    return _fin_statement("RPT_F10_FINANCE_GBALANCE", symbol, limit)


def stock_fin_income_em(symbol="000001", limit: int = 8) -> pd.DataFrame:
    """利润表（东财 F10）。含 TOTAL_OPERATE_INCOME / OPERATE_COST /
    PARENT_NETPROFIT / NOTICE_DATE。"""
    return _fin_statement("RPT_F10_FINANCE_GINCOME", symbol, limit)


def stock_fin_cashflow_em(symbol="000001", limit: int = 8) -> pd.DataFrame:
    """现金流量表（东财 F10）。含 NETCASH_OPERATE / NETPROFIT /
    INVENTORY_REDUCE / NOTICE_DATE。"""
    return _fin_statement("RPT_F10_FINANCE_GCASHFLOW", symbol, limit)


FORECAST_COLS = ["代码", "名称", "报告期", "公告日", "指标", "预告类型",
                 "净利润下限", "净利润上限", "同比下限", "同比上限", "是否最新", "预告摘要"]


def stock_forecast_em(symbol="", limit: int = 40, start_date: str = "") -> pd.DataFrame:
    """业绩预告（东财 RPT_PUBLIC_OP_NEWPREDICT）。symbol 空=全市场最新。"""
    flt = "(IS_LATEST=\"T\")" if not symbol else f'(SECURITY_CODE="{str(symbol)[-6:]}")'
    sd = str(start_date or "")[:10]
    if sd:
        flt += f"(NOTICE_DATE>='{sd}')"
    payload = em_datacenter(
        reportName="RPT_PUBLIC_OP_NEWPREDICT",
        columns="ALL",
        filter=flt,
        pageSize=max(1, int(limit)),
        pageNumber=1,
        source="WEB",
        client="WEB",
        sortColumns="NOTICE_DATE,REPORT_DATE",
        sortTypes="-1,-1",
    )
    rows = em_dc_rows(payload)
    if not rows:
        return pd.DataFrame(columns=FORECAST_COLS)

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = []
    for r in rows:
        out.append({
            "代码": str(r.get("SECURITY_CODE") or "").zfill(6),
            "名称": str(r.get("SECURITY_NAME_ABBR") or ""),
            "报告期": str(r.get("REPORT_DATE") or "")[:10],
            "公告日": str(r.get("NOTICE_DATE") or "")[:10],
            "指标": str(r.get("PREDICT_FINANCE") or ""),
            "预告类型": str(r.get("PREDICT_TYPE") or ""),
            "净利润下限": _f(r.get("PREDICT_AMT_LOWER")),
            "净利润上限": _f(r.get("PREDICT_AMT_UPPER")),
            "同比下限": _f(r.get("ADD_AMP_LOWER")),
            "同比上限": _f(r.get("ADD_AMP_UPPER")),
            "是否最新": str(r.get("IS_LATEST") or ""),
            "预告摘要": str(r.get("PREDICT_CONTENT") or ""),
        })
    return pd.DataFrame(out, columns=FORECAST_COLS)
