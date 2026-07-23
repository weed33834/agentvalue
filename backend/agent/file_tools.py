# 参考自 opencode (MIT License) - https://github.com/sst/opencode
"""
增强版文件操作工具模块

参考来源 (opencode packages/opencode/src/tool/):
- read.ts:     文件读取 + 二进制检测 (扩展名黑名单 + 不可打印字符比例) + 行号格式
- write.ts:    文件写入 + 自动创建父目录 + 写入后 diff 反馈
- edit.ts:     SEARCH/REPLACE 编辑 + 多策略匹配 + 匹配失败时相似行建议
- glob.ts:     glob 文件模式匹配
- grep.ts:     内容搜索 (ripgrep 优先, Python re 降级)
- shell.ts:    命令执行 + 超时终止 + workdir 支持
- apply_patch.ts: opencode patch 格式 (*** Begin/End Patch, Add/Delete/Update File)

工具列表:
1. read_file       - 读取文件 (带行号, 二进制检测, 截断提示)
2. write_file      - 写入文件 (自动创建父目录)
3. edit_file       - 编辑文件 (SEARCH/REPLACE, 匹配失败返回相似行建议)
4. list_directory  - 列出目录 (带类型标记)
5. search_files    - 搜索文件内容 (ripgrep 优先, Python re 降级)
6. run_command     - 执行命令 (shlex.split + shell=False, 超时终止)
7. apply_patch     - 应用 patch 文本 (opencode patch 格式)

设计原则:
- LangChain 为可选依赖, 未安装时降级为纯 async 函数 + 手动 schema
- 所有文件操作限制在允许的工作目录内 (防止路径穿越)
- CodeEditor 从 agent.editor 导入, 不存在时使用内置 fallback 实现
- 每个工具返回 str, 便于 LLM 理解与 ToolMessage 传递
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import logging
import os
import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Tuple

# ===== LangChain 可选依赖 =====
# langchain>=0.3.0 在 requirements.txt 中声明, 但运行时可能未安装
try:
    from langchain_core.tools import tool as _lc_tool

    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

    def _lc_tool(*args: Any, **kwargs: Any) -> Any:
        """LangChain 不可用时的 fallback 装饰器。

        保持函数原样返回, 仅附加 _is_tool 标记。
        装饰后的对象可直接 await 调用, 也可通过 get_tool_schemas() 获取 schema。
        """
        if len(args) == 1 and callable(args[0]) and not kwargs:
            func = args[0]
            func._is_tool = True  # type: ignore[attr-defined]
            return func

        def decorator(func: Any) -> Any:
            func._is_tool = True  # type: ignore[attr-defined]
            return func

        return decorator


# ===== CodeEditor 导入 (可选) =====
# 从 agent.editor 导入 CodeEditor, 不存在时使用内置 fallback
# edit_file 使用 CodeEditor().apply_search_replace (SEARCH/REPLACE)
# apply_patch 使用 CodeEditor().apply_patch_text
try:
    from agent.editor import CodeEditor  # type: ignore[import-untyped]

    CODEEDITOR_AVAILABLE = True
except ImportError:
    CODEEDITOR_AVAILABLE = False

    class CodeEditor:  # type: ignore[no-redef]
        """CodeEditor fallback 实现 (当 agent.editor 不存在时使用)。

        提供与正式 CodeEditor (agent.editor.CodeEditor) 相同的接口:
        - apply_search_replace: SEARCH/REPLACE 编辑 (简化版, 仅精确匹配)
        - apply_patch_text: 应用 opencode patch 格式文本 (简化版)

        正式 CodeEditor 支持多策略模糊匹配 (精确→空白灵活→省略号→编辑距离),
        此 fallback 仅支持精确匹配 + 简单 patch 解析。
        """

        def __init__(self, fence: Tuple[str, str] = ("```", "```")):
            """初始化代码编辑器。

            Args:
                fence: 围栏符号对 (与正式 CodeEditor 保持接口一致)
            """
            self.fence = fence

        def apply_search_replace(
            self,
            file_path: str,
            content: Optional[str],
            search_text: str,
            replace_text: str,
        ) -> Optional[str]:
            """应用 SEARCH/REPLACE 编辑 (简化版, 仅精确匹配)。

            与正式 CodeEditor.apply_search_replace 接口完全一致。
            正式版本支持多策略模糊匹配, 此 fallback 仅支持精确匹配。

            Args:
                file_path: 文件路径 (仅用于错误信息, 不读取文件)
                content: 当前文件内容; None 表示文件不存在
                search_text: SEARCH 文本, 为空时表示追加/创建
                replace_text: REPLACE 文本

            Returns:
                替换后的完整文件内容; 匹配失败返回 None
            """
            # 文件不存在且 search_text 为空 -> 创建新文件
            if content is None and not search_text.strip():
                return replace_text

            if content is None:
                return None

            # search_text 为空 -> 追加到文件末尾
            if not search_text.strip():
                return content + replace_text

            # 精确匹配替换 (仅替换第一个匹配)
            if search_text in content:
                return content.replace(search_text, replace_text, 1)

            # 匹配失败
            return None

        def apply_patch_text(
            self,
            patch_text: str,
            root_dir: str,
        ) -> Dict[str, Optional[str]]:
            """应用 opencode patch 格式文本 (简化版)。

            与正式 CodeEditor.apply_patch_text 接口完全一致。
            解析 patch 文本, 读取 root_dir 下的相关文件, 返回每个文件的新内容。
            注意: 此方法不写入磁盘, 调用方负责将结果写入文件。

            Args:
                patch_text: opencode patch 格式的文本
                root_dir: 项目根目录, 用于读取文件

            Returns:
                {文件路径: 新内容}; ADD/UPDATE 返回内容字符串;
                DELETE 返回 None (表示文件应被删除);
                MOVE 时原路径为 None, 新路径为内容

            Raises:
                ValueError: patch 解析或应用失败
                FileNotFoundError: Update/Delete 操作引用了不存在的文件
            """
            operations = _parse_patch(patch_text)
            results: Dict[str, Optional[str]] = {}

            for op in operations:
                file_path = os.path.join(root_dir, op["path"])

                if op["type"] == "add":
                    content_lines: List[str] = op.get("contents", [])
                    content = "\n".join(content_lines)
                    if content and not content.endswith("\n"):
                        content += "\n"
                    results[op["path"]] = content

                elif op["type"] == "delete":
                    results[op["path"]] = None

                elif op["type"] == "update":
                    if not os.path.exists(file_path):
                        raise FileNotFoundError(
                            f"apply_patch 失败: 文件不存在: {op['path']}"
                        )
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()

                    for hunk in op.get("hunks", []):
                        content = _apply_update_hunk(content, hunk)

                    if op.get("move_to"):
                        results[op["path"]] = None  # 原路径标记删除
                        results[op["move_to"]] = content  # 新路径内容
                    else:
                        results[op["path"]] = content

            return results


logger = logging.getLogger(__name__)

# ===== 常量 (参考 opencode read.ts) =====

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_LINE_SUFFIX = f"... (行截断为 {MAX_LINE_LENGTH} 字符)"
MAX_READ_BYTES = 50 * 1024  # 50 KB
SAMPLE_BYTES = 4096

# 二进制文件扩展名黑名单 (参考 opencode read.ts isBinaryFile)
BINARY_EXTENSIONS = frozenset(
    {
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
        ".so",
        ".class",
        ".jar",
        ".war",
        ".7z",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".bin",
        ".dat",
        ".obj",
        ".o",
        ".a",
        ".lib",
        ".wasm",
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".tiff",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".pdf",
        ".eot",
        ".ttf",
        ".woff",
        ".woff2",
    }
)

SEARCH_MAX_RESULTS = 100
LIST_MAX_ENTRIES = 500
COMMAND_DEFAULT_TIMEOUT = 30

# ===== 允许的工作目录 (防止路径穿越) =====

_ALLOWED_ROOT: Optional[str] = None


def set_allowed_root(path: str) -> None:
    """设置允许的工作目录根路径。

    所有文件操作将被限制在此目录内。
    未调用此函数时, 默认使用 FILE_TOOLS_ROOT 环境变量或当前工作目录。

    Args:
        path: 允许的工作目录根路径
    """
    global _ALLOWED_ROOT
    _ALLOWED_ROOT = os.path.realpath(path)


def _get_allowed_root() -> str:
    """获取允许的工作目录根路径。"""
    global _ALLOWED_ROOT
    if _ALLOWED_ROOT is not None:
        return _ALLOWED_ROOT
    return os.path.realpath(os.environ.get("FILE_TOOLS_ROOT", os.getcwd()))


# ===== 路径验证 =====


def _validate_path(path: str) -> str:
    """验证路径在允许的工作目录内, 防止路径穿越。

    相对路径会基于允许的根目录解析。
    使用 realpath 解析符号链接, 确保路径真实位置在允许范围内。

    Args:
        path: 文件路径 (绝对或相对)

    Returns:
        验证后的绝对路径

    Raises:
        ValueError: 路径穿越检测失败
    """
    root = _get_allowed_root()

    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(root, path))

    # 检查路径在允许的根目录内
    # 使用 startswith(root + os.sep) 避免前缀匹配问题
    # (如 /foo/barbaz 不应被认为在 /foo/bar 内)
    if resolved == root or resolved.startswith(root + os.sep):
        return resolved

    raise ValueError(
        f"路径穿越检测: 路径 '{path}' 解析为 '{resolved}', "
        f"不在允许的工作目录 '{root}' 内"
    )


# ===== 辅助函数 =====


def _is_binary_file(file_path: str, sample: bytes) -> bool:
    """检测文件是否为二进制文件。

    双重检测策略 (参考 opencode read.ts isBinaryFile):
    1. 扩展名黑名单: 已知的二进制文件扩展名直接判定
    2. 不可打印字符比例: 采样字节中不可打印字符占比 > 30% 判定为二进制

    Args:
        file_path: 文件路径 (用于检查扩展名)
        sample: 文件内容的采样字节

    Returns:
        True 如果是二进制文件
    """
    # 策略1: 扩展名黑名单
    ext = os.path.splitext(file_path)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return True

    # 策略2: 不可打印字符比例
    if not sample:
        return False

    # 出现 null 字节直接判定为二进制
    non_printable = 0
    for byte in sample:
        if byte == 0:
            return True
        # 不可打印字符: 不在 \t \n \r 范围且 < 32
        if byte < 9 or (13 < byte < 32):
            non_printable += 1

    return (non_printable / len(sample)) > 0.3


def _read_sample(file_path: str, sample_size: int = SAMPLE_BYTES) -> bytes:
    """读取文件开头的采样字节, 用于二进制检测。"""
    try:
        with open(file_path, "rb") as f:
            return f.read(sample_size)
    except OSError:
        return b""


def _truncate_line(line: str) -> str:
    """截断过长的行 (参考 opencode read.ts MAX_LINE_LENGTH)。"""
    if len(line) > MAX_LINE_LENGTH:
        return line[:MAX_LINE_LENGTH] + MAX_LINE_SUFFIX
    return line


def _generate_diff(old_content: str, new_content: str, file_path: str) -> str:
    """生成 unified diff 预览。

    Args:
        old_content: 原始内容
        new_content: 新内容
        file_path: 文件路径 (用于 diff 头)

    Returns:
        unified diff 字符串
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{os.path.basename(file_path)}",
        tofile=f"b/{os.path.basename(file_path)}",
    )
    result = "".join(diff)
    return result if result else "(无差异)"


