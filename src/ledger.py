"""账号台账（`ledger/` 目录）的读写与合并。

台账长什么样（2026-09-20 第二次改）
----------------------------------
    ledger/runs/2026-09-20/results-20260920-061230.json   ← **读源**（全量台账）
    ledger/runs/2026-09-20/results-20260920-055527.json   ← 上一份快照（回滚点）
    ledger/latest.json                                    ← **本批结果**（含失败/跳过）

两个文件职责**不同**，混用是本模块最大的坑：

  * `runs/` 里的快照 = **按 email 合并后的累计全量**；读源是**最新那一份**；
  * `latest.json`     = **最近一次落盘写进去的记录**，只给人看"这次写了什么"：
    跑批场景（`run.py`）= **本批**（含失败 / 跳过）；整本重写的工具
    （`prune_ledger` / `restore_results` / `run_downstream`）= 全量。

🔴 为什么 `latest.json` 不能当读源
---------------------------------
它只含本批那几十条。拿它当读源 ⇒ 下一次运行合并的基准只有上一批 ⇒
**台账停止累积、每次跑批覆盖上一次**。本项目栽过两次，第二次就是
`run.py --count 1`（探测服务端是否解封）把 53 条台账**覆盖成 1 条**。

而 `check_keys_alive.py` / `probe_login_only.py` / `migrate_quota_scope.py`
做的是**覆盖率对账**（"台账 N 把 / 快照 M 把"）。读源只有本批的话，它们会报
"全绿"但**只覆盖了最后一批** —— 这是本项目最警惕的**文件级静默缩水**：
行级缩水至少分母变了，文件级缩水连**分母本身**都是错的。

所以要"`latest.json` 只留本批"，前提是**全量台账另有归属** —— 就是快照。

为什么要单起目录
----------------
台账同时是 `run.py --out` 的默认目标，所以**一次小规模运行就可能把它覆盖掉**
（上面那个 53→1 的事故）。老布局还把"历史全量"和"这次跑出来的"塞进同一个
根文件，于是仓库根堆了一串手写备份（`results.json.bak-batch50b-*` …），
复盘时得靠时间戳猜哪个是哪个。新布局按**日期分目录 + 时间戳命名**，
快照本身就是可回滚点。

`latest.json` 用"写同目录临时文件 + 原子替换"刷新，读者永远看到一个完整的
JSON（不是软链 —— Windows 建软链要开发者模式，而"读不到台账"在本项目是
最高危的静默失败，`tests/conftest.py` 记过它在 CI 上炸出的三种形态）。
⚠ 这个写法的实现在 `src/fsutil.py`（2026-09-20 起与 `proxypool` / `quota`
  的另外两处共用一份）；本模块的 `_atomic_write()` 只是它的一层薄封装。

所以"合并而不是覆盖"这条规则必须**只有一处实现**，被所有会写台账的工具复用
（`run.py` / `tools/run_downstream.py` / `tools/data/recover_activation.py` 用
`merge_records`；`tools/data/restore_results.py` 用 `merge_fragments`）。

⚠ 两个入口的**降级行为不同**，且这个不同是有意的 —— 理由见 `merge_fragments`
的 docstring。想"顺手统一"之前先读那一段，2026-09-19 已经实测过统一会丢数据。
"""

import json
import re
import time
from pathlib import Path

from . import fsutil

# 台账目录：仓库根下的 `ledger/`（整个目录被 .gitignore 忽略 —— 里面是明文凭据）。
# ⚠ 用 `Path(__file__).resolve().parents[1]` 而不是 `Path.cwd()`：cwd 是调用者的，
#   从别的目录跑 `python tools/xxx.py` 会把台账写到别处去。
ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / "ledger"
RUNS_DIRNAME = "runs"
SNAPSHOT_STEM = "results"
# **本批结果**的文件名。⚠ 它不是台账、**不能当读源**（见模块 docstring）。
LAST_RUN_NAME = "latest.json"

