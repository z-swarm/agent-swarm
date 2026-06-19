"""单元测试：security/channel_approver.py——W11 飞书审批 Approver"""

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
from agent_swarm.security.channel_approver import (
    ApprovalRequest,
    ChannelApprover,
)
from agent_swarm.security.context import SecurityContext, default_local_context
from agent_swarm.security.policy import PolicyDecision


# Stub connector that records sent messages
class _StubConnector:
    def __init__(self, ct: ChannelType = ChannelType.LARK) -> None:
        self._ct = ct
        self.sent: list[dict] = []
    @property
    def channel_type(self) -> ChannelType:
        return self._ct
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, response: ChannelResponse, target) -> bool:
        self.sent.append({"response": response, "target": target})
        return True
    def subscribe(self, handler) -> None: ...
    def unsubscribe(self, handler) -> None: ...


def _decision(reason: str = "high risk op") -> PolicyDecision:
    return PolicyDecision("REQUIRE_APPROVAL", reason, auto_sandbox=False)


def _admin() -> ChannelUser:
    return ChannelUser(channel=ChannelType.LARK, user_id="ou_admin", display_name="Admin")


def _ctx() -> SecurityContext:
    return default_local_context("S-test")


@pytest.mark.asyncio
async def test_approver_sends_card_and_waits_for_callback() -> None:
    """正常路径：发送卡片 → 回调 approve → 放行"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    ctx = _ctx()
    # 在后台跑 __call__，等待回调
    async def run_call():
        return await approver(_decision("rm -rf /"), ctx)

    task = asyncio.create_task(run_call())
    # 等卡片被发出去
    await asyncio.sleep(0.05)
    assert len(c.sent) == 1
    assert c.sent[0]["response"].card_template == "confirm_dialog"
    assert c.sent[0]["target"].user_id == "ou_admin"

    # 模拟用户点 Approve
    request_id = approver._inflight[list(approver._inflight.keys())[0]].request_id
    fake_msg = ChannelMessage(
        id="act_1", channel=ChannelType.LARK,
        from_user=_admin(),
        content=json.dumps({"value": {"action": f"approve:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(fake_msg)

    result = await task
    assert result is True


@pytest.mark.asyncio
async def test_approver_deny_callback_blocks() -> None:
    """用户点 Deny → 拒绝"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    async def run_call():
        return await approver(_decision("x"), _ctx())

    task = asyncio.create_task(run_call())
    await asyncio.sleep(0.05)
    request_id = list(approver._inflight.keys())[0]

    fake_msg = ChannelMessage(
        id="act_2", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": f"deny:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(fake_msg)

    result = await task
    assert result is False


@pytest.mark.asyncio
async def test_approver_timeout_returns_false_fail_closed() -> None:
    """超时未回复 → fail-closed（默认 False）"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=0.2)

    start = time.time()
    result = await approver(_decision("x"), _ctx())
    elapsed = time.time() - start

    assert result is False
    assert elapsed < 0.5  # 实际 ~0.2s


@pytest.mark.asyncio
async def test_approver_send_failure_returns_false() -> None:
    """send 失败 → 直接 False（不阻塞）"""
    class _FailingConnector(_StubConnector):
        async def send(self, response, target) -> bool:
            return False
    c = _FailingConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    result = await approver(_decision("x"), _ctx())
    assert result is False
    assert approver.inflight_count == 0  # 没有进入等待


@pytest.mark.asyncio
async def test_approver_invalid_action_content_ignored() -> None:
    """回调 content 非 JSON → 忽略，不抛"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    fake_msg = ChannelMessage(
        id="act_bad", channel=ChannelType.LARK, from_user=_admin(),
        content="not-json", msg_type=MessageType.EVENT,
    )
    # 不应抛
    await approver.handle_card_action(fake_msg)
    assert approver.inflight_count == 0


