"""
基于 APScheduler 的定时任务调度器

封装默认定时任务（数据留存清理、SLA 监控、公平性审计、API Key 过期检查、通知清理），
并支持动态增删改查与手动触发。任务配置持久化到 scheduled_tasks 表，
每次执行记录写入 scheduled_task_runs 表。

后台任务使用 AsyncSessionLocal 独立获取数据库会话，不依赖请求级 session。
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select

from core.config import get_settings
from core.database import AsyncSessionLocal
from core.tenant_context import set_current_tenant
from models.models import (
    DEFAULT_TENANT_ID,
    ApiKey,
    Notification,
    ScheduledTask,
    ScheduledTaskRun,
)

logger = logging.getLogger(__name__)

# APScheduler 可选依赖标志，缺失时降级为空操作（不影响应用启动）
try:
    _APSCHEDULER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _APSCHEDULER_AVAILABLE = False


# 默认任务定义：task_id -> (name, description, cron, task_type, func_factory)
# func_factory 接收 TaskScheduler 实例，返回 async callable
_DEFAULT_TASKS: List[Dict[str, Any]] = [
    {
        "task_id": "retention_cleanup",
        "name": "数据留存清理",
        "description": "按 GDPR/个保法要求自动归档与清理过期数据（原始输入 2 年、评估 5 年）",
        "cron_expression": "0 3 * * *",  # 每天凌晨 3 点
        "task_type": "retention",
    },
    {
        "task_id": "sla_monitor",
        "name": "SLA 监控",
        "description": "监控申诉处理时效，对照 72 小时响应 SLA 输出达成率与超时清单",
        "cron_expression": "*/30 * * * *",  # 每 30 分钟
        "task_type": "sla",
    },
    {
        "task_id": "fairness_audit_monthly",
        "name": "月度公平性审计",
        "description": "按部门/职级/性别/办公地分组统计评估公平性，输出组间差异与风险告警",
        "cron_expression": "0 2 1 * *",  # 每月 1 号凌晨 2 点
        "task_type": "fairness",
    },
    {
        "task_id": "api_key_expiry_check",
        "name": "API Key 过期检查",
        "description": "检查即将过期或已过期的 API Key，自动禁用过期 Key 并记录告警",
        "cron_expression": "0 4 * * *",  # 每天凌晨 4 点
        "task_type": "api_key",
    },
    {
        "task_id": "notification_cleanup",
        "name": "通知清理",
        "description": "清理 30 天前已读通知，避免通知表无限膨胀",
        "cron_expression": "0 5 * * 0",  # 每周日凌晨 5 点
        "task_type": "notification",
    },
]


# ============================================================
# 默认任务实现（封装现有脚本逻辑为可调用的 async 函数）
# ============================================================


async def run_retention_cleanup(tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    """数据留存清理任务

    复用 scripts.data_retention.RetentionPolicy 的完整流程：扫描 → 归档 → 清理。
    返回执行摘要 dict。
    """
    set_current_tenant(tenant_id)
    from scripts.data_retention import RetentionPolicy

    async with AsyncSessionLocal() as session:
        policy = RetentionPolicy(session)
        summary = await policy.run_retention_job()
        await session.commit()
    logger.info(
        "[retention_cleanup] 扫描过期 %s 条，归档 %s 条，清理 %s",
        summary["scanned_expired"],
        summary["archived"],
        summary["purged"],
    )
    return summary


async def run_sla_monitor(tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    """SLA 监控任务

    复用 scripts.sla_monitor.generate_sla_report 生成 SLA 报告。
    当前使用造数函数生成模拟申诉数据（与 CLI 脚本一致），
    后续可接入真实申诉数据源。
    """
    set_current_tenant(tenant_id)
    from scripts.sla_monitor import generate_sla_report

    report = generate_sla_report()
    summary = report.get("summary", {})
    logger.info(
        "[sla_monitor] 申诉总数 %s，达成 %s，超时 %s，达成率 %s%%",
        report.get("total_appeals"),
        summary.get("met"),
        summary.get("breached"),
        summary.get("achievement_rate"),
    )
    return {
        "total_appeals": report.get("total_appeals"),
        "achievement_rate": summary.get("achievement_rate"),
        "breached": summary.get("breached"),
    }


async def run_fairness_audit(tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    """月度公平性审计任务

    复用 scripts.run_fairness_monthly.generate_monthly_report 生成公平性审计月报。
    当前使用造数函数生成模拟评估数据（与 CLI 脚本一致），
    后续可从 evaluations 表读取真实数据。
    """
    set_current_tenant(tenant_id)
    from scripts.run_fairness_monthly import generate_monthly_report

    report = generate_monthly_report()
    by_dim = report.get("by_dimension", {})
    risk_dims = [
        dim for dim, stat in by_dim.items() if stat.get("has_risk")
    ]
    logger.info(
        "[fairness_audit] 评估样本 %s，风险维度 %s",
        report.get("total_evaluations"),
        risk_dims or "无",
    )
    return {
        "total_evaluations": report.get("total_evaluations"),
        "risk_dimensions": risk_dims,
        "overall_mean": report.get("overall", {}).get("mean"),
    }


async def run_api_key_expiry_check(
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Dict[str, Any]:
    """API Key 过期检查任务

    扫描 api_keys 表中已过期（expires_at < now）但仍处于 active 状态的 Key，
    自动将其禁用（is_active=False, revoked_at=now），返回禁用条数。
    同时识别 7 天内即将过期的 Key 并记录告警。
    """
    set_current_tenant(tenant_id)
    now = datetime.now(timezone.utc)
    warning_threshold = now + timedelta(days=7)

    async with AsyncSessionLocal() as session:
        # 查询已过期但仍 active 的 Key
        expired_keys = (
            (
                await session.execute(
                    select(ApiKey).where(
                        ApiKey.tenant_id == tenant_id,
                        ApiKey.is_active.is_(True),
                        ApiKey.expires_at.is_not(None),
                        ApiKey.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        expired_count = 0
        for key in expired_keys:
            key.is_active = False
            key.revoked_at = now
            expired_count += 1
            logger.warning(
                "[api_key_expiry] API Key %s (%s) 已过期，自动禁用",
                key.key_id,
                key.name,
            )

        # 查询 7 天内即将过期的 Key
        upcoming = (
            (
                await session.execute(
                    select(ApiKey).where(
                        ApiKey.tenant_id == tenant_id,
                        ApiKey.is_active.is_(True),
                        ApiKey.expires_at.is_not(None),
                        ApiKey.expires_at >= now,
                        ApiKey.expires_at < warning_threshold,
                    )
                )
            )
            .scalars()
            .all()
        )
        for key in upcoming:
            logger.warning(
                "[api_key_expiry] API Key %s (%s) 将在 %s 过期",
                key.key_id,
                key.name,
                key.expires_at.isoformat() if key.expires_at else "未知",
            )

        await session.commit()

    logger.info(
        "[api_key_expiry] 禁用已过期 Key %s 个，即将过期 %s 个",
        expired_count,
        len(upcoming),
    )
    return {
        "disabled_expired": expired_count,
        "upcoming_expiry": len(upcoming),
    }


async def run_notification_cleanup(
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Dict[str, Any]:
    """通知清理任务

    删除 30 天前已读通知（is_read=True 且 read_at < now - 30d），
    避免通知表无限膨胀。返回删除条数。
    """
    set_current_tenant(tenant_id)
    threshold = datetime.now(timezone.utc) - timedelta(days=30)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(Notification).where(
                Notification.tenant_id == tenant_id,
                Notification.is_read.is_(True),
                Notification.read_at.is_not(None),
                Notification.read_at < threshold,
            )
        )
        deleted = result.rowcount or 0
        await session.commit()

    logger.info("[notification_cleanup] 清理 30 天前已读通知 %s 条", deleted)
    return {"deleted": deleted}


# task_type -> async callable 的映射
_TASK_FUNC_REGISTRY: Dict[str, Callable] = {
    "retention": run_retention_cleanup,
    "sla": run_sla_monitor,
    "fairness": run_fairness_audit,
    "api_key": run_api_key_expiry_check,
    "notification": run_notification_cleanup,
}


class TaskScheduler:
    """基于 APScheduler 的定时任务调度器"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._tasks: Dict[str, Any] = {}  # task_id -> job

    async def start(self):
        """启动调度器"""
        self.scheduler.start()
        await self._register_default_tasks()
        logger.info("TaskScheduler 已启动，注册 %s 个默认任务", len(self._tasks))

    async def stop(self):
        """停止调度器"""
        self.scheduler.shutdown(wait=False)
        logger.info("TaskScheduler 已停止")

    async def _register_default_tasks(self):
        """注册默认定时任务

        将 _DEFAULT_TASKS 中定义的任务注册到 APScheduler，
        并持久化到 scheduled_tasks 表（若不存在则创建）。
        """
        for task_def in _DEFAULT_TASKS:
            task_id = task_def["task_id"]
            func = _TASK_FUNC_REGISTRY.get(task_def["task_type"])
            if func is None:
                logger.warning("未找到任务类型 %s 的执行函数，跳过", task_def["task_type"])
                continue

            # 持久化到 DB（已存在则跳过）
            await self._ensure_task_in_db(task_def)

            try:
                job = self.scheduler.add_job(
                    self._wrap_task(task_id, func),
                    trigger=CronTrigger.from_crontab(task_def["cron_expression"]),
                    id=task_id,
                    name=task_def["name"],
                    replace_existing=True,
                )
                self._tasks[task_id] = job
            except Exception:
                logger.error("注册默认任务 %s 失败", task_id, exc_info=True)

    async def _ensure_task_in_db(self, task_def: Dict[str, Any]) -> None:
        """确保任务记录存在于 scheduled_tasks 表（不存在则创建）"""
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.task_id == task_def["task_id"]
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                task = ScheduledTask(
                    task_id=task_def["task_id"],
                    name=task_def["name"],
                    description=task_def.get("description"),
                    cron_expression=task_def["cron_expression"],
                    task_type=task_def["task_type"],
                    config=json.dumps(task_def.get("config", {}), ensure_ascii=False),
                    is_active=True,
                    tenant_id=DEFAULT_TENANT_ID,
                )
                session.add(task)
                await session.commit()

    def _wrap_task(self, task_id: str, func: Callable) -> Callable:
        """包装任务函数：记录执行历史 + 更新 last_run 状态"""

        async def _wrapped():
            await self._execute_task(task_id, func, triggered_by="scheduler")

        return _wrapped

    async def _execute_task(
        self,
        task_id: str,
        func: Callable,
        triggered_by: str = "scheduler",
    ) -> Dict[str, Any]:
        """执行单个任务并记录结果

        无论成功或失败都会写入 scheduled_task_runs 表，
        并更新 scheduled_tasks 的 last_run_at / last_run_status / last_run_error。
        """
        started_at = datetime.now(timezone.utc)
        status_str = "success"
        error_msg: Optional[str] = None
        result_data: Optional[str] = None

        try:
            result = await func()
            result_data = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            status_str = "failed"
            error_msg = str(e)
            logger.error("任务 %s 执行失败", task_id, exc_info=True)

        finished_at = datetime.now(timezone.utc)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

        # 记录执行历史 + 更新 last_run
        async with AsyncSessionLocal() as session:
            # 更新 ScheduledTask
            task = (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if task is not None:
                task.last_run_at = finished_at
                task.last_run_status = status_str
                task.last_run_error = error_msg

            # 记录 ScheduledTaskRun
            run = ScheduledTaskRun(
                task_id=task_id,
                status=status_str,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                error=error_msg,
                result=result_data,
                triggered_by=triggered_by,
                tenant_id=DEFAULT_TENANT_ID,
            )
            session.add(run)
            await session.commit()

        return {
            "task_id": task_id,
            "status": status_str,
            "duration_ms": duration_ms,
            "error": error_msg,
            "result": result,
        } if status_str == "success" else {
            "task_id": task_id,
            "status": status_str,
            "duration_ms": duration_ms,
            "error": error_msg,
        }

    async def add_task(
        self,
        name: str,
        func: Callable,
        cron_expression: str,
        task_type: str = "custom",
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """添加定时任务

        将任务注册到 APScheduler 并持久化到 scheduled_tasks 表。
        返回 task_id。
        """
        task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"

        # 持久化到 DB
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"任务 {task_id} 已存在")

            task = ScheduledTask(
                task_id=task_id,
                name=name,
                description=description,
                cron_expression=cron_expression,
                task_type=task_type,
                config=json.dumps(config or {}, ensure_ascii=False),
                is_active=True,
                tenant_id=DEFAULT_TENANT_ID,
            )
            session.add(task)
            await session.commit()

        # 注册到调度器
        job = self.scheduler.add_job(
            self._wrap_task(task_id, func),
            trigger=CronTrigger.from_crontab(cron_expression),
            id=task_id,
            name=name,
            replace_existing=True,
        )
        self._tasks[task_id] = job
        logger.info("已添加定时任务 %s (%s)", task_id, name)
        return task_id

    async def remove_task(self, task_id: str) -> bool:
        """移除定时任务

        从 APScheduler 移除并在 DB 中标记 is_active=False。
        返回是否成功移除。
        """
        if task_id in self._tasks:
            try:
                self.scheduler.remove_job(task_id)
            except Exception:
                logger.warning("从 APScheduler 移除任务 %s 失败", task_id, exc_info=True)
            del self._tasks[task_id]

        async with AsyncSessionLocal() as session:
            task = (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if task is None:
                return False
            task.is_active = False
            await session.commit()

        logger.info("已移除定时任务 %s", task_id)
        return True

    async def list_tasks(self) -> List[Dict[str, Any]]:
        """列出所有任务

        从 DB 读取任务配置，并合并 APScheduler 的下次执行时间。
        """
        async with AsyncSessionLocal() as session:
            tasks = (
                (
                    await session.execute(
                        select(ScheduledTask).order_by(ScheduledTask.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )

        result = []
        for t in tasks:
            next_run: Optional[str] = None
            job = self._tasks.get(t.task_id)
            if job is not None:
                try:
                    next = job.next_run_time
                    if next is not None:
                        next_run = next.isoformat()
                except Exception:
                    pass
            result.append(
                {
                    "task_id": t.task_id,
                    "name": t.name,
                    "description": t.description,
                    "cron_expression": t.cron_expression,
                    "task_type": t.task_type,
                    "config": t.config,
                    "is_active": t.is_active,
                    "last_run_at": t.last_run_at.isoformat()
                    if t.last_run_at
                    else None,
                    "last_run_status": t.last_run_status,
                    "last_run_error": t.last_run_error,
                    "next_run_at": next_run,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
            )
        return result

    async def trigger_task(self, task_id: str) -> Dict[str, Any]:
        """手动触发任务

        立即执行指定任务（不等待 cron 触发），返回执行结果。
        """
        # 先查 DB 确认任务存在
        async with AsyncSessionLocal() as session:
            task = (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if task is None:
                raise ValueError(f"任务 {task_id} 不存在")

        # 查找对应的执行函数
        func = _TASK_FUNC_REGISTRY.get(task.task_type)
        if func is None:
            raise ValueError(
                f"任务类型 {task.task_type} 无注册的执行函数，无法手动触发"
            )

        return await self._execute_task(task_id, func, triggered_by="manual")

    async def update_task(
        self,
        task_id: str,
        cron_expression: Optional[str] = None,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """更新任务配置

        更新 DB 中的任务配置，并在 APScheduler 中重建 job（若 cron 变更）。
        """
        async with AsyncSessionLocal() as session:
            task = (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.task_id == task_id)
                )
            ).scalar_one_or_none()
            if task is None:
                return None

            if cron_expression is not None:
                task.cron_expression = cron_expression
            if is_active is not None:
                task.is_active = is_active
            if name is not None:
                task.name = name
            if description is not None:
                task.description = description
            await session.commit()

        # 同步到 APScheduler
        func = _TASK_FUNC_REGISTRY.get(task.task_type)
        if func is not None and task.is_active:
            try:
                self.scheduler.add_job(
                    self._wrap_task(task_id, func),
                    trigger=CronTrigger.from_crontab(task.cron_expression),
                    id=task_id,
                    name=task.name,
                    replace_existing=True,
                )
                self._tasks[task_id] = self.scheduler.get_job(task_id)
            except Exception:
                logger.error("更新调度器任务 %s 失败", task_id, exc_info=True)
        elif not task.is_active and task_id in self._tasks:
            try:
                self.scheduler.remove_job(task_id)
            except Exception:
                pass
            del self._tasks[task_id]

        return {
            "task_id": task.task_id,
            "name": task.name,
            "cron_expression": task.cron_expression,
            "is_active": task.is_active,
        }

    async def get_task_history(
        self, task_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """查询任务执行历史"""
        async with AsyncSessionLocal() as session:
            runs = (
                (
                    await session.execute(
                        select(ScheduledTaskRun)
                        .where(ScheduledTaskRun.task_id == task_id)
                        .order_by(ScheduledTaskRun.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        return [
            {
                "id": r.id,
                "task_id": r.task_id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat()
                if r.finished_at
                else None,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "result": r.result,
                "triggered_by": r.triggered_by,
            }
            for r in runs
        ]


# 全局调度器单例（在 lifespan 中 start/stop）
_scheduler_instance: Optional[TaskScheduler] = None


def get_scheduler() -> Optional[TaskScheduler]:
    """获取全局调度器实例（未启动时返回 None）"""
    return _scheduler_instance


def set_scheduler(scheduler: Optional[TaskScheduler]) -> None:
    """设置全局调度器实例"""
    global _scheduler_instance
    _scheduler_instance = scheduler
