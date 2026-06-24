"""
@module tests.integration.test_skill_in_agent
@brief  Skill 系统 + AgentRunner 集成——验证 skill prompt 被注入

层级: integration——AgentRunner + SkillRegistry + FakeLLMProvider
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.agent_runner import AgentRunner
from agent_swarm.core.types import Agent, AgentCapabilities, Task
from agent_swarm.tools.builtin.file_ops import ReadFileTool
from tests.conftest import FakeLLMProvider, ScriptedResponse


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "code.py").write_text(
        "query = f'SELECT * FROM x WHERE id = {uid}'", encoding="utf-8"
    )
    return tmp_path


def _agent_with_skills(skill_ids: list[str]) -> Agent:
    return Agent(
        id="sec-1",
        role="security expert",
        persona="meticulous reviewer",
        provider="openai",
        model="gpt-4o-mini",
        capabilities=AgentCapabilities.worker({"read_file"}),
        tools=["read_file"],
        skills=skill_ids,
        max_iterations=3,
    )


async def test_security_skill_extends_system_prompt(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """启用 code-review:security 后，system prompt 必含 SQL Injection 检查清单"""
    fake_llm.script.append(ScriptedResponse(content="reviewed", finish_reason="stop"))

    agent = _agent_with_skills(["code-review:security"])
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    await runner.run(Task(id="t", title="review", description="check"))

    # 第一次 LLM 调用的 system message 应含 skill prompt
    sys_turn = next(t for t in fake_llm.calls[0] if t.role == "system")
    assert "SQL Injection" in sys_turn.content
    assert "code-review:security" in sys_turn.content
    assert "security expert" in sys_turn.content


async def test_no_skill_basic_prompt_only(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """不启用 skill 时——system prompt 仅含 base persona，不含 Skills 段落"""
    fake_llm.script.append(ScriptedResponse(content="ok", finish_reason="stop"))

    agent = _agent_with_skills([])
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    await runner.run(Task(id="t", title="x", description="y"))

    sys_turn = next(t for t in fake_llm.calls[0] if t.role == "system")
    assert "SQL Injection" not in sys_turn.content
    assert "Skills" not in sys_turn.content


async def test_unknown_skill_warned_but_not_raised(
    fake_llm: FakeLLMProvider, workspace: Path, caplog
) -> None:
    """skill 不存在——记 warning，agent 仍能跑（向前兼容）"""
    fake_llm.script.append(ScriptedResponse(content="ok", finish_reason="stop"))

    agent = _agent_with_skills(["bogus:nonexistent"])
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})

    import logging

    with caplog.at_level(logging.WARNING):
        res = await runner.run(Task(id="t", title="x", description="y"))

    assert res.task.status == "completed"
    # warning 应出现在日志
    assert any("bogus:nonexistent" in r.message for r in caplog.records)


async def test_multiple_skills_compose(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """多个 skill 都应被注入"""
    fake_llm.script.append(ScriptedResponse(content="ok", finish_reason="stop"))

    agent = _agent_with_skills(
        [
            "code-review:security",
            "code-review:performance",
        ]
    )
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    await runner.run(Task(id="t", title="x", description="y"))

    sys_turn = next(t for t in fake_llm.calls[0] if t.role == "system")
    assert "code-review:security" in sys_turn.content
    assert "code-review:performance" in sys_turn.content


# ---------------------------------------------------------------------------
# YAML → Swarm 加载时 skill required_tools 自动并入
# ---------------------------------------------------------------------------


async def test_yaml_skills_auto_includes_required_tools(tmp_path: Path) -> None:
    """yaml 只声明 skills，不显式列 tools——但 read_file 应自动并入"""
    import yaml as _yaml

    from agent_swarm.core.swarm import Swarm

    cfg = {
        "name": "skill-only",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "persona": "p",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "skills": ["code-review:security"],
                # 注意：tools 没有显式给
            }
        ],
        "tasks": [{"title": "t"}],
    }
    p = tmp_path / "x.yaml"
    p.write_text(_yaml.safe_dump(cfg), encoding="utf-8")

    swarm = Swarm.from_yaml(p)
    a = swarm.agents[0]
    # required_tools 已并入 capabilities + tools
    assert "read_file" in a.capabilities.allowed_tools
    assert "read_file" in a.tools
    assert a.skills == ["code-review:security"]
