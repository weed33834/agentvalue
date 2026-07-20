"""自定义工具 Admin API (P3-1: 自定义工具上传 - OpenAPI Schema 导入)

对标 Dify Custom Tool: 用户粘贴 OpenAPI JSON/YAML → 解析 paths → 每个 operation 生成一个 LangChain Tool。

路由前缀: /api/v1/admin/custom-tools
权限: Role.ADMIN (router 级 dependencies)
凭证加密: FieldCipher 加密 auth_credentials (存储时加密,使用时解密)

完整功能 (8 端点):
- GET    /                  - 列表 (支持 search + tenant_id 过滤)
- POST   /                  - 创建 (解析 OpenAPI + 存储,返回 ToolSpec 预览)
- GET    /{tool_id}         - 详情
- PUT    /{tool_id}         - 更新 (重新解析 OpenAPI)
- DELETE /{tool_id}         - 删除
- POST   /{tool_id}/toggle  - 启用/禁用
- POST   /{tool_id}/test    - 测试 (实际调 HTTP endpoint 返回响应)
- POST   /parse             - 仅解析 OpenAPI (不入库,返回 ToolSpec 预览)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.field_crypto import get_field_cipher
from core.rate_limit import rate_limit
from core.tenant_context import get_current_tenant
from core.tools.openapi_parser import (
    AuthConfig,
    ToolSpec,
    parse_openapi_string,
)
from models.custom_tool import CustomTool

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/custom-tools",
    tags=["admin-custom-tools"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class CustomToolCreate(BaseModel):
    """创建自定义工具请求"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="工具名 (租户内唯一)")
    description: str = Field(default="", max_length=512, description="工具描述")
    openapi_schema: Dict[str, Any] = Field(
        ..., description="OpenAPI 3.x spec (已 parse 为 dict)"
    )
    base_url: str = Field(min_length=1, max_length=512, description="API base URL")
    auth_type: str = Field(
        default="none",
        description="鉴权类型: none / bearer / api_key / basic",
    )
    auth_credentials: Optional[str] = Field(
        default=None, max_length=512, description="鉴权凭证 (存储时加密)"
    )
    tenant_id: Optional[str] = Field(
        default=None, max_length=64, description="租户 ID (None 时用当前租户上下文)"
    )


class CustomToolUpdate(BaseModel):
    """更新自定义工具请求 (所有字段可选)"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    openapi_schema: Optional[Dict[str, Any]] = None
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    auth_type: Optional[str] = None
    auth_credentials: Optional[str] = Field(default=None, max_length=512)


class CustomToolToggle(BaseModel):
    """启用/禁用"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., description="目标状态")


