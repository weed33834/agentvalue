"""
Prompt 优化建议服务

对标 Langfuse LLM Playground 交互测试：
- 创建优化任务（支持 improve/simplify/translate/specialize 四种类型）
- 异步执行优化：构建 LLM prompt -> 调用 LLM -> 解析 JSON 结果 -> 存储评分与建议
- 任务 CRUD（全部 tenant_id 过滤）

后台任务使用 asyncio.create_task() 异步执行，不阻塞 API 响应。
后台任务内部通过 AsyncSessionLocal 创建独立数据库会话。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.llm_call import call_llm_with_fallback
from core.providers.base import ChatMessage
from core.tenant_context import tenant_scope
from models.prompt_optimization_models import PromptOptimizationTask

logger = logging.getLogger(__name__)

# 优化提示词模板（按 task_type 区分）
_OPTIMIZATION_PROMPT_TEMPLATES: Dict[str, str] = {
    "improve": """你是一个专业的 Prompt 工程师。请分析以下提示词并给出优化建议。

## 原始提示词
{original_prompt}

## 任务要求
1. 分析原始提示词存在的问题（清晰度、具体性、完整性、有效性）
2. 给出优化后的提示词
3. 列出具体的优化建议（按维度分类）
4. 对优化后的提示词进行质量评分

## 输出要求
请返回 JSON 格式，包含以下字段:
- "optimized_prompt": 优化后的提示词（字符串）
- "suggestions": 优化建议列表，每项含 {{"type": "clarity|specificity|completeness|effectiveness", "comment": "具体建议"}}
- "quality_scores": 质量评分 {{"clarity": 1-10, "specificity": 1-10, "completeness": 1-10, "effectiveness": 1-10}}
- "overall_score": 综合评分（0-10 的浮点数）

示例:
{{"optimized_prompt": "...", "suggestions": [{{"type": "clarity", "comment": "增加明确的角色定义"}}], "quality_scores": {{"clarity": 8, "specificity": 7, "completeness": 9, "effectiveness": 8}}, "overall_score": 8.0}}""",
    "simplify": """你是一个专业的 Prompt 工程师。请简化以下提示词，使其更简洁易读。

## 原始提示词
{original_prompt}

## 任务要求
1. 移除冗余内容，保留核心指令
2. 简化复杂句式，使用清晰直接的表达
3. 给出简化后的提示词
4. 评估简化后提示词的质量

## 输出要求
请返回 JSON 格式，包含以下字段:
- "optimized_prompt": 简化后的提示词（字符串）
- "suggestions": 简化建议列表，每项含 {{"type": "clarity|specificity|completeness|effectiveness", "comment": "具体建议"}}
- "quality_scores": 质量评分 {{"clarity": 1-10, "specificity": 1-10, "completeness": 1-10, "effectiveness": 1-10}}
- "overall_score": 综合评分（0-10 的浮点数）""",
    "translate": """你是一个专业的 Prompt 工程师。请将以下中文提示词翻译为英文，并确保语义准确、表达地道。

## 原始提示词
{original_prompt}

## 任务要求
1. 将提示词翻译为英文
2. 保持原始意图与指令结构
3. 使用地道的英文表达
4. 评估翻译后提示词的质量

## 输出要求
请返回 JSON 格式，包含以下字段:
- "optimized_prompt": 翻译后的英文提示词（字符串）
- "suggestions": 翻译说明列表，每项含 {{"type": "clarity|specificity|completeness|effectiveness", "comment": "具体说明"}}
- "quality_scores": 质量评分 {{"clarity": 1-10, "specificity": 1-10, "completeness": 1-10, "effectiveness": 1-10}}
- "overall_score": 综合评分（0-10 的浮点数）""",
    "specialize": """你是一个专业的 Prompt 工程师，专注于 HR 评估场景。请为 HR 员工评估场景专门优化以下提示词。

## 原始提示词
{original_prompt}

## 任务要求
1. 针对 HR 员工价值量化与成长评估场景优化提示词
2. 确保提示词能引导模型输出结构化的评估结果（维度分数 + 证据 + 总结）
3. 考虑公平性、可解释性与合规性
4. 给出优化后的提示词与具体建议

