"""单元测试：SessionManager 元数据 + 事件流恢复"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.session_manager import RestoredState, SessionManager
from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.sqlite_sink import SqliteEventSink


@pytest.fixture
async def manager(tmp_path: Path):
    sink = SqliteEventSink(tmp_path / "sessions.db")
    mgr = SessionManager(sink)
    yield mgr, sink
    await sink.aclose()


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


async def test_create_session_returns_id(manager) -> None:
    mgr, _ = manager
    sid = await mgr.create_session("swarm-x", config_yaml="name: x")
    assert sid.startswith("s-")


async def test_create_session_with_explicit_id(manager) -> None:
    mgr, _ = manager
    sid = await mgr.create_session("swarm-x", session_id="my-id")
    assert sid == "my-id"


async def test_get_session_unknown_returns_none(manager) -> None:
    mgr, _ = manager
    assert await mgr.get_session("ghost") is None


async def test_get_session_returns_summary(manager) -> None:
    mgr, _ = manager
    sid = await mgr.create_session("x")
    info = await mgr.get_session(sid)
    assert info is not None
    assert info.session_id == sid
    assert info.swarm_name == "x"
    assert info.ended_at is None
    assert info.state is None


async def test_end_session_marks_state(manager) -> None:
    mgr, _ = manager
    sid = await mgr.create_session("x")
    await mgr.end_session(sid, "completed")
    info = await mgr.get_session(sid)
    assert info.state == "completed"
    assert info.ended_at is not None


async def test_list_sessions(manager) -> None:
    mgr, _ = manager
    await mgr.create_session("a")
    await mgr.create_session("b")
    sessions = await mgr.list_sessions()
    assert len(sessions) == 2
    names = {s.swarm_name for s in sessions}
    assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# 恢复——事件流回放
# ---------------------------------------------------------------------------


async def _seed_events(sink: SqliteEventSink, sid: str, events: list[SessionEvent]) -> None:
    for e in events:
        await sink.consume(e)


async def test_restore_session_unknown_raises(manager) -> None:
    mgr, _ = manager
    with pytest.raises(ValueError, match="not found"):
        await mgr.restore_session("ghost")


async def test_restore_session_no_events(manager) -> None:
    """已注册 session 但无事件——返回空 task_queue / mailbox"""
    mgr, _ = manager
    sid = await mgr.create_session("x")
    state = await mgr.restore_session(sid)
    assert isinstance(state, RestoredState)
    assert state.event_count == 0
    assert state.last_seq == -1
    assert await state.task_queue.list_all() == []
    assert await state.mailbox.all_messages() == []


async def test_restore_task_lifecycle(manager) -> None:
    """task.created → claimed → completed 重放后状态一致"""
    mgr, sink = manager
    sid = await mgr.create_session("x")

    await _seed_events(
        sink, sid,
        [
            SessionEvent(
                event_name="task.created", session_id=sid, timestamp=1.0, seq=0,
                payload={
                    "task_id": "T1", "title": "build", "description": "do it",
                    "status": "pending", "assigned_to": None, "depends_on": [],
                },
            ),
            SessionEvent(
                event_name="task.claimed", session_id=sid, timestamp=2.0, seq=1,
                payload={"task_id": "T1", "agent_id": "agent-1", "version": 1},
            ),
            SessionEvent(
                event_name="task.completed", session_id=sid, timestamp=3.0, seq=2,
                payload={"task_id": "T1", "version": 2, "result": "DONE"},
            ),
        ],
    )

    state = await mgr.restore_session(sid)
    tasks = await state.task_queue.list_all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "T1"
    assert t.status == "completed"
    assert t.assigned_to == "agent-1"
    assert t.version == 2
    assert t.result == "DONE"
    assert state.event_count == 3
    assert state.last_seq == 2


async def test_restore_task_failure(manager) -> None:
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(event_name="task.created", session_id=sid, timestamp=1.0, seq=0,
                         payload={"task_id": "T", "title": "x", "description": "y",
                                  "status": "pending", "depends_on": []}),
            SessionEvent(event_name="task.claimed", session_id=sid, timestamp=2.0, seq=1,
                         payload={"task_id": "T", "agent_id": "a", "version": 1}),
            SessionEvent(event_name="task.failed", session_id=sid, timestamp=3.0, seq=2,
                         payload={"task_id": "T", "version": 2, "error": "boom"}),
        ],
    )
    state = await mgr.restore_session(sid)
    t = (await state.task_queue.list_all())[0]
    assert t.status == "failed"
    assert t.error == "boom"


async def test_restore_dependency_unblock(manager) -> None:
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(event_name="task.created", session_id=sid, timestamp=1.0, seq=0,
                         payload={"task_id": "T1", "title": "a", "description": "",
                                  "status": "pending", "depends_on": []}),
            SessionEvent(event_name="task.created", session_id=sid, timestamp=1.0, seq=1,
                         payload={"task_id": "T2", "title": "b", "description": "",
                                  "status": "blocked", "depends_on": ["T1"]}),
            SessionEvent(event_name="task.claimed", session_id=sid, timestamp=2.0, seq=2,
                         payload={"task_id": "T1", "agent_id": "a", "version": 1}),
            SessionEvent(event_name="task.completed", session_id=sid, timestamp=3.0, seq=3,
                         payload={"task_id": "T1", "version": 2, "result": "ok"}),
            SessionEvent(event_name="task.unblocked", session_id=sid, timestamp=3.0, seq=4,
                         payload={"task_id": "T2", "version": 1, "trigger": "T1"}),
        ],
    )
    state = await mgr.restore_session(sid)
    by_id = {t.id: t for t in await state.task_queue.list_all()}
    assert by_id["T1"].status == "completed"
    assert by_id["T2"].status == "pending"  # unblocked → pending
    assert by_id["T2"].depends_on == ["T1"]


async def test_restore_messages(manager) -> None:
    """message.sent → mailbox 重建；message.received → read 标记"""
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(
                event_name="message.sent", session_id=sid, timestamp=1.0, seq=0,
                payload={
                    "msg_id": "m-1", "from": "a", "to": "b",
                    "msg_type": "notify", "content": "hi",
                    "target_type": "internal", "refs": [], "reply_to": None,
                },
            ),
            SessionEvent(
                event_name="message.sent", session_id=sid, timestamp=2.0, seq=1,
                payload={
                    "msg_id": "m-2", "from": "a", "to": "b",
                    "msg_type": "notify", "content": "second",
                    "target_type": "internal", "refs": [], "reply_to": None,
                },
            ),
            SessionEvent(
                event_name="message.received", session_id=sid, timestamp=3.0, seq=2,
                payload={"agent_id": "b", "msg_ids": ["m-1"], "count": 1},
            ),
        ],
    )

    state = await mgr.restore_session(sid)
    msgs = await state.mailbox.all_messages()
    assert len(msgs) == 2
    by_id = {m.id: m for m in msgs}
    assert by_id["m-1"].read is True   # 已 mark_read
    assert by_id["m-2"].read is False
    # b 的未读箱
    unread = await state.mailbox.receive("b", unread_only=True)
    assert [m.id for m in unread] == ["m-2"]


async def test_restore_skips_swarm_lifecycle_events(manager) -> None:
    """swarm.started / completed 不影响内部状态——只要不抛即可"""
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(event_name="swarm.started", session_id=sid, timestamp=1.0, seq=0,
                         payload={"name": "x"}),
            SessionEvent(event_name="task.created", session_id=sid, timestamp=2.0, seq=1,
                         payload={"task_id": "T", "title": "t", "description": "",
                                  "status": "pending", "depends_on": []}),
            SessionEvent(event_name="swarm.completed", session_id=sid, timestamp=3.0, seq=2,
                         payload={"duration_seconds": 1.0}),
        ],
    )
    state = await mgr.restore_session(sid)
    assert state.event_count == 3
    tasks = await state.task_queue.list_all()
    assert len(tasks) == 1


async def test_restore_unknown_event_name_does_not_crash(manager) -> None:
    """未识别事件名仅 debug 日志，不影响后续重放"""
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(event_name="custom.weird.event", session_id=sid,
                         timestamp=1.0, seq=0, payload={}),
            SessionEvent(event_name="task.created", session_id=sid, timestamp=2.0, seq=1,
                         payload={"task_id": "T", "title": "t", "description": "",
                                  "status": "pending", "depends_on": []}),
        ],
    )
    state = await mgr.restore_session(sid)
    assert state.event_count == 2
    assert len(await state.task_queue.list_all()) == 1


async def test_restore_does_not_re_emit_events(manager) -> None:
    """关键：重放时不应再产生新事件——否则 SQLite 中事件会双倍"""
    mgr, sink = manager
    sid = await mgr.create_session("x")
    await _seed_events(
        sink, sid,
        [
            SessionEvent(event_name="task.created", session_id=sid, timestamp=1.0, seq=0,
                         payload={"task_id": "T", "title": "t", "description": "",
                                  "status": "pending", "depends_on": []}),
        ],
    )
    before = await sink.get_events(sid)
    await mgr.restore_session(sid)
    after = await sink.get_events(sid)
    assert len(before) == len(after) == 1  # 没新增事件
