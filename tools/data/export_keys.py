"""导出历史提取到的 API Key。

为什么单独做成工具
------------------
key 散落在**多个** `results.json` 与**历史备份 zip** 里（本项目：项目内 11 个 +
备份 zip 20 个）。手工翻既容易漏、又容易把重复的算两遍 —— 同一批文件可能被
多次导出/备份，同一把 key 会在多处出现。

三重去重口径（都要过）：
  1. **按 api_key 去重** —— 同一把 key 在多个文件里出现只算一次
  2. **按 email 交叉核对** —— 同一邮箱出现两把不同的 key 要**报警**，
     因为这通常意味着"重复建 key"或"记录被覆盖"，是需要人看一眼的异常
  3. 只收 `sk-` 开头的（历史上有过把 `sk- key`（含空格）误判成 key 的教训）

安全
----
输出含**明文 key**。默认写到 `.workbuddy-ai/exports/`（被 .gitignore 整个排除），
并且**每次都会用 `git check-ignore` 验证输出目录真的被忽略** —— 没被忽略就
直接拒绝写盘。不要把这层检查当成多余：`*.csv` / `*.txt` 并不在 .gitignore 里，
一旦输出到项目根就会变成可提交的明文凭据。

用法
----
    python tools/data/export_keys.py                        # 用默认来源
    python tools/data/export_keys.py --source a.json --source b.zip
    python tools/data/export_keys.py --out /tmp/keys
"""

import argparse
import csv
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from _path import ROOT  # noqa: E402  （副作用：把 tools/ 与仓库根加进 sys.path）

DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports"
KEY_PREFIX = "sk-"

FIELDS = ["created_at", "email", "username", "password", "api_key",
          "key_id", "credits", "verify", "source"]


# ────────────────────────────────────────────────────────────────
# 来源扫描
# ────────────────────────────────────────────────────────────────
def iter_json_blobs(paths):
    """产出 `(来源标签, 记录列表)`。

    支持四种来源：
      - 目录（递归找 `.json`）
      - `.json`（本工具的输出格式：list[dict]）
      - `.zip`（读其中所有 `.json`）
      - **`.csv`（本工具自己的输出 `keys_export.csv`）**

    🔴 为什么要读自己的 CSV：**导出文件可能变成唯一副本**。
    本项目就发生过 —— 历史备份 zip 被删掉后，38 个账号的 key 只剩
    `keys_export.csv` 里有；而本工具原先只认 JSON，重跑一次就会从 53 把
    掉到 15 把（静默缩水，最坏的一种失败）。把 CSV 也当来源，导出才能**自保**。
    """
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"  ⚠ 来源不存在，跳过：{p}")
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*.json")):
                try:
                    yield str(f), json.loads(f.read_text(encoding="utf-8"))
                except (ValueError, OSError) as ex:
                    print(f"  ⚠ 解析失败，跳过 {f}: {ex}")
        elif p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if not n.lower().endswith(".json"):
                            continue
                        try:
                            yield f"{p.name}!{n}", json.loads(
                                z.read(n).decode("utf-8"))
                        except (ValueError, KeyError, UnicodeDecodeError):
                            continue
            except (zipfile.BadZipFile, OSError) as ex:
                print(f"  ⚠ zip 读取失败，跳过 {p}: {ex}")
        elif p.suffix.lower() == ".csv":
            try:
                with p.open(encoding="utf-8-sig") as f:
                    # CSV 已经是我们输出的扁平结构，直接当记录用
                    yield str(p), [dict(r) for r in csv.DictReader(f)]
            except (OSError, csv.Error) as ex:
                print(f"  ⚠ CSV 读取失败，跳过 {p}: {ex}")
        else:
            try:
                yield str(p), json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError) as ex:
                print(f"  ⚠ 解析失败，跳过 {p}: {ex}")


def collect(paths):
    """返回 `(records, stats)`。records 已按 api_key 去重并按时间排序。"""
    by_key: dict[str, dict] = {}
    email_to_keys: dict[str, set] = {}
    stats = {"files": 0, "raw_records": 0, "with_key": 0, "dup_key": 0,
             "bad_prefix": 0}

    for src, blob in iter_json_blobs(paths):
        stats["files"] += 1
        if not isinstance(blob, list):
            continue
        for rec in blob:
            if not isinstance(rec, dict):
                continue
            stats["raw_records"] += 1
            key = (rec.get("api_key") or "").strip()
            if not key:
                continue
            if not key.startswith(KEY_PREFIX):
                # 只认字面量前缀 —— 历史上正则放宽导致把 "sk- key"（含空格）
                # 也算成 key，统计虚高。
                stats["bad_prefix"] += 1
                continue
            stats["with_key"] += 1
            email = (rec.get("email") or "").strip()
            email_to_keys.setdefault(email, set()).add(key)
            if key in by_key:
                stats["dup_key"] += 1
                continue
            stages = rec.get("stages") or {}
            by_key[key] = {
                "created_at": rec.get("created_at") or "",
                "email": email,
                "username": rec.get("username") or "",
                "password": rec.get("password") or "",
                "api_key": key,
                "key_id": rec.get("key_id") or "",
                "credits": rec.get("credits") or "",
                # 来源可能是 results.json（verify 在 stages 里）或本工具自己的
                # CSV（verify 是顶层列）—— 两种都要认，否则二次导出会丢这一列。
                "verify": rec.get("verify") or stages.get("verify") or "",
                "source": src,
            }

    # 同一邮箱多把 key → 异常，必须让人看见
    multi = {e: sorted(ks) for e, ks in email_to_keys.items() if len(ks) > 1}

    recs = sorted(by_key.values(), key=lambda r: r["created_at"])
    stats["unique_keys"] = len(recs)
    stats["unique_emails"] = len(email_to_keys)
    stats["multi_key_emails"] = multi
    return recs, stats


