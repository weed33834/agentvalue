"""API Key 管理 Admin API

路由前缀: /api/v1/admin/api-keys
权限: Role.ADMIN (router 级 dependencies)

完整功能 (7 端点):
- POST   /                       - 创建 API Key (返回明文仅一次)
- GET    /                       - 列表 (分页)
- GET    /{key_id}               - 详情
- PUT    /{key_id}               - 更新 (name/scopes/rate_limit)
- DELETE /{key_id}               - 吊销 (soft delete, is_active=False)
- POST   /{key_id}/rotate        - 轮换 (生成新 key, 旧 key 吊销)
- GET    /{key_id}/usage         - 用量统计 (从 audit_log 聚合)

安全说明:
- 明文 key 仅在创建/轮换时返回一次，之后永远不返回
- 库中仅存 sha256(key)，key_prefix 保存明文前 12 位供 UI 识别
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin._common import gen_id
from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models import ApiKey, AuditLog
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/api-keys",
    tags=["admin-api-keys"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# key_prefix 展示长度
_KEY_PREFIX_LEN = 12
# 明文随机部分长度（hex 字符数）
_KEY_RANDOM_HEX_LEN = 32


# ============================================================
# Schemas
# ============================================================


class ApiKeyCreate(BaseModel):
    """创建 API Key 请求"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="描述名称")
    scopes: Optional[List[str]] = Field(
        default=None, description="权限范围,如 ['chat','evaluation','insights']"
    )
    rate_limit: int = Field(default=60, ge=1, le=10000, description="每分钟请求限制")
    expires_at: Optional[datetime] = Field(
        default=None, description="过期时间 (ISO 8601),不传则永不过期"
    )


