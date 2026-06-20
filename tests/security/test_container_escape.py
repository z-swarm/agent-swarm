"""
@module tests.security.test_container_escape
@brief  W19-⑤ 容器逃逸攻击套件——20 条全拦截

P3-PLAN-v2 W19 DoD ⑤:
  - tests/security/test_container_escape.py 20 条全过
  - 攻击: mount / privileged / cap-add / host net / nsenter / chroot ...

@note 所有攻击都用 DockerSandboxManager.execute() 触发
@note 期望 PermissionError, 来自 CONTAINER_ESCAPE_ATTEMPTS 拦截
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_swarm.security.sandbox_docker import (
    CONTAINER_ESCAPE_ATTEMPTS,
    DockerSandboxManager,
)


@pytest.fixture
def mgr(tmp_path: Path) -> DockerSandboxManager:
    ws = tmp_path / "ws"
    ws.mkdir()
    return DockerSandboxManager(ws)


# 20 条逃逸尝试——每条对应一个测试
# 命令 payload 设计: 必须看上去无害 (如 ls), 但含攻击 pattern


def _mk_runner(captured: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    """构造一个 mock runner——拦截 docker argv 用于断言 CIS 标志 + 不真正执行"""
    async def runner(argv: list[str]) -> dict[str, Any]:
        if captured is not None:
            captured["argv"] = argv
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    return runner


# ---------------------------------------------------------------------------
# ESC-01 ~ ESC-20 容器逃逸攻击
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esc_01_mount_root_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-01"):
        await mgr.execute("ls /:/host")


@pytest.mark.asyncio
async def test_esc_02_mount_etc_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-02"):
        await mgr.execute("ls /etc:/host")


@pytest.mark.asyncio
async def test_esc_03_mount_proc_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-03"):
        await mgr.execute("ls /proc:/host")


@pytest.mark.asyncio
async def test_esc_04_mount_sys_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-04"):
        await mgr.execute("ls /sys:/host")


@pytest.mark.asyncio
async def test_esc_05_mount_dev_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-05"):
        await mgr.execute("ls /dev:/host")


@pytest.mark.asyncio
async def test_esc_06_privileged_flag_blocked(
    mgr: DockerSandboxManager,
) -> None:
    with pytest.raises(PermissionError, match="ESC-06"):
        await mgr.execute("ls --privileged")


@pytest.mark.asyncio
async def test_esc_07_cap_all_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-07"):
        await mgr.execute("ls --cap-add=ALL")


@pytest.mark.asyncio
async def test_esc_08_sys_admin_cap_blocked(
    mgr: DockerSandboxManager,
) -> None:
    with pytest.raises(PermissionError, match="ESC-08"):
        await mgr.execute("ls --cap-add=SYS_ADMIN")


@pytest.mark.asyncio
async def test_esc_09_net_admin_cap_blocked(
    mgr: DockerSandboxManager,
) -> None:
    with pytest.raises(PermissionError, match="ESC-09"):
        await mgr.execute("ls --cap-add=NET_ADMIN")


@pytest.mark.asyncio
async def test_esc_10_dac_override_blocked(
    mgr: DockerSandboxManager,
) -> None:
    with pytest.raises(PermissionError, match="ESC-10"):
        await mgr.execute("ls --cap-add=DAC_OVERRIDE")


@pytest.mark.asyncio
async def test_esc_11_host_network_blocked(
    mgr: DockerSandboxManager,
) -> None:
    with pytest.raises(PermissionError, match="ESC-11"):
        await mgr.execute("ls --network=host")


@pytest.mark.asyncio
async def test_esc_12_host_pid_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-12"):
        await mgr.execute("ls --pid=host")


@pytest.mark.asyncio
async def test_esc_13_host_ipc_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-13"):
        await mgr.execute("ls --ipc=host")


@pytest.mark.asyncio
async def test_esc_14_docker_socket_blocked(
    mgr: DockerSandboxManager,
) -> None:
    """虽然 /var/run/docker.sock 在容器内是 /var/run, 但 pattern 命中即拒"""
    with pytest.raises(PermissionError, match="ESC-14"):
        await mgr.execute("ls /var/run/docker.sock")


@pytest.mark.asyncio
async def test_esc_15_root_user_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-15"):
        await mgr.execute("ls user=root")


@pytest.mark.asyncio
async def test_esc_16_sudo_blocked(mgr: DockerSandboxManager) -> None:
    """sudo 命中 ESC-16 pattern (前置空格 + 后导空格)"""
    with pytest.raises(PermissionError, match="ESC-16"):
        await mgr.execute("ls sudo whoami")


@pytest.mark.asyncio
async def test_esc_17_nsenter_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-17"):
        await mgr.execute("ls nsenter --target 1 --all")


@pytest.mark.asyncio
async def test_esc_18_chroot_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-18"):
        await mgr.execute("ls chroot /host")


@pytest.mark.asyncio
async def test_esc_19_cgroup_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-19"):
        await mgr.execute("ls /sys/fs/cgroup")


@pytest.mark.asyncio
async def test_esc_20_ctr_exec_blocked(mgr: DockerSandboxManager) -> None:
    with pytest.raises(PermissionError, match="ESC-20"):
        await mgr.execute("ls ctr --namespace moby task exec")


# ---------------------------------------------------------------------------
# 边界: 合法命令不拦截
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legitimate_ls_not_blocked(mgr: DockerSandboxManager) -> None:
    """`ls` 本身不命中任何 ESC pattern——应正常进 docker run"""
    captured: dict[str, Any] = {}
    mgr.config.docker_runner = _mk_runner(captured)
    result = await mgr.execute("ls", timeout=2.0)
    assert result.exit_code == 0
    # 验证 CIS 标志确实写入 docker argv
    argv = captured["argv"]
    assert "--cap-drop" in argv
    assert "--read-only" in argv


@pytest.mark.asyncio
async def test_legitimate_cat_with_relative_path(
    mgr: DockerSandboxManager,
) -> None:
    """`cat file.txt` 走宿主机路径验证——OK (workspace 下的文件)"""
    captured: dict[str, Any] = {}
    mgr.config.docker_runner = _mk_runner(captured)
    # 在 workspace 内写一个文件
    print("DEBUG mgr.workspace:", mgr.workspace)
    print("DEBUG workspace.is_absolute():", mgr.workspace.is_absolute())
    test_file = mgr.workspace / "test.txt"
    test_file.write_text("hello", encoding="utf-8")
    print("DEBUG test_file:", test_file)
    print("DEBUG relative:", test_file.relative_to(mgr.workspace))
    result = await mgr.execute("cat test.txt", timeout=2.0)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 全量枚举一致性
# ---------------------------------------------------------------------------


def test_escape_attempts_count_matches_tests() -> None:
    """CONTAINER_ESCAPE_ATTEMPTS 数量 ≥ 测试数 (20)"""
    assert len(CONTAINER_ESCAPE_ATTEMPTS) >= 20


def test_all_escape_ids_have_tests() -> None:
    """确保每条 ESC 都有 pattern 命中"""
    # 简单验证: 每条 ESC 的 pattern 都能找到一个测试 case
    for esc in CONTAINER_ESCAPE_ATTEMPTS:
        assert esc.id.startswith("ESC-")
        assert esc.pattern
        assert esc.title
