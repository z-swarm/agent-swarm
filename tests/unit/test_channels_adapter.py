"""单元测试：channels/adapter.py——DESIGN §4.3 ChannelAdapter"""

from __future__ import annotations

import time

import pytest

from agent_swarm.channels.adapter import (
    APIKeyStore,
    ChannelAdapter,
    RateLimiter,
    SessionBindingManager,
)
from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageType,
)


# RateLimiter
@pytest.mark.asyncio
async def test_rate_limiter_allows_within_window() -> None:
    rl = RateLimiter(max_count=3, window_seconds=10.0)
    for _ in range(3):
        assert await rl.allow("u1") is True
    assert await rl.allow("u1") is False


@pytest.mark.asyncio
async def test_rate_limiter_isolates_keys() -> None:
    rl = RateLimiter(max_count=2, window_seconds=10.0)
    assert await rl.allow("u1") is True
    assert await rl.allow("u1") is True
    assert await rl.allow("u1") is False
    assert await rl.allow("u2") is True
    assert await rl.allow("u2") is True


@pytest.mark.asyncio
async def test_rate_limiter_window_expiry() -> None:
    rl = RateLimiter(max_count=1, window_seconds=0.1)
    assert await rl.allow("u1") is True
    assert await rl.allow("u1") is False
    time.sleep(0.15)
    assert await rl.allow("u1") is True


@pytest.mark.asyncio
async def test_rate_limiter_reset() -> None:
    rl = RateLimiter(max_count=1, window_seconds=10.0)
    assert await rl.allow("u1") is True
    assert await rl.allow("u1") is False
    await rl.reset("u1")
    assert await rl.allow("u1") is True


# SessionBindingManager
def _user(uid: str = "u1") -> ChannelUser:
    return ChannelUser(channel=ChannelType.LARK, user_id=uid, display_name=uid)


def test_session_binding_basic() -> None:
    mgr = SessionBindingManager()
    u = _user()
    assert mgr.bind(u, "S1") is True
    binding = mgr.lookup(u)
    assert binding is not None
    assert binding.session_id == "S1"
    assert mgr.unbind(u, "S1") is True
    assert mgr.lookup(u) is None


def test_session_binding_idempotent_rebind() -> None:
    mgr = SessionBindingManager()
    u = _user()
    assert mgr.bind(u, "S1") is True
    assert mgr.bind(u, "S1") is True
    assert len(mgr.all_sessions(u)) == 1


def test_session_binding_max_sessions_enforced() -> None:
    mgr = SessionBindingManager(max_sessions_per_user=2)
    u = _user()
    assert mgr.bind(u, "S1") is True
    assert mgr.bind(u, "S2") is True
    assert mgr.bind(u, "S3") is False
    assert len(mgr.all_sessions(u)) == 2


def test_session_binding_lookup_returns_most_recent() -> None:
    mgr = SessionBindingManager()
    u = _user()
    mgr.bind(u, "S1")
    time.sleep(0.01)
    mgr.bind(u, "S2")
    assert mgr.lookup(u).session_id == "S2"


# APIKeyStore
def test_api_key_store_register_and_lookup() -> None:
    store = APIKeyStore()
    store.register("k1", "user-1")
    assert store.lookup("k1") == "user-1"
    assert store.lookup("unknown") is None


# ChannelAdapter
class _StubConnector(ChannelConnector):
    def __init__(self, channel_type: ChannelType) -> None:
        self._type = channel_type
        self.handlers: list = []
        self.sent: list = []

    @property
    def channel_type(self) -> ChannelType:
        return self._type

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, response, target) -> bool:  # type: ignore[override]
        self.sent.append((response, target))
        return True

    def subscribe(self, handler) -> None:
        self.handlers.append(handler)

    def unsubscribe(self, handler) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)


def _make_msg(content: str, uid: str = "u1") -> ChannelMessage:
    return ChannelMessage(
        id="m1", channel=ChannelType.LARK,
        from_user=_user(uid), content=content,
    )


