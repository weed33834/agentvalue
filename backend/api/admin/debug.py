"""调试与可观测性 Admin API (P1 调试增强)

参考:
- Langfuse Trace + Prompt 绑定: https://langfuse.com/docs/prompt-management/data-model
- Langfuse Observability: https://langfuse.com/docs/tracing

功能:
1. 查询某评估使用了哪个 prompt 版本 (从 audit_info 解密)
2. 查询某评估的完整 trace 链路 (model_tier / processing_time / triggered_rules)
3. 系统健康汇总 (circuit breaker / health cache / MCP 状态)

这些端点为 admin 提供调试入口,对标 Langfuse Dashboard 的 trace 详情页:
- 哪个 prompt 版本被使用
- 哪个 model tier 被选中
- 触发了哪些护栏规则
- 耗时与 token 消耗
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_evaluation_service
from auth.rbac import Role, require_role
from core.database import get_db
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/debug",
    tags=["admin-debug"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def _build_trace_spans(audit: Dict[str, Any], manager_view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 audit / manager_view 推断 7 个节点级 span,对标 Langfuse 节点级 trace。

    数据有限时使用合理默认值:
    - 缺失字段 → duration_ms=0、status="skipped"
    - 触发了护栏规则 → input_sanitizer 标记 warning
    - 多模态输入数 → 从 raw_data_refs 长度推断
    - retrieve_count 缺失 → retrieve_context 标记 skipped

    返回的 spans 已按执行顺序排列,start_ms 为相对时间线零点的偏移。
    """
    processing_time_ms = audit.get("processing_time_ms") or 0
    if not isinstance(processing_time_ms, (int, float)):
        processing_time_ms = 0

    triggered_rules = audit.get("triggered_rules") or []
    if not isinstance(triggered_rules, list):
        triggered_rules = []

    raw_data_refs = audit.get("raw_data_refs") or []
    if not isinstance(raw_data_refs, list):
        raw_data_refs = []

    retrieve_count = audit.get("retrieve_count") or 0
    if not isinstance(retrieve_count, (int, float)):
        retrieve_count = 0

    confidence_score = audit.get("confidence_score")
    prompt_version = audit.get("prompt_version")
    prompt_source = audit.get("prompt_source", "file")
    prompt_version_id = audit.get("prompt_version_id")
    model_name = audit.get("model_name")
    model_tier = audit.get("model_tier")

    # 各 span 时长估算(无精确分项耗时数据时按经验拆分)
    # 输入消毒通常很快,数据清洗随多模态输入数线性增长,
    # 检索中等,Prompt 渲染很短,LLM 调用占大头,解析较快,落库固定小值
    input_sanitizer_ms = 5 + (10 if triggered_rules else 0)
    data_cleaning_ms = 10 + len(raw_data_refs) * 20
    retrieve_ms = int(retrieve_count * 30) if retrieve_count else 0
    build_prompt_ms = 8
    persist_ms = 50
    parse_output_ms = 15

    # LLM 调用耗时 = 总耗时 - 其他已知 span 时长(避免负值)
    call_llm_ms = max(
        0,
        int(processing_time_ms)
        - input_sanitizer_ms
        - data_cleaning_ms
        - retrieve_ms
        - build_prompt_ms
        - parse_output_ms
        - persist_ms,
    )

    # 构建 span 列表(按执行顺序)
    spans: List[Dict[str, Any]] = []

    # 1. input_sanitizer —— InputGuard 耗时
    spans.append({
        "name": "input_sanitizer",
        "start_ms": 0,
        "duration_ms": input_sanitizer_ms,
        "status": "warning" if triggered_rules else "success",
        "attributes": {
            "triggered_rules": triggered_rules,
            "rules_count": len(triggered_rules),
        },
    })

    # 2. data_cleaning —— MultimodalCleaner
    multimodal_status = "success" if raw_data_refs else "skipped"
    spans.append({
        "name": "data_cleaning",
        "start_ms": input_sanitizer_ms,
        "duration_ms": data_cleaning_ms if raw_data_refs else 0,
        "status": multimodal_status,
        "attributes": {
            "raw_data_refs": raw_data_refs,
            "multimodal_input_count": len(raw_data_refs),
        },
    })

    # 3. retrieve_context —— RAG 检索
    retrieve_status = "skipped" if not retrieve_count else "success"
    spans.append({
        "name": "retrieve_context",
        "start_ms": input_sanitizer_ms + data_cleaning_ms,
        "duration_ms": retrieve_ms,
        "status": retrieve_status,
        "attributes": {
            "retrieve_count": retrieve_count,
        },
    })

    # 4. build_prompt —— Prompt 渲染
    spans.append({
        "name": "build_prompt",
        "start_ms": input_sanitizer_ms + data_cleaning_ms + retrieve_ms,
        "duration_ms": build_prompt_ms,
        "status": "success" if prompt_version else "warning",
        "attributes": {
            "prompt_version": prompt_version,
            "prompt_source": prompt_source,
            "prompt_version_id": prompt_version_id,
        },
    })

    # 5. call_llm —— LLM 调用(占大头)
    spans.append({
        "name": "call_llm",
        "start_ms": input_sanitizer_ms
        + data_cleaning_ms
        + retrieve_ms
        + build_prompt_ms,
        "duration_ms": call_llm_ms,
        "status": "success" if call_llm_ms > 0 else "skipped",
        "attributes": {
            "model_name": model_name,
            "model_tier": model_tier,
        },
    })

    # 6. parse_output —— OutputGuard
    spans.append({
        "name": "parse_output",
        "start_ms": input_sanitizer_ms
        + data_cleaning_ms
        + retrieve_ms
        + build_prompt_ms
        + call_llm_ms,
        "duration_ms": parse_output_ms,
        "status": "success" if confidence_score is not None else "warning",
        "attributes": {
            "confidence_score": confidence_score,
        },
    })

    # 7. persist —— 落库(固定小值)
    spans.append({
        "name": "persist",
        "start_ms": input_sanitizer_ms
        + data_cleaning_ms
        + retrieve_ms
        + build_prompt_ms
        + call_llm_ms
        + parse_output_ms,
        "duration_ms": persist_ms,
        "status": "success",
        "attributes": {
            "fixed_estimate": True,
        },
    })

    return spans


