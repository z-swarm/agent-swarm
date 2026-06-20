"""单元测试：ApprovalFlow——DESIGN.md §8.3 最小占位"""

from __future__ import annotations

import sys

import pytest

from agent_swarm.security import (
    ApprovalFlow,
    PolicyDecision,
    SecurityContext,
    SecurityContextManager,
    default_local_context,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: subprocess run_command differs on Windows",
)


def _decision(reason: str = "high risk") -> PolicyDecision:
    return PolicyDecision("REQUIRE_APPROVAL", reason, auto_sandbox=False)


@pytest.mark.asyncio
async def test_default_approver_denies() -> None:
    """@brief 默认 approver 拒绝 + ctx 包含"""
    flow = ApprovalFlow()
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        assert await flow.request_approval(_decision(), ctx) is False


@pytest.mark.asyncio
async def test_explicit_approver_can_grant() -> None:
    """@brief 注入 approver 返回 True → 放行"""
    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: True)  # 全放行
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        assert await flow.request_approval(_decision(), ctx) is True


@pytest.mark.asyncio
async def test_approver_chain_short_circuits_on_grant() -> None:
    """@brief 首个 True approver 即放行, 后续不再调"""
    flow = ApprovalFlow()
    calls: list[int] = []

    def second(d: PolicyDecision, c: SecurityContext) -> bool:
        calls.append(2)
        return True

    flow.append_approver(second)
    flow.append_approver(lambda d, c: (calls.append(3), False)[1])
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        assert await flow.request_approval(_decision(), ctx) is True
    # 第二个 approver 调用了, 第三个因为 short circuit 没调用
    assert calls == [2]


@pytest.mark.asyncio
async def test_approver_exception_treated_as_deny_continues_chain() -> None:
    """@brief approver 抛异常 → 视为 deny, 不阻断后续 approver"""
    flow = ApprovalFlow()

    def boom(d: PolicyDecision, c: SecurityContext) -> bool:
        raise RuntimeError("approver crash")

    flow.append_approver(boom)
    flow.append_approver(lambda d, c: True)
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        # 第一个抛异常, 第二个放行
        assert await flow.request_approval(_decision(), ctx) is True


@pytest.mark.asyncio
async def test_reset_approvers_back_to_default() -> None:
    """@brief reset 后只剩默认 deny"""
    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: True)
    assert await flow.request_approval(_decision(), default_local_context()) is True
    flow.reset_approvers()
    assert await flow.request_approval(_decision(), default_local_context()) is False


@pytest.mark.asyncio
async def test_approver_supports_async() -> None:
    """W11: async approver 也支持（返回 coroutine）"""
    import asyncio

    flow = ApprovalFlow()

    async def async_allow(d, c):
        await asyncio.sleep(0.01)
        return True

    flow.append_approver(async_allow)
    assert await flow.request_approval(_decision(), default_local_context()) is True


@pytest.mark.asyncio
async def test_run_command_high_risk_uses_approval_flow() -> None:
    """
    @brief RunCommandTool 集成: REQUIRE_APPROVAL 走 ApprovalFlow

    验证:
      - 默认 deny-by-default → 工具返回 [error]
      - 注入 allow-all approver → 工具继续执行 (但因 sandbox 白名单仍可能拒)
    """
    from agent_swarm.security import SandboxManager, SecurityPolicy
    from agent_swarm.tools.builtin.shell import RunCommandTool

    policy = SecurityPolicy(workspace="/tmp")
    sb = SandboxManager(workspace="/tmp")
    # 默认 approval_flow = None → 走 deny
    tool = RunCommandTool(policy=policy, sandbox=sb)
    out = await tool.invoke({"command": "rm -rf /tmp/x"})  # HIGH risk + 黑名单命令
    assert "[error]" in out

    # 注入 allow-all approver
    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: True)
    tool2 = RunCommandTool(policy=policy, sandbox=sb, approval_flow=flow)
    out2 = await tool2.invoke({"command": "rm -rf /tmp/x"})
    # policy 在审批前已 DENY (黑名单 rm) → 仍 [error]
    assert "[error]" in out2
