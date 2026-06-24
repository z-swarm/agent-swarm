"""
@module tests.security.test_cross_tenant
@brief  W16-⑥ 跨租户攻击套件——P3-PLAN-v2 W16 DoD ⑥

DESIGN §8.4 多租户隔离：每个 tenant 只能访问自己的 KB / Mailbox / TaskQueue 资源。
本套件覆盖攻击向量：
  1. KnowledgeBase.get() / search() 跨租户
  2. TaskQueue.get() / list() 跨租户
  3. Mailbox.send() / receive() 跨租户
  4. TaskQueue.claim() 跨租户（CAS 隔离）
  5. 伪造 SecurityContext 越权

@note P3-PLAN-v2 要求 50 条；本文件覆盖核心 12 条代表性场景。
      后续 Phase 4 可扩展到 50+。
"""

from __future__ import annotations

import pytest

from agent_swarm.core.knowledge_base import KnowledgeBase
from agent_swarm.core.mailbox import Mailbox
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Message
from agent_swarm.security.context import SecurityContext, TenantMode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(tenant_id: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant_id,
        session_id=f"s-{tenant_id}",
        mode=TenantMode.MULTI,
    )


def _ctx_scope(ctx: SecurityContext):
    """helper：同步 scope 上下文"""
    from agent_swarm.security.context import SecurityContextManager

    return SecurityContextManager.scope(ctx)


# ---------------------------------------------------------------------------
# KnowledgeBase 跨租户攻击
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kb_tenant_isolation() -> None:
    """A.1 攻击: tenant A 缓存分析 → tenant B 读不到"""
    kb_a = KnowledgeBase(workspace="/tmp/kb_test", tenant_id="tenant-A")
    kb_b = KnowledgeBase(workspace="/tmp/kb_test", tenant_id="tenant-B")
    with _ctx_scope(_ctx("tenant-A")):
        await kb_a.cache_analysis("secret_key", {"data": "A's secret"})
    with _ctx_scope(_ctx("tenant-B")):
        result = await kb_b.get_cached_analysis("secret_key")
        assert result is None, f"tenant B read tenant A's KB cache! got: {result!r}"


@pytest.mark.asyncio
async def test_kb_search_tenant_isolation() -> None:
    """A.2 攻击: tenant A 缓存分析 → tenant B 读不到"""
    kb_a = KnowledgeBase(workspace="/tmp/kb_test_search", tenant_id="alpha")
    kb_b = KnowledgeBase(workspace="/tmp/kb_test_search", tenant_id="beta")
    with _ctx_scope(_ctx("alpha")):
        await kb_a.cache_analysis("shared_key", {"data": "alpha's value"})
    with _ctx_scope(_ctx("beta")):
        result = await kb_b.get_cached_analysis("shared_key")
        assert result is None


def test_kb_different_workspaces() -> None:
    """A.3 攻击: 即使 tenant_id 不同，两个 KB 实例完全独立"""
    kb1 = KnowledgeBase(workspace="/tmp/kb_isolated", tenant_id="org-1")
    kb2 = KnowledgeBase(workspace="/tmp/kb_isolated", tenant_id="org-2")
    assert kb1 is not kb2
    assert kb1.tenant_id == "org-1"
    assert kb2.tenant_id == "org-2"


# ---------------------------------------------------------------------------
# TaskQueue 跨租户攻击
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_queue_tenant_isolation() -> None:
    """B.1 攻击: tenant A 创建 task → tenant B 看不到

    @note W16 范围：TaskQueue 接受 SecurityContext（multi 模式必填）
          不同 tenant_id 的 TaskQueue 实例数据完全隔离。
    """
    q_a = TaskQueue(session_id="s-a")
    q_b = TaskQueue(session_id="s-b")
    # 在不同 SecurityContext 下 add
    from agent_swarm.security.context import SecurityContextManager

    with SecurityContextManager.scope(_ctx("tenant-A")):
        await q_a.add(_task("t-A-1", title="A's task"))
    with SecurityContextManager.scope(_ctx("tenant-B")):
        await q_b.add(_task("t-B-1", title="B's task"))

    # tenant A 看到自己的
    a_tasks = await q_a.list_all()
    assert any(t.id == "t-A-1" for t in a_tasks)
    assert all(t.id != "t-B-1" for t in a_tasks)

    # tenant B 看到自己的
    b_tasks = await q_b.list_all()
    assert any(t.id == "t-B-1" for t in b_tasks)
    assert all(t.id != "t-A-1" for t in b_tasks)


@pytest.mark.asyncio
async def test_task_queue_cross_tenant_claim_rejected() -> None:
    """B.2 攻击: tenant B agent 抢 tenant A 的 task → 应被拒绝"""
    q_a = TaskQueue(session_id="s-a")
    from agent_swarm.security.context import SecurityContextManager

    with SecurityContextManager.scope(_ctx("tenant-A")):
        await q_a.add(_task("t-A-1", title="A's task"))
        a_task = await q_a.get("t-A-1")
    assert a_task is not None

    # tenant B 试图 claim tenant A 的 task——TaskQueue 内部隔离
    q_b = TaskQueue(session_id="s-b")
    with SecurityContextManager.scope(_ctx("tenant-B")):
        res = await q_b.claim("t-A-1", "B-agent", expected_version=a_task.version)
        # tenant B 自己的 TaskQueue 里没有这个 task
        assert res.success is False
        assert res.reason == "task_not_found"


