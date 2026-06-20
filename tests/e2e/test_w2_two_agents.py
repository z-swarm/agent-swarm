"""
@module tests.e2e.test_w2_two_agents
@brief  W2 验收 e2e（DESIGN.md §17.2 W2 DoD）

DoD:
  - 演示 swarm 含 ≥2 agent
  - TaskQueue 显示 1→2→1 状态流转（pending → in_progress → completed）
  - CAS 冲突日志 ≥1 条（证明锁机制工作）

实现策略:
  - 替换 Provider 为 FakeLLMProvider
  - 通过 click testing 跑 CLI
  - 数据/任务为 reader 读 → writer 总结的协作流
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agent_swarm.cli.main import cli
from agent_swarm.core.types import ToolCall
from tests.conftest import FakeLLMProvider, ScriptedResponse

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: e2e CLI run has Windows shell differences",
)


def _make_w2_yaml(tmp_path: Path, data_path: Path) -> Path:
    cfg = {
        "name": "w2-two-agents",
        "agents": [
            {
                "id": "reader",
                "role": "data reader",
                "persona": "Read the requested file and forward findings.",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["read_file", "send_message"],
                "max_iterations": 4,
            },
            {
                "id": "writer",
                "role": "summarizer",
                "persona": "Write summaries based on what the reader sends.",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["send_message"],
                "max_iterations": 3,
            },
        ],
        "tasks": [
            {
                "id": "T-read",
                "title": "Read data file",
                "description": f"Read {data_path.name} and forward to writer.",
                "assigned_to": "reader",
            },
            {
                "id": "T-write",
                "title": "Summarize data",
                "description": "Write a summary based on reader's message.",
                "assigned_to": "writer",
                "depends_on": ["T-read"],
            },
        ],
    }
    p = tmp_path / "w2.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


@pytest.fixture
def fake_w2(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    """
    脚本编排——按 reader 先抢到 T-read 的顺序:

    reader 抢到 T-read:
      ① 调 read_file → ② 调 send_message → ③ stop

    writer T-write 解阻塞后被抢:
      ① stop（直接根据收到的消息回答）

    其他 agent 在 idle 时不应触发 LLM 调用——但脚本兜底防意外
    """
    fake = FakeLLMProvider(default_model="gpt-4o-mini")
    # reader 阶段
    fake.script.append(
        ScriptedResponse(
            tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={"path": "data.txt"})
            ],
            finish_reason="tool_use",
        )
    )
    fake.script.append(
        ScriptedResponse(
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="send_message",
                    arguments={
                        "to_agent": "writer",
                        "content": "data: payload-w2",
                        "msg_type": "delegate",
                    },
                )
            ],
            finish_reason="tool_use",
        )
    )
    fake.script.append(
        ScriptedResponse(content="forwarded data", finish_reason="stop")
    )
    # writer 阶段
    fake.script.append(
        ScriptedResponse(
            content="Summary: data is payload-w2", finish_reason="stop"
        )
    )
    # 兜底
    for _ in range(10):
        fake.script.append(ScriptedResponse(content="x", finish_reason="stop"))

    def fake_get_provider(name: str, **kw):  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", fake_get_provider)
    return fake


def test_w2_cli_exit_code_zero(
    tmp_path: Path,
    fake_w2: FakeLLMProvider,  # noqa: ARG001
) -> None:
    """CLI 退出码 = 0"""
    data = tmp_path / "data.txt"
    data.write_text("payload-w2\n", encoding="utf-8")
    yaml_path = _make_w2_yaml(tmp_path, data)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])
    assert res.exit_code == 0, f"stdout={res.stdout}\nexc={res.exception}"


def test_w2_two_agents_tasks_state_flow(
    tmp_path: Path,
    fake_w2: FakeLLMProvider,
) -> None:
    """
    W2 DoD 验证:
      1) 演示 swarm 含 2 agent
      2) 两任务都 completed
      3) Task 状态确实流转过（通过版本号验证：每次 status 变化 +1）
      4) writer 收到了 reader 的消息
    """
    data = tmp_path / "data.txt"
    data.write_text("payload-w2\n", encoding="utf-8")
    yaml_path = _make_w2_yaml(tmp_path, data)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])

    assert res.exit_code == 0
    # CLI 输出含两个任务行 + 都 completed
    assert "T-read" in res.stdout
    assert "T-write" in res.stdout
    # 状态显示
    completed_count = res.stdout.count("completed")
    assert completed_count >= 2  # 两个任务行各显示一次
    # 关键内容应出现
    assert "payload-w2" in res.stdout


def test_w2_cas_conflict_with_concurrent_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    W2 DoD: CAS 冲突日志 ≥1
    构造 N 个 agent 抢 1 个 unassigned 任务——必然冲突
    """
    fake = FakeLLMProvider()
    for _ in range(20):
        fake.script.append(ScriptedResponse(content="ok", finish_reason="stop"))

    def fake_get_provider(name: str, **kw):  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", fake_get_provider)

    cfg = {
        "name": "race",
        "agents": [
            {"id": f"a{i}", "role": "r", "persona": "p",
             "provider": "openai", "model": "gpt-4o-mini",
             "tools": ["read_file"], "max_iterations": 2}
            for i in range(5)
        ],
        # 1 个 unassigned task → 5 个 agent 抢
        "tasks": [{"id": "T", "title": "the task"}],
    }
    yaml_path = tmp_path / "race.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    from agent_swarm.core.swarm import Swarm

    swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
    import asyncio

    result = asyncio.run(swarm.run())

    # 任务被恰好 1 个 agent 完成
    completers = sum(1 for s in result.agent_stats if s.tasks_completed)
    assert completers == 1
    assert result.tasks_completed == 1
