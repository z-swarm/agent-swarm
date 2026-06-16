"""单元测试：ObservabilityBus + 默认 sinks（W3）"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability import (
    InMemorySink,
    JsonLogSink,
    ObservabilityBus,
    ObservabilitySink,
    emit,
    get_global_bus,
    set_global_bus,
)

# ---------------------------------------------------------------------------
# Bus 基础
# ---------------------------------------------------------------------------


async def test_bus_emit_assigns_seq() -> None:
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)

    e1 = await bus.emit_event("a", "s1")
    e2 = await bus.emit_event("b", "s1")
    e3 = await bus.emit_event("a", "s2")  # 不同 session

    assert e1.seq == 0
    assert e2.seq == 1
    assert e3.seq == 0  # session s2 独立计数
    assert e1.timestamp > 0


async def test_bus_emit_dispatches_to_all_sinks() -> None:
    bus = ObservabilityBus()
    s1 = InMemorySink()
    s2 = InMemorySink()
    bus.register_sink(s1)
    bus.register_sink(s2)

    await bus.emit_event("test.x", "S")
    assert len(s1.get_events("S")) == 1
    assert len(s2.get_events("S")) == 1


async def test_bus_unregister_sink() -> None:
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)
    bus.unregister_sink(sink)
    assert sink not in bus.sinks


async def test_bus_sink_error_does_not_propagate() -> None:
    """sink consume 抛异常不应影响其他 sink 也不应让 emit 抛"""

    class BoomSink(ObservabilitySink):
        async def consume(self, event: SessionEvent) -> None:
            raise RuntimeError("boom")

    bus = ObservabilityBus()
    good = InMemorySink()
    bus.register_sink(BoomSink())
    bus.register_sink(good)

    # 不应抛
    await bus.emit_event("x", "S")
    # 好 sink 仍然收到
    assert len(good.get_events("S")) == 1


async def test_bus_concurrent_seq_no_dup() -> None:
    """并发 emit——seq 不重复且单调"""
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)

    async def fire(i: int) -> None:
        await bus.emit_event(f"e{i}", "S")

    await asyncio.gather(*[fire(i) for i in range(50)])
    seqs = [e.seq for e in sink.get_events("S")]
    assert sorted(seqs) == list(range(50))  # 0..49 各一份


async def test_bus_payload_passthrough() -> None:
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)
    await bus.emit_event("e", "S", payload={"k": 1, "n": "x"}, request_id="r1")
    e = sink.get_events("S")[0]
    assert e.payload == {"k": 1, "n": "x"}
    assert e.request_id == "r1"


# ---------------------------------------------------------------------------
# 全局 bus / emit 便捷函数
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_global_bus():
    """每个测试前后清空全局 bus，避免互相污染"""
    set_global_bus(None)
    yield
    set_global_bus(None)


async def test_emit_no_bus_returns_none() -> None:
    """全局 bus 未设置——emit 应返回 None 不抛"""
    set_global_bus(None)
    res = await emit("x", "S")
    assert res is None


async def test_emit_with_global_bus() -> None:
    bus = ObservabilityBus()
    sink = InMemorySink()
    bus.register_sink(sink)
    set_global_bus(bus)

    e = await emit("e", "S", payload={"k": "v"})
    assert e is not None
    assert e.event_name == "e"
    assert sink.get_events("S")[0].payload == {"k": "v"}


def test_get_global_bus() -> None:
    assert get_global_bus() is None
    bus = ObservabilityBus()
    set_global_bus(bus)
    assert get_global_bus() is bus


# ---------------------------------------------------------------------------
# JsonLogSink
# ---------------------------------------------------------------------------


async def test_json_log_sink_writes_each_event_as_json_line() -> None:
    buf = io.StringIO()
    sink = JsonLogSink(stream=buf)

    e = SessionEvent(
        event_name="task.created",
        session_id="S1",
        timestamp=1234.5,
        payload={"task_id": "T1"},
        seq=0,
    )
    await sink.consume(e)

    # 输出应是合法 JSON 一行
    line = buf.getvalue().strip()
    assert "\n" not in line  # 单行
    obj = json.loads(line)
    assert obj["event"] == "task.created"
    assert obj["session"] == "S1"
    assert obj["payload"]["task_id"] == "T1"
    assert obj["seq"] == 0


async def test_json_log_sink_handles_unserializable_payload() -> None:
    """payload 含不可 JSON 序列化对象——不应抛，转 str"""
    buf = io.StringIO()
    sink = JsonLogSink(stream=buf)

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    e = SessionEvent(
        event_name="x",
        session_id="S",
        timestamp=0.0,
        payload={"obj": Weird()},
        seq=0,
    )
    await sink.consume(e)
    obj = json.loads(buf.getvalue().strip())
    assert "<weird>" in obj["payload"]["obj"]


async def test_json_log_sink_includes_request_id_only_when_present() -> None:
    buf = io.StringIO()
    sink = JsonLogSink(stream=buf)
    e_no_req = SessionEvent(event_name="x", session_id="S", timestamp=0.0, seq=0)
    await sink.consume(e_no_req)
    line1 = buf.getvalue().strip()
    obj1 = json.loads(line1)
    assert "req" not in obj1

    buf2 = io.StringIO()
    sink2 = JsonLogSink(stream=buf2)
    e_req = SessionEvent(
        event_name="x", session_id="S", timestamp=0.0, seq=0, request_id="r1"
    )
    await sink2.consume(e_req)
    obj2 = json.loads(buf2.getvalue().strip())
    assert obj2["req"] == "r1"


# ---------------------------------------------------------------------------
# InMemorySink
# ---------------------------------------------------------------------------


async def test_inmemory_sink_groups_by_session() -> None:
    sink = InMemorySink()
    await sink.consume(SessionEvent(event_name="a", session_id="S1", timestamp=0.0, seq=0))
    await sink.consume(SessionEvent(event_name="b", session_id="S1", timestamp=0.0, seq=1))
    await sink.consume(SessionEvent(event_name="c", session_id="S2", timestamp=0.0, seq=0))
    assert len(sink.get_events("S1")) == 2
    assert len(sink.get_events("S2")) == 1
    assert set(sink.all_sessions()) == {"S1", "S2"}


async def test_inmemory_sink_returns_sorted_by_seq() -> None:
    sink = InMemorySink()
    # 反序插入
    for s in (3, 1, 2):
        await sink.consume(SessionEvent(event_name="x", session_id="S", timestamp=0.0, seq=s))
    assert [e.seq for e in sink.get_events("S")] == [1, 2, 3]


async def test_inmemory_sink_clear() -> None:
    sink = InMemorySink()
    await sink.consume(SessionEvent(event_name="x", session_id="S", timestamp=0.0, seq=0))
    sink.clear()
    assert sink.get_events("S") == []
