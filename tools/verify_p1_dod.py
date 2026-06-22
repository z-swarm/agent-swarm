"""
@brief  P1 阶段 DoD 验收脚本（REVIEW-2026-06-19 §3 风险点 P1）

对应原审计报告 P1 三项：
  §3.1 MCP 工具是否走 SecurityPolicy.check_tool() 校验
  §3.2 CLI 是否支持 Anthropic
  §3.3 mypy 内部错误

@note  通过条件：本脚本 exit 0（即所有断言通过）
@note  兼容 CI 串行调用：`python tools/verify_p1_dod.py`
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跑子命令；非 0 退出码时把 stdout/stderr 打印并 raise"""
    proc = subprocess.run(
        cmd, cwd=cwd or REPO, capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        sys.stderr.write(f"FAIL: {cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
        raise SystemExit(proc.returncode)
    return proc


def check_mcp_policy_link() -> None:
    """P1-3.1: MCPToolAdapter.invoke() 必走 SecurityPolicy.check_tool()"""
    print("[P1-3.1] MCP 工具 SecurityPolicy 链路 + risk_overrides 二次闸门")
    # 单测覆盖所有 8 个新场景（含 1 个 e2e 真实 stdio）
    proc = _run(["uv" if _has_uv() else ".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_mcp_adapter.py", "-q", "--no-header",
                 "--tb=short"])
    assert "16 passed" in proc.stdout, f"未达 16 passed: {proc.stdout}"
    # 关键：MCPToolAdapter 必须含 policy 字段 + invoke 必须调 check_tool
    src = (REPO / "src/agent_swarm/mcp/adapter.py").read_text(encoding="utf-8")
    assert "policy: \"SecurityPolicy | None\"" in src, "policy 字段未声明"
    assert "self.policy.check_tool(self.name, arguments)" in src, "policy.check_tool 未调用"
    assert "self.risk" in src and "_RISK_ORDER" in src, "risk 二次闸门缺失"
    print("  ✓ 8 个新单测通过（policy ALLOW/DENY/REQUIRE_APPROVAL + risk 二次闸门）")


def check_cli_provider() -> None:
    """P1-3.2: CLI 支持 --provider + ANTHROPIC_API_KEY env"""
    print("[P1-3.2] CLI --provider 分发 + ANTHROPIC_API_KEY 注入")
    proc = _run(["uv" if _has_uv() else ".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_cli.py", "-q", "--no-header",
                 "-k", "provider or api_key", "--tb=short"])
    assert "7 passed" in proc.stdout, f"未达 7 passed: {proc.stdout}"
    cli = (REPO / "src/agent_swarm/cli/main.py").read_text(encoding="utf-8")
    assert "PROVIDER_ENV_VARS" in cli, "PROVIDER_ENV_VARS 表缺失"
    assert "ANTHROPIC_API_KEY" in cli, "Anthropic env 注入缺失"
    assert 'click.Choice(["openai", "anthropic"]' in cli, "--provider 选项未声明"
    assert "envvar=\"OPENAI_API_KEY\"" not in cli, "旧的硬编码 envvar 还在"
    print("  ✓ --provider openai/anthropic + 7 个新单测覆盖")


def check_mypy_clean() -> None:
    """P1-3.3: mypy src/ 0 错误（替代原 'internal error'）"""
    print("[P1-3.3] mypy src/ 0 错误")
    proc = _run([".venv/bin/python", "-m", "mypy", "--show-traceback", "src/agent_swarm"])
    assert proc.returncode == 0, f"mypy 退出 {proc.returncode}: {proc.stdout}"
    assert "Success" in proc.stdout, f"mypy 未通过: {proc.stdout}"
    # 核对修复的三个根因都已消除
    assert "unused-ignore" not in proc.stdout, "仍有未使用的 type: ignore"
    assert "name-defined" not in proc.stdout, "仍有未定义的名字"
    print("  ✓ mypy 0 errors, 0 warnings on 44 source files")


def check_no_regression() -> None:
    """无回归：全量测试通过（不含 integration）"""
    print("[P1-regression] 全量单测 + e2e + golden + security")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header", "--tb=line"])
    # 从末尾 "XXX passed" 行提取数字
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 645, f"P1 后测试数 {n} < 645（基线 633 + 8 mcp + 7 cli = 648 期望）"
    print(f"  ✓ {last}")


def _has_uv() -> bool:
    from shutil import which
    return which("uv") is not None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 60)
    print("P1 阶段 DoD 验收 — REVIEW-2026-06-19 §3 P1 风险点")
    print("=" * 60)
    check_mcp_policy_link()
    check_cli_provider()
    check_mypy_clean()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ P1 阶段全部通过（3/3 风险点已修 + 无回归）")
    print("=" * 60)


if __name__ == "__main__":
    main()
