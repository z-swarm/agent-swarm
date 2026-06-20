"""
@module agent_swarm.core.task_queue_backend
@brief  W18 TaskQueue 持久化后端抽象

P3-PLAN-v2 W18 DoD:
  - W18-1 StorageBackend ABC
  - W18-2 MemoryBackend (现有 TaskQueue 重命名/复用)
  - W18-3 RedisBackend (WATCH/MULTI/EXEC CAS)
  - W18-4 多进程并发安全 (Redis 版本)
  - W18-5 G-020 Golden Case
  - W18-6 bench_storage.py

@note 与现有 TaskQueue 的关系:
  - 当前 TaskQueue (core/task_queue.py) 保留——向后兼容
  - 本文件提供 backend 抽象, 供 TaskQueue 注入
  - Redis 后端启用多进程共享 (W18-4)
  - W19 可选: 接入 PostgresBackend

@note CAS 抽象:
  - get(key) -> value | None
  - compare_and_set(key, expected_version, new_value) -> bool
  - list_claimable(filter) -> list[(key, version, value)]
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """后端通用错误"""


class VersionMismatchError(BackendError):
    """乐观锁冲突——expected_version 与实际不符"""

    def __init__(
        self, key: str, expected: int, actual: int,
    ) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"version_mismatch on {key!r}: expected={expected}, actual={actual}"
        )


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class StoredTask:
    """后端存储的 Task 快照——与 core.types.Task 解耦,便于序列化"""

    id: str
    title: str
    description: str
    status: str
    version: int
    assigned_to: str | None
    depends_on: list[str]
    result: Any
    error: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "version": self.version,
            "assigned_to": self.assigned_to,
            "depends_on": list(self.depends_on),
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredTask:
        return cls(
            id=d["id"],
            title=d["title"],
            description=d.get("description", ""),
            status=d["status"],
            version=int(d.get("version", 0)),
            assigned_to=d.get("assigned_to"),
            depends_on=list(d.get("depends_on", [])),
            result=d.get("result"),
            error=d.get("error"),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
        )


# ---------------------------------------------------------------------------
# 抽象后端
# ---------------------------------------------------------------------------


class TaskQueueBackend(ABC):
    """
    TaskQueue 后端接口——DESIGN §9.4 + W18

    @note W18-1: ABC; W18-2: MemoryBackend; W18-3: RedisBackend
    @note CAS 通过 compare_and_set 表达, 内部实现差异对调用方透明
    """

    @abstractmethod
    async def get(self, task_id: str) -> StoredTask | None: ...

    @abstractmethod
    async def put(self, task: StoredTask) -> None:
        """
        写入——若已存在则抛 ValueError
        @note 不做 CAS——纯插入语义, 重复 id 由调用方检查
        """

    @abstractmethod
    async def list_all(self) -> list[StoredTask]: ...

    @abstractmethod
    async def compare_and_set(
        self,
        task_id: str,
        expected_version: int,
        mutator: Callable[[StoredTask], StoredTask],
    ) -> StoredTask:
        """
        CAS 更新——若 version 不符抛 VersionMismatchError

        @param mutator  接收当前 StoredTask, 返回新 StoredTask (version +1)
        @return  更新后的 StoredTask
        @raise VersionMismatchError, KeyError
        """

    @abstractmethod
    async def stats(self) -> dict[str, int]: ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接——Redis: 释放 pool; Memory: noop"""


__all__ = [
    "BackendError",
    "VersionMismatchError",
    "StoredTask",
    "TaskQueueBackend",
]
