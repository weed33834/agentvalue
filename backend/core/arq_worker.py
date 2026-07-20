"""
arq 任务队列 Worker(P3 规模化就绪,H2)

启动方式(独立 worker 进程,与 FastAPI 解耦):
    cd backend
    arq core.arq_worker.WorkerSettings

特点:
1. 独立进程,与 FastAPI 解耦,可多实例水平扩展
2. 自动重投:arq max_tries 配置,失败重试到顶入死信队列
3. 死信队列:`agentvalue:dead_letter:{job_id}`,运维可捞起重投
4. 优雅关闭:on_shutdown 关闭 DB 连接池

设计要点:
- enqueue 仍由 FastAPI 进程发起(ArqJobQueue.enqueue_job)
- worker 进程调用 routes._run_evaluation_job 复用现有评估逻辑,不重复实现
- 状态查询走 RedisJobQueue(共享 Redis),与不开 arq 时一致

arq 0.28+ API 适配:
- 不再使用 ActorMeta 元类(arq 0.26 之后移除)
- WorkerSettings 为普通类,属性被 arq.worker.get_kwargs 读取后构造 Worker
- redis_settings 必须是 RedisSettings 实例(早期版本支持 callable,0.28 已废弃)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from arq import ArqRedis
from arq.connections import RedisSettings

from core.config import get_settings

logger = logging.getLogger(__name__)

# 死信队列 key 前缀(与 RedisJobQueue 一致的命名空间)
DEAD_LETTER_PREFIX = "agentvalue:dead_letter:"

# arq 队列名(默认队列)
QUEUE_NAME = "evaluations"


def _build_redis_settings() -> RedisSettings:
    """从 settings.redis_url 构造 arq RedisSettings。

    from_dsn 仅做 URL 解析(不连 Redis),可在模块导入期调用。
    若未配置 REDIS_URL(本地开发/测试环境)返回默认 RedisSettings(localhost:6379),
    保证模块导入不抛异常;真正启动 worker 时若 Redis 不可达会由 arq 报错。
    """
    s = get_settings()
    if not s.redis_url:
        # 默认 localhost:6379,与 arq 默认行为一致
        # 仅 FastAPI 进程惰性 import 本模块但实际不启动 worker 的场景下使用
        logger.debug("未配置 REDIS_URL,arq_worker 使用默认 RedisSettings(localhost:6379)")
        return RedisSettings()
    # arq 支持 redis:// rediss:// unix:// URL
    return RedisSettings.from_dsn(s.redis_url)


async def _write_to_dead_letter(
    redis: ArqRedis,
    job_id: str,
    raw_inputs: list,
    reason: str,
) -> None:
    """将失败任务写入死信队列供运维捞取"""
    dead_key = f"{DEAD_LETTER_PREFIX}{job_id}"
    payload = {
        "job_id": job_id,
        "raw_inputs": raw_inputs,
        "reason": reason,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await redis.set(dead_key, json.dumps(payload, default=str))
        logger.warning("任务 %s 已入死信队列: %s", job_id, reason)
    except Exception:
        logger.exception("写入死信队列失败 job_id=%s", job_id)


async def run_evaluation_task(
    ctx: Dict[str, Any],
    job_id: str,
    employee_id: str,
    period: str,
    raw_inputs: list,
    tenant_id: str = "default",
    actor_id: str = "system",
) -> None:
    """arq 任务入口:调用 routes._run_evaluation_job 复用评估逻辑

    ctx 是 arq 注入的上下文,含:
    - redis: ArqRedis 连接池(worker 级)
    - job_id / job_try / enqueue_time / score(job 级,arq.run_job 注入)

    死信机制:
    - arq 在 job_try > max_tries 时拒绝任务(不再调用本函数,见 arq.worker.run_job)
    - 因此当 job_try >= max_tries 且本次仍失败时,本函数是最后一次执行
    - 此时写死信队列,运维可从 agentvalue:dead_letter:{job_id} 捞起重投

    P3 修复:AppState 从 on_startup 创建的 ctx["app_state"] 取,不再调
    api.deps.get_app_state(request)(arq worker 是独立进程,无 request 上下文,
    调用会抛 TypeError: get_app_state() missing 1 required positional argument)。
    兼容旧版 worker:on_startup 未设置时现场创建并缓存到 ctx。
    """
    from api.deps import AppState
    from api.routes import _run_evaluation_job

    app_state = ctx.get("app_state")
    if app_state is None:
        # 兼容旧版 worker(未走 on_startup 初始化):现场创建并缓存
        app_state = AppState(get_settings())
        ctx["app_state"] = app_state
    try:
        await _run_evaluation_job(
            job_id=job_id,
            employee_id=employee_id,
            period=period,
            raw_inputs=raw_inputs,
            app_state=app_state,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
    except Exception as e:
        # 判断是否为最终失败:job_try >= max_tries 时本函数不会再被调用
        job_try = ctx.get("job_try", 1)
        max_tries = get_settings().arq_max_tries
        if job_try >= max_tries:
            redis: Optional[ArqRedis] = ctx.get("redis")
            if redis is not None:
                await _write_to_dead_letter(redis, job_id, raw_inputs, repr(e))
            else:
                logger.error(
                    "无 redis 上下文,无法写入死信队列 job_id=%s job_try=%s",
                    job_id,
                    job_try,
                )
        raise  # 继续抛出,让 arq 决定是否重试


async def on_startup(ctx: Dict[str, Any]) -> None:
    """worker 启动钩子:预热资源(连接池 + AppState)

    P3 修复:arq worker 是独立进程,无 FastAPI request 上下文,
    不能复用 api.deps.get_app_state(request) 依赖注入。
    在 on_startup 时创建 AppState 单例存到 ctx,run_evaluation_task 直接从 ctx 取。
    """
    from api.deps import AppState

    settings = get_settings()
    try:
        ctx["app_state"] = AppState(settings)
        logger.info("arq worker 启动,队列: %s, AppState 已初始化", QUEUE_NAME)
    except Exception as e:
        logger.exception("AppState 初始化失败,worker 无法启动: %s", e)
        raise


async def on_shutdown(ctx: Dict[str, Any]) -> None:
    """worker 关闭钩子:释放资源"""
    app_state = ctx.get("app_state")
    if app_state is not None:
        try:
            await app_state.close()
        except Exception:
            logger.debug("关闭 AppState 失败", exc_info=True)
    logger.info("arq worker 关闭")


class WorkerSettings:
    """arq WorkerSettings 配置(arq 0.28+ 不再使用 ActorMeta 元类)。

    启动: `arq core.arq_worker.WorkerSettings`

    arq.cli 通过 import_string 加载本类,arq.worker.create_worker 读取类属性
    构造 Worker 实例(get_kwargs 过滤 __dict__ 中匹配 Worker.__init__ 参数名的字段)。
    """

    functions = [run_evaluation_task]
    on_startup = on_startup
    on_shutdown = on_shutdown
    # 必须是 RedisSettings 实例(arq 0.28 校验类型,不再接受 callable)
    redis_settings = _build_redis_settings()
    queue_name = QUEUE_NAME
    # 单 worker 并发任务数(可按 CPU/IO 调整)
    max_jobs = 10
    # 任务超时(秒):超时 worker 取消并触发重投
    job_timeout = int(get_settings().arq_job_timeout)
    # 重投次数(含首次):job_try > max_tries 时 arq 拒绝任务(不再调用本函数)
    max_tries = get_settings().arq_max_tries
