"""人工标注服务

对标 Langfuse Human-in-the-loop:
- 标注任务 CRUD (tenant_id 过滤)
- 分配标注任务
- 提交标注结果
- 标注列表查询
- 统计信息 (总数/已完成/待标注/平均分)
- 从评测结果批量创建标注任务 (标注-评测-优化闭环)

事务边界由路由层控制 (service 层不 commit)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.annotation_models import Annotation, AnnotationTask

logger = logging.getLogger(__name__)

# 允许的来源类型
VALID_SOURCE_TYPES = {"evaluation_result", "chat_message", "agent_output"}

# 允许的任务状态
VALID_TASK_STATUSES = {"pending", "in_progress", "completed"}


class AnnotationService:
    """人工标注服务 (数据库实现)"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 标注任务 CRUD =====================

    async def create_task(
        self,
        name: str,
        content: str,
        *,
        tenant_id: str = "default",
        description: Optional[str] = None,
        source_type: str = "agent_output",
        source_id: Optional[str] = None,
        priority: int = 0,
    ) -> AnnotationTask:
        """创建标注任务

        Args:
            name: 任务名称。
            content: 待标注内容。
            tenant_id: 租户 ID。
            description: 任务描述。
            source_type: 来源类型 (evaluation_result/chat_message/agent_output)。
            source_id: 来源记录 ID。
            priority: 优先级 (数值越大越优先)。

        Returns:
            创建的 AnnotationTask 对象。
        """
        if not name or not name.strip():
            raise ValueError("任务名称不能为空")
        if not content or not content.strip():
            raise ValueError("待标注内容不能为空")
        if source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"无效的来源类型: {source_type}, 可选: {VALID_SOURCE_TYPES}"
            )

        entity = AnnotationTask(
            tenant_id=tenant_id,
            name=name.strip(),
            description=description,
            source_type=source_type,
            source_id=source_id,
            content=content.strip(),
            status="pending",
            priority=priority,
        )
        self.session.add(entity)
        await self.session.flush()
        logger.info(
            "创建标注任务: %s (来源: %s, 租户: %s)", name, source_type, tenant_id
        )
        return entity

    async def get_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Optional[AnnotationTask]:
        """获取标注任务详情

        Args:
            task_id: 任务 ID。
            tenant_id: 租户 ID。

        Returns:
            AnnotationTask 对象, 不存在返回 None。
        """
        return (
            await self.session.execute(
                select(AnnotationTask).where(
                    AnnotationTask.id == task_id,
                    AnnotationTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        assigned_to: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询标注任务列表

        Args:
            tenant_id: 租户 ID。
            status: 按状态过滤 (None 表示全部)。
            assigned_to: 按分配人过滤 (None 表示全部)。
            page: 页码 (从 1 开始)。
            size: 每页条数。

        Returns:
            {"items": [...], "total": N, "page": P, "size": S}
        """
        base = (
            select(AnnotationTask)
            .where(AnnotationTask.tenant_id == tenant_id)
            .order_by(AnnotationTask.priority.desc(), AnnotationTask.created_at.desc())
        )
        if status:
            base = base.where(AnnotationTask.status == status)
        if assigned_to:
            base = base.where(AnnotationTask.assigned_to == assigned_to)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            (await self.session.execute(base.offset(offset).limit(size)))
            .scalars()
            .all()
        )

        return {
            "items": [self._task_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def update_task(
        self,
        task_id: int,
        *,
        tenant_id: str = "default",
        name: Optional[str] = None,
        description: Optional[str] = None,
        content: Optional[str] = None,
        priority: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Optional[AnnotationTask]:
        """更新标注任务

        Args:
            task_id: 任务 ID。
            tenant_id: 租户 ID。
            name: 新名称。
            description: 新描述。
            content: 新内容。
            priority: 新优先级。
            status: 新状态。

        Returns:
            更新后的 AnnotationTask 对象, 不存在返回 None。
        """
        entity = await self.get_task(task_id, tenant_id=tenant_id)
        if entity is None:
            return None

        if name is not None:
            if not name.strip():
                raise ValueError("任务名称不能为空")
            entity.name = name.strip()
        if description is not None:
            entity.description = description
        if content is not None:
            if not content.strip():
                raise ValueError("待标注内容不能为空")
            entity.content = content.strip()
        if priority is not None:
            entity.priority = priority
        if status is not None:
            if status not in VALID_TASK_STATUSES:
                raise ValueError(
                    f"无效的任务状态: {status}, 可选: {VALID_TASK_STATUSES}"
                )
            entity.status = status
            if status == "completed":
                from datetime import datetime, timezone

                entity.completed_at = datetime.now(timezone.utc)

        await self.session.flush()
        return entity

    async def delete_task(self, task_id: int, *, tenant_id: str = "default") -> bool:
        """删除标注任务 (同时删除所有标注)

        Args:
            task_id: 任务 ID。
            tenant_id: 租户 ID。

        Returns:
            True 表示已删除, False 表示不存在。
        """
        entity = await self.get_task(task_id, tenant_id=tenant_id)
        if entity is None:
            return False

        # 删除所有关联标注
        annotations = (
            (
                await self.session.execute(
                    select(Annotation).where(
                        Annotation.task_id == task_id,
                        Annotation.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for ann in annotations:
            await self.session.delete(ann)

        await self.session.delete(entity)
        await self.session.flush()
        logger.info("删除标注任务 id=%s (含 %d 标注)", task_id, len(annotations))
        return True

    # ===================== 分配与标注 =====================

    async def assign_task(
        self, task_id: int, user_id: str, *, tenant_id: str = "default"
    ) -> Optional[AnnotationTask]:
        """分配标注任务给指定用户

        Args:
            task_id: 任务 ID。
            user_id: 用户 ID。
            tenant_id: 租户 ID。

        Returns:
            更新后的 AnnotationTask 对象, 不存在返回 None。
        """
        entity = await self.get_task(task_id, tenant_id=tenant_id)
        if entity is None:
            return None

        entity.assigned_to = user_id
        if entity.status == "pending":
            entity.status = "in_progress"
        await self.session.flush()
        logger.info("分配标注任务 %s 给用户 %s", task_id, user_id)
        return entity

    async def submit_annotation(
        self,
        task_id: int,
        annotator_id: str,
        *,
        tenant_id: str = "default",
        label: Optional[str] = None,
        score: float = 0.0,
        feedback: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Annotation]:
        """提交标注结果

        Args:
            task_id: 任务 ID。
            annotator_id: 标注人 ID。
            tenant_id: 租户 ID。
            label: 标签。
            score: 评分 (0-100)。
            feedback: 反馈文本。
            metadata: 附加元数据。

        Returns:
            创建的 Annotation 对象, 任务不存在返回 None。
        """
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return None

        # 裁剪评分范围
        score = max(0.0, min(100.0, float(score)))

        annotation = Annotation(
            tenant_id=tenant_id,
            task_id=task_id,
            annotator_id=annotator_id,
            label=label,
            score=score,
            feedback=feedback,
            metadata_=metadata or {},
        )
        self.session.add(annotation)
        await self.session.flush()

        # 提交标注后标记任务完成
        task.status = "completed"
        from datetime import datetime, timezone

        task.completed_at = datetime.now(timezone.utc)
        await self.session.flush()

        logger.info(
            "提交标注: 任务 %s, 标注人 %s, 评分 %s", task_id, annotator_id, score
        )
        return annotation

    async def list_annotations(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        """查询任务的标注列表

        Args:
            task_id: 任务 ID。
            tenant_id: 租户 ID。

        Returns:
            标注列表 [{id, annotator_id, label, score, feedback, ...}]
        """
        rows = (
            (
                await self.session.execute(
                    select(Annotation)
                    .where(
                        Annotation.task_id == task_id,
                        Annotation.tenant_id == tenant_id,
                    )
                    .order_by(Annotation.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        return [self._annotation_to_dict(a) for a in rows]

    # ===================== 统计 =====================

    async def get_annotation_stats(
        self, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取标注统计信息

        Args:
            tenant_id: 租户 ID。

        Returns:
            {"total": N, "completed": N, "pending": N, "in_progress": N, "avg_score": float}
        """
        # 按状态分组统计任务数
        status_rows = (
            await self.session.execute(
                select(AnnotationTask.status, func.count(AnnotationTask.id))
                .where(AnnotationTask.tenant_id == tenant_id)
                .group_by(AnnotationTask.status)
            )
        ).all()
        status_counts = {row[0]: row[1] for row in status_rows}
        total = sum(status_counts.values())

        # 平均分 (所有标注的评分均值)
        avg_score = (
            await self.session.execute(
                select(func.avg(Annotation.score)).where(
                    Annotation.tenant_id == tenant_id
                )
            )
        ).scalar()

        return {
            "total": total,
            "completed": status_counts.get("completed", 0),
            "pending": status_counts.get("pending", 0),
            "in_progress": status_counts.get("in_progress", 0),
            "avg_score": round(float(avg_score), 2) if avg_score else 0.0,
        }

    # ===================== 批量创建 =====================

    async def batch_create_tasks_from_evaluation(
        self,
        eval_task_id: int,
        *,
        tenant_id: str = "default",
        priority: int = 0,
    ) -> Dict[str, Any]:
        """从 LLM 评测结果批量创建标注任务

        遍历指定评测任务的所有结果, 为每条创建一个标注任务,
        形成 "评测 -> 人工标注 -> 优化" 闭环。

        Args:
            eval_task_id: LLM 评测任务 ID。
            tenant_id: 租户 ID。
            priority: 标注任务优先级。

        Returns:
            {"created": N, "skipped": N, "errors": [...]}
        """
        # 延迟导入避免循环依赖
        from models.evaluation_models import EvaluationResult, EvaluationTask

        # 校验评测任务存在
        eval_task = (
            await self.session.execute(
                select(EvaluationTask).where(
                    EvaluationTask.id == eval_task_id,
                    EvaluationTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if eval_task is None:
            return {
                "created": 0,
                "skipped": 0,
                "errors": [f"评测任务 {eval_task_id} 不存在"],
            }

        # 获取所有评测结果
        results = (
            (
                await self.session.execute(
                    select(EvaluationResult).where(
                        EvaluationResult.task_id == eval_task_id,
                        EvaluationResult.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        created = 0
        skipped = 0
        errors: List[str] = []
        for result in results:
            try:
                content = result.agent_output or ""
                if not content.strip():
                    skipped += 1
                    continue

                # 构造标注任务内容: 含 Agent 输出 + Judge 评分 (供标注人参考)
                import json as _json

                content_with_context = (
                    f"## Agent 输出\n{content}\n\n"
                    f"## Judge 自动评分\n{_json.dumps(result.judge_scores, ensure_ascii=False)}\n\n"
                    f"## Judge 反馈\n{result.judge_feedback or '无'}"
                )

                task = AnnotationTask(
                    tenant_id=tenant_id,
                    name=f"标注-评测任务{eval_task_id}-结果{result.id}",
                    description=f"从评测任务 {eval_task.name} (ID: {eval_task_id}) 自动创建",
                    source_type="evaluation_result",
                    source_id=str(result.id),
                    content=content_with_context,
                    status="pending",
                    priority=priority,
                )
                self.session.add(task)
                created += 1
            except Exception as e:
                skipped += 1
                errors.append(f"创建标注任务失败 (结果 {result.id}): {e}")

        if created > 0:
            await self.session.flush()

        logger.info(
            "从评测任务 %s 批量创建标注任务: 成功 %d, 跳过 %d",
            eval_task_id,
            created,
            skipped,
        )
        return {"created": created, "skipped": skipped, "errors": errors}

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _task_to_dict(t: AnnotationTask) -> Dict[str, Any]:
        """AnnotationTask -> dict"""
        return {
            "id": t.id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "description": t.description,
            "source_type": t.source_type,
            "source_id": t.source_id,
            "content": t.content,
            "status": t.status,
            "assigned_to": t.assigned_to,
            "priority": t.priority,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    @staticmethod
    def _annotation_to_dict(a: Annotation) -> Dict[str, Any]:
        """Annotation -> dict"""
        return {
            "id": a.id,
            "tenant_id": a.tenant_id,
            "task_id": a.task_id,
            "annotator_id": a.annotator_id,
            "label": a.label,
            "score": a.score,
            "feedback": a.feedback,
            "metadata": a.metadata_,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
