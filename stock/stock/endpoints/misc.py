# -*- coding: utf-8 -*-
"""杂项数据接口（14 个）。

覆盖其余来源的一组接口：中证指数、沪深交易所市场概况、金十 Shibor、
商务部社融、财联社电报、乐咕乐股活跃度、百度估值、同花顺 ETF、蛋卷基金详情、
东财基金规模、巨潮资讯全市场公告。

实现约束
--------
- 只用 ``stock.http``（经 ``.common`` 转出）的 ``get_json`` / ``get_text`` 发请求，
  统一 UA / 限流 / 退避重试；xls/xlsx 这类二进制用同模块的 ``request`` 取原始字节。
- 返回的 DataFrame 列名、顺序、单位是下游契约，**不可随意改**。
- 不做缓存（缓存由 ``stock.skilldata.datasource`` 负责）。

两个不得不的例外（站点本身的要求，已在函数旁注明）
--------------------------------------------------
- ``macro_china_shrzgm``：商务部接口只接受 POST，且服务器只认 TLS1.2 + SECLEVEL=1；
  用 ``request(transport=...)`` 注入一个 POST / 旧版 TLS 的 transport，限流与重试仍走 stock.http。
- 中证成分股 / 深交所总貌：返回 .xls/.xlsx 字节流，``get_text`` 会把二进制解坏，
  因此用 ``request`` 拿 bytes 再交给 pandas。

已知不稳定接口
--------------
- ``stock_market_activity_legu`` 依赖乐咕乐股（legulegu.com），该站会随机返回
  429 / 504（实测与请求频率无关的 WAF 行为）；函数内已有最多 5 次短退避重试。
  若站点对该 IP 持续封禁，直连失败属正常现象，调用方（旧策略任务）按 fail-soft 处理。
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from io import BytesIO, StringIO

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

from ..http import HttpError, request  # request：二进制 xls/xlsx 与 mofcom 的 POST transport

from .common import get_json, get_text

__all__ = [
    "index_stock_cons_csindex",
    "stock_zh_index_hist_csindex",
    "stock_sse_deal_daily",
    "stock_szse_summary",
    "macro_china_shibor_all",
    "macro_china_shrzgm",
    "stock_info_global_cls",
    "stock_market_activity_legu",
    "stock_zh_valuation_baidu",
    "fund_etf_spot_ths",
    "fund_individual_basic_info_xq",
    "fund_scale_em",
    "stock_notice_report",
    "stock_notice_text",
]

#: 中证指数站点（首页/详情页都要求 Referer 才返回 JSON）
CSINDEX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.csindex.com.cn/zh-CN/indices/index-detail/000300",
}
#: 上交所 / 深交所行情总貌
SSE_HEADERS = {"Referer": "https://www.sse.com.cn/"}
SZSE_HEADERS = {"Referer": "https://www.szse.cn/market/overview/index.html"}
#: 财联社电报
CLS_HEADERS = {"Referer": "https://www.cls.cn/telegraph"}
#: 乐咕乐股（无 Referer 会被防盗链挡）
LEGU_HEADERS = {"Referer": "https://www.legulegu.com/stockdata/market-activity"}
#: 百度股市通
BAIDU_HEADERS = {"Referer": "https://gushitong.baidu.com/stock/ab-002044"}
#: 同花顺基金数据中心
THS_HEADERS = {"Referer": "https://fund.10jqka.com.cn/datacenter/jz/kfs/etf/"}
#: 蛋卷基金（雪球）
XQ_HEADERS = {"Referer": "https://danjuanfunds.com/"}
EM_FUND_HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    # 该接口是移动端 app 接口：桌面 Chrome UA 会被判成爬虫（Success=False
    # 「网络繁忙，请稍后重试！」），移动端 UA 才稳定返回规模字段。
    "User-Agent": "Mozilla/5.0 (Linux; Android 12)",
}


# --------------------------------------------------------------------------- #
# 中证指数
# --------------------------------------------------------------------------- #
def index_stock_cons_csindex(symbol: str = "000300") -> pd.DataFrame:
    """中证指数网站-成份股目录（最新一期）。

    https://www.csindex.com.cn/zh-CN/indices/index-detail/000300
    :param symbol: 指数代码（6 位，如 000300 / 000905）
    :return: 最新指数的成份股
    """
    url = (
        f"https://oss-ch.csindex.com.cn/static/html/csindex/"
        f"public/uploads/file/autofile/cons/{symbol}cons.xls"
    )
    raw = request(url)  # 二进制 .xls，不能用 get_text
    temp_df = pd.read_excel(BytesIO(raw))
    temp_df.columns = [
        "日期",
        "指数代码",
        "指数名称",
        "指数英文名称",
        "成分券代码",
        "成分券名称",
        "成分券英文名称",
        "交易所",
        "交易所英文名称",
    ]
    temp_df["日期"] = pd.to_datetime(
        temp_df["日期"], format="%Y%m%d", errors="coerce"
    ).dt.date
    temp_df["指数代码"] = temp_df["指数代码"].astype(str).str.zfill(6)
    temp_df["成分券代码"] = temp_df["成分券代码"].astype(str).str.zfill(6)
    return temp_df


def stock_zh_index_hist_csindex(
    symbol: str = "000928",
    start_date: str = "20180526",
    end_date: str = "20240604",
) -> pd.DataFrame:
    """中证指数-具体指数-历史行情数据（只有收盘价，正常应优先用新浪/东财）。

    https://www.csindex.com.cn/zh-CN/indices/index-detail/H30374
    :param symbol: 指数代码；e.g., H30374
    :param start_date: 开始日期，YYYYMMDD
    :param end_date: 结束日期，YYYYMMDD
    """
    url = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    params = {
        "indexCode": symbol,
        "startDate": start_date,
        "endDate": end_date,
    }
    data_json = get_json(url, params=params, headers=CSINDEX_HEADERS)
    temp_df = pd.DataFrame(data_json["data"])
    temp_df.columns = [
        "日期",
        "指数代码",
        "指数中文全称",
        "指数中文简称",
        "指数英文全称",
        "指数英文简称",
        "开盘",
        "最高",
        "最低",
        "收盘",
        "涨跌",
        "涨跌幅",
        "成交量",
        "成交金额",
        "样本数量",
        "滚动市盈率",
    ]
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    temp_df["开盘"] = pd.to_numeric(temp_df["开盘"], errors="coerce")
    temp_df["最高"] = pd.to_numeric(temp_df["最高"], errors="coerce")
    temp_df["最低"] = pd.to_numeric(temp_df["最低"], errors="coerce")
    temp_df["收盘"] = pd.to_numeric(temp_df["收盘"], errors="coerce")
    temp_df["涨跌"] = pd.to_numeric(temp_df["涨跌"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交金额"] = pd.to_numeric(temp_df["成交金额"], errors="coerce")
    temp_df["样本数量"] = pd.to_numeric(temp_df["样本数量"], errors="coerce")
    temp_df["滚动市盈率"] = pd.to_numeric(temp_df["滚动市盈率"], errors="coerce")
    return temp_df


# --------------------------------------------------------------------------- #
# 交易所市场概况
# --------------------------------------------------------------------------- #
def stock_szse_summary(date: str = "20240830") -> pd.DataFrame:
    """深证证券交易所-总貌-证券类别统计（接口返回 xlsx）。

    https://www.szse.cn/market/overview/index.html
    :param date: 最近结束交易日，YYYYMMDD
    :return: 证券类别统计
    """
    url = "http://www.szse.cn/api/report/ShowReport"
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1803_sczm",
        "TABKEY": "tab1",
        "txtQueryDate": "-".join([date[:4], date[4:6], date[6:]]),
        "random": "0.39339437497296137",
    }
    raw = request(url, params=params, headers=SZSE_HEADERS)
    temp_df = pd.read_excel(BytesIO(raw), engine="openpyxl")
    temp_df["证券类别"] = temp_df["证券类别"].str.strip()
    temp_df.iloc[:, 2:] = temp_df.iloc[:, 2:].map(lambda x: x.replace(",", ""))
    temp_df.columns = ["证券类别", "数量", "成交金额", "总市值", "流通市值"]
    temp_df["数量"] = pd.to_numeric(temp_df["数量"], errors="coerce")
    temp_df["成交金额"] = pd.to_numeric(temp_df["成交金额"], errors="coerce")
    temp_df["总市值"] = pd.to_numeric(temp_df["总市值"], errors="coerce")
    temp_df["流通市值"] = pd.to_numeric(temp_df["流通市值"], errors="coerce")
    return temp_df


def stock_sse_deal_daily(date: str = "20241216") -> pd.DataFrame:
    """上海证券交易所-数据-股票数据-成交概况-每日股票情况。

    https://www.sse.com.cn/market/stockdata/overview/day/
    :param date: 交易日，YYYYMMDD
    :return: 每日股票情况
    """
    url = "https://query.sse.com.cn/commonQuery.do"
    params = {
        "sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
        "PRODUCT_CODE": "01,02,03,11,17",
        "type": "inParams",
        "SEARCH_DATE": "-".join([date[:4], date[4:6], date[6:]]),
    }
    data_json = get_json(url, params=params, headers=SSE_HEADERS)
    temp_df = pd.DataFrame(data_json["result"])
    temp_df = temp_df.T
    temp_df.reset_index(inplace=True)
    if len(temp_df.columns) == 5:
        # 20250228 之后
        temp_df.columns = ["单日情况", "主板A", "主板B", "科创板", "股票"]
        temp_df["股票回购"] = "-"
    elif len(temp_df.columns) == 4:
        # 20220104 之前的旧结构
        temp_df.columns = ["单日情况", "主板A", "主板B", "科创板"]
        temp_df["股票"] = "-"
        temp_df["股票回购"] = "-"
    else:
        temp_df.columns = [
            "单日情况",
            "主板A",
            "主板B",
            "科创板",
            "股票回购",
            "股票",
        ]
    temp_df = temp_df[
        ["单日情况", "股票", "主板A", "主板B", "科创板", "股票回购"]
    ]
    temp_df["单日情况"] = [
        "市价总值",
        "成交量",
        "平均市盈率",
        "换手率",
        "成交金额",
        "-",
        "流通市值",
        "流通换手率",
        "报告日期",
        "挂牌数",
        "-",
    ]
    temp_df = temp_df[temp_df["单日情况"] != "-"]
    temp_df = temp_df[temp_df["单日情况"] != "报告日期"]
    # 指标顺序固定成人类可读的顺序
    desired_order = [
        "挂牌数",
        "市价总值",
        "流通市值",
        "成交金额",
        "成交量",
        "平均市盈率",
        "换手率",
        "流通换手率",
    ]
    temp_df["单日情况"] = pd.Categorical(
        temp_df["单日情况"], categories=desired_order, ordered=True
    )
    temp_df.sort_values("单日情况", ignore_index=True, inplace=True)
    temp_df["股票"] = pd.to_numeric(temp_df["股票"], errors="coerce")
    temp_df["主板A"] = pd.to_numeric(temp_df["主板A"], errors="coerce")
    temp_df["主板B"] = pd.to_numeric(temp_df["主板B"], errors="coerce")
    temp_df["科创板"] = pd.to_numeric(temp_df["科创板"], errors="coerce")
    temp_df["股票回购"] = pd.to_numeric(temp_df["股票回购"], errors="coerce")
    return temp_df


# --------------------------------------------------------------------------- #
# 宏观：金十 Shibor / 商务部社融
# --------------------------------------------------------------------------- #
def macro_china_shibor_all() -> pd.DataFrame:
    """上海银行业同业拆借报告（金十数据中心），数据区间 20170317 至今。

    https://datacenter.jin10.com/reportType/dc_shibor
    :return: 各期限 Shibor 的定价与涨跌幅
    """
    t = time.time()
    params = {"_": t}
    data_json = get_json(
        "https://cdn.jin10.com/data_center/reports/il_1.json", params=params
    )
    temp_df = pd.DataFrame(data_json["values"]).T
    big_df = pd.DataFrame()
    temp_df.fillna(value="--", inplace=True)
    big_df["O/N-定价"] = temp_df["O/N"].apply(lambda x: x[0])
    big_df["O/N-涨跌幅"] = temp_df["O/N"].apply(lambda x: x[1])
    big_df["1W-定价"] = temp_df["1W"].apply(lambda x: x[0])
    big_df["1W-涨跌幅"] = temp_df["1W"].apply(lambda x: x[1])
    big_df["2W-定价"] = temp_df["2W"].apply(lambda x: x[0])
    big_df["2W-涨跌幅"] = temp_df["2W"].apply(lambda x: x[1])
    big_df["1M-定价"] = temp_df["1M"].apply(lambda x: x[0])
    big_df["1M-涨跌幅"] = temp_df["1M"].apply(lambda x: x[1])
    big_df["3M-定价"] = temp_df["3M"].apply(lambda x: x[0])
    big_df["3M-涨跌幅"] = temp_df["3M"].apply(lambda x: x[1])
    big_df["6M-定价"] = temp_df["6M"].apply(lambda x: x[0])
    big_df["6M-涨跌幅"] = temp_df["6M"].apply(lambda x: x[1])
    big_df["9M-定价"] = temp_df["9M"].apply(lambda x: x[0])
    big_df["9M-涨跌幅"] = temp_df["9M"].apply(lambda x: x[1])
    big_df["1Y-定价"] = temp_df["1Y"].apply(lambda x: x[0])
    big_df["1Y-涨跌幅"] = temp_df["1Y"].apply(lambda x: x[1])
    big_df = big_df.apply(lambda x: x.replace("-", np.nan))
    big_df = big_df.apply(lambda x: x.replace([None], np.nan))
    for item in big_df.columns:
        big_df[item] = pd.to_numeric(big_df[item], errors="coerce")
    big_df.sort_index(inplace=True)
    big_df.reset_index(inplace=True)
    big_df.rename(columns={"index": "日期"}, inplace=True)
    return big_df


def _mofcom_post_transport(url: str, headers: dict, timeout: float) -> bytes:
    """商务部站点专用 POST transport。

    该站只接受 POST，且只认 TLS1.2 + SECLEVEL=1（urllib 默认的 TLS1.3 套件会被
    服务端以 SSLV3_ALERT_HANDSHAKE_FAILURE 拒掉）。交给 ``stock.http.request`` 的
    ``transport`` 注入点，这样限流、指数退避重试仍然复用公共实现。
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    req = urllib.request.Request(
        url, data=b"", headers=dict(headers or {}), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def macro_china_shrzgm() -> pd.DataFrame:
    """商务数据中心-国内贸易-社会融资规模增量统计。

    https://data.mofcom.gov.cn/gnmy/shrzgm.shtml
    :return: 社会融资规模增量统计
    """
    url = "https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery"
    headers = {
        "Referer": "https://data.mofcom.gov.cn/gnmy/shrzgm.shtml",
        "Origin": "https://data.mofcom.gov.cn",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
    }
    raw = request(url, headers=headers, transport=_mofcom_post_transport)
    data_json = json.loads(raw.decode("utf-8"))
    temp_df = pd.DataFrame(data_json)
    temp_df.columns = [
        "月份",
        "其中-未贴现银行承兑汇票",
        "其中-委托贷款",
        "其中-委托贷款外币贷款",
        "其中-人民币贷款",
        "其中-企业债券",
        "社会融资规模增量",
        "其中-非金融企业境内股票融资",
        "其中-信托贷款",
    ]
    temp_df = temp_df[
        [
            "月份",
            "社会融资规模增量",
            "其中-人民币贷款",
            "其中-委托贷款外币贷款",
            "其中-委托贷款",
            "其中-信托贷款",
            "其中-未贴现银行承兑汇票",
            "其中-企业债券",
            "其中-非金融企业境内股票融资",
        ]
    ]
    temp_df["社会融资规模增量"] = pd.to_numeric(
        temp_df["社会融资规模增量"], errors="coerce"
    )
    temp_df["其中-人民币贷款"] = pd.to_numeric(
        temp_df["其中-人民币贷款"], errors="coerce"
    )
    temp_df["其中-委托贷款外币贷款"] = pd.to_numeric(
        temp_df["其中-委托贷款外币贷款"], errors="coerce"
    )
    temp_df["其中-委托贷款"] = pd.to_numeric(temp_df["其中-委托贷款"], errors="coerce")
    temp_df["其中-信托贷款"] = pd.to_numeric(temp_df["其中-信托贷款"], errors="coerce")
    temp_df["其中-未贴现银行承兑汇票"] = pd.to_numeric(
        temp_df["其中-未贴现银行承兑汇票"], errors="coerce"
    )
    temp_df["其中-企业债券"] = pd.to_numeric(temp_df["其中-企业债券"], errors="coerce")
    temp_df["其中-非金融企业境内股票融资"] = pd.to_numeric(
        temp_df["其中-非金融企业境内股票融资"], errors="coerce"
    )
    temp_df.sort_values(["月份"], inplace=True)
    temp_df.reset_index(drop=True, inplace=True)
    return temp_df


# --------------------------------------------------------------------------- #
# 财联社电报
# --------------------------------------------------------------------------- #
def stock_info_global_cls(symbol: str = "全部") -> pd.DataFrame:
    """财联社-电报（接口要 sha1+md5 的 sign 参数）。

    https://www.cls.cn/telegraph
    :param symbol: choice of {"全部", "重点"}
    :return: 电报列表
    """
    url = "https://www.cls.cn/v1/roll/get_roll_list"
    params = {
        "app": "CailianpressWeb",
        "category": "",
        "last_time": int(time.time()),
        "os": "web",
        "refresh_type": "1",
        "rn": "20",
        "sv": "8.4.6",
    }
    params["sign"] = hashlib.md5(
        hashlib.sha1(urllib.parse.urlencode(params).encode("utf-8")).hexdigest().encode(
            "utf-8"
        )
    ).hexdigest()
    data_json = get_json(url, params=params, headers=CLS_HEADERS)
    temp_df = pd.DataFrame(data_json["data"]["roll_data"])
    big_df = temp_df.copy()
    big_df = big_df[["title", "content", "ctime", "level"]]
    big_df["ctime"] = pd.to_datetime(big_df["ctime"], unit="s", utc=True).dt.tz_convert(
        "Asia/Shanghai"
    )
    big_df.columns = ["标题", "内容", "发布时间", "等级"]
    big_df.sort_values(["发布时间"], inplace=True)
    big_df.reset_index(inplace=True, drop=True)
    big_df["发布日期"] = big_df["发布时间"].dt.date
    big_df["发布时间"] = big_df["发布时间"].dt.time
    if symbol == "重点":
        big_df = big_df[(big_df["等级"] == "B") | (big_df["等级"] == "A")]
        big_df.reset_index(inplace=True, drop=True)
    return big_df[["标题", "内容", "发布日期", "发布时间"]]


# --------------------------------------------------------------------------- #
# 乐咕乐股：赚钱效应
# --------------------------------------------------------------------------- #
def stock_market_activity_legu() -> pd.DataFrame:
    """乐咕乐股网-赚钱效应分析（HTML 表格 + 两个摘要 div）。

    https://www.legulegu.com/stockdata/market-activity
    :return: item / value 两列的指标表
    """
    url = "https://legulegu.com/stockdata/market-activity"
    # 该站会随机回 429（同一 IP 上约每 5 次 1 次，实测与频率无关），
    # 而 stock.http 对 4xx 不重试，所以这里做几次短退避重试。
    for attempt in range(5):
        try:
            html = get_text(url, headers=LEGU_HEADERS)
            break
        except HttpError as exc:
            if "HTTP 429" not in str(exc) or attempt == 4:
                raise
            time.sleep(1.0 * (attempt + 1))
    temp_df = pd.read_html(StringIO(html))[0]
    temp_df_one = temp_df.iloc[:, :2]
    temp_df_one.columns = ["item", "value"]
    temp_df_two = temp_df.iloc[:, 2:4]
    temp_df_two.columns = ["item", "value"]
    temp_df_three = temp_df.iloc[:, 4:6]
    temp_df_three.columns = ["item", "value"]
    temp_df = pd.concat(
        objs=[temp_df_one, temp_df_two, temp_df_three], ignore_index=True
    )
    temp_df.dropna(how="all", axis=0, inplace=True)
    soup = BeautifulSoup(html, features="lxml")
    item_str = soup.find(name="div", attrs={"class": "metric-activity"}).text.strip()
    inner_temp_df = pd.DataFrame([item.strip() for item in item_str.split("\n")]).T
    inner_temp_df.columns = ["item", "value"]
    temp_df = pd.concat(objs=[temp_df, inner_temp_df], ignore_index=True)
    item_str = soup.find(
        name="div", attrs={"class": "market-activity-meta"}
    ).text.strip()
    inner_temp_df = pd.DataFrame(["统计日期", item_str]).T
    inner_temp_df.columns = ["item", "value"]
    temp_df = pd.concat(objs=[temp_df, inner_temp_df], ignore_index=True)
    temp_df.reset_index(inplace=True, drop=True)
    return temp_df


# --------------------------------------------------------------------------- #
# 百度股市通：估值
# --------------------------------------------------------------------------- #
def stock_zh_valuation_baidu(
    symbol: str = "002044", indicator: str = "总市值", period: str = "近一年"
) -> pd.DataFrame:
    """百度股市通-A股-财务报表-估值数据（历史曲线逐点返回）。

    https://gushitong.baidu.com/stock/ab-002044
    :param symbol: 股票代码
    :param indicator: choice of {"总市值", "市盈率(TTM)", "市盈率(静)", "市净率", "市现率"}
    :param period: choice of {"近一年", "近三年", "近五年", "近十年", "全部"}
    :return: date / value 两列的估值曲线
    """
    url = "https://gushitong.baidu.com/opendata"
    params = {
        "openapi": "1",
        "dspName": "iphone",
        "tn": "tangram",
        "client": "app",
        "query": indicator,
        "code": symbol,
        "word": "",
        "resource_id": "51171",
        "market": "ab",
        "tag": indicator,
        "chart_select": period,
        "industry_select": "",
        "skip_industry": "1",
        "finClientType": "pc",
    }
    data_json = get_json(url, params=params, headers=BAIDU_HEADERS)
    temp_df = pd.DataFrame(
        data_json["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"][
            "chartInfo"
        ][0]["body"]
    )
    temp_df.columns = ["date", "value"]
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
    temp_df["value"] = pd.to_numeric(temp_df["value"], errors="coerce")
    return temp_df


# --------------------------------------------------------------------------- #
# 同花顺：ETF 实时行情
# --------------------------------------------------------------------------- #
def _ths_etf_category_frame(date: str = "") -> pd.DataFrame:
    """同花顺理财-基金数据-ETF 实时行情（JSONP：``g(...)``）。"""
    symbol_map = {
        "股票型": "gpx",
        "债券型": "zqx",
        "混合型": "hhx",
        "ETF": "ETF",
        "LOF": "LOF",
        "QDII": "QDII",
        "保本型": "bbx",
        "指数型": "zsx",
        "": "all",
    }
    inner_symbol = symbol_map.get("ETF", "ETF")
    inner_date = "-".join([date[:4], date[4:6], date[6:]]) if date != "" else 0
    url = (
        f"https://fund.10jqka.com.cn/data/Net/info/"
        f"{inner_symbol}_rate_desc_{inner_date}_0_1_9999_0_0_0_jsonp_g.html"
    )
    text = get_text(url, headers=THS_HEADERS)
    data_text = text[2:-1]  # 去掉 JSONP 包裹 "g(" / ")"
    data_json = json.loads(data_text)
    temp_df = pd.DataFrame(data_json["data"]["data"]).T
    temp_df.reset_index(inplace=True, drop=True)
    temp_df.reset_index(inplace=True)
    temp_df["index"] = temp_df["index"] + 1
    temp_df.rename(
        columns={
            "index": "序号",
            "code": "基金代码",
            "typename": "基金类型",
            "net": "当前-单位净值",
            "name": "基金名称",
            "totalnet": "当前-累计净值",
            "newnet": "最新-单位净值",
            "newtotalnet": "最新-累计净值",
            "newdate": "最新-交易日",
            "net1": "前一日-单位净值",
            "totalnet1": "前一日-累计净值",
            "ranges": "增长值",
            "rate": "增长率",
            "shstat": "赎回状态",
            "sgstat": "申购状态",
        },
        inplace=True,
    )
    temp_df = temp_df[
        [
            "序号",
            "基金代码",
            "基金名称",
            "当前-单位净值",
            "当前-累计净值",
            "前一日-单位净值",
            "前一日-累计净值",
            "增长值",
            "增长率",
            "赎回状态",
            "申购状态",
            "最新-交易日",
            "最新-单位净值",
            "最新-累计净值",
            "基金类型",
        ]
    ]
    query_date = inner_date if inner_date != 0 else temp_df["最新-交易日"].iloc[0]
    temp_df["查询日期"] = query_date
    temp_df["查询日期"] = pd.to_datetime(temp_df["查询日期"], errors="coerce").dt.date
    temp_df["当前-单位净值"] = pd.to_numeric(temp_df["当前-单位净值"], errors="coerce")
    temp_df["当前-累计净值"] = pd.to_numeric(temp_df["当前-累计净值"], errors="coerce")
    temp_df["前一日-单位净值"] = pd.to_numeric(
        temp_df["前一日-单位净值"], errors="coerce"
    )
    temp_df["前一日-累计净值"] = pd.to_numeric(
        temp_df["前一日-累计净值"], errors="coerce"
    )
    temp_df["增长值"] = pd.to_numeric(temp_df["增长值"], errors="coerce")
    temp_df["增长率"] = pd.to_numeric(temp_df["增长率"], errors="coerce")
    temp_df["最新-单位净值"] = pd.to_numeric(temp_df["最新-单位净值"], errors="coerce")
    temp_df["最新-累计净值"] = pd.to_numeric(temp_df["最新-累计净值"], errors="coerce")
    temp_df["最新-交易日"] = pd.to_datetime(
        temp_df["最新-交易日"], errors="coerce"
    ).dt.date
    return temp_df


def fund_etf_spot_ths(date: str = "", start_date: str | None = None) -> pd.DataFrame:
    """同花顺理财-基金数据-每日净值-ETF-实时行情。

    https://fund.10jqka.com.cn/datacenter/jz/kfs/etf/
    :param date: 查询日期，YYYYMMDD；空串表示最新
    :param start_date: ``date`` 的别名（兼容按 start_date 调用的调用方）
    :return: ETF 实时行情
    """
    if start_date is not None and date == "":
        date = start_date
    return _ths_etf_category_frame(date=date)


# --------------------------------------------------------------------------- #
# 蛋卷（雪球）基金详情
# --------------------------------------------------------------------------- #
def fund_individual_basic_info_xq(
    symbol: str = "000001", timeout: float | None = None
) -> pd.DataFrame:
    """雪球基金-基金详情（蛋卷接口）。

    https://danjuanfunds.com/djapi/fund/675091
    :param symbol: 基金代码
    :param timeout: choice of None or a positive float number
    :return: item / value 两列的基本信息
    """
    url = f"https://danjuanfunds.com/djapi/fund/{symbol}"
    json_data = get_json(url, headers=XQ_HEADERS, timeout=timeout)
    data = json_data.get("data") if isinstance(json_data, dict) else None
    if not data:
        msg = ""
        if isinstance(json_data, dict):
            msg = str(json_data.get("message") or json_data.get("result_code") or "")
        raise ValueError(f"蛋卷基金档案无数据：{symbol} {msg}".strip())
    temp_df = pd.json_normalize(data)
    temp_df.rename(
        columns={
            "fd_code": "基金代码",
            "fd_name": "基金名称",
            "fd_full_name": "基金全称",
            "found_date": "成立时间",
            "totshare": "最新规模",
            "keeper_name": "基金公司",
            "manager_name": "基金经理",
            "trup_name": "托管银行",
            "type_desc": "基金类型",
            "rating_source": "评级机构",
            "rating_desc": "基金评级",
            "invest_orientation": "投资策略",
            "invest_target": "投资目标",
            "performance_bench_mark": "业绩比较基准",
        },
        inplace=True,
    )
    if "评级机构" not in temp_df.columns:
        temp_df["评级机构"] = pd.NA
    temp_df = temp_df[
        [
            "基金代码",
            "基金名称",
            "基金全称",
            "成立时间",
            "最新规模",
            "基金公司",
            "基金经理",
            "托管银行",
            "基金类型",
            "评级机构",
            "基金评级",
            "投资策略",
            "投资目标",
            "业绩比较基准",
        ]
    ]
    temp_df = temp_df.T.reset_index()
    temp_df.columns = ["item", "value"]
    return temp_df


def fund_scale_em(symbol: str = "000001", timeout: float | None = None) -> pd.DataFrame:
    """天天基金-基金规模（东财 fundmobapi）。蛋卷取不到规模时的兜底源。

    https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation?FCODE=005025
    :param symbol: 基金代码（6 位）
    :return: 基金代码 / 基金简称 / 最新规模(元) / 规模日期 各一行的 DataFrame；
             取不到规模抛 ValueError（由调用方决定是否降级）
    """
    url = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation"
           f"?FCODE={symbol}&deviceid=1&plat=Android&product=EFund&version=6.3.8")
    data = (get_json(url, headers=EM_FUND_HEADERS, timeout=timeout).get("Datas") or {})
    nav = pd.to_numeric(data.get("ENDNAV"), errors="coerce")
    if nav is None or pd.isna(nav) or float(nav) <= 0:
        raise ValueError(f"东财基金规模取不到：{symbol}")
    return pd.DataFrame([{
        "基金代码": str(data.get("FCODE") or symbol).zfill(6),
        "基金简称": str(data.get("SHORTNAME") or ""),
        "最新规模(元)": float(nav),
        "规模日期": str(data.get("FSRQ") or "")[:10],
    }])

