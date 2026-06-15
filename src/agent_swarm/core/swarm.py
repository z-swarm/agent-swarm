"""
@module agent_swarm.core.swarm
@brief  Swarm 编排器（W1 单 agent 单任务版本）

DESIGN.md §3.2 完整 API；W1 仅实现：
  - Swarm.from_yaml()
  - run()
  - 单 agent + 单任务 → 串行执行

后续扩展：add_agent/add_task/pause/resume/handle_external_message
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_swarm.core.agent_runner import AgentRunner, AgentRunResult
from agent_swarm.core.types import Agent, AgentCapabilities, Task
from agent_swarm.providers import get_provider
from agent_swarm.tools import build_default_tools

log = logging.getLogger(__name__)


@dataclass
class SwarmResult:
    """Swarm.run() 返回值——DESIGN.md §A.1 子集"""

    name: str
    state: str  # "completed" / "failed"
    duration_seconds: float
    tasks_completed: int
    tasks_failed: int
    agent_results: list[AgentRunResult] = field(default_factory=list)
    error: str | None = None


class Swarm:
    """W1 最小 Swarm——单 agent 串行跑任务列表"""

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
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self.provider_overrides = provider_overrides or {}

    # ------------------------------------------------------------------
    # 加载入口
    # ------------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> Swarm:
        """从 YAML 文件构造 Swarm（W1 schema 见 examples/w1_hello.yaml）"""
        p = Path(path)
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            raise ValueError(f"YAML root must be a mapping, got {type(cfg).__name__}")

        return cls.from_dict(cfg, base_dir=p.parent.resolve())

    @classmethod
    def from_dict(cls, cfg: dict[str, Any], base_dir: Path | None = None) -> Swarm:
        """从 dict 构造 Swarm（便于测试）"""
        name = cfg.get("name", "unnamed-swarm")

        agents_cfg = cfg.get("agents") or []
        if not agents_cfg:
            raise ValueError("config missing 'agents'")
        agents = [_parse_agent(a) for a in agents_cfg]

        tasks_cfg = cfg.get("tasks") or []
        if not tasks_cfg:
            raise ValueError("config missing 'tasks'")
        tasks = [_parse_task(t, idx=i) for i, t in enumerate(tasks_cfg)]

        # workspace：默认 yaml 所在目录；可被 cfg.workspace 覆盖
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
        @brief W1 串行执行：第一个 agent 跑所有任务

        后续 Weekly Slice：
          - W2 → 多 agent 并发，TaskQueue 认领
          - W3 → 中断恢复
        """
        import time as _time

        t0 = _time.monotonic()
        log.info("swarm=%s start (%d agent(s), %d task(s))",
                 self.name, len(self.agents), len(self.tasks))

        # W1：唯一 agent
        agent = self.agents[0]
        provider = self._build_provider(agent)
        tools = build_default_tools(workspace=self.workspace)
        runner = AgentRunner(agent=agent, provider=provider, tools=tools)

        results: list[AgentRunResult] = []
        completed = 0
        failed = 0
        first_error: str | None = None

        for task in self.tasks:
            try:
                res = await runner.run(task)
                results.append(res)
                if task.status == "completed":
                    completed += 1
                else:
                    failed += 1
                    if first_error is None:
                        first_error = task.error or "unknown failure"
            except Exception as exc:  # noqa: BLE001 - W1 顶层兜底
                # B2 修复：runner 抛异常时仍要保持 results 与 tasks 一一对应
                # 不然 CLI 表格会少行，agent_results 长度 != completed+failed
                log.exception("swarm task failed: %s", exc)
                task.status = "failed"
                task.error = str(exc)
                failed += 1
                if first_error is None:
                    first_error = str(exc)
                results.append(
                    AgentRunResult(
                        task=task,
                        history=[],
                        iterations=0,
                        tokens_total=0,
                        final_text="",
                        finish_reason="error",
                    )
                )

        state = "completed" if failed == 0 else "failed"
        duration = _time.monotonic() - t0

        log.info(
            "swarm=%s done in %.1fs: %d completed, %d failed",
            self.name, duration, completed, failed,
        )

        return SwarmResult(
            name=self.name,
            state=state,
            duration_seconds=duration,
            tasks_completed=completed,
            tasks_failed=failed,
            agent_results=results,
            error=first_error if state == "failed" else None,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _build_provider(self, agent: Agent):
        """根据 agent 配置构造 provider；支持测试期 override"""
        kwargs: dict[str, Any] = {"default_model": agent.model}
        kwargs.update(self.provider_overrides.get(agent.provider, {}))
        return get_provider(agent.provider, **kwargs)


# ----------------------------------------------------------------------
# YAML 解析辅助
# ----------------------------------------------------------------------
def _parse_agent(cfg: dict[str, Any]) -> Agent:
    """解析单个 agent 配置——W1 schema：id/role/persona/provider/model/tools"""
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

    # B6 修复：max_iterations 必须是合法正整数；YAML 写错（"five" / -1 / 0）
    # 应在加载阶段就拒绝，而不是 runner 跑起来才崩
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


def _parse_task(cfg: dict[str, Any], idx: int) -> Task:
    """解析单个 task 配置——W1 schema：title/description"""
    if not isinstance(cfg, dict):
        raise ValueError(f"task[{idx}] must be a mapping")
    title = cfg.get("title")
    if not title:
        raise ValueError(f"task[{idx}] missing 'title'")
    # B3 修复：dict.get 默认值仅在 key 不存在时生效；
    # 显式 `description: null` 会拿到 None → str(None) = "None" 注入 LLM
    # 用 `or title` 兜底覆盖空字符串/None 两种情况
    description = cfg.get("description") or title
    task_id = cfg.get("id") or f"t-{idx}"
    return Task(id=task_id, title=title, description=str(description))
