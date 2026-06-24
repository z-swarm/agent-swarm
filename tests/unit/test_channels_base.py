"""单元测试：channels/base.py——DESIGN §4.2 通道抽象"""

from __future__ import annotations

import pytest

from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageType,
)


def test_channel_type_has_lark_and_sdk() -> None:
    """ChannelType 包含 MVP 必需通道"""
    assert ChannelType.LARK.value == "lark"
    assert ChannelType.CLI.value == "cli"
    assert ChannelType.SDK.value == "sdk"


def test_channel_user_required_fields() -> None:
    """ChannelUser 必填字段 + extra 默认空"""
    u = ChannelUser(channel=ChannelType.LARK, user_id="ou_123", display_name="Alice")
    assert u.user_id == "ou_123"
    assert u.display_name == "Alice"
    assert u.extra == {}


def test_channel_message_default_text() -> None:
    """ChannelMessage 默认 TEXT 类型 + 无 media"""
    m = ChannelMessage(
        id="m1",
        channel=ChannelType.LARK,
        from_user=ChannelUser(channel=ChannelType.LARK, user_id="u", display_name="u"),
        content="hello",
    )
    assert m.msg_type == MessageType.TEXT
    assert m.media_urls == []
    assert m.reply_to is None
    assert m.timestamp == 0.0


def test_channel_response_text_and_card() -> None:
    """ChannelResponse 同时支持 TEXT 和 CARD"""
    r_text = ChannelResponse(content="hi")
    assert r_text.msg_type == MessageType.TEXT
    assert r_text.card_template is None

    r_card = ChannelResponse(
        content="please confirm",
        msg_type=MessageType.CARD,
        card_template="confirm_dialog",
        card_data={"title": "Deploy?"},
    )
    assert r_card.card_template == "confirm_dialog"


def test_channel_connector_is_abstract() -> None:
    """ChannelConnector 不能直接实例化——必须实现所有抽象方法"""
    with pytest.raises(TypeError, match="abstract"):
        ChannelConnector()  # type: ignore[abstract]


def test_channel_connector_subclass_must_implement_all() -> None:
    """只实现部分方法的子类不能实例化"""

    class _Partial(ChannelConnector):
        @property
        def channel_type(self) -> ChannelType:
            return ChannelType.LARK

    with pytest.raises(TypeError, match="abstract"):
        _Partial()  # type: ignore[abstract]


def test_channel_connector_full_subclass_works() -> None:
    """完整实现可实例化"""

    class _Full(ChannelConnector):
        @property
        def channel_type(self) -> ChannelType:
            return ChannelType.LARK

        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        async def send(self, response, target) -> bool:  # type: ignore[override]
            return True

        def subscribe(self, handler) -> None: ...
        def unsubscribe(self, handler) -> None: ...

    c = _Full()
    assert c.channel_type == ChannelType.LARK
