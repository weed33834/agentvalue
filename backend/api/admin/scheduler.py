"""定时任务调度管理 API

路由前缀: /api/v1/admin/scheduler
权限: Role.ADMIN (router 级 dependencies)

完整功能 (6 端点):
- GET    /tasks                   - 列出所有定时任务
- POST   /tasks                   - 创建定时任务
- PUT    /tasks/{task_id}         - 更新定时任务
- DELETE /tasks/{task_id}         - 删除定时任务
- POST   /tasks/{task_id}/trigger - 手动触发任务
- GET    /tasks/{task_id}/history - 查询执行历史
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from auth.rbac import Role, require_role
from core.scheduler import get_scheduler

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/scheduler",
    tags=["admin-scheduler"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# Schemas
# ============================================================


class TaskCreate(BaseModel):
    """创建定时任务"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="任务名称")
    description: Optional[str] = Field(default=None, description="任务描述")
    cron_expression: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="cron 表达式（5 段: 分 时 日 月 周）",
    )
    task_type: str = Field(
        default="custom",
        description="任务类型: retention/sla/fairness/api_key/notification/custom",
    )
    config: Optional[Dict[str, Any]] = Field(default=None, description="JSON 配置")


class TaskUpdate(BaseModel):
    """更新定时任务（所有字段可选）"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    cron_expression: Optional[str] = Field(default=None, max_length=128)
    is_active: Optional[bool] = None


class TaskTriggerResponse(BaseModel):
    """手动触发结果"""

    task_id: str
    status: str
    duration_ms: Optional[int] = None
    error: Optional[str] = None


# ============================================================
# 路由
# ============================================================


def _get_scheduler():
    """获取全局调度器实例，未启动时返回 503"""
    scheduler = get_scheduler()
    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="调度器未启动，请检查应用是否正确初始化",
        )
    return scheduler


@router.get("/tasks", response_model=Dict[str, Any])
async def list_tasks(
    task_type: Optional[str] = Query(None, description="按任务类型过滤"),
):
    """列出所有定时任务"""
    scheduler = _get_scheduler()
    tasks = await scheduler.list_tasks()
    if task_type:
        tasks = [t for t in tasks if t.get("task_type") == task_type]
    return {"items": tasks, "total": len(tasks)}


@router.post(
    "/tasks",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def create_task(payload: TaskCreate):
    """创建定时任务

    cron_expression 为标准 5 段 cron（分 时 日 月 周）。
    task_type 决定执行逻辑：内置类型（retention/sla/fairness/api_key/notification）
    使用预注册的执行函数；custom 类型暂不支持自定义执行逻辑（需后续扩展）。
    """
    scheduler = _get_scheduler()

    # 校验 cron 表达式格式
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(payload.cron_expression)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"无效的 cron 表达式: {payload.cron_expression}",
        )

    # custom 类型暂不支持自定义执行函数
    if payload.task_type == "custom":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="custom 类型暂不支持自定义执行函数，请使用内置类型"
            "（retention/sla/fairness/api_key/notification）",
        )

    try:
        from core.scheduler import _TASK_FUNC_REGISTRY

        func = _TASK_FUNC_REGISTRY.get(payload.task_type)
        if func is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不支持的任务类型: {payload.task_type}",
            )

        task_id = await scheduler.add_task(
            name=payload.name,
            func=func,
            cron_expression=payload.cron_expression,
            task_type=payload.task_type,
            description=payload.description,
            config=payload.config,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # 返回创建后的任务详情
    tasks = await scheduler.list_tasks()
    task = next((t for t in tasks if t["task_id"] == task_id), None)
    return task or {"task_id": task_id, "name": payload.name}


@router.put("/tasks/{task_id}", response_model=Dict[str, Any])
async def update_task(task_id: str, payload: TaskUpdate):
    """更新定时任务（任意字段可选，task_id 不可改）"""
    scheduler = _get_scheduler()

    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="未提供任何更新字段",
        )

    # 校验 cron 表达式
    if payload.cron_expression is not None:
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(payload.cron_expression)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"无效的 cron 表达式: {payload.cron_expression}",
            )

    result = await scheduler.update_task(
        task_id=task_id,
        cron_expression=payload.cron_expression,
        is_active=payload.is_active,
        name=payload.name,
        description=payload.description,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务 {task_id!r} 不存在",
        )
    return result


@router.delete("/tasks/{task_id}", response_model=Dict[str, Any])
async def delete_task(task_id: str):
    """删除定时任务（从调度器移除并标记 is_active=False）"""
    scheduler = _get_scheduler()
    deleted = await scheduler.remove_task(task_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务 {task_id!r} 不存在",
        )
    return {"deleted": True, "task_id": task_id}


@router.post(
    "/tasks/{task_id}/trigger",
    response_model=Dict[str, Any],
)
async def trigger_task(task_id: str):
    """手动触发任务

    立即执行指定任务，不等待 cron 触发。返回执行结果摘要。
    """
    scheduler = _get_scheduler()
    try:
        result = await scheduler.trigger_task(task_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    return result


@router.get("/tasks/{task_id}/history", response_model=Dict[str, Any])
async def get_task_history(
    task_id: str,
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
):
    """查询任务执行历史"""
    scheduler = _get_scheduler()
    history = await scheduler.get_task_history(task_id, limit=limit)
    return {"items": history, "total": len(history)}
