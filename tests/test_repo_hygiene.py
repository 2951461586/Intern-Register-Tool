"""仓库级元测试：拦住「本地全绿、新克隆/CI 必红」那一类失效。

存在理由
========
本仓库**已经两次**因为「`.gitignore` 挡住了必须有文件入库的东西」而在 CI 变红。
第一次的教训写成了 `.gitignore` 里那行 `!tests/fixtures/ledger_sample.json` ——
但**例外是逐文件手写的**，于是 2026-09-20（B5）新增
`tests/fixtures/report_render_golden.json` 时**同一个坑再踩一次**：

    夹具生成完，`git status` 里根本看不到它（被 `*.json` 吞了），
    本地 pytest 全绿，而新克隆的仓库会直接 FileNotFoundError。

失效点不是"忘了加例外"，而是**"记得加例外"这件事只能靠人**。
本文件把那个不变量变成可执行断言 —— 下次再加夹具，本地就会红。

⚠ 与 `tests/test_dependency_surface.py` 同族：都是**元测试**，
  守的是"测试与仓库配置之间的约定"，不是业务行为。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture_files() -> list[str]:
    """`tests/fixtures/` 下全部文件的仓库相对路径（POSIX 形式，git 认这个）。"""
    if not FIXTURES.is_dir():
        return []
    return sorted(p.relative_to(ROOT).as_posix() for p in FIXTURES.rglob("*") if p.is_file())


def test_the_fixture_scan_actually_found_files():
    """给扫描面自己的守卫。

    目录改名 / 挪位置会让下面那条参数化测试**静默消失**（0 个用例 = 全绿），
    而不是失败。这正是本文件要防的那类假绿。
    """
    files = _fixture_files()
    assert files, f"没在 {FIXTURES} 下扫到任何文件 —— 参数化测试会静默消失"


@pytest.mark.parametrize("rel", _fixture_files())
def test_every_test_fixture_is_committable(rel):
    """`tests/fixtures/` 下的每个文件都必须**不被 `.gitignore` 忽略**。

    判据走 `git check-ignore`（权威实现），不自己解析 `.gitignore` 的 glob ——
    自己实现一遍必然与 git 漂移，而漂移方向恰好是"以为没被忽略"。

    `check-ignore` 的退出码：**0 = 被忽略**，1 = 没被忽略，>1 = 用法/环境错误。
    """
    if not (ROOT / ".git").exists():
        pytest.skip("不是 git 检出（如 tarball 解压）—— .gitignore 规则不适用")

    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(ROOT), capture_output=True)
    assert r.returncode == 1, (
        f"{rel} 被 .gitignore 忽略了（check-ignore rc={r.returncode}）。\n"
        "后果：本地 pytest 全绿，**新克隆的仓库读不到这个文件** ⇒ 用例直接报错。\n"
        "修法：在 .gitignore 里加一行只放行**这一个文件**的例外，"
        f"形如 `!{rel}`（⚠ 不要写成 `!tests/fixtures/*` —— 那会把真凭据也放进来）。"
    )