@pytest.mark.asyncio
async def test_approver_callback_unknown_request_ignored() -> None:
    """回调 request_id 不存在（已超时/被取消）→ 忽略"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    fake_msg = ChannelMessage(
        id="act_ghost", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": "approve:nonexistent-id"}}),
        msg_type=MessageType.EVENT,
    )
    # 不应抛
    await approver.handle_card_action(fake_msg)


@pytest.mark.asyncio
async def test_approver_callback_already_done_ignored() -> None:
    """同一个 request_id 收到两次回调 → 第二次忽略"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0)

    async def run_call():
        return await approver(_decision("x"), _ctx())

    task = asyncio.create_task(run_call())
    await asyncio.sleep(0.05)
    request_id = list(approver._inflight.keys())[0]

    # 第一次
    msg1 = ChannelMessage(
        id="a1", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": f"approve:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(msg1)
    # 第二次（重复）
    msg2 = ChannelMessage(
        id="a2", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": f"deny:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(msg2)

    result = await task
    assert result is True  # 第一次赢


@pytest.mark.asyncio
async def test_approver_cancel_specific_request() -> None:
    """cancel_inflight(request_id) 取消指定请求 → 立即 False"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=10.0)

    async def run_call():
        return await approver(_decision("x"), _ctx())

    task = asyncio.create_task(run_call())
    await asyncio.sleep(0.05)
    request_id = list(approver._inflight.keys())[0]

    n = await approver.cancel_inflight(request_id)
    assert n == 1
    result = await task
    assert result is False
    assert approver.inflight_count == 0


@pytest.mark.asyncio
async def test_approver_cancel_all() -> None:
    """cancel_inflight() 无参数 → 取消所有 in-flight"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=10.0)

    tasks = []
    for i in range(3):
        async def run_one():
            return await approver(_decision(f"op-{i}"), _ctx())
        tasks.append(asyncio.create_task(run_one()))
    await asyncio.sleep(0.1)
    assert approver.inflight_count == 3

    n = await approver.cancel_inflight()
    assert n == 3
    results = await asyncio.gather(*tasks)
    assert all(r is False for r in results)


@pytest.mark.asyncio
async def test_approver_card_data_custom_fn() -> None:
    """card_data_fn 自定义：注入额外字段"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)

    def custom_fn(decision, ctx):
        return {
            "title": f"Custom: {decision.reason}",
            "message": f"tenant={ctx.tenant_id}",
        }

    approver = ChannelApprover(adapter, _admin(), approval_timeout=5.0, card_data_fn=custom_fn)

    async def run_call():
        return await approver(_decision("custom-op"), _ctx())
    task = asyncio.create_task(run_call())
    await asyncio.sleep(0.05)
    # 校验卡片含 custom 内容
    sent_card_data = c.sent[0]["response"].card_data
    assert sent_card_data["title"] == "Custom: custom-op"
    assert "tenant=local" in sent_card_data["message"]

    # 清理
    request_id = list(approver._inflight.keys())[0]
    msg = ChannelMessage(
        id="a", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": f"approve:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(msg)
    await task


@pytest.mark.asyncio
async def test_approver_inflight_count_tracks_correctly() -> None:
    """inflight_count 实时反映 in-flight 数量"""
    c = _StubConnector()
    adapter = ChannelAdapter()
    adapter.register_connector(c)
    approver = ChannelApprover(adapter, _admin(), approval_timeout=10.0)

    assert approver.inflight_count == 0

    async def run_call():
        return await approver(_decision("x"), _ctx())
    task = asyncio.create_task(run_call())
    await asyncio.sleep(0.05)
    assert approver.inflight_count == 1

    request_id = list(approver._inflight.keys())[0]
    msg = ChannelMessage(
        id="a", channel=ChannelType.LARK, from_user=_admin(),
        content=json.dumps({"value": {"action": f"approve:{request_id}"}}),
        msg_type=MessageType.EVENT,
    )
    await approver.handle_card_action(msg)
    await task
    assert approver.inflight_count == 0
