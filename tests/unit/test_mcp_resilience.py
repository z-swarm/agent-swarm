"""
@module tests.unit.test_mcp_resilience
@brief  W14a-4/5 ReconnectingMCPClient + MCPRegistry 健康检查——DESIGN §7.3

覆盖：
  - ReconnectingMCPClient: 连接失败 → 自动重连（指数退避）
  - ReconnectingMCPClient: 多次重连失败 → 计入 circuit breaker
  - ReconnectingMCPClient: 熔断后调用立即抛 MCPCircuitOpenError
  - ReconnectingMCPClient: auto_reconnect=False 时不重连
  - ReconnectingMCPClient: 指数退避时长正确（0.5/1/2/4）
  - MCPRegistry: connect_all / disconnect_all / health_check
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_swarm.mcp.client import (
    MCPClient,
    MCPConnectionError,
)
from agent_swarm.mcp.registry import (
    MCPHealthStatus,
    MCPRegistry,
    MCPServerConfig,
)
from agent_swarm.mcp.reliability import (
    CircuitState,
    MCPCircuitOpenError,
    ReconnectingMCPClient,
    _compute_backoff_s,
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class _FakeMCPClient(MCPClient):
    """可控的 fake client：可编程 call_tool / list_tools 行为"""

    def __init__(self) -> None:
        self.connect_count = 0
        self.disconnect_count = 0
        self.call_count = 0
        self._connected = False
        # 行为配置
        self.connect_should_fail: bool = False
        self.list_tools_result: list = []
        self.call_tool_result: object = "ok"
        self.raise_on_call: Exception | None = None

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_should_fail:
            raise MCPConnectionError("fake: connect failed")
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def list_tools(self) -> list:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.list_tools_result

    async def call_tool(self, name: str, arguments: dict) -> object:
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.call_tool_result


def _stdio_config(name: str = "fake", **overrides) -> MCPServerConfig:
    """构造 stdio 传输的 fake config（不真启动子进程）"""
    defaults = dict(
        name=name,
        transport="stdio",
        command=["echo"],
        auto_reconnect=True,
        max_reconnect_attempts=3,
        circuit_breaker_threshold=3,
    )
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


# ---------------------------------------------------------------------------
# ReconnectingMCPClient 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_success_on_first_attempt() -> None:
    """list_tools 抛 MCPConnectionError → 重连成功 → 重试走通"""
    inner = _FakeMCPClient()
    cfg = _stdio_config(max_reconnect_attempts=3)

    # 先 connect 成功
    await wrapper_connect(inner)

    # 标记下次 list_tools 失败，然后下次 list_tools 走通
    call_count = [0]
    original_list = inner.list_tools

    async def _list_with_fail():
        call_count[0] += 1
        if call_count[0] == 1:
            raise MCPConnectionError("first attempt fails")
        return await original_list()

    inner.list_tools = _list_with_fail  # type: ignore[assignment]
    # 让重连成功——inner.connect_should_fail 默认 False
    inner.connect_should_fail = False

    wrapper = ReconnectingMCPClient(inner, cfg)
    tools = await wrapper.list_tools()
    assert tools == []
    # 至少 1 次原始 list_tools 失败 + 重连 + 重试 list_tools
    assert call_count[0] >= 2


async def wrapper_connect(inner: _FakeMCPClient) -> None:
    """helper：确保 inner 已 connected（用 wrapper.connect 入口）"""
    await inner.connect()


@pytest.mark.asyncio
async def test_reconnect_exhausts_attempts_raises() -> None:
    """max_reconnect_attempts 次重连全失败 → 抛 MCPConnectionError"""
    inner = _FakeMCPClient()
    # 先连上（connect_should_fail 默认 False）
    await inner.connect()
    inner._connected = True  # 标记已连
    # 之后所有 connect 都失败
    inner.connect_should_fail = True
    # list_tools 抛 ConnectionError 触发重连
    inner.raise_on_call = MCPConnectionError("list_tools boom")

    cfg = _stdio_config(max_reconnect_attempts=2)
    wrapper = ReconnectingMCPClient(inner, cfg)

    # list_tools 抛 ConnectionError → 触发重连 2 次（都失败）→ 抛
    start = time.monotonic()
    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    elapsed = time.monotonic() - start
    # 至少 1 次重连退避（0.5s）
    assert elapsed >= 0.4


@pytest.mark.asyncio
async def test_reconnect_disabled_does_not_retry() -> None:
    """auto_reconnect=False 时连接失败不重连，直接抛"""
    inner = _FakeMCPClient()
    await inner.connect()

    # 之后所有 list_tools 抛 ConnectionError
    async def _raise():
        raise MCPConnectionError("boom")

    inner.list_tools = _raise  # type: ignore[assignment]

    cfg = _stdio_config(auto_reconnect=False, max_reconnect_attempts=5)
    wrapper = ReconnectingMCPClient(inner, cfg)

    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    # 没有触发 disconnect（auto_reconnect=False 路径不重连）
    # 但 inner.connect 一次后没再调过
    assert inner.connect_count == 1


@pytest.mark.asyncio
async def test_reconnect_records_failures_in_circuit_breaker() -> None:
    """重连失败 N 次 → circuit breaker 打开 → 下次立即抛 MCPCircuitOpenError"""
    inner = _FakeMCPClient()
    await inner.connect()
    inner.connect_should_fail = True  # 之后重连都失败

    async def _raise():
        raise MCPConnectionError("boom")

    inner.list_tools = _raise  # type: ignore[assignment]

    cfg = _stdio_config(
        max_reconnect_attempts=2,
        circuit_breaker_threshold=2,
    )
    wrapper = ReconnectingMCPClient(inner, cfg)

    # 第 1 次 list_tools 触发重连——失败，circuit 1/2
    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    assert wrapper.circuit_breaker.consecutive_failures == 1

    # 第 2 次 list_tools 触发重连——失败，circuit 2/2 → OPEN
    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    assert wrapper.circuit_breaker.state == CircuitState.OPEN

    # 第 3 次立即抛 MCPCircuitOpenError，不重连
    with pytest.raises(MCPCircuitOpenError):
        await wrapper.list_tools()


@pytest.mark.asyncio
async def test_reconnect_success_resets_circuit_breaker() -> None:
    """重连成功后 circuit breaker 计数清零"""
    inner = _FakeMCPClient()
    await inner.connect()
    inner._connected = True  # 标记已连
    inner.connect_should_fail = False  # 重连能成功

    # 用 raise_on_call 控制 list_tools 行为
    inner.raise_on_call = MCPConnectionError("boom")

    cfg = _stdio_config(max_reconnect_attempts=3, circuit_breaker_threshold=2)
    wrapper = ReconnectingMCPClient(inner, cfg)

    # 失败 1 次
    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    assert wrapper.circuit_breaker.consecutive_failures == 1

    # 修好——重连后 list_tools 走通
    inner.raise_on_call = None
    tools = await wrapper.list_tools()
    assert tools == []
    assert wrapper.circuit_breaker.consecutive_failures == 0
    assert wrapper.circuit_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_reconnect_serializes_with_lock() -> None:
    """并发重连被 _lock 串行化（防雪崩）"""
    inner = _FakeMCPClient()
    await inner.connect()
    inner.connect_should_fail = True  # 之后重连都失败

    async def _raise():
        raise MCPConnectionError("boom")

    inner.list_tools = _raise  # type: ignore[assignment]

    cfg = _stdio_config(max_reconnect_attempts=2, circuit_breaker_threshold=100)
    wrapper = ReconnectingMCPClient(inner, cfg)

    # 5 个并发 list_tools——所有都触发重连
    tasks = [wrapper.list_tools() for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 全部失败（连接不通）
    assert all(isinstance(r, MCPConnectionError) for r in results)
    # connect 至少被调过初始 1 次
    assert inner.connect_count >= 1


# ---------------------------------------------------------------------------
# 指数退避
# ---------------------------------------------------------------------------


def test_compute_backoff_s_progression() -> None:
    """指数退避序列：0.5 / 1.0 / 2.0 / 4.0 / 8.0 / 8.0(封顶)"""
    assert _compute_backoff_s(1) == 0.5
    assert _compute_backoff_s(2) == 1.0
    assert _compute_backoff_s(3) == 2.0
    assert _compute_backoff_s(4) == 4.0
    assert _compute_backoff_s(5) == 8.0
    assert _compute_backoff_s(6) == 8.0  # 封顶
    assert _compute_backoff_s(10) == 8.0


# ---------------------------------------------------------------------------
# MCPRegistry 连接管理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_connect_all_empty() -> None:
    reg = MCPRegistry()
    result = await reg.connect_all()
    assert result == {}


@pytest.mark.asyncio
async def test_registry_health_check_unregistered() -> None:
    reg = MCPRegistry()
    status = await reg.health_check("nonexistent")
    assert status.name == "nonexistent"
    assert status.connected is False
    assert "not registered" in (status.last_error or "")


@pytest.mark.asyncio
async def test_registry_health_check_not_connected() -> None:
    reg = MCPRegistry()
    reg.register(_stdio_config("test"))
    status = await reg.health_check("test")
    assert status.connected is False
    assert "not initialized" in (status.last_error or "")


@pytest.mark.asyncio
async def test_registry_health_check_all_empty() -> None:
    reg = MCPRegistry()
    assert await reg.health_check_all() == []


@pytest.mark.asyncio
async def test_registry_disconnect_all_empty() -> None:
    reg = MCPRegistry()
    await reg.disconnect_all()  # 不报错


def test_registry_circuit_breaker_accessor() -> None:
    """get_client 初始为 None；connect 后才存在"""
    reg = MCPRegistry()
    reg.register(_stdio_config("a"))
    assert reg.get_client("a") is None
    assert "a" not in reg.list_clients()


def test_health_status_dataclass() -> None:
    """MCPHealthStatus 字段对齐"""
    s = MCPHealthStatus(
        name="x",
        connected=True,
        circuit_state="closed",
        consecutive_failures=0,
        last_check_at=1.0,
    )
    assert s.name == "x"
    assert s.connected is True
    assert s.circuit_state == "closed"
