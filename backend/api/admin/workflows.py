"""工作流可视化编排 Admin API (P4-2: 对标 Dify Workflow / Coze Bot 编排)

路由前缀: /api/v1/admin/workflows
权限: Role.ADMIN (router 级 dependencies)

完整功能 (11 端点):
- GET    /                              - 列表 (支持 search + tenant_id 过滤)
- POST   /                              - 创建 (name + description + graph + input_schema)
- GET    /{workflow_id}                 - 详情
- PUT    /{workflow_id}                 - 更新
- DELETE /{workflow_id}                 - 删除
- POST   /{workflow_id}/toggle          - 启用/禁用
- POST   /{workflow_id}/run             - 执行, body {inputs: dict}, 返回 {run_id, thread_id, status}
- GET    /runs/{run_id}                 - 查询运行状态
- GET    /runs/{run_id}/node-states     - 节点级执行状态
- GET    /{workflow_id}/runs            - 工作流的运行历史
- POST   /{workflow_id}/validate        - 验证 graph 合法性 (检查环 / 必填字段 / 节点类型)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AppState, get_app_state
from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from core.workflow_engine import WorkflowEngine, WorkflowValidationError
from models.workflow import Workflow, WorkflowRun

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/workflows",
    tags=["admin-workflows"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class WorkflowCreate(BaseModel):
    """创建工作流请求"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="工作流名")
    description: str = Field(default="", max_length=512, description="工作流描述")
    graph: Dict[str, Any] = Field(..., description="DAG 定义 {nodes, edges}")
    input_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description='输入变量 schema {"variables": [{"name, type, default}]}',
    )
    enabled: bool = Field(default=True, description="启用状态")
    tenant_id: Optional[str] = Field(
        default=None, max_length=64, description="租户 ID (None 时用当前上下文)"
    )


class WorkflowUpdate(BaseModel):
    """更新工作流请求 (所有字段可选)"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    graph: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class WorkflowToggle(BaseModel):
    """启用/禁用切换"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., description="目标状态")


class WorkflowRunRequest(BaseModel):
    """执行工作流请求"""

    model_config = ConfigDict(extra="forbid")

    inputs: Dict[str, Any] = Field(default_factory=dict, description="输入变量值")
    thread_id: Optional[str] = Field(
        default=None, description="线程 ID (可关联 trace, 不传则自动生成)"
    )


class WorkflowValidateRequest(BaseModel):
    """验证 graph (允许直接传 graph, 不入库)"""

    model_config = ConfigDict(extra="forbid")

    graph: Optional[Dict[str, Any]] = Field(
        default=None, description="待验证的 graph (不传则用已存 workflow 的 graph)"
    )


# ============================================================
# 工具函数
# ============================================================


def _gen_id(prefix: str = "wf") -> str:
    """生成主键 (uuid4 hex 带前缀)"""
    # review 优化: 复用 admin/_common.gen_id,统一 ID 生成逻辑
    from api.admin._common import gen_id

    return gen_id(prefix=prefix)


def _entity_to_dict(entity: Workflow) -> Dict[str, Any]:
    """Workflow entity → dict (供 API 返回)"""
    return {
        "id": entity.id,
        "name": entity.name,
        "description": entity.description,
        "graph": entity.graph,
        "input_schema": entity.input_schema or {},
        "enabled": entity.enabled,
        "version": entity.version,
        "tenant_id": entity.tenant_id,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }


def _run_entity_to_dict(entity: WorkflowRun) -> Dict[str, Any]:
    """WorkflowRun entity → dict"""
    return {
        "id": entity.id,
        "workflow_id": entity.workflow_id,
        "thread_id": entity.thread_id,
        "status": entity.status,
        "inputs": entity.inputs,
        "outputs": entity.outputs,
        "node_states": entity.node_states or {},
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "completed_at": (
            entity.completed_at.isoformat() if entity.completed_at else None
        ),
    }


