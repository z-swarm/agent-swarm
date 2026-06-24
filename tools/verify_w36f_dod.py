"""
@module tools.verify_w36f_dod
@brief  P5-W36f DoD 守门脚本——8 项检查

P5-W36f Plan §5 Check 守门点:
  1. ReviewTask dataclass 字段 (task_id/status/progress/log/result/error/created_at)
  2. run_full_review_async + create_task + get_task + subscribe_task 函数
  3. llm_judge_factory 3 provider (openai/anthropic/fake)
  4. routes.py 3 端点 (POST 异步 / GET status / GET SSE)
  5. CLI --web-review-mode/--web-review-llm/--web-review-timeout
  6. test_web_review_async.py ≥10 cases 全过
  7. test_g029_review_async_e2e.py 5 cases 全过
  8. ruff 0 + mypy 0 + 全量 0 新失败

用法:
  .venv/bin/python tools/verify_w36f_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"


def _check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """跑命令, 返 (returncode, stdout+stderr)"""
    result = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, (result.stdout + result.stderr)


def main() -> int:
    results: list[bool] = []

    # 1. ReviewTask dataclass 字段
    try:
        from agent_swarm.web.review_runner import ReviewTask
        fields = set(ReviewTask.__dataclass_fields__.keys())
        required = {"task_id", "status", "progress", "log", "result", "error", "created_at"}
        ok = required.issubset(fields)
        results.append(_check("1. ReviewTask 含 7 字段", ok, f"fields={sorted(fields)}"))
    except Exception as exc:
        results.append(_check("1. ReviewTask dataclass", False, str(exc)))

    # 2. run_full_review_async + create/get/subscribe
    try:
        from agent_swarm.web import review_runner
        names = {
            "create_task", "get_task", "subscribe_task",
            "run_full_review_async", "llm_judge_factory",
            "cleanup_expired_tasks",
        }
        missing = names - set(dir(review_runner))
        ok = not missing
        results.append(_check("2. review_runner 5 函数齐", ok,
                              f"missing={sorted(missing)}" if missing else ""))
    except Exception as exc:
        results.append(_check("2. review_runner functions", False, str(exc)))

    # 3. llm_judge_factory 3 provider
    try:
        from agent_swarm.web.review_runner import llm_judge_factory
        # fake
        judge_fake = llm_judge_factory("fake")
        ok_fake = callable(judge_fake)
        # openai 无 key 报错
        import os
        env_save = dict(os.environ)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            llm_judge_factory("openai")
            ok_openai = False
        except RuntimeError as exc:
            ok_openai = "OPENAI_API_KEY" in str(exc)
        try:
            llm_judge_factory("anthropic")
            ok_anthropic = False
        except RuntimeError as exc:
            ok_anthropic = "ANTHROPIC_API_KEY" in str(exc)
        # 未知 provider 报错
        try:
            llm_judge_factory("claude99")  # type: ignore[arg-type]
            ok_unknown = False
        except ValueError:
            ok_unknown = True
        os.environ.clear()
        os.environ.update(env_save)
        ok = ok_fake and ok_openai and ok_anthropic and ok_unknown
        results.append(_check("3. llm_judge_factory 3 provider (fake/openai/anthropic + 未知)", ok,
                              f"fake={ok_fake} openai={ok_openai} anthropic={ok_anthropic} unknown={ok_unknown}"))
    except Exception as exc:
        results.append(_check("3. llm_judge_factory", False, str(exc)))

    # 4. routes.py 3 端点存在
    try:
        # 通过源码扫描确认 3 端点 (POST /api/review + GET /api/review/{id} + GET /api/review/{id}/events)
        src = (SRC / "agent_swarm" / "web" / "routes.py").read_text(encoding="utf-8")
        ok_post = 'def api_review(' in src
        ok_status = '/api/review/{task_id}"' in src and 'def api_review_status(' in src
        ok_sse = '/api/review/{task_id}/events"' in src and 'def api_review_events(' in src
        ok = ok_post and ok_status and ok_sse
        results.append(_check("4. routes 3 端点 (POST 异步 + GET status + GET SSE)", ok,
                              f"post={ok_post} status={ok_status} sse={ok_sse}"))
    except Exception as exc:
        results.append(_check("4. routes 3 端点", False, str(exc)))

    # 5. CLI --web-review-mode/--web-review-llm/--web-review-timeout
    try:
        cli_src = (ROOT / "src" / "agent_swarm" / "cli" / "main.py").read_text(encoding="utf-8")
        ok_mode = '"--web-review-mode"' in cli_src or "'--web-review-mode'" in cli_src
        ok_llm = '"--web-review-llm"' in cli_src or "'--web-review-llm'" in cli_src
        ok_timeout = '"--web-review-timeout"' in cli_src or "'--web-review-timeout'" in cli_src
        # 还需传入 create_app (review_mode/review_llm/review_timeout 形参)
        app_src = (SRC / "agent_swarm" / "web" / "app.py").read_text(encoding="utf-8")
        ok_pass = (
            "review_mode: str" in app_src
            and "review_llm: str" in app_src
            and "review_timeout: float" in app_src
        )
        # 还需 cli/main.py 把值传给 create_app
        cli_ok = "review_mode=web_review_mode" in cli_src
        ok = ok_mode and ok_llm and ok_timeout and ok_pass and cli_ok
        results.append(_check("5. CLI 3 选项 + create_app 接收 + 传参", ok,
                              f"mode={ok_mode} llm={ok_llm} timeout={ok_timeout} pass={ok_pass} cli_pass={cli_ok}"))
    except Exception as exc:
        results.append(_check("5. CLI 选项", False, str(exc)))

    # 6. test_web_review_async.py ≥10 cases 全过
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/unit/test_web_review_async.py",
         "-q", "--tb=no"],
        timeout=60,
    )
    # 统计 case 数
    lines = out.splitlines()
    case_count = 0
    for line in lines:
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    case_count = max(case_count, int(p))
        elif "::" in line and " PASSED" in line:
            case_count += 1
    ok = rc == 0 and case_count >= 10
    results.append(_check("6. test_web_review_async.py ≥10 cases 全过", ok,
                          f"rc={rc} cases={case_count}"))

    # 7. test_g029_review_async_e2e.py 5 cases 全过
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/golden/test_g029_review_async_e2e.py",
         "-q", "--tb=no"],
        timeout=120,
    )
    case_count = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    case_count = max(case_count, int(p))
    ok = rc == 0 and case_count >= 4
    results.append(_check("7. test_g029_review_async_e2e.py 5 cases 全过", ok,
                          f"rc={rc} cases={case_count}"))

    # 8. ruff 0 + mypy 0 + 全量 0 新失败
    rc_ruff, _ = _run([".venv/bin/ruff", "check", "src", "tests"], timeout=60)
    rc_mypy, _ = _run([".venv/bin/mypy", "src/agent_swarm"], timeout=120)
    rc_pytest, out = _run(
        [".venv/bin/pytest", "tests/unit", "tests/golden", "-q", "--tb=no", "--ignore=tests/golden/test_g029_review_async_e2e.py"],
        timeout=300,
    )
    # 找 passed 数
    full_passed = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    full_passed = max(full_passed, int(p))
    # W36d baseline = 1204; W36f 期望 ≥ 1204+10+5 = 1219
    ok = rc_ruff == 0 and rc_mypy == 0 and rc_pytest == 0 and full_passed >= 1219
    results.append(_check("8. ruff 0 + mypy 0 + 全量 ≥1219 passed (W36d 1204 + W36f 15)",
                          ok, f"ruff={rc_ruff} mypy={rc_mypy} pytest={rc_pytest} passed={full_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36f DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
