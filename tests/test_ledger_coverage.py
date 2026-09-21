"""`src/ledger` 的两道「导出快照 vs 权威台账」覆盖护栏。离线，零网络。

覆盖两个**兄弟**判据，触发场景不同：

| 判据 | 管什么 | 谁在用 |
|---|---|---|
| `key_coverage` | 我**核验了多少把 key** | `tools/ops/check_keys_alive.py` |
| `account_coverage` | 我**能从哪个池子里取账号** | `tools/probes/probe_login_only.py` |

为什么值得单独钉住
------------------
两个工具的默认输入都是**某次导出的 CSV 快照**，而它们只报自己的分母：
`check_keys_alive` 报"存活 N/N"、`probe_login_only` 报"6/6 登录成功"。
快照整体过期时，这些数字**看着全绿**，实则完全没覆盖台账。

2026-09-20 实测踩到两处：
  * 快照停在 3 天前、只有 53 把 key，而台账已有 407 把 ⇒ **"53/53 存活"**；
  * 快照全是 5 天前的账号，而台账已有 417 个 ⇒ **"6/6 登录成功"**。

这不是新问题，是**同一个文件里已有护栏的高一层版本**：
`check_keys_alive.py` 早就对"行级静默缩水"（过滤掉非 `sk-` 行）做了报数，
但**文件级缩水没人管** —— 而行级缩水至少分母变了，文件级缩水连分母本身都是错的。

`account_coverage` 那一侧更隐蔽：探针的结论**本身没错**（那 6 个号确实能登录），
错的是**结论的适用范围**被静默限死在一个很小的旧样本上。

覆盖方向是**单向**的
--------------------
`missing` 只表达「台账有、清单没有」。反方向（清单有而台账没有）两个函数都不判 ——
`test_direction_is_one_way` 专门把这个契约钉住，防止后来者把"missing 为空"
误读成"两边一致"。测试链不含 `tools/`，所以这里**不 import 那些工具**：
纯判据在 `src/ledger.py`，本文件测它 + 用静态检查确认接线还在。
"""

from pathlib import Path

import pytest

from src import ledger

# 仓库根 —— 静态接线检查要用（见文件末尾）。
_ROOT = Path(__file__).resolve().parents[1]


# ── 契约：missing 的方向 ────────────────────────────────────────────

def test_fully_covered_means_empty_missing(any_ledger):
    """清单 == 台账的 key 全集 ⇒ 没有遗漏（这是"快照是最新的"这一档）。"""
    keys = {r["api_key"] for r in any_ledger if r.get("api_key")}
    ledger_n, known_n, missing = ledger.key_coverage(any_ledger, keys)
    assert missing == set(), f"清单已含全部台账 key，却报出遗漏 {len(missing)} 把"
    assert ledger_n == known_n, "两边都是全量，计数应当一致"


def test_stale_snapshot_is_detected(any_ledger):
    """🔴 本次要防的真实场景：快照只覆盖了前 3 把。

    断言的是**告警会触发**（`missing` 非空）—— 这正是旧版本做不到的事。
    """
    keys = [r["api_key"] for r in any_ledger if r.get("api_key")]
    if len(keys) < 4:
        pytest.skip("台账里带 key 的记录太少，构不出'快照落后'的场景")

    ledger_n, known_n, missing = ledger.key_coverage(any_ledger, set(keys[:3]))
    assert known_n == 3
    assert len(missing) == ledger_n - 3, (
        f"台账 {ledger_n} 把、快照 3 把 ⇒ 应报出 {ledger_n - 3} 把遗漏，"
        f"实际 {len(missing)}"
    )
    assert missing, "快照远落后于台账，却没有报出任何遗漏 —— 护栏失效"


def test_direction_is_one_way():
    """清单里有、台账里没有的 key，**不得**出现在 missing 里。

    契约：missing = 台账 − 清单。反方向是"台账被覆盖过"的另一个问题，
    本函数刻意不判。别把 missing 为空读成"两边一致"。
    """
    records = [{"email": "a@x", "api_key": "sk-ledger1"}]
    _, known_n, missing = ledger.key_coverage(records, {"sk-ledger1", "sk-orphan"})
    assert known_n == 2, "清单计数应当把孤儿 key 也算进去（它是清单自己的分母）"
    assert missing == set(), f"孤儿 key 不该出现在 missing 里，却出现了 {missing}"


