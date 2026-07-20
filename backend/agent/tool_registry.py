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

import logging
from typing import Any, Dict, List, Optional

from agent.react_agent import build_all_tools
from agent.tools import AgentToolkit
from core.config import Settings

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册表（对齐 opencode ToolRegistry + SessionTools.resolve）"""

    def __init__(self, toolkit: Optional[AgentToolkit] = None, settings: Optional[Settings] = None):
        self.toolkit = toolkit
        self.settings = settings
        self._tools: List[Any] = []
        self._tools_by_name: Dict[str, Any] = {}
        self._loaded: bool = False

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
        - 执行成功 → output（字符串）
        - 执行异常 → error
        """
        tool = self.get_tool_by_name(name)
        if tool is None:
            return {"output": None, "error": f"工具 '{name}' 不存在"}
        try:
            result = await tool.ainvoke(args)
            output = result if isinstance(result, str) else str(result)
            return {"output": output, "error": None}
        except Exception as e:
            logger.warning("工具 %s 执行失败: %s", name, e, exc_info=True)
            return {"output": None, "error": str(e)}


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
