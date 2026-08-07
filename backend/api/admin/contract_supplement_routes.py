"""契约补齐 Admin API (B 类缺口集中实现)

本模块集中补齐前端已调用、但后端此前缺失的 11 个 Admin 端点。
这些端点分散在多个业务前缀下 (gray-release / api-health / graph-rag /
sensitive-words / tool-config / nl2sql / budgets / annotations / alerts /
scheduler)，因此本 router **不设置 prefix**，每条路由使用完整路径声明。

设计约束 (见 docs/API-CONTRACT-REPAIR-DESIGN.md §4.3):
1. 只新增路由，绝不修改既有路由 —— 零回归风险。
2. 数据层全部复用既有 Service / Model，不重复实现业务逻辑。
3. 本 router 必须在 main.py 中 **最后挂载**，保证既有的静态路径
   (如 /budgets/status、/alerts/stats) 先于本模块的动态路径
   (如 /budgets/{budget_id}、/alerts/{alert_id}) 完成匹配。
4. 统一 Role.ADMIN 鉴权 + 多租户上下文 + 写操作审计日志。

完整端点 (11 个):
- GET  /api/v1/admin/gray-release/releases/{release_id}/stats  灰度发布统计
- GET  /api/v1/admin/api-health/stats                          API 健康总览
- GET  /api/v1/admin/graph-rag/tasks/{task_id}/visualize       任务级图谱可视化
- PUT  /api/v1/admin/sensitive-words/{word_id}                 更新敏感词
- POST /api/v1/admin/sensitive-words/{word_id}/review          敏感词审核
- POST /api/v1/admin/tool-config/{tool_name}/reset             重置工具超时
- POST /api/v1/admin/nl2sql/schemas/{schema_id}/refresh        刷新表结构
- GET  /api/v1/admin/budgets/{budget_id}                       预算详情
- GET  /api/v1/admin/annotations/tasks/{task_id}/export        标注结果导出
- GET  /api/v1/admin/alerts/{alert_id}                         告警详情
- GET  /api/v1/admin/scheduler/tasks/{task_id}                 定时任务详情
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.scheduler import _get_scheduler
from api.admin.tool_config_routes import _get_tool_registry
from api.deps import get_audit_service
from auth.rbac import Role, get_current_user_id, require_role
from core.database import get_db
from core.tenant_context import get_current_tenant
from models.knowledge_graph_models import (
    KnowledgeGraphEntity,
    KnowledgeGraphRelation,
)
from models.quota_models import BudgetAlert
from models.sensitive_word import SensitiveWord
from services.alert_service import AlertService
from services.annotation_service import AnnotationService
from services.api_health_service import ApiHealthService
from services.budget_service import BudgetService
from services.graph_rag_service import GraphRAGService
from services.gray_release_service import GrayReleaseService
from services.nl2sql_service import NL2SQLService
from services.sensitive_word_service import SensitiveWordService

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# 敏感词字段合法取值 (与 sensitive_word_service.add_word 保持一致)
_WORD_CATEGORIES = {"politics", "porn", "violence", "ad", "spam", "custom"}
_WORD_SEVERITIES = {"low", "medium", "high"}
_WORD_ACTIONS = {"block", "replace", "mask"}
# 可视化默认节点上限 (防止大图拖垮前端渲染)
_VIZ_NODE_LIMIT = 200


# ============================================================
# Schemas
# ============================================================


class SensitiveWordUpdate(BaseModel):
    """更新敏感词请求 (所有字段可选，至少提供一个)"""

    model_config = ConfigDict(extra="forbid")

    word: Optional[str] = Field(
        default=None, min_length=1, max_length=256, description="敏感词文本"
    )
    category: Optional[str] = Field(default=None, description="分类")
    severity: Optional[str] = Field(default=None, description="严重程度")
    action: Optional[str] = Field(default=None, description="处理动作")
    replacement: Optional[str] = Field(
        default=None, max_length=64, description="替换文本 (action=replace 时生效)"
    )
    is_active: Optional[bool] = Field(default=None, description="是否启用")


class SensitiveWordReview(BaseModel):
    """敏感词审核请求

    审核结论落到 is_active 字段:
    - approve -> is_active=True  (词条生效, 参与文本检测)
    - reject  -> is_active=False (词条保留但不生效, 便于追溯)
    审核人 / 审核意见写入审计日志 (审计表已有完整字段, 无需为此加表结构)。
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., description="审核结论: approve / reject")
    comment: Optional[str] = Field(
        default=None, max_length=512, description="审核意见"
    )


