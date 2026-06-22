"""
@module tests.unit.test_sandbox_docker_long_lived
@brief  P4-W24 DockerSandboxManager 长生命周期测试

覆盖:
  - long_lived=True: 首次 execute 启容器, 后续 docker exec
  - 100 次 execute 只启 1 容器
  - close() 停容器
  - async context manager (async with) 也能 stop
  - long_lived=False 兼容 W19 行为 (每次 docker run)
  - CIS 安全参数仍然生效 (start 时检查 docker_argv 包含 --cap-drop=ALL 等)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_swarm.security.sandbox_docker import (
    DockerConfig,
    DockerSandboxManager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeDockerRunner:
    """模拟 docker CLI, 记录调用并返回可控结果"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.run_counter = 0
        self.exec_counter = 0
        self.stop_counter = 0
        self.container_id = "abc123def456"
        # 模拟容器状态: 启动后一直存活
        self.container_alive = False

    async def __call__(self, argv: list[str]) -> dict[str, Any]:
        self.calls.append(list(argv))
        cmd = argv[1] if len(argv) > 1 else ""
        if cmd == "run":
            self.run_counter += 1
            self.container_alive = True
            return {
                "exit_code": 0,
                "stdout": self.container_id,
                "stderr": "",
            }
        if cmd == "exec":
            self.exec_counter += 1
            return {
                "exit_code": 0,
                "stdout": f"exec-output-{self.exec_counter}",
                "stderr": "",
            }
        if cmd == "stop":
            self.stop_counter += 1
            self.container_alive = False
            return {"exit_code": 0, "stdout": self.container_id, "stderr": ""}
        if cmd == "version":
            return {"exit_code": 0, "stdout": "{}", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runner() -> FakeDockerRunner:
    return FakeDockerRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def manager(workspace: Path, fake_runner: FakeDockerRunner) -> DockerSandboxManager:
    cfg = DockerConfig(
        docker_runner=fake_runner,  # type: ignore[arg-type]
        long_lived=True,
    )
    return DockerSandboxManager(workspace, config=cfg)


# ---------------------------------------------------------------------------
# Long-lived mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_lived_first_execute_starts_container(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """首次 execute() 应启动容器 (docker run -d) + exec 一次"""
    await manager.execute("echo hello")
    assert fake_runner.run_counter == 1
    # 第一次 execute 内部: 1 run + 1 exec
    assert fake_runner.exec_counter == 1
    # 启动参数含 sleep infinity
    run_call = next(c for c in fake_runner.calls if "run" in c)
    assert "-d" in run_call
    assert "sleep" in run_call
    assert "infinity" in run_call


@pytest.mark.asyncio
async def test_long_lived_subsequent_executes_reuse_container(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """后续 execute() 用 docker exec, 不再启新容器"""
    await manager.execute("echo a")
    await manager.execute("echo b")
    await manager.execute("echo c")
    assert fake_runner.run_counter == 1
    assert fake_runner.exec_counter == 3


@pytest.mark.asyncio
async def test_long_lived_100_executes_one_container(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """100 次 execute() 只启 1 容器 (vs W19 兼容模式的 100 次)"""
    for i in range(100):
        await manager.execute(f"echo {i}")
    assert fake_runner.run_counter == 1
    assert fake_runner.exec_counter == 100
    assert fake_runner.stop_counter == 0  # 还没关


@pytest.mark.asyncio
async def test_long_lived_close_stops_container(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """close() 调 docker stop"""
    await manager.execute("echo init")
    assert fake_runner.run_counter == 1
    await manager.close()
    assert fake_runner.stop_counter == 1
    # 再 execute 应该重启
    await manager.execute("echo again")
    assert fake_runner.run_counter == 2


@pytest.mark.asyncio
async def test_long_lived_async_context_manager(
    workspace: Path, fake_runner: FakeDockerRunner,
) -> None:
    """async with DockerSandboxManager(): 退出时自动 stop"""
    cfg = DockerConfig(
        docker_runner=fake_runner,  # type: ignore[arg-type]
        long_lived=True,
    )
    async with DockerSandboxManager(workspace, config=cfg) as mgr:
        await mgr.execute("echo in_ctx")
        assert fake_runner.run_counter == 1
        assert fake_runner.stop_counter == 0
    # 退出后 stop
    assert fake_runner.stop_counter == 1


@pytest.mark.asyncio
async def test_long_lived_close_idempotent(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """close() 多次调用安全"""
    await manager.execute("echo")
    await manager.close()
    await manager.close()  # 第二次 no-op
    assert fake_runner.stop_counter == 1


@pytest.mark.asyncio
async def test_long_lived_close_without_start(
    workspace: Path, fake_runner: FakeDockerRunner,
) -> None:
    """未启容器时 close() 不报错"""
    cfg = DockerConfig(
        docker_runner=fake_runner,  # type: ignore[arg-type]
        long_lived=True,
    )
    mgr = DockerSandboxManager(workspace, config=cfg)
    await mgr.close()  # no raise
    assert fake_runner.stop_counter == 0


# ---------------------------------------------------------------------------
# 兼容 W19 行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_lived_false_uses_run_each_time(
    workspace: Path, fake_runner: FakeDockerRunner,
) -> None:
    """long_lived=False 保持 W19 行为: 每次 docker run --rm"""
    cfg = DockerConfig(
        docker_runner=fake_runner,  # type: ignore[arg-type]
        long_lived=False,
    )
    mgr = DockerSandboxManager(workspace, config=cfg)
    await mgr.execute("echo a")
    await mgr.execute("echo b")
    assert fake_runner.run_counter == 2
    assert fake_runner.exec_counter == 0


# ---------------------------------------------------------------------------
# CIS 安全参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_lived_start_includes_cis_params(
    manager: DockerSandboxManager, fake_runner: FakeDockerRunner,
) -> None:
    """启动容器时 CIS 安全参数必须包含"""
    await manager.execute("echo")
    run_argv = fake_runner.calls[0]
    joined = " ".join(run_argv)
    assert "--user" in joined
    assert "1000:1000" in joined
    assert "--cap-drop" in joined
    assert "ALL" in joined
    assert "--read-only" in joined
    assert "no-new-privileges:true" in joined
    assert "--pids-limit" in joined
    assert "--memory" in joined
    assert "--cpus" in joined
    assert "--network" in joined
    assert "none" in joined
    assert "-v" in joined
    assert "/workspace" in joined


@pytest.mark.asyncio
async def test_long_lived_container_name_unique_per_workspace(
    workspace: Path, tmp_path: Path, fake_runner: FakeDockerRunner,
) -> None:
    """不同 workspace 拿不同容器名"""
    cfg = DockerConfig(
        docker_runner=fake_runner,  # type: ignore[arg-type]
        long_lived=True,
    )
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    mgr1 = DockerSandboxManager(workspace, config=cfg)
    mgr2 = DockerSandboxManager(ws2, config=cfg)
    await mgr1.execute("echo")
    await mgr2.execute("echo")
    # 提取两次 docker run 的 --name 参数 (calls 中每第 2 条是 run)
    # 顺序: [run1, exec1, run2, exec2] -> 提取 calls[0] 和 calls[2]
    name1 = next(
        a for a in fake_runner.calls[0] if a.startswith("agentswarm-")
    )
    name2 = next(
        a for a in fake_runner.calls[2] if a.startswith("agentswarm-")
    )
    assert name1 != name2, f"expected different names, got {name1!r} and {name2!r}"
    # 也验证 hash 段不同 (workspace 路径不同)
    assert name1.split("-")[1] != name2.split("-")[1]
    await mgr1.close()
    await mgr2.close()


@pytest.mark.asyncio
async def test_long_lived_start_failure_returns_error_result(
    workspace: Path,
) -> None:
    """容器启动失败应返回错误 SandboxResult, 不抛"""
    async def failing_runner(argv: list[str]) -> dict[str, Any]:
        return {"exit_code": 1, "stdout": "", "stderr": "image not found"}

    cfg = DockerConfig(
        docker_runner=failing_runner,  # type: ignore[arg-type]
        long_lived=True,
    )
    mgr = DockerSandboxManager(workspace, config=cfg)
    result = await mgr.execute("echo")
    assert result.exit_code == -1
    assert "start failed" in result.stderr


# ---------------------------------------------------------------------------
# 容器名生成
# ---------------------------------------------------------------------------


def test_container_name_format(manager: DockerSandboxManager) -> None:
    """容器名 = prefix + workspace hash + pid + counter"""
    name = manager._make_container_name()
    assert name.startswith("agentswarm-")
    # 应包含 4 段
    parts = name.split("-")
    assert len(parts) == 4
    # 第 2 段是 hex (8 chars)
    assert len(parts[1]) == 8
    # 第 3 段是 pid
    assert parts[2] == str(__import__("os").getpid())
    # 第 4 段是 counter (4-digit int)
    assert len(parts[3]) == 4
    int(parts[3])  # 应是有效数字


def test_container_name_prefix_configurable(workspace: Path) -> None:
    """container_name_prefix 可配"""
    cfg = DockerConfig(
        docker_runner=None,
        long_lived=True,
        container_name_prefix="myapp",
    )
    mgr = DockerSandboxManager(workspace, config=cfg)
    name = mgr._make_container_name()
    assert name.startswith("myapp-")
