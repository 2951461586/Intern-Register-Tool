"""`src/browser/` 的**契约测试** —— 零浏览器、零网络。

为什么需要这个文件
------------------
2026-09-19 做阶段 A 重构（抽 `_AttemptState` / 8 个 `_step_*` / `_retry_loop`）时发现：
这个模块**此前零测试覆盖**（`grep -rln browser_login tests/` 为空）。

它是全项目最长的模块（当时 955 行），而它向外承诺的东西全在**数据形状**里：

  - `LoginResult.captcha_stage` 的键名 —— 探针读、台账存
  - `LoginResult` 的字段清单
  - `_retry_loop` 的计数与冷却口径

这些一旦改坏**不会报错**，只会在某次跑批时表现为"字段没了"。
所以这里把形状冻结下来 —— 它们**不需要浏览器就能验证**。

🔴 本文件在阶段 B 拆包后**刻意从真源子模块导入**（`src.browser.attempt` 等），
   而不是从包上取。包只 re-export 公共 API，私有函数不走包 ——
   这样"旧路径还能用"的错觉会立刻变成 `ImportError`，而不是静默失效。
   同理，两处 `monkeypatch` 的目标也重定向到了真源命名空间，
   见 `test_default_chrome_args_come_from_the_reader_module` 与
   `test_patching_the_package_attribute_does_not_reach_launch_kwargs`。

⚠ 本文件**不验证**真浏览器行为（验证码通路、点击轨迹、JWT 捕获）。
   那部分的唯一验收手段是真跑一次登录 —— 已于 2026-09-19 完成，
   见 `docs/refactor-14-browser-split-plan.md` §9.7（正向全链路 + 差分比对）
   与 §10（阶段 B 拆包验收）。
"""

import dataclasses
import time

import pytest

import src.browser.session as browser_session
from src.browser import CHROME_ARGS, LoginResult, build_login_url
from src.browser.attempt import _build_result
from src.browser.session import BrowserSession, _launch_kwargs, _retry_loop
from src.browser.state import _AttemptState


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """`_retry_loop` 的冷却会真 sleep —— 测试里换成记录器。

    ⚠ 这里 patch 的是 **stdlib `time` 模块**的 `sleep`（不是某个子模块的属性），
      所以是全局生效 —— monkeypatch 会在每个用例后恢复。
      只换 `sleep`、不换整个 `time`：`_AttemptState.mark()` 还要用 `time.time()`。
      拆包前写的是 `browser_login.time`，那只是 stdlib 模块的一个别名，
      改成直接 `import time` 后语义完全一致。
    """
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


# ────────────────────────────────────────────────────────────────
# [1] 数据形状冻结（下游契约）
# ────────────────────────────────────────────────────────────────
def test_login_result_field_order_is_frozen():
    """字段名与顺序都是契约 —— `to_json` 式的展开按定义顺序。"""
    assert [f.name for f in dataclasses.fields(LoginResult)] == [
        "ok", "jwt", "code", "reason", "cookies",
        "captcha_stage", "attempts_used", "timings",
    ]


def test_captcha_stage_keys_are_frozen():
    """`captcha_stage` 的键被探针与台账依赖，少一个就是静默丢数据。"""
    res = _build_result(_AttemptState(), cookies={}, waited=0)
    assert set(res.captcha_stage) == {
        "init", "verify", "last_ok", "slider", "traceless_reject",
        "payload", "events", "path", "mouse", "captcha_wait_ms",
    }


def test_captcha_stage_mouse_keys_are_frozen():
    res = _build_result(_AttemptState(), cookies={}, waited=0)
    assert set(res.captcha_stage["mouse"]) == {
        "micro_move", "budget_s", "moves", "points",
        "idle_waits", "budget_exhausted",
    }


def test_build_result_defaults_when_nothing_happened():
    res = _build_result(_AttemptState(), cookies={}, waited=123)
    assert res.ok is False
    assert res.jwt == ""
    assert res.reason == "no jwt captured"
    assert res.captcha_stage["captcha_wait_ms"] == 123
    assert res.captcha_stage["init"] == 0


