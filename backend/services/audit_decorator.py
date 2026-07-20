"""
service 层统一审计装饰器。

用法::

    from services.audit_decorator import audit_action

    class EvaluationService:
        @audit_action("create_evaluation")
        async def create_evaluation(self, evaluation_data: Dict) -> Evaluation:
            ...

装饰器自动：
- 从 contextvar（set_audit_context 注入）或 kwargs 兜底提取 actor_id / ip
- 从返回值或 kwargs 提取 resource_id
- 复用 self.session 构造 AuditService 写入审计日志
- 审计失败不阻断业务，仅记 agentvalue_audit_log_failures_total 指标

设计要点：
- 不依赖路由层手动调 audit_service.log，service 层方法被装饰后自动产生审计
- 通过 contextvar 获取当前用户/租户/IP；contextvar 未设置时从 kwargs 兜底
  （actor_id / user_id / approver_id / ip_address / client_ip）
- 审计日志与业务共用同一 session，由路由层统一 commit，保证原子性
"""

import contextvars
import functools
import logging
from typing import Any, Optional

from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# 请求级审计上下文，由路由层/中间件在鉴权后注入
# 未设置时装饰器从 kwargs 兜底，保证 service 层独立可测
_current_actor_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_audit_current_actor_id", default=None
)
_current_actor_ip: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_audit_current_actor_ip", default=None
)


def set_audit_context(
    actor_id: Optional[str], ip: Optional[str] = None
) -> contextvars.Token:
    """设置当前请求的审计上下文（actor_id 与 ip）。

    供路由层/中间件在鉴权完成后调用，写入 contextvar 供 service 层装饰器读取。
    返回 token 供 reset_audit_context 恢复，避免请求间泄漏。
    """
    t1 = _current_actor_id.set(actor_id or "system")
    t2 = _current_actor_ip.set(ip)
    # 复合 token：恢复时需同时 reset 两个 var
    return (t1, t2)  # type: ignore[return-value]


def reset_audit_context(token) -> None:
    """恢复审计上下文，避免请求间泄漏。"""
    t1, t2 = token  # type: ignore[misc]
    _current_actor_id.reset(t1)
    _current_actor_ip.reset(t2)


def _extract_actor_id(kwargs: dict) -> str:
    """优先 contextvar，其次 kwargs 兜底，最后 system。"""
    actor = _current_actor_id.get()
    if actor:
        return actor
    for key in ("actor_id", "user_id", "approver_id"):
        val = kwargs.get(key)
        if val:
            return str(val)
    return "system"


def _extract_ip(kwargs: dict) -> Optional[str]:
    """优先 contextvar，其次 kwargs 兜底。"""
    ip = _current_actor_ip.get()
    if ip:
        return ip
    for key in ("ip_address", "client_ip"):
        val = kwargs.get(key)
        if val:
            return str(val)
    return None


def _extract_resource_id(result: Any, kwargs: dict) -> Optional[str]:
    """从返回值属性或 kwargs 提取资源 ID。"""
    # 1. 返回对象属性（Evaluation/RawInput/Feedback 等都有对应 ID 字段）
    if result is not None:
        for attr in (
            "evaluation_id",
            "feedback_id",
            "input_id",
            "user_id",
            "kb_id",
            "period",
        ):
            rid = getattr(result, attr, None)
            if rid:
                return str(rid)
    # 2. kwargs 参数
    for key in (
        "evaluation_id",
        "feedback_id",
        "input_id",
        "user_id",
        "kb_id",
        "employee_id",
    ):
        val = kwargs.get(key)
        if val:
            return str(val)
    return None


def _get_audit_service_from_self(args: tuple):
    """从被装饰方法的 self.session 构造 AuditService。

    service 层方法签名为 async def method(self, ...)，args[0] 为 self。
    若 self 无 session 属性或 args 为空，返回 None（跳过审计）。
    """
    if not args:
        return None
    self_obj = args[0]
    session = getattr(self_obj, "session", None)
    if session is None:
        return None
    try:
        from services.audit_service import AuditService

        return AuditService(session)
    except Exception:
        return None


