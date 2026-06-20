"""
@module agent_swarm.observability.sqlite_sink
@brief  SQLite EventSink——事件持久化（W3 + W5 多租户隔离）

DESIGN.md §5.4 / §8.4 / §10:
  - 事件流是 Session 恢复的唯一来源
  - 多租户隔离：所有 SQL 强制带 WHERE tenant_id = ?
  - tenant_id 从 SecurityContextManager.current_or_default() 隐式获取

Schema (W5 升级, V2):
  CREATE TABLE session_events (
      tenant_id  TEXT NOT NULL,                  -- 多租户隔离
      session_id TEXT NOT NULL,
      seq        INTEGER NOT NULL,
      event_name TEXT NOT NULL,
      timestamp  REAL NOT NULL,
      payload    TEXT NOT NULL,
      request_id TEXT,
      PRIMARY KEY (tenant_id, session_id, seq)
  );

  CREATE TABLE sessions (
      tenant_id   TEXT NOT NULL,                  -- 多租户隔离
      session_id  TEXT NOT NULL,
      swarm_name  TEXT NOT NULL,
      created_at  REAL NOT NULL,
      ended_at    REAL,
      state       TEXT,
      config_yaml TEXT,
      PRIMARY KEY (tenant_id, session_id)
  );

升级策略（Phase 1 -> 多租户零代码改动）:
  - 检测旧 V1 schema 存在 -> 创建 V2 临时表 + 数据迁移 + drop 旧表
  - tenant_id 默认 'local' (W3 阶段所有 session 都落 local)

WAL 模式 + synchronous=NORMAL 平衡安全/性能（DESIGN.md §12.4）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink

log = logging.getLogger(__name__)


# V2 schema——多租户隔离
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS session_events (
    tenant_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    payload    TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY (tenant_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_tenant_session
    ON session_events(tenant_id, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_tenant_name
    ON session_events(tenant_id, event_name);
-- W12-2 完整事件目录：五元组索引
-- (session_id, tenant_id, event_name, seq, request_id)
CREATE INDEX IF NOT EXISTS idx_events_5tuple
    ON session_events(session_id, tenant_id, event_name, seq, request_id);
-- 时间范围查询（回放 UI 用）
CREATE INDEX IF NOT EXISTS idx_events_tenant_time
    ON session_events(tenant_id, session_id, timestamp);
-- request_id 关联审计
CREATE INDEX IF NOT EXISTS idx_events_request_id
    ON session_events(tenant_id, request_id) WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    tenant_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    swarm_name  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    ended_at    REAL,
    state       TEXT,
    config_yaml TEXT,
    PRIMARY KEY (tenant_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_created
    ON sessions(tenant_id, created_at DESC);
"""


# V1 → V2 migration SQL（仅在检测到旧 schema 时执行）
_MIGRATE_V1_V2 = """
CREATE TABLE IF NOT EXISTS session_events_v2 (
    tenant_id  TEXT NOT NULL DEFAULT 'local',
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    payload    TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY (tenant_id, session_id, seq)
);
INSERT OR IGNORE INTO session_events_v2
    (tenant_id, session_id, seq, event_name, timestamp, payload, request_id)
    SELECT 'local', session_id, seq, event_name, timestamp, payload, request_id
    FROM session_events;
DROP TABLE IF EXISTS session_events;
ALTER TABLE session_events_v2 RENAME TO session_events;
CREATE INDEX IF NOT EXISTS idx_events_tenant_session
    ON session_events(tenant_id, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_tenant_name
    ON session_events(tenant_id, event_name);

CREATE TABLE IF NOT EXISTS sessions_v2 (
    tenant_id   TEXT NOT NULL DEFAULT 'local',
    session_id  TEXT NOT NULL,
    swarm_name  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    ended_at    REAL,
    state       TEXT,
    config_yaml TEXT,
    PRIMARY KEY (tenant_id, session_id)
);
INSERT OR IGNORE INTO sessions_v2
    (tenant_id, session_id, swarm_name, created_at, ended_at, state, config_yaml)
    SELECT 'local', session_id, swarm_name, created_at, ended_at, state, config_yaml
    FROM sessions;
DROP TABLE IF EXISTS sessions;
ALTER TABLE sessions_v2 RENAME TO sessions;
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_created
    ON sessions(tenant_id, created_at DESC);
"""


def _current_tenant_id() -> str:
    """@brief 从 SecurityContextManager 取当前 tenant_id（DESIGN §8.4 隐式）"""
    from agent_swarm.security.context import SecurityContextManager

    return SecurityContextManager.current_or_default().tenant_id


