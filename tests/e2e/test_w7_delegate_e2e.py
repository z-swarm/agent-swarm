"""
@module tests.e2e.test_w7_delegate_e2e
@brief  W7 验收 e2e（DESIGN §15 Phase 2 W1 / Delegate Mode DoD）

DoD:
  ① Swarm.from_yaml 支持 lead/worker/plan_only 角色（role_type 字段）
  ② Swarm.set_protocol(DelegateMode()) + run_with_protocol() 走通
  ③ lead agent 的 tools 注入 lead 工具集（spawn_agent/shutdown_agent/
     assign_task/update_task/review_plan）
  ④ worker agent 的 tools **不**含 lead 工具
  ⑤ DelegateMode 协议返回 ProtocolResult 携带 lead/worker 分组
  ⑥ YAML 校验：role_type 非法值被拒绝
  ⑦ plan_only agent 在 DelegateMode partition 中既不归 lead 也不归 worker
     （DESIGN §6.1）

实现策略：
  - 用 FakeLLMProvider 跑 agent——script 让 lead/worker 几次后 stop（不真调工具）
  - 验证主路径：YAML → Swarm → set_protocol → run_with_protocol → ProtocolResult
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agent_swarm.cli.main import cli
from agent_swarm.core.protocols import DelegateMode
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import (
    AgentCapabilities,
    Task,
    ToolCall,
)
from agent_swarm.providers.base import LLMProvider
from tests.conftest import FakeLLMProvider, ScriptedResponse


# ---------------------------------------------------------------------------
# 共享 fixture：最小 W7 配置
# ---------------------------------------------------------------------------


def _w7_yaml() -> dict:
    return {
        "name": "w7-delegate-e2e",
        "agents": [
            {
                "id": "lead",
                "role": "lead",
                "role_type": "lead",
                "persona": "you are the lead",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "max_iterations": 2,
            },
            {
                "id": "worker-1",
                "role": "reader",
                "role_type": "worker",
                "persona": "you read files",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["read_file"],
                "max_iterations": 2,
            },
        ],
        "tasks": [
            {
                "id": "t-1",
                "title": "noop",
                "description": "noop",
                "assigned_to": "worker-1",
            },
        ],
    }


def _write_cfg(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "w7.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _stop_script(n: int) -> list[ScriptedResponse]:
    """生成 n 次 'no-op stop' 响应，让 agent loop 自然退出"""
    return [
        ScriptedResponse(content="ok", finish_reason="stop") for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# ① YAML 解析支持 lead role
# ---------------------------------------------------------------------------


def test_yaml_parses_lead_role(tmp_path: Path) -> None:
    """YAML role_type=lead → AgentCapabilities.lead()（DESIGN §7.1）"""
    p = _write_cfg(tmp_path, _w7_yaml())
    swarm = Swarm.from_yaml(p)
    lead = next(a for a in swarm.agents if a.id == "lead")
    assert lead.capabilities.can_spawn_agents is True
    assert lead.capabilities.can_shutdown_agents is True
    assert lead.capabilities.can_assign_tasks is True
    assert lead.capabilities.can_execute_actions is False


def test_yaml_parses_worker_role(tmp_path: Path) -> None:
    """YAML role_type=worker → AgentCapabilities.worker()（默认 + 向后兼容）"""
    p = _write_cfg(tmp_path, _w7_yaml())
    swarm = Swarm.from_yaml(p)
    w = next(a for a in swarm.agents if a.id == "worker-1")
    assert w.capabilities.can_execute_actions is True
    assert w.capabilities.can_spawn_agents is False


def test_yaml_default_role_is_worker(tmp_path: Path) -> None:
    """省略 role_type → 默认为 worker（Phase 1 YAML 兼容）"""
    cfg = _w7_yaml()
    cfg["agents"][1].pop("role_type")  # 移除显式 role_type
    p = _write_cfg(tmp_path, cfg)
    swarm = Swarm.from_yaml(p)
    w = next(a for a in swarm.agents if a.id == "worker-1")
    assert w.capabilities.can_execute_actions is True


def test_yaml_rejects_invalid_role_type(tmp_path: Path) -> None:
    """role_type 非法值应被拒绝（fail-fast）"""
    cfg = _w7_yaml()
    cfg["agents"][0]["role_type"] = "overlord"
    p = _write_cfg(tmp_path, cfg)
    with pytest.raises(ValueError, match="role_type must be one of"):
        Swarm.from_yaml(p)


# ---------------------------------------------------------------------------
# ② 协议入口可走通
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_mode_protocol_runs_end_to_end(tmp_path: Path) -> None:
    """YAML → Swarm → set_protocol(DelegateMode) → run_with_protocol → ProtocolResult"""
    p = _write_cfg(tmp_path, _w7_yaml())
    swarm = Swarm.from_yaml(p)

    # 注入 fake provider：让 agent 跑 max_iterations 次后 stop
    fake = FakeLLMProvider()
    fake.script = _stop_script(8)  # 2 agents × max 2 iter + 余量
    # _build_provider 是 Swarm 私有 API；W7 e2e 用 monkey-patch 替换
    swarm._build_provider = lambda agent: fake  # type: ignore[assignment]

    swarm.set_protocol(DelegateMode())
    result = await swarm.run_with_protocol()

    assert result.success is True
    assert "Delegated" in result.summary
    assert result.artifacts["leads"] == ["lead"]
    assert result.artifacts["workers"] == ["worker-1"]


# ---------------------------------------------------------------------------
# ③/④ lead 工具按 capability 注入
# ---------------------------------------------------------------------------


def test_lead_agent_gets_lead_tools() -> None:
    """lead agent 的 tools 包含 5 个 lead 工具"""
    from agent_swarm.tools.builtin.lead import build_lead_tools

    lead = _build_agent_with_lead()
    tools = build_lead_tools(lead.id, _StubCtx([lead]))
    names = {t.name for t in tools}
    assert names == {
        "spawn_agent",
        "shutdown_agent",
        "assign_task",
        "update_task",
        "review_plan",
    }


def test_worker_agent_has_no_lead_tools() -> None:
    """worker 没 can_spawn_agents → build_lead_tools 仍返回 5 工具，但 invoke 时被拒绝

    （W7 设计：构造时不剔除，权限校验在 invoke() 内——双层保护：
     AgentRunner 只会把 capabilities.allowed_tools 里的工具塞给 agent，
     而 lead 默认 allowed_tools 不含 lead 工具 id，所以 worker agent
     实际不会拿到这些工具）
    """
    from agent_swarm.core.types import Agent
    from agent_swarm.tools.builtin.lead import SpawnAgentTool

    w = Agent(
        id="w",
        role="worker",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.worker({"read_file"}),
    )
    assert "spawn_agent" not in w.capabilities.allowed_tools
    # 即便硬塞，invoke 也会拒绝（无 can_spawn_agents）
    tool = SpawnAgentTool(caller_agent_id="w", ctx=_StubCtx([w]))
    import asyncio

    out = asyncio.run(
        tool.invoke({"agent_id": "x", "role": "x", "model": "m", "provider": "p"})
    )
    assert "[error]" in out
    assert "can_spawn_agents" in out


def _build_agent_with_lead():
    from agent_swarm.core.types import Agent

    return Agent(
        id="lead",
        role="lead",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.lead(),
    )


class _StubCtx:
    def __init__(self, agents):
        self._agents = {a.id: a for a in agents}

    def add_agent(self, agent) -> None:
        self._agents[agent.id] = agent

    def remove_agent(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None

    def get_agent(self, agent_id: str):
        return self._agents.get(agent_id)

    def list_agents(self):
        return list(self._agents.values())

    def assign_task_to(self, task_id: str, agent_id: str) -> bool:
        return False

    def update_task_status(self, task_id: str, status: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# ⑤ ProtocolResult artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_mode_artifacts_carry_lead_worker_split(tmp_path: Path) -> None:
    """ProtocolResult.artifacts 含 mode / leads / workers / tasks_* / swarm_state"""
    p = _write_cfg(tmp_path, _w7_yaml())
    swarm = Swarm.from_yaml(p)
    fake = FakeLLMProvider()
    fake.script = _stop_script(8)
    swarm._build_provider = lambda agent: fake  # type: ignore[assignment]

    swarm.set_protocol(DelegateMode())
    result = await swarm.run_with_protocol()
    art = result.artifacts
    assert art["mode"] == "delegate"
    assert "leads" in art and "workers" in art
    assert art["tasks_total"] == 1
    assert art["tasks_completed"] + art["tasks_failed"] <= art["tasks_total"]
    assert art["swarm_state"] in ("completed", "failed")


# ---------------------------------------------------------------------------
# ⑦ plan_only 角色
# ---------------------------------------------------------------------------


def test_yaml_parses_plan_only_role(tmp_path: Path) -> None:
    """role_type=plan_only → AgentCapabilities.plan_only()"""
    cfg = _w7_yaml()
    cfg["agents"][1]["role_type"] = "plan_only"
    cfg["agents"][1].pop("tools", None)
    p = _write_cfg(tmp_path, cfg)
    swarm = Swarm.from_yaml(p)
    p_agent = next(a for a in swarm.agents if a.id == "worker-1")
    assert p_agent.capabilities.can_execute_actions is False
    assert p_agent.capabilities.can_spawn_agents is False
    # plan_only 不在 DelegateMode partition 的 leads 也不在 workers
    from agent_swarm.core.protocols import DelegateMode

    leads, workers = DelegateMode._partition(swarm.agents)
    assert p_agent not in leads
    assert p_agent not in workers
