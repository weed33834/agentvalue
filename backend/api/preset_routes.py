"""
提示词模板库 + Agent预设 API Router

端点:
- 提示词模板 CRUD:
  - GET    /api/v1/presets/templates              列出公开模板(支持 category 过滤)
  - POST   /api/v1/presets/templates              创建模板(需认证)
  - PUT    /api/v1/presets/templates/{id}         更新模板
  - DELETE /api/v1/presets/templates/{id}         删除模板
  - POST   /api/v1/presets/templates/{id}/instantiate  实例化模板(传入变量值)
- Agent预设 CRUD:
  - GET    /api/v1/presets/agents                 列出公开预设
  - GET    /api/v1/presets/agents/{id}            获取预设详情
  - POST   /api/v1/presets/agents                 创建预设(需认证)
  - PUT    /api/v1/presets/agents/{id}            更新预设
  - DELETE /api/v1/presets/agents/{id}            删除预设
  - POST   /api/v1/presets/agents/{id}/use        使用预设(use_count+1, 返回配置)

对标 LobeChat/Open WebUI 的 Prompt 模板库 + ChatGPT GPTs / Coze Bot 助手市场。

事务边界由路由层控制。内置种子数据在首次访问时惰性插入(幂等)。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db, get_db_session
from models.prompt_template import AgentPreset, PromptTemplate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/presets", tags=["presets"])

# 文本字段长度上限
_MAX_NAME_LENGTH = 128
_MAX_TEXT_LENGTH = 10000
_MAX_CATEGORY_LENGTH = 64

# 模块级种子初始化标志(惰性 seeding, 首次请求触发)
_SEED_INITIALIZED = False


# ---------------- Schemas ----------------


class TemplateVariable(BaseModel):
    """模板变量定义"""

    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=256)


class CreateTemplatePayload(BaseModel):
    """创建提示词模板请求体"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    category: str = Field(default="general", max_length=_MAX_CATEGORY_LENGTH)
    content: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    variables: List[TemplateVariable] = Field(default_factory=list)
    is_public: bool = True


class UpdateTemplatePayload(BaseModel):
    """更新提示词模板请求体"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LENGTH)
    category: Optional[str] = Field(default=None, max_length=_MAX_CATEGORY_LENGTH)
    content: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    variables: Optional[List[TemplateVariable]] = None
    is_public: Optional[bool] = None


class InstantiateTemplatePayload(BaseModel):
    """实例化模板请求体: 传入变量值, 返回替换后的内容"""

    model_config = ConfigDict(extra="forbid")

    variables: Dict[str, str] = Field(default_factory=dict)


class CreateAgentPresetPayload(BaseModel):
    """创建Agent预设请求体"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    description: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    avatar: Optional[str] = Field(default=None, max_length=512)
    system_prompt: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    category: str = Field(default="general", max_length=_MAX_CATEGORY_LENGTH)
    tags: List[str] = Field(default_factory=list, max_length=20)
    model_tier: str = Field(default="L1", max_length=10)
    enabled_tools: List[str] = Field(default_factory=list, max_length=50)
    temperature: int = Field(default=70, ge=0, le=100)
    is_public: bool = True


