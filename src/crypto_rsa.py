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
  - 🔴 PKCS#1 v1.5 的**加密**填充是**随机**的（签名才是确定性的）⇒ 同一组输入
    两次加密得到的密文**不同**。所以任何"比对两次密文"的用法都在比噪声 ——
    探针一律比**服务端响应**，不比密文。这条不变式由
    `tests/test_crypto_rsa.py` 钉住（该文件同时接管了原先那个无人调用的
    `selftest()` 所断言的长度不变式）。
"""

import base64
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from common import config

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
