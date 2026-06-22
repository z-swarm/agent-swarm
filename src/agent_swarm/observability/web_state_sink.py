"""
@module agent_swarm.observability.web_state_sink
@brief  P5-W31 WebStateSink——把 SessionEvent 推入 WebState

用法:
    state = WebState()
    sink = WebStateSink(state)
    bus.register_sink(sink)
    # 后续: 任何 emit 都会推到 web UI
"""

from __future__ import annotations

import logging

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink
from agent_swarm.web.state import WebState

log = logging.getLogger(__name__)


class WebStateSink(ObservabilitySink):
    """
    把 SessionEvent 灌进 WebState——驱动 P5-W28 Web UI

    @param web_state  WebState 实例 (共享)
    @param drop_unsupported  未知 event_name 是否丢弃 (默认 False——全推)

    @note 同一 web_state 可注册多个 sink (WebSocket + Prometheus 共存)
    """

    def __init__(
        self,
        web_state: WebState,
        *,
        drop_unsupported: bool = False,
    ) -> None:
        self.web_state = web_state
        self.drop_unsupported = drop_unsupported

    async def consume(self, event: SessionEvent) -> None:
        try:
            await self.web_state.push_event(
                event_name=event.event_name,
                session_id=event.session_id,
                seq=event.seq,
                payload=dict(event.payload) if event.payload else {},
            )
        except Exception as exc:  # noqa: BLE001
            # sink 内部错误不传播——记 warning
            log.warning(
                "WebStateSink failed: event=%s err=%s",
                event.event_name,
                exc,
            )


__all__ = ["WebStateSink"]
