# -*- coding: utf-8 -*-
"""磁盘缓存 + 溯源：取数一次、复用一天，并且记得「这个数字从哪来」。

设计
----
- 键 = `kind` + 参数签名的稳定哈希（json sort_keys + sha1 前 16 位），
  文件名形如 `cache/ak_daily/3f9a...c1.parquet`，方便人眼排查。
- 格式：有 pyarrow 存 parquet（快、保 dtype），否则退 CSV（UTF-8-SIG）。
- TTL：默认 `config.CACHE_TTL_HOURS`。
- 溯源：每次**真实取数**（不是命中缓存）追加一行到 `data/out/provenance.jsonl`，
  字段 `{ts, kind, key, source, rows, extra}`。
- `STOCK_REFRESH=1` 时跳过读取（仍写新缓存）。

注意：pandas 是硬依赖；本模块在 import 时**不联网**。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pandas as pd

from . import config

def prov_path() -> Path:
    """溯源文件路径（每次现算，测试里改 config.OUT_DIR 才生效）。"""
    return Path(config.OUT_DIR) / "provenance.jsonl"


# ---------------------------------------------------------------- 键与路径
def _sig(params) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def path_for(kind: str, params, ext: str = "parquet") -> Path:
    d = Path(config.CACHE_DIR) / str(kind)
    return d / f"{_sig(params)}.{ext}"


def describe(kind: str, params) -> str:
    """人读的键描述，写进溯源。"""
    return f"{kind}:{json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)}"


# ---------------------------------------------------------------- 读写
def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


_PQ = _has_pyarrow()


def read_frame(kind: str, params) -> pd.DataFrame | None:
    if config.force_refresh():
        return None
    p = path_for(kind, params, "parquet" if _PQ else "csv")
    try:
        if not p.exists():
            return None
        age_h = (time.time() - p.stat().st_mtime) / 3600.0
        if age_h > config.CACHE_TTL_HOURS:
            return None
        if _PQ:
            return pd.read_parquet(p)
        return pd.read_csv(p)
    except Exception:
        return None


def write_frame(kind: str, params, df: pd.DataFrame) -> Path:
    config.ensure_dirs()
    ext = "parquet" if _PQ else "csv"
    p = path_for(kind, params, ext)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        if _PQ:
            df.to_parquet(tmp, index=False)
        else:
            df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return p


# ---------------------------------------------------------------- 溯源
def record(source: str, kind: str, params, rows: int | None = None, **extra) -> None:
    """每次真实取数写一行溯源；缓存命中不写（避免把命中记成新取数）。"""
    config.ensure_dirs()
    p = prov_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
           "kind": kind, "key": describe(kind, params), "rows": rows}
    if extra:
        row["extra"] = extra
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- 组合入口
def frame(kind: str, params, loader, source: str = "", post=None) -> pd.DataFrame:
    """取 DataFrame 的统一入口：缓存命中 → 直接返回；未命中 → loader() 取数 + 规范化 + 落盘。

    这里不吞异常——**fail loud**，由上层 api.py 决定是否换源。
    """
    hit = read_frame(kind, params)
    if hit is not None:
        hit.attrs.setdefault("source", "cache")
        hit.attrs.setdefault("cached", True)
        return hit
    df = loader()
    if post is not None:
        df = post(df)
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    write_frame(kind, params, df)
    record(source or kind, kind, params, len(df))
    df.attrs["source"] = source or kind
    df.attrs["cached"] = False
    df.attrs["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return df


# ---------------------------------------------------------------- 维护
def stats() -> dict:
    root = Path(config.CACHE_DIR)
    n, size = 0, 0
    if root.exists():
        for p in root.rglob("*"):
            if p.is_file():
                n += 1
                size += p.stat().st_size
    return {"files": n, "mb": round(size / 1048576.0, 2), "root": str(root)}


def clear(kind: str | None = None) -> int:
    root = Path(config.CACHE_DIR) / kind if kind else Path(config.CACHE_DIR)
    n = 0
    if root.exists():
        for p in root.rglob("*"):
            if p.is_file():
                p.unlink()
                n += 1
    return n
