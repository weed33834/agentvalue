"""
站内通知服务

管理用户通知的创建、查询、已读标记与删除。
通知类型:evaluation(评估)/ approval(审批)/ system(系统)/ webhook(外部事件)。
事务边界由路由层控制(service 层不 commit)。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.tenant_context import get_current_tenant
from models.models import DEFAULT_TENANT_ID, Notification

logger = logging.getLogger(__name__)

# 允许的通知类型白名单
NOTIFICATION_TYPES = {"evaluation", "approval", "system", "webhook"}

# 单页最大条数
_MAX_PAGE_SIZE = 100
# 默认每页条数
_DEFAULT_PAGE_SIZE = 20


class NotificationService:
    """站内通知服务（数据库实现）"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_notification(
        self,
        user_id: str,
        type: str,
        title: str,
        content: Optional[str] = None,
        link: Optional[str] = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Notification:
        """创建一条站内通知（不 commit，由调用方控制事务）

        Args:
            user_id: 接收通知的用户 ID。
            type: 通知类型 evaluation/approval/system/webhook。
            title: 通知标题。
            content: 通知正文（可空）。
            link: 点击跳转 URL（可空）。
            tenant_id: 租户 ID，默认 default。

        Returns:
            创建的 Notification 对象。
        """
        if type not in NOTIFICATION_TYPES:
            raise ValueError(
                f"无效的通知类型: {type}, 可选: {NOTIFICATION_TYPES}"
            )

        notification = Notification(
            notification_id=f"NTF-{uuid.uuid4().hex[:16]}",
            user_id=user_id,
            type=type,
            title=title,
            content=content,
            link=link,
            category=type,  # 兼容旧字段
            is_read=False,
            tenant_id=tenant_id or get_current_tenant(),
        )
        self.session.add(notification)
        await self.session.flush()
        logger.info(
            "通知已创建 user_id=%s type=%s title=%s", user_id, type, title
        )
        return notification

    async def list_notifications(
        self,
        user_id: str,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """分页查询用户通知列表

        Args:
            user_id: 用户 ID。
            unread_only: 仅返回未读通知。
            page: 页码（从 1 开始）。
            page_size: 每页条数。

        Returns:
            {"total", "page", "page_size", "unread_count", "items"}
        """
        page = max(page, 1)
        page_size = min(max(page_size, 1), _MAX_PAGE_SIZE)
        tenant_id = get_current_tenant()

        # 基础查询条件
        conditions = [
            Notification.user_id == user_id,
            Notification.tenant_id == tenant_id,
        ]
        if unread_only:
            conditions.append(Notification.is_read == False)  # noqa: E712

        # 总数
        count_stmt = select(func.count()).select_from(Notification)
        for cond in conditions:
            count_stmt = count_stmt.where(cond)
        total = (await self.session.execute(count_stmt)).scalar() or 0

        # 未读数（无论 unread_only 都返回，便于前端展示角标）
        unread_stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                Notification.is_read == False,  # noqa: E712
            )
        )
        unread_count = (await self.session.execute(unread_stmt)).scalar() or 0

        # 分页查询
        offset = (page - 1) * page_size
        list_stmt = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = (await self.session.execute(list_stmt)).scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "unread_count": unread_count,
            "items": [self._serialize(n) for n in rows],
        }

    async def mark_as_read(self, notification_id: str, user_id: str | None = None) -> bool:
        """标记单条通知为已读（不 commit）

        Args:
            notification_id: 通知 ID（notification_id 字段，非主键 id）。
            user_id: 用户 ID，用于权限校验（防止 IDOR 越权）。

        Returns:
            True 表示成功标记，False 表示通知不存在。
        """
        tenant_id = get_current_tenant()
        stmt = (
            select(Notification)
            .where(
                Notification.notification_id == notification_id,
                Notification.tenant_id == tenant_id,
            )
        )
        if user_id is not None:
            stmt = stmt.where(Notification.user_id == user_id)
        notification = (await self.session.execute(stmt)).scalar_one_or_none()
        if notification is None:
            return False
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.now(timezone.utc)
            await self.session.flush()
        return True

    async def mark_all_as_read(self, user_id: str) -> int:
        """标记用户所有未读通知为已读（不 commit）

        Args:
            user_id: 用户 ID。

        Returns:
            被标记为已读的通知数量。
        """
        tenant_id = get_current_tenant()
        now = datetime.now(timezone.utc)
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                Notification.is_read == False,  # noqa: E712
            )
            .values(is_read=True, read_at=now)
        )
        result = await self.session.execute(stmt)
        affected = result.rowcount or 0
        logger.info(
            "批量标记已读 user_id=%s count=%d", user_id, affected
        )
        return affected

    async def delete_notification(self, notification_id: str, user_id: str | None = None) -> bool:
        """删除单条通知（不 commit）

        Args:
            notification_id: 通知 ID。
            user_id: 用户 ID，用于权限校验（防止 IDOR 越权）。

        Returns:
            True 表示已删除，False 表示通知不存在。
        """
        tenant_id = get_current_tenant()
        stmt = (
            select(Notification)
            .where(
                Notification.notification_id == notification_id,
                Notification.tenant_id == tenant_id,
            )
        )
        if user_id is not None:
            stmt = stmt.where(Notification.user_id == user_id)
        notification = (await self.session.execute(stmt)).scalar_one_or_none()
        if notification is None:
            return False
        await self.session.delete(notification)
        await self.session.flush()
        return True

    async def get_unread_count(self, user_id: str) -> int:
        """获取用户未读通知数量

        Args:
            user_id: 用户 ID。

        Returns:
            未读通知数量。
        """
        tenant_id = get_current_tenant()
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                Notification.is_read == False,  # noqa: E712
            )
        )
        return (await self.session.execute(stmt)).scalar() or 0

    @staticmethod
    def _serialize(notification: Notification) -> Dict[str, Any]:
        """序列化 Notification 为 dict"""
        return {
            "notification_id": notification.notification_id,
            "user_id": notification.user_id,
            "type": notification.type,
            "title": notification.title,
            "content": notification.content,
            "link": notification.link,
            "is_read": notification.is_read,
            "read_at": notification.read_at.isoformat()
            if notification.read_at
            else None,
            "created_at": notification.created_at.isoformat()
            if notification.created_at
            else None,
        }
