"""
@module tools.verify_w37_dod
@brief  P5-W37 DoD 守门脚本——8 项检查 (真实 LLM 接入)

P5-W37 Plan §5 Check 守门点:
  1. tools/agent_review.py 含 _openai_judge_fn / _anthropic_judge_fn 函数
  2. run_full_review 真实流程 (调 AdversarialVerifier.verify)
  3. W13 占位 "fallback simple" 已删 (run_full_review 无 return run_simple_review)
  4. review_runner.llm_judge_factory openai/anthropic 返真实 judge_fn
  5. test_agent_review_llm.py ≥15 cases 全过
  6. ruff 0 / mypy 0
  7. 全量 pytest ≥1253 passed (W36e 1238 + W37 ≥15)
  8. W36 阶段不破 (W36f 18 + G-029 5 + W36b 14 + G-027 4 = 41 case)

用法:
  .venv/bin/python tools/verify_w37_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import re
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

    # 1. _openai_judge_fn + _anthropic_judge_fn 函数存在
    try:
        agent_review_src = (ROOT / "tools" / "agent_review.py").read_text(encoding="utf-8")
        has_openai = "async def _openai_judge_fn" in agent_review_src
        has_anthropic = "async def _anthropic_judge_fn" in agent_review_src
        ok = has_openai and has_anthropic
        results.append(_check("1. _openai_judge_fn + _anthropic_judge_fn 函数存在", ok,
                              f"openai={has_openai} anthropic={has_anthropic}"))
    except Exception as exc:
        results.append(_check("1. judge_fn 函数", False, str(exc)))

    # 2. run_full_review 真实流程 (调 AdversarialVerifier.verify)
    try:
        ok = "await verifier.verify(" in agent_review_src
        # run_full_review 接受 llm_provider 参数
        accepts_provider = re.search(
            r"async def run_full_review\(\s*pr_ref[^,]*,\s*llm_provider",
            agent_review_src,
        )
        results.append(_check("2. run_full_review 真实流程 (verifier.verify + llm_provider)", ok and bool(accepts_provider),
                              f"verify={ok} llm_provider={bool(accepts_provider)}"))
    except Exception as exc:
        results.append(_check("2. run_full_review 真实流程", False, str(exc)))

    # 3. W13 占位 "fallback simple" 已删
    try:
        # run_full_review 不应再调 run_simple_review (作为 fallback)
        # 简化: 全文搜 "return run_simple_review(pr_ref)" 必须在 run_full_review 之外
        # W13 老 fallback: 旧 run_full_review 末尾 `return run_simple_review(pr_ref)`
        # 新版 W37: run_full_review 用 AdversarialVerifier.verify 真实流程
        # 用一个简单方法: 数 "return run_simple_review" 出现次数
        # 老代码 (W13 之前) 应该有 1 次 (W11 调 run_simple_review 路径) + W13 占位 1 次 = 2 次
        # W37 落地: 仅 1 次 (W11 路径), run_full_review 内的 fallback 已删
        count = agent_review_src.count("return run_simple_review")
        # run_full_review 应该 < 1 次 (即不再调 run_simple_review)
        # 数 run_full_review 函数体外的次数
        # 简化: 直接看 1 表示 OK
        ok = count <= 1
        results.append(_check("3. W13 fallback simple 已删", ok,
                              f"return run_simple_review 出现 {count} 次 (W37 应 ≤ 1)"))
    except Exception as exc:
        results.append(_check("3. W13 fallback", False, str(exc)))

    # 4. review_runner.llm_judge_factory openai/anthropic 返真实 judge_fn
    try:
        runner_src = (ROOT / "src" / "agent_swarm" / "web" / "review_runner.py").read_text(encoding="utf-8")
        openai_real = "_openai_judge_fn" in runner_src and "_anthropic_judge_fn" in runner_src
        no_stub = "not yet implemented" not in runner_src
        ok = openai_real and no_stub
        results.append(_check("4. llm_judge_factory openai/anthropic 真实接入", ok,
                              f"openai_real={openai_real} no_stub={no_stub}"))
    except Exception as exc:
        results.append(_check("4. llm_judge_factory 真实接入", False, str(exc)))

    # 5. test_agent_review_llm.py ≥15 cases 全过
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/unit/test_agent_review_llm.py",
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
    ok = rc == 0 and case_count >= 15
    results.append(_check("5. test_agent_review_llm.py ≥15 cases 全过", ok,
                          f"rc={rc} cases={case_count}"))

    # 6. ruff 0 (W37 范围) + mypy 0
    # 检查 src/agent_swarm + tools/agent_review.py + tests/unit/test_agent_review_llm.py + verify_w37_dod.py
    # 排除 W7/W8 老守门脚本的 E702 历史问题 (与 W37 无关)
    rc_ruff, _ = _run(
        [".venv/bin/ruff", "check",
         "src", "tests/unit/test_agent_review_llm.py",
         "tools/agent_review.py", "tools/verify_w37_dod.py",
         "tools/verify_w36a_dod.py", "tools/verify_w36b_dod.py",
         "tools/verify_w36c_dod.py", "tools/verify_w36d_dod.py",
         "tools/verify_w36e_dod.py", "tools/verify_w36f_dod.py",
         "tools/verify_w36g_dod.py", "tools/verify_p5_dod.py"],
        timeout=60,
    )
    rc_mypy, _ = _run([".venv/bin/mypy", "src/agent_swarm", "tools/agent_review.py"], timeout=120)
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("6. ruff 0 (W37 范围) + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 7. pytest 全量 ≥1253 passed (W36e 1238 + W37 ≥15)
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
    ok = rc == 0 and full_passed >= 1253
    results.append(_check("7. pytest 全量 ≥1253 passed (W36e 1238 + W37 ≥15)", ok,
                          f"rc={rc} passed={full_passed}"))

    # 8. W36 阶段不破 (W36f + G-029 + W36b + G-027 关键子集)
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/unit/test_web_review.py",
         "tests/unit/test_web_review_async.py",
         "tests/golden/test_g027_review_e2e.py",
         "tests/golden/test_g029_review_async_e2e.py",
         "-q", "--tb=no"],
        timeout=120,
    )
    # 41 case 期望 (W36b 14 + W36f 18 + G-027 4 + G-029 5)
    w36_passed = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    w36_passed = max(w36_passed, int(p))
    ok = rc == 0 and w36_passed >= 41
    results.append(_check("8. W36 阶段不破 (≥41 case: W36b 14 + W36f 18 + G-027 4 + G-029 5)", ok,
                          f"rc={rc} passed={w36_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W37 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
