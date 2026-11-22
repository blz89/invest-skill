# -*- coding: utf-8 -*-
"""HTTP 基础设施：统一 UA / 超时 / 退避重试 / 单域名限流。

三条纪律（与 invest 技能一致）
------------------------------
1. **fail loud**：重试耗尽后抛 `HttpError`，不返回假数据、不吞异常。
2. **不打死对方**：同一域名最小间隔 `config.MIN_INTERVAL_S`（默认 0.2s），
   东财/新浪这类免费接口被限流后整条链路都会挂。
3. **可注入**：`transport` 参数允许测试替换真实网络（离线单测用）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import config


class HttpError(RuntimeError):
    """网络或服务端错误（重试后仍失败）。"""


class _Fatal(HttpError):
    """不可重试的错误（4xx：参数错/权限错，重试没有意义）。"""


_last_hit: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_host_locks_guard = threading.Lock()


def _host_lock(host: str) -> threading.Lock:
    """每个域名一把锁（惰性创建）：限流只需同域名互斥，不同域名仍可并行。"""
    with _host_locks_guard:
        lock = _host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _host_locks[host] = lock
        return lock


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc
    except Exception:
        return url


def throttle(host: str) -> None:
    """同域名串行限流：不足最小间隔就补 sleep。

    并发（prefetch --workers 8）下多个线程可能同时进入：原来 read-then-sleep 没有
    互斥，大家都读到旧时间戳、都只睡一小段就一起发车，实际间隔被压到 0（bug-io
    实测 48 请求只用了 1.204s，串行应为 9.4s）。持锁到写回时间戳为止，保证同一
    域名相邻两次请求至少间隔 MIN_INTERVAL_S。
    """
    gap = config.MIN_INTERVAL_S
    with _host_lock(host):
        now = time.time()
        wait = gap - (now - _last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()


def _decode(raw: bytes, encoding: str | None) -> str:
    for enc in ([encoding] if encoding else []) + ["utf-8", "gbk"]:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def request(url: str, params: dict | None = None, headers: dict | None = None,
            timeout: float | None = None, retries: int | None = None,
            transport: Callable | None = None, encoding: str | None = None) -> bytes:
    """GET 一个 URL 并返回原始字节，带限流 + 指数退避重试。

    `transport` 可注入（离线测试用），签名 `transport(url, headers, timeout)`，
    返回 `bytes | str`，或 `(status, body)` 二元组（body 为 bytes/str）——
    返回元组时按 status 判断：4xx 立即失败、5xx 重试。
    """
    if params:
        sep = "&" if urllib.parse.urlsplit(url).query else "?"
        url = url + sep + urllib.parse.urlencode(params, doseq=True)
    hdrs = {"User-Agent": config.UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    host = _host_of(url)
    tries = config.RETRIES if retries is None else retries
    last_exc: Exception | None = None

    def _as_bytes(body) -> bytes:
        if isinstance(body, bytes):
            return body
        return str(body).encode(encoding or "utf-8")

    for i in range(max(1, tries)):
        throttle(host)
        try:
            if transport is not None:
                out = transport(url, hdrs, timeout or config.TIMEOUT_S)
                if isinstance(out, tuple) and len(out) == 2:
                    status, body = int(out[0]), out[1]
                    if 400 <= status < 500:
                        raise _Fatal(f"HTTP {status} {url}")
                    if status >= 500:
                        raise HttpError(f"HTTP {status} {url}")
                    return _as_bytes(body)
                return _as_bytes(out)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout or config.TIMEOUT_S) as r:
                return r.read()
        except urllib.error.HTTPError as e:      # 4xx 不重试（参数/权限错，重试没用）
            if 400 <= e.code < 500:
                raise HttpError(f"HTTP {e.code} {url}") from e
            last_exc = e
        except _Fatal:
            raise
        except Exception as e:                    # 网络抖动 / 5xx / 超时
            last_exc = e
        if i < tries - 1:
            time.sleep(1.0 + 1.5 * i)
    raise HttpError(f"取数失败（{tries} 次）：{url}｜{type(last_exc).__name__}: {last_exc}")


def get_text(url: str, **kw) -> str:
    return _decode(request(url, **kw), kw.get("encoding"))


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             **kw) -> dict | list:
    """东财/新浪常见的 JSONP 包装在这里剥掉（`cb(...)` → `obj`）。"""
    raw = _decode(request(url, params=params, headers=headers, **kw), kw.get("encoding"))
    # 有的接口/中间代理会带 UTF-8 BOM：str.strip() 不去 \ufeff，会整段解析失败
    text = raw.lstrip("\ufeff").strip()
    if not text:
        raise HttpError(f"空响应：{url}")
    if text[0] not in "[{":
        i, j = text.find("("), text.rfind(")")
        if i >= 0 and j > i:
            text = text[i + 1:j]
    try:
        return json.loads(text)
    except Exception as e:
        raise HttpError(f"JSON 解析失败：{url}｜{text[:120]}") from e


def get_jsonp(url: str, params: dict | None = None, **kw):
    """显式 JSONP 版本（有些接口必须带 callback 参数才返回，内容照样解析）。"""
    return get_json(url, params=params, **kw)
