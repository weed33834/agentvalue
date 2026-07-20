"""
ArqJobQueue:arq 任务队列适配器(P3 规模化就绪,H2)

设计:
- 实现 JobQueue 接口,与 RedisJobQueue 兼容(状态查询走同一 Redis key)
- enqueue 时:
  1. 写 RedisJobQueue 状态(status=pending,供前端查询)
  2. enqueue 到 arq 队列(异步执行,自动重投+死信)
- get/list_active/delete 走 RedisJobQueue(状态查询)
- update 走 RedisJobQueue(update 时由 worker 进程调用)
- clear 仅清 RedisJobQueue,不影响 arq 队列(避免误清运行中任务)

降级:未启用 use_arq_queue 时,create_job_queue 返回 RedisJobQueue(现有行为)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.job_queue import JobQueue, RedisJobQueue, _report_active_jobs

logger = logging.getLogger(__name__)


class ArqJobQueue(JobQueue):
    """arq 任务队列适配器:enqueue 入 arq,状态走 RedisJobQueue

    与 RedisJobQueue 共享 Redis key 前缀(agentvalue:job:),状态查询接口完全兼容。
    新增能力:enqueue 时入 arq 队列,worker 进程独立消费,失败自动重投+死信。
    """

    def __init__(self, redis_url: str, arq_redis: Any = None) -> None:
        # 复用 RedisJobQueue 的状态查询能力
        self._state_queue = RedisJobQueue(redis_url)
        # arq 客户端(延迟初始化,避免模块导入期连 Redis)
        self._arq_redis = arq_redis
        self._redis_url = redis_url

    async def _get_arq_redis(self) -> Any:
        """延迟获取 arq redis 客户端(模块导入期不连 Redis)"""
        if self._arq_redis is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            settings = RedisSettings.from_dsn(self._redis_url)
            self._arq_redis = await create_pool(settings)
        return self._arq_redis

    async def enqueue(self, job_id: str, job_info: Dict[str, Any]) -> None:
        """入队:1) 写 Redis 状态 2) enqueue 到 arq 队列"""
        # 1. 写状态(供前端 /evaluations/jobs/{id} 查询)
        await self._state_queue.enqueue(job_id, job_info)

        # 2. 入 arq 队列(异步执行)
        try:
            arq_redis = await self._get_arq_redis()
            # 从 job_info 提取 arq 任务所需参数
            from core.arq_worker import QUEUE_NAME, run_evaluation_task

            await arq_redis.enqueue_job(
                function=run_evaluation_task.__name__,
                args=(
                    job_id,
                    job_info.get("employee_id", ""),
                    job_info.get("period", ""),
                    job_info.get("raw_inputs", []),
                    job_info.get("tenant_id", "default"),
                    job_info.get("actor_id", "system"),
                ),
                _queue_name=QUEUE_NAME,
                # job_id 用业务 job_id,便于 arq 去重 + 运维追踪
                _job_id=job_id,
            )
            logger.info("arq enqueue 成功 job_id=%s", job_id)
        except Exception as e:
            logger.warning(
                "arq enqueue 失败 job_id=%s(任务状态已写 Redis,可手动捞取): %s",
                job_id,
                e,
            )

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """状态查询走 RedisJobQueue"""
        return await self._state_queue.get(job_id)

    async def update(self, job_id: str, job_info: Dict[str, Any]) -> None:
        """worker 进程更新状态:走 RedisJobQueue(Lua 原子 update)"""
        await self._state_queue.update(job_id, job_info)

    async def list_active(self) -> List[Dict[str, Any]]:
        """活跃任务列表:走 RedisJobQueue"""
        return await self._state_queue.list_active()

    async def delete(self, job_id: str) -> None:
        """删除任务状态(不影响 arq 队列中的待执行任务)"""
        await self._state_queue.delete(job_id)

    async def clear(self) -> None:
        """清空任务状态(不影响 arq 队列,避免误清运行中任务)"""
        await self._state_queue.clear()
