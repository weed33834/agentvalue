"""对外公共 API v1（WS-3）

路由前缀: ``/api/public/v1``
鉴权:     ``X-API-Key`` 请求头 + scope 校验（:func:`api.deps.require_api_key`），
          **不接受 JWT**。租户由 API Key 绑定，调用方无法跨租户读写。

端点清单::

    GET  /me                        身份自省（scopes / 租户 / 配额）
    POST /evaluations               发起一次评估（异步，返回 job_id）
    GET  /evaluations               评估列表（分页 + 状态过滤）
    GET  /evaluations/{id}          评估详情
    GET  /agents                    Agent 预设列表
    POST /agents/{id}/invoke        调用 Agent 做一次补全
    GET  /datasets                  数据集列表
    GET  /datasets/{id}/items       数据集条目（分页）
    GET  /traces                    调用链路列表（trace 模块缺失时 503）

路由顺序遵循「静态先于动态」：``/evaluations`` 声明在 ``/evaluations/{id}`` 之前，
避免 FastAPI 把静态段误匹配到路径参数。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import ApiKeyPrincipal, AppState, get_app_state, require_api_key
from core.database import get_db
from models.dataset_models import DatasetItem, EvaluationDataset
from models.prompt_template import AgentPreset
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public/v1", tags=["public-api-v1"])

# ---------------------------------------------------------------------------
# 每个 API Key 的进程内滑动窗口限流
# ---------------------------------------------------------------------------
# 说明：core/rate_limit.py 的 slowapi 装饰器按 IP 限流，无法读取 API Key 上的
# 每分钟配额；这里补一层按 key_id 的窗口计数。单进程内存实现，多副本部署时
# 实际配额为 N × rate_limit（WS-4 的 Redis 分布式限流落地后可替换本实现）。
_RATE_WINDOW_SECONDS = 60.0
_rate_buckets: Dict[str, Deque[float]] = {}
_RATE_BUCKET_MAX_KEYS = 5000


def _enforce_rate_limit(principal: ApiKeyPrincipal) -> None:
    """按 API Key 的 rate_limit_per_minute 做滑动窗口限流，超限抛 429。"""
    quota = principal.rate_limit_per_minute
    if quota <= 0:
        return
    now = time.monotonic()
    bucket = _rate_buckets.get(principal.key_id)
    if bucket is None:
        # 简单容量保护：key 数量爆表时清空最早写入的一半，避免无界增长
        if len(_rate_buckets) >= _RATE_BUCKET_MAX_KEYS:
            for stale in list(_rate_buckets)[: _RATE_BUCKET_MAX_KEYS // 2]:
                _rate_buckets.pop(stale, None)
        bucket = deque()
        _rate_buckets[principal.key_id] = bucket
    while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= quota:
        retry_after = max(1, int(_RATE_WINDOW_SECONDS - (now - bucket[0])) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"超出 API Key 限流配额（{quota} 次/分钟）",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)


def reset_rate_limit_buckets() -> None:
    """清空限流窗口（测试专用，避免用例之间互相污染）。"""
    _rate_buckets.clear()


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class PublicRawInput(BaseModel):
    """评估原始输入项"""

    content: str = Field(..., min_length=1, max_length=20000, description="输入正文")
    type: str = Field("daily_report", max_length=32, description="输入类型")
    input_id: Optional[str] = Field(None, max_length=64, description="幂等输入 ID")


class CreateEvaluationBody(BaseModel):
    """发起评估的请求体"""

    employee_id: str = Field(..., min_length=1, max_length=64)
    period: str = Field(..., min_length=1, max_length=32, description="评估周期，如 2026-Q1")
    raw_inputs: List[PublicRawInput] = Field(
        default_factory=list, description="留空则复用库中已有的该周期输入"
    )


class InvokeAgentBody(BaseModel):
    """调用 Agent 的请求体"""

    input: str = Field(..., min_length=1, max_length=20000, description="用户输入")
    context: Optional[str] = Field(
        None, max_length=20000, description="附加上下文，拼接在用户输入之前"
    )


def _iso(value: Optional[datetime]) -> Optional[str]:
    """datetime → ISO8601 字符串（naive 视为 UTC）"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ---------------------------------------------------------------------------
# 身份自省
# ---------------------------------------------------------------------------