## 输出要求
请返回 JSON 格式，包含以下字段:
- "optimized_prompt": 针对 HR 评估场景优化后的提示词（字符串）
- "suggestions": 优化建议列表，每项含 {{"type": "clarity|specificity|completeness|effectiveness", "comment": "具体建议"}}
- "quality_scores": 质量评分 {{"clarity": 1-10, "specificity": 1-10, "completeness": 1-10, "effectiveness": 1-10}}
- "overall_score": 综合评分（0-10 的浮点数）""",
}


class PromptOptimizationService:
    """Prompt 优化建议服务

    支持两种使用模式:
    1. 路由层: PromptOptimizationService(session) 配合 get_db 依赖
    2. 内部调用: PromptOptimizationService() 无 session，内部自建会话并自动 commit
    """

    def __init__(self, session: Optional[AsyncSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        self._session = AsyncSessionLocal()
        self._owns_session = True
        return self._session

    async def _commit_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.commit()

    async def _close_if_owned(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ============================================================
    # 任务 CRUD
    # ============================================================

    async def create_task(
        self,
        tenant_id: str,
        original_prompt: str,
        task_type: str = "improve",
        model_used: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建优化任务"""
        if not original_prompt or not original_prompt.strip():
            raise ValueError("原始提示词不能为空")
        if task_type not in ("improve", "simplify", "translate", "specialize"):
            raise ValueError(f"不支持的任务类型: {task_type}")

        session = await self._get_session()
        try:
            task = PromptOptimizationTask(
                tenant_id=tenant_id,
                original_prompt=original_prompt,
                task_type=task_type,
                model_used=model_used,
                status="pending",
            )
            session.add(task)
            await session.flush()
            await self._commit_if_owned()
            logger.info(
                "租户 %s 创建 Prompt 优化任务 %s (类型: %s)",
                tenant_id,
                task.id,
                task_type,
            )
            return self._serialize(task)
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_task(
        self, task_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取优化任务详情"""
        session = await self._get_session()
        try:
            task = await self._get_task_owned(session, task_id, tenant_id)
            return self._serialize(task) if task else None
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def list_tasks(
        self,
        tenant_id: str,
        task_status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询优化任务列表"""
        session = await self._get_session()
        try:
            base = (
                select(PromptOptimizationTask)
                .where(PromptOptimizationTask.tenant_id == tenant_id)
                .order_by(PromptOptimizationTask.created_at.desc())
            )
            if task_status:
                base = base.where(PromptOptimizationTask.status == task_status)

            total = (
                await session.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar() or 0

            offset = (page - 1) * size
            rows = (
                await session.execute(base.offset(offset).limit(size))
            ).scalars().all()

            return {
                "items": [self._serialize(t) for t in rows],
                "total": total,
                "page": page,
                "size": size,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def delete_task(self, task_id: int, tenant_id: str) -> bool:
        """删除优化任务"""
        session = await self._get_session()
        try:
            task = await self._get_task_owned(session, task_id, tenant_id)
            if task is None:
                return False
            await session.delete(task)
            await session.flush()
            await self._commit_if_owned()
            return True
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.rollback()
            raise
        finally:
            if self._owns_session:
                await self._close_if_owned()

    async def get_task_result(
        self, task_id: int, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取优化结果详情（含优化后 prompt、建议、评分）"""
        session = await self._get_session()
        try:
            task = await self._get_task_owned(session, task_id, tenant_id)
            if task is None:
                return None
            return {
                "task_id": task.id,
                "tenant_id": task.tenant_id,
                "status": task.status,
                "task_type": task.task_type,
                "original_prompt": task.original_prompt,
                "optimized_prompt": task.optimized_prompt,
                "suggestions": task.suggestions,
                "quality_scores": task.quality_scores,
                "overall_score": task.overall_score,
                "model_used": task.model_used,
                "created_at": task.created_at.isoformat()
                if task.created_at
                else None,
                "completed_at": task.completed_at.isoformat()
                if task.completed_at
                else None,
                "error": None,
            }
        finally:
            if self._owns_session:
                await self._close_if_owned()

    # ============================================================
    # 任务执行
    # ============================================================

    def run_optimization_background(
        self,
        task_id: int,
        model_router: Any,
        *,
        tenant_id: str = "default",
    ) -> asyncio.Task:
        """启动后台优化任务（不阻塞 API 响应）

        Args:
            task_id: 优化任务 ID
            model_router: ModelRouter 实例（用于 LLM 调用）
            tenant_id: 租户 ID

        Returns:
            asyncio.Task 对象
        """
        return asyncio.create_task(
            self._run_optimization_async(task_id, model_router, tenant_id=tenant_id)
        )

    async def run_optimization(
        self,
        task_id: int,
        model_router: Any,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """同步执行优化（供后台任务调用）

        Args:
            task_id: 优化任务 ID
            model_router: ModelRouter 实例
            tenant_id: 租户 ID

        Returns:
            优化结果 dict
        """
        return await self._run_optimization_async(
            task_id, model_router, tenant_id=tenant_id
        )

    async def _run_optimization_async(
        self,
        task_id: int,
        model_router: Any,
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """后台异步执行 Prompt 优化

        使用独立数据库会话，设置租户上下文，调用 LLM 获取优化建议和评分。
        """
        with tenant_scope(tenant_id):
            async with AsyncSessionLocal() as session:
                try:
                    # 获取任务
                    task = await self._get_task_owned(session, task_id, tenant_id)
                    if task is None:
                        logger.error("优化任务 %s 不存在", task_id)
                        return {"task_id": task_id, "status": "failed", "error": "任务不存在"}

                    # 更新状态为 processing
                    task.status = "processing"
                    await session.commit()

                    # 构建优化 prompt
                    optimization_prompt = self._build_optimization_prompt(
                        task.original_prompt, task.task_type
                    )

                    # 调用 LLM
                    try:
                        completion, tier = await call_llm_with_fallback(
                            model_router,
                            messages=[
                                ChatMessage(role="system", content=optimization_prompt),
                                ChatMessage(
                                    role="user",
                                    content="请根据以上要求分析并优化提示词，返回 JSON 格式结果。",
                                ),
                            ],
                        )
                    except Exception as e:
                        task.status = "failed"
                        task.completed_at = datetime.now(timezone.utc)
                        await session.commit()
                        logger.exception("优化任务 %s LLM 调用失败", task_id)
                        return {
                            "task_id": task_id,
                            "status": "failed",
                            "error": str(e),
                        }

                    # 解析 LLM 返回的 JSON 结果
                    parsed = self._parse_optimization_result(completion.content)
                    if parsed is None:
                        task.status = "failed"
                        task.completed_at = datetime.now(timezone.utc)
                        await session.commit()
                        return {
                            "task_id": task_id,
                            "status": "failed",
                            "error": "LLM 返回结果解析失败",
                        }

                    # 存储优化结果
                    task.optimized_prompt = parsed.get("optimized_prompt", "")
                    task.suggestions = parsed.get("suggestions", [])
                    task.quality_scores = parsed.get("quality_scores", {})
                    task.overall_score = float(parsed.get("overall_score", 0.0))
                    task.model_used = completion.model or tier
                    task.status = "completed"
                    task.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                    logger.info("优化任务 %s 完成, 综合评分: %s", task_id, task.overall_score)
                    return {
                        "task_id": task_id,
                        "status": "completed",
                        "optimized_prompt": task.optimized_prompt,
                        "suggestions": task.suggestions,
                        "quality_scores": task.quality_scores,
                        "overall_score": task.overall_score,
                        "model_used": task.model_used,
                    }

                except Exception as e:
                    # 标记任务失败
                    try:
                        task = await self._get_task_owned(session, task_id, tenant_id)
                        if task is not None:
                            task.status = "failed"
                            task.completed_at = datetime.now(timezone.utc)
                            await session.commit()
                    except Exception:
                        pass
                    logger.exception("优化任务 %s 执行异常", task_id)
                    return {"task_id": task_id, "status": "failed", "error": str(e)}

    # ============================================================
    # Prompt 构建与结果解析
    # ============================================================

    def _build_optimization_prompt(
        self, original_prompt: str, task_type: str
    ) -> str:
        """根据 task_type 构建 LLM 优化 prompt

        Args:
            original_prompt: 原始提示词
            task_type: 任务类型 (improve/simplify/translate/specialize)

        Returns:
            构建好的 LLM 系统提示词
        """
        template = _OPTIMIZATION_PROMPT_TEMPLATES.get(
            task_type, _OPTIMIZATION_PROMPT_TEMPLATES["improve"]
        )
        return template.format(original_prompt=original_prompt)

    def _parse_optimization_result(self, result_text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 结果（容错处理）

        LLM 返回可能包含 markdown 代码块包裹的 JSON，或前后有多余文本，
        此方法尝试多种方式提取有效 JSON。

        Args:
            result_text: LLM 返回的文本

        Returns:
            解析后的 dict，失败返回 None
        """
        if not result_text:
            return None

        # 尝试直接解析
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 代码块
        code_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(code_block_pattern, result_text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        # 尝试提取第一个 { ... } 块
        brace_pattern = r"\{.*\}"
        match = re.search(brace_pattern, result_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("无法解析 LLM 返回的 JSON 结果: %s", result_text[:200])
        return None

    # ============================================================
    # 内部工具
    # ============================================================

    @staticmethod
    async def _get_task_owned(
        session: AsyncSession, task_id: int, tenant_id: str
    ) -> Optional[PromptOptimizationTask]:
        result = await session.execute(
            select(PromptOptimizationTask).where(
                PromptOptimizationTask.id == task_id,
                PromptOptimizationTask.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize(task: PromptOptimizationTask) -> Dict[str, Any]:
        return {
            "id": task.id,
            "tenant_id": task.tenant_id,
            "original_prompt": task.original_prompt,
            "optimized_prompt": task.optimized_prompt,
            "task_type": task.task_type,
            "model_used": task.model_used,
            "suggestions": task.suggestions,
            "quality_scores": task.quality_scores,
            "overall_score": task.overall_score,
            "status": task.status,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat()
            if task.completed_at
            else None,
        }
