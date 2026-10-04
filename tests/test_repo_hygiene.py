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

import ast
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


# ══════════════════════════════════════════════════════════════════
# 元测试：`T | None = None` 的形参标注约定
# ══════════════════════════════════════════════════════════════════
# 形如 `def f(x: str = None)` 的写法**在类型上撒谎**：默认值是 `None`，
# 标注却说 `str`。静态检查器（pyright 等）会当成硬错误，而运行时毫无提示。
#
# 🔴 为什么值得一条测试：仓库已经为它做过一次**全库**扫描
#    （`640044e style(types): 形参默认值 X = None → X | None = None`，
#     7 文件 / 31 处），之后**又攒回了 5 处** —— 而"记得扫"这件事只能靠人。
#    手扫会漏，`ast` 不会：同一批遗留，正则在一条命令里只数出 1 处，
#    实际是 5 处（正则的候选类型表没覆盖 `list[dict]` 这类下标泛型）。
#    这就是 `tests/test_dependency_surface.py` 那套思路：把"以后记得改"
#    变成一条当场会红的断言。
_SOURCE_ROOTS = ["src", "common", "tools", "tests"]
_EXTRA_FILES = ["run.py"]


def _source_files() -> list[Path]:
    """要扫的源码文件：4 个源码目录 + 仓库根的 `run.py`。"""
    files = [ROOT / f for f in _EXTRA_FILES]
    for r in _SOURCE_ROOTS:
        files += [p for p in (ROOT / r).rglob("*.py") if "__pycache__" not in p.parts]
    return files


def _non_optional_none_params() -> list[str]:
    """扫出「默认值是 `None`、标注却不是 `X | None`」的形参（可读位置串）。

    覆盖位置参数、仅位置参数与仅关键字参数。
    `kw_defaults` 里用 Python `None`（而非 `ast.Constant`）表示"没有默认值"，
    所以"有默认值且默认值是字面量 None"必须与它区分开 —— 否则会把必填参数
    全误报一遍。
    """
    hits: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            positional = a.posonlyargs + a.args
            paired = list(
                zip(
                    positional,
                    [None] * (len(positional) - len(a.defaults)) + list(a.defaults),
                    strict=True,
                )
            )
            paired += list(zip(a.kwonlyargs, a.kw_defaults, strict=True))
            for arg, default in paired:
                if arg.annotation is None or default is None:
                    continue
                if not (isinstance(default, ast.Constant) and default.value is None):
                    continue
                ann = ast.unparse(arg.annotation)
                if not ann.endswith("| None"):
                    rel = path.relative_to(ROOT).as_posix()
                    hits.append(f"{rel}:{node.lineno}  {node.name}({arg.arg}: {ann} = None)")
    return hits


def test_the_annotation_scan_actually_parsed_the_tree():
    """给扫描面自己的守卫。

    目录改名 / 挪位置会让下面那条参数化断言**静默变成 0 命中**（全绿），
    而不是失败 —— 与本文件开头那个夹具坑同形。
    """
    n = len(_source_files())
    assert n > 50, f"只扫到 {n} 个源码文件 —— 扫描面塌了，断言会空转"


def test_none_defaults_declare_optional_annotations():
    """🔴 默认值是 `None` ⇒ 标注必须写成 `T | None`。

    纯标注改动，不改任何运行时行为。失败时下面会把每一处的
    `文件:行号  函数(形参: 标注 = None)` 列出来，可直接照着改。
    """
    hits = _non_optional_none_params()
    assert not hits, (
        "这些形参的默认值是 None，标注却没写 `| None`（类型上撒谎）：\n  "
        + "\n  ".join(hits)
        + "\n修法：`x: T = None` → `x: T | None = None`（纯标注，行为不变）。"
    )


# ══════════════════════════════════════════════════════════════════
# 元测试：`ledger.merge_records()` 的调用点必须按 **4 元组**解包
# ══════════════════════════════════════════════════════════════════
# 2026-10-04 实测到一处真的会崩：`tools/data/recover_activation.py` 写着
#
#     merged, upgraded = ledger.merge_records(existing, updates)
#
# 而该函数返回 `(merged, kept, added, upgraded)` ⇒ 一旦加上 `--write`
# 且真救回至少一个账号，**当场 `ValueError: too many values to unpack`**。
#
# 🔴 为什么必须用元测试兜：这个失效**编译期不报**，而且那个工具不是批量跑
#    的日常路径 —— 等它崩的时候，恰恰是你丢了一批激活状态、正需要它救数据
#    的时候。"返回的元组变长了、某个调用点没跟上"在本仓库已出现过同类
#    （标注扫描也是扫了两轮才干净），所以把不变量写成断言，而不是写进注释。
_MERGE_RETURN_ARITY = 4


def _merge_records_unpack_sites() -> list[tuple[str, int, int]]:
    """全部**解包** `merge_records()` 返回值的位置 → `(文件, 行号, 解包个数)`。

    只统计"真在解包"的写法：
      · `a, b, c, d = merge_records(...)`        → 统计（四元组）
      · `rows, *_ = merge_records(...)`          → 跳过（`*` 能吸收多余项）
      · `res = merge_records(...)`               → 跳过（没解包）
    """
    sites: list[tuple[str, int, int]] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            fn = node.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "merge_records":
                continue
            target = node.targets[0]
            if not isinstance(target, (ast.Tuple, ast.List)):
                continue
            if any(isinstance(e, ast.Starred) for e in target.elts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            sites.append((rel, node.lineno, len(target.elts)))
    return sites


def test_the_merge_records_scan_actually_found_call_sites():
    """给扫描面自己的守卫 —— 扫到 0 个调用点时下面那条会空转成结。"""
    sites = _merge_records_unpack_sites()
    assert sites, "没扫到任何解包 merge_records() 的位置 —— 扫描面塔了"


def test_merge_records_call_sites_unpack_all_four_values():
    """🔴 `merge_records()` 返回 4 项，解包就必须是 4 项。"""
    bad = [
        f"{f}:{ln}  解包成 {n} 个（该函数返回 {_MERGE_RETURN_ARITY} 个）"
        for f, ln, n in _merge_records_unpack_sites()
        if n != _MERGE_RETURN_ARITY
    ]
    assert not bad, (
        "这些位置解包的个数与 `ledger.merge_records()` 的返回元组不符，"
        "运行时会在那一行 `ValueError: too many values to unpack`：\n  "
        + "\n  ".join(bad)
        + "\n修法：改成 `merged, kept, added, upgraded = ...`"
        "（或确认不用后两项时写 `merged, _kept, _added, upgraded = ...`）。"
    )
