"""单元测试：adversarial 单轮算法 + 淘汰判定（W8-2 / DESIGN §6.2.3）"""

from __future__ import annotations

import pytest

from agent_swarm.core.adversarial import (
    attach_judgements,
    compute_support_scores,
    eliminate,
    gather_round,
)
from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    HypothesisState,
    Judgement,
    Stance,
)

# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def _plan_only_agent(id: str) -> Agent:
    return Agent(
        id=id, role="judge", persona="", model="gpt-4o-mini",
        provider="openai", capabilities=AgentCapabilities.plan_only(),
    )


def _hs(id: str, statement: str = "h") -> HypothesisState:
    return HypothesisState(id=id, statement=statement)


# ---------------------------------------------------------------------------
# gather_round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_round_returns_judgement_per_pair() -> None:
    """2 agents × 3 hypotheses = 6 judgements"""
    agents = [_plan_only_agent("a1"), _plan_only_agent("a2")]
    hyps = [_hs(f"h{i}") for i in range(3)]

    async def judge_fn(agent, hyp_id, round_no):
        return Judgement(
            agent_id=agent.id, hypothesis_id=hyp_id, round_no=round_no,
            stance=Stance.SUPPORT, confidence=1.0,
        )
    judgements = await gather_round(agents, hyps, round_no=1, judge_fn=judge_fn)
    assert len(judgements) == 6
    # 所有 judgement 的 round_no 都是 1
    assert all(j.round_no == 1 for j in judgements)


@pytest.mark.asyncio
async def test_gather_round_skips_eliminated_hypotheses() -> None:
    """已淘汰假设不参与本轮 judgement"""
    agents = [_plan_only_agent("a1")]
    h1 = _hs("h1")
    h2 = _hs("h2")
    h2.eliminated = True
    h2.eliminated_at_round = 0

    async def judge_fn(agent, hyp_id, round_no):
        return Judgement(
            agent_id=agent.id, hypothesis_id=hyp_id, round_no=round_no,
            stance=Stance.SUPPORT, confidence=1.0,
        )
    judgements = await gather_round(agents, [h1, h2], round_no=1, judge_fn=judge_fn)
    assert len(judgements) == 1
    assert judgements[0].hypothesis_id == "h1"


@pytest.mark.asyncio
async def test_gather_round_treats_judge_exception_as_uncertain() -> None:
    """judge_fn 抛异常时该 (agent, hyp) 组合 → UNCERTAIN（DESIGN §6.2.5）"""
    agents = [_plan_only_agent("a1"), _plan_only_agent("a2")]
    hyps = [_hs("h1")]

    async def judge_fn(agent, hyp_id, round_no):
        if agent.id == "a1":
            raise RuntimeError("simulated LLM failure")
        return Judgement(
            agent_id=agent.id, hypothesis_id=hyp_id, round_no=round_no,
            stance=Stance.SUPPORT, confidence=1.0,
        )
    judgements = await gather_round(agents, hyps, round_no=1, judge_fn=judge_fn)
    a1_j = next(j for j in judgements if j.agent_id == "a1")
    a2_j = next(j for j in judgements if j.agent_id == "a2")
    assert a1_j.stance == Stance.UNCERTAIN
    assert a1_j.confidence == 0.0
    assert "judge_fn raised" in a1_j.reasoning
    assert a2_j.stance == Stance.SUPPORT  # 另一个 agent 不受影响


@pytest.mark.asyncio
async def test_gather_round_empty_inputs_returns_empty() -> None:
    """agents=[] 或 全 eliminated → 返回 []"""
    h = _hs("h1")
    h.eliminated = True
    judgements = await gather_round([], [h], round_no=1, judge_fn=lambda *_: None)
    assert judgements == []


# ---------------------------------------------------------------------------
# attach_judgements
# ---------------------------------------------------------------------------


def test_attach_judgements_groups_by_round() -> None:
    """attach_judgements 按 round_no 分组挂到 hypothesis"""
    h1 = _hs("h1")
    h2 = _hs("h2")
    judgements = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 0.9),
        Judgement("a2", "h1", 1, Stance.REFUTE, 0.8),
        Judgement("a1", "h2", 1, Stance.UNCERTAIN, 0.5),
    ]
    attach_judgements([h1, h2], judgements)
    assert len(h1.judgements_by_round[1]) == 2
    assert len(h2.judgements_by_round[1]) == 1


