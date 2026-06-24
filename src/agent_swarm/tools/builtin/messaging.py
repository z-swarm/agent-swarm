"""
@module agent_swarm.tools.builtin.messaging
@brief  send_message 工具——agent 通过 Mailbox 给其他 agent 发消息

DESIGN.md §6.5 / §14 使用示例。W2 引入。

@note 工具持有 from_agent 和 mailbox 引用——构造时由 AgentRunner 注入
      避免每个 agent 共享同一 send_message 实例后串了 from_agent
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast, get_args

from agent_swarm.core.mailbox import Mailbox

log = logging.getLogger(__name__)

# msg_type 合法值——与 types.Message.msg_type Literal 保持一致
_MsgType = Literal["question", "challenge", "reply", "notify", "delegate"]
_MSG_TYPES: tuple[str, ...] = get_args(_MsgType)


class SendMessageTool:
    """
    给指定 agent 发送一条消息——LLM 友好接口

    @note 每个 agent 应持有自己的 SendMessageTool 实例（from_agent 不同）
    """

    name = "send_message"
    description = (
        "向团队中的另一个 agent 发送消息。"
        "用于提问、回复、委托或通知。"
        "参数 to_agent 必须是已知 agent id；msg_type 表达消息意图。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to_agent": {
                "type": "string",
                "description": "接收者 agent id",
            },
            "content": {
                "type": "string",
                "description": "消息正文",
            },
            "msg_type": {
                "type": "string",
                "enum": list(_MSG_TYPES),
                "description": (
                    "消息类型: question=提问 / challenge=质疑 / "
                    "reply=回复 / notify=通知 / delegate=委派任务"
                ),
                "default": "notify",
            },
            "reply_to": {
                "type": "string",
                "description": "若是对某条消息的回复，填其 message id（可选）",
            },
        },
        "required": ["to_agent", "content"],
    }

    def __init__(
        self,
        from_agent: str,
        mailbox: Mailbox,
        known_agents: set[str] | None = None,
    ) -> None:
        """
        @param from_agent   发送方——固定为持有此工具的 agent id
        @param mailbox      共享的 Mailbox 实例
        @param known_agents 可选——已知 agent 白名单，防止 LLM 杜撰 to_agent
        """
        self.from_agent = from_agent
        self.mailbox = mailbox
        self.known_agents = known_agents

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """执行——返回 LLM 友好的字符串结果"""
        to_agent = arguments.get("to_agent")
        content = arguments.get("content")
        msg_type_raw = arguments.get("msg_type", "notify")
        reply_to = arguments.get("reply_to")

        if not to_agent or not isinstance(to_agent, str):
            return "[error] missing or invalid 'to_agent'"
        if not content or not isinstance(content, str):
            return "[error] missing or invalid 'content'"
        if to_agent == self.from_agent:
            return "[error] cannot send message to yourself"

        if self.known_agents is not None and to_agent not in self.known_agents:
            return f"[error] unknown agent {to_agent!r}. Known agents: {sorted(self.known_agents)}"

        if msg_type_raw not in _MSG_TYPES:
            return f"[error] invalid msg_type {msg_type_raw!r}; allowed: {list(_MSG_TYPES)}"
        msg_type = cast(_MsgType, msg_type_raw)

        msg = Mailbox.make_message(
            from_agent=self.from_agent,
            to_agent=to_agent,
            content=content,
            msg_type=msg_type,
            reply_to=reply_to if isinstance(reply_to, str) else None,
        )
        await self.mailbox.send(msg)
        return f"[ok] message {msg.id} sent to {to_agent}"
