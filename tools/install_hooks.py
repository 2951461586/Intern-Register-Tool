#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""安装提交闸门钩子（`pre-commit` + `pre-push`）。

为什么是**两个**钩子
--------------------
* `pre-commit` → `check_leaks.py --staged`：拦"这一次要提交的内容"。
  快，覆盖绝大多数情况。
* `pre-push` → `check_leaks.py --history`：连**全部历史**逐 blob 扫。
  慢一点，但它才是能拦住"历史里早就躺着一条凭据、现在要推出去"的那道门 ——
  2026-09-18/19 两次泄漏正是这样溜出去的（提交时没人查，push 时也没人查）。

🔴 钩子装在 `.git/hooks/` 里，**不随仓库分发**。所以：
  * 新 clone 下来必须自己跑一次这个脚本；
  * `.github/workflows/secret-scan.yml` 是同一套判据的**服务端**兜底 ——
    本地钩子能被 `--no-verify` 绕过，CI 不能。

用法：
    python tools/install_hooks.py            # 安装 / 覆盖
    python tools/install_hooks.py --uninstall
    python tools/install_hooks.py --check    # 只看装没装

退出码：0 = 成功；1 = 失败。
"""

import argparse
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / ".git" / "hooks"

MARK = "# ── leak-gate (由 tools/install_hooks.py 生成，勿手工改) ──"

# 🔴 解释器不能写死绝对路径之外还只写一条：钩子跑在 Git Bash 里，
#    PATH 与交互式 shell 未必一致。按优先级探测，最后兜底到
#    **安装时**用的这个解释器（绝对路径只落在 .git/hooks/ 里，不入库）。
PY_PROBE = """PY=""
for c in python python3 py; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  PY="__PYTHON__"
fi
if [ ! -x "$PY" ] && ! command -v "$PY" >/dev/null 2>&1; then
  echo "⚠ 找不到 python，跳过泄漏闸门（请手工跑 tools/check_leaks.py）" >&2
  exit 0
fi
"""

PRE_COMMIT = """#!/bin/sh
{mark}
# 拦"这一次要提交的内容"。快，覆盖绝大多数情况。
{probe}
"$PY" tools/check_leaks.py --staged || {
  echo "" >&2
  echo "✗ 提交被泄漏闸门拦下（见上方命中项）。" >&2
  echo "  修好后重新 git add 再提交。" >&2
  echo "  ⚠ 不要用 --no-verify 绕过 —— 那正是上一次泄漏发生的方式。" >&2
  exit 1
}
"""

PRE_PUSH = """#!/bin/sh
{mark}
# 连全部历史一起扫。慢，但它是"历史里躺着凭据、现在要推出去"的唯一拦截点。
# 紧急情况可临时跳过：IR_SKIP_LEAK_GATE=1 git push
if [ "$IR_SKIP_LEAK_GATE" = "1" ]; then
  echo "⚠ IR_SKIP_LEAK_GATE=1：已跳过 pre-push 泄漏闸门" >&2
  exit 0
fi
{probe}
"$PY" tools/check_leaks.py --history || {
  echo "" >&2
  echo "✗ 推送被泄漏闸门拦下：**历史**里有命中项（不只是这一次的改动）。" >&2
  echo "  修法见 docs/security-conventions.md；清历史用 git-history 流程。" >&2
  exit 1
}
"""

# 🔴 模板用 `str.replace()` 渲染，**不是** `str.format()` ——
#    所以 shell 的花括号必须写成单个 `{` / `}`。
#    写成 `{{` 会原样落到钩子里，运行时报 `line N: {{: command not found`
#    （实测踩到：闸门虽然仍能拦住，但每行都多一条噪音报错）。
HOOKS_MAP = {"pre-commit": PRE_COMMIT, "pre-push": PRE_PUSH}


def render(tpl: str) -> str:
    return (tpl.replace("{mark}", MARK)
               .replace("{probe}", PY_PROBE.strip())
               .replace("__PYTHON__", sys.executable.replace("\\", "/")))


def install() -> int:
    if not HOOKS.is_dir():
        print(f"✗ 找不到 {HOOKS} —— 这不是一个 git 仓库？", file=sys.stderr)
        return 1
    for name, tpl in HOOKS_MAP.items():
        p = HOOKS / name
        existed = p.exists()
        # 🔴 用 LF 写：钩子是 shell 脚本，CRLF 会让 `#!/bin/sh` 带上 \r 而报
        #    "bad interpreter"（在 Windows 上尤其容易踩）。
        p.write_text(render(tpl), encoding="utf-8", newline="\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  {'覆盖' if existed else '安装'} {name}")
    print("\n✓ 钩子已就位。注意它们**不随仓库分发** ——")
    print("  新 clone 后要自己再跑一次 `python tools/install_hooks.py`。")
    print("  服务端兜底见 .github/workflows/secret-scan.yml（CI 不能被 --no-verify 绕过）。")
    return 0


def uninstall() -> int:
    for name in HOOKS_MAP:
        p = HOOKS / name
        if p.exists() and MARK in p.read_text(encoding="utf-8", errors="replace"):
            p.unlink()
            print(f"  已删 {name}")
        elif p.exists():
            print(f"  跳过 {name}（不是本脚本装的，不碰）")
    return 0


def check() -> int:
    ok = True
    for name in HOOKS_MAP:
        p = HOOKS / name
        mine = p.exists() and MARK in p.read_text(encoding="utf-8", errors="replace")
        print(f"  {name:12s} {'✓ 已安装' if mine else '✗ 未安装'}")
        ok &= mine
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="安装/卸载提交泄漏闸门钩子")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.uninstall:
        return uninstall()
    if args.check:
        return check()
    return install()


if __name__ == "__main__":
    sys.exit(main())
