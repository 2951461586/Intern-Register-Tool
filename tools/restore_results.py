"""从各个散落来源**重建** `results.json`（账号台账）。

为什么需要它
------------
`results.json` 是**账号台账**，但它同时也是 `run.py --out` 的默认目标，
所以很容易被一次小规模运行覆盖掉。本项目已经栽过**两次**：

  1. `_backups/` 整个目录被清理 → 38 个账号的记录只剩导出 CSV 里有一份
  2. `run.py --count 1`（探测服务端是否解封）→ 把 53 条台账**覆盖成 1 条**
     （`run.py` 已修：现在默认按 email 合并，并有"条数不得变少"的硬护栏）

**第二次能救回来，纯靠 `tools/export_keys.py` 的导出文件。** 所以本工具
把"导出文件"也当成一等来源 —— 它经常是**唯一副本**。

来源与优先级
------------
按**字段丰富度**取优（同 email 合并，不是简单覆盖）：

| 来源 | 通常有哪些字段 |
|------|----------------|
| `results.json` | 最全（含 `jwt` / `sso_uid` / `stages`） |
| `--from` 指定的任意 json/csv | 看情况 |
| `.workbuddy-ai/exports/keys_export.csv` | email/username/password/api_key/key_id/credits |
| `.workbuddy-ai/tmp/*.json` | 历次实验的中间产物，可能带 `jwt` |

**合并规则**：同 email 取"字段更多"的那条；`status` 按 成功 > 跳过 > 失败
取优（不让失败盖掉成功）。JWT 这类只有部分来源有的字段会被**补进去**。

用法
----
    python tools/restore_results.py                    # 干跑，只报告
    python tools/restore_results.py --write            # 真写回 results.json
    python tools/restore_results.py --write --out x.json
    python tools/restore_results.py --from a.json --from b.csv --write
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".workbuddy-ai"
DEFAULT_OUT = ROOT / "results.json"

# 台账里"有意义的"字段。用来算丰富度，也用来报缺什么。
FIELDS = ["email", "username", "password", "sso_uid", "jwt",
          "api_key", "key_id", "credits", "status", "stages", "created_at"]

_RANK = {"success": 2, "skipped": 1}


def _rank(rec: dict) -> int:
    return _RANK.get(rec.get("status"), 0)


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


def _richness(rec: dict) -> int:
    """非空字段数 —— 用它决定同 email 时留哪条。"""
    n = 0
    for k in FIELDS:
        v = rec.get(k)
        if v not in (None, "", {}, []):
            n += 1
    return n


def _better(a: dict, b: dict) -> dict:
    """返回 a、b 里更该保留的那条（先比 status，再比字段数）。"""
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return a if ra > rb else b
    return a if _richness(a) >= _richness(b) else b


def _fill_missing(keep: dict, other: dict) -> dict:
    """把 `other` 里 `keep` 缺的字段补进去（例如只在一处有的 jwt）。"""
    out = dict(keep)
    for k, v in other.items():
        if out.get(k) in (None, "", {}, []) and v not in (None, "", {}, []):
            out[k] = v
    return out


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

    merged: dict[str, dict] = {}
    dropped = 0
    for p in paths:
        for rec in load_source(p):
            if not is_account(rec):
                dropped += 1
                continue
            email = rec["email"]
            if email not in merged:
                merged[email] = rec
            else:
                merged[email] = _fill_missing(_better(merged[email], rec), rec)
    out_recs = sorted(merged.values(), key=lambda r: r.get("created_at") or "")

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
              f"（`tools/probe_login_only.py --with-discovery`）")

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