class CustomToolTestRequest(BaseModel):
    """测试自定义工具调用"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="OpenAPI path (如 /users/{id})")
    method: str = Field(..., description="HTTP 方法 (如 GET/POST)")
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="参数 (path/query/header/body 合并)"
    )


class ParseRequest(BaseModel):
    """解析 OpenAPI (不入库)"""

    model_config = ConfigDict(extra="forbid")

    openapi_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="OpenAPI spec (已 parse 为 dict)"
    )
    raw: Optional[str] = Field(
        default=None, description="OpenAPI spec 原文 (JSON 或 YAML 字符串)"
    )
    base_url: str = Field(default="", max_length=512, description="API base URL")


# ============================================================
# 工具函数
# ============================================================


def _gen_id() -> str:
    """生成主键 (uuid4 hex,32 字符)"""
    # review 优化: 复用 admin/_common.gen_id,统一 ID 生成逻辑
    from api.admin._common import gen_id
    return gen_id()


def _validate_auth_type(auth_type: str) -> str:
    """校验 auth_type 必须为 none/bearer/api_key/basic"""
    allowed = {"none", "bearer", "api_key", "basic"}
    if auth_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"auth_type 必须为 {allowed} 之一,实际: {auth_type}",
        )
    return auth_type


def _encrypt_credentials(credentials: Optional[str]) -> Optional[str]:
    """加密凭证 (FieldCipher),None 时返 None"""
    if not credentials:
        return None
    cipher = get_field_cipher()
    return cipher.encrypt(credentials)


def _decrypt_credentials(stored: Optional[str]) -> Optional[str]:
    """解密凭证 (FieldCipher),None 时返 None"""
    if not stored:
        return None
    cipher = get_field_cipher()
    return cipher.decrypt(stored)


def _spec_to_dict(tool_spec: ToolSpec) -> Dict[str, Any]:
    """ToolSpec → dict (供 API 返回)"""
    return {
        "name": tool_spec.name,
        "description": tool_spec.description,
        "method": tool_spec.method,
        "url": tool_spec.url,
        "path": tool_spec.path,
        "parameters": tool_spec.parameters,
        "operation_id": tool_spec.operation_id,
        "summary": tool_spec.summary,
    }


def _entity_to_dict(
    entity: CustomTool, include_tools: bool = False
) -> Dict[str, Any]:
    """CustomTool entity → dict (不含敏感凭证,可附带解析出的 tools)"""
    result: Dict[str, Any] = {
        "id": entity.id,
        "name": entity.name,
        "description": entity.description,
        "base_url": entity.base_url,
        "auth_type": entity.auth_type,
        "has_credentials": bool(entity.auth_credentials),
        "enabled": entity.enabled,
        "tenant_id": entity.tenant_id,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }
    if include_tools:
        try:
            specs = parse_openapi_to_tools_safe(
                entity.openapi_schema, entity.base_url
            )
            result["tools"] = [_spec_to_dict(s) for s in specs]
        except Exception as e:
            result["tools"] = []
            result["parse_error"] = str(e)
    return result


def parse_openapi_to_tools_safe(
    spec: Dict[str, Any], base_url: str
) -> List[ToolSpec]:
    """安全解析 (API 层调用,捕获 ValueError 转为 422)"""
    from core.tools.openapi_parser import parse_openapi_to_tools

    try:
        return parse_openapi_to_tools(spec, base_url)
    except ValueError:
        raise
    except Exception as e:
        # 兜底: 其他异常也视为 spec 无效
        raise ValueError(f"OpenAPI spec 解析失败: {e}") from e


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=Dict[str, Any])
async def list_custom_tools(
    request: Request,
    search: Optional[str] = Query(None, description="按 name/description 模糊搜索"),
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有自定义工具 (支持 search + tenant_id 过滤)"""
    stmt = select(CustomTool)
    if search:
        kw = f"%{search}%"
        stmt = stmt.where(
            or_(CustomTool.name.ilike(kw), CustomTool.description.ilike(kw))
        )
    if tenant_id:
        stmt = stmt.where(CustomTool.tenant_id == tenant_id)
    stmt = stmt.order_by(CustomTool.created_at.desc())

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return {
        "items": [_entity_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.post("/parse", response_model=Dict[str, Any])
@rate_limit("30/minute")
async def parse_openapi(
    payload: ParseRequest,
    request: Request,
):
    """仅解析 OpenAPI (不入库),返回 ToolSpec 预览

    支持:
    - openapi_schema: 已 parse 的 dict
    - raw: JSON 或 YAML 字符串 (有 raw 时优先)
    """
    base_url = payload.base_url or ""
    spec: Optional[Dict[str, Any]] = payload.openapi_schema
    if payload.raw:
        try:
            specs = parse_openapi_string(payload.raw, base_url)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"OpenAPI spec 解析失败: {e}",
            )
        return {
            "tools": [_spec_to_dict(s) for s in specs],
            "count": len(specs),
            "base_url": base_url,
        }
    if not spec:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="必须提供 openapi_schema (dict) 或 raw (字符串)",
        )
    try:
        specs = parse_openapi_to_tools_safe(spec, base_url)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OpenAPI spec 解析失败: {e}",
        )
    return {
        "tools": [_spec_to_dict(s) for s in specs],
        "count": len(specs),
        "base_url": base_url,
    }


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
@rate_limit("20/minute")
async def create_custom_tool(
    payload: CustomToolCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """创建自定义工具: 解析 OpenAPI → 存储 → 返回 ToolSpec 预览

    凭证用 FieldCipher 加密后存储。
    """
    # 校验 auth_type
    _validate_auth_type(payload.auth_type)

    # 解析 spec (容错: 无效 spec 返回 422)
    try:
        specs = parse_openapi_to_tools_safe(payload.openapi_schema, payload.base_url)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OpenAPI spec 解析失败: {e}",
        )

    # 租户 ID: 优先用 payload.tenant_id,其次当前上下文
    tenant = payload.tenant_id or get_current_tenant()

    # 唯一性校验 (tenant_id + name)
    existing = await session.execute(
        select(CustomTool).where(
            CustomTool.tenant_id == tenant, CustomTool.name == payload.name
        )
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"工具名 {payload.name} 在租户 {tenant} 下已存在",
        )

    entity = CustomTool(
        id=_gen_id(),
        name=payload.name,
        description=payload.description,
        openapi_schema=payload.openapi_schema,
        base_url=payload.base_url,
        auth_type=payload.auth_type,
        auth_credentials=_encrypt_credentials(payload.auth_credentials),
        enabled=True,
        tenant_id=tenant,
    )
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    result = _entity_to_dict(entity, include_tools=True)
    result["tools"] = [_spec_to_dict(s) for s in specs]
    return result