def audit_action(action: str, resource_type: str = "evaluation"):
    """service 层统一审计装饰器。

    参数：
        action: 审计动作名（如 "create_evaluation" / "approve" / "view"）
        resource_type: 资源类型，默认 "evaluation"。
            当前仅 evaluation 资源会写入 evaluation_id 字段，
            其他类型资源 ID 落到 details.resource_id。

    行为：
        - 业务方法成功返回后记录审计(P1-N3 修复: 失败也记录 *_failed 审计,
          含异常类型/消息, 供安全审计追查越权/非法操作)
        - actor_id 从 contextvar 或 kwargs 提取
        - 审计写入失败不阻断业务, 仅记 agentvalue_audit_log_failures_total
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                # P1-N3 修复: 业务失败时也写一条 *_failed 审计, 记录异常类型与消息
                # 供安全审计追查越权尝试/非法状态转换/被 RBAC 拒绝等场景
                # P2-5 修复: 用独立 AsyncSessionLocal 写 _failed 审计并立即 commit,
                # 避免路由层业务 session rollback 时把 _failed 审计一并回滚
                # (越权/非法操作场景下业务 session 通常会被回滚, 审计必须留存)
                try:
                    actor_id = _extract_actor_id(kwargs)
                    ip = _extract_ip(kwargs)
                    from services.audit_service import AuditService

                    async with AsyncSessionLocal() as audit_session:
                        audit_service = AuditService(audit_session)
                        await audit_service.log(
                            actor_id=actor_id,
                            action=f"{action}_failed",
                            ip_address=ip,
                            details={
                                "resource_id": _extract_resource_id(None, kwargs),
                                "exception_type": type(exc).__name__,
                                "exception_msg": str(exc)[:500],
                            },
                        )
                        await audit_session.commit()
                    try:
                        from core.metrics import record_audit_log

                        record_audit_log(f"{action}_failed")
                    except Exception:
                        logger.debug(
                            "记录审计指标失败 action=%s_failed", action, exc_info=True
                        )
                except Exception:
                    logger.exception("审计装饰器(失败分支)记录失败 action=%s", action)
                    try:
                        from core.metrics import record_audit_log_failure

                        record_audit_log_failure()
                    except Exception:
                        logger.debug("记录审计失败指标失败", exc_info=True)
                raise  # re-raise 原异常, 不改变业务行为
            try:
                actor_id = _extract_actor_id(kwargs)
                ip = _extract_ip(kwargs)
                resource_id = _extract_resource_id(result, kwargs)
                audit_service = _get_audit_service_from_self(args)
                if audit_service is not None:
                    log_kwargs = {
                        "actor_id": actor_id,
                        "action": action,
                        "ip_address": ip,
                    }
                    if resource_type == "evaluation" and resource_id:
                        log_kwargs["evaluation_id"] = resource_id
                    elif resource_id:
                        # 非评估资源 ID 放 details, 避免占用 evaluation_id 列
                        log_kwargs["details"] = {"resource_id": resource_id}
                    # 兜底 employee_id(若 kwargs 显式传入)
                    employee_id = kwargs.get("employee_id")
                    if employee_id:
                        log_kwargs["employee_id"] = str(employee_id)
                    await audit_service.log(**log_kwargs)
                try:
                    from core.metrics import record_audit_log

                    record_audit_log(action)
                except Exception:
                    logger.debug("记录审计指标失败 action=%s", action, exc_info=True)
            except Exception:
                logger.exception("审计装饰器记录失败 action=%s", action)
                try:
                    from core.metrics import record_audit_log_failure

                    record_audit_log_failure()
                except Exception:
                    logger.debug("记录审计失败指标失败", exc_info=True)
            return result

        return wrapper

    return decorator
