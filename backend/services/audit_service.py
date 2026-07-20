"""
审计日志服务
记录所有对评估结果的关键操作，便于 HR 复核与合规追溯。
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.tenant_context import get_current_tenant
from core.utils.pii import redact_audit_details, redact_pii
from models import AuditLog


class AuditService:
    """审计服务（数据库实现）"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self,
        actor_id: str,
        action: str,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        details: Optional[Dict] = None,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> AuditLog:
        """记录审计日志（不 commit，由调用方控制事务）

        P0-3: details 写库前先做 PII 脱敏，避免手机号/邮箱/身份证号等明文落库。
        脱敏递归处理嵌套 dict/list 中的字符串值，非字符串类型原样保留。
        """
        details = redact_audit_details(details or {})
        entry = AuditLog(
            log_id=f"LOG-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            actor_id=actor_id,
            action=action,
            evaluation_id=evaluation_id,
            employee_id=employee_id,
            details=details or {},
            ip_address=ip_address,
            tenant_id=tenant_id or get_current_tenant(),
        )
        self.session.add(entry)
        return entry

    async def get_logs(
        self,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == get_current_tenant())
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        if evaluation_id:
            stmt = stmt.where(AuditLog.evaluation_id == evaluation_id)
        if employee_id:
            stmt = stmt.where(AuditLog.employee_id == employee_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def record_guard_check(
        self,
        guard_type: str,
        result: str,
        triggered_rules: Optional[List[str]] = None,
        would_be_false_positive: bool = False,
        evaluation_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> AuditLog:
        """记录一次护栏检查到审计日志。

        P1-5：在审计 details 中标注 would_be_false_positive，便于区分"真拦截"
        与"误报"（命中但实际为正常内容）。仅在 would_be_false_positive=True 时
        写入该键，避免污染正常拦截记录。误报判定为初版启发式，后续可接人工回标。

        参数：
            guard_type: 护栏类型（"input" / "output"）
            result: 检查结果（"clean" / "blocked"）
            triggered_rules: 触发的规则列表（clean 时为空）
            would_be_false_positive: 命中但实际为正常内容时置 True
        """
        details: Dict = {
            "guard_type": guard_type,
            "result": result,
        }
        if triggered_rules:
            # P0-3: triggered_rules 可能含被拦截原文，先逐条脱敏再写入；
            # log() 会再次整体脱敏 details（幂等，双重保险）
            details["triggered_rules"] = [redact_pii(str(r)) for r in triggered_rules]
        if would_be_false_positive:
            details["would_be_false_positive"] = True
        return await self.log(
            actor_id="system",
            action="guard_check",
            evaluation_id=evaluation_id,
            employee_id=employee_id,
            details=details,
            ip_address=ip_address,
        )

    async def list_logs(
        self,
        actor_id: Optional[str] = None,
        action: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict:
        """分页查询审计日志，支持按操作人、动作筛选"""
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == get_current_tenant())
            .order_by(AuditLog.created_at.desc())
        )
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        offset = (page - 1) * page_size
        page_stmt = stmt.offset(offset).limit(page_size)
        result = await self.session.execute(page_stmt)
        logs = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "logs": [
                {
                    "log_id": log.log_id,
                    "actor_id": log.actor_id,
                    "action": log.action,
                    "evaluation_id": log.evaluation_id,
                    "employee_id": log.employee_id,
                    "details": log.details,
                    "ip_address": log.ip_address,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }
