"""多 Agent 协作 Admin API (P4-1, 对标 Coze Multi-Agent)

路由前缀: /api/v1/admin/multi-agent
权限: Role.ADMIN (router 级 dependencies)

完整功能 (5 端点):
- POST /run                            - 异步运行多 Agent 任务, 返回 thread_id, 后台运行
- GET  /threads/{thread_id}/state      - 查询状态 (含 next / values / interrupt 信息)
- POST /threads/{thread_id}/resume     - 恢复执行, body {decision?, comment?}
- GET  /threads/{thread_id}/artifacts - 查询各 Agent 产出
- POST /test                           - 同步测试 (不进队列, 直接执行返回结果)

设计要点:
- 内存 thread_store 保存 thread_id → 元信息 (生产应替换为持久化)
- asyncio.create_task 后台执行, 不阻塞 HTTP 响应
- max_iterations 默认 10, 硬上限 50 (在 agent.multi_agent 层强制)
- 各 expert agent 失败时 artifacts[name] = {error: str}, 不影响其他 agent
- 支持按租户隔离 (复用 app_state.get_memory_store / get_kb_store)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.types import Command as LangGraphCommand
from pydantic import BaseModel, ConfigDict, Field

from api.deps import AppState, get_app_state
from auth.rbac import Role, get_current_user_id, require_role
from core.tenant_context import get_current_tenant
from models.models import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/multi-agent",
    tags=["admin-multi-agent"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# 内存 thread store (生产应替换为 Redis / DB 持久化)
# ============================================================

# thread_id → meta (task / status / artifacts / final_report / created_at / tenant_id ...)
_multi_agent_threads: Dict[str, Dict[str, Any]] = {}
# thread_id → asyncio.Task (用于后台执行, 完成后清理)
_running_tasks: Dict[str, asyncio.Task] = {}
_MAX_THREADS = 1000


def _put_thread(thread_id: str, meta: Dict[str, Any]) -> None:
    """写入 thread_store, 超限时按插入顺序删除最早的若干条目, 防止无界增长。"""
    _multi_agent_threads[thread_id] = meta
    if len(_multi_agent_threads) > _MAX_THREADS:
        overflow = len(_multi_agent_threads) - _MAX_THREADS
        for key in list(_multi_agent_threads.keys())[:overflow]:
            _multi_agent_threads.pop(key, None)


def clear_thread_store() -> None:
    """测试 helper: 清空 thread store 与 running tasks (conftest 调用)"""
    _multi_agent_threads.clear()
    # 不强制 cancel 已运行 task (异步取消可能引发副作用), 测试场景下让自然完成
    _running_tasks.clear()


# ============================================================
# 多 Agent 图缓存 (类似 _interrupt_graphs 模式, 按租户惰性创建)
# ============================================================


def _get_or_create_multi_agent_graph(app_state: AppState, tenant_id: str):
    """获取或创建多 Agent 图实例 (按租户惰性创建, 复用 checkpointer)。

    缓存到 app_state._multi_agent_graphs, 避免每次请求重建图。
    每个 tenant_id 一份独立实例 (含独立 checkpointer), 避免跨租户 thread_id 状态串扰。
    """
    if not hasattr(app_state, "_multi_agent_graphs"):
        app_state._multi_agent_graphs = {}
    graph = app_state._multi_agent_graphs.get(tenant_id)
    if graph is None:
        from agent.multi_agent import create_multi_agent_graph
        from agent.tools import AgentToolkit

        toolkit = AgentToolkit(
            memory=app_state.get_memory_store(tenant_id),
            kb=app_state.get_kb_store(tenant_id),
        )
        graph = create_multi_agent_graph(
            model_router=app_state.model_router,
            toolkit=toolkit,
            prompt_loader=app_state.prompt_loader,
        )
        app_state._multi_agent_graphs[tenant_id] = graph
    return graph


# ============================================================
# Schemas
# ============================================================


class MultiAgentRunRequest(BaseModel):
    """运行多 Agent 任务请求"""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(..., min_length=1, max_length=2000, description="任务描述")
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="共享上下文, 如 {employee_id, period}",
    )
    max_iterations: int = Field(
        default=10,
        ge=1,
        le=50,
        description="最大迭代次数, 默认 10, 硬上限 50",
    )
    interrupt_at: Optional[str] = Field(
        default=None,
        description="到此节点暂停等人工 (data_analyst / code_reviewer / risk_assessor / report_writer)",
    )


class MultiAgentResumeRequest(BaseModel):
    """恢复执行请求"""

    model_config = ConfigDict(extra="forbid")

    decision: Optional[str] = Field(
        default=None, max_length=2000, description="人工决策"
    )
    comment: Optional[str] = Field(
        default=None, max_length=5000, description="人工备注"
    )


# ============================================================
# Helper: 构造初始 state
# ============================================================


def _build_initial_state(req: MultiAgentRunRequest) -> Dict[str, Any]:
    """构造多 Agent 图初始 state"""
    return {
        "messages": [],
        "task": req.task,
        "context": req.context,
        "max_iterations": req.max_iterations,
        "interrupt_at": req.interrupt_at,
        "iteration": 0,
        "next_agent": "",
        "artifacts": {},
        "final_report": None,
        "error": None,
        "timeline": [],
    }


def _extract_interrupt_info(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 ainvoke 结果中提取 interrupt 信息 (若有)"""
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if not interrupts:
        return None
    info = interrupts[0]
    # LangGraph 把 interrupt value 包成 Interrupt 对象
    return info.value if hasattr(info, "value") else info


