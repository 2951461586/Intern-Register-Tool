"""`src/browser/waf.py` 的代理串解析 —— 以及它与配置层语法的**一致性契约**。

为什么要单独一个文件
====================
2026-10-05 00:0x 实测：`sticky_ip_run.py` 用 `host:port:user:pass` 形态的
`IR_PROXY` 连打 10 次，**10/10** 全是 `Browser.new_context: Invalid URL`。
根因不在那次实验，而在 `src/browser/waf.py` 把**原始代理串**直接塞进了
`new_context(proxy={"server": px})`：

  · `host:port:user:pass` 不是 URL（`config.proxies()` 却明确支持这种写法）；
  · 账密内嵌（`scheme://user:pass@host:port`）也不行 —— Chromium 报
    `net::ERR_INVALID_AUTH_CREDENTIALS`，Playwright 要求**分字段**。

这条路径之前一直没被踩到，只因为主流水线的槽位 URL 恰好是
`http://127.0.0.1:7901`（无账密）。

🔴 **全程不启动浏览器**：`playwright` 只在 `solve_acw_challenge` 的函数体内
   import，本文件用 `sys.modules` 注入一个假模块来验接线 —— 真浏览器测试是
   `probe_*` 的事（`tests/test_dependency_surface.py` 把 playwright 列为
   FORBIDDEN，模块级装进来会当场红）。

⚠ 本文件刻意**不** `import playwright`：CI 不装它。
"""

from __future__ import annotations

import sys
import types

import pytest

from common import config
from src.browser.settings import BrowserSettings
from src.browser.waf import playwright_proxy, solve_acw_challenge

# ── 1. 原始串 → Playwright 形态 ────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        # 空 ⇒ 直连（`new_context` 不能收到一个空 dict）
        ("", None),
        ("   ", None),
        (None, None),
        # 无账密：两种写法都补上 scheme
        ("127.0.0.1:7901", {"server": "http://127.0.0.1:7901"}),
        ("http://127.0.0.1:7901", {"server": "http://127.0.0.1:7901"}),
        ("socks5://127.0.0.1:1080", {"server": "socks5://127.0.0.1:1080"}),
        # 有账密：`host:port:user:pass`（2026-10-05 炸掉的那一种）
        (
            "gateway.example.com:9091:user:pw",
            {
                "server": "http://gateway.example.com:9091",
                "username": "user",
                "password": "pw",
            },
        ),
        # 有账密：URL 写法 ⇒ **必须拆成三个字段**
        (
            "http://u:p@h.example:9091",
            {"server": "http://h.example:9091", "username": "u", "password": "p"},
        ),
        # URL 写法里账密是**百分号编码**的 ⇒ 要解回原文
        (
            "http://u%40x:p%3Aw@h.example:9091",
            {"server": "http://h.example:9091", "username": "u@x", "password": "p:w"},
        ),
        # 只有用户名没有密码：Playwright 要字符串，不能塞 None
        (
            "http://u@h.example:9091",
            {"server": "http://h.example:9091", "username": "u", "password": ""},
        ),
        # 首尾空白先 strip（配置里粘进来空格是常事）
        ("  http://127.0.0.1:7901  ", {"server": "http://127.0.0.1:7901"}),
    ],
)
def test_playwright_proxy_forms(raw, expect):
    assert playwright_proxy(raw) == expect


@pytest.mark.parametrize("raw", ["h", "a:b:c", "a:b:c:d:e", "http://", "http://:9091"])
def test_unrecognized_proxy_strings_raise_instead_of_silently_going_direct(raw):
    """语法不认识 ⇒ **抛**，不能返回 None。

    🔴 判别力：返回 None 会被上层当成"直连"，于是**配置写错**伪装成
      "盾没解开"（`sso.py` 会报 `WAF 解盾失败`）—— 方向错得和本项目最警惕的
      那类失败一样。而"返回 None 更宽容"正是最容易顺手写下的实现。
    """
    with pytest.raises(ValueError):
        playwright_proxy(raw)


def test_credentials_are_never_embedded_in_the_server_url():
    """变异验证：钉死"分字段"这件事本身（内嵌会让 Chromium 拒绝认证）。"""
    d = playwright_proxy("gateway.example.com:9091:user:pw")
    assert d is not None
    assert "@" not in d["server"], (
        "账密又被内嵌进 server URL 了 —— Chromium 会 ERR_INVALID_AUTH_CREDENTIALS"
    )
    assert d["username"] == "user" and d["password"] == "pw"


# ── 2. 与配置层的语法契约 ──────────────────────────────────────────
# `common/config.py:proxies()` 是第一处实现（requests 侧），本函数是第二处
# （Playwright 侧）。浏览器层不能 import config（阶段 A 消掉的回边），所以语法
# 知识必须复制一份 ⇒ 用同一张表钉住"两边接受的写法集合"，防漂移。

