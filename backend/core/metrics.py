"""
Prometheus 指标收集。

业务代码只管调下面的埋点函数，label 拼装和命名集中在这里管，避免散落到各处拼写出错。
setup_metrics(app) 把 prometheus_client 的 ASGI 应用挂到 /metrics，不走鉴权，方便 Prometheus 直接抓。

指标统一以 agentvalue_ 为前缀。
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

logger = logging.getLogger(__name__)


# P0 修复: 默认放行 loopback + RFC1918 私网段,生产环境仅内网 Prometheus 可抓
_DEFAULT_ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
]


def _client_ip_from_scope(scope) -> str:
    """从 ASGI scope 取 client IP,优先用 X-Forwarded-For(反向代理场景)"""
    headers = dict(scope.get("headers", []) or [])
    xff = headers.get(b"x-forwarded-for")
    if xff:
        try:
            return xff.decode().split(",")[0].strip()
        except Exception:
            pass
    client = scope.get("client")
    return client[0] if client else "unknown"


def _ip_allowed(ip_str: str, allowed_networks) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in allowed_networks)
    except ValueError:
        return False


def _build_allowed_networks(settings) -> list:
    """构造允许访问 /metrics 的网络白名单。

    metrics_allowed_ips 配置时用配置,否则用默认(_DEFAULT_ALLOWED_NETWORKS)。
    """
    if not settings.metrics_allowed_ips:
        return list(_DEFAULT_ALLOWED_NETWORKS)
    networks = []
    for part in settings.metrics_allowed_ips.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                networks.append(ipaddress.ip_network(part, strict=False))
            else:
                networks.append(ipaddress.ip_network(f"{part}/32"))
        except ValueError:
            logger.warning("metrics_allowed_ips 配置项无效: %s", part)
    return networks or list(_DEFAULT_ALLOWED_NETWORKS)


def _make_authed_metrics_asgi(settings):
    """包装 prometheus ASGI app,加 IP / Bearer 鉴权层。

    P0 修复: 原 setup_metrics 直接 app.mount("/metrics", make_asgi_app()),
    任何能访问 API 的人都能抓走业务敏感指标(评估量、token 用量、健康度等)。

    鉴权模式(由 settings.metrics_auth_mode 控制):
    - "ip": 仅允许配置的 IP 白名单(默认 loopback + RFC1918),其余 403
    - "token": 校验 Authorization: Bearer <token>,不匹配 401
    - "none": 不鉴权(仅本地开发,生产环境部署脚本应禁止)

    参考: LiteLLM/Langfuse 生产实践均对 /metrics 做网络层或 token 隔离

    P1 修复: mode/allowed_networks/expected_token 在请求时按 settings 当前值
    重新读取(早期版本在 ASGI 构造期一次性捕获,导致测试 monkeypatch
    settings.metrics_auth_mode 后 /metrics 仍走旧鉴权模式)。生产环境 settings
    一次性加载,本变更对其无影响。
    """
    inner = make_asgi_app()

    async def authed_app(scope, receive, send):
        if scope.get("type") != "http":
            return await inner(scope, receive, send)

        # 请求时按当前 settings 重读 mode,使测试 monkeypatch 生效
        mode = (settings.metrics_auth_mode or "ip").lower()
        if mode == "none":
            return await inner(scope, receive, send)

        # token 模式: 校验 Authorization header
        if mode == "token":
            expected_token = settings.metrics_bearer_token
            if not expected_token:
                logger.error(
                    "metrics_auth_mode=token 但未配置 METRICS_BEARER_TOKEN,"
                    "拒绝所有 /metrics 请求"
                )
                await _send_json(send, 503, {"detail": "metrics auth misconfigured"})
                return
            headers = dict(scope.get("headers", []) or [])
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                await _send_json(send, 401, {"detail": "missing bearer token"})
                return
            token = auth[7:].strip()
            if token != expected_token:
                await _send_json(send, 403, {"detail": "invalid token"})
                return
            return await inner(scope, receive, send)

        # ip 模式(默认): 校验客户端 IP
        if mode == "ip":
            allowed_networks = _build_allowed_networks(settings)
            client_ip = _client_ip_from_scope(scope)
            if not _ip_allowed(client_ip, allowed_networks):
                logger.info("/metrics 拒绝 IP: %s", client_ip)
                await _send_json(send, 403, {"detail": "ip not allowed"})
                return
            return await inner(scope, receive, send)

        # 未知模式: 默认拒绝
        logger.error("未知 metrics_auth_mode=%s,拒绝 /metrics 请求", mode)
        await _send_json(send, 503, {"detail": "unknown auth mode"})
        return

    return authed_app


async def _send_json(send, status: int, body: dict) -> None:
    """发送 JSON 错误响应"""
    import json

    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(payload)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


# P3-5：从租户上下文取 tenant_id 作为 label，基数受控（仅已注册租户）。
# contextvar 未设置时回退 default，单租户历史数据无需改动埋点调用。
def _tenant_label() -> str:
    """获取当前租户 ID 作为 metrics label，未设置时回退 default。"""
    try:
        from core.tenant_context import get_current_tenant

        return get_current_tenant()
    except Exception:
        logger.debug("获取 tenant label 失败,降级 default", exc_info=True)
        return "default"


# 业务指标定义

# 评估总数（按终态 status 与模型档位 model_tier 维度统计）
# P3-5：加 tenant_id label，从 contextvar 自动填充，便于按租户聚合评估量。
EVALUATIONS_TOTAL = Counter(
    "agentvalue_evaluations_total",
    "完成的评估总数",
    ["status", "model_tier", "tenant_id"],
)

# 评估耗时分布（按模型档位统计，单位：秒）
EVALUATION_DURATION_SECONDS = Histogram(
    "agentvalue_evaluation_duration_seconds",
    "单次评估耗时（秒）",
    ["model_tier"],
)

# 审批状态流转次数（记录 action 与 from/to 状态，便于分析审批漏斗）
APPROVAL_TRANSITIONS_TOTAL = Counter(
    "agentvalue_approval_transitions_total",
    "审批状态流转次数",
    ["action", "from_status", "to_status"],
)

# 反馈/申诉总数（按类型统计：feedback / appeal）
FEEDBACK_TOTAL = Counter(
    "agentvalue_feedback_total",
    "员工反馈与申诉数",
    ["type"],
)

# LLM 调用次数（按模型档位与调用状态统计：success / error / timeout 等）
LLM_REQUESTS_TOTAL = Counter(
    "agentvalue_llm_requests_total",
    "LLM 调用次数",
    ["model_tier", "status"],
)

# 当前活跃评估任务数（异步评估 job 的实时存量，Gauge 可升可降）
ACTIVE_JOBS = Gauge(
    "agentvalue_active_jobs",
    "当前活跃评估任务数",
)

# 评估失败数（按失败原因统计，供"评估失败率"告警做分子）
# reason: graph_error 图执行返回错误 / no_result 未生成评估结果 / exception 处理异常
# P3-5：加 tenant_id label，便于按租户聚合失败率告警。
EVALUATION_FAILURES_TOTAL = Counter(
    "agentvalue_evaluation_failures_total",
    "评估失败数",
    ["reason", "tenant_id"],
)

# JWT 黑名单降级放行次数（Redis 故障时回退本地镜像未命中的次数）
TOKEN_BLACKLIST_DEGRADED_TOTAL = Counter(
    "agentvalue_token_blacklist_degraded_total",
    "JWT 黑名单 Redis 故障降级放行次数",
)

# 审计日志写入次数（按 action 统计）
# P3-5：加 tenant_id label，便于按租户聚合审计量做合规报表。
AUDIT_LOG_TOTAL = Counter(
    "agentvalue_audit_log_total",
    "审计日志写入次数",
    ["action", "tenant_id"],
)

# 审计日志写入失败次数（审计异常不阻断业务，但需告警）
AUDIT_LOG_FAILURES_TOTAL = Counter(
    "agentvalue_audit_log_failures_total",
    "审计日志写入失败次数",
)

# 字段级加解密操作次数（按 status 统计：success / failure）
FIELD_ENCRYPTION_TOTAL = Counter(
    "agentvalue_field_encryption_total",
    "字段级加解密操作次数",
    ["status"],
)

# 字段级解密/加密失败次数（AES-GCM 解密失败、密钥不匹配或数据损坏等）
# P1-4：保留旧 Counter 名（向后兼容测试与既有查询），同时新增独立 encrypt 失败 Counter，
# 让加密与解密失败能分别告警（加密失败=明文泄漏风险，解密失败=数据损坏/密钥轮换问题）。
FIELD_DECRYPT_FAILURES_TOTAL = Counter(
    "agentvalue_field_decrypt_failures_total",
    "字段级解密失败次数（AES-GCM 解密失败、密钥不匹配或数据损坏等）",
)
FIELD_ENCRYPTION_FAILURES_TOTAL = Counter(
    "agentvalue_field_encryption_failures_total",
    "字段级加密失败次数（AES-GCM 加密异常，生产环境直接抛出不降级）",
)

# 护栏检查次数（按 type 与 result 统计：input/output × clean/blocked）
GUARD_CHECKS_TOTAL = Counter(
    "agentvalue_guard_checks_total",
    "护栏检查次数",
    ["type", "result"],
)

# 护栏误报次数（命中但实际为正常内容，按 type 统计）
GUARD_FALSE_POSITIVES_TOTAL = Counter(
    "agentvalue_guard_false_positives_total",
    "护栏误报次数",
    ["type"],
)

# ====== Provider 能力扩展指标（vision） ======

# 视觉调用次数（按档位与状态统计）
LLM_VISION_CALLS_TOTAL = Counter(
    "agentvalue_llm_vision_calls_total",
    "LLM 视觉调用次数",
    ["model_tier", "status"],
)

# Provider 健康度评分（0-100，由 ModelRouter 基于最近 health_check 成功率与平均响应时间计算）
PROVIDER_HEALTH_SCORE = Gauge(
    "agentvalue_provider_health_score",
    "Provider 健康度评分（0-100）",
    ["model_tier"],
)

# LLM token 用量（按模型档位、模型名、方向 prompt|completion 统计，供成本与配额分析）
# P1 增强: 加 tenant_id label,便于按租户聚合 token 成本做配额告警与计费
LLM_TOKEN_USAGE_TOTAL = Counter(
    "agentvalue_llm_token_usage_total",
    "LLM token 用量",
    ["tier", "model", "direction", "tenant_id"],
)


# 便捷埋点函数


def record_evaluation(
    status: str, model_tier: str, tenant_id: str | None = None
) -> None:
    """记录一次评估完成（status 为评估终态，model_tier 为模型档位）。

    P3-5：tenant_id 默认从 contextvar 取，便于按租户聚合评估量；可显式传入覆盖。
    """
    EVALUATIONS_TOTAL.labels(
        status=status, model_tier=model_tier, tenant_id=tenant_id or _tenant_label()
    ).inc()


def observe_evaluation_duration(duration: float, model_tier: str) -> None:
    """观测一次评估耗时（duration 单位：秒）。"""
    EVALUATION_DURATION_SECONDS.labels(model_tier=model_tier).observe(duration)


def record_approval_transition(action: str, from_status: str, to_status: str) -> None:
    """记录一次审批状态流转。"""
    APPROVAL_TRANSITIONS_TOTAL.labels(
        action=action, from_status=from_status, to_status=to_status
    ).inc()


def record_feedback(feedback_type: str) -> None:
    """记录一次反馈/申诉（feedback_type: feedback / appeal）。"""
    FEEDBACK_TOTAL.labels(type=feedback_type).inc()


def record_llm_request(model_tier: str, status: str) -> None:
    """记录一次 LLM 调用（status: success / error / timeout 等）。"""
    LLM_REQUESTS_TOTAL.labels(model_tier=model_tier, status=status).inc()


def set_active_jobs(n: int) -> None:
    """设置当前活跃评估任务数。"""
    ACTIVE_JOBS.set(n)


def record_evaluation_failure(reason: str, tenant_id: str | None = None) -> None:
    """记录一次评估失败（reason: graph_error / no_result / exception）。

    P3-5：tenant_id 默认从 contextvar 取，便于按租户聚合失败率告警。
    """
    EVALUATION_FAILURES_TOTAL.labels(
        reason=reason, tenant_id=tenant_id or _tenant_label()
    ).inc()


def record_token_blacklist_degraded() -> None:
    """记录一次 JWT 黑名单 Redis 故障降级放行。"""
    TOKEN_BLACKLIST_DEGRADED_TOTAL.inc()


def record_audit_log(action: str, tenant_id: str | None = None) -> None:
    """记录一次审计日志写入（action 为业务动作名）。

    P3-5：tenant_id 默认从 contextvar 取，便于按租户聚合审计量做合规报表。
    现有调用方（audit_decorator）仅传 action，向后兼容。
    """
    AUDIT_LOG_TOTAL.labels(action=action, tenant_id=tenant_id or _tenant_label()).inc()


def record_audit_log_failure() -> None:
    """记录一次审计日志写入失败（审计异常不阻断业务）。"""
    AUDIT_LOG_FAILURES_TOTAL.inc()


def record_field_encryption(status: str) -> None:
    """记录一次字段级加解密操作（status: success_encrypted / success_passthrough / failure）。"""
    FIELD_ENCRYPTION_TOTAL.labels(status=status).inc()


def record_field_encryption_failure() -> None:
    """记录一次字段级加密失败（AES-GCM 加密异常，生产环境直接抛出不降级）。

    P1-4：与解密失败分离埋点，让加密失败（明文泄漏风险）能独立告警。
    """
    FIELD_ENCRYPTION_FAILURES_TOTAL.inc()


def record_field_decrypt_failure() -> None:
    """记录一次字段级解密失败（AES-GCM 解密失败、密钥不匹配或数据损坏等）。

    保留向后兼容（旧测试与既有查询可能引用此函数）。
    """
    FIELD_DECRYPT_FAILURES_TOTAL.inc()


def record_guard_check(guard_type: str, result: str) -> None:
    """记录一次护栏检查（guard_type: input/output, result: clean/blocked）。"""
    GUARD_CHECKS_TOTAL.labels(type=guard_type, result=result).inc()


def record_guard_false_positive(guard_type: str) -> None:
    """记录一次护栏误报（命中但实际为正常内容）。"""
    GUARD_FALSE_POSITIVES_TOTAL.labels(type=guard_type).inc()


def record_llm_vision_call(model_tier: str, status: str) -> None:
    """记录一次 LLM 视觉调用（status: success / error）。"""
    LLM_VISION_CALLS_TOTAL.labels(model_tier=model_tier, status=status).inc()


def set_provider_health_score(model_tier: str, score: float) -> None:
    """设置某档位 Provider 的健康度评分（0-100）。"""
    PROVIDER_HEALTH_SCORE.labels(model_tier=model_tier).set(score)


def record_token_usage(
    tier: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    tenant_id: str | None = None,
) -> None:
    """记录一次 LLM 调用的 prompt / completion token 用量。

    按 direction 维度分别累加，便于成本分析与配额告警。
    入参为 0 时跳过对应方向，避免产生无意义样本。

    P1 增强: tenant_id 默认从 contextvar 取,便于按租户聚合 token 成本做计费。
    """
    tid = tenant_id or _tenant_label()
    if prompt_tokens:
        LLM_TOKEN_USAGE_TOTAL.labels(
            tier=tier, model=model, direction="prompt", tenant_id=tid
        ).inc(prompt_tokens)
    if completion_tokens:
        LLM_TOKEN_USAGE_TOTAL.labels(
            tier=tier, model=model, direction="completion", tenant_id=tid
        ).inc(completion_tokens)


# ASGI 挂载


def setup_metrics(app: FastAPI) -> None:
    """
    把 prometheus_client 的 ASGI 应用挂到 FastAPI 的 /metrics 路径。

    P0 修复: 不再裸挂 make_asgi_app(),改用 _make_authed_metrics_asgi() 包装,
    加 IP / Bearer 鉴权层,避免业务敏感指标被未授权抓取。

    鉴权模式由 settings.metrics_auth_mode 控制(默认 ip)。
    Prometheus 抓取配置需对应:
    - ip 模式: 直接抓(Prometheus 通常部署在集群内网)
    - token 模式: 抓取 header 加 `Authorization: Bearer <token>`
    """
    from core.config import get_settings

    settings = get_settings()
    app.mount("/metrics", _make_authed_metrics_asgi(settings))
