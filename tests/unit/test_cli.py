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
    res = runner.invoke(
        cli, ["session", "list", "--db", str(tmp_path / "nope.db")]
    )
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
            SessionEvent(event_name="task.created", session_id="S1",
                         timestamp=1.0, seq=0, payload={"task_id": "T"})
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
            SessionEvent(event_name="task.created", session_id="S2",
                         timestamp=1.0, seq=0, payload={"task_id": "T"})
        )
        await sink.aclose()

    asyncio.run(_seed())

    runner = CliRunner()
    res = runner.invoke(
        cli, ["session", "show", "S2", "--no-events", "--db", str(db)]
    )
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
    res = runner.invoke(
        cli, ["session", "resume", "x", "--db", str(tmp_path / "nope.db")]
    )
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
            ("task.created", {"task_id": "T1", "title": "build", "description": "do",
                              "status": "pending", "depends_on": []}),
            ("task.claimed", {"task_id": "T1", "agent_id": "a", "version": 1}),
            ("task.completed", {"task_id": "T1", "version": 2, "result": "OK"}),
            ("message.sent", {"msg_id": "m1", "from": "a", "to": "b",
                              "msg_type": "notify", "content": "ping"}),
        ]
        for i, (name, payload) in enumerate(events):
            await sink.consume(SessionEvent(
                event_name=name, session_id="R1",
                timestamp=float(i + 1), seq=i, payload=payload,
            ))
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
