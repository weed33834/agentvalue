"""Admin Analytics API (P2-1: Token/成本趋势看板)

提供 4 个 admin 端点,聚合 Prometheus 时序指标 + DB 评估统计:
1. GET /token-usage           - Token 用量时序(prompt/completion/total)
2. GET /cost                   - 成本统计(按 model / tenant 维度,USD)
3. GET /provider-distribution  - Provider 调用分布(调用次数 + token 总数 + 平均延迟)
4. GET /evaluation-stats       - 评估统计(总数 + 按状态 + 按周期)

设计要点:
- Prometheus 查询失败时优雅降级返回空数组,不抛 500(避免单点故障阻塞整页)
- 成本计算使用内置 MODEL_PRICING 字典(USD per 1K tokens),未知模型按 0 计费
- DB 评估统计走原生 SQL,绕过 EvaluationService 的 tenant 过滤,
  admin 视角下可看所有租户(tenant_id 可选过滤)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.rbac import Role, require_role
from core.config import get_settings
from core.database import get_db
from models.models import Evaluation

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["admin-analytics"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


# ============================================================
# 模型定价字典(USD per 1K tokens)
# 数据来源: 各 Provider 2024-2025 公开定价页
# 未知模型默认 0(自托管/开源模型不计费)
# ============================================================

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"prompt": 0.005, "completion": 0.015},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4-turbo": {"prompt": 0.01, "completion": 0.03},
    "gpt-4": {"prompt": 0.03, "completion": 0.06},
    "gpt-3.5-turbo": {"prompt": 0.0005, "completion": 0.0015},
    # Anthropic
    "claude-3-5-sonnet": {"prompt": 0.003, "completion": 0.015},
    "claude-3-5-haiku": {"prompt": 0.0008, "completion": 0.004},
    "claude-3-opus": {"prompt": 0.015, "completion": 0.075},
    # Google
    "gemini-1.5-pro": {"prompt": 0.00125, "completion": 0.005},
    "gemini-1.5-flash": {"prompt": 0.000075, "completion": 0.0003},
    # 本地/自托管模型不计费
    "llama": {"prompt": 0.0, "completion": 0.0},
}

# 默认定价: 未知模型按 0 计费(本地模型或自托管服务通常免费)
_DEFAULT_PRICING = {"prompt": 0.0, "completion": 0.0}


def _get_pricing(model: str) -> Dict[str, float]:
    """根据模型名取定价,支持前缀匹配(如 gpt-4o-2024-08-06 命中 gpt-4o)"""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # 前缀匹配: gpt-4o-xxx 命中 gpt-4o
    for prefix, pricing in MODEL_PRICING.items():
        if model.startswith(prefix):
            return pricing
    return _DEFAULT_PRICING


# ============================================================
# Prometheus 查询辅助
# ============================================================


async def _query_range(
    promql: str,
    start: datetime,
    end: datetime,
    step: str,
) -> List[Dict[str, Any]]:
    """调用 Prometheus /api/v1/query_range 拉取时序数据。

    返回 [{timestamp: <unix_ts>, value: <float>}, ...] 形式的时间点列表。
    任何异常(网络/解析/Prometheus 不可达)都吞掉返回空列表,
    避免单次查询失败阻塞整个 dashboard。

    Args:
        promql: PromQL 查询表达式
        start: 起始时间
        end: 结束时间
        step: 步长,如 "1h" / "1d"
    """
    settings = get_settings()
    url = f"{settings.prometheus_url.rstrip('/')}/api/v1/query_range"
    params = {
        "query": promql,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data or data.get("status") != "success":
            logger.warning("Prometheus 返回非 success: %s", data)
            return []
        result = data.get("data", {}).get("result", []) or []
        # 取第一个 series(查询已是聚合后单 series)
        if not result:
            return []
        values = result[0].get("values", []) or []
        points: List[Dict[str, Any]] = []
        for ts, val in values:
            try:
                points.append({"timestamp": float(ts), "value": float(val)})
            except (TypeError, ValueError):
                continue
        return points
    except Exception:
        logger.warning(
            "Prometheus query_range 失败 promql=%s start=%s end=%s step=%s",
            promql,
            start.isoformat(),
            end.isoformat(),
            step,
            exc_info=True,
        )
        return []


async def _query_instant(promql: str) -> List[Dict[str, Any]]:
    """调用 Prometheus /api/v1/query 拉取瞬时值。

    返回每个 label 组合的 [{labels: {...}, value: <float>}, ...]。
    失败时返回空列表。
    """
    settings = get_settings()
    url = f"{settings.prometheus_url.rstrip('/')}/api/v1/query"
    params = {"query": promql}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data or data.get("status") != "success":
            logger.warning("Prometheus 返回非 success: %s", data)
            return []
        result = data.get("data", {}).get("result", []) or []
        points: List[Dict[str, Any]] = []
        for item in result:
            labels = item.get("metric", {}) or {}
            try:
                value_str = item.get("value", [0, "0"])[1]
                value = float(value_str)
            except (IndexError, TypeError, ValueError):
                continue
            points.append({"labels": labels, "value": value})
        return points
    except Exception:
        logger.warning("Prometheus query 失败 promql=%s", promql, exc_info=True)
        return []


def _parse_iso_date(value: Optional[str], default: datetime) -> datetime:
    """解析 ISO 日期字符串(YYYY-MM-DD 或带时间),失败回退 default"""
    if not value:
        return default
    try:
        # 允许 YYYY-MM-DD 或带 T 时间,统一补 UTC tz
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return default


def _window_for_granularity(granularity: str) -> str:
    """根据粒度返回 PromQL increase 窗口大小"""
    if granularity == "hour":
        return "1h"
    return "1d"


def _step_for_granularity(granularity: str) -> str:
    """根据粒度返回 query_range step 大小"""
    if granularity == "hour":
        return "1h"
    return "1d"


# ============================================================
# 端点 1: Token 用量时序
# ============================================================


@router.get("/token-usage")
async def token_usage(
    start_date: Optional[str] = Query(None, description="起始日期 ISO,默认 7 天前"),
    end_date: Optional[str] = Query(None, description="结束日期 ISO,默认今天"),
    granularity: str = Query(
        "day", pattern="^(day|hour)$", description="粒度: day / hour"
    ),
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
    model: Optional[str] = Query(None, description="按模型过滤"),
    tier: Optional[str] = Query(None, description="按档位过滤(L0/L1/L2/L3)"),
):
    """Token 用量时序聚合。

    返回 {timeline: [unix_ts], series: {prompt: [counts], completion: [counts], total: [counts]}}
    """
    now = datetime.now(timezone.utc)
    end = _parse_iso_date(end_date, now)
    start = _parse_iso_date(start_date, now - timedelta(days=7))
    if start >= end:
        start = end - timedelta(days=7)

    # 拼 label 过滤器
    label_filters: List[str] = []
    if tenant_id:
        label_filters.append(f'tenant_id="{tenant_id}"')
    if model:
        label_filters.append(f'model=~"{model}"')
    if tier:
        label_filters.append(f'tier="{tier}"')
    label_filter_str = ",".join(label_filters)
    selector = (
        f"agentvalue_llm_token_usage_total{{{label_filter_str}}}"
        if label_filter_str
        else "agentvalue_llm_token_usage_total"
    )

    window = _window_for_granularity(granularity)
    step = _step_for_granularity(granularity)

    # 三组 PromQL: prompt / completion / total
    # 用 increase 取窗口内增量,避免 Counter 累积值无法看趋势
    promql_prompt = f'sum(increase({selector}{{direction="prompt"}}[{window}]))'
    promql_completion = f'sum(increase({selector}{{direction="completion"}}[{window}]))'
    promql_total = f"sum(increase({selector}[{window}]))"

    prompt_points, completion_points, total_points = await _gather(
        _query_range(promql_prompt, start, end, step),
        _query_range(promql_completion, start, end, step),
        _query_range(promql_total, start, end, step),
    )

    # 三组 query 时间轴理论上对齐(同 step),但 Prometheus 返回的点数可能不一致
    # (某些 step 因无样本被跳过),取并集 + 缺失点补 0
    all_ts = sorted(
        {p["timestamp"] for p in prompt_points}
        | {p["timestamp"] for p in completion_points}
        | {p["timestamp"] for p in total_points}
    )

    def _series_at(points: List[Dict[str, Any]], ts: float) -> float:
        for p in points:
            if abs(p["timestamp"] - ts) < 1.0:
                return p["value"]
        return 0.0

    return {
        "timeline": all_ts,
        "series": {
            "prompt": [_series_at(prompt_points, ts) for ts in all_ts],
            "completion": [_series_at(completion_points, ts) for ts in all_ts],
            "total": [_series_at(total_points, ts) for ts in all_ts],
        },
    }


async def _gather(*aws):
    """并发执行多个 awaitable,返回结果元组(顺序与入参一致)"""
    import asyncio

    results = await asyncio.gather(*aws, return_exceptions=False)
    return results


# ============================================================
# 端点 2: 成本统计
# ============================================================


@router.get("/cost")
async def cost(
    start_date: Optional[str] = Query(None, description="起始日期 ISO,默认 7 天前"),
    end_date: Optional[str] = Query(None, description="结束日期 ISO,默认今天"),
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
):
    """成本统计:按 model / tenant 维度聚合 token 与 USD 成本。

    成本 = prompt_tokens/1000 * prompt_price + completion_tokens/1000 * completion_price
    未知模型按 0 计费(本地/自托管模型)
    """
    now = datetime.now(timezone.utc)
    end = _parse_iso_date(end_date, now)
    start = _parse_iso_date(start_date, now - timedelta(days=7))
    if start >= end:
        start = end - timedelta(days=7)

    # 时间窗口大小,用于 increase 计算
    # Prometheus 不支持动态窗口,直接用整个时间跨度作为窗口
    # 转为 Prometheus duration 字符串(向上取整到秒)
    span_seconds = int((end - start).total_seconds())
    span_seconds = max(span_seconds, 1)
    window = f"{span_seconds}s"

    tenant_filter = f'tenant_id="{tenant_id}",' if tenant_id else ""

    # 按 model × direction 二维查询(用于 by_model 与总成本)
    promql_prompt_by_model = (
        f"sum by (model) (increase(agentvalue_llm_token_usage_total"
        f'{{{tenant_filter}direction="prompt"}}[{window}]))'
    )
    promql_completion_by_model = (
        f"sum by (model) (increase(agentvalue_llm_token_usage_total"
        f'{{{tenant_filter}direction="completion"}}[{window}]))'
    )

    # 按 tenant × direction × model 三维查询(用于 by_tenant 精确成本)
    promql_prompt_by_tenant_model = (
        f"sum by (tenant_id, model) (increase(agentvalue_llm_token_usage_total"
        f'{{direction="prompt"}}[{window}]))'
    )
    promql_completion_by_tenant_model = (
        f"sum by (tenant_id, model) (increase(agentvalue_llm_token_usage_total"
        f'{{direction="completion"}}[{window}]))'
    )

    (
        prompt_by_model,
        completion_by_model,
        prompt_by_tenant_model,
        completion_by_tenant_model,
    ) = await _gather(
        _query_instant(promql_prompt_by_model),
        _query_instant(promql_completion_by_model),
        _query_instant(promql_prompt_by_tenant_model),
        _query_instant(promql_completion_by_tenant_model),
    )

    # 聚合 by_model
    prompt_model_map = {
        p["labels"].get("model", ""): p["value"] for p in prompt_by_model
    }
    completion_model_map = {
        p["labels"].get("model", ""): p["value"] for p in completion_by_model
    }
    all_models = set(prompt_model_map) | set(completion_model_map)
    by_model: List[Dict[str, Any]] = []
    total_cost = 0.0
    for m in all_models:
        if not m:
            continue
        prompt_tokens = int(prompt_model_map.get(m, 0))
        completion_tokens = int(completion_model_map.get(m, 0))
        pricing = _get_pricing(m)
        cost_usd = (
            prompt_tokens / 1000.0 * pricing["prompt"]
            + completion_tokens / 1000.0 * pricing["completion"]
        )
        by_model.append(
            {
                "model": m,
                "tokens": prompt_tokens + completion_tokens,
                "cost_usd": round(cost_usd, 6),
            }
        )
        total_cost += cost_usd
    by_model.sort(key=lambda x: x["cost_usd"], reverse=True)

    # 聚合 by_tenant: 用 tenant × model 二维数据精确计费
    # tenant_prompt_map[(tenant, model)] = tokens
    tenant_prompt_map = {
        (p["labels"].get("tenant_id", ""), p["labels"].get("model", "")): p["value"]
        for p in prompt_by_tenant_model
    }
    tenant_completion_map = {
        (p["labels"].get("tenant_id", ""), p["labels"].get("model", "")): p["value"]
        for p in completion_by_tenant_model
    }
    all_tenant_keys = set(tenant_prompt_map) | set(tenant_completion_map)
    # 按租户聚合 token 与 cost
    tenant_totals: Dict[str, Dict[str, Any]] = {}
    for tenant, model in all_tenant_keys:
        if not tenant:
            continue
        prompt_tokens = int(tenant_prompt_map.get((tenant, model), 0))
        completion_tokens = int(tenant_completion_map.get((tenant, model), 0))
        pricing = _get_pricing(model)
        cost_usd = (
            prompt_tokens / 1000.0 * pricing["prompt"]
            + completion_tokens / 1000.0 * pricing["completion"]
        )
        if tenant not in tenant_totals:
            tenant_totals[tenant] = {"tokens": 0, "cost_usd": 0.0}
        tenant_totals[tenant]["tokens"] += prompt_tokens + completion_tokens
        tenant_totals[tenant]["cost_usd"] += cost_usd

    by_tenant: List[Dict[str, Any]] = [
        {
            "tenant_id": t,
            "tokens": v["tokens"],
            "cost_usd": round(v["cost_usd"], 6),
        }
        for t, v in tenant_totals.items()
    ]
    by_tenant.sort(key=lambda x: x["cost_usd"], reverse=True)

    return {
        "by_model": by_model,
        "by_tenant": by_tenant,
        "total_cost_usd": round(total_cost, 6),
    }


# ============================================================
# 端点 3: Provider 调用分布
# ============================================================


@router.get("/provider-distribution")
async def provider_distribution(
    start_date: Optional[str] = Query(None, description="起始日期 ISO,默认 7 天前"),
    end_date: Optional[str] = Query(None, description="结束日期 ISO,默认今天"),
):
    """Provider 调用分布:每个 model 的调用次数 / token 总数 / 平均延迟。

    数据源:
    - 调用次数: agentvalue_llm_requests_total Counter 的 increase
    - token 总数: agentvalue_llm_token_usage_total Counter 的 increase
    - 平均延迟: agentvalue_llm_request_duration_seconds (Histogram,可能未启用)
    """
    now = datetime.now(timezone.utc)
    end = _parse_iso_date(end_date, now)
    start = _parse_iso_date(start_date, now - timedelta(days=7))
    if start >= end:
        start = end - timedelta(days=7)

    span_seconds = max(int((end - start).total_seconds()), 1)
    window = f"{span_seconds}s"

    # 调用次数: 按 model_tier 聚合(agentvalue_llm_requests_total 的 label 是 model_tier)
    promql_calls = (
        f"sum by (model_tier) (increase(agentvalue_llm_requests_total[{window}]))"
    )
    # token 总数: 按 model 聚合(指标 label 是 model)
    promql_tokens = (
        f"sum by (model) (increase(agentvalue_llm_token_usage_total[{window}]))"
    )
    # 平均延迟: Histogram 的 sum/count 比值
    # agentvalue_llm_request_duration_seconds 可能未注册,查询返回空数组时优雅降级
    promql_latency = (
        f"sum by (model_tier) (rate(agentvalue_llm_request_duration_seconds_sum[{window}])) "
        f"/ sum by (model_tier) (rate(agentvalue_llm_request_duration_seconds_count[{window}]))"
    )

    calls_points, tokens_points, latency_points = await _gather(
        _query_instant(promql_calls),
        _query_instant(promql_tokens),
        _query_instant(promql_latency),
    )

    # 以 model 为 provider 维度,合并调用次数(按 tier 取最近匹配)
    providers: List[Dict[str, Any]] = []
    for tp in tokens_points:
        model_name = tp["labels"].get("model", "")
        if not model_name:
            continue
        token_total = int(tp["value"])
        # 调用次数: 该 model 所属 tier 的 counter(简化: tier 取所有 model_tier 总和)
        call_count = int(sum(p["value"] for p in calls_points))
        # 平均延迟: 取所有 tier 的平均(简化处理)
        latency_values = [p["value"] for p in latency_points if p["value"] > 0]
        avg_latency_ms = (
            round(sum(latency_values) / len(latency_values) * 1000, 2)
            if latency_values
            else 0.0
        )
        providers.append(
            {
                "name": model_name,
                "call_count": call_count,
                "token_total": token_total,
                "avg_latency_ms": avg_latency_ms,
            }
        )

    # 若无 model 数据但 calls 有,补一条占位(避免前端空状态)
    if not providers and calls_points:
        total_calls = int(sum(p["value"] for p in calls_points))
        latency_values = [p["value"] for p in latency_points if p["value"] > 0]
        avg_latency_ms = (
            round(sum(latency_values) / len(latency_values) * 1000, 2)
            if latency_values
            else 0.0
        )
        providers.append(
            {
                "name": "all",
                "call_count": total_calls,
                "token_total": 0,
                "avg_latency_ms": avg_latency_ms,
            }
        )

    providers.sort(key=lambda x: x["token_total"], reverse=True)
    return {"providers": providers}


# ============================================================
# 端点 4: 评估统计
# ============================================================


@router.get("/evaluation-stats")
async def evaluation_stats(
    start_date: Optional[str] = Query(None, description="起始日期 ISO,默认 7 天前"),
    end_date: Optional[str] = Query(None, description="结束日期 ISO,默认今天"),
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
    session: AsyncSession = Depends(get_db),
):
    """评估统计:总数 + 按状态分布 + 按周期分布。

    数据源: evaluations 表(原生 SQL,绕过 EvaluationService 的 tenant 过滤,
    admin 可看所有租户,tenant_id 可选过滤)
    """
    now = datetime.now(timezone.utc)
    end = _parse_iso_date(end_date, now)
    start = _parse_iso_date(start_date, now - timedelta(days=7))
    if start >= end:
        start = end - timedelta(days=7)

    # 总数
    count_stmt = select(func.count(Evaluation.id))
    if tenant_id:
        count_stmt = count_stmt.where(Evaluation.tenant_id == tenant_id)
    count_stmt = count_stmt.where(
        Evaluation.created_at >= start,
        Evaluation.created_at <= end,
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    # 按状态分布
    status_stmt = (
        select(Evaluation.status, func.count(Evaluation.id))
        .where(
            Evaluation.created_at >= start,
            Evaluation.created_at <= end,
        )
        .group_by(Evaluation.status)
    )
    if tenant_id:
        status_stmt = status_stmt.where(Evaluation.tenant_id == tenant_id)
    status_rows = (await session.execute(status_stmt)).all()
    by_status: Dict[str, int] = {
        "ai_drafted": 0,
        "approved": 0,
        "rejected": 0,
        "manager_review": 0,
        "hr_audit": 0,
    }
    for status_val, count_val in status_rows:
        if status_val in by_status:
            by_status[status_val] = int(count_val)
        else:
            # 未知状态也存进去(向前兼容新增状态)
            by_status[status_val] = int(count_val)

    # 按周期分布(period 字段,如 2026-W28)
    period_stmt = (
        select(Evaluation.period, func.count(Evaluation.id))
        .where(
            Evaluation.created_at >= start,
            Evaluation.created_at <= end,
        )
        .group_by(Evaluation.period)
        .order_by(Evaluation.period)
    )
    if tenant_id:
        period_stmt = period_stmt.where(Evaluation.tenant_id == tenant_id)
    period_rows = (await session.execute(period_stmt)).all()
    by_period: List[Dict[str, Any]] = [
        {"period": p, "count": int(c)} for p, c in period_rows if p
    ]

    return {
        "total_evaluations": int(total),
        "by_status": by_status,
        "by_period": by_period,
    }
