"""用户管理 Admin API

路由前缀: /api/v1/admin/users
权限: Role.ADMIN / Role.HR (router 级 dependencies)

完整功能 (6 端点):
- GET    /                       - 列表 (分页, 可按 role/department 过滤)
- GET    /{user_id}              - 详情
- PUT    /{user_id}              - 更新 (name/role/department/manager_id)
- POST   /{user_id}/disable      - 禁用
- DELETE /{user_id}              - 删除
- POST   /batch                  - 批量导入 (JSON 数组)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.deps import get_audit_service, get_evaluation_service
from auth.rbac import Role, get_current_user_id, require_role
from core.tenant_context import get_current_tenant
from models import User
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_role(Role.ADMIN, Role.HR))],
)


# ============================================================
# Schemas
# ============================================================


class UserUpdate(BaseModel):
    """更新用户信息请求"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    role: Optional[str] = Field(
        default=None, description="角色: employee/manager/hr/admin/disabled"
    )
    department: Optional[str] = Field(default=None, max_length=128)
    manager_id: Optional[str] = Field(default=None, max_length=64)


class BatchUserItem(BaseModel):
    """批量导入单个用户"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    email: Optional[str] = Field(default=None, max_length=256)
    role: Optional[str] = Field(default="employee")
    department: Optional[str] = Field(default=None, max_length=128)
    manager_id: Optional[str] = Field(default=None, max_length=64)


class BatchUserCreate(BaseModel):
    """批量导入用户请求"""

    model_config = ConfigDict(extra="forbid")

    users: List[BatchUserItem] = Field(
        ..., min_length=1, max_length=1000, description="用户列表 (最多 1000 条)"
    )


# ============================================================
# 工具函数
# ============================================================


def _user_to_dict(user: User) -> Dict[str, Any]:
    """User entity → dict (不含 password_hash)"""
    return {
        "id": user.id,
        "user_id": user.user_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "department": user.department,
        "manager_id": user.manager_id,
        "tenant_id": user.tenant_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _get_tenant_id() -> str:
    """获取当前租户 ID（admin 端默认操作当前租户）"""
    return get_current_tenant()


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_users(
    request: Request,
    role: Optional[str] = Query(None, description="按角色过滤"),
    department: Optional[str] = Query(None, description="按部门过滤"),
    page: int = Query(1, ge=1, description="页码, 从 1 开始"),
    page_size: int = Query(20, ge=1, le=500, description="每页条数"),
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """列出用户 (分页, 可按 role / department 过滤)"""
    tenant_id = _get_tenant_id()
    result = await eval_service.list_users(
        tenant_id=tenant_id,
        role=role,
        department=department,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_user_to_dict(u) for u in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/{user_id}", response_model=Dict[str, Any])
async def get_user(
    user_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """获取用户详情"""
    user = await eval_service.get_user(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )
    return _user_to_dict(user)


@router.put("/{user_id}", response_model=Dict[str, Any])
async def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新用户信息 (name / role / department / manager_id)"""
    # 只传非 None 的字段
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    tenant_id = _get_tenant_id()
    user = await eval_service.update_user(tenant_id, user_id, **fields)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="update_user",
        employee_id=user_id,
        details={"changed": fields},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await eval_service.session.commit()
    await eval_service.session.refresh(user)
    return _user_to_dict(user)


@router.post("/{user_id}/disable", response_model=Dict[str, Any])
async def disable_user(
    user_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """禁用用户 (设置 role 为 disabled)

    禁用后用户无法登录，但记录保留可查。
    """
    tenant_id = _get_tenant_id()
    # 不允许禁用自己
    if user_id == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能禁用当前登录用户",
        )

    success = await eval_service.disable_user(tenant_id, user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="disable_user",
        employee_id=user_id,
        details={"user_id": user_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await eval_service.session.commit()
    return {"disabled": True, "user_id": user_id}


@router.delete("/{user_id}", response_model=Dict[str, Any])
async def delete_user(
    user_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """删除用户 (hard delete, 不可恢复)

    物理删除用户记录，关联数据（评估/反馈等）不受级联影响。
    """
    tenant_id = _get_tenant_id()
    # 不允许删除自己
    if user_id == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除当前登录用户",
        )

    success = await eval_service.delete_user(tenant_id, user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="delete_user",
        employee_id=user_id,
        details={"user_id": user_id},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await eval_service.session.commit()
    return {"deleted": True, "user_id": user_id}


@router.post("/batch", response_model=Dict[str, Any])
async def batch_create_users(
    payload: BatchUserCreate,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """批量导入用户 (JSON 数组)

    每个用户需包含 user_id + name，其余字段可选。
    已存在的 user_id（同租户内）跳过，不报错。
    """
    tenant_id = _get_tenant_id()
    users_data = [item.model_dump() for item in payload.users]
    created = await eval_service.batch_create_users(tenant_id, users_data)

    await audit_service.log(
        actor_id=current_user_id,
        action="batch_create_users",
        details={
            "count": len(created),
            "user_ids": [u.user_id for u in created],
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await eval_service.session.commit()

    return {
        "created": [_user_to_dict(u) for u in created],
        "created_count": len(created),
        "skipped_count": len(payload.users) - len(created),
        "total_requested": len(payload.users),
    }