# ============================================================
# 工具函数
# ============================================================


def _iso(value: Optional[datetime]) -> Optional[str]:
    """datetime -> ISO 8601 字符串 (None 安全)"""
    return value.isoformat() if value else None


def _duration_seconds(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """计算时长 (秒)。start 为空返回 None; end 为空取当前时间。"""
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    finish = end or datetime.now(timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    return round((finish - start).total_seconds(), 3)


def _doc_id_set(raw: Any) -> set:
    """把 source_docs / document_ids 归一化成 str 集合

    历史数据里这两个 JSON 字段可能是 list / dict / None，统一容错处理。
    """
    if raw is None:
        return set()
    if isinstance(raw, dict):
        raw = raw.get("ids", list(raw.values()))
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw}
    return {str(raw)}


# ============================================================
# 1. 灰度发布统计
# ============================================================


@router.get(
    "/api/v1/admin/gray-release/releases/{release_id}/stats",
    response_model=Dict[str, Any],
    tags=["admin-gray-release"],
    summary="灰度发布统计",
)
async def gray_release_stats(
    release_id: int,
    session: AsyncSession = Depends(get_db),
):
    """灰度发布统计

    返回流量切分、阶段进度、时间线三部分:
    - traffic: 灰度/基线流量占比
    - progress: 按发布类型计算的阶段进度 (rolling 分步、canary/blue_green 二态)
    - timeline: 创建/启动/结束时间与各阶段耗时
    """
    tenant_id = get_current_tenant()
    service = GrayReleaseService(session)
    release = await service.get_release(release_id, tenant_id=tenant_id)
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"灰度发布 {release_id} 不存在",
        )

    config = release.config or {}
    traffic = int(release.traffic_percentage or 0)

    # 阶段进度: rolling 用 steps 数组定位当前步骤, 其余类型按流量二态判断
    progress: Dict[str, Any] = {"release_type": release.release_type}
    if release.release_type == "rolling":
        steps = config.get("steps") or []
        steps = [int(s) for s in steps if isinstance(s, (int, float, str)) and str(s).lstrip("-").isdigit()]
        steps = sorted(set(steps))
        current_index = -1
        for idx, s in enumerate(steps):
            if traffic >= s:
                current_index = idx
        progress.update(
            {
                "steps": steps,
                "current_step": traffic,
                "current_step_index": current_index,
                "total_steps": len(steps),
                "remaining_steps": [s for s in steps if s > traffic],
                "percent_complete": (
                    round((current_index + 1) / len(steps) * 100, 2) if steps else None
                ),
            }
        )
    elif release.release_type == "blue_green":
        progress.update(
            {
                "blue_version": config.get("blue_version"),
                "green_version": config.get("green_version"),
                "current": config.get("current", "blue"),
                "percent_complete": 100.0 if release.status == "completed" else 0.0,
            }
        )
    else:  # canary
        progress.update(
            {
                "baseline_version": config.get("baseline_version"),
                "percent_complete": float(traffic),
            }
        )

    return {
        "release_id": release.id,
        "tenant_id": release.tenant_id,
        "name": release.name,
        "agent_id": release.agent_id,
        "version_id": release.version_id,
        "status": release.status,
        "traffic": {
            "gray_percentage": traffic,
            "baseline_percentage": 100 - traffic,
        },
        "progress": progress,
        "timeline": {
            "created_at": _iso(release.created_at),
            "started_at": _iso(release.started_at),
            "completed_at": _iso(release.completed_at),
            "updated_at": _iso(release.updated_at),
            "running_seconds": _duration_seconds(
                release.started_at, release.completed_at
            ),
            "age_seconds": _duration_seconds(release.created_at, None),
        },
    }


# ============================================================
# 2. API 健康总览
# ============================================================


