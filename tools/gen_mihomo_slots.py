r"""从订阅生成一个**独立的 mihomo 配置**，带 N 个"一槽一端口"的出口。
要解决什么问题
--------------
本项目的封禁是 **IP 维度累计配额**（`B0000`）。而旧实现里整个批次共用
**一个出口 IP**（`IR_PROXY` 只有一条）—— 也就是说，**不管并发调到几，
出口 IP 都只有一个，撞配额是必然的**。

`workers` 调多大都没用：真正的约束是"有多少个不同出口 IP"。

设计参考
--------
`asz798838958/aBaiFreeGPT` 的 `deploy/mihomo/augment_slots.py`。它的做法是
给 mihomo 注入 N 个策略组 + N 个 **listener**，每个 listener 有**自己的端口**：

    proxy-groups:
      - name: REGISTER-SLOT-01
        type: select
        use: [<订阅 provider>]
    listeners:
      - name: REGISTER-IN-01
        type: mixed
        port: 7901                # ← port_base + slot
        proxy: REGISTER-SLOT-01   # ← 这个端口固定走这个组

于是运行时可以**给每个槽位单独选节点**（通过控制器 `PUT /proxies/REGISTER-SLOT-01`），
N 个 worker 各拿一个端口 → **N 个不同的出口 IP**。

🔴 为什么不复用本机的 Clash Verge
--------------------------------
实测本机 Clash Verge Rev 的运行时配置里：

    external-controller: ''          ← TCP 控制器被**显式关掉**
    external-controller-pipe: \\.\pipe\verge-mihomo   ← 只有命名管道

所以没法用 HTTP 驱动它。而且它的 `config.yaml` 是**每次切换配置就重新生成**的，
手工加 `listeners:` 会被覆盖。另外它开着 TUN（`auto-route: true`），
改坏了会影响整机网络。

→ 正确做法是**另起一个独立实例**（本脚本生成的配置就是给它用的），
  不动用户的 Clash Verge。这也正是 aBaiFreeGPT 的做法（它自带 `deploy/mihomo/`）。

本脚本与参考实现的差异
--------------------
1. **节点内联**，不用 `proxy-providers`：订阅拉一次、节点名写进配置，
   每个槽位组直接 `proxies: [<一个节点>]`。好处是**配置自包含**，
   不依赖 mihomo 自己去拉订阅（少一个失败点，也少一次订阅泄露面）。
2. **不开 TUN**：我们只要一个 HTTP 代理端口给 `requests` 用，
   不需要接管整机流量。
3. 顺带把 `IR_PROXY_SLOTS` 的值打出来，直接粘进 `.env`。

用法：
    # 先看订阅里有哪些节点（不发任何注册请求）
    python tools/gen_mihomo_slots.py --sub Basic-912138 --list-nodes

    # 生成 8 个槽位（每个槽位一个不同节点）
    python tools/gen_mihomo_slots.py --sub Basic-912138 --slots 8

    # 只挑美国节点
    python tools/gen_mihomo_slots.py --sub glados --slots 12 --filter "(?i)美国|United States|US"

生成的配置用这条命令启动（**不是** Clash Verge）：
    "F:/IDE/Clash Verge/verge-mihomo.exe" -d .workbuddy-ai/proxypool \
        -f .workbuddy-ai/proxypool/config.yaml
"""

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / ".workbuddy-ai" / "proxypool"
SUBS_JSON = ROOT / ".workbuddy-ai" / "tmp" / "subs.json"
# 拉订阅时优先直连；失败再借本机 Clash。订阅服务器常常本身就在墙外，
# 所以"直连失败"是常态而不是异常。
LOCAL_CLASH = "http://127.0.0.1:7897"

# 槽位名/端口。刻意用与参考实现一致的命名习惯，方便对照排查。
GROUP_PREFIX = "SLOT-"
LISTENER_PREFIX = "SLOT-IN-"


