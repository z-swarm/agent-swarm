"""
@module tests.golden.test_g025_cross_process
@brief  P5-W35 G-025 Golden Case — 跨进程 LISTEN/NOTIFY fan-out 端到端

@note 真实环境: 两个 agent-swarm 实例共享同一 Postgres DSN,
      实例 A push_event → NOTIFY → 实例 B 实时收到 (跨进程, 跨内存)

@note 测试环境: 用 fake asyncpg module (W25 风格) 模拟 NOTIFY bus
      两个 PostgresNotifier 实例共享 fake conn, 模拟"两个进程"
"""

from __future__ import annotations

import pytest

from agent_swarm.web import (
    PostgresNotifier,
)
from agent_swarm.web.store import (
    NotifyEnvelope,
)

# ---------------------------------------------------------------------------
# 共享 fake bus — 模拟 Postgres LISTEN/NOTIFY
# ---------------------------------------------------------------------------


class _FakeAsyncpgConn:
    """模拟 asyncpg connection 的 NOTIFY/LISTEN 行为"""

    def __init__(self, bus: _FakeBus) -> None:
        self.bus = bus
        self._listeners: dict[str, list] = {}

    def add_listener(self, channel: str, callback) -> None:
        self._listeners.setdefault(channel, []).append(callback)
        self.bus.register(channel, self, callback)

    async def execute(self, sql: str, *args) -> str:
        sql_clean = sql.strip().rstrip(";").strip()
        if sql_clean.upper().startswith("NOTIFY "):
            parts = sql_clean.split(None, 2)
            chan = parts[1].rstrip(",")
            payload = ""
            if len(parts) >= 3:
                payload = parts[2].strip()
                if payload.startswith("$") and args:
                    payload = args[0]
            # bus fan-out: 给所有注册了该 channel 的 conn (除 self) 触发
            await self.bus.notify(chan, payload, exclude=self)
            return "NOTIFY"
        return ""

    async def close(self) -> None:
        for chan in list(self._listeners):
            self.bus.unregister(chan, self)


class _FakeBus:
    """共享 bus: 多个 _FakeAsyncpgConn 注册 listener, NOTIFY 触发其他 conn"""

    def __init__(self) -> None:
        self._registry: dict[str, list[tuple[_FakeAsyncpgConn, callable]]] = {}
        self.sent: list[tuple[str, str, int]] = []  # (chan, payload, sender_id)

    def register(self, channel: str, conn: _FakeAsyncpgConn, callback) -> None:
        self._registry.setdefault(channel, []).append((conn, callback))

    def unregister(self, channel: str, conn: _FakeAsyncpgConn) -> None:
        for entry in self._registry.get(channel, []):
            if entry[0] is conn:
                self._registry[channel].remove(entry)

    async def notify(self, channel: str, payload: str, exclude: _FakeAsyncpgConn) -> None:
        # sender_id 用来 trace (实际 asyncpg 没有, 我们用 payload origin 区分)
        self.sent.append((channel, payload[:40], id(exclude)))
        for entry in list(self._registry.get(channel, [])):
            conn, cb = entry
            if conn is exclude:
                continue
            cb(conn, 99999, channel, payload)


class _FakeAsyncpgPool:
    """fake asyncpg pool: 每个 acquire 返同一个 conn (LISTEN 需要长连接)"""

    def __init__(self, bus: _FakeBus) -> None:
        self.bus = bus
        self._shared_conn: _FakeAsyncpgConn | None = None

    async def create_pool(self, **kwargs):
        return self

    async def acquire(self):
        if self._shared_conn is None:
            self._shared_conn = _FakeAsyncpgConn(self.bus)
        return self._shared_conn

    async def close(self):
        if self._shared_conn is not None:
            await self._shared_conn.close()
        self._shared_conn = None


