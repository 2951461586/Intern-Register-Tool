"""判定注册封禁的**作用范围**：是 IP 还是邮箱域名？

背景
----
2026-09-15 14:05 之后 `register/byEmail` 一直返回 `B0000 请求频繁`。
22:39（**8.6 小时后**）单账号探测**仍然 B0000** —— 推翻了"数分钟恢复"的假设。

封禁范围决定了修法完全不同：
  - **IP 被封** → 换出口 IP / 挂代理；换域名没用
  - **域名被封** → 换发信域名即可（Worker 支持 7 个），IP 没问题

判别方法（同 IP、只改一个变量）
------------------------------
Worker 支持多个域名，于是可以做**控制变量实验**：

    同一台机器、同一个 IP、同一套请求头，
    只把邮箱域名从 `IR_WORKER_DOMAIN` 换成另一个 → 看是否还 B0000

  - 换域名成功 → **域名维度**（这个发信域名被烧了）
  - 换域名仍失败 → **IP 维度**（或更上层）

顺序上先试**一个**其它域名：若成功就收工（信息已足够，且不再消耗尝试次数）；
只有失败才再试第二个**不同 TLD** 的域名（排除"同后缀连带"这种解释）。

🔴 必须加间隔，否则实验会被另一层限流污染
----------------------------------------
第一次跑这个探针时两个域名**在同一秒**各发一次，第二个立刻拿到

    HTTP 429  {"error": "Too many request"}          ← 无 traceId

而 B0000 长这样：

    HTTP 200  {"traceId":..., "msgCode":"B0000", ...} ← 有 traceId

**这是两套不同的限流系统**：
  - `HTTP 429 {"error":...}` = **网关层**（响应头 `server: istio-envoy`）
    → 管瞬时突发，几秒级
  - `HTTP 200 B0000` = **应用层**
    → 管累计量，窗口以小时/天计

同秒连发会稳定命中网关层，把应用层的真实结果**盖掉** → 实验无效。
所以每个域名内**逐次重试并留间隔**，并且只有拿到 `HTTP 200` 的响应
才算对应用层下了判断。

用法：
    python tools/probe_quota_scope.py [--gap 6] [--tries 3]
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.tempmail import TempMailClient  # noqa: E402

BASELINE_DOMAIN = config.WORKER_DOMAIN        # 取自 IR_WORKER_DOMAIN
# 第二个候选刻意选**不同后缀**的域名，排除"同一后缀被连带"的解释。
# ⚠ 这里不再写死具体域名，请改成你自己 Worker 支持的另一个域名。
FALLBACK_DOMAIN = os.getenv("IR_PROBE_FALLBACK_DOMAIN", "")

INTERESTING_HEADERS = ("retry-after", "x-ratelimit", "x-rate", "cf-",
                       "set-cookie", "server", "date")


def classify(r) -> str:
    """把一次尝试归类：`OK` / `B0000`（应用层封） / `GW429`（网关层） / 其它。"""
    if r.status_code == 429:
        return "GW429"
    try:
        body = r.json()
    except ValueError:
        return f"http{r.status_code}"
    if body.get("success") is True:
        return "OK"
    return str(body.get("msgCode") or f"http{r.status_code}")


def raw_register(email: str, username: str, password: str):
    """打**裸请求**并保留完整响应（含响应头），便于看有没有 Retry-After 之类。"""
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "lang": "zh-CN",
        "Origin": config.SSO_BASE,
        "Referer": f"{config.SSO_BASE}/register",
        "User-Agent": config.USER_AGENT,
    })
    payload = {
        "username": username,
        "email": email,
        "password": encrypt_password(email, password),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    return s.post(f"{config.SSO_GW}/register/byEmail", json=payload, timeout=30)


def show(tag: str, r) -> None:
    body = r.text
    print(f"\n  [{tag}] HTTP {r.status_code}  ({len(body)} B)")
    for k, v in r.headers.items():
        if any(k.lower().startswith(p) for p in INTERESTING_HEADERS):
            print(f"      {k}: {v[:120]}")
    print(f"      body: {body[:400]}")


def check_read_endpoints() -> None:
    """只读接口是否仍正常 —— 用来确认封禁只挂在**写操作**上。"""
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Origin": config.SSO_BASE,
        "Referer": f"{config.SSO_BASE}/register",
        "User-Agent": config.USER_AGENT,
    })
    print("\n只读接口（应与封禁无关）：")
    for name, path, payload in (
        ("cipher/getPubKey", "/cipher/getPubKey",
         {"type": "register", "from": "browser"}),
        ("personal/username/check", "/personal/username/check",
         {"username": f"lz{int(time.time()) % 1000000}"}),
        ("register/check", "/register/check",
         {"item": "probe@example.com", "type": "email"}),
    ):
        try:
            r = s.post(f"{config.SSO_GW}{path}", json=payload, timeout=20)
            ok = r.status_code == 200 and r.json().get("success") is True
            print(f"  {'✓' if ok else '✗'} {name:26s} HTTP {r.status_code} "
                  f"success={r.json().get('success')}")
        except Exception as ex:                       # noqa: BLE001
            print(f"  ✗ {name:26s} 异常 {ex}")


def probe_domain(mail, dom: str, *, tries: int, gap: float) -> dict:
    """在单个域名上重试 `tries` 次（间隔 `gap`），返回 `{verdict, attempts}`。

    `verdict` 只在拿到 **HTTP 200** 时才有意义（429 是网关层，没触到应用层）。
    """
    try:
        email = mail.create_mailbox(domain=dom, count=1)[0]
    except Exception as ex:                           # noqa: BLE001
        return {"verdict": "mailbox-failed", "attempts": [f"建邮箱失败 {ex}"]}

    print(f"  邮箱：{email}")
    attempts = []
    verdict = "inconclusive"
    for i in range(tries):
        if i:
            time.sleep(gap)
        user = f"lz{int(time.time() * 1000) % 1000000:06d}"
        t = time.time()
        r = raw_register(email, user, "Lz#Probe12345")
        v = classify(r)
        attempts.append(f"{v}({time.time() - t:.2f}s)")
        print(f"    第 {i + 1} 次 → {v}   HTTP {r.status_code}  "
              f"body={r.text[:120]}")
        if v == "OK":
            verdict = "OK"
            break
        if v == "B0000":
            # 拿到 HTTP 200 的应用层响应 → 这个域名下的判断**已成立**，
            # 不必再试（也少消耗一次尝试）。
            verdict = "B0000"
            break
        # GW429 / 其它 → 没触到应用层，继续重试
    if verdict == "inconclusive" and "GW429" in " ".join(attempts):
        print(f"    ⚠ {tries} 次全被**网关层** 429 挡住，没触到应用层 → "
              f"加大 --gap 再试")
    return {"verdict": verdict, "attempts": attempts}


def main() -> int:
    ap = argparse.ArgumentParser(description="判定注册封禁的作用范围")
    ap.add_argument("--gap", type=float, default=6.0, help="同域名内重试间隔（秒）")
    ap.add_argument("--tries", type=int, default=3, help="每个域名最多试几次")
    args = ap.parse_args()

    mail = TempMailClient()
    domains = [BASELINE_DOMAIN, FALLBACK_DOMAIN]
    print(f"域名候选：{BASELINE_DOMAIN}（当前） / {FALLBACK_DOMAIN}（对照）")
    print(f"间隔 {args.gap}s，每域名最多 {args.tries} 次")

    check_read_endpoints()

    results = {}
    for i, dom in enumerate(domains):
        print(f"\n{'=' * 70}\n[{i + 1}/{len(domains)}] 域名 = {dom}")
        res = probe_domain(mail, dom, tries=args.tries, gap=args.gap)
        results[dom] = res
        if res["verdict"] == "OK":
            print(f"\n  ✅ 注册成功 → 封禁是**域名维度**，换域名即可绕过")
            break

    print(f"\n{'=' * 70}\n结论")
    for d, r in results.items():
        print(f"  {d:24s} -> {r['verdict']:14s}  {', '.join(r['attempts'])}")

    verdicts = [r["verdict"] for r in results.values()]
    if "OK" in verdicts:
        print("\n→ **域名维度封禁**：同一 IP 换发信域名即可恢复。")
        print("  修法：把 WORKER_DOMAIN 换成没被烧的域名，并在域名池间轮换。")
    elif verdicts and all(v == "B0000" for v in verdicts):
        print("\n→ 换域名**无效** → 封禁是 **IP 维度**（或更上层）。")
        print("  修法：换出口 IP（代理池），换域名没用。")
    else:
        print("\n→ **结论不确定**：没能在每个域名上都拿到 HTTP 200 的应用层响应。")
        print("  加大 --gap（网关层 429 需要更长间隔）后重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
