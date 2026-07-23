"""
API 健康监控服务

对标 Langfuse 延迟监控 / 告警系统：基于 api_health_metrics 与 slo_definitions 表
做端点级健康监控与 SLO 达成分析。

核心方法:
- record_request:      记录一次请求度量
- get_endpoint_stats:  端点统计（请求数/平均/P95 延迟/错误率）
- get_slo_status:      单个 SLO 达成状态
- get_all_slo_status:  所有启用 SLO 状态概览
- list_slos / create_slo / update_slo / delete_slo: SLO CRUD（全部 tenant_id 过滤）

事务边界: 传入 session 时由调用方控制 commit；未传入 session 时内部自建会话并 commit。
所有方法接受 tenant_id 参数并过滤查询。
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.tenant_context import get_current_tenant
from models.api_health import ApiHealthMetric, SloDefinition

logger = logging.getLogger(__name__)

# 视为成功的状态码下限（< 400 视为成功）
_SUCCESS_STATUS_THRESHOLD = 400


class ApiHealthService:
    """API 健康监控服务

    支持两种使用模式:
    1. 路由层: ApiHealthService(session) 配合 get_db 依赖，事务由路由控制
    2. 内部调用: ApiHealthService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        self._session = AsyncSessionLocal()
        self._owns_session = True
        return self._session

    async def _commit_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.commit()

    async def _close_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ============================================================
    # 记录请求
    # ============================================================

    async def record_request(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        response_time_ms: float,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录一次请求度量。

        Args:
            endpoint: API 端点路径
            method: HTTP 方法
            status_code: HTTP 状态码
            response_time_ms: 响应时间（毫秒）
            tenant_id: 租户 ID（未传则取当前上下文）

        Returns:
            记录的度量信息
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            metric = ApiHealthMetric(
                tenant_id=tid,
                endpoint=endpoint,
                method=method.upper(),
                status_code=int(status_code),
                response_time_ms=float(response_time_ms),
                timestamp=datetime.now(timezone.utc),
            )
            session.add(metric)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_metric(metric)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 端点统计
    # ============================================================

    async def get_endpoint_stats(
        self,
        endpoint: str,
        start_time: datetime,
        end_time: datetime,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """端点详细统计（请求数/平均/P95 延迟/错误率）。

        Args:
            endpoint: 端点路径
            start_time: 起始时间（含）
            end_time: 结束时间（含）
            tenant_id: 租户 ID

        Returns:
            {"endpoint": str, "start_time": ..., "end_time": ...,
             "request_count": int, "avg_latency_ms": float, "p95_latency_ms": float,
             "error_count": int, "error_rate": float,
             "by_method": [...], "by_status": [...]}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            base_filter = (
                ApiHealthMetric.tenant_id == tid,
                ApiHealthMetric.endpoint == endpoint,
                ApiHealthMetric.timestamp >= start_time,
                ApiHealthMetric.timestamp <= end_time,
            )

            # 总数 + 平均延迟
            agg_result = await session.execute(
                select(
                    func.count(ApiHealthMetric.id).label("count"),
                    func.avg(ApiHealthMetric.response_time_ms).label("avg"),
                    func.sum(ApiHealthMetric.response_time_ms).label("sum"),
                ).where(*base_filter)
            )
            agg = agg_result.one()
            count = int(agg.count or 0)
            avg = float(agg.avg or 0.0)

            # 错误数（状态码 >= 400）
            err_result = await session.execute(
                select(func.count(ApiHealthMetric.id)).where(
                    *base_filter,
                    ApiHealthMetric.status_code >= _SUCCESS_STATUS_THRESHOLD,
                )
            )
            error_count = int(err_result.scalar() or 0)

            # P95 延迟：拉取全量样本在 Python 侧计算（SQLite 无原生 percentile）
            p95 = 0.0
            if count > 0:
                lat_result = await session.execute(
                    select(ApiHealthMetric.response_time_ms).where(*base_filter)
                )
                latencies = [float(r[0]) for r in lat_result.all() if r[0] is not None]
                p95 = self._percentile(latencies, 0.95)

            # 按方法分组
            method_stmt = (
                select(
                    ApiHealthMetric.method.label("method"),
                    func.count(ApiHealthMetric.id).label("count"),
                    func.avg(ApiHealthMetric.response_time_ms).label("avg"),
                )
                .where(*base_filter)
                .group_by(ApiHealthMetric.method)
            )
            method_result = await session.execute(method_stmt)
            by_method = [
                {
                    "method": r.method,
                    "count": int(r.count or 0),
                    "avg_latency_ms": round(float(r.avg or 0.0), 4),
                }
                for r in method_result.all()
            ]

            # 按状态码分组
            status_stmt = (
                select(
                    ApiHealthMetric.status_code.label("status_code"),
                    func.count(ApiHealthMetric.id).label("count"),
                )
                .where(*base_filter)
                .group_by(ApiHealthMetric.status_code)
            )
            status_result = await session.execute(status_stmt)
            by_status = [
                {"status_code": int(r.status_code), "count": int(r.count or 0)}
                for r in status_result.all()
            ]

            error_rate = (error_count / count) if count else 0.0
            return {
                "endpoint": endpoint,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "request_count": count,
                "avg_latency_ms": round(avg, 4),
                "p95_latency_ms": round(p95, 4),
                "error_count": error_count,
                "error_rate": round(error_rate, 6),
                "by_method": by_method,
                "by_status": by_status,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_endpoints(
        self,
        tenant_id: Optional[str] = None,
        window_minutes: int = 5,
    ) -> List[Dict[str, Any]]:
        """列出当前租户近期有流量的端点及健康状态概览。"""
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=window_minutes)
            stmt = (
                select(
                    ApiHealthMetric.endpoint.label("endpoint"),
                    func.count(ApiHealthMetric.id).label("count"),
                    func.avg(ApiHealthMetric.response_time_ms).label("avg"),
                    func.max(ApiHealthMetric.response_time_ms).label("max"),
                    func.sum(
                        ApiHealthMetric.status_code >= _SUCCESS_STATUS_THRESHOLD
                    ).label("errors"),
                )
                .where(
                    ApiHealthMetric.tenant_id == tid,
                    ApiHealthMetric.timestamp >= start,
                )
                .group_by(ApiHealthMetric.endpoint)
                .order_by(func.count(ApiHealthMetric.id).desc())
            )
            result = await session.execute(stmt)
            endpoints = []
            for r in result.all():
                count = int(r.count or 0)
                errors = int(r.errors or 0)
                endpoints.append(
                    {
                        "endpoint": r.endpoint,
                        "request_count": count,
                        "avg_latency_ms": round(float(r.avg or 0.0), 4),
                        "max_latency_ms": round(float(r.max or 0.0), 4),
                        "error_count": errors,
                        "error_rate": round((errors / count) if count else 0.0, 6),
                        "window_minutes": window_minutes,
                    }
                )
            return endpoints
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # SLO
    # ============================================================

    async def create_slo(
        self,
        tenant_id: str,
        name: str,
        endpoint: str,
        target_latency_ms: float,
        target_success_rate: float = 0.99,
        window_minutes: int = 5,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """创建 SLO 定义。"""
        session = await self._get_session()
        try:
            slo = SloDefinition(
                tenant_id=tenant_id,
                name=name,
                endpoint=endpoint,
                target_latency_ms=float(target_latency_ms),
                target_success_rate=float(target_success_rate),
                window_minutes=int(window_minutes),
                enabled=enabled,
            )
            session.add(slo)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_slo(slo)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def update_slo(
        self, slo_id: int, tenant_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """更新 SLO 定义（仅本租户）。"""
        allowed = {
            "name",
            "endpoint",
            "target_latency_ms",
            "target_success_rate",
            "window_minutes",
            "enabled",
        }
        session = await self._get_session()
        try:
            slo = await self._get_slo_owned(session, slo_id, tenant_id)
            if slo is None:
                return None
            for key, value in kwargs.items():
                if key in allowed and value is not None:
                    setattr(slo, key, value)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize_slo(slo)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_slo(self, slo_id: int, tenant_id: str) -> bool:
        """删除 SLO 定义（仅本租户）。"""
        session = await self._get_session()
        try:
            slo = await self._get_slo_owned(session, slo_id, tenant_id)
            if slo is None:
                return False
            await session.delete(slo)
            await session.flush()
            await self._commit_if_owned()
            return True
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_slos(
        self, tenant_id: str, enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """列出当前租户的 SLO 定义。"""
        session = await self._get_session()
        try:
            stmt = (
                select(SloDefinition)
                .where(SloDefinition.tenant_id == tenant_id)
                .order_by(SloDefinition.id.asc())
            )
            if enabled_only:
                stmt = stmt.where(SloDefinition.enabled.is_(True))
            result = await session.execute(stmt)
            return [self._serialize_slo(s) for s in result.scalars().all()]
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_slo_status(
        self, slo_id: int, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """单个 SLO 达成状态。

        在 window_minutes 窗口内计算:
        - 实际 P95 延迟 vs target_latency_ms
        - 实际成功率 vs target_success_rate

        Returns:
            {"slo": {...}, "window_minutes": int, "request_count": int,
             "p95_latency_ms": float, "success_rate": float,
             "latency_met": bool, "success_rate_met": bool, "achieved": bool}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            slo = await self._get_slo_owned(session, slo_id, tid)
            if slo is None:
                return {"slo": None, "achieved": False, "error": "SLO 不存在"}
            return await self._compute_slo_status(session, slo, tid)
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_all_slo_status(
        self, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """所有启用 SLO 状态概览。"""
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            result = await session.execute(
                select(SloDefinition)
                .where(
                    SloDefinition.tenant_id == tid,
                    SloDefinition.enabled.is_(True),
                )
                .order_by(SloDefinition.id.asc())
            )
            slos = result.scalars().all()
            statuses = []
            achieved_count = 0
            for slo in slos:
                status = await self._compute_slo_status(session, slo, tid)
                if status.get("achieved"):
                    achieved_count += 1
                statuses.append(status)
            return {
                "tenant_id": tid,
                "total": len(slos),
                "achieved": achieved_count,
                "violated": len(slos) - achieved_count,
                "items": statuses,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 内部工具
    # ============================================================

    async def _compute_slo_status(
        self,
        session: AsyncSession,
        slo: SloDefinition,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """计算单个 SLO 的达成状态。"""
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=slo.window_minutes)
        base_filter = (
            ApiHealthMetric.tenant_id == tenant_id,
            ApiHealthMetric.endpoint == slo.endpoint,
            ApiHealthMetric.timestamp >= start,
        )

        # 总数 + 成功数
        agg_result = await session.execute(
            select(
                func.count(ApiHealthMetric.id).label("count"),
                func.sum(
                    ApiHealthMetric.status_code >= _SUCCESS_STATUS_THRESHOLD
                ).label("errors"),
            ).where(*base_filter)
        )
        agg = agg_result.one()
        count = int(agg.count or 0)
        errors = int(agg.errors or 0)
        success_rate = ((count - errors) / count) if count else 0.0

        # P95 延迟
        p95 = 0.0
        if count > 0:
            lat_result = await session.execute(
                select(ApiHealthMetric.response_time_ms).where(*base_filter)
            )
            latencies = [float(r[0]) for r in lat_result.all() if r[0] is not None]
            p95 = self._percentile(latencies, 0.95)

        latency_met = p95 <= slo.target_latency_ms if count else True
        success_rate_met = success_rate >= slo.target_success_rate if count else True
        achieved = bool(latency_met and success_rate_met)

        return {
            "slo": self._serialize_slo(slo),
            "window_minutes": slo.window_minutes,
            "request_count": count,
            "error_count": errors,
            "p95_latency_ms": round(p95, 4),
            "target_latency_ms": slo.target_latency_ms,
            "success_rate": round(success_rate, 6),
            "target_success_rate": slo.target_success_rate,
            "latency_met": latency_met,
            "success_rate_met": success_rate_met,
            "achieved": achieved,
        }

    @staticmethod
    async def _get_slo_owned(
        session: AsyncSession, slo_id: int, tenant_id: str
    ) -> Optional[SloDefinition]:
        result = await session.execute(
            select(SloDefinition).where(
                SloDefinition.id == slo_id,
                SloDefinition.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _percentile(latencies: List[float], p: float) -> float:
        """在 Python 侧计算分位数。"""
        if not latencies:
            return 0.0
        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)
        if n == 1:
            return latencies_sorted[0]
        k = (n - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return latencies_sorted[int(k)]
        d0 = latencies_sorted[int(f)] * (c - k)
        d1 = latencies_sorted[int(c)] * (k - f)
        return d0 + d1

    @staticmethod
    def _serialize_metric(metric: ApiHealthMetric) -> Dict[str, Any]:
        return {
            "id": metric.id,
            "tenant_id": metric.tenant_id,
            "endpoint": metric.endpoint,
            "method": metric.method,
            "status_code": metric.status_code,
            "response_time_ms": metric.response_time_ms,
            "timestamp": metric.timestamp.isoformat() if metric.timestamp else None,
        }

    @staticmethod
    def _serialize_slo(slo: SloDefinition) -> Dict[str, Any]:
        return {
            "id": slo.id,
            "tenant_id": slo.tenant_id,
            "name": slo.name,
            "endpoint": slo.endpoint,
            "target_latency_ms": slo.target_latency_ms,
            "target_success_rate": slo.target_success_rate,
            "window_minutes": slo.window_minutes,
            "enabled": slo.enabled,
            "created_at": slo.created_at.isoformat() if slo.created_at else None,
            "updated_at": slo.updated_at.isoformat() if slo.updated_at else None,
        }