@router.get(
    "/api/v1/admin/api-health/stats",
    response_model=Dict[str, Any],
    tags=["admin-api-health"],
    summary="API 健康总览",
)
async def api_health_stats(
    session: AsyncSession = Depends(get_db),
    window_minutes: int = Query(
        default=5, ge=1, le=1440, description="统计窗口（分钟）"
    ),
    top_n: int = Query(default=5, ge=1, le=50, description="返回的 TopN 端点数量"),
):
    """API 健康总览 (端点聚合 + SLO 概览)

    在 list_endpoints / get_all_slo_status 之上做二次聚合:
    总请求数、总错误数、加权平均延迟、最慢端点 TopN、错误率最高端点 TopN、
    以及 SLO 达成/违约计数。
    """
    tenant_id = get_current_tenant()
    service = ApiHealthService(session)
    endpoints: List[Dict[str, Any]] = await service.list_endpoints(
        tenant_id, window_minutes=window_minutes
    )

    total_requests = sum(int(e.get("request_count") or 0) for e in endpoints)
    total_errors = sum(int(e.get("error_count") or 0) for e in endpoints)
    # 按请求数加权的平均延迟, 避免低流量端点拉偏整体均值
    weighted_latency = sum(
        float(e.get("avg_latency_ms") or 0.0) * int(e.get("request_count") or 0)
        for e in endpoints
    )
    avg_latency = round(weighted_latency / total_requests, 4) if total_requests else 0.0
    max_latency = max(
        (float(e.get("max_latency_ms") or 0.0) for e in endpoints), default=0.0
    )

    slowest = sorted(
        endpoints, key=lambda e: float(e.get("avg_latency_ms") or 0.0), reverse=True
    )[:top_n]
    most_errors = sorted(
        endpoints, key=lambda e: float(e.get("error_rate") or 0.0), reverse=True
    )
    most_errors = [e for e in most_errors if int(e.get("error_count") or 0) > 0][:top_n]

    try:
        slo_overview = await service.get_all_slo_status(tenant_id)
    except Exception as exc:  # SLO 计算失败不应让总览整体 500
        logger.warning("SLO 概览计算失败: %s", exc)
        slo_overview = {"total": 0, "achieved": 0, "violated": 0, "items": []}

    return {
        "tenant_id": tenant_id,
        "window_minutes": window_minutes,
        "summary": {
            "endpoint_count": len(endpoints),
            "total_requests": total_requests,
            "total_errors": total_errors,
            "error_rate": (
                round(total_errors / total_requests, 6) if total_requests else 0.0
            ),
            "avg_latency_ms": avg_latency,
            "max_latency_ms": round(max_latency, 4),
        },
        "slo": {
            "total": slo_overview.get("total", 0),
            "achieved": slo_overview.get("achieved", 0),
            "violated": slo_overview.get("violated", 0),
        },
        "slowest_endpoints": slowest,
        "error_prone_endpoints": most_errors,
    }


# ============================================================
# 3. 任务级图谱可视化
# ============================================================


