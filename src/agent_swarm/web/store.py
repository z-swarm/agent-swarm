"""
@module agent_swarm.web.store
@brief  P5-W33 WebStateStore 协议 + Postgres 实现

DESIGN §17.2 P5-W33 DoD 拆解:
  - D1 协议 (append/recent/subscribe) + Postgres 实现, 匹配 WebState 内存 API
  - D2 Schema: webstate_events(seq BIGSERIAL PK, ts, event_type, payload JSONB, session_id, tenant_id)
              + 3 索引 (ts DESC / session_id+seq / tenant_id+ts DESC)
  - 复用 W25 fake_module 注入模式 (零真 PG 依赖)
  - 单进程内存 subscribe (跨进程 fan-out 受 PG 限制, 见 W33 Plan R4)

@note 与 WebState.push_event 行为兼容: append 内部广播给本地订阅者
      跨进程 fan-out 是 P5 §17.2 已知限制, 后续 W34+ 加 LISTEN/NOTIFY
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema + 索引 SQL
# ---------------------------------------------------------------------------

# seq 用 BIGSERIAL 自动递增, 索引按 ts DESC 利于 "recent n 条"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    seq         BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    session_id  TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS {table}_ts_idx
    ON {table} (ts DESC);
CREATE INDEX IF NOT EXISTS {table}_session_seq_idx
    ON {table} (session_id, seq);
CREATE INDEX IF NOT EXISTS {table}_tenant_ts_idx
    ON {table} (tenant_id, ts DESC);
"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WebStateConfig:
    """
    @brief WebStateStore 配置

    @param dsn         Postgres DSN (None/空 → 走内存 store)
    @param table       表名 (默认 webstate_events)
    @param min_size    asyncpg pool min
    @param max_size    asyncpg pool max
    @param command_timeout  asyncpg command timeout (秒)
    @param fake_module 测试用: 注入 fake asyncpg-like module
    @param tenant_id   默认 tenant (多租户隔离; 默认 'local')
    """

    dsn: str | None = None
    table: str = "webstate_events"
    min_size: int = 1
    max_size: int = 5
    command_timeout: float = 5.0
    fake_module: Any = None
    tenant_id: str = "local"


# ---------------------------------------------------------------------------
# Store 协议
# ---------------------------------------------------------------------------


@runtime_checkable
class WebStateStore(Protocol):
    """
    @brief WebStateStore 协议——内存 / Postgres 都实现同一接口

    行为契约 (与 WebState 内存版 push_event 兼容):
      - append(): 写入并广播给本地订阅者 (单进程 dict; 跨进程 fan-out 是 P5 §17.2 已知限制)
      - recent(): 按时间倒序取最近 n 条, 可选 session_id 过滤
      - subscribe(): 注册回调, 后续 append 会通知
    """

    async def append(
        self,
        event_name: str,
        session_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> None: ...

    async def recent(
        self,
        n: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def subscribe(self, callback: Callable[..., Any]) -> None: ...

    def unsubscribe(self, callback: Callable[..., Any]) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 内存实现 (降级默认)
# ---------------------------------------------------------------------------


@dataclass
class MemoryWebStateStore:
    """
    @brief 内存 WebStateStore——DSN 缺省时的零破坏降级

    @param max_events 内存环形缓冲大小 (超出丢老)
    """

    max_events: int = 500
    _events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=500),
    )
    _subscribers: list[Callable[..., Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        # maxlen 由 field default_factory 固定, 但允许 max_events 调整后重新构造
        if self.max_events != self._events.maxlen:
            self._events = deque(self._events, maxlen=self.max_events)

    async def append(
        self,
        event_name: str,
        session_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> None:
        rec: dict[str, Any] = {
            "event_name": event_name,
            "session_id": session_id,
            "seq": seq,
            "timestamp": time.time(),
            "payload": dict(payload) if payload else {},
        }
        async with self._lock:
            self._events.append(rec)
            subs = list(self._subscribers)
        for cb in subs:
            try:
                await cb(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("subscriber notify failed: %s", exc)

    async def recent(
        self,
        n: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        snapshot = list(self._events)
        if session_id is not None:
            snapshot = [r for r in snapshot if r["session_id"] == session_id]
        return list(reversed(snapshot[-n:]))

    def subscribe(self, callback: Callable[..., Any]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[..., Any]) -> None:
        with __import__("contextlib").suppress(ValueError):
            self._subscribers.remove(callback)

    async def close(self) -> None:
        self._subscribers.clear()


# ---------------------------------------------------------------------------
# Postgres 实现
# ---------------------------------------------------------------------------


class PostgresWebStateStore:
    """
    @brief Postgres WebStateStore——生产级持久化

    复用 W25 PostgresBackend 的 fake_module 注入模式:
      - 测试: 注入 fake_module, 用 dict 模拟 pool
      - 生产: 真 asyncpg pool
    Schema 与 SCHEMA_SQL 一致, _init_schema() 幂等创建表 + 3 索引

    已知限制 (P5 §17.2):
      - subscribe 仅对**当前进程**有效 (PG 无 in-memory pub/sub)
      - 跨进程实时 fan-out 需 W34+ 加 LISTEN/NOTIFY
      - 本类只保证 "重启不丢事件" (append 落盘, recent 重启后能拉回)
    """

    def __init__(self, config: WebStateConfig | None = None) -> None:
        self.config = config or WebStateConfig()
        self._pool: Any = None
        self._initialized = False
        self._init_lock: asyncio.Lock | None = None
        self._subscribers: list[Callable[..., Any]] = []
        self._subs_lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._initialized:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._initialized:
                return
            if self.config.fake_module is not None:
                self._pool = await self.config.fake_module.create_pool(
                    dsn=self.config.dsn or "postgresql://fake",
                    min_size=self.config.min_size,
                    max_size=self.config.max_size,
                    command_timeout=self.config.command_timeout,
                )
            else:
                import asyncpg
                self._pool = await asyncpg.create_pool(
                    dsn=self.config.dsn,
                    min_size=self.config.min_size,
                    max_size=self.config.max_size,
                    command_timeout=self.config.command_timeout,
                )
            await self._init_schema()
            self._initialized = True

    async def _init_schema(self) -> None:
        sql = SCHEMA_SQL.format(table=self.config.table)
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def append(
        self,
        event_name: str,
        session_id: str,
        seq: int,
        payload: dict[str, Any],
    ) -> None:
        await self._ensure_connected()
        sql = (
            f"INSERT INTO {self.config.table} "
            f"(event_type, payload, session_id, tenant_id) "
            f"VALUES ($1, $2, $3, $4)"
        )
        payload_json = json.dumps(payload or {})
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql, event_name, payload_json, session_id, self.config.tenant_id,
            )
        # 通知本地订阅者 (单进程 fan-out)
        rec: dict[str, Any] = {
            "event_name": event_name,
            "session_id": session_id,
            "seq": seq,
            "timestamp": time.time(),
            "payload": dict(payload) if payload else {},
        }
        async with self._subs_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                await cb(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("subscriber notify failed: %s", exc)

    async def recent(
        self,
        n: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        # 按 ts DESC 取最近 n; session_id 过滤走索引
        if session_id is not None:
            sql = (
                f"SELECT seq, ts, event_type, payload, session_id, tenant_id "
                f"FROM {self.config.table} WHERE session_id = $1 "
                f"ORDER BY ts DESC LIMIT $2"
            )
            args: tuple[Any, ...] = (session_id, n)
        else:
            sql = (
                f"SELECT seq, ts, event_type, payload, session_id, tenant_id "
                f"FROM {self.config.table} "
                f"ORDER BY ts DESC LIMIT $1"
            )
            args = (n,)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        out: list[dict[str, Any]] = []
        for row in rows:
            data = row["payload"]
            if isinstance(data, str):
                data = json.loads(data)
            ts = row["ts"]
            ts_float = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
            out.append({
                "seq": int(row["seq"]),
                "timestamp": ts_float,
                "event_name": row["event_type"],
                "session_id": row["session_id"],
                "tenant_id": row["tenant_id"],
                "payload": data,
            })
        # recent 返回按时间正序 (新→旧为 reversed, 但 ORDER BY DESC + list 已新→旧)
        # 调用方期望"新→旧", 已是正确顺序
        return out

    def subscribe(self, callback: Callable[..., Any]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[..., Any]) -> None:
        import contextlib
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    async def close(self) -> None:
        if self._pool is not None and self.config.fake_module is None:
            await self._pool.close()
        self._initialized = False
        self._subscribers.clear()


__all__ = [
    "WebStateConfig",
    "WebStateStore",
    "MemoryWebStateStore",
    "PostgresWebStateStore",
    "SCHEMA_SQL",
]