def _find_similar_lines(
    target: str, content: str, max_suggestions: int = 3
) -> List[str]:
    """在文件内容中查找与目标相似的行。

    使用 difflib.get_close_matches 进行模糊匹配。
    取 old_string 的第一个非空行作为搜索目标。

    Args:
        target: 要搜索的目标文本 (通常是 old_string)
        content: 文件内容
        max_suggestions: 最多返回的建议数

    Returns:
        相似行列表
    """
    content_lines = [line for line in content.split("\n") if line.strip()]

    # 取 target 的第一个非空行作为搜索目标
    target_line = next(
        (line for line in target.split("\n") if line.strip()),
        target.strip(),
    )
    if not target_line:
        return []

    matches = difflib.get_close_matches(
        target_line, content_lines, n=max_suggestions, cutoff=0.4
    )
    return matches


def _match_filename(filename: str, pattern: str) -> bool:
    """匹配文件名, 支持 brace expansion。

    支持标准 glob 模式 (*, ?, [...]) 以及 brace expansion ({ts,tsx})。
    fnmatch 不原生支持 brace expansion, 这里手动展开。

    Args:
        filename: 文件名
        pattern: glob 模式 (如 "*.py", "*.{ts,tsx}")

    Returns:
        True 如果匹配
    """
    if not pattern or pattern == "*":
        return True

    # 处理 brace expansion: *.{ts,tsx} -> ["*.ts", "*.tsx"]
    brace_match = re.match(r"^(.*)\{(.+)\}(.*)$", pattern)
    if brace_match:
        prefix, body, suffix = brace_match.groups()
        for part in body.split(","):
            if fnmatch.fnmatch(filename, prefix + part + suffix):
                return True
        return False

    return fnmatch.fnmatch(filename, pattern)


