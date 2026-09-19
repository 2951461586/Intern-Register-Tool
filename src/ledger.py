"""账号台账（`results.json`）的读写与合并。

为什么单独成一个模块
--------------------
台账同时是 `run.py --out` 的默认目标，所以**一次小规模运行就可能把它覆盖掉**。
本项目已经栽过两次：

  1. `_backups/` 被清理 → 38 个账号的记录只剩导出 CSV 里有一份
  2. `run.py --count 1`（探测服务端是否解封）→ 把 53 条台账**覆盖成 1 条**

所以"合并而不是覆盖"这条规则必须**只有一处实现**，被所有会写台账的工具复用
（`run.py` / `tools/run_downstream.py` / `tools/data/recover_activation.py` 用
`merge_records`；`tools/data/restore_results.py` 用 `merge_fragments`）。

⚠ 两个入口的**降级行为不同**，且这个不同是有意的 —— 理由见 `merge_fragments`
的 docstring。想"顺手统一"之前先读那一段，2026-09-19 已经实测过统一会丢数据。
"""

import json
from pathlib import Path

# 记录"优劣"排序：成功 > 跳过 > 失败。合并时不让失败盖掉成功。
_RANK = {"success": 2, "skipped": 1}

# 合并碎片时判定"这个值算不算有内容"。
# ⚠ `0` / `False` **不在**这里 —— 它们是有效观测值（余额 0、验证不通过），
#   当成空值会让真实数据被后面的碎片覆盖掉。
_EMPTY = (None, "", {}, [])

# `merge_fragments` 里"这个键还没被任何碎片赋过值"的哨兵。
# 用独立对象而不是 `None`：`None` 本身是合法值（"明确记为空"）。
_UNSET = object()


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


def merge_fragments(records: list[dict]) -> dict:
    """把**同一个 email** 的多条碎片合成一条 —— 字段只增不减。

    与 `merge_records` 的分工（两者**都**要留着，别合并）
    ---------------------------------------------------
    |          | `merge_records`              | `merge_fragments`              |
    |----------|------------------------------|--------------------------------|
    | 场景     | 一次运行的结果并入台账       | 从散落来源**重建**台账          |
    | 输入     | `(existing, new)` 两个列表   | 同一账号的全部碎片，按来源优先级 |
    | 降级时   | **不动**（rank 门控）        | **仍要补缺口**                  |

    🔴 为什么降级时行为必须不同（2026-09-19 实测，不是口味问题）：

    运行期合并里，rank 更低的记录往往是**一次失败的尝试**，它的 `error` /
    中间态字段不该挂到一个已经成功的账号上 —— `tools/run_downstream.py:269`
    正是靠"并集 + 显式写空值"来清陈旧 `error` 的，`tests/test_ledger_merge.py`
    的 T9c 也钉死了"降级不产生任何改动"。

    而重建时，rank=0 的碎片是 `export_keys` 的**导出行**，它带的 `source` /
    `verify` 是这个账号的真实属性。实测：直接把 `restore_results` 改成调用
    `merge_records`，**15 个账号丢 18 个字段**（`source` × 15、`verify` × 3），
    且这 15 个账号本来**能**拿到理论最大字段集 —— 纯属规则太严导致的丢失。

    规则
    ----
      - `rank` **更高**的碎片可以覆盖值（成功 > 跳过 > 失败）
      - `rank` 同级或更低：只能**补缺口**，不覆盖已有的非空值
      - 空值（见 `_EMPTY`）永远不覆盖非空值
      - 键取并集：任何碎片里出现过的键，结果里一定有

    ⚠ `records[0]` 是**最高优先级**来源。调用方必须保证顺序 ——
      `tools/data/restore_results.py` 的 `results.json` → `--from` → `exports/`
      → `tmp/*.json` 就是这个顺序。同级碎片里，靠前的来源最后落笔因而胜出。

    键顺序刻意跟**来源优先级**一致（不是按 rank 排）：`results.json` 的字段
    在前，后来补上的 `source` / `verify` 在后。`results.json` 是人会直接看的
    台账，键顺序乱了读起来就费劲。
    """
    out: dict = {}
    for rec in records:                       # 先按优先级把键序固定下来
        for k in rec:
            out.setdefault(k, _UNSET)
    # 低优先级先写、高优先级后写 —— 靠"最后落笔者胜"实现来源优先级，
    # 而不是靠 `>=` 之类的比较，这样规则只有一条、不依赖任何启发式。
    for i in sorted(range(len(records)), key=lambda i: (rank(records[i]), -i)):
        for k, v in records[i].items():
            if out[k] is _UNSET or v not in _EMPTY:
                out[k] = v
    return out


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