# ────────────────────────────────────────────────────────────────
# 订阅获取
# ────────────────────────────────────────────────────────────────
def resolve_source(spec: str) -> str:
    """`--sub` 可以是订阅名 / URL / 本地文件路径，统一成"可 fetch 的东西"。"""
    p = Path(spec)
    if p.is_file():
        return f"file://{p.resolve()}"
    if "://" in spec:
        return spec
    if SUBS_JSON.is_file():
        subs = json.loads(SUBS_JSON.read_text(encoding="utf-8"))
        if spec in subs:
            return subs[spec]
        # 允许模糊匹配（订阅名里有中文，手打容易差一个字）
        hit = [k for k in subs if spec.lower() in k.lower()]
        if len(hit) == 1:
            print(f"（订阅名按模糊匹配到「{hit[0]}」）")
            return subs[hit[0]]
        if hit:
            raise SystemExit(f"✗「{spec}」匹配到多个订阅：{hit}，请写全")
    raise SystemExit(f"✗ 无法解析 --sub {spec!r}（既不是文件、URL，也不在 "
                     f"{SUBS_JSON.relative_to(ROOT)} 里）")


def fetch_subscription(src: str, *, timeout: int = 30) -> str:
    """拉订阅正文。**不打印 URL**（订阅 URL 本身就是凭据）。"""
    if src.startswith("file://"):
        return Path(src[7:]).read_text(encoding="utf-8")

    last = None
    for label, px in (("直连", None), ("经本机 Clash", {"http": LOCAL_CLASH,
                                                     "https": LOCAL_CLASH})):
        try:
            r = requests.get(src, timeout=timeout, proxies=px,
                             headers={"User-Agent": "Clash.Meta"})
            r.raise_for_status()
            print(f"✓ 订阅已获取（{label}），{len(r.text)} 字节")
            return r.text
        except Exception as ex:                                   # noqa: BLE001
            last = f"{label}: {type(ex).__name__}: {ex}"[:160]
            print(f"  ⚠ {label} 失败：{last}")
    raise SystemExit(f"✗ 订阅拉取失败（两条路都不通）。最后错误：{last}")


def parse_nodes(text: str) -> list[dict]:
    """从订阅正文里取 `proxies:` 列表。

    🔴 订阅有两种常见形态，都要认：
      1. 标准 Clash YAML（有 `proxies:` 键）—— 三毛机场 / glados / 104G 都是这种
      2. **Base64 编码的 URI 列表**（`vless://` / `hysteria2://` …）—— 见
         `decode_uri_subscription()`。只认第一种会在某些订阅上"解析出 0 个节点"，
         而报错信息看起来像订阅坏了。
    """
    try:
        d = yaml.safe_load(text)
        if isinstance(d, dict) and isinstance(d.get("proxies"), list):
            return [x for x in d["proxies"] if isinstance(x, dict) and x.get("name")]
    except yaml.YAMLError:
        pass

    # 形态 2：整段 Base64 → URI 列表
    import base64

    for candidate in (text.strip(), text.strip().replace("\n", "")):
        try:
            dec = base64.b64decode(candidate + "=" * (-len(candidate) % 4))
            body = dec.decode("utf-8", "replace")
            if "://" in body:
                print("（订阅是 Base64 URI 形态，按 URI 解析）")
                return parse_uri_list(body)
        except Exception:                                         # noqa: BLE001
            continue
    return []


# ── Base64 URI 订阅 → mihomo 节点 ───────────────────────────────
# 🔴 为什么值得写：机场给的订阅**不一定**是 Clash 格式。实测本机 8 个订阅里
#    有 1 个（Basic-912138）给的是 base64(vless://.../hysteria2://.../tuic://...)，
#    而另外 3 个是 Clash YAML。只支持一种就会"换一个订阅就解析出 0 个节点"。
_URI_SUPPORT = ("vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic")


def _q(parsed) -> dict:
    """query string → 单值 dict（同名多值取最后一个）。"""
    out = {}
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        out[k] = v
    return out


def _frag(parsed, fallback: str) -> str:
    name = urllib.parse.unquote(parsed.fragment or "").strip()
    return name or fallback


def parse_uri_list(body: str) -> list[dict]:
    """把 `scheme://...` 行转成 mihomo 的 proxy dict。认不出的行跳过并计数。"""
    nodes: list[dict] = []
    skipped: dict[str, int] = {}
    for raw in body.split("\n"):
        line = raw.strip()
        if "://" not in line:
            continue
        scheme = line.split("://", 1)[0].lower()
        if scheme not in _URI_SUPPORT:
            skipped[scheme] = skipped.get(scheme, 0) + 1
            continue
        try:
            node = _uri_to_proxy(scheme, line)
        except Exception:                                         # noqa: BLE001
            node = None
        if node:
            nodes.append(node)
        else:
            skipped[scheme] = skipped.get(scheme, 0) + 1
    if skipped:
        print(f"  ⚠ 跳过了 {sum(skipped.values())} 行无法转换的节点：{skipped}")
    return nodes


