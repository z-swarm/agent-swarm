"""
@module tools.verify_w38_dod
@brief  P5-W38 DoD 守门脚本——6 项检查 (Phase 5 收口)

P5-W38 Plan §5 Check 守门点:
  1. .git-blame-ignore-revs 存在 + 含 W36e commit hash 16a8556
  2. pyproject description 含 "Phase 5"
  3. pyproject keywords ≥10
  4. pyproject classifiers ≥3 (含 Python 3.11/3.12)
  5. RELEASE.md 存在 + 含 TestPyPI/PyPI upload 命令
  6. W36/W37 baseline 不破 (W36b/f + G-027/029 ≥41 case)

用法:
  .venv/bin/python tools/verify_w38_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
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

    # 1. .git-blame-ignore-revs 存在 + 含 16a8556
    try:
        blame_file = ROOT / ".git-blame-ignore-revs"
        ok = blame_file.exists()
        if ok:
            content = blame_file.read_text(encoding="utf-8")
            has_w36e = "16a8556" in content
            ok = has_w36e
        else:
            has_w36e = False
        results.append(_check("1. .git-blame-ignore-revs 含 W36e 16a8556", ok,
                              f"exists={blame_file.exists()} has_w36e={has_w36e}"))
    except Exception as exc:
        results.append(_check("1. .git-blame-ignore-revs", False, str(exc)))

    # 2. pyproject description 含 "Phase 5"
    try:
        with open(ROOT / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        desc = pyproject["project"]["description"]
        has_phase5 = "Phase 5" in desc
        no_phase2 = "Phase 2" not in desc
        ok = has_phase5 and no_phase2
        results.append(_check("2. pyproject description 含 Phase 5, 不含 Phase 2", ok,
                              f"description={desc[:60]}..."))
    except Exception as exc:
        results.append(_check("2. pyproject description", False, str(exc)))

    # 3. pyproject keywords ≥10
    try:
        keywords = pyproject["project"].get("keywords", [])
        ok = len(keywords) >= 10
        results.append(_check("3. pyproject keywords ≥10", ok,
                              f"keywords={len(keywords)}: {keywords}"))
    except Exception as exc:
        results.append(_check("3. pyproject keywords", False, str(exc)))

    # 4. pyproject classifiers ≥3 (含 Python 3.11/3.12)
    try:
        classifiers = pyproject["project"].get("classifiers", [])
        has_py311 = any("3.11" in c for c in classifiers)
        has_py312 = any("3.12" in c for c in classifiers)
        has_license = any("MIT" in c for c in classifiers)
        ok = len(classifiers) >= 3 and has_py311 and has_py312 and has_license
        results.append(_check("4. pyproject classifiers ≥3 + Python 3.11/3.12 + MIT", ok,
                              f"classifiers={len(classifiers)} py311={has_py311} py312={has_py312} mit={has_license}"))
    except Exception as exc:
        results.append(_check("4. pyproject classifiers", False, str(exc)))

    # 5. RELEASE.md 存在 + 含 upload 命令
    try:
        release_file = ROOT / "RELEASE.md"
        ok = release_file.exists()
        if ok:
            content = release_file.read_text(encoding="utf-8")
            has_testpypi = "twine upload --repository testpypi" in content
            has_pypi = "twine upload dist/" in content
            has_token = "pypirc" in content
            ok = has_testpypi and has_pypi and has_token
        else:
            has_testpypi = has_pypi = has_token = False
        results.append(_check("5. RELEASE.md 含 TestPyPI/PyPI upload 命令", ok,
                              f"exists={release_file.exists()} testpypi={has_testpypi} pypi={has_pypi} pypirc={has_token}"))
    except Exception as exc:
        results.append(_check("5. RELEASE.md", False, str(exc)))

    # 6. W36/W37 baseline 不破 (≥41 case)
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
    results.append(_check(f"6. W36/W37 baseline 不破 (≥41 case)", ok,
                          f"rc={rc} passed={w36_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W38 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
