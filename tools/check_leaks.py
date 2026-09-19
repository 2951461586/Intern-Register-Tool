#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""提交前泄漏闸门 —— 命中即非 0 退出，用来在**入库之前**拦住凭据与风控标识。

为什么需要它
------------
2026-09-18 / 09-19 本项目连续踩了两次：
  1. 一个"改配置前顺手做的备份" `.env.bak-20260918-110124` 没被 .gitignore 挡住；
  2. 代理账密、4 个出口 IP、本机直连出口 IP 被写进 README / 源码 / 工具注释，
     并随提交推到公开仓库。
两次都不是"不懂"，而是**没有任何自动检查**。这个脚本就是那个检查。

设计原则（每条都是踩出来的）
----------------------------
* **fail-closed**：默认扫**全部文本文件**，只跳过真正的二进制与构建产物。
  用"后缀白名单"决定扫什么 = fail-open —— 没人预料到的后缀会被静默跳过。
* **两层判据**：
    - 第 1 层「具体模式」扫全部文本文件（真实 IP、`host:port:user:pass`、
      token 前缀、`*.workers.dev` 实例子域、绝对路径）；
    - 第 2 层「高熵串」只扫数据类文件（`.json/.jsonl/.log/.txt/.csv`），
      因为扫 `.md` / `.py` 会把文档示例全报出来 —— **一吵就被 `--no-verify` 绕过**。
* **项目自带模式**：自动读 `.env`，把你**真实在用的**值（Worker 子域、出口 IP、
  代理串、token）也当成禁用串。这样"别把 .env 里的东西写回仓库"是自动生效的，
  不需要另外维护一份清单。
* **闸门必须能失败**：`tools/selftest_check_leaks.py` 做变异验证 ——
  塞假凭据进去必须被拦并点名文件；干净树必须通过。
  "拿旧代码跑一遍通过"只证明它不过度拦截，**完全没有证明它会拦**。

用法
----
    python tools/check_leaks.py             # 已跟踪 + 未跟踪未被忽略的文件（默认）
    python tools/check_leaks.py --staged    # 只查暂存区（pre-commit 钩子用）
    python tools/check_leaks.py --all       # 工作区全部文件（含被忽略的，最严）
    python tools/check_leaks.py --history   # 连**全部 git 历史**一起扫（发布前用）
    python tools/check_leaks.py --quiet     # 只输出结论

退出码：0 = 干净；1 = 有命中；2 = 环境/用法错误。
"""

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# 🔴 一层 parents 就到仓库根（本文件在 tools/ 下）。
#    多套一层会让 ROOT 指向仓库的**父目录** → 所有 isdir 判断失败被静默跳过
#    → 扫描范围变空 → "全 0 命中"的假绿。这是经典 off-by-one，必须钉死。
ROOT = Path(__file__).resolve().parents[1]


def set_root(p) -> None:
    """覆盖扫描根 —— **仅供 `selftest_check_leaks.py` 用**。

    自检要在临时仓库里造变异样本（塞假凭据进去），必须能把闸门指过去，
    否则就得往真实仓库里扔垃圾文件。
    """
    global ROOT
    ROOT = Path(p).resolve()


# ═══════════════════════════════════════════════════════════════════
# 一、文件名校验（与 .gitignore 是**同一套规则的第二个实现**）
# ═══════════════════════════════════════════════════════════════════
# 🔴 两层必须同时拦。只改一层会漂移成"一个放行一个拦截"，
#    而真正决定出货与否的是 .gitignore 那层。
#
# 🔴 用**家族正则**，不枚举后缀：`.bak-<描述>` / `.old` / `.save.1` 是不收敛的。
BLOCKED_NAME_RE = re.compile(r"""
      ^\.env(\.|$)                  # .env / .env.<任何>
    | \.bak($|[.\-])                # .bak / .bak-x / .bak.1
    | \.old$ | \.orig$ | \.rej$
    | \.save($|[.\-]) | \.tmp($|[.\-]) | \.copy($|[.\-]) | \.backup($|[.\-])
    | \.swp$ | \.swo$ | ~$
    | \.pem$ | \.key$ | \.p12$ | \.pfx$ | \.jks$ | \.keystore$
    | ^proxies\.txt | ^slots\.txt | _proxies\.txt$
    | \.log($|\.)
    | ^mihomo.*\.ya?ml$ | ^config\.yaml
