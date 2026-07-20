"""
异步评估任务队列抽象

历史背景:routes.py 原先用模块级 Dict 存 job 状态,导致只能单实例运行。
本模块抽取出 JobQueue 接口,提供 InMemory(测试/本地)与 Redis(多实例生产)两套实现,
解除单实例约束。create_job_queue 按 settings.redis_url 自动选择,Ruby 不可达时降级内存。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 模块级暴露 redis 客户端入口,便于测试 monkeypatch;
# 真正 import 失败(未装 redis)时降级为 None,工厂会回退到 InMemoryJobQueue。
try:
    import redis as redis_sync  # noqa: F401
    import redis.asyncio as redis_asyncio  # noqa: F401
except ImportError:  # pragma: no cover - redis 在 requirements 中,仅兜底
    redis_sync = None  # type: ignore[assignment]
    redis_asyncio = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 活跃任务状态集合：pending/running 计入活跃存量
_ACTIVE_STATUSES = ("pending", "running")


def _report_active_jobs(count: int) -> None:
    """H2：刷新活跃任务数 Gauge。埋点失败不阻断队列主流程。"""
    try:
        from core.metrics import set_active_jobs

        set_active_jobs(count)
    except Exception:
        logger.exception("set_active_jobs 埋点失败")


class JobQueue(ABC):
    """任务队列抽象:语义对齐原 job_store 的 Dict 行为(get 返回 None 表示不存在)"""

    @abstractmethod
    async def enqueue(self, job_id: str, job_info: Dict[str, Any]) -> None:
        """整体写入一条任务(等同 job_store[job_id] = job_info)"""

    @abstractmethod
    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """读取任务,不存在返回 None"""

    @abstractmethod
    async def update(self, job_id: str, job_info: Dict[str, Any]) -> None:
        """浅合并更新任务字段并刷新 updated_at(对齐原 _update_job 行为)"""

    @abstractmethod
    async def list_active(self) -> List[Dict[str, Any]]:
        """列出未完结(pending/running)任务,供运维巡检"""

    @abstractmethod
    async def delete(self, job_id: str) -> None:
        """删除任务"""

    @abstractmethod
    async def clear(self) -> None:
        """清空全部任务(测试间状态隔离用)"""


class InMemoryJobQueue(JobQueue):
    """内存实现:行为对齐原模块级 job_store Dict(仅 get 改为返回拷贝)。

    P0 修复: 原 get() 返回引用,与 RedisJobQueue 返回拷贝语义不一致。
    业务代码切换实现时会出现 "Redis 下并发 update 丢更新" 的隐性 bug。
    现在统一返回深拷贝,强制业务通过 update() 显式持久化修改。
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    def _sync_active_gauge(self) -> None:
        """统计当前活跃任务并刷新 Gauge"""
        count = sum(
            1 for j in self._store.values() if j.get("status") in _ACTIVE_STATUSES
        )
        _report_active_jobs(count)

    async def enqueue(self, job_id: str, job_info: Dict[str, Any]) -> None:
        self._store[job_id] = job_info
        self._sync_active_gauge()

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        # P0 修复: 返回深拷贝,与 RedisJobQueue 语义一致。
        # 原返回引用会让"修改后忘记调 update 也能持久化"成为隐性 bug,
        # 切换到 Redis 后会丢失更新。
        import copy

        job = self._store.get(job_id)
        return copy.deepcopy(job) if job else None

    async def update(self, job_id: str, job_info: Dict[str, Any]) -> None:
        job = self._store.get(job_id)
        if not job:
            return
        job.update(job_info)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._sync_active_gauge()

    async def list_active(self) -> List[Dict[str, Any]]:
        import copy

        return [
            copy.deepcopy(j)
            for j in self._store.values()
            if j.get("status") in ("pending", "running")
        ]

    async def delete(self, job_id: str) -> None:
        self._store.pop(job_id, None)
        self._sync_active_gauge()

    async def clear(self) -> None:
        self._store.clear()
        self._sync_active_gauge()


class RedisJobQueue(JobQueue):
    """Redis 实现:多实例共享任务状态,key 前缀 agentvalue:job:

    所有操作包 try/except:Redis 故障时仅记日志不抛异常,避免拖垮评估主流程
    (任务状态查询失败远比阻断评估可接受)。

    P0 修复: update() 改用 Lua 脚本原子"读-改-写",消除原"GET→JSON 解析→
    Python dict.update→SET"链路的并发丢更新竞态(参考 LiteLLM Redis 实践)。
    """

    KEY_PREFIX = "agentvalue:job:"

    # Lua 脚本: 原子读-改-写 update
    # KEYS[1] = job key
    # ARGV[1] = 新字段 JSON
    # ARGV[2] = updated_at 时间戳(ISO 字符串)
    # 返回: 1 成功 / 0 key 不存在
    _UPDATE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local obj = cjson.decode(cur)