def _is_waiting(result: Dict[str, Any]) -> bool:
    return isinstance(result, dict) and "__interrupt__" in result


# ============================================================
# 后台执行任务
# ============================================================


async def _run_multi_agent_async(
    thread_id: str,
    req: MultiAgentRunRequest,
    app_state: AppState,
    tenant_id: str,
    actor_id: str,
) -> None:
    """后台执行多 Agent 任务, 完成后更新 thread_store"""
    graph = _get_or_create_multi_agent_graph(app_state, tenant_id)
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = _build_initial_state(req)

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.exception("multi-agent 任务执行失败 thread_id=%s", thread_id)
        meta = _multi_agent_threads.get(thread_id, {})
        meta["status"] = "failed"
        meta["error"] = f"执行异常: {e}"
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        _multi_agent_threads[thread_id] = meta
        return

    meta = _multi_agent_threads.get(thread_id, {})

    if _is_waiting(result):
        # 触发 interrupt, 状态置为 waiting
        interrupt_info = _extract_interrupt_info(result)
        meta["status"] = "waiting"
        meta["interrupt_node"] = (
            interrupt_info.get("node") if isinstance(interrupt_info, dict) else None
        )
        meta["interrupt_info"] = interrupt_info
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "multi-agent 任务暂停 thread_id=%s node=%s",
            thread_id,
            meta.get("interrupt_node"),
        )
    else:
        meta["status"] = "completed"
        meta["final_report"] = result.get("final_report")
        meta["artifacts"] = result.get("artifacts")
        meta["error"] = result.get("error")
        meta["timeline"] = result.get("timeline")
        meta["iteration"] = result.get("iteration")
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "multi-agent 任务完成 thread_id=%s artifacts=%s",
            thread_id,
            list((meta.get("artifacts") or {}).keys()),
        )

    _multi_agent_threads[thread_id] = meta


# ============================================================
# 端点 1: POST /run - 异步运行多 Agent 任务
# ============================================================


