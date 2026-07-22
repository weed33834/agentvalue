"""SSO 单点登录服务

对标 Dify SSO / Bisheng SSO, 支持 OAuth2 / SAML / LDAP 三种协议。

功能:
- SSO 配置 CRUD (tenant_id 隔离)
- get_authorization_url: 生成 OAuth2/SAML 授权 URL
- handle_callback: 处理 OAuth2 回调, 获取用户信息, 创建/更新用户映射
- authenticate_ldap: LDAP 用户名密码认证
- _create_or_update_user_mapping: 外部用户 → 内部用户映射 (首次登录自动创建内部用户)

安全:
- config 中的敏感信息 (client_secret / bind_password / certificate) 在 API 响应中脱敏
- LDAP 认证使用 ldap3 (未安装时降级返回错误)
- OAuth2 回调用 httpx 异步请求 token_url / userinfo_url

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt_handler import create_access_token
from models.sso_models import SSOConfig, SSOSession
from models.models import User

logger = logging.getLogger(__name__)

# 支持的 SSO 协议类型
SSO_PROVIDER_TYPES = {"oauth2", "saml", "ldap"}

# OAuth2 配置中需要脱敏的字段
_SENSITIVE_KEYS = {"client_secret", "bind_password", "certificate", "saml_private_key"}

# OAuth2 默认 token 有效期 (分钟)
DEFAULT_TOKEN_EXPIRE_MINUTES = 60

# OAuth2 state 有效期 (秒, 10 分钟)
STATE_EXPIRE_SECONDS = 600


class SSOService:
    """SSO 单点登录服务"""

    # 类级 state 存储 (H1: OAuth2 state 服务端校验)
    # {state: {tenant_id, provider_name, expires_at}}
    _state_store: Dict[str, Dict[str, Any]] = {}

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 配置 CRUD =====================

    async def create_config(
        self,
        provider_name: str,
        provider_type: str,
        config: Dict[str, Any],
        enabled: bool = True,
        *,
        tenant_id: str = "default",
    ) -> SSOConfig:
        """创建 SSO 配置

        Args:
            provider_name: 提供商名称 (租户内唯一, 如 google / github / keycloak)。
            provider_type: 协议类型 (oauth2 / saml / ldap)。
            config: 配置 JSON (结构按 provider_type 不同)。
            enabled: 是否启用。
            tenant_id: 租户 ID。

        Returns:
            创建的 SSOConfig 对象。

        Raises:
            ValueError: 参数无效或名称已存在。
        """
        if provider_type not in SSO_PROVIDER_TYPES:
            raise ValueError(
                f"无效的 provider_type: {provider_type}, 可选: {SSO_PROVIDER_TYPES}"
            )
        if not provider_name or not provider_name.strip():
            raise ValueError("provider_name 不能为空")

        # 检查租户内是否已存在同名 provider
        existing = (
            await self.session.execute(
                select(SSOConfig).where(
                    SSOConfig.tenant_id == tenant_id,
                    SSOConfig.provider_name == provider_name.strip(),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"SSO 配置 '{provider_name}' 已存在")

        # 校验配置必填字段
        self._validate_config(provider_type, config)

        sso_config = SSOConfig(
            tenant_id=tenant_id,
            provider_name=provider_name.strip(),
            provider_type=provider_type,
            config=config,
            enabled=enabled,
        )
        self.session.add(sso_config)
        await self.session.flush()
        logger.info(
            "创建 SSO 配置 id=%s provider=%s type=%s tenant=%s",
            sso_config.id,
            provider_name,
            provider_type,
            tenant_id,
        )
        return sso_config

    async def get_config(
        self, config_id: int, *, tenant_id: str = "default"
    ) -> Optional[SSOConfig]:
        """获取 SSO 配置 (含敏感信息, 仅内部使用)"""
        return (
            await self.session.execute(
                select(SSOConfig).where(
                    SSOConfig.id == config_id,
                    SSOConfig.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def get_config_by_name(
        self, provider_name: str, *, tenant_id: str = "default"
    ) -> Optional[SSOConfig]:
        """按名称获取 SSO 配置"""
        return (
            await self.session.execute(
                select(SSOConfig).where(
                    SSOConfig.tenant_id == tenant_id,
                    SSOConfig.provider_name == provider_name,
                )
            )
        ).scalar_one_or_none()

    async def list_configs(
        self, *, tenant_id: str = "default"
    ) -> List[SSOConfig]:
        """列出租户所有 SSO 配置"""
        result = await self.session.execute(
            select(SSOConfig)
            .where(SSOConfig.tenant_id == tenant_id)
            .order_by(SSOConfig.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_config(
        self,
        config_id: int,
        *,
        provider_name: Optional[str] = None,
        provider_type: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        enabled: Optional[bool] = None,
        tenant_id: str = "default",
    ) -> SSOConfig:
        """更新 SSO 配置

        Args:
            config_id: 配置 ID。
            provider_name: 新名称 (可选)。
            provider_type: 新类型 (可选)。
            config: 新配置 JSON (可选)。
            enabled: 是否启用 (可选)。
            tenant_id: 租户 ID。

        Returns:
            更新后的 SSOConfig 对象。

        Raises:
            ValueError: 配置不存在或参数无效。
        """
        sso_config = await self.get_config(config_id, tenant_id=tenant_id)
        if sso_config is None:
            raise ValueError(f"SSO 配置 {config_id} 不存在")

        if provider_type is not None:
            if provider_type not in SSO_PROVIDER_TYPES:
                raise ValueError(
                    f"无效的 provider_type: {provider_type}, 可选: {SSO_PROVIDER_TYPES}"
                )
            sso_config.provider_type = provider_type

        if provider_name is not None:
            if not provider_name.strip():
                raise ValueError("provider_name 不能为空")
            # 检查名称冲突 (排除自身)
            conflict = (
                await self.session.execute(
                    select(SSOConfig).where(
                        SSOConfig.tenant_id == tenant_id,
                        SSOConfig.provider_name == provider_name.strip(),
                        SSOConfig.id != config_id,
                    )
                )
            ).scalar_one_or_none()
            if conflict is not None:
                raise ValueError(f"SSO 配置 '{provider_name}' 已存在")
            sso_config.provider_name = provider_name.strip()

        if config is not None:
            self._validate_config(sso_config.provider_type, config)
            sso_config.config = config

        if enabled is not None:
            sso_config.enabled = enabled

        await self.session.flush()
        logger.info("更新 SSO 配置 id=%s tenant=%s", config_id, tenant_id)
        return sso_config

    async def delete_config(
        self, config_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除 SSO 配置

        Returns:
            True 表示删除成功, False 表示配置不存在。
        """
        sso_config = await self.get_config(config_id, tenant_id=tenant_id)
        if sso_config is None:
            return False
        await self.session.delete(sso_config)
        await self.session.flush()
        logger.info("删除 SSO 配置 id=%s tenant=%s", config_id, tenant_id)
        return True

    # ===================== 授权流程 =====================

    async def get_authorization_url(
        self, config_id: int, *, tenant_id: str = "default", state: Optional[str] = None
    ) -> Dict[str, str]:
        """获取授权 URL

        OAuth2: 构建授权码 URL (authorize_url?client_id=...&redirect_uri=...&response_type=code&scope=...&state=...)
        SAML: 返回 SSO URL (SP 发起)
        LDAP: 不支持授权 URL (用 ldap-login)

        Returns:
            {"url": str, "state": str}

        Raises:
            ValueError: 配置不存在 / 不支持 / 已禁用。
        """
        sso_config = await self.get_config(config_id, tenant_id=tenant_id)
        if sso_config is None:
            raise ValueError(f"SSO 配置 {config_id} 不存在")
        if not sso_config.enabled:
            raise ValueError(f"SSO 配置 {sso_config.provider_name} 已禁用")

        # 生成 state 防 CSRF
        if state is None:
            state = secrets.token_urlsafe(16)

        # H1: 将 state 存储到服务端, 关联 tenant_id + provider_name + 过期时间
        self._save_state(state, tenant_id, sso_config.provider_name)

        cfg = sso_config.config or {}

        if sso_config.provider_type == "oauth2":
            params = {
                "client_id": cfg.get("client_id", ""),
                "redirect_uri": cfg.get("redirect_uri", ""),
                "response_type": "code",
                "state": state,
            }
            scopes = cfg.get("scopes")
            if scopes:
                if isinstance(scopes, list):
                    params["scope"] = " ".join(scopes)
                else:
                    params["scope"] = str(scopes)
            authorize_url = cfg.get("authorize_url", "")
            if not authorize_url:
                raise ValueError("OAuth2 配置缺少 authorize_url")
            url = f"{authorize_url}?{urlencode(params)}"
            return {"url": url, "state": state}

        elif sso_config.provider_type == "saml":
            sso_url = cfg.get("sso_url", "")
            if not sso_url:
                raise ValueError("SAML 配置缺少 sso_url")
            # SAML SP 发起: 返回 SSO URL + entity_id
            return {
                "url": f"{sso_url}?RelayState={state}",
                "state": state,
            }

        else:
            raise ValueError(
                f"provider_type '{sso_config.provider_type}' 不支持授权 URL, 请使用 ldap-login"
            )

    async def handle_callback(
        self,
        config_id: int,
        code: str,
        *,
        tenant_id: str = "default",
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """处理 OAuth2 回调

        1. 用 code 换取 access_token (POST token_url)
        2. 用 access_token 获取用户信息 (GET userinfo_url)
        3. 创建/更新用户映射 (external_user_id → internal_user_id)
        4. 生成内部 JWT token

        Returns:
            {
                "access_token": str,          # 内部 JWT
                "token_type": "bearer",
                "user": {user_id, name, email, role},
                "provider": provider_name,
                "external_user_id": str,
            }

        Raises:
            ValueError: 配置不存在 / 已禁用 / 回调失败。
        """
        sso_config = await self.get_config(config_id, tenant_id=tenant_id)
        if sso_config is None:
            raise ValueError(f"SSO 配置 {config_id} 不存在")
        if not sso_config.enabled:
            raise ValueError(f"SSO 配置 {sso_config.provider_name} 已禁用")
        if sso_config.provider_type != "oauth2":
            raise ValueError("回调仅支持 OAuth2 类型配置")

        # H1: 校验 state (CSRF 防护)
        if not state:
            raise ValueError("缺少 state 参数 (CSRF 防护)")
        state_data = self._consume_state(state)
        if state_data is None:
            raise ValueError("state 无效或已过期 (CSRF 防护)")
        # 验证 state 关联的租户和 provider 与当前请求匹配
        if (
            state_data.get("tenant_id") != tenant_id
            or state_data.get("provider_name") != sso_config.provider_name
        ):
            raise ValueError("state 与当前请求的租户/提供商不匹配")

        cfg = sso_config.config or {}

        # 1. 用 code 换取 access_token
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.get("redirect_uri", ""),
            "client_id": cfg.get("client_id", ""),
            "client_secret": cfg.get("client_secret", ""),
        }
        token_url = cfg.get("token_url", "")
        if not token_url:
            raise ValueError("OAuth2 配置缺少 token_url")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                token_resp = await client.post(token_url, data=token_data)
                token_resp.raise_for_status()
                token_json = token_resp.json()
        except httpx.HTTPError as e:
            logger.warning("OAuth2 token 交换失败 provider=%s: %s", sso_config.provider_name, e)
            raise ValueError(f"OAuth2 token 交换失败: {e}")

        access_token = token_json.get("access_token")
        if not access_token:
            raise ValueError(f"OAuth2 token 响应缺少 access_token: {token_json}")

        # 2. 用 access_token 获取用户信息
        userinfo_url = cfg.get("userinfo_url", "")
        if not userinfo_url:
            raise ValueError("OAuth2 配置缺少 userinfo_url")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                userinfo_resp = await client.get(
                    userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo_resp.raise_for_status()
                user_info = userinfo_resp.json()
        except httpx.HTTPError as e:
            logger.warning("OAuth2 userinfo 获取失败 provider=%s: %s", sso_config.provider_name, e)
            raise ValueError(f"OAuth2 用户信息获取失败: {e}")

        # 属性映射: 外部字段 → 内部字段
        attr_mapping = cfg.get("attribute_mapping", {})
        external_id = str(
            user_info.get(attr_mapping.get("external_id", "sub"))
            or user_info.get("sub")
            or user_info.get("id")
            or ""
        )
        if not external_id:
            raise ValueError("无法从 userinfo 提取 external_user_id")

        # 3. 创建/更新用户映射
        internal_user = await self._create_or_update_user_mapping(
            tenant_id=tenant_id,
            provider_name=sso_config.provider_name,
            external_id=external_id,
            user_info=user_info,
            attr_mapping=attr_mapping,
        )

        # 记录 SSO 会话
        expires_in = token_json.get("expires_in", DEFAULT_TOKEN_EXPIRE_MINUTES * 60)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        sso_session = SSOSession(
            tenant_id=tenant_id,
            provider_name=sso_config.provider_name,
            external_user_id=external_id,
            internal_user_id=internal_user.user_id,
            access_token=access_token,
            refresh_token=token_json.get("refresh_token"),
            expires_at=expires_at,
        )
        self.session.add(sso_session)
        await self.session.flush()

        # 4. 生成内部 JWT
        jwt_token = create_access_token(
            user_id=internal_user.user_id,
            role=internal_user.role,
            name=internal_user.name,
        )

        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "user": {
                "user_id": internal_user.user_id,
                "name": internal_user.name,
                "email": internal_user.email,
                "role": internal_user.role,
            },
            "provider": sso_config.provider_name,
            "external_user_id": external_id,
        }

    async def authenticate_ldap(
        self,
        config_id: int,
        username: str,
        password: str,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """LDAP 认证

        1. 用 bind_dn + bind_password 绑定到 LDAP 服务器
        2. 用 search_filter 搜索用户 DN
        3. 用用户 DN + password 二次绑定验证密码
        4. 创建/更新用户映射

        Returns:
            {
                "access_token": str,          # 内部 JWT
                "token_type": "bearer",
                "user": {user_id, name, email, role},
                "provider": provider_name,
                "external_user_id": str,
            }

        Raises:
            ValueError: 配置不存在 / 已禁用 / 认证失败 / ldap3 未安装。
        """
        try:
            import ldap3  # type: ignore
        except ImportError:
            raise ValueError("ldap3 未安装, 请运行 pip install ldap3 后重试")

        sso_config = await self.get_config(config_id, tenant_id=tenant_id)
        if sso_config is None:
            raise ValueError(f"SSO 配置 {config_id} 不存在")
        if not sso_config.enabled:
            raise ValueError(f"SSO 配置 {sso_config.provider_name} 已禁用")
        if sso_config.provider_type != "ldap":
            raise ValueError("ldap-login 仅支持 LDAP 类型配置")

        cfg = sso_config.config or {}
        server_url = cfg.get("server_url", "")
        bind_dn = cfg.get("bind_dn", "")
        bind_password = cfg.get("bind_password", "")
        search_base = cfg.get("search_base", "")
        search_filter_tmpl = cfg.get("search_filter", "(uid={username})")

        if not server_url or not search_base:
            raise ValueError("LDAP 配置缺少 server_url 或 search_base")

        # 1. 管理员绑定
        server = ldap3.Server(server_url, get_info=ldap3.ALL)
        conn = ldap3.Connection(
            server, user=bind_dn, password=bind_password, auto_bind=True
        )
        try:
            # 2. 搜索用户 DN (H2: 转义 LDAP 特殊字符防过滤器注入)
            escaped_username = self._escape_ldap_filter(username)
            search_filter = search_filter_tmpl.replace(
                "{username}", escaped_username
            )
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=ldap3.ALL_ATTRIBUTES,
            )
            if not conn.entries:
                raise ValueError(f"LDAP 用户 '{username}' 未找到")
            user_entry = conn.entries[0]
            user_dn = user_entry.entry_dn

            # 3. 用户密码验证 (二次绑定)
            user_conn = ldap3.Connection(
                server, user=user_dn, password=password, auto_bind=True
            )
            user_conn.unbind()
        except ldap3.core.exceptions.LDAPBindError as e:
            raise ValueError(f"LDAP 认证失败: 用户名或密码错误")
        except ldap3.core.exceptions.LDAPException as e:
            raise ValueError(f"LDAP 操作失败: {e}")
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

        # 提取用户属性
        attr_mapping = cfg.get("attribute_mapping", {})
        user_info: Dict[str, Any] = {}
        # 从 LDAP entry 提取属性
        for attr_name in user_entry.entry_attributes:
            val = user_entry[attr_name].value
            user_info[attr_name] = val if not isinstance(val, list) else (val[0] if val else None)

        external_id = str(user_dn)

        # 4. 创建/更新用户映射
        internal_user = await self._create_or_update_user_mapping(
            tenant_id=tenant_id,
            provider_name=sso_config.provider_name,
            external_id=external_id,
            user_info=user_info,
            attr_mapping=attr_mapping,
        )

        # 生成内部 JWT
        jwt_token = create_access_token(
            user_id=internal_user.user_id,
            role=internal_user.role,
            name=internal_user.name,
        )

        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "user": {
                "user_id": internal_user.user_id,
                "name": internal_user.name,
                "email": internal_user.email,
                "role": internal_user.role,
            },
            "provider": sso_config.provider_name,
            "external_user_id": external_id,
        }

    # ===================== 用户映射 =====================

    async def _create_or_update_user_mapping(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        external_id: str,
        user_info: Dict[str, Any],
        attr_mapping: Optional[Dict[str, str]] = None,
    ) -> User:
        """创建或更新外部用户 → 内部用户映射

        首次登录: 根据 external_id 查找是否已有映射, 无则创建内部 User。
        已有映射: 更新用户信息 (name/email)。

        attr_mapping 字段映射:
          - external_id → 内部 user_id (默认用 external_id)
          - name → 内部 name (默认用 user_info.name/displayName/cn)
          - email → 内部 email (默认用 user_info.email/mail)

        Returns:
            内部 User 对象。
        """
        attr_mapping = attr_mapping or {}

        # 查找已有映射
        existing_session = (
            await self.session.execute(
                select(SSOSession).where(
                    SSOSession.tenant_id == tenant_id,
                    SSOSession.provider_name == provider_name,
                    SSOSession.external_user_id == external_id,
                )
            )
        ).scalars().first()

        # 映射字段
        name_key = attr_mapping.get("name", "name")
        email_key = attr_mapping.get("email", "email")
        internal_user_id = str(
            user_info.get(attr_mapping.get("user_id", "user_id"))
            or external_id
        )
        display_name = str(
            user_info.get(name_key)
            or user_info.get("name")
            or user_info.get("displayName")
            or user_info.get("cn")
            or user_info.get("preferred_username")
            or external_id
        )
        email = user_info.get(email_key) or user_info.get("email") or user_info.get("mail")

        if existing_session is not None:
            # 已有映射, 更新内部用户信息
            user = (
                await self.session.execute(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.user_id == existing_session.internal_user_id,
                    )
                )
            ).scalar_one_or_none()
            if user is not None:
                if display_name:
                    user.name = display_name
                if email:
                    user.email = str(email)
                await self.session.flush()
                return user

        # 首次登录: 创建内部 User
        user = (
            await self.session.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.user_id == internal_user_id,
                )
            )
        ).scalar_one_or_none()

        if user is None:
            user = User(
                tenant_id=tenant_id,
                user_id=internal_user_id,
                name=display_name,
                email=str(email) if email else None,
                role="employee",
            )
            self.session.add(user)
            await self.session.flush()

        logger.info(
            "SSO 用户映射 provider=%s external=%s → internal=%s tenant=%s",
            provider_name,
            external_id,
            user.user_id,
            tenant_id,
        )
        return user

    # ===================== 内部方法 =====================

    @classmethod
    def _save_state(
        cls, state: str, tenant_id: str, provider_name: str
    ) -> None:
        """H1: 存储 OAuth2 state 到服务端, 关联租户 + provider + 过期时间

        Args:
            state: 生成的 state 字符串。
            tenant_id: 租户 ID。
            provider_name: SSO 提供商名称。
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_EXPIRE_SECONDS)
        cls._state_store[state] = {
            "tenant_id": tenant_id,
            "provider_name": provider_name,
            "expires_at": expires_at,
        }
        logger.debug(
            "保存 SSO state tenant=%s provider=%s expires_at=%s",
            tenant_id,
            provider_name,
            expires_at,
        )

    @classmethod
    def _consume_state(cls, state: str) -> Optional[Dict[str, Any]]:
        """H1: 消费并验证 state (使用后删除, 防重放)

        Args:
            state: 待验证的 state 字符串。

        Returns:
            state 关联数据 dict, 如果 state 不存在或已过期则返回 None。
        """
        # 清理过期 state (惰性清理)
        now = datetime.now(timezone.utc)
        expired_keys = [
            k
            for k, v in cls._state_store.items()
            if v.get("expires_at", now) < now
        ]
        for k in expired_keys:
            cls._state_store.pop(k, None)

        state_data = cls._state_store.pop(state, None)
        if state_data is None:
            return None

        # 再次检查是否过期 (双重保障)
        if state_data.get("expires_at", now) < now:
            return None

        return state_data

    @staticmethod
    def _escape_ldap_filter(value: str) -> str:
        """H2: 转义 LDAP 搜索过滤器中的特殊字符 (RFC 4515)

        转义字符: \\ * ( ) \\0 (NULL)
        """
        return value.translate(
            str.maketrans(
                {
                    "\\": "\\5c",
                    "*": "\\2a",
                    "(": "\\28",
                    ")": "\\29",
                    "\0": "\\00",
                }
            )
        )

    def _validate_config(self, provider_type: str, config: Dict[str, Any]) -> None:
        """校验配置必填字段

        Raises:
            ValueError: 缺少必填字段。
        """
        if not config or not isinstance(config, dict):
            raise ValueError("config 不能为空且必须是 dict")

        required: Dict[str, List[str]] = {
            "oauth2": ["client_id", "client_secret", "authorize_url", "token_url", "userinfo_url", "redirect_uri"],
            "saml": ["entity_id", "sso_url", "certificate"],
            "ldap": ["server_url", "search_base"],
        }
        for field in required.get(provider_type, []):
            if not config.get(field):
                raise ValueError(f"{provider_type} 配置缺少必填字段: {field}")

    @staticmethod
    def _mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """脱敏配置中的敏感信息 (用于 API 响应)

        将 client_secret / bind_password / certificate 等字段替换为 "****"。
        """
        if not config or not isinstance(config, dict):
            return config or {}
        masked = {}
        for k, v in config.items():
            if k in _SENSITIVE_KEYS and v:
                masked[k] = "****"
            else:
                masked[k] = v
        return masked

    @staticmethod
    def _config_to_dict(c: SSOConfig, mask_sensitive: bool = True) -> Dict[str, Any]:
        """SSOConfig → dict

        Args:
            c: SSOConfig 对象。
            mask_sensitive: True 时脱敏敏感信息 (API 响应), False 时返回明文 (内部使用)。
        """
        config_value = c.config if isinstance(c.config, dict) else {}
        return {
            "id": c.id,
            "tenant_id": c.tenant_id,
            "provider_name": c.provider_name,
            "provider_type": c.provider_type,
            "config": SSOService._mask_config(config_value) if mask_sensitive else config_value,
            "enabled": c.enabled,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }

    @staticmethod
    def _session_to_dict(s: SSOSession) -> Dict[str, Any]:
        """SSOSession → dict (token 脱敏)"""
        return {
            "id": s.id,
            "tenant_id": s.tenant_id,
            "provider_name": s.provider_name,
            "external_user_id": s.external_user_id,
            "internal_user_id": s.internal_user_id,
            "access_token": "****" if s.access_token else None,
            "refresh_token": "****" if s.refresh_token else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
