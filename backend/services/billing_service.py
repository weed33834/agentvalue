"""
API 计费服务

提供计费记录的写入、汇总查询与导出功能。
- record_billing:        记录单次请求的计费信息
- get_billing_summary:   获取账单汇总（总请求数、总 token、总成本）
- get_billing_by_user:   按用户汇总计费
- get_billing_by_endpoint: 按端点汇总计费
- export_billing:        导出账单（CSV / JSON 格式）

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from models.models import DEFAULT_TENANT_ID
from models.quota_models import BillingRecord

logger = logging.getLogger(__name__)


class BillingService:
    """API 计费服务

    支持两种使用模式:
    1. 路由层: BillingService(session) 配合 get_db 依赖，事务由路由控制
    2. 中间件/后台: BillingService() 无 session，内部自建会话并自动 commit
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

    async def record_billing(
        self,
        tenant_id: str,
        user_id: str,
        endpoint: str,
        method: str,
        tokens: int,
        cost: float,
    ) -> Dict[str, Any]:
        """记录单次请求的计费信息

        Args:
            tenant_id: 租户 ID
            user_id: 调用方用户 ID
            endpoint: API 端点路径
            method: HTTP 方法
            tokens: 本次请求消耗的 token 数
            cost: 本次成本（美元）

        Returns:
            创建的计费记录信息
        """
        now = datetime.now(timezone.utc)
        # 账单周期: YYYY-MM 格式
        invoice_period = now.strftime("%Y-%m")

        session = await self._get_session()
        try:
            record = BillingRecord(
                tenant_id=tenant_id,
                user_id=user_id,
                api_endpoint=endpoint,
                method=method.upper(),
                tokens_used=tokens,
                cost_usd=cost,
                billed_at=now,
                invoice_period=invoice_period,
            )
            session.add(record)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_record(record)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_billing_summary(
        self,
        tenant_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """获取账单汇总

        Args:
            tenant_id: 租户 ID
            period_start: 起始时间（默认 30 天前）
            period_end: 截止时间（默认当前时间）

        Returns:
            汇总信息: total_requests, total_tokens, total_cost, period_start, period_end
        """
        period_start, period_end = self._default_period(period_start, period_end)

        session = await self._get_session()
        try:
            stmt = (
                select(
                    func.count(BillingRecord.id).label("total_requests"),
                    func.coalesce(func.sum(BillingRecord.tokens_used), 0).label(
                        "total_tokens"
                    ),
                    func.coalesce(func.sum(BillingRecord.cost_usd), 0).label(
                        "total_cost"
                    ),
                )
                .where(
                    BillingRecord.tenant_id == tenant_id,
                    BillingRecord.billed_at >= period_start,
                    BillingRecord.billed_at <= period_end,
                )
            )
            result = await session.execute(stmt)
            row = result.one()

            return {
                "tenant_id": tenant_id,
                "total_requests": row.total_requests or 0,
                "total_tokens": int(row.total_tokens or 0),
                "total_cost": round(float(row.total_cost or 0), 6),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_billing_by_user(
        self,
        tenant_id: str,
        user_id: Optional[str] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """按用户汇总计费

        Args:
            tenant_id: 租户 ID
            user_id: 可选，指定用户 ID 过滤（不传则返回所有用户）
            period_start: 起始时间
            period_end: 截止时间

        Returns:
            按用户聚合的计费列表
        """
        period_start, period_end = self._default_period(period_start, period_end)

        session = await self._get_session()
        try:
            stmt = (
                select(
                    BillingRecord.user_id,
                    func.count(BillingRecord.id).label("request_count"),
                    func.coalesce(
                        func.sum(BillingRecord.tokens_used), 0
                    ).label("total_tokens"),
                    func.coalesce(
                        func.sum(BillingRecord.cost_usd), 0
                    ).label("total_cost"),
                )
                .where(
                    BillingRecord.tenant_id == tenant_id,
                    BillingRecord.billed_at >= period_start,
                    BillingRecord.billed_at <= period_end,
                )
                .group_by(BillingRecord.user_id)
                .order_by(func.sum(BillingRecord.cost_usd).desc())
            )
            if user_id is not None:
                stmt = stmt.where(BillingRecord.user_id == user_id)

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "user_id": row.user_id,
                    "request_count": row.request_count,
                    "total_tokens": int(row.total_tokens or 0),
                    "total_cost": round(float(row.total_cost or 0), 6),
                }
                for row in rows
            ]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_billing_by_endpoint(
        self,
        tenant_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """按端点汇总计费

        Args:
            tenant_id: 租户 ID
            period_start: 起始时间
            period_end: 截止时间

        Returns:
            按端点聚合的计费列表
        """
        period_start, period_end = self._default_period(period_start, period_end)

        session = await self._get_session()
        try:
            stmt = (
                select(
                    BillingRecord.api_endpoint,
                    BillingRecord.method,
                    func.count(BillingRecord.id).label("request_count"),
                    func.coalesce(
                        func.sum(BillingRecord.tokens_used), 0
                    ).label("total_tokens"),
                    func.coalesce(
                        func.sum(BillingRecord.cost_usd), 0
                    ).label("total_cost"),
                )
                .where(
                    BillingRecord.tenant_id == tenant_id,
                    BillingRecord.billed_at >= period_start,
                    BillingRecord.billed_at <= period_end,
                )
                .group_by(BillingRecord.api_endpoint, BillingRecord.method)
                .order_by(func.sum(BillingRecord.cost_usd).desc())
            )

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "api_endpoint": row.api_endpoint,
                    "method": row.method,
                    "request_count": row.request_count,
                    "total_tokens": int(row.total_tokens or 0),
                    "total_cost": round(float(row.total_cost or 0), 6),
                }
                for row in rows
            ]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def export_billing(
        self,
        tenant_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        format: str = "json",
    ) -> Tuple[str, bytes]:
        """导出账单数据

        Args:
            tenant_id: 租户 ID
            period_start: 起始时间
            period_end: 截止时间
            format: 导出格式 csv / json

        Returns:
            (filename, content_bytes) 元组
        """
        period_start, period_end = self._default_period(period_start, period_end)
        format = format.lower()

        session = await self._get_session()
        try:
            stmt = (
                select(BillingRecord)
                .where(
                    BillingRecord.tenant_id == tenant_id,
                    BillingRecord.billed_at >= period_start,
                    BillingRecord.billed_at <= period_end,
                )
                .order_by(BillingRecord.billed_at.desc())
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
        finally:
            if self._owns_session:
                await self._close_if_owned()

        # 日期范围用于文件名
        date_range = (
            f"{period_start.strftime('%Y%m%d')}-{period_end.strftime('%Y%m%d')}"
        )

        if format == "csv":
            # 生成 CSV
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    "id",
                    "tenant_id",
                    "user_id",
                    "api_endpoint",
                    "method",
                    "tokens_used",
                    "cost_usd",
                    "billed_at",
                    "invoice_period",
                ]
            )
            for record in records:
                writer.writerow(
                    [
                        record.id,
                        record.tenant_id,
                        record.user_id,
                        record.api_endpoint,
                        record.method,
                        record.tokens_used,
                        f"{record.cost_usd:.6f}",
                        record.billed_at.isoformat() if record.billed_at else "",
                        record.invoice_period,
                    ]
                )
            filename = f"billing_{tenant_id}_{date_range}.csv"
            content = output.getvalue().encode("utf-8")
            # 添加 BOM 以便 Excel 正确识别 UTF-8
            content = b"\xef\xbb\xbf" + content
            return filename, content

        else:
            # 默认 JSON 格式
            data = {
                "tenant_id": tenant_id,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "total_records": len(records),
                "records": [self._serialize_record(r) for r in records],
            }
            filename = f"billing_{tenant_id}_{date_range}.json"
            content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            return filename, content

    @staticmethod
    def _default_period(
        period_start: Optional[datetime],
        period_end: Optional[datetime],
    ) -> Tuple[datetime, datetime]:
        """填充默认时间范围（默认最近 30 天）"""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        if period_end is None:
            period_end = now
        if period_start is None:
            period_start = now - timedelta(days=30)
        return period_start, period_end

    @staticmethod
    def _serialize_record(record: BillingRecord) -> Dict[str, Any]:
        """序列化 BillingRecord 为 dict"""
        return {
            "id": record.id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "api_endpoint": record.api_endpoint,
            "method": record.method,
            "tokens_used": record.tokens_used,
            "cost_usd": round(record.cost_usd, 6),
            "billed_at": record.billed_at.isoformat() if record.billed_at else None,
            "invoice_period": record.invoice_period,
        }