# ── 契约：计数口径 ──────────────────────────────────────────────────

def test_non_prefixed_keys_are_ignored():
    """不以 `sk-` 开头的 `api_key` 不计入 —— 与工具的行级过滤同一套前缀。"""
    records = [
        {"api_key": "sk-good"},
        {"api_key": "pk-other"},       # 前缀不对
        {"api_key": "notakeyatall"},
        {"api_key": "sk-good"},        # 重复：集合去重，计数仍是 1
    ]
    ledger_n, known_n, missing = ledger.key_coverage(records, {"sk-good"})
    assert ledger_n == 1, f"只有一把 sk- key，却数成 {ledger_n}"
    assert known_n == 1
    assert missing == set()


def test_records_without_api_key_are_ignored():
    """缺失 / 空 / None 的 `api_key` 一律不计 —— 它们不是 key，不是"死 key"。"""
    records = [
        {"email": "a@x"},                      # 没有这个字段
        {"email": "b@x", "api_key": ""},       # 空串
        {"email": "c@x", "api_key": None},     # None
        {"email": "d@x", "api_key": "sk-real"},
    ]
    ledger_n, _, _ = ledger.key_coverage(records, set())
    assert ledger_n == 1, f"只有 1 条真 key，却数成 {ledger_n}"


def test_non_dict_records_are_ignored():
    """台账里混进非 dict（手改坏了）不该让统计炸掉。

    `load_existing` 已经过滤过一层，但 `key_coverage` 是公开函数，
    调用方可能直接喂原始 list。
    """
    records = ["oops", 42, None, {"api_key": "sk-ok"}]
    ledger_n, _, _ = ledger.key_coverage(records, set())
    assert ledger_n == 1


def test_empty_known_keys_do_not_inflate_count():
    """清单里的 `None` / `""` 不算 key —— 否则分母会虚高，覆盖率被算得更好看。"""
    records = [{"api_key": "sk-a"}]
    ledger_n, known_n, missing = ledger.key_coverage(records, {None, "", "sk-a"})
    assert known_n == 1, f"空值混进清单计数，known_n={known_n}"
    assert missing == set()


def test_prefix_constant_is_shared_and_conventional():
    """前缀只定义一次，值是 `sk-`。改了它会同时改变过滤与统计口径。"""
    assert ledger.KEY_PREFIX == "sk-"
    # 用常量本身构造输入：前缀一旦被改，这个用例跟着改，不会偷偷不一致
    assert ledger.key_coverage([{"api_key": ledger.KEY_PREFIX + "x"}], set())[0] == 1


# ── 边界 ────────────────────────────────────────────────────────────

def test_empty_inputs():
    """两个输入都空 ⇒ `(0, 0, set())`，且 `missing` 是空集不是 `None`。

    ⚠ 空输入**不是**"数据没问题"的证据 —— 它只说明没有可比的东西。
    调用方看到 `(0, 0, set())` 必须自己判断台账是不是根本没读到。
    """
    assert ledger.key_coverage([], set()) == (0, 0, set())


def test_returns_plain_set_not_falsy_sentinel():
    """返回值类型契约：`missing` 是可做集合运算的 `set`。"""
    _, _, missing = ledger.key_coverage([{"api_key": "sk-a"}], {"sk-b"})
    assert isinstance(missing, set)
    assert missing == {"sk-a"}
    assert "sk-b" not in missing


# ── account_coverage：同一道护栏的另一个字段 ────────────────────────

def test_account_coverage_counts_emails(any_ledger):
    """清单 == 台账的 email 全集 ⇒ 没有遗漏。"""
    emails = {r["email"] for r in any_ledger if r.get("email")}
    ledger_n, known_n, missing = ledger.account_coverage(any_ledger, emails)
    assert missing == set(), f"清单已含全部 email，却报出遗漏 {len(missing)} 个"
    assert ledger_n == known_n


