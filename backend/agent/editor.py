# 移植自 Aider (Apache 2.0 License) - https://github.com/Aider-AI/aider
#
# 本文件将 Aider 的两种代码编辑格式（EditBlock SEARCH/REPLACE 与 apply_patch）
# 移植为纯函数 + logging 实现，去除了所有 Aider 的 io/Coder 依赖。
#
# 来源文件:
#   - aider/coders/editblock_coder.py  (EditBlock 格式)
#   - aider/coders/patch_coder.py      (Patch 格式)
#
# License: Apache License 2.0
"""代码编辑器 - 支持两种 LLM 代码编辑格式

本模块提供两种主流的 LLM 代码编辑格式解析与应用能力:

1. **EditBlock 格式** (SEARCH/REPLACE)
   - LLM 输出形如 ``<<<<<<< SEARCH`` / ``=======`` / ``>>>>>>> REPLACE`` 的块
   - 支持多策略匹配: 精确匹配 → 空白灵活匹配 → 省略号(...)匹配 → 模糊匹配
   - 适用于大多数通用 LLM (GPT-4, Claude, DeepSeek 等)

2. **Patch 格式** (apply_patch)
   - LLM 输出形如 ``*** Begin Patch`` / ``*** Update File:`` / ``@@`` 的结构化 patch
   - 支持文件新增(Add)、删除(Delete)、更新(Update)三种操作
   - 上下文匹配支持精确匹配、rstrip 匹配、strip 匹配三级模糊度

使用方式::

    from agent.editor import CodeEditor

    editor = CodeEditor()

    # 方式一: SEARCH/REPLACE
    new_content = editor.apply_search_replace(
        file_path="src/main.py",
        content=old_content,
        search_text="old code",
        replace_text="new code",
    )

    # 方式二: apply_patch
    results = editor.apply_patch_text(patch_text, root_dir="/path/to/project")

    # 方式三: 解析 LLM 输出（自动检测格式）
    edits = editor.parse_llm_response(llm_response)
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =========================================================================== #
#  EditBlock 格式 (SEARCH/REPLACE)
# =========================================================================== #

#: 默认围栏符号，使用三个反引号
DEFAULT_FENCE: Tuple[str, str] = ("`" * 3, "`" * 3)

#: 始终愿意将三个反引号视为围栏（用于搜索文件名时）
_TRIPLE_BACKTICKS: str = "`" * 3

# SEARCH/REPLACE 块的分隔符正则
HEAD = r"^<{5,9} SEARCH>?\s*$"
DIVIDER = r"^={5,9}\s*$"
UPDATED = r"^>{5,9} REPLACE\s*$"

# 错误提示中使用的字面量
HEAD_ERR = "<<<<<<< SEARCH"
DIVIDER_ERR = "======="
UPDATED_ERR = ">>>>>>> REPLACE"

#: 合并后的分隔符正则（用于 split）
_SEPARATORS = "|".join([HEAD, DIVIDER, UPDATED])
_SPLIT_RE = re.compile(r"^((?:" + _SEPARATORS + r")[ ]*\n)", re.MULTILINE | re.DOTALL)

#: 文件名缺失时的错误提示
_MISSING_FILENAME_ERR = (
    "Bad/missing filename. The filename must be alone on the line before the opening fence"
    " {fence[0]}"
)


def _prep(content: str) -> Tuple[str, List[str]]:
    """预处理文本内容，确保以换行结尾并拆分为行列表。

    Args:
        content: 原始文本

    Returns:
        (预处理后的完整文本, 行列表)
    """
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    return content, lines


def perfect_replace(
    whole_lines: List[str],
    part_lines: List[str],
    replace_lines: List[str],
) -> Optional[str]:
    """精确匹配替换。

    在 ``whole_lines`` 中查找与 ``part_lines`` 完全一致的连续行块，
    找到后用 ``replace_lines`` 替换。

    Args:
        whole_lines: 完整文件的行列表
        part_lines: 待查找的 SEARCH 行列表
        replace_lines: 替换后的 REPLACE 行列表

    Returns:
        替换后的完整文件内容，未匹配则返回 None
    """
    part_tup = tuple(part_lines)
    part_len = len(part_lines)

    for i in range(len(whole_lines) - part_len + 1):
        whole_tup = tuple(whole_lines[i : i + part_len])
        if part_tup == whole_tup:
            res = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(res)
    return None


def match_but_for_leading_whitespace(
    whole_lines: List[str],
    part_lines: List[str],
) -> Optional[str]:
    """检查两组行是否仅在行首空白上存在差异。

    如果所有行的非空白内容完全一致，且行首空白偏移量统一，
    则返回该统一的偏移量前缀。

    Args:
        whole_lines: 文件中的行片段
        part_lines: SEARCH 行片段

    Returns:
        统一的行首空白前缀，或不匹配时返回 None
    """
    num = len(whole_lines)

    # 非空白部分是否完全一致
    if not all(whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)):
        return None

    # 行首空白偏移量是否统一
    add = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )

    if len(add) != 1:
        return None

    return add.pop()


def replace_part_with_missing_leading_whitespace(
    whole_lines: List[str],
    part_lines: List[str],
    replace_lines: List[str],
) -> Optional[str]:
    """处理 LLM 常见的行首空白错误。

    GPT 等模型经常在 ORIG/UPD 块中统一丢失或保留部分行首空白。
    本函数尝试统一去除最小行首空白后进行匹配。

    Args:
        whole_lines: 完整文件的行列表
        part_lines: SEARCH 行列表
        replace_lines: REPLACE 行列表

    Returns:
        替换后的完整文件内容，未匹配则返回 None
    """
    # 收集所有非空行的行首空白长度
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]

    # 统一去除最小行首空白
    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    num_part_lines = len(part_lines)

    for i in range(len(whole_lines) - num_part_lines + 1):
        add_leading = match_but_for_leading_whitespace(
            whole_lines[i : i + num_part_lines], part_lines
        )

        if add_leading is None:
            continue

        # 将统一的前缀加回到 replace_lines
        replace_lines = [
            add_leading + rline if rline.strip() else rline for rline in replace_lines
        ]
        whole_lines = (
            whole_lines[:i] + replace_lines + whole_lines[i + num_part_lines :]
        )
        return "".join(whole_lines)

    return None


def perfect_or_whitespace(
    whole_lines: List[str],
    part_lines: List[str],
    replace_lines: List[str],
) -> Optional[str]:
    """先尝试精确匹配，失败后尝试空白灵活匹配。

    Args:
        whole_lines: 完整文件的行列表
        part_lines: SEARCH 行列表
        replace_lines: REPLACE 行列表

    Returns:
        替换后的完整文件内容，未匹配则返回 None
    """
    # 策略一: 精确匹配
    res = perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # 策略二: 行首空白灵活匹配
    res = replace_part_with_missing_leading_whitespace(
        whole_lines, part_lines, replace_lines
    )
    if res:
        return res

    return None


def try_dotdotdots(whole: str, part: str, replace: str) -> Optional[str]:
    """处理包含 ``...`` 省略号的 SEARCH/REPLACE 块。

    当 LLM 用 ``...`` 省略中间代码时，本函数将 SEARCH 和 REPLACE
    按 ``...`` 拆分为片段，逐段进行精确匹配和替换。

    Args:
        whole: 完整文件内容
        part: SEARCH 文本（可能含 ``...``）
        replace: REPLACE 文本（可能含 ``...``）

    Returns:
        替换后的完整文件内容；
        无 ``...`` 时返回 None；
        ``...`` 不配对或不匹配时抛出 ValueError

    Raises:
        ValueError: SEARCH 和 REPLACE 中的 ``...`` 不配对或不匹配
    """
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")

    if len(part_pieces) == 1:
        # 无省略号，返回 None 交由其他策略处理
        return None

    # 检查所有 ... 片段（奇数索引）是否一致
    all_dots_match = all(
        part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2)
    )

    if not all_dots_match:
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    # 取偶数索引（非 ... 的文本片段）
    part_pieces = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_pieces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    pairs = zip(part_pieces, replace_pieces)
    for part_piece, replace_piece in pairs:
        if not part_piece and not replace_piece:
            continue

        if not part_piece and replace_piece:
            # SEARCH 为空、REPLACE 非空 → 追加内容
            if not whole.endswith("\n"):
                whole += "\n"
            whole += replace_piece
            continue

        if whole.count(part_piece) == 0:
            raise ValueError("dotdotdots: SEARCH 片段在文件中未找到")
        if whole.count(part_piece) > 1:
            raise ValueError("dotdotdots: SEARCH 片段在文件中出现多次，无法定位")

        whole = whole.replace(part_piece, replace_piece, 1)

    return whole


def replace_closest_edit_distance(
    whole_lines: List[str],
    part: str,
    part_lines: List[str],
    replace_lines: List[str],
) -> Optional[str]:
    """模糊匹配替换（基于编辑距离的相似度）。

    当精确匹配和空白匹配均失败时，使用 SequenceMatcher 计算相似度，
    找到最相似的代码块进行替换。

    Args:
        whole_lines: 完整文件的行列表
        part: SEARCH 原始文本
        part_lines: SEARCH 行列表
        replace_lines: REPLACE 行列表

    Returns:
        替换后的完整文件内容，相似度低于阈值则返回 None
    """
    similarity_thresh = 0.8

    max_similarity = 0.0
    most_similar_chunk_start = -1
    most_similar_chunk_end = -1

    scale = 0.1
    min_len = math.floor(len(part_lines) * (1 - scale))
    max_len = math.ceil(len(part_lines) * (1 + scale))

    for length in range(min_len, max_len):
        for i in range(len(whole_lines) - length + 1):
            chunk = whole_lines[i : i + length]
            chunk = "".join(chunk)

            similarity = SequenceMatcher(None, chunk, part).ratio()

            if similarity > max_similarity and similarity:
                max_similarity = similarity
                most_similar_chunk_start = i
                most_similar_chunk_end = i + length

    if max_similarity < similarity_thresh:
        return None

    modified_whole = (
        whole_lines[:most_similar_chunk_start]
        + replace_lines
        + whole_lines[most_similar_chunk_end:]
    )
    return "".join(modified_whole)


def replace_most_similar_chunk(whole: str, part: str, replace: str) -> Optional[str]:
    """多策略查找 ``part`` 在 ``whole`` 中的位置并用 ``replace`` 替换。

    匹配策略按优先级依次执行:
        1. 精确匹配 (perfect_replace)
        2. 行首空白灵活匹配 (replace_part_with_missing_leading_whitespace)
        3. 跳过首行空行后再次尝试精确/空白匹配
        4. 省略号(...)匹配 (try_dotdotdots)
        5. 模糊匹配 (replace_closest_edit_distance)

    Args:
        whole: 完整文件内容
        part: SEARCH 文本
        replace: REPLACE 文本

    Returns:
        替换后的完整文件内容，所有策略均失败则返回 None
    """
    whole, whole_lines = _prep(whole)
    part, part_lines = _prep(part)
    replace, replace_lines = _prep(replace)

    # 策略一 + 二: 精确匹配 / 空白灵活匹配
    res = perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # 策略三: 跳过首行空行后重试 (GPT 有时会多余地添加空行, issue #25)
    if len(part_lines) > 2 and not part_lines[0].strip():
        skip_blank_line_part_lines = part_lines[1:]
        res = perfect_or_whitespace(
            whole_lines, skip_blank_line_part_lines, replace_lines
        )
        if res:
            return res

    # 策略四: 省略号(...)匹配
    try:
        res = try_dotdotdots(whole, part, replace)
        if res:
            return res
    except ValueError:
        pass

    # 策略五: 模糊匹配
    res = replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines)
    if res:
        return res

    return None


def strip_quoted_wrapping(
    res: str,
    fname: Optional[str] = None,
    fence: Tuple[str, str] = DEFAULT_FENCE,
) -> str:
    """去除文本外层的多余包装（文件名、围栏符号）。

    例如输入::

        filename.ext
        ```
        We just want this content
        Not the filename and triple quotes
        ```

    将返回中间的实际内容。

    Args:
        res: 可能带包装的文本
        fname: 文件名，用于匹配首行
        fence: 围栏符号对

    Returns:
        去除包装后的文本
    """
    if not res:
        return res

    res = res.splitlines()

    if fname and res[0].strip().endswith(Path(fname).name):
        res = res[1:]

    if res[0].startswith(fence[0]) and res[-1].startswith(fence[1]):
        res = res[1:-1]

    res = "\n".join(res)
    if res and res[-1] != "\n":
        res += "\n"

    return res


def do_replace(
    fname: str,
    content: Optional[str],
    before_text: str,
    after_text: str,
    fence: Optional[Tuple[str, str]] = None,
) -> Optional[str]:
    """执行 SEARCH/REPLACE 替换（纯函数，不操作文件系统）。

    Args:
        fname: 文件路径（仅用于 strip_quoted_wrapping 去除文件名包装）
        content: 当前文件内容；None 表示文件不存在
        before_text: SEARCH 文本
        after_text: REPLACE 文本
        fence: 围栏符号对，默认为三个反引号

    Returns:
        替换后的完整文件内容；替换失败返回 None
    """
    if fence is None:
        fence = DEFAULT_FENCE

    before_text = strip_quoted_wrapping(before_text, fname, fence)
    after_text = strip_quoted_wrapping(after_text, fname, fence)

    # 文件不存在且 before_text 为空 → 创建新文件
    if content is None and not before_text.strip():
        content = ""

    if content is None:
        return None

    if not before_text.strip():
        # 追加到现有文件，或创建新文件
        new_content = content + after_text
    else:
        new_content = replace_most_similar_chunk(content, before_text, after_text)

    return new_content


def strip_filename(filename: str, fence: Tuple[str, str]) -> Optional[str]:
    r"""从可能带包装的行中提取文件名。

    处理各种 LLM 输出风格:
    - ````python word_count.py```` （围栏 + 文件名）
    - ``# word_count.py`` （注释前缀）
    - ``word_count.py:`` （冒号后缀）
    - ``\`word_count.py\``` （反引号包裹）

    Args:
        filename: 可能带包装的文件名字符串
        fence: 围栏符号对

    Returns:
        清理后的文件名，无效则返回 None
    """
    filename = filename.strip()

    if filename == "...":
        return None

    start_fence = fence[0]
    if filename.startswith(start_fence):
        candidate = filename[len(start_fence) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return None

    if filename.startswith(_TRIPLE_BACKTICKS):
        candidate = filename[len(_TRIPLE_BACKTICKS) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return None

    filename = filename.rstrip(":")
    filename = filename.lstrip("#")
    filename = filename.strip()
    filename = filename.strip("`")
    filename = filename.strip("*")

    return filename


