"""邮箱源抽象层（`src/mailbox.py`）的契约与行为测试。

分两半，理由见 `docs/` 里反复出现的那条：**只测一半会留下"看着全绿"的口子**。

  1. **契约** —— 两个实现都满足 `MailboxSource`，且工厂的取值集合与
     `config.validate()` 认得的取值集合相等（两处各写一份必然漂移）。
  2. **行为** —— `WorkerMailbox` 是**纯转发**（差分：与直接调
     `TempMailClient` 的返回值逐字段相同）；`ImapMailbox` 的领用去重、
     域名过滤、池子耗尽即报错、`Mail` 字段口径（`received_at` 是**毫秒**）；
     `ChataiMailbox` 的加密信封（独立复算）、探活三态、死号 negative cache、
     「500 + JSON 也是业务失败」这条判据。

🔴 **全程不碰网络、不碰真实凭据**：
   · `WorkerMailbox` 注入假 client；
   · `ImapMailbox` 用 `monkeypatch` 换掉 `_connect`（唯一的网络出口），
     其余（SEARCH 条件构造、MIME 解析、链接提取、状态落盘）都是**真跑**；
   · `ChataiMailbox` 换掉 `self.session`（唯一的网络出口），其余
     （AES-GCM 信封、状态码判据、探活三态、状态落盘）都是**真跑**。

🔴 **`issubclass()` 不可用** —— `MailboxSource` 含数据成员
   （`last_error` / `last_polls` / `last_http_errors`），对它调 `issubclass`
   会抛 `TypeError: Protocols with non-method members ...`（Python 3.13 实测）。
   契约断言一律用 `isinstance`。
"""

from __future__ import annotations

import base64
import email
import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common import config
from src import mailbox
from src.mailbox import (
    ChataiMailbox,
    ImapMailbox,
    MailboxSource,
    RemailMailbox,
    WorkerMailbox,
    make_source,
    parse_chatai_accounts,
    parse_credentials,
)
from src.tempmail import Mail

# ── 夹具：假 Worker client / 假 IMAP 连接 ──────────────────────────

FAKE_MAIL = Mail(
    id="m1",
    to_address="a@x.com",
    from_address="no-reply@openxlab.org.cn",
    subject="激活",
    body="click https://sso.openxlab.org.cn/active?t=1",
    extracted_json='[{"value": "https://sso.openxlab.org.cn/active?t=1"}]',
    received_at=1789000000000,
)


class FakeWorkerClient:
    """`TempMailClient` 的最小替身 —— 记录调用、返回固定值。"""

    def __init__(self, mails=None, exc=None):
        self.mails = mails if mails is not None else [FAKE_MAIL]
        self.exc = exc
        self.calls: list[tuple] = []
        self.last_error = "fake-last-error"
        self.last_polls = 7
        self.last_http_errors = 3

    def create_mailbox(self, domain=None, count=1):
        self.calls.append(("create_mailbox", domain, count))
        if self.exc:
            raise self.exc
        return [f"u{i}@x.com" for i in range(count)]

    def wait_for_mail(
        self,
        address,
        sender_contains="openxlab",
        timeout=None,
        interval=None,
        since_ts=0,
        limit=None,
    ):
        self.calls.append(
            ("wait_for_mail", address, sender_contains, timeout, interval, since_ts, limit)
        )
        return self.mails[0] if self.mails else None

    def wait_for_activation_link(self, address, **kw):
        self.calls.append(("wait_for_activation_link", address, tuple(sorted(kw))))
        return "https://sso.openxlab.org.cn/active?t=1"


class FakeIMAP:
    """`imaplib.IMAP4_SSL` 的最小替身。

    ⚠ `search` 的签名必须收 `*criteria` —— `ImapMailbox._search_ids` 会把
      `FROM` / `TO` 条件摊开传进来，收成 `criteria=None` 会让"条件构造"
      这一整块逻辑**测不到**（那正是最容易写错的地方）。
    """

    def __init__(self, raws: list[bytes]):
        self.raws = raws
        self.search_args: tuple | None = None
        self.logged_out = False
        self.select_args: tuple | None = None

    def search(self, charset, *criteria):
        self.search_args = criteria
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.raws)))
        return "OK", [ids]

    def fetch(self, uid, spec):
        idx = int(uid) - 1
        return "OK", [(b"1 (RFC822)", self.raws[idx])]

    def select(self, folder, readonly=False):
        self.select_args = (folder, readonly)
        return "OK", [b"1"]

    def logout(self):
        self.logged_out = True
        return "BYE", []


