"""
@module agent_swarm.observability.bus
@brief  ObservabilityBus（W3 落地版本）

DESIGN.md §5.2 / §5.4 完整版。W3 阶段:
  - emit_event(SessionEvent) → 同步派发给所有订阅 sink
  - Sink 内部异步处理（不阻塞业务路径）
  - JsonLogSink（默认开启，stderr）
  - InMemorySink（测试/调试，按 session 分组）
  - SqliteEventSink（W3 #19 单独实现）

W4+ 扩展:
  - 事件名 glob 过滤
  - WebSocketSink (TUI)
  - PrometheusSink

设计要点:
  - emit 永不抛——sink 内部异常被捕获并记 warning，不影响业务
  - bus 不持有 session_id：调用方填入 SessionEvent.session_id
  - seq 由 bus 内部分发——同一 session 单调递增，保证回放有序
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from agent_swarm.core.types import SessionEvent

log = logging.getLogger(__name__)


class ObservabilitySink(ABC):
    """事件接收方抽象——sink 应保证 consume() 不抛异常"""

    @abstractmethod
    async def consume(self, event: SessionEvent) -> None:
        """
        消费一条事件

        @note 实现必须自己捕获所有异常；bus 也会兜底 try/except，
              但 sink 内部 catch 更便于上下文准确的错误日志
        """
        ...

    async def aclose(self) -> None:
        """可选：sink 关闭钩子（如刷盘、关连接）"""
        return None


class ObservabilityBus:
    """
    事件总线——单例模式，通过 set_global / current 注入

    @note 不直接持锁——多 agent 并发 emit 时各自 await sink.consume()
          顺序由 _seq_counters[session_id] 保证（asyncio.Lock 保护）
    """

    def __init__(self) -> None:
        self._sinks: list[ObservabilitySink] = []
        # 每个 session 独立的单调序号
        self._seq_counters: dict[str, int] = {}
        self._seq_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def register_sink(self, sink: ObservabilitySink) -> None:
        """添加订阅者——所有事件都会广播给注册的 sink"""
        self._sinks.append(sink)
        log.debug("obs.sink_registered name=%s", type(sink).__name__)

    def unregister_sink(self, sink: ObservabilitySink) -> None:
        """移除订阅者（测试用）"""
        if sink in self._sinks:
            self._sinks.remove(sink)

    @property
    def sinks(self) -> list[ObservabilitySink]:
        """暴露 sink 列表（只读视图）——便于测试断言"""
        return list(self._sinks)

    # ------------------------------------------------------------------
    # 发出事件
    # ------------------------------------------------------------------
    async def emit_event(
        self,
        event_name: str,
        session_id: str,
        payload: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> SessionEvent:
        """
        广播一条事件到所有 sink

        @return 已分配 seq 的 SessionEvent（便于调用方关联）
        """
        # 分配 seq——单调递增，同 session 内严格有序
        async with self._seq_lock:
            seq = self._seq_counters.get(session_id, 0)
            self._seq_counters[session_id] = seq + 1

        evt = SessionEvent(
            event_name=event_name,
            session_id=session_id,
            timestamp=time.time(),
            payload=payload or {},
            seq=seq,
            request_id=request_id,
        )

        # 派发——sink 抛异常不影响其他 sink 与业务
        for sink in self._sinks:
            try:
                await sink.consume(evt)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "obs.sink_error sink=%s event=%s err=%s",
                    type(sink).__name__,
                    event_name,
                    exc,
                )
        return evt

    async def aclose(self) -> None:
        """关闭所有 sink 并清空订阅列表（W3-Z3）"""
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning("obs.sink_close_error sink=%s err=%s", type(sink).__name__, exc)
        # 清空——避免 close 后误用已关闭 sink
        self._sinks.clear()


# ---------------------------------------------------------------------------
# 全局单例（contextvars 风格）
# ---------------------------------------------------------------------------


_global_bus: ObservabilityBus | None = None


def set_global_bus(bus: ObservabilityBus | None) -> None:
    """设置全局 bus——Swarm.run() 启动时调一次"""
    global _global_bus
    _global_bus = bus


def get_global_bus() -> ObservabilityBus | None:
    """获取当前全局 bus（可能为 None——表示无 observability，emit 应跳过）"""
    return _global_bus


async def emit(
    event_name: str,
    session_id: str,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> SessionEvent | None:
    """
    便捷函数——任何模块直接调用此 emit() 不必持有 bus 实例

    @return SessionEvent | None（无 bus 时返回 None）
    """
    bus = _global_bus
    if bus is None:
        return None
    return await bus.emit_event(event_name, session_id, payload, request_id)