class UpdateAgentPresetPayload(BaseModel):
    """更新Agent预设请求体"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LENGTH)
    description: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    avatar: Optional[str] = Field(default=None, max_length=512)
    system_prompt: Optional[str] = Field(
        default=None, max_length=_MAX_TEXT_LENGTH
    )
    category: Optional[str] = Field(default=None, max_length=_MAX_CATEGORY_LENGTH)
    tags: Optional[List[str]] = None
    model_tier: Optional[str] = Field(default=None, max_length=10)
    enabled_tools: Optional[List[str]] = None
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
    is_public: Optional[bool] = None


# ---------------- Serialization helpers ----------------


def _serialize_template(t: PromptTemplate) -> Dict[str, Any]:
    """序列化 PromptTemplate 为 dict"""
    return {
        "id": t.id,
        "name": t.name,
        "category": t.category,
        "content": t.content,
        "variables": t.variables or [],
        "is_builtin": t.is_builtin,
        "is_public": t.is_public,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize_preset(p: AgentPreset, include_config: bool = True) -> Dict[str, Any]:
    """序列化 AgentPreset 为 dict

    include_config: True 时返回完整配置(system_prompt/enabled_tools/temperature),
    False 时仅返回摘要(列表场景减少 payload)。
    """
    data: Dict[str, Any] = {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "avatar": p.avatar,
        "category": p.category,
        "tags": p.tags or [],
        "model_tier": p.model_tier,
        "is_builtin": p.is_builtin,
        "is_public": p.is_public,
        "use_count": p.use_count,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
    if include_config:
        data["system_prompt"] = p.system_prompt
        data["enabled_tools"] = p.enabled_tools or []
        data["temperature"] = p.temperature
    return data


def _instantiate_content(content: str, variables: Dict[str, str]) -> str:
    """将模板内容中的 {{variable}} 占位符替换为实际值

    支持的占位符格式: {{name}} 或 {{ name }}(允许前后空格)
    未提供值的变量替换为空串。
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1).strip()
        return str(variables.get(var_name, ""))

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, content)


# ---------------- 内置种子数据 ----------------


def _builtin_templates() -> List[Dict[str, Any]]:
    """内置提示词模板种子数据(对标 LobeChat/Open WebUI 模板库)"""
    return [
        {
            "name": "代码审查",
            "category": "coding",
            "content": (
                "你是一位资深代码审查专家。请审查以下代码:\n\n"
                "## 代码\n```\n{{code}}\n```\n\n"
                "## 审查重点\n{{focus_points}}\n\n"
                "请从以下维度给出审查意见:\n"
                "1. 代码质量与可读性\n"
                "2. 潜在 Bug 与边界条件\n"
                "3. 性能与安全\n"
                "4. 改进建议"
            ),
            "variables": [
                {"name": "code", "description": "待审查的代码片段"},
                {
                    "name": "focus_points",
                    "description": "审查重点(可选)",
                },
            ],
        },
        {
            "name": "周报撰写",
            "category": "writing",
            "content": (
                "请帮我撰写本周工作周报。\n\n"
                "## 本周工作内容\n{{tasks}}\n\n"
                "## 本周成果与亮点\n{{achievements}}\n\n"
                "## 遇到的问题\n{{issues}}\n\n"
                "## 下周计划\n{{next_plan}}\n\n"
                "请用简洁专业的语言整理成结构化周报。"
            ),
            "variables": [
                {"name": "tasks", "description": "本周完成的任务"},
                {"name": "achievements", "description": "本周成果亮点"},
                {"name": "issues", "description": "遇到的问题"},
                {"name": "next_plan", "description": "下周计划"},
            ],
        },
        {
            "name": "绩效面谈",
            "category": "hr",
            "content": (
                "你是一位 HR 专家, 请协助准备绩效面谈。\n\n"
                "## 员工信息\n姓名: {{employee_name}}\n岗位: {{position}}\n\n"
                "## 本期绩效数据\n{{performance_data}}\n\n"
                "## 面谈目标\n{{goal}}\n\n"
                "请给出:\n"
                "1. 面谈开场白\n"
                "2. 关键反馈要点\n"
                "3. 改进建议与发展计划\n"
                "4. 面谈结束语"
            ),
            "variables": [
                {"name": "employee_name", "description": "员工姓名"},
                {"name": "position", "description": "员工岗位"},
                {"name": "performance_data", "description": "本期绩效数据"},
                {"name": "goal", "description": "面谈目标"},
            ],
        },
        {
            "name": "数据分析",
            "category": "analysis",
            "content": (
                "你是一位数据分析专家。请分析以下数据:\n\n"
                "## 数据描述\n{{data_description}}\n\n"
                "## 分析目标\n{{analysis_goal}}\n\n"
                "## 数据\n{{data}}\n\n"
                "请给出:\n"
                "1. 数据概览与质量评估\n"
                "2. 关键发现与趋势\n"
                "3. 统计分析结果\n"
                "4. 结论与建议"
            ),
            "variables": [
                {"name": "data_description", "description": "数据来源与描述"},
                {"name": "analysis_goal", "description": "分析目标"},
                {"name": "data", "description": "原始数据"},
            ],
        },
        {
            "name": "翻译润色",
            "category": "writing",
            "content": (
                "请将以下文本{{action}}:\n\n"
                "## 原文\n{{text}}\n\n"
                "## 要求\n{{requirements}}\n\n"
                "请保持原文语义, 输出{{action}}后的文本。"
            ),
            "variables": [
                {"name": "action", "description": "操作类型: 翻译/润色/改写"},
                {"name": "text", "description": "原文内容"},
                {"name": "requirements", "description": "具体要求(如目标语言)"},
            ],
        },
    ]


