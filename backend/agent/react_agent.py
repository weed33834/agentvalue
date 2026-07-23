"""ReAct Agent with ToolNode (P1 工具管理增强)

参考:
- LangGraph ToolNode: https://github.langchain.ac.cn/langgraph/how-tos/tool-calling/
- LangGraph create_react_agent: https://langgraph.com.cn/agents/tools.1.html
- LangChain ReAct 模式: https://docs.langchain.com/oss/python/langchain-tools#using-tools-with-agents

设计原则:
1. ReAct Agent 与固定评估流水线并存:
   - 固定流水线 (graph.py create_evaluation_graph): 确定性评估流程
   - ReAct Agent (本模块): 复杂推理任务,如"分析某员工本周表现并给出改进建议"
2. 使用 langgraph.prebuilt.create_react_agent + bind_tools 模式
3. LangChain 为可选依赖,未安装时抛出清晰错误
4. 工具列表 = 内置工具 + toolkit 工具 + MCP 外部工具
5. 支持迭代上限防死循环,超限返回当前最佳结果

ReAct 循环 (Reason + Act):
    用户问题 → LLM 思考 → 调用工具 → 观察结果 → LLM 再思考 → ... → 最终答案

对标 Dify/Coze:
- Dify: Agent 节点支持 ReAct/Function Calling 两种模式
- Coze: Bot 支持 Auto 模式 (LLM 自主选择工具) 与 工作流模式
- 本模块: ReAct 模式,LLM 自主决定何时调用哪个工具
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.langchain_tools import (
    LANGCHAIN_TOOLS_AVAILABLE,
    build_langchain_tools,
)
from agent.tools import AgentToolkit
from core.config import Settings
from core.model_router import ModelRouter

logger = logging.getLogger(__name__)

# LangGraph prebuilt 为可选 (langgraph 已在 requirements 中,但 create_react_agent 可能版本差异)
try:
    from langgraph.prebuilt import create_react_agent

    REACT_AGENT_AVAILABLE = True
except ImportError:
    REACT_AGENT_AVAILABLE = False
    create_react_agent = None  # type: ignore[assignment, misc]


def is_react_agent_available() -> bool:
    """ReAct Agent 是否可用 (依赖 langgraph.prebuilt + langchain_core)"""
    return REACT_AGENT_AVAILABLE and LANGCHAIN_TOOLS_AVAILABLE


async def build_all_tools(
    toolkit: Optional[AgentToolkit] = None,
    settings: Optional[Settings] = None,
) -> List[Any]:
    """构建完整工具列表: 内置 + toolkit + MCP + 自定义工具

    参考 LangChain bind_tools 用法:
    https://python.langchain.ac.cn/docs/how_to/tool_calling/

    Args:
        toolkit: AgentToolkit 实例 (memory/kb)
        settings: 配置 (含 enabled_tools / mcp_servers)

    Returns:
        LangChain BaseTool 列表
    """
    enabled_csv = getattr(settings, "enabled_tools", None) if settings else None
    tools: List[Any] = []

    # 1) 内置 + toolkit 工具
    tools.extend(build_langchain_tools(toolkit=toolkit, enabled_csv=enabled_csv))

    # 2) MCP 外部工具 (懒加载)
    if settings and getattr(settings, "mcp_servers", None):
        from agent.mcp_client import get_global_mcp_manager

        manager = get_global_mcp_manager(settings.mcp_servers)
        mcp_tools = await manager.get_tools()
        tools.extend(mcp_tools)
        if mcp_tools:
            logger.info("加载 %d 个 MCP 外部工具", len(mcp_tools))

    # 3) P3-1: 自定义工具 (OpenAPI Schema 导入,对标 Dify Custom Tool)
    custom_tools_count = await _load_custom_tools(tools)
    if custom_tools_count:
        logger.info("加载 %d 个自定义工具 (OpenAPI)", custom_tools_count)

    return tools


async def _load_custom_tools(tools: List[Any]) -> int:
    """加载所有 enabled 的 CustomTool,转换为 LangChain BaseTool 加入 toolkit

    设计:
    - DB 不可达时静默跳过 (不阻断 ReAct Agent 启动)
    - 单个工具解析失败时记 warning 跳过,不影响其他工具
    - 凭证用 FieldCipher 解密后注入 AuthConfig

    Returns:
        成功加载的工具数 (含每个 CustomTool 解析出的多个 operation)
    """
    try:
        from core.database import AsyncSessionLocal
        from core.field_crypto import get_field_cipher
        from core.tools.openapi_parser import (
            AuthConfig,
            build_langchain_tool,
            parse_openapi_to_tools,
        )
        from models.custom_tool import CustomTool
        from sqlalchemy import select
    except ImportError:
        # 依赖未就绪 (如 langchain_core 未装),静默跳过
        return 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CustomTool).where(CustomTool.enabled == True)  # noqa: E712
            )
            rows = result.scalars().all()
    except Exception as e:
        logger.warning("加载自定义工具失败 (DB 不可达?): %s", e)
        return 0

    cipher = get_field_cipher()
    count = 0
    for entity in rows:
        try:
            specs = parse_openapi_to_tools(entity.openapi_schema, entity.base_url)
            auth = AuthConfig(
                auth_type=entity.auth_type,
                credentials=(
                    cipher.decrypt(entity.auth_credentials)
                    if entity.auth_credentials
                    else None
                ),
            )
            for spec in specs:
                try:
                    tool = build_langchain_tool(spec, auth=auth)
                    tools.append(tool)
                    count += 1
                except Exception as e:
                    logger.warning(
                        "构建自定义工具 %s 的 operation %s 失败: %s",
                        entity.name,
                        spec.name,
                        e,
                    )
        except Exception as e:
            logger.warning("解析自定义工具 %s 的 OpenAPI spec 失败: %s", entity.name, e)
    return count


def create_evaluation_react_agent(
    toolkit: AgentToolkit,
    model_router: ModelRouter,
    settings: Optional[Settings] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """创建 ReAct Agent,用于复杂评估推理任务。

    与 create_evaluation_graph (固定流水线) 的区别:
    - 固定流水线: 9 个节点顺序执行,适合标准化评估流程
    - ReAct Agent: LLM 自主决定调用哪些工具、调用几次,适合:
      * "分析张三本周表现,对比历史,给出改进建议"
      * "查询公司知识库中研发部评估标准,并评估李四"
      * "综合多源信息生成本季度团队评估报告"

    参考 create_react_agent 文档:
    https://langgraph.com.cn/agents/agents.1.html

    Args:
        toolkit: AgentToolkit (memory/kb)
        model_router: ModelRouter (提供 LLM)
        settings: 配置 (enabled_tools / mcp_servers / react_agent_max_iterations)
        system_prompt: 自定义系统提示 (None 时用默认)

    Returns:
        编译后的 LangGraph agent (可 ainvoke)

    Raises:
        RuntimeError: LangGraph/langchain 未安装
    """
    if not is_react_agent_available():
        raise RuntimeError(
            "ReAct Agent 不可用: 需安装 langgraph (含 prebuilt) + langchain-core。"
            "运行 pip install langgraph langchain"
        )

    import asyncio

    async def _build():
        # 1) 构建工具列表
        tools = await build_all_tools(toolkit, settings)
        if not tools:
            logger.warning("ReAct Agent 无可用工具,将仅依赖 LLM 自身能力")

        # 2) 获取 LLM (从 model_router 取最佳 provider)
        provider, tier = await model_router.get_provider_with_fallback()

        # 3) 把 provider 适配为 LangChain BaseChatModel (供 bind_tools 用)
        # OpenAICompatibleProvider 内部用 openai SDK,这里构造 ChatOpenAI 复用其配置
        llm = _build_langchain_chat_model(provider)
        if llm is None:
            raise RuntimeError(
                f"无法为 provider {type(provider).__name__} 构造 LangChain ChatModel,"
                "ReAct Agent 需要 OpenAI 兼容 provider"
            )

        # 4) 默认系统提示
        if system_prompt is None:
            default_sp = (
                "你是 AgentValue-AI 评估助手。你可以调用工具获取员工历史、"
                "查询公司知识库,进行综合分析后给出评估建议。"
                "回答时引用具体数据与证据,避免臆测。"
            )
        else:
            default_sp = system_prompt

        # 5) 迭代上限
        max_iter = (
            getattr(settings, "react_agent_max_iterations", 10) if settings else 10
        )

        # 6) 创建 ReAct Agent
        agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=default_sp,
            recursion_limit=max_iter * 2 + 5,  # 每轮迭代约 2 步 (reason + act)
        )
        return agent

    # create_react_agent 是同步的,但工具构建是异步的,用 asyncio 桥接
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已在事件循环中 (如 FastAPI),返回协程让调用方 await
            return _build()
        else:
            return loop.run_until_complete(_build())
    except RuntimeError:
        # 无事件循环,创建新的
        return asyncio.run(_build())


def _build_langchain_chat_model(provider: Any) -> Optional[Any]:
    """把 OpenAICompatibleProvider 适配为 LangChain ChatOpenAI

    LangChain 的 bind_tools 需要 BaseChatModel 实例。
    我们复用 provider 的 OpenAI client 配置,构造 ChatOpenAI。

    参考 LangChain ChatOpenAI:
    https://python.langchain.ac.cn/docs/integrations/chat/openai/
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning("langchain_openai 未安装,无法构造 ChatOpenAI for ReAct Agent")
        return None

    # 从 provider 提取配置
    config = getattr(provider, "config", None)
    if config is None:
        logger.warning("provider 无 config 属性,无法构造 ChatOpenAI")
        return None

    api_key = getattr(config, "api_key", None) or "dummy"
    base_url = getattr(config, "base_url", None) or "https://api.openai.com/v1"
    model_name = getattr(config, "model_name", "gpt-4o-mini")

    # 从 provider 的 client 复用 (避免重复建 client)
    client = getattr(provider, "_client", None)

    kwargs: Dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": 0.3,  # ReAct 适度随机
    }
    if client is not None:
        # 复用 httpx client (含连接池/超时配置)
        kwargs["http_client"] = getattr(client, "_client", None) or client

    try:
        return ChatOpenAI(**kwargs)
    except Exception as e:
        logger.warning("构造 ChatOpenAI 失败: %s", e)
        return None


async def invoke_react_agent(
    agent: Any,
    user_message: str,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 ReAct Agent 并返回结构化结果。

    Args:
        agent: create_evaluation_react_agent 返回的 agent
        user_message: 用户问题 (如 "分析张三本周表现")
        thread_id: 会话 ID (用于 checkpointer 多轮对话,可选)

    Returns:
        {"messages": [...], "final_answer": "...", "tool_calls_made": [...]}
    """
    config: Dict[str, Any] = {}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config if config else None,
    )

    # 提取最终答案与工具调用记录
    messages = result.get("messages", [])
    final_answer = ""
    tool_calls_made: List[Dict[str, Any]] = []

    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content and not getattr(msg, "tool_calls", None):
            final_answer = content
            break

    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                tool_calls_made.append(
                    {
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                        "id": tc.get("id", ""),
                    }
                )

    return {
        "messages": [
            {
                "role": getattr(m, "type", "unknown"),
                "content": getattr(m, "content", ""),
            }
            for m in messages
        ],
        "final_answer": final_answer,
        "tool_calls_made": tool_calls_made,
        "message_count": len(messages),
    }