def test_account_coverage_detects_stale_pool(any_ledger):
    """🔴 真实场景：账号池来自一个**很小的旧快照**。

    这是 `probe_login_only.py` 的失效形态 —— 探针会报"6/6 登录成功"，
    而那 6 个号是从快照里取的，够不到台账里新注册的账号。
    结论本身没错，错的是**适用范围**被静默限死。
    """
    emails = [r["email"] for r in any_ledger if r.get("email")]
    if len(emails) < 4:
        pytest.skip("台账里带 email 的记录太少，构不出'池子落后'的场景")

    ledger_n, known_n, missing = ledger.account_coverage(any_ledger, set(emails[:3]))
    assert known_n == 3
    assert len(missing) == ledger_n - 3
    assert missing, "池子远小于台账，却没有报出任何遗漏 —— 护栏失效"


def test_account_coverage_is_independent_of_key_coverage(any_ledger):
    """两个判据**各看各的字段**，不互相顶替。

    台账里存在"有 email 但没有 api_key"的记录（失败/跳过的账号），
    也存在"有 api_key 但 email 为空"的历史记录。
    ⇒ 两者的计数**本来就不该相等**，谁也不能拿另一个的数当自己的。
    """
    key_n = ledger.key_coverage(any_ledger, set())[0]
    acct_n = ledger.account_coverage(any_ledger, set())[0]
    assert key_n > 0 and acct_n > 0
    # 不假设大小关系（两种记录都可能存在），只断言它们**是独立算出来的**：
    # 换一个字段就不该得到同一个数，除非台账恰好两者齐全且一一对应。
    keys = {r["api_key"] for r in any_ledger if r.get("api_key")}
    emails = {r["email"] for r in any_ledger if r.get("email")}
    if len(keys) != len(emails):
        assert key_n != acct_n, "两个字段数量不同却算出同一个数 —— 可能算串了字段"


def test_account_coverage_ignores_empty_emails():
    """空 email 不计入 —— 台账里有"无 email 的配额拦截记录"这类行。"""
    records = [
        {"email": "", "api_key": "sk-a"},
        {"email": None},
        {"note": "没有 email 字段"},
        {"email": "real@x"},
    ]
    ledger_n, known_n, _ = ledger.account_coverage(records, {"real@x", "", None})
    assert ledger_n == 1, f"只有 1 个真 email，却数成 {ledger_n}"
    assert known_n == 1, f"清单侧空值没被丢掉，known_n={known_n}"


def test_account_coverage_empty_inputs():
    assert ledger.account_coverage([], set()) == (0, 0, set())


# ── 接线：护栏真的被工具调用了 ──────────────────────────────────────
#
# ⚠ 只能做静态检查 —— 跑这两个工具都要发真实网络请求，不适合放进测试链。
#   它证明的是"接线还在"，**不是**"告警文案对"：把护栏删掉会红，
#   把文案改丑**不会**红。这是已知盲区，别读成"告警一定在正确时机打印"。

TOOLS = {
    "check_keys_alive": (_ROOT / "tools" / "ops" / "check_keys_alive.py",
                         "ledger.key_coverage(", "key_coverage"),
    "probe_login_only": (_ROOT / "tools" / "probes" / "probe_login_only.py",
                         "ledger.account_coverage(", "account_coverage"),
}


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_actually_wires_the_guard(name):
    path, call, fn_name = TOOLS[name]
    src = path.read_text(encoding="utf-8")
    assert call in src, f"{name} 没调用 {fn_name} —— 文件级护栏被摘了"
    assert "ledger.load_existing(" in src, f"{name} 没读台账 —— 覆盖比对失去参照物"
    # 光"算了覆盖"不够 —— 得**真的告警**。少了这一条，把整个 `if missing:`
    # 告警块删掉（保留计算）本用例仍会绿，而那正是护栏失效的主路径。
    assert "if missing:" in src, f"{name} 算了覆盖却没有告警分支 —— 护栏等于没接"
    # ── 2026-09-21 补：护栏不能只走 stdout ──────────────────────────
    # 上面三条只保证"算了覆盖 + 打了告警"。但告警**只存在于当时的终端输出**里：
    # 事后单独打开 artifact（或由它派生的报告）看到的是"total=53, alive=53"，
    # 连"分母"这个概念都没有。而退出码恒为 0 ⇒ 脚本化调用读到"成功"。
    # 下面四条分别钉住「退出码判据被调用」「放行开关被接线」「artifact 带覆盖块」
    # 「返回值不再是硬编码 0」。删掉任何一处都会让本用例变红。
    assert "ledger.coverage_exit_code(" in src, f"{name} 没接退出码判据"
    assert "allow_partial=args.allow_partial" in src, f"{name} 接了判据却没接放行开关 —— --allow-partial 会静默无效"
    assert '"coverage": coverage' in src, f"{name} 算了覆盖却没写进 artifact —— 事后单独看 artifact 仍是'全绿'"
    assert "return gap_rc" in src, f"{name} 护栏触发了却仍返回固定值 —— 脚本化调用会读到'成功'"


