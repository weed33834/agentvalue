"""实验对比 Admin API (WS-2 评估体系升级)

路由前缀: /api/v1/admin/experiments
权限: Role.ADMIN (router 级 dependencies)

端点 (15 个):
- POST   /                         - 创建实验
- GET    /                         - 实验列表 (分页)
- POST   /ragas/score              - 单样本 RAGAS 即席打分
- GET    /ragas/metrics            - 可用 RAGAS 指标清单
- GET    /compare                  - Run A vs Run B 对比 (含 bootstrap 置信区间)
- GET    /compare/regression       - CI 门禁用回归报告
- GET    /runs/{run_id}            - 运行详情
- GET    /runs/{run_id}/items      - 逐样本结果 (分页, 可按状态/得分区间过滤)
- POST   /runs/{run_id}/cancel     - 取消运行
- GET    /runs/{run_id}/export     - 导出运行结果 (csv / json)
- DELETE /runs/{run_id}            - 删除运行
- GET    /{experiment_id}          - 实验详情
- PUT    /{experiment_id}          - 更新实验
- DELETE /{experiment_id}          - 删除实验
- POST   /{experiment_id}/runs     - 创建并启动运行
- GET    /{experiment_id}/runs     - 运行列表 (分页)

路由顺序: 所有静态路径 (/ragas/*, /compare*, /runs/*) 必须声明在动态
`/{experiment_id}` 之前, 否则 FastAPI 会把 "ragas" 当成 experiment_id。

长任务执行: 通过 FastAPI BackgroundTasks 托管 (core/arq_job_queue.py 目前只注册了
run_evaluation_task 一种 arq 函数, 无通用任务分发入口)。刻意不用裸
asyncio.create_task —— v3 审计已将其列为"进程重启即丢"的缺陷。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_app_state, get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.experiment_models import RUN_STATUS_PENDING
from services.audit_service import AuditService
from services.experiment_service import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_REGRESSION_THRESHOLD,
    ExperimentService,
    execute_run_background,
)
from services.ragas_metrics_service import ALL_METRICS, RagasMetricsService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/experiments",
    tags=["admin-experiments"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ExperimentCreate(BaseModel):
    """创建实验请求"""

    name: str = Field(..., min_length=1, max_length=256, description="实验名称")
    dataset_id: int = Field(..., description="关联评测数据集 ID")
    task_type: str = Field(default="rag", description="任务类型 rag/agent/prompt/judge")
    description: Optional[str] = Field(default=None, description="实验描述")
    metrics: Optional[List[str]] = Field(default=None, description="参与评测的指标名")
    config: Optional[Dict[str, Any]] = Field(default=None, description="实验级配置")


class ExperimentUpdate(BaseModel):
    """更新实验请求"""

    name: Optional[str] = Field(default=None, max_length=256, description="实验名称")
    description: Optional[str] = Field(default=None, description="实验描述")
    metrics: Optional[List[str]] = Field(default=None, description="指标名列表")
    config: Optional[Dict[str, Any]] = Field(default=None, description="实验级配置")
    status: Optional[str] = Field(
        default=None, description="实验状态 draft/active/archived"
    )


class RunCreate(BaseModel):
    """创建并启动运行请求"""

    name: Optional[str] = Field(default=None, max_length=256, description="运行名称")
    variant: Dict[str, Any] = Field(
        default_factory=dict,
        description="被测变体: model / prompt_version / agent_version / retriever 设置",
    )
    start: bool = Field(default=True, description="是否立即在后台开始执行")


class RagasScoreRequest(BaseModel):
    """单样本 RAGAS 即席打分请求"""

    question: str = Field(..., min_length=1, description="问题")
    answer: str = Field(..., min_length=1, description="待评答案")
    contexts: List[str] = Field(default_factory=list, description="检索上下文块")
    ground_truth: Optional[str] = Field(default=None, description="标准答案 (可选)")
    metrics: Optional[List[str]] = Field(
        default=None, description="指标子集, 缺省按输入条件自动选取"
    )


# ============================================================
# 实验 CRUD (静态路径)
# ============================================================


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_experiment(
    payload: ExperimentCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建实验"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        entity = await service.create_experiment(
            name=payload.name,
            dataset_id=payload.dataset_id,
            tenant_id=tenant_id,
            description=payload.description,
            task_type=payload.task_type,
            metrics=payload.metrics,
            config=payload.config,
            created_by=current_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="create_experiment",
        details={
            "experiment_id": entity.id,
            "dataset_id": payload.dataset_id,
            "task_type": payload.task_type,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = ExperimentService._experiment_to_dict(entity)
    await session.commit()
    return result


@router.get("", response_model=Dict[str, Any])
async def list_experiments(
    request: Request,
    session: AsyncSession = Depends(get_db),
    experiment_status: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    task_type: Optional[str] = Query(default=None, description="按任务类型过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """实验列表 (分页)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    return await service.list_experiments(
        tenant_id=tenant_id,
        status=experiment_status,
        task_type=task_type,
        page=page,
        size=size,
    )


