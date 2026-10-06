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

三条 2026-10-05 的修正（近三轮实测 428 个账号 / 81 个失败）：
  - 429 **仍是第一失败原因**（47/81），而 `_post` 的退避重试原先**绕过了**
    调用方的速率闸门 ⇒ 多线程退避时长相同、重试请求同步撞车。现在
    `_post(retry_gate=…)` 让**每一次重试**都重新过闸门（见 `_post`）。
  - 传输层瞬时错误（SSL EOF / 读超时 / 代理断开）**原先是零重试**，
    直接算账号失败（26/81）。现在与 429/5xx 一起按同一退避重试。
  - 解盾结果原先**每个账号重解一次**（每次启一个真实 Chrome，~5–10s）。
    现在按出口缓存 `acw_sc__v2`（`WafCookieCache`），同一出口的下一个账号直接复用。

⚠ 上面那条 2026-09-15 的「1.2s 边界」是**单出口**测出来的；是否仍成立需要
  用 `tools/probes/probe_reg_interval.py` 按当前出口复测（见 `REG_MIN_INTERVAL`）。
"""

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import requests

from common import config

from . import fsutil
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


# `_post` 的默认重试次数。
#
# 🔴 为什么从 4 提到 6（2026-10-06 实测）：在 5 出口槽位模式下连跑 6 批
#    72 个账号，`429` 是**唯一持续出现的可重试信号**（共 47 次），而**仅有的
#    2 个失败都是“连续 4 发全 429”耗尽预算**。提到 6 覆盖这种“连续几发都被
#    限流”的形态。
#
# ⚠ 代价：最坏耗时从 ~10.5s 涨到 ~42.5s（指数退避封顶 20s，只发生在
#    真要失败的那几条上）。正常账号不会走到第 5/6 发。
POST_ATTEMPTS = 6


def waf_state_path() -> Path:
    """解盾 cookie 的持久化位置。`IR_WAF_STATE` 可覆盖（测试隔离 / 多份状态并存）。

    与 `quota.state_path()` / `proxypool.state_path()` **同一约定**：
    环境变量在**调用时**读，不在 import 期或模块常量里定值 —— 否则测试没法
    把状态重定向到临时文件。

    ⚠ 默认落在 `.workbuddy-ai/state/`（已被 `.gitignore` 排除）：
      文件里是**凭据**（WAF cookie 能在该出口上换取放行），绝不进仓库。
    """
    override = os.getenv("IR_WAF_STATE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / ".workbuddy-ai" / "state" / "waf_cookies.json"


class WafCookieCache:
    """`acw_sc__v2`（WAF 解盾结果）的共享缓存，按出口分桶，**可选持久化到磁盘**。

    🔴 为什么按出口分桶：挑战是**按出口 IP** 下发的，换 IP 解出来的 cookie
       在原来的出口上没用（见 `docs/protocol.md`「写接口的 WAF 挑战」）。

    🔴 为什么缓存 **cookie** 而不是复用 **Session**：`requests.Session` 的
       cookie jar 与连接池**没有跨线程保证**。单出口模式下多个 producer 会
       共用同一个出口 ⇒ 若共享一个 Session，就是多线程同时读写同一个 jar。
       只共享 cookie 值，则每个账号仍各用各的 Session，省掉重复解盾这个大头。
       （槽位模式下 `_SsoPool` 另做了 per-出口 的 client 复用，见 `pipeline`。）

    🔴 为什么要能**落盘**（2026-10-06 新增）：内存缓存的寿命 = **一个进程**，
       而每次 `run.py` 都是新进程 ⇒ 同一出口又得重解一次。实测解盾单价
       **~8.8s**，一批 12 账号固定要 5 次（每出口 1 次）≈ **44s/批**。落盘后
       **跨批次**复用；cookie 失效时服务端照常回挑战页 → 重解并覆盖，自愈。

    ⚠ 这份缓存**不判过期**：加一层 TTL 猜测只会引入新的漂移点（见上）。
    ⚠ 持久化是 **best-effort**：读写失败只记一条告警，绝不让注册跑不动 ——
       与 `proxypool` / `quota` 的状态文件同一策略（见 `src/fsutil.py`）。
    """

    def __init__(self, path=None, *, log=None):
        self._lock = threading.Lock()
        self._acw: dict[str, str] = {}
        self._path = Path(path) if path else None
        self._log = log
        if self._path is not None:
            self._load()

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log(f"⚠ waf_cookie {self._path}: {msg}")

    def _load(self) -> None:
        if self._path is None:  # 只为让类型检查器收窄（调用方已有同样的判断）
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as ex:
            self._warn(f"读不出（{type(ex).__name__}: {str(ex)[:80]}）—— 当作空缓存")
            return
        if isinstance(data, dict):
            self._acw = {str(k): str(v) for k, v in data.items() if v}

    def _persist(self) -> None:
        # ⚠ 调用方必须持锁（`set` 里调）。
        try:
            fsutil.atomic_write_text(
                self._path, json.dumps(self._acw, ensure_ascii=False, indent=2)
            )
        except OSError as ex:
            self._warn(f"写不回（{type(ex).__name__}: {str(ex)[:80]}）—— 只影响下次重解一次盾")

    def get(self, key: str) -> str:
        with self._lock:
            return self._acw.get(key, "")

    def set(self, key: str, acw: str) -> None:
        if not acw:
            return
        with self._lock:
            if self._acw.get(key) == acw:
                return  # 没变就别写盘（同一出口重复解盾时的常见情形）
            self._acw[key] = acw
            if self._path is not None:
                self._persist()


@dataclass
class RegisterResult:
    ok: bool
    sso_uid: str = ""
    email: str = ""
    username: str = ""
    msg_code: str = ""
    msg: str = ""


class SSOClient:
    def __init__(
        self,
        timeout: int | None = None,
        proxy: str | None = None,
        *,
        waf_solver=None,
        waf_cache: WafCookieCache | None = None,
    ):
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
        # 解盾结果的跨账号共享缓存（按出口分桶）。`None` = 不复用（旧行为）。
        # 缓存键就是**出口串** —— 挑战按出口下发，换了出口缓存必须分开。
        self._waf_cache = waf_cache
        self._cache_key = proxy or config.IR_PROXY or ""
        # 埋点（见 `take_stats`）。稀疏字典：没发生的键不出现。
        self._stats: dict = {}
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

    # ── 埋点与退避 ────────────────────────────────────────────
    # 🔴 为什么要这个观测面：优化前只能靠 `register_call` 的**总耗时**反推
    #    "到底是在等退避、还是在启浏览器解盾"，很钝。分开计数后，"解盾花了
    #    多少 / 重试了几次 / 吃了几个 429"直接进台账，改动的效果可以被复核。
    def _bump(self, key: str, n: int = 1) -> None:
        self._stats[key] = self._stats.get(key, 0) + n

    def take_stats(self) -> dict:
        """取出自上次取出以来的埋点计数并**清零**（稀疏：没发生的键不出现）。"""
        out = dict(self._stats)
        self._stats.clear()
        return out

    def _backoff(self, i: int, resp: requests.Response | None = None) -> float:
        """退避秒数：优先用服务端 `Retry-After`，否则指数退避 + 抖动。"""
        ra = resp.headers.get("Retry-After") if resp is not None else None
        try:
            delay = float(ra) if ra else 1.5 * (2**i)
        except (TypeError, ValueError):
            delay = 1.5 * (2**i)
        return min(delay, 20.0) + random.uniform(0, 1.2)

    def _seed_waf_cookie(self) -> None:
        """把**缓存的**解盾 cookie 提前装上，省掉一次"明知会被挑战"的请求。

        ⚠ cookie 可能已过期（阿里云 TTL 未测）。过期时服务端照常回挑战页，
          走正常解盾路径并**覆盖**缓存 —— 自愈，不需要额外的失效逻辑。
        """
        if self._waf_cache is None:
            return
        acw = self._waf_cache.get(self._cache_key)
        if acw:
            self.session.cookies.set("acw_sc__v2", acw, domain="sso.openxlab.org.cn")

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
        self,
        challenge: requests.Response,
        url: str,
        headers: dict,
        payload: dict,
        *,
        retry_gate=None,
    ) -> requests.Response:
        """撞到挑战页 → 解盾、回填 cookie、重试一次。

        挑战是**按出口 IP** 下发的，所以解盾浏览器也走同一个出口（`self.proxy`）——
        换了 IP 解出来的 cookie 在原来的出口上没用。
        解不出来就抛**说人话**的错，而不是让上层去吃 `JSONDecodeError`。

        🔴 解盾结果写进 `self._waf_cache`（按出口分桶）—— 同一出口的**下一个账号**
           因此不必再启一次 Chrome。实测（10-03~10-05，347 个成功账号）：
           `register_call` 中位 14.7s、92% 落在 >10s，大头就是每账号一次解盾。

        🔴 `retry_gate`：解盾后这次重发**也是写请求** —— 重试必须重新过闸门，
           否则多线程同步退避会再撞 429（见 `_post`）。
        """
        t0 = time.time()
        acw = self._solve_acw(challenge.text)
        self._bump("waf_solves")
        self._bump("waf_solve_ms", round((time.time() - t0) * 1000))
        if not acw:
            raise RuntimeError(
                "WAF 解盾失败：接口返回 JS 挑战页，但拿不到 acw_sc__v2。\n"
                f"  出口：{self.proxy or config.IR_PROXY or '直连'}\n"
                f"  修法：确认本机能启动 Chrome（IR_CHROME_PATH={config.CHROME_PATH}）"
                f"且能到达 {config.SSO_BASE}（解盾浏览器要走同一出口代理）。"
            )
        self.session.cookies.set("acw_sc__v2", acw, domain="sso.openxlab.org.cn")
        if self._waf_cache is not None:
            self._waf_cache.set(self._cache_key, acw)
        if retry_gate is not None:
            retry_gate()
        self._bump("retries")
        retried = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
        if _looks_like_waf_challenge(retried):
            raise RuntimeError("WAF 解盾后仍返回挑战页 —— 盾没解开（换出口 / 稍后重试）。")
        return retried

    def _post(
        self,
        path: str,
        payload: dict,
        *,
        referer: str = "/register",
        attempts: int = POST_ATTEMPTS,
        retry_gate=None,
    ) -> requests.Response:
        """带退避重试的 POST。

        🔴 为什么必须有：`register/byEmail` 有写操作限流。实测 4 路并发注册时
        3 路立刻拿到 `429 Too Many Requests`（~1.2s 就返回，不是超时）。
        把 429 当注册失败会让批量任务大面积假失败 —— 它只是"慢点再来"。

        🔴 `attempts` 默认 `POST_ATTEMPTS=6`（2026-10-06 从 4 提到 6，依据见
           那里的注释：仅有的 2 个失败都是 429 连续 4 发耗尽预算）。

        🔴 `retry_gate`（可选 callable）：**每一次重试前**调用。调用方负责
           第 0 次尝试的限速（`stage_register` 的 `_write_gate`）；这里只为
           重试补上限速。原先重试完全绕过闸门 ⇒ 多线程退避时长相同、重试
           请求同步撞车，服务端继续回 429（2026-10-05：47/81 的失败源于此）。
           闸门同时会复查配额信号，抛 `_QuotaAbort` 中止（由调用方接住）。

        🔴 传输层瞬时错误（`requests.exceptions.RequestException`：SSL EOF /
           读超时 / 代理断开）**也走同一条退避重试**。原先它们直接冒泡成
           账号失败 —— 26/81 的失败属于这一类，其中 10 条还是只读接口。

        ⚠ 2026-09-20 删掉了原先的 `auth: str = None` 形参（连同一个
        `if auth: h["Authorization"] = …` 分支）：它唯一的调用者是已删除的
        `internal_auth()`，删后全仓无调用者传 `auth=`（已 grep 确认）。
        """
        if attempts < 1:
            raise ValueError(f"_post: attempts 必须 >= 1（收到 {attempts}）")
        h = self._headers(referer)
        url = f"{self.gw}{path}"
        self._seed_waf_cookie()
        last = None
        for i in range(attempts):
            if i and retry_gate is not None:
                retry_gate()  # 重试也要过闸门（否则同步退避会再撞 429）
            try:
                r = self.session.post(url, headers=h, json=payload, timeout=self.timeout)
            except requests.exceptions.RequestException:
                if i == attempts - 1:
                    raise
                self._bump("transport_retries")
                self._bump("retries")
                time.sleep(self._backoff(i))
                continue
            if _looks_like_waf_challenge(r):
                # 挑战不算失败，不计入 attempts 退避 —— 它要的是解盾不是等待。
                r = self._pass_waf(r, url, h, payload, retry_gate=retry_gate)
            if r.status_code == 429 or r.status_code >= 500:
                last = r
                self._bump("http_429" if r.status_code == 429 else "http_5xx")
                if i == attempts - 1:
                    break
                self._bump("retries")
                time.sleep(self._backoff(i, r))
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
        if last is None:  # 理论不可达：attempts >= 1 时第 0 次必然 return/raise
            raise ValueError(f"_post: attempts 必须 >= 1（收到 {attempts}）")
        last.raise_for_status()
        return last

    # ── 可用性校验 ────────────────────────────────────────────
    def check_username(self, username: str) -> bool:
        """True 表示用户名可用。

        ⚠ 只读接口（实测 8 路并发也不限流）⇒ **不**过写限速闸门。
        """
        r = self._post("/personal/username/check", {"username": username})
        return _json_true(r.json().get("data"))

    # ── 注册 ──────────────────────────────────────────────────
    def register(
        self, username: str, email: str, password: str, *, retry_gate=None
    ) -> RegisterResult:
        payload = {
            "username": username,
            "email": email,
            "password": encrypt_password(email, password),
            "source": config.SOURCE,
            "clientId": config.CLIENT_ID,
        }
        r = self._post("/register/byEmail", payload, retry_gate=retry_gate)
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
    def activate(self, token: str, sign: str, *, retry_gate=None) -> bool:
        r = self._post(
            "/register/active",
            {"token": token, "sign": sign},
            referer="/active",
            retry_gate=retry_gate,
        )
        return _json_true(r.json().get("success"))

    def activate_from_url(self, url: str, *, retry_gate=None) -> bool:
        """从激活链接中解析 token/sign 并激活。"""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        token = (qs.get("token") or [""])[0]
        sign = (qs.get("sign") or [""])[0]
        if not token or not sign:
            raise ValueError(f"activation url missing token/sign: {url}")
        return self.activate(token, sign, retry_gate=retry_gate)
