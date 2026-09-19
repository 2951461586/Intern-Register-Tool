"""用**已存的 JWT** 直接查额度 —— 不开浏览器，1 秒查一个账号。

为什么值得单独做
----------------
`credits/balance`、`free-grant-status`、`getUserInfo` 这几个**只读**接口
只认 JWT，**不需要浏览器 Cookie**（2026-09-16 实测，见 README）。
而 JWT 有 14 天有效期。

于是"查额度"这件事根本不用付登录的代价：
    浏览器登录 ≈ 15~24s + 一次验证码 + 一个 Chrome 进程
    JWT 直查    ≈ 0.5s，纯 HTTP，可以几十路并发

这在本项目的处境下尤其重要 —— **注册被封（B0000）期间，JWT 是唯一还能
大规模利用的凭证**。台账里存着 15+ 条 09-15 签发的 JWT（有效期到 09-29），
拿它们就能把整个账号池的额度分布摸清楚，而不用碰浏览器、更不用碰注册。

要查什么
--------
`available_credits` 单独一个数看不出问题（它只是各窗口的最小值）。
真正要的是 `usage_windows`：

    { "5h": {limit 10, used ?, remaining ?},
      "7d": {limit 50, used ?, remaining ?} }

**已用额度才是"这个账号被消耗了多少"的直接证据** —— 本项目 2026-09-18
就是靠它查出：12 个账号里有 3 个的 7d 窗口已被消耗 0.43~1.98 credits，
而本项目的测试调用一次只有 ~0.0001 credits，对不上量级。

用法：
    python tools/probe_balance.py                 # 所有带 jwt 的账号
    python tools/probe_balance.py --limit 20 --workers 8
    python tools/probe_balance.py --show-ok       # 连正常的也逐条列
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT / "results.json"
DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports" / "balance_dump.json"


def probe(rec: dict) -> dict:
    """查一个账号的额度。返回统一结构的 dict（**不抛异常**）。"""
    from src.discovery import DiscoveryClient

    email = rec.get("email", "")
    jwt = (rec.get("jwt") or "").strip()
    if not jwt:
        return {"email": email, "verdict": "no_jwt"}
    try:
        dc = DiscoveryClient(jwt=jwt)
        bal = dc.balance()
        w = bal.get("usage_windows") or {}
        return {
            "email": email,
            "verdict": "ok",
            "available": bal.get("available_credits"),
            "rpm_limit": bal.get("rpm_limit"),
            "windows": {
                k: {"limit": (v or {}).get("limit_credits"),
                    "used": (v or {}).get("used_credits"),
                    "remaining": (v or {}).get("remaining_credits"),
                    "next_recover_at": (v or {}).get("next_recover_at")}
                for k, v in w.items()
            },
            "raw": bal,
        }
    except Exception as ex:                                       # noqa: BLE001
        msg = f"{type(ex).__name__}: {ex}"[:180]
        # 🔴 区分"JWT 过期"和"网络/其他错误"：前者是这个账号的 JWT 该换了，
        #    后者跟账号无关。混成一类会让排查方向跑偏。
        verdict = "jwt_expired" if ("A0211" in msg or "401" in msg) else "error"
        return {"email": email, "verdict": verdict, "detail": msg}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="用已存 JWT 查额度（不开浏览器）")
    ap.add_argument("--from", dest="src", default=str(DEFAULT_IN))
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 个（0=全部）")
    ap.add_argument("--workers", type=int, default=8, help="并发数（纯 HTTP，可给大）")
    ap.add_argument("--show-ok", action="store_true", help="连未消耗的也逐条列")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="快照落盘路径")
    args = ap.parse_args()

    src = Path(args.src)
    try:
        all_recs = json.loads(src.read_text(encoding="utf-8"))
    except (ValueError, OSError) as ex:
        print(f"✗ 读不到台账 {src}: {ex}")
        return 1

    # 只挑有 jwt 的 —— 没 jwt 的账号这个工具帮不上（要查得先登录）。
    # **必须把数量差报出来**，否则会让人以为"整个池子都查过了"。
    with_jwt = [r for r in all_recs if (r.get("jwt") or "").strip()]
    print(f"台账 {len(all_recs)} 条，其中带 JWT {len(with_jwt)} 条"
          f"（{len(all_recs) - len(with_jwt)} 条无 JWT，本工具查不了）")
    if not with_jwt:
        print("✗ 没有可查的账号")
        return 1
    if args.limit:
        with_jwt = with_jwt[:args.limit]

    print(f"查询 {len(with_jwt)} 个账号（并发 {args.workers}，纯 HTTP）")
    print("=" * 78)

    t0 = time.time()
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for f in as_completed([ex.submit(probe, r) for r in with_jwt]):
            out.append(f.result())
    wall = time.time() - t0

    ok = [r for r in out if r["verdict"] == "ok"]
    expired = [r for r in out if r["verdict"] == "jwt_expired"]
    err = [r for r in out if r["verdict"] == "error"]

    print(f"成功 {len(ok)}/{len(out)}   JWT 过期 {len(expired)}   其他错误 {len(err)}"
          f"   （{wall:.1f}s）")
    for r in expired[:5]:
        print(f"  ⚠ JWT 过期：{r['email']}  {r.get('detail', '')[:90]}")
    for r in err[:5]:
        print(f"  ⚠ 错误：{r['email']}  {r.get('detail', '')[:90]}")

    if not ok:
        print("✗ 没有任何账号查成功，后面没意义")
        return 1

    # ── 按 7d 已用量排序：这是"被消耗了多少"的直接证据 ──────────
    def used7(r):
        return _f(((r.get("windows") or {}).get("7d") or {}).get("used"))

    ok.sort(key=used7, reverse=True)
    consumed = [r for r in ok if used7(r) > 0.001]

    print(f"\n{'账号':38s} {'available':>12s} {'5h 已用':>10s} {'7d 已用':>10s}"
          f" {'7d 剩余':>10s}  7d 重置")
    for r in (ok if args.show_ok else ok[:25]):
        w = r.get("windows") or {}
        w5, w7 = w.get("5h") or {}, w.get("7d") or {}
        print(f"{r['email']:38s} {str(r.get('available')):>12s}"
              f" {str(w5.get('used')):>10s} {str(w7.get('used')):>10s}"
              f" {str(w7.get('remaining')):>10s}  {str(w7.get('next_recover_at'))[:16]}")

    tot5 = sum(_f(((r.get("windows") or {}).get("5h") or {}).get("used")) for r in ok)
    tot7 = sum(used7(r) for r in ok)
    print(f"\n合计：5h 已用 {tot5:.6f} credits，7d 已用 {tot7:.6f} credits"
          f"（{len(ok)} 个账号）")
    print(f"其中 **有消耗** 的账号 {len(consumed)} 个"
          + (f"，单账号最多 {used7(ok[0]):.6f} credits" if consumed else ""))

    if consumed:
        print("\n🔴 有消耗的账号 —— 本项目自己的测试调用一次约 0.0001 credits，")
        print("   量级对不上的就是**外部真实使用**（key 被别处用掉了）：")
        for r in consumed:
            w7 = (r.get("windows") or {}).get("7d") or {}
            print(f"  {r['email']:38s} 7d 已用 {w7.get('used')}  "
                  f"≈ {_f(w7.get('used')) * 1e6:,.0f} 输入 token 当量")

    # ── 落盘（按 email 合并，保留历史快照便于对比增速）─────────
    op = Path(args.out)
    op.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if op.is_file():
        try:
            old = json.loads(op.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            old = {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in out:
        if r["verdict"] != "ok":
            continue
        e = old.get(r["email"]) or {}
        hist = e.get("history") or []
        hist.append({"at": now, "available": r.get("available"),
                     "used_5h": ((r.get("windows") or {}).get("5h") or {}).get("used"),
                     "used_7d": used7(r)})
        old[r["email"]] = {"at": now, "balance": r.get("raw"),
                           "history": hist[-20:]}
    op.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n快照已落盘 {op}（累计 {len(old)} 个账号，每个最多留 20 次历史）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
