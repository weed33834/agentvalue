"""
配额管理服务

提供租户配额的检查、记录、查询、更新与日重置功能。
- check_quota:    请求前检查是否超出日请求/token 配额
- record_usage:   请求后记录实际使用量（累加到 current_* 与日志表）
- get_quota:       获取配额配置（不存在则自动创建默认配额）
- update_quota:    更新配额上限配置
- get_usage_stats: 获取最近 N 天使用统计
- reset_daily_usage: 重置所有租户的日累计用量（由调度器调用）

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.tenant_context import get_current_tenant
from models.models import DEFAULT_TENANT_ID
from models.quota_models import (
    DEFAULT_MAX_API_KEYS,
    DEFAULT_MAX_REQUESTS_PER_DAY,
    DEFAULT_MAX_TOKENS_PER_DAY,
    QuotaUsageLog,
    TenantQuota,
)

logger = logging.getLogger(__name__)

# 默认配额常量（供外部引用）
_DEFAULT_QUOTA = {
    "max_requests_per_day": DEFAULT_MAX_REQUESTS_PER_DAY,
    "max_tokens_per_day": DEFAULT_MAX_TOKENS_PER_DAY,
    "max_api_keys": DEFAULT_MAX_API_KEYS,
}


class QuotaService:
    """配额管理服务

    支持两种使用模式:
    1. 路由层: QuotaService(session) 配合 get_db 依赖，事务由路由控制
    2. 中间件/后台: QuotaService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        # 标记是否由本服务管理 session（需自行 commit/close）
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        """获取或创建数据库会话"""
        if self._session is not None:
            return self._session
        # 中间件/后台调用时自建会话
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

    async def _get_or_create_quota(
        self, session: AsyncSession, tenant_id: str
    ) -> TenantQuota:
        """查询租户配额，不存在则用默认值创建"""
        result = await session.execute(
            select(TenantQuota).where(TenantQuota.tenant_id == tenant_id)
        )
        quota = result.scalar_one_or_none()
        if quota is None:
            # 不存在则创建默认配额
            quota = TenantQuota(
                tenant_id=tenant_id,
                max_requests_per_day=DEFAULT_MAX_REQUESTS_PER_DAY,
                max_tokens_per_day=DEFAULT_MAX_TOKENS_PER_DAY,
                max_api_keys=DEFAULT_MAX_API_KEYS,
                current_requests_today=0,
                current_tokens_today=0,
                enabled=True,
            )
            session.add(quota)
            await session.flush()
            logger.info("为租户 %s 创建默认配额", tenant_id)
        return quota

    async def check_quota(self, tenant_id: str, estimated_tokens: int = 0) -> bool:
        """检查租户是否超出配额

        检查逻辑:
        - 配额未启用（enabled=False）直接放行
        - current_requests_today >= max_requests_per_day 拒绝
        - current_tokens_today + estimated_tokens >= max_tokens_per_day 拒绝

        Args:
            tenant_id: 租户 ID
            estimated_tokens: 预估本次请求将消耗的 token 数

        Returns:
            True 表示配额充足可放行，False 表示已超限
        """
        session = await self._get_session()
        try:
            quota = await self._get_or_create_quota(session, tenant_id)

            # 配额未启用，直接放行
            if not quota.enabled:
                return True

            # 检查请求数配额
            if quota.current_requests_today >= quota.max_requests_per_day:
                logger.warning(
                    "租户 %s 请求数超限: %d/%d",
                    tenant_id,
                    quota.current_requests_today,
                    quota.max_requests_per_day,
                )
                return False

            # 检查 token 配额（加上预估 token）
            projected_tokens = quota.current_tokens_today + estimated_tokens
            if projected_tokens >= quota.max_tokens_per_day:
                logger.warning(
                    "租户 %s token 超限: %d(+%d)/%d",
                    tenant_id,
                    quota.current_tokens_today,
                    estimated_tokens,
                    quota.max_tokens_per_day,
                )
                return False

            return True
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def record_usage(
        self,
        tenant_id: str,
        request_count: int,
        token_count: int,
        cost: float,
    ) -> None:
        """记录使用量（累加到配额表当前用量 + 写入日志表）

        在请求处理完成后调用，将实际消耗的请求数、token 数与成本记录下来。

        Args:
            tenant_id: 租户 ID
            request_count: 本次新增请求数（通常为 1）
            token_count: 本次消耗的 token 数
            cost: 本次成本（美元）
        """
        session = await self._get_session()
        try:
            quota = await self._get_or_create_quota(session, tenant_id)

            # 累加当前用量
            quota.current_requests_today += request_count
            quota.current_tokens_today += token_count

            # 写入/更新日志表（按天聚合）
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_result = await session.execute(
                select(QuotaUsageLog).where(
                    QuotaUsageLog.tenant_id == tenant_id,
                    QuotaUsageLog.usage_date == today,
                )
            )
            usage_log = log_result.scalar_one_or_none()
            if usage_log is None:
                # 当天首条记录
                usage_log = QuotaUsageLog(
                    tenant_id=tenant_id,
                    usage_date=today,
                    request_count=request_count,
                    token_count=token_count,
                    cost_usd=cost,
                )
                session.add(usage_log)
            else:
                # 累加到已有记录
                usage_log.request_count += request_count
                usage_log.token_count += token_count
                usage_log.cost_usd += cost

            await session.flush()
            await self._commit_if_owned()
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_quota(self, tenant_id: str) -> Dict[str, Any]:
        """获取租户配额配置（不存在则自动创建默认配额）

        Args:
            tenant_id: 租户 ID

        Returns:
            配额信息字典
        """
        session = await self._get_session()
        try:
            quota = await self._get_or_create_quota(session, tenant_id)
            return self._serialize_quota(quota)
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_quota(self, tenant_id: str, **kwargs) -> Dict[str, Any]:
        """更新租户配额配置

        可更新字段: max_requests_per_day, max_tokens_per_day, max_api_keys, enabled

        Args:
            tenant_id: 租户 ID
            **kwargs: 要更新的字段

        Returns:
            更新后的配额信息
        """
        # 允许更新的字段白名单
        allowed_fields = {
            "max_requests_per_day",
            "max_tokens_per_day",
            "max_api_keys",
            "enabled",
        }
        session = await self._get_session()
        try:
            quota = await self._get_or_create_quota(session, tenant_id)

            changed: Dict[str, Any] = {}
            for key, value in kwargs.items():
                if key in allowed_fields and value is not None:
                    old_value = getattr(quota, key)
                    if old_value != value:
                        setattr(quota, key, value)
                        changed[key] = value

            if changed:
                await session.flush()
                await self._commit_if_owned()
                logger.info("租户 %s 配额已更新: %s", tenant_id, changed)

            return self._serialize_quota(quota)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_usage_stats(
        self, tenant_id: str, days: int = 30
    ) -> List[Dict[str, Any]]:
        """获取租户最近 N 天的使用统计

        Args:
            tenant_id: 租户 ID
            days: 统计天数（默认 30）

        Returns:
            每日使用量列表，按日期升序排列
        """
        session = await self._get_session()
        try:
            # 计算起始日期
            now = datetime.now(timezone.utc)
            start_date = now.strftime("%Y-%m-%d")
            # 向前推 days 天
            from datetime import timedelta

            start_dt = now - timedelta(days=days)
            start_date_str = start_dt.strftime("%Y-%m-%d")

            result = await session.execute(
                select(QuotaUsageLog)
                .where(
                    QuotaUsageLog.tenant_id == tenant_id,
                    QuotaUsageLog.usage_date >= start_date_str,
                )
                .order_by(QuotaUsageLog.usage_date.asc())
            )
            logs = result.scalars().all()

            return [
                {
                    "usage_date": log.usage_date,
                    "request_count": log.request_count,
                    "token_count": log.token_count,
                    "cost_usd": round(log.cost_usd, 6),
                }
                for log in logs
            ]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def reset_daily_usage(self) -> int:
        """重置所有租户的日配额用量（由调度器每日调用）

        将 current_requests_today / current_tokens_today 清零，
        并更新 quota_reset_at 为当前时间。

        Returns:
            被重置的租户数量
        """
        session = await self._get_session()
        try:
            now = datetime.now(timezone.utc)
            result = await session.execute(
                update(TenantQuota).values(
                    current_requests_today=0,
                    current_tokens_today=0,
                    quota_reset_at=now,
                )
            )
            affected = result.rowcount or 0
            await self._commit_if_owned()
            logger.info("日配额重置完成，影响 %d 个租户", affected)
            return affected
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    @staticmethod
    def _serialize_quota(quota: TenantQuota) -> Dict[str, Any]:
        """序列化 TenantQuota 为 dict"""
        return {
            "id": quota.id,
            "tenant_id": quota.tenant_id,
            "max_requests_per_day": quota.max_requests_per_day,
            "max_tokens_per_day": quota.max_tokens_per_day,
            "max_api_keys": quota.max_api_keys,
            "current_requests_today": quota.current_requests_today,
            "current_tokens_today": quota.current_tokens_today,
            "quota_reset_at": (
                quota.quota_reset_at.isoformat() if quota.quota_reset_at else None
            ),
            "enabled": quota.enabled,
            "created_at": quota.created_at.isoformat() if quota.created_at else None,
            "updated_at": quota.updated_at.isoformat() if quota.updated_at else None,
        }