def _parse_patch(patch_text: str) -> List[Dict[str, Any]]:
    """解析 opencode patch 格式文本。

    格式参考 opencode apply_patch.txt:
        *** Begin Patch
        *** Add File: <path>
        +<line content>
        *** Update File: <path>
        *** Move to: <new_path>
        @@ <context>
        -<remove line>
        +<add line>
         <context line>
        *** Delete File: <path>
        *** End Patch

    Returns:
        操作列表, 每项为 dict:
        - {"type": "add", "path": "...", "contents": [...]}
        - {"type": "delete", "path": "..."}
        - {"type": "update", "path": "...", "move_to": "...", "hunks": [[...]]}
    """
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 查找 Begin/End 标记
    begin_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "*** Begin Patch":
            begin_idx = i
        elif stripped == "*** End Patch":
            end_idx = i
            break

    if begin_idx is None:
        raise ValueError("patch 解析失败: 缺少 *** Begin Patch 标记")

    end = end_idx if end_idx is not None else len(lines)
    patch_lines = lines[begin_idx + 1 : end]

    operations: List[Dict[str, Any]] = []
    current_op: Optional[Dict[str, Any]] = None
    current_hunk: Optional[List[str]] = None

    def _finalize_hunk() -> None:
        """将当前 hunk 添加到当前操作的 hunks 列表。"""
        nonlocal current_hunk
        if current_op is not None and current_hunk is not None:
            current_op.setdefault("hunks", []).append(current_hunk)
            current_hunk = None

    for line in patch_lines:
        if line.startswith("*** Add File: "):
            _finalize_hunk()
            if current_op is not None:
                operations.append(current_op)
            current_op = {
                "type": "add",
                "path": line[len("*** Add File: ") :].strip(),
                "contents": [],
            }
        elif line.startswith("*** Delete File: "):
            _finalize_hunk()
            if current_op is not None:
                operations.append(current_op)
            current_op = {
                "type": "delete",
                "path": line[len("*** Delete File: ") :].strip(),
            }
        elif line.startswith("*** Update File: "):
            _finalize_hunk()
            if current_op is not None:
                operations.append(current_op)
            current_op = {
                "type": "update",
                "path": line[len("*** Update File: ") :].strip(),
                "move_to": None,
                "hunks": [],
            }
        elif line.startswith("*** Move to: "):
            if current_op is not None:
                current_op["move_to"] = line[len("*** Move to: ") :].strip()
        elif line.startswith("@@"):
            _finalize_hunk()
            current_hunk = [line]
        elif current_op is None:
            continue
        elif current_op["type"] == "add":
            if line.startswith("+"):
                current_op["contents"].append(line[1:])
        elif current_op["type"] == "update" and current_hunk is not None:
            current_hunk.append(line)

    _finalize_hunk()
    if current_op is not None:
        operations.append(current_op)

    if not operations:
        raise ValueError("patch 解析失败: 未找到任何文件操作")

    return operations


