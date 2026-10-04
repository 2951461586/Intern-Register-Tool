r"""判定「站点接受哪些邮箱域名」—— 注册门的白名单边界。

为什么需要它
------------
2026-09-23 实测：CF Worker 的全部 7 个邮箱域名被站点以 `A0232` 拒，且 `A0232`
**带 traceId** ⇒ 是**应用层**判定，换出口 IP、换浏览器链路**都无效**。
于是「换域名」成了唯一出路 —— 但**站点到底接受哪些域名**此前没人知道。

本探针把这件事变成一次可复现的**三臂**实验。

做法（控制变量）
----------------
三臂，**全用合成地址**，不消耗真实邮箱池：

| 臂 | 地址 | 作用 |
|---|---|---|
| ① 正对照 | `probe-ctl-<rand>@<已知被拒域名>` | **必须复现 A0232**，否则本次实验**无效** |
| ② 待测 | `probe-<rand>@<--domain>` | 逐个域名给判定 |
| ③ 别名 | `probe-als-<rand>+tag@<域名>` | 验证「被拒的是域名，还是 `+别名` 格式」 |

🔴 正对照的域名**不写死**，从 `.env` 的 `IR_WORKER_DOMAIN` 取 —— 那是本项目的
自有域名（已知被拒），写进源码属于基础设施标识，会被泄漏闸门拦，而且每台机器
装的都不一样。也可用 `--control-domain` 显式覆盖。

判据
----
只看 `register/byEmail` **过 WAF 盾之后**的 `msgCode`：

| 响应 | 判定 |
|---|---|
| `msgCode == "A0232"` | ❌ 该域名被拒 |
| `success == true` | ✅ 该域名通过 |
| 其它 `msgCode` | ✅ 域名通过（撞的是别的业务校验，**不是**域名门） |
| `{"error":"Too many request"}`（**无** traceId） | ⚠ **网关层限流，样本无效** —— 退避重试后重读 |

🔴 **429 绝不能当结论**。2026-09-23 第一版就栽在这里：`gmail+别名` 那一枪撞上
网关限流，差点得出「gmail 别名被拒」的**假结论**。本工具内置退避重试。

🔴 **挑战页也不是结论**。写接口有阿里云 WAF 的 JS 挑战（`200 + text/html`），
必须先用真实浏览器算出 `acw_sc__v2` 再重试 —— 否则会像 `sso.py` 的 `_post`
那样直接抛 `JSONDecodeError`，把「被盾拦了」误读成「邮箱/网络错误」。

成本与安全
----------
- 每臂 **1 次注册写请求**。合成地址 ⇒ **不消耗真实邮箱**；代价是留下未激活的
  垃圾账号（激活需点邮件链接，所以是惰性的，无害）。
- 注册是写操作且限流极凶（实测正对照**连撞 2 次 429**才通）⇒ 默认串行 + 强制间隔。

用法：
    python tools/probes/probe_domain_gate.py
    python tools/probes/probe_domain_gate.py --domain icloud.com --domain proton.me
    python tools/probes/probe_domain_gate.py --address "someone+tag@gmail.com"
    python tools/probes/probe_domain_gate.py --control-domain other.example
"""

import argparse
import json
import random
import string
import sys
import time
from pathlib import Path

import requests
from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports" / "domain_gate.json"
DEFAULT_PROXY = "http://127.0.0.1:7901"

# 默认待测域名：主流邮箱服务商。
# 🔴 **不要**把自有域名写在这里 —— 那是基础设施标识，会被泄漏闸门拦，
#    而且每台机器都不一样。正对照域名从 .env 的 IR_WORKER_DOMAIN 取。
DEFAULT_DOMAINS = ["icloud.com", "gmail.com", "outlook.com", "qq.com"]

REJECTED_CODE = "A0232"


