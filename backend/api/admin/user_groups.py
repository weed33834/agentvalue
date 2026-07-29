"""用户组管理 Admin API (P1-7: ABAC 属性级访问控制)

路由前缀: /api/v1/admin/user-groups
权限: Role.ADMIN / Role.HR (router 级 dependencies)

完整功能:
用户组 CRUD:
- POST   /                       - 创建用户组
- GET    /                       - 列表 (分页, 可按 name 过滤)
- GET    /{group_id}             - 详情
- PUT    /{group_id}             - 更新 (name/description)
- DELETE /{group_id}             - 删除 (级联清理成员)

成员管理:
- POST   /{group_id}/members     - 添加成员 (支持批量)
- DELETE /{group_id}/members/{user_id} - 移除成员
- GET    /{group_id}/members     - 列表成员

权限策略 (为组设置 ABAC 策略):
- POST   /{group_id}/policies    - 为组创建权限策略
- GET    /{group_id}/policies    - 列表组的权限策略
- DELETE /{group_id}/policies/{policy_id} - 删除组的某条策略

说明:
- 策略 subject_type 固定为 "group", subject_id 自动设为 group_id。
- 策略按 tenant_id 隔离,仅同租户生效。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin._common import gen_id
from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.policy import (
    DEFAULT_PRIORITY,
    EFFECT_ALLOW,
    EFFECTS,
    SUBJECT_TYPE_GROUP,
    WILDCARD,
    Policy,
)
from models.user_group import UserGroup, UserGroupMember

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/user-groups",
    tags=["admin-user-groups"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# Schemas
# ============================================================


class UserGroupCreate(BaseModel):
    """创建用户组"""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=2, max_length=64, description="组标识 (唯一)")
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=1024)


class UserGroupUpdate(BaseModel):
    """更新用户组 (group_id 不可改)"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=1024)


