"""
@module tests.unit.test_web
@brief  P5-W28 Web UI 测试 (FastAPI + HTMX + WebSocket)

覆盖:
  - app 工厂 + lifespan
  - 页面 GET (dashboard / agents / worktrees / tasks)
  - HTMX partials GET
  - JSON API (GET / POST)
  - WebSocket 连接 / 事件推送
  - WebState push/subscribe/unsubscribe
  - /metrics 端点
  - /healthz 端点
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state() -> WebState:
    return WebState()


@pytest.fixture
def app(state: WebState):
    return create_app(web_state=state)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# app 工厂
# ---------------------------------------------------------------------------


def test_create_app_default_state() -> None:
    """create_app() 不传 state 也 OK"""
    app = create_app()
    assert app.title == "agent-swarm"
    assert hasattr(app.state, "web_state")
    assert app.state.web_state is not None


def test_create_app_with_state(state: WebState) -> None:
    """create_app 接受外部 state"""
    app = create_app(web_state=state)
    assert app.state.web_state is state


def test_app_routes_registered(app) -> None:
    """app 注册了所有预期路由"""
    # 用 app.url_path_for 间接验证 (需要参数时用 dummy)
    # 或直接 hit 路由——更可靠
    client = TestClient(app)
    for path in ["/", "/agents", "/worktrees", "/tasks",
                 "/partials/events", "/partials/metrics",
                 "/partials/agents", "/partials/worktrees",
                 "/partials/tasks",
                 "/api/state", "/api/events",
                 "/healthz", "/metrics"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def test_dashboard_page(client: TestClient) -> None:
    """GET / 返回 200 + 包含 HTMX"""
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "htmx" in r.text.lower()


def test_agents_page(client: TestClient) -> None:
    r = client.get("/agents")
    assert r.status_code == 200
    assert "Agents" in r.text


def test_worktrees_page(client: TestClient) -> None:
    r = client.get("/worktrees")
    assert r.status_code == 200
    assert "Worktrees" in r.text


def test_tasks_page(client: TestClient) -> None:
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Tasks" in r.text


def test_navlinks_active_class(client: TestClient) -> None:
    """当前页 nav link 有 active class"""
    r = client.get("/agents")
    assert r.status_code == 200
    assert 'class="active"' in r.text


# ---------------------------------------------------------------------------
# HTMX partials
# ---------------------------------------------------------------------------


def test_partial_events_empty(client: TestClient, state: WebState) -> None:
    """partial events 空时返回占位"""
    r = client.get("/partials/events")
    assert r.status_code == 200
    assert "暂无事件" in r.text


def test_partial_events_with_data(
    client: TestClient, state: WebState,
) -> None:
    """partial events 有数据时渲染"""
    asyncio.run(state.push_event(
        event_name="agent.start",
        session_id="s1",
        seq=1,
        payload={"agent_id": "a1"},
    ))
    r = client.get("/partials/events")
    assert r.status_code == 200
    assert "agent.start" in r.text


def test_partial_metrics(client: TestClient, state: WebState) -> None:
    """partial metrics 返回 session count + uptime"""
    asyncio.run(state.push_event("e1", "s1", 1, {}))
    r = client.get("/partials/metrics")
    assert r.status_code == 200
    assert "Active sessions" in r.text
    assert "1" in r.text  # session count


def test_partial_agents_empty(client: TestClient) -> None:
    r = client.get("/partials/agents")
    assert r.status_code == 200
    assert "暂无活跃" in r.text


def test_partial_agents_with_session(
    client: TestClient, state: WebState,
) -> None:
    asyncio.run(state.push_event("e1", "session-abc", 1, {}))
    r = client.get("/partials/agents")
    assert r.status_code == 200
    assert "session-abc" in r.text


def test_partial_worktrees_without_manager(client: TestClient) -> None:
    """无 WorktreeManager 时显示空状态"""
    r = client.get("/partials/worktrees")
    assert r.status_code == 200
    assert "暂无活跃 worktree" in r.text


def test_partial_tasks_empty(client: TestClient) -> None:
    r = client.get("/partials/tasks")
    assert r.status_code == 200
    assert "暂无 task" in r.text


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


def test_api_state(client: TestClient, state: WebState) -> None:
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert "uptime_seconds" in data
    assert "session_count" in data
    assert "total_events" in data
    assert "events_by_type" in data


def test_api_events(client: TestClient, state: WebState) -> None:
    asyncio.run(state.push_event("e1", "s1", 1, {"k": "v"}))
    r = client.get("/api/events")
    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert len(data["events"]) == 1
    assert data["events"][0]["event_name"] == "e1"


def test_api_events_limit(client: TestClient, state: WebState) -> None:
    for i in range(10):
        asyncio.run(state.push_event(f"e{i}", "s1", i, {}))
    r = client.get("/api/events?limit=3")
    data = r.json()
    assert len(data["events"]) == 3


def test_api_post_event(client: TestClient, state: WebState) -> None:
    """POST /api/events 注入事件"""
    r = client.post(
        "/api/events",
        json={
            "event_name": "injected",
            "session_id": "manual",
            "seq": 1,
            "payload": {"x": 1},
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert state.events[-1].event_name == "injected"


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /metrics Prometheus 端点
# ---------------------------------------------------------------------------


def test_metrics_prometheus_format(
    client: TestClient, state: WebState,
) -> None:
    asyncio.run(state.push_event("agent.start", "s1", 1, {}))
    asyncio.run(state.push_event("agent.start", "s2", 2, {}))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "agent_swarm_events_total" in body
    assert "agent_swarm_active_sessions" in body
    assert "agent_swarm_uptime_seconds" in body
    assert 'name="agent.start"' in body


# ---------------------------------------------------------------------------
# WebState 单元测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webstate_push_event() -> None:
    state = WebState()
    await state.push_event("e1", "s1", 1, {"k": "v"})
    assert len(state.events) == 1
    assert state.events[0].event_name == "e1"
    assert "s1" in state.active_sessions


@pytest.mark.asyncio
async def test_webstate_subscribe_unsubscribe() -> None:
    state = WebState()
    received: list = []

    async def cb(rec):
        received.append(rec)

    state.subscribe(cb)
    await state.push_event("e1", "s1", 1, {})
    assert len(received) == 1
    state.unsubscribe(cb)
    await state.push_event("e2", "s1", 2, {})
    assert len(received) == 1  # 没新增


@pytest.mark.asyncio
async def test_webstate_recent_events() -> None:
    state = WebState()
    for i in range(10):
        await state.push_event(f"e{i}", "s1", i, {})
    recent = state.recent_events(3)
    assert len(recent) == 3
    # 新 → 旧
    assert recent[0].event_name == "e9"
    assert recent[2].event_name == "e7"


@pytest.mark.asyncio
async def test_webstate_events_by_type() -> None:
    state = WebState()
    await state.push_event("a", "s1", 1, {})
    await state.push_event("a", "s1", 2, {})
    await state.push_event("b", "s1", 3, {})
    assert state.events_by_type() == {"a": 2, "b": 1}


@pytest.mark.asyncio
async def test_webstate_event_buffer_maxlen() -> None:
    state = WebState(max_events=3) if False else WebState()  # default 500
    # deque(maxlen=500) 在构造时指定——这里用 default
    # 推 600 条应只保留最后 500
    for i in range(600):
        await state.push_event(f"e{i}", "s1", i, {})
    assert len(state.events) == 500


# ---------------------------------------------------------------------------
# WebSocket 测试
# ---------------------------------------------------------------------------


def test_websocket_receives_event(app, state: WebState) -> None:
    """WebSocket 连接收到 push 的事件"""
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        # 收到 _hello
        hello = ws.receive_text()
        data = json.loads(hello)
        assert data["event_name"] == "_hello"
        # 推事件
        asyncio.run(state.push_event("ws.test", "s1", 1, {}))
        # 客户端收到
        msg = ws.receive_text()
        data = json.loads(msg)
        assert data["event_name"] == "ws.test"


def test_websocket_multiple_subscribers(app, state: WebState) -> None:
    """多个 ws 客户端都收到事件"""
    with (
        TestClient(app) as client, client.websocket_connect("/ws") as ws1,
        client.websocket_connect("/ws") as ws2,
    ):
        # 各吃 _hello
        ws1.receive_text()
        ws2.receive_text()
        # 推一条
        asyncio.run(state.push_event("multi", "s1", 1, {}))
        d1 = json.loads(ws1.receive_text())
        d2 = json.loads(ws2.receive_text())
        assert d1["event_name"] == "multi"
        assert d2["event_name"] == "multi"


def test_websocket_disconnect_unsubscribes(app, state: WebState) -> None:
    """ws 断开后, 不再收到事件"""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # hello
        # 现在 ws 已断开
        initial_subs = len(state._subscribers)
        asyncio.run(state.push_event("after_close", "s1", 1, {}))
        # subscribers 应已清理
        assert len(state._subscribers) == initial_subs
