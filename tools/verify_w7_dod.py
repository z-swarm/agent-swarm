"""
@module tools.verify_w7_dod
@brief  W7 DoD 验收脚本——对照 DESIGN §17.2 Phase 2 W1

DESIGN §17.2 Phase 2 W1 7 条 DoD 逐条机器验证；任一失败即退出码非 0。

@usage  .venv/bin/python tools/verify_w7_dod.py
@exit   0 = 7/7 通过，W7 阶段门控通过；非 0 = DoD 未全过
@note   阶段门控原则（DESIGN §15 延伸）：前一阶段 DoD 通过才能开下一阶段
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _check_yaml_example() -> tuple[bool, str]:
    """DoD ②：examples/w7_delegate.yaml 存在 + Swarm.from_yaml 成功"""
    from agent_swarm.core.swarm import Swarm

    p = REPO / "examples" / "w7_delegate.yaml"
    if not p.exists():
        return False, "examples/w7_delegate.yaml 不存在"
    try:
        swarm = Swarm.from_yaml(p)
    except Exception as exc:
        return False, f"Swarm.from_yaml 失败: {exc}"
    return True, (f"  agents: {[a.id for a in swarm.agents]}\n"
                  f"  tasks:  {[t.id for t in swarm.tasks]}")


def _check_yaml_role_types() -> tuple[bool, str]:
    """DoD ③：role_type 字段支持 + Phase 1 examples 向后兼容"""
    from agent_swarm.core.swarm import Swarm
    from agent_swarm.core.types import AgentCapabilities

    caps_lead = AgentCapabilities.lead()
    caps_plan = AgentCapabilities.plan_only()
    caps_worker = AgentCapabilities.worker({"read_file"})

    if not (caps_lead.can_spawn_agents
            and caps_plan.can_execute_actions is False
            and caps_worker.can_execute_actions):
        return False, "AgentCapabilities 预设字段错误"

    phase1_files = ["w1_hello.yaml", "w2_two_agents.yaml", "w3_resume.yaml",
                    "w5_secure.yaml", "w6_tui.yaml"]
    evidence: list[str] = []
    for f in phase1_files:
        p = REPO / "examples" / f
        if not p.exists():
            evidence.append(f"  (skip) {f}")
            continue
        try:
            Swarm.from_yaml(p)
            evidence.append(f"  ✓ {f}")
        except Exception as exc:
            return False, f"Phase 1 example {f} 解析失败: {exc}"
    return True, "\n".join(evidence)


def _check_lead_tools_injection() -> tuple[bool, str]:
    """DoD ④：lead 工具 5 个注入 + worker 工具不污染"""
    from agent_swarm.core.types import Agent, AgentCapabilities
    from agent_swarm.tools.builtin.lead import build_lead_tools

    class _StubCtx:
        def __init__(self, agents):
            self._a = {a.id: a for a in agents}

        def add_agent(self, agent): self._a[agent.id] = agent
        def remove_agent(self, agent_id): return self._a.pop(agent_id, None) is not None
        def get_agent(self, agent_id): return self._a.get(agent_id)
        def list_agents(self): return list(self._a.values())
        def assign_task_to(self, t, a): return False
        def update_task_status(self, t, s): return False

    lead = Agent(id="lead", role="lead", persona="", model="gpt-4o-mini",
                 provider="openai", capabilities=AgentCapabilities.lead())
    worker = Agent(id="w", role="worker", persona="", model="gpt-4o-mini",
                   provider="openai", capabilities=AgentCapabilities.worker({"read_file"}))
    lead_tools = {t.name for t in build_lead_tools("lead", _StubCtx([lead]))}
    expected = {"spawn_agent", "shutdown_agent", "assign_task",
                "update_task", "review_plan"}
    if lead_tools != expected:
        return False, f"lead tools 集合错: {sorted(lead_tools)}"
    if "spawn_agent" in worker.capabilities.allowed_tools:
        return False, "worker.allowed_tools 污染了 lead 工具"
    return True, (f"  lead tools: {sorted(lead_tools)}\n"
                  f"  worker.allowed_tools: {sorted(worker.capabilities.allowed_tools)}")


def _check_delegate_mode_validation() -> tuple[bool, str]:
    """DoD ⑤：DelegateMode 校验（无 lead / 无 worker → success=False + 明确 error）"""
    from agent_swarm.core.protocols import DelegateMode
    from agent_swarm.core.types import Agent, AgentCapabilities

    class _StubSwarm:
        def __init__(self, agents): self.agents = agents; self.tasks = []
        async def run(self): return _StubResult("completed")

    class _StubResult:
        def __init__(self, state): self.state = state

    worker = Agent(id="w", role="worker", persona="", model="gpt-4o-mini",
                   provider="openai", capabilities=AgentCapabilities.worker({"read_file"}))
    lead = Agent(id="lead", role="lead", persona="", model="gpt-4o-mini",
                 provider="openai", capabilities=AgentCapabilities.lead())

    async def run():
        r1 = await DelegateMode().execute(_StubSwarm([worker]))
        r2 = await DelegateMode().execute(_StubSwarm([lead]))
        return r1, r2

    r1, r2 = asyncio.run(run())
    ok = (r1.success is False and "lead" in (r1.error or "")
          and r2.success is False and "worker" in (r2.error or ""))
    if not ok:
        return False, f"校验逻辑错: r1={r1.success}/{r1.error!r} r2={r2.success}/{r2.error!r}"
    return True, f"  无 lead: {r1.error}\n  无 worker: {r2.error}"


def _check_artifacts() -> tuple[bool, str]:
    """DoD ⑥：ProtocolResult.artifacts 字段完整"""
    from agent_swarm.core.protocols import DelegateMode
    from agent_swarm.core.types import Agent, AgentCapabilities

    class _StubSwarm:
        def __init__(self, agents): self.agents = agents; self.tasks = []
        async def run(self): return _StubResult("completed")

    class _StubResult:
        def __init__(self, state): self.state = state

    lead = Agent(id="lead", role="lead", persona="", model="gpt-4o-mini",
                 provider="openai", capabilities=AgentCapabilities.lead())
    worker = Agent(id="w", role="worker", persona="", model="gpt-4o-mini",
                   provider="openai", capabilities=AgentCapabilities.worker({"read_file"}))

    async def run():
        return await DelegateMode().execute(_StubSwarm([lead, worker]))

    expected = {"leads", "workers", "tasks_total", "tasks_completed",
                "tasks_failed", "swarm_state"}
    art = asyncio.run(run()).artifacts
    missing = expected - art.keys()
    if missing:
        return False, f"artifacts 缺字段: {sorted(missing)}\n  has: {sorted(art.keys())}"
    return True, f"  字段: {sorted(art.keys())}"


def _check_readme() -> tuple[bool, str]:
    """DoD ⑦：README quickstart 含 W7 入口（CLI + 程序化）"""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    flags = {
        "w7_delegate.yaml": "w7_delegate.yaml" in readme,
        "DelegateMode 引用": "DelegateMode" in readme,
        "run_with_protocol 引用": "swarm.run_with_protocol" in readme,
    }
    missing = [k for k, v in flags.items() if not v]
    if missing:
        return False, f"README 缺: {missing}"
    return True, "  CLI + 程序化入口齐全"


def main() -> int:
    # ① pytest 计数——5 个 W7 测试文件
    test_files = [
        "tests/unit/test_types.py",
        "tests/unit/test_protocols.py",
        "tests/unit/test_swarm_protocol_api.py",
        "tests/unit/test_lead_tools.py",
        "tests/e2e/test_w7_delegate_e2e.py",
    ]
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", *test_files, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    check1_ok = result.returncode == 0
    check1_evidence = result.stdout.strip().splitlines()[-1] if result.stdout else (result.stderr or "")

    checks: list[tuple[str, bool, str]] = [
        ("① 5 个 W7 测试文件 pytest 全过", check1_ok, check1_evidence),
        ("② examples/w7_delegate.yaml 存在 + Swarm.from_yaml 成功", *_check_yaml_example()),
        ("③ role_type 字段支持 + Phase 1 examples 向后兼容", *_check_yaml_role_types()),
        ("④ lead 工具 5 个注入 + worker 工具不污染", *_check_lead_tools_injection()),
        ("⑤ DelegateMode 校验（无 lead / 无 worker → fail-fast）", *_check_delegate_mode_validation()),
        ("⑥ ProtocolResult.artifacts 字段完整", *_check_artifacts()),
        ("⑦ README quickstart 含 W7 入口（CLI + 程序化）", *_check_readme()),
    ]

    print("=" * 72)
    print(" W7 DoD 验收报告 (DESIGN §17.2 Phase 2 W1)")
    print("=" * 72)
    for name, ok, evidence in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
        if evidence:
            for line in evidence.splitlines()[:6]:
                print(f"     {line}")
    print("=" * 72)
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    if passed == total:
        print(f" 总计: {passed}/{total} 通过")
        print(" ✅ W7 DoD 全部通过 → 阶段门控 → 允许开 W8 (Adversarial Verify)")
        return 0
    print(f" 总计: {passed}/{total} 通过")
    print(" ❌ W7 DoD 未全过 → 阶段门控失败 → 停手回头修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
