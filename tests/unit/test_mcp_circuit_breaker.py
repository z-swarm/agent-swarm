"""
@module tests.unit.test_mcp_circuit_breaker
@brief  W14a-5 CircuitBreaker 单元测试——DESIGN §7.3 熔断器

覆盖：
  - 正常状态：CLOSED 成功不计数
  - 失败计数：连续 N 次失败 → OPEN
  - 熔断后调用：直接抛 MCPCircuitOpenError（不调 fn）
  - 冷却：OPEN 状态 cool_off 后自动 → HALF_OPEN
  - HALF_OPEN 试探：成功 → CLOSED，失败 → OPEN
  - 总开关次数累计
  - 边界：threshold=1 / cool_off=0
"""

from __future__ import annotations

import asyncio

import pytest

from agent_swarm.mcp.reliability import (
    CircuitBreaker,
    CircuitState,
    MCPCircuitOpenError,
)


@pytest.mark.asyncio
async def test_closed_state_success_does_not_count() -> None:
    cb = CircuitBreaker(failure_threshold=3, cool_off_s=60.0, server_name="t1")
    cb2 = await cb.call(lambda: _async_return("ok"))
    assert cb2 == "ok"
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_closed_state_failure_increments_counter() -> None:
    cb = CircuitBreaker(failure_threshold=3, cool_off_s=60.0, server_name="t2")
    with pytest.raises(RuntimeError, match="boom"):
        await cb.call(lambda: _async_raise(RuntimeError("boom")))
    assert cb.consecutive_failures == 1
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_reaches_threshold_opens_circuit() -> None:
    cb = CircuitBreaker(failure_threshold=3, cool_off_s=60.0, server_name="t3")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(lambda: _async_raise(RuntimeError("boom")))
    assert cb.state == CircuitState.OPEN
    assert cb.consecutive_failures == 3
    assert cb.total_trips == 1


@pytest.mark.asyncio
async def test_open_state_rejects_without_calling_fn() -> None:
    """OPEN 状态直接抛 MCPCircuitOpenError，不调底层 fn"""
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=60.0, server_name="t4")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("boom")))
    assert cb.state == CircuitState.OPEN
    # 此时再调一次应该抛 MCPCircuitOpenError，fn 不应被调到
    called = False

    def _fn():
        nonlocal called
        called = True
        return _async_return("should not run")

    with pytest.raises(MCPCircuitOpenError) as exc_info:
        await cb.call(_fn)
    assert not called
    assert exc_info.value.server_name == "t4"
    assert exc_info.value.failure_count == 1
    assert "circuit OPEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cool_off_transitions_to_half_open() -> None:
    """OPEN 状态 cool_off 后变 HALF_OPEN；调用一次尝试"""
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=0.05, server_name="t5")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("boom")))
    assert cb.state == CircuitState.OPEN
    # 等待 cool_off
    await asyncio.sleep(0.06)
    # 状态访问触发转换
    assert cb.state == CircuitState.HALF_OPEN
    # 试探成功
    result = await cb.call(lambda: _async_return("recovered"))
    assert result == "recovered"
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert cb.total_trips == 2  # OPEN→HALF_OPEN 一次 + HALF_OPEN→CLOSED 一次


@pytest.mark.asyncio
async def test_half_open_failure_reopens_circuit() -> None:
    """HALF_OPEN 试探失败立即回 OPEN"""
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=0.05, server_name="t6")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("boom")))
    assert cb.state == CircuitState.OPEN
    await asyncio.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN
    # 试探失败
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("still broken")))
    assert cb.state == CircuitState.OPEN
    # 立即再调被拒绝
    with pytest.raises(MCPCircuitOpenError):
        await cb.call(lambda: _async_return("never"))


@pytest.mark.asyncio
async def test_threshold_one_opens_on_first_failure() -> None:
    """threshold=1 边界：1 次失败就熔断"""
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=60.0, server_name="t7")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("x")))
    assert cb.state == CircuitState.OPEN


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="failure_threshold must be >= 1"):
        CircuitBreaker(failure_threshold=0)


def test_invalid_cool_off_raises() -> None:
    with pytest.raises(ValueError, match="cool_off_s must be >= 0"):
        CircuitBreaker(failure_threshold=3, cool_off_s=-1.0)


@pytest.mark.asyncio
async def test_success_resets_consecutive_failures() -> None:
    """CLOSE 状态下成功调用清零连续失败计数"""
    cb = CircuitBreaker(failure_threshold=3, cool_off_s=60.0, server_name="t8")
    # 2 次失败
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(lambda: _async_raise(RuntimeError("x")))
    assert cb.consecutive_failures == 2
    # 1 次成功
    await cb.call(lambda: _async_return("ok"))
    assert cb.consecutive_failures == 0
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_manual_reset_returns_to_closed() -> None:
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=60.0, server_name="t9")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("x")))
    assert cb.state == CircuitState.OPEN
    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    # 之后能正常调
    result = await cb.call(lambda: _async_return("back"))
    assert result == "back"


@pytest.mark.asyncio
async def test_circuit_open_error_includes_remaining_time() -> None:
    """MCPCircuitOpenError 错误信息含剩余冷却时间"""
    cb = CircuitBreaker(failure_threshold=1, cool_off_s=10.0, server_name="t10")
    with pytest.raises(RuntimeError):
        await cb.call(lambda: _async_raise(RuntimeError("x")))
    with pytest.raises(MCPCircuitOpenError) as exc_info:
        await cb.call(lambda: _async_return("never"))
    msg = str(exc_info.value)
    assert "remaining=" in msg
    assert "cool_off=10.0s" in msg


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


async def _async_return(value: str) -> str:
    return value


async def _async_raise(exc: Exception) -> None:
    raise exc
