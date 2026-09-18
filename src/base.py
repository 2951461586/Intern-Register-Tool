"""临时邮箱服务提供者抽象基类。

各提供者（CF Worker / YYDS Mail 等）实现同一套接口，
pipeline 通过 `create_mail_client()` 工厂按配置选择具体实现。
"""

from abc import ABC, abstractmethod


class MailProvider(ABC):
    """临时邮箱提供者抽象基类。

    子类需实现：
      - create_mailbox() -> str       创建一个新邮箱，返回完整地址
      - wait_otp(...) -> str          等待并返回验证码（无验证码场景可返回 ""）
    可选实现：
      - wait_link(...) -> str | None  等待并返回邮件中的激活链接
      - list_domains() -> list[dict]
      - close()                       释放资源
    """

    name: str = ""
    display_name: str = ""

    @abstractmethod
    def create_mailbox(self) -> str:
        """创建一个新邮箱，返回完整邮箱地址（如 a1b2c3@example.com）。"""

    def wait_otp(self, timeout: int = 120, poll_interval: int = 3) -> str:
        """等待并返回邮件中的验证码；超时或无法提取时返回 ""。"""
        return ""

    def wait_link(self, timeout: int = 120, poll_interval: int = 3,
                  keywords: tuple = ("active", "activat", "verif", "confirm")) -> str | None:
        """等待并返回邮件中的激活链接；超时返回 None。"""
        return None

    def list_domains(self) -> list[dict]:
        return []

    def close(self) -> None:
        pass
