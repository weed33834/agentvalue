"""出站 Webhook 投递服务 (WS-3, 对标 Svix / Stripe Webhooks)

能力
----
1. **订阅匹配**: 精确名 / ``evaluation.*`` 前缀通配 / ``*`` 全量
2. **幂等入队**: 同一 ``(subscription_id, event_id)`` 只会产生一条投递
3. **签名防重放**: Stripe 风格 ``X-AgentValue-Signature: t=<ts>,v1=<hex>``
4. **指数退避重试**: 1s → 2s → 4s → 8s → 16s → 32s (封顶) + 抖动
5. **死信**: ``attempt >= max_attempts`` 后置 ``dead``, 仅可手动重放
6. **熔断**: 订阅连续失败达阈值后自动禁用并写 ``disabled_reason``
7. **SSRF 防护**: 复用 ``core/workflow_engine._is_internal_url`` 的内网黑名单

签名校验配方 (发给用户文档 / SDK 实现的唯一权威描述)
------------------------------------------------------
收到请求后:

1. 取 header ``X-AgentValue-Signature``, 形如 ``t=1754630400,v1=9f86d0...``;
2. 拆出 ``t`` (unix 秒) 与 ``v1`` (hex);
3. 校验 ``abs(now - t) <= 300`` (5 分钟容忍窗口), 超出即判定重放攻击;
4. 计算 ``expected = HMAC_SHA256(key=secret, msg=f"{t}.{raw_body}").hexdigest()``,
   其中 ``raw_body`` 是**未经任何反序列化/重新序列化的原始请求体字符串**;
5. 用常量时间比较 ``hmac.compare_digest(expected, v1)``。

注意: 必须用原始 body 而非 ``json.dumps(json.loads(body))``, 否则键序/空白差异
会导致签名不匹配。平台侧发送时固定使用 ``sort_keys=True`` + 紧凑分隔符, 但校验方
不应依赖该细节。

事务边界
--------
本服务**始终使用自己的数据库会话** (``AsyncSessionLocal``), 绝不复用业务侧会话,
从而保证 webhook 失败永远不会污染或回滚业务事务。
``dispatch`` 自身吞掉所有异常并打日志, 调用方即使忘记 try/except 也不会被影响。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx
from sqlalchemy import func, select

from models.webhook_subscription import (
    DELIVERY_RETRYABLE_STATUSES,
    DELIVERY_STATUS_DEAD,
    DELIVERY_STATUS_DELIVERING,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SUCCESS,
    RESPONSE_BODY_MAX_CHARS,
    WebhookDelivery,
    WebhookSubscription,
)

logger = logging.getLogger(__name__)


# ============================================================
# 事件目录
# ============================================================

# 平台真实会发出的事件。**只登记代码里确实 dispatch 的事件**,
# 前端订阅页直接渲染本目录作为勾选项, 登记未实现的事件等同于欺骗用户。
EVENT_CATALOG: List[Dict[str, str]] = [
    {
        "name": "evaluation.completed",
        "category": "evaluation",
        "description": "AI 评估生成完毕并落库 (services/evaluation_service.create_evaluation)",
    },
    {
        "name": "evaluation.approved",
        "category": "evaluation",
        "description": "评估经审批流转为 approved 状态",
    },
    {
        "name": "evaluation.rejected",
        "category": "evaluation",
        "description": "评估被驳回 (rejected / 需重评)",
    },
    {
        "name": "alert.triggered",
        "category": "alert",
        "description": "告警触发并对外发送通知 (services/alert_service.send_alert)",
    },
    {
        "name": "alert.resolved",
        "category": "alert",
        "description": "告警被标记为已解决",
    },
    {
        "name": "budget.exceeded",
        "category": "billing",
        "description": "租户成本预算达到告警阈值 (services/budget_service.check_budget)",
    },
    {
        "name": "user.created",
        "category": "user",
        "description": "新用户被创建 (含批量导入与自动建档)",
    },
    {
        "name": "ping",
        "category": "system",
        "description": "订阅连通性自检事件, 由「测试」按钮手动触发",
    },
]

# 事件名集合, 用于 dispatch 时的合法性提示
EVENT_NAMES = frozenset(item["name"] for item in EVENT_CATALOG)

EVENT_PING = "ping"


# ============================================================
# 可调参数
# ============================================================

# 退避基数上限 (秒): 1,2,4,8,16,32 后不再翻倍
BACKOFF_CAP_SECONDS = 32
# 抖动比例: 实际延迟 = base * (1 + uniform(0, JITTER_RATIO)), 避免同批投递齐步重试
BACKOFF_JITTER_RATIO = 0.2
# 订阅连续失败多少次后自动禁用
AUTO_DISABLE_AFTER_FAILURES = 10
# 签名时间戳容忍窗口 (秒), 与文档中的校验配方一致
SIGNATURE_TOLERANCE_SECONDS = 300
# retry poller 单轮并发上限
POLLER_CONCURRENCY = 8

# 持有 immediate delivery 的 task 强引用, 防止被 GC 提前回收。
# 注意: 这只是「尽快投递」的优化路径, 投递行在 dispatch 时已落库,
# 进程重启后由 core/scheduler.py 注册的周期任务兜底, 不存在任务丢失。
_INFLIGHT_TASKS: set = set()


# ============================================================
# 会话与工具函数
# ============================================================


def _session_factory():
    """惰性获取 sessionmaker。

    延迟到调用时 import, 使测试可以 monkeypatch ``core.database.AsyncSessionLocal``
    指向临时库 (与 core/observe.py 的写法保持一致)。
    """
    from core.database import AsyncSessionLocal

    return AsyncSessionLocal


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_secret() -> str:
    """生成订阅签名密钥 (32 字节 URL-safe 随机串)"""
    return f"whsec_{secrets.token_urlsafe(32)}"


def event_matches(patterns: Sequence[str], event: str) -> bool:
    """判断事件是否命中订阅的事件模式列表。

    支持三种模式:
    - ``"*"``: 匹配全部
    - ``"evaluation.*"``: 前缀通配, 命中 ``evaluation.completed`` 等
    - ``"alert.triggered"``: 精确匹配

    Args:
        patterns: 订阅登记的事件模式列表。
        event: 实际发生的事件名。

    Returns:
        True 表示该订阅需要收到此事件。
    """
    if not patterns:
        return False
    for raw in patterns:
        if not isinstance(raw, str):
            continue
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-1]  # 保留末尾的 "."
            if event.startswith(prefix):
                return True
            continue
        if pattern.endswith("*"):
            if event.startswith(pattern[:-1]):
                return True
            continue
        if pattern == event:
            return True
    return False


def backoff_seconds(attempt: int, *, jitter: bool = True) -> float:
    """第 ``attempt`` 次尝试失败后, 距离下次重试应等待的秒数。

    基础序列: attempt=1 → 1s, 2 → 2s, 3 → 4s, 4 → 8s, 5 → 16s, 6+ → 32s (封顶)。
    ``jitter=True`` 时在基数上叠加 [0, 20%) 的正向抖动。

    Args:
        attempt: 已完成的尝试次数 (从 1 开始)。
        jitter: 是否叠加抖动, 单测校验固定序列时传 False。

    Returns:
        等待秒数 (float)。
    """
    if attempt < 1:
        attempt = 1
    base = float(min(2 ** (attempt - 1), BACKOFF_CAP_SECONDS))
    if not jitter:
        return base
    return base * (1.0 + random.uniform(0.0, BACKOFF_JITTER_RATIO))


# ============================================================
# 签名
# ============================================================


def build_signature(secret: str, timestamp: int, body: str) -> str:
    """构造 ``X-AgentValue-Signature`` 头的值。

    Args:
        secret: 订阅密钥。
        timestamp: unix 秒。
        body: 原始请求体字符串。

    Returns:
        ``t=<timestamp>,v1=<hex_hmac_sha256>``
    """
    signed_payload = f"{timestamp}.{body}"
    digest = hmac.new(
        secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def parse_signature(header: str) -> Dict[str, str]:
    """解析签名头为 dict, 非法输入返回空 dict。"""
    parsed: Dict[str, str] = {}
    if not header:
        return parsed
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def verify_signature(
    secret: str,
    body: str,
    signature_header: str,
    *,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> bool:
    """校验签名 (服务端自检 / SDK 参考实现, 与文档配方一一对应)。

    Args:
        secret: 订阅密钥。
        body: 收到的原始请求体字符串。
        signature_header: ``X-AgentValue-Signature`` 头原值。
        tolerance_seconds: 时间戳容忍窗口, <=0 表示不校验时间。
        now: 覆盖当前时间 (unix 秒), 仅测试使用。

    Returns:
        True 表示签名有效且未过期。
    """
    parsed = parse_signature(signature_header)
    ts_raw = parsed.get("t")
    provided = parsed.get("v1")
    if not ts_raw or not provided:
        return False
    try:
        timestamp = int(ts_raw)
    except ValueError:
        return False
    if tolerance_seconds > 0:
        current = int(time.time()) if now is None else now
        if abs(current - timestamp) > tolerance_seconds:
            return False
    expected = build_signature(secret, timestamp, body)
    expected_v1 = parse_signature(expected).get("v1", "")
    return hmac.compare_digest(expected_v1, provided)


# ============================================================
# 事件信封
# ============================================================


def build_envelope(delivery: WebhookDelivery) -> Dict[str, Any]:
    """构造对外 POST 的事件信封。

    从投递行本身派生, 保证重试时字节级一致 (签名可复算)。
    """
    created = delivery.created_at or _now()
    return {
        "id": f"whd_{delivery.id}",
        "event": delivery.event,
        "event_id": delivery.event_id,
        "created_at": created.isoformat(),
        "tenant_id": delivery.tenant_id,
        "data": delivery.payload,
    }


def serialize_body(envelope: Dict[str, Any]) -> str:
    """信封 → 紧凑且键序稳定的 JSON 字符串 (签名对象)。"""
    return json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


async def _is_url_blocked(url: str) -> bool:
    """SSRF 检查: 复用 core/workflow_engine 的内网黑名单实现。

    该函数内部会做同步 DNS 解析, 放到线程池执行避免阻塞事件循环。
    这里刻意复用而非另写一份, 保证 HTTP 节点与 webhook 的防护口径一致。
    """
    from core.workflow_engine import _is_internal_url

    try:
        return await asyncio.to_thread(_is_internal_url, url)
    except Exception:
        logger.exception("SSRF 检查异常, 保守判定为不安全 url=%s", url)
        return True


# ============================================================
# 分发
# ============================================================


async def dispatch(
    event: str,
    payload: Any,
    *,
    tenant_id: str,
    event_id: Optional[str] = None,
    deliver_now: bool = True,
) -> List[int]:
    """把一个业务事件分发给所有匹配的启用订阅。

    行为:
    1. 查询该租户下 ``enabled=True`` 且事件模式命中的订阅;
    2. 每个订阅创建一条 ``pending`` 投递 (``event_id`` 已存在则跳过, 保证幂等);
    3. ``deliver_now=True`` 时立即异步投递一次, 失败由退避重试与 poller 接管。

    本函数**不会抛异常**: 任何失败只记录日志并返回已创建的 id 列表,
    确保 webhook 永远不会破坏业务主链路。

    Args:
        event: 事件名, 建议取自 ``EVENT_CATALOG``。
        payload: 事件业务数据 (需可 JSON 序列化)。
        tenant_id: 租户 ID。
        event_id: 幂等键, 同一订阅下重复的 event_id 不会二次入队。
        deliver_now: 是否立即触发投递。

    Returns:
        新建的投递 ID 列表 (幂等命中或无匹配订阅时为空列表)。
    """
    created_ids: List[int] = []
    try:
        if event not in EVENT_NAMES:
            logger.warning("dispatch 未登记的事件名 %s (仍会投递)", event)

        session_factory = _session_factory()
        async with session_factory() as session:
            subs = (
                (
                    await session.execute(
                        select(WebhookSubscription).where(
                            WebhookSubscription.tenant_id == tenant_id,
                            WebhookSubscription.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            matched = [s for s in subs if event_matches(s.events or [], event)]
            if not matched:
                return []

            now = _now()
            for sub in matched:
                if event_id:
                    exists = (
                        await session.execute(
                            select(WebhookDelivery.id).where(
                                WebhookDelivery.subscription_id == sub.id,
                                WebhookDelivery.event_id == event_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if exists is not None:
                        logger.debug(
                            "webhook 幂等命中, 跳过入队 sub=%s event_id=%s",
                            sub.id,
                            event_id,
                        )
                        continue
                delivery = WebhookDelivery(
                    subscription_id=sub.id,
                    tenant_id=tenant_id,
                    event=event,
                    event_id=event_id,
                    payload=payload,
                    status=DELIVERY_STATUS_PENDING,
                    attempt=0,
                    max_attempts=sub.max_attempts,
                    next_retry_at=now,
                )
                session.add(delivery)
                await session.flush()
                created_ids.append(delivery.id)
            await session.commit()
    except Exception:
        logger.exception("webhook dispatch 失败 event=%s tenant=%s", event, tenant_id)
        return created_ids

    if deliver_now and created_ids:
        _schedule_immediate(created_ids)
    return created_ids


def dispatch_after_commit(
    session: Any,
    event: str,
    payload: Any,
    *,
    tenant_id: str,
    event_id: Optional[str] = None,
) -> bool:
    """把事件挂到业务会话的 ``after_commit`` 上, 提交成功后才真正分发。

    为什么需要它: 多数 service 层只 ``flush`` 不 ``commit`` (事务边界在路由层)。
    若在 flush 后立即分发, 事务一旦回滚就会发出「凭空出现」的事件。挂 after_commit
    可保证只有真正落库的业务变更才触发 webhook。

    该函数是**同步**的, 不会 await 任何 IO, 因此可以安全嵌入业务代码;
    真正的分发在提交后以后台任务执行, 失败只打日志。

    已知边界: 若事务回滚后同一个 session 又发生了另一次 commit, 监听器会在那次
    commit 上触发。考虑到 service 层会话生命周期即请求生命周期, 该场景可忽略。

    Args:
        session: SQLAlchemy ``AsyncSession`` (或带 ``sync_session`` 的兼容对象)。
        event: 事件名。
        payload: 事件数据。
        tenant_id: 租户 ID。
        event_id: 幂等键。

    Returns:
        True 表示监听器已挂载 (或已降级为立即分发)。
    """

    def _fire() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "无事件循环, 事件 %s 未能分发 (tenant=%s)", event, tenant_id
            )
            return
        task = loop.create_task(
            dispatch(event, payload, tenant_id=tenant_id, event_id=event_id)
        )
        _INFLIGHT_TASKS.add(task)
        task.add_done_callback(_INFLIGHT_TASKS.discard)

    try:
        from sqlalchemy import event as sa_event

        sync_session = getattr(session, "sync_session", None)
        if sync_session is None:
            raise RuntimeError("会话不支持 after_commit 挂钩")

        def _on_commit(_sess: Any) -> None:
            _fire()

        sa_event.listen(sync_session, "after_commit", _on_commit, once=True)
        return True
    except Exception:
        logger.exception(
            "挂载 after_commit 失败, 降级为立即分发 event=%s", event
        )
        _fire()
        return True


def _schedule_immediate(delivery_ids: Sequence[int]) -> None:
    """尽力立即投递: 无事件循环时静默跳过, 交给周期任务兜底。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for did in delivery_ids:
        task = loop.create_task(_deliver_quietly(did))
        _INFLIGHT_TASKS.add(task)
        task.add_done_callback(_INFLIGHT_TASKS.discard)


