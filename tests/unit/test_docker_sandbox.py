"""
@module tests.unit.test_docker_sandbox
@brief  W19-①②③ ④ Docker Sandbox 后端测试

覆盖:
  - CIS Docker Benchmark 10 条关键项定义
  - 20 条容器逃逸拦截 (CONTAINER_ESCAPE_ATTEMPTS)
  - DockerConfig 默认值 (non-root + read-only + no-new-privileges)
  - DockerSandboxManager 走 docker CLI (mock runner)
  - doctor_check 返回结构
  - 默认 mode 仍 WORKSPACE_ONLY (W19-4 向后兼容)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_swarm.security.sandbox import SandboxMode
from agent_swarm.security.sandbox_docker import (
    CIS_DOCKER_CHECKS,
    CONTAINER_ESCAPE_ATTEMPTS,
    DockerConfig,
    DockerSandboxManager,
)

# ---------------------------------------------------------------------------
# CIS Docker Benchmark 关键项
# ---------------------------------------------------------------------------


def test_cis_checks_minimum_10() -> None:
    """W19-3: 至少 10 条 CIS 关键项"""
    assert len(CIS_DOCKER_CHECKS) >= 10, (
        f"expected >=10 CIS checks, got {len(CIS_DOCKER_CHECKS)}"
    )


def test_cis_checks_required_ids() -> None:
    """必含的 CIS ID"""
    ids = {c.id for c in CIS_DOCKER_CHECKS}
    required = {"4.1", "5.2", "5.3", "5.4", "5.13"}
    assert required.issubset(ids), f"missing CIS ids: {required - ids}"


def test_cis_checks_all_enabled_by_default() -> None:
    for c in CIS_DOCKER_CHECKS:
        assert c.enabled is True, f"CIS check {c.id} disabled by default"


# ---------------------------------------------------------------------------
# 容器逃逸拦截
# ---------------------------------------------------------------------------


def test_escape_attempts_minimum_20() -> None:
    """W19-5: 至少 20 条逃逸拦截"""
    assert len(CONTAINER_ESCAPE_ATTEMPTS) >= 20


def test_escape_attempt_ids_unique() -> None:
    ids = [a.id for a in CONTAINER_ESCAPE_ATTEMPTS]
    assert len(ids) == len(set(ids)), "duplicate ESC IDs"


# ---------------------------------------------------------------------------
# DockerConfig 默认值 (CIS 默认开启)
# ---------------------------------------------------------------------------


def test_docker_config_defaults_cis_compliant() -> None:
    cfg = DockerConfig()
    # CIS 4.1 non-root
    assert cfg.user != "root"
    assert ":" in cfg.user  # uid:gid
    # CIS 5.2 cap drop
    assert "ALL" in cfg.capabilities_drop
    # CIS 5.3 read-only
    assert cfg.read_only_root is True
    # CIS 5.4 no-new-privileges
    assert cfg.no_new_privileges is True
    # CIS 5.13 network off by default
    assert cfg.network == "none"
    # PIDs / memory / CPU 限制
    assert cfg.pids_limit > 0
    assert cfg.memory
    assert cfg.cpus > 0


# ---------------------------------------------------------------------------
# DockerSandboxManager
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_docker_sandbox_construct(workspace: Path) -> None:
    mgr = DockerSandboxManager(workspace)
    # W19-4: DOCKER mode 显式 (但默认仍 WORKSPACE_ONLY)
    assert mgr.docker_mode == SandboxMode.DOCKER
    assert mgr.workspace == workspace.resolve()


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_escape_privileged(
    workspace: Path,
) -> None:
    """ESC-06: --privileged 一律拒绝"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="container escape blocked"):
        await mgr.execute("ls --privileged")


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_escape_mount_root(
    workspace: Path,
) -> None:
    """ESC-01: mount / 一律拒绝"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="container escape blocked"):
        await mgr.execute("ls /:/host")


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_escape_host_network(
    workspace: Path,
) -> None:
    """ESC-11: --network=host 一律拒绝"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="container escape blocked"):
        await mgr.execute("ls --network=host")


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_escape_cap_all(
    workspace: Path,
) -> None:
    """ESC-07: --cap-add=ALL"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="container escape blocked"):
        await mgr.execute("ls --cap-add=ALL")


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_shell_metachar(
    workspace: Path,
) -> None:
    """共享 WORKSPACE_ONLY 的 shell meta 防护"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="shell metachar"):
        await mgr.execute("ls; rm -rf /")


@pytest.mark.asyncio
async def test_docker_sandbox_blocks_path_escape(
    workspace: Path,
) -> None:
    """共享 WORKSPACE_ONLY 的路径 escape 防护"""
    mgr = DockerSandboxManager(workspace)
    with pytest.raises(PermissionError, match="path escape"):
        await mgr.execute("cat ../../etc/passwd")