# ============================================================
# RAGAS 即席打分 (静态路径, 必须在 /{experiment_id} 之前)
# ============================================================


@router.get("/ragas/metrics", response_model=Dict[str, Any])
async def list_ragas_metrics(request: Request):
    """列出可用的 RAGAS 生成质量指标及其输入要求"""
    return {
        "metrics": [
            {
                "name": "faithfulness",
                "requires": ["answer", "contexts"],
                "description": "答案的原子声明被检索上下文支撑的比例",
            },
            {
                "name": "answer_relevancy",
                "requires": ["question", "answer"],
                "description": "由答案反推问题, 与原问题的平均语义相似度",
            },
            {
                "name": "context_precision",
                "requires": ["question", "contexts"],
                "description": "相关上下文块的排序加权平均精度",
            },
            {
                "name": "context_recall",
                "requires": ["ground_truth", "contexts"],
                "description": "标准答案声明可归因到上下文的比例",
            },
            {
                "name": "answer_correctness",
                "requires": ["answer", "ground_truth"],
                "description": "事实 TP/FP/FN F1 与语义相似度加权",
            },
        ],
        "total": len(ALL_METRICS),
    }


@router.post("/ragas/score", response_model=Dict[str, Any])
async def score_ragas_sample(
    payload: RagasScoreRequest,
    request: Request,
):
    """对单条样本即席计算 RAGAS 指标 (不落库)

    LLM 或 embedding 不可用时, 对应指标返回 score=null +
    status="unavailable" + reason, 绝不返回编造的数值。
    """
    unknown = [m for m in (payload.metrics or []) if m not in ALL_METRICS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知指标: {unknown}, 可选: {ALL_METRICS}",
        )

    app_state = get_app_state(request)
    service = RagasMetricsService(app_state.model_router)
    result = await service.evaluate_sample(
        question=payload.question,
        answer=payload.answer,
        contexts=payload.contexts,
        ground_truth=payload.ground_truth,
        metrics=payload.metrics,
    )
    return result.to_dict()


# ============================================================
# Run 对比 (静态路径)
# ============================================================


@router.get("/compare", response_model=Dict[str, Any])
async def compare_runs(
    request: Request,
    run_a: int = Query(..., description="基线运行 ID"),
    run_b: int = Query(..., description="候选运行 ID"),
    session: AsyncSession = Depends(get_db),
    primary_metric: Optional[str] = Query(
        default=None, description="逐样本对比的主指标, 缺省取全部指标均值"
    ),
    max_samples: Optional[int] = Query(
        default=None, ge=1, le=5000, description="逐样本清单最多返回条数"
    ),
    iterations: int = Query(
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
        ge=100,
        le=10000,
        description="bootstrap 重采样次数",
    ),
    seed: int = Query(default=DEFAULT_BOOTSTRAP_SEED, description="bootstrap 随机种子"),
):
    """Run A vs Run B 对比

    返回逐指标 delta + bootstrap 95% 置信区间 + 显著性判定,
    以及按 sample_id 对齐的逐样本回归清单 (回归最严重的排最前)。
    """
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        result = await service.compare_runs(
            run_a,
            run_b,
            tenant_id=tenant_id,
            bootstrap_iterations=iterations,
            seed=seed,
            primary_metric=primary_metric,
            max_samples=max_samples,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return result.to_dict()


@router.get("/compare/regression", response_model=Dict[str, Any])
async def get_regression_report(
    request: Request,
    run_a: int = Query(..., description="基线运行 ID"),
    run_b: int = Query(..., description="候选运行 ID"),
    session: AsyncSession = Depends(get_db),
    threshold: float = Query(
        default=DEFAULT_REGRESSION_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="回归阈值 (0-1 量纲)",
    ),
    primary_metric: Optional[str] = Query(default=None, description="逐样本主指标"),
):
    """CI 门禁用回归报告: 下降幅度超过 threshold 的样本与指标"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        return await service.get_regression_report(
            run_a,
            run_b,
            threshold,
            tenant_id=tenant_id,
            primary_metric=primary_metric,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# ============================================================
# Run 详情 / 逐样本 / 取消 / 导出 (静态前缀 /runs)
# ============================================================


@router.get("/runs/{run_id}", response_model=Dict[str, Any])
async def get_run(
    run_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """运行详情 (含进度与 metric_summary, 供 UI 轮询)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    run = await service.get_run(run_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"实验运行 {run_id} 不存在"
        )
    return ExperimentService._run_to_dict(run)


@router.get("/runs/{run_id}/items", response_model=Dict[str, Any])
async def list_run_items(
    run_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    item_status: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤 success/failed/skipped"
    ),
    metric: Optional[str] = Query(
        default=None, description="得分过滤所依据的指标, 缺省取全部指标均值"
    ),
    min_score: Optional[float] = Query(default=None, description="得分下限"),
    max_score: Optional[float] = Query(default=None, description="得分上限"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=50, ge=1, le=200, description="每页条数"),
):
    """逐样本结果 (分页, 支持状态与得分区间过滤)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    run = await service.get_run(run_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"实验运行 {run_id} 不存在"
        )
    return await service.list_run_items(
        run_id,
        tenant_id=tenant_id,
        status=item_status,
        metric=metric,
        min_score=min_score,
        max_score=max_score,
        page=page,
        size=size,
    )


@router.post("/runs/{run_id}/cancel", response_model=Dict[str, Any])
async def cancel_run(
    run_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """取消运行 (最迟在下一批样本处生效)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    run = await service.get_run(run_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"实验运行 {run_id} 不存在"
        )
    cancelled = await service.cancel_run(run_id, tenant_id=tenant_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"实验运行 {run_id} 当前状态 {run.status} 不可取消",
        )
    await audit_service.log(
        actor_id=current_user_id,
        action="cancel_experiment_run",
        details={"run_id": run_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"run_id": run_id, "status": "cancelled"}


@router.get("/runs/{run_id}/export")
async def export_run(
    run_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    format: str = Query(default="json", pattern="^(json|csv)$", description="导出格式"),
):
    """导出运行的逐样本结果 (json / csv)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        content = await service.export_run(run_id, format, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    media_type = "text/csv" if format == "csv" else "application/json"
    return Response(
        content=content,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="experiment_run_{run_id}.{format}"'
            )
        },
    )


@router.delete("/runs/{run_id}", response_model=Dict[str, Any])
async def delete_run(
    run_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除运行及其逐样本结果"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    deleted = await service.delete_run(run_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"实验运行 {run_id} 不存在"
        )
    await audit_service.log(
        actor_id=current_user_id,
        action="delete_experiment_run",
        details={"run_id": run_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "run_id": run_id}


# ============================================================
# 实验详情 / 更新 / 删除 / 运行 (动态路径, 必须放在最后)
# ============================================================


@router.get("/{experiment_id}", response_model=Dict[str, Any])
async def get_experiment(
    experiment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """实验详情"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    entity = await service.get_experiment(experiment_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实验 {experiment_id} 不存在",
        )
    return ExperimentService._experiment_to_dict(entity)


