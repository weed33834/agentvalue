"""MCP (Model Context Protocol) 客户端管理 (P1 工具管理增强)

参考:
- LangChain MCP Adapters: https://github.com/langchain-ai/langchain-mcp-adapters
- MCP 官方文档: https://modelcontextprotocol.io/introduction
- MultiServerMCPClient: https://pypi.org/project/langchain-mcp-adapters/

设计原则:
1. MCP 适配器为可选依赖,未安装时降级返回空工具列表
2. 支持多 MCP 服务器 (stdio / streamable_http / sse)
3. 懒加载: 首次调用 get_tools() 时才建立连接
4. 连接失败不阻断主流程,记录 warning 并返回空列表
5. 提供 list_servers / test_connection 管理接口

对标 Dify 的工具接入:
- Dify 支持 OpenAPI schema 转工具, 也支持 MCP
- Coze 支持插件市场, 工具通过插件形式接入
- 本模块: 通过 MCP 协议统一接入外部工具服务器

配置示例 (mcp_servers JSON):
{
    "jira": {
        "transport": "streamable_http",
        "url": "http://localhost:8001/mcp"
    },
    "filesystem": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data"]
    },
    "github": {
        "transport": "sse",
        "url": "https://mcp.github.com/sse",
        "headers": {"Authorization": "Bearer xxx"}
    }
}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# MCP 适配器为可选依赖
try:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    MultiServerMCPClient = None  # type: ignore[assignment, misc]


class MCPClientManager:
    """MCP 多服务器客户端管理器

    用法:
        manager = MCPClientManager(settings)
        await manager.initialize()  # 建立连接
        tools = await manager.get_tools()  # 获取 LangChain 工具
        servers = manager.list_servers()  # 列出已配置服务器
        await manager.test_connection("jira")  # 测试单个服务器
        await manager.close()  # 关闭所有连接
    """

    def __init__(self, mcp_servers_json: Optional[str] = None):
        self._raw_config = mcp_servers_json
        self._servers_config: Dict[str, Dict[str, Any]] = {}
        self._client: Optional[Any] = None
        self._initialized = False
        self._tools_cache: Optional[List[Any]] = None

        if mcp_servers_json:
            try:
                self._servers_config = json.loads(mcp_servers_json)
                if not isinstance(self._servers_config, dict):
                    logger.warning(
                        "mcp_servers JSON 必须为对象,实际为 %s",
                        type(self._servers_config).__name__,
                    )
                    self._servers_config = {}
            except json.JSONDecodeError as e:
                logger.warning("mcp_servers JSON 解析失败: %s", e)
                self._servers_config = {}

    def is_available(self) -> bool:
        """MCP 适配器是否可用 (已安装 langchain-mcp-adapters)"""
        return MCP_AVAILABLE

    def has_servers(self) -> bool:
        """是否配置了 MCP 服务器"""
        return bool(self._servers_config)

    def list_servers(self) -> List[Dict[str, Any]]:
        """列出所有已配置的 MCP 服务器 (供 Admin API 展示)"""
        result: List[Dict[str, Any]] = []
        for name, config in self._servers_config.items():
            transport = config.get("transport", config.get("type", "unknown"))
            entry: Dict[str, Any] = {
                "name": name,
                "transport": transport,
                "initialized": self._initialized,
                "available": self.is_available(),
            }
            if "url" in config:
                entry["url"] = config["url"]
            if "command" in config:
                entry["command"] = config["command"]
                entry["args"] = config.get("args", [])
            if "headers" in config:
                # 隐去 Authorization 等敏感 header
                safe_headers = {}
                for k, v in config["headers"].items():
                    if k.lower() in ("authorization", "x-api-key", "token"):
                        safe_headers[k] = "***"
                    else:
                        safe_headers[k] = v
                entry["headers"] = safe_headers
            result.append(entry)
        return result

    async def initialize(self) -> None:
        """初始化 MCP 客户端,建立与所有服务器的连接。

        失败时记录 warning 但不抛异常,确保主流程不受影响。
        """
        if not MCP_AVAILABLE:
            logger.warning("langchain-mcp-adapters 未安装,MCP 工具不可用")
            return
        if not self._servers_config:
            logger.debug("未配置 MCP 服务器,跳过初始化")
            return
        if self._initialized:
            return

        try:
            self._client = MultiServerMCPClient(self._servers_config)
            # get_tools() 会触发连接建立
            self._tools_cache = await self._client.get_tools()
            self._initialized = True
            tool_names = [getattr(t, "name", str(t)) for t in (self._tools_cache or [])]
            logger.info(
                "MCP 客户端初始化成功,加载 %d 个工具: %s",
                len(self._tools_cache or []),
                tool_names,
            )
        except Exception as e:
            logger.warning("MCP 客户端初始化失败 (主流程不受影响): %s", e)
            self._client = None
            self._initialized = False

    async def get_tools(self) -> List[Any]:
        """获取所有 MCP 服务器提供的 LangChain 工具。

        首次调用会触发 initialize()。后续调用返回缓存。
        失败时返回空列表。
        """
        if not self._initialized:
            await self.initialize()
        return self._tools_cache or []

    async def test_connection(self, server_name: str) -> Dict[str, Any]:
        """测试单个 MCP 服务器连接 (供 Admin API 调用)

        独立于已初始化的客户端,创建临时连接测试。
        """
        if not MCP_AVAILABLE:
            return {
                "server": server_name,
                "connected": False,
                "error": "langchain-mcp-adapters 未安装",
            }
        if server_name not in self._servers_config:
            return {
                "server": server_name,
                "connected": False,
                "error": f"服务器 {server_name} 未配置",
            }

        config = {server_name: self._servers_config[server_name]}
        try:
            tmp_client = MultiServerMCPClient(config)
            tools = await tmp_client.get_tools()
            return {
                "server": server_name,
                "connected": True,
                "tool_count": len(tools),
                "tool_names": [getattr(t, "name", str(t)) for t in tools],
            }
        except Exception as e:
            return {
                "server": server_name,
                "connected": False,
                "error": str(e),
            }

    async def reload_config(self, mcp_servers_json: Optional[str]) -> None:
        """重新加载配置 (Admin API 修改后热更新)

        关闭旧连接,加载新配置,下次 get_tools() 时重新初始化。
        """
        await self.close()
        self._raw_config = mcp_servers_json
        self._servers_config = {}
        if mcp_servers_json:
            try:
                self._servers_config = json.loads(mcp_servers_json)
            except json.JSONDecodeError as e:
                logger.warning("reload_config: JSON 解析失败: %s", e)

    async def close(self) -> None:
        """关闭所有 MCP 连接"""
        # MultiServerMCPClient 目前无显式 close,依赖 GC
        # 未来版本可能增加 close 方法,此处预留
        self._client = None
        self._initialized = False
        self._tools_cache = None


# 全局单例
_global_mcp_manager: Optional[MCPClientManager] = None


def get_global_mcp_manager(mcp_servers_json: Optional[str] = None) -> MCPClientManager:
    """获取全局 MCPClientManager 单例。

    首次调用传入配置时初始化。后续调用复用单例。
    若需热更新配置,调用 reload_config()。
    """
    global _global_mcp_manager
    if _global_mcp_manager is None:
        _global_mcp_manager = MCPClientManager(mcp_servers_json)
    return _global_mcp_manager
