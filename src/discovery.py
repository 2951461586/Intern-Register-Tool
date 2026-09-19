"""Discovery 平台 API 客户端 —— 领取免费额度并创建 API Key。

鉴权机制（易踩坑）：
  - 平台有两套风格混用：
      * user-center 接口把 JWT 放在 **请求体** `{"jwt": "..."}` 里
      * tokenplan 接口用 **请求头** `Authorization: Bearer <jwt>`
  - 用错鉴权方式时返回 `{"code":-10002,"msg":"参数错误，请求未认证"}`；
    而 `Authorization: Bearer` 若 token 无效则返回 SSO 网关格式
    `{"traceId":...,"msgCode":"A0211","msg":"user token expired"}`。
    两者错误结构不同，可据此判断鉴权头是否正确送达。
  - **必须带 `Origin` + `Referer`**。早期只带 `Authorization` 会稳定拿到
    `-10002`；补上这两个头之后鉴权即通过（HAR 中这些请求本身
    `cookies: []`，即 Cookie 并非必要条件）。

🔴 POST /tokenplan/v1/keys 必须带 `Idempotency-Key`（UUID v4）：
    这是本项目最后一个、也最隐蔽的坑。缺少该头时服务端建不了幂等记录，
    不会报"缺少参数"，而是回落成通用业务错误：
        {"code":-15100,"msg":"API Key 获取失败，请刷新页面重试"}
    这个提示把方向引向"额度没到账 / 需要刷新"，实测等待 8 秒重试无效、
    改用 code 换来的 token 也无效 —— 真正的差异只是少了一个请求头。
    抓包证据：HAR 中 POST /keys 的请求头含
        Idempotency-Key: 9ebccda8-c0c0-48fc-a694-a54f15a89805
    而同一会话里的 GET /keys、POST free-grant 都没有该头，只有建 Key 有。

业务流程：
  1. POST /api/user-center/v1/users/auth   {"code": "uaa::code::xxx"}  -> {token}
     ⚠ 实测该接口返回的 token 与 `login/byAccount` 响应头 `authorization`
       里的 JWT **逐字符相同**，因此登录后直接复用即可，无需再走 code 交换。
  2. GET  /api/tokenplan/v1/users/free-grant-status
  3. POST /api/tokenplan/v1/users/free-grant        （领取免费套餐）
  4. POST /api/tokenplan/v1/keys           {"name": "..."}  -> {key: "sk-..."}
"""

import uuid
from dataclasses import dataclass

import requests

from . import config


@dataclass
class ApiKey:
    id: str
    name: str
    key: str
    masked_key: str
    status: str
    created_at: str


class DiscoveryClient:
    def __init__(self, jwt: str = "", cookies: dict = None, timeout: int = None):
        self.base = config.DISCOVERY_API
        self.jwt = jwt
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        # 出口代理（`IR_PROXY`）。目标站点的封禁是 IP 维度，换 IP 靠这里。
        # `apply_proxy` 会同时关掉 `trust_env`，理由见它的 docstring。
        config.apply_proxy(self.session)
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": config.DISCOVERY_BASE,
            "Referer": f"{config.DISCOVERY_BASE}/token-plan/home?tabIndex=0",
            "accept-language": "zh-CN",
            "User-Agent": config.USER_AGENT,
            # 以下头逐条对齐 HAR 里的真实浏览器请求，缺 Origin/Referer 会稳定 -10002
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        if cookies:
            self.session.cookies.update(cookies)
        self._apply_auth()

    def set_cookies(self, cookies: dict):
        if cookies:
            self.session.cookies.update(cookies)

    def _apply_auth(self):
        if self.jwt:
            self.session.headers["Authorization"] = f"Bearer {self.jwt}"
        else:
            self.session.headers.pop("Authorization", None)

    def set_jwt(self, jwt: str):
        self.jwt = jwt
        self._apply_auth()

    # ── 鉴权 ──────────────────────────────────────────────────
    def exchange_code_for_jwt(self, code: str) -> str:
        """用 SSO 的 uaa code 换取 discovery 的 JWT。"""
        r = self.session.post(f"{self.base}/user-center/v1/users/auth",
                              json={"code": code}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        token = (data.get("data") or {}).get("token", "")
        if not token:
            raise RuntimeError(f"exchange_code_for_jwt failed: {data}")
        self.set_jwt(token)
        return token

    def get_user_info(self) -> dict:
        """用户信息 —— 注意此接口 JWT 走 body。"""
        r = self.session.post(f"{self.base}/user-center/v1/users/getUserInfo",
                              json={"jwt": self.jwt}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or {}

    # ── 额度 ──────────────────────────────────────────────────
    def free_grant_status(self) -> dict:
        r = self.session.get(f"{self.base}/tokenplan/v1/users/free-grant-status",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or {}

    def claim_free_grant(self) -> dict:
        r = self.session.post(f"{self.base}/tokenplan/v1/users/free-grant",
                              timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or {}

    def balance(self) -> dict:
        r = self.session.get(f"{self.base}/tokenplan/v1/credits/balance",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or {}

    # ── API Key ───────────────────────────────────────────────
    def list_keys(self) -> list[dict]:
        r = self.session.get(f"{self.base}/tokenplan/v1/keys", timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        # 🔴 不要静默吞掉错误码。不带浏览器 Cookie 时这里是
        #    {"code":-10002,"msg":"request is not authenticated"}，
        #    旧实现直接 .get("data",{}).get("items",[]) 会返回空列表，
        #    表现为"这个账号还没有 Key"，而真相是鉴权失败 —— 会误导排查方向。
        if body.get("code") not in (0, None):
            raise RuntimeError(
                f"list_keys failed: code={body.get('code')} msg={body.get('msg')}"
            )
        return (body.get("data") or {}).get("items", []) or []

    def create_key(self, name: str = "default") -> ApiKey:
        # 🔴 必须带 Idempotency-Key（UUID v4），否则返回 -15100 通用错误。
        #    见模块 docstring 的详细说明。
        r = self.session.post(
            f"{self.base}/tokenplan/v1/keys",
            json={"name": name},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        if not data.get("key"):
            raise RuntimeError(f"create_key failed: {r.text[:300]}")
        return ApiKey(
            id=data.get("id", ""), name=data.get("name", ""), key=data.get("key", ""),
            masked_key=data.get("masked_key", ""), status=data.get("status", ""),
            created_at=data.get("created_at", ""),
        )

    def ensure_key(self, name: str = "default") -> ApiKey:
        """幂等创建：已存在同名 key 则直接返回（但列表不含明文 key）。"""
        for it in self.list_keys():
            if it.get("name") == name:
                return ApiKey(
                    id=it.get("id", ""), name=it.get("name", ""), key="",
                    masked_key=it.get("masked_key", ""), status=it.get("status", ""),
                    created_at=it.get("created_at", ""),
                )
        return self.create_key(name)
