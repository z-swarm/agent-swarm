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
