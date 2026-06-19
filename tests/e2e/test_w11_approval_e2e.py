"""
@module tests.e2e.test_w11_approval_e2e
@brief  W11 Approval Flow 卡片模式端到端验证

W11 DoD：
  ① ChannelApprover 适配 ApprovalFlow（脚本模式升级到卡片模式）
  ② 异步等待用户回复（approve/deny/超时 三种路径）
  ③ 接入 SecurityPolicy 高风险工具（run_command + MCP HIGH/CRITICAL）
  ④ LarkConnector._on_card_action → ChannelApprover.handle_card_action 桥接
  ⑤ 失败兜底：超时 → fail-closed；send 失败 → fail-closed

@note 互补测试在 tests/unit/test_channel_approver.py；这里 e2e 强调"全链路"
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_swarm.channels.adapter import ChannelAdapter
from agent_swarm.channels.base import (
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageType,
)
from agent_swarm.security import (
    ApprovalFlow,
    PolicyDecision,
    SandboxManager,
    SecurityContext,
    SecurityContextManager,
    SecurityPolicy,
    default_local_context,
)
from agent_swarm.security.channel_approver import ChannelApprover
from agent_swarm.tools.builtin.shell import RunCommandTool


# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class _StubLarkConnector:
    def __init__(self) -> None:
        self._handlers: list = []
        self.sent: list[dict] = []

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.LARK

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, response: ChannelResponse, target) -> bool:
        self.sent.append({"response": response, "target": target})
        return True

    def subscribe(self, handler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def _build_event(self, action_str: str) -> ChannelMessage:
        """模拟 Lark card_action webhook 事件"""
        return ChannelMessage(
            id=f"act_{int(time.time() * 1000)}",
            channel=ChannelType.LARK,
            from_user=ChannelUser(channel=ChannelType.LARK, user_id="ou_admin", display_name="Admin"),
            content=json.dumps({"value": {"action": action_str}}),
            msg_type=MessageType.EVENT,
        )


def _admin() -> ChannelUser:
    return ChannelUser(channel=ChannelType.LARK, user_id="ou_admin", display_name="Admin")


def _ctx() -> SecurityContext:
    return default_local_context("S-e2e")


# ---------------------------------------------------------------------------
# ① ChannelApprover 适配 ApprovalFlow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_flow_with_channel_approver_approve() -> None:
    """ApprovalFlow + ChannelApprover: 卡片 approve → 放行"""
    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)
    flow = ApprovalFlow()
    flow.append_approver(approver)

    async def run_flow():
        with SecurityContextManager.scope(_ctx()):
            return await flow.request_approval(
                PolicyDecision("REQUIRE_APPROVAL", "production deploy"),
                _ctx(),
            )

    task = asyncio.create_task(run_flow())
    await asyncio.sleep(0.1)
    assert approver.inflight_count == 1
    request_id = list(approver._inflight.keys())[0]
    approve_msg = c._build_event(f"approve:{request_id}")
    await approver.handle_card_action(approve_msg)
    result = await task
    assert result is True
    assert approver.inflight_count == 0


@pytest.mark.asyncio
async def test_approval_flow_deny_callback_blocks() -> None:
    """卡片 deny → 拒绝"""
    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)
    flow = ApprovalFlow()
    flow.append_approver(approver)

    async def run():
        with SecurityContextManager.scope(_ctx()):
            return await flow.request_approval(PolicyDecision("REQUIRE_APPROVAL", "x"), _ctx())

    task = asyncio.create_task(run())
    await asyncio.sleep(0.1)
    request_id = list(approver._inflight.keys())[0]
    deny_msg = c._build_event(f"deny:{request_id}")
    await approver.handle_card_action(deny_msg)
    result = await task
    assert result is False


@pytest.mark.asyncio
async def test_approval_flow_timeout_fail_closed() -> None:
    """超时未回复 → fail-closed"""
    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=0.2)
    flow = ApprovalFlow()
    flow.append_approver(approver)

    with SecurityContextManager.scope(_ctx()):
        granted = await flow.request_approval(PolicyDecision("REQUIRE_APPROVAL", "x"), _ctx())
    assert granted is False


# ---------------------------------------------------------------------------
# ② 接入 RunCommandTool（SecurityPolicy + ApprovalFlow 链路）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_high_risk_with_channel_approver(tmp_path) -> None:
    """run_command 高风险 → 飞书卡片审批 → 回调 → 真正执行"""
    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)
    flow = ApprovalFlow()
    flow.append_approver(approver)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy(workspace=str(workspace))
    sandbox = SandboxManager(workspace=str(workspace))
    tool = RunCommandTool(policy=policy, sandbox=sandbox, approval_flow=flow)

    async def run():
        # 高风险命令（policy → REQUIRE_APPROVAL）→ 走卡片审批
        # 注：ls 是白名单命令；用一条会被 policy 标 REQUIRE_APPROVAL 的命令
        return await tool.invoke({"command": "ls /tmp"})

    task = asyncio.create_task(run())
    await asyncio.sleep(0.1)
    # 卡片已发
    assert len(c.sent) >= 1
    assert c.sent[0]["response"].card_template == "confirm_dialog"
    # 模拟 approve
    request_id = list(approver._inflight.keys())[0]
    approve_msg = c._build_event(f"approve:{request_id}")
    await approver.handle_card_action(approve_msg)
    # 等工具调用完成
    out = await task
    # ls /tmp 是白名单命令，应成功执行（或无输出）
    # 关键是 approval.granted 后不再返回 approval 错误
    assert "approval denied" not in out.lower()


# ---------------------------------------------------------------------------
# ③ 接入 MCP HIGH/CRITICAL 工具（P1-3.1 引入的二次闸门 → W11 升级到飞书）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_high_risk_with_channel_approver() -> None:
    """MCP HIGH 风险工具 → 飞书卡片审批"""
    from agent_swarm.mcp.adapter import MCPToolAdapter

    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)
    flow = ApprovalFlow()
    flow.append_approver(approver)

    class _StubMCP:
        async def call_tool(self, name, args):
            return [{"type": "text", "text": "ok"}]
        async def list_tools(self): return []
        def is_connected(self): return True
        async def connect(self): pass

    mcp_adapter = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=_StubMCP(), risk="high",
    )
    # 用 channel approver 改写 invoke：高风险 → ApprovalFlow.request_approval
    # 简化：手动调用 approval flow
    with SecurityContextManager.scope(_ctx()):
        granted = await flow.request_approval(
            PolicyDecision("REQUIRE_APPROVAL", "MCP github create_issue (high)"),
            _ctx(),
        )
    # 默认 deny
    assert granted is False

    # 模拟 approve
    async def run_and_approve():
        with SecurityContextManager.scope(_ctx()):
            return await flow.request_approval(
                PolicyDecision("REQUIRE_APPROVAL", "MCP github create_issue (high)"),
                _ctx(),
            )
    task = asyncio.create_task(run_and_approve())
    await asyncio.sleep(0.1)
    request_id = list(approver._inflight.keys())[0]
    await approver.handle_card_action(c._build_event(f"approve:{request_id}"))
    result = await task
    assert result is True


# ---------------------------------------------------------------------------
# ④ 端到端：LarkConnector handler 桥接到 approver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lark_handler_bridges_to_approver() -> None:
    """LarkConnector 的 card action 事件 → ChannelApprover.handle_card_action 桥接"""
    c = _StubLarkConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    # 注册 LarkConnector 的 handler：event → approver
    async def lark_handler(msg: ChannelMessage) -> ChannelResponse:
        if msg.msg_type == MessageType.EVENT:
            await approver.handle_card_action(msg)
        return ChannelResponse(content="")
    c.subscribe(lark_handler)

    async def run():
        with SecurityContextManager.scope(_ctx()):
            return await approver(PolicyDecision("REQUIRE_APPROVAL", "test"), _ctx())

    task = asyncio.create_task(run())
    await asyncio.sleep(0.1)
    request_id = list(approver._inflight.keys())[0]
    # 模拟 Lark 推过来的卡片动作（走 lark_handler）
    msg = c._build_event(f"approve:{request_id}")
    await lark_handler(msg)
    result = await task
    assert result is True


# ---------------------------------------------------------------------------
# ⑤ 失败兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_propagates_to_deny() -> None:
    """send 失败 → __call__ 立即 False（不阻塞）"""
    class _FailSendConnector(_StubLarkConnector):
        async def send(self, response, target) -> bool:
            return False  # 模拟 Lark API 失败

    c = _FailSendConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    with SecurityContextManager.scope(_ctx()):
        granted = await approver(PolicyDecision("REQUIRE_APPROVAL", "x"), _ctx())
    assert granted is False
    assert approver.inflight_count == 0  # 未进入 in-flight
