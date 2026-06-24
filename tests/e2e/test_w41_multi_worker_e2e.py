"""
@file tests/e2e/test_w41_multi_worker_e2e.py
@brief W41 多 worker 部署 e2e 测试

模式: 同进程内 2 个 FastAPI app 实例 + 共享 fakeredis (不同 task_store 实例) 模拟
"2 个 worker 跨进程";通过 httpx.AsyncClient 模拟 HTTP 请求。

验证:
  1. 跨 worker 状态可见 (A.create_task → B.get_task 看到)
  2. 跨 worker SSE 通知 (A.update_task → B.subscribe_task 收到事件)
  3. 跨 worker update_task 状态 (B.update_task → A.get_task 看到)
  4. SSE done 终止流 (更新到 done → SSE 收到 done 后流关闭)
  5. cleanup_expired 幂等 (两 worker 都跑 cleanup, 结果一致)
  6. MemoryTaskStore 路径兼容 (单 worker 行为不破)
  7. app_factory() 工厂模式 (无参 + env 注入返 FastAPI)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from agent_swarm.web import app_factory, create_app
from agent_swarm.web.review_runner import (
    MemoryTaskStore,
    RedisTaskStore,
    create_task_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_fakeredis() -> Any:
    """W41: 共享 fakeredis (模拟 Redis server, 多个 store 实例共享状态)

    @note  decode_responses=True 必须, 跟 RedisTaskStore 的 from_url(decode_responses=True) 对齐
    """
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def task_store_a(shared_fakeredis: Any) -> RedisTaskStore:
    """Worker A 的 task store (连同 shared_fakeredis)"""
    return RedisTaskStore.from_redis_client(shared_fakeredis)


@pytest.fixture
def task_store_b(shared_fakeredis: Any) -> RedisTaskStore:
    """Worker B 的 task store (连同 shared_fakeredis, 模拟跨 worker 共享)"""
    return RedisTaskStore.from_redis_client(shared_fakeredis)


@pytest.fixture
def app_a(task_store_a: RedisTaskStore) -> FastAPI:
    return create_app(task_store=task_store_a, review_mode="full", review_llm="fake")


@pytest.fixture
def app_b(task_store_b: RedisTaskStore) -> FastAPI:
    return create_app(task_store=task_store_b, review_mode="full", review_llm="fake")


# ---------------------------------------------------------------------------
# 直接 task_store 协议层 e2e (不依赖 HTTP, 验证 Redis 跨实例同步)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_worker_state_visible(
    task_store_a: RedisTaskStore,
    task_store_b: RedisTaskStore,
) -> None:
    """A.create_task → B.get_task 看到 (跨实例状态共享)"""
    task = await task_store_a.create_task("main..HEAD", "fake")
    # B 端看到
    seen = await task_store_b.get_task(task.task_id)
    assert seen is not None
    assert seen.task_id == task.task_id
    assert seen.pr_ref == "main..HEAD"
    assert seen.llm_provider == "fake"


@pytest.mark.asyncio
async def test_cross_worker_update_task_propagates(
    task_store_a: RedisTaskStore,
    task_store_b: RedisTaskStore,
) -> None:
    """A.update_task → B.get_task 看到更新 (Redis hash 跨实例共享)"""
    task = await task_store_a.create_task("main..HEAD", "fake")
    await task_store_a.update_task(task.task_id, status="running", progress="50")
    # B 端看到 A 的更新
    seen = await task_store_b.get_task(task.task_id)
    assert seen is not None
    assert seen.status == "running"
    assert seen.progress == 50  # RedisTaskStore.get_task 转 int
    # B 端再更新
    await task_store_b.update_task(task.task_id, progress="75")
    # A 端看到 B 的更新
    seen_a = await task_store_a.get_task(task.task_id)
    assert seen_a is not None
    assert seen_a.progress == 75


@pytest.mark.asyncio
async def test_cross_worker_sse_notification(
    task_store_a: RedisTaskStore,
    task_store_b: RedisTaskStore,
) -> None:
    """A.update_task → B.subscribe_task 收到事件 (Redis pub/sub 跨实例)"""
    task = await task_store_a.create_task("main..HEAD", "fake")
    # B 先订阅
    queue = await task_store_b.subscribe_task(task.task_id)
    assert queue is not None
    # 等订阅注册 (Redis pub/sub 异步)
    await asyncio.sleep(0.05)
    # A 端更新
    await task_store_a.update_task(task.task_id, status="running", progress="25")
    # B 端应该收到事件 (1.5s 内)
    event = await asyncio.wait_for(queue.get(), timeout=1.5)
    assert event.get("type") == "update"
    assert event.get("status") == "running"
    assert event.get("progress") == "25"
    # 再发一次
    await task_store_a.update_task(task.task_id, status="done", progress="100", result={"x": 1})
    done_event = await asyncio.wait_for(queue.get(), timeout=1.5)
    assert done_event.get("status") == "done"
    assert done_event.get("progress") == "100"


@pytest.mark.asyncio
async def test_cross_worker_cleanup_idempotent(
    task_store_a: RedisTaskStore,
    task_store_b: RedisTaskStore,
) -> None:
    """两 worker 各自跑 cleanup_expired, 删各自看到的 expired, 总数等于 expired 数

    @note  task created_at 改为 1 小时前 (TTL=3600s), 否则 fresh task 不会被清理
    """
    import time

    expired_time = time.time() - 4000  # > 3600s TTL
    for _ in range(3):
        task = await task_store_a.create_task("main..HEAD", "fake")
        await task_store_a.update_task(task.task_id, status="done", progress="100")
        # 同时改 hash created_at + sorted set score (cleanup 看 zrangebyscore)
        await task_store_a._redis.hset(  # type: ignore[attr-defined]
            f"task:{task.task_id}", "created_at", str(expired_time)
        )
        await task_store_a._redis.zadd(  # type: ignore[attr-defined]
            "tasks:pending", {task.task_id: expired_time}
        )
    # 两个 worker 都跑 cleanup
    removed_a = await task_store_a.cleanup_expired()
    removed_b = await task_store_b.cleanup_expired()
    # 第二个 worker 应该看到 0 (已被 A 删了)
    assert removed_a == 3
    assert removed_b == 0
    # 再跑一次, 仍然幂等
    assert await task_store_a.cleanup_expired() == 0
    assert await task_store_b.cleanup_expired() == 0


# ---------------------------------------------------------------------------
# 进程内 2 app 实例 + httpx 模拟 2 worker (真实 HTTP path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_status_visible_across_workers(
    app_a: FastAPI,
    app_b: FastAPI,
    task_store_a: RedisTaskStore,
) -> None:
    """store A create task → B GET /api/review/{id} 看到 (HTTP 跨 worker)"""
    transport_a = httpx.ASGITransport(app=app_a)
    transport_b = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport_a, base_url="http://a") as ca:
        async with httpx.AsyncClient(transport=transport_b, base_url="http://b") as cb:
            # A 端直接 create (用 store API 制造 task)
            task = await task_store_a.create_task("main..HEAD", "fake")
            # B 端查状态
            r = await cb.get(f"/api/review/{task.task_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["task_id"] == task.task_id
            assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_http_sse_done_event_terminates(
    app_a: FastAPI,
    app_b: FastAPI,
    task_store_a: RedisTaskStore,
) -> None:
    """B 订阅 SSE → A 更新到 done → B 收到 done 事件 + 流自动关闭"""
    transport_a = httpx.ASGITransport(app=app_a)
    transport_b = httpx.ASGITransport(app=app_b)
    task = await task_store_a.create_task("main..HEAD", "fake")
    sse_done = asyncio.Event()
    sse_events: list[dict[str, Any]] = []

    async def consume_sse() -> None:
        async with httpx.AsyncClient(transport=transport_b, base_url="http://b") as cb:
            async with cb.stream("GET", f"/api/review/{task.task_id}/events") as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    sse_events.append(payload)
                    if payload.get("status") == "done":
                        sse_done.set()
                        return

    consumer = asyncio.create_task(consume_sse())
    # 等消费者启动 + 订阅注册
    await asyncio.sleep(0.2)
    # A 端推进 task
    await task_store_a.update_task(task.task_id, status="running", progress="50")
    await asyncio.sleep(0.1)
    await task_store_a.update_task(task.task_id, status="done", progress="100", result={"ok": True})
    # 等 B 收到 done (timeout 2s)
    await asyncio.wait_for(sse_done.wait(), timeout=2.0)
    await consumer
    # 验证事件序列
    assert any(e.get("status") == "running" for e in sse_events)
    assert sse_events[-1].get("status") == "done"
    assert sse_events[-1].get("result") == {"ok": True}


@pytest.mark.asyncio
async def test_memory_store_path_compatible() -> None:
    """MemoryTaskStore 路径 (单 worker) 行为不破 — W36f 回归"""
    mem_store = MemoryTaskStore()
    app_mem = create_app(task_store=mem_store, review_mode="full", review_llm="fake")
    # 也能正常 create + update + get
    task = await mem_store.create_task("main..HEAD", "fake")
    await mem_store.update_task(task.task_id, status="running", progress="10")
    seen = await mem_store.get_task(task.task_id)
    assert seen is not None
    assert seen.status == "running"
    # HTTP 路径也通
    transport = httpx.ASGITransport(app=app_mem)
    async with httpx.AsyncClient(transport=transport, base_url="http://mem") as c:
        r = await c.get(f"/api/review/{task.task_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "running"


# ---------------------------------------------------------------------------
# app_factory (uvicorn factory 模式) 验证
# ---------------------------------------------------------------------------


def test_app_factory_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_factory() 无参调用, 全 env 缺省 (单进程内存路径, 零破坏)"""
    # 清空相关 env
    for k in [
        "WEB_POSTGRES_DSN", "WEB_POSTGRES_TABLE", "WEB_POSTGRES_TENANT",
        "WEB_CROSS_PROCESS", "WEB_JWT_SECRET", "WEB_JWT_SECRET_REF",
        "WEB_REVIEW_MODE", "WEB_REVIEW_LLM", "WEB_REVIEW_TIMEOUT",
        "WEB_TASK_STORE", "WEB_REDIS_DSN",
        "WEB_WORKTREE_REPO", "WEB_WORKTREE_BASE",
    ]:
        monkeypatch.delenv(k, raising=False)
    app = app_factory()
    assert isinstance(app, FastAPI)
    # 默认: memory task store
    assert isinstance(app.state.task_store, MemoryTaskStore)


def test_app_factory_with_redis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_factory() 读 WEB_TASK_STORE=redis + WEB_REDIS_DSN 注入 RedisTaskStore"""
    monkeypatch.setenv("WEB_TASK_STORE", "redis")
    monkeypatch.setenv("WEB_REDIS_DSN", "redis://localhost:6379/0")
    app = app_factory()
    assert isinstance(app, FastAPI)
    assert isinstance(app.state.task_store, RedisTaskStore)


def test_create_task_store_factory_unknown_backend() -> None:
    """create_task_store 工厂: 未知 backend 抛 ValueError"""
    with pytest.raises(ValueError, match="unknown task store backend"):
        create_task_store("unknown", None)


def test_create_task_store_factory_redis_no_dsn() -> None:
    """create_task_store 工厂: redis 但无 DSN → 降级 MemoryTaskStore (W33b 模式)"""
    store = create_task_store("redis", None)
    assert isinstance(store, MemoryTaskStore)