def test_build_result_falls_back_to_uaa_token_cookie():
    """没有抓到 Authorization 头时，退到 cookie 里的 uaa-token。"""
    res = _build_result(
        _AttemptState(), cookies={"uaa-token": "from-cookie"}, waited=0)
    assert res.ok is True
    assert res.jwt == "from-cookie"


def test_build_result_prefers_captured_jwt_over_cookie():
    st = _AttemptState()
    st.jwt = "from-header"
    res = _build_result(st, cookies={"uaa-token": "from-cookie"},
                                      waited=0)
    assert res.jwt == "from-header"


@pytest.mark.parametrize("init,last_ok,expected", [
    (0, False, "?"),    # 还没跑过验证码
    (1, True, "A"),     # TRACELESS 自过（0 次点击）
    (1, False, "?"),    # 预检被拒，还没降级
    (2, False, "B"),    # 已降级到 CHECK_BOX
    (3, True, "B"),     # init>=2 一律记 B
])
def test_captcha_path_classification(init, last_ok, expected):
    """Path A / B 的判据：A = `init<=1 且通过`，B = `init>=2`。"""
    st = _AttemptState()
    st.cap["init"] = init
    st.cap["last_ok"] = last_ok
    res = _build_result(st, cookies={}, waited=0)
    assert res.captcha_stage["path"] == expected


def test_reason_priority_slider_beats_captcha_reject():
    """没拿到 jwt 时，滑块 > 验证码被拒 > 什么都没发生。"""
    st = _AttemptState()
    st.cap["slider"] = "aliyunCaptcha-sliding"
    st.cap["verify"] = ["F001"]
    assert "secondary captcha" in _build_result(
        st, cookies={}, waited=0).reason

    st2 = _AttemptState()
    st2.cap["verify"] = ["F001"]
    assert "captcha rejected (F001)" == _build_result(
        st2, cookies={}, waited=0).reason


# ────────────────────────────────────────────────────────────────
# [2] `_AttemptState`：从闭包改成类，行为必须一致
# ────────────────────────────────────────────────────────────────
def test_mark_records_milliseconds():
    st = _AttemptState()
    st.mark("goto")
    assert "goto" in st.timings
    assert isinstance(st.timings["goto"], int)
    assert st.timings["goto"] >= 0


def test_ev_appends_triple_to_events():
    st = _AttemptState()
    st.ev("submit")
    st.ev("click#1", "clicked=True")
    assert [e[1] for e in st.cap["events"]] == ["submit", "click#1"]
    assert st.cap["events"][1][2] == "clicked=True"
    assert all(isinstance(e[0], int) for e in st.cap["events"])


def test_state_starts_with_the_original_container_shapes():
    st = _AttemptState()
    assert st.cap == {"init": 0, "verify": [], "last_ok": False, "payload": "",
                      "slider": "", "trivial": 0, "events": []}
    assert st.mv == {}
    assert st.timings == {}
    assert st.jwt == "" and st.code == ""


class _FakeResp:
    """最小响应替身：只提供 `on_response` 真正读的那几个属性。"""

    def __init__(self, url="", headers=None, post_data="", payload=None):
        self.url = url
        self.headers = headers or {}
        self.request = type("R", (), {"post_data": post_data})()
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_on_response_captures_bearer_token():
    st = _AttemptState()
    st.on_response(_FakeResp(headers={"authorization": "Bearer abc.def"}))
    assert st.jwt == "abc.def"


def test_on_response_captures_only_first_bearer_token():
    st = _AttemptState()
    st.on_response(_FakeResp(headers={"authorization": "Bearer first"}))
    st.on_response(_FakeResp(headers={"authorization": "Bearer second"}))
    assert st.jwt == "first"


def test_on_response_captures_auth_code():
    st = _AttemptState()
    st.on_response(_FakeResp(url="https://x/internal/auth",
                             payload={"data": {"code": "C123"}}))
    assert st.code == "C123"


