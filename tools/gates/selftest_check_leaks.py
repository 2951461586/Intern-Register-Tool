#!/usr/bin/env python
r"""验证 `tools/gates/check_leaks.py` **真的会拦**，而不是只会放行。

为什么必须有这个文件
--------------------
"拿现在的干净仓库跑一遍，闸门返回 0" —— 这只证明了它**不过度拦截**（假阳性方向），
**完全没有证明它会拦**。一个 `return 0` 的死脚本也能让这条通过。

所以这里做**变异验证**（mutation testing）：
  1. 造一个含**已知坏样本**的临时仓库 → 闸门必须**非 0 退出，且报错里点名那个文件**；
  2. 造一个干净仓库 → 闸门必须通过（否则"一律拦截"也能让第 1 条变绿）；
  3. 在进程内把某个检测器**故意改坏**，断言对应的检查**不再命中** ——
     证明上面的断言真的在考察那个检测器，而不是空转。

第 3 条是关键：只做 1+2 的话，如果检测器被删了、而断言写成了别的形式，
测试会一直绿。**变异体存活 = 闸门和测试一起是假绿的。**

用法：
    python tools/gates/selftest_check_leaks.py

退出码：0 = 全部通过；1 = 有断言失败。
"""

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _path import ROOT  # noqa: E402  （副作用：把 tools/ 与仓库根加进 sys.path）

REPO = ROOT
GATE = Path(__file__).resolve().parent / "check_leaks.py"   # 与本文件同目录

PASS, FAIL = "✓", "✗"
_fails = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f"  —— {detail}" if detail and not ok else ""))
    if not ok:
        _fails.append(name)


# ── 变异样本（都是**假的**，但形状与真实泄漏一致）──────────────────
#
# 🔴 必须**运行时拼接**，不能写成完整字面量。
#    原因：本文件自己也在闸门的扫描范围内。写成字面量的话，
#    "自检文件里躺着一条假凭据"会被闸门自己抓到 —— 这是实测踩到的
#    （第 5 项直接红）。
#    修法**不是**给本文件加扫描白名单（那等于开一个藏东西的口子），
#    而是让磁盘上的文本里**不存在**任何完整可匹配的串。
#    ⚠ 别"顺手简化"回字面量 —— 那样闸门立刻自咬。
#
# 另外：203.0.114.x 故意选在 203.0.113.0/24 之外 —— 那个文档段是放行的，
# 用它会测不出"真实 IP 检测"到底有没有生效。
BAD_IP = "203.0.114" + "." + "5"
BAD_CRED = "proxy-node-7.example" + ".net" + ":" + "8080" + ":" + "alice9x2k" + ":" + "zzz8p2k1"
BAD_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BAD_WORKER = "mytool-abc123xyz" + "." + "workers" + "." + "dev"
BAD_PATH = "F" + ":" + "/IDE/" + "secret-tool/bin/tool.exe"

BAD_TEXT = "\n".join([
    f"出口 {BAD_IP}",
    f"代理 {BAD_CRED}",
    f"token {BAD_TOKEN}",
    f"worker https://{BAD_WORKER}/api",
    f"内核 {BAD_PATH}",
]) + "\n"

CLEAN_TEXT = "\n".join([
    "出口 203.0.113.11",              # RFC 5737 文档段 → 放行
    "代理 203.0.113.30:764:USERNAME:PASSWORD",   # 占位词 → 放行
    "worker https://<worker>.<your-subdomain>.workers.dev",
    "内核 <你的 mihomo 可执行文件>",
]) + "\n"


