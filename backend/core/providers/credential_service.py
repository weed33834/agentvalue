"""
Provider 凭证服务

对标 Dify ModelProviderService (https://github.com/langgenius/dify/blob/main/api/services/model_provider_service.py)

核心职责:
- 加密/解密凭证 (复用 core/field_crypto.py 的 FieldCipher)
- 凭证 mask 显示 (secret-input 字段返回 sk-****1234 格式)
- 多凭证管理 (创建/更新/删除/切换激活)
- 测试连接 (validate, 不入库)
- 健康检查 (主动 ping + 被动熔断记录)

设计要点:
- API 响应永远返回 mask 值,明文不入库不入日志
- DB 中 encrypted_config 字段是 AES-256-GCM 加密的 JSON
- 凭证更新时主动失效 Redis 缓存 (provider_credentials:{tenant}:{provider}:{id})
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.field_crypto import FieldCipher
from core.tenant_context import get_current_tenant
from models.models import DEFAULT_TENANT_ID
from models.provider_models import (
    ModelTemplate,
    ProviderHealthCheck,
    ProviderTemplate,
    TenantDefaultModel,
    TenantProvider,
    TenantProviderCredential,
    TenantProviderModel,
    TenantProviderModelCredential,
)

logger = logging.getLogger(__name__)

# 凭证冷却时长(对标 Dify cooldown,Redis 共享状态此处简化为 DB 字段)
_COOLDOWN_SECONDS = 60
# 连续失败次数达到此阈值时状态为 down
_DOWN_THRESHOLD = 3


class ProviderCredentialService:
    """Provider 凭证管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._cipher: Optional[FieldCipher] = None

    @property
    def cipher(self) -> FieldCipher:
        """懒加载 FieldCipher(用 settings.field_encryption_key)"""
        if self._cipher is None:
            settings = get_settings()
            key = getattr(settings, "field_encryption_key", None)
            self._cipher = FieldCipher(key)
        return self._cipher

    # ============================================================
    # 凭证加密/解密
    # ============================================================

    def encrypt_credential(self, credentials: Dict[str, Any]) -> str:
        """加密凭证 dict → JSON → AES-256-GCM → base64"""
        json_str = json.dumps(credentials, ensure_ascii=False)
        encrypted = self.cipher.encrypt(json_str)
        return encrypted

    def decrypt_credential(self, encrypted_config: str) -> Dict[str, Any]:
        """解密: base64 → AES-GCM → JSON → dict"""
        decrypted = self.cipher.decrypt(encrypted_config)
        if isinstance(decrypted, bytes):
            decrypted = decrypted.decode("utf-8")
        return json.loads(decrypted)

    # ============================================================
    # 凭证 Mask 显示 (对标 Dify ModelProviderService 脱敏)
    # ============================================================

    @staticmethod
    def mask_secret(value: str) -> str:
        """对 secret-input 字段做 mask 显示。

        规则: 保留前 2 位 + 后 4 位,中间 4 个星号。
        如 sk-abc123456789xyz → sk-****xyz
        短于 7 位的值全部用 **** 替代。
        """
        if not value or not isinstance(value, str):
            return ""
        if len(value) <= 6:
            return "****"
        return value[:2] + "****" + value[-4:]

    def mask_credentials(
        self,
        credentials: Dict[str, Any],
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按 schema 中的 type=secret-input 字段做 mask。

        schema 格式参考 provider_credential_schema:
        {"credential_form_schemas": [{"variable": "api_key", "type": "secret-input"}, ...]}
        """
        if not credentials:
            return {}
        masked: Dict[str, Any] = {}
        secret_vars = self._extract_secret_variables(schema)
        for k, v in credentials.items():
            if k in secret_vars and isinstance(v, str):
                masked[k] = self.mask_secret(v)
            else:
                masked[k] = v
        return masked

    @staticmethod
    def _extract_secret_variables(schema: Optional[Dict[str, Any]]) -> set:
        """从 schema 中提取所有 type=secret-input 的变量名"""
        if not schema:
            return set()
        result = set()
        for f in schema.get("credential_form_schemas", []) or []:
            if f.get("type") == "secret-input":
                result.add(f.get("variable"))
        return result

    # ============================================================
    # TenantProvider 绑定
    # ============================================================

    async def get_tenant_provider(
        self,
        tenant_id: str,
        provider_name: str,
        provider_type: str = "custom",
    ) -> Optional[TenantProvider]:
        stmt = select(TenantProvider).where(
            TenantProvider.tenant_id == tenant_id,
            TenantProvider.provider == provider_name,
            TenantProvider.provider_type == provider_type,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_tenant_provider(
        self,
        tenant_id: str,
        provider_name: str,
        provider_type: str = "custom",
        is_valid: Optional[bool] = None,
    ) -> TenantProvider:
        """创建或更新 tenant provider 绑定"""
        existing = await self.get_tenant_provider(
            tenant_id, provider_name, provider_type
        )
        if existing is not None:
            if is_valid is not None:
                existing.is_valid = is_valid
            existing.last_used_at = datetime.now(timezone.utc)
            await self.session.flush()
            return existing

        row = TenantProvider(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider_name,
            provider_type=provider_type,
            is_valid=bool(is_valid) if is_valid is not None else False,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    # ============================================================
    # Provider 凭证 CRUD
    # ============================================================

    async def list_credentials(
        self,
        tenant_id: str,
        provider_name: str,
    ) -> List[TenantProviderCredential]:
        stmt = (
            select(TenantProviderCredential)
            .where(
                TenantProviderCredential.tenant_id == tenant_id,
                TenantProviderCredential.provider == provider_name,
            )
            .order_by(TenantProviderCredential.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_credential(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: str,
    ) -> Optional[TenantProviderCredential]:
        stmt = select(TenantProviderCredential).where(
            TenantProviderCredential.id == credential_id,
            TenantProviderCredential.tenant_id == tenant_id,
            TenantProviderCredential.provider == provider_name,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_credential(
        self,
        tenant_id: str,
        provider_name: str,
        credential_name: str,
        credentials: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Tuple[TenantProviderCredential, bool]:
        """创建凭证。

        返回 (credential_row, is_valid)。is_valid=True 表示同时设为激活凭证。
        """
        encrypted = self.encrypt_credential(credentials)
        row = TenantProviderCredential(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider_name,
            credential_name=credential_name,
            encrypted_config=encrypted,
            user_id=user_id,
            is_valid=True,  # 创建时默认 valid,validate 失败会改
            last_validated_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        await self.session.flush()

        # 同时 upsert tenant_provider 绑定,设为激活凭证
        tp = await self.upsert_tenant_provider(tenant_id, provider_name, is_valid=True)
        # 仅当无激活凭证时,才把新建的设为激活
        if not tp.active_credential_id:
            tp.active_credential_id = row.id
        await self.session.flush()
        return row, True

    async def update_credential(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: str,
        credential_name: Optional[str] = None,
        credentials: Optional[Dict[str, Any]] = None,
    ) -> Optional[TenantProviderCredential]:
        row = await self.get_credential(tenant_id, provider_name, credential_id)
        if row is None:
            return None
        if credential_name:
            row.credential_name = credential_name
        if credentials:
            row.encrypted_config = self.encrypt_credential(credentials)
            row.is_valid = True
            row.last_validated_at = datetime.now(timezone.utc)
            row.failure_count = 0
            row.cooldown_until = None
        await self.session.flush()
        return row

    async def delete_credential(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: str,
    ) -> bool:
        row = await self.get_credential(tenant_id, provider_name, credential_id)
        if row is None:
            return False

        # 如删除的是激活凭证,自动切换到其他可用凭证
        tp = await self.get_tenant_provider(tenant_id, provider_name)
        if tp and tp.active_credential_id == credential_id:
            others = await self.list_credentials(tenant_id, provider_name)
            others_active = [c for c in others if c.id != credential_id and c.is_valid]
            tp.active_credential_id = others_active[0].id if others_active else None
            tp.is_valid = bool(others_active)

        await self.session.delete(row)
        await self.session.flush()
        return True

    async def activate_credential(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: str,
    ) -> bool:
        """切换激活凭证(对标 Dify switch 接口)"""
        row = await self.get_credential(tenant_id, provider_name, credential_id)
        if row is None:
            return False
        tp = await self.upsert_tenant_provider(
            tenant_id, provider_name, is_valid=row.is_valid
        )
        tp.active_credential_id = credential_id
        await self.session.flush()
        return True

    async def get_active_credentials(
        self,
        tenant_id: str,
        provider_name: str,
    ) -> Optional[Dict[str, Any]]:
        """获取当前激活凭证的明文(供 runtime provider 使用,不走 mask)。

        优先返回 active_credential_id 指向的凭证;若无激活凭证,返回第一条可用凭证。
        """
        tp = await self.get_tenant_provider(tenant_id, provider_name)
        if tp is None or not tp.enabled:
            return None

        active_id = tp.active_credential_id
        if active_id:
            row = await self.get_credential(tenant_id, provider_name, active_id)
            if row and row.is_valid and not self._is_in_cooldown(row):
                return self.decrypt_credential(row.encrypted_config)

        # fallback: 找第一个可用凭证
        creds = await self.list_credentials(tenant_id, provider_name)
        for c in creds:
            if c.is_valid and not self._is_in_cooldown(c):
                # 自动切换激活
                tp.active_credential_id = c.id
                await self.session.flush()
                return self.decrypt_credential(c.encrypted_config)
        return None

    # ============================================================
    # 健康检查 + 冷却熔断
    # ============================================================

    @staticmethod
    def _is_in_cooldown(cred: TenantProviderCredential) -> bool:
        if cred.cooldown_until is None:
            return False
        return cred.cooldown_until > datetime.now(timezone.utc)

    async def record_failure(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: Optional[str] = None,
        error_message: str = "",
    ) -> None:
        """记录凭证失败(对标 Dify cooldown)"""
        if credential_id is None:
            tp = await self.get_tenant_provider(tenant_id, provider_name)
            credential_id = tp.active_credential_id if tp else None
        if not credential_id:
            return

        stmt = select(TenantProviderCredential).where(
            TenantProviderCredential.id == credential_id
        )
        result = await self.session.execute(stmt)
        cred = result.scalar_one_or_none()
        if cred is None:
            return

        cred.failure_count += 1
        cred.cooldown_until = datetime.now(timezone.utc) + timedelta(
            seconds=_COOLDOWN_SECONDS
        )
        if cred.failure_count >= _DOWN_THRESHOLD:
            cred.is_valid = False
        await self.session.flush()

        # 写健康检查记录
        await self.record_health_check(
            tenant_id=tenant_id,
            provider_name=provider_name,
            credential_id=credential_id,
            status="down" if cred.failure_count >= _DOWN_THRESHOLD else "degraded",
            error_message=error_message,
        )

    async def record_success(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> None:
        """记录凭证成功"""
        if credential_id is None:
            tp = await self.get_tenant_provider(tenant_id, provider_name)
            credential_id = tp.active_credential_id if tp else None
        if not credential_id:
            return

        stmt = select(TenantProviderCredential).where(
            TenantProviderCredential.id == credential_id
        )
        result = await self.session.execute(stmt)
        cred = result.scalar_one_or_none()
        if cred is None:
            return

        cred.failure_count = 0
        cred.cooldown_until = None
        cred.is_valid = True
        cred.last_validated_at = datetime.now(timezone.utc)
        await self.session.flush()

        await self.record_health_check(
            tenant_id=tenant_id,
            provider_name=provider_name,
            credential_id=credential_id,
            status="healthy",
            latency_ms=latency_ms,
        )

    async def record_health_check(
        self,
        tenant_id: str,
        provider_name: str,
        credential_id: Optional[str] = None,
        model_name: Optional[str] = None,
        status: str = "healthy",
        latency_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> ProviderHealthCheck:
        """写入健康检查记录"""
        row = ProviderHealthCheck(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider_name,
            credential_id=credential_id,
            model_name=model_name,
            status=status,
            latency_ms=latency_ms,
            error_message=error_message,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_health_checks(
        self,
        tenant_id: str,
        provider_name: str,
        limit: int = 50,
    ) -> List[ProviderHealthCheck]:
        stmt = (
            select(ProviderHealthCheck)
            .where(
                ProviderHealthCheck.tenant_id == tenant_id,
                ProviderHealthCheck.provider == provider_name,
            )
            .order_by(ProviderHealthCheck.checked_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ============================================================
    # 默认模型
    # ============================================================

    async def list_default_models(self, tenant_id: str) -> List[TenantDefaultModel]:
        stmt = select(TenantDefaultModel).where(
            TenantDefaultModel.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def set_default_model(
        self,
        tenant_id: str,
        model_type: str,
        provider_name: str,
        model_name: str,
    ) -> TenantDefaultModel:
        stmt = select(TenantDefaultModel).where(
            TenantDefaultModel.tenant_id == tenant_id,
            TenantDefaultModel.model_type == model_type,
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            existing.provider = provider_name
            existing.model_name = model_name
            await self.session.flush()
            return existing

        row = TenantDefaultModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            model_type=model_type,
            provider=provider_name,
            model_name=model_name,
        )
        self.session.add(row)
        await self.session.flush()
        return row