@router.get("/evaluations", response_model=Dict[str, Any])
async def list_evaluations_for_trace(
    eval_service: EvaluationService = Depends(get_evaluation_service),
    page: int = Query(1, ge=1, description="页码,从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页大小,1-100"),
    employee_id: Optional[str] = Query(None, description="按员工 ID 精确过滤"),
    period: Optional[str] = Query(None, description="按评估周期过滤,如 2026-W28"),
    status: Optional[str] = Query(None, description="按评估状态过滤(ai_drafted/approved/...)"),
):
    """评估分页列表(Trace 浏览器左侧列表使用)。

    返回精简字段:{items: [{evaluation_id, employee_id, period, status,
    overall_score, created_at}], total, page, page_size}。

    鉴权沿用 router 级 dependencies=[require_role(ADMIN)]。
    """
    result = await eval_service.list_evaluations(
        employee_id=employee_id,
        status=status,
        period=period,
        page=page,
        page_size=page_size,
    )

    # 仅暴露列表所需字段,避免泄漏 manager_view / audit 等敏感字段
    items = []
    for ev in result["items"]:
        items.append({
            "evaluation_id": ev.evaluation_id,
            "employee_id": ev.employee_id,
            "period": ev.period,
            "status": ev.status,
            "overall_score": ev.overall_score,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        })

    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/evaluation/{evaluation_id}/prompt-version", response_model=Dict[str, Any])
async def get_evaluation_prompt_version(
    evaluation_id: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """查询某评估使用的 prompt 版本信息。

    P1 调试增强: 从 evaluation.audit (AES-GCM 加密) 解密出 prompt_version_info,
    便于追溯某次评估用了哪个 prompt 版本。

    对标 Langfuse: trace 详情页的 "prompt_version" metadata 字段。
    """
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评估 {evaluation_id} 不存在",
        )

    audit = evaluation.audit or {}
    if not isinstance(audit, dict):
        return {
            "evaluation_id": evaluation_id,
            "error": "audit 字段非 dict,无法解析",
            "raw_audit_type": str(type(audit)),
        }

    return {
        "evaluation_id": evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "prompt_version": audit.get("prompt_version"),
        "prompt_source": audit.get("prompt_source", "file"),
        "prompt_version_id": audit.get("prompt_version_id"),
        "model_name": audit.get("model_name"),
        "model_tier": audit.get("model_tier"),
        "processing_time_ms": audit.get("processing_time_ms"),
        "confidence_score": audit.get("confidence_score"),
        "triggered_rules": audit.get("triggered_rules", []),
        "note": (
            "prompt_source=file: 来自文件 PromptLoader; "
            "db: 来自 DbPromptLoader (含 A/B / 灰度); "
            "file_fallback: DB 无此 prompt 回退文件; "
            "file_error: DB 异常回退文件"
        ),
    }