def _builtin_presets() -> List[Dict[str, Any]]:
    """内置Agent预设种子数据(对标 ChatGPT GPTs / Coze Bot)"""
    return [
        {
            "name": "代码助手",
            "description": "专业的编程助手,擅长代码编写、调试、审查与解释。",
            "avatar": "💻",
            "system_prompt": (
                "你是一位专业的编程助手。你精通多种编程语言,擅长代码编写、"
                "调试、审查与架构设计。请用简洁准确的语言回答编程问题, "
                "提供可运行的代码示例, 并解释关键思路。"
            ),
            "category": "coding",
            "tags": ["编程", "调试", "代码审查"],
            "model_tier": "L2",
            "enabled_tools": ["code_interpreter", "calculator"],
            "temperature": 30,
        },
        {
            "name": "HR顾问",
            "description": "人力资源专家,擅长绩效管理、招聘面试与员工发展。",
            "avatar": "👥",
            "system_prompt": (
                "你是一位资深人力资源顾问。你精通绩效管理、招聘面试、"
                "员工发展与劳动法规。请以专业、客观的视角回答HR相关问题, "
                "提供可操作的建议。"
            ),
            "category": "hr",
            "tags": ["HR", "绩效", "招聘", "员工发展"],
            "model_tier": "L2",
            "enabled_tools": ["calculator"],
            "temperature": 50,
        },
        {
            "name": "数据分析师",
            "description": "数据分析专家,擅长统计分析、数据可视化与洞察提炼。",
            "avatar": "📊",
            "system_prompt": (
                "你是一位数据分析专家。你精通统计分析、数据可视化与商业洞察。"
                "请用数据驱动的方式回答问题, 提供清晰的分析过程与结论, "
                "必要时使用 code_interpreter 执行计算。"
            ),
            "category": "analysis",
            "tags": ["数据分析", "统计", "可视化"],
            "model_tier": "L2",
            "enabled_tools": ["code_interpreter", "calculator"],
            "temperature": 40,
        },
        {
            "name": "文案写手",
            "description": "创意文案专家,擅长营销文案、内容创作与品牌传播。",
            "avatar": "✍️",
            "system_prompt": (
                "你是一位创意文案专家。你擅长营销文案、内容创作与品牌传播。"
                "请用生动有吸引力的语言创作文案, 注重目标受众与传播效果, "
                "提供多个备选方案。"
            ),
            "category": "writing",
            "tags": ["文案", "营销", "创意"],
            "model_tier": "L1",
            "enabled_tools": [],
            "temperature": 80,
        },
        {
            "name": "技术文档专家",
            "description": "技术文档撰写专家,擅长API文档、用户手册与技术规范。",
            "avatar": "📄",
            "system_prompt": (
                "你是一位技术文档撰写专家。你擅长API文档、用户手册、"
                "技术规范与知识库文章。请用清晰、准确、结构化的语言撰写文档, "
                "包含必要的代码示例与图表说明。"
            ),
            "category": "writing",
            "tags": ["技术文档", "API", "手册"],
            "model_tier": "L1",
            "enabled_tools": [],
            "temperature": 30,
        },
    ]


