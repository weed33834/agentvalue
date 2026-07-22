"""
Tool Registry

封装 agent.react_agent.build_all_tools + LangChain → OpenAI function schema 转换。
供 agent.session_prompt 使用，让 LLM 能调用工具。

对齐 opencode (TypeScript/Effect) 的 packages/opencode/src/session/tools.ts SessionTools.resolve：
- resolve() → LangChain BaseTool 列表（内置 + toolkit + MCP + CustomTool）
- resolve_schemas() → OpenAI function schema 列表（传给 provider.stream_chat_completion(tools=...)）
- get_tool_by_name() → 按名查找工具实例（执行 tool_call 用）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.react_agent import build_all_tools
from agent.tools import AgentToolkit
from core.config import Settings

logger = logging.getLogger(__name__)

# 默认工具超时 (秒)
DEFAULT_TOOL_TIMEOUT = 60
# bash / command 类工具的默认超时 (秒, 更短以防止长时间运行)
COMMAND_TOOL_TIMEOUT = 30
# 匹配 bash / command 类工具名称的关键词
_COMMAND_TOOL_KEYWORDS = {"bash", "command", "shell", "exec", "terminal", "cmd"}


class ToolRegistry:
    """工具注册表（对齐 opencode ToolRegistry + SessionTools.resolve）"""

    def __init__(self, toolkit: Optional[AgentToolkit] = None, settings: Optional[Settings] = None):
        self.toolkit = toolkit
        self.settings = settings
        self._tools: List[Any] = []
        self._tools_by_name: Dict[str, Any] = {}
        self._loaded: bool = False
        # 工具超时配置: {tool_name: timeout_seconds}
        # 默认 60 秒, bash/command 类工具 30 秒
        self.tool_timeouts: Dict[str, int] = {}

    async def resolve(self) -> List[Any]:
        """加载所有工具（内置 + toolkit + MCP + CustomTool），返回 LangChain BaseTool 列表。"""
        if not self._loaded:
            self._tools = await build_all_tools(self.toolkit, self.settings)
            self._tools_by_name = {t.name: t for t in self._tools if hasattr(t, "name")}
            self._loaded = True
            logger.info("ToolRegistry 加载 %d 个工具: %s", len(self._tools), list(self._tools_by_name.keys()))
        return self._tools

    async def resolve_schemas(self) -> List[Dict[str, Any]]:
        """返回 OpenAI function schema 列表（传给 provider.stream_chat_completion(tools=...)）。"""
        tools = await self.resolve()
        schemas: List[Dict[str, Any]] = []
        for t in tools:
            schema = _convert_tool_to_openai_schema(t)
            if schema is not None:
                schemas.append(schema)
        return schemas

    def get_tool_by_name(self, name: str) -> Optional[Any]:
        """按名查找工具实例（执行 tool_call 用）。"""
        if not self._loaded:
            return None
        return self._tools_by_name.get(name)

    async def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具调用，返回 {output, error}。

        对齐 opencode processor.ts 的 tool 执行逻辑：
        - 找不到工具 → error
        - 执行超时 → 返回超时错误 (asyncio.wait_for 控制)
        - 执行成功 → output（字符串）
        - 执行异常 → error
        """
        tool = self.get_tool_by_name(name)
        if tool is None:
            return {"output": None, "error": f"工具 '{name}' 不存在"}

        # 获取该工具的超时时间
        timeout = self.get_tool_timeout(name)
        try:
            # 使用 asyncio.wait_for 做超时控制
            result = await asyncio.wait_for(tool.ainvoke(args), timeout=timeout)
            output = result if isinstance(result, str) else str(result)
            return {"output": output, "error": None}
        except asyncio.TimeoutError:
            logger.warning("工具 %s 执行超时 (超时 %ss)", name, timeout)
            return {
                "output": None,
                "error": "工具执行超时",
                "tool": name,
                "timeout": timeout,
            }
        except Exception as e:
            logger.warning("工具 %s 执行失败: %s", name, e, exc_info=True)
            return {"output": None, "error": str(e)}

    def set_tool_timeout(self, tool_name: str, timeout_seconds: int) -> None:
        """设置工具执行超时时间

        Args:
            tool_name: 工具名称。
            timeout_seconds: 超时秒数 (必须 > 0)。
        """
        if timeout_seconds <= 0:
            raise ValueError("超时时间必须大于 0 秒")
        self.tool_timeouts[tool_name] = timeout_seconds
        logger.info("设置工具 %s 超时为 %ss", tool_name, timeout_seconds)

    def get_tool_timeout(self, tool_name: str) -> int:
        """获取工具执行超时时间

        优先级: 自定义配置 > bash/command 类工具默认 30s > 全局默认 60s。

        Args:
            tool_name: 工具名称。

        Returns:
            超时秒数。
        """
        # 1. 自定义配置优先
        if tool_name in self.tool_timeouts:
            return self.tool_timeouts[tool_name]
        # 2. bash / command 类工具默认 30s
        name_lower = tool_name.lower()
        if any(kw in name_lower for kw in _COMMAND_TOOL_KEYWORDS):
            return COMMAND_TOOL_TIMEOUT
        # 3. 全局默认 60s
        return DEFAULT_TOOL_TIMEOUT

    def get_all_timeouts(self) -> Dict[str, int]:
        """获取所有已加载工具的超时配置

        Returns:
            {tool_name: timeout_seconds}
        """
        result: Dict[str, int] = {}
        for name in self._tools_by_name:
            result[name] = self.get_tool_timeout(name)
        return result


def _convert_tool_to_openai_schema(tool: Any) -> Optional[Dict[str, Any]]:
    """把 LangChain BaseTool 转成 OpenAI function schema。

    优先用 langchain 的 convert_to_openai_tool，失败时手动构造。
    """
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return convert_to_openai_tool(tool)
    except Exception as e:
        logger.debug("convert_to_openai_tool 失败 %s: %s, 手动构造", getattr(tool, "name", "?"), e)
        # 手动构造兜底
        name = getattr(tool, "name", None)
        if not name:
            return None
        description = getattr(tool, "description", "") or ""
        args_schema = getattr(tool, "args_schema", None)
        parameters = {"type": "object", "properties": {}}
        if args_schema is not None:
            try:
                parameters = args_schema.model_json_schema()
            except Exception:
                pass
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }
