"""CF Worker 临时邮箱客户端。

远端 Worker 已实现「激活链接自动提取」，邮件到达后
`extracted_json` 字段直接给出候选链接，无需自行解析 HTML。

API（均需 X-Admin-Token）：
  POST /api/mailboxes           {"domain": "...", "count": N} -> {"emails": [...]}
  GET  /api/inbox?email=<addr>  按收件人列出邮件（走 idx_emails_to_address 索引）
  GET  /admin/all?limit=N       列出最新 N 封（全表，不支持按收件人过滤）
  GET  /admin/msg?id=&email=    单封邮件详情
  DELETE /admin/delete?id=&email=

踩坑记录：
  - 鉴权头同时支持 `X-Admin-Token` 与 `Authorization: Bearer`，两个都带最稳
  - **收信一律走 `/api/inbox?email=`**，别用 `/admin/all` —— 理由见 `list_mails`
  - 邮件通常在注册后 3 秒内到达，但首次请求偶发返回空列表，需要轮询
"""

import json
import os
import time
from dataclasses import dataclass

import requests

from . import config


@dataclass
class Mail:
    id: str
    to_address: str
    from_address: str
    subject: str
    body: str
    extracted_json: str
    received_at: int

    @property
    def links(self) -> list[str]:
        """解析 Worker 已提取的链接列表。"""
        try:
            items = json.loads(self.extracted_json or "[]")
        except (ValueError, TypeError):
            return []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("value"):
                out.append(str(it["value"]))
        return out

    def find_link(self, *keywords: str) -> str | None:
        """按关键词匹配链接（默认匹配激活类）。"""
        kws = [k.lower() for k in keywords] or ["active", "activat"]
        for url in self.links:
            low = url.lower()
            if any(k in low for k in kws):
                return url
        return None


