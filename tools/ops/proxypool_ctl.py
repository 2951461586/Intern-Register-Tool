#!/usr/bin/env python
"""槽位代理实例的启停与体检（**只碰我们自己的实例，绝不动 Clash Verge**）。

为什么必须有这个工具
--------------------
这台机器上同时跑着 **3 个 `verge-mihomo.exe`**：

    26952  0.0.0.0:1053 + 127.0.0.1:7897      ← Clash Verge 主内核（用户上网用）
    11156  127.0.0.1:7901-7906, 17901/17902   ← 本项目的槽位实例
    35588  127.0.0.1:7911-7913, 17911/17912   ← 本项目的对照实验实例

**它们的可执行文件路径完全一样**（同一个 `mihomo` 二进制），进程名也一样。
所以"按进程名 kill"是必错的做法 —— 杀到主内核就直接断网。

唯一可靠的区分方式是**看它监听哪些端口**：

    监听 7897 或 1053        → Clash Verge 主内核，**永远不碰**
    监听 79xx 槽位端口段      → 我们的实例

本工具把这个判断固化下来，并在动手前**再校验一次**（`stop` 会拒绝杀任何
持有 7897/1053 的进程，哪怕命令行看起来像是我们的）。

用法：
    python tools/ops/proxypool_ctl.py status      # 体检：谁是谁、端口在哪
    python tools/ops/proxypool_ctl.py stop        # 只停我们的实例
    python tools/ops/proxypool_ctl.py start       # 起槽位实例（用 config.yaml）
    python tools/ops/proxypool_ctl.py start --config .workbuddy-ai/proxypool/test_proxy_ref.yaml
    python tools/ops/proxypool_ctl.py restart

退出码：0 = 成功；1 = 失败（`status` 恒为 0）。
"""

import argparse
import ctypes
import ctypes.wintypes as w
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

DEFAULT_DIR = ROOT / ".workbuddy-ai" / "proxypool"
DEFAULT_CONFIG = DEFAULT_DIR / "config.yaml"

# 🔴 这两个端口是 Clash Verge 主内核的命门。任何进程只要持有其中之一，
#    本工具就**拒绝终止**它 —— 这是最后一道保险，防止判断逻辑被改坏。
CLASH_VERGE_PORTS = {7897, 1053}
# Clash Verge 自己的进程（不是 mihomo 内核，但同样不能碰）
NEVER_TOUCH_NAMES = {"clash-verge.exe", "clash-verge-service.exe", "clash-verge-rev.exe"}

