"""单元测试：Swarm.set_protocol() + run_with_protocol() API（W7-4）"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_swarm.core.protocols import (
    CollaborationProtocol,
    DelegateMode,
    ProtocolResult,
)
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import Agent, AgentCapabilities, Task

# ---------------------------------------------------------------------------
# 测试用 fixture
# ---------------------------------------------------------------------------


def _build_minimal_swarm(protocol=None) -> Swarm:
    """
    构造最小 Swarm——只为测 set_protocol / run_with_protocol 的入口行为

    不真跑 LLM；run() 不会在本测试中被实际调用（除非协议触发）
    """
    lead = Agent(
        id="lead-1",
        role="lead",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.lead(),
    )
    worker = Agent(
        id="worker-1",
        role="worker",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.worker({"read_file"}),
    )
    task = Task(id="t-1", title="noop", description="noop")
    swarm = Swarm(name="test-swarm", agents=[lead, worker], tasks=[task])
    if protocol is not None:
        swarm.set_protocol(protocol)
    return swarm


# ---------------------------------------------------------------------------
# set_protocol
# ---------------------------------------------------------------------------


def test_set_protocol_initial_state_is_none() -> None:
    """新建 Swarm 时 protocol 属性默认为 None"""
    s = _build_minimal_swarm()
    assert s.protocol is None


def test_set_protocol_registers_protocol() -> None:
    """set_protocol() 后 swarm.protocol 指向已注册实例"""
    s = _build_minimal_swarm(protocol=DelegateMode())
    assert isinstance(s.protocol, DelegateMode)


def test_set_protocol_rejects_double_registration() -> None:
    """重复注册应抛 ValueError，避免后注册的协议静默覆盖"""
    s = _build_minimal_swarm(protocol=DelegateMode())
    with pytest.raises(ValueError, match="already has protocol"):
        s.set_protocol(DelegateMode())


# ---------------------------------------------------------------------------
# run_with_protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_protocol_requires_registration() -> None:
    """未注册协议就调用 run_with_protocol() → 抛 ValueError"""
    s = _build_minimal_swarm()  # protocol=None
    with pytest.raises(ValueError, match="no protocol registered"):
        await s.run_with_protocol()


@dataclass
class _OkProtocol(CollaborationProtocol):
    """stub 协议——直接返回成功 ProtocolResult，验证 run_with_protocol 透传"""

    return_value: ProtocolResult | None = None

    async def execute(self, swarm) -> ProtocolResult:  # type: ignore[override]
        return self.return_value or ProtocolResult(
            success=True, summary="stub-ok", artifacts={"swarm_name": swarm.name}
        )


@pytest.mark.asyncio
async def test_run_with_protocol_delegates_to_protocol() -> None:
    """run_with_protocol() 应调用 protocol.execute(self) 并透传其结果"""
    expected = ProtocolResult(success=True, summary="ok-from-protocol", artifacts={"k": "v"})
    s = _build_minimal_swarm(protocol=_OkProtocol(return_value=expected))
    result = await s.run_with_protocol()
    assert result.success is True
    assert result.summary == "ok-from-protocol"
    assert result.artifacts == {"k": "v"}


@dataclass
class _BoomProtocol(CollaborationProtocol):
    """stub 协议——execute 抛异常，验证 run_with_protocol 错误包装"""

    async def execute(self, swarm) -> ProtocolResult:  # type: ignore[override]
        raise RuntimeError("kaboom")


@pytest.mark.asyncio
async def test_run_with_protocol_wraps_protocol_exception() -> None:
    """协议抛异常时 run_with_protocol 包装成 success=False + error 描述"""
    s = _build_minimal_swarm(protocol=_BoomProtocol())
    result = await s.run_with_protocol()
    assert result.success is False
    assert "kaboom" in (result.error or "")
    assert "_BoomProtocol" in (result.error or "")
    assert result.artifacts["protocol"] == "_BoomProtocol"


# ---------------------------------------------------------------------------
# 向后兼容：run() 不受影响
# ---------------------------------------------------------------------------


def test_run_method_still_exists() -> None:
    """W7 不得破坏 Phase 1 的 Swarm.run() 入口"""
    s = _build_minimal_swarm()
    assert callable(getattr(s, "run", None))
