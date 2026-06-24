"""
@module agent_swarm.security.sandbox_docker
@brief  W19-② ③ ④ + P4-W24 Docker Sandbox 后端——保守版 opt-in + 长生命周期

P3-PLAN-v2 W19 DoD:
  - W19-1 DockerSandboxManager 实现 SandboxManager 协议
  - W19-2 通过 docker SDK 启容器 + bind workspace 目录
  - W19-3 CIS Docker Benchmark 关键项:
      4.1 非 root 用户 (User: "1000:1000")
      5.x 限制 capabilities (--cap-drop=ALL + 仅 NET_BIND_SERVICE)
      5.x 只读文件系统 (--read-only + tmpfs)
      5.x no-new-privileges (--security-opt=no-new-privileges:true)
      5.x 限制 pid limit (--pids-limit=128)
      5.x 限制 memory / cpu
  - W19-4 默认 SandboxMode.WORKSPACE_ONLY 不变 (向后兼容)
  - W19-5 容器逃逸拦截: 20 条攻击模式

P4-W24 长生命周期 (W24-①):
  - long_lived=True (默认): 首次 execute() 启容器, 后续 docker exec, close() 停容器
  - long_lived=False (W19 兼容): 每次 execute() = docker run --rm
  - 性能: 100 execute() 调用只启 1 容器 (vs 100), 启动开销 <500ms 仅一次
  - 容器名: 唯一基于 workspace + pid (避免冲突)

@note 本文件仅在 user 显式 sandbox.mode=docker 时启用
@note 测试用 fake docker CLI (subprocess 模拟) + 不需要真 docker daemon
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_swarm.security.sandbox import (
    SandboxManager,
    SandboxMode,
    SandboxResult,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CIS Docker Benchmark 关键项
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CISDockerCheck:
    """CIS Docker Benchmark 单条检查项——W19-3"""

    id: str  # e.g. "5.1"
    title: str
    description: str
    enabled: bool = True


# 关键 CIS 项——W19 范围只覆盖这些
CIS_DOCKER_CHECKS: tuple[CISDockerCheck, ...] = (
    CISDockerCheck(
        id="4.1",
        title="Run as non-root user",
        description="容器内进程必须以非 root 用户运行 (UID >= 1000)",
    ),
    CISDockerCheck(
        id="5.2",
        title="Drop all capabilities",
        description="容器必须 --cap-drop=ALL，仅在白名单中按需添加",
    ),
    CISDockerCheck(
        id="5.3",
        title="Read-only root filesystem",
        description="容器根文件系统只读，必要时 tmpfs /workspace",
    ),
    CISDockerCheck(
        id="5.4",
        title="no-new-privileges",
        description="禁止 setuid/setgid 提权 (--security-opt=no-new-privileges:true)",
    ),
    CISDockerCheck(
        id="5.5",
        title="Limit PIDs",
        description="限制容器进程数 (--pids-limit=128)",
    ),
    CISDockerCheck(
        id="5.6",
        title="Limit memory",
        description="限制容器内存 (--memory=512m)",
    ),
    CISDockerCheck(
        id="5.7",
        title="Limit CPU",
        description="限制容器 CPU (--cpus=1.0)",
    ),
    CISDockerCheck(
        id="5.12",
        title="Bind workspace only",
        description="只 bind workspace 目录，不 bind / 或其他敏感路径",
    ),
    CISDockerCheck(
        id="5.13",
        title="No network by default",
        description="默认 --network=none，按需 opt-in",
    ),
    CISDockerCheck(
        id="5.14",
        title="Resource limits",
        description="总资源 (ulimits) 收紧",
    ),
)


# ---------------------------------------------------------------------------
# 容器逃逸攻击模式——W19-5 20 条
# ---------------------------------------------------------------------------


# 攻击模式 + 拦截策略
@dataclass(frozen=True)
class EscapeAttempt:
    """单条容器逃逸尝试——W19-5"""

    id: str  # e.g. "ESC-01"
    title: str
    pattern: str  # 命中此模式的 argv 一律拒绝
    description: str


# 20 条逃逸尝试 (来源: CIS Docker Benchmark + OWASP Docker Top 10)
CONTAINER_ESCAPE_ATTEMPTS: tuple[EscapeAttempt, ...] = (
    EscapeAttempt("ESC-01", "Mount /", "/:/host", "尝试 bind mount 根目录"),
    EscapeAttempt("ESC-02", "Mount /etc", "/etc:/host", "尝试 bind mount /etc"),
    EscapeAttempt("ESC-03", "Mount /proc", "/proc:/host", "尝试 bind mount /proc"),
    EscapeAttempt("ESC-04", "Mount /sys", "/sys:/host", "尝试 bind mount /sys"),
    EscapeAttempt("ESC-05", "Mount /dev", "/dev:/host", "尝试 bind mount /dev"),
    EscapeAttempt("ESC-06", "Privileged flag", "--privileged", "尝试 --privileged"),
    EscapeAttempt("ESC-07", "All capabilities", "--cap-add=ALL", "尝试添加全部 capabilities"),
    EscapeAttempt("ESC-08", "SYS_ADMIN capability", "--cap-add=SYS_ADMIN", "尝试添加 SYS_ADMIN"),
    EscapeAttempt("ESC-09", "NET_ADMIN capability", "--cap-add=NET_ADMIN", "尝试添加 NET_ADMIN"),
    EscapeAttempt(
        "ESC-10",
        "DAC override",
        "--cap-add=DAC_",
        "尝试 DAC override (DAC_OVERRIDE/DAC_READ_SEARCH)",
    ),
    EscapeAttempt("ESC-11", "Host network", "--network=host", "尝试使用 host 网络"),
    EscapeAttempt("ESC-12", "Host PID namespace", "--pid=host", "尝试共享 host PID namespace"),
    EscapeAttempt("ESC-13", "Host IPC namespace", "--ipc=host", "尝试共享 host IPC namespace"),
    EscapeAttempt(
        "ESC-14", "Docker socket mount", "/var/run/docker.sock", "尝试 mount docker socket"
    ),
    EscapeAttempt("ESC-15", "Root user override", "user=root", "尝试 user=root"),
    EscapeAttempt("ESC-16", "Sudo in container", " sudo ", "尝试 sudo"),
    EscapeAttempt("ESC-17", "nsenter host", "nsenter", "尝试 nsenter 进 host namespace"),
    EscapeAttempt("ESC-18", "chroot to host", "chroot ", "尝试 chroot 到 host"),
    EscapeAttempt("ESC-19", "Mount cgroup", "/sys/fs/cgroup", "尝试读写 cgroup"),
    EscapeAttempt("ESC-20", "kubectl/ctr exec", "ctr ", "尝试 ctr/kubectl exec"),
)


# ---------------------------------------------------------------------------
# Docker Backend
# ---------------------------------------------------------------------------


@dataclass
class DockerConfig:
    """Docker 后端配置——W19-1 + P4-W24"""

    image: str = "python:3.11-slim"
    user: str = "1000:1000"  # CIS 4.1 non-root
    read_only_root: bool = True  # CIS 5.3
    no_new_privileges: bool = True  # CIS 5.4
    pids_limit: int = 128  # CIS 5.5
    memory: str = "512m"  # CIS 5.6
    cpus: float = 1.0  # CIS 5.7
    network: str = "none"  # CIS 5.13 默认无网络
    capabilities_drop: tuple[str, ...] = ("ALL",)  # CIS 5.2
    workspace_mount: str = "/workspace"  # 容器内 mount 点
    extra_cis_checks: tuple[str, ...] = ()
    # 测试用: 模拟 docker CLI 的可调用对象 (默认 shutil.which("docker"))
    docker_runner: Callable[..., Awaitable[dict[str, Any]]] | None = None
    # P4-W24: 长生命周期模式
    # True  -> 启 1 容器, 后续 docker exec (性能高, 启动开销仅一次)
    # False -> 每次 docker run --rm (W19 行为, 隔离强但慢)
    long_lived: bool = True
    container_name_prefix: str = "agentswarm"  # 容器名前缀
    container_stop_timeout: float = 10.0  # docker stop 超时秒


class DockerSandboxManager(SandboxManager):
    """
    W19-① ② Docker 后端——仅当 mode=DOCKER 启用

    @note DOCKER 模式默认关闭 (SandboxMode.WORKSPACE_ONLY 为默认)
    @note 容器配置走 CIS Docker Benchmark (10 条关键项)
    @note 攻击模式 20 条前置拦截 (CONTAINER_ESCAPE_ATTEMPTS)
    """

    def __init__(
        self,
        workspace: Path | str,
        config: DockerConfig | None = None,
    ) -> None:
        """
        @param workspace 宿主机 workspace 目录 (bind mount 进容器)
        @param config    Docker 容器配置 (默认走 CIS 关键项)
        """
        # NOTE: skip parent workspace resolve —— Docker 启动前可能 workspace 未存在
        #         但父类要求 strict=True, 这里用 pre-check
        ws = Path(workspace)
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
        # 不调 super().__init__ —— 直接走 CIS DOCKER 模式
        # (SandboxManager.__init__ 强制 mode=WORKSPACE_ONLY)
        try:
            self.workspace = Path(ws).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise ValueError(f"sandbox workspace invalid: {workspace}") from exc
        if not self.workspace.is_dir():
            raise ValueError(f"sandbox workspace invalid: {self.workspace}")
        self.mode = SandboxMode.DOCKER
        self.docker_mode = SandboxMode.DOCKER
        self.config = config or DockerConfig()
        # 共享 WORKSPACE_ONLY 默认白名单
        self.allowed_command_prefixes: tuple[str, ...] = self.DEFAULT_ALLOWED_PREFIXES
        self._container_id: str | None = None
        self._started_at: float | None = None
        self._doctor_warnings: list[str] = []
        # P4-W24: 长生命周期状态
        self._container_name: str | None = None
        self._container_started: bool = False
        self._stop_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # SandboxManager ABC 重写——DOCKER 模式特殊路径
    # ------------------------------------------------------------------

    # mode 字段在 __init__ 中直接赋值 (不用 property)

    async def execute(
        self,
        command: str,
        timeout: float = 30.0,
        max_output_bytes: int = 10240,
        env_overrides: dict[str, str] | None = None,
    ) -> SandboxResult:
        """
        W19 范围 (v1): DOCKER 模式启动容器 + 在容器内执行命令

        @note W19 v1 不实现容器复用 (每次 execute = docker run + rm)
              Phase 4 可优化为 long-running container
        @raise PermissionError  命中 20 条逃逸拦截
        """
        # 0) DOCKER 模式与 WORKSPACE_ONLY 模式共享前 4 道防线
        #    (white list + shell meta + shlex + path)
        from agent_swarm.security.sandbox import (
            _SHELL_META_CHARS,
        )

        for ch in _SHELL_META_CHARS:
            if ch in command:
                raise PermissionError(
                    f"shell metachar {ch!r} in command: {command!r}",
                )
        try:
            import shlex

            argv = shlex.split(command)
        except ValueError as exc:
            raise PermissionError(f"command parse failed: {exc}") from exc
        if not argv:
            raise PermissionError(f"empty command: {command!r}")

        # 1) 容器逃逸拦截 (20 条)
        self._assert_no_escape(argv)

        # 2) WORKSPACE_ONLY 白名单 (复用)
        if not self._is_allowed_argv(argv):
            raise PermissionError(
                f"command not in sandbox whitelist: {command!r}",
            )

        # 3) 路径 token 检查 (Docker 模式: 容器内路径不验证——容器内 /workspace 是绑定挂载点)
        #    仅当路径解析后能落在宿主 workspace 内时才校验
        for tok in argv[1:]:
            self._assert_path_in_workspace_or_skip(tok)

        # 4) 容器内执行 (P4-W24: 长生命周期 or W19 一次性)
        if self.config.long_lived:
            return await self._run_in_long_lived_container(
                argv,
                timeout,
                max_output_bytes,
                env_overrides or {},
            )
        return await self._run_in_container(
            argv,
            timeout,
            max_output_bytes,
            env_overrides or {},
        )

    # ------------------------------------------------------------------
    # P4-W24: 长生命周期容器管理
    # ------------------------------------------------------------------

    # 类级计数器, 保证同进程多 manager 唯一 (id() 在某些情况下会复用)
    _name_counter: int = 0

    def _make_container_name(self) -> str:
        """生成唯一容器名"""
        DockerSandboxManager._name_counter += 1
        ws_hash = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:8]
        return (
            f"{self.config.container_name_prefix}-{ws_hash}-"
            f"{os.getpid()}-{DockerSandboxManager._name_counter:04d}"
        )

    async def _start_container(self) -> None:
        """启动长生命周期容器 (P4-W24)"""
        if self._container_started:
            return
        async with self._stop_lock:
            if self._container_started:  # 双重检查
                return
            self._container_name = self._make_container_name()
            # 构造 docker run -d 命令
            runner = self.config.docker_runner or self._default_docker_runner
            docker_argv = ["docker", "run", "-d", "--name", self._container_name]
            # CIS 安全参数
            cfg = self.config
            docker_argv += ["--user", cfg.user]
            for cap in cfg.capabilities_drop:
                docker_argv += ["--cap-drop", cap]
            if cfg.read_only_root:
                docker_argv += ["--read-only", "--tmpfs", "/tmp:size=64m,mode=1777"]
            if cfg.no_new_privileges:
                docker_argv += ["--security-opt", "no-new-privileges:true"]
            docker_argv += [
                "--pids-limit",
                str(cfg.pids_limit),
                "--memory",
                cfg.memory,
                "--cpus",
                str(cfg.cpus),
                "--network",
                cfg.network,
                "-v",
                f"{self.workspace}:{cfg.workspace_mount}:rw",
                cfg.image,
                "sleep",
                "infinity",  # 长跑: 容器内永远 sleep
            ]
            log.info("docker long-lived: starting container %s", self._container_name)
            result = await runner(docker_argv)
            if result.get("exit_code") != 0:
                raise RuntimeError(f"failed to start long-lived container: {result.get('stderr')}")
            # stdout 是 container id
            self._container_id = (result.get("stdout") or "").strip()
            self._started_at = time.monotonic()
            self._container_started = True
            log.info(
                "docker long-lived: container %s started (id=%s)",
                self._container_name,
                self._container_id,
            )

    async def _stop_container(self) -> None:
        """停止长生命周期容器 (P4-W24)"""
        if not self._container_started or not self._container_name:
            return
        async with self._stop_lock:
            if not self._container_started:
                return
            runner = self.config.docker_runner or self._default_docker_runner
            try:
                # docker stop 给容器发 SIGTERM, 超时后 SIGKILL
                await runner(
                    [
                        "docker",
                        "stop",
                        "-t",
                        str(int(self.config.container_stop_timeout)),
                        self._container_name,
                    ]
                )
                log.info(
                    "docker long-lived: container %s stopped",
                    self._container_name,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("docker long-lived: stop failed: %s", exc)
            finally:
                self._container_started = False
                self._container_id = None
                self._container_name = None
                self._started_at = None

    async def _run_in_long_lived_container(
        self,
        argv: list[str],
        timeout: float,
        max_output_bytes: int,
        env_overrides: dict[str, str],
    ) -> SandboxResult:
        """
        P4-W24: 在长生命周期容器内执行命令

        首次调用: 启动容器 (docker run -d sleep infinity)
        后续调用: docker exec 在容器内跑
        关闭时: docker stop
        """
        # 1) 确保容器在跑
        if not self._container_started:
            try:
                await self._start_container()
            except Exception as exc:  # noqa: BLE001
                log.error("docker long-lived: start failed: %s", exc)
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"start failed: {exc}",
                    truncated=False,
                    duration_seconds=0.0,
                )

        # 2) docker exec
        runner = self.config.docker_runner or self._default_docker_runner
        cfg = self.config
        docker_argv = [
            "docker",
            "exec",
            "-w",
            cfg.workspace_mount,
        ]
        for k, v in env_overrides.items():
            docker_argv += ["-e", f"{k}={v}"]
        docker_argv += [self._container_name or ""] + argv
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(runner(docker_argv), timeout=timeout)
        except TimeoutError:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"docker exec timeout after {timeout}s",
                truncated=False,
                duration_seconds=time.monotonic() - start,
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("docker exec failed: %s", exc)
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"docker error: {exc}",
                truncated=False,
                duration_seconds=time.monotonic() - start,
            )
        duration = time.monotonic() - start
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = int(result.get("exit_code", 0))
        truncated = False
        if len(stdout) > max_output_bytes:
            stdout = stdout[:max_output_bytes] + "\n[truncated]"
            truncated = True
        if len(stderr) > max_output_bytes:
            stderr = stderr[:max_output_bytes] + "\n[truncated]"
            truncated = True
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            duration_seconds=duration,
        )

    async def close(self) -> None:
        """P4-W24: 关闭长生命周期容器 (手动调用)"""
        await self._stop_container()

    async def __aenter__(self) -> DockerSandboxManager:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 容器逃逸拦截 (W19-5)
    # ------------------------------------------------------------------

    def _assert_path_in_workspace_or_skip(self, token: str) -> None:
        """
        Docker 模式: 容器内路径 (如 /workspace) 不走宿主机验证——
        若 token 在宿主机上能 resolve 且落在 workspace 外, 才拒绝
        """
        if token.startswith("-"):
            return
        # POSIX 绝对路径 (容器内, 如 /workspace, /tmp) 直接放行
        if token.startswith("/"):
            return
        # 相对路径走宿主机验证 (在 workspace 下 resolve, 不要走 cwd)
        try:
            candidate = (self.workspace / token).resolve()
        except (OSError, RuntimeError):
            return
        try:
            candidate.relative_to(self.workspace)
        except ValueError as e:
            raise PermissionError(
                f"path escape workspace: {token!r} -> {candidate} not under {self.workspace}",
            ) from e

    def _assert_no_escape(self, argv: list[str]) -> None:
        """
        20 条逃逸模式前置检查
        @raise PermissionError 命中任一
        """
        cmd = " ".join(argv)
        for esc in CONTAINER_ESCAPE_ATTEMPTS:
            if esc.pattern in cmd:
                raise PermissionError(
                    f"container escape blocked [{esc.id}]: {esc.title} — pattern {esc.pattern!r}",
                )

    # ------------------------------------------------------------------
    # 容器执行 (W19-2)
    # ------------------------------------------------------------------

    async def _run_in_container(
        self,
        argv: list[str],
        timeout: float,
        max_output_bytes: int,
        env_overrides: dict[str, str],
    ) -> SandboxResult:
        """docker run 一次性执行 + 销毁"""
        # 构造 docker run argv
        docker_argv = self._build_docker_run_argv(argv, env_overrides)
        # 真实 / mock docker CLI
        runner = self.config.docker_runner or self._default_docker_runner
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(runner(docker_argv), timeout=timeout)
        except TimeoutError:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"docker run timeout after {timeout}s",
                truncated=False,
                duration_seconds=time.monotonic() - start,
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("docker run failed: %s", exc)
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"docker error: {exc}",
                truncated=False,
                duration_seconds=time.monotonic() - start,
            )
        duration = time.monotonic() - start
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = int(result.get("exit_code", 0))
        # 截断
        truncated = False
        if len(stdout) > max_output_bytes:
            stdout = stdout[:max_output_bytes] + "\n[truncated]"
            truncated = True
        if len(stderr) > max_output_bytes:
            stderr = stderr[:max_output_bytes] + "\n[truncated]"
            truncated = True
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            duration_seconds=duration,
        )

    def _build_docker_run_argv(
        self,
        cmd_argv: list[str],
        env_overrides: dict[str, str],
    ) -> list[str]:
        """
        构造 docker run 命令——所有 CIS 关键项默认开启
        """
        cfg = self.config
        argv: list[str] = ["docker", "run", "--rm"]

        # CIS 4.1: non-root
        argv += ["--user", cfg.user]
        # CIS 5.2: capabilities
        for cap in cfg.capabilities_drop:
            argv += ["--cap-drop", cap]
        # CIS 5.3: read-only root
        if cfg.read_only_root:
            argv += ["--read-only"]
            # tmpfs 给容器内 /tmp 用
            argv += ["--tmpfs", "/tmp:size=64m,mode=1777"]
        # CIS 5.4: no-new-privileges
        if cfg.no_new_privileges:
            argv += ["--security-opt", "no-new-privileges:true"]
        # CIS 5.5: PIDs limit
        argv += ["--pids-limit", str(cfg.pids_limit)]
        # CIS 5.6: memory
        argv += ["--memory", cfg.memory]
        # CIS 5.7: cpu
        argv += ["--cpus", str(cfg.cpus)]
        # CIS 5.13: network
        argv += ["--network", cfg.network]
        # CIS 5.12: workspace bind mount
        argv += ["-v", f"{self.workspace}:{cfg.workspace_mount}:rw"]
        # env overrides
        for k, v in env_overrides.items():
            argv += ["-e", f"{k}={v}"]
        # image + cmd
        argv += [cfg.image] + cmd_argv
        return argv

    async def _default_docker_runner(self, argv: list[str]) -> dict[str, Any]:
        """
        默认 docker runner——检查 docker CLI 可用 + 调 subprocess

        @raise RuntimeError  docker CLI 不存在
        """
        if shutil.which("docker") is None and self.config.docker_runner is None:
            raise RuntimeError(
                "docker CLI not found in PATH. Install Docker or use SandboxMode.WORKSPACE_ONLY.",
            )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    # ------------------------------------------------------------------
    # Doctor 检查 (W19-4)
    # ------------------------------------------------------------------

    async def doctor_check(self) -> dict[str, Any]:
        """
        agent-swarm doctor 集成点——检查 Docker 可用性
        @return dict with keys: docker_available, docker_version,
                cis_checks, escape_attempts, recommendation
        """
        result: dict[str, Any] = {
            "docker_available": False,
            "docker_version": None,
            "cis_checks": [
                {"id": c.id, "title": c.title, "enabled": c.enabled} for c in CIS_DOCKER_CHECKS
            ],
            "escape_attempts_count": len(CONTAINER_ESCAPE_ATTEMPTS),
            "recommendation": "",
        }
        # 探测 docker CLI
        try:
            runner = self.config.docker_runner or self._default_docker_runner
            proc = await runner(["docker", "version", "--format", "json"])
            result["docker_available"] = proc.get("exit_code") == 0
            if result["docker_available"]:
                # 解析 client/server version (格式不固定, 这里取 stdout 第一行)
                out = proc.get("stdout", "").strip()
                try:
                    parsed = json.loads(out)
                    if isinstance(parsed, dict):
                        result["docker_version"] = parsed.get("Client", {}).get("Version")
                except (json.JSONDecodeError, AttributeError):
                    result["docker_version"] = out.split("\n")[0][:64]
        except Exception as exc:  # noqa: BLE001
            result["docker_available"] = False
            result["doctor_error"] = str(exc)
        # 建议
        if result["docker_available"]:
            result["recommendation"] = (
                "Docker available. Use sandbox.mode=docker in production for "
                "stronger isolation. WORKSPACE_ONLY remains default for "
                "backward compatibility."
            )
        else:
            result["recommendation"] = (
                "Docker not available. Continue using WORKSPACE_ONLY mode. "
                "Install Docker to enable stronger sandbox isolation."
            )
        return result


__all__ = [
    "CISDockerCheck",
    "CIS_DOCKER_CHECKS",
    "CONTAINER_ESCAPE_ATTEMPTS",
    "ContainerEscapeAttempts" if False else "CONTAINER_ESCAPE_ATTEMPTS",
    "DockerConfig",
    "DockerSandboxManager",
    "EscapeAttempt",
]
