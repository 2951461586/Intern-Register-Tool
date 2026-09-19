"""只测**登录阶段**（不注册、不建 key）—— 用已有账号。

为什么需要它
------------
注册被 IP 维度封掉之后（`B0000`，实测持续 > 8.6h），**注册→登录**的整链跑不了，
但登录本身用的是**已有账号**，完全不消耗注册配额。于是它成了两件事的探针：

  1. **登录是否也被同一个 IP 封禁影响？**
     之前的数据（`opt6_w4`）显示注册被封时登录仍正常，但那是 8 小时前的观测。
     直接测一次才知道现在能不能继续干活。
  2. **`workers` 的并发扩展性**（一直悬而未决的 todo）
     原计划"复测 workers=3"必须走注册，被配额挡住了。
     只测登录就把**浏览器阶段**单独隔离出来 —— 这才是 `workers` 真正控制的阶段，
     而且避开了注册配额这个混杂因素。

⚠ 两个刻意的设计
----------------
- **不建 key**：建 key 会改变账号状态，且网关侧有传播延迟会污染计时。
  登录拿到 JWT 就够了，这是浏览器阶段唯一的产出。
- **每次用不同账号**（`--offset`）：避免"同一账号第二次登录更快"的缓存偏差
  混进配置对比里。宁可换账号引入账号间差异，也不要引入顺序偏差
  —— 顺序偏差是**系统性**的，会稳定地把后测的配置显得更快。

用法：
    python tools/probes/probe_login_only.py --workers 2 --count 6 --offset 0
    python tools/probes/probe_login_only.py --workers 3 --count 6 --offset 6
"""

import argparse
import csv
import json
import queue
import statistics
import sys
import threading
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

DEFAULT_CSV = ROOT / ".workbuddy-ai" / "exports" / "keys_export.csv"


def load_accounts(csv_path: Path, offset: int, count: int) -> list[tuple]:
    with csv_path.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("email") and r.get("password")]
    rows.sort(key=lambda r: r.get("created_at") or "")
    return [(r["email"], r["password"]) for r in rows[offset:offset + count]]


