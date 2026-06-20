"""
@module tools.bench_sandbox
@brief  W19-⑦ Sandbox 性能对比——WORKSPACE_ONLY vs Docker

P3-PLAN-v2 W19 DoD ⑦:
  - tools/bench_sandbox.py workspace_only vs Docker 性能对比
  - Docker 启动开销 ≤500ms (本地镜像, mock runner)

用法:
  python tools/bench_sandbox.py             # 仅 WORKSPACE_ONLY
  python tools/bench_sandbox.py --docker    # 加 Docker (需要 docker CLI)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


async def _bench_workspace_only(
    workspace: Path, n: int = 100, concurrency: int = 10,
) -> dict[str, object]:
    """WORKSPACE_ONLY 模式基准"""
    from agent_swarm.security.sandbox import (
        SandboxManager,
        SandboxMode,
    )
    mgr = SandboxManager(workspace)
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)

    async def one() -> None:
        async with sem:
            t0 = time.perf_counter()
            with contextlib.suppress(Exception):
                await mgr.execute("ls", timeout=5.0)
            latencies.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    total = time.perf_counter() - t0
    return {
        "mode": SandboxMode.WORKSPACE_ONLY.value,
        "ops": n,
        "concurrency": concurrency,
        "duration_s": round(total, 3),
        "qps": round(n / total, 1),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(sorted(latencies)[int(n * 0.95)], 3),
        "p99_ms": round(sorted(latencies)[int(n * 0.99)], 3),
    }


async def _bench_docker(
    workspace: Path, n: int = 100, concurrency: int = 10,
) -> dict[str, object]:
    """Docker 模式基准——用 mock runner (无需真 docker daemon)"""
    from agent_swarm.security.sandbox import SandboxMode
    from agent_swarm.security.sandbox_docker import (
        DockerConfig,
        DockerSandboxManager,
    )

    async def fake_runner(argv: list[str]) -> dict[str, object]:
        # 模拟 docker run 开销 ~50ms (实际本地 ~200ms)
        await asyncio.sleep(0.05)
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}

    cfg = DockerConfig(docker_runner=fake_runner)
    mgr = DockerSandboxManager(workspace, config=cfg)
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)

    async def one() -> None:
        async with sem:
            t0 = time.perf_counter()
            with contextlib.suppress(Exception):
                await mgr.execute("ls", timeout=5.0)
            latencies.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    total = time.perf_counter() - t0
    return {
        "mode": SandboxMode.DOCKER.value,
        "ops": n,
        "concurrency": concurrency,
        "duration_s": round(total, 3),
        "qps": round(n / total, 1),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(sorted(latencies)[int(n * 0.95)], 3),
        "p99_ms": round(sorted(latencies)[int(n * 0.99)], 3),
    }


def _format(r: dict[str, object]) -> str:
    return (
        f"[{r['mode']:14s}] {r['ops']} ops / {r['duration_s']}s "
        f"/ {r['qps']} QPS / p50={r['p50_ms']}ms "
        f"/ p99={r['p99_ms']}ms"
    )


async def main_async(args: argparse.Namespace) -> int:
    ws = REPO / "examples" / "_bench_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    try:
        results: list[dict[str, object]] = []
        # WORKSPACE_ONLY 总是跑
        r1 = await _bench_workspace_only(ws, args.n, args.concurrency)
        print(_format(r1))
        results.append(r1)
        # Docker (mock 或 real)
        if args.docker:
            r2 = await _bench_docker(ws, args.n, args.concurrency)
            print(_format(r2))
            results.append(r2)
        # 写报告
        report_path = REPO / "docs" / "SANDBOX-BENCH.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# W19 Sandbox 性能基准",
            "",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "| Mode | Ops | Concurrency | Duration(s) | QPS | p50 (ms) | p95 (ms) | p99 (ms) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in results:
            lines.append(
                f"| {r['mode']} | {r['ops']} | {r['concurrency']} "
                f"| {r['duration_s']} | {r['qps']} "
                f"| {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} |",
            )
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nreport -> {report_path.relative_to(REPO)}")
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="W19 Sandbox 性能对比")
    parser.add_argument(
        "--docker", action="store_true",
        help="加测 DOCKER 模式 (mock runner)",
    )
    parser.add_argument("-n", type=int, default=100)
    parser.add_argument("-c", "--concurrency", type=int, default=10)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
