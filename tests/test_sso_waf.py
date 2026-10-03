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

from src.sso import SSOClient, _json_true, _looks_like_waf_challenge

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


def _client(responses, *, solver=None, proxy=None) -> tuple[SSOClient, Any]:
    c = SSOClient(proxy=proxy, waf_solver=solver)
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
