"""
@module agent_swarm.core.agent_runner
@brief  Agent 主循环——W1 最小实现

DESIGN.md §7.1 完整循环 observe → think → act → reflect
W1 简化:
  - observe = 读取当前任务（W1 单任务，直接传入）
  - think   = LLM 调用 + 工具 schema
  - act     = 执行工具调用
  - reflect = 检查 finish_reason，决定是否继续
  W1 暂不接 ObservabilityBus（W3 上线）；用 logger 占位
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from agent_swarm.core.types import (
    Agent,
    LLMResponse,
    Task,
    Tool,
    Turn,
)
from agent_swarm.providers.base import LLMProvider

log = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    """单任务运行结果"""

    task: Task
    history: list[Turn]
    iterations: int
    tokens_total: int
    final_text: str
    finish_reason: str  # 终止原因："stop" / "max_iterations" / "error"


class AgentRunner:
    """
    Agent 行为驱动器

    @note 保持 Agent 数据类纯净（types.py 中的 Agent）；
          AgentRunner 持有运行时依赖（provider / tools / logger）
    """

    def __init__(
        self,
        agent: Agent,
        provider: LLMProvider,
        tools: dict[str, Tool],
    ) -> None:
        self.agent = agent
        self.provider = provider
        # 仅保留 capabilities.allowed_tools 中允许的工具（防越权）
        allowed = set(agent.capabilities.allowed_tools)
        self.tools: dict[str, Tool] = {
            name: t for name, t in tools.items() if name in allowed
        }
        # 缓存工具 schema——每次 LLM 调用都要传
        self._tool_schemas = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self.tools.values()
        ]

    async def run(self, task: Task) -> AgentRunResult:
        """
        @brief 执行单个任务，返回结果

        循环：think → 若有工具调用则 act → reflect → 重新 think
              直至 finish_reason == "stop" 或达 max_iterations
        """
        # B1 修复：边界保护——max_iterations <= 0 直接拒绝，避免 for 循环
        # 不执行导致 iteration 变量未定义的 NameError
        if self.agent.max_iterations <= 0:
            raise ValueError(
                f"agent {self.agent.id}: max_iterations must be >= 1, "
                f"got {self.agent.max_iterations}"
            )

        log.info("agent=%s starting task=%s: %s", self.agent.id, task.id, task.title)
        task.status = "in_progress"
        task.assigned_to = self.agent.id

        history: list[Turn] = self._build_initial_history(task)
        tokens_total = 0
        last_text = ""
        finish: str = "stop"
        iteration = 0  # 显式初始化——保证 return 时一定有定义

        for iteration in range(1, self.agent.max_iterations + 1):
            log.debug("agent=%s iter=%d/%d", self.agent.id, iteration, self.agent.max_iterations)

            # think: LLM 调用
            try:
                resp = await self._think(history)
            except Exception as exc:  # noqa: BLE001 - W1 顶层兜底
                log.exception("agent=%s LLM error: %s", self.agent.id, exc)
                task.status = "failed"
                task.error = f"LLM error: {exc}"
                finish = "error"
                break

            tokens_total += resp.tokens_prompt + resp.tokens_completion
            last_text = resp.content

            # 把 assistant 回复入历史
            history.append(
                Turn(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    timestamp=time.time(),
                )
            )

            # reflect: 没有工具调用 → 终止
            if resp.finish_reason == "stop" or not resp.tool_calls:
                finish = "stop"
                task.status = "completed"
                task.result = resp.content
                break

            if resp.finish_reason == "length":
                # 超 max_tokens——把当前结果作为最终输出，不再继续
                finish = "length"
                task.status = "completed"
                task.result = resp.content
                break

            # act: 执行所有工具调用
            for tc in resp.tool_calls:
                tool_result = await self._act(tc.name, tc.arguments)
                history.append(
                    Turn(
                        role="tool",
                        content=tool_result,
                        tool_call_id=tc.id,
                        timestamp=time.time(),
                    )
                )
        else:
            # 达到 max_iterations 仍未停止
            log.warning(
                "agent=%s reached max_iterations=%d without stop",
                self.agent.id,
                self.agent.max_iterations,
            )
            finish = "max_iterations"
            task.status = "completed"  # 视为已完成（带最近一次回复）
            task.result = last_text

        return AgentRunResult(
            task=task,
            history=history,
            iterations=iteration,  # B1 修复：iteration 已在循环前显式初始化为 0
            tokens_total=tokens_total,
            final_text=last_text,
            finish_reason=finish,
        )

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _build_initial_history(self, task: Task) -> list[Turn]:
        """构造起始对话——system prompt + 任务描述"""
        sys_prompt = (
            f"You are {self.agent.role}. "
            f"{self.agent.persona}\n\n"
            "Use the provided tools to gather information when needed. "
            "When you have completed the task, provide your final answer "
            "without calling any more tools."
        )
        user_prompt = (
            f"Task: {task.title}\n\n"
            f"Description:\n{task.description}\n\n"
            "Please complete this task."
        )
        return [
            Turn(role="system", content=sys_prompt, timestamp=time.time()),
            Turn(role="user", content=user_prompt, timestamp=time.time()),
        ]

    async def _think(self, history: list[Turn]) -> LLMResponse:
        """LLM 调用"""
        return await self.provider.chat(
            messages=history,
            tools=self._tool_schemas if self._tool_schemas else None,
            model=self.agent.model,
        )

    async def _act(self, tool_name: str, arguments: dict) -> str:
        """执行单次工具调用"""
        tool = self.tools.get(tool_name)
        if tool is None:
            # LLM 调了不存在/未授权的工具——返回错误供其修正
            return f"[error] tool {tool_name!r} not available to this agent"
        try:
            return await tool.invoke(arguments)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed: %s", tool_name, exc)
            return f"[error] tool {tool_name!r} raised: {exc}"
