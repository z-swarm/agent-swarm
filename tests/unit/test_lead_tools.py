"""单元测试：Lead 工具集（W7-5）"""

from __future__ import annotations

import pytest

from agent_swarm.core.types import Agent, AgentCapabilities, Task
from agent_swarm.tools.builtin.lead import (
    AssignTaskTool,
    MockLeadContext,
    ReviewPlanTool,
    ShutdownAgentTool,
    SpawnAgentTool,
    UpdateTaskTool,
    build_lead_tools,
)

# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def _lead() -> Agent:
    return Agent(
        id="lead-1",
        role="lead",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.lead(),
    )


def _worker() -> Agent:
    return Agent(
        id="worker-1",
        role="worker",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.worker({"read_file"}),
    )


def _ctx_with(lead: Agent | None = None, worker: Agent | None = None) -> MockLeadContext:
    """构造含 lead + worker + 一个 pending task 的测试上下文"""
    ctx = MockLeadContext()
    if lead is not None:
        ctx.agents[lead.id] = lead
    if worker is not None:
        ctx.agents[worker.id] = worker
    ctx.tasks["t-1"] = Task(id="t-1", title="noop", description="x")
    return ctx


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_agent_creates_and_registers() -> None:
    """lead 调用 spawn_agent → 新 agent 出现在 ctx"""
    ctx = _ctx_with(lead=_lead())
    tool = SpawnAgentTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke(
        {
            "agent_id": "worker-x",
            "role": "reader",
            "model": "gpt-4o-mini",
            "provider": "openai",
            "tools": ["read_file"],
        }
    )
    assert "spawned" in out
    assert ctx.get_agent("worker-x") is not None
    assert ctx.get_agent("worker-x").role == "reader"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_spawn_agent_rejects_duplicate_id() -> None:
    """重复 id 应被拒绝，避免覆盖现有 agent"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = SpawnAgentTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke(
        {
            "agent_id": "worker-1",  # 已存在
            "role": "dup",
            "model": "gpt-4o-mini",
            "provider": "openai",
        }
    )
    assert "[error]" in out
    assert "already exists" in out


@pytest.mark.asyncio
async def test_spawn_agent_rejects_worker_caller() -> None:
    """worker 没有 can_spawn_agents → 拒绝"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = SpawnAgentTool(caller_agent_id="worker-1", ctx=ctx)  # caller 是 worker
    out = await tool.invoke(
        {
            "agent_id": "worker-y",
            "role": "x",
            "model": "gpt-4o-mini",
            "provider": "openai",
        }
    )
    assert "[error]" in out
    assert "can_spawn_agents" in out
    assert ctx.get_agent("worker-y") is None


@pytest.mark.asyncio
async def test_spawn_agent_rejects_missing_caller() -> None:
    """caller id 不在 ctx → 拒绝（防 lead 被注销后的 stale tool）"""
    ctx = _ctx_with(worker=_worker())  # 无 lead
    tool = SpawnAgentTool(caller_agent_id="ghost-lead", ctx=ctx)
    out = await tool.invoke(
        {
            "agent_id": "worker-y",
            "role": "x",
            "model": "gpt-4o-mini",
            "provider": "openai",
        }
    )
    assert "[error]" in out
    assert "ghost-lead" in out


# ---------------------------------------------------------------------------
# shutdown_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_agent_removes() -> None:
    """lead 注销动态 spawn 的 worker → ctx 里没了"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = ShutdownAgentTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"agent_id": "worker-1"})
    assert "shutdown" in out
    assert ctx.get_agent("worker-1") is None


@pytest.mark.asyncio
async def test_shutdown_agent_prevents_self_shutdown() -> None:
    """lead 不能 shutdown 自己（避免误操作关掉编排者）"""
    ctx = _ctx_with(lead=_lead())
    tool = ShutdownAgentTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"agent_id": "lead-1"})
    assert "[error]" in out
    assert "cannot shutdown self" in out
    assert ctx.get_agent("lead-1") is not None  # 仍在


@pytest.mark.asyncio
async def test_shutdown_agent_rejects_worker_caller() -> None:
    """worker 没有 can_shutdown_agents → 拒绝"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = ShutdownAgentTool(caller_agent_id="worker-1", ctx=ctx)
    out = await tool.invoke({"agent_id": "worker-1"})  # 试图自杀
    assert "[error]" in out
    assert "can_shutdown_agents" in out


