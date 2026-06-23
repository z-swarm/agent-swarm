"""
@module tests.unit.test_doctor
@brief  W14b agent-swarm doctor 子命令单元测试——DESIGN §17.7 DX 工具

覆盖:
  - CheckStatus 枚举 + render 配色
  - DoctorReport 汇总 + 退出码
  - check_sqlite_lock: 正常 / 不存在 / 锁 / 权限
  - check_secrets: 多种 env var 组合
  - check_llm_provider: env 缺失 / 网络失败（用 monkeypatch）
  - check_mcp_servers: config 缺失 / 解析失败 / 全部不可达
  - CLI 入口: --help + 退出码
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from agent_swarm.cli.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    check_secrets,
    check_sqlite_lock,
    doctor,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: doctor CLI invocation differs on Windows",
)

# ---------------------------------------------------------------------------
# CheckStatus + Report
# ---------------------------------------------------------------------------


def test_check_status_values() -> None:
    assert CheckStatus.OK.value == "ok"
    assert CheckStatus.WARN.value == "warn"
    assert CheckStatus.FAIL.value == "fail"
    assert CheckStatus.SKIP.value == "skip"


def test_doctor_report_empty() -> None:
    r = DoctorReport()
    assert not r.has_failures()
    assert not r.has_warnings()
    assert r.exit_code() == 0


def test_doctor_report_with_warning() -> None:
    r = DoctorReport()
    r.add(CheckResult("a", CheckStatus.WARN, "x"))
    assert r.has_warnings()
    assert not r.has_failures()
    assert r.exit_code() == 1


def test_doctor_report_with_failure() -> None:
    r = DoctorReport()
    r.add(CheckResult("a", CheckStatus.OK, "x"))
    r.add(CheckResult("b", CheckStatus.FAIL, "y"))
    assert r.has_failures()
    assert r.exit_code() == 2


def test_check_result_render_contains_status_and_name() -> None:
    r = CheckResult("test.check", CheckStatus.OK, "all good")
    rendered = r.render()
    assert "test.check" in rendered
    assert "OK" in rendered
    assert "all good" in rendered


# ---------------------------------------------------------------------------
# SQLite 锁
# ---------------------------------------------------------------------------


def test_sqlite_lock_in_memory(tmp_path: Path) -> None:
    """内存 db 不查锁（防 sqlite :memory: 行为差异）"""
    r = check_sqlite_lock(Path(":memory:"))
    assert r.status == CheckStatus.OK
    assert "in-memory" in r.message.lower()


def test_sqlite_lock_not_exists(tmp_path: Path) -> None:
    """db 不存在 = OK（首次 run 会创建）"""
    r = check_sqlite_lock(tmp_path / "nope.db")
    assert r.status == CheckStatus.OK
    assert "not yet created" in r.message


def test_sqlite_lock_writable(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    # 先建一个
    sqlite3.connect(str(db)).close()
    r = check_sqlite_lock(db)
    assert r.status == CheckStatus.OK
    assert "writable" in r.message


def test_sqlite_lock_locked(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE 阻塞 → 视为锁"""
    db = tmp_path / "locked.db"
    conn = sqlite3.connect(str(db), timeout=1.0)
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = check_sqlite_lock(db)
        # timeout=2.0 应该会等 + 抛 OperationalError
        # 但 BEGIN IMMEDIATE 持有者是本进程——新 conn 会等 2s 后报错
        assert r.status == CheckStatus.FAIL
        assert "lock" in r.message.lower() or "error" in r.message.lower()
    finally:
        conn.execute("ROLLBACK")
        conn.close()


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_secrets_no_keys_needed_when_fake_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_SWARM_FAKE_LLM=1 时不需要 LLM key"""
    for var in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_SWARM_FAKE_LLM", "1")
    r = check_secrets()
    assert r.status == CheckStatus.OK


def test_secrets_no_keys_no_fake_llm_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 key 无 fake_llm = WARN"""
    for var in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_SWARM_FAKE_LLM"]:
        monkeypatch.delenv(var, raising=False)
    r = check_secrets()
    assert r.status == CheckStatus.WARN
    assert "no LLM provider key" in r.message


def test_secrets_with_openai_key_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = check_secrets()
    assert r.status == CheckStatus.OK
    assert "1 secret" in r.message or "secrets present" in r.message


def test_secrets_with_anthropic_key_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = check_secrets()
    assert r.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# LLM Provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_provider_missing_key() -> None:
    """env 缺失 = WARN（不是 FAIL——Lark/Vault 也是 WARN）"""
    from agent_swarm.cli.doctor import check_llm_provider

    with patch.dict("os.environ", {}, clear=False):
        import os as _os
        _os.environ.pop("OPENAI_API_KEY", None)
        r = await check_llm_provider("openai", "OPENAI_API_KEY")
    assert r.status == CheckStatus.WARN
    assert "not set" in r.message


@pytest.mark.asyncio
async def test_llm_provider_connection_error() -> None:
    """网络错误 = FAIL"""
    from agent_swarm.cli.doctor import check_llm_provider

    # mock aiohttp 抛 ClientError
    with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(
            side_effect=Exception("mocked error"),
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            r = await check_llm_provider("openai", "OPENAI_API_KEY")
    # 任意失败都是 FAIL 或非 OK
    assert r.status in (CheckStatus.FAIL, CheckStatus.SKIP)


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_servers_no_config() -> None:
    """未传 --mcp-config = SKIP"""
    from agent_swarm.cli.doctor import check_mcp_servers

    r = await check_mcp_servers(None)
    assert r.status == CheckStatus.SKIP


@pytest.mark.asyncio
async def test_mcp_servers_config_not_found(tmp_path: Path) -> None:
    from agent_swarm.cli.doctor import check_mcp_servers

    r = await check_mcp_servers(tmp_path / "nope.yaml")
    assert r.status == CheckStatus.FAIL
    assert "not found" in r.message


@pytest.mark.asyncio
async def test_mcp_servers_invalid_config(tmp_path: Path) -> None:
    from agent_swarm.cli.doctor import check_mcp_servers

    bad = tmp_path / "bad.yaml"
    bad.write_text(": not valid yaml :::", encoding="utf-8")
    r = await check_mcp_servers(bad)
    assert r.status == CheckStatus.FAIL
    assert "load" in r.message.lower() or "yaml" in r.message.lower()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def test_doctor_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(doctor, ["--help"])
    assert result.exit_code == 0
    assert "agent-swarm 健康检查" in result.output
    assert "--skip-llm" in result.output
    assert "--skip-mcp" in result.output


def test_doctor_cli_skip_all_warns_secrets(tmp_path: Path) -> None:
    """无 LLM key + 跳过 LLM/MCP：secrets 仍 WARN，exit 1"""
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        [
            "--db", str(tmp_path / "sessions.db"),
            "--skip-llm",
            "--skip-mcp",
        ],
    )
    # exit 1 = WARN
    assert result.exit_code == 1
    assert "secrets" in result.output
    assert "WARN" in result.output


def test_doctor_cli_all_skipped_via_fake_llm(tmp_path: Path) -> None:
    """AGENT_SWARM_FAKE_LLM=1 时 secrets OK，exit 0"""
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        [
            "--db", str(tmp_path / "sessions.db"),
            "--skip-llm",
            "--skip-mcp",
            "--skip-sandbox",
        ],
        env={"AGENT_SWARM_FAKE_LLM": "1", "PATH": "/usr/bin"},
    )
    # exit 0 = 全 OK 或 SKIP
    assert result.exit_code == 0
    assert "Summary" in result.output
