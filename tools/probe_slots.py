#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""探测槽位池：**有多少个槽位 ≠ 有多少个出口 IP**，以及每个出口能不能到目标站。

为什么必须单独有这个工具
------------------------
`src/proxypool.py` 的池子是按**槽位**（= 本地端口 = mihomo 里一条 listener）
记账的，但目标站点的封禁是按**出口 IP**记账的。两者不是一对一：

    2026-09-18 实测（6 个槽位 / 6 个不同节点名）
        SLOT-01 (7901) -> 203.0.113.11
        SLOT-02 (7902) -> 203.0.113.12
        SLOT-03 (7903) -> 203.0.113.13
        SLOT-04 (7904) -> 203.0.113.14
        SLOT-05 (7905) -> 203.0.113.13   ← 与 03 重复
        SLOT-06 (7906) -> 203.0.113.11   ← 与 01 重复
    → 6 个槽位只有 **4 个**不同出口 IP

所以"我配了 6 个槽位，所以能并发 6 个账号"是错的 —— 真实上限是
**不同出口 IP 的个数**。这个工具就是用来把这个数字量出来的。

它还回答第二个问题：**这些出口能不能到 `sso.openxlab.org.cn`**。
判据沿用 `tools/probe_proxy.py` 的三档（见那边的模块 docstring）：
    可用 / 被代理拦截（代理 ACL 拒了目标域名）/ 不通

用法：
    python tools/probe_slots.py                       # 读 .env 的 IR_PROXY_SLOTS*
    python tools/probe_slots.py --slots-file .workbuddy-ai/proxypool/slots.txt
    python tools/probe_slots.py --slots 127.0.0.1:7901,127.0.0.1:7902
    python tools/probe_slots.py --fast                # 只探出口 IP，跳过归属/对照/目标

