"""
@module agent_swarm.mcp.reliability
@brief  W14a-5 MCP 可靠性：CircuitBreaker + 重连 + 熔断错误——DESIGN §7.3

设计要点（DESIGN §7.3 "MCP 可靠性策略" v4.1）：
  - 凭证管理（SecretManager）：W14a 范围之外（Phase 3 W20 Vault 集成）
  - 连接监控（health check + auto_reconnect）：W14a-4 落地
  - 熔断（circuit breaker）：连续 circuit_breaker_threshold 次失败 → 60s 内
    该 server 全部 tools 立即返回 MCPCircuitOpenError，避免雪崩
  - 凭证轮换（SecretManager.rotate）：Phase 3 W20

W14a-5 范围：
  - CircuitBreaker 类（独立 + 可嵌入 MCPRegistry 包装 server 调用）
  - MCPCircuitOpenError 异常
  - ReconnectingMCPClient 装饰器：把任意 MCPClient 包成"调用失败自动重连
    → 重连后再失败计入 circuit breaker → 熔断"

@note W14a-5 不动 StdioMCPClient / SseMCPClient 自身实现；只做包装
@note 重试策略：指数退避（0.5s → 1s → 2s → 4s），最多 max_reconnect_attempts 次
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_swarm.mcp.client import MCPClient, MCPConnectionError

if TYPE_CHECKING:
    from agent_swarm.mcp.registry import MCPServerConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class MCPCircuitOpenError(MCPConnectionError):
    """Circuit breaker 打开——该 server 全部 tools 暂时不可用

    DESGIN §7.3: "连续 circuit_breaker_threshold 次工具调用失败 →
    该 server 的所有 tools 在 60 秒内被标记不可用"

    @note 60s 后自动进入 HALF_OPEN——下次调用尝试重连
    """

    def __init__(
        self,
        server_name: str,
        failure_count: int,
        opened_at: float,
        cool_off_s: float,
    ) -> None:
        self.server_name = server_name
        self.failure_count = failure_count
        self.opened_at = opened_at
        self.cool_off_s = cool_off_s
        # 距可重试剩余秒数
        remaining = max(0.0, cool_off_s - (time.monotonic() - opened_at))
        super().__init__(
            f"MCP {server_name!r} circuit OPEN "
            f"(failures={failure_count}, "
            f"cool_off={cool_off_s}s, "
            f"remaining={remaining:.1f}s)"
        )


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState:
    """熔断器状态机（DESIGN §7.3 三态）"""

    CLOSED = "closed"          # 正常——失败计数，连续 N 次 → OPEN
    OPEN = "open"              # 熔断——任何调用立即抛 MCPCircuitOpenError
    HALF_OPEN = "half_open"    # 半开——允许 1 次试探；成功 → CLOSED，失败 → OPEN


@dataclass
class CircuitBreaker:
    """
    熔断器——DESIGN §7.3

    @param failure_threshold  连续失败次数达此值 → 切 OPEN
    @param cool_off_s         OPEN 状态持续秒数；到时切 HALF_OPEN
    @param server_name        仅用于日志/异常

    用法：
        cb = CircuitBreaker(failure_threshold=3, cool_off_s=60.0, server_name="github")
        try:
            result = await cb.call(client.list_tools)
        except MCPCircuitOpenError:
            # 当前 server 不可用
            ...
    """

    failure_threshold: int = 3
    cool_off_s: float = 60.0
    server_name: str = "unnamed"

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be >= 1, got {self.failure_threshold}"
            )
        if self.cool_off_s < 0:
            raise ValueError(
                f"cool_off_s must be >= 0, got {self.cool_off_s}"
            )
        self._state: str = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: float = 0.0
        self._total_failures: int = 0
        self._total_trips: int = 0  # OPEN 切回次数累计

    @property
    def state(self) -> str:
        """当前状态（带时间触发的 OPEN→HALF_OPEN 转换）"""
        self._maybe_half_open()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def total_trips(self) -> int:
        return self._total_trips

    def _maybe_half_open(self) -> None:
        """OPEN 状态到 cool_off 后自动切 HALF_OPEN（lazy——下次 state 访问时）"""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.cool_off_s:
                log.info(
                    "MCP %s circuit OPEN → HALF_OPEN (after %.1fs cool-off)",
                    self.server_name, self.cool_off_s,
                )
                self._state = CircuitState.HALF_OPEN

    def _record_success(self) -> None:
        """调用成功——重置失败计数 + 关闭熔断"""
        if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            log.info(
                "MCP %s circuit %s → CLOSED (probe succeeded)",
                self.server_name, self._state,
            )
            self._total_trips += 1
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """调用失败——递增计数；达到阈值切 OPEN"""
        self._consecutive_failures += 1
        self._total_failures += 1
        if (
            self._state == CircuitState.HALF_OPEN
            or self._consecutive_failures >= self.failure_threshold
        ):
            if self._state != CircuitState.OPEN:
                log.warning(
                    "MCP %s circuit %s → OPEN (failures=%d, threshold=%d)",
                    self.server_name, self._state,
                    self._consecutive_failures, self.failure_threshold,
                )
                self._total_trips += 1
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    async def call(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        调用 fn 一次；遵守熔断器规则

        @raise MCPCircuitOpenError 当前 OPEN——直接拒绝
        @raise fn 自身的异常（视为一次失败）
        """
        self._maybe_half_open()
        if self._state == CircuitState.OPEN:
            raise MCPCircuitOpenError(
                server_name=self.server_name,
                failure_count=self._consecutive_failures,
                opened_at=self._opened_at,
                cool_off_s=self.cool_off_s,
            )
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def reset(self) -> None:
        """手动重置（健康检查发现 server 恢复时调用）"""
        if self._state != CircuitState.CLOSED:
            log.info("MCP %s circuit reset to CLOSED", self.server_name)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0