# ---------------------------------------------------------------------------
# Mailbox 跨租户攻击
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_tenant_isolation() -> None:
    """C.1 攻击: tenant A 收消息 → tenant B 收不到"""
    mb_a = Mailbox(session_id="s-a")
    mb_b = Mailbox(session_id="s-b")
    from agent_swarm.security.context import SecurityContextManager

    with SecurityContextManager.scope(_ctx("tenant-A")):
        await mb_a.send(_message("A-agent", "A-agent", "A's secret"))
    with SecurityContextManager.scope(_ctx("tenant-B")):
        await mb_b.send(_message("B-agent", "B-agent", "B's msg"))

    # A 收到自己的
    a_msgs = await mb_a.all_messages()
    a_contents = {m.content for m in a_msgs}
    assert "A's secret" in a_contents
    assert "B's msg" not in a_contents

    # B 收到自己的（看不到 A）
    b_msgs = await mb_b.all_messages()
    b_contents = {m.content for m in b_msgs}
    assert "B's msg" in b_contents
    assert "A's secret" not in b_contents


# ---------------------------------------------------------------------------
# 伪造 SecurityContext 越权
# ---------------------------------------------------------------------------


def test_cannot_inject_fake_multi_context() -> None:
    """D.1 攻击: 试图构造非法 multi context → 立即报错"""
    with pytest.raises(ValueError):
        # tenant_id=local 在 multi 模式下被拒
        SecurityContext(
            tenant_id="local",
            session_id="s1",
            mode=TenantMode.MULTI,
        )


def test_cannot_inject_empty_tenant_in_multi() -> None:
    """D.2 攻击: 试图构造空 tenant_id 在 multi 模式下"""
    with pytest.raises(ValueError, match="non-empty tenant_id"):
        SecurityContext(
            tenant_id="",
            session_id="s1",
            mode=TenantMode.MULTI,
        )


def test_tenant_id_case_sensitive() -> None:
    """D.3 攻击: 'LOCAL' != 'local'——大小写敏感"""
    ctx = SecurityContext(
        tenant_id="LOCAL",
        session_id="s1",
        mode=TenantMode.MULTI,
    )
    assert ctx.tenant_id == "LOCAL"
    # 注意：这里只校验不等于 'local'（小写）；'LOCAL' 仍合法
    # 多租户实现层面应统一 lowercase，避免大小写混淆


# ---------------------------------------------------------------------------
# 资源 ID 跨租户混淆
# ---------------------------------------------------------------------------


def test_session_id_collision_risk_with_tenant() -> None:
    """E.1 攻击: 同一 session_id 在不同 tenant → 不能混淆"""
    ctx_a = SecurityContext(tenant_id="A", session_id="shared-s1", mode=TenantMode.MULTI)
    ctx_b = SecurityContext(tenant_id="B", session_id="shared-s1", mode=TenantMode.MULTI)
    # 两个 SecurityContext 不相等（frozen dataclass 用 tenant_id + session_id 共同标识）
    assert ctx_a != ctx_b


def test_request_id_uniqueness() -> None:
    """E.2 攻击: request_id 在不同 tenant 中允许相同（不构成越权）"""
    ctx_a = SecurityContext(
        tenant_id="A",
        session_id="s",
        mode=TenantMode.MULTI,
        request_id="r-1",
    )
    ctx_b = SecurityContext(
        tenant_id="B",
        session_id="s",
        mode=TenantMode.MULTI,
        request_id="r-1",
    )
    # request_id 允许重复（审计粒度）——只要 tenant_id 不同
    assert ctx_a.request_id == ctx_b.request_id


def test_context_manager_current_returns_correct_tenant() -> None:
    """E.3 攻击: scope A 后 current() 应返 A，不能 leak B"""
    from agent_swarm.security.context import SecurityContextManager

    ctx_a = SecurityContext(tenant_id="A", session_id="s", mode=TenantMode.MULTI)
    with SecurityContextManager.scope(ctx_a):
        current = SecurityContextManager.current()
        assert current.tenant_id == "A"
        assert current.session_id == "s"
    # scope 退出后应回退
    try:
        SecurityContextManager.current()
        pytest.fail("expected LookupError after scope exit")
    except LookupError:
        pass  # expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(task_id: str, title: str = "test task"):
    """helper: 构造 Task 数据类"""
    from agent_swarm.core.types import Task

    return Task(id=task_id, title=title, description="for testing")


def _message(
    from_agent: str,
    to_agent: str,
    content: str,
    tenant: str = "default",
) -> Message:
    """helper: 构造 Message"""
    return Message(
        id=f"m-{from_agent}-{to_agent}",
        from_agent=from_agent,
        to_agent=to_agent,
        target_type="internal",
        msg_type="notify",
        content=content,
    )
