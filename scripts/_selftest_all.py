# -*- coding: utf-8 -*-
"""自检汇总器自身的离线自检：计数正则、套件清单一致性、启动规则文档。

2026-11-26 第三轮测试审计补的缺口（tests B9 / C3 / C5）：
  - setuptest_all.count_of 的 7 条正则与「全部通过但不报数」→ None；
  - run_one 对临时脚本的 rc/计数解析（含失败脚本）；
  - SUITES + NETWORK 覆盖脚本目录下全部 _selftest*.py（无孤儿、无幽灵）；
  - 套数一致性：SUITES=30 / NETWORK=3 / 共 33，且 SKILL/README/risk-review/workflow 与代码一致；
  - 启动规则（2026-11-26 定）与「只认四条定时任务」的文本存在。
"""
from __future__ import annotations

import glob
import atexit
import shutil
import os
import re
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

N = [0]
BAD = []


def check(msg: str, ok: bool, detail: str = "") -> None:
    N[0] += 1
    if ok:
        print(f"  OK  {msg}")
    else:
        print(f"FAIL  {msg}" + (f"  [{detail}]" if detail else ""))
        BAD.append(msg)


def _read(path: str) -> str:
    raw = open(path, "rb").read()
    return raw.decode("utf-8-sig", "replace")


def main() -> int:
    import selftest_all as S

    print("1) count_of：7 条计数正则 + 无数字 → None")
    cases = [
        ("通过 89 项，失败 0 项", 89),
        ("通过 106 / 106 项", 106),
        ("OK 全部通过（115 项断言）", 115),
        ("断言：85 项｜通过 85", 85),
        ("断言 197 项｜失败 0 项", 197),
        ("PASS 全部 68 项", 68),
        ("全部 64 项", 64),
    ]
    for text, want in cases:
        got = S.count_of(text)
        check(f"count_of({text!r}) == {want}", got == want, f"got={got!r}")
    check("无数字但全部通过 → None", S.count_of("结果：全部通过。") is None)
    check("无任何计数 → None", S.count_of("nothing here") is None)

    print("2) run_one：临时脚本的 rc / 计数解析")
    tmp = tempfile.mkdtemp(prefix="selftest_all_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    _ok = os.path.join(tmp, "s_ok.py")
    _nonum = os.path.join(tmp, "s_nonum.py")
    _fail = os.path.join(tmp, "s_fail.py")
    with open(_ok, "w", encoding="utf-8") as f:
        f.write('print("OK 全部通过（3 项断言）")\n')
    with open(_nonum, "w", encoding="utf-8") as f:
        f.write('print("结果：PASS — 全部通过。")\n')
    with open(_fail, "w", encoding="utf-8") as f:
        f.write("import sys\nprint('boom')\nsys.exit(1)\n")
    rc, n, _ = S.run_one(_ok)
    check("run_one 正常脚本 rc=0 / 3 项", rc == 0 and n == 3, f"rc={rc}, n={n}")
    rc, n, _ = S.run_one(_nonum)
    check("run_one 只报通过不报数 → n=None", rc == 0 and n is None, f"rc={rc}, n={n}")
    rc, n, _ = S.run_one(_fail)
    check("run_one 失败脚本 rc=1", rc == 1, f"rc={rc}, n={n}")

    print("3) 套件清单：无孤儿、无幽灵")
    listed = {name for name, _ in S.SUITES} | {name for name, _ in S.NETWORK}
    files = {os.path.basename(p) for p in glob.glob(os.path.join(SCRIPT_DIR, "_selftest*.py"))}
    files |= {"_smoke.py"} if os.path.exists(os.path.join(SCRIPT_DIR, "_smoke.py")) else set()
    check("脚本目录 _selftest*.py 全部登记", files - listed == set(), str(sorted(files - listed)))
    check("清单里的套件都存在", listed - files == set(), str(sorted(listed - files)))

    print("4) 套数一致性：代码 = 文档")
    check("SUITES == 30", len(S.SUITES) == 30, str(len(S.SUITES)))
    check("NETWORK == 3", len(S.NETWORK) == 3, str(len(S.NETWORK)))
    total = len(S.SUITES) + len(S.NETWORK)
    check("合计 == 33", total == 33, str(total))
    skill = _read(os.path.join(REPO_ROOT, "SKILL.md"))
    readme = _read(os.path.join(REPO_ROOT, "README.md"))
    risk = _read(os.path.join(REPO_ROOT, "references", "risk-review.md"))
    wf = _read(os.path.join(REPO_ROOT, ".github", "workflows", "selftest.yml"))
    check("SKILL 写 33 套（30 套离线 + 3 套联网）",
          "33 套（30 套离线 + 3 套联网）" in skill)
    check("README 写 33 套（30 套离线 + 3 套联网）",
          "33 套（30 套离线 + 3 套联网）" in readme)
    check("risk-review 写 30 套离线", "30 套离线" in risk)
    check("workflow 步骤名写（30 套）", "（30 套）" in wf)

    print("5) 启动规则")
    check("SKILL 有启动规则标题", "**启动规则**" in skill)
    check("SKILL 写「直接执行第一笔决策」", "直接执行第一笔决策" in skill)
    check("SKILL 首次运行先刷新货基排行", "cash_pool.py" in skill)
    check("SKILL 写「只认四条」定时任务", "只认四条" in skill)

    print("=" * 78)
    if BAD:
        print(f"FAIL 共 {len(BAD)} 项失败 / {N[0]} 项")
        for b in BAD:
            print("  - " + b)
        return 1
    print(f"OK 全部通过（{N[0]} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
