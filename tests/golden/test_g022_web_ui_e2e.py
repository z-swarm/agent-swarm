"""
@module tests.golden.test_g022_web_ui_e2e
@brief  P5 G-022 Golden Case: Web UI 端到端事件流

DoD (P5 W28+W31+W32 闭环):
  - SessionEvent 经 WebStateSink 推入 WebState
  - WebSocket 客户端实时收到 JSON 事件
  - /partials/events 返回 HTML 片段包含全部事件
  - 多订阅者 fan-out
  - payload 嵌套结构保留
  - 缓冲上限触发后丢老不报错
  - sink 异常隔离不传播
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest

from agent_swarm.observability.web_state_sink import WebStateSink
from agent_swarm.web import create_app
from agent_swarm.web.state import EventRecord, WebState

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def web_state() -> WebState:
    """默认缓冲 500,够大多数测试用"""
    return WebState()


@pytest.fixture
def small_state() -> WebState:
    """小缓冲 (5) 专给 overflow 测试用——避免推 500 条"""
    s = WebState()
    s.events = deque(maxlen=5)
    return s


@pytest.fixture
def app(web_state: WebState):
    """最小 FastAPI app,只挂 web_state"""
    return create_app(web_state=web_state)


# ============================================================
# G-022-1: WebStateSink → WebState 单调推送
# ============================================================

@pytest.mark.asyncio
async def test_g022_sink_pushes_to_state(web_state: WebState) -> None:
    """WebStateSink.consume 把 SessionEvent 转 EventRecord 推入 WebState"""
    sink = WebStateSink(web_state)
    fake_event = _make_event("task.completed", session_id="sess-1", payload={"task_id": "t1"})

    await sink.consume(fake_event)

    assert len(web_state.events) == 1
    rec = list(web_state.events)[0]
    assert rec.event_name == "task.completed"
    assert rec.session_id == "sess-1"
    assert rec.payload == {"task_id": "t1"}


# ============================================================
# G-022-2: WebSocket 端到端
# ============================================================

def test_g022_ws_receives_pushed_events(app, web_state: WebState) -> None:
    """WS 客户端能收到 sink 推入的全部事件 (跳过连接时的 _hello)"""
    from fastapi.testclient import TestClient

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        # 跳过 _hello 心跳
        hello = json.loads(ws.receive_text())
        assert hello["event_name"] == "_hello"

        sink = WebStateSink(web_state)
        for i in range(3):
            asyncio.run(sink.consume(_make_event(
                f"event.{i}", session_id="sess-ws", payload={"i": i}
            )))
        received: list[dict[str, Any]] = []
        for _ in range(3):
            msg = ws.receive_text()
            received.append(json.loads(msg))
        assert [r["event_name"] for r in received] == ["event.0", "event.1", "event.2"]
        assert all(r["session_id"] == "sess-ws" for r in received)
        assert [r["payload"] for r in received] == [{"i": 0}, {"i": 1}, {"i": 2}]


# ============================================================
# G-022-3: /partials/events HTML 渲染
# ============================================================

def test_g022_partials_events_renders_html(app, web_state: WebState) -> None:
    """/partials/events 返回 HTMX 片段,含全部事件"""
    from fastapi.testclient import TestClient

    asyncio.run(WebStateSink(web_state).consume(_make_event(
        "swarm.start", session_id="abc12345xyz", payload={"agent": "writer"}
    )))

    with TestClient(app) as client:
        r = client.get("/partials/events")
        assert r.status_code == 200
        html = r.text
        assert "swarm.start" in html
        assert "abc12345" in html  # session_id 截断 8 字符
        assert "writer" in html  # payload 字符串化


# ============================================================
# G-022-4: 多订阅者 fan-out
# ============================================================

@pytest.mark.asyncio
async def test_g022_multi_subscriber_fanout(web_state: WebState) -> None:
    """同一 WebState 多订阅者,每条事件都收到"""
    received_a: list[str] = []
    received_b: list[str] = []

    async def sub_a(rec: EventRecord) -> None:
        received_a.append(rec.event_name)

    async def sub_b(rec: EventRecord) -> None:
        received_b.append(rec.event_name)

    web_state.subscribe(sub_a)
    web_state.subscribe(sub_b)
    sink = WebStateSink(web_state)

    await sink.consume(_make_event("a"))
    await sink.consume(_make_event("b"))

    assert received_a == ["a", "b"]
    assert received_b == ["a", "b"]


# ============================================================
# G-022-5: 缓冲上限 (丢老不报错)
# ============================================================

@pytest.mark.asyncio
async def test_g022_buffer_overflow_drops_old(small_state: WebState) -> None:
    """WebState deque maxlen 触发后,旧事件被丢弃,新事件仍推送"""
    sink = WebStateSink(small_state)
    for i in range(10):
        await sink.consume(_make_event(f"e.{i:03d}"))
    # 缓冲 5 条,前 5 条已丢
    assert len(small_state.events) == 5
    assert list(small_state.events)[0].event_name == "e.005"
    assert list(small_state.events)[-1].event_name == "e.009"


# ============================================================
# G-022-6: sink 异常隔离
# ============================================================

@pytest.mark.asyncio
async def test_g022_sink_exception_isolated(web_state: WebState) -> None:
    """WebStateSink 推送失败不传播到其他 sink / 业务"""
    async def bad(rec: EventRecord) -> None:
        raise RuntimeError("subscriber boom")

    received: list[str] = []

    async def good(rec: EventRecord) -> None:
        received.append(rec.event_name)

    web_state.subscribe(bad)
    web_state.subscribe(good)
    sink = WebStateSink(web_state)

    # 不应抛
    await sink.consume(_make_event("evt"))
    assert received == ["evt"]


# ============================================================
# Helpers
# ============================================================

def _make_event(
    name: str,
    *,
    session_id: str = "sess-default",
    payload: dict[str, Any] | None = None,
) -> Any:
    """构造 SessionEvent 鸭子类型——WebStateSink 只需 .event_name/.session_id/.payload"""
    class _FakeEvent:
        pass

    e = _FakeEvent()
    e.event_name = name
    e.session_id = session_id
    e.payload = payload or {}
    e.timestamp = 1234567890.0
    e.seq = 0
    return e
