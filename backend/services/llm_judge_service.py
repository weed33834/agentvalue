"""LLM-as-a-Judge 自动评测服务

对标 Langfuse LLM-as-a-Judge + Dify 日志回放:
- 创建评测任务 (关联数据集, 配置 judge 模型/提示词/维度)
- 异步执行评测: 遍历数据集条目, 用 LLM 生成 Agent 输出, 用 LLM Judge 评分
- 分页查询结果
- 汇总统计 (平均分/通过率/各维度得分)

后台任务使用 asyncio.create_task() 异步执行, 不阻塞 API 响应。
后台任务内部通过 AsyncSessionLocal 创建独立数据库会话。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.llm_call import call_llm_with_fallback
from core.providers.base import ChatMessage
from core.tenant_context import tenant_scope
from models.dataset_models import DatasetItem, EvaluationDataset
from models.evaluation_models import EvaluationResult, EvaluationTask

logger = logging.getLogger(__name__)

# 默认评测维度
DEFAULT_METRICS = ["accuracy", "relevance", "completeness", "fluency"]

# 默认通过阈值 (综合评分 >= 60 视为通过)
DEFAULT_PASS_THRESHOLD = 60.0

# 默认 judge 提示词模板
DEFAULT_JUDGE_PROMPT_TEMPLATE = """你是一个专业的 AI 输出质量评审员。请对以下 Agent 输出进行评分。

## 输入
{input}

## 期望输出
{expected_output}

## Agent 实际输出
{output}

## 评测维度
请对以下每个维度打分 (0-100 分整数):
{metrics}

## 输出要求
请返回 JSON 格式, 包含以下字段:
- 各维度的分数 (key 为维度名, value 为 0-100 整数)
- "overall": 综合评分 (0-100 整数)
- "feedback": 简短中文反馈说明

