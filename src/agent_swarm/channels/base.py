"""
@module agent_swarm.channels.base
@brief  消息通道抽象基类——DESIGN.md §4.2

W10 范围：
  - ChannelType / MessageType 枚举
  - ChannelUser / ChannelMessage / ChannelResponse 数据类
  - ChannelConnector ABC（含 channel_type / start / stop / send / subscribe / unsubscribe）
  - MessageHandler 类型别名

@note Phase 2 W10 引入；Phase 1 不涉及通道
@note 此模块不依赖 Lark SDK——保持纯 Python 抽象
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChannelType(Enum):
    """
    消息通道类型——DESIGN §4.2

    LARK       飞书（MVP 唯一通道，W10）
    REST_API   通用 HTTP 接入（远期）
    WEB_SOCKET WebSocket 通道（远期）
    CLI        命令行（Phase 1 已用，仅供测试/SDK）
    SDK        程序化调用（Phase 1 SDK 入口）
    """

    LARK = "lark"
    REST_API = "rest_api"
    WEB_SOCKET = "web_socket"
    CLI = "cli"
    SDK = "sdk"


class MessageType(Enum):
    """消息体类型——DESIGN §4.2"""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    CARD = "card"     # 飞书卡片 / Slack block 等结构化消息
    EVENT = "event"   # 卡片按钮点击 / webhook 触发等
    COMMAND = "command"


@dataclass
class ChannelUser:
    """
    通道用户标识——DESIGN §4.2

    @note user_id 是通道原生 ID（飞书 open_id / 企业微信 userid / CLI 进程 PID）
    @note extra 存通道特定附加字段（飞书 tenant_key / 邮箱 等）
    """

    channel: ChannelType
    user_id: str
    display_name: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelMessage:
    """
    统一消息格式——DESIGN §4.2

    @note 所有通道的消息归一化为此结构
    @note raw 存通道原生 payload（用于卡片回调时还原）
    """

    id: str
    channel: ChannelType
    from_user: ChannelUser
    content: str
    msg_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)
    reply_to: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class ChannelResponse:
    """
    统一响应格式——swarm 回复归一化后由各通道适配器渲染

    @note 简单文本 → msg_type=TEXT, content=...
    @note 卡片 → msg_type=CARD, card_template + card_data
    """

    content: str
    msg_type: MessageType = MessageType.TEXT
    card_template: str | None = None
    card_data: dict[str, Any] | None = None
    media_urls: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    reply_to: str | None = None


# handler 签名：async (msg: ChannelMessage) -> ChannelResponse
# 各连接器调 subscribe(handler) 后收到消息时回调
MessageHandler = Callable[[ChannelMessage], Awaitable[ChannelResponse]]


class ChannelConnector(ABC):
    """
    消息通道连接器基类——DESIGN §4.2

    每个通道实现自己的连接器，负责：
      1. 接收消息 → 归一化为 ChannelMessage
      2. 发送响应 → 将 ChannelResponse 渲染为通道原生格式
      3. 会话管理 → 绑定通道用户与 swarm session

    @note subscribe/unsubscribe 用于多 handler 场景
          （ChannelAdapter 自身是主 handler；测试/审计可附加额外 handler）
    """

    @property
    @abstractmethod
    def channel_type(self) -> ChannelType:
        """返回该连接器对应的通道类型"""
        ...

    @abstractmethod
    async def start(self) -> None:
        """
        启动连接器——开始接收消息
        @note 幂等：多次调用应不报错
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """
        停止连接器——停止接收消息并清理资源
        @note 幂等
        """
        ...

    @abstractmethod
    async def send(
        self,
        response: ChannelResponse,
        target: ChannelUser | str,
    ) -> bool:
        """
        发送响应到指定目标
        @param target  ChannelUser 实例或通道原生 user_id 字符串
        @return True=成功 / False=失败（不抛异常——业务路径不应被打断）
        """
        ...

    @abstractmethod
    def subscribe(self, handler: MessageHandler) -> None:
        """注册消息处理回调"""
        ...

    @abstractmethod
    def unsubscribe(self, handler: MessageHandler) -> None:
        """注销消息处理回调"""
        ...


__all__ = [
    "ChannelConnector",
    "ChannelMessage",
    "ChannelResponse",
    "ChannelType",
    "ChannelUser",
    "MessageHandler",
    "MessageType",
]