# 快照文件名的**严格**形状：`results-YYYYMMDD-HHMMSS.json`。
# 🔴 用正则筛，不用 `glob("*.json")`：`runs/` 是个人工可写的目录，一个
#    `notes.json` / `results.json.bak` / `results-tmp.json` 落进去，按"最新"
#    （mtime 或字典序）就会被当成台账读源 —— 于是缩水护栏的分母、`--count`
#    的取样池全跟着变，而且**全程不报错**。
SNAPSHOT_RE = re.compile(rf"^{SNAPSHOT_STEM}-(\d{{8}})-(\d{{6}})\.json$")


def snapshot_path(when=None) -> Path:
    """新快照的落点：`ledger/runs/<YYYY-MM-DD>/results-<YYYYMMDD-HHMMSS>.json`。

    按**本地日期**分目录（复盘时想的是"那天那批"），文件名用 `YYYYMMDD-HHMMSS`
    时间戳 —— 与 `.workbuddy-ai/backups/results-20260918-103003.json` 既有命名一致。

    `when` 传 epoch 秒（不传 = 现在）。存在的唯一理由是让测试能钉住路径格式，
    生产代码不该传。
    """
    t = time.localtime() if when is None else time.localtime(when)
    return (
        LEDGER_DIR
        / RUNS_DIRNAME
        / time.strftime("%Y-%m-%d", t)
        / f"{SNAPSHOT_STEM}-{time.strftime('%Y%m%d-%H%M%S', t)}.json"
    )


def snapshot_paths() -> list:
    """`runs/` 下所有**命名合规**的快照，按时间**升序**。

    排序键取**文件名里的时间戳**，不取 mtime —— mtime 会被 `cp -p` / 解压 /
    同步工具改掉，而文件名是落盘那一刻写死的。`YYYYMMDD-HHMMSS` 定长零填充，
    所以字典序就是时间序，不需要解析。

    按 `(日期目录名, 文件名)` 两级排序：跨天的快照也排得对（同一天内文件名
    已经带日期，但万一有人把文件挪到别的日期目录下，两级排序至少不会错得更乱）。
    """
    runs = LEDGER_DIR / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [p for p in runs.glob(f"*/{SNAPSHOT_STEM}-*.json") if SNAPSHOT_RE.match(p.name)]
    return sorted(found, key=lambda p: (p.parent.name, p.name))


def ledger_path() -> Path:
    """台账的**唯一读源**：`runs/` 里**最新的那份全量快照**。

    所有"读台账"的工具都该用这个函数取路径，不要自己拼
    `ROOT / "results.json"`，也不要拿 `latest_path()` 顶替 —— 后者只含本批，
    读它会让合并退化成"每次从零开始"（本项目栽过两次，见模块 docstring）。
    路径一旦有两处实现，改布局时必然漏掉一处，而漏掉的表现是
    `load_existing()` 静默返回 `[]`（文件不存在不抛异常，见它的 docstring）。

    🔴 每次调用都**重新扫目录**，不要冻进模块级常量。
       `tools/run_downstream.py` 那种"先读台账 → 跑 → 再写回台账"的流程里，
       常量会一直指向**落盘之前**那份快照 ⇒ 写回去等于覆盖掉刚生成的新快照。

    没有任何快照时返回一个**永不存在的占位路径**：让 `load_existing()` 按契约
    返回 `[]`，而不是抛异常 —— 一个空的台账目录不该让工具起不来。
    """
    snaps = snapshot_paths()
    if snaps:
        return snaps[-1]
    return LEDGER_DIR / RUNS_DIRNAME / "__none__" / f"{SNAPSHOT_STEM}-none.json"


def last_run_path() -> Path:
    """**本批结果**的落点：`ledger/latest.json`。

    ⚠ 这不是台账、**不能当读源**。它只含最近一次落盘那批的记录，历史账号不在
      这里。读台账一律用 `ledger_path()`。
    """
    return LEDGER_DIR / LAST_RUN_NAME


