"""
@module agent_swarm.observability.websocket_sink
@brief  WebSocketSink——DESIGN §5.3 / Phase 2 W12

把 SessionEvent 推送到 WebSocket 客户端（TUI / GUI 实时仪表盘）

W12 范围：
  - 启动 aiohttp web server 暴露 /ws 端点
  - 多客户端广播（fan-out）
  - 心跳（ping/pong）— 检测死连接
  - 断线重连（客户端断开不影响 sink）
  - 背压：客户端慢时丢最旧（避免内存爆炸）
  - seq 单调：客户端可基于 seq 校验完整性

@note 协议（应用层 JSON over WS）：
  {
    "type": "event" | "ack" | "ping" | "pong",
    "seq": int,
    "data": {...SessionEvent fields...}
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocketClient：维护单个客户端的发送队列
# ---------------------------------------------------------------------------


@dataclass
class _ClientState:
    """单个 WS 客户端的运行时状态"""

    client_id: str
    ws: web.WebSocketResponse
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    last_pong_at: float = field(default_factory=time.time)
    dropped_events: int = 0
    send_count: int = 0


class WebSocketSink(ObservabilitySink):
    """
    WebSocket Sink——DESIGN §5.3

    @note W12 范围：stdio 不带 WebSocket，TUI/GUI 启动时显式 register
    @note 失败兜底：consume() 永不抛——sink 内部错误仅记 warning
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8766,
        path: str = "/ws",
        heartbeat_interval: float = 30.0,
        max_queue_per_client: int = 256,
    ) -> None:
        """
        @param host                  绑定地址
        @param port                  绑定端口（0 = 自动）
        @param path                  WebSocket 路径
        @param heartbeat_interval    心跳间隔（秒）；超 2 倍未收到 pong → 断连
        @param max_queue_per_client  单客户端发送队列上限（满后丢最旧）
        """
        self._host = host
        self._port = port
        self._path = path
        self._heartbeat_interval = heartbeat_interval
        self._max_queue = max_queue_per_client
        self._clients: dict[str, _ClientState] = {}
        self._clients_lock = asyncio.Lock()
        self._server: web.AppRunner | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._started = False
        self._event_seq = 0
        self._started_at: float | None = None
        # 统计
        self.total_events_sent = 0
        self.total_clients_seen = 0

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def active_clients(self) -> int:
        return len(self._clients)

    @property
    def bound_port(self) -> int:
        """获取实际绑定端口（port=0 时由 OS 决定）"""
        if self._server is None:
            return self._port
        try:
            sites = self._server.sites
            if sites:
                site = next(iter(sites))
                bound = getattr(site, "_bound_port", None)
                if bound:
                    return int(bound)
        except Exception:  # noqa: BLE001
            pass
        return self._port

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 WS server + 心跳 task"""
        if self._started:
            return
        self._started = True
        self._started_at = time.time()
        app = web.Application()
        app.router.add_get(self._path, self._handle_ws)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._server = runner
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info("websocket_sink.started host=%s port=%s path=%s",
                 self._host, self.bound_port, self._path)

    async def stop(self) -> None:
        """关闭所有客户端 + 停止 server"""
        if not self._started:
            return
        self._started = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat_task = None
        # 关闭所有客户端
        async with self._clients_lock:
            for client in list(self._clients.values()):
                try:
                    await client.ws.close(code=WSCloseCode.GOING_AWAY)
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()
        if self._server is not None:
            try:
                await self._server.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ------------------------------------------------------------------
    # 事件消费
    # ------------------------------------------------------------------
    async def consume(self, event: SessionEvent) -> None:
        """收到事件 → 编码为 JSON → 推给所有活跃客户端"""
        if not self._started or not self._clients:
            return
        self._event_seq += 1
        msg = self._encode_event(event, self._event_seq)
        async with self._clients_lock:
            clients = list(self._clients.values())
        for client in clients:
            await self._enqueue(client, msg)

    def _encode_event(self, event: SessionEvent, seq: int) -> dict[str, Any]:
        """SessionEvent → WS 协议消息"""
        return {
            "type": "event",
            "seq": seq,
            "data": {
                "event_name": event.event_name,
                "session_id": event.session_id,
                "timestamp": event.timestamp,
                "seq": event.seq,
                "request_id": event.request_id,
                "payload": event.payload,
            },
        }

    async def _enqueue(self, client: _ClientState, msg: dict[str, Any]) -> None:
        """入队：满则丢最旧 + 计数"""
        try:
            client.queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                _ = client.queue.get_nowait()
                client.dropped_events += 1
                client.queue.put_nowait(msg)
            except (asyncio.QueueEmpty, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------
    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=self._heartbeat_interval)
        await ws.prepare(request)
        # 注册客户端
        client_id = f"ws-{id(ws):x}"
        client = _ClientState(client_id=client_id, ws=ws)
        async with self._clients_lock:
            self._clients[client_id] = client
            self.total_clients_seen += 1
        log.info("websocket_sink.client_connected id=%s", client_id)
        # 推一条 welcome 消息
        await ws.send_json({
            "type": "hello",
            "seq": 0,
            "data": {
                "client_id": client_id,
                "server_time": time.time(),
                "max_queue": self._max_queue,
            },
        })
        try:
            # 启动发送协程 + 接收协程
            sender = asyncio.create_task(self._send_loop(client))
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        # 处理客户端 ping / ack
                        try:
                            data = json.loads(msg.data)
                            msg_type = data.get("type")
                            if msg_type == "ping":
                                client.last_pong_at = time.time()
                                await ws.send_json({"type": "pong", "seq": 0, "data": {}})
                            elif msg_type == "ack":
                                client.last_pong_at = time.time()
                        except (json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
                            log.debug("websocket_sink.invalid_client_msg err=%s", exc)
                    elif msg.type == WSMsgType.PING:
                        client.last_pong_at = time.time()
                    elif msg.type == WSMsgType.PONG:
                        client.last_pong_at = time.time()
                    elif msg.type == WSMsgType.ERROR:
                        log.warning("websocket_sink.ws_error id=%s err=%s",
                                    client_id, ws.exception())
                        break
            finally:
                sender.cancel()
                try:
                    await sender
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        finally:
            async with self._clients_lock:
                self._clients.pop(client_id, None)
            log.info("websocket_sink.client_disconnected id=%s", client_id)
        return ws

    async def _send_loop(self, client: _ClientState) -> None:
        """持续从 queue 拉消息 → 通过 ws 发送"""
        while True:
            try:
                msg = await client.queue.get()
                if client.ws.closed:
                    break
                await client.ws.send_json(msg)
                client.send_count += 1
                # L4 注释:+= 在 asyncio 单线程下是原子的(无 await 中断)
                # 若未来切到多线程,需改用 int 包裹或 asyncio.Lock
                self.total_events_sent += 1
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("websocket_sink.send_error id=%s err=%s",
                            client.client_id, exc)
                break

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """定期检查客户端心跳（pong 超过 2×heartbeat_interval 未到 → 断）"""
        while self._started:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                cutoff = time.time() - (self._heartbeat_interval * 2)
                async with self._clients_lock:
                    stale = [
                        c for c in self._clients.values()
                        if c.last_pong_at < cutoff
                    ]
                for c in stale:
                    log.info("websocket_sink.heartbeat_timeout id=%s", c.client_id)
                    try:
                        await c.ws.close(code=WSCloseCode.GOING_AWAY)
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("websocket_sink.heartbeat_error err=%s", exc)


__all__ = [
    "WebSocketSink",
]
