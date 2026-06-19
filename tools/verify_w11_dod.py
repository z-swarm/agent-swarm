"""
@brief  W11 Approval Flow 卡片模式 DoD 验收脚本

W11 DoD（DESIGN §15 Phase 2 + 升级 P2-3.4 脚本模式 → 卡片模式）：
  ① ChannelApprover 适配 ApprovalFlow（异步 approver）
  ② 异步等待用户回复（approve/deny/超时 三种路径）
  ③ 接入 SecurityPolicy 高风险工具（run_command + MCP HIGH/CRITICAL）
  ④ LarkConnector card action → ChannelApprover.handle_card_action 桥接
  ⑤ 失败兜底：超时 → fail-closed；send 失败 → fail-closed
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


def check_module_import() -> None:
    """① ChannelApprover 模块可导入"""
    print("[1/5] ChannelApprover 模块 + 公开 API")
    proc = _run([".venv/bin/python", "-c", """
from agent_swarm.security.channel_approver import (
    ChannelApprover, ApprovalRequest,
)
from agent_swarm.security import ApprovalFlow, Approver
import inspect
# 验证 ApprovalFlow.request_approval 是 async
assert inspect.iscoroutinefunction(ApprovalFlow.request_approval), \
    "ApprovalFlow.request_approval 应是 async"
print("ok")
"""])
    assert "ok" in proc.stdout
    print("  ✓ ChannelApprover + ApprovalRequest 导出 + ApprovalFlow 已升级为 async")


def check_unit_tests() -> None:
    """② 单元测试全过"""
    print("[2/5] W11 单元测试 (ChannelApprover)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_channel_approver.py",
                 "tests/unit/test_approval.py",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 18, f"W11 单元测试 {n} < 18 (基线 7 + 11 新增)"
    print(f"  ✓ {last}")


def check_e2e_tests() -> None:
    """③ e2e 全过"""
    print("[3/5] W11 e2e (含 run_command + MCP 集成)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/e2e/test_w11_approval_e2e.py",
                 "tests/e2e/test_w10_approval_e2e.py",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 17, f"W11 e2e 测试 {n} < 17"
    print(f"  ✓ {last}")


def check_run_command_integration() -> None:
    """④ run_command 集成链路（高风险命令 → 飞书卡片审批）"""
    print("[4/5] run_command 集成校验")
    cli = (REPO / "src/agent_swarm/tools/builtin/shell.py").read_text(encoding="utf-8")
    assert "await self._approval.request_approval" in cli, \
        "RunCommandTool 应 await ApprovalFlow（支持异步 approver）"
    print("  ✓ RunCommandTool.invoke() 走 await approval_flow.request_approval")


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
    assert n >= 770, f"W11 后测试数 {n} < 770"
    print(f"  ✓ {last}")


def main() -> None:
    print("=" * 60)
    print("W11 Approval Flow 卡片模式 DoD 验收")
    print("=" * 60)
    check_module_import()
    check_unit_tests()
    check_e2e_tests()
    check_run_command_integration()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ W11 全部通过（5/5 DoD 验收项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