# ---------------------------------------------------------------------------
# compute_support_scores
# ---------------------------------------------------------------------------


def test_compute_support_scores_skips_eliminated() -> None:
    """compute_support_scores 不返回已淘汰假设"""
    h1 = _hs("h1")
    h1.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0)
    ]
    h2 = _hs("h2")
    h2.eliminated = True
    h2.judgements_by_round[1] = [
        Judgement("a1", "h2", 1, Stance.SUPPORT, 1.0)
    ]
    scores = compute_support_scores([h1, h2], 1)
    assert "h1" in scores
    assert "h2" not in scores
    assert scores["h1"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# eliminate
# ---------------------------------------------------------------------------


def test_eliminate_score_below_threshold() -> None:
    """条件 a：score <= threshold → 淘汰"""
    h = _hs("h1")
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.REFUTE, 1.0),
        Judgement("a2", "h1", 1, Stance.REFUTE, 1.0),
    ]
    result = eliminate([h], compute_support_scores([h], 1), threshold=-0.5)
    assert h.eliminated is True
    assert h.eliminated_at_round == 1
    assert result.just_eliminated == [h]
    assert result.still_alive == []


def test_eliminate_score_above_threshold_survives() -> None:
    """score > threshold → 存活"""
    h = _hs("h1")
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0),
    ]
    result = eliminate([h], compute_support_scores([h], 1), threshold=-0.5)
    assert h.eliminated is False
    assert result.still_alive == [h]


def test_eliminate_two_consecutive_negative_rounds() -> None:
    """条件 b：连续 2 轮 score < 0 → 淘汰"""
    h = _hs("h1")
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.REFUTE, 0.6),
    ]
    h.judgements_by_round[2] = [
        Judgement("a1", "h1", 2, Stance.REFUTE, 0.6),
    ]
    # round 2 的 score=-0.6 > threshold=-0.5 看似存活，但 cond_b 触发
    result = eliminate([h], compute_support_scores([h], 2), threshold=-0.5)
    assert h.eliminated is True
    assert result.just_eliminated == [h]


def test_eliminate_no_support_stance_in_round() -> None:
    """条件 c：最新一轮没有 SUPPORT → 淘汰"""
    h = _hs("h1")
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.UNCERTAIN, 0.5),
        Judgement("a2", "h1", 1, Stance.REFUTE, 0.3),
    ]
    # score = (-0.3) / 2 = -0.15 > threshold=-0.5 看似存活
    # 但 cond_c 触发（无 SUPPORT）
    eliminate([h], compute_support_scores([h], 1), threshold=-0.5)
    assert h.eliminated is True


def test_eliminate_does_not_re_eliminate() -> None:
    """已淘汰的假设不会被再次放进 just_eliminated"""
    h = _hs("h1")
    h.eliminated = True
    h.eliminated_at_round = 1
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.REFUTE, 1.0)
    ]
    result = eliminate([h], {"h1": -1.0}, threshold=-0.5)
    assert result.just_eliminated == []
    assert result.still_alive == []


def test_eliminate_mixed_outcomes() -> None:
    """混合：h1 存活 / h2 淘汰（a 条件） / h3 淘汰（c 条件）"""
    h1 = _hs("h1")
    h1.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0),
    ]
    h2 = _hs("h2")
    h2.judgements_by_round[1] = [
        Judgement("a1", "h2", 1, Stance.REFUTE, 1.0),
    ]
    h3 = _hs("h3")
    h3.judgements_by_round[1] = [
        Judgement("a1", "h3", 1, Stance.UNCERTAIN, 0.5),
    ]
    result = eliminate(
        [h1, h2, h3],
        compute_support_scores([h1, h2, h3], 1),
        threshold=-0.5,
    )
    assert h1.eliminated is False
    assert h2.eliminated is True  # score=-1.0 <= -0.5
    assert h3.eliminated is True  # 无 SUPPORT
    assert [h.id for h in result.still_alive] == ["h1"]
    assert {h.id for h in result.just_eliminated} == {"h2", "h3"}
