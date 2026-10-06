"""`SSOClient` 的 WAF 解盾路径。

为什么单独一个文件
==================
2026-10-03 实测：Aliyun WAF 对 `register/byEmail` 在**所有出口**（槽位代理 /
直连 / 真·直连）都返回 `200 + text/html` 的 JS 挑战页。`sso.py` 原先没有解盾能力，
`r.json()` 直接抛 `JSONDecodeError` —— 被上层读成"网络/邮箱错误"，**方向完全错**。
本文件钉住修好后的判据与重试路径。

🔴 **全程不启动浏览器**：`SSOClient` 的解盾器是**可注入**的（`waf_solver=`），
   测试注入一个假解盾器。真解盾在 `src/browser/waf.py`，它只在函数体内
   import playwright —— 测试链不许被拖进这个重依赖
   （见 `tests/test_dependency_surface.py` 的 FORBIDDEN）。

🔴 假对象一律标成 `Any`：它们是**故意的无类型替身**，结构上刻意不等于
   `requests.Response` / `requests.Session`。
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from src.sso import SSOClient, WafCookieCache, _json_true, _looks_like_waf_challenge

# ── 假对象：`SSOClient` 唯一的网络出口 + 响应 ───────────────────────


class FakeResp:
    def __init__(self, status_code, *, json_body=None, text="", ct="application/json"):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = {"content-type": ct}

    def json(self):
        if self._json is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeCookies:
    def __init__(self):
        self.set_calls: list[tuple] = []

    def set(self, name, value, **kw):
        self.set_calls.append((name, value, kw))


class FakeSession:
    """`SSOClient` 唯一的网络出口。按预置队列依次弹出响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers: dict = {}
        self.cookies = FakeCookies()
        self.posts: list[tuple] = []
        self.trust_env = True

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if not self._responses:
            raise AssertionError("预置响应用完了，又来了一次 post")
        return self._responses.pop(0)


def _resp(*, status_code=200, json_body=None, text="", ct="application/json") -> Any:
    return FakeResp(status_code, json_body=json_body, text=text, ct=ct)


def _challenge() -> Any:
    return _resp(text="<html>acw challenge</html>", ct="text/html; charset=utf-8")


def _client(responses, *, solver=None, proxy=None, cache=None) -> tuple[SSOClient, Any]:
    c = SSOClient(proxy=proxy, waf_solver=solver, waf_cache=cache)
    fake: Any = FakeSession(responses)
    c.session = fake
    return c, fake


# ── 1. 判据 ────────────────────────────────────────────────────────


def test_challenge_detection_matrix():
    """`200 + 非 JSON` 才是挑战页；5xx 的 HTML 错误页**不是**。

    🔴 判别力：把 5xx 也当挑战，会把一次可退避重试的服务端抖动
      变成一次浏览器启动（slow + 指错方向）。
    """
    assert _looks_like_waf_challenge(_challenge())
    assert _looks_like_waf_challenge(_resp(text="<!DOCTYPE html><html>...", ct=""))
    assert not _looks_like_waf_challenge(_resp(json_body={"success": False}))
    assert not _looks_like_waf_challenge(
        _resp(status_code=500, text="<html>boom</html>", ct="text/html")
    )
    assert not _looks_like_waf_challenge(_resp(status_code=429, text="too many", ct="text/plain"))


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        (True, True),
        (False, False),
        (1, False),
        (0, False),
        ("true", False),
        (None, False),
        ({}, False),
        ([], False),
    ],
)
def test_json_true_is_strict(value, expect):
    """严格判真：只有 JSON 字面 `true` 算真（`1` / `"true"` 不算）。

    与契约上原来的 `value is True` 等价 —— 拿 `1` 当真是掩盖契约漂移。
    """
    assert _json_true(value) is expect


# ── 2. 解盾 → 回填 cookie → 重试 ───────────────────────────────────


def test_post_solves_challenge_sets_cookie_and_retries_once():
    ok = _resp(json_body={"success": True, "data": {"ssoUid": "1"}})
    seen: dict = {}

    def solver(html, proxy):
        seen["html"] = html
        seen["proxy"] = proxy
        return "ACW123"

    c, fake = _client([_challenge(), ok], solver=solver, proxy="http://127.0.0.1:7903")
    r = c._post("/register/byEmail", {"a": 1})

    assert r is ok
    assert seen["html"] == "<html>acw challenge</html>"
    assert seen["proxy"] == "http://127.0.0.1:7903", (
        "解盾必须走**同一个出口 IP** —— 挑战是按出口下发的，换 IP 解出来的 cookie 在原出口上没用"
    )
    assert fake.cookies.set_calls[0][:2] == ("acw_sc__v2", "ACW123")
    assert len(fake.posts) == 2, "先撞挑战、解盾后重试恰好一次"


def test_5xx_html_never_starts_the_browser():
    """5xx + HTML 走退避重试，**不**触发解盾。"""
    started: list = []
    c, _ = _client(
        [_resp(status_code=502, text="<html>bad gateway</html>", ct="text/html")],
        solver=lambda h, p: started.append(h) or "X",
    )
    with pytest.raises(requests.HTTPError):
        c._post("/register/byEmail", {}, attempts=1)  # attempts=1 ⇒ 不 sleep
    assert started == []


