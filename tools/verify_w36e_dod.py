"""
@module tools.verify_w36e_dod
@brief  P5-W36e DoD 守门脚本——5 项检查 (技术债清理)

P5-W36e Plan §5 Check 守门点:
  1. ruff format --check 0 欠债 (185 files already formatted)
  2. ruff check 0 errors
  3. mypy 0 errors
  4. pytest 全量 ≥1233 passed (W36f baseline 不破)
  5. git diff --stat 含 150 files (确认 W36e 改的范围)

用法:
  .venv/bin/python tools/verify_w36e_dod.py
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


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    result = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    results: list[bool] = []

    # 1. ruff format --check 0 欠债
    rc, out = _run([".venv/bin/ruff", "format", "--check", "src", "tests"])
    # 期望 "N files already formatted" 不含 "would reformat"
    already = re.search(r"(\d+)\s+files?\s+already\s+formatted", out)
    would = "would reformat" in out
    ok = rc == 0 and not would
    n = already.group(1) if already else "?"
    results.append(_check("1. ruff format --check 0 欠债", ok,
                          f"already_formatted={n} would_reformat={would} rc={rc}"))

    # 2. ruff check 0 errors
    rc, out = _run([".venv/bin/ruff", "check", "src", "tests"])
    ok = rc == 0
    results.append(_check("2. ruff check 0 errors", ok, f"rc={rc}"))

    # 3. mypy 0 errors
    rc, out = _run([".venv/bin/mypy", "src/agent_swarm"], timeout=120)
    ok = rc == 0
    results.append(_check("3. mypy 0 errors", ok, f"rc={rc}"))

    # 4. pytest 全量 ≥1233 passed
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
    ok = rc == 0 and full_passed >= 1233
    results.append(_check("4. pytest 全量 ≥1233 passed (W36f baseline)", ok,
                          f"rc={rc} passed={full_passed}"))

    # 5. 查 HEAD commit (W36e) 的 stat
    rc, out = _run(["git", "show", "--stat", "--format=", "HEAD"])
    file_count = 0
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+files?\s+changed", line)
        if m:
            file_count = int(m.group(1))
    ok = 145 <= file_count <= 200
    results.append(_check("5. HEAD (W36e commit) 含 ~150 files", ok,
                          f"HEAD files_changed={file_count} (expect 145-200)"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36e DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
