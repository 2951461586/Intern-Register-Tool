"""邮箱源抽象层：把「建邮箱 / 收信」从 CF Worker 解耦出来。

背景
----
2026-09-23 实测把整链的堵点收敛成两条，而**没有任何一个邮箱源同时满足**：

    | 邮箱源                    | 过域名门 | 可编程读信                      |
    |---------------------------|----------|---------------------------------|
    | 本项目 CF Worker 自有域名 | ❌ A0232 | ✅（Worker 自带 `/api/inbox`）  |
    | gmail / outlook / qq      | ✅       | ❌ 需要 app password / OAuth    |
    | 定制域名（Google Workspace）| ✅     | ❌ 同上（但凭据已验证有效）      |
    | 供应商 outlook 账号池     | ✅       | ✅（chatai.codes 读信页）        |

所以把「邮箱源」做成可插拔：域名门不合格就换源，读信不通就换源，
而 `stage_register` 的业务逻辑一行都不用动。

做法
----
`MailboxSource` 是**结构性契约**（`typing.Protocol`），不是基类 ——
`stage_register` 本来就是鸭子类型（`tests/test_error_kind.py` 传的是
`FakeMail()`），这里只是把既有事实写成可检查的接口，**不引入继承关系**。

    create_mailbox(domain=None, count=1) -> list[str]
    wait_for_mail(address, sender_contains="openxlab", ...) -> Mail | None
    wait_for_activation_link(address, **kw) -> str | None
    last_error: str        # "" = 邮件没到；非空 = 读不出来（两者修法不同）
    last_polls: int        # 打了几次收信
    last_http_errors: int  # 读失败了几次

三个实现 + 一个工厂：

  · `WorkerMailbox` —— 包住既有 `TempMailClient`，**纯转发、行为零变化**
    （默认路径，`IR_MAILBOX_KIND` 不设或设成 `worker`）。
  · `ImapMailbox`   —— stdlib `imaplib`，从凭据文件**领用**地址（不是新建）。
  · `ChataiMailbox` —— 从供应商账号池**探活后领用**，经读信页收信。
  · `make_source()` —— 按 `IR_MAILBOX_KIND` 选实现。

🔴 **不引入新依赖**：`tests/test_dependency_surface.py` 只允许
   `pytest` / `requests` / `cryptography`，所以 IMAP 只能用 stdlib `imaplib`，
   而 chatai 侧用的是 `requests` + `cryptography`（AES-GCM）—— 都在白名单内。

🔴 **`Mail` 数据类仍留在 `tempmail.py`，本模块单向 import 它。**
   反过来（把 `Mail` 搬到这里、再让 `tempmail` 回导）会形成循环 import，
   而循环只能靠函数内延迟 import 绕 —— 那正是 `python-compat-shell-refactor`
   记的坑：**补丁面打在壳上，测试会静默失效**（单独跑绿、全量跑红）。

🔴 **`issubclass()` 不能用**：`MailboxSource` 含数据成员（`last_error` 等），
   `issubclass` 会直接抛 `TypeError: Protocols with non-method members ...`。
   契约测试只能用 `isinstance`（已实测，Python 3.13.14）。

判据
----
`ImapMailbox.create_mailbox()` **不是新建邮箱**，而是从凭据池里**领一个**
并写进 `.used` 状态 —— 判据是「同一个地址绝不会被领两次」，见
`tests/test_mailbox.py`。
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import hmac
import html
import imaplib
import json
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config
from .tempmail import Mail, TempMailClient

__all__ = [
    "MailboxSource",
    "WorkerMailbox",
    "ImapMailbox",
    "ChataiMailbox",
    "RemailMailbox",
    "make_source",
    "used_state_path",
    "chatai_state_path",
    "parse_credentials",
    "parse_chatai_accounts",
]

# 凭据行分隔符（供应商格式：`邮箱----密码----恢复邮箱`）。
CRED_SEP = "----"

# 链接提取：够用即可，不做 HTML 解析（Worker 那侧也是结构化提取）。
_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]\}]+", re.I)
# 中英文标点常被粘在 URL 尾部，剪掉。
_TRAILING = ".,;:!?、。，；：！？）】」』\"'"

# `create_mailbox` 是 read-modify-write（读凭据 → 读已用 → 写已用），
# 而 `pipeline.run_batch` 会从多个 producer 线程同时调它 —— 不加锁会
# **把同一个地址发给两个任务**，而两边都会以为自己拿到了唯一地址。
_LOCK = threading.Lock()


def used_state_path() -> Path:
    """IMAP 凭据「已领用」状态文件。`IR_IMAP_USED_STATE` 可覆盖。

    🔴 刻意**不写在凭据文件旁边** —— 凭据文件通常在用户的下载目录里，
    往那儿写状态文件会污染用户目录。统一落在 `.workbuddy-ai/state/`
    （该目录已被 .gitignore **整目录**排除，见 .gitignore §7）。

    ⚠ 与 `quota.state_path()` 同一个约定：在**调用时**读环境变量，
    不受导入顺序影响 —— 自检脚本靠它做隔离。
    """
    override = os.getenv("IR_IMAP_USED_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / ".workbuddy-ai" / "state" / "imap_used.json"


def parse_credentials(text: str) -> dict[str, str]:
    """解析凭据文件 → `{邮箱: 密码}`（保持文件顺序，dict 有序）。

    供应商格式：`邮箱----密码` 或 `邮箱----密码----恢复邮箱`。
    第三段是**恢复邮箱**（含 `@`），不是 token —— 2026-09-23 初判时
    曾把它当成凭据，差点把「IMAP 是否可行」判反。这里只取前两段。

    跳过：空行、`#` 注释、段数不足 2 的行、以及密码段为空的行。
    """
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(CRED_SEP)]
        if len(parts) < 2:
            continue
        addr, pwd = parts[0], parts[1]
        if addr and pwd and "@" in addr:
            out[addr] = pwd
    return out


def _extract_links(text: str) -> list[str]:
    """从正文里抽 http(s) 链接，去重且保持出现顺序。

    `html.unescape` 是必须的：HTML 正文里的 `&amp;` 会让提取到的 URL
    在浏览器里打不开（查询串被截断），而这条链接正是激活链接。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(html.unescape(text or "")):
        url = raw.rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _body_text(msg) -> str:
    """取可读正文：优先 `text/plain`，退回 `text/html`，跳过附件。

    ⚠ 必须跳过 `Content-Disposition: attachment` —— 附件里可能也有
      链接，但那不是激活链接，混进来会污染 `find_link` 的结果。
    """
    plain: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except Exception:  # noqa: BLE001
            # 编码声明坏掉的邮件很常见（charset 写错 / 没写）。
            payload = part.get_payload(decode=True) or b""
            if isinstance(payload, bytes):
                payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(payload, str):
            (plain if ctype == "text/plain" else html_parts).append(payload)
    if plain:
        return "\n".join(plain)
    return "\n".join(html_parts)