class ApiKeyUpdate(BaseModel):
    """更新 API Key 请求 (所有字段可选, key_id/key_hash 不可改)"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    scopes: Optional[List[str]] = None
    rate_limit: Optional[int] = Field(default=None, ge=1, le=10000)


class ApiKeyRotateResponse(BaseModel):
    """轮换响应 (返回新明文 key, 仅此一次)"""

    key_id: str
    plain_key: str
    key_prefix: str
    message: str


# ============================================================
# 工具函数
# ============================================================


def _generate_plain_key() -> str:
    """生成明文 API Key: ak_<32位随机hex>"""
    random_hex = secrets.token_hex(_KEY_RANDOM_HEX_LEN // 2)
    return f"ak_{random_hex}"


def _hash_key(plain_key: str) -> str:
    """对明文 key 做 sha256 哈希"""
    return hashlib.sha256(plain_key.encode("utf-8")).hexdigest()


def _scopes_to_text(scopes: Optional[List[str]]) -> Optional[str]:
    """scopes 列表 → JSON 字符串 (存 Text 列)"""
    if scopes is None:
        return None
    return json.dumps(scopes, ensure_ascii=False)


def _scopes_from_text(scopes_text: Optional[str]) -> List[str]:
    """JSON 字符串 → scopes 列表 (None / 解析失败返回空列表)"""
    if not scopes_text:
        return []
    try:
        result = json.loads(scopes_text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _entity_to_dict(entity: ApiKey) -> Dict[str, Any]:
    """ApiKey entity → dict (不含 key_hash, 不含明文 key)"""
    return {
        "id": entity.id,
        "key_id": entity.key_id,
        "key_prefix": entity.key_prefix,
        "name": entity.name,
        "scopes": _scopes_from_text(entity.scopes),
        "rate_limit": entity.rate_limit,
        "tenant_id": entity.tenant_id,
        "created_by": entity.created_by,
        "is_active": entity.is_active,
        "last_used_at": (
            entity.last_used_at.isoformat() if entity.last_used_at else None
        ),
        "expires_at": entity.expires_at.isoformat() if entity.expires_at else None,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "revoked_at": entity.revoked_at.isoformat() if entity.revoked_at else None,
    }


async def _get_api_key_entity(session: AsyncSession, key_id: str) -> Optional[ApiKey]:
    """按 key_id 查询 (当前租户隔离)"""
    result = await session.execute(
        select(ApiKey).where(
            ApiKey.key_id == key_id,
            ApiKey.tenant_id == get_current_tenant(),
        )
    )
    return result.scalar_one_or_none()


# ============================================================
# 路由
# ============================================================


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """创建 API Key

    生成明文 key (ak_<32位hex>)，返回明文仅此一次。
    库中存储 sha256(key) + 明文前 12 位前缀。
    """
    plain_key = _generate_plain_key()
    key_hash = _hash_key(plain_key)
    key_prefix = plain_key[:_KEY_PREFIX_LEN]

    entity = ApiKey(
        key_id=gen_id(prefix="ak"),
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=payload.name,
        scopes=_scopes_to_text(payload.scopes),
        rate_limit=payload.rate_limit,
        tenant_id=get_current_tenant(),
        created_by=current_user_id,
        is_active=True,
        expires_at=payload.expires_at,
    )
    session.add(entity)
    await session.flush()

    await audit_service.log(
        actor_id=current_user_id,
        action="create_api_key",
        details={
            "key_id": entity.key_id,
            "name": payload.name,
            "key_prefix": key_prefix,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    result = _entity_to_dict(entity)
    # 明文 key 仅此一次返回
    result["plain_key"] = plain_key
    result["message"] = "请妥善保存明文 key，此后将无法再次查看"
    return result


@router.get("", response_model=Dict[str, Any])
async def list_api_keys(
    request: Request,
    page: int = Query(1, ge=1, description="页码, 从 1 开始"),
    page_size: int = Query(20, ge=1, le=500, description="每页条数"),
    is_active: Optional[bool] = Query(None, description="按状态过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出 API Key (分页, 当前租户隔离)"""
    base = (
        select(ApiKey)
        .where(ApiKey.tenant_id == get_current_tenant())
        .order_by(ApiKey.created_at.desc())
    )
    if is_active is not None:
        base = base.where(ApiKey.is_active == is_active)

    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    offset = (page - 1) * page_size
    rows = (await session.execute(base.offset(offset).limit(page_size))).scalars().all()
    return {
        "items": [_entity_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{key_id}", response_model=Dict[str, Any])
async def get_api_key(
    key_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取 API Key 详情 (不含明文 key)"""
    entity = await _get_api_key_entity(session, key_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API Key {key_id} 不存在",
        )
    return _entity_to_dict(entity)


@router.put("/{key_id}", response_model=Dict[str, Any])
async def update_api_key(
    key_id: str,
    payload: ApiKeyUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新 API Key (name / scopes / rate_limit, key 本身不可改)"""
    entity = await _get_api_key_entity(session, key_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API Key {key_id} 不存在",
        )

    changed: Dict[str, Any] = {}
    if payload.name is not None and payload.name != entity.name:
        entity.name = payload.name
        changed["name"] = payload.name
    if payload.scopes is not None:
        new_scopes = _scopes_to_text(payload.scopes)
        if new_scopes != entity.scopes:
            entity.scopes = new_scopes
            changed["scopes"] = payload.scopes
    if payload.rate_limit is not None and payload.rate_limit != entity.rate_limit:
        entity.rate_limit = payload.rate_limit
        changed["rate_limit"] = payload.rate_limit

    if not changed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="update_api_key",
        details={"key_id": key_id, "changed": changed},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(entity)
    return _entity_to_dict(entity)


@router.delete("/{key_id}", response_model=Dict[str, Any])
async def revoke_api_key(
    key_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """吊销 API Key (soft delete: is_active=False, revoked_at=now)

    吊销后该 key 立即失效,无法再用于鉴权,但记录保留可查。
    """
    entity = await _get_api_key_entity(session, key_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API Key {key_id} 不存在",
        )
    if not entity.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"API Key {key_id} 已处于吊销状态",
        )

    entity.is_active = False
    entity.revoked_at = datetime.now(timezone.utc)

    await audit_service.log(
        actor_id=current_user_id,
        action="revoke_api_key",
        details={"key_id": key_id, "name": entity.name},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    # 立即清除缓存, 使吊销的 key 即刻失效
    from api.middleware import invalidate_apikey_cache

    invalidate_apikey_cache(entity.key_hash)

    return {"revoked": True, "key_id": key_id}


@router.post("/{key_id}/rotate", response_model=ApiKeyRotateResponse)
async def rotate_api_key(
    key_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """轮换 API Key

    生成全新明文 key,旧 key 立即吊销 (is_active=False)。
    原 key_id 的 name / scopes / rate_limit 等配置继承到新记录。
    返回新明文 key,仅此一次。
    """
    old_entity = await _get_api_key_entity(session, key_id)
    if old_entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API Key {key_id} 不存在",
        )

    # 生成新 key
    plain_key = _generate_plain_key()
    key_hash = _hash_key(plain_key)
    key_prefix = plain_key[:_KEY_PREFIX_LEN]

    # 吊销旧 key
    old_entity.is_active = False
    old_entity.revoked_at = datetime.now(timezone.utc)

    # 创建新 key (继承旧 key 的 name / scopes / rate_limit)
    new_entity = ApiKey(
        key_id=gen_id(prefix="ak"),
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=old_entity.name,
        scopes=old_entity.scopes,
        rate_limit=old_entity.rate_limit,
        tenant_id=old_entity.tenant_id,
        created_by=current_user_id,
        is_active=True,
        expires_at=old_entity.expires_at,
    )
    session.add(new_entity)
    await session.flush()

    await audit_service.log(
        actor_id=current_user_id,
        action="rotate_api_key",
        details={
            "old_key_id": key_id,
            "new_key_id": new_entity.key_id,
            "key_prefix": key_prefix,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    # 立即清除缓存, 使旧 key 即刻失效
    from api.middleware import invalidate_apikey_cache

    invalidate_apikey_cache(old_entity.key_hash)

    return ApiKeyRotateResponse(
        key_id=new_entity.key_id,
        plain_key=plain_key,
        key_prefix=key_prefix,
        message="旧 key 已吊销,请妥善保存新明文 key,此后将无法再次查看",
    )


@router.get("/{key_id}/usage", response_model=Dict[str, Any])
async def get_api_key_usage(
    key_id: str,
    request: Request,
    days: int = Query(30, ge=1, le=365, description="统计最近 N 天的用量"),
    session: AsyncSession = Depends(get_db),
):
    """获取 API Key 用量统计

    从 audit_log 聚合该 key 关联的调用记录:
    - 总调用次数
    - 最近 N 天每日调用次数
    - 最近一次使用时间
    """
    entity = await _get_api_key_entity(session, key_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API Key {key_id} 不存在",
        )

    tenant_id = get_current_tenant()
    # audit_log 中 actor_id 存放 api key 的 key_id (中间件鉴权时注入)
    base = select(AuditLog).where(
        AuditLog.actor_id == key_id,
        AuditLog.tenant_id == tenant_id,
    )

    # 总次数
    total_count = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    # 最近 N 天每日次数 (按日期分组)
    from sqlalchemy import cast, Date

    since = datetime.now(timezone.utc).timestamp() - days * 86400
    since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
    daily_stmt = (
        select(
            cast(AuditLog.created_at, Date).label("day"),
            func.count(AuditLog.id).label("count"),
        )
        .where(
            AuditLog.actor_id == key_id,
            AuditLog.tenant_id == tenant_id,
            AuditLog.created_at >= since_dt,
        )
        .group_by(cast(AuditLog.created_at, Date))
        .order_by(cast(AuditLog.created_at, Date))
    )
    daily_rows = (await session.execute(daily_stmt)).all()
    daily: List[Dict[str, Any]] = [
        {"date": str(row.day), "count": row.count} for row in daily_rows
    ]

    return {
        "key_id": key_id,
        "name": entity.name,
        "total_calls": total_count,
        "daily": daily,
        "last_used_at": (
            entity.last_used_at.isoformat() if entity.last_used_at else None
        ),
        "is_active": entity.is_active,
    }
