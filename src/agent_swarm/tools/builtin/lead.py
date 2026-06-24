"""
@module agent_swarm.tools.builtin.lead
@brief  W7 Lead 工具集——Lead Agent 编排动作（spawn / shutdown / assign / update / review）

DESIGN §6.1 Delegate Mode + §7.1 AgentCapabilities：
  - spawn_agent    需要 can_spawn_agents=True
  - shutdown_agent 需要 can_shutdown_agents=True
  - assign_task    需要 can_assign_tasks=True
  - update_task    需要 can_assign_tasks=True
  - review_plan    仅 lead 角色（无副作用，仅生成审批/反馈字符串）

W7 范围（最小骨架）：
  - 工具语义 + 权限校验（caller capabilities 显式校验，不依赖 AgentRunner 隐式过滤）
  - LeadToolContext 协议：抽象掉对 Swarm 的直接依赖，便于单测用 mock
  - W8+ 接入：Swarm 自身实现 LeadToolContext 协议，构造 Runner 时把 caller_agent_id
    注入到 lead 工具的构造参数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    Task,
    Tool,
)

# ---------------------------------------------------------------------------
# 上下文协议——lead 工具不直接持有 Swarm 引用，便于单测
# ---------------------------------------------------------------------------


@runtime_checkable
class LeadToolContext(Protocol):
    """
    Lead 工具的运行环境协议——W7 最小集

    实现方通常是 Swarm 本身（W7-6 接入），单测可用 MockLeadContext。
    所有方法在 agent_id/task_id 不存在时应抛 KeyError（工具层捕获并返回 [error]）。
    """

    def add_agent(self, agent: Agent) -> None:
        """注册 agent 到 swarm（spawn_agent 用）"""
        ...

    def remove_agent(self, agent_id: str) -> bool:
        """注销 agent；返回是否真注销了一个 agent"""
        ...

    def get_agent(self, agent_id: str) -> Agent | None:
        """按 id 查 agent；不存在返回 None（不抛）"""
        ...

    def list_agents(self) -> list[Agent]:
        """列所有 agent（含 lead + worker）"""
        ...

    def assign_task_to(self, task_id: str, agent_id: str) -> bool:
        """
        把 task 派给 agent（assign_task 用）

        行为：设置 task.assigned_to = agent_id + status=in_progress；返回是否真派了
        """
        ...

    def update_task_status(self, task_id: str, status: str) -> bool:
        """更新 task.status；返回是否真改了"""
        ...


# ---------------------------------------------------------------------------
# 内部：统一的工具基类——共享 caller 校验
# ---------------------------------------------------------------------------


class _LeadToolBase:
    """
    Lead 工具基类——统一管理 caller 校验与错误返回风格

    子类必须：
      - 设置 class 属性 name / description / parameters
      - 设置 class 属性 _REQUIRED_CAPS：调用方必须具备的 capabilities 字段名集合
      - 实现 _do_invoke(arguments, caller, ctx) -> str
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    # 需要的 capabilities 字段名（任一满足即可）；空集合 = 无限制
    _REQUIRED_CAPS: set[str] = set()

    def __init__(self, caller_agent_id: str, ctx: LeadToolContext) -> None:
        """
        @param caller_agent_id 调用方 agent 的 id（构造时注入，不接受 arguments 传入）
        @param ctx            Lead 工具上下文——单测可传 MockLeadContext
        """
        self._caller_agent_id = caller_agent_id
        self._ctx = ctx

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """统一入口：caller 校验 + 委托 _do_invoke"""
        caller = self._ctx.get_agent(self._caller_agent_id)
        if caller is None:
            return f"[error] caller agent {self._caller_agent_id!r} not found in swarm"

        if self._REQUIRED_CAPS and not self._has_required_caps(caller.capabilities):
            needed = ", ".join(sorted(self._REQUIRED_CAPS))
            return (
                f"[error] {self.name} denied for agent {caller.id!r}: "
                f"missing one of capabilities: {needed}"
            )

        return self._do_invoke(arguments, caller, self._ctx)

    def _has_required_caps(self, caps: AgentCapabilities) -> bool:
        """子类用 OR 语义：caller 拥有 _REQUIRED_CAPS 中任一字段即通过"""
        cap_flags: dict[str, bool] = {
            "can_spawn_agents": caps.can_spawn_agents,
            "can_shutdown_agents": caps.can_shutdown_agents,
            "can_assign_tasks": caps.can_assign_tasks,
        }
        return any(cap_flags.get(c, False) for c in self._REQUIRED_CAPS)

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------