async def _deliver_quietly(delivery_id: int) -> None:
    """后台投递包装: 异常只打日志, 不产生 never-retrieved 噪声。"""
    try:
        await deliver(delivery_id)
    except Exception:
        logger.exception("webhook 后台投递异常 delivery_id=%s", delivery_id)


# ============================================================
# 投递
# ============================================================


async def deliver(delivery_id: int) -> Dict[str, Any]:
    """执行一次投递 (含签名、超时、SSRF 防护、退避与死信判定)。

    Args:
        delivery_id: 投递 ID。

    Returns:
        ``{"delivery_id", "status", "attempt", "response_code", "error", "duration_ms"}``
    """
    session_factory = _session_factory()
    async with session_factory() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"delivery_id": delivery_id, "status": "not_found"}
        if delivery.status in (DELIVERY_STATUS_SUCCESS, DELIVERY_STATUS_DEAD):
            return {
                "delivery_id": delivery_id,
                "status": delivery.status,
                "attempt": delivery.attempt,
                "skipped": True,
            }

        subscription = await session.get(
            WebhookSubscription, delivery.subscription_id
        )
        if subscription is None:
            delivery.status = DELIVERY_STATUS_DEAD
            delivery.error = "订阅已删除, 投递作废"
            delivery.next_retry_at = None
            await session.commit()
            return {
                "delivery_id": delivery_id,
                "status": DELIVERY_STATUS_DEAD,
                "error": delivery.error,
            }

        # 占位为 delivering 并推进尝试计数, 先落库避免并发重复投递
        delivery.status = DELIVERY_STATUS_DELIVERING
        delivery.attempt += 1
        attempt = delivery.attempt
        max_attempts = delivery.max_attempts or subscription.max_attempts
        envelope = build_envelope(delivery)
        body = serialize_body(envelope)
        url = subscription.url
        secret = subscription.secret
        timeout = subscription.timeout_seconds or 10
        extra_headers = subscription.headers or {}
        await session.commit()

        # ---- 网络 IO (不持有任何行级锁) ----
        if await _is_url_blocked(url):
            # 内网/非法协议属于永久性失败, 重试无意义, 直接死信
            delivery.status = DELIVERY_STATUS_DEAD
            delivery.error = f"目标地址被 SSRF 防护拦截: {url}"
            delivery.next_retry_at = None
            delivery.duration_ms = 0
            await _mark_subscription_failure(session, subscription, delivery.error)
            await session.commit()
            logger.warning("webhook 投递被 SSRF 防护拦截 delivery=%s url=%s", delivery_id, url)
            return {
                "delivery_id": delivery_id,
                "status": DELIVERY_STATUS_DEAD,
                "attempt": attempt,
                "error": delivery.error,
            }

        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AgentValue-Webhook/1.0",
            "X-AgentValue-Event": delivery.event,
            "X-AgentValue-Delivery": str(delivery.id),
            "X-AgentValue-Timestamp": str(timestamp),
            "X-AgentValue-Signature": build_signature(secret, timestamp, body),
        }
        for key, value in extra_headers.items():
            # 自定义头不允许覆盖签名相关头, 防止订阅配置削弱安全性
            if str(key).lower().startswith("x-agentvalue-"):
                continue
            headers[str(key)] = str(value)

        started = time.perf_counter()
        status_code: Optional[int] = None
        response_text: Optional[str] = None
        error: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url, content=body.encode("utf-8"), headers=headers
                )
                status_code = response.status_code
                response_text = (response.text or "")[:RESPONSE_BODY_MAX_CHARS]
        except Exception as exc:  # noqa: BLE001 - 任何传输层异常都算投递失败
            error = f"{type(exc).__name__}: {exc}"[:RESPONSE_BODY_MAX_CHARS]
        duration_ms = int((time.perf_counter() - started) * 1000)

        # ---- 结果落库 ----
        delivery.response_code = status_code
        delivery.response_body = response_text
        delivery.duration_ms = duration_ms
        succeeded = status_code is not None and 200 <= status_code < 300

        if succeeded:
            delivery.status = DELIVERY_STATUS_SUCCESS
            delivery.error = None
            delivery.next_retry_at = None
            delivery.delivered_at = _now()
            subscription.consecutive_failures = 0
            subscription.last_status = DELIVERY_STATUS_SUCCESS
            subscription.last_delivery_at = delivery.delivered_at
        else:
            if error is None:
                error = f"HTTP {status_code}"
            delivery.error = error
            if attempt >= max_attempts:
                delivery.status = DELIVERY_STATUS_DEAD
                delivery.next_retry_at = None
                logger.error(
                    "webhook 投递进入死信 delivery=%s sub=%s event=%s attempt=%s/%s: %s",
                    delivery_id,
                    subscription.id,
                    delivery.event,
                    attempt,
                    max_attempts,
                    error,
                )
            else:
                delivery.status = DELIVERY_STATUS_FAILED
                delivery.next_retry_at = _now() + timedelta(
                    seconds=backoff_seconds(attempt)
                )
            await _mark_subscription_failure(session, subscription, error)

        await session.commit()
        return {
            "delivery_id": delivery_id,
            "status": delivery.status,
            "attempt": attempt,
            "response_code": status_code,
            "error": delivery.error,
            "duration_ms": duration_ms,
        }


