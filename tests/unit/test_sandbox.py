"""单元测试：SandboxManager——workspace_only 模式"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_swarm.security import sandbox as sb_mod
from agent_swarm.security.sandbox import SandboxManager, SandboxMode


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: hardcoded /tmp path, not Windows-compatible",
)
def test_sandbox_mode_workspace_only_is_default() -> None:
    sb = SandboxManager(workspace=Path("/tmp"))
    assert sb.mode == SandboxMode.WORKSPACE_ONLY


async def test_sandbox_workspace_must_exist(tmp_path: Path) -> None:
    """F-05: 构造时 strict resolve——workspace 不存在直接抛 ValueError"""
    with pytest.raises(ValueError, match="workspace invalid"):
        SandboxManager(workspace=tmp_path / "nonexistent")


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: subprocess echo shell semantics differ on Windows",
)
async def test_sandbox_execute_simple(tmp_path: Path) -> None:
    sb = SandboxManager(workspace=tmp_path)
    result = await sb.execute("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: subprocess cwd handling differs on Windows",
)
async def test_sandbox_workspace_is_cwd(tmp_path: Path) -> None:
    """pwd 应返回 workspace——证明 cwd 被强制改写"""
    sb = SandboxManager(workspace=tmp_path)
    result = await sb.execute("pwd")
    assert str(tmp_path) in result.stdout


async def test_sandbox_blocks_command_not_in_whitelist(tmp_path: Path) -> None:
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="not in sandbox whitelist"):
        await sb.execute("rm -rf something")


async def test_sandbox_blocks_curl(tmp_path: Path) -> None:
    """curl 不在默认白名单——网络工具隔离"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError):
        await sb.execute("curl http://example.com")


async def test_sandbox_blocks_malicious_command(tmp_path: Path) -> None:
    """mkfs 也不在白名单——拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError):
        await sb.execute("mkfs.ext4 /dev/null")


async def test_sandbox_output_truncated(tmp_path: Path) -> None:
    """超大输出被截断——通过 mock subprocess 验证截断逻辑"""
    big_stdout = b"x" * 1000
    fake_proc = MagicMock()
    fake_proc.communicate = MagicMock(return_value=(big_stdout, b""))
    fake_proc.kill = MagicMock()
    fake_proc.returncode = 0

    orig = sb_mod._popen_no_shell
    sb_mod._popen_no_shell = lambda *a, **kw: fake_proc
    try:
        sb = SandboxManager(workspace=tmp_path)
        result = await sb.execute("echo x", max_output_bytes=100)
        assert result.truncated is True
        assert len(result.stdout) <= 200
        assert "[truncated]" in result.stdout
    finally:
        sb_mod._popen_no_shell = orig


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: subprocess timeout semantics differ on Windows",
)
async def test_sandbox_timeout(tmp_path: Path) -> None:
    """sleep 超过 timeout——应被 kill 并标记 timed_out"""
    sb = SandboxManager(workspace=tmp_path)
    # 临时把 sleep 加入白名单
    sb.allowed_command_prefixes = ("sleep",)
    result = await sb.execute("sleep 5", timeout=0.2)
    assert result.timed_out is True
    assert result.exit_code != 0


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: HOME env differs on Windows",
)
async def test_sandbox_home_is_workspace(tmp_path: Path) -> None:
    """HOME 应被设为 workspace——隔离家目录访问"""
    sb = SandboxManager(workspace=tmp_path)
    result = await sb.execute("env")
    assert f"HOME={tmp_path}" in result.stdout


async def test_sandbox_custom_whitelist(tmp_path: Path) -> None:
    """白名单可定制"""
    sb = SandboxManager(
        workspace=tmp_path,
        allowed_command_prefixes=("custom_cmd",),
    )
    with pytest.raises(PermissionError):
        await sb.execute("echo hello")  # echo 不在白名单
    # custom_cmd 也不在系统——但白名单过，shell 会失败
    # 沙箱应返回非 0 退出码而非抛异常
    result = await sb.execute("custom_cmd arg")
    assert result.exit_code != 0  # shell: command not found (127)


async def test_sandbox_is_allowed_handles_whitespace(tmp_path: Path) -> None:
    """tab/space 分隔的命令都应匹配白名单"""
    sb = SandboxManager(
        workspace=tmp_path,
        allowed_command_prefixes=("ls",),
    )
    assert sb._is_allowed("ls -la")
    assert sb._is_allowed("ls\t-la")
    assert sb._is_allowed("ls")
    assert not sb._is_allowed("rm file")
    # 前缀相似但不等于——不应误中
    assert not sb._is_allowed("lsblk")
    assert not sb._is_allowed("lsof")


# ---------------------------------------------------------------------------
# F-03 / F-04 / F-05 / P1-9 攻击套件
# ---------------------------------------------------------------------------


async def test_sandbox_blocks_shell_metachar_semicolon(tmp_path: Path) -> None:
    """F-03: ; 应被拒绝——白名单首词 ls 命中, 但 ; 注入 cat"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="metachar"):
        await sb.execute("ls;cat /etc/passwd")


