"""
@module agent_swarm.cli.main
@brief  agent-swarm CLI 入口

W1: run
W2: ——（仅 run 内部能力增强）
W3: session list / show / resume；run 自动写 SQLite 事件流
"""

from __future__ import annotations

import asyncio
import contextlib  # P5-W31: web 关闭 suppress
import logging
import os  # P-4 修复:从函数内 inline import 提升到顶部
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from agent_swarm import __version__
from agent_swarm.core.session_manager import SessionManager
from agent_swarm.core.swarm import Swarm, SwarmResult
from agent_swarm.observability import (
    JsonLogSink,
    ObservabilityBus,
    SqliteEventSink,
    set_global_bus,
)

console = Console()

# 默认 session 数据库路径——CLI 进程内统一一份
DEFAULT_DB_PATH = Path.home() / ".agent_swarm" / "sessions.db"

# P1-3.2 (REVIEW-2026-06-19 §3.2)：provider ↔ env var 映射
# 让 CLI 不再硬写 OPENAI_API_KEY，Anthropic 支持真正落地
PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _resolve_api_key_env(provider: str | None, api_key: str | None) -> str | None:
    """
    解析 API key 注入到哪个环境变量

    @param provider  --provider 选项值；None 时按 api_key 是否给出来推断
    @param api_key   --api-key 显式值；None 时不做任何环境变量注入

    @return 被设置的环境变量名；未设置时返回 None

    @note 行为约定:
      - 给了 --api-key 但没给 --provider → 报 Click error（避免误注入）
      - 没给 --api-key → 留给各 provider 自己读 env（向后兼容）
    """
    if api_key is None:
        return None
    if provider is None:
        raise click.UsageError(
            "--api-key requires --provider (openai|anthropic); "
            "to set the env var, specify which one."
        )
    p = provider.lower()
    if p not in PROVIDER_ENV_VARS:
        raise click.UsageError(
            f"unknown --provider {provider!r}; valid: {sorted(PROVIDER_ENV_VARS.keys())}"
        )
    return PROVIDER_ENV_VARS[p]


# P2-3.6 (REVIEW-2026-06-19 §3.6)：session DB 路径 fail-fast
# 多人共用机器 / CI runner 时，~ 路径可能不可写或被其他人读到；
# CLI 应在打开 db 之前显式校验，否则会 create 一个无权限空文件
def _validate_db_writable(db_path: Path) -> None:
    """
    校验 session db 路径可写

    @raise click.UsageError 路径无写权限 / 父目录不存在 / 路径指向不可写文件
    """
    if str(db_path) == ":memory:":
        return  # SQLite 内存库特殊标识
    db_path = db_path.resolve()

    # 1) 文件已存在 → 检查是否可写
    if db_path.exists():
        if db_path.is_dir():
            raise click.UsageError(f"session db path {db_path} is a directory, not a file")
        if not db_path.is_file():
            raise click.UsageError(f"session db path {db_path} exists but is not a regular file")
        if not _is_writable_file(db_path):
            raise click.UsageError(
                f"session db {db_path} is not writable (permissions={oct(db_path.stat().st_mode)})"
            )
        return

    # 2) 文件不存在 → 检查父目录可写
    parent = db_path.parent
    if not parent.exists():
        raise click.UsageError(
            f"session db parent directory does not exist: {parent}\n  hint: mkdir -p {parent}"
        )
    if not parent.is_dir():
        raise click.UsageError(f"session db parent {parent} is not a directory")
    if not _is_writable_dir(parent):
        raise click.UsageError(f"session db parent directory {parent} is not writable")


def _is_writable_file(path: Path) -> bool:
    """@brief 校验文件当前用户可写（用 os.access）"""
    import os as _os

    return _os.access(str(path), _os.W_OK)


def _is_writable_dir(path: Path) -> bool:
    """@brief 校验目录当前用户可写（os.access W_OK + 实际能创建 .write_test）"""
    import os as _os
    import tempfile

    if not _os.access(str(path), _os.W_OK):
        return False
    # 真创建一次临时文件——避免 sticky bit 等情况
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".agent_swarm_write_test_",
            dir=str(path),
            delete=True,
        ):
            pass
        return True
    except (OSError, PermissionError):
        return False


