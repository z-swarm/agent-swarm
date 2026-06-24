"""
@module agent_swarm.cli.doctor
@brief  W14b agent-swarm doctor 子命令——4 类健康检查（DESIGN §17.7 DX 工具）

@note DESIGN §17.7 列的 doctor 用途：
  "agent-swarm doctor" | 检查 LLM provider 连通性、SQLite 锁、MCP server 状态、密钥就位

W14b 范围：
  - LLM provider 连通：openai / anthropic（HTTP HEAD 探活，不消耗 token）
  - SQLite 锁：检查默认 session db 是否可写、是否有锁
  - MCP server 状态：扫描 ~/.agent_swarm/mcp.yaml 或 --mcp-config 注册表，connect_all + health_check
  - 密钥就位：检查 OPENAI_API_KEY / ANTHROPIC_API_KEY / LARK_* 等环境变量

@note 退出码：
  - 0: 全部 OK
  - 1: 至少 1 项 WARN（可继续跑）
  - 2: 至少 1 项 FAIL（不可继续跑）
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import click
from rich.console import Console

console = Console()


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """单条 doctor 检查结果"""

    name: str
    status: CheckStatus
    message: str
    detail: str = ""

    def render(self) -> str:
        icon = {
            CheckStatus.OK: "[green]✓ OK[/]",
            CheckStatus.WARN: "[yellow]! WARN[/]",
            CheckStatus.FAIL: "[red]✗ FAIL[/]",
            CheckStatus.SKIP: "[dim]- SKIP[/]",
        }[self.status]
        return f"  {icon}  [bold]{self.name}[/]  {self.message}"


@dataclass
class DoctorReport:
    """完整 doctor 报告"""

    results: list[CheckResult] = field(default_factory=list)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)

    def has_failures(self) -> bool:
        return any(r.status == CheckStatus.FAIL for r in self.results)

    def has_warnings(self) -> bool:
        return any(r.status == CheckStatus.WARN for r in self.results)

    def exit_code(self) -> int:
        if self.has_failures():
            return 2
        if self.has_warnings():
            return 1
        return 0

    def render(self) -> None:
        console.rule("[bold cyan]agent-swarm doctor[/]")
        if not self.results:
            console.print("  [dim](no checks run)[/]")
            return
        for r in self.results:
            console.print(r.render())
            if r.detail:
                for line in r.detail.splitlines():
                    console.print(f"        [dim]{line}[/]")

        # 汇总
        counts = {s: 0 for s in CheckStatus}
        for r in self.results:
            counts[r.status] += 1
        summary = (
            f"  [bold]Summary:[/] "
            f"[green]{counts[CheckStatus.OK]} OK[/] · "
            f"[yellow]{counts[CheckStatus.WARN]} WARN[/] · "
            f"[red]{counts[CheckStatus.FAIL]} FAIL[/] · "
            f"[dim]{counts[CheckStatus.SKIP]} SKIP[/]"
        )
        console.print()
        console.print(summary)

        if self.has_failures():
            console.print("\n  [red bold]At least one FAIL — swarm may not start.[/]")
        elif self.has_warnings():
            console.print(
                "\n  [yellow]Warnings present — swarm can start but check items above.[/]"
            )
        else:
            console.print("\n  [green bold]All checks passed.[/]")


# ---------------------------------------------------------------------------
# LLM Provider 连通性
# ---------------------------------------------------------------------------


async def check_llm_provider(provider: str, env_var: str) -> CheckResult:
    """
    LLM provider 连通性检查——不消耗 token 的 HEAD 探活

    @param provider "openai" / "anthropic"
    @param env_var  对应环境变量名（OPENAI_API_KEY / ANTHROPIC_API_KEY）
    """
    api_key = os.environ.get(env_var)
    if not api_key:
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.WARN,
            message=f"{env_var} not set",
            detail="Set the env var or use --api-key to enable this provider.",
        )

    # 探活——不同 provider 不同 endpoint
    if provider == "openai":
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.SKIP,
            message=f"unknown provider {provider!r}",
        )

    try:
        import aiohttp
    except ImportError:
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.SKIP,
            message="aiohttp not installed",
            detail="(this is unexpected — aiohttp is a project dep)",
        )

    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as session,
            session.get(url, headers=headers) as resp,
        ):
            # openai: HEAD 不被支持，改 GET v1/models
            # anthropic: POST /v1/messages 必传 body；用 GET 探测（会被拒 405），
            # 401/403/405 都算"端点可达 + key 校验生效"
            if resp.status < 500:
                return CheckResult(
                    name=f"llm.{provider}",
                    status=CheckStatus.OK,
                    message=f"reachable (status={resp.status})",
                )
            return CheckResult(
                name=f"llm.{provider}",
                status=CheckStatus.FAIL,
                message=f"server error status={resp.status}",
            )
    except aiohttp.ClientError as exc:
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.FAIL,
            message=f"connection error: {exc}",
        )
    except TimeoutError:
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.FAIL,
            message="timeout (10s)",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=f"llm.{provider}",
            status=CheckStatus.FAIL,
            message=f"unexpected: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# SQLite 锁检查
# ---------------------------------------------------------------------------


def check_sqlite_lock(db_path: Path) -> CheckResult:
    """
    SQLite 锁检查——DESIGN §17.6 "SQLite WAL 在容器/NFS 不可靠"

    @note 简化实现：尝试 BEGIN IMMEDIATE + 立刻 ROLLBACK——
          任何"database is locked"或 IO 错误即视为锁问题
    """
    if str(db_path) == ":memory:":
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.OK,
            message="in-memory db (no lock check)",
        )

    db_path = db_path.expanduser()
    if not db_path.exists():
        # 不存在 OK——Swarm 第一次跑会创建
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.OK,
            message=f"db not yet created at {db_path} (will be created on first run)",
        )

    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.OK,
            message=f"writable at {db_path}",
        )
    except sqlite3.OperationalError as exc:
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.FAIL,
            message=f"db locked: {exc}",
            detail=(
                "Possible causes:\n"
                "  - Another agent-swarm process holds the db\n"
                "  - SQLite WAL on NFS/network FS is unreliable (§17.6)\n"
                "  - Use Redis backend (Phase 3 W18) for multi-process safety"
            ),
        )
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.FAIL,
            message=f"db error: {exc}",
        )
    except OSError as exc:
        return CheckResult(
            name="sqlite.lock",
            status=CheckStatus.FAIL,
            message=f"IO error: {exc}",
            detail="Check parent directory exists and is writable",
        )


# ---------------------------------------------------------------------------
# MCP server 状态
# ---------------------------------------------------------------------------


async def check_mcp_servers(config_path: Path | None) -> CheckResult:
    """
    MCP server 健康检查——DESIGN §7.3 连接监控

    @param config_path  YAML config 路径；None 时跳过（不报错）
    @note W14b 简化：单条 CheckResult 含每个 server 状态
    """
    if config_path is None:
        return CheckResult(
            name="mcp.servers",
            status=CheckStatus.SKIP,
            message="no --mcp-config specified",
            detail="Pass a YAML path to check MCP server reachability.",
        )
    if not config_path.exists():
        return CheckResult(
            name="mcp.servers",
            status=CheckStatus.FAIL,
            message=f"config not found: {config_path}",
        )

    # 延迟 import 避免在 doctor 不查 MCP 时强制依赖 mcp 模块
    from agent_swarm.mcp import MCPRegistry

    try:
        registry = MCPRegistry.from_yaml(str(config_path))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="mcp.servers",
            status=CheckStatus.FAIL,
            message=f"failed to load config: {exc}",
        )

    if not registry.list_names():
        return CheckResult(
            name="mcp.servers",
            status=CheckStatus.OK,
            message="no MCP servers registered",
        )

    connect_results = await registry.connect_all()
    try:
        statuses = await registry.health_check_all()
    finally:
        await registry.disconnect_all()

    lines: list[str] = []
    has_fail = False
    for s in statuses:
        if s.connected:
            lines.append(f"    [green]✓[/] {s.name}: connected")
        else:
            has_fail = True
            lines.append(
                f"    [red]✗[/] {s.name}: connect_failed={not connect_results.get(s.name, True)} "
                f"circuit={s.circuit_state}"
            )

    # 总结
    failed = [s.name for s in statuses if not s.connected]
    if has_fail:
        return CheckResult(
            name="mcp.servers",
            status=CheckStatus.FAIL,
            message=f"{len(failed)}/{len(statuses)} servers unreachable",
            detail="\n".join(lines),
        )
    return CheckResult(
        name="mcp.servers",
        status=CheckStatus.OK,
        message=f"{len(statuses)} servers reachable",
        detail="\n".join(lines) if lines else "",
    )


# ---------------------------------------------------------------------------
# 密钥就位
# ---------------------------------------------------------------------------


def check_secrets() -> CheckResult:
    """
    密钥就位检查——DESIGN §17.6 + §7.3

    @note 不做实际 API 调用（LLM provider check 负责）；这里只看 env var 是否就位
    """
    candidates = [
        ("OPENAI_API_KEY", "OpenAI provider"),
        ("ANTHROPIC_API_KEY", "Anthropic provider"),
        ("LARK_APP_SECRET", "Lark 飞书连接器 (Phase 2 W10)"),
        ("LARK_VERIFICATION_TOKEN", "Lark 飞书连接器 (Phase 2 W10)"),
        ("LARK_ENCRYPT_KEY", "Lark 飞书加密 (Phase 2 W10)"),
        ("VAULT_ADDR", "Vault 密钥管理 (Phase 3 W20)"),
        ("VAULT_TOKEN", "Vault 凭证 (Phase 3 W20)"),
    ]
    present: list[str] = []
    missing: list[str] = []
    for var, desc in candidates:
        if os.environ.get(var):
            present.append(f"{var} ({desc})")
        else:
            missing.append(f"{var} ({desc})")

    if not present and not missing:
        return CheckResult(
            name="secrets",
            status=CheckStatus.OK,
            message="no secrets checked (candidates list empty)",
        )

    # 至少需要一个 LLM provider key 才能跑（除非用 fake_llm 测试）
    has_llm = any("OPENAI_API_KEY" in p or "ANTHROPIC_API_KEY" in p for p in present)
    if not has_llm and not os.environ.get("AGENT_SWARM_FAKE_LLM"):
        return CheckResult(
            name="secrets",
            status=CheckStatus.WARN,
            message="no LLM provider key set",
            detail=(
                "Set OPENAI_API_KEY or ANTHROPIC_API_KEY, or "
                "AGENT_SWARM_FAKE_LLM=1 for offline test mode.\n"
                "Other keys (Lark/Vault) are optional based on what you run."
            ),
        )

    return CheckResult(
        name="secrets",
        status=CheckStatus.OK,
        message=f"{len(present)} secret(s) present",
        detail="\n".join(f"    [green]✓[/] {p}" for p in present),
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=Path.home() / ".agent_swarm" / "sessions.db",
    help="Session 数据库路径 (默认 ~/.agent_swarm/sessions.db)",
)
@click.option(
    "--mcp-config",
    "mcp_config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="MCP YAML 配置路径；提供则检查 MCP server 状态",
)
@click.option(
    "--skip-llm",
    is_flag=True,
    help="跳过 LLM provider 连通性检查（避免网络调用）",
)
@click.option(
    "--skip-mcp",
    is_flag=True,
    help="跳过 MCP server 状态检查",
)
@click.option(
    "--skip-sandbox",
    is_flag=True,
    help="跳过 Docker sandbox 可用性检查（无 Docker 环境的 CI 友好）",
)
def doctor(
    db_path: Path,
    mcp_config: Path | None,
    skip_llm: bool,
    skip_mcp: bool,
    skip_sandbox: bool,
) -> None:
    """agent-swarm 健康检查——LLM/SQLite/MCP/密钥"""
    report = DoctorReport()

    # 1) LLM provider 连通
    if skip_llm:
        report.add(
            CheckResult(
                name="llm.providers",
                status=CheckStatus.SKIP,
                message="skipped (--skip-llm)",
            )
        )
    else:

        async def _run_llm() -> list[CheckResult]:
            results = []
            for prov, env in [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")]:
                results.append(await check_llm_provider(prov, env))
            return results

        try:
            for r in asyncio.run(_run_llm()):
                report.add(r)
        except Exception as exc:  # noqa: BLE001
            report.add(
                CheckResult(
                    name="llm.providers",
                    status=CheckStatus.FAIL,
                    message=f"check crashed: {exc}",
                )
            )

    # 2) SQLite 锁
    report.add(check_sqlite_lock(db_path))

    # 3) MCP server
    if skip_mcp:
        report.add(
            CheckResult(
                name="mcp.servers",
                status=CheckStatus.SKIP,
                message="skipped (--skip-mcp)",
            )
        )
    else:
        try:
            mcp_result = asyncio.run(check_mcp_servers(mcp_config))
            report.add(mcp_result)
        except Exception as exc:  # noqa: BLE001
            report.add(
                CheckResult(
                    name="mcp.servers",
                    status=CheckStatus.FAIL,
                    message=f"check crashed: {exc}",
                )
            )

    # 4) 密钥
    report.add(check_secrets())

    # 5) Docker sandbox (W19-4) —— 检查 Docker 可用性
    if skip_sandbox:
        report.add(
            CheckResult(
                name="sandbox.docker",
                status=CheckStatus.SKIP,
                message="skipped (--skip-sandbox)",
            )
        )
    else:
        try:
            from agent_swarm.security.sandbox_docker import (
                DockerSandboxManager,
            )

            # 用临时 workspace 跑 doctor_check
            with tempfile.TemporaryDirectory() as td:
                ws = Path(td) / "ws"
                ws.mkdir()
                docker_mgr = DockerSandboxManager(ws)
                dck_report = asyncio.run(docker_mgr.doctor_check())
            if dck_report.get("docker_available"):
                msg = (
                    f"Docker available (v{dck_report['docker_version']}). "
                    f"{len(dck_report['cis_checks'])} CIS checks enabled, "
                    f"{dck_report['escape_attempts_count']} escape attempts blocked. "
                    f"Recommendation: {dck_report['recommendation'][:80]}"
                )
                status = CheckStatus.OK
            else:
                msg = (
                    "Docker not available. WORKSPACE_ONLY mode active. "
                    f"Recommendation: {dck_report['recommendation'][:80]}"
                )
                status = CheckStatus.WARN
            report.add(
                CheckResult(
                    name="sandbox.docker",
                    status=status,
                    message=msg,
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                CheckResult(
                    name="sandbox.docker",
                    status=CheckStatus.WARN,
                    message=f"Docker check skipped: {exc}",
                )
            )

    report.render()
    sys.exit(report.exit_code())


if __name__ == "__main__":
    doctor()
