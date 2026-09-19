"""`QuotaGovernor` 与 `settle_lease` 的**差分等价测试**。

为什么用差分而不是"照着重写一遍断言"
--------------------------------------
`#13` 这次改造的验收标准是「**不改变任何判据**，只是把决策集中」。
"我读了一遍觉得一样"不是证据 —— 所以本文件把改造前从 `run_batch` 里
**逐字抄下来**的三段内联逻辑（`_ref_*` 系列，取自 `git show HEAD:src/pipeline.py`）
放在这里当**参考实现**，然后对同一批输入分别跑"旧逻辑"和"新代码"，断言结论一致。

这样做的价值：如果将来有人"顺手优化"了 governor 里的某个判据，
`_ref_*` 不会跟着变，测试当场变红。

⚠ 参考实现里保留的是**旧判据**（`is_quota_block(rec.error)` 文本匹配），
不是新判据 —— 这正是要对比的东西。所以 `_ref_*` 看起来"过时"是对的，
**不要**去"修"它。

⚠ 本文件**不全是**差分用例：`2b` 那组（`test_ignore_quota_*`）断言的是
**新契约**（`--ignore-quota` 必须覆盖全部检查点）。理由写在那一组的注释里 ——
简言之，`_ref_check_slot` / `_ref_claim_slot` 本身就没读开关，拿它们当参照物
只会把洞固化成"预期行为"。

跑法：
    pytest tests/test_quota_governor.py -v
"""

import ast
import threading
from pathlib import Path

import pytest

from src import config, quota
from src.pipeline import (
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    QUOTA_MSG_CODE,
    QUOTA_STREAK_STOP,
    AccountRecord,
    QuotaGovernor,
    is_quota_block,
    settle_lease,
)

REPO = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════════
# 参考实现 —— 从 `git show HEAD:src/pipeline.py` 逐字抄下来的旧逻辑
# ══════════════════════════════════════════════════════════════════
def _ref_allow(count, *, pool, ignore_quota, logs):
    """旧「开跑前」块（原 `run_batch` 第 504–517 行）。

    唯一改动：`print(..., flush=True)` → `logs.append(...)`，文本逐字未动。
    """
    if not ignore_quota and pool is None:
        st = quota.check_or_raise(planned=count, allow_partial=True)
        if st.used + count > st.limit:
            keep = st.remaining
            logs.append(f"⚠ 本地配额保护：计划注册 {count} 个，但{st.describe()} "
                        f"—— 本次只跑 {keep} 个。\n"
                        f"  想全跑：调大 IR_REG_QUOTA_MAX，或加 --ignore-quota"
                        f"（先确认服务端确实已恢复）。")
            count = keep
    elif not ignore_quota and pool is not None:
        logs.append(f"ℹ 槽位模式：跳过全局配额守卫（本地计数 {quota.status().describe()} "
                    f"是**老出口**的，与新槽位无关）——\n"
                    f"  改按槽位分别计数，每个出口各自独立。")
    return count


def _ref_check_slot(index, *, pool):
    """旧 `_slot_has_quota` 闭包（原 `producer` 内）。

    ⚠ **刻意没有 `ignore` 参数** —— 旧逻辑就没读 `--ignore-quota`，这是历史
    遗留的洞（对比 `_ref_allow` 是有的）。不要"补"上去：它是参照物，
    补了就再也照不出这个差异。新契约由 2b 那组用例单独断言。
    """
    try:
        return not quota.status(
            scope=config.slot_scope(pool.url_of(index))).exhausted
    except ValueError:
        return False


def _ref_claim_slot(scope, logs):
    """旧「拿到租约后复查」块（原 `producer` 内）。

    ⚠ 同上：**刻意没有 `ignore`**。
    """
    st = quota.status(scope=scope)
    if st.exhausted:
        logs.append(f"跳过（出口 {scope} 配额保护：{st.describe()}）")
        return (f"quota guard: 出口 {scope} 本地计数已满"
                f"（{st.describe()}），未发请求")
    return ""