# ---------------------------------------------------------------------------
# assign_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_task_to_worker() -> None:
    """lead 把 task 派给 worker → task.assigned_to + status=in_progress"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = AssignTaskTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"task_id": "t-1", "agent_id": "worker-1"})
    assert "assigned" in out
    assert ctx.tasks["t-1"].assigned_to == "worker-1"
    assert ctx.tasks["t-1"].status == "in_progress"


@pytest.mark.asyncio
async def test_assign_task_rejects_plan_only_target() -> None:
    """不能把 task 派给 plan_only（不能 execute）"""
    plan = Agent(
        id="plan-1",
        role="planner",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.plan_only(),
    )
    ctx = _ctx_with(lead=_lead())
    ctx.agents["plan-1"] = plan
    tool = AssignTaskTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"task_id": "t-1", "agent_id": "plan-1"})
    assert "[error]" in out
    assert "cannot execute" in out


@pytest.mark.asyncio
async def test_assign_task_rejects_worker_caller() -> None:
    """worker 没有 can_assign_tasks → 拒绝"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = AssignTaskTool(caller_agent_id="worker-1", ctx=ctx)
    out = await tool.invoke({"task_id": "t-1", "agent_id": "worker-1"})
    assert "[error]" in out
    assert "can_assign_tasks" in out


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_task_status() -> None:
    """lead 更新 task 状态 → ctx 里改动了"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = UpdateTaskTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"task_id": "t-1", "status": "completed"})
    assert "updated" in out
    assert ctx.tasks["t-1"].status == "completed"


@pytest.mark.asyncio
async def test_update_task_rejects_invalid_status() -> None:
    """status 不在白名单 → 拒绝"""
    ctx = _ctx_with(lead=_lead())
    tool = UpdateTaskTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"task_id": "t-1", "status": "exploded"})
    assert "[error]" in out


@pytest.mark.asyncio
async def test_update_task_swarm_api_rejects_invalid_status(tmp_path) -> None:
    """P3-3.8a (REVIEW-2026-06-19 §3.8)：Swarm.update_task_status 显式枚举校验

    之前用 type: ignore[assignment] 跳过 Literal 检查；现在用 frozenset
    显式校验非法 status 抛 ValueError。
    """
    import yaml

    from agent_swarm.core.swarm import Swarm

    cfg = {
        "name": "enum-test",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": [],
            }
        ],
        "tasks": [{"id": "t-1", "title": "t", "description": "d", "assigned_to": "a"}],
    }
    cfg_path = tmp_path / "x.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    swarm = Swarm.from_yaml(cfg_path)

    # 1) 合法 status → True
    assert swarm.update_task_status("t-1", "completed") is True
    assert swarm.tasks[0].status == "completed"

    # 2) 非法 status → ValueError
    import pytest

    with pytest.raises(ValueError, match="invalid status"):
        swarm.update_task_status("t-1", "exploded")
    with pytest.raises(ValueError, match="invalid status"):
        swarm.update_task_status("t-1", "")

    # 3) task_id 不存在 → False（不抛）
    assert swarm.update_task_status("ghost", "pending") is False


# ---------------------------------------------------------------------------
# review_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_plan_returns_feedback() -> None:
    """review_plan 接受 plan 文本并返回带 caller id 的反馈"""
    ctx = _ctx_with(lead=_lead())
    tool = ReviewPlanTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"plan": "1. read file\n2. summarize", "feedback": "looks good"})
    assert "lead-1" in out
    assert "plan len=" in out
    assert "looks good" in out


@pytest.mark.asyncio
async def test_review_plan_allows_worker_too() -> None:
    """review_plan _REQUIRED_CAPS 为空——任何角色都能调"""
    ctx = _ctx_with(lead=_lead(), worker=_worker())
    tool = ReviewPlanTool(caller_agent_id="worker-1", ctx=ctx)
    out = await tool.invoke({"plan": "x"})
    assert "worker-1" in out


@pytest.mark.asyncio
async def test_review_plan_rejects_empty_plan() -> None:
    ctx = _ctx_with(lead=_lead())
    tool = ReviewPlanTool(caller_agent_id="lead-1", ctx=ctx)
    out = await tool.invoke({"plan": "  "})
    assert "[error]" in out


# ---------------------------------------------------------------------------
# build_lead_tools
# ---------------------------------------------------------------------------


def test_build_lead_tools_returns_five_tools() -> None:
    """build_lead_tools 一次返回 5 个工具实例"""
    ctx = _ctx_with(lead=_lead())
    tools = build_lead_tools("lead-1", ctx)
    assert len(tools) == 5
    names = {t.name for t in tools}
    assert names == {
        "spawn_agent",
        "shutdown_agent",
        "assign_task",
        "update_task",
        "review_plan",
    }
