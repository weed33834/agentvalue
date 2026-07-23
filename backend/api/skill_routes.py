"""
Skill 技能模块 API Router

端点:
- GET    /api/v1/skills                 列出所有公开技能(支持 category 过滤)
- GET    /api/v1/skills/builtin         列出内置技能
- GET    /api/v1/skills/{id}            获取技能详情
- POST   /api/v1/skills                 创建技能(需认证)
- PUT    /api/v1/skills/{id}            更新技能
- DELETE /api/v1/skills/{id}            删除技能
- POST   /api/v1/skills/{id}/execute    执行技能(传入 input + 可选 context)
- POST   /api/v1/skills/{id}/use        标记使用(use_count+1)

对标 Claude Skills / Trae Skills 的可复用技能模块。
事务边界由路由层控制。内置种子数据在首次访问时惰性插入(幂等)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import get_current_user_id
from core.database import get_db, get_db_session
from models.skill import Skill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

# 文本字段长度上限
_MAX_NAME_LENGTH = 128
_MAX_DISPLAY_NAME_LENGTH = 256
_MAX_TEXT_LENGTH = 10000
_MAX_CATEGORY_LENGTH = 64
_MAX_VERSION_LENGTH = 32

# 模块级种子初始化标志(惰性 seeding, 首次请求触发)
_SEED_INITIALIZED = False


# ---------------- Schemas ----------------


class CreateSkillPayload(BaseModel):
    """创建技能请求体"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    display_name: Optional[str] = Field(
        default=None, max_length=_MAX_DISPLAY_NAME_LENGTH
    )
    description: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    category: str = Field(default="general", max_length=_MAX_CATEGORY_LENGTH)
    version: str = Field(default="1.0.0", max_length=_MAX_VERSION_LENGTH)
    system_prompt: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    required_tools: List[str] = Field(default_factory=list, max_length=50)
    model_tier: str = Field(default="L1", max_length=10)
    temperature: int = Field(default=70, ge=0, le=100)
    is_public: bool = True
    is_active: bool = True
    tags: List[str] = Field(default_factory=list, max_length=20)
    config: Dict[str, Any] = Field(default_factory=dict)


class UpdateSkillPayload(BaseModel):
    """更新技能请求体"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LENGTH)
    display_name: Optional[str] = Field(
        default=None, max_length=_MAX_DISPLAY_NAME_LENGTH
    )
    description: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    category: Optional[str] = Field(default=None, max_length=_MAX_CATEGORY_LENGTH)
    version: Optional[str] = Field(default=None, max_length=_MAX_VERSION_LENGTH)
    system_prompt: Optional[str] = Field(default=None, max_length=_MAX_TEXT_LENGTH)
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    required_tools: Optional[List[str]] = None
    model_tier: Optional[str] = Field(default=None, max_length=10)
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
    is_public: Optional[bool] = None
    is_active: Optional[bool] = None
    tags: Optional[List[str]] = None
    config: Optional[Dict[str, Any]] = None


class ExecuteSkillPayload(BaseModel):
    """执行技能请求体"""

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    context: Optional[Dict[str, Any]] = None


# ---------------- Serialization helpers ----------------


def _serialize_skill(s: Skill, include_prompt: bool = True) -> Dict[str, Any]:
    """序列化 Skill 为 dict

    include_prompt: True 时返回完整 system_prompt/input_schema/output_schema/required_tools,
    False 时仅返回摘要(列表场景减少 payload)。
    """
    data: Dict[str, Any] = {
        "id": s.id,
        "name": s.name,
        "display_name": s.display_name,
        "description": s.description,
        "category": s.category,
        "version": s.version,
        "model_tier": s.model_tier,
        "temperature": s.temperature,
        "is_builtin": s.is_builtin,
        "is_public": s.is_public,
        "is_active": s.is_active,
        "use_count": s.use_count,
        "tags": s.tags or [],
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if include_prompt:
        data["system_prompt"] = s.system_prompt
        data["input_schema"] = s.input_schema or {}
        data["output_schema"] = s.output_schema or {}
        data["required_tools"] = s.required_tools or []
        data["config"] = s.config or {}
    return data


# ---------------- 内置种子初始化 ----------------


async def _ensure_seed() -> None:
    """惰性种子初始化: 检测无内置 Skill 则写入(幂等)。

    使用模块级 _SEED_INITIALIZED 标志避免每次请求都查 DB。
    """
    global _SEED_INITIALIZED
    if _SEED_INITIALIZED:
        return

    try:
        from agent.skills import SkillExecutor

        # 复用 SkillExecutor 的种子写入逻辑(内部已做幂等检查)
        # model_router 可为 None(种子写入不依赖 LLM)
        executor = SkillExecutor(model_router=None, settings=None)
        await executor._seed_builtin_skills()
    except Exception as e:
        logger.warning("内置 Skill 种子初始化失败(下次请求重试): %s", e)
        return

    _SEED_INITIALIZED = True


# ---------------- 模型路由获取 ----------------


def _get_executor(request: Request):
    """从 app_state 获取 SkillExecutor; 不可用时降级构造临时实例。"""
    try:
        app_state = request.app.state.app_state
        from agent.skills import SkillExecutor

        return SkillExecutor(
            model_router=app_state.model_router,
            settings=app_state.settings,
        )
    except Exception:
        # app_state 不可用时仍允许调用(无 LLM 执行能力),但 execute 会失败
        from agent.skills import SkillExecutor

        return SkillExecutor(model_router=None, settings=None)


# ---------------- Endpoints ----------------


@router.get("")
@router.get("/")
async def list_skills(
    category: Optional[str] = Query(default=None, description="按分类过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有公开技能(支持 category 过滤)"""
    await _ensure_seed()

    stmt = select(Skill).where(Skill.is_public.is_(True))
    if category:
        stmt = stmt.where(Skill.category == category)
    stmt = stmt.order_by(Skill.use_count.desc(), Skill.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "items": [_serialize_skill(s, include_prompt=False) for s in rows],
    }


