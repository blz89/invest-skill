# -*- coding: utf-8 -*-
"""运行数据保留期清理：默认只留近 90 天（约三个月）「跑出来的数据」。

清理什么
--------
- `cache/`：按日期命名的取数缓存（日线 CSV/parquet、货基快照等）——过期即无用。
- `data\\shared\\cache/`：两技能共用的统一取数缓存——按文件日期留 `SHARED_CACHE_RETENTION_DAYS`
  （默认 7 天；一天约 200MB，留 90 天会白占十几 GB）。
- `data\\shared\\snapshots/`：每次全量取数的统一记录（manifest/summary，按日目录）——留 90 天，
  保证「那天的取数记录」可回看，同时避免无界增长。
- `out/`：报告、排行、巡检产物——过期即历史。
- `logs/`：巡检日志（patrol_*.log、*.out.log、*.err.log）。
- `archive/`：按日期归档的整个目录（`<YYYY-MM-DD>/`）。

绝对不删什么
------------
- 任何 `paper/` 目录下的文件：账本（state_50k.json）、持仓、成交历史，是账户记录。
- `*.key` / `*.env` / `.secrets/`：凭据。
- 工作目录根下的单个状态文件（cash_rates_live.json、fuyao.key 等）。
- `state*.json`、`*_latest.json`：下一次运行要读的「当前状态」，不是历史。

安全设计
--------
- 按 **mtime** 判新旧：正在写的日志 mtime 新鲜，不会被误删。
- 空目录也一起删（archive 的子目录、cache 的分区目录）。
- `dry=True` 只统计不删；CLI 默认先 `--dry` 看一眼。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config

# 整棵保留的目录名（赚了钱的那个「账本」）
KEEP_DIR_NAMES = {"paper", ".secrets", ".git"}
# 凭据/状态文件特征
KEEP_SUFFIXES = (".key", ".env")
KEEP_NAME_PREFIXES = ("state",)      # state_50k.json、state.json
KEEP_NAME_SUFFIXES = ("_latest.json", ".lastgood.json")
KEEP_EXACT_NAMES = {"etf_scale_lastgood.json", "offpeak_holidays.json"}


def _keep(path: Path) -> bool:
    low = path.name.lower()
    if any(part in KEEP_DIR_NAMES for part in path.parts):
        return True
    if low in KEEP_EXACT_NAMES:
        return True
    if low.endswith(KEEP_SUFFIXES):
        return True
    if any(low.startswith(p) for p in KEEP_NAME_PREFIXES):
        return True
    if low.endswith(KEEP_NAME_SUFFIXES):
        return True
    return False


def _resolved_key(path: Path) -> str:
    """用于去重的真实路径：技能 cache 是指向 shared\\cache 的 junction，解析后同一棵树。"""
    try:
        return str(Path(path).resolve(strict=False)).lower()
    except OSError:
        return str(Path(path)).lower()


def targets() -> list[Path]:
    """参与保留期清理的根目录（不存在就跳过；junction 解析后去重）。

    含 `data\\shared\\snapshots`：统一取数记录按默认 90 天保留（manifest 每天约 0.1–2.4MB，
    一年会到几百 MB，必须进清理，否则无界增长）。
    """
    out = [
        Path(config.CACHE_DIR),
        Path(config.SHARED_CACHE_DIR),
        Path(config.SNAPSHOTS_DIR),
        Path(config.INVEST_DIR) / "cache",
        Path(config.OUT_DIR),
        Path(config.INVEST_DIR) / "out",
        Path(config.LOGS_DIR),
    ]
    seen, uniq = set(), []
    for p in out:
        k = _resolved_key(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def targets_with_days() -> list[tuple[Path, int]]:
    """(根目录, 保留天数)：共享取数缓存单独 7 天（一天约 200MB），其余 90 天。"""
    base = config.RETENTION_DAYS
    shared_days = config.SHARED_CACHE_RETENTION_DAYS
    if shared_days <= 0:
        shared_days = base
    shared_real = _resolved_key(Path(config.SHARED_CACHE_DIR))
    out = []
    for p in targets():
        days = shared_days if _resolved_key(p) == shared_real else base
        out.append((p, days))
    return out


def _archive_dir() -> Path:
    return Path(config.ARCHIVE_DIR)


def scan(days: int | None = None) -> dict:
    """列出会被删除的条目（不删）。返回 {cutoff, files:[...], dirs:[...], bytes, counts}。

    共享取数缓存（data\\shared\\cache）按 SHARED_CACHE_RETENTION_DAYS（默认 7 天）清，
    其余目录按 days（默认 90 天）。days 显式传入时对共享缓存也生效（向下取更严的那个）。
    """
    days = config.RETENTION_DAYS if days is None else int(days)
    now = time.time()
    files: list[Path] = []
    dirs: list[Path] = []
    total = 0

    for root, keep_days in targets_with_days():
        if days is not None and days < keep_days:
            keep_days = days
        cutoff = now - keep_days * 86400
        if not root.exists():
            continue
        for cur, subdirs, names in os.walk(root, topdown=True):
            curp = Path(cur)
            subdirs[:] = [d for d in subdirs if d not in KEEP_DIR_NAMES]
            for name in names:
                p = curp / name
                if _keep(p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    files.append(p)
                    total += st.st_size

    arc_cutoff = now - days * 86400
    arc = _archive_dir()
    if arc.exists():
        for p in sorted(arc.iterdir()):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= arc_cutoff:
                continue
            if p.is_dir():
                dirs.append(p)
                for cur, _subdirs, names in os.walk(p):
                    for name in names:
                        try:
                            total += (Path(cur) / name).stat().st_size
                        except OSError:
                            pass
            else:
                files.append(p)
                total += st.st_size

    return {"cutoff": now - days * 86400, "files": files, "dirs": dirs, "bytes": total,
            "counts": {"files": len(files), "dirs": len(dirs)}}


def prune(days: int | None = None, dry: bool = False) -> dict:
    """删掉过期数据。返回 {deleted_files, deleted_dirs, bytes, dry, days}。"""
    days = config.RETENTION_DAYS if days is None else int(days)
    if days <= 0:
        return {"deleted_files": 0, "deleted_dirs": 0, "bytes": 0, "dry": dry,
                "days": days, "skipped": "retention disabled"}
    plan = scan(days)
    deleted_files = 0
    deleted_dirs = 0
    freed = 0
    if not dry:
        for p in plan["files"]:
            try:
                sz = p.stat().st_size
                p.unlink()
                deleted_files += 1
                freed += sz
            except OSError:
                pass
        for d in plan["dirs"]:
            try:
                sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                deleted_dirs += 1
                freed += sz
            except OSError:
                pass
        # 顺手清掉清理后变空的目录（cache 分区、out 子目录）
        for root in targets():
            if not root.exists():
                continue
            for cur, _subdirs, _names in os.walk(root, topdown=False):
                curp = Path(cur)
                if curp == root or any(part in KEEP_DIR_NAMES for part in curp.parts):
                    continue
                try:
                    if not any(curp.iterdir()):
                        curp.rmdir()
                except OSError:
                    pass
    return {"deleted_files": deleted_files, "deleted_dirs": deleted_dirs,
            "bytes": freed, "dry": dry, "days": days,
            "planned_files": plan["counts"]["files"], "planned_dirs": plan["counts"]["dirs"],
            "planned_bytes": plan["bytes"]}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stock prune", description="清理过期运行数据（默认 90 天）")
    ap.add_argument("--days", type=int, default=None, help="保留天数，默认取 config.RETENTION_DAYS")
    ap.add_argument("--dry", action="store_true", help="只统计不删除")
    args = ap.parse_args(argv)
    r = prune(args.days, args.dry)
    if r.get("skipped"):
        print(f"跳过清理：{r['skipped']}")
        return 0
    if args.dry:
        print(f"[dry] 保留 {r['days']} 天 → 计划删除 {r['planned_files']} 个文件 / "
              f"{r['planned_dirs']} 个目录，约 {human(r['planned_bytes'])}")
        plan = scan(args.days)
        for p in plan["files"][:15]:
            print(f"  file  {p}")
        for d in plan["dirs"][:5]:
            print(f"  dir   {d}")
        if r["planned_files"] > 15:
            print(f"  ... 其余 {r['planned_files'] - 15} 个文件")
    else:
        print(f"保留 {r['days']} 天 → 删除 {r['deleted_files']} 个文件 / "
              f"{r['deleted_dirs']} 个目录，释放 {human(r['bytes'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
