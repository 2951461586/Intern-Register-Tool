"""单账号登录入口 —— 每次尝试新建浏览器（`with sync_playwright()`）。

⚠ 与 `session.BrowserSession` 的区别：本入口**不复用浏览器会话**。
  批量场景要用 `BrowserSession` 复用同一个浏览器，省掉每次冷启动。
"""

from .attempt import _run_attempt
from .session import _launch_kwargs, _retry_loop
from .settings import BrowserSettings
from .state import LoginResult


# ────────────────────────────────────────────────────────────────
# 单账号入口（自带浏览器生命周期）
# ────────────────────────────────────────────────────────────────
def login(
    account: str,
    password: str,
    *,
    headless: bool = True,
    timeout: int = 150,
    attempts: int = 3,
    cooldown: float = 15.0,
    screenshot_prefix: str | None = None,
    verbose: bool = False,
    chrome_args=None,
    settings: BrowserSettings,
) -> LoginResult:
    """用真实浏览器登录 SSO，返回 JWT。

    单账号场景用这个；批量场景请用 `BrowserSession` 复用浏览器进程。

    Args:
        account: 邮箱 / 手机号 / 用户名
        password: 明文密码
        headless: 无头模式。**默认 True**（实测可用，3/3 通过，见文件头说明）。
            ⚠ 2026-09-20 起默认无头：要弹窗口排查请显式传 `headless=False`。
        timeout: 单次尝试里验证码阶段的等待上限（秒）
        attempts: 失败后换新会话重试的次数
        cooldown: 两次尝试之间的冷却基数（秒），按次数线性递增
        screenshot_prefix: 若提供，保存过程截图便于排查
        verbose: 打印验证码交互细节
        chrome_args: 自定义 Chrome 启动参数；`None` → 模块级 `CHROME_ARGS`。
            **要注入启动参数请用这个**，不要去改 `CHROME_ARGS` 全局
            （理由见 `_launch_kwargs()`）。
        settings: 浏览器层的外部配置（Chrome 路径 / SSO 与 discovery 基础 URL /
            clientId / source）。**阶段 A 起必填** —— 本包不再 `from .. import config`
            （回边见 `settings.py`）。要现成的一份就
            `BrowserSettings.from_config(config)`。

    Returns:
        LoginResult；`attempts_used` 记录实际用掉几次尝试，`timings` 是各阶段耗时(ms)。
    """
    from playwright.sync_api import sync_playwright

    def run_once(tag):
        with sync_playwright() as p:
            browser = p.chromium.launch(
                **_launch_kwargs(headless, chrome_args, executable_path=settings.chrome_path)
            )
            try:
                return _run_attempt(
                    browser,
                    account=account,
                    password=password,
                    headless=headless,
                    timeout=timeout,
                    settings=settings,
                    screenshot_prefix=screenshot_prefix,
                    verbose=verbose,
                    tag=tag,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    return _retry_loop(
        run_once, attempts=attempts, cooldown=cooldown, verbose=verbose, retry_hint="换新会话重试"
    )
