"""
@module agent_swarm.core.types
@brief  W1 核心数据类型——按 DESIGN.md §A 附录的子集落地

W1 范围：Agent、Task、Turn、ToolCall、Tool、AgentCapabilities、LLMResponse
       后续 Weekly Slice 扩展 Message / SessionEvent / Verdict 等
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# 工具相关（最简版本，W5 才接 SecurityPolicy）
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """LLM 请求的一次工具调用——对应 DESIGN.md §A.2"""

    id: str
    name: str
    arguments: dict[str, Any]


class Tool(Protocol):
    """
    工具协议——任何拥有 name / description / parameters / invoke 的对象都可作为工具

    W1 仅用 read_file；后续扩展 write_file / run_command / MCPToolAdapter
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """执行工具，返回字符串结果（用于注入回 LLM）"""
        ...


# ---------------------------------------------------------------------------
# Agent 与能力
# ---------------------------------------------------------------------------


@dataclass
class AgentCapabilities:
    """
    能力清单——单一权威来源（DESIGN.md §7.1）

    W1 仅使用 worker 预设；lead/plan_only 留待 Phase 2
    """

    allowed_tools: set[str] = field(default_factory=set)
    can_spawn_agents: bool = False
    can_assign_tasks: bool = False
    can_execute_actions: bool = True
    max_tokens_per_task: int = 100_000

    @classmethod
    def worker(cls, tools: set[str]) -> AgentCapabilities:
        """预设：执行者（W1 唯一用到的预设）"""
        return cls(allowed_tools=set(tools), can_execute_actions=True)


@dataclass
class Turn:
    """对话历史的一轮——DESIGN.md §A.4"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # role=tool 时必填
    timestamp: float = 0.0


@dataclass
class Agent:
    """
    Agent 数据载体——运行时由 AgentRunner 驱动

    @note W1 把 Agent 设计为纯数据 + 配置；行为由 AgentRunner 持有
          这样后续做持久化/序列化时不会被行为方法污染数据结构
    """

    id: str
    role: str
    persona: str
    model: str
    provider: str  # "openai" / "anthropic"
    capabilities: AgentCapabilities
    tools: list[str] = field(default_factory=list)  # 工具 id 列表
    max_iterations: int = 10  # 单任务最多 OTAR 轮次（防死循环）


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """
    任务——DESIGN.md §6.4.1

    W2: 引入 version (CAS 乐观锁) + depends_on 依赖链
    """

    id: str
    title: str
    description: str
    status: Literal["pending", "blocked", "in_progress", "completed", "failed"] = "pending"
    assigned_to: str | None = None
    assigned_skill: str | None = None  # W4 启用——按技能匹配 agent
    depends_on: list[str] = field(default_factory=list)  # 依赖任务 id 列表
    result: Any | None = None
    error: str | None = None
    version: int = 0  # CAS 版本号——每次状态变更 +1
    created_at: float = 0.0
    updated_at: float = 0.0


# ---------------------------------------------------------------------------
# 任务认领结果——DESIGN.md §6.4.3
# ---------------------------------------------------------------------------


@dataclass
class ClaimResult:
    """
    任务认领/状态更新的结果

    用 reason 显式区分失败原因（避免 None 三义混淆）:
      - task_not_found
      - version_mismatch (CAS 冲突——其他 agent 抢先更新)
      - already_claimed
      - dependency_blocked
    """

    success: bool
    task: Task | None = None
    reason: Literal[
        "ok",
        "task_not_found",
        "version_mismatch",
        "already_claimed",
        "dependency_blocked",
    ] = "ok"


# ---------------------------------------------------------------------------
# LLM 响应
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """LLMProvider.chat() 返回值——DESIGN.md §A.2"""

    content: str
    tool_calls: list[ToolCall]
    finish_reason: Literal["stop", "tool_use", "length", "content_filter"]
    tokens_prompt: int
    tokens_completion: int
    model: str


# ---------------------------------------------------------------------------
# Mailbox 消息——DESIGN.md §6.5
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """
    Agent 间点对点消息

    @note W2 内存实现；W3 起持久化（与 SessionEvent 共用 SQLite store）
    """

    id: str
    from_agent: str
    to_agent: str | None  # None = broadcast（W2 暂不实现广播）
    target_type: Literal["internal", "external"]  # internal=agent间; external=对外
    msg_type: Literal["question", "challenge", "reply", "notify", "delegate"]
    content: str
    refs: list[str] = field(default_factory=list)  # 引用的其他消息/任务 id
    reply_to: str | None = None  # 父消息 id
    timestamp: float = 0.0
    read: bool = False
