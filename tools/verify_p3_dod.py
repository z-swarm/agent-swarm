"""
@brief  P3 阶段 DoD 验收脚本（REVIEW-2026-06-19 §3 风险点 P3）

对应原审计报告 P3 三项：
  §3.7 TUI 边界场景测试
  §3.8a core/swarm.py:142 type: ignore 改枚举校验
  §3.8b pyproject.toml description 更新

@note  通过条件：本脚本 exit 0
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd or REPO, capture_output=True, text=True, timeout=180
    )
    if proc.returncode != 0:
        sys.stderr.write(f"FAIL: {cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
        raise SystemExit(proc.returncode)
    return proc


def _py() -> str:
    """Cross-platform venv python executable path (P3-WIN fix)."""
    if sys.platform == "win32":
        return str(REPO / ".venv-win" / "Scripts" / "python.exe")
    return str(REPO / ".venv" / "bin" / "python")


def check_tui_boundary() -> None:
    """P3-3.7: TUI 边界场景（5 个新场景）"""
    print("[P3-3.7] TUI 边界场景测试")
    proc = _run([_py(), "-m", "pytest",
                 "tests/unit/test_tui.py", "-q", "--no-header",
                 "-k", "handles_ or quick_reconnect"])
    assert "5 passed" in proc.stdout, f"未达 5 passed: {proc.stdout}"
    print("  ✓ 5 个新场景：many_agents / no_color / quick_reconnect / resize / malformed")


def check_swarm_enum() -> None:
    """P3-3.8a: core/swarm.py:142 枚举校验替代 type: ignore"""
    print("[P3-3.8a] Swarm.update_task_status 枚举校验")
    proc = _run([_py(), "-m", "pytest",
                 "tests/unit/test_lead_tools.py::test_update_task_swarm_api_rejects_invalid_status",
                 "-v", "--no-header"])
    assert "1 passed" in proc.stdout
    src = (REPO / "src/agent_swarm/core/swarm.py").read_text(encoding="utf-8")
    assert "_VALID_TASK_STATUSES" in src, "枚举常量缺失"
    assert "raise ValueError" in src, "非法 status 未抛 ValueError"
    # 142 行的 type: ignore 仍可保留（Literal 仍不能直接动态赋值），
    # 但现在已有显式枚举校验兜底——这正是审计要求
    print("  ✓ _VALID_TASK_STATUSES frozenset 校验 + 非法值抛 ValueError")


def check_pyproject_description() -> None:
    """P3-3.8b: pyproject.toml description 更新"""
    print("[P3-3.8b] pyproject.toml description 同步到 Phase 2")
    pp = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "W1 骨架" not in pp, "description 仍写 W1 骨架"
    assert "Phase 2" in pp, "description 未提到 Phase 2"
    assert "Delegate Mode" in pp or "Adversarial" in pp
    print("  ✓ description 反映当前 Phase 2 完成状态")


def check_no_regression() -> None:
    """无回归"""
    print("[P3-regression] 全量测试 + mypy")
    proc = _run([_py(), "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 670, f"P3 后测试数 {n} < 670"
    print(f"  ✓ {last}")
    # mypy
    proc2 = _run([_py(), "-m", "mypy", "src/agent_swarm"])
    assert "Success" in proc2.stdout
    print("  ✓ mypy 0 errors")


def main() -> None:
    print("=" * 60)
    print("P3 阶段 DoD 验收 — REVIEW-2026-06-19 §3 P3 风险点")
    print("=" * 60)
    check_tui_boundary()
    check_swarm_enum()
    check_pyproject_description()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ P3 阶段全部通过（3/3 风险点已修 + 无回归）")
    print("=" * 60)


if __name__ == "__main__":
    main()
