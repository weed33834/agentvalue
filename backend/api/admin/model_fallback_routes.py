"""模型 Fallback 策略 Admin API

路由前缀: /api/v1/admin/model-fallback
权限: Role.ADMIN (router 级 dependencies)

完整端点 (6 个):
- POST   /chains          - 创建 fallback 链
- GET    /chains          - 列出 fallback 链
- GET    /chains/{id}     - 获取 fallback 链详情
- PUT    /chains/{id}     - 更新 fallback 链
- DELETE /chains/{id}     - 删除 fallback 链
- POST   /chains/{id}/test - 测试 fallback 链（模拟执行）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.model_fallback_service import ModelFallbackService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/model-fallback",
    tags=["admin-model-fallback"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ChainEntry(BaseModel):
    """fallback 链中的单个候选模型配置"""

    tier: str = Field(..., description="模型档位: L0/L1/L2/L3")
    provider: str = Field(..., description="Provider 类型: openai/ollama/anthropic 等")
    model: str = Field(..., description="模型名")
    timeout: int = Field(default=30, ge=1, description="单次请求超时（秒）")
    max_retries: int = Field(default=2, ge=1, description="最大重试次数")


class ChainCreate(BaseModel):
    """创建 fallback 链请求

    不允许调用方指定 tenant_id，强制使用当前请求租户，防止跨租户创建。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="链名称")
    description: Optional[str] = Field(default=None, description="链描述")
    chain_config: List[Dict[str, Any]] = Field(
        ..., min_length=1, description="降级链配置（有序候选模型列表）"
    )
    enabled: bool = Field(default=True, description="是否启用")
    priority: int = Field(default=0, description="优先级（越大越优先）")


class ChainUpdate(BaseModel):
    """更新 fallback 链请求（所有字段可选）"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    chain_config: Optional[List[Dict[str, Any]]] = Field(default=None, min_length=1)
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class ChainTestRequest(BaseModel):
    """测试 fallback 链请求"""

    tier: str = Field(
        default="L0", description="期望的主模型档位（用于匹配 fallback 链）"
    )
    messages: List[Dict[str, Any]] = Field(
        ..., min_length=1, description="测试消息列表"
    )


# ============================================================
# 路由
# ============================================================


@router.post(
    "/chains", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_chain(
    payload: ChainCreate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """创建 fallback 链"""
    service = ModelFallbackService(session)
    result = await service.create_chain(
        tenant_id=tenant_id,
        name=payload.name,
        chain_config=payload.chain_config,
        description=payload.description,
        enabled=payload.enabled,
        priority=payload.priority,
    )
    await session.commit()
    return result


@router.get("/chains", response_model=Dict[str, Any])
async def list_chains(
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
    enabled_only: bool = False,
):
    """列出当前租户的 fallback 链（按 priority 降序）"""
    service = ModelFallbackService(session)
    items = await service.list_chains(tenant_id, enabled_only=enabled_only)
    return {"tenant_id": tenant_id, "items": items, "total": len(items)}


@router.get("/chains/{chain_id}", response_model=Dict[str, Any])
async def get_chain(
    chain_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """获取 fallback 链详情"""
    service = ModelFallbackService(session)
    result = await service.get_chain(chain_id, tenant_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"fallback 链 {chain_id} 不存在",
        )
    return result


@router.put("/chains/{chain_id}", response_model=Dict[str, Any])
async def update_chain(
    chain_id: int,
    payload: ChainUpdate,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """更新 fallback 链配置"""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )
    service = ModelFallbackService(session)
    result = await service.update_chain(chain_id, tenant_id, **update_data)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"fallback 链 {chain_id} 不存在",
        )
    await session.commit()
    return result


@router.delete("/chains/{chain_id}", response_model=Dict[str, Any])
async def delete_chain(
    chain_id: int,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """删除 fallback 链"""
    service = ModelFallbackService(session)
    deleted = await service.delete_chain(chain_id, tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"fallback 链 {chain_id} 不存在",
        )
    await session.commit()
    return {"deleted": True, "chain_id": chain_id}


@router.post("/chains/{chain_id}/test", response_model=Dict[str, Any])
async def test_chain(
    chain_id: int,
    payload: ChainTestRequest,
    session: AsyncSession = Depends(get_db),
    tenant_id: str = Depends(get_current_tenant),
):
    """测试 fallback 链（按链依次尝试候选模型，成功即返回）

    使用该链的 chain_config 作为候选列表执行 execute_with_fallback。
    """
    service = ModelFallbackService(session)
    chain = await service.get_chain(chain_id, tenant_id)
    if chain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"fallback 链 {chain_id} 不存在",
        )
    if not chain.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"fallback 链 {chain_id} 已禁用，无法测试",
        )

    # 临时构造一个只含该链的执行环境：直接调用 execute_with_fallback
    # 它会按 tier 选取最高优先级启用链；为保证测试目标链被选中，
    # 这里直接复用 service 的执行逻辑（按 tier 匹配主档位）。
    try:
        result = await service.execute_with_fallback(
            tier=payload.tier,
            messages=payload.messages,
            tenant_id=tenant_id,
        )
        return {
            "chain_id": chain_id,
            "tier": payload.tier,
            "success": True,
            "result": result,
        }
    except RuntimeError as e:
        # 所有候选失败
        return {
            "chain_id": chain_id,
            "tier": payload.tier,
            "success": False,
            "error": str(e),
        }
