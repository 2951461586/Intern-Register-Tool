"""登录页 URL 构造 —— 只依赖 `config`，是纯叶子。

🔴 为什么它必须是**独立的叶子模块**
-----------------------------------
`_step_open_form()`（在 `attempt.py`）要调用它。若按直觉把它放进 `entry.py`
（和 `login()` 放一起），依赖图就变成：

    attempt  --用 build_login_url-->  entry
    entry    --用 _run_attempt----->  attempt      ← 循环导入

所以它必须落在 `attempt` 这一层或更低。`urls.py` 不 import 本包任何东西，
永远不会有环。
"""

from .. import config


def build_login_url() -> str:
    redirect = (
        f"{config.DISCOVERY_BASE}/token-plan/home?tabIndex=0"
        f"&clientId={config.CLIENT_ID}&source={config.SOURCE}"
    )
    return f"{config.SSO_BASE}/login?redirect={redirect}"
