"""元测试：钉住「测试链需要哪些第三方依赖」。

为什么需要这个测试
==================
`.github/workflows/ci.yml` 的 test job 装的是一个**精确列表**
（`pytest requests cryptography`），而不是 `-r requirements.txt` ——
因为测试**真的**不需要 `playwright`（`src/browser/session.py` 里是函数体内
的延迟 import），这个边界值得留住。

但"精确列表"有个致命风险：**哪天有人给 `src/` 加一行模块级 `import foo`，
而 `foo` 被测试链间接拉进来 —— 本地测试会过（本地装过），CI 会红。**

2026-09-19 就是这样：`ci.yml` 里写着"测试链上零第三方依赖"，实际
`tests/test_error_kind.py` → `src.pipeline` → `src.discovery` → `import requests`，
**CI 连续红了 3 次**，而本地全绿。

⇒ 本测试把"注释里的假设"变成**可执行断言**：依赖面一变，本地跑测试就红。

判据
----
从 `tests/test_*.py` 出发，递归展开 `src/` 的**模块级** import，
收集所有「非 stdlib、非本项目」的顶层包名，断言：

    found ⊆ ALLOWED

⚠ 只看模块级 import —— 函数体内的延迟 import（如 `playwright`）在收集测试时
  不会执行到，所以不算测试链依赖。这正是"零 playwright"能成立的原因。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STDLIB = set(sys.stdlib_module_names)

# 🔴 允许测试链使用的第三方包。
#    加新包**必须同时改这里和 `.github/workflows/ci.yml` 的安装步骤** ——
#    两处一起改才不会漂移（只改一处，就是本测试要拦的那种失败）。
ALLOWED = {"pytest", "requests", "cryptography"}

# 明确**不许**被测试链拉进来的重依赖。列出来只为给出可读的失败信息 ——
# 它们本来就会被 ALLOWED 拦下。
FORBIDDEN = {"playwright"}


def _rel(path: Path) -> str:
    """仓库内文件的相对路径；仓库外的（测试用临时文件）退回文件名。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def _pkg_parts(path: Path) -> list[str]:
    """文件所在包的分段；仓库外返回 `[]`。"""
    try:
        return list(path.relative_to(ROOT).parent.parts)
    except ValueError:
        return []


def _module_file(mod: str) -> Path | None:
    """把 `src.a.b` 映射到仓库里的 `.py` 文件；不是模块就返回 None。"""
    if not mod:
        return None
    p = ROOT.joinpath(*mod.split("."))
    for cand in (p.with_suffix(".py"), p / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _resolve_relative(pkg_parts: list[str], level: int, module: str | None) -> str:
    """把相对导入解析成绝对模块名。

    `pkg_parts` 是**当前文件所在包**的分段，例如 `src/pipeline.py` 是 `["src"]`，
    `src/browser/attempt.py` 是 `["src", "browser"]`。

    `level=1` → 当前包；`level=2` → 上一级；以此类推。
    """
    base = pkg_parts[: len(pkg_parts) - (level - 1)] if level > 1 else pkg_parts
    return ".".join([*base, module]) if module else ".".join(base)


def _collect(path: Path, chain: tuple[str, ...], found: dict[str, str],
             visited: set[str]) -> None:
    """递归扫描一个文件的**模块级** import。

    `found` 收集「第三方包名 → 第一条把它拉进来的 import 链」（用于失败信息）。
    """
    rel = _rel(path)
    if rel in visited:
        return
    visited.add(rel)

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):                         # pragma: no cover
        return

    pkg_parts = _pkg_parts(path)

    for node in tree.body:                                 # ⚠ 只扫模块级
        candidates: list[str] = []

        if isinstance(node, ast.Import):
            candidates = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                mod = _resolve_relative(pkg_parts, node.level, node.module)
                if node.module:
                    candidates = [mod, *(f"{mod}.{a.name}" for a in node.names)]
                else:
                    candidates = [f"{mod}.{a.name}" for a in node.names]
            else:
                mod = node.module or ""
                candidates = [mod, *(f"{mod}.{a.name}" for a in node.names)]

        for mod in candidates:
            if not mod:
                continue
            top = mod.split(".")[0]
            if top in STDLIB or top == "__future__":
                continue

            f = _module_file(mod)
            if f is not None:
                _collect(f, chain + (mod,), found, visited)
                continue

            # 解析不到文件：本项目内的（`src`）多半是**符号**而非模块，跳过
            # （例：`from src.browser.constants import MICRO_MOVE` 里的 MICRO_MOVE）
            if top == "src":
                continue

            found.setdefault(top, " → ".join(chain + (mod,)))


