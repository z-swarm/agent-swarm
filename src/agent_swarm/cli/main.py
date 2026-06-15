"""
@module agent_swarm.cli.main
@brief  agent-swarm CLI 入口（W1 仅 run 子命令）

用法:
    agent-swarm run path/to/swarm.yaml
    agent-swarm run path/to/swarm.yaml --verbose
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from agent_swarm import __version__
from agent_swarm.core.swarm import Swarm, SwarmResult

console = Console()


def _configure_logging(verbose: bool) -> None:
    """统一日志配置——使用 rich 美化"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )


@click.group()
@click.version_option(version=__version__, prog_name="agent-swarm")
def cli() -> None:
    """通用多 Agent 协作框架（W1 骨架）"""


@cli.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-v", "--verbose", is_flag=True, help="显示 DEBUG 级日志")
def run(config: Path, verbose: bool) -> None:
    """运行 swarm（从 YAML 配置启动）"""
    _configure_logging(verbose)

    console.print(f"[bold cyan]agent-swarm[/] loading [yellow]{config}[/]")
    try:
        swarm = Swarm.from_yaml(config)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to load config:[/] {exc}")
        sys.exit(2)

    console.print(
        f"[bold cyan]swarm=[/]{swarm.name} "
        f"[dim]agents={len(swarm.agents)}, tasks={len(swarm.tasks)}[/]"
    )

    try:
        result: SwarmResult = asyncio.run(swarm.run())
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


def _print_summary(res: SwarmResult) -> None:
    """打印运行结果概要"""
    color = "green" if res.state == "completed" else "red"
    console.rule(f"[{color}]swarm done[/] · {res.state} · {res.duration_seconds:.1f}s")

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

    # 打印每个任务的最终输出（W1 没有持久化，CLI 是唯一窗口）
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
