"""
@module agent_swarm.core.backends.redis_backend
@brief  W18-③ RedisBackend——多进程共享 TaskQueue 后端

DESIGN §9.4 + P3-PLAN-v2 W18 DoD:
  - W18-3 RedisBackend 实现
  - W18-4 多进程并发安全 (WATCH/MULTI/EXEC 乐观锁)

CAS 实现:
  1. WATCH tasks:{id} -> 监视该 key
  2. GET tasks:{id}    -> 读取当前版本
  3. 比对 expected_version
     不匹配 -> 抛 VersionMismatchError (Redis 自动 UNWATCH)
  4. MULTI / SET tasks:{id} {new_value} EX / EXEC
     EXEC 失败 (WATCH 触发) -> 重试, 抛 VersionMismatchError

存储:
  - tasks:{id} = JSON(StoredTask) (含 version)
  - tasks:index = SET (id 列表, 用于 list_all)
  - 序列化用 orjson (若可用) 或 json

@note redis>=5.0.0 asyncio API
"""

from __future__ import annotations

import asyncio
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


@dataclass
class RedisConfig:
    """Redis 连接配置"""

    url: str = "redis://localhost:6379/0"
    namespace: str = "agent_swarm"  # key 前缀
    pool_max_connections: int = 20
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    retry_on_timeout: bool = True
    # W18 测试用 fakeredis——只需 import fakeredis 并设 client_cls
    use_fakeredis: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class RedisBackend(TaskQueueBackend):
    """W18-③ Redis 后端——多进程共享 + WATCH/MULTI/EXEC CAS"""

    def __init__(self, config: RedisConfig | None = None) -> None:
        self.config = config or RedisConfig()
        self._redis: Any = None  # redis.asyncio.Redis 或 fakeredis.FakeAsyncRedis
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if self.config.use_fakeredis:
                import fakeredis.aioredis as far

                # 用独立 FakeServer 实例保证 namespace 隔离
                from fakeredis import FakeServer

                server = FakeServer()
                self._redis = far.FakeRedis(server=server)
            else:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    self.config.url,
                    max_connections=self.config.pool_max_connections,
                    socket_timeout=self.config.socket_timeout,
                    socket_connect_timeout=self.config.socket_connect_timeout,
                    retry_on_timeout=self.config.retry_on_timeout,
                    decode_responses=True,
                    **self.config.extra,
                )
            self._initialized = True

    def _k(self, task_id: str) -> str:
        return f"{self.config.namespace}:tasks:{task_id}"

    @property
    def _index_key(self) -> str:
        return f"{self.config.namespace}:tasks:index"

    async def get(self, task_id: str) -> StoredTask | None:
        await self._ensure_connected()
        raw = await self._redis.get(self._k(task_id))
        if raw is None:
            return None
        return StoredTask.from_dict(json.loads(raw))

    async def put(self, task: StoredTask) -> None:
        await self._ensure_connected()
        # 先检查重复 (无 WATCH 也能保证 put 语义; 调用方负责并发安全)
        key = self._k(task.id)
        exists = await self._redis.exists(key)
        if exists:
            raise ValueError(f"task {task.id!r} already exists")
        # pipeline 写 + index 添加——非事务, 但对 task_id unique 已足够
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(key, json.dumps(task.to_dict()))
            pipe.sadd(self._index_key, task.id)
            await pipe.execute()

    async def list_all(self) -> list[StoredTask]:
        await self._ensure_connected()
        ids_raw = await self._redis.smembers(self._index_key)
        # fakeredis 在 decode_responses=True 下 smembers 可能返 bytes, 兜底解码
        ids: set[str] = set()
        for i in ids_raw:
            if isinstance(i, bytes):
                ids.add(i.decode("utf-8"))
            else:
                ids.add(i)
        if not ids:
            return []
        keys = [self._k(i) for i in ids]
        raws = await self._redis.mget(keys)
        out: list[StoredTask] = []
        for raw in raws:
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            out.append(StoredTask.from_dict(json.loads(raw)))
        return out

    async def compare_and_set(
        self,
        task_id: str,
        expected_version: int,
        mutator: Callable[[StoredTask], StoredTask],
    ) -> StoredTask:
        """
        WATCH/MULTI/EXEC CAS——W18-3 核心

        @raise VersionMismatchError  版本不符或 WATCH 触发
        @raise KeyError              task 不存在
        """
        await self._ensure_connected()
        key = self._k(task_id)
        # WATCH 监视 + 读当前值
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            try:
                raw = await self._redis.get(key)
                if raw is None:
                    await pipe.unwatch()
                    raise KeyError(task_id)
                current = StoredTask.from_dict(json.loads(raw))
                if current.version != expected_version:
                    await pipe.unwatch()
                    raise VersionMismatchError(
                        task_id,
                        expected_version,
                        current.version,
                    )
                # 计算 new
                new = mutator(current)
                if new.version != expected_version + 1:
                    raise ValueError(
                        f"mutator must bump version by 1, got {expected_version} -> {new.version}",
                    )
                pipe.multi()
                pipe.set(key, json.dumps(new.to_dict()))
                result = await pipe.execute()
            except VersionMismatchError:
                raise
            except KeyError:
                raise
            except Exception:
                await pipe.reset()
                raise
            if not result:
                # EXEC 失败 (WATCH 触发) — 重读抛 VersionMismatchError
                raw2 = await self._redis.get(key)
                if raw2 is None:
                    raise KeyError(task_id)
                current2 = StoredTask.from_dict(json.loads(raw2))
                raise VersionMismatchError(
                    task_id,
                    expected_version,
                    current2.version,
                )
            return new

    async def stats(self) -> dict[str, int]:
        tasks = await self.list_all()
        s: dict[str, int] = {}
        for t in tasks:
            s[t.status] = s.get(t.status, 0) + 1
        return s

    async def close(self) -> None:
        if self._redis is not None and not self.config.use_fakeredis:
            await self._redis.aclose()
        self._initialized = False


__all__ = ["RedisBackend", "RedisConfig"]
