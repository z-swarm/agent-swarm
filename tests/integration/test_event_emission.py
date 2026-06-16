"""
@module tests.integration.test_event_emission
@brief  集成测试——TaskQueue/Mailbox/Swarm 在 ObservabilityBus 下 emit 的事件流

层级：integration——多模块协作 + InMemorySink 收集事件
覆盖：
  - task.created / task.claimed / task.completed / task.failed / task.unblocked
  - task.cas_conflict
  - message.sent / message.received
  - swarm.started / swarm.completed / swarm.failed
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Task
from agent_swarm.observability import (
    InMemorySink,
    ObservabilityBus,
    set_global_bus,
)
from tests.conftest import FakeLLMProvider, ScriptedResponse


@pytest.fixture
def bus_with_sink() -> tuple[ObservabilityBus, InMemorySink]:
    """全局 bus + InMemorySink 收集事件，自动清理"""
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)
    set_global_bus(bus)
    yield bus, sink
    set_global_bus(None)


# ---------------------------------------------------------------------------
# TaskQueue → 事件流
# ---------------------------------------------------------------------------


async def test_task_queue_add_emits_created(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    _bus, sink = bus_with_sink
    q = TaskQueue(session_id="S1")
    await q.add(Task(id="T1", title="t", description="d"))

    events = sink.get_events("S1")
    assert len(events) == 1
    assert events[0].event_name == "task.created"
    assert events[0].payload["task_id"] == "T1"


async def test_task_queue_full_lifecycle_event_stream(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    """add → claim → complete 全流程事件名按顺序"""
    _bus, sink = bus_with_sink
    q = TaskQueue(session_id="S")
    await q.add(Task(id="T", title="x", description="y"))
    claimed = await q.claim("T", "agent-1", expected_version=0)
    assert claimed.success
    await q.complete("T", "result", expected_version=claimed.task.version)  # type: ignore[union-attr]

    names = [e.event_name for e in sink.get_events("S")]
    assert names == ["task.created", "task.claimed", "task.completed"]


async def test_task_queue_cas_conflict_emits_event(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    """version 不匹配时 emit task.cas_conflict 含 op/expected/actual"""
    _bus, sink = bus_with_sink
    q = TaskQueue(session_id="S")
    await q.add(Task(id="T", title="x", description="y"))
    await q.claim("T", "agent-1", expected_version=0)  # v: 0→1
    bad = await q.claim("T", "agent-2", expected_version=0)  # 仍传 0 → 冲突
    assert not bad.success

    cas_events = [e for e in sink.get_events("S") if e.event_name == "task.cas_conflict"]
    assert len(cas_events) == 1
    p = cas_events[0].payload
    assert p["op"] == "claim"
    assert p["expected_version"] == 0
    assert p["actual_version"] == 1


async def test_task_queue_unblock_dependents_emits_event(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    """T1 完成 → T2 自动 unblocked，应 emit task.unblocked"""
    _bus, sink = bus_with_sink
    q = TaskQueue(session_id="S")
    await q.add(Task(id="T1", title="x", description="y"))
    await q.add(Task(id="T2", title="x", description="y", depends_on=["T1"]))

    claimed = await q.claim("T1", "a", expected_version=0)
    await q.complete("T1", "ok", expected_version=claimed.task.version)  # type: ignore[union-attr]

    # 应包含 task.unblocked，且 trigger=T1
    unblocked = [e for e in sink.get_events("S") if e.event_name == "task.unblocked"]
    assert len(unblocked) == 1
    assert unblocked[0].payload["task_id"] == "T2"
    assert unblocked[0].payload["trigger"] == "T1"
    # 且 unblocked 在 task.completed 之后
    completed_seq = next(e.seq for e in sink.get_events("S") if e.event_name == "task.completed")
    assert unblocked[0].seq > completed_seq


async def test_task_queue_fail_emits_event(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    _bus, sink = bus_with_sink
    q = TaskQueue(session_id="S")
    await q.add(Task(id="T", title="x", description="y"))
    claimed = await q.claim("T", "a", expected_version=0)
    await q.fail("T", "boom", expected_version=claimed.task.version)  # type: ignore[union-attr]

    failed = [e for e in sink.get_events("S") if e.event_name == "task.failed"]
    assert len(failed) == 1
    assert failed[0].payload["error"] == "boom"


# ---------------------------------------------------------------------------
# Mailbox → 事件流
# ---------------------------------------------------------------------------


async def test_mailbox_send_emits_event(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    _bus, sink = bus_with_sink
    mb = Mailbox(session_id="S")
    await mb.send(Mailbox.make_message("a", "b", "hi", msg_type="question"))

    events = [e for e in sink.get_events("S") if e.event_name == "message.sent"]
    assert len(events) == 1
    p = events[0].payload
    assert p["from"] == "a"
    assert p["to"] == "b"
    assert p["msg_type"] == "question"
    assert p["content"] == "hi"


async def test_mailbox_mark_read_emits_received(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    """mark_read → emit message.received"""
    _bus, sink = bus_with_sink
    mb = Mailbox(session_id="S")
    m1 = Mailbox.make_message("a", "b", "x")
    m2 = Mailbox.make_message("a", "b", "y")
    await mb.send(m1)
    await mb.send(m2)
    await mb.mark_read("b", [m1.id, m2.id])

    received = [e for e in sink.get_events("S") if e.event_name == "message.received"]
    assert len(received) == 1
    assert received[0].payload["count"] == 2
    assert set(received[0].payload["msg_ids"]) == {m1.id, m2.id}


async def test_mailbox_mark_read_no_event_when_nothing_marked(
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    """所有消息已读 → 无新 received 事件"""
    _bus, sink = bus_with_sink
    mb = Mailbox(session_id="S")
    m = Mailbox.make_message("a", "b", "x")
    await mb.send(m)
    await mb.mark_read("b", [m.id])  # 第一次：1 条
    sink.clear()
    await mb.mark_read("b", [m.id])  # 第二次：已读，count=0
    assert sink.get_events("S") == []


# ---------------------------------------------------------------------------
# Swarm.run() → 完整事件流
# ---------------------------------------------------------------------------


async def test_swarm_run_emits_started_and_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    _bus, sink = bus_with_sink

    fake = FakeLLMProvider()
    fake.script.append(ScriptedResponse(content="ok", finish_reason="stop"))
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: fake)

    cfg = {
        "name": "obs-test",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "persona": "p",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 2,
            }
        ],
        "tasks": [{"id": "T", "title": "t"}],
    }
    swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
    res = await swarm.run()
    assert res.state == "completed"

    events = sink.get_events(swarm.session_id)
    names = [e.event_name for e in events]
    # 必须含 started 在前、completed 在最后
    assert names[0] == "swarm.started"
    assert names[-1] == "swarm.completed"
    # 中间应有 task.created / task.claimed / task.completed
    assert "task.created" in names
    assert "task.claimed" in names
    assert "task.completed" in names


async def test_swarm_session_id_is_unique(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bus_with_sink: tuple[ObservabilityBus, InMemorySink],
) -> None:
    fake = FakeLLMProvider()
    for _ in range(5):
        fake.script.append(ScriptedResponse(content="ok", finish_reason="stop"))
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: fake)

    cfg = {
        "name": "x",
        "agents": [
            {"id": "a", "role": "r", "persona": "p",
             "provider": "openai", "model": "gpt-4o-mini",
             "tools": [], "max_iterations": 2}
        ],
        "tasks": [{"id": "T", "title": "t"}],
    }
    s1 = Swarm.from_dict(cfg, base_dir=tmp_path)
    s2 = Swarm.from_dict(cfg, base_dir=tmp_path)
    assert s1.session_id != s2.session_id
    assert s1.session_id.startswith("s-")
