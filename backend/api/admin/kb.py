"""知识库管理 Admin API (P1-1)

参考已注册 admin 路由模块组织。完整功能:
- 文档 CRUD(分页 + 搜索 + 元信息)
- 重建向量索引(先删后建,按 chunk 配置分块嵌入)
- 检索测试台(RAG 召回验证)
- 分块配置管理(写入 settings + .env.runtime)

所有端点仅 admin 可访问 (require_role(Role.ADMIN))。
向后兼容: 不改动现有 /api/v1/kb 路由。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AppState, get_app_state, get_audit_service, get_evaluation_service
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.models import CompanyKB
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["admin-kb"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# chunk 配置可写字段(写入 .env.runtime,重启后自动加载)
_KB_CONFIG_FIELDS = ("chunk_size", "chunk_overlap", "embedding_model")


# ====== Pydantic 请求模型 ======


class CreateKBDocRequest(BaseModel):
    """创建知识库文档(可指定 chunk_size/chunk_overlap 元信息)"""

    model_config = ConfigDict(extra="forbid")

    kb_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20000)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    chunk_size: Optional[int] = Field(default=None, ge=100, le=2000)
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=500)


class UpdateKBDocRequest(BaseModel):
    """更新知识库文档(标题/内容/元信息)"""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=256)
    content: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    metadata: Optional[Dict[str, Any]] = None


class TestRetrievalRequest(BaseModel):
    """检索测试台请求体"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    tenant_id: Optional[str] = Field(default=None, max_length=64)


class UpdateKBConfigRequest(BaseModel):
    """更新 chunk 配置"""

    model_config = ConfigDict(extra="forbid")

    chunk_size: Optional[int] = Field(default=None, ge=100, le=2000)
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=500)
    embedding_model: Optional[str] = Field(default=None, max_length=128)


# ====== 序列化与辅助 ======


