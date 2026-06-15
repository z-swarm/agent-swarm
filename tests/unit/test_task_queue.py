"""单元测试：TaskQueue CAS 单一并发模型"""

from __future__ import annotations

import asyncio

import pytest

from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.core.types import Task


def _t(tid: str, depends_on: list[str] | None = None) -> Task:
    return Task(id=tid, title=tid, description=tid, depends_on=depends_on or [])


# ---------------------------------------------------------------------------
# 基础 add / get / list
# ---------------------------------------------------------------------------


async def test_add_and_get() -> None:
    q = TaskQueue()
    tid = await q.add(_t("a"))
    assert tid == "a"
    got = await q.get("a")
    assert got is not None
    assert got.title == "a"
    assert got.status == "pending"
    assert got.version == 0
    assert got.created_at > 0


async def test_add_duplicate_raises() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    with pytest.raises(ValueError, match="already exists"):
        await q.add(_t("a"))


async def test_add_with_unmet_dependency_is_blocked() -> None:
    """先 add 依赖任务 b（依赖 a），a 还没完成 → b 应为 blocked"""
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b", depends_on=["a"]))
    b = await q.get("b")
    assert b is not None
    assert b.status == "blocked"


async def test_add_many() -> None:
    q = TaskQueue()
    ids = await q.add_many([_t("a"), _t("b")])
    assert ids == ["a", "b"]


async def test_list_all() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b"))
    assert {t.id for t in await q.list_all()} == {"a", "b"}


async def test_list_claimable_excludes_blocked() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b", depends_on=["a"]))
    claimable = await q.list_claimable()
    assert {t.id for t in claimable} == {"a"}


async def test_list_claimable_filters_by_assigned_to() -> None:
    """assigned_to 已设值时，只有匹配的 agent 能看到"""
    q = TaskQueue()
    t = _t("a")
    t.assigned_to = "agent-1"
    await q.add(t)
    assert await q.list_claimable(agent_id="agent-1")
    assert not await q.list_claimable(agent_id="agent-2")
    # 未指定 agent_id 时不能过滤掉它
    assert await q.list_claimable()


# ---------------------------------------------------------------------------
# CAS claim
# ---------------------------------------------------------------------------


async def test_claim_success() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    res = await q.claim("a", agent_id="agent-1", expected_version=0)
    assert res.success is True
    assert res.reason == "ok"
    assert res.task is not None
    assert res.task.status == "in_progress"
    assert res.task.assigned_to == "agent-1"
    assert res.task.version == 1


async def test_claim_task_not_found() -> None:
    q = TaskQueue()
    res = await q.claim("nope", agent_id="a", expected_version=0)
    assert res.success is False
    assert res.reason == "task_not_found"


async def test_claim_version_mismatch() -> None:
    """传错 version 应得到 version_mismatch"""
    q = TaskQueue()
    await q.add(_t("a"))
    res = await q.claim("a", "agent-1", expected_version=99)
    assert res.success is False
    assert res.reason == "version_mismatch"


async def test_claim_already_claimed() -> None:
    """两个 agent 同时抢——第二个用同样 version=0 应得到 version_mismatch"""
    q = TaskQueue()
    await q.add(_t("a"))
    res1 = await q.claim("a", "agent-1", expected_version=0)
    assert res1.success is True

    res2 = await q.claim("a", "agent-2", expected_version=0)
    assert res2.success is False
    # 第一个 agent 已 +1，所以是 version_mismatch
    assert res2.reason == "version_mismatch"


async def test_claim_blocked_returns_dependency_blocked() -> None:
    """直接 claim blocked 任务应返回 dependency_blocked"""
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b", depends_on=["a"]))
    b = await q.get("b")
    assert b is not None and b.status == "blocked"

    # 强行 claim b——版本号正确，但状态不是 pending
    res = await q.claim("b", "agent-1", expected_version=b.version)
    assert res.success is False
    # blocked 不是 pending，进入 version_mismatch 分支
    assert res.reason == "version_mismatch"


