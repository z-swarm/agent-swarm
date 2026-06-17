"""
@module agent_swarm.tools
@brief  工具注册表——W2 含 read_file + send_message

设计说明:
  - read_file 全局共享（无 per-agent 状态）
  - send_message 必须 per-agent（持有 from_agent）—— build_per_agent_tools() 返回
"""

from __future__ import annotations

from pathlib import Path

from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.types import Tool
from agent_swarm.tools.builtin.file_ops import ReadFileTool
from agent_swarm.tools.builtin.messaging import SendMessageTool
from agent_swarm.tools.builtin.shell import RunCommandTool


def build_shared_tools(workspace: Path | str | None = None) -> dict[str, Tool]:
    """
    构造无 per-agent 状态的共享工具集

    @param workspace read_file 的工作目录限制（W5 改由 SecurityContext 注入）
    """
    rf = ReadFileTool(workspace=workspace)
    return {rf.name: rf}


def build_per_agent_tools(
    agent_id: str,
    mailbox: Mailbox,
    known_agents: set[str] | None = None,
) -> dict[str, Tool]:
    """
    构造 per-agent 工具集——目前仅 send_message

    @param agent_id     持有此工具的 agent id（注入 from_agent）
    @param mailbox      共享 Mailbox 实例
    @param known_agents 已知 agent 白名单——防 LLM 杜撰 to_agent
    """
    sm = SendMessageTool(
        from_agent=agent_id,
        mailbox=mailbox,
        known_agents=known_agents,
    )
    return {sm.name: sm}


# W1 兼容别名——保持向后兼容（test_w1_hello 仍引用）
__all__ = [
    "ReadFileTool",
    "RunCommandTool",
    "SendMessageTool",
    "build_per_agent_tools",
    "build_shared_tools",
]
