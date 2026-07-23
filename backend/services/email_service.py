"""
邮件通知服务

用 aiosmtplib 异步发送 HTML 邮件,SMTP 未配置时仅记录日志(不报错)。
aiosmtplib 未安装时降级为 no-op(仅日志),不阻塞主流程。

环境变量(通过 Settings 或直接传入):
- SMTP_HOST: SMTP 服务器地址
- SMTP_PORT: SMTP 端口(默认 587)
- SMTP_USER: SMTP 用户名
- SMTP_PASSWORD: SMTP 密码
- SMTP_FROM_EMAIL: 发件人邮箱
- SMTP_USE_TLS: 是否启用 STARTTLS(默认 True)
"""

from __future__ import annotations

import logging
import os
from email.message import EmailMessage
from typing import Optional

# aiosmtplib 为可选依赖,未安装时降级为 no-op(仅日志)
try:
    import aiosmtplib  # type: ignore

    AIOSMTP_AVAILABLE = True
except ImportError:
    aiosmtplib = None  # type: ignore[assignment]
    AIOSMTP_AVAILABLE = False

logger = logging.getLogger(__name__)

# 默认 HTML 邮件模板(通知类邮件)
_NOTIFICATION_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5;">
  <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);">
    <h2 style="color: #1a1a1a; margin: 0 0 16px 0;">{title}</h2>
    <div style="color: #4a4a4a; font-size: 14px; line-height: 1.6;">
      {content}
    </div>
    {link_html}
    <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 24px 0;">
    <p style="color: #999; font-size: 12px; margin: 0;">
      此邮件由 AgentValue-AI 系统自动发送,请勿直接回复。
    </p>
  </div>
</body>
</html>
"""


class EmailService:
    """异步邮件发送服务

    SMTP 未配置(参数全空)或 aiosmtplib 未安装时,所有发送操作降级为仅记录日志,
    不抛异常,确保调用方(如通知服务)不受邮件通道故障影响。
    """

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        from_email: Optional[str] = None,
        use_tls: bool = True,
    ):
        # 优先用传入参数,兜底读环境变量
        self.smtp_host = smtp_host or os.environ.get("SMTP_HOST")
        self.smtp_port = int(smtp_port or os.environ.get("SMTP_PORT", 587))
        self.smtp_user = smtp_user or os.environ.get("SMTP_USER")
        self.smtp_password = smtp_password or os.environ.get("SMTP_PASSWORD")
        self.from_email = from_email or os.environ.get("SMTP_FROM_EMAIL")
        self.use_tls = use_tls

        # 判断是否已配置:host + user + from_email 均非空才视为可用
        self._configured = bool(self.smtp_host and self.smtp_user and self.from_email)

        if not AIOSMTP_AVAILABLE:
            logger.warning("aiosmtplib 未安装,邮件发送将降级为仅日志模式")
        if not self._configured:
            logger.info("SMTP 未配置(host/user/from_email),邮件发送将降级为仅日志模式")

    @property
    def is_available(self) -> bool:
        """邮件服务是否可用(已配置且 aiosmtplib 已安装)"""
        return self._configured and AIOSMTP_AVAILABLE

    async def send_email(self, to_email: str, subject: str, html_body: str) -> bool:
        """发送 HTML 邮件

        Args:
            to_email: 收件人邮箱。
            subject: 邮件主题。
            html_body: HTML 正文。

        Returns:
            True 表示发送成功,False 表示发送失败或降级(仅日志)。
        """
        if not self.is_available:
            logger.info("邮件发送降级(仅日志) to=%s subject=%s", to_email, subject)
            logger.debug("邮件正文: %s", html_body[:500])
            return False

        message = EmailMessage()
        message["From"] = self.from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content("此邮件需要 HTML 支持,请使用支持 HTML 的客户端查看。")
        message.add_alternative(html_body, subtype="html")

        try:
            await aiosmtplib.send(
                message,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=self.use_tls,
            )
            logger.info("邮件发送成功 to=%s subject=%s", to_email, subject)
            return True
        except Exception:
            logger.exception("邮件发送失败 to=%s subject=%s", to_email, subject)
            return False

    async def send_notification_email(
        self,
        to_email: str,
        title: str,
        content: str,
        link: Optional[str] = None,
    ) -> bool:
        """发送通知类邮件(使用内置 HTML 模板)

        Args:
            to_email: 收件人邮箱。
            title: 通知标题。
            content: 通知正文(纯文本,会嵌入 HTML 模板)。
            link: 点击跳转 URL(可空,非空时模板中渲染按钮)。

        Returns:
            True 表示发送成功,False 表示发送失败或降级。
        """
        link_html = ""
        if link:
            link_html = (
                f'<a href="{link}" style="display: inline-block; '
                f"margin-top: 16px; padding: 10px 24px; "
                f"background-color: #2563eb; color: #ffffff; "
                f"text-decoration: none; border-radius: 6px; "
                f'font-size: 14px;">查看详情</a>'
            )

        html_body = _NOTIFICATION_TEMPLATE.format(
            title=title,
            content=content or "",
            link_html=link_html,
        )

        return await self.send_email(to_email, title, html_body)