示例:
{{"accuracy": 85, "relevance": 90, "completeness": 80, "fluency": 95, "overall": 87, "feedback": "输出准确且相关, 但完整性可提升"}}"""


class LLMJudgeService:
    """LLM-as-a-Judge 自动评测服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ===================== 任务 CRUD =====================

    async def create_task(
        self,
        name: str,
        dataset_id: int,
        *,
        tenant_id: str = "default",
        judge_model: str = "L0",
        metrics: Optional[List[str]] = None,
        judge_prompt_template: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> EvaluationTask:
        """创建评测任务

        Args:
            name: 任务名称。
            dataset_id: 关联数据集 ID。
            tenant_id: 租户 ID。
            judge_model: 评判模型档位 (L0/L1/L2/L3)。
            metrics: 评测维度列表。
            judge_prompt_template: 评判提示词模板。
            created_by: 创建人 ID。

        Returns:
            创建的 EvaluationTask 对象。
        """
        if not name or not name.strip():
            raise ValueError("任务名称不能为空")

        # 校验数据集存在
        dataset = (
            await self.session.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.id == dataset_id,
                    EvaluationDataset.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if dataset is None:
            raise ValueError(f"数据集 {dataset_id} 不存在")

        # 统计数据集条目数
        item_count = (
            await self.session.execute(
                select(func.count(DatasetItem.id)).where(
                    DatasetItem.dataset_id == dataset_id,
                    DatasetItem.tenant_id == tenant_id,
                )
            )
        ).scalar() or 0

        task = EvaluationTask(
            tenant_id=tenant_id,
            name=name.strip(),
            dataset_id=dataset_id,
            judge_model=judge_model,
            judge_prompt_template=judge_prompt_template
            or DEFAULT_JUDGE_PROMPT_TEMPLATE,
            metrics=metrics or DEFAULT_METRICS,
            status="pending",
            total_items=item_count,
            completed_items=0,
            progress=0,
            created_by=created_by,
        )
        self.session.add(task)
        await self.session.flush()
        logger.info(
            "创建评测任务: %s (数据集: %s, 条目数: %d, 租户: %s)",
            name,
            dataset_id,
            item_count,
            tenant_id,
        )
        return task

    async def get_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Optional[EvaluationTask]:
        """获取评测任务详情"""
        return (
            await self.session.execute(
                select(EvaluationTask).where(
                    EvaluationTask.id == task_id,
                    EvaluationTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        tenant_id: str = "default",
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询评测任务列表"""
        base = (
            select(EvaluationTask)
            .where(EvaluationTask.tenant_id == tenant_id)
            .order_by(EvaluationTask.created_at.desc())
        )
        if status:
            base = base.where(EvaluationTask.status == status)

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._task_to_dict(t) for t in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def delete_task(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> bool:
        """删除评测任务 (同时删除所有结果)"""
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return False

        # 删除所有关联结果
        results = (
            await self.session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.task_id == task_id,
                    EvaluationResult.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        for r in results:
            await self.session.delete(r)

        await self.session.delete(task)
        await self.session.flush()
        logger.info("删除评测任务 id=%s (含 %d 结果)", task_id, len(results))
        return True

    # ===================== 任务执行 =====================

    def run_task_background(
        self,
        task_id: int,
        model_router: Any,
        *,
        tenant_id: str = "default",
    ) -> asyncio.Task:
        """启动后台评测任务 (不阻塞 API 响应)

        Args:
            task_id: 评测任务 ID。
            model_router: ModelRouter 实例 (用于 LLM 调用)。
            tenant_id: 租户 ID。

        Returns:
            asyncio.Task 对象。
        """
        return asyncio.create_task(
            self._run_task_async(task_id, model_router, tenant_id=tenant_id)
        )

    async def _run_task_async(
        self,
        task_id: int,
        model_router: Any,
        *,
        tenant_id: str = "default",
    ) -> None:
        """后台异步执行评测任务

        使用独立数据库会话, 设置租户上下文, 遍历数据集条目进行评测。
        """
        # 设置租户上下文 (后台任务需要显式设置 contextvar)
        with tenant_scope(tenant_id):
            async with AsyncSessionLocal() as session:
                try:
                    # 标记任务为运行中
                    task = (
                        await session.execute(
                            select(EvaluationTask).where(
                                EvaluationTask.id == task_id,
                                EvaluationTask.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if task is None:
                        logger.error("评测任务 %s 不存在", task_id)
                        return

                    task.status = "running"
                    await session.commit()

                    # 加载数据集条目
                    items = (
                        await session.execute(
                            select(DatasetItem)
                            .where(
                                DatasetItem.dataset_id == task.dataset_id,
                                DatasetItem.tenant_id == tenant_id,
                            )
                            .order_by(DatasetItem.created_at.asc())
                        )
                    ).scalars().all()

                    task.total_items = len(items)
                    await session.commit()

                    completed = 0
                    for item in items:
                        try:
                            result = await self._judge_single(
                                session=session,
                                task=task,
                                item=item,
                                model_router=model_router,
                                tenant_id=tenant_id,
                            )
                            if result:
                                session.add(result)
                                await session.commit()

                            completed += 1
                        except Exception as e:
                            logger.warning(
                                "评测条目 %s 失败: %s", item.id, e, exc_info=True
                            )
                            completed += 1

                        # 更新进度
                        task.completed_items = completed
                        task.progress = int(
                            (completed / task.total_items * 100)
                            if task.total_items > 0
                            else 0
                        )
                        await session.commit()

                    # 计算汇总
                    summary = await self._compute_summary(
                        session, task_id, tenant_id=tenant_id
                    )
                    task.results_summary = summary
                    task.status = "completed"
                    task.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                    logger.info(
                        "评测任务 %s 完成: %d/%d 条目, 平均分 %.1f",
                        task_id,
                        completed,
                        task.total_items,
                        summary.get("avg_score", 0),
                    )

                except Exception as e:
                    logger.error(
                        "评测任务 %s 执行失败: %s", task_id, e, exc_info=True
                    )
                    # 标记任务为失败
                    try:
                        async with AsyncSessionLocal() as err_session:
                            err_task = (
                                await err_session.execute(
                                    select(EvaluationTask).where(
                                        EvaluationTask.id == task_id,
                                        EvaluationTask.tenant_id == tenant_id,
                                    )
                                )
                            ).scalar_one_or_none()
                            if err_task is not None:
                                err_task.status = "failed"
                                err_task.completed_at = datetime.now(timezone.utc)
                                await err_session.commit()
                    except Exception:
                        logger.error("标记任务失败状态时出错", exc_info=True)

    async def _judge_single(
        self,
        *,
        session: AsyncSession,
        task: EvaluationTask,
        item: DatasetItem,
        model_router: Any,
        tenant_id: str = "default",
    ) -> Optional[EvaluationResult]:
        """对单条数据集条目进行评测

        1. 用 LLM 生成 Agent 输出 (以 input 为提示)
        2. 用 LLM Judge 对输出评分
        3. 返回 EvaluationResult 对象

        Args:
            session: 数据库会话。
            task: 评测任务。
            item: 数据集条目。
            model_router: ModelRouter 实例。
            tenant_id: 租户 ID。

        Returns:
            EvaluationResult 对象。
        """
        start_time = time.monotonic()

        # 1. 生成 Agent 输出
        input_text = self._extract_text(item.input)
        try:
            agent_messages = [
                ChatMessage(role="system", content="你是一个专业的 AI 助手, 请根据输入生成回答。"),
                ChatMessage(role="user", content=input_text),
            ]
            completion, _tier = await call_llm_with_fallback(
                model_router, messages=agent_messages
            )
            agent_output = completion.content or ""
        except Exception as e:
            logger.warning("生成 Agent 输出失败 (条目 %s): %s", item.id, e)
            agent_output = f"[生成失败: {e}]"

        # 2. LLM Judge 评分
        expected_text = self._extract_text(item.expected_output) if item.expected_output else "无"
        metrics_str = "\n".join(f"- {m}" for m in (task.metrics or DEFAULT_METRICS))

        prompt_template = task.judge_prompt_template or DEFAULT_JUDGE_PROMPT_TEMPLATE
        judge_prompt = prompt_template.format(
            input=input_text,
            expected_output=expected_text,
            output=agent_output,
            metrics=metrics_str,
        )

        judge_scores: Dict[str, Any] = {}
        judge_feedback = ""
        try:
            judge_messages = [ChatMessage(role="system", content=judge_prompt)]
            completion, _tier = await call_llm_with_fallback(
                model_router, messages=judge_messages
            )
            judge_scores, judge_feedback = self._parse_judge_response(
                completion.content, task.metrics or DEFAULT_METRICS
            )
        except Exception as e:
            logger.warning("LLM Judge 评分失败 (条目 %s): %s", item.id, e)
            judge_feedback = f"评分失败: {e}"

        # 3. 判断是否通过
        overall_score = judge_scores.get("overall", 0)
        if not isinstance(overall_score, (int, float)):
            overall_score = 0
        passed = float(overall_score) >= DEFAULT_PASS_THRESHOLD

        latency_ms = int((time.monotonic() - start_time) * 1000)

        return EvaluationResult(
            tenant_id=tenant_id,
            task_id=task.id,
            dataset_item_id=item.id,
            agent_output=agent_output,
            judge_scores=judge_scores,
            judge_feedback=judge_feedback,
            passed=passed,
            latency_ms=latency_ms,
        )

    # ===================== 结果查询 =====================

    async def get_task_results(
        self,
        task_id: int,
        *,
        tenant_id: str = "default",
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询评测结果"""
        base = (
            select(EvaluationResult)
            .where(
                EvaluationResult.task_id == task_id,
                EvaluationResult.tenant_id == tenant_id,
            )
            .order_by(EvaluationResult.created_at.desc())
        )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        offset = (page - 1) * size
        rows = (
            await self.session.execute(base.offset(offset).limit(size))
        ).scalars().all()

        return {
            "items": [self._result_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_task_summary(
        self, task_id: int, *, tenant_id: str = "default"
    ) -> Dict[str, Any]:
        """获取评测任务汇总统计

        Returns:
            {"total": N, "completed": N, "avg_score": float, "pass_rate": float,
             "metric_scores": {"accuracy": float, ...}, "avg_latency_ms": float}
        """
        # 如果任务已完成, 直接返回缓存的汇总
        task = await self.get_task(task_id, tenant_id=tenant_id)
        if task is None:
            return {"error": "任务不存在"}

        if task.results_summary:
            return task.results_summary

        # 实时计算
        return await self._compute_summary(
            self.session, task_id, tenant_id=tenant_id
        )

    async def _compute_summary(
        self,
        session: AsyncSession,
        task_id: int,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """计算评测汇总统计"""
        results = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.task_id == task_id,
                    EvaluationResult.tenant_id == tenant_id,
                )
            )
        ).scalars().all()

        if not results:
            return {
                "total": 0,
                "completed": 0,
                "avg_score": 0.0,
                "pass_rate": 0.0,
                "metric_scores": {},
                "avg_latency_ms": 0.0,
            }

        total = len(results)
        passed = sum(1 for r in results if r.passed)

        # 各维度平均分
        metric_scores: Dict[str, List[float]] = {}
        overall_scores: List[float] = []
        latencies: List[int] = []

        for r in results:
            scores = r.judge_scores or {}
            for key, val in scores.items():
                if key == "overall":
                    continue
                if isinstance(val, (int, float)):
                    metric_scores.setdefault(key, []).append(float(val))

            overall = scores.get("overall", 0)
            if isinstance(overall, (int, float)):
                overall_scores.append(float(overall))

            if r.latency_ms:
                latencies.append(r.latency_ms)

        avg_overall = (
            sum(overall_scores) / len(overall_scores) if overall_scores else 0.0
        )
        avg_metric = {
            k: round(sum(v) / len(v), 2) for k, v in metric_scores.items() if v
        }
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        return {
            "total": total,
            "completed": total,
            "avg_score": round(avg_overall, 2),
            "pass_rate": round(passed / total * 100, 2) if total > 0 else 0.0,
            "metric_scores": avg_metric,
            "avg_latency_ms": round(avg_latency, 2),
        }

    # ===================== 辅助方法 =====================

    @staticmethod
    def _extract_text(data: Any) -> str:
        """从 input/expected_output 中提取纯文本

        支持字符串、dict (取 text/query/content 字段)、list 等格式。
        """
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("text", "query", "content", "message", "prompt"):
                if key in data:
                    return str(data[key])
            return json.dumps(data, ensure_ascii=False)
        if isinstance(data, list):
            return json.dumps(data, ensure_ascii=False)
        return str(data)

    @staticmethod
    def _parse_judge_response(
        content: str, metrics: List[str]
    ) -> tuple[Dict[str, Any], str]:
        """解析 LLM Judge 的评分响应

        Args:
            content: LLM 返回的文本。
            metrics: 期望的评测维度。

        Returns:
            (scores, feedback): scores 为各维度评分 dict, feedback 为反馈文本。
        """
        scores: Dict[str, Any] = {}
        feedback = ""

        # 尝试解析 JSON
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                for key in metrics:
                    if key in data:
                        val = data[key]
                        if isinstance(val, (int, float)):
                            scores[key] = max(0, min(100, int(val)))
                if "overall" in data:
                    val = data["overall"]
                    if isinstance(val, (int, float)):
                        scores["overall"] = max(0, min(100, int(val)))
                else:
                    # 自动计算 overall
                    metric_vals = [v for v in scores.values() if isinstance(v, (int, float))]
                    if metric_vals:
                        scores["overall"] = round(sum(metric_vals) / len(metric_vals))
                feedback = str(data.get("feedback", ""))
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败, 尝试提取分数
            feedback = f"评分解析失败: {content[:200]}"
            for metric in metrics:
                import re

                match = re.search(rf'{metric}["\']?\s*[:=]\s*(\d+)', content, re.IGNORECASE)
                if match:
                    scores[metric] = max(0, min(100, int(match.group(1))))

        # 如果没有 overall, 用各维度均值
        if "overall" not in scores:
            metric_vals = [v for v in scores.values() if isinstance(v, (int, float))]
            if metric_vals:
                scores["overall"] = round(sum(metric_vals) / len(metric_vals))
            else:
                scores["overall"] = 0

        return scores, feedback

    # ===================== 序列化辅助 =====================

    @staticmethod
    def _task_to_dict(t: EvaluationTask) -> Dict[str, Any]:
        """EvaluationTask -> dict"""
        return {
            "id": t.id,
            "tenant_id": t.tenant_id,
            "name": t.name,
            "dataset_id": t.dataset_id,
            "judge_model": t.judge_model,
            "judge_prompt_template": t.judge_prompt_template,
            "metrics": t.metrics,
            "status": t.status,
            "progress": t.progress,
            "total_items": t.total_items,
            "completed_items": t.completed_items,
            "results_summary": t.results_summary,
            "created_by": t.created_by,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    @staticmethod
    def _result_to_dict(r: EvaluationResult) -> Dict[str, Any]:
        """EvaluationResult -> dict"""
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "task_id": r.task_id,
            "dataset_item_id": r.dataset_item_id,
            "agent_output": r.agent_output,
            "judge_scores": r.judge_scores,
            "judge_feedback": r.judge_feedback,
            "passed": r.passed,
            "latency_ms": r.latency_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
