"""CF Worker 临时邮箱客户端。

远端 Worker 已实现「激活链接自动提取」，邮件到达后
`extracted_json` 字段直接给出候选链接，无需自行解析 HTML。

API（均需 X-Admin-Token）：
  POST /api/mailboxes           {"domain": "...", "count": N} -> {"emails": [...]}
  GET  /admin/all?limit=N       列出邮件（含 extracted_json）
  GET  /admin/msg?id=&email=    单封邮件详情
  DELETE /admin/delete?id=&email=

踩坑记录：
  - 鉴权头同时支持 `X-Admin-Token` 与 `Authorization: Bearer`，两个都带最稳
  - /admin/all 返回全量邮件，必须按 to_address 过滤（大小写不敏感）
  - 邮件通常在注册后 3 秒内到达，但首次请求偶发返回空列表，需要轮询
"""

import json
import time
import urllib.parse
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
    def list_mails(self, limit: int = None) -> list[Mail]:
        limit = limit or config.MAIL_LIST_LIMIT
        r = self.session.get(f"{self.base}/admin/all", params={"limit": limit}, timeout=self.timeout)
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

        🔴 **自适应 limit（2026-09-15 实测优化）**
        `/admin/all` 有两个实测特性（`.workbuddy-ai/tmp/` 探针结论）：

          1. **不支持按收件人过滤** —— `email` / `to` / `to_address` 三个参数
             全被忽略，返回体逐字节相同。所以只能整表拉回来自己筛。
          2. **延迟与返回体积正相关**：

             | limit | 耗时 | 体积 |
             |-------|------|------|
             | 50 | 568ms | 57 KB |
             | 5  | 265ms | 5.8 KB |
             | 1  | 269ms | 1.2 KB |

        → 固定 `limit=50` 意味着**每次轮询都在拉 57KB**。4 个生产者并发轮询
          就是 ~170KB/s 砸向 Worker，既慢（每次白等 300ms）又挤占带宽。

        做法：从 `MAIL_LIST_MIN`（5）起步，**未命中就翻倍**，上限 `limit`（50）。
        注册期绝大多数轮询会立刻命中 → 每次都走小包（省 ~300ms/次）；
        真碰上 Worker 繁忙（我们的邮件被别人的挤出前几条）再自动放大，
        不会漏。这是"快路径 + 兜底"而不是"猜一个够用的值"。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        cap = limit or config.MAIL_LIST_LIMIT
        cur = min(config.MAIL_LIST_MIN, cap)
        deadline = time.time() + timeout
        target = address.lower()

        while time.time() < deadline:
            for m in self.list_mails(limit=cur):
                if m.to_address.lower() != target:
                    continue
                if sender_contains and sender_contains.lower() not in m.from_address.lower():
                    continue
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                return m
            cur = min(cur * 2, cap)      # 未命中 → 扩大窗口（见 docstring）
            time.sleep(interval)
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")
