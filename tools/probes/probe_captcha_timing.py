"""对照实验：验证码初始化等待期间做鼠标微移动，究竟加快还是拖慢了
TRACELESS→CHECK_BOX 的降级时序？

背景
----
`_run_attempt` 第 7 步原本是纯空转等待（~4s，零输入事件、零行为数据）。
优化后改成"边等边做鼠标微移动"（`_micro_move`），理论上把暖场开销降到 0。
但优化后的 `captcha_ready` 实测 8.19s / 6.66s，看起来**比优化前的 4s 还慢**。
单个粗粒度耗时标记无法归因 —— 必须拿到事件时间线才知道时间花在哪。

做法
----
`MICRO_MOVE` 在模块 import 时从环境变量读取，同进程无法切换，
因此**每种模式各起一个进程**（由外层 shell 设 IR_NO_MICRO_MOVE=1 控制）。

每个进程内跑 N 轮：建邮箱 → 注册 → 激活 → 登录（打印事件时间线）。
对比指标：captcha_ready 耗时、InitCaptchaV3 到达时刻、首次点击通过率。

用法
----
    # 实验组（微移动开，即生产默认）
    python tools/probes/probe_captcha_timing.py --mode on --rounds 3

    # 对照组（微移动关，纯等待）
    IR_NO_MICRO_MOVE=1 python tools/probes/probe_captcha_timing.py --mode off --rounds 3
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["on", "off"], required=True,
                    help="on=实验组(微移动) off=对照组(纯等待)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=4.0, help="轮次间隔（秒），避开注册限流")
    ap.add_argument("--budget", type=float, default=None,
                    help="微移动预算秒数（对应 IR_MICRO_BUDGET），仅 mode=on 有意义")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # 一致性自检：环境变量是否真的生效（写错就白跑一整轮）
    flag_ok = (browser.MICRO_MOVE == (args.mode == "on"))
    if args.budget is not None and args.mode == "on":
        flag_ok = flag_ok and abs(browser.MICRO_BUDGET_S - args.budget) < 1e-6
    print(f"[probe] mode={args.mode}  MICRO_MOVE={browser.MICRO_MOVE}  "
          f"budget={browser.MICRO_BUDGET_S}s  一致={flag_ok}", flush=True)
    if not flag_ok:
        print("[probe] ✗ 环境变量与 --mode/--budget 不一致，请检查 "
              "IR_NO_MICRO_MOVE / IR_MICRO_BUDGET", flush=True)
        return 2

    rows = []
    for i in range(args.rounds):
        print(f"\n{'─' * 68}\n[probe] {args.mode} 第 {i + 1}/{args.rounds} 轮\n{'─' * 68}",
              flush=True)
        rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        mail, sso = TempMailClient(), SSOClient()
        if not stage_register(mail, sso, rec, log=lambda m: print(f"  {m}", flush=True)):
            print(f"[probe] 注册失败: {rec.error}", flush=True)
            continue

        t0 = time.time()
        res = browser.login(rec.email, rec.password, headless=False,
                                  timeout=60, attempts=1, verbose=True)
        wall = round(time.time() - t0, 2)

        cs = res.captcha_stage or {}
        events = cs.get("events") or []
        row = {
            "mode": args.mode,
            "round": i + 1,
            "email": rec.email,
            "ok": res.ok,
            "reason": res.reason,
            "wall_s": wall,
            "path": cs.get("path"),
            "captcha_wait_s": round(cs.get("captcha_wait_ms", 0) / 1000, 2),
            "captcha_ready_s": round((res.timings or {}).get("captcha_ready", 0) / 1000, 2),
            "init": cs.get("init"),
            "traceless_reject": cs.get("traceless_reject"),
            "verify_codes": cs.get("verify"),
            "mouse": cs.get("mouse"),
            "events": events,
            "timings": res.timings or {},
        }
        rows.append(row)

        print(f"\n  事件时间线（{rec.email}）:", flush=True)
        for t, kind, detail in events:
            print(f"    +{t / 1000:6.2f}s  {kind:16s} {detail}", flush=True)
        print(f"  → ok={res.ok} path={row['path']} reason={res.reason!r}", flush=True)
        print(f"  → captcha_ready={row['captcha_ready_s']}s  "
              f"captcha_wait={row['captcha_wait_s']}s  "
              f"mouse={row['mouse']}", flush=True)
        prev = 0
        print("  登录内部阶段:", flush=True)
        for k, v in (res.timings or {}).items():
            print(f"    {k:16s} +{(v - prev) / 1000:6.2f}s   (累计 {v / 1000:5.2f}s)",
                  flush=True)
            prev = v

        if i < args.rounds - 1:
            time.sleep(args.gap)

    # ── 汇总 ────────────────────────────────────────────────
    ok_rows = [r for r in rows if r["ok"]]
    print(f"\n{'=' * 68}")
    print(f"SUMMARY mode={args.mode}  rounds={len(rows)}  成功={len(ok_rows)}")
    print(f"{'=' * 68}")
    print(f"  {'#':>2s} {'ok':>3s} {'path':>4s} {'captcha_ready':>14s} "
          f"{'captcha_wait':>13s} {'init':>5s} {'移动':>5s} {'轨迹点':>7s} "
          f"{'纯等待':>6s} {'verify':>22s}")
    for r in rows:
        m = r["mouse"] or {}
        print(f"  {r['round']:>2d} {str(r['ok']):>3s} {str(r['path']):>4s} "
              f"{r['captcha_ready_s']:>13.2f}s {r['captcha_wait_s']:>12.2f}s "
              f"{r['init']:>5} {m.get('moves', 0):>5} {m.get('points', 0):>7} "
              f"{m.get('idle_waits', 0):>6} {str(r['verify_codes']):>22s}")

    if ok_rows:
        avg_cr = sum(r["captcha_ready_s"] for r in ok_rows) / len(ok_rows)
        avg_w = sum(r["wall_s"] for r in ok_rows) / len(ok_rows)
        waits = sorted(r["captcha_wait_s"] for r in ok_rows)
        med = waits[len(waits) // 2]
        print(f"\n  平均 captcha_ready = {avg_cr:.2f}s")
        print(f"  平均 captcha_wait  = {sum(waits) / len(waits):.2f}s  "
              f"（中位 {med:.2f}s，范围 {waits[0]:.2f}~{waits[-1]:.2f}s）")
        print(f"  平均 登录总耗时     = {avg_w:.2f}s")
        paths = [r["path"] for r in ok_rows]
        print(f"  通路分布            = {paths.count('A')}×Path A / "
              f"{paths.count('B')}×Path B")
        # InitCaptchaV3 #2 的到达时刻 = SDK 真正切到 CHECK_BOX 的时间点
        t2 = [e[0] / 1000 for r in ok_rows for e in (r["events"] or [])
              if e[1] == "Init#2"]
        if t2:
            print(f"  Init#2 平均到达     = {sum(t2) / len(t2):.2f}s  "
                  f"(范围 {min(t2):.2f}~{max(t2):.2f}s)")
    print("=" * 68)

    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"[probe] 明细已写入 {args.out}", flush=True)
    return 0 if len(ok_rows) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
