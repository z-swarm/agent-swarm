"""
@module tests.unit.test_postgres_backend
@brief  P4-W25 PostgresBackend 单元测试 (用 fake asyncpg)

覆盖:
  - 基本 CRUD (get / put / list_all)
  - CAS 原子性 (version check)
  - 重复 put 抛错
  - stats 聚合
  - 命名空间 (schema) 隔离
  - close 清理连接池
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_swarm.core.backends.postgres_backend import (
    PostgresBackend,
    PostgresConfig,
)
from agent_swarm.core.task_queue_backend import (
    StoredTask,
    TaskQueueBackend,
    VersionMismatchError,
)

# ---------------------------------------------------------------------------
# Fake asyncpg
# ---------------------------------------------------------------------------


class FakeConn:
    """模拟 asyncpg.Connection, 用 dict 存储数据"""

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self.store = store

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        sql_l = sql.lower().lstrip()
        if sql_l.startswith("select data from"):
            task_id = args[0]
            row = self.store.get(task_id)
            return dict(row) if row else None
        if sql_l.startswith("update "):
            new_version, data_json, task_id, expected_version = args
            row = self.store.get(task_id)
            if row is None:
                return None
            if row["version"] != expected_version:
                return None
            row["version"] = new_version
            row["data"] = json.loads(data_json)
            row["updated_at"] = "now"
            return {"data": row["data"]}
        if sql_l.startswith("create "):
            return None
        if sql_l.startswith("create index"):
            return None
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        sql_l = sql.lower().lstrip()
        if sql_l.startswith("select data from"):
            return [dict(r) for r in self.store.values()]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        sql_l = sql.lower().lstrip()
        if sql_l.startswith("insert into"):
            task_id, version, data_json = args
            if task_id in self.store:
                # 模拟 unique violation
                raise Exception("duplicate key value violates unique constraint 23505")
            self.store[task_id] = {
                "id": task_id,
                "version": version,
                "data": json.loads(data_json),
                "updated_at": "now",
            }
            return "INSERT 0 1"
        if sql_l.startswith("create schema"):
            return "CREATE SCHEMA"
        if sql_l.startswith("create table"):
            return "CREATE TABLE"
        if sql_l.startswith("create index"):
            return "CREATE INDEX"
        return "OK"


class FakePool:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self.store = store

    def acquire(self) -> _Acquire:
        return _Acquire(self.store)


class _Acquire:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self.store = store

    async def __aenter__(self) -> FakeConn:
        return FakeConn(self.store)

    async def __aexit__(self, *args: Any) -> None:
        pass


def _mk_fake_module(store: dict[str, dict[str, Any]]) -> Any:
    """构造 fake asyncpg-like module"""
    class FakeMod:
        @staticmethod
        async def create_pool(**kwargs: Any) -> FakePool:
            return FakePool(store)

    return FakeMod()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> dict[str, dict[str, Any]]:
    return {}


@pytest.fixture
def backend(store: dict[str, dict[str, Any]]) -> PostgresBackend:
    cfg = PostgresConfig(
        dsn="postgresql://fake",
        namespace="public",
        fake_module=_mk_fake_module(store),
    )
    return PostgresBackend(cfg)


def _mk_task(tid: str, version: int = 0, status: str = "pending") -> StoredTask:
    import time
    now = time.time()
    return StoredTask(
        id=tid, title=f"t-{tid}", description="test",
        status=status, version=version, assigned_to=None,
        depends_on=[], result=None, error=None,
        created_at=now, updated_at=now,
    )


# ---------------------------------------------------------------------------
# 基本 CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_then_get(backend: PostgresBackend) -> None:
    """put + get: 数据持久化"""
    t = _mk_task("t1", version=0)
    await backend.put(t)
    got = await backend.get("t1")
    assert got is not None
    assert got.id == "t1"
    assert got.version == 0
    assert got.title == "t-t1"


@pytest.mark.asyncio
async def test_get_missing_returns_none(backend: PostgresBackend) -> None:
    """get 不存在的 task 返回 None"""
    got = await backend.get("nonexistent")
    assert got is None


@pytest.mark.asyncio
async def test_put_duplicate_raises(backend: PostgresBackend) -> None:
    """重复 put 同 id 抛 ValueError"""
    t = _mk_task("t1")
    await backend.put(t)
    with pytest.raises(ValueError, match="already exists"):
        await backend.put(t)


@pytest.mark.asyncio
async def test_list_all_empty(backend: PostgresBackend) -> None:
    """list_all: 空 store 返回空列表"""
    assert await backend.list_all() == []


@pytest.mark.asyncio
async def test_list_all_returns_all_tasks(backend: PostgresBackend) -> None:
    """list_all: 返回所有 task"""
    for i in range(5):
        await backend.put(_mk_task(f"t{i}"))
    tasks = await backend.list_all()
    assert len(tasks) == 5
    assert {t.id for t in tasks} == {f"t{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cas_success(backend: PostgresBackend) -> None:
    """CAS 成功: 版本匹配, 原子更新"""
    await backend.put(_mk_task("t1", version=0))
    new = await backend.compare_and_set(
        "t1", 0, lambda t: StoredTask(
            **{**t.to_dict(), "version": 1, "status": "in_progress"},
        ),
    )
    assert new.version == 1
    assert new.status == "in_progress"
    # 持久化了
    current = await backend.get("t1")
    assert current is not None
    assert current.version == 1
    assert current.status == "in_progress"


@pytest.mark.asyncio
async def test_cas_version_mismatch(backend: PostgresBackend) -> None:
    """CAS 版本不符: 抛 VersionMismatchError"""
    await backend.put(_mk_task("t1", version=0))
    # 直接通过 CAS 改到 version=1, 模拟别人改了
    await backend.compare_and_set(
        "t1", 0, lambda t: StoredTask(
            **{**t.to_dict(), "version": 1, "status": "in_progress"},
        ),
    )
    # 现在用 expected=0 改, 应失败
    with pytest.raises(VersionMismatchError) as exc_info:
        await backend.compare_and_set(
            "t1", 0, lambda t: StoredTask(
                **{**t.to_dict(), "version": 1},
            ),
        )
    assert exc_info.value.expected == 0
    assert exc_info.value.actual == 1


@pytest.mark.asyncio
async def test_cas_missing_task(backend: PostgresBackend) -> None:
    """CAS 不存在的 task: 抛 KeyError"""
    with pytest.raises(KeyError, match="nonexistent"):
        await backend.compare_and_set(
            "nonexistent", 0, lambda t: t,
        )


@pytest.mark.asyncio
async def test_cas_must_bump_version(backend: PostgresBackend) -> None:
    """CAS mutator 必须 version+1"""
    await backend.put(_mk_task("t1", version=0))
    with pytest.raises(ValueError, match="must bump version by 1"):
        await backend.compare_and_set(
            "t1", 0, lambda t: t,  # 不改 version
        )


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats(backend: PostgresBackend) -> None:
    """stats: 按 status 聚合"""
    await backend.put(_mk_task("t1", status="pending"))
    await backend.put(_mk_task("t2", status="pending"))
    await backend.put(_mk_task("t3", status="completed"))
    s = await backend.stats()
    assert s == {"pending": 2, "completed": 1}


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


def test_implements_protocol(backend: PostgresBackend) -> None:
    """PostgresBackend 满足 TaskQueueBackend 协议"""
    assert isinstance(backend, TaskQueueBackend)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close(store: dict[str, dict[str, Any]]) -> None:
    """close: 释放连接池"""
    backend = PostgresBackend(
        config=PostgresConfig(fake_module=_mk_fake_module(store)),
    )
    await backend._ensure_connected()
    assert backend._initialized
    await backend.close()
    assert not backend._initialized


@pytest.mark.asyncio
async def test_close_without_init_does_not_raise() -> None:
    """close 未初始化时也不报错"""
    backend = PostgresBackend(config=PostgresConfig(fake_module=None))
    await backend.close()  # no raise
