"""CLI commands for middleware management."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

middleware_cli = typer.Typer(
    name="middleware",
    help="Manage middleware packages (list, install, create, uninstall).",
    no_args_is_help=True,
)


@middleware_cli.command(name="list")
def list_middleware() -> None:
    """List all installed middleware packages."""
    try:
        from digitorn.core.middleware_store import get_middleware_registry
        registry = get_middleware_registry()
        if not registry.list_all():
            registry.discover()

        entries = registry.list_all()
        if not entries:
            console.print("[dim]No middleware packages installed.[/dim]")
            return

        table = Table(title="Middleware Packages")
        table.add_column("Name", style="cyan")
        table.add_column("Level")
        table.add_column("Description")

        # entries may be a list or dict depending on registry implementation
        items = entries.items() if isinstance(entries, dict) else enumerate(entries)
        for key, entry in items:
            name = key if isinstance(key, str) else getattr(entry, "name", str(key))
            table.add_row(
                name,
                getattr(entry, "level", "?"),
                getattr(entry, "description", "")[:60],
            )

        console.print(table)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


@middleware_cli.command()
def install(
    package: Annotated[str, typer.Argument(help="Package name (pip install name).")],
) -> None:
    """Install a middleware package."""
    import subprocess
    import sys

    console.print(f"Installing [cyan]{package}[/cyan]...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        console.print(f"[green]Installed[/green] {package}")
    else:
        console.print(f"[red]Failed[/red]")
        console.print(result.stderr[:500])
        raise typer.Exit(1)


@middleware_cli.command()
def uninstall(
    package: Annotated[str, typer.Argument(help="Package name to remove.")],
) -> None:
    """Uninstall a middleware package."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", package],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        console.print(f"[green]Removed[/green] {package}")
    else:
        console.print(f"[red]Failed[/red]")
        console.print(result.stderr[:500])
