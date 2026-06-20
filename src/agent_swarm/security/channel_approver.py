"""
@module agent_swarm.security.channel_approver
@brief  ChannelAdapter-based Approver——W11

DESIGN §4.4 + §8.3 整合：
  - ApprovalFlow 调用 Approver(decision, ctx) → True/False
  - ChannelApprover 包装 ChannelAdapter：
    1. REQUIRE_APPROVAL 决策 → 渲染飞书 confirm_dialog 卡片 → 发送给 approver_user
    2. 启动一个 Future 等待用户卡片回调
    3. 卡片回调（approve/deny 按钮）→ set Future 结果
    4. 等待 timeout 秒未收到 → set False（fail-closed）
  - 与 LarkConnector 解耦：可换其他 ChannelConnector

W11 范围：
  - ChannelApprover 适配 ApprovalFlow
  - 异步等待 + 超时 → fail-closed
  - 取消机制 (cancel_inflight)：避免泄漏 Future
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_swarm.channels.adapter import ChannelAdapter
from agent_swarm.channels.base import (
    ChannelMessage,
    ChannelResponse,
    ChannelUser,
    MessageType,
)
from agent_swarm.security.context import SecurityContext
from agent_swarm.security.policy import PolicyDecision

log = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    """
    单次审批请求状态

    @note 用 asyncio.Future 等待用户回复
    @note 持久化：远期可写 SQLite（崩溃恢复）；W11 内存版
    """

    request_id: str
    decision: PolicyDecision
    approver: ChannelUser
    sent_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class ChannelApprover:
    """
    通过消息通道异步等待用户审批的 Approver

    @note 适配 ApprovalFlow：作为 Approver(decision, ctx) 注入
    @note 行为：发送 confirm_dialog 卡片 → 等待用户按钮回调 → 解析 approve/deny
    @note 失败默认 deny (fail-closed)
    """

    def __init__(
        self,
        adapter: ChannelAdapter,
        approver_user: ChannelUser,
        approval_timeout: float = 3600.0,
        card_data_fn: Callable[[PolicyDecision, SecurityContext], dict[str, Any]] | None = None,
    ) -> None:
        """
        @param adapter           ChannelAdapter 实例（已注册 LarkConnector）
        @param approver_user     接收审批卡片的用户（一般是管理员）
        @param approval_timeout  单次审批等待超时（秒）；超时 → fail-closed
        @param card_data_fn      可选：自定义卡片 data 的回调（决策/上下文 → dict）
        """
        self._adapter = adapter
        self._approver = approver_user
        self._timeout = approval_timeout
        self._card_data_fn = card_data_fn
        # request_id → ApprovalRequest
        self._inflight: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    @property
    def inflight_count(self) -> int:
        """当前在等待用户回复的审批数（监控用）"""
        return len(self._inflight)

    async def __call__(
        self,
        decision: PolicyDecision,
        ctx: SecurityContext,
    ) -> bool:
        """
        适配 ApprovalFlow.Approver 签名

        @return True=批准 / False=拒绝（含超时）
        """
        request_id = f"approval-{uuid.uuid4().hex[:12]}"
        # 1) 构造卡片
        card_data = self._build_card_data(decision, ctx, request_id)
        # 2) 通过 adapter 发送
        response = ChannelResponse(
            content=f"[approval required] {decision.reason}",
            msg_type=MessageType.CARD,
            card_template="confirm_dialog",
            card_data=card_data,
        )
        ok = await self._adapter.send(response, self._approver)
        if not ok:
            log.warning("channel_approver.send_failed reason=%s", decision.reason)
            return False
        # 3) 等待用户回复
        request = ApprovalRequest(
            request_id=request_id, decision=decision, approver=self._approver,
        )
        async with self._lock:
            self._inflight[request_id] = request
        try:
            log.info("channel_approver.request_sent id=%s reason=%s",
                     request_id, decision.reason)
            return await asyncio.wait_for(request.future, timeout=self._timeout)
        except TimeoutError:
            log.warning("channel_approver.timeout id=%s after %.1fs",
                        request_id, self._timeout)
            return False
        except asyncio.CancelledError:
            log.info("channel_approver.cancelled id=%s", request_id)
            return False
        finally:
            async with self._lock:
                self._inflight.pop(request_id, None)

    def _build_card_data(
        self, decision: PolicyDecision, ctx: SecurityContext, request_id: str,
    ) -> dict[str, Any]:
        """
        构造 confirm_dialog 卡片数据

        @note card_data_fn 可覆盖默认行为（业务侧定制）
        """
        if self._card_data_fn is not None:
            data = dict(self._card_data_fn(decision, ctx))
        else:
            data = {
                "title": "🔐 Approval Required",
                "message": (
                    f"**Tool**: {decision.reason}\n"
                    f"**Tenant**: `{ctx.tenant_id}`\n"
                    f"**Session**: `{ctx.session_id}`\n"
                    f"**Request ID**: `{request_id}`"
                ),
            }
        # 强制把 request_id 加到 actions value，让回调可识别
        data.setdefault("actions", [
            {"text": "Approve", "value": f"approve:{request_id}", "type": "primary"},
            {"text": "Deny", "value": f"deny:{request_id}", "type": "danger"},
        ])
        return data

    async def handle_card_action(self, msg: ChannelMessage) -> None:
        """
        卡片按钮回调入口——从 LarkConnector._on_card_action 注入

        @note msg.content 是 Lark 卡片 action JSON（含 value.action = "approve:xxx"）
        @note 解析出 request_id 和 decision → 触发对应 ApprovalRequest.future
        """
        import json
        try:
            action = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            log.warning("channel_approver.invalid_action_content")
            return
        # value.action 形如 "approve:approval-abc12345"
        value = action.get("value", {})
        action_str = value.get("action", "")
        if ":" not in action_str:
            log.warning("channel_approver.action_no_request_id action=%s", action_str)
            return
        decision_str, request_id = action_str.split(":", 1)
        granted = decision_str == "approve"
        async with self._lock:
            request = self._inflight.get(request_id)
            if request is None:
                # 可能已超时或被取消
                log.info("channel_approver.callback_unknown_request id=%s", request_id)
                return
            if request.future.done():
                log.info("channel_approver.callback_already_done id=%s", request_id)
                return
            request.future.set_result(granted)
        log.info("channel_approver.callback_processed id=%s granted=%s", request_id, granted)

    async def cancel_inflight(self, request_id: str | None = None) -> int:
        """
        取消 in-flight 请求

        @param request_id  指定取消一个；None 时取消所有
        @return 实际取消的数量
        """
        cancelled = 0
        async with self._lock:
            if request_id is not None:
                req = self._inflight.pop(request_id, None)
                if req is not None and not req.future.done():
                    req.future.set_result(False)
                    cancelled += 1
            else:
                for req in self._inflight.values():
                    if not req.future.done():
                        req.future.set_result(False)
                        cancelled += 1
                self._inflight.clear()
        return cancelled


__all__ = [
    "ApprovalRequest",
    "ChannelApprover",
]
