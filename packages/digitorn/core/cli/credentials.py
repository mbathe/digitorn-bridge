"""CLI commands for the unified credential system."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

credentials_cli = typer.Typer(
    name="credentials",
    help="Universal credential management (API keys, OAuth, MCP).",
    no_args_is_help=True,
)


_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _parse_fields(field_args: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in field_args:
        if "=" not in f:
            raise typer.BadParameter(
                f"field {f!r} must be in the form key=value"
            )
        k, v = f.split("=", 1)
        out[k.strip()] = v
    return out


def _print_cred_table(creds: list[dict]) -> None:
    if not creds:
        console.print("[dim]No credentials.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Provider", style="cyan")
    table.add_column("Type")
    table.add_column("Label")
    table.add_column("Owner")
    table.add_column("Status")
    for c in creds:
        table.add_row(
            (c.get("id") or "")[:12],
            c.get("provider_name", ""),
            c.get("provider_type", ""),
            c.get("label") or (c.get("display_metadata") or {}).get("label") or "default",
            c.get("owner_type", "user"),
            c.get("status", ""),
        )
    console.print(table)


@credentials_cli.command(name="list")
def list_credentials(
    provider: str = typer.Option("", "--provider", "-p", help="Filter by provider."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """List every credential owned by the current user."""
    url = f"{daemon}/api/credentials"
    if provider:
        url += f"?provider={provider}"
    resp = daemon_request("get", url)
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    creds = data.get("data", {}).get("credentials") or []
    _print_cred_table(creds)


@credentials_cli.command(name="show")
def show_credential(
    credential_id: Annotated[str, typer.Argument()],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Show one credential's full metadata (no plaintext)."""
    resp = daemon_request("get", f"{daemon}/api/credentials/{credential_id}")
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    cred = data.get("data") or {}
    for k, v in cred.items():
        console.print(f"  [cyan]{k}:[/cyan] {v}")


@credentials_cli.command(name="create")
def create_credential(
    provider: str = typer.Option(..., "--provider", "-p", help="Provider name."),
    provider_type: str = typer.Option("api_key", "--type", "-t"),
    label: str = typer.Option("default", "--label", "-l"),
    name: str = typer.Option(
        "", "--name", "-n",
        help=(
            "Stable slug for the YAML 'credential:' block to reference. "
            "Must match ^[a-z][a-z0-9_-]{0,63}$. "
            "Defaults to provider name when omitted."
        ),
    ),
    field: list[str] = typer.Option(
        [], "--field", "-f",
        help="Field key=value. Repeatable.",
    ),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Create a new user-owned credential."""
    fields = _parse_fields(field)
    if not fields:
        console.print("[red]At least one --field key=value required.[/red]")
        raise typer.Exit(1)
    body: dict[str, Any] = {
        "provider_name": provider,
        "provider_type": provider_type,
        "label": label,
        "fields": fields,
    }
    if name:
        body["name"] = name
    resp = daemon_request("post", f"{daemon}/api/credentials", json=body)
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    cred = data.get("data") or {}
    console.print(f"[green]Created credential {cred.get('id')}[/green]")
    _print_cred_table([cred])


@credentials_cli.command(name="delete")
def delete_credential(
    credential_id: Annotated[str, typer.Argument()],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Delete a credential and all its grants."""
    resp = daemon_request(
        "delete", f"{daemon}/api/credentials/{credential_id}",
    )
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    console.print("[green]Deleted.[/green]")


@credentials_cli.command(name="grants")
def list_grants(
    app_id: str = typer.Option("", "--app", "-a"),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """List every grant owned by the current user."""
    resp = daemon_request("get", f"{daemon}/api/credentials-grants")
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    grants = data.get("data", {}).get("grants") or []
    if app_id:
        grants = [g for g in grants if g.get("app_id") == app_id]
    if not grants:
        console.print("[dim]No grants.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Grant ID", style="dim", width=12)
    table.add_column("App", style="cyan")
    table.add_column("Provider")
    table.add_column("Credential", style="dim", width=12)
    table.add_column("Granted At")
    for g in grants:
        table.add_row(
            (g.get("id") or "")[:12],
            g.get("app_id", ""),
            g.get("provider_name", ""),
            (g.get("credential_id") or "")[:12],
            (g.get("granted_at") or "")[:19],
        )
    console.print(table)


@credentials_cli.command(name="grant-add")
def grant_add(
    credential_id: Annotated[str, typer.Argument()],
    app_id: Annotated[str, typer.Argument()],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Authorize an app to use a credential."""
    resp = daemon_request(
        "post",
        f"{daemon}/api/credentials/{credential_id}/grants",
        json={"app_id": app_id},
    )
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Granted {credential_id[:12]} → {app_id}[/green]")


@credentials_cli.command(name="grant-revoke")
def grant_revoke(
    credential_id: Annotated[str, typer.Argument()],
    app_id: Annotated[str, typer.Argument()],
    hard: bool = typer.Option(False, "--hard", help="Hard-delete the row."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Revoke an app's access to a credential."""
    url = f"{daemon}/api/credentials/{credential_id}/grants/{app_id}"
    if hard:
        url += "?hard=true"
    resp = daemon_request("delete", url)
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Revoked {credential_id[:12]} from {app_id}[/green]")


@credentials_cli.command(name="admin-list")
def admin_list(
    provider: str = typer.Option("", "--provider", "-p"),
    app_id: str = typer.Option("", "--app", "-a"),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """[admin] List admin/system credentials visible to the daemon."""
    url = f"{daemon}/api/admin/credentials"
    q: list[str] = []
    if provider:
        q.append(f"provider={provider}")
    if app_id:
        q.append(f"app_id={app_id}")
    if q:
        url += "?" + "&".join(q)
    resp = daemon_request("get", url)
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    _print_cred_table(data.get("data", {}).get("credentials") or [])


@credentials_cli.command(name="admin-create")
def admin_create(
    provider: str = typer.Option(..., "--provider", "-p"),
    provider_type: str = typer.Option("api_key", "--type", "-t"),
    label: str = typer.Option("default", "--label", "-l"),
    app_id: str = typer.Option(
        "", "--app", "-a",
        help="Restrict to one app (enterprise case).",
    ),
    field: list[str] = typer.Option([], "--field", "-f"),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """[admin] Create a system credential visible to every user."""
    fields = _parse_fields(field)
    if not fields:
        console.print("[red]At least one --field key=value required.[/red]")
        raise typer.Exit(1)
    body = {
        "provider_name": provider,
        "provider_type": provider_type,
        "label": label,
        "fields": fields,
        "app_id": app_id or None,
    }
    resp = daemon_request("post", f"{daemon}/api/admin/credentials", json=body)
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    cred = data.get("data") or {}
    console.print(f"[green]Created system credential {cred.get('id')}[/green]")
    _print_cred_table([cred])


@credentials_cli.command(name="admin-delete")
def admin_delete(
    credential_id: Annotated[str, typer.Argument()],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """[admin] Delete a system credential."""
    resp = daemon_request(
        "delete", f"{daemon}/api/admin/credentials/{credential_id}",
    )
    data = resp.json()
    if not data.get("success"):
        console.print(f"[red]{data.get('error')}[/red]")
        raise typer.Exit(1)
    console.print("[green]Deleted.[/green]")
