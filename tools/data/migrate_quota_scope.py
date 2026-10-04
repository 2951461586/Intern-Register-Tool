#!/usr/bin/env python
r"""把配额台账的 scope 从「槽位位置号」迁移成「出口 IP」。

为什么必须迁移
--------------
`src/quota.py` 的 scope 原来是 `f"slot{lease.slot}"` —— **槽位在
`slots.txt` 里的位置号**。这个编号不稳定：只要往 `slots.txt` 里
加一个出口、删一个死掉的出口、或者调一下顺序，**所有位置号的含义就整体平移**，
既有记录会静默错配到别的出口 IP 头上。

本项目实测已经踩到过。用台账里每条记录的 `proxy_slot`
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
台账的 `proxy_slot` 字段里存着**当时的真实槽位 URL**
（形如 `slot2(https://a.example.com:7119)`），
把它交给 `config.slot_scope()` 换算成出口 IP，用 `email` 做键关联即可。

🔴 **换算必须走 `config.slot_scope()`，本工具不自己查表** ——
查表规则有两条（先 `host:port`、回退裸 `port`，见 `common/config.py`），
在这里再写一遍就会在**端口重复**（远程代理的常态）时分叉：
本地 mihomo 端口唯一、只拿端口查得中，于是分叉长期不暴露。

关联不上的记录（池化之前那批，没有 `proxy_slot`）
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

from common import config  # noqa: E402
from src import ledger  # noqa: E402

DEFAULT_LEDGER = ROOT / ".workbuddy-ai" / "state" / "register_quota.jsonl"
DEFAULT_RESULTS = ledger.ledger_path()

# 从 `slot2(http://127.0.0.1:7903)` 里把**槽位 URL** 抠出来。
#
# 🔴 不能只抠端口：`SLOT_EGRESS_IPS` 的键可以是 `host:port`（远程代理的端口
#    会重复，只写端口区分不了机房）。只拿端口查表会把这类槽位全判成
#    "不在映射表里" —— 而那是**静默跳过**：不报错、不崩，迁移报告看起来
#    一切正常，实际上一条都没迁。
_SLOT_URL_RE = re.compile(r"\((.*)\)\s*$")


def _slot_url(proxy_slot: str) -> str | None:
    """`slot2(http://127.0.0.1:7903)` → `http://127.0.0.1:7903`。"""
    m = _SLOT_URL_RE.search(proxy_slot)
    return m.group(1).strip() if m else None


def _scope_key_for_report(url: str) -> str:
    """查表失败时用来汇报的键 —— 优先 `host:port`（可直接拷进 `.env`）。"""
    try:
        host, port = config.slot_host_port(url)
    except ValueError:
        return url
    return f"{host}:{port}"


def load_email_to_ip(results_path: Path) -> dict[str, str]:
    """台账 → {email: 出口 IP}。

    用 `proxy_slot` 里的**真实槽位 URL** 过一遍 `config.slot_scope()` 换算成 IP
    —— 查表顺序只在那一处实现（见模块 docstring 里的理由）。
    换算不出来的跳过（宁可少迁，不可迁错），并把缺失的键报出来。
    """
    if not results_path.is_file():
        raise SystemExit(f"找不到 results 文件：{results_path}")
    # ⚠ 刻意**不吞**异常：文件损坏时这里就该失败 —— 下一步是改写台账，
    #   带着半份数据继续跑会把迁移**做一半**（且分布图看不出少了谁）。
    #   但要把"哪个文件"说清楚：原始 traceback 只给偏移量，
    #   对着一个几 MB 的台账根本定位不了。
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        raise SystemExit(f"results 文件不是合法 JSON：{results_path}\n  {ex}") from ex
    out: dict[str, str] = {}
    unknown: collections.Counter = collections.Counter()
    for rec in data:
        ps = rec.get("proxy_slot") or ""
        email = rec.get("email") or ""
        if not ps or not email:
            continue
        url = _slot_url(ps)
        if url is None:
            continue
        try:
            out[email] = config.slot_scope(url)
        except ValueError:
            unknown[_scope_key_for_report(url)] += 1
    if unknown:
        print("⚠ 这些槽位的出口 IP 不在 SLOT_EGRESS_IPS 里，已跳过（补进 .env 后重跑即可）：")
        for key, n in unknown.most_common():
            print(f"   {key}  ({n} 条)")
    return out


def read_ledger(path: Path) -> list[dict]:
    """逐行读台账。坏行**当场炸**（带行号），不静默跳过。

    为什么不能跳过坏行：这个文件的读入结果会被 `main()` 原样写回去，
    跳过一行就等于**默默删掉一个账号的配额记录**。而"能读出文件、但某行不是
    合法 JSON"恰恰意味着台账被人手改坏过 —— 那是最需要停下来看的情形。

    缺文件的情形由 `main()` 里的 `is_file()` 先挡住；到不了这里。
    """
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as ex:
            raise SystemExit(f"台账第 {lineno} 行不是合法 JSON：{path}\n  {ex}") from ex
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="把配额台账的 scope 从槽位位置号迁移成出口 IP")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--results", default=str(DEFAULT_RESULTS))
    ap.add_argument("--apply", action="store_true", help="真迁移（默认只报告）。会自动备份原台账。")
    args = ap.parse_args()

    ledger_path = Path(args.ledger)
    if not ledger_path.is_file():
        raise SystemExit(f"找不到台账：{ledger_path}")

    e2ip = load_email_to_ip(Path(args.results))
    rows = read_ledger(ledger_path)
    print(f"台账 {len(rows)} 条；台账能关联出出口 IP 的 {len(e2ip)} 条\n")

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
            pass  # 已经是 IP 形式了，幂等
        else:
            if old:
                unmapped.append(r)  # 有旧 scope 但关联不出真实出口
            r["scope"] = ""  # 池化前那批：本来就没有出口归属
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
        print(f"\n⚠ 有 {len(unmapped)} 条带着旧 scope 但关联不出真实出口，已清空 scope：")
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
