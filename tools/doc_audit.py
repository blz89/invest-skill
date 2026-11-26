# -*- coding: utf-8 -*-
"""不变量审计：压缩文档前后，规则性 token 必须一个不少。

用法：
  python doc_audit.py --old <原文件> --new <压缩后文件> [--strict-new]

检查项：
  1) 数字（含小数/百分号）集合：old 中每个数字都必须在 new 中出现
  2) 反引号 span 集合：old 中每个 `...` 内容都必须在 new 中
  3) 命令/脚本/flag：scripts/*.py、--flag
  4) 标题（#/##/### 行）：old 的标题必须全部保留
  5) 关键规则词表：必须全部出现
  6) 代码块数量不得少于 old 的 70%
--strict-new 时，new 中出现的“新数字”也报出来（人工判断是否引入新含义）。
"""
import argparse
import re
import sys

NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")
TICK_RE = re.compile(r"`([^`\n]{1,120})`")
SCRIPT_RE = re.compile(r"scripts[\\/][\w\-]+\.py")
FLAG_RE = re.compile(r"--[a-zA-Z][\w\-]{1,40}")
HEAD_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.M)
CODE_RE = re.compile(r"```")

KEYWORDS = [
    # 边界/约束
    "硬约束", "一票否决", "四道否决", "熔断", "止损", "止盈", "冷却",
    "限价", "涨停", "跌停", "停牌", "ST", "退市", "T+0", "T+1",
    # 仓位/权重
    "防守", "中性", "进攻", "深熊", "熊市", "牛市", "MA200", "ATR",
    "单票", "行业", "篮子", "权重", "分批", "三批", "建仓",
    # 阈值
    "买入线", "观察线", "安全边际", "目标价", "可证伪",
    # 现金层
    "朝朝宝", "货基", "逆回购", "万份收益", "实得年化", "R2", "R3",
    # 账本/流程
    "账本", "净值", "回撤", "基准", "超额", "净利润", "可投资本",
    "决策日", "巡检", "月报", "周报", "年报", "计分卡", "评分",
    "定时任务", "prompts", "HANDOVER", "pending_changes",
    # 合规
    "模拟盘", "不承诺收益", "不接券商", "非投资建议",
    # 数据
    "PIT", "缓存", "取数", "prefetch", "coverage", "doctor", "preflight",
    "billing", "paper_exec", "patrol.py", "run.py", "weekly.py",
]


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def toks(text):
    nums = set(m.group(0).replace("−", "-") for m in NUM_RE.finditer(text))
    ticks = set(m.group(1).strip() for m in TICK_RE.finditer(text)
                if m.group(1).strip())
    scripts = set(m.group(0).replace("\\", "/") for m in SCRIPT_RE.finditer(text))
    flags = set(m.group(0) for m in FLAG_RE.finditer(text))
    heads = set(m.group(2).strip() for m in HEAD_RE.finditer(text))
    return nums, ticks, scripts, flags, heads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--strict-new", action="store_true")
    ap.add_argument("--also", action="append", default=[], help="参与 new 侧 token 合并的额外文件（可重复）")
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    if a.also:
        new = new + "\\n\\n" + "\\n\\n".join(load(x) for x in a.also)
    on, ot, os_, of, oh = toks(old)
    nn, nt, ns, nf, nh = toks(new)
    bad = 0

    def report(title, missing, extra=None):
        nonlocal bad
        if missing:
            bad += 1
            print(f"!! {title} 缺失 {len(missing)} 项：")
            for x in sorted(missing)[:400]:
                print("   -", x)
        else:
            print(f"OK {title} 全部保留（{len(missing)} 缺失）")
        if extra:
            print(f"   ~ {title} 新增 {len(extra)} 项（人工判断）：")
            for x in sorted(extra)[:80]:
                print("     +", x)

    report("数字", on - nn, (nn - on) if a.strict_new else None)
    report("反引号内容", ot - nt)
    report("脚本路径", os_ - ns)
    report("命令行 flag", of - nf)
    report("标题", oh - nh)

    missing_kw = [k for k in KEYWORDS if k in old and k not in new]
    if missing_kw:
        bad += 1
        print(f"!! 关键规则词缺失 {len(missing_kw)} 个：{missing_kw}")
    else:
        print(f"OK 关键规则词全部保留（{len(KEYWORDS)} 词）")

    oc, nc = len(CODE_RE.findall(old)), len(CODE_RE.findall(new))
    ratio = nc / oc if oc else 1.0
    print(f"代码围栏：old={oc} new={nc} ratio={ratio:.2f}")
    if ratio < 0.7:
        bad += 1
        print("!! 代码块减少超过 30%")

    print(f"字符数：old={len(old)} new={len(new)} 压缩率={1 - len(new)/len(old):.1%}")
    print("结论：", "PASS" if bad == 0 else f"FAIL（{bad} 项）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

