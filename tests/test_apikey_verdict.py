"""`src.apikey` 的**存活性探针判定策略**：纯函数契约 + 工具静态接线。

为什么这两半要放在一起
----------------------
`tools/ops/check_keys_alive.py` 是 CLI —— 测试链**不 import 它**（它要发真实网络
请求，而且 `from _path import ROOT` 要求 `tools/` 在 `sys.path` 上）。
⇒ 判据必须抽到 `src/apikey.py`（纯函数，可单独测），工具只负责接线。
⇒ 本文件两半：**契约**钉纯函数，**静态接线**钉工具真的在用它。

要防的那个误读
--------------
旧版把 HTTP 429 归进 `error`，于是**复核自己造成的限流**读起来像
"这把 key 有问题"。2026-09-22 实测（100 批次复核）：

| 口径 | 存活 | 429 |
|---|---|---|
| 并发 8（无间隔） | 10/47 | **37** |
| 串行 + 间隔 5s | **97/97** | 0 |

⇒ 429 必须是**独立的一档**，且**绝不进** `dead`（`dead` 只认 401/403 这种
  确定性拒绝）。下面的用例就是钉这件事，`test_rate_limit_is_not_death`
  和 `test_naive_429_as_dead_changes_the_bottom_line` 是其中的判别力来源。
"""

from collections import Counter
from pathlib import Path

import pytest

from src import apikey as A

# 工具源码 —— 静态接线检查要用（见文件后半）。
_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "ops" / "check_keys_alive.py"


def _tool_src() -> str:
    return _TOOL.read_text(encoding="utf-8")


# ── 契约：状态码 → 结论 ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("code", "expect"),
    [
        (200, A.VERDICT_ALIVE),
        (401, A.VERDICT_DEAD),
        (403, A.VERDICT_DEAD),
        (429, A.VERDICT_RATE_LIMITED),
        (400, A.VERDICT_ERROR),
        (404, A.VERDICT_ERROR),
        (500, A.VERDICT_ERROR),
        (502, A.VERDICT_ERROR),
        (503, A.VERDICT_ERROR),
        (0, A.VERDICT_ERROR),
        (999, A.VERDICT_ERROR),
        (None, A.VERDICT_ERROR),
    ],
)
def test_status_mapping(code, expect):
    assert A.verdict_of_status(code) == expect


def test_rate_limit_is_not_death():
    """🔴 本文件存在的理由：429 与 401/403 必须落在**不同的**档。

    判别力是双向的：
      * 把 429 判成 `dead` ⇒ 第 2 条红；
      * 把 401 判成 `rate_limited` ⇒ 第 3 条红。
    """
    assert A.verdict_of_status(429) == A.VERDICT_RATE_LIMITED
    assert A.verdict_of_status(429) != A.VERDICT_DEAD
    assert A.verdict_of_status(401) == A.VERDICT_DEAD
    assert A.verdict_of_status(429) != A.verdict_of_status(401)


def test_naive_429_as_dead_changes_the_bottom_line():
    """把 429 判成 `dead` 会**改变结论的含义**，不只是换个标签。

    用真实分布（10 把 200 + 37 把 429，即 2026-09-22 并发 8 那次的形态）
    对比两种实现：正确实现报「0 死亡、37 状态未知」，朴素实现报「37 死亡」。
    ⇒ 同一批数据，一个说"没问题"，一个说"37 把 key 废了"。
    """
    codes = [200] * 10 + [429] * 37
    good = Counter(A.verdict_of_status(c) for c in codes)
    naive = Counter(
        A.VERDICT_DEAD if c == 429 else A.verdict_of_status(c) for c in codes
    )
    assert good[A.VERDICT_DEAD] == 0, "正确实现里不该有任何一把被判死"
    assert naive[A.VERDICT_DEAD] == 37, "朴素实现会把 37 把 429 全判死"
    assert good[A.VERDICT_DEAD] != naive[A.VERDICT_DEAD], (
        "两种实现的结论相同 ⇒ 这个用例没有判别力"
    )


def test_verdict_is_total():
    """任何整数输入都要落在四档里 —— 不许出现 `None` 或未定义字符串。

    汇总代码用 `==` 逐档筛，漏一档就会**静默少算**（既不进 alive 也不进
    dead，最后只在 total 里出现）。
    """
    allowed = {
        A.VERDICT_ALIVE, A.VERDICT_DEAD, A.VERDICT_RATE_LIMITED, A.VERDICT_ERROR,
    }
    for code in list(range(0, 600)) + [None]:
        assert A.verdict_of_status(code) in allowed, f"{code} 落到了未定义的档"


def test_dead_status_contract_is_exactly_401_and_403():
    """`DEAD_STATUS` 是**契约数值**，不是实现细节 —— 放宽它会凭空造出死 key。

    ⚠ 钉这个常量等于钉"什么叫 key 死了"。要改它，得先想清楚
      新加进去的码是不是**确定性拒绝**（例如 429 就不是）。
    """
    assert tuple(A.DEAD_STATUS) == (401, 403)
    assert A.RATE_LIMIT_STATUS == 429
    # 两个集合不许相交 —— 相交的话"确定性拒绝"这个语义就自相矛盾了
    assert A.RATE_LIMIT_STATUS not in A.DEAD_STATUS


# ── 契约：429 退避时长 ──────────────────────────────────────────────

def test_backoff_starts_at_base():
    assert A.rate_limit_backoff(0, base=20.0) == 20.0
    assert A.rate_limit_backoff(0, base=7.5) == 7.5


