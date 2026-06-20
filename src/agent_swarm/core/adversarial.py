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

from agent_swarm.core.protocols import CollaborationProtocol, ProtocolResult
from agent_swarm.core.types import (
    Agent,
    HypothesisState,
    Judgement,
    Stance,
    Verdict,
)

if TYPE_CHECKING:
    from agent_swarm.core.swarm import Swarm  # 仅类型注解用

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

    # _safe_judge 异常时返回 None——必须把 None 写进 Awaitable 类型参数
    tasks: list[Awaitable[Judgement | None]] = []
    task_meta: list[tuple[Agent, str]] = []  # 与 tasks 一一对应，便于错误兜底
    for agent in agents:
        for h in targets:
            tasks.append(_safe_judge(judge_fn, agent, h, round_no))
            task_meta.append((agent, h.id))

    raw = await asyncio.gather(*tasks)
    judgements: list[Judgement] = []
    for j, (agent, h_id) in zip(raw, task_meta, strict=False):
        if j is None:
            # judge_fn 抛异常——兜底为 UNCERTAIN
            judgements.append(Judgement(
                agent_id=agent.id,
                hypothesis_id=h_id,
                round_no=round_no,
                stance=Stance.UNCERTAIN,
                confidence=0.0,
                reasoning="judge_fn raised; treated as UNCERTAIN per DESIGN §6.2.5",
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



# ---------------------------------------------------------------------------
# W8-4: AdversarialVerifier 协议实现
# ---------------------------------------------------------------------------


class VerifierStallError(RuntimeError):
    """连续 2 轮所有 agent 都失败 → 整个验证 stall（DESIGN §6.2.5）"""


class AdversarialVerifier(CollaborationProtocol):
    """
    对抗式验证协议——DESIGN §6.2

    @param min_survivors       目标存活假设数（默认 1）
    @param max_rounds          最大轮数（默认 5）
    @param eliminate_threshold 淘汰分数阈值（默认 -0.5）
    @param per_round_timeout   单轮 LLM 调用超时（DESIGN §6.2.6，默认 120s）
    """

    def __init__(
        self,
        min_survivors: int = 1,
        max_rounds: int = 5,
        eliminate_threshold: float = -0.5,
        per_round_timeout: float = 120.0,
    ) -> None:
        if min_survivors < 0:
            raise ValueError(f"min_survivors must be >= 0, got {min_survivors}")
        if max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
        self._min_survivors = min_survivors
        self._max_rounds = max_rounds
        self._eliminate_threshold = eliminate_threshold
        self._per_round_timeout = per_round_timeout
        self._rounds_run: int = 0
        self._all_failed_streak: int = 0

    async def verify(
        self,
        hypotheses: list[str],
        agents: list[Agent],
        judge_fn: JudgeFn | None = None,
    ) -> Verdict:
        """跑一轮对抗式验证（DESIGN §6.2.6）"""
        if not hypotheses:
            raise ValueError("hypotheses must be non-empty")
        if not agents:
            raise ValueError("agents must be non-empty")

        states: list[HypothesisState] = [
            HypothesisState(id=f"h{i}", statement=stmt)
            for i, stmt in enumerate(hypotheses)
        ]
        history: list[Judgement] = []
        _judge_fn = judge_fn or _default_judge_fn
        prev_stances: dict[tuple[str, str], Stance] = {}

        for round_no in range(1, self._max_rounds + 1):
            self._rounds_run = round_no
            judgements = await gather_round(agents, states, round_no, _judge_fn)
            attach_judgements(states, judgements)
            history.extend(judgements)

            all_failed = self._round_all_failed(states, round_no)
            if all_failed:
                self._all_failed_streak += 1
                if self._all_failed_streak >= 2:
                    raise VerifierStallError(
                        f"AdversarialVerifier stalled: round {round_no} "
                        f"(consecutive {self._all_failed_streak} all-failed rounds)"
                    )
                _rollback_round(states, round_no)
                history = [j for j in history if j.round_no != round_no]
                log.warning(
                    "adversarial.round %d: all agents failed; rolling back (streak=%d)",
                    round_no, self._all_failed_streak,
                )
                continue
            self._all_failed_streak = 0

            scores = compute_support_scores(states, round_no)
            result = eliminate(states, scores, self._eliminate_threshold)
            log.debug(
                "adversarial.round %d: alive=%s, eliminated=%s",
                round_no,
                [h.id for h in result.still_alive],
                [h.id for h in result.just_eliminated],
            )

            curr_stances = _build_stance_dict(states, round_no)
            cc = check_convergence(
                states, round_no,
                min_survivors=self._min_survivors,
                max_rounds=self._max_rounds,
                prev_round_stances=prev_stances,
                curr_round_stances=curr_stances,
            )
            if cc.converged:
                return self._build_verdict(
                    states, history, rounds_used=round_no,
                    convergence_reason=cc.reason or "max_rounds_exhausted",
                )
            prev_stances = curr_stances

        return self._build_verdict(
            states, history, rounds_used=self._max_rounds,
            convergence_reason="max_rounds_exhausted",
        )

    async def execute(self, swarm: Swarm) -> ProtocolResult:
        """
        按协议驱动 swarm 跑对抗式验证

        @param swarm 假设从 swarm.tasks[*].title 收集；judge agents 优先选
                     plan_only 角色（capabilities.can_execute_actions=False），
                     缺则退化用所有 agent。
        @note W8 骨架：假设/agent 提取用最简规则；W8-5 Golden Case 时细化。
        """

        hypotheses = [t.title for t in swarm.tasks if t.title]
        judges = [
            a for a in swarm.agents
            if not a.capabilities.can_execute_actions
        ]
        if not judges:
            judges = list(swarm.agents)
        try:
            verdict = await self.verify(hypotheses, judges)
        except VerifierStallError as exc:
            return ProtocolResult(
                success=False,
                error=f"VerifierStallError: {exc}",
                artifacts={"protocol": "AdversarialVerifier"},
            )

        success = bool(verdict.survivors) and verdict.convergence_reason != "all_eliminated"
        return ProtocolResult(
            success=success,
            summary=(
                f"AdversarialVerifier: {len(verdict.survivors)} survivor(s) "
                f"after {verdict.rounds_used} round(s) "
                f"(reason={verdict.convergence_reason})"
            ),
            artifacts={
                "protocol": "AdversarialVerifier",
                "survivors": [h.id for h in verdict.survivors],
                "eliminated": [h.id for h in verdict.eliminated],
                "rounds_used": verdict.rounds_used,
                "convergence_reason": verdict.convergence_reason,
                "root_cause": verdict.root_cause,
                "confidence": verdict.confidence,
            },
        )

    @staticmethod
    def _round_all_failed(states: list[HypothesisState], round_no: int) -> bool:
        """本轮所有 (agent, hyp) 都 UNCERTAIN 视作全员失败

        H2 fix：当本轮已无存活假设（gather_round 跳过所有 eliminated），
        judgements_by_round 全部为空——这不算"全员失败"，而是
        "无需 judge"——直接 return False 避免误触发 stall。
        """
        # H2: 无存活假设 → 视作"无需 judge"，不算全员失败
        if not any(not h.eliminated for h in states):
            return False
        for h in states:
            if h.eliminated:
                continue
            js = h.judgements_by_round.get(round_no, [])
            if not js:
                return True
            if not all(j.stance == Stance.UNCERTAIN for j in js):
                return False
        return True

    @staticmethod
    def _build_verdict(
        states: list[HypothesisState],
        history: list[Judgement],
        rounds_used: int,
        convergence_reason: ConvergenceReason,
    ) -> Verdict:
        """构造 Verdict——survivors 按最后一轮 support_score 降序排"""
        survivors = [h for h in states if not h.eliminated]
        eliminated = [h for h in states if h.eliminated]

        def _sort_key(h: HypothesisState) -> float:
            rounds = sorted(h.judgements_by_round.keys())
            if not rounds:
                return float("-inf")
            return h.support_score(rounds[-1])
        survivors.sort(key=_sort_key, reverse=True)

        root_cause: str | None = None
        confidence = 0.0
        if convergence_reason == "all_eliminated":
            if eliminated:
                latest = max(
                    eliminated,
                    key=lambda h: h.eliminated_at_round or 0,
                )
                root_cause = (
                    f"all hypotheses eliminated; weak recommendation: {latest.statement}"
                )
                confidence = 0.1
        elif len(survivors) == 1:
            root_cause = survivors[0].statement
            last_round = max(survivors[0].judgements_by_round.keys())
            supports = [
                j.confidence for j in survivors[0].judgements_by_round[last_round]
                if j.stance == Stance.SUPPORT
            ]
            confidence = sum(supports) / len(supports) if supports else 0.0

        return Verdict(
            survivors=survivors,
            eliminated=eliminated,
            rounds_used=rounds_used,
            convergence_reason=convergence_reason,
            root_cause=root_cause,
            confidence=confidence,
            full_history=history,
        )


def _build_stance_dict(
    states: list[HypothesisState], round_no: int,
) -> dict[tuple[str, str], Stance]:
    """构造 (agent_id, hyp_id) -> Stance 字典（供 consensus_stable 判定）"""
    out: dict[tuple[str, str], Stance] = {}
    for h in states:
        for j in h.judgements_by_round.get(round_no, []):
            out[(j.agent_id, j.hypothesis_id)] = j.stance
    return out


def _rollback_round(states: list[HypothesisState], round_no: int) -> None:
    """该轮作废时把 judgements_by_round[round_no] 清掉"""
    for h in states:
        h.judgements_by_round.pop(round_no, None)


async def _default_judge_fn(
    agent: Agent, hypothesis_id: str, round_no: int,
) -> Judgement:
    """
    默认 judge_fn——若用户未注入则抛错（强制 caller 接真 LLM）

    @note W8 骨架：默认空实现防止"忘了传 judge_fn 静默跑通"；W8-5 Golden
          Case 时用 FakeLLMProvider 脚本构造确定性 judge_fn。
    """
    raise NotImplementedError(
        "AdversarialVerifier.verify() requires judge_fn; "
        "W8 骨架不绑死 LLM provider，调用方需注入。"
    )


__all__ = [
    "AdversarialVerifier",
    "ConvergenceCheck",
    "ConvergenceReason",
    "EliminationResult",
    "JudgeFn",
    "VerifierStallError",
    "attach_judgements",
    "check_convergence",
    "compute_support_scores",
    "eliminate",
    "gather_round",
]
