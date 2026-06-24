"""
@module agent_swarm.core.session_manager
@brief  SessionManager——会话持久化 + 事件流恢复（W3）

DESIGN.md §6.7 完整规约。W3 实现：
  - create_session: 在 SqliteEventSink 注册 session 元数据
  - restore_session: 读 SqliteEventSink 事件流，重放到新 TaskQueue/Mailbox
  - list_sessions / get_session: 查询元数据

恢复逻辑（事件 → 状态）：
  task.created   → TaskQueue 内部插入 task（绕过 add，避免重复 emit）
  task.claimed   → 设置 status=in_progress, assigned_to, version
  task.completed → status=completed, result, version
  task.failed    → status=failed, error, version
  task.unblocked → status=pending, version
  message.sent   → Mailbox 内部插入 message（保留 read 状态）
  message.received → 把对应 msg 的 read 标记为 True
  swarm.started/completed/failed → 仅元数据，不影响内部状态
  task.cas_conflict → 跳过（不影响最终一致状态）

@note 恢复后 ObservabilityBus 不应再发"重放事件"——重放是只读的状态重建
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Message, SessionEvent, Task
from agent_swarm.observability.sqlite_sink import SqliteEventSink

log = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """list_sessions 返回项——DESIGN.md §A.1 子集"""

    session_id: str
    swarm_name: str
    state: str | None
    created_at: float
    ended_at: float | None


@dataclass
class RestoredState:
    """restore_session 返回值——已重建的运行时状态"""

    session_id: str
    swarm_name: str
    state: str | None  # last seen (completed/failed/None=未结束)
    config_yaml: str | None
    task_queue: TaskQueue
    mailbox: Mailbox
    event_count: int
    last_seq: int


class SessionManager:
    """
    会话生命周期管理器

    @note 持有一个 SqliteEventSink；register_session 在 swarm 启动时调一次
          restore_session 在 CLI `session resume` 时调
    """

    def __init__(self, sink: SqliteEventSink) -> None:
        self._sink = sink

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    async def create_session(
        self,
        swarm_name: str,
        session_id: str | None = None,
        config_yaml: str | None = None,
    ) -> str:
        """
        新建 session 元数据——返回 session_id

        @note 不发任何事件；仅写 sessions 表。事件由 swarm.started 后续 emit
        """
        sid = session_id or f"s-{uuid4().hex[:12]}"
        await self._sink.register_session(sid, swarm_name, config_yaml)
        log.info("session.created id=%s swarm=%s", sid, swarm_name)
        return sid

    async def end_session(self, session_id: str, state: str) -> None:
        await self._sink.end_session(session_id, state)

    async def list_sessions(self) -> list[SessionSummary]:
        rows = await self._sink.list_sessions()
        return [
            SessionSummary(
                session_id=r["session_id"],
                swarm_name=r["swarm_name"],
                state=r["state"],
                created_at=r["created_at"],
                ended_at=r["ended_at"],
            )
            for r in rows
        ]

    async def get_session(self, session_id: str) -> SessionSummary | None:
        info = await self._sink.get_session(session_id)
        if info is None:
            return None
        return SessionSummary(
            session_id=info["session_id"],
            swarm_name=info["swarm_name"],
            state=info["state"],
            created_at=info["created_at"],
            ended_at=info["ended_at"],
        )

    # ------------------------------------------------------------------
    # 事件流恢复
    # ------------------------------------------------------------------
    async def restore_session(self, session_id: str) -> RestoredState:
        """
        从 SQLite 事件流重建 TaskQueue + Mailbox

        @raise ValueError session_id 不存在
        """
        info = await self._sink.get_session(session_id)
        if info is None:
            raise ValueError(f"session {session_id!r} not found")

        events = await self._sink.get_events(session_id)
        log.info("session.restoring id=%s events=%d", session_id, len(events))

        # 重建——使用同 session_id 的新 TaskQueue/Mailbox 实例
        # 注意：重放时不应再 emit（避免事件流双倍）；通过 _silent 上下文
        task_queue = TaskQueue(session_id=session_id)
        mailbox = Mailbox(session_id=session_id)

        for evt in events:
            await self._apply_event(evt, task_queue, mailbox)

        last_seq = events[-1].seq if events else -1
        # 用公开 API 拿统计——避免触碰私有字段
        all_tasks = await task_queue.list_all()
        all_msgs = await mailbox.all_messages()
        log.info(
            "session.restored id=%s last_seq=%d tasks=%d msgs=%d",
            session_id,
            last_seq,
            len(all_tasks),
            len(all_msgs),
        )

        return RestoredState(
            session_id=session_id,
            swarm_name=info["swarm_name"],
            state=info["state"],
            config_yaml=info["config_yaml"],
            task_queue=task_queue,
            mailbox=mailbox,
            event_count=len(events),
            last_seq=last_seq,
        )

    # ------------------------------------------------------------------
    # 事件应用——纯函数式重放
    # ------------------------------------------------------------------
    async def _apply_event(
        self,
        evt: SessionEvent,
        tq: TaskQueue,
        mb: Mailbox,
    ) -> None:
        """
        把单条事件应用到 task_queue / mailbox

        @note 通过 TaskQueue.restore_task / restore_apply 与 Mailbox.restore_message
              进入持锁的恢复路径——不再触碰内部字典；不重新 emit
              这里的"信任源"是事件流——payload 字段是契约
        """
        name = evt.event_name
        p = evt.payload

        if name == "task.created":
            t = Task(
                id=p["task_id"],
                title=p.get("title", ""),
                description=p.get("description", ""),
                status=p.get("status", "pending"),
                assigned_to=p.get("assigned_to"),
                depends_on=list(p.get("depends_on") or []),
                version=0,
                created_at=evt.timestamp,
                updated_at=evt.timestamp,
            )
            await tq.restore_task(t)

        elif name == "task.claimed":
            await tq.restore_apply(
                p["task_id"],
                {
                    "status": "in_progress",
                    "assigned_to": p.get("agent_id"),
                    "version": p.get("version", 1),
                    "updated_at": evt.timestamp,
                },
            )

        elif name == "task.completed":
            await tq.restore_apply(
                p["task_id"],
                {
                    "status": "completed",
                    "result": p.get("result"),
                    "version": p.get("version", 2),
                    "updated_at": evt.timestamp,
                },
            )

        elif name == "task.failed":
            await tq.restore_apply(
                p["task_id"],
                {
                    "status": "failed",
                    "error": p.get("error"),
                    "version": p.get("version", 2),
                    "updated_at": evt.timestamp,
                },
            )

        elif name == "task.unblocked":
            await tq.restore_apply(
                p["task_id"],
                {
                    "status": "pending",
                    "version": p.get("version", 1),
                    "updated_at": evt.timestamp,
                },
            )

        elif name == "message.sent":
            msg = Message(
                id=p["msg_id"],
                from_agent=p["from"],
                to_agent=p["to"],
                target_type=p.get("target_type", "internal"),
                msg_type=p["msg_type"],
                content=p["content"],
                refs=list(p.get("refs") or []),
                reply_to=p.get("reply_to"),
                timestamp=evt.timestamp,
                read=False,
            )
            await mb.restore_message(msg)

        elif name == "message.received":
            ids = list(p.get("msg_ids") or [])
            await mb.restore_mark_read(ids)

        elif name in (
            "task.cas_conflict",
            "swarm.started",
            "swarm.completed",
            "swarm.failed",
            "agent.loop.iteration_complete",
        ):
            # 不影响内部状态——元数据/统计事件
            return

        else:
            log.debug("session.replay_skip event=%s (unknown)", name)


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------


def make_session_manager(db_path: str | Any) -> tuple[SessionManager, SqliteEventSink]:
    """
    一键创建 SessionManager + 共享 sink——方便 CLI / Swarm 用

    @return (manager, sink) 调用方持有 sink 用于注册到 ObservabilityBus
    """
    sink = SqliteEventSink(db_path)
    return SessionManager(sink), sink