def _serialize_kb_doc(doc: CompanyKB) -> Dict[str, Any]:
    return {
        "kb_id": doc.kb_id,
        "title": doc.title,
        "content": doc.content,
        "metadata": doc.metadata_,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _snippet(text: str, max_len: int = 200) -> str:
    """截取内容片段用于列表展示"""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _chunk_text(content: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    """按 chunk_size 切分文本,带 chunk_overlap 重叠。

    step = chunk_size - chunk_overlap,确保不后退。
    """
    if chunk_size <= 0:
        return [content]
    chunks: List[str] = []
    start = 0
    text_len = len(content)
    while start < text_len:
        end = start + chunk_size
        chunks.append(content[start:end])
        if end >= text_len:
            break
        step = max(1, chunk_size - chunk_overlap)
        start += step
    return chunks


def _persist_kb_config_env(settings) -> None:
    """将 chunk 配置持久化到 .env.runtime,重启后自动加载。

    保留 .env.runtime 已有内容,只更新/追加 KB 配置字段。
    """
    runtime_path = os.path.join(os.getcwd(), ".env.runtime")
    existing: Dict[str, str] = {}
    if os.path.exists(runtime_path):
        try:
            with open(runtime_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        existing[k.strip().upper()] = v
        except Exception:
            logger.warning("读取 .env.runtime 失败,将覆盖重写", exc_info=True)
            existing = {}
    for field in _KB_CONFIG_FIELDS:
        val = getattr(settings, field, None)
        if val is None:
            continue
        existing[field.upper()] = str(val)
    try:
        lines = [f"{k}={v}" for k, v in existing.items()]
        with open(runtime_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        logger.exception("持久化 .env.runtime 失败")


# ====== 端点 ======


@router.get("/docs")
async def list_kb_docs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    search: Optional[str] = Query(None, max_length=128, description="标题/内容模糊搜索"),
    title: Optional[str] = Query(None, max_length=128, description="标题模糊搜索"),
    session: AsyncSession = Depends(get_db),
):
    """分页查询知识库文档,支持 search/title 过滤。

    返回 {items, total, page, page_size}。
    """
    tenant_id = get_current_tenant()
    base = select(CompanyKB).where(CompanyKB.tenant_id == tenant_id)
    keyword = search or title
    if keyword:
        like = f"%{keyword}%"
        base = base.where(
            or_(
                CompanyKB.title.like(like),
                CompanyKB.content.like(like),
            )
        )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0
    offset = (page - 1) * page_size
    stmt = base.order_by(CompanyKB.created_at.desc()).offset(offset).limit(page_size)
    docs = (await session.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                **_serialize_kb_doc(d),
                "content_snippet": _snippet(d.content),
            }
            for d in docs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/docs")
async def create_kb_doc(
    payload: CreateKBDocRequest,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
):
    """创建知识库文档并写入向量库。

    支持在 metadata 中携带 chunk_size/chunk_overlap(后续 reindex 时使用)。
    """
    existing = await eval_service.get_kb_doc(payload.kb_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"知识库文档 kb_id={payload.kb_id} 已存在",
        )
    metadata = dict(payload.metadata or {})
    if payload.chunk_size is not None:
        metadata["chunk_size"] = payload.chunk_size
    if payload.chunk_overlap is not None:
        metadata["chunk_overlap"] = payload.chunk_overlap
    doc = await eval_service.create_kb_doc(
        {
            "kb_id": payload.kb_id,
            "title": payload.title,
            "content": payload.content,
            "metadata": metadata,
        }
    )
    # 同步写入向量库(失败不阻断 DB 创建,可后续 reindex)
    store = app_state.get_kb_store(get_current_tenant())
    try:
        await _index_doc(store, doc, app_state.settings)
    except Exception as e:
        logger.warning(f"向量库写入失败(忽略,可后续 reindex): {e}")
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_create_kb_doc",
        details={"kb_id": doc.kb_id, "title": doc.title},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_kb_doc(doc)


@router.get("/docs/{kb_id}")
async def get_kb_doc(
    kb_id: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """查询知识库文档详情"""
    doc = await eval_service.get_kb_doc(kb_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    return _serialize_kb_doc(doc)


@router.put("/docs/{kb_id}")
async def update_kb_doc(
    kb_id: str,
    payload: UpdateKBDocRequest,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
):
    """更新知识库文档(标题/内容/元信息)"""
    doc = await eval_service.get_kb_doc(kb_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    if payload.title is not None:
        doc.title = payload.title
    if payload.content is not None:
        doc.content = payload.content
    if payload.metadata is not None:
        doc.metadata_ = payload.metadata
    await session.flush()
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_update_kb_doc",
        details={"kb_id": kb_id},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_kb_doc(doc)


@router.delete("/docs/{kb_id}")
async def delete_kb_doc(
    kb_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
):
    """删除知识库文档及向量库索引"""
    doc = await eval_service.get_kb_doc(kb_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    await eval_service.delete_kb_doc(kb_id)
    # 同步删除向量库(失败不阻断 DB 删除)
    store = app_state.get_kb_store(get_current_tenant())
    try:
        await _delete_doc_vectors(store, kb_id)
    except Exception as e:
        logger.warning(f"向量库删除失败(忽略): {e}")
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_delete_kb_doc",
        details={"kb_id": kb_id},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"kb_id": kb_id, "deleted": True}


@router.post("/docs/{kb_id}/reindex")
async def reindex_kb_doc(
    kb_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
):
    """重建该知识库文档的向量索引。

    流程: 删除现有向量 → 按当前 chunk 配置分块重新嵌入。
    失败返回 422 + detail。
    """
    doc = await eval_service.get_kb_doc(kb_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    store = app_state.get_kb_store(get_current_tenant())
    settings = app_state.settings
    try:
        await _delete_doc_vectors(store, kb_id)
        await _index_doc(store, doc, settings)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"重建索引失败 kb_id={kb_id}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"重建索引失败: {e}",
        )
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_reindex_kb_doc",
        details={"kb_id": kb_id},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"kb_id": kb_id, "reindexed": True}


@router.post("/test-retrieval")
async def test_retrieval(
    payload: TestRetrievalRequest,
    app_state: AppState = Depends(get_app_state),
):
    """检索测试台: 用 query 在向量库做 top_k 召回,返回匹配结果。

    返回 {matches: [{kb_id, title, content_snippet, score, metadata}]}。
    """
    tenant_id = payload.tenant_id or get_current_tenant()
    store = app_state.get_kb_store(tenant_id)
    try:
        results = await store.query(payload.query, top_k=payload.top_k)
    except Exception as e:
        logger.exception("检索测试台查询失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"检索失败: {e}",
        )
    matches: List[Dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        score = r.get("_retrieval_score")
        if score is None:
            score = r.get("score", 0.0)
        content = r.get("content", "") or ""
        metadata = r.get("metadata", {}) or {}
        # metadata 可能是 JSON 字符串(ChromaCompanyKB 存储时序列化)
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                pass
        matches.append(
            {
                "kb_id": r.get("kb_id", ""),
                "title": r.get("title", ""),
                "content_snippet": _snippet(content, max_len=300),
                "score": float(score),
                "metadata": metadata,
            }
        )
    return {"matches": matches}


@router.get("/config")
async def get_kb_config(
    app_state: AppState = Depends(get_app_state),
):
    """获取当前 chunk 配置(chunk_size/chunk_overlap/embedding_model)"""
    s = app_state.settings
    return {
        "chunk_size": getattr(s, "chunk_size", 800),
        "chunk_overlap": getattr(s, "chunk_overlap", 100),
        "embedding_model": getattr(s, "embedding_model", ""),
    }


@router.put("/config")
async def update_kb_config(
    payload: UpdateKBConfigRequest,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
):
    """更新 chunk 配置(写入 settings + .env.runtime,重启后自动加载)"""
    async with app_state._settings_lock:
        changed: List[str] = []
        for field in _KB_CONFIG_FIELDS:
            new_val = getattr(payload, field, None)
            if new_val is None:
                continue
            old_val = getattr(app_state.settings, field, None)
            if old_val != new_val:
                setattr(app_state.settings, field, new_val)
                changed.append(field)
        if changed:
            try:
                _persist_kb_config_env(app_state.settings)
            except Exception:
                logger.exception("持久化 KB 配置失败")
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_update_kb_config",
        details={"fields": changed},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "chunk_size": getattr(app_state.settings, "chunk_size", 800),
        "chunk_overlap": getattr(app_state.settings, "chunk_overlap", 100),
        "embedding_model": getattr(app_state.settings, "embedding_model", ""),
        "changed": changed,
    }


# ====== 内部辅助: 向量库操作(兼容 DummyCompanyKB / ChromaCompanyKB) ======


async def _index_doc(store, doc: CompanyKB, settings) -> None:
    """按 chunk 配置切分并写入向量库。

    ChromaCompanyKB 有 add_document 方法;DummyCompanyKB 没有,跳过(测试场景)。
    """
    add_doc = getattr(store, "add_document", None)
    if add_doc is None:
        # 测试场景的 DummyCompanyKB 没有 add_document,记录后跳过
        logger.debug(f"store 无 add_document 方法,跳过向量写入: {type(store)}")
        return
    chunk_size = getattr(settings, "chunk_size", 800) or 800
    chunk_overlap = getattr(settings, "chunk_overlap", 100) or 0
    metadata = doc.metadata_ or {}
    content = doc.content or ""
    chunks = _chunk_text(content, chunk_size, chunk_overlap)
    if not chunks:
        chunks = [content]
    if len(chunks) == 1:
        await add_doc(
            kb_id=doc.kb_id,
            title=doc.title,
            content=content,
            metadata=metadata,
        )
        return
    # 多块: 用 {kb_id}__chunk_{i} 作为 id,metadata 标记 parent_kb_id 便于批量删除
    for i, chunk in enumerate(chunks):
        chunk_id = f"{doc.kb_id}__chunk_{i}"
        chunk_meta = dict(metadata)
        chunk_meta["parent_kb_id"] = doc.kb_id
        chunk_meta["chunk_index"] = i
        chunk_meta["chunk_total"] = len(chunks)
        await add_doc(
            kb_id=chunk_id,
            title=f"{doc.title} (chunk {i + 1}/{len(chunks)})",
            content=chunk,
            metadata=chunk_meta,
        )


async def _delete_doc_vectors(store, kb_id: str) -> None:
    """删除该 kb_id 的所有向量(含 chunk 子文档)。

    ChromaCompanyKB 通过 collection.delete 删除;DummyCompanyKB 无 collection,跳过。
    """
    collection = getattr(store, "collection", None)
    if collection is None:
        return
    # 先按 metadata.parent_kb_id 删除所有 chunk 子文档
    await asyncio.to_thread(
        collection.delete,
        where={"parent_kb_id": kb_id},
    )
    # 再删除主文档
    await asyncio.to_thread(
        collection.delete,
        ids=[kb_id],
    )