def test_code_is_a_race_snapshot_taken_when_the_attempt_ends():
    """`LoginResult.code` 只是"收工快照" —— 回调没跑到就永远是空串。

    ⚠ 这条**记录既有性质**，不是断言它"应该"这样：
      `internal/auth` 的响应由 Playwright 在事件循环里**异步回调**，
      而 `_build_result()` 在 `_run_attempt()` 收尾时**同步**读 `st.code`。
      谁先到取决于网络时序 ⇒ `code` 天然是**不确定值**。

    实测（2026-09-19 阶段 A 差分验收，5 次真登录）：
      新旧实现都观测到 `len(code)` = 20 与 0 两种取值；唯一的 0 在**反转
      调用顺序后不复现** ⇒ 与实现无关。且该字段**全仓零生产读者**
      （`.workbuddy-ai/tmp/scan_code_field_readers.py` AST 扫描：10 处
      `.code` 访问，0 处的基名是 `LoginResult` 型变量）。

    ⇒ 所以差分验收里 `code_len` 不一致**不构成**行为不等价的证据。
    """
    # ① 回调还没跑到 → 空串
    st = _AttemptState()
    assert _build_result(st, cookies={}, waited=0).code == ""

    # ② 回调跑到之后 → 有值
    st2 = _AttemptState()
    st2.on_response(_FakeResp(url="https://x/internal/auth",
                              payload={"data": {"code": "C" * 20}}))
    assert _build_result(st2, cookies={}, waited=0).code == "C" * 20

    # ③ 快照一旦交出去，之后再来的回调补不回来
    st3 = _AttemptState()
    res = _build_result(st3, cookies={}, waited=0)
    st3.on_response(_FakeResp(url="https://x/internal/auth",
                              payload={"data": {"code": "late"}}))
    assert res.code == ""


def test_on_response_counts_init_captcha():
    st = _AttemptState()
    st.on_response(_FakeResp(post_data='{"x":"InitCaptchaV3"}'))
    st.on_response(_FakeResp(post_data='{"x":"InitCaptchaV3"}'))
    assert st.cap["init"] == 2
    assert [e[1] for e in st.cap["events"]] == ["Init#1", "Init#2"]


@pytest.mark.parametrize("init,ok,expected_trivial", [
    (1, False, 1),   # Init#1 阶段被拒 → 计一次
    (1, True, 0),    # TRACELESS 直接通过（T001）→ 不算 reject
    (2, False, 0),   # 降级之后的拒绝不算 TRACELESS 预检被拒
])
def test_on_response_trivial_counts_only_init1_rejections(init, ok,
                                                          expected_trivial):
    """🔴 判据必须同时满足"在 Init#1 阶段"和"ok=False" —— 见原注释。"""
    st = _AttemptState()
    st.cap["init"] = init
    st.on_response(_FakeResp(post_data="VerifyCaptchaV3",
                             payload={"Result": {"VerifyCode": "F001",
                                                 "VerifyResult": ok}}))
    assert st.cap["trivial"] == expected_trivial
    assert st.cap["last_ok"] is ok


def test_on_response_records_reject_payload_but_not_success():
    st = _AttemptState()
    st.on_response(_FakeResp(post_data="VerifyCaptchaV3",
                             payload={"Result": {"VerifyCode": "F001",
                                                 "VerifyResult": False}}))
    assert st.cap["payload"] != ""

    st2 = _AttemptState()
    st2.on_response(_FakeResp(post_data="VerifyCaptchaV3",
                              payload={"Result": {"VerifyCode": "T001",
                                                  "VerifyResult": True}}))
    assert st2.cap["payload"] == ""


def test_on_response_survives_malformed_json():
    """回调挂在事件循环上 —— 可恢复的畸形响应必须被吞掉，不能污染整页。"""
    st = _AttemptState()
    st.on_response(_FakeResp(post_data="VerifyCaptchaV3"))   # json() 会抛
    st.on_response(_FakeResp(post_data="InitCaptchaV3"))     # 这条不读 json
    assert st.cap["verify"] == []
    assert st.cap["init"] == 1


