"""`src/report.render_batch_report()` 的契约测试。

存在理由
========
这块 stdout 原本是 `run.py` 的第 231..408 行（178 行），**测试链碰不到它** ——
而它恰恰是人唯一会读的东西。2026-09-20（B5）把它搬到 `src/report.py`，
验收标准是「与改前**逐字节**相同」（见 §[8] 的金标准比对）。

本文件钉的是**行为**：每个分支各自的判据。金标准只能证明"没变"，
不能说明"对"；两者缺一不可。

写法约定
--------
每条断言都尽量构造「两种实现结果不同」的输入 —— 否则测了等于没测。
例：`test_totals_column_is_per_account_not_the_batch_wall_clock` 里两个账号的
`batch_total` **故意相同**，只有三段之和不同，退化成读 `batch_total` 的实现
会印出两个一样的数。
"""

import contextlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import report
from src.pipeline import ERR_NETWORK, ERR_QUOTA, AccountRecord, error_kind_of

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "report_render_golden.json"

QUOTA_DESC = "3/40 已用（窗口 24h，最早 213.7 分钟后滑出）"
CONFIG_STUB = SimpleNamespace(
    CHAT_API_BASE="https://discovery-api.intern-ai.org.cn/v1",
    CHAT_MODELS=["deepseek-v4-flash-0731"],
)
WRITTEN = Path("ledger/runs/2026-09-20/results-20260920-120000.json")

# 落盘路径是绝对路径，夹具里换成占位符（机器路径不能进仓库，见模块 docstring 的说明）
_DONE_RE = re.compile(r"^(DONE: \d+/\d+ succeeded -> ).*$", re.M)


def _neutralize(text: str) -> str:
    return _DONE_RE.sub(r"\1<WRITTEN>", text)


# ── 桩与构造器 ─────────────────────────────────────────────────────────


class _QuotaStub:
    def status(self):
        return SimpleNamespace(describe=lambda: QUOTA_DESC)


def rec(email, status, **kw):
    """造一条 `AccountRecord`；`reg` / `login` / `key` 是毫秒。"""
    tm = {}
    for src, dst in (("reg", "register"), ("login", "login"), ("key", "key")):
        if src in kw:
            tm[dst] = kw.pop(src)
    if "login_detail" in kw:
        tm["login_detail"] = kw.pop("login_detail")
    if "reg_detail" in kw:
        tm["register_detail"] = kw.pop("reg_detail")
    for k in ("keys_done", "batch_total"):
        if k in kw:
            tm[k] = kw.pop(k)
    captcha = kw.pop("captcha", "")
    if captcha:
        kw["stages"] = {"captcha_path": captcha}
    if "kind" in kw:  # 短别名，少写 8 个字符
        kw["error_kind"] = kw.pop("kind")
    return AccountRecord(email=email, status=status, timings=tm, **kw)


def ok(email, *, reg=1000, login=1000, key=1000, **kw):
    """一条成功记录，三段耗时都有。`api_key` 可覆盖，默认 `"k"`。"""
    kw.setdefault("api_key", "k")
    return rec(email, "success", reg=reg, login=login, key=key, **kw)


def render(
    results,
    *,
    written=WRITTEN,
    wall=63.4,
    workers=4,
    quota=None,
    config=CONFIG_STUB,
    quota_line=None,
) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report.render_batch_report(
            results,
            written,
            wall,
            workers=workers,
            quota=quota or _QuotaStub(),
            config=config,
            error_kind_of=error_kind_of,
            err_quota=ERR_QUOTA,
            quota_line=quota_line,
        )
    return buf.getvalue()


# ══ [1] 头部 ═══════════════════════════════════════════════════════════


def test_done_line_counts_only_successes_and_resolves_the_path():
    out = render([ok("a@x.com"), rec("b@x.com", "failed", error="boom"), rec("c@x.com", "skipped")])
    assert f"DONE: 1/3 succeeded -> {WRITTEN.resolve()}" in out


def test_skipped_hint_appears_only_when_something_was_skipped():
    with_skip = render([ok("a@x.com"), rec("b@x.com", "skipped")])
    without = render([ok("a@x.com")])
    assert "个被配额保护跳过（未发请求，非失败）" in with_skip
    assert "个被配额保护跳过" not in without


