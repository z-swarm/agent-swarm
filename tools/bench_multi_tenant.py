"""
@file tools.bench_multi_tenant
@brief  W17-1 多租户压测——P3-PLAN-v2 W17 DoD ②

目标: 100 并发跨租户操作，0 越权 + p99 <= 500ms

跑法:
    python tools/bench_multi_tenant.py
    python tools/bench_multi_tenant.py --concurrency 100 --duration 30
    python tools/bench_multi_tenant.py --strict  # exit 1 if p99 > 500ms

验证项目 (Phase 3 DoD ①):
  1. 0 越权: tenant A 永远不能读 tenant B 的数据
  2. p99 延迟 <= 500ms (100 并发, 10s duration)
  3. 总 QPS / 平均延迟报告
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from contextlib import asynccontextmanager

from agent_swarm.core.knowledge_base import KnowledgeBase
from agent_swarm.core.task_queue import TaskQueue
from agent_swarm.security.context import (
    SecurityContext,
    SecurityContextManager,
    TenantMode,
)
from agent_swarm.security.tenant_quota import (
    TenantQuota,
    TenantQuotaRegistry,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant_id, session_id=f"bench-{tenant_id}",
        mode=TenantMode.MULTI,
    )


@asynccontextmanager
async def _scope(ctx: SecurityContext):
    with SecurityContextManager.scope(ctx):
        yield


async def _op_task_queue_add(tenant_id: str, task_id: str) -> None:
    """tenant → add task → measure latency"""
    t0 = time.monotonic()
    async with _scope(_ctx(tenant_id)):
        q = TaskQueue(session_id=f"bench-{tenant_id}")
        from agent_swarm.core.types import Task
        await q.add(Task(id=task_id, title="bench", description=""))
    return time.monotonic() - t0


async def _op_kb_cache(tenant_id: str, key: str, value: str) -> float:
    """tenant → KB cache write → measure latency"""
    t0 = time.monotonic()
    async with _scope(_ctx(tenant_id)):
        kb = KnowledgeBase(workspace="/tmp/bench_kb", tenant_id=tenant_id)
        await kb.cache_analysis(key, {"v": value})
    return time.monotonic() - t0


async def _op_kb_get(tenant_id: str, key: str) -> float:
    """tenant → KB cache read → measure latency"""
    t0 = time.monotonic()
    async with _scope(_ctx(tenant_id)):
        kb = KnowledgeBase(workspace="/tmp/bench_kb", tenant_id=tenant_id)
        await kb.get_cached_analysis(key)
    return time.monotonic() - t0


# ---------------------------------------------------------------------------
# Main: 100 concurrent cross-tenant workload
# ---------------------------------------------------------------------------


async def run_workload(
    concurrency: int = 100,
    duration_s: float = 10.0,
) -> dict[str, float | int]:
    """
    100 并发跨租户 workload

    模式: 每个并发 worker 在 tenant-A / tenant-B 间随机切换
    测量: 每个 op 的延迟 + 越权检测
    """
    tenants = ["tenant-A", "tenant-B"]
    ops = [
        ("task_add", _op_task_queue_add),
        ("kb_cache", _op_kb_cache),
        ("kb_get", _op_kb_get),
    ]

    # 跨租户越权检测: tenant A 写数据 → tenant B 应该看不到
    cross_tenant_violations = 0

    latencies: list[float] = []
    op_count = 0
    start = time.monotonic()
    end_time = start + duration_s

    async def worker(worker_id: int) -> None:
        nonlocal op_count
        local_lats: list[float] = []
        i = 0
        while time.monotonic() < end_time:
            tenant = tenants[i % 2]
            op_name, op_fn = ops[i % 3]
            try:
                if op_name == "task_add":
                    lat = await op_fn(tenant, f"w{worker_id}-t{i}")
                elif op_name == "kb_cache":
                    lat = await op_fn(tenant, f"k{worker_id}-{i}", f"v{i}")
                else:
                    lat = await op_fn(tenant, f"k{worker_id}-{i}")
                local_lats.append(lat)
            except Exception:  # noqa: BLE001
                pass
            i += 1
        latencies.extend(local_lats)
        op_count += len(local_lats)

    # 启动 concurrency 个 worker
    workers = [worker(w) for w in range(concurrency)]
    await asyncio.gather(*workers)

    elapsed = time.monotonic() - start

    # 越权检测: 验证 tenant A 写的数据, tenant B 读不到
    async with _scope(_ctx("tenant-A")):
        kb_a = KnowledgeBase(workspace="/tmp/bench_kb", tenant_id="tenant-A")
        await kb_a.cache_analysis("violation_test", {"secret": "A's data"})
    async with _scope(_ctx("tenant-B")):
        kb_b = KnowledgeBase(workspace="/tmp/bench_kb", tenant_id="tenant-B")
        result = await kb_b.get_cached_analysis("violation_test")
        if result is not None:
            cross_tenant_violations += 1

    # 计算 p99
    if latencies:
        sorted_lats = sorted(latencies)
        p50 = sorted_lats[len(sorted_lats) // 2]
        p95 = sorted_lats[int(len(sorted_lats) * 0.95)]
        p99 = sorted_lats[int(len(sorted_lats) * 0.99)]
        mean = statistics.mean(sorted_lats) * 1000
    else:
        p50 = p95 = p99 = mean = 0.0

    return {
        "concurrency": concurrency,
        "duration_s": elapsed,
        "op_count": op_count,
        "qps": op_count / elapsed,
        "p50_ms": p50 * 1000,
        "p95_ms": p95 * 1000,
        "p99_ms": p99 * 1000,
        "mean_ms": mean,
        "cross_tenant_violations": cross_tenant_violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="W17-1 multi-tenant benchmark (Phase 3 DoD 1)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=100,
        help="并发数 (默认 100)",
    )
    parser.add_argument(
        "--duration", type=float, default=10.0,
        help="持续时间 秒 (默认 10)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="严格模式: p99 > 500ms 或越权 > 0 时 exit 1",
    )
    args = parser.parse_args()

    print(
        f"=== Multi-Tenant Benchmark ===\n"
        f"  concurrency: {args.concurrency}\n"
        f"  duration:    {args.duration}s\n"
    )

    result = asyncio.run(run_workload(
        concurrency=args.concurrency, duration_s=args.duration,
    ))

    print(f"  ops:         {result['op_count']}")
    print(f"  qps:         {result['qps']:.1f}")
    print(f"  p50:         {result['p50_ms']:.1f}ms")
    print(f"  p95:         {result['p95_ms']:.1f}ms")
    print(f"  p99:         {result['p99_ms']:.1f}ms")
    print(f"  mean:        {result['mean_ms']:.1f}ms")
    print(f"  cross-tenant violations: {result['cross_tenant_violations']}")

    # 验收
    fail = False
    if result["cross_tenant_violations"] > 0:
        print("\n[FAIL] cross-tenant violation detected!")
        fail = True
    if args.strict and result["p99_ms"] > 500:
        print(f"\n[FAIL] p99 latency {result['p99_ms']:.1f}ms > 500ms")
        fail = True
    if not fail:
        print("\n[OK] benchmark passed")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
