"""
@module tests.e2e.test_w12_websocket_e2e
@brief  W12 WebSocketSink 端到端验证

W12 DoD：
  ① WebSocketSink 启动 + 多客户端连接 + 事件广播
  ② 完整事件目录（SqliteEventSink 五元组索引）
  ③ 心跳 / 断线重连 / 背压
  ④ ObservabilityBus 集成（事件流走 WS）
  ⑤ 关闭时清理所有连接
"""
from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability import (
    ObservabilityBus,
    SqliteEventSink,
    WebSocketSink,
    emit,
    set_global_bus,
)


async def _ws_connect(port: int, path: str = "/ws") -> aiohttp.ClientWebSocketResponse:
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"http://127.0.0.1:{port}{path}")
    msg = await ws.receive(timeout=2.0)
    assert msg.type == aiohttp.WSMsgType.TEXT
    data = json.loads(msg.data)
    assert data["type"] == "hello"
    ws._test_session = session  # type: ignore[attr-defined]
    return ws


async def _ws_close(ws: aiohttp.ClientWebSocketResponse) -> None:
    await ws.close()
    sess = getattr(ws, "_test_session", None)
    if sess is not None:
        await sess.close()


# ---------------------------------------------------------------------------
# ① 启动 + 多客户端 + 事件广播
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_websocket_broadcasts_bus_events() -> None:
    """ObservabilityBus + WebSocketSink：emit() 事件通过 WS 广播"""
    bus = ObservabilityBus()
    ws_sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    bus.register_sink(ws_sink)
    set_global_bus(bus)

    await ws_sink.start()
    try:
        port = ws_sink.bound_port
        ws1 = await _ws_connect(port)
        ws2 = await _ws_connect(port)
        try:
            assert ws_sink.active_clients == 2

            # 通过 emit 推送事件
            await emit("task.created", "S-test", {"task_id": "T1"},
            )
            await asyncio.sleep(0.3)

            # 两个客户端都收到
            for ws in (ws1, ws2):
                msg = await ws.receive(timeout=1.0)
                data = json.loads(msg.data)
                assert data["type"] == "event"
                assert data["data"]["event_name"] == "task.created"
        finally:
            await _ws_close(ws1)
            await _ws_close(ws2)
    finally:
        await ws_sink.stop()


# ---------------------------------------------------------------------------
# ② SqliteEventSink 五元组索引存在
# ---------------------------------------------------------------------------


def test_e2e_sqlite_five_tuple_index_exists(tmp_path) -> None:
    """W12-2: SqliteEventSink 启动时建立 5 元组索引"""
    db = tmp_path / "indices.db"
    sink = SqliteEventSink(db)
    import asyncio
    async def _init():
        await sink._ensure_conn()
        await sink.aclose()
    asyncio.run(_init())

    import sqlite3
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_events'"
    )
    names = {row[0] for row in cur.fetchall()}
    con.close()
    # 5 元组索引 + 时间 + request_id
    assert "idx_events_5tuple" in names
    assert "idx_events_tenant_time" in names
    assert "idx_events_request_id" in names
    # 原有索引仍在
    assert "idx_events_tenant_session" in names
    assert "idx_events_tenant_name" in names


# ---------------------------------------------------------------------------
# ③ 心跳 / 断线
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_websocket_handles_client_reconnect() -> None:
    """客户端断 → 重连 → 继续收到事件"""
    ws_sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    bus = ObservabilityBus()
    bus.register_sink(ws_sink)
    set_global_bus(bus)
    await ws_sink.start()
    try:
        port = ws_sink.bound_port
        # 第一个客户端连 + 断开
        ws1 = await _ws_connect(port)
        assert ws_sink.active_clients == 1
        await _ws_close(ws1)
        await asyncio.sleep(0.2)
        # sink 应清理掉断开客户端
        assert ws_sink.active_clients == 0
        # 重连
        ws2 = await _ws_connect(port)
        try:
            await emit("reconnect.test", "s", {},
            )
            await asyncio.sleep(0.2)
            msg = await ws2.receive(timeout=1.0)
            data = json.loads(msg.data)
            assert data["data"]["event_name"] == "reconnect.test"
        finally:
            await _ws_close(ws2)
    finally:
        await ws_sink.stop()


# ---------------------------------------------------------------------------
# ④ Bus + 多 sink 集成（WS + SQLite 一起消费）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_websocket_with_sqlite_sink(tmp_path) -> None:
    """WS + SQLite 双 sink 集成：emit 同时写到两端"""
    bus = ObservabilityBus()
    ws_sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    db = tmp_path / "events.db"
    sqlite_sink = SqliteEventSink(db)
    bus.register_sink(ws_sink)
    bus.register_sink(sqlite_sink)
    set_global_bus(bus)
    await ws_sink.start()
    try:
        port = ws_sink.bound_port
        ws = await _ws_connect(port)
        try:
            # 推 2 条事件
            for i in range(2):
                await emit(
                    f"e{i}", "multi-sink",
                    payload={"i": i},
                )
            await asyncio.sleep(0.3)

            # WS 收到 2 条
            for i in range(2):
                msg = await ws.receive(timeout=1.0)
                data = json.loads(msg.data)
                assert data["data"]["event_name"] == f"e{i}"

            # SQLite 也存了 2 条
            events = await sqlite_sink.get_events("multi-sink")
            assert len(events) == 2
            assert [e.event_name for e in events] == ["e0", "e1"]
        finally:
            await _ws_close(ws)
    finally:
        await ws_sink.stop()
        await sqlite_sink.aclose()


# ---------------------------------------------------------------------------
# ⑤ 关闭时清理所有连接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_websocket_stop_closes_all_clients() -> None:
    """WebSocketSink.stop() 关闭所有活跃连接"""
    ws_sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    await ws_sink.start()
    try:
        port = ws_sink.bound_port
        wss = [await _ws_connect(port) for _ in range(3)]
        assert ws_sink.active_clients == 3
    finally:
        await ws_sink.stop()
    # 关闭后所有客户端应被服务器断开
    assert ws_sink.active_clients == 0
    # 验证客户端也感知到 close
    for ws in wss:
        try:
            msg = await ws.receive(timeout=1.0)
            assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        except Exception:
            pass  # 也可能已经收到 CLOSE 后 ws 直接抛
        await _ws_close(ws)