class _RefSentinel:
    """旧 `_note_register_result` 闭包（原 `run_batch` 内）的原样抄写。

    闭包状态 `quota_hit` / `_quota_streak` / `_quota_lock` 变成实例属性，
    其余一字未动 —— 尤其那句 `elif is_quota_block(rec.error)` 的**文本**判据。
    """

    def __init__(self, pool):
        self.pool = pool
        self.hit = threading.Event()
        self.streak = 0
        self.lock = threading.Lock()

    def note(self, ok: bool, rec) -> None:
        with self.lock:
            if self.pool is not None:
                if ok:
                    self.streak = 0
                return
            if ok:
                self.streak = 0
            elif rec.status == "skipped":
                pass
            elif is_quota_block(rec.error):
                self.streak += 1
                if self.streak >= QUOTA_STREAK_STOP:
                    self.hit.set()


def _ref_settle(pool, lease, ok: bool, rec) -> None:
    """旧 `_settle_lease` 闭包的原样抄写（判据是**文本匹配**）。"""
    if pool is None or lease is None:
        return
    if is_quota_block(rec.error):
        pool.report_banned(lease, f"{QUOTA_MSG_CODE} @ {rec.email}")
    elif ok:
        pool.release(lease)
    elif rec.status == "skipped":
        pool.release(lease)
    else:
        pool.report_failed(lease, rec.error[:80])


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════
@pytest.fixture
def env(tmp_path, monkeypatch):
    """把配额计数重定向到临时文件，并把上限压小让触顶容易构造。"""
    state = tmp_path / "register_quota.jsonl"
    monkeypatch.setattr(quota, "state_path", lambda: state)
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 5)
    return state


class FakePool:
    """只实现被用到的两个方法：`url_of` 和记录动作。"""

    def __init__(self, urls):
        self._urls = urls
        self.actions = []

    def url_of(self, slot: int) -> str:
        return self._urls[slot - 1]

    # ── `settle_lease` 会调的三个 ──
    def report_banned(self, lease, why):
        self.actions.append(("banned", str(lease), why))

    def release(self, lease):
        self.actions.append(("release", str(lease), ""))

    def report_failed(self, lease, why):
        self.actions.append(("failed", str(lease), why))


SLOTS = ["http://127.0.0.1:17911", "http://127.0.0.1:17912"]


@pytest.fixture
def slot_env(monkeypatch):
    """给两个槽位端口登记出口 IP，并让上限小到一两个账号就满。"""
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"17911": "203.0.113.11",
                                                    "17912": "203.0.113.12"})
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 2)
    return config


# ══════════════════════════════════════════════════════════════════
# 1. allow() —— 开跑前裁剪
# ══════════════════════════════════════════════════════════════════
# (已有记录数, 计划量, 是否池模式, ignore_quota, 说明)
ALLOW_CASES = [
    (0, 3, False, False, "余量充足，不裁"),
    (3, 3, False, False, "差 1 个额度 → 裁到 2"),
    (4, 3, False, False, "只剩 1 个额度 → 裁到 1"),
    (5, 3, False, False, "已触顶 → 两边都应抛 QuotaExceeded"),
    (0, 3, True, False, "池模式 → 跳过全局守卫（不裁、不抛）"),
    (5, 3, True, False, "池模式 + 全局已满 → 仍然放行（全局计数是老出口的）"),
    (5, 3, False, True, "--ignore-quota → 不裁、不抛、不打印"),
    (0, 3, True, True, "--ignore-quota + 池模式 → 什么都不做"),
]


@pytest.mark.parametrize("used,count,is_pool,ignore,why", ALLOW_CASES)
def test_allow_matches_reference(env, used, count, is_pool, ignore, why):
    for i in range(used):
        quota.record(f"u{i}@example.com", scope="")

    old_logs: list[str] = []
    old_pool = object() if is_pool else None
    try:
        old = _ref_allow(count, pool=old_pool, ignore_quota=ignore, logs=old_logs)
        old_exc = None
    except quota.QuotaExceeded as ex:
        old, old_exc = None, type(ex)

    new_logs: list[str] = []
    gov = QuotaGovernor(pool=old_pool, ignore=ignore, log=new_logs.append)
    try:
        new = gov.allow(count)
        new_exc = None
    except quota.QuotaExceeded as ex:
        new, new_exc = None, type(ex)

    assert new_exc is old_exc, f"[{why}] 异常类型不一致：{old_exc} vs {new_exc}"
    if old_exc is None:
        assert new == old, f"[{why}] 返回值不一致：{old} vs {new}"
    assert new_logs == old_logs, f"[{why}] 打印文本不一致：\n{old_logs}\n{new_logs}"