def test_solve_failure_raises_a_clear_actionable_error():
    """解不出来要报**能照着修**的错，而不是 `JSONDecodeError`。"""
    c, _ = _client([_challenge()], solver=lambda h, p: "", proxy="http://127.0.0.1:7903")
    with pytest.raises(RuntimeError) as ei:
        c._post("/register/byEmail", {})
    msg = str(ei.value)
    assert "WAF" in msg and "acw_sc__v2" in msg
    assert "IR_CHROME_PATH" in msg, "要指出跟哪个配置项有关"
    assert "http://127.0.0.1:7903" in msg, "要带上出口，否则没法排查"


def test_persistent_challenge_after_solve_is_reported():
    """解盾后仍被挑战 ⇒ 明确报出来（不是静默返回 HTML 让上层炸 JSON）。"""
    c, _ = _client([_challenge(), _challenge()], solver=lambda h, p: "ACW")
    with pytest.raises(RuntimeError, match="仍返回挑战页"):
        c._post("/register/byEmail", {})


def test_non_json_hard_block_raises_a_clear_error():
    """WAF **硬拦截**（实测 `405 + text/html`，errors.aliyun.com 页面）也要报清楚。

    🔴 判别力：这种页不是挑战页（没有 acw_sc__v2 可解），但又不能让上层去吃
       `JSONDecodeError` —— 那会把“出口被封了”读成“网络/邮箱错误”。
    """
    hard = _resp(status_code=405, text="<html>405</html>", ct="text/html; charset=utf-8")
    c, _ = _client([hard], proxy="http://127.0.0.1:7903")
    with pytest.raises(RuntimeError) as ei:
        c._post("/register/byEmail", {}, attempts=1)
    msg = str(ei.value)
    assert "非 JSON" in msg and "405" in msg
    assert "http://127.0.0.1:7903" in msg


# ── 3. 与真实调用方的接线 ──────────────────────────────────────────


def test_register_returns_a_result_after_the_waf_retry():
    """`register()` 经解盾后照常拿到 `RegisterResult`（端到端的形状）。"""
    ok = _resp(
        json_body={
            "success": True,
            "msgCode": "10000",
            "data": {"ssoUid": "471000651", "email": "a@outlook.com", "username": "lz851510"},
        }
    )
    c, _ = _client([_challenge(), ok], solver=lambda h, p: "ACW")
    res = c.register("lz851510", "a@outlook.com", "pw")
    assert res.ok is True
    assert res.sso_uid == "471000651"
    assert res.email == "a@outlook.com"


def test_activate_uses_strict_json_true_after_the_waf_retry():
    ok = _resp(json_body={"success": True})
    c, _ = _client([_challenge(), ok], solver=lambda h, p: "ACW")
    assert c.activate("tok", "sign") is True

    # JSON `1` 不算 `true`（严格性没被解盾改动顺手改掉）
    loose = _resp(json_body={"success": 1})
    c2, _ = _client([_challenge(), loose], solver=lambda h, p: "ACW")
    assert c2.activate("tok", "sign") is False


# ── 4. 传输层重试（2026-10-05 新增）──────────────────────────────


def test_transport_error_is_retried_then_succeeds():
    """SSL EOF / 读超时 / 代理断开**不再**直接算账号失败。

    🔴 判别力：近三轮 428 个账号里 26 个失败是这一类（其中 10 条还是只读接口）
       —— 原先 `session.post` 一抛就冒泡成 `failed`。
    """
    ok = _resp(json_body={"success": True})
    c, fake = _client([])
    calls = {"n": 0}

    def post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("SSL: UNEXPECTED_EOF_WHILE_READING")
        fake.posts.append((url, json))
        return ok

    fake.post = post
    c._backoff = lambda i, resp=None: 0.0  # 不真睡
    r = c._post("/personal/username/check", {"username": "x"}, attempts=3)

    assert r is ok
    assert calls["n"] == 2
    st = c.take_stats()
    assert st["transport_retries"] == 1
    assert st["retries"] == 1


def test_transport_error_on_the_last_attempt_still_raises():
    """重试用尽后要抛**原始**异常，不能静默返回。"""
    c, fake = _client([])

    def post(url, headers=None, json=None, timeout=None):
        raise requests.exceptions.ReadTimeout("read timeout=30")

    fake.post = post
    c._backoff = lambda i, resp=None: 0.0
    with pytest.raises(requests.exceptions.ReadTimeout):
        c._post("/register/byEmail", {}, attempts=2)
    assert c.take_stats()["transport_retries"] == 1


def test_retry_gate_runs_before_every_retry_not_before_the_first_attempt():
    """闸门只为**重试**补限速 —— 第 0 次由调用方（`_write_gate`）负责。

    🔴 判别力：重试原先完全绕过闸门 ⇒ 多线程退避时长相同、重试请求同步撞车，
       服务端继续回 429（实测 47/81 的失败源于此）。
    """
    too_many = _resp(status_code=429, text="slow down", ct="text/plain")
    ok = _resp(json_body={"success": True})
    c, _ = _client([too_many, ok])
    c._backoff = lambda i, resp=None: 0.0
    gated: list = []

    r = c._post("/register/byEmail", {}, retry_gate=lambda: gated.append(1))

    assert r is ok
    assert len(gated) == 1, "只该在重试前补一次（第 0 次由调用方管）"
    st = c.take_stats()
    assert st["http_429"] == 1
    assert st["retries"] == 1


