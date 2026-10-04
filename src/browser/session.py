"""浏览器会话与重试循环。

- `_launch_kwargs()` 是阶段 A 引入的**注入面** —— `chrome_args` 为 `None` 时
  用 `constants.CHROME_ARGS`，否则用传入值。这让探针不必再改写模块全局。
- `_retry_loop()` 消掉了 `login()` 与 `BrowserSession.login()` 约 20 行重复。

🔴 2026-10-04（阶段 A）：`executable_path`（= `config.CHROME_PATH`）改为
  显式参数、`BrowserSession` 改为接收 `settings: BrowserSettings` ——
  本模块不再 `from .. import config`（回边见 `settings.py`）。
"""

import random
import time

from .attempt import _run_attempt
from .constants import CHROME_ARGS
from .settings import BrowserSettings
from .state import LoginResult


# ────────────────────────────────────────────────────────────────
# 启动参数与重试循环（两个入口共用）
# ────────────────────────────────────────────────────────────────
def _launch_kwargs(headless: bool, chrome_args=None, *, executable_path: str) -> dict:
    """组装 `chromium.launch()` 的参数。

    🔴 `chrome_args` 必须是**显式参数**，不能只靠调用方去改模块级 `CHROME_ARGS`。

    探针 `tools/probes/probe_headless.py` 原先就是这么干的：
    `bl.CHROME_ARGS = BASE_ARGS + extra_args`。那是"改模块全局命名空间"的写法，
    依赖"模块属性赋值 == 改该模块的全局变量"这条 Python 语义。它现在能工作，
    但只要本模块被**拆包或加兼容壳**，这种注入就会**静默失效** ——
    改的是壳的属性，真源没变，而且不报错，探针会照常跑完并给出失真的结论。

    改成参数后，注入面不再依赖模块内部布局。`None` → 用模块级 `CHROME_ARGS`，
    所以旧调用方零改动。

    🔴 `executable_path` 是**必填**的 keyword-only：它就是
      `config.CHROME_PATH`，本来从 `config` 读，是那条回边的一部分。
      做成必填而不是默认 `None`，是为了让"忘记注入"在**调用点**就报 TypeError，
      而不是拿着一个空路径去启动浏览器、在很远的地方抛一个看不出真因的错。
    """
    return dict(
        executable_path=executable_path,
        headless=headless,
        args=CHROME_ARGS if chrome_args is None else list(chrome_args),
    )


def _retry_loop(run_once, *, attempts: int, cooldown: float, verbose: bool,
                retry_hint: str) -> LoginResult:
    """失败重试的公共骨架。

    `run_once(tag) -> LoginResult` 由调用方提供，它自己负责浏览器的创建或复用 ——
    这正是两个入口**唯一**的差别：

      - `login()`                 每次尝试新建浏览器（`with sync_playwright()`）
      - `BrowserSession.login()`  复用同一个浏览器，每次只换 context

    原实现把这段循环抄了两遍（约 20 行 × 2），差异只有末句文案，即 `retry_hint`。

    ⚠ 冷却用 `time.sleep`：此刻处于两次尝试之间，没有待泵送的页面网络事件
      （`_run_attempt` 已经返回、context 已关闭或被新 context 取代）。
      这是**唯一**可以安全 sleep 的位置 —— `_run_attempt` 内部一律用
      `page.wait_for_timeout`（见文件头第 3 条）。
    """
    last = LoginResult(ok=False, reason="not attempted")
    for i in range(max(1, attempts)):
        tag = f"_a{i + 1}"
        if verbose and attempts > 1:
            print(f"    [login] === 尝试 {i + 1}/{attempts} ===", flush=True)
        res = run_once(tag)
        res.attempts_used = i + 1
        if res.ok:
            return res
        last = res
        if i < attempts - 1:
            wait = cooldown * (i + 1) + random.uniform(0, 5)
            if verbose:
                print(f"    [login] 第 {i + 1} 次失败（{res.reason}），"
                      f"冷却 {wait:.0f}s 后{retry_hint}", flush=True)
            time.sleep(wait)
    return last


# ────────────────────────────────────────────────────────────────
# 浏览器复用会话
# ────────────────────────────────────────────────────────────────
class BrowserSession:
    """复用一个浏览器进程，每个账号开一个全新 context。

    Chrome 冷启动约 1.5~2s，批量场景下这笔开销乘以账号数。
    复用浏览器只换 context（cookie / localStorage / 缓存全新建），
    会话隔离性与新开浏览器等价 —— 风控看到的是新会话，不是新进程。

    用法：
        with BrowserSession(headless=True) as sess:
            for acct in accounts:
                res = sess.login(acct.email, acct.password, verbose=True)

    ⚠ Playwright 同步 API 不能跨线程共享。多线程并发时，
      **每个线程各自建一个 BrowserSession**。

    `chrome_args`：自定义 Chrome 启动参数，`None` → 模块级 `CHROME_ARGS`。
    理由见 `_launch_kwargs()` —— **探针要注入启动参数请用它，别改全局**。

    🔴 `settings` 是**必填**的 keyword-only（阶段 A 起）：一个账号都不要的
      浏览器启动路径也是从它读的。要现成的一份就
      `BrowserSettings.from_config(config)`，见 `settings.py`。
    """

    def __init__(self, headless: bool = True, chrome_args=None, *,
                 settings: BrowserSettings):
        self.headless = headless
        self.chrome_args = chrome_args
        self.settings = settings
        self._pw = None
        self._browser = None
        self.launch_ms = 0

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        t0 = time.time()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            **_launch_kwargs(self.headless, self.chrome_args,
                             executable_path=self.settings.chrome_path))
        self.launch_ms = round((time.time() - t0) * 1000)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 显式写三个参数而不是 `*exc`：两者语义等价（本方法不读这三个值、
        # 返回 False 让异常继续传播），但 pi-lens 的 `no-dunder-exit-wrong-arity`
        # 规则会把 `*exc` 当成"只有一个额外参数"从而误报。
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False

    def login(self, account: str, password: str, *, timeout: int = 150,
              attempts: int = 3, cooldown: float = 15.0,
              screenshot_prefix: str | None = None, verbose: bool = False) -> LoginResult:
        """在复用的浏览器上登录，失败换新 context 重试。"""
        def run_once(tag):
            return _run_attempt(self._browser, account=account, password=password,
                                headless=self.headless, timeout=timeout,
                                settings=self.settings,
                                screenshot_prefix=screenshot_prefix,
                                verbose=verbose, tag=tag)

        return _retry_loop(run_once, attempts=attempts, cooldown=cooldown,
                           verbose=verbose, retry_hint="重试")
