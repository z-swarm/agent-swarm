"""
@module examples.w12_websocket_realtime
@brief  W12 WebSocketSink + TUI 实时仪表盘 example

启动：
    # 终端 1: 启动 WebSocketSink（默认 127.0.0.1:8766 /ws）
    .venv/bin/python -m agent_swarm.cli.main run \\
        examples/w12_websocket_realtime.yaml

    # 终端 2: 连接 WebSocket 接收事件流
    wscat -c ws://127.0.0.1:8766/ws

行为：
  - 启动 WebSocketSink 监听端口
  - ObservabilityBus 所有事件 (task.* / message.* / swarm.*) 推送到 WS
  - 客户端可订阅实时事件流 → TUI / GUI 实时渲染
  - 5 元组索引支持事件回放（按 session_id / 时间范围查询）

@note W12 落地后：TUI 不再依赖文件轮询；可实时刷新
"""
from __future__ import annotations

import asyncio
import logging

from agent_swarm.observability import (
    ObservabilityBus,
    WebSocketSink,
    emit,
    set_global_bus,
)

log = logging.getLogger(__name__)


async def main() -> None:
    """启动 WebSocketSink 并保持运行"""
    bus = ObservabilityBus()
    ws_sink = WebSocketSink(
        host="127.0.0.1",
        port=8766,
        path="/ws",
        heartbeat_interval=30.0,
        max_queue_per_client=256,
    )
    bus.register_sink(ws_sink)
    set_global_bus(bus)

    log.info("websocket_sink: starting on ws://127.0.0.1:8766/ws")
    await ws_sink.start()
    log.info("websocket_sink: started, clients can now connect")

    # 演示：定期 emit 心跳
    try:
        counter = 0
        while True:
            await asyncio.sleep(5.0)
            counter += 1
            await emit(
                "metric.heartbeat", "demo",
                payload={"counter": counter, "active_clients": ws_sink.active_clients},
            )
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        await ws_sink.stop()


if __name__ == "__main__":
    asyncio.run(main())