def test_on_response_url_access_is_unguarded_today():
    """⚠ 记录一个**既有**脆弱点（非本次重构引入）。

    `resp.url` / `resp.request.post_data` 的取值不在 try 块内，缺属性会抛出去。
    阶段 A 重构**逐字保留**了这个行为 —— 这条测试的作用是把它记录在案，
    将来谁要加固，先看到这里。
    """
    with pytest.raises(AttributeError):
        _AttemptState().on_response(object())


# ────────────────────────────────────────────────────────────────
# [3] `_retry_loop`：两个入口共用的重试骨架
# ────────────────────────────────────────────────────────────────
def _runner(results):
    """按顺序吐出给定结果，并记录每次拿到的 tag。"""
    tags = []
    it = iter(results)

    def run_once(tag):
        tags.append(tag)
        return next(it)

    return run_once, tags


def test_retry_loop_stops_at_first_success(_no_real_sleep):
    run_once, tags = _runner([LoginResult(ok=True, jwt="j")])
    res = _retry_loop(run_once, attempts=3, cooldown=15.0,
                                    verbose=False, retry_hint="重试")
    assert res.ok is True and res.attempts_used == 1
    assert tags == ["_a1"]
    assert _no_real_sleep == []


def test_retry_loop_uses_all_attempts_and_numbers_them(_no_real_sleep):
    run_once, tags = _runner([LoginResult(ok=False, reason="r1"),
                              LoginResult(ok=False, reason="r2"),
                              LoginResult(ok=False, reason="r3")])
    res = _retry_loop(run_once, attempts=3, cooldown=15.0,
                                    verbose=False, retry_hint="重试")
    assert tags == ["_a1", "_a2", "_a3"]
    assert res.attempts_used == 3
    assert res.reason == "r3"
    assert len(_no_real_sleep) == 2          # 最后一次失败后不再等


def test_retry_loop_cooldown_grows_linearly(_no_real_sleep):
    """冷却 = `cooldown * (i+1) + 抖动[0,5)`。"""
    run_once, _ = _runner([LoginResult(ok=False), LoginResult(ok=False),
                           LoginResult(ok=False)])
    _retry_loop(run_once, attempts=3, cooldown=15.0,
                              verbose=False, retry_hint="重试")
    assert 15.0 <= _no_real_sleep[0] < 20.0
    assert 30.0 <= _no_real_sleep[1] < 35.0


def test_retry_loop_treats_attempts_zero_as_one(_no_real_sleep):
    """`max(1, attempts)` —— attempts=0 也要跑一次。"""
    run_once, tags = _runner([LoginResult(ok=False, reason="only")])
    res = _retry_loop(run_once, attempts=0, cooldown=15.0,
                                    verbose=False, retry_hint="重试")
    assert tags == ["_a1"]
    assert res.reason == "only"


def test_retry_loop_propagates_attempts_used_to_each_result(_no_real_sleep):
    """`attempts_used` 写在每次的返回值上 —— 下游（台账）依赖它。"""
    seen = []
    results = [LoginResult(ok=False), LoginResult(ok=True)]

    def run_once(tag):
        r = results.pop(0)
        seen.append(r)
        return r

    _retry_loop(run_once, attempts=3, cooldown=15.0,
                              verbose=False, retry_hint="重试")
    assert [r.attempts_used for r in seen] == [1, 2]


def test_retry_loop_hint_appears_in_log(capsys, _no_real_sleep):
    run_once, _ = _runner([LoginResult(ok=False, reason="boom"),
                           LoginResult(ok=True)])
    _retry_loop(run_once, attempts=2, cooldown=1.0,
                              verbose=True, retry_hint="换新会话重试")
    out = capsys.readouterr().out
    assert "=== 尝试 1/2 ===" in out
    assert "冷却" in out and "换新会话重试" in out


