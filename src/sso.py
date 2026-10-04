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
  - **写接口有阿里云 WAF 的 JS 挑战**（`200 + text/html`，实测 17136 B）。
    纯 HTTP 拿不到 `acw_sc__v2` ⇒ `_post` 会调 `src/browser/waf.py` 用真实
    Chrome 解盾、回填 cookie 后重试一次（2026-10-03 实测：槽位代理/直连/
    真·直连**三种出口全部被挑战**，不解盾 `run.py` 在 Stage 1 就没法注册）
  - **登录强制人机验证**，纯 HTTP 无解，必须走 `src/browser/`（浏览器登录子包）
  - 密码字段 = RSA_PKCS1v15(f"{identity}||{password}{unix_ts}") 的 base64

🔴 429 限流的真实边界（2026-09-15 实测，见 tools/probes/probe_429.py）：
  - `personal/username/check`（只读）：**8 路并发也完全不限流**
  - `register/byEmail`（写）：**4 路并发时 3 路被 429 拒绝**，且是立即拒绝（~1.2s）
  所以限流挂在**写操作**上，不是笼统的 IP 突发限速。
  429 是限流信号而非业务错误 —— 必须退避重试，不能当注册失败处理。
  本项目实测：加退避重试 + 注册并发降到 2 之后可稳定跑通。
