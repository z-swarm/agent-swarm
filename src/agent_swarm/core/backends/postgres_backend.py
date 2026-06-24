"""
@module agent_swarm.core.backends.postgres_backend
@brief  P4-W25 PostgresBackend——生产级 TaskQueue 后端

DESIGN §9.4 + P4-PLAN W25 DoD:
  - W25-1 PostgresBackend 实现 TaskQueueBackend 协议
  - W25-2 schema: 单表 (id, version, data JSONB, updated_at)
  - W25-3 CAS: UPDATE ... WHERE id=? AND version=? RETURNING data
               (单语句原子, 无需 WATCH/MULTI/EXEC)
  - W25-4 多进程并发安全 (PostgreSQL MVCC + 事务隔离)
  - W25-5 命名空间隔离 (schema per namespace)

存储:
  - tasks(id TEXT PK, version INT, data JSONB, updated_at TIMESTAMPTZ)
  - 每个 namespace = 一个 schema (or 共享 schema + namespace 列)

性能:
  - 比 Redis 多一次网络往返 (TCP vs 内存)
  - 但有 WAL 持久化 + 事务一致性 + 大规模 (sharding 友好)

@note 异步连接 (asyncpg>=0.27)
@note 测试用 use_fakepg 注入 mock, 不需真 PG server
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_swarm.core.task_queue_backend import (
    StoredTask,
    TaskQueueBackend,
    VersionMismatchError,
)

log = logging.getLogger(__name__)


# Schema 初始化 SQL
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id          TEXT PRIMARY KEY,
    version     INTEGER NOT NULL DEFAULT 0,
    data        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS {table}_updated_at_idx
    ON {table} (updated_at);
"""


@dataclass
class PostgresConfig:
    """PostgreSQL 连接配置"""

    dsn: str = "postgresql://localhost:5432/agent_swarm"
    namespace: str = "public"  # schema 名, 默认 public
    table: str = "tasks"  # 表名
    min_size: int = 1
    max_size: int = 20
    command_timeout: float = 5.0
    # 测试用: 注入 fake asyncpg 模块 (避免依赖真 PG server)
    # 形如 FakeAsyncpgModule, 暴露 connect()/Connection 等
    fake_module: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PostgresBackend
# ---------------------------------------------------------------------------


class PostgresBackend(TaskQueueBackend):
    """
    P4-W25 PostgreSQL 后端——生产级持久化 + CAS

    CAS 实现:
      1. UPDATE {table} SET version=version+1, data=$1, updated_at=NOW()
         WHERE id=$2 AND version=$3 RETURNING data
      2. 若 RETURNING 为空 → 版本不符, 抛 VersionMismatchError
      3. PostgreSQL 单语句原子, 无需显式事务

    命名空间隔离:
      - 简单方案: 不同 namespace 共享 table, 多一列 namespace
      - 高级方案: 不同 schema (本实现选简单, namespace 是 schema 名, table 是固定名)
    """

    def __init__(self, config: PostgresConfig | None = None) -> None:
        self.config = config or PostgresConfig()
        self._pool: Any = None
        self._initialized = False
        self._init_lock: Any = None  # 延迟 import asyncio.Lock

    async def _ensure_connected(self) -> None:
        if self._initialized:
            return
        import asyncio

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._initialized:
                return
            if self.config.fake_module is not None:
                # 测试模式: 注入 fake
                self._pool = await self.config.fake_module.create_pool(
                    dsn=self.config.dsn,
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
                    **self.config.extra,
                )
            # 确保 schema + table 存在
            await self._init_schema()
            self._initialized = True

    async def _init_schema(self) -> None:
        """初始化 schema 和 table (幂等)"""
        sql = SCHEMA_SQL.format(table=self.config.table)
        async with self._pool.acquire() as conn:
            # schema 已存在时 (public) CREATE SCHEMA 会报错, 容错
            if self.config.namespace != "public":
                await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.config.namespace}")
            # 路径: schema.table (public 简写)
            full_table = (
                f"{self.config.namespace}.{self.config.table}"
                if self.config.namespace != "public"
                else self.config.table
            )
            # 替换 SCHEMA_SQL 里的 {table} 为 full_table
            sql = SCHEMA_SQL.format(table=full_table)
            await conn.execute(sql)

    @property
    def _full_table(self) -> str:
        if self.config.namespace == "public":
            return self.config.table
        return f"{self.config.namespace}.{self.config.table}"

    async def get(self, task_id: str) -> StoredTask | None:
        await self._ensure_connected()
        sql = f"SELECT data FROM {self._full_table} WHERE id = $1"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, task_id)
        if row is None:
            return None
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        return StoredTask.from_dict(data)

    async def put(self, task: StoredTask) -> None:
        await self._ensure_connected()
        sql = (
            f"INSERT INTO {self._full_table} (id, version, data, updated_at) "
            f"VALUES ($1, $2, $3, NOW())"
        )
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(sql, task.id, task.version, json.dumps(task.to_dict()))
        except Exception as exc:
            # asyncpg.UniqueViolationError (code 23505)
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                raise ValueError(f"task {task.id!r} already exists") from exc
            raise

    async def list_all(self) -> list[StoredTask]:
        await self._ensure_connected()
        sql = f"SELECT data FROM {self._full_table}"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        out: list[StoredTask] = []
        for row in rows:
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            out.append(StoredTask.from_dict(data))
        return out

    async def compare_and_set(
        self,
        task_id: str,
        expected_version: int,
        mutator: Callable[[StoredTask], StoredTask],
    ) -> StoredTask:
        """
        CAS: UPDATE ... WHERE id=$1 AND version=$2 RETURNING data

        PostgreSQL 单语句原子, 比 Redis WATCH/MULTI/EXEC 简单且强一致
        """
        await self._ensure_connected()
        # 1. 读当前值
        current = await self.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.version != expected_version:
            raise VersionMismatchError(
                task_id,
                expected_version,
                current.version,
            )
        # 2. 计算 new
        new = mutator(current)
        if new.version != expected_version + 1:
            raise ValueError(
                f"mutator must bump version by 1, got {expected_version} -> {new.version}",
            )
        # 3. 原子 UPDATE
        sql = (
            f"UPDATE {self._full_table} "
            f"SET version = $1, data = $2, updated_at = NOW() "
            f"WHERE id = $3 AND version = $4 "
            f"RETURNING data"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                new.version,
                json.dumps(new.to_dict()),
                task_id,
                expected_version,
            )
        if row is None:
            # 版本已被别人改
            current2 = await self.get(task_id)
            if current2 is None:
                raise KeyError(task_id)
            raise VersionMismatchError(
                task_id,
                expected_version,
                current2.version,
            )
        # row['data'] 可能是 JSON 字符串 (jsonb 自动反序列化取决于 driver)
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        return StoredTask.from_dict(data)

    async def stats(self) -> dict[str, int]:
        tasks = await self.list_all()
        s: dict[str, int] = {}
        for t in tasks:
            s[t.status] = s.get(t.status, 0) + 1
        return s

    async def close(self) -> None:
        if self._pool is not None and self.config.fake_module is None:
            await self._pool.close()
        self._initialized = False


__all__ = ["PostgresBackend", "PostgresConfig", "SCHEMA_SQL"]
