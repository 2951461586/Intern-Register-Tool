"""阿里云 WAF 的 JS 挑战解盾（`acw_sc__v2`）。

背景
----
2026-09-23 起，OpenXLab 的**写接口**（`register/byEmail` / `register/active` …）
会间歇性返回 **`200 + text/html`** 的 JS 挑战页（实测 17136 B），而不是 JSON。
挑战逻辑要在浏览器里跑 JS、把 `acw_sc__v2` 写进 cookie，**纯 HTTP 拿不到**。

不做这件事的后果不是"报个错"，而是**指错方向**：`src/sso.py` 的 `_post` 会
在 `r.json()` 处抛 `JSONDecodeError`，被上层读成"网络/邮箱错误"，
而真实原因是"被盾拦了"。2026-10-03 实测：槽位代理 / 直连 / 真·直连
**三种出口全部被挑战** ⇒ `run.py` 在 Stage 1 无法注册。

为什么放在 `src/browser/`
------------------------
解盾**必须用真实 Chrome**，而 `src/browser/` 是本项目唯一拥有 playwright 的层
（`src/sso.py` 是纯 HTTP 层）。依赖方向保持单向：`sso` → `browser`（且是延迟 import）。

🔴 playwright **只在函数体内** import —— `tests/test_dependency_surface.py` 把
   `playwright` 列为 FORBIDDEN（测试链不许被拖进这个重依赖 + 它还要另下浏览器）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from .settings import BrowserSettings


def playwright_proxy(px: str | None) -> dict | None:
    """把**原始代理串**转成 Playwright `new_context(proxy=...)` 要的形态。

    接受的写法与 `common/config.py:proxies()` **完全一致**（配置层已经按这套语法
    校验过，这里不另立一套，只是换个目标形态）：

        host:port:user:pass          → {"server": "http://host:port",
                                        "username": …, "password": …}
        scheme://user:pass@host:port → 同上（scheme 原样保留，账密按 URL 解码）
        host:port / scheme://host:port → {"server": "…"}（无账密）
        空串 / None                  → None（`new_context` 不挂代理 = 直连）

    🔴 为什么不能直接把原始串塞进 `proxy={"server": px}`（2026-10-05 实测全灭）
    --------------------------------------------------------------------------
    `sticky_ip_run.py` 用 `host:port:user:pass` 形态的代理连打 10 次，**10/10**
    都是 `register: Browser.new_context: Invalid URL`。两个独立的坑：

      1. `host:port:user:pass` **不是 URL** ⇒ Chromium 侧直接 `Invalid URL`。
         而 `config.proxies()` 明确支持这种写法（它是 requests 的常见形态）。
      2. 就算补上 scheme，把账密**内嵌**在 server 里也不行 —— 实测
         `{"server": "http://user:pass@h:9091"}` 会变成
         `net::ERR_INVALID_AUTH_CREDENTIALS`。Playwright 要求
         `server` / `username` / `password` **分字段**。

    为什么这个坑一直没露出来：主流水线的槽位 URL 恰好是 `http://127.0.0.1:7901`
    （无账密、已是 URL）⇒ 两条路都走得通。只有 `IR_PROXY` 走
    `host:port:user:pass` 时才炸，而那条路是 `.env` 文档允许的。

    ⚠ 这是代理串解析的**第二处实现**（第一处 `common/config.py:proxies()`）。
      浏览器层**不能** import `config` —— 那正是阶段 A 消掉的回边
      （见 `settings.py` 与 `tests/test_dependency_surface.py`）⇒ 语法知识
      在这里必须复制一份。为防漂移，`tests/test_waf_proxy_parsing.py` 用同一张
      用例表钉住两边**接受的写法集合**。

    ⚠ 语法不认识就**抛** `ValueError`（与 `config.proxies()` 同款），不返回 None：
      返回 None 会被上层读成"直连"，于是真正的问题（配置写错）伪装成
      "盾没解开"—— 方向错得和本项目最警惕的那类失败一样。
    """
    raw = (px or "").strip()
    if not raw:
        return None

    if "://" in raw:
        u = urlsplit(raw)
        if not u.hostname:
            raise ValueError(f"代理串缺少主机名：{raw!r}（期望 scheme://[user:pass@]host:port）")
        d: dict[str, str] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            d["username"] = unquote(u.username)
            # `password` 可能是 None（`scheme://user@host:port`），Playwright 要字符串
            d["password"] = unquote(u.password or "")
        return d

    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": pwd}
    if len(parts) == 2:
        return {"server": f"http://{raw}"}
    raise ValueError(
        f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass 或 scheme://user:pass@host:port）"
    )


def solve_acw_challenge(challenge_html: str, proxy: str = "", *, settings: BrowserSettings) -> str:
    """把挑战页交给真实 Chrome 当**文档**加载，取回 `acw_sc__v2`。

    拿不到就返回 `""`（**不抛**）—— 由调用方决定怎么报错。这样本函数可以
    在"没有 Chrome"的机器上被安全调用。

    🔴 两个实测坑（2026-09-23，见 `docs/protocol.md`）：
      1. 必须 `page.set_content` 把挑战页当**文档**加载。在既有页面上下文里
         `eval` 不对 —— 环境不对，挑战逻辑**不触发**。
      2. 必须先 `goto` **同域**页面再 `set_content`：保持当前 origin，
         cookie 才写得对域（否则 `ctx.cookies()` 里拿不到）。

    `proxy` 为空表示直连（`new_context` 不挂代理）。槽位池场景必须传具体值 ——
    挑战是**按出口 IP** 下发的，换 IP 解盾没有意义。

    `proxy` 是**原始串**（与 requests 侧同一份配置值），由 `playwright_proxy()`
    转成 Playwright 形态 —— **不能直接塞进去**：`host:port:user:pass` 会
    `Invalid URL`、账密内嵌会 `ERR_INVALID_AUTH_CREDENTIALS`，详见它自己的 docstring。

    `settings`（阶段 A 起必填）：Chrome 路径与 SSO 基础 URL 的注入面 ——
    本模块原先 `from .. import config`（回边），见 `settings.py`。
    调用方（`src/sso.py`）用 `functools.partial` 把它**提前绑好**，
    从而让"解盾器可注入"这个接口（`waf_solver=(html, proxy) -> str`）保持不变。
    """
    from playwright.sync_api import sync_playwright

    from .constants import CHROME_ARGS

    # 代理串先解析：**配置写错**要当场炸，不能等到浏览器起来后伪装成"盾没解开"。
    px = playwright_proxy(proxy)

    if not Path(settings.chrome_path).is_file():
        return ""

    acw = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=settings.chrome_path, headless=True, args=list(CHROME_ARGS)
        )
        try:
            # 显式传参而不是摊开一个 dict：摊开会让类型检查器把整个 dict 的
            # 值类型并集（str|bool|list[str]）套到每个参数上。
            if px:
                ctx = browser.new_context(viewport=None, proxy=px)
            else:
                ctx = browser.new_context(viewport=None)
            page = ctx.new_page()
            page.goto(f"{settings.sso_base}/register", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(800)
            page.set_content(challenge_html, wait_until="domcontentloaded")
            for _ in range(10):
                page.wait_for_timeout(1000)
                acw = next(
                    (
                        str(c.get("value"))
                        for c in ctx.cookies()
                        if c.get("name") == "acw_sc__v2" and c.get("value")
                    ),
                    "",
                )
                if acw:
                    break
        finally:
            browser.close()
    return acw