@router.get(
    "/api/v1/admin/graph-rag/tasks/{task_id}/visualize",
    response_model=Dict[str, Any],
    tags=["admin-graph-rag"],
    summary="任务级图谱可视化",
)
async def graph_rag_task_visualize(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    limit: int = Query(
        default=_VIZ_NODE_LIMIT, ge=1, le=1000, description="节点数量上限"
    ),
):
    """任务级图谱可视化数据 (nodes + edges)

    与既有 GET /visualization/{entity_id} (以单个实体为中心 BFS) 互补:
    本端点以「抽取任务」为范围，返回该任务产出的实体子图。

    实体与任务的关联通过 source_docs ∩ task.document_ids 判定
    (实体表本身不冗余 task_id，避免同名实体跨任务合并时的归属歧义)。
    任务未记录 document_ids 时，退化为返回租户内最近更新的实体。
    """
    tenant_id = get_current_tenant()
    service = GraphRAGService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"抽取任务 {task_id} 不存在",
        )

    doc_ids = _doc_id_set(task.document_ids)

    rows = (
        (
            await session.execute(
                select(KnowledgeGraphEntity)
                .where(KnowledgeGraphEntity.tenant_id == tenant_id)
                .order_by(KnowledgeGraphEntity.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )

    if doc_ids:
        entities = [e for e in rows if _doc_id_set(e.source_docs) & doc_ids]
        scope = "task_documents"
    else:
        entities = list(rows)
        scope = "tenant_fallback"
    truncated = len(entities) > limit
    entities = entities[:limit]

    entity_ids = {e.id for e in entities}
    edges: List[Dict[str, Any]] = []
    if entity_ids:
        relations = (
            (
                await session.execute(
                    select(KnowledgeGraphRelation).where(
                        KnowledgeGraphRelation.tenant_id == tenant_id,
                        KnowledgeGraphRelation.source_entity_id.in_(entity_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        # 只保留两端都在子图内的边, 避免前端出现悬挂边
        edges = [
            {
                "id": r.id,
                "source": r.source_entity_id,
                "target": r.target_entity_id,
                "relation_type": r.relation_type,
                "weight": r.weight,
                "properties": r.properties or {},
            }
            for r in relations
            if r.target_entity_id in entity_ids
        ]

    nodes = [
        {
            "id": e.id,
            "name": e.name,
            "entity_type": e.entity_type,
            "description": e.description,
            "properties": e.properties or {},
        }
        for e in entities
    ]

    return {
        "task_id": task.id,
        "task_name": task.name,
        "task_status": task.status,
        "scope": scope,
        "truncated": truncated,
        "limit": limit,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


# ============================================================
# 4-5. 敏感词更新 / 审核
# ============================================================


async def _get_word_or_404(
    session: AsyncSession, word_id: int, tenant_id: str
) -> SensitiveWord:
    """按 id + tenant 取敏感词, 不存在则 404 (防跨租户访问)"""
    word = (
        await session.execute(
            select(SensitiveWord).where(
                SensitiveWord.id == word_id,
                SensitiveWord.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if word is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"敏感词 {word_id} 不存在"
        )
    return word


def _word_to_dict(w: SensitiveWord) -> Dict[str, Any]:
    """敏感词序列化 (与 sensitive_word_routes 列表结构保持一致)"""
    return {
        "id": w.id,
        "tenant_id": w.tenant_id,
        "word": w.word,
        "category": w.category,
        "severity": w.severity,
        "action": w.action,
        "replacement": w.replacement,
        "is_active": w.is_active,
        "created_by": w.created_by,
        "created_at": _iso(w.created_at),
    }


@router.put(
    "/api/v1/admin/sensitive-words/{word_id}",
    response_model=Dict[str, Any],
    tags=["admin-sensitive-words"],
    summary="更新敏感词",
)
async def update_sensitive_word(
    word_id: int,
    payload: SensitiveWordUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service=Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """更新敏感词

    AC 自动机为 Service 实例级懒加载缓存 (每请求重建)，
    因此本次更新在下一次 check/filter 调用时自动生效，无需额外失效处理。
    """
    tenant_id = get_current_tenant()
    word = await _get_word_or_404(session, word_id, tenant_id)

    changed: Dict[str, Any] = {}
    if payload.category is not None:
        if payload.category not in _WORD_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法分类: {payload.category}，可选: {sorted(_WORD_CATEGORIES)}",
            )
        word.category = payload.category
        changed["category"] = payload.category
    if payload.severity is not None:
        if payload.severity not in _WORD_SEVERITIES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法严重程度: {payload.severity}，可选: {sorted(_WORD_SEVERITIES)}",
            )
        word.severity = payload.severity
        changed["severity"] = payload.severity
    if payload.action is not None:
        if payload.action not in _WORD_ACTIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法动作: {payload.action}，可选: {sorted(_WORD_ACTIONS)}",
            )
        word.action = payload.action
        changed["action"] = payload.action
    if payload.word is not None:
        new_word = payload.word.strip()
        if not new_word:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="敏感词不能为空",
            )
        if new_word != word.word:
            # 同租户内敏感词去重
            dup = (
                await session.execute(
                    select(SensitiveWord.id).where(
                        SensitiveWord.tenant_id == tenant_id,
                        SensitiveWord.word == new_word,
                        SensitiveWord.id != word_id,
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"敏感词 '{new_word}' 已存在 (id={dup})",
                )
            word.word = new_word
            changed["word"] = new_word
    if payload.replacement is not None:
        word.replacement = payload.replacement
        changed["replacement"] = payload.replacement
    if payload.is_active is not None:
        word.is_active = payload.is_active
        changed["is_active"] = payload.is_active

    if not changed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未提供任何更新字段"
        )

    await session.flush()
    await audit_service.log(
        actor_id=current_user_id,
        action="update_sensitive_word",
        details={"word_id": word_id, "changed": changed},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(word)
    return _word_to_dict(word)


@router.post(
    "/api/v1/admin/sensitive-words/{word_id}/review",
    response_model=Dict[str, Any],
    tags=["admin-sensitive-words"],
    summary="敏感词审核",
)
async def review_sensitive_word(
    word_id: int,
    payload: SensitiveWordReview,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service=Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """敏感词审核 (approve 生效 / reject 停用)

    审核人与审核意见写入审计日志，可通过审计查询接口按 action=review_sensitive_word 追溯。
    """
    decision = payload.decision.strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="decision 只能是 approve 或 reject",
        )

    tenant_id = get_current_tenant()
    word = await _get_word_or_404(session, word_id, tenant_id)

    word.is_active = decision == "approve"
    await session.flush()

    reviewed_at = datetime.now(timezone.utc).isoformat()
    await audit_service.log(
        actor_id=current_user_id,
        action="review_sensitive_word",
        details={
            "word_id": word_id,
            "word": word.word,
            "decision": decision,
            "comment": payload.comment,
            "reviewed_at": reviewed_at,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()
    await session.refresh(word)

    return {
        "word": _word_to_dict(word),
        "review": {
            "decision": decision,
            "comment": payload.comment,
            "reviewer_id": current_user_id,
            "reviewed_at": reviewed_at,
        },
    }


# ============================================================
# 6. 重置工具超时
# ============================================================


@router.post(
    "/api/v1/admin/tool-config/{tool_name}/reset",
    response_model=Dict[str, Any],
    tags=["admin-tool-config"],
    summary="重置工具超时为默认值",
)
async def reset_tool_timeout(
    tool_name: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service=Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """重置工具超时

    移除自定义超时覆盖，回落到 ToolRegistry 的默认策略
    (bash/command 类 30s，其余 60s)。重置后立即生效。
    """
    tool_registry = _get_tool_registry(request)
    if tool_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ToolRegistry 尚未初始化, 无法重置超时",
        )

    previous = tool_registry.tool_timeouts.pop(tool_name, None)
    effective = tool_registry.get_tool_timeout(tool_name)

    await audit_service.log(
        actor_id=current_user_id,
        action="reset_tool_timeout",
        details={
            "tool_name": tool_name,
            "previous_timeout": previous,
            "effective_timeout": effective,
        },
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    return {
        "tool_name": tool_name,
        "previous_timeout": previous,
        "timeout": effective,
        "had_override": previous is not None,
        "message": (
            f"工具 {tool_name} 已重置为默认超时 {effective} 秒"
            if previous is not None
            else f"工具 {tool_name} 未设置自定义超时, 当前默认 {effective} 秒"
        ),
    }


# ============================================================
# 7. 刷新表结构
# ============================================================


def _introspect_columns(sync_session, table_name: str) -> Optional[Dict[str, Any]]:
    """在同步上下文中反射真实表结构

    返回 None 表示表不存在。结构与 NL2SQLService._get_schema_info 消费的格式一致:
    {"columns": [{name, type, nullable, description}], "primary_key": str, "foreign_keys": [...]}
    """
    inspector = sa_inspect(sync_session.get_bind())
    if not inspector.has_table(table_name):
        return None

    columns = [
        {
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": bool(col.get("nullable", True)),
            "description": (col.get("comment") or ""),
        }
        for col in inspector.get_columns(table_name)
    ]
    pk_cols = (inspector.get_pk_constraint(table_name) or {}).get(
        "constrained_columns"
    ) or []
    foreign_keys = [
        {
            "column": (fk.get("constrained_columns") or [None])[0],
            "ref_table": fk.get("referred_table"),
            "ref_column": (fk.get("referred_columns") or [None])[0],
        }
        for fk in inspector.get_foreign_keys(table_name)
    ]
    indexes = [
        {"name": idx.get("name"), "columns": idx.get("column_names") or []}
        for idx in inspector.get_indexes(table_name)
    ]
    return {
        "columns": columns,
        "primary_key": ", ".join(pk_cols),
        "foreign_keys": foreign_keys,
        "indexes": indexes,
    }


@router.post(
    "/api/v1/admin/nl2sql/schemas/{schema_id}/refresh",
    response_model=Dict[str, Any],
    tags=["admin-nl2sql"],
    summary="从真实库反射刷新表结构",
)
async def refresh_nl2sql_schema(
    schema_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service=Depends(get_audit_service),
    current_user_id: str = Depends(get_current_user_id),
):
    """刷新 NL2SQL 表结构定义

    直接反射数据库真实表结构 (列/主键/外键/索引) 覆盖 schema_definition，
    并保留人工维护的列 description，避免刷新后丢失业务语义标注。
    """
    tenant_id = get_current_tenant()
    service = NL2SQLService(session)
    schema = await service.get_schema(schema_id, tenant_id=tenant_id)
    if schema is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"schema {schema_id} 不存在"
        )

    table_name = schema.table_name
    reflected = await session.run_sync(_introspect_columns, table_name)
    if reflected is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据库中不存在表 {table_name}，无法刷新",
        )

    # 保留人工维护的列描述
    old_def = schema.schema_definition or {}
    old_desc = {
        c.get("name"): c.get("description")
        for c in (old_def.get("columns") or [])
        if c.get("description")
    }
    for col in reflected["columns"]:
        if not col["description"] and col["name"] in old_desc:
            col["description"] = old_desc[col["name"]]

    old_names = {c.get("name") for c in (old_def.get("columns") or [])}
    new_names = {c["name"] for c in reflected["columns"]}
    diff = {
        "added": sorted(new_names - old_names),
        "removed": sorted(old_names - new_names),
        "total": len(new_names),
    }

    updated = await service.update_schema(
        schema_id, schema_definition=reflected, tenant_id=tenant_id
    )
    await audit_service.log(
        actor_id=current_user_id,
        action="refresh_nl2sql_schema",
        details={"schema_id": schema_id, "table_name": table_name, "diff": diff},
        ip_address=request.headers.get("x-forwarded-for"),
    )
    await session.commit()

    result = NL2SQLService._schema_to_dict(updated)
    result["diff"] = diff
    return result


# ============================================================
# 8. 预算详情
# ============================================================


@router.get(
    "/api/v1/admin/budgets/{budget_id}",
    response_model=Dict[str, Any],
    tags=["admin-budget"],
    summary="预算详情",
)
async def get_budget_detail(
    budget_id: int,
    session: AsyncSession = Depends(get_db),
):
    """预算详情 (含使用百分比与告警标记)"""
    tenant_id = get_current_tenant()
    budget = (
        await session.execute(
            select(BudgetAlert).where(
                BudgetAlert.id == budget_id,
                BudgetAlert.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if budget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"预算 {budget_id} 不存在"
        )
    return BudgetService._serialize_budget_status(budget)


# ============================================================
# 9. 标注结果导出
# ============================================================


@router.get(
    "/api/v1/admin/annotations/tasks/{task_id}/export",
    tags=["admin-annotation"],
    summary="导出标注结果",
)
async def export_annotations(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    export_format: str = Query(
        default="csv", alias="format", description="导出格式: csv / json"
    ),
):
    """导出某个标注任务的全部标注结果

    前端以 blob 方式接收，因此统一返回带 Content-Disposition 的附件响应。
    """
    fmt = export_format.strip().lower()
    if fmt not in {"csv", "json"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format 只能是 csv 或 json",
        )

    tenant_id = get_current_tenant()
    service = AnnotationService(session)
    task = await service.get_task(task_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"标注任务 {task_id} 不存在"
        )
    annotations = await service.list_annotations(task_id, tenant_id=tenant_id)

    if fmt == "json":
        body = json.dumps(
            {
                "task": AnnotationService._task_to_dict(task),
                "annotations": annotations,
                "total": len(annotations),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        media_type = "application/json; charset=utf-8"
        filename = f"annotations_task_{task_id}.json"
    else:
        buffer = io.StringIO()
        fields = [
            "id",
            "task_id",
            "annotator_id",
            "label",
            "score",
            "feedback",
            "created_at",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in annotations:
            writer.writerow({k: row.get(k, "") for k in fields})
        # BOM 前缀: 保证 Excel 直接打开中文不乱码
        body = "\ufeff" + buffer.getvalue()
        media_type = "text/csv; charset=utf-8"
        filename = f"annotations_task_{task_id}.csv"

    return Response(
        content=body.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
# 10. 告警详情
# ============================================================


@router.get(
    "/api/v1/admin/alerts/{alert_id}",
    response_model=Dict[str, Any],
    tags=["admin-alerts"],
    summary="告警详情",
)
async def get_alert_detail(
    alert_id: int,
    session: AsyncSession = Depends(get_db),
):
    """告警详情"""
    tenant_id = get_current_tenant()
    service = AlertService(session)
    alert = await service.get_alert(alert_id, tenant_id=tenant_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"告警 {alert_id} 不存在"
        )
    return AlertService._alert_to_dict(alert)


# ============================================================
# 11. 定时任务详情
# ============================================================


@router.get(
    "/api/v1/admin/scheduler/tasks/{task_id}",
    response_model=Dict[str, Any],
    tags=["admin-scheduler"],
    summary="定时任务详情",
)
async def get_scheduled_task(task_id: str):
    """定时任务详情 (DB 配置 + APScheduler 下次执行时间)"""
    scheduler = _get_scheduler()
    tasks = await scheduler.list_tasks()
    for t in tasks:
        if str(t.get("task_id")) == str(task_id):
            return t
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"定时任务 {task_id} 不存在"
    )