# --------------------------------------------------------------------------- #
# 巨潮资讯：全市场公告（按披露日）
# --------------------------------------------------------------------------- #
CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HEADERS = {
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
#: 巨潮的市场参数：plate 为空 = 全市场（含港股/新三板，总量约 2,000+ 条/日）；
#: A 股要按三市分别取（sz/sh/bj），三市合计 = 全 A 股当日公告。
CNINFO_PLATES = ("sz", "sh", "bj")
_NOTICE_COLUMNS = ["代码", "名称", "公告标题", "公告类型", "公告日期", "网址"]

_NOTICE_PDF_HOST = "www.cninfo.com.cn"
_NOTICE_PDF_STATIC = "static.cninfo.com.cn"

CNINFO_PDF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}


def _cninfo_post(url: str, headers: dict, timeout: float, form: dict) -> bytes:
    """POST 表单取原始字节（仅供 ``stock_notice_report`` 用）。

    巨潮查询接口只接受 POST，而 :mod:`stock.http` 只有 GET，所以按
    ``macro_china_shrzgm`` 的做法把 POST 作为 transport 注入——限流/退避重试仍走 stock.http。
    """
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def stock_notice_report(date: str, plate: str = "all", page_size: int = 30,
                        max_pages: int = 400) -> pd.DataFrame:
    """巨潮资讯网：全市场公告（按披露日，A 股三市）。

    为什么走巨潮：证监会指定披露平台，A 股全部公告都在这里，且每条带披露时间戳
    （point-in-time 的前提）。东财公告接口按概念分类、标题会被截断，不适合做
    「标题正则 + 披露日」的规则打标。

    契约（列名/顺序是下游契约，不可随意改）：
        ``代码 / 名称 / 公告标题 / 公告类型 / 公告日期 / 网址``
    ``公告日期`` 为 ``YYYY-MM-DD``（接口给毫秒时间戳，按本机时区取日期）。

    :param date: 披露日，``YYYYMMDD`` 或 ``YYYY-MM-DD``
    :param plate: ``all``（三市合并）/ ``sz`` / ``sh`` / ``bj``
    :param page_size: 巨潮服务端硬上限 30 条/页（传更大也按 30 返回）
    :param max_pages: 单市场翻页上限（防御：接口异常时不无限翻页）
    """
    d = str(date or "").strip().replace("/", "").replace("-", "")
    if len(d) != 8 or not d.isdigit():
        raise ValueError(f"date 需为 YYYYMMDD 或 YYYY-MM-DD：{date!r}")
    day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    plates = CNINFO_PLATES if str(plate or "all").lower() == "all" else (str(plate).lower(),)
    bad = [p for p in plates if p not in CNINFO_PLATES]
    if bad:
        raise ValueError(f"plate 只支持 all/{'/'.join(CNINFO_PLATES)}，收到 {plate!r}")

    rows: list[dict] = []
    for pl in plates:
        total = None
        got = 0
        for page in range(1, int(max_pages) + 1):
            form = {
                "pageNum": str(page), "pageSize": str(int(page_size)),
                "column": "szse", "tabName": "fulltext", "plate": pl,
                "stock": "", "searchkey": "", "secDate": "", "category": "",
                "trade": "", "seDate": f"{day}~{day}",
                "sortName": "", "sortType": "", "isHLtitle": "false",
            }

            def transport(_url, _headers, _timeout, _form=form):
                return _cninfo_post(_url, _headers, _timeout, _form)

            raw = request(CNINFO_QUERY, headers=CNINFO_HEADERS, transport=transport)
            try:
                js = json.loads(raw.decode("utf-8", "replace"))
            except ValueError as e:
                raise ValueError(f"巨潮 {day}({pl}) 第 {page} 页不是 JSON：{e}") from e
            if total is None:
                total = int(js.get("totalAnnouncement") or 0)
            anns = js.get("announcements") or []
            if not anns:
                break
            for a in anns:
                ts = a.get("announcementTime")
                when = day
                if ts:
                    try:
                        when = time.strftime("%Y-%m-%d", time.localtime(float(ts) / 1000.0))
                    except (TypeError, ValueError, OSError):
                        when = day
                adj = str(a.get("adjunctUrl") or "").lstrip("/")
                rows.append({
                    "代码": str(a.get("secCode") or "").zfill(6),
                    "名称": str(a.get("secName") or ""),
                    "公告标题": str(a.get("announcementTitle") or "").strip(),
                    "公告类型": str(a.get("announcementTypeName") or "").strip(),
                    "公告日期": when,
                    "网址": ("http://static.cninfo.com.cn/" + adj) if adj else "",
                })
            got += len(anns)
            if total and got >= total:
                break

    df = pd.DataFrame(rows, columns=_NOTICE_COLUMNS)
    if len(df):
        df = df.drop_duplicates(subset=["代码", "公告标题", "公告日期"])
        df = df.sort_values(["公告日期", "代码"]).reset_index(drop=True)
    return df