# ---------------------------------------------------------------------------
# Reconnecting MCPClient 装饰器——DESIGN §7.3 "断连后按 max_reconnect_attempts 自动重连（指数退避）"
# ---------------------------------------------------------------------------


def _compute_backoff_s(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """指数退避：0.5 / 1 / 2 / 4 / 8...（封顶 8s）"""
    return min(cap, base * (2 ** max(0, attempt - 1)))


class ReconnectingMCPClient:
    """
    把任意 MCPClient 包成"自动重连 + 熔断"版本——DESIGN §7.3

    行为：
      - 调用方法（list_tools / call_tool / initialize）：
        1. 检查 circuit breaker；OPEN → 立即抛 MCPCircuitOpenError
        2. 调底层 client；若抛 MCPConnectionError → 重连（最多 max_reconnect_attempts 次，
           指数退避）；重连后再调一次原方法；仍失败 → 计入 circuit breaker
        3. 调底层 client 成功 → 计入 circuit breaker 成功
      - is_connected 转发到底层

    @note W14a-5 范围：只包装；不改 StdioMCPClient / SseMCPClient 自身
    @note 这里的"重连"对 stdio 是 terminate+start_subprocess_exec，对 sse 是 close+reopen
    @note 防无限重连：单次失败触发一轮重连循环（最多 N 次），重连后再失败 → 直接抛
          （不让 circuit breaker 反复重连同一死 server）
    """

    def __init__(
        self,
        inner: MCPClient,
        config: MCPServerConfig,
    ) -> None:
        self._inner = inner
        self._config = config
        self._breaker = CircuitBreaker(
            failure_threshold=config.circuit_breaker_threshold,
            cool_off_s=60.0,
            server_name=config.name,
        )
        self._lock = asyncio.Lock()  # 防并发 reconnect
        # 标记本次调用是否已尝试过重连——防单次 list_tools 失败触发无限 reconnect 循环
        self._reconnect_attempted: bool = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def inner(self) -> MCPClient:
        return self._inner

    async def connect(self) -> None:
        await self._inner.connect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    async def _reconnect_with_backoff(self) -> None:
        """按 max_reconnect_attempts 指数退避重连"""
        # MCPConnectionError 触发重连；其他异常（MCPRPCError 等）不重连（应用层错误）
        last_exc: Exception | None = None
        for attempt in range(1, self._config.max_reconnect_attempts + 1):
            backoff = _compute_backoff_s(attempt)
            log.info(
                "MCP %s reconnect attempt %d/%d after %.1fs backoff",
                self._config.name, attempt,
                self._config.max_reconnect_attempts, backoff,
            )
            await asyncio.sleep(backoff)
            try:
                await self._inner.disconnect()
                await self._inner.connect()
                log.info("MCP %s reconnected (attempt %d)",
                         self._config.name, attempt)
                return
            except MCPConnectionError as exc:
                last_exc = exc
                log.warning("MCP %s reconnect attempt %d failed: %s",
                            self._config.name, attempt, exc)
        # 重连全部失败
        raise MCPConnectionError(
            f"MCP {self._config.name!r} failed to reconnect after "
            f"{self._config.max_reconnect_attempts} attempts: {last_exc}"
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._breaker.call(
            self._list_tools_with_reconnect,
        )

    async def _list_tools_with_reconnect(self) -> list[dict[str, Any]]:
        # 关键：单次 list_tools 调用最多触发 1 次 reconnect 循环（防无限循环）
        # 多次失败由 circuit breaker 累计 → 熔断 OPEN → 后续快速拒绝
        try:
            return await self._inner.list_tools()
        except MCPConnectionError as exc:
            if not self._config.auto_reconnect or self._reconnect_attempted:
                raise
            log.warning("MCP %s list_tools failed: %s — reconnecting (single shot)",
                        self._config.name, exc)
            # 标记 + lock——保护本次单次调用；同时 lock 也防并发 reconnect
            self._reconnect_attempted = True
            try:
                await self._reconnect_with_backoff()
                # 重连成功后再调一次；如果仍抛，向上抛（不再触发新一轮重连）
                return await self._inner.list_tools()
            finally:
                self._reconnect_attempted = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._breaker.call(
            self._call_tool_with_reconnect, name, arguments,
        )

    async def _call_tool_with_reconnect(
        self, name: str, arguments: dict[str, Any],
    ) -> Any:
        try:
            return await self._inner.call_tool(name, arguments)
        except MCPConnectionError as exc:
            if not self._config.auto_reconnect or self._reconnect_attempted:
                raise
            log.warning("MCP %s call_tool(%s) failed: %s — reconnecting (single shot)",
                        self._config.name, name, exc)
            self._reconnect_attempted = True
            try:
                await self._reconnect_with_backoff()
                return await self._inner.call_tool(name, arguments)
            finally:
                self._reconnect_attempted = False

    async def initialize(self) -> dict[str, Any]:
        # MCPClient ABC 没声明 initialize（initialize 是 MCP 协议层握手）
        # 用 getattr 防御式调用
        if not hasattr(self._inner, "initialize"):
            raise NotImplementedError(
                f"MCP {self._config.name!r} client has no initialize()"
            )
        inner_init = self._inner.initialize
        return await self._breaker.call(inner_init)


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "MCPCircuitOpenError",
    "ReconnectingMCPClient",
]
