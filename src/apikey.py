"""API Key 推理网关客户端（OpenAI 兼容）。

🔴 主机名容易搞错，这是本项目的一个坑：
  - `https://discovery-api.intern-ai.org.cn/v1`  ← **接受 sk- key**，本模块用这个
  - `https://chat.intern-ai.org.cn/api/v1`       ← 网页版聊天后端，只认 SSO JWT，
                                                    且要求账号绑定手机号（-20035）
  拿 sk- key 去打 chat.intern-ai.org.cn 会得到
  `401 {"msgCode":"A0211","msg":"user token expired"}` —— 这个提示会让人误以为
  key 无效或未生效，实际上只是打错了主机。

模型名同样有坑：`intern-s1` 不在 TokenPlan 的可用清单里，用它返回
`model_not_available: intern-s1 is not supported by TokenPlan`。
可用清单见 `config.CHAT_MODELS`（也可用 `list_models()` 动态获取）。
"""

import time
from dataclasses import dataclass, field

import requests

from common import config


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""
    # 🔴 `ok=True` 只代表"网关接受了并给了 choices"，**不代表正文非空**。
    # 本项目实测（2026-09-16）：`deepseek-v4-flash-0731` 是**带 reasoning 的模型**，
    # 响应里除了 `content` 还有 `reasoning_content`。`max_tokens=32` 时
    # `reasoning_tokens` 就吃了 34 → `content` 空、`finish_reason="length"`。
    # 只看 `ok` 会把这种情况当成"模型没说话"；有 finish_reason 才能分辨
    # "被截断"和"真的没内容"。
    finish_reason: str = ""
    reasoning: str = ""

    @property
    def truncated(self) -> bool:
        """正文被 max_tokens 截断（不是 key 或模型的问题）。"""
        return self.finish_reason == "length"


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def list_models(key: str, timeout: int = 30) -> list[str]:
    """列出该 key 可用的模型 id。"""
    r = requests.get(f"{config.CHAT_API_BASE}/models",
                     headers=_headers(key), timeout=timeout,
                     proxies=config.proxies())
    r.raise_for_status()
    return [m.get("id", "") for m in (r.json().get("data") or []) if m.get("id")]


def verify_key(key: str, timeout: int = 30) -> bool:
    """轻量校验：能列模型就说明 key 有效。"""
    try:
        return bool(list_models(key, timeout=timeout))
    except Exception:
        return False


def wait_until_active(key: str, *, attempts: int = 4, delay: float = 6.0,
                      timeout: int = 30) -> bool:
    """等 key 在网关侧生效。

    🔴 新建的 key 有传播延迟：`POST /tokenplan/v1/keys` 已返回 sk-...，
    但立刻拿去打 `/v1/models` 会得到 401。实测约 10 秒后即正常。
    这不是 key 无效，也不是代码 bug —— 直接判定失败会误报。
    """
    for i in range(attempts):
        if verify_key(key, timeout=timeout):
            return True
        if i < attempts - 1:
            time.sleep(delay)
    return False


