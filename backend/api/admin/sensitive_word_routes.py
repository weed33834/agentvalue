"""敏感词字典管理 Admin API

路由前缀: /api/v1/admin/sensitive-words
权限: Role.ADMIN

完整端点:
- GET    /            - 分页列表 (支持 category 过滤)
- POST   /            - 添加敏感词
- POST   /batch       - 批量添加
- DELETE /{word_id}   - 删除
- POST   /check       - 检查文本 (body: {"text": "..."})
- POST   /filter      - 过滤文本
- POST   /import      - 导入敏感词表
- GET    /export      - 导出敏感词表
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService
from services.sensitive_word_service import (
    VALID_CATEGORIES,
    SensitiveWordService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/sensitive-words",
    tags=["admin-sensitive-words"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class WordCreate(BaseModel):
    """添加敏感词请求"""

    word: str = Field(..., description="敏感词文本")
    category: str = Field(default="custom", description="分类")
    severity: str = Field(default="medium", description="严重程度: low/medium/high")
    action: str = Field(default="mask", description="处理动作: block/replace/mask")
    replacement: Optional[str] = Field(
        default=None, description="替换文本 (action=replace 时使用)"
    )


class WordBatchCreate(BaseModel):
    """批量添加敏感词请求"""

    words: List[Dict[str, Any]] = Field(..., description="敏感词列表")


class TextCheckRequest(BaseModel):
    """文本检查请求"""

    text: str = Field(..., description="待检查文本")


class TextFilterRequest(BaseModel):
    """文本过滤请求"""

    text: str = Field(..., description="待过滤文本")


class ImportRequest(BaseModel):
    """导入敏感词请求"""

    file_content: str = Field(..., description="文件内容字符串")
    format: str = Field(default="csv", description="格式: csv / json")


# ============================================================
# 路由
# ============================================================


@router.get("/", response_model=Dict[str, Any])
async def list_words(
    request: Request,
    session: AsyncSession = Depends(get_db),
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    page: int = Query(default=1, ge=1, description="页码 (从 1 开始)"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
):
    """分页查询敏感词列表 (支持 category 过滤)"""
    if category and category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的分类: {category}, 可选: {VALID_CATEGORIES}",
        )
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    return await service.list_words(category=category, page=page, size=size, tenant_id=tenant_id)


@router.post(
    "/",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def add_word(
    payload: WordCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """添加敏感词"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    try:
        word = await service.add_word(
            word=payload.word,
            category=payload.category,
            severity=payload.severity,
            action=payload.action,
            replacement=payload.replacement,
            created_by=current_user_id,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="add_sensitive_word",
        details={"word": payload.word, "category": payload.category},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return SensitiveWordService._word_to_dict(word)


@router.post("/batch", response_model=Dict[str, Any])
async def batch_add_words(
    payload: WordBatchCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """批量添加敏感词"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    result = await service.batch_add_words(
        payload.words, created_by=current_user_id, tenant_id=tenant_id
    )

    await audit_service.log(
        actor_id=current_user_id,
        action="batch_add_sensitive_words",
        details={"count": len(payload.words), "added": result["added"]},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.delete("/{word_id}", response_model=Dict[str, Any])
async def remove_word(
    word_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除敏感词"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    deleted = await service.remove_word(word_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"敏感词 {word_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_sensitive_word",
        details={"word_id": word_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return {"deleted": True, "word_id": word_id}


@router.post("/check", response_model=Dict[str, Any])
async def check_text(
    payload: TextCheckRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """检查文本中的敏感词 (返回命中详情)"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    matches = await service.check_text(payload.text, tenant_id=tenant_id)
    return {
        "text": payload.text,
        "matches": matches,
        "hit_count": len(matches),
        "has_sensitive": len(matches) > 0,
    }


@router.post("/filter", response_model=Dict[str, Any])
async def filter_text(
    payload: TextFilterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """过滤文本 (按 action 处理: block/replace/mask)"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    filtered = await service.filter_text(payload.text, tenant_id=tenant_id)
    # 判断是否被拦截
    blocked = filtered.startswith("[内容包含违禁词, 已拦截]")
    return {
        "original": payload.text,
        "filtered": filtered,
        "blocked": blocked,
        "changed": filtered != payload.text,
    }


@router.post("/import", response_model=Dict[str, Any])
async def import_words(
    payload: ImportRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """导入敏感词表 (支持 CSV / JSON 格式)"""
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    try:
        result = await service.import_words(
            payload.file_content, payload.format, created_by=current_user_id, tenant_id=tenant_id
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"导入失败: {e}",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="import_sensitive_words",
        details={
            "format": payload.format,
            "added": result["added"],
            "skipped": result["skipped"],
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    return result


@router.get("/export", response_class=PlainTextResponse)
async def export_words(
    request: Request,
    session: AsyncSession = Depends(get_db),
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    format: str = Query(default="csv", description="格式: csv / json"),
):
    """导出敏感词表 (CSV / JSON)"""
    if category and category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的分类: {category}, 可选: {VALID_CATEGORIES}",
        )
    tenant_id = get_current_tenant()
    service = SensitiveWordService(session)
    try:
        content = await service.export_words(category=category, format=format, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )

    media_type = "text/csv" if format.lower() == "csv" else "application/json"
    filename = f"sensitive_words.{format.lower()}"
    return PlainTextResponse(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
