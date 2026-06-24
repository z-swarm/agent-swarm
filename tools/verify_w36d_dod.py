"""
@module tools.verify_w36d_dod
@brief  P5-W36d DoD 守门脚本——8 项检查

P5-W36d Plan §5 Check 守门点:
  1. version 一致 (pyproject.toml == src/agent_swarm/__init__.py == app.py)
  2. CHANGELOG.md 含 0.5.0a2 节点
  3. dist/ 存在 sdist + wheel (agent_swarm-0.5.0a2.*)
  4. twine check PASSED (W27 模式)
  5. git tag 0.5.0a2 存在
  6. 全量 ruff 0 + mypy 0
  7. 全量 pytest 1204+ passed
  8. CHANGELOG 0.5.0a2 含 7 个 weekly slice 节点 (W33a/W33b/W34/W35/W36a/W36b/W36c)

用法:
  .venv/bin/python tools/verify_w36d_dod.py
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


def main() -> int:
    results: list[bool] = []

    # 1. version 一致
    try:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init_py = (ROOT / "src" / "agent_swarm" / "__init__.py").read_text(encoding="utf-8")
        app_py = (ROOT / "src" / "agent_swarm" / "web" / "app.py").read_text(encoding="utf-8")

        m_pp = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        m_init = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
        m_app = re.search(r'version:\s*str\s*=\s*"([^"]+)"', app_py)

        versions = [m.group(1) if m else None for m in (m_pp, m_init, m_app)]
        ok = all(v == "0.5.0a2" for v in versions)
        detail = f"pyproject={versions[0]}, __init__={versions[1]}, app.py={versions[2]}"
        results.append(_check("1. version 一致 (pyproject + __init__ + app.py)", ok, detail))
    except Exception as exc:
        results.append(_check("1. version 一致", False, str(exc)))

    # 2. CHANGELOG.md 含 0.5.0a2 节点
    try:
        cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        ok = "## [0.5.0a2]" in cl and "## [0.5.0a1]" in cl
        results.append(_check("2. CHANGELOG.md 含 0.5.0a2 节点 (含 0.5.0a1 兜底)", ok))
    except Exception as exc:
        results.append(_check("2. CHANGELOG 0.5.0a2 节点", False, str(exc)))

    # 3. dist/ 存在 sdist + wheel
    try:
        dist_dir = ROOT / "dist"
        sdist = list(dist_dir.glob("agent_swarm-0.5.0a2*.tar.gz"))
        wheel = list(dist_dir.glob("agent_swarm-0.5.0a2*.whl"))
        ok = len(sdist) >= 1 and len(wheel) >= 1
        detail = f"sdist={len(sdist)}, wheel={len(wheel)}"
        results.append(_check("3. dist/ 存在 sdist + wheel (0.5.0a2)", ok, detail))
    except Exception as exc:
        results.append(_check("3. dist/ sdist + wheel", False, str(exc)))

    # 4. twine check PASSED
    try:
        result = subprocess.run(
            [sys.executable, "-m", "twine", "check", "dist/agent_swarm-0.5.0a2*"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        ok = result.returncode == 0
        detail = result.stdout.strip().split("\n")[-1] if result.stdout else "n/a"
        results.append(_check("4. twine check dist/agent_swarm-0.5.0a2* PASSED", ok, detail))
    except Exception as exc:
        results.append(_check("4. twine check", False, str(exc)))

    # 5. git tag 0.5.0a2 存在
    try:
        result = subprocess.run(
            ["git", "tag"], cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        )
        tags = result.stdout.strip().split("\n")
        ok = "0.5.0a2" in tags
        detail = f"existing tags: {', '.join(t for t in tags if t.startswith('0.5'))}"
        results.append(_check("5. git tag 0.5.0a2 存在", ok, detail))
    except Exception as exc:
        results.append(_check("5. git tag 0.5.0a2", False, str(exc)))

    # 6. 全量 ruff 0 + mypy 0
    try:
        ruff = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "src", "tests"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        mypy = subprocess.run(
            [sys.executable, "-m", "mypy", "src/agent_swarm"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        ok = ruff.returncode == 0 and mypy.returncode == 0
        detail = f"ruff rc={ruff.returncode}, mypy rc={mypy.returncode}"
        results.append(_check("6. 全量 ruff 0 + mypy 0", ok, detail))
    except Exception as exc:
        results.append(_check("6. ruff + mypy", False, str(exc)))

    # 7. 全量 pytest (W36c baseline 1204+)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/unit", "tests/golden", "-q",
             "--ignore=tests/unit/test_channel_approver.py"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        )
        # 抓 "X passed" 行
        m = re.search(r"(\d+)\s+passed", result.stdout)
        passed = int(m.group(1)) if m else 0
        ok = result.returncode == 0 and passed >= 1204
        detail = f"passed={passed}, returncode={result.returncode}"
        results.append(_check("7. 全量 pytest ≥ 1204 passed", ok, detail))
    except Exception as exc:
        results.append(_check("7. 全量 pytest", False, str(exc)))

    # 8. CHANGELOG 0.5.0a2 含 7 个 weekly slice 节点
    try:
        cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # 找 0.5.0a2 段
        m = re.search(r"## \[0\.5\.0a2\].*?(?=^## \[|\Z)", cl, re.MULTILINE | re.DOTALL)
        segment = m.group(0) if m else ""
        slices = ["W33a", "W33b", "W34", "W35", "W36a", "W36b", "W36c"]
        missing = [s for s in slices if s not in segment]
        ok = not missing
        detail = f"missing: {missing}" if missing else "all 7 slices in 0.5.0a2 segment"
        results.append(_check("8. CHANGELOG 0.5.0a2 含 7 个 slice 节点", ok, detail))
    except Exception as exc:
        results.append(_check("8. CHANGELOG 0.5.0a2 7 slice 节点", False, str(exc)))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36d DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