# ══ [2] 耗时明细 ═══════════════════════════════════════════════════════


def test_incomplete_rows_show_dash_and_stay_out_of_the_stats_sample():
    """缺段的账号在「合计」列必须是 `-`，**且不能被算进统计样本**。

    两种实现结果不同：把缺段当 0 参与统计的实现，样本数会变成 2、均值也会变。
    """
    out = render(
        [
            ok("full@x.com", reg=2000, login=15000, key=7000),
            rec("half@x.com", "failed", error="boom", reg=9000),
        ]
    )
    assert "── 统计（1 个完整样本）" in out, "缺段账号被算进了统计样本"
    assert "24.0 / 24.0 / 24.0 / 24.0" in out, "统计值受了缺段账号影响"
    assert re.search(r"^  half@x\.com\s+9\.0\s+-\s+-\s+-$", out, re.M), "缺段账号的合计列不是 `-`"


def test_totals_column_is_per_account_not_the_batch_wall_clock():
    """合计列必须是**单账号三段之和**，不能退化成 `batch_total`。

    历史缺陷：`tm.get('total') or tm.get('batch_total')` —— `total` 键根本不存在，
    于是每行都印同一个批次墙钟（实测 510.0 刷满 39 行）。
    这里两个账号的 `batch_total` **故意相同**，退化的实现会印出两个一样的数。
    """
    out = render(
        [
            ok("a@x.com", reg=1000, login=1000, key=1000, keys_done=51000, batch_total=58000),
            ok("b@x.com", reg=9000, login=9000, key=9000, keys_done=51000, batch_total=58000),
        ]
    )
    assert re.search(r"^  a@x\.com\s+1\.0\s+1\.0\s+1\.0\s+3\.0$", out, re.M)
    assert re.search(r"^  b@x\.com\s+9\.0\s+9\.0\s+9\.0\s+27\.0$", out, re.M)


def test_missing_email_gets_a_placeholder_not_an_empty_column():
    out = render([rec("", "failed", error="boom")])
    assert "(未建邮箱)" in out


# ══ [3] 统计行：偏态样本钉顺序 ═════════════════════════════════════════


def _skewed_batch():
    """三段之和 = 3.0 / 6.0 / 27.0 秒 ⇒ 均值 12.0 / 中位 6.0 / 最快 3.0 / 最慢 27.0。

    🔴 四个值**互不相等**。等距样本（如 1,2,3,4）会让均值==中位，
    顺序错位就看不出来了 —— 那正是本模块要防的缺陷。
    """
    return [
        ok("a@x.com", reg=1000, login=1000, key=1000),
        ok("b@x.com", reg=2000, login=2000, key=2000),
        ok("c@x.com", reg=9000, login=9000, key=9000),
    ]


def test_stats_line_keeps_labels_and_values_on_one_line():
    out = render(_skewed_batch())
    hits = [ln for ln in out.splitlines() if report.LATENCY_LABELS in ln]
    assert len(hits) == 1, f"标签应当只出现在一行，实际 {len(hits)} 行"
    line = hits[0]

    labels = report.LATENCY_LABELS.split(" / ")
    values = [v.strip() for v in line.split(report.LATENCY_LABELS)[1].split(" / ")]
    assert len(labels) == 4
    assert len(values) == len(labels), (
        f"标签 {len(labels)} 个 / 取值 {len(values)} 个 —— 又错位了：{line!r}"
    )


def test_stats_values_are_mean_median_min_max_in_that_order():
    """偏态样本下四个值互不相等，顺序一错立刻红。"""
    out = render(_skewed_batch())
    line = next(ln for ln in out.splitlines() if report.LATENCY_LABELS in ln)
    values = [v.strip() for v in line.split(report.LATENCY_LABELS)[1].split(" / ")]
    assert values == ["12.0", "6.0", "3.0", "27.0"], (
        f"顺序必须是 均值 / 中位 / 最快 / 最慢；第三个值是**最快**、第四个是**最慢**，实际 {values}"
    )