def is_ledger_path(path) -> bool:
    """这个路径是不是落在**项目台账目录**里（而不是随便一个导出文件）。

    各工具用它决定"这是正式落盘（要留快照）还是导出到别处（不留）"。

    判据是"在 `LEDGER_DIR` 底下"，**不是**"等于读源" —— 读源每次落盘都换名字，
    拿它做相等比较必然漏：第二次落盘之后，`--out` 传进来的旧路径就"不是台账"了，
    于是它会退化成只写那一个文件、不留快照。
    """
    try:
        return Path(path).resolve().is_relative_to(LEDGER_DIR.resolve())
    except (OSError, ValueError):
        return False


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


# `api_key` 的前缀。抽成常量：覆盖统计与 `check_keys_alive.py` 的行级过滤
# 必须认同一套前缀，否则两边口径不一致会互相掩盖（一边当 key、另一边当噪音）。
KEY_PREFIX = "sk-"


def _coverage_of(ledger_side, known_side) -> tuple:
    """`(台账侧去重计数, 清单侧去重计数, 台账有而清单没有的集合)`。

    两侧的**空值都丢掉**：`None` / `""` 不是"一个缺失的条目"，
    把它们算进分母会让覆盖率看着更好看（分母虚高、差集虚小）。
    """
    a = {v for v in ledger_side if v}
    b = {k for k in known_side if k}
    return len(a), len(b), a - b


def key_coverage(records, known_keys) -> tuple:
    """台账里的 API Key 相对一份**外部清单**（如导出快照 CSV）的覆盖情况。

    返回 `(台账 key 数, 清单 key 数, 台账有而清单没有的 key 集合)`。

    🔴 为什么要有这个函数
    ---------------------
    `tools/ops/check_keys_alive.py` 的默认输入是**某次导出的 CSV 快照**，
    而它只报"存活 N/N" —— N 是**快照自己的**分母，不告诉你快照覆盖了多少台账。

    实测踩到（2026-09-20）：快照停在 3 天前、只有 53 把，台账里已有 407 把，
    于是跑出"53/53 全绿"这种**看着没问题、实则完全没覆盖本批**的结论。

    这与 `check_keys_alive.py` 里已有的"行级防静默缩水"（过滤掉非 `sk-` 行时
    必须把丢掉的条数报出来）是**同一类缺陷，只是高了一层**：文件级也会静默缩水，
    而且更难发现 —— 行级缩水至少分母变了，文件级缩水连**分母本身**都是错的。

    ⚠ 返回的 `missing` 只覆盖**台账有、清单没有**这一个方向。反方向
      （清单有而台账没有）是另一回事（可能台账被覆盖过），本函数**不判** ——
      别把"missing 为空"读成"数据没问题"。
    """
    return _coverage_of(
        [
            r.get("api_key")
            for r in records
            if isinstance(r, dict) and str(r.get("api_key") or "").startswith(KEY_PREFIX)
        ],
        known_keys,
    )


def account_coverage(records, known_emails) -> tuple:
    """台账里的**账号**（`email`）相对一份外部清单的覆盖情况。

    返回 `(台账账号数, 清单账号数, 台账有而清单没有的 email 集合)`。

    与 `key_coverage` 是同一道护栏的另一个字段 —— 触发场景不同：

    * `key_coverage` 管的是"**我核验了多少把 key**"（`check_keys_alive.py`）；
    * `account_coverage` 管的是"**我能从哪个池子里取账号**"
      （`tools/probes/probe_login_only.py` 用 `--offset/--count` 从 CSV 取号）。

    后者更隐蔽：探针会报"6/6 登录成功"，你完全看不出那 6 个号是
    **从 5 天前的 53 行快照**里取的，而台账里已经有 417 个账号。
    登录本身没问题，但**结论的适用范围**被静默限死了。

    ⚠ 与 `key_coverage` 一样，`missing` 是单向的（台账 − 清单）。
    """
    return _coverage_of([r.get("email") for r in records if isinstance(r, dict)], known_emails)


# 覆盖不足时的退出码。**与 `run.py` 的"防静默缩水"护栏同码**（那边在
# `except ValueError` 分支里也是 `return 3`）—— "护栏触发"在仓库里只该有
# 一个含义，别为每个工具各发明一个数字。
EXIT_COVERAGE_GAP = 3


