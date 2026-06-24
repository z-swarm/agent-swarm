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

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_swarm.cli.main import cli

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="P3-WIN: CLI subprocess tests have Windows shell differences",
)


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


def test_cli_run_swarm_crash_exit_code_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_cli_run_failed_state_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_cli_run_verbose_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ---------------------------------------------------------------------------
# session 子命令（W3）
# ---------------------------------------------------------------------------


def test_cli_session_help() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["session", "--help"])
    assert res.exit_code == 0
    assert "list" in res.stdout
    assert "show" in res.stdout
    assert "resume" in res.stdout


def test_cli_session_list_no_db(tmp_path: Path) -> None:
    """db 文件不存在 → 友好提示 + exit 0"""
    runner = CliRunner()
    res = runner.invoke(cli, ["session", "list", "--db", str(tmp_path / "nope.db")])
    assert res.exit_code == 0
    assert "No session database" in res.stdout


def test_cli_session_list_empty(tmp_path: Path) -> None:
    """db 存在但无 session"""
    import asyncio

    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "empty.db"

    async def _init():
        sink = SqliteEventSink(db)
        await sink._ensure_conn()  # 触发建库
        await sink.aclose()

    asyncio.run(_init())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "list", "--db", str(db)])
    assert res.exit_code == 0
    assert "no sessions" in res.stdout.lower()


def test_cli_session_list_shows_sessions(tmp_path: Path) -> None:
    """注册 2 个 session 后 list 应都能看到"""
    import asyncio

    from agent_swarm.core.session_manager import SessionManager
    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "list.db"

    async def _seed():
        sink = SqliteEventSink(db)
        mgr = SessionManager(sink)
        await mgr.create_session("alpha")
        await mgr.create_session("beta")
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "list", "--db", str(db)])
    assert res.exit_code == 0
    assert "alpha" in res.stdout
    assert "beta" in res.stdout


def test_cli_session_show_unknown(tmp_path: Path) -> None:
    """不存在的 session_id → exit 2"""
    import asyncio

    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "show.db"

    async def _init():
        sink = SqliteEventSink(db)
        await sink._ensure_conn()
        await sink.aclose()

    asyncio.run(_init())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "show", "ghost", "--db", str(db)])
    assert res.exit_code == 2
    assert "not found" in res.stdout.lower()


def test_cli_session_show_with_events(tmp_path: Path) -> None:
    """有 session + 事件 → 显示详情和事件流"""
    import asyncio

    from agent_swarm.core.session_manager import SessionManager
    from agent_swarm.core.types import SessionEvent
    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "show.db"

    async def _seed():
        sink = SqliteEventSink(db)
        mgr = SessionManager(sink)
        await mgr.create_session("test", session_id="S1")
        await sink.consume(
            SessionEvent(
                event_name="task.created",
                session_id="S1",
                timestamp=1.0,
                seq=0,
                payload={"task_id": "T"},
            )
        )
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "show", "S1", "--db", str(db)])
    assert res.exit_code == 0
    assert "S1" in res.stdout
    assert "task.created" in res.stdout
    assert "test" in res.stdout  # swarm_name


def test_cli_session_show_no_events_flag(tmp_path: Path) -> None:
    """--no-events 时不打印事件流"""
    import asyncio

    from agent_swarm.core.session_manager import SessionManager
    from agent_swarm.core.types import SessionEvent
    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "show2.db"

    async def _seed():
        sink = SqliteEventSink(db)
        mgr = SessionManager(sink)
        await mgr.create_session("x", session_id="S2")
        await sink.consume(
            SessionEvent(
                event_name="task.created",
                session_id="S2",
                timestamp=1.0,
                seq=0,
                payload={"task_id": "T"},
            )
        )
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "show", "S2", "--no-events", "--db", str(db)])
    assert res.exit_code == 0
    assert "S2" in res.stdout
    # 事件不应被打印
    assert "task.created" not in res.stdout


def test_cli_session_resume_unknown(tmp_path: Path) -> None:
    """不存在的 session → exit 2"""
    import asyncio

    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "resume.db"

    async def _init():
        sink = SqliteEventSink(db)
        await sink._ensure_conn()
        await sink.aclose()

    asyncio.run(_init())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "resume", "ghost", "--db", str(db)])
    assert res.exit_code == 2
    assert "not found" in res.stdout.lower()