@router.post("/run", response_model=Dict[str, Any])
async def run_multi_agent(
    req: MultiAgentRunRequest,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """异步运行多 Agent 任务, 返回 thread_id, 后台执行。

    body:
        {
          "task": "分析员工 E1001 在 2026-W28 的表现,综合日报、代码贡献和风险",
          "context": {"employee_id": "E1001", "period": "2026-W28"},
          "max_iterations": 10,
          "interrupt_at": "report_writer"  // 可选
        }

    返回 thread_id, 可用 GET /threads/{thread_id}/state 轮询状态。
    """
    tenant_id = get_current_tenant()
    thread_id = f"ma-{uuid.uuid4().hex[:12]}"
    actor_id = "anonymous"
    try:
        actor_id = await get_current_user_id(request)
    except Exception:
        pass

    _put_thread(
        thread_id,
        {
            "thread_id": thread_id,
            "task": req.task,
            "context": req.context,
            "max_iterations": req.max_iterations,
            "interrupt_at": req.interrupt_at,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "created_by": actor_id,
            "artifacts": {},
            "final_report": None,
            "error": None,
            "timeline": [],
        },
    )

    # 后台执行 (不阻塞 HTTP 响应)
    task = asyncio.create_task(
        _run_multi_agent_async(thread_id, req, app_state, tenant_id, actor_id)
    )
    _running_tasks[thread_id] = task

    return {
        "thread_id": thread_id,
        "status": "running",
        "created_at": _multi_agent_threads[thread_id]["created_at"],
    }


# ============================================================
# 端点 2: GET /threads/{thread_id}/state - 查询状态
# ============================================================


@router.get("/threads/{thread_id}/state", response_model=Dict[str, Any])
async def get_multi_agent_state(
    thread_id: str,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """查询多 Agent 任务当前状态。

    返回:
    - meta: thread_store 中的元信息 (status / created_at / interrupt_node ...)
    - next: 当前 LangGraph state 的 next (即将执行的节点列表)
    - values: 当前 LangGraph state 的完整 values (含 artifacts / timeline / iteration)
    - interrupt: 若暂停, 包含 interrupt 信息
    """
    meta = _multi_agent_threads.get(thread_id)
    if not meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"thread {thread_id} 不存在",
        )

    # 从 LangGraph checkpointer 取实时 state (与 thread_store meta 互补)
    tenant_id = meta.get("tenant_id", DEFAULT_TENANT_ID)
    graph = _get_or_create_multi_agent_graph(app_state, tenant_id)
    config = {"configurable": {"thread_id": thread_id}}

    state_obj = None
    try:
        state_obj = await graph.aget_state(config)
    except Exception:
        logger.debug(
            "查询 multi-agent state 失败 thread_id=%s", thread_id, exc_info=True
        )

    next_nodes: List[str] = []
    values: Dict[str, Any] = {}
    if state_obj is not None:
        next_nodes = list(state_obj.next) if state_obj.next else []
        values = state_obj.values or {}

    return {
        "thread_id": thread_id,
        "meta": {
            "task": meta.get("task"),
            "status": meta.get("status"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "interrupt_node": meta.get("interrupt_node"),
            "interrupt_info": meta.get("interrupt_info"),
            "error": meta.get("error"),
            "final_report": meta.get("final_report"),
        },
        "next": next_nodes,
        "values": values,
    }


# ============================================================
# 端点 3: POST /threads/{thread_id}/resume - 恢复执行
# ============================================================


@router.post("/threads/{thread_id}/resume", response_model=Dict[str, Any])
async def resume_multi_agent(
    thread_id: str,
    payload: MultiAgentResumeRequest,
    request: Request,
    app_state: AppState = Depends(get_app_state),
):
    """恢复暂停的多 Agent 任务。

    body: {"decision": "<可选>", "comment": "<可选>"}

    只能在 status=waiting 时调用, 恢复后状态变为 running, 完成后变 completed。
    """
    meta = _multi_agent_threads.get(thread_id)
    if not meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"thread {thread_id} 不存在",
        )
    if meta.get("status") != "waiting":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"thread 状态 {meta.get('status')} 不可恢复, 仅 waiting 可恢复",
        )

    tenant_id = meta.get("tenant_id", DEFAULT_TENANT_ID)
    graph = _get_or_create_multi_agent_graph(app_state, tenant_id)
    config = {"configurable": {"thread_id": thread_id}}

    resume_value = {
        "decision": payload.decision or "approve",
        "comment": payload.comment or "",
        "actor_id": "anonymous",
    }
    try:
        resume_value["actor_id"] = await get_current_user_id(request)
    except Exception:
        pass

    # 更新 thread_store 状态为 running
    meta["status"] = "running"
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    _multi_agent_threads[thread_id] = meta

    # 后台恢复执行
    task = asyncio.create_task(
        _resume_multi_agent_async(thread_id, graph, config, resume_value)
    )
    _running_tasks[thread_id] = task

    return {
        "thread_id": thread_id,
        "status": "running",
        "resume_value": resume_value,
    }


async def _resume_multi_agent_async(
    thread_id: str,
    graph: Any,
    config: Dict[str, Any],
    resume_value: Dict[str, Any],
) -> None:
    """后台恢复执行多 Agent 任务"""
    try:
        result = await graph.ainvoke(
            LangGraphCommand(resume=resume_value), config=config
        )
    except Exception as e:
        logger.exception("multi-agent 任务恢复失败 thread_id=%s", thread_id)
        meta = _multi_agent_threads.get(thread_id, {})
        meta["status"] = "failed"
        meta["error"] = f"恢复异常: {e}"
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        _multi_agent_threads[thread_id] = meta
        return

    meta = _multi_agent_threads.get(thread_id, {})

    if _is_waiting(result):
        interrupt_info = _extract_interrupt_info(result)
        meta["status"] = "waiting"
        meta["interrupt_node"] = (
            interrupt_info.get("node") if isinstance(interrupt_info, dict) else None
        )
        meta["interrupt_info"] = interrupt_info
    else:
        meta["status"] = "completed"
        meta["final_report"] = result.get("final_report")
        meta["artifacts"] = result.get("artifacts")
        meta["error"] = result.get("error")
        meta["timeline"] = result.get("timeline")
        meta["iteration"] = result.get("iteration")

    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    _multi_agent_threads[thread_id] = meta