def _get_engine(app_state: AppState) -> WorkflowEngine:
    """从 AppState 获取 / 构造 WorkflowEngine"""
    engine = getattr(app_state, "_workflow_engine", None)
    if engine is None:
        engine = WorkflowEngine(app_state=app_state)
        app_state._workflow_engine = engine  # type: ignore[attr-defined]
    return engine


# ============================================================
# CRUD 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_workflows(
    request: Request,
    search: Optional[str] = Query(None, description="按 name/description 模糊搜索"),
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有工作流 (支持 search + tenant_id 过滤)"""
    stmt = select(Workflow)
    if search:
        kw = f"%{search}%"
        stmt = stmt.where(or_(Workflow.name.ilike(kw), Workflow.description.ilike(kw)))
    if tenant_id:
        stmt = stmt.where(Workflow.tenant_id == tenant_id)
    stmt = stmt.order_by(Workflow.created_at.desc())

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return {
        "items": [_entity_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建工作流"""
    # 校验 graph (创建前预检, 避免存入无效 graph)
    engine = WorkflowEngine()
    errors = engine.validate(payload.graph)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"工作流图校验失败: {'; '.join(errors)}",
        )

    tenant = payload.tenant_id or get_current_tenant()
    entity = Workflow(
        id=_gen_id("wf"),
        name=payload.name,
        description=payload.description,
        graph=payload.graph,
        input_schema=payload.input_schema or {},
        enabled=payload.enabled,
        version=1,
        tenant_id=tenant,
    )
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    return _entity_to_dict(entity)


@router.get("/{workflow_id}", response_model=Dict[str, Any])
async def get_workflow(
    workflow_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取工作流详情"""
    entity = await session.get(Workflow, workflow_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流 {workflow_id} 不存在",
        )
    return _entity_to_dict(entity)


@router.put("/{workflow_id}", response_model=Dict[str, Any])
async def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新工作流 (graph 变化时 version +1)"""
    entity = await session.get(Workflow, workflow_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流 {workflow_id} 不存在",
        )

    graph_changed = False
    if payload.name is not None:
        # 检查重名 (排除自身)
        existing = await session.execute(
            select(Workflow).where(
                Workflow.tenant_id == entity.tenant_id,
                Workflow.name == payload.name,
                Workflow.id != entity.id,
            )
        )
        if existing.scalars().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"工作流名 {payload.name} 已存在",
            )
        entity.name = payload.name
    if payload.description is not None:
        entity.description = payload.description
    if payload.graph is not None:
        # 校验新 graph
        engine = WorkflowEngine()
        errors = engine.validate(payload.graph)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"工作流图校验失败: {'; '.join(errors)}",
            )
        entity.graph = payload.graph
        graph_changed = True
    if payload.input_schema is not None:
        entity.input_schema = payload.input_schema
        graph_changed = True
    if payload.enabled is not None:
        entity.enabled = payload.enabled

    if graph_changed:
        entity.version = (entity.version or 1) + 1

    await session.commit()
    await session.refresh(entity)
    return _entity_to_dict(entity)


