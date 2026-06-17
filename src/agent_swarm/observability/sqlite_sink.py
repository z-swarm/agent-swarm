"""
@module agent_swarm.observability.sqlite_sink
@brief  SQLite EventSink——事件持久化（W3）

DESIGN.md §5.4 / §10：事件流是 Session 恢复的唯一来源
W3 简化策略：每条事件直接 INSERT；W4 批量优化（每 N 条 / 每 5 秒刷盘）

Schema:
  CREATE TABLE session_events (
      session_id TEXT NOT NULL,
      seq        INTEGER NOT NULL,
      event_name TEXT NOT NULL,
      timestamp  REAL NOT NULL,
      payload    TEXT NOT NULL,           -- JSON
      request_id TEXT,
      PRIMARY KEY (session_id, seq)
  );
  CREATE INDEX idx_events_session ON session_events(session_id, seq);
  CREATE INDEX idx_events_name ON session_events(event_name);

  CREATE TABLE sessions (
      session_id   TEXT PRIMARY KEY,
      swarm_name   TEXT NOT NULL,
      created_at   REAL NOT NULL,
      ended_at     REAL,
      state        TEXT,             -- last seen swarm.* state
      config_yaml  TEXT               -- 原始 yaml（resume 时用）
  );

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_events (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    payload    TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_name ON session_events(event_name);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    swarm_name   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    ended_at     REAL,
    state        TEXT,
    config_yaml  TEXT
);
"""


class SqliteEventSink(ObservabilitySink):
    """
    SQLite 事件持久化 sink

    @note 单例 / 进程内复用；多进程访问需 WAL（默认开启）
    @note connect() 是延迟初始化——首次 consume 时建库 + schema
    """

    def __init__(self, db_path: str | Path) -> None:
        # ``:memory:`` 是 SQLite 的内存库特殊标识，不能当成路径 resolve（否则会变成 cwd/:memory: 物理文件）
        if isinstance(db_path, str) and db_path == ":memory:":
            self.db_path: Path | str = ":memory:"
        else:
            self.db_path = Path(db_path).resolve()
        self._conn: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    async def _ensure_conn(self) -> aiosqlite.Connection:
        """懒连接 + schema + pragma 配置"""
        if self._conn is not None:
            return self._conn
        async with self._init_lock:
            if self._conn is not None:
                return self._conn
            # 父目录不存在则建（:memory: 跳过）
            if str(self.db_path) != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self.db_path))
            # WAL 仅对实体库生效；:memory: 会忽略
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA cache_size=-64000")
            await conn.executescript(_SCHEMA)
            await conn.commit()
            self._conn = conn
            log.info("SqliteEventSink connected to %s", self.db_path)
            return conn

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 事件写入
    # ------------------------------------------------------------------
    async def consume(self, event: SessionEvent) -> None:
        try:
            conn = await self._ensure_conn()
            await conn.execute(
                "INSERT OR REPLACE INTO session_events "
                "(session_id, seq, event_name, timestamp, payload, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
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

    # ------------------------------------------------------------------
    # Session 元数据 + 事件流读取（SessionManager 用）
    # ------------------------------------------------------------------
    async def register_session(
        self,
        session_id: str,
        swarm_name: str,
        config_yaml: str | None = None,
    ) -> None:
        """记录 session 元数据——SessionManager.create_session 调用"""
        conn = await self._ensure_conn()
        await conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(session_id, swarm_name, created_at, config_yaml) "
            "VALUES (?, ?, ?, ?)",
            (session_id, swarm_name, time.time(), config_yaml),
        )
        await conn.commit()

    async def end_session(self, session_id: str, state: str) -> None:
        """标记 session 结束——SessionManager 在 swarm.completed 后调用"""
        conn = await self._ensure_conn()
        await conn.execute(
            "UPDATE sessions SET ended_at=?, state=? WHERE session_id=?",
            (time.time(), state, session_id),
        )
        await conn.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT session_id, swarm_name, created_at, ended_at, state, config_yaml "
            "FROM sessions WHERE session_id=?",
            (session_id,),
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
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT session_id, swarm_name, created_at, ended_at, state "
            "FROM sessions ORDER BY created_at DESC"
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
        """按 seq 升序读取一个 session 的全部事件——回放/恢复用"""
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT session_id, seq, event_name, timestamp, payload, request_id "
            "FROM session_events WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
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