def _apply_update_hunk(content: str, hunk_lines: List[str]) -> str:
    """应用单个 update hunk 到文件内容。

    hunk 格式:
        @@ <context_marker>
         <context line>  (空格前缀, 保留)
        -<remove line>   (减号前缀, 删除)
        +<add line>      (加号前缀, 新增)

    策略: 提取 old_block (context + removals) 和 new_block (context + additions),
    在文件内容中找到 old_block 并替换为 new_block。

    Args:
        content: 文件原始内容
        hunk_lines: hunk 的行列表

    Returns:
        修改后的内容

    Raises:
        ValueError: 无法在文件中找到要替换的代码块
    """
    context_marker: Optional[str] = None
    old_lines: List[str] = []
    new_lines: List[str] = []

    for line in hunk_lines:
        if line.startswith("@@"):
            context_marker = line[2:].strip() if len(line) > 2 else ""
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])

    old_block = "\n".join(old_lines)
    new_block = "\n".join(new_lines)

    # 纯插入 (无删除, 无上下文): 在 context_marker 后插入
    if not old_lines:
        if context_marker:
            content_lines = content.split("\n")
            for i, line in enumerate(content_lines):
                if context_marker in line:
                    content_lines.insert(i + 1, new_block)
                    return "\n".join(content_lines)
        # 无 context_marker, 末尾追加
        return content + "\n" + new_block if content else new_block

    # 查找并替换
    if old_block not in content:
        raise ValueError(
            f"无法在文件中找到要替换的代码块:\n{old_block[:200]}..."
            if len(old_block) > 200
            else f"无法在文件中找到要替换的代码块:\n{old_block}"
        )

    return content.replace(old_block, new_block, 1)


# ===== 工具定义 =====


