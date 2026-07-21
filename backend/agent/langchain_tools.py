"""LangChain @tool 工具定义 (P1 工具管理增强)

参考:
- LangChain Tools: https://docs.langchain.com/oss/python/langchain-tools
- LangGraph ToolNode: https://github.langchain.ac.cn/langgraph/how-tos/tool-calling/
- LangChain @tool decorator: https://python.langchain.ac.cn/docs/concepts/tools/

设计原则:
1. 内置工具用 @tool 装饰器自动生成 schema (name/description/args_schema)
2. 依赖 toolkit 实例的工具用闭包工厂模式创建 (运行时绑定 memory/kb)
3. LangChain 为可选依赖,未安装时降级返回空列表 (不阻断主流程)
4. 每个工具返回 str (JSON 序列化),便于 LLM 理解与 ToolMessage 传递
5. 异步工具 (_arun) 优先,同步兜底 (_run) 调 asyncio.run

对标 Dify/Coze 的工具管理:
- Dify: 内置工具 (google_search, wikipedia, calculator) + 自定义工具 + 工具测试
- Coze: 插件系统 (Plugin) + 工具定义 (input/output schema) + Bot 绑定工具
- 本模块: 内置工具 + MCP 外部工具 + Admin API 管理工具列表与测试
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from typing import Any, Dict, List, Optional

from agent.tools import AgentToolkit

logger = logging.getLogger(__name__)

# LangChain 为可选依赖: langgraph 依赖 langchain-core, 但完整 langchain 包需单独装
try:
    from langchain_core.tools import BaseTool, tool  # noqa: F401

    LANGCHAIN_TOOLS_AVAILABLE = True
except ImportError:
    LANGCHAIN_TOOLS_AVAILABLE = False
    BaseTool = object  # type: ignore[assignment, misc]


def _is_tool_enabled(tool_name: str, enabled_csv: Optional[str]) -> bool:
    """检查工具是否在启用列表中。enabled_csv=None 时全部启用。"""
    if not enabled_csv:
        return True
    enabled_set = {t.strip() for t in enabled_csv.split(",") if t.strip()}
    return tool_name in enabled_set


def _truncate_result(data: Any, max_chars: int = 4000) -> str:
    """截断工具返回结果,避免 token 膨胀。"""
    if isinstance(data, (dict, list)):
        s = json.dumps(data, ensure_ascii=False, default=str)
    else:
        s = str(data)
    if len(s) > max_chars:
        return s[:max_chars] + f"\n...[truncated, total {len(s)} chars]"
    return s


# ====== 内置独立工具 (不依赖 toolkit 实例) ======


def _build_builtin_tools(enabled_csv: Optional[str] = None) -> List[Any]:
    """创建不依赖 toolkit 的内置工具 (calculator / datetime / sentiment 等)。

    参考 Dify 内置工具集: calculator, datetime, wikipedia 等。
    参考 Coze 内置插件: 时间工具, 计算器, 文本处理。
    """
    if not LANGCHAIN_TOOLS_AVAILABLE:
        return []

    tools: List[Any] = []

    if _is_tool_enabled("calculator", enabled_csv):

        @tool
        def calculator(expression: str) -> str:
            """Calculate a mathematical expression. Supports +, -, *, /, **, sqrt, sin, cos, etc.

            Args:
                expression: Math expression, e.g. "2 + 3 * 4" or "math.sqrt(16)" or "math.sin(0)"
            """
            try:
                # 安全 eval: 仅允许 math 模块的函数与基本运算
                allowed_names = {
                    "math": math,
                    "abs": abs,
                    "round": round,
                    "min": min,
                    "max": max,
                    "sum": sum,
                }
                result = eval(expression, {"__builtins__": {}}, allowed_names)
                return f"结果: {result}"
            except Exception as e:
                return f"计算失败: {e}"

        tools.append(calculator)

    if _is_tool_enabled("datetime", enabled_csv):

        @tool
        def get_current_datetime(timezone_offset: Optional[int] = None) -> str:
            """Get current date and time.

            Args:
                timezone_offset: Optional UTC offset in hours (e.g. 8 for UTC+8).
                                If not provided, returns UTC time.
            """
            now = datetime.datetime.now(datetime.timezone.utc)
            if timezone_offset is not None:
                tz = datetime.timezone(datetime.timedelta(hours=timezone_offset))
                now = now.astimezone(tz)
            return now.strftime("%Y-%m-%d %H:%M:%S %Z")

        tools.append(get_current_datetime)

    # NOTE: bash/read_file/write_file/list_directory 已迁移到 file_tools.py
    # 新版本提供路径穿越防护、二进制检测、ripgrep集成等增强功能

    if _is_tool_enabled("web_fetch", enabled_csv):

        @tool
        async def web_fetch(url: str, max_length: int = 5000) -> str:
            """Fetch the content of a web page and return it as text.

            Args:
                url: The URL to fetch
                max_length: Maximum characters to return (default 5000)
            """
            try:
                import urllib.request

                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                # Simple HTML tag removal
                import re

                text = re.sub(
                    r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL
                )
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return _truncate_result(text, max_chars=max_length)
            except Exception as e:
                return f"Fetch failed: {e}"

        tools.append(web_fetch)

    if _is_tool_enabled("code_interpreter", enabled_csv):

        @tool
        async def code_interpreter(code: str) -> str:
            """Execute Python code in a sandbox. Supports math, statistics, data processing.

            Allowed modules: math, json, statistics, datetime, re, collections,
            itertools, functools. Dangerous modules (os, sys, subprocess) and
            builtins (open, exec, eval) are blocked. Execution timeout: 10s.

            Set the `__result__` variable in your code to return a structured value.
            print() output is captured as stdout.

            Args:
                code: Python code to execute
            """
            try:
                from agent.code_interpreter import CodeInterpreter

                interpreter = CodeInterpreter()
                result = await interpreter.execute(code)
                return _truncate_result(result, max_chars=8000)
            except Exception as e:
                logger.warning("code_interpreter 工具调用失败: %s", e)
                return f"Code execution failed: {e}"

        tools.append(code_interpreter)

    # ====== 文件操作工具 (移植自 opencode + Aider) ======
    if _is_tool_enabled("read_file", enabled_csv):
        from agent.file_tools import read_file as _read_file

        tools.append(_read_file)

    if _is_tool_enabled("write_file", enabled_csv):
        from agent.file_tools import write_file as _write_file

        tools.append(_write_file)

    if _is_tool_enabled("edit_file", enabled_csv):
        from agent.file_tools import edit_file as _edit_file

        tools.append(_edit_file)

    if _is_tool_enabled("list_directory", enabled_csv):
        from agent.file_tools import list_directory as _list_directory

        tools.append(_list_directory)

    if _is_tool_enabled("search_files", enabled_csv):
        from agent.file_tools import search_files as _search_files

        tools.append(_search_files)

    if _is_tool_enabled("run_command", enabled_csv):
        from agent.file_tools import run_command as _run_command

        tools.append(_run_command)

    if _is_tool_enabled("apply_patch", enabled_csv):
        from agent.file_tools import apply_patch as _apply_patch

        tools.append(_apply_patch)

    return tools


# ====== 依赖 toolkit 的工具 (闭包工厂) ======


def _build_toolkit_tools(
    toolkit: AgentToolkit, enabled_csv: Optional[str] = None
) -> List[Any]:
    """创建依赖 AgentToolkit 实例的工具 (employee_history / company_kb)。

    闭包模式: 在工厂函数内部定义 @tool, 捕获 toolkit 变量。
    运行时调用 toolkit.memory / toolkit.kb 的 async 方法。
    """
    if not LANGCHAIN_TOOLS_AVAILABLE:
        return []

    tools: List[Any] = []

    if _is_tool_enabled("employee_history", enabled_csv):

        @tool
        async def get_employee_history(
            employee_id: str, period: Optional[str] = None, limit: int = 5
        ) -> str:
            """Retrieve employee's historical evaluation records and memory.

            Useful for understanding past performance trends, growth trajectory,
            and context for the current evaluation period.

            Args:
                employee_id: The employee's unique identifier
                period: Optional period filter (e.g. "2025-W01"). If None, returns all periods.
                limit: Maximum number of records to return (default 5)
            """
            try:
                history = await toolkit.get_employee_history(
                    employee_id, period=period, limit=limit
                )
                return _truncate_result(history)
            except Exception as e:
                logger.warning("get_employee_history 工具调用失败: %s", e)
                return f"获取员工历史失败: {e}"

        tools.append(get_employee_history)

    if _is_tool_enabled("company_kb", enabled_csv):

        @tool
        async def query_company_kb(query: str, top_k: int = 3) -> str:
            """Search the company knowledge base for evaluation criteria, values, and policies.

            Returns relevant documents that define scoring standards, company culture,
            and growth framework guidelines.

            Args:
                query: Search query (e.g. "研发部评估标准" or "价值观考核")
                top_k: Maximum number of documents to return (default 3)
            """
            try:
                docs = await toolkit.query_company_kb(query, top_k=top_k)
                return _truncate_result(docs)
            except Exception as e:
                logger.warning("query_company_kb 工具调用失败: %s", e)
                return f"查询公司知识库失败: {e}"

        tools.append(query_company_kb)

    if _is_tool_enabled("grep_tool", enabled_csv):

        @tool
        async def grep_tool(
            pattern: str, path: str = ".", glob_pattern: str = "*"
        ) -> str:
            """Search for a text pattern (regex) in files.

            Uses ripgrep (rg) if available, falls back to Python re + os.walk.
            Returns matching lines in 'file:line:content' format (max 50 matches).

            Args:
                pattern: Regular expression pattern to search for
                path: Directory to search in (default current directory)
                glob_pattern: File name glob filter (default "*" for all files)
            """
            try:
                import fnmatch
                import os
                import re as re_module
                import shutil
                import subprocess

                matches: List[str] = []
                rg_path = shutil.which("rg")

                if rg_path:
                    # 优先使用 ripgrep
                    cmd = [
                        rg_path,
                        "--no-heading",
                        "-n",
                        pattern,
                        "-g",
                        glob_pattern,
                        path,
                    ]
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if proc.stdout:
                        for line in proc.stdout.splitlines()[:50]:
                            matches.append(line)
                else:
                    # 降级: Python re + os.walk
                    regex = re_module.compile(pattern)
                    for root, _dirs, files in os.walk(path):
                        for fname in files:
                            if not fnmatch.fnmatch(fname, glob_pattern):
                                continue
                            fpath = os.path.join(root, fname)
                            try:
                                with open(
                                    fpath, "r", encoding="utf-8", errors="replace"
                                ) as f:
                                    for lineno, line in enumerate(f, 1):
                                        if regex.search(line):
                                            matches.append(
                                                f"{fpath}:{lineno}:{line.rstrip()}"
                                            )
                                            if len(matches) >= 50:
                                                break
                            except (OSError, UnicodeDecodeError):
                                continue
                        if len(matches) >= 50:
                            break

                if not matches:
                    return f"No matches found for pattern '{pattern}' in {path}"
                return _truncate_result("\n".join(matches), max_chars=4000)
            except Exception as e:
                return f"Grep failed: {e}"

        tools.append(grep_tool)

    if _is_tool_enabled("glob_tool", enabled_csv):

        @tool
        async def glob_tool(pattern: str, path: str = ".") -> str:
            """Find files matching a glob pattern.

            Uses Python's glob module with recursive matching.
            Returns matching file paths (max 100 results).

            Args:
                pattern: Glob pattern (e.g. "**/*.py" for all Python files)
                path: Base directory to search in (default current directory)
            """
            try:
                import glob as glob_module
                import os

                full_pattern = (
                    os.path.join(path, pattern) if path != "." else pattern
                )
                matches = sorted(glob_module.glob(full_pattern, recursive=True))
                if not matches:
                    return f"No files found matching '{pattern}' in {path}"
                result = matches[:100]
                if len(matches) > 100:
                    result.append(f"... [{len(matches) - 100} more files]")
                return _truncate_result("\n".join(result), max_chars=4000)
            except Exception as e:
                return f"Glob failed: {e}"

        tools.append(glob_tool)

    if _is_tool_enabled("patch_tool", enabled_csv):

        @tool
        async def patch_tool(
            file_path: str, old_content: str, new_content: str
        ) -> str:
            """Patch a file by replacing exact string content.

            Performs exact string matching (not regex). Replaces all occurrences.
            Returns an error if old_content is not found in the file.

            Args:
                file_path: Path to the file to patch
                old_content: The exact string to find and replace
                new_content: The string to replace old_content with
            """
            try:
                import os

                if not os.path.exists(file_path):
                    return f"File not found: {file_path}"
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if old_content not in content:
                    return f"Error: old_content not found in {file_path}"
                count = content.count(old_content)
                new_text = content.replace(old_content, new_content)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                return (
                    f"Successfully patched {file_path}, "
                    f"replaced {count} occurrence(s)"
                )
            except Exception as e:
                return f"Patch failed: {e}"

        tools.append(patch_tool)

    if _is_tool_enabled("web_search_tool", enabled_csv):

        @tool
        async def web_search_tool(query: str, max_results: int = 5) -> str:
            """Search the web using DuckDuckGo (no API key required).

            Returns search results with title, URL, and snippet.

            Args:
                query: Search query string
                max_results: Maximum number of results to return (default 5)
            """
            try:
                import re as re_module
                import urllib.parse
                import urllib.request

                encoded_query = urllib.parse.quote(query)
                url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")

                # 解析 DuckDuckGo HTML 结果
                # 提取结果链接 (标题 + URL)
                link_pattern = re_module.compile(
                    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    re_module.DOTALL,
                )
                links = link_pattern.findall(html)

                # 提取摘要
                snippet_pattern = re_module.compile(
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    re_module.DOTALL,
                )
                snippets = snippet_pattern.findall(html)

                results: List[str] = []
                for i, (href, title) in enumerate(links):
                    if len(results) >= max_results:
                        break
                    # 清理标题中的 HTML 标签
                    clean_title = re_module.sub(r"<[^>]+>", "", title).strip()
                    snippet = ""
                    if i < len(snippets):
                        snippet = re_module.sub(
                            r"<[^>]+>", "", snippets[i]
                        ).strip()
                    # DuckDuckGo 通过 /l/?uddg= 重定向,提取真实 URL
                    if "uddg=" in href:
                        parsed = urllib.parse.parse_qs(
                            urllib.parse.urlparse(href).query
                        )
                        if "uddg" in parsed:
                            href = parsed["uddg"][0]
                    results.append(
                        f"{len(results) + 1}. {clean_title}\n"
                        f"   URL: {href}\n"
                        f"   {snippet}"
                    )

                if not results:
                    return f"No results found for '{query}'"
                return _truncate_result("\n".join(results), max_chars=4000)
            except Exception as e:
                logger.warning("web_search_tool 调用失败: %s", e)
                return (
                    "Web search unavailable. "
                    "Try web_fetch to fetch a specific URL."
                )

        tools.append(web_search_tool)

    return tools


# ====== 公共入口 ======


def build_langchain_tools(
    toolkit: Optional[AgentToolkit] = None,
    enabled_csv: Optional[str] = None,
) -> List[Any]:
    """构建 LangChain 工具列表 (内置 + toolkit 工具)。

    参考 LangChain ToolNode 文档:
    https://docs.langchain.com/oss/python/langchain-tools#toolnode

    Args:
        toolkit: AgentToolkit 实例 (提供 memory/kb)。None 时仅返回内置工具。
        enabled_csv: 启用的工具名 (逗号分隔),None 时全部启用。

    Returns:
        LangChain BaseTool 列表,可直接传给 bind_tools() 或 ToolNode。
        LangChain 未安装时返回空列表。
    """
    if not LANGCHAIN_TOOLS_AVAILABLE:
        logger.debug("langchain_core 未安装,build_langchain_tools 返回空列表")
        return []

    tools: List[Any] = []
    tools.extend(_build_builtin_tools(enabled_csv))
    if toolkit is not None:
        tools.extend(_build_toolkit_tools(toolkit, enabled_csv))
    return tools


def list_available_tools(enabled_csv: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出所有可用工具的元数据 (供 Admin API 展示)。

    不需要 toolkit 实例,仅返回 schema 信息。
    对标 Dify 工具市场: 展示工具名/描述/参数 schema/启用状态。
    """
    # 先用空 toolkit 构建,取 schema
    tools = build_langchain_tools(toolkit=None, enabled_csv=None)
    result: List[Dict[str, Any]] = []
    for t in tools:
        result.append(
            {
                "name": getattr(t, "name", str(t)),
                "description": getattr(t, "description", ""),
                "args_schema": _safe_schema_dict(t),
                "category": "builtin",
                "enabled": _is_tool_enabled(getattr(t, "name", ""), enabled_csv),
            }
        )
    # toolkit 工具的 schema (即使无 toolkit 也能展示元数据)
    for name, desc, args in _TOOLKIT_TOOL_META:
        result.append(
            {
                "name": name,
                "description": desc,
                "args_schema": args,
                "category": "toolkit",
                "enabled": _is_tool_enabled(name, enabled_csv),
            }
        )
    return result