# 🔴 mihomo 内核路径：**不写死**（2026-09-19 安全重构前是一个硬编码的绝对路径）。
#    两个理由：
#      1. 绝对路径会暴露本机目录结构 / 项目代号 —— 属于基础设施标识，
#         与出口 IP 同级，不进仓库（见 docs/security-conventions.md）。
#      2. 每个人装的地方都不一样，写死等于把"能跑"绑死在一台机器上。
#    从环境变量读；没配时在 `cmd_start()` 里报错并给出修法。
#    （顺手修掉原常量的拼写：`MIHOMO_EXE` 少了一个 I。）
MIHOMO_EXE = Path(os.getenv("IR_MIHOMO_EXE", "").strip())

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x2

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = w.HANDLE
_k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, ctypes.c_wchar_p, ctypes.POINTER(w.DWORD)]
_k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
_k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def list_processes() -> list[tuple[int, str]]:
    """`[(pid, exe_name)]`。用 toolhelp 快照 —— 本环境 `wmic` / `tasklist`
    都拿不到输出（前者不可用，后者输出不回流），ctypes 是唯一稳的路子。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    e = _PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    out = []
    ok = _k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.szExeFile))
        ok = _k32.Process32NextW(snap, ctypes.byref(e))
    _k32.CloseHandle(snap)
    return out


def image_path(pid: int) -> str:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # err=5 = Access Denied（提权跑的进程，比如 Clash Verge 的内核）
        return f"<拿不到，err={ctypes.get_last_error()}>"
    buf = ctypes.create_unicode_buffer(32768)
    n = w.DWORD(len(buf))
    ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    _k32.CloseHandle(h)
    return buf.value if ok else f"<查询失败 err={ctypes.get_last_error()}>"


_NETSTAT_TTL = 1.5
_netstat_cache: dict = {"t": 0.0, "data": {}}


def all_listening_ports(force: bool = False) -> dict[int, set[int]]:
    """`{pid: {端口}}`。**一次 netstat 全量解析**。

    🔴 别写成"每个进程各调一次 netstat"：一次 netstat 在这台机器上要 ~0.5s，
    而我们有 3~4 个候选进程 ⇒ 白等 1.5s 以上。加个 1.5s TTL 缓存，
    只在 `stop` 动手**前后**的复查上用 `force=True` 拿新鲜数据。
    """
    now = time.monotonic()
    if not force and now - _netstat_cache["t"] < _NETSTAT_TTL:
        return _netstat_cache["data"]
    out: dict[int, set[int]] = {}
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           timeout=30, errors="replace")
    except Exception:                                              # noqa: BLE001
        return _netstat_cache["data"]
    for line in r.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        m = re.search(r":(\d+)$", parts[1])
        if not m:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        out.setdefault(pid, set()).add(int(m.group(1)))
    _netstat_cache.update(t=now, data=out)
    return out


def listening_ports(pid: int, force: bool = False) -> set[int]:
    """这个 PID 正在 LISTEN 的本地端口。**判断身份的唯一可靠依据。**"""
    return all_listening_ports(force=force).get(pid, set())


def classify(pid: int, exe: str, ports: set[int]) -> tuple[str, str]:
    """返回 `(类别, 说明)`。类别 ∈ {clash_verge, ours, other}。"""
    if exe.lower() in NEVER_TOUCH_NAMES:
        return "clash_verge", "Clash Verge 本体（不是内核）"
    if ports & CLASH_VERGE_PORTS:
        hit = sorted(ports & CLASH_VERGE_PORTS)
        return "clash_verge", f"持有主内核端口 {hit} —— **用户上网靠它**"
    if exe.lower().startswith("verge-mihomo"):
        if ports:
            return "ours", f"槽位/实验实例，监听 {sorted(ports)}"
        return "ours", "mihomo 实例（当前没有 LISTEN 端口，可能刚起来）"
    return "other", ""


def cmd_status() -> int:
    print("=" * 74)
    print("代理相关进程体检")
    print("=" * 74)
    ports_map = all_listening_ports(force=True)
    procs = [(pid, exe) for pid, exe in list_processes()
             if "mihomo" in exe.lower() or "clash" in exe.lower()]
    if not procs:
        print("（没有 mihomo / clash 进程）")
    ours = []
    for pid, exe in procs:
        ports = ports_map.get(pid, set())
        kind, note = classify(pid, exe, ports)
        icon = {"clash_verge": "🔴", "ours": "🔀", "other": "⚪"}[kind]
        if kind == "ours":
            ours.append(pid)
        print(f"\n{icon} PID {pid}  {exe}")
        print(f"   exe   : {image_path(pid)}")
        print(f"   端口  : {sorted(ports) if ports else '(无 LISTEN)'}")
        if note:
            print(f"   判定  : {note}")

    print("\n" + "=" * 74)
    print(f"我们的实例: {ours if ours else '（无）'}")
    print(f"Clash Verge 主内核端口 {sorted(CLASH_VERGE_PORTS)} 会被本工具显式保护")
    return 0


def cmd_stop(force_all: bool = False) -> int:
    procs = [(pid, exe) for pid, exe in list_processes()
             if "mihomo" in exe.lower()]
    ports_map = all_listening_ports(force=True)
    victims, protected = [], []
    for pid, exe in procs:
        ports = ports_map.get(pid, set())
        kind, note = classify(pid, exe, ports)
        if kind == "clash_verge":
            protected.append((pid, exe, note))
            continue
        victims.append((pid, exe, sorted(ports)))

    if protected:
        print("🛡  保护（不会碰）:")
        for pid, exe, note in protected:
            print(f"     PID {pid}  {exe}  {note}")
    if not victims:
        print("没有可停的槽位实例。")
        return 0

    print("\n🔀 准备停止:")
    for pid, _exe, ports in victims:
        print(f"     PID {pid}  端口 {ports}")

    stopped, failed = [], []
    for pid, _exe, ports in victims:
        # 🔴 动手前**再校验一次**，而且要 `force=True` 拿新鲜端口 ——
        #    判断逻辑将来被改坏时，这一道能兜住。
        if listening_ports(pid, force=True) & CLASH_VERGE_PORTS:
            print(f"     ⛔ PID {pid} 突然持有主内核端口 —— 放弃，绝不冒险")
            failed.append(pid)
            continue
        h = _k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if not h:
            print(f"     ✗ PID {pid} 打不开（err={ctypes.get_last_error()}）")
            failed.append(pid)
            continue
        ok = _k32.TerminateProcess(h, 1)
        rc = _k32.WaitForSingleObject(h, 5000)
        _k32.CloseHandle(h)
        if ok and rc == 0:
            print(f"     ✓ PID {pid} 已停止（端口 {ports} 应已释放）")
            stopped.append(pid)
        else:
            print(f"     ✗ PID {pid} 停止失败 ok={bool(ok)} rc={rc}")
            failed.append(pid)

    time.sleep(0.6)
    # 复查：端口真的释放了吗？主内核真的还在吗？—— 两个方向都要有硬凭据
    left = all_listening_ports(force=True)
    leaked = {p: sorted(left.get(p, set())) for p in stopped if left.get(p)}
    holders = sorted(p for p, ps in left.items() if ps & CLASH_VERGE_PORTS)
    print(f"\n已停 {len(stopped)} 个，失败 {len(failed)} 个")
    print(f"   端口复查：{'✓ 全部释放' if not leaked else '❌ 仍占用 ' + str(leaked)}")
    print(f"🛡  主内核复查：{'✓ 仍持有 ' + str(sorted(CLASH_VERGE_PORTS)) + f'（PID {holders}）' if holders else '❌ 主内核不见了！'}")
    return 0 if not failed else 1


def cmd_start(config: Path, workdir: Path, wait: float = 6.0) -> int:
    if not str(MIHOMO_EXE) or not MIHOMO_EXE.is_file():
        # 未配置与配错要分开报 —— 否则人会去翻"文件是不是被删了"。
        if not str(MIHOMO_EXE):
            print("✗ 未配置 mihomo 内核路径（IR_MIHOMO_EXE）。\n"
                  "  这个值**故意不写死在源码里**（绝对路径会暴露本机目录结构，\n"
                  "  而且每个人装的位置不同）。请在 .env 里加一行：\n"
                  '    IR_MIHOMO_EXE=<你的 mihomo 可执行文件绝对路径>\n'
                  "  Clash Verge 用户通常在它的安装目录下，文件名形如 "
                  "`verge-mihomo.exe`。", file=sys.stderr)
        else:
            print(f"✗ 找不到内核：{MIHOMO_EXE}\n"
                  f"  路径来自 IR_MIHOMO_EXE，检查一下是不是写错了。", file=sys.stderr)
        return 1
    if not config.is_file():
        print(f"✗ 找不到配置：{config}\n"
              f"  先生成：python tools/ops/gen_mihomo_slots.py --sub <订阅名> --slots N",
              file=sys.stderr)
        return 1

    # 已经有实例在跑就先提示（同一个 mixed-port 会撞车）
    for pid, exe in list_processes():
        if "mihomo" in exe.lower():
            ports = listening_ports(pid)
            if ports and not (ports & CLASH_VERGE_PORTS):
                print(f"⚠ PID {pid} 已经在跑（监听 {sorted(ports)}）。"
                      f"同端口会启动失败 —— 先 stop，或用 restart。")

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    cmd = [str(MIHOMO_EXE), "-d", str(workdir), "-f", str(config)]
    print(f"启动: {' '.join(cmd)}")
    # 🔴 必须 detached —— 否则进程会随这个 shell 一起被回收（踩过）。
    p = subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"已拉起，PID {p.pid}，等 {wait:.0f}s 让它把 listener 起来 ...")
    time.sleep(wait)

    ports = listening_ports(p.pid, force=True)
    if not ports:
        print(f"✗ PID {p.pid} 没有监听任何端口 —— 起来就退出了？"
              f"\n  手动跑一次看报错：{MIHOMO_EXE} -d {workdir} -f {config}", file=sys.stderr)
        return 1
    print(f"✓ PID {p.pid} 监听 {sorted(ports)}")
    print("  下一步：python tools/probes/probe_slots.py")
    # 🔴 这条必须说清楚，否则下次会以为"起好了"，结果下一分钟端口就没了。
    print("\n⚠ 常驻性提醒：本工具用 `DETACHED_PROCESS` 拉起进程，在**你自己的终端**里\n"
          "   跑是能常驻的；但如果是在受管沙箱里执行（进程挂在 Job Object 下），\n"
          "   进程会**随这次调用结束被回收** —— 表现是「同一次调用里 status 看得到，\n"
          "   下一次调用就没了」。那种环境要用宿主提供的后台执行能力起，\n"
          "   或者在自己的终端窗口里跑上面那条命令。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="槽位代理实例的启停与体检")
    ap.add_argument("action", choices=["status", "stop", "start", "restart"])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--workdir", default=str(DEFAULT_DIR))
    ap.add_argument("--wait", type=float, default=6.0, help="start 后等多久再查端口")
    args = ap.parse_args()

    if args.action == "status":
        return cmd_status()
    if args.action == "stop":
        return cmd_stop()
    if args.action == "start":
        return cmd_start(Path(args.config), Path(args.workdir), args.wait)
    # restart
    rc = cmd_stop()
    if rc != 0:
        print("stop 有失败项，仍尝试 start ...")
    return cmd_start(Path(args.config), Path(args.workdir), args.wait)


if __name__ == "__main__":
    sys.exit(main())
