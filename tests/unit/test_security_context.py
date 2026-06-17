"""单元测试：SecurityContext + SecurityContextManager"""

from __future__ import annotations

import asyncio

import pytest

from agent_swarm.security.context import (
    SecurityContext,
    SecurityContextManager,
    default_local_context,
)


def test_current_raises_when_unset() -> None:
    """未设置时 current() 抛 LookupError"""
    # 用新 ContextVar 隔离——避免测试间污染
    with pytest.raises(LookupError):
        SecurityContextManager.current()


def test_current_or_default_returns_local_when_unset() -> None:
    """未设置时 current_or_default() 返回默认 local 上下文"""
    ctx = SecurityContextManager.current_or_default()
    assert ctx.tenant_id == "local"
    assert ctx.session_id == "default"


def test_scope_sync() -> None:
    """sync scope 生效 + 退出后回到默认"""
    custom = SecurityContext(tenant_id="A", session_id="S1")
    with SecurityContextManager.scope(custom):
        assert SecurityContextManager.current() is custom
    # 退出 scope → 回到默认
    with pytest.raises(LookupError):
        SecurityContextManager.current()


async def test_async_scope() -> None:
    """async scope 生效 + 退出后回到默认"""
    custom = SecurityContext(tenant_id="B", session_id="S2")
    async with SecurityContextManager.async_scope(custom):
        assert SecurityContextManager.current() is custom
    with pytest.raises(LookupError):
        SecurityContextManager.current()


async def test_nested_scopes() -> None:
    """嵌套 scope 恢复正确——内层退出后回到外层"""
    outer = SecurityContext(tenant_id="OUTER", session_id="O")
    inner = SecurityContext(tenant_id="INNER", session_id="I")
    with SecurityContextManager.scope(outer):
        assert SecurityContextManager.current().tenant_id == "OUTER"
        with SecurityContextManager.scope(inner):
            assert SecurityContextManager.current().tenant_id == "INNER"
        # 内层退出后回到 outer
        assert SecurityContextManager.current().tenant_id == "OUTER"
    # 外层也退出
    with pytest.raises(LookupError):
        SecurityContextManager.current()


async def test_async_context_under_asyncio() -> None:
    """async 任务树中 scope 隐式传递"""
    outer = SecurityContext(tenant_id="PARENT", session_id="P")
    inner_seen: list[str] = []

    async def child():
        # 不显式传参——直接 current() 拿到
        inner_seen.append(SecurityContextManager.current().tenant_id)

    async with SecurityContextManager.async_scope(outer):
        await child()
    assert inner_seen == ["PARENT"]


async def test_concurrent_tasks_have_independent_contexts() -> None:
    """并发任务各自 scope 互不干扰"""
    seen: list[str] = []

    async def worker(tenant: str, delay: float) -> None:
        async with SecurityContextManager.async_scope(
            SecurityContext(tenant_id=tenant, session_id=f"S-{tenant}")
        ):
            await asyncio.sleep(delay)
            seen.append(SecurityContextManager.current().tenant_id)

    await asyncio.gather(worker("A", 0.02), worker("B", 0.01))
    # 两个 tenant 都各看到自己的值（没有互相污染）
    assert sorted(seen) == ["A", "B"]


def test_default_local_context_factory() -> None:
    ctx = default_local_context(session_id="my-session")
    assert ctx.tenant_id == "local"
    assert ctx.session_id == "my-session"
    assert ctx.user is None


def test_security_context_frozen() -> None:
    """SecurityContext 是 frozen——不可变（线程安全 + 安全）"""
    ctx = SecurityContext(tenant_id="A", session_id="B")
    with pytest.raises((AttributeError, Exception)):
        ctx.tenant_id = "C"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F-09: asyncio.create_task 跨 task 边界 SecurityContext 传播
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_with_context_preserves_tenant() -> None:
    """
    @brief F-09: 显式 context=ctx.asyncio_context() 时, 子 task 内的 ctx 不丢
    """
    captured_tenant: list[str] = []

    async def child_task() -> None:
        from agent_swarm.security.context import SecurityContextManager
        captured_tenant.append(SecurityContextManager.current().tenant_id)

    ctx = SecurityContext(tenant_id="T-X", session_id="S-X")
    async with SecurityContextManager.async_scope(ctx):
        # 不传 context: Python 3.11+ 默认复制, 但显式传更稳
        task_ctx = ctx.asyncio_context()
        task = asyncio.create_task(child_task(), context=task_ctx)
        await task

    assert captured_tenant == ["T-X"]





def test_security_context_asyncio_context_returns_context() -> None:
    """
    @brief SecurityContext.asyncio_context() 返回 contextvars.Context 实例
    """
    import contextvars
    ctx = SecurityContext(tenant_id="T", session_id="S")
    result = ctx.asyncio_context()
    assert isinstance(result, contextvars.Context)

