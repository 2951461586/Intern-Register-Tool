#!/usr/bin/env python
r"""把配额台账的 scope 从「槽位位置号」迁移成「出口 IP」。

为什么必须迁移
--------------
`src/quota.py` 的 scope 原来是 `f"slot{lease.slot}"` —— **槽位在
`slots.txt` 里的位置号**。这个编号不稳定：只要往 `slots.txt` 里
加一个出口、删一个死掉的出口、或者调一下顺序，**所有位置号的含义就整体平移**，
既有记录会静默错配到别的出口 IP 头上。

本项目实测已经踩到过。用 `results.json` 里每条记录的 `proxy_slot`
（形如 `slot2(http://127.0.0.1:7903)`，里面带着**当时的真实端口**）
做交叉表，台账里出现了 3 条错配：

    台账 scope  真实端口   条数
    slot1       7901      41    ✓
    slot2       7903      39    ✓
    slot2       7902       1    ← 错配
    slot3       7904      37    ✓
    slot3       7903       1    ← 错配
    slot4       7904       1    ← 错配

⚠ 注意：**总数是对的，错的只是标签**（按 IP 聚合后仍是 40/39/37/0）。
所以迁移不会改变"还能注册几个"，只是把账记到正确的 IP 名下。
迁移之后 `slots.txt` 的顺序就随便怎么排都不影响记账了。

怎么定位每条记录的真实出口
--------------------------
`results.json` 的 `proxy_slot` 字段里存着**当时的真实端口**，
用 `email` 做键关联即可。关联不上的记录（池化之前那批，没有 `proxy_slot`）
**保持原样** —— 它们本来就没有出口归属，不该硬塞一个。

用法
----
    python tools/data/migrate_quota_scope.py                # 只报告，不落盘（默认）
    python tools/data/migrate_quota_scope.py --apply        # 真迁移（自动备份）
    python tools/data/migrate_quota_scope.py --ledger <path> --results <path>
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src import config  # noqa: E402

DEFAULT_LEDGER = ROOT / ".workbuddy-ai" / "state" / "register_quota.jsonl"
DEFAULT_RESULTS = ROOT / "results.json"

# 从 `slot2(http://127.0.0.1:7903)` 里把端口抠出来。
_PORT_RE = re.compile(r":(\d+)\s*\)")


def load_email_to_ip(results_path: Path) -> dict[str, str]:
    """`results.json` → {email: 出口 IP}。

    用 `proxy_slot` 里的**真实端口** + `config.SLOT_EGRESS_IPS` 换算成 IP。
    端口不在映射表里就跳过（宁可少迁，不可迁错）。
    """
    if not results_path.is_file():
        raise SystemExit(f"找不到 results 文件：{results_path}")
    data = json.loads(results_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    unknown_port: collections.Counter = collections.Counter()
    for rec in data:
        ps = rec.get("proxy_slot") or ""
        email = rec.get("email") or ""
        if not ps or not email:
            continue
        m = _PORT_RE.search(ps)
        if not m:
            continue
        ip = config.SLOT_EGRESS_IPS.get(m.group(1))
        if ip:
            out[email] = ip
        else:
            unknown_port[m.group(1)] += 1
    if unknown_port:
        print(f"⚠ 有端口的出口 IP 不在 SLOT_EGRESS_IPS 里，已跳过："
              f"{dict(unknown_port)}")
    return out


def read_ledger(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把配额台账的 scope 从槽位位置号迁移成出口 IP")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--results", default=str(DEFAULT_RESULTS))
    ap.add_argument("--apply", action="store_true",
                    help="真迁移（默认只报告）。会自动备份原台账。")
    args = ap.parse_args()

    ledger_path = Path(args.ledger)
    if not ledger_path.is_file():
        raise SystemExit(f"找不到台账：{ledger_path}")

    e2ip = load_email_to_ip(Path(args.results))
    rows = read_ledger(ledger_path)
    print(f"台账 {len(rows)} 条；results.json 能关联出出口 IP 的 {len(e2ip)} 条\n")

    # ── 迁移 ──────────────────────────────────────────────────────
    before = collections.Counter()
    after = collections.Counter()
    moved = 0
    unmapped: list[dict] = []
    for r in rows:
        old = r.get("scope") or ""
        before[old or "<none>"] += 1
        ip = e2ip.get(r.get("email") or "")
        if ip:
            if old != ip:
                moved += 1
            r["scope"] = ip
        elif old and not old.startswith(("slot",)):
            pass                      # 已经是 IP 形式了，幂等
        else:
            if old:
                unmapped.append(r)    # 有旧 scope 但关联不出真实出口
            r["scope"] = ""           # 池化前那批：本来就没有出口归属
        after[r.get("scope") or "<none>"] += 1

    # ── 报告 ──────────────────────────────────────────────────────
    def show(title: str, c: collections.Counter) -> None:
        print(title)
        for k, v in sorted(c.items(), key=lambda kv: -kv[1]):
            print(f"   {k:<20} {v:>4} 条")

    show("迁移前 scope 分布：", before)
    print()
    show("迁移后 scope 分布（= 出口 IP）：", after)
    print(f"\n改动了 {moved} 条")
    if unmapped:
        print(f"\n⚠ 有 {len(unmapped)} 条带着旧 scope 但关联不出真实出口，"
              f"已清空 scope：")
        for r in unmapped[:10]:
            print(f"   {r.get('email')}  old={r.get('scope')!r}")

    if not args.apply:
        print("\n（这是 dry-run，台账未改动。要落地加 --apply）")
        return 0

    # ── 落盘（先备份）─────────────────────────────────────────────
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = ledger_path.with_suffix(ledger_path.suffix + f".pre-scope-migrate-{ts}")
    shutil.copy2(ledger_path, bak)
    tmp = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(ledger_path)
    print(f"\n✅ 已写入 {ledger_path}\n   备份：{bak}")

    # 回读校验：条数不变、每条都能解析
    back = read_ledger(ledger_path)
    assert len(back) == len(rows), f"条数变了！{len(rows)} -> {len(back)}"
    assert all(isinstance(x.get("scope"), str) for x in back), "有 scope 不是字符串"
    print(f"   回读校验：{len(back)} 条，全部可解析 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
