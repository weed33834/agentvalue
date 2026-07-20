"""结构化日志配置。

P2-N2:统一应用层日志格式,加上 trace_id / tenant_id / timestamp 等结构化字段,
便于日志聚合系统(Loki / ELK / CloudWatch)查询与关联追踪。

设计要点:
- 默认输出到 stdout(K8s/容器环境标准做法)。
- JSON 格式可选,通过 setup_logging(json_logs=True) 开启;默认仍是人类可读的
  彩色格式,本地开发与 CI 日志易读。
- 不强制依赖第三方库(structlog / python-json-logger),用标准 logging.Formatter
  自定义格式,保持依赖最小化。如需更复杂的结构化输出,后续可平滑切换到 structlog。
- 通过环境变量 LOG_LEVEL / LOG_FORMAT=json 控制,无需改代码。
- 不重复 alembic 的 fileConfig 路径,避免 disable_existing_loggers 污染测试。

使用方式:
    # main.py 顶部
    from core.logging_config import setup_logging
    setup_logging()  # 在创建 FastAPI app 之前调用

    # 其他模块继续用标准 logging.getLogger(__name__),无需改动
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional


# 默认日志格式:时间 + level + logger + 消息
# 比 logging.BASIC_FORMAT 多了 timestamp / process / thread,便于多进程调试
DEFAULT_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


class StructuredJsonFormatter(logging.Formatter):
    """单行 JSON 日志格式器,适配 Loki / ELK / CloudWatch 等聚合系统。

    输出示例:
        {"ts":"2026-07-05T08:00:00Z","level":"INFO","logger":"api.routes",
         "msg":"evaluation created","trace_id":"abc","tenant_id":"default"}
    """

    # 标准 levelname -> 优先级(可选,供 ELK /promtail 使用)
    _LEVEL_PRIORITY = {
        "DEBUG": 10,
        "INFO": 20,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }

    def format(self, record: logging.LogRecord) -> str:
        # 时间戳统一 ISO8601 + UTC,便于跨时区对齐
        ts = (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pid": record.process,
            "thread": record.threadName,
        }

        # 关联字段:从 record.__dict__ 提取 extra 注入的字段
        # 约定:extra 中以 _log_ 开头的字段会平铺到顶层,其余进 fields
        extra_keys = {
            k: v
            for k, v in record.__dict__.items()
            if k
            not in logging.LogRecord(
                name=record.name,
                level=record.levelno,
                pathname=record.pathname,
                lineno=record.lineno,
                msg=record.msg,
                args=record.args,
                exc_info=record.exc_info,
            ).__dict__
            and not k.startswith("_")
        }
        # 常见结构化字段(trace_id / tenant_id / user_id / request_id)平铺
        flat_fields = {
            "trace_id",
            "tenant_id",
            "user_id",
            "request_id",
            "evaluation_id",
            "employee_id",
        }
        for k in flat_fields:
            if k in extra_keys:
                payload[k] = extra_keys.pop(k)
        if extra_keys:
            payload["fields"] = extra_keys

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class _StdoutFilter(logging.Filter):
    """只允许 INFO 及以下进 stdout,WARNING+ 进 stderr。

    容器环境按 stream 采集,错误日志单独走 stderr 便于告警/告警分级。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


class TraceContextFilter(logging.Filter):
    """从 contextvar 注入 trace_id / tenant_id 到 LogRecord。

    P0 修复: tracing.py 已 set _current_trace_id contextvar,但原 StructuredJsonFormatter
    只从 record.__dict__ 读 extra 注入字段,contextvar 永远进不了日志。

    参考 Langfuse/Loki 标准做法: 在 root logger 挂 Filter,filter() 时读 contextvar
    写到 record,所有 logger(含第三方库)自动带 trace_id,无需业务代码改 extra。

    tenant_id 同理从 core.tenant_context contextvar 取,统一注入。
    若 record 已有同名属性(extra 注入),保留 extra 注入值不覆盖。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", None):
            try:
                from core.tracing import tracer

                tid = tracer.current_trace_id()
                if tid:
                    record.trace_id = tid
            except Exception:
                pass
        if not getattr(record, "tenant_id", None):
            try:
                from core.tenant_context import get_current_tenant

                record.tenant_id = get_current_tenant()
            except Exception:
                pass
        return True


def setup_logging(
    level: Optional[str] = None,
    json_logs: Optional[bool] = None,
    fmt: Optional[str] = None,
) -> None:
    """初始化全局日志配置。

    在 main.py 创建 FastAPI app 之前调用一次即可。

    Args:
        level: 日志级别(如 "INFO" / "DEBUG"),None 时从环境变量 LOG_LEVEL 读取,
            默认 INFO。
        json_logs: 是否输出 JSON 格式。None 时从环境变量 LOG_FORMAT 读取,
            "json" 开启,其他值或未设置则使用人类可读格式。
        fmt: 自定义 Formatter 模板,仅对非 JSON 模式生效。

    注意:
        - 多次调用安全(幂等):每次都会清除现有 handler 后重建。
        - 不调用 logging.config.fileConfig,避免 disable_existing_loggers=True
          污染已注册的 logger(测试 caplog 也依赖此约定)。
        - 不修改第三方库(logger 名 'urllib3' / 'chromadb' 等)的日志级别,
          如需静默可在调用后再 logger.setLevel('WARNING')。
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    if json_logs is None:
        json_logs = os.getenv("LOG_FORMAT", "").lower() == "json"
    if fmt is None:
        fmt = DEFAULT_FMT

    root = logging.getLogger()
    # 清理已有 handler,避免重复挂载(尤其在被多次调用时)
    for h in list(root.handlers):
        root.removeHandler(h)

    # stdout: INFO 以下
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(_StdoutFilter())

    # stderr: WARNING 及以上
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(logging.WARNING)

    # P0 修复: 挂 TraceContextFilter 到 root,让所有 logger 自动从 contextvar
    # 读 trace_id / tenant_id 写入 LogRecord,Formatter 再平铺到日志顶层。
    trace_filter = TraceContextFilter()

    if json_logs:
        formatter: logging.Formatter = StructuredJsonFormatter()
    else:
        formatter = logging.Formatter(fmt=fmt, datefmt=DEFAULT_DATEFMT)

    for h in (stdout_handler, stderr_handler):
        h.setFormatter(formatter)
        h.addFilter(trace_filter)
        root.addHandler(h)

    root.setLevel(level)

    # 让 uvicorn 的 access log 走统一 formatter(可选,不强改其 logger 结构)
    # 若 uvicorn 自挂 handler,会重复输出;这里设置 propagate 即可。
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True