def _safe_schema_dict(t: Any) -> Dict[str, Any]:
    """安全提取工具的 args_schema 为 dict"""
    schema = getattr(t, "args_schema", None)
    if schema is None:
        return {}
    try:
        if hasattr(schema, "model_json_schema"):
            return schema.model_json_schema()
        if hasattr(schema, "schema"):
            return schema.schema()
    except Exception:
        pass
    return {}


# toolkit 工具的元数据 (用于 list_available_tools 在无实例时展示)
_TOOLKIT_TOOL_META: List = [
    (
        "get_employee_history",
        "Retrieve employee's historical evaluation records and memory.",
        {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "员工 ID"},
                "period": {"type": "string", "description": "周期 (如 2025-W01)"},
                "limit": {"type": "integer", "default": 5, "description": "最大返回数"},
            },
            "required": ["employee_id"],
        },
    ),
    (
        "query_company_kb",
        "Search the company knowledge base for evaluation criteria and policies.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "default": 3, "description": "最大返回数"},
            },
            "required": ["query"],
        },
    ),
    # bash / read_file / write_file / list_directory / web_fetch 的元数据
    # 已由 _build_builtin_tools() 中的 @tool 装饰器自动生成，无需在此重复定义
    # grep_tool / glob_tool / patch_tool / web_search_tool 的元数据
    (
        "grep_tool",
        "Search for a text pattern (regex) in files using ripgrep or Python fallback.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "搜索路径",
                },
                "glob_pattern": {
                    "type": "string",
                    "default": "*",
                    "description": "文件名过滤 (glob)",
                },
            },
            "required": ["pattern"],
        },
    ),
    (
        "glob_tool",
        "Find files matching a glob pattern using Python glob module.",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式 (如 **/*.py)",
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "搜索路径",
                },
            },
            "required": ["pattern"],
        },
    ),
    (
        "patch_tool",
        "Patch a file by exact string replacement (not regex).",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "old_content": {"type": "string", "description": "要替换的原文"},
                "new_content": {"type": "string", "description": "替换为的新内容"},
            },
            "required": ["file_path", "old_content", "new_content"],
        },
    ),
    (
        "web_search_tool",
        "Search the web using DuckDuckGo (no API key required).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "最大返回数",
                },
            },
            "required": ["query"],
        },
    ),
]
