"""混合检索 Admin API

路由前缀: /api/v1/admin/search
权限: Role.ADMIN (router 级 dependencies)

端点:
- POST /hybrid  - 混合检索（向量 + BM25，alpha 控制权重，RRF 融合）
- POST /vector  - 纯向量检索
- POST /bm25    - 纯 BM25 全文检索
- GET  /config  - 获取当前检索配置（alpha 默认值、是否启用 BM25 等）
- PUT  /config  - 更新检索配置
- POST /documents/{document_id}/incremental-update - 文档增量更新
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AppState, get_app_state, get_audit_service
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.models import SearchConfig
from services.audit_service import AuditService
from services.hybrid_search_service import HybridSearchService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/search",
    tags=["admin-search"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# 默认配置（与 services/hybrid_search_service._DEFAULT_CONFIG 对齐）
# ============================================================

_DEFAULT_CONFIG_MAP = {
    "default_alpha": {
        "value": "0.5",
        "description": "混合检索默认权重（0=纯BM25, 1=纯向量, 0.5=等权混合）",
    },
    "bm25_enabled": {"value": "true", "description": "是否启用 BM25 全文检索"},
    "rrf_k": {"value": "60", "description": "RRF (Reciprocal Rank Fusion) 常数 k"},
    "bm25_k1": {"value": "1.5", "description": "BM25 参数 k1（词频饱和度）"},
    "bm25_b": {"value": "0.75", "description": "BM25 参数 b（文档长度归一化）"},
}


# ============================================================
# Pydantic 请求/响应模型
# ============================================================


class HybridSearchRequest(BaseModel):
    """混合检索请求体"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    top_k: int = Field(default=5, ge=1, le=100, description="返回结果数")
    alpha: float = Field(
        default=0.5, ge=0.0, le=1.0, description="向量/BM25权重（0=纯BM25, 1=纯向量）"
    )
    metadata_filter: Optional[Dict[str, Any]] = Field(
        default=None, description='元数据过滤条件，如 {"source": "hr_manual"}'
    )
    collection_name: Optional[str] = Field(
        default=None, max_length=128, description="ChromaDB collection 名称"
    )


class VectorSearchRequest(BaseModel):
    """纯向量检索请求体"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    top_k: int = Field(default=5, ge=1, le=100, description="返回结果数")
    metadata_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="元数据过滤条件"
    )
    collection_name: Optional[str] = Field(
        default=None, max_length=128, description="ChromaDB collection 名称"
    )


class BM25SearchRequest(BaseModel):
    """纯 BM25 检索请求体"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    top_k: int = Field(default=5, ge=1, le=100, description="返回结果数")
    metadata_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="元数据过滤条件"
    )
    collection_name: Optional[str] = Field(
        default=None, max_length=128, description="ChromaDB collection 名称"
    )


class UpdateSearchConfigRequest(BaseModel):
    """更新检索配置请求体"""

    model_config = ConfigDict(extra="forbid")

    default_alpha: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="混合检索默认权重"
    )
    bm25_enabled: Optional[bool] = Field(
        default=None, description="是否启用 BM25 全文检索"
    )
    rrf_k: Optional[int] = Field(default=None, ge=1, le=1000, description="RRF 常数 k")
    bm25_k1: Optional[float] = Field(
        default=None, ge=0.0, le=10.0, description="BM25 参数 k1"
    )
    bm25_b: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="BM25 参数 b"
    )


