"""
@module tools.verify_w43_dod
@brief  P6-W43 DoD 守门脚本——8 项检查 (1.0.0-rc1 release 准备)

P6-W43 Plan §5 Check 守门点:
  1. W43a TUI _pump_events drain 模式 (队列非空时 get_nowait)
  2. version 三处一致 (pyproject / __init__ / app)
  3. CHANGELOG 1.0.0-rc1 节点
  4. dist 重建 (sdist + wheel) 存在
  5. twine check PASSED
  6. git tag 1.0.0-rc1 存在
  7. ruff 0 / mypy 0
  8. pytest 全量 ≥1368 passed (W41 baseline 守门)

用法:
  .venv/bin/python tools/verify_w43_dod.py
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


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str]:
    result = subprocess.run(
        cmd, cwd=cwd or str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    results: list[bool] = []

    # 1. W43a TUI drain 模式
    try:
        tui_src = (ROOT / "src" / "agent_swarm" / "tui" / "app.py").read_text(encoding="utf-8")
        has_drain = "max_drain = 1000" in tui_src
        has_get_nowait = "get_nowait" in tui_src
        ok = has_drain and has_get_nowait
        results.append(_check(
            "1. TUI _pump_events drain 模式 (max_drain=1000 + get_nowait)",
            ok, f"max_drain={has_drain} get_nowait={has_get_nowait}",
        ))
    except Exception as exc:
        results.append(_check("1. TUI drain 模式", False, str(exc)))

    # 2. version 三处一致
    try:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init_py = (ROOT / "src" / "agent_swarm" / "__init__.py").read_text(encoding="utf-8")
        app_py = (ROOT / "src" / "agent_swarm" / "web" / "app.py").read_text(encoding="utf-8")
        v_pp = re.search(r'version\s*=\s*"([^"]+)"', pyproject).group(1)
        v_init = re.search(r'__version__\s*=\s*"([^"]+)"', init_py).group(1)
        v_app = re.search(r'version:\s*str\s*=\s*"([^"]+)"', app_py).group(1)
        ok = v_pp == v_init == v_app == "1.0.0-rc1"
        results.append(_check(
            "2. version 三处一致 (pyproject / __init__ / app = 1.0.0-rc1)",
            ok, f"pyproject={v_pp} __init__={v_init} app={v_app}",
        ))
    except Exception as exc:
        results.append(_check("2. version 三处一致", False, str(exc)))

    # 3. CHANGELOG 1.0.0-rc1 节点
    try:
        cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        has_node = "## [1.0.0-rc1]" in cl
        has_w43a = "W43a" in cl
        has_w43c = "W43c" in cl
        has_known = "W43b TestPyPI" in cl
        ok = has_node and has_w43a and has_w43c and has_known
        results.append(_check(
            "3. CHANGELOG 1.0.0-rc1 节点含 W43a + W43c + W43b 已知限制",
            ok, f"node={has_node} W43a={has_w43a} W43c={has_w43c} known={has_known}",
        ))
    except Exception as exc:
        results.append(_check("3. CHANGELOG 1.0.0-rc1", False, str(exc)))

    # 4. dist 重建
    sdist = list((ROOT / "dist").glob("agent_swarm-1.0.0rc1*.tar.gz"))
    wheel = list((ROOT / "dist").glob("agent_swarm-1.0.0rc1*.whl"))
    ok = bool(sdist) and bool(wheel)
    results.append(_check(
        "4. dist 重建 (sdist + wheel 存在)",
        ok, f"sdist={len(sdist)} wheel={len(wheel)}",
    ))

    # 5. twine check
    rc, out = _run(
        [".venv/bin/twine", "check"] + [str(p) for p in sdist + wheel],
        timeout=60,
    )
    twine_pass = "PASSED" in out
    ok = rc == 0 and twine_pass
    results.append(_check("5. twine check dist/agent_swarm-1.0.0rc1* PASSED", ok, f"rc={rc}"))

    # 6. git tag 1.0.0-rc1 存在
    rc, out = _run(["git", "tag", "--list"])
    has_tag = "1.0.0-rc1" in out
    results.append(_check("6. git tag 1.0.0-rc1 存在 (本地)", has_tag, f"tags={out.strip()}"))

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
         "tools/verify_w42_dod.py", "tools/verify_w43_dod.py",
         "tools/multi_worker_smoke.py", "tools/verify_p5_dod.py"],
        timeout=60,
    )
    rc_mypy, _ = _run(
        [".venv/bin/mypy", "src/agent_swarm", "tools/agent_review.py"],
        timeout=120,
    )
    ok = rc_ruff == 0 and rc_mypy == 0
    results.append(_check("7. ruff 0 + mypy 0", ok, f"ruff={rc_ruff} mypy={rc_mypy}"))

    # 8. pytest 全量 ≥1368 passed
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
    ok = rc == 0 and full_passed >= 1368
    results.append(_check("8. pytest 全量 ≥1368 passed (W41 baseline 守门)", ok,
                          f"rc={rc} passed={full_passed}"))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W43 DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
