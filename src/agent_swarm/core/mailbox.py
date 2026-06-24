"""
@module agent_swarm.core.mailbox
@brief  Mailbox（W2 内存 + W3 ObservabilityBus 集成）

DESIGN.md §6.5 完整规约。
W2: 内存 dict[agent_id → list[Message]] + asyncio.Event 唤醒
W3: send/receive 路径 emit 事件
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal
from uuid import uuid4

from agent_swarm.core.types import Message
from agent_swarm.observability import emit

log = logging.getLogger(__name__)


class Mailbox:
    """
    内存 Mailbox——按 agent_id 维护收件箱

    @note 不主动推送；agent 主动 receive() 拉取
          send() 唤醒等待该 agent 的 wait_for_message()
    @note W3 事件: message.sent / message.received（仅在 receive 真返回非空时）
    """

    def __init__(self, session_id: str = "local") -> None:
        # agent_id → 已发到该 agent 的消息列表（含已读）
        self._inbox: dict[str, list[Message]] = {}
        # agent_id → 唤醒事件（receive 等待用）
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        # 全局有序消息表，方便 get_thread / 调试
        self._all: dict[str, Message] = {}
        self.session_id = session_id

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def send(self, msg: Message) -> None:
        """
        投递消息——直接 append 到接收方收件箱并唤醒等待者

        @raise ValueError to_agent=None（W2 暂不支持广播）
        """
        if msg.to_agent is None:
            raise ValueError("broadcast not supported in W2 (to_agent=None)")
        if not msg.timestamp:
            msg.timestamp = time.time()

        async with self._lock:
            self._inbox.setdefault(msg.to_agent, []).append(msg)
            self._all[msg.id] = msg
            log.info(
                "message.sent id=%s from=%s to=%s type=%s",
                msg.id,
                msg.from_agent,
                msg.to_agent,
                msg.msg_type,
            )
            # 唤醒等待该 agent 的协程
            ev = self._events.get(msg.to_agent)
            if ev is not None:
                ev.set()
        # 锁外 emit
        await emit(
            "message.sent",
            self.session_id,
            {
                "msg_id": msg.id,
                "from": msg.from_agent,
                "to": msg.to_agent,
                "msg_type": msg.msg_type,
                "content": msg.content,
                "reply_to": msg.reply_to,
                "refs": list(msg.refs),
                "target_type": msg.target_type,
            },
        )

    @staticmethod
    def make_message(
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: Literal["question", "challenge", "reply", "notify", "delegate"] = "notify",
        refs: list[str] | None = None,
        reply_to: str | None = None,
        target_type: Literal["internal", "external"] = "internal",
    ) -> Message:
        """工厂方法——便于工具/编排层构造消息"""
        return Message(
            id=f"m-{uuid4().hex[:12]}",
            from_agent=from_agent,
            to_agent=to_agent,
            target_type=target_type,
            msg_type=msg_type,
            content=content,
            refs=refs or [],
            reply_to=reply_to,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    async def receive(
        self,
        agent_id: str,
        unread_only: bool = True,
        msg_type: str | None = None,
    ) -> list[Message]:
        """
        拉取 agent 的收件箱

        @param unread_only 只返回未读消息（默认 True）
        @param msg_type    过滤特定类型（None=不过滤）
        """
        async with self._lock:
            box = self._inbox.get(agent_id, [])
            return [
                m
                for m in box
                if (not unread_only or not m.read) and (msg_type is None or m.msg_type == msg_type)
            ]

    async def wait_for_message(self, agent_id: str, timeout: float | None = None) -> bool:
        """
        阻塞直到该 agent 收到新消息（或超时）

        @return True=有消息到达；False=超时
        """
        async with self._lock:
            # 已有未读 → 立刻返回
            box = self._inbox.get(agent_id, [])
            if any(not m.read for m in box):
                return True
            ev = self._events.setdefault(agent_id, asyncio.Event())
            ev.clear()

        try:
            if timeout is None:
                await ev.wait()
            else:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def mark_read(self, agent_id: str, message_ids: list[str]) -> int:
        """标记消息为已读——返回实际标记数"""
        marked: list[str] = []
        async with self._lock:
            box = self._inbox.get(agent_id, [])
            ids = set(message_ids)
            for m in box:
                if m.id in ids and not m.read:
                    m.read = True
                    marked.append(m.id)
        if marked:
            await emit(
                "message.received",
                self.session_id,
                {"agent_id": agent_id, "msg_ids": marked, "count": len(marked)},
            )
        return len(marked)

    async def get(self, msg_id: str) -> Message | None:
        async with self._lock:
            return self._all.get(msg_id)

    async def all_messages(self) -> list[Message]:
        """返回全部消息（按发送时间）——调试/可观测用"""
        async with self._lock:
            return sorted(self._all.values(), key=lambda m: m.timestamp)

    # ------------------------------------------------------------------
    # 恢复支持（W3）——SessionManager 用，绕过 emit
    # ------------------------------------------------------------------
    async def restore_message(self, msg: Message) -> None:
        """
        回放一条消息进 mailbox——绕过 send() 的 emit

        @note 仅供 SessionManager 在重放时调用
        """
        async with self._lock:
            if msg.to_agent is None:
                return  # broadcast 不应出现在事件流中
            self._inbox.setdefault(msg.to_agent, []).append(msg)
            self._all[msg.id] = msg

    async def restore_mark_read(self, msg_ids: list[str]) -> None:
        """回放消息已读标记——绕过 emit"""
        async with self._lock:
            ids = set(msg_ids)
            for mid in ids:
                m = self._all.get(mid)
                if m is not None:
                    m.read = True
