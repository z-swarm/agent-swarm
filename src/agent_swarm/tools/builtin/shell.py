"""
@module agent_swarm.tools.builtin.shell
@brief  run_command 工具——W5 新增（DESIGN.md §8.2 + §8.3）

@note W5 落地:
  - 必须配合 SecurityPolicy + SandboxManager
  - 风险等级 HIGH → SecurityPolicy 返回 REQUIRE_APPROVAL
  - W5+ 接 ApprovalFlow 实际审批（当前 REQUIRE_APPROVAL 直接返回 [error]）
  - sandbox.execute 真正执行命令
"""

from __future__ import annotations

import logging
from typing import Any

from agent_swarm.security import (
    ApprovalFlow,
    SandboxManager,
    SecurityContextManager,
    SecurityPolicy,
)

log = logging.getLogger(__name__)


class RunCommandTool:
    """
    执行受限 shell 命令——W5 新增

    @note 必须注入 policy + sandbox；否则构造抛 RuntimeError
    @note 强制走 SecurityPolicy → ApprovalFlow → SandboxManager 链路
    @note approval_flow 可选——None 时 REQUIRE_APPROVAL 默认拒绝
    """

    name = "run_command"
    description = (
        "在 sandbox 中执行 shell 命令。只支持白名单命令前缀；"
        "高风险命令必须经审批。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令字符串",
            },
            "timeout": {
                "type": "number",
                "description": "超时（秒），默认 30",
                "default": 30.0,
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        policy: SecurityPolicy,
        sandbox: SandboxManager,
        approval_flow: ApprovalFlow | None = None,
    ) -> None:
        if policy is None:
            raise RuntimeError("RunCommandTool requires SecurityPolicy")
        if sandbox is None:
            raise RuntimeError("RunCommandTool requires SandboxManager")
        self._policy = policy
        self._sandbox = sandbox
        self._approval = approval_flow

    async def invoke(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        if not command or not isinstance(command, str):
            return "[error] missing or invalid 'command'"

        # 1) SecurityPolicy 决策
        decision = self._policy.check_tool("run_command", {"command": command})
        if decision.decision == "DENY":
            return f"[error] policy denied: {decision.reason}"
        if decision.decision == "REQUIRE_APPROVAL":
            # P0-4: 走 ApprovalFlow (默认 deny, 注入 approver 可放行)
            # W11: request_approval 是 async（支持 ChannelApprover 异步等待）
            ctx = SecurityContextManager.current_or_default()
            if self._approval is None or not await self._approval.request_approval(decision, ctx):
                return f"[error] requires approval denied: {decision.reason}"

        # 2) 走 sandbox 实际执行
        timeout = float(arguments.get("timeout", 30.0))
        try:
            result = await self._sandbox.execute(command, timeout=timeout)
        except PermissionError as exc:
            return f"[error] sandbox denied: {exc}"
        except ValueError as exc:
            return f"[error] sandbox error: {exc}"

        # 3) 格式化输出
        if result.timed_out:
            return f"[error] command timed out after {timeout}s"
        out = result.stdout
        if result.stderr:
            out += f"\n[stderr] {result.stderr}"
        if result.exit_code != 0:
            out = f"[exit_code={result.exit_code}] " + out
        if result.truncated:
            out += "\n[output truncated]"
        return out or "[no output]"
