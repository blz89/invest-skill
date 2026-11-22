# -*- coding: utf-8 -*-
"""账本回滚（rollback）：把模拟盘账本恢复到某份备份，并把当前账本再备份一次（回滚也可逆）。

为什么要单独一个脚本：`paper.py` 在决策/补跑前会自动备份（`<账本目录>/_backup/`），
但「有备份」不等于「能回滚」——真出事（重复下单、手改错、脚本写坏）时，
人需要在 30 秒内退回到上一次好状态，而不是去翻文件管理器。

用法：
  python scripts/rollback.py --ledger 50k --list          # 看有哪些备份（含现金/净值摘要）
  python scripts/rollback.py --ledger 50k --dry-run       # 预览「恢复到最近一份」会变成什么
  python scripts/rollback.py --ledger 50k                 # 真正恢复到最近一份
  python scripts/rollback.py --ledger 50k --index 2      # 恢复到倒数第 3 份（-1 是最近；本部署单账本 50k）
  python scripts/rollback.py --state <路径> --path <备份>  # 恢复任意账本/指定备份

退出码：0=成功；1=失败（没有备份/备份坏了/路径不对）；2=参数错误。
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import paper


def ledgers():
    return config.PAPER.get("ledgers") or {
        "50k": {"capital": config.PAPER.get("capital"), "tier": "B",
                "state_file": config.PAPER["state_file"], "label": "5 万"}}


def resolve_state(a) -> str:
    if a.state:
        return a.state
    led = ledgers().get(a.ledger)
    if not led:
        raise SystemExit(f"[rollback] ✗ 没有账本 {a.ledger}；可用：{', '.join(sorted(ledgers()))}")
    return led["state_file"]


def summarize(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        pend = [o for o in st.get("orders", []) if o.get("status") == "pending"]
        nav = (st.get("nav") or [{}])[-1]
        meta = st.get("meta", {})
        return (f"本金 {float(meta.get('capital') or 0):,.0f}｜现金 {float(st.get('cash') or 0):,.2f}"
                f"｜净值 {float(nav.get('nav') or 0):,.2f}｜持仓 {len(st.get('holdings') or {})} 只"
                f"｜挂单 {len(pend)} 张｜成交 {len(st.get('trades') or [])} 笔"
                f"｜最后结算 {nav.get('date')}")
    except Exception as e:  # noqa: BLE001
        return f"（读不出来：{e}）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把模拟盘账本回滚到某份备份")
    ap.add_argument("--ledger", default=config.PAPER.get("default_ledger", "50k"),
                    help="账本名（本部署单账本：50k）")
    ap.add_argument("--state", default="", help="直接指定账本文件（覆盖 --ledger）")
    ap.add_argument("--list", action="store_true", help="只列出备份，不恢复")
    ap.add_argument("--index", type=int, default=-1, help="备份序号，-1=最近一份（默认）")
    ap.add_argument("--path", default="", help="直接指定备份文件")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    a = ap.parse_args(argv)

    state_path = resolve_state(a)
    bks = paper.list_backups(state_path)

    if a.list:
        print(f"账本：{state_path}")
        print(f"当前：{summarize(state_path) if os.path.exists(state_path) else '（不存在）'}")
        if not bks:
            print("备份：无（首次决策前会自动生成）")
            return 0
        print(f"备份：{len(bks)} 份（-1 = 最近）")
        for i, p in enumerate(bks):
            print(f"  [{i:>2}] {os.path.basename(p)}")
            print(f"        {summarize(p)}")
        return 0

    if not os.path.exists(state_path):
        print(f"[rollback] ✗ 账本不存在：{state_path}")
        return 1

    try:
        if a.dry_run:
            src = a.path or (bks[a.index] if bks and -len(bks) <= a.index < len(bks) else "")
            if not src:
                print("[rollback] ✗ 没有可用的备份")
                return 1
            print(f"[rollback] 预览：将把\n  {state_path}\n恢复到\n  {src}")
            print(f"[rollback] 现在：{summarize(state_path)}")
            print(f"[rollback] 恢复后：{summarize(src)}")
            print("[rollback] （--dry-run，未落盘）")
            return 0
        res = paper.restore_state(state_path, backup_path=a.path or None, index=a.index)
    except paper.LedgerError as e:
        print(f"[rollback] ✗ {e}")
        return 1

    print(f"[rollback] ✓ 已恢复：{state_path}")
    print(f"[rollback]   来源：{res['restored_from']}")
    if res["previous_backup"]:
        print(f"[rollback]   回滚前的账本已另存：{res['previous_backup']}（回滚本身可逆）")
    print(f"[rollback]   现在：{summarize(state_path)}")
    print(f"[rollback]   时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("[rollback] 提示：回滚后跑一次 `python scripts/doctor.py --ledger all` 确认 GO，再决策。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