def find_filename(
    lines: List[str],
    fence: Tuple[str, str],
    valid_fnames: Optional[List[str]],
) -> Optional[str]:
    """从 SEARCH 块前的若干行中查找文件名。

    Deepseek Coder v2 等模型有时会输出::

        ```python
        word_count.py
        ```
        ```python
        <<<<<<< SEARCH
        ...

    本函数向前回溯最多 3 行，灵活查找文件名。

    Args:
        lines: SEARCH 行之前的若干行（将逆序搜索）
        fence: 围栏符号对
        valid_fnames: 当前对话中有效的文件名列表，用于模糊匹配

    Returns:
        最佳匹配的文件名，未找到返回 None
    """
    if valid_fnames is None:
        valid_fnames = []

    # 回溯最近 3 行
    lines = list(reversed(lines))
    lines = lines[:3]

    filenames = []
    for line in lines:
        filename = strip_filename(line, fence)
        if filename:
            filenames.append(filename)

        # 仅在持续看到围栏行时继续回溯
        if not line.startswith(fence[0]) and not line.startswith(_TRIPLE_BACKTICKS):
            break

    if not filenames:
        return None

    # 选择最佳文件名

    # 1. 精确匹配
    for fname in filenames:
        if fname in valid_fnames:
            return fname

    # 2. 基名匹配（basename）
    for fname in filenames:
        for vfn in valid_fnames:
            if fname == Path(vfn).name:
                return vfn

    # 3. 模糊匹配
    for fname in filenames:
        close_matches = difflib.get_close_matches(fname, valid_fnames, n=1, cutoff=0.8)
        if len(close_matches) == 1:
            return close_matches[0]

    # 4. 任意带扩展名的文件名
    for fname in filenames:
        if "." in fname:
            return fname

    if filenames:
        return filenames[0]

    return None


