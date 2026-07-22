"""深度文档解析 Admin API

路由前缀: /api/v1/admin/doc-parsing
权限: Role.ADMIN / Role.HR

完整端点:
- POST   /tasks          - 创建解析任务
- GET    /tasks          - 任务列表
- GET    /tasks/{id}     - 任务详情
- POST   /tasks/{id}/process - 执行解析 (后台异步)
- GET    /tasks/{id}/results - 解析结果 (可按页过滤)
- DELETE /tasks/{id}     - 删除任务
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.doc_parsing_models import DocParsingTask
from services.doc_parsing_service import (
    SUPPORTED_FILE_TYPES,
    SUPPORTED_STRATEGIES,
    TASK_STATUS_PROCESSING,
    DocParsingService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/doc-parsing",
    tags=["admin-doc-parsing"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# Schemas
# ============================================================


class TaskCreate(BaseModel):
    """创建解析任务请求"""

    file_path: str = Field(..., description="待解析文件路径")
    file_type: str = Field(..., description="文件类型: pdf/docx/xlsx/pptx/txt/md")
    parse_strategy: str = Field(default="auto", description="解析策略: auto/ocr/structure/hybrid")


# ============================================================
# 路由
# ============================================================


@router.post("/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_db),
):
    """创建解析任务"""
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    try:
        task = await service.create_task(
            payload.file_path,
            payload.file_type,
            payload.parse_strategy,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    await session.commit()
    return DocParsingService._task_to_dict(task)


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    status_filter: Optional[str] = Query(None, alias="status", description="状态过滤"),
    file_type: Optional[str] = Query(None, description="文件类型过滤"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
):
    """任务列表 (分页)"""
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    return await service.list_tasks(
        status_filter=status_filter,
        file_type=file_type,
        page=page,
        size=size,
        tenant_id=tenant_id,
    )


@router.get("/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_db),
):
    """任务详情"""
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"解析任务 {task_id} 不存在",
        )
    return DocParsingService._task_to_dict(task)


@router.post("/tasks/{task_id}/process", response_model=Dict[str, Any])
async def process_task(
    task_id: int,
    sync: bool = Query(False, description="True 同步执行 (等待完成), False 后台异步执行"),
    session: AsyncSession = Depends(get_db),
):
    """执行解析任务

    - sync=False (默认): 后台异步执行 (asyncio.create_task), 立即返回 pending/processing 状态。
    - sync=True: 同步执行 (等待解析完成), 适用于小文件或测试。

    解析库 (pdfplumber/python-docx/openpyxl/python-pptx) 未安装时返回 400 错误。
    """
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"解析任务 {task_id} 不存在",
        )

    # H5: 原子条件 UPDATE 防止 TOCTOU 竞态 (重复执行检查)
    # 仅当 status != "processing" 时才更新为 "processing"
    result = await session.execute(
        update(DocParsingTask)
        .where(
            DocParsingTask.id == task_id,
            DocParsingTask.tenant_id == tenant_id,
            DocParsingTask.status != TASK_STATUS_PROCESSING,
        )
        .values(status=TASK_STATUS_PROCESSING)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # 状态已被其他请求修改为 processing (竞态), 返回 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"解析任务 {task_id} 正在处理中, 请勿重复执行",
        )
    await session.commit()
    session.expire(task)  # 使缓存失效, 后续查询从数据库重新加载

    if sync:
        # 同步执行 (等待完成)
        try:
            await service.process_task(task_id, tenant_id=tenant_id)
        except ValueError as e:
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
            )
        await session.commit()
        # 重新查询获取最新状态
        task = await service.get_task(task_id, tenant_id=tenant_id)
        return DocParsingService._task_to_dict(task)
    else:
        # 后台异步执行 (独立 session, 传入 tenant_id 设置租户上下文)
        service.schedule_processing(task_id, tenant_id=tenant_id)
        return {
            "id": task_id,
            "status": "processing",
            "message": "解析任务已调度后台执行, 请稍后查询任务详情获取结果",
        }


@router.get("/tasks/{task_id}/results", response_model=Dict[str, Any])
async def get_task_results(
    task_id: int,
    page_num: Optional[int] = Query(None, ge=1, description="按页过滤 (页码从 1 开始)"),
    session: AsyncSession = Depends(get_db),
):
    """获取解析结果 (可按页过滤)

    返回结构化解析结果 (text / table / image / heading / list)。
    table 类型的 content 为 JSON 字符串 (含 headers + rows)。
    """
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    # 先检查任务是否存在
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"解析任务 {task_id} 不存在",
        )
    results = await service.get_task_results(
        task_id, page_num=page_num, tenant_id=tenant_id
    )
    return {
        "task_id": task_id,
        "task_status": task.status,
        "page_num_filter": page_num,
        "items": [DocParsingService._result_to_dict(r) for r in results],
        "total": len(results),
    }


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除解析任务 (级联删除结果)"""
    tenant_id = get_current_tenant()
    service = DocParsingService(session)
    deleted = await service.delete_task(task_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"解析任务 {task_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": task_id}