def test_backoff_grows_linearly_then_flatlines_at_cap():
    """线性加长 + 封顶。钉住"封顶存在"这件事 —— 没有它，attempt 大了会等到天亮。"""
    assert A.rate_limit_backoff(1, base=20.0) == 40.0
    assert A.rate_limit_backoff(2, base=20.0) == 60.0
    assert A.rate_limit_backoff(99, base=20.0) == A.RATE_LIMIT_BACKOFF_CAP
    assert A.rate_limit_backoff(10_000, base=20.0) == A.RATE_LIMIT_BACKOFF_CAP


def test_backoff_is_monotonic_and_positive():
    seq = [A.rate_limit_backoff(i, base=20.0) for i in range(20)]
    assert all(b > 0 for b in seq), f"退避时长必须为正：{seq}"
    assert seq == sorted(seq), f"退避时长必须单调不减：{seq}"


def test_backoff_cap_is_a_sane_short_window():
    """封顶不该超过"短窗口限流"的量级（本项目实测窗口是几十秒）。"""
    assert 30.0 <= A.RATE_LIMIT_BACKOFF_CAP <= 300.0, (
        f"封顶 {A.RATE_LIMIT_BACKOFF_CAP}s 不像一个短窗口限流的退避上限"
    )


# ── 静态接线：工具真的用了这套判据 ──────────────────────────────────
#
# ⚠ 只能做静态检查 —— 跑这个工具要发真实网络请求，不适合进测试链。
#   它证明的是"接线还在"，**不是**"运行结果一定对"。这是已知盲区。

def test_tool_declares_the_pacing_flags():
    src = _tool_src()
    for flag in ('"--interval"', '"--backoff"', '"--max-retry"'):
        assert flag in src, f"工具没声明 {flag} —— 核验存活率时又会踩假 429"


def test_tool_cli_defaults_come_from_module_constants():
    """默认值必须来自模块常量，不许在 argparse 里另抄一份字面量。

    抄一份的后果：有人调了 `DEFAULT_BACKOFF`，`--help` 显示的还是旧值，
    而两边都"看起来对"（同 `MAIL_POLL_INTERVAL` 那个坑）。
    """
    src = _tool_src()
    assert "default=DEFAULT_BACKOFF" in src, "退避默认值不是来自常量"
    assert "default=DEFAULT_MAX_RETRY" in src, "重试次数默认值不是来自常量"


def test_tool_calls_the_pure_policy():
    src = _tool_src()
    assert "_ak.verdict_of_status(" in src, "工具没调用纯判据 —— 分类逻辑又回到工具里了"
    assert "_ak.rate_limit_backoff(" in src, "工具没调用退避判据"


def test_tool_retries_429_instead_of_giving_up():
    """光"认出 429"不够 —— 还得**退避重试**，否则 429 仍然等于测不出来。"""
    src = _tool_src()
    assert "time.sleep(delay)" in src, "认出了 429 却没有退避 —— 等于没重试"
    assert "for attempt in range(max_retry + 1)" in src, "没有重试循环"


def test_dead_bucket_is_built_only_from_verdict_dead():
    """`dead` 桶只能来自 `VERDICT_DEAD`，不能来自状态码字面量列表。

    判别力：谁把 `dead` 改回 `if code in (401, 403, 429)` 那种形式，这条就红
    —— 而那正是本文件要防的回归。同时钉住 `rate_limited` 有**自己的**桶。
    """
    src = _tool_src()
    assert 'v["verdict"] == _ak.VERDICT_DEAD' in src, "dead 桶不是按纯判据筛的"
    assert 'v["verdict"] == _ak.VERDICT_RATE_LIMITED' in src, (
        "限流没有自己的桶 —— 它会被并回 dead/error，正是要防的误读"
    )
    # 429 的字面量只允许出现在说明文字里，不许出现在筛选用途上
    assert 'v["verdict"] == "dead"' not in src, "又出现了硬编码的 dead 字面量"
    assert 'v["verdict"] == "alive"' not in src, "又出现了硬编码的 alive 字面量"


def test_rate_limited_reaches_the_artifact():
    """429 必须进 `keys_alive.json` —— 只在 stdout 说一次，事后单独打开
    artifact 的人仍会把它读成"测过了、没问题"（同覆盖护栏那次的教训）。"""
    src = _tool_src()
    assert '"rate_limited": len(rl)' in src, "限流计数没写进 artifact"
    assert '"interval_s": args.interval' in src, (
        "artifact 没记本次的速率参数 —— 事后无法判断这份结论是快测还是慢测出来的"
    )


def test_tool_has_the_serial_guard_and_it_actually_bites():
    """`--interval` 与并发同时给时，必须**真的把并发改成 1**，不能只打印。"""
    src = _tool_src()
    assert "已强制串行" in src, "没有'强制串行'的提示"
    assert "args.workers = 1" in src, (
        "只打印了提示、没真的改并发 —— 那这条护栏是装饰"
    )


def test_tool_warns_about_the_known_false_429_configuration():
    """并发 >1 且无间隔是**已知会打出假 429** 的配置，必须提示。"""
    src = _tool_src()
    assert "已知会打出假 429" in src, "少了这条提示，下一个人还会踩同一个坑"


def test_serial_branch_and_concurrent_branch_both_exist():
    """两条路径都要在：串行（有间隔）与并发（无间隔）。

    判别力：只留一条会红 —— 把并发路径删掉会让既有用法变慢，
    把串行路径删掉则 `--interval` 变成空开关。
    """
    src = _tool_src()
    assert "if args.interval > 0:" in src, "没有串行分支 —— --interval 是空开关"
    assert "ThreadPoolExecutor(max_workers=args.workers)" in src, "并发分支被删了"


def test_tool_skips_the_trailing_sleep():
    """串行分支的最后一把之后不该再睡 —— 白等一轮 interval 没有意义。"""
    src = _tool_src()
    assert "if i < len(rows) - 1:" in src, "最后一把之后还在睡 —— 白等"
