"""CLI commands for app YAML management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

app_cli = typer.Typer(
    name="app",
    help="Application YAML management.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


@app_cli.command()
def validate(
    path: Annotated[Path, typer.Argument(help="Path to the app YAML file.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Validate an app YAML file without deploying it."""
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(1)

    resp = daemon_request(
        "post",
        f"{daemon}/api/apps/validate",
        json={"yaml_path": str(path.resolve())},
        timeout=30.0,
    )
    data = resp.json()

    if data.get("success"):
        console.print(f"[green]Valid[/green] - {data.get('data', {}).get('app_id', '?')}")
    else:
        console.print(f"[red]Invalid[/red] - {data.get('error', 'Unknown error')}")
        raise typer.Exit(1)


def _deploy_app(path: Path, daemon: str, force: bool) -> dict:
    """Shared deploy logic - returns the info dict on success, raises Exit on failure."""
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(1)

    console.print(f"Deploying [cyan]{path.name}[/cyan]...", end=" ")

    resp = daemon_request(
        "post",
        f"{daemon}/api/apps/deploy",
        json={"yaml_path": str(path.resolve()), "force": force},
        timeout=30.0,
    )
    data = resp.json()

    if not data.get("success"):
        console.print("[red]FAILED[/red]")
        console.print(f"  {data.get('error', 'Unknown error')}")
        raise typer.Exit(1)

    info = data["data"]
    console.print("[green]OK[/green]")
    console.print(f"  App:    [bold]{info['app_id']}[/bold]")
    console.print(f"  Tools:  {info.get('total_tools', '?')}")
    console.print(f"  Mode:   {info.get('mode', '?')}")
    console.print(f"  Agents: {', '.join(info.get('agents', []))}")
    return info


@app_cli.command()
def deploy(
    path: Annotated[Path, typer.Argument(help="Path to the app YAML file.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
    force: bool = typer.Option(False, "--force", "-f", help="Force redeploy if already deployed."),
) -> None:
    """Deploy an app to the running daemon."""
    _deploy_app(path, daemon, force)


@app_cli.command(name="list")
def list_apps(
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """List all deployed apps on the daemon."""
    resp = daemon_request("get", f"{daemon}/api/apps")
    data = resp.json()

    if not data.get("success"):
        console.print(f"[red]Error: {data.get('error', '?')}[/red]")
        raise typer.Exit(1)

    apps = data["data"]
    if isinstance(apps, dict):
        apps = apps.get("apps", [])

    if not apps:
        console.print("[dim]No apps deployed.[/dim]")
        return

    table = Table(title="Deployed Apps")
    table.add_column("App ID", style="cyan")
    table.add_column("Name")
    table.add_column("Mode")
    table.add_column("Tools", justify="right")
    table.add_column("Agents")

    for app in apps:
        table.add_row(
            app.get("app_id", "?"),
            app.get("name", "?"),
            app.get("mode", "?"),
            str(app.get("total_tools", "?")),
            ", ".join(app.get("agents", [])),
        )

    console.print(table)


@app_cli.command()
def undeploy(
    app_id: Annotated[str, typer.Argument(help="App ID to undeploy.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Stop an app in memory but keep its bundle + DB rows.

    The app remains on disk and is automatically re-deployed at the next
    daemon restart. Use `digitorn app delete` to remove it permanently.
    """
    resp = daemon_request(
        "delete",
        f"{daemon}/api/apps/{app_id}",
        params={"undeploy_only": "true"},
    )
    data = resp.json()

    if data.get("success"):
        console.print(f"[green]Stopped[/green] {app_id}")
        console.print(
            "[dim]Bundle + DB preserved. Will reload at next daemon restart.[/dim]"
        )
        console.print(
            f"[dim]Run [bold]digitorn app delete {app_id}[/bold] to remove permanently.[/dim]"
        )
    else:
        console.print(f"[red]Failed[/red] - {data.get('error', '?')}")
        raise typer.Exit(1)


@app_cli.command()
def delete(
    app_id: Annotated[str, typer.Argument(help="App ID to delete.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Permanently remove an app - bundle, DB rows, sessions, and secrets.

    This is irreversible. The daemon will NOT reload the app at the next
    restart. To temporarily stop an app without losing data, use
    `digitorn app undeploy` instead.
    """
    if not yes:
        console.print(
            f"[yellow]⚠  This will permanently delete '{app_id}' - "
            f"bundles on disk, DB rows, sessions and secrets.[/yellow]"
        )
        try:
            confirm = typer.confirm("Proceed?", default=False)
        except typer.Abort:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    resp = daemon_request("delete", f"{daemon}/api/apps/{app_id}")
    data = resp.json()

    if not data.get("success"):
        console.print(f"[red]Failed[/red] - {data.get('error', '?')}")
        raise typer.Exit(1)

    info = data.get("data", {})
    console.print(f"[green]Deleted[/green] {app_id}")
    console.print(f"  Bundles removed: {info.get('bundles_deleted', 0)}")
    console.print(f"  Secrets purged:  {info.get('secrets_deleted', 0)}")
    console.print(f"  DB row removed:  {info.get('db_removed', False)}")


@app_cli.command()
def run(
    path: Annotated[Path, typer.Argument(help="Path to the app YAML file.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Deploy and run an app. Background apps auto-start their triggers.

    Equivalent to 'deploy --force' but also shows trigger/channel status
    for background mode apps.
    """
    import time

    info = _deploy_app(path, daemon, force=True)

    # For background apps, poll for trigger readiness (up to 3 attempts)
    if info.get("mode") == "background":
        triggers: list = []
        channels: list = []
        for attempt in range(3):
            time.sleep(0.5)
            resp = daemon_request("get", f"{daemon}/api/apps/{info['app_id']}/triggers")
            tdata = resp.json()
            if tdata.get("success"):
                td = tdata["data"]
                triggers = td.get("triggers", [])
                channels = td.get("channels", [])
                if triggers or channels:
                    break
        if triggers:
            console.print(f"  Triggers: {len(triggers)}")
            for t in triggers:
                console.print(f"    [{t['type']}] {t['id']} - {t.get('schedule', t.get('path', ''))}")
        if channels:
            console.print(f"  Channels: {len(channels)}")
            for c in channels:
                status = c.get("status", "?")
                style = "green" if status == "active" else "yellow"
                console.print(f"    [{style}]{c['name']}[/{style}] ({c['type']})")
        console.print()
        console.print("[green]Background app running.[/green] Triggers are listening.")
    else:
        console.print()
        console.print(f"[green]App deployed.[/green] Use chat or /sessions/{{sid}}/messages to interact.")


@app_cli.command()
def schema(
    module_id: Annotated[str, typer.Argument(help="Module ID to show schema for.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d", help="Daemon URL."),
) -> None:
    """Show the YAML configuration schema for a module."""
    resp = daemon_request("get", f"{daemon}/api/modules/{module_id}")
    data = resp.json()

    if resp.status_code == 404:
        console.print(f"[red]Module '{module_id}' not found.[/red]")
        raise typer.Exit(1)

    console.print(json.dumps(data, indent=2))