@router.get("/evaluation/{evaluation_id}/trace", response_model=Dict[str, Any])
async def get_evaluation_trace(
    evaluation_id: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """查询某评估的完整 trace 链路信息。

    汇总 audit + manager_view 中的执行元数据,对标 Langfuse trace 详情:
    - model 选型 (tier / name)
    - prompt 版本
    - 护栏触发
    - 风险标记
    - 处理耗时

    P1-2 扩展: 增加 spans 数组(7 个节点级 span)与 timeline 字段,
    供前端时间线/Gantt 可视化渲染。保留原有 trace 字段以保证向后兼容。
    """
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评估 {evaluation_id} 不存在",
        )

    audit = evaluation.audit or {}
    if not isinstance(audit, dict):
        audit = {}
    manager_view = evaluation.manager_view or {}
    if not isinstance(manager_view, dict):
        manager_view = {}

    triggered_rules = audit.get("triggered_rules") or []
    if not isinstance(triggered_rules, list):
        triggered_rules = []
    raw_data_refs = audit.get("raw_data_refs") or []
    if not isinstance(raw_data_refs, list):
        raw_data_refs = []

    # 构建 7 个节点级 span(对标 Langfuse 节点级 trace)
    spans = _build_trace_spans(audit, manager_view)

    # 时间线总耗时: spans 中最后一个 span 的结束时间
    total_ms = 0
    if spans:
        last_span = spans[-1]
        total_ms = last_span["start_ms"] + last_span["duration_ms"]
    # 兜底: 若 spans 推算为 0,直接采用 audit.processing_time_ms
    if total_ms == 0:
        proc = audit.get("processing_time_ms") or 0
        if isinstance(proc, (int, float)):
            total_ms = int(proc)

    # 失败 span 计数(status 非 success/skipped 即视为失败)
    failed_count = sum(1 for s in spans if s["status"] not in ("success", "skipped"))

    return {
        "evaluation_id": evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "overall_score": evaluation.overall_score,
        "status": evaluation.status,
        "trace": {
            "model": {
                "name": audit.get("model_name"),
                "tier": audit.get("model_tier"),
            },
            "prompt": {
                "version": audit.get("prompt_version"),
                "source": audit.get("prompt_source", "file"),
                "version_id": audit.get("prompt_version_id"),
            },
            "performance": {
                "processing_time_ms": audit.get("processing_time_ms"),
                "confidence_score": audit.get("confidence_score"),
            },
            "guards": {
                "triggered_rules": triggered_rules,
                "raw_data_refs": raw_data_refs,
            },
            "risk": {
                "risk_flags": manager_view.get("risk_flags", []) if isinstance(manager_view, dict) else [],
            },
        },
        # P1-2: 节点级 spans,对标 Langfuse trace 节点
        "spans": spans,
        # P1-2: 时间线渲染所需的总览信息
        "timeline": {
            "start_ms": 0,
            "total_ms": total_ms,
            "span_count": len(spans),
            "failed_count": failed_count,
        },
        "langfuse_hint": (
            "若已配置 Langfuse,可在 Langfuse UI 按 evaluation_id 搜索 trace,"
            "查看节点级耗时与 LLM generation 详情"
        ),
    }


@router.get("/system-health", response_model=Dict[str, Any])
async def system_health(
    session: AsyncSession = Depends(get_db),
):
    """系统健康汇总 (对标 Langfuse Dashboard 的系统状态页)。

    汇总:
    - Circuit Breaker 状态 (各 tier 是否熔断)
    - Health Cache 状态 (各 tier 健康缓存)
    - MCP 服务器连接状态
    - LangChain 工具可用性
    """
    # Circuit Breaker
    circuit_states: Dict[str, Any] = {}
    try:
        from core.circuit_breaker import get_global_registry

        registry = get_global_registry()
        circuit_states = registry.all_states()
    except Exception as e:
        circuit_states = {"error": str(e)}

    # Health Cache (通过 model_router 间接获取)
    hardware_report: Dict[str, Any] = {}
    try:
        # 不直接依赖 app_state,通过 settings 构造临时 router 取报告
        # (避免循环依赖,实际生产应在 app_state 层暴露)
        hardware_report = {"note": "通过 GET /admin/model-status 获取详细硬件报告"}
    except Exception:
        pass

    # MCP
    mcp_status: Dict[str, Any] = {}
    try:
        from agent.mcp_client import MCP_AVAILABLE

        mcp_status = {
            "available": MCP_AVAILABLE,
            "servers": [],
        }
        from core.config import get_settings

        settings = get_settings()
        if settings.mcp_servers:
            from agent.mcp_client import get_global_mcp_manager

            manager = get_global_mcp_manager(settings.mcp_servers)
            mcp_status["servers"] = manager.list_servers()
    except Exception as e:
        mcp_status = {"error": str(e)}

    # LangChain 工具
    tools_status: Dict[str, Any] = {}
    try:
        from agent.langchain_tools import LANGCHAIN_TOOLS_AVAILABLE, list_available_tools
        from agent.react_agent import REACT_AGENT_AVAILABLE

        tools_status = {
            "langchain_available": LANGCHAIN_TOOLS_AVAILABLE,
            "react_agent_available": REACT_AGENT_AVAILABLE,
            "available_tools": [
                t["name"] for t in list_available_tools()
            ],
        }
    except Exception as e:
        tools_status = {"error": str(e)}

    return {
        "circuit_breakers": circuit_states,
        "hardware": hardware_report,
        "mcp": mcp_status,
        "tools": tools_status,
    }
