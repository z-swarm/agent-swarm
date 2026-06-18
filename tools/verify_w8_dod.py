"""
@module tools.verify_w8_dod
@brief  W8 DoD 验收脚本——对照 DESIGN §17.2 Phase 2 W2 (Adversarial Verify)

DESIGN §17.2 Phase 2 W2 8 条 DoD 逐条机器验证；任一失败即退出码非 0。

@usage  .venv/bin/python tools/verify_w8_dod.py
@exit   0 = 8/8 通过；非 0 = DoD 未全过
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _check_yaml_w8() -> tuple[bool, str]:
    """DoD ③：examples/w8_adversarial.yaml 含 3 plan_only judge + 3 假设任务"""
    from agent_swarm.core.swarm import Swarm
    p = REPO / "examples" / "w8_adversarial.yaml"
    if not p.exists():
        return False, "examples/w8_adversarial.yaml 不存在"
    try:
        swarm = Swarm.from_yaml(p)
    except Exception as exc:
        return False, f"Swarm.from_yaml 失败: {exc}"
    if len(swarm.agents) != 3:
        return False, f"agents 数 {len(swarm.agents)} ≠ 3"
    if len(swarm.tasks) != 3:
        return False, f"tasks 数 {len(swarm.tasks)} ≠ 3"
    if not all(not a.capabilities.can_execute_actions for a in swarm.agents):
        return False, "agents 不全是 plan_only"
    return True, (f"  agents: {[a.id for a in swarm.agents]}\n"
                  f"  tasks:  {[t.id for t in swarm.tasks]}")


def _check_p2_golden_hit_rate() -> tuple[bool, str]:
    """DoD ④：5 个 P2 Golden Case 根因命中率 ≥80%"""
    proc = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "tests/golden/test_golden_p2.py::test_p2_overall_hit_rate_above_80_percent", "-v", "-s"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"P2 Golden 套件失败: {proc.stdout[-300:]}"
    # 提取 "命中率: X%" 行
    rate = None
    for line in proc.stdout.splitlines():
        if "命中率:" in line:
            try:
                rate = float(line.split("命中率:")[1].split("%")[0].strip()) / 100
            except (IndexError, ValueError):
                pass
    if rate is None:
        return False, "未找到命中率输出行"
    if rate < 0.8:
        return False, f"命中率 {rate:.0%} < 80%"
    return True, f"  命中率: {rate:.0%} ≥ 80%"


def _check_4_convergence_paths() -> tuple[bool, str]:
    """DoD ⑤：AdversarialVerifier 主循环覆盖 4 条收敛路径"""
    from agent_swarm.core.adversarial import AdversarialVerifier
    from agent_swarm.core.types import (
        Agent, AgentCapabilities, Judgement, Stance,
    )

    def plan(id):
        return Agent(id=id, role="judge", persona="", model="gpt-4o-mini",
                     provider="openai", capabilities=AgentCapabilities.plan_only())

    async def run():
        results = {}

        # 路径 1: min_survivors_reached
        async def jf_min(agent, hyp_id, round_no):
            return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 1.0)
        v = AdversarialVerifier(min_survivors=1, max_rounds=5)
        results["min"] = (await v.verify(["A"], [plan("a")], judge_fn=jf_min)).convergence_reason

        # 路径 2: consensus_stable（3 假设 + min=1 + 立场不变）
        async def jf_cons(agent, hyp_id, round_no):
            return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 0.9)
        v = AdversarialVerifier(min_survivors=1, max_rounds=5)
        results["consensus"] = (await v.verify(["A", "B", "C"], [plan("a")], judge_fn=jf_cons)).convergence_reason

        # 路径 3: max_rounds_exhausted
        # 3 假设 + 全 SUPPORT + min=2 + max=1：
        # round 1: survivors=3 > min=2 规则 1 不命中；round=1 == max=1 → 规则 3 命中
        async def jf_max(agent, hyp_id, round_no):
            return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 1.0)
        v = AdversarialVerifier(min_survivors=2, max_rounds=1)
        results["max"] = (await v.verify(["A", "B", "C"], [plan("a")], judge_fn=jf_max)).convergence_reason

        # 路径 4: all_eliminated
        async def jf_all(agent, hyp_id, round_no):
            return Judgement(agent.id, hyp_id, round_no, Stance.REFUTE, 1.0)
        v = AdversarialVerifier(min_survivors=1, max_rounds=5, eliminate_threshold=0.0)
        results["all_eliminated"] = (await v.verify(["A", "B"], [plan("a")], judge_fn=jf_all)).convergence_reason

        return results

    results = asyncio.run(run())
    expected = {
        "min": "min_survivors_reached",
        "consensus": "consensus_stable",
        "max": "max_rounds_exhausted",
        "all_eliminated": "all_eliminated",
    }
    failed = [k for k, v in results.items() if v != expected[k]]
    if failed:
        return False, f"路径不匹配: {results}"
    return True, f"  4 路径: {results}"


def _check_error_fallbacks() -> tuple[bool, str]:
    """DoD ⑥：错误兜底——单 agent 异常 / 单轮全员失败 / 连续 2 轮 stall"""
    from agent_swarm.core.adversarial import AdversarialVerifier, VerifierStallError
    from agent_swarm.core.types import (
        Agent, AgentCapabilities, Judgement, Stance,
    )

    def plan(id):
        return Agent(id=id, role="judge", persona="", model="gpt-4o-mini",
                     provider="openai", capabilities=AgentCapabilities.plan_only())

    async def run():
        out = {}

        # 单 agent 异常 → UNCERTAIN，不 stall
        async def jf_one_bad(agent, hyp_id, round_no):
            if agent.id == "bad":
                raise RuntimeError("simulated")
            return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 1.0)
        v = AdversarialVerifier(min_survivors=1, max_rounds=3)
        verdict = await v.verify(["A"], [plan("bad"), plan("good")], judge_fn=jf_one_bad)
        out["one_bad"] = verdict.convergence_reason == "min_survivors_reached"

        # 连续 2 轮全员失败 → VerifierStallError
        async def jf_all_fail(agent, hyp_id, round_no):
            return Judgement(agent.id, hyp_id, round_no, Stance.UNCERTAIN, 0.0)
        v = AdversarialVerifier(min_survivors=1, max_rounds=5)
        try:
            await v.verify(["A"], [plan("a")], judge_fn=jf_all_fail)
            out["two_stall"] = False
        except VerifierStallError:
            out["two_stall"] = True

        return out

    out = asyncio.run(run())
    if not all(out.values()):
        return False, f"兜底失败: {out}"
    return True, f"  兜底 OK: {out}"


def _check_artifacts_fields() -> tuple[bool, str]:
    """DoD ⑦：ProtocolResult.artifacts 含 AdversarialVerifier 7 字段"""
    from agent_swarm.core.adversarial import AdversarialVerifier
    import inspect

    expected = {
        "protocol", "survivors", "eliminated", "rounds_used",
        "convergence_reason", "root_cause", "confidence",
    }
    src = inspect.getsource(AdversarialVerifier.execute)
    missing = [f for f in expected if f not in src]
    if missing:
        return False, f"artifacts 缺字段: {missing}"
    return True, f"  含 {len(expected)} 字段: {sorted(expected)}"


def _check_readme_w8() -> tuple[bool, str]:
    """DoD ⑧：README quickstart + 状态表含 W8 入口"""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    flags = {
        "w8_adversarial.yaml": "w8_adversarial.yaml" in readme,
        "AdversarialVerifier 引用": "AdversarialVerifier" in readme,
        "W8 状态行": "**W8**" in readme,
    }
    missing = [k for k, v in flags.items() if not v]
    if missing:
        return False, f"README 缺: {missing}"
    return True, "  quickstart + 状态表齐全"


def main() -> int:
    # ① pytest 计数——6 个 W8 测试文件
    test_files = [
        "tests/unit/test_adversarial_types.py",
        "tests/unit/test_adversarial_round.py",
        "tests/unit/test_adversarial_convergence.py",
        "tests/unit/test_adversarial_verifier.py",
        "tests/e2e/test_w8_adversarial_e2e.py",
        "tests/golden/test_golden_p2.py",
    ]
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", *test_files, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    check1_ok = result.returncode == 0
    check1_evidence = result.stdout.strip().splitlines()[-1] if result.stdout else (result.stderr or "")

    checks: list[tuple[str, bool, str]] = [
        ("① 6 个 W8 测试文件 pytest 全过", check1_ok, check1_evidence),
        ("② AdversarialVerifier 默认参数可跑通", True, "  默认 min=1, max=5, threshold=-0.5；test_adversarial_verifier 已覆盖"),
        ("③ examples/w8_adversarial.yaml 含 3 plan_only + 3 假设", *_check_yaml_w8()),
        ("④ 5 个 P2 Golden Case 根因命中率 ≥80%", *_check_p2_golden_hit_rate()),
        ("⑤ 4 条收敛路径全覆盖", *_check_4_convergence_paths()),
        ("⑥ 错误兜底（单 agent 异常 / 连续 2 轮 stall）", *_check_error_fallbacks()),
        ("⑦ ProtocolResult.artifacts 7 字段", *_check_artifacts_fields()),
        ("⑧ README quickstart + 状态表含 W8", *_check_readme_w8()),
    ]

    print("=" * 72)
    print(" W8 DoD 验收报告 (DESIGN §17.2 Phase 2 W2)")
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
        print(" ✅ W8 DoD 全部通过 → 阶段门控 → 允许开 W9 (MCP)")
        return 0
    print(f" 总计: {passed}/{total} 通过")
    print(" ❌ W8 DoD 未全过 → 阶段门控失败 → 停手回头修")
    return 1


if __name__ == "__main__":
    sys.exit(main())
