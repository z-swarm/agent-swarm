"""单元测试：observability/websocket_sink.py——W12 WebSocketSink"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.websocket_sink import WebSocketSink


async def _connect_ws(port: int, path: str = "/ws") -> aiohttp.ClientWebSocketResponse:
    """连接 WebSocket 并 consume hello 消息"""
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"http://127.0.0.1:{port}{path}")
    msg = await ws.receive(timeout=2.0)
    assert msg.type == aiohttp.WSMsgType.TEXT
    data = json.loads(msg.data)
    assert data["type"] == "hello"
    # 保留 session 引用，方便 close
    ws._test_session = session  # type: ignore[attr-defined]
    return ws


async def _close(ws: aiohttp.ClientWebSocketResponse) -> None:
    await ws.close()
    sess = getattr(ws, "_test_session", None)
    if sess is not None:
        await sess.close()


# ---------------------------------------------------------------------------
# 启动/停止
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_start_stop_idempotent() -> None:
    """start/stop 幂等"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    assert sink.is_running
    assert sink.bound_port > 0
    await sink.start()  # 幂等
    assert sink.is_running
    await sink.stop()
    assert not sink.is_running
    await sink.stop()  # 幂等


# ---------------------------------------------------------------------------
# 多客户端连接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_broadcasts_to_multiple_clients() -> None:
    """多客户端：consume 一条事件 → 所有客户端都收到"""
    sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    await sink.start()
    try:
        port = sink.bound_port
        # 3 个客户端
        wss = [await _connect_ws(port) for _ in range(3)]
        try:
            assert sink.active_clients == 3

            # 推送一条事件
            await sink.consume(
                SessionEvent(
                    event_name="task.created",
                    session_id="S1",
                    timestamp=time.time(),
                    seq=1,
                    payload={"task_id": "T1"},
                )
            )
            # 给发送协程时间跑
            await asyncio.sleep(0.3)
            # 每个客户端都收到
            for ws in wss:
                msg = await ws.receive(timeout=1.0)
                assert msg.type == aiohttp.WSMsgType.TEXT
                data = json.loads(msg.data)
                assert data["type"] == "event"
                assert data["data"]["event_name"] == "task.created"
                assert data["data"]["session_id"] == "S1"
        finally:
            for ws in wss:
                await _close(ws)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 心跳 / ping-pong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_handles_client_ping() -> None:
    """客户端发 ping → 收到 pong"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    try:
        port = sink.bound_port
        ws = await _connect_ws(port)
        try:
            await ws.send_json({"type": "ping", "seq": 0, "data": {}})
            msg = await ws.receive(timeout=1.0)
            data = json.loads(msg.data)
            assert data["type"] == "pong"
        finally:
            await _close(ws)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 断线重连 / 客户端断开不影响 sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_client_disconnect_does_not_crash_sink() -> None:
    """客户端断开 → sink 继续运行 + 接受新客户端"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    try:
        port = sink.bound_port
        # 第一个客户端断开
        ws1 = await _connect_ws(port)
        await _close(ws1)
        await asyncio.sleep(0.2)
        # sink 仍运行
        assert sink.is_running
        # 新客户端能连
        ws2 = await _connect_ws(port)
        try:
            await sink.consume(
                SessionEvent(
                    event_name="x",
                    session_id="s",
                    timestamp=time.time(),
                    seq=1,
                    payload={},
                )
            )
            msg = await ws2.receive(timeout=1.0)
            data = json.loads(msg.data)
            assert data["type"] == "event"
        finally:
            await _close(ws2)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 背压：慢客户端不阻塞
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_enqueue_drops_when_queue_full() -> None:
    """单元测试 _enqueue：直接验证背压逻辑（不依赖 send loop 时序）"""
    sink = WebSocketSink(host="127.0.0.1", port=0, max_queue_per_client=2)
    await sink.start()
    try:
        port = sink.bound_port
        ws = await _connect_ws(port)
        try:
            # 构造一个 _ClientState 用最小的 maxsize queue
            client = next(iter(sink._clients.values()))
            # 替换 queue 为小容量以便快速验证
            client.queue = asyncio.Queue(maxsize=2)
            # 第一次入队正常
            await sink._enqueue(client, {"x": 1})
            await sink._enqueue(client, {"x": 2})
            assert client.dropped_events == 0
            # 第 3 条：满 → 丢最旧
            await sink._enqueue(client, {"x": 3})
            assert client.dropped_events == 1
            # 队列里应是 {2, 3}（旧的 1 被丢）
            assert client.queue.qsize() == 2
            contents = [client.queue.get_nowait() for _ in range(2)]
            assert contents == [{"x": 2}, {"x": 3}]
        finally:
            await _close(ws)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 统计字段
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_tracks_client_stats() -> None:
    """active_clients + total_clients_seen + total_events_sent"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    try:
        port = sink.bound_port
        assert sink.active_clients == 0
        assert sink.total_clients_seen == 0
        assert sink.total_events_sent == 0

        ws = await _connect_ws(port)
        try:
            assert sink.active_clients == 1
            assert sink.total_clients_seen == 1
            for i in range(3):
                await sink.consume(
                    SessionEvent(
                        event_name="x",
                        session_id="s",
                        timestamp=time.time(),
                        seq=i,
                        payload={},
                    )
                )
            await asyncio.sleep(0.3)
            assert sink.total_events_sent == 3
        finally:
            await _close(ws)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 事件编码格式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_event_envelope_format() -> None:
    """事件 envelope 含 type/seq/data 三个字段"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    try:
        port = sink.bound_port
        ws = await _connect_ws(port)
        try:
            await sink.consume(
                SessionEvent(
                    event_name="metric.count",
                    session_id="S",
                    timestamp=1234.5,
                    seq=42,
                    payload={"value": 7},
                    request_id="req-abc",
                )
            )
            await asyncio.sleep(0.2)
            msg = await ws.receive(timeout=1.0)
            data = json.loads(msg.data)
            assert data["type"] == "event"
            assert "seq" in data and data["seq"] > 0  # 内部递增
            assert data["data"]["event_name"] == "metric.count"
            assert data["data"]["session_id"] == "S"
            assert data["data"]["timestamp"] == 1234.5
            assert data["data"]["seq"] == 42
            assert data["data"]["payload"] == {"value": 7}
            assert data["data"]["request_id"] == "req-abc"
        finally:
            await _close(ws)
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# 空状态：no clients → consume 不抛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_consume_with_no_clients_is_noop() -> None:
    """无客户端时 consume 不抛、total_events_sent 不变"""
    sink = WebSocketSink(host="127.0.0.1", port=0)
    await sink.start()
    try:
        await sink.consume(
            SessionEvent(
                event_name="x",
                session_id="s",
                timestamp=time.time(),
                seq=1,
                payload={},
            )
        )
        assert sink.total_events_sent == 0
    finally:
        await sink.stop()


