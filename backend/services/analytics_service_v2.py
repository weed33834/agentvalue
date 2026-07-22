"""
会话分析服务 V2

对标 Langfuse Token 分析 / Dashboard：基于 conversation_metrics 表做多维聚合统计，
包括 Token 趋势、延迟分位（P50/P95/P99）、错误率、成本分解与异常用量检测（Z-score）。

核心方法:
- record_metrics:        记录一次会话/LLM 调用度量
- get_token_trends:      Token 趋势聚合（按 day/model/user/agent 分组）
- get_latency_stats:     延迟分位统计（P50/P95/P99）
- get_error_rate:        错误率统计
- get_cost_breakdown:    成本分解（按 model/user/agent 分组）
- detect_anomalies:      异常用量检测（简单 Z-score）

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
from models.conversation_analytics import ConversationMetrics

logger = logging.getLogger(__name__)

# 异常检测 Z-score 阈值：|z| 超过此值视为异常
_ANOMALY_ZSCORE_THRESHOLD = 3.0
# 异常检测回溯窗口（天）
_ANOMALY_LOOKBACK_DAYS = 7


class AnalyticsServiceV2:
    """会话分析服务 V2

    支持两种使用模式:
    1. 路由层: AnalyticsServiceV2(session) 配合 get_db 依赖，事务由路由控制
    2. 内部调用: AnalyticsServiceV2() 无 session，内部自建会话并自动 commit
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
    # 记录度量
    # ============================================================

    async def record_metrics(
        self,
        conversation_id: str,
        user_id: Optional[str],
        agent_id: Optional[str],
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cost: float,
        latency_ms: Optional[float],
        status: str = "success",
        error_message: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录一次会话/LLM 调用度量。

        Args:
            conversation_id: 会话 ID
            user_id: 发起用户 ID
            agent_id: 处理 Agent ID
            model: 模型名
            input_tokens: 输入 token 数
            output_tokens: 输出 token 数
            cost: 成本（美元）
            latency_ms: 延迟（毫秒）
            status: 调用状态（success/error/timeout 等）
            error_message: 错误信息
            tenant_id: 租户 ID（未传则取当前上下文）

        Returns:
            记录的度量信息
        """
        tid = tenant_id or get_current_tenant()
        total = int(input_tokens) + int(output_tokens)
        session = await self._get_session()
        try:
            metric = ConversationMetrics(
                tenant_id=tid,
                conversation_id=conversation_id,
                user_id=user_id,
                agent_id=agent_id,
                model=model,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                total_tokens=total,
                cost=float(cost),
                latency_ms=float(latency_ms) if latency_ms is not None else None,
                status=status,
                error_message=error_message,
                timestamp=datetime.now(timezone.utc),
            )
            session.add(metric)
            await session.flush()
            await self._commit_if_owned()
            return self._serialize(metric)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 聚合统计
    # ============================================================

    async def get_token_trends(
        self,
        start_date: datetime,
        end_date: datetime,
        tenant_id: Optional[str] = None,
        group_by: str = "day",
    ) -> Dict[str, Any]:
        """Token 趋势聚合统计。

        Args:
            start_date: 起始时间（含）
            end_date: 结束时间（含）
            tenant_id: 租户 ID
            group_by: 分组维度 day/model/user/agent

        Returns:
            {"group_by": str, "start_date": ..., "end_date": ...,
             "totals": {...}, "series": [{"key": ..., "input_tokens": ..., ...}]}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            group_col = self._group_column(group_by, ConversationMetrics)
            stmt = (
                select(
                    group_col.label("key"),
                    func.sum(ConversationMetrics.input_tokens).label("input_tokens"),
                    func.sum(ConversationMetrics.output_tokens).label("output_tokens"),
                    func.sum(ConversationMetrics.total_tokens).label("total_tokens"),
                    func.sum(ConversationMetrics.cost).label("cost"),
                    func.count(ConversationMetrics.id).label("request_count"),
                )
                .where(
                    ConversationMetrics.tenant_id == tid,
                    ConversationMetrics.timestamp >= start_date,
                    ConversationMetrics.timestamp <= end_date,
                )
                .group_by(group_col)
                .order_by(group_col)
            )
            result = await session.execute(stmt)
            rows = result.all()

            series = []
            tot_input = tot_output = tot_total = req_count = 0
            tot_cost = 0.0
            for r in rows:
                key = self._format_key(r.key, group_by)
                it = int(r.input_tokens or 0)
                ot = int(r.output_tokens or 0)
                tt = int(r.total_tokens or 0)
                rc = int(r.request_count or 0)
                cc = float(r.cost or 0.0)
                series.append({
                    "key": key,
                    "input_tokens": it,
                    "output_tokens": ot,
                    "total_tokens": tt,
                    "cost": round(cc, 6),
                    "request_count": rc,
                })
                tot_input += it
                tot_output += ot
                tot_total += tt
                req_count += rc
                tot_cost += cc

            return {
                "group_by": group_by,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "totals": {
                    "input_tokens": tot_input,
                    "output_tokens": tot_output,
                    "total_tokens": tot_total,
                    "cost": round(tot_cost, 6),
                    "request_count": req_count,
                },
                "series": series,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_latency_stats(
        self,
        start_date: datetime,
        end_date: datetime,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """延迟分位统计（P50/P95/P99）。

        SQLite 不原生支持 PERCENTILE，故在 Python 侧对全量样本排序计算分位。
        数据量大时建议迁至 PostgreSQL + percentile_cont。

        Returns:
            {"count": int, "avg_ms": float, "p50_ms": float,
             "p95_ms": float, "p99_ms": float, "min_ms": float, "max_ms": float}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            stmt = (
                select(ConversationMetrics.latency_ms)
                .where(
                    ConversationMetrics.tenant_id == tid,
                    ConversationMetrics.timestamp >= start_date,
                    ConversationMetrics.timestamp <= end_date,
                    ConversationMetrics.latency_ms.is_not(None),
                )
            )
            result = await session.execute(stmt)
            latencies = [float(r[0]) for r in result.all() if r[0] is not None]
            return self._compute_latency_stats(latencies)
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_error_rate(
        self,
        start_date: datetime,
        end_date: datetime,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """错误率统计。

        status != 'success' 视为错误。

        Returns:
            {"total": int, "errors": int, "error_rate": float,
             "by_status": [{"status": str, "count": int}]}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            base_filter = (
                ConversationMetrics.tenant_id == tid,
                ConversationMetrics.timestamp >= start_date,
                ConversationMetrics.timestamp <= end_date,
            )
            # 总数
            total_result = await session.execute(
                select(func.count(ConversationMetrics.id)).where(*base_filter)
            )
            total = int(total_result.scalar() or 0)

            # 按状态分组
            status_stmt = (
                select(
                    ConversationMetrics.status.label("status"),
                    func.count(ConversationMetrics.id).label("count"),
                )
                .where(*base_filter)
                .group_by(ConversationMetrics.status)
            )
            status_result = await session.execute(status_stmt)
            by_status = [
                {"status": r.status, "count": int(r.count or 0)}
                for r in status_result.all()
            ]
            errors = sum(
                s["count"] for s in by_status if s["status"] != "success"
            )
            error_rate = (errors / total) if total else 0.0
            return {
                "total": total,
                "errors": errors,
                "error_rate": round(error_rate, 6),
                "by_status": by_status,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_cost_breakdown(
        self,
        start_date: datetime,
        end_date: datetime,
        tenant_id: Optional[str] = None,
        group_by: str = "model",
    ) -> Dict[str, Any]:
        """成本分解（按 model/user/agent 分组）。

        Returns:
            {"group_by": str, "total_cost": float, "total_tokens": int,
             "items": [{"key": ..., "cost": ..., "tokens": ..., "count": ...}]}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            group_col = self._group_column(group_by, ConversationMetrics)
            stmt = (
                select(
                    group_col.label("key"),
                    func.sum(ConversationMetrics.cost).label("cost"),
                    func.sum(ConversationMetrics.total_tokens).label("tokens"),
                    func.count(ConversationMetrics.id).label("count"),
                )
                .where(
                    ConversationMetrics.tenant_id == tid,
                    ConversationMetrics.timestamp >= start_date,
                    ConversationMetrics.timestamp <= end_date,
                )
                .group_by(group_col)
                .order_by(func.sum(ConversationMetrics.cost).desc())
            )
            result = await session.execute(stmt)
            items = []
            tot_cost = 0.0
            tot_tokens = 0
            for r in result.all():
                key = self._format_key(r.key, group_by)
                cc = float(r.cost or 0.0)
                tt = int(r.tokens or 0)
                items.append({
                    "key": key,
                    "cost": round(cc, 6),
                    "tokens": tt,
                    "count": int(r.count or 0),
                })
                tot_cost += cc
                tot_tokens += tt
            return {
                "group_by": group_by,
                "total_cost": round(tot_cost, 6),
                "total_tokens": tot_tokens,
                "items": items,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def detect_anomalies(
        self, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """异常用量检测（简单 Z-score）。

        对最近 _ANOMALY_LOOKBACK_DAYS 天内按天聚合的 total_tokens 做 Z-score 检测，
        |z| > _ANOMALY_ZSCORE_THRESHOLD 的日期标记为异常。

        Returns:
            {"lookback_days": int, "threshold": float, "mean": float,
             "stddev": float, "anomalies": [{"date": ..., "tokens": ..., "z_score": ...}]}
        """
        tid = tenant_id or get_current_tenant()
        session = await self._get_session()
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=_ANOMALY_LOOKBACK_DAYS)
            # 按天聚合 total_tokens
            day_col = func.strftime("%Y-%m-%d", ConversationMetrics.timestamp)
            stmt = (
                select(
                    day_col.label("day"),
                    func.sum(ConversationMetrics.total_tokens).label("tokens"),
                )
                .where(
                    ConversationMetrics.tenant_id == tid,
                    ConversationMetrics.timestamp >= start,
                )
                .group_by(day_col)
                .order_by(day_col)
            )
            result = await session.execute(stmt)
            rows = result.all()

            if not rows:
                return {
                    "lookback_days": _ANOMALY_LOOKBACK_DAYS,
                    "threshold": _ANOMALY_ZSCORE_THRESHOLD,
                    "mean": 0.0,
                    "stddev": 0.0,
                    "anomalies": [],
                }

            values = [float(r.tokens or 0) for r in rows]
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            stddev = math.sqrt(variance)

            anomalies: List[Dict[str, Any]] = []
            for r, v in zip(rows, values):
                if stddev > 0:
                    z = (v - mean) / stddev
                else:
                    z = 0.0
                if abs(z) > _ANOMALY_ZSCORE_THRESHOLD:
                    anomalies.append({
                        "date": r.day,
                        "tokens": int(v),
                        "z_score": round(z, 4),
                    })

            return {
                "lookback_days": _ANOMALY_LOOKBACK_DAYS,
                "threshold": _ANOMALY_ZSCORE_THRESHOLD,
                "mean": round(mean, 4),
                "stddev": round(stddev, 4),
                "anomalies": anomalies,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 内部工具
    # ============================================================

    @staticmethod
    def _group_column(group_by: str, model):
        """根据 group_by 返回对应的分组列表达式。"""
        if group_by == "model":
            return model.model
        if group_by == "user":
            return model.user_id
        if group_by == "agent":
            return model.agent_id
        # 默认按天
        return func.strftime("%Y-%m-%d", model.timestamp)

    @staticmethod
    def _format_key(key: Any, group_by: str) -> str:
        """格式化分组键为字符串。"""
        if key is None:
            return "unknown"
        return str(key)

    @staticmethod
    def _compute_latency_stats(latencies: List[float]) -> Dict[str, Any]:
        """在 Python 侧计算延迟分位统计。"""
        if not latencies:
            return {
                "count": 0,
                "avg_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
            }
        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)

        def _percentile(p: float) -> float:
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

        avg = sum(latencies_sorted) / n
        return {
            "count": n,
            "avg_ms": round(avg, 4),
            "p50_ms": round(_percentile(0.50), 4),
            "p95_ms": round(_percentile(0.95), 4),
            "p99_ms": round(_percentile(0.99), 4),
            "min_ms": round(latencies_sorted[0], 4),
            "max_ms": round(latencies_sorted[-1], 4),
        }

    @staticmethod
    def _serialize(metric: ConversationMetrics) -> Dict[str, Any]:
        return {
            "id": metric.id,
            "tenant_id": metric.tenant_id,
            "conversation_id": metric.conversation_id,
            "user_id": metric.user_id,
            "agent_id": metric.agent_id,
            "model": metric.model,
            "input_tokens": metric.input_tokens,
            "output_tokens": metric.output_tokens,
            "total_tokens": metric.total_tokens,
            "cost": metric.cost,
            "latency_ms": metric.latency_ms,
            "status": metric.status,
            "error_message": metric.error_message,
            "timestamp": metric.timestamp.isoformat() if metric.timestamp else None,
        }
