"""
tool_calls 流式 delta 聚合器

对标 OpenAI 流式 tool_calls delta 拼接:
https://theneuralbase.com/function-calling/learn/intermediate/assembling-arguments-from-stream/

OpenAI 流式响应中 delta.tool_calls 是稀疏的:
- 首个 chunk 携带 id + function.name
- 后续 chunk 通过 function.arguments 字符串逐片补全
- 流结束后才 json.loads 得到完整参数

聚合器职责:
- 按 index 累加 arguments 字符串
- 流结束后统一 json.loads
- 支持 30s 超时熔断
"""

import json
import logging
from typing import Any, Dict, List, Optional

from core.providers.base import StreamChunk, ToolCallDelta

logger = logging.getLogger(__name__)


class ToolCallAggregator:
    """tool_calls 流式 delta 聚合器

    用法:
        aggregator = ToolCallAggregator()
        async for chunk in provider.stream_chat_completion(...):
            aggregator.feed(chunk)
            # 前端实时显示 args 增量...
        final_calls = aggregator.finalize()  # json.loads 得完整参数
    """

    def __init__(self):
        # {index: {"name": str, "id": str, "arguments": str}}
        self._buffers: Dict[int, Dict[str, str]] = {}

    def feed(self, chunk: StreamChunk) -> List[ToolCallDelta]:
        """喂入一个 chunk,返回本 chunk 携带的 delta 列表(供前端实时显示)"""
        if not chunk.tool_calls:
            return []
        for tc in chunk.tool_calls:
            buf = self._buffers.setdefault(
                tc.index, {"name": "", "id": "", "arguments": ""}
            )
            if tc.id:
                buf["id"] = tc.id
            if tc.name:
                buf["name"] = tc.name
            if tc.arguments:
                buf["arguments"] += tc.arguments
        return chunk.tool_calls

    def finalize(self) -> List[Dict[str, Any]]:
        """流结束后调用,返回完整的 tool_calls 列表(参数已 json.loads)。

        格式对标 ChatCompletion.tool_calls:
        [{"id": str, "name": str, "arguments": dict}]
        """
        result: List[Dict[str, Any]] = []
        for idx in sorted(self._buffers):
            buf = self._buffers[idx]
            try:
                args = json.loads(buf["arguments"]) if buf["arguments"] else {}
            except json.JSONDecodeError as e:
                logger.warning(
                    "tool_call arguments JSON 解析失败 index=%d args_len=%d err=%s",
                    idx,
                    len(buf["arguments"]),
                    e,
                )
                # 解析失败时保留原始字符串,不阻断流程
                args = {"_raw": buf["arguments"], "_parse_error": str(e)}
            result.append(
                {
                    "id": buf["id"],
                    "name": buf["name"],
                    "arguments": args,
                }
            )
        return result

    def get_accumulated_args(self, index: int) -> str:
        """获取某 index 当前累加的 arguments 字符串(供前端实时显示)"""
        return self._buffers.get(index, {}).get("arguments", "")
