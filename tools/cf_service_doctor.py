#!/usr/bin/env python3
"""邮箱 Worker / D1 服务端体检 —— 从**服务端**判断"到底修好没有"。

为什么要有这个工具
==================
我们踩过的坑是：Worker 的 `/admin/all` 报 500，客户端只能看到一个 Cloudflare
的 `Error 1101 worker_threw_exception` 页面 —— **异常文本拿不到**，于是"到底
是代码错、数据错、还是配额超了"完全靠猜。上一轮就是因为靠猜，把"配额超限"
误判成"代码还有 bug"，白折腾了半天。

实际上这些答案在 CF 的 API 里全都有，而且都是**权威数字**，不是推断：

  /workers/scripts/{name}                 -> 线上到底跑的哪个版本（modified_on/etag）
  /workers/scripts/{name}/deployments     -> 部署历史。🔴 version_id 重复出现 = 回滚！
  /workers/scripts/{name}/versions        -> 版本清单
  /d1/database/{db}                       -> 库大小、read_replication
  /d1/database/{db}/query                 -> 🔴 能直接拿到 D1 的**错误码**
                                             (7500 = 免费额度用尽)
  GraphQL d1AnalyticsAdaptiveGroups       -> 每天 / 每小时 rowsRead、rowsWritten
  GraphQL workersInvocationsAdaptive      -> 每天 / 每小时 success /
                                             scriptThrewException / exceededResources
  Worker 端点直连                          -> 到底哪条路由挂了

🔴 三条必须记住的判据（都是实测出来的）
--------------------------------------
1. **D1 免费额度**：rows read **5,000,000 / 天**、rows written **100,000 / 天**，
   **UTC 00:00 重置**（= 北京 08:00）。超了之后 D1 直接拒答，错误码 `7500`。
   Worker 里没 try/catch 的话，就表现为客户端看到 500 + Error 1101。

2. **拦的是"扫描行数"，不是"返回行数"**（官方定义：rows_read = rows scanned）。
   实测（配额用尽状态下）：
       SELECT 1                                     -> ✅ rows_read=0
       SELECT ... WHERE to_address='<不存在>' LIMIT 5 -> ✅ rows_read=0（走索引，0 个索引项）
       SELECT ... WHERE id = -1                     -> ❌ 7500（要扫表）
       SELECT ... ORDER BY id DESC LIMIT 1          -> ❌ 7500（读 1 行）
       SELECT COUNT(*)                              -> ❌ 7500
   ⇒ **"返回空"不等于"没被拦"**。判断某个按索引过滤的端点是否真的可用，
     不能只看它 200，要看它返回的到底是 0 条还是被 500 顶掉。

3. **不带浏览器 User-Agent 的请求会被 CF 边缘直接 403**（body 是
   `error code: 1010`，不是 Worker 回的）。用 python-urllib / requests 裸打
   会误判成"服务挂了"。必须带 UA。

用法
====
    export CF_API_TOKEN=...          # 🔴 只走环境变量，绝不落盘
    python tools/cf_service_doctor.py
    python tools/cf_service_doctor.py --hours 8
    python tools/cf_service_doctor.py --json .workbuddy-ai/exports/service_doctor.json

退出码：0 = 服务可用；1 = 被 D1 配额限制（等重置）；2 = token 无效；3 = 其他
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"

# ── 默认值：允许用环境变量覆盖，方便换账号/换库 ────────────────────────────
DEFAULT_ACCOUNT = os.getenv("CF_ACCOUNT_ID", "")
DEFAULT_WORKER = os.getenv("CF_WORKER_NAME", "")
DEFAULT_DB = os.getenv("CF_D1_DATABASE_ID", "")
DEFAULT_BASE = os.getenv("IR_WORKER_BASE", "")

# D1 Workers Free 计划的每日额度。超了就报 7500。
D1_FREE_ROWS_READ = 5_000_000
D1_FREE_ROWS_WRITTEN = 100_000

# 🔴 没有这个 UA，CF 边缘会用 1010 把请求拒掉（403），我们会把边缘拦截误读成
#    "Worker 挂了"。任何打 workers.dev 的探针都必须带上它。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

OK, BAD, WARN, INFO = "✅", "❌", "⚠️ ", "  "


# ══════════════════════════════════════════════════════════════════════════
# HTTP 小工具
# ══════════════════════════════════════════════════════════════════════════
def api_get(path: str, token: str, timeout: int = 60):
    """打 CF REST API。返回 (status, json|bytes)。"""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400].decode("utf-8", "replace")}


def api_get_raw(path: str, token: str, timeout: int = 120):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def d1_query(sql: str, token: str, account: str, db: str, timeout: int = 60):
    """打 D1 HTTP API。

    返回 (ok, rows, meta, err_code, err_msg)。

    🔴 关键点：**D1 的错误码在这里是拿得到的**（配额超限 = 7500）。Worker 那边
    抛出来是 Error 1101，看不见文本；这里能看见。排查时必须从这里入手。
    """
    body = json.dumps({"sql": sql}).encode()
    req = urllib.request.Request(
        f"{API}/accounts/{account}/d1/database/{db}/query",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            j = json.loads(resp.read())
        ok = True
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read())
        except Exception:
            j = {}
        ok = False
    except Exception as ex:  # 网络层
        return False, [], {}, None, repr(ex)

    res = (j.get("result") or [{}])
    res = res[0] if isinstance(res, list) else {}
    errs = j.get("errors") or []
    code = errs[0].get("code") if errs else None
    msg = errs[0].get("message") if errs else None
    return ok, (res.get("results") or []), (res.get("meta") or {}), code, msg


def gql(query: str, variables: dict, token: str, timeout: int = 120):
    req = urllib.request.Request(
        f"{API}/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": e.read()[:300].decode("utf-8", "replace")}]}


def probe_endpoint(base: str, path: str, token: str = "", timeout: int = 45):
    """直连 Worker 端点。返回 (status, bytes, ms, body)。

    🔴 一定要带 BROWSER_UA，否则会被 CF 边缘 1010 拒掉。
    """
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": BROWSER_UA}
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(base + path, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            b = resp.read()
            return resp.status, len(b), (time.time() - t0) * 1000, b
    except urllib.error.HTTPError as e:
        b = e.read()
        return e.code, len(b), (time.time() - t0) * 1000, b
    except Exception as e:
        return 0, 0, (time.time() - t0) * 1000, repr(e).encode()


def bj(hour_iso: str) -> str:
    """UTC ISO 小时 -> 北京时间字符串。所有报告都用北京时间，避免看错。"""
    try:
        t = dt.datetime.fromisoformat(hour_iso.replace("Z", "+00:00"))
        return (t + dt.timedelta(hours=8)).strftime("%m-%d %H:%M")
    except Exception:
        return hour_iso


# ══════════════════════════════════════════════════════════════════════════
# 各项检查
# ══════════════════════════════════════════════════════════════════════════
def check_token(token: str, account: str) -> dict:
    print("── 1. Token 与账号 ─────────────────────────────────────")
    st, j = api_get("/accounts", token)
    if st != 200:
        print(f"  {BAD} /accounts HTTP {st}: {json.dumps(j, ensure_ascii=False)[:200]}")
        return {"ok": False}
    accts = j.get("result") or []
    hit = next((a for a in accts if a.get("id") == account), None)
    print(f"  {OK} token 有效，可见账号 {len(accts)} 个")
    if hit:
        print(f"  {OK} 目标账号: {hit.get('id')} / {hit.get('name')}")
    else:
        print(f"  {WARN} 账号 {account} 不在可见列表里")
    # 🔴 /user/tokens/verify 返回 1000 Invalid API Token 是**正常**的：
    #    那个端点要求 "User API Tokens Read" 权限，跟能不能操作 D1/Workers 无关。
    #    别拿它当"token 失效"的判据。
    return {"ok": True, "accounts": len(accts)}


def check_worker(token: str, account: str, worker: str) -> dict:
    print("\n── 2. Worker 版本与部署历史 ─────────────────────────────")
    out: dict = {}
    st, j = api_get(f"/accounts/{account}/workers/scripts", token)
    if st == 200:
        me = next((s for s in (j.get("result") or []) if s.get("id") == worker), None)
        if me:
            print(f"  {OK} {worker}  modified_on={me.get('modified_on')}  "
                  f"(北京 {bj(me.get('modified_on') or '')})")
            out["modified_on"] = me.get("modified_on")
        else:
            print(f"  {BAD} 账号下没有名为 {worker} 的 Worker")
    else:
        print(f"  {BAD} 列 scripts HTTP {st}")

    st, j = api_get(f"/accounts/{account}/workers/scripts/{worker}/deployments", token)
    if st == 200:
        deps = ((j.get("result") or {}).get("deployments") or [])[:8]
        seen: dict[str, str] = {}
        print(f"  {INFO} 最近 {len(deps)} 次部署（version_id 重复 = 回滚到旧版本）:")
        for d in deps:
            when = d.get("created_on") or ""
            src = d.get("source")
            for v in (d.get("versions") or []):
                vid = v.get("version_id") or ""
                flag = ""
                if vid in seen:
                    flag = f"  {WARN} 与 {seen[vid]} 同一版本 → 回滚！"
                else:
                    seen[vid] = when
                print(f"      {when}  {src:<9} {vid[:12]}{flag}")
        out["deployments"] = deps
    return out


def check_d1_usage(token: str, account: str, db: str, hours: int) -> dict:
    print("\n── 3. D1 每日用量（限额：读 5,000,000 / 写 100,000，UTC 00:00 重置）──")
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=3)).date()
    q = """query($a:String!,$db:String!,$d:Date!){
      viewer{ accounts(filter:{accountTag:$a}){
        d1AnalyticsAdaptiveGroups(limit:20, filter:{date_geq:$d, databaseId:$db}, orderBy:[date_ASC]){
          dimensions{ date } sum{ rowsRead rowsWritten readQueries writeQueries } }}}}
    """
    g = gql(q, {"a": account, "db": db, "d": str(since)}, token)
    if g.get("errors"):
        print(f"  {BAD} GraphQL: {json.dumps(g['errors'], ensure_ascii=False)[:220]}")
        return {}
    rows = (((g.get("data") or {}).get("viewer") or {}).get("accounts") or [{}])[0].get(
        "d1AnalyticsAdaptiveGroups") or []
    today = str(now.date())
    res: dict = {"days": []}
    for r in rows:
        d = r["dimensions"]["date"]
        s = r.get("sum") or {}
        rr, rw = s.get("rowsRead") or 0, s.get("rowsWritten") or 0
        rq = s.get("readQueries") or 0
        pr = 100 * rr / D1_FREE_ROWS_READ
        pw = 100 * rw / D1_FREE_ROWS_WRITTEN
        mark = ""
        if pr >= 100:
            mark = f"  {BAD} 读额度已爆"
        elif pr >= 70:
            mark = f"  {WARN} 读额度 >70%"
        if pw >= 80:
            mark += f"  {WARN} 写额度 {pw:.0f}%"
        print(f"  {d}  读 {rr:>12,} ({pr:5.1f}%)  写 {rw:>8,} ({pw:5.1f}%)  "
              f"读次数 {rq:>8,}  均 {rr // rq if rq else 0:>6,} 行/次{mark}")
        res["days"].append({"date": d, "rowsRead": rr, "rowsWritten": rw,
                            "readQueries": rq, "pct_read": round(pr, 2), "pct_write": round(pw, 2)})
        if d == today:
            res["today"] = res["days"][-1]

    # 小时粒度：定位"哪一小时爆的"，并验证修复是否真的止住了血
    s_iso = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")
    e_iso = now.strftime("%Y-%m-%dT%H:00:00Z")
    q2 = """query($a:String!,$db:String!,$s:Time!,$e:Time!){
      viewer{ accounts(filter:{accountTag:$a}){
        d1AnalyticsAdaptiveGroups(limit:200,
          filter:{datetime_geq:$s, datetime_leq:$e, databaseId:$db}, orderBy:[datetimeHour_ASC]){
          dimensions{ datetimeHour } sum{ rowsRead rowsWritten readQueries } }}}}
    """
    g = gql(q2, {"a": account, "db": db, "s": s_iso, "e": e_iso}, token)
    rows = (((g.get("data") or {}).get("viewer") or {}).get("accounts") or [{}])[0].get(
        "d1AnalyticsAdaptiveGroups") or []
    if rows:
        print(f"\n  {INFO} 小时粒度（最近 {hours}h，北京时间为准）—— 均行数突然变大 = 全表扫复活:")
        peak = max(((r.get("sum") or {}).get("rowsRead") or 0) for r in rows) or 1
        res["hourly"] = []
        for r in rows:
            h = r["dimensions"]["datetimeHour"]
            s = r.get("sum") or {}
            rr, rq = s.get("rowsRead") or 0, s.get("readQueries") or 0
            avg = rr // rq if rq else 0
            bar = "█" * int(30 * rr / peak)
            flag = f"  {BAD} 单小时超限额" if rr > D1_FREE_ROWS_READ else ""
            print(f"      {h} (北京 {bj(h)})  读 {rr:>11,}  次 {rq:>7,}  均 {avg:>6,}  {bar}{flag}")
            res["hourly"].append({"hour": h, "rowsRead": rr, "readQueries": rq, "avg": avg})
    return res


def check_worker_invocations(token: str, account: str, worker: str, hours: int) -> dict:
    print("\n── 4. Worker 调用结果（哪些小时在抛异常）─────────────────")
    now = dt.datetime.now(dt.timezone.utc)
    s_iso = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")
    e_iso = now.strftime("%Y-%m-%dT%H:00:00Z")
    q = """query($a:String!,$s:Time!,$e:Time!){
      viewer{ accounts(filter:{accountTag:$a}){
        workersInvocationsAdaptive(limit:200,
          filter:{scriptName:"%s", datetime_geq:$s, datetime_leq:$e}, orderBy:[datetimeHour_ASC]){
          dimensions{ datetimeHour status } sum{ requests errors } }}}}
    """ % worker
    g = gql(q, {"a": account, "s": s_iso, "e": e_iso}, token)
    if g.get("errors"):
        print(f"  {BAD} GraphQL: {json.dumps(g['errors'], ensure_ascii=False)[:220]}")
        return {}
    rows = (((g.get("data") or {}).get("viewer") or {}).get("accounts") or [{}])[0].get(
        "workersInvocationsAdaptive") or []
    agg: dict = {}
    for r in rows:
        d = r["dimensions"]
        s = r.get("sum") or {}
        agg.setdefault(d["datetimeHour"], {})[str(d.get("status"))] = s.get("requests") or 0
    out = {"hourly": []}
    for h in sorted(agg):
        parts = agg[h]
        bad = sum(v for k, v in parts.items() if k != "success")
        # exceededResources = Worker CPU/内存超限 —— 全表 SELECT * 大字段的典型症状
        tag = ""
        if parts.get("exceededResources"):
            tag = "  ← exceededResources = CPU/内存超限，查是不是在扫全表"
        print(f"  {h} (北京 {bj(h)})  " +
              "  ".join(f"{k}={v:,}" for k, v in sorted(parts.items())) +
              (f"  失败={bad:,}{tag}" if bad else ""))
        out["hourly"].append({"hour": h, **parts, "failed": bad})
    return out


def check_d1_probe(token: str, account: str, db: str) -> dict:
    """读 0 行 vs 读 ≥1 行 —— 直接判定配额是不是正在拦。

    🔴 这是整个工具里最有价值的一条：它把"服务到底还能不能用"变成一个
       二值判据，而不是靠端点返回 500 去猜。
    """
    print("\n── 5. D1 探针（判定配额是否正在拦截）─────────────────────")
    cases = [
        ("不碰表（SELECT 1）", "SELECT 1 AS x"),
        ("索引查找，0 命中", "SELECT id FROM emails WHERE to_address = 'doctor-none@nowhere.test' LIMIT 5"),
        ("读 1 行", "SELECT id FROM emails ORDER BY id DESC LIMIT 1"),
    ]
    out = {"cases": []}
    blocked = 0
    for label, sql in cases:
        ok, rows, meta, code, msg = d1_query(sql, token, account, db)
        rr = meta.get("rows_read")
        flag = OK if ok else BAD
        print(f"  {flag} rows_read={str(rr):>6}  {label:<20} " +
              (f"err={code}" if code else ""))
        if not ok and code == 7500:
            blocked += 1
        out["cases"].append({"label": label, "ok": ok, "rows_read": rr, "err": code})
    out["quota_blocking"] = blocked > 0
    if blocked:
        print(f"  {BAD} D1 正在用 7500 拒绝读取 → 今日额度已爆，等 UTC 00:00（北京 08:00）重置")
        nxt = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
            + dt.timedelta(days=1)
        print(f"      预计恢复：{nxt.strftime('%Y-%m-%d %H:%M')} UTC = "
              f"{(nxt + dt.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')} 北京 "
              f"（约 {(nxt - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600:.1f} 小时后）")
    else:
        print(f"  {OK} 未发现 7500 —— 读取正常")
    return out


def check_endpoints(base: str, admin_token: str) -> dict:
    print("\n── 6. Worker 端点直连（带浏览器 UA）─────────────────────")
    probes = [
        ("/health", False),
        ("/api/domains", False),
        ("/api/inbox?email=doctor-none@nowhere.test", False),   # 走索引，0 命中
        ("/admin/all?limit=1", True),                            # 要读行
    ]
    out = {"probes": []}
    for path, need_tok in probes:
        st, n, ms, b = probe_endpoint(base, path, admin_token if need_tok else "")
        note = ""
        if st == 403 and b"1010" in b:
            note = f"  {WARN} CF 边缘 1010（缺浏览器 UA 或 WAF）"
        elif st == 200:
            try:
                j = json.loads(b)
                if isinstance(j, dict) and "messages" in j:
                    note = f"  messages={len(j.get('messages') or [])}"
                elif isinstance(j, dict) and j.get("ok") is not None:
                    note = f"  ok={j.get('ok')}"
            except Exception:
                pass
        elif st >= 500:
            note = "  ← 大概率是 D1 配额 7500 冒泡成未捕获异常"
        print(f"  {st if st else 'ERR'}  {ms:6.0f}ms  {n:>6}B  {path}{note}")
        out["probes"].append({"path": path, "status": st, "ms": round(ms), "bytes": n})
    return out


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="邮箱 Worker / D1 服务端体检")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--worker", default=DEFAULT_WORKER)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--base", default=DEFAULT_BASE, help="Worker 对外地址")
    ap.add_argument("--hours", type=int, default=12, help="看最近多少小时的小时粒度")
    ap.add_argument("--json", dest="json_out", default="", help="把结果写到这个文件")
    args = ap.parse_args()

    # 🔴 账号 / Worker 名 / 库 ID / Worker 地址都不再写死在仓库里
    #    （本仓库是公开的），必须由环境变量或命令行提供。
    if not args.base:
        print(f"{BAD} 缺少 IR_WORKER_BASE / --base（Worker 对外地址）。")
        print("     这个值不写死在仓库里，请用环境变量或命令行参数提供。")
        return 2

    token = os.getenv("CF_API_TOKEN", "").strip()
    admin = os.getenv("IR_WORKER_ADMIN_TOKEN", "").strip()
    full = bool(token)

    print("═" * 74)
    print("  邮箱 Worker / D1 服务端体检")
    print(f"  账号 {args.account} / Worker {args.worker} / 库 {args.db}")
    print(f"  报告时间（UTC）{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}"
          f"  = 北京 {(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)):%Y-%m-%d %H:%M:%S}")
    if not full:
        print(f"  {WARN} 未提供 CF_API_TOKEN —— 只跑「端点直连」部分；"
              f"D1 用量 / 部署历史 / 配额探针会跳过。")
    print("═" * 74)

    report: dict = {"mode": "full" if full else "endpoints_only"}

    # 🔴 没有 CF_API_TOKEN 时**降级而不是退出**：端点直连那一半不需要 CF 凭据，
    #    而它恰好包含最有价值的那条判据（`/admin/all?limit=1` 必须读 ≥1 行）。
    #    这样无人值守的定时任务就不必把凭据持久化到任何文件里。
    if full and not (args.account and args.worker and args.db):
        print(f"{BAD} full 模式还需要 CF_ACCOUNT_ID / CF_WORKER_NAME / CF_D1_DATABASE_ID")
        print("     它们不写死在仓库里，请用环境变量或命令行参数提供。")
        return 2

    if full:
        t = check_token(token, args.account)
        report["token"] = t
        if not t.get("ok"):
            print(f"\n{BAD} CF_API_TOKEN 无效 —— 降级为「只跑端点直连」。")
            full = False
            report["mode"] = "endpoints_only"

    if full:
        report["worker"] = check_worker(token, args.account, args.worker)
        report["d1_usage"] = check_d1_usage(token, args.account, args.db, args.hours)
        report["invocations"] = check_worker_invocations(token, args.account, args.worker, args.hours)
        report["d1_probe"] = check_d1_probe(token, args.account, args.db)

    report["endpoints"] = check_endpoints(args.base, admin)

    # ── 结论 ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 74)
    print("  结论")
    print("═" * 74)

    probes = {p["path"]: p for p in report["endpoints"]["probes"]}
    list_status = (probes.get("/admin/all?limit=1") or {}).get("status")

    if full:
        quota = bool((report.get("d1_probe") or {}).get("quota_blocking"))
        today = (report.get("d1_usage") or {}).get("today") or {}
        pct = today.get("pct_read", 0)

        if quota:
            print(f"  {BAD} 服务当前【不可用】：D1 免费读取额度已用尽（今日 {pct:.1f}%），")
            print("     任何需要读 ≥1 行的查询都会被拒（错误码 7500），Worker 里没兜住 → 客户端看到 500。")
            print("     恢复时间：UTC 00:00 = 北京 08:00（自动重置，无需操作）。")
            print(f"  {INFO} 注意：这不代表「没修好」。要判断代码是否已修，看第 3 节小时粒度的")
            print("     「均行数」—— 修复后应回落到几十行/次，而不是 1,000+。")
            rc = 1
        elif pct >= 70:
            print(f"  {WARN} 服务可用，但今日读取额度已用 {pct:.1f}%，继续高频轮询会再次打爆。")
            rc = 0
        else:
            print(f"  {OK} 服务可用，额度健康（今日读取 {pct:.1f}%）。")
            rc = 0

        # 🔴 「均行数稀释」只看**最近 2 小时**。
        #    把恢复前的小时也算进来，会得出「服务明明恢复了、工具还在喊失败率 49%」
        #    的假警报（实测踩过：窗口 8h，其中 2h 是配额耗尽期，6h 健康 ⇒ 整体 49%）。
        #    整体失败率高但近期低 ⇒ 那是历史，不是现状。
        inv = (report.get("invocations") or {}).get("hourly") or []

        def _rate(rows):
            r = sum(sum(v for k, v in h.items()
                        if isinstance(v, int) and k not in ("hour", "failed")) for h in rows)
            f = sum(h.get("failed", 0) for h in rows)
            return r, f

        tot_r, tot_f = _rate(inv)
        rec_r, rec_f = _rate(inv[-2:] if len(inv) >= 2 else inv)
        if rec_r and rec_f / rec_r > 0.3:
            print(f"  {WARN} 最近 2h 失败率 {100 * rec_f / rec_r:.0f}%（{rec_f:,}/{rec_r:,}）"
                  f" —— 此时「均行数」已被大量被拒查询稀释，**不能再当健康判据**。")
            print("     必须和失败率成对看：均行数小 + 失败率低 = 真健康；")
            print("     均行数小 + 失败率高 = 大部分查询压根没跑起来。")
        elif tot_r and tot_f / tot_r > 0.3:
            print(f"  {INFO} 窗口内整体失败率 {100 * tot_f / tot_r:.0f}%（{tot_f:,}/{tot_r:,}），"
                  f"但最近 2h 只有 {100 * rec_f / rec_r:.0f}% —— 失败集中在更早的小时，属历史，不是现状。")
    else:
        # 没 token：只能看端点。`/admin/all?limit=1` 是决定性判据 ——
        # 它必须读 ≥1 行，所以配额一旦爆掉它必然 500。
        if list_status == 200:
            print(f"  {OK} 端点已恢复：/admin/all?limit=1 返回 200。")
            rc = 0
        else:
            print(f"  {BAD} 端点仍未恢复：/admin/all?limit=1 返回 {list_status or 'ERR'}。")
            print("     它必须读 ≥1 行 —— 被拒通常意味着 D1 每日读取额度还没重置。")
            rc = 1
        print(f"  {WARN} 本次没做 D1 用量核对（缺 CF_API_TOKEN），"
              f"无法区分「配额」还是「别的 500 原因」。")
        print("     要拿 D1 用量/部署历史/配额探针：export CF_API_TOKEN=... 再跑一次。")

    if args.json_out:
        p = args.json_out
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n  {INFO} 结果已写入 {p}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
