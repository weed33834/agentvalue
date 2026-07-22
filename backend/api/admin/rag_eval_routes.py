"""RAG 质量评测 Admin API

路由前缀: /api/v1/admin/rag-eval
权限: Role.ADMIN (router 级 dependencies)

完整端点 (7 个):
- POST   /tasks            - 创建 RAG 评测任务
- GET    /tasks            - 列表
- GET    /tasks/{task_id}  - 详情
- POST   /tasks/{task_id}/run - 启动评测 (异步后台执行)
- DELETE /tasks/{task_id}  - 删除
- GET    /tasks/{task_id}/results - 结果列表 (分页)
- GET    /tasks/{task_id}/summary - 汇总
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
from models.rag_eval_models import RagEvalTask
from services.hybrid_search_service import HybridSearchService
from services.rag_eval_service import DEFAULT_TOP_K, RagEvalService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/rag-eval",
    tags=["admin-rag-eval"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class TestQueryItem(BaseModel):
    """测试查询项"""

    query: str = Field(..., description="查询文本")
    relevant_doc_ids: List[str] = Field(
        default_factory=list, description="相关文档 ID 列表"
    )


class RagEvalTaskCreate(BaseModel):
    """创建 RAG 评测任务请求"""

    name: str = Field(..., min_length=1, max_length=256, description="任务名称")
    collection_name: str = Field(..., description="被评测的 ChromaDB collection 名称")
    test_queries: List[TestQueryItem] = Field(
        ..., min_length=1, description="测试查询列表"
    )


# ============================================================
# 辅助函数
# ============================================================


def _get_search_service(request: Request, tenant_id: str) -> HybridSearchService:
    """获取当前租户的 HybridSearchService 实例"""
    app_state = get_app_state(request)
    kb_store = app_state.get_kb_store(tenant_id)
    return HybridSearchService(kb_store=kb_store, settings=app_state.settings)


def _resolve_collection_name(
    request: Request, tenant_id: str, collection_name: str
) -> str:
    """校验 collection_name 归属当前租户, 防止跨租户访问知识库"""
    tenant_prefix = f"agentvalue_kb_{tenant_id}"
    if collection_name == tenant_prefix or collection_name.startswith(
        tenant_prefix + "_"
    ):
        return collection_name
    # 非本租户 collection, 拒绝访问
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="无权访问指定集合, 仅允许访问当前租户的知识库",
    )


# ============================================================
# 任务 CRUD 路由
# ============================================================


@router.post("/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: RagEvalTaskCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建 RAG 评测任务"""
    tenant_id = get_current_tenant()

    # 校验 collection 归属
    _resolve_collection_name(request, tenant_id, payload.collection_name)

    service = RagEvalService(session)
    try:
        entity = await service.create_task(
            name=payload.name,
            collection_name=payload.collection_name,
            test_queries=[q.model_dump() for q in payload.test_queries],
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    await session.refresh(entity)
    return RagEvalService._task_to_dict(entity)


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    request: Request,
    session: AsyncSession = Depends(get_db),
    task_status: Optional[str] = Query(default=None, alias="status", description="按状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """RAG 评测任务列表"""
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
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
    """获取 RAG 评测任务详情"""
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
    entity = await service.get_task(task_id, tenant_id=tenant_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG 评测任务 {task_id} 不存在",
        )
    return RagEvalService._task_to_dict(entity)


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def run_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    top_k: int = Query(default=DEFAULT_TOP_K, ge=1, le=50, description="检索返回的文档数"),
):
    """启动 RAG 评测任务 (异步后台执行, 不阻塞 API 响应)

    通过 asyncio.create_task() 在后台执行:
    1. 对每个测试 query 执行混合检索
    2. 计算 Precision@K / Recall / MRR / NDCG
    3. 存储结果并更新进度
    """
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG 评测任务 {task_id} 不存在",
        )

    # 校验任务状态
    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"RAG 评测任务 {task_id} 正在运行中",
        )

    # H5: 在路由层立即将状态设为 "running" 并 commit, 防止 TOCTOU 竞态
    # 使用原子条件 UPDATE: 仅当 status != "running" 时才更新
    result = await session.execute(
        update(RagEvalTask)
        .where(
            RagEvalTask.id == task_id,
            RagEvalTask.tenant_id == tenant_id,
            RagEvalTask.status != "running",
        )
        .values(status="running")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # 状态已被其他请求修改为 running (竞态), 返回 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"RAG 评测任务 {task_id} 正在运行中",
        )
    await session.commit()

    # 获取搜索服务
    search_service = _get_search_service(request, tenant_id)

    # 启动后台任务
    service.run_task_background(
        task_id, search_service, tenant_id=tenant_id, top_k=top_k
    )
    logger.info("启动 RAG 评测任务 %s (后台执行)", task_id)

    return {
        "task_id": task_id,
        "status": "running",
        "message": "RAG 评测任务已启动, 请通过 GET /tasks/{id} 或 GET /tasks/{id}/results 查看进度和结果",
    }


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除 RAG 评测任务 (同时删除所有结果)"""
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
    deleted = await service.delete_task(task_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG 评测任务 {task_id} 不存在",
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
    """RAG 评测结果列表 (分页)"""
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG 评测任务 {task_id} 不存在",
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
    """RAG 评测汇总 (平均 precision/recall/MRR/NDCG + 延迟统计)"""
    tenant_id = get_current_tenant()
    service = RagEvalService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG 评测任务 {task_id} 不存在",
        )
    return await service.get_task_summary(task_id, tenant_id=tenant_id)
