"""
@module agent_swarm.core.agent_runner
@brief  Agent 主循环——W2 扩展为多任务自认领 + Mailbox 感知

W1 → W2 演进:
  - W1: AgentRunner.run(task) 跑单个任务
  - W2: AgentRunner.run_loop(task_queue, mailbox) 持续抢任务 + 处理消息
        run(task) 仍保留——单任务模式（测试与回退）

设计要点:
  - observe 阶段把 mailbox 中的未读消息渲染到对话历史的 user turn
  - 每完成一个任务，CAS complete，再回头抢
  - 抢不到 + mailbox 空 → wait_for_message(timeout) 避免忙等
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field

from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import (
    Agent,
    LLMResponse,
    Message,
    Task,
    Tool,
    Turn,
)
from agent_swarm.providers.base import LLMProvider
from agent_swarm.skills import SkillRegistry, compose_system_prompt

log = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    """单任务运行结果"""

    task: Task
    history: list[Turn]
    iterations: int
    tokens_total: int
    final_text: str
    finish_reason: str  # "stop" / "max_iterations" / "error" / "length"


@dataclass
class AgentLoopStats:
    """run_loop 的统计——一个 agent 在 swarm 中跑完后产出"""

    agent_id: str
    tasks_completed: list[str] = field(default_factory=list)
    tasks_failed: list[str] = field(default_factory=list)
    cas_conflicts: int = 0  # 抢任务时遇到 version_mismatch 的次数
    messages_consumed: int = 0
    tokens_total: int = 0
    task_results: list[AgentRunResult] = field(default_factory=list)
    # 内部状态——_loop_once 与 run_loop 之间传递"本轮是否 idle"
    # repr=False 避免污染日志/打印；compare=False 避免影响 == 判断
    last_round_was_idle: bool = field(default=False, repr=False, compare=False)


class AgentRunner:
    """
    Agent 行为驱动器

    @note 保持 Agent 数据类纯净（types.py 中的 Agent）；
          AgentRunner 持有运行时依赖（provider / tools / logger）

    W2 修订:
      - tools 字典里的 send_message 等 per-agent 工具由调用方传入
      - 新增 run_loop()——多 agent swarm 模式
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

    # ==================================================================
    # 单任务模式（W1 接口，保持向后兼容；测试与简单 swarm 仍可用）
    # ==================================================================
    async def run(
        self,
        task: Task,
        inbox_messages: list[Message] | None = None,
    ) -> AgentRunResult:
        """
        @brief 执行单个任务

        @param task           待执行任务（**会被深拷贝**，run() 不修改入参对象）
        @param inbox_messages 启动时已有的 mailbox 消息——会渲染到首轮 user prompt

        W2-B2 修复：操作 task 副本，避免污染 TaskQueue 内部对象。
        所有状态变更必须通过 TaskQueue.complete/fail 走 CAS（由 run_loop 负责）。
        """
        if self.agent.max_iterations <= 0:
            raise ValueError(
                f"agent {self.agent.id}: max_iterations must be >= 1, "
                f"got {self.agent.max_iterations}"
            )

        # 深拷贝 task——后续 run() 内的所有修改只动副本
        task = copy.deepcopy(task)

        log.info("agent=%s starting task=%s: %s",
                 self.agent.id, task.id, task.title)
        task.status = "in_progress"
        task.assigned_to = self.agent.id

        history: list[Turn] = self._build_initial_history(task, inbox_messages)
        tokens_total = 0
        last_text = ""
        finish: str = "stop"
        iteration = 0

        for iteration in range(1, self.agent.max_iterations + 1):
            log.debug("agent=%s iter=%d/%d",
                      self.agent.id, iteration, self.agent.max_iterations)

            try:
                resp = await self._think(history)
            except Exception as exc:  # noqa: BLE001
                log.exception("agent=%s LLM error: %s", self.agent.id, exc)
                task.status = "failed"
                task.error = f"LLM error: {exc}"
                finish = "error"
                break

            tokens_total += resp.tokens_prompt + resp.tokens_completion
            last_text = resp.content

            history.append(
                Turn(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    timestamp=time.time(),
                )
            )

            if resp.finish_reason == "stop" or not resp.tool_calls:
                finish = "stop"
                task.status = "completed"
                task.result = resp.content
                break

            if resp.finish_reason == "length":
                finish = "length"
                task.status = "completed"
                task.result = resp.content
                break

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
            log.warning(
                "agent=%s reached max_iterations=%d without stop",
                self.agent.id, self.agent.max_iterations,
            )
            finish = "max_iterations"
            task.status = "completed"
            task.result = last_text

        return AgentRunResult(
            task=task,
            history=history,
            iterations=iteration,
            tokens_total=tokens_total,
            final_text=last_text,
            finish_reason=finish,
        )

    # ==================================================================
    # 多 agent swarm 模式（W2 新增）
    # ==================================================================
    async def run_loop(
        self,
        task_queue: TaskQueue,
        mailbox: Mailbox,
        idle_timeout: float = 1.0,
        max_idle_polls: int = 3,
    ) -> AgentLoopStats:
        """
        持续抢任务 + 处理消息，直到无任务可抢且连续 N 次 idle

        @param idle_timeout    每次 wait_for_message 的超时
        @param max_idle_polls  连续无任务+无消息几次后退出

        终止条件:
          - 连续 max_idle_polls 次 idle 检查都没有任务/消息
          - 调用方 cancel 此 coroutine（CancelledError 触发后仍 return 已积累 stats）
        """
        stats = AgentLoopStats(agent_id=self.agent.id)
        idle_rounds = 0
        log.info("agent=%s loop start", self.agent.id)

        try:
            while idle_rounds < max_idle_polls:
                await self._loop_once(task_queue, mailbox, stats, idle_timeout)
                if stats.last_round_was_idle:
                    idle_rounds += 1
                else:
                    idle_rounds = 0
        except asyncio.CancelledError:
            # 被 watcher 取消——返回已有 stats，不让异常传播
            log.info("agent=%s loop cancelled by orchestrator", self.agent.id)

        log.info(
            "agent=%s loop end: completed=%d failed=%d cas_conflicts=%d",
            self.agent.id, len(stats.tasks_completed),
            len(stats.tasks_failed), stats.cas_conflicts,
        )
        return stats

    async def _loop_once(
        self,
        task_queue: TaskQueue,
        mailbox: Mailbox,
        stats: AgentLoopStats,
        idle_timeout: float,
    ) -> None:
        """run_loop 的一次迭代——抽取出来便于阅读 + 单测"""
        # ① 拉取未读消息
        inbox = await mailbox.receive(self.agent.id, unread_only=True)
        stats.messages_consumed += len(inbox)
        if inbox:
            ids = [m.id for m in inbox]
            await mailbox.mark_read(self.agent.id, ids)

        # ② 尝试抢任务
        claimable = await task_queue.list_claimable(agent_id=self.agent.id)
        claimed_task: Task | None = None
        for cand in claimable:
            res = await task_queue.claim(
                cand.id, self.agent.id, expected_version=cand.version
            )
            if res.success and res.task:
                claimed_task = res.task
                break
            if res.reason == "version_mismatch":
                stats.cas_conflicts += 1

        # ③ 路由
        if claimed_task is not None:
            stats.last_round_was_idle = False
            run_res = await self.run(claimed_task, inbox_messages=inbox)
            stats.task_results.append(run_res)
            stats.tokens_total += run_res.tokens_total

            # claim 成功后版本号已 +1（claim 内部递增）；run() 不改原对象（W2-B2）
            # 所以 expected_version = claimed_task.version（claim 返回值）
            version_after_run = claimed_task.version
            # run_res.task 是副本——通过它判断最终状态（W2-B2）
            final_task = run_res.task
            if final_task.status == "completed":
                cr = await task_queue.complete(
                    claimed_task.id, run_res.final_text,
                    expected_version=version_after_run,
                )
                if cr.success:
                    stats.tasks_completed.append(claimed_task.id)
                else:
                    log.warning("agent=%s complete cas failed: %s",
                                self.agent.id, cr.reason)
                    stats.tasks_failed.append(claimed_task.id)
            else:
                cr = await task_queue.fail(
                    claimed_task.id, final_task.error or "unknown",
                    expected_version=version_after_run,
                )
                if cr.success:
                    stats.tasks_failed.append(claimed_task.id)
        elif inbox:
            stats.last_round_was_idle = False
        else:
            got = await mailbox.wait_for_message(
                self.agent.id, timeout=idle_timeout
            )
            stats.last_round_was_idle = not got

    # ==================================================================
    # 内部步骤
    # ==================================================================
    def _build_initial_history(
        self,
        task: Task,
        inbox_messages: list[Message] | None = None,
    ) -> list[Turn]:
        """
        构造起始对话——system prompt（含 skill extension）+ 任务描述 + 初始消息

        W4 改造：通过 SkillRegistry 加载 agent.skills 列表，把每个 skill 的
        prompt extension 拼入 system message（compose_system_prompt 负责）
        """
        # 解析 skills——未注册的 skill 仅记 warning，不阻断（向前兼容）
        resolved_skills = []
        for sid in self.agent.skills:
            s = SkillRegistry.get(sid)
            if s is None:
                log.warning(
                    "agent=%s skill %r not registered—prompt will skip it",
                    self.agent.id, sid,
                )
                continue
            resolved_skills.append(s)

        sys_prompt = compose_system_prompt(
            base_persona=self.agent.persona,
            role=self.agent.role,
            agent_id=self.agent.id,
            skills=resolved_skills,
        )

        user_lines = [
            f"Task: {task.title}",
            "",
            f"Description:\n{task.description}",
        ]

        if inbox_messages:
            user_lines.append("")
            user_lines.append("Pending messages from other agents:")
            for m in inbox_messages:
                user_lines.append(
                    f"  [{m.msg_type}] from {m.from_agent}: {m.content}"
                )

        user_lines.append("")
        user_lines.append("Please complete this task.")

        return [
            Turn(role="system", content=sys_prompt, timestamp=time.time()),
            Turn(role="user", content="\n".join(user_lines), timestamp=time.time()),
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
            return f"[error] tool {tool_name!r} not available to this agent"
        try:
            return await tool.invoke(arguments)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed: %s", tool_name, exc)
            return f"[error] tool {tool_name!r} raised: {exc}"
