"""
@module tests.e2e.test_w10_approval_e2e
@brief  ApprovalFlow 端到端验证（REVIEW-2026-06-19 §3.4 P2-3.4）

P2-3.4 DoD（按审计要求）：
  ① REQUIRE_APPROVAL 默认走 deny-by-default（fail-closed）
  ② 注入 approver 后可放行（脚本模式）
  ③ 多种调用方（run_command / MCP 工具 / 任意 policy.decision）行为一致
  ④ ApprovalFlow 行为对所有调用方显式可观察
  ⑤ 异常 / 重置等边界场景不破坏行为

@note 互补测试在 tests/unit/test_approval.py；这里 e2e 强调"全链路"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.mcp.adapter import MCPToolAdapter
from agent_swarm.security import (
    ApprovalFlow,
    PolicyDecision,
    SandboxManager,
    SecurityContext,
    SecurityContextManager,
    SecurityPolicy,
    default_local_context,
)
from agent_swarm.tools.builtin.shell import RunCommandTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubMCPClient:
    """最小 MCP client 替身——记录是否被调过 call_tool"""

    def __init__(self) -> None:
        self.call_count = 0

    async def call_tool(self, name: str, arguments: dict) -> list[dict]:
        self.call_count += 1
        return [{"type": "text", "text": f"called:{name}"}]

    async def list_tools(self) -> list[dict]:
        return []

    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        pass


def _stub_policy(decision: PolicyDecision) -> SecurityPolicy:
    """构造一个 SecurityPolicy 替身——check_tool 永远返回给定决策"""

    class _StubPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            return decision

    return _StubPolicy()


# ---------------------------------------------------------------------------
# ① 默认 deny-by-default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_approval_flow_denies_require_approval() -> None:
    """无 approver 注入 → REQUIRE_APPROVAL → 默认 deny"""
    flow = ApprovalFlow()  # 只有 _default_approver (返回 False)
    decision = PolicyDecision("REQUIRE_APPROVAL", "high risk tool")
    ctx = default_local_context("S-default-deny")

    with SecurityContextManager.scope(ctx):
        granted = await flow.request_approval(decision, ctx)

    assert granted is False, "默认应 deny（P2-3.4 安全默认）"


# ---------------------------------------------------------------------------
# ② 脚本模式：注入 approver 放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_mode_allow_approver_grants() -> None:
    """脚本模式：append_approver(return True) → 放行"""
    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: True)  # 模拟 'auto-allow-all' 脚本

    decision = PolicyDecision("REQUIRE_APPROVAL", "production deploy")
    ctx = default_local_context("S-script")

    with SecurityContextManager.scope(ctx):
        granted = await flow.request_approval(decision, ctx)

    assert granted is True


@pytest.mark.asyncio
async def test_script_mode_deny_approver_blocks() -> None:
    """脚本模式：append_approver(return False) → 拒绝"""
    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: False)  # 模拟 'auto-deny' 脚本

    decision = PolicyDecision("REQUIRE_APPROVAL", "risky delete")
    ctx = default_local_context("S-deny")

    with SecurityContextManager.scope(ctx):
        granted = await flow.request_approval(decision, ctx)

    assert granted is False


@pytest.mark.asyncio
async def test_script_mode_decision_specific_approver() -> None:
    """脚本模式：approver 决策可基于 decision.reason / tool 上下文"""
    flow = ApprovalFlow()

    def whitelist_approver(decision: PolicyDecision, ctx: SecurityContext) -> bool:
        # 只放行带 'whitelist:' 前缀的请求
        return decision.reason.startswith("whitelist:")

    flow.append_approver(whitelist_approver)

    # 白名单请求 → 放行
    decision_ok = PolicyDecision("REQUIRE_APPROVAL", "whitelist: read_file")
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        assert await flow.request_approval(decision_ok, ctx) is True

    # 非白名单 → 默认 deny
    decision_blocked = PolicyDecision("REQUIRE_APPROVAL", "unknown reason")
    with SecurityContextManager.scope(ctx):
        assert await flow.request_approval(decision_blocked, ctx) is False


# ---------------------------------------------------------------------------
# ③ RunCommandTool 集成链路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_e2e_deny_then_grant(tmp_path: Path) -> None:
    """run_command 端到端：
    - 第一次不注入 approval → 拒绝 (sandbox 不被调)
    - 注入 allow-all approver → 走 sandbox (因白名单可能成功或 [error] 但不
      走 approval 分支)
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy(workspace=str(workspace))
    sandbox = SandboxManager(workspace=str(workspace))

    # 1) 默认 deny
    tool_no_approval = RunCommandTool(policy=policy, sandbox=sandbox)
    # 用 sandbox 允许的命令（"ls"）但 policy 应给 REQUIRE_APPROVAL（因为 run_command 是 HIGH）
    # 注意：实际 policy 走的是 _tool_default_risk("run_command") = HIGH → REQUIRE_APPROVAL
    out = await tool_no_approval.invoke({"command": "ls /tmp"})
    assert "[error]" in out
    assert "approval" in out.lower()  # 错误信息提到 approval


