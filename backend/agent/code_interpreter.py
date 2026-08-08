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
6. WS-4 加固: resource 资源限制(AS/CPU/NOFILE/FSIZE/NPROC) + 独立进程组
   (超时 killpg 连子孙进程一起杀) + 每次执行独立临时工作目录

返回格式:
    {
        "success": bool,
        "stdout": str,
        "stderr": str,
        "result": Any,
        "error": Optional[str],
    }

WS-4 安全边界说明（诚实声明）
-----------------------------
本沙箱**能**做到:
- 限制子进程的 CPU 时间 / 地址空间 / 打开文件数 / 单文件写入上限 / 进程数,
  拦截 [0]*10**10 这类内存炸弹、死循环、写爆磁盘、fork bomb;
- 超时后连子进程 fork 出的孙进程一起杀掉,不留孤儿进程;
- 白名单 import + 危险内建屏蔽,拦截 os/subprocess/open/exec/eval 等。

本沙箱**不能**做到（它不是容器）:
- 没有网络命名空间隔离: 沙箱进程能访问宿主网络(若攻击者绕过内建屏蔽拿到 socket);
- 没有文件系统隔离: rlimit 只限大小,不限路径(能读到的路径仍能读);
- 没有内核级隔离: 与主进程共享内核,``preexec_fn`` 里的 setrlimit 不是 seccomp;
- RLIMIT_NPROC 按**真实 UID** 计数: 宿主同用户进程多时可能误伤,生产建议单独
  跑沙箱用户或调大 ``sandbox_max_processes``。

结论: 这是**纵深防御的一层**,不是安全边界。运行不可信代码的严格隔离仍应
使用容器(gVisor/Firecracker)或独立沙箱服务; 本实现面向「低风险代码片段 +
缓解常见滥用」的场景。

对标 Dify/Coze 的 Code 节点:
- Dify: Code 节点 (Python/JS) 限制 10s 超时 + 沙箱
- Coze: Code 插件 (Python) 沙箱执行 + 输出捕获
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

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
_RUNNER_BODY = """import sys
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
"""


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
        4. 超时保护: 默认 10s, 超时杀整个进程组(SIGKILL, 连子孙进程一起杀)
        5. 资源限制 (WS-4, POSIX only): RLIMIT_AS/CPU/NOFILE/FSIZE/NPROC,
           拦截内存炸弹 / 死循环 / 写爆磁盘 / fork bomb
        6. 无文件系统访问: open 已被屏蔽; 每次执行使用独立临时工作目录(用完即删)

    限制 (WS-4 诚实声明): 无网络命名空间 / 无文件系统路径隔离 / 非容器,
    严格隔离不可信代码请使用容器或独立沙箱服务。详见模块顶部 docstring。
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

        # 每次执行创建独立临时工作目录: 隔离 cwd + 事后统一清理
        # (沙箱内 open 已被屏蔽, 目录主要防止并发执行间工作目录互相污染,
        #  并为未来放开受限文件读写预留干净挂载点)
        workdir = tempfile.mkdtemp(prefix="agentvalue_sandbox_")
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    _sys_executable(),
                    "-c",
                    full_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # 子进程不继承 stdin, 避免交互式阻塞
                    stdin=asyncio.subprocess.DEVNULL,
                    # WS-4: 独立工作目录 + POSIX 资源限制/独立进程组
                    cwd=workdir,
                    preexec_fn=_build_preexec_fn(),
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
                # 超时杀进程: 杀整个进程组, 连子进程 fork 出的孙进程一起清理,
                # 避免遗留孤儿进程继续消耗 CPU/内存(旧实现只 kill 直接子进程)
                _terminate_process_group(proc)
                try:
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
                    + stdout[e_idx + len(marker_end) :]
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
        finally:
            # 每次执行独立临时目录, 用完即删(含超时/异常路径)
            shutil.rmtree(workdir, ignore_errors=True)


def _sys_executable() -> str:
    """获取当前 Python 解释器路径(延迟 import sys 避免模块级污染)."""
    import sys

    return sys.executable or "python3"


# ===========================================================================
# WS-4 沙箱加固: 资源限制 + 进程组隔离
# ===========================================================================


