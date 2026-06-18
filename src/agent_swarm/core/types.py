"""
@module agent_swarm.core.types
@brief  W1 核心数据类型——按 DESIGN.md §A 附录的子集落地

W1 范围：Agent、Task、Turn、ToolCall、Tool、AgentCapabilities、LLMResponse
W2+ 扩展：Message / SessionEvent / Verdict 等
W7 扩展：AgentCapabilities.lead() / .plan_only() 预设（DESIGN §7.1）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

# security 模块定义 ToolRisk——types.py 反向引用
# 安全模块不依赖 core.types，依赖方向 core→security 不会形成循环
if TYPE_CHECKING:
    from agent_swarm.security.policy import ToolRisk

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

    W1/W2/W3/W4/W5/W6 仅使用 worker 预设。
    W7 落地 lead / plan_only 预设（Phase 2 Delegate Mode 基础）。
    """

    allowed_tools: set[str] = field(default_factory=set)
    can_spawn_agents: bool = False       # 编排能力：等价于旧 mode=delegate
    can_shutdown_agents: bool = False    # W7 新增：lead 关闭临时 worker 用
    can_assign_tasks: bool = False       # 编排能力：派发任务给 worker
    can_execute_actions: bool = True     # False 等价于旧 mode=plan_only
    max_tokens_per_task: int = 100_000
    # max_tool_risk: 工具风险等级上限（DESIGN §7.1）——运行时类型是 ToolRisk
    # 此处仅声明为对象引用，实际类型检查在 SecurityPolicy.evaluate() 内
    # 用 TYPE_CHECKING 避免 types.py 运行时 import security 模块
    max_tool_risk: Any = None  # ToolRisk.MEDIUM 推荐默认

    @classmethod
    def worker(cls, tools: set[str], max_risk: Any = None) -> "AgentCapabilities":
        """预设：执行者——只执行不编排

        @param tools 允许使用的工具 id 集合（被复制，避免外部污染）
        @param max_risk 工具风险等级上限；None 表示 MEDIUM
        """
        if max_risk is None:
            from agent_swarm.security.policy import ToolRisk

            max_risk = ToolRisk.MEDIUM
        return cls(
            allowed_tools=set(tools),
            can_execute_actions=True,
            max_tool_risk=max_risk,
        )

    @classmethod
    def lead(cls) -> "AgentCapabilities":
        """预设：协调者——只编排不执行（DESIGN §7.1）

        允许工具：send_message / review_plan / update_task
        禁止 can_execute_actions（不能直接 read_file / run_command 等）
        """
        from agent_swarm.security.policy import ToolRisk

        return cls(
            allowed_tools={"send_message", "review_plan", "update_task", "spawn_agent", "shutdown_agent", "assign_task"},
            can_spawn_agents=True,
            can_shutdown_agents=True,
            can_assign_tasks=True,
            can_execute_actions=False,
            max_tool_risk=ToolRisk.LOW,
        )

    @classmethod
    def plan_only(cls) -> "AgentCapabilities":
        """预设：只规划不动手（DESIGN §7.1）——只读工具集

        允许工具：read_file / search_code / send_message
        不能 spawn / assign / execute
        """
        from agent_swarm.security.policy import ToolRisk

        return cls(
            allowed_tools={"read_file", "search_code", "send_message"},
            can_execute_actions=False,
            max_tool_risk=ToolRisk.LOW,
        )


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
    skills: list[str] = field(default_factory=list)  # W4: skill id 列表
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
      - dependency_blocked (status=blocked,依赖未满足)
      - task_terminal  (status=completed/failed,终态)
    """

    success: bool
    task: Task | None = None
    reason: Literal[
        "ok",
        "task_not_found",
        "version_mismatch",
        "already_claimed",
        "dependency_blocked",
        "task_terminal",
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


# ---------------------------------------------------------------------------
# Session 事件——DESIGN.md §5.4 / §6.7（W3 引入）
# ---------------------------------------------------------------------------


@dataclass
class SessionEvent:
    """
    统一事件结构——事件名采用 §5.4 字符串规范 {layer}.{module}.{action}

    一套事件流同时支撑：
      - JSON 日志（JsonLogSink）
      - 持久化 + 恢复（SqliteEventSink → SessionManager.restore_session）
      - 实时推送（WebSocketSink，W6）
      - Prometheus 指标（PrometheusSink，Phase 3）
    """

    event_name: str  # 例如 "task.created" / "agent.loop.iteration_complete"
    session_id: str
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0  # 单调递增序号——同一 session 内严格有序（事件流回放靠这个）
    request_id: str | None = None  # 关联 SecurityContext.request_id（W5 启用）



# ---------------------------------------------------------------------------
# Adversarial Verify 数据结构——DESIGN §6.2.2 / W8-1
# ---------------------------------------------------------------------------


class Stance(Enum):
    """
    Agent 对单个假设的立场——DESIGN §6.2.2

    SUPPORT  支持（找到了证据）
    REFUTE  反驳（找到了反例）
    UNCERTAIN  不确定（证据不足）
    """

    SUPPORT = "support"
    REFUTE = "refute"
    UNCERTAIN = "uncertain"


@dataclass
class Judgement:
    """
    单个 agent 对单个假设在某一轮的判断——DESIGN §6.2.2

    @note 不进入 ConversationContext.history——通过 external_inputs 单独
          承载（DESIGN §6.6 "对抗式验证的隔离要求"）
    @note evidence 是引用列表（文件路径/日志片段/工具输出引用），非内联
    """

    agent_id: str
    hypothesis_id: str
    round_no: int
    stance: Stance
    confidence: float  # 0.0 ~ 1.0
    evidence: list[str] = field(default_factory=list)
    reasoning: str = ""


@dataclass
class HypothesisState:
    """
    假设在多轮对抗中的状态——DESIGN §6.2.2

    judgements_by_round 形如 {1: [Judgement, Judgement, ...], 2: [...]}，
    每轮每个 agent 一个 Judgement。
    """

    id: str
    statement: str
    eliminated: bool = False
    eliminated_at_round: int | None = None
    judgements_by_round: dict[int, list["Judgement"]] = field(default_factory=dict)

    def support_score(self, round_no: int) -> float:
        """
        加权支持度：support 加分，refute 扣分，按 confidence 加权——DESIGN §6.2.2

        @return 归一化到 [-1.0, 1.0]；该轮无 Judgement 时返回 0.0
        """
        js = self.judgements_by_round.get(round_no, [])
        if not js:
            return 0.0
        score = sum(
            j.confidence
            * (1 if j.stance == Stance.SUPPORT
               else -1 if j.stance == Stance.REFUTE
               else 0)
            for j in js
        )
        return score / len(js)


@dataclass
class Verdict:
    """
    对抗式验证的最终结论——DESIGN §6.2.2

    @note survivors 按最后一轮 support_score 降序排（DESIGN §6.2.5）
    @note convergence_reason 决定 root_cause 是否有效：
          仅当 survivors 恰有 1 个且 reason != "all_eliminated" 时 root_cause 有意义
    @note full_history 完整 Judgement 流，可 emit 到 ObservabilityBus
    """

    survivors: list[HypothesisState]
    eliminated: list[HypothesisState]
    rounds_used: int
    convergence_reason: Literal[
        "min_survivors_reached",
        "consensus_stable",
        "max_rounds_exhausted",
        "all_eliminated",
    ]
    root_cause: str | None = None
    confidence: float = 0.0
    full_history: list[Judgement] = field(default_factory=list)


# 导出——便于单测和外部 import
__all__ = [
    "Agent",
    "AgentCapabilities",
    "ClaimResult",
    "HypothesisState",
    "Judgement",
    "LLMResponse",
    "Message",
    "SessionEvent",
    "Stance",
    "Task",
    "ToolCall",
    "Turn",
    "Verdict",
]