def _repo_rel(path) -> str:
    """路径归一化成**相对仓库根**的 posix 串；不在仓库内则原样返回。

    🔴 artifact 里不能存绝对路径
    ---------------------------
    绝对路径带盘符和用户名，一旦这份 artifact 被拷进仓库就会撞泄漏闸门的
    `ABS_PATH_RE`。artifact 本身在 `.workbuddy-ai/` 下、是 gitignored 的，
    但"它不会被提交"不是一条靠人守得住的规则 —— 这里直接把形态消掉。

    ⚠ 落在仓库外的路径无法相对化，会原样返回绝对路径。这是**有意的**兜底：
       伪造一个假的相对路径，比如实暴露绝对路径更坏。
    """
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def coverage_block(ledger_n, known_n, missing, *, ledger=None, snapshot=None) -> dict:
    """把覆盖三元组整理成 artifact 里的 `coverage` 块。

    🔴 为什么它必须进 artifact，而不能只打印
    ---------------------------------------
    护栏只走 stdout 的话，**事后单独看 artifact**（或由它派生的报告）就是
    "53/53 全绿" —— 看不出它只覆盖了台账的 53/605。stdout 不随 artifact 走，
    而 artifact 才是被反复引用、被派生、几天后又被重新打开的那份东西。

    这是同一类缺陷的**第三层**：
      行级缩水（分母变了）→ 文件级缩水（分母本身是错的）→
      **artifact 级缩水（artifact 里根本没有"分母"这个概念）**。

    `ledger` / `snapshot` 是来源路径，过 `_repo_rel()` 去掉盘符。
    """
    return {
        "ledger_n": ledger_n,
        "known_n": known_n,
        "uncovered": len(missing),
        "covers_ledger": not missing,
        "ledger": _repo_rel(ledger),
        "snapshot": _repo_rel(snapshot),
    }


def coverage_exit_code(missing, *, allow_partial: bool = False) -> int:
    """覆盖不足且未显式放行 → `EXIT_COVERAGE_GAP`，否则 0。

    ⚠ 这是一个**策略锁**，不是"有分支的判据"：配套用例钉的是"默认不放行"
      这个决定本身。别把它读成覆盖了复杂逻辑。

    🔴 为什么退出码必须非 0
    ----------------------
    护栏原来只 `print`，`main()` 照样 `return 0` —— 任何脚本化调用都会读到
    "成功"。本项目对"静默"的容忍度是零：宁可让调用方炸，也不要让一份只覆盖
    53/605 的报告被当成全量结论。
    """
    if allow_partial or not missing:
        return 0
    return EXIT_COVERAGE_GAP


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
            continue  # 同 email 的旧重复记录，留第一条
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
            merged[pos] = rec  # 升级：整体替换（如 failed→success）
            upgraded += 1
        elif r_new == r_old:
            union = {**cur, **rec}  # 同级：并集，新值胜出
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
    for rec in records:  # 先按优先级把键序固定下来
        for k in rec:
            out.setdefault(k, _UNSET)
    # 低优先级先写、高优先级后写 —— 靠"最后落笔者胜"实现来源优先级，
    # 而不是靠 `>=` 之类的比较，这样规则只有一条、不依赖任何启发式。
    for i in sorted(range(len(records)), key=lambda i: (rank(records[i]), -i)):
        for k, v in records[i].items():
            if out[k] is _UNSET or v not in _EMPTY:
                out[k] = v
    return out


def _guard_no_shrink(records: list[dict], existing) -> None:
    """合并后条数**少于**原有就抛 `ValueError`（`existing=None` 表示不检查）。

    少数据但所有指标都"正常"是最坏的一类失败 —— 宁可报错也不要静默丢账号。
    逃生口是 `existing=[]`：显式声明"这次缩水是我要的"
    （`tools/data/prune_ledger.py` 就是唯一那个正当用法）。
    """
    if existing is None:
        return
    if len(records) < len(existing):
        raise ValueError(
            f"拒绝写盘：合并后 {len(records)} 条 < 原有 {len(existing)} 条"
            f"（防静默缩水）。确认要覆盖请显式调用 save(..., existing=[])"
        )


