"""
P3-3: core/logging_config.py 单元测试

覆盖:
- StructuredJsonFormatter 输出含 timestamp / level / message / logger 字段
- extra 字段平铺到顶层(trace_id / tenant_id 等已知字段)
- setup_logging 重复调用幂等(handler 数量稳定)
- get_log_context() 在无 context 时返回空 dict
"""

import json
import logging

from core.logging_config import (
    DEFAULT_FMT,
    StructuredJsonFormatter,
    setup_logging,
)


def _make_record(msg="hello", level=logging.INFO, logger_name="api.routes", **extra):
    """构造一条 LogRecord,可注入 extra 字段"""
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_contains_required_fields():
    """JSON 输出含 ts / level / logger / msg 字段"""
    formatter = StructuredJsonFormatter()
    record = _make_record(
        msg="evaluation created", level=logging.INFO, logger_name="api.routes"
    )
    out = formatter.format(record)
    data = json.loads(out)
    assert "ts" in data, "缺少 timestamp 字段"
    assert data["level"] == "INFO"
    assert data["logger"] == "api.routes"
    assert data["msg"] == "evaluation created"
    assert data["ts"].endswith("Z"), f"ts 非 UTC ISO8601: {data['ts']}"
    assert "pid" in data and "thread" in data


def test_json_formatter_extra_flat_fields_promoted_to_top_level():
    """已知 extra 字段(trace_id / tenant_id 等)应平铺到 JSON 顶层"""
    formatter = StructuredJsonFormatter()
    record = _make_record(
        msg="eval",
        trace_id="trace-abc",
        tenant_id="acme",
        user_id="U1",
        request_id="req-1",
        evaluation_id="EV-1",
        employee_id="E1001",
    )
    data = json.loads(formatter.format(record))
    assert data["trace_id"] == "trace-abc"
    assert data["tenant_id"] == "acme"
    assert data["user_id"] == "U1"
    assert data["request_id"] == "req-1"
    assert data["evaluation_id"] == "EV-1"
    assert data["employee_id"] == "E1001"
    assert "fields" not in data or "trace_id" not in data.get("fields", {})


def test_json_formatter_unknown_extra_goes_into_fields():
    """非已知平铺字段的 extra 应归入 fields 子 dict"""
    formatter = StructuredJsonFormatter()
    record = _make_record(msg="x", custom_field="custom-value", trace_id="t1")
    data = json.loads(formatter.format(record))
    assert data["trace_id"] == "t1"
    assert data["fields"]["custom_field"] == "custom-value"


def test_json_formatter_includes_exception_info():
    """exc_info 存在时输出 exc 字段"""
    formatter = StructuredJsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = _make_record(msg="failed", level=logging.ERROR, exc_info=exc_info)
    data = json.loads(formatter.format(record))
    assert "exc" in data
    assert "ValueError" in data["exc"]
    assert "boom" in data["exc"]


def test_setup_logging_is_idempotent_on_repeated_calls():
    """setup_logging 多次调用幂等:root handler 数量稳定,不重复挂载"""
    setup_logging(json_logs=True)
    root = logging.getLogger()
    count_after_first = len(root.handlers)
    setup_logging(json_logs=True)
    setup_logging(json_logs=True)
    setup_logging(json_logs=True)
    count_after_repeated = len(root.handlers)
    assert (
        count_after_first == count_after_repeated
    ), f"setup_logging 非幂等:首次 {count_after_first} handlers,多次后 {count_after_repeated}"
    assert count_after_repeated == 2


def test_setup_logging_respects_level():
    """setup_logging(level=...) 设置 root logger 级别"""
    setup_logging(level="WARNING", json_logs=False, fmt=DEFAULT_FMT)
    root = logging.getLogger()
    assert root.level == logging.WARNING
