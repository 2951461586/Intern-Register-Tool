"""探测 `register/byEmail` 的写操作限流边界：两次注册之间最小安全间隔是多少？

背景
----
实测 4 路**并发**注册时 3 路被 `429 Too Many Requests` 拒绝（且 ~1.2s 立即返回，
不是超时）。`sso._post()` 已加退避重试，`pipeline._RateLimiter` 用
`REG_MIN_INTERVAL = 2.5s` 把速率钉死。

但 2.5s 是**保守估计**，不是实测出来的边界。这个值直接决定流水线的
"首账号就绪时刻" —— 4 个账号 × 2.5s = 10s，而浏览器 worker 在这 10s 里
完全空转（关键路径 = 首账号就绪 10s + 2×17.5s 登录）。

所以要把这个值探到真实边界附近。

做法
----
**必须绕开 `_post()` 的退避重试** —— 它会把 429 吞掉，探不到边界。
这里直接用 `session.post` 打原始请求，记录 status_code。

**降序试探 + 见 429 即停**：从已知安全的 2.5s 往下试，一旦某档出现 429
就立刻停止，不再往下压。理由：本机出口 IP 是共用资源，把限流器激怒到
"硬封禁"的代价远大于这点性能收益。

用法
----
    python .workbuddy-ai/tmp/probe_reg_interval.py
    python .workbuddy-ai/tmp/probe_reg_interval.py --levels 2.5,2.0,1.5,1.0 --per 4
"""

import argparse
import json
import random
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.sso import SSOClient  # noqa: E402
from src.tempmail import TempMailClient  # noqa: E402


def gen_username() -> str:
    return "lz" + "".join(random.choices(string.digits, k=6))


def raw_register(sso: SSOClient, email: str, username: str, password: str) -> tuple:
    """不走退避重试的裸注册。返回 (status_code, elapsed_s, msg_code)。"""
    payload = {
        "username": username,
        "email": email,
        "password": encrypt_password(email, password),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    h = sso._headers("/register")
    t0 = time.time()
    r = sso.session.post(f"{sso.gw}/register/byEmail", headers=h, json=payload,
                         timeout=30)
    el = round(time.time() - t0, 2)
    mc = ""
    try:
        mc = r.json().get("msgCode", "")
    except Exception:
        pass
    return r.status_code, el, mc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="2.5,2.0,1.5,1.0",
                    help="要探测的间隔档位（秒），**降序**，见 429 即停")
    ap.add_argument("--per", type=int, default=4, help="每档发几次注册")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    levels = [float(x) for x in args.levels.split(",")]
    mail, sso = TempMailClient(), SSOClient()
    report = []
    stop = False

    for lv in levels:
        if stop:
            print(f"[probe] 跳过 {lv}s（上一档已出现 429）", flush=True)
            continue
        print(f"\n{'─' * 66}\n[probe] 档位 interval = {lv}s  ×{args.per}\n{'─' * 66}",
              flush=True)
        row = {"interval": lv, "runs": [], "n429": 0}
        for i in range(args.per):
            if i > 0:
                time.sleep(lv)
            try:
                email = mail.create_mailbox(count=1)[0]
            except Exception as ex:
                print(f"  [{i + 1}] 建邮箱失败: {ex}", flush=True)
                continue
            try:
                code, el, mc = raw_register(sso, email, gen_username(),
                                            "Lz#probe" + "".join(
                                                random.choices(string.ascii_letters, k=4)))
            except Exception as ex:
                print(f"  [{i + 1}] 请求异常: {ex}", flush=True)
                continue
            row["runs"].append({"status": code, "elapsed_s": el, "msg_code": mc})
            flag = "🔴 429" if code == 429 else ("✅" if code == 200 else f"⚠ {code}")
            print(f"  [{i + 1}/{args.per}] {flag}  {el:5.2f}s  msgCode={mc or '-'}",
                  flush=True)
            if code == 429:
                row["n429"] += 1
        report.append(row)
        if row["n429"]:
            print(f"[probe] ⚠ {lv}s 档出现 {row['n429']} 次 429 → 停止下探", flush=True)
            stop = True

    # ── 汇总 ────────────────────────────────────────────────
    print(f"\n{'=' * 66}")
    print("SUMMARY  register/byEmail 写操作限流边界")
    print(f"{'=' * 66}")
    print(f"  {'间隔':>6s} {'成功':>5s} {'429':>5s} {'平均耗时':>9s}  判定")
    safe = []
    for row in report:
        n = len(row["runs"])
        ok = sum(1 for r in row["runs"] if r["status"] == 200)
        avg = (sum(r["elapsed_s"] for r in row["runs"]) / n) if n else 0
        verdict = "✅ 安全" if row["n429"] == 0 and ok == n else f"❌ 触发限流"
        if verdict.startswith("✅"):
            safe.append(row["interval"])
        print(f"  {row['interval']:>5.1f}s {ok:>5d} {row['n429']:>5d} "
              f"{avg:>8.2f}s  {verdict}")
    if safe:
        print(f"\n  实测安全区间: ≤ {min(safe):.1f}s 档无 429"
              f"（本探针未探到悬崖，可再往下压一档验证）")
        print(f"  ⚠ 注意：间隔缩短**不带来吞吐提升** —— 注册不是瓶颈。"
              f"workers=2 时浏览器侧消耗速率 ≈ 0.11 账号/秒，"
              f"而 {min(safe):.1f}s 闸门给出 {1 / min(safe):.2f} 账号/秒。")
        print(f"     真实收益只有：减少 worker 冷启动空转、避免队列堆深。")
    print("=" * 66)

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"[probe] 明细已写入 {args.out}", flush=True)


if __name__ == "__main__":
    main()
