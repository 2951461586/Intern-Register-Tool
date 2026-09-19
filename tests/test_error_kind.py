"""`AccountRecord.error_kind` —— 错误结构化字段的**打标点**与**读点**测试。

这个字段存在的唯一理由
----------------------
在这之前，"这个失败是不是出口被封"靠**在 `rec.error` 文本里搜 `B0000`** 判断。
问题在于：**我们自己拼的守卫文案里也含 `B0000`**（是引用，不是服务端返回）。
于是"一个请求都没发的主动中止"会被读成"服务端确认的封禁"。

这个坑已经咬过一次 —— `_note_register_result` 里有一句专门的排除，注释写着
「不排除就会被当成新证据重复计数」。同样的地雷在 `settle_lease` 里还埋着：
那里把文本匹配放在**分支最前面**，一旦踩上，一个**干净出口**会被白晾 120s
（冷却时长差 6 倍：被封 120s vs 偶发故障 20s）。

所以本文件钉住三件事：
  1. **打标点**：每条失败/中止路径都写出正确的 `error_kind`（T1–T6）
  2. **读点**：字段非空即权威，文本只在字段为空时兜底（T7–T10）
  3. **分歧清单**：新旧判据会分歧的形状**只有两条**，且都不可达（T11–T13）

跑法：
    pytest tests/test_error_kind.py -v
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import pipeline
from src.pipeline import (
    ERR_BROWSER,
    ERR_NETWORK,
    ERR_NONE,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    QUOTA_MSG_CODE,
    AccountRecord,
    error_kind_of,
    is_quota_block,
    stage_login_key,
    stage_register,
)
from src.sso import RegisterResult

REPO = Path(__file__).resolve().parents[1]


# ── 夹具：驱动 `stage_register` 的最小假件 ────────────────────────────
# 只实现 `stage_register` 真正会调到的方法，多一个都不给 —— 这样"实现偷偷
# 多调了一个接口"会当场 `AttributeError`，而不是静默走偏。
# `stage_register` 覆盖**注册 + 收信激活**两个阶段，所以成功路径需要邮件假件。


class FakeMsg:
    """一封激活邮件。`received_at` 用真实形态的**毫秒**时间戳。"""

    received_at = 1789449135216

    def find_link(self, *needles):
        return "https://example.com/active?token=t&sign=s"


class FakeMail:
    def __init__(self, exc=None, msg=None):
        self.exc = exc
        self.msg = msg
        self.last_polls = 1
        self.last_http_errors = 0

    def create_mailbox(self, domain=None, count=1):
        if self.exc:
            raise self.exc
        return ["probe@example.com"]

    def wait_for_mail(self, email):
        return self.msg


class FakeSSO:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc

    def check_username(self, username: str) -> bool:
        return True

    def register(self, username: str, email: str, password: str) -> RegisterResult:
        if self.exc:
            raise self.exc
        return self.result

    def activate_from_url(self, url: str) -> bool:
        return True


@pytest.fixture
def quiet(monkeypatch):
    """成功路径会调 `quota.record()` —— 拦掉，别碰真实计数文件。"""
    monkeypatch.setattr(pipeline.quota, "record", lambda *a, **k: None)


def _run(rec, *, mail=None, sso=None, **kw) -> bool:
    """跑一次 `stage_register`，日志静音。"""
    return stage_register(mail or FakeMail(), sso or FakeSSO(RegisterResult(ok=True)),
                          rec, log=lambda _m: None, **kw)


# ══════════════════════════════════════════════════════════════════
# 1. 打标点
# ══════════════════════════════════════════════════════════════════
def test_server_b0000_marks_quota_and_survives_generic_except(quiet):
    """🔴 回归：`if not reg.ok` 里打的标**不能被外层 `except Exception` 冲掉。

    那条路是 `raise RuntimeError(...)`，会被同一个 try 的
    `except Exception` 接到。如果那里写成无条件的 `rec.error_kind = ERR_NETWORK`，
    `ERR_QUOTA` 就没了 —— 而失败计数、出口冷却全都依赖它。
    """
    rec = AccountRecord()
    sso = FakeSSO(RegisterResult(ok=False, msg_code=QUOTA_MSG_CODE, msg="请求频繁"))

    assert _run(rec, sso=sso) is False
    assert rec.status == "failed"
    assert rec.error_kind == ERR_QUOTA
    assert error_kind_of(rec) == ERR_QUOTA
    # 旧的结构化标记（`stages["quota_blocked"]`）必须还在 —— 台账里有人在读它
    assert rec.stages["quota_blocked"] == QUOTA_MSG_CODE
    assert QUOTA_MSG_CODE in rec.error          # 文本也照旧带着


def test_server_rejection_is_not_quota(quiet):
    """服务端明确拒绝（非配额）→ `rejected`，绝不能被算成配额证据。"""
    rec = AccountRecord()
    sso = FakeSSO(RegisterResult(ok=False, msg_code="B0001", msg="用户名已存在"))

    assert _run(rec, sso=sso) is False
    assert rec.error_kind == ERR_REJECTED
    assert error_kind_of(rec) != ERR_QUOTA


def test_network_failure_marks_network(quiet):
    rec = AccountRecord()
    assert _run(rec, mail=FakeMail(exc=TimeoutError("connect timeout"))) is False
    assert rec.status == "failed"
    assert rec.error_kind == ERR_NETWORK


def test_guard_abort_marks_quota_guard_and_is_not_evidence(quiet):
    """🔴 **本字段存在的理由**：守卫中止的文案含 `B0000`，但它不是服务端证据。

    `should_stop=True` 模拟"已确认配额触顶"，此时 `stage_register` 会
    **一个请求都不发**就抛 `_QuotaAbort`。
    """
    rec = AccountRecord()

    assert _run(rec, should_stop=lambda: True) is False
    assert rec.status == "skipped"
    assert rec.error_kind == ERR_QUOTA_GUARD

    # 文本判据说"是配额" —— 这就是地雷本身
    assert QUOTA_MSG_CODE in rec.error
    assert is_quota_block(rec.error) is True
    # 结构化判据说"不是" —— 这就是修掉它的办法
    assert error_kind_of(rec) == ERR_QUOTA_GUARD
    assert error_kind_of(rec) != ERR_QUOTA


def test_success_has_no_error_kind(quiet):
    """走完整条成功路径（注册 → 收信 → 激活）后，字段必须仍是空的。

    这条是"打标只在失败时发生"的守卫 —— 若有人在成功路径上顺手写了个
    `ERR_*`，`error_kind_of()` 就会把成功账号读成失败。
    """
    rec = AccountRecord()
    sso = FakeSSO(RegisterResult(ok=True, sso_uid="12345"))

    assert _run(rec, mail=FakeMail(msg=FakeMsg()), sso=sso) is True
    assert rec.stages["register"] == "ok"
    assert rec.stages["activate"] == "ok"
    assert rec.status == "init"                 # stage_register 不管 status
    assert rec.error == ""
    assert rec.error_kind == ERR_NONE
    assert error_kind_of(rec) == ERR_NONE


def test_activation_failure_is_rejected_not_network(quiet):
    """邮件没到 / 激活链接找不到 / activate 返回 false → `rejected`（服务端行为
    不符合预期），不是 `network`。判据是**抛出点**打的标，不是异常类型。"""
    rec = AccountRecord()
    sso = FakeSSO(RegisterResult(ok=True, sso_uid="12345"))

    assert _run(rec, mail=FakeMail(msg=None), sso=sso) is False
    assert rec.status == "failed"
    assert rec.error_kind == ERR_REJECTED
    assert error_kind_of(rec) != ERR_QUOTA


def test_browser_stage_marks_browser(monkeypatch):
    """登录失败 → `browser`。用假 session 走真实的 `stage_login_key` 分支。"""
    class FakeSession:
        def login(self, email, password, **kw):
            return SimpleNamespace(ok=False, reason="captcha timeout")

    rec = AccountRecord(email="a@example.com", password="pw")
    assert stage_login_key(rec, session=FakeSession(), log=lambda _m: None) is False
    assert rec.status == "failed"
    assert rec.error_kind == ERR_BROWSER
    assert rec.error == "login: captcha timeout"
    assert error_kind_of(rec) == ERR_BROWSER


# ══════════════════════════════════════════════════════════════════
# 2. 读点
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", [ERR_QUOTA, ERR_QUOTA_GUARD, ERR_REJECTED,
                                  ERR_NETWORK, ERR_BROWSER])
def test_nonempty_field_is_authoritative(kind):
    """字段非空 ⇒ 文本完全不参与判断（哪怕文本里全是 `B0000`）。"""
    rec = AccountRecord(error=f"register: {QUOTA_MSG_CODE} 请求频繁", error_kind=kind)
    rec.status = "failed"
    assert error_kind_of(rec) == kind


def test_empty_field_falls_back_to_text():
    """老记录（没有 `error_kind` 字段）→ 文本兜底，行为与改造前一致。"""
    rec = AccountRecord(error=f"register: {QUOTA_MSG_CODE} 请求频繁")
    rec.status = "failed"
    assert rec.error_kind == ""                 # 确认字段真的是空的
    assert error_kind_of(rec) == ERR_QUOTA


def test_skipped_never_yields_quota_evidence_even_without_field():
    """🔴 逻辑而非启发式：中止 = 没发请求 = 不可能有服务端响应。"""
    rec = AccountRecord(
        error=f"quota guard: 已确认 {QUOTA_MSG_CODE}（累计配额触顶），未发注册请求")
    rec.status = "skipped"
    assert rec.error_kind == ""
    assert is_quota_block(rec.error) is True    # 文本说"是"
    assert error_kind_of(rec) != ERR_QUOTA      # 结构化说"不是"


def test_reads_dicts_from_results_json_too():
    """读点必须同时吃 `AccountRecord` 和从台账读回来的 dict。

    `run.py` 走台账那条路时用的是 `json.loads(r.to_json())` —— dict。
    """
    assert error_kind_of({"error_kind": ERR_QUOTA}) == ERR_QUOTA
    assert error_kind_of({"error": f"register: {QUOTA_MSG_CODE}"}) == ERR_QUOTA
    assert error_kind_of({"error": f"register: {QUOTA_MSG_CODE}",
                          "status": "skipped"}) != ERR_QUOTA
    assert error_kind_of({}) == ERR_NONE
    # 非字符串值不能炸（旧台账里字段可能是 null）
    assert error_kind_of({"error_kind": None, "status": None, "error": None}) == ERR_NONE


def test_error_kind_is_serialized_into_ledger_rows():
    """`run.py` 用 `to_json()` 造台账行 ⇒ 新字段必须进 JSON，否则事后查不了。"""
    rec = AccountRecord(email="a@example.com", status="failed",
                        error=f"register: {QUOTA_MSG_CODE} 请求频繁",
                        error_kind=ERR_QUOTA)
    row = json.loads(rec.to_json())
    assert row["error_kind"] == ERR_QUOTA
    assert row["proxy_slot"] == ""              # 同批次加的字段也还在


# ══════════════════════════════════════════════════════════════════
# 3. 分歧清单 —— 新旧判据不一致的形状，以及"它们不可达"的证据
# ══════════════════════════════════════════════════════════════════
# 当前**可达**的记录形状，从 `pipeline.py` 各条失败/中止路径逐条抄下来。
# 元组：(status, error, error_kind, 旧文本判据的结果)
REACHABLE = [
    ("failed", f"register: {QUOTA_MSG_CODE} 请求频繁", ERR_QUOTA, True),
    ("failed", "register: B0001 用户名已存在", ERR_REJECTED, False),
    ("failed", "register: connect timeout", ERR_NETWORK, False),
    ("failed", "register: 槽位池等待超时（6 个槽位，排除 0 个后仍无可用）",
     ERR_NETWORK, False),
    ("skipped", "quota guard: 所有出口配额均已满（6 个槽位里没有一个合格"
                "（被 accept 全部否掉）），未发请求", ERR_QUOTA_GUARD, False),
    ("skipped", "quota guard: 出口 203.0.113.7 本地计数已满"
                "（配额已用尽（40/40；窗口 24h），7.5 分钟后可再注册），未发请求",
     ERR_QUOTA_GUARD, False),
    ("failed", "login: captcha timeout", ERR_BROWSER, False),
    ("failed", "key: create_key failed", ERR_BROWSER, False),
]


@pytest.mark.parametrize("status,error,kind,old", REACHABLE)
def test_new_predicate_agrees_with_old_on_reachable_shapes(status, error, kind, old):
    """🔴 差分等价：**当前可达**的每一种形状上，新旧判据结论必须一致。

    这条是"改判据没改行为"的证据。它不靠读代码保证 —— 形状是从源码抄下来的，
    断言是跑出来的。
    """
    rec = AccountRecord(status=status, error=error, error_kind=kind)
    assert is_quota_block(rec.error) is old, "旧文本判据的期望值写错了"
    assert (error_kind_of(rec) == ERR_QUOTA) is old, "新旧判据结论不一致"


# 这**两条**是唯一会分歧的形状。共同点：`status == "skipped"` 且 error 文本含
# `B0000` —— 都是我们自己拼的守卫文案，不是服务端返回。
DIVERGENT = [
    f"quota guard: 已确认 {QUOTA_MSG_CODE}（累计配额触顶），未发注册请求",
    f"quota guard: 前序账号已触发 {QUOTA_MSG_CODE}，跳过投递（未发请求）",
]


@pytest.mark.parametrize("text", DIVERGENT)
def test_divergent_shapes_are_exactly_these_two(text):
    rec = AccountRecord(status="skipped", error=text, error_kind=ERR_QUOTA_GUARD)
    assert is_quota_block(rec.error) is True        # 旧判据：是封禁
    assert error_kind_of(rec) != ERR_QUOTA          # 新判据：不是证据


# ⚠ "这两条分歧形状**不可达**（因而改动是零行为变化）"的证明在
#   `tests/test_quota_governor.py::test_divergent_shapes_cannot_reach_settle_lease`
#   —— 它需要 `QuotaGovernor` 的行为，放在那边更顺。


def _calls_to(src_text: str, fname: str) -> list[list[str]]:
    """AST 找出源码里所有对 `fname` 的调用，返回每个调用的实参源码片段。"""
    out = []
    for node in ast.walk(ast.parse(src_text)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Name) and fn.id == fname) or \
           (isinstance(fn, ast.Attribute) and fn.attr == fname):
            out.append([ast.unparse(a) for a in node.args])
    return out


def _literal_lines(src_text: str, value: str) -> list[int]:
    """AST 找出 `value` 作为**字符串字面量**出现的行号（含定义那一行）。"""
    return [n.lineno for n in ast.walk(ast.parse(src_text))
            if isinstance(n, ast.Constant) and n.value == value]


def test_no_record_error_is_judged_by_text_anymore():
    """结构性守卫：记录级的判据不许再走文本匹配。

    `is_quota_block()` 本身保留（探针要判服务端原始响应文本），
    但不许再拿 `rec.error` / `r.error` 去喂它，也不许再出现裸的 `"B0000"` 字面量。

    🔴 这条测试**必须走 AST，不能走文本包含**：`settle_lease` 的 docstring
    里正大光明地写着 `is_quota_block(rec.error)` 这几个字（用来解释**为什么
    不能那么写**）。用 `in src` 判断会把这段说明当成违规 —— 本项目已经踩过
    一次同形的坑（"工具分不清代码与描述代码的文本"）。
    """
    for rel in ("src/pipeline.py", "run.py"):
        src = (REPO / rel).read_text(encoding="utf-8")

        for args in _calls_to(src, "is_quota_block"):
            for a in args:
                assert not a.endswith(".error"), \
                    f"{rel} 里用记录级 error 文本喂 is_quota_block：{a}"

        for lineno in _literal_lines(src, "B0000"):
            line = src.splitlines()[lineno - 1].strip()
            assert line.startswith("QUOTA_MSG_CODE ="), \
                f"{rel}:{lineno} 出现裸的 B0000 字面量，应改用 QUOTA_MSG_CODE：{line}"