def probe_discovery(jwt: str, cookies: dict) -> dict:
    """登录后的**只读** Stage 4 调用（不建 key，零状态变更）。

    为什么加这一段：只测登录只能回答"浏览器阶段能并发到几"，
    但整条链路在登录之后还有 4 个 HTTP 调用（用户信息 / 领额度状态 / 余额 /
    已有 key 列表）。它们虽然快（~3s），却是**整链吞吐**的一部分，
    而且能顺带验证 JWT + Cookie 的鉴权在**高并发下**是否仍然成立
    （`/tokenplan/v1/keys` 那组接口校验更严，仅带 Authorization 会 -10002）。
    """
    from src.discovery import DiscoveryClient

    out = {}
    try:
        dc = DiscoveryClient(jwt=jwt, cookies=cookies)
    except Exception as ex:                                   # noqa: BLE001
        return {"error": f"client init: {ex}"[:120]}
    for name, fn in (("user_info", dc.get_user_info),
                     ("grant_status", dc.free_grant_status),
                     ("balance", dc.balance),
                     ("list_keys", dc.list_keys)):
        t = time.time()
        try:
            v = fn()
            if name == "balance":
                out[name] = {"ok": True, "ms": round((time.time() - t) * 1000),
                             "credits": str(v.get("available_credits", ""))}
            elif name == "list_keys":
                out[name] = {"ok": True, "ms": round((time.time() - t) * 1000),
                             "n": len(v)}
            else:
                out[name] = {"ok": True, "ms": round((time.time() - t) * 1000)}
        except Exception as ex:                               # noqa: BLE001
            out[name] = {"ok": False, "ms": round((time.time() - t) * 1000),
                         "error": str(ex)[:120]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="只测登录阶段（用已有账号）")
    ap.add_argument("--workers", type=int, default=2, help="浏览器并发数")
    ap.add_argument("--count", type=int, default=6, help="账号数")
    ap.add_argument("--offset", type=int, default=0, help="从第几个账号开始取")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--headful", dest="headless", action="store_false")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="账号来源 CSV")
    ap.add_argument("--with-discovery", action="store_true",
                    help="登录后跑一遍**只读**的 Stage 4（用户信息/额度/余额/key 列表）")
    ap.add_argument("--out", default=None, help="结果 JSON 落盘路径")
    args = ap.parse_args()

    accts = load_accounts(Path(args.csv), args.offset, args.count)
    if not accts:
        print(f"✗ 从 {args.csv} 取不到账号（offset={args.offset} count={args.count}）")
        return 1

    print(f"登录测试：{len(accts)} 个账号 / workers={args.workers} / "
          f"headless={args.headless}")
    print(f"账号来源 {args.csv}（offset={args.offset}）")
    for e, _ in accts:
        print(f"  - {e}")

    from src.browser import BrowserSession

    q: queue.Queue = queue.Queue()
    for a in accts:
        q.put(a)
    out, lock = [], threading.Lock()

    def worker(wid: int):
        try:
            with BrowserSession(headless=args.headless) as sess:
                print(f"[worker {wid + 1}] browser ready ({sess.launch_ms}ms)",
                      flush=True)
                while True:
                    try:
                        email, pw = q.get_nowait()
                    except queue.Empty:
                        return
                    t0 = time.time()
                    disc = None
                    try:
                        res = sess.login(email, pw, verbose=False)
                        ok, reason = res.ok, res.reason
                        path = (res.captcha_stage or {}).get("path", "")
                        detail = res.timings
                        jwt_len = len(res.jwt)
                        if ok and args.with_discovery:
                            disc = probe_discovery(res.jwt, res.cookies)
                    except Exception as ex:                   # noqa: BLE001
                        ok, reason, path, detail, jwt_len = (
                            False, f"exception: {str(ex)[:120]}", "", {}, 0)
                    dt = time.time() - t0
                    rec = {"email": email, "ok": ok, "seconds": round(dt, 2),
                           "captcha_path": path, "reason": reason or "",
                           "jwt_len": jwt_len, "worker": wid + 1,
                           "timings": detail, "discovery": disc}
                    with lock:
                        out.append(rec)
                        print(f"  {'✓' if ok else '✗'} {email:38s} "
                              f"{dt:6.2f}s  path={path or '-'}  "
                              f"{(reason or '')[:60]}", flush=True)
        except Exception as ex:                               # noqa: BLE001
            with lock:
                print(f"[worker {wid + 1}] 会话异常: {ex}", flush=True)

    t_start = time.time()
    ts = [threading.Thread(target=worker, args=(i,), daemon=True)
          for i in range(args.workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.time() - t_start

    ok = [r for r in out if r["ok"]]
    times = sorted(r["seconds"] for r in ok)
    paths = {}
    for r in out:
        paths[r["captcha_path"] or "-"] = paths.get(r["captcha_path"] or "-", 0) + 1

    print(f"\n{'=' * 72}")
    print(f"登录结果：{len(ok)}/{len(out)} 成功   总耗时 {wall:.1f}s "
          f"= {wall / max(len(out), 1):.1f}s / 账号")
    if times:
        print(f"单账号登录耗时：最快 {times[0]:.1f}s · 中位 "
              f"{statistics.median(times):.1f}s · 最慢 {times[-1]:.1f}s · "
              f"极差 {times[-1] - times[0]:.1f}s")
    print(f"验证码通路分布：{paths}")

    # 只读 Stage 4 的统计（若开了 --with-discovery）
    disc_recs = [r["discovery"] for r in out if r.get("discovery")]
    if disc_recs:
        calls = ("user_info", "grant_status", "balance", "list_keys")
        print(f"\n只读 Stage 4（{len(disc_recs)} 个账号）：")
        for c in calls:
            okn = sum(1 for d in disc_recs if (d.get(c) or {}).get("ok"))
            ms = [d[c]["ms"] for d in disc_recs if (d.get(c) or {}).get("ok")]
            extra = ""
            if c == "balance":
                cs = {d[c].get("credits") for d in disc_recs
                      if (d[c] or {}).get("ok")}
                extra = f"  credits={sorted(cs)}"
            if c == "list_keys":
                ns = sorted({d[c].get("n") for d in disc_recs
                             if (d[c] or {}).get("ok")})
                extra = f"  每账号 key 数={ns}"
            med = f" 中位 {statistics.median(ms):.0f}ms" if ms else ""
            print(f"  {'✓' if okn == len(disc_recs) else '✗'} {c:14s} "
                  f"{okn}/{len(disc_recs)}{med}{extra}")
        bad = [(r["email"], d) for r, d in
               ((r, r["discovery"]) for r in out if r.get("discovery"))
               if any(not (d.get(c) or {}).get("ok") for c in calls)]
        if bad:
            print(f"  ⚠ {len(bad)} 个账号有失败调用：")
            for e, d in bad[:5]:
                errs = {c: d[c].get("error") for c in calls
                        if (d.get(c) or {}).get("error")}
                print(f"      {e}: {str(errs)[:140]}")

    if len(out) > len(ok):
        print(f"\n失败 {len(out) - len(ok)} 个：")
        for r in out:
            if not r["ok"]:
                print(f"  {r['email']:38s} {r['reason'][:90]}")
    print("=" * 72)

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"workers": args.workers, "headless": args.headless,
             "offset": args.offset, "wall_s": round(wall, 2),
             "records": out}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已落盘 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