def _raw_mail(
    *,
    to: str,
    frm: str = "no-reply@openxlab.org.cn",
    subject: str = "Activate your account",
    body: str = "click https://sso.openxlab.org.cn/active?t=abc",
    date: str = "Tue, 23 Sep 2026 03:00:00 +0000",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    return msg.as_bytes()


@pytest.fixture()
def pool(tmp_path):
    """一份隔离的凭据池 + 隔离的 used 状态文件。返回 `(credentials_path, used)`。"""
    creds = tmp_path / "creds.txt"
    creds.write_text(
        "a@x.com----pw-a\nb@x.com----pw-b\nc@y.com----pw-c\n",
        encoding="utf-8",
    )
    return creds, tmp_path / "used.json"


def _imap(pool, raws=None, **kw) -> tuple[ImapMailbox, FakeIMAP]:
    """造一个把网络出口换掉的 `ImapMailbox`。"""
    creds, used = pool
    m = ImapMailbox(credentials=str(creds), used_state=str(used), **kw)
    fake: Any = FakeIMAP(raws if raws is not None else [])
    m._connect = lambda address: fake  # ← 唯一的网络出口
    return m, fake


# ══════════════════════════════════════════════════════════════════
# 1. 契约
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", ["worker", "imap", "chatai", "remail"])
def test_every_implementation_satisfies_the_protocol(kind, pool, tmp_path, monkeypatch):
    """每个实现都必须满足 `MailboxSource`。

    🔴 用 `isinstance` 而不是 `issubclass` —— 见模块 docstring。
    """
    monkeypatch.setattr(config, "MAILBOX_KIND", kind)
    if kind == "imap":
        src = _imap(pool)[0]
    elif kind == "chatai":
        src = ChataiMailbox(
            accounts=str(_accounts_file(tmp_path, ["u0@outlook.com----p----c----r"])),
            state=str(tmp_path / "s.json"),
        )
    elif kind == "remail":
        src = RemailMailbox(api_key="rk-test")
    else:
        src = WorkerMailbox(FakeWorkerClient())
    assert isinstance(src, MailboxSource)


def test_factory_and_validator_agree_on_kinds():
    """工厂认得的取值 == `config.MAILBOX_KINDS`。

    🔴 这条是给**漂移**设的闸：两处各写一份取值集合，早晚会一边加了一边没加，
      表现为「`validate()` 说配置没问题，`make_source()` 却抛未知取值」——
      而报错文案指向的是"配置写错了"，方向完全反了。
    """
    assert set(mailbox._KINDS) == set(config.MAILBOX_KINDS)


def test_unknown_kind_raises_instead_of_falling_back_to_worker():
    """未知取值必须抛错。

    🔴 静默退回 worker 的后果：拼错 `IR_MAILBOX_KIND` 的人会看到"跑通了"，
      于是得出「IMAP 那条路走不通」的**错误结论**，而其实根本没走那条路。
    """
    with pytest.raises(ValueError) as ei:
        make_source("imapp")  # 故意拼错
    msg = str(ei.value)
    assert "imapp" in msg and "imap" in msg and "worker" in msg
    assert "chatai" in msg, "报错文案必须列出**全部**合法取值"


def test_make_source_defaults_to_worker(monkeypatch):
    """不配 `IR_MAILBOX_KIND` 时必须还是 worker（行为零变化的判据）。"""
    monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
    assert isinstance(make_source(), WorkerMailbox)


# ══════════════════════════════════════════════════════════════════
# 2. WorkerMailbox —— 纯转发（差分）
# ══════════════════════════════════════════════════════════════════


def test_worker_mailbox_forwards_everything_verbatim():
    """差分：`WorkerMailbox` 的返回值与参数必须与直连 client **逐项相同**。

    🔴 判别力在哪：把转发写成"加工一下"（比如改默认值、吞异常、
      或把 `last_polls` 读成快照）都会让下面任一条断言变红。
    """
    client = FakeWorkerClient()
    w = WorkerMailbox(client)

    assert w.create_mailbox(domain="x.com", count=2) == ["u0@x.com", "u1@x.com"]
    assert client.calls[-1] == ("create_mailbox", "x.com", 2)

    got = w.wait_for_mail(
        "a@x.com", sender_contains="openxlab", timeout=11, interval=0.5, since_ts=123, limit=9
    )
    assert got is FAKE_MAIL  # 同一个对象，不是重建的
    assert client.calls[-1] == ("wait_for_mail", "a@x.com", "openxlab", 11, 0.5, 123, 9)

    assert (
        w.wait_for_activation_link("a@x.com", timeout=3) == "https://sso.openxlab.org.cn/active?t=1"
    )


def test_worker_mailbox_state_is_passthrough_not_snapshot():
    """状态必须**透传**，不能是构造时的快照。

    🔴 判别力：`stage_register` 是在 `wait_for_mail()` **之后**才读这三个值。
      快照实现会让它们永远停在构造时的初值 —— 而初值恰好是
      `""` / `0` / `0`，看起来完全像"邮件没到、零次轮询"，不报错。
    """
    client = FakeWorkerClient()
    w = WorkerMailbox(client)
    assert (w.last_error, w.last_polls, w.last_http_errors) == ("fake-last-error", 7, 3)

    client.last_error, client.last_polls, client.last_http_errors = "变了", 42, 9
    assert (w.last_error, w.last_polls, w.last_http_errors) == ("变了", 42, 9)


def test_worker_mailbox_does_not_swallow_exceptions():
    """转发不能吞异常 —— `stage_register` 靠异常把失败归类。"""
    w = WorkerMailbox(FakeWorkerClient(exc=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        w.create_mailbox()


# ══════════════════════════════════════════════════════════════════
# 3. ImapMailbox —— 领用去重 / 域名过滤 / 耗尽即报错
# ══════════════════════════════════════════════════════════════════


def test_imap_never_hands_out_the_same_address_twice(pool):
    """🔴 本文件最重要的一条：同一个地址绝不会被领两次。

    判别力：忽略 used 状态的实现会**每次都返回 `a@x.com`** ——
    于是同一个邮箱被两个账号共用，第二个账号永远收不到激活邮件，
    而报出来的是「激活邮件超时」，离真正原因很远。
    """
    m, _ = _imap(pool)
    first = m.create_mailbox(count=1)
    second = m.create_mailbox(count=1)
    third = m.create_mailbox(count=1)

    assert first == ["a@x.com"]
    assert second == ["b@x.com"]
    assert third == ["c@y.com"]
    assert len({first[0], second[0], third[0]}) == 3


def test_imap_used_state_is_persisted_and_survives_a_new_instance(pool):
    """领用记录必须**落盘** —— 换一个实例（= 下一次跑批）也要接着往下领。

    判别力：只在内存里去重的实现，重跑一次批次就会把池子从头领一遍。
    """
    creds, used = pool
    m1, _ = _imap(pool)
    assert m1.create_mailbox(count=2) == ["a@x.com", "b@x.com"]
    assert used.is_file()

    # 新实例、同一份状态文件 ⇒ 只能领到剩下的那个
    m2 = ImapMailbox(credentials=str(creds), used_state=str(used))
    assert m2.create_mailbox(count=1) == ["c@y.com"]

    raw = json.loads(used.read_text(encoding="utf-8"))
    assert raw[str(creds)] == ["a@x.com", "b@x.com", "c@y.com"]


def test_imap_exhausted_pool_raises_and_writes_nothing(pool):
    """池子不够时必须**抛错**，而且**不得产生部分副作用**。

    🔴 两条断言缺一不可：
      · 只断言"抛了异常" —— 一个先写 used 再抛的实现照样通过；
      · 只断言"文件没变" —— 一个静默少给几个的实现照样通过。
      少给几个的后果：`stage_register` 在 `emails[0]` 上抛 `IndexError`，
      报错点离原因十万八千里。

    ⚠ 用 `count=3` 领完再领 —— 此时池子剩 0 个。
    """
    creds, used = pool
    m, _ = _imap(pool)
    assert m.create_mailbox(count=3) == ["a@x.com", "b@x.com", "c@y.com"]
    before = used.read_bytes()

    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox(count=1)
    assert "只剩 0 个" in str(ei.value)
    assert used.read_bytes() == before, "报错路径不得改动 used 状态文件"


def test_imap_domain_filter_only_takes_from_that_domain(pool):
    """`domain` 过滤必须生效（大小写不敏感），且**不得跨域名**。

    判别力：忽略 domain 的实现会返回 `a@x.com`，而调用方以为自己拿到了
    一个 `@y.com` 的地址 —— 域名门是按域名判的，拿错域名等于白跑。
    """
    m, _ = _imap(pool)
    assert m.create_mailbox(domain="y.com", count=1) == ["c@y.com"]

    # y.com 已经空了。用**大写**再要一次：既验证大小写不敏感，
    # 也验证它**没有**跨到 x.com 去凑数（凑数会返回 a@x.com）。
    with pytest.raises(RuntimeError, match=r"限制了域名 @y\.com"):
        m.create_mailbox(domain="Y.COM", count=1)

    # 反证：x.com 那边还有 2 个，说明上面那次确实是"过滤挡住的"，
    # 不是"池子整体空了"。
    assert m.create_mailbox(domain="x.com", count=2) == ["a@x.com", "b@x.com"]


def test_imap_missing_credentials_file_raises_with_the_path(tmp_path):
    """凭据文件不存在时，报错必须**带上路径** —— 否则用户不知道该改哪一项。"""
    missing = tmp_path / "nope.txt"
    m = ImapMailbox(credentials=str(missing), used_state=str(tmp_path / "u.json"))
    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox(count=1)
    assert str(missing) in str(ei.value)
    assert "IR_IMAP_CREDENTIALS" in str(ei.value)


def test_imap_concurrent_claim_never_duplicates(pool):
    """并发领用不得发出重复地址（`create_mailbox` 是 read-modify-write）。

    判别力：`pipeline.run_batch` 会从多个 producer 线程同时调它。
      去掉 `mailbox._LOCK` 后，两个线程会各自读到同一份 used 快照、
      各自认为 `a@x.com` 可用 —— 然后**两边都领到它**。

    ⚠ 这条断言的是**确定性不变量**（正确实现下永真），所以不会 flaky；
      只是它在错误实现下**不保证**每次都红（GIL 会掩盖一部分交错）。
    """
    creds, used = pool
    big = creds.with_name("big.txt")
    big.write_text("".join(f"u{i}@x.com----pw{i}\n" for i in range(24)), encoding="utf-8")
    m = ImapMailbox(credentials=str(big), used_state=str(used))

    got: list[str] = []
    lock = threading.Lock()

    def worker():
        addr = m.create_mailbox(count=1)[0]
        with lock:
            got.append(addr)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(got) == 24
    assert len(set(got)) == 24, f"并发领用发出了重复地址：{sorted(got)}"


# ══════════════════════════════════════════════════════════════════
# 4. ImapMailbox —— 收信（Mail 字段口径）
# ══════════════════════════════════════════════════════════════════


def test_imap_wait_for_mail_returns_a_mail_with_expected_fields(pool):
    """收信主路径：字段、链接、`received_at` 的**单位**都要对。"""
    raws = [
        _raw_mail(to="a@x.com", body="Hi,\nactivate: https://sso.openxlab.org.cn/active?t=zz\n")
    ]
    m, fake = _imap(pool, raws)

    got = m.wait_for_mail("a@x.com", timeout=5, interval=0.01)

    assert got is not None
    assert got.from_address == "no-reply@openxlab.org.cn"
    assert got.to_address == "a@x.com"
    assert got.subject == "Activate your account"
    assert "https://sso.openxlab.org.cn/active?t=zz" in got.links
    assert got.find_link("active") == "https://sso.openxlab.org.cn/active?t=zz"
    assert m.last_polls >= 1 and m.last_http_errors == 0
    assert m.last_error == ""
    assert fake.logged_out is True, "连接必须被关掉（否则句柄泄漏）"


def test_imap_received_at_is_milliseconds(pool):
    """🔴 `received_at` 必须是**毫秒**。

    判别力：`stage_register` 用 `m.received_at > 1e11` 判单位 ——
      返回秒（1.79e9）会走错分支，`arrival_delay_ms` 直接算成
      **几十万毫秒的负数/怪数**，而报告里只是"耗时有点怪"，不报错。

    oracle 刻意用**另一条代码路径**算（`datetime.timestamp()`），
      不复用被测实现里的 `email.utils.mktime_tz`。
    """
    raws = [_raw_mail(to="a@x.com", date="Tue, 23 Sep 2026 03:00:00 +0000")]
    m, _ = _imap(pool, raws)
    got = m.wait_for_mail("a@x.com", timeout=5, interval=0.01)

    expect = int(datetime(2026, 9, 23, 3, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert got is not None
    assert got.received_at == expect
    assert got.received_at > 1e11, "必须是毫秒（秒级时间戳会走错 pipeline 的分支）"


def test_imap_search_criteria_carry_sender_and_recipient(pool):
    """SEARCH 条件必须同时带 `FROM` 与 `TO`。

    🔴 判别力：漏掉 `TO <自己>` 的实现会把同一个 IMAP 账号下**别人的邮件**
      也捞进来；漏掉 `FROM` 则会把任何发给自己的邮件都当成激活邮件。
      两种情况下报出来的都是「找不到激活链接」，离真正原因很远。
    """
    m, fake = _imap(pool, [_raw_mail(to="a@x.com")])
    m.wait_for_mail("a@x.com", sender_contains="openxlab", timeout=5, interval=0.01)
    assert fake.search_args == ("FROM", '"openxlab"', "TO", '"a@x.com"')


def test_imap_respects_since_ts_and_times_out_cleanly(pool):
    """`since_ts` 之后的邮件才算；都不满足时**超时返回 None** 且 `last_error` 为空。

    🔴 「邮件没到」(`last_error == ""`) 与「读不出来」(非空) 必须分开 ——
      前者该等，后者该修，两者的处置完全不同（见 `tempmail.wait_for_mail`）。
    """
    m, _ = _imap(pool, [_raw_mail(to="a@x.com")])
    got = m.wait_for_mail("a@x.com", timeout=1, interval=0.05, since_ts=9_999_999_999_999)  # 远未来
    assert got is None
    assert m.last_error == "", "纯粹是没到，不该报成读不出来"


def test_imap_connection_failure_is_reported_as_last_error(pool):
    """连接层失败要进 `last_error`（与「邮件没到」区分开）。"""
    m, _ = _imap(pool)

    def boom(address):
        raise OSError("connection refused")

    m._connect = boom
    got = m.wait_for_mail("a@x.com", timeout=1, interval=0.05)
    assert got is None
    assert "IMAP 读信持续失败" in m.last_error
    assert m.last_http_errors >= 1


def test_imap_never_leaks_the_password_into_last_error(pool):
    """🔴 密码绝不能出现在 `last_error` 里。

    判别力：`last_error` 会被 `stage_register` 写进 `rec.error` → 台账
      → stdout。一个把异常原样拼进去的实现在这里就漏了 ——
      而凭据进公开仓库是本项目最贵的一类事故。
    """
    secret = "pw-a"  # 池里 a@x.com 的密码
    m, _ = _imap(pool)

    def boom(address):
        # 模拟"异常里恰好带了密码"（imaplib 在 debug 打开时就会这样）
        raise OSError(f"login failed for {address} with {secret}")

    m._connect = boom
    m.wait_for_mail("a@x.com", timeout=1, interval=0.05)

    assert secret not in m.last_error
    assert "***" in m.last_error


# ══════════════════════════════════════════════════════════════════
# 5. 纯函数：凭据解析 / 链接提取 / 正文选取
# ══════════════════════════════════════════════════════════════════


def test_parse_credentials_takes_first_two_segments_only():
    """`邮箱----密码----恢复邮箱` —— 第三段是**恢复邮箱**，不是 token。

    🔴 这条是有来历的：2026-09-23 初判时把第三段当成 16 字符 token，
      差点把「IMAP 是否可行」判反（见 `docs/protocol.md`）。
      所以这里显式钉住"第三段被丢掉"。
    """
    text = (
        "# 注释行\n"
        "\n"
        "a@x.com----pw1----recovery@outlook.com\n"
        "b@x.com----pw2\n"
        "没有分隔符的行\n"
        "c@x.com----\n"  # 密码为空 ⇒ 跳过
        "----pw3\n"  # 邮箱为空 ⇒ 跳过
        "  d@x.com  ----  pw4  \n"  # 两侧空白要 strip
    )
    assert parse_credentials(text) == {"a@x.com": "pw1", "b@x.com": "pw2", "d@x.com": "pw4"}


def test_parse_credentials_keeps_file_order():
    """顺序必须保持（`create_mailbox` 靠它决定"先领哪个"）。"""
    text = "z@x.com----1\nm@x.com----2\na@x.com----3\n"
    assert list(parse_credentials(text)) == ["z@x.com", "m@x.com", "a@x.com"]


def test_extract_links_unescapes_entities_and_trims_punctuation():
    """HTML 实体要解码、尾部标点要剪掉。

    🔴 判别力：不解 `&amp;` 的实现会给出 `?a=1&amp;b=2` —— 这个 URL
      在浏览器里查询串被截断，**激活会失败**，而日志里的链接"看着是完整的"。
    """
    body = (
        '<a href="https://sso.openxlab.org.cn/active?t=1&amp;u=2">x</a>，'
        "另见 https://example.com/a。 和 (https://example.com/b)"
    )
    assert mailbox._extract_links(body) == [
        "https://sso.openxlab.org.cn/active?t=1&u=2",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_body_text_prefers_plain_and_skips_attachments():
    """优先 `text/plain`；附件里的链接不得混进匹配面。"""
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg.set_content("PLAIN-BODY https://plain.example/x")
    msg.add_alternative("<p>HTML-BODY https://html.example/y</p>", subtype="html")
    msg.add_attachment(
        b"https://attachment.example/z", maintype="text", subtype="plain", filename="note.txt"
    )

    got = mailbox._body_text(msg)
    # ⚠ 用 strip() 比：`set_content` 会在正文尾部补一个换行，那是
    #   EmailMessage 的行为、不是被测逻辑的一部分。
    assert got.strip() == "PLAIN-BODY https://plain.example/x"
    assert "attachment.example" not in got, "附件正文混进了匹配面"
    assert "html.example" not in got, "有 text/plain 时不该退回 HTML"


def test_body_text_falls_back_to_html_when_no_plain_part():
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg.set_content("<p>only html https://html.example/y</p>", subtype="html")
    assert "https://html.example/y" in mailbox._body_text(msg)


def test_received_ms_returns_zero_on_unparseable_date():
    """坏 `Date` 头不该让整条收信流程失败 —— 退回 0（未知）。"""
    msg = email.message_from_string("From: a@x.com\nDate: 不是日期\n\nbody\n")
    assert mailbox._received_ms(msg) == 0
    assert mailbox._received_ms(email.message_from_string("From: a@x.com\n\nx\n")) == 0


# ══════════════════════════════════════════════════════════════════
# 6. 状态文件位置 / config.validate() 的分支
# ══════════════════════════════════════════════════════════════════


def test_used_state_path_honors_env_override(tmp_path, monkeypatch):
    """`IR_IMAP_USED_STATE` 必须生效，且是**调用时**读（自检脚本靠它隔离）。"""
    target = tmp_path / "custom.json"
    monkeypatch.setenv("IR_IMAP_USED_STATE", str(target))
    assert mailbox.used_state_path() == target

    monkeypatch.delenv("IR_IMAP_USED_STATE", raising=False)
    default = mailbox.used_state_path()
    assert default.name == "imap_used.json"
    assert ".workbuddy-ai" in default.parts and "state" in default.parts


def test_used_state_default_lives_outside_the_credentials_directory():
    """🔴 状态文件**不得**落在凭据文件旁边。

    凭据文件通常由用户从下载目录直接指向 —— 往那儿写状态文件等于
    污染用户目录（本机规矩：不动用户的个人目录）。
    """
    default = mailbox.used_state_path()
    assert default.parent.name == "state"
    assert "Downloads" not in default.parts and "Desktop" not in default.parts


def _validate_with(monkeypatch, kind, *, need_worker_token=True, **attrs):
    """在**与槽位配置无关**的条件下跑 `validate()`。

    🔴 必须把槽位相关的常量清空 —— 否则结果取决于本机 `.env`：配了槽位池
      却没填 `IR_SLOT_EGRESS_IPS` 时 `validate()` 会**多报一项**，
      于是这些用例在本机绿、在别人机器 / CI 上红。
      **断言里混进环境状态，等于这个断言没在测被测对象。**
    """
    monkeypatch.setattr(config, "MAILBOX_KIND", kind)
    monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
    monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
    for k, v in attrs.items():
        monkeypatch.setattr(config, k, v)
    return config.validate(need_worker_token=need_worker_token)


def test_validate_worker_branch_is_unchanged(monkeypatch):
    """worker 分支必须与引入开关之前**逐项相同**。

    🔴 这是"默认行为零变化"的判据：漏配时报告的缺项名一字不差，
      否则用户照着 `.env.example` 补也补不对。
    """
    missing = _validate_with(
        monkeypatch, "worker", WORKER_ADMIN_TOKEN="", WORKER_BASE="", WORKER_DOMAIN=""
    )
    assert missing[:3] == ["IR_WORKER_ADMIN_TOKEN", "IR_WORKER_BASE", "IR_WORKER_DOMAIN"]

    missing = _validate_with(
        monkeypatch,
        "worker",
        WORKER_ADMIN_TOKEN="t",
        WORKER_BASE="https://x",
        WORKER_DOMAIN="d.com",
    )
    assert missing == []


def test_validate_worker_branch_skips_token_when_not_needed(monkeypatch):
    """`need_worker_token=False` 的老语义必须保留（离线分析路径在用）。"""
    missing = _validate_with(
        monkeypatch,
        "worker",
        need_worker_token=False,
        WORKER_ADMIN_TOKEN="",
        WORKER_BASE="https://x",
        WORKER_DOMAIN="d.com",
    )
    assert missing == []


def test_validate_imap_branch_requires_credentials_file(monkeypatch, tmp_path):
    """imap 分支：**只**看凭据文件，不再要求 `IR_WORKER_*`。

    🔴 判别力：若 `validate()` 仍无条件检查 `IR_WORKER_*`，IMAP 模式下
      会报"缺 IR_WORKER_BASE"，把人引向一个**跟当前源毫无关系**的配置项。
    """
    monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "WORKER_BASE", "")
    monkeypatch.setattr(config, "WORKER_DOMAIN", "")

    missing = _validate_with(monkeypatch, "imap", IMAP_CREDENTIALS="")
    assert missing == ["IR_IMAP_CREDENTIALS"]

    ghost = tmp_path / "ghost.txt"
    missing = _validate_with(monkeypatch, "imap", IMAP_CREDENTIALS=str(ghost))
    assert len(missing) == 1 and "文件不存在" in missing[0] and str(ghost) in missing[0]

    real = tmp_path / "real.txt"
    real.write_text("a@x.com----pw\n", encoding="utf-8")
    assert _validate_with(monkeypatch, "imap", IMAP_CREDENTIALS=str(real)) == []


def test_validate_rejects_unknown_kind(monkeypatch):
    """未知 `IR_MAILBOX_KIND` 要在启动阶段报出来，并列出合法取值。"""
    missing = _validate_with(monkeypatch, "imapp")
    assert len(missing) == 1
    assert "IR_MAILBOX_KIND" in missing[0]
    assert "worker" in missing[0] and "imap" in missing[0]
    assert "chatai" in missing[0], "报错文案必须列出**全部**合法取值"


def test_imap_credentials_path_is_expanduser_ed(tmp_path, monkeypatch):
    """`~` 要展开 —— `.env` 里写 `~/creds.txt` 是常见写法。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "creds.txt").write_text("a@x.com----pw\n", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    m = ImapMailbox(credentials="~/creds.txt", used_state=str(tmp_path / "u.json"))
    assert m.create_mailbox(count=1) == ["a@x.com"]


def test_no_construction_site_bypasses_the_factory():
    """🔴 全树静态断言：除 `src/mailbox.py` 自身外，任何地方都不得直接构造
    `TempMailClient`；且工厂调用点必须达到预期数量。

    判别力：只要有一处漏改，那处就会**永远走 Worker**，而
      `IR_MAILBOX_KIND=imap` 看起来"生效了"（别处走了 IMAP）——
      表现为"有些账号收得到信、有些收不到"，最难查的一类现象。
      探针漏改更坏：给出「探针绿、跑批红」的**假绿**。

    走 AST 而不是文本匹配：注释里出现这个字符串不算违规
      （本项目踩过"被自己刚写的注释判红"的坑，假阳性比漏检更坏）。

    ⚠ 两条断言缺一不可：
      · 只断言"没有违规" ⇒ 扫描器一旦坏掉（glob 写错 / 路径变了）就**永远为真**；
      · 只断言"工厂调用够多" ⇒ 漏改一处照样通过。
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    exempt = root / "src" / "mailbox.py"  # WorkerMailbox 就是包它的地方
    sources = sorted({*root.glob("src/**/*.py"), *root.glob("tools/**/*.py")})
    assert len(sources) > 20, (
        f"只扫到 {len(sources)} 个文件 —— glob 或目录结构变了，断言会退化成永远为真"
    )

    bypass: list[str] = []
    factory: list[str] = []
    for py in sources:
        if py == exempt:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            rel = py.relative_to(root).as_posix()
            if name == "TempMailClient":
                bypass.append(f"{rel}:{node.lineno}")
            elif name == "make_source":
                factory.append(f"{rel}:{node.lineno}")

    assert not bypass, (
        "这些地方仍在直接构造 TempMailClient —— 换源时它们不会跟着换：\n  " + "\n  ".join(bypass)
    )

    # 工厂调用点：pipeline 2 处 + 6 个探针/工具各 1 处 = 8 处。
    # 用**下界**而不是等值 —— 将来新增调用点是好事，不该让断言变红；
    # 但"一处都找不到"必须是红的（那是扫描器坏了，不是代码变好了）。
    assert len(factory) >= 8, (
        f"工厂调用点只有 {len(factory)} 处（预期 ≥8）—— 要么接线被回退了，要么扫描器坏了：{factory}"
    )


# ══════════════════════════════════════════════════════════════════
# 7. ChataiMailbox —— 加密信封 / 探活三态 / 领用与状态
# ══════════════════════════════════════════════════════════════════
# 夹具设计：`ChataiMailbox` 的网络出口是 `self.session`（requests.Session），
# 所以把它整个换成 `FakeChataiSession` 之后，`_ensure_session` / `_envelope` /
# `_post` / `_fetch_mails` / `_probe` / `create_mailbox` / `wait_for_mail`
# **全部真跑**，包括 AES-GCM 加密与 HMAC 签名。
# ⚠ 只 patch 一个函数（比如 `_fetch_mails`）会让加密信封与状态码判据**测不到**，
#   而那两处恰恰是最容易写错、且错了会静默失效的地方。

CHATAI_KEY = bytes(range(32))  # AES-256-GCM 的合法密钥长度
CHATAI_SESSION = {
    "success": True,
    "sessionId": "sid-1",
    "sessionToken": "stok-1",
    "sessionKey": base64.b64encode(CHATAI_KEY)
    .decode()
    .replace("+", "-")
    .replace("/", "_")
    .rstrip("="),
    "expiresAt": "2030-01-01T00:00:00Z",
}

_NOT_JSON = object()


class FakeResponse:
    """`requests.Response` 的最小替身。

    🔴 `json()` 与 `raise_for_status()` 必须**各自独立**：被测逻辑要区分的
       正是这两件事 —— 读信页用 `500 + JSON` 报业务失败，一个"先
       raise_for_status"的实现会把 body 吞掉（实测踩过）。
    """

    def __init__(self, status_code, body, *, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)

    def json(self):
        if self._body is _NOT_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class FakeChataiSession:
    """`requests.Session` 的最小替身 —— chatai 侧**唯一的网络出口**。

    ⚠ 安全会话端点**自动应答**（它是基础设施，不是被测对象），其余端点按
      预置队列依次弹出。会话调用单独计数，用来钉"会话被缓存"。
    """

    def __init__(self, responses=(), *, always=None, session_response=None):
        self.responses = list(responses)
        self.always = always
        self.session_response = session_response or FakeResponse(200, dict(CHATAI_SESSION))
        self.calls: list[tuple[str, dict]] = []  # 只记业务端点
        self.session_calls = 0
        self.headers: dict = {}
        self.trust_env = True

    def post(self, url, json=None, timeout=None):  # noqa: A002 - 对齐 requests
        path = url.split("/api/", 1)[-1]
        if path == "security-session":
            self.session_calls += 1
            if isinstance(self.session_response, BaseException):
                raise self.session_response
            return self.session_response
        self.calls.append((path, json or {}))
        r = (
            self.always
            if self.always is not None
            else (self.responses.pop(0) if self.responses else None)
        )
        if r is None:
            raise AssertionError(f"预置响应用完了，又来了一次 {path}")
        if isinstance(r, BaseException):
            raise r
        return r


def _accounts_file(tmp_path, lines) -> Path:
    p = tmp_path / "chatai_accounts.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _chatai(tmp_path, *, lines=None, responses=(), always=None):
    """造一个把网络出口换成假 session 的 `ChataiMailbox`。返回 `(mailbox, fake)`。"""
    if lines is None:
        lines = ["u0@outlook.com----pw0----cid-0----rtok-0"]
    accounts = _accounts_file(tmp_path, lines)
    m = ChataiMailbox(accounts=str(accounts), state=str(tmp_path / "chatai_state.json"))
    fake: Any = FakeChataiSession(responses, always=always)
    m.session = fake
    return m, fake


def _payload_of(fake, index=-1) -> dict:
    """解开假 session 记录的第 `index` 次业务请求的信封 → 原始 payload。

    🔴 用它断言"**发出去**的是什么"，而不只是"收到了什么"：比如
      `sender` / `keyword` 必须传空串（服务端不做模糊匹配）—— 这条只看
      返回值永远查不出来。顺带也就验证了信封能被 sessionKey 解开。
    """
    env = fake.calls[index][1]
    iv = mailbox._b64u_dec(env["iv"])
    ct = mailbox._b64u_dec(env["ciphertext"])
    return json.loads(AESGCM(CHATAI_KEY).decrypt(iv, ct, None).decode("utf-8"))


def _chatai_mail_json(over: dict | None = None) -> dict:
    """读信页返回的一封邮件（字段名照抄实测响应）。"""
    m = {
        "id": "<abc@dm.openxlab.org.cn>",
        "messageId": "<abc@dm.openxlab.org.cn>",
        "subject": "【OpenXLab】注册激活",
        "from": "no-reply@dm.openxlab.org.cn",
        "fromName": "OpenXLab",
        "date": "2026-09-23T18:09:37Z",
        "bodyPreview": "欢迎使用 OpenXLab 平台，请在 2 小时内激活账户。",
        "bodyText": "",
        "bodyHtml": '<a href="https://sso.openxlab.org.cn/active?token=t1&amp;sign=s1">激活</a>',
    }
    m.update(over or {})
    return m


def _ok(mails=()) -> FakeResponse:
    mails = list(mails)
    return FakeResponse(
        200, {"success": True, "protocol": "graph", "count": len(mails), "emails": mails}
    )


def _dead(code="TOKEN_EXPIRED_OR_REVOKED", *, status=500, error=None) -> FakeResponse:
    """死号的**真实**响应：HTTP 500 + 结构化 JSON（实测，不是推测）。"""
    return FakeResponse(
        status,
        {
            "success": False,
            "code": code,
            "error": error or "刷新令牌无效或已过期，请重新获取 refresh_token",
            "detail": "Token 刷新失败: invalid_grant - AADSTS70000: The user could "
            "not be authenticated as the grant is expired.",
        },
    )


def test_chatai_envelope_is_aes_gcm_and_hmac_over_the_frontend_text(tmp_path):
    """信封必须能被 `sessionKey` 解开，且签名覆盖前端约定的那串文本。

    🔴 这是整条链路的地基：信封差一个字节服务端就 401，整个源等于死的。
      判别力全部来自**独立复算**（这里不复用被测实现的任何一行）：
        · 解密结果必须**逐字节等于** `json.dumps(..., ensure_ascii=False)`
          —— 把 `ensure_ascii` 打开会让中文变成 `\\uXXXX`，字节不同 ⇒ 签名对不上；
        · 签名必须等于 `HMAC-SHA256(key, "sessionId.nonce.timestamp.iv.ciphertext")`
          —— 拼接顺序或分隔符写错都会红。
    """
    m, _ = _chatai(tmp_path)
    payload = {
        "email": "u0@outlook.com",
        "keyword": "",
        "sender": "",
        "limit": 1,
        "备注": "中文不能转义",
    }
    env = m._envelope(dict(CHATAI_SESSION), payload)

    assert env["secure"] is True
    assert env["sessionId"] == "sid-1" and env["sessionToken"] == "stok-1"
    assert len(mailbox._b64u_dec(env["iv"])) == 12, "AES-GCM 的 IV 必须是 12 字节"

    plain = AESGCM(CHATAI_KEY).decrypt(
        mailbox._b64u_dec(env["iv"]), mailbox._b64u_dec(env["ciphertext"]), None
    )
    assert plain == json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert "中文不能转义".encode() in plain, "中文被转义了（ensure_ascii 必须为 False）"

    signed = ".".join(
        [env["sessionId"], env["nonce"], str(env["timestamp"]), env["iv"], env["ciphertext"]]
    )
    assert (
        mailbox._b64u_dec(env["signature"])
        == hmac.new(CHATAI_KEY, signed.encode("utf-8"), hashlib.sha256).digest()
    )


def test_chatai_dead_account_is_unreadable_not_missing_mail(tmp_path):
    """死号必须落进 `last_error`，且文案要**说清是账号失效**。

    🔴 钉的是实测踩过的坑：读信页用 `HTTP 500 + JSON` 报业务失败。一个先
      `raise_for_status()` 的实现会把 body 吞掉，于是死号被当成"服务端故障"：
      `last_error` 变成"读信页持续失败（N 次…）"、N 一直涨到超时，探活也
      永远拿不到 False ⇒ 死号标不出来。
      ⇒ 断言必须**同时**钉文案与轮询次数；只看"`last_error` 非空"的话
        两种实现都能过，等于没测。
    """
    m, _ = _chatai(tmp_path, responses=[_dead()])
    got = m.wait_for_mail("u0@outlook.com", timeout=3, interval=0.01)

    assert got is None
    assert "账号已失效" in m.last_error
    assert "TOKEN_EXPIRED_OR_REVOKED" in m.last_error
    assert m.last_polls == 1, "已确认失效就不该继续轮询（等下去也不会好）"
    assert m.last_http_errors == 0, "这是业务失败，不是传输失败"


def test_chatai_alive_mail_becomes_a_mail_with_millisecond_timestamp(tmp_path):
    """收信主路径：字段、链接、`received_at` 单位，以及**发出去的参数**。"""
    m, fake = _chatai(tmp_path, responses=[_ok([_chatai_mail_json()])])
    got = m.wait_for_mail("u0@outlook.com", sender_contains="openxlab", timeout=5, interval=0.01)

    assert got is not None
    assert got.from_address == "no-reply@dm.openxlab.org.cn"
    assert got.subject == "【OpenXLab】注册激活"
    assert got.to_address == "u0@outlook.com"
    assert got.received_at == int(datetime(2026, 9, 23, 18, 9, 37, tzinfo=UTC).timestamp() * 1000)
    assert got.received_at > 1e11, "必须是毫秒（秒级时间戳会走错 pipeline 的分支）"
    assert (
        got.find_link("active", "activat", "verif", "confirm")
        == "https://sso.openxlab.org.cn/active?token=t1&sign=s1"
    )
    assert m.last_error == "" and m.last_http_errors == 0

    sent = _payload_of(fake)
    assert sent["sender"] == "" and sent["keyword"] == "", (
        "sender/keyword 必须传空串 —— 服务端不做模糊匹配，实测传 openxlab 返回 0 封"
    )
    assert sent["email"] == "u0@outlook.com"
    assert sent["refreshToken"] == "rtok-0"


def test_chatai_sender_filter_runs_locally(tmp_path):
    """`sender_contains` 必须在**本地**筛：非匹配的邮件不得被当成激活邮件。

    判别力：把筛选交给服务端的实现会返回第一封（x.ai 的），而 `find_link`
      在它身上返回 None —— 报出来是「找不到激活链接」，离真因很远。
    """
    mails = [
        _chatai_mail_json(
            {
                "id": "<x@x.ai>",
                "from": "noreply@x.ai",
                "subject": "Your code",
                "bodyHtml": "https://x.ai/confirm?c=9",
            }
        ),
        _chatai_mail_json(),
    ]
    m, _ = _chatai(tmp_path, responses=[_ok(mails)])
    got = m.wait_for_mail("u0@outlook.com", sender_contains="openxlab", timeout=5, interval=0.01)
    assert got is not None
    assert got.subject == "【OpenXLab】注册激活"


@pytest.mark.parametrize(
    ("resp", "expect"),
    [
        (_ok([]), True),
        (_dead(), False),
        (_dead("UPSTREAM_TIMEOUT", error="上游超时，请稍后重试"), None),
        (requests.ConnectionError("boom"), None),
    ],
)
def test_chatai_probe_is_three_state_and_only_evidence_marks_death(tmp_path, resp, expect):
    """探活必须是**三态**，且只有"证据确凿"才判死。

    🔴 判别力：一个"任何失败都当死号"的实现会在后两行返回 False ——
       一次服务端抖动或网络抖动就能把**整池账号**标成失效，用户看到的是
       "莫名其妙全没了，只能清状态文件重来"。
    """
    m, _ = _chatai(tmp_path, responses=[resp])
    assert m._probe("u0@outlook.com") is expect


def test_chatai_create_mailbox_probes_past_dead_and_caches_the_verdict(tmp_path):
    """领用要跳过死号、两种记录都落盘，且**下次不再重探死号**。

    🔴 三层判别力：
      · 不探活的实现会直接发出 `u0`（死号）—— 白跑一遍注册；
      · 不落 `dead` 的实现下次还会把死号重探一遍（实测整池 ~1 分钟）；
      · 把死号也记进 `used` 的实现会让"重置"没法做（两个语义混了）。
    """
    lines = [f"u{i}@outlook.com----pw{i}----cid-{i}----rtok-{i}" for i in range(3)]
    m, _ = _chatai(tmp_path, lines=lines, responses=[_dead(), _dead(), _ok([])])
    assert m.create_mailbox(count=1) == ["u2@outlook.com"]

    bucket = json.loads(m._state_path().read_text(encoding="utf-8"))[str(m.accounts_path)]
    assert bucket["used"] == ["u2@outlook.com"]
    assert bucket["dead"] == ["u0@outlook.com", "u1@outlook.com"]

    # 第二个实例：死号不得被重探（预置队列为空 ⇒ 一旦重探就 AssertionError）
    m2 = ChataiMailbox(accounts=str(m.accounts_path), state=str(m._state_path()))
    fake2: Any = FakeChataiSession([])
    m2.session = fake2
    with pytest.raises(RuntimeError, match="只剩 0 个"):
        m2.create_mailbox(count=1)
    assert fake2.calls == [], "死号被重探了（negative cache 没生效）"


def test_chatai_short_pool_writes_dead_but_never_writes_used(tmp_path):
    """凑不齐时：`dead` 照落（有价值的负面信息），`used` **一个字都不写**。

    🔴 两条断言缺一不可（与 `ImapMailbox` 同一条原则）：
      · 只断言"抛了异常" ⇒ 一个先写 used 再抛的实现照样通过；
      · 只断言"文件没变" ⇒ 一个静默少给几个的实现照样通过。
    """
    lines = ["u0@outlook.com----pw0----cid-0----rtok-0", "u1@y.com----pw1----cid-1----rtok-1"]
    m, _ = _chatai(tmp_path, lines=lines, responses=[_dead(), _dead()])
    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox(count=1)

    msg = str(ei.value)
    assert "只剩 0 个" in msg
    assert "outlook.com" in msg and "y.com" in msg, "错误文案要带上池内域名分布"

    bucket = json.loads(m._state_path().read_text(encoding="utf-8"))[str(m.accounts_path)]
    assert bucket["used"] == []
    assert sorted(bucket["dead"]) == ["u0@outlook.com", "u1@y.com"]


def test_chatai_unreachable_page_aborts_without_marking_anyone_dead(tmp_path):
    """读信页整体不可达时必须**快速失败**，且不得把任何账号标死。

    🔴 判别力：没有这道闸门的实现会把整池账号逐个探一遍（每次都是"未知"），
       既慢，又会在日志里留下"整池都不可用"的假象 —— 而真相是网络不通。
    """
    lines = [f"u{i}@outlook.com----pw{i}----cid-{i}----rtok-{i}" for i in range(10)]
    m, fake = _chatai(tmp_path, lines=lines, always=requests.ConnectionError("boom"))
    with pytest.raises(RuntimeError, match="不可达"):
        m.create_mailbox(count=1)

    assert len(fake.calls) == mailbox._MAX_UNKNOWN, (
        f"应当探到上限（{mailbox._MAX_UNKNOWN}）就停，而不是把整池探完"
    )
    assert not m._state_path().exists(), "网络问题不该留下任何判定记录"


def test_chatai_never_leaks_credentials_into_last_error(tmp_path):
    """🔴 密码与 refreshToken 绝不能出现在 `last_error` 里。

    `last_error` 会被 `stage_register` 写进 `rec.error` → 台账 → stdout，
    而本仓库是公开的。
    """
    m, _ = _chatai(
        tmp_path,
        always=requests.ConnectionError("connect failed for u0@outlook.com rt=rtok-0 pw=pw0"),
    )
    m.wait_for_mail("u0@outlook.com", timeout=1, interval=0.05)

    assert "rtok-0" not in m.last_error
    assert "pw0" not in m.last_error
    assert "***" in m.last_error


def test_chatai_session_is_cached_and_rebuilt_only_on_envelope_errors(tmp_path):
    """会话必须缓存；401/403 **只在信封问题**上才重建重试。

    🔴 判别力：一个"任何 401/403 都重试"的实现会在第 3 段多调一次会话端点 ——
       前端注释写得很清楚："业务认证失败不能靠重建安全封包修复，重试反而
       会重复登录邮箱。"
    """
    # ① 连续两次拉信 ⇒ 会话只建一次
    m, fake = _chatai(tmp_path, responses=[_ok([]), _ok([])])
    m._fetch_mails("u0@outlook.com", limit=1)
    m._fetch_mails("u0@outlook.com", limit=1)
    assert fake.session_calls == 1, "会话被缓存了吗？"

    # ② 401 + SECURITY_ENVELOPE_INVALID ⇒ 重建会话并重试，最终成功
    env_err = FakeResponse(
        401, {"success": False, "code": "SECURITY_ENVELOPE_INVALID", "error": "请求签名无效"}
    )
    m2, fake2 = _chatai(tmp_path, responses=[env_err, _ok([])])
    assert m2._fetch_mails("u0@outlook.com", limit=1) == []
    assert fake2.session_calls == 2, "信封过期必须重建会话"

    # ③ 401 + 业务码 ⇒ **不重试**
    biz_err = FakeResponse(
        401, {"success": False, "code": "ACCOUNT_FORBIDDEN", "error": "账号被限制"}
    )
    m3, fake3 = _chatai(tmp_path, responses=[biz_err])
    with pytest.raises(RuntimeError, match="ACCOUNT_FORBIDDEN"):
        m3._fetch_mails("u0@outlook.com", limit=1)
    assert fake3.session_calls == 1, "业务 401 不该重建会话"


def test_chatai_domain_filter_never_crosses_domains(tmp_path):
    """`domain` 过滤必须生效，且**不得跨域名凑数**（与 IMAP 同一条）。

    判别力：忽略 domain 的实现会返回 `u0@outlook.com`，而调用方以为自己
      拿到的是 `@y.com` —— 域名门是按域名判的，拿错域名等于白跑。
    """
    lines = ["u0@outlook.com----pw0----cid-0----rtok-0", "u1@y.com----pw1----cid-1----rtok-1"]
    m, _ = _chatai(tmp_path, lines=lines, responses=[_ok([])])
    assert m.create_mailbox(domain="y.com", count=1) == ["u1@y.com"]

    with pytest.raises(RuntimeError, match=r"限制了域名 @y\.com"):
        m.create_mailbox(domain="Y.COM", count=1)


def test_chatai_missing_accounts_file_raises_with_the_path(tmp_path):
    """账号文件不存在时，报错必须**带上路径与配置项名** —— 否则用户不知道改哪。"""
    missing = tmp_path / "nope.txt"
    m = ChataiMailbox(accounts=str(missing), state=str(tmp_path / "s.json"))
    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox(count=1)
    assert str(missing) in str(ei.value)
    assert "IR_CHATAI_ACCOUNTS" in str(ei.value)


def test_chatai_does_not_route_through_a_proxy_by_default(tmp_path, monkeypatch):
    """读信页是**国内站**，默认必须直连（与 `TempMailClient` 同一约定）。

    🔴 `trust_env=False` 就是判据：本机若设了 `HTTP_PROXY`，requests 会默认
       走它 —— 那样读信绕道出口节点，慢且无任何收益。
    """
    monkeypatch.delenv("IR_PROXY_MAIL", raising=False)
    accounts = _accounts_file(tmp_path, ["u0@outlook.com----pw0----cid-0----rtok-0"])
    m = ChataiMailbox(accounts=str(accounts), state=str(tmp_path / "s.json"))
    assert m.session.trust_env is False


def test_chatai_state_path_honors_env_override(tmp_path, monkeypatch):
    """`IR_CHATAI_STATE` 必须生效，且是**调用时**读（自检脚本靠它隔离）。"""
    target = tmp_path / "custom.json"
    monkeypatch.setenv("IR_CHATAI_STATE", str(target))
    assert mailbox.chatai_state_path() == target

    monkeypatch.delenv("IR_CHATAI_STATE", raising=False)
    default = mailbox.chatai_state_path()
    assert default.name == "chatai_used.json"
    assert ".workbuddy-ai" in default.parts and "state" in default.parts
    assert "Downloads" not in default.parts and "Desktop" not in default.parts, (
        "状态文件不得落在用户的个人目录里"
    )


def test_parse_chatai_accounts_keeps_order_and_skips_bad_lines():
    """4 段格式解析：保持文件顺序、跳过残缺行、两侧空白 strip。

    ⚠ 返回 **list** 而不是 dict —— `create_mailbox` 靠"按文件顺序逐个试"，
      去重交给状态文件（见 `ChataiMailbox.create_mailbox`）。
    """
    text = (
        "# 注释行\n"
        "\n"
        "a@x.com----pw1----cid-1----rt1\n"
        "段数不够----pw2----cid-2\n"
        "----pw3----cid-3----rt3\n"
        "b@x.com----pw4----cid-4----\n"
        "  c@x.com  ----  pw5  ----  cid-5  ----  rt5  \n"
    )
    got = parse_chatai_accounts(text)
    assert [a["email"] for a in got] == ["a@x.com", "c@x.com"]
    assert got[1] == {
        "email": "c@x.com",
        "password": "pw5",
        "clientId": "cid-5",
        "refreshToken": "rt5",
    }


def test_chatai_iso_ms_is_milliseconds_and_tolerates_garbage():
    """`date` 是 ISO-8601（`...T18:09:37Z`）⇒ 必须转成**毫秒**。

    🔴 第 3 条断言钉的是"无时区按 UTC 算"：用 `dt.timestamp()` 直接算天真
       datetime 会按**本机时区**（GMT+8）解释，差值正好 8 小时 ——
       `arrival_delay_ms` 于是变成 ±8 小时的怪数，而报告里只是"耗时有点怪"。
    """
    expect = int(datetime(2026, 9, 23, 18, 9, 37, tzinfo=UTC).timestamp() * 1000)
    assert mailbox._iso_ms("2026-09-23T18:09:37Z") == expect
    assert mailbox._iso_ms("2026-09-23T18:09:37+00:00") == expect
    assert mailbox._iso_ms("2026-09-23T18:09:37") == expect, "无时区必须按 UTC 算"
    assert mailbox._iso_ms("") == 0
    assert mailbox._iso_ms(None) == 0
    assert mailbox._iso_ms("不是时间") == 0


def test_validate_chatai_branch_requires_accounts_file(monkeypatch, tmp_path):
    """chatai 分支：**只**看账号文件，不再要求 `IR_WORKER_*` / `IR_IMAP_*`。

    🔴 判别力：若 `validate()` 仍无条件检查 `IR_WORKER_*`，chatai 模式下会
      报"缺 IR_WORKER_BASE"，把人引向一个**跟当前源毫无关系**的配置项。
    """
    monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "WORKER_BASE", "")
    monkeypatch.setattr(config, "WORKER_DOMAIN", "")
    monkeypatch.setattr(config, "IMAP_CREDENTIALS", "")

    assert _validate_with(monkeypatch, "chatai", CHATAI_ACCOUNTS="") == ["IR_CHATAI_ACCOUNTS"]

    ghost = tmp_path / "ghost.txt"
    missing = _validate_with(monkeypatch, "chatai", CHATAI_ACCOUNTS=str(ghost))
    assert len(missing) == 1 and "文件不存在" in missing[0] and str(ghost) in missing[0]

    real = tmp_path / "real.txt"
    real.write_text("u0@outlook.com----pw----cid----rtok\n", encoding="utf-8")
    assert _validate_with(monkeypatch, "chatai", CHATAI_ACCOUNTS=str(real)) == []


# ══════════════════════════════════════════════════════════════════
# 5. RemailMailbox —— 按订单买邮箱 / 按 token 取件
# ══════════════════════════════════════════════════════════════════


class FakeResp:
    """`requests.Response` 的最小替身。`body=None` 表示"不是 JSON"。"""

    def __init__(self, status_code, body, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeRemailSession:
    """`RemailMailbox` 唯一的网络出口（`self.session`）。

    与 chatai 的测试同构：换掉 `session` 之后，其余全是**真跑**
    （下单参数拼装、token 记账、轮询、正文提取、`&amp;` 反转义）。
    """

    def __init__(self, *, orders=None, pickups=None, messages=None, remote_orders=None):
        self.orders = (
            orders
            if orders is not None
            else [
                {
                    "orderNo": "OR1",
                    "deliveryEmail": "u@outlook.com",
                    "serviceToken": "st_1",
                    "status": "active",
                }
            ]
        )
        # `remote_orders` = **平台侧**的订单（可能是别的进程 / 更早的运行下的单），
        # 形如详情接口的返回（含 `serviceToken`）。
        # 🔴 列表接口会**抹掉** `serviceToken` —— 这是照实现实行为造的
        #    （2026-10-04 实测：`GET /v1/open/orders` 不含 token，
        #      只有 `GET /v1/open/orders/{orderNo}` 才给）。
        #    所以一个“直接信列表”的实现会在下面几条用例里当场红。
        self.remote_orders = remote_orders if remote_orders is not None else []
        self.pickups = pickups if pickups is not None else [{"items": []}]
        self.messages = messages or {}
        self.calls: list[tuple] = []
        self.headers: dict = {}
        self.trust_env = True
        self._order_i = 0
        self._pickup_i = 0

    def post(self, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append(("POST", url, params, json, headers))
        o = self.orders[min(self._order_i, len(self.orders) - 1)]
        self._order_i += 1
        return FakeResp(201, o)

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.calls.append(("GET", url, params, None, headers))
        if "/messages/" in url:
            return FakeResp(200, self.messages.get(params.get("email"), {}))
        if url.rstrip("/").endswith("/v1/open/orders"):
            term = str(params.get("search") or "")
            matched = [
                o
                for o in self.remote_orders
                if not term or term in str(o.get("deliveryEmail") or "")
            ]
            # 列表**不含** token（见上面 `remote_orders` 的说明）
            items = [{k: v for k, v in o.items() if k != "serviceToken"} for o in matched]
            return FakeResp(200, {"items": items, "total": len(items), "hasNext": False})
        if "/v1/open/orders/" in url:
            want = url.rsplit("/", 1)[-1]
            for o in self.remote_orders:
                if o.get("orderNo") == want:
                    return FakeResp(200, o)
            return FakeResp(404, {"message": "order not found"})
        b = self.pickups[min(self._pickup_i, len(self.pickups) - 1)]
        self._pickup_i += 1
        return FakeResp(200, b)


def _remail(monkeypatch, fake: Any, **kw) -> RemailMailbox:
    monkeypatch.setattr(config, "REMAIL_API_KEY", "rk-test")
    m = RemailMailbox(**kw)
    m.session = fake
    return m


def test_remail_create_mailbox_places_an_order_and_remembers_the_token(monkeypatch):
    """下单参数必须带对项目 / 后缀 / 供给策略，并把 token 记在**地址**上。

    🔴 判别力：`private_first` 与 `projectId` 是取件能不能命中 OpenXLab 邮件的
      **决定性参数**（2026-09-24 实测：public_only + 无规则项目 ⇒ items 恒空）。
    """
    fake = FakeRemailSession()
    m = _remail(monkeypatch, fake)

    assert m.create_mailbox() == ["u@outlook.com"]
    assert m._tokens["u@outlook.com"] == "st_1"

    _, url, params, body, headers = fake.calls[0]
    assert url.endswith("/v1/open/orders")
    assert params == {"serviceMode": "purchase", "supply": "private_first"}
    assert body == {"projectId": 170, "emailSuffix": "outlook.com"}
    assert headers["Authorization"] == "Bearer rk-test"
    assert headers["Idempotency-Key"], "下单必须带幂等键"


def test_remail_domain_argument_overrides_the_default_suffix(monkeypatch):
    fake = FakeRemailSession()
    m = _remail(monkeypatch, fake)
    m.create_mailbox(domain="hotmail.com")
    assert fake.calls[0][3]["emailSuffix"] == "hotmail.com"


def test_remail_wait_for_mail_reads_full_body_and_unescapes_the_link(monkeypatch):
    """`bodyPreview` 只是摘要 ⇒ 激活链接要单独拉正文，且必须反转义 `&amp;`。

    🔴 判别力：不拉正文 = 找不到链接；不 `html.unescape` = query 被截断
      （2026-10-03 实测：token 对、sign 丢，激活报缺参）。
    """
    item = {
        "id": 42,
        "sender": "OpenXLab <no-reply@dm.openxlab.org.cn>",
        "recipient": "u@outlook.com",
        "subject": "【OpenXLab】注册激活",
        "receivedAt": "2026-10-03T12:25:01+08:00",
        "bodyPreview": "请在 2 小时内激活账户。",
    }
    html_body = (
        '<a href="https://sso.openxlab.org.cn/active'
        '?token=abc&amp;sign=def">激活 Activate &gt;&gt;</a>'
    )
    fake = FakeRemailSession(
        pickups=[{"items": []}, {"items": [item]}], messages={"u@outlook.com": {"body": html_body}}
    )
    m = _remail(monkeypatch, fake)
    m._tokens["u@outlook.com"] = "st_1"

    mail = m.wait_for_mail("u@outlook.com", timeout=5, interval=0.01)
    assert mail is not None
    assert m.last_polls == 2, "第一轮空、第二轮命中"
    assert mail.received_at > 1e11, "必须是毫秒（stage_register 按 >1e11 判单位）"
    assert mail.find_link("active") == "https://sso.openxlab.org.cn/active?token=abc&sign=def"
    assert m.wait_for_activation_link("u@outlook.com", timeout=5, interval=0.01)


def test_remail_pickup_carries_the_per_order_token(monkeypatch):
    """取件必须带**该地址的** serviceToken（取件接口 `security: []`，不带 API Key）。"""
    fake = FakeRemailSession(pickups=[{"items": []}])
    m = _remail(monkeypatch, fake)
    m._tokens["u@outlook.com"] = "st_secret"
    m.wait_for_mail("u@outlook.com", timeout=1, interval=0.01)
    gets = [c for c in fake.calls if c[0] == "GET"]
    assert gets, "至少打了一次取件"
    assert gets[0][2]["token"] == "st_secret"
    assert gets[0][2]["email"] == "u@outlook.com"


def test_remail_create_mailbox_without_key_raises_with_the_fix(monkeypatch):
    monkeypatch.setattr(config, "REMAIL_API_KEY", "")
    m = RemailMailbox()
    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox()
    assert "IR_REMAIL_API_KEY" in str(ei.value)


def test_remail_failed_order_raises_instead_of_silently_returning_fewer(monkeypatch):
    """下单失败要**抛**，不能少给地址 —— 否则真实原因被"邮箱建不出来"盖住。"""

    class BadSession(FakeRemailSession):
        def post(self, url, headers=None, params=None, json=None, timeout=None):
            return FakeResp(400, {"msgCode": "insufficient_balance"}, text="余额不足")

    m = _remail(monkeypatch, BadSession())
    with pytest.raises(RuntimeError) as ei:
        m.create_mailbox()
    assert "HTTP 400" in str(ei.value)


def test_remail_unknown_address_sets_last_error_not_raises(monkeypatch):
    m = _remail(monkeypatch, FakeRemailSession())
    assert m.wait_for_mail("nobody@x.com", timeout=1, interval=0.01) is None
    assert "订单凭据" in m.last_error


# ── 5b. 按地址回捞凭据（2026-10-04）──────────────────────────────────
# 为什么必须有这条通路：`_tokens` 只在 `create_mailbox` 时进**内存**、**不落盘**
# ⇒ 任何新进程（`recover_activation.py` 就是）都必然说“没有订单凭据”，
#   remail 账号的激活就永远救不回来。
# 平台侧能查回来（实测确认）：
#     GET /v1/open/orders?search=<完整地址>   → 列表（**不含** token）
#     GET /v1/open/orders/{orderNo}           → 详情（**含** serviceToken）
def test_remail_recovers_credentials_for_an_earlier_process_order(monkeypatch):
    """🔴 本进程没下过单，也要能按地址把 `serviceToken` 找回来并取件。"""
    fake = FakeRemailSession(
        remote_orders=[
            {
                "orderNo": "OR9",
                "deliveryEmail": "old@outlook.com",
                "serviceToken": "st_9",
                "status": "active",
            }
        ],
        pickups=[
            {
                "items": [
                    {
                        "id": 1,
                        "sender": "no-reply@openxlab.org.cn",
                        "subject": "activate",
                        "bodyPreview": "https://sso.openxlab.org.cn/a?t=1",
                    }
                ]
            }
        ],
    )
    m = _remail(monkeypatch, fake)
    assert m._tokens == {}, "本进程不该有任何凭据"

    mail = m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01)
    assert mail is not None, m.last_error
    assert m._tokens["old@outlook.com"] == "st_9"
    assert m._orders["old@outlook.com"] == "OR9"

    # 取件必须带上**回捞到的** token（不是空串、也不是别人的）
    pickup = [c for c in fake.calls if c[0] == "GET" and str(c[1]).endswith("/v1/pickup")]
    assert pickup, "至少打了一次取件"
    assert pickup[0][2]["token"] == "st_9"


def test_remail_recovered_credentials_are_cached(monkeypatch):
    """回捞一次就缓存 —— 不然每轮轮询都会再打一次列表接口。"""
    fake = FakeRemailSession(
        remote_orders=[
            {
                "orderNo": "OR9",
                "deliveryEmail": "old@outlook.com",
                "serviceToken": "st_9",
                "status": "active",
            }
        ],
        pickups=[
            {
                "items": [
                    {
                        "id": 1,
                        "sender": "no-reply@openxlab.org.cn",
                        "subject": "x",
                        "bodyPreview": "https://x/y",
                    }
                ]
            }
        ],
    )
    m = _remail(monkeypatch, fake)
    m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01)
    m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01)
    lists = [
        c for c in fake.calls if c[0] == "GET" and str(c[1]).rstrip("/").endswith("/v1/open/orders")
    ]
    assert len(lists) == 1, f"回捞了 {len(lists)} 次，应当只 1 次"


def test_remail_lookup_failure_degrades_to_the_plain_error(monkeypatch):
    """回捞接口本身出错时**不能**抛 —— 退化成原来的“没有订单凭据”。

    收信流程绝不能因为一个附加的查询而崩：主因（邮件没到）会被这条路盖住。
    """

    class BrokenList(FakeRemailSession):
        def get(self, url, headers=None, params=None, timeout=None):
            if str(url).rstrip("/").endswith("/v1/open/orders"):
                self.calls.append(("GET", url, params, None, headers))
                return FakeResp(500, {"message": "boom"}, text="boom")
            return super().get(url, headers=headers, params=params, timeout=timeout)

    m = _remail(monkeypatch, BrokenList())
    assert m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01) is None
    assert "订单凭据" in m.last_error


def test_remail_restore_failure_says_why(monkeypatch):
    """🔴 回捞失败要能**区分**“平台侧没这个单”与“单在、但平台没给 token”。

    两者处置完全不同：前者是账号 / Key 不对（换 Key，或承认救不回来），
    后者是订单被退款 / 清理（等多少天都没用）。混成一句“查不到”会把人引去
    翻 API Key —— 而真实原因在另一边（2026-10-04 实测两种同时出现：
    一批“不在本 Key 名下”的地址是前者，一个“订单已退款”的是后者）。

    ⚠ 这里刻意**不写真实域名**：它是 `.env` 里的基础设施标识，进公开仓库
      等于贴出去（泄漏闸门会拦 —— 本用例就因此被拦过一次）。
    """
    fake = FakeRemailSession(
        remote_orders=[
            {
                "orderNo": "ORZ",
                "deliveryEmail": "refunded@x.com",
                "status": "refunded",
            }
        ],  # 单在，但**没有** serviceToken
    )
    m = _remail(monkeypatch, fake)

    assert m.wait_for_mail("nobody@x.com", timeout=1, interval=0.01) is None
    assert "不是本 API Key 下的单" in m.last_error, m.last_error

    assert m.wait_for_mail("refunded@x.com", timeout=1, interval=0.01) is None
    assert "refunded" in m.last_error, m.last_error
    assert "serviceToken" in m.last_error, m.last_error
    assert "不是本 API Key 下的单" not in m.last_error, (
        "单已经找到了，就不该再说“不是本 Key 下的单” —— 那是另一个原因"
    )


def test_remail_lookup_carries_the_api_key(monkeypatch):
    """🔴 订单查询接口**要 API Key** —— 与取件（只认 `serviceToken`）不是一套鉴权。

    判别力：`RemailMailbox` 给 `self.session` 设的默认头里**只有** `Accept`。
    鉴权头原本只在 `_place_order` 里逐次传，回捞那条路漏了 ⇒ 查询 401，
    而 401 在调用方眼里就是“查不到订单”，把“Key 不对”伪装成“这单不是你的”。
    实测踩到过（2026-10-04：直接探针查得到，走 `_lookup_order` 却“未命中”）。
    """
    fake = FakeRemailSession(
        remote_orders=[
            {"orderNo": "OR9", "deliveryEmail": "old@outlook.com", "serviceToken": "st_9"}
        ]
    )
    m = _remail(monkeypatch, fake)
    m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01)

    # 列表 + 详情**两处**都要带（本次就是详情那处漏了：只修列表不够）
    orders = [c for c in fake.calls if c[0] == "GET" and "/v1/open/orders" in str(c[1])]
    assert len(orders) == 2, f"应当打了列表 + 详情各一次：{orders}"
    for c in orders:
        assert (c[4] or {}).get("Authorization") == "Bearer rk-test", (
            f"{c[1]} 没带 Authorization：{c[4]!r}"
        )


def test_remail_detail_query_failure_is_not_reported_as_a_missing_token(monkeypatch):
    """🔴 详情查询**失败** ⇒ 不能说成“平台没给 token / 已退款”。

    实测踩到过：`status=active`、明明有 token 的订单，被报成
    “多半已退款 / 清理” —— 根因只是详情那次调用漏了鉴权头。
    **错误结论比没有结论更坏**：它会让人去查订单为什么被退款。
    """

    class BrokenDetail(FakeRemailSession):
        def get(self, url, headers=None, params=None, timeout=None):
            if "/v1/open/orders/" in str(url):
                self.calls.append(("GET", url, params, None, headers))
                return FakeResp(500, {"message": "boom"}, text="boom")
            return super().get(url, headers=headers, params=params, timeout=timeout)

    fake = BrokenDetail(
        remote_orders=[
            {"orderNo": "OR9", "deliveryEmail": "old@outlook.com", "serviceToken": "st_9"}
        ]
    )
    m = _remail(monkeypatch, fake)
    assert m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01) is None
    assert "详情查询失败" in m.last_error, m.last_error
    assert "已退款" not in m.last_error, m.last_error


