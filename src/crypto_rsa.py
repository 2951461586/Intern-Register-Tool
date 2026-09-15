"""RSA 加密 —— 复刻 OpenXLab SSO 前端的密码加密逻辑。

前端实现（来自 sso.openxlab.org.cn/static/js/main.59963db7.chunk.js）：

    a.setPublicKey(pubKey);
    a.encrypt(email + "||" + password + Math.floor(Date.now() / 1e3))

即：
  1. 明文 = f"{identity}||{password}{unix_seconds}"
  2. RSA/ECB/PKCS1Padding（jsencrypt 默认）加密
  3. 密文 base64 编码

踩坑记录：
  - 只加密 password（不带 email 前缀和时间戳）→ 服务端报 A0216「用户密码解密失败」
  - 时间戳必须是秒级整数，与客户端本地时间一致
  - 注册/登录/改密三种场景的 identity 分别是 email / account / email
"""

import base64
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config

_PUBLIC_KEY = serialization.load_der_public_key(
    base64.b64decode(config.SSO_PUBKEY_B64)
)


def encrypt_password(identity: str, password: str, timestamp: int | None = None) -> str:
    """按前端逻辑加密密码。

    Args:
        identity: 注册/登录时使用的账号标识（email 或 account）。
        password: 明文密码。
        timestamp: 秒级 Unix 时间戳，默认取当前时间。

    Returns:
        base64 编码的 RSA 密文。
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    plain = f"{identity}||{password}{ts}"
    cipher = _PUBLIC_KEY.encrypt(plain.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def selftest() -> bool:
    """自检：密文长度应为 RSA-1024 单块（128 字节 → base64 172 字符）。"""
    ct = encrypt_password("a@b.com", "Test123!")
    return 168 <= len(ct) <= 176
