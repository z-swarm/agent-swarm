"""
@module tests.unit.test_webstate_store
@brief  P5-W33 WebStateStore 单测 (≥15 cases)

覆盖:
  - MemoryWebStateStore: append/recent/subscribe/maxlen/unsubscribe/close
  - PostgresWebStateStore (fake_module): append/recent/subscribe/session 过滤
  - WebState 集成: 双写到 store
  - 协议检查: isinstance MemoryWebStateStore/PostgresWebStateStore 满足 WebStateStore
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_swarm.web.state import EventRecord, WebState
from agent_swarm.web.store import (
    MemoryWebStateStore,
    PostgresWebStateStore,
    WebStateConfig,
    WebStateStore,
)

# ---------------------------------------------------------------------------
# Fake asyncpg (webstate_events 表)
# ---------------------------------------------------------------------------


class _FakeConn:
    """模拟 asyncpg.Connection, 用 list 存 webstate_events 行"""

    def __init__(self, store: list[dict[str, Any]], seq_counter: list[int]) -> None:
        self.store = store
        self.seq_counter = seq_counter

    async def execute(self, sql: str, *args: Any) -> str:
        s = sql.lower().lstrip()
        if s.startswith("create "):
            return "OK"
        if s.startswith("insert into"):
            event_type, payload_json, session_id, tenant_id = args
            self.seq_counter[0] += 1
            self.store.append(
                {
                    "seq": self.seq_counter[0],
                    "ts": 1.0 + self.seq_counter[0] * 0.001,
                    "event_type": event_type,
                    "payload": payload_json,  # 存为 str (真实 jsonb 行为见 row fetch)
                    "session_id": session_id,
                    "tenant_id": tenant_id,
                }
            )
            return "INSERT 0 1"
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        s = sql.lower().lstrip()
        if "where session_id" in s:
            session_id, n = args
            rows = [r for r in self.store if r["session_id"] == session_id]
            # ORDER BY ts DESC
            rows.sort(key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in rows[:n]]
        if "from webstate_events" in s:
            n = args[0]
            # ORDER BY ts DESC
            sorted_rows = sorted(self.store, key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in sorted_rows[:n]]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class _Acquire:
    def __init__(self, store: list[dict[str, Any]], seq_counter: list[int]) -> None:
        self.store = store
        self.seq_counter = seq_counter

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self.store, self.seq_counter)

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakePool:
    def __init__(self, store: list[dict[str, Any]], seq_counter: list[int]) -> None:
        self.store = store
        self.seq_counter = seq_counter

    def acquire(self) -> _Acquire:
        return _Acquire(self.store, self.seq_counter)


def _mk_fake_module() -> Any:
    """构造 fake module, 内部维护共享 store + seq_counter (跨实例共享)"""
    state: dict[str, Any] = {"store": [], "seq_counter": [0]}

    class FakeMod:
        @staticmethod
        async def create_pool(**kwargs: Any) -> _FakePool:
            return _FakePool(state["store"], state["seq_counter"])

    FakeMod._state = state  # type: ignore[attr-defined]
    return FakeMod()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_store() -> MemoryWebStateStore:
    return MemoryWebStateStore(max_events=100)


@pytest.fixture
def pg_factory() -> Any:
    """返回 (mk_store_fn, shared_state) — 多实例共享 store 模拟跨进程"""
    mod = _mk_fake_module()
    return mod


def _mk_pg_store(fake_mod: Any) -> PostgresWebStateStore:
    return PostgresWebStateStore(
        WebStateConfig(
            dsn="postgresql://fake",
            fake_module=fake_mod,
            tenant_id="local",
        )
    )


# ---------------------------------------------------------------------------
# MemoryWebStateStore tests
# ---------------------------------------------------------------------------


async def test_memory_append_records_event(memory_store: MemoryWebStateStore) -> None:
    await memory_store.append("e1", "s1", 1, {"k": "v"})
    recs = await memory_store.recent(10)
    assert len(recs) == 1
    assert recs[0]["event_name"] == "e1"
    assert recs[0]["session_id"] == "s1"
    assert recs[0]["payload"] == {"k": "v"}


async def test_memory_recent_returns_newest_first(
    memory_store: MemoryWebStateStore,
) -> None:
    for i in range(5):
        await memory_store.append(f"e{i}", "s", i, {"i": i})
    recs = await memory_store.recent(3)
    assert len(recs) == 3
    # 期望新→旧: e4, e3, e2
    assert [r["event_name"] for r in recs] == ["e4", "e3", "e2"]


async def test_memory_recent_filters_by_session_id(
    memory_store: MemoryWebStateStore,
) -> None:
    await memory_store.append("e1", "s1", 1, {})
    await memory_store.append("e2", "s2", 2, {})
    await memory_store.append("e3", "s1", 3, {})
    recs = await memory_store.recent(10, session_id="s1")
    assert [r["event_name"] for r in recs] == ["e3", "e1"]


async def test_memory_maxlen_drops_old() -> None:
    s = MemoryWebStateStore(max_events=3)
    for i in range(5):
        await s.append(f"e{i}", "s", i, {})
    recs = await s.recent(10)
    assert [r["event_name"] for r in recs] == ["e4", "e3", "e2"]


async def test_memory_subscribe_notifies_on_append(
    memory_store: MemoryWebStateStore,
) -> None:
    received: list[dict[str, Any]] = []

    async def cb(rec: dict[str, Any]) -> None:
        received.append(rec)

    memory_store.subscribe(cb)
    await memory_store.append("e", "s", 1, {"x": 1})
    assert len(received) == 1
    assert received[0]["event_name"] == "e"


async def test_memory_subscribe_multiple_subscribers(
    memory_store: MemoryWebStateStore,
) -> None:
    a: list[dict[str, Any]] = []
    b: list[dict[str, Any]] = []

    async def cb_a(rec: dict[str, Any]) -> None:
        a.append(rec)

    async def cb_b(rec: dict[str, Any]) -> None:
        b.append(rec)

    memory_store.subscribe(cb_a)
    memory_store.subscribe(cb_b)
    await memory_store.append("e", "s", 1, {})
    assert len(a) == 1
    assert len(b) == 1


async def test_memory_unsubscribe_stops_notifications(
    memory_store: MemoryWebStateStore,
) -> None:
    received: list[dict[str, Any]] = []

    async def cb(rec: dict[str, Any]) -> None:
        received.append(rec)

    memory_store.subscribe(cb)
    memory_store.unsubscribe(cb)
    await memory_store.append("e", "s", 1, {})
    assert received == []


async def test_memory_close_clears_subscribers(
    memory_store: MemoryWebStateStore,
) -> None:
    async def cb(rec: dict[str, Any]) -> None: ...

    memory_store.subscribe(cb)
    await memory_store.close()
    assert memory_store._subscribers == []  # type: ignore[attr-defined]


async def test_memory_satisfies_protocol(
    memory_store: MemoryWebStateStore,
) -> None:
    """MemoryWebStateStore 满足 WebStateStore 协议"""
    assert isinstance(memory_store, WebStateStore)


# ---------------------------------------------------------------------------
# PostgresWebStateStore tests
# ---------------------------------------------------------------------------


async def test_pg_append_persists_row(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    await store.append("e1", "s1", 1, {"k": "v"})
    recs = await store.recent(10)
    assert len(recs) == 1
    assert recs[0]["event_name"] == "e1"
    assert recs[0]["payload"] == {"k": "v"}


async def test_pg_recent_session_filter(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    await store.append("e1", "s1", 1, {})
    await store.append("e2", "s2", 2, {})
    await store.append("e3", "s1", 3, {})
    recs = await store.recent(10, session_id="s1")
    assert [r["event_name"] for r in recs] == ["e3", "e1"]


async def test_pg_tenant_id_recorded(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    await store.append("e", "s", 1, {})
    recs = await store.recent(1)
    assert recs[0]["tenant_id"] == "local"


async def test_pg_subscribe_local_fanout(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    received: list[dict[str, Any]] = []

    async def cb(rec: dict[str, Any]) -> None:
        received.append(rec)

    store.subscribe(cb)
    await store.append("e", "s", 1, {})
    assert len(received) == 1


async def test_pg_close_clears_state(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    await store.append("e", "s", 1, {})
    await store.close()
    assert store._subscribers == []  # type: ignore[attr-defined]


async def test_pg_satisfies_protocol(pg_factory: Any) -> None:
    store = _mk_pg_store(pg_factory)
    assert isinstance(store, WebStateStore)


async def test_pg_schema_sql_contains_required_columns() -> None:
    """SCHEMA_SQL 必须含 seq/ts/event_type/payload/session_id/tenant_id + 3 索引"""
    from agent_swarm.web.store import SCHEMA_SQL

    for col in ("seq", "ts", "event_type", "payload", "session_id", "tenant_id"):
        assert col in SCHEMA_SQL, f"缺列: {col}"
    for idx in ("_ts_idx", "_session_seq_idx", "_tenant_ts_idx"):
        assert idx in SCHEMA_SQL, f"缺索引: {idx}"


# ---------------------------------------------------------------------------
# WebState 集成: 双写到 store
# ---------------------------------------------------------------------------


async def test_webstate_push_event_writes_to_memory_only_by_default() -> None:
    """W28 向后兼容: 无 store 时只写内存"""
    state = WebState()
    await state.push_event("e1", "s1", 1, {"k": 1})
    assert len(state.recent_events(10)) == 1
    assert state.store is None


async def test_webstate_push_event_writes_to_store_when_attached() -> None:
    """W33: 有 store 时双写 (内存 + store)"""
    mem = MemoryWebStateStore(max_events=100)
    state = WebState(store=mem)
    await state.push_event("e1", "s1", 1, {"k": 1})
    assert len(state.recent_events(10)) == 1
    recs = await mem.recent(10)
    assert len(recs) == 1
    assert recs[0]["event_name"] == "e1"


async def test_webstate_store_append_failure_does_not_break_memory() -> None:
    """store.append 抛错时, 内存路径仍正常"""

    class _Boom:
        async def append(self, *args: Any, **kw: Any) -> None:
            raise RuntimeError("simulated PG down")

        async def close(self) -> None: ...

    state = WebState(store=_Boom())  # type: ignore[arg-type]
    # 不应抛
    await state.push_event("e1", "s1", 1, {})
    assert len(state.recent_events(10)) == 1


async def test_webstate_eventrecord_to_html() -> None:
    rec = EventRecord(event_name="e", session_id="abcdefgh", timestamp=0.0, seq=1, payload={"x": 1})
    html = rec.to_html()
    assert "event-e" in html
    assert "abcdefgh"[:8] in html


# ---------------------------------------------------------------------------
# G-023: 重启恢复 (同 fake 共享 store 模拟跨进程)
# ---------------------------------------------------------------------------


async def test_g023_recovery_after_close(pg_factory: Any) -> None:
    """
    G-023 Golden Case (基础版):
      进程 A: append 5 条 → close
      进程 B: 新 store 实例 (共享 fake) → recent(50) 拉回 5 条
    """
    # 进程 A
    proc_a = _mk_pg_store(pg_factory)
    for i in range(5):
        await proc_a.append(f"e{i}", "s-A", i, {"i": i})
    await proc_a.close()

    # 进程 B (新实例, 共享 fake store)
    proc_b = _mk_pg_store(pg_factory)
    recs = await proc_b.recent(50)
    assert len(recs) == 5
    assert [r["event_name"] for r in recs] == ["e4", "e3", "e2", "e1", "e0"]


async def test_g023_session_isolation_after_recovery(pg_factory: Any) -> None:
    """G-023 续: 重启后 session_id 过滤仍工作"""
    proc_a = _mk_pg_store(pg_factory)
    await proc_a.append("e1", "s-A", 1, {})
    await proc_a.append("e2", "s-B", 2, {})
    await proc_a.append("e3", "s-A", 3, {})
    await proc_a.close()

    proc_b = _mk_pg_store(pg_factory)
    recs_a = await proc_b.recent(50, session_id="s-A")
    recs_b = await proc_b.recent(50, session_id="s-B")
    assert [r["event_name"] for r in recs_a] == ["e3", "e1"]
    assert [r["event_name"] for r in recs_b] == ["e2"]