def load_gate():
    """把闸门当模块加载（不执行 main）。"""
    spec = importlib.util.spec_from_file_location("check_leaks_mod", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_repo(tmp: Path, files: dict) -> None:
    """建一个临时 git 仓库，`.gitignore` 从真实仓库复制过来。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    shutil.copy2(REPO / ".gitignore", tmp / ".gitignore")
    for name, content in files.items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def run_gate(root: Path, *extra):
    p = subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    print("=" * 72)
    print("check_leaks 自检 —— 验证闸门真的会拦（变异测试）")
    print("=" * 72)

    # ── [1] 单元级：检测器对已知坏样本必须命中 ─────────────────────
    print("\n[1] 单元级：检测器对已知坏样本必须命中")
    cl = load_gate()

    name_hits = [h for h in cl.check_names([".env.canary-123", "proxies.txt",
                                            "node.key", ".env.example"])]
    hit_names = {h[0] for h in name_hits}
    check("文件名校验：.env.canary-123 被拦", ".env.canary-123" in hit_names)
    check("文件名校验：proxies.txt 被拦", "proxies.txt" in hit_names)
    check("文件名校验：node.key 被拦", "node.key" in hit_names)
    check("文件名校验：.env.example **不**被拦（例外行）",
          ".env.example" not in hit_names)

    content_hits = cl.check_content("notes.txt", BAD_TEXT, [])
    whys = " | ".join(w for _r, w, _l in content_hits)
    check("内容校验：真实 IPv4 被拦", "IPv4" in whys)
    check("内容校验：host:port:user:pass 被拦", "host:port:user:pass" in whys)
    check("内容校验：令牌前缀被拦", "令牌" in whys)
    check("内容校验：实例子域被拦", "workers.dev" in whys)
    check("内容校验：绝对路径被拦", "绝对路径" in whys)

    clean_hits = cl.check_content("notes.txt", CLEAN_TEXT, [])
    check("内容校验：占位符 / 文档段**不**误报", not clean_hits,
          f"误报 {len(clean_hits)} 条：{[w for _r, w, _l in clean_hits]}")

    # ── [2] 变异验证：把检测器改坏，断言必须变红 ───────────────────
    # 这一步证明 [1] 的断言真的在考察那个检测器，而不是空转。
    print("\n[2] 变异验证：故意改坏检测器，对应断言必须失效")
    cl2 = load_gate()
    cl2.IPV4_RE = re.compile(r"(?!x)x")          # 永不匹配
    mut_hits = cl2.check_content("notes.txt", BAD_TEXT, [])
    mut_whys = " | ".join(w for _r, w, _l in mut_hits)
    check("改坏 IPV4_RE 后，IPv4 命中消失（证明该断言有效）",
          "IPv4" not in mut_whys)

    cl3 = load_gate()
    cl3.BLOCKED_NAME_RE = re.compile(r"(?!x)x")
    check("改坏 BLOCKED_NAME_RE 后，文件名命中消失（证明该断言有效）",
          not cl3.check_names([".env.canary-123"]))

    # ── [3] 端到端：坏样本仓库必须被拦且点名 ───────────────────────
    print("\n[3] 端到端（变异）：含坏样本的仓库必须非 0 退出并点名")
    tmp = Path(tempfile.mkdtemp(prefix="irt-gate-bad-"))
    try:
        make_repo(tmp, {
            "notes.txt": BAD_TEXT,                    # 内容向量
            ".env.canary-9f3a": "IR_X=whatever\n",    # 文件名向量
        })
        rc, out = run_gate(tmp, "--all")
        check("坏样本仓库：退出码非 0", rc != 0, f"实际 {rc}")
        check("坏样本仓库：输出点名了 notes.txt", "notes.txt" in out)
        check("坏样本仓库：输出点名了 .env.canary-9f3a", ".env.canary-9f3a" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── [4] 端到端：干净仓库必须通过 ───────────────────────────────
    print("\n[4] 端到端（对照）：干净仓库必须通过")
    tmp = Path(tempfile.mkdtemp(prefix="irt-gate-ok-"))
    try:
        make_repo(tmp, {"README.md": CLEAN_TEXT, ".env.example": "IR_X=\n"})
        rc, out = run_gate(tmp, "--all")
        check("干净仓库：退出码为 0", rc == 0, f"实际 {rc}\n{out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── [5] 真实仓库必须干净 ───────────────────────────────────────
    print("\n[5] 真实仓库当前状态")
    rc, out = run_gate(REPO)
    check("真实仓库：默认模式通过", rc == 0, out.strip()[-400:])

    print("\n" + "=" * 72)
    if _fails:
        print(f"{FAIL} {len(_fails)} 项失败：")
        for f in _fails:
            print(f"    - {f}")
        return 1
    print(f"{PASS} 全部通过 —— 闸门既不过度拦截，也确实会拦。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
