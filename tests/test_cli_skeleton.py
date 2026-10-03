"""`src/cli.py` 的契约测试 + 「两个入口真的共用了它」的接线护栏。

存在理由
========
`run.py` 与 `tools/run_downstream.py` 的 argparse 接线曾逐字重复
（见 `docs/audit-2026-09-20.md` §3.1）。抽成 `src/cli.py` 只解决**一半**问题 ——
另一半是"下一个人还照不照着用"。所以本文件两半都要：

  * **契约**：`add_headless_args` 的接线语义、`merge_summary_line` 的逐字节格式；
  * **接线**：两个入口必须**调用**它，而且**不能**再手写那一对开关。

⚠ 与 `tests/test_report.py` 的 §[4] 同型：只测被抽出来的函数不够 ——
  缺陷发生在**调用方**（漏 `dest`、help 又抄一份、只搬一半），
  那些在纯函数测试里全都看不见。
"""

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import cli

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ("run.py", "tools/run_downstream.py")


# ══ [1] `add_headless_args` 的接线语义 ═════════════════════════════════


def _parse_headless(argv):
    ap = argparse.ArgumentParser()
    cli.add_headless_args(ap)
    return ap.parse_args(argv)


def test_headless_is_the_default():
    """默认就是无头（不弹窗口）。"""
    assert _parse_headless([]).headless is True


def test_headful_writes_into_the_same_destination():
    """🔴 关键判据：`--headful` 必须写回 `args.headless`。

    漏掉 `dest="headless"` 时 argparse 会造出一个**独立**的 `args.headful`，
    而所有调用方都只读 `args.headless` ⇒ **`--headful` 静默无效**
    （不报错，用户以为切了有头，实际还是无头）。这正是要抽出来的理由。
    """
    ns = _parse_headless(["--headful"])
    assert ns.headless is False
    assert not hasattr(ns, "headful"), (
        "`--headful` 变成了一个独立字段 —— `dest='headless'` 丢了，开关会静默失效"
    )


def test_headless_flag_is_accepted_and_idempotent():
    """`--headless` 仍接受（旧脚本 / 文档里这么写），是幂等空操作。"""
    assert _parse_headless(["--headless"]).headless is True
    assert _parse_headless(["--headless", "--headful"]).headless is False


# ══ [2] `merge_summary_line` 的逐字节格式 ═════════════════════════════


def test_merge_summary_keeps_the_original_wording_byte_for_byte():
    assert cli.merge_summary_line(53, 4, 0, 57) == "\n结果合并：原有 53 条 + 本次新增 4 条 = 57 条"


def test_merge_summary_mentions_upgraded_only_when_nonzero():
    assert cli.merge_summary_line(10, 2, 3, 12) == (
        "\n结果合并：原有 10 条 + 本次新增 2 条（3 条已更新：升级或补全字段） = 12 条"
    )
    assert "已更新" not in cli.merge_summary_line(10, 2, 0, 12)


def test_merge_summary_returns_the_leading_newline():
    """换行是契约的一部分 —— 两个调用方原先都写成 `print(f"\\n结果合并：…")`。"""
    assert cli.merge_summary_line(1, 1, 0, 2).startswith("\n")
    assert cli.merge_summary_line(1, 1, 0, 2).count("\n") == 1


# ══ [3] 接线护栏：两个入口必须调用它，且不能再手写 ════════════════════


def _called_attr_names(path: Path) -> set[str]:
    """模块里所有 `X.<name>(...)` 形式的调用名。

    ⚠ 走 AST 而不是搜文本：本仓库的注释里大量提到这些名字
      （"别把它搬进 src/cli.py"之类），搜文本会被自己的注释判红。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _add_argument_flags(path: Path) -> list[str]:
    """文件里 `ap.add_argument("<flag>", ...)` 的第一个字面量参数。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    flags: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "add_argument"):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            flags.append(node.args[0].value)
    return flags


