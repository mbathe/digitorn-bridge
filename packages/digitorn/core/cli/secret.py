"""CLI commands for per-app secret management."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

secret_cli = typer.Typer(
    name="secret",
    help="Per-app secret management (encrypted key/value store).",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@secret_cli.command(name="list")
def list_secrets(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all secret keys for an app (values are not shown)."""
    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}/secrets")
    data = resp.json()

    if not data.get("success"):
        console.print(f"[red]{data.get('error', '?')}[/red]")
        raise typer.Exit(1)

    keys = data.get("data", {}).get("keys", data.get("data", []))
    if isinstance(keys, list):
        for k in keys:
            console.print(f"  [cyan]{k}[/cyan]")
    elif isinstance(keys, dict):
        for k in keys:
            console.print(f"  [cyan]{k}[/cyan]")
    if not keys:
        console.print("[dim]No secrets set.[/dim]")


@secret_cli.command(name="set")
def set_secret(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    key: Annotated[str, typer.Argument(help="Secret key.")],
    value: Annotated[str, typer.Argument(help="Secret value.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Set a secret for an app."""
    resp = daemon_request(
        "put",
        f"{daemon}/api/apps/{app_id}/secrets/{key}",
        json={"value": value},
    )
    data = resp.json()

    if data.get("success"):
        console.print(f"[green]Set[/green] {app_id}/{key}")
    else:
        console.print(f"[red]Failed[/red] - {data.get('error', '?')}")
        raise typer.Exit(1)


@secret_cli.command(name="get")
def get_secret(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    key: Annotated[str, typer.Argument(help="Secret key.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Check if a secret exists (value is never returned)."""
    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}/secrets/{key}")
    data = resp.json()

    if data.get("success"):
        console.print(f"[green]Exists[/green] - {app_id}/{key}")
    else:
        console.print(f"[yellow]Not found[/yellow] - {app_id}/{key}")


@secret_cli.command()
def delete(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    key: Annotated[str, typer.Argument(help="Secret key.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Delete a secret."""
    resp = daemon_request("delete", f"{daemon}/api/apps/{app_id}/secrets/{key}")
    data = resp.json()

    if data.get("success"):
        console.print(f"[green]Deleted[/green] {app_id}/{key}")
    else:
        console.print(f"[red]Failed[/red] - {data.get('error', '?')}")
