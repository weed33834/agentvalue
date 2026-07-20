"""
RBAC（基于角色的访问控制）
优先从 Authorization header 解析 JWT 获取用户身份与角色；
若开启演示模式（auth_demo_mode=True），可降级为信任 x-user-role / x-user-id header。
"""

import logging
from enum import Enum
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request, status

from auth.jwt_handler import decode_access_token, extract_bearer_token
from auth.token_blacklist import token_blacklist
from core.config import get_settings

logger = logging.getLogger(__name__)


class Role(str, Enum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR = "hr"
    ADMIN = "admin"


# 角色可访问的评估视图
VIEW_PERMISSIONS = {
    Role.EMPLOYEE: ["employee_view"],
    Role.MANAGER: ["employee_view", "manager_view", "audit"],
    Role.HR: ["employee_view", "manager_view", "audit"],
    Role.ADMIN: ["employee_view", "manager_view", "audit"],
}


def can_access(role: Role, view: str) -> bool:
    return view in VIEW_PERMISSIONS.get(role, [])


async def _resolve_user(request: Request) -> Tuple[Optional[Role], Optional[str], str]:
    """
    解析当前用户身份。
    返回 (role, user_id, source)，source 为 "jwt" / "demo" / "anonymous"。
    """
    settings = get_settings()

    # 1. 优先解析 JWT
    auth_header = request.headers.get("authorization")
    token = extract_bearer_token(auth_header)
    if token:
        payload = decode_access_token(token)
        if payload:
            # 黑名单校验:jti 已被吊销则视为无效 token
            jti = payload.get("jti")
            if jti and await token_blacklist.is_revoked(jti):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="token 已被吊销",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                role = Role(payload.get("role", ""))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="token 中角色无效",
                )
            user_id = payload.get("sub")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="token 缺少用户标识",
                )
            return role, user_id, "jwt"
        # token 存在但无效
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. 演示模式：信任 header
    if settings.auth_demo_mode:
        role_header = request.headers.get("x-user-role", "")
        if role_header:
            try:
                role = Role(role_header.lower())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"无效的角色: {role_header}",
                )
        else:
            # 演示模式下未提供角色，默认为 employee（仅用于本地开发）
            role = Role.EMPLOYEE
        user_id = request.headers.get("x-user-id") or "anonymous"
        return role, user_id, "demo"

    return None, None, "anonymous"


async def get_current_user_role(request: Request) -> Role:
    """从请求解析当前用户角色"""
    role, _, source = await _resolve_user(request)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证，请提供有效 token 或开启演示模式",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if source == "demo":
        logger.debug("使用演示模式 header 鉴权，生产环境应禁用 auth_demo_mode")
    return role


async def get_current_user_id(request: Request) -> str:
    """从请求解析当前用户 ID"""
    _, user_id, source = await _resolve_user(request)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证，请提供有效 token 或开启演示模式",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


def get_client_ip(request: Request) -> str:
    """提取客户端真实 IP（支持反向代理）"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_role(*allowed_roles: Role):
    """FastAPI 依赖：要求特定角色"""

    async def checker(role: Role = Depends(get_current_user_role)):
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return role

    return checker
