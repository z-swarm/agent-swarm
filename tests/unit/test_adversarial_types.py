"""单元测试：Adversarial Verify 数据结构（W8-1 / DESIGN §6.2.2）"""

from __future__ import annotations

import pytest

from agent_swarm.core.types import (
    HypothesisState,
    Judgement,
    Stance,
    Verdict,
)


# ---------------------------------------------------------------------------
# Stance
# ---------------------------------------------------------------------------


def test_stance_has_three_values() -> None:
    """Stance 是 3 值 enum：SUPPORT / REFUTE / UNCERTAIN"""
    assert {s.name for s in Stance} == {"SUPPORT", "REFUTE", "UNCERTAIN"}
    assert Stance.SUPPORT.value == "support"
    assert Stance.REFUTE.value == "refute"
    assert Stance.UNCERTAIN.value == "uncertain"


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------


def test_judgement_minimal_construction() -> None:
    """Judgement 必填字段：agent_id / hypothesis_id / round_no / stance / confidence"""
    j = Judgement(
        agent_id="a1",
        hypothesis_id="h1",
        round_no=1,
        stance=Stance.SUPPORT,
        confidence=0.8,
    )
    assert j.agent_id == "a1"
    assert j.hypothesis_id == "h1"
    assert j.round_no == 1
    assert j.stance == Stance.SUPPORT
    assert j.confidence == 0.8
    assert j.evidence == []  # 默认空
    assert j.reasoning == ""  # 默认空


# ---------------------------------------------------------------------------
# HypothesisState.support_score
# ---------------------------------------------------------------------------


def _hs() -> HypothesisState:
    return HypothesisState(id="h1", statement="root cause X")


def test_hypothesis_state_default_not_eliminated() -> None:
    """新建假设默认 eliminated=False"""
    h = _hs()
    assert h.eliminated is False
    assert h.eliminated_at_round is None
    assert h.judgements_by_round == {}


def test_support_score_empty_round_returns_zero() -> None:
    """某轮无 Judgement → support_score = 0.0"""
    h = _hs()
    assert h.support_score(1) == 0.0
    assert h.support_score(999) == 0.0  # 任何不存在的轮都返 0


def test_support_score_all_support_returns_one() -> None:
    """全 SUPPORT + confidence=1.0 → score=1.0"""
    h = _hs()
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0),
        Judgement("a2", "h1", 1, Stance.SUPPORT, 1.0),
    ]
    assert h.support_score(1) == pytest.approx(1.0)


def test_support_score_all_refute_returns_minus_one() -> None:
    """全 REFUTE + confidence=1.0 → score=-1.0"""
    h = _hs()
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.REFUTE, 1.0),
        Judgement("a2", "h1", 1, Stance.REFUTE, 1.0),
    ]
    assert h.support_score(1) == pytest.approx(-1.0)


def test_support_score_mixed_stances_weighted_by_confidence() -> None:
    """混合 stance + 不同 confidence → 加权平均"""
    h = _hs()
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0),    # +1
        Judgement("a2", "h1", 1, Stance.REFUTE, 0.5),     # -0.5
        Judgement("a3", "h1", 1, Stance.UNCERTAIN, 0.9),  # 0
    ]
    # (1.0 + -0.5 + 0) / 3 = 0.1666...
    assert h.support_score(1) == pytest.approx(1.0 / 6.0, rel=1e-3)


def test_support_score_isolated_per_round() -> None:
    """不同轮的 judgements_by_round 互不影响"""
    h = _hs()
    h.judgements_by_round[1] = [
        Judgement("a1", "h1", 1, Stance.SUPPORT, 1.0),
    ]
    h.judgements_by_round[2] = [
        Judgement("a1", "h1", 2, Stance.REFUTE, 1.0),
    ]
    assert h.support_score(1) == pytest.approx(1.0)
    assert h.support_score(2) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def test_verdict_minimal_construction() -> None:
    """Verdict 必填字段：survivors / eliminated / rounds_used / convergence_reason"""
    v = Verdict(
        survivors=[_hs()],
        eliminated=[],
        rounds_used=3,
        convergence_reason="consensus_stable",
    )
    assert v.survivors == [_hs()] or len(v.survivors) == 1
    assert v.eliminated == []
    assert v.rounds_used == 3
    assert v.convergence_reason == "consensus_stable"
    assert v.root_cause is None
    assert v.confidence == 0.0
    assert v.full_history == []


def test_verdict_with_root_cause() -> None:
    """verdict.root_cause 在 survivors=1 时承载结论（DESIGN §6.2.5）"""
    h = _hs()
    v = Verdict(
        survivors=[h],
        eliminated=[],
        rounds_used=2,
        convergence_reason="min_survivors_reached",
        root_cause=h.statement,
        confidence=0.92,
    )
    assert v.root_cause == "root cause X"
    assert v.confidence == pytest.approx(0.92)


def test_verdict_convergence_reason_literal_values() -> None:
    """convergence_reason 4 个合法值（DESIGN §6.2.2）"""
    legal = {
        "min_survivors_reached",
        "consensus_stable",
        "max_rounds_exhausted",
        "all_eliminated",
    }
    for reason in legal:
        v = Verdict(
            survivors=[],
            eliminated=[],
            rounds_used=1,
            convergence_reason=reason,  # type: ignore[arg-type]
        )
        assert v.convergence_reason == reason