def test_check_keys_alive_shares_the_prefix_constant():
    """前缀只定义一次：行级过滤与覆盖统计各认一套口径会互相掩盖。"""
    src = TOOLS["check_keys_alive"][0].read_text(encoding="utf-8")
    assert "KEY_PREFIX = ledger.KEY_PREFIX" in src


# ── artifact 落点与退出码策略（2026-09-21 补）───────────────────────
#
# 背景：护栏原来**只走 stdout**。事后单独打开 `keys_alive.json` 看到的是
# "total=53, alive=53"，看不出它只覆盖了台账的 53/605 —— artifact 里连
# "分母"这个概念都没有。这是同一类缺陷的第三层：
#   行级缩水（分母变了）→ 文件级缩水（分母本身是错的）→ artifact 级（没有分母）


def test_coverage_block_reports_the_gap():
    blk = ledger.coverage_block(605, 53, {f"sk-{i}" for i in range(552)})
    assert blk["ledger_n"] == 605
    assert blk["known_n"] == 53
    assert blk["uncovered"] == 552
    assert blk["covers_ledger"] is False


def test_coverage_block_full_coverage():
    blk = ledger.coverage_block(53, 53, set())
    assert blk["uncovered"] == 0
    assert blk["covers_ledger"] is True


def test_coverage_block_strips_drive_letters_from_source_paths():
    """来源路径必须相对化 —— 绝对路径带盘符/用户名，拷进仓库就撞泄漏闸门。

    🔴 这是**防回归**不是风格偏好：artifact 在 `.workbuddy-ai/` 下（gitignored），
       但"它不会被提交"守不住，所以直接把形态消掉。
    """
    blk = ledger.coverage_block(
        1, 1, set(),
        ledger=ledger.ROOT / "ledger" / "x.json",
        snapshot=ledger.ROOT / "a" / "b.csv")
    assert blk["ledger"] == "ledger/x.json"
    assert blk["snapshot"] == "a/b.csv"
    assert ":" not in blk["ledger"]


def test_coverage_block_keeps_outside_paths_verbatim():
    """仓库外的路径无法相对化 —— 兜底原样返回。

    ⚠ 有意的：伪造一个假的相对路径，比如实暴露绝对路径更坏（后者至少可查）。
    """
    outside = ledger.ROOT.parent / "definitely-not-in-repo.csv"
    blk = ledger.coverage_block(1, 1, set(), ledger=outside)
    assert blk["ledger"] == str(outside)


def test_coverage_block_handles_missing_sources():
    blk = ledger.coverage_block(1, 1, set())
    assert blk["ledger"] == ""
    assert blk["snapshot"] == ""


def test_coverage_exit_code_is_fatal_by_default():
    """默认**不放行**。

    ⚠ 这是**策略锁**，不是"有分支的判据"：它钉的是"覆盖不足默认让调用方炸"
      这个决定本身。别读成覆盖了复杂逻辑。
    """
    assert ledger.coverage_exit_code({"sk-a"}) == ledger.EXIT_COVERAGE_GAP
    assert ledger.coverage_exit_code({"sk-a"}) != 0


def test_coverage_exit_code_zero_when_fully_covered():
    assert ledger.coverage_exit_code(set()) == 0


def test_coverage_exit_code_allow_partial_opts_out():
    assert ledger.coverage_exit_code({"sk-a"}, allow_partial=True) == 0


def test_exit_coverage_gap_matches_run_py_guard_code():
    """与 `run.py` 的防静默缩水护栏**同码**（那边 `except ValueError` 分支 return 3）。

    ⚠ 钉数值就是钉契约：改它等于改所有调用方的语义。真要改，两边一起改。
    """
    assert ledger.EXIT_COVERAGE_GAP == 3