退出码：0 = 至少一个槽位可用；1 = 一个可用的都没有。
"""

import argparse
import importlib.util
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# `probe_proxy.py` 不是包的一部分（tools/ 没有 __init__.py），按路径加载它，
# 复用那套已经踩过坑的判定逻辑 —— 重写一遍只会把坑重踩一遍。
_spec = importlib.util.spec_from_file_location(
    "_probe_proxy", str(Path(__file__).resolve().parent / "probe_proxy.py"))
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

from src import config  # noqa: E402

DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports" / "slot_probe.json"

# 只探出口 IP 的极简路径（--fast）。`probe_proxy.probe` 一次要打 6 个请求
# （出口 IP + geo + 2 对照 + 2 目标），槽位多时纯属浪费。
ECHO_URL = pp.ECHO_URL


def load_slots(args) -> list[str]:
    """槽位来源优先级：--slots-file > --slots > 环境（IR_PROXY_SLOTS_FILE > IR_PROXY_SLOTS）。"""
    if args.slots_file:
        p = Path(args.slots_file)
        if not p.is_file():
            raise SystemExit(f"槽位文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
        return [x.strip() for x in raw.replace("\n", ",").split(",")
                if x.strip() and not x.strip().startswith("#")]
    if args.slots:
        out: list[str] = []
        for chunk in args.slots:
            out += [x.strip() for x in chunk.split(",") if x.strip()]
        return out
    return config.proxy_slots()


def exit_ip_only(slot: str, timeout: float) -> dict:
    """`--fast` 路径：只拿出口 IP。"""
    px = pp.parse_proxy(slot)
    st, body, _h, err = pp._get(ECHO_URL, px, timeout)
    if err or st != 200:
        return {"slot": slot, "egress_ip": "", "verdict": "不通",
                "note": err or f"HTTP {st}", "targets": {}}
    try:
        ip = json.loads(body).get("ip", "")
    except ValueError:
        return {"slot": slot, "egress_ip": "", "verdict": "不通",
                "note": f"响应无法解析：{body[:60]!r}", "targets": {}}
    return {"slot": slot, "egress_ip": ip, "verdict": "可用", "note": "",
            "targets": {}}


def _down_detail(r: dict) -> str:
    """给"不通"的槽位拼一句**真正说明原因**的话。

    坑：`probe()` 的 `note` 字段会在第 2 步被 geo 结论覆盖
    （"⚠ 出口是机房 IP（信誉差，封禁风险高）"）。如果第 4 步目标站没通，
    `note` 往往还是那条 geo 提示 —— 直接打印它等于**答非所问**，
    会让人以为"是出口类型的问题"，实际上只是网络抖动或超时。
    所以：先从 targets / controls 里找失败明细，找不到才退回 note。
    """
    parts = []
    for k, v in (r.get("targets") or {}).items():
        if not str(v).endswith("✓"):
            parts.append(f"target:{k}={v}")
    if not parts:
        for k, v in (r.get("controls") or {}).items():
            if "ACL" in str(v) or str(v).startswith("ERR"):
                parts.append(f"control:{k}={v}")
    if parts:
        return "；".join(parts)
    note = r.get("note") or ""
    return note if note else "（无失败明细）"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="探测槽位池的真实出口 IP 个数与目标站可达性")
    ap.add_argument("--slots", action="append",
                    help="槽位代理串，逗号分隔；可重复传")
    ap.add_argument("--slots-file", help="从文件读（一行一个或逗号分隔，# 为注释）")
    ap.add_argument("--workers", type=int, default=6, help="并发探测数（默认 6）")
    ap.add_argument("--timeout", type=float, default=20.0, help="单请求超时（秒）")
    ap.add_argument("--fast", action="store_true",
                    help="只探出口 IP，跳过 geo / 对照组 / 目标站")
    ap.add_argument("--no-geo", action="store_true", help="跳过出口 IP 归属查询")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="报告落盘路径")
    ap.add_argument("--no-write", action="store_true", help="不落盘")
    args = ap.parse_args()

    slots = load_slots(args)
    if not slots:
        print("没有槽位可探。\n"
              "  配一个：IR_PROXY_SLOTS_FILE=.workbuddy-ai/proxypool/slots.txt\n"
              "  或直接传：--slots 127.0.0.1:7901,127.0.0.1:7902", flush=True)
        return 1

    print(f"探测 {len(slots)} 个槽位"
          f"（{'仅出口 IP' if args.fast else '出口 IP + 目标站'}，"
          f"并发 {args.workers}）...", flush=True)
    t0 = time.time()

    results: list = [None] * len(slots)
    lock = threading.Lock()
    done = [0]

    def one(i: int, slot: str):
        try:
            if args.fast:
                r = exit_ip_only(slot, args.timeout)
            else:
                r = pp.probe(slot, do_geo=not args.no_geo, timeout=args.timeout)
                r = {"slot": slot, "egress_ip": r.get("egress_ip", ""),
                     "verdict": r.get("verdict", ""), "note": r.get("note", ""),
                     "targets": r.get("targets", {}),
                     "controls": r.get("controls", {})}
        except Exception as ex:                                    # noqa: BLE001
            r = {"slot": slot, "egress_ip": "", "verdict": "不通",
                 "note": f"{type(ex).__name__}: {ex}"[:120], "targets": {}}
        results[i] = r
        with lock:
            done[0] += 1
            ip = r["egress_ip"] or "-"
            print(f"  [{done[0]}/{len(slots)}] {slot:<28} -> {ip:<18} "
                  f"{r['verdict']}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        list(ex.map(lambda p: one(*p), list(enumerate(slots))))

    wall = round(time.time() - t0, 1)

    # ── 出口 IP 去重：这才是真正的并发上限 ────────────────────────
    by_ip: dict[str, list[str]] = {}
    dead: list[str] = []
    for r in results:
        if r["egress_ip"]:
            by_ip.setdefault(r["egress_ip"], []).append(r["slot"])
        else:
            dead.append(r["slot"])

    print("\n" + "=" * 62)
    print(f"槽位总数      : {len(slots)}")
    print(f"**不同出口 IP**: {len(by_ip)}   ← 这才是能同时用的账号数上限")
    if dead:
        print(f"拿不到出口 IP : {len(dead)}  {', '.join(dead)}")
    for ip, ss in sorted(by_ip.items(), key=lambda kv: -len(kv[1])):
        tag = f"  ⚠ {len(ss)} 个槽位共用同一个出口" if len(ss) > 1 else ""
        print(f"  {ip:<18} {len(ss)} 个槽位: {', '.join(ss)}{tag}")

    if not args.fast:
        ok = [r for r in results if r["verdict"] == "可用"]
        blocked = [r for r in results if r["verdict"] == "被代理拦截"]
        down = [r for r in results if r["verdict"] == "不通"]
        print(f"\n目标站可达性（sso.openxlab.org.cn / discovery-api）")
        print(f"  可用 {len(ok)}   被代理拦截 {len(blocked)}   不通 {len(down)}")
        for r in blocked:
            print(f"  ⛔ {r['slot']} 被 ACL 拦了目标域名 —— "
                  f"换出口 IP 也没用，得找服务商加白名单")
        for r in down:
            # 别直接打 note —— note 可能存的是第 2 步的 geo 提示
            # （"出口是机房 IP"这类），跟"为什么不通"完全无关，
            # 会把人往错误方向带。优先报**真正的失败明细**。
            detail = _down_detail(r)
            print(f"  ✗ {r['slot']} 不通：{detail[:100]}")

        usable_ips = {r["egress_ip"] for r in ok if r["egress_ip"]}
        print(f"\n✅ 既拿得到出口 IP、又能到目标站的**不同出口**: {len(usable_ips)} 个")
        if len(usable_ips) < len(by_ip):
            print(f"   （有 {len(by_ip) - len(usable_ips)} 个出口能连外网但到不了目标站，"
                  f"别把它们算进并发）")

    # ── 给出可执行的结论 ──────────────────────────────────────────
    print("\n结论：")
    if len(by_ip) < len(slots):
        print(f"  ⚠ 配了 {len(slots)} 个槽位，但只有 {len(by_ip)} 个不同出口。"
              f"把 workers 提到 {len(slots)} 并不会更快 —— 多出来的槽位只是在"
              f"同一个出口上排队，反而更快撞穿那个 IP 的配额。")
    if len(by_ip) >= 1:
        print(f"  → 合理的并发上限：{len(by_ip)}（= 不同出口 IP 个数）")
    print(f"  → 想提高上限只有两条路：① 换订阅/加节点 ② 确认节点真的落在"
          f"不同机房（同机房的多台机器常常共用同一个 NAT 出口）")

    if not args.no_write:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wall_seconds": wall,
            "slots_total": len(slots),
            "distinct_egress_ips": len(by_ip),
            "by_egress_ip": by_ip,
            "no_egress": dead,
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已落盘：{out.relative_to(ROOT)}")

    print(f"耗时 {wall}s")
    usable = len(by_ip)
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