# ---------------------------------------------------------------------------
# Golden Case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g025_cross_process_notify_roundtrip() -> None:
    """
    G-025: 进程 A push → 进程 B 实时收到 (跨"进程" fan-out)

    设置:
      - 共享 fake_bus 模拟 PG bus
      - 进程 A: PostgresNotifier(DSN, fake_module_using_bus)
      - 进程 B: 同上, 不同 origin_id
      - A 调 notifier.notify(...) → bus 触发 B 的 listener
    """
    bus = _FakeBus()
    pool_a = _FakeAsyncpgPool(bus)
    pool_b = _FakeAsyncpgPool(bus)

    notifier_a = PostgresNotifier(
        dsn="postgresql://fake",
        origin_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fake_module=pool_a,
    )
    notifier_b = PostgresNotifier(
        dsn="postgresql://fake",
        origin_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        fake_module=pool_b,
    )
    await notifier_a.listen()
    await notifier_b.listen()

    # B 订阅
    received_b: list[NotifyEnvelope] = []
    notifier_b.on_notify(lambda env: received_b.append(env))

    # A 推一条事件
    await notifier_a.notify(
        event_name="agent.start",
        session_id="s-A",
        seq=1,
        payload={"agent_id": "a1"},
        ts=1234.5,
    )

    # B 应该收到
    assert len(received_b) == 1
    env = received_b[0]
    assert env.event_name == "agent.start"
    assert env.origin == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert env.payload == {"agent_id": "a1"}


@pytest.mark.asyncio
async def test_g025_no_self_loop_in_broadcast() -> None:
    """G-025 边界: 同一 notifier 发 + 收, 不应触发自己的 listener"""
    bus = _FakeBus()
    pool = _FakeAsyncpgPool(bus)
    notifier = PostgresNotifier(dsn="postgresql://fake", fake_module=pool)
    await notifier.listen()
    received: list = []
    notifier.on_notify(lambda env: received.append(env))
    await notifier.notify("e1", "s1", 1, {}, 1.0)
    # 自己的 notify 不触发自己
    assert received == []


@pytest.mark.asyncio
async def test_g025_multi_process_fanout() -> None:
    """G-025 多进程: A 推 → B + C 同时收到"""
    bus = _FakeBus()
    pools = [_FakeAsyncpgPool(bus) for _ in range(3)]
    notifiers = [
        PostgresNotifier(
            dsn="postgresql://fake",
            origin_id=f"proc_{i}_" + "a" * 26,
            fake_module=pools[i],
        )
        for i in range(3)
    ]
    for n in notifiers:
        await n.listen()

    received = {0: [], 1: [], 2: []}
    for i, n in enumerate(notifiers):
        n.on_notify(lambda env, idx=i: received[idx].append(env))

    # proc 0 推
    await notifiers[0].notify("multi.test", "s0", 1, {"from": "proc0"}, 1.0)
    # proc 1 + 2 应收到, proc 0 不应收到自己
    assert len(received[0]) == 0
    assert len(received[1]) == 1
    assert len(received[2]) == 1
    assert received[1][0].origin.startswith("proc_0_")
    assert received[2][0].origin.startswith("proc_0_")


@pytest.mark.asyncio
async def test_g025_three_procs_sequential_notify() -> None:
    """G-025 顺序: 三进程各推一条, 每个进程收到 2 条 (来自其他两进程)"""
    bus = _FakeBus()
    pools = [_FakeAsyncpgPool(bus) for _ in range(3)]
    notifiers = [
        PostgresNotifier(
            dsn="postgresql://fake",
            origin_id=f"p{i}",
            fake_module=pools[i],
        )
        for i in range(3)
    ]
    for n in notifiers:
        await n.listen()
    received: list[list[NotifyEnvelope]] = [[], [], []]
    for i, n in enumerate(notifiers):
        n.on_notify(lambda env, idx=i: received[idx].append(env))
    for i, n in enumerate(notifiers):
        await n.notify(f"e{i}", f"s{i}", i, {"from": i}, float(i))
    for i in range(3):
        assert len(received[i]) == 2, f"proc {i} should receive 2"
        origins = {e.origin for e in received[i]}
        # 各进程应收到除自己外的两条
        expected_origins = {f"p{j}" for j in range(3) if j != i}
        assert origins == expected_origins
