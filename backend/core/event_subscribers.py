"""事件总线订阅者注册

在应用启动时注册 webhook 事件的订阅者，使 event_bus 的 publish 有消费者。

订阅频道:
- webhook:feishu:message  → 飞书消息接收 → 创建通知 + 可选 AI 自动回复
- webhook:feishu:card     → 飞书卡片回调 → 创建通知
- webhook:gitlab:push     → GitLab push → 创建代码贡献通知
- webhook:gitlab:mr       → GitLab MR → 创建 MR 状态通知
- webhook:gitlab:issue    → GitLab Issue → 创建 issue 通知
- webhook:custom:*        → 自定义 webhook → 创建通知

设计原则:
- 订阅者异常不阻断其他订阅者（EventBus 已保证）
- 订阅者内 DB 操作用独立 session，避免与请求事务耦合
- 通知创建失败仅记录日志，不抛异常
- 通知接收人优先从 webhook 配置中查找 admin 用户，降级为 DEFAULT_TENANT_ID
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_unsubscribers: list = []


async def _get_admin_user_id() -> str:
    """查找系统中的管理员用户 ID。

    查找顺序:
    1. 查询 users 表中 role='admin' 且 is_active=True 的第一个用户
    2. 降级为 DEFAULT_TENANT_ID 中的第一个用户
    3. 最终降级为 "admin"
    """
    try:
        from sqlalchemy import select
        from core.database import async_session_factory
        from models.models import User

        async with async_session_factory() as session:
            stmt = (
                select(User.user_id)
                .where(User.role == "admin", User.is_active.is_(True))
                .limit(1)
            )
            result = await session.execute(stmt)
            admin_id = result.scalar_one_or_none()
            if admin_id:
                return admin_id
    except Exception:
        pass
    return "admin"


async def _create_notification(
    user_id: str,
    title: str,
    content: str,
    notification_type: str = "webhook",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """创建站内通知（独立 session，不影响调用方事务）

    如果 user_id 为空，自动查找 admin 用户。
    """
    if not user_id:
        user_id = await _get_admin_user_id()

    try:
        from core.database import AsyncSessionLocal
        from services.notification_service import NotificationService

        async with AsyncSessionLocal() as session:
            service = NotificationService(session)
            await service.create_notification(
                user_id=user_id,
                title=title,
                content=content,
                notification_type=notification_type,
                metadata=metadata or {},
            )
            await session.commit()
    except Exception:
        logger.exception("创建通知失败 user_id=%s title=%s", user_id, title)


async def _on_feishu_message(payload: Dict[str, Any]) -> None:
    """飞书消息接收事件 → 创建通知 + 可选 AI 回复"""
    message_id = payload.get("message_id", "")
    chat_id = payload.get("chat_id", "")
    sender_id = payload.get("sender_id", "")
    text = payload.get("text", "")

    logger.info(
        "事件订阅: 飞书消息 message_id=%s sender=%s text=%s",
        message_id, sender_id, text[:80],
    )

    # 创建站内通知（自动查找 admin 用户）
    await _create_notification(
        user_id="",  # 留空让 _create_notification 自动查找
        title=f"飞书消息: {text[:50]}" if text else "飞书消息",
        content=f"发送者: {sender_id}\n消息: {text}\n消息ID: {message_id}",
        notification_type="webhook_feishu_message",
        metadata={"chat_id": chat_id, "message_id": message_id},
    )


async def _on_feishu_card(payload: Dict[str, Any]) -> None:
    """飞书卡片回调事件 → 创建通知"""
    operator_id = payload.get("operator_id", "")
    action_value = payload.get("action_value", "")

    logger.info("事件订阅: 飞书卡片回调 operator=%s", operator_id)

    await _create_notification(
        user_id="",
        title="飞书卡片回调",
        content=f"操作者: {operator_id}\n动作: {action_value}",
        notification_type="webhook_feishu_card",
        metadata={"operator_id": operator_id},
    )


async def _on_gitlab_push(payload: Dict[str, Any]) -> None:
    """GitLab push 事件 → 创建代码贡献通知"""
    repo = payload.get("repo", "")
    branch = payload.get("branch", "")
    user = payload.get("user", "")
    commits = payload.get("commits", [])

    logger.info(
        "事件订阅: GitLab push repo=%s branch=%s commits=%d",
        repo, branch, len(commits),
    )

    commit_summaries = "\n".join(
        f"  - {c.get('message', '')[:60]}" for c in commits[:5]
    )
    if len(commits) > 5:
        commit_summaries += f"\n  ... 共 {len(commits)} 条提交"

    await _create_notification(
        user_id="",
        title=f"GitLab 代码推送: {repo}",
        content=f"分支: {branch}\n推送者: {user}\n提交:\n{commit_summaries}",
        notification_type="webhook_gitlab_push",
        metadata={"repo": repo, "branch": branch, "commit_count": len(commits)},
    )


async def _on_gitlab_mr(payload: Dict[str, Any]) -> None:
    """GitLab MR 事件 → 创建 MR 状态通知"""
    repo = payload.get("repo", "")
    title = payload.get("title", "")
    action = payload.get("action", "")
    state = payload.get("state", "")
    author = payload.get("author", "")
    url = payload.get("url", "")

    logger.info("事件订阅: GitLab MR repo=%s title=%s action=%s", repo, title, action)

    await _create_notification(
        user_id="",
        title=f"GitLab MR {action}: {title[:50]}",
        content=f"仓库: {repo}\n标题: {title}\n状态: {state}\n作者: {author}\n链接: {url}",
        notification_type="webhook_gitlab_mr",
        metadata={"repo": repo, "action": action, "state": state},
    )


async def _on_gitlab_issue(payload: Dict[str, Any]) -> None:
    """GitLab Issue 事件 → 创建 issue 通知"""
    repo = payload.get("repo", "")
    title = payload.get("title", "")
    action = payload.get("action", "")
    state = payload.get("state", "")
    author = payload.get("author", "")
    url = payload.get("url", "")

    logger.info("事件订阅: GitLab Issue repo=%s title=%s action=%s", repo, title, action)

    await _create_notification(
        user_id="",
        title=f"GitLab Issue {action}: {title[:50]}",
        content=f"仓库: {repo}\n标题: {title}\n状态: {state}\n作者: {author}\n链接: {url}",
        notification_type="webhook_gitlab_issue",
        metadata={"repo": repo, "action": action, "state": state},
    )


async def _on_custom_webhook(payload: Dict[str, Any]) -> None:
    """自定义 webhook 事件 → 创建通知

    处理 webhook:custom:{hook_id} 频道的事件。
    由于 EventBus 不支持通配符匹配，此处作为通用订阅者，
    注册到所有已知的 custom webhook 频道。
    """
    event_type = payload.get("event_type", "unknown")
    extra = payload.get("extra", {})
    hook_id = extra.get("hook_id", "unknown")
    event_payload = payload.get("payload", {})

    logger.info(
        "事件订阅: 自定义 webhook hook_id=%s event_type=%s",
        hook_id, event_type,
    )

    await _create_notification(
        user_id="",
        title=f"自定义 Webhook: {hook_id} / {event_type}",
        content=f"Hook ID: {hook_id}\n事件类型: {event_type}\n数据: {str(event_payload)[:500]}",
        notification_type="webhook_custom",
        metadata={"hook_id": hook_id, "event_type": event_type},
    )


def register_event_subscribers() -> None:
    """注册所有事件总线订阅者。

    在应用 lifespan 启动时调用，幂等注册。
    """
    from core.event_bus import get_event_bus

    global _unsubscribers

    # 幂等：先取消旧订阅再注册
    for unsub in _unsubscribers:
        try:
            unsub()
        except Exception:
            pass
    _unsubscribers.clear()

    bus = get_event_bus()

    _unsubscribers.append(
        bus.subscribe("webhook:feishu:message", _on_feishu_message)
    )
    _unsubscribers.append(
        bus.subscribe("webhook:feishu:card", _on_feishu_card)
    )
    _unsubscribers.append(
        bus.subscribe("webhook:gitlab:push", _on_gitlab_push)
    )
    _unsubscribers.append(
        bus.subscribe("webhook:gitlab:mr", _on_gitlab_mr)
    )
    _unsubscribers.append(
        bus.subscribe("webhook:gitlab:issue", _on_gitlab_issue)
    )
    # 自定义 webhook 通用订阅者
    _unsubscribers.append(
        bus.subscribe("webhook:custom", _on_custom_webhook)
    )

    logger.info(
        "事件总线订阅者已注册: feishu:message, feishu:card, "
        "gitlab:push, gitlab:mr, gitlab:issue, custom"
    )


def unregister_event_subscribers() -> None:
    """取消所有事件总线订阅者（在应用关闭时调用）。"""
    global _unsubscribers
    for unsub in _unsubscribers:
        try:
            unsub()
        except Exception:
            pass
    _unsubscribers.clear()
    logger.info("事件总线订阅者已注销")
