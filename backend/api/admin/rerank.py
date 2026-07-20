"""
Rerank Admin API (P2-2, 对标 Dify Rerank 测试台)

路由前缀: /api/v1/admin/rerank
权限: Role.ADMIN (router 级 dependencies)

端点:
- POST /test - rerank 测试台, 输入 query + documents, 返回 reranked 结果
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.deps import AppState, get_app_state
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from services.audit_service import AuditService

from api.deps import get_audit_service
from core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/rerank",
    tags=["admin-rerank"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class RerankTestRequest(BaseModel):
    """Rerank 测试台请求体"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    documents: List[str] = Field(
        ..., min_length=1, max_length=100, description="待重排序的文档列表(纯文本)"
    )
    top_k: Optional[int] = Field(
        default=None, ge=1, le=100, description="返回前 top_k 个, 默认全部"
    )


class RerankedItem(BaseModel):
    """单个 rerank 结果项"""

    index: int = Field(..., description="原始 documents 中的下标")
    document: str = Field(..., description="原文档文本")
    rerank_score: float = Field(..., description="相关性分数, 越大越相关")


# ============================================================
# 端点
# ============================================================


@router.post("/test")
async def test_rerank(
    payload: RerankTestRequest,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
):
    """Rerank 测试台: 调用当前配置的 rerank provider 对 documents 重排序

    返回 {reranked: [{index, document, rerank_score}], provider: <name>}

    用途:
    - 在 Admin LLM 配置页测试 Rerank Provider 是否可用
    - 验证 query 与候选 documents 的相关性排序质量
    - 对比不同 rerank 模型(cohere/jina/bge)的排序效果

    鉴权: require_role(ADMIN)
    """
    reranker = app_state.rerank_provider
    if reranker is None:
        # 兜底: AppState 初始化失败时也应能给出明确错误
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="rerank provider 未初始化",
        )
    # 包装成 dict 列表便于 provider 处理(支持 content/text/page_content 字段)
    docs: List[Dict[str, Any]] = [{"content": d} for d in payload.documents]
    top_k = payload.top_k if payload.top_k is not None else len(docs)
    try:
        reranked_docs = await reranker.rerank(
            query=payload.query, documents=docs, top_k=top_k
        )
    except NotImplementedError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前 rerank provider 不可用: {e}",
        )
    except Exception as e:
        logger.exception("rerank 测试失败")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"rerank 调用失败: {e}",
        )
    # 还原成 (index, document, rerank_score) 三元组, index 指向原 documents 下标
    reranked: List[Dict[str, Any]] = []
    for doc in reranked_docs:
        content = doc.get("content", "")
        # DummyRerankProvider 等实现保持原顺序, index 通过 content 在原列表中定位
        try:
            idx = payload.documents.index(content)
        except ValueError:
            idx = -1
        reranked.append(
            {
                "index": idx,
                "document": content,
                "rerank_score": float(doc.get("rerank_score", 0.0)),
            }
        )
    # 审计日志: 仅记录测试行为与 provider 名, 不记录 documents 内容
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="admin_test_rerank",
        details={
            "provider": reranker.name,
            "query_len": len(payload.query),
            "documents_count": len(payload.documents),
            "top_k": top_k,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "reranked": reranked,
        "provider": reranker.name,
    }
