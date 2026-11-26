# -*- coding: utf-8 -*-
"""文档结构校验：标题序列、§ 交叉引用、frontmatter 是否保持不变。

用法：
  python doc_verify.py --old <原文件> --new <压缩后文件>
"""
import argparse
import re
import sys

HEAD_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
SEC_RE = re.compile(r"§\s*([0-9]+(?:\.[0-9]+)*)")


def load(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return text[:end + 4] if end > 0 else None


def headings(text):
    return [m.group(2).strip() for m in HEAD_RE.finditer(text)]


def section_numbers(heads):
    nums = set()
    for h in heads:
        for m in re.finditer(r"([0-9]+(?:\.[0-9]+)+)", h):
            nums.add(m.group(1))
        for m in re.finditer(r"^\s*([0-9]+)\s*[.、]", h):
            nums.add(m.group(1))
    return nums


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--skip-frontmatter", action="store_true")
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    bad = 0

    oh, nh = headings(old), headings(new)
    if oh == nh:
        print(f"OK 标题序列完全一致（{len(oh)} 个）")
    else:
        bad += 1
        print(f"!! 标题不一致：old {len(oh)} 个 / new {len(nh)} 个")
        set_o, set_n = set(oh), set(nh)
        for x in oh:
            if x not in set_n:
                print("   丢失:", x)
        for x in nh:
            if x not in set_o:
                print("   新增:", x)

    if not a.skip_frontmatter:
        fo, fn = frontmatter(old), frontmatter(new)
        if fo == fn:
            print("OK frontmatter 一致" if fo else "OK 无 frontmatter")
        else:
            bad += 1
            print("!! frontmatter 不一致")
            print("--- old ---"); print(fo)
            print("--- new ---"); print(fn)

    nums_old, nums_new = section_numbers(oh), section_numbers(nh)
    refs_old = set(SEC_RE.findall(old))
    refs_new = set(SEC_RE.findall(new))
    broken_new = sorted(r for r in refs_new if r not in nums_new)
    broken_old = sorted(r for r in refs_old if r not in nums_old)
    print(f"§ 引用：old={len(refs_old)} new={len(refs_new)}")
    if broken_new:
        bad += 1
        print("!! new 中指向不存在章节的引用：", broken_new)
    else:
        print("OK new 的全部 § 引用都能落到现有标题")
    if broken_old:
        print(f"   （old 本身有 {len(broken_old)} 处悬空引用：{broken_old[:10]}）")

    print(f"字符数：old={len(old)} new={len(new)} 压缩率={1-len(new)/len(old):.1%}")
    print("结构结论：", "PASS" if bad == 0 else f"FAIL（{bad} 项）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