def _uri_to_proxy(scheme: str, line: str) -> dict | None:
    if scheme == "vmess":
        # vmess 是 base64(JSON)，不是标准 URL
        import base64

        payload = line.split("://", 1)[1]
        d = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4))
                       .decode("utf-8", "replace"))
        node = {
            "name": str(d.get("ps") or d.get("add") or "vmess"),
            "type": "vmess",
            "server": str(d.get("add") or ""),
            "port": int(d.get("port") or 0),
            "uuid": str(d.get("id") or ""),
            "alterId": int(d.get("aid") or 0),
            "cipher": str(d.get("scy") or "auto"),
        }
        if not node["server"] or not node["port"]:
            return None
        tls = str(d.get("tls") or "").lower()
        if tls in ("tls", "true", "1"):
            node["tls"] = True
            if d.get("sni") or d.get("host"):
                node["servername"] = d.get("sni") or d.get("host")
        if d.get("net") in ("ws", "grpc", "h2", "http"):
            node["network"] = d["net"]
            opts = {}
            if d.get("path"):
                opts["path"] = d["path"]
            if d.get("host"):
                opts["headers"] = {"Host": d["host"]}
            if opts:
                node[f"{d['net']}-opts"] = opts
        return node

    parsed = urllib.parse.urlparse(line)
    q = _q(parsed)
    host = parsed.hostname or ""
    port = parsed.port or 0
    if not host or not port:
        return None
    name = _frag(parsed, f"{scheme}-{host}:{port}")

    if scheme == "vless":
        node = {"name": name, "type": "vless", "server": host, "port": port,
                "uuid": urllib.parse.unquote(parsed.username or ""),
                "udp": True}
        if q.get("flow"):
            node["flow"] = q["flow"]
        net = q.get("type") or "tcp"
        node["network"] = net
        if net == "ws":
            opts = {"path": q.get("path", "/")}
            if q.get("host"):
                opts["headers"] = {"Host": q["host"]}
            node["ws-opts"] = opts
        elif net == "grpc":
            node["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
        elif net == "tcp" and q.get("headerType") == "http":
            node["network"] = "http"
            node["http-opts"] = {"path": [q.get("path", "/")]}
        if q.get("security") in ("tls", "reality"):
            node["tls"] = True
            if q.get("sni"):
                node["servername"] = q["sni"]
            if q.get("fp"):
                node["client-fingerprint"] = q["fp"]
            if q.get("security") == "reality":
                node["reality-opts"] = {"public-key": q.get("pbk", ""),
                                        "short-id": q.get("sid", "")}
        return node

    if scheme == "trojan":
        node = {"name": name, "type": "trojan", "server": host, "port": port,
                "password": urllib.parse.unquote(parsed.username or ""),
                "udp": True, "skip-cert-verify": q.get("allowInsecure") == "1"}
        if q.get("sni"):
            node["sni"] = q["sni"]
        if q.get("type") == "ws":
            node["network"] = "ws"
            node["ws-opts"] = {"path": q.get("path", "/")}
        return node

    if scheme in ("hysteria2", "hy2"):
        node = {"name": name, "type": "hysteria2", "server": host, "port": port,
                "password": urllib.parse.unquote(parsed.username or ""),
                "skip-cert-verify": q.get("insecure") in ("1", "true")}
        if q.get("sni"):
            node["sni"] = q["sni"]
        if q.get("obfs"):
            node["obfs"] = q["obfs"]
            node["obfs-password"] = q.get("obfs-password", "")
        return node

    if scheme == "tuic":
        node = {"name": name, "type": "tuic", "server": host, "port": port,
                "uuid": urllib.parse.unquote(parsed.username or ""),
                "password": urllib.parse.unquote(parsed.password or ""),
                "skip-cert-verify": q.get("allow_insecure") == "1"}
        if q.get("sni"):
            node["sni"] = q["sni"]
        if q.get("congestion_control"):
            node["congestion-controller"] = q["congestion_control"]
        return node

    if scheme == "ss":
        # ss:// 有两种：base64(method:pass)@host:port  和 明文 method:pass@host:port
        import base64

        userinfo = urllib.parse.unquote(parsed.username or "")
        pwd = urllib.parse.unquote(parsed.password or "")
        if not pwd and userinfo:
            try:
                dec = base64.b64decode(userinfo + "=" * (-len(userinfo) % 4))
                userinfo = dec.decode("utf-8", "replace")
            except Exception:                                     # noqa: BLE001
                pass
            if ":" in userinfo:
                userinfo, pwd = userinfo.split(":", 1)
        return {"name": name, "type": "ss", "server": host, "port": port,
                "cipher": userinfo, "password": pwd, "udp": True}
    return None


# 🔴 机场订阅里会夹**信息型假节点**（"剩余流量：91.35 GB"、"套餐到期：..."），
#    它们的 server 常常是 `127.0.0.1`。不过滤掉的话，槽位会分到这些死节点上，
#    表现为"槽位有了但完全不通"，而且看起来像"节点被墙了"。
_INFO_NODE_NAME = re.compile(
    r"剩余流量|距离下次重置|套餐到期|官网|建议每天|更新订阅|expire|traffic", re.I)
_INFO_NODE_SERVER = {"127.0.0.1", "localhost", "0.0.0.0", ""}


def drop_info_nodes(nodes: list[dict]) -> tuple[list[dict], int]:
    """剔除信息型假节点。返回 `(干净节点, 剔除数)`。"""
    clean = []
    for n in nodes:
        server = str(n.get("server") or "").strip().lower()
        if server in _INFO_NODE_SERVER or _INFO_NODE_NAME.search(n.get("name", "")):
            continue
        clean.append(n)
    return clean, len(nodes) - len(clean)



# ────────────────────────────────────────────────────────────────
# 配置生成
# ────────────────────────────────────────────────────────────────
def build_config(nodes: list[dict], *, slots: int, port_base: int,
                 mixed_port: int, controller_port: int,
                 group_prefix: str = GROUP_PREFIX) -> tuple[dict, list[str]]:
    """生成独立 mihomo 配置。返回 `(config, 被选中的节点名列表)`。

    每个槽位组只挂**一个**节点 —— 这样"槽位 ↔ 出口 IP"是静态确定的，
    不依赖控制器就能跑。需要轮换时再用控制器改这个组的选中项。
    """
    picked = [n["name"] for n in nodes[:slots]]
    groups = []
    listeners = []
    for i, node_name in enumerate(picked, start=1):
        gname = f"{group_prefix}{i:02d}"
        groups.append({
            "name": gname,
            "type": "select",
            # 单元素列表 = 静态绑定到这个节点
            "proxies": [node_name],
        })
        listeners.append({
            "name": f"{LISTENER_PREFIX}{i:02d}",
            "type": "mixed",
            "port": port_base + i,
            "listen": "127.0.0.1",
            "proxy": gname,
        })

    cfg = {
        # 这个 mixed-port 基本不用（我们用 listeners 的槽位端口），
        # 但留着方便手工 curl 调试。
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        # 🔴 自己这个实例的控制器**要开**（与 Clash Verge 相反）——
        #    槽位轮换就靠它。
        "external-controller": f"127.0.0.1:{controller_port}",
        "secret": "",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "profile": {"store-selected": True, "store-fake-ip": False},
        "proxies": nodes,
        "proxy-groups": groups,
        "listeners": listeners,
        # 不开 TUN：只要一个 HTTP 代理，不要接管整机流量。
        "rules": ["MATCH,DIRECT"],
    }
    return cfg, picked


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从订阅生成带 N 个槽位出口的独立 mihomo 配置")
    ap.add_argument("--sub", required=True,
                    help="订阅名（读 .workbuddy-ai/tmp/subs.json）/ URL / 本地文件")
    ap.add_argument("--slots", type=int, default=8, help="槽位数量（默认 8）")
    ap.add_argument("--filter", default="", help="节点名正则过滤，如 '(?i)美国|US'")
    ap.add_argument("--exclude", default="", help="节点名正则排除")
    ap.add_argument("--port-base", type=int, default=7900,
                    help="槽位端口基数（槽位 i 用 port_base+i）")
    ap.add_argument("--mixed-port", type=int, default=17901)
    ap.add_argument("--controller-port", type=int, default=17902)
    ap.add_argument("--out", default=str(DEFAULT_DIR / "config.yaml"))
    ap.add_argument("--list-nodes", action="store_true", help="只列节点，不生成")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args()

    src = resolve_source(args.sub)
    text = fetch_subscription(src)
    nodes = parse_nodes(text)
    print(f"订阅内节点总数：{len(nodes)}")
    if not nodes:
        print("✗ 解析出 0 个节点。可能是订阅格式不认识，或订阅已过期。")
        return 1

    nodes, dropped = drop_info_nodes(nodes)
    if dropped:
        print(f"  剔除信息型假节点（server=127.0.0.1 / 名字是流量·到期提示）："
              f"{dropped} 个 → 剩 {len(nodes)}")

    if args.filter:
        rx = re.compile(args.filter)
        nodes = [n for n in nodes if rx.search(n["name"])]
        print(f"按 --filter {args.filter!r} 过滤后：{len(nodes)} 个")
    if args.exclude:
        rx = re.compile(args.exclude)
        before = len(nodes)
        nodes = [n for n in nodes if not rx.search(n["name"])]
        print(f"按 --exclude {args.exclude!r} 排除：{before - len(nodes)} 个")

    if args.list_nodes:
        for n in nodes:
            print(f"  {n['name']:44s} {n.get('type', '?'):10s} "
                  f"{n.get('server', '')}")
        return 0

    if not nodes:
        print("✗ 过滤后没有节点了")
        return 1
    if len(nodes) < args.slots:
        print(f"⚠ 只有 {len(nodes)} 个节点，少于要的 {args.slots} 个槽位 —— "
              f"实际只生成 {len(nodes)} 个槽位。\n"
              f"  （槽位多于节点没有意义：同一个出口 IP 用两个槽位，"
              f"对 IP 维度配额毫无帮助，只是自己骗自己。）")

    cfg, picked = build_config(
        nodes, slots=min(args.slots, len(nodes)), port_base=args.port_base,
        mixed_port=args.mixed_port, controller_port=args.controller_port)
    n_slots = len(picked)

    print(f"\n生成 {n_slots} 个槽位（每个绑一个不同节点）：")
    for i, name in enumerate(picked, start=1):
        print(f"  {GROUP_PREFIX}{i:02d}  127.0.0.1:{args.port_base + i}  ← {name}")

    # 🔴 必须检查"节点是否真的互不相同"。同名的两个槽位等于一个出口，
    #    而表面上"有 N 个槽位"，最容易骗过自己。
    if len(set(picked)) != len(picked):
        dup = [n for n in set(picked) if picked.count(n) > 1]
        print(f"\n✗ 有重复节点（{dup}）—— 槽位必须绑不同节点，否则出口 IP 会重复")
        return 2

    slots_value = ",".join(f"http://127.0.0.1:{args.port_base + i}"
                           for i in range(1, n_slots + 1))
    print(f"\n{'=' * 72}")
    print(f"把这个写进 .env（{n_slots} 个槽位，共 {len(slots_value)} 字符）：")
    print(f"\nIR_PROXY_SLOTS={slots_value}\n")
    print(f"启动独立实例（**不是** Clash Verge）：")
    print(f'  "F:/IDE/Clash Verge/verge-mihomo.exe" -d {DEFAULT_DIR.relative_to(ROOT)}'
          f' -f {Path(args.out).relative_to(ROOT)}')
    print(f"验证槽位出口 IP：python tools/probe_slots.py")
    print("=" * 72)

    if args.dry_run:
        print("\n（--dry-run：未写盘）")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 🔴 配置里含**节点凭据**，必须落在 gitignored 的 .workbuddy-ai/ 下。
    #    这里做一次显式断言，防止有人把 --out 指到仓库里。
    if ".workbuddy-ai" not in out.resolve().as_posix():
        print(f"✗ 拒绝写盘：{out} 不在 .workbuddy-ai/ 下 —— "
              f"该文件含节点凭据，会被提交进 git。", file=sys.stderr)
        return 3
    out.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    print(f"\n配置已写入 {out}")
    # 顺手把槽位值存一份，方便脚本读取（避免 50 个 URL 塞进环境变量）
    (out.parent / "slots.txt").write_text(slots_value, encoding="utf-8")
    print(f"槽位清单已写入 {out.parent / 'slots.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