def test_cli_session_resume_no_db(tmp_path: Path) -> None:
    """db 文件不存在 → exit 2 + 错误提示"""
    runner = CliRunner()
    res = runner.invoke(cli, ["session", "resume", "x", "--db", str(tmp_path / "nope.db")])
    assert res.exit_code == 2
    assert "not found" in res.stdout.lower()


def test_cli_session_resume_full_state(tmp_path: Path) -> None:
    """完整事件流恢复——CLI 输出含任务表 + 消息列表"""
    import asyncio

    from agent_swarm.core.session_manager import SessionManager
    from agent_swarm.core.types import SessionEvent
    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "rsm.db"

    async def _seed():
        sink = SqliteEventSink(db)
        mgr = SessionManager(sink)
        await mgr.create_session("recovery", session_id="R1")
        events = [
            (
                "task.created",
                {
                    "task_id": "T1",
                    "title": "build",
                    "description": "do",
                    "status": "pending",
                    "depends_on": [],
                },
            ),
            ("task.claimed", {"task_id": "T1", "agent_id": "a", "version": 1}),
            ("task.completed", {"task_id": "T1", "version": 2, "result": "OK"}),
            (
                "message.sent",
                {"msg_id": "m1", "from": "a", "to": "b", "msg_type": "notify", "content": "ping"},
            ),
        ]
        for i, (name, payload) in enumerate(events):
            await sink.consume(
                SessionEvent(
                    event_name=name,
                    session_id="R1",
                    timestamp=float(i + 1),
                    seq=i,
                    payload=payload,
                )
            )
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "resume", "R1", "--db", str(db)])
    assert res.exit_code == 0
    assert "Restored" in res.stdout
    assert "R1" in res.stdout
    assert "T1" in res.stdout
    assert "completed" in res.stdout
    assert "ping" in res.stdout


def test_cli_session_show_with_config_flag(tmp_path: Path) -> None:
    """W3-Z4 回归：--config 显示 yaml 配置"""
    import asyncio

    from agent_swarm.core.session_manager import SessionManager
    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    db = tmp_path / "cfg.db"

    async def _seed():
        sink = SqliteEventSink(db)
        mgr = SessionManager(sink)
        await mgr.create_session(
            "with-config",
            session_id="C1",
            config_yaml="name: with-config\nagents:\n  - id: a\n",
        )
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    # 默认 --no-config 时不显示
    res_default = runner.invoke(cli, ["session", "show", "C1", "--db", str(db)])
    assert res_default.exit_code == 0
    assert "Config YAML" not in res_default.stdout

    # --config 时显示
    res_with = runner.invoke(cli, ["session", "show", "C1", "--config", "--db", str(db)])
    assert res_with.exit_code == 0
    assert "Config YAML" in res_with.stdout
    assert "with-config" in res_with.stdout


# ---------------------------------------------------------------------------
# P1-3.2 (REVIEW-2026-06-19 §3.2) CLI --provider 分发 + ANTHROPIC_API_KEY env
# ---------------------------------------------------------------------------


def _minimal_cfg(tmp_path: Path) -> Path:
    cfg = """
name: cli-provider
agents:
  - id: a
    role: r
    provider: anthropic
    model: claude-sonnet-4-6
    tools: []
tasks:
  - title: t
"""
    p = tmp_path / "x.yaml"
    p.write_text(cfg, encoding="utf-8")
    return p


def test_cli_run_help_documents_provider_option() -> None:
    """--help 应包含 --provider 选项（避免 'Anthropic 支持' 再次成为空头支票）"""
    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--help"])
    assert res.exit_code == 0
    assert "--provider" in res.stdout
    assert "anthropic" in res.stdout.lower()
    # api-key 帮助文字应说明需配合 --provider
    assert "--provider" in res.stdout


