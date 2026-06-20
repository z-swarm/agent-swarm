"""
@module tools.bench_storage
@brief  W18-⑥ TaskQueue 后端压测

P3-PLAN-v2 W18 DoD ⑥:
  - bench_storage.py: Memory vs Redis 后端 QPS / p99 对比
  - 默认 fakeredis (无需 Redis server); --real-redis 时连真实 Redis

用法:
  python tools/bench_storage.py memory
  python tools/bench_storage.py redis
  python tools/bench_storage.py both
  python tools/bench_storage.py --real-redis redis
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _mk_task(tid: str, version: int = 0) -> Any:
    from agent_swarm.core.task_queue_backend import StoredTask
    now = time.time()
    return StoredTask(
        id=tid, title=f"t-{tid}", description="bench",
        status="pending", version=version, assigned_to=None,
        depends_on=[], result=None, error=None,
        created_at=now, updated_at=now,
    )


async def _bench_backend(
    name: str, factory: Any, n: int = 5000, concurrency: int = 50,
) -> dict[str, Any]:
    """
    @param factory  () -> TaskQueueBackend 实例
    """
    backend = factory()
    try:
        # 预热: 写入 n 个 task
        for i in range(n):
            await backend.put(_mk_task(f"t-{i}"))

        # CAS 压测: 每 task_id claim 一次
        sem = asyncio.Semaphore(concurrency)
        latencies: list[float] = []

        async def one_cas(i: int) -> None:
            async with sem:
                t0 = time.perf_counter()
                from agent_swarm.core.task_queue_backend import (
                    VersionMismatchError,
                )

                def mut(t: Any) -> Any:
                    t.status = "in_progress"
                    t.version += 1
                    t.assigned_to = "bench-agent"
                    return t

                with contextlib.suppress(VersionMismatchError):
                    await backend.compare_and_set(f"t-{i}", 0, mut)
                latencies.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*(one_cas(i) for i in range(n)))
        total = time.perf_counter() - t0
        return {
            "backend": name,
            "ops": n,
            "duration_s": round(total, 3),
            "qps": round(n / total, 1),
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(
                sorted(latencies)[int(n * 0.95)], 3,
            ),
            "p99_ms": round(
                sorted(latencies)[int(n * 0.99)], 3,
            ),
            "max_ms": round(max(latencies), 3),
        }
    finally:
        await backend.close()


async def main_async(args: argparse.Namespace) -> int:
    runners: dict[str, Any] = {}
    if args.mode in ("memory", "both"):
        from agent_swarm.core.backends.memory import MemoryBackend
        runners["memory"] = MemoryBackend
    if args.mode in ("redis", "both"):
        pytest_skip = False
        try:
            import fakeredis.aioredis  # noqa: F401
        except ImportError:
            pytest_skip = True
        if pytest_skip:
            print("[SKIP] redis: fakeredis not installed")
        else:
            from agent_swarm.core.backends.redis_backend import (
                RedisBackend,
                RedisConfig,
            )

            def redis_factory() -> Any:
                if args.real_redis:
                    cfg = RedisConfig(
                        url=args.redis_url,
                        namespace=f"bench-{time.time_ns()}",
                    )
                else:
                    cfg = RedisConfig(
                        namespace=f"bench-{time.time_ns()}",
                        use_fakeredis=True,
                    )
                return RedisBackend(cfg)

            runners["redis"] = redis_factory

    if not runners:
        print("[ERR] no backends to benchmark")
        return 1

    results: list[dict[str, Any]] = []
    for name, cls_or_factory in runners.items():
        if name == "memory":
            res = await _bench_backend(name, cls_or_factory)
        else:
            res = await _bench_backend(name, cls_or_factory)
        results.append(res)
        print(
            f"  [{name:6s}] {res['ops']} ops / {res['duration_s']}s "
            f"/ {res['qps']} QPS / p50={res['p50_ms']}ms "
            f"/ p99={res['p99_ms']}ms",
        )

    # 写 JSON 报告
    report_path = REPO / "docs" / "STORAGE-BENCH.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# W18 后端压测报告",
        "",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Backend | Ops | Duration(s) | QPS | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['backend']} | {r['ops']} | {r['duration_s']} "
            f"| {r['qps']} | {r['p50_ms']} | {r['p95_ms']} "
            f"| {r['p99_ms']} | {r['max_ms']} |",
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nreport -> {report_path.relative_to(REPO)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="W18 后端压测")
    parser.add_argument(
        "mode", choices=["memory", "redis", "both"],
        default="both", nargs="?",
    )
    parser.add_argument(
        "--real-redis", action="store_true",
        help="连真实 Redis (默认 fakeredis)",
    )
    parser.add_argument(
        "--redis-url", default="redis://localhost:6379/0",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
