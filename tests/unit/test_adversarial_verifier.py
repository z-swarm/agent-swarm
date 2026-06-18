"""单元测试：AdversarialVerifier 协议（W8-4 / DESIGN §6.2.5）"""

from __future__ import annotations

import pytest

from agent_swarm.core.adversarial import (
    AdversarialVerifier,
    VerifierStallError,
)
from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    Judgement,
    Stance,
)


def _plan_only(id: str) -> Agent:
    return Agent(
        id=id, role="judge", persona="", model="gpt-4o-mini",
        provider="openai", capabilities=AgentCapabilities.plan_only(),
    )


def _make_judge_fn(per_hyp_stance: dict[str, Stance], confidence: float = 1.0):
    """
    构造确定性 judge_fn：每个 (agent, hyp) 组合按 per_hyp_stance 给立场

    @param per_hyp_stance  hypothesis_id -> Stance
    """
    async def judge_fn(agent, hyp_id, round_no):
        return Judgement(
            agent_id=agent.id, hypothesis_id=hyp_id, round_no=round_no,
            stance=per_hyp_stance[hyp_id], confidence=confidence,
            reasoning=f"scripted: {per_hyp_stance[hyp_id].value}",
        )
    return judge_fn


# ---------------------------------------------------------------------------
# 主循环 + 收敛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_survivors_reached_after_one_round() -> None:
    """1 假设 + 全部 SUPPORT → 第 1 轮就 min_survivors_reached"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=5)
    verdict = await v.verify(
        hypotheses=["root cause: X"],
        agents=[_plan_only("a1"), _plan_only("a2")],
        judge_fn=_make_judge_fn({"h0": Stance.SUPPORT}),
    )
    assert verdict.convergence_reason == "min_survivors_reached"
    assert len(verdict.survivors) == 1
    assert verdict.root_cause == "root cause: X"


@pytest.mark.asyncio
async def test_consensus_stable_after_two_rounds() -> None:
    """3 假设 + min=1 + 全 SUPPORT → round 1 不命中 min（3>1），
    round 2 stance 不变 → consensus_stable（避免 max 截断）"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=5)
    verdict = await v.verify(
        hypotheses=["hypothesis A", "hypothesis B", "hypothesis C"],
        agents=[_plan_only("a1")],
        judge_fn=_make_judge_fn({
            "h0": Stance.SUPPORT, "h1": Stance.SUPPORT, "h2": Stance.SUPPORT,
        }),
    )
    assert verdict.convergence_reason == "consensus_stable"
    assert len(verdict.survivors) == 3


@pytest.mark.asyncio
async def test_max_rounds_exhausted_with_two_survivors() -> None:
    """2 假设 + min=1 + 立场每轮变化 → 永远不共识；max_rounds 截断"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=2)
    # round 1: SUPPORT both; round 2: REFUTE both → support_score <0 → 连续 2 轮负数
    # 会触发 cond_b 淘汰，所以换种方式
    call = {"n": 0}
    async def judge_fn(agent, hyp_id, round_no):
        call["n"] += 1
        # 立场随 round 变化（防 consensus_stable），但不淘汰
        if round_no == 1:
            stance = Stance.SUPPORT
        else:
            stance = Stance.UNCERTAIN  # score=0，不触发任何淘汰
        return Judgement(agent.id, hyp_id, round_no, stance, 0.8)
    verdict = await v.verify(
        hypotheses=["A", "B"],
        agents=[_plan_only("a1")],
        judge_fn=judge_fn,
    )
    assert verdict.convergence_reason == "max_rounds_exhausted"
    assert verdict.rounds_used == 2


@pytest.mark.asyncio
async def test_all_eliminated_returns_weak_recommendation() -> None:
    """全淘汰 → all_eliminated + 被淘汰最晚的假设作弱推荐"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=5, eliminate_threshold=0.0)
    # 全部 REFUTE + 高 confidence → score=-1 < threshold=0 → 全淘汰
    verdict = await v.verify(
        hypotheses=["A", "B"],
        agents=[_plan_only("a1")],
        judge_fn=_make_judge_fn({"h0": Stance.REFUTE, "h1": Stance.REFUTE}),
    )
    assert verdict.convergence_reason == "all_eliminated"
    assert len(verdict.survivors) == 0
    assert verdict.root_cause is not None
    assert "weak recommendation" in verdict.root_cause
    assert verdict.confidence == 0.1


