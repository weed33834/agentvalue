"""Skill 执行引擎

职责:
1. 加载Skill定义
2. 构建Agent(注入系统提示词+工具)
3. 执行用户输入
4. 验证输出格式
5. 返回结构化结果

对标 Claude Skills / Trae Skills 的运行时:
- Skill = 系统提示词 + 工具配置 + 输入/输出schema 的封装包
- SkillExecutor 负责"实例化"一个 Skill 为临时 Agent 并执行
- 输出若声明了 output_schema, 会尝试 JSON 解析校验
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db_session
from core.providers.base import ChatMessage
from models.skill import Skill

logger = logging.getLogger(__name__)


# 内置技能种子数据(首次启动时惰性写入, 幂等)
_BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "name": "code_review",
        "display_name": "代码审查",
        "description": "对代码片段进行多维审查: 可读性、Bug、性能、安全、改进建议。",
        "category": "coding",
        "version": "1.0.0",
        "system_prompt": (
            "你是代码审查专家。请对用户提交的代码进行系统性审查, 覆盖以下维度:\n"
            "1. 代码质量与可读性(命名/注释/结构)\n"
            "2. 潜在 Bug 与边界条件\n"
            "3. 性能与资源占用\n"
            "4. 安全风险(注入/越权/敏感信息泄露)\n"
            "5. 具体可执行的改进建议\n"
            "请用 Markdown 输出结构化审查报告, 必要时给出修正后的代码片段。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "待审查的代码"},
                "language": {"type": "string", "description": "编程语言"},
            },
            "required": ["code"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "issues": {"type": "array"},
                "suggestions": {"type": "array"},
            },
        },
        "required_tools": ["grep_tool", "read_file"],
        "model_tier": "L0",
        "temperature": 30,
        "tags": ["代码", "审查", "quality"],
    },
    {
        "name": "performance_analysis",
        "display_name": "绩效分析",
        "description": "基于员工历史评估数据生成绩效分析与改进建议。",
        "category": "hr",
        "version": "1.0.0",
        "system_prompt": (
            "你是绩效分析专家。请基于员工的历史评估数据、关键事件与维度得分, "
            "输出客观、可量化的绩效分析, 包括:\n"
            "1. 整体表现概述(对比历史与同组均值)\n"
            "2. 优势维度与待提升维度\n"
            "3. 关键事件归因(正向/负向)\n"
            "4. 下一周期改进建议与发展计划\n"
            "请避免主观情感词汇, 用数据支撑结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "period": {"type": "string"},
            },
            "required": ["employee_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "overall": {"type": "string"},
                "strengths": {"type": "array"},
                "weaknesses": {"type": "array"},
                "actions": {"type": "array"},
            },
        },
        "required_tools": ["employee_history", "company_kb"],
        "model_tier": "L0",
        "temperature": 40,
        "tags": ["绩效", "HR", "分析"],
    },
    {
        "name": "doc_generation",
        "display_name": "文档生成",
        "description": "根据需求生成技术文档(API文档/用户手册/规范)。",
        "category": "writing",
        "version": "1.0.0",
        "system_prompt": (
            "你是技术文档专家。请根据用户输入生成清晰、结构化的技术文档, 要求:\n"
            "1. 标题层级清晰, 含目录与示例代码\n"
            "2. 术语统一, 关键概念首次出现给出定义\n"
            "3. 覆盖背景/使用方式/参数说明/异常处理/最佳实践\n"
            "4. 使用 Markdown 输出, 必要时附表格与图示说明"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "文档主题"},
                "audience": {"type": "string", "description": "目标读者"},
            },
            "required": ["topic"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sections": {"type": "array"},
            },
        },
        "required_tools": ["write_file"],
        "model_tier": "L0",
        "temperature": 30,
        "tags": ["文档", "技术写作", "API"],
    },
    {
        "name": "data_insight",
        "display_name": "数据洞察",
        "description": "对结构化/非结构化数据进行统计分析与洞察提炼。",
        "category": "analysis",
        "version": "1.0.0",
        "system_prompt": (
            "你是数据分析师。请用数据驱动方式回答用户问题, 要求:\n"
            "1. 必要时使用 code_interpreter 执行计算, 不要凭直觉估算\n"
            "2. 输出数据概览、关键发现、趋势、统计结论\n"
            "3. 用清晰的表格或文字描述分布与对比\n"
            "4. 给出可执行的洞察建议, 标注置信度与样本量"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "数据描述或CSV"},
                "question": {"type": "string", "description": "分析目标"},
            },
            "required": ["question"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "findings": {"type": "array"},
                "recommendations": {"type": "array"},
            },
        },
        "required_tools": ["code_interpreter", "web_search"],
        "model_tier": "L0",
        "temperature": 40,
        "tags": ["数据分析", "统计", "洞察"],
    },
]


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中尝试提取 JSON 对象。

    顺序:
    1. 去除 markdown ```json ... ``` 代码块
    2. 直接 json.loads 整段
    3. 贪婪匹配第一个 {...} 块再解析
    解析失败返回 None(不抛异常, 由调用方决定降级行为)。
    """
    if not text:
        return None
    cleaned = text.strip()
    # 去除 markdown 代码块包裹
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json") :]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 贪婪匹配第一个 {...}
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


