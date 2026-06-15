"""
@module agent_swarm.core.swarm
@brief  Swarm 编排器（W2 多 agent 并发版本）

DESIGN.md §3.2 完整 API；W2 阶段:
  - Swarm.from_yaml() / from_dict()
  - run() → 多 agent 并发跑（asyncio.gather agent.run_loop）
  - TaskQueue 内存实现
  - Mailbox 内存实现

W1 → W2 演进:
  - W1: 取第一个 agent 串行跑所有任务
  - W2: 所有 agent 并发；任务通过 TaskQueue.claim 自分配
        agent 间通过 Mailbox.send_message 协作

W3 起 SQLite 持久化；handle_external_message / pause / resume 留待后续
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_swarm.core.agent_runner import AgentLoopStats, AgentRunner, AgentRunResult
from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Agent, AgentCapabilities, Task
from agent_swarm.providers import get_provider
from agent_swarm.tools import build_per_agent_tools, build_shared_tools

log = logging.getLogger(__name__)


@dataclass
class SwarmResult:
    """Swarm.run() 返回值——DESIGN.md §A.1"""

    name: str
    state: str  # "completed" / "failed"
    duration_seconds: float
    tasks_completed: int
    tasks_failed: int
    agent_results: list[AgentRunResult] = field(default_factory=list)
    agent_stats: list[AgentLoopStats] = field(default_factory=list)
    error: str | None = None


class Swarm:
    """W2 多 agent Swarm——TaskQueue 自分配 + Mailbox 协作"""

    def __init__(
        self,
        name: str,
        agents: list[Agent],
        tasks: list[Task],
        workspace: Path | str | None = None,
        provider_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if not agents:
            raise ValueError("Swarm needs at least one agent")
        if not tasks:
            raise ValueError("Swarm needs at least one task")
        self.name = name
        self.agents = agents
        self.tasks = tasks
        self.workspace = (
            Path(workspace).resolve() if workspace else Path.cwd().resolve()
        )
        self.provider_overrides = provider_overrides or {}

        # W2 共享基础设施
        self.task_queue = TaskQueue()
        self.mailbox = Mailbox()

    # ------------------------------------------------------------------
    # 加载入口
    # ------------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> Swarm:
        p = Path(path)
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            raise ValueError(
                f"YAML root must be a mapping, got {type(cfg).__name__}"
            )
        return cls.from_dict(cfg, base_dir=p.parent.resolve())

    @classmethod
    def from_dict(cls, cfg: dict[str, Any], base_dir: Path | None = None) -> Swarm:
        name = cfg.get("name", "unnamed-swarm")

        agents_cfg = cfg.get("agents") or []
        if not agents_cfg:
            raise ValueError("config missing 'agents'")
        agents = [_parse_agent(a) for a in agents_cfg]

        tasks_cfg = cfg.get("tasks") or []
        if not tasks_cfg:
            raise ValueError("config missing 'tasks'")
        # 让 task 可按 title 引用其他 task 作为依赖（用户友好）
        agent_ids = {a.id for a in agents}
        tasks = [_parse_task(t, idx=i, agent_ids=agent_ids) for i, t in enumerate(tasks_cfg)]
        # 二次扫描：把 depends_on 中的 title 解析为 task.id
        _resolve_task_dependencies(tasks)

        ws = cfg.get("workspace", base_dir)

        return cls(
            name=name,
            agents=agents,
            tasks=tasks,
            workspace=ws,
            provider_overrides=cfg.get("provider_overrides", {}),
        )

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    async def run(self) -> SwarmResult:
        """
        @brief 多 agent 并发执行——asyncio.gather 跑所有 agent.run_loop

        W2 流程:
          1. 把所有 task 注入 TaskQueue
          2. 为每个 agent 构造 AgentRunner（含 per-agent send_message 工具）
          3. asyncio.gather 跑所有 run_loop
          4. 所有 agent 退出后汇总
        """
        t0 = time.monotonic()
        log.info(
            "swarm=%s start (%d agent(s), %d task(s))",
            self.name, len(self.agents), len(self.tasks),
        )

        # 1) 任务入队
        await self.task_queue.add_many(self.tasks)

        # 2) 构造 runner——共享 read_file，per-agent send_message
        shared_tools = build_shared_tools(workspace=self.workspace)
        agent_ids = {a.id for a in self.agents}
        runners: list[AgentRunner] = []
        for agent in self.agents:
            per_agent_tools = build_per_agent_tools(
                agent_id=agent.id,
                mailbox=self.mailbox,
                known_agents=agent_ids,
            )
            tools = {**shared_tools, **per_agent_tools}
            provider = self._build_provider(agent)
            runners.append(AgentRunner(agent, provider, tools))

        # 3) 并发跑——agent loops + 监控者协程
        #    监控者：检测所有任务进入终态后立即取消 agent loops，避免 idle 等待
        async def _watcher(loops: list[asyncio.Task]) -> None:
            poll_interval = 0.05
            while True:
                await asyncio.sleep(poll_interval)
                tasks_now = await self.task_queue.list_all()
                terminal = all(
                    t.status in ("completed", "failed") for t in tasks_now
                )
                if terminal:
                    log.info("swarm=%s all tasks terminal; canceling agent loops",
                             self.name)
                    for lp in loops:
                        if not lp.done():
                            lp.cancel()
                    return

        try:
            loop_tasks = [
                asyncio.create_task(r.run_loop(self.task_queue, self.mailbox))
                for r in runners
            ]
            watcher_task = asyncio.create_task(_watcher(loop_tasks))

            stats_list: list[AgentLoopStats] = []
            for lp in loop_tasks:
                try:
                    stats_list.append(await lp)
                except asyncio.CancelledError:
                    # 被 watcher 取消——视为正常完成，stats 不可用
                    log.debug("agent loop cancelled by watcher")
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task
        except Exception as exc:  # noqa: BLE001
            log.exception("swarm crashed: %s", exc)
            return SwarmResult(
                name=self.name,
                state="failed",
                duration_seconds=time.monotonic() - t0,
                tasks_completed=0,
                tasks_failed=len(self.tasks),
                error=str(exc),
            )

        # 4) 汇总
        all_run_results: list[AgentRunResult] = []
        for s in stats_list:
            all_run_results.extend(s.task_results)

        # 任务最终状态来自 task_queue（不是 agent 的 task 副本）
        all_tasks_final = await self.task_queue.list_all()
        completed = sum(1 for t in all_tasks_final if t.status == "completed")
        failed = sum(1 for t in all_tasks_final if t.status == "failed")
        # blocked / pending 视为未完成——若 swarm 退出但仍有这些状态算 failed
        unfinished = sum(
            1 for t in all_tasks_final
            if t.status in ("blocked", "pending", "in_progress")
        )

        first_error: str | None = None
        for t in all_tasks_final:
            if t.status == "failed" and t.error:
                first_error = t.error
                break

        state = "completed" if (failed == 0 and unfinished == 0) else "failed"
        if unfinished > 0 and first_error is None:
            first_error = f"{unfinished} task(s) did not finish"

        duration = time.monotonic() - t0
        log.info(
            "swarm=%s done in %.1fs: %d completed, %d failed, %d unfinished",
            self.name, duration, completed, failed, unfinished,
        )

        return SwarmResult(
            name=self.name,
            state=state,
            duration_seconds=duration,
            tasks_completed=completed,
            tasks_failed=failed + unfinished,
            agent_results=all_run_results,
            agent_stats=list(stats_list),
            error=first_error if state == "failed" else None,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _build_provider(self, agent: Agent):
        kwargs: dict[str, Any] = {"default_model": agent.model}
        kwargs.update(self.provider_overrides.get(agent.provider, {}))
        return get_provider(agent.provider, **kwargs)


# ----------------------------------------------------------------------
# YAML 解析辅助
# ----------------------------------------------------------------------
def _parse_agent(cfg: dict[str, Any]) -> Agent:
    try:
        agent_id = cfg["id"]
        role = cfg["role"]
        persona = cfg.get("persona", "")
        provider = cfg["provider"]
        model = cfg["model"]
    except KeyError as exc:
        raise ValueError(f"agent config missing required field: {exc}") from exc

    tools = cfg.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError(f"agent {agent_id} 'tools' must be a list")

    capabilities = AgentCapabilities.worker(set(tools))

    raw_iter = cfg.get("max_iterations", 10)
    try:
        max_iter = int(raw_iter)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"agent {agent_id}: max_iterations must be int, got {raw_iter!r}"
        ) from exc
    if max_iter <= 0:
        raise ValueError(
            f"agent {agent_id}: max_iterations must be >= 1, got {max_iter}"
        )

    return Agent(
        id=agent_id,
        role=role,
        persona=persona,
        provider=provider,
        model=model,
        capabilities=capabilities,
        tools=tools,
        max_iterations=max_iter,
    )


def _parse_task(
    cfg: dict[str, Any], idx: int, agent_ids: set[str]
) -> Task:
    if not isinstance(cfg, dict):
        raise ValueError(f"task[{idx}] must be a mapping")
    title = cfg.get("title")
    if not title:
        raise ValueError(f"task[{idx}] missing 'title'")
    description = cfg.get("description") or title
    task_id = cfg.get("id") or f"t-{idx}"
    assigned_to = cfg.get("assigned_to")
    if assigned_to is not None and assigned_to not in agent_ids:
        raise ValueError(
            f"task[{idx}] assigned_to={assigned_to!r} is not a known agent id"
        )
    depends_on = cfg.get("depends_on") or []
    if not isinstance(depends_on, list):
        raise ValueError(f"task[{idx}] 'depends_on' must be a list")

    return Task(
        id=task_id,
        title=title,
        description=str(description),
        assigned_to=assigned_to,
        depends_on=list(depends_on),
    )


def _resolve_task_dependencies(tasks: list[Task]) -> None:
    """
    把 depends_on 中的 title 引用解析为 task.id（W2 用户友好）

    用户可写 depends_on: ["read README"]，我们把它替换为对应任务 id。
    若同时存在多个同名 title，报错——避免歧义。
    """
    title_to_ids: dict[str, list[str]] = {}
    for t in tasks:
        title_to_ids.setdefault(t.title, []).append(t.id)
    id_set = {t.id for t in tasks}

    for t in tasks:
        new_deps: list[str] = []
        for d in t.depends_on:
            if d in id_set:
                new_deps.append(d)
                continue
            if d in title_to_ids:
                ids = title_to_ids[d]
                if len(ids) > 1:
                    raise ValueError(
                        f"task {t.id!r} depends_on title {d!r} is ambiguous "
                        f"(matches {ids})—use task id instead"
                    )
                new_deps.append(ids[0])
                continue
            raise ValueError(
                f"task {t.id!r} depends_on={d!r} not found "
                f"(neither task id nor title)"
            )
        t.depends_on = new_deps