""", re.X)

# 例外：这些名字长得像禁用家族，但是**必须入库**的模板。
ALLOWED_NAMES = {".env.example"}


# ═══════════════════════════════════════════════════════════════════
# 二、内容模式
# ═══════════════════════════════════════════════════════════════════
# 放行的网段（RFC 5737 文档段 / RFC 1918 私网 / 回环 / 链路本地 / 保留）
ALLOW_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
        "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    )
]
# 单独放行的**公共**地址：出现在文档里但没有任何标识性
#  1.1.1.1 / 8.8.8.8  公共 DNS
#  1.2.3.4 / 5.6.7.8  惯例占位
#  152.0.0.0          Chrome UA 里的版本串（Chrome/152.0.0.0）
#  131.0.0.0          README 里讲 CIDR 时的示例
ALLOW_IP_EXACT = {"1.1.1.1", "2.2.2.2", "8.8.8.8", "1.2.3.4", "5.6.7.8",
                  "152.0.0.0", "131.0.0.0"}

# 凭据里的占位词 —— `host:port:USERNAME:PASSWORD` 这种文档写法不该报
ALLOW_CRED_PARTS = {
    "username", "password", "user", "pass", "user1", "pass1", "xxxx",
    "your-user", "your-pass", "userid", "secret", "token", "placeholder",
}

IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
# host:port:user:pass —— 端口必须是数字，否则文档里的 `host:port:user:pass` 会误报
CRED_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9.\-]{3,}):(\d{2,5}):"
    r"([A-Za-z0-9._\-]{4,}):([A-Za-z0-9._\-]{4,})\b")
TOKEN_RE = re.compile(
    r"\b(cfat_[A-Za-z0-9]{16,}"          # Cloudflare API Token
    r"|ghp_[A-Za-z0-9]{20,}"             # GitHub 经典 PAT
    r"|ghu_[A-Za-z0-9]{20,}"             # GitHub 细粒度令牌
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"              # OpenAI 风格
    r"|AKIA[0-9A-Z]{16}"                 # AWS
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"    # Slack
    r")\b")
# 实例子域（`<...>` 占位不算）
WORKER_HOST_RE = re.compile(r"\b([a-z0-9][a-z0-9\-]{1,62})\.workers\.dev\b")
# 本机绝对路径：Windows 盘符 / macOS / Linux home
ABS_PATH_RE = re.compile(r"(?:\b[A-Za-z]:[\\/](?:Users|IDE|epsoft|tools|dev|code)\b"
                         r"|/Users/[A-Za-z0-9._-]+/"
                         r"|/home/[A-Za-z0-9._-]+/)")
# 高熵串（只用于数据类文件）
HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_\-]{28,}")
# 数据类文件 —— 第 2 层只扫这些
DATA_SUFFIXES = {".json", ".jsonl", ".log", ".txt", ".csv", ".env", ".yaml", ".yml"}

# 二进制后缀：直接跳过（内容层）
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".otf",
    ".pyc", ".pyo", ".class", ".jar", ".mp4", ".mp3", ".webp",
}


def _entropy(s: str) -> float:
    """Shannon 熵（bits/char）。随机 base64 一般 > 4.0，正常单词 < 3.5。"""
    if not s:
        return 0.0
    from collections import Counter
    from math import log2
    n = len(s)
    return -sum((c / n) * log2(c / n) for c in Counter(s).values())


def _ip_allowed(ip: str) -> bool:
    if ip in ALLOW_IP_EXACT:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True                      # 不是合法 IP（如 999.1.1.1）→ 不算
    return any(a in net for net in ALLOW_NETS)


# ═══════════════════════════════════════════════════════════════════
# 三、项目自带模式：把 .env 里**真实在用的值**也当成禁用串
# ═══════════════════════════════════════════════════════════════════
# 这些键的值**不是秘密**（路径 / 数字 / 开关 / 公开标识）。
# 🔴 拿它们当禁用串会把正常文档写法误报成泄漏 —— 误报一多，
#    闸门第一天就会被 `--no-verify` 绕过，等于没有。
BENIGN_KEY_RE = re.compile(
    r"(_FILE|_PATH|_STATE|_COOLDOWN|_TIMEOUT|_MAX|_WINDOW|_INTERVAL|_LIMIT"
    r"|_DELAY|_BUDGET|_PREFLIGHT|_MICRO|_NO_|_LO$|_HI$"
    r"|SOURCE|SLOTS$|CHROME_PATH|CLIENT_ID)")


def _looks_like_path(s: str) -> bool:
    """相对/绝对路径、纯数字 —— 都不是秘密。"""
    if s.startswith(("./", "../", "/", "~")):
        return True
    if re.fullmatch(r"[\d.]+", s):
        return True
    # 含 `/` 但不是 URL、也不含 userinfo ⇒ 是路径
    return "/" in s and "://" not in s and "@" not in s


def load_env_patterns() -> list:
    """从 `.env` 提取**真正的秘密值**，返回 `[(标签, 值)]`。

    🔴 只返回**标签**，值只在本进程内用于匹配 —— 任何输出里都不出现值。
    这是"别把 .env 里的东西写回仓库"的自动实现：不用另外维护一份清单。
    """
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return []
    out = []
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if not key or len(val) < 8 or BENIGN_KEY_RE.search(key):
            continue
        # `IR_SLOT_EGRESS_IPS=7901=1.2.3.4,7902=5.6.7.8` → 拆成单个值
        for piece in re.split(r"[,\s]+", val):
            piece = piece.strip()
            if "=" in piece and key.endswith("EGRESS_IPS"):
                piece = piece.rsplit("=", 1)[-1].strip()
            if len(piece) >= 8 and not _looks_like_path(piece):
                out.append((key, piece))
        # 代理串里的 host 也单独提取（`scheme://user:pass@host:port`）
        if key.startswith("IR_PROXY") and "://" in val:
            m = re.search(r"://(?:[^@/]*@)?([^:/@]+)", val)
            if m and len(m.group(1)) >= 8:
                out.append((key + " (host)", m.group(1)))
    return out


# ═══════════════════════════════════════════════════════════════════
# 四、收集待查文件
# ═══════════════════════════════════════════════════════════════════
def _git(*args, text=True):
    p = subprocess.run(["git", *args], cwd=str(ROOT),
                       capture_output=True, text=text)
    return p.returncode, (p.stdout or "")


def collect(mode: str) -> list:
    """返回仓库根相对路径列表。"""
    if mode == "staged":
        _rc, out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        return sorted({p for p in out.splitlines() if p.strip()})
    if mode == "all":
        skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
        got = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in skip for part in rel.parts):
                continue
            got.append(rel.as_posix())
        return sorted(got)
    # 默认：已跟踪 + 未跟踪但未被忽略
    _rc, tracked = _git("ls-files")
    _rc2, untracked = _git("ls-files", "--others", "--exclude-standard")
    return sorted({p for p in (tracked + untracked).splitlines() if p.strip()})


# ═══════════════════════════════════════════════════════════════════
# 五、检查
# ═══════════════════════════════════════════════════════════════════
def check_names(rels: list) -> list:
    """第 0 层：文件名本身是否属于禁用家族（.gitignore 的第二层）。"""
    hits = []
    for rel in rels:
        base = Path(rel).name
        if base in ALLOWED_NAMES:
            continue
        if BLOCKED_NAME_RE.search(base):
            hits.append((rel, "文件名命中禁用家族（凭据/快照/日志/证书）", ""))
    return hits


def check_ignore_canaries() -> list:
    """自审 `.gitignore`：正查该拒的、反查不该误伤的。

    🔴 护栏自己没坏、坏的是它的前提假设 —— 所以必须成对审：
    只查"该拒的有没有拒"会漏掉"模板被误伤"（误伤是静默的）。
    """
    hits = []
    must_ignore = [
        ".env", ".env.local", ".env.bak-20260918-110124", ".env.old",
        "results.json.bak-x", "proxies.txt", "slots.txt.bak",
        ".workbuddy-ai/proxypool/slots.txt", ".workbuddy-ai/tmp/batch.log",
        "node.key", "client.pem", "mihomo-slots.yaml", "config.yaml.bak",
    ]
    p = subprocess.run(["git", "check-ignore", "--stdin"], cwd=str(ROOT),
                       # 🔴 必须传 **bytes**，不能用 `input=<str>` + `text=True`：
                       #    Windows 上 text 模式会给 stdin 套 TextIOWrapper(newline=None)，
                       #    把 `\n` 翻成 `\r\n` ⇒ git 收到的路径变成 `.env\r` ⇒ 一条都不匹配
                       #    ⇒ 守卫静默变成"全部放行"。实测踩到过（13/13 假报）。
                       input="\n".join(must_ignore).encode("utf-8"),
                       capture_output=True)
    ignored = set(p.stdout.decode("utf-8", "replace").split())
    for f in must_ignore:
        if f not in ignored:
            hits.append(("<.gitignore>", f".gitignore 放行了本该拒绝的文件：{f}", ""))
    # 反查：模板必须**不**被忽略
    p2 = subprocess.run(["git", "check-ignore", "-q", ".env.example"],
                        cwd=str(ROOT), capture_output=True)
    if p2.returncode == 0:
        hits.append(("<.gitignore>", ".env.example 被误伤（例外行丢了？）", ""))
    return hits


def check_content(rel: str, text: str, env_patterns: list) -> list:
    """第 1 层（全文本）+ 第 2 层（仅数据类文件）。"""
    hits = []
    suffix = Path(rel).suffix.lower()

    # ── 第 1 层 ────────────────────────────────────────────────────
    for m in IPV4_RE.finditer(text):
        ip = m.group(1)
        if not _ip_allowed(ip):
            hits.append((rel, f"真实 IPv4（非文档/私网段）：{ip}", _line_of(text, m.start())))

    for m in CRED_RE.finditer(text):
        host, port, user, pwd = m.groups()
        if user.lower() in ALLOW_CRED_PARTS or pwd.lower() in ALLOW_CRED_PARTS:
            continue
        if host.lower() in ALLOW_CRED_PARTS:
            continue
        hits.append((rel, f"疑似 `host:port:user:pass` 凭据（host={host} port={port}）",
                     _line_of(text, m.start())))

    for m in TOKEN_RE.finditer(text):
        # 只报前缀类别，不回显值
        hits.append((rel, f"疑似令牌（前缀 {m.group(1)[:5]}…）", _line_of(text, m.start())))

    for m in WORKER_HOST_RE.finditer(text):
        hits.append((rel, f"实例 workers.dev 子域：{m.group(1)[:4]}…（应写 <your-subdomain>）",
                     _line_of(text, m.start())))

    for m in ABS_PATH_RE.finditer(text):
        hits.append((rel, "本机绝对路径（暴露目录结构 / 项目代号）",
                     _line_of(text, m.start())))

    # ── 项目自带模式（来自 .env 的真实值）──────────────────────────
    for label, val in env_patterns:
        if val in text:
            hits.append((rel, f"命中 .env 中的真实值（{label}）", _line_of(text, text.index(val))))

    # ── 第 2 层：高熵串，只扫数据类文件 ────────────────────────────
    if suffix in DATA_SUFFIXES:
        for m in HIGH_ENTROPY_RE.finditer(text):
            s = m.group(0)
            if _entropy(s) > 4.2:
                hits.append((rel, f"高熵串（长度 {len(s)}，疑似密钥）",
                             _line_of(text, m.start())))
    return hits


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_file(rel: str, env_patterns: list) -> list:
    p = ROOT / rel
    if not p.is_file():
        return []
    if p.suffix.lower() in BINARY_SUFFIXES:
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return []                       # 二进制 / 读不了 → 跳过（内容层）
    return check_content(rel, text, env_patterns)


# ═══════════════════════════════════════════════════════════════════
# 六、历史扫描
# ═══════════════════════════════════════════════════════════════════
def scan_history() -> list:
    """逐 blob 扫**全部历史**。

    比 `git log -S` 硬：pickaxe 只看"出现次数是否变化"，
    某个串被移动但次数不变就抓不到；逐 blob 读全文才真正穷尽。
    """
    hits = []
    _rc, objs = _git("rev-list", "--objects", "--all")
    seen = set()
    for line in objs.splitlines():
        oid = line.split(" ", 1)[0]
        if not oid or oid in seen:
            continue
        seen.add(oid)
    for oid in sorted(seen):
        rc, t = _git("cat-file", "-t", oid)
        if rc != 0 or t.strip() != "blob":
            continue
        rc, content = _git("cat-file", "-p", oid)
        if rc != 0 or not content:
            continue
        # 历史里只跑第 1 层的"具体模式"，且不回显内容
        for m in IPV4_RE.finditer(content):
            if not _ip_allowed(m.group(1)):
                hits.append((f"<history:{oid[:10]}>", f"历史 blob 含真实 IPv4：{m.group(1)}", 0))
        for m in CRED_RE.finditer(content):
            host, _port, user, pwd = m.groups()
            if user.lower() in ALLOW_CRED_PARTS or pwd.lower() in ALLOW_CRED_PARTS:
                continue
            if host.lower() in ALLOW_CRED_PARTS:
                continue
            hits.append((f"<history:{oid[:10]}>", f"历史 blob 含疑似凭据（host={host}）", 0))
        for m in TOKEN_RE.finditer(content):
            hits.append((f"<history:{oid[:10]}>", f"历史 blob 含疑似令牌（{m.group(1)[:5]}…）", 0))
    return hits


# ═══════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="提交前泄漏闸门：拦住凭据与风控标识入库")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="只查暂存区（pre-commit 用）")
    g.add_argument("--all", action="store_true", help="查工作区全部文件（含被忽略的）")
    ap.add_argument("--history", action="store_true", help="连全部 git 历史一起扫")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    ap.add_argument("--root", default="",
                    help="覆盖扫描根（仅供自检用；默认=本脚本所在仓库根）")
    args = ap.parse_args()

    if args.root:
        set_root(args.root)

    if not (ROOT / ".git").exists():
        print(f"✗ 找不到仓库根（{ROOT} 下没有 .git）—— ROOT 算错了？", file=sys.stderr)
        return 2

    mode = "staged" if args.staged else ("all" if args.all else "default")
    rels = collect(mode)
    env_patterns = load_env_patterns()

    if not args.quiet:
        print(f"🔍 泄漏闸门：模式={mode}，待查 {len(rels)} 个文件"
              f"，项目自带模式 {len(env_patterns)} 条"
              f"（来自 .env，值不显示）")

    hits = []
    hits += check_names(rels)
    hits += check_ignore_canaries()
    for rel in rels:
        hits += scan_file(rel, env_patterns)
    if args.history:
        if not args.quiet:
            print("🕓 扫全部 git 历史（逐 blob）…")
        hits += scan_history()

    if not hits:
        if not args.quiet:
            print("✅ 未发现泄漏。")
        return 0

    print(f"\n🔴 发现 {len(hits)} 处问题：\n")
    for rel, why, line in hits:
        loc = f"{rel}:{line}" if line else rel
        print(f"  {loc}\n      {why}")
    print("\n修法：值改从 .env 读（默认空），文档里的值换成占位符"
          "（见 docs/security-conventions.md）。")
    print("⚠ 不要用 --no-verify 绕过 —— 那正是上一次泄漏发生的方式。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
