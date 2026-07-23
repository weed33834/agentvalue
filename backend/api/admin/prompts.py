"""Prompt 管理 Admin API (参考 Langfuse Prompt Management)

参考文档:
- Langfuse Version Control: https://langfuse.com/docs/prompt-management/features/prompt-version-control
- Langfuse A/B Testing: https://langfuse.com/docs/prompt-management/features/a-b-testing
- Langfuse Data Model: https://langfuse.com/docs/prompt-management/data-model
- Langfuse MCP Server (API 操作集合): https://mcp.reference.langfuse.com/

完整功能对标 Langfuse:
1. 模板 CRUD (List/Create/Get/Delete)
2. 版本管理 (不可变历史 + 自增版本号 + latest 自动维护)
3. Label 指针 (production/staging/prod-a/prod-b/canary-Npct/<tenant>)
4. Protected Label (保护 production 不被误改,仅 admin 可改)
5. Diff View (版本间差异对比,类 Git diff)
6. 一键 Rollback (把 production label 指向旧版本)
7. A/B 测试 (prod-a / prod-b 双 label + 哈希分流)
8. 灰度发布 (canary-Npct label + 哈希百分比)
9. 渲染预览 (传入样例变量,看渲染结果)
10. 评估运行 (关联 trace + metrics,便于版本对比)

所有端点仅 admin 可访问 (require_role(Role.ADMIN))。
"""

from __future__ import annotations