# ────────────────────────────────────────────────────────────────
# 安全：输出目录必须在 gitignore 覆盖范围内
# ────────────────────────────────────────────────────────────────
def _is_git_ignored(path: Path) -> bool:
    """用 `git check-ignore` 判定 —— 不要靠"看 .gitignore 里的模式"推断。"""
    probe = path if path.suffix else path / "_probe"
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(probe)],
                           cwd=str(ROOT), capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="导出历史提取的 API Key")
    ap.add_argument("--source", action="append", default=None,
                    help="来源文件/目录/zip，可重复。默认扫项目 tmp + 备份 zip")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--force", action="store_true",
                    help="即使输出目录未被 gitignore 覆盖也写盘（危险）")
    args = ap.parse_args()

    if args.source:
        sources = args.source
    else:
        sources = [str(ROOT / ".workbuddy-ai" / "tmp")]
        backups = ROOT.parent / "_backups"
        if backups.is_dir():
            sources += [str(z) for z in sorted(backups.glob("*.zip"))]
        # 🔴 把**上一次的输出**也当来源：历史备份可能被删，那时 CSV 就是唯一副本，
        #    不带它重跑会让导出**静默缩水**（本项目真发生过：53 → 15）。
        prev = DEFAULT_OUT / "keys_export.csv"
        if prev.is_file():
            sources.append(str(prev))

    print(f"来源 {len(sources)} 个：")
    for s in sources:
        print(f"  - {s}")

    recs, stats = collect(sources)

    print(f"\n扫描 {stats['files']} 个 json，{stats['raw_records']} 条记录")
    print(f"  含 api_key：{stats['with_key']} 条"
          f"（前缀不符 {stats['bad_prefix']} 条，按 key 去重掉 {stats['dup_key']} 条）")
    print(f"  → 唯一 key {stats['unique_keys']} 把 / 唯一邮箱 {stats['unique_emails']} 个")

    multi = stats["multi_key_emails"]
    if multi:
        print(f"\n  ⚠ {len(multi)} 个邮箱对应**多把** key（需要人看一眼，"
              f"通常是重复建 key 或记录被覆盖）：")
        for e, ks in list(multi.items())[:10]:
            print(f"      {e}: {len(ks)} 把")
    if stats["unique_keys"] != stats["unique_emails"]:
        print(f"\n  ⚠ 唯一 key 数({stats['unique_keys']}) ≠ 唯一邮箱数"
              f"({stats['unique_emails']}) —— 正常应相等（一个账号一把 key）")

    if not recs:
        print("\n没有可导出的 key。")
        return 1

    out = Path(args.out)
    if not args.force and not _is_git_ignored(out):
        print(f"\n✗ 拒绝写盘：{out} 未被 .gitignore 覆盖。")
        print("  输出含**明文 key**，且 *.csv / *.txt 并不在 .gitignore 里 ——")
        print("  写到那里会变成可提交的凭据。改用默认目录，或加 --force 自负风险。")
        return 2

    out.mkdir(parents=True, exist_ok=True)
    csv_p = out / "keys_export.csv"
    json_p = out / "keys_export.json"
    txt_p = out / "keys_only.txt"

    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    json_p.write_text(json.dumps(recs, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    txt_p.write_text("".join(r["api_key"] + "\n" for r in recs),
                     encoding="utf-8")

    dates = [r["created_at"] for r in recs if r["created_at"]]
    print(f"\n已导出 {len(recs)} 把 key → {out}")
    print(f"  {csv_p.name}      （表格：邮箱/账号/密码/key/额度/时间/来源）")
    print(f"  {json_p.name}     （完整记录）")
    print(f"  {txt_p.name}      （纯 key 列表，一行一把）")
    if dates:
        print(f"  时间范围：{min(dates)} ~ {max(dates)}")
    print("\n⚠ 这些是**明文凭据**，不要提交到仓库、不要贴到公开场合。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