def _atomic_write(path, records: list[dict]) -> None:
    """写 JSON 台账：先落同目录临时文件再原子替换，读者永远看不到半个文件。

    🔴 直接 `write_text` 有真实的半截风险：台账约 1 MB，写到一半被 Ctrl-C /
       进程被杀，留下的就是一个**语法不完整的 JSON** ⇒ 下次 `load_existing()`
       返回 `[]` ⇒ 合并退化成"只有本次" ⇒ 静默丢台账。
       这不是理论风险 —— `merge_fragments` 整个函数就是为"从碎片重建"而写的。

    ⚠ 这里**保留 `_atomic_write` 这个名字**，而不是让三个调用点直接调
      `fsutil.atomic_write_text()`：`tests/test_ledger_layout.py` 的
      `test_snapshot_is_written_before_latest` 靠 monkeypatch 这个名字来验证
      "**先写快照、后刷 latest**"这个**顺序**。改名会让那条用例静默失效
      （monkeypatch 一个不存在的属性会直接报错，但如果只是把调用点换成
      `fsutil.`，spy 就再也拦不到，用例会以"calls == []"失败 —— 而失败原因
      看着像"没写文件"，跟改名毫无关联）。

    具体实现已归一 —— 2026-09-20 起三处原子写盘共用 `fsutil.atomic_write_text`。
    """
    fsutil.atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=2))


def save(path, records: list[dict], *, existing: list[dict] = None) -> Path:
    """写台账到**显式路径**，带防静默缩水护栏。返回实际落盘路径。

    走台账目录请用 `save_snapshot()` —— 这个函数只写你给的那个文件，
    **不落快照**。它存在是为了 `--out keys.json` 这类"我就想导一份到别处"的用法，
    以及 `prune_ledger` 那种"先写 tmp 再自己替换"的场景。
    """
    p = Path(path)
    if existing is None:
        existing = load_existing(p)
    _guard_no_shrink(records, existing)
    _atomic_write(p, records)
    return p


def save_snapshot(
    merged: list[dict], batch: list[dict] = None, *, existing: list[dict] = None, when=None
) -> tuple:
    """正式落盘：**全量快照** + **本批结果**。返回 `(快照路径, 本批路径)`。

    `快照路径` 同时就是**新的读源** —— 落盘之后 `ledger_path()` 会指向它。

    三个动作，顺序不能换：

      1. 护栏 —— `merged` 条数 < `existing` 就抛 `ValueError`（**先于任何写入**）；
      2. 写快照 `ledger/runs/<日期>/results-<时间戳>.json`，
         内容是 `merged`（**合并后的全量台账**）；
      3. 原子刷新 `ledger/latest.json`，内容是 `batch`（**本批**，不传则同 `merged`）。

    🔴 快照必须是**全量**。只存本批的话，下一次 `ledger_path()` 读到的最新快照
       就只有本批那几条 ⇒ 合并等于没做 ⇒ 台账被切碎 ⇒ `run.py --count 1`
       又变回 1 条。这正是"`latest.json` 不能当读源"的原因。
       护栏也只对快照有意义：`batch` 天然是子集，拿它比 `existing` 必然误报。

    🔴 先写快照、再刷本批。万一第 3 步挂了，读源仍是**上一份完整台账**，
       而新快照已经躺在 `runs/` 里可以人工捞回；反过来会变成
       "读源已更新、快照没留下"，这次落盘就没有回滚点了。

    `existing=None` 时以**当前读源**（`ledger_path()`）为准。

    ⚠ `merged` 必须是**合并后**的列表、`batch` 是**本批原始**结果。这里不做合并
      —— 合并规则只有 `merge_records` / `merge_fragments` 两处实现。
    """
    if existing is None:
        existing = load_existing(ledger_path())
    _guard_no_shrink(merged, existing)
    snap = snapshot_path(when)
    _atomic_write(snap, merged)
    last = last_run_path()
    _atomic_write(last, merged if batch is None else batch)
    return snap, last