# ────────────────────────────────────────────────────────────────
# [4] Chrome 启动参数的注入面
# ────────────────────────────────────────────────────────────────
def test_launch_kwargs_defaults_to_module_constant():
    kw = _launch_kwargs(headless=True, chrome_args=None)
    assert kw["args"] is CHROME_ARGS
    assert kw["headless"] is True
    assert kw["executable_path"]


def test_launch_kwargs_uses_injected_args():
    kw = _launch_kwargs(headless=False, chrome_args=["--x"])
    assert kw["args"] == ["--x"]


def test_launch_kwargs_copies_injected_list():
    """注入的 list 被复制 —— 调用方事后改自己的 list 不该影响已建的参数。"""
    mine = ["--x"]
    kw = _launch_kwargs(headless=False, chrome_args=mine)
    mine.append("--y")
    assert kw["args"] == ["--x"]


def test_empty_injection_is_respected_not_treated_as_absent():
    """`chrome_args=[]` 是"不要任何参数"，不能被当成"没传"。"""
    kw = _launch_kwargs(headless=False, chrome_args=[])
    assert kw["args"] == []


def test_session_keeps_injected_chrome_args():
    sess = BrowserSession(headless=True, chrome_args=["--z"])
    assert sess.chrome_args == ["--z"]
    assert sess.headless is True
    assert sess.launch_ms == 0


def test_session_defaults_to_none_meaning_module_constant():
    assert BrowserSession().chrome_args is None


def test_default_chrome_args_come_from_the_reader_module(monkeypatch):
    """🔴 `_launch_kwargs` 的默认值从**它所在模块**的命名空间读，不是从包里读。

    拆包后同一个常量有**三处可见**：

      | 位置 | 怎么来的 |
      |---|---|
      | `src.browser.constants.CHROME_ARGS` | 定义处（真源） |
      | `src.browser.CHROME_ARGS` | `__init__` 的 re-export |
      | `src.browser.session.CHROME_ARGS` | `from .constants import` 绑的**同一对象** |

    三者**初始时指向同一个 list**（`is` 成立），但**重新赋值**只改被赋的那个
    命名空间。所以：

      ✅ patch `src.browser.session.CHROME_ARGS` → **生效**
      ❌ patch `src.browser.CHROME_ARGS`        → **静默失效**（不报错）

    这条钉住"生效的那条路"。
    """
    # 真源唯一：包级与 session 级初始指向同一个对象
    assert browser_session.CHROME_ARGS is CHROME_ARGS

    monkeypatch.setattr(browser_session, "CHROME_ARGS", ["--patched"])
    kw = _launch_kwargs(headless=True, chrome_args=None)
    assert kw["args"] == ["--patched"]


def test_patching_the_package_attribute_does_not_reach_launch_kwargs(monkeypatch):
    """🔴 **反向**守卫：把"patch 包级属性会静默失效"这个坑钉死。

    这是拆分**新引入**的陷阱（对应 skill `python-compat-shell-refactor` §D1/D2）：
    `from .constants import CHROME_ARGS` 在 `session` 里绑定了一份引用，
    改包上的名字改不到它，而且**不报错**。

    为什么要把一个"不生效"钉成测试：它是**静默**的 —— 哪天有人把
    `session.py` 改成 `from . import constants` + `constants.CHROME_ARGS`
    （运行时查属性，patch 就会生效），这条会红，提示
    「失效面变了，请同步更新 `probe_headless.py` 的注释与本文档」。

    ⚠ 这条断言的是**当前设计**，不是"正确的设计"。它存在的意义是让变化被看见。
    """
    import src.browser as pkg

    monkeypatch.setattr(pkg, "CHROME_ARGS", ["--should-not-arrive"])
    kw = _launch_kwargs(headless=True, chrome_args=None)
    assert kw["args"] != ["--should-not-arrive"]
    assert kw["args"] is browser_session.CHROME_ARGS


# ────────────────────────────────────────────────────────────────
# [5] 纯函数
# ────────────────────────────────────────────────────────────────
def test_build_login_url_shape():
    url = build_login_url()
    assert "/login?redirect=" in url
    assert "token-plan/home" in url
    assert "clientId=" in url and "source=" in url
