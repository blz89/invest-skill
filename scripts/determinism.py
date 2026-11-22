# -*- coding: utf-8 -*-
"""可复现性检查：同一份缓存、同一套阈值，跑两遍管线，产出必须逐字一致。

SKILL 的承诺是「同一份数据必得同一结论」。这条承诺没人验证过——浮点累计顺序、
字典/集合遍历顺序、排序并列处理、CSV 缓存读回类型，任何一处不确定都会让
同样的输入给出不同的组合。无人值守系统如果不可复现，复盘归因就是假的。

用法：
  python scripts/determinism.py                   # 默认 5 万口径（主账本）
  python scripts/determinism.py --capital 50000   # 本部署 5 万口径
  python scripts/determinism.py --keep           # 保留两遍的产出以便人工比对

判定：CSV 逐字节一致；JSON 去掉时间戳类字段后逐字节一致。
例外（设计如此）：provenance_*.json 是**血缘**不是结论，只比「取到了哪些数据」
（kind/key 集合 + 两遍都记了行数时的 rows），不比 source/cached_at——第一次联网取、
第二次命中缓存，来源标注本来就该不同，详见 provenance_canon()。
退出码：0 = 可复现；1 = 有差异（会打印差异字段/行号）；2 = 管线失败。
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

SC = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OUT = config.OUT_DIR

# 会随时间变化的字段/片段，不算「结论」
VOLATILE_KEYS = ("generated_at", "elapsed", "elapsed_sec", "timestamp", "duration",
                 "run_at", "provenance_at", "fetched_at", "cache_date", "cached_at", "secs")
VOLATILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _is_provenance(name: str) -> bool:
    return name.startswith("provenance_") and name.endswith(".json")


def provenance_canon(path: str) -> tuple[list[tuple[str, str]], dict[tuple[str, str], int]]:
    """血缘文件只比「取到了哪些数据」，不比「这次是联网取还是命中缓存」。

    为什么不能逐字比：`datasource._cached()` 的联网路径记 `source:"endpoints"` + `rows` +
    `secs`，缓存命中路径记 `source:"cache"` + `cached_at`（见 scripts/datasource.py 的
    _cached）。缓存冷热一变，同一份数据的「来源标注」本来就该不同——把来源当确定性断言，
    等于要求「第二次读缓存时必须撒谎说自己是联网取的」。所以这里归一成 (kind, key) 集合；
    `rows`（行数）只在两遍都记录了行数时比对（缓存命中不记 rows），行数不同 = 两遍取到的
    数据不同，那是真问题，必须报出来。

    注意：这里**只放宽 provenance_*.json 的 source/cached_at/rows 缺省**，其余文件仍逐字比。
    同一 kind/key 被取多次时按**集合**归一：`_prov` 记的是「取数过程」，同一份数据被
    fetch 两次就是两条记录（缓存刚写好又被另一个调用绕过等），条数不是结论；但 kind/key
    集合少了/多了、或两遍都联网取到的行数不同，都是真问题。
    """
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    pairs: set = set()
    rows: dict = {}
    for name, recs in (d.get("items") or {}).items():
        for r in recs or []:
            k = (str(r.get("kind") or name.split("::")[0]), str(r.get("key") or name))
            pairs.add(k)
            if isinstance(r.get("rows"), (int, float)):
                rows[k] = int(r["rows"])
    return sorted(pairs), rows


def _env():
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["TQDM_DISABLE"] = "1"
    return e


def run_pipeline(capital: float, tier: str, limit: int) -> int:
    cmd = [PY, os.path.join(SC, "run.py"), "--capital", f"{capital:.0f}", "--tier", tier]
    if limit:
        cmd += ["--limit", str(limit)]
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=_env(), cwd=SC).returncode


def files_of_interest() -> list[str]:
    names = []
    for n in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, n)
        if not os.path.isfile(p):
            continue
        if n.endswith((".csv", ".json")):
            names.append(n)
    return names


def snapshot(dst: str, names: list[str]) -> None:
    os.makedirs(dst, exist_ok=True)
    for n in names:
        shutil.copy2(os.path.join(OUT, n), os.path.join(dst, n))


def strip_json(obj):
    if isinstance(obj, dict):
        return {k: strip_json(v) for k, v in obj.items()
                if not any(t in k.lower() for t in VOLATILE_KEYS)}
    if isinstance(obj, list):
        return [strip_json(v) for v in obj]
    if isinstance(obj, str):
        return VOLATILE_RE.sub("<ts>", obj)
    return obj


def norm(path: str) -> str:
    if path.endswith(".csv"):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.dumps(strip_json(json.load(f)), ensure_ascii=False, sort_keys=True, indent=1)
        except Exception:  # noqa: BLE001
            return f.read()


def first_diff(a: str, b: str) -> str:
    la, lb = a.splitlines(), b.splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<缺行>"
        y = lb[i] if i < len(lb) else "<缺行>"
        if x != y:
            return f"第 {i + 1} 行不同：\n  run1: {x[:200]}\n  run2: {y[:200]}"
    return "行数相同但没有差异（不应发生）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可复现性检查：同缓存同阈值跑两遍，产出必须一致")
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--tier", default="B")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)

    base = os.path.join(OUT, "_determinism")
    d1, d2 = os.path.join(base, "run1"), os.path.join(base, "run2")
    for d in (d1, d2):
        shutil.rmtree(d, ignore_errors=True)

    print(f"=== 第一遍（本金 {a.capital:,.0f} 元 · {a.tier} 档）===", flush=True)
    if run_pipeline(a.capital, a.tier, a.limit) != 0:
        print("[determinism] ✗ 第一遍管线失败")
        return 2
    names = files_of_interest()
    snapshot(d1, names)

    print(f"\n=== 第二遍（同缓存、同参数）===", flush=True)
    if run_pipeline(a.capital, a.tier, a.limit) != 0:
        print("[determinism] ✗ 第二遍管线失败")
        return 2
    snapshot(d2, names)

    print("\n=== 比对 ===")
    diffs = []
    for n in names:
        if _is_provenance(n):
            # 血缘文件：按「取到了哪些数据」比，不按「从哪取的」比（理由见 provenance_canon）
            p1, r1 = provenance_canon(os.path.join(d1, n))
            p2, r2 = provenance_canon(os.path.join(d2, n))
            common = sorted(set(r1) & set(r2))
            rows_diff = [(k, r1[k], r2[k]) for k in common if r1[k] != r2[k]]
            if p1 == p2 and not rows_diff:
                print(f"  ✓ {n}（血缘：kind/key 集合 {len(p1)} 条一致；两遍都记 rows 的 "
                      f"{len(common)} 条行数一致）")
            else:
                only1 = [k for k in p1 if k not in set(p2)]
                only2 = [k for k in p2 if k not in set(p1)]
                diffs.append((n, f"血缘差异：仅 run1 {only1[:10]}；仅 run2 {only2[:10]}；"
                                 f"rows 不同 {rows_diff[:10]}"))
                print(f"  ✗ {n}")
            continue
        a1 = norm(os.path.join(d1, n))
        a2 = norm(os.path.join(d2, n))
        if a1 == a2:
            print(f"  ✓ {n}")
        else:
            print(f"  ✗ {n}")
            diffs.append((n, first_diff(a1, a2)))

    if not diffs:
        print("\n[determinism] ✓ 可复现：两遍产出逐字一致（时间戳类字段已剔除）")
        if not a.keep:
            shutil.rmtree(base, ignore_errors=True)
        return 0

    print(f"\n[determinism] ✗ 不可复现：{len(diffs)} 个文件有差异")
    for n, d in diffs:
        print(f"\n--- {n} ---\n{d}")
    print(f"\n两遍产出保留在：{base}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