def test_stats_header_reports_the_sample_count():
    out = render(_skewed_batch())
    assert "── 统计（3 个完整样本）" in out


# ══ [4] 取「最慢账号」而不是第一个 ═════════════════════════════════════


def test_login_detail_comes_from_the_slowest_account():
    """批量吞吐由关键路径决定，离群值才是要找的东西 —— 不能只印第一个账号。"""
    out = render(
        [
            ok("fast@x.com", login=3000, login_detail={"goto": 500, "only_fast": 900}),
            ok("slow@x.com", login=39000, login_detail={"goto": 1000, "only_slow": 31000}),
        ]
    )
    assert "登录内部阶段（最慢账号 slow@x.com，登录 39.0s）：" in out
    assert "only_slow" in out
    assert "only_fast" not in out, "印的是第一个账号，不是最慢的"


def test_register_detail_comes_from_the_slowest_account():
    out = render(
        [
            ok("fast@x.com", reg=2000, reg_detail={"mailbox": 100, "only_fast_reg": 900}),
            ok("slow@x.com", reg=40000, reg_detail={"mailbox": 200, "only_slow_reg": 39000}),
        ]
    )
    assert "注册内部阶段（最慢账号 slow@x.com，注册 40.0s）：" in out
    assert "only_slow_reg" in out
    assert "only_fast_reg" not in out


def test_captcha_path_is_annotated_only_for_traceless():
    a = render([ok("a@x.com", login_detail={"goto": 900}, captcha="A")])
    b = render([ok("b@x.com", login_detail={"goto": 900}, captcha="B")])
    assert "Path A（TRACELESS 自过，零点击）" in a
    assert "Path B" in b and "TRACELESS" not in b


# ══ [5] 注册明细：毫秒 / 计数 分开 ═════════════════════════════════════


def test_mail_wait_is_split_into_arrival_and_poll_overhead():
    out = render(
        [
            ok(
                "a@x.com",
                reg=2000,
                reg_detail={"mail_wait": 4200, "arrival_delay_ms": 2600, "poll_overhead_ms": 1600},
            )
        ]
    )
    assert "mail_wait        （到达 2.60s + 轮询 1.60s）" in out
    assert "arrival_delay_ms" not in out, "拆分用的键不该再单独印一行"
    assert "poll_overhead_ms" not in out


def test_count_fields_are_printed_as_counts_not_seconds():
    """`mail_polls` / `mail_5xx` 是**计数** —— 混进耗时列会印成 `0.01s` 那种假耗时。"""
    out = render(
        [ok("a@x.com", reg=2000, reg_detail={"mailbox": 300, "mail_polls": 7, "mail_5xx": 1})]
    )
    assert "mailbox" in out and "0.30s" in out
    assert "7 次 /api/inbox" in out
    assert "，其中 5xx 重试 1 次" in out
    assert "mail_polls" not in out
    assert "mail_5xx" not in out


# ══ [6] 登录耗时离散度 ═════════════════════════════════════════════════


def test_spread_warning_fires_only_above_one_and_a_half_times():
    """阈值边界 1.5×：20.0/10.0 触发，14.0/10.0 不触发。"""
    wide = render([ok("a@x.com", login=10000), ok("b@x.com", login=20000)])
    narrow = render([ok("a@x.com", login=10000), ok("b@x.com", login=14000)])
    assert "极差超过最快值的 1.5 倍" in wide
    assert "极差超过最快值的 1.5 倍" not in narrow


def test_spread_block_needs_at_least_two_samples():
    out = render([ok("a@x.com", login=10000)])
    assert "登录耗时分布" not in out


# ══ [7] 吞吐 ═══════════════════════════════════════════════════════════


def test_throughput_prefers_keys_done_over_the_batch_wall_clock():
    out = render([ok("a@x.com", keys_done=51000, batch_total=58000)], wall=999.9)
    assert "关键路径（全部 key 建出）:   51.0s =  51.0s / 账号" in out
    assert "含末尾统一校验          :   58.0s =  58.0s / 账号" in out
    assert "999.9" not in out, "有 keys_done 时不该退回墙钟"