# ============================================================
# 端点 4: GET /threads/{thread_id}/artifacts - 查询各 Agent 产出
# ============================================================


@router.get("/threads/{thread_id}/artifacts", response_model=Dict[str, Any])
async def get_multi_agent_artifacts(
    thread_id: str,
    app_state: AppState = Depends(get_app_state),
):
    """查询各 Agent 产出 (artifacts)。

    返回 {thread_id, artifacts: {agent_name: dict, ...}, final_report, status}
    """
    meta = _multi_agent_threads.get(thread_id)
    if not meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"thread {thread_id} 不存在",
        )

    # 优先用 thread_store 中已汇总的 artifacts
    artifacts = meta.get("artifacts") or {}
    if not artifacts:
        # 兜底: 从 LangGraph state 取实时 artifacts
        tenant_id = meta.get("tenant_id", DEFAULT_TENANT_ID)
        graph = _get_or_create_multi_agent_graph(app_state, tenant_id)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            state_obj = await graph.aget_state(config)
            if state_obj and state_obj.values:
                artifacts = state_obj.values.get("artifacts") or {}
        except Exception:
            logger.debug(
                "从 LangGraph state 取 artifacts 失败 thread_id=%s",
                thread_id,
                exc_info=True,
            )

    return {
        "thread_id": thread_id,
        "artifacts": artifacts,
        "final_report": meta.get("final_report"),
        "status": meta.get("status"),
    }


# ============================================================
# 端点 5: POST /test - 同步测试 (不进队列, 直接执行返回结果)
# ============================================================


@router.post("/test", response_model=Dict[str, Any])
async def test_multi_agent(
    req: MultiAgentRunRequest,
    app_state: AppState = Depends(get_app_state),
):
    """同步测试多 Agent 任务, 直接执行返回结果。

    与 /run 区别:
    - /run: 异步, 返回 thread_id 立即响应, 后台执行
    - /test: 同步, 阻塞直到完成或暂停, 直接返回结果

    适合前端"测试"按钮, 快速验证多 Agent 配置是否正确。
    """
    tenant_id = get_current_tenant()
    graph = _get_or_create_multi_agent_graph(app_state, tenant_id)
    thread_id = f"ma-test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = _build_initial_state(req)

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.exception("multi-agent 测试执行失败")
        return {
            "thread_id": thread_id,
            "status": "failed",
            "error": f"执行异常: {e}",
            "artifacts": {},
            "final_report": None,
            "timeline": [],
            "iteration": 0,
        }

    if _is_waiting(result):
        interrupt_info = _extract_interrupt_info(result)
        return {
            "thread_id": thread_id,
            "status": "waiting",
            "interrupt": interrupt_info,
            "interrupt_node": (
                interrupt_info.get("node") if isinstance(interrupt_info, dict) else None
            ),
            "artifacts": result.get("artifacts") or {},
            "final_report": result.get("final_report"),
            "timeline": result.get("timeline") or [],
            "iteration": result.get("iteration", 0),
            "error": result.get("error"),
        }

    return {
        "thread_id": thread_id,
        "status": "completed",
        "artifacts": result.get("artifacts") or {},
        "final_report": result.get("final_report"),
        "timeline": result.get("timeline") or [],
        "iteration": result.get("iteration", 0),
        "error": result.get("error"),
    }


# ============================================================
# Helper: 列出所有 thread (供前端任务列表)
# ============================================================


@router.get("/threads", response_model=Dict[str, Any])
async def list_multi_agent_threads(
    status_filter: Optional[str] = None,
    limit: int = 50,
):
    """列出所有多 Agent 任务 (供前端任务列表)。

    query:
    - status: 可选过滤 (running / waiting / completed / failed)
    - limit: 返回条数上限, 默认 50
    """
    items = list(_multi_agent_threads.values())
    if status_filter:
        items = [m for m in items if m.get("status") == status_filter]
    # 按创建时间倒序
    items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    items = items[:limit]
    return {
        "items": [
            {
                "thread_id": m.get("thread_id"),
                "task": m.get("task"),
                "status": m.get("status"),
                "created_at": m.get("created_at"),
                "updated_at": m.get("updated_at"),
                "interrupt_node": m.get("interrupt_node"),
                "error": m.get("error"),
            }
            for m in items
        ],
        "total": len(_multi_agent_threads),
    }
