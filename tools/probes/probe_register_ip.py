r"""判定「注册封禁是不是 IP 维度、换出口 IP 能不能解开」—— 只打注册这一枪。

为什么需要它
------------
本项目的注册封禁已持续 **> 68h**（跨 3 个自然日），且已排除"每天 0 点重置"。
README 里给出的判断是"IP 维度累计配额"，但**这个判断一直没被正面验证过** ——
因为旧实现只有**一个**出口 IP（`IR_PROXY` 一条），没有第二个 IP 可做对照。

现在有了槽位（`tools/ops/gen_mihomo_slots.py` + `src/proxypool.py`），
同一时刻可以拿到多个不同出口 IP。于是终于能做这个**决定性实验**：

    同一个邮箱域名、同一个账号参数，只换出口 IP，
    看 `register/byEmail` 返回 B0000 还是 success。

三种结果的解读
--------------
| 结果 | 含义 |
|------|------|
| 新 IP 上 `success` | ✅ **封禁是 IP 维度**，换 IP 即可解开 —— 槽位方案对症 |
| 新 IP 上仍 `B0000` | ❌ 封禁不只是 IP 维度（可能还有账号/域名/全局维度），换 IP 无效 |
| 新 IP 上 `429`   | ⚠ 打到的是**网关层**限流（另一个系统），不是配额 —— 要重试 |

🔴 **成本与安全**
- 每个 IP 只花 **1 个邮箱 + 1 次注册请求**。不激活、不建 key。
- 注册是**写操作**，会被限流。所以本工具**默认串行 + 强制间隔**
  （`--interval`，默认 8s），不要为了快把它并发起来 —— 那只会制造 429，
  把真正的信号盖掉。
- ⚠ 一次失败的注册尝试**可能延长冷却**（README 记录过）。所以默认只测
  少数几个 IP（`--limit 3`），且每个 IP 只打一枪。

用法：
    python tools/probes/probe_register_ip.py --slots .workbuddy-ai/proxypool/slots.txt
    python tools/probes/probe_register_ip.py --proxy http://127.0.0.1:7901 --limit 2
    python tools/probes/probe_register_ip.py --slots-file ... --dry-run   # 只看出口 IP，不发注册
"""

import argparse
import json
import sys
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

DEFAULT_SLOTS = ROOT / ".workbuddy-ai" / "proxypool" / "slots.txt"
DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports" / "register_ip_probe.json"


def load_slots(spec: str) -> list[str]:
    """从文件或逗号分隔串读槽位。文件里允许一行一个或逗号分隔。"""
    p = Path(spec)
    raw = p.read_text(encoding="utf-8") if p.is_file() else spec
    out = []
    for chunk in raw.replace("\n", ",").split(","):
        c = chunk.strip()
        if c:
            out.append(c)
    return out


def exit_ip(proxy: str, *, timeout: int = 20) -> str:
    """拿这个出口的真实 IP。

    🔴 **必须真的发请求**，不能靠 TCP 连通性 —— 本机跑着 Clash TUN + fake-ip，
    连任何域名都可能"0.02s 连通"（连的是本地虚拟网卡）。见 `tools/probes/probe_proxy.py`。

    🔴 也不能用 `curl --noproxy '*'` 去测代理：`--noproxy` 会把 `-x` 指定的
    代理**一起禁用**，于是测出来的是**直连出口**，而看起来"代理没生效"。
    （2026-09-18 实测踩到：6 个槽位全返回同一个 IP，差点得出"槽位没用"的错误结论。）
    """
    import requests

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "http://ip-api.com/line/?fields=query"):
        try:
            r = requests.get(url, proxies={"http": proxy, "https": proxy},
                             timeout=timeout)
            if r.status_code == 200:
                ip = r.text.strip()
                if ip and len(ip) < 64 and " " not in ip:
                    return ip
        except Exception:                                         # noqa: BLE001
            continue
    return ""