def stock_notice_text(url: str, max_pages: int = 40, timeout: float = 30.0) -> str:
    """巨潮公告 PDF 正文（纯文本）。

    为什么需要：标题只够打「减持 / 立案」这类标签，正文才能分清
    「拟减持」与「已减持完毕」、「问询函」与「行政处罚决定书」。
    依赖 ``pypdf``（纯 Python，不引入系统级 PDF 工具）。

    :param url: 公告 PDF 地址，即 ``stock_notice_report`` 的「网址」列
    :param max_pages: 只解析前 N 页（公告正文一般 2–10 页，防御超长附件）
    """
    u = str(url or "").strip()
    if _NOTICE_PDF_HOST in u:
        # 巨潮的 PDF 在 static 子域；早期缓存里存过 www 地址，这里统一纠正
        u = u.replace(_NOTICE_PDF_HOST, _NOTICE_PDF_STATIC)
    if not u.lower().startswith("http"):
        raise ValueError(f"url 需为巨潮公告 PDF 地址：{url!r}")
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - 环境缺依赖时直接说明
        raise RuntimeError("公告正文解析缺依赖：pip install pypdf") from e
    raw = request(u, headers=CNINFO_PDF_HEADERS, timeout=timeout)
    if not raw:
        raise ValueError(f"公告 PDF 为空：{u}")
    try:
        reader = PdfReader(BytesIO(raw))
    except Exception as e:  # noqa: BLE001 - pypdf 抛的异常类型很杂
        raise ValueError(f"公告 PDF 解析失败：{u}：{e}") from e
    pages = list(reader.pages)[: max(1, int(max_pages))]
    chunks = []
    for p in pages:
        try:
            chunks.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页坏掉不影响整篇
            continue
    text = "\n".join(chunks)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