def find_original_update_blocks(
    content: str,
    fence: Tuple[str, str] = DEFAULT_FENCE,
    valid_fnames: Optional[List[str]] = None,
):
    """解析 LLM 输出中的 SEARCH/REPLACE 块。

    本函数为生成器，逐个 yield 解析出的编辑指令。
    每个指令为以下两种形式之一:
        - ``(None, shell_content)`` — shell 命令
        - ``(filename, original_text, updated_text)`` — SEARCH/REPLACE 编辑

    Args:
        content: LLM 的完整输出文本
        fence: 围栏符号对
        valid_fnames: 当前对话中有效的文件名列表

    Yields:
        Tuple 形式的编辑指令

    Raises:
        ValueError: SEARCH/REPLACE 块格式错误
    """
    lines = content.splitlines(keepends=True)
    i = 0
    current_filename = None

    head_pattern = re.compile(HEAD)
    divider_pattern = re.compile(DIVIDER)
    updated_pattern = re.compile(UPDATED)

    # shell 代码块的起始标记
    shell_starts = [
        "```bash",
        "```sh",
        "```shell",
        "```cmd",
        "```batch",
        "```powershell",
        "```ps1",
        "```zsh",
        "```fish",
        "```ksh",
        "```csh",
        "```tcsh",
    ]

    while i < len(lines):
        line = lines[i]

        # 检查后续 1~2 行是否为 editblock（如果是，则不当 shell 块处理）
        next_is_editblock = (
            i + 1 < len(lines)
            and head_pattern.match(lines[i + 1].strip())
            or i + 2 < len(lines)
            and head_pattern.match(lines[i + 2].strip())
        )

        # 处理 shell 代码块
        if (
            any(line.strip().startswith(start) for start in shell_starts)
            and not next_is_editblock
        ):
            shell_content = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                shell_content.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1  # 跳过闭合 ```

            yield None, "".join(shell_content)
            continue

        # 处理 SEARCH/REPLACE 块
        if head_pattern.match(line.strip()):
            try:
                # 如果 HEAD 后紧跟 DIVIDER，说明是新建空文件
                if i + 1 < len(lines) and divider_pattern.match(lines[i + 1].strip()):
                    filename = find_filename(lines[max(0, i - 3) : i], fence, None)
                else:
                    filename = find_filename(
                        lines[max(0, i - 3) : i], fence, valid_fnames
                    )

                if not filename:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(_MISSING_FILENAME_ERR.format(fence=fence))

                current_filename = filename

                # 收集 SEARCH (original) 文本
                original_text = []
                i += 1
                while i < len(lines) and not divider_pattern.match(lines[i].strip()):
                    original_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not divider_pattern.match(lines[i].strip()):
                    raise ValueError(f"Expected `{DIVIDER_ERR}`")

                # 收集 REPLACE (updated) 文本
                updated_text = []
                i += 1
                while i < len(lines) and not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    updated_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    raise ValueError(f"Expected `{UPDATED_ERR}` or `{DIVIDER_ERR}`")

                yield filename, "".join(original_text), "".join(updated_text)

            except ValueError as e:
                processed = "".join(lines[: i + 1])
                err = e.args[0]
                raise ValueError(f"{processed}\n^^^ {err}")

        i += 1


