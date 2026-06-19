"""
@brief 消息通道层——DESIGN §4

Phase 2 W10 引入飞书连接器。
骨架：ChannelType / ChannelMessage / ChannelConnector ABC / ChannelAdapter 路由
落地：LarkConnector + 5 个内置卡片模板
"""
from __future__ import annotations

from agent_swarm.channels.adapter import (
    APIKeyStore,
    ChannelAdapter,
    RateLimiter,
    SessionBinding,
    SessionBindingManager,
)
from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageHandler,
    MessageType,
)
from agent_swarm.channels.card_templates import (
    TEMPLATES,
    render_adversarial_debug,
    render_card,
    render_code_review_result,
    render_confirm_dialog,
    render_swarm_status,
    render_task_progress,
)
from agent_swarm.channels.lark import (
    CARD_TEMPLATES,
    LARK_API_BASE,
    LarkConnector,
    resolve_lark_secret,
    verify_lark_signature,
)

__all__ = [
    "APIKeyStore",
    "CARD_TEMPLATES",
    "ChannelAdapter",
    "ChannelConnector",
    "ChannelMessage",
    "ChannelResponse",
    "ChannelType",
    "ChannelUser",
    "LARK_API_BASE",
    "LarkConnector",
    "MessageHandler",
    "MessageType",
    "RateLimiter",
    "SessionBinding",
    "SessionBindingManager",
    "TEMPLATES",
    "render_adversarial_debug",
    "render_card",
    "render_code_review_result",
    "render_confirm_dialog",
    "render_swarm_status",
    "render_task_progress",
    "resolve_lark_secret",
    "verify_lark_signature",
]
