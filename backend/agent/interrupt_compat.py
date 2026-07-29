"""LangGraph interrupt() Python 3.10 兼容层

LangGraph 1.2+ 的 interrupt() 内部调用 get_config()，后者依赖
var_child_runnable_config contextvar。在 Python 3.11+ 中，LangGraph
通过 asyncio.create_task(context=...) 自动设置该 contextvar；但
Python 3.10 的 asyncio.create_task 不支持 context 参数，导致
contextvar 从未被设置，interrupt() 抛出
'Called get_config outside of a runnable context'。

本模块提供 ensure_interrupt_context / reset_interrupt_context 函数，
在 Python < 3.11 上手动设置 contextvar 作为兼容层，
Python 3.11+ 上为空操作（LangGraph 已自动处理）。

使用方式:
    from agent.interrupt_compat import ensure_interrupt_context, reset_interrupt_context

    _token = ensure_interrupt_context(config)
    try:
        interrupt_info = interrupt({...})
    finally:
        reset_interrupt_context(_token)
"""

from __future__ import annotations

import sys
from typing import Optional

from langchain_core.runnables.config import RunnableConfig, var_child_runnable_config


def ensure_interrupt_context(config: Optional[RunnableConfig]):
    """Python 3.10 兼容层：手动设置 contextvar 使 interrupt() 可用。

    在 Python < 3.11 上设置 var_child_runnable_config contextvar。
    Python 3.11+ 上返回 None（LangGraph 已自动处理）。
    """
    if sys.version_info < (3, 11) and config is not None:
        return var_child_runnable_config.set(config)
    return None


def reset_interrupt_context(token):
    """恢复 contextvar（与 ensure_interrupt_context 配对）。"""
    if token is not None:
        var_child_runnable_config.reset(token)
