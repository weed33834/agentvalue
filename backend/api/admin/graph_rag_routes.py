"""GraphRAG 知识图谱 Admin API

路由前缀: /api/v1/admin/graph-rag
权限: Role.ADMIN (router 级 dependencies)

对标 RagFlow GraphRAG + RAPTOR, 完整端点:
- POST   /tasks                  - 创建抽取任务
- GET    /tasks                  - 任务列表
- GET    /tasks/{id}             - 任务详情
- POST   /tasks/{id}/run         - 启动抽取 (后台异步执行)
- DELETE /tasks/{id}             - 删除任务
- GET    /entities               - 实体列表 (支持类型过滤)
- GET    /entities/{id}          - 实体详情
- DELETE /entities/{id}          - 删除实体
- GET    /entities/{id}/relations - 实体关联关系
- GET    /relations              - 关系列表
- GET    /search                 - 图增强检索 (query + collection_name + depth)
- GET    /visualization/{entity_id} - 图谱可视化 (depth 参数)
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
from models.knowledge_graph_models import KnowledgeGraphTask
from services.graph_rag_service import (
    SUPPORTED_ENTITY_TYPES,
    TASK_STATUS_PROCESSING,
    GraphRAGService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/graph-rag",
    tags=["admin-graph-rag"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ExtractionTaskCreate(BaseModel):
    """创建知识图谱抽取任务请求"""

    name: str = Field(..., min_length=1, max_length=256, description="任务名称")
    collection_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="ChromaDB collection 名称 (文档来源)",
    )
    document_ids: Optional[List[str]] = Field(
        default=None,
        description="待抽取的文档 ID 列表, 为空时抽取 collection 内全部文档",
    )


# ============================================================
# 任务 CRUD 路由
# ============================================================


@router.post(
    "/tasks", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_task(
    payload: ExtractionTaskCreate,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """创建知识图谱抽取任务

    从指定 collection 的文档集合中抽取实体和关系, 构建知识图谱。
    document_ids 为空时抽取 collection 内全部文档。
    """
    service = GraphRAGService(session)
    try:
        task = await service.create_extraction_task(
            name=payload.name,
            collection_name=payload.collection_name,
            document_ids=payload.document_ids,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return GraphRAGService._task_to_dict(task)


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    tenant_id: str = Depends(get_current_tenant),
    task_status: Optional[str] = Query(
        default=None,
        alias="status",
        description="按状态过滤 (pending/processing/completed/failed)",
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_db),
):
    """知识图谱抽取任务列表 (分页)"""
    service = GraphRAGService(session)
    return await service.list_tasks(
        status_filter=task_status,
        page=page,
        size=size,
        tenant_id=tenant_id,
    )


@router.get("/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(
    task_id: int,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """获取抽取任务详情"""
    service = GraphRAGService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"抽取任务 {task_id} 不存在",
        )
    return GraphRAGService._task_to_dict(task)


@router.post("/tasks/{task_id}/run", response_model=Dict[str, Any])
async def run_task(
    task_id: int,
    request: Request,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """启动知识图谱抽取任务 (异步后台执行, 不阻塞 API 响应)

    通过 asyncio.create_task() 在后台执行:
    1. 从 collection 中读取文档内容
    2. 用 LLM 抽取实体和关系
    3. 实体去重合并, 关系解析持久化
    4. 更新任务统计与状态
    """
    service = GraphRAGService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"抽取任务 {task_id} 不存在",
        )

    # 原子条件 UPDATE 防止 TOCTOU 竞态 (重复执行检查)
    result = await session.execute(
        update(KnowledgeGraphTask)
        .where(
            KnowledgeGraphTask.id == task_id,
            KnowledgeGraphTask.tenant_id == tenant_id,
            KnowledgeGraphTask.status != TASK_STATUS_PROCESSING,
        )
        .values(status=TASK_STATUS_PROCESSING)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"抽取任务 {task_id} 正在处理中, 请勿重复执行",
        )
    await session.commit()

    # 获取 app_state 中的 model_router 与 kb_store
    app_state = get_app_state(request)
    model_router = app_state.model_router
    if model_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ModelRouter 未初始化, 无法执行实体抽取",
        )
    # 优先用 app_state 中按租户隔离的 kb_store; service 内部也会按 collection_name 兜底
    kb_store = None
    try:
        kb_store = app_state.get_kb_store(tenant_id)
    except Exception:
        kb_store = None

    # 启动后台任务 (独立 session + tenant_scope)
    service.schedule_run(task_id, model_router, kb_store, tenant_id=tenant_id)
    logger.info("启动知识图谱抽取任务 %s (后台执行)", task_id)

    return {
        "id": task_id,
        "status": "processing",
        "message": "抽取任务已调度后台执行, 请稍后查询任务详情获取结果",
    }


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(
    task_id: int,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """删除抽取任务 (仅删除任务记录, 不删除已抽取的实体/关系)"""
    service = GraphRAGService(session)
    deleted = await service.delete_task(task_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"抽取任务 {task_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": task_id}


# ============================================================
# 实体路由
# ============================================================


@router.get("/entities", response_model=Dict[str, Any])
async def list_entities(
    tenant_id: str = Depends(get_current_tenant),
    entity_type: Optional[str] = Query(
        default=None,
        description=f"按实体类型过滤, 可选: {sorted(SUPPORTED_ENTITY_TYPES)}",
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_db),
):
    """实体列表 (分页, 支持类型过滤)"""
    if entity_type and entity_type not in SUPPORTED_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的实体类型: {entity_type}, 可选: {sorted(SUPPORTED_ENTITY_TYPES)}",
        )
    service = GraphRAGService(session)
    return await service.get_entities(
        tenant_id=tenant_id,
        entity_type=entity_type,
        page=page,
        size=size,
    )


@router.get("/entities/{entity_id}", response_model=Dict[str, Any])
async def get_entity(
    entity_id: int,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """获取实体详情 (含关联关系)"""
    service = GraphRAGService(session)
    detail = await service.get_entity_detail(entity_id, tenant_id=tenant_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体 {entity_id} 不存在",
        )
    return detail


@router.delete("/entities/{entity_id}", response_model=Dict[str, Any])
async def delete_entity(
    entity_id: int,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """删除实体 (级联删除关联关系)"""
    service = GraphRAGService(session)
    deleted = await service.delete_entity(entity_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体 {entity_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": entity_id}


@router.get("/entities/{entity_id}/relations", response_model=Dict[str, Any])
async def get_entity_relations(
    entity_id: int,
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """获取实体的关联关系列表"""
    service = GraphRAGService(session)
    # 先校验实体存在
    detail = await service.get_entity_detail(entity_id, tenant_id=tenant_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体 {entity_id} 不存在",
        )
    relations = await service.get_entity_relations(entity_id, tenant_id=tenant_id)
    return {
        "entity_id": entity_id,
        "items": relations,
        "total": len(relations),
    }


# ============================================================
# 关系路由
# ============================================================


@router.get("/relations", response_model=Dict[str, Any])
async def list_relations(
    tenant_id: str = Depends(get_current_tenant),
    entity_id: Optional[int] = Query(
        default=None, description="按实体 ID 过滤 (作为 source 或 target)"
    ),
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_db),
):
    """关系列表 (分页, 支持按实体过滤)"""
    service = GraphRAGService(session)
    return await service.get_relations(
        tenant_id=tenant_id,
        entity_id=entity_id,
        page=page,
        size=size,
    )


# ============================================================
# 图增强检索路由
# ============================================================


@router.get("/search", response_model=Dict[str, Any])
async def graph_search(
    request: Request,
    query: str = Query(..., min_length=1, description="查询文本"),
    collection_name: str = Query(
        ..., min_length=1, description="ChromaDB collection 名称"
    ),
    depth: int = Query(default=2, ge=0, le=5, description="图遍历深度 (跳数)"),
    top_k: int = Query(default=5, ge=1, le=50, description="向量检索返回文档数"),
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """图增强检索

    1. 向量检索获取相关文档
    2. 从文档中提取命中的实体
    3. 做图遍历 (depth 跳) 获取关联实体
    4. 合并上下文返回 (文档 + 实体描述 + 关系描述)
    """
    service = GraphRAGService(session)
    # 优先复用 app_state 中按租户隔离的 kb_store
    kb_store = None
    try:
        app_state = get_app_state(request)
        kb_store = app_state.get_kb_store(tenant_id)
    except Exception:
        kb_store = None

    return await service.search_with_graph(
        query=query,
        collection_name=collection_name,
        depth=depth,
        top_k=top_k,
        kb_store=kb_store,
        tenant_id=tenant_id,
    )


# ============================================================
# 图谱可视化路由
# ============================================================


@router.get("/visualization/{entity_id}", response_model=Dict[str, Any])
async def graph_visualization(
    entity_id: int,
    depth: int = Query(default=2, ge=0, le=5, description="遍历深度 (跳数)"),
    tenant_id: str = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    """图谱可视化数据 (nodes + edges)

    从指定实体出发, BFS depth 跳, 返回节点和边列表 (供前端图谱渲染)。
    """
    service = GraphRAGService(session)
    # 校验起始实体存在
    detail = await service.get_entity_detail(entity_id, tenant_id=tenant_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体 {entity_id} 不存在",
        )
    return await service.get_graph_visualization(
        entity_id, depth=depth, tenant_id=tenant_id
    )
