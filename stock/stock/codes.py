# -*- coding: utf-8 -*-
"""代码规范化：A 股/ETF/指数/基金在一个函数里互转。

为什么需要它
------------
同一只标的在不同数据源里的写法完全不一样：
`600519` / `sh600519` / `1.600519`（东财 secid）/ `600519.SH`（同花顺 thscode）/
`sh.600519`（baostock）。散落各处的拼接是 bug 温床，集中到这里。

口径
----
- `parse("600519")` → {"symbol": "600519", "market": "sh", "kind": "stock"}
- 6 开头 = 沪市、0/3 开头 = 深市、4/8/920 开头 = 北交所、1/5 开头 = 沪市基金/ETF、15/16 开头 = 深市基金。
  注意 900xxx 是沪 B（sh），920xxx 是北交所 2025 新段（bj），不能把 9 一律当沪市。
- 指数：`000300`（沪深300）等 000 开头在**沪市指数**口径；`399xxx` 深证指数；
  指数与「深市股票 000001」冲突，用 `kind="index"` 显式声明。
"""
from __future__ import annotations

import re
from typing import Iterable

_STOCK_PREFIX = {
    "6": "sh", "9": "sh",
    "0": "sz", "2": "sz", "3": "sz",
    "4": "bj", "8": "bj",
}
_INDEX_SH = ("000", "950", "930")
_INDEX_SZ = ("399",)
_RE_DIGITS = re.compile(r"\d{6}")


class CodeError(ValueError):
    """代码不合法（长度/字符不对）。"""


def parse(code: str, kind: str | None = None) -> dict:
    """把任意写法解析成 {symbol, market, kind}。

    kind: "stock" | "index" | "fund"（不传时按前缀猜，指数请显式传）
    """
    if code is None:
        raise CodeError("code 为空")
    raw = str(code).strip()
    if not raw:
        raise CodeError("code 为空")
    m = _RE_DIGITS.search(raw)
    if m:
        sym = m.group(0)
    else:
        # 短代码（如 "82" / "9"）按纯数字补零到 6 位（2026-11-26：fuyao.thscode 隐藏红）
        _digits = re.sub(r"\D", "", raw)
        if _digits and len(_digits) <= 4:
            sym = _digits.zfill(6)
        else:
            raise CodeError(f"不认识代码：{code!r}")
    up = raw.upper()
    market = None
    # 显式前缀：sh600519 / sz000001 / 600519.SH / sh.600519
    head = up.split(".")[0].lower()
    if head in ("sh", "ss", "sse"):
        market = "sh"
    elif head in ("sz", "sze"):
        market = "sz"
    elif head in ("bj", "bse"):
        market = "bj"
    elif up.endswith((".SH", ".SS")):
        market = "sh"
    elif up.endswith(".SZ"):
        market = "sz"
    elif up.endswith(".BJ"):
        market = "bj"
    if market is None and sym.startswith("920"):
        market = "bj"       # 北交所 2025 新代码段（900 才是沪 B，9 开头不能一律判沪）
    if kind is None:
        if market is None:
            if sym.startswith(_INDEX_SH) or sym.startswith(_INDEX_SZ):
                kind = "stock"      # 000001 默认是平安银行；指数请显式声明
            else:
                kind = "stock"
    if kind == "index":
        if market is None:
            market = "sz" if sym.startswith(_INDEX_SZ) else "sh"
        return {"symbol": sym, "market": market, "kind": "index"}
    if kind == "fund":
        if market is None:
            market = "sh" if sym[:2] in ("50", "51", "52", "56", "58") else "sz"
        return {"symbol": sym, "market": market, "kind": "fund"}
    if market is None:
        if sym.startswith(("15", "16", "18")):      # 深市 ETF/LOF
            market = "sz"
        elif sym[0] == "5":                          # 沪市 ETF/LOF
            market = "sh"
        else:
            market = _STOCK_PREFIX.get(sym[0], "")
    if not market:
        raise CodeError(f"无法判断市场：{code!r}")
    return {"symbol": sym, "market": market, "kind": "stock"}


def to_sina(code: str, kind: str | None = None) -> str:
    """`sh600519`：新浪/腾讯行情口径。"""
    d = parse(code, kind)
    return f"{d['market']}{d['symbol']}"


def to_em_secid(code: str, kind: str | None = None) -> str:
    """东财 secid：`1.600519`（1=沪、0=深/北）。"""
    d = parse(code, kind)
    mkt = 1 if d["market"] in ("sh",) else 0
    return f"{mkt}.{d['symbol']}"


def to_thscode(code: str, kind: str | None = None) -> str:
    """同花顺 thscode：`600519.SH`。"""
    d = parse(code, kind)
    return f"{d['symbol']}.{d['market'].upper()}"


def bare(code: str) -> str:
    """只要 6 位数字。"""
    return parse(code)["symbol"]


def normalize(codes: Iterable[str] | str, kind: str | None = None) -> list[str]:
    """批量规范化成 `sh600519` 形式，保序去重。"""
    seq = [codes] if isinstance(codes, str) else list(codes)
    out, seen = [], set()
    for c in seq:
        v = to_sina(c, kind)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------- 技能侧口径（唯一实现）
# invest 的代码归一、ETF 判定统一走这里，调用方直接 import 本模块的常量与函数。
_PREFIX2 = ("sh", "sz", "bj")
_SUFFIX2 = (".SH", ".SZ", ".BJ", ".SS")

# 场内 ETF 代码段（全技能唯一来源；datasource 的 ETF 全表过滤复用同一元组）
ETF_PREFIXES = ("15", "16", "51", "52", "56", "58", "59")


def norm_code(code, strict: bool = False) -> str:
    """A 股/ETF 代码 → 6 位数字串；非数字输入原样返回（strict 时返回空串）。

    接受 `600519` / `600519.SH` / `600519.SS` / `sh600519` / `SH600519` /
    `sh.600519`（baostock）/ `600519.0`（浮点串）/ `430047.BJ` / `bj430047`。
    """
    s = str(code if code is not None else "").strip()
    if not s:
        return ""
    low = s.lower()
    for p in _PREFIX2:
        if low.startswith(p):
            s = s[len(p):]
            break
    s = s.strip()
    if s.startswith("."):          # sh.600519 → .600519
        s = s[1:]
    up = s.upper()
    for suf in _SUFFIX2:
        if up.endswith(suf):
            s = s[: -len(suf)]
            break
    if s.endswith(".0") and s[:-2].isdigit():   # 600519.0（Excel/CSV 浮点化）
        s = s[:-2]
    s = s.strip()
    if s.isdigit():
        return s.zfill(6)
    return "" if strict else s


def norm_fund_code(code) -> str:
    """场外基金代码 → 6 位数字串（`018074.OF` / `of018074` → `018074`）。"""
    s = str(code if code is not None else "").strip().upper()
    if s.endswith(".OF"):
        s = s[:-3]
    if s.startswith("OF"):
        s = s[2:]
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6) if s else ""


def is_etf_code(code) -> bool:
    """场内 ETF 代码：沪市 51/52/56/58/59 开头，深市 15/16 开头（见 ETF_PREFIXES）。

    注意：1 开头的还有深市可转债（12x）与国债逆回购（131），
    所以只认 15/16 开头的深市基金，不泛化到所有 1 开头。
    """
    c = norm_code(code)
    if len(c) != 6:
        return False
    return c.startswith(ETF_PREFIXES)


def etf_symbol(code) -> str:
    """场内 ETF 代码 → 新浪代码（sh510300 / sz159915）。"""
    c = norm_code(code)
    return ("sh" if c.startswith("5") else "sz") + c
