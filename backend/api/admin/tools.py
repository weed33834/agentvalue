"""工具管理 Admin API (P1 工具管理增强)

参考:
- Dify 工具管理: 内置工具 + 自定义工具 + 工具测试 + 工具调用日志
- Coze 插件管理: 插件列表 + 插件配置 + 插件测试
- LangChain ToolNode: https://docs.langchain.com/oss/python/langchain-tools

完整功能:
1. 列出所有工具 (内置 + toolkit + MCP) 及其 schema
2. 测试单个工具 (传入参数,看返回)
3. MCP 服务器管理 (列出 / 测试连接 / 热更新配置)
4. 启用/禁用工具 (通过 config)
5. 创建 ReAct Agent 会话 (供复杂推理任务用)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.deps import get_app_state
from auth.rbac import Role, require_role
from core.config import get_settings
from core.rate_limit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/tools",
    tags=["admin-tools"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ====== Pydantic 请求模型 ======


class TestToolRequest(BaseModel):
    """测试工具调用"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128, description="工具名")
    args: Dict[str, Any] = Field(
        default_factory=dict, description="工具参数 (按 schema 提供)"
    )


class TestMCPRequest(BaseModel):
    """测试 MCP 服务器连接"""

    model_config = ConfigDict(extra="forbid")

    server_name: str = Field(min_length=1, max_length=128)


class UpdateMCPConfigRequest(BaseModel):
    """更新 MCP 配置 (热更新)"""

    model_config = ConfigDict(extra="forbid")

    mcp_servers: Optional[str] = Field(
        default=None,
        description="MCP 服务器配置 JSON 字符串,None 清空配置",
    )
    enabled_tools: Optional[str] = Field(
        default=None,
        description="启用的工具名 (逗号分隔),None 表示全部启用",
    )