def find_similar_lines(
    search_lines: str,
    content_lines: str,
    threshold: float = 0.6,
) -> str:
    """在文件内容中查找与 SEARCH 文本最相似的行块。

    用于 SEARCH 匹配失败时给出 "Did you mean..." 建议。

    Args:
        search_lines: SEARCH 文本
        content_lines: 文件内容
        threshold: 相似度阈值，低于此值返回空字符串

    Returns:
        最相似的行块文本，低于阈值返回空字符串
    """
    search_lines = search_lines.splitlines()
    content_lines = content_lines.splitlines()

    best_ratio = 0.0
    best_match = None
    best_match_i = 0

    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = content_lines[i : i + len(search_lines)]
        ratio = SequenceMatcher(None, search_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = chunk
            best_match_i = i

    if best_ratio < threshold:
        return ""

    # 首尾行都匹配时直接返回
    if best_match[0] == search_lines[0] and best_match[-1] == search_lines[-1]:
        return "\n".join(best_match)

    # 扩展上下文 N 行
    N = 5
    best_match_end = min(len(content_lines), best_match_i + len(search_lines) + N)
    best_match_i = max(0, best_match_i - N)

    best = content_lines[best_match_i:best_match_end]
    return "\n".join(best)


# =========================================================================== #
#  Patch 格式 (apply_patch)
# =========================================================================== #


class DiffError(ValueError):
    """解析或应用 patch 时发生的错误。"""


class ActionType(str, Enum):
    """patch 操作类型。"""

    ADD = "Add"
    DELETE = "Delete"
    UPDATE = "Update"


@dataclass
class Chunk:
    """patch 中的一个变更块。

    Attributes:
        orig_index: 变更在原始文件中的起始行号
        del_lines: 待删除的行
        ins_lines: 待插入的行
    """

    orig_index: int = -1
    del_lines: List[str] = field(default_factory=list)
    ins_lines: List[str] = field(default_factory=list)


@dataclass
class PatchAction:
    """针对单个文件的 patch 操作。

    Attributes:
        type: 操作类型 (ADD / DELETE / UPDATE)
        path: 文件路径
        new_content: ADD 操作时的文件新内容
        chunks: UPDATE 操作时的变更块列表
        move_path: UPDATE 操作时的移动目标路径
    """

    type: ActionType
    path: str
    new_content: Optional[str] = None
    chunks: List[Chunk] = field(default_factory=list)
    move_path: Optional[str] = None


@dataclass
class Patch:
    """完整的 patch 对象，包含所有文件操作。

    Attributes:
        actions: {文件路径: PatchAction}
        fuzz: 解析过程中累积的模糊度
    """

    actions: Dict[str, PatchAction] = field(default_factory=dict)
    fuzz: int = 0


def _norm(line: str) -> str:
    """去除行尾回车符，使 LF 和 CRLF 输入的比较结果一致。

    Args:
        line: 原始行

    Returns:
        去除 ``\\r`` 后的行
    """
    return line.rstrip("\r")


def find_context_core(
    lines: List[str],
    context: List[str],
    start: int,
) -> Tuple[int, int]:
    """在文件行列表中查找上下文块的起始位置。

    支持三级模糊度匹配:
        - fuzz 0: 精确匹配
        - fuzz 1: rstrip 匹配（忽略行尾空白）
        - fuzz 100: strip 匹配（忽略行首尾空白）

    Args:
        lines: 文件行列表
        context: 待查找的上下文行块
        start: 搜索起始行号

    Returns:
        (匹配起始行号, 模糊度)；未匹配返回 (-1, 0)
    """
    if not context:
        return start, 0

    # 第一级: 精确匹配
    for i in range(start, len(lines) - len(context) + 1):
        if lines[i : i + len(context)] == context:
            return i, 0

    # 第二级: rstrip 匹配
    norm_context = [s.rstrip() for s in context]
    for i in range(start, len(lines) - len(context) + 1):
        if [s.rstrip() for s in lines[i : i + len(context)]] == norm_context:
            return i, 1

    # 第三级: strip 匹配
    norm_context_strip = [s.strip() for s in context]
    for i in range(start, len(lines) - len(context) + 1):
        if [s.strip() for s in lines[i : i + len(context)]] == norm_context_strip:
            return i, 100

    return -1, 0


def find_context(
    lines: List[str],
    context: List[str],
    start: int,
    eof: bool,
) -> Tuple[int, int]:
    """查找上下文块，处理 EOF 标记。

    Args:
        lines: 文件行列表
        context: 待查找的上下文行块
        start: 搜索起始行号
        eof: 是否为文件末尾标记

    Returns:
        (匹配起始行号, 模糊度)；未匹配返回 (-1, ...)
    """
    if eof:
        # EOF 标记: 先尝试在文件末尾匹配
        if len(lines) >= len(context):
            new_index, fuzz = find_context_core(
                lines, context, len(lines) - len(context)
            )
            if new_index != -1:
                return new_index, fuzz
        # 末尾未匹配: 从 start 位置搜索，附加大模糊度惩罚
        new_index, fuzz = find_context_core(lines, context, start)
        return new_index, fuzz + 10_000

    # 普通情况: 从 start 位置搜索
    return find_context_core(lines, context, start)


def peek_next_section(
    lines: List[str],
    index: int,
) -> Tuple[List[str], List[Chunk], int, bool]:
    """解析 Update 块中的一个 section（上下文行 + 增删行）。

    patch 中每个 section 由以下行组成:
        - `` `` 开头的行为上下文行 (keep)
        - ``-`` 开头的行为删除行 (delete)
        - ``+`` 开头的行为新增行 (add)
        - 空行视为上下文空行

    Args:
        lines: patch 行列表
        index: 当前解析位置

    Returns:
        (上下文行列表, 变更块列表, 下一个索引, 是否为 EOF)

    Raises:
        DiffError: section 格式错误
    """
    context_lines: List[str] = []
    del_lines: List[str] = []
    ins_lines: List[str] = []
    chunks: List[Chunk] = []
    mode = "keep"
    start_index = index

    while index < len(lines):
        line = lines[index]
        norm_line = _norm(line)

        # 检查 section 终止符
        if norm_line.startswith(
            (
                "@@",
                "*** End Patch",
                "*** Update File:",
                "*** Delete File:",
                "*** Add File:",
                "*** End of File",
            )
        ):
            break
        if norm_line == "***":
            break
        if norm_line.startswith("***"):
            raise DiffError(f"Invalid patch line found in update section: {line}")

        index += 1
        last_mode = mode

        # 确定行类型并去除前缀
        if line.startswith("+"):
            mode = "add"
            line_content = line[1:]
        elif line.startswith("-"):
            mode = "delete"
            line_content = line[1:]
        elif line.startswith(" "):
            mode = "keep"
            line_content = line[1:]
        elif line.strip() == "":
            # 空行视为上下文空行
            mode = "keep"
            line_content = ""
        else:
            raise DiffError(f"Invalid line prefix in update section: {line}")

        # 模式从 add/delete 切回 keep 时，finalize 前一个 chunk
        if mode == "keep" and last_mode != "keep":
            if del_lines or ins_lines:
                chunks.append(
                    Chunk(
                        orig_index=len(context_lines) - len(del_lines),
                        del_lines=del_lines,
                        ins_lines=ins_lines,
                    )
                )
            del_lines, ins_lines = [], []

        # 按模式收集行
        if mode == "delete":
            del_lines.append(line_content)
            context_lines.append(line_content)  # 删除行也是原始上下文的一部分
        elif mode == "add":
            ins_lines.append(line_content)
        elif mode == "keep":
            context_lines.append(line_content)

    # finalize 末尾的 chunk
    if del_lines or ins_lines:
        chunks.append(
            Chunk(
                orig_index=len(context_lines) - len(del_lines),
                del_lines=del_lines,
                ins_lines=ins_lines,
            )
        )

    # 检查 EOF 标记
    is_eof = False
    if index < len(lines) and _norm(lines[index]) == "*** End of File":
        index += 1
        is_eof = True

    if index == start_index and not is_eof:
        raise DiffError("Empty patch section found.")

    return context_lines, chunks, index, is_eof


def identify_files_needed(text: str) -> List[str]:
    """从 patch 文本中提取需要读取的文件路径。

    仅提取 Update 和 Delete 操作涉及的文件（这些操作需要读取现有文件内容）。

    Args:
        text: patch 文本

    Returns:
        文件路径列表
    """
    lines = text.splitlines()
    paths = set()
    for line in lines:
        norm_line = _norm(line)
        if norm_line.startswith("*** Update File: "):
            paths.add(norm_line[len("*** Update File: ") :].strip())
        elif norm_line.startswith("*** Delete File: "):
            paths.add(norm_line[len("*** Delete File: ") :].strip())
    return list(paths)


def parse_add_file_content(
    lines: List[str],
    index: int,
) -> Tuple[PatchAction, int]:
    """解析 Add File 操作的内容行。

    Args:
        lines: patch 行列表
        index: 当前解析位置（指向第一行内容）

    Returns:
        (PatchAction, 下一个索引)

    Raises:
        DiffError: 内容行格式错误
    """
    added_lines: List[str] = []
    while index < len(lines):
        line = lines[index]
        norm_line = _norm(line)

        # 遇到下一个操作或结束标记时停止
        if norm_line.startswith(
            (
                "*** End Patch",
                "*** Update File:",
                "*** Delete File:",
                "*** Add File:",
            )
        ):
            break

        if not line.startswith("+"):
            if norm_line.strip() == "":
                # 空行视为添加空行
                added_lines.append("")
            else:
                raise DiffError(f"Invalid Add File line (missing '+'): {line}")
        else:
            added_lines.append(line[1:])  # 去除前导 '+'

        index += 1

    action = PatchAction(
        type=ActionType.ADD, path="", new_content="\n".join(added_lines)
    )
    return action, index


def parse_update_file_sections(
    lines: List[str],
    index: int,
    file_content: str,
) -> Tuple[PatchAction, int, int]:
    """解析单个 Update File 操作的所有 section。

    Args:
        lines: patch 行列表
        index: 当前解析位置
        file_content: 原始文件内容

    Returns:
        (PatchAction, 下一个索引, 累积模糊度)

    Raises:
        DiffError: 上下文匹配失败或格式错误
    """
    action = PatchAction(type=ActionType.UPDATE, path="")
    orig_lines = file_content.splitlines()
    current_file_index = 0
    total_fuzz = 0

    while index < len(lines):
        norm_line = _norm(lines[index])

        # 检查当前文件更新段的终止符
        if norm_line.startswith(
            (
                "*** End Patch",
                "*** Update File:",
                "*** Delete File:",
                "*** Add File:",
            )
        ):
            break

        # 处理 @@ 作用域行（可选）
        scope_lines = []
        while index < len(lines) and _norm(lines[index]).startswith("@@"):
            scope_line_content = lines[index][len("@@") :].strip()
            if scope_line_content:
                scope_lines.append(scope_line_content)
            index += 1

        # 在原始文件中查找作用域
        if scope_lines:
            found_scope = False
            temp_index = current_file_index
            while temp_index < len(orig_lines):
                match = True
                for i, scope in enumerate(scope_lines):
                    if (
                        temp_index + i >= len(orig_lines)
                        or _norm(orig_lines[temp_index + i]).strip() != scope
                    ):
                        match = False
                        break
                if match:
                    current_file_index = temp_index + len(scope_lines)
                    found_scope = True
                    break
                temp_index += 1

            if not found_scope:
                # 尝试模糊作用域匹配（strip 空白）
                temp_index = current_file_index
                while temp_index < len(orig_lines):
                    match = True
                    for i, scope in enumerate(scope_lines):
                        if (
                            temp_index + i >= len(orig_lines)
                            or _norm(orig_lines[temp_index + i]).strip()
                            != scope.strip()
                        ):
                            match = False
                            break
                    if match:
                        current_file_index = temp_index + len(scope_lines)
                        found_scope = True
                        total_fuzz += 1
                        break
                    temp_index += 1

            if not found_scope:
                scope_txt = "\n".join(scope_lines)
                raise DiffError(f"Could not find scope context:\n{scope_txt}")

        # 解析下一个 context/change section
        context_block, chunks_in_section, next_index, is_eof = peek_next_section(
            lines, index
        )

        # 在原始文件中查找上下文块位置
        found_index, fuzz = find_context(
            orig_lines, context_block, current_file_index, is_eof
        )
        total_fuzz += fuzz

        if found_index == -1:
            ctx_txt = "\n".join(context_block)
            marker = "*** End of File" if is_eof else ""
            raise DiffError(
                f"Could not find patch context {marker} starting near line"
                f" {current_file_index}:\n{ctx_txt}"
            )

        # 将 chunk 的 orig_index 调整为文件内的绝对行号
        for chunk in chunks_in_section:
            chunk.orig_index += found_index
            action.chunks.append(chunk)

        # 推进文件索引到上下文块之后
        current_file_index = found_index + len(context_block)
        index = next_index

    return action, index, total_fuzz


def parse_patch_text(
    lines: List[str],
    start_index: int,
    current_files: Dict[str, str],
) -> Patch:
    """解析 patch 文本为 Patch 对象。

    Args:
        lines: patch 行列表
        start_index: 起始解析位置（跳过 ``*** Begin Patch``）
        current_files: {文件路径: 文件内容}，UPDATE/DELETE 操作需要

    Returns:
        解析后的 Patch 对象

    Raises:
        DiffError: patch 格式错误
    """
    patch = Patch()
    index = start_index
    fuzz_accumulator = 0

    while index < len(lines):
        line = lines[index]
        norm_line = _norm(line)

        if norm_line == "*** End Patch":
            index += 1
            break

        # ---------- UPDATE ---------- #
        if norm_line.startswith("*** Update File: "):
            path = norm_line[len("*** Update File: ") :].strip()
            index += 1
            if not path:
                raise DiffError("Update File action missing path.")

            # 可选的 Move to 目标
            move_to = None
            if index < len(lines) and _norm(lines[index]).startswith("*** Move to: "):
                move_to = _norm(lines[index])[len("*** Move to: ") :].strip()
                index += 1
                if not move_to:
                    raise DiffError("Move to action missing path.")

            if path not in current_files:
                raise DiffError(f"Update File Error - missing file content for: {path}")

            file_content = current_files[path]

            existing_action = patch.actions.get(path)
            if existing_action is not None:
                # 合并到已有的 UPDATE 块
                if existing_action.type != ActionType.UPDATE:
                    raise DiffError(f"Conflicting actions for file: {path}")

                new_action, index, fuzz = parse_update_file_sections(
                    lines, index, file_content
                )
                existing_action.chunks.extend(new_action.chunks)

                if move_to:
                    if (
                        existing_action.move_path
                        and existing_action.move_path != move_to
                    ):
                        raise DiffError(f"Conflicting move targets for file: {path}")
                    existing_action.move_path = move_to
                fuzz_accumulator += fuzz
            else:
                # 首次出现该文件的 UPDATE 块
                action, index, fuzz = parse_update_file_sections(
                    lines, index, file_content
                )
                action.path = path
                action.move_path = move_to
                patch.actions[path] = action
                fuzz_accumulator += fuzz
            continue

        # ---------- DELETE ---------- #
        elif norm_line.startswith("*** Delete File: "):
            path = norm_line[len("*** Delete File: ") :].strip()
            index += 1
            if not path:
                raise DiffError("Delete File action missing path.")
            existing_action = patch.actions.get(path)
            if existing_action:
                if existing_action.type == ActionType.DELETE:
                    # 重复删除，忽略
                    logger.warning("重复的删除操作已忽略: %s", path)
                    continue
                else:
                    raise DiffError(f"Conflicting actions for file: {path}")

            patch.actions[path] = PatchAction(type=ActionType.DELETE, path=path)
            continue

        # ---------- ADD ---------- #
        elif norm_line.startswith("*** Add File: "):
            path = norm_line[len("*** Add File: ") :].strip()
            index += 1
            if not path:
                raise DiffError("Add File action missing path.")
            if path in patch.actions:
                raise DiffError(f"Duplicate action for file: {path}")

            action, index = parse_add_file_content(lines, index)
            action.path = path
            patch.actions[path] = action
            continue

        # 允许操作之间的空行
        if not norm_line.strip():
            index += 1
            continue

        raise DiffError(f"Unknown or misplaced line while parsing patch: {line}")

    patch.fuzz = fuzz_accumulator
    return patch


def apply_update(text: str, action: PatchAction, path: str) -> str:
    """将 UPDATE 类型的 chunks 应用到文件文本。

    Args:
        text: 原始文件内容
        action: UPDATE 类型的 PatchAction
        path: 文件路径（用于错误信息）

    Returns:
        更新后的文件内容

    Raises:
        DiffError: chunk 重叠、顺序错误或内容不匹配
    """
    if action.type is not ActionType.UPDATE:
        raise DiffError("_apply_update called with non-update action")

    orig_lines = text.splitlines()
    dest_lines: List[str] = []
    current_orig_line_idx = 0

    # 按 orig_index 排序 chunks
    sorted_chunks = sorted(action.chunks, key=lambda c: c.orig_index)

    for chunk in sorted_chunks:
        chunk_start_index = chunk.orig_index

        if chunk_start_index < current_orig_line_idx:
            raise DiffError(
                f"{path}: Overlapping or out-of-order chunk detected."
                f" Current index {current_orig_line_idx}, chunk starts at {chunk_start_index}."
            )

        # 添加上一个 chunk 与当前 chunk 之间的原始行
        dest_lines.extend(orig_lines[current_orig_line_idx:chunk_start_index])

        # 验证待删除行与文件实际内容匹配
        num_del = len(chunk.del_lines)
        actual_deleted_lines = orig_lines[
            chunk_start_index : chunk_start_index + num_del
        ]

        norm_chunk_del = [_norm(s).strip() for s in chunk.del_lines]
        norm_actual_del = [_norm(s).strip() for s in actual_deleted_lines]

        if norm_chunk_del != norm_actual_del:
            expected_str = "\n".join(f"- {s}" for s in chunk.del_lines)
            actual_str = "\n".join(f"  {s}" for s in actual_deleted_lines)
            raise DiffError(
                f"{path}: Mismatch applying patch near line {chunk_start_index + 1}.\n"
                f"Expected lines to remove:\n{expected_str}\n"
                f"Found lines in file:\n{actual_str}"
            )

        # 添加插入行
        dest_lines.extend(chunk.ins_lines)

        # 推进原始行索引
        current_orig_line_idx = chunk_start_index + num_del

    # 添加最后一个 chunk 之后的剩余行
    dest_lines.extend(orig_lines[current_orig_line_idx:])

    # 拼接并确保单个尾部换行
    result = "\n".join(dest_lines)
    if result or orig_lines:
        result += "\n"
    return result


# =========================================================================== #
#  统一编辑指令
# =========================================================================== #


@dataclass
class Edit:
    """统一的编辑指令数据类。

    表示从 LLM 输出中解析出的一条编辑指令，
    可以是 SEARCH/REPLACE 格式、patch 格式或 shell 命令。

    Attributes:
        path: 目标文件路径；shell 命令时为 None
        search_text: SEARCH 文本（edit_type='search_replace' 时有效）
        replace_text: REPLACE 文本（edit_type='search_replace' 时有效）
        action: PatchAction 对象（edit_type='patch' 时有效）
        edit_type: 编辑类型，取值为 'search_replace' / 'patch' / 'shell'
        shell_command: shell 命令文本（edit_type='shell' 时有效）
    """

    path: Optional[str]
    search_text: str = ""
    replace_text: str = ""
    action: Optional[PatchAction] = None
    edit_type: str = "search_replace"
    shell_command: str = ""


# =========================================================================== #
#  CodeEditor 统一类
# =========================================================================== #


class CodeEditor:
    """统一的代码编辑器，支持 EditBlock 与 Patch 两种编辑格式。

    本类封装了 Aider 的两种代码编辑格式，提供统一的接口:
        - ``apply_search_replace``: 应用 SEARCH/REPLACE 编辑
        - ``apply_patch_text``: 应用 apply_patch 格式的 patch
        - ``parse_llm_response``: 自动检测格式并解析 LLM 输出

    所有方法均为纯函数式实现（除 ``apply_patch_text`` 需要读取文件），
    不依赖 Aider 的 io/Coder 组件。

    Attributes:
        fence: 围栏符号对，默认为三个反引号
    """

    def __init__(self, fence: Tuple[str, str] = DEFAULT_FENCE):
        """初始化代码编辑器。

        Args:
            fence: 围栏符号对，默认为三个反引号
        """
        self.fence = fence

    # ------------------------------------------------------------------ #
    #  EditBlock (SEARCH/REPLACE) 接口
    # ------------------------------------------------------------------ #

    def apply_search_replace(
        self,
        file_path: str,
        content: Optional[str],
        search_text: str,
        replace_text: str,
    ) -> Optional[str]:
        """应用 SEARCH/REPLACE 编辑。

        使用多策略匹配在 ``content`` 中查找 ``search_text`` 并替换为
        ``replace_text``。匹配策略依次为:
        精确匹配 → 空白灵活匹配 → 跳过首行空行 → 省略号匹配 → 模糊匹配。

        Args:
            file_path: 文件路径（仅用于去除文件名包装，不读取文件）
            content: 当前文件内容；None 表示文件不存在（将创建新文件）
            search_text: SEARCH 文本，为空时表示追加/创建文件
            replace_text: REPLACE 文本

        Returns:
            替换后的完整文件内容；匹配失败返回 None

        Example::

            editor = CodeEditor()
            new = editor.apply_search_replace(
                file_path="main.py",
                content="print('hello')",
                search_text="print('hello')",
                replace_text="print('world')",
            )
            # new == "print('world')"
        """
        return do_replace(file_path, content, search_text, replace_text, self.fence)

    # ------------------------------------------------------------------ #
    #  Patch (apply_patch) 接口
    # ------------------------------------------------------------------ #

    def apply_patch_text(
        self,
        patch_text: str,
        root_dir: str,
    ) -> Dict[str, Optional[str]]:
        """应用 apply_patch 格式的 patch 文本。

        解析 patch 文本，读取 ``root_dir`` 下的相关文件，
        执行 ADD / DELETE / UPDATE 操作，返回每个文件的新内容。

        Args:
            patch_text: apply_patch 格式的文本
            root_dir: 项目根目录，用于读取和定位文件

        Returns:
            {文件路径: 新内容}；
            ADD/UPDATE 返回新内容字符串；
            DELETE 返回 None（表示文件应被删除）

        Raises:
            ValueError: patch 解析或应用失败

        Example::

            editor = CodeEditor()
            results = editor.apply_patch_text(
                patch_text=\"\"\"
            *** Begin Patch
            *** Update File: src/main.py
            @@print('hello')
            -print('hello')
            +print('world')
            *** End Patch
            \"\"\",
                root_dir="/path/to/project",
            )
            # results == {"src/main.py": "print('world')\\n"}
        """
        if not patch_text or not patch_text.strip():
            return {}

        lines = patch_text.splitlines()

        # 确定起始索引（跳过 *** Begin Patch）
        if lines and _norm(lines[0]).startswith("*** Begin Patch"):
            start_index = 1
        else:
            # 容忍缺失的哨兵标记
            is_patch_like = any(
                _norm(line).startswith(
                    ("@@", "*** Update File:", "*** Add File:", "*** Delete File:")
                )
                for line in lines
            )
            if not is_patch_like:
                logger.warning("响应文本不像 patch 格式，跳过应用")
                return {}
            logger.warning(
                "Patch 格式警告: 缺少 '*** Begin Patch'/'*** End Patch' 哨兵"
            )
            start_index = 0

        # 读取需要文件的内容
        needed_paths = identify_files_needed(patch_text)
        current_files: Dict[str, str] = {}
        for rel_path in needed_paths:
            abs_path = os.path.join(root_dir, rel_path)
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    current_files[rel_path] = f.read()
            except FileNotFoundError:
                logger.error("patch 引用的文件不存在: %s", rel_path)
                raise ValueError(f"文件不存在: {rel_path}")
            except IOError as e:
                logger.error("读取文件失败 %s: %s", rel_path, e)
                raise ValueError(f"读取文件失败 {rel_path}: {e}")

        # 解析 patch
        try:
            patch_obj = parse_patch_text(lines, start_index, current_files)
        except DiffError as e:
            raise ValueError(f"解析 patch 失败: {e}")
        except Exception as e:
            raise ValueError(f"解析 patch 时发生意外错误: {e}")

        if patch_obj.fuzz > 0:
            logger.info("patch 解析完成，累积模糊度: %d", patch_obj.fuzz)

        # 应用操作
        results: Dict[str, Optional[str]] = {}
        for path, action in patch_obj.actions.items():
            try:
                if action.type == ActionType.ADD:
                    content = action.new_content or ""
                    if not content.endswith("\n"):
                        content += "\n"
                    results[path] = content
                    logger.info("新增文件: %s", path)

                elif action.type == ActionType.DELETE:
                    results[path] = None  # None 表示删除
                    logger.info("删除文件: %s", path)

                elif action.type == ActionType.UPDATE:
                    file_content = current_files.get(path, "")
                    new_content = apply_update(file_content, action, path)
                    results[path] = new_content
                    if action.move_path:
                        logger.info("更新并移动文件: %s -> %s", path, action.move_path)
                        # 同时返回移动目标路径的内容
                        results[action.move_path] = new_content
                        # 原路径标记为删除
                        results[path] = None
                    else:
                        logger.info("更新文件: %s", path)

            except DiffError as e:
                logger.error("应用 %s 操作失败 %s: %s", action.type, path, e)
                raise ValueError(f"应用操作 '{action.type}' 到 {path} 失败: {e}")
            except Exception as e:
                logger.error("应用 %s 操作发生意外错误 %s: %s", action.type, path, e)
                raise ValueError(
                    f"应用操作 '{action.type}' 到 {path} 发生意外错误: {e}"
                )

        return results

    # ------------------------------------------------------------------ #
    #  LLM 输出解析接口
    # ------------------------------------------------------------------ #

    def parse_llm_response(self, content: str) -> List[Edit]:
        """解析 LLM 输出中的编辑指令（自动检测格式）。

        自动检测 LLM 输出格式:
            - 包含 ``*** Begin Patch`` / ``*** Update File:`` 等标记 → patch 格式
            - 包含 ``<<<<<<< SEARCH`` 等标记 → EditBlock 格式
            - 包含 shell 代码块 → 提取为 shell 命令

        Args:
            content: LLM 的完整输出文本

        Returns:
            Edit 列表，每项代表一条编辑指令

        Example::

            editor = CodeEditor()
            edits = editor.parse_llm_response(llm_output)
            for edit in edits:
                if edit.edit_type == 'search_replace':
                    new = editor.apply_search_replace(
                        edit.path, old_content, edit.search_text, edit.replace_text
                    )
                elif edit.edit_type == 'patch':
                    results = editor.apply_patch_text(patch_text, root_dir)
        """
        if not content or not content.strip():
            return []

        lines = content.splitlines()

        # 检测是否为 patch 格式
        is_patch = any(
            _norm(line).startswith(
                (
                    "*** Begin Patch",
                    "*** Update File:",
                    "*** Add File:",
                    "*** Delete File:",
                )
            )
            for line in lines
        )

        if is_patch:
            return self._parse_patch_response(content)
        else:
            return self._parse_editblock_response(content)

    def _parse_editblock_response(self, content: str) -> List[Edit]:
        """解析 EditBlock (SEARCH/REPLACE) 格式的 LLM 输出。

        Args:
            content: LLM 输出文本

        Returns:
            Edit 列表
        """
        edits: List[Edit] = []
        try:
            for item in find_original_update_blocks(content, self.fence):
                if item[0] is None:
                    # shell 命令
                    edits.append(
                        Edit(
                            path=None,
                            edit_type="shell",
                            shell_command=item[1],
                        )
                    )
                else:
                    path, original, updated = item
                    edits.append(
                        Edit(
                            path=path,
                            search_text=original,
                            replace_text=updated,
                            edit_type="search_replace",
                        )
                    )
        except ValueError as e:
            logger.error("解析 SEARCH/REPLACE 格式失败: %s", e)
        return edits

    def _parse_patch_response(self, content: str) -> List[Edit]:
        """解析 patch 格式的 LLM 输出（轻量提取，不做上下文匹配）。

        本方法仅提取文件路径、操作类型和 ADD 内容，
        不执行上下文匹配（需要文件内容）。完整解析请使用 ``apply_patch_text``。

        Args:
            content: LLM 输出文本

        Returns:
            Edit 列表
        """
        lines = content.splitlines()
        edits: List[Edit] = []
        i = 0

        # 跳过 *** Begin Patch
        if i < len(lines) and _norm(lines[i]).startswith("*** Begin Patch"):
            i += 1

        while i < len(lines):
            norm_line = _norm(lines[i])

            if norm_line == "*** End Patch":
                break

            elif norm_line.startswith("*** Update File: "):
                path = norm_line[len("*** Update File: ") :].strip()
                i += 1
                # 检查可选的 Move to
                move_to = None
                if i < len(lines) and _norm(lines[i]).startswith("*** Move to: "):
                    move_to = _norm(lines[i])[len("*** Move to: ") :].strip()
                    i += 1
                # 跳过该文件的所有 patch 行（直到下一个操作或结束）
                while i < len(lines) and not _norm(lines[i]).startswith(
                    (
                        "*** End Patch",
                        "*** Update File:",
                        "*** Delete File:",
                        "*** Add File:",
                    )
                ):
                    i += 1
                action = PatchAction(
                    type=ActionType.UPDATE, path=path, move_path=move_to
                )
                edits.append(Edit(path=path, edit_type="patch", action=action))

            elif norm_line.startswith("*** Add File: "):
                path = norm_line[len("*** Add File: ") :].strip()
                i += 1
                add_lines: List[str] = []
                while i < len(lines) and not _norm(lines[i]).startswith(
                    (
                        "*** End Patch",
                        "*** Update File:",
                        "*** Delete File:",
                        "*** Add File:",
                    )
                ):
                    line = lines[i]
                    if line.startswith("+"):
                        add_lines.append(line[1:])
                    elif _norm(line).strip() == "":
                        add_lines.append("")
                    i += 1
                action = PatchAction(
                    type=ActionType.ADD,
                    path=path,
                    new_content="\n".join(add_lines),
                )
                edits.append(Edit(path=path, edit_type="patch", action=action))

            elif norm_line.startswith("*** Delete File: "):
                path = norm_line[len("*** Delete File: ") :].strip()
                i += 1
                action = PatchAction(type=ActionType.DELETE, path=path)
                edits.append(Edit(path=path, edit_type="patch", action=action))

            else:
                i += 1

        return edits