def register_once(proxy: str) -> dict:
    """建一个邮箱 + 打一枪 `register/byEmail`。返回结构化结果。"""
    from src import config
    from src.pipeline import gen_password, is_quota_block
    from src.sso import SSOClient
    from src.tempmail import TempMailClient

    # 🔴 关键：**必须**在 import 之后改 `config.IR_PROXY` 再建 client，
    #    因为 `SSOClient.__init__` 会调 `config.apply_proxy()`。
    #    这也意味着**一个进程一次只能测一个 IP** —— 想并发就得开子进程。
    #    本工具刻意串行，就是为了让"一个 IP 一枪"这件事在代码里显式可见。
    config.IR_PROXY = proxy

    out = {"proxy": proxy, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        mail = TempMailClient()
        emails = mail.create_mailbox(count=1)
        if not emails:
            out["verdict"] = "mailbox_failed"
            return out
        out["email"] = emails[0]
    except Exception as ex:                                       # noqa: BLE001
        out["verdict"] = "mailbox_failed"
        out["detail"] = f"{type(ex).__name__}: {ex}"[:160]
        return out

    try:
        sso = SSOClient()
        res = sso.register(username=out["email"].split("@")[0],
                           email=out["email"], password=gen_password())
        out["ok"] = res.ok
        out["msg_code"] = res.msg_code
        out["msg"] = (res.msg or "")[:160]
        out["sso_uid"] = res.sso_uid
        if res.ok:
            out["verdict"] = "success"
        elif is_quota_block(res.msg_code + res.msg):
            out["verdict"] = "quota_blocked"
        else:
            out["verdict"] = "other_error"
    except Exception as ex:                                       # noqa: BLE001
        out["verdict"] = "exception"
        out["detail"] = f"{type(ex).__name__}: {ex}"[:200]
    return out


_VERDICT_CN = {
    "success": "✅ 注册成功",
    "quota_blocked": "❌ B0000 配额封禁",
    "other_error": "⚠ 其他业务错误",
    "exception": "⚠ 异常",
    "mailbox_failed": "⚠ 邮箱创建失败",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="判定注册封禁是否 IP 维度（只打注册一枪）")
    ap.add_argument("--slots", default=str(DEFAULT_SLOTS),
                    help="槽位清单（文件路径或逗号分隔的代理 URL）")
    ap.add_argument("--proxy", action="append", dest="extra", default=None,
                    help="额外指定的代理 URL（可重复）")
    ap.add_argument("--limit", type=int, default=3, help="最多测几个 IP（默认 3）")
    ap.add_argument("--interval", type=float, default=8.0,
                    help="两次注册之间的最小间隔秒数（写操作限流，别调小）")
    ap.add_argument("--mail-domain", default=None, help="覆盖发信域名")
    ap.add_argument("--dry-run", action="store_true",
                    help="只探各出口 IP，**不发任何注册请求**")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    slots: list[str] = []
    if args.slots:
        p = Path(args.slots)
        if p.is_file() or "," in args.slots or args.slots.startswith("http"):
            slots += load_slots(args.slots)
    slots += (args.extra or [])
    # 去重但保序
    seen, uniq = set(), []
    for s in slots:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    slots = uniq

    if not slots:
        print(f"✗ 没有槽位。先跑 tools/ops/gen_mihomo_slots.py 生成 {DEFAULT_SLOTS}，"
              f"或用 --proxy 指定。")
        return 1
    if args.mail_domain:
        from src import config
        config.WORKER_DOMAIN = args.mail_domain

    print(f"待测出口 {len(slots)} 个，本次最多测 {min(args.limit, len(slots))} 个"
          f"（间隔 {args.interval}s）")
    print("=" * 78)

    # ── 第一步：先把所有出口 IP 探出来。**这一步不发注册请求。** ──
    # 先探 IP 的价值：如果两个槽位的出口 IP 相同，测两次等于只测了一个 IP，
    # 而表面上"测了 2 个"。必须按**真实出口 IP**去重，不能按槽位号。
    print("① 探出口 IP（不发注册请求）")
    ip_of: dict[str, str] = {}
    for s in slots:
        ip = exit_ip(s)
        ip_of[s] = ip
        print(f"   {s:34s} -> {ip or '<取不到>'}")
    live = [s for s in slots if ip_of[s]]
    if not live:
        print("✗ 所有槽位都取不到出口 IP，先确认 mihomo 实例起来了")
        return 1

    distinct: dict[str, str] = {}
    for s in live:
        distinct.setdefault(ip_of[s], s)
    dupes = len(live) - len(distinct)
    print(f"   可用 {len(live)} 个槽位 → **不同出口 IP {len(distinct)} 个**"
          + (f"（{dupes} 个槽位与别的槽位共用出口，按 IP 去重后不再重复测）"
             if dupes else ""))

    if args.dry_run:
        print("\n（--dry-run：未发任何注册请求）")
        return 0

    # ── 第二步：每个**不同出口 IP** 打一枪 ──────────────────────
    targets = list(distinct.values())[:max(1, args.limit)]
    print(f"\n② 逐个出口打一枪 register/byEmail（共 {len(targets)} 个 IP）")
    results: list[dict] = []
    for i, s in enumerate(targets):
        if i:
            time.sleep(args.interval)
        print(f"\n   [{i + 1}/{len(targets)}] 出口 {ip_of[s]}  ({s})")
        r = register_once(s)
        r["exit_ip"] = ip_of[s]
        results.append(r)
        print(f"     → {_VERDICT_CN.get(r['verdict'], r['verdict'])}"
              + (f"  {r.get('msg') or r.get('detail', '')}" if
                 r.get("msg") or r.get("detail") else ""))

    # ── 结论 ──────────────────────────────────────────────────
    ok = [r for r in results if r["verdict"] == "success"]
    blocked = [r for r in results if r["verdict"] == "quota_blocked"]
    other = [r for r in results if r["verdict"] not in ("success", "quota_blocked")]

    print("\n" + "=" * 78)
    print(f"成功 {len(ok)}   配额封禁 {len(blocked)}   其他 {len(other)}"
          f"   （测了 {len(results)} 个不同出口 IP）")
    if ok:
        print("\n✅ **封禁是 IP 维度，换出口 IP 能解开。**")
        print(f"   成功的出口：{', '.join(r['exit_ip'] for r in ok)}")
        print("   → 槽位方案对症。下一步：把 IR_PROXY_SLOTS 配进 .env 跑批量。")
    elif blocked and not other:
        print(f"\n❌ **换了 {len(blocked)} 个不同出口 IP 仍全部 B0000。**")
        print("   说明封禁**不只是** IP 维度（可能还有账号/域名/全局维度）。")
        print("   换 IP 不是对症解法 —— 别再往这个方向投入了。")
    elif blocked:
        print("\n⚠ 部分被封、部分其他错误 —— 先看上面「其他」那几条的原因，"
              "别急着下结论。")
    else:
        print("\n⚠ 没有一枪打到注册接口（全是邮箱/网络错误）—— "
              "这次实验**无效**，不是'封禁解除了'。")

    op = Path(args.out)
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "slots_total": len(slots), "distinct_exit_ips": len(distinct),
         "tested": len(results), "success": len(ok), "blocked": len(blocked),
         "other": len(other), "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已落盘 {op}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