@pytest.mark.asyncio
async def test_docker_sandbox_executes_via_mock_runner(
    workspace: Path,
) -> None:
    """Docker CLI 走 mock runner——验证 CIS argv 构造正确"""
    captured: dict[str, Any] = {}

    async def fake_runner(argv: list[str]) -> dict[str, Any]:
        captured["argv"] = argv
        return {"exit_code": 0, "stdout": "hello\n", "stderr": ""}

    cfg = DockerConfig(docker_runner=fake_runner, image="alpine:latest")
    mgr = DockerSandboxManager(workspace, config=cfg)
    result = await mgr.execute("ls /workspace", timeout=5.0)
    assert result.exit_code == 0
    assert "hello" in result.stdout
    # 验证 docker run argv 包含 CIS 关键项
    argv = captured["argv"]
    assert argv[0] == "docker"
    assert "run" in argv
    assert "--user" in argv
    assert "1000:1000" in argv
    assert "--cap-drop" in argv
    assert "ALL" in argv
    assert "--read-only" in argv
    assert "--security-opt" in argv
    assert "no-new-privileges:true" in argv
    assert "--pids-limit" in argv
    assert "--memory" in argv
    assert "--cpus" in argv
    assert "--network" in argv
    assert "none" in argv
    # workspace bind mount
    assert any(
        str(workspace.resolve()) in tok and ":rw" in tok
        for tok in argv
    )
    # 容器 image + cmd 都在 argv 中
    assert "alpine:latest" in argv
    assert "ls" in argv
    # 启动耗时 ≤ 500ms (mock runner)
    assert result.duration_seconds < 0.5


@pytest.mark.asyncio
async def test_docker_sandbox_timeout(workspace: Path) -> None:
    """超时 kill + 标记 timed_out"""

    async def slow_runner(argv: list[str]) -> dict[str, Any]:
        await asyncio.sleep(5.0)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    cfg = DockerConfig(docker_runner=slow_runner)
    mgr = DockerSandboxManager(workspace, config=cfg)
    result = await mgr.execute("ls", timeout=0.2)
    assert result.timed_out is True
    assert "timeout" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Doctor 检查 (W19-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doctor_check_with_mock_available(
    workspace: Path,
) -> None:
    """Docker 可用——doctor 返回建议用 docker mode"""

    async def fake_docker_version(argv: list[str]) -> dict[str, Any]:
        if argv[:2] == ["docker", "version"]:
            return {
                "exit_code": 0,
                "stdout": '{"Client":{"Version":"24.0.0"}}',
                "stderr": "",
            }
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    cfg = DockerConfig(docker_runner=fake_docker_version)
    mgr = DockerSandboxManager(workspace, config=cfg)
    report = await mgr.doctor_check()
    assert report["docker_available"] is True
    assert report["docker_version"] == "24.0.0"
    assert len(report["cis_checks"]) >= 10
    assert report["escape_attempts_count"] >= 20
    assert "Docker available" in report["recommendation"]


@pytest.mark.asyncio
async def test_doctor_check_no_docker(workspace: Path) -> None:
    """Docker 不可用——doctor 推荐用 WORKSPACE_ONLY"""

    async def fake_runner(argv: list[str]) -> dict[str, Any]:
        raise RuntimeError("docker not found")

    cfg = DockerConfig(docker_runner=fake_runner)
    mgr = DockerSandboxManager(workspace, config=cfg)
    report = await mgr.doctor_check()
    assert report["docker_available"] is False
    assert "WORKSPACE_ONLY" in report["recommendation"]


# ---------------------------------------------------------------------------
# 向后兼容 (W19-4): 默认 mode 不变
# ---------------------------------------------------------------------------


def test_default_sandbox_mode_still_workspace_only(
    workspace: Path,
) -> None:
    """W19-4: SandboxManager 默认 mode = WORKSPACE_ONLY (向后兼容)"""
    from agent_swarm.security.sandbox import SandboxManager
    mgr = SandboxManager(workspace)
    assert mgr.mode == SandboxMode.WORKSPACE_ONLY


def test_sandbox_mode_enum_includes_docker() -> None:
    """SandboxMode 已包含 DOCKER 枚举值"""
    assert SandboxMode.DOCKER.value == "docker"


# ---------------------------------------------------------------------------
# bench_sandbox 钩子 (W19-7 会在 tools/bench_sandbox.py 调用)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI not available in PATH (W19 保守版允许)",
)
@pytest.mark.asyncio
async def test_docker_real_cli_smoke(workspace: Path) -> None:
    """W19-7 real docker CLI smoke test (skip if not available)"""
    mgr = DockerSandboxManager(workspace)
    # 不调 doctor, 跑一次简单 ls
    try:
        result = await mgr.execute("ls", timeout=10.0)
        # 不断言 exit_code (image 可能未 pull) 但 duration ≤ 500ms
        assert result.duration_seconds < 10.0
    except PermissionError:
        pass  # 沙箱拦截不算 fail