def _configure_logging(verbose: bool) -> None:
    """统一日志配置——使用 rich 美化"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )


def _setup_observability(
    db_path: Path, json_log: bool = False
) -> tuple[ObservabilityBus, SqliteEventSink]:
    """
    构造默认 ObservabilityBus + SqliteEventSink + 可选 JsonLogSink

    @return (bus, sink) ——sink 用于 SessionManager 注册元数据
    """
    bus = ObservabilityBus()
    sink = SqliteEventSink(db_path)
    bus.register_sink(sink)
    if json_log:
        bus.register_sink(JsonLogSink())  # 默认不开启——避免污染 stdout 表格
    set_global_bus(bus)
    return bus, sink


@click.group()
@click.version_option(version=__version__, prog_name="agent-swarm")
def cli() -> None:
    """通用多 Agent 协作框架"""


# ---------------------------------------------------------------------------
# doctor 子命令（W14b: §17.7 DX 工具）
# ---------------------------------------------------------------------------


# 模块加载时注册 doctor——避免在 __main__ 入口被重新导入触发 runpy 警告
# @note 延迟 import 内部完成；模块顶层 import doctor 只调一次
from agent_swarm.cli.doctor import doctor as _doctor_cmd  # noqa: E402

cli.add_command(_doctor_cmd)


@cli.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-v", "--verbose", is_flag=True, help="显示 DEBUG 级日志")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"会话数据库路径 (默认 {DEFAULT_DB_PATH})",
)
@click.option(
    "--json-log",
    is_flag=True,
    help="同时输出结构化 JSON 事件流到 stderr",
)
@click.option(
    "--provider",
    "provider",
    type=click.Choice(["openai", "anthropic"], case_sensitive=False),
    default=None,
    help="LLM provider (openai/anthropic); 与 --api-key 配合使用",
)
@click.option(
    "--api-key",
    "api_key",
    type=str,
    default=None,
    help=(
        "LLM provider API key（需配合 --provider 决定注入哪个环境变量）；"
        "省略时各 provider 自动从对应 env 读（OPENAI_API_KEY / ANTHROPIC_API_KEY）"
    ),
)
@click.option(
    "--web",
    "enable_web",
    is_flag=True,
    help="P5-W31: 启动 web UI (HTMX+FastAPI) 同一进程内, 浏览器看实时事件",
)
@click.option(
    "--web-host",
    "web_host",
    type=str,
    default="127.0.0.1",
    help="Web UI 绑定地址 (默认 127.0.0.1)",
)
@click.option(
    "--web-port",
    "web_port",
    type=int,
    default=8000,
    help="Web UI 端口 (默认 8000)",
)
@click.option(
    "--web-worktree-repo",
    "web_worktree_repo",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="P5-W32: WorktreeManager repo_root (git 仓库路径); 启用后 /worktrees 页显真数据",
)
@click.option(
    "--web-worktree-base",
    "web_worktree_base",
    type=click.Path(path_type=Path),
    default=None,
    help="P5-W32: WorktreeManager base_dir (worktree 输出目录); 默认 <repo>/.worktrees",
)
@click.option(
    "--web-postgres-dsn",
    "web_postgres_dsn",
    type=str,
    default=None,
    help=(
        "P5-W33: WebState Postgres 持久化 DSN (postgresql://...); "
        "省略时维持内存 WebState (W28 行为, 零破坏); "
        "启用后事件流落盘, 重启可拉回 (跨进程 fan-out 仍受 PG 限制)"
    ),
)
@click.option(
    "--web-postgres-table",
    "web_postgres_table",
    type=str,
    default="webstate_events",
    help="P5-W33: Postgres 表名 (默认 webstate_events)",
)
@click.option(
    "--web-cross-process/--no-web-cross-process",
    "web_cross_process",
    default=False,
    help=(
        "P5-W35: 启用跨进程 LISTEN/NOTIFY fan-out "
        "(需配合 --web-postgres-dsn; 多 web UI 实例实时同步事件流)"
    ),
)
@click.option(
    "--web-jwt-secret",
    "web_jwt_secret",
    type=str,
    default=None,
    help=(
        "P5-W34: HS256 共享密钥; 省略时无鉴权 (开发模式, 零破坏); "
        "支持 ${WEB_JWT_SECRET} 引用环境变量; 启用后写路径 (POST/PUT/DELETE) 需 Bearer token"
    ),
)
@click.option(
    "--web-jwt-secret-ref",
    "web_jwt_secret_ref",
    type=str,
    default=None,
    help=(
        "P5-W36a: secret 引用字符串 (literal / ${VAR} / secret://key); "
        "与 --web-jwt-secret 互斥; secret:// 模式走 SecretManager 支持轮换不重启"
    ),
)
@click.option(
    "--web-secret-manager",
    "web_secret_manager",
    type=click.Choice(["env", "vault"], case_sensitive=False),
    default="env",
    help=(
        "P5-W36a: SecretManager 后端 (仅 --web-jwt-secret-ref=secret:// 模式生效); "
        "env=EnvSecretManager (默认, 零依赖); vault=VaultSecretManager (需 hvac + --vault-*)"
    ),
)
@click.option(
    "--vault-url",
    "vault_url",
    type=str,
    default="http://127.0.0.1:8200",
    help="P5-W36a: Vault URL (仅 --web-secret-manager=vault 生效)",
)
@click.option(
    "--vault-role-id",
    "vault_role_id",
    type=str,
    default=None,
    help="P5-W36a: Vault AppRole role_id (仅 --web-secret-manager=vault 生效)",
)
@click.option(
    "--vault-secret-id",
    "vault_secret_id",
    type=str,
    default=None,
    help="P5-W36a: Vault AppRole secret_id (仅 --web-secret-manager=vault 生效)",
)
@click.option(
    "--web-jwt-expires",
    "web_jwt_expires",
    type=int,
    default=3600,
    help="P5-W34: JWT 有效期 (秒, 默认 3600)",
)
@click.option(
    "--web-review-mode",
    "web_review_mode",
    type=click.Choice(["simple", "full"], case_sensitive=False),
    default="full",
    help=(
        "P5-W36f: agent_review 模式; simple 走 W36b 同步 (确定性 Judge), "
        "full 走 W36f 异步 + SSE (LLM judge, 默认)"
    ),
)
@click.option(
    "--web-review-llm",
    "web_review_llm",
    type=click.Choice(["openai", "anthropic", "fake"], case_sensitive=False),
    default="fake",
    help=(
        "P5-W36f: full mode LLM provider; "
        "openai/anthropic 需对应 API key, fake 走确定性 Judge (W13 default)"
    ),
)
@click.option(
    "--web-review-timeout",
    "web_review_timeout",
    type=float,
    default=60.0,
    help="P5-W36f: full mode LLM 调用超时 (秒, 默认 60)",
)
@click.option(
    "--web-task-store",
    "web_task_store",
    type=click.Choice(["memory", "redis"], case_sensitive=False),
    default="memory",
    help=(
        "P5-W40: task 存储后端; memory 单进程 (W36f 兼容), "
        "redis 多 worker 共享 (需 --web-redis-dsn)"
    ),
)
@click.option(
    "--web-redis-dsn",
    "web_redis_dsn",
    type=str,
    default=None,
    help="P5-W40: Redis DSN (redis://..., 仅 --web-task-store=redis 模式生效)",
)
def run(
    config: Path,
    verbose: bool,
    db_path: Path | None,
    json_log: bool,
    provider: str | None,
    api_key: str | None,
    enable_web: bool,
    web_host: str,
    web_port: int,
    web_worktree_repo: Path | None,
    web_worktree_base: Path | None,
    web_postgres_dsn: str | None,
    web_postgres_table: str,
    web_cross_process: bool,
    web_jwt_secret: str | None,
    web_jwt_secret_ref: str | None,
    web_secret_manager: str,
    vault_url: str,
    vault_role_id: str | None,
    vault_secret_id: str | None,
    web_jwt_expires: int,
    web_review_mode: str,
    web_review_llm: str,
    web_review_timeout: float,
    web_task_store: str,
    web_redis_dsn: str | None,
) -> None:
    """运行 swarm（从 YAML 配置启动）"""
    _configure_logging(verbose)

    db = db_path or DEFAULT_DB_PATH
    _validate_db_writable(db)  # P2-3.6 fail-fast
    bus, sink = _setup_observability(db, json_log=json_log)
    # P1-3.2：根据 --provider 把 api_key 注入到正确的环境变量
    env_var = _resolve_api_key_env(provider, api_key)
    if env_var:
        os.environ[env_var] = api_key  # type: ignore[assignment]

    console.print(f"[bold cyan]agent-swarm[/] loading [yellow]{config}[/]")
    try:
        swarm = Swarm.from_yaml(config)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to load config:[/] {exc}")
        asyncio.run(bus.aclose())
        sys.exit(2)

    console.print(
        f"[bold cyan]swarm=[/]{swarm.name} "
        f"[dim]session={swarm.session_id} agents={len(swarm.agents)} "
        f"tasks={len(swarm.tasks)}[/]"
    )

    # P5-W31: 可选 web UI
    web_state = None
    web_server = None
    web_task = None
    web_sink = None
    worktree_manager = None
    if enable_web:
        try:
            import uvicorn  # noqa: E402

            from agent_swarm.observability import WebStateSink  # noqa: E402
            from agent_swarm.web import WebState, create_app  # noqa: E402
        except ImportError as exc:
            console.print(f"[red]--web 需要额外依赖: {exc}. 运行: pip install -e .[web][/]")
            sys.exit(2)
        # P5-W32: 可选 WorktreeManager 注入
        if web_worktree_repo is not None:
            try:
                from agent_swarm.worktree import WorktreeManager  # noqa: E402
            except ImportError as exc:
                console.print(f"[red]--web-worktree-repo 需要 worktree 模块: {exc}[/]")
                sys.exit(2)
            base = web_worktree_base or (web_worktree_repo / ".worktrees")
            worktree_manager = WorktreeManager(
                repo_root=web_worktree_repo,
                base_dir=base,
            )
            console.print(f"[bold magenta]worktree[/] → repo={web_worktree_repo} base={base}")
        web_state = WebState()
        web_sink = WebStateSink(web_state)
        bus.register_sink(web_sink)
        # P5-W36a/W36c: 构造 SecretManager (secret:// + vault:// 模式)
        cli_secret_manager: Any = None
        if web_jwt_secret_ref and (
            web_jwt_secret_ref.startswith("secret://") or web_jwt_secret_ref.startswith("vault://")
        ):
            if web_secret_manager.lower() == "vault":
                try:
                    from agent_swarm.security.secret_manager import (
                        VaultConfig,
                        VaultSecretManager,
                    )
                except ImportError as exc:
                    console.print(
                        f"[red]--web-secret-manager=vault 需要 hvac: {exc}. "
                        f"运行: pip install hvac[/]"
                    )
                    sys.exit(2)
                cli_secret_manager = VaultSecretManager(
                    VaultConfig(
                        url=vault_url,
                        role_id=vault_role_id or "",
                        secret_id=vault_secret_id or "",
                    )
                )
                console.print(f"[bold magenta]web secret manager[/] → vault url={vault_url}")
            else:
                # env 模式: create_app 内部自动实例化, 此处不传
                cli_secret_manager = None
        app = create_app(
            web_state=web_state,
            worktree_manager=worktree_manager,
            postgres_dsn=web_postgres_dsn,
            postgres_table=web_postgres_table,
            enable_cross_process=web_cross_process,
            jwt_secret=web_jwt_secret,
            jwt_secret_ref=web_jwt_secret_ref,
            secret_manager=cli_secret_manager,
            jwt_expires_seconds=web_jwt_expires,
            review_mode=web_review_mode,
            review_llm=web_review_llm,
            review_timeout=web_review_timeout,
        )
        # W40: task store (memory / redis)
        from agent_swarm.web.review_runner import create_task_store

        task_store = create_task_store(web_task_store, web_redis_dsn)
        app.state.task_store = task_store
        uv_config = uvicorn.Config(
            app,
            host=web_host,
            port=web_port,
            log_level="warning",
            lifespan="on",
        )
        web_server = uvicorn.Server(uv_config)
        console.print(f"[bold magenta]web UI[/] → http://{web_host}:{web_port}")
        if web_postgres_dsn:
            console.print(
                f"[bold magenta]web state store[/] → postgres table=[cyan]{web_postgres_table}[/]"
            )

    async def _run_with_session():
        nonlocal web_task
        # 注册 session 元数据
        mgr = SessionManager(sink)
        await mgr.create_session(
            swarm_name=swarm.name,
            session_id=swarm.session_id,
            config_yaml=config.read_text(encoding="utf-8"),
        )
        try:
            # 起 web (同 loop)
            if web_server is not None:
                web_task = asyncio.create_task(
                    web_server.serve(),
                    name="web-ui",
                )
            res = await swarm.run()
            await mgr.end_session(swarm.session_id, res.state)
            return res
        finally:
            # 停 web
            if web_server is not None:
                web_server.should_exit = True
            if web_task is not None:
                with contextlib.suppress(Exception):
                    await web_task
            await bus.aclose()

    try:
        result: SwarmResult = asyncio.run(_run_with_session())
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Swarm crashed:[/] {exc}")
        if verbose:
            console.print_exception()
        sys.exit(1)

    _print_summary(result)
    sys.exit(0 if result.state == "completed" else 1)


# ---------------------------------------------------------------------------
# tui 子命令（W6: 实时仪表盘）
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-v", "--verbose", is_flag=True, help="显示 DEBUG 级日志")
@click.option(
    "--provider",
    "provider",
    type=click.Choice(["openai", "anthropic"], case_sensitive=False),
    default=None,
    help="LLM provider (openai/anthropic); 与 --api-key 配合使用",
)
@click.option(
    "--api-key",
    "api_key",
    type=str,
    default=None,
    help=(
        "LLM provider API key（需配合 --provider 决定注入哪个环境变量）；"
        "省略时各 provider 自动从对应 env 读（OPENAI_API_KEY / ANTHROPIC_API_KEY）"
    ),
)
def tui(config: Path, verbose: bool, provider: str | None, api_key: str | None) -> None:
    """
    @brief  在 TUI 仪表盘中运行 swarm（DESIGN.md §17.1 W6 DoD）

    @note 与 run 不同: 实时显示 4 面板 (Status / Tasks / Messages / Budget)
          不持久化到 SQLite——TUI 是观察工具, session 历史请用 run + session show
    """
    _configure_logging(verbose)
    # P1-3.2：与 run 一致的 provider 分发
    env_var = _resolve_api_key_env(provider, api_key)
    if env_var:
        os.environ[env_var] = api_key  # type: ignore[assignment]

    # TUI 自己的轻量 bus: 1 个 JsonLogSink (stderr) + 1 个 TUISink
    from agent_swarm.observability import (
        JsonLogSink,
        ObservabilityBus,
        set_global_bus,
    )
    from agent_swarm.tui import run_dashboard

    bus = ObservabilityBus()
    bus.register_sink(JsonLogSink())
    set_global_bus(bus)

    try:
        swarm = Swarm.from_yaml(config)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to load config:[/] {exc}")
        sys.exit(2)

    console.print(
        f"[bold cyan]TUI launching[/] swarm=[yellow]{swarm.name}[/] session={swarm.session_id}"
    )
    try:
        asyncio.run(run_dashboard(swarm))
    except KeyboardInterrupt:
        console.print("[yellow]tui interrupted[/]")
        sys.exit(130)


# ---------------------------------------------------------------------------
# session 子命令组
# ---------------------------------------------------------------------------


@cli.group()
def session() -> None:
    """Session 管理：list / show / resume"""


@session.command("list")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"会话数据库路径 (默认 {DEFAULT_DB_PATH})",
)
def session_list(db_path: Path | None) -> None:
    """列出已知 session"""
    db = db_path or DEFAULT_DB_PATH
    _validate_db_writable(db)  # P2-3.6
    if not db.exists():
        console.print(f"[yellow]No session database at {db}[/]")
        sys.exit(0)

    sink = SqliteEventSink(db)
    mgr = SessionManager(sink)

    async def _list():
        try:
            return await mgr.list_sessions()
        finally:
            await sink.aclose()

    sessions = asyncio.run(_list())
    if not sessions:
        console.print("[dim](no sessions yet)[/]")
        return

    table = Table(title=f"Sessions @ {db}")
    table.add_column("Session", style="cyan")
    table.add_column("Swarm")
    table.add_column("State")
    table.add_column("Created")
    table.add_column("Ended")

    for s in sessions:
        state = s.state or "[yellow]running?[/]"
        created = datetime.fromtimestamp(s.created_at).strftime("%Y-%m-%d %H:%M:%S")
        ended = datetime.fromtimestamp(s.ended_at).strftime("%H:%M:%S") if s.ended_at else "-"
        table.add_row(s.session_id, s.swarm_name, state, created, ended)
    console.print(table)


@session.command("show")
@click.argument("session_id")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option(
    "--events/--no-events",
    default=True,
    help="是否打印事件流（默认 True）",
)
@click.option(
    "--config/--no-config",
    default=False,
    help="是否打印原始 yaml 配置（默认 False）",
)
def session_show(session_id: str, db_path: Path | None, events: bool, config: bool) -> None:
    """显示 session 详情 + 事件流"""
    db = db_path or DEFAULT_DB_PATH
    _validate_db_writable(db)  # P2-3.6
    if not db.exists():
        console.print(f"[red]Session database not found:[/] {db}")
        sys.exit(2)

    sink = SqliteEventSink(db)
    mgr = SessionManager(sink)

    async def _show():
        try:
            info = await mgr.get_session(session_id)
            if info is None:
                return None, [], None
            evts = await sink.get_events(session_id) if events else []
            # W3-Z4: 同时取回 config_yaml
            full = await sink.get_session(session_id)
            cfg_yaml = full["config_yaml"] if full and config else None
            return info, evts, cfg_yaml
        finally:
            await sink.aclose()

    info, evts, cfg_yaml = asyncio.run(_show())
    if info is None:
        console.print(f"[red]Session not found:[/] {session_id}")
        sys.exit(2)

    console.print(f"[bold cyan]session=[/]{info.session_id}")
    console.print(f"  swarm: {info.swarm_name}")
    console.print(f"  state: {info.state or '[yellow]running?[/]'}")
    console.print(
        f"  created: {datetime.fromtimestamp(info.created_at).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if info.ended_at:
        console.print(
            f"  ended: {datetime.fromtimestamp(info.ended_at).strftime('%Y-%m-%d %H:%M:%S')}"
        )

    if cfg_yaml:
        console.rule("[bold]Config YAML[/]")
        console.print(cfg_yaml)

    if events:
        console.rule(f"[bold]Events ({len(evts)})[/]")
        for e in evts:
            ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S.%f")[:-3]
            console.print(f"[dim]{e.seq:4d}[/] [yellow]{ts}[/] [cyan]{e.event_name}[/] {e.payload}")


@session.command("resume")
@click.argument("session_id")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
)
def session_resume(session_id: str, db_path: Path | None) -> None:
    """
    恢复 session——读事件流重建 task_queue / mailbox 状态并打印

    @note W3 范围：仅展示恢复后的状态；继续执行（接着跑剩余任务）留待 W4
    """
    db = db_path or DEFAULT_DB_PATH
    _validate_db_writable(db)  # P2-3.6
    if not db.exists():
        console.print(f"[red]Session database not found:[/] {db}")
        sys.exit(2)

    sink = SqliteEventSink(db)
    mgr = SessionManager(sink)

    async def _resume():
        try:
            return await mgr.restore_session(session_id)
        finally:
            await sink.aclose()

    try:
        state = asyncio.run(_resume())
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        sys.exit(2)

    console.print(
        f"[bold green]Restored[/] session={state.session_id} "
        f"swarm={state.swarm_name} events={state.event_count} "
        f"last_seq={state.last_seq}"
    )

    async def _gather():
        tasks = await state.task_queue.list_all()
        msgs = await state.mailbox.all_messages()
        return tasks, msgs

    tasks, msgs = asyncio.run(_gather())

    if tasks:
        table = Table(title="Tasks (restored)")
        table.add_column("ID", style="cyan")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Assigned")
        table.add_column("Version", justify="right")
        for t in tasks:
            color = {
                "completed": "green",
                "failed": "red",
                "in_progress": "yellow",
                "blocked": "magenta",
                "pending": "white",
            }.get(t.status, "white")
            table.add_row(
                t.id,
                t.title,
                f"[{color}]{t.status}[/]",
                t.assigned_to or "-",
                str(t.version),
            )
        console.print(table)

    if msgs:
        console.rule(f"[bold]Messages ({len(msgs)})[/]")
        for m in msgs:
            read_mark = "✓" if m.read else "○"
            console.print(
                f"  [dim]{read_mark}[/] [yellow]{m.from_agent}[/] → "
                f"[cyan]{m.to_agent}[/] [{m.msg_type}] {m.content}"
            )


def _print_summary(res: SwarmResult) -> None:
    """打印运行结果概要"""
    color = "green" if res.state == "completed" else "red"
    header = (
        f"[{color}]swarm done[/] · {res.state} · {res.duration_seconds:.1f}s · "
        f"completed={res.tasks_completed} failed={res.tasks_failed} "
        f"unfinished={res.tasks_unfinished}"
    )
    console.rule(header)

    table = Table(title="Tasks", show_lines=False)
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Tokens", justify="right")

    for ar in res.agent_results:
        status_color = "green" if ar.task.status == "completed" else "red"
        table.add_row(
            ar.task.id,
            ar.task.title,
            f"[{status_color}]{ar.task.status}[/]",
            f"{ar.tokens_total:,}",
        )
    console.print(table)

    # 打印每个任务的最终输出
    for ar in res.agent_results:
        console.rule(f"[bold]task={ar.task.id} · {ar.task.title}[/]")
        if ar.task.status == "failed":
            console.print(f"[red]error:[/] {ar.task.error}")
        else:
            console.print(ar.final_text or "[dim](no output)[/]")

    if res.state == "failed" and res.error:
        console.print(f"\n[red bold]swarm error:[/] {res.error}")


if __name__ == "__main__":
    cli()