@router.get("/me", summary="API Key 自省")
async def whoami(
    principal: ApiKeyPrincipal = Depends(require_api_key()),
) -> Dict[str, Any]:
    """返回当前 API Key 的身份、租户、scope 与限流配额。

    SDK 通常用它做连通性自检：能拿到 200 即代表 Key 有效且未过期。
    """
    _enforce_rate_limit(principal)
    return {
        "key_id": principal.key_id,
        "name": principal.name,
        "tenant_id": principal.tenant_id,
        "scopes": principal.scopes,
        "rate_limit_per_minute": principal.rate_limit_per_minute,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


@router.post("/evaluations", summary="发起一次评估")
async def create_evaluation(
    body: CreateEvaluationBody,
    background_tasks: BackgroundTasks,
    request: Request,
    principal: ApiKeyPrincipal = Depends(require_api_key("evaluations:write")),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
) -> Dict[str, Any]:
    """异步触发一次员工评估，立即返回 ``job_id``。

    复用 ``api/routes.py`` 的后台评估任务实现（同一套 LangGraph 与 job 队列），
    公共 API 侧只负责落 raw_input、入队与鉴权。
    评估完成后会触发 ``evaluation.completed`` 出站 Webhook。
    """
    _enforce_rate_limit(principal)
    # 延迟 import：api.routes 顶部依赖较重，且会反向 import api.deps
    from api.routes import _run_evaluation_job, job_queue

    eval_service = EvaluationService(session)
    await eval_service.ensure_user_exists(body.employee_id, role="employee")

    existing_period = await eval_service.get_period(body.period)
    if existing_period is not None and existing_period.status != "open":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"评估周期 {body.period} 已关闭，无法创建评估",
        )

    raw_inputs: List[Dict[str, Any]] = []
    for item in body.raw_inputs:
        input_id = item.input_id or f"input-{uuid.uuid4().hex[:8]}"
        if await eval_service.get_raw_input(input_id) is None:
            await eval_service.create_raw_input(
                {
                    "input_id": input_id,
                    "employee_id": body.employee_id,
                    "period": body.period,
                    "type": item.type,
                    "content": item.content,
                    "attachments": [],
                }
            )
        raw_inputs.append(
            {"input_id": input_id, "type": item.type, "content": item.content}
        )

    if not raw_inputs:
        stored = await eval_service.list_raw_inputs(
            employee_id=body.employee_id, period=body.period
        )
        raw_inputs = [
            {"input_id": i.input_id, "type": i.type, "content": i.content}
            for i in stored
        ]
    if not raw_inputs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="raw_inputs 为空且库中无该员工该周期的历史输入，无法发起评估",
        )

    await session.commit()

    job_id = f"job-{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    await job_queue.enqueue(
        job_id,
        {
            "job_id": job_id,
            "status": "pending",
            "employee_id": body.employee_id,
            "period": body.period,
            "created_at": now_iso,
            "updated_at": now_iso,
        },
    )
    background_tasks.add_task(
        _run_evaluation_job,
        job_id,
        body.employee_id,
        body.period,
        raw_inputs,
        app_state,
        principal.tenant_id,
        f"apikey:{principal.key_id}",
    )
    return {"job_id": job_id, "status": "pending", "period": body.period}


