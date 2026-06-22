"""
@brief  P4 阶段 DoD 验收脚本

对 W22 + W23 收尾:
  - W22-1 WorktreeManager 核心 (acquire/release/list/cleanup)
  - W22-2 并发安全 (per-tenant lock)
  - W22-3 15+ 单元测试
  - W23-1 ${WORKTREE_PATH} 占位符注入
  - W23-2 WorktreeIntegration 高层 API
  - W23-3 example w22_mcp_worktree.yaml
  - W23-4 G-021 Golden Case
  - W23-5 tools/bench_worktree.py 压测
  - W23-6 ruff 0 + mypy 0 + 全量测试

@note  通过条件：本脚本 exit 0
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _py() -> str:
    if sys.platform == "win32":
        return str(REPO / ".venv-win" / "Scripts" / "python.exe")
    return str(REPO / ".venv" / "bin" / "python")


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd or REPO, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"FAIL: {cmd}\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
        raise SystemExit(proc.returncode)
    return proc


def check_w22_core() -> None:
    """W22-1: WorktreeManager 核心 import + 基础用法"""
    print("[W22-1] WorktreeManager 核心 API")
    proc = _run([_py(), "-c", """
from agent_swarm.worktree import WorktreeManager, WorktreeHandle
from agent_swarm.worktree.manager import (
    WorktreeError, WorktreeRepoError, WorktreeConflictError, _is_git_repo, _sanitize,
)
assert WorktreeManager is not None
assert WorktreeHandle is not None
print('imports OK')
"""])
    assert "imports OK" in proc.stdout
    print("  ✓ WorktreeManager / WorktreeHandle / 异常类 全部 import")


def check_w22_integration() -> None:
    """W23-1 + W23-2: 占位符 + 高层 API"""
    print("[W23-1+2] ${WORKTREE_PATH} 占位符 + WorktreeIntegration")
    proc = _run([_py(), "-c", """
from agent_swarm.worktree import (
    PLACEHOLDER, WorktreeIntegration,
    substitute_placeholders, validate_config, find_placeholders,
)
assert PLACEHOLDER == '${WORKTREE_PATH}'
print('integration OK')
"""])
    assert "integration OK" in proc.stdout
    print("  ✓ PLACEHOLDER + substitute + validate + find_placeholders")


def check_w22_unit_tests() -> None:
    """W22-3: 单元测试通过"""
    print("[W22-3] WorktreeManager 单元测试")
    proc = _run([
        _py(), "-m", "pytest",
        "tests/unit/test_worktree_manager.py",
        "tests/unit/test_worktree_integration.py",
        "-q", "--no-header", "--tb=no",
    ])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 26, f"W22 应 ≥ 26 tests, 实际 {n}"
    print(f"  ✓ {last}")


def check_w23_golden() -> None:
    """W23-4: G-021 Golden Case 通过"""
    print("[W23-4] G-021 Golden Case")
    proc = _run([
        _py(), "-m", "pytest",
        "tests/golden/test_g021_worktree_isolation.py",
        "-v", "--no-header", "--tb=no",
    ])
    assert "3 passed" in proc.stdout, f"G-021 失败: {proc.stdout}"
    print("  ✓ 3/3 G-021 测试通过")


def check_w23_example() -> None:
    """W23-3: example yaml 存在 + 含 ${WORKTREE_PATH}"""
    print("[W23-3] example w22_mcp_worktree.yaml")
    p = REPO / "examples" / "w22_mcp_worktree.yaml"
    assert p.exists(), f"example 不存在: {p}"
    content = p.read_text(encoding="utf-8")
    assert "${WORKTREE_PATH}" in content, "example 缺占位符"
    assert "mcp_servers" in content
    assert "filesystem" in content
    print(f"  ✓ {p.relative_to(REPO)} 含占位符 + mcp_servers")


def check_w23_bench() -> None:
    """W23-5: bench 脚本可执行 + 报告生成"""
    print("[W23-5] tools/bench_worktree.py")
    proc = _run([
        _py(), "tools/bench_worktree.py", "-n", "10", "-c", "5",
    ])
    assert "report ->" in proc.stdout
    report = REPO / "docs" / "WORKTREE-BENCH.md"
    assert report.exists(), f"bench 报告未生成: {report}"
    print("  ✓ bench + docs/WORKTREE-BENCH.md")


def check_no_regression() -> None:
    """W23-6: 全量 ruff + mypy + pytest"""
    print("[W23-6] 全量 ruff + mypy + pytest")
    # ruff
    proc = _run([_py(), "-m", "ruff", "check", "src/", "tests/"])
    assert proc.returncode == 0, f"ruff failed: {proc.stderr}"
    print("  ✓ ruff 0 errors")
    # mypy
    proc = _run([_py(), "-m", "mypy", "src/"])
    assert "Success" in proc.stdout, f"mypy failed: {proc.stdout}"
    print("  ✓ mypy 0 errors")
    # pytest
    proc = _run([
        _py(), "-m", "pytest", "tests/",
        "-q", "--no-header", "--tb=no",
    ])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 1000, f"P4 后测试数 {n} < 1000"
    print(f"  ✓ {last}")


def check_changelog() -> None:
    """W23-7: CHANGELOG 提及 W22/W23"""
    print("[W23-7] CHANGELOG 0.4.0 节点")
    p = REPO / "CHANGELOG.md"
    if not p.exists():
        print("  ⚠ CHANGELOG.md 不存在, 跳过")
        return
    content = p.read_text(encoding="utf-8")
    # 软要求: 至少提到 W22 或 W23
    if "W22" in content or "W23" in content or "0.4.0" in content:
        print("  ✓ CHANGELOG 涵盖 P4 节点")
    else:
        print("  ⚠ CHANGELOG 未提 W22/W23 (建议补)")


def main() -> None:
    print("=" * 60)
    print("P4 阶段 DoD 验收 (W22-W23 MCP Worktree 隔离)")
    print("=" * 60)
    check_w22_core()
    check_w22_integration()
    check_w22_unit_tests()
    check_w23_golden()
    check_w23_example()
    check_w23_bench()
    check_no_regression()
    check_changelog()
    print()
    print("=" * 60)
    print("✅ P4 阶段全部通过 (W22 WorktreeManager + W23 MCP 集成)")
    print("=" * 60)


if __name__ == "__main__":
    main()
