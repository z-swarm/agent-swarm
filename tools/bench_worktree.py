"""
@module tools.bench_worktree
@brief  P4-W23 Worktree 性能压测

P4 DoD:
  - 100 agent 并发 acquire / release
  - 测 QPS / p50 / p95 / p99

用法:
  python tools/bench_worktree.py             # 50 acquire
  python tools/bench_worktree.py --n 100     # 100 acquire
  python tools/bench_worktree.py --concurrent 20
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_swarm.worktree import WorktreeManager


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "bench@t"],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Bench"],
        check=True, capture_output=True, timeout=5,
    )
    (path / "README.md").write_text("# bench\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "."],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True, timeout=10,
    )


def bench_acquire_release(
    n: int, concurrency: int, *, same_key: bool = False,
) -> dict[str, object]:
    """压测 acquire + release 循环"""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "bench_repo"
        base = Path(tmp) / "worktrees"
        _init_repo(repo)
        mgr = WorktreeManager(repo, base_dir=base)
        latencies: list[float] = []

        def one_cycle(i: int) -> None:
            t0 = time.perf_counter()
            if same_key:
                h = mgr.acquire(tenant_id="bench", session_id="s1", agent_id="shared")
            else:
                h = mgr.acquire(
                    tenant_id="bench", session_id="s1", agent_id=f"a{i}",
                )
            t1 = time.perf_counter()
            # 模拟 agent 工作: 写一个文件
            (h.path / f"work_{i}.txt").write_text(f"work {i}", encoding="utf-8")
            t2 = time.perf_counter()
            mgr.release(h)
            t3 = time.perf_counter()
            latencies.append((t1 - t0) * 1000)  # acquire ms
            latencies.append((t3 - t2) * 1000)  # release ms

        t_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(one_cycle, range(n)))
        total_s = time.perf_counter() - t_start
        latencies.sort()
        return {
            "mode": "same_key" if same_key else "unique_keys",
            "ops": n,
            "concurrency": concurrency,
            "duration_s": round(total_s, 3),
            "qps": round(n * 2 / total_s, 1),  # acquire + release = 2 ops
            "acquire_p50_ms": round(latencies[0::2][len(latencies[0::2]) // 2], 3),
            "acquire_p99_ms": round(
                latencies[0::2][int(len(latencies[0::2]) * 0.99)], 3,
            ),
            "release_p50_ms": round(latencies[1::2][len(latencies[1::2]) // 2], 3),
            "release_p99_ms": round(
                latencies[1::2][int(len(latencies[1::2]) * 0.99)], 3,
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="P4-W23 Worktree 性能压测")
    parser.add_argument("-n", type=int, default=50, help="agent 数量")
    parser.add_argument(
        "-c", "--concurrency", type=int, default=10,
        help="并发线程数",
    )
    parser.add_argument(
        "--same-key", action="store_true",
        help="所有 agent 用同 (tenant, session, agent) 测试幂等性",
    )
    args = parser.parse_args()

    print(f"P4-W23 Worktree 压测: n={args.n} concurrency={args.concurrency}")
    print("=" * 70)
    results = []
    if not args.same_key:
        r = bench_acquire_release(args.n, args.concurrency, same_key=False)
        results.append(r)
        print(
            f"[unique]  ops={r['ops']} duration={r['duration_s']}s "
            f"QPS={r['qps']} "
            f"acquire p50={r['acquire_p50_ms']}ms p99={r['acquire_p99_ms']}ms "
            f"release p50={r['release_p50_ms']}ms p99={r['release_p99_ms']}ms"
        )
    r2 = bench_acquire_release(
        min(args.n, 100), args.concurrency, same_key=True,
    )
    results.append(r2)
    print(
        f"[same_key] ops={r2['ops']} duration={r2['duration_s']}s "
        f"QPS={r2['qps']} "
        f"acquire p50={r2['acquire_p50_ms']}ms p99={r2['acquire_p99_ms']}ms "
        f"release p50={r2['release_p50_ms']}ms p99={r2['release_p99_ms']}ms"
    )

    # 写报告
    report_path = Path(__file__).resolve().parents[1] / "docs" / "WORKTREE-BENCH.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# P4-W23 Worktree 压测报告",
        "",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Mode | Ops | Concurrency | Duration(s) | QPS | acquire p50 | acquire p99 | release p50 | release p99 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['mode']} | {r['ops']} | {r['concurrency']} "
            f"| {r['duration_s']} | {r['qps']} "
            f"| {r['acquire_p50_ms']} | {r['acquire_p99_ms']} "
            f"| {r['release_p50_ms']} | {r['release_p99_ms']} |"
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nreport -> {report_path.relative_to(report_path.parents[2])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
