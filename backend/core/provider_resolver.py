"""
Provider 解析器

从 backend/api/admin/playground.py 抽取的 provider 解析逻辑，供 playground 与 chat session 共用。

职责：按 model_name 路由到对应 Provider 实例，复用 tenant 凭证 + settings 兜底。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from sqlalchemy import select

from core.config import get_settings
from core.providers.base import BaseProvider, ProviderConfig
from core.tenant_context import get_current_tenant

logger = logging.getLogger(__name__)


async def get_provider_for_model(model_name: str) -> Optional[BaseProvider]:
    """按 model_name 路由到对应 Provider 实例。

    优先级：
    1. 从 tenant_provider_models 查找 model_name 对应 provider + 活跃凭证（若已配置）
    2. 按 model_name 前缀（gpt/claude/gemini/llama）选择 Provider 类，
       凭证从 settings.cloud_api_key 兜底（便于未配置也能跑 OpenAI）
    3. 失败则返回 None

    Args:
        model_name: 模型名（如 gpt-4o-mini / claude-3-5-sonnet / gemini-1.5-pro / llama3.1:8b）

    Returns:
        BaseProvider 实例，或 None（无法识别 model_name）
    """
    settings = get_settings()

    # 1. 先查 DB：tenant 是否为该 model 配置过凭证
    try:
        provider_name, credentials = await _lookup_tenant_credential_for_model(
            model_name
        )
    except Exception as e:
        logger.warning("_lookup_tenant_credential_for_model 失败: %s", e)
        provider_name, credentials = None, None

    # 2. 若 DB 未命中，按 model_name 前缀推断 provider + 从 settings 兜底
    if provider_name is None:
        provider_name, credentials = _infer_provider_from_model_name(
            model_name, settings
        )

    if provider_name is None:
        logger.warning("无法识别 model_name=%s 的 provider 类型", model_name)
        return None

    # 3. 构造 ProviderConfig
    api_key = (credentials or {}).get("api_key") if credentials else None
    api_base = (credentials or {}).get("api_base")

    config = ProviderConfig(
        model_name=model_name,
        api_key=api_key,
        base_url=api_base,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        model_tier="chat",
        request_timeout=settings.llm_request_timeout,
    )

    return _instantiate_provider(provider_name, config)


def _instantiate_provider(
    provider_name: str, config: ProviderConfig
) -> Optional[BaseProvider]:
    """按 provider_name 实例化对应 Provider。"""
    from core.providers import (
        AnthropicProvider,
        GeminiProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
    )

    if provider_name == "anthropic":
        return AnthropicProvider(config)
    if provider_name == "gemini":
        return GeminiProvider(config)
    if provider_name == "ollama":
        return OllamaProvider(config)
    # 默认走 OpenAI 兼容
    return OpenAICompatibleProvider(config)


async def _lookup_tenant_credential_for_model(
    model_name: str,
) -> Tuple[Optional[str], Optional[dict]]:
    """从 tenant_provider_models + tenant_provider_credentials 查活跃凭证。

    返回 (provider_name, credentials_dict) 或 (None, None)。
    """
    from core.database import get_db_session
    from core.providers.credential_service import ProviderCredentialService
    from models.provider_models import (
        TenantProvider,
        TenantProviderCredential,
        TenantProviderModel,
    )

    tenant_id = get_current_tenant()
    async with get_db_session() as sess:
        # 找 tenant 中匹配 model_name 的绑定
        mstmt = select(TenantProviderModel).where(
            TenantProviderModel.tenant_id == tenant_id,
            TenantProviderModel.model_name == model_name,
            TenantProviderModel.enabled.is_(True),
        )
        mresult = await sess.execute(mstmt)
        tm = mresult.scalar_one_or_none()
        provider_name = None
        active_cred_id = None
        if tm:
            provider_name = tm.provider
            active_cred_id = tm.active_credential_id
        else:
            cstmt = select(TenantProvider, TenantProviderCredential).where(
                TenantProvider.tenant_id == tenant_id,
                TenantProvider.enabled.is_(True),
                TenantProviderCredential.tenant_id == tenant_id,
            )
            cresult = await sess.execute(cstmt)
            for tp, cred in cresult.all():
                if tp.active_credential_id == cred.id:
                    provider_name = tp.provider
                    active_cred_id = cred.id
                    break
        if not provider_name or not active_cred_id:
            return None, None
        cred_stmt = select(TenantProviderCredential).where(
            TenantProviderCredential.id == active_cred_id
        )
        cred_result = await sess.execute(cred_stmt)
        cred_row = cred_result.scalar_one_or_none()
        if not cred_row:
            return provider_name, None
        svc = ProviderCredentialService(sess)
        plain = svc.decrypt_credential(cred_row.encrypted_config)
        return provider_name, plain


def _infer_provider_from_model_name(
    model_name: str, settings: Any
) -> Tuple[Optional[str], Optional[dict]]:
    """按 model_name 前缀推断 provider，凭证从 settings 兜底。"""
    name = (model_name or "").lower()
    if name.startswith("claude") or "anthropic" in name:
        return "anthropic", {
            "api_key": getattr(settings, "anthropic_api_key", None) or ""
        }
    if name.startswith("gemini") or "gemini" in name:
        return "gemini", {"api_key": getattr(settings, "gemini_api_key", None) or ""}
    if (
        name.startswith("llama")
        or name.startswith("qwen")
        or "ollama" in name
        or ":" in name
    ):
        return "ollama", {
            "api_base": getattr(settings, "local_base_url", None)
            or "http://localhost:11434"
        }
    # 默认 OpenAI 兼容
    api_key = settings.cloud_api_key or getattr(settings, "openai_api_key", None)
    api_base = settings.cloud_base_url or getattr(settings, "openai_base_url", None)
    return "openai", {"api_key": api_key, "api_base": api_base}