def rand_tag(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def build_arms(domains: list[str], addresses: list[str], control: str) -> list[dict]:
    """组装臂列表。正对照恒定第一臂 —— 它决定这次实验读不读得。"""
    arms: list[dict] = []
    if control:
        arms.append(
            {"label": "① 正对照", "email": f"probe-ctl-{rand_tag()}@{control}", "kind": "control"}
        )
    else:
        print(
            "⚠ 没有正对照域名（.env 里没配 IR_WORKER_DOMAIN，也没给 --control-domain）\n"
            "  ⇒ 本次实验**无法自证有效**，结果只能当参考。\n"
        )
    for d in domains:
        arms.append({"label": f"② 待测 {d}", "email": f"probe-{rand_tag()}@{d}", "kind": "domain"})
    for a in addresses:
        arms.append({"label": f"③ 指定 {a.split('@')[-1]}", "email": a, "kind": "address"})
    return arms


def solve_waf(challenge_html: str, proxy: str) -> str:
    """把挑战页交给真实浏览器**当文档加载**，取回 `acw_sc__v2`。

    🔴 **唯一实现已搬到 `src/browser/waf.py`**（`src/sso.py` 的 `_pass_waf` 也用它）
       —— 这里只做转发。两个拷贝的下场是漂移：探针能解、生产不能解，
       而且各自的“实测结论”会开始不一致。
    """
    from common import config
    from src.browser.settings import BrowserSettings
    from src.browser.waf import solve_acw_challenge

    acw = solve_acw_challenge(challenge_html, proxy,
                              settings=BrowserSettings.from_config(config))
    if acw:
        print(f"      ✓ 拿到 acw_sc__v2（len={len(acw)}）")
    return acw


def shoot(
    email: str, proxy: str, jar: dict, *, attempts: int = 4, backoff: float = 15.0
) -> "requests.Response":
    """打一枪 `register/byEmail`。撞 429 就退避重试。

    🔴 429 是**网关层限流**，不是业务判定 —— 直接采信会得出「域名被拒」的假结论。
    """
    from common import config
    from src.crypto_rsa import encrypt_password
    from src.pipeline import gen_password, gen_username

    target = f"{config.SSO_GW}/register/byEmail"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "lang": "zh-CN",
        "Origin": config.SSO_BASE,
        "User-Agent": config.USER_AGENT,
        "Referer": f"{config.SSO_BASE}/register",
    }
    payload = {
        "username": gen_username(),
        "email": email,
        "password": encrypt_password(email, gen_password()),
        "source": config.SOURCE,
        "clientId": config.CLIENT_ID,
    }
    s = requests.Session()
    config.apply_proxy(s, proxy)
    r = None
    for i in range(attempts):
        r = s.post(target, headers=headers, json=payload, timeout=60, cookies=jar)
        if r.status_code != 429:
            return r
        if i == attempts - 1:
            break
        wait = backoff * (i + 1)
        print(f"      429 → 退避 {wait:.0f}s 后重试（第 {i + 1} 次）")
        time.sleep(wait)
    if r is None:  # attempts <= 0：一个请求都没发
        raise ValueError(f"shoot: attempts 必须 >= 1（收到 {attempts}）")
    return r