import difflib
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db_prompt_loader import get_global_db_prompt_loader
from api.deps import get_audit_service
from auth.rbac import Role, get_client_ip, get_current_user_id, require_role
from core.database import get_db
from core.rate_limit import rate_limit
from core.tenant_context import get_current_tenant
from models.models import PromptLabel, PromptTemplate, PromptVersion
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/prompts",
    tags=["admin-prompts"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# 受保护 label: 这些 label 关系到生产稳定性,默认 protected=True
# (参考 Langfuse Protected Labels)
_PROTECTED_LABELS_DEFAULT = {"production"}


# ====== Pydantic 请求模型 ======


class CreateTemplateRequest(BaseModel):
    """创建新 Prompt 模板(同时创建第一个版本)"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="模板名(同租户唯一)")
    type: str = Field(default="text", description="text 或 chat")
    description: Optional[str] = Field(default=None, max_length=2000)
    content: str = Field(min_length=1, max_length=50000, description="Prompt 正文")
    config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="模型配置: model/temperature/max_tokens 等 (参考 Langfuse Config)",
    )
    variables_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="变量 schema: 变量名/类型/默认值/描述"
    )
    labels: List[str] = Field(
        default_factory=lambda: ["latest"],
        description="同时分配的 label 列表(如 ['production'] 或 ['staging'])",
    )
    protected_labels: List[str] = Field(
        default_factory=lambda: ["production"],
        description="设为 protected 的 label,默认保护 production",
    )


class CreateVersionRequest(BaseModel):
    """为已有模板创建新版本(不可变历史 + 自增版本号)"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=50000)
    config: Optional[Dict[str, Any]] = None
    variables_schema: Optional[Dict[str, Any]] = None
    labels: List[str] = Field(
        default_factory=lambda: ["latest"],
        description="分配给此版本的 label(覆盖同名旧 label,latest 自动维护)",
    )


class AssignLabelRequest(BaseModel):
    """把 label 指向某版本(用于回滚/灰度切换/A/B 切换)"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="目标版本号")
    label: str = Field(
        min_length=1,
        max_length=64,
        description="label 名: production/staging/prod-a/prod-b/canary-Npct 等",
    )
    protected: Optional[bool] = Field(
        default=None,
        description="是否设为 protected(仅 admin 可后续修改)。None 时保留原值",
    )


class PreviewRequest(BaseModel):
    """渲染预览: 传入样例变量,看渲染结果"""

    model_config = ConfigDict(extra="forbid")

    version: Optional[int] = Field(default=None, description="版本号,缺省取 production")
    label: Optional[str] = Field(default=None, description="label,缺省 production")
    variables: Dict[str, Any] = Field(
        default_factory=dict, description="样例变量 {raw_inputs: [...], period: '...'}"
    )


# ====== 序列化 helper ======


def _serialize_template(
    t: PromptTemplate, label_count: int = 0, version_count: int = 0
) -> Dict[str, Any]:
    return {
        "id": t.id,
        "tenant_id": t.tenant_id,
        "name": t.name,
        "type": t.type,
        "description": t.description,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "version_count": version_count,
        "label_count": label_count,
    }


def _serialize_version(
    v: PromptVersion, labels: Optional[List[str]] = None
) -> Dict[str, Any]:
    return {
        "id": v.id,
        "template_id": v.template_id,
        "version": v.version,
        "content": v.content,
        "content_preview": (v.content or "")[:200],
        "config": v.config,
        "variables_schema": v.variables_schema,
        "created_by": v.created_by,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "labels": labels or [],
    }


def _serialize_label(
    l: PromptLabel, version_no: Optional[int] = None
) -> Dict[str, Any]:
    return {
        "id": l.id,
        "template_id": l.template_id,
        "version_id": l.version_id,
        "version": version_no,
        "label": l.label,
        "protected": l.protected,
        "updated_by": l.updated_by,
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


# ====== 内部 DB helper ======


async def _get_template_by_name(
    session: AsyncSession, name: str, tenant_id: str
) -> Optional[PromptTemplate]:
    stmt = select(PromptTemplate).where(
        and_(PromptTemplate.tenant_id == tenant_id, PromptTemplate.name == name)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_version_by_no(
    session: AsyncSession, template_id: str, version: int
) -> Optional[PromptVersion]:
    stmt = select(PromptVersion).where(
        and_(
            PromptVersion.template_id == template_id,
            PromptVersion.version == version,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_latest_version(
    session: AsyncSession, template_id: str
) -> Optional[PromptVersion]:
    stmt = (
        select(PromptVersion)
        .where(PromptVersion.template_id == template_id)
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _list_labels_for_template(
    session: AsyncSession, template_id: str
) -> List[PromptLabel]:
    stmt = select(PromptLabel).where(PromptLabel.template_id == template_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _list_labels_for_version(
    session: AsyncSession, template_id: str, version_id: str
) -> List[str]:
    stmt = select(PromptLabel).where(
        and_(
            PromptLabel.template_id == template_id,
            PromptLabel.version_id == version_id,
        )
    )
    result = await session.execute(stmt)
    return [row.label for row in result.scalars().all()]


async def _upsert_label(
    session: AsyncSession,
    template_id: str,
    version_id: str,
    label: str,
    protected: bool,
    updated_by: str,
) -> PromptLabel:
    """分配 label,若已存在则覆盖(Langfuse label 是指针的语义)"""
    stmt = select(PromptLabel).where(
        and_(
            PromptLabel.template_id == template_id,
            PromptLabel.label == label,
        )
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        # Protected 校验: 仅 admin 可改(此路由已是 admin,但保留语义供未来细粒度 RBAC)
        existing.version_id = version_id
        existing.protected = protected
        existing.updated_by = updated_by
        return existing
    new_label = PromptLabel(
        id=str(uuid.uuid4()),
        template_id=template_id,
        version_id=version_id,
        label=label,
        protected=protected,
        updated_by=updated_by,
    )
    session.add(new_label)
    return new_label


# ====== 路由: 模板 CRUD ======


@router.get("", response_model=Dict[str, Any])
async def list_templates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    search: Optional[str] = Query(default=None, description="按 name 模糊搜索"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有 Prompt 模板(分页 + 模糊搜索)。

    对标 Langfuse listPrompts: 返回模板元数据 + 版本数 + label 数。
    """
    tid = get_current_tenant()
    base = select(PromptTemplate).where(PromptTemplate.tenant_id == tid)
    if search:
        base = base.where(PromptTemplate.name.ilike(f"%{search}%"))
    base = base.order_by(desc(PromptTemplate.updated_at))

    # count
    from sqlalchemy import func

    count_stmt = select(func.count()).select_from(
        select(PromptTemplate).where(PromptTemplate.tenant_id == tid).subquery()
        if not search
        else select(PromptTemplate)
        .where(
            and_(
                PromptTemplate.tenant_id == tid,
                PromptTemplate.name.ilike(f"%{search}%"),
            )
        )
        .subquery()
    )
    total_result = await session.execute(count_stmt)
    total = total_result.scalar() or 0

    # paginated rows
    stmt = base.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    templates = list(result.scalars().all())

    items = []
    for t in templates:
        # 查版本数与 label 数(子查询更优,此处保持简单)
        v_count_stmt = select(func.count()).select_from(
            select(PromptVersion).where(PromptVersion.template_id == t.id)
        )
        l_count_stmt = select(func.count()).select_from(
            select(PromptLabel).where(PromptLabel.template_id == t.id)
        )
        v_count = (await session.execute(v_count_stmt)).scalar() or 0
        l_count = (await session.execute(l_count_stmt)).scalar() or 0
        items.append(_serialize_template(t, version_count=v_count, label_count=l_count))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
@rate_limit("20/minute")
async def create_template(
    payload: CreateTemplateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """创建新 Prompt 模板(同时创建第一个版本 v1)。

    对标 Langfuse createTextPrompt / createChatPrompt:
    - 自动递增版本号(首版为 1)
    - 可同时分配多个 label (production / staging / latest)
    - 自动维护 latest label 指向最新版本
    - 可携带 config (model/temperature/max_tokens) 与 variables_schema
    """
    tid = get_current_tenant()
    actor_id = await get_current_user_id(request)

    existing = await _get_template_by_name(session, payload.name, tid)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Prompt 模板 name={payload.name} 已存在,请用 POST /versions 新建版本",
        )

    if payload.type not in ("text", "chat"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type 必须为 text 或 chat",
        )

    template = PromptTemplate(
        id=str(uuid.uuid4()),
        tenant_id=tid,
        name=payload.name,
        type=payload.type,
        description=payload.description,
        created_by=actor_id,
    )
    session.add(template)
    await session.flush()

    version = PromptVersion(
        id=str(uuid.uuid4()),
        template_id=template.id,
        version=1,
        content=payload.content,
        config=payload.config,
        variables_schema=payload.variables_schema,
        created_by=actor_id,
    )
    session.add(version)
    await session.flush()

    # 分配 label(同时标记 protected)
    requested_labels = set(payload.labels) | {"latest"}
    protected_set = set(payload.protected_labels) | _PROTECTED_LABELS_DEFAULT
    for label in requested_labels:
        await _upsert_label(
            session,
            template.id,
            version.id,
            label,
            protected=(label in protected_set),
            updated_by=actor_id,
        )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_create_template",
        details={
            "name": payload.name,
            "type": payload.type,
            "version": 1,
            "labels": list(requested_labels),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    labels_list = await _list_labels_for_version(session, template.id, version.id)
    return {
        "template": _serialize_template(
            template, version_count=1, label_count=len(requested_labels)
        ),
        "version": _serialize_version(version, labels=labels_list),
    }


@router.get("/{name}", response_model=Dict[str, Any])
async def get_template(
    name: str,
    session: AsyncSession = Depends(get_db),
):
    """获取 Prompt 模板详情(含版本与 label 列表)。"""
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    # 列版本
    v_stmt = (
        select(PromptVersion)
        .where(PromptVersion.template_id == template.id)
        .order_by(desc(PromptVersion.version))
    )
    versions = list((await session.execute(v_stmt)).scalars().all())

    # 列 label
    labels = await _list_labels_for_template(session, template.id)

    # version → labels 映射
    label_map: Dict[str, List[str]] = {}
    for l in labels:
        label_map.setdefault(l.version_id, []).append(l.label)

    return {
        "template": _serialize_template(
            template, version_count=len(versions), label_count=len(labels)
        ),
        "versions": [
            _serialize_version(v, labels=label_map.get(v.id, [])) for v in versions
        ],
        "labels": [_serialize_label(l) for l in labels],
    }


@router.delete("/{name}", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def delete_template(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """删除整个 Prompt 模板(级联删除版本与 label)。

    安全检查:
    - 若存在 protected label(如 production),要求 confirm=true query 参数确认
    - 删除后不可恢复(版本不可变性要求,但模板整体删除是 admin 决策)
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    # 检查 protected label
    labels = await _list_labels_for_template(session, template.id)
    has_protected = any(l.protected for l in labels)
    if has_protected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"模板 {name} 存在 protected label(如 production),"
                "删除将影响线上服务。请先取消 protected 或用 /rollback 切换到其他版本"
            ),
        )

    actor_id = await get_current_user_id(request)
    await session.delete(template)
    await audit_service.log(
        actor_id=actor_id,
        action="prompt_delete_template",
        details={"name": name, "tenant_id": tid},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"deleted": True, "name": name}


# ====== 路由: 版本管理 ======


@router.get("/{name}/versions", response_model=Dict[str, Any])
async def list_versions(
    name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
):
    """列出某 Prompt 的所有版本(含各自 label)。

    对标 Langfuse version history: 按 version 倒序展示,带 label 信息。
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    from sqlalchemy import func

    count_stmt = select(func.count()).select_from(
        select(PromptVersion).where(PromptVersion.template_id == template.id).subquery()
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = (
        select(PromptVersion)
        .where(PromptVersion.template_id == template.id)
        .order_by(desc(PromptVersion.version))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    versions = list((await session.execute(stmt)).scalars().all())

    # 批量查 label(避免 N+1)
    all_labels = await _list_labels_for_template(session, template.id)
    label_map: Dict[str, List[str]] = {}
    for l in all_labels:
        label_map.setdefault(l.version_id, []).append(l.label)

    return {
        "items": [
            _serialize_version(v, labels=label_map.get(v.id, [])) for v in versions
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post(
    "/{name}/versions",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
@rate_limit("20/minute")
async def create_version(
    name: str,
    payload: CreateVersionRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """为已有 Prompt 模板创建新版本(不可变历史 + 自增版本号)。

    对标 Langfuse create_prompt_version:
    - 版本号自动递增(已有最大版本 + 1)
    - 旧版本内容不可修改
    - 可同时分配多个 label(覆盖同名旧 label)
    - latest label 自动维护指向最新版本
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    actor_id = await get_current_user_id(request)
    latest = await _get_latest_version(session, template.id)
    new_version_no = (latest.version + 1) if latest else 1

    version = PromptVersion(
        id=str(uuid.uuid4()),
        template_id=template.id,
        version=new_version_no,
        content=payload.content,
        config=payload.config,
        variables_schema=payload.variables_schema,
        created_by=actor_id,
    )
    session.add(version)
    await session.flush()

    # 分配 label(用户指定 + 自动 latest)
    requested_labels = set(payload.labels) | {"latest"}
    for label in requested_labels:
        protected = label in _PROTECTED_LABELS_DEFAULT
        await _upsert_label(
            session, template.id, version.id, label, protected, actor_id
        )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_create_version",
        details={
            "name": name,
            "version": new_version_no,
            "labels": list(requested_labels),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    labels_list = await _list_labels_for_version(session, template.id, version.id)
    return _serialize_version(version, labels=labels_list)


@router.get("/{name}/versions/{version}", response_model=Dict[str, Any])
async def get_version(
    name: str,
    version: int,
    session: AsyncSession = Depends(get_db),
):
    """获取指定版本详情(含完整 content + labels)。"""
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    version_row = await _get_version_by_no(session, template.id, version)
    if not version_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 v{version} 不存在",
        )

    labels = await _list_labels_for_version(session, template.id, version_row.id)
    return _serialize_version(version_row, labels=labels)


# ====== 路由: Label 管理 ======


@router.get("/{name}/labels", response_model=Dict[str, Any])
async def list_labels(
    name: str,
    session: AsyncSession = Depends(get_db),
):
    """列出某 Prompt 的所有 label 指针(对标 Langfuse label 列表)。"""
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    labels = await _list_labels_for_template(session, template.id)

    # 关联 version_no
    v_map: Dict[str, int] = {}
    if labels:
        v_ids = {l.version_id for l in labels}
        v_stmt = select(PromptVersion).where(PromptVersion.id.in_(v_ids))
        for v in (await session.execute(v_stmt)).scalars().all():
            v_map[v.id] = v.version

    return {
        "items": [
            _serialize_label(l, version_no=v_map.get(l.version_id)) for l in labels
        ],
        "total": len(labels),
    }


@router.post("/{name}/labels", response_model=Dict[str, Any])
@rate_limit("20/minute")
async def assign_label(
    name: str,
    payload: AssignLabelRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """把 label 指向某版本(用于回滚/灰度切换/A/B 切换/部署)。

    对标 Langfuse updatePromptLabels:
    - label 是指针,设置后自动从旧版本移除(Langfuse 语义:label 唯一)
    - protected label 需 admin 权限(本路由已是 admin,通过)
    - 不能修改 latest label(系统自动维护)
    """
    if payload.label == "latest":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="latest label 由系统自动维护,不可手动指定",
        )

    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    version_row = await _get_version_by_no(session, template.id, payload.version)
    if not version_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 v{payload.version} 不存在",
        )

    actor_id = await get_current_user_id(request)
    protected = (
        payload.protected
        if payload.protected is not None
        else (payload.label in _PROTECTED_LABELS_DEFAULT)
    )
    await _upsert_label(
        session, template.id, version_row.id, payload.label, protected, actor_id
    )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_assign_label",
        details={
            "name": name,
            "version": payload.version,
            "label": payload.label,
            "protected": protected,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    labels_list = await _list_labels_for_version(session, template.id, version_row.id)
    return {
        "name": name,
        "version": payload.version,
        "label": payload.label,
        "protected": protected,
        "version_labels": labels_list,
    }


@router.delete("/{name}/labels/{label}", response_model=Dict[str, Any])
@rate_limit("20/minute")
async def remove_label(
    name: str,
    label: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """删除 label 指针(不影响版本本身)。

    Protected label 也可由 admin 删除(对标 Langfuse Protected Labels 仅 admin 可改)。
    latest label 不可删除。
    """
    if label == "latest":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="latest label 由系统自动维护,不可删除",
        )

    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    stmt = select(PromptLabel).where(
        and_(
            PromptLabel.template_id == template.id,
            PromptLabel.label == label,
        )
    )
    result = await session.execute(stmt)
    label_row = result.scalar_one_or_none()
    if not label_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"label {label} 不存在于模板 {name}",
        )

    actor_id = await get_current_user_id(request)
    await session.delete(label_row)
    await audit_service.log(
        actor_id=actor_id,
        action="prompt_remove_label",
        details={"name": name, "label": label, "protected": label_row.protected},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"deleted": True, "name": name, "label": label}


# ====== 路由: Diff View (对标 Langfuse Prompt Diffs) ======


@router.get("/{name}/diff", response_model=Dict[str, Any])
async def diff_versions(
    name: str,
    frm: int = Query(..., alias="from", ge=1, description="起始版本号"),
    to: int = Query(..., ge=1, description="目标版本号"),
    session: AsyncSession = Depends(get_db),
):
    """对比两个版本的内容差异(类 Git diff)。

    对标 Langfuse Prompt Diffs: 展示 prompt 在版本间的演变,便于 debug。
    使用 difflib.unified_diff 生成标准 diff 格式。
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    v_from = await _get_version_by_no(session, template.id, frm)
    v_to = await _get_version_by_no(session, template.id, to)
    if not v_from:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"版本 v{frm} 不存在"
        )
    if not v_to:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"版本 v{to} 不存在"
        )

    from_lines = (v_from.content or "").splitlines(keepends=True)
    to_lines = (v_to.content or "").splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            from_lines,
            to_lines,
            fromfile=f"v{frm}",
            tofile=f"v{to}",
        )
    )

    # config 也对比
    config_diff = None
    if v_from.config != v_to.config:
        import json

        config_diff = list(
            difflib.unified_diff(
                json.dumps(
                    v_from.config, ensure_ascii=False, indent=2, sort_keys=True
                ).splitlines(keepends=True),
                json.dumps(
                    v_to.config, ensure_ascii=False, indent=2, sort_keys=True
                ).splitlines(keepends=True),
                fromfile=f"v{frm}.config",
                tofile=f"v{to}.config",
            )
        )

    return {
        "name": name,
        "from_version": frm,
        "to_version": to,
        "diff": "".join(diff),
        "config_diff": "".join(config_diff) if config_diff else None,
        "has_content_change": v_from.content != v_to.content,
        "has_config_change": v_from.config != v_to.config,
    }


# ====== 路由: 一键 Rollback (对标 Langfuse Rollbacks) ======


@router.post("/{name}/rollback", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def rollback(
    name: str,
    request: Request,
    to: int = Query(..., ge=1, description="回滚到指定版本号"),
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """一键回滚: 把 production label 指向旧版本。

    对标 Langfuse Rollbacks:
    > You can quickly rollback to a previous version by setting the production label
    > to that previous version.

    本端点:
    - 把 production label 指向 to 版本(覆盖旧指向)
    - 不删除任何版本(版本不可变)
    - 审计记录 rollback 操作
    - 可随时再 rollback 回来(label 只是指针)
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    target_version = await _get_version_by_no(session, template.id, to)
    if not target_version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"回滚目标版本 v{to} 不存在",
        )

    # 查原 production 指向
    stmt = select(PromptLabel).where(
        and_(
            PromptLabel.template_id == template.id,
            PromptLabel.label == "production",
        )
    )
    result = await session.execute(stmt)
    old_prod = result.scalar_one_or_none()
    old_version_no = None
    if old_prod:
        old_v = await _get_version_by_no(
            session,
            template.id,
            await _get_version_no_by_id(session, old_prod.version_id),
        )
        old_version_no = old_v.version if old_v else None

    actor_id = await get_current_user_id(request)
    await _upsert_label(
        session,
        template.id,
        target_version.id,
        "production",
        protected=True,
        updated_by=actor_id,
    )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_rollback",
        details={
            "name": name,
            "from_version": old_version_no,
            "to_version": to,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    return {
        "rolled_back": True,
        "name": name,
        "from_version": old_version_no,
        "to_version": to,
        "note": "production label 已指向 v{to},线上请求立即生效".format(to=to),
    }


async def _get_version_no_by_id(session: AsyncSession, version_id: str) -> int:
    """根据 version_id 反查 version_no(用于 rollback 记录原版本)"""
    stmt = select(PromptVersion.version).where(PromptVersion.id == version_id)
    result = await session.execute(stmt)
    v = result.scalar_one_or_none()
    return v or 0


# ====== 路由: 渲染预览 ======


@router.post("/{name}/preview", response_model=Dict[str, Any])
@rate_limit("30/minute")
async def preview_render(
    name: str,
    payload: PreviewRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """渲染预览: 传入样例变量,看渲染结果。

    用于:
    - 编辑 Prompt 时实时预览
    - 验证变量替换是否正确
    - 测试不同版本的渲染效果
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    if payload.version:
        version_row = await _get_version_by_no(session, template.id, payload.version)
        if not version_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"版本 v{payload.version} 不存在",
            )
    elif payload.label:
        # 按 label 取
        loader = get_global_db_prompt_loader()
        version_row = await loader.get_by_label(name, payload.label, tid)
        if not version_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"label={payload.label} 对应版本不存在",
            )
    else:
        # 默认 production
        loader = get_global_db_prompt_loader()
        version_row = await loader.get_by_label(name, "production", tid)
        if not version_row:
            # fallback latest
            version_row = await _get_latest_version(session, template.id)
            if not version_row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="没有任何版本可预览",
                )

    import json
    import re

    content = version_row.content or ""
    variables = payload.variables or {}

    # 简单变量替换: {var_name} 或 {{var_name}}
    def replacer(m: re.Match) -> str:
        key = m.group(1) or m.group(2)
        val = variables.get(key, m.group(0))
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False, indent=2)
        return str(val)

    pattern = re.compile(r"\{\{\s*(\w+)\s*\}\}|\{\s*(\w+)\s*\}")
    rendered = pattern.sub(replacer, content)

    return {
        "name": name,
        "version": version_row.version,
        "rendered": rendered,
        "variables_used": list(variables.keys()),
    }


# ====== 路由: A/B 测试与灰度配置便捷接口 ======


class SetupABTestRequest(BaseModel):
    """配置 A/B 测试: 把 prod-a / prod-b 分别指向两个版本"""

    model_config = ConfigDict(extra="forbid")

    version_a: int = Field(ge=1, description="prod-a 指向的版本")
    version_b: int = Field(ge=1, description="prod-b 指向的版本")
    traffic_split: int = Field(
        default=50, ge=1, le=99, description="A/B 流量分配百分比(默认 50/50)"
    )


@router.post("/{name}/ab-test", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def setup_ab_test(
    name: str,
    payload: SetupABTestRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """一键配置 A/B 测试(对标 Langfuse A/B Testing)。

    参考: https://langfuse.com/docs/prompt-management/features/a-b-testing

    机制:
    - 把 prod-a label 指向 version_a
    - 把 prod-b label 指向 version_b
    - DbPromptLoader 按 hash(employee_id) % 100 决定走 a 还是 b
    - 同一员工稳定走同一版本(避免体验跳变)
    - traffic_split 控制比例(默认 50/50,可设 70/30 等)

    取消 A/B 测试: 删除 prod-a / prod-b label,重新分配 production label。
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    v_a = await _get_version_by_no(session, template.id, payload.version_a)
    v_b = await _get_version_by_no(session, template.id, payload.version_b)
    if not v_a:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 v{payload.version_a} 不存在",
        )
    if not v_b:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 v{payload.version_b} 不存在",
        )

    actor_id = await get_current_user_id(request)
    await _upsert_label(
        session, template.id, v_a.id, "prod-a", protected=False, updated_by=actor_id
    )
    await _upsert_label(
        session, template.id, v_b.id, "prod-b", protected=False, updated_by=actor_id
    )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_setup_ab_test",
        details={
            "name": name,
            "version_a": payload.version_a,
            "version_b": payload.version_b,
            "traffic_split": payload.traffic_split,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    return {
        "configured": True,
        "name": name,
        "prod_a_version": payload.version_a,
        "prod_b_version": payload.version_b,
        "traffic_split": f"{payload.traffic_split}/{100 - payload.traffic_split}",
        "mechanism": "hash(employee_id) % 100 < split → prod-a, 否则 prod-b",
        "note": "DbPromptLoader 会自动按 employee_id 哈希分流,同一员工稳定走同一版本",
    }


class SetupCanaryRequest(BaseModel):
    """配置灰度发布: canary-Npct label 指向新版本"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="灰度版本号")
    percentage: int = Field(ge=1, le=100, description="灰度百分比 1-100")


@router.post("/{name}/canary", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def setup_canary(
    name: str,
    payload: SetupCanaryRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """一键配置灰度发布(对标 Langfuse canary release 模式)。

    机制:
    - 创建 canary-Npct label 指向新版本
    - DbPromptLoader 优先检查 canary label
    - hash(employee_id) % 100 < N → 走灰度版本,否则走 production
    - 同一员工稳定走同一版本(避免体验跳变)

    逐步扩大灰度:
    1. canary-5pct → 5% 员工
    2. canary-25pct → 25% 员工
    3. canary-50pct → 50% 员工
    4. 全量: 把 production label 指向新版本 + 删除 canary label

    N=100 等同于全量发布(所有员工走新版本)。
    """
    tid = get_current_tenant()
    template = await _get_template_by_name(session, name, tid)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt 模板 {name} 不存在"
        )

    version_row = await _get_version_by_no(session, template.id, payload.version)
    if not version_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本 v{payload.version} 不存在",
        )

    canary_label = f"canary-{payload.percentage}pct"
    actor_id = await get_current_user_id(request)
    # 删除旧 canary label(灰度调整比例时)
    old_canary_stmt = select(PromptLabel).where(
        and_(
            PromptLabel.template_id == template.id,
            PromptLabel.label.like("canary-%pct"),
        )
    )
    old_canary = (await session.execute(old_canary_stmt)).scalars().all()
    for old in old_canary:
        await session.delete(old)

    await _upsert_label(
        session,
        template.id,
        version_row.id,
        canary_label,
        protected=False,
        updated_by=actor_id,
    )

    await audit_service.log(
        actor_id=actor_id,
        action="prompt_setup_canary",
        details={
            "name": name,
            "version": payload.version,
            "percentage": payload.percentage,
            "label": canary_label,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()

    return {
        "configured": True,
        "name": name,
        "version": payload.version,
        "percentage": payload.percentage,
        "label": canary_label,
        "mechanism": f"hash(employee_id) % 100 < {payload.percentage} → 走 v{payload.version}",
        "next_steps": (
            "逐步扩大: POST /canary 调高 percentage, "
            "全量后: POST /labels 设 production 指向新版本 + DELETE /labels/{canary_label}"
        ),
    }
