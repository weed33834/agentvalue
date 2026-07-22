"""人工标注工具 Admin API

路由前缀: /api/v1/admin/annotations
权限: Role.ADMIN (router 级 dependencies)

完整端点 (9 个):
- POST   /tasks                  - 创建标注任务
- GET    /tasks                  - 列表 (分页 + 状态过滤)
- GET    /tasks/{task_id}        - 详情
- PUT    /tasks/{task_id}        - 更新
- DELETE /tasks/{task_id}        - 删除
- POST   /tasks/{task_id}/assign - 分配
- POST   /tasks/{task_id}/annotate - 提交标注
- GET    /tasks/{task_id}/annotations - 标注列表
- GET    /stats                  - 统计
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.annotation_service import AnnotationService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/annotations",
    tags=["admin-annotations"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class AnnotationTaskCreate(BaseModel):
    """创建标注任务请求"""

    name: str = Field(..., min_length=1, max_length=256, description="任务名称")
    content: str = Field(..., min_length=1, description="待标注内容")
    description: Optional[str] = Field(default=None, description="任务描述")
    source_type: str = Field(
        default="agent_output",
        description="来源类型: evaluation_result/chat_message/agent_output",
    )
    source_id: Optional[str] = Field(default=None, description="来源记录 ID")
    priority: int = Field(default=0, ge=0, description="优先级 (越大越优先)")


class AnnotationTaskUpdate(BaseModel):
    """更新标注任务请求"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None


class AssignRequest(BaseModel):
    """分配标注任务请求"""

    user_id: str = Field(..., description="分配给的用户 ID")


class AnnotateRequest(BaseModel):
    """提交标注请求"""

    annotator_id: str = Field(..., description="标注人 ID")
    label: Optional[str] = Field(default=None, description="标签")
    score: float = Field(default=0.0, ge=0, le=100, description="评分 (0-100)")
    feedback: Optional[str] = Field(default=None, description="反馈文本")
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="附加元数据"
    )


class BatchCreateFromEvalRequest(BaseModel):
    """从评测结果批量创建标注任务请求"""

    eval_task_id: int = Field(..., description="LLM 评测任务 ID")
    priority: int = Field(default=0, ge=0, description="标注任务优先级")


# ============================================================
# 标注任务 CRUD 路由
# ============================================================


@router.post("/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: AnnotationTaskCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建标注任务"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    try:
        entity = await service.create_task(
            name=payload.name,
            content=payload.content,
            tenant_id=tenant_id,
            description=payload.description,
            source_type=payload.source_type,
            source_id=payload.source_id,
            priority=payload.priority,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    await session.refresh(entity)
    return AnnotationService._task_to_dict(entity)


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    request: Request,
    session: AsyncSession = Depends(get_db),
    task_status: Optional[str] = Query(default=None, alias="status", description="按状态过滤"),
    assigned_to: Optional[str] = Query(default=None, description="按分配人过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """标注任务列表 (分页 + 状态过滤)"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    return await service.list_tasks(
        tenant_id=tenant_id,
        status=task_status,
        assigned_to=assigned_to,
        page=page,
        size=size,
    )


@router.get("/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取标注任务详情"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    entity = await service.get_task(task_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标注任务 {task_id} 不存在",
        )
    return AnnotationService._task_to_dict(entity)


@router.put("/tasks/{task_id}", response_model=Dict[str, Any])
async def update_task(
    task_id: int,
    payload: AnnotationTaskUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新标注任务"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    try:
        entity = await service.update_task(
            task_id,
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            content=payload.content,
            priority=payload.priority,
            status=payload.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标注任务 {task_id} 不存在",
        )
    await session.commit()
    await session.refresh(entity)
    return AnnotationService._task_to_dict(entity)


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除标注任务 (同时删除所有标注)"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    deleted = await service.delete_task(task_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标注任务 {task_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "task_id": task_id}


# ============================================================
# 分配与标注路由
# ============================================================


@router.post("/tasks/{task_id}/assign", response_model=Dict[str, Any])
async def assign_task(
    task_id: int,
    payload: AssignRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """分配标注任务给指定用户"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    entity = await service.assign_task(task_id, payload.user_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标注任务 {task_id} 不存在",
        )
    await session.commit()
    await session.refresh(entity)
    return AnnotationService._task_to_dict(entity)


@router.post("/tasks/{task_id}/annotate", response_model=Dict[str, Any])
async def submit_annotation(
    task_id: int,
    payload: AnnotateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """提交标注结果"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    annotation = await service.submit_annotation(
        task_id,
        payload.annotator_id,
        tenant_id=tenant_id,
        label=payload.label,
        score=payload.score,
        feedback=payload.feedback,
        metadata=payload.metadata,
    )
    if annotation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标注任务 {task_id} 不存在",
        )
    await session.commit()
    await session.refresh(annotation)
    return AnnotationService._annotation_to_dict(annotation)


@router.get("/tasks/{task_id}/annotations", response_model=Dict[str, Any])
async def list_annotations(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """查询任务的标注列表"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    items = await service.list_annotations(task_id, tenant_id=tenant_id)
    return {"items": items, "total": len(items)}


# ============================================================
# 统计路由
# ============================================================


@router.get("/stats", response_model=Dict[str, Any])
async def get_annotation_stats(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """标注统计 (总数/已完成/待标注/平均分)"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    return await service.get_annotation_stats(tenant_id=tenant_id)


# ============================================================
# 批量创建路由
# ============================================================


@router.post("/tasks/batch-from-eval", response_model=Dict[str, Any])
async def batch_create_from_eval(
    payload: BatchCreateFromEvalRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """从 LLM 评测结果批量创建标注任务"""
    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    result = await service.batch_create_tasks_from_evaluation(
        payload.eval_task_id,
        tenant_id=tenant_id,
        priority=payload.priority,
    )
    await session.commit()
    return result
