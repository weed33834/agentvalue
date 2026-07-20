"""
认证相关路由：登录、注册、当前用户信息、刷新 token
"""

import logging
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt_handler import (
    create_access_token,
    decode_access_token,
    extract_bearer_token,
)
from auth.password import hash_password, verify_password
from auth.rbac import Role, get_client_ip, get_current_user_id, get_current_user_role
from auth.token_blacklist import token_blacklist
from api.deps import get_audit_service
from core.config import get_settings
from core.database import get_db
from core.rate_limit import rate_limit
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="employee")
    department: str | None = None


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "bearer"
    user_id: str
    name: str
    role: str


@router.post("/login", response_model=TokenResponse)
@rate_limit("10/minute")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """邮箱 + 密码登录，返回 JWT"""
    eval_service = EvaluationService(session)
    user = await eval_service.get_user_by_email(payload.email)
    if not user or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )
    if not verify_password(payload.password, user.password_hash):
        await audit_service.log(
            actor_id=user.user_id,
            action="login_failed",
            details={"email": payload.email},
            ip_address=get_client_ip(request),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )

    try:
        role = Role(user.role)
    except ValueError:
        role = Role.EMPLOYEE

    token = create_access_token(user.user_id, role.value, name=user.name)
    await audit_service.log(
        actor_id=user.user_id,
        action="login_success",
        details={"email": payload.email, "role": role.value},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return TokenResponse(
        access_token=token,
        user_id=user.user_id,
        name=user.name,
        role=role.value,
    )


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
@rate_limit("5/minute")
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """注册新用户（仅允许注册 employee/manager/hr，admin 需后台创建）"""
    if payload.role not in ("employee", "manager", "hr"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不允许注册该角色",
        )

    eval_service = EvaluationService(session)

    existing_email = await eval_service.get_user_by_email(payload.email)
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="邮箱已被注册",
        )
    existing_id = await eval_service.get_user(payload.user_id)
    if existing_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户 ID 已存在",
        )

    user = await eval_service.create_user(
        {
            "user_id": payload.user_id,
            "name": payload.name,
            "email": payload.email,
            "role": payload.role,
            "department": payload.department,
            "password_hash": hash_password(payload.password),
        }
    )

    try:
        role = Role(user.role)
    except ValueError:
        role = Role.EMPLOYEE

    token = create_access_token(user.user_id, role.value, name=user.name)
    await audit_service.log(
        actor_id=user.user_id,
        action="register",
        details={"email": payload.email, "role": role.value},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return TokenResponse(
        access_token=token,
        user_id=user.user_id,
        name=user.name,
        role=role.value,
    )


@router.get("/me", response_model=Dict[str, Any])
async def me(
    request: Request,
    role: Role = Depends(get_current_user_role),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """获取当前登录用户信息"""
    eval_service = EvaluationService(session)
    user = await eval_service.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return {
        "user_id": user.user_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "department": user.department,
    }


@router.post("/refresh", response_model=TokenResponse)
@rate_limit("20/minute")
async def refresh_token(
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """使用有效 token 换取新 token（续期）"""
    auth_header = request.headers.get("authorization")
    token = extract_bearer_token(auth_header)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 黑名单校验:已吊销的 token 不能用于 refresh,必须重新登录
    jti = payload.get("jti")
    if jti and await token_blacklist.is_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 已被吊销",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    eval_service = EvaluationService(session)
    user = await eval_service.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在"
        )

    try:
        role = Role(user.role)
    except ValueError:
        role = Role.EMPLOYEE

    new_token = create_access_token(user.user_id, role.value, name=user.name)
    # 与 login/logout 对齐:token 续期也记审计,完整刻画令牌生命周期
    await audit_service.log(
        actor_id=user.user_id,
        action="token_refresh",
        details={"role": role.value},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return TokenResponse(
        access_token=new_token,
        user_id=user.user_id,
        name=user.name,
        role=role.value,
    )


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """登出：将当前 token 的 jti 加入黑名单,主动吊销。
    token 剩余有效期后黑名单条目自动过期,Redis 不积压。
    幂等:重复登出同一 token 返回 200。"""
    auth_header = request.headers.get("authorization")
    token = extract_bearer_token(auth_header)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    if not payload:
        # token 已失效(过期或非法),无需吊销
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if not jti:
        # 兼容历史无 jti 的 token:无法吊销,直接返回成功
        return {"revoked": False, "reason": "token 无 jti,跳过吊销"}

    # TTL = token 剩余有效期(秒),至少 1 秒,避免 Redis SET ex=0 报错
    exp = payload.get("exp")
    ttl = int(exp - time.time()) if exp else 0
    if ttl <= 0:
        # token 已过期(decode 理论上会拦截,此处兜底)
        return {"revoked": False, "reason": "token 已过期"}

    await token_blacklist.revoke(jti, ttl)
    await audit_service.log(
        actor_id=payload.get("sub", "unknown"),
        action="logout",
        details={"jti": jti, "ttl_seconds": ttl},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"revoked": True, "ttl_seconds": ttl}


@router.post("/seed-demo-users", response_model=Dict[str, Any])
async def seed_demo_users(
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
):
    """初始化演示账号（仅当库中无该邮箱时创建）。仅在演示模式下可用。

    安全说明：此接口为引导型接口(创建首个 ADMIN 账号),无法要求已登录 ADMIN 调用
    (鸡生蛋问题)。gatekeeper 为 auth_demo_mode 开关:生产环境由 config validator
    强制 auth_demo_mode=false(违例即拒绝 Settings 实例化),故此接口在生产不可达。
    """
    settings = get_settings()
    if not settings.auth_demo_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="演示模式未开启，此接口不可用。请在开发环境设置 AUTH_DEMO_MODE=true",
        )
    eval_service = EvaluationService(session)
    # (user_id, name, email, role, dept, manager_id) —— manager_id 用于 RBAC 团队归属校验
    demo_accounts = [
        ("E1001", "张三（员工）", "employee@agentvalue.ai", "employee", "研发部", "M001"),
        (
            "E1002",
            "李四（员工）",
            "employee2@agentvalue.ai",
            "employee",
            "研发部",
            "M002",
        ),
        ("M001", "王五（主管）", "manager@agentvalue.ai", "manager", "研发部", None),
        ("M002", "赵六（主管）", "manager2@agentvalue.ai", "manager", "研发部", None),
        ("HR001", "孙七（HR）", "hr@agentvalue.ai", "hr", "人力资源部", None),
        (
            "ADMIN001",
            "周八（管理员）",
            "admin@agentvalue.ai",
            "admin",
            "信息技术部",
            None,
        ),
    ]
    default_password = "agentvalue123"
    created = []
    for user_id, name, email, role, dept, manager_id in demo_accounts:
        existing = await eval_service.get_user_by_email(email)
        if existing:
            continue
        await eval_service.create_user(
            {
                "user_id": user_id,
                "name": name,
                "email": email,
                "role": role,
                "department": dept,
                "manager_id": manager_id,
                "password_hash": hash_password(default_password),
            }
        )
        created.append(email)
    await session.commit()
    # 即便演示模式也留痕:批量建号(含 ADMIN)是高敏感操作,记录创建的账号与角色
    if created:
        await audit_service.log(
            actor_id="system",
            action="seed_demo_users",
            details={"created_emails": created, "count": len(created)},
        )
        await session.commit()
    return {
        "created": created,
        "note": "演示账号已初始化，生产环境请关闭演示模式并修改默认密码",
    }