async def test_sandbox_blocks_shell_metachar_pipe(tmp_path: Path) -> None:
    """F-03: pipe 也应被拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="metachar"):
        await sb.execute("ls|cat")


async def test_sandbox_blocks_shell_metachar_redirect(tmp_path: Path) -> None:
    """F-03: 重定向符 > 也应被拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="metachar"):
        await sb.execute("echo x>passwd")


async def test_sandbox_blocks_absolute_path_escape(tmp_path: Path) -> None:
    """F-04: 绝对路径 /etc/passwd 应被 workspace 验证拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="path escape workspace"):
        await sb.execute("cat /etc/passwd")


async def test_sandbox_blocks_parent_dir_escape(tmp_path: Path) -> None:
    """F-04: ../ 跳出 workspace 应被拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="path escape workspace"):
        await sb.execute("cat ../../../etc/passwd")


async def test_sandbox_blocks_symlink_escape(tmp_path: Path) -> None:
    """F-04: workspace 内 symlink 指向 /etc/passwd 应被拒绝"""
    leak = tmp_path / "leak"
    try:
        leak.symlink_to("/etc/passwd")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported in this env")
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="path escape workspace"):
        await sb.execute("cat leak")


async def test_sandbox_blocks_python_command(tmp_path: Path) -> None:
    """F-03: 图灵完备命令 python 应被白名单拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="not in sandbox whitelist"):
        await sb.execute("python foo.py")


async def test_sandbox_blocks_find_command(tmp_path: Path) -> None:
    """F-03: 全盘搜索 find 应被白名单拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="not in sandbox whitelist"):
        await sb.execute("find . -name x")


async def test_sandbox_blocks_awk_command(tmp_path: Path) -> None:
    """F-03: awk 应被白名单拒绝"""
    sb = SandboxManager(workspace=tmp_path)
    with pytest.raises(PermissionError, match="not in sandbox whitelist"):
        await sb.execute("awk foo")


async def test_sandbox_workspace_strict_resolve(tmp_path: Path) -> None:
    """F-05: workspace 不存在应 ValueError——构造时 strict resolve"""
    with pytest.raises(ValueError, match="workspace invalid"):
        SandboxManager(workspace=tmp_path / "nope")


@pytest.mark.skipif(
    sys.platform == "win32", reason="P3-WIN: subprocess pipe double-communicate differs on Windows",
)
async def test_sandbox_timeout_does_not_double_communicate(tmp_path: Path) -> None:
    """P1-9: 修 EBADF 确定性 bug——kill 后只 wait() 不 communicate"""
    sb = SandboxManager(workspace=tmp_path)
    sb.allowed_command_prefixes = ("sleep",)
    result = await sb.execute("sleep 5", timeout=0.2)
    assert result.timed_out is True
    # 不应抛 OSError EBADF