class TempMailClient:
    def __init__(self, base: str = None, token: str = None, timeout: int = None):
        self.base = (base or config.WORKER_BASE).rstrip("/")
        self.token = token or config.WORKER_ADMIN_TOKEN
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        # 🔴 邮箱 Worker **默认不走代理**（`IR_PROXY_MAIL=1` 可强制打开）。
        # 理由：收信轮询是整链的瓶颈（激活邮件到达就要 6.02s），绕道代理只会
        # 更慢；而 Cloudflare Worker 不关心我们的出口 IP，走代理没有任何收益。
        # 代理只该挂在**被封的那一侧**（sso / discovery）。
        if os.getenv("IR_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes"):
            config.apply_proxy(self.session)
        # 上一次 `wait_for_mail` 失败的原因（"" = 没有异常，纯粹是邮件没到）。
        # 用来把"邮件没到"和"邮箱服务读不出来"分开 —— 见 `wait_for_mail`。
        self.last_error = ""
        # 上一次 `wait_for_mail` 打了几次收信接口（含 5xx 重试）。
        # 🔴 为什么要数这个：收信背后是 D1，而这个 N 是判断
        #    "我们是不是把 D1 读限额打满的元凶"的唯一凭据。
        #    2026-09-19 起走 `/api/inbox?email=`（每次读 0~1 行），
        #    改造前走 `/admin/all`（每次读 N+1 行）—— 对比看这个数就知道省了多少。
        self.last_polls = 0
        self.last_http_errors = 0
        self.session.headers.update({
            "X-Admin-Token": self.token,
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })

    # ── 邮箱管理 ──────────────────────────────────────────────
    def create_mailbox(self, domain: str = None, count: int = 1) -> list[str]:
        r = self.session.post(
            f"{self.base}/api/mailboxes",
            json={"domain": domain or config.WORKER_DOMAIN, "count": count},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"create_mailbox failed: {data}")
        return data.get("emails", [])

    # ── 邮件读取 ──────────────────────────────────────────────
    def list_mails(self, limit: int = None, email: str = None) -> list[Mail]:
        """列邮件。

        🔴 **`email` 给定 → 走 `/api/inbox?email=`**（服务端按
        `idx_emails_to_address` 索引过滤，实测读 **0~1 行**）。
        不给 → 退回 `/admin/all?limit=N`（全表，读 N+1 行）。

        为什么收信必须带 `email`（2026-09-19 实测，不是推测）：

        1. `/admin/all` **不支持按收件人过滤** —— `email`/`to`/`to_address`
           三个参数全被忽略，只能整表拉回来自己筛，每次读 N+1 行。
           背后是 D1，读配额就是这么烧掉的（09-18 烧到 173.9%）。
        2. **更致命的是窗口截断**：`/admin/all` 返回的是「最新 N 条」，
           而 D1 的 retention 只保留 100 行。实测这张表被**别的项目**
           （同机的 grok 注册线，共用同一个 Worker）以 **21.6 封/小时**
           灌满 ⇒ **整表每 4.6 小时被冲刷一遍**。我们的激活邮件一旦被
           挤出「最新 N 条」窗口，就**永远读不到**了。
        3. `/api/inbox` 是按收件人索引查，**别人的邮件挤不掉我们的**。
           这比「省行数」重要得多 —— 省行数只是省钱，不被挤掉是能不能用。

        实测（2026-09-19 14:00）：`/admin/all?limit=100` 返回的 99 封里
        **0 封是我们的**（98 封 grok 推广 + 1 封别的）；而
        `/api/inbox?email=<我们的地址>` 直接命中 1 封、24KB。
        """
        if email:
            r = self.session.get(f"{self.base}/api/inbox",
                                 params={"email": email}, timeout=self.timeout)
        else:
            limit = limit or config.MAIL_LIST_LIMIT
            r = self.session.get(f"{self.base}/admin/all",
                                 params={"limit": limit}, timeout=self.timeout)
        r.raise_for_status()
        raw = r.json().get("messages", []) or []
        return [
            Mail(
                id=str(m.get("id", "")),
                to_address=str(m.get("to_address", "")),
                from_address=str(m.get("from_address", "")),
                subject=str(m.get("subject", "")),
                body=str(m.get("body") or m.get("body_text") or ""),
                extracted_json=str(m.get("extracted_json") or "[]"),
                received_at=int(m.get("received_at") or 0),
            )
            for m in raw
        ]

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int = None,
        interval: float = None,
        since_ts: int = 0,
        limit: int = None,
    ) -> Mail | None:
        """轮询等待目标地址的邮件。

        interval 默认 0.8s（实测邮件 3 秒内到达，1~2 次轮询即命中）。

        🔴 **走 `/api/inbox?email=<address>`，不再走 `/admin/all`**
        （2026-09-19 改造，理由见 `list_mails`：那条路既烧 D1 读配额，
        又会被别人的邮件挤出「最新 N 条」窗口而永久读不到）。

        历史遗留：`limit` 参数保留只为向后兼容，索引查询用不上它 ——
        `/api/inbox` 只返回这一个收件人的邮件，没有"窗口要开多大"的问题。
        原来的自适应窗口（5→10→20→50）是给 `/admin/all` 的体积/延迟
        折中用的，换成索引查询后**整个问题消失了**。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        target = address.lower()
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_code = ""

        while time.time() < deadline:
            self.last_polls += 1
            try:
                mails = self.list_mails(email=address)
            except requests.HTTPError as ex:
                # 🔴 5xx 必须重试，绝不能判死。
                #
                # 实测（2026-09-18）：邮箱 Worker 的 `/admin/all` 会**间歇性**抛
                # Cloudflare `Error 1101`（Worker 未捕获异常）。同一个请求连打
                # 25 次只成功 1 次（~4%）。而旧实现第一枪就 `raise_for_status()`
                # → 4 个**注册已经成功**的账号全被判失败 —— 激活邮件其实好好
                # 躺在库里，是我们读不出来。
                #
                # 语义上：5xx = "服务端现在读不出来"，不是"这封邮件不存在"。
                # 轮询本来就是在等，多等几次的代价远小于丢掉一个已注册账号。
                # 4xx（401 凭据错 / 404）才是**我们**的问题，必须立刻失败。
                code = ex.response.status_code if ex.response is not None else 0
                if code and code < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_code = str(code or "?")
                # 轻微退避：对一个已经在挣扎的 Worker 高频打枪没有好处。
                time.sleep(min(interval * (1 + errs // 10), 2.0))
                continue
            for m in mails:
                if m.to_address.lower() != target:
                    continue
                if sender_contains and sender_contains.lower() not in m.from_address.lower():
                    continue
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                return m
            time.sleep(interval)

        if errs:
            # 把"邮件没到"和"读不出来"分开 —— 两者的修法完全不同。
            self.last_error = (f"邮箱 Worker 持续 5xx（{errs} 次，最近 HTTP "
                               f"{last_code}）—— 不是邮件没到，是读不出来")
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")
