"""
@module agent_swarm.channels.adapter
@brief  ChannelAdapter——DESIGN §4.3 统一路由 + 鉴权 + 限流
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageHandler,
    MessageType,
)

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_count: int, window_seconds: float) -> None:
        self.max_count = max_count
        self.window = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        async with self._lock:
            q = self._events.setdefault(key, deque())
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_count:
                return False
            q.append(now)
            return True

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._events.pop(key, None)


@dataclass
class SessionBinding:
    user: ChannelUser
    session_id: str
    bound_at: float = field(default_factory=time.time)
    message_count: int = 0


class SessionBindingManager:
    def __init__(self, max_sessions_per_user: int = 5) -> None:
        self._bindings: dict[str, dict[str, SessionBinding]] = {}
        self._max = max_sessions_per_user

    def bind(self, user: ChannelUser, session_id: str) -> bool:
        key = f"{user.channel.value}:{user.user_id}"
        user_bindings = self._bindings.setdefault(key, {})
        if session_id in user_bindings:
            return True
        if len(user_bindings) >= self._max:
            return False
        user_bindings[session_id] = SessionBinding(user=user, session_id=session_id)
        return True

    def unbind(self, user: ChannelUser, session_id: str) -> bool:
        key = f"{user.channel.value}:{user.user_id}"
        user_bindings = self._bindings.get(key, {})
        return user_bindings.pop(session_id, None) is not None

    def lookup(self, user: ChannelUser) -> SessionBinding | None:
        key = f"{user.channel.value}:{user.user_id}"
        user_bindings = self._bindings.get(key, {})
        if not user_bindings:
            return None
        return max(user_bindings.values(), key=lambda b: b.bound_at)

    def all_sessions(self, user: ChannelUser) -> list[SessionBinding]:
        key = f"{user.channel.value}:{user.user_id}"
        user_bindings = self._bindings.get(key, {})
        return sorted(user_bindings.values(), key=lambda b: b.bound_at, reverse=True)


class APIKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def register(self, api_key: str, user_id: str) -> None:
        self._keys[api_key] = user_id

    def lookup(self, api_key: str) -> str | None:
        return self._keys.get(api_key)


class ChannelAdapter:
    def __init__(
        self,
        messages_per_minute: int = 30,
        sessions_per_hour: int = 10,
        max_sessions_per_user: int = 5,
        user_whitelist: set[str] | None = None,
    ) -> None:
        self._connectors: dict[ChannelType, ChannelConnector] = {}
        self._bindings = SessionBindingManager(max_sessions_per_user)
        self._rate_msg = RateLimiter(messages_per_minute, window_seconds=60.0)
        self._rate_session = RateLimiter(sessions_per_hour, window_seconds=3600.0)
        self._user_whitelist = user_whitelist or set()
        self._api_keys = APIKeyStore()
        self._handler: MessageHandler | None = None
        self._denied_handler: Callable[[ChannelMessage, str], ChannelResponse] | None = None

    def register_connector(self, connector: ChannelConnector) -> None:
        self._connectors[connector.channel_type] = connector
        connector.subscribe(self._dispatch_message)
        log.info("channel_adapter.registered type=%s", connector.channel_type.value)

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_denied_handler(self, fn: Callable[[ChannelMessage, str], ChannelResponse]) -> None:
        self._denied_handler = fn

    def api_key_store(self) -> APIKeyStore:
        return self._api_keys

    def add_whitelist_user(self, user_id: str) -> None:
        self._user_whitelist.add(user_id)

    def remove_whitelist_user(self, user_id: str) -> None:
        self._user_whitelist.discard(user_id)

    def bind_user_session(self, user: ChannelUser, session_id: str) -> bool:
        return self._bindings.bind(user, session_id)

    def unbind_user_session(self, user: ChannelUser, session_id: str) -> bool:
        return self._bindings.unbind(user, session_id)

    def lookup_user_session(self, user: ChannelUser) -> SessionBinding | None:
        return self._bindings.lookup(user)

    async def _dispatch_message(self, msg: ChannelMessage) -> ChannelResponse:
        key = f"{msg.from_user.channel.value}:{msg.from_user.user_id}"
        if not await self._rate_msg.allow(key):
            return self._make_denied_response(msg, "rate_limited")
        if self._user_whitelist and msg.from_user.user_id not in self._user_whitelist:
            return self._make_denied_response(msg, "not_in_whitelist")
        if self._handler is None:
            return self._make_denied_response(msg, "no_handler_registered")
        response = await self._handler(msg)
        return response

    def _make_denied_response(
        self,
        msg: ChannelMessage,
        reason: str,
    ) -> ChannelResponse:
        if self._denied_handler is not None:
            return self._denied_handler(msg, reason)
        return ChannelResponse(
            content=f"[denied] {reason}",
            msg_type=MessageType.TEXT,
            reply_to=msg.id,
        )

    async def send(
        self,
        response: ChannelResponse,
        target: ChannelUser | str,
        channel_type: ChannelType | None = None,
    ) -> bool:
        if isinstance(target, ChannelUser):
            ct = target.channel if channel_type is None else channel_type
        else:
            if channel_type is None:
                raise ValueError("send() with string target requires channel_type argument")
            ct = channel_type
        connector = self._connectors.get(ct)
        if connector is None:
            log.warning("channel_adapter.send_no_connector type=%s", ct.value)
            return False
        return await connector.send(response, target)

    async def start_all(self) -> None:
        for ct, conn in self._connectors.items():
            try:
                await conn.start()
                log.info("channel_adapter.started type=%s", ct.value)
            except Exception as exc:  # noqa: BLE001
                log.warning("channel_adapter.start_failed type=%s err=%s", ct.value, exc)

    async def stop_all(self) -> None:
        for ct, conn in self._connectors.items():
            try:
                await conn.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("channel_adapter.stop_failed type=%s err=%s", ct.value, exc)


__all__ = [
    "APIKeyStore",
    "ChannelAdapter",
    "RateLimiter",
    "SessionBinding",
    "SessionBindingManager",
]
