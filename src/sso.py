"""OpenXLab SSO 客户端 —— 注册、激活、登录（纯 HTTP 部分）。

接口清单（均以 /gw/uaa-be 为前缀）：
  POST /api/v1/register/check            {"item": email, "type": "email"} -> {"exist": bool}
  POST /api/v1/personal/username/check   {"username": "..."}              -> bool（true=可用）
  POST /api/v1/cipher/getPubKey          {"type": "register", "from": "browser"}
  POST /api/v1/register/byEmail          {"username","email","password","source","clientId"}
  POST /api/v1/register/active           {"token": "...", "sign": "..."}  （body 即 URL query 对象）
  POST /api/v1/login/byAccount           {"account","password","autoLogin"}  ← 强制人机验证
  POST /api/v1/internal/auth             {"clientId": "..."} -> {"code": "uaa::code::xxx"}

关键结论：
  - **注册/激活不需要人机验证**（失败时报 A0216 密码解密失败，而非 B0501 人机验证失败）
  - **登录强制人机验证**，纯 HTTP 无解，必须走 browser_login
  - 密码字段 = RSA_PKCS1v15(f"{identity}||{password}{unix_ts}") 的 base64

🔴 429 限流的真实边界（2026-09-15 实测，见 .workbuddy-ai/tmp/probe_429.py）：
  - `personal/username/check`（只读）：**8 路并发也完全不限流**
  - `register/byEmail`（写）：**4 路并发时 3 路被 429 拒绝**，且是立即拒绝（~1.2s）
  所以限流挂在**写操作**上，不是笼统的 IP 突发限速。
  429 是限流信号而非业务错误 —— 必须退避重试，不能当注册失败处理。
  本项目实测：加退避重试 + 注册并发降到 2 之后可稳定跑通。
"""

import random
import time
from dataclasses import dataclass

import requests

from . import config
from .crypto_rsa import encrypt_password


@dataclass
class RegisterResult:
    ok: bool
    sso_uid: str = ""
    email: str = ""
    username: str = ""
    msg_code: str = ""
    msg: str = ""


class SSOClient:
    def __init__(self, timeout: int = None):
        self.gw = config.SSO_GW
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "lang": "zh-CN",
            "Origin": config.SSO_BASE,
            "User-Agent": config.USER_AGENT,
        })

    def _headers(self, referer_path: str = "/register") -> dict:
        h = dict(self.session.headers)
        h["Referer"] = f"{config.SSO_BASE}{referer_path}"
        return h

    def _post(self, path: str, payload: dict, *, referer: str = "/register",
              auth: str = None, attempts: int = 4) -> requests.Response:
        """带退避重试的 POST。

        🔴 为什么必须有：`register/byEmail` 有写操作限流。实测 4 路并发注册时
        3 路立刻拿到 `429 Too Many Requests`（~1.2s 就返回，不是超时）。
        把 429 当注册失败会让批量任务大面积假失败 —— 它只是"慢点再来"。
        """
        h = self._headers(referer)
        if auth:
            h["Authorization"] = f"Bearer {auth}"
        url = f"{self.gw}{path}"
        last = None
        for i in range(attempts):
            r = self.session.post(url, headers=h, json=payload, timeout=self.timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = r
                if i == attempts - 1:
                    break
                # 优先用服务端给的 Retry-After，没有就指数退避
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else 1.5 * (2 ** i)
                except (TypeError, ValueError):
                    delay = 1.5 * (2 ** i)
                delay = min(delay, 20.0) + random.uniform(0, 1.2)
                time.sleep(delay)
                continue
            return r
        last.raise_for_status()
        return last

    # ── 可用性校验 ────────────────────────────────────────────
    def check_email(self, email: str) -> bool:
        """True 表示该邮箱已注册。"""
        r = self._post("/register/check", {"item": email, "type": "email"})
        return bool((r.json().get("data") or {}).get("exist"))

    def check_username(self, username: str) -> bool:
        """True 表示用户名可用。"""
        r = self._post("/personal/username/check", {"username": username})
        return r.json().get("data") is True

    # ── 注册 ──────────────────────────────────────────────────
    def register(self, username: str, email: str, password: str) -> RegisterResult:
        payload = {
            "username": username,
            "email": email,
            "password": encrypt_password(email, password),
            "source": config.SOURCE,
            "clientId": config.CLIENT_ID,
        }
        r = self._post("/register/byEmail", payload)
        body = r.json()
        data = body.get("data") or {}
        return RegisterResult(
            ok=body.get("success") is True,
            sso_uid=str(data.get("ssoUid", "")),
            email=data.get("email", ""),
            username=data.get("username", ""),
            msg_code=body.get("msgCode", ""),
            msg=body.get("msg", ""),
        )

    # ── 激活 ──────────────────────────────────────────────────
    def activate(self, token: str, sign: str) -> bool:
        r = self._post("/register/active", {"token": token, "sign": sign},
                       referer="/active")
        return r.json().get("success") is True

    def activate_from_url(self, url: str) -> bool:
        """从激活链接中解析 token/sign 并激活。"""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        token = (qs.get("token") or [""])[0]
        sign = (qs.get("sign") or [""])[0]
        if not token or not sign:
            raise ValueError(f"activation url missing token/sign: {url}")
        return self.activate(token, sign)

    # ── 登录取 code（需已登录态；纯 HTTP 下会被验证码拦截）────
    def internal_auth(self, jwt: str) -> str:
        """用 SSO JWT 换取 clientId 对应的 code。"""
        r = self._post("/internal/auth", {"clientId": config.CLIENT_ID},
                       referer="/authentication", auth=jwt)
        return (r.json().get("data") or {}).get("code", "")
