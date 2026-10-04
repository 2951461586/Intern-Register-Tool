"""登录页 URL 构造 —— 只依赖注入进来的 `BrowserSettings`，是纯叶子。

🔴 为什么它必须是**独立的叶子模块**
-----------------------------------
`_step_open_form()`（在 `attempt.py`）要调用它。若按直觉把它放进 `entry.py`
（和 `login()` 放一起），依赖图就变成：

    attempt  --用 build_login_url-->  entry
    entry    --用 _run_attempt----->  attempt      ← 循环导入

所以它必须落在 `attempt` 这一层或更低。`urls.py` 不 import 本包任何东西，
永远不会有环。

🔴 2026-10-04（阶段 A）：URL 的四个来源参数改为**从边界注入**，不再
   `from .. import config`。理由见 `settings.py` —— 那条回边让
   `src <-> src/browser` 成环。
"""

from .settings import BrowserSettings


def build_login_url(settings: BrowserSettings) -> str:
    redirect = (
        f"{settings.discovery_base}/token-plan/home?tabIndex=0"
        f"&clientId={settings.client_id}&source={settings.source}"
    )
    return f"{settings.sso_base}/login?redirect={redirect}"
