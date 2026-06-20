"""
@module tests.golden.cases.G-020_redis_backend_concurrent_cas
@brief  W18-⑤ G-020 Golden Case——Redis 后端并发 CAS 验证

DESIGN §9.4 + P3-PLAN-v2 W18 DoD ⑤:
  - 多 agent 并发 claim 同 task_id, Redis WATCH/MULTI/EXEC 保证仅 1 成功
  - 验证 W18-4 (多进程并发安全) 走通

@note 与 G-018/G-019 同模式: expected.yaml + input.yaml + README
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

from agent_swarm.core.backends.redis_backend import (
    RedisBackend,
    RedisConfig,
)
from agent_swarm.core.task_queue_backend import (
    StoredTask,
    VersionMismatchError,
)


def _mk_task(tid: str, version: int = 0, status: str = "pending") -> StoredTask:
    now = time.time()
    return StoredTask(
        id=tid, title=f"task-{tid}", description="",
        status=status, version=version, assigned_to=None,
        depends_on=[], result=None, error=None,
        created_at=now, updated_at=now,
    )


async def run_case() -> dict[str, object]:
    """
    @return  golden case 报告 dict
    """
    namespace = f"golden-g020-{time.time_ns()}"
    backend = RedisBackend(RedisConfig(namespace=namespace, use_fakeredis=True))

    # 1) 准备一个 task
    await backend.put(_mk_task("hot", version=0))

    # 2) 100 个并发 agent 抢同一 task
    n_agents = 100
    barrier = asyncio.Event()

    def mut(t: StoredTask) -> StoredTask:
        t.status = "in_progress"
        t.version += 1
        t.assigned_to = "agent-x"
        return t

    async def attempt(agent_id: str) -> str:
        await barrier.wait()
        try:
            await backend.compare_and_set("hot", 0, mut)
            return "ok"
        except VersionMismatchError:
            return "conflict"

    tasks = [asyncio.create_task(attempt(f"agent-{i}")) for i in range(n_agents)]
    await asyncio.sleep(0)  # 让所有 task 启动
    barrier.set()
    results = await asyncio.gather(*tasks)

    ok = results.count("ok")
    conflicts = results.count("conflict")

    # 3) 验证终态
    final = await backend.get("hot")
    final_version = final.version if final else -1
    final_assigned = final.assigned_to if final else None

    await backend.close()

    return {
        "case": "G-020_redis_backend_concurrent_cas",
        "agents": n_agents,
        "ok_count": ok,
        "conflict_count": conflicts,
        "final_version": final_version,
        "final_assigned": final_assigned,
        "invariant_ok_winner_only": ok == 1,
        "invariant_version_bumped_to_1": final_version == 1,
        "invariant_assigned_persisted": final_assigned == "agent-x",
        "invariant_no_partial_state": (
            ok + conflicts == n_agents
        ),
    }


async def main() -> int:
    repo = Path(__file__).parents[2]
    case_dir = repo / "tests" / "golden" / "cases" / "G-020_redis_backend_concurrent_cas"
    case_dir.mkdir(parents=True, exist_ok=True)
    expected_path = case_dir / "expected.yaml"
    report = await run_case()
    print("[G-020] report:", report)
    # 写 expected (用于 CI 校验)
    expected_path.write_text(
        yaml.safe_dump(report, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # DoD 校验
    assert report["invariant_ok_winner_only"], "expected exactly 1 ok"
    assert report["invariant_version_bumped_to_1"], "version should bump to 1"
    assert report["invariant_assigned_persisted"], "assigned_to should persist"
    assert report["invariant_no_partial_state"], "all attempts accounted"
    print("[G-020] OK: Redis backend CAS invariant holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
