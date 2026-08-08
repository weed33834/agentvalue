"""审计日志完整性 Admin API（WS-4 防篡改哈希链）

路由前缀: /api/v1/admin/audit-logs
权限: Role.ADMIN (router 级 dependencies)

端点:
- GET /verify-chain  - 校验当前租户审计哈希链完整性，定位第一处断点
- GET /chain-head    - 获取当前链尾哈希（供外部锚定：WORM 存储 / 时间戳服务）

租户作用域从 get_current_tenant() 获取，不接受路径/查询参数指定租户，
防止管理员横向查看其他租户的审计链状态。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/audit-logs",
    tags=["admin-audit-integrity"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class ChainVerifyResponse(BaseModel):
    """哈希链校验结果"""

    model_config = ConfigDict(extra="forbid")

    valid: bool = Field(description="链是否完整")
    checked: int = Field(description="实际校验的条目数")
    tenant_id: str = Field(description="校验的租户")
    broken_entry_id: Optional[int] = Field(
        default=None, description="第一处断链的审计条目主键（valid=true 时为空）"
    )
    broken_log_id: Optional[str] = Field(
        default=None, description="第一处断链的业务 log_id"
    )
    reason: Optional[str] = Field(default=None, description="断链原因描述")
    unchained: int = Field(
        default=0, description="未入链的条目数（迁移前历史数据 / 链计算降级条目）"
    )
    head_hash: Optional[str] = Field(
        default=None, description="校验区间内的链尾哈希"
    )


class ChainHeadResponse(BaseModel):
    """当前链尾"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(description="租户")
    head_hash: str = Field(description="链尾 entry_hash")
    is_genesis: bool = Field(description="是否尚无任何入链条目")


# ============================================================
# 路由
# ============================================================


@router.get("/verify-chain", response_model=ChainVerifyResponse)
async def verify_audit_chain(
    start: Optional[int] = Query(
        default=None, ge=1, description="起始审计条目 id（含），缺省从最早一条开始"
    ),
    end: Optional[int] = Query(
        default=None, ge=1, description="结束审计条目 id（含），缺省到最新一条"
    ),
    session: AsyncSession = Depends(get_db),
) -> ChainVerifyResponse:
    """校验当前租户审计日志哈希链完整性。

    校验逻辑见 `AuditService.verify_chain`：逐条重算
    `sha256(canonical_json(字段) + prev_hash)`，任一条内容被改写或链接断裂
    都会被定位到具体条目。

    大表提示：全量校验是 O(条目数) 的顺序扫描，百万级审计表建议用 `start` /
    `end` 分段校验（例如按天/按 id 区间跑定时任务），避免单次请求超时。
    """
    tenant_id = get_current_tenant()
    service = AuditService(session)
    result = await service.verify_chain(tenant_id=tenant_id, start=start, end=end)
    if not result.valid:
        # 审计链断裂属于安全事件，必须在服务端留痕，不能只返回给调用方
        logger.error(
            "审计哈希链校验失败 tenant_id=%s broken_entry_id=%s reason=%s",
            tenant_id,
            result.broken_entry_id,
            result.reason,
        )
    return ChainVerifyResponse(**result.to_dict())


@router.get("/chain-head", response_model=ChainHeadResponse)
async def get_audit_chain_head(
    session: AsyncSession = Depends(get_db),
) -> ChainHeadResponse:
    """获取当前租户审计链尾哈希。

    用途：定期把链尾锚定到不可变的外部介质（对象存储 WORM 桶、RFC3161 时间戳、
    公证服务），这样即便攻击者拿到 DB superuser 重算了整条链，也无法伪造历史锚点。
    """
    tenant_id = get_current_tenant()
    service = AuditService(session)
    head = await service.get_chain_head(tenant_id)
    return ChainHeadResponse(**head)