@router.put("/{experiment_id}", response_model=Dict[str, Any])
async def update_experiment(
    experiment_id: int,
    payload: ExperimentUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新实验"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        entity = await service.update_experiment(
            experiment_id,
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            metrics=payload.metrics,
            config=payload.config,
            status=payload.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实验 {experiment_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="update_experiment",
        details={"experiment_id": experiment_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = ExperimentService._experiment_to_dict(entity)
    await session.commit()
    return result


@router.delete("/{experiment_id}", response_model=Dict[str, Any])
async def delete_experiment(
    experiment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除实验 (级联删除其下的运行与逐样本结果)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    deleted = await service.delete_experiment(experiment_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实验 {experiment_id} 不存在",
        )
    await audit_service.log(
        actor_id=current_user_id,
        action="delete_experiment",
        details={"experiment_id": experiment_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "experiment_id": experiment_id}


@router.post(
    "/{experiment_id}/runs",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    experiment_id: int,
    payload: RunCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建运行并 (可选) 立即在后台开始执行

    后台执行由 BackgroundTasks 托管, 内部使用独立数据库会话与租户上下文;
    进度可通过 GET /runs/{run_id} 轮询。
    """
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    try:
        run = await service.create_run(
            experiment_id,
            tenant_id=tenant_id,
            name=payload.name,
            variant=payload.variant,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_service.log(
        actor_id=current_user_id,
        action="create_experiment_run",
        details={
            "experiment_id": experiment_id,
            "run_id": run.id,
            "variant": payload.variant,
            "start": payload.start,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    result = ExperimentService._run_to_dict(run)
    await session.commit()

    if payload.start:
        model_router = get_app_state(request).model_router
        background_tasks.add_task(
            execute_run_background,
            run.id,
            tenant_id=tenant_id,
            model_router=model_router,
        )
        logger.info("实验运行 %s 已提交后台执行", run.id)
        result["status"] = RUN_STATUS_PENDING
        result["message"] = "运行已提交后台执行, 请轮询 GET /runs/{run_id} 查看进度"
    return result


@router.get("/{experiment_id}/runs", response_model=Dict[str, Any])
async def list_runs(
    experiment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    run_status: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """某实验下的运行列表 (分页)"""
    tenant_id = get_current_tenant()
    service = ExperimentService(session)
    experiment = await service.get_experiment(experiment_id, tenant_id=tenant_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实验 {experiment_id} 不存在",
        )
    return await service.list_runs(
        experiment_id,
        tenant_id=tenant_id,
        status=run_status,
        page=page,
        size=size,
    )
