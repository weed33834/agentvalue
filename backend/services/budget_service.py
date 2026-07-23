"""
成本预算告警服务

提供预算的创建、查询、用量更新与阈值告警功能。
- check_budget:        检查预算并在超阈值时触发告警通知
- create_budget:       创建预算
- get_budgets:          获取预算列表
- update_budget_usage:  更新预算使用量
- get_budget_status:    获取预算状态（百分比、是否告警）

告警触发逻辑: 当 current_usage >= budget_limit * alert_threshold 时，
通过 NotificationService 向租户管理员发送站内通知，并将 alerted 置为 True。
事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from models.models import DEFAULT_TENANT_ID, User
from models.quota_models import BudgetAlert
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)

# 预算类型白名单
BUDGET_TYPES = {"monthly", "daily"}


class BudgetService:
    """成本预算告警服务

    支持两种使用模式:
    1. 路由层: BudgetService(session) 配合 get_db 依赖，事务由路由控制
    2. 中间件/后台: BudgetService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        """获取或创建数据库会话"""
        if self._session is not None:
            return self._session
        self._session = AsyncSessionLocal()
        self._owns_session = True
        return self._session

    async def _commit_if_owned(self) -> None:
        """如果 session 由本服务创建，则自动 commit"""
        if self._owns_session and self._session is not None:
            await self._session.commit()

    async def _close_if_owned(self) -> None:
        """如果 session 由本服务创建，则自动关闭"""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_tenant_admins(
        self, session: AsyncSession, tenant_id: str
    ) -> List[User]:
        """查询租户下的所有管理员用户（用于发送告警通知）"""
        result = await session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.role == "admin",
            )
        )
        return list(result.scalars().all())

    async def check_budget(self, tenant_id: str, cost: float) -> List[Dict[str, Any]]:
        """检查预算并在超阈值时触发告警

        遍历该租户所有有效预算，当 current_usage + cost >= limit * threshold
        且尚未告警（alerted=False）时，创建站内通知并标记 alerted=True。

        Args:
            tenant_id: 租户 ID
            cost: 本次新增成本（美元）

        Returns:
            触发告警的预算列表
        """
        session = await self._get_session()
        triggered: List[Dict[str, Any]] = []
        try:
            # 先更新用量
            await self._update_budget_usage_internal(session, tenant_id, cost)

            # 查询所有有效预算
            result = await session.execute(
                select(BudgetAlert).where(
                    BudgetAlert.tenant_id == tenant_id,
                )
            )
            budgets = result.scalars().all()

            now = datetime.now(timezone.utc)
            # 检查是否有过期预算需要重置
            for budget in budgets:
                # 日度预算: 过了 period_end 则重置
                if budget.period_end and budget.period_end < now:
                    budget.current_usage = 0.0
                    budget.alerted = False
                    budget.period_start = now
                    # 重新计算 period_end
                    if budget.budget_type == "daily":
                        from datetime import timedelta

                        budget.period_end = now + timedelta(days=1)
                    elif budget.budget_type == "monthly":
                        from datetime import timedelta

                        budget.period_end = now + timedelta(days=30)

            # 检查阈值
            for budget in budgets:
                threshold_value = budget.budget_limit * budget.alert_threshold
                if (
                    budget.current_usage >= threshold_value
                    and not budget.alerted
                    and budget.budget_limit > 0
                ):
                    # 触发告警
                    budget.alerted = True
                    await self._send_alert_notification(session, tenant_id, budget)
                    triggered.append(self._serialize_budget(budget))
                    logger.warning(
                        "预算告警触发 tenant=%s budget_id=%d usage=%.2f/%.2f(%.0f%%)",
                        tenant_id,
                        budget.id,
                        budget.current_usage,
                        budget.budget_limit,
                        (
                            (budget.current_usage / budget.budget_limit * 100)
                            if budget.budget_limit > 0
                            else 0
                        ),
                    )

            await session.flush()
            await self._commit_if_owned()
            return triggered
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def _send_alert_notification(
        self,
        session: AsyncSession,
        tenant_id: str,
        budget: BudgetAlert,
    ) -> None:
        """通过 NotificationService 向租户管理员发送告警通知"""
        try:
            admins = await self._get_tenant_admins(session, tenant_id)
            if not admins:
                logger.warning("租户 %s 无管理员用户，预算告警通知未发送", tenant_id)
                return

            usage_percent = (
                (budget.current_usage / budget.budget_limit * 100)
                if budget.budget_limit > 0
                else 0
            )
            title = f"预算告警: {budget.budget_type} 预算已使用 {usage_percent:.1f}%"
            content = (
                f"预算类型: {budget.budget_type}\n"
                f"预算上限: ${budget.budget_limit:.2f}\n"
                f"当前用量: ${budget.current_usage:.2f}\n"
                f"告警阈值: {budget.alert_threshold * 100:.0f}%\n"
                f"使用比例: {usage_percent:.1f}%"
            )

            notif_service = NotificationService(session)
            for admin in admins:
                await notif_service.create_notification(
                    user_id=admin.user_id,
                    type="system",
                    title=title,
                    content=content,
                    tenant_id=tenant_id,
                )
        except Exception:
            # 通知发送失败不阻断预算检查主流程
            logger.exception("预算告警通知发送失败 budget_id=%d", budget.id)

    async def create_budget(
        self,
        tenant_id: str,
        budget_type: str,
        limit: float,
        threshold: float = 0.8,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """创建预算

        Args:
            tenant_id: 租户 ID
            budget_type: 预算类型 monthly / daily
            limit: 预算上限（美元）
            threshold: 告警阈值（0-1，默认 0.8）
            period_start: 周期开始时间（默认当前时间）
            period_end: 周期结束时间（日度默认+1天，月度默认+30天）

        Returns:
            创建的预算信息
        """
        if budget_type not in BUDGET_TYPES:
            raise ValueError(f"无效的预算类型: {budget_type}, 可选: {BUDGET_TYPES}")
        if not (0 < threshold <= 1):
            raise ValueError(f"告警阈值必须在 (0, 1] 范围内, 当前: {threshold}")

        from datetime import timedelta

        now = datetime.now(timezone.utc)
        if period_start is None:
            period_start = now
        if period_end is None:
            if budget_type == "daily":
                period_end = now + timedelta(days=1)
            else:
                period_end = now + timedelta(days=30)

        session = await self._get_session()
        try:
            budget = BudgetAlert(
                tenant_id=tenant_id,
                budget_type=budget_type,
                budget_limit=limit,
                current_usage=0.0,
                alert_threshold=threshold,
                alerted=False,
                period_start=period_start,
                period_end=period_end,
            )
            session.add(budget)
            await session.flush()
            await self._commit_if_owned()
            logger.info(
                "预算已创建 tenant=%s type=%s limit=%.2f",
                tenant_id,
                budget_type,
                limit,
            )
            return self._serialize_budget(budget)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_budgets(self, tenant_id: str) -> List[Dict[str, Any]]:
        """获取租户的所有预算列表

        Args:
            tenant_id: 租户 ID

        Returns:
            预算信息列表
        """
        session = await self._get_session()
        try:
            result = await session.execute(
                select(BudgetAlert)
                .where(BudgetAlert.tenant_id == tenant_id)
                .order_by(BudgetAlert.created_at.desc())
            )
            budgets = result.scalars().all()
            return [self._serialize_budget(b) for b in budgets]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_budget_usage(self, tenant_id: str, cost: float) -> None:
        """更新租户所有预算的使用量（累加 cost）

        Args:
            tenant_id: 租户 ID
            cost: 新增成本（美元）
        """
        session = await self._get_session()
        try:
            await self._update_budget_usage_internal(session, tenant_id, cost)
            await self._commit_if_owned()
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def _update_budget_usage_internal(
        self, session: AsyncSession, tenant_id: str, cost: float
    ) -> None:
        """内部方法: 累加预算使用量（不 commit，由调用方控制）"""
        result = await session.execute(
            select(BudgetAlert).where(BudgetAlert.tenant_id == tenant_id)
        )
        budgets = result.scalars().all()
        now = datetime.now(timezone.utc)
        for budget in budgets:
            # 检查是否需要周期重置
            if budget.period_end and budget.period_end < now:
                budget.current_usage = 0.0
                budget.alerted = False
                budget.period_start = now
                from datetime import timedelta

                if budget.budget_type == "daily":
                    budget.period_end = now + timedelta(days=1)
                else:
                    budget.period_end = now + timedelta(days=30)
            budget.current_usage += cost

    async def get_budget_status(self, tenant_id: str) -> List[Dict[str, Any]]:
        """获取租户所有预算的状态（百分比、是否告警）

        Args:
            tenant_id: 租户 ID

        Returns:
            预算状态列表，每项包含 usage_percent / is_alerted 等
        """
        session = await self._get_session()
        try:
            result = await session.execute(
                select(BudgetAlert)
                .where(BudgetAlert.tenant_id == tenant_id)
                .order_by(BudgetAlert.created_at.desc())
            )
            budgets = result.scalars().all()
            return [self._serialize_budget_status(b) for b in budgets]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_budget(
        self, budget_id: int, updates: Dict[str, Any], tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """更新预算配置

        可更新字段: budget_type, budget_limit, alert_threshold, period_start, period_end

        Args:
            budget_id: 预算 ID
            updates: 要更新的字段字典
            tenant_id: 租户 ID (IDOR 防护: 仅允许操作本租户预算)

        Returns:
            更新后的预算信息，不存在则返回 None
        """
        allowed_fields = {
            "budget_type",
            "budget_limit",
            "alert_threshold",
            "period_start",
            "period_end",
        }
        session = await self._get_session()
        try:
            result = await session.execute(
                select(BudgetAlert).where(
                    BudgetAlert.id == budget_id,
                    BudgetAlert.tenant_id == tenant_id,
                )
            )
            budget = result.scalar_one_or_none()
            if budget is None:
                return None

            changed: Dict[str, Any] = {}
            for key, value in updates.items():
                if key in allowed_fields and value is not None:
                    old_value = getattr(budget, key)
                    if old_value != value:
                        setattr(budget, key, value)
                        changed[key] = value

            # 如果修改了 budget_limit 且当前未告警，重置 alerted 状态
            if "budget_limit" in changed and not budget.alerted:
                pass  # 无需额外处理

            await session.flush()
            await self._commit_if_owned()
            if changed:
                logger.info("预算 %d 已更新: %s", budget_id, changed)
            return self._serialize_budget(budget)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_budget(self, budget_id: int, tenant_id: str) -> bool:
        """删除预算

        Args:
            budget_id: 预算 ID
            tenant_id: 租户 ID (IDOR 防护: 仅允许操作本租户预算)

        Returns:
            True 表示已删除，False 表示不存在
        """
        session = await self._get_session()
        try:
            result = await session.execute(
                select(BudgetAlert).where(
                    BudgetAlert.id == budget_id,
                    BudgetAlert.tenant_id == tenant_id,
                )
            )
            budget = result.scalar_one_or_none()
            if budget is None:
                return False
            await session.delete(budget)
            await self._commit_if_owned()
            logger.info("预算 %d 已删除", budget_id)
            return True
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    @staticmethod
    def _serialize_budget(budget: BudgetAlert) -> Dict[str, Any]:
        """序列化 BudgetAlert 为 dict"""
        return {
            "id": budget.id,
            "tenant_id": budget.tenant_id,
            "budget_type": budget.budget_type,
            "budget_limit": budget.budget_limit,
            "current_usage": round(budget.current_usage, 6),
            "alert_threshold": budget.alert_threshold,
            "alerted": budget.alerted,
            "period_start": (
                budget.period_start.isoformat() if budget.period_start else None
            ),
            "period_end": budget.period_end.isoformat() if budget.period_end else None,
            "created_at": budget.created_at.isoformat() if budget.created_at else None,
            "updated_at": budget.updated_at.isoformat() if budget.updated_at else None,
        }

    @staticmethod
    def _serialize_budget_status(budget: BudgetAlert) -> Dict[str, Any]:
        """序列化预算状态（含百分比与告警标记）"""
        base = BudgetService._serialize_budget(budget)
        usage_percent = (
            (budget.current_usage / budget.budget_limit * 100)
            if budget.budget_limit > 0
            else 0
        )
        threshold_percent = budget.alert_threshold * 100
        base["usage_percent"] = round(usage_percent, 2)
        base["threshold_percent"] = round(threshold_percent, 2)
        base["is_alerted"] = budget.alerted
        base["remaining"] = round(max(budget.budget_limit - budget.current_usage, 0), 6)
        return base
