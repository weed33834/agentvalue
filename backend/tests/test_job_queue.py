"""
core/job_queue.py 单元测试

覆盖:
- InMemoryJobQueue:enqueue/get/update/list_active/delete/不存在返回 None
- RedisJobQueue:用 fakeredis 验证序列化与 CRUD(不依赖真实 Redis)
- create_job_queue 工厂:无 redis_url 返回 InMemory,有 url 但不可达降级 InMemory
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import core.job_queue as job_queue_module
from core.job_queue import (
    InMemoryJobQueue,
    JobQueue,
    RedisJobQueue,
    create_job_queue,
)


def _job(job_id="job-1", status="pending", employee_id="E1001"):
    return {
        "job_id": job_id,
        "status": status,
        "employee_id": employee_id,
        "period": "2026-W01",
    }


# ---------------- InMemoryJobQueue ----------------


async def test_inmemory_enqueue_and_get():
    q = InMemoryJobQueue()
    job = _job()
    await q.enqueue("job-1", job)
    got = await q.get("job-1")
    # P0 修复: get() 现在返回 deepcopy(与 RedisJobQueue 语义一致),
    # 避免业务"修改后忘记调 update 也能持久化"的隐性 bug。
    # 因此改用值相等而非引用相等。
    assert got == job


async def test_inmemory_get_missing_returns_none():
    q = InMemoryJobQueue()
    assert await q.get("nope") is None


async def test_inmemory_update_merges_and_refreshes_updated_at():
    q = InMemoryJobQueue()
    await q.enqueue("job-1", _job())
    original_updated = (
        (await q.get("job-1"))["updated_at"]
        if "updated_at" in await q.get("job-1")
        else None
    )
    await q.update("job-1", {"status": "completed", "evaluation": {"id": "EV-1"}})
    job = await q.get("job-1")
    assert job["status"] == "completed"
    assert job["evaluation"] == {"id": "EV-1"}
    # 浅合并保留原字段
    assert job["employee_id"] == "E1001"
    assert job["updated_at"]  # update 必定写入 updated_at


async def test_inmemory_update_missing_job_is_noop():
    q = InMemoryJobQueue()
    # 不存在不应抛异常
    await q.update("nope", {"status": "completed"})
    assert await q.get("nope") is None


async def test_inmemory_list_active_filters_terminal_status():
    q = InMemoryJobQueue()
    await q.enqueue("j1", _job("j1", status="pending"))
    await q.enqueue("j2", _job("j2", status="running"))
    await q.enqueue("j3", _job("j3", status="completed"))
    await q.enqueue("j4", _job("j4", status="failed"))

    active = await q.list_active()
    active_ids = {j["job_id"] for j in active}
    assert active_ids == {"j1", "j2"}


async def test_inmemory_delete():
    q = InMemoryJobQueue()
    await q.enqueue("job-1", _job())
    await q.delete("job-1")
    assert await q.get("job-1") is None
    # 重复 delete 不报错
    await q.delete("job-1")


async def test_inmemory_clear():
    q = InMemoryJobQueue()
    await q.enqueue("j1", _job("j1"))
    await q.enqueue("j2", _job("j2"))
    await q.clear()
    assert await q.list_active() == []
    assert await q.get("j1") is None


# ---------------- RedisJobQueue (fakeredis) ----------------


def _make_fake_redis_queue(monkeypatch):
    """构造一个使用 fakeredis 的 RedisJobQueue,patch 掉 from_url 避免真实连接"""
    import fakeredis.aioredis

    fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def fake_from_url(url, **kwargs):
        return fake_client

    monkeypatch.setattr(job_queue_module.redis_asyncio, "from_url", fake_from_url)
    return RedisJobQueue("redis://localhost:6379/0")


async def test_redis_enqueue_and_get(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("job-1", _job())
    got = await q.get("job-1")
    assert got is not None
    assert got["job_id"] == "job-1"
    assert got["status"] == "pending"


async def test_redis_get_missing_returns_none(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    assert await q.get("nope") is None


async def test_redis_update_merges(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("job-1", _job())
    await q.update("job-1", {"status": "completed"})
    job = await q.get("job-1")
    assert job["status"] == "completed"
    assert job["employee_id"] == "E1001"  # 原字段保留
    assert job["updated_at"]


async def test_redis_list_active(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("j1", _job("j1", status="pending"))
    await q.enqueue("j2", _job("j2", status="completed"))
    active = await q.list_active()
    assert {j["job_id"] for j in active} == {"j1"}


async def test_redis_delete(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("job-1", _job())
    await q.delete("job-1")
    assert await q.get("job-1") is None


async def test_redis_clear(monkeypatch):
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("j1", _job("j1"))
    await q.enqueue("j2", _job("j2"))
    await q.clear()
    assert await q.list_active() == []


async def test_redis_key_prefix(monkeypatch):
    """确认 key 带前缀 agentvalue:job:"""
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("job-1", _job())
    # 直接读底层数据,验证 key 命名
    keys = await q._client.keys("agentvalue:job:*")
    assert keys == ["agentvalue:job:job-1"]


async def test_redis_operation_failure_does_not_raise(monkeypatch):
    """Redis 异常时只记日志不抛,业务不中断"""
    q = _make_fake_redis_queue(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(q._client, "set", boom)
    monkeypatch.setattr(q._client, "get", boom)
    monkeypatch.setattr(q._client, "keys", boom)
    # 这些调用都不应抛
    await q.enqueue("x", _job())
    assert await q.get("x") is None
    assert await q.list_active() == []


# ---------------- create_job_queue 工厂 ----------------


def test_factory_no_redis_url_returns_inmemory():
    settings = SimpleNamespace(redis_url=None)
    q = create_job_queue(settings)
    assert isinstance(q, InMemoryJobQueue)


def test_factory_unreachable_redis_falls_back_to_inmemory(monkeypatch):
    """有 redis_url 但连不通应降级到内存,不能崩"""
    settings = SimpleNamespace(redis_url="redis://nonexistent-host:6379/0")

    async def fake_ping(self):
        raise RuntimeError("unreachable")

    # 同步探测走 redis.from_url(...).ping();patch 成抛异常即可模拟不可达
    class _DeadClient:
        def ping(self):
            raise RuntimeError("unreachable")

        def close(self):
            pass

    monkeypatch.setattr(
        job_queue_module.redis_sync, "from_url", lambda *a, **kw: _DeadClient()
    )
    q = create_job_queue(settings)
    assert isinstance(q, InMemoryJobQueue)


def test_factory_reachable_redis_returns_redis_queue(monkeypatch):
    settings = SimpleNamespace(redis_url="redis://localhost:6379/0")

    class _OkClient:
        def ping(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(
        job_queue_module.redis_sync, "from_url", lambda *a, **kw: _OkClient()
    )
    q = create_job_queue(settings)
    assert isinstance(q, RedisJobQueue)


def test_job_queue_is_abstract():
    with pytest.raises(TypeError):
        JobQueue()  # type: ignore[abstract]


# ---------------- H2：set_active_jobs 埋点 ----------------


async def test_inmemory_enqueue_updates_active_jobs_gauge(monkeypatch):
    """enqueue 后应刷新活跃任务数 Gauge"""
    captured = []
    from core import job_queue as jq_mod

    def fake_report(count):
        captured.append(count)

    monkeypatch.setattr(jq_mod, "_report_active_jobs", fake_report)
    q = InMemoryJobQueue()
    await q.enqueue("j1", _job("j1", status="pending"))
    assert captured[-1] == 1
    await q.enqueue("j2", _job("j2", status="running"))
    assert captured[-1] == 2


async def test_inmemory_update_decreases_active_jobs_gauge(monkeypatch):
    """任务转入终态后活跃数应下降"""
    captured = []
    from core import job_queue as jq_mod

    monkeypatch.setattr(jq_mod, "_report_active_jobs", lambda n: captured.append(n))
    q = InMemoryJobQueue()
    await q.enqueue("j1", _job("j1", status="pending"))
    await q.enqueue("j2", _job("j2", status="running"))
    # j1 完成后活跃数应降为 1
    await q.update("j1", {"status": "completed"})
    assert captured[-1] == 1


async def test_inmemory_clear_resets_active_jobs_gauge(monkeypatch):
    captured = []
    from core import job_queue as jq_mod

    monkeypatch.setattr(jq_mod, "_report_active_jobs", lambda n: captured.append(n))
    q = InMemoryJobQueue()
    await q.enqueue("j1", _job("j1", status="pending"))
    await q.clear()
    assert captured[-1] == 0


async def test_redis_enqueue_updates_active_jobs_gauge(monkeypatch):
    """Redis 实现同样应在 enqueue 后刷新 Gauge"""
    captured = []
    from core import job_queue as jq_mod

    monkeypatch.setattr(jq_mod, "_report_active_jobs", lambda n: captured.append(n))
    q = _make_fake_redis_queue(monkeypatch)
    await q.enqueue("j1", _job("j1", status="pending"))
    assert captured[-1] == 1
