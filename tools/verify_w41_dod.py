"""
@module tools.verify_w41_dod
@brief  P6-W41 DoD 守门脚本——8 项检查 (多 worker 部署实战)

P6-W41 Plan §5 Check 守门点:
  1. CLI --web-workers 选项 + 多 worker 启动分支
  2. app_factory (uvicorn factory 模式) 暴露
  3. routes.py 走 app.state.task_store (W40 闭环缺口修复)
  4. run_full_review_async 接 task_store 参数
  5. tools/multi_worker_smoke.py 存在 + 跑通 3/3
  6. tests/e2e/test_w41_multi_worker_e2e.py ≥8 cases 全过
  7. ruff 0 / mypy 0
  8. 全量 pytest ≥1280 passed (W40 1270 + W41 ≥10)
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

    # 1. CLI --web-workers 选项
    try:
        cli_src = (ROOT / "src" / "agent_swarm" / "cli" / "main.py").read_text(encoding="utf-8")
        has_opt = '"--web-workers"' in cli_src
        has_branch = "if web_workers > 1" in cli_src
        has_uv_factory = '"agent_swarm.web:app_factory"' in cli_src
        has_env_export = 'os.environ["WEB_TASK_STORE"]' in cli_src
        ok = has_opt and has_branch and has_uv_factory and has_env_export
        results.append(_check(
            "1. CLI --web-workers 选项 + 多 worker factory 启动分支", ok,
            f"opt={has_opt} branch={has_branch} factory={has_uv_factory} env={has_env_export}",
        ))
    except Exception as exc:
        results.append(_check("1. CLI --web-workers", False, str(exc)))

    # 2. app_factory 暴露
    try:
        web_init = (ROOT / "src" / "agent_swarm" / "web" / "__init__.py").read_text(encoding="utf-8")
        has_factory = "def app_factory" in web_init
        in_all = '"app_factory"' in web_init
        ok = has_factory and in_all
        results.append(_check(
            "2. app_factory() 暴露在 web 模块 + __all__", ok,
            f"def={has_factory} __all__={in_all}",
        ))
    except Exception as exc:
        results.append(_check("2. app_factory", False, str(exc)))

    # 3. routes.py 走 app.state.task_store
    try:
        routes_src = (ROOT / "src" / "agent_swarm" / "web" / "routes.py").read_text(encoding="utf-8")
        n_state = routes_src.count("app.state.task_store")
        n_module_legacy = (
            routes_src.count("_rr.create_task(")
            + routes_src.count("_rr.get_task(")
            + routes_src.count("_rr.subscribe_task(")
        )
        ok = n_state >= 4 and n_module_legacy == 0
        results.append(_check(
            "3. routes.py 走 app.state.task_store (W40 闭环, ≥4 处, 无 _rr.* 模块级)",
            ok, f"state_ref={n_state} legacy={n_module_legacy}",
        ))
    except Exception as exc:
        results.append(_check("3. routes.py 走 store", False, str(exc)))

    # 4. run_full_review_async 接 task_store
    try:
        runner_src = (ROOT / "src" / "agent_swarm" / "web" / "review_runner.py").read_text(encoding="utf-8")
        has_sig = "task_store: TaskStore | None = None" in runner_src
        has_helper = "async def _update" in runner_src
        has_helper2 = "async def _fetch" in runner_src
        ok = has_sig and has_helper and has_helper2
        results.append(_check(
            "4. run_full_review_async 接 task_store + _update/_fetch helper",
            ok, f"sig={has_sig} update_helper={has_helper} fetch_helper={has_helper2}",
        ))
    except Exception as exc:
        results.append(_check("4. run_full_review_async task_store", False, str(exc)))

    # 5. smoke 跑通
    smoke_path = ROOT / "tools" / "multi_worker_smoke.py"
    if not smoke_path.exists():
        results.append(_check("5. multi_worker_smoke.py 存在 + 跑通", False, "file not found"))
    else:
        rc, out = _run([".venv/bin/python", "tools/multi_worker_smoke.py"], timeout=120)
        ok = rc == 0 and "3/3 PASSED" in out
        results.append(_check("5. tools/multi_worker_smoke.py 跑通 3/3", ok, f"rc={rc}"))

    # 6. e2e
    rc, out = _run(
        [".venv/bin/pytest", "tests/e2e/test_w41_multi_worker_e2e.py", "-q", "--tb=no"],
        timeout=60,
    )
    case_count = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    case_count = max(case_count, int(p))
    ok = rc == 0 and case_count >= 8
    results.append(_check("6. test_w41_multi_worker_e2e.py ≥8 cases 全过", ok,
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
         "tools/verify_w40_dod.py", "tools/verify_w41_dod.py",
         "tools/multi_worker_smoke.py", "tools/verify_p5_dod.py"],
        timeout=60,
    )
    rc_mypy, _ = _run(
        [".venv/bin/mypy", "src/agent_swarm", "tools/agent_review.py"],
        timeout=120,
    )
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("7. ruff 0 + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 8. pytest 全量
    rc, out = _run(
        [".venv/bin/pytest", "tests/unit", "tests/golden", "tests/e2e", "-q", "--tb=no"],
        timeout=300,
    )
    full_passed = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    full_passed = max(full_passed, int(p))
    ok = rc == 0 and full_passed >= 1280
    results.append(_check("8. pytest 全量 ≥1280 passed (W40 1270 + W41 ≥10)", ok,
                          f"rc={rc} passed={full_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W41 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
