"""
@module tests.unit.test_cli
@brief  CLI 入口异常路径单测——W2-B15 补完 cli/main.py 覆盖率

W1 e2e 已覆盖 happy path；这里专挑：
  - 配置加载失败
  - swarm crash
  - KeyboardInterrupt
  - --version / 子命令 --help
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_swarm.cli.main import cli


def test_cli_version() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["--version"])
    assert res.exit_code == 0
    assert "agent-swarm" in res.stdout


def test_cli_run_help() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--help"])
    assert res.exit_code == 0
    assert "swarm" in res.stdout.lower()


def test_cli_run_invalid_yaml(tmp_path: Path) -> None:
    """yaml 不是 mapping 而是 list——加载失败应 exit_code=2"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(bad)])
    assert res.exit_code == 2
    assert "Failed to load config" in res.stdout


def test_cli_run_missing_required_field(tmp_path: Path) -> None:
    """yaml 缺 agents 字段——加载失败 exit_code=2"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ntasks:\n  - title: t\n", encoding="utf-8")
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(bad)])
    assert res.exit_code == 2


def test_cli_run_swarm_crash_exit_code_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swarm.run 内部异常——CLI 应 exit 1 并打印错误"""
    cfg_yaml = """
name: crash
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    yaml_path = tmp_path / "x.yaml"
    yaml_path.write_text(cfg_yaml, encoding="utf-8")

    # patch Swarm.run 让它抛异常
    async def boom(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("internal explosion")

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", boom)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])
    assert res.exit_code == 1
    assert "Swarm crashed" in res.stdout
    assert "internal explosion" in res.stdout


def test_cli_run_keyboard_interrupt_exit_code_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C → exit 130（标准 SIGINT 退出码）"""
    cfg_yaml = """
name: kb
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    yaml_path = tmp_path / "x.yaml"
    yaml_path.write_text(cfg_yaml, encoding="utf-8")

    async def boom(self):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt()

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", boom)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])
    assert res.exit_code == 130
    assert "interrupted" in res.stdout


def test_cli_run_failed_state_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swarm 返回 state=failed → CLI exit 1"""
    cfg_yaml = """
name: fail
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    yaml_path = tmp_path / "x.yaml"
    yaml_path.write_text(cfg_yaml, encoding="utf-8")

    from agent_swarm.core.swarm import SwarmResult

    async def fake_run(self):  # type: ignore[no-untyped-def]
        return SwarmResult(
            name="fail",
            state="failed",
            duration_seconds=0.1,
            tasks_completed=0,
            tasks_failed=1,
            tasks_unfinished=0,
            error="something broke",
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])
    assert res.exit_code == 1
    assert "failed" in res.stdout
    assert "something broke" in res.stdout


def test_cli_run_verbose_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--verbose 启用 DEBUG 日志，但不影响退出码"""
    cfg_yaml = """
name: v
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    yaml_path = tmp_path / "x.yaml"
    yaml_path.write_text(cfg_yaml, encoding="utf-8")

    from agent_swarm.core.swarm import SwarmResult

    async def fake_run(self):  # type: ignore[no-untyped-def]
        return SwarmResult(
            name="v",
            state="completed",
            duration_seconds=0.1,
            tasks_completed=1,
            tasks_failed=0,
            tasks_unfinished=0,
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--verbose", str(yaml_path)])
    assert res.exit_code == 0


def test_cli_run_nonexistent_file(tmp_path: Path) -> None:
    """click 原生处理：路径不存在 → exit_code=2 with click error message"""
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(tmp_path / "ghost.yaml")])
    assert res.exit_code != 0
