"""
@module agent_swarm.security.approval
@brief  ApprovalFlow——DESIGN.md §8.3 最小占位

W5 阶段（Phase 1）:
  - 默认行为: 拒绝 (deny-by-default) + 写 audit log
  - 支持显式 approver 注册（test 注入用）
  - 留 ChannelAdapter 接入点（Phase 2 占位）

Phase 2+ 扩展:
  - 异步等待 ChannelAdapter 用户回复
  - 超时 → 默认 deny
  - 多级审批链

@note 当前是 fail-closed 模式——REQUIRE_APPROVAL 决策默认拒绝;
      测试 / 显式 approver 注入可允许。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_swarm.security.context import SecurityContext
from agent_swarm.security.policy import PolicyDecision

log = logging.getLogger(__name__)


# Approver 函数签名: 接受 (decision, ctx) → bool
Approver = Callable[[PolicyDecision, SecurityContext], bool]


def _default_approver(decision: PolicyDecision, ctx: SecurityContext) -> bool:
    """@brief 默认 approver: deny-by-default + audit log"""
    log.warning(
        "approval.denied tenant=%s session=%s reason=%s",
        ctx.tenant_id, ctx.session_id, decision.reason,
    )
    return False


class ApprovalFlow:
    """
    @brief 审批流——REQUIRE_APPROVAL 决策的统一入口

    @note 构造时自动注册 _default_approver (deny-by-default)
          测试可显式 append approver 改变行为
    """

    def __init__(self) -> None:
        self._approvers: list[Approver] = [_default_approver]

    def append_approver(self, approver: Approver) -> None:
        """@brief 注册额外 approver——用于测试 / 显式白名单"""
        self._approvers.append(approver)

    def reset_approvers(self) -> None:
        """@brief 清空 approver 链——重置为默认 deny"""
        self._approvers = [_default_approver]

    def request_approval(
        self,
        decision: PolicyDecision,
        ctx: SecurityContext,
    ) -> bool:
        """
        @brief 同步请求审批——任一 approver 返回 True 即放行

        @param decision  SecurityPolicy 返回的 REQUIRE_APPROVAL 决策
        @param ctx       当前 SecurityContext——用于 audit log
        @return True 放行 / False 拒绝
        """
        for approver in self._approvers:
            try:
                if approver(decision, ctx):
                    log.info(
                        "approval.granted tenant=%s session=%s reason=%s",
                        ctx.tenant_id, ctx.session_id, decision.reason,
                    )
                    return True
            except Exception as exc:  # noqa: BLE001
                log.warning("approval.approver_error err=%s", exc)
        return False