def _received_ms(msg) -> int:
    """`Date` 头 → **毫秒** unix 时间戳。

    🔴 口径必须与 Worker 侧一致：`pipeline` 读 `m.received_at` 时按
    `> 1e11 就除 1000` 判单位（见 `stage_register` 的 `arrival_delay_ms`）。
    这里返回**毫秒**，正好落在那条分支上。

    `mktime_tz` 会正确处理 `+0800` 这类时区偏移；`parsedate_tz` 解析不了
    时返回 None，此时退回 0（= 未知），而不是抛异常 —— 一个坏日期头
    不该让整条收信流程失败。
    """
    raw = msg.get("Date")
    if not raw:
        return 0
    tup = email.utils.parsedate_tz(str(raw))
    if not tup:
        return 0
    try:
        return int(email.utils.mktime_tz(tup) * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


@runtime_checkable
class MailboxSource(Protocol):
    """`stage_register` 需要的**全部**能力（结构性契约，无继承关系）。

    ⚠ 只做类型与文档用途。`issubclass()` 对它**不可用**（含数据成员），
    契约测试请用 `isinstance()`。
    """

    last_error: str
    last_polls: int
    last_http_errors: int

    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """给出 `count` 个**可收信**的地址。"""
        ...

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询等待该地址的邮件；超时返回 None（原因写在 `last_error`）。"""
        ...

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        """等邮件并直接给出激活链接。"""
        ...


class WorkerMailbox:
    """把既有 `TempMailClient` 适配到 `MailboxSource`（**默认路径**）。

    🔴 **纯转发、行为零变化**：不加工返回值、不改默认值、不吞异常。
    存在的意义是给默认路径一个显式的契约落点，而不是让 `TempMailClient`
    隐式地「碰巧满足」接口 —— 那样换源时就没人知道该对齐哪些方法。

    `client` 可注入（测试用），也可用 `**kw` 透传给 `TempMailClient`。
    """

    def __init__(self, client=None, **kw):
        self._client = client if client is not None else TempMailClient(**kw)

    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        return self._client.create_mailbox(domain=domain, count=count)

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        return self._client.wait_for_mail(
            address,
            sender_contains=sender_contains,
            timeout=timeout,
            interval=interval,
            since_ts=since_ts,
            limit=limit,
        )

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        return self._client.wait_for_activation_link(address, **kw)

    # 状态是**透传**的，不是快照 —— `stage_register` 在 `wait_for_mail`
    # 之后才读它们，快照会让读到的永远是 0。
    @property
    def last_error(self) -> str:
        return self._client.last_error

    @property
    def last_polls(self) -> int:
        return self._client.last_polls

    @property
    def last_http_errors(self) -> int:
        return self._client.last_http_errors


class ImapMailbox:
    """从**预置凭据池**领用地址，用 stdlib `imaplib` 收信。

    🔴 与 Worker 模式最大的语义差异：**这里不新建邮箱**。
    Worker 的 `create_mailbox` 是真的建一个；这里是「从池子里领一个
    还没用过的」，并把领用记录落盘去重。方法名沿用是为了让
    `stage_register` 一行都不用改 —— 契约是「给我 count 个可收信的地址」。

    🔴 **密码永不进日志**：`last_error` 会被 `stage_register` 写进
    `rec.error` → 台账 → stdout，所以所有异常文案都过一遍 `_scrub()`
    （把已知密码替换成 `***`）。地址不打码 —— 台账本来就存明文邮箱。

    凭据文件格式：每行 `邮箱----密码`（`----恢复邮箱` 可有可无），
    与供应商给的文件一致；`utf-8-sig` 读取，容忍 BOM。
    """

    def __init__(
        self,
        credentials: str | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        folder: str | None = None,
        used_state: str | None = None,
        timeout: float | None = None,
    ):
        self.credentials_path = Path(
            credentials if credentials is not None else config.IMAP_CREDENTIALS
        ).expanduser()
        self.host = host or config.IMAP_HOST
        self.port = int(port or config.IMAP_PORT)
        self.folder = folder or config.IMAP_FOLDER
        self.timeout = float(timeout or config.IMAP_TIMEOUT)
        self._used_override = Path(used_state).expanduser() if used_state else None
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        self._creds: dict[str, str] | None = None

    # ── 凭据池 ────────────────────────────────────────────────
    def _load_credentials(self) -> dict[str, str]:
        """读凭据文件（只读一次，之后缓存）。"""
        if self._creds is not None:
            return self._creds
        if not str(self.credentials_path) or str(self.credentials_path) == ".":
            raise RuntimeError(
                "IMAP 凭据文件未配置。\n"
                "  修法：在 .env 里写 IR_IMAP_CREDENTIALS=<凭据文件绝对路径>\n"
                "  格式：每行 `邮箱----密码`（第三段「恢复邮箱」可有可无）"
            )
        if not self.credentials_path.is_file():
            raise RuntimeError(
                f"IMAP 凭据文件不存在：{self.credentials_path}\n"
                "  修法：确认 IR_IMAP_CREDENTIALS 指向的文件确实存在"
            )
        text = self.credentials_path.read_text(encoding="utf-8-sig")
        creds = parse_credentials(text)
        if not creds:
            raise RuntimeError(
                f"IMAP 凭据文件里没有可用条目：{self.credentials_path}\n"
                f"  期望每行 `邮箱{CRED_SEP}密码`（第三段可有可无）"
            )
        self._creds = creds
        return creds

    def _used_path(self) -> Path:
        return self._used_override or used_state_path()

    def _read_used(self) -> set[str]:
        """已领用地址（小写）。按凭据文件路径分桶，多份池互不干扰。"""
        p = self._used_path()
        if not p.is_file():
            return set()
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict):
            return set()
        vals = data.get(str(self.credentials_path))
        if not isinstance(vals, list):
            return set()
        return {str(v).lower() for v in vals}

    def _mark_used(self, addrs: list[str]) -> None:
        p = self._used_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        key = str(self.credentials_path)
        cur = data.get(key)
        cur = list(cur) if isinstance(cur, list) else []
        low = {str(v).lower() for v in cur}
        for a in addrs:
            if a.lower() not in low:
                cur.append(a)
                low.add(a.lower())
        data[key] = cur
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _password_for(self, address: str) -> str:
        creds = self._load_credentials()
        if address in creds:
            return creds[address]
        for k, v in creds.items():
            if k.lower() == address.lower():
                return v
        raise RuntimeError(f"凭据文件里没有 {address} 的密码（是不是换了凭据文件？）")

    def _scrub(self, text: str) -> str:
        """把已知密码从文本里抹掉。`last_error` 会进台账与 stdout。

        🔴 抹之前**先尽力加载凭据**，不能只依赖"调用时 `self._creds` 已就绪"：
           异常完全可能发生在加载**之前**（连接阶段就炸），那时凭据集是空的，
           抹了个寂寞 —— 而这条路径恰恰是"把异常原样写进日志"的路径。

        ⚠ 加载失败**不抛**：抹不掉密码也不能让错误处理本身炸掉
          （那时会把一个 OSError 换成 RuntimeError，反而盖住真正的原因）。
        """
        creds = self._creds
        if creds is None:
            try:
                creds = self._load_credentials()
            except Exception:  # noqa: BLE001
                creds = {}
        for pwd in creds.values():
            if pwd and pwd in text:
                text = text.replace(pwd, "***")
        return text

    # ── 契约：建邮箱 ──────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """从池里领 `count` 个未用过的地址（**不新建**，见类 docstring）。

        `domain` 给定时只在该域名下挑（大小写不敏感，匹配 `@domain` 后缀）——
        与 Worker 侧 `create_mailbox(domain=...)` 的调用姿势保持一致。

        池子不够时**抛错**而不是少给几个：少给会让 `stage_register`
        在 `emails[0]` 上抛 `IndexError`，报错点离真正的原因很远。
        """
        want = (domain or "").strip().lower()
        with _LOCK:
            creds = self._load_credentials()
            used = self._read_used()
            avail: list[str] = []
            for addr in creds:
                if addr.lower() in used:
                    continue
                if want and not addr.lower().endswith("@" + want):
                    continue
                avail.append(addr)
                if len(avail) >= count:
                    break
            if len(avail) < count:
                hint = f"，且限制了域名 @{want}" if want else ""
                raise RuntimeError(
                    f"IMAP 凭据池里只剩 {len(avail)} 个可用地址"
                    f"（需要 {count} 个）。池共 {len(creds)} 个，"
                    f"已领用 {len(used)} 个{hint}。\n"
                    f"  补货：往 {self.credentials_path} 追加 "
                    f"`邮箱{CRED_SEP}密码` 行；\n"
                    f"  重置：清空 {self._used_path()}（会重新从池头开始领）"
                )
            self._mark_used(avail)
            return avail

    # ── 契约：收信 ────────────────────────────────────────────
    def _connect(self, address: str):
        """建连 + 登录 + 选中收件箱（只读）。失败时自行清理连接。"""
        conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        try:
            conn.login(address, self._password_for(address))
            conn.select(self.folder, readonly=True)
        except Exception:  # noqa: BLE001
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            raise
        return conn

    @staticmethod
    def _close(conn) -> None:
        if conn is None:
            return
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass

    def _search_ids(
        self, conn, address: str, sender_contains: str, limit: int | None = None
    ) -> list[bytes]:
        criteria: list[str] = []
        if sender_contains:
            criteria += ["FROM", f'"{sender_contains}"']
        # 🔴 必须带 `TO <自己>` —— 与 Worker 侧走 `/api/inbox?email=` 同一个
        #    理由：同一个 IMAP 账号下可能有别人发的邮件，不加收件人过滤
        #    就会把无关邮件当成激活邮件（然后 `find_link` 返回 None，
        #    报出来的是「找不到激活链接」，离真正原因很远）。
        criteria += ["TO", f'"{address}"']
        typ, data = conn.search(None, *criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH 失败：{typ}")
        ids = (data[0] or b"").split()
        if limit and len(ids) > limit:
            ids = ids[-limit:]
        return ids

    def _fetch_one(self, conn, uid: bytes) -> Mail | None:
        typ, raw = conn.fetch(uid, "(RFC822)")
        if typ != "OK" or not raw or not isinstance(raw[0], tuple):
            return None
        msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
        body = _body_text(msg)
        urls = _extract_links(body)
        return Mail(
            id=f"imap:{uid.decode('ascii', 'replace')}",
            to_address=email.utils.parseaddr(str(msg.get("To") or ""))[1],
            from_address=email.utils.parseaddr(str(msg.get("From") or ""))[1],
            subject=str(msg.get("Subject") or ""),
            body=body,
            extracted_json=json.dumps([{"value": u} for u in urls], ensure_ascii=False),
            received_at=_received_ms(msg),
        )

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询等邮件。连上后**复用同一条连接**，断了才重连。

        🔴 每次轮询都重连会付一次 TLS 握手 + 登录（实测 Gmail 侧 ~0.5s），
        而轮询间隔只有 0.8s —— 那样大部分时间花在握手上，不是在等邮件。

        失败语义与 `TempMailClient.wait_for_mail` 对齐：
        `last_error` 为空 = 邮件没到（该等）；非空 = 读不出来（该修）。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_err = ""
        conn = None
        try:
            while time.time() < deadline:
                self.last_polls += 1
                try:
                    if conn is None:
                        conn = self._connect(address)
                    for uid in reversed(self._search_ids(conn, address, sender_contains, limit)):
                        m = self._fetch_one(conn, uid)
                        if m is None:
                            continue
                        if since_ts and m.received_at and m.received_at < since_ts:
                            continue
                        return m
                except Exception as ex:  # noqa: BLE001
                    # IMAP 侧的失败面比 HTTP 宽（连接、认证、协议、编码），
                    # 全部按"读不出来"处理并重试 —— 与 Worker 侧 5xx 必须重试
                    # 同一条理由：轮询本来就是在等，重试的代价远小于
                    # 把一个**已经注册成功**的账号判失败。
                    errs += 1
                    self.last_http_errors = errs
                    last_err = self._scrub(f"{type(ex).__name__}: {ex}")
                    self._close(conn)
                    conn = None
                    time.sleep(min(interval * (1 + errs // 5), 5.0))
                    continue
                time.sleep(interval)
        finally:
            self._close(conn)
        if errs:
            self.last_error = self._scrub(
                f"IMAP 读信持续失败（{errs} 次，最近 {last_err}）—— 不是邮件没到，是读不出来"
            )
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")


# ══ chatai.codes 读信页 ════════════════════════════════════════════
# 供应商给的 outlook 账号池，每行 4 段：
#     `邮箱----密码----clientId----refreshToken`
# 读信页（默认 https://mail.chatai.codes）用 Microsoft Graph 拉信，
# 请求体走 AES-GCM + HMAC 加密信封（算法从前端 bundle 逆向，见 docs/protocol.md）。

CHATAI_SEP = CRED_SEP

# 业务错误码里**明确表示这份凭据已经不能用**的取值。
# 实测（2026-09-24）：供应商批次 31 条里 30 条返回这个码。
_DEAD_CODES = frozenset({"TOKEN_EXPIRED_OR_REVOKED"})

# 除业务码外还要看错误正文 —— 实测返回体里带
# `AADSTS70000: the grant is expired`。只认「凭据/授权失效」这类措辞。
# 🔴 **刻意不写成 `AADSTS\d+`**：AADSTS 里也有应用级配置错误（clientId 写错），
#    那种错会让**整池**账号同时"失效"—— 一条过宽的判据就能把池子清空。
_DEAD_PAT = re.compile(r"invalid_grant|grant is expired|token.{0,12}expired|revoked", re.I)

# 401/403 响应体里指向「信封/会话过期」的措辞（用于决定要不要重建会话重试）。
# 取值直接来自前端 `secureApiFetch` 的正则，不是本项目自造的。
_ENVELOPE_PAT = re.compile(r"安全会话|请求签名|nonce|加密请求体|请求已?过期", re.I)

# 探活时连续多少次「结果未知」就判定读信页整体不可达。
# 🔴 没有这道闸门，一次网络抖动会把整池账号逐个探一遍：慢，且日志里会
#    留下"整池不可用"的假象。
_MAX_UNKNOWN = 3


def chatai_state_path() -> Path:
    """chatai 账号池「已领用 / 已失效」状态文件。`IR_CHATAI_STATE` 可覆盖。

    🔴 与 `used_state_path()` 同一个理由：**不写在账号文件旁边** ——
    账号文件在用户的下载目录里，往那儿写状态文件会污染用户目录。

    ⚠ 在**调用时**读环境变量，不受导入顺序影响（自检脚本靠它做隔离）。
    """
    override = os.getenv("IR_CHATAI_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / ".workbuddy-ai" / "state" / "chatai_used.json"


def parse_chatai_accounts(text: str) -> list[dict]:
    """解析 chatai 账号文件 → 有序 `list[dict]`（保持文件顺序）。

    供应商格式：每行 4 段
        `邮箱----密码----clientId----refreshToken`

    ⚠ 返回 **list 而不是 dict**：同一邮箱理论上可能重复出现，而这里要的是
    「按文件顺序逐个试」的语义；去重交给状态文件里的 `used` / `dead`。

    跳过：空行、`#` 注释、段数不足 4 的行、以及任一必填段为空的行。
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(CHATAI_SEP)]
        if len(parts) < 4:
            continue
        addr, pwd, cid, rtok = parts[0], parts[1], parts[2], parts[3]
        if addr and "@" in addr and pwd and cid and rtok:
            out.append({"email": addr, "password": pwd, "clientId": cid, "refreshToken": rtok})
    return out


