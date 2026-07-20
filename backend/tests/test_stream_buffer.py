"""
ToolCallAggregator 单元测试 (P4 测试补全)

覆盖 core/providers/stream_buffer.py 的 ToolCallAggregator:
- 空 aggregator finalize
- 单 tool_call delta 拼接
- 并行 tool_call 按 index 分组
- get_accumulated_args 实时查询
- JSON 解析失败容错
"""

import pytest

from core.providers.base import StreamChunk, ToolCallDelta
from core.providers.stream_buffer import ToolCallAggregator


# ============================================================
# Helpers
# ============================================================


def _tc_chunk(index, name=None, id=None, arguments=None):
    """构造一个只携带单个 tool_call delta 的 StreamChunk"""
    return StreamChunk(
        tool_calls=[ToolCallDelta(index=index, name=name, id=id, arguments=arguments)]
    )


# ============================================================
# 测试
# ============================================================


def test_empty_finalize():
    """空 aggregator finalize 返回空列表"""
    agg = ToolCallAggregator()
    assert agg.finalize() == []


def test_feed_chunk_without_tool_calls():
    """无 tool_calls 的 chunk feed 后返回空列表"""
    agg = ToolCallAggregator()
    chunk = StreamChunk(content="hello")
    assert agg.feed(chunk) == []
    assert agg.finalize() == []


def test_single_tool_call_full_flow():
    """单个 tool_call:起始 chunk + 多个 arguments delta → finalize"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="get_weather", id="call_1", arguments=""))
    agg.feed(_tc_chunk(0, arguments='{"city":"'))
    agg.feed(_tc_chunk(0, arguments="Bei"))
    agg.feed(_tc_chunk(0, arguments='jing"}'))
    result = agg.finalize()
    assert len(result) == 1
    assert result[0]["name"] == "get_weather"
    assert result[0]["id"] == "call_1"
    assert result[0]["arguments"] == {"city": "Beijing"}


def test_feed_returns_deltas():
    """feed 返回本 chunk 携带的 delta 列表"""
    agg = ToolCallAggregator()
    deltas = agg.feed(_tc_chunk(0, name="get_weather", id="call_1"))
    assert len(deltas) == 1
    assert deltas[0].name == "get_weather"
    assert deltas[0].id == "call_1"


def test_parallel_tool_calls_interleaved():
    """并行 tool_call (index 0 和 1 交错 feed) 能正确按 index 分组"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="get_weather", id="call_1", arguments=""))
    agg.feed(_tc_chunk(1, name="get_time", id="call_2", arguments=""))
    agg.feed(_tc_chunk(0, arguments='{"city":"'))
    agg.feed(_tc_chunk(1, arguments='{"tz":"'))
    agg.feed(_tc_chunk(0, arguments='NYC"}'))
    agg.feed(_tc_chunk(1, arguments='UTC"}'))
    result = agg.finalize()
    assert len(result) == 2
    assert result[0]["id"] == "call_1"
    assert result[0]["name"] == "get_weather"
    assert result[0]["arguments"] == {"city": "NYC"}
    assert result[1]["id"] == "call_2"
    assert result[1]["name"] == "get_time"
    assert result[1]["arguments"] == {"tz": "UTC"}


def test_get_accumulated_args_mid_stream():
    """get_accumulated_args 返回当前累积的 JSON 字符串"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="get_weather", id="call_1", arguments='{"city":"'))
    agg.feed(_tc_chunk(0, arguments="Bei"))
    assert agg.get_accumulated_args(0) == '{"city":"Bei'


def test_get_accumulated_args_unknown_index():
    """未知 index 返回空字符串"""
    agg = ToolCallAggregator()
    assert agg.get_accumulated_args(99) == ""


def test_get_accumulated_args_empty():
    """已 feed 但无 arguments 的 index 返回空字符串"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="get_weather", id="call_1"))
    assert agg.get_accumulated_args(0) == ""


def test_finalize_invalid_json():
    """arguments 非合法 JSON 时,finalize 返回 _raw + _parse_error"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="bad_call", id="call_1", arguments="not-json{"))
    result = agg.finalize()
    assert len(result) == 1
    assert result[0]["arguments"]["_raw"] == "not-json{"
    assert "_parse_error" in result[0]["arguments"]


def test_finalize_empty_arguments():
    """arguments 为空时,finalize 返回空 dict"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="no_args", id="call_1", arguments=""))
    result = agg.finalize()
    assert result[0]["arguments"] == {}


def test_finalize_sorted_by_index():
    """finalize 结果按 index 升序"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(2, name="c", id="3"))
    agg.feed(_tc_chunk(0, name="a", id="1"))
    agg.feed(_tc_chunk(1, name="b", id="2"))
    result = agg.finalize()
    assert [r["name"] for r in result] == ["a", "b", "c"]


def test_feed_name_only_in_first_chunk():
    """name/id 仅首个 chunk 携带,后续 chunk 不覆盖"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="get_weather", id="call_1", arguments='{"city":"'))
    # 后续 chunk 不带 name/id,只有 arguments
    agg.feed(_tc_chunk(0, arguments='NYC"}'))
    result = agg.finalize()
    assert result[0]["name"] == "get_weather"
    assert result[0]["id"] == "call_1"
    assert result[0]["arguments"] == {"city": "NYC"}


def test_multiple_chunks_same_index_accumulate():
    """多个 arguments delta 顺序累加"""
    agg = ToolCallAggregator()
    agg.feed(_tc_chunk(0, name="fn", id="c1", arguments="{"))
    agg.feed(_tc_chunk(0, arguments='"a":1,'))
    agg.feed(_tc_chunk(0, arguments='"b":2}'))
    result = agg.finalize()
    assert result[0]["arguments"] == {"a": 1, "b": 2}
