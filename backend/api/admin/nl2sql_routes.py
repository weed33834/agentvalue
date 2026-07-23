"""NL2SQL 自然语言转 SQL Admin API

路由前缀: /api/v1/admin/nl2sql
权限: Role.ADMIN / Role.HR

完整端点:
- POST   /generate             - 生成 SQL
- POST   /execute              - 执行 SQL (只读)
- POST   /generate-and-execute - 生成并执行
- GET    /queries              - 查询历史
- GET    /queries/{id}         - 查询详情
- DELETE /queries/{id}         - 删除查询
- POST   /schemas              - 创建 schema 定义
- GET    /schemas              - schema 列表
- PUT    /schemas/{id}         - 更新 schema
- DELETE /schemas/{id}         - 删除 schema

安全: 所有 SQL 执行前必须通过 _validate_sql (只允许 SELECT/WITH, 禁止 DDL/DML)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_app_state
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.nl2sql_service import NL2SQLService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/nl2sql",
    tags=["admin-nl2sql"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# Schemas
# ============================================================


class GenerateSQLRequest(BaseModel):
    """生成 SQL 请求"""

    natural_query: str = Field(..., description="自然语言查询")
    table_name: Optional[str] = Field(None, description="目标表名 (用于获取 schema)")


class ExecuteSQLRequest(BaseModel):
    """执行 SQL 请求"""

    sql: str = Field(..., description="待执行的 SQL (只读 SELECT)")
    table_name: Optional[str] = None
    query_id: Optional[int] = Field(None, description="关联的查询记录 ID")


class GenerateAndExecuteRequest(BaseModel):
    """生成并执行请求"""

    natural_query: str = Field(..., description="自然语言查询")
    table_name: Optional[str] = None


class SchemaCreate(BaseModel):
    """创建 schema 定义请求"""

    table_name: str = Field(..., description="表名 (租户内唯一)")
    schema_definition: dict = Field(..., description="表结构定义 JSON")
    description: Optional[str] = None
    sample_queries: Optional[List[str]] = None
    enabled: bool = True


class SchemaUpdate(BaseModel):
    """更新 schema 定义请求"""

    schema_definition: Optional[dict] = None
    description: Optional[str] = None
    sample_queries: Optional[List[str]] = None
    enabled: Optional[bool] = None


# ============================================================
# SQL 生成与执行
# ============================================================


def _get_llm_provider(app_state: Any) -> Any:
    """从 AppState 获取 LLM Provider (L0 低延迟)"""
    if app_state is None or getattr(app_state, "model_router", None) is None:
        return None
    try:
        return app_state.model_router.get_provider("L0")
    except Exception:
        try:
            return app_state.model_router.get_provider_with_fallback()
        except Exception:
            return None


@router.post("/generate", response_model=Dict[str, Any])
async def generate_sql(
    payload: GenerateSQLRequest,
    session: AsyncSession = Depends(get_db),
    app_state: Any = Depends(get_app_state),
    user_id: str = Depends(get_current_user_id),
):
    """生成 SQL (自然语言 → SQL)

    用 LLM 将自然语言转为 SQL, 返回 SQL + 解释 (不执行)。
    """
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    llm_provider = _get_llm_provider(app_state)
    try:
        query = await service.generate_sql(
            payload.natural_query,
            payload.table_name,
            llm_provider=llm_provider,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    # 记录创建人
    if query.created_by is None:
        query.created_by = user_id
        await session.flush()
    await session.commit()
    return NL2SQLService._query_to_dict(query)


@router.post("/execute", response_model=Dict[str, Any])
async def execute_sql(
    payload: ExecuteSQLRequest,
    session: AsyncSession = Depends(get_db),
):
    """执行 SQL (只读查询)

    执行前进行 SQL 安全验证 (只允许 SELECT/WITH)。
    """
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    try:
        result = await service.execute_sql(
            payload.sql,
            payload.table_name,
            query_id=payload.query_id,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return result


@router.post("/generate-and-execute", response_model=Dict[str, Any])
async def generate_and_execute_sql(
    payload: GenerateAndExecuteRequest,
    session: AsyncSession = Depends(get_db),
    app_state: Any = Depends(get_app_state),
    user_id: str = Depends(get_current_user_id),
):
    """生成并执行 SQL (自然语言 → SQL → 执行 → 返回结果)

    一步到位: LLM 生成 SQL → 安全验证 → 执行 → 返回结果数据。
    """
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    llm_provider = _get_llm_provider(app_state)
    try:
        query = await service.generate_sql(
            payload.natural_query,
            payload.table_name,
            llm_provider=llm_provider,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if query.created_by is None:
        query.created_by = user_id
        await session.flush()

    # 如果生成失败, 直接返回
    if query.status != "success" or not query.generated_sql:
        await session.commit()
        return {
            "query": NL2SQLService._query_to_dict(query),
            "execution": None,
        }

    # 执行生成的 SQL
    try:
        execution = await service.execute_sql(
            query.generated_sql,
            payload.table_name,
            query_id=query.id,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        await session.commit()
        return {
            "query": NL2SQLService._query_to_dict(query),
            "execution": {
                "success": False,
                "error": str(e),
                "rows": [],
                "columns": [],
                "row_count": 0,
            },
        }
    await session.commit()
    return {
        "query": NL2SQLService._query_to_dict(query),
        "execution": execution,
    }


# ============================================================
# 查询历史
# ============================================================


@router.get("/queries", response_model=Dict[str, Any])
async def list_queries(
    status_filter: Optional[str] = Query(None, alias="status", description="状态过滤"),
    table_name: Optional[str] = Query(None, description="表名过滤"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
):
    """查询历史列表 (分页)"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    return await service.list_queries(
        status_filter=status_filter,
        table_name=table_name,
        page=page,
        size=size,
        tenant_id=tenant_id,
    )


