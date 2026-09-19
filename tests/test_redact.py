"""`src/redact.py` 的行为测试 —— 脱敏是**输出边界的最后一道闸门**。

为什么值得单独钉住
------------------
脱敏坏掉的方式是"静默泄漏"：不报错、不崩溃，只是账密落进了日志。
而日志经常被贴进 issue / 对话里 —— 发现时已经晚了。
所以边界条件（空串、无 userinfo、多 @、只有 scheme）必须逐条验。

跑法：
    pytest tests/test_redact.py -v
"""

import pytest

from src import redact


# ── redact_url ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    # 标准形态：userinfo 整段换成 ***，host:port 保留
    ("http://user:pass@203.0.113.30:8080", "http://***@203.0.113.30:8080"),
    ("socks5://alice:s3cret@203.0.113.7:1080", "socks5://***@203.0.113.7:1080"),
    # 没有 userinfo -> 原样返回
    ("http://127.0.0.1:7901", "http://127.0.0.1:7901"),
    ("http://203.0.113.30:8080", "http://203.0.113.30:8080"),
    # 没有 :// -> 原样返回（不是 URL，不该瞎猜）
    ("127.0.0.1:7901", "127.0.0.1:7901"),
    ("这不是一个代理串", "这不是一个代理串"),
    # 空 / None
    ("", ""),
    (None, ""),
])
def test_redact_url_forms(raw, expected):
    assert redact.redact_url(raw) == expected


def test_redact_url_only_user_no_password():
    """只有用户名（没有 `:pass`）也要抹掉。"""
    assert redact.redact_url("http://user@203.0.113.30:8080") \
        == "http://***@203.0.113.30:8080"


def test_redact_url_password_containing_at():
    """密码里含 `@` —— 必须按**最后一个** `@` 切分，否则 host 会被切坏。"""
    assert redact.redact_url("http://user:p@ss@203.0.113.30:8080") \
        == "http://***@203.0.113.30:8080"


def test_redact_url_never_leaks_credentials():
    """兜底断言：任何形态下原密码都不该出现在输出里。"""
    secret = "sup3r-s3cret-pw"
    out = redact.redact_url(f"http://bob:{secret}@203.0.113.30:8080")
    assert secret not in out
    assert "bob" not in out, "用户名也不该留（可做关联）"


# ── redact ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    (None, ""),
    ("sk-abcdef123456", "<15 chars>"),
    ("a", "<1 chars>"),
])
def test_redact_hides_everything_by_default(raw, expected):
    assert redact.redact(raw) == expected


def test_redact_default_keeps_no_prefix():
    """🔴 默认**连前缀都不打** —— "前 6 位"也足以在别处做关联。"""
    out = redact.redact("sk-abcdef123456")
    assert "sk-" not in out
    assert "abcdef" not in out


def test_redact_keep_is_opt_in_for_local_debug():
    """`keep` 只在本地排查时显式传，且仍要标出总长度。"""
    out = redact.redact("sk-abcdef123456", keep=3)
    assert out.startswith("sk-")
    assert "<15 chars>" in out


def test_redact_keep_larger_than_input_falls_back_to_full_mask():
    """`keep` 比串还长时不能把原值整个打出来。"""
    assert redact.redact("short", keep=99) == "<5 chars>"
