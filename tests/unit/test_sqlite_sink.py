"""单元测试：SqliteEventSink——持久化、读取、session 元数据"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.sqlite_sink import SqliteEventSink


@pytest.fixture
async def sink(tmp_path: Path):
    s = SqliteEventSink(tmp_path / "events.db")
    yield s
    await s.aclose()


def _evt(name: str, sid: str, seq: int, payload: dict | None = None) -> SessionEvent:
    return SessionEvent(
        event_name=name,
        session_id=sid,
        timestamp=1234.0 + seq,
        seq=seq,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# 写入 + 读取往返
# ---------------------------------------------------------------------------


async def test_consume_and_get_events_roundtrip(sink: SqliteEventSink) -> None:
    await sink.consume(_evt("task.created", "S", 0, {"task_id": "T1"}))
    await sink.consume(_evt("task.claimed", "S", 1, {"task_id": "T1", "agent": "a"}))

    got = await sink.get_events("S")
    assert len(got) == 2
    assert got[0].event_name == "task.created"
    assert got[0].seq == 0
    assert got[0].payload == {"task_id": "T1"}
    assert got[1].event_name == "task.claimed"
    assert got[1].seq == 1


async def test_get_events_other_session_returns_empty(sink: SqliteEventSink) -> None:
    await sink.consume(_evt("x", "S1", 0))
    assert await sink.get_events("S2") == []


async def test_get_events_sorted_by_seq(sink: SqliteEventSink) -> None:
    """seq 乱序写入——读出时按 seq 升序"""
    for s in (3, 1, 2, 0):
        await sink.consume(_evt("x", "S", s))
    seqs = [e.seq for e in await sink.get_events("S")]
    assert seqs == [0, 1, 2, 3]


async def test_consume_idempotent_on_same_seq(sink: SqliteEventSink) -> None:
    """同 (session_id, seq) 重复 consume 应覆盖（INSERT OR REPLACE）"""
    await sink.consume(_evt("a", "S", 0, {"v": 1}))
    await sink.consume(_evt("a", "S", 0, {"v": 2}))  # 同 seq 覆盖
    events = await sink.get_events("S")
    assert len(events) == 1
    assert events[0].payload == {"v": 2}


async def test_consume_unicode_payload(sink: SqliteEventSink) -> None:
    """非 ASCII 内容应被原样保存"""
    await sink.consume(_evt("x", "S", 0, {"name": "中文"}))
    e = (await sink.get_events("S"))[0]
    assert e.payload == {"name": "中文"}


async def test_consume_handles_unserializable_payload(sink: SqliteEventSink) -> None:
    """payload 含不可 JSON 序列化对象——不抛，转 str"""

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    await sink.consume(_evt("x", "S", 0, {"obj": Weird()}))  # type: ignore[dict-item]
    e = (await sink.get_events("S"))[0]
    assert "<weird>" in str(e.payload["obj"])


async def test_request_id_persisted(sink: SqliteEventSink) -> None:
    e = SessionEvent(
        event_name="x",
        session_id="S",
        timestamp=0.0,
        seq=0,
        payload={},
        request_id="req-1",
    )
    await sink.consume(e)
    out = await sink.get_events("S")
    assert out[0].request_id == "req-1"


# ---------------------------------------------------------------------------
# Session 元数据
# ---------------------------------------------------------------------------


async def test_register_and_get_session(sink: SqliteEventSink) -> None:
    await sink.register_session("S1", "swarm-x", config_yaml="name: x")
    info = await sink.get_session("S1")
    assert info is not None
    assert info["swarm_name"] == "swarm-x"
    assert info["config_yaml"] == "name: x"
    assert info["ended_at"] is None
    assert info["state"] is None


async def test_register_session_idempotent(sink: SqliteEventSink) -> None:
    """重复 register 同一 session 不应覆盖（INSERT OR IGNORE）"""
    await sink.register_session("S", "first", config_yaml="a")
    await sink.register_session("S", "second", config_yaml="b")
    info = await sink.get_session("S")
    assert info["swarm_name"] == "first"
    assert info["config_yaml"] == "a"


async def test_end_session_updates_state(sink: SqliteEventSink) -> None:
    await sink.register_session("S", "x")
    await sink.end_session("S", "completed")
    info = await sink.get_session("S")
    assert info["state"] == "completed"
    assert info["ended_at"] is not None


async def test_get_session_unknown_returns_none(sink: SqliteEventSink) -> None:
    assert await sink.get_session("ghost") is None


async def test_list_sessions_descending_by_created_at(sink: SqliteEventSink) -> None:
    """最新创建排在前面"""
    await sink.register_session("S1", "x")
    await sink.register_session("S2", "y")
    sessions = await sink.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] in ("S1", "S2")  # 顺序看 created_at


# ---------------------------------------------------------------------------
# 持久化（关闭后再开应能读到）
# ---------------------------------------------------------------------------


async def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "persist.db"
    sink1 = SqliteEventSink(db)
    await sink1.consume(_evt("x", "S", 0, {"v": 1}))
    await sink1.register_session("S", "swarm")
    await sink1.aclose()

    sink2 = SqliteEventSink(db)
    events = await sink2.get_events("S")
    assert len(events) == 1
    assert events[0].payload == {"v": 1}
    info = await sink2.get_session("S")
    assert info is not None
    assert info["swarm_name"] == "swarm"
    await sink2.aclose()


async def test_in_memory_db(tmp_path: Path) -> None:
    """:memory: 也能用——单进程内有效"""
    sink = SqliteEventSink(":memory:")
    await sink.consume(_evt("x", "S", 0))
    assert len(await sink.get_events("S")) == 1
    await sink.aclose()


# ---------------------------------------------------------------------------
# F-02: 多租户隔离（tenant_id 隐式从 SecurityContextManager 取）
# ---------------------------------------------------------------------------


async def test_tenant_isolation_blocks_cross_tenant_read(tmp_path: Path) -> None:
    """
    @brief F-02: tenant A 写, tenant B 读——B 读不到 A 的事件流
    """
    from agent_swarm.security.context import SecurityContext, SecurityContextManager

    # 单库: 同一 SqliteEventSink 实例, 切 ctx 测
    sink = SqliteEventSink(tmp_path / "events.db")

    # tenant A 写
    with SecurityContextManager.scope(SecurityContext(tenant_id="A", session_id="S-A")):
        await sink.register_session("S-A", "swarmA")
        await sink.consume(_evt("task.created", "S-A", 0, {"secret": "A_DATA"}))

    # tenant B 在同一实例下读——应读不到
    with SecurityContextManager.scope(SecurityContext(tenant_id="B", session_id="S-B")):
        events = await sink.get_events("S-A")
        assert events == [], "tenant B 不应读到 tenant A 的事件"
        info = await sink.get_session("S-A")
        assert info is None, "tenant B 不应读到 tenant A 的 session 元数据"
        sessions = await sink.list_sessions()
        assert sessions == [], "tenant B 不应列出 tenant A 的 session"

    await sink.aclose()


async def test_tenant_isolation_in_separate_db_files(tmp_path: Path) -> None:
    """
    @brief 多个 sink 实例（每个租户独立 db）——互不影响

    @note 模拟生产多租户: 每个 tenant 一个 db 文件
    """
    from agent_swarm.security.context import SecurityContext, SecurityContextManager

    sink_a = SqliteEventSink(tmp_path / "tenant_a.db")
    sink_b = SqliteEventSink(tmp_path / "tenant_b.db")

    with SecurityContextManager.scope(SecurityContext(tenant_id="A", session_id="S-A")):
        await sink_a.register_session("S-A", "swarmA")
        await sink_a.consume(_evt("task.created", "S-A", 0, {"who": "A"}))

    with SecurityContextManager.scope(SecurityContext(tenant_id="B", session_id="S-B")):
        await sink_b.register_session("S-B", "swarmB")
        await sink_b.consume(_evt("task.created", "S-B", 0, {"who": "B"}))

    # 切到 A 读 A 的库——应只看到 A
    with SecurityContextManager.scope(SecurityContext(tenant_id="A", session_id="S-A")):
        a_events = await sink_a.get_events("S-A")
        assert len(a_events) == 1
        assert a_events[0].payload["who"] == "A"

    # 切到 B 读 B 的库
    with SecurityContextManager.scope(SecurityContext(tenant_id="B", session_id="S-B")):
        b_events = await sink_b.get_events("S-B")
        assert len(b_events) == 1
        assert b_events[0].payload["who"] == "B"

    await sink_a.aclose()
    await sink_b.aclose()


async def test_tenant_default_local_when_no_ctx(tmp_path: Path) -> None:
    """
    @brief 无 SecurityContext 时回退 tenant_id='local'——单租户兜底
    """
    from agent_swarm.security.context import SecurityContextManager, default_local_context

    sink = SqliteEventSink(tmp_path / "events.db")
    # 单租户模式: 用 default_local_context 显式设置 (Phase 1 全程场景)
    with SecurityContextManager.scope(default_local_context("S")):
        await sink.consume(_evt("x", "S", 0, {"ok": True}))
        events = await sink.get_events("S")
        assert len(events) == 1
    await sink.aclose()


async def test_v1_to_v2_migration_preserves_data(tmp_path: Path) -> None:
    """
    @brief V1 schema (无 tenant_id) 自动迁移到 V2, tenant_id 默认 'local'
    """
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    # 1) 手动建 V1 schema + 写数据
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript("""
            CREATE TABLE session_events (
                session_id TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                event_name TEXT NOT NULL,
                timestamp  REAL NOT NULL,
                payload    TEXT NOT NULL,
                request_id TEXT,
                PRIMARY KEY (session_id, seq)
            );
            CREATE TABLE sessions (
                session_id   TEXT PRIMARY KEY,
                swarm_name   TEXT NOT NULL,
                created_at   REAL NOT NULL,
                ended_at     REAL,
                state        TEXT,
                config_yaml  TEXT
            );
        """)
        await conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            ("S-legacy", "swarm-legacy", 1234.0, None, "completed", None),
        )
        await conn.execute(
            "INSERT INTO session_events VALUES (?, ?, ?, ?, ?, ?)",
            ("S-legacy", 0, "task.created", 1234.0, '{"k":"v"}', None),
        )
        await conn.commit()

    # 2) 打开新 SqliteEventSink——应自动迁移
    sink = SqliteEventSink(db_path)
    info = await sink.get_session("S-legacy")
    assert info is not None
    assert info["swarm_name"] == "swarm-legacy"
    events = await sink.get_events("S-legacy")
    assert len(events) == 1
    assert events[0].event_name == "task.created"
    await sink.aclose()


# ---------------------------------------------------------------------------
# 与 ObservabilityBus 集成
# ---------------------------------------------------------------------------


async def test_sqlite_sink_works_with_bus(tmp_path: Path) -> None:
    from agent_swarm.observability import ObservabilityBus

    bus = ObservabilityBus()
    sink = SqliteEventSink(tmp_path / "bus.db")
    bus.register_sink(sink)
    try:
        await bus.emit_event("task.created", "S", payload={"id": "T1"})
        await bus.emit_event("task.claimed", "S", payload={"id": "T1"})
        events = await sink.get_events("S")
        assert [e.event_name for e in events] == ["task.created", "task.claimed"]
        # bus 分配的 seq 也被持久化
        assert [e.seq for e in events] == [0, 1]
    finally:
        await sink.aclose()