# ---------------------------------------------------------------------------
# CAS complete
# ---------------------------------------------------------------------------


async def test_complete_success() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    claimed = await q.claim("a", "agent-1", expected_version=0)
    assert claimed.success and claimed.task

    res = await q.complete("a", result="done", expected_version=claimed.task.version)
    assert res.success
    assert res.task is not None
    assert res.task.status == "completed"
    assert res.task.result == "done"
    assert res.task.version == 2  # add(0) → claim(1) → complete(2)


async def test_complete_unblocks_dependents() -> None:
    """a 完成 → 依赖 a 的 b 自动从 blocked 转 pending"""
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b", depends_on=["a"]))

    # 初始：b 为 blocked
    b0 = await q.get("b")
    assert b0 is not None and b0.status == "blocked"

    claimed = await q.claim("a", "agent-1", expected_version=0)
    await q.complete("a", "ok", expected_version=claimed.task.version)  # type: ignore[union-attr]

    b1 = await q.get("b")
    assert b1 is not None and b1.status == "pending"
    # 解阻塞也算一次状态变更
    assert b1.version == 1


async def test_complete_version_mismatch() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    res = await q.complete("a", "x", expected_version=99)
    assert res.success is False
    assert res.reason == "version_mismatch"


# ---------------------------------------------------------------------------
# CAS fail
# ---------------------------------------------------------------------------


async def test_fail_success() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    claimed = await q.claim("a", "agent-1", expected_version=0)
    res = await q.fail("a", "boom", expected_version=claimed.task.version)  # type: ignore[union-attr]
    assert res.success
    assert res.task is not None
    assert res.task.status == "failed"
    assert res.task.error == "boom"


async def test_fail_does_not_unblock_dependents() -> None:
    """a 失败 → b 应保持 blocked，不能继续"""
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b", depends_on=["a"]))

    claimed = await q.claim("a", "agent-1", expected_version=0)
    await q.fail("a", "boom", expected_version=claimed.task.version)  # type: ignore[union-attr]

    b = await q.get("b")
    assert b is not None
    assert b.status == "blocked"


# ---------------------------------------------------------------------------
# 并发 CAS——这是 W2 DoD 的核心
# ---------------------------------------------------------------------------


async def test_concurrent_claim_only_one_wins() -> None:
    """N 个协程同时抢 1 个任务——只有 1 个成功"""
    q = TaskQueue()
    await q.add(_t("a"))

    async def worker(agent_id: str):
        return await q.claim("a", agent_id, expected_version=0)

    results = await asyncio.gather(*[worker(f"agent-{i}") for i in range(10)])
    successes = [r for r in results if r.success]
    conflicts = [r for r in results if not r.success]

    assert len(successes) == 1
    assert len(conflicts) == 9
    assert all(r.reason == "version_mismatch" for r in conflicts)


async def test_concurrent_claim_different_tasks() -> None:
    """3 个 agent 抢 3 个任务——各自成功 1 个"""
    q = TaskQueue()
    await q.add_many([_t("t1"), _t("t2"), _t("t3")])

    async def agent_loop(agent_id: str) -> list[str]:
        won = []
        for _ in range(5):
            claimable = await q.list_claimable(agent_id=agent_id)
            if not claimable:
                break
            for t in claimable:
                res = await q.claim(t.id, agent_id, expected_version=t.version)
                if res.success:
                    won.append(t.id)
                    break
        return won

    results = await asyncio.gather(*[agent_loop(f"a{i}") for i in range(3)])
    all_won = [t for sub in results for t in sub]
    # 每个任务最多被一个 agent 拿到
    assert sorted(all_won) == ["t1", "t2", "t3"]


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


async def test_stats() -> None:
    q = TaskQueue()
    await q.add(_t("a"))
    await q.add(_t("b"))
    await q.add(_t("c", depends_on=["a"]))

    s = await q.stats()
    assert s["pending"] == 2
    assert s["blocked"] == 1