class SpawnAgentTool(_LeadToolBase):
    """Lead 工具：动态创建 worker agent 并注册到 swarm"""

    name = "spawn_agent"
    description = "Lead 工具：动态创建一个 worker agent 并注册到 swarm。返回新 agent 的 id。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "新 agent 的 id（必须唯一）",
            },
            "role": {
                "type": "string",
                "description": "新 agent 的角色标签",
            },
            "persona": {
                "type": "string",
                "description": "agent 的人设/系统提示",
            },
            "model": {
                "type": "string",
                "description": "LLM 模型名（如 gpt-4o-mini）",
            },
            "provider": {
                "type": "string",
                "description": "LLM provider（openai / anthropic）",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "新 agent 允许使用的工具 id 列表",
            },
        },
        "required": ["agent_id", "role", "model", "provider"],
    }
    _REQUIRED_CAPS = {"can_spawn_agents"}

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        agent_id = arguments.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return "[error] spawn_agent: 'agent_id' must be a non-empty string"

        # 唯一性检查
        if ctx.get_agent(agent_id) is not None:
            return f"[error] spawn_agent: agent_id {agent_id!r} already exists"

        # 构造新 agent——默认 worker capabilities（按 tools 子集）
        tools_arg = arguments.get("tools") or []
        if not isinstance(tools_arg, list) or not all(isinstance(t, str) for t in tools_arg):
            return "[error] spawn_agent: 'tools' must be a list of strings"

        new_agent = Agent(
            id=agent_id,
            role=str(arguments.get("role", "worker")),
            persona=str(arguments.get("persona", "")),
            model=str(arguments["model"]),
            provider=str(arguments["provider"]),
            capabilities=AgentCapabilities.worker(set(tools_arg)),
            tools=list(tools_arg),
        )
        ctx.add_agent(new_agent)
        return f"spawned agent {agent_id!r} (role={new_agent.role})"


# ---------------------------------------------------------------------------
# shutdown_agent
# ---------------------------------------------------------------------------


class ShutdownAgentTool(_LeadToolBase):
    """Lead 工具：从 swarm 注销 agent（通常是动态 spawn 的临时 worker）"""

    name = "shutdown_agent"
    description = "Lead 工具：从 swarm 注销一个 agent（一般是动态 spawn 的临时 worker）。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要注销的 agent id",
            },
        },
        "required": ["agent_id"],
    }
    _REQUIRED_CAPS = {"can_shutdown_agents"}

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        target_id = arguments.get("agent_id")
        if not isinstance(target_id, str) or not target_id.strip():
            return "[error] shutdown_agent: 'agent_id' must be a non-empty string"
        # 安全：lead 不能 shutdown 自己
        if target_id == caller.id:
            return f"[error] shutdown_agent: cannot shutdown self ({caller.id!r})"
        if ctx.remove_agent(target_id):
            return f"shutdown agent {target_id!r}"
        return f"[error] shutdown_agent: agent {target_id!r} not found"


# ---------------------------------------------------------------------------
# assign_task
# ---------------------------------------------------------------------------


class AssignTaskTool(_LeadToolBase):
    """Lead 工具：把一个 task 派给指定 agent"""

    name = "assign_task"
    description = "Lead 工具：把 task 派给指定 agent（worker）。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "要派发的 task id"},
            "agent_id": {"type": "string", "description": "接收任务的 agent id"},
        },
        "required": ["task_id", "agent_id"],
    }
    _REQUIRED_CAPS = {"can_assign_tasks"}

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        task_id = arguments.get("task_id")
        agent_id = arguments.get("agent_id")
        if not isinstance(task_id, str) or not isinstance(agent_id, str):
            return "[error] assign_task: 'task_id' and 'agent_id' must be strings"
        target = ctx.get_agent(agent_id)
        if target is None:
            return f"[error] assign_task: agent {agent_id!r} not found"
        if not target.capabilities.can_execute_actions:
            return (
                f"[error] assign_task: target {agent_id!r} cannot execute "
                f"(can_execute_actions=False; only lead/plan_only role?)"
            )
        if ctx.assign_task_to(task_id, agent_id):
            return f"assigned task {task_id!r} -> {agent_id!r}"
        return f"[error] assign_task: task {task_id!r} not found"


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


