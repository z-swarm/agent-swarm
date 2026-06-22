"""
@module tests.unit.test_web_state_sink
@brief  P5-W31 WebStateSink 测试

覆盖:
  - 基本 push (WebState 收到事件)
  - 与 ObservabilityBus 集成
  - payload dict 转换
  - 异常不传播
  - 多 sink 共存 (WebState + Sqlite)
"""

from __future__ import annotations

import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability import (
    ObservabilityBus,
    WebStateSink,
)
from agent_swarm.web import WebState

# ---------------------------------------------------------------------------
# 基本 push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_pushes_event_to_web_state() -> None:
    """Sink consume → WebState 收到"""
    state = WebState()
    sink = WebStateSink(state)
    evt = SessionEvent(
        event_name="agent.start",
        session_id="s1",
        timestamp=1000.0,
        seq=1,
        payload={"agent_id": "a1"},
    )
    await sink.consume(evt)
    assert len(state.events) == 1
    assert state.events[0].event_name == "agent.start"
    assert state.active_sessions["s1"]["event_count"] == 1


@pytest.mark.asyncio
async def test_sink_with_empty_payload() -> None:
    """payload=None 或 {} 不崩"""
    state = WebState()
    sink = WebStateSink(state)
    evt = SessionEvent(
        event_name="e1", session_id="s1", timestamp=1.0,
        seq=1, payload={},
    )
    await sink.consume(evt)
    assert len(state.events) == 1
    assert state.events[0].payload == {}


@pytest.mark.asyncio
async def test_sink_with_complex_payload() -> None:
    """payload 含嵌套 dict / list 仍正常推"""
    state = WebState()
    sink = WebStateSink(state)
    payload = {
        "agent_id": "a1",
        "tools": ["read_file", "write_file"],
        "nested": {"k": "v"},
    }
    evt = SessionEvent(
        event_name="e1", session_id="s1", timestamp=1.0,
        seq=1, payload=payload,
    )
    await sink.consume(evt)
    rec = state.events[0]
    assert rec.payload == payload


# ---------------------------------------------------------------------------
# 与 Bus 集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_registers_with_bus() -> None:
    """WebStateSink 注册到 bus 后, emit 触发 sink"""
    bus = ObservabilityBus()
    state = WebState()
    sink = WebStateSink(state)
    bus.register_sink(sink)
    await bus.emit_event("e1", "s1", {"k": "v"})
    assert len(state.events) == 1
    assert state.events[0].event_name == "e1"


@pytest.mark.asyncio
async def test_multiple_sinks_coexist() -> None:
    """WebStateSink + 其他 sink (如 InMemory) 共存"""
    bus = ObservabilityBus()
    state = WebState()
    from agent_swarm.observability import InMemorySink
    ws_sink = WebStateSink(state)
    mem_sink = InMemorySink()
    bus.register_sink(ws_sink)
    bus.register_sink(mem_sink)
    await bus.emit_event("e1", "s1", {})
    # 两者都收到
    assert len(state.events) == 1
    assert len(mem_sink.get_events("s1")) == 1


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_does_not_raise_on_webstate_error() -> None:
    """WebState.push_event 抛错时, sink 不传播"""
    state = WebState()
    sink = WebStateSink(state)
    # 模拟 push 抛错——用一个 mock
    orig_push = state.push_event
    async def broken_push(*args, **kwargs):
        raise RuntimeError("simulated failure")
    state.push_event = broken_push  # type: ignore[method-assign]
    evt = SessionEvent(
        event_name="e1", session_id="s1", timestamp=1.0, seq=1, payload={},
    )
    # 不抛
    await sink.consume(evt)


@pytest.mark.asyncio
async def test_bus_emits_swallows_sink_exception() -> None:
    """bus 自身兜底: sink 抛错不影响其他 sink"""
    bus = ObservabilityBus()
    state = WebState()
    ws_sink = WebStateSink(state)

    # 第一个 sink 故意抛
    class BadSink:
        async def consume(self, event):
            raise RuntimeError("intentional")

    bus.register_sink(BadSink())  # type: ignore[abstract]
    bus.register_sink(ws_sink)
    # 不抛
    await bus.emit_event("e1", "s1", {})
    # WebStateSink 仍收到
    assert len(state.events) == 1


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_unregister_stops_events() -> None:
    """bus.unregister_sink 后, 不再收到"""
    bus = ObservabilityBus()
    state = WebState()
    sink = WebStateSink(state)
    bus.register_sink(sink)
    await bus.emit_event("e1", "s1", {})
    assert len(state.events) == 1
    bus.unregister_sink(sink)
    await bus.emit_event("e2", "s1", {})
    # 没新增
    assert len(state.events) == 1


# ---------------------------------------------------------------------------
# 协议一致性
# ---------------------------------------------------------------------------


def test_sink_inherits_protocol() -> None:
    """WebStateSink 满足 ObservabilitySink 协议"""
    from agent_swarm.observability.bus import ObservabilitySink
    sink = WebStateSink(WebState())
    assert isinstance(sink, ObservabilitySink)


def test_sink_repr() -> None:
    """sink 有可读 repr (调试用)"""
    sink = WebStateSink(WebState())
    r = repr(sink)
    assert "WebStateSink" in r
