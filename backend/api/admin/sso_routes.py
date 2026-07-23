"""SSO 单点登录 Admin API

路由前缀: /api/v1/admin/sso
权限:
- 配置管理端点 (CRUD): Role.ADMIN
- 认证流程端点 (authorize/callback/ldap-login): 公开访问 (用户尚未登录)

完整端点:
- POST   /configs              - 创建 SSO 配置                  [ADMIN]
- GET    /configs              - 配置列表 (脱敏)                 [ADMIN]
- GET    /configs/{id}         - 配置详情 (脱敏)                 [ADMIN]
- PUT    /configs/{id}         - 更新配置                       [ADMIN]
- DELETE /configs/{id}         - 删除配置                       [ADMIN]
- GET    /configs/{id}/authorize    - 获取授权 URL              [公开]
- POST   /configs/{id}/callback     - OAuth2 回调              [公开]
- POST   /configs/{id}/ldap-login   - LDAP 登录                [公开]

安全: config 中的敏感信息 (client_secret / bind_password / certificate) 在响应中脱敏。
H4: 认证流程端点不再要求 ADMIN 角色 (用户此时尚未登录)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.sso_service import SSO_PROVIDER_TYPES, SSOService

logger = logging.getLogger(__name__)

# H4: 拆分为两个 router
# 1. 配置管理 router: 要求 ADMIN 角色
# 2. 认证流程 router: 公开访问 (authorize/callback/ldap-login 是认证入口, 用户尚未登录)
router = APIRouter(prefix="/api/v1/admin/sso", tags=["admin-sso"])
config_router = APIRouter(
    dependencies=[Depends(require_role(Role.ADMIN))],
)
auth_router = APIRouter()


# ============================================================
# Schemas
# ============================================================


class SSOConfigCreate(BaseModel):
    """创建 SSO 配置请求"""

    provider_name: str = Field(..., description="提供商名称 (租户内唯一)")
    provider_type: str = Field(..., description="协议类型: oauth2 / saml / ldap")
    config: dict = Field(..., description="配置 JSON (结构按 provider_type 不同)")
    enabled: bool = Field(default=True, description="是否启用")


class SSOConfigUpdate(BaseModel):
    """更新 SSO 配置请求"""

    provider_name: Optional[str] = None
    provider_type: Optional[str] = None
    config: Optional[dict] = None
    enabled: Optional[bool] = None


class OAuth2CallbackRequest(BaseModel):
    """OAuth2 回调请求"""

    code: str = Field(..., description="授权码")
    state: Optional[str] = Field(None, description="CSRF state")


class LDAPLoginRequest(BaseModel):
    """LDAP 登录请求"""

    username: str = Field(..., description="LDAP 用户名")
    password: str = Field(..., description="LDAP 密码")


# ============================================================
# 配置 CRUD
# ============================================================


@config_router.post(
    "/configs", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_sso_config(
    payload: SSOConfigCreate,
    session: AsyncSession = Depends(get_db),
):
    """创建 SSO 配置"""
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        sso_config = await service.create_config(
            provider_name=payload.provider_name,
            provider_type=payload.provider_type,
            config=payload.config,
            enabled=payload.enabled,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return SSOService._config_to_dict(sso_config)


@config_router.get("/configs", response_model=Dict[str, Any])
async def list_sso_configs(
    session: AsyncSession = Depends(get_db),
):
    """列出租户所有 SSO 配置 (脱敏)"""
    tenant_id = get_current_tenant()
    service = SSOService(session)
    configs = await service.list_configs(tenant_id=tenant_id)
    return {
        "items": [SSOService._config_to_dict(c) for c in configs],
        "total": len(configs),
    }


@config_router.get("/configs/{config_id}", response_model=Dict[str, Any])
async def get_sso_config(
    config_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取 SSO 配置详情 (脱敏)"""
    tenant_id = get_current_tenant()
    service = SSOService(session)
    sso_config = await service.get_config(config_id, tenant_id=tenant_id)
    if sso_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO 配置 {config_id} 不存在",
        )
    return SSOService._config_to_dict(sso_config)


@config_router.put("/configs/{config_id}", response_model=Dict[str, Any])
async def update_sso_config(
    config_id: int,
    payload: SSOConfigUpdate,
    session: AsyncSession = Depends(get_db),
):
    """更新 SSO 配置"""
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        sso_config = await service.update_config(
            config_id,
            provider_name=payload.provider_name,
            provider_type=payload.provider_type,
            config=payload.config,
            enabled=payload.enabled,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await session.commit()
    return SSOService._config_to_dict(sso_config)


@config_router.delete("/configs/{config_id}", response_model=Dict[str, Any])
async def delete_sso_config(
    config_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除 SSO 配置"""
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        deleted = await service.delete_config(config_id, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO 配置 {config_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "id": config_id}


# ============================================================
# 授权流程
# ============================================================


@auth_router.get("/configs/{config_id}/authorize", response_model=Dict[str, str])
async def get_authorization_url(
    config_id: int,
    state: Optional[str] = Query(None, description="CSRF state (不传则自动生成)"),
    session: AsyncSession = Depends(get_db),
):
    """获取授权 URL (OAuth2/SAML)

    OAuth2: 返回授权码 URL, 前端跳转后 IdP 回调 redirect_uri 带 code。
    SAML: 返回 SP 发起的 SSO URL。
    LDAP: 不支持 (用 ldap-login)。
    """
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        return await service.get_authorization_url(
            config_id, tenant_id=tenant_id, state=state
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.post("/configs/{config_id}/callback", response_model=Dict[str, Any])
async def oauth2_callback(
    config_id: int,
    payload: OAuth2CallbackRequest,
    session: AsyncSession = Depends(get_db),
):
    """处理 OAuth2 回调

    用 code 换取 access_token, 获取用户信息, 创建/更新用户映射, 返回内部 JWT。
    H1: 校验 state (CSRF 防护), state 无效/过期/不匹配时返回 401。
    """
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        result = await service.handle_callback(
            config_id,
            payload.code,
            tenant_id=tenant_id,
            state=payload.state,
        )
    except ValueError as e:
        err_msg = str(e)
        # H1: state 相关错误返回 401 (CSRF 防护)
        if "state" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=err_msg
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
    await session.commit()
    return result


@auth_router.post("/configs/{config_id}/ldap-login", response_model=Dict[str, Any])
async def ldap_login(
    config_id: int,
    payload: LDAPLoginRequest,
    session: AsyncSession = Depends(get_db),
):
    """LDAP 登录

    用户名密码认证, 成功后创建/更新用户映射, 返回内部 JWT。
    """
    tenant_id = get_current_tenant()
    service = SSOService(session)
    try:
        result = await service.authenticate_ldap(
            config_id,
            payload.username,
            payload.password,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        # ldap3 未安装 / 认证失败 / 配置错误
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    await session.commit()
    return result


# ============================================================
# H4: 合并两个子路由到父路由 (必须在所有路由定义之后)
# ============================================================

router.include_router(config_router)
router.include_router(auth_router)