def test_remail_lookup_auth_failure_is_reported_as_a_key_problem(monkeypatch):
    """查询被 401 拒 ⇒ 报“Key 不对”，**不能**说成“这单不是你的”。"""

    class Unauthorized(FakeRemailSession):
        def get(self, url, headers=None, params=None, timeout=None):
            if str(url).rstrip("/").endswith("/v1/open/orders"):
                self.calls.append(("GET", url, params, None, headers))
                return FakeResp(401, {"message": "invalid api key"}, text="invalid api key")
            return super().get(url, headers=headers, params=params, timeout=timeout)

    m = _remail(monkeypatch, Unauthorized())
    assert m.wait_for_mail("old@outlook.com", timeout=1, interval=0.01) is None
    assert "API Key" in m.last_error, m.last_error
    assert "不是本 API Key 下的单" not in m.last_error, m.last_error


def test_validate_remail_branch_requires_api_key(monkeypatch):
    """remail 分支：**只**看 API Key，不再要求 Worker / IMAP / chatai 的配置。"""
    monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "WORKER_BASE", "")
    monkeypatch.setattr(config, "WORKER_DOMAIN", "")
    monkeypatch.setattr(config, "IMAP_CREDENTIALS", "")
    monkeypatch.setattr(config, "CHATAI_ACCOUNTS", "")
    assert _validate_with(monkeypatch, "remail", REMAIL_API_KEY="") == ["IR_REMAIL_API_KEY"]
    assert _validate_with(monkeypatch, "remail", REMAIL_API_KEY="rk-x") == []
