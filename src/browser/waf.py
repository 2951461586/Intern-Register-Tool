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

from .settings import BrowserSettings


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

    `settings`（阶段 A 起必填）：Chrome 路径与 SSO 基础 URL 的注入面 ——
    本模块原先 `from .. import config`（回边），见 `settings.py`。
    调用方（`src/sso.py`）用 `functools.partial` 把它**提前绑好**，
    从而让"解盾器可注入"这个接口（`waf_solver=(html, proxy) -> str`）保持不变。
    """
    from playwright.sync_api import sync_playwright

    from .constants import CHROME_ARGS

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
            if proxy:
                ctx = browser.new_context(viewport=None, proxy={"server": proxy})
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
