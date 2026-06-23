"""
@module agent_swarm.web.state
@brief  P5-W28 WebState——Web UI 全局状态容器

挂载在 app.state.web_state, 路由通过 Depends 访问

P5-W33: 新增可选 store 参数——配置 store 后, push_event 自动双写
  - 内存 events (deque, 快速访问 + 订阅) —— 保持向后兼容
  - 持久化 store (Postgres / 内存) —— 重启可拉回
DSN 缺省时 store=None, 行为与 W28 完全一致 (零破坏)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_swarm.web.store import WebStateStore

log = logging.getLogger(__name__)


@dataclass
class EventRecord:
    """Web UI 事件流单条 (来自 SessionEvent)"""

    event_name: str
    session_id: str
    timestamp: float
    seq: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_html(self) -> str:
        """转 HTMX partial HTML"""
        ts = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        payload_str = str(self.payload)[:120] if self.payload else ""
        return (
            f'<li class="event event-{self.event_name}">'
            f'<span class="ts">{ts}</span> '
            f'<span class="name">{self.event_name}</span> '
            f'<span class="sid">{self.session_id[:8]}</span> '
            f'<span class="payload">{payload_str}</span>'
            f'</li>'
        )


@dataclass
class WebState:
    """
    Web UI 全局状态

    @param max_events  内存事件缓冲 (超出则丢老的)
    @param store       可选 WebStateStore——配置后 push_event 自动双写
                       (None = 纯内存, 与 W28 行为完全一致)
    """

    started_at: float = field(default_factory=time.time)
    events: deque[EventRecord] = field(
        default_factory=lambda: deque(maxlen=500),
    )
    active_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 订阅者 (WebSocket 列表)
    _subscribers: list[Any] = field(default_factory=list)
    # W33: 可选持久化 store (TYPE_CHECKING 引用避免 web/__init__.py 循环 import)
    store: "WebStateStore | None" = field(default=None)  # noqa: UP037

    async def push_event(
        self,
        event_name: str,
        session_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> None:
        """记录一条事件 + 通知订阅者 + 双写到 store (如有)"""
        rec = EventRecord(
            event_name=event_name,
            session_id=session_id,
            timestamp=time.time(),
            seq=seq,
            payload=payload,
        )
        async with self.lock:
            self.events.append(rec)
            # 更新 session 状态
            if session_id not in self.active_sessions:
                self.active_sessions[session_id] = {
                    "first_seen": time.time(),
                    "event_count": 0,
                    "last_event": None,
                }
            self.active_sessions[session_id]["event_count"] += 1
            self.active_sessions[session_id]["last_event"] = event_name
            subs = list(self._subscribers)
        # W33: 持久化双写 (失败仅记 log, 不影响内存路径)
        if self.store is not None:
            try:
                await self.store.append(event_name, session_id, seq, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "WebState store.append failed: event=%s err=%s",
                    event_name,
                    exc,
                )
        # 通知 (lock 外, 避免死锁)
        for sub in subs:
            try:
                await sub(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("subscriber notify failed: %s", exc)

    def subscribe(self, callback: Any) -> None:
        """订阅事件"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        """取消订阅"""
        import contextlib
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def recent_events(self, n: int = 50) -> list[EventRecord]:
        """最近 n 条事件 (新 → 旧)"""
        return list(reversed(list(self.events)[-n:]))

    def session_count(self) -> int:
        return len(self.active_sessions)

    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def events_by_type(self) -> dict[str, int]:
        """按 event_name 聚合"""
        out: dict[str, int] = {}
        for rec in self.events:
            out[rec.event_name] = out.get(rec.event_name, 0) + 1
        return out

