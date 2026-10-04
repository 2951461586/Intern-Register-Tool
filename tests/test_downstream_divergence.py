"""两份下游实现的**分歧契约** —— 把"它们为什么不一样"变成可执行断言。

读源
----
  A = `src/pipeline.py:stage_login_key`（批量场景的校验延后到 `verify_keys`）
  B = `tools/run_downstream.py:run_one`
  逐字段 ceiling 表 + "为什么不能合并"的结论：`docs/audit-2026-09-20.md` §2.2「B7a」。

为什么不合并就必须有测试
------------------------
审计 §0 第 2 行的风险不是"重复"，是**修一处漏一处**。而 A/B 的 docstring
在 2026-09-20 之前**互不知晓对方存在** —— 改一边的人不会知道还有另一边。
"人记得保持两边一致"不是护栏（审计 §10 第 10 条），所以这里把三件事变成断言：

  1. **共享前缀**：给定同一个假 `dc`，两边的前四步调用序列**逐项相同**；
  2. **共享门控**：`claim_free_grant` 两边都**只在** `has_received` 为假时调；
  3. **文档写明的分歧**：A **不调** `list_keys`（B 调）、A 的校验**不含** `chat`
     （B 含）。⇒ 任何一边被"顺手改成和另一边一样"都会当场红。

⚠ 刻意**不测**"哪边落哪些字段"：那是 ceiling 表的内容，而 B 的字段面本来就更大，
  字段增减是允许的。测的是**调用序列**与**门控** —— 它们一分叉，
  两边的行为就真的开始漂了。

跑法：
    pytest tests/test_downstream_divergence.py -v
"""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

# 🔴 三个都在**模块级**导入：本文件一旦在运行期 import 某个 `src/` 模块，
#    它的模块级 import 就进了测试链，而 `tests/test_dependency_surface.py`
#    只扫**模块级** import —— 写进函数体等于给自己开一个无人看守的缺口。
#    （`src/browser/` 的 playwright 是函数体内延迟 import，所以这三行是安全的；
#    这一点由上面那个元测试持续保证。）
import src.apikey as apikey
import src.browser as browser
import src.discovery as discovery
from src import pipeline

REPO = Path(__file__).resolve().parents[1]

# 两边**共有**的只读前缀（`has_received=False` 时）。**顺序也是契约**。
SHARED_PREFIX = ["get_user_info", "free_grant_status", "claim_free_grant", "balance"]


# ── 假件 ────────────────────────────────────────────────────────────
class _Spy:
    """一次运行的调用账本。"""

    def __init__(self):
        self.dc_calls = []  # `dc.<method>()` 的名字，按调用顺序
        self.dc_ctor = []  # `DiscoveryClient(**kw)` 的 kw
        self.ak_calls = []  # `apikey.<fn>()` 的名字，按调用顺序


def _login_result():
    """两边都只需要这几个属性（A 走 `session.login()`，B 走 `BrowserSession.login()`）。"""
    return SimpleNamespace(
        ok=True,
        jwt="jwt-xyz",
        cookies={"sid": "c"},
        timings={"total": 1},
        captcha_stage={"path": "A"},
        reason="",
    )


class _FakeSession:
    """同时充当 A 的 `session=` 与 B 的 `BrowserSession`：两边都只要
    `.login()` / `.launch_ms` / 上下文管理器协议。"""

    launch_ms = 42

    def __init__(self, headless=True, *, settings=None):
        # 阶段 A 起 `BrowserSession` 还收 `settings`（注入面）。假件不关心配置，
        # 但**必须**接受它 —— 否则 pipeline 那边一调用就 TypeError。
        self.headless = headless
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, email, password, **kw):
        return _login_result()


