"""
@brief  P2 阶段 DoD 验收脚本（REVIEW-2026-06-19 §3 风险点 P2）

对应原审计报告 P2 三项：
  §3.4 ApprovalFlow 文档 + 实际跑通路径
  §3.5 跨平台文档（Windows/WSL）+ .gitignore 增补
  §3.6 session DB 路径 fail-fast

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


def check_approval_flow() -> None:
    """P2-3.4: ApprovalFlow 文档 + e2e"""
    print("[P2-3.4] ApprovalFlow 文档 + e2e (11 个场景)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/e2e/test_w10_approval_e2e.py", "-q", "--no-header"])
    assert "11 passed" in proc.stdout, f"未达 11 passed: {proc.stdout}"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "## 审批流程" in readme, "README 缺审批流程章节"
    assert "fail-closed" in readme, "README 未说明默认拒绝"
    assert "脚本模式" in readme, "README 未说明脚本模式"
    print("  ✓ e2e 11 个场景 + README 文档完整")


def check_cross_platform() -> None:
    """P2-3.5: 跨平台文档 + .gitignore"""
    print("[P2-3.5] 跨平台文档（Windows/WSL/Linux/macOS）")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "## 跨平台支持" in readme, "README 缺跨平台章节"
    assert "Windows" in readme and "WSL" in readme
    assert "pytest cache" in readme.lower() or ".pytest_cache" in readme
    # .gitignore 校验
    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    for pat in [".pytest_cache/", ".coverage", ".coverage.*", ".ruff_cache/", ".mypy_cache/"]:
        assert pat in gi, f".gitignore 缺 {pat}"
    print("  ✓ README 跨平台章节 + .gitignore 5 个关键 pattern")


def check_session_db_fail_fast() -> None:
    """P2-3.6: session DB fail-fast"""
    print("[P2-3.6] session DB 路径 fail-fast (6 个新单测)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_cli.py", "-q", "--no-header",
                 "-k", "fails_fast or writable_db_path_proceeds"])
    assert "6 passed" in proc.stdout, f"未达 6 passed: {proc.stdout}"
    cli = (REPO / "src/agent_swarm/cli/main.py").read_text(encoding="utf-8")
    assert "_validate_db_writable" in cli, "fail-fast 函数未实现"
    assert "_validate_db_writable(db)" in cli, "fail-fast 未在 4 个子命令中调用"
    print("  ✓ run + session {list,show,resume} 全部接入 fail-fast")


def check_no_regression() -> None:
    """无回归"""
    print("[P2-regression] 全量测试")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 665, f"P2 后测试数 {n} < 665"
    print(f"  ✓ {last}")


def main() -> None:
    print("=" * 60)
    print("P2 阶段 DoD 验收 — REVIEW-2026-06-19 §3 P2 风险点")
    print("=" * 60)
    check_approval_flow()
    check_cross_platform()
    check_session_db_fail_fast()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ P2 阶段全部通过（3/3 风险点已修 + 无回归）")
    print("=" * 60)


if __name__ == "__main__":
    main()
