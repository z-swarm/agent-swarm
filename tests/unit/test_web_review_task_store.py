"""
@module tests.unit.test_web_review_task_store
@brief  P5-W40 TaskStore 单测 (≥10 cases)

覆盖:
  - MemoryTaskStore CRUD (5 case)
  - RedisTaskStore CRUD (5 case, 用 fakeredis)
  - create_task_store 工厂降级零破坏 (1 case)
  - 跨 "worker" 任务同步 (1 case, fakeredis 共享)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_swarm.web.review_runner import (
    MemoryTaskStore,
    RedisTaskStore,
    create_task_store,
)


# ---------------------------------------------------------------------------
# 1. MemoryTaskStore
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_memory_store():
    """清空 MemoryTaskStore 全局状态"""
    from agent_swarm.web import review_runner

    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()
    yield
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()


@pytest.mark.asyncio
async def test_memory_store_create_and_get() -> None:
    """MemoryTaskStore: create + get"""
    store = MemoryTaskStore()
    task = await store.create_task("main..HEAD", "fake")
    assert task.task_id
    got = await store.get_task(task.task_id)
    assert got is task


@pytest.mark.asyncio
async def test_memory_store_get_not_found() -> None:
    """MemoryTaskStore: 不存在 task_id 返 None"""
    store = MemoryTaskStore()
    assert await store.get_task("nonexistent") is None


@pytest.mark.asyncio
async def test_memory_store_update() -> None:
    """MemoryTaskStore: update_task 改字段"""
    store = MemoryTaskStore()
    task = await store.create_task("main..HEAD", "fake")
    await store.update_task(task.task_id, status="running", progress=50)
    got = await store.get_task(task.task_id)
    assert got.status == "running"
    assert got.progress == 50


@pytest.mark.asyncio
async def test_memory_store_subscribe() -> None:
    """MemoryTaskStore: subscribe 返 queue, update 推事件"""
    store = MemoryTaskStore()
    task = await store.create_task("main..HEAD", "fake")
    q = await store.subscribe_task(task.task_id)
    assert q is not None
    await store.update_task(task.task_id, status="running", progress=30)
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event["status"] == "running"
    assert event["progress"] == 30


@pytest.mark.asyncio
async def test_memory_store_cleanup_expired() -> None:
    """MemoryTaskStore: cleanup 清理 done/error 超过 TTL"""
    store = MemoryTaskStore()
    task = await store.create_task("main..HEAD", "fake")
    await store.update_task(task.task_id, status="done")
    # 改 created_at 到过去
    task.created_at = 0  # very old
    removed = await store.cleanup_expired()
    assert removed == 1
    assert await store.get_task(task.task_id) is None


# ---------------------------------------------------------------------------
# 2. RedisTaskStore (fakeredis)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    """fakeredis 异步客户端"""
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_redis_store_create_and_get(fake_redis) -> None:
    """RedisTaskStore: create + get (fakeredis)"""
    store = RedisTaskStore.__new__(RedisTaskStore)
    store._redis = fake_redis
    task = await store.create_task("main..HEAD", "fake")
    assert task.task_id
    got = await store.get_task(task.task_id)
    assert got is not None
    assert got.task_id == task.task_id
    assert got.pr_ref == "main..HEAD"


@pytest.mark.asyncio
async def test_redis_store_get_not_found(fake_redis) -> None:
    """RedisTaskStore: 不存在 task_id 返 None"""
    store = RedisTaskStore.__new__(RedisTaskStore)
    store._redis = fake_redis
    assert await store.get_task("nonexistent") is None


@pytest.mark.asyncio
async def test_redis_store_update(fake_redis) -> None:
    """RedisTaskStore: update_task 改字段"""
    store = RedisTaskStore.__new__(RedisTaskStore)
    store._redis = fake_redis
    task = await store.create_task("main..HEAD", "fake")
    await store.update_task(task.task_id, status="running", progress=75)
    got = await store.get_task(task.task_id)
    assert got.status == "running"
    assert got.progress == 75


@pytest.mark.asyncio
async def test_redis_store_subscribe_publish(fake_redis) -> None:
    """RedisTaskStore: subscribe 收 pub/sub 事件"""
    store = RedisTaskStore.__new__(RedisTaskStore)
    store._redis = fake_redis
    task = await store.create_task("main..HEAD", "fake")
    q = await store.subscribe_task(task.task_id)
    assert q is not None
    # 推一个事件
    await store.update_task(task.task_id, status="done", progress=100)
    # 等事件到达
    event = await asyncio.wait_for(q.get(), timeout=2.0)
    assert event["status"] == "done"


@pytest.mark.asyncio
async def test_redis_store_cleanup_expired(fake_redis) -> None:
    """RedisTaskStore: cleanup 清理 done 超过 TTL"""
    store = RedisTaskStore.__new__(RedisTaskStore)
    store._redis = fake_redis
    task = await store.create_task("main..HEAD", "fake")
    await store.update_task(task.task_id, status="done")
    # 改 created_at 字段 + zset score
    await fake_redis.hset(f"task:{task.task_id}", "created_at", "0")
    await fake_redis.zadd("tasks:pending", {task.task_id: 0})
    removed = await store.cleanup_expired()
    assert removed == 1
    assert await store.get_task(task.task_id) is None


# ---------------------------------------------------------------------------
# 3. create_task_store 工厂
# ---------------------------------------------------------------------------


def test_create_task_store_memory() -> None:
    """factory: memory → MemoryTaskStore"""
    store = create_task_store("memory")
    assert isinstance(store, MemoryTaskStore)


def test_create_task_store_redis_no_dsn_fallback() -> None:
    """factory: redis + 无 DSN → 降级 MemoryTaskStore (W33b 模式)"""
    store = create_task_store("redis", redis_dsn=None)
    assert isinstance(store, MemoryTaskStore)


def test_create_task_store_unknown_raises() -> None:
    """factory: 未知 backend → ValueError"""
    with pytest.raises(ValueError, match="unknown task store backend"):
        create_task_store("mongodb")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. 跨 "worker" 任务同步 (fakeredis 共享)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_store_cross_worker_task_sync(fake_redis) -> None:
    """2 个 RedisTaskStore 共享 fakeredis: worker 1 写 → worker 2 读"""
    # worker 1 创建 task
    worker1 = RedisTaskStore.__new__(RedisTaskStore)
    worker1._redis = fake_redis
    task = await worker1.create_task("main..HEAD", "fake")
    # worker 2 查 task (不同实例, 同 fakeredis)
    worker2 = RedisTaskStore.__new__(RedisTaskStore)
    worker2._redis = fake_redis
    got = await worker2.get_task(task.task_id)
    assert got is not None
    assert got.task_id == task.task_id
    # worker 1 update → worker 2 查
    await worker1.update_task(task.task_id, status="done", progress=100)
    got2 = await worker2.get_task(task.task_id)
    assert got2.status == "done"
    assert got2.progress == 100