class SqliteEventSink(ObservabilitySink):
    """
    @brief SQLite 事件持久化 sink（W5 多租户隔离版）

    所有 SQL 自动带 tenant_id 隔离（DESIGN §8.4）
    """

    def __init__(self, db_path: str | Path) -> None:
        # ``:memory:`` 是 SQLite 内存库特殊标识——不能 resolve 成物理文件
        if isinstance(db_path, str) and db_path == ":memory:":
            self.db_path: Path | str = ":memory:"
        else:
            self.db_path = Path(db_path).resolve()
        self._conn: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._migrated = False

    async def _ensure_conn(self) -> aiosqlite.Connection:
        """@brief 懒连接 + schema + V1→V2 migration"""
        if self._conn is not None and self._migrated:
            return self._conn
        async with self._init_lock:
            if self._conn is not None and self._migrated:
                return self._conn
            if str(self.db_path) != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self.db_path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA cache_size=-64000")
            if not self._migrated:
                await self._migrate_v1_to_v2(conn)
                self._migrated = True
            await conn.executescript(_SCHEMA_V2)
            await conn.commit()
            log.info("SqliteEventSink connected to %s (tenant_id=%s)",
                     self.db_path, _current_tenant_id())
            self._conn = conn
            return conn

    async def _migrate_v1_to_v2(self, conn: aiosqlite.Connection) -> None:
        """
        @brief 检测 V1 schema 存在则执行迁移

        V1 特征: sessions 表无 tenant_id 列
        """
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        async with conn.execute("PRAGMA table_info(sessions)") as cur:
            cols = {r[1] async for r in cur}
        if "tenant_id" in cols:
            return
        log.info("SqliteEventSink: 检测到 V1 schema, 执行 V1→V2 migration")
        await conn.executescript(_MIGRATE_V1_V2)
        await conn.commit()

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def consume(self, event: SessionEvent) -> None:
        """@brief 写入事件——tenant_id 隐式从 SecurityContextManager 取"""
        try:
            conn = await self._ensure_conn()
            tenant = _current_tenant_id()
            await conn.execute(
                "INSERT OR REPLACE INTO session_events "
                "(tenant_id, session_id, seq, event_name, timestamp, payload, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant,
                    event.session_id,
                    event.seq,
                    event.event_name,
                    event.timestamp,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                    event.request_id,
                ),
            )
            await conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("SqliteEventSink consume failed: %s", exc)

    async def register_session(
        self,
        session_id: str,
        swarm_name: str,
        config_yaml: str | None = None,
    ) -> None:
        """@brief SessionManager.create_session 调用——tenant_id 隐式取"""
        conn = await self._ensure_conn()
        tenant = _current_tenant_id()
        await conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(tenant_id, session_id, swarm_name, created_at, config_yaml) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant, session_id, swarm_name, time.time(), config_yaml),
        )
        await conn.commit()

    async def end_session(self, session_id: str, state: str) -> None:
        """@brief 标记 session 结束——只更新当前 tenant 的"""
        conn = await self._ensure_conn()
        tenant = _current_tenant_id()
        await conn.execute(
            "UPDATE sessions SET ended_at=?, state=? "
            "WHERE tenant_id=? AND session_id=?",
            (time.time(), state, tenant, session_id),
        )
        await conn.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = await self._ensure_conn()
        tenant = _current_tenant_id()
        async with conn.execute(
            "SELECT session_id, swarm_name, created_at, ended_at, state, config_yaml "
            "FROM sessions WHERE tenant_id=? AND session_id=?",
            (tenant, session_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "session_id": row[0],
            "swarm_name": row[1],
            "created_at": row[2],
            "ended_at": row[3],
            "state": row[4],
            "config_yaml": row[5],
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        """@brief 列出当前 tenant 的全部 session——F-02 越权防护"""
        conn = await self._ensure_conn()
        tenant = _current_tenant_id()
        async with conn.execute(
            "SELECT session_id, swarm_name, created_at, ended_at, state "
            "FROM sessions WHERE tenant_id=? ORDER BY created_at DESC",
            (tenant,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "session_id": r[0],
                "swarm_name": r[1],
                "created_at": r[2],
                "ended_at": r[3],
                "state": r[4],
            }
            for r in rows
        ]

    async def get_events(self, session_id: str) -> list[SessionEvent]:
        """
        @brief 按 (tenant_id, session_id) 读取事件流——F-02 防越权读

        跨 tenant 取相同 session_id 也读不到——tenant 是隐式 WHERE
        """
        conn = await self._ensure_conn()
        tenant = _current_tenant_id()
        async with conn.execute(
            "SELECT session_id, seq, event_name, timestamp, payload, request_id "
            "FROM session_events "
            "WHERE tenant_id=? AND session_id=? ORDER BY seq ASC",
            (tenant, session_id),
        ) as cur:
            rows = await cur.fetchall()
        result: list[SessionEvent] = []
        for r in rows:
            try:
                payload = json.loads(r[4])
            except json.JSONDecodeError:
                payload = {"_raw": r[4]}
            result.append(
                SessionEvent(
                    session_id=r[0],
                    seq=r[1],
                    event_name=r[2],
                    timestamp=r[3],
                    payload=payload,
                    request_id=r[5],
                )
            )
        return result
