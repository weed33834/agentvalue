"""
评估相关数据库服务
封装 evaluations、raw_inputs、feedback、users、memories、company_kb 的 CRUD。
事务边界统一由路由层控制：service 层方法不 commit，仅 add/update 后返回。

多租户：查询方法显式追加 tenant_id 过滤（current_tenant 默认 default，兼容单租户历史数据）；
写入方法用 current_tenant 填充 tenant_id，确保新数据归属正确租户。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from core.field_crypto import get_field_cipher
from core.metrics import observe_evaluation_duration, record_evaluation
from core.tenant_context import get_current_tenant
from services.audit_decorator import audit_action
from models import (
    CompanyKB,
    DimensionScore,
    Evaluation,
    EvaluationPeriod,
    EvidenceRef,
    Feedback,
    Memory,
    RawInput,
    User,
)
from models.constants import EvaluationStatus

logger = logging.getLogger(__name__)


class EvaluationService:
    """评估服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ---------------- 字段级加密辅助 ----------------

    @staticmethod
    def _encrypt_view(value: Any) -> Any:
        """加密 manager_view / audit 敏感字段（dict → 密文字符串）。

        已是字符串（理论上不会发生）则原样返回，避免双重加密。
        FieldCipher 未配置密钥时透传（开发模式），保证向后兼容。
        """
        if isinstance(value, str):
            return value
        return get_field_cipher().encrypt_json(value)

    @staticmethod
    def _decrypt_view(value: Any) -> Any:
        """解密 manager_view / audit 字段（密文/JSON 字符串/dict → dict）。

        兼容三种输入：密文字符串、JSON 字符串（透传模式）、dict（旧明文数据）。
        """
        return get_field_cipher().decrypt_json(value)

    def _decrypt_eval_fields(self, evaluation: Optional[Evaluation]) -> None:
        """原地解密 evaluation 的 manager_view / audit，且不触发脏标记。

        使用 set_committed_value 同时更新当前值与已提交状态，避免后续
        session.commit() 把解密后的明文写回 DB（破坏加密语义）。
        无 evaluation（None）时直接返回。
        """
        if evaluation is None:
            return
        try:
            decrypted_mv = self._decrypt_view(evaluation.manager_view)
            set_committed_value(evaluation, "manager_view", decrypted_mv)
            decrypted_audit = self._decrypt_view(evaluation.audit)
            set_committed_value(evaluation, "audit", decrypted_audit)
        except Exception:
            logger.exception(
                "字段解密失败 evaluation_id=%s,降级返回原值",
                getattr(evaluation, "evaluation_id", None),
            )

    async def create_raw_input(self, data: Dict) -> RawInput:
        """创建原始输入（不 commit，由调用方控制事务）"""
        raw = RawInput(
            input_id=data.get("input_id") or f"INPUT-{uuid.uuid4().hex[:12]}",
            employee_id=data["employee_id"],
            period=data["period"],
            type=data.get("type", "daily_report"),
            content=data["content"],
            attachments=data.get("attachments", []),
            tenant_id=data.get("tenant_id") or get_current_tenant(),
        )
        self.session.add(raw)
        await self.session.flush()
        return raw

    async def get_raw_input(self, input_id: str) -> Optional[RawInput]:
        result = await self.session.execute(
            select(RawInput).where(
                RawInput.input_id == input_id,
                RawInput.tenant_id == get_current_tenant(),
            )
        )
        return result.scalar_one_or_none()

    async def list_raw_inputs(
        self,
        employee_id: Optional[str] = None,
        period: Optional[str] = None,
        limit: int = 100,
    ) -> List[RawInput]:
        stmt = (
            select(RawInput)
            .where(RawInput.tenant_id == get_current_tenant())
            .order_by(RawInput.created_at.desc())
            .limit(limit)
        )
        if employee_id:
            stmt = stmt.where(RawInput.employee_id == employee_id)
        if period:
            stmt = stmt.where(RawInput.period == period)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    @audit_action("create_evaluation")
    async def create_evaluation(self, evaluation_data: Dict) -> Evaluation:
        """保存评估结果，并同步拆分维度得分与证据引用（不 commit，由调用方控制事务）

        挂 @audit_action 后，service 层自动产生 create_evaluation 审计记录
        （actor_id 从 contextvar 读取，由路由层/中间件 set_audit_context 注入）。
        路由层若有手动 audit_service.log 调用，会产生重复审计记录——冗余优于缺失，
        路由层后续可移除手动调用。
        """
        tenant_id = evaluation_data.get("tenant_id") or get_current_tenant()
        # 字段级加密：manager_view / audit 落库为密文，DBA 直查 DB 仅见密文
        evaluation = Evaluation(
            evaluation_id=evaluation_data["evaluation_id"],
            employee_id=evaluation_data["employee_id"],
            period=evaluation_data["period"],
            overall_score=evaluation_data["overall_score"],
            employee_view=evaluation_data["employee_view"],
            manager_view=self._encrypt_view(evaluation_data["manager_view"]),
            audit=self._encrypt_view(evaluation_data["audit"]),
            status=evaluation_data.get("status", EvaluationStatus.AI_DRAFTED),
            tenant_id=tenant_id,
        )
        self.session.add(evaluation)

        # 同步拆分维度得分与证据引用，便于横向分析
        growth_areas = evaluation_data.get("employee_view", {}).get("growth_areas", [])
        for area in growth_areas:
            dim = DimensionScore(
                evaluation_id=evaluation.evaluation_id,
                employee_id=evaluation_data["employee_id"],
                period=evaluation_data["period"],
                dimension=area.get("dimension", ""),
                score=area.get("score", 0),
                improvement_actions=area.get("improvement_actions", []),
                tenant_id=tenant_id,
            )
            self.session.add(dim)
            for evidence in area.get("evidence", []):
                ref = EvidenceRef(
                    evaluation_id=evaluation.evaluation_id,
                    dimension=area.get("dimension", ""),
                    evidence_text=evidence,
                    tenant_id=tenant_id,
                )
                self.session.add(ref)

        await self.session.flush()
        # 业务埋点:评估完成量与耗时分布,model_tier/processing_time_ms 来自 audit
        # 埋点失败不影响评估落库主流程
        try:
            audit = evaluation_data.get("audit", {}) or {}
            model_tier = audit.get("model_tier") or "unknown"
            processing_ms = audit.get("processing_time_ms")
            record_evaluation(
                evaluation_data.get("status", EvaluationStatus.AI_DRAFTED), model_tier
            )
            if isinstance(processing_ms, (int, float)) and processing_ms >= 0:
                observe_evaluation_duration(processing_ms / 1000.0, model_tier)
        except Exception:
            logger.exception(
                "评估埋点失败 evaluation_id=%s", evaluation_data.get("evaluation_id")
            )
        return evaluation

    async def get_evaluation(self, evaluation_id: str) -> Optional[Evaluation]:
        result = await self.session.execute(
            select(Evaluation).where(
                Evaluation.evaluation_id == evaluation_id,
                Evaluation.tenant_id == get_current_tenant(),
            )
        )
        evaluation = result.scalar_one_or_none()
        # 解密敏感字段（set_committed_value 不触发脏标记，避免 commit 写回明文）
        self._decrypt_eval_fields(evaluation)
        return evaluation

    async def get_evaluation_for_update(
        self, evaluation_id: str
    ) -> Optional[Evaluation]:
        """带悲观锁的评估查询，用于状态转换等需要避免竞态的场景"""
        result = await self.session.execute(
            select(Evaluation)
            .where(
                Evaluation.evaluation_id == evaluation_id,
                Evaluation.tenant_id == get_current_tenant(),
            )
            .with_for_update()
        )
        evaluation = result.scalar_one_or_none()
        self._decrypt_eval_fields(evaluation)
        return evaluation

    async def update_status(
        self,
        evaluation_id: str,
        new_status: str,
        approver_id: Optional[str] = None,
    ) -> Optional[Evaluation]:
        """更新评估状态（不 commit，由调用方控制事务）"""
        evaluation = await self.get_evaluation(evaluation_id)
        if not evaluation:
            return None
        old_status = evaluation.status
        evaluation.status = new_status

        if new_status == EvaluationStatus.APPROVED:
            evaluation.approved_at = datetime.now(timezone.utc)
            if approver_id:
                evaluation.approver_id = approver_id
        elif (
            old_status == EvaluationStatus.APPROVED
            and new_status != EvaluationStatus.APPROVED
        ):
            # 离开 approved 状态时重置审批信息
            evaluation.approved_at = None
            evaluation.approver_id = None
        elif approver_id:
            evaluation.approver_id = approver_id

        return evaluation

    @audit_action("update_evaluation")
    async def update_evaluation(
        self,
        evaluation_id: str,
        evaluation_data: Dict,
    ) -> Optional[Evaluation]:
        """完整更新评估内容（不 commit，由调用方控制事务）。

        挂 @audit_action 自动产生 update_evaluation 审计记录。

        会刷新该 evaluation_id 关联的 DimensionScore / EvidenceRef：
        先删除旧记录，再按新 employee_view.growth_areas 重新拆分写入，
        与 create_evaluation 保持一致，避免重评后维度/证据过期。
        """
        evaluation = await self.get_evaluation(evaluation_id)
        if not evaluation:
            return None
        old_status = evaluation.status
        evaluation.employee_view = evaluation_data.get(
            "employee_view", evaluation.employee_view
        )
        evaluation.manager_view = evaluation_data.get(
            "manager_view", evaluation.manager_view
        )
        evaluation.audit = evaluation_data.get("audit", evaluation.audit)
        evaluation.overall_score = evaluation_data.get(
            "overall_score", evaluation.overall_score
        )
        evaluation.status = evaluation_data.get("status", evaluation.status)

        # 字段级加密：manager_view / audit 落库为密文。
        # get_evaluation 已解密为 dict，此处统一重新加密后写库。
        evaluation.manager_view = self._encrypt_view(evaluation.manager_view)
        evaluation.audit = self._encrypt_view(evaluation.audit)

        new_status = evaluation.status
        if new_status == EvaluationStatus.APPROVED:
            evaluation.approved_at = datetime.now(timezone.utc)
        elif (
            old_status == EvaluationStatus.APPROVED
            and new_status != EvaluationStatus.APPROVED
        ):
            evaluation.approved_at = None
            evaluation.approver_id = None

        # 刷新 DimensionScore / EvidenceRef：先删旧再插新
        await self.session.execute(
            delete(DimensionScore).where(DimensionScore.evaluation_id == evaluation_id)
        )
        await self.session.execute(
            delete(EvidenceRef).where(EvidenceRef.evaluation_id == evaluation_id)
        )
        growth_areas = evaluation_data.get("employee_view", {}).get("growth_areas", [])
        for area in growth_areas:
            dim = DimensionScore(
                evaluation_id=evaluation_id,
                employee_id=evaluation.employee_id,
                period=evaluation.period,
                dimension=area.get("dimension", ""),
                score=area.get("score", 0),
                improvement_actions=area.get("improvement_actions", []),
                tenant_id=evaluation.tenant_id,
            )
            self.session.add(dim)
            for evidence in area.get("evidence", []):
                ref = EvidenceRef(
                    evaluation_id=evaluation_id,
                    dimension=area.get("dimension", ""),
                    evidence_text=evidence,
                    tenant_id=evaluation.tenant_id,
                )
                self.session.add(ref)
        await self.session.flush()

        return evaluation

    async def list_evaluations(
        self,
        employee_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        period: Optional[str] = None,
    ) -> Dict:
        """分页查询评估列表，返回 {items, total, page, page_size}。

        为保持与历史调用方兼容，limit/offset 仍可使用；当传入 page/page_size 时
        优先按分页参数计算 offset，limit 取 page_size。
        period 为可选过滤参数（P1-2 trace 浏览器使用），不传时不过滤。
        """
        if page is not None and page_size is not None:
            if page < 1:
                page = 1
            if page_size < 1 or page_size > 500:
                page_size = 100
            offset = (page - 1) * page_size
            limit = page_size
        else:
            # 未传分页时，page 用 offset 推算，便于响应字段统一
            page = (offset // limit) + 1 if limit > 0 else 1
            page_size = limit

        base = (
            select(Evaluation)
            .where(Evaluation.tenant_id == get_current_tenant())
            .order_by(Evaluation.created_at.desc())
        )
        if employee_id:
            base = base.where(Evaluation.employee_id == employee_id)
        if status:
            base = base.where(Evaluation.status == status)
        if period:
            base = base.where(Evaluation.period == period)

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0

        rows = (
            (await self.session.execute(base.offset(offset).limit(limit)))
            .scalars()
            .all()
        )
        # 批量解密敏感字段（set_committed_value 不触发脏标记）
        for row in rows:
            self._decrypt_eval_fields(row)
        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def create_feedback(self, data: Dict) -> Feedback:
        """创建反馈（不 commit，由调用方控制事务）"""
        feedback = Feedback(
            feedback_id=data.get("feedback_id") or f"FB-{uuid.uuid4().hex[:12]}",
            evaluation_id=data["evaluation_id"],
            employee_id=data["employee_id"],
            type=data.get("type", "feedback"),
            content=data["content"],
            tenant_id=data.get("tenant_id") or get_current_tenant(),
        )
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def list_feedback(
        self,
        employee_id: Optional[str] = None,
        evaluation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[tuple[Feedback, Evaluation]]:
        """
        查询反馈/申诉记录，并关联其所属评估（用于前端追踪申诉处理进度）。
        返回 [(feedback, evaluation), ...]，按反馈创建时间倒序。
        不 commit（只读查询）。
        """
        stmt = (
            select(Feedback, Evaluation)
            .join(Evaluation, Feedback.evaluation_id == Evaluation.evaluation_id)
            .where(Feedback.tenant_id == get_current_tenant())
            .order_by(Feedback.created_at.desc())
            .limit(limit)
        )
        if employee_id:
            stmt = stmt.where(Feedback.employee_id == employee_id)
        if evaluation_id:
            stmt = stmt.where(Feedback.evaluation_id == evaluation_id)
        result = await self.session.execute(stmt)
        return result.all()

    async def get_user(self, user_id: str) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(
                User.user_id == user_id,
                User.tenant_id == get_current_tenant(),
            )
        )
        return result.scalar_one_or_none()

    async def get_user_by_email(self, email: str) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(
                User.email == email,
                User.tenant_id == get_current_tenant(),
            )
        )
        return result.scalar_one_or_none()

    async def create_user(self, data: Dict) -> User:
        """创建用户（不 commit，由调用方控制事务）"""
        user = User(
            user_id=data["user_id"],
            name=data["name"],
            email=data.get("email"),
            role=data.get("role", "employee"),
            department=data.get("department"),
            manager_id=data.get("manager_id"),
            password_hash=data.get("password_hash"),
            tenant_id=data.get("tenant_id") or get_current_tenant(),
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def ensure_user_exists(
        self, user_id: str, name: str = "", role: str = "employee"
    ) -> User:
        user = await self.get_user(user_id)
        if user:
            return user
        user = await self.create_user(
            {"user_id": user_id, "name": name or user_id, "role": role}
        )
        # 评估时静默建号需留痕,避免用户表无声增长且无审计可查
        try:
            from services.audit_service import AuditService

            await AuditService(self.session).log(
                actor_id="system",
                action="create_user",
                employee_id=user_id,
                details={
                    "user_id": user_id,
                    "role": role,
                    "source": "ensure_user_exists",
                },
            )
        except Exception:
            logger.warning(
                "ensure_user_exists 审计记录失败 user_id=%s", user_id, exc_info=True
            )
        return user

    async def add_memory(self, employee_id: str, memory: Dict) -> Memory:
        """添加员工记忆（不 commit，由调用方控制事务）"""
        existing = await self.session.execute(
            select(Memory).where(
                Memory.employee_id == employee_id,
                Memory.period == memory.get("period", ""),
                Memory.tenant_id == get_current_tenant(),
            )
        )
        mem = existing.scalar_one_or_none()
        if mem:
            mem.content = memory.get("summary", "")
            mem.payload = memory
        else:
            mem = Memory(
                employee_id=employee_id,
                period=memory.get("period", ""),
                content=memory.get("summary", ""),
                payload=memory,
                tenant_id=get_current_tenant(),
            )
            self.session.add(mem)
        await self.session.flush()
        return mem

    async def create_kb_doc(self, data: Dict) -> CompanyKB:
        """创建知识库文档（不 commit，由调用方控制事务）"""
        doc = CompanyKB(
            kb_id=data["kb_id"],
            title=data["title"],
            content=data["content"],
            metadata_=data.get("metadata", {}),
            tenant_id=data.get("tenant_id") or get_current_tenant(),
        )
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def get_kb_doc(self, kb_id: str) -> Optional[CompanyKB]:
        """按 kb_id 查询单条知识库文档"""
        result = await self.session.execute(
            select(CompanyKB).where(
                CompanyKB.kb_id == kb_id,
                CompanyKB.tenant_id == get_current_tenant(),
            )
        )
        return result.scalar_one_or_none()

    async def list_kb_docs(self, page: int = 1, page_size: int = 20) -> Dict:
        """分页查询知识库文档，返回 {items, total, page, page_size}"""
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 200:
            page_size = 20
        base = select(CompanyKB).where(CompanyKB.tenant_id == get_current_tenant())
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0
        offset = (page - 1) * page_size
        stmt = (
            base.order_by(CompanyKB.created_at.desc()).offset(offset).limit(page_size)
        )
        docs = (await self.session.execute(stmt)).scalars().all()
        return {
            "items": docs,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def delete_kb_doc(self, kb_id: str) -> bool:
        """删除知识库文档（不 commit），返回是否实际删除"""
        doc = await self.get_kb_doc(kb_id)
        if not doc:
            return False
        await self.session.delete(doc)
        await self.session.flush()
        return True

    async def list_direct_reports(self, manager_id: str) -> List[User]:
        """查询某主管名下的直属下属，用于 RBAC 团队归属校验"""
        result = await self.session.execute(
            select(User).where(
                User.manager_id == manager_id,
                User.tenant_id == get_current_tenant(),
            )
        )
        return result.scalars().all()

    async def get_team_analytics(self, team_members: List[str]) -> Dict:
        """团队分析聚合"""
        stmt = (
            select(
                Evaluation.employee_id,
                func.avg(Evaluation.overall_score).label("avg_score"),
                func.count(Evaluation.id).label("eval_count"),
            )
            .where(
                Evaluation.employee_id.in_(team_members),
                Evaluation.tenant_id == get_current_tenant(),
            )
            .group_by(Evaluation.employee_id)
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        return {
            "members": [
                {
                    "employee_id": row.employee_id,
                    "avg_score": round(row.avg_score or 0, 2),
                    "eval_count": row.eval_count,
                }
                for row in rows
            ],
            "overall_avg": (
                round(sum(r.avg_score or 0 for r in rows) / len(rows), 2) if rows else 0
            ),
        }

    # ---------------- 评估周期管理（H9：EvaluationPeriod 业务化） ----------------

    async def create_period(self, data: Dict) -> EvaluationPeriod:
        """创建评估周期（不 commit，由调用方控制事务）"""
        period = EvaluationPeriod(
            period=data["period"],
            period_type=data.get("period_type", "weekly"),
            start_date=data["start_date"],
            end_date=data["end_date"],
            status=data.get("status", "open"),
            tenant_id=data.get("tenant_id") or get_current_tenant(),
        )
        self.session.add(period)
        await self.session.flush()
        return period

    async def get_period(self, period: str) -> Optional[EvaluationPeriod]:
        """按周期标识查询评估周期"""
        result = await self.session.execute(
            select(EvaluationPeriod).where(
                EvaluationPeriod.period == period,
                EvaluationPeriod.tenant_id == get_current_tenant(),
            )
        )
        return result.scalar_one_or_none()

    async def list_periods(
        self, status: Optional[str] = None, limit: int = 100
    ) -> List[EvaluationPeriod]:
        """查询评估周期列表，可按状态过滤"""
        stmt = (
            select(EvaluationPeriod)
            .where(EvaluationPeriod.tenant_id == get_current_tenant())
            .order_by(EvaluationPeriod.start_date.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(EvaluationPeriod.status == status)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def close_period(self, period: str) -> Optional[EvaluationPeriod]:
        """关闭评估周期（不 commit），周期不存在返回 None"""
        period_obj = await self.get_period(period)
        if not period_obj:
            return None
        period_obj.status = "closed"
        await self.session.flush()
        return period_obj

    # ---------------- 用户管理 CRUD ----------------

    async def list_users(
        self,
        tenant_id: str,
        role: Optional[str] = None,
        department: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict:
        """列出用户（分页，支持按 role / department 过滤）

        返回 {items, total, page, page_size}。
        tenant_id 显式传入，admin 端可跨租户查询（传入目标 tenant_id）。
        """
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 500:
            page_size = 20

        base = select(User).where(User.tenant_id == tenant_id)
        if role:
            base = base.where(User.role == role)
        if department:
            base = base.where(User.department == department)

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0

        offset = (page - 1) * page_size
        stmt = base.order_by(User.created_at.desc()).offset(offset).limit(page_size)
        rows = (await self.session.execute(stmt)).scalars().all()
        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def update_user(
        self, tenant_id: str, user_id: str, **kwargs: Any
    ) -> Optional[User]:
        """更新用户信息（name / role / department / manager_id）

        仅更新 kwargs 中提供的字段，不 commit（由调用方控制事务）。
        用户不存在返回 None。
        """
        result = await self.session.execute(
            select(User).where(
                User.user_id == user_id,
                User.tenant_id == tenant_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        allowed_fields = {"name", "role", "department", "manager_id"}
        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                setattr(user, key, value)
        await self.session.flush()
        return user

    async def disable_user(self, tenant_id: str, user_id: str) -> bool:
        """禁用用户（设置 role 为 disabled）

        禁用后用户无法登录，但记录保留可查（soft disable）。
        不 commit（由调用方控制事务）。用户不存在返回 False。
        """
        result = await self.session.execute(
            select(User).where(
                User.user_id == user_id,
                User.tenant_id == tenant_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return False
        user.role = "disabled"
        await self.session.flush()
        return True

    async def delete_user(self, tenant_id: str, user_id: str) -> bool:
        """删除用户（hard delete）

        物理删除用户记录，不可恢复。不 commit（由调用方控制事务）。
        用户不存在返回 False。
        """
        result = await self.session.execute(
            select(User).where(
                User.user_id == user_id,
                User.tenant_id == tenant_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return False
        await self.session.delete(user)
        await self.session.flush()
        return True

    async def batch_create_users(self, tenant_id: str, users: List[Dict]) -> List[User]:
        """批量创建用户（不 commit，由调用方控制事务）

        每个用户 dict 需包含 user_id + name，其余字段可选。
        已存在的 user_id（同租户内）跳过，不报错。
        """
        created: List[User] = []
        for data in users:
            # 查重：同租户内 user_id 已存在则跳过
            existing = await self.session.execute(
                select(User).where(
                    User.user_id == data["user_id"],
                    User.tenant_id == tenant_id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue
            user = User(
                user_id=data["user_id"],
                name=data["name"],
                email=data.get("email"),
                role=data.get("role", "employee"),
                department=data.get("department"),
                manager_id=data.get("manager_id"),
                password_hash=data.get("password_hash"),
                tenant_id=tenant_id,
            )
            self.session.add(user)
            created.append(user)
        await self.session.flush()
        return created