# ---------------------------------------------------------------------------
# P2-NEW-2 修复：慢消费者背压测试（_send_loop 时序场景）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sink_slow_consumer_does_not_block_consume() -> None:
    """
    慢消费者不应阻塞 consume() 的快速调用。
    设计意图：consume() 内部用 put_nowait 丢最旧,emit 速率 = 入队速率,不依赖 send loop。
    """
    sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    await sink.start()
    try:
        port = sink.bound_port
        # 模拟一个慢消费者：把它的 queue 替换为 maxsize=1 的小队列
        ws_slow = await _connect_ws(port)
        try:
            # 模拟"慢消费者"——把它的 send loop 替换成永远不消费 queue 的版本
            slow_client = next(iter(sink._clients.values()))
            # 用 None 占位 sender task 来防止它消费 queue
            # 直接把 queue 替换为 maxsize=1
            slow_client.queue = asyncio.Queue(maxsize=1)
            # 推 100 条事件
            t0 = time.time()
            for i in range(100):
                await sink.consume(
                    SessionEvent(
                        event_name="burst",
                        session_id="s",
                        timestamp=time.time(),
                        seq=i,
                        payload={"i": i},
                    )
                )
            elapsed = time.time() - t0
            # 100 次 consume 应该在 1 秒内完成（不被慢消费者阻塞）
            assert elapsed < 1.0, f"consume() 被慢消费者阻塞 {elapsed:.2f}s"
            # 慢消费者应有大量 drop
            assert slow_client.dropped_events >= 90, (
                f"应有 ≥90 个 drop，实际 {slow_client.dropped_events}"
            )
            # 队列里只保留最后 1 条
            assert slow_client.queue.qsize() == 1
        finally:
            await _close(ws_slow)
    finally:
        await sink.stop()


@pytest.mark.asyncio
async def test_websocket_sink_fast_consumer_unaffected_by_slow_peer() -> None:
    """
    多客户端：慢消费者丢消息不应影响快消费者。
    """
    sink = WebSocketSink(host="127.0.0.1", port=0, heartbeat_interval=10.0)
    await sink.start()
    try:
        port = sink.bound_port
        ws_fast = await _connect_ws(port)
        ws_slow = await _connect_ws(port)
        try:
            # 找到两个客户端
            clients = list(sink._clients.values())
            # 按连接顺序
            slow_client = clients[1]
            # 慢客户端的 queue 改成 maxsize=1, 阻断 send loop
            slow_client.queue = asyncio.Queue(maxsize=1)

            # 推 50 条
            n = 50
            for i in range(n):
                await sink.consume(
                    SessionEvent(
                        event_name="multi",
                        session_id="s",
                        timestamp=time.time(),
                        seq=i,
                        payload={"i": i},
                    )
                )
            # 给快客户端时间收完
            await asyncio.sleep(0.5)

            # 快客户端应收到全部 n 条
            received = 0
            while True:
                try:
                    msg = await asyncio.wait_for(ws_fast.receive(), timeout=0.2)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data["type"] == "event":
                            received += 1
                except TimeoutError:
                    break
            assert received == n, f"快客户端应收到 {n} 条，实际 {received}"

            # 慢客户端应有大量 drop
            assert slow_client.dropped_events >= n - 5, (
                f"慢客户端应有大量 drop，实际 {slow_client.dropped_events}"
            )
        finally:
            await _close(ws_fast)
            await _close(ws_slow)
    finally:
        await sink.stop()
