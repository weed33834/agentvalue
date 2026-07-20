"""Python 代码执行沙箱 (Code Interpreter)

参考:
- OpenAI Code Interpreter: https://platform.openai.com/docs/assistants/tools/code-interpreter
- LangChain Python REPL: https://python.langchain.com/docs/integrations/tools/python
- Jupyter Kernel: 隔离进程 + 受限内建

设计原则:
1. 使用 subprocess 在隔离子进程中执行用户代码,主进程不受影响
2. 限制可用模块白名单: math, json, statistics, datetime, re, collections, itertools, functools
3. 禁止危险内建与导入: os, sys, subprocess, open, exec, eval, __import__, importlib
4. 超时保护(默认 10s),超时杀子进程避免死循环卡住 Agent
5. 捕获 stdout / stderr / result(最后一个表达式或显式 return 的值),返回结构化结果

返回格式:
    {
        "success": bool,
        "stdout": str,
        "stderr": str,
        "result": Any,
        "error": Optional[str],
    }

对标 Dify/Coze 的 Code 节点:
- Dify: Code 节点 (Python/JS) 限制 10s 超时 + 沙箱
- Coze: Code 插件 (Python) 沙箱执行 + 输出捕获
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 允许导入的模块白名单(对标 OpenAI Code Interpreter 受限环境)
ALLOWED_MODULES: List[str] = [
    "math",
    "json",
    "statistics",
    "datetime",
    "re",
    "collections",
    "itertools",
    "functools",
]

# 禁止的内建函数与名称(危险操作)。
# 注意: getattr/setattr/delattr/hasattr 不在此列 —— Python import 机制
# (importlib._bootstrap) 内部依赖 getattr, 删除会导致所有 import 失败。
# 安全性由 __import__ 白名单 + 危险模块/函数屏蔽共同保证:
# 即使拿到 builtins 模块对象, open/exec/eval 等属性已被删除。
FORBIDDEN_NAMES: List[str] = [
    "os",
    "sys",
    "subprocess",
    "open",
    "exec",
    "eval",
    "__import__",
    "importlib",
    "globals",
    "locals",
    "vars",
    "compile",
    "builtins",
    "exit",
    "quit",
    "input",
]

# 隔离子进程执行的 Python 引导脚本(固定部分, 不含 format 占位符)。
# 思路:
#   1. 先 pre-import 白名单模块(此时 __import__ 未被替换, 内部 C 依赖如 _io/_statistics
#      可正常解析), 并缓存到 _ALLOWED_CACHE;
#   2. 替换 __import__ 为 _safe_import: 仅返回缓存中的白名单模块, 拒绝其他所有 import;
#   3. 屏蔽危险内建(open/exec/eval/os/sys/...);
#   4. exec 用户代码, 捕获 stdout/stderr/result。
# 关键: 先捕获 exec 引用, 否则删除 builtins.exec 后引导脚本无法调用。
# 注意: 此字符串中不含任何 { } 或 % 格式化占位符, 直接作为 Python 源码拼接。
_RUNNER_BODY = '''import sys
import json as _json
import io as _io
import traceback as _tb

# ---- 捕获 exec 引用(后续会从 builtins 中删除 exec, 但引导脚本仍需调用) ----
_exec = exec

# ---- 预导入白名单模块(在 __import__ 被替换前, 内部依赖可正常解析) ----
import math
import json
import statistics
import datetime
import re
import collections
import itertools
import functools

_ALLOWED_CACHE = {
    "math": math,
    "json": json,
    "statistics": statistics,
    "datetime": datetime,
    "re": re,
    "collections": collections,
    "itertools": itertools,
    "functools": functools,
}

# ---- 替换 __import__ 为白名单版本 ----
# 直接返回预导入的缓存模块, 不再调用 _real_import, 避免触发内部 C 依赖的 import。
# fromlist 非空时(Python 的 from X import Y 语义)仍返回模块本身,
# Python 会自行用 getattr 从返回的模块上取 Y。
def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if top in _ALLOWED_CACHE:
        return _ALLOWED_CACHE[top]
    raise ImportError(
        "Module '" + name + "' is not allowed in sandbox. Allowed: "
        + ", ".join(sorted(_ALLOWED_CACHE.keys()))
    )

import builtins as _builtins
_builtins.__import__ = _safe_import

# ---- 移除/屏蔽危险内建 ----
# 注意: __import__ 已替换为 _safe_import, 跳过避免覆盖;
#       exec 用 _exec 引用调用, 删除 builtins.exec 不影响引导脚本。
for _n in _FORBIDDEN_NAMES:
    if _n == "__import__":
        # 已替换为安全版本, 保留
        continue
    if hasattr(_builtins, _n):
        try:
            delattr(_builtins, _n)
        except (AttributeError, TypeError):
            # 部分内建不可删除, 用占位函数替换
            def _blocked(*a, _name=_n, **kw):
                raise NameError(
                    "Name '" + _name + "' is blocked in sandbox"
                )
            setattr(_builtins, _n, _blocked)

# ---- 捕获 stdout / stderr ----
_stdout_buf = _io.StringIO()
_stderr_buf = _io.StringIO()
_old_stdout = sys.stdout
_old_stderr = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf

_result = None
_error = None
try:
    _user_ns = {"__name__": "__main__"}
    # 用捕获的 _exec 执行用户代码(此时 builtins.exec 已被屏蔽, 但 _exec 引用仍可用)
    _exec(_USER_CODE, _user_ns)
    # 若用户显式设置了 __result__, 作为结果返回
    _result = _user_ns.get("__result__", None)
except Exception as _e:
    _error = "".join(_tb.format_exception(type(_e), _e, _e.__traceback__))
finally:
    sys.stdout = _old_stdout
    sys.stderr = _old_stderr

# ---- 输出 JSON 结果给主进程 ----
_out = {
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue(),
    "result": _result,
    "error": _error,
}
sys.stdout.write("__SANDBOX_RESULT_START__")
sys.stdout.write(_json.dumps(_out, ensure_ascii=False, default=str))
sys.stdout.write("__SANDBOX_RESULT_END__")
'''


class CodeInterpreter:
    """Python 代码执行沙箱

    在隔离子进程中执行用户提供的 Python 代码, 限制可用模块与危险内建,
    捕获 stdout / stderr 与最后一个 __result__ 值。

    用法:
        interpreter = CodeInterpreter()
        result = await interpreter.execute("import math; print(math.sqrt(16))")
        # result = {"success": True, "stdout": "4.0\\n", "stderr": "", "result": None, "error": None}

    安全保障:
        1. 子进程隔离: 用户代码崩溃不影响主进程
        2. 模块白名单: 仅允许 math/json/statistics/datetime/re/collections/itertools/functools
        3. 危险内建屏蔽: os/sys/subprocess/open/exec/eval/__import__/importlib 等被替换为 blocked 函数
        4. 超时保护: 默认 10s, 超时杀子进程
        5. 无文件系统访问: open 已被屏蔽
    """

    def __init__(self, default_timeout: int = 10):
        """初始化 CodeInterpreter.

        Args:
            default_timeout: 默认执行超时(秒), 默认 10
        """
        self.default_timeout = default_timeout

    async def execute(self, code: str, timeout: int = 10) -> Dict[str, Any]:
        """在隔离沙箱中执行 Python 代码.

        Args:
            code: 要执行的 Python 代码字符串
            timeout: 执行超时(秒), 默认 10

        Returns:
            执行结果字典:
                - success (bool): 是否执行成功(无异常)
                - stdout (str): 标准输出内容
                - stderr (str): 标准错误内容
                - result (Any): 用户代码中 __result__ 变量的值, 无则为 None
                - error (Optional[str]): 异常堆栈, 无异常则为 None
        """
        if not isinstance(code, str) or not code.strip():
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "result": None,
                "error": "代码不能为空",
            }

        # 限制超时范围, 避免恶意大值
        if timeout is None or timeout <= 0:
            timeout = self.default_timeout
        # 上限 60s, 防止 Agent 调用传入超大值卡死
        timeout = min(int(timeout), 60)

        # 组装最终脚本:
        # 1. 用 repr() 安全注入用户代码与白名单(避免注入攻击)
        # 2. 拼接固定引导脚本 _RUNNER_BODY
        full_script = (
            f"_USER_CODE = {code!r}\n"
            f"_ALLOWED_MODULES = {ALLOWED_MODULES!r}\n"
            f"_FORBIDDEN_NAMES = {FORBIDDEN_NAMES!r}\n"
            f"{_RUNNER_BODY}"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                _sys_executable(),
                "-c",
                full_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 子进程不继承 stdin, 避免交互式阻塞
                stdin=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("CodeInterpreter 启动子进程失败: %s", e)
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "result": None,
                "error": f"启动沙箱子进程失败: {e}",
            }

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # 超时杀进程
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "result": None,
                "error": f"代码执行超时 ({timeout}s)",
            }
        except Exception as e:
            logger.warning("CodeInterpreter 执行异常: %s", e)
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "result": None,
                "error": f"执行异常: {e}",
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # 从 stdout 中提取沙箱结构化结果
        payload: Optional[Dict[str, Any]] = None
        marker_start = "__SANDBOX_RESULT_START__"
        marker_end = "__SANDBOX_RESULT_END__"
        if marker_start in stdout and marker_end in stdout:
            s_idx = stdout.index(marker_start) + len(marker_start)
            e_idx = stdout.index(marker_end, s_idx)
            raw_json = stdout[s_idx:e_idx]
            # 清理 stdout: 移除 marker 区块, 保留用户实际 print 的内容
            stdout = (
                stdout[: stdout.index(marker_start)]
                + stdout[e_idx + len(marker_end):]
            )
            try:
                import json as _json

                payload = _json.loads(raw_json)
            except Exception as e:
                logger.debug("解析沙箱结果 JSON 失败: %s", e)
                payload = None

        if payload is None:
            # 子进程崩溃或引导脚本异常, 直接返回原始 stdout/stderr
            return {
                "success": False,
                "stdout": stdout,
                "stderr": stderr,
                "result": None,
                "error": stderr.strip() or "沙箱执行失败(未返回结构化结果)",
            }

        success = not payload.get("error")
        return {
            "success": success,
            "stdout": payload.get("stdout", "") or stdout,
            "stderr": payload.get("stderr", "") or stderr,
            "result": payload.get("result"),
            "error": payload.get("error"),
        }


def _sys_executable() -> str:
    """获取当前 Python 解释器路径(延迟 import sys 避免模块级污染)."""
    import sys

    return sys.executable or "python3"
