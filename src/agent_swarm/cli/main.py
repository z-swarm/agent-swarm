"""
@module agent_swarm.cli.main
@brief  agent-swarm CLI 入口

W1: run
W2: ——（仅 run 内部能力增强）
W3: session list / show / resume；run 自动写 SQLite 事件流
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

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
def run(
    config: Path,
    verbose: bool,
    db_path: Path | None,
    json_log: bool,
) -> None:
    """运行 swarm（从 YAML 配置启动）"""
    _configure_logging(verbose)

    db = db_path or DEFAULT_DB_PATH
    bus, sink = _setup_observability(db, json_log=json_log)

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

    async def _run_with_session():
        # 注册 session 元数据
        mgr = SessionManager(sink)
        await mgr.create_session(
            swarm_name=swarm.name,
            session_id=swarm.session_id,
            config_yaml=config.read_text(encoding="utf-8"),
        )
        try:
            res = await swarm.run()
            await mgr.end_session(swarm.session_id, res.state)
            return res
        finally:
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
        ended = (
            datetime.fromtimestamp(s.ended_at).strftime("%H:%M:%S")
            if s.ended_at else "-"
        )
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
def session_show(session_id: str, db_path: Path | None, events: bool) -> None:
    """显示 session 详情 + 事件流"""
    db = db_path or DEFAULT_DB_PATH
    if not db.exists():
        console.print(f"[red]Session database not found:[/] {db}")
        sys.exit(2)

    sink = SqliteEventSink(db)
    mgr = SessionManager(sink)

    async def _show():
        try:
            info = await mgr.get_session(session_id)
            if info is None:
                return None, []
            evts = await sink.get_events(session_id) if events else []
            return info, evts
        finally:
            await sink.aclose()

    info, evts = asyncio.run(_show())
    if info is None:
        console.print(f"[red]Session not found:[/] {session_id}")
        sys.exit(2)

    console.print(f"[bold cyan]session=[/]{info.session_id}")
    console.print(f"  swarm: {info.swarm_name}")
    console.print(f"  state: {info.state or '[yellow]running?[/]'}")
    console.print(
        f"  created: "
        f"{datetime.fromtimestamp(info.created_at).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if info.ended_at:
        console.print(
            f"  ended: "
            f"{datetime.fromtimestamp(info.ended_at).strftime('%Y-%m-%d %H:%M:%S')}"
        )

    if events:
        console.rule(f"[bold]Events ({len(evts)})[/]")
        for e in evts:
            ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S.%f")[:-3]
            console.print(
                f"[dim]{e.seq:4d}[/] [yellow]{ts}[/] "
                f"[cyan]{e.event_name}[/] {e.payload}"
            )


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
                "completed": "green", "failed": "red",
                "in_progress": "yellow", "blocked": "magenta",
                "pending": "white",
            }.get(t.status, "white")
            table.add_row(
                t.id, t.title, f"[{color}]{t.status}[/]",
                t.assigned_to or "-", str(t.version),
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