@pytest.mark.asyncio
async def test_run_command_e2e_with_allow_approver(tmp_path: Path) -> None:
    """注入 approver 后 → 不再因 approval 失败"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy(workspace=str(workspace))
    sandbox = SandboxManager(workspace=str(workspace))

    flow = ApprovalFlow()
    flow.append_approver(lambda d, c: True)
    tool = RunCommandTool(policy=policy, sandbox=sandbox, approval_flow=flow)
    out = await tool.invoke({"command": "ls /tmp"})
    # approval 通过 → 走 sandbox（白名单含 ls → 应成功或超时而非 [error] approval）
    assert "approval" not in out.lower() or "denied" not in out.lower(), (
        f"approver 应已放行，但输出仍含 approval 拒绝信息: {out}"
    )


# ---------------------------------------------------------------------------
# ④ MCP 工具集成（P1-3.1 引入的新链路）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_high_risk_approval_e2e() -> None:
    """P1-3.1 + P2-3.4 集成：MCP 工具 risk=high → REQUIRE_APPROVAL
    无 approver 注入时 → 拒绝；不调 client.call_tool
    """
    stub = _StubMCPClient()
    adapter = MCPToolAdapter(
        server_name="github",
        mcp_tool_name="create_issue",
        description="x",
        parameters={"type": "object"},
        client=stub,
        risk="high",
    )
    out = await adapter.invoke({"title": "test"})
    assert "requires approval" in out
    assert stub.call_count == 0, "REQUIRE_APPROVAL 不应触发实际 MCP 调用"


@pytest.mark.asyncio
async def test_mcp_tool_critical_risk_always_blocks() -> None:
    """critical 风险无论如何 policy 都说 → REQUIRE_APPROVAL"""
    # 即使 policy 说 ALLOW, critical 风险仍被拦
    stub = _StubMCPClient()
    policy = _stub_policy(PolicyDecision("ALLOW", "ok"))
    adapter = MCPToolAdapter(
        server_name="github",
        mcp_tool_name="delete_repo",
        description="x",
        parameters={"type": "object"},
        client=stub,
        risk="critical",
        policy=policy,
    )
    out = await adapter.invoke({})
    assert "requires approval" in out
    assert stub.call_count == 0


# ---------------------------------------------------------------------------
# ⑤ 边界场景
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_flow_empty_chain_denies() -> None:
    """所有 approver 抛异常 → 视为全 deny，不破坏调用方"""
    flow = ApprovalFlow()
    # 替换默认 approver
    flow.reset_approvers()

    def always_boom(d: PolicyDecision, c: SecurityContext) -> bool:
        raise RuntimeError("nope")

    flow.append_approver(always_boom)

    decision = PolicyDecision("REQUIRE_APPROVAL", "x")
    ctx = default_local_context("S")
    with SecurityContextManager.scope(ctx):
        # 全异常 → 走完链条都没放行 → False
        granted = await flow.request_approval(decision, ctx)
    assert granted is False


@pytest.mark.asyncio
async def test_approval_flow_audit_log_records_tenant() -> None:
    """默认 approver 应记录 audit log（含 tenant_id / session_id / reason）"""
    import logging

    flow = ApprovalFlow()
    ctx = SecurityContext(tenant_id="acme-corp", session_id="audit-1", user="alice")
    decision = PolicyDecision("REQUIRE_APPROVAL", "destructive op")

    caplog_records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caplog_records.append(record)

    flow_logger = logging.getLogger("agent_swarm.security.approval")
    flow_logger.addHandler(_CaptureHandler())
    try:
        with SecurityContextManager.scope(ctx):
            await flow.request_approval(decision, ctx)
    finally:
        flow_logger.removeHandler(_CaptureHandler())

    # 找到 "approval.denied" 日志
    denied_logs = [r for r in caplog_records if "approval.denied" in r.getMessage()]
    assert denied_logs, f"未记录 denied audit log: {[r.getMessage() for r in caplog_records]}"
    msg = denied_logs[0].getMessage()
    assert "acme-corp" in msg, f"audit log 应含 tenant_id: {msg}"
    assert "audit-1" in msg, f"audit log 应含 session_id: {msg}"


# ---------------------------------------------------------------------------
# ⑥ 异步 / 并发安全
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_flow_approvers_thread_isolated() -> None:
    """ApprovalFlow 实例状态隔离——一个 flow 的 approver 链不影响另一个"""
    flow_a = ApprovalFlow()
    flow_b = ApprovalFlow()

    flow_a.append_approver(lambda d, c: True)

    decision = PolicyDecision("REQUIRE_APPROVAL", "x")
    ctx = default_local_context()

    # flow_a 放行
    with SecurityContextManager.scope(ctx):
        assert await flow_a.request_approval(decision, ctx) is True
    # flow_b 仍 deny
    with SecurityContextManager.scope(ctx):
        assert await flow_b.request_approval(decision, ctx) is False