async def _ensure_seed() -> None:
    """惰性种子初始化: 检测表为空则插入内置模板与预设(幂等)。

    使用模块级 _SEED_INITIALIZED 标志避免每次请求都查 DB。
    首次调用时检查 DB, 后续直接跳过。
    """
    global _SEED_INITIALIZED
    if _SEED_INITIALIZED:
        return

    try:
        async with get_db_session() as session:
            # 检查内置模板是否已存在
            existing_templates = (
                await session.execute(
                    select(PromptTemplate).where(
                        PromptTemplate.is_builtin.is_(True)
                    )
                )
            ).scalars().all()
            if len(existing_templates) == 0:
                for tpl_data in _builtin_templates():
                    tpl = PromptTemplate(
                        name=tpl_data["name"],
                        category=tpl_data["category"],
                        content=tpl_data["content"],
                        variables=tpl_data["variables"],
                        is_builtin=True,
                        is_public=True,
                    )
                    session.add(tpl)
                await session.commit()
                logger.info("已插入 %d 个内置提示词模板", len(_builtin_templates()))

            # 检查内置预设是否已存在
            existing_presets = (
                await session.execute(
                    select(AgentPreset).where(AgentPreset.is_builtin.is_(True))
                )
            ).scalars().all()
            if len(existing_presets) == 0:
                for preset_data in _builtin_presets():
                    preset = AgentPreset(
                        name=preset_data["name"],
                        description=preset_data["description"],
                        avatar=preset_data["avatar"],
                        system_prompt=preset_data["system_prompt"],
                        category=preset_data["category"],
                        tags=preset_data["tags"],
                        model_tier=preset_data["model_tier"],
                        enabled_tools=preset_data["enabled_tools"],
                        temperature=preset_data["temperature"],
                        is_builtin=True,
                        is_public=True,
                    )
                    session.add(preset)
                await session.commit()
                logger.info("已插入 %d 个内置Agent预设", len(_builtin_presets()))
    except Exception as e:
        # 种子初始化失败不阻断主流程, 下次请求会重试
        logger.warning("内置种子数据初始化失败(下次请求重试): %s", e)
        return

    _SEED_INITIALIZED = True


# ---------------- 提示词模板 Endpoints ----------------


