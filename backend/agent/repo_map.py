"""Repo Map - 代码库结构映射

对标 Cursor/Aider 的 repo map 功能:
- 扫描代码库目录结构
- 提取关键符号(类、函数、方法定义)
- 生成简洁的结构概览供Agent使用

设计要点:
1. 扫描时排除构建产物、依赖目录、数据库文件等无关文件
2. 用正则提取 Python/JS/Vue 的类和函数定义, 不依赖 AST 解析 (轻量)
3. 限制扫描文件数和符号数, 避免 token 膨胀
4. 生成文本格式的 repo map 供 LLM 上下文使用 (类似 Aider 的 --map-tokens)
5. 提供 LangChain @tool 包装, 供 Agent 按需调用

LangChain 为可选依赖, 未安装时降级返回 None (不阻断主流程)。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# LangChain 为可选依赖
try:
    from langchain_core.tools import tool  # noqa: F401

    LANGCHAIN_TOOLS_AVAILABLE = True
except ImportError:
    LANGCHAIN_TOOLS_AVAILABLE = False

# ====== 排除规则 ======

# 排除的目录名 (精确匹配)
_EXCLUDED_DIRS = {
    "__pycache__",
    "node_modules",
    ".git",
    ".venv",
    "venv",
    ".env",
    "dist",
    "build",
    "chroma_db",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    ".idea",
    ".vscode",
    "htmlcov",
    "coverage",
    ".next",
    ".nuxt",
    ".cache",
    "target",
    "bin",
    "obj",
}

# 排除的目录名后缀 (glob 风格, 如 *.egg-info)
_EXCLUDED_DIR_SUFFIXES = (
    ".egg-info",
    ".egg-link",
)

# 排除的文件扩展名
_EXCLUDED_FILE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    ".db",
    ".sqlite3",
    ".sqlite",
    ".shm",
    ".wal",
    ".so",
    ".dll",
    ".dylib",
    ".class",
    ".o",
    ".a",
    ".wasm",
    ".lock",
    ".pid",
    ".tmp",
    ".swp",
    ".swo",
}

# 支持符号提取的文件扩展名 → 语言
_SYMBOL_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}

# 每个文件最多提取的符号数量
_MAX_SYMBOLS_PER_FILE = 20

# ====== 符号提取正则 ======

# Python: class / def / async def
_PY_CLASS_RE = re.compile(r"^class\s+(\w+)")
_PY_DEF_RE = re.compile(r"^\s*(async\s+)?def\s+(\w+)")
# Python: 顶层变量赋值 (常量, 如 MAX_RETRIES = 100) — 仅 ALL_CAPS
_PY_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*")

# JS/TS: function / async function / export function
_JS_FUNCTION_RE = re.compile(
    r"^(export\s+)?(default\s+)?(async\s+)?function\s+(\w+)"
)
# JS/TS: class / export class
_JS_CLASS_RE = re.compile(r"^(export\s+)?(default\s+)?(abstract\s+)?class\s+(\w+)")
# JS/TS: 箭头函数 const myFunc = (args) => {  或  const myFunc = async (args) =>
_JS_ARROW_RE = re.compile(
    r"^\s*(export\s+)?(const|let)\s+(\w+)\s*=\s*(async\s*)?\("
)
# JS/TS: 方法简写 (对象方法)  myMethod(args) {
_JS_METHOD_RE = re.compile(r"^\s*(async\s+)?(\w+)\s*\([^)]*\)\s*\{")
# Vue: defineComponent / export default
_VUE_COMPONENT_RE = re.compile(r"^\s*(export\s+)?default\s+defineComponent\s*\(")


class RepoMap:
    """代码库结构映射

    扫描代码库目录, 提取关键符号, 生成结构概览。

    用法:
        rm = RepoMap("/path/to/project")
        result = rm.scan(max_files=200, max_depth=5)
        text = rm.to_text(result)  # 供 LLM 上下文使用
    """

    def __init__(self, root_dir: str = "."):
        """初始化 RepoMap。

        Args:
            root_dir: 代码库根目录路径
        """
        self.root_dir = os.path.abspath(root_dir)

    # ====== 公共方法 ======

    def scan(self, max_files: int = 200, max_depth: int = 5) -> Dict[str, Any]:
        """扫描代码库, 返回结构化概览。

        Args:
            max_files: 最多扫描的文件数 (防止超大仓库)
            max_depth: 目录递归最大深度

        Returns:
            {
                "root": str,           # 根目录绝对路径
                "tree": dict,          # 嵌套目录树
                "symbols": list,       # 关键符号列表
                "stats": dict,         # 统计信息
            }
        """
        symbols: List[Dict[str, Any]] = []
        stats = {
            "total_files": 0,
            "total_dirs": 0,
            "total_symbols": 0,
            "languages": {},
        }

        # 扫描计数器 (用 list 包裹以便在闭包中修改)
        file_counter = [0]

        tree = self._scan_directory(
            self.root_dir, 0, max_depth, max_files, file_counter, symbols, stats
        )

        stats["total_symbols"] = len(symbols)

        return {
            "root": self.root_dir,
            "tree": tree,
            "symbols": symbols,
            "stats": stats,
        }

    def to_text(self, scan_result: Dict[str, Any]) -> str:
        """将扫描结果转换为文本格式的 repo map (供 LLM 上下文)。

        Args:
            scan_result: scan() 方法的返回值

        Returns:
            文本格式的 repo map 字符串
        """
        root = scan_result.get("root", "")
        tree = scan_result.get("tree", {})
        symbols = scan_result.get("symbols", [])
        stats = scan_result.get("stats", {})

        lines: List[str] = []
        lines.append(f"Repository: {root}")
        lines.append(
            f"Stats: {stats.get('total_files', 0)} files, "
            f"{stats.get('total_dirs', 0)} directories, "
            f"{stats.get('total_symbols', 0)} symbols"
        )

        # 语言分布
        languages = stats.get("languages", {})
        if languages:
            lang_parts = [f"{lang}({count})" for lang, count in sorted(languages.items(), key=lambda x: -x[1])]
            lines.append(f"Languages: {', '.join(lang_parts)}")

        lines.append("")
        lines.append("Directory Tree:")
        if tree:
            tree_text = self._format_tree(tree, indent=1)
            lines.append(tree_text)
        else:
            lines.append("  (empty)")

        lines.append("")
        lines.append("Key Symbols:")
        if symbols:
            # 按文件分组
            by_file: Dict[str, List[Dict[str, Any]]] = {}
            for sym in symbols:
                fpath = sym.get("file", "")
                by_file.setdefault(fpath, []).append(sym)

            for fpath, syms in sorted(by_file.items()):
                # 相对路径, 更简洁
                rel_path = self._relative_path(fpath)
                lines.append(f"  {rel_path}:")
                for sym in syms:
                    sym_type = sym.get("type", "def")
                    name = sym.get("name", "")
                    line_no = sym.get("line", 0)
                    signature = sym.get("signature", "")
                    sig_str = f" {signature}" if signature else ""
                    lines.append(f"    L{line_no}: {sym_type} {name}{sig_str}")
        else:
            lines.append("  (no symbols found)")

        return "\n".join(lines)

    # ====== 内部方法 ======

    def _scan_directory(
        self,
        path: str,
        depth: int,
        max_depth: int,
        max_files: int,
        file_counter: List[int],
        symbols: List[Dict[str, Any]],
        stats: Dict[str, Any],
    ) -> Dict[str, Any]:
        """递归扫描目录。

        Args:
            path: 当前目录路径
            depth: 当前深度 (0 = root)
            max_depth: 最大递归深度
            max_files: 最大文件数
            file_counter: 文件计数器 (list 包裹以便修改)
            symbols: 符号收集列表
            stats: 统计信息字典

        Returns:
            目录树节点 {"name", "type", "children"}
        """
        dir_name = os.path.basename(path) or path

        # 深度限制
        if depth > max_depth:
            return {"name": dir_name, "type": "dir", "children": []}

        # 文件数限制
        if file_counter[0] >= max_files:
            return {"name": dir_name, "type": "dir", "children": []}

        node: Dict[str, Any] = {
            "name": dir_name,
            "type": "dir",
            "children": [],
        }

        try:
            entries = sorted(os.listdir(path))
        except (PermissionError, OSError) as e:
            logger.debug("无法访问目录 %s: %s", path, e)
            return node

        stats["total_dirs"] += 1

        for entry in entries:
            if file_counter[0] >= max_files:
                break

            full_path = os.path.join(path, entry)

            # 跳过排除的目录
            if os.path.isdir(full_path):
                if self._is_excluded_dir(entry):
                    continue
                child = self._scan_directory(
                    full_path,
                    depth + 1,
                    max_depth,
                    max_files,
                    file_counter,
                    symbols,
                    stats,
                )
                node["children"].append(child)
            elif os.path.isfile(full_path):
                if self._is_excluded_file(entry):
                    continue
                file_counter[0] += 1
                stats["total_files"] += 1

                # 统计语言
                ext = os.path.splitext(entry)[1].lower()
                lang = _SYMBOL_LANGUAGES.get(ext)
                if lang:
                    stats["languages"][lang] = stats["languages"].get(lang, 0) + 1

                # 添加文件节点
                file_node = {"name": entry, "type": "file", "children": []}
                node["children"].append(file_node)

                # 提取符号
                if lang:
                    try:
                        file_symbols = self._extract_symbols(full_path)
                        for sym in file_symbols:
                            sym["file"] = full_path
                            symbols.append(sym)
                    except Exception as e:
                        logger.debug("符号提取失败 %s: %s", full_path, e)

        return node

    def _is_excluded_dir(self, dir_name: str) -> bool:
        """检查目录是否应被排除"""
        if dir_name in _EXCLUDED_DIRS:
            return True
        if dir_name.startswith("."):
            # 隐藏目录 (但允许 . (当前目录) 和 .. (父目录) — 这两个不会出现在 listdir 中)
            return True
        for suffix in _EXCLUDED_DIR_SUFFIXES:
            if dir_name.endswith(suffix):
                return True
        return False

    def _is_excluded_file(self, file_name: str) -> bool:
        """检查文件是否应被排除"""
        ext = os.path.splitext(file_name)[1].lower()
        if ext in _EXCLUDED_FILE_EXTENSIONS:
            return True
        # 排除常见的非代码大文件
        if file_name in (".DS_Store", "Thumbs.db", "package-lock.json", "yarn.lock"):
            return True
        return False

    def _extract_symbols(self, file_path: str) -> List[Dict[str, Any]]:
        """用正则提取文件的类和函数定义。

        支持 Python / JS / TS / Vue 文件。

        Args:
            file_path: 文件路径

        Returns:
            符号列表 [{file, line, type, name, signature}]
            type: "class" / "def" / "const"
        """
        ext = os.path.splitext(file_path)[1].lower()
        language = _SYMBOL_LANGUAGES.get(ext)

        if language is None:
            return []

        symbols: List[Dict[str, Any]] = []

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            logger.debug("读取文件失败 %s: %s", file_path, e)
            return []

        for lineno, line in enumerate(lines, 1):
            if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
                break

            line_stripped = line.rstrip("\n\r")

            if language == "python":
                sym = self._extract_python_symbol(line_stripped, lineno)
            elif language in ("javascript", "typescript"):
                sym = self._extract_js_symbol(line_stripped, lineno)
            elif language == "vue":
                sym = self._extract_vue_symbol(line_stripped, lineno)
            else:
                sym = None

            if sym:
                symbols.append(sym)

        return symbols

    def _extract_python_symbol(
        self, line: str, lineno: int
    ) -> Optional[Dict[str, Any]]:
        """提取 Python 符号 (class / def / async def / 常量)"""
        # class
        m = _PY_CLASS_RE.match(line)
        if m:
            name = m.group(1)
            signature = self._extract_signature_after(line, name)
            return {
                "line": lineno,
                "type": "class",
                "name": name,
                "signature": signature,
            }
        # def / async def
        m = _PY_DEF_RE.match(line)
        if m:
            is_async = bool(m.group(1))
            name = m.group(2)
            signature = self._extract_signature_after(line, name)
            return {
                "line": lineno,
                "type": "async def" if is_async else "def",
                "name": name,
                "signature": signature,
            }
        # 常量 (仅 ALL_CAPS 顶层赋值)
        m = _PY_CONST_RE.match(line)
        if m:
            name = m.group(1)
            # 跳过太短的名称 (如单字母)
            if len(name) >= 2:
                return {
                    "line": lineno,
                    "type": "const",
                    "name": name,
                    "signature": "",
                }
        return None

    def _extract_js_symbol(
        self, line: str, lineno: int
    ) -> Optional[Dict[str, Any]]:
        """提取 JS/TS 符号 (function / class / 箭头函数)"""
        # function
        m = _JS_FUNCTION_RE.match(line)
        if m:
            name = m.group(4)
            signature = self._extract_signature_after(line, name)
            return {
                "line": lineno,
                "type": "function",
                "name": name,
                "signature": signature,
            }
        # class
        m = _JS_CLASS_RE.match(line)
        if m:
            name = m.group(4)
            signature = self._extract_signature_after(line, name)
            return {
                "line": lineno,
                "type": "class",
                "name": name,
                "signature": signature,
            }
        # 箭头函数: const myFunc = (args) => {
        m = _JS_ARROW_RE.match(line)
        if m:
            name = m.group(3)
            is_async = bool(m.group(4))
            signature = self._extract_signature_after(line, name)
            return {
                "line": lineno,
                "type": "async arrow" if is_async else "arrow",
                "name": name,
                "signature": signature,
            }
        return None

    def _extract_vue_symbol(
        self, line: str, lineno: int
    ) -> Optional[Dict[str, Any]]:
        """提取 Vue 符号 (复用 JS 提取 + defineComponent)"""
        # 先尝试 JS/TS 模式 (Vue SFC 的 <script> 部分用 JS 语法)
        sym = self._extract_js_symbol(line, lineno)
        if sym:
            return sym
        # defineComponent
        m = _VUE_COMPONENT_RE.match(line)
        if m:
            return {
                "line": lineno,
                "type": "component",
                "name": "defineComponent",
                "signature": "",
            }
        return None

    @staticmethod
    def _extract_signature_after(line: str, name: str) -> str:
        """提取定义行中 name 之后的部分作为签名。

        例如:
            "def my_func(arg1, arg2) -> str:" → "(arg1, arg2) -> str:"
            "class MyClass(BaseClass):" → "(BaseClass):"
            "function myFunc(a, b) {" → "(a, b) {"
        """
        idx = line.find(name)
        if idx == -1:
            return line.strip()
        rest = line[idx + len(name):].strip()
        # 移除尾部的冒号/大括号 (保留参数部分)
        # 对于 Python: "def func(a, b):" → "(a, b):" → "(a, b)"
        # 对于 JS: "function func(a, b) {" → "(a, b) {" → "(a, b)"
        rest = rest.rstrip(":")
        rest = rest.rstrip("{")
        return rest.strip()

    def _format_tree(self, tree: Dict[str, Any], indent: int = 0) -> str:
        """格式化目录树为文本 (缩进风格)。

        Args:
            tree: 目录树节点
            indent: 当前缩进级别

        Returns:
            格式化的目录树文本
        """
        lines: List[str] = []
        prefix = "  " * indent

        name = tree.get("name", "")
        node_type = tree.get("type", "dir")
        children = tree.get("children", [])

        if node_type == "dir":
            if indent == 0:
                # 根目录显示绝对路径的最后一段
                lines.append(f"{prefix}{name}/")
            else:
                lines.append(f"{prefix}{name}/")
        else:
            lines.append(f"{prefix}{name}")

        for child in children:
            lines.append(self._format_tree(child, indent + 1))

        return "\n".join(lines)

    def _relative_path(self, abs_path: str) -> str:
        """将绝对路径转为相对于 root_dir 的相对路径"""
        try:
            rel = os.path.relpath(abs_path, self.root_dir)
            return rel
        except ValueError:
            return abs_path


# ====== LangChain @tool 包装 ======


def _truncate_text(text: str, max_chars: int = 8000) -> str:
    """截断文本, 避免 token 膨胀"""
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated, total {len(text)} chars]"
    return text


async def get_repo_map_tool(root_dir: str = ".", max_files: int = 200):
    """创建一个 repo_map 工具供 Agent 使用。

    返回一个 LangChain @tool 装饰的异步函数, Agent 可通过 bind_tools() 绑定。
    LangChain 未安装时返回 None。

    Args:
        root_dir: 代码库根目录 (默认当前目录)
        max_files: 最大扫描文件数 (默认 200)

    Returns:
        LangChain BaseTool 实例, 或 None (LangChain 不可用时)
    """
    if not LANGCHAIN_TOOLS_AVAILABLE:
        logger.debug("langchain_core 未安装, get_repo_map_tool 返回 None")
        return None

    # 捕获工厂参数作为闭包默认值
    _default_root = root_dir
    _default_max_files = max_files

    @tool
    async def repo_map(
        root_dir: Optional[str] = None, max_files: Optional[int] = None
    ) -> str:
        """Get a structured overview of the codebase. Returns directory tree and key symbols.

        Use this tool to understand the project structure before making changes.
        Returns a text summary with the directory tree and key class/function definitions.

        Args:
            root_dir: Root directory to scan (optional, defaults to factory default)
            max_files: Maximum number of files to scan (optional, defaults to factory default)
        """
        try:
            import asyncio

            actual_root = root_dir if root_dir else _default_root
            actual_max = max_files if max_files else _default_max_files

            rm = RepoMap(actual_root)

            # 在线程中运行同步扫描, 避免阻塞事件循环
            result = await asyncio.to_thread(rm.scan, actual_max, 5)
            text = await asyncio.to_thread(rm.to_text, result)

            return _truncate_text(text, max_chars=8000)
        except Exception as e:
            logger.warning("repo_map 工具调用失败: %s", e)
            return f"Repo map failed: {e}"

    return repo_map