@router.delete("/{workflow_id}", response_model=Dict[str, Any])
async def delete_workflow(
    workflow_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除工作流 (关联的 runs 保留, 通过 workflow_id 软关联)"""
    entity = await session.get(Workflow, workflow_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流 {workflow_id} 不存在",
        )
    name = entity.name
    await session.delete(entity)
    await session.commit()
    return {"deleted": True, "id": workflow_id, "name": name}


@router.post("/{workflow_id}/toggle", response_model=Dict[str, Any])
async def toggle_workflow(
    workflow_id: str,
    payload: WorkflowToggle,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """启用/禁用工作流"""
    entity = await session.get(Workflow, workflow_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流 {workflow_id} 不存在",
        )
    entity.enabled = payload.enabled
    await session.commit()
    await session.refresh(entity)
    return {"id": entity.id, "name": entity.name, "enabled": entity.enabled}


# ============================================================
# 执行路由
# ============================================================


@router.post("/{workflow_id}/run", response_model=Dict[str, Any])
async def run_workflow(
    workflow_id: str,
    payload: WorkflowRunRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
):
    """执行工作流

    流程:
    1. 取 workflow 实体, 校验 enabled
    2. 校验 graph (执行前再次检查, 防止绕过 validate 直接运行)
    3. 调 WorkflowEngine.execute 解释执行
    4. 落库 WorkflowRun, 返回 {run_id, thread_id, status, ...}
    """
    entity = await session.get(Workflow, workflow_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流 {workflow_id} 不存在",
        )
    if not entity.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"工作流 {workflow_id} 已禁用, 不能执行",
        )

    engine = _get_engine(app_state)
    # 执行前再次校验 graph
    errors = engine.validate(entity.graph)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"工作流图校验失败: {'; '.join(errors)}",
        )

    # 创建 WorkflowRun (status=pending)
    run_id = _gen_id("run")
    thread_id = payload.thread_id or f"thr_{uuid.uuid4().hex[:16]}"
    run_entity = WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        thread_id=thread_id,
        status="running",
        inputs=payload.inputs,
        outputs={},
        node_states={},
    )
    session.add(run_entity)
    await session.commit()
    await session.refresh(run_entity)

    # 执行 (同步等待, 失败标 failed)
    try:
        result = await engine.execute(
            entity, inputs=payload.inputs, thread_id=thread_id
        )
    except WorkflowValidationError as e:
        run_entity.status = "failed"
        run_entity.node_states = {"_validation": {"error": str(e)}}
        run_entity.completed_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("工作流 %s 执行异常", workflow_id)
        run_entity.status = "failed"
        run_entity.node_states = {"_engine": {"error": str(e)}}
        run_entity.completed_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"工作流执行失败: {e}",
        )

    # 落库结果
    run_entity.status = result.get("status", "completed")
    run_entity.outputs = result.get("outputs", {})
    run_entity.node_states = result.get("node_states", {})
    run_entity.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(run_entity)

    return {
        "run_id": run_entity.id,
        "thread_id": run_entity.thread_id,
        "status": run_entity.status,
        "workflow_id": workflow_id,
        "outputs": run_entity.outputs,
        "node_states": run_entity.node_states,
    }


@router.get("/runs/{run_id}", response_model=Dict[str, Any])
async def get_run(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """查询运行状态"""
    entity = await session.get(WorkflowRun, run_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流运行 {run_id} 不存在",
        )
    return _run_entity_to_dict(entity)


@router.get("/runs/{run_id}/node-states", response_model=Dict[str, Any])
async def get_run_node_states(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """查询节点级执行状态 (供前端时间线展示)"""
    entity = await session.get(WorkflowRun, run_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工作流运行 {run_id} 不存在",
        )
    return {
        "run_id": entity.id,
        "workflow_id": entity.workflow_id,
        "thread_id": entity.thread_id,
        "status": entity.status,
        "node_states": entity.node_states or {},
    }


@router.get("/{workflow_id}/runs", response_model=Dict[str, Any])
async def list_workflow_runs(
    workflow_id: str,
    request: Request,
    status_filter: Optional[str] = Query(
        None, alias="status", description="按状态过滤"
    ),
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
    session: AsyncSession = Depends(get_db),
):
    """查询工作流的运行历史 (按创建时间倒序)"""
    stmt = select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
    if status_filter:
        stmt = stmt.where(WorkflowRun.status == status_filter)
    stmt = stmt.order_by(WorkflowRun.created_at.desc()).limit(limit)

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return {
        "items": [_run_entity_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.post("/{workflow_id}/validate", response_model=Dict[str, Any])
async def validate_workflow(
    workflow_id: str,
    payload: WorkflowValidateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """验证 graph 合法性

    若 payload.graph 提供, 用之; 否则用已存 workflow 的 graph
    """
    graph = payload.graph
    if graph is None:
        entity = await session.get(Workflow, workflow_id)
        if entity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"工作流 {workflow_id} 不存在",
            )
        graph = entity.graph

    engine = WorkflowEngine()
    errors = engine.validate(graph)
    return {
        "workflow_id": workflow_id,
        "valid": len(errors) == 0,
        "errors": errors,
    }
