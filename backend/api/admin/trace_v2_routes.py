"""原生链路追踪 Admin API V2（WS-1）

路由前缀: /api/v1/admin/traces
权限: Role.ADMIN (router 级 dependencies)

对标 Langfuse Traces / LangSmith Runs，完整端点 (6 个):
- GET    /            - trace 列表（多维过滤 + 分页，最新优先）
- GET    /stats       - 统计聚合（总量/错误率/P50·P95·P99/token/成本/每小时趋势）
- GET    /cost        - 成本分解（按 model / user / day / kind 分组）
- GET    /export      - 导出（CSV / JSON，与列表同过滤条件）
- GET    /{trace_id}  - 瀑布图详情（span 嵌套树 + 相对起始偏移）
- DELETE /{trace_id}  - 删除 trace 及其 span（审计留痕）

路由顺序注意: /stats、/cost、/export 三个静态路径必须声明在 /{trace_id} 之前，
否则 FastAPI 会把它们当成 trace_id 路径参数吞掉。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.trace_models import STATUS_ERROR, SpanRecord, TraceRecord

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/traces",
    tags=["admin-traces-v2"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# 合法的成本分组维度
_COST_GROUP_BY = {"model", "user", "day", "kind"}
# 列表 / 导出单页上限（导出放宽到 10000，避免大批量导出被静默截断而无感知）
_MAX_PAGE_SIZE = 200
_MAX_EXPORT_ROWS = 10000


# ============================================================
# Pydantic 响应模型
# ============================================================


class TraceListItem(BaseModel):
    """trace 列表行"""

    trace_id: str
    tenant_id: Optional[str] = None
    name: str
    kind: str
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    total_spans: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost: float = 0.0
    error: Optional[str] = None
    tags: Optional[List[str]] = None


class TraceListResponse(BaseModel):
    """分页列表响应"""

    items: List[TraceListItem]
    total: int
    page: int
    page_size: int


class SpanNode(BaseModel):
    """瀑布图节点（自引用嵌套）"""

    span_id: str
    parent_span_id: Optional[str] = None
    name: str
    kind: str
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    # 相对 trace 起点的偏移（毫秒），前端据此直接画甘特条，无需二次计算
    relative_start_ms: float = 0.0
    model: Optional[str] = None
    provider: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    error: Optional[str] = None
    input: Optional[Any] = None
    output: Optional[Any] = None
    attributes: Optional[Dict[str, Any]] = None
    children: List["SpanNode"] = Field(default_factory=list)


SpanNode.model_rebuild()


class TraceDetailResponse(BaseModel):
    """瀑布图详情"""

    trace: TraceListItem
    spans: List[SpanNode]
    span_count: int
    # 瀑布图总宽度基准（毫秒）：trace.duration_ms 缺失时用 span 最大结束偏移兜底
    timeline_ms: float


class TraceStatsResponse(BaseModel):
    """统计聚合"""

    total_traces: int
    error_traces: int
    error_rate: float
    p50_duration_ms: float
    p95_duration_ms: float
    p99_duration_ms: float
    avg_duration_ms: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost: float
    traces_per_hour: List[Dict[str, Any]]


class CostGroupItem(BaseModel):
    """成本分组行"""

    group: str
    traces: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float


class CostBreakdownResponse(BaseModel):
    """成本分解"""

    group_by: str
    items: List[CostGroupItem]
    total_cost: float
    currency: str = "USD"


# ============================================================
# 内部工具
# ============================================================


def _parse_datetime(value: str, field_name: str) -> datetime:
    """解析 ISO 8601 日期时间字符串，失败时抛 422。

    与 analytics_v2_routes._parse_datetime 行为一致：兼容 Z 后缀、无时区、纯日期。
    """
    raw = value.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            return datetime.fromisoformat(raw + "T00:00:00+00:00")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field_name} 格式无效，需 ISO 8601（如 2026-07-01T00:00:00Z）",
            )


def _build_filters(
    tenant_id: str,
    *,
    kind: Optional[str],
    trace_status: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
    model: Optional[str],
    min_duration_ms: Optional[float],
    start_time: Optional[str],
    end_time: Optional[str],
    q: Optional[str],
) -> List[Any]:
    """把查询参数翻译为 SQLAlchemy 条件列表（列表 / 导出 / 统计共用）。

    ``model`` 是 span 上的字段，通过 EXISTS 子查询下推，避免 join 造成行重复。
    """
    conditions: List[Any] = [TraceRecord.tenant_id == tenant_id]
    if kind:
        conditions.append(TraceRecord.kind == kind)
    if trace_status:
        conditions.append(TraceRecord.status == trace_status)
    if user_id:
        conditions.append(TraceRecord.user_id == user_id)
    if session_id:
        conditions.append(TraceRecord.session_id == session_id)
    if min_duration_ms is not None:
        conditions.append(TraceRecord.duration_ms >= float(min_duration_ms))
    if start_time:
        conditions.append(
            TraceRecord.started_at >= _parse_datetime(start_time, "start_time")
        )
    if end_time:
        conditions.append(
            TraceRecord.started_at <= _parse_datetime(end_time, "end_time")
        )
    if q:
        conditions.append(TraceRecord.name.ilike(f"%{q.strip()}%"))
    if model:
        conditions.append(
            select(SpanRecord.id)
            .where(
                SpanRecord.trace_id == TraceRecord.trace_id,
                SpanRecord.model == model,
            )
            .exists()
        )
    return conditions


def _trace_to_item(trace: TraceRecord) -> TraceListItem:
    tags = trace.tags if isinstance(trace.tags, list) else None
    return TraceListItem(
        trace_id=trace.trace_id,
        tenant_id=trace.tenant_id,
        name=trace.name,
        kind=trace.kind,
        status=trace.status,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        duration_ms=trace.duration_ms,
        user_id=trace.user_id,
        session_id=trace.session_id,
        total_spans=trace.total_spans or 0,
        total_prompt_tokens=trace.total_prompt_tokens or 0,
        total_completion_tokens=trace.total_completion_tokens or 0,
        total_cost=float(trace.total_cost or 0.0),
        error=trace.error,
        tags=tags,
    )


def _percentile(sorted_values: List[float], p: float) -> float:
    """线性插值分位数（与 AnalyticsServiceV2._compute_latency_stats 同算法）。

    SQLite 不原生支持 PERCENTILE，故在 Python 侧对全量样本排序计算。
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[int(f)] * (c - k) + sorted_values[int(c)] * (k - f)


