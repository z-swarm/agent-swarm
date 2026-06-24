"""
@module tests.e2e.test_w1_hello
@brief  W1 验收 e2e（DESIGN.md §17.2 W1 DoD）

DoD:
  - `agent-swarm run examples/w1_hello.yaml` 退出码=0
  - 输出包含 README 关键词
  - 单 agent + read_file 工具走通 OTAR

实现策略:
  - 替换 OpenAIProvider 为 FakeLLMProvider（脚本回放）
  - 通过 monkeypatch 注入到 get_provider
  - 用 click testing 跑 CLI
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
    sys.platform == "win32",
    reason="P3-WIN: e2e CLI run has Windows shell differences",
)


def _make_w1_yaml(tmp_path: Path, readme_path: Path) -> Path:
    cfg = {
        "name": "w1-hello",
        "agents": [
            {
                "id": "reader-1",
                "role": "documentation reader",
                "persona": "Read documents and answer concisely.",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["read_file"],
                "max_iterations": 5,
            }
        ],
        "tasks": [
            {
                "title": "Summarize README",
                "description": f"Read {readme_path.name} and produce a one-line summary.",
            }
        ],
    }
    p = tmp_path / "w1.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


@pytest.fixture
def fake_w1(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    """
    在 get_provider 路径上注入 FakeLLMProvider
    脚本编排 OTAR：第 1 轮调 read_file，第 2 轮 stop
    """
    fake = FakeLLMProvider(default_model="gpt-4o-mini")
    fake.script.append(
        ScriptedResponse(
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})],
            finish_reason="tool_use",
        )
    )
    fake.script.append(
        ScriptedResponse(
            content="The project is agent-swarm: a multi-agent collaboration framework.",
            finish_reason="stop",
        )
    )

    # monkeypatch get_provider 在 swarm 模块的引用（from-import 已绑定）
    def fake_get_provider(name: str, **kw):  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", fake_get_provider)
    return fake


def test_w1_hello_cli_exit_code_zero(
    tmp_path: Path,
    fake_w1: FakeLLMProvider,  # noqa: ARG001 - 触发注入
) -> None:
    """CLI 退出码 = 0"""
    readme = tmp_path / "README.md"
    readme.write_text(
        "# agent-swarm\n\nA multi-agent collaboration framework.\n",
        encoding="utf-8",
    )
    yaml_path = _make_w1_yaml(tmp_path, readme)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])

    assert res.exit_code == 0, f"stdout={res.stdout}\nexc={res.exception}"


def test_w1_hello_output_contains_readme_keywords(
    tmp_path: Path,
    fake_w1: FakeLLMProvider,
) -> None:
    """输出包含 README 关键词（DoD 要求）"""
    readme = tmp_path / "README.md"
    readme.write_text(
        "# agent-swarm\n\nA multi-agent collaboration framework.\n",
        encoding="utf-8",
    )
    yaml_path = _make_w1_yaml(tmp_path, readme)

    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(yaml_path)])

    assert "agent-swarm" in res.stdout
    assert "framework" in res.stdout
    # task status 应为 completed
    assert "completed" in res.stdout

    # 验证 OTAR 真的发生：fake_llm 被调用了 2 次
    assert len(fake_w1.calls) == 2
    # 第 2 次 chat 必须含 tool 角色消息（说明 act 阶段把工具结果回灌了）
    second_call_roles = [t.role for t in fake_w1.calls[1]]
    assert "tool" in second_call_roles


def test_w1_handles_missing_yaml(tmp_path: Path) -> None:
    """不存在的配置文件返回非 0 退出码"""
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(tmp_path / "nope.yaml")])
    assert res.exit_code != 0
