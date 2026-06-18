"""
@module agent_swarm.core.swarm
@brief  Swarm 编排器（W2 多 agent 并发 + W3 ObservabilityBus 集成）

DESIGN.md §3.2 完整 API；W2 阶段:
  - Swarm.from_yaml() / from_dict()
  - run() → 多 agent 并发跑（asyncio.gather agent.run_loop）
  - TaskQueue 内存实现
  - Mailbox 内存实现

W3 增强:
  - 自动生成 session_id（uuid4）
  - 启动时注入 ObservabilityBus（默认 JsonLogSink + InMemorySink）
  - run() 周期内 emit swarm.* 事件
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from agent_swarm.core.agent_runner import AgentLoopStats, AgentRunner, AgentRunResult
from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.protocols import CollaborationProtocol, ProtocolResult
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Agent, AgentCapabilities, Task
from agent_swarm.observability import (
    emit,
)
from agent_swarm.providers import get_provider
from agent_swarm.security.context import (
    SecurityContext,
    SecurityContextManager,
)
from agent_swarm.skills import SkillRegistry
from agent_swarm.tools import build_per_agent_tools, build_shared_tools

log = logging.getLogger(__name__)


@dataclass
class SwarmResult:
    """
    Swarm.run() 返回值——DESIGN.md §A.1

    W2-B6 修复：tasks_failed 与 tasks_unfinished 分开统计，避免语义混淆。
    """

    name: str
    state: str  # "completed" / "failed"
    duration_seconds: float
    tasks_completed: int
    tasks_failed: int  # 仅 status=failed 的任务数
    tasks_unfinished: int = 0  # blocked / pending / in_progress 残留
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
        session_id: str | None = None,
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
        # W3: 自动分配 session_id 用于事件流标记 / SessionManager 恢复
        self.session_id = session_id or f"s-{uuid4().hex[:12]}"

        # W3 共享基础设施——session_id 同步注入
        self.task_queue = TaskQueue(session_id=self.session_id)
        self.mailbox = Mailbox(session_id=self.session_id)
        self._run_called: bool = False  # W2-B8: 防止 run() 被调多次
        # W7: 协作协议——None 表示不启用协议（走 Phase 1 run() 直跑模式）
        self.protocol: CollaborationProtocol | None = None

    # ------------------------------------------------------------------
    # 协议注册（W7 入口）
    # ------------------------------------------------------------------
    def set_protocol(self, protocol: CollaborationProtocol) -> None:
        """
        注册协作协议——例如 DelegateMode() / AdversarialVerifier(...)

        @note 必须在 run_with_protocol() 之前调用；W7 暂只支持注册一次
              （重复注册抛 ValueError，避免后注册的协议静默覆盖前一个）
        """
        if self.protocol is not None:
            raise ValueError(
                f"Swarm {self.name!r} already has protocol "
                f"{type(self.protocol).__name__}; create a new Swarm to switch"
            )
        self.protocol = protocol

    async def run_with_protocol(self) -> ProtocolResult:
        """
        按协议驱动一轮协作——W7 入口

        行为：
          - 协议未注册 → 抛 ValueError（强制调用方先 set_protocol）
          - 协议已注册 → 调用 protocol.execute(self) 并返回其 ProtocolResult
          - 协议执行抛异常 → 包装成 ProtocolResult(success=False, error=...)
        """
        if self.protocol is None:
            raise ValueError(
                f"Swarm {self.name!r} has no protocol registered; "
                "call set_protocol() first or use run() for direct execution"
            )
        try:
            return await self.protocol.execute(self)
        except Exception as exc:  # noqa: BLE001
            return ProtocolResult(
                success=False,
                error=f"{type(self.protocol).__name__}.execute() raised: {exc!r}",
                artifacts={"protocol": type(self.protocol).__name__},
            )

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

        @raise RuntimeError 同一 Swarm 实例的 run() 被调用第二次（W2-B8）

        @note F-01/F-09: 整个 run() 包在 SecurityContextManager.async_scope 内;
              asyncio.create_task 显式传 context= 避免跨 task 边界丢 ctx
        """
        # W2-B8: 防止重复 run——TaskQueue 状态会污染，必须新建 Swarm 重跑
        if self._run_called:
            raise RuntimeError(
                f"Swarm {self.name!r} run() already called; "
                "create a new Swarm instance to run again"
            )
        self._run_called = True

        # F-01: 从 ctx 隐式取 tenant_id, 用 self.session_id 显式覆盖
        existing = SecurityContextManager.current_or_default(session_id=self.session_id)
        ctx = SecurityContext(
            tenant_id=existing.tenant_id,
            session_id=self.session_id,
            user=existing.user,
            request_id=existing.request_id,
        )

        # F-09: 整个 run() 在 async_scope 内
        async with SecurityContextManager.async_scope(ctx):
            return await self._run_impl()

    async def _run_impl(self) -> SwarmResult:
        """@brief Swarm.run 的实际实现——F-09 在 async_scope 内调用"""
        t0 = time.monotonic()
        log.info(
            "swarm=%s session=%s start (%d agent(s), %d task(s))",
            self.name, self.session_id, len(self.agents), len(self.tasks),
        )
        await emit(
            "swarm.started",
            self.session_id,
            {
                "name": self.name,
                "agent_ids": [a.id for a in self.agents],
                # P-3 修复:TUI 之前只显示 (?),因为 payload 里没 model
                "agents": [
                    {"id": a.id, "model": a.model, "role": a.role}
                    for a in self.agents
                ],
                "task_count": len(self.tasks),
            },
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

        stats_list: list[AgentLoopStats] = []
        watcher_task: asyncio.Task | None = None
        try:
            # F-09: create_task 显式传 context——防跨 task 边界丢 ctx
            ctx_var = SecurityContextManager.current().asyncio_context()
            loop_tasks = [
                asyncio.create_task(
                    r.run_loop(self.task_queue, self.mailbox),
                    context=ctx_var,
                )
                for r in runners
            ]
            watcher_task = asyncio.create_task(_watcher(loop_tasks), context=ctx_var)

            for lp in loop_tasks:
                try:
                    stats_list.append(await lp)
                except asyncio.CancelledError:
                    # 被 watcher 取消——视为正常完成，stats 不可用
                    log.debug("agent loop cancelled by watcher")
        except Exception as exc:  # noqa: BLE001
            log.exception("swarm crashed: %s", exc)
            # W3-Z1 修复：crash 路径也要 emit swarm.failed，保证事件流完整
            await emit(
                "swarm.failed",
                self.session_id,
                {
                    "duration_seconds": time.monotonic() - t0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "tasks_unfinished": len(self.tasks),
                    "error": str(exc),
                },
            )
            return SwarmResult(
                name=self.name,
                state="failed",
                duration_seconds=time.monotonic() - t0,
                tasks_completed=0,
                tasks_failed=0,
                tasks_unfinished=len(self.tasks),
                error=str(exc),
            )
        finally:
            # BUG-2 修复:watcher 协程在 crash 路径必须被 cancel + await,
            # 否则它会在 _watcher() 里持续 await asyncio.sleep(0.05) 轮询,
            # 嵌入长生命周期进程(IM/REST 场景)会协程泄露
            if watcher_task is not None and not watcher_task.done():
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher_task

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
        await emit(
            "swarm.completed" if state == "completed" else "swarm.failed",
            self.session_id,
            {
                "duration_seconds": duration,
                "tasks_completed": completed,
                "tasks_failed": failed,
                "tasks_unfinished": unfinished,
                "error": first_error,
            },
        )

        return SwarmResult(
            name=self.name,
            state=state,
            duration_seconds=duration,
            tasks_completed=completed,
            tasks_failed=failed,                # W2-B6: 不再混入 unfinished
            tasks_unfinished=unfinished,
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

    # W4: skills 字段（可选，默认空）
    skills = cfg.get("skills") or []
    if not isinstance(skills, list):
        raise ValueError(f"agent {agent_id} 'skills' must be a list")

    # 自动并入 skill required_tools——避免用户必须重复声明
    # （未注册的 skill 仅 warning，不阻断；与 AgentRunner 的处理保持一致）
    auto_tools: set[str] = set(tools)
    for sid in skills:
        s = SkillRegistry.get(sid)
        if s is not None:
            auto_tools.update(s.required_tools)

    capabilities = AgentCapabilities.worker(auto_tools)

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
        tools=list(auto_tools),
        skills=skills,
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
    把 depends_on 中的 title 引用解析为 task.id（W2 用户友好），并检测循环依赖。

    用户可写 depends_on: ["read README"]，我们把它替换为对应任务 id。
    若同时存在多个同名 title，报错——避免歧义。

    W2-B9 修复：解析后做拓扑排序检测环，避免 swarm 启动后所有任务 blocked。
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

    # W2-B9: 拓扑排序检测环——Kahn 算法
    in_degree = {t.id: 0 for t in tasks}
    for t in tasks:
        # t 依赖每个 d → t 入度增 = len(t.depends_on)
        in_degree[t.id] = len(t.depends_on)

    # 反向邻接表：dep → 依赖它的任务
    rev_adj: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            rev_adj[d].append(t.id)

    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for nxt in rev_adj[cur]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if visited != len(tasks):
        cyclic = [tid for tid, deg in in_degree.items() if deg > 0]
        raise ValueError(
            f"task dependency cycle detected involving: {sorted(cyclic)}"
        )
