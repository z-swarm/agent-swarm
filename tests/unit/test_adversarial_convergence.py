"""单元测试：adversarial 收敛判定（W8-3 / DESIGN §6.2.4）"""

from __future__ import annotations

from agent_swarm.core.adversarial import check_convergence
from agent_swarm.core.types import HypothesisState, Stance


def _hs(id: str, eliminated: bool = False) -> HypothesisState:
    h = HypothesisState(id=id, statement=id)
    h.eliminated = eliminated
    return h


def test_rule_a_min_survivors_reached() -> None:
    """规则 1：survivors <= min_survivors（>0）→ min_survivors_reached"""
    h1 = _hs("h1")
    h2 = _hs("h2", eliminated=True)
    c = check_convergence([h1, h2], round_no=2, min_survivors=1, max_rounds=5)
    assert c.converged is True
    assert c.reason == "min_survivors_reached"
    assert c.rule_a_hit is True


def test_rule_a_min_survivors_zero_not_triggered_here() -> None:
    """survivors=0 时规则 1 不触发（由规则 4 接管，避免误判 min=0）"""
    h1 = _hs("h1", eliminated=True)
    c = check_convergence([h1], round_no=1, min_survivors=1, max_rounds=5)
    assert c.converged is True
    assert c.reason == "all_eliminated"  # 规则 4 兜底
    assert c.rule_d_hit is True


def test_rule_b_consensus_stable() -> None:
    """规则 2：连续 2 轮 stance 字典完全相同 + 无淘汰 → consensus_stable"""
    h1 = _hs("h1")
    h2 = _hs("h2")
    stances = {("a1", "h1"): Stance.SUPPORT, ("a1", "h2"): Stance.REFUTE}
    c = check_convergence(
        [h1, h2],
        round_no=2,
        min_survivors=1,
        max_rounds=5,
        prev_round_stances=stances,
        curr_round_stances=stances,
    )
    assert c.converged is True
    assert c.reason == "consensus_stable"
    assert c.rule_b_hit is True


def test_rule_b_not_triggered_when_stance_changed() -> None:
    """立场改变 → 规则 2 不触发"""
    h1 = _hs("h1")
    prev = {("a1", "h1"): Stance.SUPPORT}
    curr = {("a1", "h1"): Stance.REFUTE}
    c = check_convergence(
        [h1],
        round_no=2,
        min_survivors=1,
        max_rounds=5,
        prev_round_stances=prev,
        curr_round_stances=curr,
    )
    assert c.rule_b_hit is False


def test_rule_b_skipped_when_no_stance_dicts() -> None:
    """未传 stance 字典 → 规则 2 不评估（视为信息不足）"""
    h1 = _hs("h1")
    c = check_convergence([h1], round_no=1, min_survivors=0, max_rounds=5)
    assert c.rule_b_hit is False


def test_rule_c_max_rounds_exhausted() -> None:
    """规则 3：round_no >= max_rounds → max_rounds_exhausted"""
    h1 = _hs("h1")
    h2 = _hs("h2")
    c = check_convergence([h1, h2], round_no=5, min_survivors=1, max_rounds=5)
    assert c.converged is True
    assert c.reason == "max_rounds_exhausted"
    assert c.rule_c_hit is True


def test_rule_d_all_eliminated() -> None:
    """规则 4：survivors=0 → all_eliminated 兜底"""
    h1 = _hs("h1", eliminated=True)
    h2 = _hs("h2", eliminated=True)
    c = check_convergence([h1, h2], round_no=1, min_survivors=1, max_rounds=5)
    assert c.converged is True
    assert c.reason == "all_eliminated"
    assert c.rule_d_hit is True


def test_no_convergence() -> None:
    """未达任何规则 → converged=False"""
    h1 = _hs("h1")
    h2 = _hs("h2")
    h3 = _hs("h3")
    c = check_convergence([h1, h2, h3], round_no=1, min_survivors=1, max_rounds=5)
    assert c.converged is False
    assert c.reason is None


def test_priority_min_survivors_beats_max_rounds() -> None:
    """规则 1 优先于规则 3：survivors 达 min + 达到 max → reason=min_survivors_reached"""
    h1 = _hs("h1")
    c = check_convergence([h1], round_no=5, min_survivors=1, max_rounds=5)
    assert c.reason == "min_survivors_reached"  # 优先


def test_priority_all_eliminated_beats_max_rounds() -> None:
    """规则 4 优先于规则 3：全淘汰 + 达到 max → reason=all_eliminated"""
    h1 = _hs("h1", eliminated=True)
    c = check_convergence([h1], round_no=5, min_survivors=1, max_rounds=5)
    assert c.reason == "all_eliminated"