@_lc_tool
async def read_file(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """读取文件内容, 带行号返回。

    从本地文件系统读取文件。如果路径不存在则返回错误。
    二进制文件 (通过扩展名黑名单和不可打印字符比例检测) 会被拒绝读取。

    用法:
    - file_path 应为绝对路径或相对于工作目录的相对路径
    - 默认返回文件开头最多 2000 行
    - offset 是开始读取的行号 (1-indexed), 用于读取后续部分
    - 如需读取大文件的后续部分, 使用更大的 offset 再次调用
    - 超过 2000 字符的行会被截断
    - 可以在一条消息中并行调用多个 read_file 来读取多个文件

    Args:
        file_path: 文件的绝对路径或相对路径
        offset: 开始读取的行号 (1-indexed, 默认 1)
        limit: 最多读取的行数 (默认 2000)
    """
    try:
        resolved = _validate_path(file_path)
    except ValueError as e:
        return f"错误: {e}"

    # 路径不存在
    if not os.path.exists(resolved):
        # 尝试提供 "Did you mean?" 建议
        parent = os.path.dirname(resolved)
        base = os.path.basename(resolved)
        suggestions: List[str] = []
        if os.path.isdir(parent):
            try:
                entries = os.listdir(parent)
            except OSError:
                entries = []
            for entry in entries:
                if base.lower() in entry.lower() or entry.lower() in base.lower():
                    suggestions.append(os.path.join(parent, entry))
        if suggestions:
            hint = "\n\n你是否想读取以下文件?\n" + "\n".join(suggestions[:3])
            return f"文件不存在: {file_path}{hint}"
        return f"文件不存在: {file_path}"

    # 是目录而非文件
    if os.path.isdir(resolved):
        return (
            f"路径是目录而非文件: {file_path}\n"
            f"请使用 list_directory 工具列出目录内容"
        )

    # 二进制检测
    sample = _read_sample(resolved)
    if _is_binary_file(resolved, sample):
        return f"无法读取二进制文件: {file_path}"

    # 读取文件内容
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return f"读取文件失败: {e}"

    total_lines = len(all_lines)

    # 计算 offset 和 limit
    offset = max(1, offset)
    start = offset - 1  # 转为 0-indexed
    end = start + limit

    if start >= total_lines and total_lines > 0:
        return f"offset {offset} 超出文件范围 (文件共 {total_lines} 行)"

    selected = all_lines[start:end]
    truncated = end < total_lines

    # 格式化输出: "行号: 内容"
    output_lines: List[str] = []
    total_bytes = 0
    for i, line in enumerate(selected):
        line_num = start + i + 1  # 1-indexed
        # 去掉行尾换行符, 统一处理
        stripped = line.rstrip("\n").rstrip("\r")
        truncated_line = _truncate_line(stripped)
        formatted = f"{line_num}: {truncated_line}"
        total_bytes += len(formatted.encode("utf-8")) + 1
        output_lines.append(formatted)

        # 字节上限检查 (参考 opencode MAX_BYTES)
        if total_bytes >= MAX_READ_BYTES:
            truncated = True
            break

    output = "\n".join(output_lines)

    # 添加截断/结束提示
    last_line = start + len(output_lines)
    next_offset = last_line + 1
    if truncated:
        output += (
            f"\n\n(显示第 {offset}-{last_line} 行, 共 {total_lines} 行. "
            f"使用 offset={next_offset} 继续读取)"
        )
    else:
        output += f"\n\n(文件结束 - 共 {total_lines} 行)"

    return output


@_lc_tool
async def write_file(file_path: str, content: str) -> str:
    """写入文件内容, 自动创建父目录。

    如果文件已存在则覆盖, 不存在则创建。
    父目录不存在时会自动递归创建。

    用法:
    - file_path 应为绝对路径或相对于工作目录的相对路径
    - content 是要写入的完整文件内容
    - 此工具会覆盖整个文件, 如需部分修改请使用 edit_file

    Args:
        file_path: 文件的绝对路径或相对路径
        content: 要写入的文件内容
    """
    try:
        resolved = _validate_path(file_path)
    except ValueError as e:
        return f"错误: {e}"

    # 自动创建父目录
    parent = os.path.dirname(resolved)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"创建父目录失败: {e}"

    # 写入文件
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"写入文件失败: {e}"

    file_size = len(content.encode("utf-8"))
    return f"文件写入成功: {file_path} ({file_size} 字节, {len(content)} 字符)"


@_lc_tool
async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """使用 SEARCH/REPLACE 方式编辑文件。

    在文件中查找 old_string 并替换为 new_string。
    匹配失败时返回错误信息和相似行建议。

    用法:
    - 编辑前应先用 read_file 读取文件内容
    - old_string 必须与文件内容完全匹配 (包括空白和缩进)
    - 从 read_file 输出中复制内容时, 行号前缀后的空格之后才是实际内容
    - old_string 和 new_string 不能相同
    - 如果 old_string 在文件中出现多次, 需提供更多上下文使其唯一, 或设置 replace_all=True
    - old_string 为空且文件不存在时, 会创建新文件 (类似 write_file)
    - 优先编辑现有文件, 不要轻易创建新文件

    Args:
        file_path: 文件的绝对路径或相对路径
        old_string: 要替换的文本 (必须与文件内容完全匹配)
        new_string: 替换后的文本 (必须与 old_string 不同)
        replace_all: 是否替换所有匹配项 (默认 False, 仅替换第一个唯一匹配)
    """
    # 参数校验
    if old_string == new_string:
        return "错误: old_string 和 new_string 相同, 无需编辑"

    try:
        resolved = _validate_path(file_path)
    except ValueError as e:
        return f"错误: {e}"

    # 读取当前文件内容 (None 表示文件不存在)
    if os.path.exists(resolved):
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content: Optional[str] = f.read()
        except OSError as e:
            return f"读取文件失败: {e}"
    else:
        content = None

    # 使用 CodeEditor 执行 SEARCH/REPLACE
    # CodeEditor.apply_search_replace 返回新内容或 None (匹配失败)
    editor = CodeEditor()

    if replace_all and content is not None and old_string in content:
        # replace_all=True 且精确匹配存在: 直接替换所有
        new_content = content.replace(old_string, new_string)
    elif content is not None and old_string in content and not replace_all:
        # 精确匹配存在, 检查是否有多处匹配
        match_count = content.count(old_string)
        if match_count > 1:
            return (
                f"编辑失败: old_string 在文件中找到 {match_count} 处匹配。"
                f"请提供更多上下文以唯一定位, 或设置 replace_all=True。"
            )
        # 使用 CodeEditor 进行替换 (支持多策略匹配)
        new_content = editor.apply_search_replace(
            resolved, content, old_string, new_string
        )
    else:
        # 精确匹配不存在或文件不存在: 尝试 CodeEditor 多策略匹配
        new_content = editor.apply_search_replace(
            resolved, content, old_string, new_string
        )

    # 匹配失败
    if new_content is None:
        if content is not None:
            suggestions = _find_similar_lines(old_string, content)
            hint = ""
            if suggestions:
                hint = "\n\n相似行建议 (可能是你想要匹配的内容):\n"
                hint += "\n".join(f"  > {s}" for s in suggestions)
            return (
                f"编辑失败: 未在文件中找到 old_string。"
                f"请确保完全匹配 (包括空白和缩进)。\n"
                f"文件路径: {file_path}{hint}"
            )
        return f"编辑失败: 文件不存在且 old_string 非空, 无法创建文件: {file_path}"

    # 写入文件
    try:
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return f"写入文件失败: {e}"

    # 生成 diff 预览
    old_for_diff = content or ""
    diff = _generate_diff(old_for_diff, new_content, resolved)

    result = "编辑成功。"
    if diff and diff != "(无差异)":
        result += f"\n\nDiff 预览:\n{diff}"
    return result


@_lc_tool
async def list_directory(path: str = ".", pattern: str = "*") -> str:
    """列出目录内容, 带类型标记。

    返回目录中的文件和子目录列表, 每项带有类型标记:
    - [DIR]  表示目录 (带 / 后缀)
    - [FILE] 表示文件 (带大小信息)

    用法:
    - path 默认为当前工作目录
    - pattern 是可选的 glob 过滤模式 (如 "*.py", "*.{ts,tsx}")
    - 结果按字母排序, 最多返回 500 条

    Args:
        path: 目录路径 (默认当前目录)
        pattern: glob 过滤模式 (默认 "*" 表示全部)
    """
    try:
        resolved = _validate_path(path)
    except ValueError as e:
        return f"错误: {e}"

    if not os.path.exists(resolved):
        return f"目录不存在: {path}"

    if not os.path.isdir(resolved):
        return f"路径不是目录: {path}"

    try:
        entries = sorted(os.listdir(resolved))
    except OSError as e:
        return f"列出目录失败: {e}"

    # pattern 过滤
    if pattern and pattern != "*":
        entries = [e for e in entries if _match_filename(e, pattern)]

    if not entries:
        return f"目录 '{path}' 中没有匹配 '{pattern}' 的条目"

    # 格式化输出
    result_lines: List[str] = []
    for entry in entries[:LIST_MAX_ENTRIES]:
        full_path = os.path.join(resolved, entry)
        try:
            if os.path.isdir(full_path):
                result_lines.append(f"[DIR]  {entry}/")
            else:
                size = os.path.getsize(full_path)
                result_lines.append(f"[FILE] {entry} ({size} 字节)")
        except OSError:
            result_lines.append(f"[???] {entry}")

    output = "\n".join(result_lines)

    if len(entries) > LIST_MAX_ENTRIES:
        output += (
            f"\n\n(显示前 {LIST_MAX_ENTRIES} 条, 共 {len(entries)} 条. "
            f"请使用更具体的 pattern 缩小范围)"
        )
    else:
        output += f"\n\n(共 {len(entries)} 条)"

    return output


@_lc_tool
async def search_files(pattern: str, path: str = ".", include: str = "*") -> str:
    """搜索文件内容 (grep), 使用正则表达式。

    优先使用 ripgrep (rg) 进行搜索 (如果系统可用), 否则降级到 Python re。
    返回匹配的行, 带文件名和行号。

    用法:
    - pattern 是正则表达式 (如 "log.*Error", "function\\s+\\w+")
    - path 是搜索目录 (默认当前目录)
    - include 是文件名 glob 过滤 (如 "*.py", "*.{ts,tsx}")
    - 最多返回 100 条匹配结果
    - 如需查找文件名而非内容, 请使用 list_directory

    Args:
        pattern: 正则表达式模式
        path: 搜索目录 (默认当前目录)
        include: 文件名 glob 过滤 (默认 "*" 表示全部文件)
    """
    if not pattern:
        return "错误: pattern 不能为空"

    try:
        resolved = _validate_path(path)
    except ValueError as e:
        return f"错误: {e}"

    if not os.path.exists(resolved):
        return f"路径不存在: {path}"

    # 确定搜索目录
    if os.path.isdir(resolved):
        search_dir = resolved
    else:
        # 如果是文件, 在其所在目录搜索
        search_dir = os.path.dirname(resolved)

    # 验证正则有效性
    try:
        re.compile(pattern)
    except re.error as e:
        return f"正则表达式无效: {e}"

    matches: List[str] = []

    # 优先使用 ripgrep
    rg_path = shutil.which("rg")
    if rg_path:
        try:
            cmd = [
                rg_path,
                "--no-heading",
                "-n",
                "--color",
                "never",
                "-g",
                include,
                pattern,
                search_dir,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if stdout:
                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    if len(matches) >= SEARCH_MAX_RESULTS:
                        break
                    matches.append(line)
        except asyncio.TimeoutError:
            return "搜索超时 (30秒), 请缩小搜索范围或使用更具体的 pattern"
        except Exception as e:
            logger.warning("ripgrep 搜索失败, 降级到 Python re: %s", e)
            matches = []
    else:
        # 降级: Python re + os.walk
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"正则表达式无效: {e}"

        for root, _dirs, files in os.walk(search_dir):
            for fname in files:
                if not _match_filename(fname, include):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append(f"{fpath}:{lineno}:{line.rstrip()}")
                                if len(matches) >= SEARCH_MAX_RESULTS:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
                if len(matches) >= SEARCH_MAX_RESULTS:
                    break
            if len(matches) >= SEARCH_MAX_RESULTS:
                break

    if not matches:
        return f"未找到匹配 '{pattern}' 的内容 (路径: {path}, 文件过滤: {include})"

    # 格式化输出 (参考 opencode grep.ts)
    output_lines: List[str] = []
    truncated = len(matches) >= SEARCH_MAX_RESULTS
    total = len(matches)

    output_lines.append(
        f"找到 {total} 处匹配{' (更多匹配未显示)' if truncated else ''}"
    )

    current_file = ""
    for match in matches:
        # ripgrep 格式: file:line:content
        # Python 格式: file:line:content (已格式化)
        parts = match.split(":", 2)
        if len(parts) >= 3:
            fpath, lineno, text = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            fpath, lineno = parts[0], parts[1]
            text = ""
        else:
            fpath = match
            lineno = "?"
            text = ""

        if current_file != fpath:
            if current_file != "":
                output_lines.append("")
            current_file = fpath
            output_lines.append(f"{fpath}:")
        output_lines.append(f"  第 {lineno} 行: {text}")

    if truncated:
        output_lines.append("")
        output_lines.append("(结果已截断, 请使用更具体的路径或 pattern)")

    return "\n".join(output_lines)


@_lc_tool
async def run_command(command: str, workdir: str = "", timeout: int = 30) -> str:
    """执行命令 (安全模式, 不使用 shell)。

    使用 shlex.split + shell=False 执行命令, 避免 shell 注入风险。
    超时后自动终止进程。

    安全说明:
    - 不使用 shell=True, 因此不支持 shell 特性 (管道 |, 重定向 >, && 等)
    - 命令通过 shlex.split 解析为参数列表后直接执行
    - 如需运行复杂 shell 命令, 请将命令写入脚本文件后执行

    用法:
    - command 是要执行的命令 (如 "git status", "ls -la")
    - workdir 是工作目录 (默认为允许的工作目录根)
    - timeout 是超时秒数 (默认 30), 超时后进程会被终止

    Args:
        command: 要执行的命令
        workdir: 工作目录 (默认为允许的工作目录根)
        timeout: 超时秒数 (默认 30)
    """
    if not command or not command.strip():
        return "错误: command 不能为空"

    # 解析工作目录
    if workdir:
        try:
            cwd = _validate_path(workdir)
        except ValueError as e:
            return f"错误: {e}"
        if not os.path.isdir(cwd):
            return f"工作目录不存在: {workdir}"
    else:
        cwd = _get_allowed_root()

    # 使用 shlex.split 解析命令 (安全: 不使用 shell)
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"命令解析失败 (shlex): {e}"

    if not args:
        return "错误: 解析后命令为空"

    # 执行命令
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError:
        return f"命令不存在: {args[0]}"
    except PermissionError:
        return f"没有执行权限: {args[0]}"
    except Exception as e:
        return f"启动命令失败: {e}"

    # 等待完成 (带超时)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时: 终止进程
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass  # 进程已退出
        return f"命令超时 ({timeout}秒), 已终止进程。\n" f"命令: {command}"

    # 构建输出
    result_parts: List[str] = []

    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if stdout_text:
        result_parts.append(stdout_text.rstrip())
    if stderr_text:
        result_parts.append(f"[STDERR]\n{stderr_text.rstrip()}")

    result_parts.append(f"[退出码: {proc.returncode}]")

    output = "\n\n".join(result_parts)
    if not stdout_text and not stderr_text:
        output = f"(无输出)\n\n[退出码: {proc.returncode}]"

    return output


@_lc_tool
async def apply_patch(patch_text: str, root_dir: str = ".") -> str:
    """应用 patch 文本 (opencode patch 格式)。

    使用 opencode 的 patch 格式批量修改文件。支持新增、修改、删除文件。

    patch 格式:
        *** Begin Patch
        *** Add File: <path>
        +<文件内容行>
        *** Update File: <path>
        *** Move to: <new_path>     (可选, 移动文件)
        @@ <上下文标记>
         <上下文行>                  (空格前缀, 保留)
        -<删除行>                    (减号前缀, 删除)
        +<新增行>                    (加号前缀, 新增)
        *** Delete File: <path>
        *** End Patch

    用法:
    - patch_text 是完整的 patch 文本
    - root_dir 是 patch 应用的根目录 (默认当前目录)
    - 所有文件路径相对于 root_dir 解析
    - 返回修改的文件列表 (A=新增, M=修改, D=删除)

    Args:
        patch_text: 完整的 patch 文本
        root_dir: patch 应用的根目录 (默认当前目录)
    """
    if not patch_text or not patch_text.strip():
        return "错误: patch_text 不能为空"

    try:
        resolved_root = _validate_path(root_dir)
    except ValueError as e:
        return f"错误: {e}"

    # 使用 CodeEditor.apply_patch_text
    # CodeEditor.apply_patch_text 返回 {文件路径: 新内容或None}
    # None 表示删除文件, str 表示写入新内容
    # 注意: CodeEditor.apply_patch_text 不写入磁盘, 需要调用方写入
    editor = CodeEditor()
    try:
        results: Dict[str, Optional[str]] = editor.apply_patch_text(
            patch_text, resolved_root
        )
    except FileNotFoundError as e:
        return f"patch 应用失败: {e}"
    except ValueError as e:
        return f"patch 应用失败: {e}"
    except Exception as e:
        logger.warning("CodeEditor.apply_patch_text 异常: %s", e)
        return f"patch 应用失败: {e}"

    if not results:
        return "patch 应用成功, 但未修改任何文件"

    # 将结果写入磁盘
    summary_lines: List[str] = []
    for file_rel_path, new_content in results.items():
        abs_path = os.path.join(resolved_root, file_rel_path)

        # 路径安全验证
        try:
            _validate_path(abs_path)
        except ValueError as e:
            return f"路径验证失败: {e}"

        existed = os.path.exists(abs_path)

        if new_content is None:
            # 删除文件
            if existed:
                try:
                    os.remove(abs_path)
                except OSError as e:
                    return f"删除文件失败 {file_rel_path}: {e}"
                summary_lines.append(f"D {file_rel_path}")
            else:
                summary_lines.append(f"D {file_rel_path} (已不存在)")
        else:
            # 写入文件内容
            parent = os.path.dirname(abs_path)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as e:
                    return f"创建目录失败 {file_rel_path}: {e}"
            try:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
            except OSError as e:
                return f"写入文件失败 {file_rel_path}: {e}"
            if existed:
                summary_lines.append(f"M {file_rel_path}")
            else:
                summary_lines.append(f"A {file_rel_path}")

    output = "成功更新以下文件:\n" + "\n".join(summary_lines)
    return output


# ===== 公共 API =====


def get_file_tools() -> List[Any]:
    """返回所有文件操作工具的列表。

    当 LangChain 可用时, 返回 StructuredTool 对象列表,
    可直接传给 bind_tools() 或 ToolNode。
    当 LangChain 不可用时, 返回 async 函数列表, 可直接 await 调用。

    Returns:
        工具列表 (read_file, write_file, edit_file, list_directory,
                  search_files, run_command, apply_patch)
    """
    return [
        read_file,
        write_file,
        edit_file,
        list_directory,
        search_files,
        run_command,
        apply_patch,
    ]


# 手动 schema 定义 (用于无 LangChain 时的工具元数据展示)
_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "读取文件内容, 带行号返回。支持二进制检测和截断。",
        "args_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件的绝对路径或相对路径",
                },
                "offset": {
                    "type": "integer",
                    "default": 1,
                    "description": "开始读取的行号 (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "default": 2000,
                    "description": "最多读取的行数",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "写入文件内容, 自动创建父目录。",
        "args_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件的绝对路径或相对路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "使用 SEARCH/REPLACE 方式编辑文件。匹配失败时返回相似行建议。",
        "args_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件的绝对路径或相对路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的文本 (必须与文件内容完全匹配)",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否替换所有匹配项 (默认 False)",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_directory",
        "description": "列出目录内容, 带类型标记 ([DIR]/[FILE])。",
        "args_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "目录路径 (默认当前目录)",
                },
                "pattern": {
                    "type": "string",
                    "default": "*",
                    "description": "glob 过滤模式",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_files",
        "description": "搜索文件内容 (grep), 使用正则表达式。ripgrep 优先, Python re 降级。",
        "args_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式模式",
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "搜索目录",
                },
                "include": {
                    "type": "string",
                    "default": "*",
                    "description": "文件名 glob 过滤",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "执行命令 (安全模式, shlex.split + shell=False, 超时终止)。",
        "args_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令",
                },
                "workdir": {
                    "type": "string",
                    "default": "",
                    "description": "工作目录",
                },
                "timeout": {
                    "type": "integer",
                    "default": 30,
                    "description": "超时秒数",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "apply_patch",
        "description": "应用 patch 文本 (opencode patch 格式), 批量修改文件。",
        "args_schema": {
            "type": "object",
            "properties": {
                "patch_text": {
                    "type": "string",
                    "description": "完整的 patch 文本",
                },
                "root_dir": {
                    "type": "string",
                    "default": ".",
                    "description": "patch 应用的根目录",
                },
            },
            "required": ["patch_text"],
        },
    },
]


