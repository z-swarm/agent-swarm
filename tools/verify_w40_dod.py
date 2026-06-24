"""
@module tools.verify_w40_dod
@brief  P5-W40 DoD 守门脚本——8 项检查 (Redis task store 接入)

P5-W40 Plan §5 Check 守门点:
  1. TaskStore Protocol 定义
  2. MemoryTaskStore 包装现有 (W36f 兼容)
  3. RedisTaskStore 真实实现 (用 redis.asyncio)
  4. create_app 接 task_store
  5. CLI --web-task-store / --web-redis-dsn 选项
  6. test_web_review_task_store.py ≥10 cases 全过
  7. ruff 0 / mypy 0
  8. 全量 pytest ≥1266 passed (W39 1256 + W40 ≥10)

用法:
  .venv/bin/python tools/verify_w40_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    result = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    results: list[bool] = []

    # 1. TaskStore Protocol 定义
    try:
        runner_src = (ROOT / "src" / "agent_swarm" / "web" / "review_runner.py").read_text(encoding="utf-8")
        has_protocol = "class TaskStore(Protocol)" in runner_src
        # 5 个方法名
        methods = ["create_task", "get_task", "update_task", "subscribe_task", "cleanup_expired"]
        has_all = all(f"async def {m}" in runner_src for m in methods)
        ok = has_protocol and has_all
        results.append(_check("1. TaskStore Protocol 定义 (5 方法)", ok,
                              f"protocol={has_protocol} methods={has_all}"))
    except Exception as exc:
        results.append(_check("1. TaskStore Protocol", False, str(exc)))

    # 2. MemoryTaskStore 包装
    try:
        has_memory = "class MemoryTaskStore" in runner_src
        # W36f 函数调用 MemoryTaskStore
        # 守门: MemoryTaskStore 存在 + 5 方法 (create/get/update/subscribe/cleanup)
        mm_methods = ["create_task", "get_task", "update_task", "subscribe_task", "cleanup_expired"]
        mm_in_class = all(
            f"async def {m}" in runner_src[runner_src.index("class MemoryTaskStore"):]
            for m in mm_methods
        )
        ok = has_memory and mm_in_class
        results.append(_check("2. MemoryTaskStore 包装 (5 方法 async)", ok,
                              f"class={has_memory} methods={mm_in_class}"))
    except Exception as exc:
        results.append(_check("2. MemoryTaskStore", False, str(exc)))

    # 3. RedisTaskStore 真实实现
    try:
        has_redis = "class RedisTaskStore" in runner_src
        has_redis_import = "import redis.asyncio as redis_async" in runner_src
        has_factory = "def create_task_store" in runner_src
        ok = has_redis and has_redis_import and has_factory
        results.append(_check("3. RedisTaskStore 真实实现 + create_task_store 工厂", ok,
                              f"class={has_redis} import={has_redis_import} factory={has_factory}"))
    except Exception as exc:
        results.append(_check("3. RedisTaskStore", False, str(exc)))

    # 4. create_app 接 task_store
    try:
        app_src = (ROOT / "src" / "agent_swarm" / "web" / "app.py").read_text(encoding="utf-8")
        ok = "task_store: Any = None" in app_src and "app.state.task_store" in app_src
        results.append(_check("4. create_app 接 task_store 参数 + app.state 存储", ok))
    except Exception as exc:
        results.append(_check("4. create_app task_store", False, str(exc)))

    # 5. CLI --web-task-store / --web-redis-dsn
    try:
        cli_src = (ROOT / "src" / "agent_swarm" / "cli" / "main.py").read_text(encoding="utf-8")
        has_store_opt = '"--web-task-store"' in cli_src or "'--web-task-store'" in cli_src
        has_dsn_opt = '"--web-redis-dsn"' in cli_src or "'--web-redis-dsn'" in cli_src
        # cli 应传 create_app (task_store)
        has_pass = "create_task_store" in cli_src
        ok = has_store_opt and has_dsn_opt and has_pass
        results.append(_check("5. CLI --web-task-store / --web-redis-dsn + 传 create_app", ok,
                              f"store_opt={has_store_opt} dsn_opt={has_dsn_opt} pass={has_pass}"))
    except Exception as exc:
        results.append(_check("5. CLI 选项", False, str(exc)))

    # 6. test_web_review_task_store.py ≥10 cases 全过
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/unit/test_web_review_task_store.py",
         "-q", "--tb=no"],
        timeout=60,
    )
    case_count = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    case_count = max(case_count, int(p))
    ok = rc == 0 and case_count >= 10
    results.append(_check("6. test_web_review_task_store.py ≥10 cases 全过", ok,
                          f"rc={rc} cases={case_count}"))

    # 7. ruff 0 + mypy 0
    rc_ruff, _ = _run(
        [".venv/bin/ruff", "check",
         "src", "tools/agent_review.py",
         "tools/verify_w36a_dod.py", "tools/verify_w36b_dod.py",
         "tools/verify_w36c_dod.py", "tools/verify_w36d_dod.py",
         "tools/verify_w36e_dod.py", "tools/verify_w36f_dod.py",
         "tools/verify_w36g_dod.py", "tools/verify_w37_dod.py",
         "tools/verify_w38_dod.py", "tools/verify_w39_dod.py",
         "tools/verify_w40_dod.py", "tools/verify_p5_dod.py"],
        timeout=60,
    )
    rc_mypy, _ = _run(
        [".venv/bin/mypy", "src/agent_swarm", "tools/agent_review.py"],
        timeout=120,
    )
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("7. ruff 0 + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 8. pytest 全量 ≥1266 passed
    rc, out = _run(
        [".venv/bin/pytest", "tests/unit", "tests/golden", "-q", "--tb=no"],
        timeout=300,
    )
    full_passed = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    full_passed = max(full_passed, int(p))
    ok = rc == 0 and full_passed >= 1266
    results.append(_check("8. pytest 全量 ≥1266 passed (W39 1256 + W40 ≥10)", ok,
                          f"rc={rc} passed={full_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W40 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