class SkillExecutor:
    """Skill 执行引擎

    通过 ModelRouter 获取 LLM Provider, 将 Skill 的系统提示词与用户输入
    组装成消息列表调用 LLM, 并按 output_schema 尝试解析输出为 JSON。
    """

    def __init__(self, model_router, settings=None):
        """
        Args:
            model_router: ModelRouter 实例, 提供 get_provider(tier) 方法
            settings: 全局 Settings(可选, 预留用于后续扩展如超时/重试配置)
        """
        self.model_router = model_router
        self.settings = settings

    # ---------------- 核心执行 ----------------

    async def execute(
        self,
        skill: Skill,
        user_input: str,
        context: Optional[dict] = None,
    ) -> dict:
        """执行 Skill

        Args:
            skill: Skill ORM 实例
            user_input: 用户输入文本
            context: 可选上下文(字典), 会以结构化方式拼接到用户消息前

        Returns:
            {
                "output": str,           # LLM 原始文本输出
                "parsed": Optional[dict], # output_schema 存在时尝试解析的 JSON
                "skill_id": int,
                "tokens_used": int,
            }
        """
        try:
            # 1. 构建消息列表: [system_prompt, user_message]
            system_prompt = skill.system_prompt or ""
            user_message = self._build_user_message(user_input, context)
            messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_message),
            ]

            # 2. 获取 LLM provider (按 Skill 指定的档位, 降级到 L0 云端)
            try:
                provider = self.model_router.get_provider(skill.model_tier)
            except Exception:
                provider, _ = await self.model_router.get_provider_with_fallback()

            # 3. 注入 Skill 温度(0-100 -> 0.0-1.0), 仅当 skill.temperature 有效时覆盖
            try:
                temp_value = (
                    int(skill.temperature) if skill.temperature is not None else None
                )
                if temp_value is not None and 0 <= temp_value <= 100:
                    # ProviderConfig 是 dataclass, 直接 mutate 即可;
                    # get_provider 每次返回新实例, 不会污染全局
                    provider.config.temperature = temp_value / 100.0
            except Exception as e:
                logger.debug("注入 skill temperature 失败, 使用 provider 默认: %s", e)

            # 4. 调用 LLM 生成 (不使用 response_format, 某些 API 代理不支持)
            completion = await provider.chat_completion(
                messages=messages,
            )

            output_text = completion.content or ""
            tokens_used = 0
            if completion.usage:
                tokens_used = int(
                    completion.usage.get("total_tokens", 0)
                    or (
                        completion.usage.get("prompt_tokens", 0)
                        + completion.usage.get("completion_tokens", 0)
                    )
                )

            # 6. 如果有 output_schema, 尝试解析输出为 JSON
            parsed: Optional[dict] = None
            if skill.output_schema:
                parsed = _extract_json(output_text)

            return {
                "output": output_text,
                "parsed": parsed,
                "skill_id": skill.id,
                "tokens_used": tokens_used,
            }
        except Exception as e:
            logger.exception(
                "Skill 执行失败 skill_id=%s: %s", getattr(skill, "id", None), e
            )
            return {
                "output": "",
                "parsed": None,
                "skill_id": getattr(skill, "id", None),
                "tokens_used": 0,
                "error": str(e),
            }

    @staticmethod
    def _build_user_message(user_input: str, context: Optional[dict]) -> str:
        """将 context 与 user_input 组装为最终 user 消息文本。

        context 非空时, 以 "## 上下文" 段落前置注入, 便于 LLM 引用。
        """
        if not context:
            return user_input
        try:
            context_str = json.dumps(context, ensure_ascii=False, indent=2, default=str)
        except Exception:
            context_str = str(context)
        return f"## 上下文\n{context_str}\n\n## 用户输入\n{user_input}"

    # ---------------- 数据库加载 ----------------

    async def load_skill(self, skill_id: int) -> Optional[Skill]:
        """从数据库加载 Skill by id"""
        try:
            async with get_db_session() as session:  # type: AsyncSession
                stmt = select(Skill).where(Skill.id == skill_id)
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as e:
            logger.exception("加载 Skill 失败 id=%s: %s", skill_id, e)
            return None

    async def list_skills(
        self,
        category: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Skill]:
        """列出技能(支持 category 过滤与 active 过滤)"""
        try:
            async with get_db_session() as session:  # type: AsyncSession
                stmt = select(Skill)
                if category:
                    stmt = stmt.where(Skill.category == category)
                if active_only:
                    stmt = stmt.where(Skill.is_active.is_(True))
                stmt = stmt.order_by(Skill.use_count.desc(), Skill.id.asc())
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as e:
            logger.exception("列出 Skill 失败: %s", e)
            return []

    # ---------------- 内置种子 ----------------

    async def _seed_builtin_skills(self) -> int:
        """惰性写入内置 Skill 种子数据(幂等)。

        Returns:
            本次新插入的条数(已存在则返回 0)
        """
        try:
            async with get_db_session() as session:  # type: AsyncSession
                # 检查是否已有内置 Skill
                existing = (
                    (
                        await session.execute(
                            select(Skill).where(Skill.is_builtin.is_(True))
                        )
                    )
                    .scalars()
                    .all()
                )
                existing_names = {s.name for s in existing}
                inserted = 0
                for skill_data in _BUILTIN_SKILLS:
                    if skill_data["name"] in existing_names:
                        continue
                    skill = Skill(
                        name=skill_data["name"],
                        display_name=skill_data.get("display_name"),
                        description=skill_data.get("description"),
                        category=skill_data.get("category", "general"),
                        version=skill_data.get("version", "1.0.0"),
                        system_prompt=skill_data["system_prompt"],
                        input_schema=skill_data.get("input_schema", {}),
                        output_schema=skill_data.get("output_schema", {}),
                        required_tools=skill_data.get("required_tools", []),
                        model_tier=skill_data.get("model_tier", "L0"),
                        temperature=skill_data.get("temperature", 70),
                        is_builtin=True,
                        is_public=True,
                        is_active=True,
                        use_count=0,
                        tags=skill_data.get("tags", []),
                        config=skill_data.get("config", {}),
                    )
                    session.add(skill)
                    inserted += 1
                if inserted > 0:
                    await session.commit()
                    logger.info("已插入 %d 个内置 Skill", inserted)
                return inserted
        except Exception as e:
            logger.exception("内置 Skill 种子写入失败: %s", e)
            return 0
