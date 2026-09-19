"""对**已有账号**跑通下游全链路：登录 → 查额度 → 建/复用 Key → 真发推理。

为什么需要这个工具
------------------
`run.py` 跑的是**完整**链路，第一步就是注册 —— 而注册当前被封（`B0000`，
IP 维度累计配额）。这意味着**注册之后的每一段都测不了**，哪怕它们和封禁
毫无关系（登录走 SSO、建 Key 走 discovery、推理走 discovery-api，三个
不同主机）。

这个工具从 `results.json` 里取**已经注册成功**的账号，只跑下游：

    Stage 3  登录（Playwright，取 JWT + 浏览器 Cookie）
    Stage 4  查额度（纯只读：getUserInfo / free-grant-status / balance / list_keys）
    Stage 5  建/复用 API Key（`ensure_key` 幂等）
    Stage 6  真发一次 chat/completions —— 唯一能证明"这个账号真能用"的判据

**全程零注册请求**，因此不会加深封禁，可以随便跑。

为什么要复用 `src/ledger.py` 而不是自己写 `--out`
-------------------------------------------------
`results.json` 同时是"运行报告"和"账号台账"。本项目已经因为"直接覆盖"
丢过一次台账（53 条 → 1 条）。所以这里的写盘走 `ledger.save()`：
按 email 合并、失败不盖成功、条数不得变少的护栏。**任何会写台账的工具
都必须复用同一个合并实现**，不能各写各的。

🔴 三个必须小心的点
-------------------
1. **`ensure_key` 命中已有 key 时返回 `key=""`**（列表接口不返回明文）。
   所以复用分支**绝不能**用它去覆盖 `rec.api_key` —— 那会把一把好 key
   洗成空字符串。本工具用 `or` 兜底：新明文优先，否则保留原值。

2. **`create_key` 是非幂等的**（每次新 `Idempotency-Key`，每次建一把新的）。
   默认**不建**，只做幂等复用；要验证"建 Key"这条路径得显式给 `--create N`。
   否则每跑一次工具就在每个账号上多堆一把 key。

3. **`GET/POST /tokenplan/v1/keys` 只带 Authorization 会 `-10002`**，
   必须同时带浏览器会话 Cookie。所以 Stage 5 不能省掉浏览器登录。

用法：
    python tools/run_downstream.py                      # 4 个账号，只读+复用
    python tools/run_downstream.py --limit 12 --workers 4
    python tools/run_downstream.py --create 2           # 其中 2 个真建新 key
    python tools/run_downstream.py --no-write           # 只看结果，不落盘
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

DEFAULT_IN = ROOT / "results.json"
DEFAULT_OUT = ROOT / "results.json"


# ────────────────────────────────────────────────────────────────
# 记录构造
# ────────────────────────────────────────────────────────────────
def _to_record(d: dict):
    """dict → AccountRecord。只喂 dataclass 认识的字段。

    🔴 不能用 `AccountRecord(**d)` 直接展开：台账里混着历史字段
    （`source` / `verify` 是早期版本写的），多一个未知 key 就 TypeError，
    整个工具跑不起来。这里显式取交集，未知字段原样留在 dict 里不丢。
    """
    from dataclasses import fields

    from src.pipeline import AccountRecord

    known = {f.name for f in fields(AccountRecord)}
    return AccountRecord(**{k: v for k, v in d.items() if k in known})


def _eligible(d: dict) -> bool:
    """这个账号能不能跑下游。

    判据 = **有 email + 有 password**。不需要 jwt —— jwt 只有 14 天有效期，
    而且下游第一步就是重新登录拿一个新的，本来就该以"能不能登录"为准。
    """
    return bool((d.get("email") or "").strip() and (d.get("password") or "").strip())


# ────────────────────────────────────────────────────────────────
# 单账号：Stage 3~6
# ────────────────────────────────────────────────────────────────
def run_one(d: dict, *, headless: bool, create: bool, key_name: str,
            log=print) -> dict:
    """跑一个账号的下游链路，返回**要合并回台账的字段**（不整条覆盖）。

    返回 dict 而不是 AccountRecord：台账里那 38 条从 CSV 恢复的记录字段
    比 dataclass 少，整条覆盖会把它们已有的 `source` 等字段洗掉。
    """
    from src.discovery import DiscoveryClient

    rec = _to_record(d)
    out: dict = {"email": rec.email}
    t_all = time.time()
    timings: dict = {}

    # ── Stage 3：登录 ─────────────────────────────────────────
    t = time.time()
    try:
        from src.browser import BrowserSession

        with BrowserSession(headless=headless) as sess:
            res = sess.login(rec.email, rec.password, verbose=False)
            timings["browser_launch"] = sess.launch_ms
            if not res.ok:
                raise RuntimeError(res.reason or "login failed")
            rec.jwt = res.jwt
            timings["login"] = round((time.time() - t) * 1000)
            timings["login_detail"] = res.timings
            out["jwt"] = res.jwt
            out["login_ms"] = timings["login"]
            cs = res.captcha_stage or {}
            log(f"✓ 登录 {timings['login']}ms  jwt={len(res.jwt)}B  "
                f"captcha={cs.get('path', '?')}")
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"login_failed: {str(ex)[:160]}"
        out["login_ms"] = round((time.time() - t) * 1000)
        log(f"✗ 登录失败：{str(ex)[:140]}")
        return out

    # ── Stage 4：只读（user_info / grant / balance / list_keys）──
    t = time.time()
    try:
        dc = DiscoveryClient(jwt=rec.jwt, cookies=res.cookies)

        info = dc.get_user_info()
        out["sso_username"] = info.get("sso_username", "")
        out["sso_email"] = info.get("sso_email", "")

        grant = dc.free_grant_status()
        out["grant_has_received"] = grant.get("has_received")
        claimed = False
        if not grant.get("has_received"):
            # 幂等：没领过才领。已经领过就别再打这个接口。
            dc.claim_free_grant()
            claimed = True
            out["grant_has_received"] = True
        out["grant_claimed_now"] = claimed

        bal = dc.balance()
        out["credits"] = str(bal.get("available_credits", ""))
        # 🔴 原始 balance 全量留档。上一轮发现 12 个账号里有
        #    `6.547000` / `9.936000` / `9.999000` 这种非 10 的取值，
        #    只存 `available_credits` 一个数查不出原因（是被消耗了？
        #    还是发放口径不同？），必须连 `usage_windows` 一起留下。
        out["balance_raw"] = bal

        keys = dc.list_keys()
        out["key_count"] = len(keys)
        out["key_names"] = [k.get("name", "") for k in keys]

        timings["readonly"] = round((time.time() - t) * 1000)
        log(f"✓ 只读 {timings['readonly']}ms  credits={out['credits']}  "
            f"keys={out['key_count']}{[k.get('name') for k in keys]}")
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"readonly_failed: {str(ex)[:160]}"
        timings["readonly"] = round((time.time() - t) * 1000)
        log(f"✗ 只读失败：{str(ex)[:140]}")
        return out

    # ── Stage 5：建/复用 Key ──────────────────────────────────
    t = time.time()
    plaintext = ""
    try:
        if create:
            # 非幂等：显式要才建。名字带时间戳，避免和已有的 default 撞名
            # 从而"以为建了其实只是复用了"。
            name = f"{key_name}-{time.strftime('%m%d-%H%M%S')}"
            ak = dc.create_key(name)
            plaintext = ak.key
            out["key_created"] = name
            # 🔴 **新建的明文必须落盘** —— 这是 `--create` 的唯一产出。
            #
            # 2026-09-19 修：原先这里只把明文存进局部变量 `plaintext` 用于
            # Stage 6 验证，**从不写进 `out`** ⇒ 下游跑完、验证也过了
            # （`verify=ok(10 models)`），但台账里 `api_key` 是空的 ——
            # 账号可用、key 却拿不回来，等于白建。
            # 而且 `create_key` 是**非幂等**的（见上面第 2 点），
            # 想再拿一次只能再建一把，凭空多一把孤儿 key。
            #
            # ⚠ 与**复用分支相反**：复用命中的 key 列表接口不返回明文
            #   （`ak.key == ""`），那种情况绝不能赋值，否则会把已有的
            #   好 key 洗成空串。所以这里必须判 `if ak.key`。
            if ak.key:
                out["api_key"] = ak.key
            log(f"✓ 新建 key「{name}」= {ak.key[:12]}…")
        else:
            ak = dc.ensure_key(key_name)
            # 🔴 命中已有 key 时 `ak.key` 是空串（列表不给明文）。
            #    绝不能用它覆盖 rec.api_key —— 见模块 docstring 第 1 点。
            plaintext = ak.key
            out["key_created"] = "" if ak.key else None
            log(f"✓ 复用 key「{key_name}」"
                f"{'（无明文，列表接口不返回）' if not ak.key else ''}")
        out["key_id"] = ak.id
        out["key_masked"] = ak.masked_key
        out["key_status"] = ak.status
        timings["key"] = round((time.time() - t) * 1000)
    except Exception as ex:                                       # noqa: BLE001
        out["downstream"] = f"key_failed: {str(ex)[:160]}"
        timings["key"] = round((time.time() - t) * 1000)
        log(f"✗ 建 Key 失败：{str(ex)[:140]}")
        return out

    # ── Stage 6：真发一次推理 ─────────────────────────────────
    # 明文 key 的优先来源：本次新建 > 台账里已有的。
    # 台账里那把是**同一个账号**的 key，用它验证等价于验证这个账号。
    verify_key = plaintext or (d.get("api_key") or "")
    out["verify_key_source"] = ("new" if plaintext else
                                "ledger" if d.get("api_key") else "none")
    if verify_key:
        t = time.time()
        try:
            from src import apikey as _ak

            # 🔴 **新建的 key 有传播延迟**：`POST /tokenplan/v1/keys` 已经返回
            #    sk-...，但立刻拿去打 `/v1/models` 会 **401 Unauthorized**。
            #    2026-09-18 实测：`--create 1` 那个账号就这样报了 401，
            #    而它明明刚建成功 —— 这不是 key 无效，是还没在网关侧生效。
            #    复用路径不需要等（老 key 早就生效了），只对新建的等。
            if plaintext:
                _ak.wait_until_active(verify_key, attempts=4, delay=3.0)

            models = _ak.list_models(verify_key)
            out["verify_models"] = len(models)
            res2 = _ak.chat(verify_key, "只回复两个字：成功", max_tokens=128)
            timings["verify"] = round((time.time() - t) * 1000)
            if res2.ok and not res2.truncated:
                out["verify"] = f"ok({len(models)} models)"
                out["verify_reply"] = res2.text[:40]
                out["verify_usage"] = res2.usage
                log(f"✓ 推理 {timings['verify']}ms  {len(models)} models  "
                    f"reply={res2.text[:20]!r}  usage={res2.usage.get('total_tokens')}")
            elif res2.ok and res2.truncated:
                # 网关通了、模型在推理，只是 reasoning 把 max_tokens 吃光了。
                # 这不是失败 —— 见 src/apikey.py 的 ChatResult 注释。
                out["verify"] = f"ok({len(models)} models, 正文被截断)"
                out["verify_reply"] = ""
                log(f"✓ 推理 {timings['verify']}ms  {len(models)} models  "
                    f"正文被 reasoning 截断（非失败）")
            else:
                out["verify"] = f"failed: {res2.error[:120]}"
                log(f"✗ 推理失败：{res2.error[:140]}")
        except Exception as ex:                                   # noqa: BLE001
            msg = f"{type(ex).__name__}: {ex}"[:160]
            # 401 + 本次是新建的 key = 传播还没到位，**不是** key 坏了。
            # 旧实现直接写 `failed`，会把一个刚建成功的账号误报成失败。
            if plaintext and "401" in msg:
                out["verify"] = "not active yet(401, 传播延迟)"
                log("⚠ 新建 key 网关侧尚未生效（401），属传播延迟，非失败")
            else:
                out["verify"] = f"failed: {msg[:120]}"
                log(f"✗ 推理异常：{msg[:140]}")
            timings["verify"] = round((time.time() - t) * 1000)
    else:
        out["verify"] = "skipped: no plaintext key"

    out["downstream"] = "ok"
    out["downstream_ms"] = round((time.time() - t_all) * 1000)
    out["timings_downstream"] = timings
    out["downstream_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 显式清空 `error`：合并是**并集**（见 src/ledger.merge_records），
    # 老记录里若留着上次的失败原因，不清就会挂在一个已经跑通的账号上。
    out["error"] = ""
    return out


# ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="对已有账号跑下游全链路（零注册请求）")
    ap.add_argument("--from", dest="src", default=str(DEFAULT_IN),
                    help="账号台账来源")
    ap.add_argument("--limit", type=int, default=4, help="跑几个账号（0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="并发数（受内存约束）")
    ap.add_argument("--create", type=int, default=0,
                    help="其中前 N 个**真建新 key**（验证建 Key 路径）。"
                         "0=全部只做幂等复用（默认，不污染 key 列表）")
    ap.add_argument("--key-name", default="default", help="复用/新建的 key 名")
    ap.add_argument("--headless", action="store_true", help="无头浏览器")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="台账输出")
    ap.add_argument("--no-write", action="store_true", help="不写台账，只打印")
    args = ap.parse_args()

    from src import ledger

    src_path = Path(args.src)
    existing = ledger.load_existing(src_path)
    if not existing:
        print(f"✗ 读不到台账：{src_path}")
        return 1

    pool = [d for d in existing if _eligible(d)]
    if not pool:
        print(f"✗ {len(existing)} 条记录里没有可用的（需要 email + password）")
        return 1

    # 优先挑有 jwt 的（说明曾经登录成功过，密码大概率仍有效），
    # 再挑有 api_key 的（Stage 6 能直接拿它验，不依赖本次是否建 key）。
    pool.sort(key=lambda d: (not d.get("jwt"), not d.get("api_key")))
    n = len(pool) if args.limit <= 0 else min(args.limit, len(pool))
    targets = pool[:n]

    print(f"台账 {len(existing)} 条 → 可用 {len(pool)} 条 → 本次跑 {n} 条"
          f"（并发 {args.workers}，"
          f"{'建新 key' if args.create else '仅幂等复用'}）")
    print("⚠ 零注册请求：本工具不打注册接口，不会加深 B0000 封禁")
    print("=" * 72)

    lock = threading.Lock()
    results: list[dict] = [None] * n

    def work(i: int, d: dict) -> None:
        tag = f"[{i + 1}/{n}] {d.get('email', '?')[:34]}"
        def log(msg: str):
            with lock:
                print(f"{tag} {msg}", flush=True)
        try:
            # 🔴 `{**d, **out}` 而不是直接 `out`：台账写回是**按 email 整条替换**的
            #    （见 src/ledger.merge_records）。若只交增量字段，一旦这条胜出
            #    就会把 username/password/api_key 全洗掉。交超集才安全 ——
            #    无论哪一方胜出，信息都不减少。
            results[i] = {**d, **run_one(d, headless=args.headless,
                                         create=(i < args.create),
                                         key_name=args.key_name, log=log)}
        except Exception as ex:                                   # noqa: BLE001
            results[i] = {**d, "downstream": f"crash: {str(ex)[:160]}"}
            log(f"✗ 崩溃：{str(ex)[:140]}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for f in as_completed([ex.submit(work, i, d) for i, d in enumerate(targets)]):
            f.result()
    wall = time.time() - t0
    results = [r for r in results if r]

    # ── 汇总 ──────────────────────────────────────────────────
    def cnt(key, pred=lambda v: True):
        return sum(1 for r in results if pred(r.get(key)))

    ok_login = cnt("login_ms")
    ok_key = cnt("key_id")
    ok_ver = cnt("verify", lambda v: isinstance(v, str) and v.startswith("ok"))
    no_key = cnt("verify", lambda v: v == "skipped: no plaintext key")
    pending = cnt("verify", lambda v: isinstance(v, str) and v.startswith("not active"))

    print("\n" + "=" * 72)
    print(f"下游全链路  {n} 账号 / 并发 {args.workers}  wall={wall:.1f}s")
    print(f"  登录      {ok_login}/{n}")
    print(f"  只读额度  {cnt('credits')}/{n}")
    print(f"  建/复用Key {ok_key}/{n}")
    print(f"  真实推理  {ok_ver}/{n}"
          + (f"（{no_key} 个无明文 key 跳过）" if no_key else "")
          + (f"（{pending} 个新建 key 传播未到位）" if pending else ""))

    # ── 额度分布：上一轮发现过非 10 的取值，这里必须显式列出来 ──
    creds = [r.get("credits") for r in results if r.get("credits")]
    if creds:
        uniq = sorted(set(creds))
        print(f"\n  credits 分布：{uniq}")
        # 把每个窗口的"已用/剩余"摊开 —— `available_credits` 只是各窗口的
        # 最小值，光看它分不清是哪个窗口被消耗了。
        print(f"  {'账号':38s} {'5h 已用/剩余':>18s} {'7d 已用/剩余':>18s}  available")
        for r in results:
            b = r.get("balance_raw") or {}
            w = b.get("usage_windows") or {}
            # ⚠ `w=w` 是**显式绑定**，不是冗余：`fmt` 是闭包，若不绑定就会捕获
            #   循环变量 `w` 的**引用**。当前是在同轮内立即调用（所以行为正确），
            #   但一旦有人把 `fmt` 存起来延后调用，全部窗口会变成最后一条记录的。
            def fmt(k, w=w):
                x = w.get(k) or {}
                if not x:
                    return "-"
                return f"{x.get('used_credits')}/{x.get('remaining_credits')}"
            print(f"  {r['email']:38s} {fmt('5h'):>18s} {fmt('7d'):>18s}"
                  f"  {b.get('available_credits')}")

        odd = [r for r in results if r.get("credits") not in ("10.000000", "10")]
        if odd:
            print(f"\n  ⚠ {len(odd)} 个账号 credits ≠ 10（原始 balance 已存进"
                  f" exports/balance_dump.json）")

    # 原始 balance 落盘：只读接口拿到的，是排查"额度去哪了"的唯一证据。
    # 追加式保存（按 email 覆盖同一条），避免每次跑都丢上一次的快照。
    if any(r.get("balance_raw") for r in results):
        dump_p = (ROOT / ".workbuddy-ai" / "exports" / "balance_dump.json")
        dump_p.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        if dump_p.is_file():
            try:
                old = json.loads(dump_p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = {}
        for r in results:
            if r.get("balance_raw"):
                old[r["email"]] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                   "balance": r["balance_raw"]}
        dump_p.write_text(json.dumps(old, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"\n  balance 原始快照已存 {dump_p}（累计 {len(old)} 个账号）")

    failed = [r for r in results if r.get("downstream") != "ok"]
    if failed:
        print(f"\n  未走通 {len(failed)} 条：")
        for r in failed:
            print(f"    {r['email']:38s} {r.get('downstream')}")

    # ── 写回台账（合并，不覆盖）──────────────────────────────
    if args.no_write:
        print("\n（--no-write：未写台账）")
        return 0

    merged, kept, added, upgraded = ledger.merge_records(existing, results)
    print(f"\n结果合并：原有 {kept} 条 + 本次新增 {added} 条"
          + (f"（{upgraded} 条已更新：升级或补全字段）" if upgraded else "")
          + f" = {len(merged)} 条")
    try:
        ledger.save(src_path, merged)
    except ValueError as ex:
        print(f"✗ {ex}", file=sys.stderr)
        return 3
    print(f"台账已更新 {src_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