async def _mark_subscription_failure(
    session, subscription: WebhookSubscription, error: str
) -> None:
    """累计订阅连续失败次数, 达阈值自动禁用 (熔断)。"""
    subscription.consecutive_failures = (subscription.consecutive_failures or 0) + 1
    subscription.last_status = DELIVERY_STATUS_FAILED
    subscription.last_delivery_at = _now()
    if (
        subscription.enabled
        and subscription.consecutive_failures >= AUTO_DISABLE_AFTER_FAILURES
    ):
        subscription.enabled = False
        subscription.disabled_reason = (
            f"连续失败 {subscription.consecutive_failures} 次已自动禁用, "
            f"最后错误: {error}"
        )[:1000]
        logger.error(
            "webhook 订阅 %s 连续失败 %s 次, 已自动禁用",
            subscription.id,
            subscription.consecutive_failures,
        )


# ============================================================
# 重试轮询 (由 core/scheduler.py 注册的周期任务驱动)
# ============================================================


async def process_due_deliveries(limit: int = 50) -> Dict[str, Any]:
    """扫描并投递所有到期的重试。

    查询条件: ``status IN (pending, failed) AND next_retry_at <= now()``。
    以有界并发执行, 避免单轮把连接池打满。

    Args:
        limit: 单轮最多处理的投递条数。

    Returns:
        ``{"scanned", "success", "failed", "dead"}`` 摘要。
    """
    session_factory = _session_factory()
    now = _now()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(WebhookDelivery.id)
                    .where(
                        WebhookDelivery.status.in_(DELIVERY_RETRYABLE_STATUSES),
                        WebhookDelivery.next_retry_at.is_not(None),
                        WebhookDelivery.next_retry_at <= now,
                    )
                    .order_by(WebhookDelivery.next_retry_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    summary = {"scanned": len(rows), "success": 0, "failed": 0, "dead": 0}
    if not rows:
        return summary

    semaphore = asyncio.Semaphore(POLLER_CONCURRENCY)

    async def _run(did: int) -> Optional[str]:
        async with semaphore:
            try:
                result = await deliver(did)
                return result.get("status")
            except Exception:
                logger.exception("webhook 重试投递异常 delivery_id=%s", did)
                return None

    for status_value in await asyncio.gather(*[_run(d) for d in rows]):
        if status_value == DELIVERY_STATUS_SUCCESS:
            summary["success"] += 1
        elif status_value == DELIVERY_STATUS_DEAD:
            summary["dead"] += 1
        elif status_value is not None:
            summary["failed"] += 1

    logger.info(
        "[webhook_delivery] 到期投递 %s 条: 成功 %s / 失败 %s / 死信 %s",
        summary["scanned"],
        summary["success"],
        summary["failed"],
        summary["dead"],
    )
    return summary


# ============================================================
# 手动运维: 重放 / 连通性自检
# ============================================================


async def replay(delivery_id: int) -> Dict[str, Any]:
    """手动重放一条投递 (含死信)。

    重置 ``attempt`` 与错误信息后立即重新投递, 保留原始 payload 与 event_id,
    因此重放出去的请求体与首次完全一致 (含 ``id`` 字段), 便于对端做幂等。

    Args:
        delivery_id: 投递 ID。

    Returns:
        deliver() 的结果; 投递不存在时 ``{"status": "not_found"}``。
    """
    session_factory = _session_factory()
    async with session_factory() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"delivery_id": delivery_id, "status": "not_found"}
        delivery.status = DELIVERY_STATUS_PENDING
        delivery.attempt = 0
        delivery.error = None
        delivery.response_code = None
        delivery.response_body = None
        delivery.delivered_at = None
        delivery.next_retry_at = _now()
        await session.commit()
    return await deliver(delivery_id)