@router.get("/queries/{query_id}", response_model=Dict[str, Any])
async def get_query(
    query_id: int,
    session: AsyncSession = Depends(get_db),
):
    """查询详情"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    query = await service.get_query(query_id, tenant_id=tenant_id)
    if query is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"查询 {query_id} 不存在",
        )
    return NL2SQLService._query_to_dict(query)


@router.delete("/queries/{query_id}", response_model=Dict[str, Any])
async def delete_query(
    query_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除查询记录"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    deleted = await service.delete_query(query_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"查询 {query_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": query_id}


# ============================================================
# Schema 管理
# ============================================================


@router.post(
    "/schemas", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_schema(
    payload: SchemaCreate,
    session: AsyncSession = Depends(get_db),
):
    """创建表结构定义"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    try:
        schema = await service.create_schema(
            payload.table_name,
            payload.schema_definition,
            description=payload.description,
            sample_queries=payload.sample_queries,
            enabled=payload.enabled,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return NL2SQLService._schema_to_dict(schema)


@router.get("/schemas", response_model=Dict[str, Any])
async def list_schemas(
    session: AsyncSession = Depends(get_db),
):
    """schema 列表"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    schemas = await service.list_schemas(tenant_id=tenant_id)
    return {
        "items": [NL2SQLService._schema_to_dict(s) for s in schemas],
        "total": len(schemas),
    }


@router.put("/schemas/{schema_id}", response_model=Dict[str, Any])
async def update_schema(
    schema_id: int,
    payload: SchemaUpdate,
    session: AsyncSession = Depends(get_db),
):
    """更新 schema 定义"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    try:
        schema = await service.update_schema(
            schema_id,
            schema_definition=payload.schema_definition,
            description=payload.description,
            sample_queries=payload.sample_queries,
            enabled=payload.enabled,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return NL2SQLService._schema_to_dict(schema)


@router.delete("/schemas/{schema_id}", response_model=Dict[str, Any])
async def delete_schema(
    schema_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除 schema 定义"""
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    deleted = await service.delete_schema(schema_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"schema {schema_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": schema_id}
