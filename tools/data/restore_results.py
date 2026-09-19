"""从各个散落来源**重建** `results.json`（账号台账）。

为什么需要它
------------
`results.json` 是**账号台账**，但它同时也是 `run.py --out` 的默认目标，
所以很容易被一次小规模运行覆盖掉。本项目已经栽过**两次**：

  1. `_backups/` 整个目录被清理 → 38 个账号的记录只剩导出 CSV 里有一份
  2. `run.py --count 1`（探测服务端是否解封）→ 把 53 条台账**覆盖成 1 条**
     （`run.py` 已修：现在默认按 email 合并，并有"条数不得变少"的硬护栏）

**第二次能救回来，纯靠 `tools/data/export_keys.py` 的导出文件。** 所以本工具
把"导出文件"也当成一等来源 —— 它经常是**唯一副本**。

来源与优先级
------------
按**来源顺序**取优（同 email 合并，不是简单覆盖）：

| 顺序 | 来源 | 通常有哪些字段 |
|------|------|----------------|
| 1 | `results.json` | 最全（含 `jwt` / `sso_uid` / `stages`） |
| 2 | `--from` 指定的任意 json/csv | 看情况 |
| 3 | `.workbuddy-ai/exports/keys_export.{json,csv}` | email/username/password/api_key/key_id/credits/verify/source |
| 4 | `.workbuddy-ai/tmp/*.json` | 历次实验的中间产物，可能带 `jwt` |

**合并规则**：见 `src/ledger.merge_fragments()` —— 键取并集、`rank` 更高者
可覆盖、同级/降级只补缺口、靠前的来源胜出。`status` 按 成功 > 跳过 > 失败 取优。

⚠ **不要**把这里改成 `ledger.merge_records()`。看着像重复，其实降级行为必须
不同：本工具处理的 rank=0 记录是 `export_keys` 的导出行（带 `source` /
`verify`，是账号的真实属性），而运行期合并里的 rank=0 记录往往是一次失败
尝试（`error` 不该挂到成功账号上）。2026-09-19 实测：改用 `merge_records`
会让 **15 个账号丢 18 个字段**。完整推导见 `src/ledger.merge_fragments` 的 docstring。

用法
----
    python tools/data/restore_results.py                    # 干跑，只报告
    python tools/data/restore_results.py --write            # 真写回 results.json
    python tools/data/restore_results.py --write --out x.json
    python tools/data/restore_results.py --from a.json --from b.csv --write
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src.ledger import merge_fragments  # noqa: E402

WORK = ROOT / ".workbuddy-ai"
DEFAULT_OUT = ROOT / "results.json"


def is_account(rec: dict) -> bool:
    """这条记录是否代表一个**真的注册成功了**的账号。

    🔴 不能只用"有 email 就算" —— 本项目 `.workbuddy-ai/tmp/` 里混着三类记录，
    只有一类是账号（2026-09-18 实测，差点把台账从 53 条灌成 93 条）：

    | 来源 | `stages` 是什么 | 是账号吗 |
    |------|----------------|---------|
    | 流水线结果（`opt6_*`） | 阶段字典，`register == "ok"` | ✓ |
    | 导出 CSV | 无 `stages`，但有 `api_key` | ✓ |
    | **验证码计时实验**（`type_*` / `prewarm_*`） | **计时字段**（goto/typed/checkbox…），`ok: true` 指的是**验证码通过**，不是注册成功；且**没有 username/password** | ✗ 不可用 |
    | 失败的注册（`opt6_w6` / `rec_*`） | `{}` 或 `{quota_blocked: B0000}` | ✗ |

    ⚠ `ok: true` 是**同名不同义**的陷阱字段 —— 计时实验里它表示"验证码过了"，
    绝不能当注册成功的判据。判据只看 `stages.register` / `api_key` / `status`。
    """
    if (rec.get("stages") or {}).get("register") == "ok":
        return True
    if rec.get("api_key"):
        return True
    return rec.get("status") == "success"


def load_source(p: Path) -> list[dict]:
    """读一个来源，产出记录列表。支持 `.json`（list 或 {"results": [...]}）与 `.csv`。"""
    try:
        if p.suffix.lower() == ".csv":
            with p.open(encoding="utf-8-sig") as f:
                return [dict(r) for r in csv.DictReader(f) if r.get("email")]
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as ex:
        print(f"  ✗ 跳过 {p.name}：{ex}")
        return []
    if isinstance(d, dict):
        d = d.get("results") or d.get("keys") or []
    if not isinstance(d, list):
        return []
    return [r for r in d if isinstance(r, dict) and r.get("email")]


def collect(extra: list[str]) -> tuple[list[dict], dict[str, int]]:
    """按优先级收集所有来源。返回 `(records, {来源标签: 条数})`。"""
    sources: list[Path] = []
    if DEFAULT_OUT.is_file():
        sources.append(DEFAULT_OUT)
    sources += [Path(x) for x in extra]
    # 导出文件 —— 经常是唯一副本，所以是默认来源
    sources += [WORK / "exports" / "keys_export.json",
                WORK / "exports" / "keys_export.csv"]
    # 历次实验的中间产物（可能带 jwt）
    sources += sorted((WORK / "tmp").glob("*.json"))

    seen, counts = [], {}
    for p in sources:
        if not p.is_file() or p in seen:
            continue
        seen.append(p)
        recs = load_source(p)
        if recs:
            counts[str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)] = len(recs)
    return seen, counts


def main() -> int:
    ap = argparse.ArgumentParser(description="从散落来源重建 results.json")
    ap.add_argument("--from", dest="extra", action="append", default=[],
                    help="额外来源（可多次）；目录则展开其中的 json/csv")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--write", action="store_true", help="真写盘（默认干跑）")
    args = ap.parse_args()

    extra = []
    for x in args.extra:
        p = Path(x)
        if p.is_dir():
            extra += [str(q) for q in sorted(p.glob("*.json")) + sorted(p.glob("*.csv"))]
        else:
            extra.append(str(p))

    paths, counts = collect(extra)
    print(f"扫描 {len(paths)} 个来源：")
    for name, n in counts.items():
        print(f"  {n:>4} 条  {name}")
    if not counts:
        print("✗ 所有来源都读不到记录")
        return 1

    # 按 email 分组。**组内顺序 = 来源优先级**（`paths` 是 collect() 排好的），
    # 这一点是 merge_fragments 的前提，不能打乱。
    grouped: dict[str, list[dict]] = {}
    dropped = 0
    for p in paths:
        for rec in load_source(p):
            if not is_account(rec):
                dropped += 1
                continue
            grouped.setdefault(rec["email"], []).append(rec)

    out_recs = sorted((merge_fragments(g) for g in grouped.values()),
                      key=lambda r: r.get("created_at") or "")

    if dropped:
        print(f"\n⚠ 丢弃 {dropped} 条**非账号**记录（验证码计时实验 / 注册失败 / "
              f"无凭据）—— 判据见 is_account()")
    ok = sum(1 for r in out_recs if r.get("status") == "success")
    with_key = sum(1 for r in out_recs if r.get("api_key"))
    with_jwt = sum(1 for r in out_recs if r.get("jwt"))
    with_pwd = sum(1 for r in out_recs if r.get("password"))
    print(f"\n合并后：**{len(out_recs)}** 个账号"
          f"（status=success {ok}，有 password {with_pwd}，"
          f"有 api_key {with_key}，有 jwt {with_jwt}）")

    missing = [k for k in ("username", "password", "api_key")
               if not any(r.get(k) for r in out_recs)]
    if missing:
        print(f"  ⚠ 完全缺字段：{'、'.join(missing)}")
    no_pwd = len(out_recs) - with_pwd
    if no_pwd:
        print(f"  ⚠ {no_pwd} 条缺 password —— 这些账号**无法登录**，"
              f"多半是验证码计时实验留下的（账号在服务端存在但凭据没记录）")
    no_jwt = len(out_recs) - with_jwt
    if no_jwt:
        print(f"  ⚠ {no_jwt} 条缺 jwt —— 可用密码重新登录取回"
              f"（`tools/probes/probe_login_only.py --with-discovery`）")

    out = Path(args.out)
    if not args.write:
        print(f"\n（干跑）要写回 {out} 请加 --write")
        return 0

    # 硬护栏：不能比现有文件更少
    if out.is_file():
        old = len(load_source(out))
        if len(out_recs) < old:
            print(f"✗ 重建后 {len(out_recs)} 条 < 现有 {old} 条，拒绝写盘",
                  file=sys.stderr)
            return 2
    out.write_text(json.dumps(out_recs, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n✓ 已写回 {out}（{len(out_recs)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
