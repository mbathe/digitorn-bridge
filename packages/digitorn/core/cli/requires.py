"""CLI commands for module requirements.

    digitorn requires list        - List all requirements with status
    digitorn requires install     - Install a specific requirement
    digitorn requires install-all - Install all missing requirements
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

requires_cli = typer.Typer(
    name="requires",
    help="Module requirements - list and install external dependencies.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@requires_cli.command(name="list")
def list_requires(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all requirements with install status."""
    resp = daemon_request("get", f"{daemon}/api/requires")
    data = resp.json()

    if isinstance(data, dict) and "requirements" in data:
        reqs = data["requirements"]
    elif isinstance(data, list):
        reqs = data
    else:
        reqs = []

    if not reqs:
        console.print("[green]All requirements satisfied.[/green]")
        return

    table = Table(title="Module Requirements")
    table.add_column("Package", style="cyan")
    table.add_column("Module")
    table.add_column("Status")

    for req in reqs:
        if isinstance(req, dict):
            status = "[green]installed[/green]" if req.get("installed") else "[red]missing[/red]"
            table.add_row(req.get("name", "?"), req.get("module", "?"), status)

    console.print(table)


@requires_cli.command()
def install(
    name: Annotated[str, typer.Argument(help="Package name to install.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Install a specific requirement."""
    console.print(f"Installing [cyan]{name}[/cyan]...", end=" ")
    resp = daemon_request(
        "post",
        f"{daemon}/api/requires/install",
        json={"name": name},
        timeout=120.0,
    )
    data = resp.json()

    if data.get("success"):
        console.print("[green]OK[/green]")
    else:
        console.print("[red]FAILED[/red]")
        console.print(f"  {data.get('error', '?')}")
        raise typer.Exit(1)


@requires_cli.command(name="install-all")
def install_all(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Install all missing requirements."""
    console.print("Installing all missing requirements...")
    resp = daemon_request(
        "post",
        f"{daemon}/api/requires/install-all",
        timeout=300.0,
    )
    data = resp.json()

    if data.get("success"):
        summary = data.get("data", {}).get("summary", {})
        console.print(f"[green]Done[/green] - installed: {summary.get('installed', 0)}, failed: {summary.get('failed', 0)}")
    else:
        console.print(f"[red]Failed[/red] - {data.get('error', '?')}")
