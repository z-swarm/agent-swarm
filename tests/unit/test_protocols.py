"""单元测试：CollaborationProtocol 抽象基类 + ProtocolResult + DelegateMode"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_swarm.core.protocols import (
    CollaborationProtocol,
    DelegateMode,
    ProtocolResult,
)
from agent_swarm.core.types import Agent, AgentCapabilities, Task

# ---------------------------------------------------------------------------
# ProtocolResult
# ---------------------------------------------------------------------------


def test_protocol_result_minimal() -> None:
    """ProtocolResult 最小构造：仅 success=True，其他字段默认"""
    r = ProtocolResult(success=True)
    assert r.success is True
    assert r.summary == ""
    assert r.error is None
    assert r.artifacts == {}


def test_protocol_result_with_error() -> None:
    """失败时 error 字段承载原因"""
    r = ProtocolResult(success=False, error="all agents failed")
    assert r.success is False
    assert r.error == "all agents failed"


def test_protocol_result_with_artifacts() -> None:
    """artifacts 字段可承载结构化数据（verdict / final_report 等）"""
    r = ProtocolResult(
        success=True,
        summary="all done",
        artifacts={"verdict": "root_cause: X", "confidence": 0.92},
    )
    assert r.artifacts["verdict"] == "root_cause: X"
    assert r.artifacts["confidence"] == 0.92


# ---------------------------------------------------------------------------
# CollaborationProtocol ABC
# ---------------------------------------------------------------------------


def test_collaboration_protocol_is_abstract() -> None:
    """直接实例化抽象类应当失败"""
    with pytest.raises(TypeError):
        CollaborationProtocol()  # type: ignore[abstract]


@dataclass
class _StubProtocol(CollaborationProtocol):
    """最小具体实现——仅做协议注册与 execute 调用计数"""

    call_count: int = 0
    return_value: ProtocolResult | None = None

    async def execute(self, swarm) -> ProtocolResult:  # type: ignore[override]
        self.call_count += 1
        return self.return_value or ProtocolResult(success=True, summary="stub")


@pytest.mark.asyncio
async def test_stub_protocol_execute() -> None:
    """具体协议可被实例化、execute 可被调用"""
    p = _StubProtocol(return_value=ProtocolResult(success=True, summary="hi"))
    result = await p.execute(swarm=None)  # stub 不真用 swarm
    assert result.success is True
    assert result.summary == "hi"
    assert p.call_count == 1


# ---------------------------------------------------------------------------
# DelegateMode——partition 单元 + execute 校验
# ---------------------------------------------------------------------------


def _agent(agent_id: str, *, spawn: bool = False, execute: bool = True) -> Agent:
    """构造测试用 Agent——只填 DelegateMode 关心的字段"""
    if spawn and not execute:
        caps = AgentCapabilities.lead()
    elif execute:
        caps = AgentCapabilities.worker({"read_file"})
    else:
        caps = AgentCapabilities.plan_only()
    return Agent(
        id=agent_id,
        role="test",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=caps,
    )


def test_delegate_mode_partition_classifies_lead_and_worker() -> None:
    """partition 把 agent 拆成 leads/workers（按 can_spawn_agents / can_execute_actions）"""
    lead = _agent("lead-1", spawn=True, execute=False)
    worker_a = _agent("worker-a", execute=True)
    worker_b = _agent("worker-b", execute=True)
    plan = _agent("plan-1", spawn=False, execute=False)  # plan_only

    leads, workers = DelegateMode._partition([lead, worker_a, worker_b, plan])
    assert [a.id for a in leads] == ["lead-1"]
    # plan_only 既非 lead 也非 worker（advisor/judge 角色，由 W8+ Adversarial Verify 用）
    assert [a.id for a in workers] == ["worker-a", "worker-b"]


def test_delegate_mode_partition_includes_plan_only_warning() -> None:
    """plan_only 角色在 partition 中被忽略——这是预期行为（DESIGN §6.1 仅划 lead/worker）"""
    plan = _agent("plan-1", spawn=False, execute=False)
    leads, workers = DelegateMode._partition([plan])
    assert leads == []
    assert workers == []


@dataclass
class _StubSwarm:
    """最小可被 DelegateMode 调用的 swarm 双胞胎

    只暴露 DelegateMode 真正用到的属性/方法：agents / tasks / run()
    """

    agents: list[Agent]
    tasks: list[Task]
    run_result_state: str = "completed"
    run_should_raise: bool = False

    async def run(self) -> Any:
        if self.run_should_raise:
            raise RuntimeError("simulated swarm failure")
        # 返回带 state 字段的对象（DelegateMode 只读 state）
        return _StubSwarmResult(state=self.run_result_state)


@dataclass
class _StubSwarmResult:
    state: str


async def _run_with_agents(agents: list[Agent]) -> ProtocolResult:
    return await DelegateMode().execute(_StubSwarm(agents=agents, tasks=[]))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delegate_mode_rejects_no_lead() -> None:  # noqa: F811
    """无 lead → fail-fast，避免跑空"""
    workers = [_agent(f"w{i}", execute=True) for i in range(2)]
    result = await _run_with_agents(workers)
    assert result.success is False
    assert "lead" in (result.error or "")
    assert "DelegateMode" in (result.error or "")


@pytest.mark.asyncio
async def test_delegate_mode_rejects_no_worker() -> None:
    """无 worker → fail-fast"""
    lead = _agent("lead-1", spawn=True, execute=False)
    result = await _run_with_agents([lead])
    assert result.success is False
    assert "worker" in (result.error or "")


@pytest.mark.asyncio
async def test_delegate_mode_rejects_empty_swarm() -> None:
    """空 swarm → fail-fast（先报 lead 缺失）"""
    result = await _run_with_agents([])
    assert result.success is False
    assert "lead" in (result.error or "")


@pytest.mark.asyncio
async def test_delegate_mode_happy_path_runs_swarm() -> None:
    """正常 lead+worker → 触发 swarm.run() 并返回成功"""
    lead = _agent("lead-1", spawn=True, execute=False)
    worker = _agent("worker-1", execute=True)
    swarm = _StubSwarm(
        agents=[lead, worker],
        tasks=[],
        run_result_state="completed",
    )
    result = await DelegateMode().execute(swarm)  # type: ignore[arg-type]
    assert result.success is True
    assert "Delegated" in result.summary
    assert result.artifacts["mode"] == "delegate"
    assert result.artifacts["leads"] == ["lead-1"]
    assert result.artifacts["workers"] == ["worker-1"]
    assert result.artifacts["swarm_state"] == "completed"


@pytest.mark.asyncio
async def test_delegate_mode_propagates_swarm_failure() -> None:
    """swarm.run() 抛异常 → ProtocolResult.success=False + 错误包装"""
    lead = _agent("lead-1", spawn=True, execute=False)
    worker = _agent("worker-1", execute=True)
    swarm = _StubSwarm(agents=[lead, worker], tasks=[], run_should_raise=True)
    result = await DelegateMode().execute(swarm)  # type: ignore[arg-type]
    assert result.success is False
    assert "simulated swarm failure" in (result.error or "")
    # 即便失败，artifacts 仍应有 lead/worker 分类（便于排查）
    assert result.artifacts["leads"] == ["lead-1"]
    assert result.artifacts["workers"] == ["worker-1"]


@pytest.mark.asyncio
async def test_delegate_mode_marks_failed_when_swarm_state_failed() -> None:
    """swarm.run() 返回 state=failed → ProtocolResult.success=False"""
    lead = _agent("lead-1", spawn=True, execute=False)
    worker = _agent("worker-1", execute=True)
    swarm = _StubSwarm(agents=[lead, worker], tasks=[], run_result_state="failed")
    result = await DelegateMode().execute(swarm)  # type: ignore[arg-type]
    assert result.success is False
    assert result.artifacts["swarm_state"] == "failed"
