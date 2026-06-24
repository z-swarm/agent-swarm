"""
@module tools.verify_w39_dod
@brief  P5-W39 DoD 守门脚本——5 项检查 (Phase 6 启动)

P5-W39 Plan §5 Check 守门点:
  1. docs/PHASE6-PLAN.md 存在 + 字数 ≥500
  2. PHASE6-PLAN.md 含 4 关键词 (1.0.0 / W40 / Phase 5 / TestPyPI)
  3. CHANGELOG.md 含 "Phase 6 启动" 节点
  4. ruff 0 / mypy 0
  5. W38/W37/W36 baseline 不破 (≥41 case)

用法:
  .venv/bin/python tools/verify_w39_dod.py
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

    # 1. PHASE6-PLAN.md 存在 + ≥500 字
    try:
        plan_file = ROOT / "docs" / "PHASE6-PLAN.md"
        ok = plan_file.exists()
        if ok:
            content = plan_file.read_text(encoding="utf-8")
            # 去除 markdown 标记 (粗体 / 标题 / 列表 / 链接) 估算实质字数
            text = re.sub(r"[#*`|\-\[\]()>]", "", content)
            text = re.sub(r"\s+", "", text)
            char_count = len(text)
            ok = char_count >= 500
        else:
            char_count = 0
        results.append(_check("1. PHASE6-PLAN.md 存在 + ≥500 字", ok,
                              f"exists={plan_file.exists()} chars={char_count}"))
    except Exception as exc:
        results.append(_check("1. PHASE6-PLAN.md 存在", False, str(exc)))

    # 2. PHASE6-PLAN.md 含 4 关键词
    try:
        if plan_file.exists():
            content = plan_file.read_text(encoding="utf-8")
            keywords = ["1.0.0", "W40", "Phase 5", "TestPyPI"]
            found = [k for k in keywords if k in content]
            ok = len(found) == len(keywords)
            results.append(_check("2. PHASE6-PLAN.md 含 4 关键词 (1.0.0/W40/Phase 5/TestPyPI)", ok,
                                  f"found={found}"))
        else:
            results.append(_check("2. PHASE6-PLAN.md 关键词", False, "PHASE6-PLAN.md 不存在"))
    except Exception as exc:
        results.append(_check("2. PHASE6-PLAN.md 关键词", False, str(exc)))

    # 3. CHANGELOG 含 "Phase 6 启动" 节点
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # W39 节点应含 Phase 6 启动
        ok = "Phase 6 启动" in changelog
        # 同时含 W39 引用
        has_w39 = "W39" in changelog
        ok = ok and has_w39
        results.append(_check("3. CHANGELOG 含 W39 'Phase 6 启动' 节点", ok,
                              f"phase6={ok} w39={has_w39}"))
    except Exception as exc:
        results.append(_check("3. CHANGELOG W39 节点", False, str(exc)))

    # 4. ruff 0 + mypy 0
    rc_ruff, _ = _run(
        [".venv/bin/ruff", "check",
         "src", "tools/agent_review.py", "tools/verify_w39_dod.py",
         "tools/verify_w36a_dod.py", "tools/verify_w36b_dod.py",
         "tools/verify_w36c_dod.py", "tools/verify_w36d_dod.py",
         "tools/verify_w36e_dod.py", "tools/verify_w36f_dod.py",
         "tools/verify_w36g_dod.py", "tools/verify_w37_dod.py",
         "tools/verify_w38_dod.py", "tools/verify_p5_dod.py"],
        timeout=60,
    )
    rc_mypy, _ = _run([".venv/bin/mypy", "src/agent_swarm", "tools/agent_review.py"], timeout=120)
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("4. ruff 0 + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 5. W36/W37/W38 baseline 不破 (≥41 case)
    rc, out = _run(
        [".venv/bin/pytest",
         "tests/unit/test_web_review.py",
         "tests/unit/test_web_review_async.py",
         "tests/golden/test_g027_review_e2e.py",
         "tests/golden/test_g029_review_async_e2e.py",
         "-q", "--tb=no"],
        timeout=120,
    )
    w36_passed = 0
    for line in out.splitlines():
        if " passed" in line and "::" not in line:
            parts = line.split()
            for p in parts:
                if p.isdigit():
                    w36_passed = max(w36_passed, int(p))
    ok = rc == 0 and w36_passed >= 41
    results.append(_check("5. W36/W37/W38 baseline 不破 (≥41 case)", ok,
                          f"rc={rc} passed={w36_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W39 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