def build_waterfall(
    trace: TraceRecord, spans: List[SpanRecord]
) -> tuple[List[SpanNode], float]:
    """把扁平 span 列表组装成嵌套瀑布图。

    - 以 ``parent_span_id`` 建父子关系；父节点缺失（被采样丢弃 / 队列溢出）的
      孤儿 span 提升为根节点，保证不会静默丢数据；
    - ``relative_start_ms`` 相对 trace 起点计算，前端可直接按
      ``left = relative_start_ms / timeline_ms`` 画甘特条；
    - 存在环（异常数据）时按首次访问顺序打断，避免无限递归。

    Returns:
        (根节点列表, 时间轴总长度 ms)
    """
    origin = trace.started_at
    if origin is None and spans:
        origin = min(s.started_at for s in spans if s.started_at is not None)

    def _offset(dt: Optional[datetime]) -> float:
        if dt is None or origin is None:
            return 0.0
        return max(0.0, (dt - origin).total_seconds() * 1000.0)

    nodes: Dict[str, SpanNode] = {}
    ordered: List[SpanRecord] = sorted(
        spans, key=lambda s: (s.started_at or origin or datetime.min, s.id or 0)
    )
    for s in ordered:
        nodes[s.span_id] = SpanNode(
            span_id=s.span_id,
            parent_span_id=s.parent_span_id,
            name=s.name,
            kind=s.kind,
            status=s.status,
            started_at=s.started_at,
            ended_at=s.ended_at,
            duration_ms=s.duration_ms,
            relative_start_ms=round(_offset(s.started_at), 3),
            model=s.model,
            provider=s.provider,
            prompt_tokens=s.prompt_tokens or 0,
            completion_tokens=s.completion_tokens or 0,
            total_tokens=s.total_tokens or 0,
            cost=float(s.cost or 0.0),
            error=s.error,
            input=s.input,
            output=s.output,
            attributes=s.attributes if isinstance(s.attributes, dict) else None,
        )

    roots: List[SpanNode] = []
    attached: set[str] = set()
    for s in ordered:
        node = nodes[s.span_id]
        parent_id = s.parent_span_id
        # 父节点不存在（孤儿）或自引用/成环 → 提升为根，不丢数据
        if parent_id and parent_id in nodes and parent_id != s.span_id:
            if _is_descendant(nodes, parent_id, s.span_id):
                logger.warning(
                    "trace %s 的 span %s 存在父子环，提升为根节点",
                    trace.trace_id,
                    s.span_id,
                )
                roots.append(node)
            else:
                nodes[parent_id].children.append(node)
                attached.add(s.span_id)
        else:
            if parent_id and parent_id not in nodes:
                logger.debug(
                    "trace %s 的 span %s 父节点 %s 缺失，提升为根节点",
                    trace.trace_id,
                    s.span_id,
                    parent_id,
                )
            roots.append(node)

    # 时间轴长度：优先用 trace 自身耗时，缺失时取 span 最大结束偏移
    timeline = float(trace.duration_ms or 0.0)
    for s in ordered:
        end_offset = _offset(s.ended_at) if s.ended_at else _offset(s.started_at)
        timeline = max(timeline, end_offset)
    return roots, round(timeline, 3)