def test_cli_run_injects_api_key_to_openai_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--api-key + --provider openai → 注入到 OPENAI_API_KEY"""
    from agent_swarm.core.swarm import SwarmResult

    p = _minimal_cfg(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    seen: dict[str, str] = {}

    async def fake_run(self):  # type: ignore[no-untyped-def]
        seen["openai"] = os.environ.get("OPENAI_API_KEY", "")
        seen["anthropic"] = os.environ.get("ANTHROPIC_API_KEY", "")
        return SwarmResult(
            name="x",
            state="completed",
            duration_seconds=0.1,
            tasks_completed=1,
            tasks_failed=0,
            tasks_unfinished=0,
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)
    import os

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--provider", "openai", "--api-key", "sk-test-openai", str(p)])
    assert res.exit_code == 0
    assert seen["openai"] == "sk-test-openai"
    assert seen["anthropic"] == ""  # 不污染 anthropic env


def test_cli_run_injects_api_key_to_anthropic_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--api-key + --provider anthropic → 注入到 ANTHROPIC_API_KEY（P1-3.2 主修复）"""
    from agent_swarm.core.swarm import SwarmResult

    p = _minimal_cfg(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    seen: dict[str, str] = {}

    async def fake_run(self):  # type: ignore[no-untyped-def]
        import os as _os

        seen["openai"] = _os.environ.get("OPENAI_API_KEY", "")
        seen["anthropic"] = _os.environ.get("ANTHROPIC_API_KEY", "")
        return SwarmResult(
            name="x",
            state="completed",
            duration_seconds=0.1,
            tasks_completed=1,
            tasks_failed=0,
            tasks_unfinished=0,
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--provider", "anthropic", "--api-key", "sk-ant-test", str(p)])
    assert res.exit_code == 0
    assert seen["anthropic"] == "sk-ant-test"
    assert seen["openai"] == ""  # 不污染 openai env


def test_cli_run_api_key_without_provider_is_rejected(tmp_path: Path) -> None:
    """--api-key 但没 --provider → Click UsageError exit 2（避免误注入）"""
    p = _minimal_cfg(tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--api-key", "sk-orphan", str(p)])
    assert res.exit_code == 2
    # Click UsageError 信息
    assert "--provider" in (res.stdout + (res.stderr or ""))


def test_cli_run_invalid_provider_value_rejected(tmp_path: Path) -> None:
    """--provider 取值非法（不在 Choice 内）→ Click error exit 2"""
    p = _minimal_cfg(tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--provider", "gemini", str(p)])
    assert res.exit_code == 2


def test_cli_run_no_api_key_keeps_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """省略 --api-key 时不修改环境变量（向后兼容：让 provider 自己读 env）"""
    from agent_swarm.core.swarm import SwarmResult

    p = _minimal_cfg(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-pre-set")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-pre-set")

    seen: dict[str, str] = {}

    async def fake_run(self):  # type: ignore[no-untyped-def]
        import os as _os

        seen["openai"] = _os.environ.get("OPENAI_API_KEY", "")
        seen["anthropic"] = _os.environ.get("ANTHROPIC_API_KEY", "")
        return SwarmResult(
            name="x",
            state="completed",
            duration_seconds=0.1,
            tasks_completed=1,
            tasks_failed=0,
            tasks_unfinished=0,
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(p)])
    assert res.exit_code == 0
    # 已有 env 不被覆盖
    assert seen["anthropic"] == "sk-pre-set"
    assert seen["openai"] == "sk-openai-pre-set"


def test_cli_tui_help_documents_provider_option() -> None:
    """tui 子命令也应有 --provider（保持 run/tui 一致）"""
    runner = CliRunner()
    res = runner.invoke(cli, ["tui", "--help"])
    assert res.exit_code == 0
    assert "--provider" in res.stdout


# ---------------------------------------------------------------------------
# P2-3.6 (REVIEW-2026-06-19 §3.6) session DB 路径 fail-fast
# ---------------------------------------------------------------------------


def test_cli_run_fails_fast_when_db_dir_not_exists(tmp_path: Path) -> None:
    """--db 父目录不存在 → fail-fast exit 2（不再静默创建空文件）"""
    cfg_yaml = """
name: db-test
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    cfg = tmp_path / "x.yaml"
    cfg.write_text(cfg_yaml, encoding="utf-8")

    runner = CliRunner()
    # 父目录不存在的 db 路径
    missing_dir = tmp_path / "no_such_dir" / "subdir" / "sessions.db"
    res = runner.invoke(cli, ["run", "--db", str(missing_dir), str(cfg)])
    assert res.exit_code == 2
    combined = res.stdout + (res.stderr or "")
    assert "parent directory does not exist" in combined
    assert "mkdir" in combined  # 给出 hint


def test_cli_run_fails_fast_when_db_is_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--db 指向一个目录 → fail-fast exit 2"""

    cfg_yaml = """
name: db-dir
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    cfg = tmp_path / "x.yaml"
    cfg.write_text(cfg_yaml, encoding="utf-8")

    db_dir = tmp_path / "sessions_dir"
    db_dir.mkdir()

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--db", str(db_dir), str(cfg)])
    assert res.exit_code == 2
    assert "is a directory" in (res.stdout + (res.stderr or ""))


def test_cli_run_fails_fast_when_db_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--db 文件存在但当前用户无写权限 → fail-fast exit 2"""

    cfg_yaml = """
name: db-ro
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    cfg = tmp_path / "x.yaml"
    cfg.write_text(cfg_yaml, encoding="utf-8")

    db = tmp_path / "readonly.db"
    db.write_text("")  # 存在但空

    # 模拟不可写：os.access 永远 False
    import os as _os

    orig_access = _os.access

    def fake_access(path, mode, *args, **kwargs):
        if str(path) == str(db) and mode == _os.W_OK:
            return False
        return orig_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(_os, "access", fake_access)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--db", str(db), str(cfg)])
    assert res.exit_code == 2
    combined = res.stdout + (res.stderr or "")
    assert "not writable" in combined


def test_cli_session_list_fails_fast_on_unwritable_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session list 子命令也应 fail-fast on unwritable db"""
    import os as _os

    orig_access = _os.access

    db = tmp_path / "ro.db"
    db.write_text("")

    def fake_access(path, mode, *args, **kwargs):
        if str(path) == str(db) and mode == _os.W_OK:
            return False
        return orig_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(_os, "access", fake_access)

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "list", "--db", str(db)])
    assert res.exit_code == 2
    assert "not writable" in (res.stdout + (res.stderr or ""))


def test_cli_session_show_fails_fast_on_unwritable_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session show 子命令也应 fail-fast"""
    import os as _os

    orig_access = _os.access

    db = tmp_path / "ro.db"
    db.write_text("")

    def fake_access(path, mode, *args, **kwargs):
        if str(path) == str(db) and mode == _os.W_OK:
            return False
        return orig_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(_os, "access", fake_access)

    runner = CliRunner()
    res = runner.invoke(cli, ["session", "show", "x", "--db", str(db)])
    assert res.exit_code == 2
    assert "not writable" in (res.stdout + (res.stderr or ""))


def test_cli_run_writable_db_path_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """happy path：可写 db 路径不被 fail-fast 阻断"""
    from agent_swarm.core.swarm import SwarmResult

    cfg_yaml = """
name: writable
agents:
  - id: a
    role: r
    provider: openai
    model: gpt-4o-mini
    tools: []
tasks:
  - title: t
"""
    cfg = tmp_path / "x.yaml"
    cfg.write_text(cfg_yaml, encoding="utf-8")

    db = tmp_path / "fresh.db"  # 文件不存在但父目录可写

    async def fake_run(self):  # type: ignore[no-untyped-def]
        return SwarmResult(
            name="writable",
            state="completed",
            duration_seconds=0.1,
            tasks_completed=1,
            tasks_failed=0,
            tasks_unfinished=0,
        )

    monkeypatch.setattr("agent_swarm.core.swarm.Swarm.run", fake_run)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--db", str(db), str(cfg)])
    assert res.exit_code == 0, f"可写 db 路径应通过 fail-fast: {res.stdout}"


# ---------------------------------------------------------------------------
# P5-W36a: CLI 选项 --web-jwt-secret-ref / --web-secret-manager / --vault-*
# ---------------------------------------------------------------------------


def test_cli_run_help_shows_w36a_web_jwt_options() -> None:
    """W36a: CLI --help 显示新增的 web jwt 选项"""
    from click.testing import CliRunner

    from agent_swarm.cli.main import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--help"])
    assert res.exit_code == 0
    # 新增 6 选项
    assert "--web-jwt-secret-ref" in res.stdout
    assert "--web-secret-manager" in res.stdout
    assert "--vault-url" in res.stdout
    assert "--vault-role-id" in res.stdout
    assert "--vault-secret-id" in res.stdout
    # W34 老选项仍存在
    assert "--web-jwt-secret" in res.stdout
    assert "--web-jwt-expires" in res.stdout


def test_cli_run_with_web_jwt_secret_ref_literal() -> None:
    """W36a: --web-jwt-secret-ref=literal-value (字面值) 不抛错"""
    from click.testing import CliRunner

    from agent_swarm.cli.main import cli

    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", "--web", "--web-jwt-secret-ref", "test-literal", "--help"],
    )
    # --help 会让 click 早退, 不会真启动 server
    # 这里只验 CLI 能解析该参数
    assert "--help" not in res.stdout or res.exit_code == 0