def _install(monkeypatch, spy, *, has_received):
    """把假件装到**两边各自解析符号的位置**上。

    ⚠ 这不是冗余：A 在**模块级** `from .discovery import DiscoveryClient`
      （⇒ 要补 `src.pipeline` 的全局），B 在**函数体内**
      `from src.discovery import DiscoveryClient`（⇒ 要补源模块）。
      只补一处的话另一边会去连真网络 —— 而测试会以"调用序列为空"的形式
      **静默变绿**（空 == 期望前缀？不，会红；但若是另一方向就会假绿）。
    """

    class _FakeDiscovery:
        """只实现两边**真的会调到**的方法 —— 多调一个就 `AttributeError`，
        不静默走偏。"""

        def __init__(self, **kw):
            spy.dc_ctor.append(kw)

        def get_user_info(self):
            spy.dc_calls.append("get_user_info")
            return {"sso_username": "u", "sso_email": "e@example.com"}

        def free_grant_status(self):
            spy.dc_calls.append("free_grant_status")
            return {"has_received": has_received}

        def claim_free_grant(self):
            spy.dc_calls.append("claim_free_grant")
            return {}

        def balance(self):
            spy.dc_calls.append("balance")
            return {"available_credits": "10.000000", "rpm_limit": 60, "usage_windows": {}}

        def list_keys(self):
            spy.dc_calls.append("list_keys")
            return []

        def create_key(self, name="default"):
            spy.dc_calls.append("create_key")
            return SimpleNamespace(
                id="kid", name=name, key="sk-new-plain", masked_key="sk-***", status="active"
            )

        def ensure_key(self, name="default"):
            spy.dc_calls.append("ensure_key")
            # 命中已有 key 时列表接口不返回明文 ⇒ `key` 是空串（见 B 的 docstring）。
            return SimpleNamespace(
                id="kid", name=name, key="", masked_key="sk-***", status="active"
            )

    def _dc(**kw):
        return _FakeDiscovery(**kw)

    monkeypatch.setattr(pipeline, "DiscoveryClient", _dc)
    monkeypatch.setattr(discovery, "DiscoveryClient", _dc)

    def _ak(name, ret):
        def _f(*a, **kw):
            spy.ak_calls.append(name)
            return ret

        return _f

    monkeypatch.setattr(apikey, "wait_until_active", _ak("wait_until_active", True))
    monkeypatch.setattr(apikey, "list_models", _ak("list_models", ["m1", "m2"]))
    monkeypatch.setattr(
        apikey,
        "chat",
        _ak(
            "chat",
            SimpleNamespace(
                ok=True, truncated=False, text="成功", usage={"total_tokens": 7}, error=""
            ),
        ),
    )

    monkeypatch.setattr(browser, "BrowserSession", _FakeSession)


def _load_downstream_module(monkeypatch):
    """把 `tools/run_downstream.py` 当**独立模块**加载，不碰测试进程的环境。

    🔴 为什么不用 `import run_downstream` / `import tools.run_downstream`：

      1. `tools/` **刻意不是包**（没有 `__init__.py`，见 `tools/_bootstrap.py`），
         顶层 import 得先把 `tools/` 塞进 `sys.path` —— 那是**全局副作用**，
         会改变同进程其他用例的 import 行为。
      2. 它顶部 `from _bootstrap import ROOT`，而 `_bootstrap` 的副作用是
         **加载 `.env`**（末尾 `import common.config`）。在测试进程里跑它会把使用者
         真实的 `IR_*` 灌进 `os.environ`，污染后续用例 ——
         `tests/test_tools_shim_parity.py` 正是为这个理由改用子进程。

    所以：给 `_bootstrap` 打一个**只带 `ROOT` 的桩**（随 `monkeypatch` 回滚），
    再用 `spec_from_file_location` 单独加载。
    """
    boot = types.ModuleType("_bootstrap")
    # 走 `__dict__` 而不是 `boot.ROOT = REPO`：桩模块是运行时造的，
    # `ModuleType` 上本来就没有 `ROOT` 属性，静态检查器会拒绝直接赋值；
    # 而 `setattr(boot, "ROOT", ...)` 会被 ruff 的 B010 拦下。两者运行时等价。
    boot.__dict__["ROOT"] = REPO
    monkeypatch.setitem(sys.modules, "_bootstrap", boot)

    path = REPO / "tools" / "run_downstream.py"
    spec = importlib.util.spec_from_file_location("_rd_under_test", path)
    # 这两条不变量本来就必须成立（否则下一行会 AttributeError）；
    # 显式断言只是把"不可能发生"变成静态可验证的。
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run_one(monkeypatch):
    """`tools/run_downstream.py:run_one`（加载方式见上）。"""
    return _load_downstream_module(monkeypatch).run_one


# ── 驱动 ────────────────────────────────────────────────────────────
def _run_a(monkeypatch, spy, *, verify, has_received):
    """驱动 A：`pipeline.stage_login_key`（`session=` 复用路径，不起浏览器）。"""
    _install(monkeypatch, spy, has_received=has_received)
    rec = pipeline.AccountRecord(email="a@example.com", password="pw")
    ok = pipeline.stage_login_key(rec, session=_FakeSession(), verify=verify, log=lambda _m: None)
    assert ok, f"A 没走通：{rec.error!r}"
    return rec


def _run_b(run_one, monkeypatch, spy, *, create, has_received):
    """驱动 B：`run_downstream.run_one`。"""
    _install(monkeypatch, spy, has_received=has_received)
    out = run_one(
        {"email": "a@example.com", "password": "pw"},
        headless=True,
        create=create,
        key_name="default",
        log=lambda _m: None,
    )
    assert out.get("downstream") == "ok", f"B 没走通：{out.get('downstream')!r}"
    return out


