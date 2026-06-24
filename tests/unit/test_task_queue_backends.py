"""
@module tests.unit.test_task_queue_backends
@brief  W18 TaskQueueBackend (Memory + Redis/fakeredis) 单元测试

覆盖:
  - StoredTask 序列化/反序列化
  - MemoryBackend: put/get/list/cas/stats
  - RedisBackend (用 fakeredis 跑): 同样覆盖
  - CAS 版本冲突语义
  - 多后端行为一致 (Memory vs Redis CAS 语义)
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.core.backends.memory import MemoryBackend
from agent_swarm.core.task_queue_backend import (
    StoredTask,
    VersionMismatchError,
)


def _mk_task(
    tid: str = "t1",
    version: int = 0,
    status: str = "pending",
) -> StoredTask:
    now = time.time()
    return StoredTask(
        id=tid,
        title=f"task-{tid}",
        description="",
        status=status,
        version=version,
        assigned_to=None,
        depends_on=[],
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# StoredTask
# ---------------------------------------------------------------------------


def test_stored_task_roundtrip() -> None:
    t = _mk_task(version=3, status="in_progress")
    t.assigned_to = "agent-1"
    t.depends_on = ["t0"]
    t.result = {"k": "v"}
    d = t.to_dict()
    t2 = StoredTask.from_dict(d)
    assert t2.id == t.id
    assert t2.version == t.version
    assert t2.assigned_to == "agent-1"
    assert t2.depends_on == ["t0"]
    assert t2.result == {"k": "v"}


def test_stored_task_from_dict_defaults() -> None:
    t = StoredTask.from_dict({"id": "x", "title": "T", "status": "pending"})
    assert t.version == 0
    assert t.depends_on == []
    assert t.result is None
    assert t.description == ""


# ---------------------------------------------------------------------------
# MemoryBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_put_get_list() -> None:
    b = MemoryBackend()
    t1 = _mk_task("t1")
    t2 = _mk_task("t2")
    await b.put(t1)
    await b.put(t2)
    g1 = await b.get("t1")
    assert g1 is not None and g1.id == "t1"
    all_tasks = await b.list_all()
    assert sorted(t.id for t in all_tasks) == ["t1", "t2"]


@pytest.mark.asyncio
async def test_memory_put_duplicate_raises() -> None:
    b = MemoryBackend()
    await b.put(_mk_task("t1"))
    with pytest.raises(ValueError, match="already exists"):
        await b.put(_mk_task("t1"))


@pytest.mark.asyncio
async def test_memory_cas_success() -> None:
    b = MemoryBackend()
    await b.put(_mk_task("t1", version=0))

    def mut(t: StoredTask) -> StoredTask:
        t.status = "in_progress"
        t.version += 1
        t.assigned_to = "agent-1"
        return t

    new = await b.compare_and_set("t1", 0, mut)
    assert new.version == 1
    assert new.status == "in_progress"
    assert new.assigned_to == "agent-1"


@pytest.mark.asyncio
async def test_memory_cas_version_mismatch() -> None:
    b = MemoryBackend()
    await b.put(_mk_task("t1", version=2))

    def mut(t: StoredTask) -> StoredTask:
        t.version += 1
        return t

    with pytest.raises(VersionMismatchError) as ei:
        await b.compare_and_set("t1", 0, mut)
    assert ei.value.expected == 0
    assert ei.value.actual == 2


@pytest.mark.asyncio
async def test_memory_cas_key_not_found() -> None:
    b = MemoryBackend()

    def mut(t: StoredTask) -> StoredTask:
        return t

    with pytest.raises(KeyError):
        await b.compare_and_set("ghost", 0, mut)


@pytest.mark.asyncio
async def test_memory_cas_must_bump_version_by_one() -> None:
    b = MemoryBackend()
    await b.put(_mk_task("t1", version=0))

    def bad_mut(t: StoredTask) -> StoredTask:
        t.version += 2  # 非法: +2
        return t

    with pytest.raises(ValueError, match="must bump version by 1"):
        await b.compare_and_set("t1", 0, bad_mut)


@pytest.mark.asyncio
async def test_memory_stats() -> None:
    b = MemoryBackend()
    await b.put(_mk_task("t1", status="pending"))
    await b.put(_mk_task("t2", status="completed"))
    await b.put(_mk_task("t3", status="completed"))
    await b.put(_mk_task("t4", status="failed"))
    s = await b.stats()
    assert s == {"pending": 1, "completed": 2, "failed": 1}


# ---------------------------------------------------------------------------
# RedisBackend (用 fakeredis 跑)
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_backend(request):  # type: ignore[no-untyped-def]
    pytest.importorskip("fakeredis", reason="fakeredis not installed")
    from agent_swarm.core.backends.redis_backend import (
        RedisBackend,
        RedisConfig,
    )

    # 用 test name + 时间戳做 namespace, 隔离 fakeredis 全局状态
    cfg = RedisConfig(
        namespace=f"test-{request.node.name}-{time.time_ns()}",
        use_fakeredis=True,
    )
    b = RedisBackend(cfg)
    yield b
    await b.close()


@pytest.mark.asyncio
async def test_redis_put_get_list(redis_backend) -> None:  # type: ignore[no-untyped-def]
    await redis_backend.put(_mk_task("t1"))
    await redis_backend.put(_mk_task("t2"))
    g = await redis_backend.get("t1")
    assert g is not None and g.id == "t1"
    all_tasks = await redis_backend.list_all()
    assert sorted(t.id for t in all_tasks) == ["t1", "t2"]


@pytest.mark.asyncio
async def test_redis_cas_success(redis_backend) -> None:  # type: ignore[no-untyped-def]
    await redis_backend.put(_mk_task("t1", version=0))

    def mut(t: StoredTask) -> StoredTask:
        t.status = "completed"
        t.version += 1
        t.result = "ok"
        return t

    new = await redis_backend.compare_and_set("t1", 0, mut)
    assert new.version == 1
    assert new.status == "completed"


@pytest.mark.asyncio
async def test_redis_cas_version_mismatch(redis_backend) -> None:  # type: ignore[no-untyped-def]
    await redis_backend.put(_mk_task("t1", version=3))

    def mut(t: StoredTask) -> StoredTask:
        t.version += 1
        return t

    with pytest.raises(VersionMismatchError) as ei:
        await redis_backend.compare_and_set("t1", 0, mut)
    assert ei.value.actual == 3


@pytest.mark.asyncio
async def test_redis_cas_key_not_found(redis_backend) -> None:  # type: ignore[no-untyped-def]
    def mut(t: StoredTask) -> StoredTask:
        return t

    with pytest.raises(KeyError):
        await redis_backend.compare_and_set("ghost", 0, mut)


@pytest.mark.asyncio
async def test_redis_concurrent_cas_only_one_wins(redis_backend) -> None:  # type: ignore[no-untyped-def]
    """并发 50 个 claim 同 task_id, 只能 1 个成功——Redis WATCH/MULTI/EXEC 验证"""
    import asyncio

    await redis_backend.put(_mk_task("t1", version=0))

    def mut(t: StoredTask) -> StoredTask:
        t.status = "in_progress"
        t.version += 1
        t.assigned_to = "agent-1"
        return t

    async def attempt() -> str:
        try:
            await redis_backend.compare_and_set("t1", 0, mut)
            return "ok"
        except VersionMismatchError:
            return "conflict"

    results = await asyncio.gather(*(attempt() for _ in range(50)))
    ok_count = results.count("ok")
    conflict_count = results.count("conflict")
    # Redis 单线程串行化保证: 仅 1 个 OK, 49 个 conflict
    # 注: fakeredis 是单线程, 真实 Redis 也是单线程命令执行, 行为一致
    assert ok_count == 1, f"expected 1 ok, got {ok_count}"
    assert conflict_count == 49, f"expected 49 conflict, got {conflict_count}"


@pytest.mark.asyncio
async def test_redis_namespace_isolation() -> None:
    """不同 namespace 不互相污染"""
    pytest.importorskip("fakeredis")
    from agent_swarm.core.backends.redis_backend import (
        RedisBackend,
        RedisConfig,
    )

    a = RedisBackend(RedisConfig(namespace="ns-a", use_fakeredis=True))
    b = RedisBackend(RedisConfig(namespace="ns-b", use_fakeredis=True))
    try:
        await a.put(_mk_task("t1"))
        # b 的 namespace 没数据
        assert await b.get("t1") is None
        await b.put(_mk_task("t1", version=5))
        # a 仍是 version=0
        ta = await a.get("t1")
        assert ta is not None and ta.version == 0
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_redis_stats(redis_backend) -> None:  # type: ignore[no-untyped-def]
    await redis_backend.put(_mk_task("t1", status="pending"))
    await redis_backend.put(_mk_task("t2", status="completed"))
    await redis_backend.put(_mk_task("t3", status="completed"))
    s = await redis_backend.stats()
    assert s == {"pending": 1, "completed": 2}


# ---------------------------------------------------------------------------
# 行为等价 (Memory vs Redis CAS 语义一致)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_vs_redis_cas_semantics_match() -> None:
    pytest.importorskip("fakeredis")
    from agent_swarm.core.backends.redis_backend import (
        RedisBackend,
        RedisConfig,
    )

    async def run_cas_scenario(backend) -> dict:  # type: ignore[no-untyped-def]
        # 重置
        for _t in await backend.list_all():
            pass  # noqa
        # 用一个独立的 task_id 避免污染
        tid = f"scenario-{id(backend)}-{time.time_ns()}"
        await backend.put(_mk_task(tid, version=0))

        # 1) CAS OK
        def mut_ok(t: StoredTask) -> StoredTask:
            t.version += 1
            return t

        await backend.compare_and_set(tid, 0, mut_ok)
        # 2) CAS 冲突
        try:
            await backend.compare_and_set(tid, 0, mut_ok)
            return {"scenario": "no_conflict"}
        except VersionMismatchError as e:
            return {"scenario": "conflict", "expected": e.expected, "actual": e.actual}

    mem = MemoryBackend()
    redis_b = RedisBackend(RedisConfig(namespace="scenario", use_fakeredis=True))
    try:
        r_mem = await run_cas_scenario(mem)
        r_redis = await run_cas_scenario(redis_b)
        assert r_mem == r_redis
    finally:
        await redis_b.close()