# ══════════════════════════════════════════════════════════════════
# 2. check_slot() / claim_slot() —— 拿租约前后
# ══════════════════════════════════════════════════════════════════
def test_check_slot_matches_reference(slot_env, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    pool = FakePool(SLOTS)
    gov = QuotaGovernor(pool=pool)

    # 空池子：两个出口都有额度
    assert [gov.check_slot(i) for i in (1, 2)] == \
           [_ref_check_slot(i, pool=pool) for i in (1, 2)]

    # 把 17911 的出口记满
    quota.record("a@example.com", scope="203.0.113.11")
    quota.record("b@example.com", scope="203.0.113.11")
    assert [gov.check_slot(i) for i in (1, 2)] == \
           [_ref_check_slot(i, pool=pool) for i in (1, 2)]
    assert gov.check_slot(1) is False, "记满的出口必须被 accept 否掉"
    assert gov.check_slot(2) is True, "另一个出口不该被拖累"


def test_check_slot_rejects_unmapped_port(slot_env, tmp_path, monkeypatch):
    """端口不在 `SLOT_EGRESS_IPS` 里 → 两边都必须 `False`（不敢用）。

    判据是 `config.slot_scope()` 抛的 `ValueError`，两边捕获点必须一致 ——
    `pool.url_of()` 也在 try 里，别把它挪出去。
    """
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    pool = FakePool(["http://127.0.0.1:19999"])       # 没登记
    gov = QuotaGovernor(pool=pool)

    assert _ref_check_slot(1, pool=pool) is False
    assert gov.check_slot(1) is False


def test_claim_slot_matches_reference(slot_env, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    gov = QuotaGovernor(pool=object())

    for scope, n in (("203.0.113.11", 0), ("203.0.113.11", 2), ("203.0.113.12", 1)):
        for _ in range(n):
            quota.record("x@example.com", scope=scope)
        old_logs: list[str] = []
        old = _ref_claim_slot(scope, old_logs)
        new_logs: list[str] = []
        new = gov.claim_slot(scope, new_logs.append)
        assert new == old, f"scope={scope} 跳过原因不一致"
        assert new_logs == old_logs, f"scope={scope} 日志不一致"


# ══════════════════════════════════════════════════════════════════
# 2b. `--ignore-quota` 的覆盖面（2026-09-20 补）
# ══════════════════════════════════════════════════════════════════
# 🔴 为什么不并进上面的差分用例：差分是拿**改造前的内联逻辑**当参照物，
#    而 `_ref_check_slot` / `_ref_claim_slot` 里**本来就没有** ignore 分支
#    —— 那个漏是历史遗留（`_ref_allow` 有 ignore 参数，这两个没有），
#    不是 #13 重构引入的回归。所以下面断言的是**新契约**，不是"与旧逻辑一致"；
#    拿旧逻辑当参照物只会把这个洞固化成"预期行为"。
#    非 ignore 档的等价性仍由上面两个差分用例保证（它们传的是默认 ignore=False）。
#
# 背景（2026-09-20 实测）：槽位模式下 `--ignore-quota` 完全失效 —— 50 个任务
# 0 个成功，全部 `skipped`，报错是"所有出口配额均已满（4 个槽位里没有一个合格
# （被 accept 全部否掉））"。看着像池子满了，实际是 `check_slot` 没读开关。

IGNORE_SCOPE = "203.0.113.11"


def _exhaust(scope: str) -> None:
    """把某个出口的 scope 记到本地上限（`slot_env` 把上限压到了 2）。"""
    for _ in range(config.REG_QUOTA_MAX):
        quota.record("x@example.com", scope=scope)


def test_ignore_quota_makes_check_slot_accept_exhausted_slot(slot_env, tmp_path,
                                                             monkeypatch):
    """开关打开时，记满的出口也必须被 `accept` 放行。

    🔴 这就是那次 0/50 的直接复现：`check_slot` 是 `accept` 谓词，一路
    `False` → `acquire()` 抛 `NoEligibleSlot` → 整批 skipped。
    """
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    pool = FakePool(SLOTS)
    _exhaust(IGNORE_SCOPE)

    assert _ref_check_slot(1, pool=pool) is False, \
        "参照物没有 ignore 分支 —— 这正是历史遗留的洞（别去修它）"
    assert QuotaGovernor(pool=pool).check_slot(1) is False, "关着时必须照拦"
    assert QuotaGovernor(pool=pool, ignore=True).check_slot(1) is True, \
        "--ignore-quota 开着还被拦 = 开关失效"
    assert QuotaGovernor(pool=pool, ignore=True).check_slot(2) is True


def test_ignore_quota_does_not_bypass_unknown_egress(slot_env, tmp_path,
                                                     monkeypatch):
    """🔴 边界：`--ignore-quota` **不**放开"端口没登记出口 IP"那一支。

    两者拦的根本不是一回事：一个拦"额度用完了"（保守估计，可以不信），
    一个拦"不知道该把额度记到谁头上"（放行 = 某个真实出口**静默**超限，
    事后查不出来）。所以开关只覆盖前者。
    """
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    pool = FakePool(["http://127.0.0.1:19999"])          # 没登记出口 IP

    assert QuotaGovernor(pool=pool).check_slot(1) is False
    assert QuotaGovernor(pool=pool, ignore=True).check_slot(1) is False, \
        "账目错乱不能靠开关绕过"


def test_ignore_quota_makes_claim_slot_pass_and_stay_silent(slot_env, tmp_path,
                                                            monkeypatch):
    """`claim_slot` 在开关打开时必须返回 `""`（放行）且**不打印**跳过日志。

    ⚠ 只补 `check_slot` 不够：那一处管"能不能拿到租约"，这一处管"拿到之后
    放不放行"。漏掉这里会变成"租约照拿、请求不发"，白占一个出口。
    """
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    _exhaust(IGNORE_SCOPE)

    on_logs: list[str] = []
    gov_on = QuotaGovernor(pool=object(), ignore=True)
    assert gov_on.claim_slot(IGNORE_SCOPE, on_logs.append) == ""
    assert on_logs == [], f"开关打开时不该打印跳过日志：{on_logs}"

    # 关掉开关仍然拦 —— 保证 `ignore` 是唯一变量，不是判据被改坏了。
    off_logs: list[str] = []
    gov_off = QuotaGovernor(pool=object())
    assert gov_off.claim_slot(IGNORE_SCOPE, off_logs.append) != ""
    assert len(off_logs) == 1, f"关着时应当且只当打印一条：{off_logs}"


@pytest.mark.parametrize("mode", ["non_pool", "slot"])
def test_ignore_quota_covers_all_checkpoints(slot_env, tmp_path, monkeypatch,
                                             mode):
    """🔴 回归护栏：开关必须覆盖**全部**检查点，一处漏掉这条就红。

    形态是刻意选的 —— 不逐点罗列，而是问一个端到端的问题：
    「配额全部用光 + 开关打开时，还有任何一处会拦我吗？」
    2026-09-20 之前它会失败（`check_slot` 拦），而当时三个检查点各自的用例
    都不存在，所以洞一直没人发现。新增检查点时应把它加进这里。
    """
    monkeypatch.setattr(quota, "state_path", lambda: tmp_path / "q.jsonl")
    pool = FakePool(SLOTS) if mode == "slot" else None
    gov = QuotaGovernor(pool=pool, ignore=True, log=lambda _m: None)

    for scope in ("203.0.113.11", "203.0.113.12"):
        _exhaust(scope)

    # ① 开跑前
    assert gov.allow(3) == 3, "开跑前被裁"
    # ② 拿租约前 + ③ 拿到租约后
    if pool is not None:
        assert all(gov.check_slot(i) for i in (1, 2)), "拿租约前被否掉"
        for scope in ("203.0.113.11", "203.0.113.12"):
            assert gov.claim_slot(scope, lambda _m: None) == "", "拿到租约后被拦"


# ══════════════════════════════════════════════════════════════════
# 3. note_result() —— 运行中哨兵
# ══════════════════════════════════════════════════════════════════
# 一串注册结果事件：(ok, status, error, error_kind)
EVENTS = [
    (False, "failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA),
    (False, "failed", "register: B0001 用户名已存在", ERR_REJECTED),
    (True, "", "", ""),
    (False, "failed", "register: connect timeout", ERR_NETWORK),
    (False, "skipped", f"quota guard: 已确认 {QUOTA_MSG_CODE}（累计配额触顶），未发注册请求",
     ERR_QUOTA_GUARD),
    (False, "failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA),
    (False, "failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA),
    (False, "failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA),
]


@pytest.mark.parametrize("is_pool", [False, True])
def test_note_result_matches_reference(is_pool):
    """🔴 差分：逐事件对比"连续计数"与"是否置位"。

    注意池模式那一档也**必须**一致 —— 旧逻辑在池模式下只做 `if ok: streak = 0`
    然后 `return`，从不累加、从不置位。这是"`B0000` 是出口维度信号"那条设计的
    实现形态，改坏了会让整批在第一个 `B0000` 就停。
    """
    pool = object() if is_pool else None
    ref = _RefSentinel(pool)
    gov = QuotaGovernor(pool=pool, log=lambda _m: None)

    for i, (ok, status, error, kind) in enumerate(EVENTS):
        rec = AccountRecord(status=status, error=error, error_kind=kind)
        ref.note(ok, rec)
        gov.note_result(ok, rec)
        assert gov.streak() == ref.streak, f"第 {i} 个事件后连续计数不一致"
        assert gov.hit() == ref.hit.is_set(), f"第 {i} 个事件后置位状态不一致"


def test_pool_mode_never_sets_the_stop_flag():
    """🔴 行为测试（替代原来的结构性守卫）：池模式下见再多 `B0000` 也不停。

    自我保护在池模式下由**槽位冷却**承担（被封出口 120s 内不再分配），
    不是"整批停" —— 因为 `B0000` 只封那个出口，别的出口还有额度。
    """
    gov = QuotaGovernor(pool=object(), log=lambda _m: None)
    rec = AccountRecord(status="failed", error=f"register: {QUOTA_MSG_CODE} 请求频繁",
                        error_kind=ERR_QUOTA)

    for _ in range(QUOTA_STREAK_STOP * 3):
        gov.note_result(False, rec)

    assert gov.hit() is False, "池模式下不该置位整批停止"
    assert gov.streak() == 0, "池模式下不该累加连续计数"


def test_non_pool_mode_stops_at_the_threshold():
    """非池模式：第 `QUOTA_STREAK_STOP` 个才置位，不多不少。"""
    gov = QuotaGovernor(pool=None, log=lambda _m: None)
    rec = AccountRecord(status="failed", error=f"register: {QUOTA_MSG_CODE} 请求频繁",
                        error_kind=ERR_QUOTA)

    for _ in range(QUOTA_STREAK_STOP - 1):
        gov.note_result(False, rec)
        assert gov.hit() is False

    gov.note_result(False, rec)
    assert gov.hit() is True
    assert gov.streak() == QUOTA_STREAK_STOP


def test_unrelated_failure_does_not_reset_the_streak():
    """一个无关失败不能把已确认的配额信号抹掉（旧逻辑就是这么写的）。"""
    gov = QuotaGovernor(pool=None, log=lambda _m: None)
    quota_rec = AccountRecord(status="failed", error=f"register: {QUOTA_MSG_CODE}",
                              error_kind=ERR_QUOTA)
    other = AccountRecord(status="failed", error="register: connect timeout",
                          error_kind=ERR_NETWORK)

    gov.note_result(False, quota_rec)
    gov.note_result(False, other)
    assert gov.streak() == 1, "无关失败不该清零，也不该累加"

    gov.note_result(False, quota_rec)
    assert gov.hit() is True, "第 2 个配额信号应能置位"


def test_success_resets_the_streak():
    gov = QuotaGovernor(pool=None, log=lambda _m: None)
    quota_rec = AccountRecord(status="failed", error=f"register: {QUOTA_MSG_CODE}",
                              error_kind=ERR_QUOTA)

    gov.note_result(False, quota_rec)
    gov.note_result(True, AccountRecord(status="success"))
    assert gov.streak() == 0
    assert gov.hit() is False


# ══════════════════════════════════════════════════════════════════
# 4. settle_lease() —— 租约归还（新判据 vs 旧判据）
# ══════════════════════════════════════════════════════════════════
# 当前**可达**的记录形状：(status, error, error_kind, ok)
REACHABLE_RESULTS = [
    ("failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA, False),
    ("failed", "register: B0001 用户名已存在", ERR_REJECTED, False),
    ("failed", "register: connect timeout", ERR_NETWORK, False),
    ("failed", "activate: activation link not found in mail", ERR_REJECTED, False),
    ("skipped", "quota guard: 所有出口配额均已满（…被 accept 全部否掉），未发请求",
     ERR_QUOTA_GUARD, False),
    ("skipped", "quota guard: 出口 203.0.113.11 本地计数已满"
                "（配额已用尽（2/2；窗口 24h），7.5 分钟后可再注册），未发请求",
     ERR_QUOTA_GUARD, False),
    ("init", "", "", True),                       # 注册成功（status 仍是 init）
    ("success", "", "", True),
]


@pytest.mark.parametrize("status,error,kind,ok", REACHABLE_RESULTS)
def test_settle_lease_matches_reference(status, error, kind, ok):
    """🔴 差分：可达形状上，新判据与旧判据的**动作序列**必须逐字一致。"""
    rec = AccountRecord(email="a@example.com", status=status, error=error,
                        error_kind=kind)

    old_pool = FakePool(SLOTS)
    _ref_settle(old_pool, "slot1", ok, rec)

    new_pool = FakePool(SLOTS)
    settle_lease(new_pool, "slot1", ok, rec)

    assert new_pool.actions == old_pool.actions, \
        f"动作不一致：旧={old_pool.actions} 新={new_pool.actions}"


@pytest.mark.parametrize("lease,pool", [(None, FakePool(SLOTS)), ("slot1", None)])
def test_settle_lease_is_a_noop_without_pool_or_lease(lease, pool):
    rec = AccountRecord(status="failed", error=f"register: {QUOTA_MSG_CODE}",
                        error_kind=ERR_QUOTA)
    settle_lease(pool, lease, False, rec)
    if pool is not None:
        assert pool.actions == []


def test_settle_lease_diverges_on_the_two_guard_texts():
    """⚠ **已知分歧**：两条守卫文案（`skipped` + 文本含 `B0000`）新旧判据不一致。

    旧判据（文本匹配）会 `report_banned` —— 把一个**一个请求都没发**的干净
    出口按 120s 长冷却晾起来；新判据走 `status == "skipped"` → `release`。

    这条测试**刻意钉住分歧**，不是为了让它过，而是为了让"分歧只有这两条"
    这个事实可查。它们为什么不可达见下一条测试。
    """
    for text in (f"quota guard: 已确认 {QUOTA_MSG_CODE}（累计配额触顶），未发注册请求",
                 f"quota guard: 前序账号已触发 {QUOTA_MSG_CODE}，跳过投递（未发请求）"):
        rec = AccountRecord(email="a@example.com", status="skipped", error=text,
                            error_kind=ERR_QUOTA_GUARD)

        old_pool = FakePool(SLOTS)
        _ref_settle(old_pool, "slot1", False, rec)
        new_pool = FakePool(SLOTS)
        settle_lease(new_pool, "slot1", False, rec)

        assert old_pool.actions[0][0] == "banned", "旧判据确实会误封干净出口"
        assert new_pool.actions[0][0] == "release", "新判据会正确释放"
        assert new_pool.actions != old_pool.actions


def test_divergent_shapes_cannot_reach_settle_lease():
    """🔴 证明上一条的分歧**不可达**，所以整个改造是零行为变化。

    推理链（每一步都在本文件里有测试）：
      ① 两条分歧形状都在 `gov.hit()` 为真时才产生（`producer` 开头那个 `if`）；
      ② `hit()` 只在**非池模式**置位（`test_pool_mode_never_sets_the_stop_flag`）；
      ③ 非池模式 ⇒ `pool is None` ⇒ `settle_lease` 首行早退。

    所以"干净出口被误封"这条路在当前结构下走不通。这里只把 ③ 钉住 ——
    ①② 由上面两条行为测试覆盖。

    🔴 用 AST 而不是文本查找：`settle_lease` 的 docstring 里也写着
    `error_kind_of()` 和 `is_quota_block(rec.error)`（用来解释为什么那么写），
    按文本找位置会命中说明文字而不是代码。
    """
    tree = ast.parse((REPO / "src" / "pipeline.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "settle_lease")

    first = _body_without_docstring(fn)[0]
    assert isinstance(first, ast.If), "第一条语句必须是非池/无租约的早退"
    assert ast.unparse(first.test) == "pool is None or lease is None"
    assert isinstance(first.body[0], ast.Return), "早退分支必须是裸 return"


def _body_without_docstring(fn) -> list:
    """函数体去掉开头的 docstring（它是 `Expr(Constant)`，会占掉 `body[0]`）。"""
    stmts = fn.body
    if stmts and isinstance(stmts[0], ast.Expr) \
            and isinstance(stmts[0].value, ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        return stmts[1:]
    return stmts