class InvokeReActAgentRequest(BaseModel):
    """调用 ReAct Agent"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=10000, description="用户消息")
    thread_id: Optional[str] = Field(
        default=None, max_length=128, description="会话 ID (多轮对话用)"
    )


# ====== 路由: 工具列表与元数据 ======


@router.get("", response_model=Dict[str, Any])
async def list_tools(
    request: Request,
    app_state=Depends(get_app_state),
):
    """列出所有可用工具及其 schema (对标 Dify 工具市场)。

    返回:
    - builtin: 内置工具 (calculator, datetime 等)
    - toolkit: 依赖 AgentToolkit 的工具 (employee_history, company_kb)
    - mcp: MCP 外部服务器提供的工具
    - 每个工具含 name/description/args_schema/category/enabled
    """
    from agent.langchain_tools import list_available_tools

    settings = app_state.settings
    enabled_csv = getattr(settings, "enabled_tools", None)

    # 1) 内置 + toolkit 工具
    builtin_tools = list_available_tools(enabled_csv)

    # 2) MCP 工具
    mcp_tools: List[Dict[str, Any]] = []
    if getattr(settings, "mcp_servers", None):
        from agent.mcp_client import get_global_mcp_manager

        manager = get_global_mcp_manager(settings.mcp_servers)
        if manager.is_available():
            try:
                tools = await manager.get_tools()
                for t in tools:
                    schema = {}
                    try:
                        if hasattr(t, "args_schema") and t.args_schema:
                            schema = t.args_schema.model_json_schema()
                    except Exception:
                        pass
                    mcp_tools.append(
                        {
                            "name": getattr(t, "name", str(t)),
                            "description": getattr(t, "description", ""),
                            "args_schema": schema,
                            "category": "mcp",
                            "enabled": True,
                        }
                    )
            except Exception as e:
                logger.warning("列出 MCP 工具失败: %s", e)

    return {
        "builtin": builtin_tools,
        "mcp": mcp_tools,
        "langchain_available": _check_langchain_available(),
        "mcp_available": _check_mcp_available(),
        "enabled_tools": enabled_csv,
        "mcp_servers": _safe_mcp_servers_list(settings),
    }


def _check_langchain_available() -> bool:
    try:
        from agent.langchain_tools import LANGCHAIN_TOOLS_AVAILABLE

        return LANGCHAIN_TOOLS_AVAILABLE
    except ImportError:
        return False


def _check_mcp_available() -> bool:
    try:
        from agent.mcp_client import MCP_AVAILABLE

        return MCP_AVAILABLE
    except ImportError:
        return False


def _safe_mcp_servers_list(settings) -> List[Dict[str, Any]]:
    """安全列出 MCP 服务器配置 (隐藏敏感 header)"""
    if not getattr(settings, "mcp_servers", None):
        return []
    from agent.mcp_client import get_global_mcp_manager

    manager = get_global_mcp_manager(settings.mcp_servers)
    return manager.list_servers()


# ====== 路由: 测试工具 ======


@router.post("/test", response_model=Dict[str, Any])
@rate_limit("30/minute")
async def test_tool(
    payload: TestToolRequest,
    request: Request,
    app_state=Depends(get_app_state),
):
    """测试单个工具调用 (对标 Dify 工具测试面板)。

    传入工具名与参数,返回工具执行结果。
    支持: 内置工具 (calculator/datetime) + toolkit 工具 (employee_history/company_kb)。
    MCP 工具测试通过 /mcp/{server_name}/test 端点。
    """
    import json

    from agent.langchain_tools import build_langchain_tools

    settings = app_state.settings
    enabled_csv = getattr(settings, "enabled_tools", None)

    # toolkit 工具需要 memory/kb (用 default 租户的)
    toolkit = None
    try:
        toolkit = app_state.get_graph  # placeholder, 实际需要构造 AgentToolkit
        # 直接用 app_state 的 memory_store / company_kb
        from agent.tools import AgentToolkit

        toolkit = AgentToolkit(
            memory=app_state.get_memory_store(),
            kb=app_state.get_kb_store(),
        )
    except Exception as e:
        logger.warning("构造 AgentToolkit 失败,仅测试内置工具: %s", e)

    tools = build_langchain_tools(toolkit=toolkit, enabled_csv=enabled_csv)
    target = None
    for t in tools:
        if getattr(t, "name", "") == payload.tool_name:
            target = t
            break

    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"工具 {payload.tool_name} 不存在或未启用",
        )

    try:
        import asyncio
        import inspect

        if inspect.iscoroutinefunction(target):
            result = await target.ainvoke(payload.args)
        else:
            result = target.invoke(payload.args)

        # result 可能是 str 或 ToolMessage
        if hasattr(result, "content"):
            result_str = result.content
        else:
            result_str = str(result)

        return {
            "tool_name": payload.tool_name,
            "args": payload.args,
            "result": result_str,
            "success": True,
        }
    except Exception as e:
        logger.warning("测试工具 %s 失败: %s", payload.tool_name, e)
        return {
            "tool_name": payload.tool_name,
            "args": payload.args,
            "error": str(e),
            "success": False,
        }


# ====== 路由: MCP 服务器管理 ======


@router.get("/mcp/servers", response_model=Dict[str, Any])
async def list_mcp_servers(
    request: Request,
    app_state=Depends(get_app_state),
):
    """列出所有已配置的 MCP 服务器 (对标 Dify 外部工具接入管理)"""
    settings = app_state.settings
    return {
        "servers": _safe_mcp_servers_list(settings),
        "mcp_available": _check_mcp_available(),
        "raw_config": settings.mcp_servers,
    }


@router.post("/mcp/test", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def test_mcp_connection(
    payload: TestMCPRequest,
    request: Request,
    app_state=Depends(get_app_state),
):
    """测试 MCP 服务器连接 (对标 Dify 工具连接测试)"""
    settings = app_state.settings
    if not getattr(settings, "mcp_servers", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未配置 MCP 服务器",
        )

    from agent.mcp_client import get_global_mcp_manager

    manager = get_global_mcp_manager(settings.mcp_servers)
    result = await manager.test_connection(payload.server_name)
    return result


@router.put("/mcp/config", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def update_mcp_config(
    payload: UpdateMCPConfigRequest,
    request: Request,
    app_state=Depends(get_app_state),
):
    """更新 MCP 配置与启用工具列表 (热更新,无需重启)"""
    settings = app_state.settings

    # 更新 settings (运行时)
    if payload.mcp_servers is not None:
        # 验证 JSON
        if payload.mcp_servers:
            import json

            try:
                parsed = json.loads(payload.mcp_servers)
                if not isinstance(parsed, dict):
                    raise ValueError("mcp_servers 必须为 JSON 对象")
            except (json.JSONDecodeError, ValueError) as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"mcp_servers JSON 无效: {e}",
                )
        # 热更新 MCP 客户端
        if settings.mcp_servers:
            from agent.mcp_client import get_global_mcp_manager

            manager = get_global_mcp_manager(settings.mcp_servers)
            await manager.reload_config(payload.mcp_servers or None)
        # 更新 settings
        try:
            settings.mcp_servers = payload.mcp_servers or None
        except Exception:
            # model_config 可能未开启 validate_assignment, 直接改属性
            object.__setattr__(settings, "mcp_servers", payload.mcp_servers or None)

    if payload.enabled_tools is not None:
        try:
            settings.enabled_tools = payload.enabled_tools or None
        except Exception:
            object.__setattr__(settings, "enabled_tools", payload.enabled_tools or None)

    return {
        "updated": True,
        "mcp_servers": payload.mcp_servers,
        "enabled_tools": payload.enabled_tools,
        "note": "配置已热更新,后续请求生效",
    }


# ====== 路由: ReAct Agent 调用 ======


@router.post("/react-invoke", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def invoke_react_agent(
    payload: InvokeReActAgentRequest,
    request: Request,
    app_state=Depends(get_app_state),
):
    """调用 ReAct Agent 进行复杂推理任务。

    对标 Dify Agent 节点: LLM 自主选择工具、多轮推理、最终汇总答案。

    与固定评估流水线的区别:
    - 固定流水线 (POST /evaluations): 标准化评估,9 节点顺序执行
    - ReAct Agent (本端点): 复杂推理,如"对比张三历史表现并给出建议"

    适用场景:
    - 分析性查询: "分析本周研发部整体表现趋势"
    - 多步推理: "查询知识库标准 + 获取员工历史 + 综合评估"
    - 开放式问答: "如何提高李四的协作能力评分?"
    """
    from agent.react_agent import (
        is_react_agent_available,
        create_evaluation_react_agent,
        invoke_react_agent as _invoke,
    )

    if not is_react_agent_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ReAct Agent 不可用: 需安装 langgraph (prebuilt) + langchain + langchain_openai",
        )

    try:
        from agent.tools import AgentToolkit

        toolkit = AgentToolkit(
            memory=app_state.get_memory_store(),
            kb=app_state.get_kb_store(),
        )
        agent = create_evaluation_react_agent(
            toolkit=toolkit,
            model_router=app_state.model_router,
            settings=app_state.settings,
        )
        result = await _invoke(agent, payload.message, thread_id=payload.thread_id)
        return result
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("ReAct Agent 调用失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ReAct Agent 调用失败: {e}",
        )