@pytest.mark.asyncio
async def test_channel_adapter_register_connector() -> None:
    a = ChannelAdapter()
    c = _StubConnector(ChannelType.LARK)
    a.register_connector(c)
    assert c in a._connectors.values()
    assert a._dispatch_message in c.handlers


@pytest.mark.asyncio
async def test_channel_adapter_dispatches_to_handler() -> None:
    a = ChannelAdapter()
    c = _StubConnector(ChannelType.LARK)
    a.register_connector(c)

    called: list[ChannelMessage] = []

    async def my_handler(msg: ChannelMessage) -> ChannelResponse:
        called.append(msg)
        return ChannelResponse(content="echo:" + msg.content)

    a.set_handler(my_handler)
    response = await a._dispatch_message(_make_msg("hello"))
    assert response.content == "echo:hello"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_channel_adapter_no_handler_returns_denied() -> None:
    a = ChannelAdapter()
    response = await a._dispatch_message(_make_msg("x"))
    assert "[denied]" in response.content
    assert "no_handler_registered" in response.content


@pytest.mark.asyncio
async def test_channel_adapter_whitelist_blocks() -> None:
    a = ChannelAdapter(user_whitelist={"allowed"})
    response = await a._dispatch_message(_make_msg("x", uid="blocked"))
    assert "not_in_whitelist" in response.content
    response2 = await a._dispatch_message(_make_msg("x", uid="allowed"))
    assert "not_in_whitelist" not in response2.content


@pytest.mark.asyncio
async def test_channel_adapter_rate_limit_blocks() -> None:
    a = ChannelAdapter(messages_per_minute=2, user_whitelist={"u1"})

    async def echo(msg):
        return ChannelResponse(content="ok")

    a.set_handler(echo)
    await a._dispatch_message(_make_msg("a", uid="u1"))
    await a._dispatch_message(_make_msg("b", uid="u1"))
    response = await a._dispatch_message(_make_msg("c", uid="u1"))
    assert "rate_limited" in response.content


@pytest.mark.asyncio
async def test_channel_adapter_denied_handler_custom() -> None:
    a = ChannelAdapter()

    def custom_denied(msg, reason):
        return ChannelResponse(
            content=f"NOPE: {reason}",
            msg_type=MessageType.CARD,
            card_template="confirm_dialog",
        )

    a.set_denied_handler(custom_denied)
    response = await a._dispatch_message(_make_msg("x"))
    assert response.content == "NOPE: no_handler_registered"
    assert response.msg_type == MessageType.CARD


@pytest.mark.asyncio
async def test_channel_adapter_send_via_user() -> None:
    a = ChannelAdapter()
    c = _StubConnector(ChannelType.LARK)
    a.register_connector(c)
    u = _user("u1")
    resp = ChannelResponse(content="hi")
    ok = await a.send(resp, u)
    assert ok is True
    assert len(c.sent) == 1


@pytest.mark.asyncio
async def test_channel_adapter_send_string_requires_channel_type() -> None:
    a = ChannelAdapter()
    c = _StubConnector(ChannelType.LARK)
    a.register_connector(c)
    with pytest.raises(ValueError, match="requires channel_type"):
        await a.send(ChannelResponse(content="x"), "u1")


@pytest.mark.asyncio
async def test_channel_adapter_send_no_connector_returns_false() -> None:
    a = ChannelAdapter()
    u = _user("u1")
    ok = await a.send(ChannelResponse(content="x"), u)
    assert ok is False


def test_channel_adapter_add_remove_whitelist() -> None:
    a = ChannelAdapter(user_whitelist={"a"})
    assert "a" in a._user_whitelist
    a.add_whitelist_user("b")
    assert "b" in a._user_whitelist
    a.remove_whitelist_user("a")
    assert "a" not in a._user_whitelist
    a.remove_whitelist_user("nonexistent")