async def test_subscription(subscription_id: int) -> Dict[str, Any]:
    """向订阅端点发送一条 ``ping`` 事件, 用于 UI 上的「测试」按钮。

    无论订阅当前是否 enabled 都会发送 (用户可能正是想验证修好没有),
    但仍会走完整的签名 / SSRF / 超时链路, 结果与真实投递口径一致。

    Args:
        subscription_id: 订阅 ID。

    Returns:
        deliver() 的结果; 订阅不存在时 ``{"status": "not_found"}``。
    """
    session_factory = _session_factory()
    async with session_factory() as session:
        subscription = await session.get(WebhookSubscription, subscription_id)
        if subscription is None:
            return {"subscription_id": subscription_id, "status": "not_found"}
        delivery = WebhookDelivery(
            subscription_id=subscription.id,
            tenant_id=subscription.tenant_id,
            event=EVENT_PING,
            event_id=f"ping-{uuid.uuid4().hex[:16]}",
            payload={
                "message": "AgentValue webhook 连通性测试",
                "subscription_id": subscription.id,
                "sent_at": _now().isoformat(),
            },
            status=DELIVERY_STATUS_PENDING,
            attempt=0,
            # 自检不做重试: 用户在 UI 等结果, 失败应立刻看到
            max_attempts=1,
            next_retry_at=_now(),
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
        await session.commit()
    return await deliver(delivery_id)


# ============================================================
# 统计
# ============================================================


async def delivery_stats(
    tenant_id: str, *, subscription_id: Optional[int] = None
) -> Dict[str, Any]:
    """投递状态分布统计 (供 admin 概览卡片)。

    Args:
        tenant_id: 租户 ID。
        subscription_id: 限定单个订阅, None 表示全租户。

    Returns:
        ``{"total", "by_status", "success_rate", "avg_duration_ms"}``
    """
    session_factory = _session_factory()
    async with session_factory() as session:
        conditions = [WebhookDelivery.tenant_id == tenant_id]
        if subscription_id is not None:
            conditions.append(WebhookDelivery.subscription_id == subscription_id)

        rows = (
            await session.execute(
                select(WebhookDelivery.status, func.count())
                .where(*conditions)
                .group_by(WebhookDelivery.status)
            )
        ).all()
        by_status = {status_value: count for status_value, count in rows}

        avg_duration = (
            await session.execute(
                select(func.avg(WebhookDelivery.duration_ms)).where(*conditions)
            )
        ).scalar()

    total = sum(by_status.values())
    success = by_status.get(DELIVERY_STATUS_SUCCESS, 0)
    return {
        "total": total,
        "by_status": by_status,
        "success_rate": round(success / total, 4) if total else 0.0,
        "avg_duration_ms": round(float(avg_duration), 2) if avg_duration else 0.0,
    }