_SHARED_GRAMMAR = [
    "127.0.0.1:7901",
    "http://127.0.0.1:7901",
    "socks5://127.0.0.1:1080",
    "127.0.0.1:7901:user:pw",
    "http://user:pw@127.0.0.1:7901",
    "http://u%40x:p%3Aw@h.example:9091",
    "host.example:9091:user:p:w",  # 密码带 `:` ⇒ 5 段，两边都该拒
]


@pytest.mark.parametrize("raw", _SHARED_GRAMMAR)
def test_both_parsers_agree_on_wellformed_forms(raw, tmp_path):
    """**良构**写法两边必须一致接受（或一致拒绝）—— 否则 `.env` 里一句配置
    会让 requests 走代理、浏览器解盾直连，症状是"同一个出口"的假设静默失效。
    """
    try:
        config.proxies(raw)
        cfg_ok = True
    except ValueError:
        cfg_ok = False

    try:
        playwright_proxy(raw)
        waf_ok = True
    except ValueError:
        waf_ok = False

    assert cfg_ok == waf_ok, (
        f"{raw!r}：config.proxies() 接受={cfg_ok}，playwright_proxy() 接受={waf_ok}。\n"
        "两边语法必须一致 —— 否则 requests 与解盾浏览器会走**不同出口**。"
    )


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_means_direct_on_both_sides(raw):
    assert config.proxies(raw or "") is None
    assert playwright_proxy(raw) is None


def test_the_one_intentional_difference_is_pinned():
    """畸形 `scheme://` 上两边**故意**不同：配置层不校验，浏览器层抛错。

    `config.proxies("http://")` 原样透传（requests 自己会报错），而
    `playwright_proxy("http://")` 抛 `ValueError` —— 因为 Playwright 会先抛
    `Invalid URL`，把它提前成一句能照着修的话更有用。

    🔴 钉住它是为了**防止**有人把这里"顺手统一"成返回 None：
      那正好把配置错误变回"直连"，即上面那个测试拦下的失败形态。
    """
    assert config.proxies("http://") is not None, "配置层的宽容是既有行为，本测试不改它"
    with pytest.raises(ValueError):
        playwright_proxy("http://")


# ── 3. 接线：`solve_acw_challenge` 真的把**解析后**的 dict 交出去 ────
# 这是本次修复的本体。只测 `playwright_proxy` 会漏掉"函数里还用着原始串"
# 这种一半的修复 —— 那正是 2026-10-05 之前的状态。


class _FakePage:
    def __init__(self):
        self.content = None

    def goto(self, *a, **kw):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def set_content(self, html, **kw):
        self.content = html


class _FakeContext:
    def __init__(self, kw):
        self.kw = kw

    def new_page(self):
        return _FakePage()

    def cookies(self):
        return [{"name": "acw_sc__v2", "value": "ACW"}]


class _FakeBrowser:
    def __init__(self, recorder):
        self._recorder = recorder

    def new_context(self, **kw):
        self._recorder.append(kw)
        return _FakeContext(kw)

    def close(self):
        pass


class _FakePlaywright:
    def __init__(self, recorder):
        self._recorder = recorder
        self.chromium = self

    def launch(self, **_kw):
        return _FakeBrowser(self._recorder)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def fake_playwright(monkeypatch):
    """把假 `playwright.sync_api` 注入 `sys.modules`（函数体内 import 会命中它）。

    返回一个 list：每次 `new_context(**kw)` 的 kw 都会追加进去。
    """
    recorder: list[dict] = []
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _FakePlaywright(recorder)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    return recorder


def _settings() -> BrowserSettings:
    # `chrome_path` 必须是一个**真实存在**的文件：函数开头有 `is_file()` 早退。
    return BrowserSettings(
        chrome_path=sys.executable,
        sso_base="https://sso.example",
        discovery_base="https://ds.example",
        client_id="cid",
        source="src",
    )


def test_solve_acw_challenge_passes_the_parsed_dict(fake_playwright):
    acw = solve_acw_challenge(
        "<html>challenge</html>",
        "gateway.example.com:9091:user:pw",
        settings=_settings(),
    )
    assert acw == "ACW"
    assert len(fake_playwright) == 1
    assert fake_playwright[0]["proxy"] == {
        "server": "http://gateway.example.com:9091",
        "username": "user",
        "password": "pw",
    }, "原始串又被直接交给 Playwright 了（本次修复的一半）"


def test_solve_acw_challenge_goes_direct_without_proxy(fake_playwright):
    assert solve_acw_challenge("<html>x</html>", "", settings=_settings()) == "ACW"
    assert "proxy" not in fake_playwright[0], (
        "空代理串会转成 None ⇒ 不能给 `new_context` 传一个空/None 的 proxy（会是 Invalid URL）"
    )


def test_solve_acw_challenge_surfaces_a_bad_proxy_string(fake_playwright):
    """配置写错时**不启动浏览器**就炸 ⇒ 报错指向配置，而不是"盾没解开"。"""
    with pytest.raises(ValueError):
        solve_acw_challenge("<html>x</html>", "a:b:c", settings=_settings())
    assert fake_playwright == [], "代理串都没解析出来就把浏览器起来了 —— 白白多启动一个 Chrome"
