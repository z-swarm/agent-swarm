"""
@module tests.e2e.test_w3_resume
@brief  W3 验收 e2e（DESIGN.md §15 W3 DoD）

DoD:
  - 跑完 swarm（CLI run）→ 数据库写入事件流
  - agent-swarm session list 能看到该 session
  - agent-swarm session resume <id> 能 100% 重建状态

实现策略:
  - 整测试都跑在子进程隔离 OS 环境（避免真实 OPENAI_API_KEY）
  - 用 monkeypatch + CliRunner 模拟"两次独立调用"
  - run 阶段注入 FakeLLMProvider；resume 阶段无 LLM（只读事件）
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agent_swarm.cli.main import cli
from tests.conftest import FakeLLMProvider, ScriptedResponse


def _w3_yaml(tmp_path: Path) -> Path:
    cfg = {
        "name": "w3-resume",
        "agents": [
            {
                "id": "worker",
                "role": "worker",
                "persona": "do work",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 3,
            }
        ],
        "tasks": [
            {"id": "T-A", "title": "task A"},
            {"id": "T-B", "title": "task B", "depends_on": ["T-A"]},
        ],
    }
    p = tmp_path / "w3.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


@pytest.fixture
def fake_w3(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    fake = FakeLLMProvider()
    fake.script.append(ScriptedResponse(content="A done", finish_reason="stop"))
    fake.script.append(ScriptedResponse(content="B done", finish_reason="stop"))

    def fake_get_provider(name: str, **kw):  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", fake_get_provider)
    return fake


def test_w3_run_persists_then_resume(
    tmp_path: Path,
    fake_w3: FakeLLMProvider,
) -> None:
    """W3 DoD：CLI run → CLI session list → CLI session resume，全链路通过"""
    yaml_path = _w3_yaml(tmp_path)
    db_path = tmp_path / "events.db"

    runner = CliRunner()

    # 1) run
    res_run = runner.invoke(
        cli, ["run", str(yaml_path), "--db", str(db_path)]
    )
    assert res_run.exit_code == 0, f"run failed: {res_run.stdout}"
    assert "completed" in res_run.stdout

    # 数据库已生成
    assert db_path.exists()

    # 2) session list 应看到 w3-resume
    res_list = runner.invoke(cli, ["session", "list", "--db", str(db_path)])
    assert res_list.exit_code == 0
    assert "w3-resume" in res_list.stdout

    # 3) 提取 session_id
    # 从 list 输出中解析（session id 以 s- 开头）
    import re
    match = re.search(r"(s-[a-f0-9]+)", res_list.stdout)
    assert match is not None, f"no session id in: {res_list.stdout}"
    session_id = match.group(1)

    # 4) session show 应有完整事件流
    res_show = runner.invoke(
        cli, ["session", "show", session_id, "--db", str(db_path)]
    )
    assert res_show.exit_code == 0
    assert "task.created" in res_show.stdout
    assert "task.completed" in res_show.stdout
    assert "swarm.completed" in res_show.stdout

    # 5) session resume 应重建出两个 completed 任务
    res_resume = runner.invoke(
        cli, ["session", "resume", session_id, "--db", str(db_path)]
    )
    assert res_resume.exit_code == 0
    assert "Restored" in res_resume.stdout
    assert "T-A" in res_resume.stdout
    assert "T-B" in res_resume.stdout
    # 两个任务都应是 completed
    assert res_resume.stdout.count("completed") >= 2


def test_w3_resume_unknown_session_returns_error(tmp_path: Path) -> None:
    """resume 不存在的 session → exit 2"""
    db = tmp_path / "events.db"
    # 先建空 db
    import asyncio

    from agent_swarm.observability.sqlite_sink import SqliteEventSink

    async def _init():
        sink = SqliteEventSink(db)
        await sink._ensure_conn()
        await sink.aclose()
    asyncio.run(_init())

    runner = CliRunner()
    res = runner.invoke(
        cli, ["session", "resume", "s-ghost", "--db", str(db)]
    )
    assert res.exit_code == 2


def test_w3_run_with_failing_task_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W3：失败任务也应在事件流中持久化，恢复后看得到"""

    class BoomProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            raise RuntimeError("planned failure")

    boom = BoomProvider()
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: boom)

    yaml_path = _w3_yaml(tmp_path)
    db_path = tmp_path / "fail.db"

    runner = CliRunner()
    res_run = runner.invoke(
        cli, ["run", str(yaml_path), "--db", str(db_path)]
    )
    assert res_run.exit_code == 1
    assert "failed" in res_run.stdout

    # session list 应看到——state=failed
    res_list = runner.invoke(cli, ["session", "list", "--db", str(db_path)])
    assert res_list.exit_code == 0
    assert "failed" in res_list.stdout
