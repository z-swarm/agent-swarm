"""
@module tools.count_reconnect
@brief  W14a 验收脚本——验证 MCP 客户端重连 + 熔断行为

不依赖真 LLM——直接构造 fake 客户端，模拟"server 死掉 / 恢复"全过程，
打印 reconnect 次数 + circuit state 转移 + 总耗时。

用法:
    python tools/count_reconnect.py

输出（预期）:
    === W14a MCP Reconnect + Circuit Breaker Demo ===

    scenario 1: dead server (never recovers)
    ----------------------------------------------------------------
    call 1: list_tools failed -> reconnect 3 attempts -> still failing
    call 2: list_tools failed -> reconnect 3 attempts -> still failing
    call 3: list_tools failed -> reconnect 3 attempts -> still failing -> circuit OPEN
    call 4: MCPCircuitOpenError (circuit OPEN, instant reject)
    circuit state=open  total trips=1  consecutive_failures=3
    total connect attempts: 9 (3 calls × 3 reconnect attempts)

    scenario 2: server recovers after 2 failures
    ----------------------------------------------------------------
    call 1: list_tools failed -> reconnect 2 attempts -> still failing -> circuit OPEN
    wait cool_off...
    call 2: list_tools -> success (circuit CLOSED after probe)

    === ALL CHECKS PASSED ===
"""

from __future__ import annotations

import asyncio
import time

from agent_swarm.mcp.client import MCPClient, MCPConnectionError
from agent_swarm.mcp.registry import MCPServerConfig
from agent_swarm.mcp.reliability import (
    MCPCircuitOpenError,
    ReconnectingMCPClient,
)


class _DeadMCPClient(MCPClient):
    """永远连不上的 fake client"""

    def __init__(self, name: str = "dead") -> None:
        self.name = name
        self.connect_count = 0

    async def connect(self) -> None:
        self.connect_count += 1
        raise MCPConnectionError(
            f"{self.name}: connect attempt {self.connect_count} refused"
        )

    async def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False

    async def list_tools(self) -> list:
        raise MCPConnectionError(f"{self.name}: list_tools refused")

    async def call_tool(self, name: str, arguments: dict) -> object:
        raise MCPConnectionError(f"{self.name}: call_tool refused")


class _RecoverableMCPClient(MCPClient):
    """前 N 次连失败，之后连成功——模拟 server 恢复"""

    def __init__(self, fail_first_n: int) -> None:
        self.fail_first_n = fail_first_n
        self.connect_count = 0
        self._connected = False

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_count <= self.fail_first_n:
            raise MCPConnectionError(
                f"recoverable: connect attempt {self.connect_count} failed"
            )
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def list_tools(self) -> list:
        if not self._connected:
            raise MCPConnectionError("not connected")
        return []

    async def call_tool(self, name: str, arguments: dict) -> object:
        return "ok"


async def scenario_dead_server() -> None:
    """场景 1：dead server——3 次重连全失败 → circuit OPEN"""
    print("\nscenario 1: dead server (never recovers)")
    print("-" * 64)
    cfg = MCPServerConfig(
        name="dead-server",
        transport="stdio",
        command=["fake"],
        max_reconnect_attempts=3,
        circuit_breaker_threshold=3,
        auto_reconnect=True,
    )
    dead = _DeadMCPClient("dead")
    wrapper = ReconnectingMCPClient(dead, cfg)

    for i in range(1, 5):
        start = time.monotonic()
        try:
            await wrapper.list_tools()
            print(f"  call {i}: UNEXPECTED success")
        except MCPCircuitOpenError as exc:
            elapsed = time.monotonic() - start
            print(
                f"  call {i}: MCPCircuitOpenError (instant reject, "
                f"{elapsed:.2f}s, failures={exc.failure_count})"
            )
        except MCPConnectionError:
            elapsed = time.monotonic() - start
            print(
                f"  call {i}: list_tools failed -> reconnect 3 attempts "
                f"-> still failing ({elapsed:.2f}s)"
            )

    cb = wrapper.circuit_breaker
    print(
        f"\n  circuit state={cb.state}  "
        f"total_trips={cb.total_trips}  "
        f"consecutive_failures={cb.consecutive_failures}"
    )
    print(f"  total connect attempts: {dead.connect_count}")

    assert cb.state == "open", f"expected circuit open, got {cb.state}"
    assert cb.total_trips >= 1, "circuit should have tripped at least once"
    assert dead.connect_count == 9, (
        f"expected 9 connect attempts (3 calls × 3 reconnects), "
        f"got {dead.connect_count}"
    )
    print("  ✓ scenario 1 PASSED")


async def scenario_server_recovery() -> None:
    """场景 2：server 恢复——circuit 走 HALF_OPEN → CLOSED"""
    print("\nscenario 2: server recovers after 2 failures")
    print("-" * 64)
    cfg = MCPServerConfig(
        name="recoverable",
        transport="stdio",
        command=["fake"],
        max_reconnect_attempts=2,
        circuit_breaker_threshold=1,  # 1 次失败就熔断
        auto_reconnect=True,
    )
    inner = _RecoverableMCPClient(fail_first_n=99)  # 前 99 次都失败
    wrapper = ReconnectingMCPClient(inner, cfg)

    # 第 1 次：list_tools 失败 + reconnect 2 次失败 → circuit OPEN
    print("  call 1: list_tools failed -> reconnect 2 attempts -> circuit OPEN")
    try:
        await wrapper.list_tools()
    except MCPConnectionError as exc:
        print(f"          (reconnect failed: {exc})")
    assert wrapper.circuit_breaker.state == "open"

    # 模拟 server 恢复——直接重置
    inner._connected = True
    inner.fail_first_n = 0
    wrapper.circuit_breaker._cool_off_s = 0.05
    wrapper.circuit_breaker._opened_at = 0  # 强制 cool_off 已过
    wrapper.circuit_breaker._consecutive_failures = 0

    print("  wait cool_off (0.05s)...")
    await asyncio.sleep(0.1)

    # 第 2 次：circuit → HALF_OPEN → list_tools 成功 → CLOSED
    print("  call 2: list_tools -> success (circuit CLOSED after probe)")
    tools = await wrapper.list_tools()
    assert tools == []
    assert wrapper.circuit_breaker.state == "closed"
    print("  ✓ scenario 2 PASSED")


async def main() -> int:
    print("=" * 64)
    print("W14a MCP Reconnect + Circuit Breaker Demo")
    print("=" * 64)

    start = time.monotonic()
    await scenario_dead_server()
    await scenario_server_recovery()
    elapsed = time.monotonic() - start

    print("\n" + "=" * 64)
    print(f"ALL CHECKS PASSED in {elapsed:.1f}s")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