def get_tool_schemas() -> List[Dict[str, Any]]:
    """返回所有工具的 schema 信息 (用于 Admin API 展示)。

    当 LangChain 可用时, 优先从 StructuredTool 提取 schema。
    当 LangChain 不可用时, 返回手动定义的 schema。

    Returns:
        工具 schema 列表, 每项包含 name, description, args_schema
    """
    if LANGCHAIN_AVAILABLE:
        result: List[Dict[str, Any]] = []
        tools = get_file_tools()
        for t in tools:
            schema = getattr(t, "args_schema", None)
            schema_dict: Dict[str, Any] = {}
            if schema is not None:
                try:
                    if hasattr(schema, "model_json_schema"):
                        schema_dict = schema.model_json_schema()
                    elif hasattr(schema, "schema"):
                        schema_dict = schema.schema()
                except Exception:
                    schema_dict = {}
            result.append(
                {
                    "name": getattr(t, "name", getattr(t, "__name__", str(t))),
                    "description": getattr(t, "description", getattr(t, "__doc__", "")),
                    "args_schema": schema_dict,
                }
            )
        return result

    # LangChain 不可用时返回手动 schema
    return list(_TOOL_SCHEMAS)


__all__ = [
    # 工具函数
    "read_file",
    "write_file",
    "edit_file",
    "list_directory",
    "search_files",
    "run_command",
    "apply_patch",
    # 公共 API
    "get_file_tools",
    "get_tool_schemas",
    "set_allowed_root",
    # 状态标记
    "LANGCHAIN_AVAILABLE",
    "CODEEDITOR_AVAILABLE",
]
