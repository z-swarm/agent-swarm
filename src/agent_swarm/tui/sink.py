"""
@module agent_swarm.tui.sink
@brief  TUISink —— 把 ObservabilityBus 事件桥接到 TUI 的 asyncio.Queue

设计:
  - 实现 ObservabilitySink 协议
  - consume() 内仅 put_nowait 到 Queue, 不做任何渲染逻辑（避免阻塞 emit 路径）
  - Queue 满时 drop 最旧——保护业务路径不被 TUI 拖慢

@note 不持有 Textual 引用——TUI App 自己决定如何消费 Queue（典型: worker 协程 get + post_message）
"""

from __future__ import annotations

import asyncio
import logging

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink

log = logging.getLogger(__name__)


class TUISink(ObservabilitySink):
    """
    @brief  把事件投递到内部 asyncio.Queue 的 sink

    @note Queue 默认 maxsize=1024——TUI 来不及消费时丢最旧而不是阻塞业务
    """

    def __init__(self, maxsize: int = 1024) -> None:
        """
        @param maxsize Queue 容量上限；超出后 drop 最旧事件
        """
        self.queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0

    async def consume(self, event: SessionEvent) -> None:
        """
        @brief 收到一条事件——尝试塞进 Queue, 满则 drop 最旧

        @note 永不抛——业务路径 await emit() 时不能因 TUI 渲染慢而失败
        """
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
                self.queue.put_nowait(event)
                self._dropped += 1
                if self._dropped % 100 == 1:
                    log.warning("tui.sink_queue_full dropped=%d", self._dropped)
            except asyncio.QueueEmpty:  # 极端竞态——忽略
                pass

    async def aclose(self) -> None:
        """关闭——丢弃残余事件即可（TUI 退出时一并丢弃）"""
        # 不清空 queue，让 TUI 自行消费/丢弃
        return None

    @property
    def dropped(self) -> int:
        """@brief 已丢弃事件数（监控用）"""
        return self._dropped
