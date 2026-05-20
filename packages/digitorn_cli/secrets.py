"""CLI commands for per-app secret management."""

from __future__ import annotations

import getpass
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

secret_cli = typer.Typer(
    name="secret",
    help="Per-app secret management (encrypted key/value store).",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _api_call(method: str, url: str, daemon: str = _DEFAULT_DAEMON, **kwargs):
    """Make an authenticated API call to the daemon."""
    from digitorn_cli.auth import daemon_request

    try:
        resp = daemon_request(method, url, daemon=daemon, **kwargs)
        return resp.json()
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[bold red]Error: {exc}[/bold red]")
        raise typer.Exit(1)


@secret_cli.command(name="set")
def set_secret(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    key: Annotated[str, typer.Argument(help="Secret key name.")],
    value: Annotated[str | None, typer.Argument(help="Secret value (omit to prompt).")] = None,
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Set an encrypted secret for an application.

    If value is omitted, you'll be prompted to enter it securely (hidden input).

    Examples:
        digitorn secret set my-app API_KEY sk-live-abc123
        digitorn secret set my-app API_KEY   (prompts for value)
    """
    if value is None:
        value = getpass.getpass(f"Enter value for {key}: ")
        if not value:
            console.print("[bold red]Empty value - aborting.[/bold red]")
            raise typer.Exit(1)

    url = f"{daemon}/api/apps/{app_id}/secrets/{key}"
    data = _api_call("put", url, json={"value": value})

    if data.get("success"):
        console.print(f"[green]Secret '{key}' set for app '{app_id}'.[/green]")
    else:
        console.print(f"[bold red]Failed:[/bold red] {data.get('error')}")
        raise typer.Exit(1)


@secret_cli.command(name="list")
def list_secrets(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List secret keys for an application (values are never shown).

    Examples:
        digitorn secret list my-app
    """
    url = f"{daemon}/api/apps/{app_id}/secrets"
    data = _api_call("get", url)

    if not data.get("success"):
        console.print(f"[bold red]Failed:[/bold red] {data.get('error')}")
        raise typer.Exit(1)

    keys = data["data"].get("keys", [])
    if not keys:
        console.print(f"\n[dim]No secrets for app '{app_id}'.[/dim]\n")
        return

    table = Table(title=f"Secrets for '{app_id}'", border_style="cyan")
    table.add_column("#", style="dim")
    table.add_column("Key", style="bold")
    for i, key in enumerate(keys, 1):
        table.add_row(str(i), key)
    console.print()
    console.print(table)
    console.print()


@secret_cli.command(name="delete")
def delete_secret(
    app_id: Annotated[str, typer.Argument(help="Application ID.")],
    key: Annotated[str, typer.Argument(help="Secret key name.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Delete a secret for an application.

    Examples:
        digitorn secret delete my-app API_KEY
    """
    url = f"{daemon}/api/apps/{app_id}/secrets/{key}"
    data = _api_call("delete", url)

    if data.get("success"):
        console.print(f"[green]Secret '{key}' deleted for app '{app_id}'.[/green]")
    else:
        detail = data.get("detail", data.get("error", "Unknown error"))
        console.print(f"[bold red]Failed:[/bold red] {detail}")
        raise typer.Exit(1)