local patch = cjson.decode(ARGV[1])
for k, v in pairs(patch) do obj[k] = v end
obj['updated_at'] = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(obj))
return 1
"""

    def __init__(self, redis_url: str) -> None:
        self._client = redis_asyncio.from_url(redis_url, decode_responses=True)
        # 预注册 Lua 脚本,后续 update 调用走 evalsha 减少网络往返
        self._update_script = self._client.register_script(self._UPDATE_LUA)

    def _key(self, job_id: str) -> str:
        return f"{self.KEY_PREFIX}{job_id}"

    async def _sync_active_gauge(self) -> None:
        """从 Redis 统计活跃任务并刷新 Gauge；失败仅记日志"""
        try:
            count = len(await self.list_active())
            _report_active_jobs(count)
        except Exception:
            logger.exception("Redis 同步活跃任务数失败")

    async def enqueue(self, job_id: str, job_info: Dict[str, Any]) -> None:
        try:
            await self._client.set(self._key(job_id), json.dumps(job_info, default=str))
        except Exception as e:
            logger.warning("Redis enqueue 失败 job_id=%s: %s", job_id, e)
        await self._sync_active_gauge()

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            raw = await self._client.get(self._key(job_id))
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("Redis get 失败 job_id=%s: %s", job_id, e)
            return None

    async def update(self, job_id: str, job_info: Dict[str, Any]) -> None:
        """P0 修复: 原子 update via Lua 脚本,消除并发丢更新。

        原 Python 实现"GET → json.loads → dict.update → SET"链路在并发 update
        时会丢字段(两个并发 update 各自读老值、各自写新值,后写覆盖先写)。
        Lua 脚本在 Redis 单线程内原子执行,无竞态。

        redis-py AsyncScript 调用方式:script(keys=[...], args=[...])(直接调用,
        非 script.eval — AsyncScript 类只有 __call__,没有 eval 方法,
        早期代码误用 .eval() 会抛 AttributeError 被 try/except 静默吞掉)
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            patch_json = json.dumps(job_info, default=str)
            # AsyncScript 通过 __call__ 触发 evalsha(失败时自动 fallback eval)
            await self._update_script(
                keys=[self._key(job_id)],
                args=[patch_json, now],
            )
        except Exception as e:
            logger.warning("Redis update 失败 job_id=%s: %s", job_id, e)
        await self._sync_active_gauge()

    async def list_active(self) -> List[Dict[str, Any]]:
        try:
            # SCAN 非阻塞游标迭代,避免 KEYS 在大量 key 时阻塞 Redis
            jobs: List[Dict[str, Any]] = []
            async for k in self._client.scan_iter(f"{self.KEY_PREFIX}*"):
                raw = await self._client.get(k)
                if not raw:
                    continue
                job = json.loads(raw)
                if job.get("status") in ("pending", "running"):
                    jobs.append(job)
            return jobs
        except Exception as e:
            logger.warning("Redis list_active 失败: %s", e)
            return []

    async def delete(self, job_id: str) -> None:
        try:
            await self._client.delete(self._key(job_id))
        except Exception as e:
            logger.warning("Redis delete 失败 job_id=%s: %s", job_id, e)
        await self._sync_active_gauge()

    async def clear(self) -> None:
        try:
            keys = [k async for k in self._client.scan_iter(f"{self.KEY_PREFIX}*")]
            if keys:
                await self._client.delete(*keys)
        except Exception as e:
            logger.warning("Redis clear 失败: %s", e)
        await self._sync_active_gauge()


def _can_connect_sync(redis_url: str, timeout: float = 1.0) -> bool:
    """同步探测 Redis 可达性。工厂在模块导入期被调用,此时无事件循环,
    用同步 client 做一次 ping 即可,失败时由调用方降级到内存队列。"""
    if redis_sync is None:
        return False
    try:
        client = redis_sync.from_url(
            redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
        )
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def create_job_queue(settings: Any) -> JobQueue:
    """按 settings 选择实现:

    1. use_arq_queue=True + redis_url 可达 → ArqJobQueue(独立 worker + 重投 + 死信)
    2. redis_url 可达 → RedisJobQueue(裸 redis.asyncio 共享存储,无 worker)
    3. 以上都失败 → InMemoryJobQueue(单实例,测试与本地开发默认)

    降级而非崩溃是关键:本地开发或 CI 无 Redis 时也能正常启动。

    P3 规模化就绪:启用 arq 时需另行启动 worker:
        arq core.arq_worker.WorkerSettings
    """
    redis_url = getattr(settings, "redis_url", None)
    if not redis_url:
        return InMemoryJobQueue()

    if not _can_connect_sync(redis_url):
        logger.warning("Redis 不可达,降级使用 InMemoryJobQueue: %s", redis_url)
        return InMemoryJobQueue()

    # P3:启用 arq 时优先 ArqJobQueue(独立 worker + 自动重投 + 死信队列)
    if getattr(settings, "use_arq_queue", False):
        try:
            from core.arq_job_queue import ArqJobQueue

            logger.info("任务队列使用 ArqJobQueue(独立 worker): %s", redis_url)
            return ArqJobQueue(redis_url)
        except ImportError:
            logger.warning("arq 未安装,降级使用 RedisJobQueue")
        except Exception as e:
            logger.warning("ArqJobQueue 初始化失败,降级使用 RedisJobQueue: %s", e)

    logger.info("任务队列使用 RedisJobQueue: %s", redis_url)
    return RedisJobQueue(redis_url)
