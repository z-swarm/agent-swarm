"""
@module agent_swarm.core.task_queue
@brief  TaskQueue（W2 内存实现，CAS 单一并发模型）

DESIGN.md §6.4 完整规约。W2 阶段:
  - 内存实现（asyncio.Lock 保护 dict）
  - CAS 通过版本号比较实现
  - W3 切到 SQLite——StorageBackend.cas_update_task 替换内存路径

为什么内存够用?
  - W2 单进程内多 agent 并发，asyncio.Lock 已能保证原子性
  - W3 跨进程恢复才需要 SQLite WAL；W2 不引入 IO 复杂度
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent_swarm.core.types import ClaimResult, Task

log = logging.getLogger(__name__)


class TaskQueue:
    """
    声明式状态账本——单一乐观锁并发模型

    @note CAS 路径:
        list_claimable() → 拿到 (task, version)
        claim(task_id, agent_id, expected_version) → CAS 更新
        complete/fail(task_id, ..., expected_version) → CAS 更新
        所有 CAS 失败返回 reason="version_mismatch"，agent 应重新拉取
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()  # 保护 _tasks 的所有读写

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def add(self, task: Task) -> str:
        """新增任务——返回 task.id"""
        async with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"task {task.id!r} already exists")
            now = time.time()
            if task.created_at == 0.0:
                task.created_at = now
            task.updated_at = now
            # 依赖未完成则置为 blocked
            if task.depends_on:
                deps_done = all(
                    self._tasks.get(d) and self._tasks[d].status == "completed"
                    for d in task.depends_on
                )
                if not deps_done:
                    task.status = "blocked"
            self._tasks[task.id] = task
            log.debug("task.created id=%s title=%s status=%s",
                      task.id, task.title, task.status)
            return task.id

    async def add_many(self, tasks: list[Task]) -> list[str]:
        """批量新增——便于 Swarm 启动时一次性注入"""
        ids = []
        for t in tasks:
            ids.append(await self.add(t))
        return ids

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def get(self, task_id: str) -> Task | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def list_all(self) -> list[Task]:
        async with self._lock:
            return list(self._tasks.values())

    async def list_claimable(self, agent_id: str | None = None) -> list[Task]:
        """
        列出当前可认领的任务

        条件:
          1. status == "pending"
          2. 所有依赖任务已 completed
          3. 当 agent_id 指定时:
             - assigned_to 为空（任意 agent 可抢） 或
             - assigned_to == agent_id（已分派给此 agent）
             agent_id=None 视为"全部认领权"——返回所有 pending+依赖满足的任务
        """
        async with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status == "pending"
                and self._deps_satisfied(t)
                and (
                    agent_id is None
                    or t.assigned_to is None
                    or t.assigned_to == agent_id
                )
            ]

    def _deps_satisfied(self, task: Task) -> bool:
        """所有依赖任务都 completed？（持锁状态下调用）"""
        return all(
            self._tasks.get(d) and self._tasks[d].status == "completed"
            for d in task.depends_on
        )

    # ------------------------------------------------------------------
    # CAS 状态变更
    # ------------------------------------------------------------------
    async def claim(
        self, task_id: str, agent_id: str, expected_version: int
    ) -> ClaimResult:
        """
        认领任务——CAS 更新 status=pending → in_progress, assigned_to=agent_id

        @return ClaimResult.success=True 时 task 是更新后的副本
                失败时 reason 区分原因；冲突时调用方应 list_claimable 重试
        """
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return ClaimResult(success=False, reason="task_not_found")
            if t.version != expected_version:
                # CAS 冲突——其他 agent 抢先了
                log.info("task.cas_conflict id=%s expected=%d actual=%d agent=%s",
                         task_id, expected_version, t.version, agent_id)
                return ClaimResult(success=False, reason="version_mismatch")
            if t.status == "in_progress":
                return ClaimResult(success=False, reason="already_claimed")
            if t.status != "pending":
                return ClaimResult(success=False, reason="version_mismatch")
            if not self._deps_satisfied(t):
                return ClaimResult(success=False, reason="dependency_blocked")

            # 认领成功——原地更新
            t.status = "in_progress"
            t.assigned_to = agent_id
            t.version += 1
            t.updated_at = time.time()
            log.info("task.claimed id=%s agent=%s v=%d", task_id, agent_id, t.version)
            return ClaimResult(success=True, task=t, reason="ok")

    async def complete(
        self, task_id: str, result: Any, expected_version: int
    ) -> ClaimResult:
        """CAS 更新 status=in_progress → completed；冲突返回 version_mismatch"""
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return ClaimResult(success=False, reason="task_not_found")
            if t.version != expected_version:
                log.info("task.cas_conflict on complete id=%s expected=%d actual=%d",
                         task_id, expected_version, t.version)
                return ClaimResult(success=False, reason="version_mismatch")

            t.status = "completed"
            t.result = result
            t.version += 1
            t.updated_at = time.time()
            log.info("task.completed id=%s v=%d", task_id, t.version)

            # 解阻塞依赖此任务的其他 task
            self._unblock_dependents(task_id)
            return ClaimResult(success=True, task=t, reason="ok")

    async def fail(
        self, task_id: str, error: str, expected_version: int
    ) -> ClaimResult:
        """CAS 更新 status → failed"""
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return ClaimResult(success=False, reason="task_not_found")
            if t.version != expected_version:
                return ClaimResult(success=False, reason="version_mismatch")
            t.status = "failed"
            t.error = error
            t.version += 1
            t.updated_at = time.time()
            log.warning("task.failed id=%s v=%d error=%s", task_id, t.version, error)
            return ClaimResult(success=True, task=t, reason="ok")

    # ------------------------------------------------------------------
    # 内部：依赖解阻塞
    # ------------------------------------------------------------------
    def _unblock_dependents(self, completed_task_id: str) -> None:
        """task X 完成后，把所有依赖 X 的 blocked 任务转回 pending"""
        for t in self._tasks.values():
            if (
                t.status == "blocked"
                and completed_task_id in t.depends_on
                and self._deps_satisfied(t)
            ):
                t.status = "pending"
                t.version += 1
                t.updated_at = time.time()
                log.debug("task.unblocked id=%s", t.id)

    # ------------------------------------------------------------------
    # 统计快照
    # ------------------------------------------------------------------
    async def stats(self) -> dict[str, int]:
        """各状态任务数（用于 SwarmStatus / TUI）"""
        async with self._lock:
            stats: dict[str, int] = {}
            for t in self._tasks.values():
                stats[t.status] = stats.get(t.status, 0) + 1
            return stats
