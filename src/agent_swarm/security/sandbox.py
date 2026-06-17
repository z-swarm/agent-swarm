"""
@module agent_swarm.security.sandbox
@brief  SandboxManager——DESIGN.md §8.2

W5 范围（重写后）: workspace_only 模式 + 真实防护
  - shell=False + shlex.split（防命令注入 F-03）
  - 所有路径 token 走 realpath 验证必须在 workspace 内（防 symlink 逃逸 F-04）
  - workspace 在 __init__ resolve(strict=True) 锁定（防 TOCTOU F-05）
  - 禁止任何 shell 元字符 ; & | ` $ < > ( ) { } \\ ! # * ? [ ] ' "（防 pipeline bypass）
  - 限时（timeout）+ 杀进程后不再 communicate（修 EBADF bug P1-9）
  - 限输出（max_output_bytes）

  拒绝图灵完备 / 全盘搜索类命令 (python/python3/awk/sed/find/node/jq/perl/ruby)——
  这些命令本身能跳出白名单

W5 不实现（Phase 3 再做）:
  - Docker 容器隔离
  - firejail / bubblewrap
  - chroot / namespace
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class SandboxMode(Enum):
    """@brief 沙箱模式——DESIGN §8.2"""

    WORKSPACE_ONLY = "workspace_only"
    # Phase 3+ 才有
    # DOCKER = "docker"
    # FIREJAIL = "firejail"


@dataclass
class SandboxResult:
    """@brief 沙箱执行结果"""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_seconds: float
    timed_out: bool = False


# shell 元字符 + 重定向 + 变量展开 + glob——出现即拒绝
_SHELL_META_CHARS = frozenset(";&|`$<>\\!#*?[]{}()'\"")


class SandboxManager:
    """
    @brief workspace_only 沙箱——W5 落地的最小安全执行器

    防护层级（4 道防线）:
      1) 命令白名单（argv[0] 精确匹配）
      2) shell 元字符检测（防注入）
      3) shlex.split 拆 argv, shell=False
      4) 每 token 路径 realpath 验证在 workspace 内（防 symlink + 绝对路径逃逸）
    """

    # 保守只读型命令白名单——不包含任何能写文件/执行代码/全盘搜索的命令
    DEFAULT_ALLOWED_PREFIXES: tuple[str, ...] = (
        "ls", "cat", "head", "tail", "wc", "grep",
        "echo", "pwd", "env", "which", "file", "stat",
        "du", "df", "ps", "sort", "uniq", "tr",
        "cut", "paste", "diff", "tree", "less", "more",
        "date", "uname", "whoami", "id", "true", "false",
        "git status", "git log", "git diff", "git show", "git branch",
    )

    def __init__(
        self,
        workspace: Path | str,
        mode: SandboxMode = SandboxMode.WORKSPACE_ONLY,
        allowed_command_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        """
        @param workspace  沙箱根目录——构造时 resolve 锁定
        @param mode       当前只支持 WORKSPACE_ONLY
        @param allowed_command_prefixes  None 时用默认白名单

        @raise ValueError workspace 不存在或不是目录
        """
        try:
            self.workspace = Path(workspace).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise ValueError(f"sandbox workspace invalid: {workspace}") from exc
        if not self.workspace.is_dir():
            raise ValueError(f"sandbox workspace invalid: {self.workspace}")
        self.mode = mode
        self.allowed_command_prefixes = (
            allowed_command_prefixes or self.DEFAULT_ALLOWED_PREFIXES
        )

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,
        max_output_bytes: int = 10240,
        env_overrides: dict[str, str] | None = None,
    ) -> SandboxResult:
        """
        @brief 同步执行命令（asyncio.to_thread 包装）——W5 简化

        @raise PermissionError 命令不在白名单 / 含 shell 元字符 / 路径逃逸 workspace
        @raise ValueError     workspace 路径无效
        """
        if self.mode != SandboxMode.WORKSPACE_ONLY:
            raise NotImplementedError(
                f"sandbox mode {self.mode} not implemented in W5"
            )

        # 1) shell 元字符检测（在白名单之前——避免 metachar 拆解绕过）
        for ch in _SHELL_META_CHARS:
            if ch in command:
                raise PermissionError(
                    f"shell metachar {ch!r} in command: {command!r}"
                )

        # 2) shlex 拆 argv
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise PermissionError(f"command parse failed: {exc}") from exc
        if not argv:
            raise PermissionError(f"empty command: {command!r}")

        # 3) argv[0] 精确匹配白名单
        if not self._is_allowed_argv(argv):
            raise PermissionError(
                f"command not in sandbox whitelist: {command!r}"
            )

        # 4) 每 token 路径验证（含裸文件名——防 symlink 逃逸）
        for tok in argv[1:]:
            self._assert_path_in_workspace(tok)

        # 准备环境
        cwd = str(self.workspace)
        env = os.environ.copy()
        env["PWD"] = cwd
        env["HOME"] = str(self.workspace)
        if env_overrides:
            env.update(env_overrides)

        start = time.monotonic()
        timed_out = False
        proc: subprocess.Popen | None = None
        try:
            # 5) shell=False + argv 列表——命令注入面闭合
            proc = await asyncio.to_thread(_popen_no_shell, argv, cwd, env)
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.to_thread(proc.communicate),
                    timeout=timeout,
                )
            except TimeoutError:
                # EBADF 修复: kill 后用 wait() 收尸, 不再 communicate（fd 已关闭）
                proc.kill()
                await asyncio.to_thread(proc.wait)
                stdout, stderr = b"", b""
                timed_out = True
        except FileNotFoundError as exc:
            return SandboxResult(
                exit_code=127,
                stdout="",
                stderr=f"command not found: {exc}",
                truncated=False,
                duration_seconds=time.monotonic() - start,
                timed_out=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("sandbox.execute failed: %s", exc)
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"sandbox error: {exc}",
                truncated=False,
                duration_seconds=time.monotonic() - start,
                timed_out=False,
            )

        duration = time.monotonic() - start
        exit_code = proc.returncode if proc.returncode is not None else -1

        stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
        stdout_truncated = False
        stderr_truncated = False
        if len(stdout_str) > max_output_bytes:
            stdout_str = stdout_str[:max_output_bytes] + "\n[truncated]"
            stdout_truncated = True
        if len(stderr_str) > max_output_bytes:
            stderr_str = stderr_str[:max_output_bytes] + "\n[truncated]"
            stderr_truncated = True

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=duration,
            timed_out=timed_out,
        )

    def _is_allowed(self, command: str) -> bool:
        """@brief 旧接口兼容——首词匹配"""
        cmd = command.strip()
        for prefix in self.allowed_command_prefixes:
            if cmd == prefix or cmd.startswith(prefix + " ") or cmd.startswith(prefix + "\t"):
                return True
        return False

    def _is_allowed_argv(self, argv: list[str]) -> bool:
        """@brief 按 argv[0] 精确匹配白名单——shlex.split 后调用, 无 metachar 干扰"""
        if not argv:
            return False
        cmd0 = argv[0]
        return any(
            cmd0 == prefix or cmd0.startswith(prefix + " ")
            for prefix in self.allowed_command_prefixes
        )

    def _assert_path_in_workspace(self, token: str) -> None:
        """
        @brief 验证 token 路径解析后在 workspace 内

        纯 flag 跳过; 其他 token 一律 resolve 后必须落在 workspace 内——
        包含裸文件名也强制 resolve, 防 workspace 内 symlink 逃逸
        （如 `leak -> /etc/passwd` + `cat leak` 必须拦截）
        """
        if token.startswith("-"):
            return
        try:
            if os.path.isabs(token):
                candidate = Path(token).resolve()
            else:
                candidate = (self.workspace / token).resolve()
        except (OSError, RuntimeError):
            raise PermissionError(f"path resolve failed: {token!r}")
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            raise PermissionError(
                f"path escape workspace: {token!r} -> {candidate} not under {self.workspace}"
            )


# ---------------------------------------------------------------------------
# 内部：subprocess 启动的同步包装
# ---------------------------------------------------------------------------


def _popen_no_shell(argv: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
    """@brief 同步 subprocess.Popen + shell=False——注入面闭合"""
    return subprocess.Popen(
        argv,
        shell=False,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
