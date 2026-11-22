# -*- coding: utf-8 -*-
"""新浪财经系接口（债券 / 外汇 / A股 / 指数 / 板块 / 快讯 / 交易日历 /
财务指标 / ETF）＋ 中国债券信息网收益率曲线。

约束
----
- 只用 ``stock.http`` 的 ``get_text`` / ``get_json`` 发请求（禁用 requests）；
- 新浪 K 线接口需要 ``hk_js_decode`` 解码，本模块已内联该解码器，不引外部包；
- 不做缓存：一次调用一次请求；列名/顺序/单位是下游契约。
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import math
import re
from io import StringIO

import pandas as pd
import py_mini_racer
from bs4 import BeautifulSoup

from .common import get_json, get_text

# ---------------------------------------------------------------------------
# URL 常量
# ---------------------------------------------------------------------------
#: 新浪 A 股/ETF 日线（klc2 目录，需 JS 解密）
SINA_HIST_KLC2 = "https://finance.sina.com.cn/realstock/company/{}/hisdata_klc2/klc_kl.js"
#: 新浪指数/债券日线（history 目录，需 JS 解密）
SINA_HIST_KLC = "https://finance.sina.com.cn/realstock/company/{}/hisdata/klc_kl.js"
#: 新浪交易日历
SINA_TRADE_DATE = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
#: 新浪 A 股流通股本（amount 接口）
SINA_A_STOCK_AMOUNT = (
    "https://stock.finance.sina.com.cn/stock/api/jsonp.php/"
    "var%20KKE_ShareAmount_{}=/StockService.getAmountBySymbol?_=20&symbol={}"
)
#: 新浪 A 股后复权/前复权因子
SINA_A_STOCK_HFQ = "https://finance.sina.com.cn/realstock/company/{}/hfq.js"
SINA_A_STOCK_QFQ = "https://finance.sina.com.cn/realstock/company/{}/qfq.js"
#: 中债收益率曲线
CHINABOND_YIELD = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
#: 新浪中行牌价
SINA_FOREX = "http://biz.finance.sina.com.cn/forex/forex.php"
#: 新浪全球财经快讯
SINA_GLOBAL_NEWS = "https://zhibo.sina.com.cn/api/zhibo/feed"
#: 新浪财务报表-关键指标
SINA_FINANCE_ABSTRACT = (
    "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
)
#: 新浪财务指标（HTML 表格）
SINA_FIN_GUIDELINE = (
    "https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/"
    "stockid/{}/ctrl/{}/displaytype/4.phtml"
)
#: 新浪板块行情
SINA_SECTOR_INDUSTRY = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
SINA_SECTOR_QMX = "http://biz.finance.sina.com.cn/hq/qmxIndustryHq.php"
SINA_SECTOR_CLASS = "http://money.finance.sina.com.cn/q/view/newFLJK.php"
#: 新浪板块成分 / 基金列表
SINA_MARKET_CENTER = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.{}"
)
SINA_FUND_LIST = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
    "IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']/Market_Center.getHQNodeDataSimple"
)

#: 财务指标页需要带浏览器 UA（否则可能被拦）
_CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    )
}
_CONS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# 新浪 K 线 JS 解密器（MIT 许可，源自 stock/cons.py::hk_js_decode）
# ---------------------------------------------------------------------------
_HK_JS_DECODE = """
function d(t) {
            var e, i, n, r, a, o, s, l = (arguments,
            864e5), u = 7657, c = [], d = [], h = ~(3 << 30), f = 1 << 30, m = [0, 3, 5, 6, 9, 10, 12, 15, 17, 18, 20, 23, 24, 27, 29, 30], p = Math, g = function() {
                var l, u;
                for (l = 0; 64 > l; l++)
                    d[l] = p.pow(2, l),
                    26 > l && (c[l] = v(l + 65),
                    c[l + 26] = v(l + 97),
                    10 > l && (c[l + 52] = v(l + 48)));
                for (c.push("+", "/"),
                c = c.join(""),
                i = t.split(""),
                n = i.length,
                l = 0; n > l; l++)
                    i[l] = c.indexOf(i[l]);
                return r = {},
                e = o = 0,
                a = {},
                u = w([12, 6]),
                s = 63 ^ u[1],
                {
                    _1479: D,
                    _136: _,
                    _200: C,
                    _139: R,
                    _197: A,
                    _3466: O
                }["_" + u[0]] || function() {
                    return []
                }
            }, v = String.fromCharCode, b = function(t) {
                return t === {}._
            }, N = function() {
                var t, e;
                for (t = y(),
                e = 1; ; ) {
                    if (!y())
                        return e * (2 * t - 1);
                    e++
                }
            }, y = function() {
                var t;
                return e >= n ? 0 : (t = i[e] & 1 << o,
                o++,
                o >= 6 && (o -= 6,
                e++),
                !!t)
            }, w = function(t, r, a) {
                var s, l, u, c, h;
                for (l = [],
                u = 0,
                r || (r = []),
                a || (a = []),
                s = 0; s < t.length; s++)
                    if (c = t[s],
                    u = 0,
                    c) {
                        if (e >= n)
                            return l;
                        if (t[s] <= 0)
                            u = 0;
                        else if (t[s] <= 30) {
                            for (; h = 6 - o,
                            h = c > h ? h : c,
                            u |= (i[e] >> o & (1 << h) - 1) << t[s] - c,
                            o += h,
                            o >= 6 && (o -= 6,
                            e++),
                            c -= h,
                            !(0 >= c); )
                                ;
                            r[s] && u >= d[t[s] - 1] && (u -= d[t[s]])
                        } else
                            u = w([30, t[s] - 30], [0, r[s]]),
                            a[s] || (u = u[0] + u[1] * d[30]);
                        l[s] = u
                    } else
                        l[s] = 0;
                return l
            }, x = function() {
                var t;
                return t = w([3])[0],
                1 == t ? (r.d = w([18], [1])[0],
                t = 0) : t || (t = w([6])[0]),
                t
            }, S = function(t) {
                var e, i, n;
                for (t > 1 && (e = 0),
                e = 0; t > e; e++)
                    r.d++,
                    n = r.d % 7,
                    (3 == n || 4 == n) && (r.d += 5 - n);
                return i = new Date,
                i.setTime((u + r.d) * l),
                i
            }, k = function(t) {
                var e, i, n;
                for (n = r.wd || 62,
                e = 0; t > e; e++)
                    do
                        r.d++;
                    while (!(n & 1 << (r.d % 7 + 10) % 7));
                return i = new Date,
                i.setTime((u + r.d) * l),
                i
            }, T = function(t) {
                var e, i, n;
                return t ? 0 > t ? (e = T(-t),
                [-e[0], -e[1]]) : (e = t % 3,
                i = (t - e) / 3,
                n = [i, i],
                e && n[e - 1]++,
                n) : [0, 0]
            }, P = function(t, e, i) {
                var n, r, a;
                for (r = "number" == typeof e ? T(e) : e,
                a = T(i),
                n = [a[0] - r[0], a[1] - r[1]],
                r = 1; n[0] < n[1]; )
                    r *= 5,
                    n[1]--;
                for (; n[1] < n[0]; )
                    r *= 2,
                    n[0]--;
                if (r > 1 && (t *= r),
                n = n[0],
                t = E(t),
                0 > n) {
                    for (; t.length + n <= 0; )
                        t = "0" + t;
                    return n += t.length,
                    r = t.substr(0, n) - 0,
                    void 0 === i ? r + "." + t.substr(n) - 0 : (a = t.charAt(n) - 0,
                    a > 5 ? r++ : 5 == a && (t.substr(n + 1) - 0 > 0 ? r++ : r += 1 & r),
                    r)
                }
                for (; n > 0; n--)
                    t += "0";
                return t - 0
            }, C = function() {
                var t, i, a, o, l;
                if (s >= 1)
                    return [];
                for (r.d = w([18], [1])[0] - 1,
                a = w([3, 3, 30, 6]),
                r.p = a[0],
                r.ld = a[1],
                r.cd = a[2],
                r.c = a[3],
                r.m = p.pow(10, r.p),
                r.pc = r.cd / r.m,
                i = [],
                t = 0; o = {
                    d: 1
                },
                y() && (a = w([3])[0],
                0 == a ? o.d = w([6])[0] : 1 == a ? (r.d = w([18])[0],
                o.d = 0) : o.d = a),
                l = {
                    date: S(o.d)
                },
                y() && (r.ld += N()),
                a = w([3 * r.ld], [1]),
                r.cd += a[0],
                l.close = r.cd / r.m,
                i.push(l),
                !(e >= n) && (e != n - 1 || 63 & (r.c ^ t + 1)); t++)
                    ;
                return i[0].prevclose = r.pc,
                i
            }, _ = function() {
                var t, i, a, o, l, u, c, d, h, f, m;
                if (s > 2)
                    return [];
                for (c = [],
                h = {
                    v: "volume",
                    p: "price",
                    a: "avg_price"
                },
                r.d = w([18], [1])[0] - 1,
                d = {
                    date: S(1)
                },
                a = w(1 > s ? [3, 3, 4, 1, 1, 1, 5] : [4, 4, 4, 1, 1, 1, 3]),
                t = 0; 7 > t; t++)
                    r[["la", "lp", "lv", "tv", "rv", "zv", "pp"][t]] = a[t];
                for (r.m = p.pow(10, r.pp),
                s >= 1 ? (a = w([3, 3]),
                r.c = a[0],
                a = a[1]) : (a = 5,
                r.c = 2),
                r.pc = w([6 * a])[0],
                d.pc = r.pc / r.m,
                r.cp = r.pc,
                r.da = 0,
                r.sa = r.sv = 0,
                t = 0; !(e >= n) && (e != n - 1 || 7 & (r.c ^ t)); t++) {
                    for (l = {},
                    o = {},
                    f = r.tv ? y() : 1,
                    i = 0; 3 > i; i++)
                        if (m = ["v", "p", "a"][i],
                        (f ? y() : 0) && (a = N(),
                        r["l" + m] += a),
                        u = "v" == m && r.rv ? y() : 1,
                        a = w([3 * r["l" + m] + ("v" == m ? 7 * u : 0)], [!!i])[0] * (u ? 1 : 100),
                        o[m] = a,
                        "v" == m) {
                            if (!(l[h[m]] = a) && (s > 1 || 241 > t) && (r.zv ? !y() : 1)) {
                                o.p = 0;
                                break
                            }
                        } else
                            "a" == m && (r.da = (1 > s ? 0 : r.da) + o.a);
                    r.sv += o.v,
                    l[h.p] = (r.cp += o.p) / r.m,
                    r.sa += o.v * r.cp,
                    l[h.a] = b(o.a) ? t ? c[t - 1][h.a] : l[h.p] : r.sv ? ((p.floor((r.sa * (2e3 / r.m) + r.sv) / r.sv) >> 1) + r.da) / 1e3 : l[h.p] + r.da / 1e3,
                    c.push(l)
                }
                return c[0].date = d.date,
                c[0].prevclose = d.pc,
                c
            }, D = function() {
                var t, e, i, n, a, o, l;
                if (s >= 1)
                    return [];
                for (r.lv = 0,
                r.ld = 0,
                r.cd = 0,
                r.cv = [0, 0],
                r.p = w([6])[0],
                r.d = w([18], [1])[0] - 1,
                r.m = p.pow(10, r.p),
                a = w([3, 3]),
                r.md = a[0],
                r.mv = a[1],
                t = []; a = w([6]),
                a.length; ) {
                    if (i = {
                        c: a[0]
                    },
                    n = {},
                    i.d = 1,
                    32 & i.c)
                        for (; ; ) {
                            if (a = w([6])[0],
                            63 == (16 | a)) {
                                l = 16 & a ? "x" : "u",
                                a = w([3, 3]),
                                i[l + "_d"] = a[0] + r.md,
                                i[l + "_v"] = a[1] + r.mv;
                                break
                            }
                            if (32 & a) {
                                o = 8 & a ? "d" : "v",
                                l = 16 & a ? "x" : "u",
                                i[l + "_" + o] = (7 & a) + r["m" + o];
                                break
                            }
                            if (o = 15 & a,
                            0 == o ? i.d = w([6])[0] : 1 == o ? (r.d = o = w([18])[0],
                            i.d = 0) : i.d = o,
                            !(16 & a))
                                break
                        }
                    n.date = S(i.d);
                    for (o in {
                        v: 0,
                        d: 0
                    })
                        b(i["x_" + o]) || (r["l" + o] = i["x_" + o]),
                        b(i["u_" + o]) && (i["u_" + o] = r["l" + o]);
                    for (i.l_l = [i.u_d, i.u_d, i.u_d, i.u_d, i.u_v],
                    l = m[15 & i.c],
                    1 & i.u_v && (l = 31 - l),
                    16 & i.c && (i.l_l[4] += 2),
                    e = 0; 5 > e; e++)
                        l & 1 << 4 - e && i.l_l[e]++,
                        i.l_l[e] *= 3;
                    i.d_v = w(i.l_l, [1, 0, 0, 1, 1], [0, 0, 0, 0, 1]),
                    o = r.cd + i.d_v[0],
                    n.open = o / r.m,
                    n.high = (o + i.d_v[1]) / r.m,
                    n.low = (o - i.d_v[2]) / r.m,
                    n.close = (o + i.d_v[3]) / r.m,
                    a = i.d_v[4],
                    "number" == typeof a && (a = [a, a >= 0 ? 0 : -1]),
                    r.cd = o + i.d_v[3],
                    l = r.cv[0] + a[0],
                    r.cv = [l & h, r.cv[1] + a[1] + !!((r.cv[0] & h) + (a[0] & h) & f)],
                    n.volume = (r.cv[0] & f - 1) + r.cv[1] * f,
                    t.push(n)
                }
                return t
            }, R = function() {
                var t, e, i, n;
                if (s > 1)
                    return [];
                for (r.l = 0,
                n = -1,
                r.d = w([18])[0] - 1,
                i = w([18])[0]; r.d < i; )
                    e = S(1),
                    0 >= n ? (y() && (r.l += N()),
                    n = w([3 * r.l], [0])[0] + 1,
                    t || (t = [e],
                    n--)) : t.push(e),
                    n--;
                return t
            }, A = function() {
                var t, i, a, o;
                if (s >= 1)
                    return [];
                for (r.f = w([6])[0],
                r.c = w([6])[0],
                a = [],
                r.dv = [],
                r.dl = [],
                t = 0; t < r.f; t++)
                    r.dv[t] = 0,
                    r.dl[t] = 0;
                for (t = 0; !(e >= n) && (e != n - 1 || 7 & (r.c ^ t)); t++) {
                    for (o = [],
                    i = 0; i < r.f; i++)
                        y() && (r.dl[i] += N()),
                        r.dv[i] += w([3 * r.dl[i]], [1])[0],
                        o[i] = r.dv[i];
                    a.push(o)
                }
                return a
            }, O = function() {
                if (r = {
                    b_avp: 1,
                    b_ph: 0,
                    b_phx: 0,
                    b_sep: 0,
                    p_p: 6,
                    p_v: 0,
                    p_a: 0,
                    p_e: 0,
                    p_t: 0,
                    l_o: 3,
                    l_h: 3,
                    l_l: 3,
                    l_c: 3,
                    l_v: 5,
                    l_a: 5,
                    l_e: 3,
                    l_t: 0,
                    u_p: 0,
                    u_v: 0,
                    u_a: 0,
                    wd: 62,
                    d: 0
                },
                s > 0)
                    return [];
                var t, i, a, o, l, u, c;
                for (t = []; ; ) {
                    if (e >= n)
                        return void 0;
                    if (a = {
                        d: 1,
                        c: 0
                    },
                    y())
                        if (y()) {
                            if (y()) {
                                for (a.c++,
                                a.a = r.b_avp,
                                y() && (r.b_avp ^= y(),
                                r.b_ph ^= y(),
                                r.b_phx ^= y(),
                                a.s = r.b_sep,
                                r.b_sep ^= y(),
                                y() && (r.wd = w([7])[0]),
                                a.s ^ r.b_sep && (a.s ? r.u_p = r.u_c : r.u_o = r.u_h = r.u_l = r.u_c = r.u_p)),
                                u = 0; u < 3 + 2 * r.b_ph; u++)
                                    if (y() && (l = "pvaet".charAt(u),
                                    o = r["p_" + l],
                                    r["p_" + l] += N(),
                                    r["u_" + l] = P(r["u_" + l], o, r["p_" + l]) - 0,
                                    r.b_sep && !u))
                                        for (c = 0; 4 > c; c++)
                                            l = "ohlc".charAt(c),
                                            r["u_" + l] = P(r["u_" + l], o, r.p_p) - 0;
                                !r.b_avp && a.a && (r.u_a = P(i && i.amount || 0, 0, r.p_a))
                            }
                            if (y())
                                for (a.c++,
                                u = 0; u < 7 + r.b_ph + r.b_phx; u++)
                                    y() && (6 == u ? a.d = x() : r["l_" + "ohlcva*et".charAt(u)] += N());
                            if (y() && (a.c++,
                            l = r.l_o + (y() && N()),
                            o = w([3 * l], [1])[0],
                            a.p = r.b_sep ? r.u_c + o : r.u_p += o),
                            !a.c)
                                break
                        } else
                            y() ? y() ? y() ? a.d = x() : r.l_v += N() : r.b_ph && y() ? r["l_" + "et".charAt(r.b_phx && y())] += N() : r.l_a += N() : r["l_" + "ohlc".charAt(w([2])[0])] += N();
                    for (u = 0; u < 6 + r.b_ph + r.b_phx; u++)
                        c = "ohlcvaet".charAt(u),
                        o = (r.b_sep ? 191 : 185) >> u & 1,
                        a["v_" + c] = w([3 * r["l_" + c]], [o])[0];
                    i = {
                        date: k(a.d)
                    },
                    a.p && (i.prevclose = P(a.p, r.p_p)),
                    r.b_sep ? (i.open = P(r.u_o += a.v_o, r.p_p),
                    i.high = P(r.u_h += a.v_h, r.p_p),
                    i.low = P(r.u_l += a.v_l, r.p_p),
                    i.close = P(r.u_c += a.v_c, r.p_p)) : (a.o = r.u_p + a.v_o,
                    i.open = P(a.o, r.p_p),
                    i.high = P(a.o + a.v_h, r.p_p),
                    i.low = P(a.o - a.v_l, r.p_p),
                    i.close = P(r.u_p = a.o + a.v_c, r.p_p)),
                    i.volume = P(r.u_v += a.v_v, r.p_v),
                    r.b_avp ? (o = T(r.p_p),
                    l = T(r.p_v),
                    i.amount = P(P(Math.floor((r.b_sep ? (r.u_o + r.u_h + r.u_l + r.u_c) / 4 : a.o + (a.v_h - a.v_l + a.v_c) / 4) * r.u_v + .5), [o[0] + l[0], o[1] + l[1]], r.p_a) + a.v_a, r.p_a)) : (r.u_a += a.v_a,
                    i.amount = P(r.u_a, r.p_a)),
                    r.b_ph && (i.postVol = P(a.v_e, r.p_e),
                    i.postAmt = P(Math.floor(i.postVol * i.close + (r.b_phx ? P(a.v_t, r.p_t) : 0) + .5), 0)),
                    t.push(i)
                }
                return t
            }, E = function(t) {
                var e, i, n;
                if (t = (t || 0).toString(),
                n = [],
                i = t.toLowerCase().indexOf("e"),
                i > 0) {
                    for (e = t.substr(i + 1) - 0; e >= 0; e--)
                        n.push(Math.floor(e * Math.pow(10, -e) + .5) - 0);
                    return n.join("")
                }
                return t
            };
            return g()()
        }
        ;
