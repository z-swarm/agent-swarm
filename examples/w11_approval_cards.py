"""
@module examples.w11_approval_cards
@brief  W11 飞书卡片审批流 example

启动：
    .venv/bin/python -m agent_swarm.cli.main run \\
        examples/w11_approval_cards.py

前置：
  1. 飞书应用创建好（见 examples/w10_lark.yaml）
  2. 环境变量配置：
       export LARK_APP_SECRET=...
       export LARK_VERIFICATION_TOKEN=...
       export LARK_ENCRYPT_KEY=...
       export OPENAI_API_KEY=...

行为：
  - swarm 启动时通过 ChannelAdapter 注册 LarkConnector
  - ApprovalFlow.append_approver(ChannelApprover(...))
  - 当 agent 触发 run_command（高风险）或 MCP HIGH/CRITICAL 工具时：
    1) SecurityPolicy 返回 REQUIRE_APPROVAL
    2) ChannelApprover 渲染 confirm_dialog 卡片 → 发送给审批人
    3) 等待审批人点 Approve / Deny 按钮（默认 3600s 超时）
    4) 超时 → fail-closed（默认拒绝）
    5) 用户点 Approve → 工具继续执行；Deny → 拒绝

@note W11 落地后：之前默认 deny 的高风险命令现在可以通过飞书卡片审批
      实现 Human-in-the-Loop 完整流程
"""
from __future__ import annotations

import asyncio
import logging

from agent_swarm.channels.adapter import ChannelAdapter
from agent_swarm.channels.base import (
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageType,
)
from agent_swarm.channels.lark import LarkConnector
from agent_swarm.security import ApprovalFlow
from agent_swarm.security.channel_approver import ChannelApprover
from agent_swarm.security.context import default_local_context

log = logging.getLogger(__name__)


# 1) 构造 LarkConnector（带 SecretManager 引用 + 用户白名单）
lark = LarkConnector(
    app_id="cli_xxxxxxxx",
    app_secret="${LARK_APP_SECRET}",  # 生产用 SecretManager 注入
    verification_token="${LARK_VERIFICATION_TOKEN}",
    user_whitelist=["ou_admin_user_id"],
    webhook_host="0.0.0.0",
    webhook_port=8765,
)

# 2) 构造 ChannelAdapter（路由 + 鉴权 + 限流）
adapter = ChannelAdapter(
    messages_per_minute=30,
    sessions_per_hour=10,
    user_whitelist={"ou_admin_user_id"},
)
adapter.register_connector(lark)

# 3) 构造 ChannelApprover（异步审批人）
admin = ChannelUser(
    channel=ChannelType.LARK,
    user_id="ou_admin_user_id",
    display_name="Admin",
)
approver = ChannelApprover(
    adapter=adapter,
    approver_user=admin,
    approval_timeout=3600.0,  # 1 小时超时 → fail-closed
)

# 4) 把 ChannelApprover 注入 ApprovalFlow
flow = ApprovalFlow()
flow.append_approver(approver)

# 5) 桥接：LarkConnector card action 回调 → ChannelApprover.handle_card_action
async def _lark_handler(msg):  # noqa: ANN001
    if msg.msg_type == MessageType.EVENT:
        await approver.handle_card_action(msg)
    return ChannelResponse(content="")


lark.subscribe(_lark_handler)


# 6) 在真实运行中，ApprovalFlow 注入到 RunCommandTool / MCP adapter
# 例如：
#   tool = RunCommandTool(policy=..., sandbox=..., approval_flow=flow)
#   tool = MCPToolAdapter(..., risk="high")
#     (MCPToolAdapter 内置 risk 二次闸门——需 ApprovalFlow 显式注入以升级为卡片审批)
#
# 这里是示例骨架；具体集成见 tests/e2e/test_w11_approval_e2e.py


async def main() -> None:
    """demo 入口：仅启动 LarkConnector + ApprovalFlow；不做实际任务"""
    log.info("starting lark connector + approval flow...")
    await adapter.start_all()
    ctx = default_local_context("demo")
    log.info("connector started, waiting for high-risk tool calls...")
    # 模拟收到一个 REQUIRE_APPROVAL
    from agent_swarm.security.policy import PolicyDecision
    decision = PolicyDecision("REQUIRE_APPROVAL", "demo: production deploy")
    with __import__("agent_swarm.security").security.SecurityContextManager.scope(ctx):
        # 这里会一直等用户回复（超时 1h）
        # granted = await flow.request_approval(decision, ctx)
        # log.info("granted=%s", granted)
        log.info("(skipped: would block until user replies)")


if __name__ == "__main__":
    asyncio.run(main())
