"""CLI commands for module management (pure HTTP client).

    digitorn modules list          List all loaded modules (via daemon API)
    digitorn modules info <id>     Show details of a specific module
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
modules_cli = typer.Typer(name="modules", help="Module management commands.")

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@modules_cli.command("list")
def list_modules(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all modules loaded on the daemon."""
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("get", f"{daemon}/api/modules", daemon=daemon)
    data = resp.json()
    modules = data.get("modules", [])

    table = Table(title="Digitorn Modules", border_style="cyan")
    table.add_column("Module ID", style="bold white")
    table.add_column("Version", style="cyan")
    table.add_column("Status")
    table.add_column("Actions", style="dim")

    style_map = {
        "loaded": "green",
        "error": "red",
        "failed": "red",
        "platform_excluded": "dim",
    }

    for m in sorted(modules, key=lambda x: x.get("module_id", "")):
        mid = m.get("module_id", "?")
        version = m.get("version", "?")
        status = m.get("status", "?")
        actions = m.get("actions", [])
        style = style_map.get(status, "yellow")
        table.add_row(
            mid,
            version,
            f"[{style}]{status}[/{style}]",
            str(len(actions)) if actions else (m.get("error", "-") or "-")[:40],
        )

    console.print(table)


@modules_cli.command("info")
def module_info(
    module_id: str = typer.Argument(help="Module ID to inspect."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Show detailed information about a specific module."""
    from digitorn_cli.auth import daemon_request

    resp = daemon_request("get", f"{daemon}/api/modules/{module_id}", daemon=daemon)
    if resp.status_code == 404:
        console.print(f"[bold red]Module '{module_id}' not found.[/bold red]")
        raise typer.Exit(1)

    data = resp.json()

    # Module metadata
    meta_table = Table(show_header=False, box=None, padding=(0, 2))
    meta_table.add_column(style="bold white")
    meta_table.add_column(style="green")
    meta_table.add_row("ID", data.get("module_id", "?"))
    meta_table.add_row("Version", data.get("version", "?"))
    platforms = data.get("platforms", [])
    meta_table.add_row("Platforms", ", ".join(platforms) if platforms else "all")

    console.print(Panel(
        meta_table,
        title=f"[bold]{module_id}[/bold]",
        border_style="cyan",
    ))

    # Actions table
    actions = data.get("actions", [])
    if actions:
        act_table = Table(title="Actions", border_style="cyan")
        act_table.add_column("Action", style="bold white")
        act_table.add_column("Risk", style="yellow")
        act_table.add_column("Description", style="green")

        for action in sorted(actions, key=lambda a: a.get("name", "")):
            act_table.add_row(
                action.get("name", "?"),
                action.get("risk_level", "low"),
                (action.get("description", "") or "")[:60],
            )

        console.print(act_table)
    else:
        console.print("[dim]No actions registered.[/dim]")