"""

_racer = None


def _js_engine():
    """惰性创建唯一的 V8 上下文（MiniRacer 初始化有成本）。"""
    global _racer
    if _racer is None:
        engine = py_mini_racer.MiniRacer()
        engine.eval(_HK_JS_DECODE)
        _racer = engine
    return _racer


def _close_js_engine() -> None:
    """退出前显式关闭 V8 上下文。

    本机 py_mini_racer 不解构 V8 isolate 就无法回收，进程会在解释器退出时
    卡死（表现为脚本跑完不返回）；显式 ``close()`` 可正常退出。
    """
    global _racer
    if _racer is not None:
        try:
            _racer.close()
        except Exception:
            pass
        _racer = None


atexit.register(_close_js_engine)


def _decrypt(text: str) -> list:
    """剥掉 ``var XXX_...="密文";`` 外壳并用 JS 解密成 list[dict]。"""
    body = text.split("=", 1)[1].split(";", 1)[0].replace('"', "")
    return _js_engine().call("d", body)


def _loads(text: str):
    """容错 JSON 解析：先严格 ``json.loads``，失败再补 key 引号 / 换单引号。"""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fixed = re.sub(r"([{,])\s*([A-Za-z_]\w*)\s*:", r'\1"\2":', text)
    try:
        return json.loads(fixed)
    except Exception:
        return json.loads(fixed.replace("'", '"'))


def _klc_frames(symbol: str, template: str = SINA_HIST_KLC2, params: dict | None = None):
    """新浪 K 线通用抓取：返回 list[dict]（空列表表示接口没数据）。"""
    text = get_text(template.format(symbol), params=params)
    try:
        return _decrypt(text)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 债券
# ---------------------------------------------------------------------------
def bond_zh_hs_daily(symbol: str = "sh010107") -> pd.DataFrame:
    """新浪财经-债券-沪深债券-历史行情数据（不复权日 K 线）。

    :param symbol: 沪深债券代码，如 ``sh010107``
    :return: 列 ``date/open/high/low/close``
    """
    url = "https://finance.sina.com.cn/realstock/company/{}/hisdata/klc_kl.js?d={}".format(
        symbol, dt.datetime.now().strftime("%Y_%m_%d")
    )
    dict_list = _decrypt(get_text(url))
    data_df = pd.DataFrame(dict_list)
    data_df["date"] = pd.to_datetime(data_df["date"], errors="coerce").dt.date
    for item in ("open", "high", "low", "close"):
        data_df[item] = pd.to_numeric(data_df[item], errors="coerce")
    return data_df


def bond_china_yield(
    start_date: str = "20200204", end_date: str = "20210124"
) -> pd.DataFrame:
    """中国债券信息网-国债及其他债券收益率曲线（HTML 表格）。

    注意：``end_date - start_date`` 应小于一年。

    :param start_date: ``YYYYMMDD``
    :param end_date: ``YYYYMMDD``
    """
    params = {
        "startDate": "-".join([start_date[:4], start_date[4:6], start_date[6:]]),
        "endDate": "-".join([end_date[:4], end_date[4:6], end_date[6:]]),
        "gjqx": "0",
        "qxId": "ycqx",
        "locale": "cn_ZH",
    }
    data_text = get_text(CHINABOND_YIELD, params=params, headers=_CONS_HEADERS)
    data_text = data_text.replace("&nbsp", "")
    data_df = pd.read_html(StringIO(data_text), header=0)[1]
    data_df["日期"] = pd.to_datetime(data_df["日期"], errors="coerce").dt.date
    for item in ("3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年"):
        data_df[item] = pd.to_numeric(data_df[item], errors="coerce")
    data_df.sort_values(by="日期", inplace=True)
    data_df.reset_index(inplace=True, drop=True)
    return data_df


# ---------------------------------------------------------------------------
# 外汇（中行牌价）
# ---------------------------------------------------------------------------
def _currency_boc_sina_map(start_date: str, end_date: str) -> dict:
    """外汇中文名 → 新浪 money_code 映射（如 ``美元`` → ``USD``）。"""
    params = {
        "startdate": "-".join([start_date[:4], start_date[4:6], start_date[6:]]),
        "enddate": "-".join([end_date[:4], end_date[4:6], end_date[6:]]),
        "money_code": "EUR",
        "type": "0",
    }
    r_text = get_text(SINA_FOREX, params=params, encoding="gbk")
    soup = BeautifulSoup(r_text, "lxml")
    container = soup.find(attrs={"id": "money_code"})
    options = container.find_all("option")
    return dict(zip([item.text for item in options], [item["value"] for item in options]))


def currency_boc_sina(
    symbol: str = "美元", start_date: str = "20230304", end_date: str = "20231110"
) -> pd.DataFrame:
    """新浪财经-中行人民币牌价历史数据查询。

    :param symbol: 货币中文名，如 ``美元``/``欧元``/``港币``
    :param start_date: ``YYYYMMDD``
    :param end_date: ``YYYYMMDD``
    """
    data_dict = _currency_boc_sina_map(start_date=start_date, end_date=end_date)
    params = {
        "money_code": data_dict[symbol],
        "type": "0",
        "startdate": "-".join([start_date[:4], start_date[4:6], start_date[6:]]),
        "enddate": "-".join([end_date[:4], end_date[4:6], end_date[6:]]),
        "page": "1",
        "call_type": "ajax",
    }
    r_text = get_text(SINA_FOREX, params=params, encoding="gbk")
    soup = BeautifulSoup(r_text, features="lxml")
    page_element_list = soup.find_all("a", attrs={"class": "page"})
    page_num = int(page_element_list[-2].text) if len(page_element_list) != 0 else 1
    big_df = pd.DataFrame()
    for page in range(1, page_num + 1):
        params.update({"page": page})
        r_text = get_text(SINA_FOREX, params=params, encoding="gbk")
        temp_df = pd.read_html(StringIO(r_text), header=0)[0]
        big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
    big_df.columns = [
        "日期",
        "中行汇买价",
        "中行钞买价",
        "中行钞卖价/汇卖价",
        "央行中间价",
        "中行折算价",
    ]
    big_df["日期"] = pd.to_datetime(big_df["日期"], errors="coerce").dt.date
    for item in big_df.columns[1:]:
        big_df[item] = pd.to_numeric(big_df[item], errors="coerce")
    return big_df.sort_values(by=["日期"], ignore_index=True)


# ---------------------------------------------------------------------------
# A 股 / 指数日线
# ---------------------------------------------------------------------------
def stock_zh_a_daily(
    symbol: str = "sh603843",
    start_date: str = "19900101",
    end_date: str = "21000118",
    adjust: str = "",
) -> pd.DataFrame:
    """新浪财经-A 股-个股历史行情数据。

    :param symbol: ``sh600000``
    :param start_date: ``YYYYMMDD``
    :param end_date: ``YYYYMMDD``
    :param adjust: ``""`` 不复权；``qfq`` 前复权；``hfq`` 后复权；
        ``hfq-factor``/``qfq-factor`` 只返回复权因子
    """

    def _fq_factor(method: str) -> pd.DataFrame:
        url = (SINA_A_STOCK_HFQ if method == "hfq" else SINA_A_STOCK_QFQ).format(symbol)
        text = get_text(url)
        payload = _loads(text.split("=", 1)[1].split("\n")[0])
        factor_df = pd.DataFrame(payload["data"])
        if factor_df.shape[0] == 0:
            raise ValueError("sina hfq factor not available")
        factor_df.columns = ["date", "hfq_factor" if method == "hfq" else "qfq_factor"]
        factor_df["date"] = pd.to_datetime(factor_df["date"], errors="coerce")
        factor_col = "hfq_factor" if method == "hfq" else "qfq_factor"
        factor_df[factor_col] = pd.to_numeric(factor_df[factor_col], errors="coerce")
        factor_df = factor_df.dropna(subset=["date", factor_col])
        factor_df = factor_df.set_index("date")
        factor_df.index.name = "date"
        return factor_df

    if adjust in ("hfq-factor", "qfq-factor"):
        return _fq_factor(adjust.split("-")[0]).reset_index()

    dict_list = _klc_frames(symbol)
    data_df = pd.DataFrame(dict_list)
    data_df.index = pd.to_datetime(data_df["date"], errors="coerce").dt.date
    del data_df["date"]
    try:
        del data_df["prevclose"]
    except KeyError:
        pass
    try:
        del data_df["postVol"]
        del data_df["postAmt"]
    except KeyError:
        pass
    data_df = data_df.astype("float")

    amount_text = get_text(SINA_A_STOCK_AMOUNT.format(symbol, symbol))
    amount_json = _loads(amount_text[amount_text.find("[") : amount_text.rfind("]") + 1])
    amount_df = pd.DataFrame(amount_json)
    amount_df.columns = ["date", "outstanding_share"]
    amount_df.index = pd.to_datetime(amount_df.date)
    del amount_df["date"]

    temp_df = pd.merge(
        data_df, amount_df, left_index=True, right_index=True, how="outer"
    )
    temp_df.ffill(inplace=True)
    temp_df = temp_df.astype(float)
    temp_df["outstanding_share"] = temp_df["outstanding_share"] * 10000
    temp_df["turnover"] = temp_df["volume"] / temp_df["outstanding_share"]
    temp_df.columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "outstanding_share",
        "turnover",
    ]

    if adjust == "":
        temp_df = temp_df[start_date:end_date]
        temp_df.drop_duplicates(
            subset=["open", "high", "low", "close", "volume", "amount"], inplace=True
        )
        for item in ("open", "high", "low", "close"):
            temp_df[item] = round(temp_df[item], 2)
        temp_df.dropna(inplace=True)
        temp_df.drop_duplicates(inplace=True)
        temp_df.reset_index(inplace=True)
        temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
        return temp_df

    if adjust in ("hfq", "qfq"):
        factor_df = _fq_factor(adjust)
        # 日期索引统一成 DatetimeIndex 再 merge（temp_df 原来是 datetime.date 对象索引，
        # 与日期索引的因子表按位置错配）；pandas 3 对含 datetime 列的整表 astype(float)
        # 会抛 TypeError: Cannot cast DatetimeArray to dtype float64，故 merge 后只保留数值列。
        temp_df.index = pd.to_datetime(temp_df.index)
        temp_df.index.name = "date"
        temp_df = pd.merge(
            temp_df, factor_df, left_index=True, right_index=True, how="outer"
        )
        temp_df.ffill(inplace=True)
        num_cols = [
            c for c in temp_df.columns
            if pd.api.types.is_numeric_dtype(temp_df[c])
        ]
        temp_df = temp_df[num_cols].astype(float)
        temp_df.dropna(inplace=True)
        temp_df.drop_duplicates(
            subset=["open", "high", "low", "close", "volume", "amount"], inplace=True
        )
        factor_col = "hfq_factor" if adjust == "hfq" else "qfq_factor"
        op = "__mul__" if adjust == "hfq" else "__truediv__"
        for item in ("open", "high", "low", "close"):
            temp_df[item] = getattr(temp_df[item], op)(temp_df[factor_col])
        temp_df = temp_df.iloc[:, :-1]
        temp_df = temp_df[start_date:end_date]
        for item in ("open", "high", "low", "close"):
            temp_df[item] = round(temp_df[item], 2)
        temp_df.dropna(inplace=True)
        temp_df.reset_index(inplace=True)
        temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
        return temp_df
    return pd.DataFrame()


def stock_zh_index_daily(symbol: str = "sh000922") -> pd.DataFrame:
    """新浪财经-指数-历史行情数据。

    :param symbol: 指数代码，如 ``sh000300``
    :return: 列 ``date/open/high/low/close/volume``
    """
    dict_list = _klc_frames(symbol, SINA_HIST_KLC, params={"d": "2020_2_4"})
    temp_df = pd.DataFrame(dict_list)
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
    for item in ("open", "close", "high", "low", "volume"):
        temp_df[item] = pd.to_numeric(temp_df[item], errors="coerce")
    return temp_df


# ---------------------------------------------------------------------------
# 板块
# ---------------------------------------------------------------------------
_SECTOR_COLUMNS = [
    "label",
    "板块",
    "公司家数",
    "平均价格",
    "涨跌额",
    "涨跌幅",
    "总成交量",
    "总成交额",
    "股票代码",
    "个股-涨跌幅",
    "个股-当前价",
    "个股-涨跌额",
    "股票名称",
]
_SECTOR_NUMERIC = [
    "公司家数",
    "平均价格",
    "涨跌额",
    "涨跌幅",
    "总成交量",
    "总成交额",
    "个股-涨跌幅",
    "个股-当前价",
    "个股-涨跌额",
]


def stock_sector_spot(indicator: str = "新浪行业") -> pd.DataFrame:
    """新浪财经-板块行情（板块层面）。

    :param indicator: ``新浪行业`` / ``启明星行业`` / ``概念`` / ``地域`` / ``行业``
    :return: ``label`` 列即 :func:`stock_sector_detail` 的 ``sector`` 入参
    """
    params = None
    headers = None
    if indicator == "新浪行业":
        url = SINA_SECTOR_INDUSTRY
    elif indicator == "启明星行业":
        url = SINA_SECTOR_QMX
        headers = {"Accept-Charset": "gb2312"}
    elif indicator == "概念":
        url = SINA_SECTOR_CLASS
        params = {"param": "class"}
    elif indicator == "地域":
        url = SINA_SECTOR_CLASS
        params = {"param": "area"}
    elif indicator == "行业":
        url = SINA_SECTOR_CLASS
        params = {"param": "industry"}
    else:
        raise ValueError(f"不支持的 indicator：{indicator}")

    text_data = get_text(url, params=params, headers=headers, encoding="gb2312" if indicator == "启明星行业" else None)
    json_data = json.loads(text_data[text_data.find("{") :])
    temp_df = pd.DataFrame([value.split(",") for key, value in json_data.items()])
    temp_df.columns = _SECTOR_COLUMNS
    for item in _SECTOR_NUMERIC:
        temp_df[item] = pd.to_numeric(temp_df[item])
    return temp_df


def stock_sector_detail(sector: str = "gn_gfgn") -> pd.DataFrame:
    """新浪财经-板块行情-成份详情。

    :param sector: :func:`stock_sector_spot` 返回的 ``label`` 值
    """
    count_url = SINA_MARKET_CENTER.format("getHQNodeStockCount")
    total_num = int(get_json(count_url, params={"node": sector}))
    total_page_num = math.ceil(total_num / 80)
    data_url = SINA_MARKET_CENTER.format("getHQNodeData")
    big_df = pd.DataFrame()
    for page in range(1, total_page_num + 1):
        params = {
            "page": str(page),
            "num": "80",
            "sort": "symbol",
            "asc": "1",
            "node": sector,
            "symbol": "",
            "_s_r_a": "page",
        }
        temp_df = pd.DataFrame(_loads(get_text(data_url, params=params)))
        big_df = pd.concat([big_df, temp_df], ignore_index=True)
    for item in (
        "trade",
        "pricechange",
        "changepercent",
        "buy",
        "sell",
        "settlement",
        "open",
        "high",
        "low",
        "volume",
        "amount",
        "per",
        "pb",
        "mktcap",
        "nmc",
        "turnoverratio",
    ):
        big_df[item] = pd.to_numeric(big_df[item], errors="coerce")
    return big_df


# ---------------------------------------------------------------------------
# 快讯 / 交易日历
# ---------------------------------------------------------------------------
def stock_info_global_sina() -> pd.DataFrame:
    """新浪财经-全球财经快讯。

    :return: 列 ``时间`` / ``内容``
    """
    params = {
        "page": "1",
        "page_size": "20",
        "zhibo_id": "152",
        "tag_id": "0",
        "dire": "f",
        "dpc": "1",
        "pagesize": "20",
        "type": "1",
    }
    data_json = get_json(SINA_GLOBAL_NEWS, params=params)
    feed_list = data_json["result"]["data"]["feed"]["list"]
    time_list = [item["create_time"] for item in feed_list]
    text_list = [item["rich_text"] for item in feed_list]
    temp_df = pd.DataFrame([time_list, text_list]).T
    temp_df.columns = ["时间", "内容"]
    return temp_df


def tool_trade_date_hist_sina() -> pd.DataFrame:
    """新浪财经-交易日历-历史数据。

    :return: 列 ``trade_date``（``datetime.date`` 对象）
    """
    dict_list = _decrypt(get_text(SINA_TRADE_DATE))
    temp_df = pd.DataFrame(dict_list)
    temp_df.columns = ["trade_date"]
    temp_df["trade_date"] = pd.to_datetime(temp_df["trade_date"]).dt.date
    temp_list = temp_df["trade_date"].to_list()
    # 该日期是交易日，但新浪返回的交易日历里缺失，这里补上
    temp_list.append(dt.date(year=1992, month=5, day=4))
    temp_list.sort()
    return pd.DataFrame(temp_list, columns=["trade_date"])


# ---------------------------------------------------------------------------
# 财务
# ---------------------------------------------------------------------------
_ABSTRACT_SECTIONS = (
    ("常用指标", "每股指标"),
    ("每股指标", "盈利能力"),
    ("盈利能力", "成长能力"),
    ("成长能力", "收益质量"),
    ("收益质量", "财务风险"),
    ("财务风险", "营运能力"),
    ("营运能力", None),
)


def stock_financial_abstract(symbol: str = "600004") -> pd.DataFrame:
    """新浪财经-财务报表-关键指标。

    :param symbol: 股票代码，如 ``600519``
    """
    params = {
        "paperCode": f"sh{symbol}",
        "source": "gjzb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    data_json = get_json(SINA_FINANCE_ABSTRACT, params=params)
    report_list = data_json["result"]["data"]["report_list"]
    key_list = list(report_list.keys())
    temp_df = pd.DataFrame(report_list[key_list[0]]["data"])
    big_df = temp_df["item_title"]
    for item in key_list:
        temp_df = pd.DataFrame(report_list[item]["data"])
        big_df = pd.concat(
            objs=[big_df, temp_df["item_value"]], axis=1, ignore_index=True
        )
    big_df.index = big_df.iloc[:, 0]
    big_df = big_df.iloc[:, 1:]

    part_list = []
    for left, right in _ABSTRACT_SECTIONS:
        if right is None:
            part_df = big_df.loc[left:]
        else:
            part_df = big_df.loc[left:right]
        part_df = part_df.iloc[1:-1, :]
        part_df.reset_index(inplace=True)
        part_df.insert(0, "选项", left)
        part_list.append(part_df)
    big_df = pd.concat(objs=part_list, ignore_index=True)
    key_list.insert(0, "选项")
    key_list.insert(1, "指标")
    big_df.columns = key_list
    for item in big_df.columns[2:]:
        big_df[item] = pd.to_numeric(big_df[item], errors="coerce")
    return big_df


_INDICATOR_SECTIONS = (
    "每股指标",
    "盈利能力",
    "成长能力",
    "营运能力",
    "偿债及资本结构",
    "现金流量",
    "其他指标",
)


def _fin_year_list(symbol: str, start_year: str = "1900") -> list[str]:
    """财务指标页的可用年份列表（已按 start_year 过滤；页面结构变化/无年份 → []）。"""
    first_url = SINA_FIN_GUIDELINE.format(symbol, "2020")
    html = get_text(first_url, headers=_CHROME_HEADERS)
    soup = BeautifulSoup(html, "lxml")
    year_container = soup.find(attrs={"id": "con02-1"})
    if year_container is None or year_container.find("table") is None:
        return []
    year_context = year_container.find("table").find_all("a")
    year_list = [item.text.strip() for item in year_context if item.text.strip()]
    if not year_list:
        return []
    if start_year.isdigit():
        # 默认 start_year="1900" 表示尽可能获取全部可用年份数据
        year_list = [item for item in year_list if item.isdigit() and item >= start_year]
    elif start_year in year_list:
        year_list = year_list[: year_list.index(start_year) + 1]
    else:
        return []
    return year_list


def _fin_year_page(symbol: str, year_item: str) -> pd.DataFrame:
    """单年财务指标页：逐节解析成「index=报告期字符串、列=指标名」的**原始字符串**表。

    不做 dropna / 日期归一化 / 数值化——这些是全量拼接后的统一收尾（见
    :func:`stock_financial_analysis_indicator`）。单独暴露出来是为了让数据层做
    「本年+上年两页增量」，与全量口径逐值一致（2026-11-17 实测抽样对比通过）。
    """
    html = get_text(SINA_FIN_GUIDELINE.format(symbol, year_item), headers=_CHROME_HEADERS)
    temp_df = pd.read_html(StringIO(html))[12].iloc[:, :-1]
    temp_df.columns = temp_df.iloc[0, :]
    temp_df = temp_df.iloc[1:, :]
    year_df = pd.DataFrame()
    for i, section in enumerate(_INDICATOR_SECTIONS):
        start = temp_df.loc[temp_df.iloc[:, 0].str.find(section) == 0, :].index[0]
        if i == len(_INDICATOR_SECTIONS) - 1:
            inner_df = temp_df[start:].T
        else:
            end = temp_df.loc[
                temp_df.iloc[:, 0].str.find(_INDICATOR_SECTIONS[i + 1]) == 0, :
            ].index[0]
            inner_df = temp_df[start : end - 1].T
        inner_df = inner_df.reset_index(drop=True)
        year_df = pd.concat(objs=[year_df, inner_df], axis=1)
    year_df.columns = year_df.iloc[0, :].tolist()
    year_df = year_df.iloc[1:, :]
    year_df.index = temp_df.columns.tolist()[1:]
    return year_df


def stock_financial_analysis_indicator_years(symbol: str, years) -> pd.DataFrame:
    """按指定年份抓多页并纵向拼接（原始字符串，未 dropna/未归一化）。

    给数据层做跨日增量复用用：只抓「本年 + 上年」两页，往年行复用上一份缓存。
    单页解析完全复用 :func:`_fin_year_page`，因此与全量抓所有年份的口径一致。
    """
    frames = []
    for y in years:
        page = _fin_year_page(symbol, str(y))
        if page is None or page.empty:
            continue
        frames.append(page)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def stock_financial_analysis_indicator(
    symbol: str = "600004", start_year: str = "1900"
) -> pd.DataFrame:
    """新浪财经-财务分析-财务指标（HTML 表格，按年份逐页解析）。

    :param symbol: 股票代码，如 ``600519``
    :param start_year: 开始年份；默认 ``1900`` 表示取全部可用年份
    """
    year_list = _fin_year_list(symbol, start_year)
    if not year_list:
        return pd.DataFrame()

    out_df = stock_financial_analysis_indicator_years(symbol, year_list)
    out_df.dropna(inplace=True)
    out_df.reset_index(inplace=True)
    out_df.rename(columns={"index": "日期"}, inplace=True)
    out_df.sort_values(by=["日期"], ignore_index=True, inplace=True)
    out_df["日期"] = pd.to_datetime(out_df["日期"], errors="coerce").dt.date
    for item in out_df.columns[1:]:
        out_df[item] = pd.to_numeric(out_df[item], errors="coerce")
    return out_df


# ---------------------------------------------------------------------------
# 基金 / ETF
# ---------------------------------------------------------------------------
_FUND_MAP = {
    "封闭式基金": "close_fund",
    "ETF基金": "etf_hq_fund",
    "LOF基金": "lof_hq_fund",
}
_FUND_COLUMNS = [
    "代码",
    "名称",
    "最新价",
    "涨跌额",
    "涨跌幅",
    "买入",
    "卖出",
    "昨收",
    "今开",
    "最高",
    "最低",
    "成交量",
    "成交额",
]
_FUND_RAW_COLUMNS_CLOSE = _FUND_COLUMNS + ["_", "_"]
_FUND_RAW_COLUMNS_OTHER = _FUND_COLUMNS + ["_", "_", "_", "_"]


def fund_etf_category_sina(symbol: str = "LOF基金") -> pd.DataFrame:
    """新浪财经-基金列表（封闭式/ETF/LOF）。

    :param symbol: ``封闭式基金`` / ``ETF基金`` / ``LOF基金``
    """
    params = {
        "page": "1",
        "num": "5000",
        "sort": "symbol",
        "asc": "0",
        "node": _FUND_MAP[symbol],
        "[object HTMLDivElement]": "qvvne",
    }
    data_text = get_text(SINA_FUND_LIST, params=params)
    data_json = _loads(data_text[data_text.find("([") + 1 : -2])
    temp_df = pd.DataFrame(data_json)
    if symbol == "封闭式基金":
        temp_df.columns = _FUND_RAW_COLUMNS_CLOSE
    else:
        temp_df.columns = _FUND_RAW_COLUMNS_OTHER
    temp_df = temp_df[_FUND_COLUMNS]
    for item in _FUND_COLUMNS[2:]:
        temp_df[item] = pd.to_numeric(temp_df[item], errors="coerce")
    return temp_df


def fund_etf_hist_sina(symbol: str = "sh510050") -> pd.DataFrame:
    """新浪财经-基金-ETF 基金-日行情数据（不复权）。

    :param symbol: 基金代码，如 ``sz159707``
    """
    dict_list = _klc_frames(symbol)
    temp_df = pd.DataFrame(dict_list)
    if temp_df.empty:  # 处理获取数据为空的问题
        return pd.DataFrame()
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.tz_localize(None)
    for item in ("open", "high", "low", "close", "volume"):
        temp_df[item] = pd.to_numeric(temp_df[item], errors="coerce")
    temp_df["date"] = temp_df["date"].dt.date
    return temp_df.sort_values(by="date", ascending=True)


def stock_info_a_code_name() -> pd.DataFrame:
    """新浪财经-沪深京 A 股-股票列表（代码 / 名称）。

    用途：核对「风险警示（ST/*ST）」与「退市整理（简称含「退」）」名单。
    交易所简称本身就带 ST/退 标记，所以按名单排除比逐个看行情更稳、也不会漏掉停牌股。
    全市场约 5,600 只、按 80 条一页约 70 页；返回列固定为 ``code`` / ``name``。
    """
    count_url = SINA_MARKET_CENTER.format("getHQNodeStockCount")
    total_num = int(get_json(count_url, params={"node": "hs_a"}))
    data_url = SINA_MARKET_CENTER.format("getHQNodeData")
    frames = []
    for page in range(1, math.ceil(total_num / 80) + 1):
        params = {
            "page": str(page),
            "num": "80",
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        }
        items = _loads(get_text(data_url, params=params))
        if not items:
            break
        frames.append(pd.DataFrame(items))
    if not frames:
        return pd.DataFrame(columns=["code", "name"])
    temp_df = pd.concat(frames, ignore_index=True)
    temp_df = temp_df[["code", "name"]].copy()
    temp_df["code"] = temp_df["code"].astype(str).str.strip().str.zfill(6)
    temp_df["name"] = temp_df["name"].astype(str).str.replace(" ", "", regex=False)
    temp_df = temp_df.drop_duplicates(subset="code").sort_values("code")
    return temp_df.reset_index(drop=True)