def _b64u_dec(s: str) -> bytes:
    """base64url → bytes（补回被 `rstrip("=")` 去掉的填充）。"""
    t = str(s or "").replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t)


def _b64u_enc(b: bytes) -> str:
    """bytes → base64url（去填充，与前端 `bytesToBase64Url` 对齐）。"""
    return base64.b64encode(b).decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")


def _iso_ms(raw) -> int:
    """ISO-8601 时间串（读信页给的是 `2026-08-14T00:52:03Z`）→ **毫秒**。

    🔴 口径必须与 `_received_ms` 一致：`stage_register` 按 `> 1e11` 判单位。
    解析不了就退回 0（= 未知），不抛 —— 一个坏时间戳不该让收信流程失败。
    """
    s = str(raw or "").strip()
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return 0


def _is_dead(code: str, error: str) -> bool:
    """这份凭据是不是**确凿**失效了。

    🔴 判据必须"证据确凿"：认不出来的一律返回 False（= 结果未知）。
       否则一次服务端抖动会把整池账号标成死号，而用户看到的是
       "莫名其妙全失效了，只能清状态文件重来"。
    """
    if (code or "").strip() in _DEAD_CODES:
        return True
    return bool(_DEAD_PAT.search(f"{code or ''} {error or ''}"))


