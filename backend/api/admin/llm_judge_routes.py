"""LLM-as-a-Judge 自动评测 Admin API

路由前缀: /api/v1/admin/llm-judge
权限: Role.ADMIN (router 级 dependencies)

完整端点 (7 个):
- POST   /tasks            - 创建评测任务
- GET    /tasks            - 列表
- GET    /tasks/{task_id}  - 详情
- POST   /tasks/{task_id}/run - 启动评测 (异步后台执行)
- DELETE /tasks/{task_id}  - 删除
- GET    /tasks/{task_id}/results - 结果列表 (分页)
- GET    /tasks/{task_id}/summary - 汇总统计
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_app_state
from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.evaluation_models import EvaluationTask
from services.llm_judge_service import DEFAULT_JUDGE_PROMPT_TEMPLATE, DEFAULT_METRICS, LLMJudgeService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/llm-judge",
    tags=["admin-llm-judge"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class EvalTaskCreate(BaseModel):
    """创建评测任务请求"""

    name: str = Field(..., min_length=1, max_length=256, description="任务名称")
    dataset_id: int = Field(..., description="关联数据集 ID")
    judge_model: str = Field(default="L0", description="评判模型档位 (L0/L1/L2/L3)")
    metrics: Optional[List[str]] = Field(
        default=None, description="评测维度列表 (默认: accuracy/relevance/completeness/fluency)"
    )
    judge_prompt_template: Optional[str] = Field(
        default=None, description="评判提示词模板 (支持 {input}/{expected_output}/{output}/{metrics} 占位符)"
    )


# ============================================================
# 任务 CRUD 路由
# ============================================================


@router.post("/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: EvalTaskCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建评测任务"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    try:
        entity = await service.create_task(
            name=payload.name,
            dataset_id=payload.dataset_id,
            tenant_id=tenant_id,
            judge_model=payload.judge_model,
            metrics=payload.metrics,
            judge_prompt_template=payload.judge_prompt_template,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    await session.refresh(entity)
    return LLMJudgeService._task_to_dict(entity)


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    request: Request,
    session: AsyncSession = Depends(get_db),
    task_status: Optional[str] = Query(default=None, alias="status", description="按状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """评测任务列表"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    return await service.list_tasks(
        tenant_id=tenant_id,
        status=task_status,
        page=page,
        size=size,
    )


@router.get("/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取评测任务详情"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    entity = await service.get_task(task_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评测任务 {task_id} 不存在",
        )
    return LLMJudgeService._task_to_dict(entity)


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def run_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """启动评测任务 (异步后台执行, 不阻塞 API 响应)

    通过 asyncio.create_task() 在后台执行:
    1. 遍历数据集条目
    2. 用 LLM 生成 Agent 输出
    3. 用 LLM Judge 评分
    4. 存储结果并更新进度
    """
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评测任务 {task_id} 不存在",
        )

    # 校验任务状态 (避免重复运行)
    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"评测任务 {task_id} 正在运行中",
        )

    # H5: 在路由层立即将状态设为 "running" 并 commit, 防止 TOCTOU 竞态
    # 使用原子条件 UPDATE: 仅当 status != "running" 时才更新
    result = await session.execute(
        update(EvaluationTask)
        .where(
            EvaluationTask.id == task_id,
            EvaluationTask.tenant_id == tenant_id,
            EvaluationTask.status != "running",
        )
        .values(status="running")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # 状态已被其他请求修改为 running (竞态), 返回 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"评测任务 {task_id} 正在运行中",
        )
    await session.commit()

    # 获取 app_state 中的 model_router
    app_state = get_app_state(request)
    model_router = app_state.model_router
    if model_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ModelRouter 未初始化, 无法执行 LLM 评测",
        )

    # 启动后台任务
    service.run_task_background(task_id, model_router, tenant_id=tenant_id)
    logger.info("启动评测任务 %s (后台执行)", task_id)

    return {
        "task_id": task_id,
        "status": "running",
        "message": "评测任务已启动, 请通过 GET /tasks/{id} 或 GET /tasks/{id}/results 查看进度和结果",
    }


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除评测任务 (同时删除所有结果)"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    deleted = await service.delete_task(task_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评测任务 {task_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "task_id": task_id}


# ============================================================
# 结果查询路由
# ============================================================


@router.get("/tasks/{task_id}/results", response_model=Dict[str, Any])
async def get_task_results(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """评测结果列表 (分页)"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    # 校验任务存在
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评测任务 {task_id} 不存在",
        )
    return await service.get_task_results(
        task_id, tenant_id=tenant_id, page=page, size=size
    )


@router.get("/tasks/{task_id}/summary", response_model=Dict[str, Any])
async def get_task_summary(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """评测汇总统计 (平均分/通过率/各维度得分)"""
    tenant_id = get_current_tenant()
    service = LLMJudgeService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评测任务 {task_id} 不存在",
        )
    return await service.get_task_summary(task_id, tenant_id=tenant_id)
