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

from . import config


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


def chat(key: str, prompt: str, *, model: str = None, timeout: int = 180,
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