@router.get("/{tool_id}", response_model=Dict[str, Any])
async def get_custom_tool(
    tool_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取自定义工具详情"""
    entity = await session.get(CustomTool, tool_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"自定义工具 {tool_id} 不存在",
        )
    return _entity_to_dict(entity, include_tools=True)


@router.put("/{tool_id}", response_model=Dict[str, Any])
async def update_custom_tool(
    tool_id: str,
    payload: CustomToolUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """更新自定义工具 (重新解析 OpenAPI,凭证可选更新)"""
    entity = await session.get(CustomTool, tool_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"自定义工具 {tool_id} 不存在",
        )

    # 更新字段
    if payload.name is not None:
        # 检查重名 (排除自身)
        existing = await session.execute(
            select(CustomTool).where(
                CustomTool.tenant_id == entity.tenant_id,
                CustomTool.name == payload.name,
                CustomTool.id != entity.id,
            )
        )
        if existing.scalars().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"工具名 {payload.name} 已存在",
            )
        entity.name = payload.name
    if payload.description is not None:
        entity.description = payload.description
    if payload.openapi_schema is not None:
        # 更新 spec 前先校验解析通过 (避免存入无效 spec)
        try:
            parse_openapi_to_tools_safe(
                payload.openapi_schema, entity.base_url
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"OpenAPI spec 解析失败: {e}",
            )
        entity.openapi_schema = payload.openapi_schema
    if payload.base_url is not None:
        entity.base_url = payload.base_url
    if payload.auth_type is not None:
        _validate_auth_type(payload.auth_type)
        entity.auth_type = payload.auth_type
    if payload.auth_credentials is not None:
        # None 表示清空,空字符串视为清空,非空才加密
        entity.auth_credentials = _encrypt_credentials(payload.auth_credentials) or None

    await session.commit()
    await session.refresh(entity)
    return _entity_to_dict(entity, include_tools=True)


@router.delete("/{tool_id}", response_model=Dict[str, Any])
async def delete_custom_tool(
    tool_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """删除自定义工具"""
    entity = await session.get(CustomTool, tool_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"自定义工具 {tool_id} 不存在",
        )
    name = entity.name
    await session.delete(entity)
    await session.commit()
    return {"deleted": True, "id": tool_id, "name": name}


@router.post("/{tool_id}/toggle", response_model=Dict[str, Any])
async def toggle_custom_tool(
    tool_id: str,
    payload: CustomToolToggle,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """启用/禁用自定义工具 (禁用的工具不会加载到 ReAct Agent)"""
    entity = await session.get(CustomTool, tool_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"自定义工具 {tool_id} 不存在",
        )
    entity.enabled = payload.enabled
    await session.commit()
    await session.refresh(entity)
    return {
        "id": entity.id,
        "name": entity.name,
        "enabled": entity.enabled,
    }


@router.post("/{tool_id}/test", response_model=Dict[str, Any])
@rate_limit("30/minute")
async def test_custom_tool(
    tool_id: str,
    payload: CustomToolTestRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """测试自定义工具调用 (实际调 HTTP endpoint 返回响应)

    输入: path + method + parameters (合并 path/query/header/body)
    流程: 找到对应 ToolSpec → 用 AuthConfig 解密凭证 → httpx 调用 → 返回响应
    """
    entity = await session.get(CustomTool, tool_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"自定义工具 {tool_id} 不存在",
        )

    try:
        specs = parse_openapi_to_tools_safe(
            entity.openapi_schema, entity.base_url
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OpenAPI spec 解析失败: {e}",
        )

    # 找到匹配的 ToolSpec (path + method 不区分大小写)
    target: Optional[ToolSpec] = None
    method_upper = payload.method.upper()
    for s in specs:
        if s.path == payload.path and s.method.upper() == method_upper:
            target = s
            break
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"在工具 {entity.name} 中未找到 path={payload.path} method={method_upper} 的 operation",
        )

    # 解密凭证
    auth = AuthConfig(
        auth_type=entity.auth_type,
        credentials=_decrypt_credentials(entity.auth_credentials),
    )

    # 用 build_langchain_tool 包装 + invoke (复用 httpx 调用逻辑)
    from core.tools.openapi_parser import build_langchain_tool

    tool = build_langchain_tool(target, auth=auth)
    try:
        # 测试端点用同步 invoke (httpx.Client),便于在 TestClient 中 mock 验证
        # ReAct Agent 中通过 ainvoke 走异步路径
        result = tool.invoke(payload.parameters)
    except Exception as e:
        logger.warning("测试自定义工具 %s 失败: %s", entity.name, e)
        return {
            "tool_id": tool_id,
            "tool_name": entity.name,
            "path": payload.path,
            "method": method_upper,
            "parameters": payload.parameters,
            "error": str(e),
            "success": False,
        }

    return {
        "tool_id": tool_id,
        "tool_name": entity.name,
        "path": payload.path,
        "method": method_upper,
        "parameters": payload.parameters,
        "result": result,
        "success": True,
    }