class UpdateTaskTool(_LeadToolBase):
    """Lead 工具：更新 task 状态（pending/in_progress/completed/failed）"""

    name = "update_task"
    description = "Lead 工具：更新 task 状态。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "目标 task id"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "failed"],
                "description": "新状态",
            },
        },
        "required": ["task_id", "status"],
    }
    _REQUIRED_CAPS = {"can_assign_tasks"}

    _ALLOWED_STATUSES = {"pending", "in_progress", "completed", "failed"}

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        task_id = arguments.get("task_id")
        status = arguments.get("status")
        if not isinstance(task_id, str):
            return "[error] update_task: 'task_id' must be a string"
        if status not in self._ALLOWED_STATUSES:
            return f"[error] update_task: 'status' must be one of {sorted(self._ALLOWED_STATUSES)}"
        if ctx.update_task_status(task_id, str(status)):
            return f"updated task {task_id!r} -> {status}"
        return f"[error] update_task: task {task_id!r} not found"


# ---------------------------------------------------------------------------
# review_plan
# ---------------------------------------------------------------------------


class ReviewPlanTool(_LeadToolBase):
    """Lead 工具：审查一个 plan 文本并产出反馈（无副作用，仅生成字符串）"""

    name = "review_plan"
    description = (
        "Lead 工具：审查一段 plan 文本，返回结构化反馈（approve / request_changes）。"
        "无副作用，不修改 swarm 状态。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "待审查的 plan 文本（worker 提交的执行计划）",
            },
            "feedback": {
                "type": "string",
                "description": "可选：审查者的具体反馈意见",
            },
        },
        "required": ["plan"],
    }
    # review_plan 任何 lead/worker 都可调（不需要 spawn/shutdown/assign 权限）
    _REQUIRED_CAPS: set[str] = set()

    def _do_invoke(self, arguments: dict[str, Any], caller: Agent, ctx: LeadToolContext) -> str:
        plan = arguments.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return "[error] review_plan: 'plan' must be a non-empty string"
        feedback = arguments.get("feedback") or ""
        # W7 骨架：仅透传 + 标记 lead 已审；W8+ 接入真 LLM-as-judge
        return f"reviewed by {caller.id}: plan len={len(plan)}, feedback={feedback!r}"


# ---------------------------------------------------------------------------
# 工厂：构造 Lead 工具集（按 caller 区分）
# ---------------------------------------------------------------------------


def build_lead_tools(caller_agent_id: str, ctx: LeadToolContext) -> list[Tool]:
    """
    为指定 caller 构造 lead 工具集

    返回的每个工具在 invoke() 时都会按 caller_agent_id 校验 capabilities。
    W7 一次返回全部 5 个工具；权限校验在 invoke() 内部按 _REQUIRED_CAPS 决策。
    """
    return [
        SpawnAgentTool(caller_agent_id, ctx),
        ShutdownAgentTool(caller_agent_id, ctx),
        AssignTaskTool(caller_agent_id, ctx),
        UpdateTaskTool(caller_agent_id, ctx),
        ReviewPlanTool(caller_agent_id, ctx),
    ]


# ---------------------------------------------------------------------------
# MockLeadContext——单测用
# ---------------------------------------------------------------------------


@dataclass
class MockLeadContext:
    """
    单测用 LeadToolContext——内存版

    add_agent 时如 id 已存在抛 ValueError；remove_agent / assign_task / update
    返回是否真改了。
    """

    agents: dict[str, Agent] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)

    def add_agent(self, agent: Agent) -> None:
        if agent.id in self.agents:
            raise ValueError(f"agent {agent.id!r} already exists")
        self.agents[agent.id] = agent

    def remove_agent(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def list_agents(self) -> list[Agent]:
        return list(self.agents.values())

    def assign_task_to(self, task_id: str, agent_id: str) -> bool:
        t = self.tasks.get(task_id)
        if t is None:
            return False
        t.assigned_to = agent_id
        t.status = "in_progress"
        return True

    def update_task_status(self, task_id: str, status: str) -> bool:
        t = self.tasks.get(task_id)
        if t is None:
            return False
        t.status = status  # type: ignore[assignment]
        return True
