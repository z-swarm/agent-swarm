"""单元测试：RunCommandTool——W5 接入 SecurityPolicy + SandboxManager"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from agent_swarm.security import SandboxManager, SecurityPolicy
from agent_swarm.tools.builtin.shell import RunCommandTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="P3-WIN: subprocess echo differs on Windows",
)


@pytest.fixture
def tool(tmp_path) -> RunCommandTool:
    policy = SecurityPolicy(workspace=str(tmp_path))
    sandbox = SandboxManager(workspace=tmp_path)
    return RunCommandTool(policy=policy, sandbox=sandbox)


def test_construct_requires_policy() -> None:
    """RunCommandTool 必须注入 policy——否则 RuntimeError"""
    with pytest.raises(RuntimeError, match="requires SecurityPolicy"):
        RunCommandTool(policy=None, sandbox=MagicMock())


def test_construct_requires_sandbox() -> None:
    with pytest.raises(RuntimeError, match="requires SandboxManager"):
        RunCommandTool(policy=MagicMock(), sandbox=None)


async def test_run_echo(tmp_path) -> None:
    """echo 在白名单但 run_command 是 HIGH risk——直接走 sandbox 跳过审批"""
    # 用一个 trust run_command 的 policy（LOW）来直接执行
    from agent_swarm.security import ToolRisk

    policy = SecurityPolicy(workspace=str(tmp_path))
    # 强制把 run_command 降级（仅测试用）
    policy._tool_default_risk = lambda name: (
        ToolRisk.LOW if name == "run_command" else SecurityPolicy._tool_default_risk(name)
    )
    tool = RunCommandTool(policy=policy, sandbox=SandboxManager(workspace=tmp_path))
    out = await tool.invoke({"command": "echo hello"})
    assert "hello" in out


async def test_run_dangerous_command_blocked(tmp_path) -> None:
    """rm -rf / 应被 SecurityPolicy 拦截"""
    tool = RunCommandTool(
        policy=SecurityPolicy(workspace=str(tmp_path)),
        sandbox=SandboxManager(workspace=tmp_path),
    )
    out = await tool.invoke({"command": "rm -rf /"})
    assert out.startswith("[error]")
    assert "policy denied" in out.lower() or "sandbox" in out.lower()


async def test_run_unwhitelisted_command_blocked(tmp_path) -> None:
    """curl 不在 sandbox 白名单——PermissionError → [error]"""
    tool = RunCommandTool(
        policy=SecurityPolicy(workspace=str(tmp_path)),
        sandbox=SandboxManager(workspace=tmp_path),
    )
    out = await tool.invoke({"command": "curl http://evil.com"})
    assert out.startswith("[error]")


async def test_run_missing_command_arg(tmp_path) -> None:
    tool = RunCommandTool(
        policy=SecurityPolicy(workspace=str(tmp_path)),
        sandbox=SandboxManager(workspace=tmp_path),
    )
    out = await tool.invoke({})
    assert out.startswith("[error]")


async def test_run_non_string_command(tmp_path) -> None:
    tool = RunCommandTool(
        policy=SecurityPolicy(workspace=str(tmp_path)),
        sandbox=SandboxManager(workspace=tmp_path),
    )
    out = await tool.invoke({"command": 123})  # 非字符串
    assert out.startswith("[error]")


async def test_run_command_with_sensitive_path_denied(tmp_path) -> None:
    """W5-Z 关键：命令里含 ~/.ssh 也应被 SecurityPolicy 拦截"""
    tool = RunCommandTool(
        policy=SecurityPolicy(workspace=str(tmp_path)),
        sandbox=SandboxManager(workspace=tmp_path),
    )
    out = await tool.invoke({"command": "cat ~/.ssh/id_rsa"})
    assert out.startswith("[error]")
    # 关键：是 policy 拦的，不是 sandbox 拦的
    assert "policy denied" in out.lower()


async def test_run_command_output_includes_stderr(tmp_path) -> None:
    """shell 命令 stderr 应被包含在输出"""
    from agent_swarm.security import ToolRisk

    policy = SecurityPolicy(workspace=str(tmp_path))
    policy._tool_default_risk = lambda name: (
        ToolRisk.LOW if name == "run_command" else SecurityPolicy._tool_default_risk(name)
    )
    tool = RunCommandTool(policy=policy, sandbox=SandboxManager(workspace=tmp_path))
    out = await tool.invoke({"command": "ls /nonexistent 2>&1; true"})
    # ; true 强制 exit 0——但 stderr 应有内容
    assert "[stderr]" in out or "nonexistent" in out