def _is_descendant(
    nodes: Dict[str, SpanNode], candidate_id: str, ancestor_id: str
) -> bool:
    """判断 candidate 是否已经在 ancestor 的子树中（成环检测）。"""
    stack = [nodes[ancestor_id]] if ancestor_id in nodes else []
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node.span_id in seen:
            continue
        seen.add(node.span_id)
        if node.span_id == candidate_id:
            return True
        stack.extend(node.children)
    return False


# ============================================================
# 1. trace 列表
# ============================================================


@router.get("", response_model=TraceListResponse, summary="trace 列表")
@router.get("/", response_model=TraceListResponse, include_in_schema=False)
async def list_traces(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    kind: Optional[str] = Query(default=None, description="链路类型: chat/evaluation/workflow/agent"),
    trace_status: Optional[str] = Query(
        default=None, alias="status", description="状态: running/success/error"
    ),
    user_id: Optional[str] = Query(default=None, description="发起用户 ID"),
    session_id: Optional[str] = Query(default=None, description="会话 ID"),
    model: Optional[str] = Query(default=None, description="链路中出现过的模型名"),
    min_duration_ms: Optional[float] = Query(
        default=None, ge=0, description="最小耗时（毫秒），用于筛慢链路"
    ),
    start_time: Optional[str] = Query(default=None, description="起始时间（ISO 8601）"),
    end_time: Optional[str] = Query(default=None, description="结束时间（ISO 8601）"),
    q: Optional[str] = Query(default=None, description="按 trace 名称模糊搜索"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE, description="每页条数"),
):
    """分页查询 trace 列表，按开始时间倒序（最新优先）"""
    conditions = _build_filters(
        tenant_id,
        kind=kind,
        trace_status=trace_status,
        user_id=user_id,
        session_id=session_id,
        model=model,
        min_duration_ms=min_duration_ms,
        start_time=start_time,
        end_time=end_time,
        q=q,
    )

    total = (
        await session.execute(
            select(func.count()).select_from(TraceRecord).where(*conditions)
        )
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(TraceRecord)
                .where(*conditions)
                .order_by(TraceRecord.started_at.desc(), TraceRecord.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return TraceListResponse(
        items=[_trace_to_item(r) for r in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


# ============================================================
# 2. 统计聚合（必须声明在 /{trace_id} 之前）
# ============================================================


@router.get("/stats", response_model=TraceStatsResponse, summary="trace 统计聚合")
async def trace_stats(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    kind: Optional[str] = Query(default=None, description="链路类型"),
    trace_status: Optional[str] = Query(default=None, alias="status", description="状态"),
    user_id: Optional[str] = Query(default=None, description="发起用户 ID"),
    session_id: Optional[str] = Query(default=None, description="会话 ID"),
    model: Optional[str] = Query(default=None, description="链路中出现过的模型名"),
    min_duration_ms: Optional[float] = Query(default=None, ge=0, description="最小耗时"),
    start_time: Optional[str] = Query(default=None, description="起始时间（ISO 8601）"),
    end_time: Optional[str] = Query(default=None, description="结束时间（ISO 8601）"),
    q: Optional[str] = Query(default=None, description="按 trace 名称模糊搜索"),
):
    """总量 / 错误率 / 耗时分位 / token / 成本 / 每小时趋势

    分位数在 Python 侧计算（SQLite 无 PERCENTILE），因此只取 duration 列，
    不 SELECT 整行，避免大结果集把内存打满。
    """
    conditions = _build_filters(
        tenant_id,
        kind=kind,
        trace_status=trace_status,
        user_id=user_id,
        session_id=session_id,
        model=model,
        min_duration_ms=min_duration_ms,
        start_time=start_time,
        end_time=end_time,
        q=q,
    )

    agg = (
        await session.execute(
            select(
                func.count(TraceRecord.id),
                func.coalesce(func.sum(TraceRecord.total_prompt_tokens), 0),
                func.coalesce(func.sum(TraceRecord.total_completion_tokens), 0),
                func.coalesce(func.sum(TraceRecord.total_cost), 0.0),
            ).where(*conditions)
        )
    ).one()
    total_traces = int(agg[0] or 0)
    prompt_tokens = int(agg[1] or 0)
    completion_tokens = int(agg[2] or 0)
    total_cost = float(agg[3] or 0.0)

    error_traces = int(
        (
            await session.execute(
                select(func.count(TraceRecord.id)).where(
                    *conditions, TraceRecord.status == STATUS_ERROR
                )
            )
        ).scalar_one()
        or 0
    )

    durations = sorted(
        float(r[0])
        for r in (
            await session.execute(
                select(TraceRecord.duration_ms).where(
                    *conditions, TraceRecord.duration_ms.is_not(None)
                )
            )
        ).all()
        if r[0] is not None
    )

    # 每小时趋势：DB 侧按 'YYYY-MM-DD HH' 截断分组，避免把全量行拉到内存
    hour_expr = func.strftime("%Y-%m-%dT%H:00:00", TraceRecord.started_at)
    try:
        hour_rows = (
            await session.execute(
                select(
                    hour_expr.label("bucket"),
                    func.count(TraceRecord.id),
                    func.coalesce(func.sum(TraceRecord.total_cost), 0.0),
                )
                .where(*conditions)
                .group_by("bucket")
                .order_by("bucket")
            )
        ).all()
        traces_per_hour = [
            {
                "hour": row[0],
                "traces": int(row[1] or 0),
                "cost": round(float(row[2] or 0.0), 6),
            }
            for row in hour_rows
            if row[0]
        ]
    except Exception as exc:
        # strftime 是 SQLite 方言函数；切到 PostgreSQL 后由 date_trunc 承担，
        # 此处降级为空序列并告警，不让整个统计接口 500
        logger.warning("每小时趋势聚合失败，返回空序列: %s", exc, exc_info=True)
        traces_per_hour = []

    return TraceStatsResponse(
        total_traces=total_traces,
        error_traces=error_traces,
        error_rate=round(error_traces / total_traces, 6) if total_traces else 0.0,
        p50_duration_ms=round(_percentile(durations, 0.50), 3),
        p95_duration_ms=round(_percentile(durations, 0.95), 3),
        p99_duration_ms=round(_percentile(durations, 0.99), 3),
        avg_duration_ms=(
            round(sum(durations) / len(durations), 3) if durations else 0.0
        ),
        total_prompt_tokens=prompt_tokens,
        total_completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        total_cost=round(total_cost, 6),
        traces_per_hour=traces_per_hour,
    )


# ============================================================
# 3. 成本分解（必须声明在 /{trace_id} 之前）
# ============================================================


@router.get("/cost", response_model=CostBreakdownResponse, summary="trace 成本分解")
async def trace_cost(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    group_by: str = Query(default="model", description="分组维度: model/user/day/kind"),
    start_time: Optional[str] = Query(default=None, description="起始时间（ISO 8601）"),
    end_time: Optional[str] = Query(default=None, description="结束时间（ISO 8601）"),
):
    """按模型 / 用户 / 日期 / 链路类型分解成本

    ``model`` 维度的成本来自 span 表（成本记在 span 上），其余维度来自 trace 表。
    """
    if group_by not in _COST_GROUP_BY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的 group_by: {group_by}, 可选: {sorted(_COST_GROUP_BY)}",
        )

    if group_by == "model":
        conditions: List[Any] = [SpanRecord.tenant_id == tenant_id]
        if start_time:
            conditions.append(
                SpanRecord.started_at >= _parse_datetime(start_time, "start_time")
            )
        if end_time:
            conditions.append(
                SpanRecord.started_at <= _parse_datetime(end_time, "end_time")
            )
        group_col = func.coalesce(SpanRecord.model, "unknown")
        rows = (
            await session.execute(
                select(
                    group_col.label("grp"),
                    func.count(func.distinct(SpanRecord.trace_id)),
                    func.coalesce(func.sum(SpanRecord.prompt_tokens), 0),
                    func.coalesce(func.sum(SpanRecord.completion_tokens), 0),
                    func.coalesce(func.sum(SpanRecord.cost), 0.0),
                )
                .where(*conditions)
                .group_by("grp")
                .order_by(func.coalesce(func.sum(SpanRecord.cost), 0.0).desc())
            )
        ).all()
    else:
        conditions = [TraceRecord.tenant_id == tenant_id]
        if start_time:
            conditions.append(
                TraceRecord.started_at >= _parse_datetime(start_time, "start_time")
            )
        if end_time:
            conditions.append(
                TraceRecord.started_at <= _parse_datetime(end_time, "end_time")
            )
        if group_by == "user":
            group_col = func.coalesce(TraceRecord.user_id, "anonymous")
        elif group_by == "kind":
            group_col = func.coalesce(TraceRecord.kind, "unknown")
        else:  # day
            group_col = func.strftime("%Y-%m-%d", TraceRecord.started_at)
        rows = (
            await session.execute(
                select(
                    group_col.label("grp"),
                    func.count(TraceRecord.id),
                    func.coalesce(func.sum(TraceRecord.total_prompt_tokens), 0),
                    func.coalesce(func.sum(TraceRecord.total_completion_tokens), 0),
                    func.coalesce(func.sum(TraceRecord.total_cost), 0.0),
                )
                .where(*conditions)
                .group_by("grp")
                .order_by("grp")
            )
        ).all()

    items = [
        CostGroupItem(
            group=str(row[0]) if row[0] is not None else "unknown",
            traces=int(row[1] or 0),
            prompt_tokens=int(row[2] or 0),
            completion_tokens=int(row[3] or 0),
            total_tokens=int(row[2] or 0) + int(row[3] or 0),
            cost=round(float(row[4] or 0.0), 6),
        )
        for row in rows
    ]
    return CostBreakdownResponse(
        group_by=group_by,
        items=items,
        total_cost=round(sum(i.cost for i in items), 6),
    )


# ============================================================
# 4. 导出（必须声明在 /{trace_id} 之前）
# ============================================================


@router.get("/export", summary="导出 trace（CSV / JSON）")
async def export_traces(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    export_format: str = Query(
        default="csv", alias="format", description="导出格式: csv / json"
    ),
    kind: Optional[str] = Query(default=None, description="链路类型"),
    trace_status: Optional[str] = Query(default=None, alias="status", description="状态"),
    user_id: Optional[str] = Query(default=None, description="发起用户 ID"),
    session_id: Optional[str] = Query(default=None, description="会话 ID"),
    model: Optional[str] = Query(default=None, description="链路中出现过的模型名"),
    min_duration_ms: Optional[float] = Query(default=None, ge=0, description="最小耗时"),
    start_time: Optional[str] = Query(default=None, description="起始时间（ISO 8601）"),
    end_time: Optional[str] = Query(default=None, description="结束时间（ISO 8601）"),
    q: Optional[str] = Query(default=None, description="按 trace 名称模糊搜索"),
):
    """按列表相同的过滤条件导出 trace

    前端以 blob 方式接收，因此统一返回带 Content-Disposition 的附件响应。
    单次最多导出 _MAX_EXPORT_ROWS 行，被截断时通过响应头 X-Export-Truncated 显式告知。
    """
    fmt = export_format.strip().lower()
    if fmt not in {"csv", "json"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format 只能是 csv 或 json",
        )

    conditions = _build_filters(
        tenant_id,
        kind=kind,
        trace_status=trace_status,
        user_id=user_id,
        session_id=session_id,
        model=model,
        min_duration_ms=min_duration_ms,
        start_time=start_time,
        end_time=end_time,
        q=q,
    )
    total = (
        await session.execute(
            select(func.count()).select_from(TraceRecord).where(*conditions)
        )
    ).scalar_one() or 0
    rows = (
        (
            await session.execute(
                select(TraceRecord)
                .where(*conditions)
                .order_by(TraceRecord.started_at.desc(), TraceRecord.id.desc())
                .limit(_MAX_EXPORT_ROWS)
            )
        )
        .scalars()
        .all()
    )
    truncated = int(total) > len(rows)
    if truncated:
        logger.warning(
            "trace 导出被截断: 命中 %d 行，仅导出 %d 行（tenant=%s）",
            total,
            len(rows),
            tenant_id,
        )

    fields = [
        "trace_id",
        "name",
        "kind",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "user_id",
        "session_id",
        "total_spans",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cost",
        "error",
    ]
    dict_rows: List[Dict[str, Any]] = []
    for r in rows:
        item = _trace_to_item(r).model_dump()
        item["started_at"] = r.started_at.isoformat() if r.started_at else ""
        item["ended_at"] = r.ended_at.isoformat() if r.ended_at else ""
        dict_rows.append(item)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if fmt == "json":
        body = json.dumps(
            {
                "traces": dict_rows,
                "total": int(total),
                "exported": len(dict_rows),
                "truncated": truncated,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        media_type = "application/json; charset=utf-8"
        filename = f"traces_{stamp}.json"
    else:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in dict_rows:
            writer.writerow({k: row.get(k, "") for k in fields})
        # BOM 前缀: 保证 Excel 直接打开中文不乱码
        body = "\ufeff" + buffer.getvalue()
        media_type = "text/csv; charset=utf-8"
        filename = f"traces_{stamp}.csv"

    return Response(
        content=body.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Truncated": "1" if truncated else "0",
        },
    )


# ============================================================
# 5. 瀑布图详情（动态路径，必须在静态路径之后）
# ============================================================


@router.get("/{trace_id}", response_model=TraceDetailResponse, summary="trace 瀑布图详情")
async def get_trace_detail(
    trace_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """返回 trace 及其 span 组装成的嵌套树，每个 span 附带相对起始偏移"""
    trace = (
        await session.execute(
            select(TraceRecord).where(
                TraceRecord.trace_id == trace_id,
                TraceRecord.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"trace {trace_id} 不存在"
        )

    spans = (
        (
            await session.execute(
                select(SpanRecord)
                .where(SpanRecord.trace_id == trace_id)
                .order_by(SpanRecord.started_at.asc(), SpanRecord.id.asc())
            )
        )
        .scalars()
        .all()
    )
    roots, timeline_ms = build_waterfall(trace, list(spans))
    return TraceDetailResponse(
        trace=_trace_to_item(trace),
        spans=roots,
        span_count=len(spans),
        timeline_ms=timeline_ms,
    )


# ============================================================
# 6. 删除 trace（审计留痕）
# ============================================================


@router.delete("/{trace_id}", response_model=Dict[str, Any], summary="删除 trace")
async def delete_trace(
    trace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    current_user_id: str = Depends(get_current_user_id),
    audit_service=Depends(get_audit_service),
):
    """删除单条 trace 及其全部 span（不可恢复，写审计日志）"""
    trace = (
        await session.execute(
            select(TraceRecord).where(
                TraceRecord.trace_id == trace_id,
                TraceRecord.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"trace {trace_id} 不存在"
        )

    span_result = await session.execute(
        delete(SpanRecord).where(SpanRecord.trace_id == trace_id)
    )
    await session.delete(trace)
    await session.flush()

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_trace",
        details={
            "trace_id": trace_id,
            "name": trace.name,
            "kind": trace.kind,
            "deleted_spans": int(span_result.rowcount or 0),
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    return {
        "success": True,
        "trace_id": trace_id,
        "deleted_spans": int(span_result.rowcount or 0),
    }