def test_throughput_falls_back_to_the_wall_clock():
    out = render([ok("a@x.com")], wall=63.4)
    assert "总耗时   63.4s =  63.4s / 账号" in out
    assert "关键路径" not in out


def test_idle_worker_warning_fires_only_when_count_is_not_divisible():
    """判据是 `ceil(count/workers)*workers != count` —— 边界正好是**整除**。"""
    odd = render(
        [ok(f"u{i}@x.com", keys_done=51000, batch_total=58000) for i in range(6)], workers=4
    )
    even = render(
        [ok(f"u{i}@x.com", keys_done=51000, batch_total=58000) for i in range(8)], workers=4
    )
    assert "6 个账号 / 4 路 = 2 轮" in odd
    assert "凑成 8 个能摊得更薄" in odd
    assert "最后一轮有 worker 空转" not in even


# ══ [8] 失败段：配额触顶走结构化字段 ═══════════════════════════════════


def test_quota_text_without_the_quota_kind_is_not_counted():
    """`error` 文本里有 `B0000`、但 `error_kind` 是网络错 ⇒ **不算**配额触顶。

    只用文本匹配的实现会在这里多报 1 个（我们自己拼的守卫文案里也含 `B0000`）。
    """
    out = render(
        [
            rec(
                "a@x.com",
                "failed",
                kind=ERR_NETWORK,
                error="login: B0000 请求频繁（这是引用，不是服务端返回）",
            )
        ]
    )
    assert "注册配额触顶" not in out


def test_quota_kind_without_the_text_is_still_counted():
    """`error_kind=quota` 但文本里没有 `B0000` ⇒ **仍要**算。文本匹配会漏报。"""
    out = render([rec("a@x.com", "failed", kind=ERR_QUOTA, error="register: 服务端拒绝")])
    assert "其中 1 个是注册配额触顶" in out


def test_skipped_records_are_neither_failures_nor_quota_hits():
    out = render([rec("a@x.com", "skipped", kind=ERR_QUOTA, error="guard: B0000")])
    assert not re.search(r"^失败 \d+ 个：", out, re.M)
    assert "注册配额触顶" not in out


def test_error_text_is_truncated_to_90_chars():
    out = render([rec("a@x.com", "failed", error="E" * 200)])
    assert "E" * 90 in out
    assert "E" * 91 not in out


# ══ [9] 尾部 ═══════════════════════════════════════════════════════════


def test_api_usage_block_only_when_something_succeeded():
    with_ok = render([ok("a@x.com", api_key="kk")])
    without = render([rec("a@x.com", "failed", error="boom")])
    assert "调用方式（OpenAI 兼容）" in with_ok
    assert "api_key  = kk" in with_ok
    assert "调用方式" not in without


def test_quota_is_reported_again_at_the_very_end():
    out = render([ok("a@x.com"), rec("b@x.com", "skipped")])
    assert f"本地配额：{QUOTA_DESC}（本次成功 1 个，跳过 1 个）" in out


def test_quota_line_can_be_replaced_by_the_caller():
    """🔴 槽位模式下那行“合计”必须由调用方换掉（不得把合计当上限）。

    默认仍走 `quota.status().describe()` —— 单代理模式下它是对的，
    而 `report.py` **有意**不知道“槽位”这个概念（只依赖注入对象）。
    所以由知道的那一方（`run.py`）把渲染好的整行传进来。
    """
    line = "槽位模式 5 个出口：余 31/35/33/34/38（各自上限 40）⇒ 合计余 171"
    out = render([ok("a@x.com"), rec("b@x.com", "skipped")], quota_line=line)
    assert f"本地配额：{line}（本次成功 1 个，跳过 1 个）" in out
    assert QUOTA_DESC not in out, "传了 quota_line 就不该再印默认描述"


def test_output_is_wrapped_in_72_char_rules():
    out = render([ok("a@x.com")]).splitlines()
    assert out[0] == ""
    assert out[1] == "=" * 72
    assert out[-1] == "=" * 72