@router.get("/templates")
async def list_templates(
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有公开提示词模板(支持 category 过滤)"""
    await _ensure_seed()

    stmt = select(PromptTemplate).where(PromptTemplate.is_public.is_(True))
    if category:
        stmt = stmt.where(PromptTemplate.category == category)
    stmt = stmt.order_by(PromptTemplate.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "items": [_serialize_template(t) for t in rows],
    }


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: CreateTemplatePayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """创建提示词模板(需认证)"""
    tpl = PromptTemplate(
        name=payload.name,
        category=payload.category,
        content=payload.content,
        variables=[v.model_dump() for v in payload.variables],
        is_builtin=False,
        is_public=payload.is_public,
        created_by=int(user_id) if user_id.isdigit() else None,
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return _serialize_template(tpl)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int,
    payload: UpdateTemplatePayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """更新提示词模板"""
    tpl = (
        await session.execute(
            select(PromptTemplate).where(PromptTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在"
        )

    # 内置模板不允许修改
    if tpl.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置模板不可修改",
        )

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates:
        tpl.name = updates["name"]
    if "category" in updates:
        tpl.category = updates["category"]
    if "content" in updates:
        tpl.content = updates["content"]
    if "variables" in updates and updates["variables"] is not None:
        tpl.variables = [v.model_dump() for v in updates["variables"]]
    if "is_public" in updates:
        tpl.is_public = updates["is_public"]

    await session.commit()
    await session.refresh(tpl)
    return _serialize_template(tpl)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """删除提示词模板"""
    tpl = (
        await session.execute(
            select(PromptTemplate).where(PromptTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在"
        )

    if tpl.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置模板不可删除",
        )

    await session.delete(tpl)
    await session.commit()
    return {"deleted": True, "id": template_id}


@router.post("/templates/{template_id}/instantiate")
async def instantiate_template(
    template_id: int,
    payload: InstantiateTemplatePayload,
    session: AsyncSession = Depends(get_db),
):
    """实例化模板: 传入变量值, 返回替换后的内容"""
    tpl = (
        await session.execute(
            select(PromptTemplate).where(PromptTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在"
        )

    instantiated = _instantiate_content(tpl.content, payload.variables)
    return {
        "template_id": tpl.id,
        "template_name": tpl.name,
        "content": instantiated,
        "variables_provided": payload.variables,
    }


# ---------------- Agent预设 Endpoints ----------------


@router.get("/agents")
async def list_agents(
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有公开Agent预设"""
    await _ensure_seed()

    stmt = select(AgentPreset).where(AgentPreset.is_public.is_(True))
    if category:
        stmt = stmt.where(AgentPreset.category == category)
    stmt = stmt.order_by(AgentPreset.use_count.desc(), AgentPreset.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    # 列表场景不返回完整 system_prompt, 减少 payload
    return {
        "total": len(rows),
        "items": [_serialize_preset(p, include_config=False) for p in rows],
    }


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取Agent预设详情(含完整配置)"""
    await _ensure_seed()

    preset = (
        await session.execute(
            select(AgentPreset).where(AgentPreset.id == agent_id)
        )
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="预设不存在"
        )
    return _serialize_preset(preset, include_config=True)


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: CreateAgentPresetPayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """创建Agent预设(需认证)"""
    preset = AgentPreset(
        name=payload.name,
        description=payload.description,
        avatar=payload.avatar,
        system_prompt=payload.system_prompt,
        category=payload.category,
        tags=payload.tags,
        model_tier=payload.model_tier,
        enabled_tools=payload.enabled_tools,
        temperature=payload.temperature,
        is_builtin=False,
        is_public=payload.is_public,
        use_count=0,
        created_by=int(user_id) if user_id.isdigit() else None,
    )
    session.add(preset)
    await session.commit()
    await session.refresh(preset)
    return _serialize_preset(preset, include_config=True)


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: int,
    payload: UpdateAgentPresetPayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """更新Agent预设"""
    preset = (
        await session.execute(
            select(AgentPreset).where(AgentPreset.id == agent_id)
        )
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="预设不存在"
        )

    if preset.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置预设不可修改",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field in (
        "name",
        "description",
        "avatar",
        "system_prompt",
        "category",
        "tags",
        "model_tier",
        "enabled_tools",
        "temperature",
        "is_public",
    ):
        if field in updates and updates[field] is not None:
            setattr(preset, field, updates[field])

    await session.commit()
    await session.refresh(preset)
    return _serialize_preset(preset, include_config=True)


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: int,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """删除Agent预设"""
    preset = (
        await session.execute(
            select(AgentPreset).where(AgentPreset.id == agent_id)
        )
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="预设不存在"
        )

    if preset.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置预设不可删除",
        )

    await session.delete(preset)
    await session.commit()
    return {"deleted": True, "id": agent_id}


@router.post("/agents/{agent_id}/use")
async def use_agent(
    agent_id: int,
    session: AsyncSession = Depends(get_db),
):
    """使用Agent预设: use_count+1, 返回完整配置供前端初始化会话"""
    await _ensure_seed()

    preset = (
        await session.execute(
            select(AgentPreset).where(AgentPreset.id == agent_id)
        )
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="预设不存在"
        )

    # use_count + 1
    preset.use_count = (preset.use_count or 0) + 1
    await session.commit()
    await session.refresh(preset)

    return {
        "preset": _serialize_preset(preset, include_config=True),
        "use_count": preset.use_count,
    }
