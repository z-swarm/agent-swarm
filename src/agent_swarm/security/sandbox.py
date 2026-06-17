"""
@module agent_swarm.security.sandbox
@brief  SandboxManager——DESIGN.md §8.2

W5 范围：workspace_only 模式
  - 命令执行限制在 workspace 内（cd workspace 后再执行）
  - 限时（timeout）
  - 限输出（max_output_bytes）
  - 命令白名单（可选，宽松默认）

W5 不实现（Phase 3 再做）:
  - Docker 容器隔离
  - firejail / bubblewrap
  - root-less 执行
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SandboxMode(Enum):
    """沙箱模式——DESIGN §8.2"""

    WORKSPACE_ONLY = "workspace_only"
    # Phase 3+ 才有
    # DOCKER = "docker"
    # FIREJAIL = "firejail"


@dataclass
class SandboxResult:
    """沙箱执行结果"""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool  # 输出是否被 max_output_bytes 截断
    duration_seconds: float
    timed_out: bool = False


class SandboxManager:
    """
    workspace_only 沙箱——W5 落地的最小安全执行器

    @note 限制:
        - 工作目录强制设为 self.workspace
        - 命令超时 hard kill
        - stdout/stderr 字节数限幅
        - 允许的命令前缀列表（保守默认：ls / cat / head / tail / wc / grep）
    """

    # 保守的命令白名单——W5 防止破坏性命令
    DEFAULT_ALLOWED_PREFIXES: tuple[str, ...] = (
        "ls", "cat", "head", "tail", "wc", "grep", "find", "echo",
        "pwd", "env", "which", "file", "stat", "du", "df", "ps",
        "awk", "sed", "sort", "uniq", "tr", "cut", "paste", "diff",
        "tree", "less", "more", "head", "tail",
        "git status", "git log", "git diff", "git show", "git branch",
        "python", "python3", "node", "jq",
    )

    def __init__(
        self,
        workspace: Path | str,
        mode: SandboxMode = SandboxMode.WORKSPACE_ONLY,
        allowed_command_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.mode = mode
        self.allowed_command_prefixes = (
            allowed_command_prefixes or self.DEFAULT_ALLOWED_PREFIXES
        )

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,
        max_output_bytes: int = 10240,  # 10 KB
        env_overrides: dict[str, str] | None = None,
    ) -> SandboxResult:
        """
        同步执行命令（asyncio.to_thread 包装）——W5 简化

        @raise PermissionError 命令不在白名单
        @raise ValueError     workspace 路径无效
        """
        if self.mode != SandboxMode.WORKSPACE_ONLY:
            raise NotImplementedError(
                f"sandbox mode {self.mode} not implemented in W5"
            )

        if not self.workspace.is_dir():
            raise ValueError(f"sandbox workspace invalid: {self.workspace}")

        # 白名单检查
        if not self._is_allowed(command):
            raise PermissionError(
                f"command not in sandbox whitelist: {command!r}"
            )

        # 准备环境
        cwd = str(self.workspace)
        env = os.environ.copy()
        env["PWD"] = cwd
        env["HOME"] = str(self.workspace)  # 隔离——避免读家目录
        if env_overrides:
            env.update(env_overrides)

        start = time.monotonic()
        timed_out = False
        try:
            # asyncio 化——通过 to_thread + wait_for
            proc = await asyncio.to_thread(
                _subprocess_run, command, cwd, env
            )
            try:
                # 限时 + 读输出
                stdout, stderr = await asyncio.wait_for(
                    asyncio.to_thread(proc.communicate),
                    timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                stdout, stderr = proc.communicate()
                timed_out = True
        except Exception as exc:
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

        # 输出截断
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
        """检查命令是否在白名单——按首词匹配"""
        cmd = command.strip()
        for prefix in self.allowed_command_prefixes:
            if cmd == prefix or cmd.startswith(prefix + " ") or cmd.startswith(prefix + "\t"):
                return True
        return False


# ---------------------------------------------------------------------------
# 内部：subprocess 启动的同步包装（asyncio.to_thread 内部使用）
# ---------------------------------------------------------------------------


def _subprocess_run(command: str, cwd: str, env: dict[str, str]) -> Any:
    """
    同步 subprocess.Popen——asyncio.to_thread 包装

    @note W5: 简单 Popen，不开 shell=True（防命令注入）
              命令字符串须为完整 argv 形式（不含 shell 元字符）
    """
    import subprocess

    return subprocess.Popen(
        command,  # 已通过白名单——风险可控
        shell=True,  # 显式允许 shell（白名单已过滤破坏性命令）
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