class IncrementalUpdateRequest(BaseModel):
    """文档增量更新请求体"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        ..., min_length=1, max_length=100000, description="新的文档全文"
    )
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="文档元数据")


# ============================================================
# 辅助函数
# ============================================================


def _get_search_service(app_state: AppState, tenant_id: str) -> HybridSearchService:
    """获取当前租户的 HybridSearchService 实例"""
    kb_store = app_state.get_kb_store(tenant_id)
    return HybridSearchService(kb_store=kb_store, settings=app_state.settings)


def _resolve_collection_name(
    app_state: AppState, tenant_id: str, collection_name: Optional[str]
) -> str:
    """解析 collection 名称：未指定时使用租户默认 KB collection 名

    H5: 强制校验 collection_name 归属当前租户, 防止跨租户访问知识库。
    只允许使用租户默认 collection (agentvalue_kb_{tenant_id}) 或以该前缀
    开头的子 collection; 用户提供的其它 collection 一律拒绝。
    """
    # 租户 collection 前缀 (租户隔离边界)
    tenant_prefix = f"agentvalue_kb_{tenant_id}"

    # 若调用方显式指定了 collection_name, 必须归属当前租户, 否则拒绝访问
    if collection_name:
        if collection_name == tenant_prefix or collection_name.startswith(
            tenant_prefix + "_"
        ):
            return collection_name
        # 非本租户 collection, 拒绝访问 (防止跨租户读取知识库)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问指定集合，仅允许访问当前租户的知识库",
        )

    # 未指定 collection_name: 使用租户默认 collection 名
    kb_store = app_state.get_kb_store(tenant_id)
    collection = getattr(kb_store, "collection", None)
    if collection is not None:
        try:
            return collection.name
        except Exception:
            pass
    return tenant_prefix


async def _load_search_config(session: AsyncSession, tenant_id: str) -> Dict[str, Any]:
    """从数据库加载检索配置，缺失项使用默认值"""
    stmt = select(SearchConfig).where(SearchConfig.tenant_id == tenant_id)
    result = await session.execute(stmt)
    db_configs = {row.config_key: row.config_value for row in result.scalars().all()}

    config: Dict[str, Any] = {}
    for key, default in _DEFAULT_CONFIG_MAP.items():
        raw_value = db_configs.get(key, default["value"])
        # 按类型转换
        if key in ("default_alpha", "bm25_k1", "bm25_b"):
            try:
                config[key] = float(raw_value)
            except (ValueError, TypeError):
                config[key] = float(default["value"])
        elif key in ("rrf_k",):
            try:
                config[key] = int(raw_value)
            except (ValueError, TypeError):
                config[key] = int(default["value"])
        elif key in ("bm25_enabled",):
            config[key] = str(raw_value).lower() in ("true", "1", "yes")
        else:
            config[key] = raw_value
    return config


async def _save_search_config(
    session: AsyncSession,
    tenant_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """保存检索配置到数据库（upsert 语义）"""
    changed: List[str] = []
    for key, value in updates.items():
        if value is None or key not in _DEFAULT_CONFIG_MAP:
            continue
        # 查找现有记录
        stmt = select(SearchConfig).where(
            SearchConfig.tenant_id == tenant_id,
            SearchConfig.config_key == key,
        )
        result = await session.execute(stmt)
        existing = result.scalars().first()

        str_value = str(value)
        if key == "bm25_enabled":
            str_value = "true" if value else "false"

        if existing:
            if existing.config_value != str_value:
                existing.config_value = str_value
                changed.append(key)
        else:
            new_config = SearchConfig(
                config_key=key,
                config_value=str_value,
                description=_DEFAULT_CONFIG_MAP[key]["description"],
                tenant_id=tenant_id,
            )
            session.add(new_config)
            changed.append(key)

    return changed


# ============================================================
# 端点
# ============================================================


@router.post("/hybrid")
async def hybrid_search(
    payload: HybridSearchRequest,
    app_state: AppState = Depends(get_app_state),
):
    """混合检索（向量 + BM25，alpha 控制权重，RRF 融合）

    返回 {results: [{content, score, metadata, source}], total: int}
    """
    tenant_id = get_current_tenant()
    service = _get_search_service(app_state, tenant_id)
    collection_name = _resolve_collection_name(
        app_state, tenant_id, payload.collection_name
    )
    try:
        results = await service.search(
            query=payload.query,
            collection_name=collection_name,
            top_k=payload.top_k,
            metadata_filter=payload.metadata_filter,
            alpha=payload.alpha,
        )
    except Exception as e:
        logger.exception("混合检索失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"混合检索失败: {e}",
        )
    return {"results": results, "total": len(results)}


@router.post("/vector")
async def vector_search(
    payload: VectorSearchRequest,
    app_state: AppState = Depends(get_app_state),
):
    """纯向量检索

    返回 {results: [{content, score, metadata, source}], total: int}
    """
    tenant_id = get_current_tenant()
    service = _get_search_service(app_state, tenant_id)
    collection_name = _resolve_collection_name(
        app_state, tenant_id, payload.collection_name
    )
    try:
        # alpha=1.0 即纯向量检索
        results = await service.search(
            query=payload.query,
            collection_name=collection_name,
            top_k=payload.top_k,
            metadata_filter=payload.metadata_filter,
            alpha=1.0,
        )
    except Exception as e:
        logger.exception("向量检索失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"向量检索失败: {e}",
        )
    return {"results": results, "total": len(results)}


@router.post("/bm25")
async def bm25_search(
    payload: BM25SearchRequest,
    app_state: AppState = Depends(get_app_state),
):
    """纯 BM25 全文检索

    返回 {results: [{content, score, metadata, source}], total: int}
    """
    tenant_id = get_current_tenant()
    service = _get_search_service(app_state, tenant_id)
    collection_name = _resolve_collection_name(
        app_state, tenant_id, payload.collection_name
    )
    try:
        # alpha=0.0 即纯 BM25 检索
        results = await service.search(
            query=payload.query,
            collection_name=collection_name,
            top_k=payload.top_k,
            metadata_filter=payload.metadata_filter,
            alpha=0.0,
        )
    except Exception as e:
        logger.exception("BM25 检索失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"BM25 检索失败: {e}",
        )
    return {"results": results, "total": len(results)}


@router.get("/config")
async def get_search_config(
    session: AsyncSession = Depends(get_db),
):
    """获取当前检索配置（alpha 默认值、是否启用 BM25、RRF 参数等）"""
    tenant_id = get_current_tenant()
    config = await _load_search_config(session, tenant_id)
    # 附加描述信息
    config_with_desc = {}
    for key, value in config.items():
        config_with_desc[key] = {
            "value": value,
            "description": _DEFAULT_CONFIG_MAP.get(key, {}).get("description", ""),
        }
    return {"config": config_with_desc, "tenant_id": tenant_id}


@router.put("/config")
async def update_search_config(
    payload: UpdateSearchConfigRequest,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
):
    """更新检索配置（alpha 默认值、BM25 开关、RRF 参数等）"""
    tenant_id = get_current_tenant()

    # 收集要更新的字段
    updates: Dict[str, Any] = {}
    for field in (
        "default_alpha",
        "bm25_enabled",
        "rrf_k",
        "bm25_k1",
        "bm25_b",
    ):
        value = getattr(payload, field, None)
        if value is not None:
            updates[field] = value

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未提供任何要更新的配置字段",
        )

    changed = await _save_search_config(session, tenant_id, updates)

    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_update_search_config",
        details={"changed": changed, "updates": updates},
        ip_address=get_client_ip(request),
    )
    await session.commit()

    # 返回更新后的完整配置
    config = await _load_search_config(session, tenant_id)
    return {"config": config, "changed": changed, "tenant_id": tenant_id}


@router.post("/documents/{document_id}/incremental-update")
async def incremental_update_document(
    document_id: str,
    payload: IncrementalUpdateRequest,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
):
    """文档增量更新

    对比新旧内容的 hash，如果内容未变化则跳过；
    如果内容变化，通过 difflib 对比段落级差异，只重新分块和嵌入变化的部分。

    返回 {updated: bool, added: int, deleted: int, reason: str, content_hash: str}
    """
    tenant_id = get_current_tenant()
    service = _get_search_service(app_state, tenant_id)

    try:
        result = await service.incremental_update(
            document_id=document_id,
            content=payload.content,
            metadata=payload.metadata,
        )
    except Exception as e:
        logger.exception(f"文档增量更新失败: document_id={document_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"增量更新失败: {e}",
        )

    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_incremental_update_doc",
        details={"document_id": document_id, "result": result},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"document_id": document_id, **result}