@pytest.mark.parametrize("rel", ENTRIES)
def test_entry_point_calls_the_shared_skeleton(rel):
    names = _called_attr_names(ROOT / rel)
    for fn in ("add_headless_args", "merge_summary_line"):
        assert fn in names, (
            f"{rel} 没有调用 src.cli.{fn} —— 又手写了一份？"
            "两个入口对同一个开关/同一行摘要给出不同行为，是不会报错的缺陷。"
        )


@pytest.mark.parametrize("rel", ENTRIES)
def test_entry_point_no_longer_hand_writes_the_headless_pair(rel):
    """反向护栏：防止"只搬一半"（抽了函数，旧的手写定义还留着）。

    留着的话 argparse 会**重复注册** `--headless`，`--help` 里出现两遍，
    而两个定义谁生效取决于顺序 —— 属于最难查的一类。
    """
    flags = _add_argument_flags(ROOT / rel)
    assert "--headless" not in flags and "--headful" not in flags, (
        f"{rel} 里还有手写的 --headless/--headful 定义 —— 应当只走 src.cli.add_headless_args"
    )


# ══ [4] 行为护栏：两个入口的 --help 必须报同一份说明 ══════════════════


def _help_text(rel: str) -> str:
    # 🔴 两个环境变量缺一不可，各有各的失败模式：
    #   · COLUMNS=200 —— 防 argparse 按窄终端折行，把说明句折成两段、
    #     断言里那句连续文本就再也匹配不到。
    #   · PYTHONIOENCODING=utf-8 —— 子进程 stdout 的默认编码**跟随本机
    #     区域设置**（Windows 中文机 = gbk），而下面固定按 utf-8 解码 ⇒
    #     中文说明会变成乱码、断言必红。CI 在 ubuntu 上跑（locale 本就是
    #     UTF-8）不会暴露，所以这个坑**只在 Windows 本地**出现 ——
    #     显式钉住子进程编码，判定才与运行机器的 locale 无关。
    env = dict(os.environ, COLUMNS="200", PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [sys.executable, rel, "--help"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.returncode == 0, f"{rel} --help 退出码 {r.returncode}：{r.stderr[:400]}"
    return r.stdout


@pytest.mark.parametrize("rel", ENTRIES)
def test_both_entries_advertise_the_same_headless_help(rel):
    """两个入口的 `--help` 里那一对说明必须**逐字**是 `src/cli.py` 的那两份。

    这条比"调用了函数"更强：有人把 help 文本改回各自一份，断言当场红。
    """
    text = _help_text(rel)
    assert cli.HEADLESS_HELP in text, f"{rel} 的 --headless 说明与 src/cli.py 不一致"
    assert cli.HEADFUL_HELP in text, f"{rel} 的 --headful 说明与 src/cli.py 不一致"


# ══ [5] 故意钉住"不统一"的那个决定 ════════════════════════════════════


def test_run_py_does_not_branch_on_is_ledger_path():
    """🔴 这条**故意**钉住一个"看起来该统一、其实不该"的决定。

    `run.py` 的落盘判据是 `out is None`（显式给 `--out` ⇒ 纯导出、不落快照）；
    `tools/run_downstream.py` 是 `ledger.is_ledger_path(dest)`（路径在台账目录里
    ⇒ 落快照）。在"显式 `--out` 指向 `ledger/` 内部"这一种输入下**两者行为不同**，
    而各自的 `--help` 都把自己那套写成了承诺 ⇒ 统一 = 悄悄改掉一边的语义。

    本断言不是"证明现状对"，而是让**想统一的人必须先读到这段说明**。
    """
    assert "is_ledger_path" not in _called_attr_names(ROOT / "run.py")


def test_run_downstream_does_branch_on_is_ledger_path():
    """另一半：那边确实靠 `is_ledger_path` 分流（上面那条的反面）。"""
    assert "is_ledger_path" in _called_attr_names(ROOT / "tools" / "run_downstream.py")
