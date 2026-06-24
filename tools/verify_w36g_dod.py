"""
@module tools.verify_w36g_dod
@brief  P5-W36g DoD 守门脚本——8 项检查 (0.5.0 final release)

P5-W36g Plan §5 Check 守门点:
  1. version 一致 (pyproject / __init__ / app.py / base.html 4 处 = 0.5.0)
  2. CHANGELOG.md 含 0.5.0 节点
  3. dist/ 存在 0.5.0 sdist + wheel
  4. twine check dist/agent_swarm-0.5.0* PASSED
  5. git tag 0.5.0 存在 (新打, 指向 W36g commit)
  6. ruff 0 + mypy 0 (W36e baseline)
  7. 全量 pytest ≥1238 passed (W36e baseline)
  8. CHANGELOG 0.5.0 含 6 个 W36 slice 节点 (W36a/b/c/d/e/f)

用法:
  .venv/bin/python tools/verify_w36g_dod.py
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

    # 1. version 一致 (4 处)
    try:
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            pyproject_ver = tomllib.load(f)["project"]["version"]
        init_src = (ROOT / "src" / "agent_swarm" / "__init__.py").read_text(encoding="utf-8")
        init_ver = re.search(r'__version__\s*=\s*"([^"]+)"', init_src)
        init_ver = init_ver.group(1) if init_ver else None
        app_src = (ROOT / "src" / "agent_swarm" / "web" / "app.py").read_text(encoding="utf-8")
        app_ver = re.search(r'version:\s*str\s*=\s*"([^"]+)"', app_src)
        app_ver = app_ver.group(1) if app_ver else None
        base_src = (ROOT / "src" / "agent_swarm" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
        base_ver_set = set(re.findall(r'0\.5\.0[a-z0-9]*', base_src))
        ok = (
            pyproject_ver == "0.5.0"
            and init_ver == "0.5.0"
            and app_ver == "0.5.0"
            and base_ver_set == {"0.5.0"}
        )
        results.append(_check("1. version 一致 (pyproject + __init__ + app.py + base.html = 0.5.0)",
                              ok, f"pyproject={pyproject_ver} init={init_ver} app={app_ver} base={base_ver_set}"))
    except Exception as exc:
        results.append(_check("1. version 一致", False, str(exc)))

    # 2. CHANGELOG.md 含 0.5.0 节点
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # 找 0.5.0 节点 (不含 alpha 后缀的)
        m = re.search(r"^##\s+\[0\.5\.0\]\s+-\s+\d{4}-\d{2}-\d{2}", changelog, re.MULTILINE)
        ok = m is not None
        results.append(_check("2. CHANGELOG.md 含 0.5.0 节点", ok, f"match={m.group(0) if m else 'None'}"))
    except Exception as exc:
        results.append(_check("2. CHANGELOG 0.5.0 节点", False, str(exc)))

    # 3. dist/ 存在 0.5.0 sdist + wheel
    try:
        dist_dir = ROOT / "dist"
        sdist = list(dist_dir.glob("agent_swarm-0.5.0*.tar.gz"))
        wheel = list(dist_dir.glob("agent_swarm-0.5.0*.whl"))
        ok = len(sdist) >= 1 and len(wheel) >= 1
        results.append(_check("3. dist/ 存在 0.5.0 sdist + wheel", ok,
                              f"sdist={len(sdist)} wheel={len(wheel)}"))
    except Exception as exc:
        results.append(_check("3. dist 0.5.0", False, str(exc)))

    # 4. twine check dist/agent_swarm-0.5.0* PASSED
    try:
        rc, out = _run([".venv/bin/twine", "check", "dist/agent_swarm-0.5.0*"])
        ok = rc == 0 and "PASSED" in out
        # 提取 PASSED 数
        passed_count = out.count("PASSED")
        results.append(_check("4. twine check dist/agent_swarm-0.5.0* PASSED", ok,
                              f"rc={rc} passed={passed_count}"))
    except Exception as exc:
        results.append(_check("4. twine check", False, str(exc)))

    # 5. git tag 0.5.0 存在
    try:
        rc, out = _run(["git", "tag", "-l", "0.5.0"])
        ok = "0.5.0" in out.strip().splitlines()
        results.append(_check("5. git tag 0.5.0 存在", ok, f"tags={out.strip()}"))
    except Exception as exc:
        results.append(_check("5. git tag 0.5.0", False, str(exc)))

    # 6. ruff 0 + mypy 0
    rc_ruff, _ = _run([".venv/bin/ruff", "check", "src", "tests"])
    rc_mypy, _ = _run([".venv/bin/mypy", "src/agent_swarm"], timeout=120)
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("6. ruff 0 + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 7. pytest 全量 ≥1238 passed
    try:
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
        ok = rc == 0 and full_passed >= 1238
        results.append(_check("7. 全量 pytest ≥1238 passed (W36e baseline)", ok,
                              f"rc={rc} passed={full_passed}"))
    except Exception as exc:
        results.append(_check("7. pytest 全量", False, str(exc)))

    # 8. CHANGELOG 0.5.0 含 6 个 W36 slice 节点
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # 提取 0.5.0 整段 (到下一个 ## [ 或文末)
        m = re.search(r"^##\s+\[0\.5\.0\][^\n]*\n(.*?)(?=^##\s+\[|\Z)", changelog, re.MULTILINE | re.DOTALL)
        if m:
            segment = m.group(1)
            slices = ["W36a", "W36b", "W36c", "W36d", "W36e", "W36f"]
            missing = [s for s in slices if f"**{s}**" not in segment and s not in segment]
            ok = not missing
            results.append(_check("8. CHANGELOG 0.5.0 含 6 W36 slice 节点", ok,
                                  f"missing={missing}"))
        else:
            results.append(_check("8. CHANGELOG 0.5.0 段解析", False, "未找到 0.5.0 段"))
    except Exception as exc:
        results.append(_check("8. CHANGELOG 0.5.0 slice 引用", False, str(exc)))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36g DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
