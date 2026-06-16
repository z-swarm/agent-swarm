"""
@module agent_swarm.observability.sinks
@brief  默认 Sink 实现——JsonLogSink + InMemorySink

W3 范围:
  - JsonLogSink: 输出到 stderr 的结构化 JSON 日志（默认开启）
  - InMemorySink: 测试/调试用，按 session_id 收集事件
  W3 #19 单独引入 SqliteEventSink
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from typing import IO

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink

log = logging.getLogger(__name__)


class JsonLogSink(ObservabilitySink):
    """
    输出结构化 JSON 到 stderr / 自定义 stream

    每行一个 JSON object，便于 jq / log aggregator 处理。
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        # 默认 stderr——stdout 留给业务输出（CLI 表格等）
        self.stream = stream if stream is not None else sys.stderr

    async def consume(self, event: SessionEvent) -> None:
        try:
            line = json.dumps(
                {
                    "ts": event.timestamp,
                    "seq": event.seq,
                    "session": event.session_id,
                    "event": event.event_name,
                    "payload": event.payload,
                    **({"req": event.request_id} if event.request_id else {}),
                },
                ensure_ascii=False,
                default=str,  # 兜底：非 JSON 类型转 str
            )
            self.stream.write(line + "\n")
            self.stream.flush()
        except Exception as exc:  # noqa: BLE001
            # sink 内部错误不应传播——记 warning 即可
            log.warning("JsonLogSink failed: %s", exc)


class InMemorySink(ObservabilitySink):
    """
    内存 sink——测试用 + W3 SessionManager 在没有 SQLite 时的 fallback

    @note 按 session_id 分组保存事件，便于回放断言
    """

    def __init__(self) -> None:
        self.events_by_session: dict[str, list[SessionEvent]] = defaultdict(list)

    async def consume(self, event: SessionEvent) -> None:
        self.events_by_session[event.session_id].append(event)

    def get_events(self, session_id: str) -> list[SessionEvent]:
        """返回指定 session 的事件流（按 seq 排序——一般已经有序）"""
        return sorted(
            self.events_by_session.get(session_id, []),
            key=lambda e: e.seq,
        )

    def all_sessions(self) -> list[str]:
        return list(self.events_by_session.keys())

    def clear(self) -> None:
        self.events_by_session.clear()
