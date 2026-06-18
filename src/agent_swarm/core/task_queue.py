"""
@module agent_swarm.core.task_queue
@brief  TaskQueue（W2 内存实现 + W3 ObservabilityBus 集成）

DESIGN.md §6.4 完整规约。
W2: 内存实现 + CAS
W3: 每次状态变更 emit 事件，事件流即恢复源
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent_swarm.core.types import ClaimResult, Task
from agent_swarm.observability import emit

log = logging.getLogger(__name__)


class TaskQueue:
    """
    声明式状态账本——单一乐观锁并发模型

    @note CAS 路径:
        list_claimable() → 拿到 (task, version)
        claim(task_id, agent_id, expected_version) → CAS 更新
        complete/fail(task_id, ..., expected_version) → CAS 更新
        所有 CAS 失败返回 reason="version_mismatch"，agent 应重新拉取

    @note W3 事件:
        task.created / task.claimed / task.completed / task.failed / task.unblocked
        task.cas_conflict (claim/complete/fail 任意一个版本不匹配时)
    """

    def __init__(self, session_id: str = "local") -> None:
        """
        @param session_id 用于事件标记——SessionManager 按此分组持久化
        """
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self.session_id = session_id

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def add(self, task: Task) -> str:
        """新增任务——返回 task.id"""
        # 持锁完成状态变更，emit 在锁外执行（避免持锁 await 跨边界）
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
            payload = {
                "task_id": task.id,
                "title": task.title,
                # BUG-1 修复:加 description——SessionManager 重放靠事件 payload
                # 重建 Task(description=...)。缺这个字段时,恢复后 task.description
                # 永远是空,Phase 2+ 计划做的"session resume 接着跑"会让 LLM 看不到
                # 原任务描述。
                "description": task.description,
                "status": task.status,
                "depends_on": list(task.depends_on),
                "assigned_to": task.assigned_to,
            }
        # 锁外 emit
        await emit("task.created", self.session_id, payload)
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
        # 持锁完成状态变更；事件类型在锁外 emit
        event_name: str | None = None
        event_payload: dict[str, Any] = {}
        result: ClaimResult

        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                result = ClaimResult(success=False, reason="task_not_found")
            elif t.version != expected_version:
                # CAS 冲突——其他 agent 抢先了
                log.info("task.cas_conflict id=%s expected=%d actual=%d agent=%s",
                         task_id, expected_version, t.version, agent_id)
                event_name = "task.cas_conflict"
                event_payload = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "op": "claim",
                    "expected_version": expected_version,
                    "actual_version": t.version,
                }
                result = ClaimResult(success=False, reason="version_mismatch")
            elif t.status == "in_progress":
                result = ClaimResult(success=False, reason="already_claimed")
            elif t.status == "blocked":
                # BUG-3 修复:显式返回 dependency_blocked 而非误导的
                # version_mismatch——agent 能从 reason 直接判断是依赖未完成
                # (重试无用)还是真 CAS 冲突(应重读 task_queue)
                result = ClaimResult(success=False, reason="dependency_blocked")
            elif t.status in ("completed", "failed"):
                # 终态任务不能再 claim——给一个明确 reason
                result = ClaimResult(success=False, reason="task_terminal")
            elif t.status != "pending":
                # 兜底:未来若新增 status 而漏改这里,仍走 version_mismatch
                # 而不是静默成功
                result = ClaimResult(success=False, reason="version_mismatch")
            elif not self._deps_satisfied(t):
                result = ClaimResult(success=False, reason="dependency_blocked")
            else:
                # 认领成功——原地更新
                t.status = "in_progress"
                t.assigned_to = agent_id
                t.version += 1
                t.updated_at = time.time()
                log.info("task.claimed id=%s agent=%s v=%d", task_id, agent_id, t.version)
                event_name = "task.claimed"
                event_payload = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "version": t.version,
                }
                result = ClaimResult(success=True, task=t, reason="ok")

        if event_name is not None:
            await emit(event_name, self.session_id, event_payload)
        return result

    async def complete(
        self, task_id: str, result: Any, expected_version: int
    ) -> ClaimResult:
        """CAS 更新 status=in_progress → completed；冲突返回 version_mismatch"""
        event_name: str | None = None
        event_payload: dict[str, Any] = {}
        # 解阻塞产生的事件
        unblocked_events: list[tuple[str, dict[str, Any]]] = []
        ret: ClaimResult

        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                ret = ClaimResult(success=False, reason="task_not_found")
            elif t.version != expected_version:
                log.info("task.cas_conflict on complete id=%s expected=%d actual=%d",
                         task_id, expected_version, t.version)
                event_name = "task.cas_conflict"
                event_payload = {
                    "task_id": task_id,
                    "op": "complete",
                    "expected_version": expected_version,
                    "actual_version": t.version,
                }
                ret = ClaimResult(success=False, reason="version_mismatch")
            else:
                t.status = "completed"
                t.result = result
                t.version += 1
                t.updated_at = time.time()
                log.info("task.completed id=%s v=%d", task_id, t.version)
                event_name = "task.completed"
                # W3-Z2 修复：result 直接传，由 sink (json.dumps default=str) 兜底
                # 上层粗暴 str() 会丢失结构化信息（如 dict/list）
                event_payload = {
                    "task_id": task_id,
                    "version": t.version,
                    "result": result,
                }
                # 解阻塞依赖此任务的其他 task——同时收集事件
                unblocked_events = self._unblock_dependents(task_id)
                ret = ClaimResult(success=True, task=t, reason="ok")

        if event_name is not None:
            await emit(event_name, self.session_id, event_payload)
        for name, payload in unblocked_events:
            await emit(name, self.session_id, payload)
        return ret

    async def fail(
        self, task_id: str, error: str, expected_version: int
    ) -> ClaimResult:
        """CAS 更新 status → failed"""
        event_name: str | None = None
        event_payload: dict[str, Any] = {}
        ret: ClaimResult

        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                ret = ClaimResult(success=False, reason="task_not_found")
            elif t.version != expected_version:
                event_name = "task.cas_conflict"
                event_payload = {
                    "task_id": task_id,
                    "op": "fail",
                    "expected_version": expected_version,
                    "actual_version": t.version,
                }
                ret = ClaimResult(success=False, reason="version_mismatch")
            else:
                t.status = "failed"
                t.error = error
                t.version += 1
                t.updated_at = time.time()
                log.warning("task.failed id=%s v=%d error=%s", task_id, t.version, error)
                event_name = "task.failed"
                event_payload = {
                    "task_id": task_id,
                    "version": t.version,
                    "error": error,
                }
                ret = ClaimResult(success=True, task=t, reason="ok")

        if event_name is not None:
            await emit(event_name, self.session_id, event_payload)
        return ret

    # ------------------------------------------------------------------
    # 内部：依赖解阻塞
    # ------------------------------------------------------------------
    def _unblock_dependents(
        self, completed_task_id: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """
        task X 完成后，把所有依赖 X 的 blocked 任务转回 pending

        @return 待 emit 的事件列表 [(event_name, payload), ...]
                调用方在锁外 await emit
        """
        events: list[tuple[str, dict[str, Any]]] = []
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
                events.append(
                    (
                        "task.unblocked",
                        {
                            "task_id": t.id,
                            "version": t.version,
                            "trigger": completed_task_id,
                        },
                    )
                )
        return events

    # ------------------------------------------------------------------
    # 恢复支持（W3）——SessionManager 用，绕过 emit
    # ------------------------------------------------------------------
    async def restore_task(self, task: Task) -> None:
        """
        从事件流回放写入一个 Task——绕过 add() 的 emit + 依赖检查

        @note 仅供 SessionManager 在重放时调用；普通业务路径请用 add()
        """
        async with self._lock:
            self._tasks[task.id] = task

    async def restore_apply(
        self,
        task_id: str,
        updates: dict[str, Any],
    ) -> None:
        """
        从事件流回放更新一个 Task 字段——绕过 CAS 与 emit

        @note SessionManager 重放 task.claimed/completed/failed/unblocked 时调
        """
        async with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            for k, v in updates.items():
                if hasattr(t, k):
                    setattr(t, k, v)

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