def _build_rlimits(settings=None) -> Dict[int, Tuple[int, int]]:
    """从配置组装 RLIMIT 字典: {resource -> (soft, hard)}。

    独立成纯函数便于单测直接断言（不依赖真实 fork 子进程）。
    配置项见 core/config.py ``sandbox_*`` 系列; ``sandbox_rlimit_enabled=False``
    时返回空字典（完全关闭资源限制）。

    返回值（均为 hard=soft, 子进程无法自行调高）:
    - RLIMIT_AS:     地址空间上限(字节), 拦截 [0]*10**10 内存炸弹
    - RLIMIT_CPU:    CPU 时间上限(秒), 与 wall-clock 超时互补
    - RLIMIT_NOFILE: 打开文件数上限
    - RLIMIT_FSIZE:  单文件写入上限(字节), 拦截磁盘写爆
    - RLIMIT_NPROC:  进程数上限, 拦截 fork bomb
    """
    from core.config import get_settings

    s = settings or get_settings()
    if not getattr(s, "sandbox_rlimit_enabled", True):
        return {}
    import resource

    memory_bytes = max(int(getattr(s, "sandbox_max_memory_mb", 512)), 1) * 1024 * 1024
    cpu_seconds = max(int(getattr(s, "sandbox_max_cpu_seconds", 10)), 1)
    open_files = max(int(getattr(s, "sandbox_max_open_files", 64)), 1)
    file_bytes = max(int(getattr(s, "sandbox_max_file_size_mb", 16)), 1) * 1024 * 1024
    processes = max(int(getattr(s, "sandbox_max_processes", 32)), 1)

    limits: Dict[int, Tuple[int, int]] = {
        resource.RLIMIT_AS: (memory_bytes, memory_bytes),
        resource.RLIMIT_CPU: (cpu_seconds, cpu_seconds),
        resource.RLIMIT_NOFILE: (open_files, open_files),
        resource.RLIMIT_FSIZE: (file_bytes, file_bytes),
        resource.RLIMIT_NPROC: (processes, processes),
    }
    return limits


def _build_preexec_fn(settings=None) -> Optional[Callable[[], None]]:
    """构造子进程 ``preexec_fn``：exec 前设置资源限制 + 创建独立进程组。

    仅 POSIX 生效（``resource`` / ``os.setsid`` 依赖 POSIX）；Windows 平台
    返回 None 并打 WARNING 明确告知资源限制降级（超时仍会杀直接子进程）。

    返回 None 时 ``create_subprocess_exec(preexec_fn=None)`` 等同未加固。
    """
    if sys.platform == "win32":
        logger.warning(
            "CodeInterpreter 资源限制仅在 POSIX 生效, Windows 平台跳过 "
            "(rlimit/进程组隔离降级, 仅保留超时杀直接子进程)"
        )
        return None
    try:
        import resource  # noqa: F401 - 确保 resource 模块可用
    except ImportError:
        logger.warning("resource 模块不可用, CodeInterpreter 资源限制降级跳过")
        return None

    limits = _build_rlimits(settings)

    def _preexec() -> None:
        # 创建新会话 -> 新进程组, 使子进程成为组 leader(pgid == pid),
        # 超时后才能用 killpg 连子孙进程一起杀
        try:
            os.setsid()
        except OSError:  # pragma: no cover - 理论上不会发生(fork 后非组 leader)
            logger.warning("os.setsid 失败, 超时将只能杀直接子进程", exc_info=True)
        for rsrc, (soft, hard) in limits.items():
            try:
                resource.setrlimit(rsrc, (soft, hard))
            except (ValueError, OSError) as exc:  # pragma: no cover
                logger.warning("setrlimit(%s) 失败, 该项限制降级跳过: %s", rsrc, exc)

    return _preexec if limits else None


def _terminate_process_group(proc) -> None:
    """超时清理: 杀整个进程组(含子进程 fork 的孙进程), 避免孤儿进程。

    安全护栏: 只有当 ``pgid == proc.pid``（即 setsid 成功、子进程确实是组
    leader）才 killpg；否则退回只杀直接子进程——绝不 killpg 父进程组,
    防止误杀与沙箱无关的主进程。
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return  # 进程已退出
    if pgid == proc.pid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return  # 组已不存在
        except PermissionError:  # pragma: no cover
            logger.warning("killpg 权限不足, 退回杀直接子进程 pid=%s", proc.pid)
    try:
        proc.kill()
    except Exception:
        pass
