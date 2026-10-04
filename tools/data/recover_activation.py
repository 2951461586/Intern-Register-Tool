#!/usr/bin/env python
"""补激活：把「注册成功但激活失败」的账号救回来。

什么时候需要它
--------------
注册和激活是两步。注册成功 = 服务端已经有这个账号了；激活失败 ≠ 账号不存在，
只是**我们没把激活请求发出去**（通常因为读不到激活邮件）。这种账号白扔了可惜，
而这个工具就是把它们捞回来。

2026-09-18 的实例：邮箱 Worker 的 `/admin/all` 间歇性抛 Cloudflare
`Error 1101`（实测 25 次里成功 1 次 ≈ 4%），旧代码第一枪 500 就判账号失败
—— 4 个**已经注册成功**的账号全被标成 failed，而激活邮件好好躺在 D1 里。
修掉 `wait_for_mail` 的 5xx 重试之后，这些账号可以直接补激活。

判据
----
对每个目标邮箱：
  1. 从邮箱 Worker 拉最新邮件（**重试 5xx**），按收件人 + 发件人筛出激活邮件
  2. 从链接里取 `token` / `sign`，调 `POST /register/active`
  3. `success: true` → 这个账号救回来了

用法：
    # 从台账里自动挑"注册成功、激活失败"的账号
    #   `--from` 必须传**台账读源**（最新那份全量快照）。取路径：
    #     python -c "from src import ledger; print(ledger.ledger_path())"
    #   ⚠ 别传 `ledger/latest.json` —— 它只含最近一批那几十条，历史账号不在
    #     里面，候选会少一个数量级，而且**不报错**。
    python tools/data/recover_activation.py --from <台账读源>

    # 指定邮箱
    python tools/data/recover_activation.py --emails a@x.com,b@x.com

    # 只列出候选、不真发激活请求
    python tools/data/recover_activation.py --from <台账读源> --dry-run

    # 救回来的结果写回台账（默认只打印）
    python tools/data/recover_activation.py --from <台账读源> --write

退出码：0 = 全部救回（或 dry-run）；1 = 有账号仍没救回来。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from common import config  # noqa: E402
from src import ledger  # noqa: E402
from src.mailbox import MailboxSource, make_source  # noqa: E402
from src.proxypool import build_pool  # noqa: E402
from src.sso import SSOClient  # noqa: E402


def is_activation_failure(rec: dict) -> bool:
    """注册成功、但卡在激活那一步。

    🔴 判据要卡在 `stages.register == "ok"` 上，不能只看 `status == "failed"`：
    注册本身失败的账号（`B0000` 之类）**服务端没有这个账号**，补激活无从谈起。
    """
    if rec.get("stages", {}).get("register") != "ok":
        return False
    if rec.get("stages", {}).get("activate") == "ok":
        return False
    return rec.get("status") != "success"


def pick_from_ledger(path: Path) -> list[str]:
    """从台账里挑出「注册成功、但卡在激活」的邮箱。

    🔴 文件坏掉时**当场失败**，绝不返回空列表：本函数的返回值决定"要去救
       哪些账号"，返回 `[]` 等于对外宣称"没有需要救的" —— 而那是这里最坏的
       一种假绿（真有一批账号等着救，工具却说没事）。缺文件的情形由调用方
       的 `is_file()` 先挡住。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        raise SystemExit(f"台账不是合法 JSON：{path}\n  {ex}") from ex
    if isinstance(data, dict):  # 兼容 {"records": [...]}
        data = data.get("records", [])
    out = []
    for rec in data:
        if isinstance(rec, dict) and is_activation_failure(rec) and rec.get("email"):
            out.append(rec["email"])
    return out


