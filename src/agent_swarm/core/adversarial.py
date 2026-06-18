"""
@module agent_swarm.core.adversarial
@brief  W8 对抗式验证协议——DESIGN §6.2

W8 切分：
  W8-2: 单轮算法 gather_round + 淘汰判定 eliminate (本文件)
  W8-3: 收敛判定（4 条优先级）
  W8-4: AdversarialVerifier 协议实现

设计要点：
  - gather_round 接受"judge callable"——不绑死 LLM provider，便于单测
  - 错误兜底：judge 抛异常时该 agent 该轮 stance 计为 UNCERTAIN（DESIGN §6.2.5）
  - 淘汰规则 3 条：score<=threshold / 连续 2 轮 < 0 / 无任何 SUPPORT
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agent_swarm.core.types import (
    Agent,
    HypothesisState,
    Judgement,
    Stance,
    Verdict,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# judge callable 签名：给定 hypothesis + round_no + agent，返回 Judgement
# 用 callable 而不是直接传 LLM provider，让单测用确定性脚本回放
JudgeFn = Callable[["Agent", str, int], Awaitable[Judgement]]


# ---------------------------------------------------------------------------
# W8-2: 单轮算法 + 淘汰判定
# ---------------------------------------------------------------------------


async def gather_round(
    agents: list[Agent],
    hypotheses: list[HypothesisState],
    round_no: int,
    judge_fn: JudgeFn,
) -> list[Judgement]:
    """
    并行让每个 agent 对每个未淘汰假设产出 Judgement（DESIGN §6.2.3 第 2 步）

    @param agents      judge agent 列表（通常 plan_only 角色）
    @param hypotheses  全部假设状态；只对 eliminated=False 的产生 Judgement
    @param round_no    本轮编号（≥1）
    @param judge_fn    judge callable：async (agent, hypothesis_id, round_no) -> Judgement

    @return 全部 agent × 全部存活假设的 Judgement 列表
    @note 错误兜底：judge_fn 抛异常时该 (agent, hypothesis) 组合记为
          stance=UNCERTAIN、confidence=0.0、reasoning=含错误描述；
          不影响其他 agent/hypothesis 的判断（DESIGN §6.2.5）
    """
    targets: list[HypothesisState] = [h for h in hypotheses if not h.eliminated]
    if not targets or not agents:
        return []

    tasks: list[Awaitable[Judgement]] = []
    task_meta: list[tuple[Agent, str]] = []  # 与 tasks 一一对应，便于错误兜底
    for agent in agents:
        for h in targets:
            tasks.append(_safe_judge(judge_fn, agent, h, round_no))
            task_meta.append((agent, h.id))

    raw = await asyncio.gather(*tasks)
    judgements: list[Judgement] = []
    for j, (agent, h_id) in zip(raw, task_meta):
        if j is None:
            # judge_fn 抛异常——兜底为 UNCERTAIN
            judgements.append(Judgement(
                agent_id=agent.id,
                hypothesis_id=h_id,
                round_no=round_no,
                stance=Stance.UNCERTAIN,
                confidence=0.0,
                reasoning=f"judge_fn raised; treated as UNCERTAIN per DESIGN §6.2.5",
            ))
        else:
            judgements.append(j)
    return judgements


async def _safe_judge(
    judge_fn: JudgeFn, agent: Agent, h: HypothesisState, round_no: int,
) -> Judgement | None:
    """调 judge_fn，捕获异常并返回 None（gather_round 兜底为 UNCERTAIN）"""
    try:
        return await judge_fn(agent, h.id, round_no)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "adversarial.gather_round: judge_fn raised for agent=%s hyp=%s: %s",
            agent.id, h.id, exc,
        )
        return None


def attach_judgements(
    hypotheses: list[HypothesisState], judgements: list[Judgement],
) -> None:
    """把 gather_round 产出的 Judgement 按 round_no 挂到对应假设上"""
    for j in judgements:
        for h in hypotheses:
            if h.id == j.hypothesis_id:
                h.judgements_by_round.setdefault(j.round_no, []).append(j)
                break


def compute_support_scores(
    hypotheses: list[HypothesisState], round_no: int,
) -> dict[str, float]:
    """对每个未淘汰假设返回本轮 support_score 字典"""
    return {
        h.id: h.support_score(round_no)
        for h in hypotheses
        if not h.eliminated
    }


@dataclass
class EliminationResult:
    """eliminate() 的返回：存活 + 刚淘汰"""
    still_alive: list[HypothesisState] = field(default_factory=list)
    just_eliminated: list[HypothesisState] = field(default_factory=list)


def eliminate(
    hypotheses: list[HypothesisState],
    scores: dict[str, float],
    threshold: float = -0.5,
) -> EliminationResult:
    """
    淘汰判定（DESIGN §6.2.3 第 4 步 + §6.2.5 兜底）——3 条 OR 关系

    假设被淘汰的任一条件：
      a) support_score(N) <= threshold
      b) 连续 2 轮 score < 0（被持续反驳）
      c) 该轮没有任何 agent 给出 SUPPORT 立场

    @param hypotheses 全部假设（已 attach judgements）
    @param scores     本轮 score 字典（compute_support_scores 输出）
    @param threshold  淘汰分数阈值（默认 -0.5）
    @return EliminationResult(still_alive, just_eliminated)
    @note 不会重复淘汰（h.eliminated=True 的会跳过）
    """
    result = EliminationResult()
    for h in hypotheses:
        if h.eliminated:
            continue

        score = scores.get(h.id, 0.0)

        # 条件 a：当前轮 score 跌破阈值
        cond_a = score <= threshold

        # 条件 b：连续 2 轮 score < 0
        cond_b = _has_two_consecutive_negative(h)

        # 条件 c：本轮没有任何 SUPPORT 立场
        cond_c = _round_has_no_support(h)

        if cond_a or cond_b or cond_c:
            h.eliminated = True
            h.eliminated_at_round = max(h.judgements_by_round.keys(), default=0)
            result.just_eliminated.append(h)
        else:
            result.still_alive.append(h)
    return result


def _has_two_consecutive_negative(h: HypothesisState) -> bool:
    """检查假设 h 是否最近 2 轮 support_score 都 < 0"""
    rounds = sorted(h.judgements_by_round.keys())
    if len(rounds) < 2:
        return False
    last_two = rounds[-2:]
    return all(h.support_score(r) < 0 for r in last_two)


def _round_has_no_support(h: HypothesisState) -> bool:
    """最新一轮没有任何 agent 给出 SUPPORT 立场"""
    rounds = sorted(h.judgements_by_round.keys())
    if not rounds:
        return False
    last_round = rounds[-1]
    judgements = h.judgements_by_round[last_round]
    if not judgements:
        return True  # 该轮无 judgement 也算"无支持"
    return not any(j.stance == Stance.SUPPORT for j in judgements)



# ---------------------------------------------------------------------------
# W8-3: 收敛判定（DESIGN §6.2.4）—— 4 条优先级
# ---------------------------------------------------------------------------


ConvergenceReason = Literal[
    "min_survivors_reached",
    "consensus_stable",
    "max_rounds_exhausted",
    "all_eliminated",
]


@dataclass
class ConvergenceCheck:
    """单次 check_convergence() 的输出：是否收敛 + 原因（如果收敛）"""
    converged: bool
    reason: ConvergenceReason | None = None
    # 调试信息：每条规则的命中情况（供日志 / observability 排查）
    rule_a_hit: bool = False  # len(survivors) <= min_survivors
    rule_b_hit: bool = False  # 连续 2 轮无任何淘汰 + 无任何 agent 改变立场
    rule_c_hit: bool = False  # round_no >= max_rounds
    rule_d_hit: bool = False  # len(survivors) == 0


def check_convergence(
    hypotheses: list[HypothesisState],
    round_no: int,
    min_survivors: int,
    max_rounds: int,
    prev_round_stances: dict[tuple[str, str], Stance] | None = None,
    curr_round_stances: dict[tuple[str, str], Stance] | None = None,
) -> ConvergenceCheck:
    """
    收敛判定（DESIGN §6.2.4，按优先级 1→4）

    规则：
      1. len(survivors) <= min_survivors
      2. 连续 2 轮无任何假设被淘汰 + 无任何 agent 改变立场
         （prev_round_stances vs curr_round_stances；空集合算"未变"）
      3. round_no >= max_rounds
      4. len(survivors) == 0（all_eliminated 兜底）

    @param prev_round_stances  上一轮 (agent_id, hyp_id) -> Stance；可空
    @param curr_round_stances  本轮 (agent_id, hyp_id) -> Stance；可空
    @note 规则 2 需要"对比两轮 stance"——gather_round 返回 judgements 后
          调用方构造这两个字典传入；不耦合 LLM
    """
    survivors = [h for h in hypotheses if not h.eliminated]
    n_survivors = len(survivors)

    # 规则 1：survivors 达到 min
    rule_a = n_survivors <= min_survivors and n_survivors > 0
    # 规则 4：全淘汰（兜底，单独抽出便于日志区分）
    rule_d = n_survivors == 0

    # 规则 2：连续 2 轮无任何假设被淘汰 + 无任何 agent 改变立场
    # "无任何假设被淘汰"——本轮无 just_eliminated
    # "无任何 agent 改变立场"——prev 与 curr 两个 dict 完全相同
    rule_b = False
    if (
        prev_round_stances is not None
        and curr_round_stances is not None
        and prev_round_stances == curr_round_stances
    ):
        rule_b = True

    # 规则 3：max_rounds 截断
    rule_c = round_no >= max_rounds

    if rule_a:
        return ConvergenceCheck(
            converged=True, reason="min_survivors_reached",
            rule_a_hit=rule_a, rule_b_hit=rule_b,
            rule_c_hit=rule_c, rule_d_hit=rule_d,
        )
    if rule_b:
        return ConvergenceCheck(
            converged=True, reason="consensus_stable",
            rule_a_hit=rule_a, rule_b_hit=rule_b,
            rule_c_hit=rule_c, rule_d_hit=rule_d,
        )
    if rule_d:
        return ConvergenceCheck(
            converged=True, reason="all_eliminated",
            rule_a_hit=rule_a, rule_b_hit=rule_b,
            rule_c_hit=rule_c, rule_d_hit=rule_d,
        )
    if rule_c:
        return ConvergenceCheck(
            converged=True, reason="max_rounds_exhausted",
            rule_a_hit=rule_a, rule_b_hit=rule_b,
            rule_c_hit=rule_c, rule_d_hit=rule_d,
        )

    return ConvergenceCheck(
        converged=False, reason=None,
        rule_a_hit=rule_a, rule_b_hit=rule_b,
        rule_c_hit=rule_c, rule_d_hit=rule_d,
    )


__all__ = [
    "ConvergenceCheck",
    "ConvergenceReason",
    "EliminationResult",
    "JudgeFn",
    "attach_judgements",
    "check_convergence",
    "compute_support_scores",
    "eliminate",
    "gather_round",
]
