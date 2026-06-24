"""
@module agent_swarm.core.backends.memory
@brief  W18-② MemoryBackend——TaskQueue 后端的内存实现

@note 与现有 TaskQueue 不同: 仅提供 storage 层; 状态机由 TaskQueue 调用方维护
@note CAS 通过 asyncio.Lock 实现 (单进程版本)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from agent_swarm.core.task_queue_backend import (
    StoredTask,
    TaskQueueBackend,
    VersionMismatchError,
)

log = logging.getLogger(__name__)


class MemoryBackend(TaskQueueBackend):
    """W18-② 内存后端——单进程; 与现有 TaskQueue 行为一致"""

    def __init__(self) -> None:
        self._tasks: dict[str, StoredTask] = {}
        self._lock = asyncio.Lock()

    async def get(self, task_id: str) -> StoredTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def put(self, task: StoredTask) -> None:
        async with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"task {task.id!r} already exists")
            self._tasks[task.id] = task

    async def list_all(self) -> list[StoredTask]:
        async with self._lock:
            return list(self._tasks.values())

    async def compare_and_set(
        self,
        task_id: str,
        expected_version: int,
        mutator: Callable[[StoredTask], StoredTask],
    ) -> StoredTask:
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                raise KeyError(task_id)
            if t.version != expected_version:
                raise VersionMismatchError(task_id, expected_version, t.version)
            new: StoredTask = mutator(t)
            if new.version != expected_version + 1:
                raise ValueError(
                    f"mutator must bump version by 1, got {expected_version} -> {new.version}"
                )
            self._tasks[task_id] = new
            return new

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            s: dict[str, int] = {}
            for t in self._tasks.values():
                s[t.status] = s.get(t.status, 0) + 1
            return s

    async def close(self) -> None:
        # noop
        return None


__all__ = ["MemoryBackend"]