def _json_body(r):
    """HTTP 响应 → JSON dict；不是 JSON 就返回 None（**不抛**）。

    🔴 单独抽出来是为了把「是不是 JSON」与「状态码多少」解耦 ——
       读信页用 `500 + JSON` 报业务失败，两者必须分开判。
    """
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _is_envelope_error(body) -> bool:
    """401/403 的响应体是不是「信封/会话过期」（值得重建会话重试）。

    判据与前端 `secureApiFetch` 逐条对齐：
      · `code == "SECURITY_ENVELOPE_INVALID"` → 是
      · 有其它业务 `code`                     → **不是**（业务失败重试无用，
        前端注释："重试反而会重复登录邮箱"）
      · body 解析不出                         → 是（重建一次代价极小）
      · 否则看文案关键词
    """
    if not isinstance(body, dict):
        return True
    if body.get("code") == "SECURITY_ENVELOPE_INVALID":
        return True
    if body.get("code"):
        return False
    text = f"{body.get('error') or ''} {body.get('message') or ''}"
    return bool(_ENVELOPE_PAT.search(text))


class _AccountDead(RuntimeError):
    """读信页明确告知「这份凭据已失效」。"""


class ChataiMailbox:
    """从**供应商账号池**领用 outlook 地址，经读信页收信。

    🔴 这是第三套语义，与另外两套都不同：

      · Worker —— `create_mailbox` **真的新建**一个地址（站点侧无副作用）。
      · IMAP   —— 从池里**领用**，用密码直连 IMAP 收信。
      · chatai —— 从池里**领用**，且**领用前先探活**（见下）。

    为什么必须探活
    --------------
    供应商这一批 31 个账号里 **30 个的 refreshToken 已失效**
    （2026-09-24 实测，返回 `TOKEN_EXPIRED_OR_REVOKED` /
    `AADSTS70000: the grant is expired`）。不探活就领，等于把 30 个必死的
    账号挨个喂给 `stage_register`，每个都白跑一遍注册 —— 而注册是**有
    副作用**的（站点侧会建号），白跑不是零成本。

    探活三态，且**只有"证据确凿"才标死号**：
      · 拉到邮件       → 活，领用
      · 明确的失效信号 → 死，写进状态文件的 `dead`（下次直接跳过）
      · 认不出来的失败 → **未知**，不领用也不标死（下次重探）

    加密信封
    --------
    读信页不接受明文请求体：每次 POST 都包一层
    `AES-GCM(JSON) + HMAC-SHA256(签名)`，密钥来自
    `POST /api/security-session` 的 `sessionKey`。算法逐字段对齐前端
    bundle 的 `createSecureEnvelope`（见 `docs/protocol.md`）。

    🔴 会话**要缓存**：前端的 `getApiSecuritySession()` 就是缓存到
    `expiresAtMs - 60s` 才重建。每次轮询都重建会让请求数翻倍。
    服务端重启会换密钥，此时返回 401/403 ⇒ 丢缓存重建并重试一次
    （与前端 `secureApiFetch` 的 `SECURITY_ENVELOPE_INVALID` 重试同构）。

    🔴 **业务失败也是 HTTP 500**：读信页把凭据问题放在 `success:false` 的
       JSON 里，但状态码给的是 500（实测死号返回
       `500 {"success":false,"code":"TOKEN_EXPIRED_OR_REVOKED",...}`）。
       所以判据顺序必须是「先解析 body、再看状态码」—— 反过来会把死号
       误判成服务端故障（见 `_post`）。

    🔴 **凭据永不进日志**：`last_error` 会经 `stage_register` 写进
    `rec.error` → 台账 → stdout，所以异常文案统一过 `_scrub()`
    （抹掉密码与 refreshToken）。地址不打码 —— 台账本来就存明文邮箱。
    """

    def __init__(
        self,
        accounts: str | None = None,
        *,
        base: str | None = None,
        timeout: float | None = None,
        state: str | None = None,
    ):
        self.accounts_path = Path(
            accounts if accounts is not None else config.CHATAI_ACCOUNTS
        ).expanduser()
        self.base = (base or config.CHATAI_BASE).rstrip("/")
        self.timeout = float(timeout or config.CHATAI_TIMEOUT)
        self._state_override = Path(state).expanduser() if state else None
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        self._accounts: list[dict] | None = None
        self._session: dict | None = None
        self._expires_ms = 0
        self.session = requests.Session()
        # 🔴 读信页是**国内站**，默认直连。与 `TempMailClient` 同一个约定：
        #    只有显式设 `IR_PROXY_MAIL=1` 才挂代理。
        if os.getenv("IR_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes"):
            config.apply_proxy(self.session)
        else:
            self.session.trust_env = False

    # ── 账号池 ────────────────────────────────────────────────
    def _load_accounts(self) -> list[dict]:
        """读账号文件（只读一次，之后缓存）。"""
        if self._accounts is not None:
            return self._accounts
        if not str(self.accounts_path) or str(self.accounts_path) == ".":
            raise RuntimeError(
                "chatai 账号文件未配置。\n"
                "  修法：在 .env 里写 IR_CHATAI_ACCOUNTS=<账号文件绝对路径>\n"
                f"  格式：每行 `邮箱{CHATAI_SEP}密码{CHATAI_SEP}clientId"
                f"{CHATAI_SEP}refreshToken`"
            )
        if not self.accounts_path.is_file():
            raise RuntimeError(
                f"chatai 账号文件不存在：{self.accounts_path}\n"
                "  修法：确认 IR_CHATAI_ACCOUNTS 指向的文件确实存在"
            )
        text = self.accounts_path.read_text(encoding="utf-8-sig")
        accts = parse_chatai_accounts(text)
        if not accts:
            raise RuntimeError(
                f"chatai 账号文件里没有可用条目：{self.accounts_path}\n"
                f"  期望每行 `邮箱{CHATAI_SEP}密码{CHATAI_SEP}clientId"
                f"{CHATAI_SEP}refreshToken`"
            )
        self._accounts = accts
        return accts

    def _state_path(self) -> Path:
        return self._state_override or chatai_state_path()

    def _read_state(self) -> tuple[set[str], set[str]]:
        """`(已领用, 已失效)`，均为**小写**。按账号文件路径分桶。

        ⚠ 存小写是有意的：邮箱大小写不敏感，存原始大小写会让
          "同一个地址换个大小写就绕过去重"。
        """
        p = self._state_path()
        if not p.is_file():
            return set(), set()
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return set(), set()
        if not isinstance(data, dict):
            return set(), set()
        bucket = data.get(str(self.accounts_path))
        if isinstance(bucket, list):  # 容忍只记 used 的旧形态
            return {str(v).lower() for v in bucket}, set()
        if not isinstance(bucket, dict):
            return set(), set()

        def _set(key: str) -> set[str]:
            vals = bucket.get(key)
            return {str(v).lower() for v in vals} if isinstance(vals, list) else set()

        return _set("used"), _set("dead")

    def _write_state(self, used: set[str], dead: set[str]) -> None:
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        data[str(self.accounts_path)] = {"used": sorted(used), "dead": sorted(dead)}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _account_for(self, address: str) -> dict:
        low = (address or "").lower()
        for a in self._load_accounts():
            if a["email"].lower() == low:
                return a
        raise RuntimeError(f"chatai 账号池里没有 {address}（是不是换了账号文件？）")

    def _scrub(self, text: str) -> str:
        """把已知凭据从文本里抹掉。`last_error` 会进台账与 stdout。

        🔴 与 `ImapMailbox._scrub` 同构：**先尽力加载账号文件**，不能只依赖
           "调用时 `self._accounts` 已就绪" —— 异常完全可能发生在加载之前，
           那时凭据集是空的，抹了个寂寞。加载失败**不抛**：抹不掉也不能让
           错误处理本身炸掉（那会把真原因盖住）。

        ⚠ 只抹**密码与 refreshToken**。`clientId` 不抹 —— 它是 UUID，
          单独泄露没有利用价值，抹掉反而让错误文案失去排查线索。
        """
        accts = self._accounts
        if accts is None:
            try:
                accts = self._load_accounts()
            except Exception:  # noqa: BLE001
                accts = []
        for a in accts:
            for key in ("password", "refreshToken"):
                val = a.get(key)
                if val and val in text:
                    text = text.replace(val, "***")
        return text

    # ── 加密信封 ──────────────────────────────────────────────
    def _ensure_session(self) -> dict:
        """拿（并缓存）安全会话。对齐前端 `getApiSecuritySession()`。"""
        now_ms = int(time.time() * 1000)
        if self._session is not None and self._expires_ms - now_ms > 60_000:
            return self._session
        r = self.session.post(f"{self.base}/api/security-session", json={}, timeout=self.timeout)
        d = _json_body(r)
        if d is None:
            r.raise_for_status()
            raise RuntimeError(
                f"chatai 安全会话返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}"
            )
        if not d.get("success"):
            # 同样不先 `raise_for_status()`：失败原因在 body 里，比状态码有用。
            raise RuntimeError(
                f"chatai 安全会话初始化失败（HTTP {r.status_code}）："
                f"{json.dumps(d, ensure_ascii=False)[:200]}"
            )
        self._session = d
        # `expiresAt` 缺失时给一个保守的 10 分钟；真正的兜底是 401/403 重试。
        self._expires_ms = _iso_ms(d.get("expiresAt")) or (now_ms + 600_000)
        return d

    def _envelope(self, sess: dict, payload: dict) -> dict:
        """构造加密信封（逐字段对齐前端 `createSecureEnvelope`）。

        🔴 `ensure_ascii=False` 必须开：前端用的是 `JSON.stringify`，中文
           不转义。转义后字节不同 ⇒ HMAC 签名对不上 ⇒ 服务端直接 401。
        """
        key = _b64u_dec(sess["sessionKey"])
        iv = os.urandom(12)
        nonce = _b64u_enc(os.urandom(16))
        ts = int(time.time() * 1000)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ct = AESGCM(key).encrypt(iv, body, None)
        iv_b, ct_b = _b64u_enc(iv), _b64u_enc(ct)
        signed = f"{sess['sessionId']}.{nonce}.{ts}.{iv_b}.{ct_b}"
        sig = hmac.new(key, signed.encode("utf-8"), hashlib.sha256).digest()
        return {
            "secure": True,
            "sessionId": sess["sessionId"],
            "sessionToken": sess["sessionToken"],
            "nonce": nonce,
            "timestamp": ts,
            "iv": iv_b,
            "ciphertext": ct_b,
            "signature": _b64u_enc(sig),
        }

    def _post(self, path: str, payload: dict, *, retry: int = 1) -> dict:
        """发一次加密请求 → 响应体的 JSON。

        🔴 **不能先 `raise_for_status()`**（实测踩过，见 `docs/protocol.md`）：
           读信页把**业务失败**也放在 `success:false` 的 JSON 里，而 HTTP
           状态码给的是 **500** —— 死号实测返回
           `HTTP 500 {"success":false,"code":"TOKEN_EXPIRED_OR_REVOKED",...}`。
           先 `raise_for_status()` 会把 body 直接吞掉，于是「账号失效」被
           误判成「服务端故障」，死号永远标不出来（探活整池全 None，
           最后卡在 `_MAX_UNKNOWN` 上）。
           ⇒ 判据顺序固定为：**先解析 body，再看状态码**。

        🔴 401/403 **只在 body 指向信封问题时**才重建会话重试（判据见
           `_is_envelope_error`，与前端 `secureApiFetch` 对齐）。
        """
        for attempt in range(retry + 1):
            sess = self._ensure_session()
            r = self.session.post(
                f"{self.base}{path}", json=self._envelope(sess, payload), timeout=self.timeout
            )
            body = _json_body(r)
            if r.status_code in (401, 403) and attempt < retry and _is_envelope_error(body):
                self._session = None
                self._expires_ms = 0
                time.sleep(0.5)
                continue
            if body is None:
                # 非 JSON ⇒ 这才是真的传输/网关问题（Cloudflare 错误页等）。
                r.raise_for_status()
                raise RuntimeError(
                    f"chatai 读信页返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}"
                )
            return body
        raise RuntimeError(f"chatai 读信页持续拒绝加密信封：{path}")

    # ── 拉信 ──────────────────────────────────────────────────
    def _fetch_mails(self, address: str, limit: int) -> list[Mail]:
        """拉一次该地址的邮件（走 `fetch-graph`）。

        ⚠ 读信页的 `sender` / `keyword` 参数**服务端不做模糊匹配**：实测
           `sender="openxlab"` 直接返回 0 封，而同一时刻不带过滤能拉到 10 封
           （其中就有 openxlab 的）。所以**过滤一律在本地做**，这两个参数
           固定传空串。

        🔴 **没有 `fetch-imap` 退路**：实测它恒返回
           `HTTP 501 IMAP_REQUIRES_CONTAINER`（原文："Cloudflare Workers
           免费运行时不支持当前 IMAP TCP/TLS 实现"）。留着它只会让每次
           失败多打一枪，并把真因（graph 侧的业务码）冲淡成一条
           "IMAP 不可用"。前端会退，是因为它自己部署的 Worker 可能开了
           Containers；我们连的这个部署没开。
        """
        acct = self._account_for(address)
        payload = {
            "email": acct["email"],
            "clientId": acct["clientId"],
            "refreshToken": acct["refreshToken"],
            "keyword": "",
            "limit": int(limit),
            "sender": "",
        }
        d = self._post("/api/fetch-graph", payload)
        if not d.get("success"):
            code = str(d.get("code") or "")
            err = self._scrub(str(d.get("error") or d.get("message") or ""))
            if _is_dead(code, err):
                raise _AccountDead(
                    f"chatai 账号已失效：{code or '?'}（{err[:120] or '读信页未给原因'}）"
                )
            raise RuntimeError(f"chatai 读信失败：{code or '?'} {err[:120]}")
        return [
            self._to_mail(acct["email"], m) for m in (d.get("emails") or []) if isinstance(m, dict)
        ]

    @staticmethod
    def _to_mail(address: str, m: dict) -> Mail:
        """读信页的一封邮件 → `Mail`。

        ⚠ 读信页**不返回收件人**（字段只有 `from` / `subject` / `date` /
           `bodyHtml` / `bodyText` / `bodyPreview` / `id` / `messageId` …），
           所以 `to_address` 用请求时给的那个地址填。

        ⚠ `bodyText` / `bodyHtml` / `bodyPreview` **都拼进来**：实测有些邮件
           只有其中一个非空，而激活链接恰恰可能在另一个里。`_extract_links`
           会去重，所以重复拼接不会产生重复链接。
        """
        parts = [str(m.get(k) or "") for k in ("bodyText", "bodyHtml", "bodyPreview")]
        body = "\n".join(p for p in parts if p)
        urls = _extract_links(body)
        return Mail(
            id=str(m.get("id") or m.get("messageId") or ""),
            to_address=address,
            from_address=str(m.get("from") or ""),
            subject=str(m.get("subject") or ""),
            body=body,
            extracted_json=json.dumps([{"value": u} for u in urls], ensure_ascii=False),
            received_at=_iso_ms(m.get("date")),
        )

    def _probe(self, address: str) -> bool | None:
        """探活：`True` 活 / `False` 死 / `None` 结果未知。"""
        try:
            self._fetch_mails(address, limit=1)
            return True
        except _AccountDead:
            return False
        except Exception as ex:  # noqa: BLE001
            # 网络 / 5xx / 非 JSON —— 都不是"这个账号坏了"的证据。
            self.last_error = self._scrub(f"{type(ex).__name__}: {ex}")
            return None

    # ── 契约：领用 ────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """从池里**探活后**领 `count` 个地址（**不新建**，见类 docstring）。

        `domain` 给定时只在该域名下挑（大小写不敏感）—— 与 Worker / IMAP 的
        调用姿势一致。⚠ CLI 的 `--mail-domain` 默认取 `IR_WORKER_DOMAIN`
        （本项目是自有域名），拿它跑 chatai 会**一个都挑不出来**，所以错误
        文案里带上池内的域名分布。

        🔴 **持锁跑完整个探活循环**：探活是网络往返，看起来"不该持锁"，但
           放锁会让两个线程同时探到同一个活号、然后都以为自己领到了它。
           代价是首个 `create_mailbox` 串行化整池探测（实测 31 条约 1 分钟），
           而且**只有第一次**付这个成本 —— 死号落盘后后续直接跳过。

        🔴 **凑不齐就不记 `used`**：死号照落（那是有价值的负面信息，丢了就
           每次重探），但"领用"记录只在凑齐 `count` 时才写 —— 与
           `ImapMailbox.create_mailbox` 同一条"不产生部分副作用"的原则。
        """
        want = (domain or "").strip().lower()
        with _LOCK:
            accts = self._load_accounts()
            used, dead = self._read_state()
            picked: list[str] = []
            newly_dead: list[str] = []
            unknown = 0
            for a in accts:
                addr = a["email"]
                key = addr.lower()
                if key in used or key in dead:
                    continue
                if want and not key.endswith("@" + want):
                    continue
                got = self._probe(addr)
                if got is None:
                    unknown += 1
                    if unknown >= _MAX_UNKNOWN:
                        if newly_dead:
                            self._write_state(used, dead | {d.lower() for d in newly_dead})
                        raise RuntimeError(
                            f"chatai 读信页连续 {unknown} 次不可达"
                            f"（最近：{self.last_error}）\n"
                            f"  已探明失效 {len(newly_dead)} 个（已落盘，下次直接跳过）；\n"
                            f"  本次已探活的 {len(picked)} 个**没有**被记为已领用。\n"
                            f"  修法：确认本机能直连 {self.base}（该站直连，不挂代理）"
                        )
                    continue
                if got:
                    picked.append(addr)
                    if len(picked) >= count:
                        break
                    continue
                newly_dead.append(addr)
            if len(picked) < count:
                if newly_dead:
                    self._write_state(used, dead | {d.lower() for d in newly_dead})
                doms = sorted({a["email"].split("@")[-1].lower() for a in accts})
                hint = f"，且限制了域名 @{want}" if want else ""
                raise RuntimeError(
                    f"chatai 账号池里只剩 {len(picked)} 个可用地址"
                    f"（需要 {count} 个）。池共 {len(accts)} 个，"
                    f"已领用 {len(used)} 个，已失效 "
                    f"{len(dead) + len(newly_dead)} 个{hint}。\n"
                    f"  池内域名：{'、'.join(doms)}\n"
                    f"  补货：往 {self.accounts_path} 追加 "
                    f"`邮箱{CHATAI_SEP}密码{CHATAI_SEP}clientId"
                    f"{CHATAI_SEP}refreshToken` 行；\n"
                    f"  重置：清空 {self._state_path()}"
                    f"（会重新探活全部账号，含已判失效的）"
                )
            self._write_state(
                used | {a.lower() for a in picked}, dead | {d.lower() for d in newly_dead}
            )
            return picked

    # ── 契约：收信 ────────────────────────────────────────────
    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询读信页等邮件。

        失败语义与 `TempMailClient` / `ImapMailbox` 对齐：
        `last_error` 为空 = 邮件没到（该等）；非空 = 读不出来（该修）。

        🔴 账号失效**立刻返回**而不是轮询到超时：`_AccountDead` 意味着
           "这份凭据再等也不会好"，继续打枪只是白烧一轮超时。

        ⚠ `limit` 只影响一次拉多少封（本地筛最近的那封），不是"窗口大小"。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        want = (sender_contains or "").lower()
        deadline = time.time() + timeout
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_err = ""
        while time.time() < deadline:
            self.last_polls += 1
            try:
                mails = self._fetch_mails(address, limit or config.MAIL_LIST_LIMIT)
            except _AccountDead as ex:
                self.last_error = self._scrub(str(ex))
                return None
            except Exception as ex:  # noqa: BLE001
                errs += 1
                self.last_http_errors = errs
                last_err = self._scrub(f"{type(ex).__name__}: {ex}")
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            for m in mails:
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                if want and want not in m.from_address.lower():
                    continue
                return m
            time.sleep(interval)
        if errs:
            self.last_error = self._scrub(
                f"chatai 读信页持续失败（{errs} 次，最近 {last_err}）—— 不是邮件没到，是读不出来"
            )
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")


# ── 工厂 ──────────────────────────────────────────────────────────
class RemailMailbox:
    """从 Remail 接码平台**按订单购买**邮箱，经 `/v1/pickup` 取件。

    🔴 这是第四套语义，与另外三套都不同：**唯一按订单取信**的源 ——
      每个地址对应一份订单，取件要带该订单的 `serviceToken`。所以本类内部
      维护 `地址 -> (token, orderNo)` 映射；这正是 `MailboxSource` 协议里
      `wait_for_mail(address, ...)` 只需要地址、而 token 藏在源里的原因。

    🔴 **必须用带目标站邮件规则的私有项目**（默认 `projectId=170` = OpenXLab，
      `accessType=private`、`mailRuleCount=4`、`platform=sso.openxlab.org.cn`）。
      2026-09-24 实测：用 chatgpt(pid=2) / 通用接码(pid=73) 下单，pickup 恒
      `items=[]` —— 那些公共项目**没有 OpenXLab 的邮件规则**，邮件根本不会被
      抓取。换成 pid=170 后取件立刻命中（2026-10-03 实测：注册后 ~27s 到达）。
      背景见 `docs/protocol.md`「两道坎互相独立」。

    🔴 `supply` 默认 `private_first`（**服务端默认值**）：先用自有库存，无货
      再回退公共。不要改回 `public_only` —— 09-24 那版脚本写死了它，是当时
      取件失败的混杂因素之一。

    🔴 **凭据永不进日志**：`serviceToken` 只出现在请求 URL 里；`last_error`
      会经 `stage_register` 写进台账 / stdout，所以异常文案里 token 一律打码。
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project_id: int | None = None,
        suffix: str | None = None,
        supply: str | None = None,
        service_mode: str | None = None,
        base: str | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
        wait_timeout: int | None = None,
    ):
        self.api_key = (api_key if api_key is not None else config.REMAIL_API_KEY).strip()
        self.project_id = int(project_id if project_id is not None else config.REMAIL_PROJECT_ID)
        self.suffix = suffix or config.REMAIL_SUFFIX
        self.supply = supply or config.REMAIL_SUPPLY
        self.service_mode = service_mode or config.REMAIL_SERVICE_MODE
        self.base = (base or config.REMAIL_BASE).rstrip("/")
        self.timeout = float(timeout or config.REMAIL_TIMEOUT)
        self.poll_interval = float(poll_interval or config.MAIL_POLL_INTERVAL)
        self.wait_timeout = int(wait_timeout or config.MAIL_POLL_TIMEOUT)
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        self._tokens: dict[str, str] = {}  # address -> serviceToken
        self._orders: dict[str, str] = {}  # address -> orderNo
        self.session = requests.Session()
        # 接码平台在**公网**，与目标站点的封禁无关；默认直连。
        # 与 `TempMailClient` / `ChataiMailbox` 同一个约定：只有显式设
        # `IR_PROXY_MAIL=1` 才挂代理。
        if os.getenv("IR_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes"):
            config.apply_proxy(self.session)
        else:
            self.session.trust_env = False
        self.session.headers.update({"Accept": "application/json"})

    # ── 下单（= 建邮箱） ────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """下 `count` 单，返回 `deliveryEmail` 列表（token 存在实例里）。

        🔴 每单都是**真花钱**（`purchase` = 10 积分/个）。失败就抛，不静默
           少给地址 —— 少给会被 `stage_register` 当成「邮箱建不出来」，而
           真实原因（余额不足 / 库存不足 / 项目配置错）会被这条笼统错误盖住。
        """
        if not self.api_key:
            raise RuntimeError(
                "remail 缺 API Key。\n"
                "  修法：在 .env 里写 IR_REMAIL_API_KEY=rk-…（.env 已 gitignore）"
            )
        suffix = domain or self.suffix
        out: list[str] = []
        for _ in range(count):
            o = self._place_order(suffix)
            addr = str(o.get("deliveryEmail") or "")
            tok = str(o.get("serviceToken") or "")
            if not addr or not tok:
                raise RuntimeError(
                    f"remail 下单未返回完整凭据（orderNo={o.get('orderNo')}，"
                    f"邮箱={bool(addr)}，token={bool(tok)}，"
                    f"failureCode={o.get('failureCode')}）"
                )
            self._tokens[addr] = tok
            self._orders[addr] = str(o.get("orderNo") or "")
            out.append(addr)
        return out

    def _place_order(self, suffix: str) -> dict:
        idem = uuid.uuid4().hex  # 幂等键：同 key 同 idem 不会重复建单
        r = self.session.post(
            f"{self.base}/v1/open/orders",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Idempotency-Key": idem,
                "Content-Type": "application/json",
            },
            params={"serviceMode": self.service_mode, "supply": self.supply},
            json={"projectId": self.project_id, "emailSuffix": suffix},
            timeout=self.timeout,
        )
        body = _json_body(r)
        if r.status_code not in (200, 201) or body is None:
            raise RuntimeError(f"remail 下单失败 HTTP {r.status_code}: {(r.text or '')[:200]}")
        return body

    # ── 契约：收信 ────────────────────────────────────────────
    def _pickup(self, address: str, token: str) -> list:
        r = self.session.get(
            f"{self.base}/v1/pickup",
            params={"email": address, "token": token},
            timeout=self.timeout,
        )
        body = _json_body(r)
        if r.status_code != 200 or body is None:
            raise RuntimeError(f"remail 取件 HTTP {r.status_code}: {(r.text or '')[:160]}")
        return body.get("items") or []

    def _to_mail(self, address: str, token: str, item: dict) -> Mail:
        """取件条目 → `Mail`。

        🔴 **正文要单独拉**：`/v1/pickup` 的 `bodyPreview` 只是摘要，激活链接
           只在 `/v1/pickup/messages/{id}` 的完整正文里（HTML）。
        🔴 **`html.unescape` 是必须的**：正文里链接是 `&amp;` 转义，不反转义
           会把 query 截断（2026-10-03 实测：token 对、sign 丢）。
        """
        body = str(item.get("bodyPreview") or "")
        mid = item.get("id")
        if isinstance(mid, int) and mid > 0:
            try:
                r = self.session.get(
                    f"{self.base}/v1/pickup/messages/{mid}",
                    params={"email": address, "token": token},
                    timeout=self.timeout,
                )
                j = _json_body(r)
                if r.status_code == 200 and j:
                    body = str(j.get("body") or j.get("bodyHtml") or j.get("bodyText") or body)
            except Exception:  # noqa: BLE001
                # 正文拉不到就退回 preview —— 只降低 `find_link` 命中率，
                # 不该让整个收信流程失败（preview 里偶发也带链接）。
                pass
        text = html.unescape(body)
        links = _extract_links(text)
        return Mail(
            id=str(item.get("id") or ""),
            to_address=str(item.get("recipient") or address),
            from_address=str(item.get("sender") or ""),
            subject=str(item.get("subject") or ""),
            body=text,
            # `Mail.find_link` 只认 `extracted_json`（Worker 那侧是结构化给定），
            # 这里由我们抽好再喂进去，三种源的 `find_link` 行为才一致。
            extracted_json=json.dumps([{"value": u} for u in links]),
            received_at=_iso_ms(item.get("receivedAt")),
        )

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "openxlab",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询 `/v1/pickup` 等邮件。

        失败语义与另外三套对齐：`last_error` 为空 = 邮件没到（该等）；
        非空 = 读不出来（该修）。
        """
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        token = self._tokens.get(address)
        if not token:
            self.last_error = (
                f"remail 没有 {address} 的订单凭据（地址不是本实例 create_mailbox 下的单？）"
            )
            return None
        timeout = timeout or self.wait_timeout
        interval = interval or self.poll_interval
        want = (sender_contains or "").lower()
        deadline = time.time() + timeout
        errs = 0
        last_err = ""
        while time.time() < deadline:
            self.last_polls += 1
            try:
                items = self._pickup(address, token)
            except Exception as ex:  # noqa: BLE001
                errs += 1
                self.last_http_errors = errs
                last_err = f"{type(ex).__name__}: {ex}"
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                sender = str(it.get("sender") or "")
                recv = _iso_ms(it.get("receivedAt"))
                if since_ts and recv and recv < since_ts:
                    continue
                if want and want not in sender.lower():
                    continue
                return self._to_mail(address, token, it)
            time.sleep(interval)
        if errs:
            self.last_error = (
                f"remail 取件接口持续失败（{errs} 次，最近 {last_err}）—— 不是邮件没到，是读不出来"
            )
        return None

    def wait_for_activation_link(self, address: str, **kw) -> str | None:
        mail = self.wait_for_mail(address, **kw)
        if not mail:
            return None
        return mail.find_link("active", "activat", "verif", "confirm")


