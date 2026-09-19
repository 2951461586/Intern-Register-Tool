"""探测代理是否**真的能用于本项目**（换出口 IP 绕开注册封禁）。

为什么不能只看"能不能连上"
--------------------------
本项目实测（2026-09-16）踩了三个坑，每一个都会让"代理可用"的结论变假：

🔴 坑 1：**TCP 连通性完全不能作为判据**
   本机跑着 Clash Verge（TUN 模式 + fake-ip），`socket.connect(("203.0.113.30", 764))`
   返回 **0.02s 连通** —— 中国到美国不可能是 20ms。那是连到了**本地虚拟网卡**，
   真正的连接还没建立。→ 必须真的发一个 HTTP 请求、**拿到出口 IP** 才算数。

🔴 坑 2：**状态码不能作为判据，要看响应正文**
   代理的域名 ACL 拒绝时返回的是 `403`，正文才是判据：
       errorMsg: sso.openxlab.org.cn:80 not accessible
   只看 403 会以为"目标站返回了 403"，实际是**代理自己拒绝转发**。

🔴 坑 3：**必须用真实目标域名试，不能拿通用站点代跑**
   一个代理能通 google / baidu / github，**不代表**能通 `sso.openxlab.org.cn`。
   实测某代理商精确屏蔽了 `openxlab.org.cn` 和 `intern-ai.org.cn`（还有 `qq.com`），
   而 taobao / 163 / aliyun 全部正常 —— 这是**域名黑名单**，不是"中国站点被拦"。

本工具按上面三条设计，输出三档判定：`可用` / `被代理拦截` / `不通`。

用法
----
    # 单条
    python tools/probe_proxy.py 203.0.113.30:764:USERNAME:PASSWORD
    # 多条（每行一条，支持 host:port:user:pass 或完整 URL）
    python tools/probe_proxy.py --file proxies.txt
    # 只看目标域名可达性、跳过地理查询（更快）
    python tools/probe_proxy.py --no-geo <proxy>
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 探测"出口 IP"用的站点（必须能回显请求方 IP）
ECHO_URL = "https://api.ipify.org?format=json"

# 本项目真正需要的目标。**必须用它们试**，不能用通用站点代替。
# 每条给 https 与 http 两个变体：443 被 ACL 拦时，代理是**直接掐断 TLS**
# （只拿到 `UNEXPECTED_EOF`，没有正文）；而 80 端口会返回那句明确的
# `errorMsg: <host>:80 not accessible`。所以 443 失败时要用 80 回退取原因。
TARGETS = [
    ("sso", "https://sso.openxlab.org.cn/", "http://sso.openxlab.org.cn/"),
    ("discovery", "https://discovery-api.intern-ai.org.cn/v1/models",
     "http://discovery-api.intern-ai.org.cn/v1/models"),
]

# 对照组：用来区分"代理整体不通"和"只拦目标域名"
CONTROLS = [
    ("google", "https://www.google.com/"),
    ("baidu", "https://www.baidu.com/"),
]

ACL_MARK = "not accessible"      # 代理 ACL 拒绝时的正文特征（见模块 docstring 坑 2）


def parse_proxy(raw: str) -> str:
    """`host:port:user:pass` → `http://user:pass@host:port`；已是 URL 则原样返回。"""
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    raise ValueError(f"无法识别的代理格式：{raw!r}")


def _inner(ex: Exception) -> str:
    """挖到最内层异常 —— `SSLError` 的顶层消息全是样板，没信息量。"""
    while getattr(ex, "__cause__", None) or getattr(ex, "__context__", None):
        ex = ex.__cause__ or ex.__context__
    return f"{type(ex).__name__}: {ex}"


def _get(url: str, px: str, timeout: float):
    """发一个 GET。返回 `(status, body, headers, err)`；`err` 非空表示没拿到响应。"""
    import requests

    try:
        r = requests.get(url, proxies={"http": px, "https": px}, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=False)
        return r.status_code, r.text, dict(r.headers), ""
    except Exception as ex:                                   # noqa: BLE001
        return None, "", {}, _inner(ex)


def geo(ip: str, timeout: float = 15) -> dict:
    """查出口 IP 的归属与类型（**直连查询**，不经代理）。"""
    import requests

    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}"
            "?fields=status,country,regionName,city,isp,org,as,proxy,hosting,mobile",
            timeout=timeout)
        d = r.json()
        return d if d.get("status") == "success" else {}
    except Exception:                                         # noqa: BLE001
        return {}


def probe(raw: str, *, do_geo: bool = True, timeout: float = 20.0) -> dict:
    """完整探测一条代理，返回结果字典。"""
    px = parse_proxy(raw)
    res = {"proxy": raw, "url": px, "verdict": "", "egress_ip": "", "geo": {},
           "targets": {}, "controls": {}, "note": ""}

    # ── 第 1 步：出口 IP（唯一能证明"代理真的在工作"的判据）────────
    st, body, _hdr, err = _get(ECHO_URL, px, timeout)
    if err:
        res["verdict"] = "不通"
        res["note"] = f"出口 IP 探测失败 → {err}"
        return res
    if st != 200:
        res["verdict"] = "不通"
        res["note"] = f"出口 IP 探测返回 HTTP {st}"
        return res
    try:
        res["egress_ip"] = json.loads(body).get("ip", "")
    except ValueError:
        res["verdict"] = "不通"
        res["note"] = f"出口 IP 响应无法解析：{body[:80]!r}"
        return res

    # ── 第 2 步：出口 IP 的类型（住宅 vs 机房）────────────────────
    if do_geo and res["egress_ip"]:
        res["geo"] = geo(res["egress_ip"])
        if res["geo"].get("hosting") is True:
            res["note"] = "⚠ 出口是**机房** IP（信誉差，封禁风险高）"
        elif res["geo"].get("proxy") is True:
            res["note"] = "⚠ 出口被标记为 proxy（信誉差）"
        elif res["geo"].get("isp"):
            res["note"] = f"住宅/家宽倾向（{res['geo'].get('isp','')}）"

    # ── 第 3 步：对照组（区分"整体不通"与"只拦目标"）────────────
    for name, url in CONTROLS:
        st, body, _h, err = _get(url, px, timeout)
        res["controls"][name] = "ACL拦截" if ACL_MARK in body else (
            err and f"ERR {err[:40]}" or f"HTTP {st}")

    # ── 第 4 步：真实目标（唯一有决定性的判据）────────────────────
    for name, https_url, http_url in TARGETS:
        st, body, _h, err = _get(https_url, px, timeout)
        if ACL_MARK in body:
            res["targets"][name] = "ACL拦截"
            continue
        if not err and st in (200, 401, 302, 301, 307):
            # 401 也算通：那是"没带凭据"，说明**请求真的到了目标站**
            res["targets"][name] = f"HTTP {st} ✓"
            continue
        # 443 没通 → 用 80 回退，问代理"到底为什么"。这一步决定了判定是
        # "被代理拦截"（可操作：找服务商加白名单）还是"不通"（网络问题）。
        _st2, body2, _h2, err2 = _get(http_url, px, timeout)
        if ACL_MARK in body2:
            res["targets"][name] = "ACL拦截"
        elif err:
            res["targets"][name] = f"ERR {err[:48]}"
        else:
            res["targets"][name] = f"HTTP {st}"

    # ── 判定 ─────────────────────────────────────────────────────
    tvals = list(res["targets"].values())
    if tvals and all(v.endswith("✓") for v in tvals):
        res["verdict"] = "可用"
    elif any(v == "ACL拦截" for v in tvals):
        res["verdict"] = "被代理拦截"
        if not res["note"]:
            res["note"] = "代理域名 ACL 拒绝转发目标域名（换出口 IP 也没用）"
    else:
        res["verdict"] = "不通"
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="探测代理能否用于本项目")
    ap.add_argument("proxies", nargs="*", help="host:port:user:pass 或完整 URL")
    ap.add_argument("--file", help="从文件读（每行一条，# 开头为注释）")
    ap.add_argument("--no-geo", action="store_true", help="跳过出口 IP 归属查询")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    raws = list(args.proxies)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raws.append(line)
    if not raws:
        ap.error("没给代理。用位置参数或 --file。")

    print(f"探测 {len(raws)} 条代理（并发 {args.workers}）")
    print("判据：出口 IP 必须拿到 → 再看真实目标域名（不是通用站点）\n")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(
            lambda r: probe(r, do_geo=not args.no_geo, timeout=args.timeout), raws))
    dt = time.time() - t0

    for r in results:
        g = r["geo"]
        loc = (f"{g.get('country','?')}/{g.get('regionName','?')}/{g.get('city','?')} "
               f"{g.get('isp','?')}") if g else "（未查）"
        print("=" * 78)
        print(f"代理   : {r['proxy']}")
        print(f"判定   : {r['verdict']}")
        print(f"出口 IP: {r['egress_ip'] or '—'}   {loc}")
        if r["note"]:
            print(f"备注   : {r['note']}")
        if r["controls"]:
            print(f"对照组 : " + "  ".join(f"{k}={v}" for k, v in r["controls"].items()))
        print(f"目标   : " + "  ".join(f"{k}={v}" for k, v in r["targets"].items()))
    print("=" * 78)

    ok = [r for r in results if r["verdict"] == "可用"]
    blocked = [r for r in results if r["verdict"] == "被代理拦截"]
    dead = [r for r in results if r["verdict"] == "不通"]
    print(f"\n汇总：可用 {len(ok)} / 被代理拦截 {len(blocked)} / 不通 {len(dead)}"
          f"   （{dt:.1f}s）")
    if blocked:
        print("→ 被拦截的代理**换出口 IP 也没用**：黑名单在服务商侧，"
              "要找服务商把目标域名加白名单，或换一家。")
    if ok:
        print("→ 可用的可以直接设：")
        for r in ok:
            print(f"     IR_PROXY={r['proxy']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n结果已落盘 {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
