"""
Provider CRUD Admin API

对标 Dify Model Provider Controller (https://github.com/langgenius/dify/blob/main/api/controllers/console/workspace/model_providers.py)

路由前缀: /api/v1/admin/model-providers
权限: Role.ADMIN + slowapi 限流

完整功能(24 端点):
- Provider 模板列表 / 详情
- 租户 Provider 绑定(列表 / 启用禁用)
- 凭证 CRUD (多凭证 + 激活切换 + 测试连接)
- 模型管理 (启用 / 禁用 / 负载均衡)
- 模型凭证 CRUD (customizable-model)
- 默认模型管理
- 健康检查 (历史 + 触发主动检查)
"""

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.providers.credential_service import ProviderCredentialService
from core.tenant_context import get_current_tenant
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

router = APIRouter(
    prefix="/api/v1/admin/model-providers",
    tags=["admin-model-providers"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class CredentialCreate(BaseModel):
    credential_name: str = Field(..., max_length=128)
    credentials: Dict[str, Any]


class CredentialUpdate(BaseModel):
    credential_name: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None


class CredentialValidate(BaseModel):
    credentials: Dict[str, Any]


class ModelCreate(BaseModel):
    model_name: str
    model_type: str = "llm"
    credentials: Optional[Dict[str, Any]] = None
    credential_name: Optional[str] = None


class PreferredTypeUpdate(BaseModel):
    enabled: bool = Field(..., description="启用/禁用 Provider")


class DefaultModelSet(BaseModel):
    model_type: str
    provider: str
    model_name: str


class ModelCredentialCreate(BaseModel):
    credential_name: str
    credentials: Dict[str, Any]


# ============================================================
# Provider 模板 (静态注册表)
# ============================================================


@router.get("/providers")
async def list_provider_templates(
    model_type: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """列出所有 Provider 模板(支持按 model_type 过滤)"""
    stmt = select(ProviderTemplate).where(ProviderTemplate.enabled.is_(True))
    result = await session.execute(stmt)
    templates = result.scalars().all()

    data = []
    for t in templates:
        if model_type and model_type not in (t.supported_model_types or []):
            continue
        data.append(_serialize_provider_template(t))
    return {"data": data}


@router.get("/providers/{provider}")
async def get_provider_template(
    provider: str,
    session: AsyncSession = Depends(get_db),
):
    """取单个 Provider 详情(schema + 模型列表)"""
    stmt = select(ProviderTemplate).where(ProviderTemplate.provider == provider)
    result = await session.execute(stmt)
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not found")

    # 拉该 provider 下的所有模型模板
    mstmt = select(ModelTemplate).where(
        ModelTemplate.provider == provider,
        ModelTemplate.enabled.is_(True),
    )
    mresult = await session.execute(mstmt)
    models = mresult.scalars().all()

    return _serialize_provider_template(tmpl, models=models)


# ============================================================
# 租户 Provider 绑定
# ============================================================


@router.get("/workspaces/current/providers")
async def list_tenant_providers(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取当前 tenant 已配置的 provider 列表(含状态、凭证清单、模型清单)"""
    # 拉所有 provider 模板
    tstmt = select(ProviderTemplate).where(ProviderTemplate.enabled.is_(True))
    tresult = await session.execute(tstmt)
    templates = tresult.scalars().all()

    # 拉该 tenant 所有 provider 绑定
    pstmt = select(TenantProvider).where(TenantProvider.tenant_id == tenant_id)
    presult = await session.execute(pstmt)
    tenant_providers = {p.provider: p for p in presult.scalars().all()}

    # 拉所有凭证
    cstmt = select(TenantProviderCredential).where(
        TenantProviderCredential.tenant_id == tenant_id
    )
    cresult = await session.execute(cstmt)
    all_creds = cresult.scalars().all()
    creds_by_provider: Dict[str, List] = {}
    for c in all_creds:
        creds_by_provider.setdefault(c.provider, []).append(c)

    # 拉所有模型绑定
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.tenant_id == tenant_id
    )
    mresult = await session.execute(mstmt)
    tenant_models = mresult.scalars().all()
    models_by_provider: Dict[str, List] = {}
    for m in tenant_models:
        models_by_provider.setdefault(m.provider, []).append(m)

    data = []
    for tmpl in templates:
        tp = tenant_providers.get(tmpl.provider)
        creds = creds_by_provider.get(tmpl.provider, [])
        models = models_by_provider.get(tmpl.provider, [])
        data.append(
            _serialize_tenant_provider_view(tmpl, tp, creds, models)
        )
    return {"data": data}


@router.post("/workspaces/current/providers/{provider}/preferred-type")
async def update_preferred_type(
    provider: str,
    payload: PreferredTypeUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """启用/禁用 provider"""
    svc = ProviderCredentialService(session)
    tp = await svc.upsert_tenant_provider(tenant_id, provider)
    tp.enabled = payload.enabled
    if payload.enabled and not tp.preferred_type:
        tp.preferred_type = "custom"
    await session.commit()
    return {"result": "success", "enabled": tp.enabled}


# ============================================================
# Provider 凭证 CRUD
# ============================================================


@router.get("/workspaces/current/providers/{provider}/credentials")
async def list_credentials(
    provider: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取凭证列表(返回 mask 值,对标 Dify secret-input 脱敏)"""
    svc = ProviderCredentialService(session)
    creds = await svc.list_credentials(tenant_id, provider)
    tp = await svc.get_tenant_provider(tenant_id, provider)
    active_id = tp.active_credential_id if tp else None

    # 拉 provider 模板以获取 schema
    tmpl_stmt = select(ProviderTemplate).where(ProviderTemplate.provider == provider)
    tmpl = (await session.execute(tmpl_stmt)).scalar_one_or_none()
    schema = tmpl.provider_credential_schema if tmpl else None

    data = []
    for c in creds:
        try:
            plain = svc.decrypt_credential(c.encrypted_config)
            masked = svc.mask_credentials(plain, schema)
        except Exception:
            masked = {}
        data.append(
            {
                "credential_id": c.id,
                "name": c.credential_name,
                "is_active": c.id == active_id,
                "is_valid": c.is_valid,
                "in_cooldown": svc._is_in_cooldown(c),
                "failure_count": c.failure_count,
                "last_validated_at": c.last_validated_at.isoformat() if c.last_validated_at else None,
                "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None,
                "credentials_masked": masked,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
        )
    return {"data": data}


@router.post("/workspaces/current/providers/{provider}/credentials")
async def create_credential(
    provider: str,
    payload: CredentialCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建凭证(加密存储)"""
    svc = ProviderCredentialService(session)
    row, _ = await svc.create_credential(
        tenant_id=tenant_id,
        provider_name=provider,
        credential_name=payload.credential_name,
        credentials=payload.credentials,
    )
    await session.commit()
    return {"result": "success", "credential_id": row.id}, status.HTTP_201_CREATED


@router.put("/workspaces/current/providers/{provider}/credentials/{credential_id}")
async def update_credential(
    provider: str,
    credential_id: str,
    payload: CredentialUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新凭证"""
    svc = ProviderCredentialService(session)
    row = await svc.update_credential(
        tenant_id=tenant_id,
        provider_name=provider,
        credential_id=credential_id,
        credential_name=payload.credential_name,
        credentials=payload.credentials,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    await session.commit()
    return {"result": "success"}


@router.delete("/workspaces/current/providers/{provider}/credentials/{credential_id}")
async def delete_credential(
    provider: str,
    credential_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除凭证(如删除的是激活凭证,自动切换到其他可用凭证)"""
    svc = ProviderCredentialService(session)
    ok = await svc.delete_credential(tenant_id, provider, credential_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Credential not found")
    await session.commit()
    return {"result": "success"}


@router.post(
    "/workspaces/current/providers/{provider}/credentials/{credential_id}/activate"
)
async def activate_credential(
    provider: str,
    credential_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """切换激活凭证(对标 Dify switch 接口)"""
    svc = ProviderCredentialService(session)
    ok = await svc.activate_credential(tenant_id, provider, credential_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Credential not found")
    await session.commit()
    return {"result": "success"}


@router.post("/workspaces/current/providers/{provider}/credentials/validate")
async def validate_credentials(
    provider: str,
    payload: CredentialValidate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """测试连接(不入库,返回 result=success/error)"""
    try:
        result = await _validate_provider_credentials(provider, payload.credentials)
        return {"result": "success" if result else "error"}
    except Exception as e:
        logger.warning("Provider %s 凭证校验失败: %s", provider, e)
        return {"result": "error", "error": str(e)}


# ============================================================
# 模型管理
# ============================================================


@router.get("/workspaces/current/providers/{provider}/models")
async def list_tenant_models(
    provider: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """列出该 provider 下所有模型(预定义 + tenant 启用状态)"""
    # 1. 模型模板(预定义)
    tstmt = select(ModelTemplate).where(
        ModelTemplate.provider == provider,
        ModelTemplate.enabled.is_(True),
    )
    tresult = await session.execute(tstmt)
    templates = tresult.scalars().all()

    # 2. tenant 已启用的模型
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    mresult = await session.execute(mstmt)
    tenant_models = {m.model_name: m for m in mresult.scalars().all()}

    data = []
    for tmpl in templates:
        tm = tenant_models.get(tmpl.model)
        data.append(
            {
                "model": tmpl.model,
                "label": tmpl.label,
                "model_type": tmpl.model_type,
                "features": tmpl.features or [],
                "model_properties": tmpl.model_properties,
                "parameter_rules": tmpl.parameter_rules or [],
                "pricing": tmpl.pricing,
                "enabled": tm.enabled if tm else False,
                "is_valid": tm.is_valid if tm else False,
                "load_balancing_enabled": tm.load_balancing_enabled if tm else False,
                "active_credential_id": tm.active_credential_id if tm else None,
            }
        )
    return {"data": data}


@router.post("/workspaces/current/providers/{provider}/models")
async def create_tenant_model(
    provider: str,
    payload: ModelCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """添加/启用模型(customizable-model 场景)"""
    # upsert tenant_provider_model
    stmt = select(TenantProviderModel).where(
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
        TenantProviderModel.model_name == payload.model_name,
        TenantProviderModel.model_type == payload.model_type,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        existing.enabled = True
        row = existing
    else:
        import uuid

        row = TenantProviderModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider,
            model_name=payload.model_name,
            model_type=payload.model_type,
            enabled=True,
        )
        session.add(row)
    await session.flush()

    # 若提供了凭证,创建模型级凭证
    if payload.credentials and payload.credential_name:
        svc = ProviderCredentialService(session)
        encrypted = svc.encrypt_credential(payload.credentials)
        import uuid

        cred_row = TenantProviderModelCredential(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider,
            model_name=payload.model_name,
            model_type=payload.model_type,
            credential_name=payload.credential_name,
            encrypted_config=encrypted,
            is_valid=True,
        )
        session.add(cred_row)
        await session.flush()
        row.active_credential_id = cred_row.id

    await session.commit()
    return {"result": "success", "model_id": row.id}, status.HTTP_201_CREATED


@router.delete(
    "/workspaces/current/providers/{provider}/models/{model_id}"
)
async def delete_tenant_model(
    provider: str,
    model_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除/禁用模型"""
    stmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    await session.delete(row)
    await session.commit()
    return {"result": "success"}


@router.post(
    "/workspaces/current/providers/{provider}/models/{model_id}/toggle"
)
async def toggle_model(
    provider: str,
    model_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """启用/禁用模型"""
    stmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    row.enabled = not row.enabled
    await session.commit()
    return {"result": "success", "enabled": row.enabled}


@router.post(
    "/workspaces/current/providers/{provider}/models/{model_id}/load-balancing/toggle"
)
async def toggle_load_balancing(
    provider: str,
    model_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """开关负载均衡"""
    stmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    row.load_balancing_enabled = not row.load_balancing_enabled
    await session.commit()
    return {"result": "success", "load_balancing_enabled": row.load_balancing_enabled}


# ============================================================
# 模型级凭证 CRUD (customizable-model + LB)
# ============================================================


@router.get(
    "/workspaces/current/providers/{provider}/models/{model_id}/credentials"
)
async def list_model_credentials(
    provider: str,
    model_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取模型凭证列表"""
    # 先拿模型绑定以获取 model_name + model_type
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    mresult = await session.execute(mstmt)
    model = mresult.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    stmt = select(TenantProviderModelCredential).where(
        TenantProviderModelCredential.tenant_id == tenant_id,
        TenantProviderModelCredential.provider == provider,
        TenantProviderModelCredential.model_name == model.model_name,
        TenantProviderModelCredential.model_type == model.model_type,
    )
    result = await session.execute(stmt)
    creds = result.scalars().all()

    svc = ProviderCredentialService(session)
    data = []
    for c in creds:
        try:
            plain = svc.decrypt_credential(c.encrypted_config)
            masked = svc.mask_credentials(plain)
        except Exception:
            masked = {}
        data.append(
            {
                "credential_id": c.id,
                "name": c.credential_name,
                "is_active": c.id == model.active_credential_id,
                "is_valid": c.is_valid,
                "in_cooldown": svc._is_in_cooldown(c),
                "failure_count": c.failure_count,
                "credentials_masked": masked,
            }
        )
    return {"data": data}


@router.post(
    "/workspaces/current/providers/{provider}/models/{model_id}/credentials"
)
async def create_model_credential(
    provider: str,
    model_id: str,
    payload: ModelCredentialCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建模型凭证"""
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    mresult = await session.execute(mstmt)
    model = mresult.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    svc = ProviderCredentialService(session)
    encrypted = svc.encrypt_credential(payload.credentials)
    import uuid

    row = TenantProviderModelCredential(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        provider=provider,
        model_name=model.model_name,
        model_type=model.model_type,
        credential_name=payload.credential_name,
        encrypted_config=encrypted,
        is_valid=True,
    )
    session.add(row)
    await session.flush()

    if not model.active_credential_id:
        model.active_credential_id = row.id
    await session.commit()
    return {"result": "success", "credential_id": row.id}, status.HTTP_201_CREATED


@router.delete(
    "/workspaces/current/providers/{provider}/models/{model_id}/credentials/{credential_id}"
)
async def delete_model_credential(
    provider: str,
    model_id: str,
    credential_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除模型凭证"""
    stmt = select(TenantProviderModelCredential).where(
        TenantProviderModelCredential.id == credential_id,
        TenantProviderModelCredential.tenant_id == tenant_id,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    await session.delete(row)
    await session.commit()
    return {"result": "success"}


@router.post(
    "/workspaces/current/providers/{provider}/models/{model_id}/credentials/{credential_id}/activate"
)
async def activate_model_credential(
    provider: str,
    model_id: str,
    credential_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """切换激活模型凭证"""
    stmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    row.active_credential_id = credential_id
    await session.commit()
    return {"result": "success"}


@router.post(
    "/workspaces/current/providers/{provider}/models/{model_id}/credentials/validate"
)
async def validate_model_credentials(
    provider: str,
    model_id: str,
    payload: CredentialValidate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """测试模型凭证"""
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    mresult = await session.execute(mstmt)
    model = mresult.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    try:
        result = await _validate_provider_credentials(
            provider, payload.credentials, model_name=model.model_name
        )
        return {"result": "success" if result else "error"}
    except Exception as e:
        return {"result": "error", "error": str(e)}


@router.get(
    "/workspaces/current/providers/{provider}/models/{model_id}/parameter-rules"
)
async def get_parameter_rules(
    provider: str,
    model_id: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取推理参数规则"""
    mstmt = select(TenantProviderModel).where(
        TenantProviderModel.id == model_id,
        TenantProviderModel.tenant_id == tenant_id,
        TenantProviderModel.provider == provider,
    )
    mresult = await session.execute(mstmt)
    model = mresult.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    tstmt = select(ModelTemplate).where(
        ModelTemplate.provider == provider,
        ModelTemplate.model == model.model_name,
        ModelTemplate.model_type == model.model_type,
    )
    tresult = await session.execute(tstmt)
    tmpl = tresult.scalar_one_or_none()
    return {"parameter_rules": tmpl.parameter_rules if tmpl else []}


# ============================================================
# 默认模型
# ============================================================


@router.get("/workspaces/current/default-models")
async def list_default_models(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取默认模型列表"""
    stmt = select(TenantDefaultModel).where(
        TenantDefaultModel.tenant_id == tenant_id
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return {
        "data": [
            {
                "model_type": r.model_type,
                "provider": r.provider,
                "model_name": r.model_name,
            }
            for r in rows
        ]
    }


@router.post("/workspaces/current/default-models")
async def set_default_model(
    payload: DefaultModelSet,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """设置默认模型"""
    svc = ProviderCredentialService(session)
    await svc.set_default_model(
        tenant_id=tenant_id,
        model_type=payload.model_type,
        provider_name=payload.provider,
        model_name=payload.model_name,
    )
    await session.commit()
    return {"result": "success"}


# ============================================================
# 健康检查
# ============================================================


@router.get("/workspaces/current/providers/{provider}/health-checks")
async def list_health_checks(
    provider: str,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """取健康检查历史"""
    svc = ProviderCredentialService(session)
    rows = await svc.list_health_checks(tenant_id, provider, limit=limit)
    return {
        "data": [
            {
                "id": r.id,
                "credential_id": r.credential_id,
                "model_name": r.model_name,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "error_message": r.error_message,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in rows
        ]
    }


@router.post("/workspaces/current/providers/{provider}/health-check")
async def trigger_health_check(
    provider: str,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """触发一次主动健康检查(对标 LiteLLM background_health_checks)"""
    svc = ProviderCredentialService(session)
    creds = await svc.list_credentials(tenant_id, provider)
    if not creds:
        return {"result": "error", "error": "no credentials configured"}

    # 拿激活凭证做 ping
    tp = await svc.get_tenant_provider(tenant_id, provider)
    active_id = tp.active_credential_id if tp else None
    cred = next((c for c in creds if c.id == active_id), creds[0])
    try:
        plain = svc.decrypt_credential(cred.encrypted_config)
        start = time.time()
        ok = await _validate_provider_credentials(provider, plain)
        latency_ms = int((time.time() - start) * 1000)
        if ok:
            await svc.record_success(tenant_id, provider, cred.id, latency_ms)
        else:
            await svc.record_failure(tenant_id, provider, cred.id, "validation failed")
        await session.commit()
        return {"result": "success" if ok else "error", "latency_ms": latency_ms}
    except Exception as e:
        await svc.record_failure(tenant_id, provider, cred.id, str(e))
        await session.commit()
        return {"result": "error", "error": str(e)}


# ============================================================
# Helpers
# ============================================================


def _serialize_provider_template(tmpl: ProviderTemplate, models=None) -> Dict[str, Any]:
    """序列化 ProviderTemplate"""
    models_by_type: Dict[str, List] = {}
    if models:
        for m in models:
            models_by_type.setdefault(m.model_type, []).append(
                {
                    "model": m.model,
                    "label": m.label,
                    "model_type": m.model_type,
                    "features": m.features or [],
                    "model_properties": m.model_properties,
                    "parameter_rules": m.parameter_rules or [],
                    "pricing": m.pricing,
                }
            )
    return {
        "provider": tmpl.provider,
        "label": tmpl.label,
        "description": tmpl.description,
        "icon_small": tmpl.icon_small,
        "icon_large": tmpl.icon_large,
        "background": tmpl.background,
        "supported_model_types": tmpl.supported_model_types or [],
        "configurate_methods": tmpl.configurate_methods or [],
        "provider_credential_schema": tmpl.provider_credential_schema,
        "model_credential_schema": tmpl.model_credential_schema,
        "models": models_by_type,
    }


def _serialize_tenant_provider_view(
    tmpl: ProviderTemplate,
    tp: Optional[TenantProvider],
    creds: List[TenantProviderCredential],
    models: List[TenantProviderModel],
) -> Dict[str, Any]:
    """序列化租户 Provider 视图(含状态 + 凭证 + 模型)"""
    active_cred_id = tp.active_credential_id if tp else None
    # mask 凭证 (用模块级 helper, 不依赖 session)
    cred_list = []
    for c in creds:
        try:
            plain = _decrypt_credential_static(c.encrypted_config)
            masked = ProviderCredentialService.mask_credentials(
                plain, tmpl.provider_credential_schema
            )
        except Exception:
            masked = {}
        cred_list.append(
            {
                "credential_id": c.id,
                "name": c.credential_name,
                "is_active": c.id == active_cred_id,
                "is_valid": c.is_valid,
                "in_cooldown": bool(
                    c.cooldown_until
                    and c.cooldown_until.timestamp() > time.time()
                ),
                "failure_count": c.failure_count,
                "credentials_masked": masked,
            }
        )

    model_list = [
        {
            "model_name": m.model_name,
            "model_type": m.model_type,
            "enabled": m.enabled,
            "is_valid": m.is_valid,
            "load_balancing_enabled": m.load_balancing_enabled,
            "active_credential_id": m.active_credential_id,
        }
        for m in models
    ]

    # 状态聚合
    if not tp or not tp.enabled:
        status = "unconfigured"
    elif any(c.is_valid for c in creds) if creds else tp.is_valid:
        status = "active"
    else:
        status = "error"

    return {
        **_serialize_provider_template(tmpl),
        "status": status,
        "status_info": status,
        "enabled": bool(tp.enabled) if tp else False,
        "preferred_provider_type": tp.preferred_type if tp and tp.enabled else None,
        "custom_configuration": {
            "provider": {
                "credentials": cred_list,
            },
            "models": model_list,
        },
    }


async def _validate_provider_credentials(
    provider: str,
    credentials: Dict[str, Any],
    model_name: Optional[str] = None,
) -> bool:
    """校验凭证(调 provider 的探活接口)。

    对标 Dify validate_provider_credentials:通常发一次轻量 API 调用验证。
    """
    # 延迟 import 避免循环依赖
    try:
        if provider == "openai":
            api_key = credentials.get("api_key")
            api_base = credentials.get(
                "api_base", "https://api.openai.com/v1"
            )
            if not api_key:
                return False
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{api_base}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                return resp.status_code == 200
        elif provider == "anthropic":
            api_key = credentials.get("api_key")
            if not api_key:
                return False
            # Anthropic 没有公开 /models 端点,用 messages API 发一个最小请求
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model_name or "claude-3-5-haiku-20241022",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
                # 401 表示 key 无效,其他都算 key 有效
                return resp.status_code != 401
        elif provider == "gemini":
            api_key = credentials.get("api_key")
            if not api_key:
                return False
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
                )
                return resp.status_code == 200
        elif provider == "ollama":
            api_base = credentials.get(
                "api_base", "http://localhost:11434"
            )
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{api_base}/api/tags")
                return resp.status_code == 200
        return False
    except Exception as e:
        logger.warning("validate_provider_credentials 失败 provider=%s err=%s", provider, e)
        return False


def _decrypt_credential_static(encrypted_config: str) -> Dict[str, Any]:
    """模块级静态解密 helper (不依赖 session,用于序列化)"""
    import json

    from core.config import get_settings
    from core.field_crypto import FieldCipher

    settings = get_settings()
    key = getattr(settings, "field_encryption_key", None)
    cipher = FieldCipher(key)
    decrypted = cipher.decrypt(encrypted_config)
    if isinstance(decrypted, bytes):
        decrypted = decrypted.decode("utf-8")
    return json.loads(decrypted)
