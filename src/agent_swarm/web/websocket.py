"""
@module agent_swarm.web.websocket
@brief  P5-W28 WebSocket 路由——实时事件流

客户端:
    const ws = new WebSocket("ws://host/ws");
    ws.onmessage = (e) => { const rec = JSON.parse(e.data); ... }

服务器推: 新事件 → JSON 字符串 → 所有连接
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agent_swarm.web.state import EventRecord

log = logging.getLogger(__name__)

router = APIRouter()


async def _push_to_ws(ws: WebSocket, rec: EventRecord) -> None:
    """把事件转 JSON 推给一个 ws 连接"""
    await ws.send_text(json.dumps({
        "event_name": rec.event_name,
        "session_id": rec.session_id,
        "timestamp": rec.timestamp,
        "seq": rec.seq,
        "payload": rec.payload,
    }))


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    主 WebSocket 端点

    连接时: 注册到 state._subscribers
    断开时: 注销
    """
    await websocket.accept()
    state = websocket.app.state.web_state

    async def _notify(rec: EventRecord) -> None:
        try:
            await _push_to_ws(websocket, rec)
        except Exception as exc:  # noqa: BLE001
            log.debug("ws notify failed: %s", exc)
            raise

    state.subscribe(_notify)
    log.info("ws connected, total subscribers=%d", len(state._subscribers))
    try:
        # 推一条 hello 让客户端知道连接 OK
        await websocket.send_text(json.dumps({
            "event_name": "_hello",
            "session_id": "server",
            "timestamp": 0,
            "seq": 0,
            "payload": {"uptime_seconds": int(state.uptime_seconds())},
        }))
        # 阻塞, 等待客户端断开
        while True:
            # 客户端可发 keepalive / 关闭, 我们只需保持连接
            try:
                msg = await asyncio.wait_for(
                    websocket.receive_text(), timeout=60.0,
                )
                # 客户端可发 ping, 我们回 pong
                if msg == "ping":
                    await websocket.send_text("pong")
            except TimeoutError:
                # 心跳超时——发 ping 确认客户端在
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        state.unsubscribe(_notify)
        log.info(
            "ws disconnected, remaining subscribers=%d",
            len(state._subscribers),
        )
