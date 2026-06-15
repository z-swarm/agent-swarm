"""单元测试：Mailbox 内存实现"""

from __future__ import annotations

import asyncio

import pytest

from agent_swarm.core.mailbox import Mailbox

# ---------------------------------------------------------------------------
# 基础发送/接收
# ---------------------------------------------------------------------------


async def test_send_then_receive() -> None:
    mb = Mailbox()
    msg = Mailbox.make_message(from_agent="a", to_agent="b", content="hi")
    await mb.send(msg)
    inbox = await mb.receive("b")
    assert len(inbox) == 1
    assert inbox[0].content == "hi"
    assert inbox[0].timestamp > 0


async def test_receive_for_unknown_agent_returns_empty() -> None:
    mb = Mailbox()
    assert await mb.receive("nobody") == []


async def test_make_message_assigns_id_and_timestamp() -> None:
    msg = Mailbox.make_message(from_agent="a", to_agent="b", content="x")
    assert msg.id.startswith("m-")
    assert msg.from_agent == "a"
    assert msg.to_agent == "b"
    assert msg.target_type == "internal"
    assert msg.msg_type == "notify"


async def test_send_broadcast_not_supported() -> None:
    mb = Mailbox()
    msg = Mailbox.make_message(from_agent="a", to_agent="b", content="x")
    msg.to_agent = None  # 模拟广播
    with pytest.raises(ValueError, match="broadcast"):
        await mb.send(msg)


# ---------------------------------------------------------------------------
# unread_only / mark_read
# ---------------------------------------------------------------------------


async def test_unread_only_filter() -> None:
    mb = Mailbox()
    m1 = Mailbox.make_message("a", "b", "first")
    m2 = Mailbox.make_message("a", "b", "second")
    await mb.send(m1)
    await mb.send(m2)

    # 默认 unread_only=True 返回两条
    assert len(await mb.receive("b")) == 2

    # 标记 m1 已读
    n = await mb.mark_read("b", [m1.id])
    assert n == 1

    # unread_only=True → 只剩 m2
    unread = await mb.receive("b", unread_only=True)
    assert [m.content for m in unread] == ["second"]

    # unread_only=False → 仍然两条
    all_msgs = await mb.receive("b", unread_only=False)
    assert len(all_msgs) == 2


async def test_mark_read_idempotent() -> None:
    mb = Mailbox()
    m = Mailbox.make_message("a", "b", "x")
    await mb.send(m)
    assert await mb.mark_read("b", [m.id]) == 1
    # 第二次：已经读过，count=0
    assert await mb.mark_read("b", [m.id]) == 0


async def test_msg_type_filter() -> None:
    mb = Mailbox()
    await mb.send(Mailbox.make_message("a", "b", "q", msg_type="question"))
    await mb.send(Mailbox.make_message("a", "b", "r", msg_type="reply"))

    qs = await mb.receive("b", msg_type="question")
    assert len(qs) == 1 and qs[0].content == "q"


# ---------------------------------------------------------------------------
# wait_for_message——异步等待新消息
# ---------------------------------------------------------------------------


async def test_wait_returns_true_when_message_already_pending() -> None:
    mb = Mailbox()
    await mb.send(Mailbox.make_message("a", "b", "x"))
    assert await mb.wait_for_message("b", timeout=0.1) is True


async def test_wait_unblocks_on_send() -> None:
    mb = Mailbox()

    async def waiter() -> bool:
        return await mb.wait_for_message("b", timeout=2.0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # 让 waiter 先进入等待
    await mb.send(Mailbox.make_message("a", "b", "x"))

    got = await task
    assert got is True


async def test_wait_timeout() -> None:
    mb = Mailbox()
    got = await mb.wait_for_message("nobody", timeout=0.05)
    assert got is False


# ---------------------------------------------------------------------------
# get / all_messages
# ---------------------------------------------------------------------------


async def test_get_by_id() -> None:
    mb = Mailbox()
    m = Mailbox.make_message("a", "b", "x")
    await mb.send(m)
    fetched = await mb.get(m.id)
    assert fetched is not None
    assert fetched.content == "x"


async def test_all_messages_sorted_by_time() -> None:
    mb = Mailbox()
    m1 = Mailbox.make_message("a", "b", "1")
    m2 = Mailbox.make_message("a", "b", "2")
    m3 = Mailbox.make_message("a", "b", "3")
    await mb.send(m1)
    await mb.send(m2)
    await mb.send(m3)
    all_msgs = await mb.all_messages()
    assert [m.content for m in all_msgs] == ["1", "2", "3"]


async def test_concurrent_send_receive() -> None:
    """并发 sender + 唯一 receiver——所有消息都应抵达"""
    mb = Mailbox()

    async def sender(i: int) -> None:
        await mb.send(Mailbox.make_message("a", "b", f"msg-{i}"))

    await asyncio.gather(*[sender(i) for i in range(20)])
    inbox = await mb.receive("b")
    contents = sorted(m.content for m in inbox)
    assert contents == sorted(f"msg-{i}" for i in range(20))
