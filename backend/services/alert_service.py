"""告警通知服务

提供告警的创建、通知、查询与状态管理:
- create_alert: 创建告警
- send_alert: 通过配置的通道发送告警通知 (飞书群机器人 / 邮件 / Webhook)
- send_feishu_alert: 飞书群机器人通知 (交互式卡片消息)
- send_email_alert: 邮件通知 (HTML 邮件模板)
- send_webhook_alert: 出站 Webhook 通知 (WS-3: 订阅注册表 + HMAC 签名 + 退避重试 + 死信)
- list_alerts: 告警列表 (支持 severity / source 过滤)
- acknowledge_alert: 确认告警
- resolve_alert: 解决告警

告警级别: critical / warning / info
告警状态: active → acknowledged → resolved

通知通道通过环境变量配置:
- ALERT_FEISHU_WEBHOOK: 飞书群机器人 webhook URL
- ALERT_WEBHOOK_URL: 通用 Webhook URL
- ALERT_EMAIL_TO: 告警邮件收件人 (逗号分隔)
- SMTP_*: 邮件 SMTP 配置 (复用 EmailService)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert_model import Alert

logger = logging.getLogger(__name__)

# 告警级别
ALERT_SEVERITIES = {"critical", "warning", "info"}
# 告警状态
ALERT_STATUS_ACTIVE = "active"
ALERT_STATUS_ACKNOWLEDGED = "acknowledged"
ALERT_STATUS_RESOLVED = "resolved"
ALERT_STATUSES = {ALERT_STATUS_ACTIVE, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_RESOLVED}

# 级别对应的飞书卡片颜色 (danger=红 / warning=橙 / blue=蓝)
SEVERITY_COLORS = {
    "critical": "red",
    "warning": "orange",
    "info": "blue",
}
# 级别对应的中文标签
SEVERITY_LABELS = {
    "critical": "严重",
    "warning": "警告",
    "info": "信息",
}


class AlertService:
    """告警通知服务 (数据库实现)"""

    # ALERT_WEBHOOK_URL 环境变量托管订阅的固定名称 (租户内唯一标识)
    ENV_SUBSCRIPTION_NAME = "环境变量告警 Webhook"

    def __init__(self, session: AsyncSession):
        self.session = session
        # 通知通道配置 (从环境变量读取)
        self.feishu_webhook = os.environ.get("ALERT_FEISHU_WEBHOOK")
        self.webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
        self.email_to = os.environ.get("ALERT_EMAIL_TO")

    # ===================== 告警 CRUD =====================

    async def create_alert(
        self,
        severity: str,
        title: str,
        message: str,
        source: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        *,
        tenant_id: str = "default",
    ) -> Alert:
        """创建告警

        Args:
            severity: 告警级别 (critical / warning / info)。
            title: 告警标题。
            message: 告警消息内容。
            source: 告警来源 (如 system / agent_error / quota / sensitive_word)。
            metadata: 附加元数据 (如 agent_id / error_stack / threshold)。
            tenant_id: 租户 ID。

        Returns:
            创建的 Alert 对象。
        """
        if severity not in ALERT_SEVERITIES:
            raise ValueError(f"无效的告警级别: {severity}, 可选: {ALERT_SEVERITIES}")
        if not title or not title.strip():
            raise ValueError("告警标题不能为空")
        if not message or not message.strip():
            raise ValueError("告警消息不能为空")

        alert = Alert(
            tenant_id=tenant_id,
            severity=severity,
            title=title.strip(),
            message=message.strip(),
            source=source,
            status=ALERT_STATUS_ACTIVE,
            metadata_=metadata or {},
        )
        self.session.add(alert)
        await self.session.flush()
        logger.info(
            "创建告警 id=%s severity=%s source=%s title=%s",
            alert.id,
            severity,
            source,
            title,
        )
        return alert

    async def list_alerts(
        self,
        severity: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """分页查询告警列表

        Args:
            severity: 按级别过滤 (None 表示全部)。
            source: 按来源过滤 (None 表示全部)。
            status: 按状态过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。
            tenant_id: 租户 ID。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(Alert)
            .where(Alert.tenant_id == tenant_id)
            .order_by(Alert.created_at.desc())
        )
        if severity:
            base = base.where(Alert.severity == severity)
        if source:
            base = base.where(Alert.source == source)
        if status:
            base = base.where(Alert.status == status)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        return {
            "items": [self._alert_to_dict(a) for a in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_alert(
        self, alert_id: int, *, tenant_id: str = "default"
    ) -> Optional[Alert]:
        """获取告警实体 (内部使用)"""
        return (
            await self.session.execute(
                select(Alert).where(
                    Alert.id == alert_id,
                    Alert.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def get_alert_stats(self, *, tenant_id: str = "default") -> Dict[str, Any]:
        """告警统计 (按级别 / 状态 / 来源分组)

        Returns:
            {"by_severity": {...}, "by_status": {...}, "by_source": {...}, "total": N}
        """
        # 按级别统计
        severity_rows = (
            await self.session.execute(
                select(Alert.severity, func.count())
                .where(Alert.tenant_id == tenant_id)
                .group_by(Alert.severity)
            )
        ).all()
        by_severity = {row[0]: row[1] for row in severity_rows}

        # 按状态统计
        status_rows = (
            await self.session.execute(
                select(Alert.status, func.count())
                .where(Alert.tenant_id == tenant_id)
                .group_by(Alert.status)
            )
        ).all()
        by_status = {row[0]: row[1] for row in status_rows}

        # 按来源统计
        source_rows = (
            await self.session.execute(
                select(Alert.source, func.count())
                .where(Alert.tenant_id == tenant_id)
                .group_by(Alert.source)
            )
        ).all()
        by_source = {row[0]: row[1] for row in source_rows}

        total = (
            await self.session.execute(
                select(func.count())
                .select_from(Alert)
                .where(Alert.tenant_id == tenant_id)
            )
        ).scalar() or 0

        return {
            "by_severity": by_severity,
            "by_status": by_status,
            "by_source": by_source,
            "total": total,
        }

    # ===================== 状态管理 =====================

    async def acknowledge_alert(
        self, alert_id: int, user_id: str, *, tenant_id: str = "default"
    ) -> Alert:
        """确认告警

        将告警状态从 active → acknowledged。

        Args:
            alert_id: 告警 ID。
            user_id: 确认人 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 Alert 对象。
        """
        alert = (
            await self.session.execute(
                select(Alert).where(
                    Alert.id == alert_id,
                    Alert.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if alert is None:
            raise ValueError(f"告警 {alert_id} 不存在")
        if alert.status == ALERT_STATUS_RESOLVED:
            raise ValueError("已解决的告警不能确认")
        if alert.status == ALERT_STATUS_ACKNOWLEDGED:
            raise ValueError("告警已被确认")

        alert.status = ALERT_STATUS_ACKNOWLEDGED
        alert.acknowledged_at = datetime.now(timezone.utc)
        alert.acknowledged_by = user_id
        await self.session.flush()
        logger.info("确认告警 id=%s by=%s", alert_id, user_id)
        return alert

    async def resolve_alert(
        self, alert_id: int, user_id: str, *, tenant_id: str = "default"
    ) -> Alert:
        """解决告警

        将告警状态置为 resolved。

        Args:
            alert_id: 告警 ID。
            user_id: 解决人 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 Alert 对象。
        """
        alert = (
            await self.session.execute(
                select(Alert).where(
                    Alert.id == alert_id,
                    Alert.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if alert is None:
            raise ValueError(f"告警 {alert_id} 不存在")
        if alert.status == ALERT_STATUS_RESOLVED:
            raise ValueError("告警已处于解决状态")

        alert.status = ALERT_STATUS_RESOLVED
        alert.resolved_at = datetime.now(timezone.utc)
        alert.resolved_by = user_id
        # 若未确认过, 自动补充确认信息
        if alert.acknowledged_at is None:
            alert.acknowledged_at = alert.resolved_at
            alert.acknowledged_by = user_id
        await self.session.flush()
        # WS-3: 事务提交后向出站 Webhook 订阅推送 alert.resolved
        await self._dispatch_alert_event(alert, "alert.resolved")
        logger.info("解决告警 id=%s by=%s", alert_id, user_id)
        return alert

    # ===================== 通知发送 =====================

    async def send_alert(self, alert: Alert) -> Dict[str, Any]:
        """发送告警通知 (通过配置的通道)

        同时尝试飞书群机器人 / 邮件 / Webhook 三个通道,
        每个通道独立失败互不影响。

        Args:
            alert: 告警对象。

        Returns:
            {"feishu": bool, "email": bool, "webhook": bool}
        """
        results: Dict[str, bool] = {}

        # 飞书群机器人
        if self.feishu_webhook:
            results["feishu"] = await self.send_feishu_alert(alert)
        else:
            results["feishu"] = False

        # 邮件
        if self.email_to:
            results["email"] = await self.send_email_alert(alert)
        else:
            results["email"] = False

        # 出站 Webhook (WS-3: 订阅注册表 + HMAC 签名 + 指数退避重试 + 死信)
        results["webhook"] = await self.send_webhook_alert(alert)

        logger.info("告警通知发送完成 id=%s results=%s", alert.id, results)
        return results

    async def send_feishu_alert(self, alert: Alert) -> bool:
        """飞书群机器人通知 (交互式卡片消息)

        发送包含标题 + 描述 + 时间 + 级别标签的交互式卡片。

        Args:
            alert: 告警对象。

        Returns:
            True 表示发送成功, False 表示失败。
        """
        if not self.feishu_webhook:
            logger.debug("飞书 webhook 未配置, 跳过飞书通知")
            return False

        severity_label = SEVERITY_LABELS.get(alert.severity, alert.severity)
        template = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"[{severity_label}] {alert.title}",
                    },
                    "template": SEVERITY_COLORS.get(alert.severity, "blue"),
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": alert.message,
                        },
                    },
                    {
                        "tag": "div",
                        "fields": [
                            {
                                "is_short": True,
                                "text": {
                                    "tag": "lark_md",
                                    "content": f"**来源**\n{alert.source}",
                                },
                            },
                            {
                                "is_short": True,
                                "text": {
                                    "tag": "lark_md",
                                    "content": f"**级别**\n{severity_label}",
                                },
                            },
                        ],
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**时间**\n{alert.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') if alert.created_at else 'N/A'}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "此告警由 AgentValue 系统自动发送",
                            }
                        ],
                    },
                ],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.feishu_webhook, json=template)
                resp.raise_for_status()
            logger.info("飞书告警通知发送成功 id=%s", alert.id)
            return True
        except Exception as e:
            logger.warning("飞书告警通知发送失败 id=%s: %s", alert.id, e)
            return False

    async def send_email_alert(self, alert: Alert) -> bool:
        """邮件通知 (HTML 邮件模板)

        复用 EmailService 发送 HTML 邮件。

        Args:
            alert: 告警对象。

        Returns:
            True 表示发送成功, False 表示失败。
        """
        if not self.email_to:
            logger.debug("告警邮件收件人未配置, 跳过邮件通知")
            return False

        try:
            from services.email_service import EmailService

            email_service = EmailService()
            severity_label = SEVERITY_LABELS.get(alert.severity, alert.severity)

            # 构建 HTML 邮件正文
            html_body = self._build_alert_email_html(alert, severity_label)

            # 支持逗号分隔多个收件人
            recipients = [r.strip() for r in self.email_to.split(",") if r.strip()]
            success = True
            for recipient in recipients:
                ok = await email_service.send_email(
                    to_email=recipient,
                    subject=f"[{severity_label}] {alert.title}",
                    html_body=html_body,
                )
                if not ok:
                    success = False
            logger.info("邮件告警通知发送完成 id=%s success=%s", alert.id, success)
            return success
        except Exception as e:
            logger.warning("邮件告警通知发送失败 id=%s: %s", alert.id, e)
            return False

    async def send_webhook_alert(self, alert: Alert) -> bool:
        """出站 Webhook 通知 (WS-3: 走订阅注册表 + 签名 + 重试 + 死信)

        此前这里是一次裸 POST: 无签名、无重试、无投递日志, 对端抖动即丢事件。
        现改为投递给 ``webhook_subscriptions`` 注册表中匹配 ``alert.triggered``
        的所有订阅, 由 ``services/webhook_delivery_service`` 负责 HMAC 签名、
        指数退避重试与死信记录。

        兼容: 仍配置了 ``ALERT_WEBHOOK_URL`` 环境变量时, 会为该 URL 幂等托管一条
        订阅 (名称 ``环境变量告警 Webhook``), 使旧配置无需改动即可获得签名与重试;
        用户可在 admin 订阅页看到并管理它。

        Args:
            alert: 告警对象。

        Returns:
            True 表示事件已挂到事务提交后分发, False 表示分发链路不可用。
        """
        return await self._dispatch_alert_event(alert, "alert.triggered")

    async def _dispatch_alert_event(self, alert: Alert, event: str) -> bool:
        """把告警事件交给出站 Webhook 投递服务 (失败绝不影响告警主流程)"""
        try:
            from services.webhook_delivery_service import dispatch_after_commit

            await self._ensure_env_webhook_subscription(alert.tenant_id)
            return dispatch_after_commit(
                self.session,
                event,
                self._alert_to_dict(alert),
                tenant_id=alert.tenant_id,
                event_id=f"{event}:{alert.id}",
            )
        except Exception:
            logger.exception("告警事件分发失败 id=%s event=%s", alert.id, event)
            return False

    async def _ensure_env_webhook_subscription(self, tenant_id: str) -> None:
        """为 ALERT_WEBHOOK_URL 幂等托管一条订阅 (未配置该环境变量时直接返回)"""
        if not self.webhook_url:
            return
        from sqlalchemy import select as _select

        from core.database import AsyncSessionLocal
        from models.webhook_subscription import WebhookSubscription
        from services.webhook_delivery_service import generate_secret

        # 用独立会话, 避免把订阅托管写入业务事务
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    _select(WebhookSubscription).where(
                        WebhookSubscription.tenant_id == tenant_id,
                        WebhookSubscription.name == self.ENV_SUBSCRIPTION_NAME,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    WebhookSubscription(
                        tenant_id=tenant_id,
                        name=self.ENV_SUBSCRIPTION_NAME,
                        url=self.webhook_url,
                        events=["alert.*"],
                        secret=generate_secret(),
                        headers={},
                        enabled=True,
                        description="由 ALERT_WEBHOOK_URL 环境变量自动托管, "
                        "删除后下次告警会重建; 如需停用请清空该环境变量",
                        created_by="system",
                    )
                )
                await session.commit()
            elif existing.url != self.webhook_url:
                existing.url = self.webhook_url
                await session.commit()

    # ===================== 内部方法 =====================

    @staticmethod
    def _build_alert_email_html(alert: Alert, severity_label: str) -> str:
        """构建告警 HTML 邮件正文"""
        created_str = (
            alert.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if alert.created_at
            else "N/A"
        )
        metadata_str = ""
        if alert.metadata_:
            import json

            metadata_str = (
                f'<p style="color:#666;font-size:12px;margin-top:12px;">'
                f"<strong>元数据:</strong></p>"
                f'<pre style="background:#f5f5f5;padding:12px;border-radius:4px;'
                f'font-size:12px;overflow-x:auto;">'
                f"{json.dumps(alert.metadata_, ensure_ascii=False, indent=2)}"
                f"</pre>"
            )
        return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:20px;background-color:#f5f5f5;">
  <div style="max-width:600px;margin:0 auto;background-color:#fff;border-radius:8px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
    <div style="margin-bottom:16px;">
      <span style="display:inline-block;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:bold;color:#fff;background-color:#{'dc2626' if alert.severity=='critical' else '#ea580c' if alert.severity=='warning' else '#2563eb'};">
        {severity_label}
      </span>
    </div>
    <h2 style="color:#1a1a1a;margin:0 0 16px 0;">{alert.title}</h2>
    <div style="color:#4a4a4a;font-size:14px;line-height:1.6;white-space:pre-wrap;">{alert.message}</div>
    <hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">
    <p style="color:#666;font-size:12px;margin:4px 0;"><strong>来源:</strong> {alert.source}</p>
    <p style="color:#666;font-size:12px;margin:4px 0;"><strong>时间:</strong> {created_str}</p>
    <p style="color:#666;font-size:12px;margin:4px 0;"><strong>告警 ID:</strong> {alert.id}</p>
    {metadata_str}
    <hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">
    <p style="color:#999;font-size:12px;margin:0;">此邮件由 AgentValue 系统自动发送,请勿直接回复。</p>
  </div>
</body>
</html>"""

    @staticmethod
    def _alert_to_dict(a: Alert) -> Dict[str, Any]:
        """Alert → dict"""
        return {
            "id": a.id,
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "source": a.source,
            "status": a.status,
            "metadata": a.metadata_,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "acknowledged_at": (
                a.acknowledged_at.isoformat() if a.acknowledged_at else None
            ),
            "acknowledged_by": a.acknowledged_by,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "resolved_by": a.resolved_by,
        }
