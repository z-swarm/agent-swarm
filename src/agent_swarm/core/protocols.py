"""
@module agent_swarm.core.protocols
@brief  W7 协作协议抽象层——Phase 2 Delegate Mode / Adversarial Verify 共享基类

DESIGN §6.3: 所有具体协议（DelegateMode / AdversarialVerifier / ...）继承
CollaborationProtocol。Swarm 通过 set_protocol() 注册协议，run_with_protocol()
调用 execute() 驱动整轮协作。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_swarm.core.swarm import Swarm
    from agent_swarm.core.types import Agent


# ---------------------------------------------------------------------------
# 协议执行结果
# ---------------------------------------------------------------------------


@dataclass
class ProtocolResult:
    """
    协议执行结果——所有 CollaborationProtocol.execute() 的统一返回

    @note success=False 时 error 必填；summary 仍可填"已完成的中间产出"
          artifacts 用来向调用方返回结构化数据（Lead 写的总结 / Verifier 的 verdict）
    """

    success: bool
    summary: str = ""
    error: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 协议基类
# ---------------------------------------------------------------------------


class CollaborationProtocol(ABC):
    """
    协作协议抽象基类（DESIGN §6.3，v4.1 从 Protocol 改名以避开 typing.Protocol）

    协议 ≠ Agent 类——协议描述"一组 agent 怎么协作"，由 Swarm 在合适的时机触发。
    例如：
      - DelegateMode: Lead 派活、Workers 执行、Lead 汇总
      - AdversarialVerifier: 多 agent 独立评判假设、迭代收敛
    """

    @abstractmethod
    async def execute(self, swarm: "Swarm") -> ProtocolResult:
        """
        驱动一轮协议执行——阻塞直到协议达到终止条件

        @param swarm 当前 swarm 实例（agent 注册表 + task queue + mailbox）
        @return ProtocolResult
        @raise 协议内部错误向上抛；调用方负责兜底与记录
        """
        ...


# ---------------------------------------------------------------------------
# DelegateMode 协议——DESIGN §6.1
# ---------------------------------------------------------------------------


class DelegateMode(CollaborationProtocol):
    """
    委托协议：Lead 编排 + Worker 执行（DESIGN §6.1）

    校验规则：
      - swarm 至少含 1 个 lead capabilities 的 agent（can_spawn_agents=True）
      - swarm 至少含 1 个 worker capabilities 的 agent（can_execute_actions=True）

    执行流程（W7 骨架）：
      1. 校验 lead/worker 配比
      2. 复用 Phase 1 已验证的 swarm.run() 路径跑 task queue
         （lead 的 spawn_agent / assign_task 由 W7-5 工具集提供；本协议层不重复实现）
      3. 收尾：让 lead 写 final summary
         （W7 骨架简化：summary = swarm_result.summary 透传，
          W8+ 接入 Lead 工具的 review_plan 后实现真"汇总回环"）

    @note v4 修订：不再引入 LeadAgent / WorkerAgent 子类——通过 §7.1 的
          AgentCapabilities.lead() / .worker() 预设来表达，Agent 类保持单一。
    """

    def __init__(self, summary_label: str = "delegate") -> None:
        """
        @param summary_label 写入 ProtocolResult.artifacts["mode"] 的可读标签
        """
        self._summary_label = summary_label

    @staticmethod
    def _partition(agents: list["Agent"]) -> tuple[list["Agent"], list["Agent"]]:
        """
        按 capabilities 把 agent 拆成 (leads, workers)

        规则：can_spawn_agents=True 视为 lead；can_execute_actions=True 视为 worker
        两者皆可（既可 spawn 又可 execute——如 plan_only 角色），归入 leads 优先
        """
        leads: list["Agent"] = []
        workers: list["Agent"] = []
        for a in agents:
            if a.capabilities.can_spawn_agents:
                leads.append(a)
            elif a.capabilities.can_execute_actions:
                workers.append(a)
        return leads, workers

    async def execute(self, swarm: "Swarm") -> ProtocolResult:
        """驱动一轮 delegate：校验 → 跑 → 收 summary"""
        leads, workers = self._partition(swarm.agents)
        if not leads:
            return ProtocolResult(
                success=False,
                error=(
                    "DelegateMode requires at least 1 lead agent "
                    "(capabilities.can_spawn_agents=True); "
                    f"got {len(leads)} from {len(swarm.agents)} agents"
                ),
            )
        if not workers:
            return ProtocolResult(
                success=False,
                error=(
                    "DelegateMode requires at least 1 worker agent "
                    "(capabilities.can_execute_actions=True); "
                    f"got {len(workers)} from {len(swarm.agents)} agents"
                ),
            )

        # 复用 Phase 1 的 swarm.run()——不破坏向后兼容
        try:
            swarm_result = await swarm.run()
        except Exception as exc:  # noqa: BLE001
            return ProtocolResult(
                success=False,
                error=f"swarm.run() raised: {exc!r}",
                artifacts={
                    "mode": self._summary_label,
                    "leads": [a.id for a in leads],
                    "workers": [a.id for a in workers],
                },
            )

        completed = sum(1 for t in swarm.tasks if t.status == "completed")
        failed = sum(1 for t in swarm.tasks if t.status == "failed")
        # W7 骨架：summary 由协议层生成；W8+ 接入 lead 工具后由 lead 写最终 summary
        summary = (
            f"Delegated to {len(workers)} worker(s) by {len(leads)} lead(s): "
            f"{completed} completed, {failed} failed"
        )
        return ProtocolResult(
            success=swarm_result.state == "completed" and failed == 0,
            summary=summary,
            artifacts={
                "mode": self._summary_label,
                "leads": [a.id for a in leads],
                "workers": [a.id for a in workers],
                "tasks_total": len(swarm.tasks),
                "tasks_completed": completed,
                "tasks_failed": failed,
                "swarm_state": swarm_result.state,
            },
        )