# ── 工厂 ──────────────────────────────────────────────────────────
# 🔴 键集合必须与 `config.MAILBOX_KINDS` **完全相等** —— 那是"校验认得的
#    取值"和"工厂认得的取值"的唯一真源，两边各写一份必然漂移
#    （`tests/test_mailbox.py::test_factory_and_validator_agree_on_kinds` 钉住）。
_KINDS: dict[str, type] = {
    "worker": WorkerMailbox,
    "imap": ImapMailbox,
    "chatai": ChataiMailbox,
    "remail": RemailMailbox,
}


def make_source(kind: str | None = None, **kw) -> MailboxSource:
    """按 `IR_MAILBOX_KIND` 造邮箱源。

    🔴 默认 `worker` ⇒ 与加这个工厂之前**行为完全一致**（返回的
    `WorkerMailbox` 是纯转发）。这是刻意的：换源必须是**显式动作**，
    不能让没配 `.env` 的人莫名其妙换到另一条路上。

    未知取值**直接抛错**，不静默退回 worker —— 拼错 `IR_MAILBOX_KIND`
    却跑了 worker，会让人以为「IMAP 那条路走不通」，而其实根本没走。
    """
    k = (kind or config.MAILBOX_KIND or "worker").strip().lower()
    cls = _KINDS.get(k)
    if cls is None:
        raise ValueError(f"未知的邮箱源 IR_MAILBOX_KIND={k!r}；只认 {' / '.join(sorted(_KINDS))}")
    return cls(**kw)
