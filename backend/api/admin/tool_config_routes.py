"""工具配置 Admin API

路由前缀: /api/v1/admin/tool-config
权限: Role.ADMIN

完整端点:
- GET  /timeouts             - 获取所有工具超时配置
- PUT  /{tool_name}/timeout  - 设置工具超时 (body: {"timeout": 30})
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_app_state, get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/tool-config",
    tags=["admin-tool-config"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class TimeoutUpdate(BaseModel):
    """设置工具超时请求"""

    timeout: int = Field(
        ..., ge=1, le=3600, description="超时秒数 (1-3600)"
    )


# ============================================================
# 路由
# ============================================================


def _get_tool_registry(request: Request):
    """从 app_state 获取全局 ToolRegistry 实例

    app_state 中可能没有直接暴露 ToolRegistry, 这里做兼容处理:
    优先从 app_state.tool_registry 获取, 若不存在则返回 None。
    """
    app_state = get_app_state(request)
    tool_registry = getattr(app_state, "tool_registry", None)
    return tool_registry


@router.get("/timeouts", response_model=Dict[str, Any])
async def get_tool_timeouts(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """获取所有工具超时配置

    返回每个已加载工具的超时时间。
    若 ToolRegistry 未初始化, 返回默认配置说明。
    """
    tool_registry = _get_tool_registry(request)
    if tool_registry is None:
        # ToolRegistry 未初始化, 返回默认配置说明
        return {
            "timeouts": {},
            "defaults": {
                "default_timeout": 60,
                "command_tool_timeout": 30,
                "command_keywords": [
                    "bash", "command", "shell", "exec", "terminal", "cmd"
                ],
            },
            "loaded": False,
            "message": "ToolRegistry 尚未初始化, 显示默认配置",
        }

    # 确保工具已加载
    if not tool_registry._loaded:
        try:
            await tool_registry.resolve()
        except Exception as e:
            logger.warning("加载工具列表失败: %s", e)
            return {
                "timeouts": {},
                "defaults": {
                    "default_timeout": 60,
                    "command_tool_timeout": 30,
                },
                "loaded": False,
                "message": f"工具加载失败: {e}",
            }

    timeouts = tool_registry.get_all_timeouts()
    return {
        "timeouts": timeouts,
        "defaults": {
            "default_timeout": 60,
            "command_tool_timeout": 30,
        },
        "loaded": True,
        "total_tools": len(timeouts),
    }


@router.put("/{tool_name}/timeout", response_model=Dict[str, Any])
async def set_tool_timeout(
    tool_name: str,
    payload: TimeoutUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """设置工具超时时间

    设置后立即生效 (下一次工具调用使用新超时)。
    """
    tool_registry = _get_tool_registry(request)
    if tool_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ToolRegistry 尚未初始化, 无法设置超时",
        )

    try:
        tool_registry.set_tool_timeout(tool_name, payload.timeout)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )

    await audit_service.log(
        actor_id=current_user_id,
        action="set_tool_timeout",
        details={"tool_name": tool_name, "timeout": payload.timeout},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    return {
        "tool_name": tool_name,
        "timeout": payload.timeout,
        "message": f"工具 {tool_name} 超时已设置为 {payload.timeout} 秒",
    }
