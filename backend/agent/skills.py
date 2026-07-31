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
                "output": str,            # LLM 原始文本输出
                "parsed": Optional[dict], # output_schema 存在时尝试解析的 JSON
                "skill_id": int,
                "tokens_used": int,
                "tool_calls_made": list,   # 仅 ReAct 模式下出现, 记录工具调用
            }

        工具绑定:
        - 若 skill.required_tools 非空且 langchain 可用, 优先用 ReAct Agent 模式执行
          (加载声明的工具, 让 LLM 自主决定调用). 失败时降级到简单 LLM 调用。
        - 若 required_tools 为空或 langchain 不可用, 保持原有简单 LLM 调用方式。
        """
        # 工具绑定: 优先尝试 ReAct Agent 模式
        required_tools = list(skill.required_tools or [])
        if required_tools:
            react_result = await self._execute_with_react(
                skill, user_input, context, required_tools
            )
            if react_result is not None:
                return react_result
            logger.info(
                "Skill %s ReAct 模式不可用/失败, 降级到简单 LLM 调用",
                getattr(skill, "id", None),
            )

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

    # ---------------- ReAct 工具绑定执行 ----------------

    async def _execute_with_react(
        self,
        skill: Skill,
        user_input: str,
        context: Optional[dict],
        required_tools: List[str],
    ) -> Optional[dict]:
        """用 ReAct Agent 模式执行 Skill (带工具调用能力)

        流程:
        1. 惰性导入 langchain/langgraph (不可用则返回 None 触发降级)
        2. 从 agent.react_agent.build_all_tools() 获取全部工具, 过滤出 required_tools
        3. 获取 LLM provider 并适配为 LangChain ChatModel
        4. 用 langgraph.prebuilt.create_react_agent 创建 agent
        5. 执行并提取最终答案 + 工具调用记录

        Returns:
            执行结果 dict (含 tool_calls_made); 任何环节失败返回 None 让调用方降级。
        """
        # 惰性导入, 避免循环依赖 / langchain 未安装时降级
        try:
            from agent.react_agent import build_all_tools, _build_langchain_chat_model
            from langgraph.prebuilt import create_react_agent
        except ImportError:
            logger.debug("langchain/langgraph 不可用, 跳过 ReAct 模式")
            return None

        if self.model_router is None:
            logger.debug("model_router 为 None, 跳过 ReAct 模式")
            return None

        try:
            # 1. 加载所有可用工具, 过滤出 required_tools 中声明的工具
            all_tools = await build_all_tools()
            tool_map: Dict[str, Any] = {
                getattr(t, "name", ""): t for t in all_tools
            }
            selected = [tool_map[n] for n in required_tools if n in tool_map]
            if not selected:
                logger.warning(
                    "Skill 声明的 required_tools %s 均未在 build_all_tools 中找到, 降级到简单 LLM 调用",
                    required_tools,
                )
                return None

            # 2. 获取 LLM provider (按 Skill 指定档位, 降级到 L0)
            try:
                provider = self.model_router.get_provider(skill.model_tier)
            except Exception:
                provider, _ = await self.model_router.get_provider_with_fallback()

            # 3. 注入 Skill 温度 (0-100 -> 0.0-1.0)
            try:
                temp_value = (
                    int(skill.temperature) if skill.temperature is not None else None
                )
                if temp_value is not None and 0 <= temp_value <= 100:
                    provider.config.temperature = temp_value / 100.0
            except Exception as e:
                logger.debug("注入 skill temperature 失败: %s", e)

            # 4. 适配为 LangChain ChatModel (ReAct Agent 需要 BaseChatModel)
            llm = _build_langchain_chat_model(provider)
            if llm is None:
                logger.warning(
                    "无法为 provider %s 构造 LangChain ChatModel, 降级到简单 LLM 调用",
                    type(provider).__name__,
                )
                return None

            # 5. 创建 ReAct Agent
            agent = create_react_agent(
                model=llm,
                tools=selected,
                prompt=skill.system_prompt or "",
            )

            # 6. 执行
            user_message = self._build_user_message(user_input, context)
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": user_message}]}
            )

            # 7. 提取最终答案与工具调用记录
            messages = result.get("messages", [])
            final_answer = ""
            tool_calls_made: List[Dict[str, Any]] = []
            tokens_used = 0

            for msg in reversed(messages):
                content = getattr(msg, "content", "")
                if content and not getattr(msg, "tool_calls", None):
                    final_answer = content
                    break

            for msg in messages:
                tcs = getattr(msg, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        tool_calls_made.append(
                            {
                                "name": tc.get("name", ""),
                                "args": tc.get("args", {}),
                                "id": tc.get("id", ""),
                            }
                        )
                # 累加 usage metadata (LangGraph message 上的 metadata)
                meta = getattr(msg, "usage_metadata", None)
                if isinstance(meta, dict):
                    tokens_used += int(meta.get("total_tokens", 0))

            # 8. 如果有 output_schema, 尝试解析输出为 JSON
            parsed: Optional[dict] = None
            if skill.output_schema:
                parsed = _extract_json(final_answer)

            return {
                "output": final_answer,
                "parsed": parsed,
                "skill_id": getattr(skill, "id", None),
                "tokens_used": tokens_used,
                "tool_calls_made": tool_calls_made,
            }
        except Exception as e:
            logger.warning("ReAct 模式执行失败, 降级到简单 LLM 调用: %s", e)
            return None

    # ---------------- AI 自动生成 Skill ----------------

    async def generate_skill(
        self, description: str, category: str = "general"
    ) -> dict:
        """使用 LLM 根据自然语言描述自动生成 Skill 定义

        构建一个 meta-prompt, 让 LLM 输出 JSON 格式的 skill 定义。
        返回字段: name / display_name / description / category / system_prompt /
        input_schema / output_schema / required_tools / model_tier / temperature / tags

        失败时返回 {"error": "..."}。

        Args:
            description: 自然语言描述 (如 "一个能审查代码质量的技能")
            category: 目标分类, 会注入到 prompt 与最终字段

        Returns:
            Skill 定义 dict (未入库, 由调用方决定是否创建) 或 {"error": ...}
        """
        try:
            if self.model_router is None:
                return {"error": "ModelRouter 不可用, 无法生成 Skill"}

            provider, _tier = await self.model_router.get_provider_with_fallback()

            meta_prompt = (
                "你是 Skill 设计专家。请根据以下自然语言描述, 生成一个结构化的 Skill 定义。\n\n"
                f"## 用户描述\n{description}\n\n"
                f"## 目标分类\n{category}\n\n"
                "## 输出要求\n"
                "请输出一个 JSON 对象, 包含以下字段:\n"
                '- name: 技能唯一标识 (snake_case 英文, 如 "code_review")\n'
                "- display_name: 中文显示名称\n"
                "- description: 一句话技能描述\n"
                '- category: 分类 (使用用户指定的分类, 默认 "general")\n'
                "- system_prompt: 详细的系统提示词 (指导 LLM 如何执行此技能)\n"
                "- input_schema: 输入参数 JSON Schema (含 type/properties/required)\n"
                "- output_schema: 输出格式 JSON Schema (含 type/properties)\n"
                '- required_tools: 需要的工具名列表 (如 ["web_search", "calculator"], 无需工具则为空数组)\n'
                '- model_tier: 推荐模型层级 ("L0"=云端, "L1"-"L3"=本地, 默认 "L0")\n'
                "- temperature: 温度 0-100 (整数, 创造性任务偏高如 60-80, 严谨任务偏低如 20-40)\n"
                "- tags: 标签列表 (字符串数组)\n\n"
                "仅输出 JSON, 不要附加任何解释或 markdown 包裹。"
            )

            messages = [
                ChatMessage(
                    role="system",
                    content="你是 Skill 设计助手, 只输出合法 JSON。",
                ),
                ChatMessage(role="user", content=meta_prompt),
            ]
            completion = await provider.chat_completion(messages=messages)
            output_text = completion.content or ""

            skill_def = _extract_json(output_text)
            if skill_def is None:
                logger.warning("generate_skill LLM 输出无法解析为 JSON: %s", output_text[:200])
                return {
                    "error": "LLM 输出无法解析为 JSON",
                    "raw_output": output_text,
                }

            # 规范化字段 (确保所有声明字段存在, 缺省用合理默认值)
            skill_def.setdefault("name", None)
            skill_def.setdefault("display_name", skill_def.get("name"))
            skill_def.setdefault("description", "")
            skill_def.setdefault("category", category)
            skill_def.setdefault("version", "1.0.0")
            skill_def.setdefault("system_prompt", "")
            skill_def.setdefault("input_schema", {})
            skill_def.setdefault("output_schema", {})
            skill_def.setdefault("required_tools", [])
            skill_def.setdefault("model_tier", "L0")
            skill_def.setdefault("temperature", 70)
            skill_def.setdefault("tags", [])

            if not skill_def.get("name"):
                return {
                    "error": "生成的 Skill 缺少 name 字段",
                    "raw_output": output_text,
                }

            return skill_def
        except Exception as e:
            logger.exception("generate_skill 失败: %s", e)
            return {"error": str(e)}

    # ---------------- 导入 / 导出 ----------------

    def export_skill(self, skill: Skill) -> dict:
        """将 Skill 序列化为可导入的 JSON 格式

        包含所有可移植字段 (不含 id / 时间戳 / use_count 等运行时状态),
        导出结果可直接传给 import_skill / batch_import_skills 导入。
        """
        return {
            "name": skill.name,
            "display_name": skill.display_name,
            "description": skill.description,
            "category": skill.category,
            "version": skill.version,
            "system_prompt": skill.system_prompt,
            "input_schema": skill.input_schema or {},
            "output_schema": skill.output_schema or {},
            "required_tools": skill.required_tools or [],
            "model_tier": skill.model_tier,
            "temperature": skill.temperature,
            "tags": skill.tags or [],
            "config": skill.config or {},
        }

    async def import_skill(
        self, skill_data: dict, overwrite: bool = False
    ) -> dict:
        """从 JSON 数据导入 Skill

        - 如果 name 已存在且 overwrite=False, 跳过并返回 action="skipped"
        - 如果 name 已存在且 overwrite=True, 更新已有记录, action="updated"
        - 如果 name 不存在, 创建新记录, action="created"

        Returns:
            {"imported": int, "skill_id": Optional[int], "action": "created"|"updated"|"skipped"}
        """
        try:
            from sqlalchemy import select
        except ImportError as e:
            return {
                "imported": 0,
                "skill_id": None,
                "action": "skipped",
                "error": f"SQLAlchemy 不可用: {e}",
            }

        name = skill_data.get("name")
        if not name:
            return {
                "imported": 0,
                "skill_id": None,
                "action": "skipped",
                "error": "skill_data 缺少 name 字段",
            }

        try:
            async with get_db_session() as session:  # type: AsyncSession
                existing = (
                    await session.execute(select(Skill).where(Skill.name == name))
                ).scalar_one_or_none()

                if existing is not None:
                    if not overwrite:
                        logger.info(
                            "Skill name=%s 已存在且 overwrite=False, 跳过导入", name
                        )
                        return {
                            "imported": 0,
                            "skill_id": existing.id,
                            "action": "skipped",
                        }
                    # 更新已有记录
                    for field in (
                        "display_name",
                        "description",
                        "category",
                        "version",
                        "system_prompt",
                        "input_schema",
                        "output_schema",
                        "required_tools",
                        "model_tier",
                        "temperature",
                        "tags",
                        "config",
                    ):
                        if field in skill_data and skill_data[field] is not None:
                            setattr(existing, field, skill_data[field])
                    await session.commit()
                    await session.refresh(existing)
                    logger.info("Skill name=%s 已更新 (overwrite=True)", name)
                    return {
                        "imported": 1,
                        "skill_id": existing.id,
                        "action": "updated",
                    }

                # 创建新记录
                skill = Skill(
                    name=name,
                    display_name=skill_data.get("display_name"),
                    description=skill_data.get("description"),
                    category=skill_data.get("category", "general"),
                    version=skill_data.get("version", "1.0.0"),
                    system_prompt=skill_data.get("system_prompt", ""),
                    input_schema=skill_data.get("input_schema", {}),
                    output_schema=skill_data.get("output_schema", {}),
                    required_tools=skill_data.get("required_tools", []),
                    model_tier=skill_data.get("model_tier", "L0"),
                    temperature=skill_data.get("temperature", 70),
                    is_builtin=False,
                    is_public=True,
                    is_active=True,
                    use_count=0,
                    tags=skill_data.get("tags", []),
                    config=skill_data.get("config", {}),
                )
                session.add(skill)
                await session.commit()
                await session.refresh(skill)
                logger.info("Skill name=%s 已创建, id=%s", name, skill.id)
                return {
                    "imported": 1,
                    "skill_id": skill.id,
                    "action": "created",
                }
        except Exception as e:
            logger.exception("import_skill 失败 name=%s: %s", name, e)
            return {
                "imported": 0,
                "skill_id": None,
                "action": "skipped",
                "error": str(e),
            }

    async def batch_import_skills(
        self, skills_data: list, overwrite: bool = False
    ) -> dict:
        """批量导入多个 Skill

        逐条调用 import_skill, 汇总成功/跳过/错误。

        Returns:
            {"imported": int, "skipped": int, "errors": list}
        """
        imported = 0
        skipped = 0
        errors: List[dict] = []

        for idx, item in enumerate(skills_data):
            if not isinstance(item, dict):
                errors.append(
                    {"index": idx, "error": f"非 dict 类型: {type(item).__name__}"}
                )
                skipped += 1
                continue

            result = await self.import_skill(item, overwrite=overwrite)
            if result.get("imported"):
                imported += 1
            else:
                skipped += 1
                err = result.get("error")
                if err:
                    errors.append(
                        {
                            "index": idx,
                            "name": item.get("name"),
                            "error": err,
                        }
                    )

        return {"imported": imported, "skipped": skipped, "errors": errors}

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
