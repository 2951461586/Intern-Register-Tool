"""账号台账（`results.json`）的读写与合并。

为什么单独成一个模块
--------------------
台账同时是 `run.py --out` 的默认目标，所以**一次小规模运行就可能把它覆盖掉**。
本项目已经栽过两次：

  1. `_backups/` 被清理 → 38 个账号的记录只剩导出 CSV 里有一份
  2. `run.py --count 1`（探测服务端是否解封）→ 把 53 条台账**覆盖成 1 条**

所以"合并而不是覆盖"这条规则必须**只有一处实现**，被所有会写台账的工具复用
（`run.py` / `tools/run_downstream.py` / `tools/restore_results.py`）。
"""

import json
from pathlib import Path

# 记录"优劣"排序：成功 > 跳过 > 失败。合并时不让失败盖掉成功。
_RANK = {"success": 2, "skipped": 1}


def rank(rec: dict) -> int:
    return _RANK.get(rec.get("status"), 0)


def load_existing(path) -> list[dict]:
    """读现有台账。文件缺失 / 损坏 / 不是 list 都返回 `[]`，不抛异常。

    刻意不抛：一个坏掉的结果文件不该让整次运行失败 —— 那是"清理一次数据
    反而连活都干不了"。真正的护栏在 `merge_records` 的条数检查上。
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [r for r in d if isinstance(r, dict)] if isinstance(d, list) else []


def merge_records(existing: list[dict], new: list[dict]):
    """按 `email` 合并，返回 `(merged, 原有条数, 新增, 覆盖数)`。

    规则：
      - 按 `email` 去重；没有 `email` 的记录原样保留（不参与去重）
      - 同一 email：**不让失败盖掉成功**（服务端抖一下不该把好账号标成坏）
      - 同一 email 且 rank 相同时：**取并集**（`{**旧, **新}`）——
        新值胜出，但旧记录里独有的字段一个都不丢
      - 老记录里本次没跑到的，**保留** —— 它们是历史，不是垃圾

    🔴 为什么同级要取并集而不是"整条替换"或"比字段数"（2026-09-18 踩到）：
    `tools/run_downstream.py` 交回的是**增量字段**（jwt / credits / verify /
    登录耗时），**没有 `status`** → `rank` 恒为 0。旧记录要么 rank=2
    （success）、要么 rank=0（从 CSV 恢复的），于是 `rank(new) > rank(old)`
    **永远为假**：下游跑了半天，字段一个都没写进台账，而打印出来的一切
    都"正常"。这正是本项目最警惕的一类失败 —— **数据静默缩水，指标全绿**。

    也试过"比非空字段个数"（richness），同样是启发式：两边字段数**相等**时
    照样丢字段（自测 T9 当场抓到）。并集没有这个漏洞 —— 它不靠猜，
    数学上保证字段只增不减。
    """
    idx: dict[str, int] = {}
    merged: list[dict] = []
    for rec in existing:
        email = rec.get("email")
        if email and email in idx:
            continue                      # 同 email 的旧重复记录，留第一条
        if email:
            idx[email] = len(merged)
        merged.append(rec)

    kept = len(merged)
    added = upgraded = 0
    for rec in new:
        email = rec.get("email")
        if not email or email not in idx:
            if email:
                idx[email] = len(merged)
            merged.append(rec)
            added += 1
            continue
        pos = idx[email]
        cur = merged[pos]
        r_new, r_old = rank(rec), rank(cur)
        if r_new > r_old:
            merged[pos] = rec                     # 升级：整体替换（如 failed→success）
            upgraded += 1
        elif r_new == r_old:
            union = {**cur, **rec}                # 同级：并集，新值胜出
            if union != cur:
                merged[pos] = union
                upgraded += 1
    return merged, kept, added, upgraded


def save(path, records: list[dict], *, existing: list[dict] = None) -> None:
    """写台账，带**防静默缩水**护栏。

    `existing` 传入"写之前文件里有什么"。合并后条数若**少于**原有，
    直接抛 `ValueError` —— 少数据但所有指标都"正常"是最坏的一类失败，
    宁可报错也不要静默丢掉账号。
    """
    p = Path(path)
    if existing is None:
        existing = load_existing(p)
    if len(records) < len(existing):
        raise ValueError(
            f"拒绝写盘：合并后 {len(records)} 条 < 原有 {len(existing)} 条"
            f"（防静默缩水）。确认要覆盖请显式调用 save(..., existing=[])")
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                 encoding="utf-8")
