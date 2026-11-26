# -*- coding: utf-8 -*-
"""覆盖度校验：压缩后的新文档是否仍“讲到”原文档每一段的内容。

用法：
  python doc_coverage.py --old <原文件> --new <压缩后文件> [--threshold 0.35] [--show 60]

对 old 的每个非空段落（≥60 字符），用 difflib 找 new 中最相似的段落；
相似度低于阈值的即可能被“删掉而不是压缩”。按章节汇总输出。
"""
import argparse
import re
import sys
from difflib import SequenceMatcher

HEAD_RE = re.compile(r"^(#{1,3})\s+(.+)$")


def load(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def blocks(text):
    out = []
    for b in re.split(r"\n\s*\n", text):
        b = b.strip()
        if len(b) >= 60:
            out.append(b)
    return out


def cov(old_b, new_block_set):
    best = 0.0
    for nb in new_block_set:
        if abs(len(nb) - len(old_b)) > max(len(old_b), len(nb)) * 0.9:
            pass
        r = SequenceMatcher(None, old_b, nb, autojunk=False).quick_ratio()
        if r <= best:
            continue
        r2 = SequenceMatcher(None, old_b, nb, autojunk=False).ratio()
        best = max(best, r2)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--show", type=int, default=40)
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    nb = blocks(new)
    ob = blocks(old)
    scored = [(cov(b, nb), b) for b in ob]
    low = [(s, b) for s, b in scored if s < a.threshold]
    low.sort(key=lambda x: x[0])
    print(f"段落数 old={len(ob)} new={len(nb)} | 低覆盖(<{a.threshold}) {len(low)} 段 "
          f"| 平均覆盖 {sum(s for s, _ in scored)/max(1,len(scored)):.2f}")
    for s, b in low[:a.show]:
        first = b.split("\n")[0][:100].replace("\n", " ")
        print(f"  [{s:.2f}] {first}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