# ── 1. 共享前缀 ─────────────────────────────────────────────────────
def test_shared_readonly_prefix_is_identical(monkeypatch, run_one):
    """两边的前四步调用序列**逐项相同** —— 这是它们真正共用的那段链路。

    ⚠ 这条同时是"假件接上了没有"的守卫：假件没接上时 `dc_calls` 是空列表，
      `[] == SHARED_PREFIX` 为假 ⇒ 红，而不是空对空的假绿。
    """
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=False, has_received=False)
    spy_b = _Spy()
    _run_b(run_one, monkeypatch, spy_b, create=False, has_received=False)

    assert spy_a.dc_calls[:4] == SHARED_PREFIX, spy_a.dc_calls
    assert spy_b.dc_calls[:4] == SHARED_PREFIX, spy_b.dc_calls


def test_both_pass_jwt_and_cookies_to_the_client(monkeypatch, run_one):
    """`-10002` 那条规则：只带 `Authorization` 会被拒，必须同时带浏览器 Cookie。

    两边都构造 `DiscoveryClient(jwt=…, cookies=…)` —— 少一个参数就红。
    """
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=False, has_received=False)
    spy_b = _Spy()
    _run_b(run_one, monkeypatch, spy_b, create=False, has_received=False)

    for name, spy in (("A", spy_a), ("B", spy_b)):
        assert len(spy.dc_ctor) == 1, (name, spy.dc_ctor)
        kw = spy.dc_ctor[0]
        assert kw.get("jwt"), (name, kw)
        assert kw.get("cookies"), (name, kw)


# ── 2. 共享门控 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("has_received", [False, True])
def test_claim_free_grant_is_gated_on_both_sides(monkeypatch, run_one, has_received):
    """`claim_free_grant` 两边都**只在** `has_received` 为假时调（幂等）。

    已经领过还再打一次这个接口，是在给风控白送信号。
    """
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=False, has_received=has_received)
    spy_b = _Spy()
    _run_b(run_one, monkeypatch, spy_b, create=False, has_received=has_received)

    expected = not has_received
    for name, calls in (("A", spy_a.dc_calls), ("B", spy_b.dc_calls)):
        assert ("claim_free_grant" in calls) is expected, (
            f"{name} 在 has_received={has_received} 时对 claim_free_grant 的门控错了：{calls}"
        )


# ── 3. 文档写明的分歧 ───────────────────────────────────────────────
def test_documented_divergence_a_never_calls_list_keys(monkeypatch, run_one):
    """A **不调** `list_keys`；B 调。这是 §2.2「B7a」写明的分歧之一。

    ⚠ 给 A 补上 `list_keys` **不是**"补齐字段"：A 的只读 + 建 key 包在
      **同一个 `try`** 里，多一次请求 = 多一个让账号变 `failed` 的失败点。
    """
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=False, has_received=False)
    spy_b = _Spy()
    _run_b(run_one, monkeypatch, spy_b, create=False, has_received=False)

    assert "list_keys" not in spy_a.dc_calls, spy_a.dc_calls
    assert "list_keys" in spy_b.dc_calls, spy_b.dc_calls


def test_documented_divergence_only_b_sends_a_real_completion(monkeypatch, run_one):
    """B 的校验含 `chat`（真发一次推理）；A 只 `list_models`。"""
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=True, has_received=False)
    spy_b = _Spy()
    _run_b(run_one, monkeypatch, spy_b, create=True, has_received=False)

    assert spy_a.ak_calls == ["wait_until_active", "list_models"], spy_a.ak_calls
    assert spy_b.ak_calls == ["wait_until_active", "list_models", "chat"], spy_b.ak_calls


def test_documented_divergence_only_b_reuses_an_existing_key(monkeypatch, run_one):
    """只有 B 有"幂等复用"模式（`ensure_key`）；A 总是 `create_key`。

    ⚠ A 没有复用分支 ⇒ 也就不存在"把已有好 key 洗成空串"的风险；
      B 有，所以 B 那边必须守住 `if ak.key:`。这条差异别去抹平。
    """
    spy_a = _Spy()
    _run_a(monkeypatch, spy_a, verify=False, has_received=False)
    spy_reuse = _Spy()
    _run_b(run_one, monkeypatch, spy_reuse, create=False, has_received=False)

    assert "ensure_key" not in spy_a.dc_calls, spy_a.dc_calls
    assert "create_key" in spy_a.dc_calls, spy_a.dc_calls
    assert "ensure_key" in spy_reuse.dc_calls, spy_reuse.dc_calls
    assert "create_key" not in spy_reuse.dc_calls, spy_reuse.dc_calls


# ── 守卫：证明驱动的是**真的**那两份实现 ─────────────────────────────
def test_subjects_are_the_real_implementations(run_one):
    """守卫：`run_one` 必须来自仓库里那个真实文件。

    ⚠ 没有这条，将来有人把 `_load_downstream_module` 改成加载一个替身，
      上面所有"两边一致"的断言会变成对着空气断言。
    """
    assert (
        Path(run_one.__code__.co_filename).resolve()
        == (REPO / "tools" / "run_downstream.py").resolve()
    )
    assert pipeline.stage_login_key.__module__ == "src.pipeline"