# ---------------------------------------------------------------------------
# 错误兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_all_failed_is_rolled_back() -> None:
    """单轮全员失败 → 该轮作废（不计入 max_rounds），stare 不变"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=5)

    call = {"n": 0}
    async def judge_fn(agent, hyp_id, round_no):
        call["n"] += 1
        if round_no == 1:
            return Judgement(agent.id, hyp_id, 1, Stance.UNCERTAIN, 0.0)
        # round 2：恢复
        return Judgement(agent.id, hyp_id, 2, Stance.SUPPORT, 1.0)
    verdict = await v.verify(
        hypotheses=["A"],
        agents=[_plan_only("a1")],
        judge_fn=judge_fn,
    )
    # round 1 失败作废，round 2 正常 → min_survivors=1 命中
    assert call["n"] == 2  # 两个 round 都调了 judge_fn
    assert verdict.convergence_reason == "min_survivors_reached"
    assert verdict.rounds_used == 2


@pytest.mark.asyncio
async def test_two_consecutive_all_failed_rounds_raise_stall() -> None:
    """连续 2 轮全员失败 → VerifierStallError"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=5)

    async def judge_fn(agent, hyp_id, round_no):
        return Judgement(agent.id, hyp_id, round_no, Stance.UNCERTAIN, 0.0)
    with pytest.raises(VerifierStallError, match="stalled"):
        await v.verify(
            hypotheses=["A"],
            agents=[_plan_only("a1")],
            judge_fn=judge_fn,
        )


# ---------------------------------------------------------------------------
# API 校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_hypotheses_raises() -> None:
    v = AdversarialVerifier()
    with pytest.raises(ValueError, match="hypotheses"):
        await v.verify(hypotheses=[], agents=[_plan_only("a1")])


@pytest.mark.asyncio
async def test_empty_agents_raises() -> None:
    v = AdversarialVerifier()
    with pytest.raises(ValueError, match="agents"):
        await v.verify(hypotheses=["A"], agents=[])


def test_invalid_min_survivors_raises() -> None:
    with pytest.raises(ValueError, match="min_survivors"):
        AdversarialVerifier(min_survivors=-1)


def test_invalid_max_rounds_raises() -> None:
    with pytest.raises(ValueError, match="max_rounds"):
        AdversarialVerifier(max_rounds=0)


# ---------------------------------------------------------------------------
# judge_fn 异常由 gather_round 兜底为 UNCERTAIN（已在 W8-2 覆盖），
# 验证 Verifier 不会被单个 agent 异常拖垮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_agent_exception_does_not_stall_verifier() -> None:
    """1 个 agent 抛异常 → UNCERTAIN；另 1 个 agent 正常 SUPPORT → 收敛"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=3)

    async def judge_fn(agent, hyp_id, round_no):
        if agent.id == "bad":
            raise RuntimeError("simulated")
        return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 1.0)
    verdict = await v.verify(
        hypotheses=["A"],
        agents=[_plan_only("bad"), _plan_only("good")],
        judge_fn=judge_fn,
    )
    assert verdict.convergence_reason == "min_survivors_reached"


# ---------------------------------------------------------------------------
# Verdict.full_history + root_cause confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_history_contains_all_judgements() -> None:
    """Verdict.full_history 含全部 Judgement"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=2)
    verdict = await v.verify(
        hypotheses=["A"],
        agents=[_plan_only("a1")],
        judge_fn=_make_judge_fn({"h0": Stance.SUPPORT}),
    )
    assert len(verdict.full_history) >= 1
    assert all(isinstance(j, Judgement) for j in verdict.full_history)


@pytest.mark.asyncio
async def test_root_cause_confidence_from_support_confidences() -> None:
    """survivors=1 时 confidence = 最后一轮 SUPPORT 立场 confidence 均值"""
    v = AdversarialVerifier(min_survivors=1, max_rounds=3)
    verdict = await v.verify(
        hypotheses=["A"],
        agents=[_plan_only("a1"), _plan_only("a2")],
        judge_fn=_make_judge_fn({"h0": Stance.SUPPORT}, confidence=0.8),
    )
    assert verdict.confidence == pytest.approx(0.8)
