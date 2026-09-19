"""登录时序通用探针：控制「页面加载后闲置」与「逐字输入按键间隔」，
量出各段的真实耗时并给出优化判据。

为什么需要它
------------
单账号登录 ~15s，拆开是：

    goto → form_ready → typed → checkbox → warmup → captcha_ready

两个关键事实（都是实测得出，不是推断）：

1. **`captcha_wait` 从 `submit` 起算，恒为 ~4.3~4.5s**。
   对照组 4.23s、实验组（页面先闲置 15s）4.47s —— 闲置完全不改变它。
   → 说明它是 SDK 的固定处理时间，**不是**"页面加载后的收集窗口"。
   （早期把它当成"页面级窗口"是推断，已被这个实验否定。）

2. **第 1 轮总是偏慢**（captcha_wait 6.04s / 7.30s，后两轮 4.3~5.0s）
   → 冷启动。**对比两组时必须丢弃第 1 轮**，否则会把冷启动误读成配置差异。
   （"打字调快导致 captcha_wait 变长"的旧结论，很可能就是踩了这个坑。）

于是剩下唯一可压的就是 `typed`（实测 3.8~5.4s，占总时长 1/3）。
本探针就是为验证"打字调快到底省不省时间"而写。

用法
----
    # 对照组：当前生产配置
    python tools/probes/probe_login_timing.py --type-lo 45 --type-hi 110 \\
        --rounds 5 --drop-first

    # 实验组：打字调快
    python tools/probes/probe_login_timing.py --type-lo 15 --type-hi 40 \\
        --rounds 5 --drop-first

⚠ `--drop-first` 不是可选项，是**必须**：见上面第 2 条。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src import browser  # noqa: E402
from src.pipeline import AccountRecord, stage_register  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.tempmail import TempMailClient  # noqa: E402

KEYS = ["goto", "prewarm", "form_ready", "typed", "checkbox", "warmup",
        "captcha_ready"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prewarm", type=int, default=0,
                    help="goto 之后闲置的毫秒数（对应 IR_PREWARM_MS）")
    ap.add_argument("--type-lo", type=int, default=None,
                    help="逐字输入按键间隔下限 ms（对应 IR_TYPE_DELAY_LO）")
    ap.add_argument("--type-hi", type=int, default=None,
                    help="逐字输入按键间隔上限 ms（对应 IR_TYPE_DELAY_HI）")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--drop-first", action="store_true",
                    help="丢弃第 1 轮（冷启动）后再统计 —— 强烈建议开启")
    ap.add_argument("--gap", type=float, default=3.0, help="轮次间隔（秒）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # ── 一致性自检：环境变量写错就白跑一整轮（这个坑踩过） ──
    checks = [("PREWARM_MS", browser.PREWARM_MS, args.prewarm)]
    if args.type_lo is not None:
        checks.append(("TYPE_DELAY_LO", browser.TYPE_DELAY_LO, args.type_lo))
    if args.type_hi is not None:
        checks.append(("TYPE_DELAY_HI", browser.TYPE_DELAY_HI, args.type_hi))
    bad = [(n, got, want) for n, got, want in checks if got != want]
    print("[probe] 配置自检: " +
          "  ".join(f"{n}={got}" for n, got, _ in checks) +
          f"  一致={not bad}", flush=True)
    if bad:
        for n, got, want in bad:
            print(f"[probe] ✗ {n}: 实际 {got} != 期望 {want}", flush=True)
        return 2

    rows = []
    for i in range(args.rounds):
        print(f"\n{'─' * 70}\n[probe] 第 {i + 1}/{args.rounds} 轮"
              f"{'（冷启动，统计时丢弃）' if i == 0 and args.drop_first else ''}"
              f"\n{'─' * 70}", flush=True)
        rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        mail, sso = TempMailClient(), SSOClient()
        if not stage_register(mail, sso, rec, log=lambda m: None):
            print(f"[probe] 注册失败: {rec.error}", flush=True)
            continue

        res = browser.login(rec.email, rec.password, headless=True,
                                  timeout=90, attempts=1, verbose=False)
        cs = res.captcha_stage or {}
        tm = res.timings or {}
        stages, prev = {}, 0
        for k in KEYS:
            if k in tm:
                stages[k] = round((tm[k] - prev) / 1000, 2)
                prev = tm[k]

        # 关键节点时刻（毫秒 → 秒），用于定位 captcha_wait 双峰
        key_t = {}
        for t, kind, _ in (cs.get("events") or []):
            if kind in ("submit", "Init#1", "Init#2", "Verify#1") \
                    and kind not in key_t:
                key_t[kind] = round(t / 1000, 2)

        row = {
            "round": i + 1,
            "email": rec.email,
            "ok": res.ok,
            "path": cs.get("path"),
            "captcha_wait_s": round(cs.get("captcha_wait_ms", 0) / 1000, 2),
            "stages": stages,
            "total_s": round((tm.get("done") or 0) / 1000, 2),
            "verify_codes": cs.get("verify"),
            "key_events": key_t,
            "events": cs.get("events") or [],
        }
        rows.append(row)
        print(f"  ok={res.ok} path={row['path']} "
              f"typed={stages.get('typed', 0):.2f}s "
              f"captcha_wait={row['captcha_wait_s']:.2f}s "
              f"total={row['total_s']:.2f}s", flush=True)
        # 🔴 定位 captcha_wait 双峰：把 submit 之后的节点逐个摆出来
        #    Init#1 迟到 → SDK 初始化慢（与页面/网络有关）
        #    Verify#1 迟到 → 决策慢（与行为数据/风控有关）
        s = key_t.get("submit")
        if s is not None:
            seg = "  ".join(
                f"{k}@{key_t[k] - s:+.2f}s"
                for k in ("Init#1", "Init#2", "Verify#1")
                if k in key_t)
            print(f"    submit@{s:.2f}s   {seg}", flush=True)

        if i < args.rounds - 1:
            time.sleep(args.gap)

    # ── 汇总 ────────────────────────────────────────────────
    ok_all = [r for r in rows if r["ok"]]
    ok = ok_all[1:] if (args.drop_first and len(ok_all) > 1) else ok_all

    print(f"\n{'=' * 70}")
    print(f"SUMMARY type={args.type_lo}~{args.type_hi}ms prewarm={args.prewarm}ms  "
          f"成功={len(ok_all)}/{len(rows)}  统计样本={len(ok)}"
          f"{'（已丢弃第 1 轮冷启动）' if args.drop_first else ''}")
    print(f"{'=' * 70}")
    print(f"  {'#':>2s} {'ok':>3s} {'path':>4s} {'typed':>9s} {'captcha_wait':>13s} "
          f"{'total':>9s} {'typed+wait':>11s}")
    for r in rows:
        tw = r["stages"].get("typed", 0) + r["captcha_wait_s"]
        print(f"  {r['round']:>2d} {str(r['ok']):>3s} {str(r['path']):>4s} "
              f"{r['stages'].get('typed', 0):>8.2f}s {r['captcha_wait_s']:>12.2f}s "
              f"{r['total_s']:>8.2f}s {tw:>10.2f}s")

    if ok:
        def avg(key):
            return sum(key(r) for r in ok) / len(ok)
        print(f"\n  平均 typed        = {avg(lambda r: r['stages'].get('typed', 0)):.2f}s")
        print(f"  平均 captcha_wait = {avg(lambda r: r['captcha_wait_s']):.2f}s")
        print(f"  平均 typed+wait   = "
              f"{avg(lambda r: r['stages'].get('typed', 0) + r['captcha_wait_s']):.2f}s")
        print(f"  平均 登录总耗时    = {avg(lambda r: r['total_s']):.2f}s")
        paths = [r["path"] for r in ok]
        print(f"  通路分布          = {paths.count('A')}×Path A / "
              f"{paths.count('B')}×Path B")
        # 判据提示：如果 typed+wait 基本不变，说明总时长被守恒，压打字无收益
        tw = [r["stages"].get("typed", 0) + r["captcha_wait_s"] for r in ok]
        spread = max(tw) - min(tw)
        print(f"\n  typed+wait 极差 = {spread:.2f}s  "
              f"（{'≈ 守恒 → 压打字无收益' if spread < 2.0 else '有波动 → 值得再看'}）")
    print("=" * 70)

    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"[probe] 明细已写入 {args.out}", flush=True)
    return 0 if len(ok_all) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