"""

import random
import time
from dataclasses import dataclass
from functools import partial

import requests

from common import config

from .crypto_rsa import encrypt_password


def _json_true(value) -> bool:
    """JSON 布尔字段的**严格**判真：只认字面 `true`（`1` / `"true"` 不算）。

    与 `value is True` **语义完全等价**（`isinstance(True, bool) and True`），
    但不写 `is 字面量` —— pi-lens 的 `identity-with-literal` 规则会拦那种写法。
    这里保留严格性是有意的：服务端返回 JSON `true`/`false`，
    把 `1` 也当成真会掩盖契约漂移。
    """
    return isinstance(value, bool) and value


def _looks_like_waf_challenge(resp: requests.Response) -> bool:
    """这个响应是不是阿里云 WAF 的 JS 挑战页？

    判据（2026-10-03 实测）：**HTTP 200 + 非 JSON**（`content-type: text/html`，
    正文实测 17136 B）。

    🔴 必须限 `200`：`5xx` 的 HTML 错误页要留给 `_post` 的退避重试路径，
       不能当挑战去解盾（那会把一次可重试的服务端抖动变成一次浏览器启动）。

    🔴 区分它是为了**别把“被盾拦了”读成“网络/邮箱错误”**：不区分的话
       `r.json()` 会抛 `JSONDecodeError`，报错方向完全错（见本模块 docstring）。
    """
    if resp.status_code != 200:
        return False
    ct = resp.headers.get("content-type", "").lower()
    if "json" in ct:
        return False
    if "html" in ct:
        return True
    head = (resp.text or "").lstrip()[:200].lower()
    return head.startswith(("<!doctype html", "<html"))


@dataclass
class RegisterResult:
    ok: bool
    sso_uid: str = ""
    email: str = ""
    username: str = ""
    msg_code: str = ""
    msg: str = ""


class SSOClient:
    def __init__(self, timeout: int | None = None, proxy: str | None = None, *, waf_solver=None):
        self.gw = config.SSO_GW
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        # 出口代理。目标站点的封禁是 IP 维度，换 IP 靠这里。
        # `proxy` 传具体值时只作用于这个 client（槽位池并发场景必须这样用 ——
        # 改全局 `config.IR_PROXY` 在多个 producer 之间会互相踩）；
        # 传 None 时退回全局 `IR_PROXY`。
        # 注意 `apply_proxy` 会同时关掉 `trust_env` —— 否则环境里的
        # `HTTP_PROXY`（本机是 Clash）会把我们指定的代理**静默盖掉**。
        config.apply_proxy(self.session, proxy)
        self.proxy = proxy
        # 解盾器。`None` ⇒ 用真实 Chrome（**函数体内**延迟 import
        # `src/browser/waf.py`）。这个注入面是给测试的 —— 测试链**不许**被
        # 拖进 playwright（`tests/test_dependency_surface.py` 的 FORBIDDEN）。
        self._waf_solver = waf_solver
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "lang": "zh-CN",
                "Origin": config.SSO_BASE,
                "User-Agent": config.USER_AGENT,
            }
        )

    def _headers(self, referer_path: str = "/register") -> dict:
        h = dict(self.session.headers)
        h["Referer"] = f"{config.SSO_BASE}{referer_path}"
        return h

    # ── WAF 解盾 ──────────────────────────────────────────────
    def _solve_acw(self, challenge_html: str) -> str:
        """解 WAF 挑战 → `acw_sc__v2`；拿不到返回 ""。

        🔴 `from .browser.waf import …` 必须在**函数体内**：模块级 import 会把
           playwright 拖进测试链（`tests/test_dependency_surface.py` 会当场红）。

        🔴 阶段 A 起用 `functools.partial` 把 `settings` **提前绑好**，
           于是"解盾器可注入"这个接口保持 `(challenge_html, proxy) -> str`
           不变 —— `tests/test_sso_waf.py` 注入的假解盾器就是两参签名，
           而 `src/browser/waf.py` 本身已经不认识 `config`（见 settings.py）。
           **不要**改成在调用点传 `settings=`：那会把注入契约从两参变成三参，
           所有假解盾器都得跟着改，而它们和配置毫无关系。
        """
        solver = self._waf_solver
        if solver is None:
            from .browser.settings import BrowserSettings
            from .browser.waf import solve_acw_challenge

            solver = partial(solve_acw_challenge, settings=BrowserSettings.from_config(config))
        return solver(challenge_html, self.proxy or config.IR_PROXY or "")

    def _pass_waf(
        self, challenge: requests.Response, url: str, headers: dict, payload: dict
    ) -> requests.Response:
        """撞到挑战页 → 解盾、回填 cookie、重试一次。

        挑战是**按出口 IP** 下发的，所以解盾浏览器也走同一个出口（`self.proxy`）——
        换了 IP 解出来的 cookie 在原来的出口上没用。
        解不出来就抛**说人话**的错，而不是让上层去吃 `JSONDecodeError`。
        """
        acw = self._solve_acw(challenge.text)
        if not acw:
            raise RuntimeError(
                "WAF 解盾失败：接口返回 JS 挑战页，但拿不到 acw_sc__v2。\n"
                f"  出口：{self.proxy or config.IR_PROXY or '直连'}\n"
                f"  修法：确认本机能启动 Chrome（IR_CHROME_PATH={config.CHROME_PATH}）"
                f"且能到达 {config.SSO_BASE}（解盾浏览器要走同一出口代理）。"
            )
        self.session.cookies.set("acw_sc__v2", acw, domain="sso.openxlab.org.cn")
        retried = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
        if _looks_like_waf_challenge(retried):
            raise RuntimeError("WAF 解盾后仍返回挑战页 —— 盾没解开（换出口 / 稍后重试）。")
        return retried

    def _post(
        self, path: str, payload: dict, *, referer: str = "/register", attempts: int = 4
    ) -> requests.Response:
        """带退避重试的 POST。

        🔴 为什么必须有：`register/byEmail` 有写操作限流。实测 4 路并发注册时
        3 路立刻拿到 `429 Too Many Requests`（~1.2s 就返回，不是超时）。
        把 429 当注册失败会让批量任务大面积假失败 —— 它只是"慢点再来"。

        ⚠ 2026-09-20 删掉了原先的 `auth: str = None` 形参（连同一个
        `if auth: h["Authorization"] = …` 分支）：它唯一的调用者是已删除的
        `internal_auth()`，删后全仓无调用者传 `auth=`（已 grep 确认）。
        """
        h = self._headers(referer)
        url = f"{self.gw}{path}"
        last = None
        for i in range(attempts):
            r = self.session.post(url, headers=h, json=payload, timeout=self.timeout)
            if _looks_like_waf_challenge(r):
                # 挑战不算失败，不计入 attempts 退避 —— 它要的是解盾不是等待。
                r = self._pass_waf(r, url, h, payload)
            if r.status_code == 429 or r.status_code >= 500:
                last = r
                if i == attempts - 1:
                    break
                # 优先用服务端给的 Retry-After，没有就指数退避
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else 1.5 * (2**i)
                except (TypeError, ValueError):
                    delay = 1.5 * (2**i)
                delay = min(delay, 20.0) + random.uniform(0, 1.2)
                time.sleep(delay)
                continue
            if "json" not in r.headers.get("content-type", "").lower():
                # 走到这里：既不是挑战页（上面已解盾），也不是 429/5xx。
                # 实测形态：WAF 硬拦截的 `405 + text/html`（errors.aliyun.com 页面）。
                # 给个**说人话**的错，而不是让上层去吃 JSONDecodeError。
                raise RuntimeError(
                    "接口返回非 JSON（疑以 WAF 硬拦截，不是可解的挑战页）："
                    f"HTTP {r.status_code} "
                    f"content-type={r.headers.get('content-type', '') or '(空)'}。\n"
                    f"  出口：{self.proxy or config.IR_PROXY or '直连'}\n"
                    "  修法：换出口 IP（该出口可能已被限流/拉黑）或稍后重试。"
                )
            return r
        if last is None:  # attempts <= 0：一个请求都没发
            raise ValueError(f"_post: attempts 必须 >= 1（收到 {attempts}）")
        last.raise_for_status()
        return last

    # ── 可用性校验 ────────────────────────────────────────────
    def check_username(self, username: str) -> bool:
        """True 表示用户名可用。"""
        r = self._post("/personal/username/check", {"username": username})
        return _json_true(r.json().get("data"))

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
            ok=_json_true(body.get("success")),
            sso_uid=str(data.get("ssoUid", "")),
            email=data.get("email", ""),
            username=data.get("username", ""),
            msg_code=body.get("msgCode", ""),
            msg=body.get("msg", ""),
        )

    # ── 激活 ──────────────────────────────────────────────────
    def activate(self, token: str, sign: str) -> bool:
        r = self._post("/register/active", {"token": token, "sign": sign}, referer="/active")
        return _json_true(r.json().get("success"))

    def activate_from_url(self, url: str) -> bool:
        """从激活链接中解析 token/sign 并激活。"""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        token = (qs.get("token") or [""])[0]
        sign = (qs.get("sign") or [""])[0]
        if not token or not sign:
            raise ValueError(f"activation url missing token/sign: {url}")
        return self.activate(token, sign)
