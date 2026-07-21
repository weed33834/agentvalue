# 移植自 Aider (Apache 2.0 License)
# 源文件: aider/linter.py
# https://github.com/Aider-AI/aider/blob/main/aider/linter.py
#
# 改造说明:
# - 去掉 self.io 依赖，改为 logging.getLogger
# - 去掉 aider.run_cmd 依赖，flake8 直接用 subprocess.run([sys.executable, "-m", "flake8", ...])
# - 去掉 oslex 依赖，改用标准库 shlex
# - tree-sitter 优先使用 grep_ast (aider 原始依赖)，其次 tree_sitter_languages
# - 如果 tree-sitter 不可用，降级到只用 compile() + flake8
"""Aider Linter - 代码静态检查 (移植自 Aider)

提供基于 tree-sitter 语法解析、Python compile() 以及 flake8 的三重检查能力，
用于在 Agent 编辑代码后快速发现语法/引用错误并给出带上下文标记的错误报告。

设计要点:
1. ``Linter.lint(fname)`` 是主入口，根据文件扩展名自动选择 linter (默认仅注册 python)。
2. Python 文件做三重检查: tree-sitter 语法 -> compile() -> flake8 (致命规则集)。
3. tree-sitter 为可选依赖:
   - 优先使用 ``grep_ast`` (同时提供 TreeContext 漂亮的代码上下文标注)
   - 其次使用 ``tree_sitter_languages`` (可解析，但 TreeContext 降级为简易行号标注)
   - 两者都不可用时，仅保留 compile() + flake8
4. flake8 同样为可选依赖，不可用时跳过该步骤。
5. 所有错误最终拼接为一段带 ``█`` 标记的代码上下文文本，便于 LLM 定位修复。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shlex
import subprocess
import sys
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, Union

logger = logging.getLogger(__name__)

# tree-sitter 抛出 FutureWarning，忽略它以保持日志干净
warnings.simplefilter("ignore", category=FutureWarning)

# ====== flake8 可用性预检查 ======
# 避免在 flake8 未安装时，`python -m flake8` 输出的 "No module named flake8"
# 被误判为 lint 错误。
_FLAKE8_AVAILABLE: bool = importlib.util.find_spec("flake8") is not None

# ====== tree-sitter 可选依赖加载 ======
# 优先 grep_ast (提供 TreeContext + filename_to_lang + get_parser)
# 其次 tree_sitter_language_pack / tree_sitter_languages (仅 get_parser，需自行实现 filename_to_lang)
# 都不可用时降级为 compile() + flake8
_TREE_SITTER_AVAILABLE: bool = False
_TREE_SITTER_BACKEND: Optional[str] = None  # "grep_ast" | "tree_sitter_language_pack" | "tree_sitter_languages" | None
_ts_get_parser: Optional[Callable[[str], Any]] = None
_TreeContext: Optional[type] = None
_filename_to_lang: Optional[Callable[[str], Optional[str]]] = None

try:
    from grep_ast import TreeContext as _GrepTreeContext
    from grep_ast import filename_to_lang as _grep_filename_to_lang
    from grep_ast.tsl import get_parser as _grep_get_parser

    _TREE_SITTER_AVAILABLE = True
    _TREE_SITTER_BACKEND = "grep_ast"
    _ts_get_parser = _grep_get_parser
    _TreeContext = _GrepTreeContext
    _filename_to_lang = _grep_filename_to_lang
except ImportError:
    # 优先 tree_sitter_language_pack (与新版 tree-sitter 0.25+ 兼容，与 grep_ast.tsl 一致)
    _tsl_loaded = False
    for _tsl_mod, _tsl_backend in (
        ("tree_sitter_language_pack", "tree_sitter_language_pack"),
        ("tree_sitter_languages", "tree_sitter_languages"),
    ):
        try:
            _tsl_mod_obj = __import__(_tsl_mod, fromlist=["get_parser"])
            _ts_get_parser = _tsl_mod_obj.get_parser
            _TREE_SITTER_AVAILABLE = True
            _TREE_SITTER_BACKEND = _tsl_backend
            _TreeContext = None  # 无 TreeContext，降级处理
            _filename_to_lang = None  # 用内置映射
            _tsl_loaded = True
            break
        except ImportError:
            continue
    if not _tsl_loaded:
        _TREE_SITTER_AVAILABLE = False
        _TREE_SITTER_BACKEND = None
        _ts_get_parser = None
        _TreeContext = None
        _filename_to_lang = None

# tree-sitter 不支持 typescript 的语法检查 (aider #1132)
_TS_UNSUPPORTED_LANGS: Set[str] = {"typescript"}

# 内置扩展名 -> tree-sitter 语言映射 (grep_ast 不可用时使用)
_EXT_TO_LANG: dict = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".lua": "lua",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}


def _builtin_filename_to_lang(fname: str) -> Optional[str]:
    """grep_ast 不可用时的简易扩展名 -> 语言映射。"""
    return _EXT_TO_LANG.get(Path(fname).suffix.lower())


def _resolve_lang(fname: str) -> Optional[str]:
    """统一的文件名 -> 语言解析 (优先 grep_ast，其次内置映射)。"""
    if _filename_to_lang is not None:
        try:
            return _filename_to_lang(fname)
        except Exception:
            pass
    return _builtin_filename_to_lang(fname)


@dataclass
class LintResult:
    """单次 lint 的结果。

    Attributes:
        text: 错误描述文本 (可能为空，仅靠 lines 定位)。
        lines: 出错的行号列表 (0-based)。
    """

    text: str
    lines: list = field(default_factory=list)


# ====== tree-sitter 遍历与上下文 ======


def traverse_tree(node: Any) -> List[int]:
    """递归遍历 tree-sitter 节点，收集所有 ERROR / missing 节点的起始行号 (0-based)。"""
    errors: List[int] = []
    if node.type == "ERROR" or node.is_missing:
        errors.append(node.start_point[0])

    for child in node.children:
        errors += traverse_tree(child)

    return errors


def tree_context(fname: str, code: str, line_nums) -> str:
    """生成带 ``█`` 标记的代码上下文，便于 LLM 定位出错行。

    优先使用 grep_ast 的 TreeContext (带作用域折叠)，不可用时降级为简易行号标注。
    """
    line_nums = list(line_nums)
    if not line_nums:
        return ""

    if _TreeContext is not None:
        try:
            context = _TreeContext(
                fname,
                code,
                color=False,
                line_number=True,
                child_context=False,
                last_line=False,
                margin=0,
                mark_lois=True,
                loi_pad=3,
                show_top_of_file_parent_scope=False,
            )
            context.add_lines_of_interest(set(line_nums))
            context.add_context()
            s = "s" if len(line_nums) > 1 else ""
            output = f"## See relevant line{s} below marked with █.\n\n"
            output += fname + ":\n"
            output += context.format()
            return output
        except Exception as err:
            logger.debug("TreeContext 渲染失败，降级为简易标注: %s", err)

    # 降级: 简易行号标注
    return _simple_tree_context(fname, code, line_nums)


def _simple_tree_context(fname: str, code: str, line_nums) -> str:
    """无 grep_ast 时的简易上下文: 展示出错行附近 +/- 2 行，出错行用 ``█`` 标记。"""
    lines = code.splitlines()
    nums = sorted({int(n) for n in line_nums if 0 <= int(n) < len(lines)})
    if not nums:
        return ""

    s = "s" if len(nums) > 1 else ""
    output = f"## See relevant line{s} below marked with █.\n\n"
    output += fname + ":\n"

    shown: Set[int] = set()
    for ln in nums:
        for ctx in range(max(0, ln - 2), min(len(lines), ln + 3)):
            if ctx in shown:
                continue
            shown.add(ctx)
            marker = "█" if ctx == ln else " "
            output += f"{ctx + 1:6d} {marker} {lines[ctx]}\n"
    return output


def find_filenames_and_linenums(text: str, fnames) -> dict:
    """在 text 中搜索所有 ``<filename>:<行号>`` 模式，返回 {filename: set(行号)}。

    其中 filename 必须出现在 fnames 列表中。用于从 flake8 等工具的输出中提取位置。
    """
    fnames = list(fnames)
    if not fnames:
        return {}
    pattern = re.compile(
        r"(\b(?:" + "|".join(re.escape(f) for f in fnames) + r"):\d+\b)"
    )
    matches = pattern.findall(text)
    result: dict = {}
    for match in matches:
        fname, linenum = match.rsplit(":", 1)
        result.setdefault(fname, set()).add(int(linenum))
    return result


# ====== 模块级 lint 函数 ======


def basic_lint(fname: str, code: str) -> Optional[LintResult]:
    """使用 tree-sitter 查找语法错误 (ERROR 节点)。

    tree-sitter 不可用或不支持该语言时返回 None。
    """
    if not _TREE_SITTER_AVAILABLE:
        return None

    lang = _resolve_lang(fname)
    if not lang:
        return None
    if lang in _TS_UNSUPPORTED_LANGS:
        return None

    try:
        assert _ts_get_parser is not None  # noqa: S101
        parser = _ts_get_parser(lang)
    except Exception as err:
        logger.warning("无法加载 tree-sitter 解析器 (%s): %s", lang, err)
        return None

    try:
        tree = parser.parse(bytes(code, "utf-8"))
    except Exception as err:
        logger.warning("tree-sitter 解析失败 (%s): %s", fname, err)
        return None

    try:
        errors = traverse_tree(tree.root_node)
    except RecursionError:
        logger.warning("tree-sitter 遍历递归过深，跳过 %s", fname)
        return None

    if not errors:
        return None

    return LintResult(text="", lines=errors)


def lint_python_compile(fname: str, code: str) -> Optional[LintResult]:
    """使用 Python 内置 ``compile()`` 检查语法错误。"""
    try:
        compile(code, fname, "exec")  # USE TRACEBACK BELOW HERE
        return None
    except Exception as err:
        end_lineno = getattr(err, "end_lineno", err.lineno)
        line_numbers = list(range(err.lineno - 1, end_lineno))

        tb_lines = traceback.format_exception(type(err), err, err.__traceback__)
        last_file_i = 0

        target = "# USE TRACEBACK"
        target += " BELOW HERE"
        for i in range(len(tb_lines)):
            if target in tb_lines[i]:
                last_file_i = i
                break

        tb_lines = tb_lines[:1] + tb_lines[last_file_i + 1:]

        res = "".join(tb_lines)
        return LintResult(text=res, lines=line_numbers)


# ====== Linter 类 ======


class Linter:
    """代码静态检查器。

    用法::

        linter = Linter(root="/path/to/repo")
        errors = linter.lint("some_file.py")
        if errors:
            print(errors)

    Args:
        encoding: 读取文件时使用的编码。
        root: 仓库根目录，用于计算相对路径及作为子进程 cwd。
    """

    def __init__(self, encoding: str = "utf-8", root: Optional[str] = None):
        self.encoding = encoding
        self.root = root

        # 语言 -> lint 函数/命令 的映射
        self.languages: dict = dict(
            python=self.py_lint,
        )
        # 全局 lint 命令 (对所有语言生效)
        self.all_lint_cmd: Optional[Union[str, Callable]] = None

    def set_linter(self, lang: Optional[str], cmd: Union[str, Callable]) -> None:
        """为指定语言设置自定义 linter; lang 为 None 时设置全局 linter。"""
        if lang:
            self.languages[lang] = cmd
            return
        self.all_lint_cmd = cmd

    def get_rel_fname(self, fname: str) -> str:
        if self.root:
            try:
                return os.path.relpath(fname, self.root)
            except ValueError:
                return fname
        return fname

    def run_cmd(self, cmd: str, rel_fname: str, code: str) -> Optional[LintResult]:
        """执行字符串形式的 lint 命令并解析输出。"""
        full_cmd = cmd + " " + shlex.quote(rel_fname)

        returncode = 0
        stdout = ""
        try:
            result = subprocess.run(
                full_cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self.root,
                encoding=self.encoding,
                errors="replace",
                check=False,
            )
            returncode = result.returncode
            stdout = (result.stdout or "") + (result.stderr or "")
        except OSError as err:
            logger.warning("无法执行 lint 命令: %s", err)
            return None

        errors = stdout
        if returncode == 0:
            return None  # 零退出码视为无错误

        res = f"## Running: {full_cmd}\n\n"
        res += errors

        return self.errors_to_lint_result(rel_fname, res)

    def errors_to_lint_result(self, rel_fname: str, errors: str) -> Optional[LintResult]:
        if not errors:
            return None

        linenums: list = []
        filenames_linenums = find_filenames_and_linenums(errors, [rel_fname])
        if filenames_linenums:
            _filename, nums = next(iter(filenames_linenums.items()))
            linenums = [num - 1 for num in nums]  # 转为 0-based

        return LintResult(text=errors, lines=linenums)

    def lint(self, fname: str, cmd: Optional[Union[str, Callable]] = None) -> Optional[str]:
        """对单个文件执行 lint，返回带上下文标记的错误文本 (无错误返回 None)。

        Args:
            fname: 文件绝对路径。
            cmd: 指定 lint 命令/函数; 为 None 时按扩展名自动选择。
        """
        rel_fname = self.get_rel_fname(fname)
        try:
            code = Path(fname).read_text(encoding=self.encoding, errors="replace")
        except OSError as err:
            logger.warning("无法读取 %s: %s", fname, err)
            return None

        if cmd:
            cmd = cmd.strip() if isinstance(cmd, str) else cmd
        if not cmd:
            lang = _resolve_lang(fname)
            if not lang:
                return None
            if self.all_lint_cmd:
                cmd = self.all_lint_cmd
            else:
                cmd = self.languages.get(lang)

        if callable(cmd):
            lintres = cmd(fname, rel_fname, code)
        elif cmd:
            lintres = self.run_cmd(cmd, rel_fname, code)
        else:
            lintres = basic_lint(rel_fname, code)

        if not lintres:
            return None

        res = "# Fix any errors below, if possible.\n\n"
        res += lintres.text
        res += "\n"
        res += tree_context(rel_fname, code, lintres.lines)

        return res

    def py_lint(self, fname: str, rel_fname: str, code: str) -> Optional[LintResult]:
        """Python 三重检查: tree-sitter 语法 + compile() + flake8。

        注意: 保留 ``(fname, rel_fname, code)`` 三参数签名以兼容 ``lint()`` 的
        callable 命令分发机制。
        """
        basic_res = basic_lint(rel_fname, code)
        compile_res = lint_python_compile(fname, code)
        flake_res = self.flake8_lint(rel_fname)

        text = ""
        lines: Set[int] = set()
        for res in [basic_res, compile_res, flake_res]:
            if not res:
                continue
            if text:
                text += "\n"
            text += res.text
            lines.update(res.lines)

        if text or lines:
            return LintResult(text, sorted(lines))
        return None

    def flake8_lint(self, rel_fname: str) -> Optional[LintResult]:
        """调用 flake8 子进程检查致命错误 (E9/F821/F823/...)。

        flake8 不可用时返回 None。
        """
        if not _FLAKE8_AVAILABLE:
            return None

        fatal = "E9,F821,F823,F831,F406,F407,F701,F702,F704,F706"
        flake8_cmd = [
            sys.executable,
            "-m",
            "flake8",
            f"--select={fatal}",
            "--show-source",
            "--isolated",
            rel_fname,
        ]

        text = f"## Running: {' '.join(flake8_cmd)}\n\n"

        try:
            result = subprocess.run(
                flake8_cmd,
                capture_output=True,
                text=True,
                check=False,
                encoding=self.encoding,
                errors="replace",
                cwd=self.root,
            )
            errors = (result.stdout or "") + (result.stderr or "")
        except Exception as e:
            logger.debug("flake8 执行失败 (可能未安装): %s", e)
            return None

        if not errors:
            return None

        text += errors
        return self.errors_to_lint_result(rel_fname, text)


def main() -> None:
    """命令行入口: ``python -m aider_linter <file1> <file2> ...``"""
    if len(sys.argv) < 2:
        print("Usage: python -m aider_linter <file1> <file2> ...")
        sys.exit(1)

    linter = Linter(root=os.getcwd())
    for file_path in sys.argv[1:]:
        errors = linter.lint(file_path)
        if errors:
            print(errors)


if __name__ == "__main__":
    main()
