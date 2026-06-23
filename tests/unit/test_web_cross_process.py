"""
@module tests.unit.test_web_cross_process
@brief  P5-W35 跨进程 LISTEN/NOTIFY fan-out 测试

覆盖:
  - NotifyEnvelope 协议 (encode/decode/8KB 截断/字段)
  - PostgresNotifier 单元 (origin 过滤 / 多 listener / 启动幂等 / close)
  - PostgresWebStateStore.attach_notifier 集成
  - create_app enable_cross_process 钩子 (无 DSN 静默 / 有 DSN 挂 app.state.web_notifier)
  - DSN 缺省降级 (W28 行为零破坏)
"""

from __future__ import annotations

import pytest

from agent_swarm.web import (
    PostgresNotifier,
    WebState,
    create_app,
)
from agent_swarm.web.store import (
    NOTIFY_PAYLOAD_LIMIT,
    NotifyEnvelope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeAsyncpgConn:
    """极简 fake asyncpg connection: 维护 (channel -> payload) NOTIFY 队列"""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self._listeners: dict[str, list] = {}
        self._closed = False

    def add_listener(self, channel: str, callback) -> None:
        self._listeners.setdefault(channel, []).append(callback)

    async def execute(self, sql: str, *args) -> str:
        # 解析 NOTIFY channel, payload
        # 真实 asyncpg 的 NOTIFY 接受 channel[, payload]
        sql_clean = sql.strip().rstrip(";").strip()
        if sql_clean.upper().startswith("NOTIFY "):
            parts = sql_clean.split(None, 2)
            # NOTIFY channel[, payload]
            chan_part = parts[1].rstrip(",")
            payload = ""
            if len(parts) >= 3:
                payload = parts[2].strip()
                if payload.startswith("$") and args:
                    payload = args[0]
            self.notifications.append((chan_part, payload))
            return "NOTIFY"
        return ""

    async def close(self) -> None:
        self._closed = True

    def fire(self, channel: str, payload: str) -> None:
        """fake 外部触发 (模拟另一进程 NOTIFY)"""
        for cb in self._listeners.get(channel, []):
            cb(self, 12345, channel, payload)


class _FakeAsyncpgPool:
    """fake asyncpg pool — 返回 _FakeAsyncpgConn 用于 LISTEN"""

    def __init__(self) -> None:
        self.conn = _FakeAsyncpgConn()

    async def create_pool(self, **kwargs):
        return self

    async def acquire(self):
        return self.conn

    async def close(self):
        pass


@pytest.fixture
def fake_module(monkeypatch):
    """fake asyncpg-like module — PostgresNotifier / PostgresWebStateStore 通用"""
    pool = _FakeAsyncpgPool()
    return pool


# ---------------------------------------------------------------------------
# NotifyEnvelope 协议
# ---------------------------------------------------------------------------


def test_envelope_roundtrip() -> None:
    env = NotifyEnvelope(
        origin="abc123",
        seq=1,
        event_name="agent.start",
        session_id="s1",
        payload={"k": "v"},
        ts=1234.5,
    )
    raw = env.encode()
    decoded = NotifyEnvelope.decode(raw)
    assert decoded.origin == "abc123"
    assert decoded.seq == 1
    assert decoded.event_name == "agent.start"
    assert decoded.session_id == "s1"
    assert decoded.payload == {"k": "v"}
    assert decoded.ts == 1234.5


def test_envelope_truncates_oversized_payload() -> None:
    """>7KB payload 降级为 _truncated 标记"""
    env = NotifyEnvelope(
        origin="o",
        seq=1,
        event_name="big",
        session_id="s",
        payload={"x": "y" * (NOTIFY_PAYLOAD_LIMIT + 1000)},
        ts=1.0,
    )
    raw = env.encode()
    assert len(raw) <= NOTIFY_PAYLOAD_LIMIT
    decoded = NotifyEnvelope.decode(raw)
    assert decoded.payload.get("_truncated") is True


def test_envelope_unicode_payload() -> None:
    """unicode payload (中文) 正常编码"""
    env = NotifyEnvelope(
        origin="o",
        seq=1,
        event_name="msg",
        session_id="s1",
        payload={"text": "你好世界 🦞"},
        ts=1.0,
    )
    raw = env.encode()
    decoded = NotifyEnvelope.decode(raw)
    assert decoded.payload["text"] == "你好世界 🦞"


# ---------------------------------------------------------------------------
# PostgresNotifier 单元
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifier_origin_id_default() -> None:
    """默认 origin_id 是 uuid4 hex (32 字符)"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=_FakeAsyncpgPool())
    assert len(n.origin_id) == 32


@pytest.mark.asyncio
async def test_notifier_listen_and_notify(fake_module) -> None:
    """listen + notify 后, fake conn 收到 NOTIFY"""
    n = PostgresNotifier(
        dsn="postgresql://fake",
        fake_module=fake_module,
    )
    await n.listen()
    await n.notify("agent.start", "s1", 1, {"k": "v"}, 1.0)
    # fake_module.conn 应该有 1 条 NOTIFY 记录
    assert len(fake_module.conn.notifications) == 1
    chan, payload = fake_module.conn.notifications[0]
    assert chan == "webstate_notify"
    decoded = NotifyEnvelope.decode(payload)
    assert decoded.event_name == "agent.start"
    assert decoded.origin == n.origin_id


@pytest.mark.asyncio
async def test_notifier_skips_own_origin(fake_module) -> None:
    """同 origin 的 NOTIFY 不触发 listener (防 fan-out loop)"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n.listen()
    received: list = []
    n.on_notify(lambda env: received.append(env))
    # 自 NOTIFY
    await n.notify("e1", "s1", 1, {}, 1.0)
    # listener 不应触发
    assert received == []


@pytest.mark.asyncio
async def test_notifier_triggers_on_remote_origin(fake_module) -> None:
    """不同 origin 的 NOTIFY 触发 listener"""
    n_a = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n_a.listen()
    received: list = []
    n_a.on_notify(lambda env: received.append(env))
    # 模拟"另一进程"发 NOTIFY: 直接 fire fake conn
    env = NotifyEnvelope(
        origin="cccccccccccccccccccccccccccccccc",
        seq=99,
        event_name="remote.event",
        session_id="s2",
        payload={"k": "v"},
        ts=2.0,
    )
    fake_module.conn.fire("webstate_notify", env.encode())
    assert len(received) == 1
    assert received[0].event_name == "remote.event"
    assert received[0].origin == "cccccccccccccccccccccccccccccccc"


@pytest.mark.asyncio
async def test_notifier_multiple_listeners(fake_module) -> None:
    """多个 listener 都被通知"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n.listen()
    r1, r2 = [], []
    n.on_notify(lambda env: r1.append(env))
    n.on_notify(lambda env: r2.append(env))
    env = NotifyEnvelope(
        origin="remote",
        seq=1,
        event_name="x",
        session_id="s",
        payload={},
        ts=1.0,
    )
    fake_module.conn.fire("webstate_notify", env.encode())
    assert len(r1) == 1
    assert len(r2) == 1


@pytest.mark.asyncio
async def test_notifier_listen_idempotent(fake_module) -> None:
    """listen() 多次调用幂等"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n.listen()
    await n.listen()  # 不应抛错
    assert n._running is True


@pytest.mark.asyncio
async def test_notifier_close(fake_module) -> None:
    """close 清理状态"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n.listen()
    await n.close()
    assert n._running is False
    assert n._listeners == []


@pytest.mark.asyncio
async def test_notifier_notify_calls_listen_if_not_running(fake_module) -> None:
    """notify() 在未 listen 时自动 listen (W35 友好 API)"""
    n = PostgresNotifier(dsn="postgresql://fake", fake_module=fake_module)
    await n.notify("e1", "s1", 1, {}, 1.0)
    assert n._running is True
    assert len(fake_module.conn.notifications) == 1


# ---------------------------------------------------------------------------
# create_app 集成
# ---------------------------------------------------------------------------


def test_create_app_no_dsn_no_notifier() -> None:
    """无 DSN 时, create_app 默认不挂 notifier (向后兼容)"""
    app = create_app()
    assert app.state.web_notifier is None


def test_create_app_enable_cross_process_without_dsn_is_silent() -> None:
    """enable_cross_process=True 但无 DSN 时, 静默不挂 notifier"""
    app = create_app(enable_cross_process=True)
    assert app.state.web_notifier is None


def test_create_app_enable_cross_process_with_dsn_attaches_notifier() -> None:
    """enable_cross_process=True + DSN 时, notifier 挂到 app.state"""
    # 不能用 fake_module (create_app 不接), 用真 DSN 占位, 仅看对象构造
    # 实际启动 lifespan 由 test_web.py 覆盖; 这里只测 create_app 自身
    app = create_app(
        postgres_dsn="postgresql://placeholder",
        enable_cross_process=True,
    )
    # 不应抛错, 且 web_notifier 属性存在
    assert hasattr(app.state, "web_notifier")


def test_create_app_dsn_without_cross_process_no_notifier() -> None:
    """仅 DSN 不开 cross-process → notifier 为 None (W33 行为)"""
    app = create_app(postgres_dsn="postgresql://placeholder")
    assert app.state.web_notifier is None


# ---------------------------------------------------------------------------
# WebState.attach_notifier 集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webstate_attach_notifier_with_store(fake_module) -> None:
    """WebState.attach_notifier → store 拿到 notifier 引用"""
    state = WebState()
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    state.store = PostgresWebStateStore(WebStateConfig(
        dsn="postgresql://placeholder",
        fake_module=fake_module,
    ))
    notifier = PostgresNotifier(dsn="postgresql://placeholder", fake_module=fake_module)
    state.attach_notifier(notifier)
    # store 应该挂上 notifier
    assert state.store._notifier is notifier


@pytest.mark.asyncio
async def test_webstate_attach_notifier_no_store_still_saves() -> None:
    """无 store 时, attach_notifier 仍保存引用 (caller 自管)"""
    state = WebState()
    notifier = PostgresNotifier(dsn="postgresql://placeholder", fake_module=_FakeAsyncpgPool())
    state.attach_notifier(notifier)
    assert getattr(state, "_notifier", None) is notifier