def judge(r) -> tuple:
    """返回 (判定键, 中文判定, 证据摘要)。"""
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        return "waf", "⚠ 又拿到非 JSON（盾没过）", f"status={r.status_code} ct={ct}"
    try:
        b = r.json()
    except Exception as ex:  # noqa: BLE001
        return "unknown", "⚠ JSON 解析失败", f"{type(ex).__name__}: {ex}"[:120]
    if "error" in b and "Too many" in str(b.get("error")):
        return "gateway", "⚠ 网关层限流（无 traceId，样本无效）", f"raw={b}"
    code = b.get("msgCode", "")
    trace = "有" if b.get("traceId") else "无"
    if code == REJECTED_CODE:
        return "rejected", f"❌ 域名被拒（{REJECTED_CODE}）", f"traceId={trace}"
    success = b.get("success")
    if isinstance(success, bool) and success:
        uid = str((b.get("data") or {}).get("ssoUid", ""))[:12]
        return "accepted", "✅ 域名通过（注册成功）", f"ssoUid={uid}…"
    return (
        "accepted",
        f"✅ 域名通过（业务错误 {code}）",
        f"msg={b.get('msg', '')!r} traceId={trace}",
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="判定站点接受哪些邮箱域名（三臂：正对照 / 待测 / 别名）"
    )
    ap.add_argument(
        "--domain",
        action="append",
        dest="domains",
        default=None,
        help="待测域名，可重复（默认测主流服务商）",
    )
    ap.add_argument(
        "--address",
        action="append",
        dest="addresses",
        default=None,
        help="完整地址，可重复（用来测 `+别名` 形式）",
    )
    ap.add_argument(
        "--control-domain",
        default=None,
        help="正对照域名（默认取 .env 的 IR_WORKER_DOMAIN，已知被拒）",
    )
    ap.add_argument(
        "--proxy", default=DEFAULT_PROXY, help=f"出口代理（默认 {DEFAULT_PROXY}，需先起槽位实例）"
    )
    ap.add_argument(
        "--interval", type=float, default=6.0, help="两臂之间的最小间隔秒数（写操作限流，别调小）"
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    from common import config

    domains = args.domains or ([] if args.addresses else list(DEFAULT_DOMAINS))
    addresses = args.addresses or []
    control = args.control_domain or (config.WORKER_DOMAIN or "").strip()

    arms = build_arms(domains, addresses, control)
    if not arms:
        print("✗ 没有待测目标。给 --domain 或 --address。")
        return 1

    print(f"出口代理 {args.proxy}   共 {len(arms)} 臂   间隔 {args.interval}s")
    print("=" * 78)

    jar: dict = {}
    results: list[dict] = []
    for i, arm in enumerate(arms):
        print(f"\n{arm['label']}  {arm['email']}")
        r = shoot(arm["email"], args.proxy, jar)
        ct = r.headers.get("content-type", "")
        print(f"   shot  → status={r.status_code} ct={ct} len={len(r.content)}")

        if "json" not in ct:
            print("   撞上 WAF 挑战页 → 解盾 ...")
            acw = solve_waf(r.text, args.proxy)
            if not acw:
                print("   ✗ 解盾失败，本臂无效")
                results.append({**arm, "verdict": "waf", "note": "解盾失败"})
                continue
            jar = {"acw_sc__v2": acw}
            time.sleep(1.0)
            r = shoot(arm["email"], args.proxy, jar)
            ct = r.headers.get("content-type", "")
            print(f"   retry → status={r.status_code} ct={ct} len={len(r.content)}")

        key, cn, ev = judge(r)
        print(f"   → {cn}   [{ev}]")
        results.append({**arm, "verdict": key, "note": cn, "evidence": ev, "raw": r.text[:300]})

        if i < len(arms) - 1:
            time.sleep(args.interval)

    # ── 汇总 ──────────────────────────────────────────────────
    print("\n" + "=" * 78 + "\n汇总")
    for r in results:
        dom = r["email"].split("@")[-1]
        print(f"  {r['label']:16s} {dom:20s} {r['note']}")

    ctl = next((r for r in results if r["kind"] == "control"), None)
    invalid = ctl is None or ctl["verdict"] != "rejected"

    print()
    if invalid:
        print("🔴 正对照没复现 A0232 ⇒ 本次实验**无效**，别读上面的结论。")
        print("   常见原因：盾没过 / 网关限流 / 正对照域名其实已被放行。")
    else:
        ok = [r for r in results if r["verdict"] == "accepted"]
        bad = [r for r in results if r["verdict"] == "rejected"]
        gate = [r for r in results if r["verdict"] == "gateway"]
        print("✅ 实验有效（正对照复现 A0232）")
        print(f"   通过 {len(ok)}   被拒 {len(bad)}   限流未判定 {len(gate)}")
        if ok:
            print("   可用域名：" + ", ".join(sorted({r["email"].split("@")[-1] for r in ok})))
        if gate:
            print("   ⚠ 有限流样本未判定 —— 单独重跑那几个域名，别当被拒。")

    op = Path(args.out)
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(
        json.dumps(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "proxy": args.proxy,
                "control_domain": control,
                "experiment_valid": not invalid,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已落盘 {op}")
    return 0 if not invalid else 2


if __name__ == "__main__":
    sys.exit(main())
