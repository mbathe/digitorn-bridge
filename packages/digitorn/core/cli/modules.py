"""CLI commands for module management."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

modules_cli = typer.Typer(
    name="modules",
    help="Module management commands.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@modules_cli.command(name="list")
def list_modules(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all loaded modules with their status."""
    resp = daemon_request("get", f"{daemon}/api/modules")
    data = resp.json()

    modules = data.get("modules", data) if isinstance(data, dict) else data
    if isinstance(modules, dict):
        modules = list(modules.values())

    if not modules:
        console.print("[dim]No modules loaded.[/dim]")
        return

    table = Table(title="Loaded Modules")
    table.add_column("Module ID", style="cyan")
    table.add_column("Version")
    table.add_column("Actions", justify="right")
    table.add_column("Status")

    for mod in modules:
        if isinstance(mod, dict):
            table.add_row(
                mod.get("module_id", "?"),
                mod.get("version", "?"),
                str(mod.get("action_count", len(mod.get("actions", [])))),
                mod.get("status", "loaded"),
            )

    console.print(table)


@modules_cli.command()
def info(
    module_id: Annotated[str, typer.Argument(help="Module ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Show module details - actions, config schema, permissions."""
    resp = daemon_request("get", f"{daemon}/api/modules/{module_id}")

    if resp.status_code == 404:
        console.print(f"[red]Module '{module_id}' not found.[/red]")
        raise typer.Exit(1)

    data = resp.json()
    console.print(json.dumps(data, indent=2))


@modules_cli.command()
def health(
    module_id: Annotated[str, typer.Argument(help="Module ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Health check a specific module."""
    resp = daemon_request("get", f"{daemon}/api/modules/{module_id}/health")

    if resp.status_code == 404:
        console.print(f"[red]Module '{module_id}' not found.[/red]")
        raise typer.Exit(1)

    data = resp.json()
    status = data.get("status", "unknown")
    if status == "healthy":
        console.print(f"[green]{module_id}[/green] - healthy")
    else:
        console.print(f"[red]{module_id}[/red] - {status}")
        if data.get("error"):
            console.print(f"  {data['error']}")