@router.get("/evaluations", summary="评估列表")
async def list_evaluations(
    employee_id: Optional[str] = Query(None, max_length=64),
    eval_status: Optional[str] = Query(None, alias="status", max_length=32),
    period: Optional[str] = Query(None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: ApiKeyPrincipal = Depends(require_api_key("evaluations:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页查询评估列表（自动限定在 API Key 所属租户内）。"""
    _enforce_rate_limit(principal)
    eval_service = EvaluationService(session)
    result = await eval_service.list_evaluations(
        employee_id=employee_id,
        status=eval_status,
        period=period,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [
            {
                "evaluation_id": row.evaluation_id,
                "employee_id": row.employee_id,
                "period": row.period,
                "overall_score": row.overall_score,
                "status": row.status,
                "created_at": _iso(row.created_at),
            }
            for row in result["items"]
        ],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/evaluations/{evaluation_id}", summary="评估详情")
async def get_evaluation(
    evaluation_id: str,
    principal: ApiKeyPrincipal = Depends(require_api_key("evaluations:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """按 ``evaluation_id`` 查询单条评估。

    只返回员工视角结论，manager_view / audit 属内部数据，不通过公共 API 暴露。
    """
    _enforce_rate_limit(principal)
    eval_service = EvaluationService(session)
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if evaluation is None or evaluation.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在"
        )
    return {
        "evaluation_id": evaluation.evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "overall_score": evaluation.overall_score,
        "status": evaluation.status,
        "employee_view": evaluation.employee_view,
        "created_at": _iso(evaluation.created_at),
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@router.get("/agents", summary="Agent 列表")
async def list_agents(
    category: Optional[str] = Query(None, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: ApiKeyPrincipal = Depends(require_api_key("agents:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """列出当前租户可用的 Agent 预设（含内置公共预设）。"""
    _enforce_rate_limit(principal)
    base = select(AgentPreset).where(
        (AgentPreset.tenant_id == principal.tenant_id)
        | (AgentPreset.tenant_id.is_(None))
    )
    if category:
        base = base.where(AgentPreset.category == category)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(AgentPreset.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "category": row.category,
                "tags": row.tags or [],
                "model_tier": row.model_tier,
                "is_builtin": bool(row.is_builtin),
                "use_count": row.use_count or 0,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/agents/{agent_id}/invoke", summary="调用 Agent")
async def invoke_agent(
    agent_id: int,
    body: InvokeAgentBody,
    principal: ApiKeyPrincipal = Depends(require_api_key("agents:invoke")),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
) -> Dict[str, Any]:
    """用指定 Agent 预设的 system_prompt 执行一次非流式补全。

    走 ``ModelRouter.get_provider_with_fallback()``，自动继承档位降级链路；
    模型不可用时返回 503 而非 500，便于调用方区分「配置问题」与「服务不可用」。
    """
    _enforce_rate_limit(principal)
    preset = (
        await session.execute(
            select(AgentPreset).where(AgentPreset.id == agent_id)
        )
    ).scalar_one_or_none()
    if preset is None or (
        preset.tenant_id is not None and preset.tenant_id != principal.tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent 不存在"
        )

    from core.providers.base import ChatMessage

    user_content = body.input
    if body.context:
        user_content = f"[上下文]\n{body.context}\n\n[问题]\n{body.input}"
    messages = [
        ChatMessage(role="system", content=preset.system_prompt),
        ChatMessage(role="user", content=user_content),
    ]

    started = time.monotonic()
    try:
        provider, tier = await app_state.model_router.get_provider_with_fallback()
        completion = await provider.chat_completion(messages)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("公共 API 调用 Agent 失败 agent_id=%s", agent_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Agent 调用失败: {exc}",
        ) from exc
    duration_ms = round((time.monotonic() - started) * 1000, 2)

    # 使用计数为运营统计，失败不影响补全结果
    try:
        preset.use_count = (preset.use_count or 0) + 1
        await session.commit()
    except Exception:
        await session.rollback()
        logger.debug("更新 Agent use_count 失败 agent_id=%s", agent_id, exc_info=True)

    return {
        "agent_id": agent_id,
        "agent_name": preset.name,
        "output": completion.content,
        "model": completion.model,
        "model_tier": getattr(tier, "value", str(tier)),
        "usage": completion.usage or {},
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


@router.get("/datasets", summary="数据集列表")
async def list_datasets(
    dataset_type: Optional[str] = Query(None, max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: ApiKeyPrincipal = Depends(require_api_key("datasets:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页列出当前租户的评测数据集。"""
    _enforce_rate_limit(principal)
    base = select(EvaluationDataset).where(
        EvaluationDataset.tenant_id == principal.tenant_id
    )
    if dataset_type:
        base = base.where(EvaluationDataset.dataset_type == dataset_type)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(EvaluationDataset.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "dataset_type": row.dataset_type,
                "tags": row.tags or [],
                "item_count": row.item_count,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/datasets/{dataset_id}/items", summary="数据集条目")
async def list_dataset_items(
    dataset_id: int,
    item_status: Optional[str] = Query(None, alias="status", max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: ApiKeyPrincipal = Depends(require_api_key("datasets:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页拉取指定数据集下的条目，供外部批量评测使用。"""
    _enforce_rate_limit(principal)
    dataset = (
        await session.execute(
            select(EvaluationDataset).where(
                EvaluationDataset.id == dataset_id,
                EvaluationDataset.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在"
        )

    base = select(DatasetItem).where(
        DatasetItem.dataset_id == dataset_id,
        DatasetItem.tenant_id == principal.tenant_id,
    )
    if item_status:
        base = base.where(DatasetItem.status == item_status)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(DatasetItem.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "dataset_id": dataset_id,
        "dataset_name": dataset.name,
        "items": [
            {
                "id": row.id,
                "input": row.input,
                "expected_output": row.expected_output,
                "metadata": row.metadata_ or {},
                "label": row.label,
                "status": row.status,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 链路追踪（WS-1 模块，缺失时优雅降级）
# ---------------------------------------------------------------------------


@router.get("/traces", summary="调用链路列表")
async def list_traces(
    kind: Optional[str] = Query(None, max_length=32),
    trace_status: Optional[str] = Query(None, alias="status", max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: ApiKeyPrincipal = Depends(require_api_key("traces:read")),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """分页查询链路追踪记录（含 token / 成本汇总）。

    ``models/trace_models.py`` 属 WS-1 交付物，此处做 import 保护：
    模块缺失时返回 503 而非让整个公共 API 因 ImportError 挂不上路由。
    """
    _enforce_rate_limit(principal)
    try:
        from models.trace_models import TraceRecord
    except ImportError:  # pragma: no cover - 仅在 WS-1 未合入时命中
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="链路追踪模块未启用",
        )

    base = select(TraceRecord).where(TraceRecord.tenant_id == principal.tenant_id)
    if kind:
        base = base.where(TraceRecord.kind == kind)
    if trace_status:
        base = base.where(TraceRecord.status == trace_status)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        (
            await session.execute(
                base.order_by(TraceRecord.started_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "trace_id": row.trace_id,
                "name": row.name,
                "kind": row.kind,
                "status": row.status,
                "started_at": _iso(row.started_at),
                "duration_ms": row.duration_ms,
                "total_spans": row.total_spans,
                "total_prompt_tokens": row.total_prompt_tokens,
                "total_completion_tokens": row.total_completion_tokens,
                "total_cost": row.total_cost,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


__all__ = ["router", "reset_rate_limit_buckets"]