def test_persistent_429_uses_the_whole_attempt_budget():
    """🔴 默认尝试次数必须是 6：实测仅有的 2 个失败都是“连续 4 发全 429”耗尽。

    判别力：若有人把默认改回 4，“连续 5 发 429 → 第 5 发就能成”的账号会重新
    变成失败，而这条用例会当场红。
    """
    from src.sso import POST_ATTEMPTS

    c, fake = _client([_resp(status_code=429, text="slow", ct="text/plain") for _ in range(8)])
    c._backoff = lambda i, resp=None: 0.0

    with pytest.raises(requests.HTTPError):
        c._post("/register/byEmail", {})

    assert len(fake.posts) == POST_ATTEMPTS
    assert c.take_stats()["http_429"] == POST_ATTEMPTS


def test_a_late_429_still_succeeds_within_the_budget():
    """第 5 发才成功也必须成（旧预算 4 会让它失败）。"""
    resps = [_resp(status_code=429, text="slow", ct="text/plain") for _ in range(4)]
    ok = _resp(json_body={"success": True})
    c, _ = _client(resps + [ok])
    c._backoff = lambda i, resp=None: 0.0

    assert c._post("/register/byEmail", {}) is ok
    assert c.take_stats()["http_429"] == 4


def test_retry_gate_abort_propagates_to_the_caller():
    """闸门里复查到配额触顶时抛出的中止异常必须**穿出去**，不能被吞成失败。"""

    class Abort(Exception):
        pass

    def boom():
        raise Abort("quota")

    c, _ = _client([_resp(status_code=500, text="boom", ct="text/plain")])
    c._backoff = lambda i, resp=None: 0.0
    with pytest.raises(Abort):
        c._post("/register/byEmail", {}, retry_gate=boom)


# ── 5. 埋点 ────────────────────────────────────────────────────


def test_take_stats_reports_waf_solve_time_and_is_cleared_on_read():
    """埋点必须**可验证**：解盾次数/耗时、重试次数直接进台账，而不是靠反推。"""
    ok = _resp(json_body={"success": True})
    c, _ = _client([_challenge(), ok], solver=lambda h, p: "ACW")
    c._post("/register/byEmail", {})

    st = c.take_stats()
    assert st["waf_solves"] == 1
    assert st["waf_solve_ms"] >= 0
    assert st["retries"] == 1  # 解盾后的重发
    assert c.take_stats() == {}, "取过一次就清零（稀疏：没发生的键不出现）"


# ── 6. 解盾 cookie 的跨账号复用 ────────────────────────────────


def test_cached_acw_cookie_is_reused_without_starting_the_browser():
    """同一出口的下一个账号直接复用缓存 cookie —— 不再启一次 Chrome。

    🔴 判别力：这是实测 92% 的成功账号 `register_call` >10s 的主因。
    """
    cache = WafCookieCache()
    cache.set("http://127.0.0.1:7901", "ACW-CACHED")
    started: list = []
    ok = _resp(json_body={"success": True})
    c, fake = _client(
        [ok],
        solver=lambda h, p: started.append(h) or "X",
        proxy="http://127.0.0.1:7901",
        cache=cache,
    )

    c._post("/register/byEmail", {})

    assert started == [], "有缓存时**不该**启动解盾浏览器"
    assert fake.cookies.set_calls[0][:2] == ("acw_sc__v2", "ACW-CACHED")
    assert c.take_stats().get("waf_solves") is None


def test_a_fresh_solve_is_written_back_to_the_cache():
    cache = WafCookieCache()
    ok = _resp(json_body={"success": True})
    c, _ = _client(
        [_challenge(), ok],
        solver=lambda h, p: "ACW-NEW",
        proxy="http://127.0.0.1:7901",
        cache=cache,
    )

    c._post("/register/byEmail", {})

    assert cache.get("http://127.0.0.1:7901") == "ACW-NEW"
    # 缓存按出口分桶：另一个出口读不到这个值
    assert cache.get("http://127.0.0.1:7902") == ""


def test_stale_cached_cookie_self_heals_by_solving_again():
    """缓存可能过期 —— 服务端照常回挑战页，重解一次并**覆盖**缓存。"""
    cache = WafCookieCache()
    cache.set("http://127.0.0.1:7901", "ACW-STALE")
    ok = _resp(json_body={"success": True})
    c, _ = _client(
        [_challenge(), ok],
        solver=lambda h, p: "ACW-FRESH",
        proxy="http://127.0.0.1:7901",
        cache=cache,
    )

    assert c.register("u", "a@b.c", "pw").ok is True
    assert cache.get("http://127.0.0.1:7901") == "ACW-FRESH"
    assert c.take_stats()["waf_solves"] == 1
