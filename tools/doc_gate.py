# -*- coding: utf-8 -*-
"""文档闸门：防止后续编辑悄悄删掉规则性 token（数字 / 反引号 / 脚本路径 / --flag / 标题 / 规则词）。

背景：本技能的文档即「规则」，压缩/改写时最容易悄悄丢的是数字与代码标识。
本闸门把「当前已认可的文档」抽成 token 契约（tools/doc_contract.json）；之后任何编辑
只要让契约里的 token 缺失，闸门就 FAIL，必须人工确认内容确实是有意删除，再 --update 重建契约。

用法（在技能根目录跑，解释器用 stock/.venv）：
  python tools/doc_gate.py                  # 校验（决策日收口 / 改完文档后跑）
  python tools/doc_gate.py --update         # 人工确认后重建契约（建议在 commit 里写明理由与增删项）
  python tools/doc_gate.py --list           # 打印契约覆盖的文件与 token 数

覆盖文件（存在即纳入；新增 references/*.md、prompts/*.md 自动纳入）：
  SKILL.md  README.md  references/*.md  prompts/*.md
"""
import argparse
import glob
import json
import os
import re
import sys
import time

def _find_root():
    """向上找技能根目录（含 SKILL.md 的目录）；找不到就用脚本所在目录。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        if os.path.exists(os.path.join(d, "SKILL.md")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _find_root()
CONTRACT = os.path.join(ROOT, "tools", "doc_contract.json")

NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")
TICK_RE = re.compile(r"`([^`\n]{1,120})`")
SCRIPT_RE = re.compile(r"scripts[\\/][\w\-]+\.py")
FLAG_RE = re.compile(r"--[a-zA-Z][\w\-]{1,40}")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
CODE_RE = re.compile(r"```")

KEYWORDS = [
    "硬约束", "一票否决", "四道否决", "熔断", "止损", "止盈", "冷却",
    "限价", "涨停", "跌停", "停牌", "ST", "退市", "T+0", "T+1",
    "防守", "中性", "进攻", "深熊", "熊市", "牛市", "MA200", "ATR",
    "单票", "行业", "篮子", "权重", "分批", "三批", "建仓",
    "买入线", "观察线", "安全边际", "目标价", "可证伪",
    "朝朝宝", "货基", "逆回购", "万份收益", "实得年化", "R2", "R3",
    "账本", "净值", "回撤", "基准", "超额", "净利润", "可投资本",
    "决策日", "巡检", "月报", "周报", "年报", "计分卡", "评分",
    "定时任务", "prompts", "HANDOVER", "pending_changes",
    "模拟盘", "不承诺收益", "不接券商", "非投资建议",
    "PIT", "缓存", "取数", "prefetch", "coverage", "doctor", "preflight",
    "billing", "paper_exec", "patrol.py", "run.py", "weekly.py",
]


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def doc_files():
    files = []
    for rel in ("SKILL.md", "README.md"):
        if os.path.exists(os.path.join(ROOT, rel)):
            files.append(rel)
    for pat in ("references/*.md", "prompts/*.md"):
        for p in sorted(glob.glob(os.path.join(ROOT, pat))):
            files.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    return files


def toks(text):
    return {
        "nums": sorted(set(m.group(0).replace("−", "-") for m in NUM_RE.finditer(text))),
        "ticks": sorted(set(m.group(1).strip() for m in TICK_RE.finditer(text) if m.group(1).strip())),
        "scripts": sorted(set(m.group(0).replace("\\", "/") for m in SCRIPT_RE.finditer(text))),
        "flags": sorted(set(m.group(0) for m in FLAG_RE.finditer(text))),
        "heads": sorted(set(m.group(2).strip() for m in HEAD_RE.finditer(text))),
        "keywords": [k for k in KEYWORDS if k in text],
        "fences": len(CODE_RE.findall(text)),
        "chars": len(text),
    }


def build_contract():
    files = doc_files()
    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "files": {f: toks(load(os.path.join(ROOT, f))) for f in files}}
    os.makedirs(os.path.dirname(CONTRACT), exist_ok=True)
    with open(CONTRACT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"契约已重建：{CONTRACT}（{len(files)} 个文件）")
    for f in files:
        t = data["files"][f]
        print(f"  {f:34s} chars={t['chars']:6d} nums={len(t['nums']):4d} ticks={len(t['ticks']):4d} heads={len(t['heads']):3d}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="人工确认内容变更后重建契约")
    ap.add_argument("--list", action="store_true", help="打印契约概况")
    a = ap.parse_args()
    if a.update:
        return build_contract()
    if not os.path.exists(CONTRACT):
        print(f"!! 契约不存在：{CONTRACT}（先跑 --update）")
        return 2
    contract = json.load(open(CONTRACT, encoding="utf-8"))
    if a.list:
        for f, t in contract["files"].items():
            print(f"  {f:34s} chars={t['chars']:6d} nums={len(t['nums']):4d} ticks={len(t['ticks']):4d} heads={len(t['heads']):3d} fences={t['fences']}")
        return 0

    bad = 0
    seen = set()
    for rel, want in contract["files"].items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"!! 缺失文件：{rel}")
            bad += 1
            continue
        seen.add(rel)
        got = toks(load(path))
        problems = []
        for key, label in (("nums", "数字"), ("ticks", "反引号"), ("scripts", "脚本路径"),
                           ("flags", "flag"), ("heads", "标题"), ("keywords", "规则词")):
            missing = [x for x in want[key] if x not in set(got[key])]
            if missing:
                problems.append((label, missing))
        if got["fences"] < int(want["fences"] * 0.7):
            problems.append(("代码围栏", [f"{want['fences']} → {got['fences']}"]))
        if problems:
            bad += 1
            print(f"!! {rel}：")
            for label, missing in problems:
                print(f"   - {label} 缺失 {len(missing)} 项：{missing[:12]}{' …' if len(missing) > 12 else ''}")
        else:
            print(f"OK {rel}（chars {want['chars']} → {got['chars']}）")

    extra = [f for f in doc_files() if f not in seen]
    if extra:
        print(f"~ 契约外新文件（未校验，如为新增文档请 --update）：{extra}")

    print("文档闸门：", "PASS" if bad == 0 else f"FAIL（{bad} 个文件）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