class MemberAddItem(BaseModel):
    """添加成员单项"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)


class MemberAddRequest(BaseModel):
    """添加成员请求 (支持批量)"""

    model_config = ConfigDict(extra="forbid")

    user_ids: List[str] = Field(
        ..., min_length=1, max_length=1000, description="用户 ID 列表"
    )


class PolicyCreate(BaseModel):
    """为组创建权限策略"""

    model_config = ConfigDict(extra="forbid")

    resource_type: str = Field(..., min_length=1, max_length=64)
    resource_id: str = Field(default=WILDCARD, max_length=128, description="资源 ID, * 通配")
    action: str = Field(..., min_length=1, max_length=128, description="动作如 evaluation:read")
    effect: str = Field(default=EFFECT_ALLOW, description="allow / deny")
    condition: Optional[Dict[str, Any]] = Field(
        default=None, description="属性级条件 (JSON)"
    )
    priority: int = Field(default=DEFAULT_PRIORITY, description="优先级, 越小越高")
    description: Optional[str] = Field(default=None, max_length=1024)


# ============================================================
# 工具函数
# ============================================================


def _group_to_dict(g: UserGroup) -> Dict[str, Any]:
    """UserGroup → dict"""
    return {
        "id": g.id,
        "group_id": g.group_id,
        "name": g.name,
        "tenant_id": g.tenant_id,
        "description": g.description,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }


def _member_to_dict(m: UserGroupMember) -> Dict[str, Any]:
    """UserGroupMember → dict"""
    return {
        "id": m.id,
        "user_id": m.user_id,
        "group_id": m.group_id,
        "tenant_id": m.tenant_id,
        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
    }


def _policy_to_dict(p: Policy) -> Dict[str, Any]:
    """Policy → dict"""
    return {
        "id": p.id,
        "policy_id": p.policy_id,
        "subject_type": p.subject_type,
        "subject_id": p.subject_id,
        "resource_type": p.resource_type,
        "resource_id": p.resource_id,
        "action": p.action,
        "effect": p.effect,
        "condition": p.condition,
        "priority": p.priority,
        "tenant_id": p.tenant_id,
        "description": p.description,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _get_tenant_id() -> str:
    """获取当前租户 ID"""
    return get_current_tenant()


async def _get_group_or_404(
    session: AsyncSession, tenant_id: str, group_id: str
) -> UserGroup:
    """按租户 + group_id 查组, 不存在则 404"""
    stmt = select(UserGroup).where(
        UserGroup.tenant_id == tenant_id, UserGroup.group_id == group_id
    )
    result = await session.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户组 {group_id!r} 不存在",
        )
    return group


# ============================================================
# 用户组 CRUD
# ============================================================


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_user_group(
    payload: UserGroupCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建用户组 (同租户内 group_id 唯一)"""
    tenant_id = _get_tenant_id()
    # 唯一性检查
    existing = await session.execute(
        select(UserGroup).where(
            UserGroup.tenant_id == tenant_id, UserGroup.group_id == payload.group_id
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"用户组 {payload.group_id!r} 已存在",
        )
    group = UserGroup(
        group_id=payload.group_id,
        name=payload.name,
        description=payload.description,
        tenant_id=tenant_id,
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return _group_to_dict(group)


@router.get("", response_model=Dict[str, Any])
async def list_user_groups(
    request: Request,
    name: Optional[str] = Query(None, description="按名称模糊过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
):
    """列出用户组 (分页, 可按 name 模糊过滤)"""
    tenant_id = _get_tenant_id()
    base = select(UserGroup).where(UserGroup.tenant_id == tenant_id)
    count_base = select(func.count()).select_from(UserGroup).where(
        UserGroup.tenant_id == tenant_id
    )
    if name:
        base = base.where(UserGroup.name.ilike(f"%{name}%"))
        count_base = count_base.where(UserGroup.name.ilike(f"%{name}%"))

    total = (await session.execute(count_base)).scalar() or 0
    items = (
        (
            await session.execute(
                base.order_by(UserGroup.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_group_to_dict(g) for g in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{group_id}", response_model=Dict[str, Any])
async def get_user_group(
    group_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取用户组详情"""
    tenant_id = _get_tenant_id()
    group = await _get_group_or_404(session, tenant_id, group_id)
    return _group_to_dict(group)


@router.put("/{group_id}", response_model=Dict[str, Any])
async def update_user_group(
    group_id: str,
    payload: UserGroupUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新用户组 (name / description)"""
    tenant_id = _get_tenant_id()
    group = await _get_group_or_404(session, tenant_id, group_id)
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    for k, v in fields.items():
        setattr(group, k, v)
    await session.commit()
    await session.refresh(group)
    return _group_to_dict(group)


@router.delete("/{group_id}", response_model=Dict[str, Any])
async def delete_user_group(
    group_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除用户组 (级联清理成员关联 + 组绑定的策略)

    成员关联通过应用层显式删除 (兼容 SQLite 默认未开启外键级联);
    组绑定的 ABAC 策略 (subject_type=group, subject_id=group_id) 一并删除,
    避免悬空策略。
    """
    tenant_id = _get_tenant_id()
    group = await _get_group_or_404(session, tenant_id, group_id)
    # 删除成员关联
    await session.execute(
        delete(UserGroupMember).where(
            UserGroupMember.tenant_id == tenant_id,
            UserGroupMember.group_id == group_id,
        )
    )
    # 删除组绑定的策略
    await session.execute(
        delete(Policy).where(
            Policy.tenant_id == tenant_id,
            Policy.subject_type == SUBJECT_TYPE_GROUP,
            Policy.subject_id == group_id,
        )
    )
    await session.delete(group)
    await session.commit()
    return {"deleted": True, "group_id": group_id}


# ============================================================
# 成员管理
# ============================================================


@router.post(
    "/{group_id}/members", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def add_members(
    group_id: str,
    payload: MemberAddRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """添加成员 (支持批量, 请求内重复与已存在成员跳过)"""
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)

    # 请求内去重 (保持首次出现顺序, 避免同请求内重复 INSERT 触发唯一约束)
    seen_in_request: set = set()
    unique_user_ids: List[str] = []
    for uid in payload.user_ids:
        if uid not in seen_in_request:
            seen_in_request.add(uid)
            unique_user_ids.append(uid)

    # 查已有成员,避免重复加入
    existing = (
        await session.execute(
            select(UserGroupMember.user_id).where(
                UserGroupMember.tenant_id == tenant_id,
                UserGroupMember.group_id == group_id,
                UserGroupMember.user_id.in_(unique_user_ids),
            )
        )
    ).scalars().all()
    existing_set = set(existing)

    added: List[UserGroupMember] = []
    for uid in unique_user_ids:
        if uid in existing_set:
            continue
        member = UserGroupMember(
            user_id=uid, group_id=group_id, tenant_id=tenant_id
        )
        session.add(member)
        added.append(member)
    await session.commit()
    for m in added:
        await session.refresh(m)
    return {
        "added": [_member_to_dict(m) for m in added],
        "added_count": len(added),
        "skipped_count": len(payload.user_ids) - len(added),
    }


@router.delete("/members/{user_id}", response_model=Dict[str, Any])
async def remove_member(
    user_id: str,
    request: Request,
    group_id: str = Query(..., description="组 ID"),
    session: AsyncSession = Depends(get_db),
):
    """移除成员

    query 参数 group_id 指定从哪个组移除 (DELETE 路径体无法携带,改用 query)。
    """
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)
    result = await session.execute(
        delete(UserGroupMember)
        .where(
            UserGroupMember.tenant_id == tenant_id,
            UserGroupMember.group_id == group_id,
            UserGroupMember.user_id == user_id,
        )
        .returning(UserGroupMember.id)
    )
    removed = result.first()
    if removed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不在组 {group_id!r} 中",
        )
    await session.commit()
    return {"removed": True, "user_id": user_id, "group_id": group_id}


@router.get("/{group_id}/members", response_model=Dict[str, Any])
async def list_members(
    group_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    session: AsyncSession = Depends(get_db),
):
    """列表组成员 (分页)"""
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)
    base = select(UserGroupMember).where(
        UserGroupMember.tenant_id == tenant_id,
        UserGroupMember.group_id == group_id,
    )
    count_stmt = select(func.count()).select_from(UserGroupMember).where(
        UserGroupMember.tenant_id == tenant_id,
        UserGroupMember.group_id == group_id,
    )
    total = (await session.execute(count_stmt)).scalar() or 0
    items = (
        (
            await session.execute(
                base.order_by(UserGroupMember.joined_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_member_to_dict(m) for m in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================================================
# 组权限策略管理
# ============================================================


@router.post(
    "/{group_id}/policies",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_group_policy(
    group_id: str,
    payload: PolicyCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """为组创建权限策略 (subject_type=group, subject_id=group_id 自动填充)"""
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)

    # 校验 effect 取值
    if payload.effect not in EFFECTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"effect 必须为 {EFFECTS} 之一",
        )

    policy = Policy(
        policy_id=gen_id(prefix="pol"),
        subject_type=SUBJECT_TYPE_GROUP,
        subject_id=group_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        action=payload.action,
        effect=payload.effect,
        condition=payload.condition,
        priority=payload.priority,
        tenant_id=tenant_id,
        description=payload.description,
    )
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return _policy_to_dict(policy)


@router.get("/{group_id}/policies", response_model=Dict[str, Any])
async def list_group_policies(
    group_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """列表组绑定的权限策略"""
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)
    stmt = (
        select(Policy)
        .where(
            Policy.tenant_id == tenant_id,
            Policy.subject_type == SUBJECT_TYPE_GROUP,
            Policy.subject_id == group_id,
        )
        .order_by(Policy.priority.asc())
    )
    items = (await session.execute(stmt)).scalars().all()
    return {
        "items": [_policy_to_dict(p) for p in items],
        "total": len(items),
    }


@router.delete("/{group_id}/policies/{policy_id}", response_model=Dict[str, Any])
async def delete_group_policy(
    group_id: str,
    policy_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除组的某条策略 (policy_id 为业务主键)"""
    tenant_id = _get_tenant_id()
    await _get_group_or_404(session, tenant_id, group_id)
    result = await session.execute(
        delete(Policy)
        .where(
            Policy.tenant_id == tenant_id,
            Policy.policy_id == policy_id,
            Policy.subject_type == SUBJECT_TYPE_GROUP,
            Policy.subject_id == group_id,
        )
        .returning(Policy.id)
    )
    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略 {policy_id!r} 不存在或不属于组 {group_id!r}",
        )
    await session.commit()
    return {"deleted": True, "policy_id": policy_id, "group_id": group_id}