@router.get("/builtin")
async def list_builtin_skills(
    session: AsyncSession = Depends(get_db),
):
    """列出内置技能"""
    await _ensure_seed()

    stmt = select(Skill).where(Skill.is_builtin.is_(True)).order_by(Skill.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "items": [_serialize_skill(s, include_prompt=False) for s in rows],
    }


@router.get("/{skill_id}")
async def get_skill(
    skill_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取技能详情(含完整 system_prompt)"""
    await _ensure_seed()

    skill = (
        await session.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="技能不存在")
    return _serialize_skill(skill, include_prompt=True)


@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: CreateSkillPayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """创建技能(需认证)"""
    skill = Skill(
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        category=payload.category,
        version=payload.version,
        system_prompt=payload.system_prompt,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        required_tools=payload.required_tools,
        model_tier=payload.model_tier,
        temperature=payload.temperature,
        is_builtin=False,
        is_public=payload.is_public,
        is_active=payload.is_active,
        use_count=0,
        tags=payload.tags,
        config=payload.config,
        created_by=int(user_id) if user_id.isdigit() else None,
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    return _serialize_skill(skill, include_prompt=True)


@router.put("/{skill_id}")
async def update_skill(
    skill_id: int,
    payload: UpdateSkillPayload,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """更新技能"""
    skill = (
        await session.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="技能不存在")

    if skill.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置技能不可修改",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field in (
        "name",
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
        "is_public",
        "is_active",
        "tags",
        "config",
    ):
        if field in updates and updates[field] is not None:
            setattr(skill, field, updates[field])

    await session.commit()
    await session.refresh(skill)
    return _serialize_skill(skill, include_prompt=True)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: int,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """删除技能"""
    skill = (
        await session.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="技能不存在")

    if skill.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置技能不可删除",
        )

    await session.delete(skill)
    await session.commit()
    return {"deleted": True, "id": skill_id}


@router.post("/{skill_id}/execute")
async def execute_skill(
    skill_id: int,
    payload: ExecuteSkillPayload,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """执行技能

    请求体: {"input": str, "context": Optional[dict]}
    返回: {"output": str, "parsed": Optional[dict], "skill_id": int, "tokens_used": int}
    """
    await _ensure_seed()

    skill = (
        await session.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="技能不存在")
    if not skill.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="技能未激活, 无法执行",
        )

    executor = _get_executor(request)
    if executor.model_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ModelRouter 不可用, 无法执行技能",
        )

    try:
        result = await executor.execute(
            skill=skill,
            user_input=payload.input,
            context=payload.context,
        )
    except Exception as e:
        logger.exception("技能执行失败 skill_id=%s: %s", skill_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"技能执行失败: {e}",
        )

    # 执行成功后 use_count + 1 (best-effort, 失败不影响结果返回)
    try:
        skill.use_count = (skill.use_count or 0) + 1
        await session.commit()
    except Exception as e:
        logger.warning("更新 use_count 失败: %s", e)

    return result


@router.post("/{skill_id}/use")
async def use_skill(
    skill_id: int,
    session: AsyncSession = Depends(get_db),
):
    """标记技能使用(use_count+1), 返回技能摘要"""
    await _ensure_seed()

    skill = (
        await session.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="技能不存在")

    skill.use_count = (skill.use_count or 0) + 1
    await session.commit()
    await session.refresh(skill)

    return {
        "skill": _serialize_skill(skill, include_prompt=False),
        "use_count": skill.use_count,
    }
