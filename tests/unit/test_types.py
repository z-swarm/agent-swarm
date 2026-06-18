"""单元测试：核心数据类型"""

from __future__ import annotations

from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    LLMResponse,
    Task,
    ToolCall,
    Turn,
)


def test_agent_capabilities_worker_preset() -> None:
    """worker 预设授予指定工具，且默认可执行"""
    caps = AgentCapabilities.worker({"read_file"})
    assert caps.allowed_tools == {"read_file"}
    assert caps.can_execute_actions is True
    assert caps.can_spawn_agents is False
    assert caps.can_assign_tasks is False


def test_agent_capabilities_worker_isolates_tools() -> None:
    """传入的 tools 集合应被复制，避免外部修改污染"""
    src = {"a", "b"}
    caps = AgentCapabilities.worker(src)
    src.add("c")
    assert "c" not in caps.allowed_tools


def test_agent_capabilities_lead_preset() -> None:
    """lead 预设：只编排不执行，含 spawn/shutdown/assign 权限（DESIGN §7.1）"""
    from agent_swarm.security.policy import ToolRisk

    caps = AgentCapabilities.lead()
    assert caps.can_execute_actions is False
    assert caps.can_spawn_agents is True
    assert caps.can_shutdown_agents is True
    assert caps.can_assign_tasks is True
    # lead 工具白名单必须含编排工具
    assert "send_message" in caps.allowed_tools
    assert "review_plan" in caps.allowed_tools
    assert "update_task" in caps.allowed_tools
    # lead 不能直接读文件/跑命令
    assert "read_file" not in caps.allowed_tools
    assert "run_command" not in caps.allowed_tools
    # lead 风险上限是 LOW
    assert caps.max_tool_risk == ToolRisk.LOW


def test_agent_capabilities_plan_only_preset() -> None:
    """plan_only 预设：只读工具，不能 spawn/assign/execute（DESIGN §7.1）"""
    from agent_swarm.security.policy import ToolRisk

    caps = AgentCapabilities.plan_only()
    assert caps.can_execute_actions is False
    assert caps.can_spawn_agents is False
    assert caps.can_shutdown_agents is False
    assert caps.can_assign_tasks is False
    # 只读 + 通信
    assert caps.allowed_tools == {"read_file", "search_code", "send_message"}
    # 风险上限 LOW
    assert caps.max_tool_risk == ToolRisk.LOW


def test_agent_capabilities_presets_isolate_tools() -> None:
    """lead/plan_only 预设的 allowed_tools 必须是新集合（不共享引用）"""
    a = AgentCapabilities.lead()
    b = AgentCapabilities.lead()
    a.allowed_tools.add("evil_tool")
    assert "evil_tool" not in b.allowed_tools


def test_agent_dataclass_fields() -> None:
    a = Agent(
        id="a-1",
        role="reviewer",
        persona="be helpful",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.worker({"read_file"}),
    )
    assert a.id == "a-1"
    assert a.tools == []
    assert a.max_iterations == 10


def test_task_default_status() -> None:
    t = Task(id="t-1", title="x", description="y")
    assert t.status == "pending"
    assert t.assigned_to is None


def test_turn_with_tool_calls() -> None:
    tc = ToolCall(id="c1", name="read_file", arguments={"path": "x"})
    turn = Turn(role="assistant", content="", tool_calls=[tc])
    assert turn.tool_calls[0].name == "read_file"
    assert turn.role == "assistant"


def test_llm_response_minimal() -> None:
    r = LLMResponse(
        content="hi",
        tool_calls=[],
        finish_reason="stop",
        tokens_prompt=10,
        tokens_completion=5,
        model="gpt-4o-mini",
    )
    assert r.tokens_prompt + r.tokens_completion == 15
