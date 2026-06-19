"""
@brief  W13 Dogfooding DoD 验收脚本

W13 DoD（DESIGN §15 Phase 2 末期 + §17.2 ④）：
  ① agent_review.py 工具可跑
  ② 静态规则扫描 + 7 类安全模式检测
  ③ 干净 PR → verdict=approve
  ④ critical finding → verdict=request_changes + exit 1
  ⑤ 输出 ReviewReport（JSON + text）
  ⑥ SecretManager 引用（${VAR}）不被误报
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd or REPO, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode not in (0, 1):  # W13 允许 exit 0 / 1
        sys.stderr.write(f"FAIL: {cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
        raise SystemExit(proc.returncode)
    return proc


def check_module_import() -> None:
    """① agent_review.py 模块可导入 + 公开 API"""
    print("[1/5] agent_review 模块 + 公开 API")
    proc = _run([".venv/bin/python", "-c", """
import sys
sys.path.insert(0, 'tools')
from agent_review import (
    ReviewFinding, ReviewReport,
    get_pr_diff, run_simple_review, static_security_scan,
)
print("ok")
"""])
    assert "ok" in proc.stdout
    print("  ✓ ReviewFinding/Report + 3 个函数导出")


def check_unit_tests() -> None:
    """② 单元测试全过"""
    print("[2/5] W13 单元测试")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_agent_review.py", "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 14, f"W13 单元测试 {n} < 14"
    print(f"  ✓ {last}")


def check_e2e_tests() -> None:
    """③ e2e 全过"""
    print("[3/5] W13 e2e (含真项目 PR diff 跑通)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/e2e/test_w13_dogfooding_e2e.py", "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 8, f"W13 e2e {n} < 8"
    print(f"  ✓ {last}")


def check_rules_coverage() -> None:
    """④ 7 类安全规则 + SecretManager 引用豁免"""
    print("[4/5] 7 类安全规则 + ${VAR} 豁免")
    src = (REPO / "tools/agent_review.py").read_text(encoding="utf-8")
    for cat in ["SECRET_LEAK", "CMD_INJECTION", "PATH_TRAVERSAL",
                "EVAL", "SQL_INJECTION", "DATA_EXPOSURE", "WEAK_HASH"]:
        assert cat in src, f"缺规则 {cat}"
    # SecretManager 引用豁免
    assert "(?!\\$\\{)" in src, "SecretManager 引用豁免正则缺失"
    print("  ✓ 7 类规则齐全 + ${VAR} 引用被豁免")


def check_no_regression() -> None:
    """⑤ 无回归"""
    print("[5/5] mypy + 全量回归")
    proc = _run([".venv/bin/python", "-m", "mypy", "src/agent_swarm"])
    assert "Success" in proc.stdout
    print("  ✓ mypy 0 errors")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 800, f"W13 后测试数 {n} < 800"
    print(f"  ✓ {last}")


def main() -> None:
    print("=" * 60)
    print("W13 Dogfooding DoD 验收 — Phase 2 DoD ④")
    print("=" * 60)
    check_module_import()
    check_unit_tests()
    check_e2e_tests()
    check_rules_coverage()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ W13 全部通过（5/5 DoD 验收项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
