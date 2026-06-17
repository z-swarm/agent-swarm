"""
@module agent_swarm.security.context
@brief  SecurityContext + SecurityContextManager（W5）

DESIGN.md §8.4 完整规约：
  - contextvars 模式——上下文在 async 任务树隐式传递
  - scope() 同步 / async_scope() 异步 上下文管理器
  - 单租户默认：tenant_id="local"
  - W5 启用后：所有路径（KB / Storage / Tool）从 current() 取上下文

W5 落地：
  - SecurityContext dataclass（与 §A.5 一致）
  - SecurityContextManager: current() / scope() / async_scope() / current_or_default()
  - default_local_context() 工厂——便于测试 / 单租户场景

@note W5-Z 已知风险：asyncio.create_task 不会自动复制 contextvars
      （Python 3.11+ 默认会，但跨 task 边界仍需显式 scope 包裹回调）
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityContext:
    """
    请求上下文——通过 contextvars 在 async 任务树中隐式传递

    DESIGN.md §A.5：
      - tenant_id: 租户隔离的根字段
      - user: 触发请求的外部用户（可能为 None：内部任务）
      - session_id: 关联事件流
      - request_id: 关联日志/审计
    """

    tenant_id: str
    session_id: str
    user: Any | None = None
    request_id: str | None = None

    def asyncio_context(self) -> contextvars.Context:
        """
        @brief 返回一个 contextvars.Context 副本——用于 asyncio.create_task(context=...)

        @note 调用方应已在 SecurityContextManager.scope/async_scope 内, 此时
              _current_security_ctx 已 set, copy_context() 自然包含
        @note Python 3.11+ asyncio.create_task 默认会复制, 但显式传更稳
              且与 3.10 兼容 (3.10 不会自动复制)
        """
        return contextvars.copy_context()


# 全局 context var——underscore 前缀防止外部直接访问
_current_security_ctx: contextvars.ContextVar[SecurityContext] = contextvars.ContextVar(
    "agent_swarm_security_ctx"
)


class SecurityContextManager:
    """
    SecurityContext 访问门面

    @note 推荐用法:
        with SecurityContextManager.scope(ctx):    # 同步路径
            ...
        async with SecurityContextManager.async_scope(ctx):  # 异步路径
            ...
    """

    @staticmethod
    def current() -> SecurityContext:
        """
        获取当前上下文——任何代码路径都可用

        @raise LookupError 若当前 task 未设置 ctx（call current_or_default 以兜底）
        """
        return _current_security_ctx.get()

    @staticmethod
    def current_or_default(
        tenant_id: str = "local",
        session_id: str = "default",
    ) -> SecurityContext:
        """
        获取上下文；未设置或被显式 set(None) 时返回单租户默认（W1-W4 路径走此分支）
        """
        try:
            ctx = _current_security_ctx.get()
        except LookupError:
            ctx = None
        if ctx is None:
            return SecurityContext(
                tenant_id=tenant_id, session_id=session_id, user=None
            )
        return ctx

    @staticmethod
    @contextmanager
    def scope(ctx: SecurityContext) -> Iterator[SecurityContext]:
        """
        同步 scope。用法::

            with SecurityContextManager.scope(ctx):
                ...
        """
        token = _current_security_ctx.set(ctx)
        try:
            yield ctx
        finally:
            _current_security_ctx.reset(token)

    @staticmethod
    @asynccontextmanager
    async def async_scope(ctx: SecurityContext) -> AsyncIterator[SecurityContext]:
        """
        异步 scope。用法::

            async with SecurityContextManager.async_scope(ctx):
                ...

        @note 跨 task 边界（如 ApprovalFlow 回调）必须使用此版本，
              以保证 contextvar 在 async 边界正确传播
        """
        token = _current_security_ctx.set(ctx)
        try:
            yield ctx
        finally:
            _current_security_ctx.reset(token)


def default_local_context(session_id: str = "default") -> SecurityContext:
    """便捷工厂：单租户默认上下文（CLI 启动 / 测试用）"""
    return SecurityContext(
        tenant_id="local",
        session_id=session_id,
        user=None,
    )
