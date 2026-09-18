"""YYDS Mail 适配器 —— 让 YydsMailProvider 兼容 TempMailClient 的接口。

pipeline 依赖的 TempMailClient 接口：
  - create_mailbox(domain=None, count=1) -> list[str]
  - wait_for_mail(address, ...) -> Mail 对象（含 received_at / find_link / to_address）

YYDS Mail 是按「一个账号一个邮箱」设计的（create_mailbox 每次返回单个地址），
因此这里做了适配：create_mailbox 创建 count 个独立邮箱，wait_for_mail 用
地址对应的 provider 实例轮询收信。
"""

import time

from . import config
from .yydsmail import YydsMailProvider


class YydsMail:
    """YYDS 邮件对象，兼容 tempmail.Mail 的 find_link / received_at 接口。"""

    def __init__(self, *, to_address: str, from_address: str, subject: str,
                 body: str, html: str, received_at: int, provider: YydsMailProvider):
        self.to_address = to_address
        self.from_address = from_address
        self.subject = subject
        self.body = body
        self.html = html
        self.received_at = received_at      # 毫秒时间戳，与 Worker 版一致
        self._provider = provider
        self._links_cache = None

    @property
    def links(self) -> list[str]:
        if self._links_cache is None:
            from .yydsmail import _extract_urls

            self._links_cache = _extract_urls(f"{self.subject}\n{self.body}\n{self.html}")
        return self._links_cache

    def find_link(self, *keywords: str) -> str | None:
        kws = [k.lower() for k in keywords] or ["active", "activat"]
        for url in self.links:
            low = url.lower()
            if any(k in low for k in kws):
                return url
        return None


class YydsMailClient:
    """YYDS Mail 客户端（TempMailClient 兼容适配）。"""

    def __init__(self, api_key: str = "", base_url: str = "",
                 domain: str = "", subdomain: str = "", wildcard: bool = False):
        self.api_key = api_key or config.YYDS_API_KEY
        self.base_url = base_url or config.YYDS_BASE_URL
        self.domain = domain or config.YYDS_DOMAIN
        self.subdomain = subdomain or config.YYDS_SUBDOMAIN
        self.wildcard = wildcard
        self._providers: dict[str, YydsMailProvider] = {}

    # ── 邮箱管理 ──────────────────────────────────────────────
    def create_mailbox(self, domain: str = None, count: int = 1) -> list[str]:
        """创建 count 个独立邮箱，返回地址列表。"""
        emails = []
        for _ in range(max(1, count)):
            provider = YydsMailProvider(
                api_key=self.api_key,
                base_url=self.base_url,
                domain=domain or self.domain,
                subdomain=self.subdomain,
                wildcard=self.wildcard,
            )
            address = provider.create_mailbox()
            self._providers[address] = provider
            emails.append(address)
        return emails

    # ── 邮件读取 ──────────────────────────────────────────────
    def wait_for_mail(self, address: str, sender_contains: str = "openxlab",
                      timeout: int = None, interval: float = None,
                      since_ts: int = 0, limit: int = None) -> YydsMail | None:
        """轮询等待目标地址的邮件（YYDS 是独立邮箱，无需整表过滤）。"""
        provider = self._providers.get(address)
        if provider is None:
            provider = YydsMailProvider(
                api_key=self.api_key, base_url=self.base_url,
                domain=self.domain, subdomain=self.subdomain,
                wildcard=self.wildcard,
            )
            provider.address = address
            self._providers[address] = provider

        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = provider._fetch_latest_message()
            if item:
                received_at = item.get("received_at")
                received_ms = 0
                if received_at is not None:
                    received_ms = int(received_at.timestamp() * 1000)
                if since_ts and received_ms and received_ms < since_ts:
                    continue
                html = item.get("html_content") or ""
                body = item.get("text_content") or ""
                mail = YydsMail(
                    to_address=address,
                    from_address=item.get("sender") or "",
                    subject=item.get("subject") or "",
                    body=body,
                    html=html,
                    received_at=received_ms,
                    provider=provider,
                )
                if sender_contains:
                    sender_low = mail.from_address.lower()
                    if sender_contains.lower() not in sender_low:
                        # 寄件人不匹配：还可能是别的信，继续等
                        if not mail.links:
                            continue
                return mail
            time.sleep(max(0.2, interval))
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")
