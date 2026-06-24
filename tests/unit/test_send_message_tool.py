"""单元测试：send_message 工具"""

from __future__ import annotations

from agent_swarm.core.mailbox import Mailbox
from agent_swarm.tools.builtin.messaging import SendMessageTool


async def test_send_basic() -> None:
    mb = Mailbox()
    tool = SendMessageTool(from_agent="a", mailbox=mb)
    out = await tool.invoke({"to_agent": "b", "content": "hello"})
    assert out.startswith("[ok]")
    assert "sent to b" in out

    inbox = await mb.receive("b")
    assert len(inbox) == 1
    assert inbox[0].from_agent == "a"
    assert inbox[0].content == "hello"
    assert inbox[0].msg_type == "notify"


async def test_send_with_msg_type() -> None:
    mb = Mailbox()
    tool = SendMessageTool(from_agent="a", mailbox=mb)
    await tool.invoke({"to_agent": "b", "content": "why?", "msg_type": "question"})
    inbox = await mb.receive("b")
    assert inbox[0].msg_type == "question"


async def test_send_with_reply_to() -> None:
    mb = Mailbox()
    tool = SendMessageTool(from_agent="a", mailbox=mb)
    await tool.invoke(
        {
            "to_agent": "b",
            "content": "reply",
            "msg_type": "reply",
            "reply_to": "m-original",
        }
    )
    inbox = await mb.receive("b")
    assert inbox[0].reply_to == "m-original"


async def test_send_missing_to_agent() -> None:
    tool = SendMessageTool(from_agent="a", mailbox=Mailbox())
    out = await tool.invoke({"content": "x"})
    assert out.startswith("[error]")
    assert "to_agent" in out


async def test_send_missing_content() -> None:
    tool = SendMessageTool(from_agent="a", mailbox=Mailbox())
    out = await tool.invoke({"to_agent": "b"})
    assert out.startswith("[error]")
    assert "content" in out


async def test_send_to_self_rejected() -> None:
    """LLM 失误给自己发 → 返回 [error]"""
    tool = SendMessageTool(from_agent="a", mailbox=Mailbox())
    out = await tool.invoke({"to_agent": "a", "content": "x"})
    assert out.startswith("[error]")
    assert "yourself" in out


async def test_send_invalid_msg_type() -> None:
    tool = SendMessageTool(from_agent="a", mailbox=Mailbox())
    out = await tool.invoke({"to_agent": "b", "content": "x", "msg_type": "praise"})
    assert out.startswith("[error]")
    assert "msg_type" in out


async def test_send_to_unknown_agent_rejected_by_whitelist() -> None:
    """known_agents 白名单生效——杜撰 id 被拒"""
    mb = Mailbox()
    tool = SendMessageTool(from_agent="a", mailbox=mb, known_agents={"a", "b", "c"})
    out = await tool.invoke({"to_agent": "ghost", "content": "x"})
    assert out.startswith("[error]")
    assert "unknown agent" in out
    # 未发送
    assert await mb.receive("ghost") == []


async def test_send_known_agent_whitelist_passes() -> None:
    mb = Mailbox()
    tool = SendMessageTool(from_agent="a", mailbox=mb, known_agents={"a", "b"})
    out = await tool.invoke({"to_agent": "b", "content": "x"})
    assert out.startswith("[ok]")