def _third_party_of_test_chain() -> dict[str, str]:
    found: dict[str, str] = {}
    visited: set[str] = set()
    for t in sorted((ROOT / "tests").glob("test_*.py")):
        _collect(t, (t.relative_to(ROOT).as_posix(),), found, visited)
    return found


def test_test_chain_third_party_is_within_the_declared_allowlist():
    """测试链上的第三方依赖必须全在 `ALLOWED` 里。

    失败时说明：有人给 `src/`（或测试）加了新的模块级第三方 import，
    而它被测试链间接拉进来了。**修法二选一**：

      1. 把它加进 `ALLOWED`，**并同步改 `.github/workflows/ci.yml` 的安装步骤**；
      2. 改成函数体内延迟 import（如果测试确实不需要它）。

    ⚠ 不要只是把包名加进 `ALLOWED` 而不改 ci.yml —— 那样本地会过、CI 会红，
      正是本测试要消灭的那个失败模式。
    """
    found = _third_party_of_test_chain()
    unexpected = sorted(set(found) - ALLOWED)

    assert not unexpected, (
        "测试链上出现了未声明的第三方依赖：\n"
        + "\n".join(f"  ✗ {pkg}   ← {found[pkg]}" for pkg in unexpected)
        + f"\n\n已声明允许：{sorted(ALLOWED)}"
        + "\n修法：加进 tests/test_dependency_surface.py 的 ALLOWED，"
        + "**并同步改 .github/workflows/ci.yml 的 `pip install` 那一行**；"
        + "或者改成延迟 import。"
    )


def test_playwright_never_enters_the_test_chain():
    """`playwright` 是重依赖（还要另跑 `playwright install` 下浏览器）。

    测试链上不需要它 —— 这也是 ci.yml 不装 `-r requirements.txt` 的唯一理由。
    哪天它被模块级 import 拉进来了，这个测试会指出是哪条链。
    """
    found = _third_party_of_test_chain()
    leaked = sorted(FORBIDDEN & set(found))

    assert not leaked, (
        "重依赖进入了测试链（应改为函数体内延迟 import）：\n"
        + "\n".join(f"  ✗ {pkg}   ← {found[pkg]}" for pkg in leaked)
    )


def test_the_scanner_itself_detects_a_planted_dependency(tmp_path):
    """变异验证：证明扫描器**真的会拦**，而不是永远为真。

    ⚠ 没有这条，上面两个断言可能因为"扫描器坏了、什么都扫不到"而永远通过 ——
      那是最糟的假绿。

    一次同时验证三条边界：
      1. 模块级的第三方 import **必须被抓**；
      2. stdlib **不许**误报；
      3. **函数体内**的 import 不许算进来 —— 否则 `playwright` 之类的
         延迟 import 会变成假阳性，而"零 playwright"正是 ci.yml 不装
         `-r requirements.txt` 的唯一理由。
    """
    fake = tmp_path / "planted.py"
    fake.write_text(
        "import os\n"
        "import totally_made_up_pkg_xyz\n"
        "\n"
        "\n"
        "def f():\n"
        "    import also_should_not_be_seen\n"
        "    return 1\n",
        encoding="utf-8",
    )
    found: dict[str, str] = {}
    _collect(fake, ("planted.py",), found, set())

    assert "totally_made_up_pkg_xyz" in found, "扫描器漏掉了模块级第三方 import"
    assert "os" not in found, "扫描器把 stdlib 误报成第三方"
    assert "also_should_not_be_seen" not in found, (
        "扫描器把**函数体内**的 import 也算进来了 —— "
        "那会把 playwright 之类的延迟 import 变成假阳性"
    )


@pytest.mark.parametrize(
    ("pkg_parts", "level", "module", "expect"),
    [
        (["src"], 1, "discovery", "src.discovery"),
        (["src"], 1, None, "src"),
        (["src", "browser"], 1, "constants", "src.browser.constants"),
        (["src", "browser"], 2, "config", "src.config"),
    ],
)
def test_relative_import_resolution(pkg_parts, level, module, expect):
    """相对导入解析 —— 它是整个扫描器的地基，单独钉住。"""
    assert _resolve_relative(pkg_parts, level, module) == expect