def chat(key: str, prompt: str, *, model: str | None = None, timeout: int = 180,
         max_tokens: int = 256) -> ChatResult:
    """发一次对话补全。"""
    model = model or (config.CHAT_MODELS[0] if config.CHAT_MODELS else "")
    try:
        r = requests.post(
            f"{config.CHAT_API_BASE}/chat/completions",
            headers=_headers(key),
            json={"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
            proxies=config.proxies(),
        )
        if r.status_code != 200:
            return ChatResult(ok=False, model=model, error=f"{r.status_code} {r.text[:200]}")
        d = r.json()
        choices = d.get("choices") or []
        if not choices:
            return ChatResult(ok=False, model=model, error=f"no choices: {str(d)[:200]}")
        ch0 = choices[0] or {}
        msg = ch0.get("message") or {}
        return ChatResult(ok=True, text=msg.get("content") or "",
                          model=d.get("model", model), usage=d.get("usage") or {},
                          finish_reason=ch0.get("finish_reason") or "",
                          reasoning=msg.get("reasoning_content") or "")
    except Exception as ex:
        return ChatResult(ok=False, model=model, error=f"{type(ex).__name__}: {ex}"[:200])


# ── 存活性探针的判定策略 ────────────────────────────────────────────
#
# 为什么抽成纯函数放 `src/`，而不是内联在 tools/ops/check_keys_alive.py 里
# -------------------------------------------------------------------------
# 那个工具是 CLI，**测试链不 import 它**（它要发真实网络请求）。判据一旦内联，
# 就只剩"跑一遍看输出"这一种验证方式 —— 而它的失效形态恰恰是**输出看着合理**。
#
# 2026-09-22 实测的代价：旧版把 429 归进 `error`，复核时并发 8 打出去，
# 47 把里 37 把是 429，汇总行读起来像"只有 10 把 key 能用"。
# 换成串行 + 间隔 5s 后 **97/97 全活、`dead` 0** —— 那 37 个全是**复核自己造成的**。
# ⇒ 429 必须单独成一档，且"退避多久"也得是能单独测的判据。

VERDICT_ALIVE = "alive"
VERDICT_DEAD = "dead"
VERDICT_RATE_LIMITED = "rate_limited"
VERDICT_ERROR = "error"

# 🔴 **只有**这两个码是确定性拒绝 —— 只有它们配叫「key 死了」。
#    其余（超时 / 5xx / 代理抖动 / 限流）都是**状态未知**，混进 `dead` 会凭空造故障。
DEAD_STATUS = (401, 403)
RATE_LIMIT_STATUS = 429

# 429 退避封顶：再久也不该超过一个"短窗口限流"的量级。
RATE_LIMIT_BACKOFF_CAP = 120.0


def verdict_of_status(code: int | None) -> str:
    """把探针拿到的 HTTP 状态码映射成四档结论之一。

    | 码 | 结论 | 含义 |
    |---|---|---|
    | 200 | `alive` | 鉴权通过、网关能列模型 |
    | 401 / 403 | `dead` | 鉴权被**确定性**拒绝 ⇒ 这把 key 真死了 |
    | 429 | `rate_limited` | **你打太快了**，与 key 无关 ⇒ 存活状态**未知** |
    | 其它 / None | `error` | 状态未知（超时、5xx、代理抖动） |

    ⚠ `rate_limited` 与 `error` 都**不是** `dead`。把 429 读成"key 不可用"
      是本项目最容易犯的误读（见本节顶部的实测）。
    """
    if code == 200:
        return VERDICT_ALIVE
    if code in DEAD_STATUS:
        return VERDICT_DEAD
    if code == RATE_LIMIT_STATUS:
        return VERDICT_RATE_LIMITED
    return VERDICT_ERROR


def rate_limit_backoff(attempt: int, *, base: float = 20.0) -> float:
    """命中 429 后，第 `attempt` 次重试（0-based）之前该睡多少秒。

    **线性加长 + 封顶**：`base × (attempt + 1)`，不超过 `RATE_LIMIT_BACKOFF_CAP`。

    为什么线性而不是指数：429 是**按 IP 累积的短窗口限流**，窗口会自己滑走，
    目标只是"让这一轮打出去的请求密度降下来"，不是"等一个越来越坏的东西好起来"。
    指数退避在这里会把一个 5 秒的窗口等成几分钟（本项目在槽位池那边吃过这个亏）。

    ⚠ 与"网络抖动"的处置**相反**：抖动该按固定间隔重试（抖动是随机的，
      不是"这个出口越来越坏"的证据）；429 该加长间隔（它**就是**"你现在太频繁"的证据）。
    """
    return min(base * (attempt + 1), RATE_LIMIT_BACKOFF_CAP)