def recover_one(
    mail: MailboxSource, email: str, *, proxy: str | None = None, timeout: int = 120, log=print
) -> dict:
    """救一个账号。返回 `{"email", "verdict", "detail"}`。"""
    t0 = time.time()
    m = mail.wait_for_mail(email, timeout=timeout)
    if not m:
        why = getattr(mail, "last_error", "") or "在轮询窗口内没等到这封邮件"
        log(f"  ✗ 没拿到邮件：{why}")
        return {
            "email": email,
            "verdict": "mail_missing",
            "detail": why,
            "seconds": round(time.time() - t0, 1),
        }

    link = m.find_link("active", "activat", "verif", "confirm")
    if not link:
        log(f"  ✗ 邮件里没有激活链接（subject={m.subject[:40]!r}）")
        return {
            "email": email,
            "verdict": "no_link",
            "detail": f"subject={m.subject[:60]}",
            "seconds": round(time.time() - t0, 1),
        }

    sso = SSOClient(proxy=proxy)
    try:
        ok = sso.activate_from_url(link)
    except Exception as ex:  # noqa: BLE001
        detail = f"{type(ex).__name__}: {ex}"[:160]
        log(f"  ✗ 激活请求失败：{detail}")
        return {
            "email": email,
            "verdict": "activate_error",
            "detail": detail,
            "seconds": round(time.time() - t0, 1),
        }

    verdict = "recovered" if ok else "activate_rejected"
    log(f"  {'✅ 救回' if ok else '✗ 服务端返回 success=false'}（{time.time() - t0:.1f}s）")
    return {"email": email, "verdict": verdict, "detail": "", "seconds": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description="补激活：救回注册成功但激活失败的账号")
    ap.add_argument("--emails", help="逗号分隔的邮箱列表")
    ap.add_argument("--from", dest="src", help="从台账 JSON 自动挑候选")
    ap.add_argument("--timeout", type=int, default=120, help="每个账号等邮件的最长秒数（默认 120）")
    ap.add_argument(
        "--no-slot",
        action="store_true",
        help="不用槽位代理（默认有槽位就用，与注册时出口保持一致）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不发激活请求")
    ap.add_argument("--write", action="store_true", help="把救回的账号写回台账（默认只打印）")
    ap.add_argument("--out", default=str(ledger.ledger_path()), help="台账路径（配合 --write）")
    ap.add_argument(
        "--report", default=str(ROOT / ".workbuddy-ai" / "exports" / "activation_recovery.json")
    )
    args = ap.parse_args()

    if args.emails:
        targets = [e.strip() for e in args.emails.split(",") if e.strip()]
    elif args.src:
        p = Path(args.src)
        if not p.is_file():
            print(f"✗ 台账不存在：{p}", file=sys.stderr)
            return 1
        targets = pick_from_ledger(p)
    else:
        print("需要 --emails 或 --from。", file=sys.stderr)
        return 1

    if not targets:
        print("没有候选账号（判据：stages.register == 'ok' 且 activate 未成功）。")
        return 0

    print(f"候选 {len(targets)} 个：")
    for e in targets:
        print(f"  - {e}")

    if args.dry_run:
        print("\n（--dry-run：只列候选，未发任何请求）")
        return 0

    # 出口：有槽位池就按槽位轮转，与注册时保持"同一出口"的直觉。
    # 激活不在注册配额上，所以这里用不用代理都不影响成败；用它是为了
    # 不让激活请求和注册请求的出口差异太大。
    pool = None if args.no_slot else build_pool()
    if pool:
        print(f"\n🔀 用槽位池出口（{pool.describe()}）")
    else:
        print("\n（未启用槽位池，走全局 IR_PROXY / 直连）")

    mail = make_source()
    results = []
    print()
    for i, email in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {email}")
        lease = None
        try:
            if pool:
                lease = pool.acquire(timeout=config.IR_PROXY_SLOT_TIMEOUT)
            results.append(
                recover_one(mail, email, proxy=(lease.url if lease else None), timeout=args.timeout)
            )
        finally:
            if pool and lease:
                pool.release(lease)

    ok = [r for r in results if r["verdict"] == "recovered"]
    print(f"\n{'=' * 62}")
    print(f"救回 {len(ok)} / {len(targets)}")
    for r in results:
        if r["verdict"] != "recovered":
            print(f"  ✗ {r['email']}  {r['verdict']}  {r['detail'][:70]}")
    if pool:
        print(f"🔀 {pool.describe()}")

    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "targets": targets,
                "results": results,
                "recovered": [r["email"] for r in ok],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # 同 probe_slots.py：`--report` 可指到仓库外，别让打印把成功运行搞崩。
    try:
        shown = rp.relative_to(ROOT)
    except ValueError:
        shown = rp
    print(f"报告已落盘：{shown}")

    if args.write and ok:
        # 复用 ledger 的并集合并 —— 不能自己写一遍，那条规则只有一处实现。
        out = Path(args.out)
        existing = ledger.load_existing(out)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        updates = []
        for r in ok:
            updates.append(
                {
                    "email": r["email"],
                    "status": "success",
                    "error": "",
                    "stages": {"register": "ok", "activate": "ok"},
                    "timings": {"activation_recovered_at": now},
                }
            )
        # 🔴 `merge_records` 返回 **4** 项 `(merged, kept, added, upgraded)`。
        #    这里只用得到 `merged` 与 `upgraded`，但**解包个数必须写满 4** ——
        #    原先写成 `merged, upgraded = ...` ⇒ 加了 `--write` 且真救回账号时
        #    当场 `ValueError: too many values to unpack`（2026-10-04 修）。
        #    这类"返回元组变长、调用点没跟上"现在有元测试盯着：
        #    tests/test_repo_hygiene.py::test_merge_records_call_sites_unpack_all_four_values
        merged, _kept, _added, upgraded = ledger.merge_records(existing, updates)
        if ledger.is_ledger_path(out):
            # 目标在台账目录里 ⇒ 走台账目录：留一份日期/时间戳快照（随即成为
            # **新读源**），并把合并后的全量刷进 `latest.json`。
            # 这是"整本重写"而不是跑批，所以不传 `batch`（默认 = 全量）。
            snap, last = ledger.save_snapshot(merged, existing=existing)
            print(
                f"台账已更新：{len(updates)} 条激活状态写回 {snap}"
                f"（upgraded={upgraded}）\n  本批结果 {last}"
            )
        else:
            ledger.save(out, merged, existing=existing)
            print(f"台账已更新：{len(updates)} 条激活状态写回 {out}（upgraded={upgraded}）")
    elif ok:
        print("（未加 --write，台账没改；想写回加 --write）")

    return 0 if len(ok) == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
