"""Prompt 优化建议 Admin API

路由前缀: /api/v1/admin/prompt-optimization
权限: Role.ADMIN (router 级 dependencies)

完整端点 (6 个):
- POST   /tasks          - 创建优化任务
- GET    /tasks          - 列表
- GET    /tasks/{id}     - 详情
- POST   /tasks/{id}/run - 启动优化（异步后台执行）
- DELETE /tasks/{id}     - 删除
- GET    /tasks/{id}/result - 优化结果
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_app_state
from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.prompt_optimization_models import PromptOptimizationTask
from services.prompt_optimization_service import PromptOptimizationService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/prompt-optimization",
    tags=["admin-prompt-optimization"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class TaskCreate(BaseModel):
    """创建优化任务请求"""

    original_prompt: str = Field(..., min_length=1, description="原始提示词")
    task_type: str = Field(
        default="improve",
        description="任务类型: improve|simplify|translate|specialize",
    )
    model_used: Optional[str] = Field(
        default=None, description="使用的模型档位 (L0/L1/L2/L3)"
    )


# ============================================================
# 路由
# ============================================================


@router.post(
    "/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建优化任务"""
    service = PromptOptimizationService(session)
    try:
        result = await service.create_task(
            tenant_id=tenant_id,
            original_prompt=payload.original_prompt,
            task_type=payload.task_type,
            model_used=payload.model_used,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return result


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    task_status: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """优化任务列表"""
    service = PromptOptimizationService(session)
    return await service.list_tasks(
        tenant_id=tenant_id,
        task_status=task_status,
        page=page,
        size=size,
    )


@router.get("/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """获取优化任务详情"""
    service = PromptOptimizationService(session)
    result = await service.get_task(task_id, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"优化任务 {task_id} 不存在",
        )
    return result


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def run_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """启动优化任务（异步后台执行，不阻塞 API 响应）

    通过 asyncio.create_task() 在后台执行:
    1. 构建 LLM 优化 prompt（根据 task_type）
    2. 调用 LLM 获取优化建议和评分
    3. 解析 LLM 返回的 JSON 结果
    4. 存储优化后的 prompt 和评分
    """
    service = PromptOptimizationService(session)
    task = await service.get_task(task_id, tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"优化任务 {task_id} 不存在",
        )

    # 校验任务状态（避免重复运行）
    if task["status"] == "processing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"优化任务 {task_id} 正在处理中",
        )

    # 原子条件 UPDATE: 仅当 status != "processing" 时才更新
    result = await session.execute(
        update(PromptOptimizationTask)
        .where(
            PromptOptimizationTask.id == task_id,
            PromptOptimizationTask.tenant_id == tenant_id,
            PromptOptimizationTask.status != "processing",
        )
        .values(status="processing")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"优化任务 {task_id} 正在处理中",
        )
    await session.commit()

    # 获取 app_state 中的 model_router
    app_state = get_app_state(request)
    model_router = app_state.model_router
    if model_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ModelRouter 未初始化, 无法执行 LLM 优化",
        )

    # 启动后台任务
    service.run_optimization_background(task_id, model_router, tenant_id=tenant_id)
    logger.info("启动 Prompt 优化任务 %s (后台执行)", task_id)

    return {
        "task_id": task_id,
        "status": "processing",
        "message": "优化任务已启动, 请通过 GET /tasks/{id}/result 查看结果",
    }


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除优化任务"""
    service = PromptOptimizationService(session)
    deleted = await service.delete_task(task_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"优化任务 {task_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "task_id": task_id}


@router.get("/tasks/{task_id}/result", response_model=Dict[str, Any])
async def get_task_result(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """获取优化结果详情（含优化后 prompt、建议、评分）"""
    service = PromptOptimizationService(session)
    result = await service.get_task_result(task_id, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"优化任务 {task_id} 不存在",
        )
    return result
