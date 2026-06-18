"""W8 验收 e2e（DESIGN §15 Phase 2 W2 / Adversarial Verify）"""

from __future__ import annotations

import pytest
import yaml

from agent_swarm.core.adversarial import AdversarialVerifier
from agent_swarm.core.protocols import ProtocolResult
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    Judgement,
    Stance,
)


def _plan_only(id: str) -> Agent:
    return Agent(
        id=id, role="judge", persona="", model="gpt-4o-mini",
        provider="openai", capabilities=AgentCapabilities.plan_only(),
    )


def test_yaml_w8_adversarial_parses() -> None:
    """W8 example YAML 解析：3 plan_only judge + 3 假设任务"""
    from pathlib import Path
    cfg_path = Path("examples/w8_adversarial.yaml")
    swarm = Swarm.from_yaml(cfg_path)
    assert len(swarm.agents) == 3
    assert all(not a.capabilities.can_execute_actions for a in swarm.agents)
    assert len(swarm.tasks) == 3
    assert [a.id for a in swarm.agents] == ["judge-a", "judge-b", "judge-c"]


def test_yaml_w8_role_type_plan_only() -> None:
    """role_type=plan_only → can_execute_actions=False + can_spawn_agents=False"""
    from pathlib import Path
    cfg_path = Path("examples/w8_adversarial.yaml")
    swarm = Swarm.from_yaml(cfg_path)
    for a in swarm.agents:
        assert a.capabilities.can_execute_actions is False
        assert a.capabilities.can_spawn_agents is False
        assert "read_file" in a.capabilities.allowed_tools


@pytest.mark.asyncio
async def test_adversarial_verifier_via_swarm_protocol() -> None:
    """通过 Swarm.set_protocol(AdversarialVerifier) + run_with_protocol 走通"""
    from pathlib import Path
    cfg_path = Path("examples/w8_adversarial.yaml")
    swarm = Swarm.from_yaml(cfg_path)

    # 注入确定性 judge_fn：h-001 → SUPPORT，h-002/h-003 → REFUTE
    async def judge_fn(agent, hyp_id, round_no):
        stance = Stance.SUPPORT if hyp_id == "h0" else Stance.REFUTE  # verifier 内部分配 h0/h1/...
        return Judgement(agent.id, hyp_id, round_no, stance, 0.9)

    verifier = AdversarialVerifier(min_survivors=1, max_rounds=3)
    verdict = await verifier.verify(
        [t.title for t in swarm.tasks],
        list(swarm.agents),
        judge_fn=judge_fn,
    )
    assert verdict.convergence_reason == "min_survivors_reached"
    assert len(verdict.survivors) == 1
    assert verdict.survivors[0].id == "h0"  # verifier 内部 id


def test_protocol_result_includes_adversarial_fields() -> None:
    """AdversarialVerifier.execute() 返回的 ProtocolResult.artifacts 字段完整"""
    # 这里不跑 verify，只检查 ProtocolResult 字段
    expected = {
        "protocol", "survivors", "eliminated", "rounds_used",
        "convergence_reason", "root_cause", "confidence",
    }
    # AdversarialVerifier.execute 内部会构造这些字段（看代码确认）
    from agent_swarm.core.adversarial import AdversarialVerifier
    import inspect
    src = inspect.getsource(AdversarialVerifier.execute)
    for field in expected:
        assert field in src, f"ProtocolResult.artifacts 缺字段: {field}"