def test_empty_batch_prints_header_and_footer_only():
    out = render([])
    assert f"DONE: 0/0 succeeded -> {WRITTEN.resolve()}" in out
    assert "耗时明细（秒）：" in out
    for absent in (
        "── 统计",
        "登录内部阶段",
        "注册内部阶段",
        "登录耗时分布",
        "吞吐",
        "调用方式",
        "失败 ",
    ):
        assert absent not in out, f"空批次不该出现 {absent!r}"


# ══ [10] 金标准：与改前 run.py 的输出逐字节相同 ════════════════════════

_GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _golden_rich():
    """与 `gen_fixture` 生成夹具时完全一致的场景（全分支都打开）。"""
    detail = {
        "goto": 1200,
        "form_ready": 3300,
        "typed": 5200,
        "checkbox": 6100,
        "warmup": 7400,
        "captcha_ready": 17400,
        "submit_jwt": 20300,
    }
    reg_detail = {
        "mailbox": 310,
        "register": 1500,
        "mail_wait": 4200,
        "arrival_delay_ms": 2600,
        "poll_overhead_ms": 1600,
        "activate": 480,
        "mail_polls": 7,
        "mail_5xx": 1,
    }
    return [
        ok(
            "a1@example.com",
            reg=2100,
            login=20300,
            key=7600,
            api_key="sk-AAAA1111",
            login_detail=detail,
            reg_detail=reg_detail,
            captcha="B",
            keys_done=51000,
            batch_total=58000,
        ),
        ok(
            "b2@example.com",
            reg=1900,
            login=39000,
            key=7100,
            api_key="sk-BBBB2222",
            login_detail=detail,
            reg_detail=reg_detail,
            captcha="A",
            keys_done=51000,
            batch_total=58000,
        ),
        ok(
            "c3@example.com",
            reg=2400,
            login=11000,
            key=7300,
            api_key="sk-CCCC3333",
            captcha="A",
            keys_done=51000,
            batch_total=58000,
        ),
        rec(
            "",
            "failed",
            kind=ERR_QUOTA,
            error="register: B0000 请求频繁",
            reg=1600,
            reg_detail=reg_detail,
        ),
        rec(
            "e5@example.com",
            "failed",
            kind=ERR_NETWORK,
            error="login: timeout",
            reg=2000,
            login=9200,
        ),
        rec("f6@example.com", "skipped", reg=1700),
    ]


def _golden_plain():
    return [ok("only@example.com", reg=2000, login=15000, key=7000, api_key="sk-ONLY", captcha="A")]


_GOLDEN_SCENARIOS = {"rich": _golden_rich, "plain": _golden_plain, "empty": list}


@pytest.mark.parametrize("name", ["rich", "plain", "empty"])
def test_output_matches_the_frozen_golden(name):
    """整块 stdout 与**改前 `run.py` 原样跑出来的**输出逐字节相同。

    🔴 基准不是手抄的：`tests/fixtures/report_render_golden.json` 是 2026-09-20
      把当时 `run.py` 第 231..408 行原样 `exec` 出来、用同一套桩跑三个场景的结果。
      动 `render_batch_report` 任何一行都要先确认这是**有意的**改动。

    ⚠ 唯一的中性化是落盘路径（见 `_neutralize`）；它本身也被单独断言，
      免得中性化把「路径没 resolve」这类回归一起吞掉。
    """
    got = render(_GOLDEN_SCENARIOS[name]())
    assert str(WRITTEN.resolve()) in got, "落盘路径必须印 resolve() 之后的值"
    assert _neutralize(got) == _GOLDEN[name]


def test_the_golden_fixture_actually_covers_every_branch():
    """防止夹具退化成一段没内容的输出，让上面那条比对空转（假绿）。"""
    rich = _GOLDEN["rich"]
    for needle in (
        "DONE: 3/6",
        "── 统计（3 个完整样本）",
        report.LATENCY_LABELS,
        "登录内部阶段",
        "注册内部阶段",
        "收信轮询",
        "登录耗时分布",
        "吞吐",
        "失败 2 个",
        "注册配额触顶",
        "调用方式",
        "本地配额",
    ):
        assert needle in rich, f"金标准里少了 {needle!r} —— 它没覆盖到这个分支"
    assert _GOLDEN["empty"].count("\n") < _GOLDEN["plain"].count("\n") < rich.count("\n")
