"""
@module tests.unit.test_worktree_integration
@brief  P4-W23 WorktreeIntegration 单元测试

覆盖:
  - substitute_placeholders 替换 command / cwd / env
  - validate_config 错误检测
  - find_placeholders 定位占位符
  - WorktreeIntegration acquire_for_agent / release_for_agent / materialize_config
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_swarm.mcp.registry import MCPServerConfig
from agent_swarm.worktree import (
    PLACEHOLDER,
    WorktreeIntegration,
    find_placeholders,
    substitute_placeholders,
    validate_config,
)
from agent_swarm.worktree.manager import WorktreeManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"],
        check=True, capture_output=True, timeout=5,
    )
    (repo / "README.md").write_text("# T", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, timeout=10,
    )
    return repo


@pytest.fixture
def manager(git_repo: Path, tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(git_repo, base_dir=tmp_path / "worktrees")


@pytest.fixture
def integration(manager: WorktreeManager) -> WorktreeIntegration:
    return WorktreeIntegration(manager)


def _make_config(
    *,
    name: str = "filesystem",
    command: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    token: str | None = None,
    transport: str = "stdio",
) -> MCPServerConfig:
    if transport == "stdio":
        return MCPServerConfig(
            name=name,
            transport="stdio",
            command=command or [
                "npx", "-y", "@modelcontextprotocol/server-filesystem",
                PLACEHOLDER,
            ],
            cwd=cwd,
            env=env or {},
        )
    return MCPServerConfig(
        name=name,
        transport="sse",
        command=[],
        env=env or {},
        cwd=cwd,
        url=url or "https://example.com",
        token=token,
    )


# ---------------------------------------------------------------------------
# substitute_placeholders
# ---------------------------------------------------------------------------


def test_substitute_command(manager: WorktreeManager) -> None:
    """command 列表里的占位符被替换"""
    cfg = _make_config(command=["echo", PLACEHOLDER, "--opt", PLACEHOLDER])
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    new = substitute_placeholders(cfg, h.path)
    assert str(h.path) in new.command[1]
    assert str(h.path) in new.command[3]
    assert PLACEHOLDER not in " ".join(new.command)


def test_substitute_cwd(manager: WorktreeManager) -> None:
    """cwd 里的占位符被替换"""
    cfg = _make_config(cwd=PLACEHOLDER + "/subdir")
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    new = substitute_placeholders(cfg, h.path)
    assert new.cwd is not None
    assert str(h.path) in new.cwd
    assert PLACEHOLDER not in new.cwd


def test_substitute_env(manager: WorktreeManager) -> None:
    """env 字典里的占位符被替换"""
    cfg = _make_config(env={"WORKDIR": PLACEHOLDER, "PLAIN": "no-placeholder"})
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    new = substitute_placeholders(cfg, h.path)
    assert str(h.path) in new.env["WORKDIR"]
    assert new.env["PLAIN"] == "no-placeholder"
    assert PLACEHOLDER not in new.env["WORKDIR"]


def test_substitute_returns_new_config(manager: WorktreeManager) -> None:
    """substitute 返回新 config, 不修改原对象"""
    cfg = _make_config(command=["echo", PLACEHOLDER])
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    new = substitute_placeholders(cfg, h.path)
    assert new is not cfg
    # 原 config 仍含占位符
    assert PLACEHOLDER in cfg.command[1]
    # 新 config 不含占位符
    assert PLACEHOLDER not in new.command[1]
    # 新 config 含 worktree 路径
    assert str(h.path) in new.command[1]


def test_substitute_idempotent(manager: WorktreeManager) -> None:
    """对已替换的 config 再 substitute 不会有副作用"""
    cfg = _make_config(command=["echo", PLACEHOLDER])
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    once = substitute_placeholders(cfg, h.path)
    twice = substitute_placeholders(once, h.path)
    assert once.command == twice.command


# ---------------------------------------------------------------------------
# find_placeholders
# ---------------------------------------------------------------------------


def test_find_placeholders_command() -> None:
    """find_placeholders 报告 command 里的占位符"""
    cfg = _make_config(command=["echo", PLACEHOLDER])
    found = find_placeholders(cfg)
    assert any("command" in s for s in found)


def test_find_placeholders_no_match() -> None:
    """无占位符返回空列表"""
    cfg = _make_config(command=["echo", "/some/path"], env={"X": "y"})
    found = find_placeholders(cfg)
    assert found == []


def test_find_placeholders_env() -> None:
    """env 里的占位符也被报告"""
    cfg = _make_config(env={"X": PLACEHOLDER})
    found = find_placeholders(cfg)
    assert any("env[X]" in s for s in found)


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


def test_validate_rejects_placeholder_in_token() -> None:
    """token 不能含占位符"""
    cfg = _make_config(
        transport="sse",
        url="https://example.com",
        token=PLACEHOLDER,  # 故意放错位置
    )
    with pytest.raises(ValueError, match="token should not contain"):
        validate_config(cfg)


def test_validate_rejects_placeholder_in_url() -> None:
    """url 不能含占位符"""
    cfg = _make_config(
        transport="sse",
        url="https://example.com/" + PLACEHOLDER,
    )
    with pytest.raises(ValueError, match="url should not contain"):
        validate_config(cfg)


def test_validate_accepts_command_placeholder() -> None:
    """command / cwd / env 里的占位符 OK"""
    cfg = _make_config()
    validate_config(cfg)  # 不抛


# ---------------------------------------------------------------------------
# WorktreeIntegration
# ---------------------------------------------------------------------------


def test_integration_acquire_for_agent(integration: WorktreeIntegration) -> None:
    """acquire_for_agent: 返回 WorktreeHandle"""
    h = integration.acquire_for_agent(
        agent_id="a1", tenant_id="t1", session_id="s1",
    )
    assert h.agent_id == "a1"
    assert h.tenant_id == "t1"
    assert h.session_id == "s1"


def test_integration_acquire_default_tenant(
    integration: WorktreeIntegration,
) -> None:
    """acquire_for_agent 不传 tenant/session 用 'default'"""
    h = integration.acquire_for_agent(agent_id="a1")
    assert h.tenant_id == "default"
    assert h.session_id == "default"


def test_integration_release_for_agent(integration: WorktreeIntegration) -> None:
    """release_for_agent: 清理 worktree"""
    h = integration.acquire_for_agent(agent_id="a1", tenant_id="t1", session_id="s1")
    assert h.path.exists()
    integration.release_for_agent(h)
    assert not h.path.exists()


def test_integration_materialize_config(
    integration: WorktreeIntegration,
) -> None:
    """materialize_config: 把 config 注入 handle 的 path"""
    cfg = _make_config()
    h = integration.acquire_for_agent(agent_id="a1", tenant_id="t1", session_id="s1")
    new = integration.materialize_config(cfg, h)
    assert str(h.path) in " ".join(new.command)
    assert PLACEHOLDER not in " ".join(new.command)


def test_integration_full_flow_two_agents(integration: WorktreeIntegration) -> None:
    """2 agent 各自 worktree, 各自 config 注入, 文件隔离"""
    cfg = _make_config(
        command=["server", "--workspace", PLACEHOLDER],
        env={"PWD": PLACEHOLDER},
    )
    h1 = integration.acquire_for_agent(agent_id="a1", tenant_id="t1", session_id="s1")
    h2 = integration.acquire_for_agent(agent_id="a2", tenant_id="t1", session_id="s1")
    c1 = integration.materialize_config(cfg, h1)
    c2 = integration.materialize_config(cfg, h2)
    # 不同 worktree 路径
    assert str(h1.path) in c1.command[2]
    assert str(h2.path) in c2.command[2]
    assert c1.command[2] != c2.command[2]
    # 各自能写文件
    (h1.path / "a1.txt").write_text("from a1", encoding="utf-8")
    (h2.path / "a2.txt").write_text("from a2", encoding="utf-8")
    assert (h1.path / "a1.txt").read_text(encoding="utf-8") == "from a1"
    assert not (h1.path / "a2.txt").exists()
